"""Tests for the approval flow."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

import pytest

from services.approvals.approvals import (
    Approval,
    ApprovalError,
    ApprovalGate,
    ApprovalReason,
    ApprovalState,
    ApprovalStore,
    PendingAction,
)


# ── Store ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_creates_pending_approval():
    store = ApprovalStore()
    a = await store.submit(
        automaton_id="atm_x",
        tool_name="email.send",
        arguments={"to": "alice@example.com", "body": "hi"},
        risk="high",
        cost_micro=0,
        currency="USDC",
        reasoning="external effect",
        citations=["constitution:law:8"],
    )
    assert a.state == ApprovalState.PENDING
    assert a.automaton_id == "atm_x"
    assert a.action.tool_name == "email.send"
    assert a.action.arguments["to"] == "alice@example.com"
    assert a.expires_at > a.created_at


@pytest.mark.asyncio
async def test_list_for_automaton_filters_by_state():
    store = ApprovalStore()
    a1 = await store.submit(
        automaton_id="atm_x", tool_name="x", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    a2 = await store.submit(
        automaton_id="atm_x", tool_name="y", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    a3 = await store.submit(
        automaton_id="atm_other", tool_name="z", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    await store.decide(a1.id, verdict=ApprovalState.APPROVED, decided_by="alice", reason="ok")
    pending = await store.list_for_automaton("atm_x", state=ApprovalState.PENDING)
    assert len(pending) == 1
    assert pending[0].id == a2.id
    all_for_x = await store.list_for_automaton("atm_x")
    assert {a.id for a in all_for_x} == {a1.id, a2.id}
    assert len(await store.list_for_automaton("atm_other")) == 1
    assert len(await store.list_for_automaton("atm_x", state=ApprovalState.APPROVED)) == 1


@pytest.mark.asyncio
async def test_list_pending_returns_only_pending():
    store = ApprovalStore()
    a1 = await store.submit(
        automaton_id="atm_x", tool_name="x", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    a2 = await store.submit(
        automaton_id="atm_y", tool_name="y", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    await store.decide(a1.id, verdict=ApprovalState.REJECTED, decided_by="bob", reason="no")
    pending = await store.list_pending()
    assert {a.id for a in pending} == {a2.id}


@pytest.mark.asyncio
async def test_decide_approved_records_decision():
    store = ApprovalStore()
    a = await store.submit(
        automaton_id="atm_x", tool_name="x", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    res = await store.decide(
        a.id, verdict=ApprovalState.APPROVED, decided_by="alice", reason="ok",
        signature="sig123",
    )
    assert res.state == ApprovalState.APPROVED
    assert res.decision is not None
    assert res.decision.decided_by == "alice"
    assert res.decision.reason == "ok"
    assert res.decision.signature == "sig123"


@pytest.mark.asyncio
async def test_decide_invalid_verdict_rejected():
    store = ApprovalStore()
    a = await store.submit(
        automaton_id="atm_x", tool_name="x", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    with pytest.raises(ApprovalError):
        await store.decide(a.id, verdict=ApprovalState.PENDING, decided_by="x", reason="")


@pytest.mark.asyncio
async def test_double_decide_rejected():
    store = ApprovalStore()
    a = await store.submit(
        automaton_id="atm_x", tool_name="x", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    await store.decide(a.id, verdict=ApprovalState.APPROVED, decided_by="alice", reason="ok")
    with pytest.raises(ApprovalError):
        await store.decide(a.id, verdict=ApprovalState.REJECTED, decided_by="bob", reason="no")


@pytest.mark.asyncio
async def test_expiry_marks_past_approvals_expired():
    store = ApprovalStore(default_ttl_seconds=1)
    a = await store.submit(
        automaton_id="atm_x", tool_name="x", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    # Wait past the TTL.
    await asyncio.sleep(1.1)
    # A subsequent decide on this approval should mark it expired.
    res = await store.decide(
        a.id, verdict=ApprovalState.APPROVED, decided_by="alice", reason=""
    )
    assert res.state == ApprovalState.EXPIRED


@pytest.mark.asyncio
async def test_expire_due_walks_pending():
    store = ApprovalStore(default_ttl_seconds=1)
    a1 = await store.submit(
        automaton_id="atm_x", tool_name="x", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    a2 = await store.submit(
        automaton_id="atm_y", tool_name="y", arguments={},
        risk="low", cost_micro=0, currency="USDC",
        reasoning="r", citations=[],
    )
    await asyncio.sleep(1.1)
    n = await store.expire_due()
    assert n == 2
    res1 = await store.get(a1.id)
    res2 = await store.get(a2.id)
    assert res1.state == ApprovalState.EXPIRED
    assert res2.state == ApprovalState.EXPIRED


@pytest.mark.asyncio
async def test_to_dict_serializes_for_api():
    store = ApprovalStore()
    a = await store.submit(
        automaton_id="atm_x", tool_name="email.send",
        arguments={"to": "a@b.com"},
        risk="high", cost_micro=0, currency="USDC",
        reasoning="r", citations=["c1"],
    )
    d = store.to_dict(a)
    assert d["id"] == a.id
    assert d["state"] == "pending"
    assert d["action"]["tool_name"] == "email.send"
    assert d["action"]["arguments"]["to"] == "a@b.com"
    assert d["decision"] is None
    # After a decision, decision field is populated.
    await store.decide(a.id, verdict=ApprovalState.APPROVED, decided_by="alice", reason="ok")
    d2 = store.to_dict(await store.get(a.id))
    assert d2["decision"] is not None
    assert d2["decision"]["decided_by"] == "alice"


# ── Gate ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_and_await_resolves_on_approve():
    """A concurrent `decide()` call wakes the awaiter."""
    gate = ApprovalGate()
    task = asyncio.create_task(
        gate.submit_and_await(
            automaton_id="atm_x",
            tool_name="email.send",
            arguments={"to": "a@b.com"},
            risk="high",
            cost_micro=0,
            currency="USDC",
            reasoning="r",
            citations=[],
            timeout_seconds=5.0,
        )
    )
    # Give the submit a moment to land in the store.
    await asyncio.sleep(0.05)
    pending = await gate.list_pending()
    assert len(pending) == 1
    aid = pending[0].id
    # Approve from a separate task.
    await gate.decide(
        aid, verdict=ApprovalState.APPROVED, decided_by="alice", reason="ok"
    )
    res = await task
    assert res.state == ApprovalState.APPROVED
    assert res.decision is not None
    assert res.decision.decided_by == "alice"


@pytest.mark.asyncio
async def test_submit_and_await_resolves_on_reject():
    gate = ApprovalGate()
    task = asyncio.create_task(
        gate.submit_and_await(
            automaton_id="atm_x",
            tool_name="email.send",
            arguments={"to": "a@b.com"},
            risk="high",
            cost_micro=0,
            currency="USDC",
            reasoning="r",
            citations=[],
            timeout_seconds=5.0,
        )
    )
    await asyncio.sleep(0.05)
    pending = await gate.list_pending()
    await gate.decide(
        pending[0].id, verdict=ApprovalState.REJECTED, decided_by="bob", reason="no"
    )
    res = await task
    assert res.state == ApprovalState.REJECTED


@pytest.mark.asyncio
async def test_submit_and_await_resolves_on_expiry():
    """If the approval's TTL elapses without a decision, await returns
    with state=EXPIRED."""
    store = ApprovalStore(default_ttl_seconds=1)
    gate = ApprovalGate(store=store)
    task = asyncio.create_task(
        gate.submit_and_await(
            automaton_id="atm_x",
            tool_name="x",
            arguments={},
            risk="low",
            cost_micro=0,
            currency="USDC",
            reasoning="r",
            citations=[],
            timeout_seconds=10.0,  # longer than TTL on purpose
        )
    )
    res = await task
    assert res.state == ApprovalState.EXPIRED


@pytest.mark.asyncio
async def test_submit_and_await_handles_timeout():
    """If the operator doesn't respond, the awaiter returns when the
    approval's TTL elapses, with state=EXPIRED."""
    # Use a short TTL so expire_due actually expires the approval.
    store = ApprovalStore(default_ttl_seconds=1)
    gate = ApprovalGate(store=store)
    res = await gate.submit_and_await(
        automaton_id="atm_x",
        tool_name="x",
        arguments={},
        risk="low",
        cost_micro=0,
        currency="USDC",
        reasoning="r",
        citations=[],
        timeout_seconds=10.0,  # long enough for the TTL to expire
    )
    assert res.state == ApprovalState.EXPIRED
    pending = await gate.list_pending()
    assert pending == []  # no longer pending
