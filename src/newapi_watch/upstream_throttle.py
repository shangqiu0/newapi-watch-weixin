"""One bounded request gate per platform, shared by monitors, queries, bots and reports."""

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any


class UpstreamError(RuntimeError):
    """Public errors never contain credential URLs or upstream response bodies."""


class UpstreamRateLimited(UpstreamError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(
            f"NewAPI HTTP 429：平台限流，已暂停该平台请求，约 {self.retry_after} 秒后重试"
        )


class RequestThrottle:
    def __init__(
        self,
        *,
        min_interval: float = 1.2,
        cooldown: float = 60,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval, self.cooldown = min_interval, cooldown
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.lock = asyncio.Lock()
        self.next_at = 0.0
        self.blocked_until = 0.0
        self.penalties = 0

    def configure(self, min_interval: float, cooldown: float) -> None:
        self.min_interval, self.cooldown = min_interval, cooldown

    @property
    def remaining(self) -> int:
        return max(0, math.ceil(self.blocked_until - self.clock()))

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self.lock:
            if self.remaining:
                raise UpstreamRateLimited(self.remaining)
            delay = self.next_at - self.clock()
            if delay > 0:
                await self.sleep(delay)
            if self.remaining:
                raise UpstreamRateLimited(self.remaining)
            self.next_at = self.clock() + self.min_interval
        # Pace request starts, not response completion: a slow user API must not block channels.
        yield

    def rate_limited(self, header: str | None) -> UpstreamRateLimited:
        self.penalties += 1
        delay = max(self.cooldown, min(900, self.cooldown * 2 ** min(self.penalties - 1, 5)))
        if header and len(header) <= 128:
            try:
                if header.strip().isdigit():
                    retry = float(header.strip())
                else:
                    date = parsedate_to_datetime(header)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=UTC)
                    retry = date.timestamp() - self.wall_clock()
                if math.isfinite(retry):
                    delay = max(delay, retry)
            except (ValueError, TypeError, OverflowError):
                pass  # Invalid optional Retry-After: retain the configured safe backoff.
        self.blocked_until = max(self.blocked_until, self.clock() + delay)
        return UpstreamRateLimited(self.remaining)

    def succeeded(self) -> None:
        self.penalties = 0

    def snapshot(self) -> dict[str, Any]:
        remaining = self.remaining
        return {
            "min_interval_seconds": self.min_interval,
            "rate_limit_cooldown_seconds": self.cooldown,
            "cooldown_remaining": remaining,
            "cooldown_until": self.wall_clock() + remaining if remaining else None,
        }
