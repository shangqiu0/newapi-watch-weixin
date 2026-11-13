"""Platform-scoped lifecycle. Separate SQLite files and fixed bot contexts enforce isolation."""

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .assistant import Assistant
from .bot import BotGateway
from .config import Settings
from .control_store import ControlError, ControlStore
from .health import Health
from .runtime import Runtime
from .targets import Targets

logger = logging.getLogger(__name__)


@dataclass
class Entry:
    runtime: Runtime
    assistant: Assistant
    enabled: bool
    bots: list[BotGateway] = field(default_factory=list)
    bot_tasks: list[asyncio.Task[None]] = field(default_factory=list)


class Fleet:
    def __init__(self, control: ControlStore, *, demo: bool = False) -> None:
        self.control, self.demo = control, demo
        self.entries: dict[str, Entry] = {}
        self.failures: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.start_tasks = True
        self.llm_http = httpx.AsyncClient(timeout=25, follow_redirects=False)
        self.notify_http = httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def open(self, *, start_tasks: bool = True) -> None:
        self.start_tasks = start_tasks
        for platform in await self.control.platforms():
            try:
                await self.reload(platform["id"])
            except Exception as exc:
                self.failures[platform["id"]] = type(exc).__name__

    async def reload(self, platform_id: str, *, reset_model_diagnostic: bool = False) -> None:
        async with self.locks.setdefault(platform_id, asyncio.Lock()):
            config = await self.control.platform_config(platform_id)
            await self._stop(platform_id)
            stable_key = hmac.new(
                self.control.key_path.read_bytes(), platform_id.encode(), hashlib.sha256
            ).hexdigest()
            defaults = Settings.model_construct().model_dump()
            settings = Settings.model_validate(
                {
                    **defaults,
                    "demo": self.demo,
                    "database": self.control.directory
                    / "platforms"
                    / platform_id
                    / "watch.sqlite3",
                    "api_key": stable_key,
                    "newapi_url": config.newapi_url,
                    "newapi_token": config.newapi_token,
                    "newapi_user_id": config.newapi_user_id,
                    "timezone": config.timezone,
                    "poll_seconds": config.poll_seconds,
                    "log_seconds": config.log_seconds,
                    "probe_enabled": False,
                    "report_enabled": False,
                }
            )
            runtime = Runtime(settings)
            try:
                runtime.notifications.webhook = Targets(
                    self.control, platform_id, config.name, self.notify_http, dry_run=self.demo
                )
                await runtime.open(
                    start_tasks=self.start_tasks and config.enabled, monitoring=config.monitoring
                )
                if reset_model_diagnostic:
                    await runtime.store.set_meta("llm_diagnostic", "null")
                runtime.notifications.webhook = Targets(
                    self.control, platform_id, config.name, self.notify_http, dry_run=self.demo
                )
                assistant = Assistant(
                    platform_id, config.name, runtime.commands, config.llm, self.llm_http
                )
                entry = Entry(runtime, assistant, config.enabled)
                self.entries[platform_id] = entry
                bindings = await self.control.bindings(platform_id)
                configured_bots = sum(b["enabled"] and b["kind"] == "bot" for b in bindings)

                def bot_snapshot() -> dict[str, int | str]:
                    authenticated = sum(bot.authenticated for bot in entry.bots)
                    status = (
                        "unconfigured"
                        if not configured_bots
                        else "disabled"
                        if not entry.enabled
                        else "demo_disabled"
                        if self.demo
                        else "authenticated"
                        if authenticated == configured_bots
                        else "disconnected"
                    )
                    return {
                        "status": status,
                        "configured": configured_bots,
                        "authenticated": authenticated,
                    }

                runtime.commands.health = Health(
                    settings,
                    runtime.store,
                    notifications_configured=lambda: any(
                        b["enabled"] and b["kind"] == "webhook" for b in bindings
                    ),
                    bot_snapshot=bot_snapshot,
                )
                if runtime.policies:
                    runtime.commands.health.policy_snapshot = runtime.policies.snapshot
                if config.enabled and self.start_tasks and not self.demo:
                    for binding in bindings:
                        if not binding["enabled"] or binding["kind"] != "bot":
                            continue
                        private = (
                            await self.control.binding(platform_id, binding["id"], private=True)
                        )["private"]
                        bot_settings = Settings.model_validate(
                            {
                                **settings.model_dump(),
                                "bot_id": private["bot_id"],
                                "bot_secret": private["bot_secret"],
                            }
                        )
                        bot_ai = Assistant(
                            platform_id,
                            config.name,
                            runtime.commands,
                            config.llm,
                            self.llm_http,
                            binding_id=binding["id"],
                        )
                        bot = BotGateway(
                            bot_settings,
                            runtime.store,
                            bot_ai,
                            namespace="binding:" + binding["id"] + ":",
                        )
                        entry.bots.append(bot)
                        entry.bot_tasks.append(
                            asyncio.create_task(
                                runtime.periodic("bot-" + binding["id"], 5, bot.run)
                            )
                        )
                self.failures.pop(platform_id, None)
            except BaseException:
                await runtime.close()
                self.entries.pop(platform_id, None)
                self.failures[platform_id] = "启动失败，请检查平台配置"
                raise

    def entry(self, platform_id: str) -> Entry:
        if platform_id not in self.entries:
            if platform_id in self.failures:
                raise ControlError(503, "平台未能启动，请检查配置后重新保存")
            raise ControlError(404, "平台不存在")
        return self.entries[platform_id]

    async def delete_platform(
        self, platform_id: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        async with self.locks.setdefault(platform_id, asyncio.Lock()):
            # Archive first so a crash/restart cannot start this platform again.
            result = await self.control.archive_platform(platform_id, revision=revision)
            await self._stop(platform_id)
            self.failures.pop(platform_id, None)
            logger.info(
                "platform_deleted id=%s duplicate=%s history_retained=true",
                platform_id,
                result["duplicate"],
            )
            return result

    async def _stop(self, platform_id: str) -> None:
        entry = self.entries.pop(platform_id, None)
        if entry:
            for task in entry.bot_tasks:
                task.cancel()
            await asyncio.gather(*entry.bot_tasks, return_exceptions=True)
            await entry.runtime.close()

    async def close(self) -> None:
        for platform_id in list(self.entries):
            await self._stop(platform_id)
        await self.llm_http.aclose()
        await self.notify_http.aclose()
