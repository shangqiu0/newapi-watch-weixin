"""Bounded, ephemeral query-time aggregates. Business data remains in NewAPI."""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .analytics import Coverage, Metrics, Ranking, SeriesPoint, Snapshot
from .models import LogRecord
from .periods import StatisticsQuery


def metrics(records: list[LogRecord]) -> Metrics:
    successes = [r for r in records if r.type == 2]
    return Metrics(
        success_records=len(successes),
        error_records=sum(r.type == 5 for r in records),
        quota=sum(r.quota for r in successes),
        tokens=sum(r.prompt_tokens + r.completion_tokens for r in successes),
        active_users=len({r.user_id for r in successes if r.user_id > 0}),
        active_channels=len({r.channel_id for r in successes if r.channel_id > 0}),
    )


def ranking(records: list[LogRecord], dimension: str, metric: str, limit: int) -> list[Ranking]:
    groups: dict[str, list[LogRecord]] = defaultdict(list)
    for record in records:
        key = str(
            getattr(
                record,
                {"model": "model_name", "channel": "channel_id", "user": "user_id"}[dimension],
            )
        )
        groups[key].append(record)
    result = [
        Ranking(
            key=key,
            label=(key or "未知模型")
            if dimension == "model"
            else ("渠道 #" if dimension == "channel" else "用户 #") + key,
            **metrics(group).model_dump(),
        )
        for key, group in groups.items()
    ]
    field = {
        "quota": "quota",
        "calls": "success_records",
        "errors": "error_records",
        "tokens": "tokens",
    }[metric]
    return sorted(result, key=lambda r: (-getattr(r, field), r.key))[:limit]


def snapshot(
    records: list[LogRecord], query: StatisticsQuery, timezone: str, through: int
) -> Snapshot:
    start, end = query.window(timezone)
    zone = ZoneInfo(timezone)
    records = [
        r
        for r in records
        if start <= r.created_at < end
        and r.type in (2, 5)
        and (not query.channel_id or r.channel_id == query.channel_id)
    ]
    slots = []
    if query.days == 1:
        slots = [
            (
                datetime.fromtimestamp(lower, zone).isoformat(timespec="minutes"),
                lower,
                min(lower + 3600, end),
            )
            for lower in range(start, end, 3600)
        ]
    else:
        for offset in range(query.days):
            day = query.start + timedelta(days=offset)
            lower, upper = StatisticsQuery(start=day, end=day).window(timezone)
            slots.append((str(day), lower, upper))
    return Snapshot(
        query=query,
        timezone=timezone,
        summary=metrics(records),
        ranking=ranking(records, query.dimension, query.metric, query.limit),
        channel_leaders=ranking(records, "channel", "quota", 3),
        series=[
            SeriesPoint(
                label=label,
                **metrics([r for r in records if lower <= r.created_at < upper]).model_dump(),
            )
            for label, lower, upper in slots
        ],
        coverage=Coverage(complete=True, from_timestamp=start, through_timestamp=through),
    )
