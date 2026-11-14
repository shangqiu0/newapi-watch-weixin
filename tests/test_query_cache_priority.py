import asyncio

from newapi_watch.upstream_throttle import RequestThrottle


async def test_monitor_preempts_waiting_background_without_breaking_pacing():
    from newapi_watch.upstream_throttle import request_priority

    now = 0.0
    asleep = asyncio.Event()
    release = asyncio.Event()

    async def sleep(seconds):
        nonlocal now
        asleep.set()
        await release.wait()
        now = max(now, 1.2)

    gate = RequestThrottle(clock=lambda: now, sleep=sleep)
    async with gate.slot():
        pass
    order = []

    async def request(name, priority):
        with request_priority(priority):
            async with gate.slot():
                order.append(name)

    bg = asyncio.create_task(request("background", 2))
    await asleep.wait()
    monitor = asyncio.create_task(request("monitor", 0))
    # A zero-delay scheduling checkpoint, not a timing-based wait.
    await asyncio.sleep(0)
    now = 1.2
    release.set()

    async def advance(seconds):
        nonlocal now
        now += seconds

    gate.sleep = advance
    await asyncio.gather(bg, monitor)
    assert order == ["monitor", "background"]


async def test_memory_cache_expires_and_bounds_entries():
    from newapi_watch.query_engine.cache import QueryCache

    now = 0.0
    cache = QueryCache("platform-a", ttl=10, max_entries=2, clock=lambda: now)
    await cache.set("a", {"quota": 1})
    await cache.set("b", {"quota": 2})
    assert await cache.get("a") == {"quota": 1}
    await cache.set("c", {"quota": 3})
    assert await cache.get("b") is None
    now = 11
    assert await cache.get("a") is None
    await cache.close()


async def test_redis_outage_is_circuit_broken_and_memory_result_remains_available():
    from newapi_watch.query_engine.cache import QueryCache

    class UnavailableRedis:
        calls = 0

        async def get(self, key):
            self.calls += 1
            raise OSError("private-redis-password")

        async def set(self, *args, **kwargs):
            self.calls += 1
            raise OSError("private-redis-password")

        async def aclose(self):
            pass

    redis = UnavailableRedis()
    cache = QueryCache("platform-a", backend=redis)
    assert await cache.get("missing") is None
    await cache.set("a", {"quota": 7})
    assert await cache.get("a") == {"quota": 7}
    assert redis.calls == 1 and "private" not in str(cache.status())
    await cache.close()


async def test_shared_redis_cache_namespaces_do_not_cross_platforms():
    from newapi_watch.query_engine.cache import QueryCache

    class RedisMemory:
        data = {}

        async def get(self, key):
            return self.data.get(key)

        async def set(self, key, value, ex):
            self.data[key] = value

        async def aclose(self):
            pass

    shared = RedisMemory()
    a = QueryCache("platform-a-token1", backend=shared)
    b = QueryCache("platform-b-token2", backend=shared)
    await a.set("today", {"quota": 100})
    assert await b.get("today") is None
    assert all("token1" not in k for k in shared.data)
    await a.close()
    await b.close()
