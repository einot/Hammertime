"""`TrieMetrics`: strict series names and labels, and the series derived from the state.

Spec: section 37 (`trie_updates`, `trie_nodes`, `hot_ip_count`), section 46.8
(`ip_attribute_records`, `ip_attribute_bytes`, `attributes_rejected`).

Written from ADR-0017 decision 12 ("Metrics"), which follows
`AggregatorMetrics`:

* "one series is one `(name, label values)` pair, and label values are
  compared as `str(value)`";
* "the name must be one of those in the table below, and the label names
  exactly that series' own, or `increment`, `set` and `get` raise
  `ValueError`"; `increment` is for counters only and `set` for gauges only;
* `get` returns `0` for a counter never incremented, a gauge never set, any
  series read before `bind_state`, and a `family` the state does not serve.

The derived series read the bound `TrieState`: `trie_nodes` is
`trie.node_count`, `hot_ip_count` is `trie.hot_ip_count`,
`ip_attribute_records` is `len(records)`, `ip_attribute_bytes` is
`records.serialized_bytes`, and `event_sequence` is the state's.
`DEFAULT_ATTRIBUTES` encodes to 24 bytes (ADR-0015; `test_metadata.py`).

Choice of this file's own: every counter and gauge test binds a state before
it writes. Decision 12's "any series read before `bind_state`" reads `0` is
asserted only for series nothing has written, so no test depends on whether a
counter incremented before `bind_state` is visible after it.

Label *values* outside the documented sets (e.g. `result="bogus"`) are not
exercised: decision 12 constrains label names, not values.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.trie.metadata.ip_attributes import apply_hot_ip_added
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.state import TrieState

IPV4 = AddressFamily.IPV4
IPV6 = AddressFamily.IPV6

T0 = datetime.fromtimestamp(1_800_000_000, tz=UTC)
IP = Address.parse("10.20.30.40")
IP_B = Address.parse("10.20.30.41")

ONLY_V4 = frozenset({IPV4})


def _config() -> DetectionConfig:
    return DetectionConfig(
        config_version=1,
        window_seconds=300,
        bucket_seconds=10,
        hot_threshold=1000,
        cold_threshold=800,
        allowed_lateness_seconds=30,
        state_retention_seconds=600,
    )


def _bound(families: frozenset[AddressFamily] = ONLY_V4) -> tuple[TrieMetrics, TrieState]:
    state = TrieState(families=families, config=_config())
    metrics = TrieMetrics()
    metrics.bind_state(state)
    return metrics, state


def _updates(
    result: str, *, event_type: str = "HotIpAdded", family: object = "ipv4"
) -> dict[str, object]:
    return {"family": family, "event_type": event_type, "result": result}


APPLIED_ADD = _updates("applied")

# Every series with one valid label set, as `get` reads it.
EVERY_SERIES = [
    pytest.param("trie_updates", APPLIED_ADD, id="trie_updates-add-applied"),
    pytest.param(
        "trie_updates",
        _updates("unchanged", event_type="HotIpRemoved"),
        id="trie_updates-remove-unchanged",
    ),
    pytest.param("hot_ip_events_skipped", {"reason": "malformed"}, id="skipped-malformed"),
    pytest.param("hot_ip_events_skipped", {"reason": "family_not_served"}, id="skipped-family"),
    pytest.param("attributes_rejected", {"stage": "decode"}, id="rejected-decode"),
    pytest.param("attributes_rejected", {"stage": "apply"}, id="rejected-apply"),
    pytest.param("trie_nodes", {"family": "ipv4"}, id="trie_nodes"),
    pytest.param("hot_ip_count", {"family": "ipv4"}, id="hot_ip_count"),
    pytest.param("ip_attribute_records", {"family": "ipv4"}, id="ip_attribute_records"),
    pytest.param("ip_attribute_bytes", {"family": "ipv4"}, id="ip_attribute_bytes"),
    pytest.param("event_sequence", {}, id="event_sequence"),
    pytest.param("trie_recovery_seconds", {}, id="trie_recovery_seconds"),
]


class TestEverythingStartsAtZero:
    @pytest.mark.parametrize(("name", "labels"), EVERY_SERIES)
    def test_before_bind_state(self, name: str, labels: dict[str, object]) -> None:
        assert TrieMetrics().get(name, **labels) == 0

    @pytest.mark.parametrize(("name", "labels"), EVERY_SERIES)
    def test_after_binding_a_fresh_state(self, name: str, labels: dict[str, object]) -> None:
        metrics, _state = _bound()

        assert metrics.get(name, **labels) == 0


class TestCounters:
    def test_two_increments_read_two(self) -> None:
        metrics, _ = _bound()

        metrics.increment("trie_updates", **APPLIED_ADD)
        metrics.increment("trie_updates", **APPLIED_ADD)

        assert metrics.get("trie_updates", **APPLIED_ADD) == 2

    def test_other_label_combinations_still_read_zero(self) -> None:
        metrics, _ = _bound()

        metrics.increment("trie_updates", **APPLIED_ADD)

        assert metrics.get("trie_updates", **_updates("unchanged")) == 0
        assert metrics.get("trie_updates", **_updates("applied", event_type="HotIpRemoved")) == 0
        assert metrics.get("trie_updates", **_updates("applied", family="ipv6")) == 0

    def test_each_counter_is_its_own_series(self) -> None:
        metrics, _ = _bound()

        metrics.increment("hot_ip_events_skipped", reason="malformed")
        metrics.increment("attributes_rejected", stage="decode")
        metrics.increment("attributes_rejected", stage="decode")

        assert metrics.get("hot_ip_events_skipped", reason="malformed") == 1
        assert metrics.get("hot_ip_events_skipped", reason="family_not_served") == 0
        assert metrics.get("attributes_rejected", stage="decode") == 2
        assert metrics.get("attributes_rejected", stage="apply") == 0

    def test_label_values_are_compared_as_their_text(self) -> None:
        metrics, _ = _bound()

        metrics.increment("trie_updates", **_updates("applied", family=IPV4))
        metrics.increment("trie_updates", **_updates("applied", family="ipv4"))

        assert metrics.get("trie_updates", **_updates("applied", family="ipv4")) == 2
        assert metrics.get("trie_updates", **_updates("applied", family=IPV4)) == 2


class TestNameAndLabelRules:
    @pytest.mark.parametrize(
        ("name", "labels"),
        [
            pytest.param("hot_ip_count", {"family": "ipv4"}, id="derived-series"),
            pytest.param("event_sequence", {}, id="derived-event-sequence"),
            pytest.param("trie_recovery_seconds", {}, id="gauge"),
            pytest.param("no_such_series", {}, id="unknown-name"),
        ],
    )
    def test_increment_on_anything_but_a_counter_is_refused(
        self, name: str, labels: dict[str, object]
    ) -> None:
        metrics, _ = _bound()

        with pytest.raises(ValueError):
            metrics.increment(name, **labels)

    @pytest.mark.parametrize(
        ("name", "labels"),
        [
            pytest.param("trie_updates", APPLIED_ADD, id="counter"),
            pytest.param("hot_ip_events_skipped", {"reason": "malformed"}, id="counter-skipped"),
            pytest.param("hot_ip_count", {"family": "ipv4"}, id="derived-series"),
            pytest.param("event_sequence", {}, id="derived-event-sequence"),
            pytest.param("no_such_series", {}, id="unknown-name"),
        ],
    )
    def test_set_on_anything_but_a_gauge_is_refused(
        self, name: str, labels: dict[str, object]
    ) -> None:
        metrics, _ = _bound()

        with pytest.raises(ValueError):
            metrics.set(name, 1.0, **labels)

    def test_get_of_an_unknown_name_is_refused(self) -> None:
        metrics, _ = _bound()

        with pytest.raises(ValueError):
            metrics.get("no_such_series")

    @pytest.mark.parametrize(
        ("name", "labels"),
        [
            pytest.param(
                "trie_updates", {"family": "ipv4", "event_type": "HotIpAdded"}, id="missing"
            ),
            pytest.param("trie_updates", {**APPLIED_ADD, "shard": "0"}, id="extra"),
            pytest.param("hot_ip_events_skipped", {"stage": "decode"}, id="wrong"),
            pytest.param("attributes_rejected", {}, id="none-given"),
        ],
    )
    def test_increment_with_the_wrong_label_names_is_refused(
        self, name: str, labels: dict[str, object]
    ) -> None:
        metrics, _ = _bound()

        with pytest.raises(ValueError):
            metrics.increment(name, **labels)

    @pytest.mark.parametrize(
        "labels",
        [
            pytest.param({"family": "ipv4"}, id="extra"),
            pytest.param({"reason": "malformed"}, id="wrong"),
        ],
    )
    def test_set_with_the_wrong_label_names_is_refused(self, labels: dict[str, object]) -> None:
        metrics, _ = _bound()

        with pytest.raises(ValueError):
            metrics.set("trie_recovery_seconds", 1.0, **labels)

    @pytest.mark.parametrize(
        ("name", "labels"),
        [
            pytest.param(
                "trie_updates", {"family": "ipv4", "result": "applied"}, id="counter-missing"
            ),
            pytest.param(
                "attributes_rejected", {"stage": "decode", "family": "ipv4"}, id="counter-extra"
            ),
            pytest.param("hot_ip_count", {}, id="derived-missing"),
            pytest.param("trie_nodes", {"reason": "malformed"}, id="derived-wrong"),
            pytest.param("event_sequence", {"family": "ipv4"}, id="event-sequence-extra"),
            pytest.param("trie_recovery_seconds", {"family": "ipv4"}, id="gauge-extra"),
        ],
    )
    def test_get_with_the_wrong_label_names_is_refused(
        self, name: str, labels: dict[str, object]
    ) -> None:
        metrics, _ = _bound()

        with pytest.raises(ValueError):
            metrics.get(name, **labels)


class TestGauge:
    def test_trie_recovery_seconds_reads_back_what_was_set(self) -> None:
        metrics, _ = _bound()

        metrics.set("trie_recovery_seconds", 1.25)

        assert metrics.get("trie_recovery_seconds") == 1.25

    def test_a_later_set_replaces_the_value(self) -> None:
        metrics, _ = _bound()

        metrics.set("trie_recovery_seconds", 1.25)
        metrics.set("trie_recovery_seconds", 0.5)

        assert metrics.get("trie_recovery_seconds") == 0.5


class TestSeriesDerivedFromTheState:
    def test_one_hot_address_with_the_default_document(self) -> None:
        metrics, state = _bound()
        fs = state.of(IPV4)

        assert apply_hot_ip_added(fs.trie, fs.records, IP, None) is True

        assert metrics.get("hot_ip_count", family="ipv4") == 1
        assert metrics.get("trie_nodes", family="ipv4") == 1
        assert metrics.get("ip_attribute_records", family="ipv4") == 1
        assert metrics.get("ip_attribute_bytes", family="ipv4") == 24
        # Label values compare as text: the enum names the same series.
        assert metrics.get("hot_ip_count", family=IPV4) == 1

    def test_an_unserved_family_reads_zero(self) -> None:
        metrics, state = _bound()
        fs = state.of(IPV4)
        apply_hot_ip_added(fs.trie, fs.records, IP, None)

        for name in ("hot_ip_count", "trie_nodes", "ip_attribute_records", "ip_attribute_bytes"):
            assert metrics.get(name, family="ipv6") == 0, name

    def test_the_series_follow_the_state_as_it_changes(self) -> None:
        metrics, state = _bound()
        fs = state.of(IPV4)
        apply_hot_ip_added(fs.trie, fs.records, IP, None)
        apply_hot_ip_added(fs.trie, fs.records, IP_B, {"attributes_version": 1, "weight": 7})

        assert metrics.get("hot_ip_count", family="ipv4") == fs.trie.hot_ip_count == 2
        assert metrics.get("trie_nodes", family="ipv4") == fs.trie.node_count
        assert metrics.get("ip_attribute_records", family="ipv4") == len(fs.records) == 2
        assert metrics.get("ip_attribute_bytes", family="ipv4") == fs.records.serialized_bytes

    def test_event_sequence_mirrors_the_state(self) -> None:
        metrics, state = _bound()
        assert metrics.get("event_sequence") == 0

        state.note_handled(0)
        assert metrics.get("event_sequence") == state.event_sequence == 1

        state.note_applied(6, T0)
        assert metrics.get("event_sequence") == state.event_sequence == 7

    def test_both_families_are_read_separately(self) -> None:
        metrics, state = _bound(frozenset({IPV4, IPV6}))
        v6 = state.of(IPV6)

        apply_hot_ip_added(v6.trie, v6.records, Address.parse("2001:db8::1"), None)

        assert metrics.get("hot_ip_count", family="ipv6") == 1
        assert metrics.get("ip_attribute_records", family="ipv6") == 1
        assert metrics.get("hot_ip_count", family="ipv4") == 0
        assert metrics.get("ip_attribute_records", family="ipv4") == 0
