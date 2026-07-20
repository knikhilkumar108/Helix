"""
Health check aggregator. Every service registers a check; the aggregator
exposes a single report.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core.types.automaton import ComponentHealth, HealthReport


Check = Callable[[], Awaitable[ComponentHealth]]


@dataclass(slots=True)
class HealthRegistry:
    checks: dict[str, Check] = field(default_factory=dict)

    def register(self, name: str, check: Check) -> None:
        self.checks[name] = check

    async def report(self, *, timeout: float = 2.0) -> HealthReport:
        results = await asyncio.gather(
            *(self._run(name, c, timeout) for name, c in self.checks.items()),
            return_exceptions=True,
        )
        components: dict[str, ComponentHealth] = {}
        for (name, _), res in zip(self.checks.items(), results):
            if isinstance(res, BaseException):
                components[name] = ComponentHealth(
                    status="down", message=f"{type(res).__name__}: {res}"
                )
            else:
                components[name] = res
        overall = "healthy"
        for c in components.values():
            if c.status == "down":
                overall = "unhealthy"
                break
            if c.status == "degraded" and overall == "healthy":
                overall = "degraded"
        return HealthReport(
            status=overall,  # type: ignore[arg-type]
            components=components,
            checked_at=time.time(),  # type: ignore[arg-type]
        )

    @staticmethod
    async def _run(name: str, check: Check, timeout: float) -> ComponentHealth:
        try:
            return await asyncio.wait_for(check(), timeout=timeout)
        except asyncio.TimeoutError:
            return ComponentHealth(status="down", message="timeout")
        except Exception as e:  # noqa: BLE001
            return ComponentHealth(status="down", message=f"{type(e).__name__}: {e}")


# Default singleton; services can create their own.
HEALTH = HealthRegistry()


def ping_dependency(name: str, fn: Check) -> None:
    HEALTH.register(name, fn)
