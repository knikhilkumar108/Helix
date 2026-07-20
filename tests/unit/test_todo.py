"""Tests for the plan-mode TODO.md service."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.errors.errors import NotFoundError, ValidationError
from core.types.identifiers import new_automaton_id
from core.types.money import Money
from services.planning import (
    LocalTodoFileSystem,
    TodoPlan,
    TodoService,
    TodoStatus,
    TodoStep,
    make_todo_service,
)


# ── Test doubles ──────────────────────────────────────────


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


# ── Fixtures ─────────────────────────────────────────────


@pytest.fixture
def fs() -> _InMemoryFS:
    return _InMemoryFS()


@pytest.fixture
def todo(fs) -> TodoService:
    return TodoService(filesystem=fs, automaton_id=new_automaton_id())


# ── Plan creation ───────────────────────────────────────


def test_create_plan_writes_todo_md(todo, fs):
    plan = todo.create_plan(
        goal="summarize the report",
        steps=[
            {"description": "read the report"},
            {"description": "extract key points"},
            {"description": "write summary"},
        ],
        estimated_cost=Money.from_major("0.10"),
        estimated_revenue=Money.from_major("0.50"),
        probability=0.7,
    )
    assert plan.goal == "summarize the report"
    assert len(plan.steps) == 3
    # The file is on disk.
    assert "TODO.md" in fs.files
    content = fs.files["TODO.md"]
    assert "# TODO: summarize the report" in content
    assert "- [ ] 0. read the report" in content
    assert "- [ ] 2. write summary" in content
    assert "Estimated cost: 0.100000 USDC" in content
    assert "Probability: 0.7" in content


def test_create_plan_validates_goal(todo):
    with pytest.raises(ValidationError):
        todo.create_plan(
            goal="",
            steps=[{"description": "x"}],
            estimated_cost=Money.zero(),
            estimated_revenue=Money.zero(),
        )


def test_create_plan_validates_steps(todo):
    with pytest.raises(ValidationError):
        todo.create_plan(
            goal="do something",
            steps=[],
            estimated_cost=Money.zero(),
            estimated_revenue=Money.zero(),
        )


def test_create_plan_validates_step_description(todo):
    with pytest.raises(ValidationError):
        todo.create_plan(
            goal="x",
            steps=[{"description": ""}],
            estimated_cost=Money.zero(),
            estimated_revenue=Money.zero(),
        )


def test_create_plan_validates_probability(todo):
    with pytest.raises(ValidationError):
        todo.create_plan(
            goal="x",
            steps=[{"description": "y"}],
            estimated_cost=Money.zero(),
            estimated_revenue=Money.zero(),
            probability=1.5,
        )
    with pytest.raises(ValidationError):
        todo.create_plan(
            goal="x",
            steps=[{"description": "y"}],
            estimated_cost=Money.zero(),
            estimated_revenue=Money.zero(),
            probability=-0.1,
        )


def test_create_plan_includes_critique(todo, fs):
    todo.create_plan(
        goal="x",
        steps=[{"description": "y"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
        critique="This plan is good because of reasons.",
    )
    content = fs.files["TODO.md"]
    assert "## Critique" in content
    assert "This plan is good" in content


# ── Step transitions ───────────────────────────────────


def test_mark_step_in_progress(todo, fs):
    todo.create_plan(
        goal="x",
        steps=[{"description": "a"}, {"description": "b"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    todo.mark_step_in_progress(0)
    content = fs.files["TODO.md"]
    assert "- [~] 0. a" in content
    assert "- [ ] 1. b" in content


def test_mark_step_succeeded(todo, fs):
    todo.create_plan(
        goal="x",
        steps=[{"description": "a"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    todo.mark_step_in_progress(0)
    todo.mark_step_succeeded(0)
    content = fs.files["TODO.md"]
    assert "- [x] 0. a" in content


def test_mark_step_failed(todo, fs):
    todo.create_plan(
        goal="x",
        steps=[{"description": "a"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    todo.mark_step_in_progress(0)
    todo.mark_step_failed(0)
    content = fs.files["TODO.md"]
    assert "- [!] 0. a" in content


def test_mark_step_invalid_index(todo):
    todo.create_plan(
        goal="x",
        steps=[{"description": "a"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    with pytest.raises(ValidationError):
        todo.mark_step_in_progress(5)


def test_mark_step_invalid_transition(todo):
    todo.create_plan(
        goal="x",
        steps=[{"description": "a"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    todo.mark_step_in_progress(0)
    todo.mark_step_succeeded(0)
    # SUCCEEDED is terminal.
    with pytest.raises(ValidationError):
        todo.mark_step_in_progress(0)


def test_mark_step_failed_can_be_retried(todo):
    todo.create_plan(
        goal="x",
        steps=[{"description": "a"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    todo.mark_step_in_progress(0)
    todo.mark_step_failed(0)
    # FAILED can go back to PENDING.
    todo.mark_step(0, TodoStatus.PENDING)
    plan = todo.read_plan()
    assert plan.steps[0].status == TodoStatus.PENDING


# ── Read / parse ────────────────────────────────────────


def test_read_plan_parses_todo_md(todo, fs):
    todo.create_plan(
        goal="do a thing",
        steps=[
            {"description": "step one", "estimated_cost_micro": 100_000},
            {"description": "step two", "estimated_cost_micro": 200_000},
        ],
        estimated_cost=Money.from_major("0.30"),
        estimated_revenue=Money.from_major("1.00"),
        probability=0.8,
    )
    # Force a re-read by clearing the cache.
    todo._cached = None
    plan = todo.read_plan()
    assert plan.goal == "do a thing"
    assert len(plan.steps) == 2
    assert plan.steps[0].description == "step one"
    assert plan.estimated_cost.micro == 300_000
    assert plan.estimated_revenue.micro == 1_000_000
    assert plan.probability == 0.8


def test_read_plan_with_no_file_raises(todo):
    with pytest.raises(NotFoundError):
        todo.read_plan()


def test_has_plan(todo):
    assert todo.has_plan() is False
    todo.create_plan(
        goal="x",
        steps=[{"description": "y"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    assert todo.has_plan() is True


# ── Progress / completion ──────────────────────────────


def test_progress_counts(todo):
    todo.create_plan(
        goal="x",
        steps=[
            {"description": "a"},
            {"description": "b"},
            {"description": "c"},
        ],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    todo.mark_step_in_progress(0)
    todo.mark_step_succeeded(0)
    todo.mark_step_in_progress(1)
    p = todo.progress()
    assert p["total"] == 3
    assert p["succeeded"] == 1
    assert p["in_progress"] == 1
    assert p["pending"] == 1
    assert p["failed"] == 0


def test_is_complete_true_when_all_succeeded(todo):
    todo.create_plan(
        goal="x",
        steps=[{"description": "a"}, {"description": "b"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    todo.mark_step_in_progress(0)
    todo.mark_step_succeeded(0)
    todo.mark_step_in_progress(1)
    todo.mark_step_succeeded(1)
    assert todo.is_complete() is True


def test_is_complete_false_with_failures(todo):
    todo.create_plan(
        goal="x",
        steps=[{"description": "a"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    todo.mark_step_in_progress(0)
    todo.mark_step_failed(0)
    assert todo.is_complete() is False


def test_is_complete_false_when_no_plan(todo):
    assert todo.is_complete() is False


# ── LocalTodoFileSystem ──────────────────────────────────


def test_local_filesystem_writes_to_workspace(tmp_path: Path):
    fs = LocalTodoFileSystem(tmp_path)
    fs.write_text("TODO.md", "hello")
    assert (tmp_path / "TODO.md").exists()
    assert fs.read_text("TODO.md") == "hello"


def test_local_filesystem_sandbox_blocks_escape(tmp_path: Path):
    fs = LocalTodoFileSystem(tmp_path)
    with pytest.raises(ValidationError):
        fs.write_text("../escape.txt", "evil")
    with pytest.raises(ValidationError):
        fs.read_text("../escape.txt")


def test_local_filesystem_creates_workspace(tmp_path: Path):
    new_ws = tmp_path / "new_workspace"
    fs = LocalTodoFileSystem(new_ws)
    # The workspace is auto-created.
    assert new_ws.exists()
    fs.write_text("TODO.md", "x")
    assert (new_ws / "TODO.md").exists()


# ── make_todo_service factory ──────────────────────────


def test_make_todo_service_uses_local_fs(tmp_path: Path):
    aid = new_automaton_id()
    svc = make_todo_service(workspace=tmp_path, automaton_id=aid)
    plan = svc.create_plan(
        goal="x",
        steps=[{"description": "y"}],
        estimated_cost=Money.zero(),
        estimated_revenue=Money.zero(),
    )
    assert plan.automaton_id == aid
    assert (tmp_path / "TODO.md").exists()


# ── Round-trip ─────────────────────────────────────────


def test_round_trip_through_file(tmp_path: Path):
    """Create a plan, write it, read it back from a fresh service."""
    aid = new_automaton_id()
    svc1 = make_todo_service(workspace=tmp_path, automaton_id=aid)
    svc1.create_plan(
        goal="round trip",
        steps=[
            {"description": "first", "estimated_cost_micro": 50_000},
            {"description": "second", "estimated_cost_micro": 100_000},
        ],
        estimated_cost=Money.from_major("0.15"),
        estimated_revenue=Money.from_major("0.50"),
        probability=0.5,
    )
    svc1.mark_step_in_progress(0)
    svc1.mark_step_succeeded(0)
    # A fresh service reads the file from disk.
    svc2 = make_todo_service(workspace=tmp_path, automaton_id=aid)
    plan = svc2.read_plan()
    assert plan.goal == "round trip"
    assert len(plan.steps) == 2
    assert plan.steps[0].status == TodoStatus.SUCCEEDED
    assert plan.steps[1].status == TodoStatus.PENDING
