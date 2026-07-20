"""
Event types and a tiny in-process pub/sub bus.

In production, the same `Event` payloads are published to NATS/Kafka via an
adapter. The bus abstraction is kept minimal so tests can use the in-memory
implementation.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Event:
    topic: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    occurred_at: float = field(default_factory=time.time)
    causation_id: str | None = None
    correlation_id: str | None = None

    def encode(self) -> bytes:
        return json.dumps(
            {
                "id": self.id,
                "topic": self.topic,
                "payload": self.payload,
                "occurred_at": self.occurred_at,
                "causation_id": self.causation_id,
                "correlation_id": self.correlation_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

    @classmethod
    def decode(cls, raw: bytes) -> "Event":
        d = json.loads(raw)
        return cls(
            id=d["id"],
            topic=d["topic"],
            payload=d["payload"],
            occurred_at=d["occurred_at"],
            causation_id=d.get("causation_id"),
            correlation_id=d.get("correlation_id"),
        )


SubscriberFn = Callable[[Event], "asyncio.Future[None] | None"]


class InMemoryBus:
    """Topic-based fan-out. Suitable for tests and single-process runtime."""

    def __init__(self) -> None:
        self._subs: dict[str, list[SubscriberFn]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        # Snapshot subscribers to avoid mutation during dispatch.
        async with self._lock:
            subs = list(self._subs.get(event.topic, [])) + list(
                self._subs.get("*", [])
            )
        for sub in subs:
            res = sub(event)
            if asyncio.iscoroutine(res):
                await res

    async def subscribe(self, topic: str, fn: SubscriberFn) -> None:
        async with self._lock:
            self._subs.setdefault(topic, []).append(fn)

    async def unsubscribe(self, topic: str, fn: SubscriberFn) -> None:
        async with self._lock:
            if fn in self._subs.get(topic, []):
                self._subs[topic].remove(fn)

    async def stream(self, topic: str) -> AsyncIterator[Event]:
        """Async generator yielding all events on a topic until cancelled."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1024)

        async def _put(e: Event) -> None:
            await queue.put(e)

        await self.subscribe(topic, _put)
        try:
            while True:
                yield await queue.get()
        finally:
            await self.unsubscribe(topic, _put)
