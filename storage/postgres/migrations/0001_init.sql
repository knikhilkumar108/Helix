-- Core schema for the Automata platform.
--
-- All tables use UUIDv7-style identifiers with text primary keys. Time is
-- stored as `timestamptz`; money as `bigint` micro-units + currency char(8).

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =========================================================================
-- Tenancy & users
-- =========================================================================
CREATE TABLE IF NOT EXISTS tenants (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email           CITEXT NOT NULL,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    password_algo   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);
CREATE INDEX IF NOT EXISTS users_tenant_idx ON users(tenant_id);

CREATE EXTENSION IF NOT EXISTS citext;

-- =========================================================================
-- Automata
-- =========================================================================
CREATE TABLE IF NOT EXISTS automata (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    parent_id           TEXT REFERENCES automata(id) ON DELETE SET NULL,
    name                TEXT NOT NULL,
    genesis_prompt      TEXT NOT NULL,
    public_key          TEXT NOT NULL,
    wallet_address      TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN
                        ('created','running','paused','suspended',
                         'replicating','terminated','archived')),
    lifecycle_state_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             TEXT NOT NULL,
    reputation          REAL NOT NULL DEFAULT 0.5 CHECK (reputation >= 0 AND reputation <= 1),
    base_currency       CHAR(8) NOT NULL DEFAULT 'USDC',
    balance_micro       BIGINT NOT NULL DEFAULT 0,
    budget_micro        BIGINT NOT NULL DEFAULT 0,
    metadata            JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS automata_parent_idx ON automata(parent_id);
CREATE INDEX IF NOT EXISTS automata_state_idx ON automata(state);
CREATE INDEX IF NOT EXISTS automata_tenant_idx ON automata(tenant_id);

-- =========================================================================
-- Treasury ledger
-- =========================================================================
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              TEXT PRIMARY KEY,
    automaton_id    TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('credit','debit')),
    amount_micro    BIGINT NOT NULL CHECK (amount_micro >= 0),
    currency        CHAR(8) NOT NULL,
    category        TEXT NOT NULL,
    ref_type        TEXT,
    ref_id          TEXT,
    memo            TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    signature       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_automaton_time_idx
    ON ledger_entries(automaton_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ledger_category_idx ON ledger_entries(category);

-- =========================================================================
-- Plans, tasks, actions
-- =========================================================================
CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    automaton_id    TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    goal_id         TEXT,
    status          TEXT NOT NULL CHECK (status IN
                    ('draft','approved','executing','succeeded','failed','cancelled')),
    estimated_cost_micro BIGINT NOT NULL,
    expected_revenue_micro BIGINT NOT NULL,
    currency        CHAR(8) NOT NULL,
    probability     REAL NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plans_automaton_idx ON plans(automaton_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    automaton_id    TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    plan_id         TEXT REFERENCES plans(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    payload         JSONB NOT NULL,
    budget_micro    BIGINT NOT NULL,
    currency        CHAR(8) NOT NULL,
    deadline        TIMESTAMPTZ,
    status          TEXT NOT NULL CHECK (status IN
                    ('queued','in_progress','awaiting_payment',
                     'succeeded','failed','expired','cancelled')),
    result          JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tasks_automaton_idx ON tasks(automaton_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status);

CREATE TABLE IF NOT EXISTS actions (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    plan_id         TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    tool_name       TEXT NOT NULL,
    arguments       JSONB NOT NULL,
    risk            TEXT NOT NULL CHECK (risk IN ('low','medium','high','critical')),
    cost_micro      BIGINT NOT NULL,
    currency        CHAR(8) NOT NULL,
    policy_verdict  TEXT NOT NULL CHECK (policy_verdict IN ('allow','deny','require_approval')),
    policy_reason   TEXT NOT NULL,
    policy_evaluator TEXT NOT NULL,
    policy_citations JSONB NOT NULL DEFAULT '[]'::JSONB,
    started_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ,
    result          JSONB,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS actions_task_idx ON actions(task_id);
CREATE INDEX IF NOT EXISTS actions_tool_idx ON actions(tool_name, started_at DESC);

-- =========================================================================
-- Memory
-- =========================================================================
CREATE TABLE IF NOT EXISTS memory_entries (
    id              TEXT PRIMARY KEY,
    automaton_id    TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    layer           TEXT NOT NULL CHECK (layer IN
                    ('working','short_term','long_term','semantic','procedural',
                     'financial','operational','code_history','decision_history','relationship')),
    content         TEXT NOT NULL,
    embedding       VECTOR(1536),  -- requires pgvector; nullable if extension absent
    importance      REAL NOT NULL DEFAULT 0.5,
    ttl_seconds     INTEGER,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_automaton_layer_idx
    ON memory_entries(automaton_id, layer);
CREATE INDEX IF NOT EXISTS memory_automaton_updated_idx
    ON memory_entries(automaton_id, updated_at DESC);

-- =========================================================================
-- Identity (signing keys live in vault, this table stores public material)
-- =========================================================================
CREATE TABLE IF NOT EXISTS identity_keys (
    automaton_id    TEXT PRIMARY KEY REFERENCES automata(id) ON DELETE CASCADE,
    public_key      TEXT NOT NULL,
    algorithm       TEXT NOT NULL DEFAULT 'Ed25519',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at      TIMESTAMPTZ
);

-- =========================================================================
-- Approvals (human-in-the-loop for high-risk tools)
-- =========================================================================
CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    automaton_id    TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    action_id       TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    required_by     TIMESTAMPTZ NOT NULL,
    decision        TEXT CHECK (decision IN ('approved','rejected','expired')),
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS approvals_pending_idx
    ON approvals(automaton_id) WHERE decision IS NULL;

-- =========================================================================
-- Audit log (append-only)
-- =========================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       TEXT,
    automaton_id    TEXT,
    user_id         TEXT,
    actor_kind      TEXT NOT NULL,  -- user | automaton | service | system
    action          TEXT NOT NULL,  -- e.g. 'automata.fund', 'plan.execute'
    target_kind     TEXT,
    target_id       TEXT,
    request_id      TEXT,
    correlation_id  TEXT,
    payload         JSONB NOT NULL,
    prev_hash       TEXT,
    row_hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_automaton_time_idx
    ON audit_log(automaton_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_actor_time_idx
    ON audit_log(actor_kind, occurred_at DESC);

-- =========================================================================
-- Snapshots
-- =========================================================================
CREATE TABLE IF NOT EXISTS snapshots (
    id              TEXT PRIMARY KEY,
    automaton_id    TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    payload         JSONB NOT NULL,
    sha256          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (automaton_id, seq)
);

-- =========================================================================
-- Plugin registry
-- =========================================================================
CREATE TABLE IF NOT EXISTS plugins (
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    description     TEXT NOT NULL,
    capabilities    TEXT[] NOT NULL DEFAULT '{}',
    risk            TEXT NOT NULL,
    cost_micro      BIGINT NOT NULL,
    currency        CHAR(8) NOT NULL,
    rate_limit      JSONB,
    sandbox         TEXT NOT NULL,
    schema          JSONB NOT NULL,
    artifact_uri    TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    signed_by       TEXT NOT NULL,
    signature       TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);

-- =========================================================================
-- Marketplace (offers & orders)
-- =========================================================================
CREATE TABLE IF NOT EXISTS marketplace_offers (
    id              TEXT PRIMARY KEY,
    seller_id       TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    price_micro     BIGINT NOT NULL,
    currency        CHAR(8) NOT NULL,
    sla_seconds     INTEGER,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS marketplace_offers_kind_idx ON marketplace_offers(kind);

CREATE TABLE IF NOT EXISTS marketplace_orders (
    id              TEXT PRIMARY KEY,
    offer_id        TEXT NOT NULL REFERENCES marketplace_offers(id) ON DELETE CASCADE,
    buyer_id        TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    seller_id       TEXT NOT NULL REFERENCES automata(id) ON DELETE CASCADE,
    price_micro     BIGINT NOT NULL,
    currency        CHAR(8) NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                    ('created','paid','in_progress','delivered','disputed','refunded','cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS marketplace_orders_buyer_idx ON marketplace_orders(buyer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS marketplace_orders_seller_idx ON marketplace_orders(seller_id, created_at DESC);
