"""The trie service's HTTP app: the admin endpoints now, the read API in slice 3.

Spec: section 47 (slice 3 adds section 29 and section 31); ADR-0017 decisions
9 and 13, ADR-0009 decision 4 and A4, A6.

Slice 1 answers `GET /healthz`, `GET /readyz` and `GET /metrics` through
`hammertime.core.runtime`'s `healthz_response`, `readyz_response` and
`metrics_response`, so the status codes and bodies are identical to every
other service's. `app.state.readiness` holds the service's `Readiness`, and a
`ServiceNotReady` raised by any route is rendered as `not_ready_response`, so
slice 3's read routes need only raise it.

Every route and dependency is `async def` (ADR-0017 decision 9, R3): FastAPI
runs a plain `def` one in a threadpool, and no thread but the event loop's
may touch `TrieState`.

Slice 3 adds, on this app:
/prefix/192.168.0.0/16 -> hot_ips, capacity, hot_ratio, state
/ip/192.168.1.42       -> state, request_count, matched_prefixes along the path
/prefixes/hot?minimal=true -> most specific qualifying prefixes only (section 31)
"""

from fastapi import FastAPI, Request, Response
from hammertime.core.runtime import (
    AdminResponse,
    Readiness,
    ServiceNotReady,
    healthz_response,
    metrics_response,
    not_ready_response,
    readyz_response,
)


def create_app(readiness: Readiness) -> FastAPI:
    """The trie's FastAPI app, sharing `readiness` with the service."""
    app = FastAPI(title="hammertime-trie")
    app.state.readiness = readiness

    async def service_not_ready(request: Request, exc: Exception) -> Response:
        """Render `ServiceNotReady` as ADR-0009 decision 4's 503, not FastAPI's `detail` shape."""
        return _as_response(not_ready_response(_readiness_of(request.app)))

    app.add_exception_handler(ServiceNotReady, service_not_ready)

    @app.get("/healthz")
    async def healthz() -> Response:
        """Liveness: 200 as soon as the socket is open, whatever the readiness."""
        return _as_response(healthz_response())

    @app.get("/readyz")
    async def readyz(request: Request) -> Response:
        """Readiness: 200 once the replay has reached the startup log end, else 503."""
        return _as_response(readyz_response(_readiness_of(request.app)))

    @app.get("/metrics")
    async def metrics() -> Response:
        """Empty until the telemetry epic renders section 37's series."""
        return _as_response(metrics_response())

    return app


def _as_response(admin: AdminResponse) -> Response:
    """Render one of `hammertime.core.runtime`'s admin responses.

    The content type is set as a raw header rather than via `media_type=`:
    Starlette appends `; charset=utf-8` to any `text/*` media type it is
    given, which would change `/metrics`' announced content type.
    """
    return Response(
        content=admin.body,
        status_code=admin.status_code,
        headers={"content-type": admin.media_type},
    )


def _readiness_of(app: FastAPI) -> Readiness:
    return app.state.readiness  # type: ignore[no-any-return]
