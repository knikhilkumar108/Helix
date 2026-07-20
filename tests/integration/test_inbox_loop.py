"""Integration tests for the inbox wired into the runtime loop.

Verifies:
  - The agent can call `messaging.send` to enqueue a message
    for another agent.
  - The agent can call `messaging.claim` to receive its pending
    messages.
  - The observation step surfaces the pending message count.
  - The full lifecycle (send → claim → process) works end-to-end.
"""
from __future__ import annotations

import asyncio

import pytest

from core.types.identifiers import new_automaton_id
from runtime.loop.loop_init import build_default_loop
from runtime.loop.tools import ToolRegistry
from services.messaging import InboxService
from services.state.sqlite_store import SqliteStore


# ── Helpers ──────────────────────────────────────────────────


def _make_loop(aid, *, inbox: InboxService | None = "auto"):
    """Build a default loop with an optional inbox attached.

    `inbox="auto"` (the default) creates a fresh in-memory
    inbox. `inbox=None` builds a loop with no inbox at all
    (used to test the "no inbox wired" error path). Pass an
    explicit `InboxService` to share it across loops.
    """
    if inbox == "auto":
        inbox = InboxService(backend=SqliteStore(":memory:"))
    return build_default_loop(aid, inbox=inbox)


# ── Tool: send and claim ──────────────────────────────────


def test_messaging_send_tool_enqueues_a_message():
    aid_alice = new_automaton_id()
    aid_bob = new_automaton_id()
    # Alice and Bob share an inbox service (in production, this
    # is the platform's global inbox). For tests we share a
    # fresh in-memory store.
    store = SqliteStore(":memory:")
    alice_inbox = InboxService(backend=store)
    bob_inbox = InboxService(backend=store)
    loop = _make_loop(aid_alice, inbox=alice_inbox)
    # Find the messaging.send tool.
    send_tool = loop.tools.get("messaging.send")
    assert send_tool is not None
    # Call it.
    fn = loop.tools._tools["messaging.send"].fn  # type: ignore[attr-defined]
    result = fn(to=str(aid_bob), content="hi bob")
    assert result["to"] == str(aid_bob)
    assert result["from"] == str(aid_alice)
    assert result["state"] == "received"
    # Bob's inbox should see the message.
    msgs = bob_inbox.peek(str(aid_bob), limit=10)
    assert len(msgs) == 1
    assert msgs[0].content == "hi bob"


def test_messaging_claim_tool_moves_to_in_progress():
    aid_alice = new_automaton_id()
    aid_bob = new_automaton_id()
    store = SqliteStore(":memory:")
    alice_inbox = InboxService(backend=store)
    bob_inbox = InboxService(backend=store)
    # Alice sends two messages to Bob.
    alice_inbox.send(from_address=str(aid_alice), to_address=str(aid_bob), content="m1")
    alice_inbox.send(from_address=str(aid_alice), to_address=str(aid_bob), content="m2")
    # Build Bob's loop.
    loop = _make_loop(aid_bob, inbox=bob_inbox)
    claim_fn = loop.tools._tools["messaging.claim"].fn  # type: ignore[attr-defined]
    result = claim_fn(to=str(aid_bob), limit=10)
    assert result["count"] == 2
    assert all(m["state"] == "in_progress" for m in result["messages"])


def test_messaging_mark_processed_terminates_a_message():
    aid_alice = new_automaton_id()
    aid_bob = new_automaton_id()
    store = SqliteStore(":memory:")
    alice_inbox = InboxService(backend=store)
    bob_inbox = InboxService(backend=store)
    alice_inbox.send(from_address=str(aid_alice), to_address=str(aid_bob), content="x")
    loop = _make_loop(aid_bob, inbox=bob_inbox)
    claim_fn = loop.tools._tools["messaging.claim"].fn  # type: ignore[attr-defined]
    result = claim_fn(to=str(aid_bob), limit=10)
    msg_id = result["messages"][0]["id"]
    # Mark processed.
    mark_fn = loop.tools._tools["messaging.mark_processed"].fn  # type: ignore[attr-defined]
    n = mark_fn(ids=[msg_id])
    assert n["marked"] == 1
    # A second claim sees nothing.
    again = claim_fn(to=str(aid_bob), limit=10)
    assert again["count"] == 0


# ── Observation surfaces pending count ────────────────────


def test_observation_surfaces_inbox_pending_count():
    aid = new_automaton_id()
    other = new_automaton_id()
    store = SqliteStore(":memory:")
    inbox = InboxService(backend=store)
    # Three pending messages.
    for i in range(3):
        inbox.send(from_address=str(other), to_address=str(aid), content=f"m{i}")
    loop = _make_loop(aid, inbox=inbox)
    obs = loop.ctx.observe()
    assert "inbox" in obs
    assert obs["inbox"]["pending"] == 3
    assert obs["inbox"]["received"] == 3
    assert obs["inbox"]["cap"] == 1000


def test_observation_pending_decreases_after_claim():
    aid = new_automaton_id()
    other = new_automaton_id()
    store = SqliteStore(":memory:")
    inbox = InboxService(backend=store)
    for i in range(2):
        inbox.send(from_address=str(other), to_address=str(aid), content=f"m{i}")
    loop = _make_loop(aid, inbox=inbox)
    claim_fn = loop.tools._tools["messaging.claim"].fn  # type: ignore[attr-defined]
    claim_fn(to=str(aid), limit=10)
    obs = loop.ctx.observe()
    # Pending = received (0) + in_progress (2) = 2.
    assert obs["inbox"]["pending"] == 2
    assert obs["inbox"]["received"] == 0
    assert obs["inbox"]["in_progress"] == 2


# ── End-to-end: agent-to-agent round-trip ────────────────


def test_two_agents_exchange_messages_via_shared_inbox():
    """Alice asks Bob a question; Bob claims it, processes it,
    marks it done. This is the canonical agent-to-agent pattern."""
    aid_alice = new_automaton_id()
    aid_bob = new_automaton_id()
    store = SqliteStore(":memory:")
    alice_inbox = InboxService(backend=store)
    bob_inbox = InboxService(backend=store)
    # Build both loops.
    alice_loop = _make_loop(aid_alice, inbox=alice_inbox)
    bob_loop = _make_loop(aid_bob, inbox=bob_inbox)
    # Alice sends Bob a question.
    send_fn = alice_loop.tools._tools["messaging.send"].fn  # type: ignore[attr-defined]
    send_result = send_fn(to=str(aid_bob), content="What is 2+2?")
    msg_id = send_result["id"]
    # Bob claims it.
    claim_fn = bob_loop.tools._tools["messaging.claim"].fn  # type: ignore[attr-defined]
    claimed = claim_fn(to=str(aid_bob), limit=10)
    assert claimed["count"] == 1
    assert claimed["messages"][0]["content"] == "What is 2+2?"
    # Bob "processes" (here, just marks it done).
    mark_fn = bob_loop.tools._tools["messaging.mark_processed"].fn  # type: ignore[attr-defined]
    n = mark_fn(ids=[msg_id])
    assert n["marked"] == 1
    # Stats reflect the end state.
    bob_stats = bob_inbox.stats(str(aid_bob))
    assert bob_stats["received"] == 0
    assert bob_stats["in_progress"] == 0
    assert bob_stats["processed"] == 1


# ── No inbox wired → tools fail loudly ──────────────────


def test_messaging_tool_without_inbox_raises_clear_error():
    aid = new_automaton_id()
    loop = _make_loop(aid, inbox=None)  # explicitly no inbox
    send_fn = loop.tools._tools["messaging.send"].fn  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError) as exc:
        send_fn(to="atm_bob", content="hi")
    assert "InboxService" in str(exc.value)


# ── Tool registry extra dict ─────────────────────────────


def test_tool_registry_extra_is_an_open_dict():
    """The `extra` dict is a clean way to attach shared state
    to a registry without polluting the API."""
    tools = ToolRegistry()
    assert tools.extra == {}
    tools.extra["hello"] = "world"
    assert tools.extra["hello"] == "world"
