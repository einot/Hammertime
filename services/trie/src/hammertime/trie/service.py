"""Composition root: the trie service the shared runner drives.

Spec: section 33, section 47; ADR-0017 decisions 4, 10 and 13, ADR-0009
decision 3.

`build_service(settings, ...)` is the one place the object graph is
assembled -- bus, `TrieState`, `TrieMetrics`, `TrieWorker`, the config poller
and the FastAPI app -- and every transport is injectable, so `__main__.py`
and an in-process cross-service test build the identical graph.

`start()` connects the bus it built (under `connect_with_retry`), awaits the
worker's replay up to the log end read when the replay began, records the
time spent in `start()` as `trie_recovery_seconds`, and only then marks the
service ready (decision 4). `run()` serves the app with uvicorn beside
`worker.run()` and `poller.run()`; `stop()` marks readiness `stopping`, stops
the poller and the server and awaits `worker.stop()`. The bus is closed by
`run()`'s teardown, after the worker has stopped (decision 13).

The configuration version gate lives in `ConfigPoller.poll_once()` and
nowhere else; the service hands the poller `worker.apply_config`, which
adopts unconditionally (decision 10).
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping

import uvicorn
from fastapi import FastAPI
from hammertime.bus.interface import MessageBus
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.nats import TRANSIENT_ERRORS, NatsBus, bus_endpoints
from hammertime.core.config.loader import load as load_detection_config
from hammertime.core.config.models import DetectionConfig
from hammertime.core.runtime import ConfigPoller, Readiness, connect_with_retry
from hammertime.core.telemetry.logging import get_logger
from hammertime.core.time.clock import Clock
from hammertime.trie.config import TrieSettings
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.query.app import create_app
from hammertime.trie.state import TrieState
from hammertime.trie.worker import TrieWorker

#: ADR-0009 decision 1: the fixed identifier, not a configurable name.
SERVICE_NAME = "trie"


class _RunnerControlledServer(uvicorn.Server):
    """A uvicorn server whose process lifecycle belongs to `run_service`.

    `uvicorn.Server.serve()` normally installs its own `SIGTERM`/`SIGINT`
    handlers, which would displace the ones `run_service` installed (ADR-0009
    decision 5 step 4). Shutdown is requested through `should_exit` by
    `stop()`.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class TrieService:
    """The trie as ADR-0009 decision 3's `Service` (and `DescribesStartup`)."""

    def __init__(
        self,
        settings: TrieSettings,
        worker: TrieWorker,
        detection_config: DetectionConfig,
        *,
        transport: NatsBus | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = SERVICE_NAME
        self._settings = settings
        self._worker = worker
        self._transport = transport
        # `monotonic` is not called before `start()`: its first reading is
        # `start()`'s own entry reading (decision 13).
        self._sleep = sleep
        self._monotonic = monotonic
        self._readiness = Readiness()
        self._log = get_logger(SERVICE_NAME)
        self._poller = ConfigPoller(
            settings.detection_config_path,
            detection_config,
            apply=worker.apply_config,
            poll_interval_s=settings.config_poll_interval_s,
            logger=self._log,
        )
        self.app: FastAPI = create_app(self._readiness)
        self._server: _RunnerControlledServer | None = None
        self._stopping = asyncio.Event()

    # --- reads ---------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._readiness.ready

    @property
    def state(self) -> TrieState:
        return self._worker.state

    @property
    def metrics(self) -> TrieMetrics:
        return self._worker.metrics

    # --- Service protocol ---------------------------------------------------

    async def start(self) -> None:
        """Connect the bus, replay to the startup log end, then become ready (decision 4).

        `trie_recovery_seconds` is the reading taken when this ends minus the
        one taken on entry, bus connection included (ADR-0009 decision 5 step
        6). Ready is not marked if `stop()` has begun or the worker is not
        caught up.
        """
        entered = self._monotonic()
        if self._transport is not None:
            await connect_with_retry(
                "bus",
                self._transport.start,
                transient=TRANSIENT_ERRORS,
                sleep=self._sleep,
                monotonic=self._monotonic,
            )
        await self._worker.start()
        self._worker.metrics.set("trie_recovery_seconds", self._monotonic() - entered)
        if self._stopping.is_set() or not self._worker.caught_up:
            return
        self._readiness.mark_ready()

    async def run(self) -> None:
        """Serve the app, follow the log and poll the configuration until `stop()`.

        When the first task completes: collect the exceptions of the tasks
        that completed, call `stop()` inside a `try` that logs `stop_failed`
        and adds its exception, gather the rest, close the transport this
        service built, and raise the first exception collected (ADR-0013
        decision 8 as amended by Amendment 4 ruling R7).
        """
        if self._stopping.is_set():
            return
        server = _RunnerControlledServer(
            uvicorn.Config(
                self.app,
                host=self._settings.host,
                port=self._settings.port,
                # Nothing here depends on uvicorn driving the lifespan.
                lifespan="off",
                # Leave configure_logging's root handler in place.
                log_config=None,
            )
        )
        self._server = server
        tasks = [
            asyncio.create_task(server.serve(), name="trie-http"),
            asyncio.create_task(self._worker.run(), name="trie-consume"),
            asyncio.create_task(self._poller.run(), name="trie-config-poll"),
        ]
        failures: list[BaseException] = []
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            failures.extend(
                error for error in (task.exception() for task in done) if error is not None
            )
            try:
                await self.stop()
            except Exception as exc:
                self._log.warning("stop_failed", error=str(exc))
                failures.append(exc)
            for result in await asyncio.gather(*pending, return_exceptions=True):
                if isinstance(result, BaseException):
                    failures.append(result)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            await self._close_transport()
        if failures:
            raise failures[0]

    async def stop(self) -> None:
        """Request shutdown; idempotent, and safe before `start()` (decision 13)."""
        self._stopping.set()
        self._readiness.mark_stopping()
        self._poller.stop()
        if self._server is not None:
            self._server.should_exit = True
        await self._worker.stop()

    # --- ADR-0009 decision 3's per-service additions ------------------------

    async def reload_config(self) -> DetectionConfig:
        """Poll `HAMMERTIME_CONFIG_PATH` once now; return the config in force."""
        return await self._poller.poll_once()

    def startup_fields(self) -> Mapping[str, object]:
        """ADR-0009 decision 5 step 3's `starting` record, with no credential in it.

        `bus_endpoints` stands in for `HAMMERTIME_BUS_BROKERS`, which may carry
        userinfo (ADR-0013 Amendment 2); the value itself never appears.
        """
        return {
            "bus_kind": self._settings.bus_kind,
            "bus_endpoints": bus_endpoints(self._settings.bus_brokers),
            "config_path": str(self._settings.detection_config_path),
            "config_version": self._poller.current.config_version,
            "bind": f"{self._settings.host}:{self._settings.port}",
            "families": sorted(family.value for family in self._settings.families),
        }

    # --- internals ----------------------------------------------------------

    async def _close_transport(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()


def build_service(
    settings: TrieSettings,
    *,
    bus: MessageBus | None = None,
    clock: Clock | None = None,
) -> TrieService:
    """Build the trie service from `settings`, with the bus injectable.

    The detection configuration is loaded first, so a malformed one is a
    `ConfigurationError` out of the factory -- `config_invalid`, exit 2 --
    before any connection is made. `clock` is accepted for ADR-0009 decision
    3's signature and is unused until the snapshot epic stamps its files.
    """
    del clock
    detection_config = load_detection_config(settings.detection_config_path)

    transport: NatsBus | None = None
    resolved_bus: MessageBus
    if bus is not None:
        resolved_bus = bus
    elif settings.bus_kind == "nats":
        transport = NatsBus(settings.bus_brokers)
        resolved_bus = transport
    else:
        resolved_bus = InMemoryBus()

    state = TrieState(families=settings.families, config=detection_config)
    worker = TrieWorker(bus=resolved_bus, state=state, metrics=TrieMetrics())
    return TrieService(settings, worker, detection_config, transport=transport)
