"""Tests for the heartbeat daemon."""
from __future__ import annotations

import asyncio

import pytest

from core.errors.errors import ValidationError
from core.types.identifiers import new_automaton_id
from core.types.money import Money
from services.heartbeat import (
    CreditMonitorCheck,
    HealthCheck,
    HealthStatus,
    HeartbeatDaemon,
    HeartbeatReport,
    InboxSweepCheck,
    make_daemon,
)


# ── Test helpers ─────────────────────────────────────


class _FakeInboxService:
    """A minimal stand-in for `InboxService` that records
    calls to `reset_stuck()`."""

    def __init__(self, reset_count: int = 0) -> None:
        self._reset_count = reset_count
        self.calls: list[float] = []

    async def reset_stuck(self, *, stuck_for_seconds: float) -> int:
        self.calls.append(stuck_for_seconds)
        return self._reset_count


# ── HealthCheck basics ─────────────────────────────


def test_health_check_construction():
    c = HealthCheck(
        name="x",
        status=HealthStatus.OK,
        detail="fine",
        duration_ms=1.0,
        ran_at=0.0,
    )
    assert c.name == "x"
    assert c.status == HealthStatus.OK


def test_heartbeat_report_status_worst_case():
    aid = new_automaton_id()
    r = HeartbeatReport(
        automaton_id=aid,
        cycle_id=1,
        started_at=0.0,
        finished_at=0.0,
        checks=[
            HealthCheck(name="a", status=HealthStatus.OK, detail="", duration_ms=0.0, ran_at=0.0),
            HealthCheck(name="b", status=HealthStatus.CRITICAL, detail="", duration_ms=0.0, ran_at=0.0),
        ],
        tier="normal",
    )
    # Worst is CRITICAL.
    assert r.status == HealthStatus.CRITICAL


def test_heartbeat_report_status_dead_overrides_critical():
    aid = new_automaton_id()
    r = HeartbeatReport(
        automaton_id=aid,
        cycle_id=1,
        started_at=0.0,
        finished_at=0.0,
        checks=[
            HealthCheck(name="a", status=HealthStatus.CRITICAL, detail="", duration_ms=0.0, ran_at=0.0),
            HealthCheck(name="b", status=HealthStatus.DEAD, detail="", duration_ms=0.0, ran_at=0.0),
        ],
        tier="dead",
    )
    assert r.status == HealthStatus.DEAD


def test_heartbeat_report_status_all_ok():
    aid = new_automaton_id()
    r = HeartbeatReport(
        automaton_id=aid,
        cycle_id=1,
        started_at=0.0,
        finished_at=0.0,
        checks=[
            HealthCheck(name="a", status=HealthStatus.OK, detail="", duration_ms=0.0, ran_at=0.0),
        ],
        tier="normal",
    )
    assert r.status == HealthStatus.OK


# ── InboxSweepCheck ─────────────────────────────────


@pytest.mark.asyncio
async def test_inbox_sweep_ok_when_no_stuck():
    inbox = _FakeInboxService(reset_count=0)
    c = InboxSweepCheck(inbox_service=inbox, threshold_seconds=60.0)
    result = await c(automaton_id=new_automaton_id())
    assert result.status == HealthStatus.OK
    assert "no stuck messages" in result.detail
    assert inbox.calls == [60.0]


@pytest.mark.asyncio
async def test_inbox_sweep_critical_when_stuck_found():
    inbox = _FakeInboxService(reset_count=3)
    c = InboxSweepCheck(inbox_service=inbox)
    result = await c(automaton_id=new_automaton_id())
    assert result.status == HealthStatus.CRITICAL
    assert "reset 3 stuck messages" in result.detail


@pytest.mark.asyncio
async def test_inbox_sweep_warn_on_exception():
    class _ExplodingInbox:
        async def reset_stuck(self, *, stuck_for_seconds):
            raise RuntimeError("boom")
    c = InboxSweepCheck(inbox_service=_ExplodingInbox())
    result = await c(automaton_id=new_automaton_id())
    assert result.status == HealthStatus.WARN
    assert "boom" in result.detail


# ── CreditMonitorCheck ──────────────────────────────


@pytest.mark.asyncio
async def test_credit_monitor_ok_at_high_balance():
    c = CreditMonitorCheck(balance_getter=lambda: Money.from_major("5.00"))
    result = await c(automaton_id=new_automaton_id())
    assert result.status == HealthStatus.OK
    assert "tier=normal" in result.detail


@pytest.mark.asyncio
async def test_credit_monitor_warn_at_low_compute():
    c = CreditMonitorCheck(balance_getter=lambda: Money.from_major("0.20"))
    result = await c(automaton_id=new_automaton_id())
    assert result.status == HealthStatus.WARN
    assert "tier=low_compute" in result.detail


@pytest.mark.asyncio
async def test_credit_monitor_critical_at_low_balance():
    c = CreditMonitorCheck(balance_getter=lambda: Money.from_major("0.01"))
    result = await c(automaton_id=new_automaton_id())
    assert result.status == HealthStatus.CRITICAL
    assert "tier=critical" in result.detail


@pytest.mark.asyncio
async def test_credit_monitor_dead_at_zero():
    c = CreditMonitorCheck(balance_getter=lambda: Money.zero())
    result = await c(automaton_id=new_automaton_id())
    assert result.status == HealthStatus.DEAD
    assert "tier=dead" in result.detail


@pytest.mark.asyncio
async def test_credit_monitor_warn_on_exception():
    def boom():
        raise RuntimeError("nope")
    c = CreditMonitorCheck(balance_getter=boom)
    result = await c(automaton_id=new_automaton_id())
    assert result.status == HealthStatus.WARN


# ── Daemon lifecycle ────────────────────────────────


def test_daemon_validates_interval():
    aid = new_automaton_id()
    with pytest.raises(ValidationError):
        HeartbeatDaemon(automaton_id=aid, interval_seconds=0)
    with pytest.raises(ValidationError):
        HeartbeatDaemon(automaton_id=aid, interval_seconds=-1.0)


def test_daemon_starts_with_no_checks():
    aid = new_automaton_id()
    d = HeartbeatDaemon(automaton_id=aid, interval_seconds=60.0)
    assert d.last_report is None
    assert d._cycle_id == 0


@pytest.mark.asyncio
async def test_daemon_run_once_no_checks():
    aid = new_automaton_id()
    d = HeartbeatDaemon(automaton_id=aid, interval_seconds=60.0)
    report = await d.run_once()
    assert report.automaton_id == aid
    assert report.cycle_id == 1
    assert report.checks == []
    assert report.status == HealthStatus.OK
    assert d.last_report is report


@pytest.mark.asyncio
async def test_daemon_run_once_with_checks():
    aid = new_automaton_id()
    inbox = _FakeInboxService(reset_count=0)
    d = HeartbeatDaemon(
        automaton_id=aid,
        interval_seconds=60.0,
        checks=[
            InboxSweepCheck(inbox_service=inbox),
            CreditMonitorCheck(balance_getter=lambda: Money.from_major("5.00")),
        ],
    )
    report = await d.run_once()
    assert len(report.checks) == 2
    assert report.status == HealthStatus.OK
    assert report.tier == "normal"


@pytest.mark.asyncio
async def test_daemon_handles_check_exception():
    aid = new_automaton_id()

    class _ExplodingCheck:
        name = "boom"

        async def __call__(self, *, automaton_id, **kwargs):
            raise RuntimeError("oops")

    d = HeartbeatDaemon(
        automaton_id=aid,
        interval_seconds=60.0,
        checks=[_ExplodingCheck()],
    )
    report = await d.run_once()
    assert len(report.checks) == 1
    assert report.checks[0].status == HealthStatus.WARN
    assert "oops" in report.checks[0].detail


@pytest.mark.asyncio
async def test_daemon_credit_monitor_tier_propagates():
    aid = new_automaton_id()
    d = HeartbeatDaemon(
        automaton_id=aid,
        interval_seconds=60.0,
        checks=[
            CreditMonitorCheck(balance_getter=lambda: Money.zero()),
        ],
    )
    report = await d.run_once()
    assert report.tier == "dead"
    assert report.status == HealthStatus.DEAD


@pytest.mark.asyncio
async def test_daemon_start_stop_lifecycle():
    aid = new_automaton_id()
    d = HeartbeatDaemon(automaton_id=aid, interval_seconds=0.05)
    await d.start()
    assert d._task is not None
    # Give the loop a chance to run at least one cycle.
    await asyncio.sleep(0.15)
    assert d.last_report is not None
    await d.stop()
    assert d._task is None


@pytest.mark.asyncio
async def test_daemon_on_report_callback_fires():
    aid = new_automaton_id()
    reports: list[HeartbeatReport] = []

    async def cb(report: HeartbeatReport) -> None:
        reports.append(report)

    d = HeartbeatDaemon(
        automaton_id=aid,
        interval_seconds=60.0,
        on_report=cb,
    )
    await d.run_once()
    assert len(reports) == 1


@pytest.mark.asyncio
async def test_daemon_on_report_callback_exception_does_not_kill_daemon():
    aid = new_automaton_id()

    async def cb(report):
        raise RuntimeError("callback failed")

    d = HeartbeatDaemon(
        automaton_id=aid,
        interval_seconds=60.0,
        on_report=cb,
    )
    # The exception is swallowed.
    report = await d.run_once()
    assert report is not None


# ── make_daemon factory ────────────────────────────


def test_make_daemon_defaults():
    aid = new_automaton_id()
    d = make_daemon(automaton_id=aid)
    assert d.interval_seconds == 60.0
    assert d.checks == []


def test_make_daemon_with_checks():
    aid = new_automaton_id()
    inbox = _FakeInboxService()
    d = make_daemon(
        automaton_id=aid,
        interval_seconds=10.0,
        checks=[InboxSweepCheck(inbox_service=inbox)],
    )
    assert d.interval_seconds == 10.0
    assert len(d.checks) == 1
