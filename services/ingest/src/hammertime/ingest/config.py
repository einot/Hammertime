"""Service settings: bind address, limits, and auth/rate-limit/bus/store wiring.

Spec: section 4, section 36, section 47

Everything `app.py`'s lifespan needs to construct the `AgentRegistry`,
`RateLimiter`, `DedupStore`, and bus `Producer` (issue #32) is read here.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hammertime.store import validate_redis_url

_DEFAULT_BIND = "0.0.0.0:8080"
_DEFAULT_MAX_BODY_BYTES = 1_048_576
_DEFAULT_MAX_OBSERVATIONS = 10_000
_DEFAULT_CONFIG_PATH = "./config/detection.v1.json"
_DEFAULT_RATE_LIMIT_RPS = 50.0
_DEFAULT_AGENTS_PATH = "./config/agents.v2.json"
_DEFAULT_BUS_KIND = "kafka"
_DEFAULT_BUS_BROKERS = "localhost:19092"
_DEFAULT_STORE_KIND = "redis"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# --- ADR-0007: failed-authentication throttling (spec section 36.5) --------
_DEFAULT_AUTH_FAILURE_RATE_PER_MIN = 30.0
_DEFAULT_AUTH_FAILURE_BURST = 10
_DEFAULT_AUTH_FAILURE_AGENT_RATE_PER_MIN = 60.0
_DEFAULT_AUTH_FAILURE_AGENT_BURST = 20
_DEFAULT_TRUSTED_PROXY_HOPS = 0

# --- ADR-0008: observation-scaled rate limiting (spec section 36.6) --------
_DEFAULT_OBSERVATION_RATE_LIMIT_EPS = 2000.0
_DEFAULT_OBSERVATION_BURST = 10_000

# --- ADR-0009: process lifecycle (spec section 47) -------------------------
_DEFAULT_CONFIG_POLL_INTERVAL_S = 1.0

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
    #: HAMMERTIME_REDIS_URL: meaningful only when store_kind == "redis", and
    #: validated by `load_settings` in that case (ADR-0009 A12) -- the
    #: dataclass itself does not validate, so a directly built object is a
    #: plain carrier.
    redis_url: str
    #: HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN: failed-auth source-bucket
    #: refill rate, failures/min (ADR-0007, spec section 36.5).
    auth_failure_rate_per_min: float = _DEFAULT_AUTH_FAILURE_RATE_PER_MIN
    #: HAMMERTIME_INGEST_AUTH_FAILURE_BURST: failed-auth source-bucket capacity.
    auth_failure_burst: int = _DEFAULT_AUTH_FAILURE_BURST
    #: HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN: failed-auth
    #: agent-bucket refill rate, failures/min, keyed by the attempted
    #: `X-Agent-Id`.
    auth_failure_agent_rate_per_min: float = _DEFAULT_AUTH_FAILURE_AGENT_RATE_PER_MIN
    #: HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST: failed-auth agent-bucket capacity.
    auth_failure_agent_burst: int = _DEFAULT_AUTH_FAILURE_AGENT_BURST
    #: HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS: 0 = trust no X-Forwarded-For;
    #: N > 0 = take the Nth-from-the-right X-Forwarded-For entry.
    trusted_proxy_hops: int = _DEFAULT_TRUSTED_PROXY_HOPS
    #: HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS: per-agent observation
    #: budget refill rate, entries/second (ADR-0008, spec section 36.6).
    observation_rate_limit_eps: float = _DEFAULT_OBSERVATION_RATE_LIMIT_EPS
    #: HAMMERTIME_INGEST_OBSERVATION_BURST: per-agent observation budget
    #: capacity, entries. MUST be >= max_observations (enforced by
    #: `load_settings`).
    observation_burst: int = _DEFAULT_OBSERVATION_BURST
    #: HAMMERTIME_CONFIG_POLL_INTERVAL_S: how often the detection config
    #: document is re-read (ADR-0009 decision 6, spec section 47.3). A
    #: settings field rather than a direct `os.environ` read so that an
    #: in-process test driving the service off an assembled environment
    #: controls it the same way it controls every other knob.
    config_poll_interval_s: float = _DEFAULT_CONFIG_POLL_INTERVAL_S


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


def _parse_nonnegative_int(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {parsed}")
    return parsed


def load_settings(env: Mapping[str, str] | None = None) -> IngestSettings:
    """Load ingest settings from the environment (see `.env.example`)."""
    source = env if env is not None else os.environ
    host, port = _parse_bind(source.get("HAMMERTIME_INGEST_BIND", _DEFAULT_BIND))
    max_observations = _parse_positive_int(
        "HAMMERTIME_INGEST_MAX_OBSERVATIONS",
        source.get("HAMMERTIME_INGEST_MAX_OBSERVATIONS", str(_DEFAULT_MAX_OBSERVATIONS)),
    )
    observation_burst = _parse_positive_int(
        "HAMMERTIME_INGEST_OBSERVATION_BURST",
        source.get("HAMMERTIME_INGEST_OBSERVATION_BURST", str(_DEFAULT_OBSERVATION_BURST)),
    )
    if observation_burst < max_observations:
        # ADR-0008 / spec section 36.6: this is what guarantees every
        # schema-valid batch (at most max_observations distinct IPs) is
        # structurally satisfiable -- a cost above capacity must never be
        # reachable from configuration.
        raise ValueError(
            "HAMMERTIME_INGEST_OBSERVATION_BURST "
            f"({observation_burst}) must be >= HAMMERTIME_INGEST_MAX_OBSERVATIONS "
            f"({max_observations})"
        )
    store_kind = _parse_choice(
        "HAMMERTIME_STORE_KIND",
        source.get("HAMMERTIME_STORE_KIND", _DEFAULT_STORE_KIND),
        allowed=_ALLOWED_STORE_KINDS,
    )
    redis_url = source.get("HAMMERTIME_REDIS_URL", _DEFAULT_REDIS_URL)
    if store_kind == "redis":
        # ADR-0009 decision 2 / A12: a URL `Redis.from_url` would refuse is
        # invalid configuration, so it must fail here (config_invalid, exit
        # 2) rather than inside start(). Under "memory" the value is never
        # interpreted, so a set-but-malformed one is ignored, not rejected.
        validate_redis_url(redis_url)
    return IngestSettings(
        host=host,
        port=port,
        max_body_bytes=_parse_positive_int(
            "HAMMERTIME_INGEST_MAX_BODY_BYTES",
            source.get("HAMMERTIME_INGEST_MAX_BODY_BYTES", str(_DEFAULT_MAX_BODY_BYTES)),
        ),
        max_observations=max_observations,
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
        store_kind=store_kind,
        redis_url=redis_url,
        auth_failure_rate_per_min=_parse_positive_number(
            "HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN",
            source.get(
                "HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN",
                str(_DEFAULT_AUTH_FAILURE_RATE_PER_MIN),
            ),
        ),
        auth_failure_burst=_parse_positive_int(
            "HAMMERTIME_INGEST_AUTH_FAILURE_BURST",
            source.get("HAMMERTIME_INGEST_AUTH_FAILURE_BURST", str(_DEFAULT_AUTH_FAILURE_BURST)),
        ),
        auth_failure_agent_rate_per_min=_parse_positive_number(
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN",
            source.get(
                "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN",
                str(_DEFAULT_AUTH_FAILURE_AGENT_RATE_PER_MIN),
            ),
        ),
        auth_failure_agent_burst=_parse_positive_int(
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST",
            source.get(
                "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST",
                str(_DEFAULT_AUTH_FAILURE_AGENT_BURST),
            ),
        ),
        trusted_proxy_hops=_parse_nonnegative_int(
            "HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS",
            source.get("HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS", str(_DEFAULT_TRUSTED_PROXY_HOPS)),
        ),
        observation_rate_limit_eps=_parse_positive_number(
            "HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS",
            source.get(
                "HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS",
                str(_DEFAULT_OBSERVATION_RATE_LIMIT_EPS),
            ),
        ),
        observation_burst=observation_burst,
        config_poll_interval_s=_parse_positive_number(
            "HAMMERTIME_CONFIG_POLL_INTERVAL_S",
            source.get("HAMMERTIME_CONFIG_POLL_INTERVAL_S", str(_DEFAULT_CONFIG_POLL_INTERVAL_S)),
        ),
    )
