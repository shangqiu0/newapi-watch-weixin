"""Official WeCom long-connection SDK adapter; no per-member authorization."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import uuid4

from aibot import WSClient, WSClientOptions
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .audit import Source
from .config import Settings
from .models import CommandResult
from .notifications import image_payload, truncate_utf8
from .store import Store

logger = logging.getLogger(__name__)


class Text(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class Sender(BaseModel):
    userid: str = Field(default="", max_length=256)


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    msgid: str = Field(min_length=1, max_length=256)
    text: Text
    sender: Sender = Field(default_factory=Sender, alias="from")
    chatid: str = Field(default="", max_length=256)
    chattype: str = Field(default="single", max_length=32)


class SDKLogger:
    """Do not allow the SDK's raw frames, URLs or credentials into application logs."""

    def debug(self, message: str, *args: Any) -> None:
        return None

    def info(self, message: str, *args: Any) -> None:
        logger.debug("wecom_sdk_event")

    def warn(self, message: str, *args: Any) -> None:
        logger.warning("wecom_sdk_warning")

    def error(self, message: str, *args: Any) -> None:
        logger.error("wecom_sdk_error (details suppressed)")


class Executor(Protocol):
    async def execute(
        self,
        text: str,
        *,
        actor: str = "",
        source: Source = "local",
        request_id: str = "",
        conversation_id: str = "",
    ) -> CommandResult: ...


class BotGateway:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        commands: Executor | None,
        *,
        namespace: str = "bot:",
    ) -> None:
        self.settings, self.store, self.commands = settings, store, commands
        self.namespace = namespace
        self.client: Any = None
        self.active = 0
        self.authenticated = False
        self.tasks: set[asyncio.Task[Any]] = set()

    async def handle(
        self, frame: dict[str, Any], *, on_start: Callable[[], Awaitable[None]] | None = None
    ) -> CommandResult | None:
        try:
            message = Message.model_validate(frame.get("body"))
        except ValidationError:
            return None
        if self.commands is None:
            return None
        now = time.time()
        key = self.namespace + message.msgid
        if not await self.store.receipt(key, now + self.settings.command_timeout + 30, now):
            return None
        try:
            if on_start:
                await on_start()
            async with asyncio.timeout(self.settings.command_timeout):
                return await self.commands.execute(
                    message.text.content,
                    actor=message.sender.userid,
                    source="wecom",
                    request_id=message.msgid,
                    conversation_id=message.chattype
                    + ":"
                    + (message.chatid or message.sender.userid),
                )
        except asyncio.CancelledError:
            await self.store.forget_receipt(key)
            raise
        except TimeoutError:
            return CommandResult(text="查询超时，请稍后重试。监控与告警仍在运行。")
        except Exception:
            # Callback isolation boundary: do not leak raw SDK/upstream errors.
            await self.store.forget_receipt(key)
            logger.error("bot_command_failed")
            return CommandResult(text="查询失败，请检查服务健康后重试。")

    async def _on_text(self, frame: dict[str, Any]) -> None:
        if self.active >= self.settings.bot_concurrency:
            await self.client.reply_stream(frame, uuid4().hex, "查询繁忙，请稍后重试。", True)
            return
        self.active += 1
        task = asyncio.current_task()
        if task:
            self.tasks.add(task)
        stream_id = uuid4().hex

        async def progress() -> None:
            await self.client.reply_stream(
                frame, stream_id, "我正在查询这个平台的数据，稍等一下。", False
            )

        try:
            result = await self.handle(frame, on_start=progress)
            if result is None:
                return
            images = (
                [{"msgtype": "image", "image": image_payload(result.image)}]
                if result.image
                else None
            )
            await self.client.reply_stream(
                frame, stream_id, truncate_utf8(result.text, 14000), True, images
            )
            body = frame.get("body", {})
            if isinstance(body, dict) and isinstance(body.get("msgid"), str):
                await self.store.extend_receipt(self.namespace + body["msgid"], time.time() + 86400)
        except Exception:
            body = frame.get("body", {})
            if isinstance(body, dict) and isinstance(body.get("msgid"), str):
                await self.store.forget_receipt(self.namespace + body["msgid"])
            logger.error("bot_reply_failed")
        finally:
            self.active -= 1
            if task:
                self.tasks.discard(task)

    def _authenticated(self) -> None:
        self.authenticated = True
        logger.info("wecom_authenticated")

    def _disconnected(self, *args: Any) -> None:
        self.authenticated = False
        logger.warning("wecom_disconnected")

    def _error(self, *args: Any) -> None:
        logger.error("wecom_error")

    async def run(self) -> None:
        self.client = WSClient(
            WSClientOptions(
                bot_id=self.settings.bot_id,
                secret=self.settings.bot_secret.get_secret_value(),
                max_reconnect_attempts=-1,
                logger=SDKLogger(),
            )
        )
        self.client.on("message.text", self._on_text)
        self.client.on("authenticated", self._authenticated)
        self.client.on("disconnected", self._disconnected)
        self.client.on("error", self._error)
        try:
            await self.client.connect()
            await asyncio.Event().wait()
        finally:
            self.authenticated = False
            self.client.disconnect()
            if self.tasks:
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
