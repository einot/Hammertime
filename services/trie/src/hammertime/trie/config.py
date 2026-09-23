"""Service settings: query bind, bus wiring, detection config path, address families served.

Spec: section 33, section 35, section 47; ADR-0017 decision 11, ADR-0009
decision 2.

`load_settings(env)` follows ADR-0009 decision 2's pattern: `env=None` means
`os.environ`, a bad value is a `ValueError` naming the variable (so
`run_service` reports one `config_invalid` record and exits 2 before any bus
or socket is opened), and unknown variables are ignored. Which of several bad
values is reported is deliberately not pinned.

`HAMMERTIME_TRIE_FAMILIES` names the address families this process holds, one
trie and one attribute record map each (section 35's separate roots). The
default is `ipv4`, section 43's first item.

Not read in slice 1: `HAMMERTIME_STORE_KIND` and `HAMMERTIME_REDIS_URL` (the
trie has no store), `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` (slice 2), and
`HAMMERTIME_TRIE_SNAPSHOT_DIR` / `HAMMERTIME_TRIE_SNAPSHOT_INTERVAL_S` (the
snapshot epic adds them together with the code that reads them).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hammertime.bus.nats import split_bus_servers, validate_bus_url
from hammertime.core.addressing.address import AddressFamily

_DEFAULT_BIND = "0.0.0.0:8081"
_DEFAULT_CONFIG_PATH = "./config/detection.v1.json"
_DEFAULT_BUS_KIND = "nats"
_DEFAULT_BUS_BROKERS = "nats://localhost:4222"
_DEFAULT_FAMILIES = "ipv4"
_DEFAULT_CONFIG_POLL_INTERVAL_S = 1.0

_BIND_KEY = "HAMMERTIME_TRIE_QUERY_BIND"
_CONFIG_PATH_KEY = "HAMMERTIME_CONFIG_PATH"
_BUS_KIND_KEY = "HAMMERTIME_BUS_KIND"
_BUS_BROKERS_KEY = "HAMMERTIME_BUS_BROKERS"
_FAMILIES_KEY = "HAMMERTIME_TRIE_FAMILIES"
_CONFIG_POLL_INTERVAL_KEY = "HAMMERTIME_CONFIG_POLL_INTERVAL_S"

_ALLOWED_BUS_KINDS = frozenset({"nats", "memory"})
_FAMILY_NAMES: Mapping[str, AddressFamily] = {family.value: family for family in AddressFamily}


@dataclass(frozen=True, slots=True)
class TrieSettings:
    """Trie service settings, sourced from the `.env.example` keys."""

    #: HAMMERTIME_TRIE_QUERY_BIND: interface and port the HTTP server binds to.
    host: str
    port: int
    #: HAMMERTIME_CONFIG_PATH: the detection config document (section 34).
    detection_config_path: Path
    #: HAMMERTIME_BUS_KIND: "nats" | "memory".
    bus_kind: str
    #: HAMMERTIME_BUS_BROKERS: comma-separated NATS server URLs, validated by
    #: `load_settings` under `nats` only. An entry may carry userinfo, so no
    #: log record carries the value itself (ADR-0013 Amendment 2).
    bus_brokers: str
    #: HAMMERTIME_TRIE_FAMILIES: the address families this process holds.
    families: frozenset[AddressFamily] = frozenset({AddressFamily.IPV4})
    #: HAMMERTIME_CONFIG_POLL_INTERVAL_S: how often the detection config
    #: document is re-read (ADR-0009 decision 6, spec section 47.3).
    config_poll_interval_s: float = _DEFAULT_CONFIG_POLL_INTERVAL_S


def load_settings(env: Mapping[str, str] | None = None) -> TrieSettings:
    """Load the trie's settings from the environment (see `.env.example`)."""
    source = env if env is not None else os.environ
    host, port = _parse_bind(source.get(_BIND_KEY, _DEFAULT_BIND))
    bus_kind = _parse_choice(
        _BUS_KIND_KEY, source.get(_BUS_KIND_KEY, _DEFAULT_BUS_KIND), allowed=_ALLOWED_BUS_KINDS
    )
    bus_brokers = source.get(_BUS_BROKERS_KEY, _DEFAULT_BUS_BROKERS)
    if bus_kind == "nats":
        # ADR-0013 Amendment 4 ruling S2 as gated by Amendment 5 ruling 5:
        # under memory the value is never interpreted, so it is not checked.
        _validate_bus_brokers(bus_brokers)
    return TrieSettings(
        host=host,
        port=port,
        detection_config_path=Path(source.get(_CONFIG_PATH_KEY, _DEFAULT_CONFIG_PATH)),
        bus_kind=bus_kind,
        bus_brokers=bus_brokers,
        families=parse_families(source.get(_FAMILIES_KEY, _DEFAULT_FAMILIES)),
        config_poll_interval_s=_parse_positive_number(
            _CONFIG_POLL_INTERVAL_KEY,
            source.get(_CONFIG_POLL_INTERVAL_KEY, str(_DEFAULT_CONFIG_POLL_INTERVAL_S)),
        ),
    )


def parse_families(text: str) -> frozenset[AddressFamily]:
    """Parse `HAMMERTIME_TRIE_FAMILIES`: comma-separated `ipv4` / `ipv6`.

    Each entry is stripped and case-folded; duplicates collapse. An empty
    value, a whitespace-only value or an empty entry is a `ValueError`, as is
    any name other than the two (ADR-0017 decision 11).
    """
    if not text.strip():
        raise ValueError(f"{_FAMILIES_KEY} must name at least one of ipv4, ipv6, got {text!r}")
    families: set[AddressFamily] = set()
    for element in text.split(","):
        token = element.strip().casefold()
        if not token:
            raise ValueError(f"{_FAMILIES_KEY} has an empty element in {text!r}")
        family = _FAMILY_NAMES.get(token)
        if family is None:
            raise ValueError(
                f"{_FAMILIES_KEY} entries must be one of {sorted(_FAMILY_NAMES)}, got {token!r}"
            )
        families.add(family)
    return frozenset(families)


def _validate_bus_brokers(value: str) -> None:
    """Refuse an entry nats-py's own parse would refuse, naming its position only.

    `validate_bus_url`'s message carries none of the entry's text -- an entry
    may hold a password, and a `config_invalid` record must not echo it.
    """
    for index, entry in enumerate(split_bus_servers(value)):
        try:
            validate_bus_url(entry)
        except ValueError as exc:
            raise ValueError(f"{_BUS_BROKERS_KEY}: entry {index} is {exc}") from None


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port_text = bind.rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise ValueError(f"{_BIND_KEY} must be 'host:port', got {bind!r}")
    return host, int(port_text)


def _parse_positive_number(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if not parsed > 0:
        raise ValueError(f"{name} must be a positive number, got {parsed}")
    return parsed


def _parse_choice(name: str, value: str, *, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")
    return value
