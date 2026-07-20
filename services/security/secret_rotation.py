"""
Key rotation. Operators can rotate an Automaton's signing key on a schedule.
Old keys are kept in a verifiable history so previously signed artifacts can
still be validated.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.security.signing import KeyPair, canonical_json, sha256


@dataclass(slots=True)
class KeyHistoryEntry:
    public_key: str
    activated_at: float
    retired_at: float | None = None
    signature: str = ""  # signature over the entry by the new key


class KeyHistory:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_automaton: dict[str, list[KeyHistoryEntry]] = {}
        self._active: dict[str, str] = {}

    def activate(self, automaton_id: str, keypair: KeyPair) -> None:
        now = time.time()
        with self._lock:
            history = self._by_automaton.setdefault(automaton_id, [])
            for e in history:
                if e.retired_at is None:
                    e.retired_at = now
            entry = KeyHistoryEntry(
                public_key=keypair.public_b64(),
                activated_at=now,
            )
            # Sign the new entry with the new key (self-signed genesis).
            body = canonical_json(
                {
                    "automaton": automaton_id,
                    "public_key": entry.public_key,
                    "activated_at": entry.activated_at,
                }
            )
            entry.signature = keypair.sign(body).hex()
            history.append(entry)
            self._active[automaton_id] = entry.public_key

    def active(self, automaton_id: str) -> str | None:
        return self._active.get(automaton_id)

    def all(self, automaton_id: str) -> list[KeyHistoryEntry]:
        with self._lock:
            return list(self._by_automaton.get(automaton_id, []))

    def verify(self, automaton_id: str) -> bool:
        """Verify that every entry in the history was correctly self-signed."""
        history = self.all(automaton_id)
        for e in history:
            body = canonical_json(
                {
                    "automaton": automaton_id,
                    "public_key": e.public_key,
                    "activated_at": e.activated_at,
                }
            )
            try:
                # Self-signature verification requires the public key in the entry.
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
                import base64

                pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(e.public_key))
                pub.verify(bytes.fromhex(e.signature), body)
            except Exception:  # noqa: BLE001
                return False
        return True
