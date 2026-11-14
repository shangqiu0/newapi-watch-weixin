import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from newapi_watch.client import NewAPIClient
from newapi_watch.config import Settings
from newapi_watch.store import Store


def settings(tmp_path):
    return Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "jobs.db",
        newapi_url="https://up.invalid",
        newapi_token="secret",
        newapi_user_id=1,
        request_attempts=1,
    )


def input_query():
    from newapi_watch.query_engine.jobs import JobInput

    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return JobInput(start=day, end=day)


async def test_exact_job_checkpoints_restart_and_keeps_raw_log_table_empty(tmp_path):
    from newapi_watch.persistence import row
    from newapi_watch.query_engine.cache import QueryOptions
    from newapi_watch.query_engine.jobs import QueryJobs

    calls = []

    def upstream(request):
        calls.append(dict(request.url.params))
        start = int(request.url.params["start_timestamp"])
        page = int(request.url.params["p"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": start + page,
                            "created_at": start,
                            "type": 2,
                            "quota": page * 10,
                            "model_name": "m",
                            "channel": 1,
                            "user_id": 1,
                        }
                    ],
                    "total": 2,
                    "page_size": 1,
                },
            },
        )

    conf = settings(tmp_path)
    store = Store(conf.database)
    await store.open()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        client = NewAPIClient(conf, http)
        jobs = QueryJobs(
            store,
            client,
            conf.timezone,
            page_size=1,
            options=QueryOptions(_env_file=None, task_request_budget=1),
        )
        job = await jobs.create(input_query(), "owner-a")
        await jobs.tick()
        first = await jobs.get(job["id"], "owner-a")
        assert first["progress"]["records_read"] == 1 and first["status"] == "queued"
        await store.close()
        await store.open()
        jobs = QueryJobs(
            store,
            client,
            conf.timezone,
            page_size=1,
            options=QueryOptions(_env_file=None, task_request_budget=1),
        )
        await jobs.tick()
        after = await jobs.get(job["id"], "owner-a")
        assert (
            after["result"]["records_read"] == 2
            and after["status"] == "complete"
            and calls[1]["p"] == "2"
        )
        async with store.transaction() as db:
            assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 0
    await store.close()


async def test_task_dedupe_owner_scope_cancel_and_retry_are_atomic(tmp_path):
    from newapi_watch.query_engine.jobs import QueryJobs

    store = Store(settings(tmp_path).database)
    await store.open()
    async with httpx.AsyncClient() as http:
        jobs = QueryJobs(store, NewAPIClient(settings(tmp_path), http), "Asia/Shanghai")
        a, b = await __import__("asyncio").gather(
            jobs.create(input_query(), "a"), jobs.create(input_query(), "a")
        )
        assert a["id"] == b["id"]
        other = await jobs.create(input_query(), "b")
        assert other["id"] != a["id"]
        with pytest.raises(ValueError, match="不存在"):
            await jobs.get(a["id"], "b")
        await jobs.cancel(a["id"], "a")
        assert (await jobs.get(a["id"], "a"))["status"] == "cancelled"
    await store.close()


async def test_429_preserves_progress_and_schedules_after_cooldown(tmp_path):
    from newapi_watch.query_engine.jobs import QueryJobs

    store = Store(settings(tmp_path).database)
    await store.open()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "120"}))
    ) as http:
        jobs = QueryJobs(store, NewAPIClient(settings(tmp_path), http), "Asia/Shanghai")
        job = await jobs.create(input_query(), "a")
        await jobs.tick()
        current = await jobs.get(job["id"], "a")
        assert current["status"] == "waiting" and current["progress"]["records_read"] == 0
        assert current["next_at"] >= time.time() + 110
    await store.close()


async def test_changed_total_splits_uncommitted_window_not_double_counts(tmp_path):
    from newapi_watch.query_engine.jobs import QueryJobs

    store = Store(settings(tmp_path).database)
    await store.open()

    def upstream(request):
        start = int(request.url.params["start_timestamp"])
        page = int(request.url.params["p"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [{"id": start + page, "created_at": start, "type": 2, "quota": 10}],
                    "total": 2 if page == 1 else 3,
                    "page_size": 1,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        jobs = QueryJobs(
            store, NewAPIClient(settings(tmp_path), http), "Asia/Shanghai", page_size=1
        )
        job = await jobs.create(input_query(), "a")
        await jobs.tick()
        await jobs.tick()
        current = await jobs.get(job["id"], "a")
        assert current["progress"]["restarts"] >= 1 and current["status"] == "queued"
        assert current["progress"]["window_records"] == 0
    await store.close()


async def test_budget_pauses_instead_of_publishing_partial_result(tmp_path):
    from newapi_watch.query_engine.cache import QueryOptions
    from newapi_watch.query_engine.jobs import QueryJobs

    store = Store(settings(tmp_path).database)
    await store.open()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"success": True, "data": {"items": [], "total": 0, "page_size": 100}}
            )
        )
    ) as http:
        jobs = QueryJobs(
            store,
            NewAPIClient(settings(tmp_path), http),
            "Asia/Shanghai",
            options=QueryOptions(_env_file=None, task_request_budget=1, task_max_requests=1),
            page_size=1,
        )
        job = await jobs.create(input_query(), "a")
        await jobs.tick()
        current = await jobs.get(job["id"], "a")
        assert current["status"] == "paused" and current["result"] is None
        await jobs.retry(job["id"], "a")
        assert (await jobs.get(job["id"], "a"))["status"] == "queued"
    await store.close()


async def test_exact_user_ranking_counts_users_and_does_not_relabel_models(tmp_path):
    from newapi_watch.query_engine.jobs import QueryJobs

    conf = settings(tmp_path)
    store = Store(conf.database)
    await store.open()
    stamp = int(time.time()) - 1
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "id": 1,
                                "created_at": stamp,
                                "type": 2,
                                "user_id": 8,
                                "username": "alice",
                                "model_name": "m",
                                "channel": 2,
                                "quota": 100,
                            },
                            {
                                "id": 2,
                                "created_at": stamp,
                                "type": 2,
                                "user_id": 9,
                                "username": "bob",
                                "model_name": "m",
                                "channel": 2,
                                "quota": 30,
                            },
                        ],
                        "total": 2,
                        "page_size": 100,
                    },
                },
            )
        )
    ) as http:
        jobs = QueryJobs(store, NewAPIClient(conf, http), conf.timezone)
        query = input_query()
        query.dimension = "user"
        job = await jobs.create(query, "a")
        await jobs.tick()
        data = await jobs.get(job["id"], "a")
        assert data["result"]["ranking"][0]["key"] == "8"
        assert data["result"]["ranking"][0]["quota"] == 100
    await store.close()


async def test_checkpoint_saved_after_each_page_even_if_next_page_fails(tmp_path):
    from newapi_watch.query_engine.jobs import QueryJobs

    conf = settings(tmp_path)
    store = Store(conf.database)
    await store.open()

    def upstream(request):
        if request.url.params["p"] == "2":
            return httpx.Response(500)
        start = int(request.url.params["start_timestamp"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [{"id": 1, "created_at": start, "type": 2, "quota": 10}],
                    "total": 2,
                    "page_size": 1,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        jobs = QueryJobs(store, NewAPIClient(conf, http), conf.timezone, page_size=1)
        job = await jobs.create(input_query(), "a")
        await jobs.tick()
        data = await jobs.get(job["id"], "a")
        assert data["progress"]["records_read"] == 1 and data["progress"]["page"] == 2
        assert data["status"] == "paused"
    await store.close()
