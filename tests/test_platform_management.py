"""Platform lifecycle tests use real registry/runtime/SQLite and no live integrations."""

import asyncio
import sqlite3
from contextlib import closing
from importlib.resources import files

import httpx
import pytest

from newapi_watch.auth import Auth
from newapi_watch.console_api import create_console
from newapi_watch.control_models import BindingInput, PlatformInput, PlatformPatch
from newapi_watch.control_store import ControlError, ControlStore
from newapi_watch.fleet import Fleet
from newapi_watch.persistence import row
from newapi_watch.vault import Vault


@pytest.fixture
async def management(tmp_path):
    control = ControlStore(tmp_path / "console")
    await control.open()
    auth = Auth(control, secure_cookie=False)
    await auth.provision("admin", "local-test-password-1234")
    fleet = Fleet(control, demo=True)
    await fleet.open(start_tasks=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console(control, fleet, auth)),
        base_url="http://local",
    ) as http:
        try:
            yield http, control, fleet
        finally:
            await fleet.close()
            await control.close()


async def sign_in(http):
    login = await http.post(
        "/console/api/login", json={"username": "admin", "password": "local-test-password-1234"}
    )
    assert login.status_code == 200
    http.headers["X-CSRF-Token"] = login.json()["data"]["csrf_token"]


def config(name):
    return PlatformInput(
        name=name,
        newapi_url=f"https://{name.lower()}.invalid",
        newapi_token="private-platform-token",
        newapi_user_id=1,
        enabled=True,
    )


async def add(control, fleet, name):
    saved = await control.create_platform(config(name))
    await fleet.reload(saved["id"])
    return saved


async def test_platform_delete_requires_cookie_and_csrf(management):
    http, control, fleet = management
    platform = await add(control, fleet, "A")
    route = f"/console/api/platforms/{platform['id']}"
    assert (await http.delete(route)).status_code == 401
    await sign_in(http)
    token = http.headers.pop("X-CSRF-Token")
    assert (await http.delete(route)).status_code == 403
    http.headers["X-CSRF-Token"] = token
    assert (
        await http.delete(route, headers={"Origin": "https://other.invalid"})
    ).status_code == 403
    assert (await control.platform(platform["id"]))["enabled"] is True


async def test_delete_stops_only_target_and_preserves_history_and_secrets(management):
    http, control, fleet = management
    await sign_in(http)
    a, b = await add(control, fleet, "A"), await add(control, fleet, "B")
    bot = await control.create_binding(
        a["id"],
        BindingInput(
            name="bot-A", kind="bot", bot_id="bot-test-123", bot_secret="private-bot-secret"
        ),
    )
    entry, other = fleet.entry(a["id"]), fleet.entry(b["id"])
    path = entry.runtime.store.path
    await entry.runtime.store.enqueue("pending-test", "keep-history", 1000)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def background():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(background())
    entry.bot_tasks.append(task)
    await started.wait()
    response = await http.delete(f"/console/api/platforms/{a['id']}?revision={a['revision']}")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "id": a["id"],
        "deleted": True,
        "history_retained": True,
        "duplicate": False,
    }
    assert "private-" not in response.text
    assert task.done() and cancelled.is_set() and entry.runtime.stop.is_set()
    assert fleet.entry(b["id"]) is other and a["id"] not in fleet.entries
    assert path.exists()
    with closing(sqlite3.connect(path)) as history:
        assert history.execute(
            "SELECT content,status FROM notifications WHERE dedupe_key='pending-test'"
        ).fetchone() == ("keep-history", "pending")
    async with control.transaction() as db:
        archived = await row(db, "SELECT enabled,deleted_at FROM platforms WHERE id=?", (a["id"],))
        binding = await row(db, "SELECT enabled FROM bindings WHERE id=?", (bot["id"],))
    assert archived["enabled"] == 0 and archived["deleted_at"] > 0 and binding["enabled"] == 0
    # Archived bots must no longer reserve the globally-unique enabled bot ID.
    await control.create_binding(
        b["id"],
        BindingInput(name="rebind", kind="bot", bot_id="bot-test-123", bot_secret="new-bot-secret"),
    )


async def test_archived_platform_is_hidden_idempotent_and_not_restarted(management):
    http, control, fleet = management
    await sign_in(http)
    platform = await add(control, fleet, "A")
    route = f"/console/api/platforms/{platform['id']}"
    assert (await http.delete(route)).status_code == 200
    again = await http.delete(route)
    assert again.status_code == 200 and again.json()["data"]["duplicate"] is True
    assert (await http.get("/console/api/platforms")).json()["data"] == []
    for suffix in ("/overview", "/channels", "/monitoring", "/bindings", "/notifications"):
        assert (await http.get(route + suffix)).status_code == 404
    assert (await http.patch(route, json={"enabled": True})).status_code == 404
    assert (
        await http.post(
            route + "/bindings",
            json={"kind": "bot", "name": "blocked", "bot_id": "x", "bot_secret": "test"},
        )
    ).status_code == 404
    restarted = Fleet(control, demo=True)
    try:
        await restarted.open(start_tasks=False)
        assert not restarted.entries
    finally:
        await restarted.close()


async def test_delete_unknown_stale_or_invalid_revision_never_changes_other_platform(management):
    http, control, fleet = management
    await sign_in(http)
    platform = await add(control, fleet, "A")
    route = f"/console/api/platforms/{platform['id']}"
    assert (await http.delete("/console/api/platforms/does-not-exist")).status_code == 404
    assert (await http.delete(route + "?revision=0")).status_code == 400
    assert (await http.delete(route + "?revision=999")).status_code == 409
    assert fleet.entry(platform["id"]).enabled
    assert len(await control.platforms()) == 1


async def test_platform_editor_fields_keep_blank_token_model_and_monitor_settings(management):
    http, control, fleet = management
    await sign_in(http)
    a, b = await add(control, fleet, "A"), await add(control, fleet, "B")
    prior = await control.platform_config(a["id"])
    other = fleet.entry(b["id"])
    response = await http.patch(
        f"/console/api/platforms/{a['id']}",
        json={
            "name": "Renamed",
            "newapi_token": "",
            "newapi_user_id": 7,
            "timezone": "UTC",
            "enabled": False,
        },
    )
    assert response.status_code == 200 and response.json()["data"]["name"] == "Renamed"
    after = await control.platform_config(a["id"])
    assert (
        after.newapi_token == prior.newapi_token
        and after.monitoring == prior.monitoring
        and after.llm == prior.llm
    )
    assert after.newapi_user_id == 7 and after.timezone == "UTC" and not after.enabled
    assert fleet.entry(b["id"]) is other
    assert "private-platform-token" not in response.text
    assert (
        await http.patch(
            f"/console/api/platforms/{a['id']}", json={"newapi_url": "https://another.invalid"}
        )
    ).status_code == 409


async def test_late_platform_save_cannot_resurrect_deleted_record(management, monkeypatch):
    _, control, fleet = management
    platform = await add(control, fleet, "A")
    entered, release = asyncio.Event(), asyncio.Event()
    original = control.validate_monitoring

    async def blocked_validation(identifier, monitoring):
        await original(identifier, monitoring)
        entered.set()
        await release.wait()

    monkeypatch.setattr(control, "validate_monitoring", blocked_validation)
    save = asyncio.create_task(
        control.patch_platform(platform["id"], PlatformPatch(name="late", enabled=True))
    )
    await entered.wait()
    try:
        await fleet.delete_platform(platform["id"])
    finally:
        release.set()
    with pytest.raises(ControlError) as error:
        await save
    assert error.value.status == 404
    assert await control.platforms() == []


async def test_registry_v1_upgrade_preserves_existing_encrypted_config_and_reopens(tmp_path):
    directory = tmp_path / "v1"
    directory.mkdir()
    encoded = Vault(directory / "master.key", allow_create=True).seal(config("A"))
    with closing(sqlite3.connect(directory / "control.sqlite3")) as db:
        db.executescript(files("newapi_watch").joinpath("registry.sql").read_text("utf-8-sig"))
        db.execute(
            "INSERT INTO platforms(id,name,enabled,config,created_at,updated_at) VALUES('old','A',1,?,1,1)",
            (encoded,),
        )
        db.commit()
    control = ControlStore(directory)
    await control.open()
    try:
        assert (await control.platform("old"))["name"] == "A"
        await control.archive_platform("old")
    finally:
        await control.close()
    await control.open()
    try:
        assert await control.platforms() == []
        async with control.transaction() as db:
            assert (await row(db, "PRAGMA user_version"))["user_version"] == 2
            assert (await row(db, "SELECT config FROM platforms WHERE id='old'"))[
                "config"
            ] == encoded
    finally:
        await control.close()
