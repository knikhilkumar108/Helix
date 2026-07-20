"""Unit tests for replication."""
from __future__ import annotations

import pytest

from core.errors.errors import InsufficientFundsError
from core.types.identifiers import AutomatonId, new_automaton_id
from core.types.money import Money
from services.control_plane.registry import AutomatonRegistry
from services.replication.replication import ReplicationPolicy, replicate


def test_replicate_creates_child_and_charges_parent():
    reg = AutomatonRegistry()
    parent = reg.create(
        name="p",
        genesis_prompt="x",
        initial_balance=Money.from_major("10.00"),
    )
    child = replicate(
        parent.id,
        name="c",
        genesis_prompt="y",
        registry=reg,
        policy=ReplicationPolicy(seed_funds_micro=1_000_000),
    )
    assert child.parent_id == parent.id
    # Parent was charged.
    assert reg.treasury(parent.id).balance() == Money.from_major("9.00")


def test_replicate_blocks_on_insufficient_funds():
    reg = AutomatonRegistry()
    parent = reg.create(
        name="p",
        genesis_prompt="x",
        initial_balance=Money.from_major("0.10"),
    )
    with pytest.raises(InsufficientFundsError):
        replicate(
            parent.id,
            name="c",
            genesis_prompt="y",
            registry=reg,
            policy=ReplicationPolicy(seed_funds_micro=1_000_000),
        )


def test_replicate_respects_max_children():
    reg = AutomatonRegistry()
    parent = reg.create(
        name="p",
        genesis_prompt="x",
        initial_balance=Money.from_major("10.00"),
    )
    for i in range(2):
        replicate(
            parent.id,
            name=f"c{i}",
            genesis_prompt="y",
            registry=reg,
            policy=ReplicationPolicy(max_children=2, seed_funds_micro=1_000_000),
        )
    with pytest.raises(Exception):
        replicate(
            parent.id,
            name="c2",
            genesis_prompt="y",
            registry=reg,
            policy=ReplicationPolicy(max_children=2, seed_funds_micro=1_000_000),
        )
