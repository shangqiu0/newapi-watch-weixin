import httpx
import pytest

from newapi_watch.client import NewAPIClient
from newapi_watch.config import Settings
from newapi_watch.models import Channel, LogRecord
from newapi_watch.monitor import Monitor
from newapi_watch.store import Store


@pytest.fixture
async def store(tmp_path):
    db = Store(tmp_path / "test.sqlite3")
    await db.open()
    yield db
    await db.close()


async def test_automatic_disable_is_persisted_once_and_recovery_is_ordered(store):
    enabled = Channel(id=1, name="渠道一", status=1)
    disabled = enabled.model_copy(update={"status": 3})
    await store.sync_channels([enabled], 100)
    await store.sync_channels([disabled], 110)
    await store.sync_channels([disabled], 120)
    await store.sync_channels([enabled], 130)
    alerts = await store.alerts()
    assert alerts[0]["state"] == "resolved"
    messages = await store.notifications()
    assert len(messages) == 2
    assert "自动禁用" in messages[0]["content"]
    assert "恢复" in messages[1]["content"]


async def test_first_observed_auto_disabled_alerts_but_manual_disabled_is_not_critical(store):
    await store.sync_channels([Channel(id=1, status=3), Channel(id=2, status=2)], 100)
    alerts = await store.alerts()
    assert len(alerts) == 1
    assert alerts[0]["key"] == "channel:1:auto_disabled"
    assert len(await store.channels()) == 2


async def test_disappeared_channel_is_retained_as_missing(store):
    await store.sync_channels([Channel(id=1, status=1)], 100)
    await store.sync_channels([], 110)
    assert await store.channels() == []
    assert "缺失" in (await store.notifications())[0]["content"]
    await store.sync_channels([Channel(id=1, status=1)], 120)
    assert len(await store.channels()) == 1
    assert (await store.alerts())[0]["state"] == "resolved"


async def test_overlapping_logs_are_idempotent_and_checkpoint_is_atomic(store):
    rows = [
        LogRecord(id=1, created_at=100, type=2, channel=1, quota=500),
        LogRecord(id=2, created_at=100, type=5, channel=1),
    ]
    await store.save_logs(rows, 200, 50)
    await store.save_logs(rows, 210, 80)
    stats = await store.summary(0, 300)
    assert stats["success_records"] == 1
    assert stats["error_records"] == 1
    assert stats["quota"] == 500
    assert await store.get_meta("log_cursor") == "210"
    assert await store.get_meta("log_coverage_start") == "50"


async def test_failed_pagination_does_not_clear_snapshot_or_advance_cursor(store):
    await store.sync_channels([Channel(id=1, status=1)], 100)
    await store.set_meta("log_cursor", "100")
    config = Settings(
        _env_file=None, demo=True, newapi_url="https://relay.example", request_attempts=1
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as http:
        monitor = Monitor(config, store, NewAPIClient(config, http))
        assert await monitor.collect_channels(now=200) is False
        assert await monitor.collect_logs(now=200) is False
    assert (await store.channels())[0]["id"] == 1
    assert await store.get_meta("log_cursor") == "100"
    assert any(a["key"] == "service:newapi" for a in await store.alerts())


async def test_concurrent_observations_do_not_duplicate_notifications(store):
    import asyncio

    await asyncio.gather(
        *(store.condition("same", True, "异常", "warning", 100) for _ in range(10))
    )
    assert len(await store.notifications()) == 1


async def test_state_survives_database_restart(tmp_path):
    path = tmp_path / "restart.sqlite3"
    first = Store(path)
    await first.open()
    await first.sync_channels([Channel(id=1, status=3)], 100)
    await first.close()
    second = Store(path)
    await second.open()
    await second.sync_channels([Channel(id=1, status=3)], 200)
    assert len(await second.notifications()) == 1
    await second.close()


async def test_silence_suppresses_delivery_not_state(store):
    await store.silence(1, 500, 100)
    await store.sync_channels([Channel(id=1, status=3)], 110)
    assert (await store.alerts())[0]["state"] == "firing"
    assert await store.notifications() == []


async def test_concurrent_manual_probes_are_coalesced_to_protect_quota(store):
    import asyncio

    calls = 0
    cfg = Settings(
        _env_file=None, demo=True, newapi_url="https://relay.example", probe_enabled=True
    )

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"success": True, "time": 0.2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monitor = Monitor(cfg, store, NewAPIClient(cfg, http))
        results = await asyncio.gather(*(monitor.probe(1) for _ in range(6)))
    assert all(results)
    assert calls == 1
