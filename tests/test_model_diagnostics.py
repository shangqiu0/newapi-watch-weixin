import json

import httpx
import pytest

from newapi_watch.assistant import Assistant
from newapi_watch.config import Settings
from newapi_watch.control_models import LLMConfig
from newapi_watch.model_provider import Provider
from newapi_watch.runtime import Runtime


@pytest.fixture
async def runtime(tmp_path):
    runtime = Runtime(Settings(_env_file=None, demo=True, database=tmp_path / "reply.sqlite3"))
    await runtime.open(start_tasks=False)
    await runtime.monitor.collect_channels()
    try:
        yield runtime
    finally:
        await runtime.close()


async def test_greeting_is_not_treated_as_failed_data_query(runtime):
    calls = []

    def bad(request):
        calls.append(request)
        return httpx.Response(401, json={"error": {"message": "secret-value"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as http:
        bot = Assistant(
            "A",
            "真实站点",
            runtime.commands,
            LLMConfig(
                enabled=True,
                base_url="https://model.example/v1",
                api_key="secret-key",
                model="model",
            ),
            http,
        )
        reply = await bot.execute("你好", source="wecom")
    assert reply.ok and "你好" in reply.text and "真实站点" in reply.text
    assert "不可用" not in reply.text and calls == []


async def test_provider_401_is_explained_without_raw_response(runtime):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(401, json={"error": {"message": "leaked-secret-token"}})
        )
    ) as http:
        bot = Assistant(
            "A",
            "A",
            runtime.commands,
            LLMConfig(
                enabled=True,
                base_url="https://model.example/v1",
                api_key="secret-key",
                model="model",
            ),
            http,
        )
        reply = await bot.execute("查看渠道信息", source="wecom")
    diagnostic = json.loads(await runtime.store.get_meta("llm_diagnostic"))
    assert diagnostic["code"] == "authentication_failed" and diagnostic["http_status"] == 401
    assert "leaked-secret-token" not in json.dumps(diagnostic) and "secret-key" not in json.dumps(
        diagnostic
    )
    assert "鉴权" in reply.text and "DEMO 主渠道" in reply.text
    assert "默认" not in reply.text


async def test_probe_auth_failure_is_single_request_with_actionable_error():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(401, json={"error": {"message": "private-body"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await Provider(
            LLMConfig(
                enabled=True, base_url="https://model.example/v1", api_key="key", model="model"
            ),
            http,
        ).probe()
    assert result["ok"] is False and result["code"] == "authentication_failed"
    assert "Key" in result["message"] and len(calls) == 1
    assert "private-body" not in json.dumps(result)


async def test_probe_tests_tool_support_after_successful_auth():
    replies = [
        httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]}),
        httpx.Response(
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
        ),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: replies.pop(0))) as http:
        result = await Provider(
            LLMConfig(
                enabled=True, base_url="https://model.example/v1", api_key="key", model="model"
            ),
            http,
        ).probe()
    assert result["ok"] is True and result["tools_supported"] is True
