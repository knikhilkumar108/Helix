# Plan Mode with TODO.md

The agent's persistent plan. The runtime already has a
`Plan` object in memory; this module adds a *file* version
of the plan, `TODO.md`, that the agent (and humans) can
read on disk.

## Why a file

Three reasons we keep TODO.md on disk rather than purely
in memory:

1. **Survives restarts.** An in-memory `Plan` is gone when
   the agent's process dies. A file is not.
2. **Inspectable.** A human can `cat TODO.md` and see what
   the agent is working on. Operators find this invaluable
   for debugging.
3. **Inspectable by the agent.** The agent reads TODO.md
   on every tick (via `fs.read`) to see what's left. This
   makes the plan part of the agent's own memory.

## The format

TODO.md is plain markdown:

```markdown
# TODO: summarize the report

- [x] 0. read the report _(at 2026-07-18T...)_ _($0.0500)_
- [~] 1. extract key points _($0.0250)_
- [ ] 2. write summary _($0.1000)_

---

Estimated cost: 0.175000 USDC
Estimated revenue: 1.000000 USDC
Probability: 0.6

_Created at 2026-07-18T..._
```

Each step has a checkbox:
- `[ ]` — pending
- `[~]` — in progress
- `[x]` — succeeded
- `[!]` — failed

The annotations (`_(at ...)_`, `_($X.XXXX)_`) are added by
the renderer and stripped by the parser. Humans can edit
the file freely; the parser is best-effort, not strict.

## State machine

Each step transitions through:

```
   pending
      │
      │  mark_step_in_progress
      ▼
   in_progress
      │
      ├──► succeeded   (mark_step_succeeded)
      │
      └──► failed      (mark_step_failed)
                          │
                          │  mark_step(pending)  (retry)
                          ▼
                       pending
```

`SUCCEEDED` is terminal. `FAILED` can be retried by going
back to `PENDING`. The state machine is enforced by
`TodoService.mark_step()` — invalid transitions raise
`ValidationError`.

## Plan mode workflow

1. The agent reasons about what to do.
2. The agent calls `plan.create(goal, steps)` which writes
   a new TODO.md from scratch.
3. The agent executes steps, calling `plan.mark_step(0, "in_progress")`,
   then later `plan.mark_step(0, "succeeded")` after the step
   completes.
4. When all steps are `SUCCEEDED`, the plan's `is_complete`
   returns `True`. The agent can write a summary to memory
   at that point.

## Two-pass planning (plan → critique)

The `create_plan()` method accepts an optional `critique`
argument. The agent can:

1. Draft a plan.
2. Read TODO.md, think about whether it's good.
3. Re-create the plan with a `critique` field that says
   "this plan is good because…" or "this plan is risky
   because…".

A real implementation would feed the critique back into
the LLM to generate a better plan on the next iteration.
The current implementation is a single-pass plan with an
optional critique annotation.

## Architecture

```
services/planning/todo.py            — TodoService, TodoStep, TodoStatus
services/planning/__init__.py        — public surface
runtime/loop/builtins.py             — plan.* tools (create, mark_step, read)
runtime/loop/loop_init.py            — `todo=` parameter on the builders
```

## Tools the agent has

| Tool                | Description                              |
|---------------------|------------------------------------------|
| `plan.create`       | Create a new plan and write TODO.md      |
| `plan.mark_step`    | Update a step's status                   |
| `plan.read`         | Read the current plan from TODO.md       |

If an agent has no `TodoService` wired, the tools raise
`RuntimeError` with a clear message.

## What it enables

- **Persistent plans.** Restart the agent and pick up
  where you left off.
- **Operator visibility.** A human can `cat` the agent's
  TODO.md and see its state.
- **Multi-step workflows.** The agent plans a sequence
  of tool calls and marks each one as it completes.
- **Failure recovery.** A failed step is in TODO.md with
  `[!]`. The agent (or operator) can retry it.

## Future improvements

- **LLM-backed plan generation.** A real planner would
  use the LLM to propose steps given a goal, with a
  critique loop. Today's `plan.create` requires the
  agent (or its code) to pass steps explicitly.
- **Plan history.** Archive completed TODO.md files so
  the agent can refer to past plans. A real agent learns
  from its own past plans.
- **Per-step dependencies.** The format already has
  `depends_on` indices in the `PlanStep` type. The
  file format doesn't yet express them.
- **Sub-tasks.** A step can be a sub-plan (a list of
  sub-steps). Useful for "summarize the report" which
  has its own internal workflow.
