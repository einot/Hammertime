"""Per-IP ring of buckets with a maintained running total.

Spec: section 5, section 25

window = 300s, bucket = 10s -> 30 slots.
  expire slot:  total -= slot.count; slot.count = 0
  observe:      total += delta
window_count(ip) is O(1) after maintenance; never sum the ring on read
(spec section 5).

A live bucket `S` and the expired bucket `S - window_seconds` share a ring
slot exactly when the former replaces the latter, which is why the ring is
indexed by `bucket_index` (ADR-0011 decision 1).
"""

from collections.abc import Iterable

from hammertime.aggregator.lateness import is_live
from hammertime.core.time.buckets import bucket_index, bucket_start


def validate_geometry(window_seconds: int, bucket_seconds: int) -> None:
    """Spec section 5: `bucket_count = window / bucket`, exactly.

    A partial bucket has no defined edge, so a window that is not a whole
    multiple of the bucket size is refused rather than rounded.
    """
    if window_seconds <= 0:
        raise ValueError(f"window_seconds must be positive, got {window_seconds}")
    if bucket_seconds <= 0:
        raise ValueError(f"bucket_seconds must be positive, got {bucket_seconds}")
    if window_seconds % bucket_seconds:
        raise ValueError(
            f"window_seconds ({window_seconds}) must be a whole multiple of "
            f"bucket_seconds ({bucket_seconds})"
        )


class IpCounter:
    """Fixed-size bucket ring for one IP, with an incrementally kept total."""

    __slots__ = ("_bucket_seconds", "_counts", "_starts", "_total", "_window_seconds")

    def __init__(self, *, window_seconds: int, bucket_seconds: int) -> None:
        validate_geometry(window_seconds, bucket_seconds)
        self._window_seconds = window_seconds
        self._bucket_seconds = bucket_seconds
        slots = window_seconds // bucket_seconds
        self._starts: list[int | None] = [None] * slots
        self._counts: list[int] = [0] * slots
        self._total = 0

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    @property
    def bucket_seconds(self) -> int:
        return self._bucket_seconds

    @property
    def bucket_count(self) -> int:
        return len(self._counts)

    @property
    def total(self) -> int:
        """The window count, maintained on every write (spec section 5: O(1))."""
        return self._total

    def _slot(self, start: int) -> int:
        return bucket_index(start, self._bucket_seconds, len(self._counts))

    def observe(self, bucket_start: int, delta: int, *, now: int) -> int:
        """Add `delta` to the live bucket starting at `bucket_start`.

        Expiry runs first so the returned total is correct at the moment a
        state decision is taken (ADR-0011 decision 1). `bucket_start` must
        already be aligned -- rounding is the caller's step (spec section 25).
        """
        self.expire(now)
        if delta < 1:
            raise ValueError(f"delta must be at least 1, got {delta}")
        if bucket_start % self._bucket_seconds:
            raise ValueError(
                f"bucket_start ({bucket_start}) must be a multiple of "
                f"bucket_seconds ({self._bucket_seconds})"
            )
        if not is_live(bucket_start, now, self._window_seconds):
            raise ValueError(
                f"bucket {bucket_start} is not live at {now} "
                f"(window_seconds={self._window_seconds})"
            )
        index = self._slot(bucket_start)
        if self._starts[index] != bucket_start:
            # Every retained bucket is live after `expire`, and at most
            # `bucket_count` buckets are live at once, so the slot for a live
            # bucket can only be empty or already hold that same bucket.
            self._starts[index] = bucket_start
            self._counts[index] = 0
        self._counts[index] += delta
        self._total += delta
        return self._total

    def expire(self, now: int) -> int:
        """Drop every bucket that has left the window; return the count removed."""
        removed = 0
        for index, start in enumerate(self._starts):
            if start is None or is_live(start, now, self._window_seconds):
                continue
            removed += self._counts[index]
            self._starts[index] = None
            self._counts[index] = 0
        self._total -= removed
        return removed

    def buckets(self) -> tuple[tuple[int, int], ...]:
        """Retained `(bucket_start, count)` pairs with `count > 0`, ascending.

        A snapshot as of the last `observe`/`expire` call: reading does not
        itself advance the window.
        """
        return tuple(
            sorted(
                (start, count)
                for start, count in zip(self._starts, self._counts, strict=True)
                if start is not None and count > 0
            )
        )

    @classmethod
    def rebuild(
        cls,
        buckets: Iterable[tuple[int, int]],
        *,
        window_seconds: int,
        bucket_seconds: int,
        now: int,
    ) -> "IpCounter":
        """Re-place existing counts under a new geometry (ADR-0011 decision 7).

        Each pair is re-placed at `bucket_start(S, bucket_seconds)` -- the same
        one-bucket rule observations follow (ADR-0010 decision 6) -- and kept
        only if that bucket is live at `now`. Counts are neither invented nor
        scaled; pairs landing in the same new bucket are added together.
        """
        counter = cls(window_seconds=window_seconds, bucket_seconds=bucket_seconds)
        for start, count in buckets:
            placed = bucket_start(start, bucket_seconds)
            if not is_live(placed, now, window_seconds):
                continue
            index = counter._slot(placed)
            if counter._starts[index] != placed:
                counter._starts[index] = placed
                counter._counts[index] = 0
            counter._counts[index] += count
            counter._total += count
        return counter
