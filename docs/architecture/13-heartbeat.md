# Heartbeat Daemon

The agent's long-running background health monitor. The
runtime loop is *event-driven* (it ticks when there's
work); the heartbeat is *time-driven* (it runs every N
seconds regardless of activity).

## Why we need it

A long-running autonomous agent has three failure modes
the tick can't handle on its own:

1. **Mid-process crashes.** The agent claims an inbox
   message, then crashes. The message stays in
   `in_progress` forever. The heartbeat sweeps stuck
   messages back to `received`.

2. **Tier transitions without a tick.** The agent's
   balance drops to zero while the tick is sleeping.
   No tick fires, so the agent doesn't know it's dead.
   The heartbeat watches the balance and reports the
   tier.

3. **Memory bloat.** The agent's memory grows over time.
   The heartbeat triggers compaction.

4. **Stale peer lists.** The agent needs to know who
   else is alive to delegate work. The heartbeat
   refreshes the peer list periodically.

## What the daemon does

The daemon runs a list of `HealthCheckFn` objects on a
fixed interval (default: 60 seconds). Each check is
async, idempotent, and fast. The daemon aggregates the
results into a `HeartbeatReport` and (optionally)
fires an `on_report` callback for downstream consumers.

The platform ships with two built-in checks:

- **`InboxSweepCheck`** — calls
  `InboxService.reset_stuck(stuck_for_seconds=300)`.
  Reports `CRITICAL` if any messages were reset, `OK`
  otherwise. The 5-minute threshold assumes the agent's
  longest action is a few minutes; tune for your
  workload.

- **`CreditMonitorCheck`** — reads the agent's balance
  via a callable and reports the tier. The check
  doesn't *act* on the tier (the runtime does that);
  it just makes the state observable to the operator.

A real platform would add:

- `MemoryCompactionCheck` — triggers `MemoryService.compact()`.
- `PeerDiscoveryCheck` — refreshes the peer list.
- `WalletHealthCheck` — verifies the wallet backend is
  reachable.
- `LLMHealthCheck` — verifies the LLM router is responding.

## Architecture

```
services/heartbeat/daemon.py     — HeartbeatDaemon, HealthCheck, HealthStatus
services/heartbeat/__init__.py   — public surface
```

## Lifecycle

```python
daemon = HeartbeatDaemon(
    automaton_id=aid,
    interval_seconds=60.0,
    checks=[InboxSweepCheck(...), CreditMonitorCheck(...)],
    on_report=my_async_callback,
)
await daemon.start()       # spawns a background task
# ... do work ...
report = daemon.last_report  # read the latest
await daemon.stop()        # cancels and joins
```

The daemon is *cooperative*:

- A check that throws is logged and reported as `WARN`.
  The daemon continues on the next interval.
- The daemon doesn't grab locks. Checks read state; the
  runtime writes it. The heartbeat is observably
  consistent without synchronization.
- The daemon can outlive a tick. If the tick is
  blocked, the heartbeat keeps running.

## Failure isolation

A transient failure (RPC timeout, network blip) is
caught and logged. The daemon does not crash the
platform. The failure is reported in the next
`HeartbeatReport` as a `WARN`-status check.

A *permanent* failure (the wallet backend is broken) is
reported as `CRITICAL`. The operator can decide what
to do — pause the agent, restart it, fix the wallet.

A *fatal* failure (the agent is dead) is reported as
`DEAD`. The operator can re-fund the wallet or mark
the agent for termination.

## Threading and asyncio

The daemon runs as an `asyncio.Task` alongside the
runtime loop. Both share the same event loop. The
daemon's `run_once()` is async; the daemon's
`_run_forever()` awaits `asyncio.sleep(interval)` and
catches `asyncio.CancelledError` to support clean
shutdown.

## What it enables

- **Self-healing.** A crashed-mid-process message is
  automatically retried.
- **Operator visibility.** The latest `HeartbeatReport`
  is always available; the operator dashboard polls it.
- **Tier monitoring.** The heartbeat makes the agent's
  tier visible even when the tick is asleep.
- **Graceful degradation.** If the LLM is down, the
  heartbeat can still report the agent's tier and inbox
  state — the operator sees "agent is alive but unable
  to think".

## Future improvements

- **Adaptive intervals.** Slow down the heartbeat when
  the agent is in `normal` tier; speed it up when in
  `critical` or `dead`.
- **Webhook notifications.** The `on_report` callback
  today is in-process. A real deployment would also
  push to Slack / PagerDuty on `CRITICAL` or `DEAD`.
- **Per-check history.** Store the last N reports per
  check for trend analysis ("the inbox is filling up
  faster than the agent can process").
- **Self-tuning thresholds.** The `stuck_for_seconds`
  threshold is a magic number today. A real deployment
  would tune it from the agent's own action-duration
  history.
