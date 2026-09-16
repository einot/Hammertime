"""POST /v1/observations, GET /healthz, GET /metrics.

Spec: section 4, section 36, section 36.6, section 37;
docs/protocol/observation-v1.md; ADR-0004, ADR-0008

Pipeline for `POST /v1/observations` (spec section 36.6's normative order):
agent authentication (`Depends`, runs before the body is read) -> flat
per-request rate limiting (cost 1.0, still header-only) -> body/schema/
domain validation -> coalesce -> per-agent observation-cost rate limiting
(cost = distinct IPs, ADR-0008) -> dedup claim -> drop zero-count entries,
publish, 202.
"""

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from hammertime.core.errors import InvalidAddressError
from hammertime.core.events.models import Observation, RequestObservation
from hammertime.ingest.api.schemas import (
    HealthStatus,
    ObservationAccepted,
    ObservationDuplicate,
    ObservationRequest,
)
from hammertime.ingest.publisher import RequestCountOverflowError
from hammertime.ingest.ratelimit import RateLimitExceeded
from hammertime.ingest.throttling import throttled_response
from hammertime.ingest.validation import (
    BodyTooLargeError,
    IngestValidationError,
    RequestLimitExceededError,
    SchemaValidationError,
)
from hammertime.ingest.validation.addresses import parse_address
from hammertime.ingest.validation.limits import (
    check_observation_count,
    check_window_alignment,
)

if TYPE_CHECKING:
    from hammertime.ingest.app import IngestState

router = APIRouter()

logger = logging.getLogger(__name__)


def _get_ingest_state(request: Request) -> "IngestState":
    # app.py's lifespan sets this; imported only for type-checking above to
    # avoid a routes.py <-> app.py import cycle (app.py imports `router`
    # from this module at import time).
    return request.app.state.ingest  # type: ignore[no-any-return]


async def _authenticate(request: Request) -> str:
    """Auth dependency: delegate to the `require_agent` closure built once at startup.

    `IngestState.authenticate` is `require_agent(registry)`, built exactly
    once in `app.py`'s lifespan (not rebuilt per request) -- this function
    just invokes it, mirroring `_get_ingest_state`'s "read singletons off
    `request.app.state`" convention. It raises `HTTPException(401)`/
    `HTTPException(403)` itself (see `auth/middleware.py`) and never touches
    the request body, so a FastAPI `Depends` on this runs -- and can reject
    the request -- before the body is read.

    `async def`, not a plain `def`: `require_agent`'s dependency is itself
    `async def` so FastAPI dispatches it on the event loop rather than a
    worker thread (see its docstring); this wrapper must stay `async def`
    too so FastAPI awaits it directly instead of routing it through
    `run_in_threadpool`, which would reintroduce the same concurrency
    mismatch one layer up.
    """
    return await _get_ingest_state(request).authenticate(request)


async def _read_body_within_limit(request: Request, *, max_body_bytes: int) -> bytes:
    """Read the request body without ever buffering more than `max_body_bytes`.

    `await request.body()` buffers the *entire* body into memory before
    returning, so checking its length afterwards (`check_body_size`) is too
    late to bound memory usage -- an attacker with no Content-Length
    accuracy requirement (or none at all, under chunked transfer encoding)
    can already have forced an arbitrarily large allocation by the time that
    check runs. This runs after auth and rate limiting (both header-only,
    body-free checks), but is still the line of defense against an
    oversized body from an authenticated, rate-limit-passing caller (spec
    section 36's "request size limits") and it must actually bound memory,
    not just reject after the fact.

    Two layers: a `Content-Length` pre-check rejects an obviously oversized
    request before reading anything (cheap, but the header can be absent or
    understate the true size), and a streaming read aborts the moment the
    running total exceeds the limit, which holds regardless of whether
    `Content-Length` was present, correct, or omitted (chunked encoding).
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > max_body_bytes:
            raise BodyTooLargeError(
                f"Content-Length {declared_size} exceeds the {max_body_bytes}-byte limit"
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_body_bytes:
            raise BodyTooLargeError(
                f"request body exceeds the {max_body_bytes}-byte limit while streaming"
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
async def create_observation(
    request: Request, agent_id: str = Depends(_authenticate)
) -> ObservationAccepted | JSONResponse:
    state = _get_ingest_state(request)
    settings = state.settings

    agent_record = state.registry.get(agent_id)
    effective_limit_rps = (
        agent_record.rate_limit_rps
        if agent_record is not None and agent_record.rate_limit_rps is not None
        else settings.rate_limit_rps
    )
    try:
        state.rate_limiter.check(agent_id, effective_limit_rps)
    except RateLimitExceeded as exc:
        raise throttled_response(exc, scope="requests", detail=str(exc)) from exc

    try:
        body = await _read_body_within_limit(request, max_body_bytes=settings.max_body_bytes)
    except RequestLimitExceededError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    try:
        document = json.loads(body)
    except UnicodeDecodeError as exc:
        # json.loads decodes bytes->str internally; invalid UTF-8 raises
        # UnicodeDecodeError, not JSONDecodeError, before syntax is ever
        # considered -- must not be allowed to fall through as a 500.
        # (UnicodeDecodeError is itself a ValueError subclass, so this must
        # stay ahead of the bare `except ValueError` below.)
        raise HTTPException(
            status_code=400, detail=f"request body is not valid UTF-8: {exc}"
        ) from exc
    except RecursionError as exc:
        # A pathologically nested body (a few KB of nested `[`/`{`) blows
        # json.loads's recursion limit before it ever raises
        # JSONDecodeError -- see hammertime.core.events.codec.decode, which
        # documents and guards against this same input shape.
        raise HTTPException(
            status_code=400, detail="request body is nested too deeply to parse"
        ) from exc
    except ValueError as exc:
        # Catches json.JSONDecodeError (a ValueError subclass, malformed
        # JSON) and also CPython's int-string conversion limit: a
        # many-thousand-digit integer literal (e.g. an oversized `sequence`)
        # raises a bare ValueError from *inside* json.loads, before syntax
        # is otherwise at fault -- neither of the two more specific except
        # clauses above catches it, and it must not fall through as a 500.
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

    if payload.agent_id != agent_id:
        # auth/middleware.py's integration-contract note: the authenticated
        # identity is the only agent_id ever used downstream. A body that
        # claims to be a *different* agent than the one that authenticated
        # the request must be rejected outright, not silently trusted --
        # otherwise an authenticated agent could desync another agent's
        # dedup/rate-limit state.
        raise HTTPException(
            status_code=403,
            detail=(
                f"request body agent_id {payload.agent_id!r} does not match the "
                f"authenticated agent {agent_id!r}"
            ),
        )

    try:
        observation = _to_request_observation(payload, bucket_seconds=state.bucket_seconds)
    except (IngestValidationError, InvalidAddressError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        coalesced = state.publisher.coalesce(observation)
    except RequestCountOverflowError as exc:
        # Validated (and rejected, if it overflows) before the dedup claim
        # below, so an overflowing batch never consumes the sequence's
        # claim -- a corrected retry with the same sequence must still be
        # accepted, not told "duplicate" (ADR-0004 section 3/5).
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ADR-0008 / spec section 36.6: the observation budget is charged per
    # distinct IP -- exactly `len(coalesced)`, counted before decision 5's
    # zero-count drop (publisher.py's `publish` does that drop; this charges
    # the full coalesced count regardless). `load_settings` guarantees
    # observation_burst >= max_observations, so a schema-valid batch can
    # never cost more than the bucket can ever hold -- this comparison is
    # defense-in-depth, and it runs *before* `check()` so an over-capacity
    # cost is never passed to the limiter (which would raise `ValueError`).
    cost = len(coalesced)
    if cost > settings.observation_burst:
        raise HTTPException(
            status_code=413,
            detail=(
                f"batch cost {cost} (distinct IPs) exceeds the observation "
                f"capacity of {settings.observation_burst}"
            ),
        )
    try:
        state.observation_limiter.check(
            agent_id,
            settings.observation_rate_limit_eps,
            cost=cost,
            capacity=settings.observation_burst,
        )
    except RateLimitExceeded as exc:
        raise throttled_response(exc, scope="observations", detail=str(exc)) from exc

    claimed = await state.dedup.claim(
        agent_id, observation.sequence, window_seconds=observation.window_seconds
    )
    if not claimed:
        # docs/protocol/observation-v1.md: 200, previously accepted, no
        # action taken. Returned via a plain JSONResponse rather than the
        # route's declared 202 response_model, so the status code and body
        # shape can both differ from the accepted case.
        return JSONResponse(status_code=200, content=ObservationDuplicate().model_dump())

    try:
        await state.publisher.publish(observation)
    except Exception as exc:
        # ADR-0004 section 5: the claim above already marks this sequence
        # seen (for allowed_lateness_seconds + window_seconds) *before*
        # publishing -- the only way to close the race where two concurrent
        # requests for the same sequence both see "not a duplicate" and
        # both publish. The cost: unlike a strict publish-then-mark order,
        # a publish failure here leaves the sequence claimed rather than
        # immediately retryable, so a retry of this exact sequence is
        # rejected as a duplicate until the claim's TTL lapses rather than
        # being reprocessed right away -- a bounded delay, not permanently
        # lost data (mark_seen's contract is "seen for at least
        # ttl_seconds", never permanent). The real exception is logged
        # server-side (it may name internal bus/broker details that must
        # not reach the caller); the agent gets a generic, retry-safe 503.
        logger.exception(
            "failed to publish observation for agent_id=%r sequence=%r",
            agent_id,
            observation.sequence,
        )
        raise HTTPException(
            status_code=503, detail="failed to publish observation; retry is safe"
        ) from exc

    return ObservationAccepted()


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    """Liveness check. No dependencies to probe yet (no bus/store wiring)."""
    return HealthStatus()


@router.get("/metrics")
async def metrics() -> Response:
    """Placeholder: `hammertime.core.telemetry.metrics` is itself unimplemented."""
    return Response(content=b"", media_type="text/plain; version=0.0.4")
