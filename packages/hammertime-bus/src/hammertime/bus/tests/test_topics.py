"""Topic registry: names, key selection, and lookup (spec section 20, section 33).

`topics.py` is currently a docstring-only stub ("Topic names, partition
counts, retention, and key selection per topic."), so this file also
defines the assumed public surface:

    TOPICS: Mapping[str, TopicConfig]

where each `TopicConfig` exposes at least a `.key_selector` callable of
`(event) -> str`, keyed by topic name. `TOPICS[name]` is the "lookup by
name" mechanism; an unknown name is assumed to raise the idiomatic
`KeyError` a plain mapping would raise -- the task instructions explicitly
call out that the exact exception type isn't pinned anywhere in spec/ADRs,
so `KeyError` is a documented assumption, not a hard requirement; adjust
`TestLookupByName.test_unknown_topic_name_raises` if `topics.py` instead
defines/raises a dedicated exception type.

Topic *name* assumptions, in decreasing order of confidence:

* `"hammertime.hot-ip.v1"` is a hard, pinned literal -- not something these
  tests infer, but something the task handed down directly, corroborated
  by `Makefile`'s `replay` target, `.env.example`'s
  `HAMMERTIME_TOPIC_HOT_IP`, and `docs/runbook.md`'s restart procedure all
  hard-referencing that exact string. Consumers outside this repo may
  depend on it, so it is asserted with exact string equality.
* The observations / observations-reconciliation (ADR-0002) / prefix-stats
  topics have no equivalent pinned literal anywhere in spec/ADRs/schemas/
  ops docs. Rather than guess an exact string (e.g. whether it's
  `hammertime.observations.v1` or something else, or whether the
  reconciliation topic is `...observations-reconciliation.v1` vs
  `...observations.reconciliation.v1`), those three are asserted for
  *presence* via substring matching on topic names in `TOPICS`, which is
  robust to the exact naming convention while still enforcing that the
  registry has the three required topic roles.

Key-selector assumption: `key_selector` is called with one of the
`hammertime.core.events.models` payload dataclasses (this package already
depends on `hammertime-core`, so there's no layering problem). For the
per-IP-sharded topics (hot-ip, observations -- section 20 shard ownership
is defined per-IP, and `Observation` -- "One (ip, delta) pair inside an
agent message" -- is the atomic per-IP unit, as opposed to the
multi-IP-batched `RequestObservation`), the selector's return value is
asserted to equal `str(event.ip)`; for prefix-stats (ADR-0001 single
writer) it's asserted to equal `event.prefix` directly. The exact string
*format* of the derived key is a secondary assumption flagged separately
from the more important invariant also checked here: same IP/prefix always
selects the same key, and different IP/prefix selects a different key.

ADR-0013 decision 1 additions (`TestPartitionFor`, `TestJetStreamNaming`
below), written from that decision's text alone:

* `partition_for(key: bytes | str, partitions: int) -> int` is "FNV-1a
  32-bit over the UTF-8 bytes of `key`, modulo `partitions`" (assumption
  4 of that ADR names the algorithm so that the in-repo mapping coincides
  with the NATS server's own `{{partition()}}` transform). Pinned against a
  local reference implementation of FNV-1a 32-bit, and as the properties
  the aggregator relies on: deterministic, in `[0, partitions)`, identical
  for a `str` and its UTF-8 `bytes`.
* Every `TopicSpec` maps to one JetStream stream "whose name is the topic
  name with every `.` replaced by `-`", with `subject(p) == f"{name}.{p}"`
  and `subject_filter == f"{name}.*"`; the decision's table gives the four
  names, stream names and partition counts (128 / 8 / 32 / 4, "unchanged",
  assumption 5), which are asserted literally. The topic *names* in that
  table are therefore now pinned literals too -- the presence-by-substring
  tests above are kept as they were because they still hold, and the
  literal table is asserted alongside them rather than instead of them.
* `TopicSpec.partition_of(subject: str) -> int | None` (added 2026-09-22,
  Amendment 4 ruling S3; `TestPartitionOf` below): "`p` when `subject ==
  self.subject(p)` for some `p >= 0` -- the canonical decimal form, so
  `<name>.7` is `7` and `<name>.07`, `<name>.-1`, `<name>.x`, `<name>.7.8`
  and `<name>` are all `None` -- and nothing else; it is what `NatsConsumer`
  uses to read a delivered message's partition from its subject, and a
  `None` is a malformed message at the transport (decision 5, as amended)".
  Assumption 78: "Only the canonical decimal token is a partition; `07` and
  `+7` are not, although `int()` accepts them, because `subject(p)` never
  produces them ... Whether the parsed partition is below the topic's count
  is not checked, as decision 3 says `NatsConsumer` accepts any
  non-negative partition." Decision 5 (as amended) says only "the pure
  helpers (`partition_of`, `partition_for`) are unit-tested; the
  term-and-continue path is the integration job's (#52)", so nothing here
  touches a consumer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hammertime.bus.topics import TOPICS, partition_for
from hammertime.core.addressing.address import Address
from hammertime.core.events.models import HotIpAdded, HotIpRemoved, Observation, PrefixStatsChanged
from hypothesis import given, settings
from hypothesis import strategies as st

HOT_IP_TOPIC = "hammertime.hot-ip.v1"

T0 = datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC)

# ADR-0013 decision 1's table: (topic name, stream name, partitions).
STREAM_TABLE = [
    ("hammertime.observations.v1", "hammertime-observations-v1", 128),
    (
        "hammertime.observations-reconciliation.v1",
        "hammertime-observations-reconciliation-v1",
        8,
    ),
    ("hammertime.hot-ip.v1", "hammertime-hot-ip-v1", 32),
    ("hammertime.prefix-stats.v1", "hammertime-prefix-stats-v1", 4),
]


def _fnv1a_32(data: bytes) -> int:
    """Reference FNV-1a, 32-bit: the algorithm ADR-0013 decision 1 names."""

    digest = 0x811C9DC5
    for byte in data:
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return digest


def _topic_name_containing(*substrings: str, excluding: str | None = None) -> str:
    """Find the (assumed unique) topic name containing all of `substrings`."""

    matches = [
        name
        for name in TOPICS
        if all(s in name.lower() for s in substrings)
        and (excluding is None or excluding not in name.lower())
    ]
    assert matches, f"expected a topic name containing {substrings!r} in {list(TOPICS)}"
    assert len(matches) == 1, f"ambiguous topic names containing {substrings!r}: {matches}"
    return matches[0]


class TestHotIpTopicNameIsPinned:
    def test_registry_contains_the_exact_hot_ip_topic_name(self) -> None:
        # Hard compatibility requirement: the Makefile's `replay` target,
        # .env.example's HAMMERTIME_TOPIC_HOT_IP, and docs/runbook.md's
        # trie-restart procedure all hard-reference this literal string.
        assert "hammertime.hot-ip.v1" in TOPICS


class TestRegistryHasRequiredTopics:
    def test_has_a_hot_ip_topic(self) -> None:
        assert HOT_IP_TOPIC in TOPICS

    def test_has_an_observations_topic(self) -> None:
        _topic_name_containing("observation", excluding="reconcil")

    def test_has_an_observations_reconciliation_topic(self) -> None:
        # ADR-0002: late observations are "written to a reconciliation
        # topic rather than silently dropped."
        _topic_name_containing("reconcil")

    def test_has_a_prefix_stats_topic(self) -> None:
        _topic_name_containing("prefix")


class TestKeySelectorForIpKeyedTopics:
    def test_hot_ip_topic_key_selector_derives_the_key_from_the_ip(self) -> None:
        topic = TOPICS[HOT_IP_TOPIC]
        addr = Address.parse("192.168.1.42")
        added = HotIpAdded(ip=addr, timestamp=T0, sequence=1, window_count=1000, config_version=1)

        assert topic.key_selector(added) == str(addr)

    def test_hot_ip_topic_key_selector_is_insensitive_to_non_ip_fields(self) -> None:
        # Same IP, everything else different -> same key. This is the
        # invariant that actually matters for shard ownership (section 20);
        # the exact string format is secondary.
        topic = TOPICS[HOT_IP_TOPIC]
        addr = Address.parse("192.168.1.42")
        added = HotIpAdded(ip=addr, timestamp=T0, sequence=1, window_count=1000, config_version=1)
        removed = HotIpRemoved(
            ip=addr, timestamp=T0, sequence=999, window_count=1, config_version=42
        )

        assert topic.key_selector(added) == topic.key_selector(removed)

    def test_hot_ip_topic_key_selector_differs_for_different_ips(self) -> None:
        topic = TOPICS[HOT_IP_TOPIC]
        a = HotIpAdded(
            ip=Address.parse("10.0.0.1"),
            timestamp=T0,
            sequence=1,
            window_count=1000,
            config_version=1,
        )
        b = HotIpAdded(
            ip=Address.parse("10.0.0.2"),
            timestamp=T0,
            sequence=1,
            window_count=1000,
            config_version=1,
        )

        assert topic.key_selector(a) != topic.key_selector(b)

    def test_observations_topic_key_selector_derives_the_key_from_the_ip(self) -> None:
        name = _topic_name_containing("observation", excluding="reconcil")
        topic = TOPICS[name]
        addr = Address.parse("10.20.30.40")
        observation = Observation(ip=addr, request_count=183)

        assert topic.key_selector(observation) == str(addr)


class TestKeySelectorForPrefixStatsTopic:
    def test_prefix_stats_topic_key_selector_derives_the_key_from_the_prefix_not_an_ip(
        self,
    ) -> None:
        name = _topic_name_containing("prefix")
        topic = TOPICS[name]
        changed = PrefixStatsChanged(
            prefix="10.20.30.0/24", hot_count=156, capacity=256, sequence=1, timestamp=T0
        )

        assert topic.key_selector(changed) == "10.20.30.0/24"

    def test_prefix_stats_topic_key_selector_differs_for_different_prefixes(self) -> None:
        name = _topic_name_containing("prefix")
        topic = TOPICS[name]
        a = PrefixStatsChanged(
            prefix="10.20.30.0/24", hot_count=156, capacity=256, sequence=1, timestamp=T0
        )
        b = PrefixStatsChanged(
            prefix="10.20.31.0/24", hot_count=16, capacity=256, sequence=1, timestamp=T0
        )

        assert topic.key_selector(a) != topic.key_selector(b)

    def test_prefix_stats_topic_key_selector_is_insensitive_to_non_prefix_fields(self) -> None:
        name = _topic_name_containing("prefix")
        topic = TOPICS[name]
        a = PrefixStatsChanged(
            prefix="10.20.30.0/24", hot_count=156, capacity=256, sequence=1, timestamp=T0
        )
        b = PrefixStatsChanged(
            prefix="10.20.30.0/24", hot_count=16, capacity=256, sequence=999, timestamp=T0
        )

        assert topic.key_selector(a) == topic.key_selector(b)


class TestLookupByName:
    def test_known_topic_name_returns_a_consistent_config(self) -> None:
        assert TOPICS[HOT_IP_TOPIC] is TOPICS[HOT_IP_TOPIC]

    def test_different_topic_names_return_different_configs(self) -> None:
        prefix_stats_name = _topic_name_containing("prefix")
        assert TOPICS[HOT_IP_TOPIC] is not TOPICS[prefix_stats_name]
        # And they really are wired to different selection semantics, not
        # just distinct objects with identical behavior.
        addr = Address.parse("192.168.1.42")
        hot_ip_event = HotIpAdded(
            ip=addr, timestamp=T0, sequence=1, window_count=1000, config_version=1
        )
        assert TOPICS[HOT_IP_TOPIC].key_selector(hot_ip_event) == str(addr)

    def test_unknown_topic_name_raises(self) -> None:
        # ASSUMPTION (see module docstring): plain KeyError from a mapping
        # lookup. Adjust if topics.py raises a dedicated exception type.
        with pytest.raises(KeyError):
            TOPICS["hammertime.does-not-exist.v1"]


class TestPartitionFor:
    """ADR-0013 decision 1: "The hash is in this repository." `partition_for`
    is "FNV-1a 32-bit over the UTF-8 bytes of `key`, modulo `partitions`",
    and "an IP's shard is `partition_for(str(ip), 128)`, stable for the life
    of the deployment"."""

    @pytest.mark.parametrize(
        "key",
        ["10.0.0.1", "203.0.113.9", "2001:db8::1", "10.20.30.0/24", "", "k"],
    )
    def test_it_is_fnv1a_32_bit_modulo_the_partition_count(self, key: str) -> None:
        for partitions in (1, 4, 8, 32, 128):
            assert partition_for(key, partitions) == _fnv1a_32(key.encode("utf-8")) % partitions

    @pytest.mark.parametrize("partitions", [1, 4, 8, 32, 128])
    def test_it_is_in_range(self, partitions: int) -> None:
        for octet in range(256):
            value = partition_for(f"10.0.0.{octet}", partitions)
            assert 0 <= value < partitions

    def test_it_is_deterministic(self) -> None:
        assert partition_for("198.51.100.7", 128) == partition_for("198.51.100.7", 128)

    @pytest.mark.parametrize("key", ["10.0.0.1", "2001:db8::dead:beef", "10.20.30.0/24"])
    def test_a_str_and_its_utf8_bytes_hash_alike(self, key: str) -> None:
        # Decision 3: a `str` key "is encoded as UTF-8 before hashing".
        assert partition_for(key, 128) == partition_for(key.encode("utf-8"), 128)

    def test_one_partition_maps_everything_to_zero(self) -> None:
        for octet in range(256):
            assert partition_for(f"10.0.0.{octet}", 1) == 0

    def test_it_spreads_keys_over_the_partitions(self) -> None:
        # Not a statistical claim -- only that the hash is not degenerate:
        # 256 consecutive addresses do not all land on one of 128 partitions.
        hit = {partition_for(f"10.0.0.{octet}", 128) for octet in range(256)}
        assert len(hit) > 1

    def test_the_ip_to_shard_mapping_uses_the_address_text(self) -> None:
        # ADR-0011 A1 / ADR-0013 decision 1: the shard is
        # `partition_for(str(ip), 128)`, i.e. the same value the key selector
        # of the observations topic hands the producer.
        addr = Address.parse("10.20.30.40")
        observations = TOPICS["hammertime.observations.v1"]
        key = observations.key_selector(Observation(ip=addr, request_count=1))

        assert partition_for(key, 128) == partition_for(str(addr), 128)


class TestJetStreamNaming:
    """ADR-0013 decision 1's table: one stream per topic, named with `-` for
    `.`; a partition `p` of topic `T` is the subject `T.<p>`; the filter is
    `T.*`; partition counts unchanged (assumption 5)."""

    @pytest.mark.parametrize(("name", "stream_name", "partitions"), STREAM_TABLE)
    def test_the_registry_holds_the_four_topics_of_the_table(
        self, name: str, stream_name: str, partitions: int
    ) -> None:
        assert name in TOPICS
        assert TOPICS[name].name == name

    @pytest.mark.parametrize(("name", "stream_name", "partitions"), STREAM_TABLE)
    def test_stream_name_replaces_every_dot_with_a_dash(
        self, name: str, stream_name: str, partitions: int
    ) -> None:
        assert TOPICS[name].stream_name == stream_name
        assert TOPICS[name].stream_name == name.replace(".", "-")
        # NATS ADR-6: stream names may not contain `.`, `*`, `>`, `/` or `\`.
        assert not any(forbidden in TOPICS[name].stream_name for forbidden in ".*>/\\")

    @pytest.mark.parametrize(("name", "stream_name", "partitions"), STREAM_TABLE)
    def test_subject_is_the_topic_name_dot_partition(
        self, name: str, stream_name: str, partitions: int
    ) -> None:
        spec = TOPICS[name]

        assert spec.subject(0) == f"{name}.0"
        assert spec.subject(partitions - 1) == f"{name}.{partitions - 1}"
        assert [spec.subject(p) for p in range(3)] == [f"{name}.0", f"{name}.1", f"{name}.2"]

    @pytest.mark.parametrize(("name", "stream_name", "partitions"), STREAM_TABLE)
    def test_subject_filter_is_the_topic_name_dot_star(
        self, name: str, stream_name: str, partitions: int
    ) -> None:
        assert TOPICS[name].subject_filter == f"{name}.*"

    @pytest.mark.parametrize(("name", "stream_name", "partitions"), STREAM_TABLE)
    def test_partition_counts_are_unchanged(
        self, name: str, stream_name: str, partitions: int
    ) -> None:
        assert TOPICS[name].partitions == partitions

    def test_every_subject_of_a_topic_matches_its_filter(self) -> None:
        # The single-token wildcard `*` matches exactly one token, and every
        # subject has exactly one token after the topic name.
        for name, _stream_name, partitions in STREAM_TABLE:
            spec = TOPICS[name]
            prefix = spec.subject_filter[: -len("*")]
            for p in range(partitions):
                subject = spec.subject(p)
                assert subject.startswith(prefix)
                assert "." not in subject[len(prefix) :]
                assert subject[len(prefix) :] == str(p)

    def test_stream_names_are_distinct(self) -> None:
        assert len({TOPICS[name].stream_name for name, _, _ in STREAM_TABLE}) == len(STREAM_TABLE)


class TestPartitionOf:
    """ADR-0013 decision 1 as amended (Amendment 4 ruling S3): `partition_of(subject)`
    is "`p` when `subject == self.subject(p)` for some `p >= 0` -- the canonical
    decimal form ... -- and nothing else"."""

    @pytest.mark.parametrize(("name", "stream_name", "partitions"), STREAM_TABLE)
    def test_every_partition_of_the_topic_round_trips(
        self, name: str, stream_name: str, partitions: int
    ) -> None:
        spec = TOPICS[name]

        for p in range(partitions):
            assert spec.partition_of(spec.subject(p)) == p

    @pytest.mark.parametrize(("name", "stream_name", "partitions"), STREAM_TABLE)
    def test_a_partition_beyond_the_topics_count_still_parses(
        self, name: str, stream_name: str, partitions: int
    ) -> None:
        # Assumption 78: "Whether the parsed partition is below the topic's
        # count is not checked, as decision 3 says `NatsConsumer` accepts any
        # non-negative partition" -- `subject(999)` is a subject `subject_filter`
        # admits, and its partition is 999.
        spec = TOPICS[name]

        assert spec.partition_of(spec.subject(partitions)) == partitions
        assert spec.partition_of(spec.subject(999)) == 999
        assert spec.partition_of(f"{name}.999") == 999

    def test_the_result_is_an_int(self) -> None:
        spec = TOPICS[HOT_IP_TOPIC]

        value = spec.partition_of(spec.subject(7))

        assert value == 7
        assert isinstance(value, int)
        assert not isinstance(value, bool)

    @pytest.mark.parametrize(
        "suffix",
        [
            pytest.param(".07", id="leading-zero"),
            pytest.param(".-1", id="negative"),
            pytest.param(".x", id="not-a-number"),
            pytest.param(".7.8", id="two-tokens"),
            pytest.param("", id="bare-topic-name"),
            pytest.param(".", id="empty-token"),
            pytest.param(".+7", id="explicit-plus"),
            pytest.param(".00", id="double-zero"),
            pytest.param(". 7", id="leading-space"),
            pytest.param(".7 ", id="trailing-space"),
            pytest.param(".7\n", id="trailing-newline"),
            pytest.param(".1_000", id="underscore"),
            pytest.param(".0x7", id="hex"),
            pytest.param(".7.", id="trailing-dot"),
            pytest.param(".*", id="wildcard"),
        ],
    )
    def test_anything_but_the_canonical_decimal_token_is_none(self, suffix: str) -> None:
        # "`<name>.07`, `<name>.-1`, `<name>.x`, `<name>.7.8` and `<name>` are
        # all `None` -- and nothing else": `int()` would accept `07`, `+7`,
        # ` 7` and `1_000`, and `subject(p)` produces none of them.
        spec = TOPICS[HOT_IP_TOPIC]

        assert spec.partition_of(f"{HOT_IP_TOPIC}{suffix}") is None

    def test_zero_is_a_partition_but_its_padded_forms_are_not(self) -> None:
        spec = TOPICS[HOT_IP_TOPIC]

        assert spec.partition_of(f"{HOT_IP_TOPIC}.0") == 0
        assert spec.partition_of(f"{HOT_IP_TOPIC}.00") is None
        assert spec.partition_of(f"{HOT_IP_TOPIC}.-0") is None

    def test_another_topics_subject_is_none(self) -> None:
        # The subject must be *this* topic's: `hammertime.hot-ip.v1.3` is not a
        # partition of the observations topic, whatever its last token.
        observations = TOPICS["hammertime.observations.v1"]
        hot_ip = TOPICS[HOT_IP_TOPIC]

        assert observations.partition_of(hot_ip.subject(3)) is None
        assert hot_ip.partition_of(observations.subject(3)) is None

    def test_a_subject_of_a_topic_whose_name_extends_this_one_is_none(self) -> None:
        # `hammertime.observations.v1` is a prefix of
        # `hammertime.observations-reconciliation.v1` only textually; neither's
        # subjects are the other's.
        observations = TOPICS["hammertime.observations.v1"]
        reconciliation = TOPICS["hammertime.observations-reconciliation.v1"]

        assert observations.partition_of(reconciliation.subject(0)) is None
        assert reconciliation.partition_of(observations.subject(0)) is None

    @pytest.mark.parametrize("subject", ["", ".", "7", ".7", "hammertime", "hammertime.hot-ip"])
    def test_a_subject_that_is_not_under_the_topic_at_all_is_none(self, subject: str) -> None:
        assert TOPICS[HOT_IP_TOPIC].partition_of(subject) is None

    def test_the_filter_itself_is_not_a_partition(self) -> None:
        # `<topic>.*` is the stream's subject filter, never a stored subject.
        spec = TOPICS[HOT_IP_TOPIC]

        assert spec.partition_of(spec.subject_filter) is None


# --- partition_of round-trips subject (decision 1, as amended) -------------------------


# Bounded to 64 bits: JetStream sequences and partitions are `uint64`, and an
# unbounded strategy could exceed CPython's `int()` digit limit, which is a
# harness artefact and not the ruling under test.
_PARTITIONS = st.integers(min_value=0, max_value=2**64 - 1)
_NEGATIVE = st.integers(min_value=-(2**64), max_value=-1)


@given(p=_PARTITIONS)
@settings(deadline=None, max_examples=200)
def test_partition_of_inverts_subject_for_every_non_negative_partition(p: int) -> None:
    # "`p` when `subject == self.subject(p)` for some `p >= 0`" -- for every
    # such `p`, on every registered topic, beyond the partition count included.
    for name, _stream_name, _partitions in STREAM_TABLE:
        spec = TOPICS[name]
        assert spec.partition_of(spec.subject(p)) == p


@given(p=_PARTITIONS)
@settings(deadline=None, max_examples=200)
def test_a_zero_padded_token_is_never_a_partition(p: int) -> None:
    # Assumption 78: the canonical decimal form has no leading zero, so `0`
    # prepended to any canonical token is a different, non-canonical token.
    spec = TOPICS[HOT_IP_TOPIC]

    assert spec.partition_of(f"{HOT_IP_TOPIC}.0{p}") is None


@given(p=_NEGATIVE)
@settings(deadline=None, max_examples=200)
def test_a_negative_token_is_never_a_partition(p: int) -> None:
    spec = TOPICS[HOT_IP_TOPIC]

    assert spec.partition_of(f"{HOT_IP_TOPIC}.{p}") is None
