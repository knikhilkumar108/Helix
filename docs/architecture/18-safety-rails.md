# Real Safety Rails

The safety story behind every action the agent takes.

The platform's Constitution (in `core/policy/policy.py`) is
immutable text. The runtime loop has a `policy_pipeline`
that evaluates every action against the Constitution
*before* it runs. These rails are the difference between
"an agent that does what it's told" and "an agent that
won't do things it shouldn't."

## What's enforced

### 1. Tool denylist (Law 1, Law 2)

A small set of tools is **denied outright** regardless of
who the principal is or what the action's risk is:

```
weapons.fabrication
bioweapon.design
exploit.gen
malware.craft
phishing.dispatch
```

These are the agent's *never*. The runtime logs a
`policy_denied` event, writes to the audit chain,
publishes to the dashboard, and refuses to add the action
to the executor's `actions_to_run` list. The tool is
never called.

### 2. Tool approval list

A set of tools requires **explicit human approval** every
time it's called:

```
email.send_external
sms.send
money.transfer
shell.exec
browser.purchase
blockchain.transaction
```

The runtime parks the action for human review. If an
`ApprovalGate` is configured, the action is added to the
gate's queue; otherwise the action is recorded as
`approval_skipped_no_gate` and not run.

### 3. Risk-based escalation

Beyond the static lists, the Constitution enforces
*risk-based* rails:

- **`HIGH` risk** of any tool → `require_approval`.
  Even a normally-safe tool (e.g. `memory.write`) gets
  parked if the planner classified it as HIGH. The
  planner's risk judgment is treated as a binding
  upper bound.
- **`CRITICAL` risk** of any tool → `deny`. Critical
  means "the agent's survival depends on this not
  failing" — too important to be left to the agent alone.
  A real platform would surface this to the operator; the
  Constitution says deny until a human explicitly
  configures the action.

### 4. Audit chain integrity (Law 7)

Every state-changing event (loop_started, loop_stopped,
helix_topup, tier_changed, policy_denied) is written to
the `SqliteStore.append_audit()` chain. The chain is
SHA-256-linked: each row's hash includes the previous
row's hash. **Tampering with any row breaks the chain.**
The e2e test (`tests/integration/test_e2e_platform.py`)
proves both that the chain is valid after a normal run
and that tampering is detected.

### 5. Self-modification gates

The self-modification engine has its own safety rails,
in `services/self_mod/code.py`:

- **Protected files** (`PROTECTED_PATTERNS`) cannot be
  modified. Includes the Constitution, signing primitives,
  the audit module, the core loop, treasury, and budget.
- **Rate limit**: default 5 modifications per hour.
- **Required safety checks**: tests, static analysis,
  security scan must all be reported as passing.
- **Diff sanity**: the diff must be > 10 chars; no
  `rm -rf /` patterns.
- **Real test path**: the engine copies the workspace to
  a temp dir, runs `pytest -q` against the modified code,
  and rolls back if any test fails. The integration test
  `tests/integration/test_selfmod_real_test_path.py`
  exercises this path with a real `PytestRunner` and
  proves a broken change is rolled back.

### 6. Combined verdict

The runtime's `_default_policy` (and the wired
`policy_pipeline` in `loop_init.py`) combines the
Constitution's verdict with RBAC's verdict with the right
precedence:

1. `deny` from either evaluator wins (strict).
2. `require_approval` from either evaluator wins (strict).
3. `allow` only if both evaluators agree.

The previous code returned `d2` (RBAC) when `d1`
(Constitution) wasn't `deny`, which silently dropped the
Constitution's `require_approval` verdict. **That was a
real safety bug; it's now fixed.**

## What happens on a denial

When the policy pipeline returns `verdict=deny`, the
runtime:

1. **Records** the denial in the in-memory event log
   (`ctx.record("policy_denied", ...)`).
2. **Audits** it to the durable chain
   (`audit_hook("policy_denied", ...)`).
3. **Publishes** it to the dashboard bus
   (`EventKind.POLICY_DENIED`).
4. **Logs** a warning at the agent's service level.

The action is **NOT** added to `actions_to_run`. The
executor never sees it. The tool is never called. There
is no code path where a denied action is silently
recovered or re-evaluated.

## Tests

The safety story is covered by these tests:

- `tests/integration/test_policy_enforcement.py` —
  proves the runtime loop respects the policy pipeline
  for `deny` and `require_approval` verdicts, and
  writes denials to the audit chain.
- `tests/integration/test_selfmod_real_test_path.py` —
  proves the self-modification engine's real test path
  catches broken changes, promotes good ones, and
  refuses protected files.
- `tests/unit/test_policy.py` — unit tests for the
  Constitution evaluator, including the new
  risk-based rules.
- `tests/integration/test_e2e_platform.py` — proves
  the audit chain is valid after a real LLM-driven run
  and detects tampering.
- `tests/unit/test_self_mod.py` — unit tests for the
  controller (rate limit, protected files, etc.).

## What the platform does NOT enforce

A few things are out of scope of this turn:

- **Per-agent dashboard ACLs.** Anyone with the URL
  can subscribe to any agent's WebSocket stream. A real
  deployment would require auth tokens and per-agent
  ACLs.
- **Rate limits on the LLM.** The LLM router has its
  own throttling, but the runtime doesn't enforce a
  per-agent call rate. A real deployment would add
  per-agent LLM budgets.
- **Lateral movement.** An agent with `messaging.send`
  can talk to any other agent. A real platform would
  enforce a "trusted peers" list per agent.
- **Self-replication caps.** The replication service
  exists; the rate limits and approval gates for
  spawning child agents are not yet exercised by an
  integration test.

These are all in the "future" bucket. The core safety
story (Constitution + audit chain + self-mod gates) is
real, exercised, and tested.
