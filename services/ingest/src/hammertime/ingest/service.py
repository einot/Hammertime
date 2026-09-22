"""Composition root: the ingest service the shared runner drives.

Spec: section 47

ADR-0009 decision 3. `build_service` wraps `app.py::create_app` -- the
injection seam ingest already had -- rather than duplicating it, so one
process (`__main__.py`) and an in-process cross-service test
(`docs/spec/integration-scenarios.md`) build the identical object graph and
differ only in what they inject.

The division of labour with `hammertime.core.runtime` follows decision 3's
reading of `Service.run()`: the runner owns the process (signals, deadlines,
exit codes), the service owns its own server. That is why `uvicorn` is a
dependency of this service and not of core.
"""

import asyncio
import contextlib
import dataclasses
from collections.abc import Iterator, Mapping
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from hammertime.bus.interface import MessageBus
from hammertime.bus.nats import bus_endpoints
from hammertime.core.config.loader import load as load_detection_config
from hammertime.core.config.models import DetectionConfig
from hammertime.core.runtime import ConfigPoller, Readiness
from hammertime.core.telemetry.logging import get_logger
from hammertime.core.time.clock import Clock, SystemClock
from hammertime.ingest.app import IngestState, create_app
from hammertime.ingest.auth.agents import (
    AgentRegistry,
    load_agent_registry_file,
    read_agent_token_key,
)
from hammertime.ingest.config import IngestSettings
from hammertime.ingest.dedup.service import DedupService
from hammertime.ingest.publisher import ObservationPublisher
from hammertime.ingest.ratelimit import RateLimiter
from hammertime.store.interface import DedupStore
from hammertime.store.memory import MemoryDedupStore

#: ADR-0009 decision 1: the fixed identifier, not a configurable name.
SERVICE_NAME = "ingest"


class _RunnerControlledServer(uvicorn.Server):
    """A uvicorn server whose process lifecycle belongs to `run_service`.

    `uvicorn.Server.serve()` normally installs its own `SIGTERM`/`SIGINT`
    handlers with `signal.signal`, which would displace the handlers
    `run_service` installed on the loop (ADR-0009 decision 5 step 4) and
    leave the drain, the shutdown deadline and the exit code to uvicorn
    instead. Shutdown is requested through `should_exit` by `stop()`.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class IngestService:
    """Ingest as ADR-0009 decision 3's `Service`.

    `start()` runs the FastAPI application's lifespan -- which *is* ingest's
    readiness (detection config and registry loaded, dedup store reachable,
    producer connected) -- so `app` is usable through
    `httpx.ASGITransport` the moment `start()` returns, with or without a
    socket ever being opened. `run()` then serves that same application over
    HTTP and polls the detection configuration until `stop()`.
    """

    def __init__(
        self,
        settings: IngestSettings,
        app: FastAPI,
        detection_config: DetectionConfig,
        *,
        poll_interval_s: float | None = None,
    ) -> None:
        self.name = SERVICE_NAME
        self.app = app
        self._settings = settings
        self._log = get_logger(SERVICE_NAME)
        self._poller = ConfigPoller(
            settings.detection_config_path,
            detection_config,
            apply=self._apply_config,
            poll_interval_s=(
                settings.config_poll_interval_s if poll_interval_s is None else poll_interval_s
            ),
            logger=self._log,
        )
        self._lifespan: AsyncExitStack | None = None
        self._server: _RunnerControlledServer | None = None
        self._stop_requested = False
        self._serving = False

    # --- Service protocol ---------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._readiness.ready

    async def start(self) -> None:
        """Run the application's lifespan; returns once ingest is ready."""
        if self._lifespan is not None:
            return
        lifespan = AsyncExitStack()
        await lifespan.enter_async_context(self.app.router.lifespan_context(self.app))
        self._lifespan = lifespan

    async def run(self) -> None:
        """Serve HTTP and poll the configuration until `stop()`; returns after the drain."""
        if self._stop_requested:
            # stop() ran before run() got scheduled: there is nothing left
            # to serve, and the lifespan has already been unwound.
            return
        server = _RunnerControlledServer(
            uvicorn.Config(
                self.app,
                host=self._settings.host,
                port=self._settings.port,
                # uvicorn defaults to proxy_headers=True, which installs its
                # own ProxyHeadersMiddleware and can rewrite scope["client"]
                # from X-Forwarded-For/X-Real-IP for any peer in
                # forwarded_allow_ips (itself 127.0.0.1,::1 by default, or *
                # if a deployment sets FORWARDED_ALLOW_IPS). That would let
                # uvicorn override the "trusted" client address behind
                # auth/middleware.py's back even when
                # HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS=0 -- two independent,
                # potentially disagreeing layers deciding whether to trust
                # X-Forwarded-For. Interpreting XFF is solely
                # middleware.py's trusted_proxy_hops logic (spec section
                # 36.5), so uvicorn's own rewriting stays off.
                proxy_headers=False,
                # start() already ran the lifespan (decision 3): uvicorn must
                # not run it a second time.
                lifespan="off",
                # Leave configure_logging's root handler in place instead of
                # replacing it with uvicorn's own dictConfig.
                log_config=None,
            )
        )
        self._server = server
        self._serving = True
        poll_task = asyncio.create_task(self._poller.run(), name="ingest-config-poll")
        try:
            await server.serve()
        finally:
            self._serving = False
            self._poller.stop()
            try:
                await poll_task
            except Exception:
                # The poller handles a rejected document itself; anything
                # escaping it must not mask why serve() returned.
                self._log.error("config_poll_failed", exc_info=True)
            await self._exit_lifespan()

    async def stop(self) -> None:
        """Request shutdown; idempotent, and safe before `start()`."""
        self._stop_requested = True
        self._readiness.mark_stopping()
        self._poller.stop()
        if self._server is not None:
            # uvicorn stops accepting connections, lets in-flight requests
            # finish, and returns from serve(); run()'s `finally` then
            # unwinds the lifespan, flushing and closing the producer and
            # the store (decision 7).
            self._server.should_exit = True
        if not self._serving:
            await self._exit_lifespan()

    # --- decision 3's per-service additions ---------------------------------

    async def reload_config(self) -> DetectionConfig:
        """Poll `HAMMERTIME_CONFIG_PATH` once now; return the config in force."""
        return await self._poller.poll_once()

    def startup_fields(self) -> Mapping[str, object]:
        """Decision 5 step 3's `starting` record, with no credential in it.

        The bus servers are named as `bus_endpoints` -- scheme and host per
        URL, userinfo dropped -- and never as `bus_brokers`: a NATS URL may
        carry `user:password@` or `token@` (ADR-0013 decision 3 as amended
        by Amendment 2, ruling 3; ADR-0009 A7's `bus_brokers` row is
        superseded).
        """
        fields: dict[str, object] = {
            "bus_kind": self._settings.bus_kind,
            "bus_endpoints": bus_endpoints(self._settings.bus_brokers),
            "store_kind": self._settings.store_kind,
            "config_path": str(self._settings.detection_config_path),
            "config_version": self._poller.current.config_version,
            "bind": f"{self._settings.host}:{self._settings.port}",
        }
        if self._settings.store_kind == "redis":
            # HAMMERTIME_REDIS_URL may embed a password: host and db only.
            url = urlsplit(self._settings.redis_url)
            fields["store_endpoint"] = f"{url.hostname}:{url.port}{url.path}"
        return fields

    # --- internals ----------------------------------------------------------

    @property
    def _readiness(self) -> Readiness:
        return self.app.state.readiness  # type: ignore[no-any-return]

    async def _apply_config(self, config: DetectionConfig) -> None:
        """Re-point the request pipeline at a newer detection configuration.

        Ingest reads three values out of the document -- `bucket_seconds`
        (window alignment), `allowed_lateness_seconds` (the dedup TTL,
        ADR-0003) and `config_version` (stamped onto every published
        envelope) -- so applying one is rebuilding the two singletons that
        hold them, around the store and producer already connected. It runs
        before the poller makes the new version current, so the version is
        never visible on an event published under the old thresholds (spec
        section 47.3).
        """
        state: IngestState = self.app.state.ingest
        self.app.state.ingest = dataclasses.replace(
            state,
            bucket_seconds=config.bucket_seconds,
            dedup=DedupService(
                state.dedup_store,
                allowed_lateness_seconds=config.allowed_lateness_seconds,
            ),
            publisher=ObservationPublisher(state.producer, config_version=config.config_version),
        )

    async def _exit_lifespan(self) -> None:
        lifespan, self._lifespan = self._lifespan, None
        if lifespan is not None:
            await lifespan.aclose()


def build_service(
    settings: IngestSettings,
    *,
    bus: MessageBus | None = None,
    clock: Clock | None = None,
    dedup_store: DedupStore | None = None,
    agent_registry: AgentRegistry | None = None,
) -> IngestService:
    """Build ingest from `settings`, with every transport injectable.

    Called by `__main__.py` with nothing but `settings`, and by the
    cross-service tests with a shared `InMemoryBus`, a `ManualClock`, an
    in-memory dedup store and a test registry. An override left `None`
    falls back to what `settings` says (`bus_kind`, `store_kind`), which is
    exactly `create_app`'s own rule.

    The detection configuration and the agent registry are loaded *here*,
    before `IngestService` exists and therefore before any connection is
    made, so that a malformed one is a `ConfigurationError` out of the
    factory -- which `run_service` reports as `config_invalid` and exit 2
    (ADR-0009 decisions 2 and 8) -- instead of a traceback from inside the
    lifespan, which would be exit 1.
    """
    resolved_clock = SystemClock() if clock is None else clock
    detection_config = load_detection_config(settings.detection_config_path)
    registry = (
        agent_registry
        if agent_registry is not None
        # The deployment key is read by the registry loader, never held on
        # settings: settings are logged at startup (ADR-0006, decision 2).
        else load_agent_registry_file(
            settings.agents_path,
            key=read_agent_token_key(),
            clock=resolved_clock,
        )
    )
    resolved_store = dedup_store
    if resolved_store is None and settings.store_kind == "memory":
        # Built here rather than left to the lifespan so it shares the
        # injected clock; the Redis store stays the lifespan's, which is
        # where its connection is retried (decision 5 step 5).
        resolved_store = MemoryDedupStore(resolved_clock)

    app = create_app(
        settings,
        agent_registry=registry,
        # Every limiter is built on the injected clock, so a ManualClock
        # test controls refill instead of racing the wall clock.
        rate_limiter=RateLimiter(resolved_clock),
        auth_failure_source_limiter=RateLimiter(resolved_clock),
        auth_failure_agent_limiter=RateLimiter(resolved_clock),
        observation_limiter=RateLimiter(resolved_clock),
        dedup_store=resolved_store,
        bus=bus,
    )
    return IngestService(settings, app, detection_config)
