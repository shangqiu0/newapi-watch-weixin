"""Authenticated event helpers and rate-limited, at-least-once WeCom delivery."""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Protocol

import httpx

from .config import Settings
from .store import Store

logger = logging.getLogger(__name__)


def verify_signature(
    body: bytes, signature: str, timestamp: str, secret: str, now: float, max_age: int
) -> bool:
    try:
        fresh = abs(now - int(timestamp)) <= max_age
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return fresh and len(signature) == 64 and hmac.compare_digest(expected, signature)


def truncate_utf8(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: limit - 24].decode("utf-8", errors="ignore") + "\n…结果已截断"


def image_payload(image: bytes) -> dict[str, str]:
    if len(image) > 2 * 1024 * 1024:
        raise ValueError("图像超过企微2MB限制")
    # MD5 is mandated by the WeCom image protocol, never used for authentication.
    return {
        "base64": base64.b64encode(image).decode("ascii"),
        "md5": hashlib.md5(image, usedforsecurity=False).hexdigest(),
    }


class Notifier(Protocol):
    async def send(self, content: str, image: bytes | None = None) -> None: ...


class GroupWebhookNotifier:
    def __init__(
        self,
        url: str,
        http: httpx.AsyncClient,
        *,
        message_format: str = "markdown",
        mention_all: bool = False,
    ) -> None:
        self.url = url
        self.http = http
        self.message_format, self.mention_all = message_format, mention_all

    async def send(self, content: str, image: bytes | None = None) -> None:
        if image is not None:
            await self._post({"msgtype": "image", "image": image_payload(image)})
        elif self.message_format == "text" or self.mention_all:
            await self._post(
                {
                    "msgtype": "text",
                    "text": {
                        "content": truncate_utf8(content, 1900),
                        "mentioned_list": ["@all"] if self.mention_all else [],
                    },
                }
            )
        else:
            await self._post(
                {"msgtype": "markdown", "markdown": {"content": truncate_utf8(content, 3800)}}
            )

    async def _post(self, payload: dict[str, object]) -> None:
        try:
            response = await self.http.post(
                self.url, json=payload, timeout=15, follow_redirects=False
            )
        except httpx.TransportError:
            raise RuntimeError("企微通知连接失败") from None
        if response.status_code != 200:
            raise RuntimeError(f"企微 HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("企微返回无效 JSON") from None
        if not isinstance(data, dict) or data.get("errcode") != 0:
            raise RuntimeError("企微通知被拒绝，请核查机器人和限额")


class NotificationWorker:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        webhook: Notifier | None,
        http: httpx.AsyncClient | None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.webhook = webhook
        self.next_send = 0.0

    async def process_one(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if now < self.next_send or (self.webhook is None and not self.settings.demo):
            return False
        last_attempt = await self.store.get_meta("notification_last_attempt")
        if last_attempt and now < float(last_attempt) + self.settings.notification_interval:
            return False
        ready = getattr(self.webhook, "ready", None)
        if callable(ready) and not await ready():
            return False
        item = await self.store.claim_notification(now)
        if item is None:
            return False
        attempts = int(item["attempts"]) + 1
        try:
            if self.webhook:
                scoped_send = getattr(self.webhook, "send_for", None)
                if callable(scoped_send):
                    routing = (
                        {"binding_ids": json.loads(item["binding_ids"])}
                        if item.get("binding_ids", "[]") != "[]"
                        else {}
                    )
                    await scoped_send(
                        item["id"],
                        item["content"],
                        item["image"],
                        kind=item.get("kind", "channel"),
                        mention_all=bool(item.get("mention_all", False)),
                        **routing,
                    )
                else:
                    await self.webhook.send(item["content"], item["image"])
            else:
                logger.info("demo_notification id=%s (not sent to WeCom)", item["id"])
        except (RuntimeError, ValueError, httpx.HTTPError):
            status = "dead" if attempts >= self.settings.notification_max_attempts else "pending"
            retry_at = now + min(
                3600, self.settings.notification_retry_seconds * 2 ** (attempts - 1)
            )
            await self.store.finish_notification(
                item["id"],
                status,
                attempts,
                retry_at,
                "通知失败，请检查连接、限额和机器人配置",
                now,
            )
        else:
            await self.store.finish_notification(item["id"], "sent", attempts, now, "", now)
        self.next_send = now + self.settings.notification_interval
        return True
