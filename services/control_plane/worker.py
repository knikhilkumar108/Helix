"""
Runtime worker. Hosts a single Automaton's runtime loop and exposes the
gRPC/REST surface for the supervisor.

The worker:
  * holds an in-process ToolRegistry, Treasury, BudgetController
  * persists state via Postgres and checkpoints to the object store
  * exposes health, metrics, and a control channel
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import asdict
from typing import Any

from core.observability.health import HEALTH
from core.observability.metrics import METRICS
from core.utils.structured_logging import configure_logging
from runtime.loop.loop import AutomatonLoop, LoopConfig
from runtime.loop.loop_init import build_default_loop
from core.types.identifiers import AutomatonId
from core.types.money import Money

log = logging.getLogger(__name__)


class RuntimeWorker:
    def __init__(
        self,
        automaton_id: AutomatonId,
        *,
        initial_balance: Money | None = None,
        loop_config: LoopConfig | None = None,
    ) -> None:
        self.automaton_id = automaton_id
        self.loop_handle: AutomatonLoop = build_default_loop(
            automaton_id,
            initial_balance=initial_balance,
            loop_config=loop_config,
        )
        self._stop = asyncio.Event()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()

        def _stop(*_: Any) -> None:
            log.info("worker_signal_stop")
            self.loop_handle.request_stop()
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _stop)

    async def start(self) -> None:
        HEALTH.register("loop", lambda: self.loop_handle.health().components["loop"])  # type: ignore[arg-type]
        self.install_signal_handlers()
        log.info("worker_starting", extra={"automaton": str(self.automaton_id)})
        await self.loop_handle.run()
        log.info("worker_stopped", extra={"automaton": str(self.automaton_id)})

    async def wait_for_stop(self) -> None:
        await self._stop.wait()

    def snapshot(self) -> dict[str, Any]:
        return self.loop_handle.snapshot()


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Automata runtime worker")
    parser.add_argument("--automaton-id", required=True)
    parser.add_argument("--initial-balance-micro", type=int, default=0)
    parser.add_argument("--currency", default=os.environ.get("AUTOMATA_CURRENCY", "USDC"))
    args = parser.parse_args()

    configure_logging(service="runtime-worker")
    aid = AutomatonId(args.automaton_id)
    bal = Money(args.initial_balance_micro, args.currency) if args.initial_balance_micro else Money.zero(args.currency)
    worker = RuntimeWorker(aid, initial_balance=bal)
    asyncio.run(worker.start())


if __name__ == "__main__":
    main()
