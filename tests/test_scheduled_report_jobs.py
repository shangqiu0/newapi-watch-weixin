"""Regression: report pagination must resume, not restart every 45 seconds and spam WeCom."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from newapi_watch.config import Settings
from newapi_watch.monitoring.models import MonitoringConfig
from newapi_watch.monitoring.service import MonitoringService
from newapi_watch.persistence import row
from newapi_watch.runtime import Runtime

NOW = datetime(2026, 11, 13, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
STAMP = int((NOW - timedelta(days=1)).timestamp())


def configuration(**changes):
    return MonitoringConfig.model_validate(
        {
            "channels": {"enabled": False},
            "daily": {"enabled": True, "with_image": False, "pages_per_run": 1, **changes},
            "source": {"confirmations": 1},
        }
    )


async def open_runtime(tmp_path, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime = Runtime(
        Settings(
            _env_file=None,
            demo=True,
            database=tmp_path / "report.sqlite3",
            request_attempts=1,
            retry_seconds=0,
        ),
        http=http,
    )
    await runtime.open(start_tasks=False)
    return runtime, http


def page_response(items, total):
    return httpx.Response(
        200, json={"success": True, "data": {"items": items, "total": total, "page_size": 1}}
    )


async def test_report_resumes_page_checkpoint_after_service_restart_without_raw_log_storage(
    tmp_path,
):
    pages = []

    def upstream(request):
        page = int(request.url.params["p"])
        pages.append(page)
        return page_response(
            [
                {
                    "id": page,
                    "type": 2,
                    "created_at": STAMP,
                    "channel": 93,
                    "model_name": "m",
                    "quota": page * 10,
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                }
            ],
            3,
        )

    runtime, http = await open_runtime(tmp_path, upstream)
    try:
        first = MonitoringService(runtime, configuration())
        await first.tick(now=NOW.timestamp())
        state = (await first.snapshot(now=NOW.timestamp()))["checks"][2]
        assert pages == [1] and state["status"] == "pending"
        assert (
            state["detail"]["report_stage"] == "collecting"
            and state["detail"]["records_fetched"] == 1
        )
        assert not await runtime.store.notifications()
        # Same clock tick cannot run the next batch; a fresh service keeps the persistent page checkpoint.
        await first.tick(now=NOW.timestamp())
        await MonitoringService(runtime, configuration()).tick(now=NOW.timestamp() + 60)
        await MonitoringService(runtime, configuration()).tick(now=NOW.timestamp() + 120)
        assert pages == [1, 2, 3]
        notices = await runtime.store.notifications()
        assert len(notices) == 1 and "60 quota" in notices[0]["content"]
        assert "消费记录：3" in notices[0]["content"] and "Token：9" in notices[0]["content"]
        async with runtime.store.transaction() as db:
            assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 0
            checkpoint = await row(
                db, "SELECT value FROM metadata WHERE id LIKE 'report_job:daily:%'"
            )
            assert (
                "error_message" not in checkpoint["value"] and "username" not in checkpoint["value"]
            )
        await MonitoringService(runtime, configuration()).tick(now=NOW.timestamp() + 180)
        assert pages == [1, 2, 3] and len(await runtime.store.notifications()) == 1
    finally:
        await runtime.close()
        await http.aclose()


async def test_report_partial_progress_and_retry_exhaustion_do_not_generate_repeated_source_notices(
    tmp_path,
):
    pages = []

    def upstream(request):
        page = int(request.url.params["p"])
        pages.append(page)
        return (
            page_response([{"id": 1, "type": 2, "created_at": STAMP, "quota": 10}], 2)
            if page == 1
            else httpx.Response(503)
        )

    runtime, http = await open_runtime(tmp_path, upstream)
    try:
        config = configuration(max_failures=2, retry_seconds=60)
        service = MonitoringService(runtime, config)
        for seconds in (0, 60, 120, 180, 3600, 7200):
            await service.tick(now=NOW.timestamp() + seconds)
        assert pages == [1, 2, 2]  # no restart from page1; bounded actual failed attempts
        notifications = await runtime.store.notifications()
        assert len(notifications) == 1 and "日报生成失败" in notifications[0]["content"]
        assert "业务状态未知" not in notifications[0]["content"]
        check = next(c for c in (await service.snapshot())["checks"] if c["id"] == "daily")
        assert check["status"] == "unknown" and check["detail"]["report_stage"] == "paused"
        assert check["detail"]["records_fetched"] == 1
        await service.tick(now=NOW.timestamp() + 7500, force=True)
        assert (
            pages[-1] == 2 and len(pages) == 4
        )  # explicit manual recheck may retry, background does not
    finally:
        await runtime.close()
        await http.aclose()


async def test_repeated_service_error_notifies_once_until_recovery_by_default(tmp_path):
    failure = True

    def upstream(request):
        return httpx.Response(503) if failure else page_response([], 0)

    runtime, http = await open_runtime(tmp_path, upstream)
    try:
        config = MonitoringConfig.model_validate(
            {
                "channels": {"enabled": True, "poll_seconds": 30},
                "source": {"confirmations": 1, "cooldown_seconds": 30},
            }
        )
        service = MonitoringService(runtime, config)
        await service.tick(now=1000)
        await service.tick(now=2000)
        await service.tick(now=3000)
        assert len(await runtime.store.notifications()) == 1
        failure = False
        await service.tick(now=5000)
        assert (
            len(await runtime.store.notifications()) == 2
            and "恢复" in (await runtime.store.notifications())[-1]["content"]
        )
    finally:
        await runtime.close()
        await http.aclose()


async def test_report_rejects_changed_pagination_total_instead_of_sending_partial_statistics(
    tmp_path,
):
    def upstream(request):
        page = int(request.url.params["p"])
        return page_response([{"id": page, "created_at": STAMP, "type": 2}], 2 if page == 1 else 3)

    runtime, http = await open_runtime(tmp_path, upstream)
    try:
        service = MonitoringService(runtime, configuration())
        await service.tick(now=NOW.timestamp())
        await service.tick(now=NOW.timestamp() + 60)
        async with runtime.store.transaction() as db:
            assert (await row(db, "SELECT COUNT(*) AS n FROM notifications WHERE kind='daily'"))[
                "n"
            ] == 0
        check = next(c for c in (await service.snapshot())["checks"] if c["id"] == "daily")
        assert check["status"] == "unknown" and "分页" in check["error"]
    finally:
        await runtime.close()
        await http.aclose()


async def test_completed_report_retries_only_chart_not_all_pages(tmp_path, monkeypatch):
    pages, renders = [], []

    def upstream(request):
        pages.append(int(request.url.params["p"]))
        return page_response([{"id": 1, "type": 2, "created_at": STAMP, "quota": 10}], 1)

    async def render(snapshot, kind):
        renders.append(kind)
        if len(renders) == 1:
            raise ValueError("temporary chart failure")
        return b"png-test"

    runtime, http = await open_runtime(tmp_path, upstream)
    monkeypatch.setattr(runtime.reports, "_image", render)
    try:
        service = MonitoringService(runtime, configuration(with_image=True, retry_seconds=60))
        await service.tick(now=NOW.timestamp())
        await service.tick(now=NOW.timestamp() + 60)
        assert pages == [1] and renders == ["trend", "trend"]
        async with runtime.store.transaction() as db:
            assert (await row(db, "SELECT COUNT(*) AS n FROM notifications WHERE kind='daily'"))[
                "n"
            ] == 1
    finally:
        await runtime.close()
        await http.aclose()


async def test_page_limit_cannot_stop_monitoring_other_sources(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        if request.url.path == "/api/channel/":
            return page_response([], 0)
        page = int(request.url.params["p"])
        return page_response([{"id": page, "created_at": STAMP, "type": 2}], 10000)

    runtime, http = await open_runtime(tmp_path, upstream)
    try:
        config = configuration(pages_per_run=2)
        config.channels.enabled = True
        service = MonitoringService(runtime, config)
        await service.tick(now=NOW.timestamp())
        assert calls.count("/api/log/") == 2 and calls.count("/api/channel/") == 1
        await service.tick(now=NOW.timestamp() + 60)
        assert calls.count("/api/log/") == 4 and calls.count("/api/channel/") == 2
        assert not await runtime.store.notifications()
    finally:
        await runtime.close()
        await http.aclose()


async def test_report_restart_migration_stops_only_legacy_pending_report_failure_messages(tmp_path):
    from newapi_watch.store import Store

    path = tmp_path / "migration.sqlite3"
    store = Store(path)
    await store.open()
    try:
        await store.enqueue("policy:policy:source:daily:1:1000", "old report failure", 1000)
        await store.enqueue(
            "policy:policy:source:channels:1:1000", "genuine channel check failure", 1000
        )
        async with store.transaction() as db:
            await db.execute("UPDATE notifications SET kind='service' WHERE id IN (1,2)")
    finally:
        await store.close()
    runtime = Runtime(Settings(_env_file=None, demo=True, database=path))
    await runtime.open(start_tasks=False, monitoring=configuration())
    try:
        items = await runtime.store.notifications()
        assert items[0]["status"] == "historical" and items[1]["status"] == "pending"
        assert "未实际补发" in items[0]["last_error"]
    finally:
        await runtime.close()


async def test_cancelled_report_preserves_last_complete_page_and_does_not_emit_failure(tmp_path):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    pages = []
    block = True

    async def upstream(request):
        page = int(request.url.params["p"])
        pages.append(page)
        if page == 2 and block:
            entered.set()
            await release.wait()
        return page_response([{"id": page, "created_at": STAMP, "type": 2, "quota": page}], 2)

    runtime, http = await open_runtime(tmp_path, upstream)
    try:
        service = MonitoringService(runtime, configuration(pages_per_run=5))
        task = asyncio.create_task(service.tick(now=NOW.timestamp()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await runtime.store.notifications()
        block = False
        await MonitoringService(runtime, configuration()).tick(now=NOW.timestamp() + 60)
        assert pages == [1, 2, 2]
        assert "3 quota" in (await runtime.store.notifications())[0]["content"]
    finally:
        release.set()
        await runtime.close()
        await http.aclose()


async def test_report_failed_page_budget_is_retried_without_losing_aggregate(tmp_path):
    pages = []

    def upstream(request):
        page = int(request.url.params["p"])
        pages.append(page)
        if page == 2 and pages.count(2) == 1:
            raise TimeoutError()
        return page_response([{"id": page, "created_at": STAMP, "type": 2, "quota": page}], 2)

    runtime, http = await open_runtime(tmp_path, upstream)
    try:
        service = MonitoringService(runtime, configuration(retry_seconds=60))
        for offset in (0, 60, 120):
            await service.tick(now=NOW.timestamp() + offset)
        assert pages == [1, 2, 2]
        async with runtime.store.transaction() as db:
            assert (await row(db, "SELECT COUNT(*) AS n FROM notifications WHERE kind='daily'"))[
                "n"
            ] == 1
            record = await row(db, "SELECT content FROM notifications WHERE kind='daily'")
            assert "3 quota" in record["content"]
    finally:
        await runtime.close()
        await http.aclose()
