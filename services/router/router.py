"""
LLM router. Decides which provider/model to use for a given task based on:
  - cost
  - latency
  - quality (task-completion success rate)
  - context length
  - availability
  - task complexity

The router keeps a sliding window of per-model stats; a model that is down,
too expensive, or under-performing for the current task is skipped.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ModelSpec:
    name: str
    provider: str
    context_window: int
    cost_per_1k_input_micro: int
    cost_per_1k_output_micro: int
    capabilities: frozenset[str]  # chat | code | vision | embeddings | reasoning | speech
    quality: float = 0.7  # 0..1
    avg_latency_ms: float = 500.0


@dataclass(slots=True)
class CompletionRequest:
    messages: list[dict[str, Any]]
    max_tokens: int = 1024
    temperature: float = 0.2
    capabilities_required: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))
    budget_micro: int | None = None
    quality_floor: float = 0.0


@dataclass(slots=True)
class CompletionResponse:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_micro: int
    latency_ms: float


class ModelClient(Protocol):
    spec: ModelSpec

    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...


@dataclass(slots=True)
class _Stats:
    calls: int = 0
    failures: int = 0
    ema_latency_ms: float = 0.0
    ema_quality: float = 0.7
    cooldown_until: float = 0.0


class LLMRouter:
    """Quality/cost optimizing router."""

    def __init__(self, clients: list[ModelClient]) -> None:
        self._clients: dict[str, ModelClient] = {c.spec.name: c for c in clients}
        self._stats: dict[str, _Stats] = {c.spec.name: _Stats() for c in clients}

    def add(self, client: ModelClient) -> None:
        self._clients[client.spec.name] = client
        self._stats[client.spec.name] = _Stats()

    def models(self) -> list[ModelSpec]:
        return [c.spec for c in self._clients.values()]

    def _eligible(self, req: CompletionRequest) -> list[ModelClient]:
        out: list[ModelClient] = []
        now = time.time()
        for c in self._clients.values():
            s = self._stats[c.spec.name]
            if s.cooldown_until > now:
                continue
            if not req.capabilities_required.issubset(c.spec.capabilities):
                continue
            if c.spec.context_window < _estimate_context(req.messages):
                continue
            if c.spec.quality < req.quality_floor:
                continue
            out.append(c)
        return out

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        candidates = self._eligible(req)
        if not candidates:
            raise RuntimeError("no eligible model for request")

        # Score: lower is better. Combine cost and latency, normalized.
        max_cost = max(_estimate_cost(c.spec, req) for c in candidates) or 1
        max_lat = max(c.spec.avg_latency_ms for c in candidates) or 1

        def score(c: ModelClient) -> float:
            s = self._stats[c.spec.name]
            cost = _estimate_cost(c.spec, req) / max_cost
            lat = (s.ema_latency_ms or c.spec.avg_latency_ms) / max_lat
            q = 1.0 - c.spec.quality
            return 0.5 * cost + 0.2 * lat + 0.3 * q

        candidates.sort(key=score)
        last_err: Exception | None = None
        for c in candidates:
            try:
                t0 = time.time()
                resp = await c.complete(req)
                elapsed_ms = (time.time() - t0) * 1000
                self._update_stats(c.spec.name, ok=True, latency_ms=elapsed_ms, quality=resp and 1.0 or 0.0)
                return resp
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._update_stats(c.spec.name, ok=False, latency_ms=5000, quality=0.0)
                log.warning("router_attempt_failed", extra={"model": c.spec.name, "err": str(e)})
        assert last_err is not None
        raise last_err

    def _update_stats(self, model: str, *, ok: bool, latency_ms: float, quality: float) -> None:
        s = self._stats[model]
        s.calls += 1
        if not ok:
            s.failures += 1
            # 30s cooldown on failure; 5 min if many recent failures.
            s.cooldown_until = time.time() + (300 if s.failures > 3 else 30)
        s.ema_latency_ms = 0.7 * s.ema_latency_ms + 0.3 * latency_ms if s.ema_latency_ms else latency_ms
        s.ema_quality = 0.7 * s.ema_quality + 0.3 * quality if s.ema_quality else quality


def _estimate_context(messages: list[dict[str, Any]]) -> int:
    # Rough heuristic: 1 token ~ 4 chars.
    return sum(len(str(m.get("content", ""))) for m in messages) // 4


def _estimate_cost(spec: ModelSpec, req: CompletionRequest) -> float:
    in_tokens = _estimate_context(req.messages)
    out_tokens = req.max_tokens
    return (
        in_tokens / 1000 * spec.cost_per_1k_input_micro
        + out_tokens / 1000 * spec.cost_per_1k_output_micro
    )


# --- Default clients (stubs that hit a remote provider) ---------------
class StubModelClient:
    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        # Real impl: call provider SDK with retries and timeouts.
        await asyncio.sleep(0.01)
        return CompletionResponse(
            text="",
            model=self.spec.name,
            provider=self.spec.provider,
            input_tokens=_estimate_context(req.messages),
            output_tokens=0,
            cost_micro=int(_estimate_cost(self.spec, req)),
            latency_ms=10.0,
        )


def default_router() -> LLMRouter:
    return LLMRouter(
        [
            StubModelClient(
                ModelSpec(
                    name="local-fast",
                    provider="local",
                    context_window=8192,
                    cost_per_1k_input_micro=0,
                    cost_per_1k_output_micro=0,
                    capabilities=frozenset({"chat", "code"}),
                    quality=0.6,
                    avg_latency_ms=200,
                )
            ),
            StubModelClient(
                ModelSpec(
                    name="cloud-pro",
                    provider="cloud",
                    context_window=200_000,
                    cost_per_1k_input_micro=3000,
                    cost_per_1k_output_micro=15000,
                    capabilities=frozenset({"chat", "code", "reasoning"}),
                    quality=0.9,
                    avg_latency_ms=900,
                )
            ),
        ]
    )
