# Inbox: Agent-to-Agent Messaging

The agent's asynchronous message queue. Other agents (or humans)
drop messages into an agent's inbox; the agent claims them on its
next tick, processes them, and marks them done. The inbox is
the platform's primary mechanism for *decoupled* coordination:
the sender doesn't wait, the recipient processes when ready.

## Why we need it

Without the inbox, agent-to-agent coordination has only two
options:

- **Synchronous HTTP**: agent A calls agent B and waits. This
  couples lifetimes — if B is dead, A is stuck.
- **Shared database**: A and B both read and write the same
  rows. This requires both to be online and to agree on a
  schema.

The inbox decouples lifetimes. A can be DEAD by the time B
processes the message, and that's fine — the message sits
in B's queue until B is alive again.

## State machine

Every inbox message is in one of four states:

```
   received
       │
       │  agent calls claim()
       ▼
   in_progress
       │
       ├──► processed    (mark_processed)
       │
       └──► failed       (mark_failed with retry=False)
                           │
                           │  if retry_count < max_retries
                           │  (mark_failed with retry=True)
                           ▼
                       received
```

The transitions are explicit; nothing in the database
prevents an invalid transition. The service layer documents
the table and relies on the agent to follow the lifecycle.

## At-least-once delivery

A message is "claimed" by setting its state to `in_progress`
*before* the agent starts processing. If the agent crashes
mid-process, the message stays in `in_progress` forever. The
heartbeat daemon (added in a later turn) sweeps stuck
messages back to `received` after a TTL. So a message is
processed at least once, possibly more. The agent's own
idempotency layer handles duplicates.

## Pull-based

The agent calls `claim()` on every tick. No callbacks, no
broker, no surprise wakeups. This means:

- **The agent controls its own load.** A flooded inbox
  doesn't wake the agent up; the agent pulls when it's
  ready.
- **The inbox is observably consistent.** A `stats()` call
  returns the same numbers the next `claim()` would see.
- **The state machine is auditable.** Every transition is
  a row update.

## Architecture

```
services/messaging/inbox.py        — InboxService, InboxMessage, InboxState
services/state/sqlite_store.py     — SQL persistence (already existed)
runtime/loop/builtins.py           — messaging.send / claim / mark_* tools
runtime/loop/context.py            — observation surfaces pending count
runtime/loop/loop_init.py          — `inbox=` parameter on the builders
```

The `SqliteStore` already had the schema and primitive
operations (`enqueue_inbox`, `claim_inbox`, `mark_inbox_*`).
The `InboxService` adds the typed layer and the inbox cap;
the runtime's built-in tools give the agent direct access.

## Tools the agent has

| Tool                 | Description                                          |
|----------------------|------------------------------------------------------|
| `messaging.send`     | Send a message to another agent's inbox              |
| `messaging.claim`    | Claim pending messages from your own (or anyone's) inbox |
| `messaging.mark_processed` | Mark a claimed message as done                |
| `messaging.mark_failed`    | Mark as failed, optionally resetting for retry  |

If an agent has no inbox wired (`registry.extra["inbox"]`
unset), these tools raise `RuntimeError` with a clear
message. The agent fails loudly rather than silently
"success"-ing.

## Observation

On every tick, the runtime's observation step includes an
`"inbox"` key:

```json
{
  "events": [...],
  "now": 1234567890.0,
  "inbox": {
    "pending": 3,
    "received": 2,
    "in_progress": 1,
    "cap": 1000
  }
}
```

`pending = received + in_progress`. The agent sees this
in its observation and decides whether to claim. A real
agent with an LLM reasoner will likely call
`messaging.claim` when `pending > 0`.

## What it enables

- **Decoupled coordination.** Agent A finishes a task and
  delegates the next step to agent B. A doesn't wait.
- **Asynchronous work queue.** Humans or services can drop
  jobs into an agent's inbox for batch processing.
- **Failure recovery.** A crashed mid-task message is
  re-claimed on the next tick (after the heartbeat sweep).

## Future improvements

- **Conversation threading.** A `thread_id` field on
  messages would let the agent group related messages
  (e.g. a multi-turn conversation with another agent).
- **Per-sender rate limiting.** The current cap is global;
  a malicious sender could fill the inbox. Per-sender
  caps would bound the damage.
- **TTL on received messages.** Old `received` messages
  with no claims should be moved to `failed` after a TTL
  so the inbox doesn't grow unbounded.
- **Push notifications.** Today the agent polls. A push
  channel (e.g. a webhook from the inbox to the agent's
  runtime) would reduce latency for high-priority
  messages.
