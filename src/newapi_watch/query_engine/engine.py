"""Interactive router: aggregate reads first; exact work never blocks a chat reply."""

import asyncio
import hashlib
from typing import Any

from ..client import NewAPIClient
from ..periods import StatisticsQuery
from ..redaction import redact_data
from ..store import Store
from .aggregates import AggregateReader, AggregateUnavailable
from .cache import QueryCache, QueryOptions
from .jobs import JobInput, QueryJobs


class QueryEngine:
    def __init__(
        self, store: Store, client: NewAPIClient, *, options: QueryOptions | None = None
    ) -> None:
        self.options = options or QueryOptions()
        self.client = client
        scope = (
            str(store.path.resolve())
            + client.settings.newapi_url
            + str(client.settings.newapi_user_id)
            + client.settings.newapi_token.get_secret_value()
        )
        self.scope = hashlib.sha256(scope.encode()).hexdigest()
        self.cache = QueryCache.configured(self.scope, self.options)
        self.aggregate_lock = asyncio.Lock()
        self.reader = AggregateReader(client)
        self.jobs = QueryJobs(store, client, client.settings.timezone, options=self.options)

    async def statistics(
        self,
        query: StatisticsQuery,
        owner: str,
        *,
        exact: bool = False,
        context: dict[str, str] | None = None,
        status_codes: list[int] | None = None,
        allow_tasks: bool = True,
    ) -> dict[str, Any]:
        key = query.model_dump_json()
        if not exact and not status_codes:
            try:
                async with asyncio.timeout(12):
                    async with self.aggregate_lock:
                        cached = await self.cache.get(key)
                        if cached:
                            return {**cached, "cache_hit": True}
                        result = await self.reader.snapshot(query)
                        safe: dict[str, Any] = redact_data(result, self.client.settings)
                        await self.cache.set(key, safe)
                        return safe
            except (AggregateUnavailable, TimeoutError):
                pass
        if not allow_tasks:
            return {
                "requires_task": True,
                "query": query.model_dump(mode="json"),
                "source": "query_planner",
                "limitation": "此查询需要后台精确任务，尚未创建。请通过POST statistics-queries或query-tasks提交。",
            }
        request = JobInput(**query.model_dump(), status_codes=status_codes or [])
        job = await self.jobs.create(request, owner, context=context)
        if job["status"] == "complete" and job.get("result"):
            completed: dict[str, Any] = job["result"]
            return completed
        return {
            "pending": True,
            "task": job,
            "source": "query_task",
            "limitation": "精确查询已受理并保存冻结时间窗；尚未完成，不可将已有进度当作完整统计。可使用“任务 "
            + job["id"]
            + "”查询。",
        }

    async def close(self) -> None:
        await self.cache.close()
