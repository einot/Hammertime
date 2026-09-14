"""Clock, SystemClock, ManualClock (spec section 25, section 32)."""

from __future__ import annotations

import time

import pytest
from hammertime.core.time.clock import ManualClock, SystemClock


class TestSystemClock:
    def test_now_tracks_wall_clock(self) -> None:
        clock = SystemClock()
        before = int(time.time())
        now = clock.now()
        after = int(time.time())
        assert before <= now <= after

    def test_now_is_an_int(self) -> None:
        assert isinstance(SystemClock().now(), int)


class TestManualClock:
    def test_starts_at_zero_by_default(self) -> None:
        assert ManualClock().now() == 0

    def test_starts_at_given_initial_value(self) -> None:
        assert ManualClock(initial=1_700_000_000).now() == 1_700_000_000

    def test_advance_moves_time_forward(self) -> None:
        clock = ManualClock(initial=100)
        assert clock.advance(30) == 130
        assert clock.now() == 130

    def test_advance_accumulates(self) -> None:
        clock = ManualClock(initial=0)
        clock.advance(10)
        clock.advance(20)
        assert clock.now() == 30

    def test_advance_rejects_negative_seconds(self) -> None:
        clock = ManualClock(initial=100)
        with pytest.raises(ValueError):
            clock.advance(-1)
        assert clock.now() == 100  # unchanged on rejection

    def test_set_jumps_to_arbitrary_time(self) -> None:
        clock = ManualClock(initial=100)
        assert clock.set(50) == 50  # set can move backwards, advance cannot
        assert clock.now() == 50

    def test_does_not_move_on_its_own(self) -> None:
        clock = ManualClock(initial=42)
        first = clock.now()
        time.sleep(0.01)
        second = clock.now()
        assert first == second == 42
