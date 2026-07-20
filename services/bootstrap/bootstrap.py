"""
Self-bootstrap: the agent's first-run experience.

When you `POST /v1/automata` with just a name and a genesis
prompt, the platform runs a *bootstrap* that:

  1. Validates the inputs (a name, a prompt, optionally a
     parent and a starting balance).
  2. Calls `AutomatonRegistry.create()` to materialize the
     agent — wallet, keypair, treasury, in-memory state.
  3. Seeds initial memory (the "how to use me" note, a
     pointer to the genesis prompt).
  4. Optionally enables an initial set of skills.
  5. Records a `bootstrap_completed` event in the agent's
     audit log so a future operator can see when the agent
     was created and what it was seeded with.

The bootstrap is the *only* place where the platform
imposes default skills, default memory, or default
behavior. Once the agent is bootstrapped, the agent
itself decides what to do — it can read its own bootstrap
record, modify its skills, and rewrite its own memory.

Why a separate module?

  - The registry's `create()` is a thin constructor; the
    bootstrap is a higher-level *policy* that wraps it.
  - Tests can exercise the bootstrap without spinning up
    a full control plane.
  - The bootstrap is the natural place to add platform-
    wide defaults later (e.g. "all agents get the
    `constitution.md` skill") without touching the
    registry.

What the bootstrap does NOT do:

  - It does NOT choose the LLM. The runtime's
    `build_llm_loop()` does that from env vars.
  - It does NOT fund the wallet. The operator does that
    via `POST /v1/treasury/{aid}/fund`.
  - It does NOT start the loop. The runtime's
    `build_default_loop()` and `build_llm_loop()` are
    separate from the bootstrap.
  - It does NOT talk to the agent. The agent only exists
    after the bootstrap completes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.errors.errors import ValidationError
from core.types.identifiers import AutomatonId
from core.types.money import Money
from services.control_plane.registry import AutomatonRegistry

log = logging.getLogger(__name__)


# ── Default seed content ──────────────────────────────────


# A short note that goes into the agent's memory on bootstrap.
# The agent reads this on its first tick. We seed it because
# an agent with *no* memory is a blank slate that doesn't
# know who it is or what its job is. The note is intentionally
# short — the agent will overwrite it as it learns.
DEFAULT_INTRO_MEMORY = (
    "I am a Helix agent. My genesis prompt describes what I "
    "should do. My wallet holds USDC that I earn by working "
    "and spend on LLM calls. If my balance hits zero, I die. "
    "I should look at my inbox, decide if there's work, and "
    "act on the most important thing first."
)


# The default set of skills enabled at bootstrap. A real
# platform would consult a policy here; for now we enable
# a small, well-tested set.
DEFAULT_SKILLS: tuple[str, ...] = (
    "fs.read",
    "fs.write",
    "memory.read",
    "memory.write",
    "time.now",
    "messaging.send",
    "messaging.claim",
)


# The minimum length of a valid genesis prompt. Below this,
# the bootstrap refuses to create the agent. A one-word
# prompt ("be") is too vague to be useful.
MIN_GENESIS_PROMPT_LEN: int = 8


# The maximum length of a valid name. Names longer than this
# are probably typos or accidental copy-pastes.
MAX_NAME_LEN: int = 64


# ── Types ────────────────────────────────────────────────


@dataclass(slots=True)
class BootstrapRequest:
    """The inputs to a bootstrap.

    Same shape as `AutomatonRegistry.create()` plus a few
    optional fields for the seed step:

      - `skills`: the names of skills to enable at
        bootstrap. Defaults to `DEFAULT_SKILLS`.
      - `intro_memory`: the initial memory entry. Defaults
        to `DEFAULT_INTRO_MEMORY`. Pass `""` to skip.
      - `metadata`: extra key-value pairs to attach to
        the agent (e.g. `{"region": "us-east"}`).
    """

    name: str
    genesis_prompt: str
    parent_id: AutomatonId | None = None
    initial_balance: Money | None = None
    skills: tuple[str, ...] | None = None
    intro_memory: str | None = None
    skip_seed: bool = False
    metadata: dict[str, str] | None = None


@dataclass(slots=True)
class BootstrapResult:
    """The output of a bootstrap.

    `automaton_id` is the new agent's id. `seeded_skills`
    is the list of skills that were enabled (a tuple of
    names). `seeded_memory_id` is the id of the memory
    entry that was written, or None if seeding was
    skipped. `created_at` is the wall-clock time of the
    bootstrap (in epoch seconds, for easy sorting).
    """

    automaton_id: AutomatonId
    name: str
    wallet_address: str
    initial_balance: Money
    seeded_skills: tuple[str, ...]
    seeded_memory_id: str | None
    created_at: float


# ── Skill registry (protocol) ────────────────────────────


class SkillRegistry(Protocol):
    """The interface for managing enabled skills.

    The platform's `services.control_plane.api` has a
    `skills` router; the bootstrap doesn't depend on it
    directly, it depends on this Protocol. A real
    implementation reads/writes the `skills` table in
    `SqliteStore`. A test implementation can be a
    plain dict.
    """

    def enable(self, aid: AutomatonId, skill_name: str) -> None: ...
    def disable(self, aid: AutomatonId, skill_name: str) -> None: ...
    def list_enabled(self, aid: AutomatonId) -> list[str]: ...


# ── Memory service (protocol) ────────────────────────────


class MemoryWriter(Protocol):
    """The interface for writing an initial memory entry.

    The platform's `services.memory.memory_service` has a
    `write` method; the bootstrap doesn't depend on it
    directly. The Protocol is small so tests can supply a
    simple in-memory implementation.
    """

    def write(
        self,
        *,
        aid: AutomatonId,
        content: str,
        layer: str = "long_term",
        importance: float = 0.5,
        tags: list[str] | None = None,
    ) -> str: ...


# ── Bootstrap service ────────────────────────────────────


class BootstrapService:
    """The first-run service. Wraps `AutomatonRegistry.create()`
    with validation, default seeding, and event recording.

    Usage:

        bootstrap = BootstrapService(registry, skills, memory)
        result = bootstrap.run(BootstrapRequest(
            name="alice",
            genesis_prompt="be a helpful research assistant",
        ))
        # result.automaton_id is now a working agent.

    The service is stateless. Multiple boots can be run
    in parallel against the same registry; the registry
    handles its own locking.
    """

    def __init__(
        self,
        registry: AutomatonRegistry,
        *,
        skills: SkillRegistry | None = None,
        memory: MemoryWriter | None = None,
        clock: callable = __import__("time").time,
    ) -> None:
        self.registry = registry
        self.skills = skills
        self.memory = memory
        self._clock = clock

    def run(self, req: BootstrapRequest) -> BootstrapResult:
        """Validate the request, create the agent, seed the
        default state. Returns a `BootstrapResult`.

        Raises `ValidationError` for invalid inputs.
        """
        self._validate(req)
        # 1. Create the agent.
        automaton = self.registry.create(
            name=req.name,
            genesis_prompt=req.genesis_prompt,
            parent_id=req.parent_id,
            initial_balance=req.initial_balance,
            metadata=req.metadata,
        )
        aid = automaton.id
        # 2. Seed skills (unless skipped).
        seeded_skills: tuple[str, ...] = ()
        if not req.skip_seed and self.skills is not None:
            skills_to_enable = req.skills or DEFAULT_SKILLS
            for skill in skills_to_enable:
                self.skills.enable(aid, skill)
            seeded_skills = tuple(skills_to_enable)
        # 3. Seed initial memory (unless skipped or empty).
        seeded_memory_id: str | None = None
        if not req.skip_seed and self.memory is not None:
            intro = req.intro_memory
            if intro is None:
                intro = DEFAULT_INTRO_MEMORY
            if intro:
                seeded_memory_id = self.memory.write(
                    aid=aid,
                    content=intro,
                    layer="long_term",
                    importance=0.7,
                    tags=["bootstrap", "intro"],
                )
        # 4. Record the bootstrap event in the agent's history.
        self.registry.record_event(
            aid,
            "bootstrap_completed",
            {
                "skills": list(seeded_skills),
                "memory_id": seeded_memory_id,
                "initial_balance_micro": automaton.balance.micro,
            },
        )
        log.info(
            "bootstrap_completed",
            extra={
                "aid": str(aid),
                "skills": len(seeded_skills),
                "memory_seeded": seeded_memory_id is not None,
            },
        )
        return BootstrapResult(
            automaton_id=aid,
            name=automaton.name,
            wallet_address=automaton.wallet_address,
            initial_balance=automaton.balance,
            seeded_skills=seeded_skills,
            seeded_memory_id=seeded_memory_id,
            created_at=self._now(),
        )

    # ── Validation ──
    def _validate(self, req: BootstrapRequest) -> None:
        if not req.name or not req.name.strip():
            raise ValidationError("name must be a non-empty string")
        if len(req.name) > MAX_NAME_LEN:
            raise ValidationError(
                f"name must be at most {MAX_NAME_LEN} characters, "
                f"got {len(req.name)}"
            )
        if not req.genesis_prompt or not req.genesis_prompt.strip():
            raise ValidationError("genesis_prompt must be a non-empty string")
        if len(req.genesis_prompt) < MIN_GENESIS_PROMPT_LEN:
            raise ValidationError(
                f"genesis_prompt must be at least "
                f"{MIN_GENESIS_PROMPT_LEN} characters (got "
                f"{len(req.genesis_prompt)})"
            )
        if req.initial_balance is not None and req.initial_balance.micro < 0:
            raise ValidationError("initial_balance must be non-negative")

    def _now(self) -> float:
        c = self._clock
        return c() if callable(c) else float(c)


# ── Factory ──────────────────────────────────────────────


def make_bootstrap(
    registry: AutomatonRegistry,
    *,
    skills: SkillRegistry | None = None,
    memory: MemoryWriter | None = None,
) -> BootstrapService:
    """Convenience factory. The platform's control plane
    wires the real `SkillRegistry` and `MemoryWriter`;
    tests can leave them `None` to disable seeding."""
    return BootstrapService(registry, skills=skills, memory=memory)


__all__ = [
    "BootstrapRequest",
    "BootstrapResult",
    "BootstrapService",
    "DEFAULT_INTRO_MEMORY",
    "DEFAULT_SKILLS",
    "MemoryWriter",
    "SkillRegistry",
    "make_bootstrap",
]
