"""Unit tests for the Treasury and Budget controller."""
from __future__ import annotations

import time

import pytest

from core.errors.errors import InsufficientFundsError
from core.types.identifiers import AutomatonId, new_automaton_id
from core.types.money import Money
from runtime.loop.budget import BudgetConfig, BudgetController
from runtime.loop.treasury import InMemoryTreasury


def test_credit_increases_balance():
    t = InMemoryTreasury(AutomatonId(new_automaton_id()), initial=Money.from_major("1.00"))
    t.credit(amount=Money.from_major("2.50"), category="test")
    assert t.balance() == Money.from_major("3.50")


def test_charge_decreases_balance():
    t = InMemoryTreasury(AutomatonId(new_automaton_id()), initial=Money.from_major("1.00"))
    t.charge(amount=Money.from_major("0.25"), category="compute")
    assert t.balance() == Money.from_major("0.75")


def test_charge_more_than_balance_raises():
    t = InMemoryTreasury(AutomatonId(new_automaton_id()), initial=Money.from_major("1.00"))
    with pytest.raises(InsufficientFundsError):
        t.charge(amount=Money.from_major("2.00"), category="compute")


def test_history_records_in_order():
    t = InMemoryTreasury(AutomatonId(new_automaton_id()), initial=Money.from_major("1.00"))
    t.charge(amount=Money.from_major("0.10"), category="a")
    t.charge(amount=Money.from_major("0.20"), category="b")
    h = t.history()
    assert [e.category for e in h] == ["a", "b"]


def test_health_runway_positive():
    t = InMemoryTreasury(AutomatonId(new_automaton_id()), initial=Money.from_major("10.00"))
    for _ in range(3):
        t.charge(amount=Money.from_major("0.10"), category="x")
    h = t.health()
    assert "balance" in h
    assert "runway_seconds" in h


def test_budget_blocks_when_balance_too_low():
    t = InMemoryTreasury(AutomatonId(new_automaton_id()), initial=Money.from_major("0.50"))
    b = BudgetController(
        BudgetConfig(
            reserve_floor=Money.from_major("0.10"),
            per_tick_max=Money.from_major("1.00"),
            per_day_max=Money.from_major("10.00"),
        ),
        balance_getter=t.balance,
    )
    assert not b.can_afford(Money.from_major("0.50"))


def test_budget_blocks_when_exceeds_tick_cap():
    t = InMemoryTreasury(AutomatonId(new_automaton_id()), initial=Money.from_major("10.00"))
    b = BudgetController(
        BudgetConfig(
            reserve_floor=Money.zero(),
            per_tick_max=Money.from_major("1.00"),
            per_day_max=Money.from_major("10.00"),
        ),
        balance_getter=t.balance,
    )
    assert not b.can_afford(Money.from_major("2.00"))
