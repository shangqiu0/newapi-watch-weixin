"""Small resumable report aggregates: no raw log payloads, users or prompts are persisted."""

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..analytics import Coverage, Metrics, Ranking, SeriesPoint, Snapshot
from ..memory_analytics import snapshot as empty_snapshot
from ..models import LogRecord
from ..periods import StatisticsQuery

FIELDS = ("success_records", "error_records", "quota", "tokens")
MAX_GROUPS = 5000


class ReportAggregates(BaseModel):
    summary: Metrics = Field(default_factory=Metrics)
    models: dict[str, Metrics] = Field(default_factory=dict)
    channels: dict[str, Metrics] = Field(default_factory=dict)
    series: dict[str, Metrics] = Field(default_factory=dict)
    users: set[int] = Field(default_factory=set)
    active_channels: set[int] = Field(default_factory=set)

    def add(self, records: list[LogRecord], query: StatisticsQuery, timezone: str) -> None:
        start, end = query.window(timezone)
        for record in records:
            if not start <= record.created_at < end or record.type not in (2, 5):
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
                self.series.setdefault(label, Metrics()),
            ):
                for name in FIELDS:
                    setattr(target, name, getattr(target, name) + getattr(metric, name))
            if record.type == 2:
                if record.user_id > 0:
                    self.users.add(record.user_id)
                if record.channel_id > 0:
                    self.active_channels.add(record.channel_id)
            if (
                len(self.models) > MAX_GROUPS
                or len(self.channels) > MAX_GROUPS
                or len(self.users) > 100000
            ):
                raise ValueError("报告聚合对象超过安全上限，未发布不完整报告")

    def snapshot(self, query: StatisticsQuery, timezone: str) -> Snapshot:
        template = empty_snapshot([], query, timezone, query.window(timezone)[1])
        summary = self.summary.model_copy(
            update={"active_users": len(self.users), "active_channels": len(self.active_channels)}
        )
        return Snapshot(
            query=query,
            timezone=timezone,
            summary=summary,
            ranking=[
                Ranking(key=k, label=k or "未知模型", **v.model_dump())
                for k, v in sorted(self.models.items(), key=lambda item: (-item[1].quota, item[0]))[
                    : query.limit
                ]
            ],
            channel_leaders=[
                Ranking(key=k, label="渠道 #" + k, **v.model_dump())
                for k, v in sorted(
                    self.channels.items(), key=lambda item: (-item[1].quota, item[0])
                )[:3]
            ],
            series=[
                SeriesPoint(
                    label=point.label, **self.series.get(point.label, Metrics()).model_dump()
                )
                for point in template.series
            ],
            coverage=Coverage(
                complete=True,
                from_timestamp=query.window(timezone)[0],
                through_timestamp=query.window(timezone)[1],
            ),
        )
