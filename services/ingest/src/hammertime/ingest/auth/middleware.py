"""Bearer-token agent authentication for the ingest API.

Spec: section 36, section 36.5, section 23; ADR-0007

Bearer token, not mTLS: this repo has no certificate provisioning or mTLS
termination configured anywhere (no PKI, nothing in `deploy/`), so this
module implements `Authorization: Bearer <token>` verification against
`hammertime.ingest.auth.agents.AgentRegistry` as the ingest API's only
authentication mechanism for now. mTLS would need infrastructure this repo
does not have yet; building it here would be unfounded scope creep for this
issue.

`docs/protocol/observation-v1.md`'s response table and spec section 36.5
collapse the previous 401/403 split: every authentication failure -- a
missing/malformed header, an over-long `X-Agent-Id` or bearer token, an
unknown `agent_id`, or a known `agent_id` with the wrong token -- answers
the identical `401` with `WWW-Authenticate: Bearer` and body
`{"detail": "invalid agent credentials"}`. `403` is reserved for exactly
one outcome: a *correct* credential for a registered but disabled agent,
which discloses nothing to an anonymous caller since it requires already
holding that agent's live token. `hammertime.ingest.auth`'s exception
hierarchy still distinguishes every failure mode internally (that is what
the log line's `outcome` field is derived from); only the HTTP status this
module picks has collapsed.

Failed-authentication throttling (ADR-0007, spec section 36.5) lives here
rather than in `api/routes.py`, because it must cover every failure mode of
`authenticate_request`, including the header-shape failures that never
reach `AgentRegistry`. `require_agent` charges two `RateLimiter` instances
(built once in `app.py`'s lifespan and passed in here) on every
authentication *failure*, source bucket first: if the source bucket is
already exhausted the agent bucket is left untouched and the request is
answered `429` immediately. A successful authentication never touches
either bucket. Per ADR-0007 Decision 8, the agent bucket is never keyed on
the raw attempted `X-Agent-Id` -- `_agent_bucket_key` maps it into a
fixed-size salted slot table first, so an attacker cannot manufacture
distinct `agent_id` values to evict and reset a targeted identity's budget.

Message-identity replay protection (spec section 23: `(agent_id, sequence)`
dedup) is *not* implemented here -- that is `hammertime.ingest.dedup`'s
job, layered on top of a request this module has already authenticated.

Integration contract for `api/routes.py`: the `agent_id` this dependency
returns -- the header-authenticated identity -- MUST be the only `agent_id`
used for every downstream decision (rate limiting, dedup, published event
attribution). If the request body also carries an `agent_id` field (per
`schemas/observation.v1.json`), `routes.py` MUST reject the request if it
disagrees with the authenticated identity rather than trusting the body's
value -- otherwise an authenticated agent could submit a body claiming to
be a *different* agent and desync that agent's dedup/rate-limit state.
"""

import hashlib
import hmac
import ipaddress
import logging
import secrets
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request
from hammertime.ingest.auth import (
    AgentAuthError,
    AgentDisabledError,
    InvalidCredentialError,
    MissingCredentialError,
    OversizedCredentialError,
    UnknownAgentError,
)
from hammertime.ingest.auth.agents import AgentRegistry
from hammertime.ingest.ratelimit import RateLimiter, RateLimitExceeded
from hammertime.ingest.throttling import throttled_response

logger = logging.getLogger("hammertime.ingest.auth")

_AUTHORIZATION_HEADER = "authorization"
_AGENT_ID_HEADER = "x-agent-id"
_FORWARDED_FOR_HEADER = "x-forwarded-for"
_BEARER_PREFIX = "Bearer "

#: schemas/observation.v1.json: agent_id maxLength (spec section 36.5).
_MAX_AGENT_ID_LENGTH = 128
#: spec section 36.5: bearer token length bound, evaluated before hashing.
_MAX_TOKEN_LENGTH = 512

#: agent bucket key when no usable X-Agent-Id was presented (spec section 36.5).
_NO_AGENT_ID_KEY = "-"

#: ADR-0007 Decision 8 / spec section 36.5: fixed size of the salted slot
#: table every presented X-Agent-Id (other than the "-" sentinel above) is
#: mapped into before it reaches the agent limiter. Not operator-configurable
#: -- see `require_agent`'s `agent_slot_count` parameter for the test-only
#: escape hatch.
_AGENT_SLOT_COUNT = 4096
_AGENT_SLOT_KEY_PREFIX = "agent-slot:"

_UNIFORM_AUTH_FAILURE_DETAIL = "invalid agent credentials"
_AGENT_DISABLED_DETAIL = "agent is disabled"
_THROTTLED_DETAIL = "too many failed authentication attempts"

#: Mirror `hammertime.ingest.config`'s defaults (`.env.example`, spec
#: section 36.5) so a caller that builds this dependency directly -- a test,
#: or any future entry point besides `app.py`'s lifespan -- gets the same
#: defaults a real deployment would rather than an unhelpful `TypeError`.
#: `app.py` always passes every one of these explicitly from `IngestSettings`
#: regardless, so production behaviour is never affected by these values.
_DEFAULT_AUTH_FAILURE_RATE_PER_MIN = 30.0
_DEFAULT_AUTH_FAILURE_BURST = 10
_DEFAULT_AUTH_FAILURE_AGENT_RATE_PER_MIN = 60.0
_DEFAULT_AUTH_FAILURE_AGENT_BURST = 20

#: Exception type -> spec section 36.5 logging `outcome`.
_OUTCOME_BY_EXCEPTION: dict[type[AgentAuthError], str] = {
    MissingCredentialError: "missing_credential",
    OversizedCredentialError: "oversized_credential",
    UnknownAgentError: "unknown_agent",
    InvalidCredentialError: "invalid_credential",
    AgentDisabledError: "agent_disabled",
}


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get(_AUTHORIZATION_HEADER)
    if header is None or not header.startswith(_BEARER_PREFIX):
        raise MissingCredentialError("missing or malformed Authorization: Bearer header")
    token = header[len(_BEARER_PREFIX) :]
    if not token:
        raise MissingCredentialError("empty bearer token")
    if len(token) > _MAX_TOKEN_LENGTH:
        raise OversizedCredentialError(f"bearer token exceeds {_MAX_TOKEN_LENGTH} characters")
    return token


def authenticate_request(request: Request, *, registry: AgentRegistry) -> str:
    """Authenticate one HTTP request and return the caller's `agent_id`.

    Reads the agent's identity from `X-Agent-Id` and its credential from
    `Authorization: Bearer <token>`, and raises `hammertime.ingest.auth`
    exceptions rather than `HTTPException` so callers can pick their own
    status-code mapping; `require_agent` below is the FastAPI-flavoured
    wrapper that does that mapping (and the throttling/logging around it)
    per docs/protocol/observation-v1.md and spec section 36.5.

    Both length bounds are checked before `AgentRegistry.authenticate` is
    ever called, so an over-long value never reaches any hashing/comparison
    work (spec section 36.5).
    """
    agent_id = request.headers.get(_AGENT_ID_HEADER)
    if not agent_id:
        raise MissingCredentialError("missing X-Agent-Id header")
    if len(agent_id) > _MAX_AGENT_ID_LENGTH:
        raise OversizedCredentialError(f"X-Agent-Id exceeds {_MAX_AGENT_ID_LENGTH} characters")
    token = _extract_bearer_token(request)
    record = registry.authenticate(agent_id, token)
    return record.agent_id


def _attempted_agent_id(request: Request) -> str | None:
    """The raw `X-Agent-Id` header value, for logging/bucket-keying only.

    Not validated or authenticated -- `authenticate_request` above is the
    source of truth for whether this identity is real.
    """
    return request.headers.get(_AGENT_ID_HEADER) or None


def _agent_bucket_key(attempted_agent_id: str | None, *, slot_salt: bytes, slot_count: int) -> str:
    """spec section 36.5 / ADR-0007 Decision 8: agent bucket key.

    The agent limiter must never see an attacker-chosen key: Decision 3's
    assumption that `RateLimiter`'s `max_keys` LRU eviction made a
    caller-controlled key safe was wrong (an evicted bucket is recreated
    full, so manufacturing enough distinct `X-Agent-Id` values resets a
    targeted identity's budget on demand). Every presented `X-Agent-Id` is
    therefore mapped into a fixed `slot_count`-sized table via a keyed hash
    under a salt drawn once per process, so the agent limiter's key space is
    exactly `slot_count + 1` regardless of the input stream.

    An absent, empty, or over-long `X-Agent-Id` still returns the shared
    `"-"` sentinel, *outside* the slot table -- unchanged semantics from
    before Decision 8, and for the same reason: an over-long value can never
    be a real `agent_id` (`schemas/observation.v1.json`'s `maxLength`), and
    the standing flood of missing-header failures must not be able to
    permanently drain some arbitrary real agent's slot.

    `.encode("utf-8")` cannot fail here: Starlette decodes header bytes as
    latin-1, so every value is a string of codepoints <= 0xFF with no
    surrogates.
    """
    if not attempted_agent_id or len(attempted_agent_id) > _MAX_AGENT_ID_LENGTH:
        return _NO_AGENT_ID_KEY
    digest = hmac.new(slot_salt, attempted_agent_id.encode("utf-8"), hashlib.sha256).digest()
    slot = int.from_bytes(digest[:8], "big") % slot_count
    return f"{_AGENT_SLOT_KEY_PREFIX}{slot}"


def _log_agent_id(attempted_agent_id: str | None) -> str | None:
    """spec section 36.5: log field is the attempted identity, truncated to 128."""
    if attempted_agent_id is None:
        return None
    return attempted_agent_id[:_MAX_AGENT_ID_LENGTH]


def _resolve_source_address(request: Request, *, trusted_proxy_hops: int) -> str:
    """The client address to key the source bucket on (spec section 36.5).

    Defaults to the socket peer address. `X-Forwarded-For` is consulted
    only when `trusted_proxy_hops > 0`, taking the entry that many
    positions from the right; an absent, shorter, or unparsable header
    falls back to the socket peer rather than to a caller-supplied value,
    since an untrusted `X-Forwarded-For` is a limiter with an infinite key
    space.

    Uses `Headers.getlist` rather than `Headers.get`: Starlette's `.get`
    returns only the first raw `X-Forwarded-For` header *line*, silently
    ignoring any duplicates. A trusted proxy that emits `X-Forwarded-For`
    as its own separate header line (rather than appending to the client's
    existing one -- common with HAProxy's `option forwardfor`, and various
    CDNs/L7 gateways) would then have an attacker-forged first line read
    instead of the proxy's real one, letting the source key be rotated at
    will even with `trusted_proxy_hops` correctly configured. Joining every
    line with `", "` before splitting matches RFC 9110's semantics for
    repeated header fields (the same join uvicorn's own
    `ProxyHeadersMiddleware` performs internally).
    """
    if trusted_proxy_hops > 0:
        header = ", ".join(request.headers.getlist(_FORWARDED_FOR_HEADER))
        if header:
            entries = [entry.strip() for entry in header.split(",") if entry.strip()]
            if len(entries) >= trusted_proxy_hops:
                candidate = entries[-trusted_proxy_hops]
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    pass
                else:
                    return candidate

    client = request.client
    if client is None:
        # No socket peer at all (e.g. a transport without one) -- a fixed
        # placeholder is still a valid, bounded bucket key.
        return "unknown"
    return client.host


def _source_bucket_key(address_text: str) -> str:
    """spec section 36.5: IPv4 /32, IPv6 /64 -- a single allocation routinely
    carries 2^64 addresses, so per-address IPv6 keying is free evasion.

    An IPv4-mapped IPv6 address (`::ffff:a.b.c.d`) is unwrapped to its
    embedded `IPv4Address` and keyed with the /32 rule, not /64: it is an
    `IPv6Address` instance as far as `ipaddress` is concerned, but
    semantically an IPv4 client (arriving over a dual-stack socket, or via
    a proxy that forwards `X-Forwarded-For: ::ffff:...`). Applying the /64
    rule to it would collapse *every* IPv4-mapped client into the single
    shared key `::/64`, letting one attacker drain the bucket and
    deny/throttle every other IPv4 client's failed-auth attempts.
    """
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        # Not a parsable address at all (e.g. the "unknown" placeholder
        # above) -- key on the literal text, still a single fixed bucket.
        return address_text
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return f"{mapped}/32"
        network = ipaddress.IPv6Network((address, 64), strict=False)
        return f"{network.network_address}/64"
    return f"{address}/32"


def require_agent(
    registry: AgentRegistry,
    *,
    source_limiter: RateLimiter | None = None,
    agent_limiter: RateLimiter | None = None,
    auth_failure_rate_per_min: float = _DEFAULT_AUTH_FAILURE_RATE_PER_MIN,
    auth_failure_burst: int = _DEFAULT_AUTH_FAILURE_BURST,
    auth_failure_agent_rate_per_min: float = _DEFAULT_AUTH_FAILURE_AGENT_RATE_PER_MIN,
    auth_failure_agent_burst: int = _DEFAULT_AUTH_FAILURE_AGENT_BURST,
    trusted_proxy_hops: int = 0,
    agent_slot_salt: bytes | None = None,
    agent_slot_count: int = _AGENT_SLOT_COUNT,
) -> Callable[[Request], Awaitable[str]]:
    """Build a FastAPI dependency that authenticates the calling agent.

    `source_limiter`/`agent_limiter` default to a fresh `RateLimiter()` each
    when omitted (mirroring `create_app`'s "`None` falls back to a fresh
    instance" convention) -- `app.py`'s lifespan always passes both
    explicitly, built once and shared with `IngestState`, so this default
    only matters to a caller that builds this dependency directly.

    `agent_slot_salt`/`agent_slot_count` (ADR-0007 Decision 8) control the
    salted-slot mapping `_agent_bucket_key` applies to every attempted
    `X-Agent-Id` before it reaches `agent_limiter`. When `agent_slot_salt` is
    `None`, a fresh 32-byte salt is drawn via `secrets.token_bytes(32)`
    exactly once, here at dependency-construction time -- never per request,
    which would make every attempt land in an independently-random slot and
    defeat the whole point of a stable per-identity budget. `app.py` passes
    neither: both parameters exist purely so tests can pin the attempted-id
    -> slot mapping deterministically. The salt MUST NOT appear in any
    response, log, or metric, and is never stored anywhere but this
    closure's scope. `agent_slot_count` MUST be positive; a non-positive
    value raises `ValueError` immediately rather than at first use.

    This is an `async def` dependency (not a plain `def`) so FastAPI/
    Starlette dispatches it directly on the single asyncio event-loop thread
    like every other request-handling code path in this service, rather
    than via `run_in_threadpool` on a worker thread. `RateLimiter.check()`
    (see `ratelimit/__init__.py`) is race-free only under that single-
    event-loop-thread model -- its bucket read-modify-write is not atomic
    across OS threads -- so a plain `def` here would let concurrent failed
    auth requests race past the burst limit. Everything this dependency
    calls (`AgentRegistry.authenticate`, both limiters' `.check()`) is a
    fast, non-blocking, synchronous call, so awaiting nothing between them
    is correct; it is `async def` purely for dispatch, not because it
    awaits anything.

    On success the dependency returns the authenticated `agent_id` and also
    sets `request.state.agent_id`, for any other dependency/handler that
    only has access to the `Request` (mirroring `app.py`'s `IngestState` /
    `routes.py`'s `_get_ingest_state` convention of hanging per-request
    state off `request.state`). A successful authentication never touches
    `source_limiter`/`agent_limiter` and logs nothing (spec section 36.5).

    On failure -- any `hammertime.ingest.auth.AgentAuthError` -- the source
    bucket is charged first; if it is already exhausted the agent bucket is
    left alone and the caller sees `429` immediately. Otherwise the agent
    bucket is charged; if *that* is exhausted the caller also sees `429`.
    Only once both buckets accept the charge does the caller see the actual
    outcome: `403` for a correct credential on a disabled agent, `401` with
    the uniform body for every other failure. Exactly one `WARNING` is
    logged per failed attempt, regardless of which of those three responses
    it produced.
    """
    if agent_slot_count <= 0:
        raise ValueError(f"agent_slot_count must be positive, got {agent_slot_count!r}")
    resolved_source_limiter = source_limiter if source_limiter is not None else RateLimiter()
    resolved_agent_limiter = agent_limiter if agent_limiter is not None else RateLimiter()
    resolved_agent_slot_salt = (
        agent_slot_salt if agent_slot_salt is not None else secrets.token_bytes(32)
    )
    auth_failure_rate_rps = auth_failure_rate_per_min / 60.0
    auth_failure_agent_rate_rps = auth_failure_agent_rate_per_min / 60.0

    async def _dependency(request: Request) -> str:
        try:
            agent_id = authenticate_request(request, registry=registry)
        except AgentAuthError as exc:
            attempted_agent_id = _attempted_agent_id(request)
            source_address = _resolve_source_address(request, trusted_proxy_hops=trusted_proxy_hops)
            source_key = _source_bucket_key(source_address)
            agent_key = _agent_bucket_key(
                attempted_agent_id,
                slot_salt=resolved_agent_slot_salt,
                slot_count=agent_slot_count,
            )
            log_agent_id = _log_agent_id(attempted_agent_id)

            try:
                resolved_source_limiter.check(
                    source_key, auth_failure_rate_rps, capacity=auth_failure_burst
                )
            except RateLimitExceeded as throttle_exc:
                logger.warning(
                    "auth_failure outcome=%s agent_id=%r source=%s path=%s",
                    "throttled",
                    log_agent_id,
                    source_key,
                    request.url.path,
                )
                raise throttled_response(
                    throttle_exc, scope="auth-failures", detail=_THROTTLED_DETAIL
                ) from exc

            try:
                resolved_agent_limiter.check(
                    agent_key, auth_failure_agent_rate_rps, capacity=auth_failure_agent_burst
                )
            except RateLimitExceeded as throttle_exc:
                logger.warning(
                    "auth_failure outcome=%s agent_id=%r source=%s path=%s",
                    "throttled",
                    log_agent_id,
                    source_key,
                    request.url.path,
                )
                raise throttled_response(
                    throttle_exc, scope="auth-failures", detail=_THROTTLED_DETAIL
                ) from exc

            outcome = _OUTCOME_BY_EXCEPTION.get(type(exc), "invalid_credential")
            logger.warning(
                "auth_failure outcome=%s agent_id=%r source=%s path=%s",
                outcome,
                log_agent_id,
                source_key,
                request.url.path,
            )

            if isinstance(exc, AgentDisabledError):
                raise HTTPException(status_code=403, detail=_AGENT_DISABLED_DETAIL) from exc
            raise HTTPException(
                status_code=401,
                detail=_UNIFORM_AUTH_FAILURE_DETAIL,
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        request.state.agent_id = agent_id
        return agent_id

    return _dependency
