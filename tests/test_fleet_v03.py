import httpx
import pytest

from newapi_watch.control_models import BindingInput, PlatformInput, PlatformPatch
from newapi_watch.control_store import ControlStore
from newapi_watch.fleet import Fleet
from newapi_watch.models import Channel
from newapi_watch.targets import Targets


@pytest.fixture
async def control(tmp_path):
    control = ControlStore(tmp_path / "control")
    await control.open()
    try:
        yield control
    finally:
        await control.close()


async def test_same_channel_ids_are_physically_isolated_and_reload_is_targeted(control):
    a = await control.create_platform(
        PlatformInput(
            name="A", newapi_url="https://a.example", newapi_token="key-a", newapi_user_id=1
        )
    )
    b = await control.create_platform(
        PlatformInput(
            name="B", newapi_url="https://b.example", newapi_token="key-b", newapi_user_id=1
        )
    )
    fleet = Fleet(control, demo=True)
    await fleet.open(start_tasks=False)
    try:
        first = fleet.entries[a["id"]].runtime
        other = fleet.entries[b["id"]].runtime
        await first.store.sync_channels([Channel(id=1, name="private-A", status=1)], 100)
        await other.store.sync_channels([Channel(id=1, name="private-B", status=3)], 100)
        assert (await first.commands.data.channels(live=False))["items"][0]["name"] == "private-A"
        assert (await other.commands.data.channels(live=False))["items"][0]["name"] == "private-B"
        assert first.store.path != other.store.path
        await control.patch_platform(a["id"], PlatformPatch(name="renamed"))
        await fleet.reload(a["id"])
        assert fleet.entries[b["id"]].runtime is other
        assert (await fleet.entries[a["id"]].runtime.commands.data.channels(live=False))["items"][
            0
        ]["name"] == "private-A"
    finally:
        await fleet.close()


async def test_multiple_webhooks_partial_failure_does_not_repeat_success(control):
    p = await control.create_platform(
        PlatformInput(
            name="A", newapi_url="https://a.example", newapi_token="key", newapi_user_id=1
        )
    )
    for key in ("one", "two"):
        await control.create_binding(
            p["id"],
            BindingInput(
                name=key,
                kind="webhook",
                webhook_url=f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
            ),
        )
    seen = []
    failed = False

    def handler(request):
        nonlocal failed
        key = request.url.params["key"]
        seen.append(key)
        if key == "two" and not failed:
            failed = True
            return httpx.Response(200, json={"errcode": 45009})
        return httpx.Response(200, json={"errcode": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        targets = Targets(control, p["id"], "A", http)
        with pytest.raises(RuntimeError):
            await targets.send_for(1, "alert")
        from newapi_watch.control_models import BindingPatch

        first_target = next(b for b in await control.bindings(p["id"]) if b["name"] == "one")
        await control.patch_binding(p["id"], first_target["id"], BindingPatch(name="renamed"))
        await targets.send_for(1, "alert")
    assert seen.count("one") == 1 and seen.count("two") == 2


async def test_platform_disabled_does_not_start_background_or_bot_tasks(control):
    p = await control.create_platform(
        PlatformInput(
            name="A",
            newapi_url="https://a.example",
            newapi_token="key",
            newapi_user_id=1,
            enabled=False,
        )
    )
    fleet = Fleet(control, demo=True)
    await fleet.open()
    try:
        assert not fleet.entries[p["id"]].runtime.tasks
        assert not fleet.entries[p["id"]].bots
    finally:
        await fleet.close()


async def test_platform_runtime_never_inherits_legacy_environment(control, monkeypatch):
    monkeypatch.setenv("WATCH_BOT_ID", "unrelated-legacy-bot")
    monkeypatch.setenv("WATCH_BOT_SECRET", "do-not-connect")
    monkeypatch.setenv(
        "WATCH_GROUP_WEBHOOK", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=do-not-send"
    )
    p = await control.create_platform(
        PlatformInput(
            name="A", newapi_url="https://a.example", newapi_token="key", newapi_user_id=1
        )
    )
    fleet = Fleet(control, demo=True)
    await fleet.open(start_tasks=False)
    try:
        settings = fleet.entry(p["id"]).runtime.settings
        assert settings.bot_id == "" and settings.group_webhook.get_secret_value() == ""
    finally:
        await fleet.close()


async def test_platform_health_uses_enabled_webhook_bindings_instead_of_legacy_env(control):
    from newapi_watch.control_models import BindingPatch

    platform = await control.create_platform(
        PlatformInput(
            name="绑定健康",
            newapi_url="https://health.example",
            newapi_token="test-only",
            newapi_user_id=1,
            enabled=True,
        )
    )
    binding = await control.create_binding(
        platform["id"],
        BindingInput(
            name="告警",
            kind="webhook",
            webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=not-real",
        ),
    )
    fleet = Fleet(control)
    await fleet.open(start_tasks=False)
    try:

        async def snapshot():
            runtime = fleet.entry(platform["id"]).runtime
            await runtime.store.set_meta("channels_last_ok", "1000")
            await runtime.store.set_meta("logs_last_ok", "1000")
            return await runtime.commands.health.snapshot(now=1000)

        ready = await snapshot()
        assert ready["notifications_configured"] is True
        assert ready["status"] == "ok" and "notifications_unconfigured" not in ready["reasons"]
        await control.patch_binding(platform["id"], binding["id"], BindingPatch(enabled=False))
        await fleet.reload(platform["id"])
        disabled = await snapshot()
        assert (
            disabled["notifications_configured"] is False
            and "notifications_unconfigured" in disabled["reasons"]
        )
    finally:
        await fleet.close()


async def test_platform_health_tracks_bound_bot_authentication_dynamically(control):
    from types import SimpleNamespace

    platform = await control.create_platform(
        PlatformInput(
            name="机器人健康",
            newapi_url="https://bot-health.example",
            newapi_token="test-only",
            newapi_user_id=1,
            enabled=True,
        )
    )
    await control.create_binding(
        platform["id"],
        BindingInput(name="bot", kind="bot", bot_id="test-bot", bot_secret="not-real"),
    )
    fleet = Fleet(control)
    await fleet.open(start_tasks=False)
    try:
        entry = fleet.entry(platform["id"])
        disconnected = await entry.runtime.commands.health.snapshot()
        assert disconnected["bot"].get("configured") == 1
        assert (
            disconnected["bot"]["status"] == "disconnected"
            and "bot_disconnected" in disconnected["reasons"]
        )
        # Only the external SDK connection state is replaced, not Health logic.
        connection = SimpleNamespace(authenticated=True)
        entry.bots.append(connection)
        ready = await entry.runtime.commands.health.snapshot()
        assert ready["bot"]["authenticated"] == 1 and ready["bot"]["status"] == "authenticated"
        assert "bot_disconnected" not in ready["reasons"]
        connection.authenticated = False
        assert (await entry.runtime.commands.health.snapshot())["bot"]["status"] == "disconnected"
        await control.patch_platform(platform["id"], PlatformPatch(enabled=False))
        await fleet.reload(platform["id"])
        paused = await fleet.entry(platform["id"]).runtime.commands.health.snapshot()
        assert paused["bot"]["status"] == "disabled" and "bot_disconnected" not in paused["reasons"]
    finally:
        await fleet.close()
