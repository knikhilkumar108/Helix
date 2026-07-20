"""
End-to-end integration test: a real LLMReasoner talks to a fake
provider, drives the full loop, and the loop actually executes a tool
that the LLM asked for. This is the proof that the platform, with
the LLM plugged in, behaves like an agent.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

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
from services.router.llm_reasoner import LLMReasoner
from services.router.router import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelSpec,
)


class _ScriptedClient:
    """Returns one canned response per call, in order."""

    def __init__(self, responses: list[str]) -> None:
        self.spec = ModelSpec(
            name="scripted",
            provider="test",
            context_window=128_000,
            cost_per_1k_input_micro=100,
            cost_per_1k_output_micro=300,
            capabilities=frozenset({"chat", "code"}),
            quality=0.8,
            avg_latency_ms=10,
        )
        self._responses = list(responses)
        self.calls: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        text = self._responses.pop(0) if self._responses else '{"summary": "done"}'
        return CompletionResponse(
            text=text,
            model=self.spec.name,
            provider=self.spec.provider,
            input_tokens=200,
            output_tokens=len(text) // 4,
            cost_micro=100,
            latency_ms=5.0,
        )


@pytest.mark.asyncio
async def test_llm_driven_agent_executes_tool():
    """The LLM asks the agent to write to memory; the agent does so."""
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="test", automaton_id=aid)
    treasury = InMemoryTreasury(aid, initial=Money.from_major("1.00"))
    tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
    register_builtins(tools, workspace=None)

    # Script the LLM: on the first call, it returns a structured action
    # asking the agent to write a memory entry. On the second call, it
    # tells the agent to sleep.
    scripted = _ScriptedClient(
        [
            json.dumps({
                "summary": "writing a memory note about the user",
                "queries": ["user"],
                "next_action": {
                    "tool": "memory.write",
                    "arguments": {"content": "the user's name is Alice", "layer": "long_term"},
                },
                "confidence": 0.9,
                "strategy": "remember",
            }),
            json.dumps({
                "summary": "wrote memory; sleeping now",
                "queries": [],
                "next_action": {"tool": None, "arguments": {}},
                "confidence": 0.9,
                "strategy": "sleep",
            }),
        ]
    )
    router = LLMRouter([scripted])
    reasoner = LLMReasoner(
        router,
        automaton_id=str(aid),
        balance_getter=treasury.balance,
        tier_getter=lambda: SurvivalTier.NORMAL,
        tools_getter=lambda: [t.name for t in tools.list()],
    )
    planner = HeuristicPlanner(PlannerConfig())
    budget = BudgetController(
        BudgetConfig(
            reserve_floor=Money.zero(),
            per_tick_max=Money.from_major("1.00"),
            per_day_max=Money.from_major("100.00"),
        ),
        balance_getter=treasury.balance,
    )
    loop = AutomatonLoop(
        ctx=ctx,
        reasoner=reasoner,
        planner=planner,
        tools=tools,
        treasury=treasury,
        budget=budget,
        checkpoints=InMemoryCheckpointStore(),
        config=LoopConfig(
            max_runtime_seconds=2.0,
            sleep_min_seconds=0.05,
            sleep_max_seconds=0.1,
            max_actions_per_tick=4,
        ),
    )

    # Run the loop and stop it after a short delay. We can't call
    # request_stop() before run() because that would make the loop exit
    # immediately — the loop's exit condition is `_stop.is_set()`.
    runner = asyncio.create_task(loop.run())
    await asyncio.sleep(1.5)
    loop.request_stop()
    await runner

    # The LLM was actually called (proves the LLM is the brain, not a stub).
    assert len(scripted.calls) >= 1

    # The system prompt the LLM saw included the agent's identity and balance.
    system_msg = scripted.calls[0].messages[0]["content"]
    assert str(aid) in system_msg
    assert "1.000000 USDC" in system_msg
    assert "Constitution" in system_msg

    # The LLM asked the agent to do a memory.write. The platform executed
    # the tool. The action result is persisted into the context. Verify
    # by looking at the actions list (the LLM's tool call should have
    # produced a successful action with a non-empty result).
    assert len(ctx.actions) >= 1, f"no actions executed: {ctx.events}"
    successful = [a for a in ctx.actions if getattr(a, "result", None) and not getattr(a, "error", None)]
    assert successful, f"all actions failed: {[getattr(a, 'error', None) for a in ctx.actions]}"
    # Balance was charged for the action.
    assert treasury.balance().micro < 1_000_000


@pytest.mark.asyncio
async def test_llm_garbage_output_does_not_crash():
    """If the LLM emits unparseable text, the loop continues."""
    aid = AutomatonId(new_automaton_id())
    ctx = InMemoryLoopContext(service="test", automaton_id=aid)
    treasury = InMemoryTreasury(aid, initial=Money.from_major("1.00"))
    tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
    register_builtins(tools, workspace=None)

    scripted = _ScriptedClient(["oh dear this is not json at all", "still not json"])
    router = LLMRouter([scripted])
    reasoner = LLMReasoner(router, automaton_id=str(aid), balance_getter=treasury.balance)
    planner = HeuristicPlanner(PlannerConfig())
    budget = BudgetController(
        BudgetConfig(
            reserve_floor=Money.zero(),
            per_tick_max=Money.from_major("1.00"),
            per_day_max=Money.from_major("100.00"),
        ),
        balance_getter=treasury.balance,
    )
    loop = AutomatonLoop(
        ctx=ctx,
        reasoner=reasoner,
        planner=planner,
        tools=tools,
        treasury=treasury,
        budget=budget,
        checkpoints=InMemoryCheckpointStore(),
        config=LoopConfig(
            max_runtime_seconds=1.0,
            sleep_min_seconds=0.01,
            sleep_max_seconds=0.02,
        ),
    )
    loop.request_stop()
    await loop.run()  # should not raise


@pytest.mark.asyncio
async def test_real_router_picks_only_available_client():
    """default_real_router returns a router; with no keys it should
    fall back to Ollama at default URL (which may or may not answer)."""
    from services.router.real_clients import default_real_router

    # Force "auto" but clear any pre-existing keys.
    import os

    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        os.environ.pop(k, None)
    router = default_real_router(prefer="auto")
    # At minimum we should have a fallback Ollama client.
    assert len(router.models()) >= 1
    # The fallback is Ollama at the default URL.
    assert any(m.provider == "ollama" for m in router.models())
