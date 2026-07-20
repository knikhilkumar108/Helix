"""
Marketplace service. Manages:
  - Offers (services Automata can sell)
  - Orders (work contracted between Automata)
  - Settlement (transfer of funds)
  - Reputation updates

Orders are state machines; transitions are atomic and audited.
"""
from __future__ import annotations

import enum
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.errors.errors import ConflictError, NotFoundError, ValidationError
from core.types.identifiers import AutomatonId
from core.types.money import Money


class OrderState(str, enum.Enum):
    CREATED = "created"
    PAID = "paid"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    DISPUTED = "disputed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Offer:
    id: str
    seller_id: AutomatonId
    kind: str
    title: str
    description: str
    price: Money
    sla_seconds: int | None
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class Order:
    id: str
    offer_id: str
    buyer_id: AutomatonId
    seller_id: AutomatonId
    price: Money
    state: OrderState
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    deliverable: dict[str, Any] | None = None


class Marketplace:
    """In-memory marketplace. Same shape as the DB-backed version."""

    def __init__(self) -> None:
        self._offers: dict[str, Offer] = {}
        self._orders: dict[str, Order] = {}
        self._audit: list[dict[str, Any]] = []
        self._listeners: list[Callable[[Order], None]] = []

    def add_listener(self, fn: Callable[[Order], None]) -> None:
        self._listeners.append(fn)

    def create_offer(
        self,
        *,
        seller_id: AutomatonId,
        kind: str,
        title: str,
        description: str,
        price: Money,
        sla_seconds: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Offer:
        if price.micro < 0:
            raise ValidationError("price must be non-negative")
        o = Offer(
            id=f"ofr_{uuid.uuid4().hex}",
            seller_id=seller_id,
            kind=kind,
            title=title,
            description=description,
            price=price,
            sla_seconds=sla_seconds,
            payload=payload or {},
        )
        self._offers[o.id] = o
        return o

    def list_offers(self, *, kind: str | None = None) -> list[Offer]:
        return [o for o in self._offers.values() if not kind or o.kind == kind]

    def get_offer(self, offer_id: str) -> Offer:
        o = self._offers.get(offer_id)
        if o is None:
            raise NotFoundError("offer not found", context={"offer_id": offer_id})
        return o

    def place_order(self, *, offer_id: str, buyer_id: AutomatonId) -> Order:
        offer = self.get_offer(offer_id)
        if offer.seller_id == buyer_id:
            raise ValidationError("cannot buy your own offer")
        order = Order(
            id=f"ord_{uuid.uuid4().hex}",
            offer_id=offer.id,
            buyer_id=buyer_id,
            seller_id=offer.seller_id,
            price=offer.price,
            state=OrderState.CREATED,
        )
        self._orders[order.id] = order
        return order

    def transition(self, order_id: str, new_state: OrderState) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise NotFoundError("order not found", context={"order_id": order_id})
        if not _valid_transition(order.state, new_state):
            raise ConflictError(
                f"invalid transition {order.state} -> {new_state}",
                context={"order_id": order_id},
            )
        order.state = new_state
        if new_state in (OrderState.DELIVERED, OrderState.REFUNDED, OrderState.CANCELLED):
            order.completed_at = time.time()
        self._audit.append(
            {
                "ts": time.time(),
                "order_id": order.id,
                "state": new_state.value,
            }
        )
        for fn in self._listeners:
            fn(order)
        return order

    def get_order(self, order_id: str) -> Order:
        o = self._orders.get(order_id)
        if o is None:
            raise NotFoundError("order not found", context={"order_id": order_id})
        return o


def _valid_transition(frm: OrderState, to: OrderState) -> bool:
    allowed: dict[OrderState, set[OrderState]] = {
        OrderState.CREATED: {OrderState.PAID, OrderState.CANCELLED},
        OrderState.PAID: {OrderState.IN_PROGRESS, OrderState.CANCELLED, OrderState.REFUNDED},
        OrderState.IN_PROGRESS: {OrderState.DELIVERED, OrderState.DISPUTED, OrderState.CANCELLED},
        OrderState.DISPUTED: {OrderState.REFUNDED, OrderState.DELIVERED},
        OrderState.DELIVERED: set(),
        OrderState.REFUNDED: set(),
        OrderState.CANCELLED: set(),
    }
    return to in allowed[frm]
