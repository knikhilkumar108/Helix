"""HTTP routes for the agent inbox (operator view + cross-agent send).

The agent's runtime reads/writes its inbox directly through
`InboxService`. These routes are the *operator* view: list
inbox messages, send a message to an agent from the outside
(human or another service), and inspect inbox stats.

The routes assume an `InboxService` is registered on the
control plane's `app.state`. If no inbox is registered
(the most common case in dev), the routes return 503
"inbox not configured" — the agent can still use its
in-process inbox just fine.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.types.identifiers import AutomatonId
from services.messaging import InboxService, InboxState

log = logging.getLogger(__name__)

router = APIRouter()


# ── DI helpers ──────────────────────────────────────────────


def get_inbox_service(request: Request) -> InboxService | None:
    """Return the platform-wide `InboxService`, if one is registered.

    The control plane doesn't require an inbox to function
    (it can serve other routes fine), so this returns `None`
    when the operator hasn't configured one. Routes handle
    the `None` case by returning 503.
    """
    return getattr(request.app.state, "inbox_service", None)


# ── Schemas ───────────────────────────────────────────────


class InboxMessageResponse(BaseModel):
    id: str
    from_address: str
    to_address: str
    content: str
    state: str
    retry_count: int
    max_retries: int
    created_at: str
    processed_at: str | None = None


class SendRequest(BaseModel):
    from_address: str = Field(min_length=1, max_length=200)
    to_address: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=65536)
    max_retries: int = Field(default=3, ge=0, le=10)


class SendResponse(BaseModel):
    id: str
    state: str
    to: str
    from_: str = Field(alias="from")


# ── Endpoints ────────────────────────────────────────────


@router.get("/{aid}/messages", response_model=list[InboxMessageResponse])
def list_inbox(
    aid: str,
    request: Request,
    limit: int = 100,
    state: str | None = None,
) -> list[InboxMessageResponse]:
    """List inbox messages for an agent. Operator view.

    `state` filters by a single state (`received`,
    `in_progress`, `processed`, `failed`). Pass
    `state=received` to see what's waiting.
    """
    svc = get_inbox_service(request)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="inbox service not configured on the control plane",
        )
    states: list[InboxState] | None = None
    if state:
        try:
            states = [InboxState(state)]
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"unknown state {state!r}; "
                f"valid: received, in_progress, processed, failed",
            )
    msgs = svc.peek(aid, limit=limit, states=states)
    return [
        InboxMessageResponse(
            id=m.id,
            from_address=m.from_address,
            to_address=m.to_address,
            content=m.content,
            state=m.state.value,
            retry_count=m.retry_count,
            max_retries=m.max_retries,
            created_at=m.created_at,
            processed_at=m.processed_at,
        )
        for m in msgs
    ]


@router.get("/{aid}/stats")
def inbox_stats(aid: str, request: Request) -> dict[str, Any]:
    """Per-agent inbox stats. Useful for the operator dashboard."""
    svc = get_inbox_service(request)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="inbox service not configured on the control plane",
        )
    return svc.stats(aid)


@router.post("/send", response_model=SendResponse)
def send_message(
    body: SendRequest,
    request: Request,
) -> SendResponse:
    """Send a message to an agent's inbox from outside (human or service).

    The agent's runtime will see the message in its next
    tick's observation. The control plane just enqueues.
    """
    svc = get_inbox_service(request)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="inbox service not configured on the control plane",
        )
    msg = svc.send(
        from_address=body.from_address,
        to_address=body.to_address,
        content=body.content,
        max_retries=body.max_retries,
    )
    return SendResponse(
        id=msg.id,
        state=msg.state.value,
        to=msg.to_address,
        **{"from": msg.from_address},  # `from` is a Python keyword
    )
