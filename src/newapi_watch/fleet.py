"""Platform-scoped lifecycle. Separate SQLite files and fixed bot contexts enforce isolation."""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .assistant import Assistant
from .bot import BotGateway
from .config import Settings
from .control_store import ControlError, ControlStore
from .health import Health
from .persistence import rows
from .runtime import Runtime
from .targets import Targets
from .upstream_accounts import UpstreamAccounts

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
                    "currency": config.currency,
                    "retention": config.retention,
                    "poll_seconds": config.poll_seconds,
                    "log_seconds": config.log_seconds,
                    "probe_enabled": False,
                    "report_enabled": False,
                }
            )
            runtime = Runtime(settings)

            async def prune_operation_records() -> None:
                await self.control.prune_operations(
                    platform_id, time.time() - config.retention.audits_days * 86400
                )

            runtime.retention_callback = prune_operation_records
            runtime.upstream = UpstreamAccounts(runtime, config.upstream_accounts)
            runtime.commands.upstream = runtime.upstream
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
                if config.enabled and self.start_tasks and not self.demo:
                    runtime.tasks.append(
                        asyncio.create_task(
                            runtime.periodic(
                                "query-delivery", 3, lambda: self.deliver_queries(platform_id)
                            )
                        )
                    )

                async def send_balance(binding_id: str, chat_id: str, text: str) -> None:
                    bot = next(
                        (b for b in entry.bots if b.namespace == "binding:" + binding_id + ":"),
                        None,
                    )
                    if bot is None:
                        raise RuntimeError("余额提醒机器人未连接")
                    await bot.send_task_result(chat_id, text)

                runtime.notifications.webhook.bot_sender = send_balance
                if config.enabled and self.start_tasks:
                    runtime.tasks.append(
                        asyncio.create_task(
                            runtime.periodic("upstream-balances", 1, runtime.upstream.tick)
                        )
                    )
                if self.start_tasks and not config.enabled:
                    # Data housekeeping must not start remote monitoring for disabled platforms.
                    runtime.tasks.append(
                        asyncio.create_task(
                            runtime.periodic("retention", 60, runtime.prune), name="retention"
                        )
                    )
                self.failures.pop(platform_id, None)
            except BaseException:
                await runtime.close()
                self.entries.pop(platform_id, None)
                self.failures[platform_id] = "启动失败，请检查平台配置"
                raise

    async def deliver_queries(self, platform_id: str) -> None:
        entry = self.entries.get(platform_id)
        if not entry or not entry.enabled:
            return
        async with entry.runtime.store.transaction() as db:
            completed = await rows(
                db,
                "SELECT id,context_json,result FROM query_jobs WHERE status='complete' AND notified_at=0 AND next_at<=? "
                "AND json_extract(context_json,'$.binding_id') IS NOT NULL AND json_extract(context_json,'$.binding_id')!='web' "
                "AND COALESCE(json_extract(context_json,'$.delivery_stopped'),0)=0 AND json_extract(context_json,'$._scope')=? ORDER BY next_at LIMIT 5",
                (time.time(), entry.runtime.query_engine.jobs.scope),
            )
        for item in completed:
            context = json.loads(item["context_json"])
            if (
                context.get("delivery_stopped")
                or context.get("_scope") != entry.runtime.query_engine.jobs.scope
            ):
                continue
            binding = context.get("binding_id", "")
            if binding == "web" or not binding:
                continue
            gateway = next(
                (
                    bot
                    for bot in entry.bots
                    if bot.namespace == "binding:" + binding + ":" and bot.authenticated
                ),
                None,
            )
            if not gateway:
                async with entry.runtime.store.transaction() as db:
                    await db.execute(
                        "UPDATE query_jobs SET next_at=? WHERE id=? AND notified_at=0",
                        (time.time() + 60, item["id"]),
                    )
                continue
            try:
                current = await self.control.binding(platform_id, binding)
            except ControlError:
                continue
            if not current["enabled"]:
                continue
            conversation = context.get("conversation_id", "")
            if not conversation.startswith(("group:", "single:")):
                continue
            chatid = conversation.split(":", 1)[1]
            if not chatid:
                continue
            result = json.loads(item["result"])
            summary = result["summary"]
            text = (
                f"查询任务已完成 · {item['id']}\n消费记录：{summary['success_records']:,}\n"
                f"错误记录：{summary['error_records']:,}\n消耗：{entry.runtime.settings.currency.display(summary['quota'])}\n"
                f"已读取：{result['records_read']:,} 条\n可回复“任务 {item['id']}”查看结果。"
            )
            if result.get("unknown_status_records"):
                text += f"\n注意：{result['unknown_status_records']} 条错误状态码未知，当前匹配数不代表全部。"
            try:
                await gateway.send_task_result(chatid, text)
            except Exception:
                # SDK boundary: failed delivery must not become a channel-health alert.
                failures = int(context.get("delivery_failures", 0)) + 1
                context["delivery_failures"] = failures
                if failures >= 3:
                    context["delivery_stopped"] = True
                async with entry.runtime.store.transaction() as db:
                    await db.execute(
                        "UPDATE query_jobs SET context_json=?,next_at=? WHERE id=? AND notified_at=0",
                        (
                            json.dumps(context),
                            time.time() + (86400 if failures >= 3 else 60 * failures),
                            item["id"],
                        ),
                    )
                logger.warning("query_delivery_unconfirmed task=%s", item["id"])
                return
            async with entry.runtime.store.transaction() as db:
                await db.execute(
                    "UPDATE query_jobs SET notified_at=? WHERE id=? AND status='complete' AND notified_at=0",
                    (time.time(), item["id"]),
                )
            return

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
