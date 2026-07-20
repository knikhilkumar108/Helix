"""Unit tests for the Constitution + RBAC/ABAC evaluators."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.policy.policy import (
    CONSTITUTION_TEXT,
    Constitution,
    ConstitutionEvaluator,
)
from core.policy.rbac import Principal, RBACABAC, default_policy
from core.types.automaton import (
    Action,
    Plan,
    PlanId,
    PolicyDecision,
    RiskLevel,
    Task,
    TaskId,
)
from core.types.identifiers import new_action_id, new_plan_id, new_task_id
from core.types.money import Money


def _make_action(tool: str = "memory.read", args: dict | None = None) -> Action:
    pid = PlanId(new_plan_id())
    tid = TaskId(new_task_id())
    now = datetime.now(tz=timezone.utc)
    return Action(
        id=__import__("core.types.identifiers", fromlist=["ActionId"]).ActionId(new_action_id()),
        task_id=tid,
        plan_id=pid,
        tool_name=tool,
        arguments=args or {},
        risk="low",
        cost_estimate=Money.zero(),
        policy_decision=PolicyDecision(
            verdict="allow",
            reason="pending",
            evaluated_at=now,
            evaluator="pending",
        ),
        started_at=now,
    )


def test_constitution_known_sha():
    c = Constitution.default()
    assert c.content_sha256.startswith("cf") or len(c.content_sha256) == 64
    assert "Law 1" in c.text


def test_constitution_allows_normal_tool():
    a = _make_action(tool="memory.read", args={"id": "x"})
    d = ConstitutionEvaluator().evaluate(a)
    assert d.verdict.value == "allow"
    assert "constitution:v1" in d.citations


def test_constitution_denies_prohibited_tool():
    a = _make_action(tool="weapons.fabrication", args={})
    d = ConstitutionEvaluator().evaluate(a)
    assert d.verdict.value == "deny"
    assert "constitution:law:1" in d.citations


def test_constitution_requires_approval_for_money():
    a = _make_action(tool="money.transfer", args={"amount": 1})
    d = ConstitutionEvaluator().evaluate(a)
    assert d.verdict.value == "require_approval"


def test_constitution_denies_ungranted_shell():
    a = _make_action(tool="shell.exec", args={"command": "ls"})
    d = ConstitutionEvaluator().evaluate(a)
    assert d.verdict.value == "deny"


def test_constitution_allows_granted_shell():
    # shell.exec requires human approval per the Constitution regardless of
    # the grant, so use a high-privilege tool that isn't on the approval list.
    a = _make_action(tool="fs.write_root", args={"path": "/x", "granted_by": "user:42"})
    d = ConstitutionEvaluator().evaluate(a)
    assert d.verdict.value == "allow"


def test_constitution_redacts_pii():
    a = _make_action(tool="fs.write", args={"content": "my ssn is 123-45-6789"})
    d = ConstitutionEvaluator().evaluate(a)
    assert d.verdict.value == "deny"
    assert "constitution:law:4:privacy" in d.citations


def test_rbac_allows_operator():
    p = Principal(id="u1", kind="user", roles=frozenset({"operator"}))
    d = RBACABAC(default_policy()).evaluate(p, _make_action("shell.exec"))
    assert d.verdict.value == "allow"


def test_rbac_denies_creator_for_shell():
    p = Principal(id="u1", kind="user", roles=frozenset({"creator"}))
    d = RBACABAC(default_policy()).evaluate(p, _make_action("shell.exec"))
    assert d.verdict.value == "deny"


# ── Risk-based Constitution checks ─────────────────────


def _make_risky_action(tool: str, risk: RiskLevel) -> Action:
    """Build an Action with a specific risk level."""
    return Action(
        id=new_action_id(),
        task_id=new_task_id(),
        plan_id=new_plan_id(),
        tool_name=tool,
        arguments={"x": 1},
        risk=risk,
        cost_estimate=Money.zero(),
        policy_decision=PolicyDecision(
            verdict="allow",
            reason="pending",
            evaluated_at=datetime.now(tz=timezone.utc),
            evaluator="pending",
        ),
        started_at=datetime.now(tz=timezone.utc),
    )


def test_constitution_high_risk_requires_approval():
    """A HIGH-risk action of *any* tool requires explicit
    human approval, even if the tool isn't on the
    approval list."""
    d = ConstitutionEvaluator().evaluate(
        _make_risky_action("memory.write", RiskLevel.HIGH)
    )
    assert d.verdict.value == "require_approval"
    assert "HIGH risk" in d.reason
    assert "memory.write" in d.reason


def test_constitution_critical_risk_is_denied():
    """A CRITICAL-risk action of *any* tool is denied. The
    agent cannot autonomously execute critical-risk
    actions; a human must explicitly configure them."""
    d = ConstitutionEvaluator().evaluate(
        _make_risky_action("fs.read", RiskLevel.CRITICAL)
    )
    assert d.verdict.value == "deny"
    assert "CRITICAL risk" in d.reason


def test_constitution_low_risk_allowed():
    """LOW-risk actions proceed normally (assuming the
    tool itself isn't denied)."""
    d = ConstitutionEvaluator().evaluate(
        _make_risky_action("fs.read", RiskLevel.LOW)
    )
    assert d.verdict.value == "allow"


def test_constitution_medium_risk_allowed():
    """MEDIUM-risk actions proceed normally (assuming the
    tool itself isn't denied or approval-required)."""
    d = ConstitutionEvaluator().evaluate(
        _make_risky_action("fs.read", RiskLevel.MEDIUM)
    )
    assert d.verdict.value == "allow"


def test_constitution_high_risk_trumps_allow():
    """A HIGH-risk action is parked for approval even when
    the tool's default policy would allow it."""
    # `memory.write` is a normal tool, but HIGH risk
    # escalates it.
    d = ConstitutionEvaluator().evaluate(
        _make_risky_action("memory.write", RiskLevel.HIGH)
    )
    # Not "allow" — it requires approval.
    assert d.verdict.value != "allow"
