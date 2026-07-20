"""REST endpoints for the approval flow."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.approvals.approvals import ApprovalReason, ApprovalState

router = APIRouter()


class ApprovalResponse(BaseModel):
    id: str
    automaton_id: str
    state: str
    created_at: str
    expires_at: str
    decided_at: str | None = None
    action: dict
    decision: dict | None = None


class DecisionRequest(BaseModel):
    verdict: str = Field(pattern="^(approved|rejected)$")
    decided_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=2048)
    signature: str | None = None


def get_gate(request: Request):
    return request.app.state.approval_gate


@router.get("/pending", response_model=list[ApprovalResponse])
async def list_pending(request: Request) -> list[ApprovalResponse]:
    gate = get_gate(request)
    items = await gate.list_pending()
    return [ApprovalResponse(**gate.store.to_dict(a)) for a in items]


@router.get("/automaton/{automaton_id}", response_model=list[ApprovalResponse])
async def list_for_automaton(
    automaton_id: str, request: Request, state: str | None = None
) -> list[ApprovalResponse]:
    gate = get_gate(request)
    state_enum = ApprovalState(state) if state else None
    items = await gate.list_for_automaton(automaton_id, state=state_enum)
    return [ApprovalResponse(**gate.store.to_dict(a)) for a in items]


@router.get("/{aid}", response_model=ApprovalResponse)
async def get_approval(aid: str, request: Request) -> ApprovalResponse:
    gate = get_gate(request)
    approval = await gate.get(aid)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return ApprovalResponse(**gate.store.to_dict(approval))


@router.post("/{aid}/decide", response_model=ApprovalResponse)
async def decide_approval(
    aid: str, req: DecisionRequest, request: Request
) -> ApprovalResponse:
    gate = get_gate(request)
    try:
        approval = await gate.decide(
            aid,
            verdict=ApprovalState(req.verdict),
            decided_by=req.decided_by,
            reason=req.reason,
            signature=req.signature,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(e))
    return ApprovalResponse(**gate.store.to_dict(approval))
