"""Whitelisted boundary DTOs; upstream credentials never enter persistence."""

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class Channel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)
    name: str = Field(default="unnamed", max_length=512)
    status: Literal[0, 1, 2, 3]
    type: int = 0
    balance: float | None = None
    response_time: float = 0
    models: str = ""
    group: str = ""
    priority: int | None = None
    weight: int | None = None
    used_quota: int = 0
    test_time: int = 0
    balance_updated_time: int = 0


class LogRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    id: int = Field(gt=0)
    created_at: int = Field(ge=0)
    type: int = Field(ge=0)
    channel_id: int = Field(default=0, validation_alias=AliasChoices("channel", "channel_id"))
    user_id: int = 0
    username: str = Field(default="", max_length=512)
    model_name: str = Field(default="", max_length=512)
    quota: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    use_time: float = Field(default=0, ge=0)
    http_status: int = Field(default=0, ge=0, le=599)
    error_code: str = Field(default="", max_length=256)
    error_message: str = Field(default="", max_length=2000)
    channel_name: str = Field(default="", max_length=512)
    request_id: str = Field(default="", max_length=128)
    upstream_request_id: str = Field(default="", max_length=128)


class LogPage(BaseModel):
    items: list[LogRecord]
    total: int | None = None
    page_size: int | None = None


class UserInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    username: str = ""
    display_name: str = ""
    quota: int = 0
    used_quota: int = 0
    request_count: int = 0
    status: int = 0
    group: str = ""


class UsageCounters(BaseModel):
    model_config = ConfigDict(extra="ignore")
    quota: int = Field(strict=True, ge=0)
    rpm: int | None = Field(default=None, strict=True, ge=0)
    tpm: int | None = Field(default=None, strict=True, ge=0)


class ProbeResult(BaseModel):
    success: bool
    time: float = 0


class CommandResult(BaseModel):
    text: str
    ok: bool = True
    image: bytes | None = Field(default=None, exclude=True)


class NewAPIEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    type: str = Field(validation_alias=AliasChoices("type", "Type"), max_length=256)
    title: str = Field(default="", validation_alias=AliasChoices("title", "Title"), max_length=2048)
    timestamp: int = Field(validation_alias=AliasChoices("timestamp", "Timestamp"), gt=0)
