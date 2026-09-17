"""Service process lifecycle: the shared runner (spec section 47, ADR-0009).

Covers ADR-0009 decision 3 (the `Service` protocol), decision 4 (readiness and
the three admin endpoints), decision 5 (startup order and dependency backoff),
decision 6 (configuration polling), decision 7 (drain on signal) and decision 8
(exit codes), i.e. spec sections 47.1-47.5.

Written from ADR-0009 and section 47. While this file was being written a
path-guard hole (since closed) exposed some implementation signatures in
`hammertime/core/runtime.py`; the two call sites that had been affected --
`connect_with_retry` and `create_admin_app` -- were afterwards rewritten from
ADR-0009 alone. What is asserted below is what the ADR and section 47 promise,
not what any implementation happens to do.

ASSUMED spellings -- ADR-0009 pins the *behaviour* of each of these but not the
parameter or member names. Every one of them is reached through a single
helper near the top of this file, so reconciling a name is a one-line change
there rather than a sweep through the tests:

* `AdminResponse` exposes `status_code: int` plus a body and a content type
  under names the ADR does not fix, so `_body`/`_content_type` accept any of
  the plausible spellings (including a `headers` mapping or ASGI-style header
  pairs) and fail loudly when none is present. Section 47.2 and
  `docs/protocol/read-api-v1.md` pin the status, the content type and the JSON
  body of each admin response, but not the field names carrying them.
* `Readiness()` starts in the "starting" state and is moved on with
  `mark_ready()` / `mark_stopping()`, exposing the current answer as
  `.ready` (`_become_ready`, `_become_stopping`, `_is_ready`). ADR-0009
  decision 4 pins the three states and the transitions ("`ready` becomes
  `True` at the end of `start()` and `False` once `stop()` is called"); the
  method names are this file's assumption.
* `create_admin_app(readiness)` (`_admin_app`). Decision 4 requires `/readyz`
  to distinguish `starting` from `stopping`, which a two-state
  `Service.ready` bool cannot do, so the app is given the three-state
  readiness object. The ADR names neither the helper nor its parameter.
* `connect_with_retry(dependency, connect)` (`_connect`): decision 5 step 5
  logs `dependency=bus|store attempt=N`, so the helper needs the dependency
  label and the connection attempt to retry. The ADR pins no further
  arguments -- in particular it never says how a failure is classified
  transient, so these tests use the one example the ADR itself gives ("a
  service that fails fast on the first refused connection"): a refused
  connection is transient.
* `ConfigPoller(path, current=..., apply=...)` polls that path with a
  configuration already in force, `await poller.poll_once()` performs exactly
  one poll (the operation ADR-0009 decision 3's `reload_config()` is defined
  in terms of: "one poll synchronously"), and `poller.current` is the
  `DetectionConfig` in force (`_make_poller`, `_poll`, `_current`). `apply` is
  assumed to be awaitable, since the re-evaluation it triggers (section 34) is
  part of a running service's event loop. Decision 2 has every service load
  the detection document before anything else starts, so there is always a
  version in force for "strictly greater than" to be measured against.
* `run_service(name, factory, *, env=...)` reads `HAMMERTIME_LOG_LEVEL`,
  `HAMMERTIME_STARTUP_TIMEOUT_S` and `HAMMERTIME_SHUTDOWN_TIMEOUT_S` from the
  `env` mapping it is given (`_env`), so no test here touches `os.environ`.
  `factory()` takes no arguments, as in decision 1's `main()` sketch and
  decision 5 step 2 (`service = factory()`).

Deliberate omissions, reported rather than guessed at:

* The structured log records decision 5 names (`config_invalid`, `starting`,
  `ready`, `dependency_unavailable`, `run_exited`, `stopping`,
  `shutdown_timeout`) are not asserted: the ADR fixes the event names and the
  fields but not the record shape (which logger, whether the fields are record
  attributes or serialised into the message), so there is nothing to assert
  against that is not a guess about `configure_logging`. The exit codes those
  records accompany are asserted in full below.
* `metrics_response`'s exposition content is section 37's and "may be empty
  until the telemetry epic lands" (decision 4), so only its status and content
  type are asserted.
* The 0.5 s doubling backoff schedule of decision 5 step 5 is *not* asserted.
  Nothing in ADR-0009 gives `connect_with_retry` an injectable sleep (unlike
  `core.config.loader.watch(..., sleep=...)`, which has one), so asserting the
  schedule means waiting 17.5 s of wall clock. What is asserted instead, at a
  cost of 50 ms, is that a refused connection is neither propagated nor
  retried immediately, and that the retry loop is interruptible by the startup
  deadline that encloses it.
* `Service` conformance is asserted structurally. ADR-0009 decision 3 declares
  a plain `Protocol` and says nothing about `runtime_checkable`, so
  `isinstance` against it is not something the ADR promises will even work.

The `run_service` tests below raise `SIGTERM`/`SIGINT` in this process, which
is the only way to exercise decision 7 at the level it is specified. Decision
5 step 4 installs those handlers *before* `start()` is awaited, so by the time
a fake service's `run()` raises one, the runner owns the signal. `_FakeService`
checks that the runner's handler really is installed before raising, so a
runner that installs none fails a test instead of terminating the test session;
the autouse fixture restores whatever handlers were installed beforehand.
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import ConfigurationError
from hammertime.core.runtime import (
    AdminResponse,
    ConfigPoller,
    Readiness,
    Service,
    ServiceNotReady,
    connect_with_retry,
    create_admin_app,
    healthz_response,
    metrics_response,
    not_ready_response,
    readyz_response,
    run_service,
)

# --------------------------------------------------------------------------
# Assumed spellings. See the module docstring; reconcile names here only.
# --------------------------------------------------------------------------

_BODY_NAMES = ("body", "content", "payload", "data", "text")
_CONTENT_TYPE_NAMES = ("content_type", "media_type", "mime_type", "mimetype")


def _first_attr(obj: object, names: tuple[str, ...]) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _header(response: AdminResponse, name: str) -> Any:
    """A header value off whatever `headers` carrier the response uses."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    pairs = headers.items() if isinstance(headers, Mapping) else headers
    for key, value in pairs:
        key_text = key.decode() if isinstance(key, bytes) else str(key)
        if key_text.lower() == name:
            return value
    return None


def _status(response: AdminResponse) -> int:
    status: int = response.status_code
    return status


def _content_type(response: AdminResponse) -> str:
    value = _first_attr(response, _CONTENT_TYPE_NAMES)
    if value is None:
        value = _header(response, "content-type")
    assert value is not None, (
        f"no content type on {response!r}: looked for attributes "
        f"{_CONTENT_TYPE_NAMES} and a content-type header"
    )
    return value.decode() if isinstance(value, bytes) else str(value)


def _body(response: AdminResponse) -> bytes:
    value = _first_attr(response, _BODY_NAMES)
    assert value is not None, f"no body on {response!r}: looked for attributes {_BODY_NAMES}"
    if isinstance(value, Mapping):
        # A response that carries its payload already parsed still pins the
        # same JSON document the protocol doc specifies.
        return json.dumps(dict(value)).encode()
    if isinstance(value, str):
        return value.encode()
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    raise AssertionError(f"admin response body is neither text nor bytes: {value!r}")


def _parts(response: AdminResponse) -> tuple[int, str, bytes]:
    return _status(response), _content_type(response), _body(response)


def _is_ready(readiness: Readiness) -> bool:
    return readiness.ready


def _become_ready(readiness: Readiness) -> None:
    readiness.mark_ready()


def _become_stopping(readiness: Readiness) -> None:
    readiness.mark_stopping()


def _admin_app(readiness: Readiness) -> Any:
    return create_admin_app(readiness)


async def _connect(
    connect: Callable[[], Awaitable[Any]],
    dependency: str = "bus",
) -> Any:
    return await connect_with_retry(dependency, connect)


def _make_poller(
    path: Path,
    apply: Callable[[DetectionConfig], Awaitable[None]],
    current: DetectionConfig,
) -> ConfigPoller:
    return ConfigPoller(path, current=current, apply=apply)


async def _poll(poller: ConfigPoller) -> None:
    await poller.poll_once()


def _current(poller: ConfigPoller) -> DetectionConfig:
    config: DetectionConfig = poller.current
    return config


def _version(poller: ConfigPoller) -> int:
    return _current(poller).config_version


def _declared_members(protocol: object) -> frozenset[str]:
    """The members a class must supply to satisfy `protocol`.

    Read off the class rather than through `isinstance`: ADR-0009 decision 3
    declares a plain `Protocol`, which `isinstance` cannot be used against.
    """
    annotated = frozenset(getattr(protocol, "__annotations__", {}))
    defined = frozenset(name for name in vars(protocol) if not name.startswith("_"))
    return annotated | defined


# --------------------------------------------------------------------------
# Shared fixtures and fakes
# --------------------------------------------------------------------------


class _Fatal(Exception):
    """A dependency error that no amount of retrying can fix."""


def _refused() -> ConnectionRefusedError:
    """The transient failure ADR-0009 decision 5 step 5 names by example."""
    return ConnectionRefusedError(111, "Connection refused")


def _handler_is_installed(sig: signal.Signals) -> bool:
    """Has someone other than the interpreter's default taken `sig` over?

    `signal.raise_signal` on a signal still at its default disposition would
    terminate the test session (SIGTERM) or abort it with a `KeyboardInterrupt`
    (SIGINT, whose default is `signal.default_int_handler`). Every test that
    raises a signal checks this first, so "the runner installed no handler"
    fails a test instead of killing the run.
    """
    handler = signal.getsignal(sig)
    if handler in (None, signal.SIG_DFL, signal.SIG_IGN):
        return False
    return handler is not signal.default_int_handler


@pytest.fixture(autouse=True)
def _restore_signal_handlers() -> Iterator[None]:
    """Hand SIGTERM/SIGINT back to pytest after a `run_service` test."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            if handler is not None:
                signal.signal(sig, handler)


def _env(**overrides: str) -> Mapping[str, str]:
    """An explicit environment for `run_service`; `os.environ` is never touched."""
    env = {
        "HAMMERTIME_LOG_LEVEL": "warning",
        "HAMMERTIME_STARTUP_TIMEOUT_S": "5",
        "HAMMERTIME_SHUTDOWN_TIMEOUT_S": "5",
    }
    env.update(overrides)
    return env


class _FakeService:
    """A scriptable `Service` (ADR-0009 decision 3).

    `opened` records every dependency the service would have connected to, so
    a test can assert decision 2's "never half-start on a bad configuration".
    `unhandled_signal` records a signal the runner had not taken over at the
    moment this fake was about to raise it; the fake declines to raise in that
    case, so the test fails rather than the process dying.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        start_error: BaseException | None = None,
        start_hangs: bool = False,
        run_returns_immediately: bool = False,
        run_error: BaseException | None = None,
        ignore_stop: bool = False,
        signal_on_run: signal.Signals | None = None,
        second_signal_on_stop: bool = False,
    ) -> None:
        self.name = name
        self.start_calls = 0
        self.run_calls = 0
        self.stop_calls = 0
        self.drained = False
        self.opened: list[str] = []
        self.unhandled_signal: signal.Signals | None = None
        self._ready = False
        self._start_error = start_error
        self._start_hangs = start_hangs
        self._run_returns_immediately = run_returns_immediately
        self._run_error = run_error
        self._ignore_stop = ignore_stop
        self._signal_on_run = signal_on_run
        self._second_signal_on_stop = second_signal_on_stop
        self._stop_requested = asyncio.Event()

    @property
    def ready(self) -> bool:
        return self._ready

    def _raise_if_owned(self, sig: signal.Signals) -> bool:
        """Raise `sig` at this process, unless nobody would catch it."""
        if not _handler_is_installed(sig):
            self.unhandled_signal = sig
            return False
        signal.raise_signal(sig)
        return True

    async def start(self) -> None:
        self.start_calls += 1
        self.opened.append("bus")
        if self._start_error is not None:
            raise self._start_error
        if self._start_hangs:
            await asyncio.Event().wait()
        self._ready = True

    async def run(self) -> None:
        self.run_calls += 1
        if self._run_error is not None:
            raise self._run_error
        if self._run_returns_immediately:
            return
        if self._signal_on_run is not None and not self._raise_if_owned(self._signal_on_run):
            # Decision 5 step 4 promises a handler is installed before start();
            # without one there is no drain to observe, so return and let the
            # test report `unhandled_signal`.
            return
        await self._stop_requested.wait()
        if self._second_signal_on_stop and not self._raise_if_owned(signal.SIGTERM):
            return
        if self._ignore_stop:
            await asyncio.Event().wait()
        self.drained = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._ready = False
        self._stop_requested.set()


def _factory(service: _FakeService) -> Callable[[], Service]:
    def build() -> Service:
        return service

    return build


def _assert_signals_were_owned(service: _FakeService) -> None:
    assert service.unhandled_signal is None, (
        f"run_service left {service.unhandled_signal!r} at its default disposition: "
        "decision 5 step 4 installs SIGTERM/SIGINT handlers before start() is awaited"
    )


# --------------------------------------------------------------------------
# Decision 3: the Service protocol
# --------------------------------------------------------------------------

_REQUIRED_MEMBERS = frozenset({"name", "start", "run", "stop", "ready"})


class TestServiceProtocol:
    """ADR-0009 decision 3: what `run_service` needs from a service.

    Decision 3 declares `class Service(Protocol)` with no mention of
    `runtime_checkable`, so conformance is a static property, checked here
    structurally (and by mypy, through the annotated assignment below and
    through `_factory`'s `-> Service`) rather than with `isinstance`.
    """

    def test_the_protocol_declares_the_members_the_runner_uses(self) -> None:
        assert _declared_members(Service) >= _REQUIRED_MEMBERS

    def test_the_protocol_members_are_usable_as_declared(self) -> None:
        service: Service = _FakeService()
        assert service.name == "fake"
        assert service.ready is False
        assert all(callable(getattr(service, member)) for member in ("start", "run", "stop"))


# --------------------------------------------------------------------------
# Decision 8 / section 47.5: exit codes
# --------------------------------------------------------------------------


class TestExitCodeZeroOnCleanShutdown:
    """Decision 7: first signal -> stop() -> drain completes -> exit 0."""

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
    def test_clean_drain_after_a_signal_exits_0(self, sig: signal.Signals) -> None:
        service = _FakeService(signal_on_run=sig)

        code = run_service("fake", _factory(service), env=_env())

        _assert_signals_were_owned(service)
        assert code == 0
        assert service.start_calls == 1
        assert service.run_calls == 1
        assert service.stop_calls >= 1
        assert service.drained is True


class TestExitCodeTwoOnInvalidConfiguration:
    """Decision 2 and 8: rejected before any bus, store or socket is opened."""

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("HAMMERTIME_WINDOW_SECONDS is not an integer"),
            ConfigurationError("detection config rejected"),
        ],
    )
    def test_factory_rejection_exits_2(self, error: Exception) -> None:
        def factory() -> Service:
            raise error

        assert run_service("fake", factory, env=_env()) == 2

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("HAMMERTIME_BUS_BROKERS is empty"),
            ConfigurationError("registry document rejected"),
        ],
    )
    def test_nothing_is_opened_when_the_factory_rejects(self, error: Exception) -> None:
        # "A service must never half-start on a bad configuration": a factory
        # that raises must mean start() was never awaited, so nothing it would
        # have connected to was ever touched.
        built: list[_FakeService] = []

        def factory() -> Service:
            service = _FakeService()
            built.append(service)
            raise error

        assert run_service("fake", factory, env=_env()) == 2
        assert len(built) == 1
        assert built[0].start_calls == 0
        assert built[0].opened == []
        assert built[0].run_calls == 0


class TestExitCodeOneOnRuntimeFailure:
    """Decision 5 steps 5 and 7, decision 7, decision 8."""

    def test_startup_deadline_exceeded_exits_1(self) -> None:
        service = _FakeService(start_hangs=True)

        code = run_service(
            "fake",
            _factory(service),
            env=_env(HAMMERTIME_STARTUP_TIMEOUT_S="0.05"),
        )

        assert code == 1
        assert service.start_calls == 1
        assert service.run_calls == 0  # never ran: it never became ready

    def test_non_transient_start_error_exits_1(self) -> None:
        service = _FakeService(start_error=_Fatal("authentication to the broker failed"))

        assert run_service("fake", _factory(service), env=_env()) == 1
        assert service.run_calls == 0

    def test_run_returning_on_its_own_exits_1(self) -> None:
        # Decision 5 step 7: a service with nothing left to do has crashed.
        service = _FakeService(run_returns_immediately=True)

        assert run_service("fake", _factory(service), env=_env()) == 1
        assert service.start_calls == 1
        assert service.run_calls == 1

    def test_run_raising_exits_1(self) -> None:
        service = _FakeService(run_error=_Fatal("consumer died"))

        assert run_service("fake", _factory(service), env=_env()) == 1
        assert service.run_calls == 1

    def test_drain_exceeding_the_shutdown_deadline_exits_1(self) -> None:
        service = _FakeService(signal_on_run=signal.SIGTERM, ignore_stop=True)

        code = run_service(
            "fake",
            _factory(service),
            env=_env(HAMMERTIME_SHUTDOWN_TIMEOUT_S="0.05"),
        )

        _assert_signals_were_owned(service)
        assert code == 1
        assert service.stop_calls >= 1
        assert service.drained is False

    def test_second_signal_during_the_drain_aborts_and_exits_1(self) -> None:
        service = _FakeService(
            signal_on_run=signal.SIGTERM,
            second_signal_on_stop=True,
            ignore_stop=True,
        )

        started_at = time.monotonic()
        code = run_service(
            "fake",
            _factory(service),
            env=_env(HAMMERTIME_SHUTDOWN_TIMEOUT_S="10"),
        )
        elapsed = time.monotonic() - started_at

        _assert_signals_were_owned(service)
        assert code == 1
        assert service.drained is False
        # "A second signal during the drain aborts it immediately": the exit
        # must come from the second signal, not from the 10 s deadline.
        assert elapsed < 3.0


# --------------------------------------------------------------------------
# Decision 4 / section 47.2: readiness and the admin responses
# --------------------------------------------------------------------------


class TestReadiness:
    """`ready` is True at the end of start() and False once stop() is called."""

    def test_is_not_ready_before_start_completes(self) -> None:
        assert _is_ready(Readiness()) is False

    def test_is_ready_once_start_completes(self) -> None:
        readiness = Readiness()
        _become_ready(readiness)
        assert _is_ready(readiness) is True

    def test_is_not_ready_again_once_shutdown_is_requested(self) -> None:
        readiness = Readiness()
        _become_ready(readiness)
        _become_stopping(readiness)
        assert _is_ready(readiness) is False


class TestAdminResponses:
    """`docs/protocol/read-api-v1.md`'s admin table, section 47.2."""

    def test_healthz_is_200_ok(self) -> None:
        status, content_type, body = _parts(healthz_response())
        assert status == 200
        assert json.loads(body) == {"status": "ok"}
        assert content_type.startswith("application/json")

    def test_readyz_is_503_starting_before_start(self) -> None:
        status, content_type, body = _parts(readyz_response(Readiness()))
        assert status == 503
        assert json.loads(body) == {"status": "starting"}
        assert content_type.startswith("application/json")

    def test_readyz_is_200_ready_when_ready(self) -> None:
        readiness = Readiness()
        _become_ready(readiness)
        status, _, body = _parts(readyz_response(readiness))
        assert status == 200
        assert json.loads(body) == {"status": "ready"}

    def test_readyz_is_503_stopping_after_stop(self) -> None:
        readiness = Readiness()
        _become_ready(readiness)
        _become_stopping(readiness)
        status, _, body = _parts(readyz_response(readiness))
        assert status == 503
        assert json.loads(body) == {"status": "stopping"}

    def test_not_ready_response_matches_readyz_before_start(self) -> None:
        # Domain read endpoints "answer 503 with the same body as /readyz".
        readiness = Readiness()
        assert _parts(not_ready_response(readiness)) == _parts(readyz_response(readiness))

    def test_not_ready_response_matches_readyz_during_shutdown(self) -> None:
        readiness = Readiness()
        _become_ready(readiness)
        _become_stopping(readiness)
        assert _parts(not_ready_response(readiness)) == _parts(readyz_response(readiness))

    def test_metrics_is_prometheus_exposition(self) -> None:
        # The exposition body is section 37's and "may be empty until then"
        # (decision 4), so only the status and the content type are asserted.
        response = metrics_response()
        assert _status(response) == 200
        content_type = _content_type(response)
        assert content_type.startswith("text/plain")
        assert "version=0.0.4" in content_type

    def test_service_not_ready_is_an_exception(self) -> None:
        assert issubclass(ServiceNotReady, Exception)
        with pytest.raises(ServiceNotReady):
            raise ServiceNotReady("trie is not ready")


# --------------------------------------------------------------------------
# Decision 4: the pure-ASGI admin app
#
# Driven through the raw ASGI protocol on purpose: core carries no FastAPI and
# no uvicorn dependency ("no FastAPI dependency in core"), so neither may these
# tests.
# --------------------------------------------------------------------------


def _http_scope(method: str, path: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8083")],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8083),
    }


async def _request(
    app: Any,
    method: str = "GET",
    path: str = "/healthz",
) -> tuple[int, dict[bytes, bytes], bytes]:
    """One ASGI request/response cycle; returns (status, headers, body)."""
    messages: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Mapping[str, Any]) -> None:
        messages.append(dict(message))

    await app(_http_scope(method, path), receive, send)

    starts = [m for m in messages if m["type"] == "http.response.start"]
    assert len(starts) == 1, messages
    headers = {bytes(key).lower(): bytes(value) for key, value in starts[0].get("headers", [])}
    body = b"".join(
        bytes(m.get("body", b"")) for m in messages if m["type"] == "http.response.body"
    )
    return int(starts[0]["status"]), headers, body


async def _lifespan_cycle(app: Any) -> list[str]:
    """Drive one lifespan startup/shutdown; returns the message types sent."""
    scripted = ["lifespan.startup", "lifespan.shutdown"]
    sent: list[str] = []
    index = 0

    async def receive() -> dict[str, Any]:
        nonlocal index
        if index >= len(scripted):
            raise AssertionError("the app asked for more lifespan messages than were scripted")
        message = {"type": scripted[index]}
        index += 1
        return message

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(str(message["type"]))

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.3"}}
    await app(scope, receive, send)
    return sent


class TestAdminApp:
    """Section 47.2's three endpoints, served without FastAPI or uvicorn."""

    async def test_healthz_is_200_even_before_the_service_is_ready(self) -> None:
        # "200 {"status":"ok"} as soon as the socket is open (liveness)."
        app = _admin_app(Readiness())
        status, headers, body = await _request(app, "GET", "/healthz")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}
        assert headers[b"content-type"].startswith(b"application/json")

    async def test_readyz_is_503_starting_before_start(self) -> None:
        app = _admin_app(Readiness())
        status, _, body = await _request(app, "GET", "/readyz")
        assert status == 503
        assert json.loads(body) == {"status": "starting"}

    async def test_readyz_is_200_ready_when_ready(self) -> None:
        readiness = Readiness()
        app = _admin_app(readiness)
        _become_ready(readiness)
        status, _, body = await _request(app, "GET", "/readyz")
        assert status == 200
        assert json.loads(body) == {"status": "ready"}

    async def test_readyz_is_503_stopping_after_stop(self) -> None:
        readiness = Readiness()
        app = _admin_app(readiness)
        _become_ready(readiness)
        _become_stopping(readiness)
        status, _, body = await _request(app, "GET", "/readyz")
        assert status == 503
        assert json.loads(body) == {"status": "stopping"}

    async def test_readyz_tracks_readiness_live(self) -> None:
        readiness = Readiness()
        app = _admin_app(readiness)
        first, _, _ = await _request(app, "GET", "/readyz")
        _become_ready(readiness)
        second, _, _ = await _request(app, "GET", "/readyz")
        _become_stopping(readiness)
        third, _, _ = await _request(app, "GET", "/readyz")
        assert (first, second, third) == (503, 200, 503)

    async def test_metrics_is_200_prometheus_text(self) -> None:
        app = _admin_app(Readiness())
        status, headers, body = await _request(app, "GET", "/metrics")
        assert status == 200
        content_type = headers[b"content-type"].decode()
        assert content_type.startswith("text/plain")
        assert "version=0.0.4" in content_type
        assert isinstance(body, bytes)  # content is section 37's; may be empty

    async def test_metrics_is_served_before_the_service_is_ready(self) -> None:
        # Decision 4 gates only the domain endpoints on readiness; /metrics is
        # scraped by Prometheus for the whole life of the process.
        app = _admin_app(Readiness())
        status, _, _ = await _request(app, "GET", "/metrics")
        assert status == 200

    @pytest.mark.parametrize("path", ["/", "/prefix/10.0.0.0/8", "/healthzz", "/metrics/"])
    async def test_unknown_path_is_404(self, path: str) -> None:
        app = _admin_app(Readiness())
        status, _, _ = await _request(app, "GET", path)
        assert status == 404

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    @pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
    async def test_non_get_method_is_405(self, method: str, path: str) -> None:
        app = _admin_app(Readiness())
        status, _, _ = await _request(app, method, path)
        assert status == 405

    async def test_lifespan_is_not_a_precondition_for_healthz(self) -> None:
        # The admin app is served by uvicorn (decision 4). Whether it takes
        # part in the lifespan protocol is not specified; what is specified is
        # that /healthz answers 200 "as soon as the socket is open", so it must
        # not depend on a lifespan handshake, and a lifespan cycle must never
        # be reported as failed.
        readiness = Readiness()
        app = _admin_app(readiness)

        before, _, _ = await _request(app, "GET", "/healthz")
        try:
            sent = await _lifespan_cycle(app)
        except Exception:
            # Declining the lifespan protocol outright is ASGI-legal: uvicorn's
            # lifespan="auto" falls back to not using it.
            sent = []
        after, _, _ = await _request(app, "GET", "/healthz")

        assert (before, after) == (200, 200)
        assert all(not kind.endswith(".failed") for kind in sent), sent
        assert sent in ([], ["lifespan.startup.complete", "lifespan.shutdown.complete"])


# --------------------------------------------------------------------------
# Decision 5 step 5: transient dependencies and backoff
#
# The 0.5 s doubling schedule itself is not asserted: ADR-0009 gives no seam
# for injecting the sleep, so asserting it would mean 17.5 s of wall clock.
# See the module docstring.
# --------------------------------------------------------------------------


class TestConnectWithRetry:
    """Decision 5 step 5: transient failures retry with backoff, under a deadline."""

    @pytest.mark.parametrize("dependency", ["bus", "store"])
    async def test_returns_the_connection_when_the_first_attempt_succeeds(
        self, dependency: str
    ) -> None:
        attempts = 0

        async def connect() -> str:
            nonlocal attempts
            attempts += 1
            return dependency

        assert await _connect(connect, dependency) == dependency
        assert attempts == 1

    async def test_a_non_transient_error_propagates_without_retrying(self) -> None:
        # The ADR does not enumerate the non-transient errors; what it does
        # require is that one of them ends startup (exit 1) rather than being
        # retried, so an error the helper cannot recognise must propagate.
        attempts = 0

        async def connect() -> str:
            nonlocal attempts
            attempts += 1
            raise _Fatal("authentication rejected")

        with pytest.raises(_Fatal):
            await _connect(connect)

        assert attempts == 1

    async def test_a_refused_connection_is_retried_rather_than_propagated(self) -> None:
        # Decision 5 step 5: "a service that fails fast on the first refused
        # connection would crash-loop for the first seconds of every deploy".
        # Raising TimeoutError rather than ConnectionRefusedError shows the
        # refusal was absorbed and the helper was waiting to try again.
        attempts = 0

        async def connect() -> str:
            nonlocal attempts
            attempts += 1
            raise _refused()

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await _connect(connect)

        # The first backoff is 0.5 s, so 50 ms is not long enough for a second
        # attempt: a helper that retried immediately would be hammering a
        # broker that is still starting.
        assert attempts == 1

    async def test_the_retry_loop_is_interruptible_by_the_startup_deadline(self) -> None:
        # Decision 5 step 5: the retry loop lives *under* the startup deadline
        # `run_service` puts around start(), which then exits 1. It must
        # therefore be interruptible by that deadline rather than retrying for
        # ever, no matter how many transient failures it has seen.
        attempts = 0

        async def connect() -> str:
            nonlocal attempts
            attempts += 1
            raise _refused()

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await _connect(connect, "store")

        assert attempts >= 1


# --------------------------------------------------------------------------
# Decision 6 / section 47.3: configuration polling
# --------------------------------------------------------------------------

_BASE_DOC: dict[str, Any] = {
    "config_version": 1,
    "window_seconds": 300,
    "bucket_seconds": 10,
    "hot_threshold": 1000,
    "cold_threshold": 800,
    "minimum_hot_ips": 16,
    "minimum_hot_ratio": 0.10,
    "allowed_lateness_seconds": 30,
    "state_retention_seconds": 600,
}


def _config(**overrides: Any) -> DetectionConfig:
    """The `DetectionConfig` a service has in force when polling starts."""
    document: dict[str, Any] = {**_BASE_DOC, **overrides}
    return DetectionConfig(**document)


def _write_config(path: Path, **overrides: Any) -> Path:
    path.write_text(json.dumps({**_BASE_DOC, **overrides}))
    return path


class _Applier:
    """Records each applied document; optionally blocks inside `apply`."""

    def __init__(self) -> None:
        self.applied: list[DetectionConfig] = []
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def __call__(self, config: DetectionConfig) -> None:
        self.applied.append(config)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()

    @property
    def versions(self) -> list[int]:
        return [config.config_version for config in self.applied]


class TestConfigPoller:
    """Decision 6: apply a document only if its config_version is strictly greater."""

    async def test_a_strictly_greater_version_is_applied(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path / "detection.json", config_version=2, hot_threshold=2000)
        applier = _Applier()
        poller = _make_poller(path, applier, _config(config_version=1))

        await _poll(poller)

        assert applier.versions == [2]
        current = _current(poller)
        assert current.config_version == 2
        assert current.hot_threshold == 2000

    async def test_successive_versions_are_each_applied_once(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path / "detection.json", config_version=1)
        applier = _Applier()
        poller = _make_poller(path, applier, _config(config_version=1))

        await _poll(poller)  # same version as the one in force: nothing to do
        _write_config(path, config_version=2)
        await _poll(poller)
        await _poll(poller)  # unchanged file: already in force
        _write_config(path, config_version=3)
        await _poll(poller)

        assert applier.versions == [2, 3]
        assert _version(poller) == 3

    async def test_the_version_already_in_force_is_ignored_silently(self, tmp_path: Path) -> None:
        # Same version, different content: an operator rolling back must
        # republish under a *new* version for the change to take effect.
        path = _write_config(tmp_path / "detection.json", config_version=1, hot_threshold=2000)
        applier = _Applier()
        poller = _make_poller(path, applier, _config(config_version=1))

        await _poll(poller)

        assert applier.versions == []
        current = _current(poller)
        assert current.config_version == 1
        assert current.hot_threshold == 1000

    async def test_a_lower_version_is_ignored_silently(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path / "detection.json", config_version=6, hot_threshold=2000)
        applier = _Applier()
        poller = _make_poller(path, applier, _config(config_version=7))

        await _poll(poller)

        assert applier.versions == []
        current = _current(poller)
        assert current.config_version == 7
        assert current.hot_threshold == 1000

    async def test_an_unparsable_document_leaves_the_previous_version_in_force(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "detection.json"
        path.write_text("{not json")
        applier = _Applier()
        poller = _make_poller(path, applier, _config(config_version=1))

        await _poll(poller)  # logged as config_rejected and ignored; never raised

        assert applier.versions == []
        current = _current(poller)
        assert current.config_version == 1
        assert current.hot_threshold == 1000

    async def test_an_invalid_document_leaves_the_previous_version_in_force(
        self, tmp_path: Path
    ) -> None:
        # Parses, but violates section 34: cold_threshold above hot_threshold.
        path = _write_config(
            tmp_path / "detection.json",
            config_version=2,
            hot_threshold=100,
            cold_threshold=200,
        )
        applier = _Applier()
        poller = _make_poller(path, applier, _config(config_version=1))

        await _poll(poller)

        assert applier.versions == []
        assert _version(poller) == 1

    async def test_a_missing_document_leaves_the_previous_version_in_force(
        self, tmp_path: Path
    ) -> None:
        path = _write_config(tmp_path / "detection.json", config_version=5)
        applier = _Applier()
        poller = _make_poller(path, applier, _config(config_version=4))
        await _poll(poller)

        path.unlink()
        await _poll(poller)

        assert applier.versions == [5]
        assert _version(poller) == 5

    async def test_a_rejected_document_does_not_block_a_later_good_one(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "detection.json"
        path.write_text("{not json")
        applier = _Applier()
        poller = _make_poller(path, applier, _config(config_version=1))
        await _poll(poller)

        _write_config(path, config_version=2, hot_threshold=2000)
        await _poll(poller)

        assert applier.versions == [2]
        assert _version(poller) == 2

    async def test_version_becomes_current_only_after_the_apply_completes(
        self, tmp_path: Path
    ) -> None:
        # "A new version becomes visible in emitted events and read responses
        # only after the re-evaluation it triggers has been applied."
        path = _write_config(tmp_path / "detection.json", config_version=2, hot_threshold=2000)
        applier = _Applier()
        applier.gate = asyncio.Event()
        poller = _make_poller(path, applier, _config(config_version=1))

        poll = asyncio.create_task(_poll(poller))
        await asyncio.wait_for(applier.entered.wait(), timeout=1.0)

        assert applier.versions == [2]
        assert _version(poller) == 1, "version must not be in force mid-apply"

        applier.gate.set()
        await poll

        assert _version(poller) == 2
