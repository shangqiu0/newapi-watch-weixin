import httpx
import pytest

from newapi_watch.api import create_app
from newapi_watch.config import Settings
from newapi_watch.models import Channel, CommandResult, LogRecord
from newapi_watch.monitor import Monitor
from newapi_watch.notifications import NotificationWorker
from newapi_watch.store import Store


@pytest.fixture
async def store(tmp_path):
    db = Store(tmp_path / "db")
    await db.open()
    try:
        yield db
    finally:
        await db.close()


async def test_retention_advances_report_coverage(store):
    await store.save_logs([LogRecord(id=1, created_at=100, type=2)], 500, 0)
    await store.prune(200)
    assert float(await store.get_meta("log_coverage_start")) >= 200


async def test_delivery_rate_limit_survives_worker_restart(store):
    cfg = Settings(_env_file=None, demo=True)
    await store.enqueue("a", "first", 10)
    await store.enqueue("b", "second", 10)
    first = NotificationWorker(cfg, store, None, None)
    assert await first.process_one(now=10)
    restarted = NotificationWorker(cfg, store, None, None)
    assert not await restarted.process_one(now=11)
    assert await restarted.process_one(now=14)


async def test_wrong_database_instance_is_rejected(store):
    await store.bind_instance("https://first.example")
    with pytest.raises(ValueError):
        await store.bind_instance("https://second.example")


async def test_command_api_requires_idempotency_for_mutation_and_limits_body(store):
    cfg = Settings(_env_file=None, demo=True, api_key="k" * 32)
    calls = []

    class Commands:
        async def execute(self, text):
            calls.append(text)
            return CommandResult(text="ok")

    app = create_app(cfg, store, Commands(), None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "k" * 32},
    ) as http:
        missing = await http.post("/api/v1/commands", json={"text": "渠道 1 测试"})
        assert missing.status_code == 400
        response = await http.post(
            "/api/v1/commands",
            json={"text": "渠道 1 测试"},
            headers={"Idempotency-Key": "probe-request-1"},
        )
        assert response.status_code == 200
        duplicate = await http.post(
            "/api/v1/commands",
            json={"text": "渠道 1 测试"},
            headers={"Idempotency-Key": "probe-request-1"},
        )
        assert duplicate.status_code == 409
        assert len(calls) == 1
        large = await http.post("/api/v1/commands", content=b"x" * 65537)
        assert large.status_code == 413


async def test_delivery_lease_recovers_without_parallel_claims(store):
    await store.enqueue("a", "first", 10)
    first = await store.claim_notification(10, lease=10)
    assert first["id"] == 1
    assert await store.claim_notification(11) is None
    assert (await store.claim_notification(21))["id"] == 1


async def test_stale_event_only_marks_reconciliation_not_channel_state(store):
    await store.sync_channels([Channel(id=1, status=1)], 100)
    assert await store.accept_event("digest", "channel_update_1_3", 110, 300)
    assert (await store.channels())[0]["status"] == 1
    assert await store.take_event()
    assert not await store.take_event()


async def test_api_reports_exposes_png_not_base64_json(store):
    class Reports:
        async def chart(self, day):
            return b"\x89PNG\r\n\x1a\n"

    class Commands:
        reports = Reports()

    cfg = Settings(_env_file=None, demo=True, api_key="k" * 32)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(cfg, store, Commands(), None)),
        base_url="http://test",
        headers={"X-API-Key": "k" * 32},
    ) as http:
        response = await http.get("/api/v1/reports/2026-11-09/chart")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"


async def test_error_alert_does_not_falsely_recover_on_empty_window(store):
    cfg = Settings(_env_file=None, demo=True, error_min_records=2)
    await store.sync_channels([Channel(id=1, status=1)], 0)
    await store.save_logs(
        [
            LogRecord(id=1, created_at=100, type=5, channel=1),
            LogRecord(id=2, created_at=101, type=5, channel=1),
        ],
        200,
        0,
    )
    monitor = Monitor(cfg, store, None)
    await monitor._error_rules(200)
    await monitor._error_rules(1000)
    assert (await store.alerts())[0]["state"] == "firing"
