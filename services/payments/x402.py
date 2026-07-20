"""
x402 payment protocol — server side.

The x402 pattern (HTTP 402 + payment-required) lets an API endpoint
demand payment as part of a single HTTP round-trip:

  1. Client requests a paid resource.
  2. Server responds 402 with payment instructions in headers.
  3. Client pays the invoice on-chain (or via mock) and retries with
     a payment proof header.
  4. Server verifies the payment, then serves the resource.

This module implements the *server* half. It is the bridge between
incoming HTTP requests and the agent's wallet (HelixTreasury's
WalletBackend). The design is intentionally minimal so the agent can
expose paid endpoints with a small wrapper.

The wire format uses custom headers (prefixed with `X-Payment-`).
We chose headers over a JSON body for the 402 response because:
  - The 402 response itself has no canonical body schema across
    implementations; headers travel through every proxy and cache
    without parsing.
  - A retry can re-send the same headers without re-serializing a
    body. Clients that don't care about payment still see a clean
    402 with machine-readable payment terms.

Header schema (all `X-Payment-*`):

  X-Payment-Version:        "x402/1"  (protocol version)
  X-Payment-Address:        0x...     (recipient USDC contract address on Base)
  X-Payment-Amount:         100000    (micro-USDC = $0.10)
  X-Payment-Token:          "USDC"    (asset identifier)
  X-Payment-Chain:          "base"    (chain identifier)
  X-Payment-Nonce:          hex32     (unique per-invoice)
  X-Payment-Invoice:        "inv_…"   (operator's invoice id)
  X-Payment-Expires-At:     RFC3339   (deadline for the payment)
  X-Payment-Memo:           "…"       (optional, human-readable)
  X-Payment-Tx:             0x...     (client's settlement tx, on retry)
  X-Payment-Payer:          0x...     (client's wallet, on retry)

The protocol intentionally does NOT define a body schema. The 402
response body is whatever the server wants to send; the headers are
the contract.

Design decisions (the ones that are easy to get wrong):

  - We do NOT verify on-chain settlement here. The WalletBackend
    abstractly models the chain; verifying settlement in the server
    would require the same chain RPC the wallet already uses. We
    delegate to a `PaymentVerifier` callable instead, so a stub
    verifier can be injected in tests, and a real one (chain RPC)
    can be wired in production. This keeps the core logic
    independent of the chain library.

  - Invoices are short-lived (default 5 minutes). The nonce binds
    the invoice to a specific (payer, amount, resource) tuple, and
    a single nonce is single-use. Replay attacks are blocked by
    tracking seen nonces in a `PaymentRegistry`.

  - We track the receipt for at most `retain_seconds` so the agent
    can prove to auditors that a given request was paid. This is
    separate from the in-memory ledger (which is the runtime's
    hot path) — the registry is a payment log, not a ledger.

  - The verifier is async because on-chain confirmation is async.
    The server handler awaits it; the in-memory mock verifier
    returns immediately.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol

from core.errors.errors import (
    InsufficientFundsError,
    ValidationError,
)
from core.types.identifiers import AutomatonId
from core.types.money import Money
from services.treasury.helix_treasury import (
    MockBackend,
    WalletBackend,
)

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────

# Protocol version. Bump if header shape changes.
X402_VERSION: str = "x402/1"

# Default invoice TTL. Short on purpose: if the client doesn't pay
# within this window, the nonce is invalidated and a new invoice
# must be requested. Long enough to allow a human in the loop;
# short enough to bound the agent's exposure to stale invoices.
DEFAULT_INVOICE_TTL_SECONDS: int = 300

# How long to keep paid receipts in the in-memory registry. The
# audit log on disk is the source of truth long-term; this is just
# for the "did this request get paid recently?" hot path.
DEFAULT_RECEIPT_TTL_SECONDS: int = 3600

# Chain defaults. USDC contract on Base mainnet. The protocol is
# chain-agnostic; the defaults match what the platform's wallet
# backend assumes.
DEFAULT_CHAIN: str = "base"
DEFAULT_TOKEN: str = "USDC"

# Header names. Module-level so tests can reference the same
# constants rather than string-literal-spamming.
H_VERSION = "X-Payment-Version"
H_ADDRESS = "X-Payment-Address"
H_AMOUNT = "X-Payment-Amount"
H_TOKEN = "X-Payment-Token"
H_CHAIN = "X-Payment-Chain"
H_NONCE = "X-Payment-Nonce"
H_INVOICE = "X-Payment-Invoice"
H_EXPIRES = "X-Payment-Expires-At"
H_MEMO = "X-Payment-Memo"
H_TX = "X-Payment-Tx"
H_PAYER = "X-Payment-Payer"

ALL_INVOICE_HEADERS: tuple[str, ...] = (
    H_VERSION, H_ADDRESS, H_AMOUNT, H_TOKEN, H_CHAIN,
    H_NONCE, H_INVOICE, H_EXPIRES, H_MEMO,
)
ALL_RECEIPT_HEADERS: tuple[str, ...] = (H_TX, H_PAYER)


# ── Types ────────────────────────────────────────────────────


@dataclass(slots=True)
class Invoice:
    """A request for payment. The client must settle this invoice
    on-chain (or via a mock backend) and retry the request with
    the corresponding payment proof.

    `nonce` is a server-generated random 32-byte hex string. The
    server uses it to bind the payment to a specific (resource,
    payer) tuple and to prevent replay attacks.

    `amount` is the full price in micro-USDC. We don't do partial
    payments — if the agent asks for $0.10, the client must pay
    exactly $0.10.

    `expires_at` is an absolute UTC timestamp. Past this point the
    invoice is invalid; the client must request a new one.
    """

    invoice_id: str
    nonce: str
    amount: Money
    address: str           # recipient wallet address
    chain: str             # "base" by default
    token: str             # "USDC" by default
    memo: str | None
    resource: str          # what the client is paying for (URL or name)
    automaton_id: AutomatonId
    issued_at: datetime
    expires_at: datetime
    settled: bool = False
    settlement_tx: str | None = None
    payer: str | None = None
    settled_at: datetime | None = None

    def to_headers(self) -> dict[str, str]:
        """Render the invoice as a header dict for the 402 response."""
        h = {
            H_VERSION: X402_VERSION,
            H_ADDRESS: self.address,
            H_AMOUNT: str(self.amount.micro),
            H_TOKEN: self.amount.currency,
            H_CHAIN: self.chain,
            H_NONCE: self.nonce,
            H_INVOICE: self.invoice_id,
            H_EXPIRES: self.expires_at.isoformat(),
        }
        if self.memo:
            h[H_MEMO] = self.memo
        return h

    def is_expired(self, *, now: datetime | None = None) -> bool:
        n = now or datetime.now(tz=timezone.utc)
        return n >= self.expires_at


@dataclass(slots=True)
class PaymentReceipt:
    """Proof that an invoice was paid. Stored in the registry so
    the agent can answer "did this nonce pay me?" for `retain_seconds`
    after the payment."""

    invoice_id: str
    nonce: str
    tx_hash: str
    payer: str
    amount: Money
    resource: str
    automaton_id: AutomatonId
    received_at: datetime
    expires_at: datetime  # when this receipt is purged from memory


# ── Payment verifier (protocol) ─────────────────────────────


class PaymentVerifier(Protocol):
    """Verifies that a client actually paid an invoice.

    A real implementation calls the chain RPC to confirm the tx is
    mined, the recipient is correct, the amount matches, and the
    token is USDC. The default `MockVerifier` accepts any tx hash
    that starts with `0x` and is at least 8 chars long.
    """

    async def verify(
        self,
        *,
        invoice: Invoice,
        tx_hash: str,
        payer: str,
    ) -> bool:
        """Return True if the payment is valid. Raise on RPC error."""


class MockVerifier:
    """A verifier that accepts any well-formed tx hash.

    Used in tests and dev. Production wire-up should replace this
    with a real chain-verifying implementation.
    """

    async def verify(
        self,
        *,
        invoice: Invoice,
        tx_hash: str,
        payer: str,
    ) -> bool:
        # Sanity-check the shape of the proof. A real verifier
        # would call the chain; we just check the fields exist.
        if not tx_hash or not tx_hash.startswith("0x") or len(tx_hash) < 10:
            return False
        if not payer or not payer.startswith("0x") or len(payer) < 10:
            return False
        if invoice.is_expired():
            return False
        return True


# ── Payment registry ────────────────────────────────────────


class PaymentRegistry:
    """In-memory store of issued invoices and received receipts.

    Thread-safety: a single `asyncio.Lock` covers both maps. The
    registry is not a hot path; a tick of the loop hits it at
    most once per incoming request.

    Bounded growth: receipts are purged after `receipt_ttl_seconds`
    and invoices after `invoice_ttl_seconds`. This is a memory
    bound, not a security guarantee — the audit log on disk is
    the long-term store.
    """

    def __init__(
        self,
        *,
        invoice_ttl_seconds: int = DEFAULT_INVOICE_TTL_SECONDS,
        receipt_ttl_seconds: int = DEFAULT_RECEIPT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._invoices: dict[str, Invoice] = {}
        self._receipts: dict[str, PaymentReceipt] = {}
        self._seen_nonces: dict[str, str] = {}  # nonce → invoice_id
        self._invoice_ttl = invoice_ttl_seconds
        self._receipt_ttl = receipt_ttl_seconds
        self._clock = clock
        self._lock = __import__("asyncio").Lock()

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    # ── invoice management ──
    def record_invoice(self, inv: Invoice) -> None:
        if inv.nonce in self._seen_nonces:
            raise ValidationError("nonce already used")
        self._invoices[inv.invoice_id] = inv
        self._seen_nonces[inv.nonce] = inv.invoice_id

    def get_invoice(self, invoice_id: str) -> Invoice | None:
        inv = self._invoices.get(invoice_id)
        if inv is None:
            return None
        if inv.is_expired(now=self._now()):
            # Don't return expired invoices. The client must
            # request a new one.
            return None
        return inv

    def get_invoice_by_nonce(self, nonce: str) -> Invoice | None:
        inv_id = self._seen_nonces.get(nonce)
        if inv_id is None:
            return None
        return self.get_invoice(inv_id)

    # ── receipt management ──
    def record_receipt(self, r: PaymentReceipt) -> None:
        self._receipts[r.invoice_id] = r

    def get_receipt(self, invoice_id: str) -> PaymentReceipt | None:
        r = self._receipts.get(invoice_id)
        if r is None:
            return None
        if self._now() >= r.expires_at:
            # Purge on read. The audit log already has it.
            self._receipts.pop(invoice_id, None)
            return None
        return r

    def has_paid(self, nonce: str) -> bool:
        inv = self.get_invoice_by_nonce(nonce)
        if inv is None:
            return False
        return self.get_receipt(inv.invoice_id) is not None

    # ── housekeeping ──
    def purge_expired(self) -> int:
        """Remove expired invoices and receipts. Returns the count."""
        now = self._now()
        n = 0
        # Invoices
        for inv_id, inv in list(self._invoices.items()):
            if inv.is_expired(now=now):
                self._invoices.pop(inv_id, None)
                self._seen_nonces.pop(inv.nonce, None)
                n += 1
        # Receipts
        for inv_id, r in list(self._receipts.items()):
            if now >= r.expires_at:
                self._receipts.pop(inv_id, None)
                n += 1
        return n

    def stats(self) -> dict[str, int]:
        return {
            "invoices": len(self._invoices),
            "receipts": len(self._receipts),
            "nonces": len(self._seen_nonces),
        }


# ── The X402 service ────────────────────────────────────────


class X402Service:
    """The server-side x402 protocol handler.

    Usage pattern (in a FastAPI handler):

        @app.get("/api/expensive")
        def expensive(x402: X402Service = Depends(get_x402)):
            # Did the request include a payment proof?
            try:
                receipt = x402.settle_request(
                    invoice_id=request.headers.get(H_INVOICE),
                    tx_hash=request.headers.get(H_TX),
                    payer=request.headers.get(H_PAYER),
                    resource="/api/expensive",
                    required_amount=Money.from_major("0.10"),
                    automaton_id=aid,
                )
            except PaymentRequired:
                # Build a 402 response with the invoice headers
                invoice = x402.issue_invoice(...)
                raise HTTPException(402, headers=invoice.to_headers())
            # Receipt is valid — serve the resource.
            return {"result": "..."}

    The service is stateless except for the `PaymentRegistry` and
    the underlying `WalletBackend`. Multiple agents can share one
    service if they all use the same wallet; for per-agent
    isolation, instantiate one service per agent.
    """

    def __init__(
        self,
        *,
        backend: WalletBackend,
        registry: PaymentRegistry | None = None,
        verifier: PaymentVerifier | None = None,
        chain: str = DEFAULT_CHAIN,
        token: str = DEFAULT_TOKEN,
        invoice_ttl_seconds: int = DEFAULT_INVOICE_TTL_SECONDS,
        receipt_ttl_seconds: int = DEFAULT_RECEIPT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend
        self.registry = registry or PaymentRegistry(
            invoice_ttl_seconds=invoice_ttl_seconds,
            receipt_ttl_seconds=receipt_ttl_seconds,
            clock=clock,
        )
        self.verifier: PaymentVerifier = verifier or MockVerifier()
        self.chain = chain
        self.token = token
        self._clock = clock

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    # ── invoice issuance ──
    def issue_invoice(
        self,
        *,
        automaton_id: AutomatonId,
        amount: Money,
        resource: str,
        memo: str | None = None,
    ) -> Invoice:
        """Create a new invoice and register it. The caller is
        expected to surface the invoice's headers in a 402 response."""
        if amount.micro <= 0:
            raise ValidationError("invoice amount must be positive")
        if amount.currency != "USDC":
            raise ValidationError(
                f"x402 currently supports USDC only, got {amount.currency}"
            )
        now = self._now()
        inv = Invoice(
            invoice_id=f"inv_{uuid.uuid4().hex}",
            nonce=secrets.token_hex(32),
            amount=amount,
            address=self.backend.address(),
            chain=self.chain,
            token=self.token,
            memo=memo,
            resource=resource,
            automaton_id=automaton_id,
            issued_at=now,
            expires_at=now + timedelta(
                seconds=self.registry._invoice_ttl  # noqa: SLF001
            ),
        )
        self.registry.record_invoice(inv)
        log.info(
            "x402_invoice_issued",
            extra={
                "invoice_id": inv.invoice_id,
                "amount_micro": amount.micro,
                "resource": resource,
            },
        )
        return inv

    # ── settlement ──
    async def settle_request(
        self,
        *,
        invoice_id: str | None,
        tx_hash: str | None,
        payer: str | None,
        resource: str,
        automaton_id: AutomatonId,
        required_amount: Money,
    ) -> PaymentReceipt:
        """Verify a payment proof and credit the wallet.

        Returns the `PaymentReceipt` on success. Raises:
          - `PaymentRequired` if no proof was supplied, or the
            proof is invalid, or the invoice has expired, or the
            nonce has already been used. The caller is expected
            to translate this into a 402 with a fresh invoice.
          - `ValidationError` if the proof is malformed.
        """
        # No proof at all → ask for payment.
        if not invoice_id or not tx_hash or not payer:
            raise PaymentRequired("no payment proof")
        inv = self.registry.get_invoice(invoice_id)
        if inv is None:
            # Either the invoice id is unknown, or it has expired.
            raise PaymentRequired("unknown or expired invoice")
        if inv.automaton_id != automaton_id:
            # Defense in depth: an invoice for agent A cannot be
            # used to pay for a resource served by agent B.
            raise PaymentRequired("invoice is for a different agent")
        if inv.resource != resource:
            # Bind the invoice to the specific resource it was
            # issued for. A client can't pay for /api/a and use
            # the receipt at /api/b.
            raise PaymentRequired("invoice is for a different resource")
        if inv.amount.micro != required_amount.micro:
            raise PaymentRequired(
                f"amount mismatch: invoice is {inv.amount.micro}, "
                f"resource requires {required_amount.micro}"
            )
        if inv.is_expired(now=self._now()):
            raise PaymentRequired("invoice has expired")
        if self.registry.has_paid(inv.nonce):
            # Single-use nonce. If the client retries the same
            # proof twice, we accept it (idempotent retry) but
            # we don't double-credit.
            existing = self.registry.get_receipt(inv.invoice_id)
            if existing is not None:
                return existing
        # Verify the on-chain payment.
        try:
            ok = await self.verifier.verify(
                invoice=inv,
                tx_hash=tx_hash,
                payer=payer,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("x402_verifier_error", extra={"err": str(e)})
            raise PaymentRequired("verifier failed") from e
        if not ok:
            raise PaymentRequired("payment proof invalid")
        # Credit the wallet. The MockBackend's `receive_payment_micro`
        # models the inbound leg; the real ChainBackend would call
        # `balanceOf` to confirm the deposit.
        try:
            await self.backend.receive_payment_micro(inv.amount.micro)
        except Exception as e:  # noqa: BLE001
            # The payment was real but our wallet didn't see it.
            # Could be a race; raise so the client can retry.
            log.warning("x402_credit_failed", extra={"err": str(e)})
            raise PaymentRequired("wallet not credited") from e
        # Mark the invoice as settled and record a receipt.
        inv.settled = True
        inv.settlement_tx = tx_hash
        inv.payer = payer
        inv.settled_at = self._now()
        now = self._now()
        receipt = PaymentReceipt(
            invoice_id=inv.invoice_id,
            nonce=inv.nonce,
            tx_hash=tx_hash,
            payer=payer,
            amount=inv.amount,
            resource=resource,
            automaton_id=automaton_id,
            received_at=now,
            expires_at=now + timedelta(
                seconds=self.registry._receipt_ttl  # noqa: SLF001
            ),
        )
        self.registry.record_receipt(receipt)
        log.info(
            "x402_payment_settled",
            extra={
                "invoice_id": inv.invoice_id,
                "tx_hash": tx_hash,
                "amount_micro": inv.amount.micro,
                "resource": resource,
            },
        )
        return receipt

    # ── inspection ──
    def stats(self) -> dict[str, Any]:
        return {
            "wallet_address": self.backend.address(),
            "chain": self.chain,
            "token": self.token,
            **self.registry.stats(),
        }


# ── Custom exception ───────────────────────────────────────


class PaymentRequired(Exception):
    """Raised by `settle_request` when the request needs a 402
    response. The caller should catch this, issue a fresh invoice,
    and return a 402 with the invoice's headers.

    The exception's `reason` is informational — useful for logs
    and for the X-Payment-Memo header on retry.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── X402 registry ─────────────────────────────────────────


class X402Registry:
    """A registry mapping `AutomatonId` to `X402Service`.

    The platform runs many agents; each one has its own wallet
    and its own payment ledger. The registry is a thin index
    over those services so a route handler can find the right
    one by `aid`.

    The registry does not own the underlying `WalletBackend`s —
    those are owned by the agent's `HelixTreasury`. The
    registry just holds the per-agent `X402Service` which wraps
    a reference to the same backend. If two agents somehow
    shared a backend, payments from one would land in the
    other's wallet, which is exactly the kind of bug the
    per-agent `X402Service` makes obvious.

    Concurrency: the registry is guarded by a single lock.
    Lookups are O(1); the lock is held only for the dict
    access, not for the underlying X402Service call.
    """

    def __init__(self) -> None:
        self._services: dict[AutomatonId, X402Service] = {}
        self._lock = __import__("asyncio").Lock()

    def register(self, aid: AutomatonId, service: X402Service) -> None:
        self._services[aid] = service

    def get(self, aid: AutomatonId) -> X402Service | None:
        return self._services.get(aid)

    def all(self) -> list[X402Service]:
        return list(self._services.values())

    def stats(self) -> dict[str, Any]:
        return {
            "agents": len(self._services),
            "services": [s.stats() for s in self._services.values()],
        }


# ── Factory ─────────────────────────────────────────────────


def make_x402(
    *,
    backend: WalletBackend | None = None,
    verifier: PaymentVerifier | None = None,
    **kwargs: Any,
) -> X402Service:
    """Convenience factory. Defaults to a `MockBackend` if no
    backend is supplied (useful in tests)."""
    b = backend or MockBackend()
    return X402Service(backend=b, verifier=verifier, **kwargs)


__all__ = [
    "ALL_INVOICE_HEADERS",
    "ALL_RECEIPT_HEADERS",
    "DEFAULT_CHAIN",
    "DEFAULT_INVOICE_TTL_SECONDS",
    "DEFAULT_RECEIPT_TTL_SECONDS",
    "DEFAULT_TOKEN",
    "Invoice",
    "MockBackend",
    "MockVerifier",
    "H_ADDRESS",
    "H_AMOUNT",
    "H_CHAIN",
    "H_EXPIRES",
    "H_INVOICE",
    "H_MEMO",
    "H_NONCE",
    "H_PAYER",
    "H_TOKEN",
    "H_TX",
    "H_VERSION",
    "PaymentReceipt",
    "PaymentRegistry",
    "PaymentRequired",
    "PaymentVerifier",
    "WalletBackend",
    "X402Registry",
    "X402Service",
    "X402_VERSION",
    "make_x402",
]
