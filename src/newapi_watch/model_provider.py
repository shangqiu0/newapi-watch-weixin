"""Safe model transport and actionable diagnostics, never exposing provider response bodies."""

import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .control_models import LLMConfig

MESSAGES = {
    "authentication_failed": "模型服务鉴权失败（HTTP 401/403）。请在模型设置中更正该服务对应的模型调用Key；管理令牌不能代替。",
    "wrong_endpoint": "模型地址返回网页而非 Chat Completions JSON；域名根地址使用 /v1，也可填写完整 /chat/completions 端点。",
    "group_unavailable": "模型令牌所属分组已弃用或不可用；请在模型服务平台修改令牌分组，并确认该分组允许使用当前模型。",
    "model_unavailable": "指定模型或上游渠道不可用，请核对模型ID及服务端渠道配置。",
    "rate_or_quota": "模型服务限流或额度不足，请检查模型调用额度并稍后重试。",
    "unsupported_tools": "模型或接口未按要求返回工具调用，请选择支持function tools的模型。",
    "provider_error": "模型服务暂时出错，请稍后重试或查看模型服务状态。",
    "connection_failed": "无法连接模型服务，请检查接口地址、网络和证书。",
    "invalid_response": "模型返回的数据格式不兼容或回答为空，请检查兼容接口和模型配置。",
}


class ModelFailure(ValueError):
    def __init__(self, code: str, http_status: int | None = None) -> None:
        super().__init__(MESSAGES[code])
        self.code, self.http_status = code, http_status

    def diagnostic(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": str(self),
            "http_status": self.http_status,
            "checked_at": time.time(),
        }


class Provider:
    def __init__(self, config: LLMConfig, http: httpx.AsyncClient) -> None:
        self.config, self.http = config, http

    @property
    def compatibility(self) -> str:
        if self.config.compatibility != "auto":
            return self.config.compatibility
        return (
            "deepseek"
            if urlsplit(self.config.base_url).hostname == "api.deepseek.com"
            else "openai"
        )

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        parts = urlsplit(base)
        if base.endswith("/chat/completions"):
            return base
        if not parts.path.strip("/") and parts.hostname != "api.deepseek.com":
            base += "/v1"
        return base + "/chat/completions"

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = {"model": self.config.model, "stream": False, **payload}
        # DeepSeek's thinking mode rejects forced tool_choice. Our assistant is a
        # bounded data-tool workflow, so use its documented non-thinking mode.
        # Do not send vendor-specific fields to unrelated OpenAI-compatible APIs.
        if self.compatibility == "deepseek":
            request["thinking"] = {"type": "disabled"}
        try:
            response = await self.http.post(
                self.endpoint,
                headers={"Authorization": "Bearer " + self.config.api_key.get_secret_value()},
                json=request,
                timeout=25,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise ModelFailure("connection_failed") from None
        status = response.status_code
        if status == 403 and self._unavailable_group(response):
            raise ModelFailure("group_unavailable", status)
        if status in {401, 403}:
            raise ModelFailure("authentication_failed", status)
        if status in {402, 429}:
            raise ModelFailure("rate_or_quota", status)
        if status in {404, 503}:
            raise ModelFailure("model_unavailable", status)
        if status != 200:
            # Inspect only to classify a compatibility failure, never persist raw text.
            if status == 400 and any(
                term in response.text.lower() for term in ("tool_choice", "function", "tools")
            ):
                raise ModelFailure("unsupported_tools", status)
            raise ModelFailure("provider_error", status)
        if "text/html" in response.headers.get(
            "content-type", ""
        ).lower() or response.text.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise ModelFailure("wrong_endpoint", status)
        try:
            data = response.json()
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("message shape")
            return message
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelFailure("invalid_response", status) from None

    @staticmethod
    def _unavailable_group(response: httpx.Response) -> bool:
        # Only classify a known upstream condition; never expose provider text,
        # private group names, credentials or request bodies in diagnostics.
        try:
            payload = response.json()
        except ValueError:
            return False
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return False
        message = str(error.get("message", ""))[:2000].lower()
        return (
            "分组" in message and any(word in message for word in ("弃用", "不可用", "禁用"))
        ) or ("group" in message and any(word in message for word in ("deprecated", "disabled")))

    async def probe(self) -> dict[str, Any]:
        try:
            plain = await self.complete(
                {
                    "messages": [{"role": "user", "content": "你好，只需回复一句确认。"}],
                    "max_tokens": 128,
                }
            )
            if not plain.get("content"):
                raise ModelFailure("invalid_response", 200)
            tools = await self.complete(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "请调用get_capabilities工具，不要回答业务数据。",
                        }
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_capabilities",
                                "description": "说明功能",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                },
                            },
                        }
                    ],
                    "tool_choice": "required",
                    "max_tokens": 256,
                }
            )
            calls = tools.get("tool_calls") or []
            if not any(
                isinstance(call, dict)
                and call.get("function", {}).get("name") == "get_capabilities"
                for call in calls
            ):
                raise ModelFailure("unsupported_tools", 200)
            return {
                "ok": True,
                "code": "ready",
                "message": "模型鉴权和工具调用均通过，可用于绑定机器人的智能问答。",
                "http_status": 200,
                "tools_supported": True,
                "protocol": "chat_completions",
                "compatibility": self.compatibility,
                "checked_at": time.time(),
            }
        except ModelFailure as error:
            return error.diagnostic()
