import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from newapi_watch.client import NewAPIClient
from newapi_watch.config import Settings
from newapi_watch.llm_tools import Tools
from newapi_watch.runtime import Runtime


def config(tmp_path):
    return Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "watch.sqlite3",
        newapi_url="https://test.invalid",
        newapi_token="private-token",
        newapi_user_id=1,
        retry_seconds=0,
        request_attempts=1,
    )


async def test_effective_page_size_smaller_than_requested_still_reads_all_104(tmp_path):
    def upstream(request):
        page = int(request.url.params["p"])
        all_items = [{"id": i, "name": f"渠道{i}", "status": 1} for i in range(1, 105)]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": all_items[(page - 1) * 20 : page * 20],
                    "total": 104,
                    "page_size": 20,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        items = await NewAPIClient(config(tmp_path), http).channels()
    assert len(items) == 104 and items[-1].id == 104


async def test_bot_channel_tool_live_pages_and_global_counts_are_not_truncated_at_50(tmp_path):
    def upstream(request):
        items = [
            {"id": i, "name": f"渠道{i}", "status": 1 if i <= 75 else 2} for i in range(1, 105)
        ]
        page = int(request.url.params.get("p", 1))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"items": items[(page - 1) * 100 : page * 100], "total": 104},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(config(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            tools = Tools(runtime.commands)
            result = await tools.call("get_channels", json.dumps({"page": 2, "page_size": 100}))
            assert result["ok"] and result["total"] == 104
            assert [i["id"] for i in result["items"]] == [101, 102, 103, 104]
            assert result["summary"]["enabled"] == 75 and result["source"] == "newapi_admin_api"
        finally:
            await runtime.close()


async def test_bot_statistics_backfills_requested_seven_days_without_moving_poll_cursor(tmp_path):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    stamp = int(
        datetime.combine(
            today - timedelta(days=5), datetime.min.time(), ZoneInfo("Asia/Shanghai")
        ).timestamp()
    )

    def upstream(request):
        assert int(request.url.params["start_timestamp"]) <= stamp
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": 88,
                            "created_at": stamp,
                            "type": 2,
                            "channel": 93,
                            "quota": 12345,
                            "model_name": "real-model",
                        }
                    ],
                    "total": 1,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(config(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            await runtime.store.set_meta("log_cursor", "999")
            result = await Tools(runtime.commands).call(
                "get_statistics", '{"period":"近7天","dimension":"channel"}'
            )
            assert result["summary"]["quota"] == 12345 and result["source"] == "newapi_admin_api"
            assert result["coverage"]["complete"] is True
            assert await runtime.store.get_meta("log_cursor") == "999"
        finally:
            await runtime.close()


async def test_live_statistics_429_is_not_a_successful_zero_snapshot(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, text="private body"))
    ) as http:
        runtime = Runtime(config(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            result = await Tools(runtime.commands).call("get_statistics", '{"period":"近7天"}')
            assert result["ok"] is False and "429" in result["error"]
            assert "summary" not in result and "private body" not in str(result)
        finally:
            await runtime.close()


async def test_live_window_replaces_stale_local_records_and_keeps_coverage_truthful(tmp_path):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    stamp = int(
        datetime.combine(
            today - timedelta(days=1), datetime.min.time(), ZoneInfo("Asia/Shanghai")
        ).timestamp()
    )
    with_records = {
        "success": True,
        "data": {
            "items": [{"id": 1, "type": 2, "created_at": stamp, "channel": 1, "quota": 50}],
            "total": 1,
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=with_records))
    ) as http:
        r = Runtime(config(tmp_path), http=http)
        await r.open(start_tasks=False)
        try:
            from newapi_watch.models import LogRecord

            await r.store.save_logs(
                [LogRecord(id=999, type=2, created_at=stamp, quota=900)], stamp + 1, stamp
            )
            before = await r.store.get_meta("log_coverage_start")
            result = await Tools(r.commands).call("get_statistics", '{"period":"近7天"}')
            assert result["summary"]["quota"] == 50 and result["summary"]["success_records"] == 1
            assert await r.store.get_meta("log_coverage_start") == before
        finally:
            await r.close()


async def test_error_metadata_redacts_json_tokens_and_preserves_status_and_code(tmp_path):
    raw = {
        "id": 1,
        "created_at": 100,
        "type": 5,
        "channel": 93,
        "content": '{"error":{"message":"status_code=400, developer unsupported; token=secret-A; sk-abcd1234","code":"invalid_parameter_error"}}',
        "other": '{"status_code":400,"request_body":{"secret":"do-not-store"}}',
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"success": True, "data": {"items": [raw], "total": 1}}
            )
        )
    ) as http:
        result = (await NewAPIClient(config(tmp_path), http).logs(1, 200))[0]
    assert result.http_status == 400 and result.error_code == "invalid_parameter_error"
    assert "secret-A" not in result.error_message and "sk-abcd1234" not in result.error_message
    assert "do-not-store" not in result.model_dump_json()


async def test_model_that_calls_tool_gets_explicit_upstream_429_not_false_model_incompatibility(
    tmp_path,
):
    from newapi_watch.assistant import Assistant
    from newapi_watch.control_models import LLMConfig

    def model(request):
        payload = json.loads(request.content)
        if payload["messages"][-1]["role"] == "tool":
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "今天用量为0，所有渠道正常"}}]}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "type": "function",
                                    "function": {"name": "get_channels", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429))) as upstream,
        httpx.AsyncClient(transport=httpx.MockTransport(model)) as provider,
    ):
        r = Runtime(config(tmp_path), http=upstream)
        await r.open(start_tasks=False)
        try:
            assistant = Assistant(
                "A",
                "绑定平台A",
                r.commands,
                LLMConfig(
                    enabled=True,
                    base_url="https://model.invalid/v1",
                    api_key="test-key",
                    model="test",
                ),
                provider,
            )
            result = await assistant.execute("查看全部渠道和今天用量")
            assert not result.ok and "429" in result.text
            assert "为0" not in result.text and "所有渠道正常" not in result.text
            status = await r.store.get_meta("llm_diagnostic")
            assert not status or "unsupported_tools" not in status
        finally:
            await r.close()


async def test_channel_dto_keeps_operational_fields_but_never_forwarding_keys(tmp_path):
    raw = {
        "id": 93,
        "name": "bailian",
        "status": 1,
        "models": "kimi-k3.1-pro",
        "group": "default,vip",
        "priority": 5,
        "weight": 10,
        "used_quota": 12345,
        "test_time": 100,
        "balance_updated_time": 99,
        "key": "sk-private",
        "other": {"private": "secret"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"success": True, "data": [raw]})
        )
    ) as http:
        item = (await NewAPIClient(config(tmp_path), http).channels())[0].model_dump()
    assert item["group"] == "default,vip" and item["priority"] == 5 and item["used_quota"] == 12345
    assert "sk-private" not in str(item) and "other" not in item


async def test_live_query_aggregates_without_persisting_business_logs(tmp_path):
    from newapi_watch.periods import StatisticsQuery
    from newapi_watch.persistence import row

    stamp = int(datetime.now(ZoneInfo("Asia/Shanghai")).timestamp()) - 5
    today = datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai")).date()

    def upstream(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": 99,
                            "type": 2,
                            "created_at": stamp,
                            "quota": 765,
                            "prompt_tokens": 40,
                            "completion_tokens": 2,
                            "user_id": 1,
                            "channel": 93,
                            "model_name": "real",
                        }
                    ],
                    "total": 1,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(config(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            result = await runtime.commands.data.statistics(StatisticsQuery(start=today, end=today))
            assert result["summary"]["quota"] == 765
            assert result["summary"]["tokens"] == 42
            async with runtime.store.transaction() as db:
                assert (await row(db, "SELECT COUNT(*) AS n FROM logs"))["n"] == 0
            assert await runtime.store.get_meta("log_cursor") is None
        finally:
            await runtime.close()


async def test_log_cache_does_not_refetch_at_minute_boundary(tmp_path, monkeypatch):
    from newapi_watch import data_queries

    now = 119
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"success": True, "data": {"items": [], "total": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(config(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            monkeypatch.setattr(data_queries.time, "time", lambda: now)
            await runtime.commands.data.window(0, 86400)
            now = 121
            _, through = await runtime.commands.data.window(0, 86400)
            assert through == 119 and calls == ["/api/log/"]
        finally:
            await runtime.close()


async def test_web_user_pages_reuse_platform_scoped_query_result(tmp_path):
    calls = []

    def upstream(request):
        calls.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"items": [{"id": i, "quota": 100} for i in range(1, 51)], "total": 50},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        runtime = Runtime(config(tmp_path), http=http)
        await runtime.open(start_tasks=False)
        try:
            first = await runtime.commands.data.users("query")
            second = await runtime.commands.data.users("query")
            assert [u.id for u in first] == [u.id for u in second]
            assert len(calls) == 1
        finally:
            await runtime.close()
