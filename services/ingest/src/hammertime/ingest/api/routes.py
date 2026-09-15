"""POST /v1/observations, GET /healthz, GET /metrics.

Spec: section 4, section 37

No agent authentication/authorization, rate limiting, dedup, or bus
publishing here -- that is Epic #4's territory. A request that passes
validation is accepted (202, per docs/protocol/observation-v1.md's response
table) but nothing is actually published behind it yet; that is an
intentional, documented gap, not a bug.
"""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pydantic
from fastapi import APIRouter, HTTPException, Request, Response
from hammertime.core.errors import InvalidAddressError
from hammertime.core.events.models import Observation, RequestObservation
from hammertime.ingest.api.schemas import HealthStatus, ObservationAccepted, ObservationRequest
from hammertime.ingest.validation import (
    IngestValidationError,
    RequestLimitExceededError,
    SchemaValidationError,
)
from hammertime.ingest.validation.addresses import parse_address
from hammertime.ingest.validation.limits import (
    check_body_size,
    check_observation_count,
    check_window_alignment,
)

if TYPE_CHECKING:
    from hammertime.ingest.app import IngestState

router = APIRouter()


def _get_ingest_state(request: Request) -> "IngestState":
    # app.py's lifespan sets this; imported only for type-checking above to
    # avoid a routes.py <-> app.py import cycle (app.py imports `router`
    # from this module at import time).
    return request.app.state.ingest  # type: ignore[no-any-return]


def _parse_window_start(value: str) -> datetime:
    """Parse `window_start` into a UTC-aware datetime.

    schemas/observation.v1.json declares `window_start` as `format:
    date-time`, but the jsonschema library only validates `format` when the
    optional `rfc3339-validator` dependency is installed (it is not a
    declared dependency of this service), so the format is validated here
    instead, mirroring `hammertime.core.events.codec`'s tolerance for a
    trailing `Z`.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SchemaValidationError(f"window_start is not a valid date-time: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _to_request_observation(
    payload: ObservationRequest, *, bucket_seconds: int
) -> RequestObservation:
    """Convert a schema-validated wire payload into the domain type.

    Raises `SchemaValidationError` (malformed `window_start`),
    `UnalignedWindowError`, or `InvalidAddressError` (malformed `ip`) -- all
    map to 400 in `create_observation` below.
    """
    window_start = _parse_window_start(payload.window_start)
    check_window_alignment(window_start, bucket_seconds=bucket_seconds)

    observations = tuple(
        Observation(ip=parse_address(entry.ip), request_count=entry.request_count)
        for entry in payload.observations
    )
    return RequestObservation(
        agent_id=payload.agent_id,
        sequence=payload.sequence,
        window_start=window_start,
        window_seconds=payload.window_seconds,
        observations=observations,
    )


@router.post("/v1/observations", status_code=202, response_model=ObservationAccepted)
async def create_observation(request: Request) -> ObservationAccepted:
    state = _get_ingest_state(request)
    settings = state.settings

    body = await request.body()
    try:
        check_body_size(body, max_body_bytes=settings.max_body_bytes)
    except RequestLimitExceededError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"request body is not valid JSON: {exc}"
        ) from exc

    if isinstance(document, dict):
        raw_observations = document.get("observations")
        if isinstance(raw_observations, list):
            try:
                check_observation_count(
                    len(raw_observations), max_observations=settings.max_observations
                )
            except RequestLimitExceededError as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from exc

    try:
        state.schema_validator.validate(document)
        payload = ObservationRequest.model_validate(document)
    except SchemaValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except pydantic.ValidationError as exc:
        # jsonschema already accepted `document`; this is a defense-in-depth
        # backstop (e.g. non-canonical numeric encodings) rather than the
        # primary validation path.
        raise HTTPException(
            status_code=400, detail=f"observation payload is invalid: {exc}"
        ) from exc

    try:
        _to_request_observation(payload, bucket_seconds=state.bucket_seconds)
    except (IngestValidationError, InvalidAddressError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Epic #4 wires dedup and bus publishing; a validated observation is not
    # actually published anywhere yet.
    return ObservationAccepted()


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    """Liveness check. No dependencies to probe yet (no bus/store wiring)."""
    return HealthStatus()


@router.get("/metrics")
async def metrics() -> Response:
    """Placeholder: `hammertime.core.telemetry.metrics` is itself unimplemented."""
    return Response(content=b"", media_type="text/plain; version=0.0.4")
