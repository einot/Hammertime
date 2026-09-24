"""The trie's read API: `GET /prefix/{cidr}`, `GET /ip/{addr}` and `GET /prefixes/hot`.

Spec: section 3 (`capacity(P) = 2^(bits - length)`, `hot_ratio = hot_count /
capacity`), section 12 (a prefix's hot count), section 13 and section 38 (the
HOT_PREFIX predicate), section 22 (every read carries `as_of`,
`event_sequence` and `config_version`), section 29 (the read path), section 31
(`minimal=true`: the most specific qualifying prefixes), section 37
(`prefix_queries`), section 42 (the worked example: `10.20.30.1` to
`10.20.30.156`), section 46.5 and 46.7 (attributes while HOT), section 47.2
(domain read endpoints answer 503 while the service is not ready).

Written from ADR-0017 Amendment 4 -- ruling 2 (routes, the order of the
checks, the six fixed texts, the bodies and the envelope), ruling 3 (the
readiness gate comes first), ruling 5 (what `GET /prefixes/hot` lists), ruling
8 (`create_app` and the service's wiring), ruling 9 (`prefix_queries`), ruling
10 (nothing echoed; every input answered) and its "Test seams" -- with
decisions 2, 9, 13 and 15 and their 2026-09-24 notes; ADR-0010 decisions 1, 2,
4 and 5 and Amendment 5; ADR-0015 Amendment 5 rulings 1 and 2
(`request_count`, `IpRecord`); and `docs/protocol/read-api-v1.md` with its
2026-09-24 notes. The detection document is integration-scenarios section
2.1's (`minimum_hot_ips` 16, `minimum_hot_ratio` 0.10), and the floors are the
defaults, IPv4 8 and IPv6 104, unless a test says otherwise.

How the tests reach the routes (Amendment 4, "Test seams"): `create_app` over a
`TrieState` the test writes with the worker's own calls -- `apply_hot_ip_added`
or `apply_hot_ip_removed` on `state.of(family)`, then `state.note_applied` --
with a `Readiness` the test marks, through `httpx.ASGITransport`; and, in
`TestThroughTheService`, `build_service` over hot-ip events published to an
`InMemoryBus`. Every expected count, capacity, ratio and state, and every
expected `GET /prefixes/hot` list, is computed here from the addresses the test
made HOT, with section 3's formulas and section 13's conditions in exact
fractions (`_qualifies`, `_expected_prefix`, `_expected_hot_list`,
`_expected_minimal`), never by asking the trie. Section 42's list is also
written out literally (`SECTION_42_HOT_LIST`).

Choices of this file's own:

* Each HOT address is applied at the next offset, `state.event_sequence`, with
  the timestamp `T0` plus that offset in seconds, so after `n` applies the
  state's `event_sequence` is `n` and its `as_of` is `T0 + (n - 1)` seconds.
* `as_of` is compared with `_render`, this file's own writing of ruling 2's
  form, and with literal strings in `TestTheEnvelope`.
* A `TrieMetrics` handed to `create_app` is bound to the state first, since
  decision 12 has `get` read `0` for "any series read before `bind_state`".
* Refusals are sent both bare and with the query `?zzzq=zzzq`, to show that
  neither the path nor the query is echoed (ruling 2: "No response repeats any
  part of the request's path or query").
* `?minimal` with no `=` is counted among ruling 2's "an empty one" and so
  refused (`TestMinimal`).
* The 400 body is compared as parsed JSON: ruling 2, "A body's parsed value is
  the contract, not its bytes". The 503 body is compared as bytes: it is
  `/readyz`'s byte-exact body (ADR-0009 A4; read-api-v1.md's table).
* The `TestPrefixQueries` cases that are not in ruling 9's text by name -- a
  documentation path, and POST on each read path -- follow its "Not counted: a
  request the framework answers (a `404` or `405`)".
"""

import ipaddress
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import HOT_IP
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import InvariantViolation
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded
from hammertime.core.runtime import Readiness
from hammertime.trie.config import TrieSettings
from hammertime.trie.metadata.ip_attributes import apply_hot_ip_added, apply_hot_ip_removed
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.query.app import create_app
from hammertime.trie.service import build_service
from hammertime.trie.state import TrieState

IPV4 = AddressFamily.IPV4
IPV6 = AddressFamily.IPV6
ONLY_V4 = frozenset({IPV4})
BOTH = frozenset({IPV4, IPV6})
FLOORS: dict[AddressFamily, int] = {IPV4: 8, IPV6: 104}

T0 = datetime.fromtimestamp(1_800_000_000, tz=UTC)
AN_HOUR = 3600.0
TOPIC = HOT_IP.name

STARTING = b'{"status":"starting"}'
STOPPING = b'{"status":"stopping"}'

# Ruling 2's six fixed texts.
MALFORMED_PREFIX = "malformed prefix"
OUT_OF_RANGE = "prefix length out of range"
HOST_BITS_SET = "host bits set"
MALFORMED_ADDRESS = "malformed address"
NOT_SERVED = "address family not served"
BAD_MINIMAL = "minimal must be true or false"

# The token every refusal test sends and no response may repeat.
TOKEN = "zzzq"

PREFIX_BODY_KEYS = {
    "prefix",
    "hot_ips",
    "capacity",
    "hot_ratio",
    "state",
    "as_of",
    "event_sequence",
    "config_version",
}
ITEM_KEYS = {"prefix", "hot_ips", "capacity", "hot_ratio", "state"}
HOT_IP_BODY_KEYS = {
    "ip",
    "state",
    "request_count",
    "attributes",
    "matched_prefixes",
    "as_of",
    "event_sequence",
    "config_version",
}
COLD_IP_BODY_KEYS = HOT_IP_BODY_KEYS - {"attributes"}
LIST_BODY_KEYS = {"prefixes", "as_of", "event_sequence", "config_version"}

# Section 42: "10.20.30.1, 10.20.30.2, ..., 10.20.30.156".
SECTION_42 = [f"10.20.30.{i}" for i in range(1, 157)]
# Section 42's addresses and the same 156 in the next /24.
NESTED = SECTION_42 + [f"10.20.31.{i}" for i in range(1, 157)]
# `2001:db8::` through `2001:db8::f`: one full IPv6 /124.
V6_BLOCK = [f"2001:db8::{i:x}" for i in range(16)]

# Section 42 under the default document, as ADR-0017 Amendment 4 ruling 2
# gives it: (prefix, hot_ips, capacity, hot_ratio), in the listed order.
SECTION_42_HOT_LIST: list[tuple[str, int, int, float]] = [
    ("10.20.30.16/28", 16, 16, 1.0),
    ("10.20.30.32/28", 16, 16, 1.0),
    ("10.20.30.48/28", 16, 16, 1.0),
    ("10.20.30.64/28", 16, 16, 1.0),
    ("10.20.30.80/28", 16, 16, 1.0),
    ("10.20.30.96/28", 16, 16, 1.0),
    ("10.20.30.112/28", 16, 16, 1.0),
    ("10.20.30.128/28", 16, 16, 1.0),
    ("10.20.30.0/27", 31, 32, 0.96875),
    ("10.20.30.32/27", 32, 32, 1.0),
    ("10.20.30.64/27", 32, 32, 1.0),
    ("10.20.30.96/27", 32, 32, 1.0),
    ("10.20.30.128/27", 29, 32, 0.90625),
    ("10.20.30.0/26", 63, 64, 0.984375),
    ("10.20.30.64/26", 64, 64, 1.0),
    ("10.20.30.128/26", 29, 64, 0.453125),
    ("10.20.30.0/25", 127, 128, 0.9921875),
    ("10.20.30.128/25", 29, 128, 0.2265625),
    ("10.20.30.0/24", 156, 256, 0.609375),
    ("10.20.30.0/23", 156, 512, 0.3046875),
    ("10.20.28.0/22", 156, 1024, 0.15234375),
]
SECTION_42_MINIMAL = [
    "10.20.30.16/28",
    "10.20.30.32/28",
    "10.20.30.48/28",
    "10.20.30.64/28",
    "10.20.30.80/28",
    "10.20.30.96/28",
    "10.20.30.112/28",
    "10.20.30.128/28",
]

ROUTES = ("prefix", "ip", "prefixes_hot")
RESULTS = ("ok", "invalid", "family_not_served", "not_ready")

# FastAPI's default documentation paths (ADR-0017 decision 13).
GENERATED_DOCUMENTATION_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


def _config(
    *, version: int = 1, minimum_hot_ips: int = 16, minimum_hot_ratio: float = 0.10
) -> DetectionConfig:
    """Integration-scenarios section 2.1's document, with the three overridable."""

    return DetectionConfig(
        config_version=version,
        window_seconds=300,
        bucket_seconds=10,
        hot_threshold=1000,
        cold_threshold=800,
        minimum_hot_ips=minimum_hot_ips,
        minimum_hot_ratio=minimum_hot_ratio,
        allowed_lateness_seconds=30,
        state_retention_seconds=600,
    )


CONFIG = _config()


# --------------------------------------------------------------------------
# Independent expectations: section 3's formulas and section 13's conditions.
# --------------------------------------------------------------------------


def _qualifies(hot: int, capacity: int, config: DetectionConfig) -> bool:
    enough = hot >= config.minimum_hot_ips
    dense = Fraction(hot, capacity) >= Fraction(config.minimum_hot_ratio)
    return enough and dense


def _item(prefix: str, hot: int, capacity: int, config: DetectionConfig) -> dict[str, Any]:
    return {
        "prefix": prefix,
        "hot_ips": hot,
        "capacity": capacity,
        "hot_ratio": float(Fraction(hot, capacity)),
        "state": "HOT_PREFIX" if _qualifies(hot, capacity, config) else "NORMAL",
    }


def _expected_prefix(
    prefix: str, hot: Iterable[str], config: DetectionConfig = CONFIG
) -> dict[str, Any]:
    network = ipaddress.ip_network(prefix)
    count = sum(1 for text in hot if ipaddress.ip_address(text) in network)
    capacity = 2 ** (network.max_prefixlen - network.prefixlen)
    return _item(str(network), count, capacity, config)


def _ancestor(address: str, length: int) -> str:
    return str(ipaddress.ip_network(f"{address}/{length}", strict=False))


def _network_text(bits: int, network: int, length: int) -> str:
    if bits == 32:
        return str(ipaddress.IPv4Network((network, length)))
    return str(ipaddress.IPv6Network((network, length)))


def _expected_hot_list(
    hot: Sequence[str],
    floors: Mapping[AddressFamily, int],
    config: DetectionConfig = CONFIG,
) -> list[dict[str, Any]]:
    """Ruling 2: per served family, IPv4 first, every prefix from the floor to
    the host route that holds a HOT address and qualifies; length descending,
    then network ascending."""

    items: list[dict[str, Any]] = []
    for family, bits in ((IPV4, 32), (IPV6, 128)):
        members = [
            int(ipaddress.ip_address(text))
            for text in hot
            if ipaddress.ip_address(text).max_prefixlen == bits
        ]
        if not members:
            continue
        for length in range(bits, floors[family] - 1, -1):
            shift = bits - length
            capacity = 1 << shift
            counts = Counter(member >> shift for member in members)
            for key in sorted(counts):
                if _qualifies(counts[key], capacity, config):
                    text = _network_text(bits, key << shift, length)
                    items.append(_item(text, counts[key], capacity, config))
    return items


def _expected_minimal(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ruling 2: keep an item only when no listed prefix of the same family lies
    strictly inside it."""

    networks = [ipaddress.ip_network(item["prefix"]) for item in items]
    kept: list[dict[str, Any]] = []
    for item, network in zip(items, networks, strict=True):
        inside = any(
            other.version == network.version
            and other.prefixlen > network.prefixlen
            and other.network_address in network
            for other in networks
        )
        if not inside:
            kept.append(item)
    return kept


def _render(timestamp: datetime) -> str:
    """Ruling 2's `as_of`: `YYYY-MM-DDTHH:MM:SS` in UTC, `.ffffff` only when the
    microseconds are not zero, then `Z`."""

    utc = timestamp.astimezone(UTC)
    fraction = f".{utc.microsecond:06d}" if utc.microsecond else ""
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


# --------------------------------------------------------------------------
# Writing a state, building an app, asking it.
# --------------------------------------------------------------------------


def _family(text: str) -> AddressFamily:
    return IPV4 if ipaddress.ip_address(text).version == 4 else IPV6


def _state(
    families: Iterable[AddressFamily] = ONLY_V4, config: DetectionConfig = CONFIG
) -> TrieState:
    return TrieState(families=frozenset(families), config=config)


def _add(
    state: TrieState,
    text: str,
    attributes: Mapping[str, object] | None = None,
    *,
    request_count: int = 1200,
    timestamp: datetime | None = None,
) -> None:
    """One applied `HotIpAdded`, as the worker writes it (Amendment 4, "Test
    seams"), at the next offset."""

    fs = state.of(_family(text))
    address = Address.parse(text)
    apply_hot_ip_added(fs.trie, fs.records, address, attributes, request_count=request_count)
    offset = state.event_sequence
    if timestamp is None:
        timestamp = T0 + timedelta(seconds=offset)
    state.note_applied(offset, timestamp)


def _remove(state: TrieState, text: str) -> None:
    fs = state.of(_family(text))
    assert apply_hot_ip_removed(fs.trie, fs.records, Address.parse(text)) is True
    offset = state.event_sequence
    state.note_applied(offset, T0 + timedelta(seconds=offset))


def _make_hot(state: TrieState, texts: Iterable[str]) -> None:
    for text in texts:
        _add(state, text)


def _ready() -> Readiness:
    readiness = Readiness()
    readiness.mark_ready()
    return readiness


def _metrics(state: TrieState) -> TrieMetrics:
    metrics = TrieMetrics()
    metrics.bind_state(state)
    return metrics


def _app(
    state: TrieState,
    *,
    readiness: Readiness | None = None,
    floors: Mapping[AddressFamily, int] = FLOORS,
    metrics: TrieMetrics | None = None,
) -> FastAPI:
    return create_app(
        readiness if readiness is not None else _ready(),
        state=state,
        metrics=metrics if metrics is not None else _metrics(state),
        min_prefix_lengths=floors,
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with _client(app) as client:
        return await client.get(path)


def _media_type(response: httpx.Response) -> str:
    return response.headers["content-type"].split(";")[0].strip()


def _ok(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.content
    assert _media_type(response) == "application/json"
    return response.json()


def _assert_refused(response: httpx.Response, text: str) -> None:
    assert response.status_code == 400, response.content
    assert _media_type(response) == "application/json"
    assert response.json() == {"detail": text}
    assert TOKEN.encode() not in response.content


def _items(body: Any) -> list[dict[str, Any]]:
    prefixes: list[dict[str, Any]] = body["prefixes"]
    for item in prefixes:
        assert set(item) == ITEM_KEYS, item
    return prefixes


def _section_42_state(families: Iterable[AddressFamily] = ONLY_V4) -> TrieState:
    state = _state(families)
    _make_hot(state, SECTION_42)
    return state


# ==========================================================================
# A. The readiness gate (ruling 3; section 47.2; read-api-v1.md).
# ==========================================================================

GATED_PATHS = (
    "/prefix/10.20.30.0/24",
    "/ip/10.20.30.1",
    "/prefixes/hot",
    "/prefixes/hot?minimal=true",
    f"/prefix/{TOKEN}",
    f"/ip/{TOKEN}",
    f"/prefixes/hot?minimal={TOKEN}",
    "/prefix/2001:db8::/32",
    "/ip/2001:db8::1",
)


class TestReadinessGate:
    """Ruling 3: "Each read route checks readiness before it looks at its
    input. While the service is not ready it raises `ServiceNotReady`, which
    decision 13's handler renders as `503` with `/readyz`'s body ... So while
    the service is not ready a malformed request is `503`, not `400`"."""

    @staticmethod
    async def _assert_gated(client: httpx.AsyncClient, body: bytes) -> None:
        for path in GATED_PATHS:
            response = await client.get(path)
            assert response.status_code == 503, path
            assert response.content == body, path
            assert _media_type(response) == "application/json", path

    @staticmethod
    async def _assert_admin(client: httpx.AsyncClient, readyz_status: int, readyz: bytes) -> None:
        healthz = await client.get("/healthz")
        assert healthz.status_code == 200
        assert healthz.content == b'{"status":"ok"}'
        response = await client.get("/readyz")
        assert response.status_code == readyz_status
        assert response.content == readyz
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")

    async def test_starting_ready_stopping(self) -> None:
        state = _section_42_state()
        readiness = Readiness()
        app = _app(state, readiness=readiness)

        async with _client(app) as client:
            await self._assert_gated(client, STARTING)
            await self._assert_admin(client, 503, STARTING)

            readiness.mark_ready()
            assert (await client.get("/prefix/10.20.30.0/24")).status_code == 200
            await self._assert_admin(client, 200, b'{"status":"ready"}')

            readiness.mark_stopping()
            await self._assert_gated(client, STOPPING)
            await self._assert_admin(client, 503, STOPPING)


# ==========================================================================
# B. GET /prefix/{cidr} on section 42's dataset.
# ==========================================================================


class TestPrefix:
    @pytest.mark.parametrize(
        ("cidr", "hot_ips", "capacity", "hot_ratio", "state_text"),
        [
            pytest.param("10.20.30.0/24", 156, 256, 0.609375, "HOT_PREFIX", id="the-24"),
            pytest.param("10.20.0.0/16", 156, 65536, 156 / 65536, "NORMAL", id="its-16"),
            pytest.param("10.20.30.16/28", 16, 16, 1.0, "HOT_PREFIX", id="a-full-28"),
            pytest.param("10.20.30.16/29", 8, 8, 1.0, "NORMAL", id="a-full-29"),
            pytest.param("10.20.30.1/32", 1, 1, 1.0, "NORMAL", id="a-host-route"),
            pytest.param("192.0.2.0/24", 0, 256, 0.0, "NORMAL", id="no-node"),
            pytest.param("0.0.0.0/0", 156, 4294967296, 156 / 2**32, "NORMAL", id="the-root"),
        ],
    )
    async def test_section_42(
        self, cidr: str, hot_ips: int, capacity: int, hot_ratio: float, state_text: str
    ) -> None:
        state = _section_42_state()

        body = _ok(await _get(_app(state), f"/prefix/{cidr}"))

        assert set(body) == PREFIX_BODY_KEYS
        assert body["prefix"] == cidr
        assert body["hot_ips"] == hot_ips
        assert body["capacity"] == capacity
        # read-api-v1.md: "`capacity` ... as a JSON integer".
        assert type(body["capacity"]) is int
        assert body["hot_ratio"] == hot_ratio
        assert body["state"] == state_text
        assert {key: body[key] for key in ITEM_KEYS} == _expected_prefix(cidr, SECTION_42)
        assert body["event_sequence"] == state.event_sequence == 156
        assert body["config_version"] == 1
        assert body["as_of"] == _render(T0 + timedelta(seconds=155))

    async def test_an_ipv6_capacity_beyond_2_to_the_53_is_an_exact_integer(self) -> None:
        # read-api-v1.md: "For IPv6 prefixes shorter than /75 this exceeds
        # 2**53; clients must parse it with arbitrary precision."
        state = _state(BOTH)
        _make_hot(state, V6_BLOCK)

        body = _ok(await _get(_app(state), "/prefix/2001:db8::/32"))

        assert body["capacity"] == 2**96
        assert type(body["capacity"]) is int
        assert body["hot_ips"] == 16
        assert body["hot_ratio"] == float(Fraction(16, 2**96))
        assert body["state"] == "NORMAL"


class TestTheEnvelope:
    """Ruling 2, "The envelope": "`as_of` is `null`, or the codec's rendering of
    a timestamp: `YYYY-MM-DDTHH:MM:SS` in UTC, then `.ffffff` only when the
    microseconds are not zero, then `Z`. `event_sequence` is
    `state.event_sequence`, and `config_version` is
    `state.config.config_version`." Decision 8: `as_of` is "the greatest
    `timestamp` among ... events whose outcome was `APPLIED` or `UNCHANGED`,
    and `None` before the first"."""

    PATHS = ("/prefix/10.20.30.0/24", "/ip/10.20.30.1", "/prefixes/hot")

    async def test_before_anything_is_applied(self) -> None:
        state = _state()

        async with _client(_app(state)) as client:
            for path in self.PATHS:
                body = _ok(await client.get(path))
                assert body["as_of"] is None, path
                assert body["event_sequence"] == 0, path
                assert body["config_version"] == 1, path

        body = _ok(await _get(_app(state), "/prefix/10.20.30.0/24"))
        assert body["hot_ips"] == 0
        assert body["state"] == "NORMAL"

    @pytest.mark.parametrize(
        ("timestamp", "text"),
        [
            pytest.param(
                datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC), "2026-09-14T10:05:00Z", id="whole"
            ),
            pytest.param(
                datetime(2026, 9, 14, 10, 5, 0, 123456, tzinfo=UTC),
                "2026-09-14T10:05:00.123456Z",
                id="microseconds",
            ),
            pytest.param(
                datetime(2026, 9, 14, 10, 5, 0, 500, tzinfo=UTC),
                "2026-09-14T10:05:00.000500Z",
                id="leading-zeros",
            ),
            pytest.param(
                datetime(2026, 9, 14, 12, 5, 0, tzinfo=timezone(timedelta(hours=2))),
                "2026-09-14T10:05:00Z",
                id="another-offset-in-utc",
            ),
        ],
    )
    async def test_as_of_rendering(self, timestamp: datetime, text: str) -> None:
        state = _state()
        _add(state, "10.20.30.1", timestamp=timestamp)

        async with _client(_app(state)) as client:
            for path in self.PATHS:
                body = _ok(await client.get(path))
                assert body["as_of"] == text, path
                assert body["event_sequence"] == 1, path

    async def test_as_of_is_the_greatest_timestamp_not_the_last(self) -> None:
        state = _state()
        later = datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC)
        _add(state, "10.20.30.1", timestamp=later)
        _add(state, "10.20.30.2", timestamp=later - timedelta(minutes=1))

        body = _ok(await _get(_app(state), "/prefix/10.20.30.0/24"))

        assert body["as_of"] == "2026-09-14T10:05:00Z"
        assert body["event_sequence"] == 2

    async def test_event_sequence_is_the_states_position_plus_one(self) -> None:
        state = _state()
        _add(state, "10.20.30.1")
        state.note_handled(41)

        async with _client(_app(state)) as client:
            for path in self.PATHS:
                body = _ok(await client.get(path))
                assert body["event_sequence"] == state.event_sequence == 42, path
                assert body["as_of"] == _render(T0), path


# ==========================================================================
# C. The configuration in force.
# ==========================================================================


class TestTheConfigurationInForce:
    """Ruling 2: `state` is `evaluate_prefix_state(hot_ips, capacity, config)`
    and `config_version` is `state.config.config_version`; ruling 4: the route
    "reads `state.config` once". Decision 10 as noted: "adopting the document
    stays the whole re-evaluation"."""

    async def test_an_adopted_document_decides_the_next_answer(self) -> None:
        state = _section_42_state()
        app = _app(state)

        async with _client(app) as client:
            body = _ok(await client.get("/prefix/10.20.30.0/24"))
            assert body["state"] == "HOT_PREFIX"
            assert body["config_version"] == 1

            stricter = _config(version=2, minimum_hot_ips=200)
            state.adopt_config(stricter)

            body = _ok(await client.get("/prefix/10.20.30.0/24"))
            assert body["state"] == "NORMAL"
            assert body["config_version"] == 2

            body = _ok(await client.get("/ip/10.20.30.1"))
            assert body["config_version"] == 2
            assert [item["state"] for item in body["matched_prefixes"]] == ["NORMAL"] * 3

            body = _ok(await client.get("/prefixes/hot"))
            assert body["config_version"] == 2
            assert _items(body) == _expected_hot_list(SECTION_42, FLOORS, stricter) == []


# ==========================================================================
# D. GET /prefix/{cidr} refusals and accepted forms (ruling 2, "Parsing
# `{cidr}`").
# ==========================================================================

MALFORMED_PREFIXES = [
    pytest.param("10.20.30.0", id="no-slash"),
    pytest.param("10.20.30.0/", id="empty-length"),
    pytest.param("10.20.30.0/+24", id="signed-length"),
    pytest.param("10.20.30.0/%2024", id="space-before-length"),
    pytest.param("10.20.30.0/2_4", id="underscore-in-length"),
    # U+0662 U+0664, ARABIC-INDIC DIGIT TWO and FOUR, as UTF-8 escapes.
    pytest.param("10.20.30.0/%D9%A2%D9%A4", id="arabic-indic-digits"),
    pytest.param("10.20.30.0/1000", id="four-digits"),
    pytest.param("10.20.30.0/24/", id="trailing-slash"),
    pytest.param("10.20.30.0/24/8", id="two-lengths"),
    pytest.param("", id="empty"),
    pytest.param(f"{TOKEN}.20.30.0/24", id="not-an-address"),
    pytest.param("300.20.30.0/24", id="octet-out-of-range"),
    pytest.param(TOKEN, id="token"),
    pytest.param(f"10.20.30.0/{TOKEN}", id="token-length"),
]


class TestPrefixRefusals:
    """Ruling 2: "A refusal is `400`, `Content-Type: application/json`, with the
    body `{"detail":"<text>"}` ... No response repeats any part of the request's
    path or query." The checks run in order and the first that fails decides:
    malformed, out of range, host bits, family not served."""

    @staticmethod
    async def _assert_prefix_refused(state: TrieState, cidr: str, text: str) -> None:
        async with _client(_app(state)) as client:
            for suffix in ("", f"?{TOKEN}={TOKEN}"):
                response = await client.get(f"/prefix/{cidr}{suffix}")
                _assert_refused(response, text)

    @pytest.mark.parametrize("cidr", MALFORMED_PREFIXES)
    async def test_malformed_prefix(self, cidr: str) -> None:
        await self._assert_prefix_refused(_section_42_state(), cidr, MALFORMED_PREFIX)

    @pytest.mark.parametrize(
        "cidr",
        [
            pytest.param("10.20.30.0/33", id="33"),
            pytest.param("10.20.30.0/999", id="999"),
            # Check 4 comes before check 5.
            pytest.param("10.20.30.1/33", id="host-bits-and-33"),
        ],
    )
    async def test_prefix_length_out_of_range(self, cidr: str) -> None:
        await self._assert_prefix_refused(_section_42_state(), cidr, OUT_OF_RANGE)

    async def test_host_bits_set(self) -> None:
        await self._assert_prefix_refused(_section_42_state(), "10.20.30.1/24", HOST_BITS_SET)

    @pytest.mark.parametrize(
        ("cidr", "text"),
        [
            pytest.param("2001:db8::/32", NOT_SERVED, id="not-served"),
            # Checks 4 and 5 come before check 6.
            pytest.param("2001:db8::/129", OUT_OF_RANGE, id="out-of-range"),
            pytest.param("2001:db8::1/64", HOST_BITS_SET, id="host-bits"),
        ],
    )
    async def test_ipv6_on_an_ipv4_only_trie(self, cidr: str, text: str) -> None:
        await self._assert_prefix_refused(_section_42_state(), cidr, text)

    @pytest.mark.parametrize(
        ("cidr", "text"),
        [
            pytest.param("2001:db8::/129", OUT_OF_RANGE, id="out-of-range"),
            pytest.param("2001:db8::1/64", HOST_BITS_SET, id="host-bits"),
        ],
    )
    async def test_ipv6_refusals_on_a_dual_family_trie(self, cidr: str, text: str) -> None:
        await self._assert_prefix_refused(_section_42_state(BOTH), cidr, text)


class TestPrefixAcceptedForms:
    @pytest.mark.parametrize(
        ("cidr", "canonical"),
        [
            # Ruling 2 / read-api-v1.md: "`/prefix/10.0.0.0/08` answers for
            # `10.0.0.0/8`"; assumption 88: "Leading zeros are allowed".
            pytest.param("10.0.0.0/08", "10.0.0.0/8", id="leading-zero"),
            pytest.param("10.20.30.0/024", "10.20.30.0/24", id="three-digits"),
        ],
    )
    async def test_the_answer_names_the_canonical_prefix(self, cidr: str, canonical: str) -> None:
        body = _ok(await _get(_app(_section_42_state()), f"/prefix/{cidr}"))

        assert body["prefix"] == canonical
        assert {key: body[key] for key in ITEM_KEYS} == _expected_prefix(canonical, SECTION_42)

    async def test_an_ipv6_prefix_on_a_dual_family_trie(self) -> None:
        body = _ok(await _get(_app(_section_42_state(BOTH)), "/prefix/2001:DB8::/32"))

        assert set(body) == PREFIX_BODY_KEYS
        assert body["prefix"] == "2001:db8::/32"
        assert body["hot_ips"] == 0
        assert body["capacity"] == 2**96
        assert body["hot_ratio"] == 0.0
        assert body["state"] == "NORMAL"

    async def test_query_parameters_are_ignored(self) -> None:
        response = await _get(
            _app(_section_42_state()), f"/prefix/10.20.30.0/24?minimal={TOKEN}&x=1"
        )

        body = _ok(response)
        assert body["prefix"] == "10.20.30.0/24"
        assert body["hot_ips"] == 156
        assert TOKEN.encode() not in response.content


# ==========================================================================
# E. GET /ip/{addr}.
# ==========================================================================


def _expected_matched(address: str, hot: Iterable[str], lengths: Sequence[int]) -> list[Any]:
    hot = list(hot)
    return [_expected_prefix(_ancestor(address, length), hot) for length in lengths]


class TestIp:
    """Ruling 2: `state` is `"HOT"` when `trie.contains(address)`; while HOT,
    `request_count` and `attributes` come from the address's record; while
    COLD, `request_count` is `0` and `attributes` is absent. `matched_prefixes`
    are the ancestors with 24, 16 and 8 host bits, shortest first, "present
    whether the address is HOT or COLD". ADR-0015 Amendment 5 ruling 1: "every
    applied `HotIpAdded` replaces it"; read-api-v1.md: "An absent `attributes`
    on the event is stored, and returned, as `{"attributes_version": 1}`"."""

    async def test_a_hot_address(self) -> None:
        state = _state()
        document = {"attributes_version": 1, "weight": 1200}
        _add(state, "10.20.30.1", document, request_count=1200)
        _make_hot(state, SECTION_42[1:])

        body = _ok(await _get(_app(state), "/ip/10.20.30.1"))

        assert set(body) == HOT_IP_BODY_KEYS
        assert body["ip"] == "10.20.30.1"
        assert body["state"] == "HOT"
        assert body["request_count"] == 1200
        assert body["attributes"] == document
        matched = body["matched_prefixes"]
        for item in matched:
            assert set(item) == ITEM_KEYS
        assert [item["prefix"] for item in matched] == [
            "10.0.0.0/8",
            "10.20.0.0/16",
            "10.20.30.0/24",
        ]
        assert matched == _expected_matched("10.20.30.1", SECTION_42, (8, 16, 24))
        assert [item["state"] for item in matched] == ["NORMAL", "NORMAL", "HOT_PREFIX"]
        assert body["event_sequence"] == state.event_sequence == 156
        assert body["config_version"] == 1
        assert body["as_of"] == _render(T0 + timedelta(seconds=155))

    async def test_a_later_add_replaces_the_count_and_the_document(self) -> None:
        state = _state()
        _add(state, "10.20.30.1", {"attributes_version": 1, "weight": 1200}, request_count=1200)
        _add(state, "10.20.30.1", None, request_count=1500)

        body = _ok(await _get(_app(state), "/ip/10.20.30.1"))

        assert set(body) == HOT_IP_BODY_KEYS
        assert body["state"] == "HOT"
        assert body["request_count"] == 1500
        assert body["attributes"] == {"attributes_version": 1}

    async def test_a_cold_address(self) -> None:
        state = _section_42_state()

        body = _ok(await _get(_app(state), "/ip/10.20.30.200"))

        assert set(body) == COLD_IP_BODY_KEYS
        assert body["ip"] == "10.20.30.200"
        assert body["state"] == "COLD"
        assert body["request_count"] == 0
        assert body["matched_prefixes"] == _expected_matched(
            "10.20.30.200", SECTION_42, (8, 16, 24)
        )
        assert body["event_sequence"] == 156

    async def test_an_address_after_its_removal(self) -> None:
        state = _section_42_state()
        _remove(state, "10.20.30.1")
        still_hot = SECTION_42[1:]

        body = _ok(await _get(_app(state), "/ip/10.20.30.1"))

        assert set(body) == COLD_IP_BODY_KEYS
        assert body["state"] == "COLD"
        assert body["request_count"] == 0
        assert body["matched_prefixes"] == _expected_matched("10.20.30.1", still_hot, (8, 16, 24))
        assert body["matched_prefixes"][2]["hot_ips"] == 155
        assert body["event_sequence"] == state.event_sequence == 157

    async def test_an_ipv6_address_matches_its_104_112_and_120(self) -> None:
        state = _section_42_state(BOTH)
        _make_hot(state, V6_BLOCK)

        async with _client(_app(state)) as client:
            hot = _ok(await client.get("/ip/2001:DB8::1"))
            cold = _ok(await client.get("/ip/2001:db8::1:0"))

        assert set(hot) == HOT_IP_BODY_KEYS
        assert hot["ip"] == "2001:db8::1"
        assert hot["state"] == "HOT"
        assert hot["request_count"] == 1200
        assert hot["attributes"] == {"attributes_version": 1}
        assert [item["prefix"] for item in hot["matched_prefixes"]] == [
            "2001:db8::/104",
            "2001:db8::/112",
            "2001:db8::/120",
        ]
        assert hot["matched_prefixes"] == _expected_matched(
            "2001:db8::1", V6_BLOCK, (104, 112, 120)
        )

        assert set(cold) == COLD_IP_BODY_KEYS
        assert cold["state"] == "COLD"
        assert cold["matched_prefixes"] == _expected_matched(
            "2001:db8::1:0", V6_BLOCK, (104, 112, 120)
        )


# ==========================================================================
# F. GET /ip/{addr} refusals.
# ==========================================================================


class TestIpRefusals:
    @pytest.mark.parametrize(
        "addr",
        [
            pytest.param(TOKEN, id="token"),
            pytest.param("10.20.30", id="three-octets"),
            # Assumption 87: the path convertor, so this meets the route's 400.
            pytest.param("10.20.30.1/32", id="a-prefix"),
            pytest.param("", id="empty"),
            pytest.param("10.20.30.256", id="octet-out-of-range"),
            pytest.param(f"10.20.30.1{TOKEN}", id="trailing-token"),
        ],
    )
    async def test_malformed_address(self, addr: str) -> None:
        async with _client(_app(_section_42_state())) as client:
            for suffix in ("", f"?{TOKEN}={TOKEN}"):
                _assert_refused(await client.get(f"/ip/{addr}{suffix}"), MALFORMED_ADDRESS)

    async def test_an_ipv6_address_on_an_ipv4_only_trie(self) -> None:
        async with _client(_app(_section_42_state())) as client:
            for suffix in ("", f"?{TOKEN}={TOKEN}"):
                _assert_refused(await client.get(f"/ip/2001:db8::1{suffix}"), NOT_SERVED)

    async def test_query_parameters_are_ignored(self) -> None:
        response = await _get(_app(_section_42_state()), f"/ip/10.20.30.1?minimal={TOKEN}&x=1")

        body = _ok(response)
        assert body["state"] == "HOT"
        assert TOKEN.encode() not in response.content


# ==========================================================================
# G. The forbidden state (ruling 2; assumption 101; "Test seams").
# ==========================================================================


class TestAHotAddressWithNoRecord:
    """Ruling 2: "A HOT address with no record is a state §46.5 forbids. The
    route then raises `InvariantViolation` ... The read path neither repairs
    the state nor ends the process." "Test seams": through
    `httpx.ASGITransport`, "whose default re-raises an application's
    exception", the request raises `InvariantViolation` in the test. Ruling 9:
    a `500` is not counted."""

    async def test_get_ip_raises_and_get_prefix_still_answers(self) -> None:
        state = _section_42_state()
        metrics = _metrics(state)
        assert state.of(IPV4).records.discard(Address.parse("10.20.30.1")) is True
        app = _app(state, metrics=metrics)

        async with _client(app) as client:
            with pytest.raises(InvariantViolation):
                await client.get("/ip/10.20.30.1")

            body = _ok(await client.get("/prefix/10.20.30.0/24"))
            assert body["hot_ips"] == 156
            assert body["state"] == "HOT_PREFIX"

        for result in RESULTS:
            assert metrics.get("prefix_queries", route="ip", result=result) == 0, result
        assert metrics.get("prefix_queries", route="prefix", result="ok") == 1


# ==========================================================================
# H. GET /prefixes/hot on section 42's dataset.
# ==========================================================================


class TestHotPrefixes:
    """Ruling 2: every prefix of a served family from the floor to the host
    route whose `hot_ips` is at least 1 and whose `state` is `HOT_PREFIX`;
    IPv4 before IPv6; length descending, then network ascending; with
    `minimal=true`, only those with no listed prefix strictly inside them."""

    async def test_an_empty_state(self) -> None:
        body = _ok(await _get(_app(_state()), "/prefixes/hot"))

        assert set(body) == LIST_BODY_KEYS
        assert body["prefixes"] == []
        assert body["as_of"] is None
        assert body["event_sequence"] == 0
        assert body["config_version"] == 1

    async def test_section_42_lists_21_prefixes(self) -> None:
        state = _section_42_state()

        body = _ok(await _get(_app(state), "/prefixes/hot"))

        assert set(body) == LIST_BODY_KEYS
        items = _items(body)
        literal = [
            {
                "prefix": prefix,
                "hot_ips": hot,
                "capacity": capacity,
                "hot_ratio": ratio,
                "state": "HOT_PREFIX",
            }
            for prefix, hot, capacity, ratio in SECTION_42_HOT_LIST
        ]
        assert len(items) == 21
        assert items == literal
        assert items == _expected_hot_list(SECTION_42, FLOORS)
        assert body["event_sequence"] == state.event_sequence == 156
        assert body["as_of"] == _render(T0 + timedelta(seconds=155))

    async def test_minimal_keeps_the_eight_28s(self) -> None:
        async with _client(_app(_section_42_state())) as client:
            full = _items(_ok(await client.get("/prefixes/hot")))
            minimal = _items(_ok(await client.get("/prefixes/hot?minimal=true")))

        assert [item["prefix"] for item in minimal] == SECTION_42_MINIMAL
        assert minimal == _expected_minimal(full)
        for item in minimal:
            assert (item["hot_ips"], item["capacity"], item["hot_ratio"]) == (16, 16, 1.0)
            assert item["state"] == "HOT_PREFIX"

    async def test_minimal_false_is_the_full_list(self) -> None:
        async with _client(_app(_section_42_state())) as client:
            full = _ok(await client.get("/prefixes/hot"))
            explicit = _ok(await client.get("/prefixes/hot?minimal=false"))

        assert explicit == full
        assert len(explicit["prefixes"]) == 21


# ==========================================================================
# I. Two neighbouring /24s.
# ==========================================================================


class TestNestedDataset:
    async def test_two_24s_list_41_prefixes(self) -> None:
        state = _state()
        _make_hot(state, NESTED)

        async with _client(_app(state)) as client:
            full = _items(_ok(await client.get("/prefixes/hot")))
            minimal = _items(_ok(await client.get("/prefixes/hot?minimal=true")))

        assert full == _expected_hot_list(NESTED, FLOORS)
        assert len(full) == 41
        by_prefix = {item["prefix"]: item for item in full}
        assert by_prefix["10.20.30.0/23"] == _item("10.20.30.0/23", 312, 512, CONFIG)
        assert by_prefix["10.20.28.0/22"] == _item("10.20.28.0/22", 312, 1024, CONFIG)
        assert by_prefix["10.20.24.0/21"] == {
            "prefix": "10.20.24.0/21",
            "hot_ips": 312,
            "capacity": 2048,
            "hot_ratio": 0.15234375,
            "state": "HOT_PREFIX",
        }
        # 312 / 4096 is below 10%.
        assert "10.20.16.0/20" not in by_prefix
        # Each /24's own 19: section 42's list without its /23 and /22.
        for third in (30, 31):
            own = [
                item
                for item in full
                if item["prefix"].startswith(f"10.20.{third}.")
                and int(item["prefix"].rsplit("/", 1)[1]) >= 24
            ]
            assert len(own) == 19, third

        assert minimal == _expected_minimal(full)
        assert len(minimal) == 16
        assert all(item["prefix"].endswith("/28") for item in minimal)
        second = [prefix.replace("10.20.30.", "10.20.31.") for prefix in SECTION_42_MINIMAL]
        assert [item["prefix"] for item in minimal] == SECTION_42_MINIMAL + second


# ==========================================================================
# J. The reporting floors (ruling 2; Amendment 2 ruling 1).
# ==========================================================================


class TestFloors:
    async def test_an_ipv4_floor_of_24(self) -> None:
        floors = {IPV4: 24, IPV6: 104}
        app = _app(_section_42_state(), floors=floors)

        async with _client(app) as client:
            items = _items(_ok(await client.get("/prefixes/hot")))
            above = _ok(await client.get("/prefix/10.20.30.0/23"))

        prefixes = [item["prefix"] for item in items]
        assert "10.20.30.0/23" not in prefixes
        assert "10.20.28.0/22" not in prefixes
        assert items == _expected_hot_list(SECTION_42, floors)
        assert len(items) == 19
        # Assumption 92: a qualifying prefix above the floor "is answered by
        # `GET /prefix/{cidr}` and not listed".
        assert above["state"] == "HOT_PREFIX"
        assert above["hot_ips"] == 156

    @pytest.mark.parametrize(
        ("floor", "expected"),
        [
            pytest.param(
                104,
                [
                    ("2001:db8::/124", 16, 16),
                    ("2001:db8::/123", 16, 32),
                    ("2001:db8::/122", 16, 64),
                    ("2001:db8::/121", 16, 128),
                ],
                id="104",
            ),
            pytest.param(
                122,
                [
                    ("2001:db8::/124", 16, 16),
                    ("2001:db8::/123", 16, 32),
                    ("2001:db8::/122", 16, 64),
                ],
                id="122",
            ),
        ],
    )
    async def test_the_ipv6_floor(self, floor: int, expected: list[tuple[str, int, int]]) -> None:
        state = _state(BOTH)
        _make_hot(state, V6_BLOCK)
        floors = {IPV4: 8, IPV6: floor}

        async with _client(_app(state, floors=floors)) as client:
            items = _items(_ok(await client.get("/prefixes/hot")))
            minimal = _items(_ok(await client.get("/prefixes/hot?minimal=true")))

        assert [(item["prefix"], item["hot_ips"], item["capacity"]) for item in items] == expected
        assert items == _expected_hot_list(V6_BLOCK, floors)
        assert [item["prefix"] for item in minimal] == ["2001:db8::/124"]

    async def test_ipv4_entries_come_before_ipv6_entries(self) -> None:
        state = _state(BOTH)
        # Interleaved, so the order cannot come from the order of the writes.
        for v4, v6 in zip(SECTION_42[:16], V6_BLOCK, strict=True):
            _add(state, v6)
            _add(state, v4)
        _make_hot(state, SECTION_42[16:])

        async with _client(_app(state)) as client:
            items = _items(_ok(await client.get("/prefixes/hot")))
            minimal = _items(_ok(await client.get("/prefixes/hot?minimal=true")))

        assert items == _expected_hot_list(SECTION_42 + V6_BLOCK, FLOORS)
        families = [ipaddress.ip_network(item["prefix"]).version for item in items]
        assert families == [4] * 21 + [6] * 4
        assert [item["prefix"] for item in minimal] == [*SECTION_42_MINIMAL, "2001:db8::/124"]


# ==========================================================================
# K. Only prefixes holding a HOT address are listed (assumption 93).
# ==========================================================================


class TestOnlyPrefixesHoldingAHotAddress:
    """Amendment 4, "Test seams": a `DetectionConfig` "may carry a
    `minimum_hot_ips` of 0, which the schema forbids and neither the loader nor
    the model refuses. It shows that only prefixes holding a HOT address are
    listed." Assumption 93: "`GET /prefix/{cidr}` still reports what the
    predicate says"."""

    async def test_a_document_with_no_minimum(self) -> None:
        config = _config(minimum_hot_ips=0, minimum_hot_ratio=0.0)
        state = _state(config=config)
        _add(state, "10.20.30.1")

        async with _client(_app(state)) as client:
            empty = _ok(await client.get("/prefix/192.0.2.0/24"))
            items = _items(_ok(await client.get("/prefixes/hot")))
            minimal = _items(_ok(await client.get("/prefixes/hot?minimal=true")))

        assert empty["state"] == "HOT_PREFIX"
        assert empty["hot_ips"] == 0

        assert [item["prefix"] for item in items] == [
            _ancestor("10.20.30.1", length) for length in range(32, 7, -1)
        ]
        assert len(items) == 25
        assert items == _expected_hot_list(["10.20.30.1"], FLOORS, config)
        for item in items:
            assert item["hot_ips"] == 1
            assert item["state"] == "HOT_PREFIX"
        assert [item["prefix"] for item in minimal] == ["10.20.30.1/32"]


# ==========================================================================
# L. `minimal` (ruling 2, "Parsing `minimal`"; assumption 95).
# ==========================================================================


class TestMinimal:
    @pytest.mark.parametrize(
        "query",
        [
            pytest.param("minimal=True", id="True"),
            pytest.param("minimal=TRUE", id="TRUE"),
            pytest.param("minimal=1", id="1"),
            pytest.param("minimal=yes", id="yes"),
            pytest.param("minimal=", id="empty"),
            pytest.param("minimal", id="no-value"),
            pytest.param("minimal=true&minimal=true", id="true-twice"),
            pytest.param("minimal=false&minimal=true", id="false-then-true"),
            pytest.param(f"minimal={TOKEN}", id="token"),
            pytest.param(f"minimal={TOKEN}&x=1", id="token-and-another"),
        ],
    )
    async def test_anything_but_true_or_false_once_is_refused(self, query: str) -> None:
        response = await _get(_app(_section_42_state()), f"/prefixes/hot?{query}")

        _assert_refused(response, BAD_MINIMAL)

    async def test_other_parameters_are_ignored(self) -> None:
        async with _client(_app(_section_42_state())) as client:
            other = _ok(await client.get(f"/prefixes/hot?foo=bar&{TOKEN}=1"))
            with_minimal = _ok(await client.get("/prefixes/hot?foo=bar&minimal=true"))

        assert len(_items(other)) == 21
        assert _items(other) == _expected_hot_list(SECTION_42, FLOORS)
        assert [item["prefix"] for item in _items(with_minimal)] == SECTION_42_MINIMAL


# ==========================================================================
# M. `prefix_queries` (ruling 9).
# ==========================================================================


def _query_counts(metrics: TrieMetrics) -> dict[tuple[str, str], int | float]:
    return {
        (route, result): metrics.get("prefix_queries", route=route, result=result)
        for route in ROUTES
        for result in RESULTS
    }


async def _assert_counted(
    client: httpx.AsyncClient,
    metrics: TrieMetrics,
    method: str,
    path: str,
    expected: tuple[str, str] | None,
) -> None:
    before = _query_counts(metrics)
    await client.request(method, path)
    after = _query_counts(metrics)
    moved = {key: after[key] - before[key] for key in after if after[key] != before[key]}
    assert moved == ({} if expected is None else {expected: 1}), (method, path)


READY_CASES = [
    pytest.param("GET", "/prefix/10.20.30.0/24", ("prefix", "ok"), id="prefix-ok"),
    pytest.param("GET", "/prefix/192.0.2.0/24", ("prefix", "ok"), id="prefix-ok-no-node"),
    pytest.param("GET", f"/prefix/{TOKEN}", ("prefix", "invalid"), id="prefix-malformed"),
    pytest.param("GET", "/prefix/10.20.30.0/33", ("prefix", "invalid"), id="prefix-range"),
    pytest.param("GET", "/prefix/10.20.30.1/24", ("prefix", "invalid"), id="prefix-host-bits"),
    pytest.param(
        "GET", "/prefix/2001:db8::/32", ("prefix", "family_not_served"), id="prefix-family"
    ),
    pytest.param("GET", "/ip/10.20.30.1", ("ip", "ok"), id="ip-ok-hot"),
    pytest.param("GET", "/ip/10.20.30.200", ("ip", "ok"), id="ip-ok-cold"),
    pytest.param("GET", f"/ip/{TOKEN}", ("ip", "invalid"), id="ip-malformed"),
    pytest.param("GET", "/ip/2001:db8::1", ("ip", "family_not_served"), id="ip-family"),
    pytest.param("GET", "/prefixes/hot", ("prefixes_hot", "ok"), id="hot-ok"),
    pytest.param("GET", "/prefixes/hot?minimal=true", ("prefixes_hot", "ok"), id="hot-minimal"),
    pytest.param(
        "GET", f"/prefixes/hot?minimal={TOKEN}", ("prefixes_hot", "invalid"), id="hot-invalid"
    ),
    pytest.param("GET", "/healthz", None, id="healthz"),
    pytest.param("GET", "/readyz", None, id="readyz"),
    pytest.param("GET", "/metrics", None, id="metrics"),
    pytest.param("GET", "/openapi.json", None, id="documentation-404"),
    pytest.param("GET", "/no/such/path", None, id="unknown-404"),
    pytest.param("POST", "/prefix/10.0.0.0/8", None, id="post-prefix-405"),
    pytest.param("POST", "/ip/10.20.30.1", None, id="post-ip-405"),
    pytest.param("POST", "/prefixes/hot", None, id="post-hot-405"),
]

NOT_READY_CASES = [
    pytest.param("/prefix/10.20.30.0/24", "prefix", id="prefix"),
    pytest.param(f"/prefix/{TOKEN}", "prefix", id="prefix-malformed"),
    pytest.param("/prefix/2001:db8::/32", "prefix", id="prefix-family"),
    pytest.param("/ip/10.20.30.1", "ip", id="ip"),
    pytest.param(f"/ip/{TOKEN}", "ip", id="ip-malformed"),
    pytest.param("/prefixes/hot", "prefixes_hot", id="hot"),
    pytest.param(f"/prefixes/hot?minimal={TOKEN}", "prefixes_hot", id="hot-invalid"),
]


class TestPrefixQueries:
    """Ruling 9: "`prefix_queries` | counter | `route` (`prefix`, `ip`,
    `prefixes_hot`), `result` (`ok`, `invalid`, `family_not_served`,
    `not_ready`) | ... the requests a read route answered, each counted once.
    `ok` is a `200`; `family_not_served` the `400` whose text is `address
    family not served`; `invalid` any other `400`; `not_ready` a `503`." "Not
    counted: a request the framework answers (a `404` or `405`), a `500`, and
    the admin routes." """

    @pytest.mark.parametrize(("method", "path", "expected"), READY_CASES)
    async def test_while_ready(
        self, method: str, path: str, expected: tuple[str, str] | None
    ) -> None:
        state = _section_42_state()
        metrics = _metrics(state)

        async with _client(_app(state, metrics=metrics)) as client:
            await _assert_counted(client, metrics, method, path, expected)
            # And again: each request is one more.
            await _assert_counted(client, metrics, method, path, expected)

    @pytest.mark.parametrize(("path", "route"), NOT_READY_CASES)
    async def test_while_not_ready(self, path: str, route: str) -> None:
        state = _section_42_state()
        metrics = _metrics(state)
        readiness = Readiness()

        async with _client(_app(state, readiness=readiness, metrics=metrics)) as client:
            await _assert_counted(client, metrics, "GET", path, (route, "not_ready"))
            await _assert_counted(client, metrics, "GET", "/readyz", None)
            readiness.mark_ready()
            readiness.mark_stopping()
            await _assert_counted(client, metrics, "GET", path, (route, "not_ready"))

        assert metrics.get("prefix_queries", route=route, result="not_ready") == 2


# ==========================================================================
# N. What the framework answers.
# ==========================================================================


class TestFrameworkAnswers:
    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/prefix/10.0.0.0/8", id="prefix"),
            pytest.param("/ip/10.20.30.1", id="ip"),
            pytest.param("/prefixes/hot", id="prefixes-hot"),
        ],
    )
    async def test_another_method_is_405(self, path: str) -> None:
        # Ruling 2: "Another method on their paths answers FastAPI's 405".
        async with _client(_app(_section_42_state())) as client:
            response = await client.post(path)

        assert response.status_code == 405

    async def test_no_generated_documentation(self) -> None:
        # Ruling 8: `create_app` keeps decision 13's three `None` URLs, so
        # these four paths answer 404 as any undeclared path does.
        async with _client(_app(_section_42_state())) as client:
            for path in GENERATED_DOCUMENTATION_PATHS:
                response = await client.get(path)
                assert response.status_code == 404, path
                body = response.text.lower()
                assert "swagger" not in body, path
                assert "redoc" not in body, path


# ==========================================================================
# O. `create_app`'s checks (ruling 8).
# ==========================================================================


class TestCreateApp:
    """Ruling 8: "`min_prefix_lengths` holds an entry for each family `state`
    serves, an integer from 0 to that family's `bit_length`. Else `create_app`
    raises `ValueError`. It keeps a copy." Assumption 105: an entry is required
    "only for the families served"."""

    @pytest.mark.parametrize(
        ("families", "floors"),
        [
            pytest.param(ONLY_V4, {}, id="v4-no-floor"),
            pytest.param(ONLY_V4, {IPV6: 104}, id="v4-only-an-ipv6-floor"),
            pytest.param(BOTH, {IPV4: 8}, id="both-no-ipv6-floor"),
            pytest.param(BOTH, {IPV6: 104}, id="both-no-ipv4-floor"),
            pytest.param(ONLY_V4, {IPV4: 33}, id="ipv4-33"),
            pytest.param(ONLY_V4, {IPV4: -1}, id="ipv4-negative"),
            pytest.param(BOTH, {IPV4: 8, IPV6: 129}, id="ipv6-129"),
            pytest.param(BOTH, {IPV4: 8, IPV6: -1}, id="ipv6-negative"),
        ],
    )
    def test_a_missing_or_out_of_range_floor_is_refused(
        self, families: frozenset[AddressFamily], floors: dict[AddressFamily, int]
    ) -> None:
        state = _state(families)

        with pytest.raises(ValueError):
            create_app(Readiness(), state=state, metrics=_metrics(state), min_prefix_lengths=floors)

    @pytest.mark.parametrize(
        ("families", "floors"),
        [
            pytest.param(ONLY_V4, {IPV4: 0}, id="ipv4-0"),
            pytest.param(ONLY_V4, {IPV4: 32}, id="ipv4-32"),
            pytest.param(ONLY_V4, {IPV4: 8, IPV6: 104}, id="v4-only-both-floors"),
            pytest.param(BOTH, {IPV4: 32, IPV6: 0}, id="ipv6-0"),
            pytest.param(BOTH, {IPV4: 0, IPV6: 128}, id="ipv6-128"),
        ],
    )
    async def test_floors_at_either_end_are_accepted(
        self, families: frozenset[AddressFamily], floors: dict[AddressFamily, int]
    ) -> None:
        state = _state(families)
        _make_hot(state, SECTION_42)

        items = _items(_ok(await _get(_app(state, floors=floors), "/prefixes/hot")))

        assert items == _expected_hot_list(SECTION_42, floors)

    async def test_it_keeps_a_copy_of_the_floors(self) -> None:
        floors = {IPV4: 8}
        app = _app(_section_42_state(), floors=floors)
        floors[IPV4] = 24

        items = _items(_ok(await _get(app, "/prefixes/hot")))

        assert len(items) == 21
        assert items[-1]["prefix"] == "10.20.28.0/22"


# ==========================================================================
# P. Through the service (ruling 8; "Test seams", "The service").
# ==========================================================================

# `docs/spec/integration-scenarios.md` section 2.1, identical to
# `config/detection.v1.json`.
_DOCUMENT: dict[str, Any] = {
    "config_version": 1,
    "window_seconds": 300,
    "bucket_seconds": 10,
    "hot_threshold": 1000,
    "cold_threshold": 800,
    "minimum_hot_ips": 16,
    "minimum_hot_ratio": 0.10,
    "allowed_lateness_seconds": 30,
    "state_retention_seconds": 600,
    "weight_function": "threshold_ratio",
    "weight_max": 1000000,
}


def _envelope(text: str, n: int, window_count: int) -> EventEnvelope[HotIpAdded]:
    ip = Address.parse(text)
    ts = T0 + timedelta(seconds=n)
    return EventEnvelope(
        agent_id="aggregator-shard-0",
        sequence=n + 1,
        event_type="HotIpAdded",
        config_version=1,
        timestamp=ts,
        subject=str(ip),
        payload=HotIpAdded(
            ip=ip, timestamp=ts, sequence=n + 1, window_count=window_count, config_version=1
        ),
    )


async def _publish(bus: InMemoryBus, envelope: EventEnvelope[HotIpAdded]) -> None:
    await bus.producer().publish(
        TOPIC,
        key=str(envelope.payload.ip),
        value=encode(envelope),
        message_id=envelope.event_id,
    )


class TestThroughTheService:
    """Ruling 8: "`TrieService` builds its app with `state=worker.state`,
    `metrics=worker.metrics` and the floors it passes the worker". "Test seams":
    "`build_service`'s floors reach the app as they reach the worker". Ruling
    3: "A request in flight when `stop()` begins sees `stopping`". Decision 8:
    on the memory log `event_sequence` "equals the number of records the trie
    has handled"."""

    async def test_the_replayed_state_is_served(self, tmp_path: Path) -> None:
        path = tmp_path / "detection.json"
        path.write_text(json.dumps(_DOCUMENT))
        bus = InMemoryBus()
        v4 = [f"10.20.30.{i}" for i in range(16, 32)]
        hot = v4 + V6_BLOCK
        counts: dict[str, int] = {}
        for n, text in enumerate(hot):
            counts[text] = 1000 + n
            await _publish(bus, _envelope(text, n, counts[text]))
        settings = TrieSettings(
            host="127.0.0.1",
            port=0,
            detection_config_path=path,
            bus_kind="memory",
            bus_brokers="nats://localhost:4222",
            config_poll_interval_s=AN_HOUR,
            families=BOTH,
            min_prefix_length_ipv6=122,
        )
        service = build_service(settings, bus=bus)
        floors = {IPV4: 8, IPV6: 122}

        async with _client(service.app) as client:
            before = await client.get("/prefixes/hot")
            assert before.status_code == 503
            assert before.content == STARTING

            await service.start()
            try:
                for text in hot:
                    body = _ok(await client.get(f"/ip/{text}"))
                    assert body["ip"] == str(ipaddress.ip_address(text)), text
                    assert body["state"] == "HOT", text
                    assert body["request_count"] == counts[text], text
                    assert body["attributes"] == {"attributes_version": 1}, text

                listing = _ok(await client.get("/prefixes/hot"))
                items = _items(listing)
                assert items == _expected_hot_list(hot, floors)
                v6_prefixes = [item["prefix"] for item in items if ":" in item["prefix"]]
                assert v6_prefixes == ["2001:db8::/124", "2001:db8::/123", "2001:db8::/122"]

                records = await bus.end_offset(TOPIC)
                assert records == len(hot)
                assert listing["event_sequence"] == records == service.state.event_sequence
                assert listing["as_of"] == _render(T0 + timedelta(seconds=len(hot) - 1))
                assert listing["config_version"] == 1

                assert service.metrics.get("prefix_queries", route="ip", result="ok") == len(hot)
            finally:
                await service.stop()

            for read in ("/prefix/10.20.30.16/28", "/ip/10.20.30.16", "/prefixes/hot"):
                response = await client.get(read)
                assert response.status_code == 503, read
                assert response.content == STOPPING, read
