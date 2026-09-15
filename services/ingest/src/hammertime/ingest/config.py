"""Service settings: bind address, limits, and auth/rate-limit/bus/store wiring.

Spec: section 4, section 36

Everything `app.py`'s lifespan needs to construct the `AgentRegistry`,
`RateLimiter`, `DedupStore`, and bus `Producer` (issue #32) is read here.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_BIND = "0.0.0.0:8080"
_DEFAULT_MAX_BODY_BYTES = 1_048_576
_DEFAULT_MAX_OBSERVATIONS = 10_000
_DEFAULT_CONFIG_PATH = "./config/detection.v1.json"
_DEFAULT_RATE_LIMIT_RPS = 50.0
_DEFAULT_AGENTS_PATH = "./config/agents.v1.json"
_DEFAULT_BUS_KIND = "kafka"
_DEFAULT_BUS_BROKERS = "localhost:19092"
_DEFAULT_STORE_KIND = "redis"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"

_ALLOWED_BUS_KINDS = frozenset({"kafka", "memory"})
_ALLOWED_STORE_KINDS = frozenset({"redis", "memory"})


@dataclass(frozen=True, slots=True)
class IngestSettings:
    """Ingest service settings, sourced from the `.env.example` ingest keys."""

    #: Interface to bind the HTTP server to (from HAMMERTIME_INGEST_BIND).
    host: str
    port: int
    #: HAMMERTIME_INGEST_MAX_BODY_BYTES: request body size limit (413 above it).
    max_body_bytes: int
    #: HAMMERTIME_INGEST_MAX_OBSERVATIONS: observations[] length limit (413 above it).
    max_observations: int
    #: HAMMERTIME_CONFIG_PATH: detection config, read for its bucket_seconds
    #: (window_start alignment -- docs/protocol/observation-v1.md) and
    #: allowed_lateness_seconds (dedup TTL, ADR-0003).
    detection_config_path: Path
    #: HAMMERTIME_INGEST_RATE_LIMIT_RPS: global default per-agent rate limit,
    #: used when an authenticated agent's own `AgentRecord.rate_limit_rps`
    #: is unset.
    rate_limit_rps: float
    #: HAMMERTIME_INGEST_AGENTS_PATH: the agent registry document.
    agents_path: Path
    #: HAMMERTIME_BUS_KIND: "kafka" | "memory".
    bus_kind: str
    #: HAMMERTIME_BUS_BROKERS: bootstrap servers, meaningful only when
    #: bus_kind == "kafka".
    bus_brokers: str
    #: HAMMERTIME_STORE_KIND: "redis" | "memory".
    store_kind: str
    #: HAMMERTIME_REDIS_URL: meaningful only when store_kind == "redis".
    redis_url: str


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port_text = bind.rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise ValueError(f"HAMMERTIME_INGEST_BIND must be 'host:port', got {bind!r}")
    return host, int(port_text)


def _parse_positive_int(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer, got {parsed}")
    return parsed


def _parse_positive_number(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number, got {parsed}")
    return parsed


def _parse_choice(name: str, value: str, *, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> IngestSettings:
    """Load ingest settings from the environment (see `.env.example`)."""
    source = env if env is not None else os.environ
    host, port = _parse_bind(source.get("HAMMERTIME_INGEST_BIND", _DEFAULT_BIND))
    return IngestSettings(
        host=host,
        port=port,
        max_body_bytes=_parse_positive_int(
            "HAMMERTIME_INGEST_MAX_BODY_BYTES",
            source.get("HAMMERTIME_INGEST_MAX_BODY_BYTES", str(_DEFAULT_MAX_BODY_BYTES)),
        ),
        max_observations=_parse_positive_int(
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS",
            source.get("HAMMERTIME_INGEST_MAX_OBSERVATIONS", str(_DEFAULT_MAX_OBSERVATIONS)),
        ),
        detection_config_path=Path(source.get("HAMMERTIME_CONFIG_PATH", _DEFAULT_CONFIG_PATH)),
        rate_limit_rps=_parse_positive_number(
            "HAMMERTIME_INGEST_RATE_LIMIT_RPS",
            source.get("HAMMERTIME_INGEST_RATE_LIMIT_RPS", str(_DEFAULT_RATE_LIMIT_RPS)),
        ),
        agents_path=Path(source.get("HAMMERTIME_INGEST_AGENTS_PATH", _DEFAULT_AGENTS_PATH)),
        bus_kind=_parse_choice(
            "HAMMERTIME_BUS_KIND",
            source.get("HAMMERTIME_BUS_KIND", _DEFAULT_BUS_KIND),
            allowed=_ALLOWED_BUS_KINDS,
        ),
        bus_brokers=source.get("HAMMERTIME_BUS_BROKERS", _DEFAULT_BUS_BROKERS),
        store_kind=_parse_choice(
            "HAMMERTIME_STORE_KIND",
            source.get("HAMMERTIME_STORE_KIND", _DEFAULT_STORE_KIND),
            allowed=_ALLOWED_STORE_KINDS,
        ),
        redis_url=source.get("HAMMERTIME_REDIS_URL", _DEFAULT_REDIS_URL),
    )
