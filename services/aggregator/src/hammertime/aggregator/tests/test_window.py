"""Bucket expiry, running total, and out-of-order writes into live buckets.

Spec: section 5, section 24

Also section 25 (bucket alignment), section 20 and section 26 (the window
store and its retention/cap), through the interfaces ADR-0011 section 9
fixes for `hammertime.aggregator.lateness` and
`hammertime.aggregator.window.{counter,store,expiry}`.

The one rule every expectation below is derived from is ADR-0011 decision 1
("a bucket is live while `S <= now < S + window_seconds`, judged against the
service clock"), which spec section 5's pointer note restates normatively.
With the defaults (`W = 300`, `B = 10`) at `now = 1000` that makes
`710, 720, ..., 1000` the thirty live buckets and `700` the one that has
just expired; every boundary case here is a restatement of that sentence
rather than a remembered constant, and `_live_model` below is the same rule
written out so the property test and the re-bucketing tests can derive their
expectations instead of guessing them.

The same sentence is what decides `classify`'s `EXPIRED`/`APPLY` split
(ADR-0011 decision 2: judged on the bucket `S`, never on the raw age of
`window_start`), what `rebucket` installs as the store's own geometry
(decision 7 step 2), and what `get_or_create` does *not* touch on an existing
entry (decision 3: `now=` seeds a new entry only).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hammertime.aggregator.lateness import (
    Disposition,
    classify,
    is_live,
    window_start_epoch,
)
from hammertime.aggregator.window.counter import IpCounter
from hammertime.aggregator.window.expiry import evict_idle, expire_buckets
from hammertime.aggregator.window.store import (
    InMemoryWindowStore,
    IpEntry,
    StoreFullError,
)
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.state.enums import IpState
from hammertime.core.time.buckets import bucket_start
from hypothesis import given, settings
from hypothesis import strategies as st

# The section 5 example, which is also DetectionConfig's default geometry.
WINDOW_SECONDS = 300
BUCKET_SECONDS = 10
NOW = 1000

# ADR-0011 decision 1, spelled out: the live buckets at NOW under the
# defaults. 710 is the oldest live one (710 + 300 = 1010 > 1000) and 1000 is
# the current, still-filling one; 700 expired at exactly now == 700 + 300.
LIVE_BUCKETS = tuple(range(710, 1001, 10))

IP_A = Address.parse("10.0.0.1")
IP_B = Address.parse("10.0.0.2")
IP_C = Address.parse("10.0.0.3")


def _live_model(
    pairs: list[tuple[int, int]],
    *,
    window_seconds: int,
    bucket_seconds: int,
    now: int,
) -> tuple[tuple[int, int], ...]:
    """The retained buckets, straight from ADR-0011 decisions 1 and 7.

    Each `(bucket_start, count)` pair is placed at `bucket_start(S, B)`
    (section 25's floor rule, the same one-bucket placement observations
    follow, ADR-0010 decision 6), counts landing in the same bucket add up,
    and a bucket survives exactly while `S <= now < S + window_seconds`.
    Counts are never invented and never scaled.
    """
    merged: dict[int, int] = {}
    for start, count in pairs:
        placed = bucket_start(start, bucket_seconds)
        if placed <= now < placed + window_seconds:
            merged[placed] = merged.get(placed, 0) + count
    return tuple(sorted(merged.items()))


def _counter(
    *, window_seconds: int = WINDOW_SECONDS, bucket_seconds: int = BUCKET_SECONDS
) -> IpCounter:
    return IpCounter(window_seconds=window_seconds, bucket_seconds=bucket_seconds)


def _store(
    *,
    window_seconds: int = WINDOW_SECONDS,
    bucket_seconds: int = BUCKET_SECONDS,
    max_tracked_ips: int = 1_000_000,
) -> InMemoryWindowStore:
    return InMemoryWindowStore(
        window_seconds=window_seconds,
        bucket_seconds=bucket_seconds,
        max_tracked_ips=max_tracked_ips,
    )


class TestIpCounterConstruction:
    """Section 5's geometry: `bucket_count = window / bucket`, exactly."""

    def test_geometry_is_exposed(self) -> None:
        counter = _counter()

        assert counter.window_seconds == WINDOW_SECONDS
        assert counter.bucket_seconds == BUCKET_SECONDS
        assert counter.bucket_count == WINDOW_SECONDS // BUCKET_SECONDS == 30

    def test_total_starts_at_zero(self) -> None:
        assert _counter().total == 0

    def test_a_fresh_counter_retains_no_buckets(self) -> None:
        assert _counter().buckets() == ()

    @pytest.mark.parametrize(
        ("window_seconds", "bucket_seconds"),
        [(305, 10), (300, 7), (10, 300), (1, 2)],
    )
    def test_rejects_window_that_is_not_a_multiple_of_the_bucket(
        self, window_seconds: int, bucket_seconds: int
    ) -> None:
        # A partial bucket has no defined edge, so section 5's
        # "bucket_count" would not be an integer.
        with pytest.raises(ValueError):
            _counter(window_seconds=window_seconds, bucket_seconds=bucket_seconds)

    @pytest.mark.parametrize(
        ("window_seconds", "bucket_seconds"),
        [(0, 10), (-300, 10), (300, 0), (300, -10), (0, 0)],
    )
    def test_rejects_non_positive_geometry(self, window_seconds: int, bucket_seconds: int) -> None:
        with pytest.raises(ValueError):
            _counter(window_seconds=window_seconds, bucket_seconds=bucket_seconds)

    @pytest.mark.parametrize(
        ("window_seconds", "bucket_seconds", "expected_bucket_count"),
        [(300, 10, 30), (300, 20, 15), (300, 300, 1), (60, 1, 60)],
    )
    def test_accepts_every_exact_division(
        self, window_seconds: int, bucket_seconds: int, expected_bucket_count: int
    ) -> None:
        counter = _counter(window_seconds=window_seconds, bucket_seconds=bucket_seconds)

        assert counter.bucket_count == expected_bucket_count


class TestObserveLiveEdge:
    """ADR-0011 decision 1 at both edges of the window, at `now = 1000`."""

    def test_oldest_live_bucket_is_counted(self) -> None:
        # 710 + 300 = 1010 > 1000, so 710 is still live.
        counter = _counter()

        assert counter.observe(710, 5, now=NOW) == 5
        assert counter.total == 5

    def test_bucket_that_expired_exactly_now_is_rejected(self) -> None:
        # 700 + 300 == 1000, and liveness is `now < S + W`, so bucket 700
        # expires at exactly now == 1000 rather than one second later.
        counter = _counter()

        with pytest.raises(ValueError):
            counter.observe(700, 5, now=NOW)

        assert counter.total == 0

    def test_current_still_filling_bucket_is_counted(self) -> None:
        # The window includes the bucket containing `now`: S == now is live.
        counter = _counter()

        assert counter.observe(1000, 7, now=NOW) == 7
        assert counter.total == 7

    def test_future_bucket_is_rejected(self) -> None:
        counter = _counter()

        with pytest.raises(ValueError):
            counter.observe(1010, 5, now=NOW)

        assert counter.total == 0

    @pytest.mark.parametrize("unaligned", [705, 701, 999, 1009])
    def test_unaligned_bucket_start_is_rejected(self, unaligned: int) -> None:
        # Section 25: a bucket start is always a multiple of B. The counter
        # does not silently round -- rounding is the caller's step.
        counter = _counter()

        with pytest.raises(ValueError):
            counter.observe(unaligned, 5, now=NOW)

        assert counter.total == 0

    @pytest.mark.parametrize("delta", [0, -1, -1000])
    def test_delta_below_one_is_rejected(self, delta: int) -> None:
        counter = _counter()

        with pytest.raises(ValueError):
            counter.observe(1000, delta, now=NOW)

        assert counter.total == 0

    def test_observe_returns_the_new_running_total(self) -> None:
        counter = _counter()

        assert counter.observe(1000, 3, now=NOW) == 3
        assert counter.observe(1000, 4, now=NOW) == 7
        assert counter.observe(710, 1, now=NOW) == 8
        assert counter.total == 8

    def test_every_live_bucket_of_the_window_is_accepted(self) -> None:
        counter = _counter()

        for index, start in enumerate(LIVE_BUCKETS, start=1):
            assert counter.observe(start, 1, now=NOW) == index

        assert counter.total == len(LIVE_BUCKETS) == counter.bucket_count == 30


class TestOutOfOrderWrites:
    """Section 24 / ADR-0002: a write into an older live bucket is normal."""

    def test_older_bucket_after_newer_adds_its_delta(self) -> None:
        counter = _counter()
        counter.observe(1000, 5, now=NOW)

        assert counter.observe(710, 3, now=NOW) == 8
        assert counter.total == 8

    def test_interleaved_writes_add_up_regardless_of_order(self) -> None:
        # The section 24 example (10:00:20, 10:00:40, 10:00:30) in bucket terms.
        counter = _counter()
        counter.observe(920, 2, now=NOW)
        counter.observe(940, 4, now=NOW)
        counter.observe(930, 8, now=NOW)

        assert counter.total == 14
        assert counter.buckets() == ((920, 2), (930, 8), (940, 4))

    def test_repeated_writes_into_the_same_older_bucket_accumulate(self) -> None:
        counter = _counter()
        counter.observe(1000, 1, now=NOW)
        counter.observe(710, 2, now=NOW)
        counter.observe(710, 3, now=NOW)

        assert counter.total == 6
        assert counter.buckets() == ((710, 5), (1000, 1))

    @settings(deadline=None)
    @given(
        pairs=st.lists(
            st.tuples(
                st.sampled_from(LIVE_BUCKETS),
                st.integers(min_value=1, max_value=10**6),
            ),
            max_size=60,
        ),
        data=st.data(),
    )
    def test_total_is_independent_of_arrival_order(
        self, pairs: list[tuple[int, int]], data: st.DataObject
    ) -> None:
        """Any permutation of writes into live buckets gives the same total.

        Section 5 keeps `total` incrementally, so the property that matters
        is that `+= delta` on an arbitrary live bucket is exactly as good as
        summing the buckets, whatever order the deltas arrive in (section 24,
        ADR-0002: "the running total is maintained on arbitrary-bucket
        updates").
        """
        shuffled = data.draw(st.permutations(pairs), label="arrival order")

        in_order = _counter()
        for start, delta in pairs:
            in_order.observe(start, delta, now=NOW)

        out_of_order = _counter()
        for start, delta in shuffled:
            out_of_order.observe(start, delta, now=NOW)

        assert out_of_order.total == sum(delta for _, delta in pairs)
        assert out_of_order.total == in_order.total
        assert out_of_order.buckets() == in_order.buckets()

    @settings(deadline=None)
    @given(
        pairs=st.lists(
            st.tuples(
                st.sampled_from(LIVE_BUCKETS),
                st.integers(min_value=1, max_value=10**6),
            ),
            max_size=60,
        ),
    )
    def test_total_tracks_the_liveness_model_as_the_clock_advances(
        self, pairs: list[tuple[int, int]]
    ) -> None:
        """After every expiry sweep, `total` is the sum of the live buckets.

        The model is ADR-0011 decision 1 applied directly:
        `sum(count for S, count if S <= now < S + W)`.
        """
        counter = _counter()
        for start, delta in pairs:
            counter.observe(start, delta, now=NOW)

        for step in range(0, WINDOW_SECONDS + 2 * BUCKET_SECONDS + 1, BUCKET_SECONDS):
            now = NOW + step
            counter.expire(now)
            expected = _live_model(
                pairs,
                window_seconds=WINDOW_SECONDS,
                bucket_seconds=BUCKET_SECONDS,
                now=now,
            )

            assert counter.buckets() == expected
            assert counter.total == sum(count for _, count in expected)

        # Every bucket observed at NOW has left the window by NOW + W + B.
        assert counter.total == 0


class TestExpiry:
    """Section 5: "when a bucket expires, total -= expired_bucket.count"."""

    def test_nothing_expires_one_second_before_the_edge(self) -> None:
        counter = _counter()
        counter.observe(710, 5, now=NOW)

        assert counter.expire(1009) == 0
        assert counter.total == 5
        assert counter.buckets() == ((710, 5),)

    def test_bucket_expires_exactly_at_start_plus_window(self) -> None:
        counter = _counter()
        counter.observe(710, 5, now=NOW)

        assert counter.expire(1010) == 5
        assert counter.total == 0
        assert counter.buckets() == ()

    def test_expiry_removes_exactly_the_expired_bucket(self) -> None:
        counter = _counter()
        counter.observe(710, 5, now=NOW)
        counter.observe(1000, 4, now=NOW)

        assert counter.expire(1010) == 5
        assert counter.total == 4
        assert counter.buckets() == ((1000, 4),)

    def test_expiry_is_idempotent(self) -> None:
        counter = _counter()
        counter.observe(710, 5, now=NOW)
        counter.expire(1010)

        assert counter.expire(1010) == 0
        assert counter.expire(1010) == 0
        assert counter.total == 0

    def test_expiry_at_an_earlier_time_removes_nothing(self) -> None:
        counter = _counter()
        counter.observe(710, 5, now=NOW)

        assert counter.expire(NOW) == 0
        assert counter.total == 5

    def test_expiry_of_several_buckets_sums_what_it_removed(self) -> None:
        counter = _counter()
        counter.observe(710, 5, now=NOW)
        counter.observe(720, 6, now=NOW)
        counter.observe(1000, 1, now=NOW)

        # At now = 1025 every bucket with S + 300 <= 1025 is gone, so 710 and
        # 720 have both expired while 1000 is still live.
        assert counter.expire(1025) == 11
        assert counter.total == 1
        assert counter.buckets() == ((1000, 1),)

    def test_observe_expires_first_so_the_total_is_current(self) -> None:
        # ADR-0011 decision 1: "expire(now) is idempotent and is run before
        # every observe, so the total is correct at the moment a state
        # decision is taken". 710 and 1010 are exactly W apart, so a ring
        # implementation reuses the slot here.
        counter = _counter()
        counter.observe(710, 5, now=NOW)
        counter.observe(1000, 3, now=NOW)

        assert counter.observe(1010, 2, now=1010) == 5
        assert counter.buckets() == ((1000, 3), (1010, 2))

    def test_expiry_after_the_whole_window_empties_the_counter(self) -> None:
        counter = _counter()
        for start in LIVE_BUCKETS:
            counter.observe(start, 2, now=NOW)

        assert counter.expire(NOW + WINDOW_SECONDS) == 2 * len(LIVE_BUCKETS)
        assert counter.total == 0
        assert counter.buckets() == ()


class TestBucketsSnapshot:
    """`buckets()`: ascending, non-empty, as of the last observe/expire."""

    def test_pairs_are_ascending_by_bucket_start(self) -> None:
        counter = _counter()
        counter.observe(1000, 1, now=NOW)
        counter.observe(710, 2, now=NOW)
        counter.observe(900, 3, now=NOW)

        assert counter.buckets() == ((710, 2), (900, 3), (1000, 1))

    def test_only_buckets_with_a_positive_count_are_reported(self) -> None:
        counter = _counter()
        counter.observe(710, 2, now=NOW)
        counter.observe(1000, 1, now=NOW)
        counter.expire(1010)

        assert [count for _, count in counter.buckets()] == [1]
        assert all(count > 0 for _, count in counter.buckets())

    def test_reported_starts_are_aligned_and_live(self) -> None:
        counter = _counter()
        for start in (710, 850, 1000):
            counter.observe(start, 1, now=NOW)

        for start, _count in counter.buckets():
            assert start % BUCKET_SECONDS == 0
            assert is_live(start, NOW, WINDOW_SECONDS)

    def test_snapshot_totals_agree_with_the_running_total(self) -> None:
        counter = _counter()
        counter.observe(710, 2, now=NOW)
        counter.observe(940, 40, now=NOW)

        assert sum(count for _, count in counter.buckets()) == counter.total == 42


class TestRebuild:
    """ADR-0011 decision 7 step 2: re-place each pair, drop what is dead."""

    def test_the_worked_example_from_adr_0011(self) -> None:
        # W = 300, B = 20, now = 1000. Both 700 and bucket_start(710, 20) =
        # 700 are expired (700 + 300 <= 1000) and are dropped;
        # bucket_start(995, 20) = 980 is live, so only its 3 survive.
        pairs = [(700, 1), (710, 2), (995, 3)]
        expected = _live_model(pairs, window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW)

        counter = IpCounter.rebuild(
            pairs, window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW
        )

        assert expected == ((980, 3),)  # the model, written out
        assert counter.buckets() == expected
        assert counter.total == 3

    def test_pairs_landing_in_the_same_new_bucket_are_merged(self) -> None:
        # bucket_start(980, 20) == bucket_start(990, 20) == 980.
        pairs = [(980, 1), (990, 2), (1000, 4)]
        expected = _live_model(pairs, window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW)

        counter = IpCounter.rebuild(
            pairs, window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW
        )

        assert expected == ((980, 3), (1000, 4))
        assert counter.buckets() == expected
        assert counter.total == 7

    def test_counts_are_neither_invented_nor_scaled(self) -> None:
        # A coarser bucket does not multiply counts: the total is exactly the
        # sum of the surviving inputs.
        pairs = [(900, 11), (910, 13), (920, 17)]

        counter = IpCounter.rebuild(
            pairs, window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW
        )

        assert counter.total == 11 + 13 + 17

    def test_the_rebuilt_counter_carries_the_new_geometry(self) -> None:
        counter = IpCounter.rebuild(
            [(995, 3)], window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW
        )

        assert counter.window_seconds == WINDOW_SECONDS
        assert counter.bucket_seconds == 20
        assert counter.bucket_count == 15

    def test_rebuild_of_nothing_is_an_empty_counter(self) -> None:
        counter = IpCounter.rebuild([], window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW)

        assert counter.total == 0
        assert counter.buckets() == ()

    def test_rebuild_with_an_unchanged_geometry_round_trips(self) -> None:
        source = _counter()
        source.observe(710, 2, now=NOW)
        source.observe(1000, 5, now=NOW)

        rebuilt = IpCounter.rebuild(
            source.buckets(),
            window_seconds=WINDOW_SECONDS,
            bucket_seconds=BUCKET_SECONDS,
            now=NOW,
        )

        assert rebuilt.buckets() == source.buckets()
        assert rebuilt.total == source.total

    def test_rebuild_drops_everything_that_is_dead_under_the_new_window(self) -> None:
        pairs = [(710, 2), (800, 3), (1000, 5)]
        # A shorter window: only S > 1000 - 60 survives, i.e. 950..1000.
        expected = _live_model(pairs, window_seconds=60, bucket_seconds=10, now=NOW)

        counter = IpCounter.rebuild(
            pairs, window_seconds=60, bucket_seconds=BUCKET_SECONDS, now=NOW
        )

        assert expected == ((1000, 5),)
        assert counter.buckets() == expected
        assert counter.bucket_count == 6

    def test_rebuild_rejects_an_invalid_geometry(self) -> None:
        # rebuild yields an IpCounter, so it cannot produce one whose
        # geometry the constructor would refuse.
        with pytest.raises(ValueError):
            IpCounter.rebuild([(1000, 1)], window_seconds=305, bucket_seconds=10, now=NOW)

    def test_a_rebuilt_counter_still_accepts_observations(self) -> None:
        counter = IpCounter.rebuild(
            [(995, 3)], window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW
        )

        assert counter.observe(1000, 2, now=NOW) == 5
        assert counter.buckets() == ((980, 3), (1000, 2))


class TestWindowStartEpoch:
    """Section 25: UTC internally; ADR-0011 section 9: naive means UTC."""

    def test_aware_utc_datetime(self) -> None:
        # 2024-01-01T00:00:00Z == 19723 days * 86400.
        assert window_start_epoch(datetime(2024, 1, 1, tzinfo=UTC)) == 1_704_067_200

    def test_naive_datetime_is_read_as_utc(self) -> None:
        naive = datetime(2024, 1, 1)  # deliberately tz-naive
        aware = datetime(2024, 1, 1, tzinfo=UTC)

        assert window_start_epoch(naive) == window_start_epoch(aware)

    def test_a_non_utc_offset_is_converted_to_the_same_instant(self) -> None:
        east = datetime(2024, 1, 1, 2, 0, tzinfo=timezone(timedelta(hours=2)))

        assert window_start_epoch(east) == 1_704_067_200

    def test_round_trips_the_epoch_seconds_used_by_classify(self) -> None:
        assert window_start_epoch(datetime.fromtimestamp(NOW, tz=UTC)) == NOW


class TestIsLive:
    """ADR-0011 decision 1, both boundaries."""

    def test_oldest_live_bucket(self) -> None:
        assert is_live(710, NOW, WINDOW_SECONDS)

    def test_bucket_expires_at_start_plus_window(self) -> None:
        assert not is_live(700, NOW, WINDOW_SECONDS)

    def test_the_bucket_containing_now_is_live(self) -> None:
        assert is_live(NOW, NOW, WINDOW_SECONDS)

    def test_a_future_bucket_is_not_live(self) -> None:
        assert not is_live(1010, NOW, WINDOW_SECONDS)

    def test_lower_edge_is_inclusive_upper_edge_is_exclusive(self) -> None:
        start = 500
        assert is_live(start, start, WINDOW_SECONDS)
        assert is_live(start, start + WINDOW_SECONDS - 1, WINDOW_SECONDS)
        assert not is_live(start, start + WINDOW_SECONDS, WINDOW_SECONDS)

    def test_exactly_bucket_count_buckets_are_live_at_any_instant(self) -> None:
        live = [
            start
            for start in range(0, NOW + BUCKET_SECONDS, BUCKET_SECONDS)
            if is_live(start, NOW, WINDOW_SECONDS)
        ]

        assert live == list(LIVE_BUCKETS)
        assert len(live) == WINDOW_SECONDS // BUCKET_SECONDS


class TestClassify:
    """ADR-0011 decision 2's disposition table, at `now = 1000`.

    Defaults: `W = 300`, `allowed_lateness_seconds = 30`, so the lateness
    horizon is 330 seconds and the `[300, 330]` band is the zone where an
    observation is inside the horizon but its bucket has already left the
    window.

    `EXPIRED` versus `APPLY` is judged on the **bucket**
    `S = bucket_start(window_start, B)` -- `APPLY` iff `is_live(S, now, W)` --
    never on the raw age of `window_start`; `FUTURE` and `LATE` stay raw-age
    tests, because the lateness horizon is a policy on event age rather than
    on bucket geometry. For the aligned `window_start` that ingest publishes
    the two readings coincide, so most cases below read the same either way;
    the unaligned cases are the ones that tell them apart.
    """

    config = DetectionConfig()

    def test_defaults_are_the_geometry_these_cases_assume(self) -> None:
        assert self.config.window_seconds == WINDOW_SECONDS
        assert self.config.bucket_seconds == BUCKET_SECONDS
        assert self.config.allowed_lateness_seconds == 30

    @pytest.mark.parametrize("window_start", [1001, 1010, 2000])
    def test_anything_ahead_of_now_is_future(self, window_start: int) -> None:
        assert classify(window_start, NOW, self.config) is Disposition.FUTURE

    def test_future_beats_late(self) -> None:
        # is_within_lateness already rejects a negative age, so 1010 would
        # otherwise fall out as LATE; FUTURE is checked first so the metric
        # names the actual cause (ADR-0011 decision 2).
        assert classify(1010, NOW, self.config) is Disposition.FUTURE
        assert classify(1010, NOW, self.config) is not Disposition.LATE

    def test_beyond_the_lateness_horizon_is_late(self) -> None:
        # age 331 > 300 + 30.
        assert classify(669, NOW, self.config) is Disposition.LATE

    def test_exactly_at_the_lateness_horizon_is_expired_not_late(self) -> None:
        # age 330 is inside the horizon (is_within_lateness is inclusive),
        # but bucket 670 left the window at 970, so it cannot be counted.
        assert classify(670, NOW, self.config) is Disposition.EXPIRED

    def test_exactly_one_window_old_is_expired(self) -> None:
        # age 300: 700 + 300 <= 1000, the bucket that just expired.
        assert classify(700, NOW, self.config) is Disposition.EXPIRED

    def test_the_oldest_live_bucket_is_applied(self) -> None:
        assert classify(710, NOW, self.config) is Disposition.APPLY

    def test_the_current_bucket_is_applied(self) -> None:
        assert classify(NOW, NOW, self.config) is Disposition.APPLY

    @pytest.mark.parametrize("window_start", [710, 720, 850, 990, 1000])
    def test_every_live_bucket_is_applied(self, window_start: int) -> None:
        assert classify(window_start, NOW, self.config) is Disposition.APPLY

    def test_an_unaligned_start_is_judged_on_its_bucket_not_on_its_age(self) -> None:
        # ADR-0011 decision 2's worked example. 705 has a raw age of 295 --
        # inside the window as an age -- but bucket_start(705, 10) is 700,
        # which left the window at exactly 1000, so it is EXPIRED. 715 rounds
        # down to 710, which is live, so it is APPLY. The bucket rule is not a
        # free choice: decision 4 applies an APPLY observation with
        # counter.observe(S, ...), which raises for a dead S, so an age-based
        # reading would turn a valid observation into a crash.
        assert bucket_start(705, BUCKET_SECONDS) == 700
        assert not is_live(700, NOW, WINDOW_SECONDS)
        assert classify(705, NOW, self.config) is Disposition.EXPIRED

        assert bucket_start(715, BUCKET_SECONDS) == 710
        assert is_live(710, NOW, WINDOW_SECONDS)
        assert classify(715, NOW, self.config) is Disposition.APPLY

    def test_an_unaligned_now_does_not_move_the_bucket_edge(self) -> None:
        # `now` is never rounded either: liveness is `S <= now < S + W` against
        # the service clock as it stands. At now = 1005, 700 is still dead
        # (700 + 300 <= 1005) and 710 is still live (710 + 300 > 1005).
        assert classify(700, 1005, self.config) is Disposition.EXPIRED
        assert classify(710, 1005, self.config) is Disposition.APPLY
        assert classify(705, 1005, self.config) is Disposition.EXPIRED
        assert classify(715, 1005, self.config) is Disposition.APPLY

    def test_a_live_bucket_is_never_also_late(self) -> None:
        # The four dispositions stay total and disjoint under the bucket rule:
        # a live S implies now - window_start < W, so nothing is both LATE and
        # countable (ADR-0011 decision 2).
        for window_start in range(600, 1101):
            disposition = classify(window_start, NOW, self.config)
            live = is_live(bucket_start(window_start, BUCKET_SECONDS), NOW, WINDOW_SECONDS)
            if disposition is Disposition.APPLY:
                assert live
                assert NOW - window_start < WINDOW_SECONDS
            else:
                assert not (live and window_start <= NOW)

    def test_allowed_lateness_does_not_widen_what_is_counted(self) -> None:
        # ADR-0011 decision 2: a bucket that has left the window cannot
        # contribute whatever the policy says. A huge lateness allowance only
        # moves the LATE/EXPIRED boundary, never the APPLY one.
        generous = DetectionConfig(allowed_lateness_seconds=600, state_retention_seconds=900)

        assert classify(700, NOW, generous) is Disposition.EXPIRED
        assert classify(500, NOW, generous) is Disposition.EXPIRED
        assert classify(710, NOW, generous) is Disposition.APPLY

    def test_zero_lateness_leaves_no_expired_band_beyond_the_window(self) -> None:
        strict = DetectionConfig(allowed_lateness_seconds=0)

        assert classify(700, NOW, strict) is Disposition.EXPIRED  # age exactly 300
        assert classify(699, NOW, strict) is Disposition.LATE  # age 301 > 300 + 0
        assert classify(710, NOW, strict) is Disposition.APPLY

    def test_every_observation_gets_exactly_one_disposition(self) -> None:
        # "applied XOR reconciled, never neither" starts here: classify is
        # total over the integers around the boundaries.
        for window_start in range(600, 1100):
            assert classify(window_start, NOW, self.config) in set(Disposition)


class TestStoreConstruction:
    """ADR-0011 section 9: the store owns a geometry, under IpCounter's rule.

    `window_seconds` / `bucket_seconds` are read-only properties reporting the
    shape every counter the store creates will have, so the geometry in force
    is observable without creating an entry.
    """

    def test_geometry_is_exposed(self) -> None:
        store = _store()

        assert store.window_seconds == WINDOW_SECONDS
        assert store.bucket_seconds == BUCKET_SECONDS

    def test_a_non_default_geometry_is_the_one_new_entries_get(self) -> None:
        store = _store(window_seconds=600, bucket_seconds=20)

        entry = store.get_or_create(IP_A, now=NOW)

        assert store.window_seconds == 600
        assert store.bucket_seconds == 20
        assert entry.counter.window_seconds == 600
        assert entry.counter.bucket_seconds == 20
        assert entry.counter.bucket_count == 30

    @pytest.mark.parametrize(
        ("window_seconds", "bucket_seconds"),
        [(305, 10), (300, 7), (10, 300), (0, 10), (300, 0), (-300, 10), (300, -10)],
    )
    def test_rejects_exactly_the_geometries_the_counter_rejects(
        self, window_seconds: int, bucket_seconds: int
    ) -> None:
        # Same rule as IpCounter.__init__: both > 0 and W % B == 0. A store
        # whose geometry no counter could take would fail only on the first
        # get_or_create, long after the operator could act on it.
        with pytest.raises(ValueError):
            _store(window_seconds=window_seconds, bucket_seconds=bucket_seconds)


class TestStoreLookup:
    """ADR-0011 decision 3: one IpEntry per tracked IP, keyed by Address."""

    def test_get_of_an_unknown_ip_is_none(self) -> None:
        store = _store()

        assert store.get(IP_A) is None
        assert IP_A not in store
        assert len(store) == 0
        assert store.tracked_ips == 0

    def test_get_or_create_makes_a_cold_entry_at_now(self) -> None:
        store = _store()

        entry = store.get_or_create(IP_A, now=NOW)

        assert isinstance(entry, IpEntry)
        assert entry.state is IpState.COLD
        assert entry.last_observed == NOW
        assert entry.counter.total == 0
        assert entry.counter.window_seconds == WINDOW_SECONDS
        assert entry.counter.bucket_seconds == BUCKET_SECONDS
        assert len(store) == 1
        assert IP_A in store

    def test_get_or_create_is_idempotent_for_a_known_ip(self) -> None:
        store = _store()
        entry = store.get_or_create(IP_A, now=NOW)
        entry.counter.observe(NOW, 4, now=NOW)

        again = store.get_or_create(IP_A, now=NOW + 5)

        assert again is entry
        assert again.counter.total == 4
        assert len(store) == 1

    def test_get_or_create_does_not_refresh_last_observed_on_a_known_ip(self) -> None:
        # ADR-0011 decision 3: `now=` seeds a *new* entry only; on an existing
        # one it is ignored. `last_observed` is the time of the last *applied*
        # observation, which the worker writes itself after counter.observe,
        # so a bare lookup must not make an idle entry look fresh to
        # retention.
        store = _store()
        entry = store.get_or_create(IP_A, now=NOW)

        again = store.get_or_create(IP_A, now=NOW + 5_000)

        assert again is entry
        assert again.last_observed == NOW
        # The observable consequence: the entry is still evictable on the
        # schedule its creation time set, not the lookup's.
        assert evict_idle(store, now=NOW + 600, state_retention_seconds=600) == 1
        assert IP_A not in store

    def test_get_returns_the_same_entry_object(self) -> None:
        store = _store()
        entry = store.get_or_create(IP_A, now=NOW)

        assert store.get(IP_A) is entry

    def test_distinct_ips_get_distinct_entries(self) -> None:
        store = _store()

        entry_a = store.get_or_create(IP_A, now=NOW)
        entry_b = store.get_or_create(IP_B, now=NOW)

        assert entry_a is not entry_b
        assert entry_a.counter is not entry_b.counter
        assert len(store) == 2


class TestStoreEntriesAndRemoval:
    def test_entries_are_in_first_seen_order(self) -> None:
        store = _store()
        store.get_or_create(IP_C, now=NOW)
        store.get_or_create(IP_A, now=NOW + 1)
        store.get_or_create(IP_B, now=NOW + 2)
        # Touching an existing entry does not reorder it.
        store.get_or_create(IP_C, now=NOW + 3)

        assert [ip for ip, _ in store.entries()] == [IP_C, IP_A, IP_B]

    def test_entries_is_a_snapshot_safe_to_mutate_under(self) -> None:
        store = _store()
        for ip in (IP_A, IP_B, IP_C):
            store.get_or_create(ip, now=NOW)

        seen: list[Address] = []
        for ip, _entry in store.entries():
            seen.append(ip)
            store.remove(ip)

        assert seen == [IP_A, IP_B, IP_C]
        assert len(store) == 0

    def test_entries_snapshot_does_not_change_when_the_store_does(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=NOW)
        snapshot = store.entries()

        store.get_or_create(IP_B, now=NOW)

        assert [ip for ip, _ in snapshot] == [IP_A]

    def test_remove_of_an_absent_ip_is_a_no_op(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=NOW)

        store.remove(IP_B)  # must not raise
        store.remove(IP_B)

        assert len(store) == 1
        assert IP_A in store

    def test_remove_forgets_the_entry(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=NOW)

        store.remove(IP_A)

        assert IP_A not in store
        assert store.get(IP_A) is None
        assert store.tracked_ips == 0

    def test_an_ip_can_be_re_created_after_removal(self) -> None:
        store = _store()
        first = store.get_or_create(IP_A, now=NOW)
        first.state = IpState.HOT
        store.remove(IP_A)

        second = store.get_or_create(IP_A, now=NOW + 10)

        assert second is not first
        assert second.state is IpState.COLD
        assert second.last_observed == NOW + 10


class TestStoreGauges:
    """Section 26 / section 37: tracked vs. active vs. hot."""

    def test_gauges_start_at_zero(self) -> None:
        store = _store()

        assert store.tracked_ips == 0
        assert store.hot_ips == 0
        assert store.active_ips == 0

    def test_tracked_counts_every_entry(self) -> None:
        store = _store()
        for ip in (IP_A, IP_B, IP_C):
            store.get_or_create(ip, now=NOW)

        assert store.tracked_ips == len(store) == 3

    def test_hot_counts_only_hot_entries(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=NOW)
        entry_b = store.get_or_create(IP_B, now=NOW)
        entry_b.state = IpState.HOT

        assert store.hot_ips == 1
        assert store.tracked_ips == 2

    def test_active_counts_entries_with_a_non_empty_window(self) -> None:
        # Section 26: "active IPs with recent observations" is a different
        # population from "currently HOT IPs", and the store may hold many
        # more IPs than either.
        store = _store()
        store.get_or_create(IP_A, now=NOW)  # tracked, empty window
        entry_b = store.get_or_create(IP_B, now=NOW)
        entry_b.state = IpState.HOT  # hot, but its window is empty
        entry_c = store.get_or_create(IP_C, now=NOW)
        entry_c.counter.observe(NOW, 5, now=NOW)  # active, but COLD

        assert store.tracked_ips == 3
        assert store.hot_ips == 1
        assert store.active_ips == 1

    def test_active_drops_when_the_window_empties(self) -> None:
        store = _store()
        entry = store.get_or_create(IP_A, now=NOW)
        entry.counter.observe(710, 5, now=NOW)

        assert store.active_ips == 1

        entry.counter.expire(1010)

        assert store.active_ips == 0
        assert store.tracked_ips == 1


class TestStoreCapacity:
    """ADR-0011 decision 3's hard cap and its eviction rule."""

    def test_oldest_cold_entry_is_evicted_when_full(self) -> None:
        store = _store(max_tracked_ips=2)
        store.get_or_create(IP_A, now=1000)
        store.get_or_create(IP_B, now=1010)

        store.get_or_create(IP_C, now=1020)

        assert IP_A not in store  # oldest last_observed among COLD entries
        assert IP_B in store
        assert IP_C in store
        assert len(store) == 2

    def test_eviction_looks_at_last_observed_not_insertion_order(self) -> None:
        store = _store(max_tracked_ips=2)
        entry_a = store.get_or_create(IP_A, now=1000)
        store.get_or_create(IP_B, now=1010)
        entry_a.last_observed = 2000  # an observation was applied to A

        store.get_or_create(IP_C, now=2010)

        assert IP_B not in store
        assert IP_A in store
        assert IP_C in store

    def test_a_hot_entry_is_never_the_victim(self) -> None:
        store = _store(max_tracked_ips=2)
        entry_a = store.get_or_create(IP_A, now=1000)
        entry_a.state = IpState.HOT  # oldest, but HOT
        store.get_or_create(IP_B, now=1010)

        store.get_or_create(IP_C, now=1020)

        assert IP_A in store
        assert IP_B not in store
        assert IP_C in store

    def test_store_full_error_when_every_entry_is_hot(self) -> None:
        store = _store(max_tracked_ips=2)
        for ip, when in ((IP_A, 1000), (IP_B, 1010)):
            store.get_or_create(ip, now=when).state = IpState.HOT

        with pytest.raises(StoreFullError):
            store.get_or_create(IP_C, now=1020)

        assert len(store) == 2
        assert IP_C not in store
        assert IP_A in store
        assert IP_B in store

    def test_a_known_ip_does_not_evict_anything_on_a_full_store(self) -> None:
        store = _store(max_tracked_ips=2)
        entry_a = store.get_or_create(IP_A, now=1000)
        store.get_or_create(IP_B, now=1010)

        assert store.get_or_create(IP_A, now=1020) is entry_a
        assert len(store) == 2
        assert IP_B in store

    def test_a_known_hot_ip_is_returned_rather_than_refused(self) -> None:
        store = _store(max_tracked_ips=2)
        entry_a = store.get_or_create(IP_A, now=1000)
        entry_a.state = IpState.HOT
        store.get_or_create(IP_B, now=1010).state = IpState.HOT

        assert store.get_or_create(IP_A, now=1020) is entry_a
        assert len(store) == 2

    def test_capacity_one_still_admits_new_cold_ips(self) -> None:
        store = _store(max_tracked_ips=1)
        store.get_or_create(IP_A, now=1000)

        store.get_or_create(IP_B, now=1010)

        assert IP_A not in store
        assert IP_B in store
        assert len(store) == 1


class TestStoreRebucket:
    """ADR-0011 decision 7 step 2, applied across the whole store."""

    def test_every_entry_is_rebuilt_under_the_new_geometry(self) -> None:
        store = _store()
        pairs_a = [(980, 1), (990, 2)]
        pairs_b = [(710, 4)]
        entry_a = store.get_or_create(IP_A, now=NOW)
        for start, delta in pairs_a:
            entry_a.counter.observe(start, delta, now=NOW)
        entry_b = store.get_or_create(IP_B, now=NOW)
        for start, delta in pairs_b:
            entry_b.counter.observe(start, delta, now=NOW)

        store.rebucket(window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW)

        rebuilt_a = store.get(IP_A)
        rebuilt_b = store.get(IP_B)
        assert rebuilt_a is not None
        assert rebuilt_b is not None
        assert rebuilt_a.counter.buckets() == _live_model(
            pairs_a, window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW
        )
        assert rebuilt_a.counter.total == 3
        # bucket_start(710, 20) == 700 and 700 + 300 <= 1000, so B's only
        # bucket does not survive the coarser alignment.
        assert rebuilt_b.counter.buckets() == ()
        assert rebuilt_b.counter.total == 0

    def test_rebucketed_counters_carry_the_new_geometry(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=NOW).counter.observe(1000, 1, now=NOW)

        store.rebucket(window_seconds=600, bucket_seconds=20, now=NOW)

        entry = store.get(IP_A)
        assert entry is not None
        assert entry.counter.window_seconds == 600
        assert entry.counter.bucket_seconds == 20
        assert entry.counter.bucket_count == 30

    def test_rebucket_keeps_state_last_observed_and_order(self) -> None:
        # Re-evaluation must not reset a running service's state: only the
        # counters are rebuilt.
        store = _store()
        entry_a = store.get_or_create(IP_A, now=900)
        entry_a.state = IpState.HOT
        store.get_or_create(IP_B, now=950)

        store.rebucket(window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW)

        assert [ip for ip, _ in store.entries()] == [IP_A, IP_B]
        kept_a = store.get(IP_A)
        kept_b = store.get(IP_B)
        assert kept_a is not None
        assert kept_b is not None
        assert kept_a.state is IpState.HOT
        assert kept_a.last_observed == 900
        assert kept_b.state is IpState.COLD
        assert kept_b.last_observed == 950
        assert store.tracked_ips == 2

    def test_rebucket_evicts_nobody(self) -> None:
        store = _store()
        for ip in (IP_A, IP_B, IP_C):
            store.get_or_create(ip, now=NOW)

        store.rebucket(window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW)

        assert len(store) == 3

    def test_rebucket_of_an_empty_store_is_harmless(self) -> None:
        store = _store()

        store.rebucket(window_seconds=WINDOW_SECONDS, bucket_seconds=20, now=NOW)

        assert len(store) == 0

    def test_rebucket_replaces_the_stores_own_geometry(self) -> None:
        # ADR-0011 decision 7 step 2: the store's window_seconds /
        # bucket_seconds are replaced too, not just the rebuilt counters'.
        store = _store()

        store.rebucket(window_seconds=600, bucket_seconds=20, now=NOW)

        assert store.window_seconds == 600
        assert store.bucket_seconds == 20

    def test_an_entry_created_after_rebucket_gets_the_new_geometry(self) -> None:
        # Without this, an observation arriving for an IP first seen after a
        # re-evaluation would be classified under the new geometry by
        # `classify` (which reads the config) and then validated against the
        # old one by its counter.
        store = _store()
        store.get_or_create(IP_A, now=NOW)

        store.rebucket(window_seconds=600, bucket_seconds=20, now=NOW)
        fresh = store.get_or_create(IP_B, now=NOW)

        assert fresh.counter.window_seconds == 600
        assert fresh.counter.bucket_seconds == 20
        assert fresh.counter.bucket_count == 30
        # 700 is dead under the old 300 s window and live under the new 600 s
        # one, so this only succeeds if the new entry really took the new
        # geometry rather than the constructor's.
        assert fresh.counter.observe(700, 3, now=NOW) == 3

    @pytest.mark.parametrize(
        ("window_seconds", "bucket_seconds"),
        [(305, 10), (300, 7), (10, 300), (0, 10), (300, 0), (-300, 10)],
    )
    def test_rebucket_rejects_an_invalid_geometry_even_on_an_empty_store(
        self, window_seconds: int, bucket_seconds: int
    ) -> None:
        # ADR-0011 decision 7 step 2: validated up front, with the same rule
        # as IpCounter.__init__, so an invalid geometry cannot be latched
        # silently and surface only on the next get_or_create.
        store = _store()

        with pytest.raises(ValueError):
            store.rebucket(window_seconds=window_seconds, bucket_seconds=bucket_seconds, now=NOW)

        assert store.window_seconds == WINDOW_SECONDS
        assert store.bucket_seconds == BUCKET_SECONDS

    def test_rebucket_rejects_an_invalid_geometry_on_a_populated_store(self) -> None:
        store = _store()
        entry = store.get_or_create(IP_A, now=NOW)
        entry.counter.observe(1000, 5, now=NOW)

        with pytest.raises(ValueError):
            store.rebucket(window_seconds=305, bucket_seconds=10, now=NOW)

        assert store.window_seconds == WINDOW_SECONDS
        assert store.bucket_seconds == BUCKET_SECONDS
        assert entry.counter.total == 5


class TestExpireBuckets:
    """`expiry.expire_buckets`: section 5's sweep across every entry."""

    def test_returns_nothing_when_no_bucket_has_expired(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=NOW).counter.observe(710, 5, now=NOW)
        store.get_or_create(IP_B, now=NOW).counter.observe(1000, 3, now=NOW)

        assert expire_buckets(store, now=1005) == []

    def test_returns_only_the_ips_whose_total_changed(self) -> None:
        store = _store()
        entry_a = store.get_or_create(IP_A, now=NOW)
        entry_a.counter.observe(710, 5, now=NOW)
        entry_b = store.get_or_create(IP_B, now=NOW)
        entry_b.counter.observe(1000, 3, now=NOW)
        store.get_or_create(IP_C, now=NOW)  # never observed at all

        assert expire_buckets(store, now=1010) == [IP_A]
        assert entry_a.counter.total == 0
        assert entry_b.counter.total == 3

    def test_changed_ips_come_back_in_store_order(self) -> None:
        store = _store()
        # Deliberately created in an order different from their addresses'.
        for ip in (IP_C, IP_A, IP_B):
            store.get_or_create(ip, now=NOW).counter.observe(710, 1, now=NOW)

        assert expire_buckets(store, now=1010) == [IP_C, IP_A, IP_B]

    def test_is_idempotent(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=NOW).counter.observe(710, 5, now=NOW)

        assert expire_buckets(store, now=1010) == [IP_A]
        assert expire_buckets(store, now=1010) == []
        assert expire_buckets(store, now=1010) == []

    def test_partial_expiry_still_reports_the_ip(self) -> None:
        store = _store()
        entry = store.get_or_create(IP_A, now=NOW)
        entry.counter.observe(710, 5, now=NOW)
        entry.counter.observe(1000, 2, now=NOW)

        assert expire_buckets(store, now=1010) == [IP_A]
        assert entry.counter.total == 2

    def test_expiry_does_not_evict_or_change_state(self) -> None:
        # Decision 3: the HotIpRemoved edge comes from re-evaluating the
        # entry, not from expire_buckets, which only moves counts.
        store = _store()
        entry = store.get_or_create(IP_A, now=NOW)
        entry.state = IpState.HOT
        entry.counter.observe(710, 5, now=NOW)

        expire_buckets(store, now=1010)

        assert IP_A in store
        assert entry.state is IpState.HOT
        assert entry.last_observed == NOW
        assert store.tracked_ips == 1

    def test_empty_store_sweeps_to_nothing(self) -> None:
        assert expire_buckets(_store(), now=NOW) == []


class TestEvictIdle:
    """Section 26 retention, ADR-0011 decision 3: COLD and idle only."""

    def test_entry_idle_for_exactly_the_retention_is_evicted(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=400)  # now - last_observed == 600

        assert evict_idle(store, now=1000, state_retention_seconds=600) == 1
        assert IP_A not in store
        assert len(store) == 0

    def test_entry_one_second_short_of_the_retention_is_kept(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=401)  # age 599

        assert evict_idle(store, now=1000, state_retention_seconds=600) == 0
        assert IP_A in store

    def test_a_hot_entry_is_never_evicted_by_retention(self) -> None:
        # Evicting it would orphan a HOT record in the trie: its buckets
        # expire first, a sweep re-evaluates it to COLD and emits
        # HotIpRemoved, and only a later sweep may evict it.
        store = _store()
        store.get_or_create(IP_A, now=0).state = IpState.HOT

        assert evict_idle(store, now=100_000, state_retention_seconds=600) == 0
        assert IP_A in store

    def test_returns_how_many_were_removed(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=100)  # idle
        store.get_or_create(IP_B, now=200)  # idle
        store.get_or_create(IP_C, now=999)  # fresh

        assert evict_idle(store, now=1000, state_retention_seconds=600) == 2
        assert [ip for ip, _ in store.entries()] == [IP_C]
        assert store.tracked_ips == 1

    def test_an_entry_that_became_cold_again_becomes_evictable(self) -> None:
        store = _store()
        entry = store.get_or_create(IP_A, now=100)
        entry.state = IpState.HOT

        assert evict_idle(store, now=1000, state_retention_seconds=600) == 0

        entry.state = IpState.COLD

        assert evict_idle(store, now=1000, state_retention_seconds=600) == 1
        assert IP_A not in store

    def test_eviction_is_judged_on_arrival_time_not_event_time(self) -> None:
        # last_observed is the service-clock time an observation was
        # *applied*; a fresh application keeps the entry regardless of how
        # old the bucket it landed in was.
        store = _store()
        entry = store.get_or_create(IP_A, now=100)
        entry.counter.observe(710, 1, now=NOW)
        entry.last_observed = NOW

        assert evict_idle(store, now=1500, state_retention_seconds=600) == 0
        assert IP_A in store

    def test_is_idempotent_and_safe_on_an_empty_store(self) -> None:
        store = _store()
        store.get_or_create(IP_A, now=100)

        assert evict_idle(store, now=1000, state_retention_seconds=600) == 1
        assert evict_idle(store, now=1000, state_retention_seconds=600) == 0

    def test_expiry_then_eviction_is_the_maintenance_order(self) -> None:
        # run_maintenance expires buckets before evicting, so an IP whose
        # window has just emptied is still present for the re-evaluation
        # that would emit its HotIpRemoved.
        store = _store()
        entry = store.get_or_create(IP_A, now=NOW)
        entry.state = IpState.HOT
        entry.counter.observe(710, 5, now=NOW)

        changed = expire_buckets(store, now=1010)
        evicted = evict_idle(store, now=1010, state_retention_seconds=600)

        assert changed == [IP_A]
        assert evicted == 0
        assert entry.counter.total == 0
        assert IP_A in store
