"""The per-IP `weight` attribute and the function that produces it.

Spec: section 46.4 (the `threshold_ratio` formula, its integer arithmetic and
the `weight_max` clamp), section 46.2/46.3 (the attribute document and the
registered `weight` name, integer 0..1000000), section 34 (`weight_function`
and `weight_max` as configuration), ADR-0005, ADR-0011 decision 4.

`hammertime.core.state.weight` is new, so -- as with test_codec.py -- this
file also states the public surface it is written against. ADR-0011
decision 4 fixes it exactly:

    threshold_ratio(window_count: int, config: DetectionConfig) -> int
    compute_weight(window_count: int, config: DetectionConfig) -> int
    transition_attributes(window_count: int, config: DetectionConfig) -> dict[str, object]

and spec section 46.4 fixes what `threshold_ratio` computes:

    clamp(
        (1000 * window_count + config.hot_threshold // 2) // config.hot_threshold,
        0,
        config.weight_max,
    )

Two properties of that definition are load-bearing and get their own tests
below rather than being left implicit in the formula:

* The arithmetic is integer throughout -- section 46.4: "replay MUST
  reproduce the same value bit for bit, which floating point does not
  guarantee across platforms". `weight` also travels on the wire as a JSON
  integer (schemas/ip_attributes.v1.json), so a float result would be a
  wire-format change, not an internal detail.
* `+ hot_threshold // 2` before the floor division is what makes the
  rounding half-up in thousandths of `hot_threshold`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded
from hammertime.core.state.weight import compute_weight, threshold_ratio, transition_attributes
from hypothesis import given, settings
from hypothesis import strategies as st

T0 = datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC)

# schemas/ip_attributes.v1.json bounds `weight` to 0..1000000, and
# schemas/detection_config.v1.json bounds `weight_max` to 1000..1000000.
REGISTRY_WEIGHT_MAX = 1_000_000


class TestSpecAnchorValues:
    """Section 46.4's own worked equivalences:

        weight = 1000   <=>   window count exactly at hot_threshold
        weight = 2500   <=>   window count 2.5x hot_threshold
    """

    def test_count_exactly_at_the_default_hot_threshold_is_one_thousand(self) -> None:
        assert threshold_ratio(1000, DetectionConfig()) == 1000

    def test_count_exactly_at_a_non_default_hot_threshold_is_also_one_thousand(self) -> None:
        # The unit is thousandths *of hot_threshold*, so "at the threshold"
        # is 1000 whatever the threshold happens to be.
        config = DetectionConfig(hot_threshold=500, cold_threshold=400)
        assert threshold_ratio(500, config) == 1000

    def test_two_and_a_half_times_the_hot_threshold_is_2500(self) -> None:
        assert threshold_ratio(2500, DetectionConfig()) == 2500

    def test_one_point_two_times_a_non_default_hot_threshold(self) -> None:
        # 600 requests against hot_threshold 500 is 1.2x -> 1200 thousandths.
        config = DetectionConfig(hot_threshold=500, cold_threshold=400)
        assert threshold_ratio(600, config) == 1200

    def test_one_point_two_times_the_default_hot_threshold(self) -> None:
        assert threshold_ratio(1200, DetectionConfig()) == 1200

    def test_five_hundred_times_the_default_hot_threshold(self) -> None:
        # Still well inside the default weight_max (1000000), so no clamping.
        assert threshold_ratio(500_000, DetectionConfig()) == 500_000


class TestHalfUpRoundingInThousandths:
    """`+ hot_threshold // 2` before the floor division rounds half *up*.

    The distinguishing case is `window_count == 1` against
    `hot_threshold == 2000`: the exact ratio is 0.5 thousandths, which
    half-up rounds to 1 and round-half-to-even (Python's own `round`) would
    round to 0.
    """

    @pytest.mark.parametrize(
        ("window_count", "hot_threshold", "expected"),
        [
            (0, 1000, 0),
            (1, 1000, 1),
            (1001, 1000, 1001),
            (1, 2000, 1),
            (3, 2000, 2),
            (1, 3000, 0),
            (2, 3000, 1),
            (1, 2001, 0),
            (1, 1, 1000),
            (1, 3, 333),
            (2, 3, 667),
        ],
        ids=[
            "zero-count-is-zero",
            "one-thousandth-exactly",
            "one-above-threshold",
            "exactly-half-rounds-up",
            "one-and-a-half-rounds-up",
            "one-third-rounds-down",
            "two-thirds-rounds-up",
            "just-under-half-rounds-down",
            "threshold-of-one",
            "recurring-third-rounds-down",
            "recurring-two-thirds-rounds-up",
        ],
    )
    def test_rounding(self, window_count: int, hot_threshold: int, expected: int) -> None:
        config = DetectionConfig(hot_threshold=hot_threshold, cold_threshold=0)
        assert threshold_ratio(window_count, config) == expected

    def test_a_zero_window_count_is_never_negative(self) -> None:
        # The lower clamp bound of section 46.4. Window counts are
        # non-negative (section 5), so 0 is the floor the clamp protects.
        config = DetectionConfig(hot_threshold=10**6, cold_threshold=0)
        assert threshold_ratio(0, config) == 0


class TestWeightMaxClamp:
    def test_a_count_far_above_the_threshold_clamps_to_weight_max(self) -> None:
        config = DetectionConfig(hot_threshold=1000, cold_threshold=800, weight_max=5000)
        # Unclamped this would be 10000 thousandths (10x the hot threshold).
        assert threshold_ratio(10_000, config) == 5000

    def test_a_value_exactly_at_weight_max_is_not_reduced(self) -> None:
        config = DetectionConfig(hot_threshold=1000, cold_threshold=800, weight_max=5000)
        assert threshold_ratio(5_000, config) == 5000

    def test_a_value_just_below_weight_max_is_untouched(self) -> None:
        config = DetectionConfig(hot_threshold=1000, cold_threshold=800, weight_max=5000)
        assert threshold_ratio(4_999, config) == 4999

    def test_the_default_weight_max_is_the_registry_upper_bound(self) -> None:
        # Section 46.3: weight is "integer 0..1000000".
        config = DetectionConfig()
        assert config.weight_max == REGISTRY_WEIGHT_MAX
        assert threshold_ratio(10**12, config) == REGISTRY_WEIGHT_MAX

    def test_clamping_uses_the_configured_maximum_not_the_registry_maximum(self) -> None:
        config = DetectionConfig(hot_threshold=1000, cold_threshold=800, weight_max=1000)
        assert threshold_ratio(10**9, config) == 1000


class TestResultIsAnInteger:
    """Section 46.4: integer arithmetic throughout.

    `weight` is serialized as a JSON integer, so a float leaking out of this
    function would change the wire format (and break bit-for-bit replay),
    not merely the Python type a caller sees.
    """

    @pytest.mark.parametrize("window_count", [0, 1, 999, 1000, 1001, 2_500, 10**9])
    def test_threshold_ratio_returns_an_int(self, window_count: int) -> None:
        value = threshold_ratio(window_count, DetectionConfig())
        assert type(value) is int

    @pytest.mark.parametrize("window_count", [0, 1, 1000, 10**9])
    def test_compute_weight_returns_an_int(self, window_count: int) -> None:
        value = compute_weight(window_count, DetectionConfig())
        assert type(value) is int

    def test_a_clamped_result_is_also_an_int(self) -> None:
        config = DetectionConfig(hot_threshold=1000, cold_threshold=800, weight_max=5000)
        assert type(threshold_ratio(10**9, config)) is int

    def test_an_odd_hot_threshold_does_not_produce_a_float(self) -> None:
        # The most likely way a float sneaks in is `/` instead of `//`;
        # an odd threshold makes the division inexact.
        config = DetectionConfig(hot_threshold=333, cold_threshold=0)
        assert type(threshold_ratio(1, config)) is int


@st.composite
def _weight_configs(draw: st.DrawFn) -> DetectionConfig:
    """Configs varying only the two fields section 46.4's formula reads.

    `cold_threshold` is pinned to 0 so that every drawn `hot_threshold`
    (minimum 1 per schemas/detection_config.v1.json) satisfies
    `cold_threshold < hot_threshold` (section 6).
    """

    hot_threshold = draw(st.integers(min_value=1, max_value=10**6))
    weight_max = draw(st.integers(min_value=1000, max_value=10**6))
    return DetectionConfig(hot_threshold=hot_threshold, cold_threshold=0, weight_max=weight_max)


@given(window_count=st.integers(min_value=0, max_value=10**9), config=_weight_configs())
@settings(deadline=None)
def test_threshold_ratio_equals_the_section_46_4_formula(
    window_count: int, config: DetectionConfig
) -> None:
    expected = min(
        max((1000 * window_count + config.hot_threshold // 2) // config.hot_threshold, 0),
        config.weight_max,
    )
    assert threshold_ratio(window_count, config) == expected


@given(window_count=st.integers(min_value=0, max_value=10**9), config=_weight_configs())
@settings(deadline=None)
def test_threshold_ratio_stays_inside_the_registered_weight_range(
    window_count: int, config: DetectionConfig
) -> None:
    value = threshold_ratio(window_count, config)
    assert type(value) is int
    assert 0 <= value <= config.weight_max
    assert 0 <= value <= REGISTRY_WEIGHT_MAX


@given(window_count=st.integers(min_value=0, max_value=10**9), config=_weight_configs())
@settings(deadline=None)
def test_threshold_ratio_is_monotone_in_the_window_count(
    window_count: int, config: DetectionConfig
) -> None:
    # "How hot is this address" must never decrease as the count grows.
    assert threshold_ratio(window_count, config) <= threshold_ratio(window_count + 1, config)


class TestComputeWeightDispatch:
    """Section 34/46.4: `weight_function` selects the function; v1 knows only
    `threshold_ratio`, which is also `DetectionConfig`'s default."""

    def test_the_default_config_selects_threshold_ratio(self) -> None:
        assert DetectionConfig().weight_function == "threshold_ratio"

    @pytest.mark.parametrize("window_count", [0, 1, 999, 1000, 1001, 2_500, 10**9])
    def test_dispatches_to_threshold_ratio(self, window_count: int) -> None:
        config = DetectionConfig(weight_function="threshold_ratio")
        assert compute_weight(window_count, config) == threshold_ratio(window_count, config)

    def test_dispatches_to_threshold_ratio_under_a_non_default_threshold(self) -> None:
        config = DetectionConfig(
            hot_threshold=500, cold_threshold=400, weight_function="threshold_ratio"
        )
        assert compute_weight(600, config) == 1200


@given(window_count=st.integers(min_value=0, max_value=10**9), config=_weight_configs())
@settings(deadline=None)
def test_compute_weight_agrees_with_threshold_ratio_for_the_v1_function(
    window_count: int, config: DetectionConfig
) -> None:
    assert compute_weight(window_count, config) == threshold_ratio(window_count, config)


class TestTransitionAttributes:
    """ADR-0011 decision 4: the document the aggregator attaches to a
    `HotIpAdded` is exactly `attributes_version` plus `weight`."""

    def test_document_is_version_one_and_the_weight(self) -> None:
        assert transition_attributes(1500, DetectionConfig()) == {
            "attributes_version": 1,
            "weight": 1500,
        }

    def test_document_carries_no_other_keys(self) -> None:
        assert set(transition_attributes(0, DetectionConfig())) == {
            "attributes_version",
            "weight",
        }

    def test_weight_is_compute_weight_of_the_same_count_and_config(self) -> None:
        config = DetectionConfig(hot_threshold=500, cold_threshold=400)
        assert transition_attributes(600, config)["weight"] == compute_weight(600, config)

    def test_weight_is_an_int_in_the_document_too(self) -> None:
        assert type(transition_attributes(1234, DetectionConfig())["weight"]) is int

    def test_attributes_version_is_the_registry_generation_not_the_config_version(self) -> None:
        # Section 46.2/46.3: `attributes_version` is the generation of the
        # attribute registry, independent of DetectionConfig.config_version.
        config = DetectionConfig(config_version=7)
        assert transition_attributes(1000, config)["attributes_version"] == 1

    def test_a_zero_window_count_still_produces_a_weight_key(self) -> None:
        # A weight of 0 is a value, not an absence: section 46.2's "absent
        # attributes is equivalent to {"attributes_version": 1}" must not be
        # reachable by a falsy weight being dropped.
        assert transition_attributes(0, DetectionConfig()) == {
            "attributes_version": 1,
            "weight": 0,
        }


def _hot_ip_added_envelope(attributes: dict[str, object]) -> EventEnvelope[HotIpAdded]:
    payload = HotIpAdded(
        ip=Address.parse("10.20.30.1"),
        timestamp=T0,
        sequence=1,
        window_count=1500,
        config_version=1,
        attributes=attributes,
    )
    return EventEnvelope(
        agent_id="aggregator-shard-0",
        sequence=1,
        event_type="HotIpAdded",
        config_version=1,
        timestamp=T0,
        payload=payload,
    )


class TestTransitionAttributesOnTheWire:
    """Section 46.5: attributes travel on the transition event. The codec
    validates them against schemas/ip_attributes.v1.json (test_ip_attributes.py),
    so anything `transition_attributes` can produce must survive a round trip
    -- a `CodecError` here would mean the aggregator cannot publish its own
    transitions."""

    @pytest.mark.parametrize(
        ("window_count", "hot_threshold", "weight_max"),
        [
            (0, 1000, 1_000_000),
            (1000, 1000, 1_000_000),
            (10**9, 1000, 1_000_000),
            (10**9, 1, 1000),
            (7, 3, 1_000_000),
        ],
        ids=["zero", "at-threshold", "huge-count", "tiny-threshold", "rounded"],
    )
    def test_round_trips_through_the_codec(
        self, window_count: int, hot_threshold: int, weight_max: int
    ) -> None:
        config = DetectionConfig(
            hot_threshold=hot_threshold, cold_threshold=0, weight_max=weight_max
        )
        attributes = transition_attributes(window_count, config)

        decoded = decode(encode(_hot_ip_added_envelope(attributes)))

        payload = decoded.payload
        assert isinstance(payload, HotIpAdded)
        assert payload.attributes == attributes

    def test_the_clamp_keeps_weight_inside_the_codec_accepted_range(self) -> None:
        # weight_max's schema maximum and weight's schema maximum are the
        # same number, so a clamped weight can never be rejected on the wire
        # however large the window count is.
        config = DetectionConfig(weight_max=REGISTRY_WEIGHT_MAX)
        attributes = transition_attributes(10**15, config)
        assert attributes["weight"] == REGISTRY_WEIGHT_MAX

        decoded = decode(encode(_hot_ip_added_envelope(attributes)))

        payload = decoded.payload
        assert isinstance(payload, HotIpAdded)
        assert payload.attributes == attributes


@given(window_count=st.integers(min_value=0, max_value=10**9), config=_weight_configs())
@settings(deadline=None, max_examples=50)
def test_any_transition_document_survives_the_codec(
    window_count: int, config: DetectionConfig
) -> None:
    attributes = transition_attributes(window_count, config)

    decoded = decode(encode(_hot_ip_added_envelope(attributes)))

    payload = decoded.payload
    assert isinstance(payload, HotIpAdded)
    assert payload.attributes == attributes
