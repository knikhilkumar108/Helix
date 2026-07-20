"""
Audit service. Append-only, hash-chained, externally verifiable.

The on-disk format matches the `audit_log` table in `storage/postgres/migrations`.
For the embedded control plane we keep the chain in memory; the same logic
applies — the chain is a sequence of rows where each row's `row_hash` is the
sha-256 of (prev_row_hash || canonical(row_payload)).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core.security.signing import KeyPair, canonical_json, sha256


@dataclass(slots=True)
class AuditEntry:
    id: str
    occurred_at: float
    tenant_id: str | None
    automaton_id: str | None
    user_id: str | None
    actor_kind: str
    action: str
    target_kind: str | None
    target_id: str | None
    request_id: str | None
    correlation_id: str | None
    payload: dict[str, Any]
    prev_hash: str
    row_hash: str
    signature: str = ""


class AuditLog:
    def __init__(self, signing_key: KeyPair | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: list[AuditEntry] = []
        self._signing = signing_key or KeyPair.generate()

    def append(
        self,
        *,
        actor_kind: str,
        action: str,
        tenant_id: str | None = None,
        automaton_id: str | None = None,
        user_id: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEntry:
        with self._lock:
            prev = self._entries[-1].row_hash if self._entries else ""
            occurred_at = time.time()
            eid = f"aud_{uuid.uuid4().hex}"
            payload = payload or {}
            material = (
                f"{prev}|{tenant_id or ''}|{automaton_id or ''}|{user_id or ''}|"
                f"{actor_kind}|{action}|{target_id or ''}|"
                f"{json.dumps(payload, sort_keys=True, separators=(',',':'))}|{occurred_at}"
            )
            row_hash = sha256(material.encode("utf-8"))
            entry = AuditEntry(
                id=eid,
                occurred_at=occurred_at,
                tenant_id=tenant_id,
                automaton_id=automaton_id,
                user_id=user_id,
                actor_kind=actor_kind,
                action=action,
                target_kind=target_kind,
                target_id=target_id,
                request_id=request_id,
                correlation_id=correlation_id,
                payload=payload,
                prev_hash=prev,
                row_hash=row_hash,
            )
            # Sign the row with the audit service's signing key.
            sig_body = canonical_json(
                {
                    "id": entry.id,
                    "row_hash": entry.row_hash,
                    "prev_hash": entry.prev_hash,
                }
            )
            entry.signature = self._signing.sign(sig_body).hex()
            self._entries.append(entry)
            return entry

    def query(
        self,
        *,
        automaton_id: str | None = None,
        actor_kind: str | None = None,
        action: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        with self._lock:
            out: list[AuditEntry] = []
            for e in reversed(self._entries):
                if automaton_id is not None and e.automaton_id != automaton_id:
                    continue
                if actor_kind is not None and e.actor_kind != actor_kind:
                    continue
                if action is not None and e.action != action:
                    continue
                if since is not None and e.occurred_at < since:
                    continue
                out.append(e)
                if len(out) >= limit:
                    break
            return out

    def verify(self) -> tuple[bool, str | None]:
        with self._lock:
            prev = ""
            for e in self._entries:
                if e.prev_hash != prev:
                    return False, e.id
                material = (
                    f"{prev}|{e.tenant_id or ''}|{e.automaton_id or ''}|{e.user_id or ''}|"
                    f"{e.actor_kind}|{e.action}|{e.target_id or ''}|"
                    f"{json.dumps(e.payload, sort_keys=True, separators=(',',':'))}|{e.occurred_at}"
                )
                if sha256(material.encode("utf-8")) != e.row_hash:
                    return False, e.id
                prev = e.row_hash
        return True, None
