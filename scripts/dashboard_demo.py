"""
Dashboard demo — events streaming from the runtime to a
WebSocket client.

Builds a HelixTreasury-backed loop, publishes events
through the dashboard bus, and shows the WebSocket
stream receiving them in real time.

Run as:

    python scripts/dashboard_demo.py
"""
from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from core.types.identifiers import new_automaton_id
from services.control_plane.api import create_app
from services.dashboard import EventKind, StreamEvent
from services.treasury.helix_treasury import (
    HelixTreasury,
    MockBackend,
    TopupPolicy,
    TopupTrigger,
)


def main() -> int:
    print("=" * 70)
    print("  Dashboard demo — events streaming to WebSocket")
    print("=" * 70)
    print()

    aid = new_automaton_id()
    print(f"Agent id: {aid}")
    print()

    # Build the control plane (which has the dashboard bus).
    app = create_app()
    bus = app.state.dashboard_bus

    # Wire a HelixTreasury so the agent can top up.
    backend = MockBackend(initial_usdc_micro=10_000_000)
    policy = TopupPolicy(
        trigger=TopupTrigger.ALWAYS,
        threshold_micro=0,
        target_micro=500_000,
    )
    helix = HelixTreasury(backend, aid, policy=policy)

    def make_event(kind: EventKind, payload: dict) -> StreamEvent:
        return StreamEvent(
            id=f"evt_{uuid.uuid4().hex[:8]}",
            kind=kind,
            aid=aid,
            payload=payload,
            occurred_at=time.time(),
        )

    # Step 1: Publish a treasury update.
    print("Step 1: publishing a treasury_update event")
    print("-" * 70)
    bus.publish(make_event(
        EventKind.TREASURY_UPDATE,
        {"balance_micro": 5_000_000, "kind": "initial"},
    ))
    print("  → published")
    print()

    # Step 2: Read events via REST.
    print("Step 2: reading events via REST")
    print("-" * 70)
    with TestClient(app) as client:
        r = client.get(f"/v1/dashboard/{aid}/events")
        body = r.json()
        print(f"  → {body['count']} event(s) in the replay buffer")
        for event in body["events"]:
            print(f"    - {event['kind']}: {event['payload']}")
    print()

    # Step 3: Stream events via WebSocket.
    print("Step 3: streaming events via WebSocket")
    print("-" * 70)
    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/dashboard/{aid}/stream") as ws:
            # Publish a few more events from a side thread.
            def publisher():
                time.sleep(0.1)
                for i in range(3):
                    bus.publish(make_event(
                        EventKind.ACTION_COMPLETED,
                        {"tool": f"tool_{i}", "ok": True},
                    ))
                    time.sleep(0.05)
            t = threading.Thread(target=publisher, daemon=True)
            t.start()
            # Read a few messages.
            for _ in range(5):
                msg = ws.receive_json()
                kind = msg["kind"]
                payload = msg["payload"]
                if kind == "heartbeat":
                    print(f"  ← heartbeat (alive)")
                else:
                    print(f"  ← {kind}: {payload}")
    print()

    # Step 4: Show the final state.
    print("Step 4: final stats")
    print("-" * 70)
    print(f"  Replay buffer size for {aid}: {len(bus._replay.get(aid, []))}")
    print(f"  Treasury address: {backend.address()[:20]}…")
    print(f"  Helix balance: {helix.credit_balance_micro} micro-USDC")
    print()

    print("=" * 70)
    print("  Demo complete — dashboard events stream end-to-end")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
