"""Unit tests for the tool registry."""
from __future__ import annotations

import asyncio

import pytest

from core.errors.errors import NotFoundError, RateLimitError, ValidationError
from core.types.automaton import Money, RiskLevel, ToolSpec
from runtime.loop.tools import ToolRegistry


def test_register_and_get():
    r = ToolRegistry()
    spec = ToolSpec(
        name="t.echo",
        version="0.1.0",
        description="echo",
        capabilities=[],
        risk=RiskLevel.LOW,
        cost=Money.zero(),
        sandbox="none",
        schema={"type": "object", "properties": {"v": {"type": "string"}}, "required": ["v"]},
    )

    def echo(v: str) -> str:
        return v

    r.register(spec, echo)
    got = r.get("t.echo")
    assert got is not None and got.name == "t.echo"


def test_invoke_validates_required_args():
    r = ToolRegistry()
    r.register(
        ToolSpec(
            name="t.echo",
            version="0.1.0",
            description="echo",
            capabilities=[],
            risk=RiskLevel.LOW,
            cost=Money.zero(),
            sandbox="none",
            schema={"type": "object", "properties": {"v": {"type": "string"}}, "required": ["v"]},
        ),
        lambda v: v,
    )
    with pytest.raises(ValidationError):
        asyncio.run(r.invoke("t.echo", {}))


def test_invoke_unknown_tool():
    r = ToolRegistry()
    with pytest.raises(NotFoundError):
        asyncio.run(r.invoke("missing", {}))


def test_rate_limit_enforced():
    r = ToolRegistry()
    r.register(
        ToolSpec(
            name="t.tick",
            version="0.1.0",
            description="tick",
            capabilities=[],
            risk=RiskLevel.LOW,
            cost=Money.zero(),
            sandbox="none",
            schema={"type": "object", "properties": {}},
            rate_limit={"perMinute": 1, "perHour": 1, "perDay": 1},
        ),
        lambda: 1,
    )
    asyncio.run(r.invoke("t.tick", {}))
    with pytest.raises(RateLimitError):
        asyncio.run(r.invoke("t.tick", {}))


def test_microvm_requires_sandbox_token():
    r = ToolRegistry()
    r.register(
        ToolSpec(
            name="t.vm",
            version="0.1.0",
            description="vm",
            capabilities=[],
            risk=RiskLevel.HIGH,
            cost=Money.zero(),
            sandbox="microvm",
            schema={"type": "object", "properties": {}},
        ),
        lambda: 1,
    )
    from core.errors.errors import SandboxError

    with pytest.raises(SandboxError):
        asyncio.run(r.invoke("t.vm", {}))
