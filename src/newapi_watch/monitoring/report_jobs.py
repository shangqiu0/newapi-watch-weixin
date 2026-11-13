"""Scheduled reports resume a frozen log window in bounded batches without a business log mirror."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ..client import UpstreamError
from ..models import LogPage
from ..periods import StatisticsQuery
from ..persistence import enqueue, row
from ..redaction import redact_text
from .models import ReportSchedule
from .report_aggregates import ReportAggregates

if TYPE_CHECKING:
    from ..runtime import Runtime


class ReportJob(BaseModel):
    next_page: int = 1
    records_fetched: int = 0
    total_records: int | None = None
    lowest_id: int = 0
    highest_id: int = 0
    direction: str = ""
    failed_attempts: int = 0
    retry_at: float = 0
    error: str = ""
    completed: bool = False
    aggregates: ReportAggregates = Field(default_factory=ReportAggregates)


def next_schedule(kind: str, schedule: ReportSchedule, now: datetime) -> datetime:
    today = now.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
    if kind == "weekly":
        today += timedelta(days=(schedule.weekday - today.weekday()) % 7)
    if today <= now:
        today += timedelta(days=7 if kind == "weekly" else 1)
    return today


class ScheduledReports:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        self.locks: dict[str, asyncio.Lock] = {}

    async def advance(
        self, kind: str, schedule: ReportSchedule, now: datetime, *, retry: bool = False
    ) -> dict[str, Any]:
        async with self.locks.setdefault(kind, asyncio.Lock()):
            return await self._advance(kind, schedule, now, retry=retry)

    async def _advance(
        self, kind: str, schedule: ReportSchedule, now: datetime, *, retry: bool
    ) -> dict[str, Any]:
        following = next_schedule(kind, schedule, now).timestamp()
        due = schedule.enabled and (now.hour, now.minute) >= (schedule.hour, schedule.minute)
        due = due and (kind != "weekly" or now.weekday() == schedule.weekday)
        if not due:
            return {
                "report_stage": "scheduled",
                "next_at": following,
                "note": "未到发送时间，不读取平台日志",
            }
        end = now.date() - timedelta(days=1)
        start = end - timedelta(days=6) if kind == "weekly" else end
        query = StatisticsQuery(start=start, end=end, limit=20)
        key = f"scheduled:{kind}:{start}:{end}"
        job_key = f"report_job:{kind}:{start}:{end}"
        async with self.runtime.store.transaction() as db:
            sent = await row(db, "SELECT id FROM notifications WHERE dedupe_key=?", (key,))
        if sent:
            return {
                "report_stage": "queued",
                "next_at": following,
                "note": "本期报告已入投递队列，不重复生成",
            }
        job = ReportJob.model_validate_json(await self.runtime.store.get_meta(job_key) or "{}")
        if retry:
            job.failed_attempts = 0
            job.retry_at = 0
        elif job.failed_attempts >= schedule.max_failures:
            return self.detail(
                job, "paused", following, "本期失败次数已达上限，后台停止重试；可点立即复查手动重试"
            )
        elif job.retry_at > now.timestamp():
            return self.detail(job, "failed", job.retry_at, "等待退避重试，不重新读取已完成页")
        begun = time.monotonic()
        try:
            if job.completed:
                await self.publish(job, query, key, kind, schedule, now)
                return self.detail(job, "queued", following, "本期完整统计已生成并加入通知队列")
            # TODO(BUG-report-volume): 日报/周报仍逐页扫描完整日志窗口。
            # 续跑仅缓解超时重扫；大数据量依然耗时，并可能超过 max_pages * page_size 上限。
            # 后续应接入并校验上游汇总接口；在此之前不能将该问题视为已修复，
            # 也不能跳过剩余页或把部分统计当完整报告发送。（按用户要求延期处理）
            lower, upper = query.window(self.runtime.settings.timezone)
            for _ in range(schedule.pages_per_run):
                async with asyncio.timeout(30):
                    page = await self.runtime.client.log_page(lower, upper - 1, job.next_page)
                updated = job.model_copy(deep=True)
                self.accept_page(updated, page)
                safe = [
                    r.model_copy(
                        update={"model_name": redact_text(r.model_name, self.runtime.settings)}
                    )
                    for r in page.items
                ]
                updated.aggregates.add(safe, query, self.runtime.settings.timezone)
                updated.records_fetched += len(page.items)
                updated.next_page += 1
                updated.error = ""
                updated.retry_at = 0
                updated.completed = (
                    updated.records_fetched == updated.total_records
                    if updated.total_records is not None
                    else len(page.items) < (page.page_size or self.runtime.settings.page_size)
                )
                await self.runtime.store.set_meta(job_key, updated.model_dump_json())
                job = updated
                if job.completed:
                    await self.publish(job, query, key, kind, schedule, now)
                    return self.detail(job, "queued", following, "本期完整统计已生成并加入通知队列")
                if time.monotonic() - begun >= 25:
                    break
            return self.detail(
                job,
                "collecting",
                now.timestamp() + 30,
                "分批汇总中，已保存页进度；下轮续读，不重复从第一页开始",
            )
        except (UpstreamError, TimeoutError, ValueError) as failure:
            job.failed_attempts += 1
            job.error = redact_text(
                str(failure) or "本页报告读取超过30秒，已完成页仍保留", self.runtime.settings
            )
            job.retry_at = now.timestamp() + max(
                schedule.retry_seconds, int(getattr(failure, "retry_after", 0))
            )
            await self.runtime.store.set_meta(job_key, job.model_dump_json())
            paused = job.failed_attempts >= schedule.max_failures
            detail = self.detail(
                job,
                "paused" if paused else "failed",
                following if paused else job.retry_at,
                "本期已达失败上限，停止后台重试；可立即复查手动重试"
                if paused
                else "本次失败，保留已完成页，稍后从失败页重试",
            )
            detail["attempt_failed"] = True
            return detail

    @staticmethod
    def accept_page(job: ReportJob, page: LogPage) -> None:
        if job.next_page > 1 and page.total != job.total_records:
            raise UpstreamError("报告分页总数变化，保留进度且停止发布；请核对平台日志保留范围")
        if job.next_page == 1:
            job.total_records = page.total
        if page.total is not None and job.records_fetched + len(page.items) > page.total:
            raise UpstreamError("报告分页条数超过总数，未生成不完整报告")
        if not page.items:
            if page.total is not None and job.records_fetched < page.total:
                raise UpstreamError("报告分页中途为空，未生成不完整报告")
            return
        low, high = min(r.id for r in page.items), max(r.id for r in page.items)
        if job.records_fetched:
            direction = (
                "desc" if high < job.lowest_id else "asc" if low > job.highest_id else "overlap"
            )
            if direction == "overlap" or (job.direction and direction != job.direction):
                raise UpstreamError("报告分页重复或顺序变化，未生成不完整报告")
            job.direction = direction
        job.lowest_id = min(job.lowest_id or low, low)
        job.highest_id = max(job.highest_id, high)

    async def publish(
        self,
        job: ReportJob,
        query: StatisticsQuery,
        key: str,
        kind: str,
        schedule: ReportSchedule,
        now: datetime,
    ) -> None:
        snapshot = job.aggregates.snapshot(query, self.runtime.settings.timezone)
        stats = snapshot.summary
        title = "周报" if kind == "weekly" else "日报"
        text = (
            f"{title} · {query.start.year}年{query.start.month}月{query.start.day}日 至 {query.end.month}月{query.end.day}日\n"
            f"来源：绑定平台管理接口 · 日志完整分页\n消费记录：{stats.success_records:,}\n错误记录：{stats.error_records:,}\n消耗：{stats.quota:,} quota\nToken：{stats.tokens:,}\n"
            + "\n".join(
                f"{index + 1}. {item.label} · {item.quota:,} quota"
                for index, item in enumerate(snapshot.ranking)
            )
        )
        image = (
            await self.runtime.reports._image(snapshot, "trend") if schedule.with_image else None
        )
        async with self.runtime.store.transaction() as db:
            await enqueue(db, key, redact_text(text, self.runtime.settings), now.timestamp(), image)
            await db.execute(
                "UPDATE notifications SET kind=?,binding_ids=? WHERE dedupe_key=?",
                (kind, json.dumps(schedule.binding_ids), key),
            )

    @staticmethod
    def detail(job: ReportJob, stage: str, next_at: float, note: str) -> dict[str, Any]:
        return {
            "report_stage": stage,
            "records_fetched": job.records_fetched,
            "total_records": job.total_records,
            "next_page": job.next_page,
            "failed_attempts": job.failed_attempts,
            "next_at": next_at,
            "error": job.error,
            "note": note,
        }
