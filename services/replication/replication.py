"""
Replication framework. An Automaton may spawn a child that inherits:
  - the Constitution (immutable)
  - selected knowledge (curated, opt-in)
  - a brand new identity (key pair + wallet)
  - its own budget and treasury (seeded from the parent)

The parent is not liable for the child's actions. The child is autonomous.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core.errors.errors import (
    InsufficientFundsError,
    PolicyDeniedError,
    ValidationError,
)
from core.security.signing import KeyPair
from core.types.automaton import (
    Automaton,
    LifecycleState,
    MemoryLayer,
)
from core.types.identifiers import AutomatonId
from core.types.money import Money
from runtime.loop.treasury import InMemoryTreasury, Treasury

from services.control_plane.registry import AutomatonRegistry

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ReplicationPolicy:
    """Per-automaton rules for replication. Operators can extend."""

    allow_replication: bool = True
    max_children: int = 8
    seed_funds_micro: int = 1_000_000  # 1.00 USDC by default
    inherit_knowledge_layers: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                MemoryLayer.SEMANTIC.value,
                MemoryLayer.PROCEDURAL.value,
            }
        )
    )


def replicate(
    parent_id: AutomatonId,
    *,
    name: str,
    genesis_prompt: str,
    registry: AutomatonRegistry,
    policy: ReplicationPolicy,
    seed_metadata: dict[str, str] | None = None,
) -> Automaton:
    parent = registry.get(parent_id)
    if not policy.allow_replication:
        raise PolicyDeniedError("replication is disabled for this automaton")
    children = [a for a in registry.list() if a.parent_id == parent_id]
    if len(children) >= policy.max_children:
        raise ValidationError(
            f"max children reached ({policy.max_children})",
            context={"parent": str(parent_id), "max": policy.max_children},
        )
    parent_treasury: Treasury = registry.treasury(parent_id)
    seed = Money(policy.seed_funds_micro, parent.balance.currency)
    if parent_treasury.balance() < seed:
        raise InsufficientFundsError(
            "parent has insufficient funds to seed child",
            context={"parent": str(parent_id), "seed": str(seed)},
        )
    parent_treasury.charge(
        amount=seed,
        category="replication:seed",
        ref_type="automaton",
        memo=f"seed for child {name}",
    )
    child = registry.create(
        name=name,
        genesis_prompt=genesis_prompt,
        parent_id=parent_id,
        initial_balance=seed,
        metadata={
            "inherits_constitution": "v1",
            "inherits_layers": ",".join(sorted(policy.inherit_knowledge_layers)),
            **(seed_metadata or {}),
        },
    )
    registry.set_state(child.id, LifecycleState.CREATED)
    log.info(
        "replicated",
        extra={"parent": str(parent_id), "child": str(child.id), "seed_micro": seed.micro},
    )
    return child
