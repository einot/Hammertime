"""Bucket arithmetic and lateness (spec section 5, section 24, section 25)."""

from __future__ import annotations

import pytest
from hammertime.core.time.buckets import bucket_index, bucket_start, is_within_lateness


class TestBucketStart:
    def test_exact_boundary(self) -> None:
        assert bucket_start(100, 10) == 100

    def test_rounds_down_to_bucket_boundary(self) -> None:
        assert bucket_start(107, 10) == 100

    def test_zero_epoch(self) -> None:
        assert bucket_start(0, 10) == 0

    @pytest.mark.parametrize("bucket_seconds", [1, 5, 10, 60, 300])
    def test_result_is_always_a_multiple_of_bucket_seconds(self, bucket_seconds: int) -> None:
        for ts in range(0, 1000, 7):
            assert bucket_start(ts, bucket_seconds) % bucket_seconds == 0


class TestBucketIndex:
    def test_wraps_around_ring_buffer(self) -> None:
        # window=300s, bucket=10s -> 30 slots; bucket 30 wraps to slot 0.
        assert bucket_index(0, 10, 30) == 0
        assert bucket_index(300, 10, 30) == 0
        assert bucket_index(299, 10, 30) == 29

    def test_adjacent_buckets_get_adjacent_slots(self) -> None:
        assert bucket_index(10, 10, 30) == 1
        assert bucket_index(20, 10, 30) == 2


class TestIsWithinLateness:
    def test_current_event_is_within_lateness(self) -> None:
        assert is_within_lateness(
            event_epoch_seconds=100,
            now_epoch_seconds=100,
            window_seconds=300,
            allowed_lateness_seconds=30,
        )

    def test_event_exactly_at_the_horizon_is_allowed(self) -> None:
        # age == window + lateness is inclusive ("<=").
        assert is_within_lateness(
            event_epoch_seconds=0,
            now_epoch_seconds=330,
            window_seconds=300,
            allowed_lateness_seconds=30,
        )

    def test_event_one_second_past_the_horizon_is_rejected(self) -> None:
        assert not is_within_lateness(
            event_epoch_seconds=0,
            now_epoch_seconds=331,
            window_seconds=300,
            allowed_lateness_seconds=30,
        )

    def test_future_dated_event_is_rejected(self) -> None:
        assert not is_within_lateness(
            event_epoch_seconds=200,
            now_epoch_seconds=100,
            window_seconds=300,
            allowed_lateness_seconds=30,
        )

    def test_zero_lateness_still_allows_events_within_the_window(self) -> None:
        assert is_within_lateness(
            event_epoch_seconds=0,
            now_epoch_seconds=300,
            window_seconds=300,
            allowed_lateness_seconds=0,
        )
