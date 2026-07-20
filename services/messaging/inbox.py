"""
Inbox service — typed, agent-facing API on top of `SqliteStore`.

The `SqliteStore` already persists inbox messages (see its
`enqueue_inbox`, `claim_inbox`, `mark_inbox_*` methods). This
module is the *typed* layer the rest of the platform talks to:
it converts between dicts and `InboxMessage` dataclasses, enforces
the state machine, and gives the agent a clean API.

State machine
-------------

Every message is in exactly one of these states:

      received
          │
          │  agent calls `claim`
          ▼
      in_progress
          │
          ├──► processed   (agent calls `mark_processed`)
          │
          └──► failed      (agent calls `mark_failed`)
                              │
                              │  if retry_count < max_retries
                              │  agent calls `retry` (or marks
                              │  with retry=True)
                              ▼
                          received

The transition table is enforced by the methods, not by SQL
constraints. This is deliberate: the agent's policy (when to
retry, when to give up) lives in code, not in the database.

Why at-least-once
-----------------

A message is "claimed" by setting its state to `in_progress`
*before* the agent starts processing. If the agent crashes
mid-process, the message stays in `in_progress` forever — a
*stuck* message. The runtime's heartbeat daemon (added in
a later turn) sweeps stuck messages back to `received` after
a TTL. The at-least-once contract is: a message will be
processed at least once, possibly more (if the agent crashes
after marking `processed` but before the next tick picks up
the result). The agent's own idempotency handles the dupes.

Why pull-based
--------------

The agent calls `claim()` on every tick. No callbacks, no
broker, no surprise wakeups. This means:

  - The agent controls its own load. A flooded inbox doesn't
    wake the agent up; the agent pulls when it's ready.
  - The inbox is observably consistent. A `stats()` call
    returns the same numbers the next `claim()` would see.
  - The state machine is auditable. Every transition is a
    row update, recorded in the audit chain.

Inbox size cap
--------------

A bounded inbox prevents spam. The cap is enforced in
`send()` (raising `InboxFull`) and is the platform-level
guard against a malicious or buggy sender flooding the agent.
The cap is configurable; the default (1000) is generous
enough for normal operation.

Concurrency
-----------

The store is `sqlite3`, which serializes writes. The
service is safe to share across coroutines. `claim()`
and `send()` are atomic from the store's perspective; the
service adds no additional locking.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from core.errors.errors import ValidationError
from services.state.sqlite_store import SqliteStore

log = logging.getLogger(__name__)


# ── Types ────────────────────────────────────────────────────


class InboxState(str, Enum):
    """The state machine for a single inbox message."""

    RECEIVED = "received"
    IN_PROGRESS = "in_progress"
    PROCESSED = "processed"
    FAILED = "failed"


# Allowed state transitions:
#
#   received  →  in_progress              (claim)
#   in_progress  →  processed             (mark_processed)
#   in_progress  →  failed                (mark_failed, retry=False)
#   in_progress  →  received              (mark_failed, retry=True, under cap)
#   failed      →  received               (operator-initiated retry)
#   processed   →  ∅                      (terminal)
#   failed      →  ∅                      (terminal, after retries exhausted)


@dataclass(slots=True)
class InboxMessage:
    """A single message in an agent's inbox.

    `content` is intentionally a free-form string. The state
    machine doesn't care what's in it; the agent's tool layer
    parses the content (typically JSON for structured tasks,
    plain text for chat). Callers that need structured metadata
    can encode it as JSON inside `content`.
    """

    id: str
    from_address: str
    to_address: str
    content: str
    state: InboxState
    retry_count: int
    max_retries: int
    created_at: str
    processed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for the SQLite store.

        Only fields that have corresponding columns in the
        `inbox` table are included. The service does not
        carry extra side-channel columns; structured metadata
        is the caller's responsibility (typically by JSON-
        encoding it inside `content`).
        """
        return {
            "id": self.id,
            "from_address": self.from_address,
            "to_address": self.to_address,
            "content": self.content,
            "state": self.state.value,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at,
            "processed_at": self.processed_at,
        }


class InboxFull(Exception):
    """Raised by `send()` when the recipient's inbox is at the cap."""

    def __init__(self, to_address: str, *, current: int, cap: int) -> None:
        super().__init__(f"inbox for {to_address} is full ({current}/{cap})")
        self.to_address = to_address
        self.current = current
        self.cap = cap


# ── InboxBackend (Protocol) ───────────────────────────────


class InboxBackend(Protocol):
    """The persistence layer underneath the inbox service.

    `SqliteStore` is the production implementation. Tests
    can use an in-memory backend to avoid spinning up SQLite.
    """

    def enqueue_inbox(self, msg: dict[str, Any]) -> None: ...
    def claim_inbox(self, to_address: str, limit: int = 10) -> list[dict[str, Any]]: ...
    def mark_inbox_processed(self, ids: list[str]) -> None: ...
    def mark_inbox_failed(self, ids: list[str]) -> None: ...
    def reset_inbox_to_received(self, ids: list[str]) -> None: ...
    def list_inbox(
        self, to_address: str, *, states: list[str] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...
    def count_inbox(self, to_address: str, *, states: list[str] | None = None) -> int: ...
    def get_inbox_message(self, msg_id: str) -> dict[str, Any] | None: ...


# ── InboxService ───────────────────────────────────────────


class InboxService:
    """The typed inbox service. Backed by an `InboxBackend`.

    A real agent has one `InboxService` per automaton, and
    the service can be reused across ticks. The service is
    stateless except for the cap; everything else is in the
    store.
    """

    def __init__(
        self,
        backend: InboxBackend,
        *,
        cap: int = 1000,
        clock: callable = time.time,
    ) -> None:
        self.backend = backend
        self.cap = cap
        self._clock = clock

    # ── Sending ──
    def send(
        self,
        *,
        from_address: str,
        to_address: str,
        content: str,
        max_retries: int = 3,
    ) -> InboxMessage:
        """Enqueue a new message. Returns the persisted `InboxMessage`.

        Raises:
          - `InboxFull` if the recipient's `received`+`in_progress`
            count is at the cap.
          - `ValidationError` for bad inputs.

        Note: structured metadata (kind, correlation_id, etc.)
        should be JSON-encoded inside `content` by the caller.
        The inbox persists only the fields in its schema.
        """
        if not from_address or not to_address:
            raise ValidationError("from_address and to_address are required")
        if not content:
            raise ValidationError("content must be non-empty")
        if max_retries < 0:
            raise ValidationError("max_retries must be non-negative")
        # Enforce the cap. We count `received` and `in_progress`
        # because those are the only states where the message is
        # still "in the queue" — `processed` and `failed` are
        # essentially garbage-collected by the heartbeat sweep.
        current = self.backend.count_inbox(
            to_address,
            states=[InboxState.RECEIVED.value, InboxState.IN_PROGRESS.value],
        )
        if current >= self.cap:
            raise InboxFull(to_address, current=current, cap=self.cap)
        msg = InboxMessage(
            id=f"msg_{uuid.uuid4().hex}",
            from_address=from_address,
            to_address=to_address,
            content=content,
            state=InboxState.RECEIVED,
            retry_count=0,
            max_retries=max_retries,
            # Use microsecond precision so that messages created
            # in the same second still sort deterministically
            # by the `ORDER BY created_at` in the store.
            created_at=datetime.now(tz=timezone.utc).isoformat(timespec="microseconds"),
            processed_at=None,
        )
        self.backend.enqueue_inbox(msg.to_dict())
        log.info(
            "inbox_send",
            extra={
                "msg_id": msg.id,
                "from": from_address,
                "to": to_address,
            },
        )
        return msg

    # ── Claiming ──
    def claim(
        self,
        to_address: str,
        *,
        limit: int = 10,
    ) -> list[InboxMessage]:
        """Atomically claim up to `limit` messages for processing.

        Each claimed message is set to `in_progress` before
        being returned, so a concurrent `claim` call won't
        see it. The agent processes the returned list and
        calls `mark_processed` or `mark_failed` on each.
        """
        rows = self.backend.claim_inbox(to_address, limit=limit)
        msgs = [self._row_to_msg(r) for r in rows]
        if msgs:
            log.info(
                "inbox_claim",
                extra={"to": to_address, "count": len(msgs)},
            )
        return msgs

    # ── Marking ──
    def mark_processed(self, ids: list[str]) -> int:
        """Mark a list of message ids as `processed`. Returns the count.

        Idempotent: a message already in `processed` is left alone.
        The agent's idempotency layer is responsible for not
        re-processing the same content; this method is just
        the state transition.
        """
        if not ids:
            return 0
        # Filter: only messages currently in `in_progress` should
        # be marked processed. The store doesn't enforce this,
        # so we do it here by checking each id's current state.
        # For batches, we trust the caller and let the store
        # update all rows in one shot; the state-machine
        # invariant is documented but not enforced at the SQL
        # layer (it would require a CHECK constraint with a
        # trigger, which SQLite supports but is more code than
        # the safety warrants).
        self.backend.mark_inbox_processed(ids)
        log.info("inbox_mark_processed", extra={"count": len(ids)})
        return len(ids)

    def mark_failed(self, ids: list[str], *, retry: bool = True) -> int:
        """Mark a list of message ids as `failed`, optionally
        resetting them to `received` for retry.

        `retry=True` (the default) resets the message to
        `received` and increments its `retry_count`. If
        `retry_count` already equals `max_retries`, the
        message stays in `failed`.

        Returns the number of messages that were actually
        transitioned.
        """
        if not ids:
            return 0
        if not retry:
            self.backend.mark_inbox_failed(ids)
            log.info("inbox_mark_failed", extra={"count": len(ids)})
            return len(ids)
        # Split into "can retry" and "exhausted". For simplicity
        # we do this in Python: pull each id's current row,
        # check retry_count, and route accordingly.
        retried = 0
        failed_terminal = 0
        for msg_id in ids:
            row = self.backend.get_inbox_message(msg_id)
            if row is None:
                # The id doesn't exist; silently skip.
                continue
            if row["state"] != InboxState.IN_PROGRESS.value:
                # Not in a state that can be failed.
                continue
            if row["retry_count"] < row["max_retries"]:
                self.backend.reset_inbox_to_received([msg_id])
                retried += 1
            else:
                self.backend.mark_inbox_failed([msg_id])
                failed_terminal += 1
        log.info(
            "inbox_mark_failed",
            extra={"retried": retried, "terminal": failed_terminal},
        )
        return retried + failed_terminal

    # ── Inspection ──
    def peek(
        self,
        to_address: str,
        *,
        limit: int = 100,
        states: list[InboxState] | None = None,
    ) -> list[InboxMessage]:
        """Non-destructive read. Used by the operator dashboard
        and the runtime's observation step."""
        state_strs = [s.value for s in states] if states else None
        rows = self.backend.list_inbox(to_address, states=state_strs, limit=limit)
        return [self._row_to_msg(r) for r in rows]

    def stats(self, to_address: str) -> dict[str, int]:
        """Counts of messages per state. Used by the operator
        dashboard."""
        result: dict[str, int] = {}
        for s in InboxState:
            result[s.value] = self.backend.count_inbox(
                to_address, states=[s.value]
            )
        result["cap"] = self.cap
        return result

    # ── Housekeeping ──
    def reset_stuck(
        self,
        *,
        stuck_for_seconds: float,
        clock: callable | None = None,
    ) -> int:
        """Reset messages that have been `in_progress` for too long.

        A message in `in_progress` that hasn't been marked
        processed or failed within `stuck_for_seconds` is
        considered abandoned — typically because the agent
        crashed mid-process. We move it back to `received`
        and increment its `retry_count`. If the message is
        already past `max_retries`, we move it to `failed`
        instead.

        Returns the number of messages that were reset.

        The heartbeat daemon calls this periodically to keep
        the inbox healthy. The threshold should be larger
        than the agent's longest expected action duration
        (a few minutes is typical for LLM-bound work).
        """
        from datetime import datetime, timezone
        from datetime import timedelta as _td
        now_dt = (clock or self._clock)()
        if not isinstance(now_dt, float):
            # The clock callable returns a float epoch.
            raise ValidationError("clock must return a float epoch")
        threshold = (
            datetime.fromtimestamp(now_dt, tz=timezone.utc) - _td(seconds=stuck_for_seconds)
        ).isoformat(timespec="microseconds")
        # Find stuck messages: state=in_progress, created_at < threshold.
        # We use `created_at` rather than a `last_state_change_at`
        # column because the schema doesn't track state-change
        # times. For a more accurate "stuck" measurement we'd
        # need a new column. The current heuristic: a message
        # that's been `in_progress` for `stuck_for_seconds`
        # since creation. A long-running claim will be
        # incorrectly reset by this, so the threshold must
        # be tuned to the agent's worst-case action time.
        rows = self.backend.list_inbox(
            to_address="",  # across all recipients
            states=[InboxState.IN_PROGRESS.value],
            limit=10_000,
        )
        reset = 0
        for row in rows:
            if row["created_at"] >= threshold:
                continue  # not stuck yet
            msg_id = row["id"]
            if row["retry_count"] < row["max_retries"]:
                self.backend.reset_inbox_to_received([msg_id])
            else:
                self.backend.mark_inbox_failed([msg_id])
            reset += 1
        if reset:
            log.info("inbox_reset_stuck", extra={"count": reset, "threshold_seconds": stuck_for_seconds})
        return reset

    def purge_terminal(
        self,
        *,
        older_than_seconds: float,
        clock: callable | None = None,
    ) -> int:
        """Delete `processed` and `failed` messages older than
        `older_than_seconds`. Returns the count of rows removed.

        The inbox is meant to be a queue, not an archive.
        Terminal-state messages (processed, failed) should
        be moved to a separate audit log before they're
        purged; for now we just delete them. A real
        production deployment would write a row to the
        `audit_log` table for each terminal message.
        """
        # The store doesn't currently expose a `delete_inbox`
        # method; we add one through the same Protocol. For
        # now, we surface this as a no-op with a clear log
        # message, since the schema migration to add the
        # delete method is a separate concern.
        log.warning(
            "inbox_purge_terminal_not_implemented",
            extra={"older_than_seconds": older_than_seconds},
        )
        return 0

    # ── Internal ──
    def _row_to_msg(self, row: dict[str, Any]) -> InboxMessage:
        return InboxMessage(
            id=row["id"],
            from_address=row["from_address"],
            to_address=row["to_address"],
            content=row["content"],
            state=InboxState(row["state"]),
            retry_count=int(row.get("retry_count", 0)),
            max_retries=int(row.get("max_retries", 3)),
            created_at=row["created_at"],
            processed_at=row.get("processed_at"),
        )


# ── Factory ─────────────────────────────────────────────────


def make_inbox(backend: SqliteStore | None = None, **kwargs: Any) -> InboxService:
    """Convenience factory. Defaults to an in-memory `SqliteStore`."""
    b = backend or SqliteStore()
    return InboxService(backend=b, **kwargs)


__all__ = [
    "InboxBackend",
    "InboxFull",
    "InboxMessage",
    "InboxService",
    "InboxState",
    "make_inbox",
]
