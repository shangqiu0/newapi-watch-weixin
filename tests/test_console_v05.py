import httpx
import pytest

from newapi_watch.auth import Auth
from newapi_watch.console_api import create_console
from newapi_watch.control_models import PlatformInput
from newapi_watch.control_store import ControlStore
from newapi_watch.fleet import Fleet


@pytest.fixture
async def api(tmp_path):
    control = ControlStore(tmp_path / "control")
    await control.open()
    auth = Auth(control, secure_cookie=False)
    await auth.provision("op", "only-test-password-123456")
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


async def ready(api, name="A"):
    http, control, fleet = api
    login = await http.post(
        "/console/api/login", json={"username": "op", "password": "only-test-password-123456"}
    )
    http.headers["X-CSRF-Token"] = login.json()["data"]["csrf_token"]
    p = await control.create_platform(
        PlatformInput(
            name=name,
            newapi_url=f"https://{name.lower()}.invalid",
            newapi_token="secret-" + name,
            newapi_user_id=1,
            enabled=True,
        )
    )
    await fleet.reload(p["id"])
    return p


async def test_new_monitoring_api_is_authenticated_and_csrf_protected(api):
    http, _, _ = api
    assert (await http.get("/console/api/platforms/missing/monitoring")).status_code == 401
    p = await ready(api)
    route = f"/console/api/platforms/{p['id']}"
    current = (await http.get(route + "/monitoring")).json()["data"]["config"]
    token = http.headers.pop("X-CSRF-Token")
    assert (await http.put(route + "/monitoring", json=current)).status_code == 403
    http.headers["X-CSRF-Token"] = token
    assert (await http.get("/console/api/platforms/missing/channels")).status_code == 404


async def test_monitors_roundtrip_separate_user_thresholds_and_platform_config(api):
    http, control, fleet = api
    p = await ready(api)
    b = await control.create_platform(
        PlatformInput(
            name="B", newapi_url="https://b.invalid", newapi_token="secret-B", newapi_user_id=1
        )
    )
    await fleet.reload(b["id"])
    route = f"/console/api/platforms/{p['id']}"
    config = (await http.get(route + "/monitoring")).json()["data"]["config"]
    config["errors"].update(enabled=True, status_codes=[400, 500], cooldown_seconds=60)
    config["balances"] = [
        {"user_id": 1, "threshold": 1234, "enabled": True},
        {"user_id": 2, "threshold": 9876, "enabled": False},
    ]
    config["weekly"]["enabled"] = True
    response = await http.put(route + "/monitoring", json=config)
    assert response.status_code == 200
    saved = (await http.get(route + "/monitoring")).json()["data"]["config"]
    assert saved["balances"][1]["threshold"] == 9876 and saved["weekly"]["enabled"]
    other = (await http.get(f"/console/api/platforms/{b['id']}/monitoring")).json()["data"][
        "config"
    ]
    assert other["balances"] == [] and not other["errors"]["enabled"]
    assert "secret-A" not in response.text
    config["errors"]["template"] = "{channel.__class__}"
    assert (await http.put(route + "/monitoring", json=config)).status_code == 400


async def test_extension_bad_period_page_and_status_are_400_not_500(api):
    http, _, _ = api
    p = await ready(api)
    route = f"/console/api/platforms/{p['id']}"
    for suffix in [
        "/statistics?period=近32天",
        "/logs?period=not-a-date",
        "/channels?page=0",
        "/channels?status=999",
    ]:
        assert (await http.get(route + suffix)).status_code == 400, suffix


async def test_channel_stats_source_and_pages_query_bound_platform_not_other(api):
    http, control, fleet = api
    p = await ready(api)
    route = f"/console/api/platforms/{p['id']}"
    calls = []

    def upstream(request):
        calls.append(request.headers["Authorization"])
        page = int(request.url.params.get("p", "1"))
        items = [{"id": i, "name": f"c{i}", "status": 1} for i in range(1, 105)]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"items": items[(page - 1) * 100 : page * 100], "total": 104},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        fleet.entry(p["id"]).runtime.commands.data.client.http = client
        result = (await http.get(route + "/channels?page=6&page_size=20")).json()["data"]
        assert result["total"] == 104 and [r["id"] for r in result["items"]] == [101, 102, 103, 104]
        assert result["summary"]["enabled"] == 104 and result["source"] == "newapi_admin_api"
        assert calls == ["Bearer secret-A"] * 2


async def test_unknown_api_does_not_return_spa_html_and_history_refresh_works(api):
    http, _, _ = api
    assert (await http.get("/console/api/does-not-exist")).status_code == 404
    response = await http.get("/console/channels")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert (await http.post("/console/unknown")).status_code == 404
    assert (await http.get("/error/404")).status_code == 200
    assert (await http.get("/error/123")).status_code == 404


async def test_binding_subscriptions_and_format_persist_without_leaking_webhook(api):
    http, control, _ = api
    p = await ready(api)
    route = f"/console/api/platforms/{p['id']}"
    secret = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=not-real"
    response = await http.post(
        route + "/bindings",
        json={
            "name": "错误群",
            "kind": "webhook",
            "webhook_url": secret,
            "events": ["request_error"],
            "message_format": "text",
            "mention_all": True,
            "enabled": False,
        },
    )
    assert response.status_code == 201
    item = response.json()["data"]
    assert item["events"] == ["request_error"] and item["mention_all"]
    assert "not-real" not in response.text
    updated = await http.patch(
        route + "/bindings/" + item["id"], json={"webhook_url": "", "name": "新名称"}
    )
    assert updated.status_code == 200
    private = await control.binding(p["id"], item["id"], private=True)
    assert private["private"]["webhook_url"] == secret
    preview = await http.post(
        route + "/alert-previews", json={"rule": {"status_codes": [400]}, "sample": {}}
    )
    assert preview.json()["data"]["sends_messages"] is False
    assert "状态码：400" in preview.json()["data"]["text"]


async def test_missing_spa_assets_has_actionable_503_not_raw_500(api, monkeypatch, tmp_path):
    import newapi_watch.console_api as module

    http, control, fleet = api
    monkeypatch.setattr(module, "__file__", str(tmp_path / "console_api.py"))
    app = module.create_console(control, fleet, Auth(control, secure_cookie=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/")
        assert response.status_code == 503 and "前端" in response.text and "pnpm" in response.text
        assert (await client.get("/health/live")).status_code == 200


async def test_monitoring_status_and_save_preserve_runtime_and_reject_cross_platform_target(api):
    from newapi_watch.control_models import BindingInput

    http, control, fleet = api
    a = await ready(api, "A")
    b = await ready(api, "B")
    target = await control.create_binding(
        b["id"],
        BindingInput(
            name="B-hook",
            kind="webhook",
            webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
        ),
    )
    route = f"/console/api/platforms/{a['id']}/monitoring"
    before = fleet.entry(a["id"]).runtime
    response = (await http.get(route)).json()["data"]
    assert response["execution"]["mode"] == "policy_checks"
    config = response["config"]
    config["channels"]["poll_seconds"] = 90
    assert (await http.put(route, json=config)).status_code == 200
    assert fleet.entry(a["id"]).runtime is before
    config["channels"]["binding_ids"] = [target["id"]]
    assert (await http.put(route, json=config)).status_code == 400


async def test_manual_check_is_authenticated_idempotent_and_disabled_platform_refused(api):
    from newapi_watch.control_models import PlatformPatch

    http, control, fleet = api
    route = "/console/api/platforms/missing/monitor-runs"
    assert (await http.post(route, json={"request_id": "request-123"})).status_code == 401
    a = await ready(api)
    route = f"/console/api/platforms/{a['id']}/monitor-runs"
    first = await http.post(route, json={"request_id": "request-123"})
    assert first.status_code == 202
    again = await http.post(route, json={"request_id": "request-123"})
    assert again.status_code == 202 and again.json()["data"]["id"] == first.json()["data"]["id"]
    await control.patch_platform(a["id"], PlatformPatch(enabled=False))
    await fleet.reload(a["id"])
    assert (await http.post(route, json={"request_id": "request-456"})).status_code == 409


async def test_notification_history_and_retry_are_scoped_and_idempotent(api):
    http, _, fleet = api
    assert (await http.get("/console/api/platforms/missing/notifications")).status_code == 401
    a = await ready(api, "A")
    b = await ready(api, "B")
    runtime = fleet.entry(a["id"]).runtime
    await runtime.store.enqueue("test", "test notification", 1000)
    await runtime.store.finish_notification(1, "dead", 8, 1000, "delivery failed", 1000)
    route = f"/console/api/platforms/{a['id']}"
    history = (await http.get(route + "/notifications")).json()["data"]
    assert history["items"][0]["status"] == "dead"
    assert (await http.get(f"/console/api/platforms/{b['id']}/notifications")).json()["data"][
        "total"
    ] == 0
    first = await http.post(
        route + "/notification-retries", json={"notification_id": 1, "request_id": "retry-123"}
    )
    assert first.status_code == 200
    again = await http.post(
        route + "/notification-retries", json={"notification_id": 1, "request_id": "retry-123"}
    )
    assert again.json()["data"]["duplicate"] is True


async def test_binding_referenced_by_policy_cannot_be_deleted_or_unsubscribed(api):
    from newapi_watch.control_models import BindingInput

    http, control, _ = api
    platform = await ready(api)
    target = await control.create_binding(
        platform["id"],
        BindingInput(
            name="policy target",
            kind="webhook",
            events=["channel"],
            webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
        ),
    )
    route = f"/console/api/platforms/{platform['id']}"
    config = (await http.get(route + "/monitoring")).json()["data"]["config"]
    config["channels"]["binding_ids"] = [target["id"]]
    assert (await http.put(route + "/monitoring", json=config)).status_code == 200
    binding = route + "/bindings/" + target["id"]
    assert (await http.delete(binding)).status_code == 409
    assert (await http.patch(binding, json={"events": []})).status_code == 409
    assert (await http.patch(binding, json={"enabled": False})).status_code == 200


async def test_platform_shared_429_cooldown_and_retry_after_are_exposed(api):
    http, _, fleet = api
    platform = await ready(api)
    runtime = fleet.entry(platform["id"]).runtime
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "90"}, text="sensitive-upstream-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_http:
        runtime.client.http = upstream_http
        route = f"/console/api/platforms/{platform['id']}"
        first = await http.get(route + "/channels")
        assert first.status_code == 429
        assert int(first.headers["Retry-After"]) >= 89
        assert "sensitive-upstream-body" not in first.text
        second = await http.get(route + "/users?keyword=a")
        assert second.status_code == 429 and len(calls) == 1
        state = (await http.get(route + "/monitoring")).json()["data"]
        assert state["execution"]["request_control"]["cooldown_remaining"] >= 89
        config = state["config"]
        config["requests"] = {"min_interval_seconds": 2, "rate_limit_cooldown_seconds": 120}
        assert (await http.put(route + "/monitoring", json=config)).status_code == 200
        assert runtime.client.throttle.remaining >= 89  # saving must not bypass a running cooldown
        config["requests"]["min_interval_seconds"] = -1
        assert (await http.put(route + "/monitoring", json=config)).status_code == 400


async def test_manual_delivery_is_distinguished_from_real_webhook_receipt(api):
    http, _, fleet = api
    platform = await ready(api)
    runtime = fleet.entry(platform["id"]).runtime
    await runtime.store.enqueue("pending-notification", "notice", 1000)
    await runtime.commands.operations.mark_pending_delivered(
        "manual-test-123", before_id=1, now=1001
    )
    response = await http.get(f"/console/api/platforms/{platform['id']}/notifications")
    item = response.json()["data"]["items"][0]
    assert item["status"] == "sent" and item["delivery_method"] == "manual"
    assert "未实际发送" in item["last_error"]


async def test_alert_preview_uses_platform_log_timezone_and_omits_removed_rows(api):
    from newapi_watch.control_models import PlatformPatch

    http, control, fleet = api
    platform = await ready(api)
    await control.patch_platform(platform["id"], PlatformPatch(timezone="UTC"))
    await fleet.reload(platform["id"])
    response = await http.post(
        f"/console/api/platforms/{platform['id']}/alert-previews",
        json={
            "rule": {
                "template": "渠道异常告警\n错误码：{error_code}\n告警时间：{time}（北京时间）\n错误日志时间：{log_time}（北京时间）"
            },
            "sample": {
                "log_id": 123456,
                "log_created_at": 1794627249,
                "upstream_request_id": "upstream-preview",
            },
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sends_messages"] is False
    assert "错误日志时间：2026年11月14日 03:34:09" in data["text"]
    assert "日志 ID：123456" in data["text"]
    assert "错误码：" not in data["text"] and "告警时间：" not in data["text"]
    assert "北京时间" not in data["text"]
