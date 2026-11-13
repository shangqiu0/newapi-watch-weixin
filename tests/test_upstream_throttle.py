"""External HTTP is faked; pacing/cooldown and the real client are exercised."""

import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from newapi_watch.client import NewAPIClient, UpstreamError
from newapi_watch.config import Settings


def settings():
    return Settings(
        _env_file=None,
        demo=True,
        newapi_url="https://upstream.invalid",
        newapi_token="test-secret",
        newapi_user_id=1,
        request_attempts=3,
        retry_seconds=0,
    )


async def test_429_is_not_immediately_retried_and_blocks_other_endpoints():
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "180"}, text="secret-response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        client = NewAPIClient(settings(), http)
        for run in (client.channels, lambda: client.user(1)):
            with pytest.raises(UpstreamError) as exc:
                await run()
            assert "429" in str(exc.value) and "secret-response" not in str(exc.value)
        assert calls == ["/api/channel/"]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.base = datetime(2026, 11, 13, tzinfo=UTC).timestamp()
        self.waits = []

    def monotonic(self):
        return self.now

    def wall(self):
        return self.base + self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


async def test_requests_share_one_paced_gate_without_sleeping_in_tests():
    from newapi_watch.upstream_throttle import RequestThrottle

    clock = Clock()
    throttle = RequestThrottle(
        min_interval=1.2, clock=clock.monotonic, wall_clock=clock.wall, sleep=clock.sleep
    )
    starts = []

    def upstream(request):
        starts.append(clock.monotonic())
        return httpx.Response(200, json={"success": True, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        client = NewAPIClient(settings(), http, throttle=throttle)
        await asyncio.gather(client.channels(), client.users("a"), client.logs(1, 2))
    assert starts == [0, 1.2, 2.4]


@pytest.mark.parametrize("header,expected", [("180", 180), ("invalid", 60), ("0", 60)])
async def test_retry_after_expiry_and_platform_isolation(header, expected):
    from newapi_watch.upstream_throttle import RequestThrottle

    clock = Clock()
    gate = RequestThrottle(
        min_interval=0, clock=clock.monotonic, wall_clock=clock.wall, sleep=clock.sleep
    )
    failed = True
    seen = []

    def upstream(request):
        seen.append(request.url.host)
        return (
            httpx.Response(429, headers={"Retry-After": header})
            if failed
            else httpx.Response(200, json={"success": True, "data": []})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        client = NewAPIClient(settings(), http, throttle=gate)
        with pytest.raises(UpstreamError) as exc:
            await client.channels()
        assert exc.value.retry_after == expected
        failed = False
        with pytest.raises(UpstreamError):
            await client.logs(1, 2)
        other = NewAPIClient(
            settings().model_copy(update={"newapi_url": "https://other.invalid"}), http
        )
        assert await other.channels() == []
        assert len(seen) == 2
        clock.now = expected
        assert await client.channels() == []


async def test_http_date_retry_after_expires_without_shortening_delay():
    from newapi_watch.upstream_throttle import RequestThrottle

    clock = Clock()
    gate = RequestThrottle(
        min_interval=1, clock=clock.monotonic, wall_clock=clock.wall, sleep=clock.sleep
    )
    deadline = format_datetime(datetime.fromtimestamp(clock.base + 120, UTC), usegmt=True)
    error = gate.rate_limited(deadline)
    assert error.retry_after == 120
    clock.now = 120
    async with gate.slot():
        pass
    assert clock.now == 120


async def test_monitor_429_next_check_respects_shared_cooldown(tmp_path):
    from newapi_watch.monitoring.models import MonitoringConfig
    from newapi_watch.runtime import Runtime

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "180"}))
    ) as http:
        runtime = Runtime(
            settings().model_copy(update={"database": tmp_path / "rate.sqlite3"}), http=http
        )
        await runtime.open(
            start_tasks=False,
            monitoring=MonitoringConfig.model_validate({"channels": {"poll_seconds": 30}}),
        )
        try:
            await runtime.policies.tick(now=1000)
            check = (await runtime.policies.snapshot(now=1000))["checks"][0]
            assert check["status"] == "unknown" and check["next_at"] >= 1180
        finally:
            await runtime.close()


async def test_cancelled_pacing_wait_does_not_lock_out_other_requests():
    from newapi_watch.upstream_throttle import RequestThrottle

    entered = asyncio.Event()
    never = asyncio.Event()
    clock = Clock()

    async def wait(seconds):
        entered.set()
        await never.wait()

    gate = RequestThrottle(
        min_interval=10, clock=clock.monotonic, wall_clock=clock.wall, sleep=wait
    )
    async with gate.slot():
        pass

    async def waiting_request():
        async with gate.slot():
            pass

    pending = asyncio.create_task(waiting_request())
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    clock.now = 10
    async with gate.slot():
        pass
    assert not gate.lock.locked()


async def test_configured_long_cooldown_is_not_shortened_by_default_cap():
    from newapi_watch.upstream_throttle import RequestThrottle

    clock = Clock()
    gate = RequestThrottle(cooldown=3600, clock=clock.monotonic, wall_clock=clock.wall)
    assert gate.rate_limited(None).retry_after == 3600
