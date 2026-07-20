"""
Conversation history — token-budgeted, summarizable, format-agnostic.

The agent's runtime calls the LLM once per tick. The LLM
sees a system prompt + the current observation. Between ticks,
the agent forgets everything the user said. That's fine for
short interactions but breaks any multi-turn conversation:
the user has to repeat themselves on every turn.

This module adds a *conversation history* to the agent:
  - A bounded list of `Turn` records (one per tick).
  - A `render_for_llm()` method that produces a list of
    messages sized to fit a token budget.
  - A `compact()` method that summarizes old turns when the
    budget is exceeded.

The history is *format-agnostic* — we don't store OpenAI
messages or Anthropic messages, we store platform-native
turns. `render_for_llm()` adapts to the target LLM's format.

Why format-agnostic?

We have multiple LLM providers (OpenAI, Anthropic, Ollama).
Their message formats differ. Storing platform-native turns
and rendering per-provider keeps the storage layer simple
and lets the same history be served to different providers
without re-parsing.

Why a budget?

LLM context windows are finite. A 100-turn conversation
can be 100K tokens; the agent's max context might be 8K.
The history has to fit, with the most recent turns
preserved verbatim and the older ones summarized.

Why summarization?

Summarization is a *quality* trade-off, not a *correctness*
trade-off. The agent can keep working with summarized old
turns; the user just sees less detail. We do best-effort
extractive summarization: take the recent system events and
concatenate them. A real LLM-backed summarizer is added in
a later turn.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

log = logging.getLogger(__name__)


# ── Turn types ─────────────────────────────────────────────


class Role(str, Enum):
    """Whose perspective the message is from."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    TOOL = "tool"
    SUMMARY = "summary"  # a synthetic turn produced by compact()


@dataclass(slots=True)
class Turn:
    """A single turn in the conversation.

    `role` says whose perspective this is. `content` is the
    human-readable text. `tool_calls` is non-empty for
    `Role.AGENT` turns where the agent invoked tools;
    `tool_results` is non-empty for `Role.TOOL` turns.
    `metadata` is open for any structured data the runtime
    wants to attach (e.g. tier at time of turn, balance, etc).
    """

    id: str
    role: Role
    content: str
    created_at: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # A `summary_of` is set when this turn is a synthetic
    # summary produced by `compact()`. It points to the ids
    # of the turns that were collapsed.
    summary_of: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role.value,
            "content": self.content,
            "created_at": self.created_at,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "metadata": self.metadata,
            "summary_of": self.summary_of,
        }


# ── Token estimation ──────────────────────────────────────


# Rough heuristic: 1 token ≈ 4 chars in English text. This is
# the "chars/4" rule of thumb used in many LLM libraries.
# It's not exact, but it's good enough for budget enforcement
# without invoking a tokenizer (which would be slow on every
# render). For high-stakes deployments, replace with a real
# tokenizer (tiktoken for OpenAI, etc).
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


# ── Conversation history ──────────────────────────────────


@dataclass
class ConversationHistory:
    """A bounded, token-budgeted history of conversation turns.

    The history is *append-only* from the outside: callers
    call `add_turn()` and the history grows. Internally,
    `compact()` may collapse old turns into a `SUMMARY`
    turn when the total token estimate exceeds the budget.

    Concurrency: the history is single-threaded; the runtime
    calls it from the loop's tick. Multi-loop histories are
    not supported (one history per agent).
    """

    budget_tokens: int = 4000
    turns: list[Turn] = field(default_factory=list)
    # `max_turns` is a hard cap regardless of token budget.
    # A runaway agent that emits tiny turns should still
    # be bounded.
    max_turns: int = 200
    # `summary_threshold` is the fraction of `budget_tokens`
    # at which we trigger `compact()`. Lower means more
    # aggressive summarization.
    summary_threshold: float = 0.8
    # Clock for tests.
    clock: callable = time.time

    def __post_init__(self) -> None:
        if self.budget_tokens <= 0:
            raise ValueError("budget_tokens must be positive")
        if self.max_turns <= 0:
            raise ValueError("max_turns must be positive")
        if not 0 < self.summary_threshold <= 1:
            raise ValueError("summary_threshold must be in (0, 1]")

    # ── Mutation ──
    def add_turn(
        self,
        *,
        role: Role,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_results: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Turn:
        """Append a turn. Returns the persisted `Turn`.

        If adding the turn would exceed `max_turns`, the
        oldest turn is dropped (after compacting if needed).
        """
        turn = Turn(
            id=f"turn_{uuid.uuid4().hex}",
            role=role,
            content=content,
            created_at=datetime.fromtimestamp(self._now(), tz=timezone.utc).isoformat(),
            tool_calls=list(tool_calls or []),
            tool_results=list(tool_results or []),
            metadata=dict(metadata or {}),
        )
        self.turns.append(turn)
        # Drop oldest if over the cap.
        while len(self.turns) > self.max_turns:
            self.turns.pop(0)
        # Compact if over the budget threshold.
        if self.estimated_tokens() > self.budget_tokens * self.summary_threshold:
            self.compact()
        return turn

    def clear(self) -> None:
        self.turns = []

    # ── Inspection ──
    def estimated_tokens(self) -> int:
        return sum(estimate_tokens(t.content) for t in self.turns)

    def __len__(self) -> int:
        return len(self.turns)

    # ── Compaction ──
    def compact(self) -> int:
        """Summarize the oldest turns to fit the budget.

        Strategy: take all `SUMMARY` and `USER` turns, plus
        the first 3 most recent non-summary turns, and
        concatenate them into a single `SUMMARY` turn. The
        rest of the recent turns are kept verbatim. This is
        a *destructive* compaction: the original turns are
        replaced by the summary, with their ids recorded in
        `summary_of` for audit.
        """
        if not self.turns:
            return 0
        # If we're already under budget, no need to compact.
        if self.estimated_tokens() <= self.budget_tokens * self.summary_threshold:
            return 0
        # Find the boundary: keep the most recent N turns
        # verbatim; collapse everything older.
        # Heuristic: keep the last 5 turns. The exact N
        # depends on turn sizes and budget; this is a
        # reasonable default that preserves short recent
        # context while making room.
        keep_recent = min(5, len(self.turns))
        old, recent = self.turns[:-keep_recent], self.turns[-keep_recent:]
        if not old:
            return 0
        # Build the summary content: a bullet list of
        # (role, content) pairs from the old turns, with
        # tool calls summarized inline. This is extractive,
        # not abstractive — a real LLM-backed summarizer
        # would do better.
        lines: list[str] = []
        for t in old:
            if t.role == Role.SUMMARY:
                # Don't recurse; just preserve the prior summary.
                lines.append(f"[prior summary] {t.content}")
                continue
            ts = t.content.strip().replace("\n", " ")[:200]
            lines.append(f"- ({t.role.value}) {ts}")
        summary_content = "Summary of earlier conversation:\n" + "\n".join(lines)
        summary_turn = Turn(
            id=f"turn_{uuid.uuid4().hex}",
            role=Role.SUMMARY,
            content=summary_content,
            created_at=datetime.fromtimestamp(self._now(), tz=timezone.utc).isoformat(),
            summary_of=[t.id for t in old],
        )
        self.turns = [summary_turn] + recent
        log.info(
            "history_compact",
            extra={
                "collapsed": len(old),
                "kept_recent": len(recent),
                "new_tokens": self.estimated_tokens(),
            },
        )
        return len(old)

    # ── Rendering ──
    def render_for_llm(
        self,
        *,
        max_tokens: int | None = None,
        include_tool_results: bool = True,
    ) -> list[dict[str, Any]]:
        """Render the history as a list of messages in
        OpenAI-compatible format.

        `max_tokens` overrides the configured budget for
        this render. `include_tool_results` controls whether
        `Role.TOOL` turns are emitted; some providers don't
        accept them in the message list.

        Render strategy: walk turns newest-to-oldest, accept
        each turn that fits within the remaining budget, stop
        when the budget is exhausted. The most recent turn
        is always included (even if it blows the budget) so
        the LLM always has the latest context. Reverse the
        result so the LLM sees oldest-first.

        For `Role.AGENT` turns with `tool_calls`, the rendered
        message includes a `tool_calls` field (OpenAI's
        function-calling format). For `Role.TOOL` turns, the
        rendered message includes a `tool_call_id` that
        references the agent's tool call. This lets a real
        tool-using agent see its prior tool activity in the
        conversation context.
        """
        budget = max_tokens or self.budget_tokens
        out: list[dict[str, Any]] = []
        used = 0
        for turn in reversed(self.turns):
            if turn.role == Role.TOOL and not include_tool_results:
                continue
            tok = estimate_tokens(turn.content)
            # Stop adding once we're over budget, BUT always
            # include the most recent turn even if it blows
            # the budget. The "current state" matters more
            # than a few hundred tokens of budget.
            if out and used + tok > budget:
                continue
            msg: dict[str, Any] = {
                "role": self._role_for_llm(turn.role),
                "content": turn.content,
            }
            # For tool calls, attach the structured form so
            # providers like OpenAI can use them in the next
            # turn. Each tool call is a dict with `id`,
            # `name`, and `args`. A real OpenAI integration
            # would emit a `function` wrapper; for now we
            # emit a flat structure that's easy for any
            # provider adapter to translate.
            if turn.role == Role.AGENT and turn.tool_calls:
                msg["tool_calls"] = turn.tool_calls
            if turn.role == Role.TOOL and turn.tool_results:
                # `tool_call_id` is the first result's id, or
                # a placeholder if missing. A real adapter
                # would map this to the provider's expected
                # field (e.g. `tool_call_id` for OpenAI).
                first = turn.tool_results[0]
                if isinstance(first, dict) and "id" in first:
                    msg["tool_call_id"] = first["id"]
            out.append(msg)
            used += tok
        out.reverse()
        return out

    def _role_for_llm(self, role: Role) -> str:
        """Map our `Role` to the LLM's role string.

        OpenAI uses: system, user, assistant, tool.
        Anthropic uses: similar.
        We map:
          USER   → user
          AGENT  → assistant
          SYSTEM → system
          TOOL   → tool
          SUMMARY → user (with a "Summary of earlier conversation:" prefix;
                         the actual prefix is in the content)
        """
        return {
            Role.USER: "user",
            Role.AGENT: "assistant",
            Role.SYSTEM: "system",
            Role.TOOL: "tool",
            Role.SUMMARY: "user",
        }[role]

    # ── Internal ──
    def _now(self) -> float:
        c = self.clock
        return c() if callable(c) else float(c)


# ── Factory ──────────────────────────────────────────────


def make_history(
    *,
    budget_tokens: int = 4000,
    max_turns: int = 200,
    clock: callable = time.time,
) -> ConversationHistory:
    """Convenience factory with the platform defaults."""
    return ConversationHistory(
        budget_tokens=budget_tokens,
        max_turns=max_turns,
        clock=clock,
    )


__all__ = [
    "ConversationHistory",
    "Role",
    "Turn",
    "estimate_tokens",
    "make_history",
]
