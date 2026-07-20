"""
Loop context: a per-Automaton handle that exposes everything the loop needs:
observation, memory, persisted action sink, and structured event recording.

The runtime never knows about Postgres, NATS, or Redis directly — the LoopContext
abstraction is implemented in `services.runtime.context_impl`.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.types.automaton import MemoryEntry
from core.types.identifiers import ActionId, AutomatonId, MemoryId, new_memory_id

log = logging.getLogger(__name__)


class LoopContext(Protocol):
    service: str
    automaton_id: AutomatonId

    def record(self, kind: str, payload: dict[str, Any]) -> None: ...
    def observe(self) -> dict[str, Any]: ...
    async def recall(self, queries: list[str]) -> list[MemoryEntry]: ...
    def write_memory(
        self,
        *,
        layer: str,
        content: str,
        importance: float,
        tags: list[str] | None = None,
    ) -> MemoryId: ...
    async def persist_action(self, action: Any) -> None: ...
    def memory_pointer(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class InMemoryLoopContext:
    """Default in-process context. Used for tests and the embedded runtime."""

    service: str
    automaton_id: AutomatonId
    memory: list[MemoryEntry] = field(default_factory=list)
    actions: list[Any] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # `extra` is an open dict for callers to attach shared
    # state — same idea as `ToolRegistry.extra`. The observation
    # code reads `extra["inbox"]` to surface pending message
    # counts; the runtime's `loop_init` sets it.
    extra: dict[str, Any] = field(default_factory=dict)

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))

    def observe(self) -> dict[str, Any]:
        # Last few events as observation. The observation is a
        # snapshot of the agent's current state — what just
        # happened, what the wall clock says, and what's waiting
        # in the inbox.
        obs: dict[str, Any] = {
            "events": list(self.events[-10:]),
            "now": __import__("time").time(),
        }
        # If the registry has an inbox attached, surface the
        # pending message count. The agent decides whether to
        # claim — observation only signals *that* work is waiting,
        # not what to do about it.
        inbox = self.extra.get("inbox") if hasattr(self, "extra") else None
        if inbox is not None:
            from services.messaging import InboxService  # local import
            if isinstance(inbox, InboxService):
                stats = inbox.stats(self.automaton_id)
                pending = stats["received"] + stats["in_progress"]
                obs["inbox"] = {
                    "pending": pending,
                    "received": stats["received"],
                    "in_progress": stats["in_progress"],
                    "cap": stats["cap"],
                }
        return obs

    async def recall(self, queries: list[str]) -> list[MemoryEntry]:
        if not queries:
            return list(self.memory[-5:])
        # Trivial keyword search — real impl uses the vector store.
        q = " ".join(queries).lower().split()
        return [m for m in self.memory if any(tok in m.content.lower() for tok in q)][:5]

    def write_memory(
        self,
        *,
        layer: str,
        content: str,
        importance: float,
        tags: list[str] | None = None,
    ) -> MemoryId:
        import time
        from datetime import datetime, timezone

        from core.types.identifiers import MemoryId as _Mid
        from core.types.automaton import MemoryEntry as _Me
        from core.types.automaton import MemoryLayer as _Ml

        mid = _Mid(new_memory_id())
        m = _Me(
            id=mid,
            automaton_id=self.automaton_id,
            layer=_Ml(layer),
            content=content,
            importance=importance,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
            tags=list(tags or []),
        )
        self.memory.append(m)
        return mid

    async def persist_action(self, action: Any) -> None:
        self.actions.append(action)

    def memory_pointer(self) -> dict[str, Any]:
        return {"size": len(self.memory), "actions": len(self.actions)}
