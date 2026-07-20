"""Tests for the bootstrap service."""
from __future__ import annotations

import time

import pytest

from core.errors.errors import ValidationError
from core.types.identifiers import new_automaton_id
from core.types.money import Money
from services.bootstrap import (
    DEFAULT_INTRO_MEMORY,
    DEFAULT_SKILLS,
    BootstrapRequest,
    BootstrapService,
    make_bootstrap,
)
from services.control_plane.registry import AutomatonRegistry


# ── Test doubles ──────────────────────────────────────────


class _FakeSkills:
    def __init__(self) -> None:
        self._enabled: dict[str, set[str]] = {}

    def enable(self, aid, skill_name: str) -> None:
        self._enabled.setdefault(str(aid), set()).add(skill_name)

    def disable(self, aid, skill_name: str) -> None:
        self._enabled.get(str(aid), set()).discard(skill_name)

    def list_enabled(self, aid) -> list[str]:
        return sorted(self._enabled.get(str(aid), set()))

    @property
    def enabled(self) -> dict[str, set[str]]:
        return self._enabled


class _FakeMemory:
    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._counter = 0

    def write(
        self,
        *,
        aid,
        content: str,
        layer: str = "long_term",
        importance: float = 0.5,
        tags: list[str] | None = None,
    ) -> str:
        self._counter += 1
        mem_id = f"mem_{self._counter:04d}"
        self.entries.append(
            {
                "id": mem_id,
                "aid": str(aid),
                "content": content,
                "layer": layer,
                "importance": importance,
                "tags": list(tags or []),
            }
        )
        return mem_id


# ── Fixtures ─────────────────────────────────────────────


@pytest.fixture
def registry() -> AutomatonRegistry:
    return AutomatonRegistry()


@pytest.fixture
def skills() -> _FakeSkills:
    return _FakeSkills()


@pytest.fixture
def memory() -> _FakeMemory:
    return _FakeMemory()


@pytest.fixture
def bootstrap(registry, skills, memory) -> BootstrapService:
    return BootstrapService(registry, skills=skills, memory=memory)


# ── Validation ──────────────────────────────────────────


def test_validate_rejects_empty_name(bootstrap):
    with pytest.raises(ValidationError):
        bootstrap.run(BootstrapRequest(name="", genesis_prompt="be helpful"))


def test_validate_rejects_whitespace_name(bootstrap):
    with pytest.raises(ValidationError):
        bootstrap.run(BootstrapRequest(name="   ", genesis_prompt="be helpful"))


def test_validate_rejects_long_name(bootstrap):
    with pytest.raises(ValidationError):
        bootstrap.run(
            BootstrapRequest(name="x" * 100, genesis_prompt="be helpful")
        )


def test_validate_rejects_empty_genesis(bootstrap):
    with pytest.raises(ValidationError):
        bootstrap.run(BootstrapRequest(name="alice", genesis_prompt=""))


def test_validate_rejects_short_genesis(bootstrap):
    with pytest.raises(ValidationError):
        bootstrap.run(BootstrapRequest(name="alice", genesis_prompt="be"))


def test_validate_rejects_negative_balance(bootstrap):
    with pytest.raises(ValidationError):
        bootstrap.run(
            BootstrapRequest(
                name="alice",
                genesis_prompt="be helpful",
                initial_balance=Money(-100, "USDC"),
            )
        )


# ── Happy path ──────────────────────────────────────────


def test_basic_bootstrap_creates_agent(bootstrap):
    result = bootstrap.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
        )
    )
    assert result.automaton_id
    assert result.name == "alice"
    assert result.wallet_address
    assert result.initial_balance.micro == 0


def test_bootstrap_with_initial_balance(bootstrap):
    result = bootstrap.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
            initial_balance=Money.from_major("5.00"),
        )
    )
    assert result.initial_balance.micro == 5_000_000


def test_bootstrap_seeds_default_skills(bootstrap, skills):
    result = bootstrap.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
        )
    )
    enabled = skills.list_enabled(result.automaton_id)
    assert set(enabled) == set(DEFAULT_SKILLS)


def test_bootstrap_seeds_custom_skills(bootstrap, skills):
    result = bootstrap.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
            skills=("fs.read", "memory.read"),
        )
    )
    enabled = skills.list_enabled(result.automaton_id)
    assert set(enabled) == {"fs.read", "memory.read"}


def test_bootstrap_seeds_default_memory(bootstrap, memory):
    result = bootstrap.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
        )
    )
    assert result.seeded_memory_id is not None
    assert len(memory.entries) == 1
    entry = memory.entries[0]
    assert entry["content"] == DEFAULT_INTRO_MEMORY
    assert entry["tags"] == ["bootstrap", "intro"]


def test_bootstrap_seeds_custom_memory(bootstrap, memory):
    result = bootstrap.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
            intro_memory="I am a custom agent",
        )
    )
    assert result.seeded_memory_id is not None
    assert memory.entries[0]["content"] == "I am a custom agent"


def test_bootstrap_skips_seed_when_requested(bootstrap, skills, memory):
    result = bootstrap.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
            skip_seed=True,
        )
    )
    assert result.seeded_skills == ()
    assert result.seeded_memory_id is None
    assert skills.list_enabled(result.automaton_id) == []
    assert memory.entries == []


def test_bootstrap_records_event(bootstrap, registry):
    result = bootstrap.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
        )
    )
    events = registry.events(result.automaton_id)
    assert any(e["kind"] == "bootstrap_completed" for e in events)


# ── Optional services missing ───────────────────────────


def test_bootstrap_works_without_skills_registry(registry, memory):
    svc = BootstrapService(registry, skills=None, memory=memory)
    result = svc.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
        )
    )
    # No skills seeded (none configured).
    assert result.seeded_skills == ()


def test_bootstrap_works_without_memory_writer(registry, skills):
    svc = BootstrapService(registry, skills=skills, memory=None)
    result = svc.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
        )
    )
    # No memory seeded.
    assert result.seeded_memory_id is None
    # But skills were still enabled.
    assert len(skills.list_enabled(result.automaton_id)) > 0


def test_bootstrap_works_with_neither(registry):
    svc = BootstrapService(registry)
    result = svc.run(
        BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
        )
    )
    assert result.seeded_skills == ()
    assert result.seeded_memory_id is None


# ── Make factory ───────────────────────────────────────


def test_make_bootstrap_factory(registry):
    svc = make_bootstrap(registry)
    assert isinstance(svc, BootstrapService)
