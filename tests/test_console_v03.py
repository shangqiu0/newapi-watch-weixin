import json

import httpx
import pytest

from newapi_watch.auth import Auth
from newapi_watch.console_api import create_console
from newapi_watch.control_store import ControlStore
from newapi_watch.fleet import Fleet


@pytest.fixture
async def console(tmp_path):
    control = ControlStore(tmp_path / "control")
    await control.open()
    auth = Auth(control, secure_cookie=False)
    await auth.provision("operator", "console-test-password-123456")
    fleet = Fleet(control, demo=True)
    await fleet.open(start_tasks=False)
    app = create_console(control, fleet, auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        try:
            yield http, control, fleet
        finally:
            await fleet.close()
            await control.close()


async def login(http):
    response = await http.post(
        "/console/api/login",
        json={"username": "operator", "password": "console-test-password-123456"},
    )
    assert response.status_code == 200
    http.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
    return response


def config(name):
    return {
        "name": name,
        "newapi_url": "https://relay.example",
        "newapi_token": "private-admin-token",
        "newapi_user_id": 1,
        "enabled": True,
    }


async def test_login_has_no_registration_cookie_is_httponly_and_csrf_required(console):
    http, _, _ = console
    assert (await http.get("/console/api/platforms")).status_code == 401
    assert (await http.post("/console/api/register", json={})).status_code == 404
    response = await login(http)
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    token = http.headers.pop("X-CSRF-Token")
    assert (await http.post("/console/api/platforms", json=config("A"))).status_code == 403
    http.headers["X-CSRF-Token"] = token
    assert (
        await http.post(
            "/console/api/platforms", json=config("A"), headers={"Origin": "https://evil.example"}
        )
    ).status_code == 403
    assert (await http.get("/console/api/session")).json()["data"]["csrf_token"] == token
    assert (await http.post("/console/api/logout")).status_code == 200
    assert (await http.get("/console/api/platforms")).status_code == 401


async def test_web_creates_two_isolated_platforms_and_never_returns_secrets(console):
    from newapi_watch.models import Channel

    http, control, fleet = console
    await login(http)
    a = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    b = (await http.post("/console/api/platforms", json=config("B"))).json()["data"]
    assert "private-admin-token" not in json.dumps(a)
    await fleet.entries[a["id"]].runtime.store.sync_channels(
        [Channel(id=1, name="ONLY_A", status=1)], 100
    )
    await fleet.entries[b["id"]].runtime.store.sync_channels(
        [Channel(id=1, name="ONLY_B", status=1)], 100
    )
    response = await http.get(f"/console/api/platforms/{a['id']}/overview")
    assert (
        response.status_code == 200 and "ONLY_A" in response.text and "ONLY_B" not in response.text
    )
    assert (
        await http.get("/api/v1/channels", headers={"X-API-Key": "anything"})
    ).status_code == 404
    assert (await http.get("/console/api/platforms/not-found/overview")).status_code == 404


async def test_binding_model_configuration_and_chat_are_functional(console):
    http, control, fleet = console
    await login(http)
    a = (await http.post("/console/api/platforms", json=config("A"))).json()["data"]
    b = (await http.post("/console/api/platforms", json=config("B"))).json()["data"]
    binding = (
        await http.post(
            f"/console/api/platforms/{a['id']}/bindings",
            json={
                "name": "bot-A",
                "kind": "bot",
                "bot_id": "test-id",
                "bot_secret": "super-secret",
            },
        )
    ).json()["data"]
    assert "super-secret" not in json.dumps(binding)
    cross = await http.patch(
        f"/console/api/platforms/{b['id']}/bindings/{binding['id']}", json={"enabled": False}
    )
    assert cross.status_code == 404
    duplicate = await http.post(
        f"/console/api/platforms/{b['id']}/bindings",
        json={
            "name": "duplicate",
            "kind": "bot",
            "bot_id": "test-id",
            "bot_secret": "super-secret",
        },
    )
    assert duplicate.status_code == 409
    patch = await http.patch(
        f"/console/api/platforms/{a['id']}",
        json={
            "llm": {
                "enabled": True,
                "base_url": "https://model.example/v1",
                "model": "test-model",
                "api_key": "model-secret",
            }
        },
    )
    assert patch.status_code == 200 and patch.json()["data"]["llm"]["has_api_key"]
    assert "model-secret" not in patch.text
    await http.patch(
        f"/console/api/platforms/{a['id']}",
        json={
            "llm": {
                "enabled": False,
                "base_url": "https://model.example/v1",
                "model": "test-model",
                "api_key": "",
            }
        },
    )
    chat = await http.post(
        f"/console/api/platforms/{a['id']}/queries", json={"text": "你有哪些功能？"}
    )
    assert chat.status_code == 200 and "A" in chat.json()["data"]["text"]
    assert (await http.get("/")).status_code == 200


async def test_model_checks_show_401_then_use_edited_web_key(console):
    http, control, fleet = console
    await login(http)
    a = (await http.post("/console/api/platforms", json=config("Web配置真实入口"))).json()["data"]
    calls = []

    def provider(request):
        calls.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer rejected-key":
            return httpx.Response(401, json={"error": {"message": "private-provider-body"}})
        payload = json.loads(request.content)
        if payload.get("tools"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "test",
                                        "type": "function",
                                        "function": {"name": "get_capabilities", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到"}}]})

    original = fleet.llm_http
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
        fleet.llm_http = model_http
        try:
            route = f"/console/api/platforms/{a['id']}"
            await http.patch(
                route,
                json={
                    "llm": {
                        "enabled": True,
                        "base_url": "https://model.example/v1",
                        "api_key": "rejected-key",
                        "model": "test",
                    }
                },
            )
            bad = await http.post(route + "/model-checks")
            assert bad.status_code == 200 and bad.json()["data"]["code"] == "authentication_failed"
            assert "private-provider-body" not in bad.text
            await http.patch(
                route,
                json={
                    "llm": {
                        "enabled": True,
                        "base_url": "https://model.example/v1",
                        "api_key": "edited-key",
                        "model": "test",
                    }
                },
            )
            good = await http.post(route + "/model-checks")
            assert good.json()["data"]["ok"] is True
            assert fleet.entry(a["id"]).assistant.config.api_key.get_secret_value() == "edited-key"
            overview = await http.get(route + "/overview")
            assert overview.json()["data"]["model_status"]["code"] == "ready"
            assert "edited-key" not in overview.text
        finally:
            fleet.llm_http = original
    assert calls[0] == "Bearer rejected-key" and calls[-1] == "Bearer edited-key"


async def test_model_config_change_invalidates_old_diagnostic_and_keeps_blank_key(console):
    http, control, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("诊断失效"))).json()["data"]
    route = f"/console/api/platforms/{platform['id']}"
    payload = {
        "enabled": True,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "api_key": "private-test-key",
        "compatibility": "deepseek",
    }
    response = await http.patch(route, json={"llm": payload})
    assert response.json()["data"]["llm"]["compatibility"] == "deepseek"
    await fleet.entry(platform["id"]).runtime.store.set_meta(
        "llm_diagnostic", json.dumps({"ok": True, "code": "ready"})
    )
    await http.patch(route, json={"name": "只改名称"})
    assert (await http.get(route + "/overview")).json()["data"]["model_status"]["ok"] is True
    changed = await http.patch(
        route, json={"llm": {**payload, "model": "changed-model", "api_key": ""}}
    )
    assert changed.status_code == 200 and "private-test-key" not in changed.text
    assert (
        await control.platform_config(platform["id"])
    ).llm.api_key.get_secret_value() == "private-test-key"
    assert (await http.get(route + "/overview")).json()["data"]["model_status"] is None


async def test_model_probe_does_not_store_stale_result_after_edit_during_request(console):
    import asyncio

    http, _, fleet = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("并发模型配置"))).json()[
        "data"
    ]
    route = f"/console/api/platforms/{platform['id']}"
    old = {
        "enabled": True,
        "base_url": "https://model.example/v1",
        "api_key": "old-test-key",
        "model": "before",
    }
    await http.patch(route, json={"llm": old})
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(request):
        payload = json.loads(request.content)
        if not payload.get("tools"):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"choices": [{"message": {"content": "确认"}}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "cap",
                                    "type": "function",
                                    "function": {"name": "get_capabilities", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
        )

    original = fleet.llm_http
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
        fleet.llm_http = model_http
        task = asyncio.create_task(http.post(route + "/model-checks"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await http.patch(route, json={"llm": {**old, "model": "after"}})
            release.set()
            response = await asyncio.wait_for(task, 5)
            assert response.status_code == 409
            assert (await http.get(route + "/overview")).json()["data"]["model_status"] is None
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            fleet.llm_http = original


async def test_model_compatibility_rejects_unknown_mode_without_echoing_secret(console):
    http, _, _ = console
    await login(http)
    platform = (await http.post("/console/api/platforms", json=config("校验"))).json()["data"]
    response = await http.patch(
        f"/console/api/platforms/{platform['id']}",
        json={"llm": {"compatibility": "unknown", "api_key": "never-echo-this"}},
    )
    assert response.status_code == 400 and "never-echo-this" not in response.text


async def test_console_serves_framework_spa_and_all_entry_assets(console):
    import re

    http, _, _ = console
    response = await http.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assets = re.findall(r'(?:src|href)="(/assets/[^" ]+)"', response.text)
    assert len(assets) >= 2
    for path in assets:
        asset = await http.get(path)
        assert asset.status_code == 200 and len(asset.content) > 100
    assert (await http.get("/console/channels")).text == response.text
    assert (await http.get("/console/api/nonexistent")).status_code == 404
