"""
gRPC server. Wire-compatible with the REST surface. Generated stubs are not
checked in by default (proto compilation step generates them); this module
defines the service interface in pure Python so the server is testable
without a protoc step in CI.

The proto definitions live in `schemas/proto/automata.proto`; this file
implements the same semantics.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

import grpc

from core.observability.metrics import METRICS
from core.types.identifiers import AutomatonId
from services.control_plane.registry import AutomatonRegistry

log = logging.getLogger(__name__)


class AutomataServicer:
    def __init__(self, registry: AutomatonRegistry) -> None:
        self.reg = registry

    async def CreateAutomaton(self, request: Any, context: grpc.aio.ServicerContext) -> dict[str, Any]:
        a = self.reg.create(
            name=request.name,
            genesis_prompt=request.genesis_prompt,
            initial_balance=None,
        )
        METRICS.actions_total.labels(service="grpc", tool="CreateAutomaton", verdict="allow").inc()
        return {
            "id": str(a.id),
            "name": a.name,
            "state": a.state.value,
            "public_key": a.public_key,
            "wallet_address": a.wallet_address,
        }

    async def GetAutomaton(self, request: Any, context: grpc.aio.ServicerContext) -> dict[str, Any]:
        a = self.reg.get(AutomatonId(request.id))
        return {
            "id": str(a.id),
            "name": a.name,
            "state": a.state.value,
            "public_key": a.public_key,
            "wallet_address": a.wallet_address,
            "balance_micro": a.balance.micro,
            "currency": a.balance.currency,
        }

    async def ListAutomata(self, request: Any, context: grpc.aio.ServicerContext) -> AsyncIterator[dict[str, Any]]:
        for a in self.reg.list():
            yield {
                "id": str(a.id),
                "name": a.name,
                "state": a.state.value,
            }

    async def Pause(self, request: Any, context: grpc.aio.ServicerContext) -> dict[str, Any]:
        from core.types.automaton import LifecycleState

        self.reg.set_state(AutomatonId(request.id), LifecycleState.PAUSED)
        return {"ok": True}

    async def Resume(self, request: Any, context: grpc.aio.ServicerContext) -> dict[str, Any]:
        from core.types.automaton import LifecycleState

        self.reg.set_state(AutomatonId(request.id), LifecycleState.RUNNING)
        return {"ok": True}

    async def Fund(self, request: Any, context: grpc.aio.ServicerContext) -> dict[str, Any]:
        from core.types.money import Money

        t = self.reg.treasury(AutomatonId(request.id))
        entry = t.credit(
            amount=Money(request.amount_micro, request.currency or "USDC"),
            category="funding:grpc",
        )
        return {"entry_id": entry.id, "amount_micro": entry.amount.micro}

    async def StreamEvents(self, request: Any, context: grpc.aio.ServicerContext) -> AsyncIterator[dict[str, Any]]:
        aid = AutomatonId(request.id)
        # The in-process registry doesn't have a true stream; poll the events list.
        last_idx = 0
        while True:
            events = self.reg.events(aid)
            for e in events[last_idx:]:
                yield {"ts": e["ts"], "kind": e["kind"], "payload": str(e["payload"])}
                last_idx += 1
            await asyncio.sleep(0.5)


async def serve(registry: AutomatonRegistry, *, port: int = 50051) -> None:
    """Bind a gRPC server. Note: this is a *placeholder* showing the wiring
    using a generic handler; the full service requires generated stubs from
    the .proto file. Use `make proto` to generate them."""
    server = grpc.aio.server()
    # The full generated servicer is added in services.grpc_service.
    await server.start()
    server.add_insecure_port(f"0.0.0.0:{port}")
    log.info("grpc_serving", extra={"port": port})
    await server.wait_for_termination()
