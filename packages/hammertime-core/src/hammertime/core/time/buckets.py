"""Deterministic time buckets.

Spec: section 5 (fixed-size buckets), section 24 (bounded lateness),
section 25 (bucket timestamps).

Every agent must map an observation to the same logical bucket, so bucketing is
pure arithmetic on a UTC epoch second:

    bucket_start = floor(event_timestamp / bucket_seconds) * bucket_seconds
"""

from __future__ import annotations


def bucket_start(event_epoch_seconds: int, bucket_seconds: int) -> int:
    """Epoch second at which the containing bucket begins."""
    return (event_epoch_seconds // bucket_seconds) * bucket_seconds


def bucket_index(event_epoch_seconds: int, bucket_seconds: int, bucket_count: int) -> int:
    """Slot in the fixed-size ring buffer that holds this bucket."""
    return (event_epoch_seconds // bucket_seconds) % bucket_count


def is_within_lateness(
    event_epoch_seconds: int,
    now_epoch_seconds: int,
    window_seconds: int,
    allowed_lateness_seconds: int,
) -> bool:
    """False for observations older than the lateness horizon (spec section 24)."""
    age = now_epoch_seconds - event_epoch_seconds
    if age < 0:
        return False  # future-dated: agent clock skew, handled separately
    return age <= window_seconds + allowed_lateness_seconds
