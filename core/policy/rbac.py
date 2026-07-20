"""
RBAC + ABAC policy layer. Decoupled from the Constitution so that operators
can adjust permissions without touching the immutable law set.

Models:
  - Subjects: user, automaton, service
  - Resources: tool, task, plan, memory, wallet
  - Actions: invoke, read, write, delete, fund, transfer

Decisions are deterministic and side-effect free; caches live at the policy
service layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from core.types.automaton import Action, PolicyDecision


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    kind: str  # user | automaton | service
    roles: frozenset[str] = field(default_factory=frozenset)
    attributes: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def has(self, role: str) -> bool:
        return role in self.roles

    def attr(self, name: str) -> str | None:
        for k, v in self.attributes:
            if k == name:
                return v
        return None


# Action classnames for RBAC matrix.
TOOL_ACTIONS: dict[str, frozenset[str]] = {
    "shell.exec": frozenset({"shell.exec"}),
    "fs.read": frozenset({"fs.read"}),
    "fs.write": frozenset({"fs.write"}),
    "http.get": frozenset({"http.get"}),
    "http.post": frozenset({"http.post"}),
    "browser.act": frozenset({"browser.act"}),
    "email.send_external": frozenset({"email.send_external"}),
    "money.transfer": frozenset({"money.transfer"}),
    "blockchain.transaction": frozenset({"blockchain.transaction"}),
    "memory.write": frozenset({"memory.write"}),
    "memory.read": frozenset({"memory.read"}),
    "tool.invoke": frozenset({"tool.invoke"}),
}


# Default role grants. Operators can extend per-tenant.
DEFAULT_ROLE_GRANTS: dict[str, frozenset[str]] = {
    "operator": frozenset(
        {
            "shell.exec",
            "fs.read",
            "fs.write",
            "http.get",
            "http.post",
            "browser.act",
            "memory.write",
            "memory.read",
            "tool.invoke",
        }
    ),
    "creator": frozenset({"memory.read", "tool.invoke", "fs.read"}),
    "auditor": frozenset({"memory.read", "fs.read"}),
    "funding_source": frozenset({"money.transfer"}),
}


@dataclass(frozen=True, slots=True)
class Policy:
    # tool_name -> required roles
    grants: dict[str, frozenset[str]] = field(default_factory=dict)
    # attribute constraints per tool: tool_name -> (attr_name, allowed_values)
    abac: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)

    def allows(self, principal: Principal, tool: str) -> tuple[bool, list[str]]:
        required = self.grants.get(tool, frozenset())
        if not required:
            return True, []
        if not (principal.roles & required):
            return False, [f"rbac:role_required:{'|'.join(sorted(required))}"]
        for attr, allowed in self.abac.get(tool, {}).items():
            v = principal.attr(attr)
            if v is None or v not in allowed:
                return False, [f"abac:{attr} in {sorted(allowed)}"]
        return True, []


def default_policy() -> Policy:
    grants: dict[str, frozenset[str]] = {}
    for tool, roles in [
        ("shell.exec", frozenset({"operator"})),
        ("fs.read", frozenset({"operator", "creator", "auditor"})),
        ("fs.write", frozenset({"operator"})),
        ("http.get", frozenset({"operator", "creator"})),
        ("http.post", frozenset({"operator"})),
        ("browser.act", frozenset({"operator"})),
        ("email.send_external", frozenset({"operator", "funding_source"})),
        ("money.transfer", frozenset({"operator", "funding_source"})),
        ("blockchain.transaction", frozenset({"operator", "funding_source"})),
        ("memory.read", frozenset({"operator", "creator", "auditor"})),
        ("memory.write", frozenset({"operator"})),
        ("tool.invoke", frozenset({"operator", "creator"})),
    ]:
        grants[tool] = roles
    return Policy(grants=grants)


class RBACABAC:
    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or default_policy()

    def evaluate(self, principal: Principal, action: Action) -> PolicyDecision:
        now = datetime.now(tz=timezone.utc)
        ok, citations = self.policy.allows(principal, action.tool_name)
        if not ok:
            return PolicyDecision(
                verdict="deny",
                reason=f"RBAC/ABAC denied: {action.tool_name}",
                evaluated_at=now,
                evaluator="rbac-abac@v1",
                citations=citations + ["constitution:law:3"],
            )
        return PolicyDecision(
            verdict="allow",
            reason="RBAC/ABAC allowed",
            evaluated_at=now,
            evaluator="rbac-abac@v1",
            citations=["rbac:allow"],
        )
