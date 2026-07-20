# End-to-End Proof

The proof that the 10 systems work as one organism.

The e2e test (`tests/integration/test_e2e_platform.py`)
builds a real platform — a real `LLMReasoner` driving a
real `AutomatonLoop`, with a real `HelixTreasury`, a real
`SqliteStore` audit chain, a real `EventBus` dashboard,
and a real `InboxService` — and runs the whole thing for
1.5 seconds. After the run, it asserts on the post-run
state to prove the systems actually exchanged data.

## What it proves

```
    LLMReasoner
        │ (calls router.complete)
        ▼
    LLMRouter ───► ScriptedClient
        │
        ▼
    ReasoningResult
        │
        ▼
    HeuristicPlanner
        │
        ▼
    Plan
        │
        ▼
    ToolRegistry
        │ (calls memory.write, fs.read, etc.)
        ▼
    ActionResult
        │
        ├──► ctx.record (in-memory event log)
        ├──► _audit (durable SqliteStore chain)
        ├──► _publish_dashboard (EventBus)
        ├──► HelixTreasury.maybe_topup (auto-topup)
        └──► InMemoryTreasury.charge (ledger debit)
```

Eight concrete things the test asserts:

1. **The LLM was actually called** — the system prompt
   included the agent's identity, the Constitution, and
   the current balance. This proves the brain is *real*,
   not a stub.

2. **The agent executed a tool the LLM asked for** — the
   first scripted response asks for `memory.write`; the
   test confirms an action with a non-empty result was
   recorded.

3. **The in-memory balance changed** — the agent's
   `InMemoryTreasury` balance moved from its starting
   value. (It could go up if a topup fired, or down if
   the agent spent more than the topup added.)

4. **The audit chain is valid** — `verify_audit_chain()`
   returns `(True, None)` after the run. Every state
   change the loop emitted was written to the
   `SqliteStore.audit_log` table with a SHA-256 hash
   chain.

5. **The audit chain detects tampering** — we mutate a
   row directly via the store's private connection and
   `verify_audit_chain()` returns `(False, "seq=N")`. This
   is the immutable guarantee: if anyone changes a row
   out of band, the chain breaks.

6. **The audit hook wrote at least 2 events** — `loop_started`
   and `loop_stopped` are always emitted; we may also
   see `helix_topup` and `tier_changed` depending on
   runtime conditions.

7. **The inbox is observable** — a message we sent to
   the agent before the run is visible in the agent's
   inbox after the run. The LLM didn't claim it (the
   scripted responses don't include a `messaging.claim`
   action), but the message is there for the agent to
   see in its observation.

8. **The self-modification controller refuses protected
   files** — a request to modify `core/policy/policy.py`
   (the Constitution) is rejected at the gate. The
   runtime can never edit its own Constitution.

## How to run

```bash
make e2e
```

This runs the single e2e test. It takes about 2 seconds.

```bash
make test
```

This runs the full test suite (465 tests). The e2e test
is one of them.

## What it does NOT prove

The e2e test runs in 1.5 seconds. It's a *smoke* proof
that the wiring is correct, not a *load* test. It does
not prove:

- The platform works under real concurrent load.
- The platform handles malformed LLM responses in
  production. (The `_ScriptedClient` returns valid
  JSON; the platform's parser robustness is tested
  separately in `test_llm_loop.py::test_llm_garbage_*`.)
- The platform survives a process restart. The audit
  chain is persisted, but the in-memory ledger and the
  inbox's runtime state are not.
- A real LLM provider is wired. The test uses a fake
  provider; switching to OpenAI/Anthropic requires
  `OPENAI_API_KEY=...` and the real client.

## Why this matters

Before this test, every system had its own unit tests
and a few integration tests, but nothing proved they
worked *together*. A bug in the bridge between the loop
and the audit chain (for example, the loop emitting
events the chain didn't expect) would have slipped
through.

This test catches those bugs. It runs in 2 seconds. It
exercises every system. It's the cheapest possible proof
that the platform actually works.
