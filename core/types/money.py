"""
Money and cost representation.

All monetary values are stored as integer micro-units (1e-6 of the base unit)
to avoid floating-point errors. Currency is an explicit string. Conversion
rates are loaded from the FX oracle and cached; rates are versioned.

Costs and budgets are also expressed in micro-units, in the Automaton's
configured base currency (default: USDC).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

MAX_MICRO: Final[int] = 2**63 - 1
MIN_MICRO: Final[int] = -(2**63)


class CurrencyError(ValueError):
    pass


class AmountError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Money:
    """Immutable money amount in micro-units of `currency`."""

    micro: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.micro, int):
            raise AmountError(f"micro must be int, got {type(self.micro).__name__}")
        if not (MIN_MICRO <= self.micro <= MAX_MICRO):
            raise AmountError("micro out of range")
        if not isinstance(self.currency, str) or not self.currency:
            raise CurrencyError("currency must be a non-empty string")
        object.__setattr__(self, "currency", self.currency.upper())

    # -- constructors -------------------------------------------------
    @classmethod
    def zero(cls, currency: str = "USDC") -> "Money":
        return cls(0, currency)

    @classmethod
    def from_major(cls, major: Decimal | int | float | str, currency: str = "USDC") -> "Money":
        d = Decimal(str(major))
        if d != d.to_integral_value() and -d.as_tuple().exponent > 6:
            raise AmountError("more than 6 decimal places")
        return cls(int(d * Decimal(1_000_000)), currency)

    # -- conversions --------------------------------------------------
    def to_major(self) -> Decimal:
        return Decimal(self.micro) / Decimal(1_000_000)

    def __str__(self) -> str:
        return f"{self.to_major():.6f} {self.currency}"

    # -- arithmetic (same currency) -----------------------------------
    def _assert_same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyError(
                f"currency mismatch: {self.currency} vs {other.currency}"
            )

    def __add__(self, other: "Money") -> "Money":
        self._assert_same(other)
        return Money(self.micro + other.micro, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._assert_same(other)
        return Money(self.micro - other.micro, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.micro, self.currency)

    def __mul__(self, factor: int) -> "Money":
        if not isinstance(factor, int):
            raise AmountError("can only multiply Money by int")
        return Money(self.micro * factor, self.currency)

    __rmul__ = __mul__

    # -- comparisons --------------------------------------------------
    def __lt__(self, other: "Money") -> bool:
        self._assert_same(other)
        return self.micro < other.micro

    def __le__(self, other: "Money") -> bool:
        self._assert_same(other)
        return self.micro <= other.micro

    def __gt__(self, other: "Money") -> bool:
        self._assert_same(other)
        return self.micro > other.micro

    def __ge__(self, other: "Money") -> bool:
        self._assert_same(other)
        return self.micro >= other.micro

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.micro == other.micro and self.currency == other.currency

    def __hash__(self) -> int:
        return hash((self.micro, self.currency))


@dataclass(frozen=True, slots=True)
class Cost:
    """A non-monetary cost expressed in abstract resource units (RU)."""

    ru: int
    kind: str  # 'cpu_ms' | 'gpu_ms' | 'net_bytes' | 'disk_bytes' | 'api_call' | 'tool_ms'

    def __post_init__(self) -> None:
        if not isinstance(self.ru, int) or self.ru < 0:
            raise AmountError("ru must be a non-negative int")
        if not isinstance(self.kind, str) or not self.kind:
            raise AmountError("kind must be a non-empty string")

    def __add__(self, other: "Cost") -> "Cost":
        if self.kind != other.kind:
            raise AmountError(f"cannot add costs of different kinds: {self.kind} + {other.kind}")
        return Cost(self.ru + other.ru, self.kind)

    def __mul__(self, factor: int) -> "Cost":
        return Cost(self.ru * factor, self.kind)
