"""Behavioral tests use real engine/SQLite and fake only the external HTTP boundary."""

import asyncio

import httpx

from newapi_watch.config import Settings
from newapi_watch.models import LogRecord
from newapi_watch.monitoring.models import MonitoringConfig
from newapi_watch.monitoring.service import MonitoringService
from newapi_watch.persistence import row
from newapi_watch.runtime import Runtime


async def setup(tmp_path, upstream, config):
    validated = MonitoringConfig.model_validate(config)
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    runtime = Runtime(
        Settings(
            _env_file=None,
            demo=True,
            database=tmp_path / "watch.sqlite3",
            request_attempts=1,
            retry_seconds=0,
        ),
        http=http,
    )
    await runtime.open(start_tasks=False)
    service = MonitoringService(runtime, validated)
    runtime.policies = service
    return runtime, http, service


def reply(items):
    return httpx.Response(
        200, json={"success": True, "data": {"items": items, "total": len(items)}}
    )


async def test_policy_tick_only_reads_enabled_sources_and_never_mirrors_logs(tmp_path):
    seen = []

    def upstream(request):
        seen.append(request.url.path)
        if request.url.path == "/api/channel/":
            return reply([{"id": 1, "status": 2, "name": "manual"}, {"id": 2, "status": 3}])
        return reply([])

    runtime, http, service = await setup(tmp_path, upstream, {"channels": {"enabled": True}})
    try:
        await service.tick(now=1000)
        assert seen == ["/api/channel/"]
        assert len(await runtime.store.notifications()) == 1
        assert "#2" in (await runtime.store.notifications())[0]["content"]
        async with runtime.store.transaction() as db:
            assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 0
    finally:
        await runtime.close()
        await http.aclose()


async def test_error_first_start_baseline_incremental_window_and_restart_dedup(tmp_path):
    requests = []

    def upstream(request):
        requests.append(dict(request.url.params))
        return reply(
            [
                {
                    "id": 10,
                    "created_at": 1005,
                    "type": 5,
                    "channel": 93,
                    "http_status": 400,
                    "other": {"status_code": 400},
                    "content": "bad",
                }
            ]
        )

    config = {"channels": {"enabled": False}, "errors": {"enabled": True, "poll_seconds": 30}}
    runtime, http, service = await setup(tmp_path, upstream, config)
    try:
        await service.tick(now=1000)
        assert requests == []
        await service.tick(now=1030)
        assert requests[0]["type"] == "5"
        assert requests[0]["start_timestamp"] == "970"
        assert len(await runtime.store.notifications()) == 1
        service = MonitoringService(runtime, MonitoringConfig.model_validate(config))
        await service.tick(now=1060)
        assert len(await runtime.store.notifications()) == 1
        async with runtime.store.transaction() as db:
            assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 0
        assert (await service.snapshot(now=1060))["checks"][1]["status"] == "ok"
    finally:
        await runtime.close()
        await http.aclose()


async def test_partial_pages_do_not_advance_cursor_and_are_unknown_not_healthy(tmp_path):
    failure = False

    def upstream(request):
        if failure:
            return httpx.Response(429)
        return reply([])

    runtime, http, service = await setup(
        tmp_path,
        upstream,
        {"channels": {"enabled": False}, "errors": {"enabled": True, "poll_seconds": 30}},
    )
    try:
        await service.tick(now=1000)
        failure = True
        await service.tick(now=1030)
        assert await runtime.store.get_meta("policy:error_cursor") == "1000"
        checks = (await service.snapshot(now=1030))["checks"]
        log_check = next(c for c in checks if c["id"] == "errors")
        assert log_check["status"] == "unknown" and "429" in log_check["error"]
        assert log_check["next_at"] > 1030
    finally:
        await runtime.close()
        await http.aclose()


async def test_channels_confirmation_manual_disable_and_recovery_are_distinct(tmp_path):
    status = 3

    def upstream(request):
        return reply([{"id": 8, "name": "test", "status": status}])

    runtime, http, service = await setup(
        tmp_path, upstream, {"channels": {"enabled": True, "poll_seconds": 30, "confirmations": 2}}
    )
    try:
        await service.tick(now=1000)
        assert not await runtime.store.notifications()
        await service.tick(now=1030)
        assert len(await runtime.store.notifications()) == 1
        status = 2
        await service.tick(now=1060)
        assert (
            len(await runtime.store.notifications()) == 1
        )  # manual disable is not proof of recovery
        status = 1
        await service.tick(now=1090)
        assert len(await runtime.store.notifications()) == 2
        assert "恢复" in (await runtime.store.notifications())[1]["content"]
    finally:
        await runtime.close()
        await http.aclose()


async def test_user_failures_do_not_zero_balance_or_resolve_low_state(tmp_path):
    quota = 20
    failed = False

    def upstream(request):
        return (
            httpx.Response(403)
            if failed
            else httpx.Response(200, json={"success": True, "data": {"id": 5, "quota": quota}})
        )

    runtime, http, service = await setup(
        tmp_path,
        upstream,
        {
            "channels": {"enabled": False},
            "balances": [{"user_id": 5, "threshold": 100, "enabled": True, "poll_seconds": 30}],
        },
    )
    try:
        await service.tick(now=1000)
        failed = True
        await service.tick(now=1030)
        result = await service.snapshot(now=1030)
        check = next(c for c in result["checks"] if c["id"] == "balance:5")
        assert check["status"] == "unknown"
        assert result["summary"]["active_incidents"] == 1
        assert len(await runtime.store.notifications()) == 1
    finally:
        await runtime.close()
        await http.aclose()


async def test_concurrent_ticks_share_a_complete_channel_request(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return reply([])

    runtime, http, service = await setup(tmp_path, upstream, {"channels": {"enabled": True}})
    try:
        first = asyncio.create_task(service.tick(now=1000))
        await entered.wait()
        second = asyncio.create_task(service.tick(now=1000))
        release.set()
        await asyncio.gather(first, second)
        assert calls == 1
    finally:
        await runtime.close()
        await http.aclose()


async def test_policy_runtime_does_not_launch_legacy_collectors(tmp_path):
    runtime = Runtime(Settings(_env_file=None, demo=True, database=tmp_path / "tasks.sqlite3"))
    try:
        await runtime.open(monitoring=MonitoringConfig())
        names = {t.get_name() for t in runtime.tasks}
        assert "policy-checks" in names
        assert not names.intersection({"logs", "channels", "events", "report"})
    finally:
        await runtime.close()


async def test_missing_channel_confirms_across_complete_snapshots(tmp_path):
    items = [{"id": 12, "status": 1}]
    runtime, http, service = await setup(
        tmp_path, lambda _: reply(items), {"channels": {"confirmations": 2, "poll_seconds": 30}}
    )
    try:
        await service.tick(now=1000)
        items.clear()
        await service.tick(now=1030)
        await service.tick(now=1060)
        assert len(await runtime.store.notifications()) == 1
        assert "缺失" in (await runtime.store.notifications())[0]["content"]
    finally:
        await runtime.close()
        await http.aclose()


async def test_initial_overlap_does_not_replay_errors_before_baseline(tmp_path):
    runtime, http, service = await setup(
        tmp_path,
        lambda _: reply([{"id": 1, "created_at": 990, "type": 5, "other": {"status_code": 400}}]),
        {"channels": {"enabled": False}, "errors": {"enabled": True, "poll_seconds": 30}},
    )
    try:
        await service.tick(now=1000)
        await service.tick(now=1030)
        assert not await runtime.store.notifications()
    finally:
        await runtime.close()
        await http.aclose()


async def test_source_failure_confirmations_and_recovery_preserve_business_condition(tmp_path):
    failed = False

    def upstream(request):
        return httpx.Response(429) if failed else reply([{"id": 8, "status": 3}])

    runtime, http, service = await setup(
        tmp_path, upstream, {"channels": {"poll_seconds": 30}, "source": {"confirmations": 2}}
    )
    clock = [1000.0]
    runtime.client.throttle.clock = lambda: clock[0]
    try:
        await service.tick(now=1000)
        failed = True
        clock[0] = 1030
        await service.tick(now=1030)
        assert len(await runtime.store.notifications()) == 1
        clock[0] = 1090
        await service.tick(now=1090)
        assert len(await runtime.store.notifications()) == 2
        failed = False
        clock[0] = 1210
        await service.tick(now=1210)
        result = await service.snapshot(now=1210)
        assert result["summary"]["active_incidents"] == 1  # channel still auto-disabled
        assert "检查恢复" in (await runtime.store.notifications())[-1]["content"]
    finally:
        await runtime.close()
        await http.aclose()


async def test_confirmations_do_not_continue_after_disable_and_reenable(tmp_path):
    runtime, http, service = await setup(
        tmp_path,
        lambda _: reply([{"id": 1, "status": 3}]),
        {"channels": {"confirmations": 2, "poll_seconds": 30}},
    )
    try:
        await service.tick(now=1000)
        await service.configure(MonitoringConfig.model_validate({"channels": {"enabled": False}}))
        await service.configure(
            MonitoringConfig.model_validate({"channels": {"confirmations": 2, "poll_seconds": 30}})
        )
        await service.tick(now=1030)
        assert not await runtime.store.notifications()
    finally:
        await runtime.close()
        await http.aclose()


async def test_slow_user_check_does_not_block_next_channel_schedule(tmp_path):
    user_entered, release, channel_rechecked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def upstream(request):
        nonlocal calls
        if request.url.path == "/api/channel/":
            calls += 1
            if calls == 2:
                channel_rechecked.set()
            return reply([])
        user_entered.set()
        await release.wait()
        return httpx.Response(200, json={"success": True, "data": {"id": 1, "quota": 1000}})

    runtime, http, service = await setup(
        tmp_path,
        upstream,
        {
            "channels": {"poll_seconds": 30},
            "balances": [{"user_id": 1, "threshold": 10, "enabled": True}],
        },
    )
    try:
        await service.schedule(now=1000)
        await asyncio.wait_for(user_entered.wait(), timeout=2)
        await asyncio.wait_for(service.engine.inflight["channels"], timeout=2)
        await service.schedule(now=1030)
        await asyncio.wait_for(channel_rechecked.wait(), timeout=2)
        assert calls == 2
    finally:
        release.set()
        await runtime.close()
        await http.aclose()


async def test_error_evidence_and_notification_redact_all_configured_secrets(tmp_path):
    runtime, http, service = await setup(
        tmp_path, lambda _: reply([]), {"channels": {"enabled": False}, "errors": {"enabled": True}}
    )
    from pydantic import SecretStr

    runtime.settings.newapi_token = SecretStr("configured-test-secret")
    try:
        await service.errors(
            [
                LogRecord(
                    id=1,
                    created_at=1000,
                    type=5,
                    http_status=400,
                    error_code="configured-test-secret",
                    channel_name="configured-test-secret",
                )
            ],
            now=1000,
        )
        notifications = await runtime.store.notifications()
        assert "configured-test-secret" not in notifications[0]["content"]
        assert "[REDACTED]" in notifications[0]["content"]
    finally:
        await runtime.close()
        await http.aclose()


async def test_missing_quota_is_unknown_not_zero_balance(tmp_path):
    runtime, http, service = await setup(
        tmp_path,
        lambda _: httpx.Response(
            200, json={"success": True, "data": {"id": 1, "username": "no-quota"}}
        ),
        {
            "channels": {"enabled": False},
            "balances": [{"user_id": 1, "threshold": 100, "enabled": True}],
        },
    )
    try:
        await service.tick(now=1000)
        result = await service.snapshot(now=1000)
        assert next(c for c in result["checks"] if c["id"] == "balance:1")["status"] == "unknown"
        assert not await runtime.store.notifications()
    finally:
        await runtime.close()
        await http.aclose()


async def test_policy_activation_pauses_legacy_backlog_without_deleting_and_allows_explicit_retry(
    tmp_path,
):
    from newapi_watch.store import Store

    path = tmp_path / "backlog.sqlite3"
    store = Store(path)
    await store.open()
    await store.enqueue("alert:legacy:1:firing", "old collector message", 1000)
    await store.enqueue("policy:channel:1:1:1000", "new policy message", 1000)
    await store.close()
    runtime = Runtime(Settings(_env_file=None, demo=True, database=path))
    await runtime.open(start_tasks=False, monitoring=MonitoringConfig())
    try:
        notifications = await runtime.store.notifications()
        assert notifications[0]["status"] == "historical"
        assert notifications[1]["status"] == "pending"
        assert notifications[0]["content"] == "old collector message"
        result = await runtime.commands.operations.retry(
            notifications[0]["id"], "retry-legacy", now=1001
        )
        assert result["status"] == "pending"
    finally:
        await runtime.close()


async def test_manual_disable_breaks_unconfirmed_failure_streak(tmp_path):
    status = 3
    runtime, http, service = await setup(
        tmp_path,
        lambda _: reply([{"id": 1, "status": status}]),
        {"channels": {"poll_seconds": 30, "confirmations": 2}},
    )
    try:
        await service.tick(now=1000)
        status = 2
        await service.tick(now=1030)
        status = 3
        await service.tick(now=1060)
        assert not await runtime.store.notifications()
    finally:
        await runtime.close()
        await http.aclose()


async def test_web_channel_query_reuses_latest_monitor_result(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        return reply([{"id": 1, "status": 1}])

    runtime, http, service = await setup(tmp_path, upstream, {"channels": {"enabled": True}})
    try:
        await service.tick(now=1000)
        result = await runtime.commands.data.channels()
        assert result["total"] == 1 and calls == ["/api/channel/"]
    finally:
        await runtime.close()
        await http.aclose()
