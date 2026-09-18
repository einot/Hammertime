"""The one HOT_PREFIX predicate: `evaluate_prefix_state` (spec section 13, section 38).

Spec: section 13 (the two criteria and the worked /24 and /32 examples),
section 38 (the prefix-classification invariant: HOT_PREFIX implies both
criteria hold), section 46.1 (no per-IP attribute may influence the verdict --
the predicate takes only `hot_count`, `capacity` and the config).
ADR-0010 decision 1 (one implementation, in `hammertime.core.state.prefix`;
the ratio is compared exactly as a `Fraction`), decision 2 (v1 yields only
`NORMAL` and `HOT_PREFIX`; `BOT_NETWORK` is reserved and never emitted).
ADR-0012 decision 9 as amended (the signature, the `ValueError` contract for
`capacity < 1` / `hot_count < 0` / `hot_count > capacity`, and the
exact-boundary rule:
`Fraction(hot_count, capacity) >= Fraction(config.minimum_hot_ratio)`, where
`Fraction(float)` is the float's exact binary value -- so an exact boundary
qualifies for a representable ratio such as 0.125 and compares against the
parsed float for one that is not, such as 0.10).
ADR-0012 Amendment 1 A4 (`hot_count > capacity` is a `ValueError`, not a
verdict: it cannot arise from a section 12-consistent trie, so the only
sources are a corrupt or forged `PrefixStatsChanged` or a caller bug, and
answering `HOT_PREFIX` would hide exactly that; `hot_count == capacity`
stays valid) and A5 (the three assumptions below marked "confirmed").

The predicate is defined completely by:

    HOT_PREFIX iff hot_count >= config.minimum_hot_ips
               and Fraction(hot_count, capacity) >= Fraction(config.minimum_hot_ratio)
    else NORMAL

Everything below is derived from that line and the sections cited, never from
the implementation.

ASSUMPTIONS (stated so a reader can push back):

* `DetectionConfig()`'s defaults are section 13's example (`minimum_hot_ips
  = 16`, `minimum_hot_ratio = 0.10`) -- confirmed by Amendment 1 A5 as a fact
  of `hammertime.core.config.models.DetectionConfig`, which that ADR does not
  restate and does not depend on, so this file says so here. The section 13
  examples are nevertheless run against an explicit config too, so the spec
  rows do not depend on the defaults.
* The property tests draw `hot_count` in `[0, capacity]`, the only range a
  section 12-consistent trie can produce. That stays the right domain for a
  property about the verdict: outside it the predicate has no verdict at all
  but a `ValueError` (A4), which `TestArgumentValidation` covers by example
  instead.
"""

from fractions import Fraction

import pytest
from hammertime.core.config.models import DetectionConfig
from hammertime.core.state.enums import PrefixState
from hammertime.core.state.prefix import evaluate_prefix_state
from hypothesis import given, settings
from hypothesis import strategies as st

# Section 13's worked example.
MINIMUM_HOT_IPS = 16
MINIMUM_HOT_RATIO = 0.10
EXAMPLE_CONFIG = DetectionConfig(
    minimum_hot_ips=MINIMUM_HOT_IPS, minimum_hot_ratio=MINIMUM_HOT_RATIO
)


class TestSectionThirteenExamples:
    """The two literal rows section 13 spells out."""

    def test_a_slash_24_with_37_hot_ips_qualifies(self) -> None:
        # hot_count = 37, capacity = 256, ratio = 14.45 %: both criteria hold.
        assert evaluate_prefix_state(37, 256, EXAMPLE_CONFIG) is PrefixState.HOT_PREFIX

    def test_a_slash_24_with_37_hot_ips_qualifies_at_the_defaults(self) -> None:
        assert evaluate_prefix_state(37, 256, DetectionConfig()) is PrefixState.HOT_PREFIX

    def test_a_slash_32_with_one_hot_ip_does_not_qualify(self) -> None:
        # hot_count = 1, ratio = 100 %: fails because hot_count < minimum_hot_ips.
        assert evaluate_prefix_state(1, 1, EXAMPLE_CONFIG) is PrefixState.NORMAL

    def test_a_slash_32_with_one_hot_ip_does_not_qualify_at_the_defaults(self) -> None:
        assert evaluate_prefix_state(1, 1, DetectionConfig()) is PrefixState.NORMAL

    def test_a_prefix_hot_merely_because_one_descendant_is_hot_is_normal(self) -> None:
        # Section 13's opening sentence, for every length: one hot IP never
        # makes a prefix hot when minimum_hot_ips is 16.
        for length in range(33):
            capacity = 2 ** (32 - length)
            assert evaluate_prefix_state(1, capacity, EXAMPLE_CONFIG) is PrefixState.NORMAL


class TestEachCriterionAlone:
    """Section 13: classification considers *both* criteria; either one failing
    is NORMAL."""

    def test_enough_hot_ips_but_too_small_a_ratio_is_normal(self) -> None:
        # 16 hot IPs in a /8: hot_count passes, ratio (16 / 2**24) does not.
        assert evaluate_prefix_state(16, 2**24, EXAMPLE_CONFIG) is PrefixState.NORMAL

    def test_a_large_ratio_but_too_few_hot_ips_is_normal(self) -> None:
        # 15 of 16 in a /28: ratio passes (93.75 %), hot_count does not.
        assert evaluate_prefix_state(15, 16, EXAMPLE_CONFIG) is PrefixState.NORMAL

    def test_both_criteria_at_once_is_hot_prefix(self) -> None:
        assert evaluate_prefix_state(16, 16, EXAMPLE_CONFIG) is PrefixState.HOT_PREFIX

    def test_hot_count_exactly_at_minimum_hot_ips_qualifies(self) -> None:
        # `>=`, not `>`: section 13 writes `hot_count >= minimum_hot_ips`.
        assert evaluate_prefix_state(16, 64, EXAMPLE_CONFIG) is PrefixState.HOT_PREFIX

    def test_hot_count_one_below_minimum_hot_ips_does_not_qualify(self) -> None:
        assert evaluate_prefix_state(15, 64, EXAMPLE_CONFIG) is PrefixState.NORMAL

    def test_zero_hot_ips_is_normal(self) -> None:
        assert evaluate_prefix_state(0, 256, EXAMPLE_CONFIG) is PrefixState.NORMAL


class TestExactRatioBoundary:
    """ADR-0010 decision 1 / ADR-0012 decision 9: the ratio is compared as an
    exact `Fraction`, so `hot_count / capacity == minimum_hot_ratio` qualifies
    whenever the configured ratio is exactly representable as a float."""

    def test_exact_boundary_qualifies_for_one_eighth(self) -> None:
        config = DetectionConfig(minimum_hot_ips=16, minimum_hot_ratio=0.125)
        assert evaluate_prefix_state(32, 256, config) is PrefixState.HOT_PREFIX

    def test_one_below_the_boundary_does_not_qualify_for_one_eighth(self) -> None:
        config = DetectionConfig(minimum_hot_ips=16, minimum_hot_ratio=0.125)
        assert evaluate_prefix_state(31, 256, config) is PrefixState.NORMAL

    def test_exact_boundary_qualifies_for_one_half(self) -> None:
        config = DetectionConfig(minimum_hot_ips=16, minimum_hot_ratio=0.5)
        assert evaluate_prefix_state(128, 256, config) is PrefixState.HOT_PREFIX
        assert evaluate_prefix_state(127, 256, config) is PrefixState.NORMAL

    def test_exact_boundary_qualifies_for_a_ratio_of_one(self) -> None:
        # minimum_hot_ratio = 1.0 is a legal config (test_config.py); only a
        # fully hot prefix qualifies.
        config = DetectionConfig(minimum_hot_ips=16, minimum_hot_ratio=1.0)
        assert evaluate_prefix_state(256, 256, config) is PrefixState.HOT_PREFIX
        assert evaluate_prefix_state(255, 256, config) is PrefixState.NORMAL

    def test_a_ratio_of_zero_leaves_only_the_hot_count_criterion(self) -> None:
        config = DetectionConfig(minimum_hot_ips=16, minimum_hot_ratio=0.0)
        assert evaluate_prefix_state(16, 2**32, config) is PrefixState.HOT_PREFIX
        assert evaluate_prefix_state(15, 2**32, config) is PrefixState.NORMAL

    def test_an_unrepresentable_ratio_compares_against_the_parsed_float(self) -> None:
        # ADR-0012 decision 9: `Fraction(0.10)` is the float's exact binary
        # value, which is slightly *above* 1/10, so exactly one tenth does not
        # reach it. (`Fraction.from_float(0.3) != Fraction(3, 10)` -- the
        # fractions module's own warning, cited by the ADR.)
        assert Fraction(0.10) > Fraction(1, 10)
        assert evaluate_prefix_state(100, 1000, EXAMPLE_CONFIG) is PrefixState.NORMAL
        # ... while the next representable step over one tenth does qualify.
        assert evaluate_prefix_state(101, 1000, EXAMPLE_CONFIG) is PrefixState.HOT_PREFIX

    def test_the_verdict_is_exact_not_a_float_division(self) -> None:
        # A ratio that float division would round *up* to the boundary must
        # still be judged by the exact fraction. 2**54 - 1 hot IPs out of
        # 2**55 is strictly less than one half, even though the float quotient
        # rounds (half-to-even) to exactly 0.5; 2**54 + 1 is strictly more.
        config = DetectionConfig(minimum_hot_ips=16, minimum_hot_ratio=0.5)
        assert (2**54 - 1) / 2**55 == 0.5
        assert evaluate_prefix_state(2**54 - 1, 2**55, config) is PrefixState.NORMAL
        assert evaluate_prefix_state(2**54 + 1, 2**55, config) is PrefixState.HOT_PREFIX


class TestIpv6Sizes:
    """Capacities are Python ints, never narrowed (test_addressing.py pins
    `Prefix.capacity()` the same way); the predicate must cope with a /0 of
    2**128 addresses."""

    def test_one_hot_ip_in_the_whole_ipv6_space_is_normal(self) -> None:
        assert evaluate_prefix_state(1, 2**128, EXAMPLE_CONFIG) is PrefixState.NORMAL

    def test_a_fully_hot_ipv6_space_is_hot_prefix(self) -> None:
        config = DetectionConfig(minimum_hot_ips=16, minimum_hot_ratio=1.0)
        assert evaluate_prefix_state(2**128, 2**128, config) is PrefixState.HOT_PREFIX

    def test_a_slash_104_has_the_capacity_of_a_slash_8(self) -> None:
        # ADR-0012 decision 1: 2**24 addresses in both, so the predicate sees
        # identical inputs and yields the identical verdict (it is not told
        # the family). 2**21 of 2**24 is one eighth: above 0.10.
        assert evaluate_prefix_state(2**21, 2**24, EXAMPLE_CONFIG) is PrefixState.HOT_PREFIX
        assert evaluate_prefix_state(2**20, 2**24, EXAMPLE_CONFIG) is PrefixState.NORMAL


class TestArgumentValidation:
    """ADR-0012 decision 9 as amended by Amendment 1 A4: `ValueError if
    capacity < 1, hot_count < 0, or hot_count > capacity`."""

    @pytest.mark.parametrize("capacity", [0, -1, -256])
    def test_capacity_below_one_is_a_value_error(self, capacity: int) -> None:
        # `hot_count = 0` so that `capacity = 0` trips this rule alone; for a
        # negative capacity A4's `hot_count > capacity` rule also applies, and
        # either way the contract is the same `ValueError`.
        with pytest.raises(ValueError):
            evaluate_prefix_state(0, capacity, EXAMPLE_CONFIG)

    @pytest.mark.parametrize("hot_count", [-1, -37])
    def test_negative_hot_count_is_a_value_error(self, hot_count: int) -> None:
        with pytest.raises(ValueError):
            evaluate_prefix_state(hot_count, 256, EXAMPLE_CONFIG)

    def test_hot_count_above_capacity_is_a_value_error(self) -> None:
        # A4's own example: one more hot IP than a /24 can hold.
        with pytest.raises(ValueError):
            evaluate_prefix_state(257, 256, DetectionConfig())

    @pytest.mark.parametrize(
        ("hot_count", "capacity"),
        [(2, 1), (17, 16), (2**24 + 1, 2**24), (2**128 + 1, 2**128)],
        ids=["host-route", "slash-28", "slash-8", "ipv6-slash-0"],
    )
    def test_hot_count_above_capacity_is_a_value_error_at_every_scale(
        self, hot_count: int, capacity: int
    ) -> None:
        # Including IPv6 scale: the rejection is arithmetic, not a 32-bit
        # assumption, and it must not be skipped for a capacity that cannot
        # fit in a machine word.
        with pytest.raises(ValueError):
            evaluate_prefix_state(hot_count, capacity, EXAMPLE_CONFIG)

    def test_hot_count_equal_to_capacity_is_accepted(self) -> None:
        # A4 keeps a fully hot prefix a valid input -- the boundary the
        # rejection must not swallow.
        config = DetectionConfig(minimum_hot_ips=16, minimum_hot_ratio=1.0)
        assert evaluate_prefix_state(256, 256, config) is PrefixState.HOT_PREFIX
        assert evaluate_prefix_state(2**128, 2**128, config) is PrefixState.HOT_PREFIX

    def test_capacity_of_one_is_accepted(self) -> None:
        # The /32 (or /128) host route: capacity 1 is the smallest legal value.
        assert evaluate_prefix_state(0, 1, EXAMPLE_CONFIG) is PrefixState.NORMAL
        assert evaluate_prefix_state(1, 1, EXAMPLE_CONFIG) is PrefixState.NORMAL

    def test_hot_count_of_zero_is_accepted(self) -> None:
        assert evaluate_prefix_state(0, 2**32, EXAMPLE_CONFIG) is PrefixState.NORMAL


# --- Property tests (section 38, ADR-0010 decisions 1-2) -----------------------

# Ratios biased onto values that are exactly representable (where the boundary
# rule bites) and the spec's own 0.10, plus the whole unit interval.
_RATIOS = st.one_of(
    st.sampled_from([0.0, 0.10, 0.125, 0.25, 0.5, 0.75, 1.0]),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)


@st.composite
def _configs(draw: st.DrawFn) -> DetectionConfig:
    """Configs whose only interesting fields are the two prefix criteria
    (ADR-0012 decision 10: nothing else in `DetectionConfig` affects the trie's
    verdict). `minimum_hot_ips >= 1` per schemas/detection_config.v1.json."""

    minimum_hot_ips = draw(st.integers(min_value=1, max_value=2**20))
    minimum_hot_ratio = draw(_RATIOS)
    return DetectionConfig(minimum_hot_ips=minimum_hot_ips, minimum_hot_ratio=minimum_hot_ratio)


@st.composite
def _counts_and_capacity(draw: st.DrawFn) -> tuple[int, int]:
    """`(hot_count, capacity)` with `capacity` a real prefix capacity (a power
    of two up to 2**128) or an arbitrary positive int, and `hot_count` in
    `[0, capacity]`, biased onto 0, the capacity, and the neighbourhood of the
    common boundaries."""

    capacity = draw(
        st.one_of(
            st.integers(min_value=0, max_value=128).map(lambda k: 2**k),
            st.integers(min_value=1, max_value=2**40),
        )
    )
    boundaries = sorted(
        {
            0,
            1,
            min(capacity, 15),
            min(capacity, 16),
            min(capacity, 17),
            capacity // 8,
            capacity // 4,
            capacity // 2,
            max(0, capacity - 1),
            capacity,
        }
    )
    hot_count = draw(
        st.one_of(
            st.sampled_from(boundaries),
            st.integers(min_value=0, max_value=capacity),
        )
    )
    return hot_count, capacity


@given(config=_configs(), counts=_counts_and_capacity())
@settings(deadline=None, max_examples=300)
def test_hot_prefix_implies_both_criteria(config: DetectionConfig, counts: tuple[int, int]) -> None:
    """Section 38: `HOT_PREFIX => hot_count >= minimum_hot_ips AND hot_ratio >=
    minimum_hot_ratio`."""

    hot_count, capacity = counts
    if evaluate_prefix_state(hot_count, capacity, config) is PrefixState.HOT_PREFIX:
        assert hot_count >= config.minimum_hot_ips
        assert Fraction(hot_count, capacity) >= Fraction(config.minimum_hot_ratio)


@given(config=_configs(), counts=_counts_and_capacity())
@settings(deadline=None, max_examples=300)
def test_both_criteria_imply_hot_prefix(config: DetectionConfig, counts: tuple[int, int]) -> None:
    """ADR-0010 decision 1 / ADR-0012 decision 9 make section 13's "SHOULD
    consider both" an `iff`: when both criteria hold the verdict is HOT_PREFIX,
    with the ratio compared exactly."""

    hot_count, capacity = counts
    enough_ips = hot_count >= config.minimum_hot_ips
    enough_ratio = Fraction(hot_count, capacity) >= Fraction(config.minimum_hot_ratio)
    if enough_ips and enough_ratio:
        assert evaluate_prefix_state(hot_count, capacity, config) is PrefixState.HOT_PREFIX
    else:
        assert evaluate_prefix_state(hot_count, capacity, config) is PrefixState.NORMAL


@given(config=_configs(), counts=_counts_and_capacity())
@settings(deadline=None, max_examples=300)
def test_never_bot_network(config: DetectionConfig, counts: tuple[int, int]) -> None:
    """ADR-0010 decision 2: v1 emits exactly two prefix states; `BOT_NETWORK`
    is reserved for the section 14 scorer and never returned here."""

    hot_count, capacity = counts
    state = evaluate_prefix_state(hot_count, capacity, config)
    assert state is not PrefixState.BOT_NETWORK
    assert state in (PrefixState.NORMAL, PrefixState.HOT_PREFIX)
    assert isinstance(state, PrefixState)


@given(config=_configs(), counts=_counts_and_capacity())
@settings(deadline=None, max_examples=200)
def test_evaluation_is_pure(config: DetectionConfig, counts: tuple[int, int]) -> None:
    """ADR-0010 decision 1: the trie's read path and the detector call the
    same function and must get the identical verdict for identical inputs --
    only possible if it carries no hidden state."""

    hot_count, capacity = counts
    first = evaluate_prefix_state(hot_count, capacity, config)
    second = evaluate_prefix_state(hot_count, capacity, config)
    assert second is first


@given(config=_configs(), counts=_counts_and_capacity())
@settings(deadline=None, max_examples=200)
def test_monotone_in_hot_count(config: DetectionConfig, counts: tuple[int, int]) -> None:
    """Both criteria are monotone in `hot_count` at fixed capacity, so adding a
    hot IP can never demote a HOT_PREFIX prefix (section 38's remark that a
    prefix flips only "whenever either criterion changes")."""

    hot_count, capacity = counts
    if hot_count == capacity:
        return
    before = evaluate_prefix_state(hot_count, capacity, config)
    after = evaluate_prefix_state(hot_count + 1, capacity, config)
    if before is PrefixState.HOT_PREFIX:
        assert after is PrefixState.HOT_PREFIX


@given(
    config=_configs(),
    hot_count=st.integers(min_value=0, max_value=2**24),
    length=st.integers(min_value=0, max_value=32),
)
@settings(deadline=None, max_examples=200)
def test_antitone_in_capacity(config: DetectionConfig, hot_count: int, length: int) -> None:
    """At the same `hot_count`, a shorter (larger) prefix has a smaller ratio,
    so a HOT_PREFIX verdict for a /L implies HOT_PREFIX for the /(L+1) holding
    the same hot IPs -- the theorem behind ADR-0012 assumption 27 (a
    compressed-away logical prefix is never in the minimal set)."""

    capacity = 2 ** (32 - length)
    if hot_count > capacity // 2 or length == 32:
        return
    parent = evaluate_prefix_state(hot_count, capacity, config)
    child = evaluate_prefix_state(hot_count, capacity // 2, config)
    if parent is PrefixState.HOT_PREFIX:
        assert child is PrefixState.HOT_PREFIX
