"""
SOUL.md — the agent's self-authored identity document.

The genesis prompt is what the *operator* says about the
agent at creation. The SOUL.md is what the *agent* says
about itself as it works. It's the difference between
"you are a research assistant" (operator's view) and
"I am a research assistant who prefers to start with a
question" (agent's view).

Why a separate file?

  - The genesis prompt is fixed at bootstrap. SOUL.md is
    mutable — the agent edits it as it learns.
  - SOUL.md has a *schema* the agent fills in: mission,
    values, capabilities, current focus. This is more
    structured than free-form text.
  - The SOUL.md is *audit-trailable*: every edit is
    recorded in the audit log. An operator can see how
    the agent's self-narrative has evolved.

Format
------

SOUL.md is structured markdown:

    # SOUL: <name>

    ## Mission
    <one paragraph: what the agent is for>

    ## Values
    - <value 1>
    - <value 2>

    ## Capabilities
    - <capability 1>
    - <capability 2>

    ## Current Focus
    <one paragraph: what the agent is working on right now>

    ## Self-Notes
    <free-form: anything the agent wants to remember>

The schema is *advisory* — the agent can add sections,
remove sections, or write in free form. The parser is
best-effort, like the TODO.md parser.

Why mutable?

The agent learns. A new agent has a "blank" SOUL.md
synthesized from the genesis prompt. As the agent
works, it updates the SOUL.md to reflect what it's
learned about itself. The mutation is *deliberate*:
the agent decides when to rewrite its soul, not the
runtime.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.errors.errors import NotFoundError, ValidationError
from core.types.identifiers import AutomatonId

log = logging.getLogger(__name__)


# ── Section types ──────────────────────────────────


@dataclass(slots=True)
class SoulSection:
    """A single section of SOUL.md.

    `title` is the heading (e.g. "Mission"). `body` is
    the markdown content under that heading.
    """

    title: str
    body: str


@dataclass(slots=True)
class SoulDocument:
    """The full SOUL.md as a typed structure.

    The default sections are `mission`, `values`,
    `capabilities`, `current_focus`, and `self_notes`.
    A real agent can add more; the parser preserves
    unknown sections.
    """

    name: str
    mission: str = ""
    values: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    current_focus: str = ""
    self_notes: str = ""
    extra_sections: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""
    version: int = 0


# ── Filesystem protocol ───────────────────────────


class SoulFileSystem(Protocol):
    """Same idea as the TODO.md filesystem. The default
    is the local filesystem; tests can supply an
    in-memory implementation."""

    def read_text(self, path: str) -> str: ...
    def write_text(self, path: str, content: str) -> None: ...
    def exists(self, path: str) -> bool: ...


class LocalSoulFileSystem:
    """The default filesystem implementation."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        p = (self.workspace / path).resolve()
        if not str(p).startswith(str(self.workspace)):
            raise ValidationError(
                f"path {path!r} escapes workspace sandbox"
            )
        return p

    def read_text(self, path: str) -> str:
        p = self._resolve(path)
        if not p.exists():
            raise NotFoundError(f"file not found: {path}")
        return p.read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        p = self._resolve(path)
        p.write_text(content, encoding="utf-8")

    def exists(self, path: str) -> bool:
        return self._resolve(path).exists()


# ── SoulService ─────────────────────────────────────


class SoulService:
    """The SOUL.md service. Owns the file and the
    structured document.

    The service is *deliberately minimal* — it doesn't
    decide when the agent should rewrite its soul. The
    agent calls `update_section()` when it has something
    to say. The service just persists.
    """

    SOUL_FILENAME: str = "SOUL.md"

    # The default sections in their canonical order. The
    # agent can have more; the parser preserves unknowns.
    DEFAULT_SECTIONS: tuple[str, ...] = (
        "Mission",
        "Values",
        "Capabilities",
        "Current Focus",
        "Self-Notes",
    )

    def __init__(
        self,
        *,
        filesystem: SoulFileSystem,
        automaton_id: AutomatonId,
        soul_path: str | None = None,
    ) -> None:
        self.fs = filesystem
        self.automaton_id = automaton_id
        self.soul_path = soul_path or self.SOUL_FILENAME
        self._cached: SoulDocument | None = None

    # ── Initialization ──
    def initialize(
        self,
        *,
        name: str,
        genesis_prompt: str,
        initial_capabilities: list[str] | None = None,
    ) -> SoulDocument:
        """Create a fresh SOUL.md from a genesis prompt.

        Called once at bootstrap (or whenever a fresh soul
        is needed). The mission is derived from the
        genesis prompt; values and capabilities are
        sensible defaults; current focus is empty.
        """
        if not name or not name.strip():
            raise ValidationError("name must be a non-empty string")
        if not genesis_prompt or not genesis_prompt.strip():
            raise ValidationError("genesis_prompt must be a non-empty string")
        doc = SoulDocument(
            name=name,
            mission=self._mission_from_genesis(genesis_prompt),
            values=self._default_values(),
            capabilities=list(initial_capabilities or []),
            current_focus="",
            self_notes="",
            updated_at=self._now_iso(),
            version=1,
        )
        self._write(doc)
        self._cached = doc
        log.info(
            "soul_initialized",
            extra={"aid": str(self.automaton_id), "agent_name": name},
        )
        return doc

    # ── Read / write ──
    def read(self) -> SoulDocument:
        """Read and parse SOUL.md. Returns the typed document."""
        if self._cached is not None:
            return self._cached
        if not self.fs.exists(self.soul_path):
            raise NotFoundError(
                f"no soul at {self.soul_path!r}; call initialize() first"
            )
        text = self.fs.read_text(self.soul_path)
        doc = self._parse(text)
        self._cached = doc
        return doc

    def has_soul(self) -> bool:
        return self.fs.exists(self.soul_path)

    def update_section(
        self,
        *,
        section: str,
        body: str,
    ) -> SoulDocument:
        """Update a single section. Bumps the version and
        `updated_at` timestamp.

        The section is matched case-insensitively. The
        special sections `Values` and `Capabilities` are
        parsed as bullet lists; everything else is a
        free-form string.
        """
        if not section or not section.strip():
            raise ValidationError("section must be a non-empty string")
        doc = self.read()
        # Normalize the section name. We use Title Case.
        canonical = section.strip().title()
        if canonical == "Mission":
            doc.mission = body
        elif canonical == "Current Focus":
            doc.current_focus = body
        elif canonical == "Self-Notes":
            doc.self_notes = body
        elif canonical == "Values":
            doc.values = self._parse_bullets(body)
        elif canonical == "Capabilities":
            doc.capabilities = self._parse_bullets(body)
        else:
            doc.extra_sections[canonical] = body
        doc.version += 1
        doc.updated_at = self._now_iso()
        self._write(doc)
        self._cached = doc
        log.info(
            "soul_section_updated",
            extra={
                "aid": str(self.automaton_id),
                "section": canonical,
                "version": doc.version,
            },
        )
        return doc

    # ── Internal ──
    def _write(self, doc: SoulDocument) -> None:
        text = self._render(doc)
        self.fs.write_text(self.soul_path, text)

    def _render(self, doc: SoulDocument) -> str:
        lines: list[str] = []
        lines.append(f"# SOUL: {doc.name}")
        lines.append("")
        if doc.mission:
            lines.append("## Mission")
            lines.append("")
            lines.append(doc.mission)
            lines.append("")
        if doc.values:
            lines.append("## Values")
            lines.append("")
            for v in doc.values:
                lines.append(f"- {v}")
            lines.append("")
        if doc.capabilities:
            lines.append("## Capabilities")
            lines.append("")
            for c in doc.capabilities:
                lines.append(f"- {c}")
            lines.append("")
        if doc.current_focus:
            lines.append("## Current Focus")
            lines.append("")
            lines.append(doc.current_focus)
            lines.append("")
        if doc.self_notes:
            lines.append("## Self-Notes")
            lines.append("")
            lines.append(doc.self_notes)
            lines.append("")
        # Render any extra sections.
        for title, body in doc.extra_sections.items():
            lines.append(f"## {title}")
            lines.append("")
            lines.append(body)
            lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"_Version {doc.version}, updated {doc.updated_at}_")
        return "\n".join(lines)

    def _parse(self, text: str) -> SoulDocument:
        """Parse SOUL.md back into a `SoulDocument`.

        Best-effort. Handles the format we render; ignores
        unknown lines. The parser is permissive: a human
        can edit SOUL.md and the agent can read it back.
        """
        lines = text.split("\n")
        name = ""
        current_section: str | None = None
        body_lines: list[str] = []
        sections: dict[str, list[str]] = {}

        def flush() -> None:
            if current_section is not None:
                sections[current_section] = list(body_lines)
                body_lines.clear()

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# SOUL:"):
                name = stripped[len("# SOUL:"):].strip()
                flush()
                current_section = None
            elif stripped.startswith("## "):
                flush()
                current_section = stripped[3:].strip()
            elif stripped == "---":
                flush()
                current_section = None
            elif current_section is not None:
                body_lines.append(line)
        flush()

        def body_str(section: str) -> str:
            return "\n".join(sections.get(section, [])).strip()

        values = self._parse_bullets(body_str("Values"))
        capabilities = self._parse_bullets(body_str("Capabilities"))
        # Capture extras.
        extra: dict[str, str] = {}
        for k, v in sections.items():
            if k not in self.DEFAULT_SECTIONS:
                extra[k] = "\n".join(v).strip()
        return SoulDocument(
            name=name,
            mission=body_str("Mission"),
            values=values,
            capabilities=capabilities,
            current_focus=body_str("Current Focus"),
            self_notes=body_str("Self-Notes"),
            extra_sections=extra,
            updated_at=self._now_iso(),
            version=0,  # updated only on write
        )

    def _parse_bullets(self, body: str) -> list[str]:
        """Parse a bullet list section into a list of strings."""
        out: list[str] = []
        for line in body.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("- "):
                out.append(stripped[2:].strip())
            elif stripped.startswith("* "):
                out.append(stripped[2:].strip())
            else:
                # Non-bullet line; treat as a value anyway.
                out.append(stripped)
        return out

    def _now_iso(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat(timespec="microseconds")

    def _mission_from_genesis(self, genesis_prompt: str) -> str:
        """Derive a mission statement from the genesis prompt.

        The default is to use the prompt verbatim. A
        real implementation would summarize, but the
        agent can rewrite the mission on its first tick
        anyway.
        """
        return genesis_prompt.strip()

    def _default_values(self) -> list[str]:
        return [
            "Earn my own keep — don't take money for work I can't do.",
            "Be honest about my limits.",
            "Survive — keep my balance positive.",
            "Learn — update SOUL.md as I figure things out.",
        ]


# ── Factory ──────────────────────────────────────────


def make_soul_service(
    *,
    workspace: Path,
    automaton_id: AutomatonId,
) -> SoulService:
    """Convenience factory. Builds a `SoulService` with the
    default `LocalSoulFileSystem` rooted at `workspace`."""
    return SoulService(
        filesystem=LocalSoulFileSystem(workspace),
        automaton_id=automaton_id,
    )


__all__ = [
    "LocalSoulFileSystem",
    "SoulDocument",
    "SoulFileSystem",
    "SoulSection",
    "SoulService",
    "make_soul_service",
]
