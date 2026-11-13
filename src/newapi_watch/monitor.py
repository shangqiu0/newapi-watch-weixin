"""Collection and rule evaluation; a failed upstream never looks like an empty snapshot."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from .client import NewAPIClient, UpstreamError
from .config import Settings
from .models import LogRecord
from .store import Store


class Monitor:
    def __init__(self, settings: Settings, store: Store, client: NewAPIClient) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.channel_lock = asyncio.Lock()
        self.log_lock = asyncio.Lock()
        self.error_observer: Callable[[list[LogRecord]], Awaitable[None]] | None = None
        self.probe_semaphore = asyncio.Semaphore(settings.probe_concurrency)
        self.probe_locks: dict[int, asyncio.Lock] = {}

    async def collect_channels(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        async with self.channel_lock:
            try:
                channels = await self.client.channels()
            except UpstreamError:
                await self.store.condition(
                    "service:newapi",
                    True,
                    "NewAPI 渠道采集失败，保留上次快照；请检查连接/令牌/版本",
                    "critical",
                    now,
                )
                return False
            await self.store.sync_channels(channels, now, self.settings.low_balance_threshold)
            await self.store.condition(
                "service:newapi", False, "NewAPI 渠道采集已恢复", "critical", now
            )
            return True

    async def collect_logs(self, now: float | None = None) -> bool:
        end = int(time.time() if now is None else now)
        async with self.log_lock:
            cursor = await self.store.get_meta("log_cursor")
            start = max(
                0,
                int(cursor) - self.settings.log_overlap_seconds
                if cursor
                else end - self.settings.log_initial_hours * 3600,
            )
            try:
                records = await self.client.logs(start, end)
            except UpstreamError:
                await self.store.condition(
                    "service:logs",
                    True,
                    "NewAPI 日志采集失败，游标未推进，统计可能不完整",
                    "warning",
                    end,
                )
                return False
            await self.store.save_logs(records, end, start)
            await self.store.condition(
                "service:logs", False, "NewAPI 日志采集已恢复", "warning", end
            )
            await self._error_rules(end)
            if self.error_observer:
                await self.error_observer(records)
            return True

    async def _error_rules(self, end: int) -> None:
        for channel in await self.store.channels():
            stats = await self.store.summary(end - 300, end + 1, channel["id"])
            total = stats["success_records"] + stats["error_records"]
            if total < self.settings.error_min_records:
                continue
            active = (
                total >= self.settings.error_min_records
                and stats["error_records"] / total >= self.settings.error_ratio_threshold
            )
            await self.store.condition(
                f"channel:{channel['id']}:error_ratio",
                active,
                f"渠道 {channel['id']} 近5分钟错误记录占比异常；{stats['error_records']}/{total}（非最终请求失败率）",
                "warning",
                end,
            )

    async def probe(self, channel_id: int) -> bool:
        lock = self.probe_locks.setdefault(channel_id, asyncio.Lock())
        async with lock, self.probe_semaphore:
            now = time.time()
            latest = await self.store.latest_probe(channel_id)
            if latest and now - float(latest["created_at"]) < self.settings.probe_min_interval:
                return bool(latest["success"])
            try:
                result = await self.client.probe(channel_id)
                success, latency = result.success, result.time
            except UpstreamError:
                success, latency = False, 0.0
            failures = await self.store.save_probe(channel_id, success, latency, now)
            if success or failures >= self.settings.probe_failures:
                await self.store.condition(
                    f"channel:{channel_id}:probe",
                    not success,
                    f"渠道 {channel_id} 主动探测异常 / 探测恢复",
                    "warning",
                    now,
                )
            return success

    async def probe_enabled(self) -> None:
        if self.settings.probe_enabled:
            await asyncio.gather(
                *(
                    self.probe(channel["id"])
                    for channel in await self.store.channels()
                    if channel["status"] == 1
                )
            )
