"""
Common identifier types used across the platform.

Identifiers are always opaque strings. Helpers validate format but never
mutate. Crypto-grade IDs (UUIDv4/v7, Ed25519 public keys, content hashes)
are produced by their respective subsystems.

We use a *thin* wrapper that subclasses `str` to get the hashing and
equality semantics of strings, plus a static type for the ID class. This
is faster, hash-stable, and avoids the `__post_init__` ordering trap
with frozen dataclasses.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Final

# Allowed character set for all opaque IDs. Conservative to keep log/DB safe.
_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_\-:]{7,127}$"
)


def _validate(value: str, kind: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise ValueError(f"invalid {kind} identifier: {value!r}")
    return value


def new_automaton_id() -> str:
    """Generate a new Automaton identifier."""
    return f"atm_{uuid.uuid4().hex}"


def new_task_id() -> str:
    return f"tsk_{uuid.uuid4().hex}"


def new_action_id() -> str:
    return f"act_{uuid.uuid4().hex}"


def new_plan_id() -> str:
    return f"pln_{uuid.uuid4().hex}"


def new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex}"


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def new_receipt_id() -> str:
    return f"rcp_{uuid.uuid4().hex}"


class _Id(str):
    """A validated string ID. Hash and equality are inherited from str."""

    _KIND: str = "id"

    def __new__(cls, value: Any) -> "_Id":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError(f"invalid {cls._KIND} identifier: {value!r}")
        v = _validate(value, cls._KIND)
        return super().__new__(cls, v)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str.__repr__(self)})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):  # noqa: D401
        from pydantic_core import core_schema

        # Treat the ID as a plain string for serialization.
        return core_schema.no_info_plain_validator_function(
            cls,
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )


class AutomatonId(_Id):
    _KIND = "automaton"


class TaskId(_Id):
    _KIND = "task"


class ActionId(_Id):
    _KIND = "action"


class PlanId(_Id):
    _KIND = "plan"


class MemoryId(_Id):
    _KIND = "memory"


class EventId(_Id):
    _KIND = "event"
