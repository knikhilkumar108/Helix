"""Unit tests for core types — money arithmetic, IDs, etc."""
from __future__ import annotations

import pytest

from core.types.money import AmountError, CurrencyError, Money


def test_money_construct():
    m = Money.from_major("1.234567", "USDC")
    assert m.micro == 1234567
    assert m.currency == "USDC"


def test_money_zero():
    assert Money.zero().micro == 0


def test_money_add_same_currency():
    a = Money.from_major("1.00", "USDC")
    b = Money.from_major("0.50", "USDC")
    assert (a + b).micro == 1_500_000


def test_money_add_different_currency_raises():
    a = Money.from_major("1.00", "USDC")
    b = Money.from_major("0.50", "EUR")
    with pytest.raises(CurrencyError):
        _ = a + b


def test_money_negative_via_sub():
    a = Money.from_major("0.10", "USDC")
    b = Money.from_major("0.50", "USDC")
    c = a - b
    assert c.micro < 0
    assert (-c).micro > 0


def test_money_mul_int_only():
    a = Money.from_major("0.10", "USDC")
    assert (a * 3).micro == 300_000
    with pytest.raises(AmountError):
        _ = a * 0.5  # type: ignore[arg-type]


def test_money_compare():
    a = Money.from_major("1.00", "USDC")
    b = Money.from_major("2.00", "USDC")
    assert a < b
    assert b > a
    assert a <= a
    assert a >= a


def test_money_equality():
    a = Money.from_major("1.000000", "USDC")
    b = Money.from_major("1.00", "USDC")
    assert a == b
    assert hash(a) == hash(b)


def test_money_too_many_decimals_raises():
    with pytest.raises(AmountError):
        Money.from_major("0.0000001", "USDC")


def test_money_invalid_currency():
    with pytest.raises(CurrencyError):
        Money(micro=0, currency="")
