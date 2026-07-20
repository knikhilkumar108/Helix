"""Integration tests for the conversation history wired into the loop."""
from __future__ import annotations

import pytest

from core.types.identifiers import new_automaton_id
from runtime.loop.loop_init import build_default_loop
from services.conversation import ConversationHistory, Role, make_history


def test_history_attached_to_loop():
    aid = new_automaton_id()
    h = make_history()
    loop = build_default_loop(aid, history=h)
    # The tool registry has the history.
    assert loop.tools.extra.get("history") is h
    # The loop context has the history.
    assert loop.ctx.extra.get("history") is h


def test_history_record_tool():
    aid = new_automaton_id()
    h = make_history()
    loop = build_default_loop(aid, history=h)
    fn = loop.tools._tools["chat.history.record"].fn  # type: ignore[attr-defined]
    result = fn(role="user", content="hello world")
    assert result["role"] == "user"
    assert result["tokens"] > 0
    assert result["turns"] == 1
    assert h.turns[0].content == "hello world"


def test_history_render_tool():
    aid = new_automaton_id()
    h = make_history()
    loop = build_default_loop(aid, history=h)
    record = loop.tools._tools["chat.history.record"].fn  # type: ignore[attr-defined]
    record(role="user", content="hi")
    record(role="agent", content="hello there")
    render = loop.tools._tools["chat.history.render"].fn  # type: ignore[attr-defined]
    result = render()
    assert len(result["messages"]) == 2
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][1]["role"] == "assistant"


def test_history_compact_tool():
    aid = new_automaton_id()
    h = ConversationHistory(budget_tokens=200, summary_threshold=0.5)
    loop = build_default_loop(aid, history=h)
    record = loop.tools._tools["chat.history.record"].fn  # type: ignore[attr-defined]
    for i in range(10):
        record(role="user", content=f"msg {i} " + "x" * 80)
    # Compact should have been triggered automatically by add_turn.
    # The tool's result reflects the current state.
    compact = loop.tools._tools["chat.history.compact"].fn  # type: ignore[attr-defined]
    result = compact()
    assert "tokens" in result
    assert "turns" in result


def test_history_tool_without_history_raises_clear_error():
    aid = new_automaton_id()
    loop = build_default_loop(aid)  # no history
    record = loop.tools._tools["chat.history.record"].fn  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError) as exc:
        record(role="user", content="x")
    assert "ConversationHistory" in str(exc.value)


def test_multi_turn_conversation_simulation():
    """Simulate a 5-turn conversation. The history should grow
    linearly; the rendered output should be in the right
    order (oldest-first)."""
    aid = new_automaton_id()
    h = make_history(budget_tokens=2000)
    loop = build_default_loop(aid, history=h)
    record = loop.tools._tools["chat.history.record"].fn  # type: ignore[attr-defined]
    render = loop.tools._tools["chat.history.render"].fn  # type: ignore[attr-defined]
    # Simulate 5 user/agent exchanges.
    for i in range(5):
        record(role="user", content=f"question {i}")
        record(role="agent", content=f"answer {i}")
    # All 10 turns are in the history.
    assert len(h) == 10
    # Rendered in order.
    result = render()
    assert len(result["messages"]) == 10
    assert result["messages"][0]["content"] == "question 0"
    assert result["messages"][-1]["content"] == "answer 4"
