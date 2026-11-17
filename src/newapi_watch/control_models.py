"""Validated control-plane inputs. Secret values are never public DTO fields."""

from typing import Literal, Self, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .money import CurrencyConfig
from .monitoring.models import EventKind, MonitoringConfig
from .retention import RetentionConfig
from .upstream_accounts import UpstreamAccount


def endpoint(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("使用不含凭证和查询参数的HTTP(S)地址")
    return value.rstrip("/")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(Input):
    enabled: bool = False
    compatibility: Literal["auto", "openai", "deepseek"] = "auto"
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    model: str = Field(default="", max_length=128)

    @field_validator("base_url")
    @classmethod
    def url(cls, value: str) -> str:
        return endpoint(value) if value else value


class PlatformInput(Input):
    name: str = Field(min_length=1, max_length=80)
    newapi_url: str
    newapi_token: SecretStr
    newapi_user_id: int = Field(gt=0)
    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    poll_seconds: float = Field(default=30, ge=10, le=3600)
    log_seconds: float = Field(default=120, ge=30, le=7200)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    currency: CurrencyConfig = Field(default_factory=CurrencyConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    upstream_accounts: list[UpstreamAccount] = Field(default_factory=list, max_length=30)

    @field_validator("newapi_url")
    @classmethod
    def url(cls, value: str) -> str:
        return endpoint(value)

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("时区不存在") from None
        return value

    @model_validator(mode="after")
    def required_secrets(self) -> Self:
        if not self.newapi_token.get_secret_value().strip():
            raise ValueError("需要NewAPI管理令牌")
        if self.llm.enabled and not (
            self.llm.base_url and self.llm.model and self.llm.api_key.get_secret_value()
        ):
            raise ValueError("启用AI需要完整模型配置")
        return self


class PlatformPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    newapi_url: str | None = None
    newapi_token: SecretStr | None = None
    newapi_user_id: int | None = Field(default=None, gt=0)
    enabled: bool | None = None
    timezone: str | None = None
    poll_seconds: float | None = Field(default=None, ge=10, le=3600)
    log_seconds: float | None = Field(default=None, ge=30, le=7200)
    llm: LLMConfig | None = None
    monitoring: MonitoringConfig | None = None
    currency: CurrencyConfig | None = None
    retention: RetentionConfig | None = None
    upstream_accounts: list[UpstreamAccount] | None = Field(default=None, max_length=30)


class BindingInput(Input):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["bot", "webhook"]
    enabled: bool = True
    bot_id: str = Field(default="", max_length=256)
    bot_secret: SecretStr = SecretStr("")
    webhook_url: SecretStr = SecretStr("")
    events: list[EventKind] = Field(
        default_factory=lambda: cast(
            list[EventKind], ["channel", "request_error", "balance", "daily", "weekly", "service"]
        )
    )
    notification_chat_id: str = Field(default="", max_length=256, pattern=r"^[^\s]*$")
    message_format: Literal["text", "markdown"] = "markdown"
    mention_all: bool = False

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.kind == "bot" and not (self.bot_id.strip() and self.bot_secret.get_secret_value()):
            raise ValueError("机器人需要ID和secret")
        if self.kind == "webhook":
            parts = urlsplit(self.webhook_url.get_secret_value())
            if (
                parts.scheme != "https"
                or parts.hostname != "qyapi.weixin.qq.com"
                or parts.path != "/cgi-bin/webhook/send"
                or not parts.query
                or parts.username
                or parts.port
            ):
                raise ValueError("使用企微官方群Webhook地址")
        return self


class BindingPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    bot_id: str | None = Field(default=None, max_length=256)
    bot_secret: SecretStr | None = None
    webhook_url: SecretStr | None = None
    events: list[EventKind] | None = None
    notification_chat_id: str | None = Field(default=None, max_length=256, pattern=r"^[^\s]*$")
    message_format: Literal["text", "markdown"] | None = None
    mention_all: bool | None = None
