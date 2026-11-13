import json

import httpx
import pytest

from newapi_watch.assistant import Assistant
from newapi_watch.config import Settings
from newapi_watch.control_models import LLMConfig
from newapi_watch.runtime import Runtime


@pytest.fixture
async def runtime(tmp_path):
    runtime = Runtime(
        Settings(_env_file=None, demo=True, database=tmp_path / "ai.sqlite3", report_enabled=False)
    )
    await runtime.open(start_tasks=False)
    try:
        await runtime.monitor.collect_channels()
        await runtime.monitor.collect_logs()
        yield runtime
    finally:
        await runtime.close()


async def test_model_calls_real_scoped_data_tools_not_help_wall(runtime):
    requests = []

    def model(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers["Authorization"] == "Bearer llm-secret"
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {"name": "get_channels", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        tool = next(item for item in payload["messages"] if item["role"] == "tool")
        assert "DEMO 主渠道" in tool["content"]
        assert "llm-secret" not in tool["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "已查询：主渠道启用，备用渠道为手动禁用，不属于自动故障。",
                        }
                    }
                ]
            },
        )

    config = LLMConfig(
        enabled=True, base_url="https://model.example/v1", api_key="llm-secret", model="test-model"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        assistant = Assistant("platform-A", "生产A", runtime.commands, config, http)
        result = await assistant.execute("现在渠道怎么样？", actor="u1", conversation_id="chat1")
    assert result.ok and "已查询" in result.text
    assert len(requests) == 2
    assert all("llm-secret" not in json.dumps(item) for item in requests)


async def test_tool_cannot_switch_platform_or_execute_mutation(runtime):
    from newapi_watch.llm_tools import Tools

    tools = Tools(runtime.commands)
    result = await tools.call("get_channels", '{"platform_id":"B"}')
    assert result["ok"] is False
    assert (await tools.call("run_sql", '{"sql":"DROP TABLE logs"}'))["ok"] is False
    assert (await tools.call("disable_channel", '{"channel_id":1}'))["ok"] is False
    assert len(await runtime.store.channels()) == 2


async def test_conversation_memory_isolated_per_sender_and_chat(runtime):
    received = []

    def model(request):
        payload = json.loads(request.content)
        received.append(payload["messages"])
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "已收到。"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        assistant = Assistant(
            "A",
            "A",
            runtime.commands,
            LLMConfig(
                enabled=True, base_url="https://model.example/v1", api_key="key", model="test"
            ),
            http,
        )
        await assistant.execute("unique-private-question", actor="one", conversation_id="group")
        await assistant.execute("followup", actor="two", conversation_id="group")
        await assistant.execute("followup", actor="one", conversation_id="private")
    assert "unique-private-question" not in json.dumps(received[1])
    assert "unique-private-question" not in json.dumps(received[2])


async def test_model_failure_does_not_break_monitor_or_invent_data(runtime):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, text="private-error"))
    ) as http:
        assistant = Assistant(
            "A",
            "A",
            runtime.commands,
            LLMConfig(
                enabled=True, base_url="https://model.example/v1", api_key="key", model="test"
            ),
            http,
        )
        result = await assistant.execute("帮我分析最近的用量异常")
        assert not result.ok and "暂时不可用" in result.text
        assert "private-error" not in result.text
        assert await runtime.monitor.collect_channels()


async def test_without_model_capability_question_is_conversational(runtime):
    async with httpx.AsyncClient() as http:
        assistant = Assistant("A", "内部站", runtime.commands, LLMConfig(), http)
        result = await assistant.execute("你有哪些功能？")
        assert "内部站" in result.text and "可以" in result.text
        assert "无账号绑定或角色区分" not in result.text
        assert "未启用" in result.text


async def test_capability_tool_is_not_evidence_for_operational_claim(runtime):
    calls = 0

    def model(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {"name": "get_capabilities", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "今日消耗888888元，所有渠道正常。",
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        assistant = Assistant(
            "A",
            "A",
            runtime.commands,
            LLMConfig(
                enabled=True, base_url="https://model.example/v1", api_key="key", model="test"
            ),
            http,
        )
        result = await assistant.execute("今天渠道状态和消耗如何？", source="wecom")
    assert "888888" not in result.text
    assert "快照" in result.text and not result.ok


async def test_read_tools_expose_freshness_and_structured_metrics(runtime):
    from newapi_watch.llm_tools import Tools

    tools = Tools(runtime.commands)
    channels = await tools.call("get_channels", "{}")
    assert "collected_at" in channels and "fresh" in channels
    stats = await tools.call(
        "get_statistics", '{"period":"近7天","dimension":"model","metric":"quota","chart":false}'
    )
    assert stats["ok"] and "summary" in stats and "coverage" in stats
    assert "text" not in stats
