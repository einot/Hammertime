"""No input sequence produces a transition that violates the hysteresis rules.

Spec: section 6, section 7, section 30, section 38

These properties are stated over `hammertime.core.state.evaluate_ip_state`
alone -- section 30 makes it the authoritative implementation of the HOT/COLD
state machine, and ADR-0011 decision 5 defines the aggregator's `decide` as
"returns a transition iff `evaluate_ip_state` differs from `previous`". Every
invariant proved here therefore carries over unchanged to the edges the
aggregator emits, without this file depending on any service module.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from hammertime.core.config.models import DetectionConfig
from hammertime.core.state import evaluate_ip_state
from hammertime.core.state.enums import IpState
from hammertime.testkit.invariants import HysteresisStep, assert_hysteresis_holds
from hypothesis import given
from hypothesis import strategies as st

MAX_THRESHOLD = 10_000
MAX_COUNT = 2 * MAX_THRESHOLD
MAX_SEQUENCE = 40

# The worked example in section 6 and section 7.
EXAMPLE_CONFIG = DetectionConfig(hot_threshold=1000, cold_threshold=800)


@st.composite
def detection_configs(draw: st.DrawFn) -> DetectionConfig:
    """Any config section 6 admits: 0 <= cold_threshold < hot_threshold."""
    hot_threshold = draw(st.integers(min_value=1, max_value=MAX_THRESHOLD))
    cold_threshold = draw(st.integers(min_value=0, max_value=hot_threshold - 1))
    return DetectionConfig(hot_threshold=hot_threshold, cold_threshold=cold_threshold)


def counts_for(config: DetectionConfig) -> st.SearchStrategy[int]:
    """Non-negative window counts, biased towards both threshold boundaries.

    A window count is a sum of `request_count` deltas and can never be
    negative (ADR-0011 decision 1), so the strategy never produces one.
    """
    boundaries = {
        0,
        max(config.cold_threshold - 1, 0),
        config.cold_threshold,
        config.cold_threshold + 1,
        config.hot_threshold - 1,
        config.hot_threshold,
        config.hot_threshold + 1,
    }
    return st.one_of(
        st.sampled_from(sorted(boundaries)),
        st.integers(min_value=0, max_value=2 * config.hot_threshold + 2),
    )


@st.composite
def configs_and_counts(draw: st.DrawFn) -> tuple[DetectionConfig, list[int]]:
    """A config plus a sequence of window counts drawn against its thresholds."""
    config = draw(detection_configs())
    counts = draw(st.lists(counts_for(config), max_size=MAX_SEQUENCE))
    return config, counts


def fold(
    counts: Iterable[int],
    config: DetectionConfig,
    *,
    initial: IpState = IpState.COLD,
) -> list[HysteresisStep]:
    """Run the state machine over `counts`, recording every evaluation.

    Returns one `(previous, count, current)` step per count, which is the
    history shape `assert_hysteresis_holds` checks. New IPs start COLD
    (ADR-0011 decision 3).
    """
    state = initial
    history: list[HysteresisStep] = []
    for count in counts:
        current = evaluate_ip_state(state, count, config)
        history.append((state, count, current))
        state = current
    return history


class TestSection6Examples:
    """The worked example in section 6, asserted literally."""

    @pytest.mark.parametrize("count", [0, 799, 800, 999])
    def test_cold_stays_cold_below_the_hot_threshold(self, count):
        assert evaluate_ip_state(IpState.COLD, count, EXAMPLE_CONFIG) is IpState.COLD

    @pytest.mark.parametrize("count", [1000, 1001, 10_000])
    def test_cold_becomes_hot_at_or_above_the_hot_threshold(self, count):
        assert evaluate_ip_state(IpState.COLD, count, EXAMPLE_CONFIG) is IpState.HOT

    @pytest.mark.parametrize("count", [800, 900, 1000])
    def test_hot_stays_hot_at_or_above_the_cold_threshold(self, count):
        assert evaluate_ip_state(IpState.HOT, count, EXAMPLE_CONFIG) is IpState.HOT

    @pytest.mark.parametrize("count", [0, 1, 799])
    def test_hot_becomes_cold_below_the_cold_threshold(self, count):
        assert evaluate_ip_state(IpState.HOT, count, EXAMPLE_CONFIG) is IpState.COLD


class TestHysteresisInvariants:
    """Section 38's per-IP invariants, folded over arbitrary count sequences."""

    @given(configs_and_counts())
    def test_cold_to_hot_edges_require_the_hot_threshold(self, case):
        """COLD -> HOT only when count >= hot_threshold (section 38)."""
        config, counts = case
        for previous, count, current in fold(counts, config):
            if previous is IpState.COLD and current is IpState.HOT:
                assert count >= config.hot_threshold

    @given(configs_and_counts())
    def test_hot_to_cold_edges_require_a_count_below_the_cold_threshold(self, case):
        """HOT -> COLD only when count < cold_threshold (section 38)."""
        config, counts = case
        for previous, count, current in fold(counts, config):
            if previous is IpState.HOT and current is IpState.COLD:
                assert count < config.cold_threshold

    @given(configs_and_counts())
    def test_hot_state_implies_a_count_at_or_above_the_cold_threshold(self, case):
        """state == HOT => count is always >= cold_threshold (section 38)."""
        config, counts = case
        for _previous, count, current in fold(counts, config):
            if current is IpState.HOT:
                assert count >= config.cold_threshold

    @given(configs_and_counts())
    def test_counts_inside_the_hysteresis_band_retain_the_state(self, case):
        """cold_threshold <= count < hot_threshold retains the state (section 7)."""
        config, counts = case
        for previous, count, current in fold(counts, config):
            if config.cold_threshold <= count < config.hot_threshold:
                assert current is previous

    @given(configs_and_counts(), st.sampled_from(list(IpState)))
    def test_invariants_hold_from_either_initial_state(self, case, initial):
        """The invariants are properties of the machine, not of starting COLD."""
        config, counts = case
        assert_hysteresis_holds(fold(counts, config, initial=initial), config=config)


class TestNoOscillation:
    """Section 6/section 7: one count value cannot drive both directions."""

    @given(configs_and_counts())
    def test_no_count_value_drives_both_directions_in_a_fold(self, case):
        """A count that produced COLD -> HOT never produces HOT -> COLD."""
        config, counts = case
        promoting: set[int] = set()
        demoting: set[int] = set()
        for previous, count, current in fold(counts, config):
            if previous is IpState.COLD and current is IpState.HOT:
                promoting.add(count)
            if previous is IpState.HOT and current is IpState.COLD:
                demoting.add(count)
        assert promoting.isdisjoint(demoting)

    @given(configs_and_counts())
    def test_no_count_value_drives_both_directions_in_isolation(self, case):
        """The same holds for each count evaluated against both states."""
        config, counts = case
        for count in counts:
            promotes = evaluate_ip_state(IpState.COLD, count, config) is IpState.HOT
            demotes = evaluate_ip_state(IpState.HOT, count, config) is IpState.COLD
            assert not (promotes and demotes)

    @given(configs_and_counts())
    def test_a_constant_count_can_change_the_state_at_most_once(self, case):
        """Section 7: a repeated count cannot produce COLD -> HOT -> COLD ..."""
        config, counts = case
        for count in counts:
            history = fold([count] * 4, config)
            changes = [step for step in history if step[0] is not step[2]]
            assert len(changes) <= 1


class TestPurity:
    """Section 30: evaluate_ip_state is a function of (previous, count, config)."""

    @given(configs_and_counts())
    def test_repeated_evaluation_returns_the_same_state(self, case):
        """No hidden state: the same arguments always give the same answer."""
        config, counts = case
        for previous in IpState:
            for count in counts:
                first = evaluate_ip_state(previous, count, config)
                assert evaluate_ip_state(previous, count, config) is first

    @given(configs_and_counts())
    def test_evaluation_is_idempotent(self, case):
        """Re-evaluating the result against the same count is a fixed point."""
        config, counts = case
        for previous in IpState:
            for count in counts:
                once = evaluate_ip_state(previous, count, config)
                assert evaluate_ip_state(once, count, config) is once

    @given(configs_and_counts())
    def test_folding_the_same_sequence_twice_gives_the_same_history(self, case):
        """Replay and re-evaluation see identical results (section 30)."""
        config, counts = case
        assert fold(counts, config) == fold(counts, config)

    @given(configs_and_counts())
    def test_evaluation_order_does_not_matter(self, case):
        """Evaluating the same pairs in reverse order yields the same table."""
        config, counts = case
        pairs = [(previous, count) for previous in IpState for count in counts]
        forward = {pair: evaluate_ip_state(pair[0], pair[1], config) for pair in pairs}
        backward = {pair: evaluate_ip_state(pair[0], pair[1], config) for pair in reversed(pairs)}
        assert forward == backward


class TestZeroColdThreshold:
    """Section 6 read literally: count < 0 is unsatisfiable (ADR-0011 decision 3)."""

    @given(
        hot_threshold=st.integers(min_value=1, max_value=MAX_THRESHOLD),
        counts=st.lists(st.integers(min_value=0, max_value=MAX_COUNT), max_size=MAX_SEQUENCE),
    )
    def test_hot_never_returns_to_cold(self, hot_threshold, counts):
        """With cold_threshold == 0 a HOT IP stays HOT for every count."""
        config = DetectionConfig(hot_threshold=hot_threshold, cold_threshold=0)
        for previous, _count, current in fold(counts, config):
            if previous is IpState.HOT:
                assert current is IpState.HOT

    @given(
        hot_threshold=st.integers(min_value=1, max_value=MAX_THRESHOLD),
        counts=st.lists(st.integers(min_value=0, max_value=MAX_COUNT), max_size=MAX_SEQUENCE),
    )
    def test_hot_is_absorbing(self, hot_threshold, counts):
        """Once HOT appears in the history, every later state is HOT."""
        config = DetectionConfig(hot_threshold=hot_threshold, cold_threshold=0)
        states = [current for _, _, current in fold(counts, config)]
        if IpState.HOT in states:
            first_hot = states.index(IpState.HOT)
            assert all(state is IpState.HOT for state in states[first_hot:])

    @given(st.integers(min_value=1, max_value=MAX_THRESHOLD))
    def test_an_empty_window_does_not_demote_a_hot_ip(self, hot_threshold):
        """The degenerate case: even count == 0 leaves a HOT IP HOT."""
        config = DetectionConfig(hot_threshold=hot_threshold, cold_threshold=0)
        assert evaluate_ip_state(IpState.HOT, 0, config) is IpState.HOT


class TestSharedInvariantChecker:
    """`hammertime.testkit.invariants.assert_hysteresis_holds` (section 38)."""

    @given(configs_and_counts())
    def test_accepts_every_fold_of_the_real_state_machine(self, case):
        config, counts = case
        assert_hysteresis_holds(fold(counts, config), config=config)

    def test_rejects_a_cold_to_hot_edge_below_the_hot_threshold(self):
        history: list[HysteresisStep] = [(IpState.COLD, 999, IpState.HOT)]
        with pytest.raises(AssertionError):
            assert_hysteresis_holds(history, config=EXAMPLE_CONFIG)

    def test_rejects_a_hot_to_cold_edge_at_or_above_the_cold_threshold(self):
        history: list[HysteresisStep] = [
            (IpState.COLD, 1000, IpState.HOT),
            (IpState.HOT, 800, IpState.COLD),
        ]
        with pytest.raises(AssertionError):
            assert_hysteresis_holds(history, config=EXAMPLE_CONFIG)

    def test_rejects_a_history_that_is_not_a_contiguous_fold(self):
        history: list[HysteresisStep] = [
            (IpState.COLD, 1000, IpState.HOT),
            (IpState.COLD, 0, IpState.COLD),
        ]
        with pytest.raises(AssertionError):
            assert_hysteresis_holds(history, config=EXAMPLE_CONFIG)

    def test_accepts_the_section_6_worked_example(self):
        counts = [799, 800, 999, 1000, 1000, 900, 800, 799]
        assert_hysteresis_holds(fold(counts, EXAMPLE_CONFIG), config=EXAMPLE_CONFIG)
