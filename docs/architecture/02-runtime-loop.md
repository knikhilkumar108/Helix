# Runtime Loop

The loop is the heartbeat of every Automaton. Each iteration is a small,
well-defined, auditable unit of work.

## Stages

| # | Stage            | Purpose                                                                 |
|---|------------------|-------------------------------------------------------------------------|
| 1 | Observe          | Snapshot the world: events, timers, mailbox, treasury, sensor inputs.  |
| 2 | Reason           | Pick the next sub-goal. Uses the LLM router for non-trivial reasoning.  |
| 3 | Retrieve memory  | Query the memory service for relevant past experience.                  |
| 4 | Generate plan    | Decompose the sub-goal into plan steps (tool / llm / external).         |
| 5 | Estimate cost    | Sum per-step costs; check against the BudgetController.                 |
| 6 | Constitution     | Evaluate the proposed plan against the immutable Constitution.          |
| 7 | Permission       | RBAC + ABAC.                                                            |
| 8 | Execute          | Run the plan via the ToolRegistry in a sandbox.                         |
| 9 | Verify           | Validate output schema; capture receipts.                               |
| 10| Learn            | Distill lessons into procedural / semantic memory.                      |
| 11| Update memory    | Persist new entries.                                                    |
| 12| Pay compute      | Charge the treasury for the cost of each action.                        |
| 13| Update treasury  | Emit metrics and audit events.                                          |
| 14| Sleep            | Yield the loop; allow other goroutines / signals.                       |

## Checkpointing

A snapshot of the loop state is written at the end of every iteration
(plus on signal). Snapshots are:

- content-addressed (sha-256)
- signed by the Automaton's Ed25519 key
- replicated to the object store

A crashed worker resumes from the latest snapshot.

## Cancellation and pausing

The loop honours three signals:

- `request_pause` — finish the current iteration, then stop issuing new
  actions. Memory and treasury state are still updated.
- `request_stop` — finish the current iteration, then exit.
- `SIGINT` / `SIGTERM` — same as `request_stop`, with a configurable grace
  period (default 30s).

## Failure semantics

- A failed action is recorded but does not abort the loop. The next tick
  re-evaluates.
- A budget block is a normal outcome; the loop sleeps longer.
- A constitution denial is logged and never retried without a code change.
- An unhandled exception is captured, the iteration marked `error`, and the
  loop continues (configurable).

## Bounds

The loop enforces hard caps on:

- actions per tick (`max_actions_per_tick`)
- runtime per tick (`max_runtime_seconds`)
- cost per tick / per day
- memory writes per tick
- recursion depth in tool calls
