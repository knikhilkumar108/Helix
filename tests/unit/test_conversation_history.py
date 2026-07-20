"""Tests for the conversation history service."""
from __future__ import annotations

import time

import pytest

from services.conversation import (
    ConversationHistory,
    Role,
    Turn,
    estimate_tokens,
    make_history,
)


# ── estimate_tokens ───────────────────────────────────────


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_short():
    # 4 chars → 1 token (min).
    assert estimate_tokens("hi") == 1


def test_estimate_tokens_long():
    # 400 chars → 100 tokens.
    assert estimate_tokens("x" * 400) == 100


# ── ConversationHistory construction ──────────────────────


def test_default_construction():
    h = ConversationHistory()
    assert h.budget_tokens == 4000
    assert h.max_turns == 200
    assert len(h) == 0


def test_invalid_budget_raises():
    with pytest.raises(ValueError):
        ConversationHistory(budget_tokens=0)
    with pytest.raises(ValueError):
        ConversationHistory(budget_tokens=-1)


def test_invalid_max_turns_raises():
    with pytest.raises(ValueError):
        ConversationHistory(max_turns=0)


def test_invalid_threshold_raises():
    with pytest.raises(ValueError):
        ConversationHistory(summary_threshold=0)
    with pytest.raises(ValueError):
        ConversationHistory(summary_threshold=1.5)


# ── add_turn ────────────────────────────────────────────


def test_add_turn_appends():
    h = ConversationHistory()
    t = h.add_turn(role=Role.USER, content="hello")
    assert len(h) == 1
    assert t.role == Role.USER
    assert t.content == "hello"
    assert t.id.startswith("turn_")


def test_add_turn_with_tool_calls():
    h = ConversationHistory()
    t = h.add_turn(
        role=Role.AGENT,
        content="",
        tool_calls=[{"name": "fs.read", "args": {"path": "/x"}}],
    )
    assert t.tool_calls == [{"name": "fs.read", "args": {"path": "/x"}}]


def test_add_turn_with_metadata():
    h = ConversationHistory()
    t = h.add_turn(
        role=Role.AGENT,
        content="hi",
        metadata={"tier": "normal", "balance_micro": 5000},
    )
    assert t.metadata["tier"] == "normal"
    assert t.metadata["balance_micro"] == 5000


def test_add_turn_respects_max_turns():
    h = ConversationHistory(max_turns=3)
    for i in range(5):
        h.add_turn(role=Role.USER, content=f"m{i}")
    assert len(h) == 3
    # The first two are dropped; we keep the last three.
    assert h.turns[0].content == "m2"
    assert h.turns[1].content == "m3"
    assert h.turns[2].content == "m4"


# ── estimated_tokens ────────────────────────────────────


def test_estimated_tokens_zero_for_empty():
    h = ConversationHistory()
    assert h.estimated_tokens() == 0


def test_estimated_tokens_sums_content():
    h = ConversationHistory()
    h.add_turn(role=Role.USER, content="x" * 100)  # 25 tokens
    h.add_turn(role=Role.AGENT, content="y" * 200)  # 50 tokens
    assert h.estimated_tokens() == 75


# ── compact ────────────────────────────────────────────


def test_compact_under_threshold_is_noop():
    h = ConversationHistory(budget_tokens=1000, summary_threshold=0.8)
    h.add_turn(role=Role.USER, content="short")
    n = h.compact()
    assert n == 0
    assert len(h) == 1


def test_compact_collapses_old_turns():
    h = ConversationHistory(budget_tokens=400, summary_threshold=0.5)
    # 10 turns of 100 chars each = 250 tokens (over the 200 threshold).
    for i in range(10):
        h.add_turn(role=Role.USER, content=f"message {i} " + "x" * 80)
    # Compact triggers automatically on add_turn when over threshold.
    # Verify: history is shorter than 10.
    assert len(h) < 10
    # The remaining history has a SUMMARY turn.
    assert any(t.role == Role.SUMMARY for t in h.turns)


def test_compact_keeps_recent_turns_intact():
    h = ConversationHistory(budget_tokens=400, summary_threshold=0.5)
    for i in range(10):
        h.add_turn(role=Role.USER, content=f"old {i} " + "x" * 80)
    # Add a clearly recent turn.
    h.add_turn(role=Role.AGENT, content="this is the most recent turn")
    # The recent turn should still be in the history verbatim.
    assert any(t.content == "this is the most recent turn" for t in h.turns)


def test_compact_records_summary_of():
    h = ConversationHistory(budget_tokens=400, summary_threshold=0.5)
    for i in range(10):
        h.add_turn(role=Role.USER, content=f"old {i} " + "x" * 80)
    summary_turns = [t for t in h.turns if t.role == Role.SUMMARY]
    assert summary_turns
    # The summary turn should reference the collapsed turns' ids.
    assert len(summary_turns[0].summary_of) > 0


# ── render_for_llm ────────────────────────────────────


def test_render_for_llm_basic():
    h = ConversationHistory()
    h.add_turn(role=Role.USER, content="hi")
    h.add_turn(role=Role.AGENT, content="hello")
    msgs = h.render_for_llm()
    assert len(msgs) == 2
    assert msgs[0] == {"role": "user", "content": "hi"}
    assert msgs[1] == {"role": "assistant", "content": "hello"}


def test_render_for_llm_role_mapping():
    h = ConversationHistory()
    h.add_turn(role=Role.SYSTEM, content="sys")
    h.add_turn(role=Role.TOOL, content="tool result")
    h.add_turn(role=Role.SUMMARY, content="summary text")
    msgs = h.render_for_llm()
    roles = [m["role"] for m in msgs]
    assert "system" in roles
    assert "tool" in roles
    # SUMMARY is mapped to "user" with the original content.
    assert "user" in roles


def test_render_for_llm_respects_budget():
    h = ConversationHistory(budget_tokens=100)
    for i in range(20):
        h.add_turn(role=Role.USER, content=f"msg {i} " + "y" * 200)
    msgs = h.render_for_llm()
    # The total content length should be bounded.
    total_chars = sum(len(m["content"]) for m in msgs)
    # The render picks the most recent turns that fit.
    assert total_chars <= 100 * 4 + 200  # generous bound


def test_render_for_llm_includes_tool_calls_on_agent_turn():
    h = ConversationHistory()
    h.add_turn(
        role=Role.AGENT,
        content="",
        tool_calls=[{"id": "call_1", "name": "fs.read", "args": {"path": "/x"}}],
    )
    msgs = h.render_for_llm()
    assert len(msgs) == 1
    assert "tool_calls" in msgs[0]
    assert msgs[0]["tool_calls"][0]["name"] == "fs.read"


def test_render_for_llm_includes_tool_call_id_on_tool_turn():
    h = ConversationHistory()
    h.add_turn(
        role=Role.AGENT,
        content="",
        tool_calls=[{"id": "call_1", "name": "fs.read", "args": {"path": "/x"}}],
    )
    h.add_turn(
        role=Role.TOOL,
        content="file contents",
        tool_results=[{"id": "call_1", "output": "file contents"}],
    )
    msgs = h.render_for_llm()
    tool_msg = [m for m in msgs if m["role"] == "tool"][0]
    assert tool_msg["tool_call_id"] == "call_1"


def test_render_for_llm_omits_tool_results_when_disabled():
    h = ConversationHistory()
    h.add_turn(role=Role.USER, content="hi")
    h.add_turn(role=Role.TOOL, content="tool result")
    h.add_turn(role=Role.AGENT, content="done")
    msgs_all = h.render_for_llm(include_tool_results=True)
    msgs_no_tool = h.render_for_llm(include_tool_results=False)
    assert any(m["role"] == "tool" for m in msgs_all)
    assert not any(m["role"] == "tool" for m in msgs_no_tool)


# ── clear ──────────────────────────────────────────────


def test_clear_empties_history():
    h = ConversationHistory()
    h.add_turn(role=Role.USER, content="a")
    h.add_turn(role=Role.USER, content="b")
    h.clear()
    assert len(h) == 0


# ── make_history factory ──────────────────────────────


def test_make_history_defaults():
    h = make_history()
    assert h.budget_tokens == 4000
    assert h.max_turns == 200


def test_make_history_with_custom_budget():
    h = make_history(budget_tokens=1000, max_turns=50)
    assert h.budget_tokens == 1000
    assert h.max_turns == 50
