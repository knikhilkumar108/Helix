"""Tests for the loop detector."""
from __future__ import annotations

from runtime.loop.loop_detector import (
    IDLE_ONLY_TOOLS,
    MUTATING_TOOLS,
    LoopDetector,
    LoopVerdict,
)


def test_clean_turns_are_ok():
    d = LoopDetector()
    assert d.observe(["time.now"]) == LoopVerdict.OK
    assert d.observe(["memory.read"]) == LoopVerdict.OK


def test_repetitive_pattern_warns_then_enforces():
    d = LoopDetector(max_repetitions=3, max_idle_turns=99)
    # Pattern A, A, A — third A triggers WARN
    d.observe(["a", "b"])
    d.observe(["a", "b"])
    verdict = d.observe(["a", "b"])
    assert verdict == LoopVerdict.WARN
    # Fourth A (after warning) triggers ENFORCE_SLEEP
    # But after WARN, history is cleared; need to repeat the same pattern again
    d.observe(["a", "b"])
    d.observe(["a", "b"])
    verdict = d.observe(["a", "b"])
    assert verdict == LoopVerdict.ENFORCE_SLEEP


def test_pattern_change_resets_warning():
    d = LoopDetector(max_repetitions=3, max_idle_turns=99)
    d.observe(["a", "b"])
    d.observe(["a", "b"])
    d.observe(["a", "b"])  # warn
    d.observe(["c"])        # different
    verdict = d.observe(["a", "b"])
    assert verdict == LoopVerdict.OK


def test_maintenance_loop_enforces():
    d = LoopDetector(max_repetitions=99, max_idle_turns=3)
    assert d.observe(["time.now"]) == LoopVerdict.OK
    assert d.observe(["memory.read"]) == LoopVerdict.OK
    assert d.observe(["health"]) == LoopVerdict.ENFORCE_SLEEP


def test_mutating_tool_resets_idle_counter():
    d = LoopDetector(max_repetitions=99, max_idle_turns=3)
    d.observe(["time.now"])
    d.observe(["time.now"])
    d.observe(["shell.exec"])  # mutating — resets
    d.observe(["time.now"])
    assert d.observe(["time.now"]) == LoopVerdict.OK  # only 2 idle in a row
    verdict = d.observe(["time.now"])
    assert verdict == LoopVerdict.ENFORCE_SLEEP


def test_did_mutate():
    d = LoopDetector()
    assert d.did_mutate(["shell.exec"])
    assert d.did_mutate(["time.now", "shell.exec"])
    assert not d.did_mutate(["time.now"])
    assert not d.did_mutate([])


def test_blocklists_nonempty():
    # Sanity: the blocklists should at least cover the obvious cases.
    assert "time.now" in IDLE_ONLY_TOOLS
    assert "shell.exec" in MUTATING_TOOLS
    assert "fs.write" in MUTATING_TOOLS
