"""Authenticated local-only data lifecycle APIs; parent supplies auth and CSRF."""

import json
import time
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .control_store import ControlStore
from .fleet import Fleet
from .persistence import row
from .retention import CleanupSelection, RetentionService


class CleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(pattern=r"^clean_[a-f0-9]{32}$")


class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(min_length=1, max_length=100)


def retention_routes(control: ControlStore, fleet: Fleet) -> APIRouter:
    router = APIRouter()

    def wrap(data: Any) -> dict[str, Any]:
        return {"code": 0, "message": "success", "data": data}

    @router.get("/{platform_id}/data-management")
    async def status(platform_id: str) -> dict[str, Any]:
        config = await control.platform_config(platform_id)
        runtime = fleet.entry(platform_id).runtime
        async with runtime.store.transaction() as db:
            # Fixed queries, not a client-selectable table name.
            counts = {}
            for key, table in (
                ("logs", "logs"),
                ("notifications", "notifications"),
                ("tasks", "query_jobs"),
                ("audits", "command_audits"),
            ):
                count = await row(db, f"SELECT COUNT(*) AS n FROM {table}")
                counts[key] = count["n"] if count else 0
        last = await runtime.store.get_meta("retention:last_run")
        return wrap(
            {
                "config": config.retention.model_dump(),
                "counts": counts,
                "last_run": json.loads(last) if last else None,
                "next_check_at": float(await runtime.store.get_meta("retention:next_at") or 0)
                or None,
                "scope": "local_only",
                "batch_limit": 100,
            }
        )

    @router.post("/{platform_id}/cleanup-previews")
    async def preview(platform_id: str, payload: CleanupSelection) -> dict[str, Any]:
        return wrap(await RetentionService(fleet.entry(platform_id).runtime.store).preview(payload))

    @router.post("/{platform_id}/cleanups")
    async def execute(platform_id: str, payload: CleanupInput) -> dict[str, Any]:
        runtime = fleet.entry(platform_id).runtime
        async with runtime.query_engine.jobs.lock:
            return wrap(await RetentionService(runtime.store).execute(payload.preview_id))

    @router.post("/{platform_id}/notification-cancellations")
    async def cancel(platform_id: str, payload: CancelInput) -> dict[str, Any]:
        runtime = fleet.entry(platform_id).runtime
        cancelled = 0
        async with runtime.store.transaction() as db:
            for identifier in set(payload.ids):
                cursor = await db.execute(
                    "UPDATE notifications SET status='cancelled',lease_until=0,last_error='管理员取消投递，未发送',updated_at=? WHERE id=? AND status IN ('pending','dead') AND lease_until=0",
                    (time.time(), identifier),
                )
                cancelled += max(0, cursor.rowcount)
        return wrap({"cancelled": cancelled, "skipped": len(set(payload.ids)) - cancelled})

    @router.get("/{platform_id}/operation-records")
    async def operations(
        platform_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
    ) -> dict[str, Any]:
        await control.platform(platform_id)
        return wrap(await control.operation_records(platform_id, page=page, page_size=page_size))

    return router
