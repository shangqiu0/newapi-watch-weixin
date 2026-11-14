import json

from test_console_v05 import api as _console_fixture
from test_console_v05 import ready

api = _console_fixture


async def test_upstream_account_api_is_scoped_encrypted_and_preserves_empty_token(api):
    http, control, fleet = api
    assert (await http.get("/console/api/platforms/x/upstream-accounts")).status_code == 401
    platform = await ready(api)
    path = f"/console/api/platforms/{platform['id']}/upstream-accounts"
    created = await http.post(
        path,
        json={
            "name": "supplier",
            "base_url": "https://upstream.invalid",
            "user_id": 9,
            "access_token": "ordinary-user-test-secret",
            "enabled": True,
        },
    )
    assert created.status_code == 201
    item = created.json()["data"]
    assert item["has_access_token"] and "access_token" not in item
    assert "ordinary-user-test-secret" not in json.dumps(await control.platform(platform["id"]))
    changed = await http.patch(
        path + "/" + item["id"], json={"name": "renamed", "access_token": ""}
    )
    assert changed.status_code == 200
    private = (await control.platform_config(platform["id"])).upstream_accounts[0]
    assert private.access_token.get_secret_value() == "ordinary-user-test-secret"
    token = http.headers.pop("X-CSRF-Token")
    assert (await http.delete(path + "/" + item["id"])).status_code == 403
    http.headers["X-CSRF-Token"] = token
    assert (await http.delete(path + "/" + item["id"])).status_code == 200
    assert (await http.get(path)).json()["data"]["total"] == 0


async def test_currency_patch_roundtrips_decimal_and_does_not_change_old_quota_threshold(api):
    http, control, _ = api
    platform = await ready(api)
    route = f"/console/api/platforms/{platform['id']}"
    payload = (await http.get(route + "/monitoring")).json()["data"]["config"]
    payload["balances"] = [{"user_id": 1, "threshold": 123456789, "enabled": False}]
    assert (await http.put(route + "/monitoring", json=payload)).status_code == 200
    saved = await http.patch(
        route,
        json={
            "currency": {"confirmed": True, "quota_per_unit": "500000", "usd_exchange_rate": "7.3"}
        },
    )
    assert saved.status_code == 200
    current = await control.platform_config(platform["id"])
    assert current.monitoring.balances[0].threshold == 123456789
    data = (await http.get(route + "/monitoring")).json()["data"]
    assert data["config"]["balances"][0]["threshold_display"].startswith("¥")


async def test_upstream_bindings_must_belong_to_same_platform(api):
    http, _, _ = api
    a = await ready(api, "A")
    b = await ready(api, "B")
    hook = (
        await http.post(
            f"/console/api/platforms/{b['id']}/bindings",
            json={
                "name": "B hook",
                "kind": "webhook",
                "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-only",
            },
        )
    ).json()["data"]
    result = await http.post(
        f"/console/api/platforms/{a['id']}/upstream-accounts",
        json={
            "name": "bad",
            "base_url": "https://up.invalid",
            "user_id": 7,
            "access_token": "secret-test",
            "query_binding_ids": [hook["id"]],
        },
    )
    assert result.status_code == 400


async def test_cny_threshold_write_only_changes_explicitly_edited_amounts(api):
    http, _, _ = api
    platform = await ready(api)
    route = f"/console/api/platforms/{platform['id']}"
    assert (
        await http.patch(
            route,
            json={
                "currency": {
                    "confirmed": True,
                    "quota_per_unit": "500000",
                    "usd_exchange_rate": "7.3",
                }
            },
        )
    ).status_code == 200
    payload = (await http.get(route + "/monitoring")).json()["data"]["config"]
    payload["balances"] = [{"user_id": 1, "threshold": 123456789, "enabled": False}]
    assert (await http.put(route + "/monitoring", json=payload)).status_code == 200
    data = (await http.get(route + "/monitoring")).json()["data"]["config"]
    data["balances"][0]["name"] = "only rename"
    assert (await http.put(route + "/monitoring", json=data)).status_code == 200
    current = (await http.get(route + "/monitoring")).json()["data"]["config"]
    assert current["balances"][0]["threshold"] == 123456789
    current["balance_amounts"] = {"1": {"threshold_cny": "100.01", "recovery_threshold_cny": "120"}}
    assert (await http.put(route + "/monitoring", json=current)).status_code == 200
    current = (await http.get(route + "/monitoring")).json()["data"]["config"]
    assert current["balances"][0]["threshold"] == 6850000


async def test_referenced_bot_cannot_lose_its_balance_delivery_destination(api):
    http, _, _ = api
    platform = await ready(api)
    route = f"/console/api/platforms/{platform['id']}"
    bot = (
        await http.post(
            route + "/bindings",
            json={
                "kind": "bot",
                "name": "receiver",
                "bot_id": "test-bot",
                "bot_secret": "test-secret",
                "notification_chat_id": "chat-only",
            },
        )
    ).json()["data"]
    created = await http.post(
        route + "/upstream-accounts",
        json={
            "name": "supplier",
            "base_url": "https://up.invalid",
            "user_id": 7,
            "access_token": "test-key",
            "notify_binding_ids": [bot["id"]],
        },
    )
    assert created.status_code == 201
    assert (
        await http.patch(route + "/bindings/" + bot["id"], json={"notification_chat_id": ""})
    ).status_code == 409
    assert (await http.delete(route + "/bindings/" + bot["id"])).status_code == 409
