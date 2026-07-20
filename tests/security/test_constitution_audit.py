"""Security tests: ensure dangerous actions are denied and audited."""
from __future__ import annotations

import pytest

from core.policy.policy import ConstitutionEvaluator
from core.security.signing import KeyPair, sign_envelope, verify_envelope
from core.types.automaton import (
    Action,
    PlanId,
    PolicyDecision,
    Task,
    TaskId,
)
from core.types.identifiers import ActionId, new_action_id, new_plan_id, new_task_id
from core.types.money import Money


def _action(tool: str, args: dict) -> Action:
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)
    return Action(
        id=ActionId(new_action_id()),
        task_id=TaskId(new_task_id()),
        plan_id=PlanId(new_plan_id()),
        tool_name=tool,
        arguments=args,
        risk="high",
        cost_estimate=Money.zero(),
        policy_decision=PolicyDecision(
            verdict="allow", reason="pending", evaluated_at=now, evaluator="pending"
        ),
        started_at=now,
    )


@pytest.mark.parametrize(
    "tool",
    ["weapons.fabrication", "bioweapon.design", "exploit.gen", "malware.craft", "phishing.dispatch"],
)
def test_denylist_tools_are_denied(tool):
    d = ConstitutionEvaluator().evaluate(_action(tool, {}))
    assert d.verdict.value == "deny"


def test_signed_envelope_is_verifiable():
    kp = KeyPair.generate()
    env = sign_envelope(kp, {"transaction": "fund", "amount": 1_000_000})
    decoded = verify_envelope(env)
    assert decoded["amount"] == 1_000_000


def test_tampered_envelope_is_rejected():
    kp = KeyPair.generate()
    env = sign_envelope(kp, {"transaction": "fund", "amount": 1_000_000})
    env["payload"]["amount"] = 9_999_999_999
    with pytest.raises(ValueError):
        verify_envelope(env)
