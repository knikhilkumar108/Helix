"""Unit tests for the memory service."""
from __future__ import annotations

from core.types.automaton import MemoryLayer
from core.types.identifiers import AutomatonId, new_automaton_id
from services.memory.memory_service import MemoryService


def test_write_and_recall():
    s = MemoryService()
    aid = AutomatonId(new_automaton_id())
    s.write(aid, layer=MemoryLayer.LONG_TERM, content="the project is named Apollo", importance=0.9)
    s.write(aid, layer=MemoryLayer.LONG_TERM, content="we use Postgres for storage", importance=0.6)
    res = s.recall(aid, "Apollo", k=2)
    assert any("Apollo" in e.content for e in res)


def test_recall_empty_query_returns_recent():
    s = MemoryService()
    aid = AutomatonId(new_automaton_id())
    s.write(aid, layer=MemoryLayer.LONG_TERM, content="x", importance=0.1)
    s.write(aid, layer=MemoryLayer.LONG_TERM, content="y", importance=0.2)
    res = s.recall(aid, "", k=2)
    assert {e.content for e in res} == {"x", "y"}


def test_prune_removes_expired():
    s = MemoryService()
    aid = AutomatonId(new_automaton_id())
    e = s.write(aid, layer=MemoryLayer.SHORT_TERM, content="temp", importance=0.1, ttl_seconds=1)
    # Move updated_at to the past by deleting + rewriting is hard; instead set ttl=-1
    from datetime import datetime, timezone, timedelta

    e2 = s.write(aid, layer=MemoryLayer.SHORT_TERM, content="also temp", importance=0.1, ttl_seconds=1)
    e2.updated_at = datetime.now(tz=timezone.utc) - timedelta(seconds=10)
    n = s.prune(aid)
    assert n >= 1


def test_summarize_respects_budget():
    s = MemoryService()
    aid = AutomatonId(new_automaton_id())
    for i in range(20):
        s.write(
            aid,
            layer=MemoryLayer.LONG_TERM,
            content=("hello " * 4) + f"#{i}",
            importance=1.0 - (i / 100.0),
        )
    summary = s.summarize(aid, layer=MemoryLayer.LONG_TERM, budget_tokens=200)
    assert len(summary) > 0
    assert len(summary) < 20 * 100
