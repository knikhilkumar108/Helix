"""
Planner. Decomposes a reasoning result into a concrete Plan with steps,
each annotated with an estimated cost and risk.

The planner is intentionally simple: it delegates step content to the
reasoner (via the LLM router) and applies the Constitution-derived
risk classification. Production planners will use tree-of-thought or
Monte-Carlo Tree Search; this skeleton provides the contract.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from core.types.automaton import (
    MemoryEntry,
    Plan,
    PlanStep,
    RiskLevel,
)
from core.types.identifiers import AutomatonId, PlanId, new_plan_id
from core.types.money import Money

from .reasoner import ReasoningResult


@dataclass(slots=True)
class PlannerConfig:
    default_currency: str = "USDC"
    max_steps: int = 8
    risk_classifier: dict[str, RiskLevel] = field(
        default_factory=lambda: {
            "shell.exec": RiskLevel.HIGH,
            "fs.write": RiskLevel.MEDIUM,
            "fs.read": RiskLevel.LOW,
            "http.get": RiskLevel.LOW,
            "http.post": RiskLevel.MEDIUM,
            "browser.act": RiskLevel.HIGH,
            "memory.write": RiskLevel.LOW,
            "memory.read": RiskLevel.LOW,
            "tool.invoke": RiskLevel.MEDIUM,
        }
    )


class Planner(Protocol):
    async def plan(
        self,
        reasoning: ReasoningResult,
        memory: list[MemoryEntry],
        ctx: Any,
    ) -> Plan: ...


class StubPlanner:
    """Plans a single noop step. Used by tests."""

    async def plan(
        self,
        reasoning: ReasoningResult,
        memory: list[MemoryEntry],
        ctx: Any,
    ) -> Plan:
        currency = "USDC"
        step = PlanStep(
            index=0,
            kind="memory.write",
            description=reasoning.summary,
            estimated_cost=Money.zero(currency),
            risk=RiskLevel.LOW,
            depends_on=[],
        )
        return Plan(
            id=PlanId(new_plan_id()),
            automaton_id=ctx.automaton_id,
            goal_id=uuid.uuid4().hex,
            steps=[step],
            estimated_cost=Money.zero(currency),
            expected_revenue=Money.zero(currency),
            probability=reasoning.confidence,
            created_at=datetime.now(tz=timezone.utc),
        )


class HeuristicPlanner:
    """Plan reasoning.summary into N steps based on the risk classifier and
    a small set of canned strategies.

    When the upstream reasoner has placed a structured `next_action` in
    `reasoning.raw` (as `LLMReasoner` does), we honor it: one plan step
    per requested tool call, with the LLM-supplied arguments. This is
    the path that turns the LLM's intent into an actual execution.
    """

    def __init__(self, config: PlannerConfig | None = None) -> None:
        self.config = config or PlannerConfig()

    async def plan(
        self,
        reasoning: ReasoningResult,
        memory: list[MemoryEntry],
        ctx: Any,
    ) -> Plan:
        currency = self.config.default_currency
        steps: list[PlanStep] = []

        # Honor the LLM's structured action if present. If it isn't, fall
        # back to writing a memory note of the summary so the agent retains
        # its own reasoning even when it has no tool to call.
        next_action = (reasoning.raw or {}).get("next_action") if reasoning.raw else None

        if isinstance(next_action, dict) and next_action.get("tool"):
            tool = next_action["tool"]
            args = next_action.get("arguments") or {}
            if not isinstance(args, dict):
                args = {"value": args}
            risk = self.config.risk_classifier.get(tool, RiskLevel.MEDIUM)
            steps.append(
                PlanStep(
                    index=0,
                    kind=tool,
                    description=args,
                    estimated_cost=Money.from_major("0.001", currency),
                    risk=risk,
                    depends_on=[],
                )
            )
        else:
            # No LLM-supplied tool (either no next_action, or tool=null).
            # Record the summary as a memory note. We use a properly-shaped
            # dict so the memory.write tool finds the required `content` arg.
            steps.append(
                PlanStep(
                    index=0,
                    kind="memory.write",
                    description={
                        "content": reasoning.summary,
                        "layer": "long_term",
                        "importance": 0.5,
                    },
                    estimated_cost=Money.zero(currency),
                    risk=RiskLevel.LOW,
                    depends_on=[],
                )
            )

        # Heuristic: if the summary suggests fetching/searching and the LLM
        # didn't already pick a tool, add a web fetch.
        last_kind = steps[-1].kind if steps else None
        if last_kind == "memory.write" and any(
            s in reasoning.summary.lower() for s in ("fetch", "search", "browse")
        ):
            steps.append(
                PlanStep(
                    index=len(steps),
                    kind="http.get",
                    description={"url": "https://example.com"},
                    estimated_cost=Money.from_major("0.001", currency),
                    risk=self.config.risk_classifier.get("http.get", RiskLevel.LOW),
                    depends_on=[0],
                )
            )

        steps = steps[: self.config.max_steps]
        total = Money.zero(currency)
        for s in steps:
            total = total + s.estimated_cost
        return Plan(
            id=PlanId(new_plan_id()),
            automaton_id=ctx.automaton_id,
            goal_id=uuid.uuid4().hex,
            steps=steps,
            estimated_cost=total,
            expected_revenue=Money.zero(currency),
            probability=reasoning.confidence,
            created_at=datetime.now(tz=timezone.utc),
        )
