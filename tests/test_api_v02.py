import time

import httpx
import pytest

from newapi_watch.api import create_app
from newapi_watch.config import Settings
from newapi_watch.runtime import Runtime


@pytest.fixture
async def app(tmp_path):
    cfg = Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "api.sqlite3",
        api_key="v02-test-service-key-1234567890",
        report_enabled=False,
    )
    runtime = Runtime(cfg)
    await runtime.open(start_tasks=False)
    try:
        await runtime.monitor.collect_channels()
        await runtime.monitor.collect_logs()
        application = create_app(cfg, runtime.store, runtime.commands, runtime.monitor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            headers={"X-API-Key": cfg.api_key.get_secret_value()},
        ) as http:
            yield http, runtime
    finally:
        await runtime.close()


async def test_new_statistics_endpoints_are_authenticated_and_return_bounded_dtos(app):
    http, runtime = app
    query = {"start": "2026-11-01", "end": "2026-11-03", "dimension": "model"}
    response = await http.get("/api/v1/statistics", params=query)
    assert response.status_code == 200
    assert response.json()["data"]["query"]["start"] == "2026-11-01"
    assert response.json()["data"]["coverage"]["complete"] is False
    image = await http.get("/api/v1/statistics/chart", params={**query, "kind": "ranking"})
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.headers["cache-control"] == "no-store"
    for path in ("statistics", "statistics/chart", "silences", "audits"):
        response = await http.get("/api/v1/" + path, params=query, headers={"X-API-Key": "wrong"})
        assert response.status_code == 401


@pytest.mark.parametrize(
    "query",
    [
        {"start": "2026-11-03", "end": "2026-11-01"},
        {"start": "2026-01-01", "end": "2026-03-01"},
        {"start": "2099-01-01", "end": "2099-01-01"},
        {"start": "2026-11-01", "end": "2026-11-03", "limit": 100000},
        {"start": "2026-11-01", "end": "2026-11-03", "dimension": "sql-injection"},
    ],
)
async def test_statistics_reject_invalid_input_without_internal_details(app, query):
    http, runtime = app
    response = await http.get("/api/v1/statistics", params=query)
    assert response.status_code == 400
    assert "traceback" not in response.text.lower()


async def test_ack_api_retries_list_pagination_and_conflicts(app):
    http, runtime = app
    await runtime.store.condition("test", True, "test alert", "warning", time.time())
    alert_id = (await runtime.store.alerts())[0]["id"]
    response = await http.put(f"/api/v1/alerts/{alert_id}/acknowledgement")
    assert response.status_code == 200 and response.json()["data"]["acknowledged"] is True
    assert (await runtime.store.alerts())[0]["state"] == "firing"
    await runtime.store.finish_notification(1, "dead", 8, 100, "failed", 100)
    missing_key = await http.post("/api/v1/notifications/1/retries")
    assert missing_key.status_code == 400
    first = await http.post(
        "/api/v1/notifications/1/retries", headers={"Idempotency-Key": "v02-request-key-001"}
    )
    assert first.status_code == 200 and first.json()["data"]["duplicate"] is False
    again = await http.post(
        "/api/v1/notifications/1/retries", headers={"Idempotency-Key": "v02-request-key-001"}
    )
    assert again.status_code == 200 and again.json()["data"]["duplicate"] is True
    conflict = await http.post(
        "/api/v1/notifications/1/retries", headers={"Idempotency-Key": "v02-new-key-002"}
    )
    assert conflict.status_code == 409
    audits = await http.get("/api/v1/audits", params={"page_size": 1})
    assert audits.status_code == 200 and len(audits.json()["data"]["items"]) == 1
    assert (await http.get("/api/v1/audits", params={"page_size": 101})).status_code == 400


async def test_command_mutations_require_idempotency_and_queries_are_audited_as_http(app):
    http, runtime = app
    missing = await http.post("/api/v1/commands", json={"text": "通知 重试 1"})
    assert missing.status_code == 400
    response = await http.post("/api/v1/commands", json={"text": "统计 近7天"})
    assert response.status_code == 200 and response.json()["data"]["ok"] is True
    assert (await runtime.commands.audits.list())["items"][0]["source"] == "http"
    await http.post("/api/v1/commands", json={"text": "告警 静默列表"})
    assert (await runtime.commands.audits.list())["items"][0]["outcome"] == "success"


async def test_health_exposes_freshness_and_bot_state_not_secrets(app):
    http, runtime = app
    response = await http.get("/api/v1/health")
    data = response.json()["data"]
    assert data["channels_fresh"] is True and data["logs_fresh"] is True
    assert data["bot"]["status"] == "unconfigured"
    assert "actor_hash" not in response.text
    assert "v02-test-service-key" not in response.text
    await runtime.store.set_meta("channels_last_ok", "1")
    data = (await http.get("/api/v1/health")).json()["data"]
    assert data["status"] == "degraded" and data["channels_fresh"] is False


async def test_legacy_chart_invalid_future_returns_validation_error(app):
    http, runtime = app
    response = await http.get("/api/v1/reports/2099-01-01/chart")
    assert response.status_code == 400


async def test_statistics_response_redacts_configured_secrets_in_labels(app):
    from datetime import datetime

    from newapi_watch.models import LogRecord

    http, runtime = app
    instant = int(datetime.fromisoformat("2026-11-02T12:00:00+00:00").timestamp())
    await runtime.store.save_logs(
        [
            LogRecord(
                id=987,
                created_at=instant,
                type=2,
                model_name=runtime.settings.api_key.get_secret_value(),
                quota=50,
            )
        ],
        instant + 1,
        instant,
    )
    response = await http.get(
        "/api/v1/statistics", params={"start": "2026-11-01", "end": "2026-11-03"}
    )
    assert response.status_code == 200
    assert runtime.settings.api_key.get_secret_value() not in response.text
    assert "[REDACTED]" in response.text


async def test_health_marks_background_task_failure_degraded(app):
    http, runtime = app
    await runtime.store.condition("service:job:broken", True, "job failed", "warning", time.time())
    response = await http.get("/api/v1/health")
    assert response.json()["data"]["status"] == "degraded"
    assert "background_jobs_failed" in response.json()["data"]["reasons"]


async def test_health_distinguishes_configured_bot_authentication_without_connecting(tmp_path):
    from newapi_watch.health import Health
    from newapi_watch.store import Store

    cfg = Settings(
        _env_file=None,
        demo=False,
        newapi_url="https://offline.example",
        newapi_token="offline-only",
        newapi_user_id=1,
        api_key="offline-service-key-1234567890",
        bot_id="offline-id",
        bot_secret="offline-secret",
    )
    store = Store(tmp_path / "health.sqlite3")
    await store.open()
    try:
        await store.set_meta("channels_last_ok", "100")
        await store.set_meta("logs_last_ok", "100")
        first = await Health(cfg, store, lambda: False).snapshot(now=105)
        assert first["bot"]["status"] == "disconnected"
        assert "bot_disconnected" in first["reasons"]
        second = await Health(cfg, store, lambda: True).snapshot(now=105)
        assert second["bot"]["status"] == "authenticated"
        assert "bot_disconnected" not in second["reasons"]
    finally:
        await store.close()
