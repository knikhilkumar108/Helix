"""
Tool registry and invocation protocol.

Tools are capability-based. Each tool declares:
  - capabilities (what it can do)
  - permissions (what RBAC role it requires)
  - cost (per-call)
  - risk (low | medium | high | critical)
  - rate limits
  - sandbox requirements

The registry is the *only* way the runtime can interact with the outside
world. New tools are added via the plugin system.
"""
from __future__ import annotations

import abc
import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from core.errors.errors import NotFoundError, ValidationError
from core.types.automaton import ToolSpec

T = TypeVar("T")


ToolFn = Callable[..., Awaitable[Any]] | Callable[..., Any]


@dataclass(slots=True)
class _RegisteredTool:
    spec: ToolSpec
    fn: ToolFn
    last_calls: list[float]  # for rate limiting


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}
        # `extra` is an open dict for callers to attach shared
        # state to the registry. Built-in tools (e.g. the
        # messaging tools) look up dependencies (InboxService,
        # the agent's own id) from here rather than via global
        # imports — this keeps the tools decoupled from the
        # runtime's global state and makes them testable in
        # isolation. Add a key like:
        #   tools.extra["inbox"] = InboxService(...)
        #   tools.extra["self_id"] = "atm_alice"
        # before calling `register_builtins(tools)`.
        self.extra: dict[str, Any] = {}

    def register(self, spec: ToolSpec, fn: ToolFn) -> None:
        if not inspect.iscoroutinefunction(fn) and not callable(fn):
            raise ValidationError("tool function must be callable")
        if spec.name in self._tools:
            raise ValidationError(f"tool {spec.name!r} already registered")
        # Sanity-check the schema is a dict (JSON schema).
        if not isinstance(spec.schema_, dict):
            raise ValidationError("tool schema must be a JSON schema object")
        self._tools[spec.name] = _RegisteredTool(spec=spec, fn=fn, last_calls=[])

    def get(self, name: str) -> ToolSpec | None:
        rt = self._tools.get(name)
        return rt.spec if rt else None

    def list(self) -> list[ToolSpec]:
        return [rt.spec for rt in self._tools.values()]

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        rt = self._tools.get(name)
        if rt is None:
            raise NotFoundError(f"tool not found: {name}", context={"tool": name})
        self._enforce_rate_limits(rt)
        self._enforce_sandbox(rt, arguments)
        # Validate arguments minimally (presence check on schema "required").
        required = rt.spec.schema_.get("required", [])
        for k in required:
            if k not in arguments:
                raise ValidationError(
                    f"missing required argument {k!r} for tool {name!r}",
                    context={"tool": name, "missing": k},
                )
        # Coerce args from string if needed (LLMs sometimes stringify).
        args = _coerce_args(arguments, rt.spec.schema_)
        result = rt.fn(**args)
        if inspect.iscoroutine(result):
            result = await result
        rt.last_calls.append(time.time())
        return result

    def _enforce_rate_limits(self, rt: _RegisteredTool) -> None:
        spec = rt.spec
        if not spec.rate_limit:
            return
        now = time.time()
        # Trim window
        rt.last_calls = [t for t in rt.last_calls if now - t < 86400]
        per_minute = sum(1 for t in rt.last_calls if now - t < 60)
        per_hour = sum(1 for t in rt.last_calls if now - t < 3600)
        per_day = len(rt.last_calls)
        rl = spec.rate_limit
        if per_minute >= rl.get("perMinute", 10**9):
            from core.errors.errors import RateLimitError

            raise RateLimitError(
                "per-minute rate limit exceeded",
                context={"tool": spec.name, "limit": "perMinute"},
            )
        if per_hour >= rl.get("perHour", 10**9):
            from core.errors.errors import RateLimitError

            raise RateLimitError(
                "per-hour rate limit exceeded",
                context={"tool": spec.name, "limit": "perHour"},
            )
        if per_day >= rl.get("perDay", 10**9):
            from core.errors.errors import RateLimitError

            raise RateLimitError(
                "per-day rate limit exceeded",
                context={"tool": spec.name, "limit": "perDay"},
            )

    def _enforce_sandbox(self, rt: _RegisteredTool, args: dict[str, Any]) -> None:
        spec = rt.spec
        if spec.sandbox == "microvm" and not args.get("sandbox_token"):
            from core.errors.errors import SandboxError

            raise SandboxError(
                "microvm sandbox token required",
                context={"tool": spec.name},
            )


def _coerce_args(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Best-effort coercion of string args to declared types."""
    properties = schema.get("properties", {})
    out: dict[str, Any] = {}
    for k, v in args.items():
        decl = properties.get(k, {})
        decl_type = decl.get("type") if isinstance(decl, dict) else None
        if decl_type == "integer" and isinstance(v, str):
            try:
                out[k] = int(v)
                continue
            except ValueError:
                pass
        if decl_type == "number" and isinstance(v, str):
            try:
                out[k] = float(v)
                continue
            except ValueError:
                pass
        if decl_type == "boolean" and isinstance(v, str):
            out[k] = v.lower() in ("true", "1", "yes")
            continue
        if decl_type == "object" and isinstance(v, str):
            try:
                out[k] = json.loads(v)
                continue
            except json.JSONDecodeError:
                pass
        if decl_type == "array" and isinstance(v, str):
            try:
                out[k] = json.loads(v)
                continue
            except json.JSONDecodeError:
                pass
        out[k] = v
    return out
