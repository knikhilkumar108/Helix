"""Unit tests for the runtime loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from core.types.automaton import MemoryLayer
from core.types.identifiers import AutomatonId, new_automaton_id
from core.types.money import Money
from runtime.loop.context import InMemoryLoopContext
from runtime.loop.loop import AutomatonLoop, LoopConfig
from runtime.loop.planner import HeuristicPlanner, PlannerConfig
from runtime.loop.reasoner import StubReasoner
from runtime.loop.treasury import InMemoryTreasury


def test_loop_runs_one_tick_without_errors():
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="t", automaton_id=aid)
    treasury = InMemoryTreasury(aid, initial=Money.from_major("1.00"))
    loop = AutomatonLoop(
        ctx=ctx,
        reasoner=StubReasoner(summary="hi"),
        planner=HeuristicPlanner(PlannerConfig()),
        tools=__import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry(),
        treasury=treasury,
        budget=__import__("runtime.loop.budget", fromlist=["BudgetController", "BudgetConfig"]).BudgetController(
            __import__("runtime.loop.budget", fromlist=["BudgetConfig"]).BudgetConfig(
                reserve_floor=Money.zero(),
                per_tick_max=Money.from_major("1.00"),
                per_day_max=Money.from_major("10.00"),
            ),
            balance_getter=treasury.balance,
        ),
        checkpoints=__import__("runtime.loop.checkpoint", fromlist=["InMemoryCheckpointStore"]).InMemoryCheckpointStore(),
        config=LoopConfig(max_runtime_seconds=2.0, sleep_min_seconds=0.01, sleep_max_seconds=0.05),
    )
    loop.request_stop()
    asyncio.run(loop.run())
    snap = loop.snapshot()
    assert snap["state"] in ("stopped", "running")
    assert "stats" in snap


def test_loop_writes_decision_history():
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="t", automaton_id=aid)
    treasury = InMemoryTreasury(aid, initial=Money.from_major("1.00"))
    loop = AutomatonLoop(
        ctx=ctx,
        reasoner=StubReasoner(),
        planner=HeuristicPlanner(PlannerConfig()),
        tools=__import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry(),
        treasury=treasury,
        budget=__import__("runtime.loop.budget", fromlist=["BudgetController", "BudgetConfig"]).BudgetController(
            __import__("runtime.loop.budget", fromlist=["BudgetConfig"]).BudgetConfig(
                reserve_floor=Money.zero(),
                per_tick_max=Money.from_major("1.00"),
                per_day_max=Money.from_major("10.00"),
            ),
            balance_getter=treasury.balance,
        ),
        checkpoints=__import__("runtime.loop.checkpoint", fromlist=["InMemoryCheckpointStore"]).InMemoryCheckpointStore(),
        config=LoopConfig(max_runtime_seconds=1.0, sleep_min_seconds=0.01, sleep_max_seconds=0.02),
    )
    asyncio.run(loop._tick())  # type: ignore[attr-defined]
    assert any(m.layer == MemoryLayer.DECISION_HISTORY for m in ctx.memory)
