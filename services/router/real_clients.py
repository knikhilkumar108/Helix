"""
Real LLM provider clients.

Implements the `ModelClient` protocol for several providers. All clients
speak the OpenAI Chat Completions HTTP shape, so swapping providers is
a configuration change rather than a code change.

Supported out of the box:
  - OpenAI        (api.openai.com)
  - OpenRouter    (openrouter.ai) — gateway to many models
  - Together      (api.together.xyz)
  - Groq          (api.groq.com/openai/v1)
  - Ollama        (localhost:11434/v1) — local models
  - vLLM / any    OpenAI-compatible server

If you need Anthropic native (different wire format), see
`AnthropicClient` below — it translates to the OpenAI shape internally.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

import httpx

from .router import (
    CompletionRequest,
    CompletionResponse,
    ModelClient,
    ModelSpec,
)

log = logging.getLogger(__name__)


class HttpError(Exception):
    pass


class _OpenAICompatClient:
    """Base for any OpenAI-compatible chat-completions API."""

    spec: ModelSpec

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        spec: ModelSpec,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.spec = spec
        self._default_headers = dict(default_headers or {})
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        url = f"{self.base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": self.spec.name,
            "messages": list(req.messages),
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        # OpenAI-style tools: pass through if any caller adds them.
        if req.capabilities_required and "json" in req.capabilities_required:
            body["response_format"] = {"type": "json_object"}
        headers = {"content-type": "application/json", **self._default_headers}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                t0 = time.time()
                r = await self._client.post(url, json=body, headers=headers)
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = HttpError(f"upstream {r.status_code}: {r.text[:300]}")
                    await _sleep_backoff(attempt)
                    continue
                r.raise_for_status()
                payload = r.json()
                latency_ms = (time.time() - t0) * 1000
                return self._parse(payload, latency_ms)
            except httpx.HTTPError as e:  # noqa: BLE001
                last_err = e
                await _sleep_backoff(attempt)
        raise last_err if last_err else HttpError("unknown failure")

    def _parse(self, payload: dict[str, Any], latency_ms: float) -> CompletionResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise HttpError(f"no choices in response: {payload}")
        first = choices[0]
        message = first.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            # Some providers return content as a list of parts.
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        usage = payload.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        cost = self._estimate_cost(in_tok, out_tok)
        return CompletionResponse(
            text=text,
            model=payload.get("model", self.spec.name),
            provider=self.spec.provider,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_micro=cost,
            latency_ms=latency_ms,
        )

    def _estimate_cost(self, in_tok: int, out_tok: int) -> int:
        return int(
            in_tok / 1000 * self.spec.cost_per_1k_input_micro
            + out_tok / 1000 * self.spec.cost_per_1k_output_micro
        )


async def _sleep_backoff(attempt: int) -> None:
    import asyncio

    await asyncio.sleep(min(2 ** attempt * 0.5, 8.0))


# ── Concrete clients ───────────────────────────────────────────────


class OpenAIClient(_OpenAICompatClient):
    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
    ) -> None:
        api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        # Pricing (per 1k tokens) as of 2024. Override in subclasses.
        pricing = {
            "gpt-4o-mini": (150, 600),       # input, output in micro-USD per 1k
            "gpt-4o": (2500, 10000),
            "gpt-4-turbo": (10000, 30000),
            "gpt-3.5-turbo": (500, 1500),
        }
        in_p, out_p = pricing.get(model, (1000, 3000))
        spec = ModelSpec(
            name=model,
            provider="openai",
            context_window=128_000,
            cost_per_1k_input_micro=in_p,
            cost_per_1k_output_micro=out_p,
            capabilities=frozenset({"chat", "code", "reasoning"}),
            quality=0.85 if "gpt-4" in model else 0.7,
            avg_latency_ms=900,
        )
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            spec=spec,
        )


class AnthropicClient(_OpenAICompatClient):
    """Anthropic via the OpenAI-compat shim at api.anthropic.com/v1.

    For native Anthropic API access, override `_parse` and the request
    body. The shim works for Claude 3.5+ and is the easiest path.
    """

    def __init__(
        self,
        *,
        model: str = "claude-3-5-sonnet-latest",
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        timeout_seconds: float = 60.0,
    ) -> None:
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        pricing = {
            "claude-3-5-sonnet-latest": (3000, 15000),
            "claude-3-5-haiku-latest": (800, 4000),
            "claude-3-opus-latest": (15000, 75000),
        }
        in_p, out_p = pricing.get(model, (3000, 15000))
        spec = ModelSpec(
            name=model,
            provider="anthropic",
            context_window=200_000,
            cost_per_1k_input_micro=in_p,
            cost_per_1k_output_micro=out_p,
            capabilities=frozenset({"chat", "code", "reasoning"}),
            quality=0.9 if "sonnet" in model or "opus" in model else 0.75,
            avg_latency_ms=1100,
        )
        # Anthropic requires this header for the OpenAI shim.
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            spec=spec,
            default_headers={"anthropic-version": "2023-06-01"},
        )


class OllamaClient(_OpenAICompatClient):
    """Local Ollama (or any OpenAI-compat server) at localhost:11434."""

    def __init__(
        self,
        *,
        model: str = "llama3.1",
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        spec = ModelSpec(
            name=model,
            provider="ollama",
            context_window=32_000,
            cost_per_1k_input_micro=0,
            cost_per_1k_output_micro=0,
            capabilities=frozenset({"chat", "code"}),
            quality=0.65,
            avg_latency_ms=2000,
        )
        super().__init__(
            base_url=base_url,
            api_key=None,
            timeout_seconds=timeout_seconds,
            spec=spec,
        )


class OpenRouterClient(_OpenAICompatClient):
    def __init__(
        self,
        *,
        model: str = "anthropic/claude-3.5-sonnet",
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 60.0,
    ) -> None:
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        spec = ModelSpec(
            name=model,
            provider="openrouter",
            context_window=200_000,
            cost_per_1k_input_micro=3000,
            cost_per_1k_output_micro=15000,
            capabilities=frozenset({"chat", "code", "reasoning"}),
            quality=0.85,
            avg_latency_ms=1500,
        )
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            spec=spec,
        )


def default_real_router(
    *,
    prefer: str = "auto",
    openai_key: str | None = None,
    anthropic_key: str | None = None,
    ollama_model: str | None = None,
) -> "Any":  # returns LLMRouter, but type cycle avoided
    """Build a router that prefers the cheapest *available* real model.

    `prefer` is one of: "auto" (recommended), "openai", "anthropic",
    "ollama", "openrouter". The "auto" mode picks the first provider
    that has a key configured (or Ollama if it answers on localhost).
    """
    from .router import LLMRouter

    candidates: list[ModelClient] = []
    openai_key = openai_key or os.environ.get("OPENAI_API_KEY")
    anthropic_key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY")
    ollama_model = ollama_model or os.environ.get("OLLAMA_MODEL")

    if prefer in ("auto", "openai") and openai_key:
        candidates.append(OpenAIClient(api_key=openai_key))
    if prefer in ("auto", "anthropic") and anthropic_key:
        candidates.append(AnthropicClient(api_key=anthropic_key))
    if prefer in ("auto", "ollama") and ollama_model:
        candidates.append(OllamaClient(model=ollama_model))
    if prefer in ("auto", "openrouter") and os.environ.get("OPENROUTER_API_KEY"):
        candidates.append(OpenRouterClient())

    # If nothing was configured, fall back to Ollama at default URL — the
    # most likely case for local development.
    if not candidates and prefer in ("auto", "ollama"):
        candidates.append(OllamaClient(model=ollama_model or "llama3.1"))

    return LLMRouter(candidates)
