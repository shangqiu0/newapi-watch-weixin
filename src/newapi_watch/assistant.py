"""Bounded OpenAI-compatible tool loop with platform-scoped data and memory."""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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
        if self.commands.query_engine is not None and (
            match := re.fullmatch(r"任务\s+(q_[a-f0-9]{32})", text)
        ):
            owner = (
                "console"
                if self.binding_id == "web"
                else f"{self.platform_id}:{self.binding_id}:{conversation_id}:{actor}"
            )
            try:
                task = await self.commands.query_engine.jobs.get(match[1], owner)
                labels = {
                    "queued": "等待执行",
                    "running": "正在读取",
                    "waiting": "限流等待",
                    "paused": "已暂停",
                    "complete": "已完成",
                    "cancelled": "已取消",
                    "failed": "失败",
                }
                text = f"查询任务 {task['id']}：{labels.get(task['status'], task['status'])}，已读取 {task['progress'].get('records_read', 0):,} 条。"
                if task["status"] == "complete" and task.get("result"):
                    summary = task["result"]["summary"]
                    text += f"\n消费记录：{summary['success_records']:,} · 错误记录：{summary['error_records']:,}\n消耗：{self.commands.settings.currency.display(summary['quota'])} · Token：{summary['tokens']:,}"
                    text += "\n排行：" + "；".join(
                        f"{i['label']} {self.commands.settings.currency.display(i['quota'])}"
                        for i in task["result"]["ranking"][:5]
                    )
                else:
                    text += "\n" + task["error"]
                return self._redact_result(CommandResult(text=text))
            except ValueError:
                return CommandResult(text="任务不存在或不属于当前机器人会话", ok=False)
        if match := re.fullmatch(
            r"(?:查(?:询)?\s*)?(?:渠道\s*#?\s*([1-9]\d{0,18})|([1-9]\d{0,18})\s*号\s*渠道)"
            r"\s*(?:今天|今日)\s*(?:用量|消耗|用了多少(?:量|额度|钱)?|消耗了多少)[？?。!！]?",
            text,
        ):
            channel_id = int(match[1] or match[2])
            try:
                async with asyncio.timeout(12):
                    usage = await self.commands.data.channel_today_usage(channel_id)
                amount = self.commands.settings.currency.display(usage["quota"])
                through = datetime.fromtimestamp(
                    usage["through_timestamp"], ZoneInfo(usage["timezone"])
                )
                return self._redact_result(
                    CommandResult(
                        text=f"渠道 #{channel_id} · {usage['channel_name']}：今日已用 {amount}。"
                        f"\n统计范围：{through:%Y-%m-%d} 00:00:00–{through:%H:%M:%S}（{usage['timezone']}）。"
                    )
                )
            except (UpstreamError, TimeoutError) as exc:
                reason = str(exc) or "上游查询超时"
                return self._redact_result(
                    CommandResult(
                        text=f"未能查询渠道 #{channel_id} 的今日用量：{reason}。未取得消耗数据，不按零计。",
                        ok=False,
                    )
                )
        if text in {"会话ID", "会话 ID", "当前会话ID"}:
            return CommandResult(
                text="当前会话 ID：" + conversation_id.split(":", 1)[-1]
                if source == "wecom" and conversation_id
                else "请在需要接收告警的企业微信会话中发送“会话ID”。"
            )
        usage_words = r"(?:今日|今天)\s*(?:已用(?:多少)?|用量|消耗(?:了多少)?|用了多少(?:钱|量)?)"
        if match := re.fullmatch(
            rf"(?:查询|查看)?\s*上游(?:账户|账号)?(?:\s*{usage_words}(?:\s+(.+?))?"
            rf"|\s+(.+?)\s*{usage_words})[？?。!！]?",
            text,
        ):
            return await self._upstream_today_usage((match[1] or match[2] or "").strip())
        if self.commands.upstream is not None and (
            match := re.fullmatch(r"(?:查询|查看)?上游(?:账户)?余额(?:\s+(.*))?", text)
        ):
            service = self.commands.upstream
            keyword = (match[1] or "").strip()
            accounts = [
                a
                for a in service.visible(self.binding_id)
                if not keyword or keyword in {a.id, a.name}
            ]
            if not accounts:
                return CommandResult(
                    text="当前机器人没有匹配的已启用上游账户，请检查该平台的查询机器人绑定。",
                    ok=False,
                )
            if len(accounts) > 5:
                return CommandResult(
                    text="请指定账户名称或ID：\n"
                    + "\n".join(f"{a.name} · {a.id}" for a in accounts)
                )
            answers = []
            for account in accounts:
                try:
                    result = await service.query(account.id, binding_id=self.binding_id)
                    answers.append(
                        f"{account.name}：可用余额 {result['quota_display']}，已用 {result.get('used_quota_display', '未提供')}（来源：上游自身账户接口）"
                    )
                except ValueError as error:
                    answers.append(f"{account.name}：查询未完成，{error}；未将未知余额当作零。")
            return self._redact_result(CommandResult(text="\n".join(answers)))
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
            return self._redact_result(result)
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
            elif error.code in {"wrong_endpoint", "group_unavailable"}:
                result.text = result.text.replace(
                    "智能表达暂时不可用，我先根据已采集数据回答：",
                    str(error) + "我先尝试返回指定数据：",
                )
            return self._redact_result(result)
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
            return self._redact_result(result)
        finally:
            self.active -= 1

    async def _upstream_today_usage(self, keyword: str) -> CommandResult:
        service = self.commands.upstream
        accounts = (
            [
                account
                for account in service.visible(self.binding_id)
                if not keyword or keyword in {account.id, account.name}
            ]
            if service is not None
            else []
        )
        if not accounts:
            return CommandResult(
                text="当前机器人没有匹配的已启用上游账户，请检查查询机器人绑定或账户名称。",
                ok=False,
            )
        if len(accounts) > 1:
            return self._redact_result(
                CommandResult(
                    text="请指定要查的账户：上游今日已用 账户名称或ID。\n"
                    + "\n".join(f"{a.name} · {a.id}" for a in accounts[:10])
                )
            )
        try:
            usage = await service.query_today_usage(accounts[0].id, binding_id=self.binding_id)
        except ValueError as error:
            return self._redact_result(
                CommandResult(
                    text=f"未能查询上游账户今日用量：{error}。未取得当前数据，不按零计。", ok=False
                )
            )
        timezone = usage["timezone"]
        end = datetime.fromtimestamp(usage["end_at"], ZoneInfo(timezone))
        return self._redact_result(
            CommandResult(
                text=f"上游账户「{usage['account_name']}」今日已用：{usage['quota_display']}。"
                f"\n统计范围：当天零点起，截至 {end.year}年{end.month}月{end.day}日 {end:%H:%M:%S}（{timezone}）。"
            )
        )

    def _redact_result(self, result: CommandResult) -> CommandResult:
        result.text = redact_text(result.text, self.commands.settings).replace(
            self.config.api_key.get_secret_value() or "\x00", "[REDACTED]"
        )
        return result

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
        context = {
            "binding_id": self.binding_id,
            "conversation_id": conversation_id,
            "actor": actor,
        }
        owner = (
            "console"
            if self.binding_id == "web"
            else f"{self.platform_id}:{self.binding_id}:{conversation_id}:{actor}"
        )
        tools = Tools(self.commands, owner=owner, context=context)
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
                evidence = [
                    name
                    for name in tools.successful
                    if name not in {"get_capabilities", "get_upstream_accounts"}
                ]
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
                if (
                    result.get("ok")
                    and result.get("pending")
                    and isinstance(result.get("task"), dict)
                ):
                    task = result["task"]
                    await self.commands.store.set_meta(
                        "llm_diagnostic",
                        json.dumps(
                            {
                                "ok": True,
                                "code": "ready",
                                "message": "模型工具调用成功，历史查询已受理",
                                "checked_at": time.time(),
                            },
                            ensure_ascii=False,
                        ),
                    )
                    return CommandResult(
                        text=f"查询任务已受理：{task['id']}\n我会在后台分批读取并保存进度，不占用本次聊天等待。完成后会尝试回到原会话通知；也可回复“任务 {task['id']}”查看。\n未完成前不会把部分数据当作完整统计。"
                    )
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
