"""HTTP integration tests for the inbox routes."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.control_plane.api import create_app
from services.messaging import InboxService
from services.state.sqlite_store import SqliteStore


@pytest.fixture
def client_with_inbox():
    # Use a file-backed store, not `:memory:`. SQLite's
    # `:memory:` databases are per-connection, and FastAPI's
    # request handlers run on a different thread from the
    # test, so two separate in-memory connections would see
    # two separate empty databases. A file path lets the
    # schema and data be shared across threads.
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    try:
        app = create_app()
        store = SqliteStore(db_path)
        inbox = InboxService(backend=store)
        app.state.inbox_service = inbox
        with TestClient(app) as c:
            c.app = app  # expose for tests
            yield c, inbox
    finally:
        db_path.unlink(missing_ok=True)


@pytest.fixture
def client_no_inbox():
    app = create_app()
    with TestClient(app) as c:
        yield c


# ── With inbox configured ─────────────────────────────────


def test_send_message_enqueues(client_with_inbox):
    client, inbox = client_with_inbox
    aid = "atm_alice_abc_1234"
    r = client.post(
        "/v1/inbox/send",
        json={
            "from_address": "atm_bob_abc_1234",
            "to_address": aid,
            "content": "hello",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "received"
    assert body["to"] == aid
    assert body["from"] == "atm_bob_abc_1234"
    assert body["id"].startswith("msg_")
    # The message is actually in the inbox.
    msgs = inbox.peek(aid, limit=10)
    assert len(msgs) == 1


def test_list_messages(client_with_inbox):
    client, inbox = client_with_inbox
    aid = "atm_carol_abc_1234"
    inbox.send(from_address="atm_a_aaaaaaaaa", to_address=aid, content="1")
    inbox.send(from_address="atm_b_bbbbbbbbb", to_address=aid, content="2")
    r = client.get(f"/v1/inbox/{aid}/messages")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert {m["content"] for m in body} == {"1", "2"}


def test_list_messages_filtered_by_state(client_with_inbox):
    client, inbox = client_with_inbox
    aid = "atm_dave_abc_1234"
    msg1_id = inbox.send(
        from_address="atm_aaaaaaaaaa", to_address=aid, content="first"
    ).id
    inbox.send(from_address="atm_bbbbbbbbbb", to_address=aid, content="second")
    # Claim one — the earliest by `created_at`.
    inbox.claim(aid, limit=1)
    r = client.get(f"/v1/inbox/{aid}/messages?state=in_progress")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == msg1_id
    # Filter by `received` should show the unclaimed one.
    r2 = client.get(f"/v1/inbox/{aid}/messages?state=received")
    assert r2.status_code == 200
    assert len(r2.json()) == 1
    assert r2.json()[0]["content"] == "second"


def test_inbox_stats(client_with_inbox):
    client, inbox = client_with_inbox
    aid = "atm_eve_abc_1234"
    inbox.send(from_address="atm_aaaaaaaaaa", to_address=aid, content="x")
    inbox.send(from_address="atm_bbbbbbbbbb", to_address=aid, content="y")
    r = client.get(f"/v1/inbox/{aid}/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["received"] == 2
    assert body["in_progress"] == 0
    assert body["cap"] == 1000


def test_send_message_validates_payload(client_with_inbox):
    client, _ = client_with_inbox
    r = client.post(
        "/v1/inbox/send",
        json={"from_address": "", "to_address": "atm_x_aaaaaaaaa", "content": "x"},
    )
    assert r.status_code == 422  # pydantic validation


# ── Without inbox configured ──────────────────────────────


def test_list_messages_without_inbox_returns_503(client_no_inbox):
    r = client_no_inbox.get("/v1/inbox/atm_alice_abc_1234/messages")
    assert r.status_code == 503


def test_send_without_inbox_returns_503(client_no_inbox):
    r = client_no_inbox.post(
        "/v1/inbox/send",
        json={
            "from_address": "atm_alice_abc_1234",
            "to_address": "atm_bob_abc_1234",
            "content": "x",
        },
    )
    assert r.status_code == 503


def test_stats_without_inbox_returns_503(client_no_inbox):
    r = client_no_inbox.get("/v1/inbox/atm_alice_abc_1234/stats")
    assert r.status_code == 503
