"""
Pydantic models for the core domain. These are used at service boundaries
(API, gRPC, DB serialization). Internally, the runtime uses lighter dataclasses
for hot paths.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .identifiers import (
    ActionId,
    AutomatonId,
    EventId,
    MemoryId,
    PlanId,
    TaskId,
    new_automaton_id,
)
from .money import Money


class LifecycleState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    SUSPENDED = "suspended"
    REPLICATING = "replicating"
    TERMINATED = "terminated"
    ARCHIVED = "archived"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyVerdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class MemoryLayer(str, Enum):
    WORKING = "working"
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    FINANCIAL = "financial"
    OPERATIONAL = "operational"
    CODE_HISTORY = "code_history"
    DECISION_HISTORY = "decision_history"
    RELATIONSHIP = "relationship"


class _Base(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=False, populate_by_name=True, arbitrary_types_allowed=True
    )


class Automaton(_Base):
    id: AutomatonId = Field(default_factory=new_automaton_id)
    name: str
    genesis_prompt: str
    parent_id: Optional[AutomatonId] = None
    public_key: str  # base64 Ed25519
    wallet_address: str
    state: LifecycleState = LifecycleState.CREATED
    created_at: datetime
    updated_at: datetime
    version: str = "0.1.0"
    reputation: float = Field(default=0.5, ge=0.0, le=1.0)
    base_currency: str = "USDC"
    balance: Money = Field(default_factory=lambda: Money.zero())
    budget: Money = Field(default_factory=lambda: Money.zero())
    metadata: dict[str, str] = Field(default_factory=dict)


class Goal(_Base):
    id: str
    description: str
    priority: int = Field(ge=0, le=100, default=50)
    expected_revenue: Money
    estimated_cost: Money
    probability: float = Field(ge=0.0, le=1.0, default=0.5)
    status: Literal["pending", "active", "completed", "failed", "cancelled"] = "pending"
    created_at: datetime
    completed_at: Optional[datetime] = None


class PlanStep(_Base):
    index: int
    kind: str  # tool | llm | external
    # description is intentionally permissive: a string for free-form steps
    # (e.g. an internal reflection note) or a dict of structured arguments
    # for a tool invocation. The runtime executor branches on the type.
    description: str | dict[str, Any]
    estimated_cost: Money
    risk: RiskLevel = RiskLevel.LOW
    depends_on: list[int] = Field(default_factory=list)


class Plan(_Base):
    id: PlanId
    automaton_id: AutomatonId
    goal_id: str
    steps: list[PlanStep]
    estimated_cost: Money
    expected_revenue: Money
    probability: float
    created_at: datetime
    status: Literal[
        "draft", "approved", "executing", "succeeded", "failed", "cancelled"
    ] = "draft"


class Task(_Base):
    id: TaskId
    automaton_id: AutomatonId
    kind: str
    payload: dict[str, Any]
    budget: Money
    deadline: Optional[datetime] = None
    status: Literal[
        "queued",
        "in_progress",
        "awaiting_payment",
        "succeeded",
        "failed",
        "expired",
        "cancelled",
    ] = "queued"
    result: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


class PolicyDecision(_Base):
    verdict: PolicyVerdict
    reason: str
    evaluated_at: datetime
    evaluator: str
    citations: list[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None


class Action(_Base):
    id: ActionId
    task_id: TaskId
    plan_id: PlanId
    tool_name: str
    arguments: dict[str, Any]
    risk: RiskLevel
    cost_estimate: Money
    policy_decision: PolicyDecision
    started_at: datetime
    completed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None


class MemoryEntry(_Base):
    id: MemoryId
    automaton_id: AutomatonId
    layer: MemoryLayer
    content: str
    embedding: Optional[list[float]] = None
    importance: float = Field(ge=0.0, le=1.0, default=0.5)
    ttl: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    tags: list[str] = Field(default_factory=list)


class TreasuryEntry(_Base):
    id: str
    automaton_id: AutomatonId
    kind: Literal["credit", "debit"]
    amount: Money
    category: str
    ref_type: Optional[str] = None
    ref_id: Optional[str] = None
    occurred_at: datetime
    memo: Optional[str] = None


class ToolSpec(_Base):
    name: str
    version: str
    description: str
    capabilities: list[str]
    risk: RiskLevel
    cost: Money
    rate_limit: Optional[dict[str, int]] = None
    sandbox: Literal["none", "process", "container", "microvm"] = "process"
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        # Allow dotted tool names like "browser.click" — alphanumeric, underscores, dots.
        if not re.match(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$", v):
            raise ValueError("tool name must be alphanumeric/underscore/dot")
        return v


class ComponentHealth(_Base):
    status: Literal["up", "down", "degraded"]
    latency_ms: Optional[float] = None
    message: Optional[str] = None


class HealthReport(_Base):
    status: Literal["healthy", "degraded", "unhealthy"]
    components: dict[str, ComponentHealth]
    checked_at: datetime
