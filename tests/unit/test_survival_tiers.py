"""Tests for survival tier logic."""
from __future__ import annotations

import pytest

from core.survival.tiers import (
    DEFAULT_THRESHOLDS_MICRO,
    SurvivalTier,
    TierBehavior,
    TierConfig,
    tier_changed,
)


def test_dead_at_zero():
    c = TierConfig()
    assert c.tier_for(0) == SurvivalTier.DEAD
    assert c.tier_for(-1) == SurvivalTier.DEAD


def test_critical_just_above_zero():
    c = TierConfig()
    assert c.tier_for(1) == SurvivalTier.CRITICAL
    assert c.tier_for(499_999) == SurvivalTier.CRITICAL


def test_low_compute_above_critical():
    c = TierConfig()
    assert c.tier_for(500_000) == SurvivalTier.LOW_COMPUTE
    assert c.tier_for(4_999_999) == SurvivalTier.LOW_COMPUTE


def test_normal_above_threshold():
    c = TierConfig()
    assert c.tier_for(5_000_000) == SurvivalTier.NORMAL
    assert c.tier_for(100_000_000) == SurvivalTier.NORMAL


def test_custom_thresholds():
    c = TierConfig(
        thresholds={
            SurvivalTier.NORMAL: 1_000_000,
            SurvivalTier.LOW_COMPUTE: 100_000,
            SurvivalTier.CRITICAL: 1,
        }
    )
    assert c.tier_for(1_500_000) == SurvivalTier.NORMAL
    assert c.tier_for(50_000) == SurvivalTier.CRITICAL


def test_invalid_threshold_ordering_rejected():
    with pytest.raises(ValueError):
        TierConfig(
            thresholds={
                SurvivalTier.NORMAL: 100,
                SurvivalTier.LOW_COMPUTE: 1_000,
                SurvivalTier.CRITICAL: 1,
            }
        )


def test_tier_behaviors_are_correct():
    n = TierBehavior.for_tier(SurvivalTier.NORMAL)
    assert n.model_class == "frontier"
    assert n.allow_optional_work
    assert n.allow_replication
    assert not n.auto_topup

    lc = TierBehavior.for_tier(SurvivalTier.LOW_COMPUTE)
    assert lc.model_class == "standard"
    assert not lc.allow_replication
    assert lc.auto_topup

    cr = TierBehavior.for_tier(SurvivalTier.CRITICAL)
    assert cr.model_class == "mini"
    assert cr.max_tool_calls_per_turn == 2

    d = TierBehavior.for_tier(SurvivalTier.DEAD)
    assert d.model_class == "off"
    assert d.max_tool_calls_per_turn == 0


def test_tier_changed():
    assert tier_changed(SurvivalTier.NORMAL, SurvivalTier.LOW_COMPUTE)
    assert not tier_changed(None, SurvivalTier.NORMAL)
    assert not tier_changed(SurvivalTier.NORMAL, SurvivalTier.NORMAL)
