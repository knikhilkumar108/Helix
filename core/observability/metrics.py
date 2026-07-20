"""
Prometheus metrics registry and helper. We expose metrics from a single
multiprocess-safe registry; collectors are wired in by each service at
startup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    multiprocess,
)

REGISTRY: Final[CollectorRegistry] = CollectorRegistry()
if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
    multiprocess.MultiProcessCollector(REGISTRY)


@dataclass(slots=True)
class Metrics:
    http_requests_total: Counter
    http_request_duration_seconds: Histogram
    loop_iterations_total: Counter
    loop_iteration_duration_seconds: Histogram
    actions_total: Counter
    policy_decisions_total: Counter
    treasury_balance: Gauge
    tool_executions_total: Counter
    tool_execution_duration_seconds: Histogram
    llm_tokens_total: Counter
    memory_writes_total: Counter
    errors_total: Counter
    replicas_total: Gauge
    queue_depth: Gauge
    active_automata: Gauge


def build_metrics(service: str) -> Metrics:
    return Metrics(
        http_requests_total=Counter(
            "http_requests_total",
            "HTTP requests",
            ["service", "method", "route", "status"],
            registry=REGISTRY,
        ),
        http_request_duration_seconds=Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["service", "method", "route"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=REGISTRY,
        ),
        loop_iterations_total=Counter(
            "loop_iterations_total",
            "Runtime loop iterations",
            ["service", "outcome"],
            registry=REGISTRY,
        ),
        loop_iteration_duration_seconds=Histogram(
            "loop_iteration_duration_seconds",
            "Loop iteration duration in seconds",
            ["service", "stage"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 30, 60, 300),
            registry=REGISTRY,
        ),
        actions_total=Counter(
            "actions_total",
            "Actions executed",
            ["service", "tool", "verdict"],
            registry=REGISTRY,
        ),
        policy_decisions_total=Counter(
            "policy_decisions_total",
            "Policy decisions",
            ["service", "evaluator", "verdict"],
            registry=REGISTRY,
        ),
        treasury_balance=Gauge(
            "treasury_balance_micro",
            "Treasury balance in micro-units",
            ["service", "automaton", "currency"],
            registry=REGISTRY,
        ),
        tool_executions_total=Counter(
            "tool_executions_total",
            "Tool executions",
            ["service", "tool", "outcome"],
            registry=REGISTRY,
        ),
        tool_execution_duration_seconds=Histogram(
            "tool_execution_duration_seconds",
            "Tool execution duration",
            ["service", "tool"],
            buckets=(0.01, 0.1, 0.5, 1, 5, 30, 60, 300, 1800),
            registry=REGISTRY,
        ),
        llm_tokens_total=Counter(
            "llm_tokens_total",
            "LLM tokens consumed",
            ["service", "provider", "model", "direction"],
            registry=REGISTRY,
        ),
        memory_writes_total=Counter(
            "memory_writes_total",
            "Memory writes",
            ["service", "layer"],
            registry=REGISTRY,
        ),
        errors_total=Counter(
            "errors_total",
            "Errors by category",
            ["service", "category", "code"],
            registry=REGISTRY,
        ),
        replicas_total=Gauge(
            "replicas_total",
            "Number of replicas for an automaton",
            ["service", "parent"],
            registry=REGISTRY,
        ),
        queue_depth=Gauge(
            "queue_depth",
            "Queue depth",
            ["service", "queue"],
            registry=REGISTRY,
        ),
        active_automata=Gauge(
            "active_automata",
            "Active automata",
            ["service", "state"],
            registry=REGISTRY,
        ),
    )


SERVICE_LABEL: Final[str] = os.environ.get("AUTOMATA_SERVICE", "automata")
METRICS: Final[Metrics] = build_metrics(SERVICE_LABEL)
