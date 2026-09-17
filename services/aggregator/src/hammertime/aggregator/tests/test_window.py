"""Bucket expiry, running total, and out-of-order writes into live buckets.

Spec: section 5 (the ring and its running total), section 24 (out-of-order
observations), section 25 (deterministic bucket arithmetic), section 26
(retention and what bounds the store), section 34 (a configuration change
must define its effect on existing state), section 37 (`tracked_ips` /
`active_ips` / `hot_ips`).

The interface under test is ADR-0011 decision 2 -- `IpCounter`
(`hammertime.aggregator.window.counter`) and `ShardWindow` / `WindowChange`
(`hammertime.aggregator.window.store`) -- and every assertion below is
derived from that decision's bullets plus the spec sections above, never
from the modules themselves.

The two rules everything else follows from:

* A bucket starting at `S` (a multiple of `bucket_seconds`) is live at `now`
  iff `bucket_start(now) - S < window_seconds`; it leaves the window at
  exactly `now = S + window_seconds` (section 5's ADR-0011 note).
* The total is a running total, maintained on every write and on every
  expiry, never recomputed by summing the ring (section 5).

Conventions and assumptions stated here because they are not pinned by the
spec:

* `BASE` is `1_800_000_000`, the `T0` of `docs/spec/integration-scenarios.md`
  section 2. It is a multiple of 300, hence of every bucket size used below
  (10 s and 30 s), so bucket arithmetic in the assertions stays exact.
* `IpState` is imported from `hammertime.core.state.enums` and `Address`
  from `hammertime.core.addressing.address`, following
  `tests/property/test_state_machine.py` and
  `services/ingest/.../tests/test_pipeline.py` respectively.
* `_bucket_start` is a local restatement of section 25's formula rather than
  an import, so these tests depend on the arithmetic the spec fixes and not
  on a particular helper signature.
* Nothing here asserts whether an *inherited* HOT IP is also `is_tracked`
  or counted by `tracked_count` at construction time: ADR-0011 decision 2
  says an inherited IP has a state and an `inherited` flag, but never says
  whether it occupies a store entry (and therefore a capacity slot) before
  its first observation. That gap is reported rather than guessed at.
"""

from __future__ import annotations

import pytest
from hammertime.aggregator.window.counter import IpCounter
from hammertime.aggregator.window.store import ShardWindow, WindowChange
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import ManualClock
from hypothesis import given, settings
from hypothesis import strategies as st

# Section 5's worked example: window = 300 s, bucket = 10 s, 30 buckets.
BUCKET_SECONDS = 10
BUCKET_COUNT = 30
WINDOW_SECONDS = 300

# `T0` from docs/spec/integration-scenarios.md section 2; a multiple of 300.
BASE = 1_800_000_000

IP_A = Address.parse("198.51.100.1")
IP_B = Address.parse("198.51.100.2")
IP_C = Address.parse("198.51.100.3")
IP_D = Address.parse("198.51.100.4")


def _bucket_start(timestamp: int, bucket_seconds: int) -> int:
    """Section 25: `bucket_start = floor(event_timestamp / B) * B`."""

    return (timestamp // bucket_seconds) * bucket_seconds


def _counter() -> IpCounter:
    return IpCounter(bucket_seconds=BUCKET_SECONDS, bucket_count=BUCKET_COUNT)


def _config(
    *,
    window_seconds: int = 300,
    bucket_seconds: int = 10,
    hot_threshold: int = 1000,
    cold_threshold: int = 800,
    allowed_lateness_seconds: int = 30,
    state_retention_seconds: int = 600,
) -> DetectionConfig:
    """The shipped defaults of `config/detection.v1.json`, overridable.

    Section 34's example document plus ADR-0002's `allowed_lateness` (30 s)
    and section 26's retention example (10 minutes).
    """

    return DetectionConfig(
        window_seconds=window_seconds,
        bucket_seconds=bucket_seconds,
        hot_threshold=hot_threshold,
        cold_threshold=cold_threshold,
        allowed_lateness_seconds=allowed_lateness_seconds,
        state_retention_seconds=state_retention_seconds,
    )


def _window(
    *,
    clock: ManualClock,
    config: DetectionConfig | None = None,
    inherited_hot: tuple[Address, ...] = (),
    max_tracked_ips: int = 1_000_000,
) -> ShardWindow:
    return ShardWindow(
        shard=0,
        config=config if config is not None else _config(),
        clock=clock,
        inherited_hot=inherited_hot,
        max_tracked_ips=max_tracked_ips,
    )


class TestIpCounterGeometry:
    """Section 5: `window = bucket * bucket_count`."""

    def test_window_seconds_is_the_product_of_the_two_dimensions(self) -> None:
        counter = _counter()
        assert counter.bucket_seconds == BUCKET_SECONDS
        assert counter.bucket_count == BUCKET_COUNT
        assert counter.window_seconds == WINDOW_SECONDS

    def test_a_fresh_counter_is_empty(self) -> None:
        counter = _counter()
        assert counter.total == 0
        assert counter.live_buckets(BASE) == ()
        assert counter.next_expiry() is None


class TestIpCounterObserve:
    """ADR-0011 decision 2, `observe(S, delta, now)`."""

    def test_a_write_into_a_live_bucket_is_applied_and_raises_the_total(self) -> None:
        counter = _counter()
        assert counter.observe(BASE, 5, BASE) is True
        assert counter.total == 5

    def test_repeated_writes_into_one_bucket_accumulate(self) -> None:
        counter = _counter()
        assert counter.observe(BASE, 5, BASE) is True
        assert counter.observe(BASE, 7, BASE) is True
        assert counter.total == 12
        assert counter.live_buckets(BASE) == ((BASE, 12),)

    def test_writes_into_distinct_live_buckets_accumulate(self) -> None:
        counter = _counter()
        counter.observe(BASE - 290, 1, BASE)
        counter.observe(BASE - 150, 2, BASE)
        counter.observe(BASE, 3, BASE)
        assert counter.total == 6

    def test_a_zero_delta_is_applied_and_changes_nothing(self) -> None:
        # ADR-0011 decision 2: "`delta == 0` is applied (returns `True`,
        # changes nothing)". A zero-count slot is not a live bucket for
        # reporting purposes -- `live_buckets` and `next_expiry` are both
        # defined over *non-zero* slots.
        counter = _counter()
        assert counter.observe(BASE, 0, BASE) is True
        assert counter.total == 0
        assert counter.live_buckets(BASE) == ()
        assert counter.next_expiry() is None

    def test_a_negative_delta_is_a_value_error(self) -> None:
        counter = _counter()
        with pytest.raises(ValueError):
            counter.observe(BASE, -1, BASE)

    def test_a_negative_delta_leaves_the_counter_untouched(self) -> None:
        counter = _counter()
        counter.observe(BASE, 4, BASE)
        with pytest.raises(ValueError):
            counter.observe(BASE, -1, BASE)
        assert counter.total == 4
        assert counter.live_buckets(BASE) == ((BASE, 4),)

    def test_a_write_into_an_expired_bucket_is_refused_and_changes_nothing(self) -> None:
        # `bucket_start(now) - S >= window_seconds`: the delta can never
        # affect a future window count (section 5's ADR-0011 note), so it is
        # refused rather than applied.
        counter = _counter()
        counter.observe(BASE, 4, BASE)
        assert counter.observe(BASE - WINDOW_SECONDS, 99, BASE) is False
        assert counter.total == 4
        assert counter.live_buckets(BASE) == ((BASE, 4),)
        assert counter.next_expiry() == BASE + WINDOW_SECONDS

    def test_a_refused_write_into_an_empty_counter_creates_nothing(self) -> None:
        counter = _counter()
        assert counter.observe(BASE - WINDOW_SECONDS - 10, 99, BASE) is False
        assert counter.total == 0
        assert counter.live_buckets(BASE) == ()
        assert counter.next_expiry() is None


class TestIpCounterLiveness:
    """The boundary: live iff `bucket_start(now) - S < window_seconds`, so a
    bucket leaves the window at exactly `now = S + window_seconds`."""

    def test_a_bucket_is_live_one_second_before_its_window_ends(self) -> None:
        assert _counter().is_live(BASE, BASE + WINDOW_SECONDS - 1) is True

    def test_a_bucket_is_gone_exactly_at_its_window_end(self) -> None:
        assert _counter().is_live(BASE, BASE + WINDOW_SECONDS) is False

    def test_the_bucket_containing_now_is_live(self) -> None:
        assert _counter().is_live(BASE, BASE) is True
        assert _counter().is_live(BASE, BASE + BUCKET_SECONDS - 1) is True

    def test_observe_accepts_at_the_last_live_instant(self) -> None:
        counter = _counter()
        assert counter.observe(BASE, 4, BASE + WINDOW_SECONDS - 1) is True
        assert counter.total == 4

    def test_observe_refuses_at_the_window_end(self) -> None:
        counter = _counter()
        assert counter.observe(BASE, 4, BASE + WINDOW_SECONDS) is False
        assert counter.total == 0

    @pytest.mark.parametrize("age", [0, 1, 10, 150, 290, 299])
    def test_every_age_below_the_window_is_live(self, age: int) -> None:
        assert _counter().is_live(BASE, BASE + age) is True

    @pytest.mark.parametrize("age", [300, 301, 310, 1_000])
    def test_every_age_at_or_beyond_the_window_is_gone(self, age: int) -> None:
        assert _counter().is_live(BASE, BASE + age) is False


class TestIpCounterOutOfOrder:
    """Section 24 / ADR-0002: the counter must accept writes into *any* live
    bucket, not just the newest, and keep the running total exact."""

    def test_section_twenty_fours_worked_arrival_order(self) -> None:
        # Section 24's example arrival order -- :20, :40, :30 -- against
        # three live buckets.
        counter = _counter()
        assert counter.observe(BASE - 280, 2, BASE) is True
        assert counter.observe(BASE - 260, 3, BASE) is True
        assert counter.observe(BASE - 270, 5, BASE) is True
        assert counter.total == 10
        assert counter.live_buckets(BASE) == (
            (BASE - 280, 2),
            (BASE - 270, 5),
            (BASE - 260, 3),
        )

    def test_a_late_write_into_the_oldest_live_bucket_still_counts(self) -> None:
        counter = _counter()
        counter.observe(BASE, 100, BASE)
        assert counter.observe(BASE - 290, 7, BASE) is True
        assert counter.total == 107


_LIVE_BUCKET = st.integers(min_value=0, max_value=BUCKET_COUNT - 1).map(
    lambda index: BASE - index * BUCKET_SECONDS
)
_DELTA = st.integers(min_value=0, max_value=5_000)
_WRITES = st.lists(st.tuples(_LIVE_BUCKET, _DELTA), min_size=1, max_size=40)


@st.composite
def _writes_in_two_orders(
    draw: st.DrawFn,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    writes = draw(_WRITES)
    return writes, draw(st.permutations(writes))


def _apply(writes: list[tuple[int, int]], *, now: int = BASE) -> IpCounter:
    counter = _counter()
    for bucket, delta in writes:
        assert counter.observe(bucket, delta, now) is True
    return counter


@given(writes=_WRITES)
@settings(deadline=None, max_examples=200)
def test_the_total_is_the_sum_of_every_applied_delta(writes: list[tuple[int, int]]) -> None:
    """Section 5 / section 24, the property-style out-of-order test: an
    arbitrary multiset of `(bucket_start, delta)` pairs inside the live range,
    applied in an arbitrary order, leaves `total` equal to the plain sum."""

    counter = _apply(writes)
    assert counter.total == sum(delta for _bucket, delta in writes)


@given(writes=_WRITES)
@settings(deadline=None, max_examples=200)
def test_each_live_bucket_holds_the_sum_of_its_own_deltas(
    writes: list[tuple[int, int]],
) -> None:
    expected: dict[int, int] = {}
    for bucket, delta in writes:
        expected[bucket] = expected.get(bucket, 0) + delta
    non_zero = {bucket: count for bucket, count in expected.items() if count > 0}

    counter = _apply(writes)
    reported = counter.live_buckets(BASE)

    assert dict(reported) == non_zero
    # Section 5's ring is reported oldest-first.
    assert [bucket for bucket, _count in reported] == sorted(non_zero)
    assert counter.total == sum(non_zero.values())


@given(orders=_writes_in_two_orders())
@settings(deadline=None, max_examples=200)
def test_arrival_order_does_not_change_the_window(
    orders: tuple[list[tuple[int, int]], list[tuple[int, int]]],
) -> None:
    """ADR-0002: "replay is deterministic" -- the same multiset of deltas
    produces the same window whatever order it arrives in."""

    first_order, second_order = orders
    first = _apply(first_order)
    second = _apply(second_order)
    assert first.total == second.total
    assert first.live_buckets(BASE) == second.live_buckets(BASE)
    assert first.next_expiry() == second.next_expiry()


class TestIpCounterExpiry:
    """Section 5: "when a bucket expires, `total -= expired_bucket.count`"."""

    def test_expire_returns_zero_and_changes_nothing_when_nothing_is_due(self) -> None:
        counter = _counter()
        counter.observe(BASE - 290, 4, BASE)
        counter.observe(BASE, 6, BASE)
        assert counter.expire(BASE) == 0
        assert counter.total == 10

    def test_expire_removes_exactly_the_buckets_that_left_the_window(self) -> None:
        counter = _counter()
        counter.observe(BASE - 290, 4, BASE)
        counter.observe(BASE, 6, BASE)
        # `now = (BASE - 290) + 300`: the older bucket has just left.
        assert counter.expire(BASE + 10) == 4
        assert counter.total == 6
        assert counter.live_buckets(BASE + 10) == ((BASE, 6),)

    def test_expire_is_idempotent(self) -> None:
        counter = _counter()
        counter.observe(BASE - 290, 4, BASE)
        assert counter.expire(BASE + 10) == 4
        assert counter.expire(BASE + 10) == 0
        assert counter.total == 0

    def test_expiring_everything_empties_the_counter(self) -> None:
        counter = _counter()
        counter.observe(BASE - 290, 4, BASE)
        counter.observe(BASE, 6, BASE)
        assert counter.expire(BASE + WINDOW_SECONDS) == 10
        assert counter.total == 0
        assert counter.live_buckets(BASE + WINDOW_SECONDS) == ()
        assert counter.next_expiry() is None


class TestIpCounterNextExpiry:
    """`next_expiry()` is `min(S) + W` over non-zero slots, `None` if empty."""

    def test_none_when_empty(self) -> None:
        assert _counter().next_expiry() is None

    def test_the_oldest_non_zero_bucket_decides(self) -> None:
        counter = _counter()
        counter.observe(BASE, 6, BASE)
        assert counter.next_expiry() == BASE + WINDOW_SECONDS
        counter.observe(BASE - 290, 4, BASE)
        assert counter.next_expiry() == BASE - 290 + WINDOW_SECONDS

    def test_it_moves_forward_as_buckets_expire(self) -> None:
        counter = _counter()
        counter.observe(BASE - 290, 4, BASE)
        counter.observe(BASE, 6, BASE)
        counter.expire(BASE + 10)
        assert counter.next_expiry() == BASE + WINDOW_SECONDS

    def test_none_again_once_every_slot_is_zero(self) -> None:
        counter = _counter()
        counter.observe(BASE, 6, BASE)
        counter.expire(BASE + WINDOW_SECONDS)
        assert counter.next_expiry() is None


class TestIpCounterSlotReuse:
    """Two live buckets never share a slot: when the ring wraps, the slot's
    previous occupant is subtracted from the total rather than added to."""

    def test_a_wrapped_slot_forgets_the_bucket_it_held(self) -> None:
        counter = _counter()
        counter.observe(BASE, 6, BASE)
        later = BASE + WINDOW_SECONDS  # same slot: (S // B) % N is unchanged
        assert counter.observe(later, 4, later) is True
        assert counter.total == 4
        assert counter.live_buckets(later) == ((later, 4),)
        assert counter.next_expiry() == later + WINDOW_SECONDS

    def test_slot_reuse_needs_no_explicit_expire_call(self) -> None:
        # The subtraction happens on the write itself, so a counter that is
        # only ever written to still reports an exact total.
        counter = _counter()
        for step in range(5):
            bucket = BASE + step * WINDOW_SECONDS
            assert counter.observe(bucket, 3, bucket) is True
            assert counter.total == 3


class TestIpCounterLiveBuckets:
    """`live_buckets(now)`: non-zero live `(S, count)` pairs, ascending."""

    def test_only_non_zero_live_slots_are_reported_and_in_order(self) -> None:
        counter = _counter()
        counter.observe(BASE, 3, BASE)
        counter.observe(BASE - 100, 0, BASE)
        counter.observe(BASE - 200, 5, BASE)
        assert counter.live_buckets(BASE) == ((BASE - 200, 5), (BASE, 3))

    def test_buckets_that_left_the_window_are_not_reported(self) -> None:
        counter = _counter()
        counter.observe(BASE - 290, 4, BASE)
        counter.observe(BASE, 6, BASE)
        # No `expire` call: liveness is evaluated against `now`.
        assert counter.live_buckets(BASE + 10) == ((BASE, 6),)


class TestShardWindowUntrackedIp:
    """ADR-0011 decision 2: `total` is 0 and `state` is COLD when untracked."""

    def test_an_unknown_ip_is_cold_empty_and_untracked(self) -> None:
        window = _window(clock=ManualClock(initial=BASE))
        assert window.state(IP_A) is IpState.COLD
        assert window.total(IP_A) == 0
        assert window.is_tracked(IP_A) is False
        assert window.tracked_count == 0
        assert window.tracked_ips() == frozenset()
        assert window.hot_ips() == frozenset()

    def test_the_shard_and_sequence_are_what_the_claim_supplied(self) -> None:
        window = _window(clock=ManualClock(initial=BASE))
        assert window.shard == 0
        # Decision 2: `next_sequence` defaults to 0 and is loaded from the
        # state store at claim time (decision 5).
        assert window.next_sequence == 0


class TestShardWindowObserve:
    """`observe` creates the entry, applies the delta, returns the change."""

    def test_the_first_observation_creates_a_cold_entry(self) -> None:
        window = _window(clock=ManualClock(initial=BASE))
        change = window.observe(IP_A, BASE, 5)
        assert change == WindowChange(ip=IP_A, state=IpState.COLD, total_before=0, total_after=5)
        assert window.is_tracked(IP_A) is True
        assert window.total(IP_A) == 5
        assert window.state(IP_A) is IpState.COLD
        assert window.tracked_count == 1

    def test_a_later_observation_reports_the_totals_around_it(self) -> None:
        window = _window(clock=ManualClock(initial=BASE))
        window.observe(IP_A, BASE, 5)
        change = window.observe(IP_A, BASE, 7)
        assert change == WindowChange(ip=IP_A, state=IpState.COLD, total_before=5, total_after=12)

    def test_the_change_carries_the_state_the_store_holds(self) -> None:
        # "the IP's state before any evaluation; the store never evaluates".
        window = _window(clock=ManualClock(initial=BASE))
        window.observe(IP_A, BASE, 5)
        window.set_state(IP_A, IpState.HOT)
        change = window.observe(IP_A, BASE, 1)
        assert change is not None
        assert change.state is IpState.HOT
        assert window.state(IP_A) is IpState.HOT

    def test_an_observation_into_a_dead_bucket_is_refused(self) -> None:
        window = _window(clock=ManualClock(initial=BASE))
        assert window.observe(IP_A, BASE - WINDOW_SECONDS, 5) is None
        assert window.is_tracked(IP_A) is False
        assert window.tracked_count == 0
        assert window.total(IP_A) == 0

    def test_a_refused_observation_does_not_disturb_an_existing_entry(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        clock.advance(WINDOW_SECONDS)
        # `BASE` has just left the window; the delta cannot be counted.
        assert window.observe(IP_A, BASE, 99) is None
        assert window.total(IP_A) == 5


class TestShardWindowExpiry:
    """`expire_due()` -- one `WindowChange` per IP whose total dropped."""

    def test_nothing_is_due_before_a_bucket_leaves_the_window(self) -> None:
        window = _window(clock=ManualClock(initial=BASE))
        window.observe(IP_A, BASE, 5)
        assert window.expire_due() == []
        assert window.total(IP_A) == 5

    def test_one_change_per_ip_whose_total_dropped(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        window.observe(IP_B, BASE, 9)
        window.set_state(IP_B, IpState.HOT)
        clock.advance(310)
        # An IP observed into the current bucket has nothing to expire.
        window.observe(IP_C, _bucket_start(clock.now(), BUCKET_SECONDS), 3)

        changes = {change.ip: change for change in window.expire_due()}

        assert set(changes) == {IP_A, IP_B}
        assert changes[IP_A] == WindowChange(
            ip=IP_A, state=IpState.COLD, total_before=5, total_after=0
        )
        # "with `state` reflecting the state before the sweep": the store
        # never evaluates, so a HOT IP is reported HOT and stays HOT.
        assert changes[IP_B] == WindowChange(
            ip=IP_B, state=IpState.HOT, total_before=9, total_after=0
        )
        assert window.state(IP_B) is IpState.HOT
        assert window.total(IP_C) == 3
        assert window.tracked_count == 3

    def test_a_second_sweep_reports_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        clock.advance(310)
        assert len(window.expire_due()) == 1
        assert window.expire_due() == []
        assert window.total(IP_A) == 0

    def test_a_partial_expiry_reports_the_remaining_total(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        clock.advance(100)
        window.observe(IP_A, _bucket_start(clock.now(), BUCKET_SECONDS), 4)
        clock.advance(WINDOW_SECONDS - 100)  # `BASE` has left, the newer one has not
        (change,) = window.expire_due()
        assert change == WindowChange(ip=IP_A, state=IpState.COLD, total_before=9, total_after=4)
        assert window.total(IP_A) == 4


class TestShardWindowRetention:
    """Section 26: a COLD IP with an empty window is evicted once
    `last_seen + state_retention_seconds <= now`; a HOT IP never is."""

    def test_a_cold_idle_ip_survives_until_the_retention_deadline(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        clock.advance(599)
        window.expire_due()
        assert window.evict_due() == 0
        assert window.is_tracked(IP_A) is True

    def test_a_cold_idle_ip_is_evicted_at_the_retention_deadline(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        clock.advance(600)
        window.expire_due()
        assert window.evict_due() == 1
        assert window.is_tracked(IP_A) is False
        assert window.tracked_count == 0

    def test_retention_eviction_is_counted(self) -> None:
        # Decision 8: `window_evictions{reason="retention"}`.
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        assert window.retention_evictions == 0
        clock.advance(600)
        window.expire_due()
        window.evict_due()
        assert window.retention_evictions == 1

    def test_a_hot_ip_is_never_evicted_by_retention(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        window.observe(IP_B, BASE, 9)
        window.set_state(IP_B, IpState.HOT)
        clock.advance(600)
        window.expire_due()
        assert window.evict_due() == 1
        assert window.is_tracked(IP_A) is False
        assert window.is_tracked(IP_B) is True
        assert window.hot_ips() == frozenset({IP_B})

    def test_a_fresh_observation_postpones_the_deadline(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        clock.advance(100)
        window.observe(IP_A, _bucket_start(clock.now(), BUCKET_SECONDS), 1)
        clock.advance(500)  # 600 s since BASE, only 500 s since `last_seen`
        window.expire_due()
        assert window.evict_due() == 0
        assert window.is_tracked(IP_A) is True


class TestShardWindowCapacity:
    """Decision 2: over the cap, the least-recently-seen COLD IP goes first;
    if every tracked IP is HOT the store grows instead."""

    def _fill(self, window: ShardWindow, clock: ManualClock) -> None:
        window.observe(IP_A, _bucket_start(clock.now(), BUCKET_SECONDS), 1)
        clock.advance(BUCKET_SECONDS)
        window.observe(IP_B, _bucket_start(clock.now(), BUCKET_SECONDS), 1)
        clock.advance(BUCKET_SECONDS)
        window.observe(IP_C, _bucket_start(clock.now(), BUCKET_SECONDS), 1)

    def test_the_least_recently_seen_cold_ip_is_evicted(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, max_tracked_ips=3)
        self._fill(window, clock)
        assert window.tracked_count == 3
        assert window.capacity_evictions == 0

        clock.advance(BUCKET_SECONDS)
        window.observe(IP_D, _bucket_start(clock.now(), BUCKET_SECONDS), 1)

        assert window.capacity_evictions == 1
        assert window.tracked_count == 3
        assert window.tracked_ips() == frozenset({IP_B, IP_C, IP_D})
        assert window.is_tracked(IP_A) is False
        assert window.total(IP_A) == 0

    def test_a_store_of_hot_ips_grows_past_the_cap(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, max_tracked_ips=3)
        self._fill(window, clock)
        for ip in (IP_A, IP_B, IP_C):
            window.set_state(ip, IpState.HOT)

        clock.advance(BUCKET_SECONDS)
        window.observe(IP_D, _bucket_start(clock.now(), BUCKET_SECONDS), 1)

        assert window.capacity_evictions == 0
        assert window.tracked_count == 4
        assert window.tracked_ips() == frozenset({IP_A, IP_B, IP_C, IP_D})
        assert window.hot_ips() == frozenset({IP_A, IP_B, IP_C})


class TestShardWindowWarmup:
    """Decision 5: a claim inherits the shard's HOT set and warms up for one
    window before an inherited IP may be demoted."""

    def test_a_window_with_no_inherited_set_never_warms_up(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        assert window.warm_until is None
        assert window.in_warmup is False
        assert window.finish_warmup_if_due() is None
        clock.advance(WINDOW_SECONDS)
        assert window.finish_warmup_if_due() is None

    def test_an_inherited_ip_starts_hot_and_flagged(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, inherited_hot=(IP_A, IP_B))
        assert window.state(IP_A) is IpState.HOT
        assert window.state(IP_B) is IpState.HOT
        assert window.is_inherited(IP_A) is True
        assert window.hot_ips() == frozenset({IP_A, IP_B})
        assert window.total(IP_A) == 0

    def test_warm_until_is_one_window_after_the_claim(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, inherited_hot=(IP_A, IP_B))
        assert window.warm_until == BASE + WINDOW_SECONDS
        assert window.in_warmup is True

    def test_warmup_is_not_due_before_warm_until(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, inherited_hot=(IP_A, IP_B))
        assert window.finish_warmup_if_due() is None
        clock.advance(WINDOW_SECONDS - 1)
        assert window.finish_warmup_if_due() is None
        assert window.in_warmup is True
        assert window.is_inherited(IP_A) is True

    def test_warmup_completes_exactly_once_at_warm_until(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, inherited_hot=(IP_A, IP_B))
        clock.advance(WINDOW_SECONDS)

        assert window.finish_warmup_if_due() == frozenset({IP_A, IP_B})

        assert window.finish_warmup_if_due() is None
        assert window.in_warmup is False
        assert window.warm_until is None
        assert window.is_inherited(IP_A) is False
        assert window.is_inherited(IP_B) is False

    def test_only_inherited_ips_that_are_still_hot_are_returned(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, inherited_hot=(IP_A, IP_B))
        window.set_state(IP_B, IpState.COLD)
        clock.advance(WINDOW_SECONDS)
        assert window.finish_warmup_if_due() == frozenset({IP_A})

    def test_an_ip_this_process_promoted_is_not_inherited(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, inherited_hot=(IP_A,))
        window.observe(IP_C, BASE, 5)
        window.set_state(IP_C, IpState.HOT)
        assert window.is_inherited(IP_C) is False
        clock.advance(WINDOW_SECONDS)
        assert window.finish_warmup_if_due() == frozenset({IP_A})

    def test_demoting_an_inherited_ip_clears_its_flag(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, inherited_hot=(IP_A, IP_B))
        window.set_state(IP_A, IpState.COLD)
        assert window.is_inherited(IP_A) is False
        assert window.state(IP_A) is IpState.COLD
        assert window.is_inherited(IP_B) is True


class TestShardWindowApplyConfig:
    """Section 34 / decision 2: a geometry change re-buckets existing counts
    by `bucket_start(S, B')`; nothing else is lost."""

    def test_a_coarser_bucket_over_the_same_window_preserves_every_total(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE - 100, 4)
        window.observe(IP_A, BASE, 6)
        window.observe(IP_B, BASE - 50, 7)

        window.apply_config(_config(bucket_seconds=30))

        assert window.config.bucket_seconds == 30
        assert window.config.window_seconds == WINDOW_SECONDS
        assert window.total(IP_A) == 10
        assert window.total(IP_B) == 7
        assert window.tracked_count == 2
        assert window.active_count == 2

    def test_a_shorter_window_drops_counts_that_fall_outside_it(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE - 200, 4)  # 200 s old: outside a 120 s window
        window.observe(IP_A, BASE - 50, 6)  # 50 s old: still inside
        window.observe(IP_B, BASE - 200, 9)

        window.apply_config(_config(window_seconds=120))

        assert window.config.window_seconds == 120
        assert window.total(IP_A) == 6
        assert window.total(IP_B) == 0
        # `apply_config` rebuilds counters; removing entries is `evict_due`'s
        # job, so IP_B is still tracked, just no longer active.
        assert window.tracked_count == 2
        assert window.active_count == 1

    def test_states_survive_a_geometry_change(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        window.set_state(IP_A, IpState.HOT)
        window.apply_config(_config(bucket_seconds=30))
        assert window.state(IP_A) is IpState.HOT
        assert window.hot_ips() == frozenset({IP_A})

    def test_warm_until_is_not_recomputed(self) -> None:
        # Decision 2: "The window's `warm_until` is not recomputed."
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock, inherited_hot=(IP_A,))
        clock.advance(100)
        window.apply_config(_config(window_seconds=120))
        assert window.warm_until == BASE + WINDOW_SECONDS

    def test_a_non_geometry_change_is_adopted_without_touching_counts(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        window.apply_config(_config(hot_threshold=500, cold_threshold=400))
        assert window.config.hot_threshold == 500
        assert window.config.cold_threshold == 400
        assert window.total(IP_A) == 5
        assert window.active_count == 1


class TestShardWindowCounts:
    """Section 37's ADR-0011 note: `tracked_ips` counts every IP in the
    store, `active_ips` those with a non-zero window total, `hot_ips` those
    currently HOT."""

    def test_active_means_a_non_zero_total(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        # A zero delta is applied (decision 2), so IP_B is tracked but its
        # window total is 0 and it is therefore not active.
        window.observe(IP_B, BASE, 0)
        assert window.tracked_count == 2
        assert window.active_count == 1
        assert window.hot_count == 0

    def test_hot_count_follows_set_state(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        window.observe(IP_B, BASE, 9)
        window.set_state(IP_A, IpState.HOT)
        assert window.hot_count == 1
        assert window.hot_ips() == frozenset({IP_A})
        window.set_state(IP_A, IpState.COLD)
        assert window.hot_count == 0
        assert window.hot_ips() == frozenset()

    def test_expiry_lowers_active_but_not_tracked(self) -> None:
        clock = ManualClock(initial=BASE)
        window = _window(clock=clock)
        window.observe(IP_A, BASE, 5)
        window.observe(IP_B, BASE, 9)
        clock.advance(310)
        window.expire_due()
        assert window.active_count == 0
        assert window.tracked_count == 2
        assert window.tracked_ips() == frozenset({IP_A, IP_B})

    def test_the_counts_of_an_empty_store_are_zero(self) -> None:
        window = _window(clock=ManualClock(initial=BASE))
        assert window.tracked_count == 0
        assert window.active_count == 0
        assert window.hot_count == 0
        assert window.capacity_evictions == 0
        assert window.retention_evictions == 0
