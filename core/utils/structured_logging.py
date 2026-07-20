"""
Structured logging with PII redaction and request correlation.

All services use this logger. Every log line is a single line of JSON with a
fixed set of fields so downstream pipelines (Loki, ES) can index it
efficiently. Redaction is content-based; we never log full request bodies.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

# PII patterns. Conservative — we'd rather redact too much than too little.
_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*([^\s,;]+)"),
)


def _redact(value: str) -> str:
    out = value
    for p in _PII_PATTERNS:
        out = p.sub("[REDACTED]", out)
    return out


_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)
_automaton_id: ContextVar[str | None] = ContextVar("automaton_id", default=None)


def set_request_id(rid: str | None) -> None:
    _request_id.set(rid)


def get_request_id() -> str | None:
    return _request_id.get()


def set_user_id(uid: str | None) -> None:
    _user_id.set(uid)


def set_automaton_id(aid: str | None) -> None:
    _automaton_id.set(aid)


@dataclass(slots=True)
class LogRecord:
    ts: float
    level: str
    service: str
    msg: str
    request_id: str | None = None
    user_id: str | None = None
    automaton_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    exc: str | None = None

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, separators=(",", ":"), sort_keys=True, default=str)


class StructuredHandler(logging.Handler):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = time.time()
            msg = _redact(record.getMessage())
            fields: dict[str, Any] = {}
            for k, v in record.__dict__.items():
                if k in {
                    "name", "msg", "args", "levelname", "levelno", "pathname",
                    "filename", "module", "exc_info", "exc_text", "stack_info",
                    "lineno", "funcName", "created", "msecs", "relativeCreated",
                    "thread", "threadName", "processName", "process", "message",
                    "asctime", "taskName",
                }:
                    continue
                try:
                    json.dumps(v, default=str)
                    fields[k] = v
                except TypeError:
                    fields[k] = repr(v)
            exc: str | None = None
            if record.exc_info:
                import traceback

                exc = _redact("".join(traceback.format_exception(*record.exc_info)))
            line = LogRecord(
                ts=ts,
                level=record.levelname,
                service=self.service,
                msg=msg,
                request_id=_request_id.get(),
                user_id=_user_id.get(),
                automaton_id=_automaton_id.get(),
                fields=fields,
                exc=exc,
            ).to_json()
            with self._lock:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
        except Exception:  # noqa: BLE001
            # Logging must never raise.
            sys.stderr.write("log handler failure\n")


def configure_logging(service: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    h = StructuredHandler(service=service)
    root.addHandler(h)
    # Quiet down noisy third parties
    for noisy in ("uvicorn", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"
