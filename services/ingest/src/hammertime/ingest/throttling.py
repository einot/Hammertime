"""The one 429 response shape every throttled path shares (spec section 36.7).

`RateLimiter` (ADR-0007, ADR-0008) is used from three call sites -- the
existing per-request limiter in `api/routes.py`, the new failed-auth
limiters in `auth/middleware.py`, and the new per-observation limiter in
`api/routes.py` -- and all three MUST answer a `RateLimitExceeded` the same
way: `429`, an integer `Retry-After` of at least one second, and an
`X-RateLimit-Scope` header naming which budget rejected the request. This
module is the single place that shape is built, so the three call sites
cannot drift from each other or from Section 36.7.

Deliberately outside both `api/` and `auth/`: both packages need it and
neither should depend on the other (`app.py` is the only module that
currently imports both).
"""

import math
from typing import Literal

from fastapi import HTTPException
from hammertime.ingest.ratelimit import RateLimitExceeded

#: Section 36.7: "`X-RateLimit-Scope` names which budget rejected the
#: request" -- `auth-failures` (ADR-0007), `requests` (the existing flat
#: per-request charge), `observations` (ADR-0008).
RateLimitScope = Literal["auth-failures", "requests", "observations"]


def throttled_response(
    exc: RateLimitExceeded, *, scope: RateLimitScope, detail: str
) -> HTTPException:
    """Build the shared `429` `HTTPException` for a `RateLimitExceeded`.

    `Retry-After` is `max(1, ceil(exc.retry_after))` -- an integer count of
    seconds, never zero, per Section 36.7 -- and `X-RateLimit-Scope` is
    `scope`. `detail` becomes the response body's `{"detail": ...}`
    (FastAPI's default `HTTPException` body shape).
    """
    retry_after_seconds = max(1, math.ceil(exc.retry_after))
    return HTTPException(
        status_code=429,
        detail=detail,
        headers={
            "Retry-After": str(retry_after_seconds),
            "X-RateLimit-Scope": scope,
        },
    )
