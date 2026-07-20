# Operator Dashboard

The human-facing view of the agent. The agent has many
moving parts — treasury, inbox, x402, heartbeat, plans,
SOUL — and the dashboard's job is to surface them all in
one place, in real time.

## Architecture

```
services/dashboard/stream.py     — EventBus, DashboardStream, StreamEvent
services/control_plane/routes/dashboard.py — REST + WebSocket routes
```

The two layers:

1. **`EventBus`** — an in-process pub/sub. Components
   publish events; the bus fans them out to subscribers.
2. **HTTP routes** — a REST endpoint to read the replay
   buffer (`GET /v1/dashboard/{aid}/events`), a WebSocket
   endpoint for real-time streaming (`WS /v1/dashboard/{aid}/stream`),
   and a publish endpoint for components that don't have
   a direct handle on the bus.

## Event kinds

The bus broadcasts a typed `StreamEvent` with a `kind`
from `EventKind`:

| Kind                  | When                                |
|-----------------------|-------------------------------------|
| `treasury_update`     | Balance changed                     |
| `tier_change`         | Survival tier flipped               |
| `inbox_update`        | Message arrived or processed        |
| `heartbeat`           | 1Hz liveness tick                   |
| `action_completed`    | A tool finished                     |
| `plan_created`        | A new plan was written to TODO.md   |
| `plan_completed`      | All steps done                      |
| `soul_updated`        | SOUL.md was rewritten               |
| `self_mod_request`    | A self-modification was proposed    |
| `self_mod_promoted`   | A self-modification was applied     |
| `self_mod_rejected`   | A self-modification was rejected    |

Components that emit these events:

- The runtime's `AutomatonLoop` emits `treasury_update`,
  `tier_change`, `action_completed` per tick.
- The `InboxService` emits `inbox_update` on send/claim.
- The `TodoService` emits `plan_created` and
  `plan_completed`.
- The `SoulService` emits `soul_updated`.
- The `SelfModificationEngine` emits `self_mod_*`.
- The `HeartbeatDaemon` emits `heartbeat` every second.

## Replay buffer

The bus keeps the last 100 events per agent in a
*replay buffer*. New WebSocket clients receive the
buffer on connect, so they don't miss what happened
before they connected. The buffer is in-memory only;
the audit log on disk is the durable history.

## Why an event bus instead of polling

Polling doesn't scale. A platform with 100 agents and a
1-second poll interval generates 8.6M requests/day per
operator. WebSocket pushes are how dashboards actually
work in production: the server pushes when something
happens, the client reacts.

## Why per-agent streams

A multi-agent platform has many agents. An operator
debugging agent A doesn't need to see agent B's events.
The per-agent model lets the operator pick what they
care about. A future turn could add a "global" stream
that fans in from all agents.

## 1Hz heartbeats

WebSocket connections can silently die (NAT timeouts,
proxy restarts). A 1Hz heartbeat lets the client detect
a dead connection within 2 seconds. The cost is a
small JSON message every second.

## What it enables

- **Live debugging.** An operator watches the agent
  work, in real time, without having to ssh into the
  box and tail a log.
- **Tier-change alerts.** A drop from `normal` to
  `critical` shows up immediately, not on the next
  polling cycle.
- **Multi-agent observability.** An operator managing
  50 agents can keep one dashboard tab per agent and
  see the live state of each.

## Future improvements

- **Aggregated metrics.** Per-second event counts,
  per-minute action histograms. The bus is event-
  shaped, not metric-shaped; a future turn would add
  a metrics layer on top.
- **Authentication.** Today the WebSocket is open. A
  real platform would require auth (token in the
  query string) and per-agent ACLs.
- **Backpressure.** A slow client can fill the bus's
  replay buffer. The current design drops events
  for slow subscribers; a future turn could push
  back to the publisher or rate-limit the client.
- **Cross-agent streams.** A "view all" mode for
  operators who manage fleets.
