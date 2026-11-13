"""Whitelisted SQL aggregates: no raw log data, arbitrary SQL or timezone shortcuts."""

import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .periods import StatisticsQuery
from .persistence import row, rows
from .store import Store

METRICS_SQL = """
 COALESCE(SUM(CASE WHEN l.type=2 THEN 1 ELSE 0 END),0) AS success_records,
 COALESCE(SUM(CASE WHEN l.type=5 THEN 1 ELSE 0 END),0) AS error_records,
 COALESCE(SUM(CASE WHEN l.type=2 THEN l.quota ELSE 0 END),0) AS quota,
 COALESCE(SUM(CASE WHEN l.type=2 THEN l.prompt_tokens+l.completion_tokens ELSE 0 END),0) AS tokens
"""
GROUPS = {"model": "l.model_name", "channel": "l.channel_id", "user": "l.user_id"}
ORDER = {
    "quota": "quota",
    "calls": "success_records",
    "errors": "error_records",
    "tokens": "tokens",
}


class Metrics(BaseModel):
    success_records: int = 0
    error_records: int = 0
    quota: int = 0
    tokens: int = 0
    active_users: int = 0
    active_channels: int = 0


class Ranking(Metrics):
    key: str
    label: str


class SeriesPoint(Metrics):
    label: str


class Coverage(BaseModel):
    complete: bool = False
    from_timestamp: int | None = None
    through_timestamp: int | None = None


class Snapshot(BaseModel):
    query: StatisticsQuery
    timezone: str
    summary: Metrics
    ranking: list[Ranking]
    channel_leaders: list[Ranking]
    series: list[SeriesPoint]
    coverage: Coverage


class Analytics:
    def __init__(self, store: Store, timezone: str) -> None:
        self.store, self.timezone = store, timezone

    async def snapshot(self, query: StatisticsQuery, *, now: float | None = None) -> Snapshot:
        now = time.time() if now is None else now
        query.reject_future(datetime.fromtimestamp(now, ZoneInfo(self.timezone)).date())
        start, end = query.window(self.timezone)
        params = (start, end, query.channel_id, query.channel_id)
        where = "l.created_at>=? AND l.created_at<? AND (?=0 OR l.channel_id=?) AND l.type IN (2,5)"
        async with self.store.transaction() as db:
            summary = await row(
                db,
                "SELECT "
                + METRICS_SQL
                + """,
                COUNT(DISTINCT CASE WHEN l.type=2 AND l.user_id>0 THEN l.user_id END) AS active_users,
                COUNT(DISTINCT CASE WHEN l.type=2 AND l.channel_id>0 THEN l.channel_id END) AS active_channels
                FROM logs l WHERE """
                + where,
                params,
            )
            ranking = await rows(
                db,
                "SELECT CAST("
                + GROUPS[query.dimension]
                + " AS TEXT) AS key,"
                + METRICS_SQL
                + " FROM logs l WHERE "
                + where
                + " GROUP BY "
                + GROUPS[query.dimension]
                + " ORDER BY "
                + ORDER[query.metric]
                + " DESC,key ASC LIMIT ?",
                (*params, query.limit),
            )
            channel_leaders = await rows(
                db,
                "SELECT CAST(l.channel_id AS TEXT) AS key,"
                + METRICS_SQL
                + " FROM logs l WHERE "
                + where
                + " GROUP BY l.channel_id ORDER BY quota DESC,key ASC LIMIT 3",
                params,
            )
            series = await self._series(db, query)
            metadata = await rows(
                db, "SELECT id,value FROM metadata WHERE id IN ('log_coverage_start','log_cursor')"
            )
        bounds = {item["id"]: int(float(item["value"])) for item in metadata}
        first, last = bounds.get("log_coverage_start"), bounds.get("log_cursor")
        return Snapshot(
            query=query,
            timezone=self.timezone,
            summary=Metrics.model_validate(summary or {}),
            ranking=[
                Ranking.model_validate({**item, "label": self._label(query.dimension, item["key"])})
                for item in ranking
            ],
            channel_leaders=[
                Ranking.model_validate({**item, "label": self._label("channel", item["key"])})
                for item in channel_leaders
            ],
            series=[SeriesPoint.model_validate(item) for item in series],
            coverage=Coverage(
                complete=first is not None and first <= start and last is not None and last >= end,
                from_timestamp=first,
                through_timestamp=last,
            ),
        )

    @staticmethod
    def _label(dimension: str, key: str) -> str:
        if dimension == "model":
            return key or "(unknown model)"
        return ("渠道 #" if dimension == "channel" else "用户 #") + key

    async def _series(self, db: Any, query: StatisticsQuery) -> list[dict[str, Any]]:
        slots = self._slots(query)
        placeholders = ",".join("(?,?,?,?)" for _ in slots)
        params: list[str | int] = []
        for index, (label, lower, upper) in enumerate(slots):
            params.extend((index, label, lower, upper))
        params.extend((query.channel_id, query.channel_id))
        return await rows(
            db,
            "WITH slots(idx,label,lower_at,upper_at) AS (VALUES "
            + placeholders
            + ") SELECT slots.label,"
            + METRICS_SQL
            + """ FROM slots LEFT JOIN logs l
                          ON l.created_at>=slots.lower_at AND l.created_at<slots.upper_at
                          AND l.type IN (2,5) AND (?=0 OR l.channel_id=?)
                          GROUP BY slots.idx,slots.label ORDER BY slots.idx""",
            params,
        )

    def _slots(self, query: StatisticsQuery) -> list[tuple[str, int, int]]:
        start, end = query.window(self.timezone)
        zone = ZoneInfo(self.timezone)
        if query.days == 1:
            return [
                (
                    datetime.fromtimestamp(lower, zone).isoformat(timespec="minutes"),
                    lower,
                    min(lower + 3600, end),
                )
                for lower in range(start, end, 3600)
            ]
        result = []
        for offset in range(query.days):
            day = query.start + timedelta(days=offset)
            lower, upper = StatisticsQuery(start=day, end=day).window(self.timezone)
            result.append((str(day), lower, upper))
        return result
