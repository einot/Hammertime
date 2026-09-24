"""The `PrefixStatsChanged` publisher on its own: `prepare()` and `publish()`.

Spec: section 3 (a prefix's `hot_count`, `capacity` and `hot_ratio`), section
13 (the stats the detector's predicate reads), section 19 (the trie publishes
`PrefixStatsChanged`), section 22 (`sequence` is the trie's `event_sequence`),
section 35 (one reporting range per family), section 37 (the counter).

Written from ADR-0017 Amendment 2 ruling 2 (`hammertime.trie.publisher`),
ruling 1 (the per-family reporting range: IPv4 `[8, 32]`, IPv6 `[104, 128]` by
default), ruling 3 ("Why two calls": a write to the trie after `prepare()`
returns does not change what `publish()` sends), ruling 7 (the counter
`prefix_stats_published{family}`), ruling 9 (`hot_ratio`) and the amendment's
"Test seams" ("The publisher on its own": a producer double and a
`TrieMetrics`; `prepare()` needs only a `PatriciaTrie(family)` with addresses
added through `add_hot_ip`), with ADR-0017 decision 14 items 2-4 (one message
per `ancestor_stats` entry, zero counts included; the envelope; key and
message id) and ADR-0015 decision 2 (`ancestor_stats`, ascending length).

Choices of this file's own:

* Expected prefix texts come from the standard library's `ipaddress`
  (`ip_network(f"{ip}/{length}", strict=False)`), which writes RFC 5952's
  canonical IPv6 form such as `2001:db8::/104`; expected counts from
  `trie.hot_count(Prefix.parse(text))`; expected ratios from
  `hot_count / capacity`, which CPython rounds correctly for two ints, as
  `Prefix.hot_ratio` narrows its exact `Fraction` once (ADR-0015 decision 2).
* The `TrieMetrics` is bound to a `TrieState` before any read, because
  ADR-0017 decision 12 has `get` return `0` for "any series read before
  `bind_state`".
* The producer double implements `publish(topic, key, value, *,
  message_id=None)` and `flush()`: whether `key` and `value` arrive by
  position or by keyword is not pinned (Amendment 2, "Test seams"). A publish
  can be held until the test releases it, or made to raise for a chosen key.
* Waiting is `asyncio.sleep(0)` in a bounded loop, and `asyncio.wait_for(...,
  10.0)` is only a failure bound. "Has not returned yet" is checked after a
  fixed number of further yields.
* Ruling 2 does not pin when the counter moves while publishes are held, so
  it is read only once `publish()` has returned or raised.
"""

import asyncio
import ipaddress
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple, cast

import pytest
from hammertime.bus.interface import Producer
from hammertime.bus.topics import PREFIX_STATS
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import decode
from hammertime.core.events.models import PrefixStatsChanged
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.publisher import (
    AGENT_ID,
    DEFAULT_MIN_PREFIX_LENGTHS,
    PrefixStatsPublisher,
    PrefixStatsPublishError,
    PreparedStats,
    StatsMessage,
)
from hammertime.trie.state import TrieState
from hammertime.trie.structure import PatriciaTrie

IPV4 = AddressFamily.IPV4
IPV6 = AddressFamily.IPV6

T0 = datetime.fromtimestamp(1_800_000_000, tz=UTC)
SEQUENCE = 42
CONFIG_VERSION = 3

IP = Address.parse("10.20.30.1")
# With `IP`, these make the counts differ along its path: 4 under 10.0.0.0/8,
# 3 under 10.20.0.0/16, 2 under 10.20.30.0/24, 1 at 10.20.30.0/31 and below.
# 11.0.0.1 is outside every reported prefix.
NEIGHBOURS = tuple(
    Address.parse(text) for text in ("10.20.30.2", "10.20.31.5", "10.99.0.1", "11.0.0.1")
)
IP_V6 = Address.parse("2001:db8::1")

MARKER = "S3CR3T-PUBLISH-MARKER"
STEPS = 10_000


def _config() -> DetectionConfig:
    return DetectionConfig(
        config_version=CONFIG_VERSION,
        window_seconds=300,
        bucket_seconds=10,
        hot_threshold=1000,
        cold_threshold=800,
        allowed_lateness_seconds=30,
        state_retention_seconds=600,
    )


def _metrics() -> TrieMetrics:
    """A `TrieMetrics` bound to a state serving both families (decision 12)."""

    metrics = TrieMetrics()
    metrics.bind_state(TrieState(families=frozenset({IPV4, IPV6}), config=_config()))
    return metrics


def _published(metrics: TrieMetrics, family: str = "ipv4") -> int | float:
    return metrics.get("prefix_stats_published", family=family)


class _Call(NamedTuple):
    topic: object
    key: object
    value: object
    message_id: object


class _Producer:
    """A producer double (Amendment 2, "Test seams").

    Records every `publish` call. A publish whose key `hold(key)` accepts waits
    until `release` is set; then, if its key is in `fail`, it raises that
    exception. `flush()` counts its calls."""

    def __init__(
        self,
        *,
        hold: Callable[[object], bool] | None = None,
        fail: Mapping[object, BaseException] | None = None,
    ) -> None:
        self.calls: list[_Call] = []
        self.release = asyncio.Event()
        self.in_flight = 0
        self.flushes = 0
        self._hold = hold if hold is not None else (lambda key: False)
        self._fail = dict(fail) if fail is not None else {}

    async def publish(
        self, topic: object, key: object, value: object, *, message_id: object = None
    ) -> None:
        self.calls.append(_Call(topic, key, value, message_id))
        if self._hold(key):
            self.in_flight += 1
            try:
                await self.release.wait()
            finally:
                self.in_flight -= 1
        error = self._fail.get(key)
        if error is not None:
            raise error

    async def flush(self) -> None:
        self.flushes += 1


class _PublishFailed(Exception):
    """Raised by `_Producer.publish` for a chosen key; no other code raises it."""


class _Halt(BaseException):
    """A `BaseException` that is not an `Exception`, and neither
    `KeyboardInterrupt` nor `SystemExit`, which asyncio treats specially."""


def _publisher(
    producer: _Producer | None = None,
    *,
    metrics: TrieMetrics | None = None,
    lengths: Mapping[AddressFamily, int] | None = None,
) -> PrefixStatsPublisher:
    wire = cast(Producer, producer if producer is not None else _Producer())
    bound = metrics if metrics is not None else _metrics()
    if lengths is None:
        return PrefixStatsPublisher(wire, metrics=bound)
    return PrefixStatsPublisher(wire, metrics=bound, min_prefix_lengths=lengths)


def _trie(*addresses: Address) -> PatriciaTrie:
    trie = PatriciaTrie(addresses[0].family)
    for address in addresses:
        assert trie.add_hot_ip(address) is True
    return trie


def _prepare(
    publisher: PrefixStatsPublisher, trie: PatriciaTrie, ip: Address, *, sequence: int = SEQUENCE
) -> PreparedStats:
    return publisher.prepare(
        trie, ip, sequence=sequence, config_version=CONFIG_VERSION, timestamp=T0
    )


def _ancestor_text(ip: Address, length: int) -> str:
    """The canonical CIDR text of `ip`'s ancestor at `length`."""

    return str(ipaddress.ip_network(f"{ip}/{length}", strict=False))


def _payload(message: StatsMessage) -> Any:
    envelope: Any = decode(message.value)
    assert isinstance(envelope.payload, PrefixStatsChanged)
    return envelope.payload


def _assert_messages(prepared: PreparedStats, trie: PatriciaTrie, ip: Address, floor: int) -> None:
    """Ruling 2's message for every length from `floor` to the host route, in
    ascending order (ADR-0015 decision 2), each decoded and checked field by
    field (decision 14 items 3 and 4)."""

    bits = ip.family.bit_length
    lengths = list(range(floor, bits + 1))
    assert len(prepared.messages) == len(lengths)
    for message, length in zip(prepared.messages, lengths, strict=True):
        assert isinstance(message, StatsMessage)
        envelope: Any = decode(message.value)
        payload = envelope.payload
        text = _ancestor_text(ip, length)
        capacity = 2 ** (bits - length)
        hot_count = trie.hot_count(Prefix.parse(text))

        assert isinstance(payload, PrefixStatsChanged)
        assert envelope.agent_id == "trie-primary"
        assert envelope.event_type == "PrefixStatsChanged"
        assert envelope.sequence == SEQUENCE
        assert payload.sequence == SEQUENCE
        assert envelope.config_version == CONFIG_VERSION
        assert envelope.timestamp == T0
        assert payload.timestamp == T0
        assert envelope.subject == payload.prefix == message.key == text
        assert message.message_id == envelope.event_id
        assert payload.hot_count == hot_count
        assert payload.capacity == capacity
        assert payload.hot_ratio == hot_count / capacity
    assert len({message.message_id for message in prepared.messages}) == len(lengths)


async def _yield_until(predicate: Callable[[], bool], *, steps: int = STEPS) -> None:
    """Yield to the event loop until `predicate` holds. Never waits on the wall clock."""

    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was never reached")


async def _spin(steps: int = 200) -> None:
    for _ in range(steps):
        await asyncio.sleep(0)


class TestConstants:
    def test_the_agent_id(self) -> None:
        # Decision 14 item 3: "`agent_id="trie-primary"`".
        assert AGENT_ID == "trie-primary"

    def test_the_default_lengths_are_8_and_104_and_read_only(self) -> None:
        # Ruling 1's table; ruling 2: "read-only: {IPV4: 8, IPV6: 104}".
        assert dict(DEFAULT_MIN_PREFIX_LENGTHS) == {IPV4: 8, IPV6: 104}

        with pytest.raises(TypeError):
            cast(Any, DEFAULT_MIN_PREFIX_LENGTHS)[IPV4] = 9

        assert dict(DEFAULT_MIN_PREFIX_LENGTHS) == {IPV4: 8, IPV6: 104}


class TestConstructor:
    """Ruling 2: the constructor "copies `min_prefix_lengths`. That mapping must
    hold an entry for each `AddressFamily`, an `int` from 0 to that family's
    bit length. A missing family, or a length out of range, is a
    `ValueError`." Assumption 59: "The lengths mapping must name both
    families." """

    @pytest.mark.parametrize(
        "lengths",
        [
            pytest.param({IPV4: 8}, id="no-ipv6"),
            pytest.param({IPV6: 104}, id="no-ipv4"),
            pytest.param({}, id="empty"),
        ],
    )
    def test_a_mapping_without_a_family_is_a_value_error(
        self, lengths: dict[AddressFamily, int]
    ) -> None:
        with pytest.raises(ValueError):
            _publisher(lengths=lengths)

    @pytest.mark.parametrize(
        "lengths",
        [
            pytest.param({IPV4: -1, IPV6: 104}, id="ipv4-minus-1"),
            pytest.param({IPV4: 33, IPV6: 104}, id="ipv4-33"),
            pytest.param({IPV4: 8, IPV6: -1}, id="ipv6-minus-1"),
            pytest.param({IPV4: 8, IPV6: 129}, id="ipv6-129"),
        ],
    )
    def test_a_length_out_of_range_is_a_value_error(
        self, lengths: dict[AddressFamily, int]
    ) -> None:
        with pytest.raises(ValueError):
            _publisher(lengths=lengths)

    @pytest.mark.parametrize(
        "lengths",
        [
            pytest.param({IPV4: 0, IPV6: 0}, id="zero"),
            pytest.param({IPV4: 32, IPV6: 128}, id="host-route"),
            pytest.param({IPV4: 24, IPV6: 64}, id="inside"),
        ],
    )
    def test_the_bounds_are_accepted_and_read_back(self, lengths: dict[AddressFamily, int]) -> None:
        publisher = _publisher(lengths=lengths)

        assert publisher.min_prefix_length(IPV4) == lengths[IPV4]
        assert publisher.min_prefix_length(IPV6) == lengths[IPV6]

    def test_the_defaults_are_read_back(self) -> None:
        publisher = _publisher()

        assert publisher.min_prefix_length(IPV4) == 8
        assert publisher.min_prefix_length(IPV6) == 104

    def test_the_mapping_is_copied(self) -> None:
        lengths = {IPV4: 24, IPV6: 104}
        publisher = _publisher(lengths=lengths)

        lengths[IPV4] = 30
        del lengths[IPV6]

        assert publisher.min_prefix_length(IPV4) == 24
        assert publisher.min_prefix_length(IPV6) == 104
        assert len(_prepare(publisher, _trie(IP), IP).messages) == 9


class TestPrepareIPv4AtTheDefaults:
    """Ruling 2, `prepare()`: "With `L = min_prefix_length(trie.family)`, it
    makes one `StatsMessage` for each entry `s` of `ancestor_stats(trie, ip,
    min_length=L)` ..., zero counts included, in that order", with the payload
    and envelope ruling 2 gives; decision 14 item 2: "25 for IPv4 with `L =
    8`"."""

    def test_25_messages_from_slash_8_to_slash_32(self) -> None:
        trie = _trie(IP, *NEIGHBOURS)

        prepared = _prepare(_publisher(), trie, IP)

        _assert_messages(prepared, trie, IP, 8)
        # Non-vacuous: the counts differ along the path.
        counts = [_payload(message).hot_count for message in prepared.messages]
        assert counts[0] == 4
        assert counts[-1] == 1
        assert len(set(counts)) >= 3

    def test_family_and_sequence(self) -> None:
        prepared = _prepare(_publisher(), _trie(IP), IP, sequence=7)

        assert isinstance(prepared, PreparedStats)
        assert prepared.family is IPV4
        assert prepared.sequence == 7

    def test_an_address_no_longer_hot_gives_every_length_with_zero_counts(self) -> None:
        trie = _trie(IP)
        assert trie.remove_hot_ip(IP) is True

        prepared = _prepare(_publisher(), trie, IP)

        _assert_messages(prepared, trie, IP, 8)
        for message in prepared.messages:
            payload = _payload(message)
            assert payload.hot_count == 0
            assert type(payload.hot_ratio) is float
            assert payload.hot_ratio == 0.0


class TestPrepareAtOtherFloors:
    """Ruling 1: IPv4 reports `[L4, 32]`, IPv6 `[L6, 128]`; ruling 9: "the
    smallest non-zero one, 2^-128 at an IPv6 `/0`, is a normal double"."""

    @pytest.mark.parametrize(
        ("floor", "count"),
        [
            pytest.param(24, 9, id="24"),
            pytest.param(32, 1, id="32"),
            pytest.param(0, 33, id="0"),
        ],
    )
    def test_ipv4(self, floor: int, count: int) -> None:
        trie = _trie(IP, *NEIGHBOURS)

        prepared = _prepare(_publisher(lengths={IPV4: floor, IPV6: 104}), trie, IP)

        assert len(prepared.messages) == count
        _assert_messages(prepared, trie, IP, floor)

    def test_ipv4_slash_0_has_capacity_2_to_the_32(self) -> None:
        trie = _trie(IP, *NEIGHBOURS)

        prepared = _prepare(_publisher(lengths={IPV4: 0, IPV6: 104}), trie, IP)

        root = _payload(prepared.messages[0])
        assert root.prefix == "0.0.0.0/0"
        assert root.capacity == 2**32
        assert root.hot_count == 5

    def test_ipv6_at_the_default_gives_slash_104_to_slash_128(self) -> None:
        trie = _trie(IP_V6)

        prepared = _prepare(_publisher(), trie, IP_V6)

        assert prepared.family is IPV6
        assert len(prepared.messages) == 25
        _assert_messages(prepared, trie, IP_V6, 104)
        assert prepared.messages[0].key == "2001:db8::/104"
        assert prepared.messages[-1].key == "2001:db8::1/128"

    def test_ipv6_at_floor_0_gives_129_and_a_ratio_of_2_to_the_minus_128(self) -> None:
        trie = _trie(IP_V6)

        prepared = _prepare(_publisher(lengths={IPV4: 8, IPV6: 0}), trie, IP_V6)

        assert len(prepared.messages) == 129
        _assert_messages(prepared, trie, IP_V6, 0)
        root = _payload(prepared.messages[0])
        assert root.prefix == "::/0"
        assert root.capacity == 2**128
        assert root.hot_count == 1
        assert root.hot_ratio == 2**-128


class TestPrepareInIsolation:
    def test_it_calls_no_producer_method(self) -> None:
        # Ruling 2: "`prepare(...)` is synchronous and calls no producer."
        producer = _Producer()
        publisher = _publisher(producer)

        _prepare(publisher, _trie(IP, *NEIGHBOURS), IP)

        assert producer.calls == []
        assert producer.flushes == 0

    def test_the_same_arguments_give_equal_results(self) -> None:
        # Ruling 2: "The same arguments over the same trie give the same
        # messages."
        publisher = _publisher()
        trie = _trie(IP, *NEIGHBOURS)

        assert _prepare(publisher, trie, IP) == _prepare(publisher, trie, IP)

    async def test_a_later_change_to_the_trie_does_not_change_what_is_sent(self) -> None:
        # Ruling 3, "Why two calls": "a write to the trie after `prepare()`
        # returns does not change what `publish()` sends."
        producer = _Producer()
        publisher = _publisher(producer)
        trie = _trie(IP)
        prepared = _prepare(publisher, trie, IP)
        slash_24 = _ancestor_text(IP, 24)

        assert trie.add_hot_ip(NEIGHBOURS[0]) is True
        assert trie.hot_count(Prefix.parse(slash_24)) == 2
        await publisher.publish(prepared)

        expected = [
            _Call(PREFIX_STATS.name, m.key, m.value, m.message_id) for m in prepared.messages
        ]
        assert Counter(producer.calls) == Counter(expected)
        sent = [call for call in producer.calls if call.key == slash_24]
        assert len(sent) == 1
        envelope: Any = decode(cast(bytes, sent[0].value))
        assert envelope.payload.hot_count == 1

    @pytest.mark.parametrize(
        ("family", "ip"),
        [
            pytest.param(IPV4, IP_V6, id="ipv6-address-on-ipv4-trie"),
            pytest.param(IPV6, IP, id="ipv4-address-on-ipv6-trie"),
        ],
    )
    def test_an_address_of_the_other_family_is_a_value_error(
        self, family: AddressFamily, ip: Address
    ) -> None:
        # Ruling 2: "A `ValueError` from `ancestor_stats` (an address of the
        # other family) ... propagate[s]."
        with pytest.raises(ValueError):
            _prepare(_publisher(), PatriciaTrie(family), ip)


class TestPublishSucceeds:
    """Ruling 2, `publish(prepared)`: it "starts `producer.publish(
    PREFIX_STATS.name, key, value, message_id=message_id)` for every message
    before it awaits any of them. It returns only once every one of them has
    returned or raised ... For each publish that returned, it counts
    `prefix_stats_published` once, with `family=prepared.family`."""

    async def test_every_message_is_published_once_with_its_key_value_and_id(self) -> None:
        producer = _Producer()
        metrics = _metrics()
        publisher = _publisher(producer, metrics=metrics)
        prepared = _prepare(publisher, _trie(IP, *NEIGHBOURS), IP)

        await publisher.publish(prepared)

        expected = [
            _Call(PREFIX_STATS.name, m.key, m.value, m.message_id) for m in prepared.messages
        ]
        assert PREFIX_STATS.name == "hammertime.prefix-stats.v1"
        assert Counter(producer.calls) == Counter(expected)
        assert len(producer.calls) == 25
        assert _published(metrics) == 25
        assert _published(metrics, "ipv6") == 0

    async def test_every_publish_is_in_flight_before_any_is_released(self) -> None:
        # Sequential publishing would stop at one call in flight, and the
        # bounded loop would fail.
        producer = _Producer(hold=lambda key: True)
        metrics = _metrics()
        publisher = _publisher(producer, metrics=metrics)
        prepared = _prepare(publisher, _trie(IP), IP)

        task = asyncio.create_task(publisher.publish(prepared))
        try:
            await _yield_until(lambda: producer.in_flight == 25)
            assert len(producer.calls) == 25
            assert not task.done()
        finally:
            producer.release.set()
        await asyncio.wait_for(task, timeout=10.0)

        assert _published(metrics) == 25

    async def test_it_does_not_return_until_every_held_publish_is_released(self) -> None:
        last = _ancestor_text(IP, 32)
        producer = _Producer(hold=lambda key: key == last)
        metrics = _metrics()
        publisher = _publisher(producer, metrics=metrics)
        prepared = _prepare(publisher, _trie(IP), IP)

        task = asyncio.create_task(publisher.publish(prepared))
        try:
            await _yield_until(lambda: len(producer.calls) == 25 and producer.in_flight == 1)
            await _spin()
            assert not task.done()
        finally:
            producer.release.set()
        await asyncio.wait_for(task, timeout=10.0)

        assert _published(metrics) == 25

    async def test_an_ipv6_event_is_counted_under_ipv6(self) -> None:
        producer = _Producer()
        metrics = _metrics()
        publisher = _publisher(producer, metrics=metrics)

        await publisher.publish(_prepare(publisher, _trie(IP_V6), IP_V6))

        assert _published(metrics, "ipv6") == 25
        assert _published(metrics, "ipv4") == 0


class TestPublishFails:
    """Ruling 2: "If any publish raised an `Exception`, `publish()` then raises
    `PrefixStatsPublishError(sequence=prepared.sequence, attempted=<the number
    of messages>, failed=<the number that raised>)`. It raises it from the
    exception of the first failed message in `messages` order, which is
    therefore its `__cause__`. The error's text carries those three numbers and
    nothing else of the event: no prefix, and not the cause's text." It returns
    only once every publish has returned or raised; the counter counts those
    that returned."""

    async def test_two_failures_among_25(self) -> None:
        slash_24 = _ancestor_text(IP, 24)
        slash_30 = _ancestor_text(IP, 30)
        held = _ancestor_text(IP, 32)
        first_error = _PublishFailed(f"{MARKER}-24")
        second_error = _PublishFailed(f"{MARKER}-30")
        producer = _Producer(
            hold=lambda key: key == held, fail={slash_24: first_error, slash_30: second_error}
        )
        metrics = _metrics()
        publisher = _publisher(producer, metrics=metrics)
        prepared = _prepare(publisher, _trie(IP, *NEIGHBOURS), IP)

        task = asyncio.create_task(publisher.publish(prepared))
        try:
            await _yield_until(lambda: len(producer.calls) == 25 and producer.in_flight == 1)
            await _spin()
            # The two failures have raised; the held publish has not returned.
            assert not task.done()
        finally:
            producer.release.set()
        with pytest.raises(PrefixStatsPublishError) as excinfo:
            await asyncio.wait_for(task, timeout=10.0)

        error = excinfo.value
        assert error.sequence == SEQUENCE
        assert error.attempted == 25
        assert error.failed == 2
        assert error.__cause__ is first_error
        assert len(producer.calls) == 25
        assert _published(metrics) == 23
        text = str(error)
        assert MARKER not in text
        for message in prepared.messages:
            assert message.key not in text

    async def test_a_base_exception_that_is_not_an_exception_is_raised_as_it_is(self) -> None:
        # Ruling 2: "A publish that raises a `BaseException` that is not an
        # `Exception`, such as a cancellation, makes `publish()` raise that
        # same exception."
        halt = _Halt()
        producer = _Producer(fail={_ancestor_text(IP, 24): halt})
        publisher = _publisher(producer)
        prepared = _prepare(publisher, _trie(IP), IP)

        with pytest.raises(_Halt) as excinfo:
            await asyncio.wait_for(publisher.publish(prepared), timeout=10.0)

        assert excinfo.value is halt
        assert not isinstance(excinfo.value, PrefixStatsPublishError)


class TestFlush:
    async def test_each_call_awaits_the_producers_flush_once(self) -> None:
        # Ruling 2: "`flush()` awaits `producer.flush()`."
        producer = _Producer()
        publisher = _publisher(producer)

        await publisher.flush()
        assert producer.flushes == 1

        await publisher.flush()
        assert producer.flushes == 2
        assert producer.calls == []
