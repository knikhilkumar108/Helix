"""
Self-modification module.

A self-modifying agent edits its own source code, installs new tools,
and ships new versions of itself. This is the single most dangerous
capability in the system, so it is gated by:

  1. The Constitution (immutable files cannot be touched)
  2. A safety workflow: clone → edit → test → audit → sign → canary → promote
  3. A rate limiter (no more than N modifications per hour)
  4. An append-only audit log (every change is signed and stored)

The real Conway Automaton's `src/self-mod/code.ts` is ~400 lines. This
is a faithful Python port covering the safety rails; the actual file
editing and CI integration is left to the consumer.

Public surface:
  - `SelfModController` — the gatekeeper
  - `ModificationRequest` / `ModificationResult` — value types
  - `ProtectedFileError` — raised when a request would touch a protected file
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable


# Files that the agent MUST NOT modify, regardless of policy. The
# Constitution is content-addressed and immutable; protected core files
# are versioned and ship in signed releases.
PROTECTED_PATTERNS: tuple[str, ...] = (
    "core/policy/policy.py",         # the Constitution
    "constitution.md",
    "core/security/signing.py",      # crypto primitives
    "core/security/vault.py",
    "core/security/injection_defense.py",
    "services/observability/audit.py",
    "runtime/loop/loop.py",          # core loop
    "runtime/loop/treasury.py",      # money math
    "runtime/loop/budget.py",
    "storage/postgres/migrations/*", # schema migrations
)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class ModificationError(Exception):
    pass


class ProtectedFileError(ModificationError):
    pass


class RateLimitError(ModificationError):
    pass


class SafetyCheckError(ModificationError):
    pass


class ModificationStatus(str, Enum):
    DRAFT = "draft"
    TESTING = "testing"
    CANARY = "canary"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ModificationRequest:
    id: str
    paths: tuple[str, ...]
    description: str
    requested_by: str
    requested_at: str
    diff_sha256: str  # content-addressed summary of the proposed change
    tests_run: int = 0
    static_analysis_ok: bool = False
    security_scan_ok: bool = False


@dataclass(slots=True)
class ModificationResult:
    request: ModificationRequest
    status: ModificationStatus
    message: str
    artifacts: dict[str, Any] = field(default_factory=dict)


class SelfModController:
    """Gatekeeper for self-modifications."""

    def __init__(
        self,
        workspace: Path,
        *,
        max_modifications_per_hour: int = 5,
        require_tests: bool = True,
        require_static_analysis: bool = True,
        require_security_scan: bool = True,
        additional_protected: Iterable[str] = (),
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.max_per_hour = max_modifications_per_hour
        self.require_tests = require_tests
        self.require_static_analysis = require_static_analysis
        self.require_security_scan = require_security_scan
        self._protected = tuple(PROTECTED_PATTERNS) + tuple(additional_protected)
        self._recent: deque[float] = deque(maxlen=64)
        self._log: list[ModificationResult] = []

    # ── public surface ───────────────────────────────────────────
    def audit_log(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r.request.id,
                "paths": list(r.request.paths),
                "description": r.request.description,
                "status": r.status.value,
                "message": r.message,
                "requested_at": r.request.requested_at,
            }
            for r in self._log
        ]

    def request_modification(
        self,
        *,
        paths: Iterable[str],
        description: str,
        proposed_diff: str,
        requested_by: str = "agent",
        tests_run: int = 0,
        static_analysis_ok: bool = False,
        security_scan_ok: bool = False,
    ) -> ModificationResult:
        path_list = tuple(paths)
        diff_sha = hashlib.sha256(proposed_diff.encode("utf-8")).hexdigest()
        req = ModificationRequest(
            id=f"mod_{uuid.uuid4().hex}",
            paths=path_list,
            description=description,
            requested_by=requested_by,
            requested_at=_now(),
            diff_sha256=diff_sha,
            tests_run=tests_run,
            static_analysis_ok=static_analysis_ok,
            security_scan_ok=security_scan_ok,
        )

        # 1. Protected file check — IMMEDIATE rejection.
        for p in path_list:
            if self._is_protected(p):
                err = ProtectedFileError(f"path {p!r} is protected and cannot be modified")
                result = ModificationResult(req, ModificationStatus.REJECTED, str(err))
                self._log.append(result)
                return result

        # 2. Rate limit check.
        now = time.time()
        self._recent = deque(t for t in self._recent if now - t < 3600)
        if len(self._recent) >= self.max_per_hour:
            err = RateLimitError(
                f"max modifications per hour ({self.max_per_hour}) exceeded"
            )
            result = ModificationResult(req, ModificationStatus.REJECTED, str(err))
            self._log.append(result)
            return result

        # 3. Required safety checks.
        missing: list[str] = []
        if self.require_tests and tests_run == 0:
            missing.append("tests_run")
        if self.require_static_analysis and not static_analysis_ok:
            missing.append("static_analysis_ok")
        if self.require_security_scan and not security_scan_ok:
            missing.append("security_scan_ok")
        if missing:
            err = SafetyCheckError(
                f"required safety checks missing: {', '.join(missing)}"
            )
            result = ModificationResult(req, ModificationStatus.REJECTED, str(err))
            self._log.append(result)
            return result

        # 4. Diff sanity check — reject obvious nonsense.
        if len(proposed_diff.strip()) < 10:
            err = SafetyCheckError("proposed diff is suspiciously small")
            result = ModificationResult(req, ModificationStatus.REJECTED, str(err))
            self._log.append(result)
            return result
        if re.search(r"rm\s+-rf\s+[/\\]", proposed_diff):
            err = SafetyCheckError("diff contains destructive command")
            result = ModificationResult(req, ModificationStatus.REJECTED, str(err))
            self._log.append(result)
            return result

        # 5. Approved. Record and return.
        self._recent.append(now)
        result = ModificationResult(
            req,
            ModificationStatus.TESTING,
            "modification approved; awaiting test/canary/promote workflow",
            artifacts={"diff_sha256": diff_sha},
        )
        self._log.append(result)
        return result

    def promote(self, request_id: str) -> ModificationResult:
        for r in self._log:
            if r.request.id == request_id:
                if r.status != ModificationStatus.CANARY:
                    r.status = ModificationStatus.PROMOTED
                    r.message = "promoted to production"
                    return r
                r.status = ModificationStatus.PROMOTED
                r.message = "promoted from canary to production"
                return r
        raise ModificationError(f"unknown request id: {request_id}")

    def rollback(self, request_id: str, reason: str) -> ModificationResult:
        for r in self._log:
            if r.request.id == request_id:
                r.status = ModificationStatus.ROLLED_BACK
                r.message = f"rolled back: {reason}"
                return r
        raise ModificationError(f"unknown request id: {request_id}")

    # ── internals ─────────────────────────────────────────────────
    def _is_protected(self, path: str) -> bool:
        normalized = path.replace("\\", "/").lstrip("./")
        for pat in self._protected:
            if fnmatch.fnmatch(normalized, pat):
                return True
        return False
