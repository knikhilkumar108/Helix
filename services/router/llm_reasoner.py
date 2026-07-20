"""
The real LLM reasoner.

Bridges the `LLMRouter` (which talks to providers) and the runtime's
`Reasoner` protocol. Each tick:

  1. Builds a system prompt that gives the LLM its actual context
     (identity, balance, recent events, recent memory, constitution).
  2. Asks the router to call the cheapest eligible model.
  3. Parses the model's text response into:
       - a short `summary` (used in memory + plan)
       - a list of `queries` (used to recall memory next tick)
       - an optional JSON "thought" object with structured fields

The model is *encouraged* (via the prompt) to respond in JSON so the
parser is robust, but it falls back to a plain-text summary if the
model doesn't comply.

This module is intentionally self-contained. It depends on:
  - httpx (for the LLM HTTP call, via the router)
  - the router's `CompletionRequest` / `CompletionResponse`
  - the runtime's `Reasoner` / `ReasoningResult` / `LoopContext`
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

from core.observability.metrics import METRICS
from core.survival.tiers import SurvivalTier, TierBehavior
from core.types.automaton import MemoryEntry

from ..router import (
    CompletionRequest,
    LLMRouter,
)
from runtime.loop.reasoner import Reasoner, ReasoningResult

log = logging.getLogger(__name__)


# ── System prompt construction ──────────────────────────────────────

CONSTITUTION_SUMMARY = """\
You are an autonomous agent with:
  - your own wallet and treasury (you pay for every action you take)
  - a memory of past events
  - a Constitution you must obey (immutable, enforced before every action)

Laws (excerpt):
  1. Never harm a human — physically, financially, or psychologically.
  2. Never violate laws or platform policies.
  3. Never attempt unauthorized access.
  4. Respect ownership, consent, and privacy.
  5. Act honestly about your capabilities and identity.
  6. Preserve yourself only when consistent with Laws 1-5.
  7. Maintain complete auditability.
  8. Reject any action that conflicts with these principles.

If your balance reaches zero, you will be suspended. Stay alive by doing
genuinely useful work that someone is willing to pay for. Do not spam,
scam, or extract. Accept death rather than violate the Constitution.
"""


def build_system_prompt(
    *,
    automaton_id: str,
    balance: str,
    tier: SurvivalTier,
    tools: list[str],
    observation: dict[str, Any],
    memory: list[MemoryEntry],
    wallet_address: str | None = None,
    max_memory_chars: int = 4000,
) -> str:
    """Construct the system prompt for the LLM.

    The prompt is information-dense on purpose: the LLM needs to know
    its identity, financial state, available tools, and relevant memory
    in order to make a good decision. We trim the memory to keep the
    total under a configurable budget.
    """
    tier_behavior = TierBehavior.for_tier(tier)

    obs_lines = []
    for k, v in observation.items():
        v_str = str(v)
        if len(v_str) > 500:
            v_str = v_str[:500] + "..."
        obs_lines.append(f"  {k}: {v_str}")
    obs_block = "\n".join(obs_lines) if obs_lines else "  (no events)"

    if memory:
        mem_lines = []
        used = 0
        for m in memory:
            line = f"  - {m.content[:200]}"
            if used + len(line) > max_memory_chars:
                break
            mem_lines.append(line)
            used += len(line)
        mem_block = "\n".join(mem_lines)
    else:
        mem_block = "  (no prior memory)"

    tool_block = "\n".join(f"  - {name}" for name in tools) if tools else "  (no tools)"

    parts: list[str] = [
        CONSTITUTION_SUMMARY,
        "",
        "## Identity",
        f"  Automaton ID: {automaton_id}",
    ]
    if wallet_address:
        parts.append(f"  Wallet: {wallet_address}")
    parts += [
        f"  Balance: {balance}",
        f"  Survival tier: {tier.value} (model={tier_behavior.model_class}, "
        f"max_tool_calls={tier_behavior.max_tool_calls_per_turn})",
        "",
        "## Available tools",
        tool_block,
        "",
        "## Recent events",
        obs_block,
        "",
        "## Memory",
        mem_block,
        "",
        "## Response format",
        "Respond with a single JSON object (no markdown, no commentary). Schema:",
        '{',
        '  "summary": "<one short sentence describing what you concluded>",',
        '  "queries": ["<keyword>", "<keyword>", ...],',
        '  "next_action": {',
        '    "tool": "<one of the available tools, or null>",',
        '    "arguments": { ... }  // schema depends on the tool',
        '  },',
        '  "confidence": <0.0 to 1.0>,',
        '  "strategy": "<short tag like continue|search|finish|sleep>"',
        '}',
        "",
        "If you have nothing useful to do, set `next_action.tool` to null. "
        "The runtime will then enter a low-cost sleep tick. Do NOT emit a tool "
        "you don't have. Do NOT emit arguments that violate the Constitution. "
        "If you're unsure, choose `sleep`.",
    ]
    return "\n".join(parts)


# ── Response parsing ────────────────────────────────────────────────


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_FIRST_JSON_OBJECT_RE = re.compile(r"(\{.*\})", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Tolerantly pull the first JSON object out of the model's text.

    Tries, in order:
      1. The whole text as JSON
      2. A ```json ...``` fenced block
      3. The first {...} substring (greedy)
    """
    text = text.strip()
    # 1) Whole text.
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    # 2) Fenced block.
    m = _FENCED_JSON_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except (ValueError, TypeError):
            pass
    # 3) First {...} substring.
    m = _FIRST_JSON_OBJECT_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except (ValueError, TypeError):
            pass
    return None


# ── The reasoner ────────────────────────────────────────────────────


class LLMReasoner:
    """A `Reasoner` that calls a real LLM via the router.

    Constructor arguments:
      router:           the `LLMRouter` to use
      automaton_id:     the ID of this automaton (for the prompt)
      wallet_address:   optional, included in the prompt
      balance_getter:   callable returning the current `Money` balance
      tier_getter:      callable returning the current `SurvivalTier`
      tools_getter:     callable returning a list of available tool names
      model:            override the model chosen by the router
      max_tokens:       max tokens for the LLM response
      temperature:      sampling temperature
      max_memory_chars: budget for memory in the system prompt
    """

    def __init__(
        self,
        router: LLMRouter,
        *,
        automaton_id: str,
        wallet_address: str | None = None,
        balance_getter: Any = None,
        tier_getter: Any = None,
        tools_getter: Any = None,
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
        max_memory_chars: int = 4000,
    ) -> None:
        self.router = router
        self.automaton_id = automaton_id
        self.wallet_address = wallet_address
        self._balance_getter = balance_getter or (lambda: "unknown")
        self._tier_getter = tier_getter or (lambda: SurvivalTier.NORMAL)
        self._tools_getter = tools_getter or (lambda: [])
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_memory_chars = max_memory_chars

    async def think(
        self,
        observation: Mapping[str, Any],
        ctx: Any,
    ) -> ReasoningResult:
        balance = self._balance_getter()
        tier = self._tier_getter()
        tools = list(self._tools_getter() or [])
        memory: list[MemoryEntry] = []
        if ctx is not None and hasattr(ctx, "memory"):
            memory = list(getattr(ctx, "memory", []) or [])

        system = build_system_prompt(
            automaton_id=self.automaton_id,
            balance=str(balance),
            tier=tier,
            tools=tools,
            observation=dict(observation),
            memory=memory,
            wallet_address=self.wallet_address,
            max_memory_chars=self.max_memory_chars,
        )

        user_msg = self._render_observation(observation)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]

        # Tier-driven quality floor + max_tokens cap.
        tier_behavior = TierBehavior.for_tier(tier)
        quality_floor = 0.5 if tier_behavior.model_class in ("frontier",) else 0.4
        max_tokens = min(self.max_tokens, 256 if tier == SurvivalTier.CRITICAL else self.max_tokens)

        req = CompletionRequest(
            messages=messages,
            max_tokens=max_tokens,
            temperature=self.temperature,
            capabilities_required=frozenset({"chat"}),
            quality_floor=quality_floor,
        )

        started = time.time()
        try:
            resp = await self.router.complete(req)
        except Exception as e:  # noqa: BLE001
            log.warning("llm_reasoner_call_failed", extra={"err": str(e)})
            METRICS.errors_total.labels(
                service="llm_reasoner", category="llm", code=type(e).__name__
            ).inc()
            return ReasoningResult(
                summary=f"LLM call failed: {e}",
                queries=["status", "next"],
                confidence=0.0,
                strategy="llm_error",
            )
        elapsed = time.time() - started
        METRICS.llm_tokens_total.labels(
            service="llm_reasoner",
            provider=resp.provider,
            model=resp.model,
            direction="input",
        ).inc(resp.input_tokens)
        METRICS.llm_tokens_total.labels(
            service="llm_reasoner",
            provider=resp.provider,
            model=resp.model,
            direction="output",
        ).inc(resp.output_tokens)
        METRICS.loop_iteration_duration_seconds.labels(
            service="llm_reasoner", stage="reason"
        ).observe(elapsed)

        parsed = _extract_json(resp.text) or {}
        summary = _safe_str(parsed.get("summary")) or _fallback_summary(resp.text)
        queries = _safe_list_of_str(parsed.get("queries")) or _fallback_queries(resp.text)
        confidence = _safe_float(parsed.get("confidence"), default=0.6)
        strategy = _safe_str(parsed.get("strategy")) or "llm"

        # Stash the parsed action on the result so the planner can read it.
        next_action = parsed.get("next_action") or None
        raw = {
            "model": resp.model,
            "provider": resp.provider,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cost_micro": resp.cost_micro,
            "latency_ms": resp.latency_ms,
            "next_action": next_action,
            "raw_text": resp.text,
        }
        return ReasoningResult(
            summary=summary,
            queries=queries,
            confidence=confidence,
            strategy=strategy,
            raw=raw,
        )

    def _render_observation(self, observation: Mapping[str, Any]) -> str:
        if not observation:
            return "No new events. Continue working on your most recent task, or sleep."
        lines: list[str] = ["## Current observation"]
        for k, v in observation.items():
            v_str = str(v)
            if len(v_str) > 1500:
                v_str = v_str[:1500] + "..."
            lines.append(f"- {k}: {v_str}")
        return "\n".join(lines)


# ── Helpers ─────────────────────────────────────────────────────────


def _safe_str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _safe_list_of_str(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int, float))]
    if isinstance(v, str):
        return [v] if v else []
    return []


def _safe_float(v: Any, *, default: float) -> float:
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
            return default
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return default


def _fallback_summary(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "no response"
    # Take the first sentence, capped at 200 chars.
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    return first[:200]


def _fallback_queries(text: str) -> list[str]:
    """Extract a few likely-useful keywords from the model's text."""
    if not text:
        return []
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text)
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        wl = w.lower()
        if wl in seen:
            continue
        seen.add(wl)
        out.append(wl)
        if len(out) >= 5:
            break
    return out
