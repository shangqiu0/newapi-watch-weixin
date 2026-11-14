"""Authenticated platform task endpoints. Parent router supplies session and CSRF."""

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query

from ..control_store import ControlError

if TYPE_CHECKING:
    from ..fleet import Fleet
from .jobs import JobInput, QueryJobs


def query_routes(fleet: "Fleet") -> APIRouter:
    router = APIRouter()

    def jobs(platform_id: str) -> QueryJobs:
        entry = fleet.entry(platform_id)
        if not entry.enabled:
            raise ControlError(409, "平台已停用，请先启用后查询")
        return entry.runtime.query_engine.jobs

    def wrap(data: Any) -> dict[str, Any]:
        return {"code": 0, "message": "success", "data": data}

    @router.get("/{platform_id}/query-tasks")
    async def listing(
        platform_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
    ) -> dict[str, Any]:
        return wrap(await jobs(platform_id).admin_page(page, page_size))

    @router.post("/{platform_id}/query-tasks", status_code=202)
    async def create(platform_id: str, payload: JobInput) -> dict[str, Any]:
        return wrap(await jobs(platform_id).create(payload, "console"))

    @router.get("/{platform_id}/query-tasks/{identifier}")
    async def detail(platform_id: str, identifier: str) -> dict[str, Any]:
        try:
            return wrap(await jobs(platform_id).admin_get(identifier))
        except ValueError:
            raise ControlError(404, "查询任务不存在") from None

    @router.delete("/{platform_id}/query-tasks/{identifier}")
    async def cancel(platform_id: str, identifier: str) -> dict[str, Any]:
        return wrap(await jobs(platform_id).admin_action(identifier, "cancel"))

    @router.post("/{platform_id}/query-tasks/{identifier}/retries")
    async def retry(platform_id: str, identifier: str) -> dict[str, Any]:
        return wrap(await jobs(platform_id).admin_action(identifier, "retry"))

    @router.get("/{platform_id}/query-status")
    async def status(platform_id: str) -> dict[str, Any]:
        engine = fleet.entry(platform_id).runtime.query_engine
        return wrap(
            {
                "cache": engine.cache.status(),
                "request_batch": engine.options.task_request_budget,
                "batch_seconds": engine.options.task_seconds_budget,
                "max_requests": engine.options.task_max_requests,
            }
        )

    return router
