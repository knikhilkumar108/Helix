"""
Configuration loader. Strict-by-default: any unknown key raises. Environment
variables override file values. Secrets must come from a secrets backend
(see `core.security.vault`) — they are never accepted from env files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

import yaml

T = TypeVar("T")


class ConfigError(ValueError):
    pass


def _coerce(value: Any, target: Any) -> Any:
    if value is None:
        return None
    origin = getattr(target, "__origin__", None)
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"expected list, got {type(value).__name__}")
        elem_t = target.__args__[0]
        return [_coerce(v, elem_t) for v in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"expected dict, got {type(value).__name__}")
        return {str(k): _coerce(v, target.__args__[1]) for k, v in value.items()}
    if target in (int, float, str, bool):
        if not isinstance(value, target):
            try:
                return target(value)
            except Exception as e:  # noqa: BLE001
                raise ConfigError(f"cannot coerce {value!r} to {target}: {e}") from e
        return value
    if isinstance(target, type):
        # nested dataclass
        if not isinstance(value, dict):
            raise ConfigError(
                f"expected dict for {target.__name__}, got {type(value).__name__}"
            )
        return _from_dict(value, target)
    return value


def _from_dict(data: dict[str, Any], cls: type[T]) -> T:
    known = {f.name for f in fields(cls)}
    extras = set(data) - known
    if extras:
        raise ConfigError(f"unknown config keys for {cls.__name__}: {sorted(extras)}")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _coerce(data[f.name], f.type)
        else:
            if f.default is not f.default_factory:  # type: ignore[misc]
                kwargs[f.name] = f.default_factory()  # type: ignore[misc]
            else:
                kwargs[f.name] = f.default
    return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    dsn: str = "postgresql://automata:automata@localhost:5432/automata"
    pool_min: int = 2
    pool_max: int = 20
    statement_timeout_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class RedisConfig:
    url: str = "redis://localhost:6379/0"
    pool_max: int = 20


@dataclass(frozen=True, slots=True)
class ObjectStoreConfig:
    backend: str = "s3"  # s3 | minio | gcs | az
    bucket: str = "automata"
    region: str = "us-east-1"
    endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class BusConfig:
    backend: str = "inproc"  # inproc | nats | kafka
    nats_url: str | None = None
    kafka_brokers: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    service_name: str = "automata"
    log_level: str = "INFO"
    otlp_endpoint: str | None = None
    metrics_path: str = "/metrics"


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    jwt_secret_env: str = "AUTOMATA_JWT_SECRET"
    jwt_audience: str = "automata-platform"
    jwt_issuer: str = "automata-control-plane"
    jwt_ttl_seconds: int = 3600
    vault_backend: str = "env"  # env | hashicorp | aws | gcp
    sandbox_default: str = "process"  # process | container | microvm


@dataclass(frozen=True, slots=True)
class AppConfig:
    env: str = "dev"
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    object_store: ObjectStoreConfig = field(default_factory=ObjectStoreConfig)
    bus: BusConfig = field(default_factory=BusConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    file_data: dict[str, Any] = {}
    if path is None:
        path = os.environ.get("AUTOMATA_CONFIG", "config.yaml")
    p = Path(path)
    if p.exists():
        file_data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(file_data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(file_data)}")

    # Environment overrides for known scalar fields.
    scalar_overrides: dict[str, str] = {
        "AUTOMATA_ENV": "env",
        "AUTOMATA_LOG_LEVEL": "observability.log_level",
        "AUTOMATA_OTLP": "observability.otlp_endpoint",
        "AUTOMATA_NATS_URL": "bus.nats_url",
        "AUTOMATA_PG_DSN": "postgres.dsn",
        "AUTOMATA_REDIS_URL": "redis.url",
        "AUTOMATA_OBJ_BUCKET": "object_store.bucket",
    }
    for env_key, dotted in scalar_overrides.items():
        v = os.environ.get(env_key)
        if v is None:
            continue
        cur = file_data
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
            if not isinstance(cur, dict):
                raise ConfigError(f"config path {dotted} is not a mapping")
        cur[parts[-1]] = v

    return _from_dict(file_data, AppConfig)
