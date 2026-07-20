"""
Approval flow for high-risk actions.

When the Constitution or RBAC returns `require_approval`, the runtime
parks the action here and waits for a human decision. The flow:

  1. The agent decides to take a high-risk action (e.g. "send email",
     "transfer $5 to 0xabc", "post to social").
  2. The policy pipeline returns `require_approval` with a reason and
     citations.
  3. The runtime records a pending Approval and parks the action.
  4. A human (operator) lists pending approvals, reads the action
     details and the policy reasoning, and decides to approve or
     reject.
  5. The runtime resumes the action if approved, or skips it and
     records the rejection.

The runtime-side helper `await_approval(...)` blocks until a decision
is made or a timeout elapses. The control-plane-side endpoints let
operators list, approve, and reject.

Design notes:
  - Approvals are keyed by an opaque `ApprovalId` (string).
  - The state machine is: pending → approved | rejected | expired.
  - The action is captured in full at submission time; an approval
    decision is a single signed verdict, not a new action.
  - Approvals are recorded in the audit chain via the host runtime.
  - Decisions are time-bounded: defaults to 24h. After expiry the
    action is rejected (the human didn't respond in time).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalReason(str, Enum):
    """Why a human review was requested."""

    CONSTITUTION = "constitution"          # Law requires explicit consent
    RBAC = "rbac"                          # RBAC requires elevated role
    FINANCIAL = "financial"                # Spending over threshold
    EXTERNAL_EFFECT = "external_effect"    # Sends data or money outside the platform
    CUSTOM = "custom"                      # Operator-defined policy


@dataclass(slots=True)
class PendingAction:
    """A snapshot of the action waiting for human review."""

    tool_name: str
    arguments: dict[str, Any]
    risk: str
    cost_micro: int
    currency: str
    reasoning: str
    citations: list[str]
    requested_at: str


@dataclass(slots=True)
class ApprovalDecision:
    """A human's verdict on a pending action."""

    decided_by: str
    decided_at: str
    reason: str
    signature: str | None = None  # signed by the operator's key, optional


@dataclass(slots=True)
class Approval:
    id: str
    automaton_id: str
    action: PendingAction
    state: ApprovalState
    created_at: str
    expires_at: str
    decided_at: str | None = None
    decision: ApprovalDecision | None = None
    # Optional callback the runtime registers to be notified when the
    # decision is made. Used by `await_approval()` to wake the loop.
    on_decide: Callable[["Approval"], None] | None = field(default=None, repr=False)
    # Internal: monotonic clock used for expiry checks. We use a separate
    # attribute so tests can mock time.
    _now: Callable[[], float] = field(default=time.time, repr=False)


class ApprovalError(Exception):
    pass


class ApprovalStore:
    """Threadsafe in-process store. The interface matches what a
    Postgres-backed store would expose; swap implementation as needed."""

    def __init__(self, *, default_ttl_seconds: int = 24 * 3600) -> None:
        self._lock = asyncio.Lock()
        self._approvals: dict[str, Approval] = {}
        self._by_automaton: dict[str, set[str]] = {}
        self._default_ttl = default_ttl_seconds

    async def submit(
        self,
        *,
        automaton_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk: str,
        cost_micro: int,
        currency: str,
        reasoning: str,
        citations: list[str],
        reason: ApprovalReason = ApprovalReason.CONSTITUTION,
        ttl_seconds: int | None = None,
        on_decide: Callable[[Approval], None] | None = None,
    ) -> Approval:
        ttl = ttl_seconds or self._default_ttl
        now = time.time()
        aid = f"apv_{uuid.uuid4().hex}"
        approval = Approval(
            id=aid,
            automaton_id=automaton_id,
            action=PendingAction(
                tool_name=tool_name,
                arguments=dict(arguments),
                risk=risk,
                cost_micro=cost_micro,
                currency=currency,
                reasoning=reasoning,
                citations=list(citations),
                requested_at=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            ),
            state=ApprovalState.PENDING,
            created_at=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            expires_at=datetime.fromtimestamp(now + ttl, tz=timezone.utc).isoformat(),
            on_decide=on_decide,
        )
        async with self._lock:
            self._approvals[aid] = approval
            self._by_automaton.setdefault(automaton_id, set()).add(aid)
        return approval

    async def get(self, aid: str) -> Approval | None:
        async with self._lock:
            return self._approvals.get(aid)

    async def list_for_automaton(
        self,
        automaton_id: str,
        *,
        state: ApprovalState | None = None,
    ) -> list[Approval]:
        async with self._lock:
            ids = self._by_automaton.get(automaton_id, set())
            items = [self._approvals[i] for i in ids if i in self._approvals]
        if state is not None:
            items = [a for a in items if a.state == state]
        # Newest first.
        items.sort(key=lambda a: a.created_at, reverse=True)
        return items

    async def list_pending(self) -> list[Approval]:
        async with self._lock:
            items = [a for a in self._approvals.values() if a.state == ApprovalState.PENDING]
        items.sort(key=lambda a: a.created_at, reverse=True)
        return items

    async def decide(
        self,
        aid: str,
        *,
        verdict: ApprovalState,
        decided_by: str,
        reason: str,
        signature: str | None = None,
    ) -> Approval:
        if verdict not in (ApprovalState.APPROVED, ApprovalState.REJECTED):
            raise ApprovalError(f"invalid verdict: {verdict}")
        async with self._lock:
            approval = self._approvals.get(aid)
            if approval is None:
                raise ApprovalError(f"unknown approval: {aid}")
            if approval.state != ApprovalState.PENDING:
                raise ApprovalError(
                    f"approval {aid} is already {approval.state.value}"
                )
            # If past expiry, mark expired instead.
            now = time.time()
            expires_ts = datetime.fromisoformat(approval.expires_at).timestamp()
            if now > expires_ts:
                approval.state = ApprovalState.EXPIRED
                approval.decided_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
                if approval.on_decide is not None:
                    approval.on_decide(approval)
                return approval
            approval.state = verdict
            approval.decided_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
            approval.decision = ApprovalDecision(
                decided_by=decided_by,
                decided_at=approval.decided_at,
                reason=reason,
                signature=signature,
            )
            callback = approval.on_decide
        if callback is not None:
            try:
                callback(approval)
            except Exception:  # noqa: BLE001
                log.exception("approval_callback_failed", extra={"id": aid})
        return approval

    async def expire_due(self) -> int:
        """Walk pending approvals; mark any past their expiry as `expired`.
        Returns the count of approvals expired in this call.
        """
        now = time.time()
        expired: list[Approval] = []
        async with self._lock:
            for a in self._approvals.values():
                if a.state != ApprovalState.PENDING:
                    continue
                try:
                    expires_ts = datetime.fromisoformat(a.expires_at).timestamp()
                except ValueError:
                    continue
                if now > expires_ts:
                    a.state = ApprovalState.EXPIRED
                    a.decided_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
                    expired.append(a)
        for a in expired:
            if a.on_decide is not None:
                try:
                    a.on_decide(a)
                except Exception:  # noqa: BLE001
                    log.exception("approval_callback_failed", extra={"id": a.id})
        return len(expired)

    def to_dict(self, approval: Approval) -> dict[str, Any]:
        """Serialize an Approval for the API. No callable fields."""
        return {
            "id": approval.id,
            "automaton_id": approval.automaton_id,
            "state": approval.state.value,
            "created_at": approval.created_at,
            "expires_at": approval.expires_at,
            "decided_at": approval.decided_at,
            "action": {
                "tool_name": approval.action.tool_name,
                "arguments": approval.action.arguments,
                "risk": approval.action.risk,
                "cost_micro": approval.action.cost_micro,
                "currency": approval.action.currency,
                "reasoning": approval.action.reasoning,
                "citations": approval.action.citations,
                "requested_at": approval.action.requested_at,
            },
            "decision": (
                {
                    "decided_by": approval.decision.decided_by,
                    "decided_at": approval.decision.decided_at,
                    "reason": approval.decision.reason,
                    "signature": approval.decision.signature,
                }
                if approval.decision
                else None
            ),
        }


# ── Runtime helper ───────────────────────────────────────────────


class ApprovalGate:
    """The runtime's view of the approval flow.

    Holds an `ApprovalStore` and a future-resolution helper used by the
    loop to block on a decision. Tests can build a gate with a custom
    store; production wires the default one.
    """

    def __init__(self, store: ApprovalStore | None = None) -> None:
        self.store = store or ApprovalStore()
        # An optional external stop signal. If set, the gate returns
        # when the stop event is set OR when the decision is made,
        # whichever happens first. The runtime wires this to its own
        # stop event so a SIGTERM doesn't leave the loop hanging on
        # a 24h approval.
        self._stop_event: asyncio.Event | None = None

    def bind_stop_event(self, stop_event: asyncio.Event) -> None:
        """Wire an external stop event. submit_and_await returns early
        when the stop event fires."""
        self._stop_event = stop_event

    async def _wait_for_decision(
        self, ev: asyncio.Event, stop_event: asyncio.Event | None, timeout: float
    ) -> None:
        if stop_event is None:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return
        # Wait on both. Whichever fires first wins.
        decision_task = asyncio.create_task(ev.wait())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {decision_task, stop_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                # Timed out.
                raise asyncio.TimeoutError()
        finally:
            for t in (decision_task, stop_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
        # Map approval id → asyncio.Event used to wake `await_approval`.
        self._events: dict[str, asyncio.Event] = {}
        # Map approval id → resolved Approval, for retrieval after wake.
        self._resolved: dict[str, Approval] = {}

    def _make_callback(self, approval_id: str) -> Callable[[Approval], None]:
        # Capture the running loop so the callback (which may run from
        # another thread/coroutine) can wake the awaiter safely.
        def _cb(resolved: Approval) -> None:
            ev = self._events.get(approval_id)
            if ev is not None:
                self._resolved[approval_id] = resolved
                ev.set()

        return _cb

    async def submit_and_await(
        self,
        *,
        automaton_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk: str,
        cost_micro: int,
        currency: str,
        reasoning: str,
        citations: list[str],
        reason: ApprovalReason = ApprovalReason.CONSTITUTION,
        ttl_seconds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> Approval:
        """Submit a high-risk action for human review and block until a
        decision is made or the timeout elapses.

        Returns the resolved Approval (state will be one of approved,
        rejected, or expired).
        """
        ev = asyncio.Event()

        def _on_decide(resolved: Approval) -> None:
            ev.set()

        # Submit with the callback. We patch the callback after submit
        # to make sure we have the real id; this is belt-and-suspenders
        # because the store may call our callback before we can patch.
        approval = await self.store.submit(
            automaton_id=automaton_id,
            tool_name=tool_name,
            arguments=arguments,
            risk=risk,
            cost_micro=cost_micro,
            currency=currency,
            reasoning=reasoning,
            citations=citations,
            reason=reason,
            ttl_seconds=ttl_seconds,
            on_decide=_on_decide,
        )
        # The store may already have fired the callback by the time we
        # get here, so the event is already set. That's fine.
        # Patching again for safety in case decide() is called *after* submit.
        existing = await self.store.get(approval.id)
        if existing is not None:
            existing.on_decide = _on_decide

        try:
            expires_ts = datetime.fromisoformat(approval.expires_at).timestamp()
        except ValueError:
            expires_ts = time.time()
        timeout = timeout_seconds
        if timeout is None:
            timeout = max(1.0, expires_ts - time.time() + 1.0)
        # Wait for the decision, but also watch a stop event so the
        # caller can cancel out of a long wait without leaking timeouts.
        stop_event = self._stop_event
        try:
            await self._wait_for_decision(ev, stop_event, timeout)
        except asyncio.TimeoutError:
            await self.store.expire_due()
        resolved = await self.store.get(approval.id)
        assert resolved is not None
        return resolved

    async def list_for_automaton(
        self, automaton_id: str, *, state: ApprovalState | None = None
    ) -> list[Approval]:
        return await self.store.list_for_automaton(automaton_id, state=state)

    async def list_pending(self) -> list[Approval]:
        return await self.store.list_pending()

    async def decide(
        self,
        aid: str,
        *,
        verdict: ApprovalState,
        decided_by: str,
        reason: str,
        signature: str | None = None,
    ) -> Approval:
        return await self.store.decide(
            aid,
            verdict=verdict,
            decided_by=decided_by,
            reason=reason,
            signature=signature,
        )

    async def get(self, aid: str) -> Approval | None:
        return await self.store.get(aid)
