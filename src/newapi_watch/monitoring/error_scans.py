"""Bounded, durable error scans. Full-window cursors never stand in for page progress."""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from ..client import UpstreamError
from ..models import LogPage

if TYPE_CHECKING:
    from .service import MonitoringService

SCAN_KEY = "policy:error_scan"
SCAN_SECONDS = 40.0


class ErrorScan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    floor: int = Field(ge=0)
    next_page: int = Field(default=1, ge=1)
    records: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    page_size: int | None = Field(default=None, ge=1)
    lowest_id: int = 0
    highest_id: int = 0
    direction: str = ""

    def accept(self, page: LogPage, requested_size: int) -> bool:
        if self.next_page > 1 and (page.total != self.total or page.page_size != self.page_size):
            raise UpstreamError("错误日志分页总数或大小变化，已从冻结窗口起点重新检查")
        if self.next_page == 1:
            self.total, self.page_size = page.total, page.page_size
        count = self.records + len(page.items)
        if page.total is not None and (
            count > page.total or (not page.items and count < page.total)
        ):
            raise UpstreamError("错误日志分页不完整，已从冻结窗口起点重新检查")
        if page.items:
            low, high = min(r.id for r in page.items), max(r.id for r in page.items)
            if self.records:
                direction = (
                    "desc"
                    if high < self.lowest_id
                    else "asc"
                    if low > self.highest_id
                    else "overlap"
                )
                if direction == "overlap" or (self.direction and direction != self.direction):
                    raise UpstreamError("错误日志分页重复或顺序变化，已从冻结窗口起点重新检查")
                self.direction = direction
            self.lowest_id = min(self.lowest_id or low, low)
            self.highest_id = max(self.highest_id, high)
        self.records = count
        self.next_page += 1
        return (
            count == page.total
            if page.total is not None
            else len(page.items) < (page.page_size or requested_size)
        )

    def detail(self, now: float, *, complete: bool) -> dict[str, Any]:
        return {
            "from_timestamp": self.start,
            "through_timestamp": self.end if complete else self.floor,
            "window_end_timestamp": self.end,
            "lag_seconds": max(0, int(now) - (self.end if complete else self.floor)),
            "records": self.records,
            "next_page": self.next_page,
            "complete": complete,
            "paused": not complete,
        }


class ErrorScanner:
    def __init__(self, service: "MonitoringService") -> None:
        self.service = service
        self.runtime = service.runtime

    async def advance(self, now: float, *, clock: Callable[[], float]) -> dict[str, Any]:
        rule, store = self.service.config.errors, self.runtime.store
        cursor = await store.get_meta("policy:error_cursor")
        if cursor is None and not rule.initial_lookback_seconds:
            async with store.transaction() as db:
                await store.put_meta(db, "policy:error_floor", str(int(now)), now)
                await store.put_meta(db, "policy:error_cursor", str(int(now)), now)
            return {"baseline": int(now), "records": 0, "note": "从当前时刻监控，不追发旧错误"}
        saved = await store.get_meta(SCAN_KEY)
        cursor_at = int(cursor) if cursor else max(0, int(now) - rule.initial_lookback_seconds)
        start = max(0, cursor_at - rule.overlap_seconds)
        baseline = int(await store.get_meta("policy:error_floor") or str(start))
        scan = (
            ErrorScan.model_validate_json(saved)
            if saved
            else ErrorScan(
                start=start,
                end=min(int(now), cursor_at + rule.window_seconds),
                floor=max(start, baseline),
            )
        )
        if not saved:
            # Freeze even before page 1: a failed first request must not change
            # the scan bounds on every retry.
            await store.set_meta(SCAN_KEY, scan.model_dump_json())
        began, completed_pages = clock(), 0
        settings = self.runtime.client.settings
        while clock() - began < SCAN_SECONDS:
            if scan.next_page > settings.max_pages:
                raise UpstreamError("错误日志超过分页上限，已保留进度；请缩小错误监控窗口")
            remaining = SCAN_SECONDS - (clock() - began)
            try:
                async with asyncio.timeout(remaining):
                    page = await self.runtime.client.log_page_error(
                        scan.start, scan.end, scan.next_page
                    )
            except TimeoutError:
                if not completed_pages:
                    raise UpstreamError("错误日志单页请求超时，已保留完成页进度") from None
                break
            updated = scan.model_copy(deep=True)
            try:
                complete = updated.accept(page, settings.page_size)
            except UpstreamError:
                # Changed offset pagination cannot be trusted. Re-read this frozen
                # window; durable error_seen absorbs already accepted records.
                reset = ErrorScan(start=scan.start, end=scan.end, floor=scan.floor)
                await store.set_meta(SCAN_KEY, reset.model_dump_json())
                raise
            records = [r for r in page.items if scan.floor <= r.created_at <= scan.end]
            # One short transaction: dedupe, buckets, outbox AND page checkpoint.
            # Cancellation commits everything or rolls back everything for this page.
            await self.service.errors(records, now=now, checkpoint=updated, complete=complete)
            scan = updated
            completed_pages += 1
            if complete:
                return scan.detail(now, complete=True)
        return scan.detail(now, complete=False)
