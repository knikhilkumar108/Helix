"""
End-to-end test: the runtime loop uses a HelixTreasury as its wallet.
The agent starts with $0 in credits but $5 in USDC. After enough
spending, the auto-topup engine fires and refills the credits.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.survival.tiers import SurvivalTier
from core.types.identifiers import AutomatonId, new_automaton_id
from core.types.money import Money
from runtime.loop.budget import BudgetConfig, BudgetController
from runtime.loop.builtins import register_builtins
from runtime.loop.checkpoint import InMemoryCheckpointStore
from runtime.loop.context import InMemoryLoopContext
from runtime.loop.loop import AutomatonLoop, LoopConfig
from runtime.loop.planner import HeuristicPlanner, PlannerConfig
from runtime.loop.treasury import InMemoryTreasury
from services.treasury.helix_treasury import (
    HelixTreasury,
    MockBackend,
    TopupPolicy,
    TopupTrigger,
)
from services.router.llm_reasoner import LLMReasoner
from services.router.router import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelSpec,
)


class _ScriptedLLM:
    """Scripted LLM that writes to memory on every call (which costs
    the agent a tiny amount of credit)."""

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
            "summary": f"thinking tick {self.calls}",
            "next_action": {
                "tool": "memory.write",
                "arguments": {"content": f"thought {self.calls}", "layer": "long_term", "importance": 0.5},
            },
            "confidence": 0.7,
            "strategy": "remember",
        })
        return CompletionResponse(
            text=text, model="scripted", provider="test",
            input_tokens=100, output_tokens=len(text) // 4,
            cost_micro=0, latency_ms=10.0,
        )


@pytest.mark.asyncio
async def test_loop_auto_tops_up_credits_from_wallet():
    """The loop should call maybe_topup() on every tick. When the credit
    balance drops below the threshold, a topup fires, the agent's
    in-memory balance goes up, and the wallet's USDC goes down."""
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="test", automaton_id=aid)
    # The in-memory treasury starts EMPTY — the agent has $0 of credits.
    treasury = InMemoryTreasury(aid, initial=Money.zero())
    # The wallet has $5 to start.
    backend = MockBackend(initial_usdc_micro=5_000_000)
    wallet = HelixTreasury(
        backend, aid,
        policy=TopupPolicy(
            trigger=TopupTrigger.ON_LOW,
            # Topup when credits drop below 500 micro-credits.
            threshold_micro=500,
            # Buy up to 10_000 micro-credits (~$0.01). Enough that
            # the agent can survive several LLM ticks before the next
            # topup.
            target_micro=10_000,
            min_wallet_balance_micro=100_000,  # keep $0.10 in the wallet
            cooldown_seconds=0,
        ),
    )
    fake = _ScriptedLLM()
    reasoner = LLMReasoner(
        LLMRouter([fake]),
        automaton_id=str(aid),
        balance_getter=treasury.balance,
        tier_getter=lambda: SurvivalTier.NORMAL,
        tools_getter=lambda: ["memory.write", "memory.read", "time.now"],
    )
    tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
    register_builtins(tools, workspace=None)
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
            max_runtime_seconds=2.0, sleep_min_seconds=0.05, sleep_max_seconds=0.1,
        ),
        helix_treasury=wallet,
    )
    runner = asyncio.create_task(loop.run())
    await asyncio.sleep(1.5)
    loop.request_stop()
    await asyncio.wait_for(runner, timeout=3.0)

    # The agent should have made multiple LLM calls.
    assert fake.calls >= 2
    # The topup engine should have fired at least once — the agent
    # started with $0 of credits, and after the first tick the
    # threshold is crossed, triggering a topup.
    topup_events = [
        p for k, p in ctx.events if k == "helix_topup"
    ]
    assert len(topup_events) >= 1, (
        f"expected at least one topup, got events: "
        f"{[(k, p) for k, p in ctx.events if k in ('helix_topup','tier_changed','plan')]}"
    )
    # Credits were credited.
    assert treasury.balance().micro > 0
    # Wallet went down (we spent some USDC on the topup).
    usdc = await backend.get_usdc_balance_micro()
    assert usdc < 5_000_000
    # The topup recorded how many credits we bought.
    total_credits_bought = sum(p["credits_micro"] for p in topup_events)
    assert total_credits_bought > 0


@pytest.mark.asyncio
async def test_loop_works_without_helix_treasury():
    """If no HelixTreasury is configured, the loop should still
    function (the in-memory treasury is the only ledger)."""
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="test", automaton_id=aid)
    treasury = InMemoryTreasury(aid, initial=Money.from_major("0.10"))
    fake = _ScriptedLLM()
    reasoner = LLMReasoner(
        LLMRouter([fake]),
        automaton_id=str(aid),
        balance_getter=treasury.balance,
        tier_getter=lambda: SurvivalTier.NORMAL,
        tools_getter=lambda: ["memory.write", "memory.read", "time.now"],
    )
    tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
    register_builtins(tools, workspace=None)
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
            max_runtime_seconds=1.0, sleep_min_seconds=0.05, sleep_max_seconds=0.1,
        ),
        # No helix_treasury — pure in-memory mode.
    )
    runner = asyncio.create_task(loop.run())
    await asyncio.sleep(0.8)
    loop.request_stop()
    await asyncio.wait_for(runner, timeout=2.0)
    # The LLM was called.
    assert fake.calls >= 1
    # No topup events were recorded (no wallet).
    topup_events = [e for k, p in ctx.events if k == "helix_topup"]
    assert len(topup_events) == 0


@pytest.mark.asyncio
async def test_topup_failure_does_not_kill_the_loop():
    """If the wallet backend throws (e.g. RPC failure), the loop
    should log a warning and continue running."""
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="test", automaton_id=aid)
    treasury = InMemoryTreasury(aid, initial=Money.from_major("0.10"))
    fake = _ScriptedLLM()
    reasoner = LLMReasoner(
        LLMRouter([fake]),
        automaton_id=str(aid),
        balance_getter=treasury.balance,
        tier_getter=lambda: SurvivalTier.NORMAL,
        tools_getter=lambda: ["memory.write", "memory.read", "time.now"],
    )
    tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
    register_builtins(tools, workspace=None)
    budget = BudgetController(
        BudgetConfig(
            reserve_floor=Money.zero(),
            per_tick_max=Money.from_major("1.00"),
            per_day_max=Money.from_major("100.00"),
        ),
        balance_getter=treasury.balance,
    )

    class _BrokenBackend:
        def address(self):
            return "0xbroken"

        async def get_usdc_balance_micro(self):
            raise ConnectionError("RPC down")

        async def transfer_usdc_micro(self, to, amount):
            raise ConnectionError("RPC down")

        async def wait_for_confirmation(self, tx, *, timeout=60.0):
            return False

    wallet = HelixTreasury(
        _BrokenBackend(), aid,
        policy=TopupPolicy(
            trigger=TopupTrigger.ON_LOW,
            threshold_micro=100,
            target_micro=1_000,
            min_wallet_balance_micro=100_000,
            cooldown_seconds=0,
        ),
    )
    loop = AutomatonLoop(
        ctx=ctx, reasoner=reasoner, planner=HeuristicPlanner(PlannerConfig()),
        tools=tools, treasury=treasury, budget=budget,
        checkpoints=InMemoryCheckpointStore(),
        config=LoopConfig(
            max_runtime_seconds=1.0, sleep_min_seconds=0.05, sleep_max_seconds=0.1,
        ),
        helix_treasury=wallet,
    )
    runner = asyncio.create_task(loop.run())
    await asyncio.sleep(0.8)
    loop.request_stop()
    # The loop should still finish without raising.
    await asyncio.wait_for(runner, timeout=2.0)
    # The LLM was called even though the wallet was broken.
    assert fake.calls >= 1
    # No topups succeeded.
    topup_events = [e for k, p in ctx.events if k == "helix_topup"]
    assert len(topup_events) == 0
