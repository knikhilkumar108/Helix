"""Tests for the operator dashboard event bus and stream."""
from __future__ import annotations

import asyncio

import pytest

from core.types.identifiers import new_automaton_id
from services.dashboard import (
    DashboardStream,
    EventBus,
    EventKind,
    StreamEvent,
    Subscriber,
    make_dashboard_stream,
)


# ── EventBus ─────────────────────────────────────


@pytest.mark.asyncio
async def test_bus_publishes_to_subscriber():
    bus = EventBus()
    aid = new_automaton_id()
    replay, sub = bus.subscribe(aid)
    assert replay == []  # no events yet
    evt = StreamEvent(
        id="evt_x",
        kind=EventKind.HEARTBEAT,
        aid=aid,
        payload={"now": 0.0},
        occurred_at=0.0,
    )
    bus.publish(evt)
    received = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert received.id == "evt_x"


@pytest.mark.asyncio
async def test_bus_replay_buffer_captures_recent_events():
    bus = EventBus()
    aid = new_automaton_id()
    # Publish 3 events before subscribing.
    for i in range(3):
        bus.publish(
            StreamEvent(
                id=f"evt_{i}",
                kind=EventKind.HEARTBEAT,
                aid=aid,
                payload={"i": i},
                occurred_at=float(i),
            )
        )
    # A new subscriber should get all 3 in the replay.
    replay, _ = bus.subscribe(aid)
    assert len(replay) == 3
    assert [e.id for e in replay] == ["evt_0", "evt_1", "evt_2"]


@pytest.mark.asyncio
async def test_bus_does_not_cross_subscribers_across_agents():
    bus = EventBus()
    aid_a = new_automaton_id()
    aid_b = new_automaton_id()
    _, sub_a = bus.subscribe(aid_a)
    _, sub_b = bus.subscribe(aid_b)
    # Publish to A only.
    bus.publish(
        StreamEvent(
            id="evt_a",
            kind=EventKind.HEARTBEAT,
            aid=aid_a,
            payload={},
            occurred_at=0.0,
        )
    )
    # A gets it; B doesn't.
    a_evt = await asyncio.wait_for(sub_a.queue.get(), timeout=1.0)
    assert a_evt.id == "evt_a"
    # B's queue is empty.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub_b.queue.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_bus_unsubscribe_stops_delivery():
    bus = EventBus()
    aid = new_automaton_id()
    _, sub = bus.subscribe(aid)
    sub.close()
    # After unsubscribe, the bus still has the (now
    # closed) queue registered; we just check the
    # subscriber set is empty.
    assert sub.queue not in bus._subscribers[aid]


@pytest.mark.asyncio
async def test_bus_drops_events_when_subscriber_is_slow():
    """A subscriber with a full queue drops events but
    the replay buffer still has them."""
    bus = EventBus()
    aid = new_automaton_id()
    # Use a small queue for the test by directly creating
    # a subscriber.
    sub = Subscriber(bus=bus, aid=aid)
    sub.queue = asyncio.Queue(maxsize=2)  # tiny
    with bus._lock:
        bus._subscribers[aid].add(sub.queue)
    # Publish 5 events; only 2 fit in the queue.
    for i in range(5):
        bus.publish(
            StreamEvent(
                id=f"evt_{i}",
                kind=EventKind.HEARTBEAT,
                aid=aid,
                payload={},
                occurred_at=0.0,
            )
        )
    # Drain the queue.
    received = []
    while not sub.queue.empty():
        received.append(sub.queue.get_nowait().id)
    # Only 2 made it.
    assert len(received) == 2
    # But the replay buffer has all 5.
    assert len(bus._replay[aid]) == 5


# ── StreamEvent ───────────────────────────────────


def test_stream_event_to_dict():
    aid = new_automaton_id()
    e = StreamEvent(
        id="evt_x",
        kind=EventKind.TREASURY_UPDATE,
        aid=aid,
        payload={"balance_micro": 1000},
        occurred_at=0.0,
    )
    d = e.to_dict()
    assert d["id"] == "evt_x"
    assert d["kind"] == "treasury_update"
    assert d["aid"] == str(aid)
    assert d["payload"] == {"balance_micro": 1000}


# ── DashboardStream ─────────────────────────────


def test_dashboard_stream_rejects_none_bus():
    with pytest.raises(Exception):
        DashboardStream(bus=None)


def test_dashboard_stream_make_event():
    bus = EventBus()
    stream = DashboardStream(bus=bus)
    aid = new_automaton_id()
    e = stream.make_event(
        kind=EventKind.HEARTBEAT,
        aid=aid,
        payload={"x": 1},
    )
    assert e.kind == EventKind.HEARTBEAT
    assert e.aid == aid
    assert e.payload == {"x": 1}
    assert e.id.startswith("evt_")


@pytest.mark.asyncio
async def test_dashboard_stream_heartbeat_emits_event():
    bus = EventBus()
    stream = DashboardStream(bus=bus)
    aid = new_automaton_id()
    # Seed the last-state with this agent so heartbeat
    # picks it up.
    stream._last_state[aid] = {"balance_micro": 0}
    await stream._heartbeat_once()
    # The bus has one event for this agent.
    assert len(bus._replay[aid]) == 1
    assert bus._replay[aid][0].kind == EventKind.HEARTBEAT


@pytest.mark.asyncio
async def test_dashboard_stream_publish_returns_task():
    bus = EventBus()
    stream = DashboardStream(bus=bus)
    aid = new_automaton_id()
    evt = stream.make_event(
        kind=EventKind.HEARTBEAT, aid=aid, payload={}
    )
    result = stream.publish(evt)
    # Publish is sync now; it returns None for backwards
    # compatibility with call sites that expected a task.
    assert result is None
    assert len(bus._replay[aid]) == 1


# ── make_dashboard_stream factory ─────────────


def test_make_dashboard_stream_returns_bus_and_stream():
    bus, stream = make_dashboard_stream()
    assert isinstance(bus, EventBus)
    assert isinstance(stream, DashboardStream)
    assert stream.bus is bus


def test_make_dashboard_stream_uses_provided_bus():
    existing = EventBus()
    bus, stream = make_dashboard_stream(bus=existing)
    assert bus is existing
