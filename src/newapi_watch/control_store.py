"""Central registry; monitor data stays in independent per-platform SQLite files."""

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from .control_models import BindingInput, BindingPatch, PlatformInput, PlatformPatch
from .monitoring.models import MonitoringConfig
from .persistence import row, rows
from .registry_migrations import migrate_registry
from .vault import Vault, plaintext


class ControlError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ControlStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.key_path = directory / "master.key"
        self.path = directory / "control.sqlite3"
        self.db: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()
        self.mutation_locks: dict[str, asyncio.Lock] = {}
        self.vault: Vault

    async def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.vault = Vault(self.key_path, allow_create=not self.path.exists())
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA journal_mode=WAL")
        try:
            await migrate_registry(self.db)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.lock:
            if self.db is None:
                raise RuntimeError("控制库未启动")
            try:
                yield self.db
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def _platform(self, platform_id: str) -> dict[str, Any]:
        async with self.transaction() as db:
            item = await row(
                db,
                "SELECT id,name,enabled,config,revision,created_at,updated_at FROM platforms WHERE id=? AND deleted_at IS NULL",
                (platform_id,),
            )
        if item is None:
            raise ControlError(404, "平台不存在")
        return item

    def public_platform(self, item: dict[str, Any]) -> dict[str, Any]:
        config = self.vault.open(item["config"])
        result = {key: item[key] for key in ("id", "name", "revision", "created_at", "updated_at")}
        result.update(
            {
                key: config[key]
                for key in (
                    "newapi_url",
                    "newapi_user_id",
                    "timezone",
                    "poll_seconds",
                    "log_seconds",
                )
            }
        )
        llm = config["llm"]
        result.update(
            enabled=bool(item["enabled"]),
            has_newapi_token=bool(config["newapi_token"]),
            llm={
                "enabled": llm["enabled"],
                "compatibility": llm.get("compatibility", "auto"),
                "base_url": llm["base_url"],
                "model": llm["model"],
                "has_api_key": bool(llm["api_key"]),
            },
        )
        result["monitoring"] = config.get("monitoring", {})
        parsed = PlatformInput.model_validate(config)
        result["currency"] = parsed.currency.model_dump(mode="json")
        result["retention"] = parsed.retention.model_dump(mode="json")
        result["upstream_accounts"] = [a.public() for a in parsed.upstream_accounts]
        return result

    async def platforms(self) -> list[dict[str, Any]]:
        async with self.transaction() as db:
            items = await rows(
                db,
                "SELECT id,name,enabled,config,revision,created_at,updated_at FROM platforms WHERE deleted_at IS NULL ORDER BY created_at,id",
            )
        return [self.public_platform(item) for item in items]

    async def platform(self, platform_id: str) -> dict[str, Any]:
        return self.public_platform(await self._platform(platform_id))

    async def platform_config(self, platform_id: str) -> PlatformInput:
        return PlatformInput.model_validate(
            self.vault.open((await self._platform(platform_id))["config"])
        )

    async def create_platform(self, config: PlatformInput) -> dict[str, Any]:
        identifier, now = uuid4().hex, time.time()
        if any(
            a.query_binding_ids or a.notify_binding_ids or not a.access_token.get_secret_value()
            for a in config.upstream_accounts
        ):
            raise ControlError(400, "新平台请先保存后添加上游账户与通知绑定")
        if any(rule.binding_ids for _, rule in config.monitoring.routes()):
            raise ControlError(400, "新平台请先创建Webhook绑定，再选择策略通知目标")
        async with self.transaction() as db:
            count = await row(
                db, "SELECT COUNT(*) AS count FROM platforms WHERE deleted_at IS NULL"
            )
            if count and count["count"] >= 30:
                raise ControlError(409, "首版最多30个平台")
            await db.execute(
                "INSERT INTO platforms(id,name,enabled,config,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (identifier, config.name, int(config.enabled), self.vault.seal(config), now, now),
            )
        return await self.platform(identifier)

    async def patch_platform(self, identifier: str, patch: PlatformPatch) -> dict[str, Any]:
        # Serialize read/merge/validate/write, not just the final SQL transaction.
        # Different settings sections must not restore a stale copy of each other.
        async with self.mutation_locks.setdefault(identifier, asyncio.Lock()):
            return await self._patch_platform(identifier, patch)

    async def _patch_platform(self, identifier: str, patch: PlatformPatch) -> dict[str, Any]:
        previous = await self.platform_config(identifier)
        values = plaintext(previous)
        changes = plaintext(patch)
        for key, value in changes.items():
            if value is None or (key == "newapi_token" and not value):
                continue
            if key == "retention":
                # Explicitly saving this section confirms the expanded history retention scope.
                value["confirmed"] = True
            if key == "llm":
                if not value["api_key"]:
                    value["api_key"] = values["llm"]["api_key"]
            values[key] = value
        if values["newapi_url"].rstrip("/") != previous.newapi_url:
            raise ControlError(409, "更换NewAPI地址需新建平台，避免历史数据混合")
        config = PlatformInput.model_validate(values)
        await self.validate_monitoring(identifier, config.monitoring)
        await self.validate_upstream_accounts(identifier, config)
        async with self.transaction() as db:
            changed = await db.execute(
                "UPDATE platforms SET name=?,enabled=?,config=?,revision=revision+1,updated_at=? WHERE id=? AND deleted_at IS NULL",
                (
                    config.name,
                    int(config.enabled),
                    self.vault.seal(config),
                    time.time(),
                    identifier,
                ),
            )
            if changed.rowcount != 1:
                raise ControlError(404, "平台不存在或已删除")
        return await self.platform(identifier)

    async def archive_platform(
        self, identifier: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        now = time.time()
        async with self.transaction() as db:
            platform = await row(
                db, "SELECT id,revision,deleted_at FROM platforms WHERE id=?", (identifier,)
            )
            if platform is None:
                raise ControlError(404, "平台不存在")
            duplicate = platform["deleted_at"] is not None
            if not duplicate:
                if revision is not None and revision != platform["revision"]:
                    raise ControlError(409, "平台配置已更新，请刷新列表后重新确认删除")
                await db.execute(
                    "UPDATE platforms SET enabled=0,deleted_at=?,revision=revision+1,updated_at=? WHERE id=? AND deleted_at IS NULL",
                    (now, now, identifier),
                )
                await db.execute(
                    "UPDATE bindings SET enabled=0,revision=revision+1,updated_at=? WHERE platform_id=? AND enabled=1",
                    (now, identifier),
                )
        return {"id": identifier, "deleted": True, "history_retained": True, "duplicate": duplicate}

    async def validate_monitoring(self, platform_id: str, config: MonitoringConfig) -> None:
        bindings = {b["id"]: b for b in await self.bindings(platform_id)}
        for kind, rule in config.routes():
            for identifier in rule.binding_ids:
                binding = bindings.get(identifier)
                if not binding or binding["kind"] != "webhook" or kind not in binding["events"]:
                    raise ControlError(400, "策略通知目标必须是本平台且订阅对应事件的群Webhook")

    async def validate_upstream_accounts(self, platform_id: str, config: PlatformInput) -> None:
        bindings = {b["id"]: b for b in await self.bindings(platform_id)}
        ids = [a.id for a in config.upstream_accounts]
        if len(ids) != len(set(ids)):
            raise ControlError(400, "上游账户ID不能重复")
        for account in config.upstream_accounts:
            if not account.access_token.get_secret_value().strip():
                raise ControlError(400, "上游账户缺少用户访问令牌")
            for identifier in account.query_binding_ids:
                if identifier not in bindings or bindings[identifier]["kind"] != "bot":
                    raise ControlError(400, "查询机器人必须属于当前平台")
            for identifier in account.notify_binding_ids:
                target = bindings.get(identifier)
                if (
                    not target
                    or "balance" not in target["events"]
                    or (target["kind"] == "bot" and not target.get("notification_chat_id"))
                ):
                    raise ControlError(
                        400, "余额通知目标必须属于当前平台、订阅余额事件；机器人还需设置接收会话ID"
                    )

    def public_binding(self, item: dict[str, Any]) -> dict[str, Any]:
        config = self.vault.open(item["config"])
        result = {
            key: item[key]
            for key in ("id", "platform_id", "kind", "name", "revision", "created_at", "updated_at")
        }
        result.update(
            enabled=bool(item["enabled"]),
            has_secret=bool(config.get("bot_secret") or config.get("webhook_url")),
            events=config.get(
                "events", ["channel", "request_error", "balance", "daily", "weekly", "service"]
            ),
            message_format=config.get("message_format", "markdown"),
            mention_all=config.get("mention_all", False),
            notification_chat_id=config.get("notification_chat_id", ""),
        )
        return result

    async def bindings(self, platform_id: str) -> list[dict[str, Any]]:
        await self._platform(platform_id)
        async with self.transaction() as db:
            items = await rows(
                db,
                "SELECT id,platform_id,kind,name,enabled,config,revision,created_at,updated_at FROM bindings WHERE platform_id=? ORDER BY created_at,id",
                (platform_id,),
            )
        return [self.public_binding(item) for item in items]

    async def binding(
        self, platform_id: str, binding_id: str, *, private: bool = False
    ) -> dict[str, Any]:
        await self._platform(platform_id)
        async with self.transaction() as db:
            item = await row(
                db,
                "SELECT id,platform_id,kind,name,enabled,config,revision,created_at,updated_at FROM bindings WHERE id=? AND platform_id=?",
                (binding_id, platform_id),
            )
        if item is None:
            raise ControlError(404, "该平台的绑定不存在")
        return (
            {**self.public_binding(item), "private": self.vault.open(item["config"])}
            if private
            else self.public_binding(item)
        )

    async def create_binding(self, platform_id: str, config: BindingInput) -> dict[str, Any]:
        await self._platform(platform_id)
        identifier, now = uuid4().hex, time.time()
        fingerprint = (
            hashlib.sha256(config.bot_id.encode()).hexdigest() if config.kind == "bot" else ""
        )
        async with self.transaction() as db:
            if not await row(
                db, "SELECT id FROM platforms WHERE id=? AND deleted_at IS NULL", (platform_id,)
            ):
                raise ControlError(404, "平台不存在或已删除")
            count = await row(
                db, "SELECT COUNT(*) AS count FROM bindings WHERE platform_id=?", (platform_id,)
            )
            if count and count["count"] >= 20:
                raise ControlError(409, "每平台最多20个绑定")
            try:
                await db.execute(
                    """INSERT INTO bindings(id,platform_id,kind,name,enabled,config,bot_fingerprint,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        identifier,
                        platform_id,
                        config.kind,
                        config.name,
                        int(config.enabled),
                        self.vault.seal(config),
                        fingerprint,
                        now,
                        now,
                    ),
                )
            except aiosqlite.IntegrityError:
                raise ControlError(409, "该机器人已被其他活跃绑定使用") from None
        return await self.binding(platform_id, identifier)

    async def patch_binding(
        self, platform_id: str, binding_id: str, patch: BindingPatch
    ) -> dict[str, Any]:
        current = await self.binding(platform_id, binding_id, private=True)
        values = current["private"]
        for key, value in plaintext(patch).items():
            if value is not None and not (
                key in {"bot_secret", "webhook_url", "bot_id"} and not value
            ):
                values[key] = value
        config = BindingInput.model_validate(values)
        platform_config = await self.platform_config(platform_id)
        if any(binding_id in a.notify_binding_ids for a in platform_config.upstream_accounts):
            if "balance" not in config.events or (
                config.kind == "bot" and not config.notification_chat_id
            ):
                raise ControlError(409, "此目标仍被上游余额监控引用，不能移除余额订阅或接收会话")
        monitoring = platform_config.monitoring
        if any(
            binding_id in rule.binding_ids and kind not in config.events
            for kind, rule in monitoring.routes()
        ):
            raise ControlError(
                409, "此Webhook仍被监控策略引用，请先移除策略中的通知目标，再取消对应事件订阅"
            )
        fingerprint = (
            hashlib.sha256(config.bot_id.encode()).hexdigest() if config.kind == "bot" else ""
        )
        async with self.transaction() as db:
            try:
                changed = await db.execute(
                    "UPDATE bindings SET name=?,enabled=?,config=?,bot_fingerprint=?,revision=revision+1,updated_at=? WHERE id=? AND platform_id=? AND EXISTS (SELECT 1 FROM platforms p WHERE p.id=bindings.platform_id AND p.deleted_at IS NULL)",
                    (
                        config.name,
                        int(config.enabled),
                        self.vault.seal(config),
                        fingerprint,
                        time.time(),
                        binding_id,
                        platform_id,
                    ),
                )
                if changed.rowcount != 1:
                    raise ControlError(404, "平台或绑定不存在")
            except aiosqlite.IntegrityError:
                raise ControlError(409, "该机器人已被其他活跃绑定使用") from None
        return await self.binding(platform_id, binding_id)

    async def record_operation(self, platform_id: str, action: str, object_id: str = "") -> None:
        now = time.time()
        async with self.transaction() as db:
            await db.execute(
                "INSERT INTO operation_records(id,platform_id,action,object_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (uuid4().hex, platform_id, action[:256], object_id[:128], now, now),
            )

    async def operation_records(
        self, platform_id: str, *, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        from .operations import Page

        paging = Page(page=page, page_size=page_size)
        async with self.transaction() as db:
            count = await row(
                db,
                "SELECT COUNT(*) AS n FROM operation_records WHERE platform_id=?",
                (platform_id,),
            )
            items = await rows(
                db,
                "SELECT id,action,object_id,outcome,created_at FROM operation_records WHERE platform_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (platform_id, paging.page_size, paging.offset),
            )
        return {"items": items, "total": count["n"] if count else 0, **paging.model_dump()}

    async def prune_operations(self, platform_id: str, before: float) -> None:
        async with self.transaction() as db:
            await db.execute(
                "DELETE FROM operation_records WHERE id IN (SELECT id FROM operation_records WHERE platform_id=? AND created_at<? ORDER BY created_at LIMIT 500)",
                (platform_id, before),
            )
