"""Per-page resumable exact-query worker. SQLite commits never hold HTTP locks."""

import asyncio
import json
import time
from typing import Any

from ..client import UpstreamError
from ..persistence import row
from ..redaction import redact_text
from ..upstream_throttle import request_priority
from .schemas import JobInput as JobInput
from .task_store import TaskRepository
from .totals import QueryTotals


class QueryJobs(TaskRepository):
    async def tick(self) -> None:
        if self.parallel_guard and not self.parallel_guard():
            return
        async with self.lock:
            now = time.time()
            async with self.store.transaction() as db:
                item = await row(
                    db,
                    "SELECT id,query,progress,context_json FROM query_jobs "
                    "WHERE status IN ('queued','waiting','running') AND next_at<=? "
                    "ORDER BY created_at LIMIT 1",
                    (now,),
                )
                if not item:
                    return
                claimed = await db.execute(
                    "UPDATE query_jobs SET status='running',updated_at=? WHERE id=? "
                    "AND status IN ('queued','waiting','running')",
                    (now, item["id"]),
                )
                if claimed.rowcount != 1:
                    return
            try:
                if json.loads(item["context_json"]).get("_scope") != self.scope:
                    raise ValueError("平台令牌或用户已变更，请重新创建查询，旧结果不与新权限混用")
                await self._step(item)
            except UpstreamError as exc:
                delay = max(30, int(getattr(exc, "retry_after", 0))) if "429" in str(exc) else 30
                status = "waiting" if "429" in str(exc) else "paused"
                await self._update_failure(item["id"], status, str(exc), delay)
            except (TimeoutError, ValueError) as exc:
                await self._update_failure(item["id"], "paused", str(exc), 60)

    async def _update_failure(self, identifier: str, status: str, message: str, delay: int) -> None:
        now = time.time()
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE query_jobs SET status=?,error=?,next_at=?,updated_at=? "
                "WHERE id=? AND status='running'",
                (
                    status,
                    redact_text(message, self.client.settings)[:1000],
                    now + delay,
                    now,
                    identifier,
                ),
            )

    async def _step(self, item: dict[str, Any]) -> None:
        request = JobInput.model_validate_json(item["query"])
        query = request.query()
        state = json.loads(item["progress"] or "{}")
        windows = state["windows"]
        current = int(state["window_index"])
        if current >= len(windows):
            return await self._complete(item["id"], request, state)
        lower, upper = windows[current]
        page = int(state["page"])
        expected_total = state["expected_total"]
        calls = 0
        started = time.monotonic()
        aggregates = QueryTotals.model_validate(state.get("aggregates") or {})
        seen = set(state.get("seen_ids") or [])
        while (
            calls < self.options.task_request_budget
            and time.monotonic() - started < self.options.task_seconds_budget
        ):
            if self.parallel_guard and not self.parallel_guard():
                break
            if state.get("total_requests", 0) >= state.get(
                "request_ceiling", self.options.task_max_requests
            ):
                await self._update_failure(
                    item["id"], "paused", "达到请求预算，进度已保存；可手工继续", 3600
                )
                return
            with request_priority(2):
                async with asyncio.timeout(30):
                    result = await self.client.query_log_page(
                        lower,
                        upper,
                        page=page,
                        page_size=self.page_size,
                        log_type=5 if request.status_codes else 0,
                        channel_id=request.channel_id,
                    )
            calls += 1
            state["total_requests"] = state.get("total_requests", 0) + 1
            if expected_total is None:
                expected_total = result.total
            elif expected_total != result.total:
                if state["restarts"] >= 100 or len(windows) >= 2000:
                    raise ValueError("分页持续变化，已达到安全拆分上限")
                windows = self._split(lower, upper, windows, current)
                state.update(
                    windows=windows,
                    page=1,
                    expected_total=None,
                    aggregates={},
                    seen_ids=[],
                    records_read=state["records_read"] - state.get("window_records", 0),
                    window_records=0,
                    unknown_status_records=state.get("unknown_status_records", 0)
                    - state.get("window_unknown", 0),
                    window_unknown=0,
                    restarts=state["restarts"] + 1,
                )
                await self._checkpoint(item["id"], state, "queued")
                return
            if int(result.total or 0) > 5000 and page == 1:
                windows = self._split(lower, upper, windows, current)
                state.update(windows=windows, restarts=state["restarts"] + 1)
                if len(windows) > 2000:
                    raise ValueError("查询区间拆分达到安全上限")
                await self._checkpoint(item["id"], state, "queued")
                return
            for record in result.items:
                if record.id in seen:
                    raise ValueError("分页记录重复，本段结果未确认")
                seen.add(record.id)
                if len(seen) > 10000:
                    raise ValueError("单段日志量超过去重预算，请缩小范围")
                if request.status_codes and record.type == 5 and not record.http_status:
                    state["unknown_status_records"] = state.get("unknown_status_records", 0) + 1
                    state["window_unknown"] = state.get("window_unknown", 0) + 1
                if (
                    record.type == 5
                    and request.status_codes
                    and record.http_status not in request.status_codes
                ):
                    continue
                aggregates.add([record], query, self.timezone)
            state.update(
                page=page + 1,
                expected_total=expected_total,
                aggregates=aggregates.model_dump(mode="json"),
                seen_ids=sorted(seen),
                records_read=state["records_read"] + len(result.items),
                window_records=state.get("window_records", 0) + len(result.items),
            )
            if page * self.page_size >= int(expected_total or 0):
                committed = QueryTotals.model_validate(state.get("committed") or {})
                committed.merge(aggregates)
                current += 1
                state.update(
                    window_index=current,
                    page=1,
                    expected_total=None,
                    seen_ids=[],
                    aggregates={},
                    committed=committed.model_dump(mode="json"),
                    window_records=0,
                    window_unknown=0,
                )
                if current >= len(windows):
                    return await self._complete(item["id"], request, state)
                lower, upper = windows[current]
                page = 1
                expected_total = None
                seen = set()
                aggregates = QueryTotals()
            else:
                page += 1
            if not await self._checkpoint(item["id"], state, "running"):
                return
        if state.get("total_requests", 0) >= state.get(
            "request_ceiling", self.options.task_max_requests
        ):
            await self._update_failure(
                item["id"], "paused", "达到请求预算，进度已保存；可手工继续", 3600
            )
            return
        await self._checkpoint(item["id"], state, "queued")

    async def _complete(self, identifier: str, request: JobInput, state: dict[str, Any]) -> None:
        result = self._result(request, state)
        now = time.time()
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE query_jobs SET status='complete',result=?,error='',progress=?,updated_at=? "
                "WHERE id=? AND status='running'",
                (
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(state, ensure_ascii=False),
                    now,
                    identifier,
                ),
            )

    async def _checkpoint(self, identifier: str, state: dict[str, Any], status: str) -> bool:
        now = time.time()
        async with self.store.transaction() as db:
            updated = await db.execute(
                "UPDATE query_jobs SET status=?,progress=?,error='',next_at=?,updated_at=? "
                "WHERE id=? AND status='running'",
                (status, json.dumps(state, ensure_ascii=False), now, now, identifier),
            )
            return updated.rowcount == 1

    @staticmethod
    def _split(start: int, end: int, windows: list[list[int]], index: int) -> list[list[int]]:
        if end - start <= 1:
            raise ValueError("分页总数在最小时间区间仍持续变化")
        middle = start + (end - start) // 2
        return [*windows[:index], [start, middle], [middle, end], *windows[index + 1 :]]

    def _result(self, request: JobInput, state: dict[str, Any]) -> dict[str, Any]:
        query = request.query()
        windows = state["windows"]
        data = QueryTotals.model_validate(state.get("committed") or {}).snapshot(
            query, self.timezone, windows[-1][1]
        )
        data.update(
            source="newapi_exact_resumable",
            records_read=state["records_read"],
            restarts=state["restarts"],
            queried_at=time.time(),
            limitation="精确读取冻结时间窗口内当前令牌可见、平台仍保留的全部分页；不包含平台未记录或已清理日志。",
        )
        if request.status_codes:
            data["status_code_filter"] = request.status_codes
            unknown = state.get("unknown_status_records", 0)
            data["unknown_status_records"] = unknown
            data["filter_complete"] = not unknown
            if unknown:
                data["coverage"]["complete"] = False
                data["limitation"] += (
                    f"其中 {unknown} 条错误未提供可识别HTTP状态码，匹配次数仅为已识别部分，不能认定为零错误。"
                )
        return data
