"""Serialized SQLite access and transactional alert/outbox state."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .migrations import migrate
from .models import Channel, LogRecord
from .persistence import enqueue, observe, row, rows


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self._connection: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA busy_timeout=5000")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        try:
            await migrate(self._connection)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.lock:
            if self._connection is None:
                raise RuntimeError("数据库未打开")
            try:
                yield self._connection
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise

    async def get_meta(self, key: str) -> str | None:
        async with self.transaction() as db:
            result = await row(db, "SELECT value FROM metadata WHERE id=?", (key,))
        return str(result["value"]) if result else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self.transaction() as db:
            await self.put_meta(db, key, value, time.time())

    @staticmethod
    async def put_meta(db: aiosqlite.Connection, key: str, value: str, now: float) -> None:
        await db.execute(
            """INSERT INTO metadata(id,value,created_at,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (key, value, now, now),
        )

    async def channels(self) -> list[dict[str, Any]]:
        async with self.transaction() as db:
            records = await rows(
                db, "SELECT payload,updated_at FROM channels WHERE is_present=1 ORDER BY id"
            )
        return [
            {**json.loads(item["payload"]), "updated_at": item["updated_at"]} for item in records
        ]

    async def sync_channels(
        self, channels: list[Channel], now: float, balance_threshold: float | None = None
    ) -> None:
        async with self.transaction() as db:
            old = await rows(db, "SELECT id,status,is_present FROM channels")
            known = {item["id"]: item for item in old}
            for channel in channels:
                await self._sync_channel(db, channel, known.get(channel.id), now, balance_threshold)
            current_ids = {channel.id for channel in channels}
            for item in old:
                if item["id"] not in current_ids and item["is_present"]:
                    await db.execute(
                        "UPDATE channels SET is_present=0,updated_at=? WHERE id=?",
                        (now, item["id"]),
                    )
                    await observe(
                        db,
                        f"channel:{item['id']}:missing",
                        True,
                        f"渠道 {item['id']} 从完整列表中缺失",
                        "warning",
                        now,
                    )
            await self.put_meta(db, "channels_last_ok", str(now), now)

    @staticmethod
    async def _sync_channel(
        db: aiosqlite.Connection,
        channel: Channel,
        previous: dict[str, Any] | None,
        now: float,
        threshold: float | None,
    ) -> None:
        await db.execute(
            """INSERT INTO channels(id,payload,status,created_at,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status=excluded.status,
            is_present=1,updated_at=excluded.updated_at""",
            (channel.id, channel.model_dump_json(), channel.status, now, now),
        )
        prefix = f"channel:{channel.id}"
        if channel.status in {1, 3}:
            await observe(
                db,
                prefix + ":auto_disabled",
                channel.status == 3,
                f"渠道 {channel.id} {channel.name}：自动禁用异常 / 已恢复启用",
                "critical",
                now,
            )
        await observe(db, prefix + ":missing", False, f"渠道 {channel.id} 重新出现", "info", now)
        if previous and previous["status"] != 2 and channel.status == 2:
            await enqueue(
                db,
                f"manual:{channel.id}:{now}",
                f"**状态变更**\n渠道 {channel.id} {channel.name}：手动禁用（非故障判定）",
                now,
            )
        if threshold is not None and channel.balance is not None:
            await observe(
                db,
                prefix + ":balance",
                channel.balance < threshold,
                f"渠道 {channel.id} {channel.name}：余额 {channel.balance:g}，阈值 {threshold:g}",
                "warning",
                now,
            )

    async def condition(self, key: str, active: bool, title: str, level: str, now: float) -> None:
        async with self.transaction() as db:
            await observe(db, key, active, title, level, now)

    async def alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.transaction() as db:
            return await rows(
                db,
                """SELECT id,key,state,title,level,generation,created_at,updated_at
                FROM alerts ORDER BY updated_at DESC LIMIT ?""",
                (limit,),
            )

    async def notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.transaction() as db:
            return await rows(
                db,
                """SELECT id,content,status,attempts,next_at,last_error
                FROM notifications ORDER BY id LIMIT ?""",
                (limit,),
            )

    async def silence(self, channel_id: int, until: float, now: float) -> None:
        async with self.transaction() as db:
            await db.execute(
                """INSERT INTO silences(id,until_at,created_at,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET until_at=excluded.until_at,updated_at=excluded.updated_at""",
                (channel_id, until, now, now),
            )

    async def save_logs(
        self,
        logs: list[LogRecord],
        end: int,
        start: int,
        *,
        advance_cursor: bool = True,
        replace_window: bool = False,
    ) -> None:
        async with self.transaction() as db:
            if replace_window:
                await db.execute(
                    "DELETE FROM logs WHERE created_at>=? AND created_at<=?", (start, end)
                )
            await db.executemany(
                """INSERT OR IGNORE INTO logs
                (id,type,channel_id,user_id,username,model_name,quota,prompt_tokens,completion_tokens,use_time,created_at,updated_at,http_status,error_code,error_message,channel_name)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        item.id,
                        item.type,
                        item.channel_id,
                        item.user_id,
                        item.username,
                        item.model_name,
                        item.quota,
                        item.prompt_tokens,
                        item.completion_tokens,
                        item.use_time,
                        item.created_at,
                        end,
                        item.http_status,
                        item.error_code,
                        item.error_message,
                        item.channel_name,
                    )
                    for item in logs
                    if start <= item.created_at <= end
                ],
            )
            if advance_cursor:
                coverage = await row(db, "SELECT value FROM metadata WHERE id='log_coverage_start'")
                if coverage is None:
                    await self.put_meta(db, "log_coverage_start", str(start), end)
            if advance_cursor:
                await self.put_meta(db, "log_cursor", str(end), end)
                await self.put_meta(db, "logs_last_ok", str(end), end)

    async def summary(self, start: int, end: int, channel_id: int = 0) -> dict[str, Any]:
        async with self.transaction() as db:
            result = await row(
                db,
                """SELECT
                COALESCE(SUM(CASE WHEN type=2 THEN 1 ELSE 0 END),0) AS success_records,
                COALESCE(SUM(CASE WHEN type=5 THEN 1 ELSE 0 END),0) AS error_records,
                COALESCE(SUM(CASE WHEN type=2 THEN quota ELSE 0 END),0) AS quota,
                COALESCE(SUM(CASE WHEN type=2 THEN prompt_tokens+completion_tokens ELSE 0 END),0) AS tokens
                FROM logs WHERE created_at>=? AND created_at<? AND (?=0 OR channel_id=?)""",
                (start, end, channel_id, channel_id),
            )
        return result or {"success_records": 0, "error_records": 0, "quota": 0, "tokens": 0}

    async def recent_errors(self, start: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self.transaction() as db:
            return await rows(
                db,
                """SELECT id,created_at,channel_id,model_name FROM logs
                WHERE type=5 AND created_at>=? ORDER BY created_at DESC,id DESC LIMIT ?""",
                (start, limit),
            )

    async def hourly(self, start: int, end: int) -> list[dict[str, Any]]:
        async with self.transaction() as db:
            return await rows(
                db,
                """SELECT CAST(created_at/3600 AS INTEGER)*3600 AS bucket,
                COUNT(*) AS calls, SUM(quota) AS quota FROM logs
                WHERE type=2 AND created_at>=? AND created_at<? GROUP BY bucket ORDER BY bucket""",
                (start, end),
            )

    async def receipt(self, key: str, expires: float, now: float) -> bool:
        async with self.transaction() as db:
            await db.execute("DELETE FROM receipts WHERE id=? AND expires_at<=?", (key, now))
            cursor = await db.execute(
                "INSERT OR IGNORE INTO receipts(id,expires_at,created_at,updated_at) VALUES(?,?,?,?)",
                (key, expires, now, now),
            )
            return cursor.rowcount == 1

    async def enqueue(self, key: str, text: str, now: float, image: bytes | None = None) -> None:
        async with self.transaction() as db:
            await enqueue(db, key, text, now, image)

    async def claim_notification(self, now: float, lease: float = 120) -> dict[str, Any] | None:
        async with self.transaction() as db:
            item = await row(
                db,
                """SELECT id,content,image,status,attempts,next_at,lease_until,kind,mention_all,binding_ids
                FROM notifications WHERE status IN ('pending','sending') ORDER BY id LIMIT 1""",
            )
            if (
                item is None
                or item["next_at"] > now
                or (item["status"] == "sending" and item["lease_until"] > now)
            ):
                return None
            await db.execute(
                "UPDATE notifications SET status='sending',lease_until=?,updated_at=? WHERE id=?",
                (now + lease, now, item["id"]),
            )
            return item

    async def finish_notification(
        self,
        notification_id: int,
        status: str,
        attempts: int,
        next_at: float,
        error: str,
        now: float,
    ) -> None:
        async with self.transaction() as db:
            await db.execute(
                """UPDATE notifications SET status=?,attempts=?,next_at=?,lease_until=0,
                last_error=?,updated_at=? WHERE id=?""",
                (status, attempts, next_at, error, now, notification_id),
            )
            await self.put_meta(db, "notification_last_attempt", str(now), now)

    async def queue_counts(self) -> dict[str, int]:
        async with self.transaction() as db:
            result = await rows(
                db, "SELECT status,COUNT(*) AS count FROM notifications GROUP BY status"
            )
        return {item["status"]: item["count"] for item in result}

    async def save_probe(self, channel_id: int, success: bool, latency: float, now: float) -> int:
        async with self.transaction() as db:
            await db.execute(
                "INSERT INTO probes(channel_id,success,latency,created_at,updated_at) VALUES(?,?,?,?,?)",
                (channel_id, int(success), latency, now, now),
            )
            recent = await rows(
                db,
                "SELECT success FROM probes WHERE channel_id=? ORDER BY id DESC LIMIT 20",
                (channel_id,),
            )
        count = 0
        for item in recent:
            if item["success"]:
                break
            count += 1
        return count

    async def prune(self, before: float) -> None:
        async with self.transaction() as db:
            coverage = await row(db, "SELECT value FROM metadata WHERE id='log_coverage_start'")
            if coverage is not None and float(coverage["value"]) < before:
                await self.put_meta(db, "log_coverage_start", str(int(before)), time.time())
            await db.execute("DELETE FROM logs WHERE created_at<?", (before,))
            await db.execute("DELETE FROM probes WHERE created_at<?", (before,))
            await db.execute("DELETE FROM error_seen WHERE updated_at<?", (before,))
            await db.execute(
                "DELETE FROM error_buckets WHERE pending_count=0 AND updated_at<?", (before,)
            )
            await db.execute(
                "DELETE FROM alerts WHERE state='event' AND updated_at<? AND id NOT IN (SELECT alert_id FROM alert_acknowledgements)",
                (before,),
            )
            await db.execute(
                "DELETE FROM metadata WHERE id LIKE 'policy:run:%' AND updated_at<?", (before,)
            )
            await db.execute(
                "DELETE FROM metadata WHERE id LIKE 'report_job:%' AND updated_at<?", (before,)
            )
            await db.execute("DELETE FROM receipts WHERE expires_at<?", (before,))
            await db.execute(
                "DELETE FROM notifications WHERE status='sent' AND updated_at<?", (before,)
            )

    async def accept_event(self, digest: str, event_type: str, now: float, max_age: int) -> bool:
        async with self.transaction() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO receipts(id,expires_at,created_at,updated_at) VALUES(?,?,?,?)",
                ("event:" + digest, now + max_age * 2, now, now),
            )
            if cursor.rowcount != 1:
                return False
            # Events are durable hints only. Current status always comes from reconciliation.
            await self.put_meta(db, "channel_event_pending", "1", now)
            return True

    async def forget_receipt(self, key: str) -> None:
        async with self.transaction() as db:
            await db.execute("DELETE FROM receipts WHERE id=?", (key,))

    async def bind_instance(self, identity: str) -> None:
        async with self.transaction() as db:
            existing = await row(db, "SELECT value FROM metadata WHERE id='instance'")
            if existing and existing["value"] != identity:
                raise ValueError("此数据库属于另一个NewAPI实例/模式，请为新实例设置独立数据库路径")
            await self.put_meta(db, "instance", identity, time.time())

    async def take_event(self) -> bool:
        async with self.transaction() as db:
            result = await row(db, "SELECT value FROM metadata WHERE id='channel_event_pending'")
            if not result or result["value"] != "1":
                return False
            await self.put_meta(db, "channel_event_pending", "0", time.time())
            return True

    async def queue_report(self, day: str, content: str, image: bytes | None, now: float) -> None:
        async with self.transaction() as db:
            await enqueue(db, f"daily:{day}:text", content, now)
            if image:
                await enqueue(db, f"daily:{day}:image", "日报图表", now, image)
            await self.put_meta(db, "daily_report", day, now)

    async def extend_receipt(self, key: str, expires: float) -> None:
        async with self.transaction() as db:
            await db.execute(
                "UPDATE receipts SET expires_at=?,updated_at=? WHERE id=?",
                (expires, time.time(), key),
            )

    async def latest_probe(self, channel_id: int) -> dict[str, Any] | None:
        async with self.transaction() as db:
            return await row(
                db,
                "SELECT success,created_at FROM probes WHERE channel_id=? ORDER BY id DESC LIMIT 1",
                (channel_id,),
            )
