"""HTTP routes for the x402 payment protocol.

Two endpoint families:

  1. Per-agent paid endpoints: `GET /v1/x402/{aid}/pay/{resource}`
     — a demo endpoint that exercises the full x402 round-trip
     through the HTTP layer. Useful for testing the wire-up and
     for clients that want to "see it work" without a real LLM.

  2. Inspection / stats endpoints: `GET /v1/x402/{aid}/stats` and
     `GET /v1/x402/stats` — operator and agent views of invoice
     and receipt counts.

Why a single demo endpoint: the x402 layer is the *plumbing* the
agent's own tools will use, not an API the agent exposes to
humans. The real "paid API" is what the agent builds on top of
its tools (e.g. "summarize this document for $0.05"). That
specific surface is owned by the agent's tool implementations;
this router just gives the operator a way to verify the x402
service is wired and reachable.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from core.errors.errors import ValidationError
from core.types.identifiers import AutomatonId
from core.types.money import Money
from services.payments import (
    H_INVOICE,
    H_PAYER,
    H_TX,
    PaymentRequired,
    X402Registry,
    X402Service,
    parse_payment_headers,
    render_payment_required,
)
from services.treasury.helix_treasury import MockBackend

log = logging.getLogger(__name__)

router = APIRouter()


# ── DI helpers ───────────────────────────────────────────────


def get_x402_registry(request: Request) -> X402Registry:
    return request.app.state.x402_registry


def _is_valid_aid(s: str) -> bool:
    """Return True if `s` parses as an AutomatonId.

    Mirrors the validation in `core.types.identifiers`. We
    pre-check before calling the constructor so a bad URL
    returns 400, not 500.
    """
    import re
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_\-:]{7,127}$", s))


def _get_or_create_service(aid: AutomatonId, reg: X402Registry) -> X402Service:
    """Return the x402 service for an agent, creating one on first use.

    The lazy-creation policy is a deliberate trade-off:
      - It avoids a surgery on `AutomatonRegistry.create()` to
        thread x402 initialization through every code path
        that creates an agent.
      - It makes the x402 service zero-config for the dev / test
        path: an agent gets a `MockBackend` automatically.
      - In production, an operator would pre-register services
        with `reg.register(aid, ...)` after deploying a real
        `HelixTreasury` for the agent.
    """
    svc = reg.get(aid)
    if svc is not None:
        return svc
    svc = X402Service(backend=MockBackend())
    reg.register(aid, svc)
    return svc


# ── Schemas ─────────────────────────────────────────────────


class PayRequest(BaseModel):
    """A request to perform a paid action.

    The `resource` field is the logical name of what the client
    wants (e.g. "research", "summarize"). The actual cost is
    determined by the server-side pricing; the client can't
    dictate the price (that's the whole point of the protocol).
    """

    resource: str
    payer: str | None = None  # optional, for the response only


class PayResponse(BaseModel):
    """A 200 response after a successful payment."""

    status: str
    paid: bool
    invoice_id: str
    tx_hash: str
    amount_micro: int
    amount_major: str
    resource: str


# ── Endpoints ───────────────────────────────────────────────


@router.get("/{aid}/stats")
def stats(
    aid: str,
    reg: X402Registry = Depends(get_x402_registry),
) -> dict[str, Any]:
    """Inspect x402 stats for a single agent."""
    aid_obj = AutomatonId(aid)
    svc = reg.get(aid_obj)
    if svc is None:
        # Don't auto-create on stats — that would pollute the
        # registry with empty services for every GET. The
        # operator can hit the pay endpoint to create one.
        raise HTTPException(status_code=404, detail=f"no x402 service for {aid}")
    return svc.stats()


@router.get("/stats")
def all_stats(reg: X402Registry = Depends(get_x402_registry)) -> dict[str, Any]:
    """Inspect x402 stats across all registered agents."""
    return reg.stats()


# ── A demo paid endpoint ──────────────────────────────────


# Default price for the demo endpoint. A real implementation would
# look this up from a pricing table keyed on `resource`.
DEMO_PRICE_MICRO: int = 100_000  # $0.10


@router.post("/{aid}/pay/{resource}")
async def pay(
    aid: str,
    resource: str,
    request: Request,
    reg: X402Registry = Depends(get_x402_registry),
) -> Response:
    """Demo paid endpoint. Walks the full x402 round-trip:

      1. If the request has no payment proof, return 402 with
         a fresh invoice.
      2. If the proof is present, settle it. On success, return
         200 with the receipt.
      3. On any failure, return 402 with a new invoice (so the
         client can retry without having to call a separate
         "create invoice" endpoint first).

    This is the canonical pattern a real paid endpoint would
    follow. The handler is intentionally short.
    """
    aid_obj = AutomatonId(aid) if _is_valid_aid(aid) else None
    if aid_obj is None:
        # Bad URL — return 400, not 500. The AutomatonId
        # constructor raises ValueError on malformed input;
        # we catch it explicitly so the global 500 handler
        # doesn't swallow it.
        raise HTTPException(status_code=400, detail=f"invalid automaton id: {aid!r}")
    svc = _get_or_create_service(aid_obj, reg)

    # 1. Try to settle an existing payment proof.
    headers = parse_payment_headers(request.headers)
    try:
        receipt = await svc.settle_request(
            invoice_id=headers["invoice_id"],
            tx_hash=headers["tx_hash"],
            payer=headers["payer"],
            resource=resource,
            automaton_id=aid_obj,
            required_amount=Money(DEMO_PRICE_MICRO, "USDC"),
        )
    except PaymentRequired as e:
        # 2. No valid proof — issue a new invoice and return 402.
        # We use the `render_payment_required` helper, NOT
        # `HTTPException`, because the 402 is a normal protocol
        # response in x402, not an exceptional condition. The
        # helper produces a clean JSONResponse with status 402
        # and the invoice headers.
        invoice = svc.issue_invoice(
            automaton_id=aid_obj,
            amount=Money(DEMO_PRICE_MICRO, "USDC"),
            resource=resource,
            memo=f"demo: {resource}",
        )
        log.info(
            "x402_demo_402",
            extra={"aid": aid, "resource": resource, "reason": e.reason},
        )
        return render_payment_required(invoice, reason=e.reason)
    except ValidationError as e:
        # The client sent malformed headers. Treat as 402.
        invoice = svc.issue_invoice(
            automaton_id=aid_obj,
            amount=Money(DEMO_PRICE_MICRO, "USDC"),
            resource=resource,
            memo=f"demo: {resource} (re-issued after validation error)",
        )
        return render_payment_required(invoice, reason=str(e))

    # 3. Settled. The agent "did the work" — for the demo, the
    # work is just returning the receipt. A real handler would
    # call into the agent's tools here.
    return PayResponse(
        status="ok",
        paid=True,
        invoice_id=receipt.invoice_id,
        tx_hash=receipt.tx_hash,
        amount_micro=receipt.amount.micro,
        amount_major=str(receipt.amount.to_major()),
        resource=resource,
    )
