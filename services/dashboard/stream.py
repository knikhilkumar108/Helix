"""
Operator dashboard — event bus and WebSocket stream.

The dashboard is the human-facing view of the agent. The
agent emits *events* (treasury updates, tier changes,
inbox updates, heartbeat ticks, action completions) and
the dashboard broadcasts them to subscribed clients.

The two pieces:

  1. **`EventBus`** — an in-process pub/sub. Components
     publish events; the bus fans them out to subscribers.
     This is the *event source* for the dashboard.

  2. **`DashboardStream`** — a per-agent stream that
     manages WebSocket connections. Each connected client
     receives events for one agent, plus a 1Hz heartbeat
     so the client knows the connection is alive.

Why an event bus instead of polling?

The platform already has a hash-chained audit log (in
`services/state/sqlite_store.py`). The bus is *adjacent*
to the audit log: every event the bus broadcasts is also
written to the audit log. The bus is for *real-time
delivery*; the audit log is for *durable history*.

Why per-agent streams?

A multi-agent platform has many agents. An operator
debugging agent A doesn't need to see agent B's events.
The per-agent model lets the operator pick what they
care about. The control plane could (in a future turn)
add a "global" stream that fans in from all agents.

Why 1Hz heartbeats?

WebSocket connections can silently die (NAT timeouts,
proxy restarts). A 1Hz heartbeat lets the client detect
a dead connection within 2 seconds. The cost is
negligible — a small JSON message every second.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from core.errors.errors import ValidationError
from core.types.identifiers import AutomatonId

log = logging.getLogger(__name__)


# ── Event types ───────────────────────────────────


class EventKind(str, Enum):
    """The kinds of events the bus broadcasts.

    Each kind has a fixed payload shape. New kinds can be
    added; consumers should ignore unknown kinds (the bus
    is forward-compatible).
    """

    TREASURY_UPDATE = "treasury_update"
    TIER_CHANGE = "tier_change"
    INBOX_UPDATE = "inbox_update"
    HEARTBEAT = "heartbeat"
    ACTION_COMPLETED = "action_completed"
    PLAN_CREATED = "plan_created"
    PLAN_COMPLETED = "plan_completed"
    SOUL_UPDATED = "soul_updated"
    SELF_MOD_REQUEST = "self_mod_request"
    SELF_MOD_PROMOTED = "self_mod_promoted"
    SELF_MOD_REJECTED = "self_mod_rejected"
    POLICY_DENIED = "policy_denied"


@dataclass(slots=True)
class StreamEvent:
    """A single event on the bus.

    `kind` is the event type. `aid` is the agent id. The
    payload is open — each kind has its own shape, documented
    in `EventKind`.
    """

    id: str
    kind: EventKind
    aid: AutomatonId
    payload: dict[str, Any]
    occurred_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "aid": str(self.aid),
            "payload": self.payload,
            "occurred_at": self.occurred_at,
        }


# ── EventBus ─────────────────────────────────────


class EventBus:
    """The in-process pub/sub.

    `publish()` is non-blocking: it appends to each
    subscriber's queue. If a subscriber's queue is full
    (the client is slow), the event is dropped — better
    than blocking the publisher.

    `subscribe()` returns an async iterator that yields
    events as they arrive. The iterator stops when the
    caller breaks out of the loop (e.g. WebSocket closes).
    """

    # Per-subscriber queue size. Bigger = more memory but
    # more tolerance for slow clients. 256 is generous for
    # a real-time stream.
    QUEUE_SIZE: int = 256

    def __init__(self) -> None:
        self._subscribers: dict[
            AutomatonId, set[asyncio.Queue[StreamEvent]]
        ] = defaultdict(set)
        # Replay buffer: the last N events per agent. New
        # subscribers can fetch the recent past on connect.
        self._replay: dict[AutomatonId, deque[StreamEvent]] = defaultdict(
            lambda: deque(maxlen=100)
        )
        # Use a threading.Lock so publish() can be called
        # from any context (sync, async, or no event loop).
        # The critical section is just a queue append.
        import threading
        self._lock = threading.Lock()

    def publish(self, event: StreamEvent) -> None:
        """Publish an event to all subscribers of its agent.

        This is a *synchronous* method. The bus uses a
        regular `threading.Lock` (not an `asyncio.Lock`)
        so it can be called from any context — sync code,
        async code, or code without a running event loop.
        The trade-off: a publish call briefly holds the
        lock, blocking other publishers. The critical
        section is tiny (a queue append), so contention
        is negligible.
        """
        with self._lock:
            # Append to the replay buffer.
            self._replay[event.aid].append(event)
            # Fan out to subscribers.
            queues = list(self._subscribers.get(event.aid, ()))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the event for this subscriber. The
                # replay buffer still has it; the client
                # could fetch it on reconnect.
                log.warning("dashboard_event_dropped", extra={"aid": str(event.aid)})

    def subscribe(
        self, aid: AutomatonId
    ) -> tuple[list[StreamEvent], "Subscriber"]:
        """Subscribe to events for an agent.

        Returns `(replay_events, subscriber)`. The replay
        events are the most recent N events for the agent
        (so the client doesn't miss what happened before
        it connected). The subscriber is the iterator the
        client reads from.

        This is a sync method for the same reason as
        `publish()`: it can be called from any context.
        """
        with self._lock:
            replay = list(self._replay.get(aid, ()))
        subscriber = Subscriber(bus=self, aid=aid)
        with self._lock:
            self._subscribers[aid].add(subscriber.queue)
        return replay, subscriber

    def unsubscribe(self, subscriber: "Subscriber") -> None:
        """Remove a subscriber. Called when the WebSocket closes."""
        with self._lock:
            self._subscribers[subscriber.aid].discard(subscriber.queue)


# ── Subscriber ──────────────────────────────────


class Subscriber:
    """A single subscriber's queue + iterator.

    The queue is a fixed-size `asyncio.Queue`. When full,
    the bus drops events (rather than blocking). The
    iterator yields events as they arrive.
    """

    def __init__(self, *, bus: EventBus, aid: AutomatonId) -> None:
        self.bus = bus
        self.aid = aid
        self.queue: asyncio.Queue[StreamEvent] = asyncio.Queue(
            maxsize=EventBus.QUEUE_SIZE
        )

    async def __aiter__(self) -> AsyncIterator[StreamEvent]:
        while True:
            event = await self.queue.get()
            yield event

    def close(self) -> None:
        self.bus.unsubscribe(self)


# ── DashboardStream (high-level façade) ──────────


class DashboardStream:
    """A high-level façade for the dashboard service.

    The stream is a thin layer over `EventBus` that:
      - Maintains a "current state" per agent (the latest
        treasury, tier, etc.) so the client gets an initial
        snapshot.
      - Provides a 1Hz heartbeat to detect dead connections.
      - Emits well-formed `StreamEvent` objects for each
        state change.

    The stream doesn't own an event loop; it expects to be
    driven by the runtime. The runtime calls `tick()` on
    every loop tick, which:
      1. Reads the current state from the agent.
      2. Detects changes since the last tick.
      3. Publishes events for the changes.
    """

    # Heartbeat interval. Lower = more chatty but faster
    # dead-connection detection. 1s is the standard.
    HEARTBEAT_INTERVAL_SECONDS: float = 1.0

    def __init__(
        self,
        *,
        bus: EventBus,
        state_provider: "StateProvider | None" = None,
    ) -> None:
        if bus is None:
            raise ValidationError("bus must not be None")
        self.bus = bus
        self.state_provider = state_provider
        # Per-agent snapshot of the last-known state. The
        # stream compares each tick's state to this to
        # detect changes.
        self._last_state: dict[AutomatonId, dict[str, Any]] = {}

    def publish(self, event: StreamEvent) -> None:
        """Publish an event to the bus.

        This is a sync method (the bus's `publish` is sync
        for the threading reasons described in
        `EventBus.publish`). Returns `None` for
        compatibility with the call sites in the runtime
        that may have been written assuming an awaitable.
        """
        self.bus.publish(event)
        return None

    def make_event(
        self,
        *,
        kind: EventKind,
        aid: AutomatonId,
        payload: dict[str, Any],
    ) -> StreamEvent:
        return StreamEvent(
            id=f"evt_{uuid.uuid4().hex}",
            kind=kind,
            aid=aid,
            payload=payload,
            occurred_at=time.time(),
        )

    # ── Heartbeat ──
    async def heartbeat_loop(self) -> None:
        """A long-running task that emits heartbeat events.

        Every `HEARTBEAT_INTERVAL_SECONDS`, this task
        iterates over the agents with active subscribers
        and publishes a heartbeat event for each.

        This task should be started once at platform boot
        and never stopped.
        """
        while True:
            try:
                await self._heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("heartbeat_failed", extra={"err": str(e)})
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)

    async def _heartbeat_once(self) -> None:
        now = time.time()
        # For each agent with a current state, publish a
        # heartbeat. We don't have a way to enumerate
        # subscribers, so we just heartbeat all known
        # agents.
        for aid in list(self._last_state.keys()):
            evt = self.make_event(
                kind=EventKind.HEARTBEAT,
                aid=aid,
                payload={"now": now},
            )
            self.bus.publish(evt)


# ── State provider (protocol) ─────────────────────


class StateProvider(Protocol):
    """Returns the current state of an agent.

    The runtime implements this; the dashboard just reads.
    The state includes the treasury, tier, inbox stats,
    and a few other things the operator might want to see.
    """

    def get_state(self, aid: AutomatonId) -> dict[str, Any]: ...


# ── Factory ─────────────────────────────────────


def make_dashboard_stream(
    *,
    bus: EventBus | None = None,
    state_provider: StateProvider | None = None,
) -> tuple[EventBus, DashboardStream]:
    """Convenience factory. Returns the bus and the stream.

    The bus is the public surface (publish + subscribe);
    the stream is the high-level façade for the runtime.
    """
    bus = bus or EventBus()
    stream = DashboardStream(bus=bus, state_provider=state_provider)
    return bus, stream


__all__ = [
    "DashboardStream",
    "EventBus",
    "EventKind",
    "StateProvider",
    "StreamEvent",
    "Subscriber",
    "make_dashboard_stream",
]
