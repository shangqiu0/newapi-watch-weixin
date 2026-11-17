"""Cookie/CSRF authenticated management API, separate from legacy global service-key routes."""

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .auth import Auth
from .client import UpstreamError
from .console_extensions import console_extensions
from .control_models import BindingInput, BindingPatch, PlatformInput, PlatformPatch
from .control_store import ControlError, ControlStore
from .fleet import Fleet
from .model_provider import Provider
from .money import decorate_money
from .operations import OperationError
from .periods import StatisticsQuery
from .transport import RequestLimits
from .upstream_throttle import UpstreamRateLimited

logger = logging.getLogger(__name__)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class QueryInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def envelope(data: Any = None, message: str = "success", code: int = 0) -> dict[str, Any]:
    return {"code": code, "data": data, "message": message}


def create_console(
    control: ControlStore, fleet: Fleet, auth: Auth, *, lifespan: Callable[..., Any] | None = None
) -> FastAPI:
    app = FastAPI(
        title="NewAPI Watch Console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestLimits)

    @app.exception_handler(ControlError)
    async def control_error(request: Request, exc: ControlError) -> JSONResponse:
        return JSONResponse(envelope(message=str(exc), code=exc.status), status_code=exc.status)

    @app.exception_handler(OperationError)
    async def operation_error(request: Request, exc: OperationError) -> JSONResponse:
        return JSONResponse(envelope(message=str(exc), code=exc.status), status_code=exc.status)

    @app.exception_handler(ValueError)
    async def bad_parameter(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            envelope(message="日期或查询参数无效，请检查范围（最多31天）", code=400),
            status_code=400,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            envelope(message="输入无效，请检查字段、长度和格式", code=400), status_code=400
        )

    @app.exception_handler(ValidationError)
    async def invalid_config(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            envelope(message="配置不完整或格式无效，请核对凭证和模型设置", code=400),
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        error_id = uuid4().hex
        logger.error("console_failed id=%s type=%s", error_id, type(exc).__name__)
        return JSONResponse(
            envelope({"error_id": error_id}, "操作暂时失败，请重试或检查配置", 500), status_code=500
        )

    def origin(request: Request) -> None:
        supplied = request.headers.get("origin")
        if supplied and supplied.rstrip("/") != str(request.base_url).rstrip("/"):
            raise ControlError(403, "只允许同源管理请求")

    async def session(request: Request) -> dict[str, str]:
        identity = await auth.authenticate(request.cookies.get("watch_session", ""))
        if request.method not in {"GET", "HEAD"}:
            origin(request)
            await auth.check_csrf(
                request.cookies.get("watch_session", ""), request.headers.get("X-CSRF-Token", "")
            )
        return identity

    @app.middleware("http")
    async def management_operations(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        params = request.path_params
        route = request.scope.get("route")
        template = getattr(route, "path", "")
        skip = (
            "cleanup-previews",
            "currency-previews",
            "model-checks",
            "statistics-queries",
            "balance-queries",
        )
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and 200 <= response.status_code < 300
            and template.startswith("/console/api/platforms/")
            and not any(v in template for v in skip)
        ):
            platform_id = str(params.get("platform_id", ""))
            if platform_id:
                object_id = next(
                    (
                        str(params[k])
                        for k in ("binding_id", "account_id", "identifier")
                        if k in params
                    ),
                    platform_id,
                )
                await control.record_operation(
                    platform_id, request.method + " " + template, object_id
                )
        return response

    @app.get("/health/live")
    async def live() -> dict[str, Any]:
        return envelope({"status": "alive", "mode": "console", "demo": fleet.demo})

    @app.post("/console/api/login")
    async def login(payload: LoginInput, request: Request) -> JSONResponse:
        origin(request)
        authenticated = await auth.login(
            payload.username, payload.password, request.client.host if request.client else "unknown"
        )
        response = JSONResponse(
            envelope(
                {
                    "username": authenticated.username,
                    "csrf_token": authenticated.csrf,
                    "demo": fleet.demo,
                }
            )
        )
        response.set_cookie(
            "watch_session",
            authenticated.token,
            max_age=8 * 3600,
            httponly=True,
            secure=auth.secure_cookie,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/console/api/session")
    async def current(
        request: Request, identity: Annotated[dict[str, str], Depends(session)]
    ) -> dict[str, Any]:
        return envelope(
            {
                "username": identity["username"],
                "csrf_token": auth.csrf(request.cookies["watch_session"]),
                "demo": fleet.demo,
            }
        )

    @app.post("/console/api/logout")
    async def logout(
        request: Request, identity: Annotated[dict[str, str], Depends(session)]
    ) -> JSONResponse:
        await auth.logout(request.cookies.get("watch_session", ""))
        response = JSONResponse(envelope())
        response.delete_cookie("watch_session", path="/")
        return response

    @app.api_route("/console/api/register", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def no_registration(request: Request) -> JSONResponse:
        return JSONResponse(envelope(message="不支持注册", code=404), status_code=404)

    @app.get("/console/api/platforms", dependencies=[Depends(session)])
    async def platforms() -> dict[str, Any]:
        items = await control.platforms()
        for item in items:
            item["runtime_status"] = (
                "error"
                if item["id"] in fleet.failures
                else "running"
                if item["enabled"]
                else "disabled"
            )
        return envelope(items)

    @app.post("/console/api/platforms", status_code=201, dependencies=[Depends(session)])
    async def create(payload: PlatformInput) -> dict[str, Any]:
        saved = await control.create_platform(payload)
        await fleet.reload(saved["id"])
        return envelope(saved)

    @app.patch("/console/api/platforms/{platform_id}", dependencies=[Depends(session)])
    async def update(platform_id: str, payload: PlatformPatch) -> dict[str, Any]:
        saved = await control.patch_platform(platform_id, payload)
        if payload.retention is not None:
            await fleet.entry(platform_id).runtime.store.set_meta(
                "retention:next_at", str(time.time() + 3600)
            )
        await fleet.reload(platform_id, reset_model_diagnostic=payload.llm is not None)
        return envelope(saved)

    @app.delete("/console/api/platforms/{platform_id}", dependencies=[Depends(session)])
    async def delete_platform(
        platform_id: str, revision: int | None = Query(None, ge=1)
    ) -> dict[str, Any]:
        return envelope(await fleet.delete_platform(platform_id, revision=revision))

    @app.get("/console/api/platforms/{platform_id}/overview", dependencies=[Depends(session)])
    async def overview(platform_id: str) -> dict[str, Any]:
        platform = await control.platform(platform_id)
        entry = fleet.entry(platform_id)
        runtime = entry.runtime
        today = datetime.now(ZoneInfo(runtime.settings.timezone)).date()
        stats = await runtime.reports.analytics.snapshot(StatisticsQuery(start=today, end=today))
        health = await runtime.commands.health.snapshot()
        health["bot"] = {
            "configured": len(
                [
                    b
                    for b in await control.bindings(platform_id)
                    if b["kind"] == "bot" and b["enabled"]
                ]
            ),
            "authenticated": sum(b.authenticated for b in entry.bots),
        }
        return envelope(
            {
                "platform": platform,
                "monitoring": await runtime.policies.snapshot() if runtime.policies else None,
                "health": health,
                "channels": await runtime.store.channels(),
                "statistics": decorate_money(
                    stats.model_dump(mode="json"), runtime.settings.currency
                ),
                "alerts": await runtime.commands.operations.alerts(),
                "audits": await runtime.commands.audits.list(),
                "bindings": await control.bindings(platform_id),
                "model_status": json.loads(
                    await runtime.store.get_meta("llm_diagnostic") or "null"
                ),
                "demo": fleet.demo,
            }
        )

    @app.get("/console/api/platforms/{platform_id}/bindings", dependencies=[Depends(session)])
    async def bindings(platform_id: str) -> dict[str, Any]:
        return envelope(await control.bindings(platform_id))

    @app.post(
        "/console/api/platforms/{platform_id}/bindings",
        status_code=201,
        dependencies=[Depends(session)],
    )
    async def bind(platform_id: str, payload: BindingInput) -> dict[str, Any]:
        saved = await control.create_binding(platform_id, payload)
        await fleet.reload(platform_id)
        return envelope(saved)

    @app.patch(
        "/console/api/platforms/{platform_id}/bindings/{binding_id}",
        dependencies=[Depends(session)],
    )
    async def update_binding(
        platform_id: str, binding_id: str, payload: BindingPatch
    ) -> dict[str, Any]:
        saved = await control.patch_binding(platform_id, binding_id, payload)
        await fleet.reload(platform_id)
        return envelope(saved)

    @app.post("/console/api/platforms/{platform_id}/queries", dependencies=[Depends(session)])
    async def query(platform_id: str, payload: QueryInput, request: Request) -> dict[str, Any]:
        await control.platform(platform_id)
        entry = fleet.entry(platform_id)
        if not entry.enabled:
            raise ControlError(409, "平台已停用，请先启用后查询")
        result = await entry.assistant.execute(
            payload.text,
            actor="console-admin",
            source="http",
            request_id=uuid4().hex,
            conversation_id=auth.session_fingerprint(request.cookies.get("watch_session", "")),
        )
        return envelope(
            {
                "text": result.text,
                "ok": result.ok,
                "image_base64": base64.b64encode(result.image).decode() if result.image else None,
            }
        )

    @app.post("/console/api/platforms/{platform_id}/model-checks", dependencies=[Depends(session)])
    async def model_check(platform_id: str) -> dict[str, Any]:
        config = (await control.platform_config(platform_id)).llm
        if not config.base_url or not config.model or not config.api_key.get_secret_value():
            raise ControlError(400, "请先保存模型地址、模型ID和模型调用Key")
        result = await Provider(config, fleet.llm_http).probe()
        # Network calls do not hold a lifecycle/database lock. Only publish the
        # result if it still belongs to the saved model configuration.
        async with fleet.locks.setdefault(platform_id, asyncio.Lock()):
            if (await control.platform_config(platform_id)).llm != config:
                raise ControlError(409, "模型配置已变更，请重新测试已保存的连接")
            await fleet.entry(platform_id).runtime.store.set_meta(
                "llm_diagnostic", json.dumps(result, ensure_ascii=False)
            )
        return envelope(result)

    @app.post("/console/api/platforms/{platform_id}/checks", dependencies=[Depends(session)])
    async def check(platform_id: str) -> dict[str, Any]:
        await control.platform(platform_id)
        try:
            channels = await fleet.entry(platform_id).runtime.client.channels()
        except Exception:
            raise ControlError(502, "连接或管理鉴权失败，请核对地址、令牌与用户ID") from None
        return envelope(
            {"ok": True, "channels": len(channels), "note": "只读检查，未探测或修改渠道"}
        )

    app.include_router(console_extensions(control, fleet, session))

    @app.exception_handler(UpstreamError)
    async def upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
        if isinstance(exc, UpstreamRateLimited):
            return JSONResponse(
                envelope({"retry_after": exc.retry_after}, message=str(exc), code=429),
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )
        return JSONResponse(envelope(message=str(exc), code=502), status_code=502)

    web = Path(__file__).with_name("web-dist")
    assets = web / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    async def index() -> Response:
        if not (web / "index.html").is_file():
            return HTMLResponse(
                "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>前端开发服务</title><body><h1>前端资源尚未生成</h1><p>API 服务仍可用。本地开发请运行 pnpm --dir frontend dev，打开 127.0.0.1:8848；如需由后端提供页面，先运行 pnpm --dir frontend build。</p></body></html>",
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/login")
    async def spa_login() -> Response:
        return await index()

    @app.get("/error/{status_code}")
    async def spa_error(status_code: int) -> Response:
        if status_code not in {403, 404, 500}:
            raise ControlError(404, "页面不存在")
        return await index()

    @app.get("/platform-config.json")
    async def frontend_config() -> FileResponse:
        return FileResponse(web / "platform-config.json", headers={"Cache-Control": "no-store"})

    @app.api_route("/console/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def spa_fallback(rest: str, request: Request) -> Response:
        if rest == "api" or rest.startswith("api/") or request.method != "GET":
            raise ControlError(404, "接口不存在")
        return await index()

    return app
