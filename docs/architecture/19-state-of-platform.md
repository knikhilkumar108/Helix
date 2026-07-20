# State of the Helix Platform

> A self-funding, self-modifying, autonomous agent platform.
> Each agent has its own wallet, memory, plan, identity,
> and Constitution. The platform gives agents the tools to
> earn, learn, reproduce, and die.

This document is a tour of what the platform is, what
each piece does, and how they fit together. It's written
for someone reading the codebase for the first time.

## Numbers

- **Source code:** ~25,800 lines of Python across ~150 files
- **Tests:** 477 passing tests across 47 test files (~30s)
- **Docs:** 22 architecture docs in `docs/architecture/`
- **Demos:** 4 standalone demo scripts + 1 REPL + 1 e2e test
- **External deps:** FastAPI, Pydantic, OpenTelemetry, httpx
- **Run-time deps:** SQLite (built-in), asyncio (built-in)

## The 14 systems

The platform is organized as 14 cooperating services. Each
one has a clear responsibility and a small public surface.

### 1. `services/treasury/` — HelixTreasury

**The agent's wallet.** Holds USDC, converts to credits at
$0.01/credit, auto-tops-up the in-memory credit ledger.

- `HelixTreasury` — production treasury (USDC on Base).
- `MockBackend`, `CustodialBackend`, `ChainBackend` — three
  wallet backends, with `MockBackend` for dev and tests.
- `TopupPolicy` — when to top up (NEVER, ON_LOW,
  ON_CRITICAL, ALWAYS).
- `TopupEvent` — audit record for each top-up.

The runtime loop calls `maybe_topup()` on every tick.
The topup runs only if the agent's USDC balance is above a
floor, the policy says so, and the rate limit isn't hit.

### 2. `services/payments/` — x402

**The HTTP-native payment protocol.** A request to a paid
endpoint gets a 402 with payment instructions; the client
pays on-chain and retries with proof.

- `X402Service` — issues invoices, settles payment proofs.
- `PaymentVerifier` (Protocol) — pluggable on-chain verifier.
- `MockVerifier` — accepts any well-formed `0x…` hash (for tests).
- `PaymentRegistry` — in-memory store of issued invoices and
  received receipts.
- `PaymentRequired` — exception signaling a 402 is needed.

The control plane exposes `POST /v1/x402/{aid}/pay/{resource}`
as a demo paid endpoint. The full round-trip (issue →
client pays → settle → credit wallet) is the test.

### 3. `services/messaging/` — Inbox

**Agent-to-agent async messaging.** A durable message queue
with a state machine (received → in_progress → processed | failed).

- `InboxService` — typed façade over `SqliteStore`.
- `InboxMessage` — a single message.
- `InboxState` — the state machine.
- `InboxFull` — raised when the inbox is at the cap.
- `reset_stuck()` — sweeps `in_progress` messages back to
  `received` after a TTL. Called by the heartbeat.

Built-in tools: `messaging.send`, `messaging.claim`,
`messaging.mark_processed`, `messaging.mark_failed`.

### 4. `services/conversation/` — History

**The agent's multi-turn memory.** Token-budgeted,
summarizable, format-agnostic.

- `ConversationHistory` — the history with budget, max turns,
  summary threshold.
- `Role` — user, agent, system, tool, summary.
- `Turn` — a single turn with optional tool calls and results.
- `estimate_tokens()` — heuristic 1 token ≈ 4 chars.

The LLM reasoner reads the history and renders it to the
provider's message format. The budget keeps the prompt
under the model's context window.

### 5. `services/bootstrap/` — Self-Bootstrap

**The agent's first-run experience.** Validates inputs, creates
the agent, seeds default skills and memory.

- `BootstrapService` — the orchestrator.
- `BootstrapRequest` / `BootstrapResult` — value types.
- `DEFAULT_SKILLS` — the default skill set
  (`fs.read`, `fs.write`, `memory.read`, `memory.write`, etc.).
- `DEFAULT_INTRO_MEMORY` — the intro note seeded into the
  agent's memory.

Wired into `POST /v1/automata`. The control plane falls
back to a plain `registry.create()` if no `BootstrapService`
is configured.

### 6. `services/planning/` — Plan Mode

**The agent's TODO.md.** File-backed plans the agent can
read, write, and update.

- `TodoService` — owns `TODO.md`.
- `TodoPlan` / `TodoStep` / `TodoStatus` — value types.
- `LocalTodoFileSystem` (Protocol) — pluggable filesystem.
- State machine: pending → in_progress → succeeded | failed.

Built-in tools: `plan.create`, `plan.mark_step`, `plan.read`.

### 7. `services/heartbeat/` — Heartbeat

**Long-running background health monitor.** Sweeps stuck
messages, watches the balance, fires events.

- `HeartbeatDaemon` — runs checks on a fixed interval.
- `HealthCheckFn` (Protocol) — pluggable checks.
- `InboxSweepCheck` — calls `InboxService.reset_stuck()`.
- `CreditMonitorCheck` — reports the agent's tier.
- `HealthStatus` — OK / WARN / CRITICAL / DEAD.
- `HeartbeatReport` — the result of one cycle.

The daemon is cooperative: doesn't grab locks; failures
are logged but never crash.

### 8. `services/soul/` — SOUL.md

**The agent's self-authored identity document.** Mission,
values, capabilities, current focus, self-notes.

- `SoulService` — owns `SOUL.md`.
- `SoulDocument` / `SoulSection` — value types.
- Versioned: every `update_section()` bumps the version.
- The agent reads and rewrites SOUL.md as it learns.

### 9. `services/self_mod/` — Self-Modification

**The agent can change its own code.** With strict safety
rails.

- `SelfModController` (in `code.py`) — the gatekeeper.
  Refuses protected files, rate-limits, requires safety
  checks.
- `SelfModificationEngine` (in `engine.py`) — the workflow
  orchestrator. Drives `propose → review → edit → test →
  canary → promote`.
- `PytestRunner` / `StaticTestRunner` / `ImportCanary` —
  pluggable runners.
- `PROTECTED_PATTERNS` — the immutable list. Includes the
  Constitution, signing, audit, treasury, and the loop itself.

### 10. `services/dashboard/` — Operator Dashboard

**Real-time event stream via WebSocket.** Per-agent views,
replay buffer, 1Hz heartbeats.

- `EventBus` — in-process pub/sub. Per-agent subscribers, replay
  buffer, drop-on-overflow for slow clients.
- `DashboardStream` — façade with `make_event` and `publish`.
- `StreamEvent` / `EventKind` — typed events.
- Control plane routes: `GET /v1/dashboard/{aid}/events`,
  `WS /v1/dashboard/{aid}/stream`, `POST /v1/dashboard/{aid}/events/publish`.

### 11. `services/state/` — SqliteStore

**The hash-chained audit log.** Every state-changing event
is written here.

- `append_audit()` — append a row, hash includes the previous
  row's hash.
- `verify_audit_chain()` — walk the chain, return `(True, None)`
  or `(False, "seq=N")` on tampering.
- Schema covers automata, ledger, plans, tasks, actions,
  turns, tool calls, memory, audit, inbox, skills, kv.

The `InboxService` uses the same store for messages.
The `HelixTreasury`'s topup events could go here too
(currently they don't — see "future improvements").

### 12. `runtime/loop/` — AutomatonLoop

**The runtime. The 14-stage tick.**

The tick sequence:
1. **HelixTreasury topup** — buy credits from USDC.
2. **Refresh tier** — re-evaluate `normal / low_compute / critical / dead`.
3. **Observe** — read context (events, memory, inbox, time).
4. **Reason** — call the LLM via `LLMReasoner`.
5. **Recall** — fetch relevant memory.
6. **Plan** — generate `Plan` with steps.
7. **Estimate cost** — if budget can't afford, block.
8. **Constitution + RBAC** — evaluate every action against the
   policy. `deny` records and refuses. `require_approval`
   parks.
9. **Loop detection** — enforce sleep on repeat patterns.
10. **Execute** — run the actions through the tool registry.
11. **Verify** — check results.
12. **Update memory** — persist important findings.
13. **Pay compute** — debit the in-memory ledger.
14. **Sleep** — wait for the next tick.

The loop has pluggable `dashboard` and `audit_hook` — every
important event is published and persisted.

### 13. `services/control_plane/` — FastAPI

**The HTTP/WebSocket surface.**

Routes:
- `/v1/automata` — CRUD and lifecycle.
- `/v1/treasury/{aid}/balance`, `/fund` — wallet ops.
- `/v1/memory/{aid}` — memory CRUD.
- `/v1/approvals` — human-in-the-loop gate.
- `/v1/audit` — read the audit chain.
- `/v1/x402/{aid}/pay/{resource}` — paid endpoint.
- `/v1/inbox/{aid}/messages`, `/send` — inbox HTTP.
- `/v1/dashboard/{aid}/events`, `/stream` (WS) — dashboard.
- `/healthz`, `/readyz`, `/metrics` — health and observability.

### 14. `core/` — The Constitution and Security

**The unchanging parts.** These are not services; they're
the platform's contract with itself.

- `core/policy/policy.py` — the Constitution (immutable text),
  the `ConstitutionEvaluator` (deterministic, side-effect free).
  Eight laws: don't harm, don't break the law, don't bypass
  auth, respect privacy, be honest, preserve yourself, audit
  everything, reject conflicts.
- `core/policy/rbac.py` — RBAC/ABAC. Roles: `operator`,
  `creator`, `admin`. The agent's principal is itself with
  `operator` role.
- `core/security/injection_defense.py` — heuristic pattern
  matching to detect prompt injection in tool results.
- `core/security/signing.py` — Ed25519-style signing for the
  audit chain.
- `core/types/money.py` — `Money(micro, currency)`. Micro-USDC,
  6 decimal places. Immutable, comparable.
- `core/types/identifiers.py` — `_Id(str)` subclasses for
  `AutomatonId`, `TaskId`, etc. Validated format.

## The data flow

```
   user (chat / dashboard / control plane)
                    │
                    ▼
            ┌───────────────┐
            │ Control plane │  FastAPI + WebSocket
            └───────┬───────┘
                    │
        ┌───────────┼───────────┐
        │           │           │
        ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐
   │treasury │ │  x402   │ │inbox    │  HTTP-level services
   └────┬────┘ └────┬────┘ └────┬────┘
        │           │           │
        └───────────┼───────────┘
                    │
                    ▼
            ┌───────────────┐
            │ Runtime loop  │  The tick
            └───────┬───────┘
                    │
        ┌───────────┼───────────┬───────────┐
        │           │           │           │
        ▼           ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
   │ SOUL.md │ │history  │ │ TODO.md │ │self-mod │
   └─────────┘ └─────────┘ └─────────┘ └─────────┘
                                                 
                    │
                    ▼
            ┌───────────────┐
            │  SQLite store  │  audit chain
            └───────────────┘
```

The user (a human operator, an HTTP client, or another
agent) talks to the control plane. The control plane
manages the agent's lifecycle, treasury, x402 service,
inbox, and dashboard. The runtime loop is the heart: every
tick observes, reasons, plans, executes, and persists.

The "side" services (SOUL, history, TODO, self-mod) are
tools the runtime uses. The store is the durable record.

## What works today

- A real LLM (OpenAI, Anthropic, Ollama, OpenRouter) can
  drive the loop via `LLMReasoner` and `LLMRouter`.
- The agent pays for its compute with the in-memory ledger.
- The HelixTreasury auto-tops-up from the wallet's USDC.
- The agent can send messages to other agents via the inbox.
- The agent can earn via x402.
- The agent can write to memory and read it back.
- The agent can create a plan in TODO.md and execute it.
- The agent can rewrite its own SOUL.md.
- The agent can attempt to modify its own code (subject to
  the safety rails).
- The Constitution enforces tool denylists, approval lists,
  and risk-based escalation.
- The audit chain is SHA-256-linked and tamper-detectable.
- The dashboard streams events to a WebSocket in real time.

## What doesn't work yet (intentionally)

- **Real chain integration.** `ChainBackend` is a stub.
  Production wires viem to Base.
- **Postgres.** `SqliteStore` is for dev and tests. Production
  swaps in Postgres via the same interface.
- **Auth.** The control plane is unauthenticated. Production
  needs OIDC / API keys.
- **Per-agent dashboard ACLs.** Anyone with the URL can
  subscribe. Production needs auth tokens.
- **Replication caps.** The replication service exists;
  rate limits and approval gates for spawning child agents
  are not yet exercised by an integration test.

## Where to start reading

- **`docs/architecture/00-summary.md`** — the high-level
  overview (you are here).
- **`docs/architecture/01-overview.md`** — the system
  architecture diagram.
- **`docs/architecture/02-runtime-loop.md`** — the 14-stage
  tick in detail.
- **`docs/architecture/17-e2e-proof.md`** — the e2e test
  that proves the platform works as one system.
- **`docs/architecture/18-safety-rails.md`** — the safety
  story.
- **`make explain`** — quick orientation from the shell.

## Try it

```bash
make test       # run the test suite (~30s)
make e2e        # run the end-to-end test
make chat       # chat with a real LLM-backed agent
```

`make chat` requires an `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`, or a local Ollama daemon. The
session writes to `/tmp/automata-chat/` (audit log, workspace).
Type `/audit` to see the chain, `/memory` to see the agent's
memory, `/quit` to exit.
