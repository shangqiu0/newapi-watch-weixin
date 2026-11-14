"""Bounded, mergeable query metrics. No names, prompts or error payloads persist."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..analytics import Coverage, Metrics, Ranking, SeriesPoint, Snapshot
from ..memory_analytics import snapshot as empty_snapshot
from ..models import LogRecord
from ..periods import StatisticsQuery

FIELDS = ("quota", "success_records", "error_records", "tokens")


class QueryTotals(BaseModel):
    summary: Metrics = Field(default_factory=Metrics)
    models: dict[str, Metrics] = Field(default_factory=dict)
    channels: dict[str, Metrics] = Field(default_factory=dict)
    users: dict[str, Metrics] = Field(default_factory=dict)
    series: dict[str, Metrics] = Field(default_factory=dict)
    active_users: set[int] = Field(default_factory=set)
    active_channels: set[int] = Field(default_factory=set)

    def add(self, records: list[LogRecord], query: StatisticsQuery, timezone: str) -> None:
        for record in records:
            if record.type not in (2, 5):
                continue
            metric = Metrics(
                success_records=int(record.type == 2),
                error_records=int(record.type == 5),
                quota=record.quota if record.type == 2 else 0,
                tokens=record.prompt_tokens + record.completion_tokens if record.type == 2 else 0,
            )
            stamp = datetime.fromtimestamp(record.created_at, ZoneInfo(timezone))
            label = (
                stamp.replace(minute=0, second=0, microsecond=0).isoformat(timespec="minutes")
                if query.days == 1
                else str(stamp.date())
            )
            for target in (
                self.summary,
                self.models.setdefault(record.model_name, Metrics()),
                self.channels.setdefault(str(record.channel_id), Metrics()),
                self.users.setdefault(str(record.user_id), Metrics()),
                self.series.setdefault(label, Metrics()),
            ):
                for field in FIELDS:
                    setattr(target, field, getattr(target, field) + getattr(metric, field))
            if record.type == 2:
                if record.user_id > 0:
                    self.active_users.add(record.user_id)
                if record.channel_id > 0:
                    self.active_channels.add(record.channel_id)
            if (
                max(len(self.models), len(self.channels), len(self.users)) > 10000
                or len(self.active_users) > 100000
            ):
                raise ValueError("聚合对象超过安全上限，未发布部分结果")

    def merge(self, other: "QueryTotals") -> None:
        for name in FIELDS:
            setattr(self.summary, name, getattr(self.summary, name) + getattr(other.summary, name))
        for key in ("models", "channels", "users", "series"):
            target = getattr(self, key)
            for name, metric in getattr(other, key).items():
                current = target.setdefault(name, Metrics())
                for field in FIELDS:
                    setattr(current, field, getattr(current, field) + getattr(metric, field))
        self.active_users.update(other.active_users)
        self.active_channels.update(other.active_channels)
        if max(len(self.models), len(self.channels), len(self.users)) > 10000:
            raise ValueError("汇总对象超过安全上限")

    def snapshot(self, query: StatisticsQuery, timezone: str, through: int) -> dict[str, Any]:
        def leaders(group: dict[str, Metrics], dimension: str, limit: int) -> list[Ranking]:
            field = {
                "quota": "quota",
                "calls": "success_records",
                "errors": "error_records",
                "tokens": "tokens",
            }[query.metric]
            return [
                Ranking(
                    key=k,
                    label=(k or "未知模型")
                    if dimension == "model"
                    else ("用户 #" if dimension == "user" else "渠道 #") + k,
                    **m.model_dump(),
                )
                for k, m in sorted(group.items(), key=lambda p: (-getattr(p[1], field), p[0]))[
                    :limit
                ]
            ]

        template = empty_snapshot([], query, timezone, through)
        summary = self.summary.model_copy(
            update={
                "active_users": len(self.active_users),
                "active_channels": len(self.active_channels),
            }
        )
        groups = {"model": self.models, "channel": self.channels, "user": self.users}
        result = Snapshot(
            query=query,
            timezone=timezone,
            summary=summary,
            ranking=leaders(groups[query.dimension], query.dimension, query.limit),
            channel_leaders=leaders(self.channels, "channel", 3),
            series=[
                SeriesPoint(label=p.label, **self.series.get(p.label, Metrics()).model_dump())
                for p in template.series
                if (
                    datetime.fromisoformat(p.label).replace(tzinfo=ZoneInfo(timezone)).timestamp()
                    if "T" not in p.label
                    else datetime.fromisoformat(p.label).timestamp()
                )
                < through
            ],
            coverage=Coverage(
                complete=True, from_timestamp=query.window(timezone)[0], through_timestamp=through
            ),
        )
        return result.model_dump(mode="json")
