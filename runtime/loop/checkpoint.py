"""
Checkpoint store. Snapshots are written to:
  - Postgres (durable, primary)
  - Object store (encrypted, for disaster recovery)
"""
from __future__ import annotations

import abc
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from core.types.identifiers import AutomatonId


@dataclass(slots=True)
class Checkpoint:
    automaton_id: AutomatonId
    seq: int
    payload: dict[str, Any]
    sha256: str
    created_at: float


class CheckpointStore(abc.ABC):
    @abc.abstractmethod
    async def save(self, automaton_id: AutomatonId, payload: dict[str, Any]) -> Checkpoint: ...

    @abc.abstractmethod
    async def latest(self, automaton_id: AutomatonId) -> Checkpoint | None: ...

    @abc.abstractmethod
    async def history(self, automaton_id: AutomatonId, *, limit: int = 50) -> list[Checkpoint]: ...


def _hash(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


class InMemoryCheckpointStore(CheckpointStore):
    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self._store: dict[str, list[Checkpoint]] = {}

    async def save(self, automaton_id: AutomatonId, payload: dict[str, Any]) -> Checkpoint:
        key = str(automaton_id)
        seq = self._seq.get(key, 0) + 1
        self._seq[key] = seq
        cp = Checkpoint(
            automaton_id=automaton_id,
            seq=seq,
            payload=payload,
            sha256=_hash(payload),
            created_at=time.time(),
        )
        self._store.setdefault(key, []).append(cp)
        return cp

    async def latest(self, automaton_id: AutomatonId) -> Checkpoint | None:
        items = self._store.get(str(automaton_id), [])
        return items[-1] if items else None

    async def history(self, automaton_id: AutomatonId, *, limit: int = 50) -> list[Checkpoint]:
        items = self._store.get(str(automaton_id), [])
        return items[-limit:]
