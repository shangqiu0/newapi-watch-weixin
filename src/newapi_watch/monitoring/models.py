"""Validated configuration; templates are substitution, never executable code."""

import re
from string import Formatter
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ERROR_TITLE = "渠道异常告警"
LEGACY_ERROR_TEMPLATE = """渠道异常告警
错误日志时间：{log_time}
日志 ID：{log_id}
上游请求 ID：{upstream_request_id}
渠道：{channel_name}（#{channel_id}）
状态码：{status_code}
模型：{model}
错误信息：{error_message}
上轮冷却窗口内同类错误累计：{count} 次"""
ERROR_TEMPLATE = f"""{ERROR_TITLE}
状态码：{{status_code}}
渠道：{{channel_name}}（#{{channel_id}}）
模型：{{model}}
错误信息：{{error_message}}
上游请求 ID：{{upstream_request_id}}
平台请求 ID：{{request_id}}
错误日志时间：{{log_time}}
上轮冷却窗口内同类错误累计：{{count}} 次"""
ERROR_FIELD_ORDER = tuple(line.partition("：")[0] for line in ERROR_TEMPLATE.splitlines()[1:])


def order_standard_error_notice(text: str) -> str:
    """Normalize recognized standard alerts without rewriting custom layouts."""
    title, separator, body = text.partition("\n")
    if not separator or title != ERROR_TITLE:
        return text
    fields: dict[str, str] = {}
    for line in body.splitlines():
        label, colon, _ = line.partition("：")
        if colon and label == "日志 ID":
            continue
        if not colon or label not in ERROR_FIELD_ORDER or label in fields:
            return text
        fields[label] = line
    if len(fields) != len(ERROR_FIELD_ORDER):
        return text
    return "\n".join([title, *(fields[label] for label in ERROR_FIELD_ORDER)])


PLACEHOLDERS = {
    "channel_name",
    "channel_id",
    "status_code",
    "error_code",
    "model",
    "error_message",
    "log_time",
    "log_id",
    "request_id",
    "upstream_request_id",
    "count",
    "time",
}
EventKind = Literal["channel", "request_error", "balance", "daily", "weekly", "service"]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Policy(Config):
    binding_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=100
    )
    confirmations: int = Field(default=1, ge=1, le=20)
    cooldown_seconds: int = Field(default=300, ge=30, le=86400)
    notify_recovery: bool = True
    mention_all: bool = False


class ChannelRule(Policy):
    enabled: bool = True
    poll_seconds: int = Field(default=60, ge=10, le=86400)
    channel_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=500)
    notify_missing: bool = True


class SourceRule(Policy):
    repeat_notifications: bool = False
    enabled: bool = True
    confirmations: int = Field(default=3, ge=1, le=20)


class ErrorRule(Policy):
    enabled: bool = False
    poll_seconds: int = Field(default=120, ge=30, le=86400)
    initial_lookback_seconds: int = Field(default=0, ge=0, le=3600)
    window_seconds: int = Field(default=300, ge=30, le=3600)
    overlap_seconds: int = Field(default=30, ge=0, le=300)
    status_codes: list[int] = Field(
        default_factory=lambda: [400, 500], min_length=1, max_length=200
    )
    channel_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=500)
    models: list[str] = Field(default_factory=list, max_length=100)
    error_codes: list[str] = Field(default_factory=list, max_length=100)
    cooldown_seconds: int = Field(default=300, ge=30, le=86400)
    mention_all: bool = False
    template: str = Field(default=ERROR_TEMPLATE, min_length=1, max_length=2500)

    @field_validator("status_codes")
    @classmethod
    def statuses(cls, value: list[int]) -> list[int]:
        if any(code < 400 or code > 599 for code in value):
            raise ValueError("仅允许400..599错误状态码")
        return sorted(set(value))

    @field_validator("template")
    @classmethod
    def template_fields(cls, value: str) -> str:
        if "\n".join(value.strip().splitlines()) == LEGACY_ERROR_TEMPLATE:
            return ERROR_TEMPLATE
        for _, name, spec, conversion in Formatter().parse(value):
            if name is not None and (name not in PLACEHOLDERS or spec or conversion):
                raise ValueError("模板包含不支持的占位符/格式")
        lines = []
        for line in value.splitlines():
            names = {name for _, name, _, _ in Formatter().parse(line)}
            if names & {"error_code", "time", "log_id"} or re.match(
                r"^\s*(?:[-*]\s*)?(?:错误码|告警时间|时间|日志\s*ID)\s*[:：]", line, re.IGNORECASE
            ):
                continue
            if "log_time" in names:
                line = line.replace("（北京时间）", "").replace("(北京时间)", "")
            lines.append(line)
        return order_standard_error_notice("\n".join(lines).strip()) or ERROR_TEMPLATE


class BalanceRule(Policy):
    poll_seconds: int | None = Field(default=None, ge=30, le=86400)
    recovery_threshold: int | None = Field(default=None, ge=0, le=10**18)
    user_id: int = Field(gt=0)
    name: str = Field(default="", max_length=80)
    threshold: int = Field(ge=0, le=10**18)
    enabled: bool = False
    notify_recovery: bool = True
    mention_all: bool = False


class ReportSchedule(Config):
    pages_per_run: int = Field(default=5, ge=1, le=10)
    max_failures: int = Field(default=3, ge=1, le=10)
    retry_seconds: int = Field(default=900, ge=30, le=86400)
    binding_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=100
    )
    enabled: bool = False
    hour: int = Field(default=9, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    weekday: int = Field(default=0, ge=0, le=6)
    with_image: bool = True


class RequestPolicy(Config):
    min_interval_seconds: float = Field(default=1.2, ge=0.1, le=60)
    rate_limit_cooldown_seconds: int = Field(default=60, ge=5, le=3600)


class MonitoringConfig(Config):
    requests: RequestPolicy = Field(default_factory=RequestPolicy)
    channels: ChannelRule = Field(default_factory=ChannelRule)
    source: SourceRule = Field(default_factory=SourceRule)
    errors: ErrorRule = Field(default_factory=ErrorRule)
    balances: list[BalanceRule] = Field(default_factory=list, max_length=100)
    balance_poll_seconds: int = Field(default=300, ge=30, le=86400)
    daily: ReportSchedule = Field(default_factory=ReportSchedule)
    weekly: ReportSchedule = Field(default_factory=ReportSchedule)

    def routes(self) -> list[tuple[EventKind, Policy | ReportSchedule]]:
        return [
            ("channel", self.channels),
            ("service", self.source),
            ("request_error", self.errors),
            *[("balance", r) for r in self.balances],
            ("daily", self.daily),
            ("weekly", self.weekly),
        ]

    @model_validator(mode="after")
    def unique_users(self) -> Self:
        for rule in self.balances:
            if rule.recovery_threshold is not None and rule.recovery_threshold < rule.threshold:
                raise ValueError("恢复阈值不能低于告警阈值")
        ids = [r.user_id for r in self.balances]
        if len(ids) != len(set(ids)):
            raise ValueError("同一平台每个用户只能配置一条余额监控")
        return self
