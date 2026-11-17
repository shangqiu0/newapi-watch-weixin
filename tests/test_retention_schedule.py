"""Exercise scheduled cleanup through Runtime/Fleet, not just the prune helper."""

import json
import time
from types import SimpleNamespace

from test_console_v03 import config, login
from test_console_v03 import console as _console

from newapi_watch import runtime as runtime_module
from newapi_watch.persistence import row, rows
from newapi_watch.retention import RetentionConfig, RetentionService

console = _console


async def create(http, name="A", **extra):
    return (await http.post("/console/api/platforms", json={**config(name), **extra})).json()[
        "data"
    ]


async def test_runtime_due_cleanup_obeys_saved_days_and_preserves_other_platform(
    console, monkeypatch
):
    http, _, fleet = console
    await login(http)
    a, b = await create(http), await create(http, "B")
    now = time.time()
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: now))
    for platform in [a, b]:
        async with fleet.entry(platform["id"]).runtime.store.transaction() as db:
            await db.executemany(
                "INSERT INTO logs(id,created_at) VALUES(?,?)",
                [(1, now - 2 * 86400), (2, now - 86400), (3, now)],
            )
    base = f"/console/api/platforms/{a['id']}"
    await http.patch(base, json={"retention": {"logs_days": 1}})
    runtime = fleet.entry(a["id"]).runtime
    await runtime.prune()
    async with runtime.store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 3
    await runtime.store.set_meta("retention:next_at", str(now - 1))
    await runtime.prune()
    async with runtime.store.transaction() as db:
        assert [r["id"] for r in await rows(db, "SELECT id FROM logs ORDER BY id")] == [2, 3]
    async with fleet.entry(b["id"]).runtime.store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 3
    state = (await http.get(base + "/data-management")).json()["data"]
    assert state["last_run"]["deleted"]["logs"] == 1
    assert state["next_check_at"] == now + 3600
    # The normal scheduler cannot re-run an already completed hourly pass.
    await runtime.store.set_meta("retention:last_run", '{"marker":"not_due"}')
    await runtime.prune()
    assert json.loads(await runtime.store.get_meta("retention:last_run"))["marker"] == "not_due"


async def test_all_automatic_categories_delete_only_expired_safe_records(console):
    http, _, fleet = console
    await login(http)
    a = await create(http)
    runtime = fleet.entry(a["id"]).runtime
    now = time.time()
    old, recent = now - 2 * 86400, now
    async with runtime.store.transaction() as db:
        await db.executemany(
            "INSERT INTO notifications(dedupe_key,status,lease_until,created_at,updated_at) VALUES(?,?,?,?,?)",
            [
                (status, status, 0, old, old)
                for status in ["sent", "historical", "cancelled", "dead", "pending"]
            ]
            + [("leased", "sending", now + 60, old, old), ("fresh", "sent", 0, recent, recent)],
        )
        await db.executemany(
            "INSERT INTO query_jobs(id,dedupe_key,owner_hash,status,context_json,notified_at,created_at,updated_at) VALUES(?,?,?, ?,?,?,?,?)",
            [
                (key, key, "owner", status, context, notified, old, old)
                for key, status, context, notified in [
                    ("web", "complete", '{"binding_id":"web"}', 0),
                    ("bot-waiting", "complete", '{"binding_id":"bot-A"}', 0),
                    ("bot-delivered", "complete", '{"binding_id":"bot-A"}', now),
                    ("failed", "failed", "{}", 0),
                    ("cancelled", "cancelled", "{}", 0),
                    ("running", "running", "{}", 0),
                    ("queued", "queued", "{}", 0),
                ]
            ],
        )
        await db.executemany(
            "INSERT INTO alerts(id,key,state,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            [
                (state, state, state, "visible", old, old)
                for state in ["firing", "resolved", "event"]
            ],
        )
        await db.executemany(
            "INSERT INTO command_audits(id,created_at,updated_at) VALUES(?,?,?)",
            [("old", old, old), ("fresh", recent, recent)],
        )
        await db.executemany(
            "INSERT INTO probes(channel_id,created_at) VALUES(1,?)", [(old,), (recent,)]
        )
        await db.executemany(
            "INSERT INTO error_seen(id,created_at,updated_at) VALUES(?,?,?)",
            [("old", 0, 0), ("fresh", recent, recent)],
        )
        await db.execute(
            "INSERT INTO error_buckets(id,pending_count,created_at,updated_at) VALUES('empty',0,0,0),('pending',7,0,0)"
        )
        await db.execute(
            "INSERT INTO receipts(id,expires_at) VALUES('expired',0),('valid',?)", (now + 100,)
        )
        await runtime.store.put_meta(db, "policy:error_scan", '{"next_page":3}', old)
        await runtime.store.put_meta(db, "retention:preview:expired", "{}", old)
    result = await RetentionService(runtime.store).prune(
        RetentionConfig(confirmed=True, logs_days=1, history_days=1, tasks_days=1, audits_days=1),
        now=now,
    )
    assert result["deleted"]["notifications"] == 4
    assert result["deleted"]["tasks"] == 4
    assert result["deleted"]["alerts"] == 2
    assert result["deleted"]["audits"] == 1
    async with runtime.store.transaction() as db:
        assert {r["id"] for r in await rows(db, "SELECT id FROM query_jobs")} == {
            "running",
            "queued",
            "bot-waiting",
        }
        assert (await row(db, "SELECT title FROM alerts WHERE state='firing'"))[
            "title"
        ] == "visible"
        assert (await row(db, "SELECT COUNT(*) AS n FROM probes"))["n"] == 1
        assert (await row(db, "SELECT COUNT(*) AS n FROM error_seen"))["n"] == 2
        assert (await row(db, "SELECT id FROM error_buckets"))["id"] == "pending"
        assert (await row(db, "SELECT id FROM receipts"))["id"] == "valid"
    assert await runtime.store.get_meta("policy:error_scan") is not None
    assert await runtime.store.get_meta("retention:preview:expired") is None
    # Retention is idempotent and active/error runtime state is not ordinary history.
    again = await RetentionService(runtime.store).prune(
        RetentionConfig(confirmed=True, logs_days=1, history_days=1, tasks_days=1, audits_days=1),
        now=now,
    )
    assert not any(again["deleted"].values())
    async with runtime.store.transaction() as db:
        await db.execute("DELETE FROM metadata WHERE id='policy:error_scan'")
    after_scan = await RetentionService(runtime.store).prune(RetentionConfig(), now=now)
    assert after_scan["deleted"]["error_seen"] == 1


async def test_control_record_cleanup_backlog_retries_in_one_minute_and_is_observable(
    console, monkeypatch
):
    http, control, fleet = console
    await login(http)
    a, b = await create(http), await create(http, "B")
    now = time.time()
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: now))
    async with control.transaction() as db:
        await db.executemany(
            "INSERT INTO operation_records(id,platform_id,action,created_at) VALUES(?,?,?,0)",
            [(f"old-{i}", a["id"], "old") for i in range(501)] + [("other", b["id"], "old")],
        )
    runtime = fleet.entry(a["id"]).runtime
    await runtime.store.set_meta("retention:next_at", str(now - 1))
    await runtime.prune()
    state = json.loads(await runtime.store.get_meta("retention:last_run"))
    assert state["deleted"]["operations"] == 500
    assert state["backlog"] is True
    assert float(await runtime.store.get_meta("retention:next_at")) == now + 60
    now += 61
    await runtime.prune()
    state = json.loads(await runtime.store.get_meta("retention:last_run"))
    assert state["deleted"]["operations"] == 1
    assert state["backlog"] is False
    assert (await control.operation_records(b["id"]))["total"] >= 1


async def test_log_page_exposes_current_local_retention_and_real_source(console):
    http, _, fleet = console
    await login(http)
    a = await create(http, retention={"enabled": True, "logs_days": 7})
    base = f"/console/api/platforms/{a['id']}"
    runtime = fleet.entry(a["id"]).runtime
    await runtime.store.set_meta("retention:next_at", "1234567890")
    local = (await http.get(base + "/logs?live=false")).json()["data"]
    assert local["source"] == "local_newapi_logs"
    assert local["retention"]["logs_days"] == 7
    assert local["retention"]["enabled"] is True
    assert local["retention"]["next_check_at"] == 1234567890
    real = (await http.get(base + "/logs")).json()["data"]
    assert real["source"] == "newapi_admin_api"
    assert real["retention"] == local["retention"]


async def test_disabled_retention_keeps_history_and_still_expires_transport_receipts(console):
    http, control, fleet = console
    await login(http)
    a = await create(http, enabled=False, retention={"enabled": False, "logs_days": 1})
    runtime = fleet.entry(a["id"]).runtime
    async with runtime.store.transaction() as db:
        await db.execute("INSERT INTO logs(id,created_at) VALUES(1,0)")
        await db.execute("INSERT INTO receipts(id,expires_at) VALUES('old',0)")
    await runtime.store.set_meta("retention:next_at", "1")
    await control.record_operation(a["id"], "old")
    async with control.transaction() as db:
        await db.execute("UPDATE operation_records SET created_at=0 WHERE action='old'")
    await runtime.prune()
    async with runtime.store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 1
        assert (await row(db, "SELECT COUNT(*) AS n FROM receipts"))["n"] == 0
    assert any(r["action"] == "old" for r in (await control.operation_records(a["id"]))["items"])


async def test_disabled_platform_starts_only_real_local_housekeeping(console, monkeypatch):
    import asyncio

    http, _, fleet = console
    await login(http)
    platform = await create(http, enabled=False, retention={"enabled": True, "logs_days": 1})
    runtime = fleet.entry(platform["id"]).runtime
    async with runtime.store.transaction() as db:
        await db.execute("INSERT INTO logs(id,created_at) VALUES(1,0)")
    await runtime.store.set_meta("retention:next_at", "1")
    ran = asyncio.Event()
    original = runtime_module.Runtime.run_job

    async def observed(self, name, callback):
        result = await original(self, name, callback)
        if name == "retention":
            ran.set()
        return result

    monkeypatch.setattr(runtime_module.Runtime, "run_job", observed)
    fleet.start_tasks = True
    await fleet.reload(platform["id"])
    await asyncio.wait_for(ran.wait(), timeout=10)
    current = fleet.entry(platform["id"]).runtime
    assert [task.get_name() for task in current.tasks] == ["retention"]
    async with current.store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 0
    assert fleet.entry(platform["id"]).bots == []


async def test_auxiliary_cleanup_batches_expose_counts_and_keep_short_backlog_retry(console):
    http, _, fleet = console
    await login(http)
    platform = await create(http)
    store = fleet.entry(platform["id"]).runtime.store
    async with store.transaction() as db:
        await db.executemany(
            "INSERT INTO probes(channel_id,created_at) VALUES(1,0)", [() for _ in range(1001)]
        )
        await db.executemany(
            "INSERT INTO receipts(id,expires_at) VALUES(?,0)", [(str(i),) for i in range(1001)]
        )
        await db.executemany(
            "INSERT INTO metadata(id,value,updated_at) VALUES(?,'{}',0)",
            [(f"retention:preview:{i}",) for i in range(1001)],
        )
    first = await RetentionService(store).prune(RetentionConfig())
    assert first["deleted"]["probes"] == 1000
    assert first["deleted"]["receipts"] == 1000
    assert first["deleted"]["previews"] == 1000
    assert first["backlog"] is True
    second = await RetentionService(store).prune(RetentionConfig())
    assert (
        second["deleted"]["probes"]
        == second["deleted"]["receipts"]
        == second["deleted"]["previews"]
        == 1
    )
    assert second["backlog"] is False
