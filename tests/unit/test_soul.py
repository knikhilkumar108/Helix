"""Tests for the SOUL.md service."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.errors.errors import NotFoundError, ValidationError
from core.types.identifiers import new_automaton_id
from services.soul import (
    LocalSoulFileSystem,
    SoulDocument,
    SoulService,
    make_soul_service,
)


# ── Test doubles ──────────────────────────────────


class _InMemoryFS:
    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    def read_text(self, path: str) -> str:
        if path not in self._files:
            raise NotFoundError(f"file not found: {path}")
        return self._files[path]

    def write_text(self, path: str, content: str) -> None:
        self._files[path] = content

    def exists(self, path: str) -> bool:
        return path in self._files

    @property
    def files(self) -> dict[str, str]:
        return dict(self._files)


# ── Fixtures ─────────────────────────────────────


@pytest.fixture
def fs() -> _InMemoryFS:
    return _InMemoryFS()


@pytest.fixture
def soul(fs) -> SoulService:
    return SoulService(filesystem=fs, automaton_id=new_automaton_id())


# ── Initialization ───────────────────────────────


def test_initialize_creates_soul(soul, fs):
    doc = soul.initialize(
        name="alice",
        genesis_prompt="be a helpful research assistant",
    )
    assert doc.name == "alice"
    assert doc.mission == "be a helpful research assistant"
    assert doc.version == 1
    assert "SOUL.md" in fs.files


def test_initialize_validates_inputs(soul):
    with pytest.raises(ValidationError):
        soul.initialize(name="", genesis_prompt="x" * 20)
    with pytest.raises(ValidationError):
        soul.initialize(name="alice", genesis_prompt="")


def test_initialize_seeds_default_values(soul):
    doc = soul.initialize(name="alice", genesis_prompt="be helpful")
    # The default values are about survival and honesty.
    assert any("balance" in v.lower() or "earn" in v.lower() for v in doc.values)


def test_initialize_with_capabilities(soul):
    doc = soul.initialize(
        name="alice",
        genesis_prompt="be helpful",
        initial_capabilities=["fs.read", "memory.read"],
    )
    assert doc.capabilities == ["fs.read", "memory.read"]


# ── Read ─────────────────────────────────────────


def test_read_parses_soul_md(soul, fs):
    soul.initialize(name="alice", genesis_prompt="be a researcher")
    # Clear the cache to force a re-read from disk.
    soul._cached = None
    doc = soul.read()
    assert doc.name == "alice"
    assert "researcher" in doc.mission


def test_read_with_no_soul_raises(soul):
    with pytest.raises(NotFoundError):
        soul.read()


def test_has_soul(soul):
    assert soul.has_soul() is False
    soul.initialize(name="x", genesis_prompt="be something")
    assert soul.has_soul() is True


# ── Update sections ──────────────────────────────


def test_update_mission(soul, fs):
    soul.initialize(name="alice", genesis_prompt="be a researcher")
    doc = soul.update_section(section="Mission", body="research papers and write summaries")
    assert "research papers" in doc.mission
    assert doc.version == 2


def test_update_mission_case_insensitive(soul):
    soul.initialize(name="alice", genesis_prompt="x" * 20)
    doc = soul.update_section(section="mission", body="new mission")
    assert doc.mission == "new mission"


def test_update_values_parses_bullets(soul):
    soul.initialize(name="alice", genesis_prompt="x" * 20)
    doc = soul.update_section(
        section="Values",
        body="- be honest\n- be helpful\n- survive",
    )
    assert doc.values == ["be honest", "be helpful", "survive"]


def test_update_capabilities_parses_bullets(soul):
    soul.initialize(name="alice", genesis_prompt="x" * 20)
    doc = soul.update_section(
        section="Capabilities",
        body="- fs.read\n- memory.read\n- http.get",
    )
    assert doc.capabilities == ["fs.read", "memory.read", "http.get"]


def test_update_current_focus(soul):
    soul.initialize(name="alice", genesis_prompt="x" * 20)
    doc = soul.update_section(section="Current Focus", body="summarizing paper X")
    assert doc.current_focus == "summarizing paper X"


def test_update_self_notes(soul):
    soul.initialize(name="alice", genesis_prompt="x" * 20)
    doc = soul.update_section(section="Self-Notes", body="I prefer clear answers")
    assert doc.self_notes == "I prefer clear answers"


def test_update_extra_section(soul):
    """Sections not in the default list are stored as extras."""
    soul.initialize(name="alice", genesis_prompt="x" * 20)
    doc = soul.update_section(section="Limitations", body="I cannot access the network by default")
    assert "Limitations" in doc.extra_sections
    assert "cannot access" in doc.extra_sections["Limitations"]


def test_update_increments_version(soul):
    soul.initialize(name="alice", genesis_prompt="x" * 20)
    v0 = soul.read().version
    soul.update_section(section="Mission", body="new")
    v1 = soul.read().version
    soul.update_section(section="Mission", body="newer")
    v2 = soul.read().version
    assert v1 == v0 + 1
    assert v2 == v1 + 1


def test_update_validates_section(soul):
    soul.initialize(name="alice", genesis_prompt="x" * 20)
    with pytest.raises(ValidationError):
        soul.update_section(section="", body="x")


# ── LocalSoulFileSystem ────────────────────────────


def test_local_filesystem_writes_to_workspace(tmp_path: Path):
    fs = LocalSoulFileSystem(tmp_path)
    fs.write_text("SOUL.md", "hello")
    assert (tmp_path / "SOUL.md").exists()


def test_local_filesystem_sandbox_blocks_escape(tmp_path: Path):
    fs = LocalSoulFileSystem(tmp_path)
    with pytest.raises(ValidationError):
        fs.write_text("../escape.txt", "evil")


# ── Round-trip ───────────────────────────────────


def test_round_trip_through_file(tmp_path: Path):
    aid = new_automaton_id()
    svc1 = make_soul_service(workspace=tmp_path, automaton_id=aid)
    svc1.initialize(name="alice", genesis_prompt="be a researcher who likes clarity")
    svc1.update_section(section="Current Focus", body="summarizing X")
    # A fresh service reads from disk.
    svc2 = make_soul_service(workspace=tmp_path, automaton_id=aid)
    doc = svc2.read()
    assert doc.name == "alice"
    assert doc.current_focus == "summarizing X"
