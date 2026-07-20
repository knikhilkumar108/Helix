"""
Budget controller. Each Automaton has:
  - a balance (treasury)
  - a budget (rate cap)
  - a reserve (floor below which the automaton will pause itself)

Budgets are denominated in the base currency. The controller is consulted
*before* expensive operations and *after* to debit costs.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.types.money import Money


@dataclass(slots=True)
class BudgetConfig:
    reserve_floor: Money
    per_tick_max: Money
    per_day_max: Money


class BudgetController:
    def __init__(self, config: BudgetConfig, balance_getter) -> None:
        self.config = config
        self._balance = balance_getter
        self._spent_today: Money = Money.zero(config.reserve_floor.currency)

    def can_afford(self, cost: Money) -> bool:
        bal = self._balance()
        if bal < cost:
            return False
        if bal - cost < self.config.reserve_floor:
            return False
        if cost > self.config.per_tick_max:
            return False
        if self._spent_today + cost > self.config.per_day_max:
            return False
        return True

    def charge(self, cost: Money) -> None:
        self._spent_today = self._spent_today + cost

    def reset_daily(self) -> None:
        self._spent_today = Money.zero(self.config.reserve_floor.currency)
