"""Validated exact-query inputs, independent of the HTTP and model transports."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..periods import StatisticsQuery


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    dimension: Literal["model", "channel", "user"] = "model"
    metric: Literal["quota", "calls", "errors", "tokens"] = "quota"
    limit: int = Field(default=10, ge=1, le=20)
    channel_id: int = Field(default=0, ge=0)
    status_codes: list[int] = Field(default_factory=list, max_length=50)

    @field_validator("status_codes")
    @classmethod
    def statuses(cls, value: list[int]) -> list[int]:
        if any(code < 400 or code > 599 for code in value):
            raise ValueError("只允许HTTP 400..599")
        return sorted(set(value))

    @model_validator(mode="after")
    def bounded_window(self) -> "JobInput":
        if self.dimension not in {"model", "user", "channel"}:
            raise ValueError("dimension invalid")
        if self.metric not in {"quota", "calls", "errors", "tokens"}:
            raise ValueError("metric invalid")
        StatisticsQuery(start=self.start, end=self.end)
        return self

    def query(self) -> StatisticsQuery:
        return StatisticsQuery(
            start=self.start,
            end=self.end,
            dimension=self.dimension,
            metric=self.metric,
            limit=self.limit,
            channel_id=self.channel_id,
        )
