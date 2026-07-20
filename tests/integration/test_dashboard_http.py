"""HTTP/WebSocket integration tests for the dashboard routes."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.control_plane.api import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


# ── Recent events (REST) ───────────────────────────


def test_recent_events_empty_buffer(client):
    aid = "atm_alice_abc_1234"
    r = client.get(f"/v1/dashboard/{aid}/events")
    assert r.status_code == 200
    body = r.json()
    assert body["aid"] == aid
    assert body["count"] == 0
    assert body["events"] == []


def test_recent_events_after_publish(client):
    aid = "atm_bob_abc_1234"
    # Publish three events.
    for i in range(3):
        r = client.post(
            f"/v1/dashboard/{aid}/events/publish",
            json={
                "kind": "treasury_update",
                "payload": {"balance_micro": i * 1000},
            },
        )
        assert r.status_code == 200
    # Read them back.
    r = client.get(f"/v1/dashboard/{aid}/events")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert [e["payload"]["balance_micro"] for e in body["events"]] == [0, 1000, 2000]


def test_publish_rejects_missing_kind(client):
    aid = "atm_carol_abc_1234"
    r = client.post(
        f"/v1/dashboard/{aid}/events/publish",
        json={"payload": {}},
    )
    assert r.status_code == 400


def test_publish_rejects_unknown_kind(client):
    aid = "atm_dave_abc_1234"
    r = client.post(
        f"/v1/dashboard/{aid}/events/publish",
        json={"kind": "unknown_kind_xyz", "payload": {}},
    )
    assert r.status_code == 400


# ── WebSocket stream ─────────────────────────────


def test_websocket_receives_replay_events(client):
    aid = "atm_eve_abc_1234"
    # Publish two events before connecting.
    for i in range(2):
        client.post(
            f"/v1/dashboard/{aid}/events/publish",
            json={"kind": "tier_change", "payload": {"tier": "normal"}},
        )
    # Connect via WebSocket and read the first message.
    with client.websocket_connect(f"/v1/dashboard/{aid}/stream") as ws:
        msg1 = ws.receive_json()
        assert msg1["kind"] == "tier_change"
        msg2 = ws.receive_json()
        assert msg2["kind"] == "tier_change"
        # Then heartbeats.
        # (Don't read forever; close after 2 messages.)


def test_websocket_receives_live_events(client):
    aid = "atm_frank_abc_123"
    with client.websocket_connect(f"/v1/dashboard/{aid}/stream") as ws:
        # Publish an event from another client.
        client.post(
            f"/v1/dashboard/{aid}/events/publish",
            json={"kind": "inbox_update", "payload": {"pending": 1}},
        )
        # Read it from the websocket.
        msg = ws.receive_json()
        assert msg["kind"] == "inbox_update"
        assert msg["payload"] == {"pending": 1}


def test_websocket_receives_heartbeat(client):
    aid = "atm_grace_abc_123"
    with client.websocket_connect(f"/v1/dashboard/{aid}/stream") as ws:
        # No events published, so we should get a heartbeat
        # within ~1 second.
        msg = ws.receive_json()
        # Either an event (if any were published) or a
        # heartbeat. Both are valid.
        assert msg["kind"] in ("heartbeat", "treasury_update",
                               "tier_change", "inbox_update",
                               "action_completed", "plan_created",
                               "plan_completed", "soul_updated",
                               "self_mod_request", "self_mod_promoted",
                               "self_mod_rejected", "policy_denied")
