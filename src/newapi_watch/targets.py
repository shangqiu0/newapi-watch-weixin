"""Per-target delivery receipts prevent successful Webhooks being resent on partial failure."""

import hashlib
import time

import httpx

from .control_store import ControlStore
from .notifications import GroupWebhookNotifier
from .persistence import row


class Targets:
    def __init__(
        self,
        control: ControlStore,
        platform_id: str,
        name: str,
        http: httpx.AsyncClient,
        *,
        dry_run: bool = False,
    ) -> None:
        self.control, self.platform_id, self.name, self.http = control, platform_id, name, http
        self.dry_run = dry_run

    async def ready(self) -> bool:
        return any(
            item["kind"] == "webhook" and item["enabled"]
            for item in await self.control.bindings(self.platform_id)
        )

    async def send_for(
        self,
        notification_id: int,
        content: str,
        image: bytes | None = None,
        *,
        kind: str = "channel",
        mention_all: bool = False,
        binding_ids: list[str] | None = None,
    ) -> None:
        targets = [
            item
            for item in await self.control.bindings(self.platform_id)
            if item["kind"] == "webhook" and item["enabled"]
        ]
        if not targets:
            raise RuntimeError("该平台尚未绑定启用的群Webhook")
        targets = [t for t in targets if kind in t.get("events", [kind])]
        if binding_ids:
            targets = [t for t in targets if t["id"] in binding_ids]
        if not targets:
            raise RuntimeError("该策略没有启用且订阅此事件的Webhook目标")
        failures = 0
        for target in targets:
            key = hashlib.sha256(
                f"{self.platform_id}:{notification_id}:{target['id']}".encode()
            ).hexdigest()
            legacy_key = hashlib.sha256(
                f"{self.platform_id}:{notification_id}:{target['id']}:{target['revision']}".encode()
            ).hexdigest()
            async with self.control.transaction() as db:
                prior = await row(
                    db, "SELECT id FROM target_deliveries WHERE id IN (?,?)", (key, legacy_key)
                )
            if prior:
                continue
            binding = await self.control.binding(self.platform_id, target["id"], private=True)
            try:
                if not self.dry_run:
                    notifier = GroupWebhookNotifier(
                        binding["private"]["webhook_url"],
                        self.http,
                        message_format=binding.get("message_format", "markdown"),
                        mention_all=mention_all or binding.get("mention_all", False),
                    )
                    await notifier.send(f"平台：{self.name}\n{content}")
                    if image is not None:
                        await notifier.send("", image)
            except (RuntimeError, httpx.HTTPError, ValueError):
                failures += 1
            else:
                now = time.time()
                async with self.control.transaction() as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO target_deliveries(id,platform_id,binding_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                        (key, self.platform_id, target["id"], now, now),
                    )
        if failures:
            raise RuntimeError("部分平台通知目标投递失败")

    async def send(self, content: str, image: bytes | None = None) -> None:
        raise RuntimeError("多目标通知必须带持久化通知ID")
