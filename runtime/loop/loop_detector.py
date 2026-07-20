"""
Loop detection.

Adapted from Conway Automaton's `loop detection` mechanism in their
`src/agent/loop.ts`. The runtime tracks the recent sequence of tool
patterns and detects when the agent is stuck in a repeat.

Two signals:
  1. **Exact pattern repeat** — the same sorted tool-call sequence
     occurs N consecutive turns. Warning first, then enforced sleep.
  2. **Maintenance loop** — every tool call in a turn is on the
     "idle-only" blocklist (read-only status checks). After N such
     turns, the agent is forced to do real work or sleep.

This module is pure logic; the runtime applies the verdict.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class LoopVerdict(str, Enum):
    OK = "ok"
    WARN = "warn"
    ENFORCE_SLEEP = "enforce_sleep"


# Tools that count as "real work" — anything not in this set is considered
# an idle/status check for the purposes of the maintenance-loop detector.
IDLE_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "time.now",
        "memory.read",
        "status",
        "balance",
        "health",
        "memory.search",
        "list",
        "get",
    }
)

# A short blocklist of mutating tools. Used for the `did_mutate` check that
# pairs with the idle-only detector.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "shell.exec",
        "fs.write",
        "fs.write_root",
        "edit_own_file",
        "transfer_credits",
        "topup_credits",
        "fund_child",
        "spawn_child",
        "start_child",
        "delete_sandbox",
        "create_sandbox",
        "install_skill",
        "create_skill",
        "remove_skill",
        "git_commit",
        "git_push",
        "send_message",
        "register_domain",
        "register_identity",
        "expose_port",
        "remove_port",
        "sleep",
        "update_soul",
        "remember_fact",
        "set_goal",
        "complete_goal",
        "save_procedure",
        "forget",
        "enter_low_compute",
        "switch_model",
    }
)


@dataclass(slots=True)
class LoopDetector:
    """Track recent turn-level tool patterns to detect repeats and idleness."""

    max_repetitions: int = 3
    max_idle_turns: int = 3
    history: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    idle_turns: int = 0
    warned_pattern: str | None = None

    def observe(self, tool_names: list[str]) -> LoopVerdict:
        if not tool_names:
            return LoopVerdict.OK

        # Maintenance-loop detection (blocklist-based: any mutating tool
        # resets the idle counter).
        if all(t in IDLE_ONLY_TOOLS for t in tool_names):
            self.idle_turns += 1
            if self.idle_turns >= self.max_idle_turns:
                self.idle_turns = 0
                return LoopVerdict.ENFORCE_SLEEP
            return LoopVerdict.OK
        self.idle_turns = 0

        # Pattern-repeat detection. Sort names so "A then B" matches "B then A".
        pattern = ",".join(sorted(tool_names))

        # If the agent changed behavior, clear the warning.
        if self.warned_pattern and self.warned_pattern != pattern:
            self.warned_pattern = None

        # Append the new pattern and trim.
        self.history.append(pattern)
        if len(self.history) > self.max_repetitions:
            # Keep only the most recent max_repetitions entries.
            self.history = deque(list(self.history)[-self.max_repetitions :], maxlen=8)

        # Count trailing identical patterns.
        if len(self.history) >= self.max_repetitions and all(p == pattern for p in self.history):
            if self.warned_pattern == pattern:
                # Already warned; now enforce.
                self.warned_pattern = None
                self.history.clear()
                return LoopVerdict.ENFORCE_SLEEP
            self.warned_pattern = pattern
            self.history.clear()
            return LoopVerdict.WARN

        return LoopVerdict.OK

    def did_mutate(self, tool_names: list[str]) -> bool:
        return any(t in MUTATING_TOOLS for t in tool_names)
