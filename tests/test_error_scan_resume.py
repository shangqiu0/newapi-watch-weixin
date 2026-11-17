"""Real paged scans with synthetic HTTP and SQLite, including exhausted budgets."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from test_policy_monitoring import setup

from newapi_watch.monitoring import engine as engine_module
from newapi_watch.persistence import row

BASE = 1_000_000


class BurstUpstream:
    def __init__(self, count=2998, *, same_second=False):
        self.clock = 0.0
        self.calls = []
        self.failure_page = 0
        self.failure_code = 503
        self.stall_page = 0
        self.release = asyncio.Event()
        self.records = [
            {
                "id": i + 1,
                "created_at": BASE + 100 if same_second else BASE + 1 + i * 299 // count,
                "type": 5,
                "channel": 7,
                "http_status": 500,
                "other": {"status_code": 500},
                "content": "synthetic failure",
            }
            for i in reversed(range(count))
        ]

    async def __call__(self, request):
        query = dict(request.url.params)
        self.calls.append(query)
        page, size = int(query["p"]), int(query["page_size"])
        if page == self.stall_page:
            await self.release.wait()
        if page == self.failure_page:
            return httpx.Response(self.failure_code)
        self.clock += 3
        selected = [
            r
            for r in self.records
            if int(query["start_timestamp"]) <= r["created_at"] <= int(query["end_timestamp"])
        ]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": selected[(page - 1) * size : page * size],
                    "total": len(selected),
                    "page_size": size,
                },
            },
        )


def config():
    return {
        "channels": {"enabled": False},
        "source": {"enabled": False},
        "errors": {
            "enabled": True,
            "poll_seconds": 30,
            "window_seconds": 300,
            "cooldown_seconds": 86400,
        },
    }


def virtual_budget(monkeypatch, upstream):
    monkeypatch.setattr(
        engine_module, "time", SimpleNamespace(monotonic=lambda: upstream.clock, time=time.time)
    )


async def seen_count(runtime):
    async with runtime.store.transaction() as db:
        return (await row(db, "SELECT COUNT(*) AS n FROM error_seen"))["n"]


@pytest.mark.parametrize("same_second", [False, True])
async def test_burst_budget_commits_every_page_then_resumes_frozen_window(
    tmp_path, monkeypatch, same_second
):
    upstream = BurstUpstream(same_second=same_second)
    virtual_budget(monkeypatch, upstream)
    runtime, http, service = await setup(tmp_path, upstream, config())
    try:
        await service.tick(now=BASE)
        await service.tick(now=BASE + 300)
        check = await service.engine.repo.check("errors")
        assert check["status"] == "pending" and check["failures"] == 0
        assert await runtime.store.get_meta("policy:error_cursor") == str(BASE)
        assert 0 < await seen_count(runtime) < 2998
        checkpoint = json.loads(await runtime.store.get_meta("policy:error_scan"))
        first_page = checkpoint["next_page"]
        first_calls = len(upstream.calls)
        assert first_page > 1
        # A new service/runtime must read durable progress, not process memory.
        await runtime.close()
        await http.aclose()
        runtime, http, service = await setup(tmp_path, upstream, config())
        for _ in range(4):
            await service.tick(now=BASE + 360, force=True)
            if await runtime.store.get_meta("policy:error_cursor") == str(BASE + 300):
                break
        assert int(upstream.calls[first_calls]["p"]) == first_page
        assert all(q["end_timestamp"] == str(BASE + 300) for q in upstream.calls)
        assert await seen_count(runtime) == 2998
        assert await runtime.store.get_meta("policy:error_cursor") == str(BASE + 300)
        assert await runtime.store.get_meta("policy:error_scan") is None
        assert len(await runtime.store.notifications()) == 1
        async with runtime.store.transaction() as db:
            pending = (await row(db, "SELECT SUM(pending_count) AS n FROM error_buckets"))["n"]
            assert pending + 100 == 2998  # first page alerted, remainder cooling down
        # Replaying overlap does not count the same error twice.
        await service.tick(now=BASE + 390, force=True)
        assert await seen_count(runtime) == 2998
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.parametrize("status", [503, 429])
async def test_failed_page_retains_prior_events_and_resumes_failed_page(
    tmp_path, monkeypatch, status
):
    upstream = BurstUpstream(450)
    upstream.failure_page, upstream.failure_code = 3, status
    virtual_budget(monkeypatch, upstream)
    runtime, http, service = await setup(tmp_path, upstream, config())
    try:
        await service.tick(now=BASE)
        await service.tick(now=BASE + 300)
        assert await seen_count(runtime) == 200
        assert await runtime.store.get_meta("policy:error_cursor") == str(BASE)
        assert json.loads(await runtime.store.get_meta("policy:error_scan"))["next_page"] == 3
        assert (await service.engine.repo.check("errors"))["status"] == "unknown"
        upstream.failure_page = 0
        runtime.client.throttle.blocked_until = 0
        first_calls = len(upstream.calls)
        await service.tick(now=BASE + 360, force=True)
        assert int(upstream.calls[first_calls]["p"]) == 3
        assert await seen_count(runtime) == 450
        assert await runtime.store.get_meta("policy:error_cursor") == str(BASE + 300)
        assert len(await runtime.store.notifications()) == 1
    finally:
        await runtime.close()
        await http.aclose()


async def test_hard_cancellation_cannot_discard_completed_pages(tmp_path, monkeypatch):
    upstream = BurstUpstream(450)
    upstream.stall_page = 3
    virtual_budget(monkeypatch, upstream)
    runtime, http, service = await setup(tmp_path, upstream, config())
    try:
        await service.tick(now=BASE)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await service.engine.errors(BASE + 300)
        assert await seen_count(runtime) == 200
        assert json.loads(await runtime.store.get_meta("policy:error_scan"))["next_page"] == 3
        upstream.release.set()
        await service.tick(now=BASE + 360, force=True)
        assert await seen_count(runtime) == 450
        assert await runtime.store.get_meta("policy:error_cursor") == str(BASE + 300)
    finally:
        await runtime.close()
        await http.aclose()


async def test_page_mutation_restarts_same_window_without_duplicate_alerts(tmp_path, monkeypatch):
    upstream = BurstUpstream(450)
    upstream.failure_page = 3
    virtual_budget(monkeypatch, upstream)
    runtime, http, service = await setup(tmp_path, upstream, config())
    try:
        await service.tick(now=BASE)
        await service.tick(now=BASE + 300)
        upstream.failure_page = 0
        # A delayed upstream insert changes the frozen window's pagination.
        upstream.records.insert(0, {**upstream.records[0], "id": 9999})
        await service.tick(now=BASE + 360, force=True)
        assert await runtime.store.get_meta("policy:error_cursor") == str(BASE)
        assert (await service.engine.repo.check("errors"))["status"] == "unknown"
        await service.tick(now=BASE + 390, force=True)
        assert await seen_count(runtime) == 451
        assert await runtime.store.get_meta("policy:error_cursor") == str(BASE + 300)
        assert len(await runtime.store.notifications()) == 1
    finally:
        await runtime.close()
        await http.aclose()


async def test_first_page_failure_still_freezes_scan_end(tmp_path, monkeypatch):
    upstream = BurstUpstream(450)
    upstream.failure_page = 1
    virtual_budget(monkeypatch, upstream)
    runtime, http, service = await setup(tmp_path, upstream, config())
    try:
        await service.tick(now=BASE)
        await service.tick(now=BASE + 100)
        saved = await runtime.store.get_meta("policy:error_scan")
        assert saved is not None
        assert json.loads(saved)["end"] == BASE + 100
        upstream.failure_page = 0
        await service.tick(now=BASE + 360, force=True)
        assert upstream.calls[1]["end_timestamp"] == str(BASE + 100)
        assert await runtime.store.get_meta("policy:error_cursor") == str(BASE + 100)
    finally:
        await runtime.close()
        await http.aclose()


async def test_checkpoint_storage_failure_rolls_back_page_events_and_outbox(tmp_path, monkeypatch):
    upstream = BurstUpstream(450)
    virtual_budget(monkeypatch, upstream)
    runtime, http, service = await setup(tmp_path, upstream, config())
    try:
        await service.tick(now=BASE)
        async with runtime.store.transaction() as db:
            await db.execute("""CREATE TRIGGER reject_checkpoint BEFORE INSERT ON metadata
                WHEN NEW.id='policy:error_scan' AND json_extract(NEW.value,'$.next_page')>1
                BEGIN SELECT RAISE(ABORT,'injected checkpoint write failure'); END""")
        await service.tick(now=BASE + 300)
        assert await seen_count(runtime) == 0
        assert len(await runtime.store.notifications()) == 0
        assert json.loads(await runtime.store.get_meta("policy:error_scan"))["next_page"] == 1
        async with runtime.store.transaction() as db:
            await db.execute("DROP TRIGGER reject_checkpoint")
        await service.tick(now=BASE + 360, force=True)
        assert await seen_count(runtime) == 450
        assert len(await runtime.store.notifications()) == 1
    finally:
        await runtime.close()
        await http.aclose()


async def test_partial_progress_does_not_resolve_existing_source_incident(tmp_path, monkeypatch):
    upstream = BurstUpstream()
    upstream.failure_page = 1
    virtual_budget(monkeypatch, upstream)
    cfg = config()
    cfg["source"] = {"enabled": True, "confirmations": 1}
    runtime, http, service = await setup(tmp_path, upstream, cfg)
    try:
        await service.tick(now=BASE)
        await service.tick(now=BASE + 300)
        upstream.failure_page = 0
        await service.tick(now=BASE + 360, force=True)
        assert (await service.engine.repo.check("errors"))["status"] == "pending"
        states = await service.engine.repo.states()
        assert next(s for s in states if s["id"] == "policy:source:errors")["is_active"] == 1
        assert not any("监控检查恢复" in n["content"] for n in await runtime.store.notifications())
        await service.tick(now=BASE + 390, force=True)
        await service.tick(now=BASE + 420, force=True)
        assert any("监控检查恢复" in n["content"] for n in await runtime.store.notifications())
    finally:
        await runtime.close()
        await http.aclose()
