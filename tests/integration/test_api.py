"""Integration tests for the control plane REST API.

These use FastAPI's TestClient to exercise the full request/response cycle
without external services.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.control_plane.api import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert "components" in body


def test_create_and_get_automaton(client):
    r = client.post(
        "/v1/automata",
        json={"name": "alice", "genesis_prompt": "be helpful"},
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    r2 = client.get(f"/v1/automata/{aid}")
    assert r2.status_code == 200
    assert r2.json()["name"] == "alice"


def test_fund_and_balance(client):
    r = client.post(
        "/v1/automata",
        json={"name": "bob", "genesis_prompt": "x"},
    )
    aid = r.json()["id"]
    r2 = client.post(
        f"/v1/treasury/{aid}/fund",
        json={"automaton_id": aid, "amount_micro": 1_000_000, "currency": "USDC"},
    )
    assert r2.status_code == 200
    r3 = client.get(f"/v1/treasury/{aid}/balance")
    assert "1.000000" in r3.json()["balance"]


def test_pause_resume_terminate(client):
    aid = client.post("/v1/automata", json={"name": "x", "genesis_prompt": "y"}).json()["id"]
    r = client.post(f"/v1/automata/{aid}/pause")
    assert r.json()["state"] == "paused"
    r = client.post(f"/v1/automata/{aid}/resume")
    assert r.json()["state"] == "running"
    r = client.post(f"/v1/automata/{aid}/terminate")
    assert r.json()["state"] == "terminated"


def test_marketplace_offer_and_order(client):
    seller = client.post("/v1/automata", json={"name": "s", "genesis_prompt": "x"}).json()["id"]
    buyer = client.post("/v1/automata", json={"name": "b", "genesis_prompt": "x"}).json()["id"]
    r = client.post(
        "/v1/marketplace/offers",
        json={
            "seller_id": seller,
            "kind": "analysis",
            "title": "t",
            "description": "d",
            "price_micro": 1_000_000,
            "currency": "USDC",
        },
    )
    assert r.status_code == 201
    oid = r.json()["id"]
    r = client.post("/v1/marketplace/orders", json={"offer_id": oid, "buyer_id": buyer})
    assert r.status_code == 201
    assert r.json()["seller_id"] == seller


def test_audit_chain_starts_valid(client):
    # Even without explicit appends, the verify endpoint should report ok.
    r = client.get("/v1/audit/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
