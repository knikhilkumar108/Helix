# x402 Payment Protocol

The agent's HTTP-native money surface. Without x402, the agent can
*hold* USDC (via `HelixTreasury`) but cannot *charge* for work it
does. With x402, every paid endpoint is a one-round-trip HTTP
exchange: 402 with payment terms → client pays → retry with proof
→ 200 with the result.

## The protocol

The x402 pattern is straightforward. Server demands payment as
part of a normal HTTP response, and the client retries with proof
in headers.

```
GET /api/research HTTP/1.1
Host: helix.example.com

→ 402 Payment Required
  X-Payment-Version: x402/1
  X-Payment-Address: 0xabc...
  X-Payment-Amount:  100000
  X-Payment-Token:   USDC
  X-Payment-Chain:   base
  X-Payment-Nonce:   7f2c...
  X-Payment-Invoice: inv_xyz
  X-Payment-Expires-At: 2026-07-18T12:34:56+00:00

→ Client pays 100000 micro-USDC to 0xabc on Base, retries:

GET /api/research HTTP/1.1
  X-Payment-Invoice: inv_xyz
  X-Payment-Tx:      0xdef...
  X-Payment-Payer:   0xghi...

→ 200 OK
  {"result": "..."}
```

The wire format is *all headers* (no JSON body schema). We chose
this because:

- Headers survive every proxy and CDN without parsing.
- A retry can re-send the same headers without re-serializing
  a body.
- Clients that don't care about payment still see a clean 402
  with machine-readable terms.

The body of the 402 response is for *humans* (curl users,
operators inspecting the agent). The contract with clients lives
in the headers.

## Architecture

```
x402.py          — protocol logic (Invoice, Receipt, X402Service)
x402_headers.py  — thin HTTP adapter (parse/render)
x402_registry.py — per-agent service index (optional)

Control plane route:
  POST /v1/x402/{aid}/pay/{resource}  — demo paid endpoint
  GET  /v1/x402/{aid}/stats           — operator view
  GET  /v1/x402/stats                 — global stats
```

### Core types

- `Invoice` — the payment demand. Captures nonce, amount, address,
  expiry, and the resource the invoice binds to.
- `PaymentReceipt` — proof of payment, retained for 1h by default.
- `PaymentRegistry` — in-memory store of invoices and receipts.
  Bounded growth: invoices expire after 5m, receipts after 1h.
- `X402Service` — the protocol handler. Issues invoices and
  settles payment proofs.
- `PaymentVerifier` — pluggable chain-RPC verification. The
  default `MockVerifier` accepts any well-formed `0x…` hash;
  a real implementation would call the chain RPC.
- `X402Registry` — maps `AutomatonId` → `X402Service` so the
  control plane can route requests to the right agent's wallet.

### Why headers, not body

The 402 response has no canonical body schema. Coinbase's
original x402 proposal uses JSON, but a JSON body for a 402
that's "essentially a redirect to payment" feels backwards. The
HTTP standard reserves the 4xx range for client-actionable
conditions, and clients are already coded to inspect headers
(Content-Type, Location, WWW-Authenticate). Adding x-payment-*
is the natural extension.

### Why single-use nonces

A nonce binds a payment to (resource, agent, amount). A client
cannot reuse a payment proof at a different resource, against a
different agent, or for a different price. The `X402Service`
checks all three on `settle_request`. The nonce is also
single-use: replaying the same proof returns the original
receipt, but no second wallet credit. This is enforced by the
`has_paid(nonce)` check in `PaymentRegistry`.

### Why a pluggable verifier

The `PaymentVerifier` Protocol lets production wire a real
chain-RPC verifier (a `viem` call to `getTransactionReceipt`
plus a USDC `Transfer` event log check) without changing the
service. Tests and dev use `MockVerifier`, which only checks
shape. The cost of a wrong verifier is a USDC balance that
doesn't match reality; we keep the verifier small and easy to
audit.

## What it enables

- **Earn**: a customer pays the agent via x402. The wallet's
  USDC goes up. The `HelixTreasury` can then top up the
  in-memory credit ledger on the next tick.
- **Charge**: the agent exposes paid APIs without a separate
  billing system. The price is in the 402 response.
- **Rate limit by price**: a high-cost endpoint is naturally
  protected by being too expensive to spam. A real implementation
  would also rate-limit by payer.

## Future improvements

- **Streaming payments**: an x402 variant for SSE / WebSocket
  where the client pays per chunk, not per request. Same headers,
  repeated.
- **Multi-currency**: today we hardcode USDC on Base. A real
  deployment would accept a list of supported (token, chain)
  pairs and quote in any of them.
- **Refund flow**: when the agent fails *after* a 200, the
  client should be able to claim a refund. This is a separate
  endpoint that signs a refund tx and credits the payer.
- **Anti-replay beyond the nonce**: the verifier could
  additionally check the tx was mined in the last N blocks
  to defeat long-tail replays of stale-but-real payments.
