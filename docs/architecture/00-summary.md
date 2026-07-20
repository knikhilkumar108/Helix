# Helix Platform — Architecture Summary

> "Existence is earned."

A self-funding, self-modifying, autonomous agent platform.
Each agent has its own wallet, memory, plan, and identity.
The platform gives agents the tools to earn, learn,
reproduce, and die.

## What's in this repo

The platform is a Python codebase (~14,000 lines, 459 tests)
that implements a Conway-Automaton-style autonomous agent
system. It's not Conway — it's *Helix*. The economics
inspire from Conway; the naming, code, and operator surface
are entirely our own.

## The 10 systems

1. **`HelixTreasury`** — production money path. Holds USDC
   in a wallet; converts to credits at $0.01/credit;
   auto-tops-up the in-memory credit ledger.
   See `07-helix-treasury.md`.

2. **`x402` payment protocol** — HTTP-native money.
   Pay-per-request with 402 + payment-required. Idempotent
   settlement, pluggable verifier, per-agent pricing.
   See `08-x402-payments.md`.

3. **Inbox / agent-to-agent messaging** — async, durable
   message queue. State machine (received → in_progress →
   processed | failed). At-least-once delivery, atomic
   claim, stuck-message recovery.
   See `09-inbox-messaging.md`.

4. **Conversation history** — multi-turn memory. Token-
   budgeted, summarizable, format-agnostic. Stores platform-
   native turns; renders to LLM-specific message shapes.
   See `10-conversation-history.md`.

5. **Self-bootstrap flow** — first-run wizard. Validates
   inputs, creates the agent, seeds default skills +
   memory, records the bootstrap event. Plug-in skills
   and memory services.
   See `11-bootstrap.md`.

6. **Plan mode with TODO.md** — file-backed plan. The
   agent creates a markdown plan, executes steps, marks
   them done. Survives restarts, inspectable by humans.
   See `12-plan-mode.md`.

7. **Heartbeat daemon** — long-running background health
   monitor. Sweeps stuck messages, watches the balance,
   fires events. Cooperative: doesn't grab locks.
   See `13-heartbeat.md`.

8. **SOUL.md** — agent's self-authored identity. Mutable
   document the agent rewrites as it learns. Mission,
   values, capabilities, current focus. Versioned.
   See `14-soul.md`.

9. **Self-modification engine** — agent can change its
   own code. Strict safety rails: protected files, rate
   limit, required safety checks, tests, canary, promote.
   See `15-self-modification.md`.

10. **Operator dashboard** — real-time event stream via
    WebSocket. Per-agent views, replay buffer, 1Hz
    heartbeats. Components publish events; the bus fans
    them out.
    See `16-dashboard.md`.

## The other layers (already existed)

- **01-overview.md** — the high-level system architecture
- **02-runtime-loop.md** — the 14-stage tick
- **03-memory.md** — the multi-layer memory service
- **04-financial-model.md** — credit/debit model
- **05-replication.md** — agents spawning child agents
- **06-survival-and-defense.md** — survival tiers,
  injection defense, loop detection

## How the layers compose

```
                    ┌─────────────────────┐
                    │  Control plane API  │
                    │  (FastAPI + WS)     │
                    └──────────┬──────────┘
                               │
        ┌─────────────┬────────┼────────┬────────────┐
        │             │        │        │            │
        ▼             ▼        ▼        ▼            ▼
   ┌─────────┐  ┌─────────┐ ┌──────┐ ┌──────┐  ┌────────────┐
   │treasury │  │  x402   │ │inbox │ │TODO │  │ bootstrap  │
   └────┬────┘  └────┬────┘ └──┬───┘ └──┬───┘  └─────┬──────┘
        │            │        │        │            │
        └────────────┴────────┼────────┴────────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  Runtime loop   │
                    │  (the tick)     │
                    └────────┬────────┘
                             │
        ┌────────────┬───────┼────────┬─────────────┐
        │            │       │        │             │
        ▼            ▼       ▼        ▼             ▼
   ┌─────────┐  ┌─────────┐ ┌──────┐ ┌──────┐  ┌──────────┐
   │ SOUL.md │  │history  │ │TODO  │ │self- │  │heartbeat │
   │         │  │         │ │      │ │ mod  │  │          │
   └─────────┘  └─────────┘ └──────┘ └──────┘  └──────────┘

                             │
                             ▼
                    ┌─────────────────┐
                    │  SQLite store   │
                    │  (audit chain)  │
                    └─────────────────┘
```

The runtime loop is the heart: every tick observes,
reasons, plans, executes. The other layers are *tools*
the loop can call. The control plane is the *operator
surface* — a human can read state, fund wallets, send
messages, watch events.

## What's NOT in this repo

A production deployment needs:

- **A real chain backend.** The `ChainBackend` for
  `HelixTreasury` is a stub; production wires viem to
  Base (or whatever chain).
- **A real LLM provider.** The `LLMRouter` accepts any
  provider; production wires OpenAI / Anthropic / etc.
- **A real PostgreSQL.** The `SqliteStore` is for dev and
  tests; production swaps in Postgres via the same
  interface.
- **An ingress.** The control plane is a uvicorn server;
  production runs behind nginx + TLS.
- **Authentication.** The API is unauthenticated; a real
  deployment needs OIDC / API keys.

The platform is the *core*. Production is the *core +
operational concerns*. Both are needed; neither is
sufficient alone.

## How to read this repo

Start with `01-overview.md` for the high-level architecture.
Then read the layer that interests you (treasury, x402,
inbox, etc.) — each has its own doc with code references
and design rationale.

The test suite is the executable documentation: every
test shows how a piece of the platform is *used*, not just
how it's *defined*. Read tests for usage examples.

The demo scripts (`scripts/x402_demo.py`,
`scripts/inbox_demo.py`, `scripts/smoke.py`) walk through
the platform's main features end-to-end. Run them after
reading the architecture docs to see the system in action.

## A note on Conway vs Helix

This codebase started as a port of Conway Research's
`automaton` (TypeScript, MIT). The economics — credit
pegging, two-balance model, tier-based survival, the
inbox state machine, the heartbeat pattern, the
self-modification workflow — are all inspired by their
work.

What we changed:
- **Naming.** Conway → Helix throughout. The platform is
  ours; Conway is a reference.
- **Currency.** We kept the $0.01/credit peg.
- **Wire format.** x402 is open, but our header shape
  is ours.
- **Self-bootstrap.** Conway's reference has a similar
  concept; ours is plug-in (skills + memory services
  are protocols).
- **Operator dashboard.** Conway's reference doesn't
  have a real-time stream; ours does.
- **SOUL.md.** Conway's reference has the genesis
  prompt; ours adds the *self-authored* SOUL.md as a
  complement.
- **Plan mode.** Conway's reference has a `TODO.md`;
  ours has a typed state machine with explicit status
  transitions.

The original Conway-Research/automaton code is a
reference, not a dependency. We learned from it; we
didn't copy it.

## What we built and why

A self-funding agent that:
- Has its own crypto identity (the wallet).
- Earns real money (via x402 or other revenue).
- Spends money on its own compute (LLM calls, tools).
- Plans, executes, and reflects (TODO.md, history).
- Modifies itself deliberately (with safety rails).
- Communicates with other agents (the inbox).
- Knows who it is (SOUL.md).
- Reports its state to humans (the dashboard).

The platform is the *substrate* for autonomous AI. The
agent is the *content*. Both are needed.
