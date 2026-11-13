"""Small deterministic parsers for statistics and local operational commands."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .audit import Audit
from .config import Settings
from .models import CommandResult
from .operations import Operations
from .periods import StatisticsQuery, parse_period
from .plotting import ChartKind
from .reports import Reports

DIMENSIONS = {"模型": "model", "渠道": "channel", "用户": "user"}
METRICS = {"消耗": "quota", "调用": "calls", "错误": "errors", "Token": "tokens", "token": "tokens"}


async def reporting_command(
    text: str, reports: Reports, settings: Settings
) -> CommandResult | None:
    match = re.fullmatch(r"(统计|stats|排行|图表)(?:\s+(.*))?", text)
    if not match:
        return None
    command, tail = match[1], (match[2] or "")
    dimension = "model"
    chart_kind: ChartKind = "trend"
    for word, value in DIMENSIONS.items():
        if tail == word or tail.startswith(word + " "):
            tail = tail[len(word) :].strip()
            dimension, chart_kind = value, "ranking"
            break
    metric = "quota"
    if metric_match := re.search(r"\s+按(消耗|调用|错误|Token|token)$", tail):
        metric = METRICS[metric_match[1]]
        tail = tail[: metric_match.start()]
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    period = parse_period(tail, today=today)
    query = StatisticsQuery.model_validate(
        {**period.model_dump(), "dimension": dimension, "metric": metric}
    )
    return await reports.range_report(query, with_image=command == "图表", chart_kind=chart_kind)


async def operational_command(
    text: str, ops: Operations, audits: Audit, *, actor: str, request_id: str
) -> CommandResult | None:
    if text in {"告警", "告警 列表"}:
        page = await ops.alerts()
        lines = ["**最近告警**"] + [
            f"{item['id']} [{item['state']}] {'已确认' if item['acknowledged'] else '未确认'} · {item['title']}"
            for item in page["items"]
        ]
        return CommandResult(text="\n".join(lines) if page["items"] else "暂无告警")
    if match := re.fullmatch(r"告警 确认 ([a-fA-F0-9]{32})", text):
        result = await ops.acknowledge(match[1], actor_hash=audits.fingerprint(actor))
        return CommandResult(
            text=f"告警 {match[1]} 已确认知晓，不代表故障恢复。"
            + ("（重复确认）" if result["duplicate"] else "")
        )
    if text in {"告警 静默列表", "静默 列表"}:
        page = await ops.silences()
        return CommandResult(
            text="**当前静默**\n"
            + (
                "\n".join(
                    f"渠道 {item['channel_id']}，到期时间戳 {item['until_at']:g}"
                    for item in page["items"]
                )
                or "暂无有效静默"
            )
        )
    if text == "通知 死信":
        page = await ops.dead_letters()
        return CommandResult(
            text="**死信通知**\n"
            + (
                "\n".join(
                    f"#{item['id']} 已尝试 {item['attempts']} 次 · {item['last_error']}"
                    for item in page["items"]
                )
                or "暂无死信"
            )
        )
    if match := re.fullmatch(r"通知 重试 (\d+)", text):
        result = await ops.retry(int(match[1]), request_id)
        return CommandResult(
            text=f"通知 {match[1]} "
            + (
                "重复请求，未再次重置"
                if result["duplicate"]
                else "已重新入队，将按原有限速投递历史通知"
            )
        )
    if text == "审计 列表":
        page = await audits.list()
        return CommandResult(
            text="**命令审计（不记录原始输入）**\n"
            + (
                "\n".join(
                    f"{item['source']} · {item['action']} · {item['outcome']} · {item['duration_ms']}ms"
                    for item in page["items"]
                )
                or "暂无记录"
            )
        )
    return None
