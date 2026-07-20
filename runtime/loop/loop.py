"""
The core runtime loop of every Automaton.

Observe -> Reason -> Retrieve memory -> Generate plan -> Estimate cost ->
Constitution -> Permission -> Execute -> Verify -> Learn -> Update memory ->
Pay compute -> Update treasury -> Sleep -> Repeat

The loop is:
  * checkpointed after every stage (so a crash resumes from the last step)
  * cancellable (pause signal stops the loop after the current stage)
  * instrumented (metrics, traces, structured logs)
  * bounded in compute spend (the budget is enforced by the executor)
"""
from __future__ import annotations

import asyncio
import enum
import logging
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.observability.metrics import METRICS
from core.observability.tracing import span
from core.security.injection_defense import (
    SanitizationMode,
    ThreatLevel,
    sanitize_input,
)
from core.survival.tiers import SurvivalTier, TierBehavior, TierConfig
from core.types.automaton import (
    Action,
    HealthReport,
    MemoryEntry,
    MemoryLayer,
    PolicyDecision,
    Task,
)
from core.types.identifiers import ActionId, TaskId, new_action_id, new_task_id
from core.types.money import Cost, Money

from .budget import BudgetController
from .checkpoint import CheckpointStore
from .context import LoopContext
from .loop_detector import LoopDetector, LoopVerdict
from .planner import Planner
from .reasoner import Reasoner
from .tools import ToolRegistry
from .treasury import Treasury

log = logging.getLogger(__name__)


class LoopState(str, enum.Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    SLEEPING = "sleeping"
    CRASHED = "crashed"


@dataclass(slots=True)
class LoopConfig:
    tick_interval_seconds: float = 1.0
    max_actions_per_tick: int = 4
    max_runtime_seconds: float = 60.0
    sleep_min_seconds: float = 1.0
    sleep_max_seconds: float = 30.0
    healthcheck_interval_seconds: float = 15.0
    checkpoint_interval_seconds: float = 5.0
    # Loop detection thresholds. The Conway Automaton reference
    # (Conway-Research/automaton) uses 3; we keep the same default.
    max_repetitions: int = 3
    max_idle_turns: int = 3
    # How long to wait on a single human approval before re-checking
    # the loop's stop signal. The approval's own TTL can be much longer.
    approval_timeout_seconds: float = 60.0
    # Tier-driven behavior. None means "use defaults".
    tier_config: TierConfig | None = None


@dataclass(slots=True)
class LoopStats:
    iterations: int = 0
    actions: int = 0
    earned: Money = field(default_factory=Money.zero)
    spent: Money = field(default_factory=Money.zero)


class AutomatonLoop:
    """The runtime that drives a single Automaton."""

    def __init__(
        self,
        ctx: LoopContext,
        reasoner: Reasoner,
        planner: Planner,
        tools: ToolRegistry,
        treasury: Treasury,
        budget: BudgetController,
        checkpoints: CheckpointStore,
        config: LoopConfig | None = None,
        *,
        policy_pipeline: Callable[[Action], Awaitable[PolicyDecision]] | None = None,
        tier_config: TierConfig | None = None,
        self_mod: "Any | None" = None,
        injection_rate_limiter: "Any | None" = None,
        approval_gate: "Any | None" = None,
        helix_treasury: "Any | None" = None,
        dashboard: "Any | None" = None,
        audit_hook: "Any | None" = None,
    ) -> None:
        self.ctx = ctx
        self.reasoner = reasoner
        self.planner = planner
        self.tools = tools
        self.treasury = treasury
        self.budget = budget
        self.checkpoints = checkpoints
        self.config = config or LoopConfig()
        self.state = LoopState.STOPPED
        self.stats = LoopStats()
        self._stop = asyncio.Event()
        self._pause = asyncio.Event()
        self._pause.set()
        self._policy_pipeline = policy_pipeline
        self.tier_config = tier_config or TierConfig()
        self.current_tier: SurvivalTier = SurvivalTier.NORMAL
        self.loop_detector = LoopDetector(
            max_repetitions=config.max_repetitions if config else 3,
            max_idle_turns=config.max_idle_turns if config else 3,
        )
        self.self_mod = self_mod
        self.injection_rate_limiter = injection_rate_limiter
        self.approval_gate = approval_gate
        # If the gate supports it, wire our stop event so a SIGTERM
        # cancels any in-flight approval wait.
        if self.approval_gate is not None and hasattr(self.approval_gate, "bind_stop_event"):
            self.approval_gate.bind_stop_event(self._stop)
        # Optional HelixTreasury: a real on-chain wallet that holds
        # USDC and auto-tops up the in-memory credit ledger when the
        # balance runs low. This is what turns the agent from a
        # credit-burning simulation into a self-funding economic agent.
        self.helix_treasury = helix_treasury
        # Optional DashboardStream: an in-process event bus that
        # the operator's WebSocket subscribes to. The runtime
        # publishes events as it works (treasury updates, tier
        # changes, action completions, etc.). If `None`, events
        # are not published — useful in tests and embedded
        # deployments.
        self.dashboard = dashboard
        # Optional audit hook: a callable the runtime calls
        # for important events (loop_started, loop_stopped,
        # helix_topup, tier_changed, action_completed, etc.).
        # The default hook writes nothing; production wires
        # this to `SqliteStore.append_audit()` so the
        # hash-chained audit log captures every state change.
        # The hook is best-effort: failures are logged but
        # never crash the loop.
        self.audit_hook = audit_hook

    # ---------- lifecycle ----------
    def request_stop(self) -> None:
        self._stop.set()
        self._pause.set()

    def request_pause(self) -> None:
        self._pause.clear()
        self.state = LoopState.PAUSED

    def request_resume(self) -> None:
        self._pause.set()

    async def health(self) -> HealthReport:
        return HealthReport(
            status="healthy"
            if self.state in (LoopState.RUNNING, LoopState.SLEEPING)
            else "degraded",
            components={
                "loop": {"status": "up", "message": self.state.value},  # type: ignore[arg-type]
                "treasury": {"status": "up", "message": str(self.treasury.balance())},  # type: ignore[arg-type]
            },
            checked_at=datetime.now(tz=timezone.utc),
        )

    # ---------- main entrypoint ----------
    async def run(self) -> None:
        self.state = LoopState.RUNNING
        self.ctx.record("loop_started", {"at": time.time()})
        self._audit(
            kind="loop_started",
            payload={"at": time.time()},
        )
        last_checkpoint = time.time()
        loop_started = time.time()

        while not self._stop.is_set():
            await self._pause.wait()
            if self._stop.is_set():
                break

            # Hard wall-clock ceiling. The runtime can run for at most
            # `max_runtime_seconds` before checkpointing and exiting so the
            # supervisor can rebalance.
            if time.time() - loop_started > self.config.max_runtime_seconds:
                log.info("loop_max_runtime_reached", extra={"max": self.config.max_runtime_seconds})
                break

            iteration_started = time.time()
            outcome = "ok"
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                outcome = "error"
                log.exception("loop_tick_error", extra={"err": str(e)})
                METRICS.errors_total.labels(
                    service=self.ctx.service,
                    category="loop",
                    code=type(e).__name__,
                ).inc()

            self.stats.iterations += 1
            METRICS.loop_iterations_total.labels(
                service=self.ctx.service, outcome=outcome
            ).inc()
            METRICS.loop_iteration_duration_seconds.labels(
                service=self.ctx.service, stage="tick"
            ).observe(time.time() - iteration_started)

            if time.time() - last_checkpoint >= self.config.checkpoint_interval_seconds:
                await self.checkpoints.save(self.ctx.automaton_id, self.snapshot())
                last_checkpoint = time.time()

            await self._sleep()

        await self.checkpoints.save(self.ctx.automaton_id, self.snapshot())
        self.state = LoopState.STOPPED
        self.ctx.record("loop_stopped", {"at": time.time()})
        self._audit(
            kind="loop_stopped",
            payload={"at": time.time()},
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "stats": {
                "iterations": self.stats.iterations,
                "actions": self.stats.actions,
                "earned_micro": self.stats.earned.micro,
                "spent_micro": self.stats.spent.micro,
                "currency": self.stats.earned.currency,
            },
            "memory_pointer": self.ctx.memory_pointer(),
        }

    # ---------- one full Observe→Sleep cycle ----------
    async def _tick(self) -> None:
        # -1) HelixTreasury topup — if the agent has a real wallet,
        # try to top up the in-memory credit ledger from USDC before
        # the tier check (otherwise the agent would die at zero credits
        # even with a full wallet). Best-effort: a transient RPC
        # failure should never kill the loop.
        if self.helix_treasury is not None:
            try:
                event = await self.helix_treasury.maybe_topup()
                if event is not None:
                    # Credit the in-memory ledger (the runtime's hot
                    # path). The HelixTreasury also has its own copy
                    # for visibility, but the runtime never reads
                    # from it — the in-memory ledger is the source of
                    # truth.
                    self.treasury.credit(
                        amount=Money(event.credits_purchased_micro, "USDC"),
                        category="topup:helix_credits",
                        ref_type="transfer",
                        ref_id=event.tx_hash,
                        memo=f"topup via {event.tx_hash[:10]}…",
                    )
                    self.ctx.record(
                        "helix_topup",
                        {
                            "credits_micro": event.credits_purchased_micro,
                            "usdc_micro": event.usdc_spent_micro,
                            "tx_hash": event.tx_hash,
                        },
                    )
                    self._audit(
                        kind="helix_topup",
                        payload={
                            "credits_micro": event.credits_purchased_micro,
                            "usdc_micro": event.usdc_spent_micro,
                            "tx_hash": event.tx_hash,
                        },
                    )
                    # Publish a treasury update to the dashboard.
                    from services.dashboard import EventKind
                    self._publish_dashboard(
                        kind=EventKind.TREASURY_UPDATE,
                        payload={
                            "kind": "topup",
                            "credits_micro": event.credits_purchased_micro,
                            "usdc_micro": event.usdc_spent_micro,
                            "tx_hash": event.tx_hash,
                            "balance_micro": self.treasury.balance().micro,
                        },
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("helix_topup_failed", extra={"err": str(e)})

        # 0) Re-evaluate survival tier AFTER the topup, so a fresh
        # topup can lift the agent out of `dead` to `critical` or
        # `low_compute` without restarting.
        self._refresh_tier()

        # If we're DEAD, refuse to do anything new until the user funds us.
        if self.current_tier is SurvivalTier.DEAD:
            self.ctx.record("tier_dead", {"balance": str(self.treasury.balance())})
            log.warning("tier_dead_suspending")
            self._stop.set()
            return

        # Tier-driven capability clamps.
        tier_behavior = TierBehavior.for_tier(self.current_tier)
        max_actions = min(self.config.max_actions_per_tick, tier_behavior.max_tool_calls_per_turn)

        # 1) Observe — sanitize any untrusted content before it reaches the LLM.
        raw_observation = self.ctx.observe()
        observation = self._sanitize_observation(raw_observation)
        self.ctx.record("observe", {"len": len(str(observation))})

        # 2) Reason
        reasoning = await self.reasoner.think(observation, self.ctx)
        self.ctx.record("reason", {"summary": reasoning.summary})

        # 3) Retrieve memory
        memory = await self.ctx.recall(reasoning.queries)
        self.ctx.record("recall", {"hits": len(memory)})

        # 4) Generate plan — the planner may consult the tier to drop optional work.
        plan = await self.planner.plan(reasoning, memory, self.ctx)
        if not tier_behavior.allow_optional_work:
            # Critical/low_compute: drop any non-essential steps.
            plan = self._strip_optional_steps(plan)
        self.ctx.record("plan", {"plan_id": str(plan.id), "steps": len(plan.steps)})

        # 5) Estimate cost
        cost_estimate = self._estimate_cost(plan)
        if not self.budget.can_afford(cost_estimate):
            self.ctx.record("budget_block", {"estimated": str(cost_estimate)})
            log.info(
                "loop_budget_block",
                extra={"plan_id": str(plan.id), "estimate": str(cost_estimate)},
            )
            return

        # 6/7) Constitution + Permission
        decisions: list[PolicyDecision] = []
        actions_to_run: list[Action] = []
        for step in plan.steps[:max_actions]:
            task = self._materialize_task(plan, step)
            # When the step description is already a dict (e.g. supplied
            # by the LLM via `next_action.arguments`), use it directly.
            # When it's a string, wrap it as {"input": ...} so tools that
            # don't expect arguments still work.
            if isinstance(step.description, dict):
                arguments: dict[str, Any] = dict(step.description)
            else:
                arguments = {"input": step.description}
            action = Action(
                id=new_action_id(),
                task_id=task.id,
                plan_id=plan.id,
                tool_name=step.kind,
                arguments=arguments,
                risk=step.risk,
                cost_estimate=step.estimated_cost,
                policy_decision=PolicyDecision(
                    verdict="allow",
                    reason="pending",
                    evaluated_at=datetime.now(tz=timezone.utc),
                    evaluator="pending",
                ),
                started_at=datetime.now(tz=timezone.utc),
            )
            decision = (
                await self._policy_pipeline(action)
                if self._policy_pipeline
                else self._default_policy(action)
            )
            action.policy_decision = decision
            decisions.append(decision)
            METRICS.policy_decisions_total.labels(
                service=self.ctx.service,
                evaluator=decision.evaluator,
                verdict=decision.verdict.value,
            ).inc()
            if decision.verdict == "allow":
                actions_to_run.append(action)
            elif decision.verdict == "require_approval":
                # Park the action for human review. We still add it to
                # `actions_to_run` so the executor can submit it to the
                # approval gate and await the decision. If no gate is
                # configured, the action is recorded but never runs.
                self.ctx.record("approval_required", {"tool": action.tool_name})
                if self.approval_gate is not None:
                    actions_to_run.append(action)
                else:
                    self.ctx.record(
                        "approval_skipped_no_gate",
                        {"tool": action.tool_name, "reason": "no approval_gate configured"},
                    )
            else:  # verdict == "deny"
                # The Constitution or RBAC denied this action.
                # Record it loudly — denials are a safety event,
                # not silent drops. The action is NOT added to
                # `actions_to_run`; the executor never sees it.
                self.ctx.record(
                    "policy_denied",
                    {
                        "tool": action.tool_name,
                        "reason": decision.reason,
                        "evaluator": decision.evaluator,
                    },
                )
                self._audit(
                    kind="policy_denied",
                    payload={
                        "tool": action.tool_name,
                        "reason": decision.reason,
                        "evaluator": decision.evaluator,
                    },
                )
                self._publish_dashboard(
                    kind=__import__(
                        "services.dashboard", fromlist=["EventKind"]
                    ).EventKind.POLICY_DENIED,
                    payload={
                        "tool": action.tool_name,
                        "reason": decision.reason,
                        "evaluator": decision.evaluator,
                    },
                )
                log.warning(
                    "policy_denied",
                    extra={
                        "tool": action.tool_name,
                        "reason": decision.reason,
                        "evaluator": decision.evaluator,
                    },
                )

        # Loop detection — pre-execution.
        verdict = self.loop_detector.observe([a.tool_name for a in actions_to_run])
        if verdict is LoopVerdict.ENFORCE_SLEEP:
            self.ctx.record("loop_enforce_sleep", {"reason": "pattern_repeat_or_idle"})
            log.warning("loop_enforced_sleep")
            await self._force_sleep(seconds=60.0)
            return
        if verdict is LoopVerdict.WARN:
            self.ctx.record("loop_warn", {"tools": [a.tool_name for a in actions_to_run]})
            log.warning("loop_warned")

        # 8) Execute
        for action in actions_to_run:
            with span("action.execute", tool=action.tool_name, action_id=str(action.id)):
                await self._execute_action(action)

        # 9) Verify
        await self._verify(actions_to_run)

        # 10) Learn
        await self._learn(actions_to_run, memory)

        # 11) Update memory
        self._update_memory(actions_to_run, reasoning)

        # 12) Pay compute
        await self._settle(actions_to_run, cost_estimate)

    # ---------- tier helpers ----------
    def _refresh_tier(self) -> None:
        new_tier = self.tier_config.tier_for(self.treasury.balance().micro)
        if new_tier is not self.current_tier:
            self.ctx.record(
                "tier_changed",
                {"from": self.current_tier.value, "to": new_tier.value},
            )
            self._audit(
                kind="tier_changed",
                payload={
                    "from": self.current_tier.value,
                    "to": new_tier.value,
                },
            )
            log.info(
                "tier_changed",
                extra={"from": self.current_tier.value, "to": new_tier.value},
            )
            # Publish a tier-change event to the dashboard,
            # if wired. The dashboard's WebSocket clients see
            # this in real time.
            if self.dashboard is not None:
                from services.dashboard import EventKind
                self._publish_dashboard(
                    kind=EventKind.TIER_CHANGE,
                    payload={
                        "from": self.current_tier.value,
                        "to": new_tier.value,
                    },
                )
        self.current_tier = new_tier

    def _publish_dashboard(
        self,
        *,
        kind: "EventKind",
        payload: dict[str, Any],
    ) -> None:
        """Publish a dashboard event. Synchronous and
        best-effort: a failure is logged but never crashes
        the loop. The bus uses a threading lock so this
        is safe to call from any context (sync, async,
        no event loop)."""
        if self.dashboard is None:
            return
        try:
            from core.types.identifiers import AutomatonId
            aid = (
                self.ctx.automaton_id
                if isinstance(self.ctx.automaton_id, AutomatonId)
                else AutomatonId(str(self.ctx.automaton_id))
            )
            evt = self.dashboard.make_event(
                kind=kind, aid=aid, payload=payload,
            )
            self.dashboard.publish(evt)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "dashboard_publish_error",
                extra={"err": str(e)},
            )

    def _audit(self, *, kind: str, payload: dict[str, Any]) -> None:
        """Write to the audit hook, if wired.

        The hook is the bridge between the in-memory
        `ctx.record()` and the durable `SqliteStore.append_audit()`.
        A production deployment wires this to the audit
        chain; a test deployment uses a no-op or an
        in-memory list.

        Failures are logged but never crash the loop:
        the audit chain is for *observability*, not
        *correctness*. A failed audit write means an
        operator can't see what happened; it doesn't
        change the agent's behavior.
        """
        if self.audit_hook is None:
            return
        try:
            self.audit_hook(kind=kind, payload=payload)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "audit_hook_failed",
                extra={"kind": kind, "err": str(e)},
            )

    @staticmethod
    def _strip_optional_steps(plan) -> Any:  # type: ignore[no-untyped-def]
        # Optional steps are anything not in the core loop: writes, installs,
        # mutations, self-modification. We keep reads, memory writes, and
        # reasoning steps. When the agent has only one step (e.g. the LLM
        # picked a specific tool), we keep it so the request isn't silently
        # dropped on a tier downshift — better to ask the LLM again next
        # tick than to lose the request entirely.
        essential = {"memory.read", "memory.write", "memory.search", "time.now"}
        if len(plan.steps) != 1:
            plan.steps = [s for s in plan.steps if s.kind in essential]
        # Recompute estimated cost so the budget check sees reality.
        from core.types.money import Money

        new_total = Money.zero(plan.estimated_cost.currency)
        for s in plan.steps:
            new_total = new_total + s.estimated_cost
        plan.estimated_cost = new_total
        return plan

    def _sanitize_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Sanitize any untrusted text in the observation before it reaches the LLM."""
        if not self.injection_rate_limiter:
            return observation
        sanitized: dict[str, Any] = {}
        for k, v in observation.items():
            if isinstance(v, str):
                out = sanitize_input(
                    v,
                    source=str(k),
                    mode=SanitizationMode.SOCIAL_MESSAGE,
                    rate_limiter=self.injection_rate_limiter,
                )
                sanitized[k] = out.content
                if out.blocked:
                    self.ctx.record("injection_blocked", {"source": k, "level": out.threat_level.value})
            else:
                sanitized[k] = v
        return sanitized

    async def _force_sleep(self, *, seconds: float) -> None:
        """Sleep without honoring pause/stop — the loop has been forcibly
        paused due to repetition. We still honor a stop signal so the
        operator can kill us mid-sleep."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        self._stop.set()

    # ---------- stage helpers ----------
    async def _observe(self) -> dict[str, Any]:
        return self.ctx.observe()

    def _estimate_cost(self, plan) -> Money:  # type: ignore[no-untyped-def]
        total = Money.zero(plan.estimated_cost.currency)
        for step in plan.steps:
            total = total + step.estimated_cost
        return total

    def _default_policy(self, action: Action) -> PolicyDecision:
        from core.policy.policy import ConstitutionEvaluator, compose_evaluators
        from core.policy.rbac import RBACABAC, Principal

        evaluator = compose_evaluators(
            ConstitutionEvaluator(),
            RBACABAC(),
        )
        # Principal is the automaton itself for self-actions.
        principal = Principal(
            id=str(self.ctx.automaton_id),
            kind="automaton",
            roles=frozenset({"operator"}),
        )
        # Composite's signature expects (Action) and calls each evaluator.
        d1 = evaluator._evals[0].evaluate(action)
        d2 = evaluator._evals[1].evaluate(principal, action)
        # Combine verdicts. The precedence is:
        #   1. deny (from either evaluator) — strict
        #   2. require_approval (from either evaluator) — strict
        #   3. allow (both evaluators agree)
        # The old logic returned `d2` when `d1` wasn't `deny`,
        # which silently dropped the Constitution's
        # `require_approval` verdict in favor of RBAC's
        # permissive `allow`. That's a real safety bug:
        # a tool that the Constitution says requires
        # approval was being run without the operator's
        # consent. We now correctly combine the verdicts.
        if d1.verdict == "deny" or d2.verdict == "deny":
            return d1 if d1.verdict == "deny" else d2
        if d1.verdict == "require_approval" or d2.verdict == "require_approval":
            return d1 if d1.verdict == "require_approval" else d2
        return d1  # both allowed; return Constitution's reasoning for the audit trail

    def _materialize_task(self, plan, step) -> Task:  # type: ignore[no-untyped-def]
        return Task(
            id=TaskId(new_task_id()),
            automaton_id=self.ctx.automaton_id,
            kind=step.kind,
            payload={"step_index": step.index, "args": step.description},
            budget=step.estimated_cost,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )

    async def _execute_action(self, action: Action) -> None:
        # If the policy says this needs human approval, block on the gate
        # BEFORE any other work. The gate's callback wakes this coroutine
        # when an operator decides. This check runs first so we never
        # touch the tool registry for an action the human has vetoed.
        if (
            action.policy_decision.verdict.value == "require_approval"
            and self.approval_gate is not None
        ):
            # If the loop has been asked to stop while we wait, skip the
            # approval and the action. Otherwise the runner can hang.
            if self._stop.is_set():
                action.error = "loop_stopping"
                self.stats.actions += 1
                await self.ctx.persist_action(action)
                return
            self.ctx.record(
                "approval_submitted",
                {
                    "tool": action.tool_name,
                    "risk": action.risk,
                    "reason": action.policy_decision.reason,
                },
            )
            from services.approvals.approvals import ApprovalReason

            # Pass a short timeout so the loop can re-check its stop
            # signal periodically. The default approval TTL is 24h, but
            # a loop's lifetime is usually minutes — we don't want to
            # block the loop on a 24h approval.
            approval = await self.approval_gate.submit_and_await(
                automaton_id=str(self.ctx.automaton_id),
                tool_name=action.tool_name,
                arguments=action.arguments,
                risk=action.risk,
                cost_micro=action.cost_estimate.micro,
                currency=action.cost_estimate.currency,
                reasoning=action.policy_decision.reason,
                citations=action.policy_decision.citations,
                reason=ApprovalReason.CONSTITUTION,
                timeout_seconds=self.config.approval_timeout_seconds,
            )
            self.ctx.record(
                "approval_decided",
                {
                    "id": approval.id,
                    "verdict": approval.state.value,
                    "decided_by": (approval.decision.decided_by if approval.decision else None),
                },
            )
            if approval.state.value != "approved":
                # Rejected or expired — skip execution.
                action.error = f"approval {approval.state.value}"
                self.stats.actions += 1
                await self.ctx.persist_action(action)
                return
            # Approved — flip the verdict so downstream code treats it as
            # an allow. The original `require_approval` decision is
            # preserved in the action's `policy_decision` field.
            from core.types.automaton import PolicyVerdict

            action.policy_decision = action.policy_decision.model_copy(
                update={"verdict": PolicyVerdict.ALLOW}
            )

        tool = self.tools.get(action.tool_name)
        if tool is None:
            log.warning("unknown_tool", extra={"tool": action.tool_name})
            return

        start = time.time()
        outcome = "ok"
        try:
            result = await self.tools.invoke(action.tool_name, action.arguments)
            action.result = result
        except Exception as e:  # noqa: BLE001
            outcome = "error"
            action.error = f"{type(e).__name__}: {e}"
        finally:
            METRICS.tool_executions_total.labels(
                service=self.ctx.service, tool=action.tool_name, outcome=outcome
            ).inc()
            METRICS.tool_execution_duration_seconds.labels(
                service=self.ctx.service, tool=action.tool_name
            ).observe(time.time() - start)
            action.completed_at = datetime.now(tz=timezone.utc)
            self.stats.actions += 1
            await self.ctx.persist_action(action)

    async def _verify(self, actions: list[Action]) -> None:
        # Default verifier: check that each action either succeeded or had a
        # recorded, non-empty error. Plans are self-correcting: failures are
        # recorded and lower the planner's confidence on the next tick.
        for a in actions:
            if a.error:
                self.ctx.record(
                    "action_failed", {"id": str(a.id), "tool": a.tool_name, "err": a.error}
                )

    async def _learn(self, actions: list[Action], memory: list[MemoryEntry]) -> None:
        # The reasoner is allowed to retain nothing. Concrete implementations
        # may distill lessons into the procedural memory layer.
        lessons: list[str] = []
        for a in actions:
            if a.error:
                lessons.append(f"tool {a.tool_name} failed: {a.error}")
        if lessons:
            self.ctx.record("learned", {"n": len(lessons)})

    def _update_memory(self, actions: list[Action], reasoning: Any) -> None:
        for a in actions:
            self.ctx.write_memory(
                layer=MemoryLayer.DECISION_HISTORY,
                content=(
                    f"action id={a.id} tool={a.tool_name} verdict={a.policy_decision.verdict.value} "
                    f"error={a.error or 'none'}"
                ),
                importance=0.5,
                tags=[a.tool_name, a.policy_decision.evaluator],
            )

    async def _settle(self, actions: list[Action], cost_estimate: Money) -> None:
        # Charge the treasury for the cost of the executed actions. We trust
        # the per-tool cost estimate; precise billing happens in the metering
        # service asynchronously.
        for a in actions:
            if a.policy_decision.verdict.value == "allow":
                self.treasury.charge(
                    amount=a.cost_estimate,
                    category=f"tool:{a.tool_name}",
                    ref_type="action",
                    ref_id=str(a.id),
                )
                self.stats.spent = self.stats.spent + a.cost_estimate
        METRICS.treasury_balance.labels(
            service=self.ctx.service,
            automaton=str(self.ctx.automaton_id),
            currency=self.treasury.balance().currency,
        ).set(self.treasury.balance().micro)

    async def _sleep(self) -> None:
        # Sleep is decided by the reasoner if available, else bounded random.
        secs = max(self.config.sleep_min_seconds, min(self.config.sleep_max_seconds, 1.0))
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            return
