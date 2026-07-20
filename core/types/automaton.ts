/**
 * Core domain types shared across services. These are wire-agnostic
 * representations; gRPC and REST layers translate to their respective
 * formats.
 */

import { Money } from "./money.js";
import { AutomatonId, PlanId, TaskId, ActionId, MemoryId } from "./ids.js";

export type ISO8601 = string;

export type LifecycleState =
  | "created"
  | "running"
  | "paused"
  | "suspended"
  | "replicating"
  | "terminated"
  | "archived";

export type RiskLevel = "low" | "medium" | "high" | "critical";

export interface Automaton {
  id: AutomatonId;
  name: string;
  genesisPrompt: string;
  parentId: AutomatonId | null;
  publicKey: string; // base64 Ed25519
  walletAddress: string;
  state: LifecycleState;
  createdAt: ISO8601;
  updatedAt: ISO8601;
  version: string;
  reputation: number; // 0..1
  baseCurrency: string;
  balance: Money;
  budget: Money;
  metadata: Record<string, string>;
}

export interface Goal {
  id: string;
  description: string;
  priority: number; // 0..100
  expectedRevenue: Money;
  estimatedCost: Money;
  probability: number; // 0..1
  status: "pending" | "active" | "completed" | "failed" | "cancelled";
  createdAt: ISO8601;
  completedAt?: ISO8601;
}

export interface Plan {
  id: PlanId;
  automatonId: AutomatonId;
  goalId: string;
  steps: PlanStep[];
  estimatedCost: Money;
  expectedRevenue: Money;
  probability: number;
  createdAt: ISO8601;
  status: "draft" | "approved" | "executing" | "succeeded" | "failed" | "cancelled";
}

export interface PlanStep {
  index: number;
  kind: string; // tool | llm | external
  description: string;
  estimatedCost: Money;
  dependsOn: number[];
}

export interface Task {
  id: TaskId;
  automatonId: AutomatonId;
  kind: string;
  payload: Record<string, unknown>;
  budget: Money;
  deadline?: ISO8601;
  status:
    | "queued"
    | "in_progress"
    | "awaiting_payment"
    | "succeeded"
    | "failed"
    | "expired"
    | "cancelled";
  result?: Record<string, unknown>;
  createdAt: ISO8601;
  updatedAt: ISO8601;
}

export interface Action {
  id: ActionId;
  taskId: TaskId;
  planId: PlanId;
  toolName: string;
  arguments: Record<string, unknown>;
  risk: RiskLevel;
  costEstimate: Money;
  policyDecision: PolicyDecision;
  startedAt: ISO8601;
  completedAt?: ISO8601;
  result?: unknown;
  error?: string;
}

export type PolicyVerdict = "allow" | "deny" | "require_approval";

export interface PolicyDecision {
  verdict: PolicyVerdict;
  reason: string;
  evaluatedAt: ISO8601;
  evaluator: string; // "constitution@v1" | "rbac" | "abac" | ...
  citations: string[]; // e.g. ["constitution:law:1", "policy:budget"]
  expiresAt?: ISO8601;
}

export interface MemoryEntry {
  id: MemoryId;
  automatonId: AutomatonId;
  layer: MemoryLayer;
  content: string;
  embedding?: number[]; // base64-encoded float32 vector
  importance: number; // 0..1
  ttl?: number; // seconds
  createdAt: ISO8601;
  updatedAt: ISO8601;
  tags: string[];
}

export type MemoryLayer =
  | "working"
  | "short_term"
  | "long_term"
  | "semantic"
  | "procedural"
  | "financial"
  | "operational"
  | "code_history"
  | "decision_history"
  | "relationship";

export interface TreasuryEntry {
  id: string;
  automatonId: AutomatonId;
  kind: "credit" | "debit";
  amount: Money;
  category: string; // "llm_inference" | "compute" | "revenue:api" | ...
  refType?: string;
  refId?: string;
  occurredAt: ISO8601;
  memo?: string;
}

export interface Tool {
  name: string;
  version: string;
  description: string;
  capabilities: string[];
  risk: RiskLevel;
  cost: Money;
  rateLimit?: { perMinute: number; perHour: number; perDay: number };
  sandbox: "none" | "process" | "container" | "microvm";
  schema: unknown; // JSON schema for arguments
}

export interface HealthReport {
  status: "healthy" | "degraded" | "unhealthy";
  components: Record<string, ComponentHealth>;
  checkedAt: ISO8601;
}

export interface ComponentHealth {
  status: "up" | "down" | "degraded";
  latencyMs?: number;
  message?: string;
}
