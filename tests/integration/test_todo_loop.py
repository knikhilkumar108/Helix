"""Integration tests for plan mode wired into the runtime loop."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.types.identifiers import new_automaton_id
from runtime.loop.loop_init import build_default_loop
from services.planning import TodoService, make_todo_service


@pytest.fixture
def todo(tmp_path: Path) -> TodoService:
    return make_todo_service(workspace=tmp_path, automaton_id=new_automaton_id())


def test_plan_create_writes_todo_md(tmp_path: Path, todo: TodoService):
    aid = new_automaton_id()
    loop = build_default_loop(aid, workspace=tmp_path, todo=todo)
    fn = loop.tools._tools["plan.create"].fn  # type: ignore[attr-defined]
    result = fn(
        goal="summarize the report",
        steps=[{"description": "read it"}, {"description": "write summary"}],
        cost_micro=100_000,
        revenue_micro=500_000,
    )
    assert result["goal"] == "summarize the report"
    assert result["steps"] == 2
    assert (tmp_path / "TODO.md").exists()


def test_plan_mark_step_transitions(todo: TodoService):
    aid = new_automaton_id()
    loop = build_default_loop(aid, todo=todo)
    create = loop.tools._tools["plan.create"].fn  # type: ignore[attr-defined]
    create(
        goal="x",
        steps=[{"description": "a"}, {"description": "b"}],
    )
    mark = loop.tools._tools["plan.mark_step"].fn  # type: ignore[attr-defined]
    r1 = mark(index=0, status="in_progress")
    assert r1["step_status"] == "in_progress"
    r2 = mark(index=0, status="succeeded")
    assert r2["step_status"] == "succeeded"


def test_plan_read_returns_plan(todo: TodoService):
    aid = new_automaton_id()
    loop = build_default_loop(aid, todo=todo)
    create = loop.tools._tools["plan.create"].fn  # type: ignore[attr-defined]
    create(goal="x", steps=[{"description": "a"}])
    read = loop.tools._tools["plan.read"].fn  # type: ignore[attr-defined]
    result = read()
    assert result["goal"] == "x"
    assert result["steps"][0]["description"] == "a"
    assert result["steps"][0]["status"] == "pending"
    assert result["is_complete"] is False


def test_plan_tools_without_todo_raises():
    aid = new_automaton_id()
    loop = build_default_loop(aid)  # no todo
    fn = loop.tools._tools["plan.create"].fn  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError) as exc:
        fn(goal="x", steps=[{"description": "y"}])
    assert "TodoService" in str(exc.value)
