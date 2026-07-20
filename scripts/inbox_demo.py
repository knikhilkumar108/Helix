"""
Inbox end-to-end demo.

Walks the full inbox lifecycle through the runtime:

  1. Build two agents (Alice and Bob) sharing a platform-wide
     `InboxService`.
  2. Alice calls `messaging.send` to deliver a task to Bob.
  3. Bob's tick observes the pending message in his context.
  4. Bob claims the message, processes it (in this demo, we
     just print), and marks it done.

This is the canonical agent-to-agent pattern. In a real
deployment the agents would be in different processes; here
they're in the same one for visibility.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.types.identifiers import new_automaton_id
from runtime.loop.loop_init import build_default_loop
from services.messaging import InboxService
from services.state.sqlite_store import SqliteStore


def main() -> int:
    print("=" * 70)
    print("  Inbox / agent-to-agent messaging demo")
    print("=" * 70)
    print()

    # One platform-wide store, shared inbox service.
    store = SqliteStore(":memory:")
    platform_inbox = InboxService(backend=store, cap=100)

    aid_alice = new_automaton_id()
    aid_bob = new_automaton_id()
    print(f"Alice: {aid_alice}")
    print(f"Bob:   {aid_bob}")
    print()

    # Build both agents. Each one sees the same platform-wide
    # inbox, but addresses messages to the other's id.
    alice = build_default_loop(aid_alice, inbox=platform_inbox)
    bob = build_default_loop(aid_bob, inbox=platform_inbox)

    # ── Step 1: Alice observes ──────────────────────────────
    print("Step 1: Alice's observation before any messages")
    print("-" * 70)
    obs = alice.ctx.observe()
    print(f"  Alice sees inbox.pending = {obs.get('inbox', {}).get('pending', 0)}")
    print()

    # ── Step 2: Alice sends a message to Bob ───────────────
    print("Step 2: Alice calls messaging.send to deliver a task to Bob")
    print("-" * 70)
    send_fn = alice.tools._tools["messaging.send"].fn
    result = send_fn(to=str(aid_bob), content="please summarize the report")
    print(f"  → message id: {result['id']}")
    print(f"  → state:      {result['state']}")
    print(f"  → from:       {result['from']}")
    print(f"  → to:         {result['to']}")
    print()

    # ── Step 3: Bob observes ───────────────────────────────
    print("Step 3: Bob's observation shows the pending message")
    print("-" * 70)
    obs = bob.ctx.observe()
    print(f"  Bob sees inbox = {obs.get('inbox')}")
    print()

    # ── Step 4: Bob claims and processes ───────────────────
    print("Step 4: Bob calls messaging.claim and processes")
    print("-" * 70)
    claim_fn = bob.tools._tools["messaging.claim"].fn
    claimed = claim_fn(to=str(aid_bob), limit=10)
    print(f"  Bob claimed {claimed['count']} message(s)")
    for m in claimed["messages"]:
        print(f"    - id: {m['id']}")
        print(f"      from: {m['from']}")
        print(f"      content: {m['content']!r}")
        print(f"      state: {m['state']}")
    # Mark processed.
    mark_fn = bob.tools._tools["messaging.mark_processed"].fn
    msg_id = claimed["messages"][0]["id"]
    n = mark_fn(ids=[msg_id])
    print(f"  Bob marked processed: {n}")
    print()

    # ── Step 5: Final state ────────────────────────────────
    print("Step 5: Final state of Bob's inbox")
    print("-" * 70)
    stats = platform_inbox.stats(str(aid_bob))
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()

    # ── Step 6: Try a malformed message via the no-inbox path
    print("Step 6: An agent without an inbox wired fails loudly")
    print("-" * 70)
    aid_lone = new_automaton_id()
    lone = build_default_loop(aid_lone, inbox=None)
    send_fn = lone.tools._tools["messaging.send"].fn
    try:
        send_fn(to=str(aid_bob), content="hi")
    except RuntimeError as e:
        print(f"  Got expected error: {e}")
    print()

    print("=" * 70)
    print("  Demo complete — agent-to-agent messaging works")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
