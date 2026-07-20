"""
OpenTelemetry tracer. No-op by default; activated when OTLP endpoint is
configured. We keep imports lazy so deployments without otel still work.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

_NOOP: Any = None


def _noop_span(*args: Any, **kwargs: Any) -> Any:
    @contextmanager
    def _cm() -> Iterator[Any]:
        yield None

    return _cm()


class _NoopTracer:
    @contextmanager
    def start_as_current_span(self, name: str, **kwargs: Any) -> Iterator[Any]:
        yield _noop_span()


_TRACER: Any = _NoopTracer()


def configure_tracing(service: str) -> None:
    """Initialize OpenTelemetry. Falls back to no-op if otel is absent."""
    global _TRACER
    endpoint = os.environ.get("AUTOMATA_OTLP")
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": service})
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer(service)
    except Exception:  # noqa: BLE001
        # Tracing is best-effort; if the otel stack is missing, no-op.
        _TRACER = _NoopTracer()


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    with _TRACER.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            try:
                sp.set_attribute(k, v)
            except Exception:  # noqa: BLE001
                pass
        yield sp


@asynccontextmanager
async def aspam(name: str, **attrs: Any) -> AsyncIterator[Any]:
    with _TRACER.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            with contextlib.suppress(Exception):
                sp.set_attribute(k, v)
        yield sp
