"""
Memory service. Provides retrieval, summarization, pruning, indexing and
versioning for the multiple memory layers.

In production this is backed by Postgres + pgvector for semantic memory
and a separate store for working/short-term memory (Redis streams).

The interface is small on purpose so the runtime doesn't bind to a specific
storage choice.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.errors.errors import NotFoundError
from core.types.automaton import MemoryEntry, MemoryLayer
from core.types.identifiers import AutomatonId, MemoryId


# Conservative token counter (~4 chars per token for English).
def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MemoryService:
    """In-process memory service. Threadsafe via a single lock.

    Storage shape (per automaton):
      by_id : dict[MemoryId, MemoryEntry]
      by_layer : dict[MemoryLayer, list[MemoryId]]
      index : dict[token, set[MemoryId]]  (TF-style inverted index)
      stats : dict[MemoryLayer, dict[MemoryId, float]]  (importance decay)
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.RLock()
        self._by_id: dict[tuple[AutomatonId, MemoryId], MemoryEntry] = {}
        self._by_layer: dict[tuple[AutomatonId, MemoryLayer], list[MemoryId]] = {}
        self._index: dict[tuple[AutomatonId, str], set[MemoryId]] = {}
        self._tf: dict[tuple[AutomatonId, MemoryId], dict[str, int]] = {}
        self._df: dict[tuple[AutomatonId, str], int] = {}
        self._version: dict[AutomatonId, int] = {}

    # ---- writes ----
    def write(
        self,
        automaton: AutomatonId,
        *,
        layer: MemoryLayer,
        content: str,
        importance: float = 0.5,
        tags: Iterable[str] = (),
        embedding: list[float] | None = None,
        ttl_seconds: int | None = None,
    ) -> MemoryEntry:
        from core.types.identifiers import MemoryId as _Mid

        mid = _Mid(f"mem_{uuid.uuid4().hex}")
        now = datetime.now(tz=timezone.utc)
        entry = MemoryEntry(
            id=mid,
            automaton_id=automaton,
            layer=layer,
            content=content,
            embedding=embedding,
            importance=max(0.0, min(1.0, importance)),
            ttl=ttl_seconds,
            created_at=now,
            updated_at=now,
            tags=list(tags),
        )
        with self._lock:
            self._by_id[(automaton, mid)] = entry
            self._by_layer.setdefault((automaton, layer), []).append(mid)
            tokens = _tokenize(content)
            tf: dict[str, int] = {}
            for tok in tokens:
                tf[tok] = tf.get(tok, 0) + 1
            self._tf[(automaton, mid)] = tf
            for tok in set(tokens):
                self._index.setdefault((automaton, tok), set()).add(mid)
                self._df[(automaton, tok)] = self._df.get((automaton, tok), 0) + 1
            self._version[automaton] = self._version.get(automaton, 0) + 1
        return entry

    def update(
        self, automaton: AutomatonId, mid: MemoryId, *, content: str | None = None, importance: float | None = None
    ) -> MemoryEntry:
        with self._lock:
            e = self._by_id.get((automaton, mid))
            if e is None:
                raise NotFoundError("memory entry not found")
            new_content = content if content is not None else e.content
            new_imp = importance if importance is not None else e.importance
            # Re-index if content changed.
            if content is not None and content != e.content:
                old_tokens = set(self._tf.get((automaton, mid), {}).keys())
                for tok in old_tokens:
                    s = self._index.get((automaton, tok))
                    if s and mid in s:
                        s.discard(mid)
                tokens = _tokenize(new_content)
                tf: dict[str, int] = {}
                for tok in tokens:
                    tf[tok] = tf.get(tok, 0) + 1
                self._tf[(automaton, mid)] = tf
                for tok in set(tokens):
                    self._index.setdefault((automaton, tok), set()).add(mid)
                    self._df[(automaton, tok)] = self._df.get((automaton, tok), 0) + 1
            new_entry = e.model_copy(
                update={
                    "content": new_content,
                    "importance": new_imp,
                    "updated_at": datetime.now(tz=timezone.utc),
                }
            )
            self._by_id[(automaton, mid)] = new_entry
            self._version[automaton] = self._version.get(automaton, 0) + 1
            return new_entry

    def delete(self, automaton: AutomatonId, mid: MemoryId) -> None:
        with self._lock:
            e = self._by_id.pop((automaton, mid), None)
            if e is None:
                raise NotFoundError("memory entry not found")
            ids = self._by_layer.get((automaton, e.layer))
            if ids and mid in ids:
                ids.remove(mid)
            tokens = self._tf.pop((automaton, mid), {})
            for tok in tokens:
                s = self._index.get((automaton, tok))
                if s and mid in s:
                    s.discard(mid)
            self._version[automaton] = self._version.get(automaton, 0) + 1

    # ---- reads ----
    def get(self, automaton: AutomatonId, mid: MemoryId) -> MemoryEntry:
        e = self._by_id.get((automaton, mid))
        if e is None:
            raise NotFoundError("memory entry not found")
        return e

    def list(
        self,
        automaton: AutomatonId,
        *,
        layer: MemoryLayer | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        with self._lock:
            if layer is not None:
                ids = list(self._by_layer.get((automaton, layer), []))[-limit:]
            else:
                ids = [k[1] for k in self._by_id.keys() if k[0] == automaton][-limit:]
            return [self._by_id[(automaton, i)] for i in ids]

    def recall(
        self,
        automaton: AutomatonId,
        query: str,
        *,
        k: int = 5,
        layers: Iterable[MemoryLayer] | None = None,
    ) -> list[MemoryEntry]:
        """BM25-ish scoring over the in-memory inverted index."""
        with self._lock:
            tokens = _tokenize(query)
            if not tokens:
                return self.list(automaton, layer=None, limit=k)
            scores: dict[MemoryId, float] = {}
            doc_count = max(1, sum(1 for k0 in self._by_id if k0[0] == automaton))
            for tok in tokens:
                df = self._df.get((automaton, tok), 0)
                if df == 0:
                    continue
                idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
                for mid in self._index.get((automaton, tok), set()):
                    tf = self._tf.get((automaton, mid), {}).get(tok, 0)
                    denom = tf + 0.25 + 0.75 * (sum(self._tf.get((automaton, mid), {}).values()) or 1) / 1.0
                    score = idf * (tf * (1.5 + 1)) / denom
                    scores[mid] = scores.get(mid, 0.0) + score
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            results: list[MemoryEntry] = []
            for mid, _score in ranked[:k]:
                e = self._by_id.get((automaton, mid))
                if e is None:
                    continue
                if layers and e.layer not in set(layers):
                    continue
                results.append(e)
            return results

    # ---- maintenance ----
    def prune(self, automaton: AutomatonId, *, now: float | None = None) -> int:
        """Remove expired entries; returns count removed."""
        now = now or time.time()
        with self._lock:
            to_remove: list[MemoryId] = []
            for (aid, mid), e in self._by_id.items():
                if aid != automaton:
                    continue
                if e.ttl is not None and (e.updated_at.timestamp() + e.ttl) < now:
                    to_remove.append(mid)
            for mid in to_remove:
                self.delete(automaton, mid)
            return len(to_remove)

    def summarize(self, automaton: AutomatonId, *, layer: MemoryLayer, budget_tokens: int = 800) -> str:
        """Trivial extractive summarizer: top-k by importance until budget is exhausted."""
        items = self.list(automaton, layer=layer, limit=200)
        items.sort(key=lambda e: e.importance, reverse=True)
        out: list[str] = []
        used = 0
        for e in items:
            cost = _approx_tokens(e.content) + 8
            if used + cost > budget_tokens:
                break
            out.append(e.content)
            used += cost
        return "\n".join(out) if out else ""

    def version(self, automaton: AutomatonId) -> int:
        return self._version.get(automaton, 0)
