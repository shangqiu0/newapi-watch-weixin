"""v0.2 HTTP resources, all protected by the same service credential."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from fastapi.responses import Response
from pydantic import ValidationError

from .audit import Audit, AuditRecord
from .config import Settings
from .operations import Operations
from .periods import StatisticsQuery
from .plotting import ChartKind
from .redaction import redact_data
from .reports import Reports


def statistics_query(
    start: date,
    end: date,
    dimension: Literal["channel", "model", "user"] = "model",
    metric: Literal["quota", "calls", "errors", "tokens"] = "quota",
    limit: int = Query(default=10, ge=1, le=20),
    channel_id: int = Query(default=0, ge=0),
) -> StatisticsQuery:
    try:
        return StatisticsQuery(
            start=start,
            end=end,
            dimension=dimension,
            metric=metric,
            limit=limit,
            channel_id=channel_id,
        )
    except ValidationError:
        raise HTTPException(400, "日期范围应为1至31天，且开始不能晚于结束") from None


def management_router(
    reports: Reports,
    ops: Operations,
    audits: Audit,
    service_key: Callable[..., Awaitable[None]],
    settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(service_key)])

    async def tracked(
        action: str, operation: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        start = time.monotonic()
        outcome = "success"
        try:
            return await operation()
        except ValueError:
            outcome = "conflict"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            await audits.record(
                AuditRecord.model_validate(
                    {
                        "source": "http",
                        "action": action,
                        "actor_hash": audits.fingerprint("shared-service"),
                        "outcome": outcome,
                        "duration_ms": max(0, int((time.monotonic() - start) * 1000)),
                    }
                )
            )

    @router.get("/statistics")
    async def statistics(
        query: Annotated[StatisticsQuery, Depends(statistics_query)],
    ) -> dict[str, Any]:
        try:
            snapshot = await reports.analytics.snapshot(query)
        except ValueError:
            raise HTTPException(400, "统计日期无效，不能包含未来日期") from None
        return {
            "code": 0,
            "data": redact_data(snapshot.model_dump(mode="json"), settings),
            "message": "success",
        }

    @router.get("/statistics/chart")
    async def chart(
        query: Annotated[StatisticsQuery, Depends(statistics_query)], kind: ChartKind = "trend"
    ) -> Response:
        try:
            image = await reports.range_chart(query, kind=kind)
        except ValueError:
            raise HTTPException(400, "统计日期无效，不能包含未来日期") from None
        return Response(image, media_type="image/png")

    @router.get("/silences")
    async def silences(
        page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)
    ) -> dict[str, Any]:
        return {
            "code": 0,
            "data": await ops.silences(page=page, page_size=page_size),
            "message": "success",
        }

    @router.get("/notifications/dead")
    async def dead(
        page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)
    ) -> dict[str, Any]:
        return {
            "code": 0,
            "data": await ops.dead_letters(page=page, page_size=page_size),
            "message": "success",
        }

    @router.get("/audits")
    async def history(
        page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)
    ) -> dict[str, Any]:
        return {
            "code": 0,
            "data": await audits.list(page=page, page_size=page_size),
            "message": "success",
        }

    @router.put("/alerts/{alert_id}/acknowledgement")
    async def acknowledge(alert_id: str = Path(pattern=r"^[a-fA-F0-9]{32}$")) -> dict[str, Any]:
        result = await tracked(
            "alerts",
            lambda: ops.acknowledge(alert_id, actor_hash=audits.fingerprint("shared-service")),
        )
        return {"code": 0, "data": result, "message": "success"}

    @router.post("/notifications/{notification_id}/retries")
    async def retry(
        notification_id: int = Path(gt=0),
        idempotency_key: str = Header(min_length=8, max_length=128),
    ) -> dict[str, Any]:
        result = await tracked("notifications", lambda: ops.retry(notification_id, idempotency_key))
        return {"code": 0, "data": result, "message": "success"}

    return router
