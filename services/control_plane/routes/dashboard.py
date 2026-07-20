"""HTTP / WebSocket routes for the operator dashboard.

Exposes:

  - `GET  /v1/dashboard/{aid}/events` — recent events from
    the replay buffer (REST, for clients that don't want
    WebSocket).
  - `WS   /v1/dashboard/{aid}/stream` — the real-time stream.
    On connect, sends the last N events from the replay
    buffer; then sends every new event as it arrives.
  - `POST /v1/dashboard/{aid}/events/publish` — publish an
    event (used by components that don't have a direct
    handle on the bus).

The bus is the *event source*. The control plane wires it
once at startup; all routes share the same bus.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from core.types.identifiers import AutomatonId
from services.dashboard import EventBus, EventKind, StreamEvent

log = logging.getLogger(__name__)

router = APIRouter()


# ── DI helpers ─────────────────────────────────────


def get_event_bus(request: Request) -> EventBus | None:
    return getattr(request.app.state, "dashboard_bus", None)


# ── REST: recent events ────────────────────────────


@router.get("/{aid}/events")
def recent_events(
    aid: str,
    request: Request,
    limit: int = 50,
) -> dict[str, Any]:
    """Return the most recent events for an agent.

    The events come from the bus's replay buffer. If the
    bus is empty, the agent is unknown or no events have
    been emitted yet.
    """
    bus = get_event_bus(request)
    if bus is None:
        raise HTTPException(
            status_code=503,
            detail="dashboard bus not configured on the control plane",
        )
    aid_obj = AutomatonId(aid)
    # `EventBus._replay` is the in-memory buffer of the
    # last 100 events per agent. We expose it directly
    # here; the bus doesn't have a public read accessor
    # because the replay is a debug/inspection tool,
    # not a production API.
    events = list(bus._replay.get(aid_obj, []))  # noqa: SLF001
    return {
        "aid": aid,
        "count": min(limit, len(events)),
        "events": [e.to_dict() for e in events[-limit:]],
    }


# ── WebSocket: real-time stream ────────────────────


@router.websocket("/{aid}/stream")
async def stream(websocket: WebSocket, aid: str) -> None:
    """Real-time event stream for an agent.

    Lifecycle:
      1. Client connects.
      2. Server sends the last N events from the replay
         buffer (so the client doesn't miss what happened
         before it connected).
      3. Server sends a 1Hz heartbeat to detect dead
         connections.
      4. Server streams every new event as it arrives.
      5. Client disconnects; server cleans up the
         subscriber.
    """
    # A `WebSocket` doesn't expose `.app` directly. We use
    # the global app via the request state. The cleaner
    # path is to look up the bus via the WebSocket's scope.
    bus = websocket.app.state.dashboard_bus if hasattr(websocket, "app") else None
    if bus is None:
        # Fall back: parse the scope to find the app.
        # (FastAPI's WebSocket doesn't have a clean `.app`
        # attribute, so we look at the scope's app.)
        app = websocket.scope.get("app")
        if app is not None:
            bus = getattr(app.state, "dashboard_bus", None)
    if bus is None:
        # We have to accept before we can close; close
        # immediately after.
        await websocket.accept()
        await websocket.close(
            code=1011,
            reason="dashboard bus not configured",
        )
        return
    await websocket.accept()
    aid_obj = AutomatonId(aid)
    replay, subscriber = bus.subscribe(aid_obj)
    try:
        # 1. Send the replay buffer.
        for event in replay:
            await websocket.send_json(event.to_dict())
        # 2. Stream new events.
        while True:
            try:
                # Wait for an event with a small timeout so
                # we can interleave heartbeats.
                event = await asyncio.wait_for(
                    subscriber.queue.get(), timeout=1.0
                )
                await websocket.send_json(event.to_dict())
            except asyncio.TimeoutError:
                # No new event; send a heartbeat.
                loop = asyncio.get_event_loop()
                now = loop.time()
                hb = StreamEvent(
                    id=f"hb_{aid}_{now}",
                    kind=EventKind.HEARTBEAT,
                    aid=aid_obj,
                    payload={"now": now},
                    occurred_at=now,
                )
                await websocket.send_json(hb.to_dict())
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.exception("dashboard_stream_error", extra={"err": str(e)})
    finally:
        subscriber.close()


# ── REST: publish an event ─────────────────────────


@router.post("/{aid}/events/publish")
async def publish_event(
    aid: str,
    request: Request,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Publish an event to the dashboard bus.

    Used by components that don't have a direct handle
    on the bus. A real platform would have typed schemas
    per event kind; this is a simple JSON pass-through.
    """
    bus = get_event_bus(request)
    if bus is None:
        raise HTTPException(
            status_code=503,
            detail="dashboard bus not configured on the control plane",
        )
    kind_str = body.get("kind")
    if not kind_str:
        raise HTTPException(status_code=400, detail="missing 'kind'")
    try:
        kind = EventKind(kind_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind: {kind_str!r}",
        )
    aid_obj = AutomatonId(aid)
    payload = body.get("payload", {})
    import time as _time
    event = StreamEvent(
        id=f"evt_{_time.time_ns()}",
        kind=kind,
        aid=aid_obj,
        payload=payload,
        occurred_at=_time.time(),
    )
    bus.publish(event)
    return {"id": event.id, "kind": kind.value}
