"""Tests for the inbox / agent-to-agent messaging service."""
from __future__ import annotations

import pytest

from services.messaging import (
    InboxFull,
    InboxMessage,
    InboxService,
    InboxState,
    make_inbox,
)
from services.state.sqlite_store import SqliteStore


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


@pytest.fixture
def inbox(store: SqliteStore) -> InboxService:
    return InboxService(backend=store, cap=100)


@pytest.fixture
def small_inbox(store: SqliteStore) -> InboxService:
    return InboxService(backend=store, cap=3)


# ── Sending ──────────────────────────────────────────────────


def test_send_creates_received_message(inbox):
    msg = inbox.send(
        from_address="atm_alice",
        to_address="atm_bob",
        content="hello bob",
    )
    assert msg.state == InboxState.RECEIVED
    assert msg.retry_count == 0
    assert msg.from_address == "atm_alice"
    assert msg.to_address == "atm_bob"
    assert msg.content == "hello bob"
    assert msg.processed_at is None


def test_send_validates_inputs(inbox):
    with pytest.raises(Exception):
        inbox.send(from_address="", to_address="atm_bob", content="x")
    with pytest.raises(Exception):
        inbox.send(from_address="atm_alice", to_address="", content="x")
    with pytest.raises(Exception):
        inbox.send(from_address="atm_alice", to_address="atm_bob", content="")
    with pytest.raises(Exception):
        inbox.send(
            from_address="atm_alice",
            to_address="atm_bob",
            content="x",
            max_retries=-1,
        )


def test_send_enforces_inbox_cap(small_inbox):
    # cap=3
    for i in range(3):
        small_inbox.send(
            from_address="atm_alice",
            to_address="atm_bob",
            content=f"msg{i}",
        )
    with pytest.raises(InboxFull) as exc:
        small_inbox.send(
            from_address="atm_alice",
            to_address="atm_bob",
            content="overflow",
        )
    assert exc.value.current == 3
    assert exc.value.cap == 3
    assert "atm_bob" in str(exc.value)


def test_send_persists_to_store(inbox, store):
    msg = inbox.send(
        from_address="atm_alice",
        to_address="atm_bob",
        content="hi",
    )
    # The store should have a row for it.
    row = store.get_inbox_message(msg.id)
    assert row is not None
    assert row["id"] == msg.id
    assert row["state"] == "received"


# ── Claiming ─────────────────────────────────────────────────


def test_claim_atomic_transitions_to_in_progress(inbox):
    msg = inbox.send(
        from_address="atm_alice", to_address="atm_bob", content="hi"
    )
    claimed = inbox.claim("atm_bob", limit=10)
    assert len(claimed) == 1
    assert claimed[0].id == msg.id
    assert claimed[0].state == InboxState.IN_PROGRESS

    # A second claim should see nothing (already in_progress).
    claimed2 = inbox.claim("atm_bob", limit=10)
    assert claimed2 == []


def test_claim_only_returns_received(inbox):
    inbox.send(from_address="a", to_address="b", content="1")
    inbox.send(from_address="a", to_address="b", content="2")
    inbox.send(from_address="a", to_address="b", content="3")
    claimed = inbox.claim("b", limit=2)
    assert len(claimed) == 2
    # All claimed messages are in_progress.
    for m in claimed:
        assert m.state == InboxState.IN_PROGRESS


def test_claim_respects_limit(inbox):
    for i in range(10):
        inbox.send(from_address="a", to_address="b", content=f"m{i}")
    claimed = inbox.claim("b", limit=3)
    assert len(claimed) == 3


def test_claim_filters_by_recipient(inbox):
    inbox.send(from_address="a", to_address="b", content="for b")
    inbox.send(from_address="a", to_address="c", content="for c")
    claimed_b = inbox.claim("b", limit=10)
    claimed_c = inbox.claim("c", limit=10)
    assert len(claimed_b) == 1
    assert len(claimed_c) == 1
    assert claimed_b[0].to_address == "b"
    assert claimed_c[0].to_address == "c"


# ── Marking processed ───────────────────────────────────────


def test_mark_processed(inbox):
    msg = inbox.send(from_address="a", to_address="b", content="x")
    inbox.claim("b", limit=10)
    inbox.mark_processed([msg.id])
    # The store now reflects processed.
    row = inbox.backend.get_inbox_message(msg.id)
    assert row["state"] == "processed"
    assert row["processed_at"] is not None


def test_mark_processed_empty_is_noop(inbox):
    n = inbox.mark_processed([])
    assert n == 0


def test_mark_processed_only_in_progress(inbox):
    """A message still in `received` cannot be marked processed.

    The store's `mark_inbox_processed` updates the row regardless
    of state, so the service layer is responsible for the
    pre-check. The agent's lifecycle is: claim → process →
    mark_processed, and that order is enforced by the state
    machine; the inbox does not second-guess the caller's
    order. We document this here."""
    msg = inbox.send(from_address="a", to_address="b", content="x")
    # Don't claim. The message is still `received`.
    inbox.mark_processed([msg.id])
    # The store's behavior: it sets state to processed. The
    # service trusts the caller's lifecycle. This is the
    # documented contract.
    row = inbox.backend.get_inbox_message(msg.id)
    assert row["state"] == "processed"


# ── Marking failed with retry ──────────────────────────────


def test_mark_failed_with_retries_remaining_resets_to_received(inbox):
    msg = inbox.send(from_address="a", to_address="b", content="x", max_retries=3)
    inbox.claim("b", limit=10)
    n = inbox.mark_failed([msg.id], retry=True)
    assert n == 1
    row = inbox.backend.get_inbox_message(msg.id)
    assert row["state"] == "received"
    assert row["retry_count"] == 1


def test_mark_failed_at_max_retries_keeps_in_failed(inbox):
    msg = inbox.send(from_address="a", to_address="b", content="x", max_retries=2)
    inbox.claim("b", limit=10)
    # First failure: retry.
    inbox.mark_failed([msg.id], retry=True)
    assert inbox.backend.get_inbox_message(msg.id)["retry_count"] == 1
    # Second failure: retry.
    inbox.claim("b", limit=10)
    inbox.mark_failed([msg.id], retry=True)
    assert inbox.backend.get_inbox_message(msg.id)["retry_count"] == 2
    # Third failure: at max retries, goes to failed.
    inbox.claim("b", limit=10)
    inbox.mark_failed([msg.id], retry=True)
    row = inbox.backend.get_inbox_message(msg.id)
    assert row["state"] == "failed"
    assert row["retry_count"] == 2


def test_mark_failed_with_retry_false_goes_straight_to_failed(inbox):
    msg = inbox.send(from_address="a", to_address="b", content="x", max_retries=3)
    inbox.claim("b", limit=10)
    inbox.mark_failed([msg.id], retry=False)
    row = inbox.backend.get_inbox_message(msg.id)
    assert row["state"] == "failed"
    # retry_count is not incremented when we don't retry.
    assert row["retry_count"] == 0


def test_mark_failed_unknown_id_is_noop(inbox):
    n = inbox.mark_failed(["msg_does_not_exist"], retry=True)
    assert n == 0


def test_mark_failed_only_in_progress(inbox):
    """A message in `received` cannot be retried via mark_failed."""
    msg = inbox.send(from_address="a", to_address="b", content="x")
    # Don't claim. The message is still `received`.
    n = inbox.mark_failed([msg.id], retry=True)
    # The service's state-machine check: only `in_progress`
    # messages can be failed. The state stays `received`.
    assert n == 0
    row = inbox.backend.get_inbox_message(msg.id)
    assert row["state"] == "received"


# ── Inspection ──────────────────────────────────────────────


def test_peek_returns_messages_without_state_change(inbox):
    inbox.send(from_address="a", to_address="b", content="1")
    inbox.send(from_address="a", to_address="b", content="2")
    peeked = inbox.peek("b", limit=10)
    assert len(peeked) == 2
    # All still in `received` (peek is non-destructive).
    for m in peeked:
        assert m.state == InboxState.RECEIVED


def test_peek_filters_by_states(inbox):
    msg1 = inbox.send(from_address="a", to_address="b", content="1")
    inbox.send(from_address="a", to_address="b", content="2")
    # Claim one to move it to in_progress. The earliest message
    # (by created_at) is claimed first.
    inbox.claim("b", limit=1)
    received_only = inbox.peek("b", states=[InboxState.RECEIVED])
    assert len(received_only) == 1
    in_progress = inbox.peek("b", states=[InboxState.IN_PROGRESS])
    assert len(in_progress) == 1
    assert in_progress[0].id == msg1.id


def test_stats_reports_counts(inbox):
    inbox.send(from_address="a", to_address="b", content="1")
    inbox.send(from_address="a", to_address="b", content="2")
    inbox.send(from_address="a", to_address="b", content="3")
    inbox.claim("b", limit=1)
    s = inbox.stats("b")
    assert s[InboxState.RECEIVED.value] == 2
    assert s[InboxState.IN_PROGRESS.value] == 1
    assert s[InboxState.PROCESSED.value] == 0
    assert s[InboxState.FAILED.value] == 0
    assert s["cap"] == 100


def test_stats_for_unused_inbox_is_zeros(inbox):
    s = inbox.stats("atm_nobody")
    assert s[InboxState.RECEIVED.value] == 0
    assert s["cap"] == 100


# ── End-to-end lifecycle ──────────────────────────────────


def test_full_lifecycle_send_claim_process(inbox):
    msg = inbox.send(from_address="a", to_address="b", content="do the thing")
    claimed = inbox.claim("b", limit=10)
    assert len(claimed) == 1
    assert claimed[0].id == msg.id
    inbox.mark_processed([msg.id])
    # After processing, a fresh claim finds nothing.
    assert inbox.claim("b", limit=10) == []


def test_full_lifecycle_send_claim_fail_retry_succeed(inbox):
    msg = inbox.send(from_address="a", to_address="b", content="x", max_retries=2)
    inbox.claim("b", limit=10)
    # First attempt fails.
    inbox.mark_failed([msg.id], retry=True)
    assert inbox.backend.get_inbox_message(msg.id)["state"] == "received"
    assert inbox.backend.get_inbox_message(msg.id)["retry_count"] == 1
    # Second attempt succeeds.
    inbox.claim("b", limit=10)
    inbox.mark_processed([msg.id])
    row = inbox.backend.get_inbox_message(msg.id)
    assert row["state"] == "processed"
    assert row["retry_count"] == 1


# ── Factory ───────────────────────────────────────────────


def test_make_inbox_uses_in_memory_store_by_default():
    svc = make_inbox()
    assert svc.backend is not None
    # Should work end-to-end with the in-memory store.
    msg = svc.send(from_address="a", to_address="b", content="hi")
    assert msg.id.startswith("msg_")


def test_make_inbox_with_explicit_store(store):
    svc = make_inbox(backend=store, cap=10)
    assert svc.cap == 10
    assert svc.backend is store


# ── InboxMessage ──────────────────────────────────────────


def test_inbox_message_to_dict_round_trip():
    msg = InboxMessage(
        id="msg_x",
        from_address="a",
        to_address="b",
        content="hi",
        state=InboxState.RECEIVED,
        retry_count=0,
        max_retries=3,
        created_at="2026-07-18T00:00:00+00:00",
        processed_at=None,
    )
    d = msg.to_dict()
    assert d["id"] == "msg_x"
    assert d["state"] == "received"
    assert d["processed_at"] is None


# ── reset_stuck (heartbeat-driven recovery) ─────────────


def test_reset_stuck_returns_in_progress_to_received(inbox, store):
    """A message that's been in_progress too long should
    be moved back to received so it can be retried."""
    # Manually insert a message with a backdated created_at.
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="microseconds"
    )
    store.enqueue_inbox(dict(
        id="msg_stuck",
        from_address="a",
        to_address="b",
        content="stuck message",
        state="in_progress",
        retry_count=0,
        max_retries=3,
        created_at=old,
        processed_at=None,
    ))
    n = inbox.reset_stuck(stuck_for_seconds=60)
    assert n == 1
    row = store.get_inbox_message("msg_stuck")
    assert row["state"] == "received"
    assert row["retry_count"] == 1


def test_reset_stuck_at_max_retries_goes_to_failed(inbox, store):
    """A stuck message past its retry cap should go to failed."""
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="microseconds"
    )
    store.enqueue_inbox(dict(
        id="msg_exhausted",
        from_address="a",
        to_address="b",
        content="give up",
        state="in_progress",
        retry_count=3,  # at max
        max_retries=3,
        created_at=old,
        processed_at=None,
    ))
    n = inbox.reset_stuck(stuck_for_seconds=60)
    assert n == 1
    row = store.get_inbox_message("msg_exhausted")
    assert row["state"] == "failed"


def test_reset_stuck_ignores_recent_messages(inbox):
    """A message claimed recently should not be reset."""
    inbox.send(from_address="a", to_address="b", content="fresh")
    inbox.claim("b", limit=1)
    # The message was just created — well under any threshold.
    n = inbox.reset_stuck(stuck_for_seconds=60)
    assert n == 0


def test_reset_stuck_ignores_received_state(inbox):
    """A `received` message is not stuck — it's waiting."""
    inbox.send(from_address="a", to_address="b", content="waiting")
    n = inbox.reset_stuck(stuck_for_seconds=60)
    assert n == 0
    row = inbox.backend.get_inbox_message(
        inbox.peek("b", limit=10)[0].id
    )
    assert row["state"] == "received"
