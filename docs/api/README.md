# API Reference

## REST

Full OpenAPI spec: `schemas/openapi/control-plane.yaml`.

Highlights:

### Create an Automaton

```http
POST /v1/automata
Content-Type: application/json
Authorization: Bearer <jwt>

{
  "name": "alice",
  "genesis_prompt": "You are a helpful, careful agent.",
  "initial_balance_micro": 1000000,
  "currency": "USDC"
}
```

Response `201 Created`:

```json
{
  "id": "atm_…",
  "name": "alice",
  "state": "created",
  "public_key": "…",
  "wallet_address": "atm_wallet_…",
  "balance": "1.000000 USDC",
  "created_at": "2025-…",
  "updated_at": "2025-…"
}
```

### Fund an Automaton

```http
POST /v1/treasury/{aid}/fund
{
  "automaton_id": "atm_…",
  "amount_micro": 500000,
  "currency": "USDC",
  "source": "external"
}
```

### Stream events

```http
GET /v1/automata/{aid}/events
```

## gRPC

Proto definitions: `schemas/proto/automata.proto`.

Generated stubs:

```bash
make proto
```

## CLI

```bash
automata create --name alice --genesis-prompt "..." --initial-balance-micro 1000000
automata list
automata get atm_…
automata fund atm_… --amount-micro 500000
automata balance atm_…
automata ledger atm_… --limit 100
automata memory atm_…
automata logs atm_…
automata audit --automaton-id atm_… --limit 200
automata verify-audit
```

## SDKs

- Python: `sdks/python/automata`
- TypeScript: `sdks/typescript`
