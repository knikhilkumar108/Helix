"""Tests for the self-modification engine."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from services.self_mod import (
    ImportCanary,
    LifecycleStage,
    ModificationStatus,
    ProposedChange,
    SelfModController,
    SelfModificationEngine,
    StaticTestRunner,
    make_engine,
)


# ── Helpers ──────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A clean workspace with a sample module to modify."""
    src = tmp_path / "services" / "foo"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("def hello():\n    return 'old'\n")
    return tmp_path


# ── Engine: full workflow ──────────────────────────


@pytest.mark.asyncio
async def test_engine_promotes_a_valid_change(workspace: Path):
    controller = SelfModController(workspace=workspace, require_tests=False, require_static_analysis=False, require_security_scan=False)
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=StaticTestRunner(),
        canary_runner=ImportCanary(),
    )
    target = workspace / "services" / "foo" / "foo.py"
    new_content = "def hello():\n    return 'new'\n"
    outcome = await engine.propose_and_apply(
        ProposedChange(
            path="services/foo/foo.py",
            old_content=target.read_text(),
            new_content=new_content,
            description="change hello to return 'new'",
        )
    )
    assert outcome.stage == LifecycleStage.PROMOTED
    # The file on disk is updated.
    assert target.read_text() == new_content


@pytest.mark.asyncio
async def test_engine_rejects_change_to_protected_file(workspace: Path):
    controller = SelfModController(workspace=workspace, require_tests=False, require_static_analysis=False, require_security_scan=False)
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=StaticTestRunner(),
        canary_runner=ImportCanary(),
    )
    outcome = await engine.propose_and_apply(
        ProposedChange(
            path="core/policy/policy.py",  # protected
            old_content="x",
            new_content="y",
            description="attempt to modify the Constitution",
        )
    )
    assert outcome.stage == LifecycleStage.REJECTED
    assert "protected" in outcome.message.lower()


@pytest.mark.asyncio
async def test_engine_rejects_when_tests_fail(workspace: Path):
    """A custom test runner that fails should roll back the change."""
    class _FailingTestRunner:
        async def run(self, *, cwd: Path) -> dict:
            return {"passed": False, "stderr": "tests failed"}

    controller = SelfModController(workspace=workspace, require_tests=False, require_static_analysis=False, require_security_scan=False)
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=_FailingTestRunner(),
        canary_runner=ImportCanary(),
    )
    target = workspace / "services" / "foo" / "foo.py"
    original = target.read_text()
    outcome = await engine.propose_and_apply(
        ProposedChange(
            path="services/foo/foo.py",
            old_content=original,
            new_content="def hello():\n    return 'broken'\n",
            description="introduce a bug",
        )
    )
    assert outcome.stage == LifecycleStage.FAILED
    assert "tests failed" in outcome.message
    # The file on disk is unchanged.
    assert target.read_text() == original


@pytest.mark.asyncio
async def test_engine_rejects_when_canary_fails(workspace: Path):
    """A custom canary that fails should roll back the change."""
    class _FailingCanary:
        async def run(self, *, file_path: Path) -> dict:
            return {"passed": False, "stderr": "import error"}

    controller = SelfModController(workspace=workspace, require_tests=False, require_static_analysis=False, require_security_scan=False)
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=StaticTestRunner(),
        canary_runner=_FailingCanary(),
    )
    target = workspace / "services" / "foo" / "foo.py"
    original = target.read_text()
    outcome = await engine.propose_and_apply(
        ProposedChange(
            path="services/foo/foo.py",
            old_content=original,
            new_content="def hello():\n    return 'broken'\n",
            description="introduce an import error",
        )
    )
    assert outcome.stage == LifecycleStage.FAILED
    assert "canary failed" in outcome.message
    # The file is unchanged.
    assert target.read_text() == original


@pytest.mark.asyncio
async def test_engine_rejects_path_escape(workspace: Path):
    """A path that escapes the workspace is rejected."""
    controller = SelfModController(workspace=workspace, require_tests=False, require_static_analysis=False, require_security_scan=False)
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=StaticTestRunner(),
        canary_runner=ImportCanary(),
    )
    outcome = await engine.propose_and_apply(
        ProposedChange(
            path="../escape.txt",
            old_content="",
            new_content="evil",
            description="escape attempt",
        )
    )
    assert outcome.stage == LifecycleStage.FAILED
    assert "escapes" in outcome.message


# ── Static test runner ─────────────────────────────


@pytest.mark.asyncio
async def test_static_test_runner_passes():
    runner = StaticTestRunner()
    result = await runner.run(cwd=Path("/tmp"))
    assert result["passed"] is True


# ── Import canary ─────────────────────────────────


@pytest.mark.asyncio
async def test_import_canary_succeeds_on_valid_python(tmp_path: Path):
    src = tmp_path / "m.py"
    src.write_text("x = 1\n")
    canary = ImportCanary()
    result = await canary.run(file_path=src)
    assert result["passed"] is True


@pytest.mark.asyncio
async def test_import_canary_fails_on_invalid_python(tmp_path: Path):
    src = tmp_path / "m.py"
    src.write_text("def x(:\n")  # syntax error
    canary = ImportCanary()
    result = await canary.run(file_path=src)
    assert result["passed"] is False


# ── Rate limiting ─────────────────────────────────


@pytest.mark.asyncio
async def test_engine_rate_limiting(workspace: Path):
    """The controller's rate limit kicks in after N modifications."""
    controller = SelfModController(
        workspace=workspace,
        max_modifications_per_hour=2,
        require_tests=False,
        require_static_analysis=False,
        require_security_scan=False,
    )
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=StaticTestRunner(),
        canary_runner=ImportCanary(),
    )
    target = workspace / "services" / "foo" / "foo.py"
    for i in range(3):
        outcome = await engine.propose_and_apply(
            ProposedChange(
                path="services/foo/foo.py",
                old_content=target.read_text(),
                new_content=f"def hello():\n    return '{i}'\n",
                description=f"change {i}",
            )
        )
        if i < 2:
            assert outcome.stage == LifecycleStage.PROMOTED
        else:
            # Third attempt hits the rate limit.
            assert outcome.stage == LifecycleStage.REJECTED


# ── Audit log ──────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_records_audit_log(workspace: Path):
    controller = SelfModController(workspace=workspace, require_tests=False, require_static_analysis=False, require_security_scan=False)
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=StaticTestRunner(),
        canary_runner=ImportCanary(),
    )
    target = workspace / "services" / "foo" / "foo.py"
    await engine.propose_and_apply(
        ProposedChange(
            path="services/foo/foo.py",
            old_content=target.read_text(),
            new_content="def hello():\n    return 'audit'\n",
            description="audit log test",
        )
    )
    log = controller.audit_log()
    assert len(log) == 1
    assert log[0]["description"] == "audit log test"


# ── Factory ───────────────────────────────────────


def test_make_engine_factory():
    controller = SelfModController(workspace=Path("/tmp"))
    engine = make_engine(controller=controller)
    assert engine.controller is controller
    # Default runners are wired.
    assert engine.test_runner is not None
    assert engine.canary_runner is not None
