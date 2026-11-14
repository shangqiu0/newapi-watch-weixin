"""Integration contracts: upstream self-only accounts, Decimal thresholds and isolation."""

import json

import httpx
import pytest
from pydantic import ValidationError

from newapi_watch.config import Settings
from newapi_watch.money import CurrencyConfig
from newapi_watch.runtime import Runtime
from newapi_watch.upstream_accounts import UpstreamAccount, UpstreamAccounts


def config(**values):
    return UpstreamAccount(
        name="上游甲",
        base_url="https://supplier.invalid",
        user_id=7,
        access_token="upstream-test-token",
        enabled=True,
        currency=CurrencyConfig(confirmed=True, quota_per_unit="500000", usd_exchange_rate="7.3"),
        **values,
    )


async def setup(tmp_path, responder, accounts):
    http = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    runtime = Runtime(Settings(_env_file=None, demo=True, database=tmp_path / "up.db"), http=http)
    await runtime.open(start_tasks=False)
    service = UpstreamAccounts(runtime, accounts)
    return runtime, http, service


async def test_ordinary_user_query_only_reads_self_and_never_admin_or_other_account(tmp_path):
    paths = []

    def source(request):
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer upstream-test-token"
        assert request.headers["New-Api-User"] == "7"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "id": 7,
                    "username": "myself",
                    "quota": 500000,
                    "used_quota": 1000000,
                    "access_token": "never-forward",
                },
            },
        )

    account = config(query_binding_ids=["bot-A"])
    runtime, http, service = await setup(tmp_path, source, [account])
    try:
        result = await service.query(account.id, binding_id="bot-A")
        assert result["quota_display"] == "¥7.30"
        assert result["used_quota_display"] == "¥14.60"
        assert "never-forward" not in json.dumps(
            result
        ) and "upstream-test-token" not in json.dumps(result)
        with pytest.raises(ValueError):
            await service.query(account.id, binding_id="bot-B")
        assert paths == ["/api/user/self"]
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.parametrize(
    "data",
    [{"id": 8, "quota": 500000}, {"id": 7}, {"id": 7, "quota": None}, {"id": 7, "quota": "0"}],
)
async def test_wrong_identity_or_missing_quota_is_unknown_not_zero(tmp_path, data):
    account = config()
    runtime, http, service = await setup(
        tmp_path, lambda _: httpx.Response(200, json={"success": True, "data": data}), [account]
    )
    try:
        with pytest.raises(ValueError):
            await service.query(account.id)
        assert not await runtime.store.notifications()
    finally:
        await runtime.close()
        await http.aclose()


async def test_low_balance_cooldown_and_recovery_notify_only_selected_targets(tmp_path):
    balance = {"quota": 500000}
    account = config(
        monitor_enabled=True,
        threshold_cny="10",
        recovery_threshold_cny="12",
        notify_binding_ids=["hook-A"],
        cooldown_seconds=60,
    )
    runtime, http, service = await setup(
        tmp_path,
        lambda _: httpx.Response(200, json={"success": True, "data": {"id": 7, **balance}}),
        [account],
    )
    try:
        await service.check(account.id, now=1000)
        await service.check(account.id, now=1001)
        notifications = await runtime.store.notifications()
        assert len(notifications) == 1
        assert "¥7.30" in notifications[0]["content"] and "¥10.00" in notifications[0]["content"]
        balance["quota"] = 1000000
        await service.check(account.id, now=1061)
        notices = await runtime.store.notifications()
        assert len(notices) == 2 and "恢复" in notices[1]["content"]
        async with runtime.store.transaction() as db:
            cursor = await db.execute("SELECT binding_ids FROM notifications")
            assert all(json.loads(row[0]) == ["hook-A"] for row in await cursor.fetchall())
    finally:
        await runtime.close()
        await http.aclose()


async def test_disabled_account_never_queries_and_auth_failure_never_falls_back(tmp_path):
    paths = []

    def source(request):
        paths.append(request.url.path)
        return httpx.Response(403)

    account = config()
    runtime, http, service = await setup(tmp_path, source, [account])
    try:
        with pytest.raises(ValueError):
            await service.query(account.id)
        assert paths == ["/api/user/self"]
        account.enabled = False
        with pytest.raises(ValueError):
            await service.query(account.id)
        assert paths == ["/api/user/self"]
    finally:
        await runtime.close()
        await http.aclose()


def test_monitored_upstream_requires_confirmed_currency_and_targets():
    with pytest.raises(ValidationError):
        config(monitor_enabled=True, threshold_cny="10")


async def test_bot_balance_delivery_requires_explicit_target_and_ack(tmp_path):
    from newapi_watch.control_models import BindingInput, PlatformInput
    from newapi_watch.control_store import ControlStore
    from newapi_watch.targets import Targets

    control = ControlStore(tmp_path / "control")
    await control.open()
    try:
        platform = await control.create_platform(
            PlatformInput(
                name="A", newapi_url="https://a.invalid", newapi_token="x", newapi_user_id=1
            )
        )
        binding = await control.create_binding(
            platform["id"],
            BindingInput(
                name="balance-bot",
                kind="bot",
                bot_id="test-id",
                bot_secret="test-secret",
                notification_chat_id="chat-001",
            ),
        )
        received = []

        async def sender(binding_id, chat_id, content):
            received.append((binding_id, chat_id, content))

        async with httpx.AsyncClient() as http:
            target = Targets(control, platform["id"], "A", http)
            target.bot_sender = sender
            with pytest.raises(RuntimeError):
                await target.send_for(1, "balance", kind="balance")
            await target.send_for(1, "balance", kind="balance", binding_ids=[binding["id"]])
            await target.send_for(1, "balance", kind="balance", binding_ids=[binding["id"]])
        assert len(received) == 1 and received[0][:2] == (binding["id"], "chat-001")
    finally:
        await control.close()


async def test_robot_tools_cannot_query_unbound_upstream_account(tmp_path):
    from newapi_watch.assistant import Assistant
    from newapi_watch.control_models import LLMConfig
    from newapi_watch.llm_tools import Tools

    account = config(query_binding_ids=["bot-A"])
    runtime, http, service = await setup(
        tmp_path,
        lambda _: httpx.Response(200, json={"success": True, "data": {"id": 7, "quota": 500000}}),
        [account],
    )
    runtime.commands.upstream = service
    try:
        allowed = Tools(runtime.commands, context={"binding_id": "bot-A"})
        result = await allowed.call("get_upstream_balance", json.dumps({"account_id": account.id}))
        assert result.get("ok") is True and result["quota_display"] == "¥7.30"
        denied = Tools(runtime.commands, context={"binding_id": "bot-B"})
        assert (await denied.call("get_upstream_balance", json.dumps({"account_id": account.id})))[
            "ok"
        ] is False
        bot = Assistant("A", "A", runtime.commands, LLMConfig(), http, binding_id="bot-A")
        result = await bot.execute("上游余额")
        assert result.ok and "¥7.30" in result.text
    finally:
        await runtime.close()
        await http.aclose()


async def test_changed_credentials_stop_old_pending_balance_notifications(tmp_path):
    from pydantic import SecretStr

    account = config(monitor_enabled=True, threshold_cny="10", notify_binding_ids=["hook-A"])
    runtime, http, service = await setup(
        tmp_path,
        lambda _: httpx.Response(200, json={"success": True, "data": {"id": 7, "quota": 1}}),
        [account],
    )
    try:
        await service.check(account.id, now=1000)
        new_account = account.model_copy(update={"access_token": SecretStr("new-user-test-token")})
        replacement = UpstreamAccounts(runtime, [new_account])
        await replacement.initialize()
        assert (await replacement.snapshot(new_account))["sample"] is None
        assert (await runtime.store.notifications())[0]["status"] == "historical"
    finally:
        await runtime.close()
        await http.aclose()


async def test_slow_upstream_does_not_block_other_upstream_checks(tmp_path):
    import asyncio

    slow_started, release, healthy_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def source(request):
        if request.url.host == "slow.invalid":
            slow_started.set()
            await release.wait()
        else:
            healthy_done.set()
        return httpx.Response(200, json={"success": True, "data": {"id": 7, "quota": 500000}})

    a = config(monitor_enabled=True, threshold_cny="10", notify_binding_ids=["hook"])
    a.base_url = "https://slow.invalid"
    b = a.model_copy(update={"id": "b" * 32, "base_url": "https://healthy.invalid"})
    runtime, http, service = await setup(tmp_path, source, [a, b])
    task = asyncio.create_task(service.tick())
    try:
        async with asyncio.timeout(1):
            await slow_started.wait()
            await healthy_done.wait()
    finally:
        release.set()
        await task
        if hasattr(service, "close"):
            await service.close()
        await runtime.close()
        await http.aclose()


async def test_upstream_metadata_is_not_evidence_of_real_balance(tmp_path):
    from newapi_watch.assistant import Assistant
    from newapi_watch.control_models import LLMConfig

    account = config(query_binding_ids=["bot-A"])
    runtime, http, service = await setup(
        tmp_path,
        lambda _: httpx.Response(200, json={"success": True, "data": {"id": 7, "quota": 500000}}),
        [account],
    )
    runtime.commands.upstream = service
    calls = []

    def model(request):
        calls.append(request)
        message = {"role": "assistant", "content": "可用余额 ¥9999.99"}
        if len(calls) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "list1",
                        "type": "function",
                        "function": {"name": "get_upstream_accounts", "arguments": "{}"},
                    }
                ],
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
        try:
            assistant = Assistant(
                "A",
                "A",
                runtime.commands,
                LLMConfig(
                    enabled=True,
                    base_url="https://model.invalid/v1",
                    model="test",
                    api_key="fake-model-key",
                ),
                model_http,
                binding_id="bot-A",
            )
            result = await assistant.execute("上游甲目前还剩多少钱？")
            assert "9999.99" not in result.text
        finally:
            await runtime.close()
            await http.aclose()


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_upstream_http_failure_never_creates_a_zero_balance_alert(tmp_path, status):
    account = config(monitor_enabled=True, threshold_cny="10", notify_binding_ids=["hook"])
    runtime, http, service = await setup(tmp_path, lambda _: httpx.Response(status), [account])
    try:
        await service.check(account.id, now=1000)
        snapshot = await service.snapshot(account)
        assert snapshot["sample"] is None and snapshot["error"]
        assert not await runtime.store.notifications()
    finally:
        await runtime.close()
        await http.aclose()


async def test_balance_query_rejects_redirect_without_sending_token_to_another_host(tmp_path):
    paths = []

    def source(request):
        paths.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.invalid/api/user/self"})

    account = config()
    runtime, http, service = await setup(tmp_path, source, [account])
    try:
        with pytest.raises(ValueError):
            await service.query(account.id)
        assert paths == ["https://supplier.invalid/api/user/self"]
    finally:
        await runtime.close()
        await http.aclose()


async def test_invalid_self_fields_do_not_expose_raw_response_in_diagnostics(tmp_path):
    account = config()
    runtime, http, service = await setup(
        tmp_path,
        lambda _: httpx.Response(
            200,
            json={
                "success": True,
                "data": {"id": 7, "quota": 100, "username": ["upstream-test-token"]},
            },
        ),
        [account],
    )
    try:
        with pytest.raises(ValueError) as error:
            await service.query(account.id)
        assert "upstream-test-token" not in str(error.value)
    finally:
        await runtime.close()
        await http.aclose()


def test_recovery_amount_must_also_fit_internal_quota_range():
    with pytest.raises(ValidationError):
        config(
            monitor_enabled=True,
            threshold_cny="1",
            recovery_threshold_cny="1000000000000000",
            notify_binding_ids=["hook"],
        )
