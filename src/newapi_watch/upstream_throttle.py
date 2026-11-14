"""One bounded request gate per platform, shared by monitors, queries, bots and reports."""

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

_priority: ContextVar[int] = ContextVar("newapi_request_priority", default=1)


@contextmanager
def request_priority(priority: int) -> Iterator[None]:
    token = _priority.set(priority)
    try:
        yield
    finally:
        _priority.reset(token)


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
        self.condition = asyncio.Condition()
        self.waiters: list[tuple[int, int]] = []
        self.sequence = 0
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
        async with self.condition:
            self.sequence += 1
            ticket = (_priority.get(), self.sequence)
            self.waiters.append(ticket)
            self.condition.notify_all()
        try:
            while True:
                async with self.condition:
                    if self.remaining:
                        raise UpstreamRateLimited(self.remaining)
                    if min(self.waiters) != ticket:
                        await self.condition.wait()
                        continue
                    delay = self.next_at - self.clock()
                    if delay <= 0:
                        self.next_at = self.clock() + self.min_interval
                        self.waiters.remove(ticket)
                        self.condition.notify_all()
                        break
                # Never hold the scheduler lock while a low-priority request waits.
                await self.sleep(delay)
        finally:
            async with self.condition:
                if ticket in self.waiters:
                    self.waiters.remove(ticket)
                    self.condition.notify_all()
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
