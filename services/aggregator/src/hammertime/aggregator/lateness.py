"""Bounded lateness policy for out-of-order observations.

Spec: section 24, section 25

Windows are event-time (ADR 0002). Observations inside the horizon land in
their own bucket even if it is not the newest; observations outside it are
counted as late_messages and routed to reconciliation, never silently dropped.

ADR-0011 decision 1 fixes the liveness edge (`S <= now < S + window_seconds`,
judged against the service clock) and decision 2 the disposition table: an
observation is either applied to the window or published to reconciliation,
never both and never neither.
"""

import math
from datetime import UTC, datetime
from enum import StrEnum

from hammertime.core.config.models import DetectionConfig
from hammertime.core.time.buckets import bucket_start, is_within_lateness


class Disposition(StrEnum):
    """What the aggregator does with a consumed observation (ADR-0011 decision 2)."""

    APPLY = "apply"
    EXPIRED = "expired"
    LATE = "late"
    FUTURE = "future"


def window_start_epoch(window_start: datetime) -> int:
    """UTC epoch second of an observation's `window_start`.

    Spec section 25 keeps every timestamp in UTC; a naive datetime is read as
    UTC rather than as local time, so the aggregator's arithmetic never
    depends on the host's timezone.
    """
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=UTC)
    return math.floor(window_start.timestamp())


def is_live(bucket_start: int, now: int, window_seconds: int) -> bool:
    """True while the bucket starting at `bucket_start` is inside the window.

    ADR-0011 decision 1: the lower edge is inclusive and the upper edge is
    exclusive, so a bucket expires at exactly `bucket_start + window_seconds`
    and the still-filling bucket containing `now` is always live.
    """
    return bucket_start <= now < bucket_start + window_seconds


def classify(window_start: int, now: int, config: DetectionConfig) -> Disposition:
    """Decide what happens to an observation for `window_start` seen at `now`.

    `FUTURE` is checked before `LATE` only so the metric names the cause;
    `is_within_lateness` already rejects a negative age. `EXPIRED` versus
    `APPLY` is judged on the bucket `S = bucket_start(window_start, B)`, never
    on the raw age of `window_start` (ADR-0011 decision 2): the caller applies
    an `APPLY` observation with `IpCounter.observe(S, ...)`, which refuses a
    dead `S`, so an age-based reading would turn a valid observation into a
    crash on the per-observation path.
    """
    if window_start > now:
        return Disposition.FUTURE
    if not is_within_lateness(
        window_start, now, config.window_seconds, config.allowed_lateness_seconds
    ):
        return Disposition.LATE
    start = bucket_start(window_start, config.bucket_seconds)
    if is_live(start, now, config.window_seconds):
        return Disposition.APPLY
    return Disposition.EXPIRED
