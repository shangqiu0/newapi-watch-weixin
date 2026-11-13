"""Read-only model tools; the bound Commands instance is the only data scope."""

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .analytics import Snapshot
from .client import UpstreamError
from .commands import Commands
from .periods import StatisticsQuery, parse_period
from .redaction import redact_data


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelListArgs(NoArgs):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=100)


class ChannelArgs(NoArgs):
    channel_id: int = Field(gt=0)


class UserArgs(ChannelListArgs):
    keyword: str = Field(min_length=1, max_length=100)


class ErrorArgs(ChannelListArgs):
    period: str = Field(default="今天", max_length=32)
    channel_id: int = Field(default=0, ge=0)


class StatsArgs(NoArgs):
    period: str = Field(default="今天", max_length=32)
    dimension: str = Field(default="model", pattern="^(model|channel|user)$")
    metric: str = Field(default="quota", pattern="^(quota|calls|errors|tokens)$")
    chart: bool = False


SCHEMAS: dict[str, tuple[type[BaseModel], str]] = {
    "get_channels": (
        ChannelListArgs,
        "实时读取绑定平台全部渠道汇总和分页明细；超过一页可连续翻页，不触发探测",
    ),
    "get_channel": (ChannelArgs, "查询当前平台单渠道详情，不触发探测"),
    "search_users": (UserArgs, "查询当前平台指定用户额度和用量，不返回凭证"),
    "get_statistics": (StatsArgs, "当前平台今天/昨天/近7天/近30天统计排行，可附图，最多31天"),
    "get_errors": (ErrorArgs, "当前平台指定日期范围的错误日志摘要，不返回原始敏感正文"),
    "get_alerts": (ChannelListArgs, "当前平台最近告警和确认状态"),
    "get_capabilities": (NoArgs, "说明本机器人可查询和统计的能力"),
}


class Tools:
    def __init__(self, commands: Commands) -> None:
        self.commands = commands
        self.image: bytes | None = None
        self.used: list[str] = []
        self.successful: list[str] = []
        self.failures: list[str] = []

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in SCHEMAS.items()
        ]

    async def call(self, name: str, arguments: str) -> dict[str, Any]:
        if name not in SCHEMAS or len(arguments) > 4000:
            return {"ok": False, "error": "该工具不允许，不能修改渠道或切换平台"}
        try:
            parsed = SCHEMAS[name][0].model_validate_json(arguments)
            self.used.append(name)
            result = await self._dispatch(name, parsed.model_dump())
            if result.get("ok") is True:
                self.successful.append(name)
            safe: dict[str, Any] = redact_data(result, self.commands.settings)
            return safe
        except UpstreamError as error:
            self.failures.append(str(error))
            return {
                "ok": False,
                "error": str(error),
                "source": "newapi_admin_api",
                "complete": False,
            }
        except (ValidationError, ValueError):
            return {"ok": False, "error": "参数无效；只允许当前绑定平台，不接受平台ID"}

    async def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in {"get_channels", "get_channel"}:
            result = await self.commands.data.channels()
            items = result["items"]
            if name == "get_channel":
                items = [item for item in items if item["id"] == args["channel_id"]]
                return {**result, "ok": bool(items), "items": items}
            page, size = args["page"], args["page_size"]
            return {
                **result,
                "ok": True,
                "items": items[(page - 1) * size : page * size],
                "page": page,
                "page_size": size,
                "has_more": page * size < result["total"],
                "limitation": "汇总覆盖全部渠道；明细分页，has_more为真时可继续翻页。手动禁用不等于故障",
            }
        if name == "search_users":
            users = await self.commands.data.users(args["keyword"])
            return {
                "ok": True,
                "items": [
                    user.model_dump()
                    for user in users[
                        (args["page"] - 1) * args["page_size"] : args["page"] * args["page_size"]
                    ]
                ],
                "total": len(users),
                "page": args["page"],
                "page_size": args["page_size"],
                "has_more": args["page"] * args["page_size"] < len(users),
                "source": "newapi_user_search",
                "collected_at": time.time(),
                "unit": "quota原始单位，非货币",
            }
        if name == "get_statistics":
            today = datetime.now(ZoneInfo(self.commands.settings.timezone)).date()
            period = parse_period(args["period"], today=today)
            query = StatisticsQuery.model_validate(
                {
                    **period.model_dump(),
                    "dimension": args["dimension"],
                    "metric": args["metric"],
                    "limit": 10,
                }
            )
            result = await self.commands.data.statistics(query)
            snapshot = Snapshot.model_validate(result)
            if args["chart"]:
                self.image = await self.commands.reports._image(
                    snapshot, "ranking" if args["dimension"] != "model" else "trend"
                )
            return {"ok": True, **result, "window": query.model_dump(mode="json")}
        if name == "get_errors":
            today = datetime.now(ZoneInfo(self.commands.settings.timezone)).date()
            period = parse_period(args["period"], today=today)
            start, end = period.window(self.commands.settings.timezone)
            result = await self.commands.data.logs(
                start,
                end,
                page=args["page"],
                page_size=args["page_size"],
                log_type=5,
                channel_id=args["channel_id"],
            )
            return {
                "ok": True,
                **result,
                "has_more": args["page"] * args["page_size"] < result["total"],
            }
        if name == "get_alerts":
            return {
                "ok": True,
                "source": "local_alert_state",
                "data": await self.commands.operations.alerts(
                    page=args["page"], page_size=args["page_size"]
                ),
            }
        return {
            "ok": True,
            "capabilities": [
                "查看当前渠道与异常状态",
                "查询用户额度和用量",
                "按日期查看错误记录",
                "分析用量与排行并生成图表",
                "整理日报和查询告警",
            ],
            "limits": "只读工具只服务本机器人绑定平台，不能执行启停渠道、SQL、文件或外部URL",
        }
