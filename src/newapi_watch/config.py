"""Validated environment configuration; no per-user permission settings."""

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .money import CurrencyConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WATCH_", env_file=".env", extra="ignore")

    demo: bool = False
    currency: CurrencyConfig = Field(default_factory=CurrencyConfig)
    database: Path = Path("data/watch.sqlite3")
    timezone: str = "Asia/Shanghai"
    api_key: SecretStr = SecretStr("")
    newapi_url: str = ""
    newapi_token: SecretStr = SecretStr("")
    newapi_user_id: int = Field(default=0, ge=0)
    request_timeout: float = Field(default=15, gt=0, le=120)
    request_attempts: int = Field(default=3, ge=1, le=5)
    retry_seconds: float = Field(default=0.5, ge=0, le=10)
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=1000, ge=1, le=10000)
    poll_seconds: float = Field(default=30, ge=1)
    log_seconds: float = Field(default=120, ge=1)
    log_initial_hours: int = Field(default=24, ge=1, le=168)
    log_overlap_seconds: int = Field(default=120, ge=1)
    retention_days: int = Field(default=30, ge=2, le=365)
    probe_enabled: bool = False
    probe_seconds: float = Field(default=600, ge=30)
    probe_min_interval: float = Field(default=30, ge=1, le=3600)
    probe_failures: int = Field(default=3, ge=1, le=20)
    probe_concurrency: int = Field(default=2, ge=1, le=10)
    low_balance_threshold: float | None = Field(default=None, ge=0)
    error_ratio_threshold: float = Field(default=0.3, gt=0, le=1)
    error_min_records: int = Field(default=10, ge=1)
    group_webhook: SecretStr = SecretStr("")
    notification_max_attempts: int = Field(default=8, ge=1, le=20)
    notification_retry_seconds: float = Field(default=5, ge=1, le=300)
    notification_interval: float = Field(default=3.4, ge=3.1)
    webhook_enabled: bool = False
    webhook_secret: SecretStr = SecretStr("")
    webhook_max_age: int = Field(default=300, ge=30, le=3600)
    bot_id: str = ""
    bot_secret: SecretStr = SecretStr("")
    bot_concurrency: int = Field(default=4, ge=1, le=20)
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = Field(default="", max_length=128)
    platform_name: str = Field(default="NewAPI平台", min_length=1, max_length=80)

    command_timeout: float = Field(default=60, ge=5, le=180)
    report_hour: int = Field(default=9, ge=0, le=23)
    report_enabled: bool = True
    chart_font: Path | None = None
    plugins: tuple[str, ...] = ()

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("无效时区") from exc
        return value

    @field_validator("newapi_url", "llm_base_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        if not value:
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("NewAPI URL 必须是 HTTP(S) 地址")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("NewAPI URL 不得包含凭证、查询或片段")
        return value.rstrip("/")

    @field_validator("group_webhook")
    @classmethod
    def check_webhook_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            return value
        parts = urlsplit(value.get_secret_value())
        if (
            parts.scheme != "https"
            or parts.hostname != "qyapi.weixin.qq.com"
            or parts.path != "/cgi-bin/webhook/send"
            or parts.username
            or parts.port
        ):
            raise ValueError("群通知必须使用官方企微 webhook/send HTTPS 地址")
        return value

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        if self.llm_enabled and not (
            self.llm_base_url and self.llm_model and self.llm_api_key.get_secret_value()
        ):
            raise ValueError(
                "启用模型需要独立的兼容接口地址、模型ID和模型调用Key；管理令牌不是模型Key"
            )
        if not self.demo and not (
            self.newapi_url
            and self.newapi_token.get_secret_value()
            and self.newapi_user_id > 0
            and len(self.api_key.get_secret_value()) >= 24
        ):
            raise ValueError("生产模式需要 NewAPI URL、管理访问令牌、用户ID与至少24位服务密钥")
        if bool(self.bot_id) != bool(self.bot_secret.get_secret_value()):
            raise ValueError("bot_id 与 bot_secret 必须同时设置")
        if self.webhook_enabled and len(self.webhook_secret.get_secret_value()) < 16:
            raise ValueError("启用事件接收需要至少16位 webhook_secret")
        return self
