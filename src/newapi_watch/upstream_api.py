"""Platform-bound ordinary-user account and currency endpoints; parent supplies auth/CSRF."""

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from .control_models import PlatformPatch
from .control_store import ControlError, ControlStore
from .fleet import Fleet
from .money import currency_candidate
from .upstream_accounts import UpstreamAccount
from .vault import plaintext


def upstream_routes(control: ControlStore, fleet: Fleet) -> APIRouter:
    router = APIRouter()
    locks: dict[str, asyncio.Lock] = {}

    def wrap(data: Any) -> dict[str, Any]:
        return {"code": 0, "message": "success", "data": data}

    @router.post("/{platform_id}/currency-previews")
    async def currency_preview(platform_id: str) -> dict[str, Any]:
        await control.platform(platform_id)
        data = await fleet.entry(platform_id).runtime.client.request("/api/status", retry=False)
        try:
            return wrap(currency_candidate(data.get("data", {})).model_dump(mode="json"))
        except ValueError as error:
            raise ControlError(400, str(error)) from None

    @router.get("/{platform_id}/upstream-accounts")
    async def listing(
        platform_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
    ) -> dict[str, Any]:
        config = await control.platform_config(platform_id)
        service = fleet.entry(platform_id).runtime.upstream
        accounts = config.upstream_accounts
        return wrap(
            {
                "items": [
                    await service.snapshot(a)
                    for a in accounts[(page - 1) * page_size : page * page_size]
                ],
                "total": len(accounts),
                "page": page,
                "page_size": page_size,
            }
        )

    @router.post("/{platform_id}/upstream-accounts", status_code=201)
    async def create(platform_id: str, account: UpstreamAccount) -> dict[str, Any]:
        if not account.access_token.get_secret_value().strip():
            raise ControlError(400, "需要上游普通用户访问令牌，不是模型API Key")
        async with locks.setdefault(platform_id, asyncio.Lock()):
            config = await control.platform_config(platform_id)
            if any(a.id == account.id for a in config.upstream_accounts):
                raise ControlError(409, "账户ID已存在")
            await control.patch_platform(
                platform_id, PlatformPatch(upstream_accounts=[*config.upstream_accounts, account])
            )
            await fleet.reload(platform_id)
        return wrap(account.public())

    @router.patch("/{platform_id}/upstream-accounts/{account_id}")
    async def update(platform_id: str, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with locks.setdefault(platform_id, asyncio.Lock()):
            config = await control.platform_config(platform_id)
            previous = next((a for a in config.upstream_accounts if a.id == account_id), None)
            if previous is None:
                raise ControlError(404, "该平台的上游账户不存在")
            changes = dict(payload)
            if "id" in changes and changes["id"] != account_id:
                raise ControlError(400, "不能修改账户ID")
            if not changes.get("access_token"):
                if (
                    changes.get("base_url", previous.base_url) != previous.base_url
                    or changes.get("user_id", previous.user_id) != previous.user_id
                ):
                    raise ControlError(400, "更换上游地址或用户需要重新填写对应访问令牌")
                changes.pop("access_token", None)
            merged = UpstreamAccount.model_validate({**plaintext(previous), **changes})
            updated = [merged if a.id == account_id else a for a in config.upstream_accounts]
            await control.patch_platform(platform_id, PlatformPatch(upstream_accounts=updated))
            await fleet.reload(platform_id)
        return wrap(merged.public())

    @router.delete("/{platform_id}/upstream-accounts/{account_id}")
    async def delete(platform_id: str, account_id: str) -> dict[str, Any]:
        async with locks.setdefault(platform_id, asyncio.Lock()):
            config = await control.platform_config(platform_id)
            if not any(a.id == account_id for a in config.upstream_accounts):
                raise ControlError(404, "该平台的上游账户不存在")
            await control.patch_platform(
                platform_id,
                PlatformPatch(
                    upstream_accounts=[a for a in config.upstream_accounts if a.id != account_id]
                ),
            )
            await fleet.reload(platform_id)
        return wrap({"deleted": True})

    @router.post("/{platform_id}/upstream-accounts/{account_id}/balance-queries")
    async def query(platform_id: str, account_id: str) -> dict[str, Any]:
        await control.platform(platform_id)
        entry = fleet.entry(platform_id)
        if not entry.enabled:
            raise ControlError(409, "当前平台已停用")
        try:
            return wrap(await entry.runtime.upstream.query(account_id))
        except ValueError as error:
            raise ControlError(502, str(error)) from None

    @router.post("/{platform_id}/upstream-accounts/{account_id}/currency-previews")
    async def upstream_currency(platform_id: str, account_id: str) -> dict[str, Any]:
        await control.platform(platform_id)
        service = fleet.entry(platform_id).runtime.upstream
        # A status preview may be used before enabling queries/monitoring.
        if account_id not in service.clients:
            raise ControlError(404, "该平台的上游账户不存在")
        data = await service.clients[account_id].request("/api/status", retry=False)
        try:
            return wrap(currency_candidate(data.get("data", {})).model_dump(mode="json"))
        except ValueError as error:
            raise ControlError(400, str(error)) from None

    return router
