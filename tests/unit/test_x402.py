"""Tests for the x402 payment service."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from core.errors.errors import ValidationError
from core.types.identifiers import new_automaton_id
from core.types.money import Money
from services.payments.x402 import (
    H_ADDRESS,
    H_AMOUNT,
    H_CHAIN,
    H_EXPIRES,
    H_INVOICE,
    H_MEMO,
    H_NONCE,
    H_PAYER,
    H_TOKEN,
    H_TX,
    H_VERSION,
    Invoice,
    MockVerifier,
    PaymentReceipt,
    PaymentRegistry,
    PaymentRequired,
    X402Registry,
    X402Service,
    make_x402,
)
from services.payments.x402_headers import (
    parse_payment_headers,
    render_payment_required,
)
from services.treasury.helix_treasury import MockBackend


# ── Helpers ───────────────────────────────────────────────────


def _ts(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def _make_invoice(
    aid,
    *,
    amount: Money | None = None,
    resource: str = "/api/foo",
    issued_at_epoch: float | None = None,
    ttl_seconds: int = 300,
    nonce: str | None = None,
    invoice_id: str | None = None,
) -> Invoice:
    if issued_at_epoch is None:
        issued_at_epoch = time.time()
    return Invoice(
        invoice_id=invoice_id or f"inv_{nonce or 'x'}",
        nonce=nonce or "a" * 64,
        amount=amount or Money(100_000, "USDC"),
        address="0x" + "1" * 40,
        chain="base",
        token="USDC",
        memo=None,
        resource=resource,
        automaton_id=aid,
        issued_at=_ts(issued_at_epoch),
        expires_at=_ts(issued_at_epoch + ttl_seconds),
    )


# ── Invoice ───────────────────────────────────────────────────


def test_invoice_to_headers_has_all_required_fields():
    aid = new_automaton_id()
    inv = _make_invoice(aid, nonce="0" * 64, invoice_id="inv_abc")
    inv.memo = "test invoice"
    h = inv.to_headers()
    assert h[H_VERSION] == "x402/1"
    assert h[H_ADDRESS].startswith("0x")
    assert h[H_AMOUNT] == "100000"
    assert h[H_TOKEN] == "USDC"
    assert h[H_CHAIN] == "base"
    assert h[H_NONCE] == "0" * 64
    assert h[H_INVOICE] == "inv_abc"
    assert "T" in h[H_EXPIRES]
    assert h[H_MEMO] == "test invoice"


def test_invoice_memo_is_optional():
    aid = new_automaton_id()
    inv = _make_invoice(aid)
    h = inv.to_headers()
    assert H_MEMO not in h


def test_invoice_is_expired_compares_to_now():
    aid = new_automaton_id()
    inv = _make_invoice(aid, issued_at_epoch=100.0, ttl_seconds=10)
    assert inv.is_expired(now=_ts(105.0)) is False
    assert inv.is_expired(now=_ts(110.0)) is True
    assert inv.is_expired(now=_ts(120.0)) is True


# ── PaymentRegistry ─────────────────────────────────────────


def test_registry_records_and_retrieves_invoice():
    aid = new_automaton_id()
    inv = _make_invoice(aid, nonce="a" * 64, invoice_id="inv_a")
    reg = PaymentRegistry()
    reg.record_invoice(inv)
    assert reg.get_invoice("inv_a") is inv
    assert reg.get_invoice_by_nonce("a" * 64) is inv


def test_registry_rejects_duplicate_nonce():
    aid = new_automaton_id()
    inv = _make_invoice(aid, nonce="dupnonce", invoice_id="inv_a")
    reg = PaymentRegistry()
    reg.record_invoice(inv)
    with pytest.raises(ValidationError):
        reg.record_invoice(inv)  # same nonce


def test_registry_purges_expired():
    aid = new_automaton_id()
    clock = {"t": 1_000_000.0}
    inv = _make_invoice(aid, issued_at_epoch=0, ttl_seconds=1, invoice_id="inv_a", nonce="a" * 64)
    reg = PaymentRegistry(clock=lambda: clock["t"])
    reg.record_invoice(inv)
    # Advance past expiry.
    clock["t"] = 2.0
    assert reg.get_invoice("inv_a") is None
    purged = reg.purge_expired()
    assert purged >= 1


def test_registry_has_paid_joints_invoice_and_receipt():
    aid = new_automaton_id()
    inv = _make_invoice(aid, nonce="a" * 64, invoice_id="inv_a")
    now = time.time()
    r = PaymentReceipt(
        invoice_id="inv_a",
        nonce="a" * 64,
        tx_hash="0x" + "f" * 64,
        payer="0x" + "1" * 40,
        amount=inv.amount,
        resource=inv.resource,
        automaton_id=aid,
        received_at=_ts(now),
        expires_at=_ts(now + 3600),
    )
    reg = PaymentRegistry()
    reg.record_invoice(inv)
    reg.record_receipt(r)
    assert reg.has_paid("a" * 64) is True
    # Without the receipt, has_paid is False.
    r2 = PaymentReceipt(
        invoice_id="inv_b",
        nonce="b" * 64,
        tx_hash="0x" + "f" * 64,
        payer="0x" + "1" * 40,
        amount=inv.amount,
        resource=inv.resource,
        automaton_id=aid,
        received_at=_ts(now),
        expires_at=_ts(now + 3600),
    )
    reg.record_receipt(r2)
    # Receipt for inv_b but no invoice for inv_b → has_paid by that
    # nonce is False (the receipt references a non-existent invoice).
    assert reg.has_paid("b" * 64) is False


# ── MockVerifier ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_verifier_accepts_well_formed_proof():
    aid = new_automaton_id()
    inv = _make_invoice(aid)
    v = MockVerifier()
    assert await v.verify(
        invoice=inv,
        tx_hash="0x" + "f" * 64,
        payer="0x" + "1" * 40,
    ) is True


@pytest.mark.asyncio
async def test_mock_verifier_rejects_malformed_proof():
    aid = new_automaton_id()
    inv = _make_invoice(aid)
    v = MockVerifier()
    # Bad tx hash (no 0x prefix)
    assert await v.verify(invoice=inv, tx_hash="ff" * 32, payer="0x" + "1" * 40) is False
    # Bad payer (too short)
    assert await v.verify(invoice=inv, tx_hash="0x" + "f" * 64, payer="0x1234") is False


@pytest.mark.asyncio
async def test_mock_verifier_rejects_expired_invoice():
    aid = new_automaton_id()
    inv = _make_invoice(aid, issued_at_epoch=0, ttl_seconds=1)
    v = MockVerifier()
    assert await v.verify(
        invoice=inv,
        tx_hash="0x" + "f" * 64,
        payer="0x" + "1" * 40,
    ) is False


# ── X402Service — invoice issuance ──────────────────────────


@pytest.mark.asyncio
async def test_issue_invoice_creates_unique_nonces():
    backend = MockBackend(initial_usdc_micro=0)
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    i1 = svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.10"), resource="/api/a")
    i2 = svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.10"), resource="/api/b")
    assert i1.invoice_id != i2.invoice_id
    assert i1.nonce != i2.nonce


def test_issue_invoice_rejects_zero_amount():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    with pytest.raises(ValidationError):
        svc.issue_invoice(automaton_id=aid, amount=Money.zero(), resource="/api/a")


def test_issue_invoice_rejects_non_usdc():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    with pytest.raises(ValidationError):
        svc.issue_invoice(automaton_id=aid, amount=Money(1000, "ETH"), resource="/api/a")


# ── X402Service — settlement ────────────────────────────────


@pytest.mark.asyncio
async def test_settle_request_no_proof_raises_payment_required():
    backend = MockBackend(initial_usdc_micro=0)
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    with pytest.raises(PaymentRequired):
        await svc.settle_request(
            invoice_id=None,
            tx_hash=None,
            payer=None,
            resource="/api/a",
            automaton_id=aid,
            required_amount=Money.from_major("0.10"),
        )


@pytest.mark.asyncio
async def test_settle_request_unknown_invoice_raises():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    with pytest.raises(PaymentRequired):
        await svc.settle_request(
            invoice_id="inv_does_not_exist",
            tx_hash="0x" + "f" * 64,
            payer="0x" + "1" * 40,
            resource="/api/a",
            automaton_id=aid,
            required_amount=Money.from_major("0.10"),
        )


@pytest.mark.asyncio
async def test_full_round_trip_issue_pay_serve():
    """End-to-end: issue an invoice, then settle it with a valid
    payment proof. The wallet should be credited with the amount."""
    backend = MockBackend(initial_usdc_micro=0)
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    amount = Money.from_major("0.25")

    inv = svc.issue_invoice(automaton_id=aid, amount=amount, resource="/api/research")
    assert inv.amount.micro == 250_000
    assert backend.address() == inv.address

    bal_before = await backend.get_usdc_balance_micro()
    assert bal_before == 0

    receipt = await svc.settle_request(
        invoice_id=inv.invoice_id,
        tx_hash="0x" + "a" * 64,
        payer="0x" + "b" * 40,
        resource="/api/research",
        automaton_id=aid,
        required_amount=amount,
    )
    assert receipt.tx_hash == "0x" + "a" * 64
    assert receipt.amount.micro == 250_000

    bal_after = await backend.get_usdc_balance_micro()
    assert bal_after == 250_000


@pytest.mark.asyncio
async def test_settle_rejects_wrong_amount():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    inv = svc.issue_invoice(
        automaton_id=aid,
        amount=Money.from_major("0.10"),
        resource="/api/a",
    )
    with pytest.raises(PaymentRequired):
        await svc.settle_request(
            invoice_id=inv.invoice_id,
            tx_hash="0x" + "a" * 64,
            payer="0x" + "b" * 40,
            resource="/api/a",
            automaton_id=aid,
            required_amount=Money.from_major("0.50"),
        )


@pytest.mark.asyncio
async def test_settle_rejects_wrong_resource():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    inv = svc.issue_invoice(
        automaton_id=aid,
        amount=Money.from_major("0.10"),
        resource="/api/a",
    )
    with pytest.raises(PaymentRequired):
        await svc.settle_request(
            invoice_id=inv.invoice_id,
            tx_hash="0x" + "a" * 64,
            payer="0x" + "b" * 40,
            resource="/api/b",
            automaton_id=aid,
            required_amount=Money.from_major("0.10"),
        )


@pytest.mark.asyncio
async def test_settle_rejects_wrong_automaton():
    backend = MockBackend()
    aid_a = new_automaton_id()
    aid_b = new_automaton_id()
    svc = X402Service(backend=backend)
    inv = svc.issue_invoice(automaton_id=aid_a, amount=Money.from_major("0.10"), resource="/api/a")
    with pytest.raises(PaymentRequired):
        await svc.settle_request(
            invoice_id=inv.invoice_id,
            tx_hash="0x" + "a" * 64,
            payer="0x" + "b" * 40,
            resource="/api/a",
            automaton_id=aid_b,
            required_amount=Money.from_major("0.10"),
        )


@pytest.mark.asyncio
async def test_settle_is_idempotent_on_retry():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    inv = svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.10"), resource="/api/a")

    await svc.settle_request(
        invoice_id=inv.invoice_id,
        tx_hash="0x" + "a" * 64,
        payer="0x" + "b" * 40,
        resource="/api/a",
        automaton_id=aid,
        required_amount=Money.from_major("0.10"),
    )
    bal_after_first = await backend.get_usdc_balance_micro()

    await svc.settle_request(
        invoice_id=inv.invoice_id,
        tx_hash="0x" + "a" * 64,
        payer="0x" + "b" * 40,
        resource="/api/a",
        automaton_id=aid,
        required_amount=Money.from_major("0.10"),
    )
    bal_after_second = await backend.get_usdc_balance_micro()

    assert bal_after_first == 100_000
    assert bal_after_second == 100_000


@pytest.mark.asyncio
async def test_settle_rejects_expired_invoice():
    backend = MockBackend()
    aid = new_automaton_id()
    clock = {"t": 1_000_000.0}
    svc = X402Service(backend=backend, clock=lambda: clock["t"])
    inv = svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.10"), resource="/api/a")

    clock["t"] += 600  # 10 minutes

    with pytest.raises(PaymentRequired):
        await svc.settle_request(
            invoice_id=inv.invoice_id,
            tx_hash="0x" + "a" * 64,
            payer="0x" + "b" * 40,
            resource="/api/a",
            automaton_id=aid,
            required_amount=Money.from_major("0.10"),
        )


@pytest.mark.asyncio
async def test_settle_rejects_bad_payment_proof():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    inv = svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.10"), resource="/api/a")
    with pytest.raises(PaymentRequired):
        await svc.settle_request(
            invoice_id=inv.invoice_id,
            tx_hash="0xshort",
            payer="0x" + "b" * 40,
            resource="/api/a",
            automaton_id=aid,
            required_amount=Money.from_major("0.10"),
        )


# ── Custom verifier injection ───────────────────────────────


class _AlwaysFailsVerifier:
    async def verify(self, *, invoice, tx_hash, payer) -> bool:
        return False


@pytest.mark.asyncio
async def test_custom_verifier_can_reject():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend, verifier=_AlwaysFailsVerifier())
    inv = svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.10"), resource="/api/a")
    with pytest.raises(PaymentRequired):
        await svc.settle_request(
            invoice_id=inv.invoice_id,
            tx_hash="0x" + "a" * 64,
            payer="0x" + "b" * 40,
            resource="/api/a",
            automaton_id=aid,
            required_amount=Money.from_major("0.10"),
        )


class _ExplodingVerifier:
    async def verify(self, *, invoice, tx_hash, payer) -> bool:
        raise RuntimeError("chain RPC down")


@pytest.mark.asyncio
async def test_verifier_exception_translates_to_payment_required():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend, verifier=_ExplodingVerifier())
    inv = svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.10"), resource="/api/a")
    with pytest.raises(PaymentRequired) as exc:
        await svc.settle_request(
            invoice_id=inv.invoice_id,
            tx_hash="0x" + "a" * 64,
            payer="0x" + "b" * 40,
            resource="/api/a",
            automaton_id=aid,
            required_amount=Money.from_major("0.10"),
        )
    assert "verifier failed" in exc.value.reason


# ── make_x402 factory ───────────────────────────────────────


def test_make_x402_default_backend():
    svc = make_x402()
    assert svc.backend is not None
    assert svc.chain == "base"
    assert svc.token == "USDC"


def test_make_x402_with_explicit_backend():
    backend = MockBackend(initial_usdc_micro=1_000_000)
    svc = make_x402(backend=backend)
    assert svc.backend is backend


# ── HTTP header adapter ─────────────────────────────────────


def test_parse_payment_headers_extracts_proof():
    class _H:
        def get(self, k):
            return {
                H_INVOICE: "inv_x",
                H_TX: "0xabc",
                H_PAYER: "0xdef",
            }.get(k)
    parsed = parse_payment_headers(_H())
    assert parsed == {"invoice_id": "inv_x", "tx_hash": "0xabc", "payer": "0xdef"}


def test_parse_payment_headers_handles_missing():
    class _H:
        def get(self, k):
            return None
    parsed = parse_payment_headers(_H())
    assert parsed == {"invoice_id": None, "tx_hash": None, "payer": None}


def test_render_payment_required_has_status_402():
    aid = new_automaton_id()
    inv = _make_invoice(aid, invoice_id="inv_a", nonce="a" * 64)
    inv.memo = "test"
    resp = render_payment_required(inv, reason="no money")
    assert resp.status_code == 402
    assert resp.headers[H_INVOICE] == "inv_a"
    assert resp.headers[H_AMOUNT] == "100000"
    assert resp.headers[H_ADDRESS].startswith("0x")


# ── Stats / inspection ──────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_reports_registry_size():
    backend = MockBackend()
    aid = new_automaton_id()
    svc = X402Service(backend=backend)
    svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.10"), resource="/api/a")
    inv2 = svc.issue_invoice(automaton_id=aid, amount=Money.from_major("0.20"), resource="/api/b")
    s = svc.stats()
    assert s["invoices"] == 2
    assert s["nonces"] == 2
    assert s["wallet_address"].startswith("0x")
    assert s["chain"] == "base"

    await svc.settle_request(
        invoice_id=inv2.invoice_id,
        tx_hash="0x" + "a" * 64,
        payer="0x" + "b" * 40,
        resource="/api/b",
        automaton_id=aid,
        required_amount=Money.from_major("0.20"),
    )
    s = svc.stats()
    assert s["receipts"] == 1


# ── X402Registry ──────────────────────────────────────────


def test_registry_register_and_get():
    aid = new_automaton_id()
    backend = MockBackend()
    svc = X402Service(backend=backend)
    reg = X402Registry()
    reg.register(aid, svc)
    assert reg.get(aid) is svc
    assert reg.get(new_automaton_id()) is None


def test_registry_stats_lists_all_services():
    aid_a = new_automaton_id()
    aid_b = new_automaton_id()
    reg = X402Registry()
    reg.register(aid_a, X402Service(backend=MockBackend()))
    reg.register(aid_b, X402Service(backend=MockBackend()))
    s = reg.stats()
    assert s["agents"] == 2
    assert len(s["services"]) == 2
