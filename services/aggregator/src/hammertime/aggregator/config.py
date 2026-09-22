"""Service settings: shard assignment, member identity, store and bus wiring.

Spec: section 20, section 47

ADR-0011 decision 9 fixes the key set; ADR-0009 decision 2 fixes the shape
(`load_settings(env)` raising `ValueError`, so `run_service` reports one
`config_invalid` record and exits 2 before any bus, store or socket is
opened). ADR-0013 decisions 6, 7 and 10 rework the bus and sharding keys.

A shard is a partition of `hammertime.observations.v1` (ADR-0011 decision
1), and assignment is static (ADR-0013 decision 6): `HAMMERTIME_SHARD_IDS`
is **required**, `all` or an explicit partition set within `0..127`. There
is no safe default -- `all` on every replica of a scaled deployment is the
two-owners misconfiguration of #90 -- so an unset variable is refused, and
so is the retired `auto` (a deployment carrying the old default fails
loudly rather than silently claiming nothing). A *set-but-empty* value is a
configuration error too, not a way of saying "nothing" (ADR-0011 Amendment
1, item A3).

Two keys identify a member for the per-shard lease of decision 7:
`HAMMERTIME_AGGREGATOR_MEMBER_ID` (default the hostname) and
`HAMMERTIME_AGGREGATOR_LEASE_TTL_S` (default 30 s), which must exceed the
maintenance interval because that is how often the lease is renewed.
"""

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hammertime.bus.nats import split_bus_servers, validate_bus_url
from hammertime.bus.topics import OBSERVATIONS
from hammertime.store import validate_redis_url

_DEFAULT_CONFIG_PATH = "./config/detection.v1.json"
_DEFAULT_BUS_KIND = "nats"
_DEFAULT_BUS_BROKERS = "nats://localhost:4222"
_DEFAULT_STORE_KIND = "redis"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# --- ADR-0009: process lifecycle (spec section 47) -------------------------
_DEFAULT_BIND = "0.0.0.0:8083"
_DEFAULT_MAINTENANCE_INTERVAL_S = 1.0
_DEFAULT_CONFIG_POLL_INTERVAL_S = 1.0

# --- ADR-0011 / ADR-0013: shards, commit cadence and store bounds ----------
#: The value that expands to every partition of the observations topic.
_SHARD_IDS_ALL = "all"
#: The retired group-managed mode (ADR-0011); refused with a pointer since
#: ADR-0013 decision 6.
_SHARD_IDS_AUTO = "auto"
_DEFAULT_COMMIT_INTERVAL_S = 1.0
_DEFAULT_MAX_TRACKED_IPS = 1_000_000
_DEFAULT_REEVALUATION_BATCH = 1000
_DEFAULT_LEASE_TTL_S = 30.0

_BUS_BROKERS_KEY = "HAMMERTIME_BUS_BROKERS"
_SHARD_IDS_KEY = "HAMMERTIME_SHARD_IDS"
_MEMBER_ID_KEY = "HAMMERTIME_AGGREGATOR_MEMBER_ID"
_LEASE_TTL_KEY = "HAMMERTIME_AGGREGATOR_LEASE_TTL_S"
_MAINTENANCE_INTERVAL_KEY = "HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S"

_ALLOWED_BUS_KINDS = frozenset({"nats", "memory"})
_ALLOWED_STORE_KINDS = frozenset({"redis", "memory"})


@dataclass(frozen=True, slots=True)
class AggregatorSettings:
    """Aggregator service settings, sourced from the `.env.example` keys."""

    #: Interface the readiness/metrics server binds to (HAMMERTIME_AGGREGATOR_BIND).
    host: str
    port: int
    #: HAMMERTIME_CONFIG_PATH: the detection config document (section 34).
    detection_config_path: Path
    #: HAMMERTIME_BUS_KIND: "nats" | "memory".
    bus_kind: str
    #: HAMMERTIME_BUS_BROKERS: comma-separated NATS server URLs, meaningful
    #: only when bus_kind == "nats". Every entry is checked by
    #: `load_settings` with `hammertime.bus.nats.validate_bus_url`, and that
    #: check runs when bus_kind == "nats" and not otherwise (ADR-0013
    #: Amendment 4 ruling S2, gated by Amendment 5 ruling 5) -- the
    #: dataclass itself does not validate at all. An
    #: entry may carry userinfo, so no log record carries the value itself
    #: -- `bus_endpoints()` reduces it (Amendment 2).
    bus_brokers: str
    #: HAMMERTIME_STORE_KIND: "redis" | "memory".
    store_kind: str
    #: HAMMERTIME_REDIS_URL: meaningful only when store_kind == "redis", and
    #: validated by `load_settings` in that case (ADR-0009 A12) -- the
    #: dataclass itself does not validate, so a directly built object is a
    #: plain carrier.
    redis_url: str
    #: HAMMERTIME_SHARD_IDS: the partitions this member owns statically
    #: (ADR-0013 decision 6). Never empty, never `None`.
    shard_ids: frozenset[int]
    #: HAMMERTIME_AGGREGATOR_MEMBER_ID: the owner id the shard leases are
    #: taken under (ADR-0013 decision 7).
    member_id: str
    #: HAMMERTIME_AGGREGATOR_LEASE_TTL_S: how long a lease outlives its last
    #: renewal (ADR-0013 decision 7).
    lease_ttl_s: float = _DEFAULT_LEASE_TTL_S
    #: HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S: expiry/retention sweep
    #: period, and the lease renewal cadence (ADR-0009, ADR-0011 decision 6,
    #: ADR-0013 decision 7).
    maintenance_interval_s: float = _DEFAULT_MAINTENANCE_INTERVAL_S
    #: HAMMERTIME_CONFIG_POLL_INTERVAL_S: how often the detection config
    #: document is re-read (ADR-0009 decision 6, spec section 47.3).
    config_poll_interval_s: float = _DEFAULT_CONFIG_POLL_INTERVAL_S
    #: HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S: acknowledgement cadence
    #: (ADR-0011 decision 6; ADR-0013 decision 8).
    commit_interval_s: float = _DEFAULT_COMMIT_INTERVAL_S
    #: HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS: per-shard cap on tracked IPs;
    #: over it, the least recently seen COLD IP is evicted (decision 2).
    max_tracked_ips: int = _DEFAULT_MAX_TRACKED_IPS
    #: HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH: how many IPs one
    #: re-evaluation slice covers after a config change (decision 7).
    reevaluation_batch: int = _DEFAULT_REEVALUATION_BATCH


def parse_shard_ids(text: str) -> frozenset[int]:
    """Parse `HAMMERTIME_SHARD_IDS`: `all` or an explicit partition set.

    `all` (case-insensitive) is `frozenset(range(OBSERVATIONS.partitions))`,
    expanded here rather than passed to the bus as `partitions=None`, so the
    aggregator always subscribes with an explicit set and gets one durable
    per shard (ADR-0013 decisions 5 and 6).

    The explicit grammar is comma-separated `n` or `lo-hi` (inclusive, `lo
    <= hi`): `0`, `0-3`, `0,2,5-7`. Duplicates and overlapping ranges
    collapse into the set rather than being errors. Every id must be below
    `OBSERVATIONS.partitions`; one at or above it is a `ValueError` naming
    the variable and the count.

    `auto` -- ADR-0011's group-managed mode -- is a `ValueError` pointing at
    the two accepted forms, so a deployment carrying the old default fails
    loudly (`config_invalid`, exit 2) rather than silently claiming nothing.
    A set-but-empty (or whitespace-only) value is a `ValueError` too: a
    static member that owns nothing would sit idle for its whole life while
    reporting itself healthy (ADR-0011 Amendment 1, item A3).
    """
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered == _SHARD_IDS_ALL:
        return frozenset(range(OBSERVATIONS.partitions))
    if lowered == _SHARD_IDS_AUTO:
        raise ValueError(
            f"{_SHARD_IDS_KEY}={text!r} is not accepted: shard assignment is static since "
            f"ADR-0013; set it to 'all' or an explicit partition set "
            f"(e.g. '0', '0-3', '0,2,5-7')"
        )
    if not stripped:
        raise ValueError(
            f"{_SHARD_IDS_KEY} must be 'all' or a non-empty partition set "
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
    """Load aggregator settings from the environment (see `.env.example`).

    Error precedence between keys is first-in-load-order and deliberately
    unpinned (ADR-0009 A12; ADR-0013 Amendment 1 ruling T7).
    """
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
    shard_ids_text = source.get(_SHARD_IDS_KEY)
    if shard_ids_text is None:
        # ADR-0013 decision 6: there is no safe default under static
        # assignment, so the operator states the set.
        raise ValueError(
            f"{_SHARD_IDS_KEY} is required: set it to 'all' or an explicit partition set "
            f"(e.g. '0', '0-3', '0,2,5-7')"
        )
    maintenance_interval_s = _parse_positive_number(
        _MAINTENANCE_INTERVAL_KEY,
        source.get(_MAINTENANCE_INTERVAL_KEY, str(_DEFAULT_MAINTENANCE_INTERVAL_S)),
    )
    lease_ttl_s = _parse_positive_number(
        _LEASE_TTL_KEY, source.get(_LEASE_TTL_KEY, str(_DEFAULT_LEASE_TTL_S))
    )
    if lease_ttl_s <= maintenance_interval_s:
        # ADR-0013 decision 7: renewal happens once per maintenance
        # interval, so a TTL that does not outlast one lapses between
        # renewals and every member loses its shards on schedule.
        raise ValueError(
            f"{_LEASE_TTL_KEY} must exceed {_MAINTENANCE_INTERVAL_KEY} "
            f"({maintenance_interval_s}), got {lease_ttl_s}"
        )
    bus_kind = _parse_choice(
        "HAMMERTIME_BUS_KIND",
        source.get("HAMMERTIME_BUS_KIND", _DEFAULT_BUS_KIND),
        allowed=_ALLOWED_BUS_KINDS,
    )
    bus_brokers = source.get(_BUS_BROKERS_KEY, _DEFAULT_BUS_BROKERS)
    if bus_kind == "nats":
        # ADR-0013 Amendment 4 ruling S2 as gated by Amendment 5 ruling 5:
        # an entry nats-py's own parse would refuse is invalid
        # configuration, so it must fail here (config_invalid, exit 2)
        # rather than inside connect(). Under memory the value is never
        # interpreted, so a set-but-malformed one is ignored, not rejected
        # (ADR-0009 A12).
        _validate_bus_brokers(bus_brokers)
    return AggregatorSettings(
        host=host,
        port=port,
        detection_config_path=Path(source.get("HAMMERTIME_CONFIG_PATH", _DEFAULT_CONFIG_PATH)),
        bus_kind=bus_kind,
        bus_brokers=bus_brokers,
        store_kind=store_kind,
        redis_url=redis_url,
        # A set-but-empty value reaches the parser, which rejects it (item A3).
        shard_ids=parse_shard_ids(shard_ids_text),
        member_id=_parse_member_id(source.get(_MEMBER_ID_KEY)),
        lease_ttl_s=lease_ttl_s,
        maintenance_interval_s=maintenance_interval_s,
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


def _validate_bus_brokers(value: str) -> None:
    """Refuse an entry nats-py's own parse would refuse, naming its position only.

    ADR-0013 decision 3 as amended by Amendment 4 ruling S2: the value is
    split as `NatsBus.__init__` splits it (on `,`, stripped, empties
    dropped) and every entry goes through `validate_bus_url`, whose message
    carries none of the entry's text -- an entry may hold a password, and a
    `config_invalid` record must not echo it. So the process exits 2 here
    rather than reaching nats-py, whose parse failure chains a `ValueError`
    that repeats the token it could not cast (assumption 46, closed).
    """
    for index, entry in enumerate(split_bus_servers(value)):
        try:
            validate_bus_url(entry)
        except ValueError as exc:
            raise ValueError(f"{_BUS_BROKERS_KEY}: entry {index} is {exc}") from None


def _parse_member_id(value: str | None) -> str:
    """`HAMMERTIME_AGGREGATOR_MEMBER_ID`: the hostname when unset; never empty.

    ADR-0013 decision 7: two members sharing an id share its leases and are
    not told apart, and "" is the templating accident ADR-0011 A3 describes,
    so a set-but-empty (or whitespace-only) value is refused. The value is
    otherwise stored as given.
    """
    if value is None:
        return socket.gethostname()
    if not value.strip():
        raise ValueError(f"{_MEMBER_ID_KEY} must not be empty; unset it to use the hostname")
    return value


def _parse_shard_id(text: str, raw: str) -> int:
    token = text.strip()
    if not token.isdigit():
        raise ValueError(
            f"{_SHARD_IDS_KEY} must be 'all' or a set of partition indices "
            f"(e.g. '0', '0-3', '0,2,5-7'), got {raw!r}"
        )
    shard = int(token)
    if shard >= OBSERVATIONS.partitions:
        raise ValueError(
            f"{_SHARD_IDS_KEY} names partition {shard}, but {OBSERVATIONS.name!r} has "
            f"{OBSERVATIONS.partitions} partitions (0..{OBSERVATIONS.partitions - 1}); "
            f"got {raw!r}"
        )
    return shard


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
