"""No input sequence produces a transition that violates the hysteresis rules.

Spec: section 6 (the transition table and its worked example), section 7 (the
dead band and why it exists), section 30 (`evaluate_ip_state` MUST be the
authoritative implementation of the machine), section 38 (core invariants).

The machine under test is the pure function section 30 names:

    evaluate_ip_state(previous_state, count, configuration) -> IpState

Section 6 defines it completely:

    if state == COLD and count >= hot_threshold:   state = HOT
    elif state == HOT and count < cold_threshold:  state = COLD
    else:                                          state unchanged

so everything below is derived from those three lines plus
`cold_threshold < hot_threshold`, never from the implementation. The
interesting content is the *dead band* `cold_threshold <= count <
hot_threshold`, where section 6 requires the outcome to depend on the
previous state and explicitly forbids a single symmetric `count >=
threshold` comparison for both edges.

Assumption stated here because neither the spec nor ADR-0011 pins it down:
`IpState` (members `COLD` and `HOT`) is imported from
`hammertime.core.state.enums`, the module ADR-0010 names for
`PrefixState`; `evaluate_ip_state` from `hammertime.core.state.machine`, the
module ADR-0011 decision 4 names.
"""

from __future__ import annotations

import pytest
from hammertime.core.config.models import DetectionConfig
from hammertime.core.state.enums import IpState
from hammertime.core.state.machine import evaluate_ip_state
from hypothesis import given, settings
from hypothesis import strategies as st

# Section 6's / section 7's worked example.
HOT_THRESHOLD = 1000
COLD_THRESHOLD = 800
EXAMPLE_CONFIG = DetectionConfig(hot_threshold=HOT_THRESHOLD, cold_threshold=COLD_THRESHOLD)


class TestSectionSixTable:
    """The four-row table of section 6, with symbolic counts."""

    def test_cold_below_hot_threshold_stays_cold(self) -> None:
        assert evaluate_ip_state(IpState.COLD, HOT_THRESHOLD - 1, EXAMPLE_CONFIG) is IpState.COLD

    def test_cold_at_or_above_hot_threshold_becomes_hot(self) -> None:
        assert evaluate_ip_state(IpState.COLD, HOT_THRESHOLD, EXAMPLE_CONFIG) is IpState.HOT

    def test_hot_below_cold_threshold_becomes_cold(self) -> None:
        assert evaluate_ip_state(IpState.HOT, COLD_THRESHOLD - 1, EXAMPLE_CONFIG) is IpState.COLD

    def test_hot_at_or_above_cold_threshold_stays_hot(self) -> None:
        assert evaluate_ip_state(IpState.HOT, COLD_THRESHOLD, EXAMPLE_CONFIG) is IpState.HOT


class TestSectionSixWorkedExample:
    """The eight literal rows section 6 spells out for
    `hot_threshold = 1000`, `cold_threshold = 800`."""

    @pytest.mark.parametrize(
        ("previous", "count", "expected"),
        [
            (IpState.COLD, 799, IpState.COLD),
            (IpState.COLD, 800, IpState.COLD),
            (IpState.COLD, 999, IpState.COLD),
            (IpState.COLD, 1000, IpState.HOT),
            (IpState.HOT, 1000, IpState.HOT),
            (IpState.HOT, 900, IpState.HOT),
            (IpState.HOT, 800, IpState.HOT),
            (IpState.HOT, 799, IpState.COLD),
        ],
        ids=[
            "cold-799",
            "cold-800",
            "cold-999",
            "cold-1000",
            "hot-1000",
            "hot-900",
            "hot-800",
            "hot-799",
        ],
    )
    def test_row(self, previous: IpState, count: int, expected: IpState) -> None:
        assert evaluate_ip_state(previous, count, EXAMPLE_CONFIG) is expected


class TestDeadBandIsStateDependent:
    """Section 7: inside `cold_threshold <= count < hot_threshold` the
    existing state is retained -- which is exactly what a single symmetric
    `count >= threshold` comparison (forbidden by section 6) could not do."""

    @pytest.mark.parametrize("count", [800, 801, 900, 950, 999])
    def test_the_same_count_yields_a_different_state_for_each_previous_state(
        self, count: int
    ) -> None:
        assert evaluate_ip_state(IpState.COLD, count, EXAMPLE_CONFIG) is IpState.COLD
        assert evaluate_ip_state(IpState.HOT, count, EXAMPLE_CONFIG) is IpState.HOT

    def test_a_cold_ip_crossing_the_whole_dead_band_never_becomes_hot(self) -> None:
        state = IpState.COLD
        for count in range(COLD_THRESHOLD, HOT_THRESHOLD):
            state = evaluate_ip_state(state, count, EXAMPLE_CONFIG)
            assert state is IpState.COLD

    def test_a_hot_ip_oscillating_inside_the_dead_band_never_flaps(self) -> None:
        # Section 7's whole purpose: "COLD -> HOT -> COLD -> HOT -> COLD
        # oscillation caused by normal statistical noise" must not happen.
        state = evaluate_ip_state(IpState.COLD, HOT_THRESHOLD, EXAMPLE_CONFIG)
        assert state is IpState.HOT
        for count in (999, 801, 950, 800, 999, 800, 998):
            state = evaluate_ip_state(state, count, EXAMPLE_CONFIG)
            assert state is IpState.HOT

    def test_a_cold_threshold_of_zero_means_a_hot_ip_is_never_demoted(self) -> None:
        # Window counts are non-negative (section 5), so `count <
        # cold_threshold` is unsatisfiable at cold_threshold 0. The dead band
        # is then the whole range below hot_threshold.
        config = DetectionConfig(hot_threshold=10, cold_threshold=0)
        assert evaluate_ip_state(IpState.HOT, 0, config) is IpState.HOT
        assert evaluate_ip_state(IpState.COLD, 0, config) is IpState.COLD


@st.composite
def _configs(draw: st.DrawFn) -> DetectionConfig:
    """Configs whose only interesting fields are the two thresholds.

    Section 6: `cold_threshold < hot_threshold` MUST hold, so the cold
    threshold is drawn under the hot one. `hot_threshold >= 2` keeps the
    boundary sampler below distinct.
    """

    hot_threshold = draw(st.integers(min_value=2, max_value=10**6))
    cold_threshold = draw(st.integers(min_value=0, max_value=hot_threshold - 1))
    return DetectionConfig(hot_threshold=hot_threshold, cold_threshold=cold_threshold)


def _counts(config: DetectionConfig) -> st.SearchStrategy[int]:
    """Window counts, biased onto the boundaries where the rules bite."""

    boundaries = sorted(
        {
            0,
            max(0, config.cold_threshold - 1),
            config.cold_threshold,
            config.cold_threshold + 1,
            config.hot_threshold - 1,
            config.hot_threshold,
            config.hot_threshold + 1,
            2 * config.hot_threshold,
        }
    )
    return st.one_of(
        st.sampled_from(boundaries),
        st.integers(min_value=0, max_value=2 * config.hot_threshold),
    )


@st.composite
def _config_and_counts(draw: st.DrawFn) -> tuple[DetectionConfig, list[int]]:
    config = draw(_configs())
    counts = draw(st.lists(_counts(config), min_size=1, max_size=32))
    return config, counts


@st.composite
def _config_and_count(draw: st.DrawFn) -> tuple[DetectionConfig, int]:
    config = draw(_configs())
    return config, draw(_counts(config))


def _run(config: DetectionConfig, counts: list[int]) -> list[tuple[IpState, int, IpState]]:
    """Feed `counts` through the machine starting COLD.

    Returns one `(previous, count, new)` triple per evaluation, in order. An
    IP is COLD before it has ever been observed (ADR-0011 decision 2:
    `ShardWindow.state` is COLD when untracked), so COLD is the only
    legitimate starting state.
    """

    state = IpState.COLD
    steps: list[tuple[IpState, int, IpState]] = []
    for count in counts:
        new_state = evaluate_ip_state(state, count, config)
        steps.append((state, count, new_state))
        state = new_state
    return steps


@given(config_and_counts=_config_and_counts())
@settings(deadline=None, max_examples=200)
def test_cold_to_hot_only_when_count_reaches_hot_threshold(
    config_and_counts: tuple[DetectionConfig, list[int]],
) -> None:
    """Section 38: `COLD -> HOT only when count >= hot_threshold`."""

    config, counts = config_and_counts
    for previous, count, new_state in _run(config, counts):
        if previous is IpState.COLD and new_state is IpState.HOT:
            assert count >= config.hot_threshold


@given(config_and_counts=_config_and_counts())
@settings(deadline=None, max_examples=200)
def test_hot_to_cold_only_when_count_falls_below_cold_threshold(
    config_and_counts: tuple[DetectionConfig, list[int]],
) -> None:
    """Section 38: `HOT -> COLD only when count < cold_threshold`."""

    config, counts = config_and_counts
    for previous, count, new_state in _run(config, counts):
        if previous is IpState.HOT and new_state is IpState.COLD:
            assert count < config.cold_threshold


@given(config_and_counts=_config_and_counts())
@settings(deadline=None, max_examples=200)
def test_the_only_edges_are_cold_to_hot_and_hot_to_cold(
    config_and_counts: tuple[DetectionConfig, list[int]],
) -> None:
    config, counts = config_and_counts
    for previous, _count, new_state in _run(config, counts):
        assert new_state in (IpState.COLD, IpState.HOT)
        if new_state is not previous:
            assert {previous, new_state} == {IpState.COLD, IpState.HOT}


@given(config_and_counts=_config_and_counts())
@settings(deadline=None, max_examples=200)
def test_no_edge_occurs_inside_the_hysteresis_dead_band(
    config_and_counts: tuple[DetectionConfig, list[int]],
) -> None:
    """Sections 6 and 7: inside `cold_threshold <= count < hot_threshold` the
    existing state is retained, in either direction. This is the whole point
    of having two thresholds."""

    config, counts = config_and_counts
    for previous, count, new_state in _run(config, counts):
        if config.cold_threshold <= count < config.hot_threshold:
            assert new_state is previous


@given(config_and_counts=_config_and_counts())
@settings(deadline=None, max_examples=200)
def test_hot_implies_the_last_state_changing_count_reached_hot_threshold(
    config_and_counts: tuple[DetectionConfig, list[int]],
) -> None:
    """Section 38 read as a statement about the run, not a single step: an IP
    can only be HOT because some earlier count crossed `hot_threshold`, and
    nothing since then has changed its state."""

    config, counts = config_and_counts
    last_edge_count: int | None = None
    for previous, count, new_state in _run(config, counts):
        if new_state is not previous:
            last_edge_count = count
        if new_state is IpState.HOT:
            assert last_edge_count is not None
            assert last_edge_count >= config.hot_threshold


@given(config_and_counts=_config_and_counts())
@settings(deadline=None, max_examples=200)
def test_hot_implies_the_current_count_is_at_least_cold_threshold(
    config_and_counts: tuple[DetectionConfig, list[int]],
) -> None:
    """Section 38: `state == HOT => count is always >= cold_threshold`."""

    config, counts = config_and_counts
    for _previous, count, new_state in _run(config, counts):
        if new_state is IpState.HOT:
            assert count >= config.cold_threshold


@given(
    config_and_count=_config_and_count(),
    previous=st.sampled_from([IpState.COLD, IpState.HOT]),
)
@settings(deadline=None, max_examples=200)
def test_evaluation_is_pure(
    config_and_count: tuple[DetectionConfig, int], previous: IpState
) -> None:
    """The same `(state, count, config)` always yields the same result --
    section 30's "single state-transition function" is only authoritative if
    it carries no hidden state of its own."""

    config, count = config_and_count
    first = evaluate_ip_state(previous, count, config)
    second = evaluate_ip_state(previous, count, config)
    assert second is first


@given(
    config_and_count=_config_and_count(),
    previous=st.sampled_from([IpState.COLD, IpState.HOT]),
)
@settings(deadline=None, max_examples=200)
def test_evaluation_is_idempotent(
    config_and_count: tuple[DetectionConfig, int], previous: IpState
) -> None:
    """Re-evaluating the result against the same count is a fixed point, so a
    re-evaluation pass (section 34, ADR-0011 decision 7) over unchanged
    counts emits nothing the first pass did not."""

    config, count = config_and_count
    once = evaluate_ip_state(previous, count, config)
    assert evaluate_ip_state(once, count, config) is once


@given(config_and_count=_config_and_count())
@settings(deadline=None, max_examples=200)
def test_a_repeated_count_produces_at_most_one_edge(
    config_and_count: tuple[DetectionConfig, int],
) -> None:
    """Corollary of idempotence: a stream that never changes the count can
    move an IP at most once, so a steady count cannot oscillate."""

    config, count = config_and_count
    steps = _run(config, [count] * 8)
    edges = [step for step in steps if step[2] is not step[0]]
    assert len(edges) <= 1
