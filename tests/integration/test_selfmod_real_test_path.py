"""
Integration tests for the self-modification engine's REAL
test path.

The engine's `PytestRunner` runs `pytest -q` against a
working copy of the workspace. This is the safety rail
that catches "this change breaks the existing tests"
before the change is promoted to production. The unit
tests use `StaticTestRunner` (always passes) to avoid
spinning up pytest for every test, but that's not the
path that matters in production.

This integration test exercises the real test path:

  1. Build a tiny Python project with one module and
     one test (that passes).
  2. Propose a change that BREAKS the test. The engine
     should detect this and roll back (status FAILED,
     file unchanged).
  3. Propose a change that DOESN'T break the test. The
     engine should detect this and promote (status
     PROMOTED, file changed).
  4. Propose a change to a PROTECTED file. The engine
     should reject at the controller gate (status
     REJECTED).

These tests use a *real* `PytestRunner` (no mocks) and a
real `ImportCanary`. The project is created in a temp
directory, and the engine is pointed at it.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from services.self_mod import (
    ImportCanary,
    LifecycleStage,
    ProposedChange,
    PytestRunner,
    SelfModController,
    SelfModificationEngine,
)


# ── Helpers ─────────────────────────────────────────


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


def _bootstrap_project(workspace: Path) -> None:
    """Create a tiny Python project with one module and one
    passing test. The project is importable as `mymod`."""
    # The module under test.
    _write(workspace / "mymod.py", """\
        def greet(name: str) -> str:
            return f"hello, {name}"
    """)
    # The test.
    _write(workspace / "test_mymod.py", """\
        from mymod import greet

        def test_greet():
            assert greet("alice") == "hello, alice"
    """)
    # A pytest config so the tests can be discovered.
    _write(workspace / "pytest.ini", """\
        [pytest]
        testpaths = .
    """)
    # A conftest.py that adds the workspace to sys.path so
    # `from mymod import greet` works.
    _write(workspace / "conftest.py", """\
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent))
    """)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_pytest_runner_passes():
    """The PytestRunner actually runs pytest and reports
    pass/fail correctly. This is a sanity check on the
    test runner before we use it in the engine."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _bootstrap_project(workspace)
        runner = PytestRunner(timeout_seconds=30.0)
        result = await runner.run(cwd=workspace)
        assert result["passed"] is True, (
            f"pytest failed: {result.get('stderr', '')[:500]}"
        )


@pytest.mark.asyncio
async def test_engine_rolls_back_change_that_breaks_tests(tmp_path: Path):
    """A self-modification that breaks the existing test
    suite is rolled back. The file on disk is unchanged."""
    _bootstrap_project(tmp_path)
    controller = SelfModController(
        workspace=tmp_path,
        require_tests=False,  # the engine runs the real tests
        require_static_analysis=False,
        require_security_scan=False,
    )
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=PytestRunner(timeout_seconds=30.0),
        canary_runner=ImportCanary(),
    )
    original = _read(tmp_path / "mymod.py")
    # Propose a change that breaks greet(): change the
    # return value to something else.
    bad_change = ProposedChange(
        path="mymod.py",
        old_content=original,
        new_content=(
            "def greet(name: str) -> str:\n"
            "    return f'goodbye, {name}'\n"
        ),
        description="change greet to say goodbye (breaks test)",
    )
    outcome = await engine.propose_and_apply(bad_change)
    # The engine detected the broken test and rolled back.
    assert outcome.stage == LifecycleStage.FAILED, (
        f"expected FAILED, got {outcome.stage}: {outcome.message}"
    )
    assert "test" in outcome.message.lower() or "fail" in outcome.message.lower()
    # The file on disk is unchanged.
    assert _read(tmp_path / "mymod.py") == original, (
        "file was modified despite broken test"
    )
    # The test result is included in the outcome.
    assert outcome.test_result is not None
    assert outcome.test_result["passed"] is False


@pytest.mark.asyncio
async def test_engine_promotes_change_that_passes_tests(tmp_path: Path):
    """A self-modification that doesn't break the test
    suite is promoted. The file on disk is changed."""
    _bootstrap_project(tmp_path)
    controller = SelfModController(
        workspace=tmp_path,
        require_tests=False,
        require_static_analysis=False,
        require_security_scan=False,
    )
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=PytestRunner(timeout_seconds=30.0),
        canary_runner=ImportCanary(),
    )
    original = _read(tmp_path / "mymod.py")
    # Propose a change that adds a new function (doesn't
    # break the existing test).
    good_change = ProposedChange(
        path="mymod.py",
        old_content=original,
        new_content=(
            "def greet(name: str) -> str:\n"
            "    return f'hello, {name}'\n"
            "\n"
            "def farewell(name: str) -> str:\n"
            "    return f'goodbye, {name}'\n"
        ),
        description="add a farewell function alongside greet",
    )
    outcome = await engine.propose_and_apply(good_change)
    # The change was promoted.
    assert outcome.stage == LifecycleStage.PROMOTED, (
        f"expected PROMOTED, got {outcome.stage}: {outcome.message}"
    )
    # The file on disk reflects the change.
    new_content = _read(tmp_path / "mymod.py")
    assert "def farewell" in new_content
    assert "def greet" in new_content
    # The original `greet` line is preserved.
    assert "return f'hello, {name}'" in new_content
    # The test result is included and shows pass.
    assert outcome.test_result is not None
    assert outcome.test_result["passed"] is True
    # The canary succeeded.
    assert outcome.canary_result is not None
    assert outcome.canary_result["passed"] is True


@pytest.mark.asyncio
async def test_engine_rejects_protected_file_with_real_tests(tmp_path: Path):
    """A self-modification to a protected file is rejected
    at the controller gate, before any tests run."""
    _bootstrap_project(tmp_path)
    # Add a conftest.py that pretends to be a protected file.
    # The controller's protected pattern list includes
    # `core/policy/policy.py` which is in the project, but
    # actually we'd need to set the workspace to the
    # platform root. For this test, we'll use the
    # `additional_protected` parameter to add a pattern.
    controller = SelfModController(
        workspace=tmp_path,
        require_tests=False,
        require_static_analysis=False,
        require_security_scan=False,
        additional_protected=("mymod.py",),
    )
    engine = SelfModificationEngine(
        controller=controller,
        test_runner=PytestRunner(timeout_seconds=30.0),
        canary_runner=ImportCanary(),
    )
    # Try to modify mymod.py (now protected).
    outcome = await engine.propose_and_apply(
        ProposedChange(
            path="mymod.py",
            old_content="def greet(name): return 'hello'",
            new_content="def greet(name): return 'goodbye'",
            description="attempt to modify a protected file",
        )
    )
    assert outcome.stage == LifecycleStage.REJECTED
    assert "protected" in outcome.message.lower()
    # The file is unchanged.
    assert "goodbye" not in _read(tmp_path / "mymod.py")
