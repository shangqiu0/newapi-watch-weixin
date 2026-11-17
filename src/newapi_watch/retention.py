"""Platform-local, bounded retention. Runtime cursors/cooldowns are never UI trash."""

import json
import time
from typing import Annotated, Any, Literal
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from .operations import OperationError
from .persistence import row, rows
from .store import Store

Kind = Literal["logs", "notifications", "tasks", "alerts", "audits"]


# Legacy defaults retained: completed query jobs were retained one day, not seven.
class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    confirmed: bool = False
    logs_days: int = Field(default=30, ge=1, le=365)
    history_days: int = Field(default=30, ge=1, le=365)
    tasks_days: int = Field(default=1, ge=1, le=365)
    audits_days: int = Field(default=30, ge=1, le=365)


class CleanupSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    before: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=100
    )


# SQL identifiers are exclusively from this constant, never request-controlled.
TABLES = {
    "logs": "logs",
    "notifications": "notifications",
    "tasks": "query_jobs",
    "alerts": "alerts",
    "audits": "command_audits",
}
SAFE = {
    "logs": "1=1",
    "audits": "1=1",
    "notifications": "status IN ('sent','historical','cancelled','dead') AND lease_until=0",
    "tasks": "status IN ('complete','cancelled','failed') AND (status!='complete' OR notified_at>0 OR COALESCE(json_extract(context_json,'$.binding_id'),'web')='web' OR COALESCE(json_extract(context_json,'$.delivery_stopped'),0)=1)",
    "alerts": "state IN ('resolved','event') AND NOT EXISTS (SELECT 1 FROM metadata WHERE id='history:hidden:'||alerts.id AND value=CAST(alerts.generation AS TEXT))",
}


class RetentionService:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def selector(selection: CleanupSelection) -> tuple[str, tuple[Any, ...]]:
        conditions: list[str] = []
        values: list[Any] = []
        if selection.before is not None:
            field = "created_at" if selection.kind in {"logs", "audits"} else "updated_at"
            conditions.append(field + "<?")
            values.append(selection.before)
        if selection.ids:
            conditions.append("id IN (" + ",".join("?" for _ in selection.ids) + ")")
            values.extend(selection.ids)
        if not conditions:
            raise OperationError(400, "请指定截止时间或选中记录，不能无条件清空")
        return " AND ".join(conditions), tuple(values)

    async def preview(self, selection: CleanupSelection) -> dict[str, Any]:
        if selection.kind == "audits" and selection.ids:
            raise OperationError(400, "操作记录仅支持按保留时间清理")
        condition, params = self.selector(selection)
        table, safe = TABLES[selection.kind], SAFE[selection.kind]
        now = time.time()
        if selection.before is not None and selection.before > now:
            raise OperationError(400, "不能清理未来时间范围")
        async with self.store.transaction() as db:
            count = await row(db, f"SELECT COUNT(*) AS n FROM {table} WHERE {condition}", params)
            eligible = await row(
                db, f"SELECT COUNT(*) AS n FROM {table} WHERE ({condition}) AND ({safe})", params
            )
            version_fields = (
                ",status"
                if selection.kind in {"notifications", "tasks"}
                else ",generation,state"
                if selection.kind == "alerts"
                else ""
            )
            items = await rows(
                db,
                f"SELECT id,created_at,updated_at{version_fields} FROM {table} WHERE ({condition}) AND ({safe}) ORDER BY rowid LIMIT 100",
                params,
            )
            plan: dict[str, Any] = {
                "kind": selection.kind,
                "ids": [str(i["id"]) for i in items],
                "versions": {
                    str(i["id"]): {k: v for k, v in i.items() if k != "id"} for i in items
                },
                "preview_id": "clean_" + uuid4().hex,
                "expires_at": now + 600,
                "count": eligible["n"] if eligible else 0,
                "protected": (count["n"] if count else 0) - (eligible["n"] if eligible else 0),
                "batch_size": len(items),
                "before": selection.before,
            }
            await self.store.put_meta(
                db, "retention:preview:" + plan["preview_id"], json.dumps(plan), now
            )
        return {k: v for k, v in plan.items() if k != "versions"}

    async def execute(self, preview_id: str) -> dict[str, Any]:
        now = time.time()
        async with self.store.transaction() as db:
            raw = await row(
                db, "SELECT value FROM metadata WHERE id=?", ("retention:preview:" + preview_id,)
            )
            if not raw:
                raise OperationError(404, "清理预览不存在或不属于当前平台")
            plan = json.loads(raw["value"])
            if "result" in plan:
                return dict(plan["result"])
            if plan["expires_at"] < now:
                raise OperationError(409, "清理预览已过期，请重新预览")
            kind = plan["kind"]
            deleted = 0
            for identifier in plan["ids"]:
                safe = SAFE[kind]
                version = plan.get("versions", {}).get(identifier)
                expected = {"created_at", "updated_at"}
                expected.update(
                    {"status"}
                    if kind in {"notifications", "tasks"}
                    else {"generation", "state"}
                    if kind == "alerts"
                    else set()
                )
                if not version or set(version) != expected:
                    continue  # Older/invalid previews must be regenerated, never weakened.
                predicate = " AND ".join(key + "=?" for key in version)
                params = (identifier, *version.values())
                safe = f"({safe}) AND ({predicate})"
                if kind == "alerts":
                    found = await row(
                        db, f"SELECT generation FROM alerts WHERE id=? AND ({safe})", params
                    )
                    if found:
                        await self.store.put_meta(
                            db, "history:hidden:" + identifier, str(found["generation"]), now
                        )
                        # Keep ID/generation for alert deduplication, discard historical display text.
                        await db.execute(
                            "UPDATE alerts SET title='' WHERE id=? AND state IN ('resolved','event')",
                            (identifier,),
                        )
                        deleted += 1
                else:
                    cursor = await db.execute(
                        f"DELETE FROM {TABLES[kind]} WHERE id=? AND ({safe})", params
                    )
                    deleted += max(0, cursor.rowcount)
            if kind == "logs" and deleted:
                # Selected-ID deletion creates holes. Conservatively invalidate prior local coverage.
                await self.store.put_meta(db, "log_coverage_start", str(int(now)), now)
            result = {
                "kind": kind,
                "deleted": deleted,
                "skipped": len(plan["ids"]) - deleted,
                "remaining": max(0, plan["count"] - deleted),
                "finished_at": now,
            }
            plan["result"] = result
            await self.store.put_meta(db, "retention:preview:" + preview_id, json.dumps(plan), now)
            await self.store.put_meta(db, "retention:last_run", json.dumps(result), now)
        return result

    async def _automatic_batch(
        self, kind: Kind, before: float, now: float, *, legacy: bool = False
    ) -> int:
        table, safe = TABLES[kind], SAFE[kind]
        if legacy and kind == "notifications":
            safe += " AND status='sent'"
        if legacy and kind == "alerts":
            safe += " AND state='event' AND id NOT IN (SELECT alert_id FROM alert_acknowledgements)"
        field = "created_at" if kind in {"logs", "audits"} else "updated_at"
        async with self.store.transaction() as db:
            if kind == "alerts":
                found = await rows(
                    db,
                    f"SELECT id,generation FROM alerts WHERE updated_at<? AND ({safe}) LIMIT 500",
                    (before,),
                )
                for item in found:
                    await self.store.put_meta(
                        db, "history:hidden:" + item["id"], str(item["generation"]), now
                    )
                    await db.execute("UPDATE alerts SET title='' WHERE id=?", (item["id"],))
                return len(found)
            cursor = await db.execute(
                f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {field}<? AND ({safe}) LIMIT 500)",
                (before,),
            )
            deleted = max(0, cursor.rowcount)
            if kind == "logs" and deleted:
                await self._advance_coverage(db, before, now)
            return deleted

    async def _advance_coverage(self, db: aiosqlite.Connection, before: float, now: float) -> None:
        previous = await row(db, "SELECT value FROM metadata WHERE id='log_coverage_start'")
        cutoff = max(float(previous["value"]) if previous else 0, before)
        await self.store.put_meta(db, "log_coverage_start", str(int(cutoff)), now)

    async def prune(self, config: RetentionConfig, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        deleted: dict[str, int] = {}
        backlog = False
        if config.enabled:
            policies: tuple[tuple[Kind, int], ...] = (
                ("logs", config.logs_days),
                ("notifications", config.history_days),
                ("alerts", config.history_days),
                ("tasks", config.tasks_days),
                ("audits", config.audits_days),
            )
            for kind, days in policies:
                # Separate short transactions, bounded wall-clock work. A backlog is retried soon.
                deadline = time.monotonic() + 3
                while True:
                    count = await self._automatic_batch(
                        kind, now - days * 86400, now, legacy=not config.confirmed
                    )
                    deleted[kind] = deleted.get(kind, 0) + count
                    if count < 500:
                        break
                    if time.monotonic() >= deadline:
                        backlog = True
                        break
            async with self.store.transaction() as db:
                await db.execute(
                    "DELETE FROM probes WHERE id IN (SELECT id FROM probes WHERE created_at<? LIMIT 1000)",
                    (now - config.logs_days * 86400,),
                )
                # Error dedupe has its own lifecycle, never shorter than seven days (lookback <= one hour).
                await db.execute(
                    "DELETE FROM error_seen WHERE id IN (SELECT id FROM error_seen WHERE updated_at<? LIMIT 1000)",
                    (now - max(7, config.logs_days) * 86400,),
                )
                await db.execute(
                    "DELETE FROM error_buckets WHERE id IN (SELECT id FROM error_buckets WHERE pending_count=0 AND updated_at<? LIMIT 1000)",
                    (now - max(7, config.logs_days) * 86400,),
                )
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE query_jobs SET status='cancelled',error='任务已超过7天有效期',updated_at=? WHERE status IN ('queued','running','waiting','paused') AND created_at<?",
                (now, now - 7 * 86400),
            )
            await db.execute(
                "DELETE FROM receipts WHERE id IN (SELECT id FROM receipts WHERE expires_at<? LIMIT 1000)",
                (now,),
            )
            await db.execute(
                "DELETE FROM metadata WHERE id IN (SELECT id FROM metadata WHERE id LIKE 'retention:preview:%' AND updated_at<? LIMIT 1000)",
                (now - 86400,),
            )
            result = {
                "deleted": deleted,
                "finished_at": now,
                "automatic": True,
                "enabled": config.enabled,
                "backlog": backlog,
            }
            await self.store.put_meta(db, "retention:last_run", json.dumps(result), now)
        return result
