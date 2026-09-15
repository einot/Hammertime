"""Bearer-token agent authentication for the ingest API.

Spec: section 36, section 23

Bearer token, not mTLS: this repo has no certificate provisioning or mTLS
termination configured anywhere (no PKI, nothing in `deploy/`), so this
module implements `Authorization: Bearer <token>` verification against
`hammertime.ingest.auth.agents.AgentRegistry` as the ingest API's only
authentication mechanism for now. mTLS would need infrastructure this repo
does not have yet; building it here would be unfounded scope creep for this
issue.

`docs/protocol/observation-v1.md`'s response table calls for 401 ("unknown
agent") or 403 ("unauthorized agent"). This module treats a missing/unknown
`agent_id` as 401, and a known agent with a wrong or disabled credential as
403 -- see `hammertime.ingest.auth`'s exception hierarchy for the exact
split.

Message-identity replay protection (spec section 23: `(agent_id, sequence)`
dedup) is *not* implemented here -- that is issue #32's
`hammertime.ingest.dedup` service, layered on top of a request this module
has already authenticated. A bearer token in this scheme is a static,
long-lived per-agent credential with no embedded nonce/freshness of its
own, so there is no narrower credential-level replay concern for this
module to separately own; resent-message detection belongs entirely to
dedup/service.py.

Not wired into `api/routes.py`: that integration is issue #32's job, once
rate limiting (#29) and the store issues have landed. `require_agent`
below is meant to be usable standalone, e.g. `Depends(require_agent(registry))`.
"""

from collections.abc import Callable

from fastapi import HTTPException, Request
from hammertime.ingest.auth import AgentAuthError, UnknownAgentError
from hammertime.ingest.auth.agents import AgentRegistry

_AUTHORIZATION_HEADER = "authorization"
_AGENT_ID_HEADER = "x-agent-id"
_BEARER_PREFIX = "Bearer "


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get(_AUTHORIZATION_HEADER)
    if header is None or not header.startswith(_BEARER_PREFIX):
        raise UnknownAgentError("missing or malformed Authorization: Bearer header")
    token = header[len(_BEARER_PREFIX) :]
    if not token:
        raise UnknownAgentError("empty bearer token")
    return token


def authenticate_request(request: Request, *, registry: AgentRegistry) -> str:
    """Authenticate one HTTP request and return the caller's `agent_id`.

    Reads the agent's identity from `X-Agent-Id` and its credential from
    `Authorization: Bearer <token>`, and raises `hammertime.ingest.auth`
    exceptions rather than `HTTPException` so callers can pick their own
    status-code mapping; `require_agent` below is the FastAPI-flavoured
    wrapper that does that mapping per docs/protocol/observation-v1.md.
    """
    agent_id = request.headers.get(_AGENT_ID_HEADER)
    if not agent_id:
        raise UnknownAgentError("missing X-Agent-Id header")
    token = _extract_bearer_token(request)
    record = registry.authenticate(agent_id, token)
    return record.agent_id


def require_agent(registry: AgentRegistry) -> Callable[[Request], str]:
    """Build a FastAPI dependency that authenticates the calling agent.

    On success the dependency returns the authenticated `agent_id` and also
    sets `request.state.agent_id`, for any other dependency/handler that
    only has access to the `Request` (mirroring `app.py`'s `IngestState` /
    `routes.py`'s `_get_ingest_state` convention of hanging per-request
    state off `request.state`). On failure it raises a clean
    `HTTPException(401)` (unknown agent) or `HTTPException(403)` (known
    agent, bad or disabled credential) -- no other exception type escapes.
    """

    def _dependency(request: Request) -> str:
        try:
            agent_id = authenticate_request(request, registry=registry)
        except UnknownAgentError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except AgentAuthError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        request.state.agent_id = agent_id
        return agent_id

    return _dependency
