"""
Secrets backend abstraction. The platform never embeds secrets in source
or env files. Production deployments should use HashiCorp Vault, AWS Secrets
Manager, or GCP Secret Manager; the `env` backend is for local dev only.
"""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock


class SecretNotFoundError(KeyError):
    pass


class SecretBackend(ABC):
    @abstractmethod
    def get(self, name: str) -> bytes: ...

    @abstractmethod
    def put(self, name: str, value: bytes, *, ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    def rotate(self, name: str) -> bytes: ...


class EnvBackend(SecretBackend):
    """Reads from process env. Only for local development."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._overrides: dict[str, tuple[bytes, float | None]] = {}

    def get(self, name: str) -> bytes:
        with self._lock:
            override = self._overrides.get(name)
            if override is not None:
                value, expires_at = override
                if expires_at is not None and time.time() > expires_at:
                    del self._overrides[name]
                else:
                    return value
        v = os.environ.get(name)
        if v is None:
            raise SecretNotFoundError(name)
        return v.encode("utf-8")

    def put(self, name: str, value: bytes, *, ttl_seconds: int | None = None) -> None:
        with self._lock:
            expires_at = time.time() + ttl_seconds if ttl_seconds else None
            self._overrides[name] = (value, expires_at)

    def rotate(self, name: str) -> bytes:
        # In real life, this would call KMS to generate fresh material.
        new = os.urandom(32)
        self.put(name, new, ttl_seconds=3600)
        return new


class InMemoryBackend(SecretBackend):
    """For tests. Never use in production."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._lock = RLock()

    def get(self, name: str) -> bytes:
        with self._lock:
            if name not in self._store:
                raise SecretNotFoundError(name)
            return self._store[name]

    def put(self, name: str, value: bytes, *, ttl_seconds: int | None = None) -> None:
        with self._lock:
            self._store[name] = value

    def rotate(self, name: str) -> bytes:
        new = os.urandom(32)
        self.put(name, new)
        return new


@dataclass(slots=True)
class CachedSecret:
    backend: SecretBackend
    name: str
    value: bytes
    fetched_at: float
    ttl_seconds: int

    def is_stale(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.fetched_at > self.ttl_seconds


class SecretManager:
    def __init__(self, backend: SecretBackend, *, default_ttl: int = 300) -> None:
        self.backend = backend
        self.default_ttl = default_ttl
        self._cache: dict[str, CachedSecret] = {}
        self._lock = RLock()

    def get(self, name: str, *, ttl: int | None = None) -> bytes:
        with self._lock:
            cached = self._cache.get(name)
            if cached and not cached.is_stale():
                return cached.value
            value = self.backend.get(name)
            self._cache[name] = CachedSecret(
                backend=self.backend,
                name=name,
                value=value,
                fetched_at=time.time(),
                ttl_seconds=ttl or self.default_ttl,
            )
            return value

    def put(self, name: str, value: bytes, *, ttl: int | None = None) -> None:
        with self._lock:
            self.backend.put(name, value, ttl_seconds=ttl)
            self._cache.pop(name, None)

    def rotate(self, name: str) -> bytes:
        with self._lock:
            new = self.backend.rotate(name)
            self._cache.pop(name, None)
            return new

    @contextmanager
    def ephemeral(self, name: str, value: bytes) -> Iterator[None]:
        prev: bytes | None
        try:
            prev = self.backend.get(name)
        except SecretNotFoundError:
            prev = None
        self.backend.put(name, value)
        with self._lock:
            self._cache.pop(name, None)
        try:
            yield
        finally:
            if prev is None:
                with self._lock:
                    self._cache.pop(name, None)
            else:
                self.backend.put(name, prev)
                with self._lock:
                    self._cache.pop(name, None)
