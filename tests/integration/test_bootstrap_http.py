"""Integration tests for the bootstrap service wired into the control plane."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.bootstrap import BootstrapService
from services.control_plane.api import create_app
from services.control_plane.registry import AutomatonRegistry


class _FakeSkills:
    def __init__(self) -> None:
        self._enabled: dict[str, set[str]] = {}

    def enable(self, aid, skill_name: str) -> None:
        self._enabled.setdefault(str(aid), set()).add(skill_name)

    def disable(self, aid, skill_name: str) -> None:
        self._enabled.get(str(aid), set()).discard(skill_name)

    def list_enabled(self, aid) -> list[str]:
        return sorted(self._enabled.get(str(aid), set()))


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
        self.entries.append({"id": mem_id, "aid": str(aid), "content": content})
        return mem_id


@pytest.fixture
def client_with_bootstrap():
    app = create_app()
    reg = app.state.registry
    skills = _FakeSkills()
    memory = _FakeMemory()
    app.state.bootstrap = BootstrapService(reg, skills=skills, memory=memory)
    with TestClient(app) as c:
        yield c, skills, memory


@pytest.fixture
def client_no_bootstrap():
    app = create_app()
    with TestClient(app) as c:
        yield c


# ── With bootstrap configured ────────────────────────────


def test_create_uses_bootstrap_when_configured(client_with_bootstrap):
    client, skills, memory = client_with_bootstrap
    r = client.post(
        "/v1/automata",
        json={"name": "alice", "genesis_prompt": "be a helpful assistant"},
    )
    assert r.status_code == 201
    body = r.json()
    aid = body["id"]
    # The bootstrap seeded skills and memory.
    enabled = skills.list_enabled(aid)
    assert "fs.read" in enabled
    assert len(memory.entries) == 1


def test_create_validates_short_genesis(client_with_bootstrap):
    client, _, _ = client_with_bootstrap
    r = client.post(
        "/v1/automata",
        json={"name": "alice", "genesis_prompt": "x"},  # too short
    )
    assert r.status_code == 400


def test_create_works_with_long_name(client_with_bootstrap):
    client, _, _ = client_with_bootstrap
    r = client.post(
        "/v1/automata",
        json={"name": "x" * 100, "genesis_prompt": "be helpful"},
    )
    # The pydantic schema has a 128-char cap, so the route's
    # pydantic layer accepts it. The bootstrap's 64-char cap
    # is reached; we expect a 400 from the bootstrap.
    assert r.status_code in (400, 422)


# ── Without bootstrap configured ─────────────────────────


def test_create_works_without_bootstrap(client_no_bootstrap):
    client = client_no_bootstrap
    r = client.post(
        "/v1/automata",
        json={"name": "alice", "genesis_prompt": "be helpful"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "alice"
