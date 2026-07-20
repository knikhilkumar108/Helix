"""Unit tests for the secrets manager."""
from __future__ import annotations

import time

import pytest

from core.security.vault import EnvBackend, InMemoryBackend, SecretManager, SecretNotFoundError


def test_in_memory_get_put():
    m = SecretManager(InMemoryBackend())
    m.put("k", b"v")
    assert m.get("k") == b"v"


def test_in_memory_missing_raises():
    m = SecretManager(InMemoryBackend())
    with pytest.raises(SecretNotFoundError):
        m.get("missing")


def test_rotation_changes_value():
    m = SecretManager(InMemoryBackend())
    m.put("k", b"v1")
    new = m.rotate("k")
    assert new != b"v1"
    assert m.get("k") == new


def test_ephemeral_restores_value():
    b = InMemoryBackend()
    m = SecretManager(b, default_ttl=10)
    m.put("k", b"v1")
    with m.ephemeral("k", b"v2"):
        assert m.get("k") == b"v2"
    assert m.get("k") == b"v1"
