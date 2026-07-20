"""
HTTP control plane for the Automata platform.

The control plane exposes:
  - Tenant & user management
  - Automaton CRUD, lifecycle controls
  - Wallet, funding, treasury
  - Memory & decisions
  - Marketplace browse/order
  - Audit
  - Health & metrics

It is intentionally thin: persistence and policy are handled by dedicated
services. Handlers validate input, call the appropriate service, and shape
the response.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.errors.errors import PlatformError
from core.observability.health import HEALTH
from core.observability.metrics import METRICS
from core.observability.tracing import span
from core.utils.structured_logging import (
    configure_logging,
    new_request_id,
    set_request_id,
    set_user_id,
)
from runtime.loop.loop_init import build_default_loop
from runtime.loop.treasury import InMemoryTreasury
from core.types.identifiers import AutomatonId
from core.types.money import Money

from .registry import AutomatonRegistry
from .routes import automata as automata_routes
from .routes import treasury as treasury_routes
from .routes import memory as memory_routes
from .routes import marketplace as marketplace_routes
from .routes import audit as audit_routes
from .routes import auth as auth_routes
from .routes import approvals as approvals_routes
from .routes import inbox as inbox_routes
from .routes import x402 as x402_routes
from .routes import dashboard as dashboard_routes
from services.approvals.approvals import ApprovalGate
from services.bootstrap import BootstrapService, make_bootstrap
from services.dashboard import EventBus
from services.messaging import InboxService
from services.payments import X402Registry

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(service="control-plane")
    HEALTH.register("self", lambda: _self_health())
    yield


def _self_health():
    from core.types.automaton import ComponentHealth

    async def _h() -> ComponentHealth:
        return ComponentHealth(status="up", latency_ms=0.1)

    return _h()


def create_app(registry: AutomatonRegistry | None = None) -> FastAPI:
    app = FastAPI(
        title="Automata Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    reg = registry or AutomatonRegistry()
    app.state.registry = reg
    app.state.approval_gate = ApprovalGate()
    app.state.x402_registry = X402Registry()
    # The platform-wide InboxService. Routes that need it
    # check for `None` and return 503 if it isn't set.
    # Operators wire a real service in production; the
    # default is `None` so the dev path doesn't carry an
    # in-memory store that masks production bugs.
    app.state.inbox_service = None
    # The bootstrap service. If set, `POST /v1/automata`
    # uses it; otherwise the route falls back to a plain
    # `registry.create()`. The default is `None` so dev
    # mode works without configuring skills + memory.
    # Production wires a real BootstrapService.
    app.state.bootstrap = None
    # The dashboard bus. The dashboard's WebSocket route
    # reads from this; components publish events into it.
    # The default is an empty bus so dev mode has a
    # working (but empty) dashboard.
    app.state.dashboard_bus = EventBus()

    @app.middleware("http")
    async def request_middleware(request: Request, call_next):
        rid = request.headers.get("x-request-id") or new_request_id()
        set_request_id(rid)
        set_user_id(request.headers.get("x-user-id"))
        started = time.time()
        try:
            with span(
                "http.request",
                method=request.method,
                path=request.url.path,
            ):
                response = await call_next(request)
        except PlatformError as e:
            response = JSONResponse(
                status_code=e.http_status, content=e.to_dict()
            )
        except Exception as e:  # noqa: BLE001
            log.exception("unhandled_error", extra={"path": request.url.path})
            response = JSONResponse(
                status_code=500,
                content={"code": "platform.internal", "message": str(e)},
            )
        elapsed = time.time() - started
        route = request.scope.get("route").path if request.scope.get("route") else request.url.path
        METRICS.http_requests_total.labels(
            service="control-plane",
            method=request.method,
            route=route,
            status=response.status_code,
        ).inc()
        METRICS.http_request_duration_seconds.labels(
            service="control-plane",
            method=request.method,
            route=route,
        ).observe(elapsed)
        response.headers["x-request-id"] = rid
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        report = await HEALTH.report()
        return {
            "status": report.status,
            "components": {k: {"status": v.status, "latency_ms": v.latency_ms, "message": v.message} for k, v in report.components.items()},
            "checked_at": report.checked_at.isoformat(),
        }

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(auth_routes.router, prefix="/v1/auth", tags=["auth"])
    app.include_router(automata_routes.router, prefix="/v1/automata", tags=["automata"])
    app.include_router(treasury_routes.router, prefix="/v1/treasury", tags=["treasury"])
    app.include_router(memory_routes.router, prefix="/v1/memory", tags=["memory"])
    app.include_router(marketplace_routes.router, prefix="/v1/marketplace", tags=["marketplace"])
    app.include_router(audit_routes.router, prefix="/v1/audit", tags=["audit"])
    app.include_router(approvals_routes.router, prefix="/v1/approvals", tags=["approvals"])
    app.include_router(x402_routes.router, prefix="/v1/x402", tags=["x402"])
    app.include_router(inbox_routes.router, prefix="/v1/inbox", tags=["inbox"])
    app.include_router(dashboard_routes.router, prefix="/v1/dashboard", tags=["dashboard"])
    return app


app = create_app()


def main() -> None:
    uvicorn.run(
        "services.control_plane.api:app",
        host="0.0.0.0",
        port=8080,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
