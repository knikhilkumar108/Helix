"""Tests for the real LLM reasoner and the JSON parser."""
from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from httpx import Response

from core.survival.tiers import SurvivalTier
from core.types.automaton import MemoryEntry, MemoryLayer
from core.types.identifiers import (
    ActionId,
    AutomatonId,
    MemoryId,
    PlanId,
    TaskId,
    new_action_id,
    new_memory_id,
    new_plan_id,
    new_task_id,
)
from core.types.money import Money
from runtime.loop.reasoner import ReasoningResult
from services.router.llm_reasoner import (
    LLMReasoner,
    _extract_json,
    _fallback_queries,
    _fallback_summary,
    build_system_prompt,
)
from services.router.real_clients import AnthropicClient, OllamaClient, OpenAIClient
from services.router.router import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelClient,
    ModelSpec,
)


# ── JSON parser ────────────────────────────────────────────────────


def test_extract_json_plain_object():
    assert _extract_json('{"summary": "hi"}') == {"summary": "hi"}


def test_extract_json_fenced():
    text = "Here is the response:\n```json\n{\"summary\": \"hi\"}\n```\nDone."
    assert _extract_json(text) == {"summary": "hi"}


def test_extract_json_embedded():
    text = 'I think {"queries": ["x", "y"]} is the right answer.'
    assert _extract_json(text) == {"queries": ["x", "y"]}


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("not json at all") is None


def test_extract_json_handles_whitespace():
    text = '   \n  {"summary": "  spaced  "}\n   '
    assert _extract_json(text) == {"summary": "  spaced  "}


# ── Fallback summary / queries ─────────────────────────────────────


def test_fallback_summary_picks_first_sentence():
    s = _fallback_summary("Hello there. This is a test. Goodbye.")
    assert s == "Hello there."


def test_fallback_summary_handles_empty():
    assert _fallback_summary("") == "no response"


def test_fallback_queries_dedupes_and_caps():
    qs = _fallback_queries("python python java go rust go python")
    assert qs.count("python") == 1
    assert len(qs) <= 5


def test_fallback_queries_handles_empty():
    assert _fallback_queries("") == []


# ── System prompt construction ────────────────────────────────────


def _memory(content: str) -> MemoryEntry:
    from datetime import datetime, timezone

    return MemoryEntry(
        id=MemoryId(new_memory_id()),
        automaton_id=AutomatonId("atm_" + "a" * 32),
        layer=MemoryLayer.LONG_TERM,
        content=content,
        importance=0.5,
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
        tags=[],
    )


def test_system_prompt_contains_identity():
    p = build_system_prompt(
        automaton_id="atm_abc",
        balance="1.00 USDC",
        tier=SurvivalTier.NORMAL,
        tools=["memory.write", "shell.exec"],
        observation={"events": ["tick"]},
        memory=[],
    )
    assert "atm_abc" in p
    assert "1.00 USDC" in p
    assert "Constitution" in p
    assert "memory.write" in p
    assert "shell.exec" in p


def test_system_prompt_includes_memory_when_present():
    p = build_system_prompt(
        automaton_id="atm_x",
        balance="0.50 USDC",
        tier=SurvivalTier.LOW_COMPUTE,
        tools=[],
        observation={},
        memory=[_memory("the user prefers Python"), _memory("we use Postgres")],
    )
    assert "Python" in p
    assert "Postgres" in p
    assert "low_compute" in p


def test_system_prompt_clamps_memory_size():
    big = _memory("x" * 20_000)
    p = build_system_prompt(
        automaton_id="atm_x",
        balance="0",
        tier=SurvivalTier.CRITICAL,
        tools=[],
        observation={},
        memory=[big],
        max_memory_chars=200,
    )
    # 200 chars of memory + 8-char prefix = well under 1000 chars total
    assert len(p) < 5000


# ── Fake router (no network) ───────────────────────────────────────


class _ScriptedModelClient:
    """A `ModelClient` that returns a pre-canned response."""

    def __init__(self, response_text: str, model: str = "fake") -> None:
        self.spec = ModelSpec(
            name=model,
            provider="fake",
            context_window=128_000,
            cost_per_1k_input_micro=0,
            cost_per_1k_output_micro=0,
            capabilities=frozenset({"chat"}),
            quality=0.7,
            avg_latency_ms=10,
        )
        self.response_text = response_text
        self.calls: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        return CompletionResponse(
            text=self.response_text,
            model=self.spec.name,
            provider=self.spec.provider,
            input_tokens=100,
            output_tokens=len(self.response_text) // 4,
            cost_micro=0,
            latency_ms=10.0,
        )


def _ctx_stub(memory: list[MemoryEntry] | None = None) -> Any:
    class _C:
        def __init__(self):
            self.memory = memory or []
            self.automaton_id = AutomatonId("atm_" + "a" * 32)

    return _C()


# ── LLMReasoner end-to-end with fake router ────────────────────────


@pytest.mark.asyncio
async def test_reasoner_parses_well_formed_json():
    client = _ScriptedModelClient(
        '{"summary": "ready to act", "queries": ["task", "next"], '
        '"next_action": {"tool": "memory.write", "arguments": {"content": "hi"}}, '
        '"confidence": 0.8, "strategy": "continue"}'
    )
    router = LLMRouter([client])
    reasoner = LLMReasoner(
        router,
        automaton_id="atm_test",
        balance_getter=lambda: Money.from_major("1.00"),
        tier_getter=lambda: SurvivalTier.NORMAL,
        tools_getter=lambda: ["memory.write", "memory.read"],
    )
    result = await reasoner.think({"events": ["wake"]}, _ctx_stub())
    assert result.summary == "ready to act"
    assert result.queries == ["task", "next"]
    assert result.confidence == 0.8
    assert result.strategy == "continue"
    assert result.raw["next_action"]["tool"] == "memory.write"
    assert result.raw["provider"] == "fake"


@pytest.mark.asyncio
async def test_reasoner_tolerates_garbage_output():
    client = _ScriptedModelClient("Sorry, I cannot help with that.")
    router = LLMRouter([client])
    reasoner = LLMReasoner(
        router,
        automaton_id="atm_test",
        balance_getter=lambda: Money.from_major("0.50"),
        tier_getter=lambda: SurvivalTier.LOW_COMPUTE,
        tools_getter=lambda: [],
    )
    result = await reasoner.think({}, _ctx_stub())
    # Falls back to a summary derived from the raw text.
    assert "Sorry" in result.summary
    # Confidence clamped to [0, 1] default 0.6.
    assert 0.0 <= result.confidence <= 1.0


@pytest.mark.asyncio
async def test_reasoner_falls_back_to_sleep_when_no_action():
    client = _ScriptedModelClient(
        '{"summary": "all done", "next_action": {"tool": null, "arguments": {}}}'
    )
    router = LLMRouter([client])
    reasoner = LLMReasoner(router, automaton_id="atm_test")
    result = await reasoner.think({}, _ctx_stub())
    assert result.summary == "all done"
    assert result.raw["next_action"]["tool"] is None


@pytest.mark.asyncio
async def test_reasoner_critical_tier_clamps_max_tokens():
    client = _ScriptedModelClient('{"summary": "x"}')
    router = LLMRouter([client])
    reasoner = LLMReasoner(
        router,
        automaton_id="atm_test",
        tier_getter=lambda: SurvivalTier.CRITICAL,
        max_tokens=2048,
    )
    await reasoner.think({}, _ctx_stub())
    sent_max_tokens = client.calls[0].max_tokens
    assert sent_max_tokens <= 256  # critical tier clamps to 256


@pytest.mark.asyncio
async def test_reasoner_charges_router_in_normal_tier():
    client = _ScriptedModelClient('{"summary": "x"}')
    router = LLMRouter([client])
    reasoner = LLMReasoner(
        router,
        automaton_id="atm_test",
        tier_getter=lambda: SurvivalTier.NORMAL,
        max_tokens=1024,
    )
    await reasoner.think({}, _ctx_stub())
    assert client.calls[0].max_tokens == 1024


# ── Real HTTP clients (mocked via respx) ───────────────────────────


@pytest.mark.asyncio
async def test_openai_client_sends_correct_request():
    client = OpenAIClient(model="gpt-4o-mini", api_key="sk-test", base_url="https://api.test/v1")
    with respx.mock(base_url="https://api.test") as mock:
        route = mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "id": "x",
                    "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )
        )
        resp = await client.complete(
            CompletionRequest(messages=[{"role": "user", "content": "hello"}])
        )
        await client.close()
    assert resp.text == "hi"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5
    assert resp.cost_micro > 0  # gpt-4o-mini isn't free
    # The request body was correct.
    body = route.calls.last.request.content
    parsed = json.loads(body)
    assert parsed["model"] == "gpt-4o-mini"
    assert parsed["messages"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_anthropic_client_uses_anthropic_version_header():
    client = AnthropicClient(model="claude-3-5-haiku-latest", api_key="sk-test", base_url="https://api.ant/v1")
    with respx.mock(base_url="https://api.ant") as mock:
        route = mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "id": "x",
                    "model": "claude-3-5-haiku-latest",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            )
        )
        await client.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))
        await client.close()
    sent = route.calls.last.request.headers
    assert sent.get("anthropic-version") == "2023-06-01"
    assert sent.get("authorization") == "Bearer sk-test"


@pytest.mark.asyncio
async def test_ollama_client_no_api_key():
    client = OllamaClient(model="llama3.1", base_url="http://localhost:11434/v1")
    with respx.mock(base_url="http://localhost:11434") as mock:
        route = mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "id": "x",
                    "model": "llama3.1",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                },
            )
        )
        resp = await client.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))
        await client.close()
    assert resp.text == "ok"
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.asyncio
async def test_http_client_retries_on_429():
    client = OpenAIClient(model="gpt-4o-mini", api_key="sk-test", base_url="https://api.test/v1")
    with respx.mock(base_url="https://api.test", assert_all_called=False) as mock:
        route = mock.post("/v1/chat/completions")
        route.side_effect = [
            Response(429, json={"error": "rate limited"}),
            Response(
                200,
                json={
                    "id": "x",
                    "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ),
        ]
        resp = await client.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))
        await client.close()
    assert resp.text == "ok"
    assert route.call_count == 2


# ── Integration: real reasoner through the router ─────────────────


@pytest.mark.asyncio
async def test_router_with_real_client_via_mock():
    """The router should pick the real client (the only one available)."""
    client = _ScriptedModelClient('{"summary": "via router"}', model="real-fake")
    router = LLMRouter([client])
    reasoner = LLMReasoner(
        router,
        automaton_id="atm_routed",
        balance_getter=lambda: Money.from_major("0.10"),
        tier_getter=lambda: SurvivalTier.CRITICAL,
    )
    result = await reasoner.think({}, _ctx_stub())
    assert result.raw["model"] == "real-fake"
