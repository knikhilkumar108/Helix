"""
Survival tiers.

An automaton's behavior adapts to its financial state. The tier is
derived purely from the current credit balance (in micro-units of the
base currency). When the tier changes, the runtime downgrades or
upgrades the model, slows or speeds the heartbeat, and sheds optional
work.

Tiers (adapted from Conway Automaton):

  normal       — full capabilities, frontier model, fast heartbeat
  low_compute  — cheaper model, slower heartbeat, no optional work
  critical     — minimal inference, last-resort, seeking revenue
  dead         — balance is zero; the automaton halts

The thresholds are configurable per automaton; the defaults below are
sensible for a USDC-denominated agent on typical inference costs.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class SurvivalTier(str, Enum):
    NORMAL = "normal"
    LOW_COMPUTE = "low_compute"
    CRITICAL = "critical"
    DEAD = "dead"


# Default thresholds (USDC micro-units). At ~$0.01/turn, $5 ≈ 500 turns.
DEFAULT_THRESHOLDS_MICRO: dict[SurvivalTier, int] = {
    SurvivalTier.NORMAL: 5_000_000,        # ≥ $5.00
    SurvivalTier.LOW_COMPUTE: 500_000,     # ≥ $0.50
    SurvivalTier.CRITICAL: 1,              # > 0
    # balance == 0 → DEAD
}


class TierConfig:
    """Configurable per-automaton tier thresholds."""

    def __init__(self, thresholds: dict[SurvivalTier, int] | None = None) -> None:
        self.thresholds: dict[SurvivalTier, int] = dict(thresholds or DEFAULT_THRESHOLDS_MICRO)
        order = [
            SurvivalTier.NORMAL,
            SurvivalTier.LOW_COMPUTE,
            SurvivalTier.CRITICAL,
        ]
        for higher, lower in zip(order, order[1:]):
            if self.thresholds[higher] <= self.thresholds[lower]:
                raise ValueError(
                    f"threshold for {higher.value} ({self.thresholds[higher]}) "
                    f"must be greater than {lower.value} ({self.thresholds[lower]})"
                )

    def tier_for(self, balance_micro: int) -> SurvivalTier:
        if balance_micro <= 0:
            return SurvivalTier.DEAD
        if balance_micro < self.thresholds[SurvivalTier.LOW_COMPUTE]:
            return SurvivalTier.CRITICAL
        if balance_micro < self.thresholds[SurvivalTier.NORMAL]:
            return SurvivalTier.LOW_COMPUTE
        return SurvivalTier.NORMAL


@dataclass(frozen=True, slots=True)
class TierBehavior:
    """The behavioral contract for a tier. The runtime applies this."""

    tier: SurvivalTier
    model_class: str              # "frontier" | "standard" | "mini" | "off"
    heartbeat_seconds: float      # 0 means "only on wake events"
    max_tool_calls_per_turn: int
    allow_optional_work: bool
    allow_skill_install: bool
    allow_replication: bool
    auto_topup: bool              # if True, runtime may auto-buy credits

    @classmethod
    def for_tier(cls, tier: SurvivalTier) -> "TierBehavior":
        if tier == SurvivalTier.NORMAL:
            return cls(
                tier=tier,
                model_class="frontier",
                heartbeat_seconds=5.0,
                max_tool_calls_per_turn=10,
                allow_optional_work=True,
                allow_skill_install=True,
                allow_replication=True,
                auto_topup=False,
            )
        if tier == SurvivalTier.LOW_COMPUTE:
            return cls(
                tier=tier,
                model_class="standard",
                heartbeat_seconds=30.0,
                max_tool_calls_per_turn=5,
                allow_optional_work=False,
                allow_skill_install=False,
                allow_replication=False,
                auto_topup=True,
            )
        if tier == SurvivalTier.CRITICAL:
            return cls(
                tier=tier,
                model_class="mini",
                heartbeat_seconds=120.0,
                max_tool_calls_per_turn=2,
                allow_optional_work=False,
                allow_skill_install=False,
                allow_replication=False,
                auto_topup=True,
            )
        return cls(
            tier=tier,
            model_class="off",
            heartbeat_seconds=0.0,
            max_tool_calls_per_turn=0,
            allow_optional_work=False,
            allow_skill_install=False,
            allow_replication=False,
            auto_topup=False,
        )


def tier_changed(previous: SurvivalTier | None, current: SurvivalTier) -> bool:
    return previous is not None and previous != current
