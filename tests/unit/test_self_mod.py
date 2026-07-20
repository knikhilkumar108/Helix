"""Tests for the self-modification controller."""
from __future__ import annotations

import pytest

from services.self_mod.code import (
    ModificationStatus,
    ProtectedFileError,
    RateLimitError,
    SafetyCheckError,
    SelfModController,
)


@pytest.fixture
def controller():
    return SelfModController(workspace="/tmp", max_modifications_per_hour=2)


def _diff(s: str = "print('hello world')\n" * 5) -> str:
    return s


def test_safe_modification_approved(controller):
    r = controller.request_modification(
        paths=["runtime/loop/tools.py"],
        description="add a new tool",
        proposed_diff=_diff(),
        tests_run=3,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    assert r.status == ModificationStatus.TESTING


def test_protected_file_rejected(controller):
    r = controller.request_modification(
        paths=["core/policy/policy.py"],
        description="modify constitution",
        proposed_diff=_diff(),
        tests_run=99,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    assert r.status == ModificationStatus.REJECTED
    assert "protected" in r.message.lower()


def test_constitution_md_protected(controller):
    r = controller.request_modification(
        paths=["constitution.md"],
        description="tweak wording",
        proposed_diff=_diff(),
        tests_run=99,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    assert r.status == ModificationStatus.REJECTED


def test_rate_limit_enforced(controller):
    for _ in range(2):
        controller.request_modification(
            paths=["runtime/loop/tools.py"],
            description="x",
            proposed_diff=_diff(),
            tests_run=1,
            static_analysis_ok=True,
            security_scan_ok=True,
        )
    r = controller.request_modification(
        paths=["runtime/loop/tools.py"],
        description="y",
        proposed_diff=_diff(),
        tests_run=1,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    assert r.status == ModificationStatus.REJECTED
    assert "max" in r.message.lower()


def test_missing_tests_rejected(controller):
    r = controller.request_modification(
        paths=["x.py"],
        description="x",
        proposed_diff=_diff(),
        tests_run=0,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    assert r.status == ModificationStatus.REJECTED


def test_missing_static_analysis_rejected(controller):
    r = controller.request_modification(
        paths=["x.py"],
        description="x",
        proposed_diff=_diff(),
        tests_run=1,
        static_analysis_ok=False,
        security_scan_ok=True,
    )
    assert r.status == ModificationStatus.REJECTED


def test_destructive_diff_rejected(controller):
    r = controller.request_modification(
        paths=["scripts/run.py"],
        description="rm",
        proposed_diff="rm -rf / --no-preserve-root",
        tests_run=1,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    assert r.status == ModificationStatus.REJECTED


def test_promote_and_rollback(controller):
    r = controller.request_modification(
        paths=["x.py"],
        description="x",
        proposed_diff=_diff(),
        tests_run=1,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    controller.promote(r.request.id)
    found = next(x for x in controller.audit_log() if x["id"] == r.request.id)
    assert found["status"] == ModificationStatus.PROMOTED.value

    controller.rollback(r.request.id, "regression in production")
    found = next(x for x in controller.audit_log() if x["id"] == r.request.id)
    assert found["status"] == ModificationStatus.ROLLED_BACK.value


def test_audit_log_accumulates(controller):
    controller.request_modification(
        paths=["x.py"],
        description="a",
        proposed_diff=_diff(),
        tests_run=1,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    controller.request_modification(
        paths=["y.py"],
        description="b",
        proposed_diff=_diff(),
        tests_run=1,
        static_analysis_ok=True,
        security_scan_ok=True,
    )
    log = controller.audit_log()
    assert len(log) == 2
    assert log[0]["description"] == "a"
    assert log[1]["description"] == "b"
