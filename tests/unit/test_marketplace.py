"""Unit tests for the marketplace state machine."""
from __future__ import annotations

import pytest

from core.errors.errors import ConflictError, NotFoundError, ValidationError
from core.types.identifiers import AutomatonId, new_automaton_id
from core.types.money import Money
from services.marketplace.marketplace import Marketplace, OrderState


def test_create_and_place_order():
    m = Marketplace()
    seller = AutomatonId(new_automaton_id())
    buyer = AutomatonId(new_automaton_id())
    o = m.create_offer(
        seller_id=seller,
        kind="analysis",
        title="data analysis",
        description="analyze your data",
        price=Money.from_major("10.00"),
    )
    order = m.place_order(offer_id=o.id, buyer_id=buyer)
    assert order.state == OrderState.CREATED


def test_cannot_buy_own_offer():
    m = Marketplace()
    seller = AutomatonId(new_automaton_id())
    o = m.create_offer(
        seller_id=seller,
        kind="x",
        title="t",
        description="d",
        price=Money.from_major("1.00"),
    )
    with pytest.raises(ValidationError):
        m.place_order(offer_id=o.id, buyer_id=seller)


def test_transition_happy_path():
    m = Marketplace()
    seller = AutomatonId(new_automaton_id())
    buyer = AutomatonId(new_automaton_id())
    o = m.create_offer(
        seller_id=seller,
        kind="x",
        title="t",
        description="d",
        price=Money.from_major("1.00"),
    )
    order = m.place_order(offer_id=o.id, buyer_id=buyer)
    m.transition(order.id, OrderState.PAID)
    m.transition(order.id, OrderState.IN_PROGRESS)
    m.transition(order.id, OrderState.DELIVERED)
    final = m.get_order(order.id)
    assert final.state == OrderState.DELIVERED


def test_transition_invalid_raises():
    m = Marketplace()
    seller = AutomatonId(new_automaton_id())
    buyer = AutomatonId(new_automaton_id())
    o = m.create_offer(
        seller_id=seller,
        kind="x",
        title="t",
        description="d",
        price=Money.from_major("1.00"),
    )
    order = m.place_order(offer_id=o.id, buyer_id=buyer)
    with pytest.raises(ConflictError):
        m.transition(order.id, OrderState.DELIVERED)


def test_get_unknown_offer_raises():
    m = Marketplace()
    with pytest.raises(NotFoundError):
        m.get_offer("nope")
