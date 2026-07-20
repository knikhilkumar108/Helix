# Automata Platform — Architecture

> A decentralized operating system for autonomous, economically self-sustaining
> AI entities.

## 1. Goals

Each **Automaton** is a persistent digital entity that:

- Has its own cryptographic identity.
- Owns its own wallet and treasury.
- Pays for its own compute, storage, and API costs.
- Earns revenue from its own work.
- Learns and improves continuously.
- Evolves safely under a constitutional constraint.
- Can spawn autonomous children.

The creator's role is limited to:

1. Authoring the **Genesis Prompt**.
2. Funding the initial balance.
3. Optionally monitoring operation.

Everything else is autonomous.

## 2. Top-level components

```
                ┌──────────────────────────────────────────────┐
                │                  Tenants                     │
                └──────────────────────────────────────────────┘
                              │
                              ▼
        ┌───────────────────────────────────────────────┐
        │              Control Plane (REST/gRPC)        │
        │     Automata CRUD, lifecycle, funding, audit  │
        └───────────────────────────────────────────────┘
              │               │                │
              ▼               ▼                ▼
       ┌──────────┐     ┌──────────┐     ┌────────────┐
       │ Identity │     │ Treasury │     │  Memory    │
       └──────────┘     └──────────┘     └────────────┘
              │               │                │
              └──────────┬────┴────────┬───────┘
                         ▼             ▼
                ┌─────────────┐  ┌──────────────┐
                │ Planner     │  │  Marketplace │
                └─────────────┘  └──────────────┘
                         │             │
                         ▼             ▼
                  ┌──────────────────────────┐
                  │       Runtime Loop        │
                  │  Observe→Reason→Plan→Exec │
                  └──────────────────────────┘
                              │
                              ▼
                  ┌──────────────────────────┐
                  │   Tool / Sandbox Layer    │
                  │ process | container | μVM │
                  └──────────────────────────┘
```

## 3. Service decomposition

| Service              | Responsibility                                       | Port |
|----------------------|------------------------------------------------------|------|
| `control-plane`      | REST + gRPC front door, lifecycle, registry          | 8080 |
| `runtime-worker`     | Long-running loop for a single Automaton             | 8081 |
| `router`             | LLM provider routing (cost/quality aware)            | 9090 |
| `memory`             | Multi-layer memory service (BM25 + vector)           | 9100 |
| `marketplace`        | Offers, orders, settlement                           | 9200 |
| `security`           | Secrets, key rotation, audit chain                   | 9300 |
| `observability`      | Metrics, traces, health                              | 9400 |
| `replication`        | Spawning children, lineage, identity                 | 9500 |
| `planner`            | Plan generation, evaluation, ranking                 | 9600 |

All services are stateless behind a Postgres + Redis + S3 substrate. State
that must survive a process restart is in the database; transient state is
in Redis; durable artifacts (snapshots, signed logs, plugin artifacts) are
in S3.

## 4. Repository layout

```
core/        shared types, errors, config, events, security
runtime/     in-process runtime: loop, planner, tools, sandbox
services/    microservices (one per row of §3)
storage/     database migrations and object-store contracts
api/         REST, gRPC, CLI entrypoints
web/         operator dashboard
infra/       Kubernetes, Helm, Terraform, Docker, CI
schemas/     proto, OpenAPI, JSON
sdks/        Python + TypeScript client libraries
docs/        architecture, security, operations, diagrams
tests/       unit, integration, property, security
```

## 5. Design principles

- **Autonomous by default.** No required operator interaction after Genesis.
- **Economically self-sustaining.** Cost accounting is integral to the loop.
- **Cryptographically verifiable.** Every important action is signed.
- **Observable.** Metrics, traces, logs, and audit at every layer.
- **Sandbox-first.** Tools are isolated; least-privilege by default.
- **Zero-trust.** No component assumes trust in another.
- **Constitution-governed.** The Constitution is immutable and consulted
  for every action.

## 6. Service interaction model

Services communicate over three channels:

1. **Synchronous HTTP/gRPC** for control-plane requests.
2. **Postgres** for durable, transactional state.
3. **Event bus** (NATS / Kafka) for asynchronous fan-out.

The bus topics follow `automata.<entity>.<verb>` (e.g. `automata.action.completed`).
All events are content-addressed and signed.

## 7. Data flow for one loop tick

```
Observe
  ↓
Reason (LLM router)
  ↓
Retrieve memory (memory service)
  ↓
Generate plan (planner)
  ↓
Estimate cost (treasury + catalog)
  ↓
Constitution + RBAC/ABAC evaluation
  ↓
Execute (sandboxed tools)
  ↓
Verify (output schema + receipts)
  ↓
Learn (distill into procedural memory)
  ↓
Update memory
  ↓
Pay compute (treasury.charge)
  ↓
Update treasury snapshot (metrics + audit)
  ↓
Sleep
  ↓
Repeat
```

See `02-runtime-loop.md` for the per-stage contracts.
