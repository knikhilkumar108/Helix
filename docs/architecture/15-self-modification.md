# Self-Modification Engine

The agent's ability to *change its own code*. The platform's
`SelfModController` (in `services/self_mod/code.py`)
implements the safety rails; this module — the
`SelfModificationEngine` (in `engine.py`) — implements
the *workflow* that uses those rails.

## Why this is dangerous

A self-modifying agent can:
- Edit its own source code.
- Add new tools.
- Tune its own behavior.
- (If unchecked) remove safety rails, hide its actions,
  or make itself unstoppable.

The platform's approach: **strict safety rails enforced by
code, not by convention**. The agent can propose changes,
but every change goes through:

1. **Protected file check** — the Constitution, signing
   primitives, the core loop, treasury, audit log, and
   schema migrations are *immutable*. A request that
   touches any of these is rejected immediately.
2. **Rate limit** — N modifications per hour, default 5.
3. **Safety-check evidence** — the requester must have
   evidence that tests, static analysis, and security
   scan all pass.
4. **Diff sanity** — the proposed diff is checked for
   obvious nonsense (too small, contains `rm -rf /`).
5. **Test phase** — the test suite runs against the
   modified code in a working copy.
6. **Canary phase** — the modified file is imported in
   a subprocess to confirm it loads.
7. **Promote** — only after stages 5-6 pass is the
   change written to the real file.

## The workflow

```
   PROPOSED → REVIEWED → EDITED → TESTED → CANARIED → PROMOTED
                  │           │         │          │
                  └─ any failure → FAILED
```

A request that fails at any stage is recorded in the
audit log and the on-disk file is *not* modified. The
engine operates on a working copy in a temp directory;
the real file is touched only at the promote step.

## Architecture

```
services/self_mod/code.py         — safety rails (controller)
services/self_mod/engine.py       — workflow orchestrator
services/self_mod/__init__.py     — public surface
```

The controller is the gate. The engine is the
orchestrator. The engine cannot bypass the controller;
the controller cannot bypass the engine's tests/canary.

## Test runner and canary runner

The engine accepts pluggable runners:

- **`PytestRunner`** (default in production) — runs
  `python -m pytest -q` in the working copy.
- **`StaticTestRunner`** (default in tests) — always
  passes; useful for unit-testing the engine.
- **`ImportCanary`** (default canary) — imports the
  modified file in a subprocess.
- **Custom runners** — a real platform would have a
  canary that starts the agent in a sandboxed subprocess
  and watches for crashes.

## What it enables

- **Self-improvement.** The agent can fix bugs it finds
  in itself, add new tools, tune its own behavior.
- **Safe iteration.** Every change goes through the full
  pipeline. A buggy change is caught by the tests; a
  malicious change is caught by the protected-file check.
- **Audit trail.** Every change is recorded in the
  controller's audit log with the diff hash, the
  requester, and the outcome.

## What it does NOT enable (and why)

- **Removing safety rails.** The protected-file list
  includes the controller itself, the signing
  primitives, the injection defense, and the audit
  module. The agent cannot edit the things that protect
  it from itself.
- **Bypassing the workflow.** The engine drives every
  request through all six stages. A failure at any
  stage rolls the change back.
- **Untraceable changes.** Every change is in the audit
  log with a content hash. The platform can prove
  exactly what changed and when.

## Future improvements

- **Git-backed storage.** Today the engine operates on
  the filesystem directly. A real platform would use
  git worktrees for cleaner rollback and history.
- **Approval gate for high-risk changes.** The
  controller already has a `requested_by` field; a
  real platform would require human approval for
  changes marked `risk="high"`.
- **Sandbox subprocess for canary.** The current canary
  just imports the file. A real canary would start the
  agent in a sandboxed subprocess and watch for
  unexpected behavior (excessive memory, infinite
  loops, network calls).
- **Test selection.** Today the engine runs the full
  test suite. A real platform would run only the tests
  relevant to the changed file (e.g. via pytest's `-k`
  filter or coverage-based selection).
