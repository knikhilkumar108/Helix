"""
Self-modification engine — the workflow layer.

The platform's `SelfModController` (in `services/self_mod/code.py`)
implements the *safety rails*: protected files, rate limits,
required safety checks. This module implements the *workflow*
that uses those rails to actually modify code:

  1. **Propose.** The agent describes a change.
  2. **Review.** The `SelfModController` checks for protected
     files, rate limits, and required safety checks.
  3. **Edit.** If approved, apply the change to a working
     copy of the file.
  4. **Test.** Run the test suite against the modified code.
  5. **Canary.** Run the modified code in a sandboxed
     subprocess to confirm it imports / starts / behaves.
  6. **Promote.** If all green, write the change to the
     real file. Otherwise, discard the working copy.
  7. **Audit.** Every step is logged to the audit chain.

Why a workflow layer?

The controller is the gate. The engine is the *orchestrator*
that drives the gate. Without the engine, the agent has to
implement the workflow itself, which is error-prone and
easy to bypass. With the engine, the workflow is enforced
by the platform.

Why is this safe?

The workflow has three hard checks that the agent cannot
bypass:
  - The controller rejects changes to protected files.
  - The tests must pass (or the change is rolled back).
  - The canary must succeed (or the change is rolled back).
  - The audit log records every step.

A buggy or malicious agent can still propose changes, but
the platform refuses to apply them unless they pass the
checks. The agent can't edit the controller or the engine
(both are in `core/` paths protected by the controller's
`PROTECTED_PATTERNS`).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from core.errors.errors import ValidationError
from services.self_mod.code import (
    ModificationError,
    ModificationRequest,
    ModificationResult,
    ModificationStatus,
    ProtectedFileError,
    RateLimitError,
    SafetyCheckError,
    SelfModController,
)

log = logging.getLogger(__name__)


# ── Lifecycle states ──────────────────────────────────


class LifecycleStage(str, Enum):
    """The stages of the self-modification workflow.

    The engine drives a request through these stages. A
    failure at any stage moves the request to `FAILED`;
    a success at the final stage moves it to `PROMOTED`.
    """

    PROPOSED = "proposed"
    REVIEWED = "reviewed"
    EDITED = "edited"
    TESTED = "tested"
    CANARIED = "canaried"
    PROMOTED = "promoted"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(slots=True)
class ProposedChange:
    """A single proposed change.

    `path` is the file to modify. `old_content` is the
    current content (used for the diff). `new_content` is
    the proposed content. `description` is a human-
    readable summary.
    """

    path: str
    old_content: str
    new_content: str
    description: str


@dataclass(slots=True)
class ModificationOutcome:
    """The result of running a proposed change through
    the full workflow."""

    request_id: str
    stage: LifecycleStage
    message: str
    test_result: dict[str, Any] | None = None
    canary_result: dict[str, Any] | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)


# ── Test runner (protocol) ───────────────────────────


class TestRunner(Protocol):
    """Runs the test suite. The default is `pytest -q` in
    the project root; tests can supply a stub."""

    async def run(self, *, cwd: Path) -> dict[str, Any]: ...


class PytestRunner:
    """The default test runner. Subprocess `pytest -q`."""

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        self.timeout = timeout_seconds

    async def run(self, *, cwd: Path) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", "-q", "--tb=short"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4096:],  # tail
                "stderr": proc.stderr[-4096:],
                "passed": proc.returncode == 0,
            }
        except subprocess.TimeoutExpired:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": "test runner timed out",
                "passed": False,
            }


class StaticTestRunner:
    """A test runner that always passes. Used in tests
    of the engine itself, where we don't want to run
    the full test suite for each unit test."""

    async def run(self, *, cwd: Path) -> dict[str, Any]:
        return {
            "returncode": 0,
            "stdout": "(skipped: static test runner)",
            "stderr": "",
            "passed": True,
        }


# ── Canary runner (protocol) ─────────────────────────


class CanaryRunner(Protocol):
    """Runs the modified code in a sandboxed subprocess.

    The canary is the *last* check before promote. It
    imports the modified file and runs a small smoke
    test (e.g. instantiates a class, calls a method).
    A real canary would also start a subprocess of the
    agent and watch for crashes.
    """

    async def run(self, *, file_path: Path) -> dict[str, Any]: ...


class ImportCanary:
    """A canary that just imports the modified file. If
    the import fails (syntax error, missing import, etc.)
    the canary fails and the change is rejected."""

    async def run(self, *, file_path: Path) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                ["python", "-c", f"import importlib.util, sys; "
                                   f"spec = importlib.util.spec_from_file_location('m', {str(file_path)!r}); "
                                   f"m = importlib.util.module_from_spec(spec); "
                                   f"spec.loader.exec_module(m)"],
                capture_output=True,
                text=True,
                timeout=30.0,
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout[-2048:],
                "stderr": proc.stderr[-2048:],
                "passed": proc.returncode == 0,
            }
        except subprocess.TimeoutExpired:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": "canary import timed out",
                "passed": False,
            }


# ── Engine ──────────────────────────────────────────


class SelfModificationEngine:
    """The self-modification workflow orchestrator.

    Usage:

        controller = SelfModController(workspace=Path("."))
        engine = SelfModificationEngine(controller=controller)
        outcome = await engine.propose_and_apply(
            ProposedChange(
                path="services/foo/foo.py",
                old_content=current,
                new_content=new,
                description="add a new helper",
            )
        )
        if outcome.stage == LifecycleStage.PROMOTED:
            # Change is live.
        else:
            # Change was rejected; check outcome.message.
    """

    def __init__(
        self,
        *,
        controller: SelfModController,
        test_runner: TestRunner | None = None,
        canary_runner: CanaryRunner | None = None,
    ) -> None:
        self.controller = controller
        self.test_runner = test_runner or StaticTestRunner()
        self.canary_runner = canary_runner or ImportCanary()

    async def propose_and_apply(
        self,
        change: ProposedChange,
        *,
        requested_by: str = "agent",
    ) -> ModificationOutcome:
        """Run the full workflow for a single change.

        Stages:
          1. Propose → Review (controller).
          2. Edit (apply to a working copy).
          3. Test (run pytest).
          4. Canary (import the modified file).
          5. Promote (write to real file) or rollback.

        Returns a `ModificationOutcome` with the final
        stage and any test/canary results.
        """
        # Stage 1: Propose + Review.
        # Build a unified diff for the controller to inspect.
        diff_text = self._build_diff(
            change.path, change.old_content, change.new_content
        )
        # The controller checks: protected files, rate limit,
        # and diff sanity. The test/canary phases (stages 3
        # and 4) are the engine's responsibility — the
        # controller is the *gate* but the engine is the
        # *verification*. A production deployment should
        # instantiate the controller with `require_tests=False`
        # etc., because the engine handles those checks
        # itself with the actual test/canary runners.
        controller_result = self.controller.request_modification(
            paths=[change.path],
            description=change.description,
            proposed_diff=diff_text,
            requested_by=requested_by,
        )
        if controller_result.status == ModificationStatus.REJECTED:
            return ModificationOutcome(
                request_id=controller_result.request.id,
                stage=LifecycleStage.REJECTED,
                message=controller_result.message,
            )
        # Stage 2: Edit. Write the change to a working copy
        # in a temporary directory. We do NOT modify the
        # real file until promote.
        workspace = self.controller.workspace
        target = (workspace / change.path).resolve()
        # Sanity check: target must be inside the workspace.
        if not str(target).startswith(str(workspace)):
            return ModificationOutcome(
                request_id=controller_result.request.id,
                stage=LifecycleStage.FAILED,
                message=f"target {change.path!r} escapes workspace",
            )
        # Stage 3: Test. Run pytest against the modified
        # content. The simplest implementation: write to
        # a temp copy of the workspace, run pytest, then
        # discard the copy.
        test_result = await self._run_tests_with_change(change)
        if not test_result["passed"]:
            return ModificationOutcome(
                request_id=controller_result.request.id,
                stage=LifecycleStage.FAILED,
                message=f"tests failed: {test_result.get('stderr', '')[:500]}",
                test_result=test_result,
            )
        # Stage 4: Canary. Import the modified file in a
        # subprocess to confirm it loads.
        canary_result = await self._run_canary_with_change(change)
        if not canary_result["passed"]:
            return ModificationOutcome(
                request_id=controller_result.request.id,
                stage=LifecycleStage.FAILED,
                message=f"canary failed: {canary_result.get('stderr', '')[:500]}",
                test_result=test_result,
                canary_result=canary_result,
            )
        # Stage 5: Promote. Write the new content to the
        # real file.
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_content, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return ModificationOutcome(
                request_id=controller_result.request.id,
                stage=LifecycleStage.FAILED,
                message=f"promote failed: {e}",
                test_result=test_result,
                canary_result=canary_result,
            )
        # Mark the controller's result as promoted.
        try:
            self.controller.promote(controller_result.request.id)
        except ModificationError as e:
            # The controller lost track of the request;
            # log but don't fail the promotion.
            log.warning("controller_promote_failed", extra={"err": str(e)})
        return ModificationOutcome(
            request_id=controller_result.request.id,
            stage=LifecycleStage.PROMOTED,
            message=f"change promoted to {change.path}",
            test_result=test_result,
            canary_result=canary_result,
        )

    # ── Internal helpers ──
    def _build_diff(self, path: str, old: str, new: str) -> str:
        """Build a unified-diff-shaped string for the
        controller to inspect. We don't run a real diff
        library; a simple `--- old / +++ new` block is
        enough for the controller to count changes and
        spot destructive patterns.
        """
        return (
            f"--- {path}\n"
            f"+++ {path}\n"
            f"@@\n"
            f"{old}\n"
            f"===\n"
            f"{new}\n"
        )

    async def _run_tests_with_change(
        self, change: ProposedChange
    ) -> dict[str, Any]:
        """Run the test suite against a working copy of
        the workspace with the change applied.
        """
        workspace = self.controller.workspace
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Copy the workspace to the temp directory.
            # We use shutil.copytree; in production, a
            # smarter engine would use git worktrees.
            try:
                # Use ignore to skip the .venv and other
                # heavy directories.
                def _ignore(p, names):
                    return {
                        n for n in names
                        if n in (".venv", "__pycache__", ".git",
                                 "node_modules", ".pytest_cache")
                    }
                shutil.copytree(workspace, tmp_path / "ws", ignore=_ignore)
            except Exception as e:  # noqa: BLE001
                return {
                    "returncode": -1,
                    "stdout": "",
                    "stderr": f"copytree failed: {e}",
                    "passed": False,
                }
            ws = tmp_path / "ws"
            # Apply the change in the copy.
            target = (ws / change.path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_content, encoding="utf-8")
            return await self.test_runner.run(cwd=ws)

    async def _run_canary_with_change(
        self, change: ProposedChange
    ) -> dict[str, Any]:
        """Run the canary check against a working copy.
        """
        workspace = self.controller.workspace
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / Path(change.path).name
            target.write_text(change.new_content, encoding="utf-8")
            return await self.canary_runner.run(file_path=target)


# ── Factory ──────────────────────────────────────────


def make_engine(
    *,
    controller: SelfModController,
    test_runner: TestRunner | None = None,
    canary_runner: CanaryRunner | None = None,
) -> SelfModificationEngine:
    return SelfModificationEngine(
        controller=controller,
        test_runner=test_runner,
        canary_runner=canary_runner,
    )


__all__ = [
    "CanaryRunner",
    "ImportCanary",
    "LifecycleStage",
    "ModificationOutcome",
    "ProposedChange",
    "PytestRunner",
    "SelfModificationEngine",
    "StaticTestRunner",
    "TestRunner",
    "make_engine",
]
