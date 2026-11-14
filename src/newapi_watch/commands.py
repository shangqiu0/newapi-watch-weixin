"""Deterministic shared commands. Actor identity never changes permissions."""

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .audit import Audit, AuditRecord, Outcome, Source
from .client import NewAPIClient, UpstreamError
from .config import Settings
from .data_queries import DataQueries
from .health import Health
from .management_commands import operational_command, reporting_command
from .models import CommandResult
from .monitor import Monitor
from .notifications import truncate_utf8
from .operations import OperationError, Operations
from .plugins import CommandHandler
from .redaction import redact_text
from .reports import Reports, day_window
from .store import Store

logger = logging.getLogger(__name__)
STATUS = {0: "未知", 1: "启用", 2: "手动禁用", 3: "自动禁用"}
HELP = """**NewAPI 运维助手 · 帮助**
所有使用者权限一致，无账号绑定或角色区分。
- 渠道状态 / 渠道 12 详情 / 渠道 12 测试
- 用户 张三（查询平台用户额度与用量，不返回密钥）
- 日志 错误 今日 / 统计 / 统计 近7天
- 排行 模型 近7天 / 排行 用户 昨天 按调用
- 图表 渠道 近7天 / 图表 2026-11-01~2026-11-03
- 图表 今日消耗 / 图表 2026-11-09
- 日报 / 日报 昨天 / 日报 2026-11-09
- 告警 列表 / 告警 静默 渠道12 1h / 告警 取消静默 渠道12
- 告警 确认 <告警ID> / 告警 静默列表
- 通知 死信 / 通知 重试 <通知ID> / 审计 列表
- 健康 / 帮助
主动测试需统一开启 WATCH_PROBE_ENABLED；不支持渠道启停或删除。
"""


class Commands:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: NewAPIClient,
        monitor: Monitor,
        reports: Reports,
    ) -> None:
        self.settings, self.store, self.client = settings, store, client
        self.monitor, self.reports = monitor, reports
        self.extensions: dict[str, CommandHandler] = {}
        self.operations = Operations(store)
        self.audits = Audit(settings, store)
        self.health = Health(settings, store)
        self.query_engine: Any = None
        self.error_search: Any = None
        self.live_queries = False
        self.upstream: Any = None
        self.data = DataQueries(settings, store, client, reports)

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
        text = re.sub(r"\s+", " ", text)
        started = time.monotonic()
        outcome: Outcome = "success"
        try:
            if len(text) > 2000:
                result = CommandResult(text="命令过长，请发送“帮助”查看格式", ok=False)
            else:
                result = await self._dispatch(
                    text, actor=actor, request_id=request_id or uuid4().hex
                )
            if not result.ok:
                outcome = "invalid_input"
        except UpstreamError as exc:
            outcome = "upstream_error"
            result = CommandResult(text=str(exc), ok=False)
        except OperationError as exc:
            outcome = "conflict"
            result = CommandResult(text=str(exc), ok=False)
        except ValueError:
            outcome = "invalid_input"
            result = CommandResult(
                text="参数或日期无效，请使用 YYYY-MM-DD；范围最多31天。发送“帮助”查看格式", ok=False
            )
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            action = {
                "渠道": "channels",
                "用户": "users",
                "日志": "logs",
                "图表": "report",
                "日报": "report",
                "统计": "report",
                "排行": "ranking",
                "告警": "alerts",
                "通知": "notifications",
                "审计": "audits",
                "健康": "health",
            }.get(self._kind(text), "help")
            if text.split(" ", 1)[0] in self.extensions:
                action = "extension"
            await self.audits.record(
                AuditRecord.model_validate(
                    {
                        "source": source,
                        "actor_hash": self.audits.fingerprint(actor),
                        "action": action,
                        "outcome": outcome,
                        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                    }
                )
            )
        result.text = redact_text(result.text, self.settings)
        result.text = truncate_utf8(result.text, 14000)
        logger.info("command_completed kind=%s outcome=%s", self._kind(text), outcome)
        return result

    @staticmethod
    def _kind(text: str) -> str:
        return next(
            (
                kind
                for kind in (
                    "渠道",
                    "用户",
                    "日志",
                    "图表",
                    "日报",
                    "告警",
                    "健康",
                    "统计",
                    "排行",
                    "通知",
                    "审计",
                )
                if text.startswith(kind)
            ),
            "help",
        )

    def _day(self, value: str, *, yesterday: bool = False) -> date:
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        if value in {"", "昨天"}:
            return today - timedelta(days=1) if yesterday or value == "昨天" else today
        if value in {"今天", "今日", "今日消耗"}:
            return today
        result = date.fromisoformat(value)
        if result > today or result < today - timedelta(days=366):
            raise ValueError("日期超出查询范围")
        return result

    async def _dispatch(self, text: str, *, actor: str, request_id: str) -> CommandResult:
        prefix = text.split(" ", 1)[0]
        if prefix in self.extensions:
            return await self.extensions[prefix](text)
        if text in {"帮助", "help", ""}:
            return CommandResult(text=HELP)
        reporting = await reporting_command(text, self.reports, self.settings)
        if reporting is not None:
            return reporting
        operational = await operational_command(
            text, self.operations, self.audits, actor=actor, request_id=request_id
        )
        if operational is not None:
            return operational
        if text in {"渠道", "渠道状态", "渠道 状态", "channels"}:
            return await self._channels()
        if match := re.fullmatch(r"渠道\s*(\d+)(?:\s*(详情|测试))?", text):
            return await self._channel(int(match[1]), match[2] or "详情")
        if text.startswith("用户 "):
            return await self._users(text[3:].strip())
        if text.startswith("日志"):
            start, _ = day_window(self._day("今天"), self.settings.timezone)
            items = (
                (await self.data.logs(start, int(time.time()), log_type=5))["items"]
                if self.live_queries
                else await self.store.recent_errors(start)
            )
            return CommandResult(
                text="今日错误记录（最多20条，不展示原始内容）：\n"
                + (
                    "\n".join(
                        f"#{item['id']} 渠道{item['channel_id']} · {item['model_name']}"
                        for item in items
                    )
                    or "暂无已采集错误记录；不代表上游必然无错误。"
                )
            )
        if text.startswith("日报"):
            return await self.reports.daily(self._day(text[2:].strip(), yesterday=True))
        if text.startswith("告警 "):
            return await self._silence(text)
        if text in {"健康", "health"}:
            data = await self.health.snapshot()
            return CommandResult(
                text=f"模式：{'DEMO' if self.settings.demo else '生产'}\n运行状态：{data['status']}\n渠道新鲜：{data['channels_fresh']} · 日志新鲜：{data['logs_fresh']}\n机器人：{data['bot']['status']}\n通知队列：{data['queue']}\n异常原因：{', '.join(data['reasons']) or '无'}\n所有接入者权限一致"
            )
        return CommandResult(text=HELP, ok=False)

    async def _channels(self) -> CommandResult:
        items = (
            (await self.data.channels())["items"]
            if self.live_queries
            else await self.store.channels()
        )
        header = "**渠道状态**（最多展示50条；接口可分页查询全部）\n"
        content = "\n".join(
            f"#{item['id']} {item['name']} · {STATUS[item['status']]}" for item in items[:50]
        )
        return CommandResult(text=header + (content or "暂无渠道或尚未采集，请查看“健康”。"))

    async def _channel(self, channel_id: int, action: str) -> CommandResult:
        items = (
            (await self.data.channels())["items"]
            if self.live_queries
            else await self.store.channels()
        )
        item = next((item for item in items if item["id"] == channel_id), None)
        if item is None:
            return CommandResult(text="渠道不存在或尚未采集", ok=False)
        if action == "测试":
            if not self.settings.probe_enabled:
                return CommandResult(
                    text="主动测试未开启；所有使用者均需统一开启 WATCH_PROBE_ENABLED", ok=False
                )
            success = await self.monitor.probe(channel_id)
            return CommandResult(
                text=f"渠道 {channel_id} 主动测试：{'成功' if success else '失败'}（测试可能消耗额度；短时间重复查询复用最近结果）"
            )
        if self.live_queries:
            return CommandResult(
                text=f"渠道 #{channel_id} {item['name']}\n状态：{STATUS[item['status']]}\n余额：{item.get('balance', '未提供')}\n快照响应时间：{item['response_time']} ms\n来源：绑定平台管理接口；用量请按需查询统计，不在后台采集历史日志"
            )
        stats = await self.store.summary(int(time.time()) - 86400, int(time.time()) + 1, channel_id)
        return CommandResult(
            text=f"**渠道 #{channel_id} {item['name']}**\n状态：{STATUS[item['status']]}\n余额：{item['balance']}\n快照响应时间：{item['response_time']} ms\n近24小时成功/错误记录：{stats['success_records']}/{stats['error_records']}\n数据为最近成功采集快照，并非实时保证。"
        )

    async def _users(self, keyword: str) -> CommandResult:
        if not keyword or len(keyword) > 100:
            raise ValueError("查询关键字过长")
        users = await self.data.users(keyword)
        return CommandResult(
            text="**用户查询**（最多20条，按当前平台额度显示口径）\n"
            + (
                "\n".join(
                    f"#{item.id} {item.username} {item.display_name} · 剩余额度 {self.settings.currency.display(item.quota)} · 已用 {self.settings.currency.display(item.used_quota)} · 请求 {item.request_count} · 分组 {item.group}"
                    for item in users
                )
                or "没有匹配用户"
            )
        )

    async def _silence(self, text: str) -> CommandResult:
        if match := re.fullmatch(r"告警 静默 渠道\s*(\d+) (\d+)(m|h)", text):
            seconds = int(match[2]) * (60 if match[3] == "m" else 3600)
            if seconds < 60 or seconds > 7 * 86400:
                raise ValueError("静默时间不合法")
            await self.store.silence(int(match[1]), time.time() + seconds, time.time())
            return CommandResult(
                text=f"渠道 {match[1]} 后续通知已静默 {match[2]}{match[3]}；状态仍记录，已入队通知不撤回。"
            )
        if match := re.fullmatch(r"告警 取消静默 渠道\s*(\d+)", text):
            await self.store.silence(int(match[1]), 0, time.time())
            return CommandResult(text=f"渠道 {match[1]} 已取消静默")
        return CommandResult(text="格式：告警 静默 渠道12 1h；或 告警 取消静默 渠道12", ok=False)
