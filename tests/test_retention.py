"""Retention safety, bounded deletion, state transitions and platform isolation."""

import time

from test_console_v03 import config, login
from test_console_v03 import console as _console

from newapi_watch.persistence import row
from newapi_watch.retention import RetentionConfig, RetentionService

console = _console


async def test_retention_keeps_runtime_state_and_active_records(console):
    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    store = fleet.entry(platform["id"]).runtime.store
    now = time.time()
    async with store.transaction() as db:
        await db.execute("INSERT INTO logs(id,created_at) VALUES(1,?),(2,?)", (now - 86401, now))
        await db.execute(
            "INSERT INTO notifications(dedupe_key,status,created_at,updated_at) VALUES('old','sent',0,0),('active','pending',0,0)"
        )
        await db.execute("INSERT INTO metadata(id,value) VALUES('policy:error_cursor','keep')")
    result = await RetentionService(store).prune(
        RetentionConfig(logs_days=1, history_days=1), now=now
    )
    assert result["deleted"]["logs"] == 1
    async with store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 1
        assert (await row(db, "SELECT COUNT(*) AS n FROM notifications WHERE status='pending'"))[
            "n"
        ] == 1
        assert (await row(db, "SELECT value FROM metadata WHERE id='policy:error_cursor'"))[
            "value"
        ] == "keep"


async def test_cleanup_preview_execute_is_scoped_idempotent_and_csrf_protected(console):
    http, _, fleet = console
    await login(http)
    a = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    b = (await http.post("/console/api/platforms", json=config("B"))).json()["data"]
    store = fleet.entry(a["id"]).runtime.store
    async with store.transaction() as db:
        await db.execute(
            "INSERT INTO notifications(dedupe_key,status,created_at,updated_at) VALUES('done','sent',0,0),('protected','pending',0,0)"
        )
    base = f"/console/api/platforms/{a['id']}"
    preview = await http.post(
        base + "/cleanup-previews", json={"kind": "notifications", "before": time.time()}
    )
    assert preview.status_code == 200
    plan = preview.json()["data"]
    assert plan["count"] == 1 and plan["protected"] == 1
    assert (
        await http.post(
            f"/console/api/platforms/{b['id']}/cleanups", json={"preview_id": plan["preview_id"]}
        )
    ).status_code == 404
    csrf = http.headers.pop("X-CSRF-Token")
    assert (
        await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})
    ).status_code == 403
    http.headers["X-CSRF-Token"] = csrf
    result = await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})
    assert result.status_code == 200 and result.json()["data"]["deleted"] == 1
    assert (await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})).json()[
        "data"
    ]["deleted"] == 1
    async with store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM notifications"))["n"] == 1


async def test_cleanup_revalidates_changed_state_and_cancel_is_not_delivered(console):
    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    base = f"/console/api/platforms/{platform['id']}"
    store = fleet.entry(platform["id"]).runtime.store
    async with store.transaction() as db:
        await db.execute(
            "INSERT INTO notifications(dedupe_key,status,created_at,updated_at) VALUES('dead','dead',0,0),('waiting','pending',0,0)"
        )
    plan = (
        await http.post(base + "/cleanup-previews", json={"kind": "notifications", "ids": ["1"]})
    ).json()["data"]
    async with store.transaction() as db:
        await db.execute("UPDATE notifications SET status='sending' WHERE id=1")
    result = (await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})).json()[
        "data"
    ]
    assert result["deleted"] == 0 and result["skipped"] == 1
    cancelled = await http.post(base + "/notification-cancellations", json={"ids": [2]})
    assert cancelled.status_code == 200
    async with store.transaction() as db:
        assert (await row(db, "SELECT status FROM notifications WHERE id=2"))[
            "status"
        ] == "cancelled"
    assert (
        await http.post(
            base + "/cleanup-previews", json={"kind": "logs", "ids": [str(x) for x in range(101)]}
        )
    ).status_code == 400


async def test_retention_settings_roundtrip_without_changing_other_configuration(console):
    http, control, _ = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    base = f"/console/api/platforms/{platform['id']}"
    response = await http.patch(
        base,
        json={
            "retention": {
                "enabled": False,
                "logs_days": 7,
                "history_days": 30,
                "tasks_days": 7,
                "audits_days": 60,
            }
        },
    )
    assert response.status_code == 200
    saved = await control.platform_config(platform["id"])
    assert saved.retention.logs_days == 7 and not saved.retention.enabled
    assert saved.newapi_token.get_secret_value() == "private-admin-token"
    assert (await http.get(base + "/data-management")).json()["data"]["config"]["logs_days"] == 7


async def test_archiving_alert_preserves_dedupe_and_new_generation_is_visible(console):
    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    runtime = fleet.entry(platform["id"]).runtime
    base = f"/console/api/platforms/{platform['id']}"
    await runtime.store.condition("channel:91", True, "offline", "warning", 10)
    await runtime.store.condition("channel:91", False, "recovered", "warning", 20)
    alerts = (await http.get(base + "/alerts")).json()["data"]
    identifier = alerts["items"][0]["id"]
    plan = (
        await http.post(base + "/cleanup-previews", json={"kind": "alerts", "ids": [identifier]})
    ).json()["data"]
    assert (await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})).json()[
        "data"
    ]["deleted"] == 1
    assert (await http.get(base + "/alerts")).json()["data"]["total"] == 0
    async with runtime.store.transaction() as db:
        assert (await row(db, "SELECT generation FROM alerts WHERE id=?", (identifier,)))[
            "generation"
        ] == 1
    await runtime.store.condition("channel:91", True, "offline again", "warning", 30)
    again = (await http.get(base + "/alerts")).json()["data"]
    assert again["total"] == 1 and again["items"][0]["title"] == "offline again"


async def test_cleanup_protects_active_tasks_and_pending_bot_result(console):
    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    base = f"/console/api/platforms/{platform['id']}"
    async with fleet.entry(platform["id"]).runtime.store.transaction() as db:
        for index, status, context in [
            (1, "running", "{}"),
            (2, "complete", '{"binding_id":"bot"}'),
            (3, "complete", "{}"),
            (4, "cancelled", "{}"),
        ]:
            await db.execute(
                "INSERT INTO query_jobs(id,dedupe_key,owner_hash,status,context_json) VALUES(?,?,?,?,?)",
                (str(index), str(index), "test", status, context),
            )
    plan = (
        await http.post(base + "/cleanup-previews", json={"kind": "tasks", "before": time.time()})
    ).json()["data"]
    assert plan["count"] == 2 and plan["protected"] == 2
    result = (await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})).json()[
        "data"
    ]
    assert result["deleted"] == 2


async def test_retention_disabled_and_settings_save_do_not_immediately_delete(console):
    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    base = f"/console/api/platforms/{platform['id']}"
    async with fleet.entry(platform["id"]).runtime.store.transaction() as db:
        await db.execute("INSERT INTO logs(id,created_at) VALUES(91,0)")
    await http.patch(base, json={"retention": {"logs_days": 1}})
    runtime = fleet.entry(platform["id"]).runtime
    await runtime.prune()
    await RetentionService(runtime.store).prune(RetentionConfig(enabled=False))
    async with runtime.store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 1
    records = (await http.get(base + "/operation-records")).json()["data"]
    assert records["total"] >= 1
    assert "private-admin-token" not in str(records)


async def test_expired_preview_requires_reconfirmation_and_selected_log_delete_invalidates_coverage(
    console,
):
    import json

    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    base = f"/console/api/platforms/{platform['id']}"
    store = fleet.entry(platform["id"]).runtime.store
    async with store.transaction() as db:
        await db.execute("INSERT INTO logs(id,created_at) VALUES(1,0)")
    plan = (
        await http.post(base + "/cleanup-previews", json={"kind": "logs", "ids": ["1"]})
    ).json()["data"]
    saved = json.loads(await store.get_meta("retention:preview:" + plan["preview_id"]))
    saved["expires_at"] = 1
    await store.set_meta("retention:preview:" + plan["preview_id"], json.dumps(saved))
    assert (
        await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})
    ).status_code == 409
    plan = (
        await http.post(base + "/cleanup-previews", json={"kind": "logs", "ids": ["1"]})
    ).json()["data"]
    assert (await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})).json()[
        "data"
    ]["deleted"] == 1
    assert float(await store.get_meta("log_coverage_start")) >= time.time() - 10


async def test_parallel_settings_sections_cannot_overwrite_each_other(console):
    import asyncio

    from newapi_watch.control_models import PlatformPatch

    http, control, _ = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    await asyncio.gather(
        control.patch_platform(platform["id"], PlatformPatch(name="renamed")),
        control.patch_platform(
            platform["id"], PlatformPatch(retention=RetentionConfig(logs_days=7))
        ),
    )
    saved = await control.platform_config(platform["id"])
    assert saved.name == "renamed"
    assert saved.retention.logs_days == 7


async def test_automatic_retention_drains_more_than_three_manual_batches(console):
    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    store = fleet.entry(platform["id"]).runtime.store
    async with store.transaction() as db:
        await db.executemany(
            "INSERT INTO logs(id,created_at) VALUES(?,0)", [(i,) for i in range(1, 701)]
        )
    result = await RetentionService(store).prune(RetentionConfig(logs_days=1))
    assert result["deleted"]["logs"] == 700
    async with store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 0


async def test_cleanup_does_not_delete_a_retried_notification_changed_after_preview(console):
    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    base = f"/console/api/platforms/{platform['id']}"
    store = fleet.entry(platform["id"]).runtime.store
    async with store.transaction() as db:
        await db.execute(
            "INSERT INTO notifications(dedupe_key,status,created_at,updated_at) VALUES('retry','dead',0,0)"
        )
    plan = (
        await http.post(
            base + "/cleanup-previews", json={"kind": "notifications", "before": time.time() - 10}
        )
    ).json()["data"]
    async with store.transaction() as db:
        await db.execute(
            "UPDATE notifications SET status='sent',updated_at=? WHERE id=1", (time.time(),)
        )
    result = (await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})).json()[
        "data"
    ]
    assert result["deleted"] == 0 and result["skipped"] == 1


async def test_archived_error_event_reappears_when_same_bucket_reports_new_error(console):
    from newapi_watch.monitoring.service import record_alert

    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    base = f"/console/api/platforms/{platform['id']}"
    store = fleet.entry(platform["id"]).runtime.store
    async with store.transaction() as db:
        await record_alert(db, "request_error:bucket", "first", True, "warning", 10)
        await db.execute("UPDATE alerts SET state='event' WHERE key='request_error:bucket'")
    identifier = (await http.get(base + "/alerts")).json()["data"]["items"][0]["id"]
    plan = (
        await http.post(base + "/cleanup-previews", json={"kind": "alerts", "ids": [identifier]})
    ).json()["data"]
    await http.post(base + "/cleanups", json={"preview_id": plan["preview_id"]})
    async with store.transaction() as db:
        await record_alert(db, "request_error:bucket", "new error", True, "warning", 20)
        await db.execute("UPDATE alerts SET state='event' WHERE key='request_error:bucket'")
    current = (await http.get(base + "/alerts")).json()["data"]
    assert current["total"] == 1 and current["items"][0]["title"] == "new error"


async def test_upgrade_preserves_previously_unbounded_history_until_strategy_confirmed(console):
    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    store = fleet.entry(platform["id"]).runtime.store
    async with store.transaction() as db:
        await db.execute(
            "INSERT INTO notifications(dedupe_key,status,created_at,updated_at) VALUES('legacy-dead','dead',0,0),('legacy-sent','sent',0,0)"
        )
    await RetentionService(store).prune(RetentionConfig())
    async with store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM notifications WHERE status='dead'"))[
            "n"
        ] == 1
        assert (await row(db, "SELECT COUNT(*) AS n FROM notifications WHERE status='sent'"))[
            "n"
        ] == 0
    base = f"/console/api/platforms/{platform['id']}"
    await http.patch(base, json={"retention": {"enabled": True, "history_days": 30}})
    saved = (await http.get(base + "/data-management")).json()["data"]["config"]
    assert saved["confirmed"] is True
    await RetentionService(fleet.entry(platform["id"]).runtime.store).prune(
        RetentionConfig.model_validate(saved)
    )
    async with fleet.entry(platform["id"]).runtime.store.transaction() as db:
        assert (await row(db, "SELECT COUNT(*) AS n FROM notifications"))["n"] == 0
