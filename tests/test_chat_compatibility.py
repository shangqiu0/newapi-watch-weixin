import json

import httpx
import pytest

from newapi_watch.control_models import LLMConfig
from newapi_watch.model_provider import Provider


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
async def test_deepseek_official_disables_thinking_for_forced_tool_chat(base_url):
    def handler(request):
        payload = json.loads(request.content)
        if payload.get("thinking") != {"type": "disabled"}:
            return httpx.Response(
                400,
                json={"error": {"message": "Thinking mode does not support tool_choice required"}},
            )
        if payload.get("tools"):
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
        return httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await Provider(
            LLMConfig(enabled=True, base_url=base_url, api_key="not-real", model="deepseek-flash"),
            http,
        ).probe()
    assert result["ok"] is True
    assert result["protocol"] == "chat_completions" and result["compatibility"] == "deepseek"


async def test_openai_compatible_provider_does_not_receive_deepseek_specific_parameters():
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append((str(request.url), payload))
        assert "thinking" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = Provider(
            LLMConfig(
                base_url="https://compatible.example/v1", api_key="not-real", model="any-model"
            ),
            http,
        )
        result = await provider.complete({"messages": [{"role": "user", "content": "hello"}]})
    assert result["content"] == "done"
    assert seen[0][0] == "https://compatible.example/v1/chat/completions"


@pytest.mark.parametrize(
    "base_url,expected",
    [
        (
            "https://compatible.example/v1/chat/completions",
            "https://compatible.example/v1/chat/completions",
        ),
        ("https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
        ("https://api.deepseek.com/", "https://api.deepseek.com/chat/completions"),
    ],
)
async def test_chat_endpoint_accepts_base_url_or_complete_endpoint(base_url, expected):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await Provider(
            LLMConfig(base_url=base_url, api_key="not-real", model="model"), http
        ).complete({"messages": []})
    assert seen == [expected]


async def test_relay_can_explicitly_select_deepseek_compatibility():
    config = LLMConfig(
        base_url="https://relay.example/v1",
        api_key="not-real",
        model="alias",
        compatibility="deepseek",
    )

    def handler(request):
        assert json.loads(request.content)["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert (await Provider(config, http).complete({"messages": []}))["content"] == "ok"
