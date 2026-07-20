"""Audit routes: read the append-only audit log."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter()

# In-memory audit log for the embedded control plane. In production this is
# the audit service backed by Postgres + the `audit_log` table.
_LOG: list[dict[str, Any]] = []


def record(entry: dict[str, Any]) -> None:
    _LOG.append(entry)


@router.get("/log")
def read_log(limit: int = 100, automaton: str | None = None) -> list[dict[str, Any]]:
    items = list(_LOG)
    if automaton:
        items = [e for e in items if e.get("automaton_id") == automaton]
    return items[-limit:]


@router.get("/verify")
def verify() -> dict[str, Any]:
    """Walk the chain and verify the hash links."""
    prev = None
    for i, e in enumerate(_LOG):
        expected = e.get("prev_hash")
        if expected != prev:
            return {"ok": False, "broken_at": i, "entry": e}
        prev = e.get("row_hash")
    return {"ok": True, "count": len(_LOG)}
