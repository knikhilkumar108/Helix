"""
Treasury: the financial heart of an Automaton. Owns:
  - balance (single mutable value, double-entry ledger)
  - revenue ledger
  - expense ledger
  - cost history
  - financial health metrics

All operations are atomic w.r.t. the underlying store. For tests we use
the in-memory implementation; for services we use the SQL-backed one.
"""
from __future__ import annotations

import abc
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.errors.errors import InsufficientFundsError
from core.types.identifiers import AutomatonId
from core.types.money import Money


@dataclass(slots=True)
class LedgerEntry:
    id: str
    automaton_id: AutomatonId
    kind: str  # credit | debit
    amount: Money
    category: str
    ref_type: str | None = None
    ref_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    memo: str | None = None


class Treasury(abc.ABC):
    @abc.abstractmethod
    def balance(self) -> Money: ...

    @abc.abstractmethod
    def credit(
        self,
        *,
        amount: Money,
        category: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        memo: str | None = None,
    ) -> LedgerEntry: ...

    @abc.abstractmethod
    def charge(
        self,
        *,
        amount: Money,
        category: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        memo: str | None = None,
    ) -> LedgerEntry: ...

    @abc.abstractmethod
    def history(self, *, limit: int = 200) -> list[LedgerEntry]: ...

    @abc.abstractmethod
    def health(self) -> dict[str, Any]: ...


class InMemoryTreasury(Treasury):
    def __init__(self, automaton_id: AutomatonId, initial: Money | None = None) -> None:
        self.automaton_id = automaton_id
        self._balance: Money = initial or Money.zero()
        self._ledger: list[LedgerEntry] = []
        self._peak: Money = self._balance
        self._burn_window: list[tuple[float, Money]] = []  # (ts, debit)

    def balance(self) -> Money:
        return self._balance

    def credit(
        self,
        *,
        amount: Money,
        category: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        memo: str | None = None,
    ) -> LedgerEntry:
        if amount.micro < 0:
            raise ValueError("credit amount must be non-negative")
        self._balance = self._balance + amount
        if self._balance > self._peak:
            self._peak = self._balance
        e = LedgerEntry(
            id=f"led_{uuid.uuid4().hex}",
            automaton_id=self.automaton_id,
            kind="credit",
            amount=amount,
            category=category,
            ref_type=ref_type,
            ref_id=ref_id,
            memo=memo,
        )
        self._ledger.append(e)
        return e

    def charge(
        self,
        *,
        amount: Money,
        category: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        memo: str | None = None,
    ) -> LedgerEntry:
        if amount.micro < 0:
            raise ValueError("charge amount must be non-negative")
        if self._balance < amount:
            raise InsufficientFundsError(
                "insufficient funds",
                context={
                    "balance": str(self._balance),
                    "required": str(amount),
                },
            )
        self._balance = self._balance - amount
        e = LedgerEntry(
            id=f"led_{uuid.uuid4().hex}",
            automaton_id=self.automaton_id,
            kind="debit",
            amount=amount,
            category=category,
            ref_type=ref_type,
            ref_id=ref_id,
            memo=memo,
        )
        self._ledger.append(e)
        self._burn_window.append((time.time(), amount))
        return e

    def history(self, *, limit: int = 200) -> list[LedgerEntry]:
        return list(self._ledger[-limit:])

    def health(self) -> dict[str, Any]:
        now = time.time()
        # 1-hour burn rate.
        one_hour = now - 3600
        spent_1h = sum((a.micro for ts, a in self._burn_window if ts >= one_hour), start=0)
        runway_seconds: float | None = None
        if spent_1h > 0:
            runway_seconds = (self._balance.micro / (spent_1h / 3600.0)) if self._balance.micro > 0 else 0.0
        return {
            "balance": str(self._balance),
            "peak": str(self._peak),
            "burn_1h_micro": spent_1h,
            "runway_seconds": runway_seconds,
        }
