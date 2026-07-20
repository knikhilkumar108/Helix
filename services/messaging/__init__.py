"""Public surface for the messaging service (agent-to-agent inbox)."""
from .inbox import (
    InboxBackend,
    InboxFull,
    InboxMessage,
    InboxService,
    InboxState,
    make_inbox,
)

__all__ = [
    "InboxBackend",
    "InboxFull",
    "InboxMessage",
    "InboxService",
    "InboxState",
    "make_inbox",
]
