"""Per-IP ring of buckets with a maintained running total.

Spec: section 5, section 24, section 25

ADR-0011 decision 2, as amended by Amendment 2 (items A4, A8, A9, A11).
The ring holds `bucket_count` slots of `bucket_seconds` each, so it covers
`window_seconds = bucket_seconds * bucket_count`. A bucket starting at `S`
(always a multiple of `B = bucket_seconds`, section 25) lives in slot
`bucket_index(S, B, N)` and is live at `now` iff
`0 <= bucket_start(now, B) - S < W`: it enters the ring with its first delta
and leaves the window at exactly `now = S + W`.

    expire slot:  total -= slot.count; slot.count = 0
    observe:      total += delta

`total` is therefore O(1) on read and is never recomputed by summing the ring
(spec section 5); out-of-order writes into *any* live bucket are accepted, so
replay order cannot change the result (spec section 24, ADR-0002).
"""

from hammertime.core.time import buckets


class IpCounter:
    """The section 5 ring for one IP: `bucket_count` slots and a running total."""

    __slots__ = ("_counts", "_starts", "_total", "bucket_count", "bucket_seconds")

    def __init__(self, *, bucket_seconds: int, bucket_count: int) -> None:
        if bucket_seconds <= 0:
            raise ValueError(f"bucket_seconds must be positive, got {bucket_seconds!r}")
        if bucket_count <= 0:
            raise ValueError(f"bucket_count must be positive, got {bucket_count!r}")
        self.bucket_seconds = bucket_seconds
        self.bucket_count = bucket_count
        # `_starts[i]` is the bucket the slot currently holds, or None when the
        # slot has never been written or has been expired.
        self._starts: list[int | None] = [None] * bucket_count
        self._counts: list[int] = [0] * bucket_count
        self._total = 0

    @property
    def window_seconds(self) -> int:
        return self.bucket_seconds * self.bucket_count

    @property
    def total(self) -> int:
        """The running total over every live bucket; maintained, never summed."""
        return self._total

    def is_live(self, bucket_start: int, now: int) -> bool:
        """True iff the bucket starting at `bucket_start` is in the ring at `now`.

        A bucket that has not started yet is *not* live (Amendment 2, item
        A4): the lower bound is what keeps two live buckets from sharing a
        slot, since a future bucket's slot still holds a live one.
        """
        age = buckets.bucket_start(now, self.bucket_seconds) - bucket_start
        return 0 <= age < self.window_seconds

    def observe(self, bucket_start: int, delta: int, now: int) -> bool:
        """Add `delta` to the bucket starting at `bucket_start`.

        Returns True when the delta was applied, False when the bucket is not
        live -- past or future -- in which case nothing changes. An unaligned
        bucket start or a negative delta is a `ValueError` and also changes
        nothing: the caller floors (ADR-0011 decision 3, Amendment 2 item A9).

        `delta == 0` is applied like any other delta (item A8). Because the
        slot's older occupant is subtracted first, an applied delta can *lower*
        `total` -- that is the sweep's work done early, not a negative delta
        (item A11).
        """
        if bucket_start % self.bucket_seconds:
            raise ValueError(
                f"bucket start {bucket_start} is not a multiple of "
                f"bucket_seconds {self.bucket_seconds}"
            )
        if delta < 0:
            raise ValueError(f"delta must be non-negative, got {delta}")
        if not self.is_live(bucket_start, now):
            return False
        slot = buckets.bucket_index(bucket_start, self.bucket_seconds, self.bucket_count)
        if self._starts[slot] != bucket_start:
            # The slot's previous occupant is necessarily a bucket that has
            # already left the window, so its count is no longer part of the
            # window total.
            self._total -= self._counts[slot]
            self._starts[slot] = bucket_start
            self._counts[slot] = 0
        self._counts[slot] += delta
        self._total += delta
        return True

    def expire(self, now: int) -> int:
        """Zero every slot whose bucket has left the window; return the amount removed."""
        removed = 0
        for slot in range(self.bucket_count):
            start = self._starts[slot]
            if start is None or self.is_live(start, now):
                continue
            removed += self._counts[slot]
            self._starts[slot] = None
            self._counts[slot] = 0
        self._total -= removed
        return removed

    def next_expiry(self) -> int | None:
        """`min(S) + W` over the non-zero slots, or None when the ring is empty.

        This is what lets the sweep (ADR-0011 decision 6) find the IPs that
        actually have something to expire instead of scanning the store.
        """
        oldest = min(
            (
                start
                for start, count in zip(self._starts, self._counts, strict=True)
                if start is not None and count > 0
            ),
            default=None,
        )
        return None if oldest is None else oldest + self.window_seconds

    def live_buckets(self, now: int) -> tuple[tuple[int, int], ...]:
        """The `(bucket_start, count)` pairs of non-zero live slots, oldest first."""
        return tuple(
            sorted(
                (start, count)
                for start, count in zip(self._starts, self._counts, strict=True)
                if start is not None and count > 0 and self.is_live(start, now)
            )
        )
