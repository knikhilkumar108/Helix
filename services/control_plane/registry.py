"""
In-memory automaton registry. Production deployments swap this for the
Postgres-backed implementation. The interface is the contract.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.errors.errors import ConflictError, NotFoundError
from core.security.signing import KeyPair
from core.types.automaton import (
    Automaton,
    Goal,
    LifecycleState,
    Plan,
    Task,
)
from core.types.identifiers import AutomatonId
from core.types.money import Money

from runtime.loop.treasury import InMemoryTreasury, Treasury


@dataclass(slots=True)
class _Entry:
    automaton: Automaton
    treasury: Treasury
    keypair: KeyPair
    goals: list[Goal] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    actions: list[Any] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class AutomatonRegistry:
    """Thread-safe in-memory registry.

    For multi-process deployments, replace with a Postgres-backed store
    using the same API. The locking discipline is the same: short critical
    sections, copy-on-read, append-only event log.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[AutomatonId, _Entry] = {}

    # ---- creation ----
    def create(
        self,
        *,
        name: str,
        genesis_prompt: str,
        parent_id: AutomatonId | None = None,
        initial_balance: Money | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Automaton:
        with self._lock:
            kp = KeyPair.generate()
            ts = datetime.now(tz=timezone.utc)
            automaton = Automaton(
                name=name,
                genesis_prompt=genesis_prompt,
                parent_id=parent_id,
                public_key=kp.public_b64(),
                wallet_address=f"atm_wallet_{kp.public_b64()[:12]}",
                created_at=ts,
                updated_at=ts,
                balance=initial_balance or Money.zero(),
                budget=Money.zero(),
                metadata=metadata or {},
            )
            if automaton.id in self._entries:
                raise ConflictError("automaton id collision")
            treasury = InMemoryTreasury(automaton.id, initial=automaton.balance)
            self._entries[automaton.id] = _Entry(
                automaton=automaton, treasury=treasury, keypair=kp
            )
            return automaton

    # ---- accessors ----
    def get(self, aid: AutomatonId) -> Automaton:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            # Return a snapshot with current balance.
            snap = e.automaton.model_copy(update={"balance": e.treasury.balance()})
            return snap

    def list(self) -> list[Automaton]:
        with self._lock:
            out: list[Automaton] = []
            for e in self._entries.values():
                out.append(e.automaton.model_copy(update={"balance": e.treasury.balance()}))
            return out

    def treasury(self, aid: AutomatonId) -> Treasury:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            return e.treasury

    def keypair(self, aid: AutomatonId) -> KeyPair:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            return e.keypair

    def goals(self, aid: AutomatonId) -> list[Goal]:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            return list(e.goals)

    def plans(self, aid: AutomatonId) -> list[Plan]:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            return list(e.plans)

    def tasks(self, aid: AutomatonId) -> list[Task]:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            return list(e.tasks)

    def actions(self, aid: AutomatonId) -> list[Any]:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            return list(e.actions)

    def events(self, aid: AutomatonId) -> list[dict[str, Any]]:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            return list(e.events)

    # ---- mutators ----
    def set_state(self, aid: AutomatonId, state: LifecycleState) -> None:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            e.automaton = e.automaton.model_copy(
                update={"state": state, "updated_at": datetime.now(tz=timezone.utc)}
            )

    def record_event(self, aid: AutomatonId, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            e.events.append(
                {"ts": time.time(), "kind": kind, "payload": payload}
            )

    def add_plan(self, aid: AutomatonId, plan: Plan) -> None:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            e.plans.append(plan)

    def add_action(self, aid: AutomatonId, action: Any) -> None:
        with self._lock:
            e = self._entries.get(aid)
            if e is None:
                raise NotFoundError(f"automaton not found: {aid}")
            e.actions.append(action)
