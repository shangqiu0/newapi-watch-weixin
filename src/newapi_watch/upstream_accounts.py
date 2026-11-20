"""Encrypted configuration DTOs and self-only ordinary-user upstream balance checks."""

import asyncio
import hashlib
import json
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .client import NewAPIClient, UpstreamError
from .models import UserInfo
from .money import CurrencyConfig, decorate_money
from .monitoring.models import Policy
from .monitoring.repository import PolicyRepository
from .redaction import redact_text
from .upstream_throttle import RequestThrottle
from .upstream_usage import UpstreamUsage

if TYPE_CHECKING:
    from .runtime import Runtime


class UpstreamAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    user_id: int = Field(gt=0)
    access_token: SecretStr = SecretStr("")
    enabled: bool = False
    monitor_enabled: bool = False
    poll_seconds: int = Field(default=300, ge=30, le=86400)
    usage_timezone: str = ""
    currency: CurrencyConfig = Field(default_factory=CurrencyConfig)
    threshold_cny: Decimal = Field(
        default=Decimal("0"), ge=0, le=Decimal("1e15"), allow_inf_nan=False, decimal_places=12
    )
    recovery_threshold_cny: Decimal | None = Field(
        default=None, ge=0, le=Decimal("1e15"), allow_inf_nan=False, decimal_places=12
    )
    cooldown_seconds: int = Field(default=1800, ge=30, le=86400)
    notify_recovery: bool = True
    query_binding_ids: list[str] = Field(default_factory=list, max_length=20)
    notify_binding_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("usage_timezone")
    @classmethod
    def valid_usage_timezone(cls, value: str) -> str:
        if value:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError:
                raise ValueError("统计时区不存在") from None
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"https", "http"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("使用不含凭证和查询参数的HTTP(S)平台地址")
        return value.rstrip("/")

    @model_validator(mode="after")
    def valid_monitor(self) -> Self:
        if (
            self.recovery_threshold_cny is not None
            and self.recovery_threshold_cny < self.threshold_cny
        ):
            raise ValueError("恢复阈值不能低于告警阈值")
        if self.monitor_enabled:
            if not self.currency.confirmed or not self.notify_binding_ids:
                raise ValueError("启用上游余额监控需要确认人民币换算并选择通知目标")
            self.currency.to_quota(self.threshold_cny)
            if self.recovery_threshold_cny is not None:
                self.currency.to_quota(self.recovery_threshold_cny)
        return self

    def public(self) -> dict[str, Any]:
        result = self.model_dump(mode="json", exclude={"access_token"})
        result["has_access_token"] = bool(self.access_token.get_secret_value())
        result["threshold_display"] = f"¥{self.threshold_cny:,.2f}"
        return result


class UpstreamAccounts:
    def __init__(self, runtime: "Runtime", accounts: list[UpstreamAccount]) -> None:
        self.runtime, self.accounts = runtime, accounts
        self.closed = False
        self.usage = UpstreamUsage(runtime)
        self.locks: dict[str, asyncio.Lock] = {}
        self.clients: dict[str, NewAPIClient] = {}
        self.repo = PolicyRepository(runtime.store)
        self.next_at: dict[str, float] = {}
        self.inflight: dict[str, asyncio.Task[None]] = {}
        self.slots = asyncio.Semaphore(3)
        throttles = {urlsplit(runtime.settings.newapi_url).netloc: runtime.client.throttle}
        for account in accounts:
            host = urlsplit(account.base_url).netloc
            throttle = throttles.setdefault(
                host, RequestThrottle(min_interval=0 if runtime.settings.demo else 1.2)
            )
            settings = runtime.settings.model_copy(
                update={
                    "newapi_url": account.base_url,
                    "newapi_user_id": account.user_id,
                    "newapi_token": account.access_token,
                    "request_attempts": 1,
                }
            )
            self.clients[account.id] = NewAPIClient(settings, runtime.http, throttle=throttle)

    def account(self, identifier: str, binding_id: str = "web") -> UpstreamAccount:
        account = next((a for a in self.accounts if a.id == identifier), None)
        if account is None or (binding_id != "web" and binding_id not in account.query_binding_ids):
            raise ValueError("上游账户不存在或未绑定当前机器人")
        if self.closed or not account.enabled:
            raise ValueError("上游账户已停用")
        return account

    def visible(self, binding_id: str = "web") -> list[UpstreamAccount]:
        return [
            a
            for a in self.accounts
            if a.enabled and (binding_id == "web" or binding_id in a.query_binding_ids)
        ]

    def scope(self, account: UpstreamAccount) -> str:
        config = account.model_dump(mode="json", exclude={"access_token", "name", "usage_timezone"})
        context = json.dumps(config, sort_keys=True) + account.access_token.get_secret_value()
        return hashlib.sha256(context.encode()).hexdigest()

    async def query(self, identifier: str, *, binding_id: str = "web") -> dict[str, Any]:
        account = self.account(identifier, binding_id)
        async with self.locks.setdefault(identifier, asyncio.Lock()):
            try:
                payload = await self.clients[identifier].request("/api/user/self", retry=False)
                data = payload.get("data")
                if (
                    not isinstance(data, dict)
                    or type(data.get("id")) is not int
                    or data["id"] != account.user_id
                    or type(data.get("quota")) is not int
                    or abs(data.get("quota", 0)) > 2**63 - 1
                ):
                    raise ValueError("上游自身接口缺少真实余额或用户身份不匹配，未将缺失值当作零")
                if "used_quota" in data and type(data["used_quota"]) is not int:
                    raise ValueError("上游累计用量字段格式不兼容")
                try:
                    user = UserInfo.model_validate(data)
                except ValidationError:
                    raise ValueError("上游自身账户字段格式不兼容") from None
                if self.closed:
                    raise ValueError("上游账户配置已变更，请重新查询")
                result = decorate_money(user.model_dump(), account.currency)
                if "used_quota" not in data:
                    result.pop("used_quota_display", None)
                    result.pop("used_quota_cny", None)
                    result["used_quota"] = None
                result.update(
                    account_id=account.id,
                    account_name=account.name,
                    source="newapi_user_self",
                    queried_at=time.time(),
                    complete=True,
                )
                result = {
                    key: redact_text(
                        redact_text(value, self.clients[identifier].settings), self.runtime.settings
                    )
                    if isinstance(value, str)
                    else value
                    for key, value in result.items()
                }
                await self.runtime.store.set_meta(
                    "upstream:sample:" + self.scope(account), json.dumps(result, ensure_ascii=False)
                )
                await self.runtime.store.set_meta("upstream:error:" + self.scope(account), "")
                return result
            except UpstreamError as error:
                raise ValueError(str(error)) from None

    async def query_today_usage(
        self, identifier: str, *, binding_id: str = "web", now: float | None = None
    ) -> dict[str, Any]:
        account = self.account(identifier, binding_id)
        client = self.clients[identifier]
        usage = await self.usage.query(account, client, self.scope(account), now=now, fresh=True)
        if self.closed:
            raise ValueError("上游账户配置已变更，请重新查询")
        if usage["status"] != "ok":
            reason = redact_text(str(usage["error"]), client.settings)
            raise ValueError(redact_text(reason, self.runtime.settings))
        return {
            **usage,
            "account_id": account.id,
            "account_name": redact_text(account.name, client.settings),
        }

    async def snapshot(self, account: UpstreamAccount) -> dict[str, Any]:
        sample = json.loads(
            await self.runtime.store.get_meta("upstream:sample:" + self.scope(account)) or "null"
        )
        error = await self.runtime.store.get_meta("upstream:error:" + self.scope(account))
        return {
            **account.public(),
            "sample": sample,
            "error": error or "",
            "usage": await self.usage.snapshot(account, self.scope(account)),
        }

    async def check(self, identifier: str, *, now: float | None = None) -> bool:
        account = self.account(identifier)
        if not account.monitor_enabled:
            return False
        now = time.time() if now is None else now
        key = "policy:upstream:" + self.scope(account)
        try:
            result = await self.query(identifier)
        except ValueError as error:
            if not self.closed:
                await self.runtime.store.set_meta(
                    "upstream:error:" + self.scope(account), str(error)
                )
            return False
        quota = result["quota"]
        threshold = account.currency.to_quota(account.threshold_cny)
        recovery = account.currency.to_quota(
            account.recovery_threshold_cny
            if account.recovery_threshold_cny is not None
            else account.threshold_cny
        )
        states = {s["id"]: s for s in await self.repo.states()}
        active = bool(states.get(key, {}).get("is_active"))
        low = quota < (recovery if active else threshold)
        text = (
            ("上游账户余额不足" if low else "上游账户余额恢复")
            + f"\n账户：{account.name}\n余额：{account.currency.display(quota)}\n告警阈值：{account.currency.display(threshold)}\n数据来源：上游自身账户接口"
        )
        text = redact_text(
            redact_text(text, self.clients[identifier].settings), self.runtime.settings
        )
        policy = Policy(
            binding_ids=account.notify_binding_ids,
            cooldown_seconds=account.cooldown_seconds,
            notify_recovery=account.notify_recovery,
        )
        await self.repo.condition(
            key,
            low,
            text,
            policy,
            "balance",
            now,
            evidence={"account_id": account.id, "quota": quota, "threshold": threshold},
            repeat_notifications=True,
        )
        return True

    async def initialize(self) -> None:
        allowed = {self.scope(a) for a in self.accounts if a.enabled and a.monitor_enabled}
        async with self.runtime.store.transaction() as db:
            cursor = await db.execute(
                "SELECT id,dedupe_key FROM notifications WHERE status IN ('pending','sending') AND dedupe_key LIKE 'policy:policy:upstream:%'"
            )
            for identifier, key in await cursor.fetchall():
                if not any(f"policy:policy:upstream:{scope}:" in key for scope in allowed):
                    await db.execute(
                        "UPDATE notifications SET status='historical',lease_until=0,last_error='上游账户或策略已变更，已停止旧通知' WHERE id=?",
                        (identifier,),
                    )

    async def tick(self) -> None:
        now = time.time()
        for account in self.accounts:
            if (
                self.closed
                or not account.enabled
                or not account.monitor_enabled
                or account.id in self.inflight
                or now < self.next_at.get(account.id, 0)
            ):
                continue
            self.next_at[account.id] = now + account.poll_seconds
            self.inflight[account.id] = asyncio.create_task(self._run(account.id, now))

    async def _run(self, identifier: str, now: float) -> None:
        try:
            async with self.slots:
                if not self.closed:
                    success = await self.check(identifier, now=now)
                    if success and not self.closed:
                        account = self.account(identifier)
                        await self.usage.query(
                            account, self.clients[identifier], self.scope(account)
                        )
        finally:
            self.inflight.pop(identifier, None)

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()
