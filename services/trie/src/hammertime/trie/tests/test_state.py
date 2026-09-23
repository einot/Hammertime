"""The trie's read-state seam: `FamilyState` and `TrieState`.

Spec: section 22 (every read carries `event_sequence`, `as_of` and
`config_version`), section 28 (readers see whole events), section 33 (the
snapshot's event sequence number), section 35 (one root per address family),
section 46.5 (the trie and the record map move together).

Written from ADR-0017 decision 2 (the `state.py` block and its bullets, as
revised on 2026-09-23) and decision 8 (`position`, `event_sequence`, `as_of`):

* the constructor builds one empty `PatriciaTrie(f)` and one empty
  `IpAttributeRecords(f)` per family; an empty `families` is a `ValueError`;
  it starts with `position = None`, `as_of = None` and the given `config`;
* `event_sequence` is `0` when `position` is `None`, else `position + 1`;
* `note_handled(offset)` sets `position`; `note_applied(offset, timestamp)`
  also sets `as_of` to the maximum seen; `note_passed(offset)` "sets
  `position = offset`, and nothing else ... `as_of` does not move"
  (`TestNotePassed`; decision 4 step 3 is its caller);
* all three refuse an offset below 0 or not greater than `position`, and
  `note_applied` a naive timestamp, with a `ValueError` "and nothing changes";
  an offset passed by `note_passed` is refused by the other two as well;
* `adopt_config` "compares no versions" (decision 10).
"""

from datetime import UTC, datetime, timedelta

import pytest
from hammertime.core.addressing.address import AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.trie.state import FamilyState, TrieState
from hammertime.trie.structure.patricia import PatriciaTrie

IPV4 = AddressFamily.IPV4
IPV6 = AddressFamily.IPV6

# `docs/spec/integration-scenarios.md` section 2's T0.
T0 = datetime.fromtimestamp(1_800_000_000, tz=UTC)


def _config(version: int = 1) -> DetectionConfig:
    """`config/detection.v1.json`'s values (integration-scenarios section 2.1)."""

    return DetectionConfig(
        config_version=version,
        window_seconds=300,
        bucket_seconds=10,
        hot_threshold=1000,
        cold_threshold=800,
        allowed_lateness_seconds=30,
        state_retention_seconds=600,
    )


def _snapshot(state: TrieState) -> tuple[object, ...]:
    return (state.position, state.event_sequence, state.as_of, state.config)


class TestConstruction:
    def test_a_fresh_ipv4_only_state(self) -> None:
        config = _config()
        state = TrieState(families=[IPV4], config=config)

        assert state.families == frozenset({IPV4})
        assert state.serves(IPV4) is True
        assert state.serves(IPV6) is False
        with pytest.raises(KeyError):
            state.of(IPV6)

        fs = state.of(IPV4)
        assert isinstance(fs, FamilyState)
        assert fs.family == IPV4
        assert isinstance(fs.trie, PatriciaTrie)
        assert fs.trie.family == IPV4
        assert fs.trie.hot_ip_count == 0
        assert fs.trie.node_count == 0
        assert fs.records.family == IPV4
        assert len(fs.records) == 0

        assert state.position is None
        assert state.event_sequence == 0
        assert state.as_of is None
        assert state.config is config

    def test_each_served_family_has_its_own_pair(self) -> None:
        state = TrieState(families=frozenset({IPV4, IPV6}), config=_config())

        assert state.families == frozenset({IPV4, IPV6})
        assert state.serves(IPV4) and state.serves(IPV6)
        v4 = state.of(IPV4)
        v6 = state.of(IPV6)
        assert v4.family == IPV4
        assert v6.family == IPV6
        assert v4.trie.family == IPV4
        assert v6.trie.family == IPV6
        assert v4.records.family == IPV4
        assert v6.records.family == IPV6
        assert v4.trie is not v6.trie
        assert v4.records is not v6.records

    @pytest.mark.parametrize(
        "families", [pytest.param([], id="list"), pytest.param(frozenset(), id="frozenset")]
    )
    def test_no_family_is_a_value_error(self, families: object) -> None:
        with pytest.raises(ValueError):
            TrieState(families=families, config=_config())  # type: ignore[arg-type]


class TestNoteHandled:
    def test_the_first_handled_offset_gives_event_sequence_one(self) -> None:
        state = TrieState(families=[IPV4], config=_config())

        state.note_handled(0)

        assert state.position == 0
        assert state.event_sequence == 1
        assert state.as_of is None

    def test_gaps_are_allowed(self) -> None:
        state = TrieState(families=[IPV4], config=_config())
        state.note_handled(0)

        state.note_handled(5)

        assert state.position == 5
        assert state.event_sequence == 6
        assert state.as_of is None


class TestNoteApplied:
    def test_it_sets_position_and_as_of(self) -> None:
        state = TrieState(families=[IPV4], config=_config())

        state.note_applied(0, T0)

        assert state.position == 0
        assert state.event_sequence == 1
        assert state.as_of == T0

    def test_as_of_is_the_greatest_timestamp_not_the_last(self) -> None:
        state = TrieState(families=[IPV4], config=_config())
        later = T0 + timedelta(seconds=10)

        state.note_applied(1, later)
        state.note_applied(2, T0)

        assert state.as_of == later
        assert state.position == 2
        assert state.event_sequence == 3

    def test_a_later_timestamp_moves_as_of_forward(self) -> None:
        state = TrieState(families=[IPV4], config=_config())
        later = T0 + timedelta(seconds=10)

        state.note_applied(0, T0)
        state.note_applied(1, later)

        assert state.as_of == later

    def test_note_handled_after_note_applied_keeps_as_of(self) -> None:
        state = TrieState(families=[IPV4], config=_config())
        state.note_applied(0, T0)

        state.note_handled(1)

        assert state.as_of == T0
        assert state.event_sequence == 2


class TestNotePassed:
    """Decision 2: "**`note_passed(offset)`** sets `position = offset`, and
    nothing else: the log holds no record at or below `offset` that the trie
    has not read (decision 4 step 3). `as_of` does not move." Decision 8:
    `position` is "the last offset the worker has *handled* or *passed*"."""

    def test_passing_offset_zero_gives_event_sequence_one(self) -> None:
        # JetStream's offset 0 holds no record, "so a trie started there
        # passes it" (decision 8, "What it costs").
        state = TrieState(families=[IPV4], config=_config())

        state.note_passed(0)

        assert state.position == 0
        assert state.event_sequence == 1
        assert state.as_of is None

    def test_passing_many_offsets_on_a_fresh_state(self) -> None:
        state = TrieState(families=[IPV4], config=_config())

        state.note_passed(500)

        assert state.position == 500
        assert state.event_sequence == 501
        assert state.as_of is None

    def test_as_of_does_not_move(self) -> None:
        state = TrieState(families=[IPV4], config=_config())
        state.note_applied(3, T0)

        state.note_passed(500)

        assert state.position == 500
        assert state.event_sequence == 501
        assert state.as_of == T0

    def test_it_leaves_the_config_alone(self) -> None:
        config = _config()
        state = TrieState(families=[IPV4], config=config)

        state.note_passed(7)

        assert state.config is config

    def test_a_passed_offset_is_not_handled_or_applied_afterwards(self) -> None:
        # "What all three refuse": an offset "not greater than `position`" --
        # and `note_passed(10)` has made `position` 10.
        state = TrieState(families=[IPV4], config=_config())
        state.note_passed(10)
        before = _snapshot(state)

        with pytest.raises(ValueError):
            state.note_handled(10)
        with pytest.raises(ValueError):
            state.note_applied(10, T0)

        assert _snapshot(state) == before

        state.note_handled(11)

        assert state.position == 11
        assert state.event_sequence == 12


class TestRefusals:
    """Decision 2: "**What all three refuse.** An `offset` below 0, or not
    greater than `position`, is a `ValueError` from `note_handled`,
    `note_applied` and `note_passed`, and nothing changes. `note_applied` also
    refuses a naive `timestamp` in the same way." """

    def test_a_negative_offset_is_refused_by_all_three(self) -> None:
        state = TrieState(families=[IPV4], config=_config())
        before = _snapshot(state)

        with pytest.raises(ValueError):
            state.note_handled(-1)
        with pytest.raises(ValueError):
            state.note_applied(-1, T0)
        with pytest.raises(ValueError):
            state.note_passed(-1)

        assert _snapshot(state) == before

    @pytest.mark.parametrize(
        "offset", [pytest.param(3, id="repeated"), pytest.param(2, id="lower")]
    )
    def test_an_offset_not_past_the_position_is_refused_by_all_three(self, offset: int) -> None:
        state = TrieState(families=[IPV4], config=_config())
        state.note_applied(3, T0)
        before = _snapshot(state)

        with pytest.raises(ValueError):
            state.note_handled(offset)
        with pytest.raises(ValueError):
            state.note_applied(offset, T0 + timedelta(seconds=60))
        with pytest.raises(ValueError):
            state.note_passed(offset)

        assert _snapshot(state) == before

    @pytest.mark.parametrize(
        "offset", [pytest.param(5, id="repeated"), pytest.param(4, id="lower")]
    )
    def test_note_passed_refuses_an_offset_not_past_a_passed_position(self, offset: int) -> None:
        state = TrieState(families=[IPV4], config=_config())
        state.note_passed(5)
        before = _snapshot(state)

        with pytest.raises(ValueError):
            state.note_passed(offset)

        assert _snapshot(state) == before

    def test_a_naive_timestamp_is_refused(self) -> None:
        state = TrieState(families=[IPV4], config=_config())
        state.note_applied(0, T0)
        before = _snapshot(state)

        with pytest.raises(ValueError):
            state.note_applied(1, datetime(2030, 1, 1, 0, 0, 0))

        assert _snapshot(state) == before

    def test_a_naive_timestamp_is_refused_on_a_fresh_state(self) -> None:
        state = TrieState(families=[IPV4], config=_config())

        with pytest.raises(ValueError):
            state.note_applied(0, datetime(2030, 1, 1, 0, 0, 0))

        assert state.position is None
        assert state.event_sequence == 0
        assert state.as_of is None


class TestAdoptConfig:
    def test_a_higher_version_is_adopted(self) -> None:
        state = TrieState(families=[IPV4], config=_config(1))
        newer = _config(2)

        state.adopt_config(newer)

        assert state.config is newer

    def test_a_lower_version_is_adopted_too(self) -> None:
        # Decision 2 / decision 10: "It compares no versions" -- the poller
        # alone gates them.
        state = TrieState(families=[IPV4], config=_config(5))
        older = _config(2)

        state.adopt_config(older)

        assert state.config is older
        assert state.config.config_version == 2

    def test_adopting_leaves_the_position_alone(self) -> None:
        state = TrieState(families=[IPV4], config=_config(1))
        state.note_applied(4, T0)

        state.adopt_config(_config(2))

        assert state.position == 4
        assert state.event_sequence == 5
        assert state.as_of == T0
