"""
Reasoner interface. The default reasoner uses the LLM router; tests can
substitute a deterministic stub.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.types.automaton import MemoryEntry
from core.types.identifiers import AutomatonId


@dataclass(slots=True)
class ReasoningResult:
    summary: str
    queries: list[str] = field(default_factory=list)
    confidence: float = 0.5
    strategy: str = "default"
    raw: dict[str, Any] = field(default_factory=dict)


class Reasoner(Protocol):
    async def think(self, observation: dict[str, Any], ctx: Any) -> ReasoningResult: ...


class StubReasoner:
    """A reasoner that always returns a fixed result. Useful for unit tests."""

    def __init__(self, summary: str = "noop", queries: Iterable[str] | None = None) -> None:
        self._summary = summary
        self._queries = list(queries or [])

    async def think(self, observation: dict[str, Any], ctx: Any) -> ReasoningResult:
        return ReasoningResult(
            summary=self._summary,
            queries=self._queries or ["status", "next"],
            confidence=0.5,
            strategy="stub",
        )
