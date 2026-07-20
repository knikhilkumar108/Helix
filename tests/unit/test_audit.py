"""Unit tests for the audit log chain."""
from __future__ import annotations

from services.observability.audit import AuditLog


def test_audit_chain_is_valid():
    log = AuditLog()
    log.append(actor_kind="user", action="automata.create", automaton_id="atm_1", payload={"k": "v"})
    log.append(actor_kind="user", action="automata.fund", automaton_id="atm_1", payload={"a": 1})
    log.append(actor_kind="automaton", action="plan.execute", automaton_id="atm_1", payload={})
    ok, broken = log.verify()
    assert ok, f"chain broken at {broken}"


def test_audit_chain_detects_tampering():
    log = AuditLog()
    log.append(actor_kind="user", action="x", payload={})
    log.append(actor_kind="user", action="y", payload={})
    log._entries[1].payload = {"tampered": True}  # noqa: SLF001
    ok, broken = log.verify()
    assert not ok
    assert broken is not None
