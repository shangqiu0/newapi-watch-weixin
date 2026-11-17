"""Additive console routes; existing authentication and response contracts stay intact."""

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .control_models import PlatformPatch
from .control_store import ControlError, ControlStore
from .fleet import Fleet
from .money import decorate_money, normalize_balance_amounts
from .monitoring.models import ErrorRule, MonitoringConfig
from .monitoring.service import preview
from .operations import OperationError
from .periods import StatisticsQuery, parse_period
from .persistence import row, rows
from .query_engine.api import query_routes
from .retention_api import retention_routes
from .upstream_api import upstream_routes


class MonitorRunInput(BaseModel):
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class NotificationRetryInput(MonitorRunInput):
    notification_id: int = Field(gt=0)


class PreviewInput(BaseModel):
    rule: ErrorRule
    sample: dict[str, str | int] = Field(default_factory=dict)


def console_extensions(
    control: ControlStore, fleet: Fleet, session: Callable[..., Any]
) -> APIRouter:
    router = APIRouter(prefix="/console/api/platforms", dependencies=[Depends(session)])

    router.include_router(query_routes(fleet))
    router.include_router(upstream_routes(control, fleet))
    router.include_router(retention_routes(control, fleet))

    def wrap(data: Any, *, platform_id: str) -> dict[str, Any]:
        data = decorate_money(data, fleet.entry(platform_id).runtime.settings.currency)
        return {"code": 0, "message": "success", "data": data}

    def entry(platform_id: str) -> Any:
        return fleet.entry(platform_id)

    @router.get("/{platform_id}/alerts")
    async def alerts(
        platform_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
    ) -> dict[str, Any]:
        result = await entry(platform_id).runtime.commands.operations.alerts(
            page=page, page_size=page_size
        )
        return wrap(result, platform_id=platform_id)

    @router.get("/{platform_id}/notifications")
    async def notifications(
        platform_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
    ) -> dict[str, Any]:
        runtime = entry(platform_id).runtime
        async with runtime.store.transaction() as db:
            count = await row(db, "SELECT COUNT(*) AS total FROM notifications")
            items = await rows(
                db,
                "SELECT id,kind,status,attempts,last_error,next_at,created_at,updated_at,binding_ids FROM notifications ORDER BY id DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            )
        for item in items:
            item["delivery_method"] = (
                "manual" if str(item["last_error"]).startswith("管理员人工标记") else "webhook"
            )
            item["binding_ids"] = json.loads(item["binding_ids"])
        return wrap(
            {
                "items": items,
                "total": count["total"] if count else 0,
                "page": page,
                "page_size": page_size,
            },
            platform_id=platform_id,
        )

    @router.post("/{platform_id}/notification-retries")
    async def retry_notification(
        platform_id: str, payload: NotificationRetryInput
    ) -> dict[str, Any]:
        try:
            result = await entry(platform_id).runtime.commands.operations.retry(
                payload.notification_id, payload.request_id
            )
        except OperationError as exc:
            raise ControlError(exc.status, str(exc)) from None
        return wrap(result, platform_id=platform_id)

    @router.get("/{platform_id}/audits")
    async def audits(
        platform_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
    ) -> dict[str, Any]:
        result = await entry(platform_id).runtime.commands.audits.list(
            page=page, page_size=page_size
        )
        return wrap(result, platform_id=platform_id)

    @router.get("/{platform_id}/channels")
    async def channels(
        platform_id: str,
        live: bool = True,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        keyword: str = Query("", max_length=100),
        status: int | None = Query(None, ge=0, le=3),
    ) -> dict[str, Any]:
        result = await entry(platform_id).runtime.commands.data.channels(live=live)
        filtered = [
            c
            for c in result["items"]
            if (status is None or c["status"] == status)
            and keyword.lower() in f"{c['name']} {c['id']}".lower()
        ]
        return wrap(
            {
                **result,
                "items": filtered[(page - 1) * page_size : page * page_size],
                "total": len(filtered),
                "all_total": result["total"],
                "page": page,
                "page_size": page_size,
            },
            platform_id=platform_id,
        )

    @router.post("/{platform_id}/statistics-queries")
    @router.get("/{platform_id}/statistics")
    async def statistics(
        platform_id: str,
        request: Request,
        period: str = Query("近7天", max_length=32),
        dimension: Literal["model", "channel", "user"] = "model",
        metric: Literal["quota", "calls", "errors", "tokens"] = "quota",
        live: bool = True,
        exact: bool = False,
    ) -> dict[str, Any]:
        runtime = entry(platform_id).runtime
        bounds = parse_period(
            period, today=datetime.now(ZoneInfo(runtime.settings.timezone)).date()
        )
        query = StatisticsQuery(**bounds.model_dump(), dimension=dimension, metric=metric, limit=20)
        if live:
            return wrap(
                await runtime.query_engine.statistics(
                    query, "console", exact=exact, allow_tasks=request.method == "POST"
                ),
                platform_id=platform_id,
            )
        return wrap(
            await runtime.commands.data.statistics(query, live=False), platform_id=platform_id
        )

    @router.get("/{platform_id}/logs")
    async def logs(
        platform_id: str,
        period: str = Query("今天", max_length=32),
        live: bool = True,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        log_type: int = Query(0, ge=0, le=5),
        channel_id: int = Query(0, ge=0),
        http_status: int = Query(0, ge=0, le=599),
        cursor: str = Query("", max_length=64),
    ) -> dict[str, Any]:
        runtime = entry(platform_id).runtime
        bounds = parse_period(
            period, today=datetime.now(ZoneInfo(runtime.settings.timezone)).date()
        )
        start, end = bounds.window(runtime.settings.timezone)
        if live and (http_status or cursor):
            return wrap(
                await runtime.error_search.search(
                    start,
                    end,
                    owner="console",
                    status=http_status,
                    channel=channel_id,
                    cursor=cursor,
                ),
                platform_id=platform_id,
            )
        if live:
            return wrap(
                await runtime.commands.data.logs(
                    start,
                    end,
                    page=page,
                    page_size=page_size,
                    log_type=log_type,
                    channel_id=channel_id,
                    http_status=http_status,
                ),
                platform_id=platform_id,
            )
        params = (start, end, log_type, log_type, channel_id, channel_id, http_status, http_status)
        where = "created_at>=? AND created_at<? AND (?=0 OR type=?) AND (?=0 OR channel_id=?) AND (?=0 OR http_status=?)"
        async with runtime.store.transaction() as db:
            count = await row(db, "SELECT COUNT(*) AS total FROM logs WHERE " + where, params)
            items = await rows(
                db,
                "SELECT id,type,channel_id,channel_name,user_id,model_name,quota,prompt_tokens,completion_tokens,use_time,created_at,http_status,error_code,error_message FROM logs WHERE "
                + where
                + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            )
        return wrap(
            {
                "items": items,
                "total": count["total"] if count else 0,
                "page": page,
                "page_size": page_size,
                "source": "newapi_admin_api" if live else "local_newapi_logs",
                "complete": live,
                "timezone": runtime.settings.timezone,
            },
            platform_id=platform_id,
        )

    @router.get("/{platform_id}/users")
    async def users(
        platform_id: str,
        keyword: str = Query("", max_length=100),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        items = await entry(platform_id).runtime.commands.data.users(keyword)
        return wrap(
            {
                "items": [i.model_dump() for i in items[(page - 1) * page_size : page * page_size]],
                "total": len(items),
                "source": "newapi_admin_api",
                "page": page,
                "page_size": page_size,
            },
            platform_id=platform_id,
        )

    @router.get("/{platform_id}/monitoring")
    async def monitoring(platform_id: str) -> dict[str, Any]:
        config = (await control.platform_config(platform_id)).monitoring
        runtime = entry(platform_id).runtime
        async with runtime.store.transaction() as db:
            samples = await rows(
                db, "SELECT id,quota,threshold,is_low,updated_at FROM account_samples"
            )
        errors = {
            str(r.user_id): await runtime.store.get_meta(f"balance_error:{r.user_id}")
            for r in config.balances
        }
        return wrap(
            {
                "config": config.model_dump(),
                "samples": samples,
                "errors": errors,
                "execution": await runtime.policies.snapshot() if runtime.policies else None,
                "last_run": json.loads(await runtime.store.get_meta("policy:last_run") or "null"),
            },
            platform_id=platform_id,
        )

    @router.put("/{platform_id}/monitoring")
    async def save_monitoring(platform_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        currency = (await control.platform_config(platform_id)).currency
        try:
            config = MonitoringConfig.model_validate(normalize_balance_amounts(payload, currency))
        except ValueError:
            raise ControlError(400, "余额阈值或策略无效，请确认人民币换算和输入范围") from None
        await control.patch_platform(platform_id, PlatformPatch(monitoring=config))
        runtime = entry(platform_id).runtime
        if runtime.policies:
            await runtime.policies.configure(config)
        return wrap(config.model_dump(), platform_id=platform_id)

    @router.post("/{platform_id}/monitor-runs", status_code=202)
    async def monitor_run(platform_id: str, payload: MonitorRunInput) -> dict[str, Any]:
        async with fleet.locks.setdefault(platform_id, asyncio.Lock()):
            e = entry(platform_id)
            runtime = e.runtime
            if not e.enabled or runtime.policies is None:
                raise ControlError(409, "平台已停用，请先启用平台")
            identifier = hashlib.sha256(payload.request_id.encode()).hexdigest()[:24]
            key = "policy:run:" + identifier
            previous = await runtime.store.get_meta(key)
            if previous:
                return wrap(json.loads(previous), platform_id=platform_id)
            if any(
                t.get_name().startswith("manual-check-") and not t.done() for t in runtime.tasks
            ):
                raise ControlError(409, "已有手动复查正在进行，请等待检查结果")
            result = {"id": identifier, "status": "queued", "requested_at": time.time()}
            await runtime.store.set_meta(key, json.dumps(result))
            await runtime.store.set_meta("policy:last_run", json.dumps(result))

            async def run() -> None:
                try:
                    await runtime.policies.tick(force=True)
                    result["status"] = "completed"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result["status"] = "failed"
                finally:
                    if result["status"] == "queued":
                        result["status"] = "interrupted"
                    result["finished_at"] = time.time()
                    await runtime.store.set_meta(key, json.dumps(result))
                    await runtime.store.set_meta("policy:last_run", json.dumps(result))

            runtime.tasks = [t for t in runtime.tasks if not t.done()]
            runtime.tasks.append(asyncio.create_task(run(), name="manual-check-" + identifier))
            return wrap(result, platform_id=platform_id)

    @router.post("/{platform_id}/alert-previews")
    async def alert_preview(platform_id: str, payload: PreviewInput) -> dict[str, Any]:
        config = await control.platform_config(platform_id)
        return wrap(
            {
                "text": preview(payload.rule, payload.sample, timezone=config.timezone),
                "sends_messages": False,
            },
            platform_id=platform_id,
        )

    @router.delete("/{platform_id}/bindings/{binding_id}")
    async def delete_binding(platform_id: str, binding_id: str) -> dict[str, Any]:
        await control.binding(platform_id, binding_id)
        saved = await control.platform_config(platform_id)
        if any(
            binding_id in a.query_binding_ids or binding_id in a.notify_binding_ids
            for a in saved.upstream_accounts
        ):
            raise ControlError(409, "此绑定仍被上游账户引用，请先调整查询或通知目标")
        monitoring = saved.monitoring
        if any(binding_id in rule.binding_ids for _, rule in monitoring.routes()):
            raise ControlError(
                409, "此Webhook仍被监控策略引用，请先修改策略中的通知目标；也可暂时停用绑定"
            )
        async with control.transaction() as db:
            await db.execute(
                "DELETE FROM target_deliveries WHERE platform_id=? AND binding_id=?",
                (platform_id, binding_id),
            )
            await db.execute(
                "DELETE FROM bindings WHERE platform_id=? AND id=?", (platform_id, binding_id)
            )
        await fleet.reload(platform_id)
        return wrap({"deleted": True}, platform_id=platform_id)

    return router
