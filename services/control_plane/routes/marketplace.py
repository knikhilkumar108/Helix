"""Marketplace routes — offers and orders."""
from __future__ import annotations

import threading
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from core.errors.errors import NotFoundError
from core.types.identifiers import AutomatonId
from core.types.money import Money

router = APIRouter()


class OfferCreate(BaseModel):
    seller_id: str
    kind: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=8192)
    price_micro: int = Field(ge=0)
    currency: str = "USDC"
    sla_seconds: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class OfferResponse(BaseModel):
    id: str
    seller_id: str
    kind: str
    title: str
    description: str
    price: str
    currency: str
    sla_seconds: int | None


class OrderCreate(BaseModel):
    offer_id: str
    buyer_id: str


class OrderResponse(BaseModel):
    id: str
    offer_id: str
    buyer_id: str
    seller_id: str
    price: str
    currency: str
    status: str


_OFFERS: dict[str, OfferResponse] = {}
_ORDERS: dict[str, OrderResponse] = {}
_LOCK = threading.Lock()


def get_registry(request: Request):
    return request.app.state.registry


@router.post("/offers", response_model=OfferResponse, status_code=201)
def create_offer(req: OfferCreate) -> OfferResponse:
    oid = f"ofr_{uuid.uuid4().hex}"
    resp = OfferResponse(
        id=oid,
        seller_id=req.seller_id,
        kind=req.kind,
        title=req.title,
        description=req.description,
        price=str(Money(req.price_micro, req.currency)),
        currency=req.currency,
        sla_seconds=req.sla_seconds,
    )
    with _LOCK:
        _OFFERS[oid] = resp
    return resp


@router.get("/offers", response_model=list[OfferResponse])
def list_offers(kind: str | None = None) -> list[OfferResponse]:
    items = list(_OFFERS.values())
    if kind:
        items = [o for o in items if o.kind == kind]
    return items


@router.post("/orders", response_model=OrderResponse, status_code=201)
def create_order(req: OrderCreate, reg=Depends(get_registry)) -> OrderResponse:
    offer = _OFFERS.get(req.offer_id)
    if offer is None:
        raise NotFoundError("offer not found", context={"offer_id": req.offer_id})
    oid = f"ord_{uuid.uuid4().hex}"
    resp = OrderResponse(
        id=oid,
        offer_id=req.offer_id,
        buyer_id=req.buyer_id,
        seller_id=offer.seller_id,
        price=offer.price,
        currency=offer.currency,
        status="created",
    )
    with _LOCK:
        _ORDERS[oid] = resp
    return resp


@router.get("/orders/{oid}", response_model=OrderResponse)
def get_order(oid: str) -> OrderResponse:
    o = _ORDERS.get(oid)
    if o is None:
        raise NotFoundError("order not found", context={"order_id": oid})
    return o
