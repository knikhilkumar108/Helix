"""
Plan mode with TODO.md — the agent's persistent plan.

The runtime already has a `Plan` object and a `Planner`. This
module is the *persistence* layer: it takes a `Plan` and writes
a `TODO.md` file the agent (and humans) can read.

Why a file?

Three reasons we keep TODO.md on disk rather than in memory:

  1. **Survives restarts.** An in-memory `Plan` is gone when
     the agent's process dies. A file is not.
  2. **Inspectable.** A human can `cat TODO.md` and see what
     the agent is working on. Operators find this invaluable.
  3. **Inspectable by the agent.** The agent reads TODO.md
     on every tick (via `fs.read`) to see what's left. This
     makes the plan part of the agent's own memory.

The TODO.md format is deliberately simple:

    # TODO: <goal>

    - [x] 0. first step (done at 2026-07-18T...)
    - [~] 1. second step (in progress)
    - [ ] 2. third step
    - [ ] 3. fourth step

    Estimated cost: $0.50
    Estimated revenue: $1.00
    Probability: 0.6

The agent can read this with `fs.read('TODO.md')` and see
exactly what state it's in. The checkboxes are the source
of truth for step completion; the runtime updates them
when a step transitions to `succeeded` or `failed`.

Plan mode is the *workflow*:

  1. The agent reasons about what to do.
  2. The agent calls `plan.create(goal, steps)` which writes
     a new TODO.md from scratch.
  3. The agent executes steps, calling
     `plan.mark_step_done(i)` after each one.
  4. When all steps are done, the plan's status flips to
     `succeeded` and the agent writes a summary back to
     memory.

A two-pass plan (plan → critique → execute) is supported via
the optional `critique` field on `Plan`. The agent can ask
itself "is this plan good?" before executing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from core.errors.errors import NotFoundError, ValidationError
from core.types.automaton import Plan, PlanStep
from core.types.identifiers import AutomatonId
from core.types.money import Money

log = logging.getLogger(__name__)


# ── Plan status (the agent-facing enum) ──────────────────


class TodoStatus(str, Enum):
    """The status of a single step in TODO.md.

    Maps to the `Plan.status` field for the overall plan,
    and to the checkbox in the markdown.
    """

    PENDING = "pending"      # [ ] — not started
    IN_PROGRESS = "in_progress"  # [~] — being worked on
    SUCCEEDED = "succeeded"  # [x] — done
    FAILED = "failed"        # [!] — failed (terminal for this step)


_STATUS_TO_MARK: dict[TodoStatus, str] = {
    TodoStatus.PENDING: "[ ]",
    TodoStatus.IN_PROGRESS: "[~]",
    TodoStatus.SUCCEEDED: "[x]",
    TodoStatus.FAILED: "[!]",
}


# ── Filesystem protocol ─────────────────────────────────


class TodoFileSystem(Protocol):
    """The interface the TODO service uses to read and write
    the file. The default is the local filesystem; tests can
    supply an in-memory dict-backed implementation."""

    def read_text(self, path: str) -> str: ...
    def write_text(self, path: str, content: str) -> None: ...
    def exists(self, path: str) -> bool: ...


class LocalTodoFileSystem:
    """The default filesystem implementation. Uses the agent's
    workspace as the root."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        # TODO.md is always at the workspace root, not a
        # subdirectory. We refuse paths that try to escape
        # the sandbox.
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


# ── TodoService ─────────────────────────────────────────


@dataclass(slots=True)
class TodoStep:
    """A single step in a plan, as it lives in TODO.md.

    `index` is the 0-based position in the plan.
    `status` is one of `TodoStatus`.
    `description` is the human-readable text.
    `completed_at` is set when the step transitions to a
    terminal state (`SUCCEEDED` or `FAILED`).
    """

    index: int
    description: str
    status: TodoStatus = TodoStatus.PENDING
    completed_at: str | None = None
    risk: str = "low"
    estimated_cost_micro: int = 0


@dataclass(slots=True)
class TodoPlan:
    """The plan as it lives in TODO.md — a list of steps plus
    a goal, an estimated cost, an estimated revenue, and a
    probability of success."""

    goal: str
    steps: list[TodoStep]
    estimated_cost: Money
    estimated_revenue: Money
    probability: float
    created_at: str
    automaton_id: AutomatonId
    plan_id: str
    critique: str | None = None  # optional self-critique


class TodoService:
    """The plan-mode service. Owns the `TODO.md` file and the
    state transitions of its steps.

    The service is *file-backed*: every operation reads or
    writes `TODO.md` on disk. This is deliberate — the file
    is the source of truth, not the in-memory state. The
    service caches the parsed plan in memory after a read,
    but every `mark_step_*` call writes the file.
    """

    TODO_FILENAME: str = "TODO.md"

    def __init__(
        self,
        *,
        filesystem: TodoFileSystem,
        automaton_id: AutomatonId,
        todo_path: str | None = None,
    ) -> None:
        self.fs = filesystem
        self.automaton_id = automaton_id
        self.todo_path = todo_path or self.TODO_FILENAME
        self._cached: TodoPlan | None = None

    # ── Plan creation ──
    def create_plan(
        self,
        *,
        goal: str,
        steps: list[dict[str, Any]],
        estimated_cost: Money,
        estimated_revenue: Money,
        probability: float = 0.5,
        critique: str | None = None,
        plan_id: str | None = None,
    ) -> TodoPlan:
        """Create a new plan and write TODO.md.

        `steps` is a list of dicts with at least `description`;
        optional fields: `risk`, `estimated_cost_micro`.

        Raises `ValidationError` if the inputs are invalid
        (empty goal, empty steps, etc.).
        """
        if not goal or not goal.strip():
            raise ValidationError("goal must be a non-empty string")
        if not steps:
            raise ValidationError("steps must be a non-empty list")
        if not 0 <= probability <= 1:
            raise ValidationError("probability must be in [0, 1]")
        # Build the TodoStep objects.
        todo_steps: list[TodoStep] = []
        for i, raw in enumerate(steps):
            if not isinstance(raw, dict):
                raise ValidationError(
                    f"step {i} must be a dict, got {type(raw).__name__}"
                )
            desc = raw.get("description")
            if not desc or not isinstance(desc, str):
                raise ValidationError(
                    f"step {i} must have a non-empty 'description' string"
                )
            todo_steps.append(
                TodoStep(
                    index=i,
                    description=desc,
                    risk=raw.get("risk", "low"),
                    estimated_cost_micro=raw.get("estimated_cost_micro", 0),
                )
            )
        plan = TodoPlan(
            goal=goal,
            steps=todo_steps,
            estimated_cost=estimated_cost,
            estimated_revenue=estimated_revenue,
            probability=probability,
            created_at=datetime.now(tz=timezone.utc).isoformat(timespec="microseconds"),
            automaton_id=self.automaton_id,
            plan_id=plan_id or f"plan_{len(todo_steps):03d}",
            critique=critique,
        )
        self._write(plan)
        self._cached = plan
        log.info(
            "plan_created",
            extra={
                "aid": str(self.automaton_id),
                "plan_id": plan.plan_id,
                "steps": len(plan.steps),
            },
        )
        return plan

    # ── Step transitions ──
    def mark_step(self, index: int, status: TodoStatus) -> TodoPlan:
        """Update a step's status. Reads the current TODO.md,
        applies the change, writes it back.

        `index` is the 0-based step position. `status` is the
        new status. Raises `ValidationError` for an invalid
        status transition.
        """
        plan = self.read_plan()
        if index < 0 or index >= len(plan.steps):
            raise ValidationError(
                f"step index {index} out of range "
                f"(plan has {len(plan.steps)} steps)"
            )
        step = plan.steps[index]
        # Validate the transition.
        valid = self._valid_transitions(step.status)
        if status not in valid:
            raise ValidationError(
                f"invalid transition: {step.status.value} → {status.value}; "
                f"valid next states: {[s.value for s in valid]}"
            )
        step.status = status
        if status in (TodoStatus.SUCCEEDED, TodoStatus.FAILED):
            step.completed_at = datetime.now(tz=timezone.utc).isoformat(
                timespec="microseconds"
            )
        self._write(plan)
        self._cached = plan
        return plan

    def mark_step_in_progress(self, index: int) -> TodoPlan:
        return self.mark_step(index, TodoStatus.IN_PROGRESS)

    def mark_step_succeeded(self, index: int) -> TodoPlan:
        return self.mark_step(index, TodoStatus.SUCCEEDED)

    def mark_step_failed(self, index: int) -> TodoPlan:
        return self.mark_step(index, TodoStatus.FAILED)

    # ── Inspection ──
    def read_plan(self) -> TodoPlan:
        """Read and parse TODO.md. Returns the parsed `TodoPlan`."""
        if self._cached is not None:
            return self._cached
        if not self.fs.exists(self.todo_path):
            raise NotFoundError(
                f"no plan file at {self.todo_path!r}; "
                f"call create_plan() first"
            )
        text = self.fs.read_text(self.todo_path)
        plan = self._parse(text)
        self._cached = plan
        return plan

    def has_plan(self) -> bool:
        return self.fs.exists(self.todo_path)

    def is_complete(self) -> bool:
        """A plan is complete when every step is `SUCCEEDED`."""
        if not self.has_plan():
            return False
        plan = self.read_plan()
        return all(s.status == TodoStatus.SUCCEEDED for s in plan.steps)

    def progress(self) -> dict[str, int]:
        """Counts of steps by status. Useful for the operator."""
        if not self.has_plan():
            return {"total": 0, "succeeded": 0, "failed": 0, "in_progress": 0, "pending": 0}
        plan = self.read_plan()
        counts = {s.value: 0 for s in TodoStatus}
        for step in plan.steps:
            counts[step.status.value] += 1
        counts["total"] = len(plan.steps)
        return counts

    # ── Serialization ──
    def _write(self, plan: TodoPlan) -> None:
        text = self._render(plan)
        self.fs.write_text(self.todo_path, text)

    def _render(self, plan: TodoPlan) -> str:
        lines: list[str] = []
        lines.append(f"# TODO: {plan.goal}")
        lines.append("")
        for step in plan.steps:
            mark = _STATUS_TO_MARK[step.status]
            ts = f" _(at {step.completed_at})_" if step.completed_at else ""
            cost = (
                f" _(${step.estimated_cost_micro / 1_000_000:.4f})_"
                if step.estimated_cost_micro
                else ""
            )
            lines.append(f"- {mark} {step.index}. {step.description}{cost}{ts}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"Estimated cost: {plan.estimated_cost}")
        lines.append(f"Estimated revenue: {plan.estimated_revenue}")
        lines.append(f"Probability: {plan.probability}")
        if plan.critique:
            lines.append("")
            lines.append("## Critique")
            lines.append("")
            lines.append(plan.critique)
        lines.append("")
        lines.append(f"_Created at {plan.created_at}_")
        return "\n".join(lines)

    def _parse(self, text: str) -> TodoPlan:
        """Parse TODO.md back into a `TodoPlan`.

        The parser is permissive: it understands the format
        we render, and silently ignores unknown lines (e.g.
        comments the agent added). It's a *best-effort*
        reader, not a strict markdown parser.
        """
        lines = text.split("\n")
        goal = ""
        steps: list[TodoStep] = []
        estimated_cost = Money.zero()
        estimated_revenue = Money.zero()
        probability = 0.5
        critique_lines: list[str] = []
        in_critique = False
        created_at = datetime.now(tz=timezone.utc).isoformat(timespec="microseconds")
        plan_id = ""
        automaton_id = self.automaton_id
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# TODO:"):
                goal = stripped[len("# TODO:"):].strip()
            elif stripped.startswith("- [") and len(stripped) > 4:
                # Parse a step line. The line looks like:
                #   - [x] 0. step description _(at 2026-...)_
                # We extract the 3-char mark (`[x]`, `[~]`, etc.),
                # the index (if present), and the description.
                # The trailing `_(at ...)_` is a completion timestamp
                # we attach when the step transitions to a terminal
                # state; we strip it during parsing so the description
                # is the original text.
                mark_end = stripped.find("]", 2)
                if mark_end < 0:
                    continue  # malformed line; skip
                mark = stripped[2:mark_end + 1]
                rest = stripped[mark_end + 1:].strip()
                # Strip a trailing timestamp annotation of the
                # form `_(at 2026-...)_` (added by the renderer).
                if "_(at " in rest and rest.endswith(")_"):
                    at_idx = rest.rfind("_(at ")
                    rest = rest[:at_idx].strip()
                # Try to extract the index (e.g. "0. first step").
                idx = -1
                if "." in rest:
                    head, _, after = rest.partition(".")
                    try:
                        idx = int(head.strip())
                        rest = after.strip()
                    except ValueError:
                        # Not a number; treat the whole thing as the description.
                        pass
                # Strip a leading `_$0.0000_` cost annotation, if present.
                # The renderer emits ` _($X.XXXX)_` between the
                # description and the trailing timestamp. We strip
                # it so the parsed description is the original text.
                if " _($" in rest:
                    cost_start = rest.find(" _($")
                    cost_end = rest.find(")_", cost_start)
                    if cost_end > cost_start:
                        rest = (rest[:cost_start] + rest[cost_end + 2:]).strip()
                status = TodoStatus.PENDING
                # The mark is case-insensitive: humans may write
                # `[X]` instead of `[x]`, or `[~]` with a space.
                mark_lower = mark.lower()
                if mark_lower == "[x]":
                    status = TodoStatus.SUCCEEDED
                elif mark_lower == "[~]":
                    status = TodoStatus.IN_PROGRESS
                elif mark_lower == "[!]":
                    status = TodoStatus.FAILED
                steps.append(
                    TodoStep(
                        index=idx if idx >= 0 else len(steps),
                        description=rest,
                        status=status,
                    )
                )
            elif stripped.startswith("Estimated cost:"):
                # Re-parse from the format "{amount} {currency}"
                parts = stripped[len("Estimated cost:"):].strip().split()
                if len(parts) >= 2:
                    try:
                        estimated_cost = Money.from_major(parts[0], parts[1])
                    except Exception:  # noqa: BLE001
                        pass
            elif stripped.startswith("Estimated revenue:"):
                parts = stripped[len("Estimated revenue:"):].strip().split()
                if len(parts) >= 2:
                    try:
                        estimated_revenue = Money.from_major(parts[0], parts[1])
                    except Exception:  # noqa: BLE001
                        pass
            elif stripped.startswith("Probability:"):
                try:
                    probability = float(stripped[len("Probability:"):].strip())
                except ValueError:
                    pass
            elif stripped.startswith("## Critique"):
                in_critique = True
            elif in_critique and stripped:
                critique_lines.append(stripped)
        critique = "\n".join(critique_lines) if critique_lines else None
        return TodoPlan(
            goal=goal,
            steps=steps,
            estimated_cost=estimated_cost,
            estimated_revenue=estimated_revenue,
            probability=probability,
            created_at=created_at,
            automaton_id=automaton_id,
            plan_id=plan_id,
            critique=critique,
        )

    # ── State machine ──
    def _valid_transitions(self, current: TodoStatus) -> set[TodoStatus]:
        if current == TodoStatus.PENDING:
            return {TodoStatus.IN_PROGRESS, TodoStatus.FAILED}
        if current == TodoStatus.IN_PROGRESS:
            return {TodoStatus.SUCCEEDED, TodoStatus.FAILED, TodoStatus.PENDING}
        if current == TodoStatus.SUCCEEDED:
            return set()  # terminal
        if current == TodoStatus.FAILED:
            return {TodoStatus.PENDING}  # can be retried
        return set()


# ── Factory ────────────────────────────────────────────


def make_todo_service(
    *,
    workspace: Path,
    automaton_id: AutomatonId,
) -> TodoService:
    """Convenience factory. Builds a `TodoService` with the
    default `LocalTodoFileSystem` rooted at `workspace`."""
    return TodoService(
        filesystem=LocalTodoFileSystem(workspace),
        automaton_id=automaton_id,
    )


__all__ = [
    "LocalTodoFileSystem",
    "TodoFileSystem",
    "TodoPlan",
    "TodoService",
    "TodoStatus",
    "TodoStep",
    "make_todo_service",
]
