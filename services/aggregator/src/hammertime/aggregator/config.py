"""Service settings: shard assignment, store and bus wiring.

Spec: section 20, section 47.1

ADR-0011 section 9 fixes the key table; ADR-0009 decision 2's lifecycle keys
are folded in because they are trivially parsed and including them means the
runtime epic does not have to reopen this module.

Only a *missing* key takes its default: an empty or whitespace-only value is a
value, and a malformed one for every key -- including the three free-text keys
that are otherwise passed through verbatim. Section 47.1 asks for an invalid
configuration to be rejected before any network connection is opened, so every
rejection names the offending variable.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CONFIG_PATH = "./config/detection.v1.json"
_DEFAULT_BUS_KIND = "kafka"
_DEFAULT_BUS_BROKERS = "localhost:19092"
_DEFAULT_STORE_KIND = "redis"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_SHARD_COUNT = 64
_DEFAULT_BIND = "0.0.0.0:8083"
_DEFAULT_MAINTENANCE_INTERVAL_S = 1.0
_DEFAULT_CONFIG_POLL_INTERVAL_S = 1.0
_DEFAULT_STARTUP_TIMEOUT_S = 60.0
_DEFAULT_SHUTDOWN_TIMEOUT_S = 8.0
_DEFAULT_MAX_TRACKED_IPS = 1_000_000
_DEFAULT_COMMIT_EVERY = 100

_ALLOWED_BUS_KINDS = frozenset({"kafka", "memory"})
_ALLOWED_STORE_KINDS = frozenset({"redis", "memory"})

#: The only value a port can never take (ADR-0011 assumptions); 0 is accepted
#: because docs/spec/integration-scenarios.md binds every service to
#: 127.0.0.1:0.
_MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class AggregatorSettings:
    """Aggregator settings, sourced from the `.env.example` aggregator keys."""

    #: HAMMERTIME_CONFIG_PATH: the detection configuration document.
    detection_config_path: Path
    #: HAMMERTIME_BUS_KIND: "kafka" | "memory".
    bus_kind: str
    #: HAMMERTIME_BUS_BROKERS: bootstrap servers, meaningful only when
    #: bus_kind == "kafka".
    bus_brokers: str
    #: HAMMERTIME_STORE_KIND: "redis" | "memory".
    store_kind: str
    #: HAMMERTIME_REDIS_URL: meaningful only when store_kind == "redis".
    redis_url: str
    #: HAMMERTIME_SHARD_COUNT: size of the shard ring (spec section 20).
    shard_count: int
    #: HAMMERTIME_SHARD_IDS: the shards this process owns; defaults to every
    #: id in [0, shard_count).
    shard_ids: tuple[int, ...]
    #: HAMMERTIME_AGGREGATOR_BIND, split on the last ":".
    host: str
    port: int
    #: HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S: how often the periodic
    #: loop calls run_maintenance() (ADR-0009 decision 3).
    maintenance_interval_s: float
    #: HAMMERTIME_CONFIG_POLL_INTERVAL_S: detection-config poll period (spec
    #: section 47.3).
    config_poll_interval_s: float
    #: HAMMERTIME_STARTUP_TIMEOUT_S / HAMMERTIME_SHUTDOWN_TIMEOUT_S: the
    #: ADR-0009 lifecycle bounds. Nothing in M3 reads them.
    startup_timeout_s: float
    shutdown_timeout_s: float
    #: HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS: the window store's hard cap
    #: (spec section 26, ADR-0011 decision 3).
    max_tracked_ips: int
    #: HAMMERTIME_AGGREGATOR_COMMIT_EVERY: messages between consumer commits.
    commit_every: int


def _require_text(name: str, value: str) -> str:
    """Free-text keys are passed through verbatim, but never empty."""
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


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


def _parse_bind(name: str, value: str) -> tuple[str, int]:
    """`host:port`, split on the LAST ":" so an IPv6 literal stays together.

    The port is a decimal integer in [0, 65535]: a sign, surrounding space or
    a radix prefix is not one, even where `int()` would accept it. The host is
    not validated further, so "[::1]" passes through as-is.
    """
    host, sep, port_text = value.rpartition(":")
    if not sep or not host or not (port_text.isascii() and port_text.isdigit()):
        raise ValueError(f"{name} must be 'host:port', got {value!r}")
    port = int(port_text)
    if port > _MAX_PORT:
        raise ValueError(f"{name} port must be within [0, {_MAX_PORT}], got {port}")
    return host, port


def _parse_shard_id(name: str, text: str) -> int:
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"{name} entries must be 'a' or 'a-b', got {text!r}")
    return int(text)


def _parse_shard_ids(name: str, value: str, *, shard_count: int) -> tuple[int, ...]:
    """Comma-separated ids and inclusive `a-b` ranges, sorted and deduplicated."""
    ids: set[int] = set()
    for raw in value.split(","):
        part = raw.strip()
        if not part:
            raise ValueError(f"{name} must not contain an empty entry, got {value!r}")
        low_text, sep, high_text = part.partition("-")
        low = _parse_shard_id(name, low_text)
        high = _parse_shard_id(name, high_text) if sep else low
        if high < low:
            raise ValueError(f"{name} range {part!r} is reversed")
        ids.update(range(low, high + 1))
    if not ids:
        raise ValueError(f"{name} must name at least one shard")
    out_of_range = sorted(shard_id for shard_id in ids if shard_id >= shard_count)
    if out_of_range:
        raise ValueError(
            f"{name} must be within [0, {shard_count}) for "
            f"HAMMERTIME_SHARD_COUNT={shard_count}, got {out_of_range}"
        )
    return tuple(sorted(ids))


def load_settings(env: Mapping[str, str] | None = None) -> AggregatorSettings:
    """Load aggregator settings from the environment (see `.env.example`)."""
    source = env if env is not None else os.environ
    shard_count = _parse_positive_int(
        "HAMMERTIME_SHARD_COUNT",
        source.get("HAMMERTIME_SHARD_COUNT", str(_DEFAULT_SHARD_COUNT)),
    )
    shard_ids_text = source.get("HAMMERTIME_SHARD_IDS")
    if shard_ids_text is None:
        # Derived from the *parsed* shard_count, never the literal "0-63"
        # `.env.example` prints: a literal default would fall outside its own
        # valid range the moment HAMMERTIME_SHARD_COUNT is lowered.
        shard_ids = tuple(range(shard_count))
    else:
        shard_ids = _parse_shard_ids(
            "HAMMERTIME_SHARD_IDS", shard_ids_text, shard_count=shard_count
        )
    host, port = _parse_bind(
        "HAMMERTIME_AGGREGATOR_BIND",
        source.get("HAMMERTIME_AGGREGATOR_BIND", _DEFAULT_BIND),
    )
    return AggregatorSettings(
        detection_config_path=Path(
            _require_text(
                "HAMMERTIME_CONFIG_PATH",
                source.get("HAMMERTIME_CONFIG_PATH", _DEFAULT_CONFIG_PATH),
            )
        ),
        bus_kind=_parse_choice(
            "HAMMERTIME_BUS_KIND",
            source.get("HAMMERTIME_BUS_KIND", _DEFAULT_BUS_KIND),
            allowed=_ALLOWED_BUS_KINDS,
        ),
        bus_brokers=_require_text(
            "HAMMERTIME_BUS_BROKERS",
            source.get("HAMMERTIME_BUS_BROKERS", _DEFAULT_BUS_BROKERS),
        ),
        store_kind=_parse_choice(
            "HAMMERTIME_STORE_KIND",
            source.get("HAMMERTIME_STORE_KIND", _DEFAULT_STORE_KIND),
            allowed=_ALLOWED_STORE_KINDS,
        ),
        redis_url=_require_text(
            "HAMMERTIME_REDIS_URL",
            source.get("HAMMERTIME_REDIS_URL", _DEFAULT_REDIS_URL),
        ),
        shard_count=shard_count,
        shard_ids=shard_ids,
        host=host,
        port=port,
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
        startup_timeout_s=_parse_positive_number(
            "HAMMERTIME_STARTUP_TIMEOUT_S",
            source.get("HAMMERTIME_STARTUP_TIMEOUT_S", str(_DEFAULT_STARTUP_TIMEOUT_S)),
        ),
        shutdown_timeout_s=_parse_positive_number(
            "HAMMERTIME_SHUTDOWN_TIMEOUT_S",
            source.get("HAMMERTIME_SHUTDOWN_TIMEOUT_S", str(_DEFAULT_SHUTDOWN_TIMEOUT_S)),
        ),
        max_tracked_ips=_parse_positive_int(
            "HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS",
            source.get("HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS", str(_DEFAULT_MAX_TRACKED_IPS)),
        ),
        commit_every=_parse_positive_int(
            "HAMMERTIME_AGGREGATOR_COMMIT_EVERY",
            source.get("HAMMERTIME_AGGREGATOR_COMMIT_EVERY", str(_DEFAULT_COMMIT_EVERY)),
        ),
    )
