"""Today usage must be self scoped, timezone correct and independent of balance."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_upstream_accounts import config, setup

from newapi_watch.assistant import Assistant
from newapi_watch.control_models import LLMConfig
from newapi_watch.llm_tools import Tools


async def test_today_usage_uses_self_stat_and_account_currency_and_reuses_cache(tmp_path):
    seen = []

    def source(request):
        seen.append(request)
        return httpx.Response(200, json={"success": True, "data": {"quota": 1000000}})

    account = config()
    runtime, http, service = await setup(tmp_path, source, [account])
    try:
        now = datetime(2026, 11, 17, 12, 34, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        result = await service.usage.query(
            account, service.clients[account.id], service.scope(account), now=now
        )
        assert result["quota_display"] == "¥14.60"
        assert (
            result["start_at"]
            == datetime(2026, 11, 17, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        )
        assert result["end_at"] == int(now)
        assert result["source"] == "newapi_log_self_stat"
        again = await service.usage.query(
            account, service.clients[account.id], service.scope(account), now=now + 20
        )
        assert again["quota"] == 1000000
        assert len(seen) == 1
        assert seen[0].url.path == "/api/log/self/stat"
        assert seen[0].url.params["start_timestamp"] == str(int(result["start_at"]))
        assert seen[0].headers["New-Api-User"] == "7"
        assert seen[0].headers["Authorization"] == "Bearer upstream-test-token"
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.parametrize(
    "data", [{}, {"quota": None}, {"quota": "0"}, {"quota": True}, {"quota": -1}]
)
async def test_today_usage_missing_or_invalid_quota_is_never_zero(tmp_path, data):
    account = config()
    runtime, http, service = await setup(
        tmp_path, lambda _: httpx.Response(200, json={"success": True, "data": data}), [account]
    )
    try:
        result = await service.usage.query(
            account, service.clients[account.id], service.scope(account)
        )
        assert result["status"] == "error"
        assert result.get("quota") is None
        assert "quota_display" not in result
    finally:
        await runtime.close()
        await http.aclose()


async def test_today_usage_rollover_hides_yesterday_and_zero_is_valid(tmp_path):
    account = config(usage_timezone="America/New_York")
    runtime, http, service = await setup(
        tmp_path,
        lambda _: httpx.Response(200, json={"success": True, "data": {"quota": 0}}),
        [account],
    )
    try:
        now = datetime(2026, 11, 1, 23, 59, tzinfo=ZoneInfo("America/New_York")).timestamp()
        result = await service.usage.query(
            account, service.clients[account.id], service.scope(account), now=now
        )
        assert result["quota_display"] == "¥0.00"
        # November 1 is 25 hours long in this timezone. Boundary is local midnight, not now - 86400.
        assert (
            result["start_at"]
            == datetime(2026, 11, 1, tzinfo=ZoneInfo("America/New_York")).timestamp()
        )
        later = await service.usage.snapshot(account, service.scope(account), now=now + 120)
        assert later["status"] == "unread"
        assert "quota" not in later
    finally:
        await runtime.close()
        await http.aclose()


async def test_usage_failure_preserves_old_cutoff_and_marks_stale(tmp_path):
    response = {"status": 200}

    def source(_):
        if response["status"] != 200:
            return httpx.Response(response["status"])
        return httpx.Response(200, json={"success": True, "data": {"quota": 1000000}})

    account = config()
    runtime, http, service = await setup(tmp_path, source, [account])
    try:
        now = datetime(2026, 11, 17, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        first = await service.usage.query(
            account, service.clients[account.id], service.scope(account), now=now
        )
        response["status"] = 403
        failed = await service.usage.query(
            account, service.clients[account.id], service.scope(account), now=now + 600
        )
        assert failed["quota_display"] == first["quota_display"]
        assert failed["end_at"] == first["end_at"] and failed["stale"]
        snapshot = await service.usage.snapshot(account, service.scope(account), now=now + 601)
        assert snapshot["stale"] and snapshot["status"] == "error"
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.parametrize("status", [401, 403, 404, 429, 502])
async def test_stat_http_failure_does_not_overwrite_valid_balance_or_send_alert(tmp_path, status):
    def source(request):
        if request.url.path == "/api/user/self":
            return httpx.Response(
                200,
                json={"success": True, "data": {"id": 7, "quota": 500000, "used_quota": 1000000}},
            )
        assert request.url.path == "/api/log/self/stat"
        return httpx.Response(status)

    account = config()
    runtime, http, service = await setup(tmp_path, source, [account])
    try:
        balance = await service.query(account.id)
        usage = await service.usage.query(
            account, service.clients[account.id], service.scope(account)
        )
        snapshot = await service.snapshot(account)
        assert usage["status"] == "error" and "quota" not in usage
        assert snapshot["sample"]["quota"] == balance["quota"] == 500000
        assert not await runtime.store.notifications()
    finally:
        await runtime.close()
        await http.aclose()


async def test_usage_timezone_change_does_not_reset_balance_monitor_identity(tmp_path):
    account = config()
    runtime, http, service = await setup(
        tmp_path,
        lambda _: httpx.Response(200, json={"success": True, "data": {"quota": 0}}),
        [account],
    )
    try:
        changed = account.model_copy(update={"usage_timezone": "UTC"})
        assert service.scope(account) == service.scope(changed)
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.parametrize("fresh_quota, expected_amount", [(2000000, "¥29.20"), (0, "¥0.00")])
async def test_robot_today_usage_refreshes_current_day_even_with_a_warm_cache(
    tmp_path, fresh_quota, expected_amount
):
    now = datetime(2026, 11, 20, 17, 41, 45, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    account = config(query_binding_ids=["bot-A"])
    seen = []

    def source(request):
        assert request.url.path == "/api/log/self/stat"
        assert request.headers["New-Api-User"] == "7"
        seen.append(request.url.params)
        quota = 1000000 if len(seen) == 1 else fresh_quota
        return httpx.Response(200, json={"success": True, "data": {"quota": quota}})

    runtime, http, service = await setup(tmp_path, source, [account])
    runtime.commands.upstream = service
    try:
        await service.usage.query(
            account, service.clients[account.id], service.scope(account), now=now - 60
        )
        result = await service.query_today_usage(account.id, binding_id="bot-A", now=now)
        assert result["quota_display"] == expected_amount and result["end_at"] == int(now)
        assert len(seen) == 2
        assert seen[-1]["start_timestamp"] == str(
            int(datetime(2026, 11, 20, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        )
        assert seen[-1]["end_timestamp"] == str(int(now))
    finally:
        await service.close()
        await runtime.close()
        await http.aclose()


async def test_upstream_today_usage_command_and_tool_only_query_authorized_accounts(tmp_path):
    calls = []

    def source(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"success": True, "data": {"quota": 500000}})

    account = config(query_binding_ids=["bot-A"])
    runtime, http, service = await setup(tmp_path, source, [account])
    runtime.commands.upstream = service
    args = f'{{"account_id":"{account.id}"}}'
    try:
        bot = Assistant("A", "平台", runtime.commands, LLMConfig(), http, binding_id="bot-A")
        reply = await bot.execute("上游今日已用 上游甲", source="wecom")
        assert (
            reply.ok and "上游甲" in reply.text and "¥7.30" in reply.text and "截至" in reply.text
        )
        allowed = await Tools(runtime.commands, context={"binding_id": "bot-A"}).call(
            "get_upstream_today_usage", args
        )
        denied = await Tools(runtime.commands, context={"binding_id": "bot-B"}).call(
            "get_upstream_today_usage", args
        )
        foreign_bot = Assistant(
            "A", "平台", runtime.commands, LLMConfig(), http, binding_id="bot-B"
        )
        foreign_reply = await foreign_bot.execute("上游今日已用 上游甲", source="wecom")
        assert allowed["ok"] and allowed["quota_display"] == "¥7.30"
        assert not denied["ok"] and "quota" not in denied
        assert not foreign_reply.ok and "¥7.30" not in foreign_reply.text
        assert calls == ["/api/log/self/stat", "/api/log/self/stat"]
    finally:
        await service.close()
        await runtime.close()
        await http.aclose()


async def test_robot_usage_refresh_failure_does_not_return_a_stale_amount(tmp_path):
    calls = []

    def source(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(200, json={"success": True, "data": {"quota": 1000000}})
        return httpx.Response(403)

    account = config(query_binding_ids=["bot-A"])
    runtime, http, service = await setup(tmp_path, source, [account])
    runtime.commands.upstream = service
    try:
        await service.usage.query(account, service.clients[account.id], service.scope(account))
        bot = Assistant("A", "平台", runtime.commands, LLMConfig(), http, binding_id="bot-A")
        reply = await bot.execute("上游账户 上游甲 今日已用", source="wecom")
        assert not reply.ok and "未能查询" in reply.text
        assert "¥14.60" not in reply.text and "¥0.00" not in reply.text
        assert calls == ["/api/log/self/stat", "/api/log/self/stat"]
    finally:
        await service.close()
        await runtime.close()
        await http.aclose()
