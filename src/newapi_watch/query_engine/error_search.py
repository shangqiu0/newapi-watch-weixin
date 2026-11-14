"""Owner-bound status-code search continuation. Scans error pages, not all traffic."""

import asyncio
import time
from typing import Any
from uuid import uuid4

from ..client import NewAPIClient, UpstreamError
from .cache import QueryCache


class ErrorSearch:
    def __init__(self, client: NewAPIClient) -> None:
        self.client = client
        self.cache = QueryCache("error-continuations:" + uuid4().hex, ttl=600, max_entries=64)
        self.lock = asyncio.Lock()

    async def search(
        self, start: int, end: int, *, owner: str, status: int, channel: int = 0, cursor: str = ""
    ) -> dict[str, Any]:
        if not cursor and not 400 <= status <= 599:
            raise ValueError("状态码查询仅支持400..599错误记录")
        async with self.lock:
            key = owner + ":" + cursor
            state = await self.cache.get(key) if cursor else None
            if cursor and not state:
                raise ValueError("续查游标无效、过期或不属于当前会话，请重新查询")
            if state is None:
                state = {
                    "start": start,
                    "end": min(end, int(time.time()) + 1),
                    "page": 1,
                    "status": status,
                    "channel": channel,
                    "scanned": 0,
                    "total": None,
                }
            found: list[dict[str, Any]] = []
            more = True
            initial_page = state["page"]
            exhausted = False
            try:
                async with asyncio.timeout(12):
                    for _ in range(3):
                        result = await self.client.query_log_page(
                            state["start"],
                            state["end"],
                            page=state["page"],
                            page_size=100,
                            log_type=5,
                            channel_id=state["channel"],
                        )
                        if state["total"] is not None and state["total"] != result.total:
                            raise UpstreamError("错误分页期间总数变化，请重新开始；未声称完整统计")
                        seen = set(state.get("seen_ids") or [])
                        if any(record.id in seen for record in result.items):
                            raise UpstreamError(
                                "错误日志续查出现跨页重复，请重新开始；未声称完整结果"
                            )
                        seen.update(record.id for record in result.items)
                        if len(seen) > 10000:
                            raise UpstreamError(
                                "错误续查超过安全去重预算，请缩小范围或创建后台精确任务"
                            )
                        state["seen_ids"] = sorted(seen)
                        state["total"] = result.total
                        state["scanned"] += len(result.items)
                        state["unknown_status_records"] = state.get(
                            "unknown_status_records", 0
                        ) + sum(not r.http_status for r in result.items)
                        found.extend(
                            r.model_dump() for r in result.items if r.http_status == state["status"]
                        )
                        state["page"] += 1
                        more = (state["page"] - 1) * 100 < int(result.total or 0)
                        if not more or found:
                            break
            except TimeoutError:
                if state["page"] == initial_page:
                    raise UpstreamError(
                        "错误日志单页查询超过预算，请稍后重试；本次未取得可确认记录"
                    ) from None
                exhausted = True
            token = uuid4().hex
            if more:
                await self.cache.set(owner + ":" + token, state)
            return {
                "items": found,
                "total": None,
                "matched_this_batch": len(found),
                "scanned_records": state["scanned"],
                "has_more": more,
                "budget_exhausted": exhausted,
                "cursor": token if more else "",
                "source": "newapi_error_pages",
                "complete": not more and not state.get("unknown_status_records", 0),
                "unknown_status_records": state.get("unknown_status_records", 0),
                "limitation": "仅为本批匹配明细，total未知；不可据本页推算全量错误次数。游标仅对当前平台机器人会话有效，10分钟过期。",
            }
