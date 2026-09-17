"""One observation, one outcome: the aggregator's lateness classification.

Spec: section 24 (out-of-order events and the bounded-lateness policy),
section 25 (bucket arithmetic), section 5 (a delta whose bucket has left the
window can never affect a future window count); ADR-0002 (event time,
`allowed_lateness`); ADR-0010 decision 6 (`window_seconds` on the message is
descriptive and is only used to reject an over-long window).

The interface under test is ADR-0011 decision 3:

    class ObservationOutcome(StrEnum):
        APPLIED = "applied"
        LATE = "late"
        FUTURE = "future"
        EXPIRED_BUCKET = "expired_bucket"
        WINDOW_TOO_LONG = "window_too_long"
        MALFORMED = "malformed"

    def classify_observation(
        *, window_start: int, window_seconds: int, now: int, config: DetectionConfig
    ) -> ObservationOutcome: ...

and the checks it makes, **in this order**:

1. `WINDOW_TOO_LONG` -- `window_seconds > config.window_seconds`
2. `FUTURE` -- `window_start > now`
3. `LATE` -- `now - window_start > config.window_seconds +
   config.allowed_lateness_seconds` (the shipped `is_within_lateness`
   horizon, against the *configured* window)
4. `EXPIRED_BUCKET` -- `bucket_start(now) - bucket_start(window_start) >=
   config.window_seconds`, i.e. `IpCounter.is_live` is false
5. otherwise `APPLIED`

`MALFORMED` is a worker outcome (a message that fails the codec or ADR-0004's
one-IP-per-message invariant) and is never returned here.

`now` is the service clock at processing time, not the envelope timestamp,
so every case below is expressed as an age relative to a fixed `NOW`.

Notes on what is and is not pinned:

* At an age of exactly `window_seconds + allowed_lateness_seconds` (330 s
  with the shipped defaults) the observation is inside the horizon -- the
  `LATE` test is strictly `>` -- so it falls through to the bucket check.
  Whether it lands on `EXPIRED_BUCKET` or `APPLIED` depends on the bucket
  arithmetic alone, and the tests below assert only that it is not `LATE`.
* `_bucket_start` restates section 25's formula locally rather than
  importing a helper, so these tests depend on the arithmetic the spec fixes
  and not on a particular helper signature.
* `NOW` is `1_800_000_000`, the `T0` of
  `docs/spec/integration-scenarios.md` section 2, a multiple of 300.
"""

from __future__ import annotations

from enum import StrEnum

import pytest
from hammertime.aggregator.lateness import ObservationOutcome, classify_observation
from hammertime.core.config.models import DetectionConfig
from hypothesis import given, settings
from hypothesis import strategies as st

NOW = 1_800_000_000

WINDOW_SECONDS = 300
BUCKET_SECONDS = 10
ALLOWED_LATENESS_SECONDS = 30
HORIZON = WINDOW_SECONDS + ALLOWED_LATENESS_SECONDS  # 330 s, ADR-0002


def _bucket_start(timestamp: int, bucket_seconds: int) -> int:
    """Section 25: `bucket_start = floor(event_timestamp / B) * B`."""

    return (timestamp // bucket_seconds) * bucket_seconds


def _config(
    *,
    window_seconds: int = WINDOW_SECONDS,
    bucket_seconds: int = BUCKET_SECONDS,
    hot_threshold: int = 1000,
    cold_threshold: int = 800,
    allowed_lateness_seconds: int = ALLOWED_LATENESS_SECONDS,
    state_retention_seconds: int = 600,
) -> DetectionConfig:
    """The shipped defaults of `config/detection.v1.json`, overridable."""

    return DetectionConfig(
        window_seconds=window_seconds,
        bucket_seconds=bucket_seconds,
        hot_threshold=hot_threshold,
        cold_threshold=cold_threshold,
        allowed_lateness_seconds=allowed_lateness_seconds,
        state_retention_seconds=state_retention_seconds,
    )


DEFAULTS = _config()


def _classify(
    *,
    age: int,
    window_seconds: int = BUCKET_SECONDS,
    config: DetectionConfig = DEFAULTS,
) -> ObservationOutcome:
    """Classify an observation whose window began `age` seconds before `NOW`.

    A negative `age` is an observation from the future.
    """

    return classify_observation(
        window_start=NOW - age,
        window_seconds=window_seconds,
        now=NOW,
        config=config,
    )


class TestObservationOutcomeEnum:
    """ADR-0011 decision 3: six outcomes, on the wire as strings so they can
    label `late_messages` / `observations_rejected` (section 37)."""

    def test_it_is_a_str_enum(self) -> None:
        assert issubclass(ObservationOutcome, StrEnum)
        assert isinstance(ObservationOutcome.APPLIED, str)

    def test_it_has_exactly_the_six_documented_members(self) -> None:
        assert {member.name: member.value for member in ObservationOutcome} == {
            "APPLIED": "applied",
            "LATE": "late",
            "FUTURE": "future",
            "EXPIRED_BUCKET": "expired_bucket",
            "WINDOW_TOO_LONG": "window_too_long",
            "MALFORMED": "malformed",
        }

    def test_the_metric_labels_are_the_member_values(self) -> None:
        # Section 37: `late_messages{reason=late|future|expired_bucket}` and
        # `observations_rejected{reason=window_too_long|malformed}`.
        assert ObservationOutcome.LATE == "late"
        assert ObservationOutcome.FUTURE == "future"
        assert ObservationOutcome.EXPIRED_BUCKET == "expired_bucket"
        assert ObservationOutcome.WINDOW_TOO_LONG == "window_too_long"


class TestApplied:
    """An observation the hot path can still count."""

    def test_an_observation_for_the_current_instant_is_applied(self) -> None:
        assert _classify(age=0) is ObservationOutcome.APPLIED

    @pytest.mark.parametrize("age", [0, 1, 9, 10, 100, 250, 290])
    def test_every_age_inside_the_window_is_applied(self, age: int) -> None:
        assert _classify(age=age) is ObservationOutcome.APPLIED

    def test_the_last_live_bucket_is_still_applied(self) -> None:
        # `NOW` is bucket-aligned, so an age of 290 is the oldest aligned
        # `window_start` still in the ring:
        # `bucket_start(now) - bucket_start(window_start) = 290 < 300`.
        assert _classify(age=290) is ObservationOutcome.APPLIED


class TestFuture:
    """`window_start > now` -- the agent's clock is ahead of the service."""

    @pytest.mark.parametrize("ahead", [1, 2, 10, 300, 10_000])
    def test_any_window_start_after_now_is_future(self, ahead: int) -> None:
        assert _classify(age=-ahead) is ObservationOutcome.FUTURE

    def test_one_second_ahead_is_future_but_the_current_instant_is_not(self) -> None:
        assert _classify(age=-1) is ObservationOutcome.FUTURE
        assert _classify(age=0) is ObservationOutcome.APPLIED


class TestLate:
    """ADR-0002's horizon: `now - window_start > window_seconds +
    allowed_lateness_seconds`, with the *configured* window."""

    @pytest.mark.parametrize("age", [HORIZON + 1, HORIZON + 10, 600, 3_600, 86_400])
    def test_beyond_the_horizon_is_late(self, age: int) -> None:
        assert _classify(age=age) is ObservationOutcome.LATE

    def test_exactly_at_the_horizon_is_not_late(self) -> None:
        # The horizon test is strictly `>`, so an age of exactly 330 s falls
        # through to the bucket check.
        outcome = _classify(age=HORIZON)
        assert outcome is not ObservationOutcome.LATE
        assert outcome in (ObservationOutcome.APPLIED, ObservationOutcome.EXPIRED_BUCKET)

    def test_one_second_past_the_horizon_is_late(self) -> None:
        assert _classify(age=HORIZON + 1) is ObservationOutcome.LATE

    def test_the_horizon_follows_the_configured_window(self) -> None:
        # A shorter configured window moves the horizon with it: 120 + 30.
        config = _config(window_seconds=120)
        assert _classify(age=150, config=config) is not ObservationOutcome.LATE
        assert _classify(age=151, config=config) is ObservationOutcome.LATE

    def test_the_horizon_follows_the_configured_lateness(self) -> None:
        config = _config(allowed_lateness_seconds=0)
        assert _classify(age=300, config=config) is not ObservationOutcome.LATE
        assert _classify(age=301, config=config) is ObservationOutcome.LATE


class TestExpiredBucket:
    """Inside the horizon, but the bucket has already left the ring, so the
    delta can never affect a future window count (section 5)."""

    @pytest.mark.parametrize("age", [300, 301, 305, 310, 329, 330])
    def test_ages_between_the_window_and_the_horizon_are_expired(self, age: int) -> None:
        assert _classify(age=age) is ObservationOutcome.EXPIRED_BUCKET

    def test_the_boundary_is_the_bucket_leaving_the_window(self) -> None:
        assert _classify(age=290) is ObservationOutcome.APPLIED
        assert _classify(age=300) is ObservationOutcome.EXPIRED_BUCKET

    def test_liveness_is_judged_on_buckets_not_on_the_raw_age(self) -> None:
        # Section 25: an age of 291 s floors to the bucket 300 s back, which
        # has left the ring even though the raw age has not reached 300.
        assert _classify(age=291) is ObservationOutcome.EXPIRED_BUCKET

    def test_a_shorter_configured_window_expires_buckets_sooner(self) -> None:
        config = _config(window_seconds=120)
        assert _classify(age=110, config=config) is ObservationOutcome.APPLIED
        assert _classify(age=120, config=config) is ObservationOutcome.EXPIRED_BUCKET


class TestWindowTooLong:
    """ADR-0010 decision 6: a window the ring cannot represent is rejected,
    and that check runs before any time check."""

    def test_a_longer_window_than_configured_is_rejected(self) -> None:
        assert _classify(age=0, window_seconds=600) is ObservationOutcome.WINDOW_TOO_LONG

    def test_it_is_rejected_even_when_otherwise_current(self) -> None:
        # Age 0: nothing else could possibly divert this message.
        assert _classify(age=0, window_seconds=301) is ObservationOutcome.WINDOW_TOO_LONG

    def test_it_wins_over_future(self) -> None:
        assert _classify(age=-100, window_seconds=600) is ObservationOutcome.WINDOW_TOO_LONG

    def test_it_wins_over_late(self) -> None:
        assert _classify(age=10_000, window_seconds=600) is ObservationOutcome.WINDOW_TOO_LONG

    def test_it_wins_over_expired_bucket(self) -> None:
        assert _classify(age=310, window_seconds=600) is ObservationOutcome.WINDOW_TOO_LONG

    def test_a_window_equal_to_the_configured_one_is_accepted(self) -> None:
        assert _classify(age=0, window_seconds=WINDOW_SECONDS) is ObservationOutcome.APPLIED

    @pytest.mark.parametrize("window_seconds", [1, 10, 30, 60, 299, 300])
    def test_no_window_up_to_the_configured_one_is_too_long(self, window_seconds: int) -> None:
        outcome = _classify(age=0, window_seconds=window_seconds)
        assert outcome is not ObservationOutcome.WINDOW_TOO_LONG

    def test_the_comparison_is_against_the_configured_window(self) -> None:
        config = _config(window_seconds=120)
        assert _classify(age=0, window_seconds=120, config=config) is ObservationOutcome.APPLIED
        too_long = _classify(age=0, window_seconds=121, config=config)
        assert too_long is ObservationOutcome.WINDOW_TOO_LONG


def _expected(
    *, window_start: int, window_seconds: int, now: int, config: DetectionConfig
) -> ObservationOutcome:
    """ADR-0011 decision 3's check order, restated."""

    if window_seconds > config.window_seconds:
        return ObservationOutcome.WINDOW_TOO_LONG
    if window_start > now:
        return ObservationOutcome.FUTURE
    if now - window_start > config.window_seconds + config.allowed_lateness_seconds:
        return ObservationOutcome.LATE
    now_bucket = _bucket_start(now, config.bucket_seconds)
    start_bucket = _bucket_start(window_start, config.bucket_seconds)
    if now_bucket - start_bucket >= config.window_seconds:
        return ObservationOutcome.EXPIRED_BUCKET
    return ObservationOutcome.APPLIED


_AGES = st.integers(min_value=-1_000, max_value=2_000)
_MESSAGE_WINDOWS = st.sampled_from([1, 5, 10, 30, 60, 120, 299, 300, 301, 600, 3_600])
_CONFIGS = st.sampled_from(
    [
        _config(),
        _config(allowed_lateness_seconds=0),
        _config(window_seconds=120),
        _config(window_seconds=60, bucket_seconds=30, state_retention_seconds=600),
    ]
)


@given(age=_AGES, window_seconds=_MESSAGE_WINDOWS, config=_CONFIGS)
@settings(deadline=None, max_examples=400)
def test_the_check_order_holds_for_every_input(
    age: int, window_seconds: int, config: DetectionConfig
) -> None:
    """The five ordered checks of decision 3, exhaustively."""

    outcome = classify_observation(
        window_start=NOW - age, window_seconds=window_seconds, now=NOW, config=config
    )
    assert outcome is _expected(
        window_start=NOW - age, window_seconds=window_seconds, now=NOW, config=config
    )


@given(age=_AGES, window_seconds=_MESSAGE_WINDOWS, config=_CONFIGS)
@settings(deadline=None, max_examples=400)
def test_malformed_is_never_returned(
    age: int, window_seconds: int, config: DetectionConfig
) -> None:
    """`MALFORMED` is a worker outcome (a message that fails the codec or
    ADR-0004's invariant); the classifier cannot produce it."""

    outcome = classify_observation(
        window_start=NOW - age, window_seconds=window_seconds, now=NOW, config=config
    )
    assert outcome is not ObservationOutcome.MALFORMED
    assert outcome in (
        ObservationOutcome.APPLIED,
        ObservationOutcome.LATE,
        ObservationOutcome.FUTURE,
        ObservationOutcome.EXPIRED_BUCKET,
        ObservationOutcome.WINDOW_TOO_LONG,
    )


@given(age=_AGES, window_seconds=_MESSAGE_WINDOWS, config=_CONFIGS)
@settings(deadline=None, max_examples=200)
def test_classification_is_pure(age: int, window_seconds: int, config: DetectionConfig) -> None:
    """Decision 3: "`classify_observation` is pure" -- the same inputs always
    give the same outcome, so a diverted message stays diverted on replay."""

    first = classify_observation(
        window_start=NOW - age, window_seconds=window_seconds, now=NOW, config=config
    )
    second = classify_observation(
        window_start=NOW - age, window_seconds=window_seconds, now=NOW, config=config
    )
    assert second is first


@given(age=st.integers(min_value=-1_000, max_value=2_000))
@settings(deadline=None, max_examples=200)
def test_only_an_applied_observation_has_a_live_bucket(age: int) -> None:
    """The `APPLIED` outcome is exactly the case the ring can still take: the
    bucket is live (section 5) and the age is inside ADR-0002's horizon."""

    outcome = _classify(age=age)
    window_start = NOW - age
    bucket_age = _bucket_start(NOW, BUCKET_SECONDS) - _bucket_start(window_start, BUCKET_SECONDS)
    if outcome is ObservationOutcome.APPLIED:
        assert 0 <= age <= HORIZON
        assert bucket_age < WINDOW_SECONDS
    else:
        assert age < 0 or age > HORIZON or bucket_age >= WINDOW_SECONDS
