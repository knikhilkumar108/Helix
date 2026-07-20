"""
Runtime loop convenience initializer. Wires up the dependencies and returns
a ready-to-run loop. This is the canonical entrypoint for the runtime.

Two flavors:

  - `build_default_loop(...)` — uses the deterministic stub reasoner
    (no LLM, no network). Great for tests, CI, and demos.

  - `build_llm_loop(...)` — uses a real LLM via the `LLMRouter`. Set
    `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OLLAMA_MODEL` in the
    environment (or pass keys directly), and the agent will actually
    think.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.policy.policy import Constitution, ConstitutionEvaluator, compose_evaluators
from core.policy.rbac import RBACABAC, Principal
from core.security.injection_defense import RateLimiter
from core.survival.tiers import SurvivalTier, TierConfig
from core.types.identifiers import AutomatonId
from core.types.money import Money

from .budget import BudgetConfig, BudgetController
from .builtins import register_builtins
from .checkpoint import InMemoryCheckpointStore
from .context import InMemoryLoopContext
from .loop import AutomatonLoop, LoopConfig
from .planner import HeuristicPlanner, PlannerConfig
from .reasoner import StubReasoner
from .tools import ToolRegistry
from .treasury import InMemoryTreasury

log = logging.getLogger(__name__)


# ── Shared wiring ────────────────────────────────────────────────


def _build_policy_pipeline(principal: Principal):
    constitution = ConstitutionEvaluator(Constitution.default())
    rbac = RBACABAC()

    async def policy_pipeline(action) -> Any:  # type: ignore[no-untyped-def]
        d_const = constitution.evaluate(action)
        if d_const.verdict.value == "deny":
            return d_const
        d_rbac = rbac.evaluate(principal, action)
        if d_rbac.verdict.value == "deny":
            return d_rbac
        if (
            d_const.verdict.value == "require_approval"
            or d_rbac.verdict.value == "require_approval"
        ):
            return d_const
        return d_const

    return policy_pipeline


def _build_loop(
    *,
    automaton_id: AutomatonId,
    initial_balance: Money,
    workspace: Path | None,
    loop_config: LoopConfig | None,
    policy_principal: Principal | None,
    reasoner: Any,
    helix_treasury: Any = None,
    inbox: Any = None,
    history: Any = None,
    todo: Any = None,
    dashboard: Any = None,
    audit_hook: Any = None,
) -> AutomatonLoop:
    from .treasury import InMemoryTreasury

    tools = ToolRegistry()
    register_builtins(tools, workspace=workspace)
    treasury = InMemoryTreasury(automaton_id, initial=initial_balance or Money.zero())
    budget = BudgetController(
        config=BudgetConfig(
            reserve_floor=Money.zero(treasury.balance().currency),
            per_tick_max=Money.from_major("1.00", treasury.balance().currency),
            per_day_max=Money.from_major("100.00", treasury.balance().currency),
        ),
        balance_getter=treasury.balance,
    )
    planner = HeuristicPlanner(PlannerConfig(default_currency=treasury.balance().currency))
    checkpoints = InMemoryCheckpointStore()
    ctx = InMemoryLoopContext(service="runtime", automaton_id=automaton_id)
    # If an inbox is provided, attach it to both the tool registry
    # (so the messaging.* tools can use it) and the loop context
    # (so the observation step surfaces pending message counts).
    if inbox is not None:
        tools.extra["inbox"] = inbox
        tools.extra["self_id"] = str(automaton_id)
        ctx.extra["inbox"] = inbox
    # Same idea for the conversation history. A real chat-style
    # agent (multi-turn conversation with a user) needs this;
    # a one-shot worker doesn't.
    if history is not None:
        tools.extra["history"] = history
        ctx.extra["history"] = history
    # Same idea for plan mode. The TodoService writes TODO.md
    # to the workspace; the agent reads it via `fs.read` and
    # updates it via the plan.* tools.
    if todo is not None:
        tools.extra["todo"] = todo
        ctx.extra["todo"] = todo
    principal = policy_principal or Principal(
        id=str(automaton_id),
        kind="automaton",
        roles=frozenset({"operator"}),
    )

    return AutomatonLoop(
        ctx=ctx,
        reasoner=reasoner,
        planner=planner,
        tools=tools,
        treasury=treasury,
        budget=budget,
        checkpoints=checkpoints,
        config=loop_config or LoopConfig(),
        policy_pipeline=_build_policy_pipeline(principal),
        tier_config=TierConfig(),
        injection_rate_limiter=RateLimiter(),
        helix_treasury=helix_treasury,
        dashboard=dashboard,
        audit_hook=audit_hook,
    )


# ── Public builders ───────────────────────────────────────────────


def build_default_loop(
    automaton_id: AutomatonId,
    *,
    initial_balance: Money | None = None,
    workspace: Path | None = None,
    loop_config: LoopConfig | None = None,
    policy_principal: Principal | None = None,
    helix_treasury: Any = None,
    inbox: Any = None,
    history: Any = None,
    todo: Any = None,
    dashboard: Any = None,
    audit_hook: Any = None,
) -> AutomatonLoop:
    """A loop with the deterministic stub reasoner. No LLM, no network.

    If `helix_treasury` is provided, the loop's in-memory treasury is
    bridged to it: a background task calls `maybe_topup()` on every
    tick, and any USDC received by the wallet is credited to the
    in-memory ledger as well. This is how a real agent earns.

    If `audit_hook` is provided, the runtime calls it for
    important events (loop_started, loop_stopped, helix_topup,
    tier_changed). Production wires this to
    `SqliteStore.append_audit` so the hash-chained audit log
    captures every state change.

    If `inbox` is provided, the agent can use the `messaging.*` tools
    to send and receive messages, and the runtime's observation will
    surface the pending message count on every tick.
    """
    from .treasury import InMemoryTreasury

    reasoner = StubReasoner(summary="begin", queries=["status"])
    return _build_loop(
        automaton_id=automaton_id,
        initial_balance=initial_balance or Money.zero(),
        workspace=workspace,
        loop_config=loop_config,
        policy_principal=policy_principal,
        reasoner=reasoner,
        helix_treasury=helix_treasury,
        inbox=inbox,
        history=history,
        todo=todo,
        dashboard=dashboard,
        audit_hook=audit_hook,
    )


def build_llm_loop(
    automaton_id: AutomatonId,
    *,
    router: Any,
    wallet_address: str | None = None,
    initial_balance: Money | None = None,
    workspace: Path | None = None,
    loop_config: LoopConfig | None = None,
    policy_principal: Principal | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    inbox: Any = None,
    history: Any = None,
    todo: Any = None,
    dashboard: Any = None,
    audit_hook: Any = None,
) -> AutomatonLoop:
    """A loop with a real LLM reasoner.

    Pass an `LLMRouter` (see `services.router.default_real_router()` to
    build one from your environment). The router picks the cheapest
    available model that meets the request's quality floor, and the
    reasoner parses the model's JSON response into a `ReasoningResult`.

    Tier-driven behavior:
      - normal:        frontier model, 512 max_tokens
      - low_compute:   standard model, 512 max_tokens, downshift
      - critical:      mini model, 256 max_tokens, sleep more
      - dead:          loop halts
    """
    from services.router.llm_reasoner import LLMReasoner

    # Pre-build the loop with a placeholder reasoner, then attach the real
    # one once we have the treasury + tools (which we need for the
    # balance_getter, tier_getter, and tools_getter closures).
    tools = ToolRegistry()
    register_builtins(tools, workspace=workspace)
    treasury = InMemoryTreasury(automaton_id, initial=initial_balance or Money.zero())

    def tools_list() -> list[str]:
        return [t.name for t in tools.list()]

    reasoner = LLMReasoner(
        router,
        automaton_id=str(automaton_id),
        wallet_address=wallet_address,
        balance_getter=treasury.balance,
        tier_getter=lambda: SurvivalTier.NORMAL,  # refined below
        tools_getter=tools_list,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return _build_loop(
        automaton_id=automaton_id,
        initial_balance=initial_balance or Money.zero(),
        workspace=workspace,
        loop_config=loop_config,
        policy_principal=policy_principal,
        reasoner=reasoner,
        inbox=inbox,
        history=history,
        todo=todo,
        dashboard=dashboard,
        audit_hook=audit_hook,
    )
