"""Automaton CRUD & lifecycle routes."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.types.automaton import Automaton, LifecycleState
from core.types.identifiers import AutomatonId
from core.types.money import Money
from services.bootstrap import BootstrapRequest

router = APIRouter()


class CreateAutomatonRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    genesis_prompt: str = Field(min_length=1, max_length=8192)
    parent_id: AutomatonId | None = None
    initial_balance_micro: int = 0
    currency: str = "USDC"
    metadata: dict[str, str] = Field(default_factory=dict)


class AutomatonResponse(BaseModel):
    id: str
    name: str
    state: LifecycleState
    parent_id: str | None
    public_key: str
    wallet_address: str
    version: str
    reputation: float
    base_currency: str
    balance: str
    created_at: str
    updated_at: str

    @classmethod
    def from_automaton(cls, a: Automaton) -> "AutomatonResponse":
        return cls(
            id=str(a.id),
            name=a.name,
            state=a.state,
            parent_id=str(a.parent_id) if a.parent_id else None,
            public_key=a.public_key,
            wallet_address=a.wallet_address,
            version=a.version,
            reputation=a.reputation,
            base_currency=a.base_currency,
            balance=str(a.balance),
            created_at=a.created_at.isoformat(),
            updated_at=a.updated_at.isoformat(),
        )


def get_registry(request: Request):
    return request.app.state.registry


def get_bootstrap(request: Request):
    """Return the platform's `BootstrapService`, or None if not configured."""
    return getattr(request.app.state, "bootstrap", None)


@router.post("", response_model=AutomatonResponse, status_code=201)
def create_automaton(
    req: CreateAutomatonRequest,
    request: Request,
    reg=Depends(get_registry),
) -> AutomatonResponse:
    bal = Money(req.initial_balance_micro, req.currency) if req.initial_balance_micro else Money.zero(req.currency)
    # If a BootstrapService is wired, use it. Otherwise fall
    # back to a plain `reg.create()` so the route still works
    # in dev environments without a bootstrap configured.
    bootstrap = get_bootstrap(request)
    if bootstrap is not None:
        result = bootstrap.run(
            BootstrapRequest(
                name=req.name,
                genesis_prompt=req.genesis_prompt,
                parent_id=req.parent_id,
                initial_balance=bal,
                metadata=req.metadata,
            )
        )
        # Round-trip the agent through the registry to get the
        # full Automaton object the response model expects.
        a = reg.get(result.automaton_id)
        return AutomatonResponse.from_automaton(a)
    a = reg.create(
        name=req.name,
        genesis_prompt=req.genesis_prompt,
        parent_id=req.parent_id,
        initial_balance=bal,
        metadata=req.metadata,
    )
    return AutomatonResponse.from_automaton(a)


@router.get("", response_model=list[AutomatonResponse])
def list_automata(reg=Depends(get_registry)) -> list[AutomatonResponse]:
    return [AutomatonResponse.from_automaton(a) for a in reg.list()]


@router.get("/{aid}", response_model=AutomatonResponse)
def get_automaton(aid: str, reg=Depends(get_registry)) -> AutomatonResponse:
    return AutomatonResponse.from_automaton(reg.get(AutomatonId(aid)))


@router.post("/{aid}/pause", response_model=AutomatonResponse)
def pause(aid: str, reg=Depends(get_registry)) -> AutomatonResponse:
    a = AutomatonId(aid)
    reg.set_state(a, LifecycleState.PAUSED)
    reg.record_event(a, "pause", {})
    return AutomatonResponse.from_automaton(reg.get(a))


@router.post("/{aid}/resume", response_model=AutomatonResponse)
def resume(aid: str, reg=Depends(get_registry)) -> AutomatonResponse:
    a = AutomatonId(aid)
    reg.set_state(a, LifecycleState.RUNNING)
    reg.record_event(a, "resume", {})
    return AutomatonResponse.from_automaton(reg.get(a))


@router.post("/{aid}/terminate", response_model=AutomatonResponse)
def terminate(aid: str, reg=Depends(get_registry)) -> AutomatonResponse:
    a = AutomatonId(aid)
    reg.set_state(a, LifecycleState.TERMINATED)
    reg.record_event(a, "terminate", {})
    return AutomatonResponse.from_automaton(reg.get(a))


@router.get("/{aid}/plans")
def list_plans(aid: str, reg=Depends(get_registry)) -> list[dict[str, Any]]:
    a = AutomatonId(aid)
    return [
        {
            "id": str(p.id),
            "status": p.status,
            "estimated_cost": str(p.estimated_cost),
            "expected_revenue": str(p.expected_revenue),
            "probability": p.probability,
            "created_at": p.created_at.isoformat(),
            "steps": len(p.steps),
        }
        for p in reg.plans(a)
    ]


@router.get("/{aid}/tasks")
def list_tasks(aid: str, reg=Depends(get_registry)) -> list[dict[str, Any]]:
    a = AutomatonId(aid)
    return [
        {
            "id": str(t.id),
            "kind": t.kind,
            "status": t.status,
            "budget": str(t.budget),
            "created_at": t.created_at.isoformat(),
        }
        for t in reg.tasks(a)
    ]


@router.get("/{aid}/events")
def list_events(aid: str, reg=Depends(get_registry)) -> list[dict[str, Any]]:
    return reg.events(AutomatonId(aid))
