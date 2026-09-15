"""Body size, observation-array length, and window_start alignment.

Spec: section 36 (request size limits); docs/protocol/observation-v1.md
(window_start alignment to bucket_seconds).
"""

from datetime import datetime

from hammertime.ingest.validation import (
    BodyTooLargeError,
    TooManyObservationsError,
    UnalignedWindowError,
)


def check_body_size(body: bytes, *, max_body_bytes: int) -> None:
    """Reject a request body larger than HAMMERTIME_INGEST_MAX_BODY_BYTES."""
    if len(body) > max_body_bytes:
        raise BodyTooLargeError(
            f"request body is {len(body)} bytes, exceeding the {max_body_bytes}-byte limit"
        )


def check_observation_count(count: int, *, max_observations: int) -> None:
    """Reject an observations array longer than HAMMERTIME_INGEST_MAX_OBSERVATIONS.

    This is checked ahead of full schema validation so an oversized array
    surfaces as 413 (limit exceeded) rather than the 400 that
    schemas/observation.v1.json's own `maxItems` would otherwise produce;
    the two limits are kept in sync (both default to 10000 -- see
    `.env.example` and `hammertime.core.events.codec._MAX_OBSERVATIONS`).
    """
    if count > max_observations:
        raise TooManyObservationsError(
            f"observations array has {count} entries, exceeding the {max_observations}-entry limit"
        )


def check_window_alignment(window_start: datetime, *, bucket_seconds: int) -> None:
    """window_start MUST be aligned to bucket_seconds.

    docs/protocol/observation-v1.md: "window_start MUST be aligned to
    bucket_seconds; ingest rejects unaligned windows rather than silently
    re-bucketing."
    """
    epoch_seconds = window_start.timestamp()
    if epoch_seconds != int(epoch_seconds) or int(epoch_seconds) % bucket_seconds != 0:
        raise UnalignedWindowError(
            f"window_start {window_start.isoformat()!r} is not aligned to "
            f"bucket_seconds={bucket_seconds}"
        )
