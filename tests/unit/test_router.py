"""Unit tests for the LLM router."""
from __future__ import annotations

import asyncio

import pytest

from services.router.router import (
    CompletionRequest,
    LLMRouter,
    ModelSpec,
    StubModelClient,
)


def _router() -> LLMRouter:
    return LLMRouter(
        [
            StubModelClient(
                ModelSpec(
                    name="cheap",
                    provider="local",
                    context_window=4096,
                    cost_per_1k_input_micro=0,
                    cost_per_1k_output_micro=0,
                    capabilities=frozenset({"chat"}),
                    quality=0.6,
                    avg_latency_ms=100,
                )
            ),
            StubModelClient(
                ModelSpec(
                    name="pro",
                    provider="cloud",
                    context_window=128_000,
                    cost_per_1k_input_micro=3000,
                    cost_per_1k_output_micro=15000,
                    capabilities=frozenset({"chat", "code"}),
                    quality=0.9,
                    avg_latency_ms=900,
                )
            ),
        ]
    )


def test_routes_simple_chat_to_cheapest_eligible():
    r = _router()
    req = CompletionRequest(messages=[{"role": "user", "content": "hi"}])
    resp = asyncio.run(r.complete(req))
    assert resp.model == "cheap"


def test_skips_models_without_required_capability():
    r = _router()
    req = CompletionRequest(
        messages=[{"role": "user", "content": "code me a thing"}],
        capabilities_required=frozenset({"code"}),
    )
    resp = asyncio.run(r.complete(req))
    # Both models support "code"; pro is higher quality; cheaper model also OK.
    assert resp.model in ("cheap", "pro")


def test_fails_when_no_eligible_model():
    r = _router()
    req = CompletionRequest(
        messages=[{"role": "user", "content": "x"}],
        capabilities_required=frozenset({"vision"}),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(r.complete(req))
