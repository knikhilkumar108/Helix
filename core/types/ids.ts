/**
 * Branded ID types. The brand is purely a TS-level type guard; the runtime
 * value is always a string. This prevents accidentally mixing IDs at compile
 * time without paying any runtime cost.
 */
export type Brand<T, B extends string> = T & { readonly __brand: B };

export type AutomatonId = Brand<string, "AutomatonId">;
export type TaskId = Brand<string, "TaskId">;
export type ActionId = Brand<string, "ActionId">;
export type PlanId = Brand<string, "PlanId">;
export type MemoryId = Brand<string, "MemoryId">;
export type EventId = Brand<string, "EventId">;

const ID_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_\-:]{7,127}$/;

function make<T extends string>(prefix: string, brand: string): T {
  // Prefer UUIDv7 if available, fall back to v4. crypto.randomUUID is widely
  // supported; for v7 ordering we polyfill via timestamp-prefixed hex.
  const rnd = (globalThis.crypto?.randomUUID?.() ?? "").replace(/-/g, "");
  if (rnd.length !== 32) {
    // Fallback: 32 hex chars
    const buf = new Uint8Array(16);
    globalThis.crypto.getRandomValues(buf);
    const hex = Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${prefix}${hex}` as T;
  }
  return `${prefix}${rnd}` as T;
}

const validate = (kind: string, v: string) => {
  if (!ID_PATTERN.test(v)) throw new Error(`invalid ${kind} identifier: ${v}`);
  return v;
};

export const newAutomatonId = (): AutomatonId =>
  make<AutomatonId>("atm_", "AutomatonId");
export const newTaskId = (): TaskId => make<TaskId>("tsk_", "TaskId");
export const newActionId = (): ActionId => make<ActionId>("act_", "ActionId");
export const newPlanId = (): PlanId => make<PlanId>("pln_", "PlanId");
export const newMemoryId = (): MemoryId => make<MemoryId>("mem_", "MemoryId");
export const newEventId = (): EventId => make<EventId>("evt_", "EventId");

export const asAutomatonId = (s: string): AutomatonId =>
  validate("automaton", s) as AutomatonId;
export const asTaskId = (s: string): TaskId => validate("task", s) as TaskId;
export const asActionId = (s: string): ActionId =>
  validate("action", s) as ActionId;
export const asPlanId = (s: string): PlanId => validate("plan", s) as PlanId;
export const asMemoryId = (s: string): MemoryId =>
  validate("memory", s) as MemoryId;
export const asEventId = (s: string): EventId => validate("event", s) as EventId;
