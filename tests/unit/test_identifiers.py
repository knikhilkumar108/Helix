"""Unit tests for identifiers."""
from __future__ import annotations

import pytest

from core.types.identifiers import (
    ActionId,
    AutomatonId,
    EventId,
    MemoryId,
    PlanId,
    TaskId,
    new_action_id,
    new_automaton_id,
    new_event_id,
    new_memory_id,
    new_plan_id,
    new_task_id,
)


def test_new_ids_have_prefix():
    assert new_automaton_id().startswith("atm_")
    assert new_task_id().startswith("tsk_")
    assert new_action_id().startswith("act_")
    assert new_plan_id().startswith("pln_")
    assert new_memory_id().startswith("mem_")
    assert new_event_id().startswith("evt_")


def test_typed_ids_validate():
    AutomatonId("atm_0123456789abcdef0123456789abcdef")
    with pytest.raises(ValueError):
        AutomatonId("bad")
    with pytest.raises(ValueError):
        AutomatonId("atm_x")  # too short
    with pytest.raises(ValueError):
        AutomatonId(123)  # type: ignore[arg-type]


def test_typed_ids_compare_by_value():
    a = AutomatonId("atm_0123456789abcdef0123456789abcdef")
    b = AutomatonId("atm_0123456789abcdef0123456789abcdef")
    assert a == b
    assert hash(a) == hash(b)
    d = {a: 1}
    assert d[b] == 1


def test_typed_ids_used_in_set():
    s = {AutomatonId("atm_" + "a" * 32), AutomatonId("atm_" + "a" * 32)}
    assert len(s) == 1
