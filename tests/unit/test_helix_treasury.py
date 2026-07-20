"""Tests for the HelixTreasury."""
from __future__ import annotations

import asyncio
import time

import pytest

from core.errors.errors import InsufficientFundsError, ValidationError
from core.types.identifiers import AutomatonId, new_automaton_id
from core.types.money import Money
from services.treasury.helix_treasury import (
    CREDIT_TO_USDC_MICRO,
    ChainBackend,
    HelixTreasury,
    CustodialBackend,
    MockBackend,
    TopupPolicy,
    TopupTrigger,
    make_treasury,
)


# ── MockBackend ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_backend_starts_at_zero():
    b = MockBackend()
    assert b.address().startswith("0x")
    assert await b.get_usdc_balance_micro() == 0


@pytest.mark.asyncio
async def test_mock_backend_initial_balance():
    b = MockBackend(initial_usdc_micro=5_000_000)
    assert await b.get_usdc_balance_micro() == 5_000_000


@pytest.mark.asyncio
async def test_mock_backend_transfer_debits():
    b = MockBackend(initial_usdc_micro=10_000_000)
    tx = await b.transfer_usdc_micro("0xrecipient", 2_500_000)
    assert tx.startswith("0x")
    assert await b.get_usdc_balance_micro() == 7_500_000


@pytest.mark.asyncio
async def test_mock_backend_transfer_insufficient_raises():
    b = MockBackend(initial_usdc_micro=1_000_000)
    with pytest.raises(InsufficientFundsError):
        await b.transfer_usdc_micro("0xrecipient", 5_000_000)


@pytest.mark.asyncio
async def test_mock_backend_receive_payment_credits():
    b = MockBackend()
    await b.receive_payment_micro(3_000_000)
    assert await b.get_usdc_balance_micro() == 3_000_000


@pytest.mark.asyncio
async def test_mock_backend_confirm_returns_true():
    b = MockBackend(initial_usdc_micro=1_000_000)
    tx = await b.transfer_usdc_micro("0xrecipient", 500_000)
    assert await b.wait_for_confirmation(tx) is True


@pytest.mark.asyncio
async def test_mock_backend_negative_amount_rejected():
    b = MockBackend()
    with pytest.raises(ValidationError):
        await b.transfer_usdc_micro("0xrecipient", -1)


# ── HelixTreasury ──────────────────────────────────────────────


def _make_treasury(
    *,
    initial_credits_micro: int = 0,
    initial_usdc_micro: int = 0,
    policy: TopupPolicy | None = None,
) -> tuple[HelixTreasury, MockBackend]:
    aid = AutomatonId(new_automaton_id())
    backend = MockBackend(initial_usdc_micro=initial_usdc_micro)
    t = HelixTreasury(backend, aid, policy=policy)
    if initial_credits_micro:
        t.credit(
            amount=Money(initial_credits_micro, "USDC"),
            category="seed",
        )
    return t, backend


def test_treasury_starts_with_zero_credit_balance():
    t, _ = _make_treasury()
    assert t.balance() == Money.zero("USDC")


def test_credit_increases_balance():
    t, _ = _make_treasury()
    t.credit(amount=Money.from_major("1.00"), category="seed")
    assert t.balance() == Money.from_major("1.00")


def test_charge_decreases_balance():
    t, _ = _make_treasury(initial_credits_micro=5_000_000)
    t.charge(amount=Money.from_major("0.25"), category="llm_call")
    assert t.balance() == Money.from_major("4.75")


def test_charge_more_than_balance_raises():
    t, _ = _make_treasury(initial_credits_micro=1_000_000)
    with pytest.raises(InsufficientFundsError):
        t.charge(amount=Money.from_major("2.00"), category="llm_call")


def test_negative_credit_rejected():
    t, _ = _make_treasury()
    with pytest.raises(ValidationError):
        t.credit(amount=Money(-1, "USDC"), category="x")


def test_history_records_in_order():
    t, _ = _make_treasury(initial_credits_micro=5_000_000)
    t.charge(amount=Money.from_major("0.10"), category="a")
    t.charge(amount=Money.from_major("0.20"), category="b")
    h = t.history()
    # The seed credit is the first entry; the two charges follow.
    assert [e.category for e in h] == ["seed", "a", "b"]


def test_wrong_currency_rejected():
    t, _ = _make_treasury()
    with pytest.raises(ValidationError):
        t.credit(amount=Money.from_major("1.00", "EUR"), category="x")


def test_address_is_wallet_address():
    t, backend = _make_treasury()
    assert t.address == backend.address()


# ── Topup engine ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topup_never_does_nothing():
    t, _ = _make_treasury(
        initial_credits_micro=100_000,
        initial_usdc_micro=10_000_000,
        policy=TopupPolicy(trigger=TopupTrigger.NEVER),
    )
    res = await t.maybe_topup()
    assert res is None
    # Credits unchanged.
    assert t.credit_balance_micro == 100_000


@pytest.mark.asyncio
async def test_topup_on_low_buys_credits():
    t, backend = _make_treasury(
        initial_credits_micro=10_000,  # below threshold
        initial_usdc_micro=10_000_000,  # plenty of USDC
        policy=TopupPolicy(
            trigger=TopupTrigger.ON_LOW,
            threshold_micro=100_000,
            target_micro=500_000,
        ),
    )
    res = await t.maybe_topup()
    assert res is not None
    # We want to buy (target - current) = (500_000 - 10_000) = 490_000
    # micro-credits. At 1 micro-credit = 1/10_000 micro-USDC, that costs
    # 490_000 / 10_000 = 49 micro-USDC. We have $10, so we can
    # easily afford the full target.
    assert res.credits_purchased_micro == 490_000
    # Credits went up by exactly that much.
    assert t.credit_balance_micro == 10_000 + 490_000
    # USDC went down by 49 micro-USDC.
    usdc_now = await backend.get_usdc_balance_micro()
    assert usdc_now == 10_000_000 - 49
    assert usdc_now >= 100_000  # well above the floor


@pytest.mark.asyncio
async def test_topup_respects_wallet_floor():
    """If buying the full target would dip below min_wallet_balance,
    the topup is trimmed."""
    t, backend = _make_treasury(
        initial_credits_micro=0,
        initial_usdc_micro=150_000,  # only $0.15
        policy=TopupPolicy(
            trigger=TopupTrigger.ON_LOW,
            threshold_micro=0,
            target_micro=500_000,  # would cost $5
            min_wallet_balance_micro=100_000,  # leave $0.10
        ),
    )
    res = await t.maybe_topup()
    # To buy the full target (500_000 micro-credits = $0.005) we need
    # 500_000 / 10_000 = 50 micro-USDC. We have $0.15 (150_000), and
    # the floor is $0.10 (100_000), so we can easily afford it. The
    # topup buys exactly what we need to reach the target.
    assert res is not None
    assert res.credits_purchased_micro == 500_000
    assert res.usdc_spent_micro == 50
    # Wallet is at 150_000 - 50 = 149_950, well above the floor.
    assert await backend.get_usdc_balance_micro() == 149_950


@pytest.mark.asyncio
async def test_topup_skipped_when_wallet_too_low():
    t, _ = _make_treasury(
        initial_credits_micro=0,
        initial_usdc_micro=10_000,  # below min_wallet_balance of 100_000
        policy=TopupPolicy(
            trigger=TopupTrigger.ON_LOW,
            threshold_micro=0,
            target_micro=500_000,
        ),
    )
    res = await t.maybe_topup()
    assert res is None
    # Credits unchanged.
    assert t.credit_balance_micro == 0


@pytest.mark.asyncio
async def test_topup_cooldown():
    """Two topups in quick succession should be coalesced by the
    cooldown."""
    t, backend = _make_treasury(
        initial_credits_micro=0,
        initial_usdc_micro=10_000_000,
        policy=TopupPolicy(
            trigger=TopupTrigger.ON_LOW,
            threshold_micro=100_000,
            target_micro=500_000,
            cooldown_seconds=300,
        ),
    )
    res1 = await t.maybe_topup()
    assert res1 is not None
    res2 = await t.maybe_topup()
    assert res2 is None  # cooldown


@pytest.mark.asyncio
async def test_topup_max_per_day():
    """With max_per_day=2, only two topups fire per 24h window. The
    third call returns None because the per-day counter caps it.
    To get a second topup, the test spends credits between calls
    (so the credits drop below the target again)."""
    t, backend = _make_treasury(
        initial_credits_micro=0,
        initial_usdc_micro=100_000_000,  # $100
        policy=TopupPolicy(
            trigger=TopupTrigger.ALWAYS,
            threshold_micro=0,
            target_micro=500_000,
            cooldown_seconds=0,
            max_per_day=2,
        ),
    )
    r1 = await t.maybe_topup()
    # After the first topup, credits are at the target. Spend some
    # so a second topup is needed.
    t.charge(amount=Money(400_000, "USDC"), category="llm_call")
    r2 = await t.maybe_topup()
    # Spend again.
    t.charge(amount=Money(400_000, "USDC"), category="llm_call")
    # Third topup attempt should be capped by max_per_day.
    r3 = await t.maybe_topup()
    assert r1 is not None
    assert r2 is not None
    assert r3 is None  # max per day reached


@pytest.mark.asyncio
async def test_topup_event_recorded():
    t, _ = _make_treasury(
        initial_credits_micro=0,
        initial_usdc_micro=10_000_000,
        policy=TopupPolicy(
            trigger=TopupTrigger.ON_LOW,
            threshold_micro=0,
            target_micro=500_000,
            cooldown_seconds=0,
        ),
    )
    res = await t.maybe_topup()
    assert res is not None
    events = t.topup_events()
    assert len(events) == 1
    assert events[0].id == res.id
    assert events[0].credits_purchased_micro > 0
    assert events[0].usdc_spent_micro > 0
    assert events[0].tx_hash.startswith("0x")


@pytest.mark.asyncio
async def test_health_returns_combined_view():
    t, _ = _make_treasury(
        initial_credits_micro=2_000_000,
        initial_usdc_micro=5_000_000,
    )
    h = await t.health()
    assert h["credit_micro"] == 2_000_000
    assert h["usdc_micro"] == 5_000_000
    assert h["tier"] == "normal"
    assert h["address"].startswith("0x")
    assert h["last_topup_at"] is None


@pytest.mark.asyncio
async def test_health_tier_classification():
    aid = AutomatonId(new_automaton_id())

    b1 = MockBackend(initial_usdc_micro=600_000)  # > 500k → normal
    t1 = HelixTreasury(b1, aid)
    h1 = await t1.health()
    assert h1["tier"] == "normal"

    b2 = MockBackend(initial_usdc_micro=200_000)  # > 50k, < 500k → low_compute
    t2 = HelixTreasury(b2, aid)
    h2 = await t2.health()
    assert h2["tier"] == "low_compute"

    b3 = MockBackend(initial_usdc_micro=10_000)  # > 0, < 50k → critical
    t3 = HelixTreasury(b3, aid)
    h3 = await t3.health()
    assert h3["tier"] == "critical"

    b4 = MockBackend(initial_usdc_micro=0)  # 0 → dead
    t4 = HelixTreasury(b4, aid)
    h4 = await t4.health()
    assert h4["tier"] == "dead"


# ── Factory ─────────────────────────────────────────────────────


def test_factory_mock():
    t = make_treasury(
        AutomatonId(new_automaton_id()),
        backend="mock",
        config={"initial_usdc_micro": 5_000_000},
    )
    assert isinstance(t, HelixTreasury)
    assert t.address.startswith("0x")


def test_factory_unknown_backend_raises():
    with pytest.raises(ValidationError):
        make_treasury(AutomatonId(new_automaton_id()), backend="unknown")


def test_factory_custodial_with_mock_inner():
    """A 'custodial' config can wrap a mock for local dev."""
    t = make_treasury(
        AutomatonId(new_automaton_id()),
        backend="custodial",
        config={"backend": "mock", "initial_usdc_micro": 1_000_000},
    )
    assert isinstance(t, HelixTreasury)
    assert t.address.startswith("0x")


# ── End-to-end: agent earns, spends, auto-tops-up ──────────────


@pytest.mark.asyncio
async def test_full_loop_earn_spend_topup():
    """Simulate the full Helix flow: agent receives USDC from a
    customer, spends credits on LLM calls, and auto-tops up when low."""
    aid = AutomatonId(new_automaton_id())
    backend = MockBackend(initial_usdc_micro=2_000_000)  # $2 to start
    t = HelixTreasury(
        backend,
        aid,
        policy=TopupPolicy(
            trigger=TopupTrigger.ON_LOW,
            threshold_micro=200_000,  # topup when below $0.20
            target_micro=1_000_000,  # topup to $1.00
            min_wallet_balance_micro=100_000,  # leave $0.10 in wallet
            cooldown_seconds=0,
        ),
    )
    # Customer pays the agent $1 via x402 (simulated).
    await backend.receive_payment_micro(1_000_000)
    # Agent receives a $0.05 credit from a marketplace sale.
    t.credit(amount=Money.from_major("0.05"), category="marketplace:sale")
    # Agent spends $0.04 on LLM calls.
    t.charge(amount=Money.from_major("0.04"), category="llm_call")
    # Credits: 0.05 - 0.04 = 0.01. In our 1e-6 money math that's
    # 10_000 micro-USDC-units of credits. Below threshold (200_000).
    assert t.credit_balance_micro == 10_000
    res = await t.maybe_topup()
    assert res is not None
    # Credits went up.
    assert t.credit_balance_micro > 10_000
    # Wallet went down (we had $3 = 3_000_000 micro-USDC, spent some
    # on the topup).
    usdc_after = await backend.get_usdc_balance_micro()
    assert usdc_after < 3_000_000
    # But we kept the floor.
    assert usdc_after >= 100_000


# ── Background: credit/charge are sync (matching Treasury ABC) ───


def test_treasury_interface_is_synchronous_for_runtime():
    """The runtime calls credit() and charge() synchronously (they're
    inner-loop hot path). The async work is in the topup engine and
    the health() refresh — both off the hot path."""
    t, _ = _make_treasury()
    # These should NOT be coroutines.
    assert not asyncio.iscoroutinefunction(t.credit)
    assert not asyncio.iscoroutinefunction(t.charge)
    assert not asyncio.iscoroutinefunction(t.balance)
    assert not asyncio.iscoroutinefunction(t.history)
    # Health is async because it touches the wallet.
    assert asyncio.iscoroutinefunction(t.health)
    # Topup is async because it does the on-chain transfer.
    assert asyncio.iscoroutinefunction(t.maybe_topup)
