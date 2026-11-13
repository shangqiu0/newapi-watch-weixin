"""Bounded, inclusive calendar periods shared by chat and HTTP."""

import re
from datetime import date, datetime, time, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Period(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if not 1 <= self.days <= 31:
            raise ValueError("日期范围应为1至31个自然日，且开始不能晚于结束")
        return self

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def window(self, timezone: str) -> tuple[int, int]:
        zone = ZoneInfo(timezone)
        start = datetime.combine(self.start, time.min, tzinfo=zone)
        end = datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=zone)
        return int(start.timestamp()), int(end.timestamp())

    def reject_future(self, today: date) -> None:
        if self.end > today:
            raise ValueError("不能查询未来日期")


class StatisticsQuery(Period):
    dimension: Literal["channel", "model", "user"] = "model"
    metric: Literal["quota", "calls", "errors", "tokens"] = "quota"
    limit: int = Field(default=10, ge=1, le=20)
    channel_id: int = Field(default=0, ge=0)


def parse_period(value: str, *, today: date) -> Period:
    value = value.strip()
    if value in {"", "今天", "今日", "今日消耗"}:
        return Period(start=today, end=today)
    if value == "昨天":
        yesterday = today - timedelta(days=1)
        return Period(start=yesterday, end=yesterday)
    if match := re.fullmatch(r"近(\d{1,2})天", value):
        days = int(match[1])
        return Period(start=today - timedelta(days=days - 1), end=today)
    parts = value.split("~")
    if len(parts) not in {1, 2}:
        raise ValueError("日期格式无效")
    start = date.fromisoformat(parts[0])
    end = date.fromisoformat(parts[-1])
    period = Period(start=start, end=end)
    period.reject_future(today)
    return period
