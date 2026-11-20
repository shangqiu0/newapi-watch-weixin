"""Durable error aggregation, per-account balance checks and report schedules."""

import hashlib
import json
import time
from datetime import datetime
from string import Formatter
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiosqlite

from ..client import UpstreamError
from ..models import LogRecord
from ..notifications import truncate_utf8
from ..persistence import enqueue, row, rows
from ..redaction import redact_text
from .error_scans import SCAN_KEY, ErrorScan
from .models import ERROR_TEMPLATE, ErrorRule, MonitoringConfig, order_standard_error_notice

if TYPE_CHECKING:
    from ..runtime import Runtime


def preview(rule: ErrorRule, sample: dict[str, Any], *, timezone: str = "Asia/Shanghai") -> str:
    defaults = dict(
        channel_name="from bailian_支持缓存写",
        channel_id=93,
        status_code=400,
        model="kimi-k3.1-pro",
        error_message="status_code=400, developer is not supported",
        log_time="未提供",
        request_id="未提供",
        upstream_request_id="未提供",
        count=7,
    )
    values = {**defaults, **{k: v for k, v in sample.items() if k in defaults}}
    if "log_created_at" in sample:
        values["log_time"] = error_log_time(sample["log_created_at"], timezone)
    for field in ("log_time", "request_id", "upstream_request_id"):
        values[field] = values[field] or "未提供"
    template = rule.template
    fields = {name for _, name, _, _ in Formatter().parse(template)}
    metadata = [
        f"{label}：{{{field}}}"
        for field, label in (
            ("log_time", "错误日志时间"),
            ("upstream_request_id", "上游请求 ID"),
            ("request_id", "平台请求 ID"),
        )
        if field not in fields
    ]
    if metadata:
        # Preserve custom layouts while keeping omitted trace details visible.
        title, separator, body = template.partition("\n")
        template = "\n".join([title, *metadata, body] if separator else [*metadata, template])
    template = order_standard_error_notice(template)
    if template == ERROR_TEMPLATE:
        # Keep the trace fields after the error text without exceeding the text Webhook budget.
        for field, limit in (("channel_name", 160), ("model", 120), ("error_message", 900)):
            values[field] = truncate_utf8(str(values[field]), limit)
    return template.format_map(values)


def error_log_time(created_at: int, timezone: str) -> str:
    if type(created_at) is not int or created_at <= 0:
        return "未提供"
    try:
        stamp = datetime.fromtimestamp(created_at, ZoneInfo(timezone))
        return f"{stamp.year}年{stamp.month}月{stamp.day}日 {stamp:%H:%M:%S}"
    except (OverflowError, OSError, ValueError):
        return "未提供"


async def record_alert(
    db: aiosqlite.Connection, key: str, title: str, active: bool, level: str, now: float
) -> None:
    await db.execute(
        """INSERT INTO alerts(id,key,state,title,level,generation,created_at,updated_at)
        VALUES(?,?,?,?,?,1,?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state,
        title=excluded.title,level=excluded.level,updated_at=excluded.updated_at,
        generation=CASE WHEN alerts.state IN ('resolved','event') AND excluded.state='firing' THEN alerts.generation+1 ELSE alerts.generation END""",
        (uuid4().hex, key, "firing" if active else "resolved", title, level, now, now),
    )


class MonitoringService:
    def __init__(self, runtime: "Runtime", config: MonitoringConfig) -> None:
        self.runtime, self.config = runtime, config
        self.balance_next = 0.0
        from .engine import PolicyEngine

        self.engine = PolicyEngine(self)
        from .report_jobs import ScheduledReports

        self.report_jobs = ScheduledReports(runtime)

    async def errors(
        self,
        records: list[LogRecord],
        *,
        now: float | None = None,
        checkpoint: ErrorScan | None = None,
        complete: bool = False,
    ) -> None:
        rule = self.config.errors
        if not rule.enabled:
            return
        now = time.time() if now is None else now
        revision = "policy-errors-v1"
        async with self.runtime.store.transaction() as db:
            for log in records:
                if (
                    log.type != 5
                    or log.http_status not in rule.status_codes
                    or (rule.channel_ids and log.channel_id not in rule.channel_ids)
                    or (rule.models and log.model_name not in rule.models)
                    or (rule.error_codes and log.error_code not in rule.error_codes)
                ):
                    continue
                seen = await db.execute(
                    "INSERT OR IGNORE INTO error_seen(id,created_at,updated_at) VALUES(?,?,?)",
                    (f"{revision}:{log.id}", now, now),
                )
                if seen.rowcount != 1:
                    continue
                key = (
                    revision
                    + ":"
                    + hashlib.sha256(
                        f"{log.channel_id}:{log.http_status}:{log.error_code}:{log.model_name}".encode()
                    ).hexdigest()
                )
                sample = dict(
                    channel_name=log.channel_name or f"渠道{log.channel_id}",
                    channel_id=log.channel_id,
                    status_code=log.http_status,
                    error_code=log.error_code or "未提供",
                    model=log.model_name,
                    error_message=redact_text(log.error_message, self.runtime.settings),
                    log_id=log.id,
                    log_created_at=log.created_at,
                    request_id=log.request_id,
                    upstream_request_id=log.upstream_request_id,
                )
                sample = {
                    key: redact_text(value, self.runtime.settings)
                    if isinstance(value, str)
                    else value
                    for key, value in sample.items()
                }
                await db.execute(
                    """INSERT INTO error_buckets(id,pending_count,sample,created_at,updated_at)
                    VALUES(?,1,?,?,?) ON CONFLICT(id) DO UPDATE SET pending_count=pending_count+1,sample=excluded.sample,updated_at=excluded.updated_at""",
                    (key, json.dumps(sample, ensure_ascii=False), now, now),
                )
            buckets = await rows(
                db,
                "SELECT id,last_sent_at,pending_count,sample FROM error_buckets WHERE pending_count>=? AND last_sent_at<=? AND id LIKE ?",
                (rule.confirmations, now - rule.cooldown_seconds, revision + ":%"),
            )
            for bucket in buckets:
                sample = json.loads(bucket["sample"])
                sample["count"] = bucket["pending_count"]
                if "log_created_at" not in sample:
                    # Old samples contain a preformatted Beijing time, not the original timestamp.
                    sample.pop("log_time", None)
                text = redact_text(
                    preview(rule, sample, timezone=self.runtime.settings.timezone),
                    self.runtime.settings,
                )
                await record_alert(
                    db,
                    "request_error:" + bucket["id"],
                    text,
                    True,
                    "critical" if sample["status_code"] >= 500 else "warning",
                    now,
                )
                await db.execute(
                    "UPDATE alerts SET state='event' WHERE key=?",
                    ("request_error:" + bucket["id"],),
                )
                await enqueue(db, f"error:{bucket['id']}:{now}", text, now)
                await db.execute(
                    "UPDATE notifications SET kind='request_error',mention_all=?,binding_ids=? WHERE dedupe_key=?",
                    (
                        int(rule.mention_all),
                        json.dumps(rule.binding_ids),
                        f"error:{bucket['id']}:{now}",
                    ),
                )
                await db.execute(
                    "UPDATE error_buckets SET pending_count=0,last_sent_at=?,updated_at=? WHERE id=?",
                    (now, now, bucket["id"]),
                )

            if checkpoint is not None:
                if complete:
                    await db.execute("DELETE FROM metadata WHERE id=?", (SCAN_KEY,))
                    await self.runtime.store.put_meta(
                        db, "policy:error_cursor", str(checkpoint.end), now
                    )
                    await self.runtime.store.put_meta(db, "logs_last_ok", str(now), now)
                else:
                    await self.runtime.store.put_meta(
                        db, SCAN_KEY, checkpoint.model_dump_json(), now
                    )

    async def balances(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now < self.balance_next:
            return
        self.balance_next = now + self.config.balance_poll_seconds
        for rule in self.config.balances:
            if not rule.enabled:
                continue
            try:
                user = await self.runtime.client.user(rule.user_id)
            except UpstreamError as error:
                await self.runtime.store.set_meta(f"balance_error:{rule.user_id}", str(error))
                continue
            await self.runtime.store.set_meta(f"balance_error:{rule.user_id}", "")
            low = user.quota < rule.threshold
            async with self.runtime.store.transaction() as db:
                prior = await row(
                    db, "SELECT is_low FROM account_samples WHERE id=?", (rule.user_id,)
                )
                await db.execute(
                    """INSERT INTO account_samples(id,quota,threshold,is_low,created_at,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET quota=excluded.quota,threshold=excluded.threshold,is_low=excluded.is_low,updated_at=excluded.updated_at""",
                    (rule.user_id, user.quota, rule.threshold, int(low), now, now),
                )
                changed = (low and (prior is None or not prior["is_low"])) or (
                    not low and prior and prior["is_low"] and rule.notify_recovery
                )
                if changed:
                    text = f"{'余额不足提醒' if low else '余额恢复通知'}\n用户 #{rule.user_id} · {rule.name or user.username}\n当前余额：{user.quota:,} quota\n监控阈值：{rule.threshold:,} quota\n时间：{datetime.fromtimestamp(now, ZoneInfo(self.runtime.settings.timezone)).strftime('%Y年%m月%d日 %H:%M:%S')}"
                    key = f"balance:{rule.user_id}:{int(low)}:{now}"
                    await record_alert(db, f"balance:{rule.user_id}", text, low, "warning", now)
                    await enqueue(db, key, redact_text(text, self.runtime.settings), now)
                    await db.execute(
                        "UPDATE notifications SET kind='balance',mention_all=? WHERE dedupe_key=?",
                        (int(rule.mention_all), key),
                    )

    async def reports(
        self, now: datetime | None = None, *, only: str | None = None, retry: bool = False
    ) -> dict[str, Any]:
        now = now or datetime.now(ZoneInfo(self.runtime.settings.timezone))
        result: dict[str, Any] = {"report_stage": "scheduled", "note": "报告调度已停用"}
        for kind, schedule in [("daily", self.config.daily), ("weekly", self.config.weekly)]:
            if only and kind != only:
                continue
            if schedule.enabled:
                result = await self.report_jobs.advance(kind, schedule, now, retry=retry)
        return result

    async def initialize(self) -> None:
        async with self.runtime.store.transaction() as db:
            if not await row(db, "SELECT id FROM metadata WHERE id='policy:report_route_v2'"):
                await db.execute("""UPDATE notifications SET status='historical',lease_until=0,
                    last_error='旧日报/周报超时重复通知，已停止历史重放；未实际补发'
                    WHERE kind='service' AND status='pending' AND
                    (dedupe_key LIKE 'policy:policy:source:daily:%' OR dedupe_key LIKE 'policy:policy:source:weekly:%')""")
                await db.execute(
                    "UPDATE monitor_checks SET next_at=0 WHERE id IN ('daily','weekly')"
                )
                await self.runtime.store.put_meta(db, "policy:report_route_v2", "1", time.time())
        # Preserve old messages for review, but never replay a collector backlog as new policy alerts.
        async with self.runtime.store.transaction() as db:
            if await row(db, "SELECT id FROM metadata WHERE id='policy:mode_initialized'"):
                return
            changed = await db.execute("""UPDATE notifications SET status='historical',lease_until=0,
                last_error='旧采集策略遗留通知，已暂停自动投递；确认仍需发送时可手动重试'
                WHERE status IN ('pending','sending') AND
                (dedupe_key LIKE 'alert:%' OR dedupe_key LIKE 'daily:%' OR
                 (dedupe_key LIKE 'error:%' AND dedupe_key NOT LIKE 'error:policy-errors-v1:%'))""")
            now = time.time()
            await self.runtime.store.put_meta(
                db, "policy:legacy_notifications_paused", str(changed.rowcount), now
            )
            await self.runtime.store.put_meta(db, "policy:mode_initialized", "1", now)

    async def configure(self, config: MonitoringConfig) -> None:
        async with self.engine.lock:
            await self.engine.cancel()
            prior = self.config
            self.config = config
            self.runtime.client.throttle.configure(
                0 if self.runtime.settings.demo else config.requests.min_interval_seconds,
                config.requests.rate_limit_cooldown_seconds,
            )
            async with self.runtime.store.transaction() as db:
                await db.execute("UPDATE monitor_checks SET next_at=0 WHERE id<>''")
                if prior.channels != config.channels:
                    await db.execute(
                        "UPDATE monitor_states SET streak=0 WHERE id LIKE 'policy:channel:%'"
                    )
                if prior.source != config.source:
                    await db.execute(
                        "UPDATE monitor_states SET streak=0 WHERE id LIKE 'policy:source:%'"
                    )
                for rule in config.balances:
                    old = next((r for r in prior.balances if r.user_id == rule.user_id), None)
                    if old != rule:
                        await db.execute(
                            "UPDATE monitor_states SET streak=0 WHERE id=?",
                            (f"policy:balance:{rule.user_id}",),
                        )
                if prior.errors != config.errors:
                    await db.execute("DELETE FROM metadata WHERE id=?", (SCAN_KEY,))
                    await db.execute(
                        "UPDATE error_buckets SET pending_count=0 WHERE id LIKE 'policy-errors-v1:%'"
                    )
                if not prior.errors.enabled and config.errors.enabled:
                    await db.execute(
                        "DELETE FROM metadata WHERE id IN ('policy:error_cursor','policy:error_floor')"
                    )

    async def schedule(self, *, now: float | None = None) -> None:
        await self.engine.tick(now=now, wait=False)

    async def close(self) -> None:
        await self.engine.cancel()

    async def tick(self, *, now: float | None = None, force: bool = False) -> None:
        await self.engine.tick(now=now, force=force)

    async def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        return await self.engine.snapshot(now=now)
