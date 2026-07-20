"""Property-based tests for the money implementation."""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from core.types.money import CurrencyError, Money


amounts = st.decimals(
    min_value=0,
    max_value=1_000_000,
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


@given(a=amounts, b=amounts)
@settings(max_examples=200, suppress_health_check=[HealthCheck.filter_too_much])
def test_addition_is_commutative(a, b):
    x = Money.from_major(a, "USDC")
    y = Money.from_major(b, "USDC")
    assert (x + y) == (y + x)


@given(a=amounts, b=amounts, c=amounts)
@settings(max_examples=200, suppress_health_check=[HealthCheck.filter_too_much])
def test_addition_is_associative(a, b, c):
    x = Money.from_major(a, "USDC")
    y = Money.from_major(b, "USDC")
    z = Money.from_major(c, "USDC")
    assert (x + y) + z == x + (y + z)


@given(a=amounts, b=amounts)
@settings(max_examples=200, suppress_health_check=[HealthCheck.filter_too_much])
def test_addition_then_subtraction_returns_original(a, b):
    x = Money.from_major(a, "USDC")
    y = Money.from_major(b, "USDC")
    if x >= y:
        assert (x + y) - y == x


def test_currency_mismatch_raises():
    a = Money.from_major(1, "USDC")
    b = Money.from_major(1, "EUR")
    with pytest.raises(CurrencyError):
        _ = a + b
