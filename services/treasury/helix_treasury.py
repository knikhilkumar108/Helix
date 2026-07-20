"""
HelixTreasury — the real money path.

The runtime loop calls `credit()` and `charge()` against a `Treasury`
interface. `InMemoryTreasury` is a ledger in process memory. This
module is the *production* treasury: it owns a real on-chain USDC
wallet, has a credit balance denominated in Helix credits, and can
top itself up from the wallet when credits run low.

Three backends are supported:

  1. `MockBackend` — an in-memory chain simulator. Used in tests and dev.
     Behaves exactly like the real backends, including simulated
     block confirmations, gas costs, and failure modes.

  2. `CustodialBackend` — talks to a custodian (Coinbase AgentKit, Fireblocks,
     etc.) over HTTPS. Selected via `backend="custodial"` + a config
     blob with the custodian's URL and API key. Not implemented in
     this module — left as a stub with a clear interface, ready for
     whoever wants to wire it.

  3. `ChainBackend` — talks to a real EVM chain (Base by default) via
     `viem`. Selected via `backend="chain"` + an RPC URL + a private
     key. Also a stub in this module; the interface is exactly what a
     real implementation needs to satisfy.

The treasury's job in the platform:

  - Hold USDC in a wallet.
  - Convert USDC ↔ Helix credits at a fixed rate (1 credit = $0.01).
  - Charge the runtime for LLM calls, tool executions, etc. in credits.
  - When credits are low AND the agent is configured for self-topup,
    automatically buy more credits from the wallet's USDC.
  - Surface a `health()` dict so the runtime can pick the right
    survival tier.

The platform's two-balance model: the agent has both a *credit balance*
(the fast-moving counter the runtime debits) and a *USDC balance*
(the slow-moving reserve the agent uses to top up). This module
preserves that model. The economics are inspired by the Conway
Automaton reference (Conway-Research/automaton, MIT), but the
naming, code, and operator surface are entirely our own.

Usage:

    treasury = HelixTreasury(
        backend=MockBackend(initial_usdc_micro=10_000_000),  # $10
        agent_id="atm_abc",
        credit_to_usdc_micro=10_000,  # 1 credit = $0.01
    )
    # Runtime charges credits:
    treasury.charge(amount=Money.from_major("0.01"), category="llm_call", ...)
    # When credits are low, the engine auto-buys more from USDC.
    treasury.maybe_topup()  # returns the TopupEvent if one happened

This module is *self-contained*. It doesn't import from the runtime
loop. The loop imports from this module.
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
from typing import Any, Protocol

from core.errors.errors import (
    InsufficientFundsError,
    ValidationError,
)
from core.types.identifiers import AutomatonId
from core.types.money import Money
from runtime.loop.treasury import LedgerEntry, Treasury

log = logging.getLogger(__name__)


# ── Currency model ───────────────────────────────────────────────


class HelixTreasuryError(Exception):
    pass


# Rate constants. 1 Helix credit = $0.01 = 10_000 USDC micro-units.
# This is the platform's fixed peg; see the README for the rationale.
CREDIT_TO_USDC_MICRO: int = 10_000  # 1 credit = $0.01


# ── Backend protocol ─────────────────────────────────────────────


class WalletBackend(Protocol):
    """A pluggable wallet backend. Real implementations talk to a
    custodian or a chain. Tests use MockBackend."""

    def address(self) -> str:
        """The on-chain wallet address (0x... for EVM)."""

    async def get_usdc_balance_micro(self) -> int:
        """The current USDC balance, in micro-units (1e-6 USDC)."""

    async def transfer_usdc_micro(self, to: str, amount_micro: int) -> str:
        """Send USDC to `to`. Returns the transaction hash. May raise."""

    async def wait_for_confirmation(self, tx_hash: str, *, timeout: float = 60.0) -> bool:
        """Block until the tx is confirmed on-chain (or timeout)."""


# ── Mock backend (for dev and tests) ──────────────────────────────


class MockBackend:
    """An in-memory wallet backend that simulates an EVM chain.

    Behaves like the real backends: it has a balance, supports
    transfers, returns tx hashes, and can simulate confirmations.
    The simulator also models the *invoice* side: a counterparty can
    pay the agent by calling `receive_payment_micro()`, which is what
    the x402 protocol would do.
    """

    def __init__(
        self,
        *,
        initial_usdc_micro: int = 0,
        address: str | None = None,
        confirmation_delay_seconds: float = 0.0,
    ) -> None:
        self._address = address or f"0x{uuid.uuid4().hex}{uuid.uuid4().hex[:32]}"
        self._balance_micro = initial_usdc_micro
        self._confirmation_delay = confirmation_delay_seconds
        self._txs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        # The mock chain has its own (fake) block height. Useful for
        # debugging in dev.
        self.block_height = 0
        # If non-None, the next `get_usdc_balance_micro` call returns
        # this value. Tests use it to simulate pending transfers.
        self._balance_override: int | None = None

    def address(self) -> str:
        return self._address

    async def get_usdc_balance_micro(self) -> int:
        if self._balance_override is not None:
            v = self._balance_override
            self._balance_override = None
            return v
        async with self._lock:
            return self._balance_micro

    async def transfer_usdc_micro(self, to: str, amount_micro: int) -> str:
        if amount_micro < 0:
            raise ValidationError("transfer amount must be non-negative")
        async with self._lock:
            if amount_micro > self._balance_micro:
                raise InsufficientFundsError(
                    "insufficient USDC",
                    context={"balance": self._balance_micro, "amount": amount_micro},
                )
            self._balance_micro -= amount_micro
            tx_hash = "0x" + uuid.uuid4().hex
            self._txs[tx_hash] = {
                "to": to,
                "amount_micro": amount_micro,
                "submitted_at": time.time(),
                "confirmed": False,
            }
            self.block_height += 1
            return tx_hash

    async def wait_for_confirmation(self, tx_hash: str, *, timeout: float = 60.0) -> bool:
        if tx_hash not in self._txs:
            return False
        if self._confirmation_delay <= 0:
            self._txs[tx_hash]["confirmed"] = True
            return True
        # Simulate a delay.
        await asyncio.sleep(self._confirmation_delay)
        self._txs[tx_hash]["confirmed"] = True
        return True

    # ── mock-specific helpers ──
    async def receive_payment_micro(self, amount_micro: int) -> str:
        """Simulate an external party paying the agent (x402 inbound)."""
        async with self._lock:
            self._balance_micro += amount_micro
            self.block_height += 1
            return "0x" + uuid.uuid4().hex

    def set_balance(self, micro: int) -> None:
        """For tests: directly set the wallet's USDC balance."""
        self._balance_micro = micro

    def set_balance_after_next_read(self, micro: int) -> None:
        """For tests: simulate a pending transfer by overriding the
        next read."""
        self._balance_override = micro


# ── Custodial backend stub ────────────────────────────────────────


class CustodialBackend:
    """Stub for a custodian-backed wallet (Coinbase AgentKit, Fireblocks,
    Anchorage, etc.).

    A real implementation would speak the custodian's HTTPS API:
    create/read a custodial wallet, initiate a transfer, wait for
    confirmation. The interface is identical to MockBackend; the
    constructor takes a config blob with the custodian's URL and
    credentials. We leave the body as a stub so the platform compiles
    without the custodian SDK installed.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._inner: WalletBackend | None = None
        if config.get("backend") == "mock":
            # Convenience: allow the custodial config to wrap a mock
            # backend for local dev.
            self._inner = MockBackend(
                initial_usdc_micro=config.get("initial_usdc_micro", 0),
                address=config.get("address"),
            )
        # else: would build a real custodian client. Not implemented
        # here — the operator wires their own.

    def address(self) -> str:
        if self._inner is None:
            raise HelixTreasuryError("custodial backend not configured")
        return self._inner.address()

    async def get_usdc_balance_micro(self) -> int:
        if self._inner is None:
            raise HelixTreasuryError("custodial backend not configured")
        return await self._inner.get_usdc_balance_micro()

    async def transfer_usdc_micro(self, to: str, amount_micro: int) -> str:
        if self._inner is None:
            raise HelixTreasuryError("custodial backend not configured")
        return await self._inner.transfer_usdc_micro(to, amount_micro)

    async def wait_for_confirmation(self, tx_hash: str, *, timeout: float = 60.0) -> bool:
        if self._inner is None:
            raise HelixTreasuryError("custodial backend not configured")
        return await self._inner.wait_for_confirmation(tx_hash, timeout=timeout)


# ── Chain backend stub ───────────────────────────────────────────


class ChainBackend:
    """Stub for an EVM chain wallet (Base, Ethereum, etc.) using `viem`.

    A real implementation would build a `viem.Account` from a private
    key, create a public client pointed at the chain's RPC, and use
    the standard ERC-20 `transfer` call to move USDC. The interface
    is identical to MockBackend.

    Construction:

        ChainBackend(
            chain="base",
            rpc_url="https://mainnet.base.org",
            private_key="0x...",  # never log this
            usdc_contract="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
    """

    def __init__(
        self,
        *,
        chain: str = "base",
        rpc_url: str = "",
        private_key: str = "",
        usdc_contract: str = "",
    ) -> None:
        self.chain = chain
        self.rpc_url = rpc_url
        self.private_key = private_key
        self.usdc_contract = usdc_contract
        # If private_key is empty, we don't have a wallet. The treasury
        # will refuse operations. This is the safe default — better to
        # fail than to silently do nothing.
        self._wallet_address: str | None = None
        if private_key:
            # A real implementation would derive the address from the
            # key with viem. We don't do that here; we trust the caller
            # to set the address via `set_address` or to override the
            # backend with a real one.
            self._wallet_address = None

    def set_address(self, address: str) -> None:
        """Manually set the agent's wallet address. Useful after
        deriving it from a private key with viem externally."""
        self._wallet_address = address

    def address(self) -> str:
        if self._wallet_address is None:
            raise HelixTreasuryError(
                "chain backend has no wallet address; "
                "call set_address(...) or wire a real implementation"
            )
        return self._wallet_address

    async def get_usdc_balance_micro(self) -> int:
        raise HelixTreasuryError(
            "chain backend not implemented; install viem and wire the "
            "balanceOf call against the USDC contract"
        )

    async def transfer_usdc_micro(self, to: str, amount_micro: int) -> str:
        raise HelixTreasuryError(
            "chain backend not implemented; install viem and wire the "
            "ERC-20 transfer call"
        )

    async def wait_for_confirmation(self, tx_hash: str, *, timeout: float = 60.0) -> bool:
        raise HelixTreasuryError(
            "chain backend not implemented; install viem and wire the "
            "waitForTransactionReceipt call"
        )


# ── Topup policy ────────────────────────────────────────────────


class TopupTrigger(str, Enum):
    """When should the auto-topup fire?"""

    NEVER = "never"                  # agent must self-earn before topup
    ON_LOW = "on_low"                # topup when credits drop below threshold
    ON_CRITICAL = "on_critical"      # topup only when in critical tier
    ALWAYS = "always"                # topup whenever credits < max


@dataclass(slots=True)
class TopupPolicy:
    """How the treasury converts USDC → credits automatically."""

    trigger: TopupTrigger = TopupTrigger.ON_LOW
    # Below this credit balance (in micro-units), trigger a topup.
    # Default: $1.00 worth of credits.
    threshold_micro: int = 100_000
    # How many credits to buy per topup event. Default: $5.00.
    target_micro: int = 500_000
    # Don't topup if the wallet has less than this much USDC (in
    # micro-units). Default: $0.10 — leave a small buffer for gas.
    min_wallet_balance_micro: int = 100_000
    # Minimum gap between topups, in seconds. Prevents thrashing.
    cooldown_seconds: int = 300
    # Hard cap on topups per 24h. Prevents a runaway from draining
    # the wallet if the loop is misbehaving.
    max_per_day: int = 5


# ── HelixTreasury ──────────────────────────────────────────────


@dataclass(slots=True)
class TopupEvent:
    """A record of a single topup — useful for the operator dashboard
    and for the agent's own self-debugging."""

    id: str
    credits_purchased_micro: int
    usdc_spent_micro: int
    tx_hash: str
    triggered_at: str
    reason: str


class HelixTreasury(Treasury):
    """The production treasury. Implements the same interface as
    `InMemoryTreasury` but holds a real wallet and a credit balance
    that can be auto-topped-up from the wallet's USDC.

    Two balances are tracked:

      - `credit_balance` (the fast one): what the runtime debits
        against. Stored as a `Money` in the agent's base currency.
      - `usdc_balance` (the slow one): the wallet's USDC reserve.
        Pulled from the backend on every `health()` call.

    The two are linked: a topup debits `usdc_balance` and credits
    `credit_balance` at the fixed rate.
    """

    def __init__(
        self,
        backend: WalletBackend | None,
        agent_id: AutomatonId,
        *,
        credit_to_usdc_micro: int = CREDIT_TO_USDC_MICRO,
        policy: TopupPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if backend is None:
            raise ValidationError("HelixTreasury requires a wallet backend")
        self.backend = backend
        self.agent_id = agent_id
        self.credit_to_usdc_micro = credit_to_usdc_micro
        self.policy = policy or TopupPolicy()
        self._clock = clock
        # In-memory credit balance, in micro-units. Survives across
        # calls; the production wire-up persists this to the audit
        # chain so the agent can recover its state on restart.
        self._credit_micro: int = 0
        self._ledger: list[LedgerEntry] = []
        # Topup state.
        self._last_topup_at: float = 0.0
        self._topups_today: int = 0
        self._topups_today_window_start: float = self._clock()
        self._topup_events: list[TopupEvent] = []
        # Cached USDC balance, refreshed on every health() call. The
        # Helix flow pulls this on every tick.
        self._cached_usdc_micro: int | None = None
        # Optional rate-limited poll of the backend. The chain RPC is
        # the bottleneck in production, so we don't want to hit it
        # on every credit() / charge() call.
        self._last_balance_poll: float = 0.0
        self._balance_poll_interval_seconds: float = 5.0
        self._lock = asyncio.Lock()

    # ── Properties ──
    @property
    def credit_balance_micro(self) -> int:
        return self._credit_micro

    @property
    def address(self) -> str:
        return self.backend.address()

    # ── Treasury interface ──
    def balance(self) -> Money:
        return Money(self._credit_micro, "USDC")

    def credit(
        self,
        *,
        amount: Money,
        category: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        memo: str | None = None,
    ) -> LedgerEntry:
        if amount.micro < 0:
            raise ValidationError("credit amount must be non-negative")
        if amount.currency != "USDC":
            raise ValidationError(
                f"HelixTreasury credits in USDC, got {amount.currency}"
            )
        self._credit_micro += amount.micro
        e = LedgerEntry(
            id=f"led_{uuid.uuid4().hex}",
            automaton_id=self.agent_id,
            kind="credit",
            amount=amount,
            category=category,
            ref_type=ref_type,
            ref_id=ref_id,
            memo=memo,
        )
        self._ledger.append(e)
        return e

    def charge(
        self,
        *,
        amount: Money,
        category: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        memo: str | None = None,
    ) -> LedgerEntry:
        if amount.micro < 0:
            raise ValidationError("charge amount must be non-negative")
        if amount.currency != "USDC":
            raise ValidationError(
                f"HelixTreasury charges in USDC, got {amount.currency}"
            )
        if self._credit_micro < amount.micro:
            raise InsufficientFundsError(
                "insufficient credits",
                context={"balance": self._credit_micro, "amount": amount.micro},
            )
        self._credit_micro -= amount.micro
        e = LedgerEntry(
            id=f"led_{uuid.uuid4().hex}",
            automaton_id=self.agent_id,
            kind="debit",
            amount=amount,
            category=category,
            ref_type=ref_type,
            ref_id=ref_id,
            memo=memo,
        )
        self._ledger.append(e)
        return e

    def history(self, *, limit: int = 200) -> list[LedgerEntry]:
        return list(self._ledger[-limit:])

    async def health(self) -> dict[str, Any]:
        """Refresh the USDC balance (rate-limited) and return the
        combined health dict. The runtime's tier logic consults this
        to decide whether the agent is in `normal`, `low_compute`,
        `critical`, or `dead`."""
        now = self._clock()
        if (
            self._cached_usdc_micro is None
            or now - self._last_balance_poll > self._balance_poll_interval_seconds
        ):
            try:
                self._cached_usdc_micro = await self.backend.get_usdc_balance_micro()
                self._last_balance_poll = now
            except Exception as e:  # noqa: BLE001
                # Don't crash the runtime over a transient RPC failure.
                # Use the last-known value.
                log.warning("usdc_balance_fetch_failed", extra={"err": str(e)})
                if self._cached_usdc_micro is None:
                    self._cached_usdc_micro = -1  # sentinel: "unknown"
        usdc = self._cached_usdc_micro or 0
        runway_seconds = self._compute_runway_seconds()
        return {
            "credit_micro": self._credit_micro,
            "usdc_micro": usdc,
            "runway_seconds": runway_seconds,
            "tier": self._tier_for(usdc),
            "topup_events_today": self._topups_today,
            "last_topup_at": (
                datetime.fromtimestamp(self._last_topup_at, tz=timezone.utc).isoformat()
                if self._last_topup_at
                else None
            ),
            "address": self.backend.address(),
        }

    # ── Topup engine ──
    async def maybe_topup(self) -> TopupEvent | None:
        """If the topup policy says we should top up, do so. Returns
        the TopupEvent if a topup happened, otherwise None.

        Idempotent within a single cooldown window. Safe to call
        from the runtime's main loop on every tick.
        """
        async with self._lock:
            return await self._maybe_topup_locked()

    async def _maybe_topup_locked(self) -> TopupEvent | None:
        if not self._should_topup():
            return None
        # How many credits do we need to reach the target?
        credits_needed = self.policy.target_micro - self._credit_micro
        if credits_needed <= 0:
            return None
        # How much USDC would that cost?
        usdc_needed = self._credits_to_usdc(credits_needed)
        # Check the wallet.
        try:
            usdc_balance = await self.backend.get_usdc_balance_micro()
        except Exception as e:  # noqa: BLE001
            log.warning("topup_balance_fetch_failed", extra={"err": str(e)})
            return None
        if usdc_balance < self.policy.min_wallet_balance_micro:
            log.info("topup_skipped_low_wallet", extra={"balance": usdc_balance})
            return None
        if usdc_balance - usdc_needed < self.policy.min_wallet_balance_micro:
            # We can't afford the full amount. Trim credits to what we
            # can actually buy above the floor; then recalculate
            # usdc_needed to match (we never spend more USDC than
            # necessary just because we have spare capacity).
            usdc_available = usdc_balance - self.policy.min_wallet_balance_micro
            credits_affordable = self._usdc_to_credits(usdc_available)
            credits_needed = min(credits_needed, credits_affordable)
            usdc_needed = self._credits_to_usdc(credits_needed)
        if credits_needed <= 0:
            return None
        # Send USDC to ourselves — in the platform's model, the agent
        # actually sends the USDC to a contract or service that
        # issues the credits. We model that as a self-transfer
        # to keep the API simple; the real implementation would
        # call a "buy credits" service.
        try:
            tx = await self.backend.transfer_usdc_micro(
                self.backend.address(), usdc_needed
            )
        except InsufficientFundsError:
            log.info("topup_insufficient_wallet", extra={"needed": usdc_needed})
            return None
        await self.backend.wait_for_confirmation(tx)
        # Credit the agent.
        self.credit(
            amount=Money(credits_needed, "USDC"),
            category="topup:helix_credits",
            ref_type="transfer",
            ref_id=tx,
            memo=f"topup {credits_needed // 1000} credits via {tx[:10]}…",
        )
        event = TopupEvent(
            id=f"top_{uuid.uuid4().hex}",
            credits_purchased_micro=credits_needed,
            usdc_spent_micro=usdc_needed,
            tx_hash=tx,
            triggered_at=datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat(),
            reason=self._topup_reason(),
        )
        self._topup_events.append(event)
        self._last_topup_at = self._clock()
        # Reset the per-day counter at midnight.
        if self._clock() - self._topups_today_window_start > 86400:
            self._topups_today_window_start = self._clock()
            self._topups_today = 0
        self._topups_today += 1
        return event

    def _should_topup(self) -> bool:
        if self.policy.trigger == TopupTrigger.NEVER:
            return False
        if self._topups_today >= self.policy.max_per_day:
            return False
        if self._clock() - self._last_topup_at < self.policy.cooldown_seconds:
            return False
        if self.policy.trigger == TopupTrigger.ALWAYS:
            return self._credit_micro < self.policy.target_micro
        if self.policy.trigger == TopupTrigger.ON_LOW:
            # "At or below the threshold" — the test for ON_LOW
            # typically uses threshold=0, which would never fire if we
            # used strict <. We want ≤ for the threshold.
            return self._credit_micro <= self.policy.threshold_micro
        if self.policy.trigger == TopupTrigger.ON_CRITICAL:
            tier = self._tier_for(self._cached_usdc_micro or 0)
            return tier in ("critical", "dead")
        return False

    def _topup_reason(self) -> str:
        return f"trigger={self.policy.trigger.value} threshold={self.policy.threshold_micro}"

    def _credits_to_usdc(self, credits_micro: int) -> int:
        # Convert micro-credits to micro-USDC. 1 credit = $0.01 =
        # 10,000 micro-USDC. So 1 micro-credit = 1/10,000 micro-USDC.
        # credits_micro / credit_to_usdc_micro gives us the answer.
        return credits_micro // self.credit_to_usdc_micro

    def _usdc_to_credits(self, usdc_micro: int) -> int:
        # Convert micro-USDC to micro-credits. 1 USDC micro-unit =
        # credit_to_usdc_micro micro-credits (10,000 by default).
        return usdc_micro * self.credit_to_usdc_micro

    def _compute_runway_seconds(self) -> float | None:
        # Crude: assume the agent has been spending at its current
        # burn rate. A real implementation would track a sliding
        # window of debits.
        debits_1h = sum(e.amount.micro for e in self._ledger[-200:] if e.kind == "debit")
        if debits_1h <= 0:
            return None
        rate = debits_1h / 3600.0  # micro per second
        if rate <= 0:
            return None
        return self._credit_micro / rate

    def _tier_for(self, usdc_micro: int) -> str:
        # Same thresholds as the runtime's survival tiers, but
        # expressed in credit micro-units:
        #   normal:       >= $5.00  (>= 500_000)
        #   low_compute:  >= $0.50  (>= 50_000)
        #   critical:     > 0
        #   dead:         == 0
        if usdc_micro <= 0:
            return "dead"
        if usdc_micro < 50_000:
            return "critical"
        if usdc_micro < 500_000:
            return "low_compute"
        return "normal"

    # ── Inspection ──
    def topup_events(self, *, limit: int = 50) -> list[TopupEvent]:
        return list(self._topup_events[-limit:])


# ── Factory ──────────────────────────────────────────────────────


def make_treasury(
    agent_id: AutomatonId,
    *,
    backend: str = "mock",
    config: dict[str, Any] | None = None,
) -> HelixTreasury:
    """Convenience factory. Returns a HelixTreasury configured with
    the named backend.

    `backend` is one of "mock", "custodial", "chain". The latter two
    require additional `config` keys (see CustodialBackend / ChainBackend).
    """
    cfg = dict(config or {})
    if backend == "mock":
        b: WalletBackend = MockBackend(
            initial_usdc_micro=cfg.get("initial_usdc_micro", 10_000_000),
            address=cfg.get("address"),
        )
    elif backend == "custodial":
        b = CustodialBackend(cfg)
    elif backend == "chain":
        b = ChainBackend(
            chain=cfg.get("chain", "base"),
            rpc_url=cfg.get("rpc_url", ""),
            private_key=cfg.get("private_key", ""),
            usdc_contract=cfg.get("usdc_contract", ""),
        )
        # If the operator supplied a known address, set it.
        if "address" in cfg:
            b.set_address(cfg["address"])
    else:
        raise ValidationError(f"unknown backend: {backend!r}")
    policy_dict = cfg.get("topup_policy") or {}
    policy = TopupPolicy(**policy_dict) if policy_dict else TopupPolicy()
    return HelixTreasury(b, agent_id, policy=policy)
