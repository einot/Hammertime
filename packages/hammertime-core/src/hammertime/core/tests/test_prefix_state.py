"""`evaluate_prefix_state`: the one HOT_PREFIX predicate.

Spec: section 13 (a prefix qualifies on both `hot_count >= minimum_hot_ips` and
`hot_ratio >= minimum_hot_ratio`; its two worked examples), section 38 (core
invariants), section 3 (`hot_ratio = hot_count / capacity`).

Written from ADR-0010 decision 1 (one implementation, in
`hammertime.core.state.prefix`, comparing the ratio exactly) with its
2026-09-24 note, decision 2 (v1 answers `NORMAL` or `HOT_PREFIX` only), and
ADR-0017 Amendment 4 ruling 1:

* `HOT_PREFIX` when `hot_count >= config.minimum_hot_ips` and
  `Fraction(hot_count, capacity) >= config.minimum_hot_ratio`, "compared
  exactly, against the exact value of the configured float, so a ratio equal
  to that value qualifies";
* "a `hot_count` or a `capacity` that is not an `int`, or is a `bool`:
  `TypeError`"; "a `capacity` below 1: `ValueError`"; "a `hot_count` below 0,
  or above `capacity`: `ValueError`"; "Types are checked before values", and
  a message's wording "is not part of the contract", so no message is
  asserted;
* "`hammertime.core.state` re-exports it beside `evaluate_ip_state`".

Unless a test says otherwise the configuration is section 13's example and
integration-scenarios section 2.1's document: `minimum_hot_ips` 16 and
`minimum_hot_ratio` 0.10. Every other `DetectionConfig` field is left at its
default.

Choices of this file's own:

* The exactness case: `0.1` as a float is exactly `3602879701896397 / 2**55`.
  With `capacity = 2**75`, `hot_count = 3602879701896397 * 2**20` makes the
  exact ratio equal to that float, and `hot_count - 1` falls short of it by
  `2**-75`, while float division of `hot_count - 1` by `capacity` still rounds
  to `0.1`. The test checks that premise with plain Python before relying on
  it.
* The sweep compares every answer against section 13's two conditions written
  out here with `fractions.Fraction`, for every `hot_count` from 0 to
  `capacity` and every `capacity` from 1 to 64, under three configurations.
* A `capacity` of 0 is refused even when the count test alone would already
  answer `NORMAL`: ruling 1 has the function refuse "before it compares".
"""

from fractions import Fraction
from typing import Any

import pytest
from hammertime.core.config.models import DetectionConfig
from hammertime.core.state import evaluate_prefix_state as reexported_evaluate_prefix_state
from hammertime.core.state.enums import PrefixState
from hammertime.core.state.prefix import evaluate_prefix_state

NORMAL = PrefixState.NORMAL
HOT_PREFIX = PrefixState.HOT_PREFIX


def _config(*, minimum_hot_ips: int = 16, minimum_hot_ratio: float = 0.10) -> DetectionConfig:
    return DetectionConfig(minimum_hot_ips=minimum_hot_ips, minimum_hot_ratio=minimum_hot_ratio)


CONFIG = _config()


def _section_13(hot_count: int, capacity: int, config: DetectionConfig) -> PrefixState:
    """Section 13's two conditions, with the ratio compared exactly."""

    enough = hot_count >= config.minimum_hot_ips
    dense = Fraction(hot_count, capacity) >= Fraction(config.minimum_hot_ratio)
    return HOT_PREFIX if enough and dense else NORMAL


class TestSection13Examples:
    def test_a_24_with_37_hot_addresses_qualifies(self) -> None:
        # Section 13: "A `/24` with: hot_count = 37, capacity = 256 ... would
        # qualify."
        assert evaluate_prefix_state(37, 256, CONFIG) == HOT_PREFIX

    def test_a_single_hot_32_does_not(self) -> None:
        # Section 13: "A `/32` with: hot_count = 1, ratio = 100% would not
        # qualify because: hot_count < minimum_hot_ips".
        assert evaluate_prefix_state(1, 1, CONFIG) == NORMAL


class TestBothConditionsAreRequired:
    def test_enough_addresses_at_too_low_a_ratio(self) -> None:
        # 16 of 256 is 6.25%, below 10%.
        assert evaluate_prefix_state(16, 256, CONFIG) == NORMAL

    def test_a_high_ratio_with_too_few_addresses(self) -> None:
        # 15 of 16 is 93.75%, but 15 < 16.
        assert evaluate_prefix_state(15, 16, CONFIG) == NORMAL

    def test_both_met(self) -> None:
        assert evaluate_prefix_state(16, 16, CONFIG) == HOT_PREFIX


class TestBoundaries:
    def test_a_ratio_exactly_at_the_minimum_qualifies(self) -> None:
        config = _config(minimum_hot_ratio=0.125)

        # 32 / 256 == 0.125 exactly.
        assert evaluate_prefix_state(32, 256, config) == HOT_PREFIX
        assert evaluate_prefix_state(31, 256, config) == NORMAL

    def test_a_count_exactly_at_the_minimum_qualifies(self) -> None:
        assert evaluate_prefix_state(16, 16, CONFIG) == HOT_PREFIX
        assert evaluate_prefix_state(15, 16, CONFIG) == NORMAL

        config = _config(minimum_hot_ips=20)
        assert evaluate_prefix_state(20, 32, config) == HOT_PREFIX
        assert evaluate_prefix_state(19, 32, config) == NORMAL

    def test_the_ratio_is_compared_exactly_not_as_a_float(self) -> None:
        # ADR-0017 Amendment 4 ruling 1: "compared exactly, against the exact
        # value of the configured float". 0.1 is 3602879701896397 / 2**55.
        capacity = 2**75
        hot_count = 3602879701896397 * 2**20
        # The premise, checked in plain Python: the first is the float exactly;
        # the second is below it, yet float division rounds it to 0.1.
        assert Fraction(hot_count, capacity) == Fraction(0.1)
        assert Fraction(hot_count - 1, capacity) < Fraction(0.1)
        assert (hot_count - 1) / capacity == 0.1

        config = _config(minimum_hot_ratio=0.1)
        assert evaluate_prefix_state(hot_count, capacity, config) == HOT_PREFIX
        assert evaluate_prefix_state(hot_count - 1, capacity, config) == NORMAL


SWEEP_CONFIGS = [
    pytest.param(CONFIG, id="default"),
    pytest.param(_config(minimum_hot_ips=1, minimum_hot_ratio=0.5), id="one-at-half"),
    pytest.param(_config(minimum_hot_ips=4, minimum_hot_ratio=0.3), id="four-at-0.3"),
]


class TestOnlyTwoStates:
    @pytest.mark.parametrize("config", SWEEP_CONFIGS)
    def test_every_answer_is_normal_or_hot_prefix_and_follows_section_13(
        self, config: DetectionConfig
    ) -> None:
        # ADR-0010 decision 2: "`PrefixState.NORMAL` and `PrefixState.HOT_PREFIX`
        # are the only values a v1 service produces."
        seen: set[PrefixState] = set()
        for capacity in range(1, 65):
            for hot_count in range(capacity + 1):
                answer = evaluate_prefix_state(hot_count, capacity, config)
                assert answer in (NORMAL, HOT_PREFIX), (hot_count, capacity)
                assert answer != PrefixState.BOT_NETWORK
                assert answer == _section_13(hot_count, capacity, config), (hot_count, capacity)
                seen.add(answer)

        assert seen == {NORMAL, HOT_PREFIX}


class TestTheConfigDecides:
    def test_the_same_counts_under_different_documents(self) -> None:
        assert evaluate_prefix_state(37, 256, _config(minimum_hot_ips=37)) == HOT_PREFIX
        assert evaluate_prefix_state(37, 256, _config(minimum_hot_ips=38)) == NORMAL
        # 37 / 256 is 14.45%.
        assert evaluate_prefix_state(37, 256, _config(minimum_hot_ratio=0.14)) == HOT_PREFIX
        assert evaluate_prefix_state(37, 256, _config(minimum_hot_ratio=0.15)) == NORMAL


class TestRefusals:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(True, id="True"),
            pytest.param(False, id="False"),
            pytest.param(1.0, id="float-1.0"),
            pytest.param(16.5, id="float"),
            pytest.param("16", id="str"),
        ],
    )
    def test_a_hot_count_that_is_not_an_int_is_a_type_error(self, value: Any) -> None:
        with pytest.raises(TypeError):
            evaluate_prefix_state(value, 256, CONFIG)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(True, id="True"),
            pytest.param(False, id="False"),
            pytest.param(1.0, id="float-1.0"),
            pytest.param(256.0, id="float"),
            pytest.param("256", id="str"),
        ],
    )
    def test_a_capacity_that_is_not_an_int_is_a_type_error(self, value: Any) -> None:
        with pytest.raises(TypeError):
            evaluate_prefix_state(0, value, CONFIG)

    def test_true_is_refused_even_where_one_would_be_in_range(self) -> None:
        # "or is a `bool`": `True` is refused although 1 would be a valid count
        # and a valid capacity.
        with pytest.raises(TypeError):
            evaluate_prefix_state(True, 1, CONFIG)
        with pytest.raises(TypeError):
            evaluate_prefix_state(1, True, CONFIG)
        with pytest.raises(TypeError):
            evaluate_prefix_state(True, 256, _config(minimum_hot_ips=1, minimum_hot_ratio=0.0))

    @pytest.mark.parametrize(
        ("hot_count", "capacity"),
        [
            pytest.param(0, 0, id="capacity-0"),
            pytest.param(0, -1, id="capacity-negative"),
            pytest.param(-1, 256, id="hot-count-negative"),
            pytest.param(257, 256, id="hot-count-above-capacity"),
            pytest.param(2, 1, id="hot-count-above-capacity-1"),
        ],
    )
    def test_values_outside_the_domain_are_a_value_error(
        self, hot_count: int, capacity: int
    ) -> None:
        with pytest.raises(ValueError):
            evaluate_prefix_state(hot_count, capacity, CONFIG)

    def test_types_are_checked_before_values(self) -> None:
        # Each argument is both of the wrong type and out of range.
        with pytest.raises(TypeError):
            evaluate_prefix_state(-1.0, 256, CONFIG)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            evaluate_prefix_state(0, 0.0, CONFIG)  # type: ignore[arg-type]


class TestWhereItLives:
    def test_the_package_re_exports_the_same_function(self) -> None:
        assert reexported_evaluate_prefix_state is evaluate_prefix_state
