"""Shared service process lifecycle: one runner, one readiness meaning, one drain.

Spec: section 47

ADR-0009: each service's `main()` is a thin adapter over `run_service`,
which owns logging setup, the startup deadline and its dependency backoff,
signal handling, the drain, and the exit codes (0 clean shutdown, 1 runtime
failure, 2 configuration invalid). Services own their domain behaviour and
-- per decision 3's reading of `Service.run()` -- their own HTTP server, so
this module deliberately depends on neither FastAPI nor uvicorn: it ships a
minimal pure-ASGI admin app for services that have no web framework, plus
the handlers the FastAPI services reuse, so `/healthz`, `/readyz` and
`/metrics` cannot drift apart between the four services.
"""

import asyncio
import importlib.metadata
import json
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hammertime.core.config.loader import load as load_detection_config
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import ConfigurationError
from hammertime.core.telemetry.logging import DEFAULT_LEVEL, configure_logging, get_logger

# --- exit codes (decision 8) ------------------------------------------------

#: Clean shutdown after a signal, drain completed.
EXIT_OK = 0
#: Runtime failure: start() deadline or non-transient error, run() exited,
#: drain timed out or was aborted, or an uncaught exception in the runner.
EXIT_FAILURE = 1
#: Configuration invalid, detected before any connection was made.
EXIT_CONFIG_INVALID = 2

# --- environment keys and their defaults (decisions 5, 6, 7) ----------------

STARTUP_TIMEOUT_ENV = "HAMMERTIME_STARTUP_TIMEOUT_S"
SHUTDOWN_TIMEOUT_ENV = "HAMMERTIME_SHUTDOWN_TIMEOUT_S"
CONFIG_POLL_INTERVAL_ENV = "HAMMERTIME_CONFIG_POLL_INTERVAL_S"
CONFIG_PATH_ENV = "HAMMERTIME_CONFIG_PATH"
LOG_LEVEL_ENV = "HAMMERTIME_LOG_LEVEL"

DEFAULT_STARTUP_TIMEOUT_S = 60.0
DEFAULT_SHUTDOWN_TIMEOUT_S = 8.0
DEFAULT_CONFIG_POLL_INTERVAL_S = 1.0

#: Transient-dependency backoff: 0.5 s doubling, capped at 5 s (decision 5).
RETRY_INITIAL_DELAY_S = 0.5
RETRY_MAX_DELAY_S = 5.0


# --- the service contract (decision 3) --------------------------------------


@runtime_checkable
class Service(Protocol):
    """What `run_service` needs from a service, and nothing more."""

    name: str

    async def start(self) -> None:
        """Connect, recover, become ready; returns when ready."""
        ...

    async def run(self) -> None:
        """Serve until `stop()` is called; returns after the drain."""
        ...

    async def stop(self) -> None:
        """Request shutdown; idempotent; safe before `start()`."""
        ...

    @property
    def ready(self) -> bool:
        """Whether the service can answer correctly (decision 4)."""
        ...


@runtime_checkable
class DescribesStartup(Protocol):
    """Optional `Service` extension: extra fields for the `starting` record.

    Decision 5 step 3 names the fields that record carries (`bus_kind`,
    `config_version`, `bind`, the consumer group, ...), but every one of
    them is service-specific while decision 3 fixes `Service` at four
    members. A service that wants those fields logged implements this hook;
    `run_service` merges whatever it returns into the record. It MUST NOT
    return a token, key, or URL with credentials -- the record is logged
    verbatim.
    """

    def startup_fields(self) -> Mapping[str, object]: ...


# --- readiness and the three admin endpoints (decision 4) -------------------


class Readiness:
    """The readiness flag a service and its HTTP app share.

    `ready` is `True` only between `mark_ready()` (end of `start()`) and
    `mark_stopping()` (`stop()` called); `state` is what `/readyz` reports
    in the body, so an operator can tell a slow start from a drain.
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state = "starting"

    @property
    def ready(self) -> bool:
        return self._state == "ready"

    @property
    def state(self) -> str:
        """`"starting"`, `"ready"` or `"stopping"`."""
        return self._state

    def mark_ready(self) -> None:
        self._state = "ready"

    def mark_stopping(self) -> None:
        self._state = "stopping"


@dataclass(frozen=True, slots=True)
class AdminResponse:
    """A framework-independent HTTP response the admin handlers return.

    The pure-ASGI app below and the FastAPI services both render this, which
    is what keeps their status codes and bodies identical.
    """

    status_code: int
    body: bytes
    media_type: str


#: Prometheus text exposition format (spec section 37).
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4"
_JSON_CONTENT_TYPE = "application/json"


def _json_response(status_code: int, payload: Mapping[str, str]) -> AdminResponse:
    return AdminResponse(
        status_code=status_code,
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        media_type=_JSON_CONTENT_TYPE,
    )


def healthz_response() -> AdminResponse:
    """`GET /healthz`: 200 as soon as the socket is open (liveness)."""
    return _json_response(200, {"status": "ok"})


def readyz_response(readiness: Readiness) -> AdminResponse:
    """`GET /readyz`: 200 `ready`, else 503 `starting`/`stopping`."""
    if readiness.ready:
        return _json_response(200, {"status": "ready"})
    return _json_response(503, {"status": readiness.state})


class ServiceNotReady(Exception):
    """A request reached a domain endpoint while the service was not ready.

    Framework-free on purpose: the FastAPI services raise it from a
    readiness gate and render it with `not_ready_response` in one exception
    handler, so every one of them answers the identical 503 body rather
    than each inventing its own `detail` shape (decision 4).
    """


def not_ready_response(readiness: Readiness) -> AdminResponse:
    """The 503 a domain read or ingestion endpoint answers while not ready.

    Deliberately the same body as `/readyz` (decision 4): a client that
    polls either one sees the same thing, and the ingestion protocol
    already reserves 503 for "retry with the same sequence".
    """
    return _json_response(503, {"status": readiness.state})


def metrics_response(render: Callable[[], bytes] | None = None) -> AdminResponse:
    """`GET /metrics`: Prometheus exposition.

    The body is spec section 37's and belongs to the telemetry epic; until
    it exists `render` is `None` and the body is empty, which is a valid
    (if uninteresting) exposition.
    """
    return AdminResponse(
        status_code=200,
        body=b"" if render is None else render(),
        media_type=METRICS_CONTENT_TYPE,
    )


Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_ADMIN_PATHS = ("/healthz", "/readyz", "/metrics")


def create_admin_app(
    readiness: Readiness, *, render_metrics: Callable[[], bytes] | None = None
) -> ASGIApp:
    """A minimal pure-ASGI app serving `/healthz`, `/readyz` and `/metrics`.

    For the services that have no web framework of their own (the
    aggregator, decision 4). It handles the ASGI lifespan protocol so a
    plain `uvicorn.Server` can drive it, answers 405 to a non-GET on a
    known path and 404 elsewhere, and has no route table beyond these
    three -- it is an admin surface, not an API.
    """

    async def _send_response(send: Send, response: AdminResponse) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [
                    (b"content-type", response.media_type.encode("latin-1")),
                    (b"content-length", str(len(response.body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": response.body})

    async def _lifespan(receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def admin_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await _lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise NotImplementedError(f"admin app does not handle {scope['type']!r} connections")
        if scope["path"] not in _ADMIN_PATHS:
            await _send_response(send, _json_response(404, {"status": "not_found"}))
        elif scope["method"] != "GET":
            await _send_response(send, _json_response(405, {"status": "method_not_allowed"}))
        elif scope["path"] == "/healthz":
            await _send_response(send, healthz_response())
        elif scope["path"] == "/readyz":
            await _send_response(send, readyz_response(readiness))
        else:
            await _send_response(send, metrics_response(render_metrics))

    return admin_app


# --- environment helpers ----------------------------------------------------


def _env_float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = source.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number, got {value}")
    return value


def startup_timeout_s(env: Mapping[str, str] | None = None) -> float:
    """`HAMMERTIME_STARTUP_TIMEOUT_S` (default 60), for callers outside the runner."""
    return _env_float(
        os.environ if env is None else env, STARTUP_TIMEOUT_ENV, DEFAULT_STARTUP_TIMEOUT_S
    )


def config_poll_interval_s(env: Mapping[str, str] | None = None) -> float:
    """`HAMMERTIME_CONFIG_POLL_INTERVAL_S` (default 1.0), for a service's own poller."""
    return _env_float(
        os.environ if env is None else env, CONFIG_POLL_INTERVAL_ENV, DEFAULT_CONFIG_POLL_INTERVAL_S
    )


# --- transient dependency retry (decision 5 step 5) -------------------------


async def connect_with_retry[T](
    dependency: str,
    connect: Callable[[], Awaitable[T]],
    *,
    timeout_s: float | None = None,
    transient: tuple[type[BaseException], ...] = (OSError,),
    logger: Any = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Call `connect`, retrying transient failures with capped exponential backoff.

    Compose's `depends_on` orders container start but not broker readiness,
    so a service that failed fast on the first refused connection would
    crash-loop through the first seconds of every deploy (decision 5). The
    delay starts at 0.5 s and doubles to a 5 s cap; every failed attempt
    logs `WARNING event=dependency_unavailable dependency=<dependency>
    attempt=N`.

    `timeout_s` defaults to `HAMMERTIME_STARTUP_TIMEOUT_S` -- the same
    deadline `run_service` puts around `start()` as a whole, so one slow
    dependency cannot outlive the process's startup budget. When it expires
    the last transient error is re-raised unchanged, which `run_service`
    reports as `start_failed`. A failure that is not in `transient` is never
    retried: it propagates on the first attempt.
    """
    log = get_logger() if logger is None else logger
    deadline = monotonic() + (startup_timeout_s() if timeout_s is None else timeout_s)
    delay = RETRY_INITIAL_DELAY_S
    attempt = 0
    while True:
        attempt += 1
        try:
            return await connect()
        except transient as exc:
            remaining = deadline - monotonic()
            log.warning(
                "dependency_unavailable",
                dependency=dependency,
                attempt=attempt,
                error=str(exc),
                retry_in_s=round(delay, 3),
            )
            if remaining <= delay:
                raise
            await sleep(delay)
            delay = min(delay * 2, RETRY_MAX_DELAY_S)


# --- configuration polling (decision 6) -------------------------------------


class ConfigPoller:
    """Polls `HAMMERTIME_CONFIG_PATH`, applying strictly newer versions only.

    A document whose `config_version` is equal to or lower than the one in
    force is ignored silently -- an operator rolling back republishes the
    old thresholds under a *new* version (`docs/runbook.md`). A document
    that fails to load or validate logs `ERROR event=config_rejected` and
    leaves the previous version in force: spec section 34 requires a
    configuration change to define its effect on existing state, and a
    broken file has none.

    `apply` is the service's own re-evaluation coroutine. It is awaited
    *before* the new version becomes `current`, so a version becomes
    visible in emitted events and read responses only once the
    re-evaluation it triggered has been applied (spec section 47.3).
    """

    def __init__(
        self,
        path: Path,
        current: DetectionConfig,
        *,
        apply: Callable[[DetectionConfig], Awaitable[None]] | None = None,
        poll_interval_s: float = DEFAULT_CONFIG_POLL_INTERVAL_S,
        logger: Any = None,
    ) -> None:
        self._path = path
        self._current = current
        self._apply = apply
        self._poll_interval_s = poll_interval_s
        self._log = get_logger() if logger is None else logger
        self._stopping = asyncio.Event()

    @property
    def current(self) -> DetectionConfig:
        """The configuration in force."""
        return self._current

    async def poll_once(self) -> DetectionConfig:
        """Read the document once, apply it if it is newer, return the config in force."""
        try:
            candidate = load_detection_config(self._path)
        except (ConfigurationError, ValueError) as exc:
            self._log.error("config_rejected", path=str(self._path), error=str(exc))
            return self._current
        if candidate.config_version <= self._current.config_version:
            return self._current
        if self._apply is not None:
            await self._apply(candidate)
        previous = self._current.config_version
        self._current = candidate
        self._log.info(
            "config_applied",
            path=str(self._path),
            config_version=candidate.config_version,
            previous_config_version=previous,
        )
        return self._current

    async def run(self) -> None:
        """Poll every `poll_interval_s` until `stop()` is called."""
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), self._poll_interval_s)
            except TimeoutError:
                pass
            else:
                return
            try:
                await self.poll_once()
            except Exception as exc:
                # A rejected document is handled (and logged) inside
                # poll_once; this is the service's own apply hook failing.
                # The poller must survive it -- the previous version stays
                # in force and the next poll retries -- rather than taking
                # the process down.
                self._log.error("config_apply_failed", error=str(exc), exc_info=True)

    def stop(self) -> None:
        """Ask `run()` to return at its next poll boundary; idempotent."""
        self._stopping.set()


# --- the runner (decisions 1, 5, 7, 8) --------------------------------------


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(f"hammertime-{name}")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - always installed
        return "unknown"


def _starting_fields(name: str, service: Service) -> dict[str, object]:
    fields: dict[str, object] = {"version": _package_version(name)}
    if isinstance(service, DescribesStartup):
        fields.update(service.startup_fields())
    return fields


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, handler: Callable[[signal.Signals], None]
) -> list[signal.Signals]:
    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handler, sig)
        except NotImplementedError:  # pragma: no cover - not POSIX
            continue
        installed.append(sig)
    return installed


async def _stop_quietly(service: Service, log: Any) -> None:
    """Best-effort `stop()` on a failed start: the real diagnostic must survive it."""
    try:
        await service.stop()
    except Exception as exc:
        log.warning("stop_failed", error=str(exc), exc_info=True)


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        # Whoever owns the task reports its failure (or it is one of this
        # module's own wait tasks, whose failure has nothing to report).
        return


async def _supervise(
    service: Service,
    *,
    log: Any,
    startup_timeout: float,
    shutdown_timeout: float,
) -> int:
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    drain_aborted = asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        if not stop_requested.is_set():
            log.info("stopping", signal=sig.name)
            stop_requested.set()
        else:
            log.warning("shutdown_aborted", signal=sig.name)
            drain_aborted.set()

    installed = _install_signal_handlers(loop, _on_signal)
    try:
        started_at = time.monotonic()
        try:
            await asyncio.wait_for(service.start(), startup_timeout)
        except TimeoutError:
            log.error("start_failed", reason="startup_timeout", timeout_s=startup_timeout)
            await _stop_quietly(service, log)
            return EXIT_FAILURE
        except Exception as exc:
            log.error("start_failed", error=str(exc), exc_info=True)
            await _stop_quietly(service, log)
            return EXIT_FAILURE
        log.info("ready", startup_seconds=round(time.monotonic() - started_at, 3))

        if stop_requested.is_set():
            # A signal arrived during start(): drain without ever serving.
            await _stop_quietly(service, log)
            log.info("stopped", exit_code=EXIT_OK)
            return EXIT_OK

        run_task = asyncio.create_task(service.run(), name=f"{service.name}-run")
        stop_task = asyncio.create_task(stop_requested.wait(), name=f"{service.name}-stop")
        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        await _cancel(stop_task)

        if run_task.done():
            # Decision 5 step 7: a service with nothing left to do has crashed.
            error = run_task.exception()
            log.error("run_exited", error=None if error is None else str(error), exc_info=error)
            await _stop_quietly(service, log)
            return EXIT_FAILURE

        await service.stop()
        abort_task = asyncio.create_task(drain_aborted.wait(), name=f"{service.name}-abort")
        await asyncio.wait(
            {run_task, abort_task},
            timeout=shutdown_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        await _cancel(abort_task)

        if not run_task.done():
            if drain_aborted.is_set():
                log.warning("shutdown_aborted", reason="second_signal")
            else:
                log.warning("shutdown_timeout", timeout_s=shutdown_timeout)
            await _cancel(run_task)
            return EXIT_FAILURE

        error = run_task.exception()
        if error is not None:
            log.error("run_exited", error=str(error), exc_info=error)
            return EXIT_FAILURE
        log.info("stopped", exit_code=EXIT_OK)
        return EXIT_OK
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def run_service(
    name: str,
    factory: Callable[[], Service],
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run one service process end to end and return its exit code.

    `main()` is what turns this into a process status (`raise
    SystemExit(run_service(...))`); returning the code instead of raising
    keeps the whole lifecycle callable from a test without a subprocess.

    `factory` is the service's composition root partially applied to the
    environment (decision 1). It is called before any event loop or signal
    handler exists, and a `ValueError`/`ConfigurationError` from it is
    reported as `config_invalid` and exit 2 -- a service must never
    half-start on a bad configuration.
    """
    source = os.environ if env is None else env

    level = source.get(LOG_LEVEL_ENV, DEFAULT_LEVEL)
    try:
        configure_logging(name, level)
    except ValueError as exc:
        # Reported through the default-level logger rather than as a
        # traceback: a bad log level is still a configuration error (2).
        configure_logging(name, DEFAULT_LEVEL)
        get_logger(name).error("config_invalid", error=str(exc))
        return EXIT_CONFIG_INVALID
    log = get_logger(name)

    try:
        startup_timeout = _env_float(source, STARTUP_TIMEOUT_ENV, DEFAULT_STARTUP_TIMEOUT_S)
        shutdown_timeout = _env_float(source, SHUTDOWN_TIMEOUT_ENV, DEFAULT_SHUTDOWN_TIMEOUT_S)
        service = factory()
    except (ValueError, ConfigurationError) as exc:
        log.error("config_invalid", error=str(exc))
        return EXIT_CONFIG_INVALID

    try:
        log.info("starting", **_starting_fields(name, service))
        return asyncio.run(
            _supervise(
                service,
                log=log,
                startup_timeout=startup_timeout,
                shutdown_timeout=shutdown_timeout,
            )
        )
    except Exception as exc:
        # Decision 8: nothing else maps to an exit code, and main() never
        # lets a traceback be the only diagnostic.
        log.error("run_failed", error=str(exc), exc_info=True)
        return EXIT_FAILURE
