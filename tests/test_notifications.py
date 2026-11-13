import hashlib
import hmac
import json
import time

import httpx
import pytest

from newapi_watch.api import create_app
from newapi_watch.config import Settings
from newapi_watch.notifications import GroupWebhookNotifier, NotificationWorker, verify_signature
from newapi_watch.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "db.sqlite3",
        api_key="k" * 32,
        group_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
        webhook_enabled=True,
        webhook_secret="s" * 32,
        webhook_max_age=300,
        notification_retry_seconds=1,
        notification_max_attempts=2,
    )


async def test_signature_requires_exact_raw_body_and_valid_timestamp():
    body = b'{"type":"channel_update_1_3","timestamp":1000}'
    signature = hmac.new(b"s" * 32, body, hashlib.sha256).hexdigest()
    assert verify_signature(body, signature, "1000", "s" * 32, 1001, 300)
    assert not verify_signature(body + b" ", signature, "1000", "s" * 32, 1001, 300)
    assert not verify_signature(body, signature, "1000", "s" * 32, 1400, 300)
    assert not verify_signature(body, "x" * 64, "1000", "s" * 32, 1001, 300)


async def test_webhook_rejects_without_valid_signature_and_accepts_legacy_and_duplicate(
    settings, store
):
    app = create_app(settings, store, None, None)
    body = json.dumps(
        {"type": "channel_update_1_3", "title": "x", "timestamp": int(time.time())},
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(b"s" * 32, body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/webhooks/newapi", content=body)).status_code == 401
        first = await client.post(
            "/webhooks/newapi",
            content=body,
            headers={
                "X-Webhook-Signature": signature,
                "X-Webhook-Timestamp": str(int(time.time())),
            },
        )
        assert first.status_code == 202
        duplicate = await client.post(
            "/webhooks/newapi",
            content=body,
            headers={
                "X-Webhook-Signature": signature,
                "X-Webhook-Timestamp": str(int(time.time())),
            },
        )
        assert duplicate.json()["data"]["duplicate"] is True
    assert await store.get_meta("channel_event_pending") == "1"


async def test_webhook_disabled_returns_not_found(settings, store):
    settings.webhook_enabled = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, store, None, None)),
        base_url="http://test",
    ) as client:
        assert (await client.post("/webhooks/newapi", content=b"{}")).status_code == 404


async def test_webhook_rejects_large_body(settings, store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, store, None, None)),
        base_url="http://test",
    ) as client:
        response = await client.post("/webhooks/newapi", content=b"x" * 65537)
    assert response.status_code == 413


async def test_service_api_key_is_required_and_comparison_is_constant_time(settings, store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, store, None, None)),
        base_url="http://test",
    ) as client:
        assert (await client.get("/api/v1/channels")).status_code == 401
        assert (
            await client.get("/api/v1/channels", headers={"X-API-Key": "wrong"})
        ).status_code == 401
        response = await client.get("/api/v1/channels", headers={"X-API-Key": "k" * 32})
        assert response.status_code == 200
        assert response.json()["data"]["items"] == []


async def test_notifier_checks_wecom_business_errcode():
    replies = [
        httpx.Response(200, json={"errcode": 45009}),
        httpx.Response(200, json={"errcode": 0}),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: replies.pop(0))) as client:
        notifier = GroupWebhookNotifier(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", client
        )
        with pytest.raises(RuntimeError):
            await notifier.send("告警")
        await notifier.send("告警")


async def test_outbox_retries_and_dead_letters(settings, store):
    await store.enqueue("fault", "内容", 10)
    worker = NotificationWorker(settings, store, None, None)

    class Failure:
        async def send(self, content, image=None):
            raise RuntimeError("remote down")

    worker.webhook = Failure()
    assert await worker.process_one(now=10)
    assert (await store.notifications())[0]["status"] == "pending"
    assert not await worker.process_one(now=10)
    await worker.process_one(now=14)
    assert (await store.notifications())[0]["status"] == "dead"


async def test_outbox_success_marks_sent(settings, store):
    await store.enqueue("ok", "告警", 10)
    worker = NotificationWorker(settings, store, None, None)

    class Success:
        async def send(self, content, image=None):
            return None

    worker.webhook = Success()
    assert await worker.process_one(now=10)
    assert (await store.notifications())[0]["status"] == "sent"
    await store.close()


@pytest.fixture
async def store(settings):
    db = Store(settings.database)
    await db.open()
    try:
        yield db
    finally:
        await db.close()
