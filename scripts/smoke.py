"""
End-to-end smoke test.

Exercises the full control plane + a runtime worker without external
dependencies. Run as:

    python scripts/smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import threading
import urllib.request
from pathlib import Path

# Make the project root importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Quiet down the test logger.
os.environ.setdefault("AUTOMATA_LOG_LEVEL", "WARNING")


def _wait_for_server(url: str, timeout: float = 10.0) -> None:
    started = time.time()
    while time.time() - started < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:  # noqa: S310
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    raise SystemExit("server did not come up")


def main() -> int:
    # Start the control plane in a background thread so the test is self-contained.
    import uvicorn

    from services.control_plane.api import create_app

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=8088, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    _wait_for_server("http://127.0.0.1:8088/healthz")

    base = "http://127.0.0.1:8088"

    def get(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=5) as r:  # noqa: S310
            import json
            return json.loads(r.read())

    def post(path: str, body: dict) -> dict:
        import json
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return json.loads(r.read())

    # 1) health
    h = get("/healthz")
    assert h["status"] == "healthy", h

    # 2) create seller + buyer
    seller = post("/v1/automata", {"name": "seller", "genesis_prompt": "x"})
    buyer = post(
        "/v1/automata",
        {"name": "buyer", "genesis_prompt": "y", "initial_balance_micro": 10_000_000},
    )
    assert seller["id"] != buyer["id"]

    # 3) fund the buyer
    post(
        f"/v1/treasury/{buyer['id']}/fund",
        {"automaton_id": buyer["id"], "amount_micro": 5_000_000, "currency": "USDC"},
    )

    # 4) balance reflects the sum
    bal = get(f"/v1/treasury/{buyer['id']}/balance")
    assert "15.000000" in bal["balance"], bal

    # 5) marketplace round trip
    offer = post(
        "/v1/marketplace/offers",
        {
            "seller_id": seller["id"],
            "kind": "analysis",
            "title": "t",
            "description": "d",
            "price_micro": 1_000_000,
            "currency": "USDC",
        },
    )
    order = post(
        "/v1/marketplace/orders",
        {"offer_id": offer["id"], "buyer_id": buyer["id"]},
    )
    assert order["status"] == "created"

    # 6) memory write + search
    post(
        "/v1/memory",
        {
            "automaton_id": buyer["id"],
            "layer": "long_term",
            "content": "the user prefers Postgres for storage",
            "importance": 0.9,
            "tags": ["preference"],
        },
    )
    res = get(f"/v1/memory/{buyer['id']}/search?query=postgres&k=3")
    assert any("Postgres" in m["content"] for m in res), res

    # 7) lifecycle
    post(f"/v1/automata/{buyer['id']}/pause", {})
    a = get(f"/v1/automata/{buyer['id']}")
    assert a["state"] == "paused"
    post(f"/v1/automata/{buyer['id']}/resume", {})
    a = get(f"/v1/automata/{buyer['id']}")
    assert a["state"] == "running"

    # 8) ledger
    ledger = get(f"/v1/treasury/{buyer['id']}/ledger?limit=10")
    assert len(ledger) >= 1
    assert all("amount" in e for e in ledger)

    # 9) audit
    audit = get("/v1/audit/verify")
    assert audit["ok"] is True

    # 10) run the runtime for a few ticks against this automaton
    from runtime.loop.loop_init import build_default_loop
    from core.types.identifiers import AutomatonId
    from core.types.money import Money

    loop = build_default_loop(
        AutomatonId(buyer["id"]),
        initial_balance=Money.from_major("0.50"),
    )
    loop.request_stop()
    asyncio.run(loop.run())
    snap = loop.snapshot()
    assert snap["state"] in ("stopped", "running"), snap
    assert "stats" in snap

    # Cleanup
    server.should_exit = True
    t.join(timeout=5)
    print("OK — all smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
