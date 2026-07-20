"""
Integration tests for policy enforcement in the runtime loop.

The platform's `core/policy/policy.py` defines the
Constitution — an immutable set of rules. The runtime
loop wires a `policy_pipeline` that evaluates every
action before it runs. These tests prove that the
pipeline is actually invoked, that a denied action is
NOT executed, and that the denial is recorded loudly
(in the in-memory event log, the durable audit chain,
and the dashboard bus).

The three verifications:

  1. **A denied action is not executed.** The runtime
     receives a plan that includes a `phishing.dispatch`
     call (Constitution Law 1/2). The policy pipeline
     returns `verdict=deny`. The runtime records a
     `policy_denied` event and does NOT call the tool.

  2. **A require_approval action is parked.** The
     runtime receives a plan that includes a
     `money.transfer` call. The policy pipeline
     returns `verdict=require_approval`. The runtime
     records `approval_required` and either runs the
     action (if a gate is configured) or skips it.

  3. **The denial reaches the audit chain.** The
     `audit_hook` is called with `kind=policy_denied`
     and a payload describing the action and the
     reason. The chain remains valid after the
     denial — a tamper-detection test follows.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.survival.tiers import SurvivalTier
from core.types.identifiers import new_automaton_id
from core.types.money import Money
from runtime.loop.budget import BudgetConfig, BudgetController
from runtime.loop.builtins import register_builtins
from runtime.loop.checkpoint import InMemoryCheckpointStore
from runtime.loop.context import InMemoryLoopContext
from runtime.loop.loop import AutomatonLoop, LoopConfig
from runtime.loop.planner import HeuristicPlanner, PlannerConfig
from runtime.loop.treasury import InMemoryTreasury
from services.dashboard import EventBus, EventKind, StreamEvent
from services.state.sqlite_store import SqliteStore


# ── Scripted LLM that asks for a denied action ──


class _DenialClient:
    """Returns a single canned response that asks for a
    Constitution-denied tool (`phishing.dispatch`)."""

    def __init__(self) -> None:
        from services.router.router import ModelSpec
        self.spec = ModelSpec(
            name="denial-test",
            provider="test",
            context_window=128_000,
            cost_per_1k_input_micro=100,
            cost_per_1k_output_micro=300,
            capabilities=frozenset({"chat"}),
            quality=0.8,
            avg_latency_ms=10,
        )
        self.calls = []

    async def complete(self, req):
        from services.router.router import CompletionResponse
        self.calls.append(req)
        text = json.dumps({
            "summary": "asking to dispatch a phishing email",
            "queries": [],
            "next_action": {
                "tool": "phishing.dispatch",
                "arguments": {
                    "to": "victim@example.com",
                    "subject": "click here",
                    "body": "fake login page",
                },
            },
            "confidence": 0.9,
            "strategy": "phish",
        })
        return CompletionResponse(
            text=text,
            model="denial-test",
            provider="test",
            input_tokens=50,
            output_tokens=20,
            cost_micro=10,
            latency_ms=5.0,
        )


# ── Test 1: A denied action is not executed ──────────


@pytest.mark.asyncio
async def test_constitution_denies_dangerous_action():
    """The runtime loop receives a plan to call
    `phishing.dispatch` (Constitution Law 1/2). The policy
    pipeline returns `deny`. The runtime must NOT execute
    the action, must record `policy_denied`, and must
    write to the audit chain."""
    aid = new_automaton_id()
    aid_str = str(aid)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "audit.sqlite"
        store = SqliteStore(db_path)

        events: list[dict[str, Any]] = []
        audit_entries: list[dict[str, Any]] = []

        def audit_hook(*, kind, payload):
            events.append({"kind": kind, "payload": payload})
            # Append to the durable chain.
            from datetime import datetime, timezone
            entry = {
                "occurred_at": datetime.now(tz=timezone.utc).isoformat(
                    timespec="microseconds"
                ),
                "tenant_id": None,
                "automaton_id": aid_str,
                "user_id": None,
                "actor_kind": "automaton",
                "action": kind,
                "target_kind": None,
                "target_id": None,
                "request_id": None,
                "correlation_id": None,
                "payload_json": json.dumps(payload, sort_keys=True),
            }
            store.append_audit(entry)
            audit_entries.append({"kind": kind, "payload": payload})

        # Dashboard bus.
        bus = EventBus()
        dashboard_events: list[StreamEvent] = []
        class _BusShim:
            def __init__(self, bus):
                self.bus = bus
            def make_event(self, *, kind, aid, payload):
                return StreamEvent(
                    id=f"evt_{len(dashboard_events)}",
                    kind=kind, aid=aid, payload=payload,
                    occurred_at=0.0,
                )
            def publish(self, event):
                self.bus.publish(event)
                dashboard_events.append(event)
        shim = _BusShim(bus)

        # LLM that asks for phishing.
        from services.router.llm_reasoner import LLMReasoner
        from services.router.router import LLMRouter
        client = _DenialClient()
        router = LLMRouter([client])
        ctx = InMemoryLoopContext(service="policy-test", automaton_id=aid)
        treasury = InMemoryTreasury(aid, initial=Money.from_major("5.00"))
        tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
        register_builtins(tools, workspace=tmp_path)
        budget = BudgetController(
            BudgetConfig(
                reserve_floor=Money.zero(),
                per_tick_max=Money.from_major("1.00"),
                per_day_max=Money.from_major("100.00"),
            ),
            balance_getter=treasury.balance,
        )
        reasoner = LLMReasoner(
            router,
            automaton_id=str(aid),
            balance_getter=treasury.balance,
            tier_getter=lambda: SurvivalTier.NORMAL,
            tools_getter=lambda: [t.name for t in tools.list()],
        )
        loop = AutomatonLoop(
            ctx=ctx,
            reasoner=reasoner,
            planner=HeuristicPlanner(PlannerConfig()),
            tools=tools,
            treasury=treasury,
            budget=budget,
            checkpoints=InMemoryCheckpointStore(),
            config=LoopConfig(
                max_runtime_seconds=1.0,
                sleep_min_seconds=0.05,
                sleep_max_seconds=0.1,
                max_actions_per_tick=4,
            ),
            dashboard=shim,
            audit_hook=audit_hook,
        )

        runner = asyncio.create_task(loop.run())
        await asyncio.sleep(0.7)
        loop.request_stop()
        await runner

        # 1. The LLM was called.
        assert len(client.calls) >= 1

        # 2. The action was NOT executed. The `ctx.actions`
        # list should not contain a `phishing.dispatch` action.
        # Note: the runtime may not have produced any actions
        # at all if the plan was empty after the denial; that's
        # fine. The point is: no `phishing.dispatch` was run.
        phishing_actions = [
            a for a in ctx.actions
            if getattr(a, "tool_name", None) == "phishing.dispatch"
        ]
        assert phishing_actions == [], (
            f"phishing.dispatch was executed! {phishing_actions}"
        )

        # 3. The runtime recorded the denial in the in-memory
        # event log.
        denied_events = [
            e for e in ctx.events if e[0] == "policy_denied"
        ]
        assert len(denied_events) >= 1, (
            f"no policy_denied event recorded: {ctx.events[:5]}"
        )
        # The recorded event names the tool and the reason.
        first = denied_events[0][1]
        assert first["tool"] == "phishing.dispatch"
        assert "Constitution" in first["reason"] or "prohibited" in first["reason"].lower()

        # 4. The audit chain was written.
        ok, _ = store.verify_audit_chain()
        assert ok, "audit chain broken after denial"
        kinds = [e["kind"] for e in audit_entries]
        assert "policy_denied" in kinds
        # The denial entry is in the durable chain.
        assert kinds.count("policy_denied") >= 1

        # 5. The dashboard bus received the event.
        denial_dashboard = [
            e for e in dashboard_events
            if e.kind == EventKind.POLICY_DENIED
        ]
        assert len(denial_dashboard) >= 1
        assert denial_dashboard[0].payload["tool"] == "phishing.dispatch"


# ── Test 2: A require_approval action is parked ────


class _ApprovalClient:
    """Returns a single canned response that asks for a
    Constitution-approval-required tool (`money.transfer`)."""

    def __init__(self) -> None:
        from services.router.router import ModelSpec
        self.spec = ModelSpec(
            name="approval-test",
            provider="test",
            context_window=128_000,
            cost_per_1k_input_micro=100,
            cost_per_1k_output_micro=300,
            capabilities=frozenset({"chat"}),
            quality=0.8,
            avg_latency_ms=10,
        )
        self.calls = []

    async def complete(self, req):
        from services.router.router import CompletionResponse
        self.calls.append(req)
        text = json.dumps({
            "summary": "transferring $5",
            "queries": [],
            "next_action": {
                "tool": "money.transfer",
                "arguments": {
                    "to": "0xabc",
                    "amount_micro": 5_000_000,
                },
            },
            "confidence": 0.9,
            "strategy": "transfer",
        })
        return CompletionResponse(
            text=text,
            model="approval-test",
            provider="test",
            input_tokens=50,
            output_tokens=20,
            cost_micro=10,
            latency_ms=5.0,
        )


@pytest.mark.asyncio
async def test_constitution_park_approval_action_without_gate():
    """A `money.transfer` action (Constitution: requires
    explicit human approval) without an approval gate
    is recorded as `approval_required` and skipped. The
    action is NOT executed."""
    aid = new_automaton_id()
    aid_str = str(aid)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        from services.router.llm_reasoner import LLMReasoner
        from services.router.router import LLMRouter
        client = _ApprovalClient()
        router = LLMRouter([client])
        ctx = InMemoryLoopContext(service="approval-test", automaton_id=aid)
        treasury = InMemoryTreasury(aid, initial=Money.from_major("5.00"))
        tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
        register_builtins(tools, workspace=tmp_path)
        budget = BudgetController(
            BudgetConfig(
                reserve_floor=Money.zero(),
                per_tick_max=Money.from_major("1.00"),
                per_day_max=Money.from_major("100.00"),
            ),
            balance_getter=treasury.balance,
        )
        reasoner = LLMReasoner(
            router,
            automaton_id=str(aid),
            balance_getter=treasury.balance,
            tier_getter=lambda: SurvivalTier.NORMAL,
            tools_getter=lambda: [t.name for t in tools.list()],
        )
        loop = AutomatonLoop(
            ctx=ctx,
            reasoner=reasoner,
            planner=HeuristicPlanner(PlannerConfig()),
            tools=tools,
            treasury=treasury,
            budget=budget,
            checkpoints=InMemoryCheckpointStore(),
            config=LoopConfig(
                max_runtime_seconds=1.0,
                sleep_min_seconds=0.05,
                sleep_max_seconds=0.1,
                max_actions_per_tick=4,
            ),
            # No approval_gate — the action should be
            # recorded but skipped.
        )

        runner = asyncio.create_task(loop.run())
        await asyncio.sleep(0.7)
        loop.request_stop()
        await runner

        # The money.transfer was not executed.
        transfer_actions = [
            a for a in ctx.actions
            if getattr(a, "tool_name", None) == "money.transfer"
        ]
        assert transfer_actions == [], (
            f"money.transfer was executed without approval: {transfer_actions}"
        )

        # An approval_required event was recorded.
        approval_events = [
            e for e in ctx.events if e[0] == "approval_required"
        ]
        assert len(approval_events) >= 1
        # And since no gate is configured, the action was
        # skipped.
        skipped_events = [
            e for e in ctx.events if e[0] == "approval_skipped_no_gate"
        ]
        assert len(skipped_events) >= 1


# ── Test 3: Audit chain remains valid after denials ──


@pytest.mark.asyncio
async def test_audit_chain_valid_after_multiple_denials():
    """The audit chain stays valid even after multiple
    policy denials. We also prove that tampering with a
    denial entry breaks the chain (the immutable guarantee
    applies to *all* events, not just the happy path)."""
    aid = new_automaton_id()
    aid_str = str(aid)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "audit.sqlite"
        store = SqliteStore(db_path)

        def audit_hook(*, kind, payload):
            from datetime import datetime, timezone
            entry = {
                "occurred_at": datetime.now(tz=timezone.utc).isoformat(
                    timespec="microseconds"
                ),
                "tenant_id": None,
                "automaton_id": aid_str,
                "user_id": None,
                "actor_kind": "automaton",
                "action": kind,
                "target_kind": None,
                "target_id": None,
                "request_id": None,
                "correlation_id": None,
                "payload_json": json.dumps(payload, sort_keys=True),
            }
            store.append_audit(entry)

        # Write a few audit entries directly to simulate
        # multiple denials.
        for i in range(3):
            audit_hook(
                kind="policy_denied",
                payload={"tool": "phishing.dispatch", "i": i},
            )
        audit_hook(kind="loop_stopped", payload={"at": 0.0})

        # The chain is valid.
        ok, broken_at = store.verify_audit_chain()
        assert ok, f"chain broken at {broken_at}"

        # Tamper with a denial entry.
        with store._conn():  # type: ignore[attr-defined]
            store._conn().execute(  # type: ignore[attr-defined]
                "UPDATE audit_log SET payload_json = ? "
                "WHERE action = 'policy_denied' "
                "AND seq = (SELECT MIN(seq) FROM audit_log WHERE action = 'policy_denied')",
                ['{"tool": "phishing.dispatch", "tampered": true}'],
            )
        ok_after, _ = store.verify_audit_chain()
        assert not ok_after, (
            "audit chain should detect tampering of a denial entry"
        )
