"""Bounded OpenAI-compatible tool loop with platform-scoped data and memory."""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import httpx

from .assistant_policy import CAPABILITIES, HELP, MUTATIONS, fallback, prompt
from .audit import Source
from .client import UpstreamError
from .commands import Commands
from .control_models import LLMConfig
from .llm_tools import Tools
from .model_provider import ModelFailure, Provider
from .models import CommandResult
from .redaction import redact_text

logger = logging.getLogger(__name__)


class Assistant:
    def __init__(
        self,
        platform_id: str,
        platform_name: str,
        commands: Commands,
        config: LLMConfig,
        http: httpx.AsyncClient,
        *,
        binding_id: str = "web",
    ) -> None:
        self.platform_id, self.platform_name, self.commands = platform_id, platform_name, commands
        self.config, self.http, self.binding_id = config, http, binding_id
        self.memory: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self.active = 0

    async def execute(
        self,
        text: str,
        *,
        actor: str = "",
        source: Source = "local",
        request_id: str = "",
        conversation_id: str = "",
    ) -> CommandResult:
        text = re.sub(r"^@\S+\s+", "", text.strip())
        if not 1 <= len(text) <= 2000:
            return CommandResult(text="请用一句话描述想查询的问题，最多2000字。", ok=False)
        if re.fullmatch(
            r"(?:你好|您好|嗨|哈喽|hello|hi|早上好|下午好|晚上好)[！!，,。.？?\s]*",
            text,
            re.IGNORECASE,
        ):
            return CommandResult(
                text=f"你好！我是「{self.platform_name}」的运维助手。你想看看哪些渠道需要注意，还是查一下用量？直接说就好。"
            )
        if text in {"帮助", "help"}:
            return CommandResult(text=HELP)
        # Only explicit local management actions bypass the model. No read query
        # is pasted verbatim to WeCom once LLM is enabled, even short commands.
        if MUTATIONS.match(text):
            return await self.commands.execute(
                text, actor=actor, source=source, request_id=request_id
            )
        if not self.config.enabled:
            result = await fallback(
                self.commands,
                text,
                model_failed=False,
                actor=actor,
                source=source,
                request_id=request_id,
            )
            if "功能" in text or "能做" in text:
                result.text = f"我是「{self.platform_name}」的运维助手。" + result.text
            return result
        if self.active >= 4:
            return CommandResult(text="当前问答较多，请稍后再试；监控仍在正常运行。", ok=False)
        self.active += 1
        try:
            async with asyncio.timeout(50):
                return await self._ask(text, actor, conversation_id)
        except ModelFailure as error:
            await self.commands.store.set_meta(
                "llm_diagnostic", json.dumps(error.diagnostic(), ensure_ascii=False)
            )
            logger.warning("model_request_failed code=%s status=%s", error.code, error.http_status)
            result = await fallback(
                self.commands,
                text,
                model_failed=True,
                actor=actor,
                source=source,
                request_id=request_id,
            )
            if error.code == "authentication_failed":
                result.text = result.text.replace(
                    "智能表达暂时不可用，我先根据已采集数据回答：",
                    "模型鉴权暂未通过，我先按本平台快照说明：",
                )
            return result
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            TimeoutError,
            UpstreamError,
        ):
            result = await fallback(
                self.commands,
                text,
                model_failed=True,
                actor=actor,
                source=source,
                request_id=request_id,
            )
            result.text = redact_text(result.text, self.commands.settings)
            return result
        finally:
            self.active -= 1

    async def _ask(self, text: str, actor: str, conversation_id: str) -> CommandResult:
        key = hashlib.sha256(
            f"{self.platform_id}:{self.binding_id}:{conversation_id}:{actor}".encode()
        ).hexdigest()
        now = time.time()
        history = self.memory.get(key, (0, []))
        previous = history[1] if now - history[0] < 1800 else []
        system = prompt(self.platform_name, self.commands.settings.timezone)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *previous,
            {"role": "user", "content": text},
        ]
        tools = Tools(self.commands)
        final = ""
        for _ in range(10):
            message = await Provider(self.config, self.http).complete(
                {
                    "messages": messages,
                    "tools": tools.definitions(),
                    "tool_choice": "required" if not tools.used else "auto",
                    "max_tokens": 1200,
                }
            )
            calls = message.get("tool_calls") or []
            if not calls:
                evidence = [name for name in tools.successful if name != "get_capabilities"]
                capability_question = bool(CAPABILITIES.search(text)) or text in {
                    "你好",
                    "谢谢",
                    "您好",
                }
                if not evidence and not capability_question and tools.failures:
                    # A real tool call denied by upstream is not a model protocol
                    # failure. Do not allow a provider to turn a 429 into zero use.
                    failures = "；".join(dict.fromkeys(tools.failures))
                    return CommandResult(
                        text=f"当前机器人绑定的平台管理接口未能完成查询：{failures}。本次没有获得完整数据，不能据此判断为零用量或渠道正常；请检查平台接口权限、限流或稍后重试。",
                        ok=False,
                    )
                if not tools.successful or (not capability_question and not evidence):
                    # Providers that ignore tool_choice must not fabricate operational answers.
                    raise ModelFailure("unsupported_tools", 200)
                final = message.get("content") or ""
                break
            if len(calls) > 6:
                raise ValueError("too many tools")
            messages.append(
                {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
            )
            for call in calls:
                function = call["function"]
                result = await tools.call(function["name"], function["arguments"])
                content = json.dumps(result, ensure_ascii=False)
                model_key = self.config.api_key.get_secret_value()
                if model_key:
                    content = content.replace(model_key, "[REDACTED]")
                if len(content) > 30000:
                    content = json.dumps(
                        {"ok": False, "error": "结果过大，请缩小查询范围"}, ensure_ascii=False
                    )
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        if not isinstance(final, str) or not final.strip():
            raise ModelFailure("invalid_response", 200)
        final = redact_text(final, self.commands.settings).replace(
            self.config.api_key.get_secret_value() or "\x00", "[REDACTED]"
        )[:12000]
        self.memory[key] = (
            now,
            [*previous, {"role": "user", "content": text}, {"role": "assistant", "content": final}][
                -8:
            ],
        )
        self.memory.move_to_end(key)
        while len(self.memory) > 128:
            self.memory.popitem(last=False)
        await self.commands.store.set_meta(
            "llm_diagnostic",
            json.dumps(
                {
                    "ok": True,
                    "code": "ready",
                    "message": "最近一次模型查询成功",
                    "checked_at": time.time(),
                },
                ensure_ascii=False,
            ),
        )
        return CommandResult(text=final, image=tools.image)
