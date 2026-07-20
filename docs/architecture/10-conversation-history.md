# Conversation History

The agent's multi-turn memory. Without it, the agent forgets
every tick — every interaction with a user or another agent
starts from a blank context. With it, the agent can hold a
real conversation: ask a follow-up question, recall what the
user said yesterday, build on prior reasoning.

## Why we need it

The LLM context window is finite (4K-128K tokens depending on
the model). The agent's runtime calls the LLM once per tick
with a system prompt + the current observation. Without a
history layer, the LLM sees *no* prior turns. The agent is
amnesiac.

A real multi-turn conversation needs:

1. **A record of what was said.** The user's last 10 questions
   and the agent's 10 responses.
2. **A bounded token budget.** A 100-turn conversation can be
   100K tokens; the agent's max context might be 8K.
3. **Recency bias.** Recent turns matter more than old ones.
4. **Summarization.** When the budget is tight, old turns
   collapse into a summary.

## Architecture

```
services/conversation/history.py — ConversationHistory, Turn, Role
runtime/loop/builtins.py        — chat.history.record / render / compact
runtime/loop/loop_init.py       — `history=` parameter on the builders
```

The history is a *platform-level* service, not an LLM-SDK
feature. We don't store OpenAI or Anthropic messages; we
store platform-native `Turn` records with a `Role` enum.
`render_for_llm()` adapts to the target LLM's format.

## Why format-agnostic

Different LLM providers have different message shapes. By
storing platform-native turns and rendering per-provider, the
same history serves any backend without re-parsing. The
mapping today:

| `Role`     | OpenAI / Anthropic |
|------------|--------------------|
| `user`     | `user`             |
| `agent`    | `assistant`        |
| `system`   | `system`           |
| `tool`     | `tool`             |
| `summary`  | `user` (with prefix in content) |

A real provider-specific adapter would handle tool-call
shape, message-id threading, etc. Today's `render_for_llm()`
emits the simplest possible shape: `{"role", "content"}`.

## Token budget

The `budget_tokens` parameter controls how much of the
history is rendered. The actual rendering walks turns
newest-to-oldest and includes each one until the budget is
exhausted. The *most recent* turn is always included, even
if it blows the budget — the current state matters more
than a few hundred tokens of budget.

The token estimate is a heuristic: `len(text) // 4` (the
"chars per token" rule of thumb). This is fast and good
enough for budget enforcement. A real deployment would
use a real tokenizer (tiktoken for OpenAI, etc.) for exact
counts.

## Compaction

When the history exceeds `budget_tokens * summary_threshold`
(default 80% of budget), `compact()` runs. The current
strategy is *extractive*:

1. Keep the last 5 turns verbatim.
2. Summarize all older turns into a single `SUMMARY` turn.
3. The summary is a bullet list of `(role, content)` pairs
   with content truncated to 200 chars.

This is a deliberately *simple* summarizer. A real LLM-backed
summarizer (e.g. "ask the LLM to summarize the conversation")
is added in a later turn.

The collapsed turns' ids are recorded in the summary's
`summary_of` field for audit. A real deployment would also
write the original turns to the audit log before discarding.

## Tools the agent has

| Tool                 | Description                                  |
|----------------------|----------------------------------------------|
| `chat.history.record`  | Append a turn to the history               |
| `chat.history.render`  | Render the history for an LLM call        |
| `chat.history.compact` | Force a compaction                       |

If an agent has no history wired, the tools raise
`RuntimeError` with a clear message.

## What it enables

- **Multi-turn chat.** A user can ask a question, get an
  answer, and ask a follow-up that references the prior
  answer. Without history, the agent has no idea what was
  said before.
- **Cross-tick reasoning.** The agent can refer back to a
  decision it made five ticks ago ("the user said X, so I
  should now do Y"). Without history, the LLM sees only
  the current observation.
- **Conversation replay.** An operator inspecting the
  history can see what the user said and what the agent
  answered, in order.

## Future improvements

- **LLM-backed summarization.** A small LLM call to compress
  old turns would produce much better summaries than
  extractive bullet lists.
- **Tool-call history in the message stream.** Today's
  `tool_calls` and `tool_results` are stored on the turn
  but not rendered into the message list. A real
  multi-tool agent needs to see what tools it called and
  what they returned.
- **Per-conversation history.** Today one history per
  agent. A real chat agent might have multiple parallel
  conversations (different users, different threads).
- **Persistence.** The history is in-process. A real
  deployment would persist to the SQLite store so the
  agent can recover from a restart.
