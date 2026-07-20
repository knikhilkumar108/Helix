"""Tests for the SQLite store."""
from __future__ import annotations

import time
import uuid

import pytest

from services.state.sqlite_store import SqliteStore


@pytest.fixture
def store():
    return SqliteStore(path=":memory:")


def _automaton_row(aid: str | None = None) -> dict:
    aid = aid or f"atm_{uuid.uuid4().hex}"
    return {
        "id": aid,
        "name": "test",
        "parent_id": None,
        "genesis_prompt": "be helpful",
        "public_key": "PUBKEY",
        "wallet_address": f"atm_wallet_{aid[:8]}",
        "state": "created",
        "lifecycle_state_at": "2025-01-01T00:00:00+00:00",
        "version": "0.1.0",
        "reputation": 0.5,
        "base_currency": "USDC",
        "balance_micro": 1_000_000,
        "budget_micro": 0,
        "metadata_json": "{}",
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def test_upsert_and_get(store):
    row = _automaton_row()
    store.upsert_automaton(row)
    got = store.get_automaton(row["id"])
    assert got is not None
    assert got["name"] == "test"
    assert got["balance_micro"] == 1_000_000


def test_upsert_updates_balance(store):
    row = _automaton_row()
    store.upsert_automaton(row)
    store.set_automaton_balance(row["id"], 5_000_000)
    got = store.get_automaton(row["id"])
    assert got["balance_micro"] == 5_000_000


def test_set_state(store):
    row = _automaton_row()
    store.upsert_automaton(row)
    store.set_automaton_state(row["id"], "running")
    got = store.get_automaton(row["id"])
    assert got["state"] == "running"


def test_list_automata(store):
    for _ in range(3):
        store.upsert_automaton(_automaton_row())
    assert len(store.list_automata()) == 3


def test_ledger_roundtrip(store):
    row = _automaton_row()
    store.upsert_automaton(row)
    store.append_ledger(
        {
            "id": f"led_{uuid.uuid4().hex}",
            "automaton_id": row["id"],
            "kind": "credit",
            "amount_micro": 1_000_000,
            "currency": "USDC",
            "category": "funding",
            "ref_type": None,
            "ref_id": None,
            "memo": "test",
            "occurred_at": "2025-01-01T00:00:00+00:00",
            "signature": "sig",
        }
    )
    history = store.ledger(row["id"])
    assert len(history) == 1
    assert history[0]["category"] == "funding"


def test_audit_chain(store):
    for i in range(5):
        store.append_audit(
            {
                "occurred_at": f"2025-01-01T00:00:0{i}+00:00",
                "tenant_id": None,
                "automaton_id": "atm_x",
                "user_id": None,
                "actor_kind": "automaton",
                "action": f"action_{i}",
                "target_kind": None,
                "target_id": None,
                "request_id": None,
                "correlation_id": None,
                "payload_json": '{"i": %d}' % i,
            }
        )
    ok, broken = store.verify_audit_chain()
    assert ok, f"chain broken at {broken}"


def test_audit_chain_tamper_detected(store):
    store.append_audit(
        {
            "occurred_at": "2025-01-01T00:00:00+00:00",
            "tenant_id": None,
            "automaton_id": "atm_x",
            "user_id": None,
            "actor_kind": "automaton",
            "action": "x",
            "target_kind": None,
            "target_id": None,
            "request_id": None,
            "correlation_id": None,
            "payload_json": '{"i": 0}',
        }
    )
    store.append_audit(
        {
            "occurred_at": "2025-01-01T00:00:01+00:00",
            "tenant_id": None,
            "automaton_id": "atm_x",
            "user_id": None,
            "actor_kind": "automaton",
            "action": "y",
            "target_kind": None,
            "target_id": None,
            "request_id": None,
            "correlation_id": None,
            "payload_json": '{"i": 1}',
        }
    )
    # Tamper: directly mutate the second row.
    with store._conn():  # type: ignore[attr-defined]
        store._conn().execute(  # type: ignore[attr-defined]
            "UPDATE audit_log SET payload_json=? WHERE seq=2", ['{"i": 999}']
        )
    ok, _ = store.verify_audit_chain()
    assert not ok


def test_turn_and_tool_calls(store):
    aid = _automaton_row()["id"]
    store.upsert_automaton(_automaton_row(aid))
    store.insert_turn(
        {
            "id": "turn_1",
            "automaton_id": aid,
            "state": "running",
            "input": "hello",
            "input_source": "wakeup",
            "thinking": "thinking...",
            "token_usage_json": '{"input": 1, "output": 2}',
            "cost_micro": 100,
            "currency": "USDC",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
    )
    store.insert_tool_call(
        {
            "id": "tc_1",
            "turn_id": "turn_1",
            "name": "shell.exec",
            "arguments_json": '{"command": "ls"}',
            "result": "out",
            "error": None,
            "started_at": "2025-01-01T00:00:00+00:00",
            "completed_at": "2025-01-01T00:00:01+00:00",
        }
    )
    tcs = store.tool_calls_for_turn("turn_1")
    assert len(tcs) == 1
    assert tcs[0]["name"] == "shell.exec"


def test_inbox_claim_and_process(store):
    aid = _automaton_row()["id"]
    store.upsert_automaton(_automaton_row(aid))
    for i in range(3):
        store.enqueue_inbox(
            {
                "id": f"msg_{i}",
                "from_address": "0xabc",
                "to_address": aid,
                "content": f"hello {i}",
                "state": "received",
                "retry_count": 0,
                "max_retries": 3,
                "created_at": "2025-01-01T00:00:00+00:00",
            }
        )
    claimed = store.claim_inbox(aid, limit=2)
    assert len(claimed) == 2
    store.mark_inbox_processed([claimed[0]["id"], claimed[1]["id"]])
    # remaining one
    claimed2 = store.claim_inbox(aid, limit=5)
    assert len(claimed2) == 1
    assert claimed2[0]["id"] == "msg_2"


def test_inbox_retry_on_failure(store):
    aid = _automaton_row()["id"]
    store.upsert_automaton(_automaton_row(aid))
    store.enqueue_inbox(
        {
            "id": "msg_x",
            "from_address": "0xabc",
            "to_address": aid,
            "content": "hi",
            "state": "received",
            "retry_count": 0,
            "max_retries": 3,
            "created_at": "2025-01-01T00:00:00+00:00",
        }
    )
    store.claim_inbox(aid)
    store.reset_inbox_to_received(["msg_x"])
    # Now should be available again
    again = store.claim_inbox(aid)
    assert len(again) == 1
    assert again[0]["retry_count"] == 1


def test_memory_write_and_list(store):
    aid = _automaton_row()["id"]
    store.upsert_automaton(_automaton_row(aid))
    store.write_memory(
        {
            "id": "mem_1",
            "automaton_id": aid,
            "layer": "long_term",
            "content": "the answer is 42",
            "importance": 0.8,
            "ttl_seconds": None,
            "tags_json": '["fact"]',
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
        }
    )
    items = store.list_memory(aid, layer="long_term")
    assert len(items) == 1


def test_kv_roundtrip(store):
    store.kv_set("foo", "bar")
    assert store.kv_get("foo") == "bar"
    store.kv_delete("foo")
    assert store.kv_get("foo") is None


def test_skills_upsert_and_list(store):
    store.upsert_skill(
        {
            "name": "code-review",
            "version": "0.1.0",
            "description": "review a PR",
            "enabled": 1,
            "source_uri": "https://example.com/skill.zip",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
    )
    enabled = store.list_enabled_skills()
    assert len(enabled) == 1
    assert enabled[0]["name"] == "code-review"
