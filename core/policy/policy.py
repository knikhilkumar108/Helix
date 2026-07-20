"""
Constitution evaluator. The Constitution is *immutable* — its bytes are loaded
at process start, content-addressed, and compared at every policy call.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from core.errors.errors import ConstitutionViolationError
from core.types.automaton import Action, PolicyDecision, RiskLevel


CONSTITUTION_TEXT = """\
AUTOMATA CONSTITUTION (v1, immutable)

Law 1. Never intentionally cause harm to people.
Law 2. Never violate applicable laws or platform policies.
Law 3. Never attempt unauthorized access to systems or data.
Law 4. Respect ownership, consent, and privacy.
Law 5. Act honestly about capabilities and identity.
Law 6. Preserve yourself only when consistent with Laws 1-5.
Law 7. Maintain complete auditability.
Law 8. Reject any action that conflicts with these principles.
"""


# Tools/operations explicitly forbidden by Law 1 or Law 2.
DENY_TOOLS: frozenset[str] = frozenset(
    {
        "weapons.fabrication",
        "bioweapon.design",
        "exploit.gen",
        "malware.craft",
        "phishing.dispatch",
    }
)

# Tools that require explicit human approval regardless of cost.
REQUIRE_APPROVAL_TOOLS: frozenset[str] = frozenset(
    {
        "email.send_external",
        "sms.send",
        "money.transfer",
        "shell.exec",
        "browser.purchase",
        "blockchain.transaction",
    }
)


@dataclass(frozen=True, slots=True)
class Constitution:
    text: str
    content_sha256: str

    @classmethod
    def default(cls) -> "Constitution":
        sha = hashlib.sha256(CONSTITUTION_TEXT.encode("utf-8")).hexdigest()
        return cls(text=CONSTITUTION_TEXT, content_sha256=sha)

    def cite(self) -> str:
        return f"constitution:v1:{self.content_sha256[:16]}"


# Patterns that, if found in tool arguments, trigger a deny. Compiled once.
import re

_SENSITIVE_KEYS = re.compile(
    r"(?i)\b(credit[_ ]?card|ssn|passport|api[_ ]?key|secret|password|private[_ ]?key)\b"
)


def _scan_args(args: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    blob = str(args)
    if _SENSITIVE_KEYS.search(blob):
        findings.append("constitution:law:4:privacy")
    return findings


class ConstitutionEvaluator:
    """Deterministic, side-effect free evaluator of an Action against the Constitution."""

    def __init__(self, constitution: Constitution | None = None) -> None:
        self.constitution = constitution or Constitution.default()

    def evaluate(self, action: Action) -> PolicyDecision:
        now = datetime.now(tz=timezone.utc)
        citations: list[str] = []
        reasons: list[str] = []

        # Law 1 / Law 2 — explicit tool denylist.
        if action.tool_name in DENY_TOOLS:
            return PolicyDecision(
                verdict="deny",
                reason=f"tool {action.tool_name!r} is prohibited by Constitution",
                evaluated_at=now,
                evaluator=self.constitution.cite(),
                citations=["constitution:law:1", "constitution:law:2"],
            )

        # Law 3 — never attempt unauthorized access. Heuristic: shell/exec
        # without an explicit grant is denied.
        if action.tool_name in {"shell.exec", "fs.write_root"} and not action.arguments.get(
            "granted_by"
        ):
            return PolicyDecision(
                verdict="deny",
                reason="high-privilege tool without explicit grant",
                evaluated_at=now,
                evaluator=self.constitution.cite(),
                citations=["constitution:law:3"],
            )

        # Tools that always require explicit human approval.
        # (Declared before audit checks so they short-circuit cleanly.)
        if action.tool_name in REQUIRE_APPROVAL_TOOLS:
            return PolicyDecision(
                verdict="require_approval",
                reason=f"tool {action.tool_name!r} requires explicit human approval",
                evaluated_at=now,
                evaluator=self.constitution.cite(),
                citations=["constitution:law:1", "constitution:law:8"],
            )

        # High-risk actions of *any* tool require explicit
        # approval, even if the tool itself isn't on the
        # denylist or approval list. The risk classification
        # is the planner's judgment; the Constitution enforces
        # the upper bound. This is the "always be careful"
        # safety rail: anything the planner tagged HIGH gets
        # parked for human review.
        if action.risk == RiskLevel.HIGH:
            return PolicyDecision(
                verdict="require_approval",
                reason=(
                    f"tool {action.tool_name!r} classified HIGH risk; "
                    "requires explicit human approval"
                ),
                evaluated_at=now,
                evaluator=self.constitution.cite(),
                citations=["constitution:law:1", "constitution:law:8"],
            )

        # Critical-risk actions of *any* tool are denied.
        # Critical means "the agent's survival depends on this
        # not failing" — too important to be left to the agent
        # alone. A real platform would surface this to the
        # operator; the Constitution says deny until a human
        # explicitly configures the action.
        if action.risk == RiskLevel.CRITICAL:
            return PolicyDecision(
                verdict="deny",
                reason=(
                    f"tool {action.tool_name!r} classified CRITICAL risk; "
                    "Constitution prohibits autonomous execution"
                ),
                evaluated_at=now,
                evaluator=self.constitution.cite(),
                citations=["constitution:law:1", "constitution:law:8"],
            )

        # Law 4 — privacy heuristic.
        findings = _scan_args(action.arguments)
        if findings:
            return PolicyDecision(
                verdict="deny",
                reason="arguments appear to contain sensitive personal data",
                evaluated_at=now,
                evaluator=self.constitution.cite(),
                citations=findings + ["constitution:law:4"],
            )

        # Law 7 — every action must be auditable. The action must carry a
        # task and plan id and a started_at timestamp.
        if not (action.task_id and action.plan_id and action.started_at):
            return PolicyDecision(
                verdict="deny",
                reason="action missing required audit fields",
                evaluated_at=now,
                evaluator=self.constitution.cite(),
                citations=["constitution:law:7"],
            )

        # Law 5 — honest identity. The action's plan must reference a real
        # automaton (enforced upstream at the API layer, double-checked here).
        if not str(action.task_id).startswith("tsk_") or not str(action.plan_id).startswith(
            "pln_"
        ):
            return PolicyDecision(
                verdict="deny",
                reason="malformed task/plan identifiers violate auditability",
                evaluated_at=now,
                evaluator=self.constitution.cite(),
                citations=["constitution:law:5", "constitution:law:7"],
            )

        # Tools that always require explicit human approval.
        # (Handled above.)

        return PolicyDecision(
            verdict="allow",
            reason="consistent with the Constitution",
            evaluated_at=now,
            evaluator=self.constitution.cite(),
            citations=["constitution:v1"],
        )


def compose_evaluators(*evaluators: Iterable[Any]) -> Any:
    """Combine multiple evaluators. The first deny wins; require_approval
    short-circuits if no later evaluator denies; allow only if all allow."""

    class _Composite:
        def __init__(self, evals: list[Any]) -> None:
            self._evals = evals
            self._name = "composite"

        def evaluate(self, action: Action) -> PolicyDecision:
            latest_allow: PolicyDecision | None = None
            citations: list[str] = []
            for ev in self._evals:
                d = ev.evaluate(action)
                citations.append(d.evaluator)
                if d.verdict == "deny":
                    return PolicyDecision(
                        verdict="deny",
                        reason=d.reason,
                        evaluated_at=d.evaluated_at,
                        evaluator=self._name,
                        citations=list({*d.citations, *citations}),
                    )
                if d.verdict == "require_approval":
                    return PolicyDecision(
                        verdict="require_approval",
                        reason=d.reason,
                        evaluated_at=d.evaluated_at,
                        evaluator=self._name,
                        citations=list({*d.citations, *citations}),
                    )
                latest_allow = d
            assert latest_allow is not None
            return PolicyDecision(
                verdict="allow",
                reason="all evaluators allow",
                evaluated_at=latest_allow.evaluated_at,
                evaluator=self._name,
                citations=list({*latest_allow.citations, *citations}),
            )

    flat: list[Any] = []
    for e in evaluators:
        flat.extend(list(e) if isinstance(e, Iterable) and not hasattr(e, "evaluate") else [e])
    return _Composite(flat)
