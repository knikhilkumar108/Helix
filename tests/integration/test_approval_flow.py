"""
End-to-end test: the runtime's policy pipeline returns
`require_approval` for a money.transfer action, the loop parks the
action with the ApprovalGate, the operator approves it from outside
the loop, and the action runs.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from core.policy.policy import (
    Constitution,
    ConstitutionEvaluator,
    PolicyDecision,
)
from core.types.automaton import PolicyVerdict
from core.survival.tiers import SurvivalTier
from core.types.automaton import Action
from core.types.identifiers import AutomatonId, new_automaton_id
from core.types.money import Money
from runtime.loop.budget import BudgetConfig, BudgetController
from runtime.loop.builtins import register_builtins
from runtime.loop.checkpoint import InMemoryCheckpointStore
from runtime.loop.context import InMemoryLoopContext
from runtime.loop.loop import AutomatonLoop, LoopConfig
from runtime.loop.planner import HeuristicPlanner, PlannerConfig
from runtime.loop.treasury import InMemoryTreasury
from services.approvals.approvals import ApprovalGate, ApprovalState
from services.router.router import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelSpec,
)


class _LLM:
    """Scripted LLM that always asks for a money.transfer."""

    def __init__(self) -> None:
        self.spec = ModelSpec(
            name="scripted", provider="test", context_window=128_000,
            cost_per_1k_input_micro=0, cost_per_1k_output_micro=0,
            capabilities=frozenset({"chat"}), quality=0.7, avg_latency_ms=10,
        )
        self.calls = 0

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        text = json.dumps({
            "summary": "sending money",
            "queries": [],
            "next_action": {
                "tool": "money.transfer",
                "arguments": {"to": "0xabc", "amount_micro": 1000000, "currency": "USDC"},
            },
            "confidence": 0.9,
            "strategy": "transfer",
        })
        return CompletionResponse(
            text=text, model="x", provider="x",
            input_tokens=10, output_tokens=len(text) // 4,
            cost_micro=0, latency_ms=1.0,
        )


class _ConstitutionRequiringApproval:
    """A custom policy pipeline that returns require_approval for any
    money.transfer, and allow for everything else."""

    async def __call__(self, action: Action) -> PolicyDecision:
        if action.tool_name == "money.transfer":
            return PolicyDecision(
                verdict=PolicyVerdict.REQUIRE_APPROVAL,
                reason="sending money requires explicit human consent",
                evaluated_at=datetime.now(tz=timezone.utc),
                evaluator="constitution@v1",
                citations=["constitution:law:8"],
            )
        return PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            reason="allowed",
            evaluated_at=datetime.now(tz=timezone.utc),
            evaluator="constitution@v1",
            citations=["constitution:v1"],
        )


@pytest.mark.asyncio
async def test_require_approval_blocks_until_human_decides():
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="test", automaton_id=aid)
    treasury = InMemoryTreasury(aid, initial=Money.from_major("1.00"))
    tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
    register_builtins(tools, workspace=None)
    fake = _LLM()
    reasoner = __import__("services.router.llm_reasoner", fromlist=["LLMReasoner"]).LLMReasoner(
        LLMRouter([fake]),
        automaton_id=str(aid),
        balance_getter=treasury.balance,
        tier_getter=lambda: SurvivalTier.NORMAL,
        tools_getter=lambda: [t.name for t in tools.list()],
    )
    gate = ApprovalGate()

    budget = BudgetController(
        BudgetConfig(
            reserve_floor=Money.zero(),
            per_tick_max=Money.from_major("1.00"),
            per_day_max=Money.from_major("100.00"),
        ),
        balance_getter=treasury.balance,
    )
    loop = AutomatonLoop(
        ctx=ctx, reasoner=reasoner, planner=HeuristicPlanner(PlannerConfig()),
        tools=tools, treasury=treasury, budget=budget,
        checkpoints=InMemoryCheckpointStore(),
        config=LoopConfig(
            max_runtime_seconds=3.0, sleep_min_seconds=0.05, sleep_max_seconds=0.1,
        ),
        policy_pipeline=_ConstitutionRequiringApproval(),
        approval_gate=gate,
    )

    # Start the loop in a background task.
    runner = asyncio.create_task(loop.run())

    # Wait for the gate to have a pending approval.
    pending: list = []
    for _ in range(40):
        pending = await gate.list_pending()
        if pending:
            break
        await asyncio.sleep(0.05)
    assert pending, "no pending approval was ever submitted"
    assert pending[0].action.tool_name == "money.transfer"
    # LLM was called at least once.
    assert fake.calls >= 1
    # The action was *not* executed yet — the tool was not invoked.
    assert all(
        getattr(a, "result", None) is None or getattr(a, "error", None) is not None
        for a in ctx.actions
    )

    # Approve from outside the loop.
    await gate.decide(
        pending[0].id, verdict=ApprovalState.APPROVED, decided_by="alice", reason="ok"
    )

    # Give the loop a moment to process the decision and attempt the
    # tool execution (which will fail with "unknown_tool" because
    # money.transfer isn't in the builtin registry — that's expected).
    await asyncio.sleep(0.3)

    # Stop the loop.
    loop.request_stop()
    await asyncio.wait_for(runner, timeout=5.0)
    # At least one approval was submitted and decided.
    assert any(e[0] == "approval_submitted" for e in ctx.events)
    assert any(e[0] == "approval_decided" for e in ctx.events)


@pytest.mark.asyncio
async def test_rejection_skips_execution():
    """If the operator rejects, the action is recorded but not executed."""
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="test", automaton_id=aid)
    treasury = InMemoryTreasury(aid, initial=Money.from_major("1.00"))
    tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
    register_builtins(tools, workspace=None)
    fake = _LLM()
    reasoner = __import__("services.router.llm_reasoner", fromlist=["LLMReasoner"]).LLMReasoner(
        LLMRouter([fake]),
        automaton_id=str(aid),
        balance_getter=treasury.balance,
        tier_getter=lambda: SurvivalTier.NORMAL,
        tools_getter=lambda: [t.name for t in tools.list()],
    )
    gate = ApprovalGate()
    budget = BudgetController(
        BudgetConfig(
            reserve_floor=Money.zero(),
            per_tick_max=Money.from_major("1.00"),
            per_day_max=Money.from_major("100.00"),
        ),
        balance_getter=treasury.balance,
    )
    loop = AutomatonLoop(
        ctx=ctx, reasoner=reasoner, planner=HeuristicPlanner(PlannerConfig()),
        tools=tools, treasury=treasury, budget=budget,
        checkpoints=InMemoryCheckpointStore(),
        config=LoopConfig(
            max_runtime_seconds=3.0, sleep_min_seconds=0.05, sleep_max_seconds=0.1,
        ),
        policy_pipeline=_ConstitutionRequiringApproval(),
        approval_gate=gate,
    )
    runner = asyncio.create_task(loop.run())
    pending: list = []
    for _ in range(40):
        pending = await gate.list_pending()
        if pending:
            break
        await asyncio.sleep(0.05)
    assert pending

    # Reject.
    await gate.decide(
        pending[0].id, verdict=ApprovalState.REJECTED, decided_by="alice", reason="no"
    )
    # Give the loop a moment to process the decision.
    await asyncio.sleep(0.3)
    loop.request_stop()
    await asyncio.wait_for(runner, timeout=5.0)

    money_actions = [a for a in ctx.actions if getattr(a, "tool_name", None) == "money.transfer"]
    assert money_actions
    # The action was rejected — its `error` field records the rejection.
    assert any("rejected" in (a.error or "") for a in money_actions)
