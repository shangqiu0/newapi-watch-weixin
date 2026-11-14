"""Adapt documented hourly aggregates without inventing unavailable counters."""

import time
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..analytics import Coverage, Metrics, Ranking, SeriesPoint, Snapshot
from ..client import NewAPIClient, UpstreamError
from ..periods import StatisticsQuery


class AggregateUnavailable(UpstreamError):
    """Missing aggregate capability is distinct from permission/rate failures."""


class AggregateRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model_name: str = Field(default="", max_length=512)
    username: str = Field(default="", max_length=512)
    channel_id: int = Field(default=0, ge=0, strict=True)
    created_at: int = Field(default=0, ge=0, strict=True)
    quota: int = Field(ge=0, strict=True)
    count: int = Field(ge=0, strict=True)
    token_used: int = Field(ge=0, strict=True)


class AggregateReader:
    def __init__(self, client: NewAPIClient) -> None:
        self.client = client

    async def snapshot(self, query: StatisticsQuery) -> dict[str, Any]:
        start, end = query.window(self.client.settings.timezone)
        if query.metric == "errors" or query.channel_id:
            raise AggregateUnavailable("此条件需要明细统计")
        path = {"model": "/api/data/", "user": "/api/data/users", "channel": "/api/data/flow"}[
            query.dimension
        ]
        try:
            payload = await self.client.request(
                path, {"start_timestamp": start, "end_timestamp": end - 1}
            )
        except UpstreamError as exc:
            if str(exc) in {"NewAPI HTTP 404", "NewAPI HTTP 405"}:
                raise AggregateUnavailable("当前平台版本未提供汇总接口") from None
            raise
        data = payload.get("data")
        if not isinstance(data, list) or not data or len(data) > 100000:
            raise AggregateUnavailable("汇总为空或结构不兼容，无法确认历史覆盖")
        try:
            records = [AggregateRow.model_validate(item) for item in data]
        except ValidationError:
            raise AggregateUnavailable("汇总缺少必需指标，不能补零") from None
        return self._snapshot(records, query, start, end)

    def _snapshot(
        self, records: list[AggregateRow], query: StatisticsQuery, start: int, end: int
    ) -> dict[str, Any]:
        groups: dict[str, Metrics] = defaultdict(Metrics)
        series: dict[str, Metrics] = defaultdict(Metrics)
        summary = Metrics()
        for item in records:
            key = (
                item.model_name
                if query.dimension == "model"
                else item.username
                if query.dimension == "user"
                else str(item.channel_id)
            )
            if not key or (query.dimension == "channel" and item.channel_id <= 0):
                raise AggregateUnavailable("汇总未提供所需维度")
            if query.dimension != "channel" and not start <= item.created_at < end:
                raise AggregateUnavailable("汇总时间字段不兼容")
            targets = [summary, groups[key]]
            if query.dimension != "channel":
                stamp = datetime.fromtimestamp(
                    item.created_at, ZoneInfo(self.client.settings.timezone)
                )
                label = (
                    stamp.isoformat(timespec="minutes") if query.days == 1 else str(stamp.date())
                )
                targets.append(series[label])
            for target in targets:
                target.quota += item.quota
                target.success_records += item.count
                target.tokens += item.token_used
        field = {"quota": "quota", "calls": "success_records", "tokens": "tokens"}[query.metric]
        ranking = [
            Ranking(
                key=k, label=("渠道 #" + k if query.dimension == "channel" else k), **v.model_dump()
            )
            for k, v in sorted(
                groups.items(), key=lambda pair: (-getattr(pair[1], field), pair[0])
            )[: query.limit]
        ]
        result = Snapshot(
            query=query,
            timezone=self.client.settings.timezone,
            summary=summary,
            ranking=ranking,
            channel_leaders=ranking[:3] if query.dimension == "channel" else [],
            series=[SeriesPoint(label=k, **v.model_dump()) for k, v in sorted(series.items())],
            coverage=Coverage(
                complete=False, from_timestamp=start, through_timestamp=min(end, int(time.time()))
            ),
        )
        return {
            **result.model_dump(mode="json"),
            "source": "newapi_hourly_aggregate",
            "unavailable_metrics": ["error_records", "active_users", "active_channels"],
            "queried_at": time.time(),
            "limitation": "来源为上游已落盘的小时汇总，非实时完整日志；历史未启用/未保存的汇总无法补齐。未提供错误数和活跃用户/渠道数；渠道维度无时间轴。空缺时段不自动补零。",
        }
