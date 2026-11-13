"""Operational freshness and component state, without credentials or user data."""

import time
from collections.abc import Callable
from typing import Any

from .config import Settings
from .persistence import rows
from .store import Store


class Health:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        bot_authenticated: Callable[[], bool] | None = None,
        *,
        notifications_configured: Callable[[], bool] | None = None,
        bot_snapshot: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.settings, self.store, self.bot_authenticated = settings, store, bot_authenticated
        self.notifications_configured = notifications_configured
        self.bot_snapshot = bot_snapshot
        self.policy_snapshot: Callable[[], Any] | None = None

    @staticmethod
    def fresh(value: str | None, now: float, threshold: float) -> bool:
        return value is not None and -5 <= now - float(value) <= threshold

    async def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        async with self.store.transaction() as db:
            metadata = await rows(
                db,
                """SELECT id,value FROM metadata
                WHERE id IN ('channels_last_ok','logs_last_ok') OR id LIKE 'job:%'""",
            )
            failed_jobs = await rows(
                db, "SELECT key FROM alerts WHERE state='firing' AND key LIKE 'service:job:%'"
            )
        values = {item["id"]: str(item["value"]) for item in metadata}
        channels, logs = values.get("channels_last_ok"), values.get("logs_last_ok")
        channels_fresh = self.fresh(channels, now, max(120, self.settings.poll_seconds * 3))
        logs_fresh = self.fresh(logs, now, max(300, self.settings.log_seconds * 3))
        queue = await self.store.queue_counts()
        bot_status = "unconfigured"
        if self.settings.bot_id:
            bot_status = (
                "demo_disabled"
                if self.settings.demo
                else (
                    "authenticated"
                    if self.bot_authenticated and self.bot_authenticated()
                    else "disconnected"
                )
            )
        bot = self.bot_snapshot() if self.bot_snapshot else {"status": bot_status}
        bot_status = str(bot["status"])
        reasons = []
        if failed_jobs:
            reasons.append("background_jobs_failed")
        policy_state = await self.policy_snapshot() if self.policy_snapshot else None
        if not channels_fresh and policy_state is None:
            reasons.append("channel_data_stale")
        if not logs_fresh and policy_state is None:
            reasons.append("log_data_stale")
        if policy_state and policy_state["summary"]["unknown"]:
            reasons.append("monitor_checks_unknown")
        if queue.get("dead", 0):
            reasons.append("dead_notifications")
        if bot_status == "disconnected":
            reasons.append("bot_disconnected")
        configured = (
            self.notifications_configured()
            if self.notifications_configured
            else bool(self.settings.group_webhook.get_secret_value())
        )
        if not configured and not self.settings.demo:
            reasons.append("notifications_unconfigured")
        return {
            "status": "degraded" if reasons else "ok",
            "reasons": reasons,
            "channels_last_ok": channels,
            "logs_last_ok": logs,
            "channels_fresh": channels_fresh,
            "logs_fresh": logs_fresh,
            "queue": queue,
            "notifications_configured": configured,
            "permissions": "shared",
            "demo": self.settings.demo,
            "bot": bot,
            "jobs": {key[4:]: value for key, value in values.items() if key.startswith("job:")},
            "failed_jobs": [item["key"].removeprefix("service:job:") for item in failed_jobs],
        }
