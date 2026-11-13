"""HTTP adapter: one shared service key, no accounts or role hierarchy."""

import asyncio
import hashlib
import hmac
import logging
import re
import time
from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .audit import Audit
from .commands import Commands
from .config import Settings
from .health import Health
from .management_api import management_router
from .models import NewAPIEvent
from .monitor import Monitor
from .notifications import verify_signature
from .operations import OperationError, Operations
from .reports import Reports
from .store import Store
from .transport import RequestLimits

logger = logging.getLogger(__name__)


class CommandInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def envelope(data: Any, message: str = "success", code: int = 0) -> dict[str, Any]:
    return {"code": code, "data": data, "message": message}


def create_app(
    settings: Settings,
    store: Store,
    commands: Any,
    monitor: Monitor | None,
    *,
    lifespan: Callable[..., Any] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="NewAPI Watch",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(RequestLimits)
    command_slots = asyncio.Semaphore(settings.bot_concurrency)

    async def service_key(x_api_key: str = Header(default="")) -> None:
        expected = settings.api_key.get_secret_value()
        if not expected or not hmac.compare_digest(expected.encode(), x_api_key.encode()):
            raise HTTPException(401, "需要有效的 X-API-Key 服务密钥")

    audits = commands.audits if isinstance(commands, Commands) else Audit(settings, store)
    ops = commands.operations if isinstance(commands, Commands) else Operations(store)
    reports = commands.reports if isinstance(commands, Commands) else Reports(settings, store)
    app.include_router(management_router(reports, ops, audits, service_key, settings))

    @app.exception_handler(OperationError)
    async def operation_error(request: Request, exc: OperationError) -> JSONResponse:
        return JSONResponse(envelope(None, str(exc), exc.status), status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            envelope(None, str(exc.detail), exc.status_code), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            envelope(None, "参数不合法，请核对类型、长度和范围", 400), status_code=400
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        error_id = uuid4().hex
        logger.error("request_failed id=%s type=%s", error_id, type(exc).__name__)
        return JSONResponse(
            envelope({"error_id": error_id}, "服务暂时不可用", 500), status_code=500
        )

    @app.get("/health/live")
    async def live() -> dict[str, Any]:
        return envelope({"status": "alive", "demo": settings.demo})

    @app.get("/api/v1/health", dependencies=[Depends(service_key)])
    async def health() -> dict[str, Any]:
        health_service = (
            commands.health if isinstance(commands, Commands) else Health(settings, store)
        )
        return envelope(await health_service.snapshot())

    @app.get("/api/v1/channels", dependencies=[Depends(service_key)])
    async def channels(
        page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)
    ) -> dict[str, Any]:
        items = await store.channels()
        start = (page - 1) * page_size
        return envelope(
            {
                "items": items[start : start + page_size],
                "total": len(items),
                "page": page,
                "page_size": page_size,
            }
        )

    @app.get("/api/v1/alerts", dependencies=[Depends(service_key)])
    async def alerts(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return envelope(await store.alerts(limit))

    @app.get("/api/v1/notifications", dependencies=[Depends(service_key)])
    async def notifications(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return envelope(await store.notifications(limit))

    @app.post("/api/v1/commands", dependencies=[Depends(service_key)])
    async def command(
        payload: CommandInput, idempotency_key: str = Header(default="")
    ) -> dict[str, Any]:
        if commands is None:
            raise HTTPException(503, "命令服务未启动")
        mutation = bool(re.search(r"测试|确认|重试|(?:静默(?!列表))", payload.text))
        receipt_key = ""
        if mutation:
            if not 8 <= len(idempotency_key) <= 128:
                raise HTTPException(400, "测试/静默/确认/重试命令需要8-128位 Idempotency-Key")
            receipt_key = "http-command:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
            if not await store.receipt(receipt_key, time.time() + 86400, time.time()):
                raise HTTPException(409, "相同幂等键已接收，请勿重复执行")
        try:
            try:
                async with asyncio.timeout(0.1):
                    await command_slots.acquire()
            except TimeoutError:
                if receipt_key:
                    await store.forget_receipt(receipt_key)
                raise HTTPException(429, "命令繁忙，请稍后重试") from None
            try:
                async with asyncio.timeout(settings.command_timeout):
                    if isinstance(commands, Commands):
                        result = await commands.execute(
                            payload.text,
                            actor="shared-service",
                            source="http",
                            request_id=idempotency_key,
                        )
                    else:
                        result = await commands.execute(payload.text)
            finally:
                command_slots.release()
        except TimeoutError:
            raise HTTPException(504, "命令超时，请稍后查看状态") from None
        return envelope(
            {"text": result.text, "has_image": result.image is not None, "ok": result.ok}
        )

    @app.get("/api/v1/reports/{day}/chart", dependencies=[Depends(service_key)])
    async def chart(day: date) -> Response:
        if commands is None:
            raise HTTPException(503, "报表服务未启动")
        try:
            image = await commands.reports.chart(day)
        except ValueError:
            raise HTTPException(400, "报表日期无效") from None
        return Response(image, media_type="image/png")

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        last = await store.get_meta("channels_last_ok")
        is_ready = last is not None and time.time() - float(last) < max(
            120, settings.poll_seconds * 3
        )
        return JSONResponse(envelope({"ready": is_ready}), status_code=200 if is_ready else 503)

    @app.get("/api/v1/schema", dependencies=[Depends(service_key)])
    async def schema() -> dict[str, Any]:
        return app.openapi()

    @app.post("/webhooks/newapi", status_code=202)
    async def newapi_event(request: Request) -> dict[str, Any]:
        if not settings.webhook_enabled:
            raise HTTPException(404, "未启用事件接收")
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 65536:
                raise HTTPException(413, "事件报文过大")
        body = bytes(chunks)
        try:
            event = NewAPIEvent.model_validate_json(body)
        except ValidationError:
            raise HTTPException(400, "事件结构无效") from None
        if not verify_signature(
            body,
            request.headers.get("X-Webhook-Signature", ""),
            str(event.timestamp),
            settings.webhook_secret.get_secret_value(),
            time.time(),
            settings.webhook_max_age,
        ):
            raise HTTPException(401, "签名无效或事件已过期")
        digest = hashlib.sha256(body).hexdigest()
        fresh = await store.accept_event(digest, event.type, time.time(), settings.webhook_max_age)
        return envelope({"duplicate": not fresh})

    return app
