/**
 * TypeScript SDK for the Automata platform. Mirrors the Python SDK.
 */
export type ISO8601 = string;

export interface Automaton {
  id: string;
  name: string;
  state: string;
  parent_id: string | null;
  public_key: string;
  wallet_address: string;
  version: string;
  reputation: number;
  base_currency: string;
  balance: string;
  created_at: ISO8601;
  updated_at: ISO8601;
}

export class PlatformError extends Error {
  status: number;
  body: Record<string, unknown>;
  constructor(message: string, status: number, body: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export interface ClientOptions {
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
}

export class AutomataClient {
  private baseUrl: string;
  private token: string;
  private timeoutMs: number;

  constructor(opts: ClientOptions = {}) {
    this.baseUrl = opts.baseUrl ?? "http://localhost:8080";
    this.token = opts.token ?? "";
    this.timeoutMs = opts.timeoutMs ?? 30_000;
  }

  private async req<T>(
    method: string,
    path: string,
    body?: unknown,
    query?: Record<string, string | number | undefined>
  ): Promise<T> {
    const url = new URL(this.baseUrl + path);
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined) url.searchParams.set(k, String(v));
      }
    }
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const headers: Record<string, string> = { "content-type": "application/json" };
      if (this.token) headers["authorization"] = `Bearer ${this.token}`;
      const res = await fetch(url, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
      if (!res.ok) {
        const b = (await res.json().catch(() => ({}))) as Record<string, unknown>;
        throw new PlatformError(
          String(b["message"] ?? "request failed"),
          res.status,
          b
        );
      }
      return (res.status === 204 ? (undefined as unknown as T) : ((await res.json()) as T));
    } finally {
      clearTimeout(t);
    }
  }

  // Automata
  createAutomaton(args: {
    name: string;
    genesis_prompt: string;
    initial_balance_micro?: number;
    currency?: string;
    parent_id?: string;
    metadata?: Record<string, string>;
  }): Promise<Automaton> {
    return this.req<Automaton>("POST", "/v1/automata", args);
  }
  getAutomaton(id: string): Promise<Automaton> {
    return this.req<Automaton>("GET", `/v1/automata/${id}`);
  }
  listAutomata(): Promise<Automaton[]> {
    return this.req<Automaton[]>("GET", "/v1/automata");
  }
  pause(id: string): Promise<Automaton> {
    return this.req<Automaton>("POST", `/v1/automata/${id}/pause`);
  }
  resume(id: string): Promise<Automaton> {
    return this.req<Automaton>("POST", `/v1/automata/${id}/resume`);
  }
  terminate(id: string): Promise<Automaton> {
    return this.req<Automaton>("POST", `/v1/automata/${id}/terminate`);
  }
  events(id: string): Promise<Array<Record<string, unknown>>> {
    return this.req("GET", `/v1/automata/${id}/events`);
  }

  // Treasury
  fund(
    id: string,
    args: { amount_micro: number; currency?: string; source?: string }
  ): Promise<Record<string, unknown>> {
    return this.req("POST", `/v1/treasury/${id}/fund`, { automaton_id: id, ...args });
  }
  balance(id: string): Promise<{ balance: string; health: Record<string, unknown> }> {
    return this.req("GET", `/v1/treasury/${id}/balance`);
  }
  ledger(id: string, limit = 100): Promise<Array<Record<string, unknown>>> {
    return this.req("GET", `/v1/treasury/${id}/ledger`, undefined, { limit });
  }

  // Memory
  memory(id: string): Promise<Array<Record<string, unknown>>> {
    return this.req("GET", `/v1/memory/${id}`);
  }
  writeMemory(
    id: string,
    args: { layer: string; content: string; importance?: number; tags?: string[] }
  ): Promise<Record<string, unknown>> {
    return this.req("POST", "/v1/memory", { automaton_id: id, ...args });
  }

  // Marketplace
  listOffers(kind?: string): Promise<Array<Record<string, unknown>>> {
    return this.req("GET", "/v1/marketplace/offers", undefined, { kind });
  }
  createOffer(args: {
    seller_id: string;
    kind: string;
    title: string;
    description: string;
    price_micro: number;
    currency?: string;
  }): Promise<Record<string, unknown>> {
    return this.req("POST", "/v1/marketplace/offers", args);
  }
  placeOrder(args: { offer_id: string; buyer_id: string }): Promise<Record<string, unknown>> {
    return this.req("POST", "/v1/marketplace/orders", args);
  }

  // Audit
  audit(automatonId?: string, limit = 100): Promise<Array<Record<string, unknown>>> {
    return this.req("GET", "/v1/audit/log", undefined, {
      automaton: automatonId,
      limit,
    });
  }
  verifyAudit(): Promise<Record<string, unknown>> {
    return this.req("GET", "/v1/audit/verify");
  }
}
