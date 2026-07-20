"""
Interactive chat with a real LLM-backed Automaton.

This is the easiest way to see the platform work end-to-end. It boots
an Automaton with a real LLM (OpenAI, Anthropic, or local Ollama) and
lets you talk to it through the REPL. The agent reasons, plans, and
executes tools using the platform's full stack (Constitution, RBAC,
budget, memory, treasury).

Usage:
    OPENAI_API_KEY=sk-... python scripts/chat.py
    ANTHROPIC_API_KEY=sk-... python scripts/chat.py
    OLLAMA_MODEL=llama3.1 python scripts/chat.py
    AUTOMATA_MODEL=gpt-4o python scripts/chat.py

The agent is funded with $1.00 of virtual USDC by default. Every LLM
call debits the treasury. If it runs out, the agent halts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.observability.metrics import METRICS
from core.survival.tiers import SurvivalTier
from core.types.identifiers import AutomatonId, new_automaton_id
from core.types.money import Money
from runtime.loop.context import InMemoryLoopContext
from runtime.loop.loop import AutomatonLoop, LoopConfig
from runtime.loop.treasury import InMemoryTreasury


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Chat with an LLM-backed Automaton")
    p.add_argument(
        "--name",
        default="chat-agent",
        help="Name for this session (used in the agent's system prompt)",
    )
    p.add_argument(
        "--balance",
        type=float,
        default=1.00,
        help="Initial virtual balance in USDC (default 1.00)",
    )
    p.add_argument(
        "--provider",
        choices=["auto", "openai", "anthropic", "ollama", "openrouter"],
        default="auto",
        help="Which provider to use",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the model name (otherwise the router picks)",
    )
    p.add_argument(
        "--max-ticks",
        type=int,
        default=20,
        help="Maximum number of reasoning turns before stopping",
    )
    p.add_argument(
        "--ticks-per-message",
        type=int,
        default=1,
        help="Reasoning turns per user message (default 1)",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-turn output; only print the agent's final summary",
    )
    return p


async def chat_loop(args: argparse.Namespace) -> int:
    # Lazy imports so the script fails cleanly if optional deps are missing.
    from services.router import default_real_router
    from services.router.llm_reasoner import LLMReasoner

    aid = AutomatonId(new_automaton_id())
    name = args.name
    initial = Money.from_major(f"{args.balance:.6f}")

    # Build the router from environment.
    if args.provider == "openai":
        os.environ.setdefault("OPENAI_API_KEY", "")
    elif args.provider == "anthropic":
        os.environ.setdefault("ANTHROPIC_API_KEY", "")
    elif args.provider == "openrouter":
        os.environ.setdefault("OPENROUTER_API_KEY", "")
    router = default_real_router(prefer=args.provider)

    if not router.models():
        print(
            "ERROR: no provider is configured. Set OPENAI_API_KEY, ANTHROPIC_API_KEY,\n"
            "OPENROUTER_API_KEY, or have Ollama running at localhost:11434.",
            file=sys.stderr,
        )
        return 1

    print("Available models:")
    for m in router.models():
        print(f"  - {m.provider}/{m.name}")

    # Set up the runtime with the real reasoner.
    tools = __import__("runtime.loop.tools", fromlist=["ToolRegistry"]).ToolRegistry()
    from runtime.loop.builtins import register_builtins

    workspace = Path("/tmp/automata-chat")
    register_builtins(tools, workspace=workspace)
    treasury = InMemoryTreasury(aid, initial=initial)

    # ── Audit chain ──
    # Every state-changing event is written to a
    # hash-chained SQLite log. The chain's integrity is
    # verifiable; tampering with any row breaks the chain.
    from services.state.sqlite_store import SqliteStore
    audit_db = workspace / "chat-audit.sqlite"
    audit_store = SqliteStore(audit_db)

    def audit_hook(*, kind, payload):
        # Synchronous hook called by the runtime. Writes
        # to the audit chain. The hash includes the
        # previous row's hash; tamper with any row and
        # the chain breaks.
        from datetime import datetime, timezone
        import json as _json
        entry = {
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(timespec="microseconds"),
            "tenant_id": None,
            "automaton_id": str(aid),
            "user_id": None,
            "actor_kind": "human" if kind == "user_message" else "automaton",
            "action": kind,
            "target_kind": None,
            "target_id": None,
            "request_id": None,
            "correlation_id": None,
            "payload_json": _json.dumps(payload, sort_keys=True),
        }
        audit_store.append_audit(entry)

    # ── Dashboard bus ──
    # The chat session publishes events to a local bus.
    # A real deployment would expose this as a WebSocket
    # for the operator dashboard; here we just print the
    # events as they happen.
    from services.dashboard import EventBus, EventKind, StreamEvent
    bus = EventBus()
    print_events: list[StreamEvent] = []

    def _on_event(event: StreamEvent) -> None:
        print_events.append(event)
        # Pretty-print a one-line summary.
        if not args.quiet:
            if event.kind == EventKind.HEARTBEAT:
                return  # too noisy
            summary = ""
            if event.kind == EventKind.TREASURY_UPDATE:
                summary = f"balance={event.payload.get('balance_micro', '?')}"
            elif event.kind == EventKind.TIER_CHANGE:
                summary = f"{event.payload.get('from')} → {event.payload.get('to')}"
            elif event.kind == EventKind.ACTION_COMPLETED:
                summary = f"{event.payload.get('tool', '?')}"
            elif event.kind == EventKind.POLICY_DENIED:
                summary = f"{event.payload.get('tool', '?')}: {event.payload.get('reason', '?')[:50]}"
            print(f"  [{event.kind.value}] {summary}")

    class _DashboardShim:
        def make_event(self, *, kind, aid, payload):
            return StreamEvent(
                id=f"evt_{len(print_events)}",
                kind=kind, aid=aid, payload=payload,
                occurred_at=time.time(),
            )
        def publish(self, event):
            bus.publish(event)
            _on_event(event)
    dashboard = _DashboardShim()

    # ── The actual loop ──
    ctx = InMemoryLoopContext(service="chat", automaton_id=aid)
    reasoner = LLMReasoner(
        router,
        automaton_id=str(aid),
        wallet_address=f"atm_wallet_{str(aid)[:8]}",
        balance_getter=treasury.balance,
        tier_getter=lambda: SurvivalTier.NORMAL,
        tools_getter=lambda: [t.name for t in tools.list()],
        model=args.model,
        max_tokens=512,
        temperature=0.2,
    )
    planner = __import__("runtime.loop.planner", fromlist=["HeuristicPlanner", "PlannerConfig"]).HeuristicPlanner(
        __import__("runtime.loop.planner", fromlist=["PlannerConfig"]).PlannerConfig(
            default_currency=treasury.balance().currency
        )
    )
    budget = __import__("runtime.loop.budget", fromlist=["BudgetController", "BudgetConfig"]).BudgetController(
        __import__("runtime.loop.budget", fromlist=["BudgetConfig"]).BudgetConfig(
            reserve_floor=Money.zero(),
            per_tick_max=Money.from_major("1.00"),
            per_day_max=Money.from_major("100.00"),
        ),
        balance_getter=treasury.balance,
    )
    checkpoints = __import__("runtime.loop.checkpoint", fromlist=["InMemoryCheckpointStore"]).InMemoryCheckpointStore()
    loop = AutomatonLoop(
        ctx=ctx,
        reasoner=reasoner,
        planner=planner,
        tools=tools,
        treasury=treasury,
        budget=budget,
        checkpoints=checkpoints,
        config=LoopConfig(max_runtime_seconds=120, sleep_min_seconds=0.5, sleep_max_seconds=1.0),
        dashboard=dashboard,
        audit_hook=audit_hook,
    )

    # Run the loop in a background task so we can inject user input.
    loop_task = asyncio.create_task(loop.run())

    print(f"\n=== Automaton {name} is alive ===")
    print(f"  ID:            {aid}")
    print(f"  Balance:       {treasury.balance()}")
    print(f"  Audit log:     {audit_db}")
    print(f"  Workspace:     {workspace}")
    print()
    print(f"  Safety:")
    print(f"    Constitution policy:  enforced (denies/approves per Law 1-8)")
    print(f"    Risk escalation:      HIGH risk → require_approval; CRITICAL → deny")
    print(f"    Audit chain:          SHA-256 hash-chained, tamper-detectable")
    print(f"    Self-modification:    requires passing tests + canary")
    print()
    print(f"  Available tools: {[t.name for t in tools.list()]}")
    print()
    print(f"  Type a message and press Enter. The agent will reason about it.")
    print(f"  Commands: /balance /memory /audit /quit\n")

    tick_count = 0
    try:
        while tick_count < args.max_ticks:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("you> ")
                )
            except (EOFError, KeyboardInterrupt):
                break

            text = user_input.strip()
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/balance":
                print(f"  balance: {treasury.balance()}")
                continue
            if text == "/memory":
                for m in ctx.memory[-10:]:
                    print(f"  [{m.layer.value}] {m.content[:120]}")
                continue
            if text == "/audit":
                # Verify the audit chain and show recent entries.
                ok, broken = audit_store.verify_audit_chain()
                print(f"  audit chain valid: {ok}")
                if not ok:
                    print(f"  broken at: {broken}")
                # Show recent entries from the store.
                cur = audit_store._conn().execute(  # type: ignore[attr-defined]
                    "SELECT seq, action, occurred_at, payload_json "
                    "FROM audit_log ORDER BY seq DESC LIMIT 10"
                )
                for row in cur:
                    payload = row["payload_json"][:80]
                    print(f"  [{row['seq']:3d}] {row['action']:20s} {payload}")
                continue

            # Inject the user message into the agent's observation by
            # recording it as a user_message event. The reasoner will
            # see it on the next tick.
            ctx.record("user_message", {"text": text, "from": "human"})
            print(f"  (sent — waiting for {args.ticks_per_message} tick(s))")

            # Wait for ticks_per_message ticks to elapse. Each tick is
            # bounded by the loop's max_actions_per_tick and sleeps.
            for _ in range(args.ticks_per_message):
                await asyncio.sleep(1.0)
                if loop_task.done():
                    break
            tick_count += args.ticks_per_message

            if not args.quiet:
                last = [e for k, e in ctx.events if k == "reason"][-1] if ctx.events else None
                if last:
                    print(f"agent> {last.get('summary', '(no summary)')}")
                if ctx.memory:
                    last_mem = ctx.memory[-1]
                    print(f"       (memory: {last_mem.content[:100]}...)")
                print(f"  balance: {treasury.balance()}")
    finally:
        loop.request_stop()
        try:
            await asyncio.wait_for(loop_task, timeout=5.0)
        except asyncio.TimeoutError:
            loop_task.cancel()

    print(f"\nFinal balance: {treasury.balance()}")
    print(f"Audit log:    {audit_db}")
    print(f"              {len(print_events)} dashboard events emitted")
    # Verify the audit chain one last time.
    ok, broken = audit_store.verify_audit_chain()
    print(f"              chain valid: {ok}" + (f" (broken at {broken})" if not ok else ""))
    return 0


def main() -> int:
    args = build_argparser().parse_args()
    return asyncio.run(chat_loop(args))


if __name__ == "__main__":
    sys.exit(main())
