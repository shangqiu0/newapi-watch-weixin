import asyncio
import sqlite3
from contextlib import closing
from importlib.resources import files

import pytest

from newapi_watch.operations import OperationError, Operations
from newapi_watch.persistence import row
from newapi_watch.store import Store


@pytest.fixture
async def store(tmp_path):
    db = Store(tmp_path / "ops.sqlite3")
    await db.open()
    try:
        yield db
    finally:
        await db.close()


async def test_v1_database_migrates_without_losing_state(tmp_path):
    path = tmp_path / "v1.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(files("newapi_watch").joinpath("schema.sql").read_text("utf-8-sig"))
        db.execute("INSERT INTO metadata(id,value) VALUES('old-checkpoint','123')")
        db.execute(
            "INSERT INTO notifications(dedupe_key,content,status) VALUES('old','keep-me','dead')"
        )
        db.commit()
    first = Store(path)
    await first.open()
    try:
        assert await first.get_meta("old-checkpoint") == "123"
        assert (await first.notifications())[0]["content"] == "keep-me"
        async with first.transaction() as db:
            assert (await row(db, "PRAGMA user_version"))["user_version"] == 6
            assert (await row(db, "SELECT COUNT(*) AS n FROM query_jobs"))["n"] == 0
        assert (await Operations(first).dead_letters())["total"] == 1
    finally:
        await first.close()
    second = Store(path)
    await second.open()
    assert (await Operations(second).dead_letters())["total"] == 1
    await second.close()


async def test_future_database_version_fails_closed(tmp_path):
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError):
        await Store(path).open()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


async def test_acknowledgement_does_not_recover_or_send_notification(store):
    await store.condition("channel:1:probe", True, "failure", "warning", 100)
    alert_id = (await store.alerts())[0]["id"]
    ops = Operations(store)
    first = await ops.acknowledge(alert_id, actor_hash="fingerprint", now=101)
    second = await ops.acknowledge(alert_id, actor_hash="another", now=102)
    assert first["acknowledged"] is True and second["duplicate"] is True
    assert (await store.alerts())[0]["state"] == "firing"
    assert len(await store.notifications()) == 1
    assert (await ops.alerts())["items"][0]["acknowledged"] is True
    await store.condition("channel:1:probe", False, "recovered", "warning", 110)
    await store.condition("channel:1:probe", True, "again", "warning", 120)
    assert (await ops.alerts())["items"][0]["acknowledged"] is False


async def test_acknowledging_unknown_or_resolved_alert_is_rejected(store):
    ops = Operations(store)
    with pytest.raises(OperationError) as unknown:
        await ops.acknowledge("0" * 32, now=100)
    assert unknown.value.status == 404
    await store.condition("test", True, "x", "warning", 100)
    await store.condition("test", False, "x", "warning", 110)
    with pytest.raises(OperationError) as resolved:
        await ops.acknowledge((await store.alerts())[0]["id"], now=120)
    assert resolved.value.status == 409


async def test_retry_dead_is_atomic_and_original_order_preserved(store):
    await store.enqueue("dead", "old alert", 100)
    await store.finish_notification(1, "dead", 8, 100, "failed", 101)
    await store.enqueue("later", "later alert", 102)
    ops = Operations(store)
    results = await asyncio.gather(*(ops.retry(1, "same-request-key", now=110) for _ in range(8)))
    assert sum(not result["duplicate"] for result in results) == 1
    first = (await store.notifications())[0]
    assert first["id"] == 1 and first["status"] == "pending" and first["attempts"] == 0
    assert "历史通知重试" in first["content"]
    assert (await store.claim_notification(110))["id"] == 1


async def test_retry_key_survives_restart_and_cannot_reset_new_dead_cycle(tmp_path):
    path = tmp_path / "restart.sqlite3"
    store = Store(path)
    await store.open()
    await store.enqueue("x", "x", 100)
    await store.finish_notification(1, "dead", 8, 100, "x", 101)
    await Operations(store).retry(1, "request-abc", now=110)
    await store.finish_notification(1, "dead", 8, 120, "x", 120)
    await store.close()
    store = Store(path)
    await store.open()
    try:
        assert (await Operations(store).retry(1, "request-abc", now=130))["duplicate"] is True
        assert (await store.notifications())[0]["status"] == "dead"
    finally:
        await store.close()


async def test_retry_rejects_non_dead_unknown_and_cross_resource_key(store):
    await store.enqueue("x", "x", 100)
    ops = Operations(store)
    with pytest.raises(OperationError):
        await ops.retry(1, "pending-request", now=110)
    with pytest.raises(OperationError) as missing:
        await ops.retry(999, "missing-request", now=110)
    assert missing.value.status == 404
    await store.finish_notification(1, "dead", 8, 100, "failed", 101)
    await ops.retry(1, "fixed-request", now=110)
    await store.enqueue("y", "y", 111)
    await store.finish_notification(2, "dead", 8, 111, "failed", 112)
    with pytest.raises(OperationError) as conflict:
        await ops.retry(2, "fixed-request", now=120)
    assert conflict.value.status == 409


async def test_silences_hide_expired_and_paginate(store):
    await store.silence(1, 200, 100)
    await store.silence(2, 90, 80)
    page = await Operations(store).silences(now=110)
    assert page["total"] == 1
    assert page["items"][0]["channel_id"] == 1


async def test_failed_upgrade_rolls_back_and_preserves_v1_data(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "failed-upgrade.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(files("newapi_watch").joinpath("schema.sql").read_text("utf-8-sig"))
        db.execute("INSERT INTO metadata(id,value) VALUES('checkpoint','keep')")
        db.commit()
    original = Path.read_text

    def broken_upgrade(self, *args, **kwargs):
        if self.name == "002.sql":
            return "CREATE TABLE partial_upgrade(id INTEGER); SELECT missing_column FROM does_not_exist;"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", broken_upgrade)
    with pytest.raises(sqlite3.OperationalError):
        await Store(path).open()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute("SELECT value FROM metadata WHERE id='checkpoint'").fetchone()[0] == "keep"
        )
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='partial_upgrade'").fetchone()
            is None
        )


async def test_manual_delivery_mark_only_pending_alerts_and_keeps_audit(store):
    await store.enqueue("manual-a", "pending alert", 1000)
    await store.enqueue("manual-b", "already sent", 1000)
    await store.enqueue("manual-c", "currently sending", 1000)
    await store.enqueue("manual-d", "daily report", 1000)
    async with store.transaction() as db:
        await db.execute("UPDATE notifications SET status='sent' WHERE id=2")
        await db.execute("UPDATE notifications SET status='sending',lease_until=2000 WHERE id=3")
        await db.execute("UPDATE notifications SET kind='daily' WHERE id=4")
    result = await Operations(store).mark_pending_delivered(
        "operator-batch-1", before_id=4, now=1001
    )
    assert result["count"] == 1 and result["duplicate"] is False
    records = await store.notifications()
    assert records[0]["status"] == "sent"
    assert "人工" in records[0]["last_error"] and "未实际发送" in records[0]["last_error"]
    assert [r["status"] for r in records[1:]] == ["sent", "sending", "pending"]
    async with store.transaction() as db:
        audit = await row(db, "SELECT action,outcome FROM command_audits")
        assert audit == {"action": "notifications_manual_mark", "outcome": "success"}
        assert (await row(db, "SELECT COUNT(*) AS n FROM notifications"))["n"] == 4
    await store.enqueue("manual-e", "new alert after request", 1002)
    again = await Operations(store).mark_pending_delivered(
        "operator-batch-1", before_id=4, now=1003
    )
    assert again["duplicate"] and again["count"] == 1
    assert (await store.notifications())[-1]["status"] == "pending"


async def test_manual_delivery_mark_validates_key_and_snapshot(store):
    with pytest.raises(OperationError):
        await Operations(store).mark_pending_delivered("x", before_id=1)
    with pytest.raises(OperationError):
        await Operations(store).mark_pending_delivered("valid-request", before_id=-1)
