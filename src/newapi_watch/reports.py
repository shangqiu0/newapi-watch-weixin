"""Calendar-correct summaries, dimension rankings and PNG reports."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date

from .analytics import Analytics, Snapshot
from .config import Settings
from .models import CommandResult
from .operations import OperationError
from .periods import Period, StatisticsQuery
from .persistence import row
from .plotting import ChartKind, render
from .redaction import redact_text
from .store import Store


def day_window(day: date, timezone: str) -> tuple[int, int]:
    return Period(start=day, end=day).window(timezone)


class Reports:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings, self.store = settings, store
        self.analytics = Analytics(store, settings.timezone)
        self.chart_lock = asyncio.Lock()
        self._chart_users = 0
        self.live_snapshot: Callable[[StatisticsQuery], Awaitable[Snapshot]] | None = None

    async def snapshot(self, query: StatisticsQuery) -> Snapshot:
        return (
            await self.live_snapshot(query)
            if self.live_snapshot
            else await self.analytics.snapshot(query)
        )

    async def daily(self, day: date, *, with_image: bool = True) -> CommandResult:
        query = StatisticsQuery(start=day, end=day, limit=3)
        snapshot = await self.snapshot(query)
        lines = self._summary(snapshot, title=f"NewAPI 日报 · {day}")
        lines.append(
            "渠道消耗前三："
            + (
                "；".join(f"{item.label} {item.quota}" for item in snapshot.channel_leaders)
                or "暂无记录"
            )
        )
        async with self.store.transaction() as db:
            alerts = await row(db, "SELECT COUNT(*) AS count FROM alerts WHERE state='firing'")
        lines.append(f"当前告警：{alerts['count'] if alerts else 0} 条未恢复（非当日触发次数）")
        image = await self._image(snapshot, "trend") if with_image else None
        return CommandResult(text="\n".join(lines), image=image)

    async def range_report(
        self, query: StatisticsQuery, *, with_image: bool = True, chart_kind: ChartKind = "trend"
    ) -> CommandResult:
        snapshot = await self.snapshot(query)
        lines = self._summary(snapshot, title=f"NewAPI 统计 · {query.start} ~ {query.end}")
        image = await self._image(snapshot, chart_kind) if with_image else None
        return CommandResult(text="\n".join(lines), image=image)

    async def chart(self, day: date) -> bytes:
        return await self.range_chart(StatisticsQuery(start=day, end=day))

    async def range_chart(self, query: StatisticsQuery, *, kind: ChartKind = "trend") -> bytes:
        return await self._image(await self.snapshot(query), kind)

    async def _image(self, snapshot: Snapshot, kind: ChartKind) -> bytes:
        if self._chart_users >= self.settings.bot_concurrency:
            raise OperationError(429, "图表生成繁忙，请稍后重试")
        self._chart_users += 1
        try:
            async with self.chart_lock:
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        render,
                        snapshot,
                        kind,
                        self.settings.chart_font,
                        self.settings.demo,
                        self.settings.currency,
                    )
                )
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Cancelling an await cannot stop matplotlib's thread. Keep its slot/lock
                    # until the worker exits, instead of allowing concurrent render threads.
                    await worker
                    raise
        finally:
            self._chart_users -= 1

    def _summary(self, snapshot: Snapshot, *, title: str) -> list[str]:
        stats = snapshot.summary
        labels = {"model": "模型", "channel": "渠道", "user": "用户"}
        amount = (
            self.settings.currency.display(stats.quota)
            if self.settings.currency.confirmed
            else f"{stats.quota} quota（原始单位，非货币）"
        )
        lines = [
            f"**{'[DEMO] ' if self.settings.demo else ''}{title}**",
            f"时区：{snapshot.timezone}",
            f"数据覆盖：{'完整' if snapshot.coverage.complete else '不完整（首次采集、采集延迟、当日未结束或超出保留期）'}",
            f"成功记录：{stats.success_records} · 错误记录：{stats.error_records}",
            f"消耗：{amount} · Token：{stats.tokens}",
            f"活跃用户：{stats.active_users} · 活跃渠道：{stats.active_channels}",
            f"{labels[snapshot.query.dimension]}排行（按 {snapshot.query.metric}，最多{snapshot.query.limit}项）：",
        ]
        lines.extend(
            f"#{index} {item.label} · 消耗 {self.settings.currency.display(item.quota)} · 成功 {item.success_records} · 错误 {item.error_records} · Token {item.tokens}"
            for index, item in enumerate(snapshot.ranking, 1)
        )
        if not snapshot.ranking:
            lines.append("暂无已采集记录")
        lines.append(
            "口径：NewAPI type=2 消费记录、type=5 错误记录；重试可能多记，不等于最终请求成功率。"
        )
        return [redact_text(line, self.settings) for line in lines]
