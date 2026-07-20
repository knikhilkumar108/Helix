"""Treasury routes: balance, funding, ledger."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from core.errors.errors import ValidationError
from core.security.signing import sign_envelope
from core.types.identifiers import AutomatonId
from core.types.money import Money

router = APIRouter()


class FundRequest(BaseModel):
    automaton_id: str
    amount_micro: int = Field(ge=0)
    currency: str = "USDC"
    memo: str | None = None
    source: str = "external"  # for audit only


class LedgerEntryResponse(BaseModel):
    id: str
    kind: str
    amount: str
    category: str
    ref_type: str | None
    ref_id: str | None
    occurred_at: str
    memo: str | None


def get_registry(request: Request):
    return request.app.state.registry


@router.get("/{aid}/balance")
def get_balance(aid: str, reg=Depends(get_registry)) -> dict[str, Any]:
    t = reg.treasury(AutomatonId(aid))
    return {"balance": str(t.balance()), "health": t.health()}


@router.post("/{aid}/fund", response_model=LedgerEntryResponse)
def fund(aid: str, req: FundRequest, reg=Depends(get_registry)) -> LedgerEntryResponse:
    if aid != req.automaton_id:
        raise ValidationError("automaton id mismatch")
    a = AutomatonId(req.automaton_id)
    t = reg.treasury(a)
    entry = t.credit(
        amount=Money(req.amount_micro, req.currency),
        category="funding:external",
        ref_type=req.source,
        memo=req.memo,
    )
    reg.record_event(a, "fund", {"amount": str(entry.amount), "source": req.source})
    # Sign the entry.
    kp = reg.keypair(a)
    envelope = sign_envelope(kp, {"ledger_id": entry.id, "amount_micro": entry.amount.micro, "currency": entry.amount.currency})
    return LedgerEntryResponse(
        id=entry.id,
        kind=entry.kind,
        amount=str(entry.amount),
        category=entry.category,
        ref_type=entry.ref_type,
        ref_id=entry.ref_id,
        occurred_at=entry.occurred_at.isoformat(),
        memo=entry.memo,
    )


@router.get("/{aid}/ledger")
def ledger(aid: str, reg=Depends(get_registry), limit: int = 100) -> list[LedgerEntryResponse]:
    t = reg.treasury(AutomatonId(aid))
    return [
        LedgerEntryResponse(
            id=e.id,
            kind=e.kind,
            amount=str(e.amount),
            category=e.category,
            ref_type=e.ref_type,
            ref_id=e.ref_id,
            occurred_at=e.occurred_at.isoformat(),
            memo=e.memo,
        )
        for e in t.history(limit=limit)
    ]
