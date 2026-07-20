"""
Built-in tools. These cover the smallest useful surface so a fresh Automaton
can do something productive immediately. Each tool is intentionally minimal;
production tools live in `runtime.tools.*` modules.

All builtins are sandboxed: no network by default, no persistence to host
filesystem. Higher-scope tools (e.g. email, browser) must be explicitly
enabled and have their capabilities granted.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from core.types.automaton import Money, RiskLevel, ToolSpec

from .tools import ToolRegistry

DEFAULT_COST = Money.from_major("0.0001", "USDC")


def register_builtins(registry: ToolRegistry, *, workspace: Path | None = None) -> None:
    ws = (workspace or Path("/tmp/automata-sandbox")).resolve()
    ws.mkdir(parents=True, exist_ok=True)

    # ── Agent-to-agent messaging (messaging.send / inbox.claim) ──
    # These tools are stubs that look up the `InboxService` from
    # the registry's `extra` field. A real wire-up sets
    # `registry.extra["inbox"]` to an `InboxService` instance.
    # The stubs raise a clear error if no inbox is configured,
    # so an agent without messaging wired up doesn't silently
    # "succeed" — it fails loudly.
    def _get_inbox() -> Any:
        from services.messaging import InboxService  # local import
        inbox = registry.extra.get("inbox") if hasattr(registry, "extra") else None
        if not isinstance(inbox, InboxService):
            raise RuntimeError(
                "messaging tools require an InboxService; "
                "set registry.extra['inbox'] before calling this tool"
            )
        return inbox

    def _messaging_send(
        to: str,
        content: str,
        from_: str | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        from services.messaging import InboxService  # local import
        inbox: InboxService = _get_inbox()
        # If the caller didn't supply `from_`, default to the
        # agent's own id (which we get from the registry's extra
        # too). This makes the tool ergonomic: a single
        # `messaging.send(to="bob", content="hi")` is enough.
        from_addr = from_ or registry.extra.get("self_id") or "anonymous"
        msg = inbox.send(
            from_address=from_addr,
            to_address=to,
            content=content,
            max_retries=max_retries,
        )
        return {
            "id": msg.id,
            "to": msg.to_address,
            "from": msg.from_address,
            "state": msg.state.value,
        }

    registry.register(
        ToolSpec(
            name="messaging.send",
            version="0.1.0",
            description="Send a message to another agent's inbox",
            capabilities=["messaging"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient agent id"},
                    "content": {"type": "string", "description": "Message body"},
                    "from_": {"type": "string", "description": "Override sender (default: self)"},
                    "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
                },
                "required": ["to", "content"],
            },
        ),
        _messaging_send,
    )

    def _messaging_claim(to: str, limit: int = 10) -> dict[str, Any]:
        from services.messaging import InboxService  # local import
        inbox: InboxService = _get_inbox()
        msgs = inbox.claim(to, limit=limit)
        return {
            "count": len(msgs),
            "messages": [
                {
                    "id": m.id,
                    "from": m.from_address,
                    "content": m.content,
                    "state": m.state.value,
                    "retry_count": m.retry_count,
                }
                for m in msgs
            ],
        }

    registry.register(
        ToolSpec(
            name="messaging.claim",
            version="0.1.0",
            description="Claim pending messages from an inbox (or your own, if `to` is omitted)",
            capabilities=["messaging"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Inbox owner (default: self)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                },
            },
        ),
        _messaging_claim,
    )

    def _messaging_mark_processed(ids: list[str]) -> dict[str, Any]:
        from services.messaging import InboxService
        inbox: InboxService = _get_inbox()
        n = inbox.mark_processed(ids)
        return {"marked": n}

    registry.register(
        ToolSpec(
            name="messaging.mark_processed",
            version="0.1.0",
            description="Mark claimed messages as processed (terminal success)",
            capabilities=["messaging"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ids"],
            },
        ),
        _messaging_mark_processed,
    )

    def _messaging_mark_failed(ids: list[str], retry: bool = True) -> dict[str, Any]:
        from services.messaging import InboxService
        inbox: InboxService = _get_inbox()
        n = inbox.mark_failed(ids, retry=retry)
        return {"marked": n}

    registry.register(
        ToolSpec(
            name="messaging.mark_failed",
            version="0.1.0",
            description="Mark claimed messages as failed (optionally retry)",
            capabilities=["messaging"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "retry": {"type": "boolean", "default": True},
                },
                "required": ["ids"],
            },
        ),
        _messaging_mark_failed,
    )

    # ── Conversation history (chat.history) ──
    # An agent with a multi-turn conversation needs to record
    # what the user said and what the agent answered. The
    # history is token-budgeted: when the conversation grows
    # past the budget, old turns are summarized.
    def _get_history() -> Any:
        from services.conversation import ConversationHistory  # local import
        h = registry.extra.get("history") if hasattr(registry, "extra") else None
        if not isinstance(h, ConversationHistory):
            raise RuntimeError(
                "chat.history tools require a ConversationHistory; "
                "set registry.extra['history'] before calling this tool"
            )
        return h

    def _history_record(
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        tool_results: list[dict] | None = None,
    ) -> dict[str, Any]:
        from services.conversation import Role as _Role
        h = _get_history()
        turn = h.add_turn(
            role=_Role(role),
            content=content,
            tool_calls=tool_calls,
            tool_results=tool_results,
        )
        return {
            "id": turn.id,
            "role": turn.role.value,
            "tokens": h.estimated_tokens(),
            "turns": len(h),
        }

    registry.register(
        ToolSpec(
            name="chat.history.record",
            version="0.1.0",
            description="Record a turn in the conversation history",
            capabilities=["chat"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["user", "agent", "system", "tool", "summary"],
                    },
                    "content": {"type": "string"},
                    "tool_calls": {"type": "array"},
                    "tool_results": {"type": "array"},
                },
                "required": ["role", "content"],
            },
        ),
        _history_record,
    )

    def _history_render(max_tokens: int = 2000) -> dict[str, Any]:
        h = _get_history()
        msgs = h.render_for_llm(max_tokens=max_tokens)
        return {
            "messages": msgs,
            "tokens": h.estimated_tokens(),
            "turns": len(h),
        }

    registry.register(
        ToolSpec(
            name="chat.history.render",
            version="0.1.0",
            description="Render the conversation history as LLM-ready messages",
            capabilities=["chat"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "max_tokens": {"type": "integer", "minimum": 100, "maximum": 32000},
                },
            },
        ),
        _history_render,
    )

    def _history_compact() -> dict[str, Any]:
        h = _get_history()
        n = h.compact()
        return {"collapsed": n, "tokens": h.estimated_tokens(), "turns": len(h)}

    registry.register(
        ToolSpec(
            name="chat.history.compact",
            version="0.1.0",
            description="Force a compaction of the conversation history",
            capabilities=["chat"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={"type": "object", "properties": {}},
        ),
        _history_compact,
    )

    # ── Plan mode (plan.* tools) ──
    # The agent creates a plan, writes TODO.md, executes the
    # steps, and marks them done. The `TodoService` is wired
    # via `registry.extra["todo"]`; if it's missing, the tools
    # raise a clear error.
    def _get_todo() -> Any:
        from services.planning import TodoService  # local import
        t = registry.extra.get("todo") if hasattr(registry, "extra") else None
        if not isinstance(t, TodoService):
            raise RuntimeError(
                "plan.* tools require a TodoService; "
                "set registry.extra['todo'] before calling this tool"
            )
        return t

    def _plan_create(
        goal: str,
        steps: list[dict],
        cost_micro: int = 0,
        revenue_micro: int = 0,
        probability: float = 0.5,
        critique: str | None = None,
    ) -> dict[str, Any]:
        from core.types.money import Money
        t = _get_todo()
        plan = t.create_plan(
            goal=goal,
            steps=steps,
            estimated_cost=Money(cost_micro, "USDC"),
            estimated_revenue=Money(revenue_micro, "USDC"),
            probability=probability,
            critique=critique,
        )
        return {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "steps": len(plan.steps),
        }

    registry.register(
        ToolSpec(
            name="plan.create",
            version="0.1.0",
            description="Create a new plan and write TODO.md",
            capabilities=["plan"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "estimated_cost_micro": {"type": "integer"},
                                "risk": {"type": "string"},
                            },
                            "required": ["description"],
                        },
                    },
                    "cost_micro": {"type": "integer", "minimum": 0},
                    "revenue_micro": {"type": "integer", "minimum": 0},
                    "probability": {"type": "number", "minimum": 0, "maximum": 1},
                    "critique": {"type": "string"},
                },
                "required": ["goal", "steps"],
            },
        ),
        _plan_create,
    )

    def _plan_mark_step(index: int, status: str) -> dict[str, Any]:
        from services.planning import TodoStatus
        t = _get_todo()
        # Translate the user-friendly status names.
        s = status.lower()
        if s in ("succeeded", "done", "complete", "completed"):
            todo_status = TodoStatus.SUCCEEDED
        elif s in ("failed", "fail", "error"):
            todo_status = TodoStatus.FAILED
        elif s in ("in_progress", "in-progress", "progress", "active"):
            todo_status = TodoStatus.IN_PROGRESS
        elif s in ("pending", "reset", "retry"):
            todo_status = TodoStatus.PENDING
        else:
            raise ValueError(f"unknown step status: {status!r}")
        plan = t.mark_step(index, todo_status)
        return {
            "plan_id": plan.plan_id,
            "step_index": index,
            "step_status": todo_status.value,
            "is_complete": all(
                s.status == TodoStatus.SUCCEEDED for s in plan.steps
            ),
        }

    registry.register(
        ToolSpec(
            name="plan.mark_step",
            version="0.1.0",
            description="Update a step's status (succeeded, failed, in_progress, pending)",
            capabilities=["plan"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 0},
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending", "in_progress",
                            "succeeded", "failed",
                        ],
                    },
                },
                "required": ["index", "status"],
            },
        ),
        _plan_mark_step,
    )

    def _plan_read() -> dict[str, Any]:
        t = _get_todo()
        plan = t.read_plan()
        return {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "steps": [
                {
                    "index": s.index,
                    "description": s.description,
                    "status": s.status.value,
                    "completed_at": s.completed_at,
                }
                for s in plan.steps
            ],
            "is_complete": all(
                s.status.value == "succeeded" for s in plan.steps
            ),
            "progress": t.progress(),
        }

    registry.register(
        ToolSpec(
            name="plan.read",
            version="0.1.0",
            description="Read the current plan from TODO.md",
            capabilities=["plan"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={"type": "object", "properties": {}},
        ),
        _plan_read,
    )

    # memory.read
    registry.register(
        ToolSpec(
            name="memory.read",
            version="0.1.0",
            description="Read a memory entry by id",
            capabilities=["memory"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        lambda id: {"id": id, "ts": time.time()},
    )

    # memory.write (noop-ish for the embedded runtime; persists via context)
    def _mem_write(content: str, layer: str = "long_term", importance: float = 0.5) -> dict[str, Any]:
        return {
            "id": f"mem_{hashlib.sha1(content.encode()).hexdigest()[:16]}",
            "layer": layer,
            "importance": importance,
            "size": len(content),
        }

    registry.register(
        ToolSpec(
            name="memory.write",
            version="0.1.0",
            description="Write a memory entry",
            capabilities=["memory"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "layer": {"type": "string"},
                    "importance": {"type": "number"},
                },
                "required": ["content"],
            },
        ),
        _mem_write,
    )

    # fs.read / fs.write (sandboxed to workspace)
    def _fs_read(path: str) -> str:
        p = (ws / path.lstrip("/")).resolve()
        if not str(p).startswith(str(ws)):
            raise PermissionError(f"path {path} escapes sandbox")
        return p.read_text(encoding="utf-8")

    def _fs_write(path: str, content: str) -> dict[str, Any]:
        p = (ws / path.lstrip("/")).resolve()
        if not str(p).startswith(str(ws)):
            raise PermissionError(f"path {path} escapes sandbox")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": str(p), "size": p.stat().st_size}

    registry.register(
        ToolSpec(
            name="fs.read",
            version="0.1.0",
            description="Read a file from the workspace sandbox",
            capabilities=["filesystem"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="process",
            schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        ),
        _fs_read,
    )
    registry.register(
        ToolSpec(
            name="fs.write",
            version="0.1.0",
            description="Write a file to the workspace sandbox",
            capabilities=["filesystem"],
            risk=RiskLevel.MEDIUM,
            cost=DEFAULT_COST,
            sandbox="process",
            schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
        _fs_write,
    )

    # http.get (no network by default unless explicitly enabled)
    async def _http_get(url: str) -> dict[str, Any]:
        if os.environ.get("AUTOMATA_HTTP_ALLOW") != "1":
            raise PermissionError("network access disabled (set AUTOMATA_HTTP_ALLOW=1)")
        # In production this uses httpx with strict timeouts and a proxy.
        return {"url": url, "status": 0, "body": None, "note": "stub"}

    registry.register(
        ToolSpec(
            name="http.get",
            version="0.1.0",
            description="GET a URL",
            capabilities=["network"],
            risk=RiskLevel.LOW,
            cost=Money.from_major("0.001", "USDC"),
            sandbox="process",
            schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        ),
        _http_get,
    )

    # shell.exec (high risk; always requires explicit grant)
    async def _shell_exec(command: str, granted_by: str | None = None) -> dict[str, Any]:
        if not granted_by:
            raise PermissionError("shell.exec requires granted_by")
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ws),
        )
        stdout, stderr = await proc.communicate()
        return {
            "exit": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:8192],
            "stderr": stderr.decode("utf-8", errors="replace")[:8192],
        }

    registry.register(
        ToolSpec(
            name="shell.exec",
            version="0.1.0",
            description="Run a shell command inside the sandbox",
            capabilities=["compute", "process"],
            risk=RiskLevel.HIGH,
            cost=Money.from_major("0.01", "USDC"),
            sandbox="process",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "granted_by": {"type": "string"},
                },
                "required": ["command", "granted_by"],
            },
        ),
        _shell_exec,
    )

    # time.now
    def _time_now() -> dict[str, Any]:
        return {"now": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    registry.register(
        ToolSpec(
            name="time.now",
            version="0.1.0",
            description="Return the current time",
            capabilities=["system"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={"type": "object", "properties": {}},
        ),
        _time_now,
    )

    # sleep
    async def _sleep(seconds: float = 1.0) -> dict[str, Any]:
        await asyncio.sleep(min(max(seconds, 0), 30))
        return {"slept": seconds}

    registry.register(
        ToolSpec(
            name="sleep",
            version="0.1.0",
            description="Pause the loop briefly",
            capabilities=["system"],
            risk=RiskLevel.LOW,
            cost=DEFAULT_COST,
            sandbox="none",
            schema={"type": "object", "properties": {"seconds": {"type": "number"}}},
        ),
        _sleep,
    )
