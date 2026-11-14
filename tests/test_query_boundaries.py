import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from newapi_watch.client import NewAPIClient
from newapi_watch.config import Settings
from newapi_watch.query_engine.cache import QueryOptions
from newapi_watch.query_engine.jobs import JobInput, QueryJobs
from newapi_watch.store import Store


@asynccontextmanager
async def opened(tmp_path, upstream, **kwargs):
    conf = Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "boundary.db",
        newapi_url="https://up.invalid",
        newapi_token="private",
        newapi_user_id=1,
        request_attempts=1,
    )
    store = Store(conf.database)
    await store.open()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        try:
            yield QueryJobs(store, NewAPIClient(conf, http), conf.timezone, **kwargs)
        finally:
            await store.close()


def query():
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return JobInput(start=day, end=day)


async def test_cancel_during_http_does_not_publish_a_result(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def upstream(request):
        entered.set()
        await release.wait()
        return httpx.Response(
            200, json={"success": True, "data": {"items": [], "total": 0, "page_size": 100}}
        )

    async with opened(tmp_path, upstream) as jobs:
        task = await jobs.create(query(), "a")
        worker = asyncio.create_task(jobs.tick())
        await entered.wait()
        await jobs.cancel(task["id"], "a")
        release.set()
        await worker
        result = await jobs.get(task["id"], "a")
        assert result["status"] == "cancelled" and result["result"] is None


async def test_changed_credentials_cannot_resume_or_read_previous_query(tmp_path):
    async with opened(
        tmp_path,
        lambda _: httpx.Response(
            200, json={"success": True, "data": {"items": [], "total": 0, "page_size": 100}}
        ),
    ) as jobs:
        task = await jobs.create(query(), "a")
        changed = NewAPIClient(
            jobs.client.settings.model_copy(update={"newapi_token": SecretStr("new-token")}),
            jobs.client.http,
        )
        next_jobs = QueryJobs(jobs.store, changed, jobs.timezone)
        with pytest.raises(ValueError):
            await next_jobs.get(task["id"], "a")
        await next_jobs.tick()
        result = await jobs.admin_get(task["id"])
        assert result["status"] == "paused" and "令牌" in result["error"]


async def test_split_replaces_unconfirmed_rows_without_losing_completed_windows(tmp_path):
    initial = None

    def upstream(request):
        nonlocal initial
        lower = int(request.url.params["start_timestamp"])
        upper = int(request.url.params["end_timestamp"]) + 1
        page = int(request.url.params["p"])
        if initial is None:
            initial = (lower, upper)
        if (lower, upper) == initial:
            total = 3 if page == 1 else 4
            amount = 999
        else:
            total = 1
            amount = 10 if lower == initial[0] else 20
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": lower + page,
                            "created_at": lower,
                            "type": 2,
                            "model_name": "m",
                            "quota": amount,
                        }
                    ],
                    "total": total,
                    "page_size": 1,
                },
            },
        )

    async with opened(tmp_path, upstream, page_size=1) as jobs:
        task = await jobs.create(query(), "a")
        await jobs.tick()
        await jobs.tick()
        data = await jobs.get(task["id"], "a")
        assert data["status"] == "complete"
        assert data["result"]["summary"]["quota"] == 30 and data["result"]["records_read"] == 2


async def test_manual_continue_extends_budget_without_losing_checkpoint(tmp_path):
    def upstream(request):
        lower = int(request.url.params["start_timestamp"])
        page = int(request.url.params["p"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [{"id": page, "created_at": lower, "type": 2, "quota": 10}],
                    "total": 2,
                    "page_size": 1,
                },
            },
        )

    async with opened(
        tmp_path,
        upstream,
        page_size=1,
        options=QueryOptions(task_request_budget=1, task_max_requests=1),
    ) as jobs:
        task = await jobs.create(query(), "a")
        await jobs.tick()
        first = await jobs.get(task["id"], "a")
        assert first["status"] == "paused"
        await jobs.retry(task["id"], "a")
        await jobs.tick()
        result = await jobs.get(task["id"], "a")
        assert result["status"] == "complete" and result["result"]["summary"]["quota"] == 20


async def test_unknown_http_status_is_not_a_proven_zero_match(tmp_path):
    def upstream(request):
        lower = int(request.url.params["start_timestamp"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [{"id": 1, "created_at": lower, "type": 5, "channel": 1}],
                    "total": 1,
                    "page_size": 100,
                },
            },
        )

    async with opened(tmp_path, upstream) as jobs:
        request = query()
        request.status_codes = [500]
        request.metric = "errors"
        task = await jobs.create(request, "a")
        await jobs.tick()
        result = await jobs.get(task["id"], "a")
        assert result["result"]["unknown_status_records"] == 1
        assert (
            not result["result"]["filter_complete"] and not result["result"]["coverage"]["complete"]
        )


async def test_active_queue_is_bounded_and_old_results_can_be_pruned(tmp_path):
    from newapi_watch.operations import OperationError
    from newapi_watch.persistence import row

    async with opened(tmp_path, lambda _: httpx.Response(500)) as jobs:
        task = await jobs.create(query(), "old")
        await jobs.cancel(task["id"], "old")
        async with jobs.store.transaction() as db:
            await db.execute("UPDATE query_jobs SET updated_at=0 WHERE id=?", (task["id"],))
        await jobs.prune()
        async with jobs.store.transaction() as db:
            assert (await row(db, "SELECT COUNT(*) AS n FROM query_jobs"))["n"] == 0
        for i in range(50):
            await jobs.create(query(), str(i))
        with pytest.raises(OperationError) as error:
            await jobs.create(query(), "overflow")
        assert error.value.status == 429
