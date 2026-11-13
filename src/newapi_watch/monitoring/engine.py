"""Platform-scoped scheduler: checks -> conditions -> durable notifications, no raw log ingestion."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..client import UpstreamError
from ..redaction import redact_text
from .checks import StateChecks, timestamp
from .repository import PolicyRepository

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .service import MonitoringService


class PolicyEngine:
    def __init__(self, service: "MonitoringService") -> None:
        self.service = service
        self.runtime = service.runtime
        self.repo = PolicyRepository(self.runtime.store)
        self.checks = StateChecks(self.runtime, self.repo)
        self.lock = asyncio.Lock()
        self.inflight: dict[str, asyncio.Task[None]] = {}
        self.balance_slots = asyncio.Semaphore(4)

    def definitions(self) -> list[dict[str, Any]]:
        c = self.service.config
        return [
            {
                "id": "channels",
                "name": "渠道状态",
                "kind": "channel",
                "enabled": c.channels.enabled,
                "interval": c.channels.poll_seconds,
            },
            {
                "id": "errors",
                "name": "请求错误事件",
                "kind": "request_error",
                "enabled": c.errors.enabled,
                "interval": c.errors.poll_seconds,
            },
            *[
                {
                    "id": f"balance:{r.user_id}",
                    "name": r.name or f"用户 #{r.user_id} 余额",
                    "kind": "balance",
                    "enabled": r.enabled,
                    "interval": r.poll_seconds or c.balance_poll_seconds,
                }
                for r in c.balances
            ],
            {
                "id": "daily",
                "name": "日报调度",
                "kind": "daily",
                "enabled": c.daily.enabled,
                "interval": 60,
            },
            {
                "id": "weekly",
                "name": "周报调度",
                "kind": "weekly",
                "enabled": c.weekly.enabled,
                "interval": 60,
            },
        ]

    async def tick(
        self, *, now: float | None = None, force: bool = False, wait: bool = True
    ) -> None:
        now = time.time() if now is None else now
        tasks: list[asyncio.Task[None]] = []
        async with self.lock:
            c = self.service.config
            for definition in self.definitions():
                if not definition["enabled"]:
                    continue
                key = definition["id"]
                pending = self.inflight.get(key)
                if pending and not pending.done():
                    tasks.append(pending)
                    continue
                prior = await self.repo.check(key)
                if not force and prior and prior["next_at"] > now:
                    continue
                if key == "channels":
                    callback: Callable[[], Awaitable[dict[str, Any]]] = partial(
                        self.checks.channels, c.channels, now
                    )
                elif key == "errors":
                    callback = partial(self.errors, now)
                elif key.startswith("balance:"):
                    rule = next(r for r in c.balances if f"balance:{r.user_id}" == key)
                    callback = partial(self.checks.balance, rule, now)
                else:
                    callback = partial(self.report, key, now)
                task = asyncio.create_task(
                    self.dispatch(key, definition["interval"], callback, now), name="check-" + key
                )
                task.add_done_callback(self.completed)
                self.inflight[key] = task
                tasks.append(task)
            # Flush previously confirmed events independently of source availability.
            await self.service.errors([], now=now)
        if wait and tasks:
            await asyncio.gather(*tasks)

    @staticmethod
    def completed(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "monitor_task_failed task=%s type=%s", task.get_name(), type(error).__name__
            )

    async def dispatch(
        self,
        key: str,
        interval: float,
        callback: Callable[[], Awaitable[dict[str, Any]]],
        now: float,
    ) -> None:
        if key.startswith("balance:"):
            async with self.balance_slots:
                await self.run(key, interval, callback, now)
        else:
            await self.run(key, interval, callback, now)

    async def cancel(self) -> None:
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()

    async def run(
        self,
        key: str,
        interval: float,
        callback: Callable[[], Awaitable[dict[str, Any]]],
        now: float,
    ) -> None:
        error = ""
        retry_after = 0
        try:
            async with asyncio.timeout(45):
                detail = await callback()
        except (UpstreamError, TimeoutError) as exc:
            retry_after = int(getattr(exc, "retry_after", 0))
            error = redact_text(str(exc) or "本次检查超过45秒预算", self.runtime.settings)
            detail = {}
        except Exception as exc:
            logger.error("monitor_check_failed key=%s type=%s", key, type(exc).__name__)
            error = "检查内部执行失败，请查看服务日志"
            detail = {}
        if error:
            prefix = "policy:channel:" if key == "channels" else f"policy:{key}"
            async with self.runtime.store.transaction() as db:
                await db.execute(
                    "UPDATE monitor_states SET streak=0 WHERE id LIKE ?", (prefix + "%",)
                )
            if key.startswith("balance:"):
                await self.runtime.store.set_meta("balance_error:" + key.split(":")[1], error)
        await self.repo.finish(
            key, now, interval, error=error, detail=detail, not_before=now + retry_after
        )
        policy = self.service.config.source
        if policy.enabled:
            text = (
                f"监控检查失败\n检查：{key}\n原因：{error}\n业务状态未知，保留上次成功结果"
                if error
                else f"监控检查恢复\n检查：{key}"
            ) + f"\n时间：{timestamp(now)}"
            await self.repo.condition(
                f"policy:source:{key}", bool(error), text, policy, "service", now
            )

    async def errors(self, now: float) -> dict[str, Any]:
        c = self.service.config.errors
        cursor = await self.runtime.store.get_meta("policy:error_cursor")
        if cursor is None and not c.initial_lookback_seconds:
            await self.runtime.store.set_meta("policy:error_floor", str(int(now)))
            await self.runtime.store.set_meta("policy:error_cursor", str(int(now)))
            return {"baseline": int(now), "records": 0, "note": "从当前时刻监控，不追发旧错误"}
        cursor_at = int(cursor) if cursor else max(0, int(now) - c.initial_lookback_seconds)
        end = min(int(now), cursor_at + c.window_seconds)
        start = max(0, cursor_at - c.overlap_seconds)
        records = await self.runtime.client.logs(start, end, log_type=5)
        floor = int(await self.runtime.store.get_meta("policy:error_floor") or str(start))
        records = [r for r in records if max(start, floor) <= r.created_at <= end]
        await self.service.errors(records, now=now)
        await self.runtime.store.set_meta("policy:error_cursor", str(end))
        await self.runtime.store.set_meta("logs_last_ok", str(now))
        return {
            "from_timestamp": start,
            "through_timestamp": end,
            "lag_seconds": max(0, int(now) - end),
            "records": len(records),
            "complete": True,
        }

    async def report(self, kind: str, now: float) -> dict[str, Any]:
        await self.service.reports(
            datetime.fromtimestamp(now, ZoneInfo(self.runtime.settings.timezone)), only=kind
        )
        return {"note": "独立报告调度；仅到时查询上游"}

    async def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        definitions = self.definitions()
        result = []
        for definition in definitions:
            saved = await self.repo.check(definition["id"])
            item = {**definition, **(saved or {}), "enabled": definition["enabled"]}
            item["status"] = (
                "disabled" if not definition["enabled"] else item.get("status", "pending")
            )
            item["stale"] = bool(
                definition["enabled"]
                and saved
                and now > saved["next_at"] + max(60, definition["interval"])
            )
            item["detail"] = json.loads(saved["detail"]) if saved else {}
            pending = self.inflight.get(definition["id"])
            item["checking"] = bool(pending and not pending.done())
            result.append(item)
        states = await self.repo.states()
        enabled = {d["id"] for d in definitions if d["enabled"]}

        def belongs(key: str) -> bool:
            if key.startswith("policy:channel:"):
                parts = key.split(":")
                rule = self.service.config.channels
                return (
                    "channels" in enabled
                    and (not rule.channel_ids or int(parts[2]) in rule.channel_ids)
                    and (parts[-1] != "missing" or rule.notify_missing)
                )
            if key.startswith("policy:source:"):
                return (
                    self.service.config.source.enabled
                    and key.removeprefix("policy:source:") in enabled
                )
            return key.removeprefix("policy:") in enabled

        active = [s for s in states if s["is_active"] and belongs(s["id"])]
        queue = await self.runtime.store.queue_counts()
        return {
            "checks": result,
            "summary": {
                "enabled": len(enabled),
                "total": len(definitions),
                "unknown": sum(c["status"] == "unknown" or c["stale"] for c in result),
                "active_incidents": len(active),
                "pending_notifications": queue.get("pending", 0) + queue.get("sending", 0),
                "failed_notifications": queue.get("dead", 0),
                "historical_notifications": queue.get("historical", 0),
            },
            "incidents": [{**s, "evidence": json.loads(s["evidence"])} for s in active],
            "queue": queue,
            "mode": "policy_checks",
            "business_logs_persisted": False,
            "request_control": self.runtime.client.throttle.snapshot(),
        }
