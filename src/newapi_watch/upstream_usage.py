"""Bounded self-only daily consumption; no balance-delta estimates or log mirroring."""

import asyncio
import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .client import NewAPIClient, UpstreamError
from .query_engine.cache import QueryCache

if TYPE_CHECKING:
    from .runtime import Runtime
    from .upstream_accounts import UpstreamAccount


class UpstreamUsage:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        # Shares optional Redis transport, whose lifecycle is owned by the query engine.
        self.cache = QueryCache(
            str(runtime.settings.database) + ":upstream-usage",
            ttl=300,
            max_entries=90,
            backend=runtime.query_engine.cache.backend,
        )
        self.locks: dict[str, asyncio.Lock] = {}

    def window(self, account: "UpstreamAccount", now: float) -> tuple[int, str]:
        timezone = account.usage_timezone or self.runtime.settings.timezone
        local = datetime.fromtimestamp(now, ZoneInfo(timezone))
        return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()), timezone

    async def snapshot(
        self, account: "UpstreamAccount", scope: str, *, now: float | None = None
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        start, timezone = self.window(account, now)
        raw = await self.runtime.store.get_meta("upstream:usage:" + scope)
        value = json.loads(raw) if raw else {}
        if value.get("start_at") != start or value.get("timezone") != timezone:
            return {"status": "unread", "timezone": timezone, "start_at": start}
        value["stale"] = bool(value.get("stale")) or now - value.get("queried_at", 0) >= 300
        return dict(value)

    async def query(
        self,
        account: "UpstreamAccount",
        client: NewAPIClient,
        scope: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        start, timezone = self.window(account, now)
        key = f"{scope}:{timezone}:{start}"
        async with self.locks.setdefault(scope, asyncio.Lock()):
            cached = await self.cache.get(key)
            if cached and 0 <= now - cached["queried_at"] < 300:
                return cached
            result: dict[str, Any] = {
                "source": "newapi_log_self_stat",
                "start_at": start,
                "end_at": int(now),
                "timezone": timezone,
                "queried_at": now,
                "stale": False,
            }
            try:
                async with asyncio.timeout(20):
                    payload = await client.request(
                        "/api/log/self/stat",
                        {"start_timestamp": start, "end_timestamp": int(now)},
                        retry=False,
                    )
                data = payload.get("data")
                quota = data.get("quota") if isinstance(data, dict) else None
                if type(quota) is not int or not 0 <= quota <= 2**63 - 1:
                    raise ValueError("上游今日统计缺少有效消费额度，未将缺失值当作零")
                result.update(
                    status="ok",
                    quota=quota,
                    quota_cny=account.currency.amount(quota),
                    quota_display=account.currency.display(quota),
                )
            except (UpstreamError, ValueError, TimeoutError) as error:
                result.update(status="error", error=str(error) or "上游今日统计读取超时")
                previous = await self.snapshot(account, scope, now=now)
                if previous.get("quota") is not None:
                    # Preserve the last successful cutoff, never label an old amount as current.
                    for field in ("quota", "quota_cny", "quota_display", "end_at"):
                        result[field] = previous[field]
                    result["stale"] = True
            if self.runtime.upstream is not None and self.runtime.upstream.closed:
                return {"status": "error", "error": "上游账户配置已变更"}
            await self.runtime.store.set_meta(
                "upstream:usage:" + scope, json.dumps(result, ensure_ascii=False)
            )
            await self.cache.set(key, result)
            return result
