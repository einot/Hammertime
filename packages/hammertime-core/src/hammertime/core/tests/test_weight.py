"""`weight` and the `IpAttributes` document the aggregator attaches at a transition.

Spec: section 46.2, section 46.4, section 46.5 (ADR-0005, ADR-0011 decision 6;
schemas/ip_attributes.v1.json, schemas/detection_config.v1.json)

Every expected value in this module is computed from spec section 46.4's
normative definition, never from the implementation:

    threshold_ratio(window_count, config) =
        clamp((1000 * window_count + config.hot_threshold // 2)
              // config.hot_threshold, 0, config.weight_max)

`_model` below is that formula transcribed once, in Python, and the exact
values asserted in `TestThresholdRatioWorkedValues` are written out literally
so a reader can check them by hand against the same formula.

Integer arithmetic is load-bearing: section 46.4 requires replay to reproduce
`weight` bit for bit, "which floating point does not guarantee across
platforms", so the tests assert the *type* of the result as well as its value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from hammertime.core.addressing.address import Address
from hammertime.core.attributes import (
    ATTRIBUTES_VERSION,
    build_attributes,
    compute_weight,
    threshold_ratio,
)
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded
from hypothesis import given
from hypothesis import strategies as st

T0 = datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC)

# schemas/ip_attributes.v1.json: weight is an integer 0..1000000.
SCHEMA_WEIGHT_MAXIMUM = 1000000

# schemas/detection_config.v1.json: weight_max is an integer 1000..1000000.
CONFIG_WEIGHT_MAX_MINIMUM = 1000


def _model(window_count: int, hot_threshold: int, weight_max: int) -> int:
    """Spec section 46.4's formula, transcribed. Integer arithmetic only."""

    ratio = (1000 * window_count + hot_threshold // 2) // hot_threshold
    return min(max(ratio, 0), weight_max)


def _config(
    *,
    hot_threshold: int = 1000,
    weight_max: int = 1000000,
    cold_threshold: int | None = None,
    minimum_hot_ips: int = 16,
) -> DetectionConfig:
    # `cold_threshold` must stay strictly below `hot_threshold`; it plays no
    # part in section 46.4's formula, so it is simply kept legal here (the
    # default 800 whenever `hot_threshold` leaves room for it).
    if cold_threshold is None:
        cold_threshold = min(800, hot_threshold - 1)
    return DetectionConfig(
        hot_threshold=hot_threshold,
        cold_threshold=cold_threshold,
        weight_max=weight_max,
        minimum_hot_ips=minimum_hot_ips,
    )


class TestThresholdRatioWorkedValues:
    """Spec section 46.4: "weight = 1000 <=> window count exactly at
    hot_threshold; weight = 2500 <=> window count 2.5x hot_threshold"."""

    @pytest.mark.parametrize(
        ("window_count", "expected"),
        [
            # (1000 * wc + 1000 // 2) // 1000, i.e. (1000 * wc + 500) // 1000.
            (1000, 1000),  # exactly at hot_threshold
            (0, 0),  # (0 + 500) // 1000
            (1200, 1200),  # 1.2x hot_threshold
            (2500, 2500),  # 2.5x hot_threshold
            (1, 1),  # (1000 + 500) // 1000
            (1499, 1499),
            (1500, 1500),
        ],
    )
    def test_exact_values_at_the_default_hot_threshold(
        self, window_count: int, expected: int
    ) -> None:
        config = _config()  # hot_threshold=1000, weight_max=1000000
        assert threshold_ratio(window_count, config) == expected
        assert expected == _model(window_count, 1000, 1000000)

    def test_exact_value_at_a_lower_hot_threshold(self) -> None:
        # hot_threshold=500: (1000 * 600 + 250) // 500 = 600250 // 500 = 1200,
        # i.e. 600 requests is 1.2x a 500-request threshold.
        config = _config(hot_threshold=500)
        assert threshold_ratio(600, config) == 1200
        assert _model(600, 500, 1000000) == 1200

    @pytest.mark.parametrize(
        ("window_count", "expected"),
        [
            # hot_threshold=3, so hot_threshold // 2 == 1:
            #   wc=1 -> (1000 + 1) // 3 == 1001 // 3 == 333
            #   wc=2 -> (2000 + 1) // 3 == 2001 // 3 == 667
            #   wc=3 -> (3000 + 1) // 3 == 3001 // 3 == 1000
            (1, 333),
            (2, 667),
            (3, 1000),
        ],
    )
    def test_exact_values_at_a_tiny_hot_threshold(self, window_count: int, expected: int) -> None:
        # NOTE: ADR-0011 decision 6's worked list prints `(1) == 334` while
        # annotating it `(1000 + 1) // 3`, which is 333 -- an arithmetic slip
        # in the ADR's prose. Spec section 46.4's formula (and
        # schemas/ip_attributes.v1.json's restatement of it) is normative and
        # yields 333: 1000/3 = 333.33, which rounds *down* under any rounding
        # rule. This test follows the formula.
        config = _config(hot_threshold=3)
        assert threshold_ratio(window_count, config) == expected
        assert _model(window_count, 3, 1000000) == expected


class TestRoundingIsHalfUpIntegerArithmetic:
    """Section 46.4: "Integer arithmetic throughout: replay MUST reproduce the
    same value bit for bit, which floating point does not guarantee"."""

    @pytest.mark.parametrize(
        ("window_count", "hot_threshold"),
        [
            (1, 16),  # 1000/16 = 62.5 -> 63 (half-up), not 62 (half-even)
            (3, 16),  # 3000/16 = 187.5 -> 188
            (1, 2000),  # 1000/2000 = 0.5 -> 1, not 0
            (3, 2000),  # 3000/2000 = 1.5 -> 2
            (1, 3),
            (2, 3),
            (7, 4),
            (999, 1000),
            (1001, 1000),
            (12345, 777),
            (5, 100000),
            (999999, 1000000),
        ],
    )
    def test_matches_the_specified_integer_formula(
        self, window_count: int, hot_threshold: int
    ) -> None:
        config = _config(hot_threshold=hot_threshold)
        assert threshold_ratio(window_count, config) == _model(
            window_count, hot_threshold, config.weight_max
        )

    @pytest.mark.parametrize(
        ("window_count", "hot_threshold", "expected"),
        [
            (1, 16, 63),  # exactly .5 -> rounds away from zero (up)
            (1, 2000, 1),
            (3, 2000, 2),
        ],
    )
    def test_an_exact_half_rounds_up_not_to_even(
        self, window_count: int, hot_threshold: int, expected: int
    ) -> None:
        config = _config(hot_threshold=hot_threshold)
        assert threshold_ratio(window_count, config) == expected

    @pytest.mark.parametrize(
        ("window_count", "hot_threshold"), [(0, 1000), (1, 3), (1200, 1000), (10**9, 1000)]
    )
    def test_result_is_an_int_never_a_float(self, window_count: int, hot_threshold: int) -> None:
        config = _config(hot_threshold=hot_threshold)
        result = threshold_ratio(window_count, config)
        assert type(result) is int
        assert not isinstance(result, float)


class TestClamping:
    def test_clamped_to_a_lowered_weight_max(self) -> None:
        # weight_max=5000: the raw ratio is (1000 * 10000 + 500) // 1000 =
        # 10000, well above the clamp.
        config = _config(weight_max=5000)
        assert _model(10_000, 1000, 5000) == 5000
        assert threshold_ratio(10_000, config) == 5000

    def test_clamped_to_the_default_weight_max_which_is_the_schema_maximum(self) -> None:
        # Section 46.4 / ADR-0011 decision 6: "(10**9) at the default clamps
        # to 1_000_000, which is exactly the schema's maximum, so a clamped
        # weight is always encodable."
        config = _config()
        assert threshold_ratio(10**9, config) == 1000000
        assert config.weight_max == SCHEMA_WEIGHT_MAXIMUM

    def test_weight_max_bounds_never_exceed_the_ip_attributes_schema_bounds(self) -> None:
        # schemas/detection_config.v1.json caps weight_max at 1000000, which
        # is schemas/ip_attributes.v1.json's maximum for `weight`, so a
        # clamped weight is always within the wire schema's bounds.
        config = _config(weight_max=SCHEMA_WEIGHT_MAXIMUM)
        assert 0 <= threshold_ratio(10**12, config) <= SCHEMA_WEIGHT_MAXIMUM

    @given(
        window_count=st.integers(min_value=0, max_value=10**9),
        hot_threshold=st.integers(min_value=1, max_value=10**6),
        weight_max=st.integers(
            min_value=CONFIG_WEIGHT_MAX_MINIMUM, max_value=SCHEMA_WEIGHT_MAXIMUM
        ),
    )
    def test_result_always_within_zero_and_weight_max(
        self, window_count: int, hot_threshold: int, weight_max: int
    ) -> None:
        config = _config(hot_threshold=hot_threshold, weight_max=weight_max)
        result = threshold_ratio(window_count, config)
        assert 0 <= result <= weight_max
        assert 0 <= result <= SCHEMA_WEIGHT_MAXIMUM
        assert result == _model(window_count, hot_threshold, weight_max)


class TestNegativeWindowCountIsAProgrammingError:
    """Section 46.4: "a negative window_count is a programming error
    (ValueError), never clamped"."""

    @pytest.mark.parametrize("window_count", [-1, -2, -1000])
    def test_threshold_ratio_rejects_a_negative_window_count(self, window_count: int) -> None:
        with pytest.raises(ValueError):
            threshold_ratio(window_count, _config())

    @pytest.mark.parametrize("window_count", [-1, -1000])
    def test_compute_weight_rejects_a_negative_window_count(self, window_count: int) -> None:
        # compute_weight dispatches to threshold_ratio (ADR-0011 decision 6),
        # so the same programming error surfaces through the dispatcher.
        with pytest.raises(ValueError):
            compute_weight(window_count, _config())


class TestDeterminism:
    """Section 46.4: "replay MUST reproduce the same value bit for bit"."""

    @pytest.mark.parametrize("window_count", [0, 1, 1200, 2500, 10**9])
    def test_repeated_calls_return_the_same_value(self, window_count: int) -> None:
        config = _config()
        results = {threshold_ratio(window_count, config) for _ in range(10)}
        assert len(results) == 1

    def test_two_equal_configs_produce_the_same_value(self) -> None:
        assert threshold_ratio(1234, _config()) == threshold_ratio(1234, _config())

    def test_weight_does_not_depend_on_cold_threshold(self) -> None:
        # Only hot_threshold and weight_max appear in section 46.4's formula.
        low = _config(cold_threshold=0)
        high = _config(cold_threshold=999)
        assert threshold_ratio(1200, low) == threshold_ratio(1200, high) == 1200

    def test_weight_does_not_depend_on_minimum_hot_ips(self) -> None:
        # minimum_hot_ips is a prefix-predicate field (section 13), not an
        # input to weight.
        few = _config(minimum_hot_ips=1)
        many = _config(minimum_hot_ips=1000)
        assert threshold_ratio(2500, few) == threshold_ratio(2500, many) == 2500

    def test_weight_does_depend_on_hot_threshold_and_weight_max(self) -> None:
        # The contrapositive of the two tests above: the fields the formula
        # does name are genuinely read.
        assert threshold_ratio(600, _config(hot_threshold=500)) == 1200
        assert threshold_ratio(600, _config(hot_threshold=1000)) == 600
        assert threshold_ratio(10_000, _config(weight_max=5000)) == 5000


class TestComputeWeightDispatch:
    """ADR-0011 decision 6: compute_weight dispatches on
    config.weight_function; v1 knows only "threshold_ratio"
    (schemas/detection_config.v1.json's enum has exactly that member)."""

    @pytest.mark.parametrize("window_count", [0, 1, 1000, 1200, 2500, 10**9])
    def test_matches_threshold_ratio_for_the_only_v1_weight_function(
        self, window_count: int
    ) -> None:
        config = _config()
        assert config.weight_function == "threshold_ratio"
        assert compute_weight(window_count, config) == threshold_ratio(window_count, config)

    def test_matches_threshold_ratio_under_a_non_default_configuration(self) -> None:
        config = _config(hot_threshold=500, weight_max=5000)
        assert compute_weight(600, config) == threshold_ratio(600, config) == 1200
        assert compute_weight(10_000, config) == threshold_ratio(10_000, config) == 5000

    def test_result_is_an_int(self) -> None:
        assert type(compute_weight(1200, _config())) is int


class TestBuildAttributes:
    """Section 46.2/46.3: the document is `{"attributes_version": 1,
    "weight": <integer 0..1000000>}`; section 46.5: it is what the aggregator
    attaches to HotIpAdded."""

    def test_attributes_version_constant_is_one(self) -> None:
        assert ATTRIBUTES_VERSION == 1

    def test_document_for_the_worked_example(self) -> None:
        assert build_attributes(1200, _config()) == {"attributes_version": 1, "weight": 1200}

    def test_document_carries_exactly_the_two_registered_names(self) -> None:
        document = build_attributes(1200, _config())
        assert set(document) == {"attributes_version", "weight"}
        assert document["attributes_version"] == ATTRIBUTES_VERSION

    @pytest.mark.parametrize("window_count", [0, 1, 1000, 2500, 10**9])
    def test_weight_is_compute_weight_of_the_same_inputs(self, window_count: int) -> None:
        config = _config()
        document = build_attributes(window_count, config)
        assert document["weight"] == compute_weight(window_count, config)

    def test_weight_is_clamped_in_the_document_too(self) -> None:
        assert build_attributes(10_000, _config(weight_max=5000)) == {
            "attributes_version": 1,
            "weight": 5000,
        }

    @pytest.mark.parametrize("window_count", [-1, -1000])
    def test_negative_window_count_is_rejected(self, window_count: int) -> None:
        with pytest.raises(ValueError):
            build_attributes(window_count, _config())


def _hot_ip_envelope(attributes: dict[str, Any] | None) -> EventEnvelope[HotIpAdded]:
    # Same envelope construction as test_ip_attributes.py's helper.
    payload = HotIpAdded(
        ip=Address.parse("10.0.0.1"),
        timestamp=T0,
        sequence=1,
        window_count=1200,
        config_version=1,
        attributes=attributes,
    )
    return EventEnvelope(
        agent_id="shard-3",
        sequence=1,
        event_type="HotIpAdded",
        config_version=1,
        timestamp=T0,
        payload=payload,
    )


class TestBuiltDocumentIsEncodable:
    """Section 46.5: attributes travel on the transition event; the produced
    document must therefore satisfy schemas/ip_attributes.v1.json as enforced
    by the codec."""

    @pytest.mark.parametrize(
        ("window_count", "hot_threshold", "weight_max"),
        [
            (1200, 1000, 1000000),
            (0, 1000, 1000000),
            (10**9, 1000, 1000000),  # clamps to the schema maximum
            (10_000, 1000, 5000),
            (600, 500, 1000000),
            (1, 3, 1000),
        ],
    )
    def test_document_round_trips_through_the_codec_unchanged(
        self, window_count: int, hot_threshold: int, weight_max: int
    ) -> None:
        config = _config(hot_threshold=hot_threshold, weight_max=weight_max)
        document = build_attributes(window_count, config)
        decoded = decode(encode(_hot_ip_envelope(dict(document))))
        payload = decoded.payload
        assert isinstance(payload, HotIpAdded)
        assert payload.attributes == document
        assert payload.attributes == {
            "attributes_version": 1,
            "weight": _model(window_count, hot_threshold, weight_max),
        }
