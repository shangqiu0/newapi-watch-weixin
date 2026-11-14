"""Optional Redis acceleration, never the durable source of task progress."""

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class QueryOptions(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WATCH_QUERY_", env_file=".env", extra="ignore")
    redis_url: SecretStr = SecretStr("")
    cache_ttl_seconds: int = Field(default=30, ge=5, le=3600)
    task_request_budget: int = Field(default=5, ge=1, le=100)
    task_seconds_budget: int = Field(default=20, ge=5, le=120)
    task_max_requests: int = Field(default=10000, ge=1, le=100000)

    @field_validator("redis_url")
    @classmethod
    def redis_endpoint(cls, value: SecretStr) -> SecretStr:
        url = value.get_secret_value()
        if url:
            parts = urlsplit(url)
            if parts.scheme not in {"redis", "rediss"} or not parts.hostname:
                raise ValueError("Redis地址需使用redis://或rediss://")
        return value


class Backend(Protocol):
    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: str, *, ex: int) -> Any: ...
    async def aclose(self) -> None: ...


class QueryCache:
    def __init__(
        self,
        scope: str,
        *,
        ttl: int = 30,
        max_entries: int = 32,
        backend: Backend | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.prefix = "newapi-watch:q1:" + hashlib.sha256(scope.encode()).hexdigest() + ":"
        self.ttl, self.capacity, self.backend, self.clock = ttl, max_entries, backend, clock
        self.items: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.blocked_until = 0.0
        self.unavailable = False

    @classmethod
    def configured(cls, scope: str, options: QueryOptions) -> "QueryCache":
        backend = None
        missing = False
        if options.redis_url.get_secret_value():
            try:
                from redis.asyncio import Redis

                backend = Redis.from_url(
                    options.redis_url.get_secret_value(),
                    decode_responses=True,
                    socket_timeout=0.5,
                    socket_connect_timeout=0.5,
                    retry_on_timeout=False,
                )
            except (ImportError, ValueError):
                missing = True
        cache = cls(scope, ttl=options.cache_ttl_seconds, backend=cast(Backend | None, backend))
        cache.unavailable = missing
        return cache

    def key(self, key: str) -> str:
        return self.prefix + hashlib.sha256(key.encode()).hexdigest()

    async def get(self, key: str) -> dict[str, Any] | None:
        full = self.key(key)
        item = self.items.get(full)
        if item and item[0] > self.clock():
            self.items.move_to_end(full)
            value: dict[str, Any] = json.loads(item[1])
            return value
        self.items.pop(full, None)
        if self.backend and self.clock() >= self.blocked_until:
            try:
                raw = await self.backend.get(full)
                if raw and len(raw) < 2_000_000:
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        return value
            except (OSError, ValueError, TimeoutError):
                self.blocked_until = self.clock() + 30
            except Exception as exc:
                # Optional client libraries have their own connection error types.
                # Do not expose exception text (may contain the Redis URL/password).
                if type(exc).__module__.startswith("redis"):
                    self.blocked_until = self.clock() + 30
                else:
                    raise
        return None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(raw) >= 2_000_000:
            return
        full = self.key(key)
        self.items[full] = (self.clock() + self.ttl, raw)
        self.items.move_to_end(full)
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
        if self.backend and self.clock() >= self.blocked_until:
            try:
                await self.backend.set(full, raw, ex=self.ttl)
            except (OSError, ValueError, TimeoutError):
                self.blocked_until = self.clock() + 30
            except Exception as exc:
                if type(exc).__module__.startswith("redis"):
                    self.blocked_until = self.clock() + 30
                else:
                    raise

    def status(self) -> dict[str, Any]:
        return {
            "backend": "redis+memory" if self.backend else "memory",
            "redis_available": bool(self.backend and self.clock() >= self.blocked_until),
            "fallback": bool(self.unavailable or self.blocked_until > self.clock()),
            "ttl_seconds": self.ttl,
        }

    async def close(self) -> None:
        if self.backend:
            try:
                await self.backend.aclose()
            except (OSError, TimeoutError):
                pass
