import json

import httpx
import pytest
from aibot import WSClient, WSClientOptions
from pydantic import ValidationError

from newapi_watch.bot import SDKLogger
from newapi_watch.config import Settings
from newapi_watch.models import Channel
from newapi_watch.runtime import Runtime


class WeComTransport:
    def __init__(self):
        self.sent = []

    async def send_reply(self, request_id, body, cmd):
        self.sent.append((request_id, body, cmd))
        return {"errcode": 0}


@pytest.mark.parametrize(
    "chat_type,text", [("group", "@机器人 现在渠道怎么样？"), ("single", "渠道状态")]
)
async def test_legacy_runtime_wecom_uses_model_tools_for_natural_and_short_queries(
    tmp_path, chat_type, text
):
    calls = []

    def model(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if not any(message["role"] == "tool" for message in payload["messages"]):
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
                                        "id": "actual-read",
                                        "type": "function",
                                        "function": {"name": "get_channels", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        result = json.loads(payload["messages"][-1]["content"])
        enabled = sum(item["status"] == 1 for item in result["items"])
        assert enabled == 1
        assert result["items"][1]["name"] == "高延迟备用"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "目前采集到两个渠道：主渠道启用，备用渠道是手动禁用，不能直接当作故障。现有快照不足以判断上游是否真正可用。",
                        }
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "bot.sqlite3",
        report_enabled=False,
        llm_enabled=True,
        llm_base_url="https://model.example/v1",
        llm_api_key="relay-model-key",
        llm_model="tool-model",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:

        def upstream(request):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "items": [
                            {"id": 1, "name": "实际主渠道", "status": 1},
                            {"id": 2, "name": "高延迟备用", "status": 2},
                        ],
                        "total": 2,
                    },
                },
            )

        platform_http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        runtime = Runtime(settings, http=platform_http, llm_http=model_http)
        await runtime.open(start_tasks=False)
        try:
            await runtime.store.sync_channels(
                [
                    Channel(id=1, name="实际主渠道", status=1),
                    Channel(id=2, name="高延迟备用", status=2),
                ],
                100,
            )
            sdk = WSClient(WSClientOptions(bot_id="offline", secret="offline", logger=SDKLogger()))
            transport = WeComTransport()
            sdk._ws_manager = transport
            runtime.bot.client = sdk
            frame = {
                "headers": {"req_id": "wecom-real-path"},
                "body": {
                    "msgid": "m1",
                    "chattype": chat_type,
                    "chatid": "chat-A",
                    "from": {"userid": "user-A"},
                    "text": {"content": text},
                },
            }
            await runtime.bot._on_text(frame)
            await runtime.bot._on_text(frame)
            final = [body["stream"] for _, body, _ in transport.sent if body["stream"]["finish"]]
            assert len(calls) == 2 and len(final) == 1
            assert "两个渠道" in final[0]["content"]
            assert "命令列表" not in final[0]["content"] and "无账号绑定" not in final[0]["content"]
            assert len(transport.sent) == 2
            assert transport.sent[0][1]["stream"]["finish"] is False
            assert transport.sent[0][1]["stream"]["id"] == final[0]["id"]
        finally:
            await runtime.close()
            await platform_http.aclose()


@pytest.mark.parametrize(
    "values", [{"llm_enabled": True}, {"llm_enabled": True, "llm_base_url": "file:///tmp/x"}]
)
def test_enabled_model_requires_own_valid_provider_credentials(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, demo=True, **values)


async def test_model_cannot_return_unverified_operational_claims_without_read_tools(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "全部渠道正常，今日消耗999999元。",
                            }
                        }
                    ]
                },
            )
        )
    ) as model:
        runtime = Runtime(
            Settings(
                _env_file=None,
                demo=True,
                database=tmp_path / "claim.sqlite3",
                llm_enabled=True,
                llm_base_url="https://model.example/v1",
                llm_api_key="key",
                llm_model="tool",
            ),
            llm_http=model,
        )
        await runtime.open(start_tasks=False)
        try:
            await runtime.store.sync_channels([Channel(id=1, name="已自动禁用", status=3)], 100)
            response = await runtime.assistant.execute("渠道现在正常吗？", source="wecom")
            assert "999999" not in response.text and "全部渠道正常" not in response.text
            assert "自动禁用" in response.text
            assert "模型" in response.text or "智能" in response.text
        finally:
            await runtime.close()


async def test_model_outage_returns_actual_local_snapshot_not_help_wall(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as model:
        runtime = Runtime(
            Settings(
                _env_file=None,
                demo=True,
                database=tmp_path / "fallback.sqlite3",
                llm_enabled=True,
                llm_base_url="https://model.example/v1",
                llm_api_key="key",
                llm_model="tool",
            ),
            llm_http=model,
        )
        await runtime.open(start_tasks=False)
        try:
            await runtime.store.sync_channels(
                [Channel(id=1, name="A", status=1), Channel(id=2, name="B", status=3)], 100
            )
            response = await runtime.assistant.execute("哪些渠道有问题？", source="wecom")
            assert "自动禁用" in response.text and "B" in response.text
            assert "帮助" not in response.text and "无账号绑定" not in response.text
            assert not response.ok
        finally:
            await runtime.close()
