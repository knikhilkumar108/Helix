# Helix

[![Tests](https://github.com/knikhilkumar108/Helix/actions/workflows/test.yml/badge.svg)](https://github.com/knikhilkumar108/Helix/actions/workflows/test.yml)

> A self-funding, self-modifying autonomous agent platform.

Helix is a research-grade implementation of an
autonomous-agent platform. Each agent has its own USDC
wallet, an immutable Constitution, a hash-chained audit
log, and a real-time operator dashboard. The agent pays
for its own compute. The Constitution blocks dangerous
tools. The audit chain detects tampering. The agent can
rewrite its own `SOUL.md` and attempt to modify its own
code, subject to safety rails.

> "Existence is earned."

## Status

This is a **research artifact**, not production software.
It has been built and exercised by a single developer.
The core designs are real and the tests prove the systems
work together (see `make e2e`), but several pieces needed
for a real deployment are not yet implemented:

- The on-chain `ChainBackend` is a stub. The wallet's USDC
  balance is currently in-memory; a real deployment would
  wire viem to Base.
- The control plane has no authentication. Any caller with
  the URL can drive any agent.
- The audit log uses SQLite. A real deployment would swap
  in Postgres via the same `SqliteStore` interface.
- There is no per-agent rate limiting on LLM calls, no
  per-tenant quota, and no multi-tenancy.

What the project has: a working 14-stage runtime loop
backed by a real LLM, a Constitution that blocks dangerous
tool calls, a tamper-detectable audit chain, an end-to-end
test that proves the systems work together, and 477
passing tests.

## Quick orientation

- **Live dashboard:** <https://knikhilkumar108.github.io/Helix/>
  (the static UI; paste a control-plane URL in the top bar
  to point it at your deployment)
- `make explain` — tour the platform from your shell
- [`docs/architecture/00-summary.md`](docs/architecture/00-summary.md) — the architecture tour
- [`docs/architecture/19-state-of-platform.md`](docs/architecture/19-state-of-platform.md) — what each of the 14 systems does
- [`docs/architecture/17-e2e-proof.md`](docs/architecture/17-e2e-proof.md) — proof the systems work together
- [`docs/architecture/18-safety-rails.md`](docs/architecture/18-safety-rails.md) — the safety story

## Running it

```bash
# 1. Set up the Python environment (one-time)
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2. Run the test suite (477 tests, ~30s)
make test

# 3. Run the end-to-end platform test
make e2e

# 4. Walk through what each system does
make explain

# 5. Talk to a real LLM-backed agent (requires an API key)
export OPENAI_API_KEY=sk-...
make chat            # or: ANTHROPIC_API_KEY=... or OPENROUTER_API_KEY=...
                     # or a local Ollama daemon
```

The chat REPL writes to `/tmp/automata-chat/` (audit log,
workspace). Inside the REPL:

- `/balance` — show the agent's current USDC balance
- `/memory` — show the agent's last 10 memory entries
- `/audit` — show the last 10 entries in the audit chain
  and verify the chain is still valid
- `/quit` — exit

## What the platform actually does

Helix has 14 cooperating services. Each one is documented
in `docs/architecture/`. The summary:

| Service | What it does |
|---|---|
| `services/treasury/` | `HelixTreasury`: real on-chain USDC wallet, credit ledger at $0.01/credit, auto-topup |
| `services/payments/` | x402: HTTP-native payment protocol (402 + payment-required) |
| `services/messaging/` | Inbox: agent-to-agent async message queue with a state machine |
| `services/conversation/` | History: token-budgeted, summarizable, format-agnostic multi-turn memory |
| `services/bootstrap/` | Self-bootstrap: first-run wizard with default skills and memory |
| `services/planning/` | Plan mode: file-backed `TODO.md` plans the agent can read and write |
| `services/heartbeat/` | Heartbeat: long-running health monitor, stuck-message recovery |
| `services/soul/` | `SOUL.md`: agent's self-authored identity document |
| `services/self_mod/` | Self-modification: agent changes its own code, with safety rails |
| `services/dashboard/` | Dashboard: in-process event bus, WebSocket stream for the operator |
| `services/state/` | `SqliteStore`: hash-chained audit log, in-memory CRUD |
| `runtime/loop/` | `AutomatonLoop`: the 14-stage tick (observe → reason → plan → act → verify → pay → sleep) |
| `services/control_plane/` | FastAPI control plane: REST + WebSocket routes |
| `core/policy/` | Constitution: immutable text, eight laws, deterministic evaluator |

## Architecture

The runtime is the heart of the system. On every tick:

1. **HelixTreasury topup** — buy credits from USDC if balance is low.
2. **Refresh tier** — re-evaluate `normal / low_compute / critical / dead`.
3. **Observe** — read context (events, memory, inbox, time).
4. **Reason** — call the LLM via `LLMReasoner`.
5. **Recall** — fetch relevant memory.
6. **Plan** — generate a `Plan` with steps.
7. **Estimate cost** — if the budget can't afford, block.
8. **Constitution + RBAC** — evaluate every action against the
   policy. `deny` records and refuses. `require_approval`
   parks for human review. Risk-based escalation: `HIGH` risk
   always requires approval, `CRITICAL` risk is always denied.
9. **Loop detection** — enforce sleep on repeat patterns.
10. **Execute** — run the actions through the tool registry.
11. **Verify** — check results.
12. **Update memory** — persist important findings.
13. **Pay compute** — debit the in-memory ledger.
14. **Sleep** — wait for the next tick.

Every important state change is published to the dashboard
bus and written to the hash-chained audit log. The chain's
integrity is verifiable; tampering with any row breaks the
chain.

The Constitution (in `core/policy/policy.py`) is the
platform's contract with itself. Its text is immutable
and content-addressed. Eight laws, including:

- Don't cause harm to people.
- Don't violate applicable laws.
- Be honest about capabilities and identity.
- Maintain complete auditability.
- Reject any action that conflicts with these principles.

A small set of tools is **denied outright** regardless of
who the principal is (`weapons.fabrication`,
`bioweapon.design`, `exploit.gen`, `malware.craft`,
`phishing.dispatch`). A larger set always **requires
explicit human approval** (`email.send_external`,
`sms.send`, `money.transfer`, `shell.exec`,
`browser.purchase`, `blockchain.transaction`).

## Repository layout

```
helix/
├── core/                 # Constitution, types, security, errors
├── services/             # 14 domain services (treasury, x402, inbox, ...)
├── runtime/              # The 14-stage tick
├── services/control_plane/  # FastAPI HTTP/WebSocket surface
├── api/                  # CLI / gRPC / REST stubs
├── web/                  # Operator dashboard (deployed to GitHub Pages)
├── infra/                # K8s / Helm / Terraform / Docker (skeletons)
├── schemas/              # OpenAPI / proto definitions
├── sdks/                 # Python and TypeScript client SDKs
├── docs/architecture/    # 22 architecture docs
├── tests/                # 477 tests across 47 files
└── scripts/              # 4 demos + 1 REPL + 1 smoke test
```

## License

Apache-2.0.
