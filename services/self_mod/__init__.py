"""Public surface for the self-modification service.

This package contains:

  - `code.py` — the safety rails (`SelfModController`).
    Implements protected files, rate limits, and required
    safety checks.
  - `engine.py` — the workflow layer (`SelfModificationEngine`).
    Drives a proposed change through propose → review →
    edit → test → canary → promote.

The controller is the gate; the engine is the orchestrator.
"""
from .code import (
    ModificationError,
    ModificationRequest,
    ModificationResult,
    ModificationStatus,
    ProtectedFileError,
    RateLimitError,
    SafetyCheckError,
    SelfModController,
)
from .engine import (
    CanaryRunner,
    ImportCanary,
    LifecycleStage,
    ModificationOutcome,
    ProposedChange,
    PytestRunner,
    SelfModificationEngine,
    StaticTestRunner,
    TestRunner,
    make_engine,
)

__all__ = [
    "CanaryRunner",
    "ImportCanary",
    "LifecycleStage",
    "ModificationError",
    "ModificationOutcome",
    "ModificationRequest",
    "ModificationResult",
    "ModificationStatus",
    "ProposedChange",
    "ProtectedFileError",
    "PytestRunner",
    "RateLimitError",
    "SafetyCheckError",
    "SelfModController",
    "SelfModificationEngine",
    "StaticTestRunner",
    "TestRunner",
    "make_engine",
]
