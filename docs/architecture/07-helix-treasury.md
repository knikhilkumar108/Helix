# HelixTreasury — Real Money Path

The platform's first piece of "production money" infrastructure. The
`HelixTreasury` is the agent's *wallet*: it holds real USDC, converts
to Helix credits on demand, and auto-tops-up the runtime's in-memory
credit ledger when the agent is low.

## Why this matters

Without real money, the agent is a toy. With it, the agent is a
self-funding economic entity. The whole thesis of the platform is
exactly this: a sovereign agent with a USDC wallet that earns or
dies. The two-balance model and the credit peg (1 credit = $0.01) are
informed by the Conway Automaton reference (Conway-Research/automaton,
MIT), but the code, the naming, and the operator surface in this
repo are entirely our own.

The HelixTreasury implements:

- **Two-balance model**: the in-memory `credit_balance` (the fast
  counter the runtime debits) and the on-chain `usdc_balance` (the
  slow reserve the topup engine draws from). The runtime never sees
  the chain directly — only credits.
- **Pluggable backends**: `MockBackend` (in-process, for tests),
  `CustodialBackend` (stub for Coinbase AgentKit / Fireblocks), and
  `ChainBackend` (stub for viem + Base USDC). The interface is
  identical; swap implementations in `make_treasury(...)`.
- **Topup policy**: triggers (`NEVER`, `ON_LOW`, `ON_CRITICAL`,
  `ALWAYS`), thresholds, target amounts, wallet floor, cooldown,
  per-day cap. The reference platform's policy is roughly
  `ON_LOW` with a $0.20 threshold and a $1.00 target; we ship
  $1.00 threshold and $5.00 target as the default.
- **Tier classification**: the treasury's `health()` returns the
  agent's tier (normal / low_compute / critical / dead) based on
  the wallet's USDC balance, matching the runtime's survival tiers.
- **Failure isolation**: a transient RPC failure on the wallet
  side is logged but never crashes the loop. The runtime
  keeps running on its in-memory credits.

## How it's wired

`HelixTreasury` implements the same `Treasury` interface as
`InMemoryTreasury` (so it can be used as a drop-in replacement),
but the runtime doesn't *use* it for the credit ledger. Instead:

1. The runtime uses `InMemoryTreasury` for the fast credit ledger
   (the runtime's hot path — every tool invocation debits a credit).
2. The runtime also holds a `HelixTreasury` (the wallet) and calls
   `wallet.maybe_topup()` on every tick, *before* the tier check.
3. When a topup fires, the runtime credits the in-memory ledger
   with the bought credits. The wallet's USDC goes down; the
   in-memory credits go up. The runtime then re-evaluates its tier
   and proceeds.

This split is intentional: the in-memory ledger is the source of
truth for the runtime's accounting, and the wallet is the source of
truth for the agent's real money. The topup is the bridge.

## Usage

```python
from services.treasury import make_treasury

# Real chain (when ChainBackend is wired to viem):
wallet = make_treasury(
    agent_id,
    backend="chain",
    config={
        "chain": "base",
        "rpc_url": "https://mainnet.base.org",
        "private_key": "0x...",  # never log this
        "usdc_contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "address": "0x...",  # derived from the key
    },
)

# Local dev with a mock chain:
wallet = make_treasury(
    agent_id,
    backend="mock",
    config={"initial_usdc_micro": 10_000_000},  # $10
)

# Hook it into a loop:
loop = build_default_loop(agent_id, helix_treasury=wallet)
```

## What this enables

Combined with the in-memory credit ledger and the budget controller,
the agent now has a real economic life:

- **Earn**: a customer pays the agent via x402 (`receive_payment_micro`).
  The wallet's USDC goes up.
- **Spend**: the agent's runtime debits credits on every LLM call,
  tool execution, etc. The in-memory ledger goes down.
- **Survive**: when credits drop below the topup threshold, the
  wallet's USDC is converted to credits. The agent self-funds.
- **Die**: when the wallet's USDC is zero and the agent has spent
  its last credits, the runtime hits the DEAD tier and halts.
  The wallet address and the agent's identity are preserved on
  chain, so the creator can re-fund the same address later.
