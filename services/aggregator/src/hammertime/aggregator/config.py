"""Service settings: shard assignment, store and bus wiring.

Spec: section 20, section 47

ADR-0011 decision 9 fixes the key set; ADR-0009 decision 2 fixes the shape
(`load_settings(env)` raising `ValueError`, so `run_service` reports one
`config_invalid` record and exits 2 before any bus, store or socket is
opened).

A shard is a partition of `hammertime.observations.v1` (ADR-0011 decision
1), so `HAMMERTIME_SHARD_IDS` is the only sharding setting: `auto` (the
default, group-managed assignment) or an explicit partition set. A
*set-but-empty* value is a configuration error, not a way of saying
"nothing" (ADR-0011 Amendment 1, item A3); only an unset variable means
`auto`.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hammertime.store import validate_redis_url

_DEFAULT_CONFIG_PATH = "./config/detection.v1.json"
_DEFAULT_BUS_KIND = "kafka"
_DEFAULT_BUS_BROKERS = "localhost:19092"
_DEFAULT_STORE_KIND = "redis"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# --- ADR-0009: process lifecycle (spec section 47) -------------------------
_DEFAULT_BIND = "0.0.0.0:8083"
_DEFAULT_MAINTENANCE_INTERVAL_S = 1.0
_DEFAULT_CONFIG_POLL_INTERVAL_S = 1.0

# --- ADR-0011: shards, commit cadence and store bounds ---------------------
#: The value that selects group-managed assignment, and the default when
#: HAMMERTIME_SHARD_IDS is unset.
_SHARD_IDS_AUTO = "auto"
_DEFAULT_COMMIT_INTERVAL_S = 1.0
_DEFAULT_MAX_TRACKED_IPS = 1_000_000
_DEFAULT_REEVALUATION_BATCH = 1000

_SHARD_IDS_KEY = "HAMMERTIME_SHARD_IDS"

_ALLOWED_BUS_KINDS = frozenset({"kafka", "memory"})
_ALLOWED_STORE_KINDS = frozenset({"redis", "memory"})


@dataclass(frozen=True, slots=True)
class AggregatorSettings:
    """Aggregator service settings, sourced from the `.env.example` keys."""

    #: Interface the readiness/metrics server binds to (HAMMERTIME_AGGREGATOR_BIND).
    host: str
    port: int
    #: HAMMERTIME_CONFIG_PATH: the detection config document (section 34).
    detection_config_path: Path
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
    #: HAMMERTIME_SHARD_IDS: None for `auto` (group-managed assignment), or
    #: the partitions this member owns statically (ADR-0011 decision 1).
    shard_ids: frozenset[int] | None
    #: HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S: expiry/retention sweep
    #: period (ADR-0009, ADR-0011 decision 6).
    maintenance_interval_s: float = _DEFAULT_MAINTENANCE_INTERVAL_S
    #: HAMMERTIME_CONFIG_POLL_INTERVAL_S: how often the detection config
    #: document is re-read (ADR-0009 decision 6, spec section 47.3).
    config_poll_interval_s: float = _DEFAULT_CONFIG_POLL_INTERVAL_S
    #: HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S: consumer offset commit
    #: cadence (ADR-0011 decision 6).
    commit_interval_s: float = _DEFAULT_COMMIT_INTERVAL_S
    #: HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS: per-shard cap on tracked IPs;
    #: over it, the least recently seen COLD IP is evicted (decision 2).
    max_tracked_ips: int = _DEFAULT_MAX_TRACKED_IPS
    #: HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH: how many IPs one
    #: re-evaluation slice covers after a config change (decision 7).
    reevaluation_batch: int = _DEFAULT_REEVALUATION_BATCH


def parse_shard_ids(text: str) -> frozenset[int] | None:
    """Parse `HAMMERTIME_SHARD_IDS`: None for `auto`, else the partition set.

    The grammar is comma-separated `n` or `lo-hi` (inclusive, `lo <= hi`):
    `0`, `0-3`, `0,2,5-7`. Duplicates and overlapping ranges collapse into the
    set rather than being errors.

    A set-but-empty (or whitespace-only) value is a `ValueError`: a static
    member that owns nothing would sit idle for its whole life while
    reporting itself healthy, which is the silent misconfiguration ADR-0009
    decision 2 exists to refuse (ADR-0011 Amendment 1, item A3). `auto` is
    the only way to say "let the group decide".
    """
    stripped = text.strip()
    if stripped.lower() == _SHARD_IDS_AUTO:
        return None
    if not stripped:
        raise ValueError(
            f"{_SHARD_IDS_KEY} must be 'auto' or a non-empty partition set "
            f"(e.g. '0', '0-3', '0,2,5-7'), got {text!r}"
        )
    shard_ids: set[int] = set()
    for element in stripped.split(","):
        token = element.strip()
        if not token:
            raise ValueError(f"{_SHARD_IDS_KEY} has an empty element in {text!r}")
        low_text, separator, high_text = token.partition("-")
        low = _parse_shard_id(low_text, text)
        if not separator:
            shard_ids.add(low)
            continue
        high = _parse_shard_id(high_text, text)
        if high < low:
            raise ValueError(
                f"{_SHARD_IDS_KEY} range {token!r} is descending; "
                f"ranges are inclusive and must have lo <= hi"
            )
        shard_ids.update(range(low, high + 1))
    return frozenset(shard_ids)


def load_settings(env: Mapping[str, str] | None = None) -> AggregatorSettings:
    """Load aggregator settings from the environment (see `.env.example`)."""
    source = env if env is not None else os.environ
    host, port = _parse_bind(source.get("HAMMERTIME_AGGREGATOR_BIND", _DEFAULT_BIND))
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
    return AggregatorSettings(
        host=host,
        port=port,
        detection_config_path=Path(source.get("HAMMERTIME_CONFIG_PATH", _DEFAULT_CONFIG_PATH)),
        bus_kind=_parse_choice(
            "HAMMERTIME_BUS_KIND",
            source.get("HAMMERTIME_BUS_KIND", _DEFAULT_BUS_KIND),
            allowed=_ALLOWED_BUS_KINDS,
        ),
        bus_brokers=source.get("HAMMERTIME_BUS_BROKERS", _DEFAULT_BUS_BROKERS),
        store_kind=store_kind,
        redis_url=redis_url,
        # `env.get(key, default)` hands a set-but-empty value to the parser,
        # which rejects it -- ingest's `load_settings` pattern (item A3).
        shard_ids=parse_shard_ids(source.get(_SHARD_IDS_KEY, _SHARD_IDS_AUTO)),
        maintenance_interval_s=_parse_positive_number(
            "HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S",
            source.get(
                "HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S",
                str(_DEFAULT_MAINTENANCE_INTERVAL_S),
            ),
        ),
        config_poll_interval_s=_parse_positive_number(
            "HAMMERTIME_CONFIG_POLL_INTERVAL_S",
            source.get("HAMMERTIME_CONFIG_POLL_INTERVAL_S", str(_DEFAULT_CONFIG_POLL_INTERVAL_S)),
        ),
        commit_interval_s=_parse_positive_number(
            "HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S",
            source.get("HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S", str(_DEFAULT_COMMIT_INTERVAL_S)),
        ),
        max_tracked_ips=_parse_positive_int(
            "HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS",
            source.get("HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS", str(_DEFAULT_MAX_TRACKED_IPS)),
        ),
        reevaluation_batch=_parse_positive_int(
            "HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH",
            source.get(
                "HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH", str(_DEFAULT_REEVALUATION_BATCH)
            ),
        ),
    )


def _parse_shard_id(text: str, raw: str) -> int:
    token = text.strip()
    if not token.isdigit():
        raise ValueError(
            f"{_SHARD_IDS_KEY} must be 'auto' or a set of partition indices "
            f"(e.g. '0', '0-3', '0,2,5-7'), got {raw!r}"
        )
    return int(token)


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port_text = bind.rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise ValueError(f"HAMMERTIME_AGGREGATOR_BIND must be 'host:port', got {bind!r}")
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
