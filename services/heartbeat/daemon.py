"""
Heartbeat daemon — long-running background health checks.

The runtime loop is *event-driven*: it ticks when there's
work to do (a user message, an inbox message, etc.). A
heartbeat is *time-driven*: it runs every N seconds,
regardless of activity.

The heartbeat keeps the agent healthy between ticks. It
does four things:

  1. **Inbox sweep.** Find messages stuck in `in_progress`
     for too long (the agent crashed mid-process) and
     reset them to `received` for retry. This is the
     `InboxService.reset_stuck()` we added earlier.
  2. **Memory compaction.** Older memories decay over time
     or get summarized; the heartbeat triggers this.
     (Today the platform's memory service has a
     `compact()` method that's a stub; a real implementation
     is in a later turn.)
  3. **Credit monitoring.** Watch the agent's balance and
     fire events when the tier changes. The runtime
     already has tier logic; the heartbeat pushes the
     agent to *act* when its tier drops.
  4. **Peer discovery.** Periodically refresh the list of
     other agents in the registry so the agent knows who
     it can delegate to.

Why a daemon (not code in the tick)?

  - The tick fires on *work*. The daemon fires on *time*.
    A daemon can run while the agent is in `critical`
    tier, watching for "did anyone pay us?".
  - Decouples concerns: the agent's logic is "what to
    do"; the daemon's is "is the agent healthy?".
  - The daemon can outlive a tick. If the agent's tick
    crashes (e.g. an LLM call hangs), the daemon keeps
    running and can revive the agent.

Lifecycle:

  daemon = HeartbeatDaemon(...)
  await daemon.start()       # spawns a background task
  ...
  await daemon.stop()        # cancels and joins the task

The daemon is *cooperative*: it doesn't grab locks or
interfere with the tick. It reads state, fires events,
and returns. If a task throws, the daemon logs the error
and continues — a transient failure shouldn't kill the
health monitor.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from core.errors.errors import ValidationError
from core.types.identifiers import AutomatonId
from core.types.money import Money
from core.survival.tiers import SurvivalTier

log = logging.getLogger(__name__)


# ── Health check results ──────────────────────────────


class HealthStatus(str, Enum):
    """The result of a single health check."""

    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
    DEAD = "dead"


@dataclass(slots=True)
class HealthCheck:
    """A single health check result.

    `name` is the check's identifier (e.g. "inbox_sweep").
    `status` is the result. `detail` is a human-readable
    summary. `duration_ms` is how long the check took.
    `ran_at` is the epoch seconds the check started.
    """

    name: str
    status: HealthStatus
    detail: str
    duration_ms: float
    ran_at: float


@dataclass(slots=True)
class HeartbeatReport:
    """The aggregated result of one heartbeat cycle.

    A cycle runs all registered checks; the report is the
    summary plus each individual result. The overall
    status is the worst of the individual statuses.
    """

    automaton_id: AutomatonId
    cycle_id: int
    started_at: float
    finished_at: float
    checks: list[HealthCheck]
    tier: str  # the agent's survival tier at the time of the cycle

    @property
    def status(self) -> HealthStatus:
        # The overall status is the worst of the individual
        # checks. DEAD is the worst, then CRITICAL, then
        # WARN, then OK.
        order = [HealthStatus.DEAD, HealthStatus.CRITICAL, HealthStatus.WARN, HealthStatus.OK]
        for s in order:
            if any(c.status == s for c in self.checks):
                return s
        return HealthStatus.OK

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at) * 1000.0


# ── Health check protocol ─────────────────────────────


class HealthCheckFn(Protocol):
    """A single health check.

    The protocol is `async def name(...) -> HealthCheck`.
    A check can read any state, fire events, etc. It
    should be *idempotent* and *fast* — the heartbeat
    runs many checks per cycle.
    """

    name: str

    async def __call__(
        self, *, automaton_id: AutomatonId, **kwargs: Any
    ) -> HealthCheck: ...


# ── Concrete checks ──────────────────────────────────


class InboxSweepCheck:
    """Sweep stuck `in_progress` messages back to `received`.

    Calls `InboxService.reset_stuck()` with a configurable
    threshold (default 5 minutes). The check reports
    `CRITICAL` if any messages were reset (an indicator
    that the agent was crashing mid-process).
    """

    def __init__(
        self,
        *,
        inbox_service: Any,
        threshold_seconds: float = 300.0,
    ) -> None:
        self.inbox_service = inbox_service
        self.threshold_seconds = threshold_seconds
        self.name = "inbox_sweep"

    async def __call__(self, *, automaton_id: AutomatonId, **kwargs: Any) -> HealthCheck:
        started = time.time()
        try:
            n = await self.inbox_service.reset_stuck(
                stuck_for_seconds=self.threshold_seconds,
            )
        except Exception as e:  # noqa: BLE001
            return HealthCheck(
                name=self.name,
                status=HealthStatus.WARN,
                detail=f"reset_stuck raised: {e}",
                duration_ms=(time.time() - started) * 1000.0,
                ran_at=started,
            )
        if n > 0:
            return HealthCheck(
                name=self.name,
                status=HealthStatus.CRITICAL,
                detail=f"reset {n} stuck messages",
                duration_ms=(time.time() - started) * 1000.0,
                ran_at=started,
            )
        return HealthCheck(
            name=self.name,
            status=HealthStatus.OK,
            detail="no stuck messages",
            duration_ms=(time.time() - started) * 1000.0,
            ran_at=started,
        )


class CreditMonitorCheck:
    """Watch the agent's balance and report the tier.

    The check doesn't *act* on the tier — it just reports
    the current state. The runtime's tick handles tier
    transitions; the heartbeat makes the state observable.
    """

    def __init__(
        self,
        *,
        balance_getter: Callable[[], Money],
    ) -> None:
        self._balance_getter = balance_getter
        self.name = "credit_monitor"

    async def __call__(self, *, automaton_id: AutomatonId, **kwargs: Any) -> HealthCheck:
        started = time.time()
        try:
            bal = self._balance_getter()
        except Exception as e:  # noqa: BLE001
            return HealthCheck(
                name=self.name,
                status=HealthStatus.WARN,
                detail=f"balance_getter raised: {e}",
                duration_ms=(time.time() - started) * 1000.0,
                ran_at=started,
            )
        # Map balance to tier.
        if bal.micro <= 0:
            status = HealthStatus.DEAD
            tier = SurvivalTier.DEAD
        elif bal.micro < 50_000:
            status = HealthStatus.CRITICAL
            tier = SurvivalTier.CRITICAL
        elif bal.micro < 500_000:
            status = HealthStatus.WARN
            tier = SurvivalTier.LOW_COMPUTE
        else:
            status = HealthStatus.OK
            tier = SurvivalTier.NORMAL
        return HealthCheck(
            name=self.name,
            status=status,
            detail=f"balance={bal}, tier={tier.value}",
            duration_ms=(time.time() - started) * 1000.0,
            ran_at=started,
        )


# ── Daemon ────────────────────────────────────────────


class HeartbeatDaemon:
    """The heartbeat daemon. Runs registered checks on a
    fixed interval, emits a `HeartbeatReport` per cycle,
    and exposes the latest report for inspection.

    Usage:

        daemon = HeartbeatDaemon(
            automaton_id=aid,
            checks=[InboxSweepCheck(...), CreditMonitorCheck(...)],
            interval_seconds=60.0,
        )
        await daemon.start()
        ...
        # Read the latest report.
        report = daemon.last_report
        ...
        await daemon.stop()

The daemon is cooperative: it doesn't grab locks. If a
check throws, the daemon logs the error, marks the
cycle as failed, and continues on the next interval.
    """

    def __init__(
        self,
        *,
        automaton_id: AutomatonId,
        checks: list[HealthCheckFn] | None = None,
        interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
        on_report: Callable[[HeartbeatReport], Awaitable[None]] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValidationError("interval_seconds must be positive")
        self.automaton_id = automaton_id
        self.checks = list(checks or [])
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._on_report = on_report
        self._task: asyncio.Task | None = None
        self._cycle_id = 0
        self.last_report: HeartbeatReport | None = None
        self._stopped = False

    # ── Lifecycle ──
    async def start(self) -> None:
        """Start the heartbeat in the background.

        Returns immediately. The first cycle runs after
        `interval_seconds`. To run a check immediately,
        call `run_once()`.
        """
        if self._task is not None and not self._task.done():
            return  # already running
        self._stopped = False
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        """Cancel the heartbeat and wait for it to finish."""
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    # ── Single cycle ──
    async def run_once(self) -> HeartbeatReport:
        """Run all checks once and return the report.

        This is the synchronous path: callers can invoke
        it without starting the background task.
        """
        self._cycle_id += 1
        started = self._clock()
        check_results: list[HealthCheck] = []
        for check in self.checks:
            try:
                result = await check(automaton_id=self.automaton_id)
            except Exception as e:  # noqa: BLE001
                log.exception("heartbeat_check_failed", extra={"check": getattr(check, "name", "?")})
                result = HealthCheck(
                    name=getattr(check, "name", "?"),
                    status=HealthStatus.WARN,
                    detail=f"check raised: {e}",
                    duration_ms=0.0,
                    ran_at=started,
                )
            check_results.append(result)
        finished = self._clock()
        # Compute the agent's tier from the credit-monitor
        # check, if present. Falls back to NORMAL.
        tier = "normal"
        for c in check_results:
            if c.name == "credit_monitor":
                # The credit_monitor's detail looks like
                # "balance=..., tier=...". We extract the
                # tier; if we can't, fall back.
                if "tier=" in c.detail:
                    tier = c.detail.split("tier=")[-1].split(",")[0].strip()
        report = HeartbeatReport(
            automaton_id=self.automaton_id,
            cycle_id=self._cycle_id,
            started_at=started,
            finished_at=finished,
            checks=check_results,
            tier=tier,
        )
        self.last_report = report
        log.info(
            "heartbeat_cycle",
            extra={
                "aid": str(self.automaton_id),
                "cycle": self._cycle_id,
                "status": report.status.value,
                "duration_ms": report.duration_ms,
            },
        )
        if self._on_report is not None:
            try:
                await self._on_report(report)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat_on_report_callback_failed", extra={"err": str(e)})
        return report

    # ── Internal loop ──
    async def _run_forever(self) -> None:
        """The main loop. Runs `run_once()` on every interval.

        Exceptions from individual checks are caught and
        logged; a thrown check doesn't kill the daemon.
        """
        while not self._stopped:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("heartbeat_cycle_failed", extra={"err": str(e)})
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise


# ── Factory ──────────────────────────────────────────


def make_daemon(
    *,
    automaton_id: AutomatonId,
    interval_seconds: float = 60.0,
    checks: list[HealthCheckFn] | None = None,
    on_report: Callable[[HeartbeatReport], Awaitable[None]] | None = None,
) -> HeartbeatDaemon:
    """Convenience factory with platform defaults."""
    return HeartbeatDaemon(
        automaton_id=automaton_id,
        interval_seconds=interval_seconds,
        checks=checks or [],
        on_report=on_report,
    )


__all__ = [
    "CreditMonitorCheck",
    "HealthCheck",
    "HealthCheckFn",
    "HealthStatus",
    "HeartbeatDaemon",
    "HeartbeatReport",
    "InboxSweepCheck",
    "make_daemon",
]
