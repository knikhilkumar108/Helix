"""
Lightweight token-bucket rate limiter used for both in-process and per-tenant
quotas. Backed by Redis for distributed deployments; in-memory for tests.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


@dataclass(slots=True)
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last: float


class InMemoryLimiter:
    def __init__(self, clock: Clock | None = None) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self.clock = clock or SystemClock()

    async def take(self, key: str, capacity: int, refill_per_sec: float, cost: float = 1.0) -> bool:
        now = self.clock.now()
        async with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(
                    capacity=capacity,
                    refill_per_sec=refill_per_sec,
                    tokens=capacity,
                    last=now,
                )
                self._buckets[key] = b
            elapsed = max(0.0, now - b.last)
            b.tokens = min(b.capacity, b.tokens + elapsed * b.refill_per_sec)
            b.last = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True
            return False


def rate_limited(
    key_fn: Callable[..., str],
    *,
    capacity: int,
    refill_per_sec: float,
    cost: float = 1.0,
) -> Callable[[Callable[..., Awaitable]]]:
    limiter = InMemoryLimiter()

    async def decorator(*args, **kwargs):  # type: ignore[no-untyped-def]
        key = key_fn(*args, **kwargs)
        ok = await limiter.take(key, capacity, refill_per_sec, cost)
        if not ok:
            from core.errors.errors import RateLimitError

            raise RateLimitError(
                f"rate limit exceeded for {key}",
                context={"key": key, "capacity": capacity, "refill_per_sec": refill_per_sec},
            )

    return decorator
