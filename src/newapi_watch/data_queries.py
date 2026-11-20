"""Platform-bound read service: complete upstream reads before publishing statistics."""

import asyncio
import time
from typing import Any

from .client import NewAPIClient, UpstreamError
from .config import Settings
from .memory_analytics import snapshot as memory_snapshot
from .models import LogRecord, UserInfo
from .periods import StatisticsQuery
from .reports import Reports
from .store import Store


class DataQueries:
    def __init__(
        self, settings: Settings, store: Store, client: NewAPIClient, reports: Reports
    ) -> None:
        self.settings, self.store, self.client, self.reports = settings, store, client, reports
        self.lock = asyncio.Lock()
        self.channel_lock = asyncio.Lock()
        self.user_lock = asyncio.Lock()
        self.user_cache: dict[str, tuple[float, list[UserInfo]]] = {}
        self.window_cache: dict[tuple[int, int], tuple[float, int, list[LogRecord]]] = {}
        self.channel_cache: tuple[float, list[dict[str, Any]]] = (0, [])

    async def channels(self, *, live: bool = True, force: bool = False) -> dict[str, Any]:
        if live:
            started = time.time()
            async with self.channel_lock:
                if (force and self.channel_cache[0] < started) or time.time() - self.channel_cache[
                    0
                ] > 15:
                    items = [item.model_dump() for item in await self.client.channels()]
                    self.channel_cache = (time.time(), items)
                queried_at, items = self.channel_cache
        else:
            items = await self.store.channels()
            queried_at = float(await self.store.get_meta("channels_last_ok") or 0)
        return {
            "items": items,
            "total": len(items),
            "complete": bool(live or queried_at),
            "summary": {
                "enabled": sum(item["status"] == 1 for item in items),
                "manual_disabled": sum(item["status"] == 2 for item in items),
                "auto_disabled": sum(item["status"] == 3 for item in items),
                "unknown": sum(item["status"] == 0 for item in items),
            },
            "source": "newapi_admin_api" if live else "channel_snapshot",
            "queried_at": queried_at,
            "collected_at": queried_at,
            "fresh": bool(
                queried_at and time.time() - queried_at <= max(120, self.settings.poll_seconds * 3)
            ),
        }

    async def channel(self, channel_id: int) -> dict[str, Any]:
        item = await self.client.channel(channel_id)
        now = time.time()
        return {
            "items": [item.model_dump()],
            "total": 1,
            "complete": True,
            "source": "newapi_admin_api",
            "queried_at": now,
            "collected_at": now,
            "fresh": True,
        }

    async def window(
        self, start: int, end: int, *, force: bool = False
    ) -> tuple[list[LogRecord], int]:
        key = (start, end)
        end = min(end, int(time.time()))
        async with self.lock:
            cached = self.window_cache.get(key)
            if not force and cached and time.time() - cached[0] < 30:
                return cached[2], cached[1]
            try:
                async with asyncio.timeout(45):
                    records = await self.client.logs(start, end)
            except TimeoutError:
                raise UpstreamError(
                    "平台日志查询超过45秒，请缩小时间范围后重试，未返回不完整统计"
                ) from None
            records = [r for r in records if start <= r.created_at <= end]
            # Small bounded in-memory cache, never a persistent business-data mirror.
            self.window_cache = {
                k: v for k, v in self.window_cache.items() if time.time() - v[0] < 30
            }
            if len(self.window_cache) >= 4:
                self.window_cache.pop(next(iter(self.window_cache)))
            self.window_cache[key] = (time.time(), end, records)
            return records, end

    async def users(self, keyword: str) -> list[UserInfo]:
        async with self.user_lock:
            cached = self.user_cache.get(keyword)
            if cached and time.time() - cached[0] < 30:
                return cached[1]
            try:
                async with asyncio.timeout(45):
                    users = await self.client.users(keyword)
            except TimeoutError:
                raise UpstreamError("平台用户查询超过45秒，请缩小关键字范围后重试") from None
            self.user_cache = {k: v for k, v in self.user_cache.items() if time.time() - v[0] < 30}
            if len(self.user_cache) >= 8:
                self.user_cache.pop(next(iter(self.user_cache)))
            self.user_cache[keyword] = (time.time(), users)
            return users

    async def sync_window(self, start: int, end: int, *, force: bool = False) -> int:
        _, through = await self.window(start, end, force=force)
        return through

    async def logs(
        self,
        start: int,
        end: int,
        *,
        page: int = 1,
        page_size: int = 20,
        log_type: int = 0,
        channel_id: int = 0,
        http_status: int = 0,
    ) -> dict[str, Any]:
        # Status-code filtering is not supported by NewAPI's list endpoint.
        # Preserve its complete-window path rather than inventing a filtered total.
        if not http_status:
            through = min(end, int(time.time()) + 1)
            try:
                async with asyncio.timeout(45):
                    result = await self.client.query_log_page(
                        start,
                        through,
                        page=page,
                        page_size=page_size,
                        log_type=log_type,
                        channel_id=channel_id,
                    )
            except TimeoutError:
                raise UpstreamError(
                    "平台日志单页查询超过45秒，请稍后重试；未扫描全部日志"
                ) from None
            total = result.total
            assert total is not None  # validated by the adapter
            has_more = page * page_size < total
            return {
                "items": [item.model_dump() for item in result.items],
                "total": total,
                "page": page,
                "page_size": page_size,
                "source": "newapi_admin_api",
                "complete": page == 1 and not has_more,
                "page_complete": True,
                "has_more": has_more,
                "through_timestamp": through - 1,
                "timezone": self.settings.timezone,
                "limitation": "仅返回所请求页；total为上游声明总数，非全窗口扫描或一致性快照，不可据本页计算全量统计",
            }
        records, through = await self.window(start, end)
        selected = sorted(
            (
                r
                for r in records
                if (not log_type or r.type == log_type)
                and (not channel_id or r.channel_id == channel_id)
                and (not http_status or r.http_status == http_status)
            ),
            key=lambda r: (r.created_at, r.id),
            reverse=True,
        )
        return {
            "items": [r.model_dump() for r in selected[(page - 1) * page_size : page * page_size]],
            "total": len(selected),
            "page": page,
            "page_size": page_size,
            "source": "newapi_admin_api",
            "complete": True,
            "through_timestamp": through,
            "timezone": self.settings.timezone,
        }

    async def channel_today_usage(self, channel_id: int) -> dict[str, Any]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        channel = await self.client.channel(channel_id)
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        usage = await self.usage_summary(
            StatisticsQuery(start=today, end=today, channel_id=channel.id)
        )
        return {
            **usage,
            "channel_id": channel.id,
            "channel_name": channel.name,
            "date": today.isoformat(),
        }

    async def usage_summary(self, query: StatisticsQuery) -> dict[str, Any]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        query.reject_future(datetime.now(ZoneInfo(self.settings.timezone)).date())
        start, end = query.window(self.settings.timezone)
        through = min(end, int(time.time()) + 1)
        result = await self.client.usage_counters(start, through, channel_id=query.channel_id)
        return {
            "source": "newapi_admin_api_aggregate",
            "quota": result.quota,
            "rpm_last_minute": result.rpm,
            "tpm_last_minute": result.tpm,
            "from_timestamp": start,
            "through_timestamp": through - 1,
            "timezone": self.settings.timezone,
            "queried_at": time.time(),
            "unit": "quota原始单位，非货币",
            "limitation": "quota为指定时间范围的上游消费汇总；RPM/TPM仅指查询时最近60秒，非所选窗口总次数/Token；不包含排名、错误数、活跃用户或趋势，缺失指标不可推算",
        }

    async def statistics(self, query: StatisticsQuery, *, live: bool = True) -> dict[str, Any]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        query.reject_future(datetime.now(ZoneInfo(self.settings.timezone)).date())
        start, end = query.window(self.settings.timezone)
        if live:
            records, through = await self.window(start, end)
            snapshot = memory_snapshot(records, query, self.settings.timezone, through)
        else:
            through = None
            snapshot = await self.reports.analytics.snapshot(query)
        result = snapshot.model_dump(mode="json")
        if live:
            result["coverage"] = {
                "complete": True,
                "from_timestamp": start,
                "through_timestamp": through,
            }
        result.update(
            source="newapi_admin_api" if live else "local_newapi_logs",
            queried_at=time.time(),
            limitation="完整性指当前令牌可见日志已全部分页获取；不包含平台未记录或已清理的数据，quota为原始额度单位",
        )
        return result
