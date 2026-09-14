"""Injectable clock so windows, expiry, and replay are testable and deterministic.

Spec: section 25, section 32
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Anything that can report the current time as a UTC epoch second.

    Every place that needs "now" (window expiry, lateness checks, snapshot
    timestamps) takes a `Clock` instead of calling `time.time()` directly, so
    tests can control time exactly rather than sleeping or racing the wall
    clock (spec section 25). Replay (spec section 32) uses the same
    protocol, driven by event timestamps instead of wall-clock time.
    """

    def now(self) -> int:
        """Current time as a UTC epoch second."""
        ...


class SystemClock:
    """Wall-clock time, for production use."""

    def now(self) -> int:
        return int(time.time())


class ManualClock:
    """A clock a test fully controls.

    Time only moves when `advance`/`set` is called, never on its own, so
    bucket/lateness/expiry logic can be tested against exact, reproducible
    timestamps instead of the real wall clock.
    """

    def __init__(self, initial: int = 0) -> None:
        self._now = initial

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> int:
        """Move the clock forward and return the new time."""
        if seconds < 0:
            raise ValueError("ManualClock cannot move backwards; use set() instead")
        self._now += seconds
        return self._now

    def set(self, epoch_seconds: int) -> int:
        """Jump the clock to an arbitrary time and return it."""
        self._now = epoch_seconds
        return self._now
