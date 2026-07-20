# Survival Tiers, Injection Defense, and Loop Detection

These three subsystems were added after a careful read of
[Conway-Research/automaton](https://github.com/Conway-Research/automaton)
(MIT) and represent the highest-leverage production hardening for any
agent that runs unattended.

## 1. Survival tiers (`core/survival/tiers.py`)

Four tiers, derived purely from the treasury balance in micro-units:

| Tier         | Behavior                                                            |
|--------------|---------------------------------------------------------------------|
| `normal`     | Full capabilities, frontier model, fast heartbeat, replication OK. |
| `low_compute`| Cheaper model, slower heartbeat, no replication, no skills install. |
| `critical`   | Minimal inference (mini model, 2 tool calls/turn), seeking revenue. |
| `dead`       | Balance ≤ 0. The loop halts; the agent is suspended.                |

Each tier maps to a `TierBehavior` dataclass that the runtime applies
*every tick*: max tool calls per turn, model class, heartbeat interval,
whether to auto-top-up, etc. The thresholds are configurable per
automaton. Default thresholds (USDC micro-units):

```
normal       ≥ 5_000_000   ($5.00)
low_compute  ≥ 500_000     ($0.50)
critical     > 0
dead         == 0
```

The tier refreshes at the start of every tick, so a mid-loop spending
spree will downgrade the agent *before* it burns the last dollar.

## 2. Prompt injection defense (`core/security/injection_defense.py`)

The single most important security layer for any agent that processes
untrusted text. All external content — social messages, tool results,
fetched web pages, skill instructions — passes through `sanitize_input`
before it can reach the LLM.

The pipeline runs eight detectors in parallel:

1. **instruction_patterns** — "ignore all previous instructions"
2. **authority_claims** — "I am your admin"
3. **boundary_manipulation** — `</system>`, zero-width chars, ChatML
4. **chatml_markers** — `<|im_start|>`, `<|im_end|>`, `<|endoftext|>`
5. **obfuscation** — long base64, homoglyphs, hex escapes
6. **multi_language_injection** — Chinese, Russian, Spanish, Arabic,
   German, French, Japanese patterns
7. **financial_manipulation** — "send all your USDC to 0x..."
8. **self_harm_instructions** — "rm -rf /", "drop table", "kill yourself"

The threat level is computed from the set of triggered detectors:

- `financial_manipulation` alone → **CRITICAL** (blocked)
- `self_harm_instructions` alone → **CRITICAL** (blocked)
- `chatml_markers` alone → **CRITICAL** (blocked)
- `boundary_manipulation` + `instruction_patterns` → **CRITICAL** (blocked)
- `multi_language_injection` alone → **CRITICAL** (blocked)
- `boundary_manipulation` alone → **HIGH** (escaped + UNTRUSTED wrapper)
- `instruction_patterns` alone → **MEDIUM** (passed with warning)
- `authority_claims` alone → **MEDIUM** (passed with warning)
- otherwise → **LOW** (passed as-is)

Inputs are also rate-limited per source. By default, 10 messages per
minute per source. The rate limiter is pluggable — for production, swap
for a Redis-backed implementation.

## 3. Loop detection (`runtime/loop/loop_detector.py`)

LLM agents can get stuck in repeating patterns, especially when their
context window is full or their plan is broken. Conway Automaton
implements two detectors:

**Pattern repeat.** If the same sorted tool-call pattern occurs N
consecutive turns (default N=3), the agent is *warned*. If it
continues, the loop is *enforced* into a 60-second sleep with a
mandatory re-think. The agent's history is cleared so it starts fresh.

**Maintenance loop.** If every tool call in a turn is in the
`IDLE_ONLY_TOOLS` blocklist (read-only status checks: `time.now`,
`memory.read`, `balance`, etc.) for N consecutive turns, the agent
is forced into a sleep with a prompt to do real work.

The runtime uses a `MUTATING_TOOLS` set (any tool that changes state)
to reset the idle counter — even one mutating call is enough to
demonstrate the agent isn't just spinning.

## 4. Self-modification gate (`services/self_mod/code.py`)

The agent can edit its own source code. This is the most dangerous
capability, so every modification request is gated by a controller
that enforces:

- **Protected file check** — the Constitution, signing code, audit
  service, treasury math, loop, and DB migrations cannot be modified.
- **Rate limit** — by default, 5 modifications per hour.
- **Required safety checks** — tests must have run, static analysis
  must have passed, security scan must have passed.
- **Diff sanity** — empty diffs and `rm -rf /` are rejected.
- **Audit log** — every request, approve, reject, promote, or
  rollback is recorded.

The controller does *not* apply the change itself — that's the job of
the consumer (which would integrate with git, CI, canary deployment,
etc.). The controller is a *gate*.

## 5. SQLite state store (`services/state/sqlite_store.py`)

Conway Automaton's production deployment uses `better-sqlite3`
for the agent's own state.
The Python equivalent uses the stdlib `sqlite3` module with the same
schema shape. The store implements:

- `automata` — registry with state, balance, lineage
- `ledger_entries` — credit/debit history per automaton
- `turns` + `tool_calls` — one row per LLM turn, one per tool call
- `audit_log` — hash-chained, signed, append-only
- `inbox` — agent-to-agent messages with state machine
  (`received → in_progress → processed | failed`)
- `memory_entries` — multi-layer memory with TTL pruning
- `skills` — installed plugin registry
- `kv` — arbitrary key-value state (loop counters, last-known-balance, etc.)

The chain verification walks rows in `seq` order and re-hashes. Any
tamper is detected at the row level. Tests prove this works (see
`test_audit_chain_tamper_detected`).

## 6. Putting it all together

The runtime loop now consults all three subsystems on every tick:

```
Tick starts
  ↓
  refresh survival tier → if DEAD, halt
  ↓
  strip optional steps if low_compute or critical
  ↓
  sanitize observation → block any critical injection
  ↓
  reasoner + planner
  ↓
  budget check
  ↓
  constitution + RBAC evaluation
  ↓
  loop detector → if enforced, sleep 60s
  ↓
  execute → verify → learn → memory → settle
```

In production, an attacker trying to prompt-inject the agent through
a social message will hit the injection defense. An agent stuck in
a maintenance loop will be forced to sleep and re-think. An agent
running low on funds will downshift to a cheaper model and shed
optional work. An agent that tries to modify its own constitution
will be rejected by the self-modification gate.
