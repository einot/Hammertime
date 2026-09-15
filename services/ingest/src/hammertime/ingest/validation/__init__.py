"""Payload validation (spec sections 4, 36)."""

from hammertime.core.errors import HammertimeError


class IngestValidationError(HammertimeError):
    """Base class for ingest request-validation failures (spec section 36)."""


class SchemaValidationError(IngestValidationError):
    """Request body does not conform to schemas/observation.v1.json (spec section 36).

    Covers malformed JSON, unknown/missing fields (including a client-asserted
    `"state"` key -- see docs/protocol/observation-v1.md), wrong types, and
    values outside the schema's declared ranges.
    """


class UnalignedWindowError(IngestValidationError):
    """window_start is not aligned to the configured bucket_seconds.

    docs/protocol/observation-v1.md: "window_start MUST be aligned to
    bucket_seconds; ingest rejects unaligned windows rather than silently
    re-bucketing."
    """


class RequestLimitExceededError(IngestValidationError):
    """A configured request-size limit was exceeded (spec section 36).

    Maps to HTTP 413, distinct from the 400s raised by the other validation
    errors in this module.
    """


class BodyTooLargeError(RequestLimitExceededError):
    """Request body exceeds HAMMERTIME_INGEST_MAX_BODY_BYTES (spec section 36)."""


class TooManyObservationsError(RequestLimitExceededError):
    """observations array exceeds HAMMERTIME_INGEST_MAX_OBSERVATIONS (spec section 36)."""
