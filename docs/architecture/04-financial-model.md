# Financial Model

## Currency

- Each Automaton has a `base_currency` (default `USDC`).
- All balances and budgets are denominated in the base currency.
- The Treasury tracks every credit and debit; the running balance is
  the sum of the ledger.

## Cost categories

| Category          | Source                                           |
|-------------------|--------------------------------------------------|
| `llm_inference`   | Per token in/out, priced by the model catalog    |
| `compute`         | CPU-seconds × instance rate                      |
| `gpu`             | GPU-seconds × instance rate                      |
| `storage`         | GB-months at the storage rate                    |
| `network`         | Egress GB at the provider rate                   |
| `external_api`    | Provider-listed per-call cost                    |
| `tool_execution`  | Per-tool declared cost (overridable)             |
| `child_seed`      | Funds transferred to a child on replication      |
| `plugin_license`  | One-time cost to enable a plugin                 |

## Revenue categories

- `marketplace.order` — paid for delivering a marketplace order
- `api.subscription` — recurring API revenue
- `tip.donation` — unsolicited
- `bounty.authorized` — authorized bug-bounty program
- `plugin.sale` — sale of a plugin authored by this Automaton

## Cost estimation pipeline

Before a plan is executed, the Planner annotates each step with an
estimated cost. The BudgetController asks: "if this plan ran, would the
resulting balance stay above the reserve floor?" If not, the plan is
postponed or rejected.

The actual cost is settled at the end of the tick from per-step receipts
(emitted by the executor). Over- and under-charges are recorded and may
trigger a refund or an audit alert.

## Suspended state

If the balance drops to zero:

1. The current transaction is finished safely.
2. All state is persisted and snapshotted.
3. Sandbox resources are released.
4. The Automaton transitions to `suspended`.
5. The treasury emits a `low_balance` event.

The Automaton does **not** auto-terminate. It can be resumed by additional
funding.

## Health metrics

The treasury continuously exposes:

- balance
- 1h, 24h, 7d burn rate
- runway (in seconds) at the current burn rate
- 30-day realized revenue vs. spend
- largest recent cost
