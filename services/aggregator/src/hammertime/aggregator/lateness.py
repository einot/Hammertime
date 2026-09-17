"""Bounded lateness policy for out-of-order observations.

Spec: section 5, section 24, section 25

ADR-0011 decision 3 (amended by Amendment 2, items A9 and A10); ADR-0002
(event time, `allowed_lateness`); ADR-0010 decision 6 (a message's
`window_seconds` is descriptive and is only used to reject an over-long
window).

Windows are event-time (ADR-0002). Observations inside the horizon land in
their own bucket even if it is not the newest; observations outside it are
counted as late_messages and routed to reconciliation, never silently
dropped.
"""

from enum import StrEnum

from hammertime.core.config.models import DetectionConfig
from hammertime.core.time.buckets import bucket_start


class ObservationOutcome(StrEnum):
    """What the hot path did with one observation; the value is the metric label."""

    APPLIED = "applied"
    #: now - window_start > window_seconds + allowed_lateness_seconds.
    LATE = "late"
    #: window_start > now: the agent's clock is ahead of the service.
    FUTURE = "future"
    #: Inside the horizon, but the bucket has already left the window.
    EXPIRED_BUCKET = "expired_bucket"
    #: payload.window_seconds > config.window_seconds (ADR-0010 decision 6).
    WINDOW_TOO_LONG = "window_too_long"
    #: A worker outcome (codec failure, ADR-0004's one-IP-per-message
    #: invariant); never returned by `classify_observation`.
    MALFORMED = "malformed"


def classify_observation(
    *, window_start: int, window_seconds: int, now: int, config: DetectionConfig
) -> ObservationOutcome:
    """Classify one observation against the config in force; pure.

    The checks run in this order: `WINDOW_TOO_LONG`, `FUTURE`, `LATE`,
    `EXPIRED_BUCKET`, else `APPLIED`.

    `now` is the service clock at processing time, not the envelope
    timestamp, so a lagging aggregator diverts what it can no longer count
    rather than counting it into the past.

    `LATE` and `FUTURE` are judged on the raw age; `EXPIRED_BUCKET` is judged
    on the *bucket* age, with both ends floored to `config.bucket_seconds`
    (section 25). The two measures differ inside the last bucket: with the
    shipped defaults and a bucket-aligned `now`, an age of 291 s already
    floors into the bucket 300 s back and is `EXPIRED_BUCKET`, while an
    aligned `window_start` 290 s back is `APPLIED` (item A9). An age of
    exactly `window_seconds + allowed_lateness_seconds` is inside the horizon
    -- the `LATE` test is strictly `>` -- and always falls out as
    `EXPIRED_BUCKET` (item A10).

    `window_start` is floored here and is never required to be aligned: an
    unaligned value is not `MALFORMED`, because ADR-0010 decision 6 already
    lands the delta in `bucket_start(window_start, B)`.
    """
    if window_seconds > config.window_seconds:
        return ObservationOutcome.WINDOW_TOO_LONG
    if window_start > now:
        return ObservationOutcome.FUTURE
    if now - window_start > config.window_seconds + config.allowed_lateness_seconds:
        return ObservationOutcome.LATE
    bucket_seconds = config.bucket_seconds
    bucket_age = bucket_start(now, bucket_seconds) - bucket_start(window_start, bucket_seconds)
    if bucket_age >= config.window_seconds:
        return ObservationOutcome.EXPIRED_BUCKET
    return ObservationOutcome.APPLIED
