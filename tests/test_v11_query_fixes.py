"""Regressions for the observed v1.1 model and query-route failures."""

import json
import time

import httpx
import pytest

from newapi_watch.assistant import Assistant
from newapi_watch.assistant_policy import fallback
from newapi_watch.config import Settings
from newapi_watch.control_models import LLMConfig
from newapi_watch.llm_tools import Tools
from newapi_watch.model_provider import Provider
from newapi_watch.models import Channel
from newapi_watch.money import CurrencyConfig
from newapi_watch.runtime import Runtime


def settings(tmp_path):
    return Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "query.sqlite3",
        newapi_url="https://platform.invalid",
        newapi_token="private-admin",
        newapi_user_id=1,
        request_attempts=1,
        retry_seconds=0,
    )


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://relay.invalid", "/v1/chat/completions"),
        ("https://relay.invalid/", "/v1/chat/completions"),
        ("https://relay.invalid/v1/", "/v1/chat/completions"),
        ("https://relay.invalid/proxy/v1", "/proxy/v1/chat/completions"),
        ("https://relay.invalid/chat/completions", "/chat/completions"),
    ],
)
async def test_custom_chat_base_sends_credentials_only_to_normalized_endpoint(base, expected):
    seen = []

    def server(request):
        seen.append(request.url.path)
        if request.url.path != expected:
            return httpx.Response(200, text="<!DOCTYPE html><html>console</html>")
        assert request.headers["Authorization"] == "Bearer test-model-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        message = await Provider(
            LLMConfig(base_url=base, api_key="test-model-key", model="glm-test"), http
        ).complete({"messages": []})
    assert message["content"] == "OK" and seen == [expected]


@pytest.mark.parametrize(
    "status,body,code",
    [
        (200, "<!doctype html><html>private-response</html>", "wrong_endpoint"),
        (
            403,
            {"error": {"message": "分组 private-group 已被弃用 private-secret"}},
            "group_unavailable",
        ),
        (401, {"error": {"message": "private-secret"}}, "authentication_failed"),
    ],
)
async def test_model_diagnostics_distinguish_html_group_rejection_and_auth(status, body, code):
    def server(_):
        return (
            httpx.Response(status, text=body, headers={"Content-Type": "text/html"})
            if isinstance(body, str)
            else httpx.Response(status, json=body)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        result = await Provider(
            LLMConfig(base_url="https://relay.invalid/v1", api_key="private-secret", model="test"),
            http,
        ).probe()
    assert result["ok"] is False and result["code"] == code
    assert result["http_status"] == status
    assert "private-secret" not in json.dumps(result) and "private-response" not in json.dumps(
        result
    )
    assert "private-group" not in json.dumps(result)


async def test_single_channel_tool_uses_detail_endpoint_not_full_list(tmp_path):
    paths = []

    def upstream(request):
        paths.append(request.url.path)
        if request.url.path != "/api/channel/93":
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "id": 93,
                    "name": "指定渠道",
                    "status": 1,
                    "models": "glm-test,kimi-test",
                    "key": "private-forwarding-key",
                    "used_quota": 1234,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(settings(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            result = await Tools(runtime.commands).call("get_channel", '{"channel_id":93}')
            assert result["ok"] and result["items"][0]["id"] == 93
            assert result["items"][0]["models"] == "glm-test,kimi-test"
            assert "private-forwarding-key" not in json.dumps(result)
            assert paths == ["/api/channel/93"]
        finally:
            await runtime.close()


async def test_model_failure_fallback_keeps_requested_channel_and_models(tmp_path):
    runtime = Runtime(settings(tmp_path))
    await runtime.open(start_tasks=False)
    try:
        await runtime.store.sync_channels(
            [
                Channel(id=1, name="无关渠道", status=1),
                Channel(id=93, name="指定渠道", status=1, models="glm-test,kimi-test"),
            ],
            time.time(),
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(401))
        ) as http:
            bot = Assistant(
                "A",
                "A",
                runtime.commands,
                LLMConfig(
                    enabled=True,
                    base_url="https://relay.invalid/v1",
                    api_key="test-key",
                    model="test",
                ),
                http,
            )
            reply = await bot.execute("“看看渠道 93 的详情和支持模型”", source="wecom")
        assert "指定渠道" in reply.text and "glm-test,kimi-test" in reply.text
        assert "无关渠道" not in reply.text and "当前快照有" not in reply.text
        assert "快照" in reply.text and "鉴权" in reply.text
    finally:
        await runtime.close()


async def test_missing_channel_fallback_does_not_answer_with_other_channels(tmp_path):
    runtime = Runtime(settings(tmp_path))
    await runtime.open(start_tasks=False)
    try:
        await runtime.store.sync_channels([Channel(id=1, name="无关渠道", status=1)], time.time())
        result = await fallback(runtime.commands, "看看渠道93的详情", model_failed=True)
        assert not result.ok and "93" in result.text and "无关渠道" not in result.text
    finally:
        await runtime.close()


async def test_log_list_pushes_filters_and_page_without_scanning_half_million_rows(tmp_path):
    seen = []
    stamp = int(time.time()) - 10

    def upstream(request):
        params = dict(request.url.params)
        seen.append(params)
        if params.get("type") != "5" or params.get("channel") != "93" or params.get("p") != "3":
            return httpx.Response(
                200, json={"success": True, "data": {"items": [], "total": 500000}}
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": i,
                            "created_at": stamp,
                            "type": 5,
                            "channel": 93,
                            "model_name": "test",
                        }
                        for i in range(42, 62)
                    ],
                    "total": 500001,
                    "page_size": 20,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(settings(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            result = await runtime.commands.data.logs(
                stamp - 100, stamp + 1, page=3, page_size=20, log_type=5, channel_id=93
            )
            assert result["items"][0]["id"] == 42 and result["total"] == 500001
            assert result["page"] == 3 and result["page_size"] == 20 and result["has_more"]
            assert result["complete"] is False and result["page_complete"] is True
            assert len(seen) == 1 and seen[0]["end_timestamp"] == str(stamp)
        finally:
            await runtime.close()


async def test_log_page_refuses_ignored_filters_instead_of_returning_wrong_data(tmp_path):
    stamp = int(time.time()) - 10

    def upstream(_):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {"id": 1, "created_at": stamp, "type": 2, "channel": 1},
                    ],
                    "total": 1,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(settings(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            from newapi_watch.client import UpstreamError

            with pytest.raises(UpstreamError, match="筛选"):
                await runtime.commands.data.logs(stamp - 100, stamp + 1, log_type=5, channel_id=93)
        finally:
            await runtime.close()


async def test_total_usage_tool_reads_upstream_aggregate_without_log_scan(tmp_path):
    paths = []

    def upstream(request):
        paths.append(request.url.path)
        if request.url.path != "/api/log/stat":
            return httpx.Response(500)
        assert request.url.params["type"] == "2"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"quota": 731234, "rpm": 15, "tpm": 900, "private": "secret"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(settings(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            result = await Tools(runtime.commands).call("get_usage_summary", '{"period":"今天"}')
            assert result["ok"] and result["quota"] == 731234
            assert result["rpm_last_minute"] == 15 and result["tpm_last_minute"] == 900
            assert "success_records" not in result and "active_users" not in result
            assert "排名" in result["limitation"] and "60" in result["limitation"]
            assert paths == ["/api/log/stat"] and "secret" not in json.dumps(result)
        finally:
            await runtime.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "data": {}},
        {"success": True, "data": {"quota": "not-a-number", "rpm": 0, "tpm": 0}},
        {"success": True, "data": {"quota": True}},
    ],
)
async def test_usage_summary_never_fills_missing_or_invalid_quota_with_zero(tmp_path, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        runtime = Runtime(settings(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            result = await Tools(runtime.commands).call("get_usage_summary", '{"period":"今天"}')
            assert result["ok"] is False and "quota" not in result
            assert "字段" in result["error"]
        finally:
            await runtime.close()


async def test_usage_aggregate_429_is_not_retried_or_fallen_back_to_log_scan(tmp_path):
    paths = []

    def upstream(request):
        paths.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(settings(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            result = await Tools(runtime.commands).call("get_usage_summary", '{"period":"今天"}')
            assert not result["ok"] and "429" in result["error"]
            assert paths == ["/api/log/stat"] and "quota" not in result
        finally:
            await runtime.close()


async def test_short_nonfinal_log_page_is_not_claimed_complete(tmp_path):
    from newapi_watch.client import UpstreamError

    stamp = int(time.time()) - 10
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "items": [{"id": 1, "type": 5, "channel": 93, "created_at": stamp}],
                        "total": 99,
                        "page_size": 20,
                    },
                },
            )
        )
    ) as http:
        runtime = Runtime(settings(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            with pytest.raises(UpstreamError, match="不完整"):
                await runtime.commands.data.logs(stamp - 100, stamp + 1, log_type=5, channel_id=93)
        finally:
            await runtime.close()


async def test_auth_failure_fallback_redacts_model_key_embedded_in_upstream_name(tmp_path):
    runtime = Runtime(settings(tmp_path))
    await runtime.open(start_tasks=False)
    try:
        await runtime.store.sync_channels(
            [Channel(id=93, name="指定渠道 private-model-key", status=1, models="test")],
            time.time(),
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(401))
        ) as http:
            bot = Assistant(
                "A",
                "A",
                runtime.commands,
                LLMConfig(
                    enabled=True,
                    base_url="https://relay.invalid/v1",
                    api_key="private-model-key",
                    model="test",
                ),
                http,
            )
            reply = await bot.execute("看看渠道93详情", source="wecom")
        assert "private-model-key" not in reply.text and "[REDACTED]" in reply.text
    finally:
        await runtime.close()


@pytest.mark.parametrize("quota, amount", [(1234567, "¥18.02"), (0, "¥0.00"), (None, None)])
async def test_channel_today_usage_reads_scoped_consumption_and_never_invents_zero(
    tmp_path, quota, amount
):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    paths = []

    def upstream(request):
        paths.append(request.url.path)
        assert request.url.host == "demo.invalid"
        if request.url.path == "/api/channel/99":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"id": 99, "name": "样例渠道", "status": 1, "used_quota": 99999999},
                },
            )
        assert request.url.path == "/api/log/stat"
        params = request.url.params
        assert params["channel"] == "99" and params["type"] == "2"
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        assert int(params["start_timestamp"]) == int(
            now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        assert abs(int(params["end_timestamp"]) - int(now.timestamp())) < 5
        return httpx.Response(
            200, json={"success": True, "data": {"quota": quota, "rpm": 13, "tpm": 50}}
        )

    config = settings(tmp_path)
    config.currency = CurrencyConfig(
        confirmed=True, quota_per_unit="500000", usd_exchange_rate="7.3"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(config, http=http)
        await runtime.open(start_tasks=False)
        try:
            result = await Tools(runtime.commands).call(
                "get_channel_today_usage", '{"channel_id":99}'
            )
            reply = await runtime.assistant.execute("99号渠道今天用了多少？", source="wecom")
            assert result["ok"] is (amount is not None)
            assert reply.ok is (amount is not None)
            if amount is not None:
                assert result["quota_display"] == amount
                assert result["quota"] == quota and result["channel_id"] == 99
                assert "今日" in reply.text and "样例渠道" in reply.text and amount in reply.text
            else:
                assert "¥0.00" not in reply.text
            invalid = await Tools(runtime.commands).call(
                "get_channel_today_usage", '{"channel_id":0}'
            )
            assert invalid["ok"] is False
        finally:
            await runtime.close()
    assert paths == ["/api/channel/99", "/api/log/stat"] * 2
