"""
HTTP header adapter for the x402 protocol.

The core `X402Service` (in `x402.py`) is HTTP-agnostic: it works
with dicts of headers, dataclasses of invoices, and exceptions.
This module is the thin layer that ties that to a real HTTP request
handler (FastAPI, Starlette, or plain WSGI).

Three responsibilities:

  1. Header name constants (`H_*`). Re-exported here so the HTTP
     layer doesn't need to import the core module for the names.

  2. `parse_payment_headers(headers)` — extract the invoice id,
     tx hash, and payer from a client's retry request.

  3. `render_payment_required(invoice)` — build a `JSONResponse`
     with status 402 and the invoice's headers, plus a small
     JSON body so humans and tests can see what went wrong.

The HTTP layer is intentionally *not* tightly coupled to FastAPI;
the `JSONResponse` type is the only FastAPI dependency, and
`render_payment_required` returns something compatible with
Starlette, FastAPI, and most ASGI frameworks. To swap to Flask,
replace `JSONResponse` with `flask.jsonify(...)`.
"""
from __future__ import annotations

from typing import Any
from fastapi.responses import JSONResponse

from .x402 import H_ADDRESS, H_AMOUNT, H_CHAIN, H_EXPIRES, H_INVOICE, H_MEMO, H_NONCE, H_PAYER, H_TOKEN, H_TX, H_VERSION, Invoice


# Re-export header names for callers that want to import them
# from one place.
__all__ = [
    "H_VERSION",
    "H_ADDRESS",
    "H_AMOUNT",
    "H_TOKEN",
    "H_CHAIN",
    "H_NONCE",
    "H_INVOICE",
    "H_EXPIRES",
    "H_MEMO",
    "H_TX",
    "H_PAYER",
    "parse_payment_headers",
    "render_payment_required",
]


# ── Parsing ──────────────────────────────────────────────────


def parse_payment_headers(headers: Any) -> dict[str, str | None]:
    """Extract the x402-relevant headers from an incoming request.

    `headers` is anything with a `.get(key)` method (FastAPI's
    `Request.headers`, Starlette's `Headers`, a plain dict, etc).

    Returns a dict with `invoice_id`, `tx_hash`, and `payer` set
    to the header value or None. The caller is expected to pass
    the dict straight to `X402Service.settle_request(...)`.
    """
    return {
        "invoice_id": headers.get(H_INVOICE),
        "tx_hash": headers.get(H_TX),
        "payer": headers.get(H_PAYER),
    }


# ── Rendering ───────────────────────────────────────────────


def render_payment_required(
    invoice: Invoice,
    *,
    reason: str = "payment required",
) -> JSONResponse:
    """Build a 402 response with the invoice's headers and a
    small JSON body explaining what the client should do next.

    The body is informational. The contract with the client lives
    in the headers — clients should be coded against the headers,
    not the body. We include the body for human operators
    inspecting the agent with curl.
    """
    body = {
        "error": "payment_required",
        "reason": reason,
        "invoice_id": invoice.invoice_id,
        "amount_micro": invoice.amount.micro,
        "amount_major": str(invoice.amount.to_major()),
        "currency": invoice.amount.currency,
        "address": invoice.address,
        "chain": invoice.chain,
        "nonce": invoice.nonce,
        "expires_at": invoice.expires_at.isoformat(),
        "memo": invoice.memo,
        "resource": invoice.resource,
        "instructions": (
            f"Send exactly {invoice.amount.micro} micro-{invoice.amount.currency} "
            f"to {invoice.address} on {invoice.chain}, then retry with the "
            f"{H_TX} and {H_PAYER} headers set."
        ),
    }
    return JSONResponse(
        status_code=402,
        content=body,
        headers=invoice.to_headers(),
    )
