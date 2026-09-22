"""Composition root: the aggregator service the shared runner drives.

Spec: section 47

ADR-0009 decision 3. `build_service(settings, ...)` is the one place the
object graph is assembled -- bus, state store, clock, worker -- and every
transport is injectable, so `__main__.py` and an in-process cross-service
test (`docs/spec/integration-scenarios.md`) build the identical graph and
differ only in what they inject.

The division of labour with `hammertime.core.runtime` follows decision 3's
reading of `Service.run()`: the runner owns the process (signals, deadlines,
exit codes), the service owns its own server. The aggregator has no web
framework, so its `/healthz`, `/readyz` and `/metrics` come from core's
pure-ASGI admin app served by uvicorn (decision 4).

`run()` is the three periodic things this service does -- consume, sweep,
poll the configuration -- alongside that server. `run_maintenance()` and
`reload_config()` are the very coroutines those loops call, not test-only
paths (spec section 47.6). The configuration version gate lives in
`ConfigPoller.poll_once()` and nowhere else (ADR-0011 Amendment 3 item A12):
this service adds no check of its own, it merely hands the poller
`worker.apply_config` as its `apply` hook.

The bus is NATS JetStream (ADR-0013): `NatsBus` is started under
`connect_with_retry` with the bus package's own transient tuple, and closed
-- which `nak`s whatever was fetched but never handled -- only after the
worker's `stop()` has sent its final acknowledgement on the live consumer
(decision 8, Amendment 1 ruling T8).
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from urllib.parse import urlsplit

import uvicorn
from hammertime.aggregator.config import AggregatorSettings
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.sharding.assignment import ShardHeldBySameMemberError
from hammertime.aggregator.worker import CONSUMER_GROUP, AggregatorWorker
from hammertime.bus.interface import MessageBus
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.nats import TRANSIENT_ERRORS, NatsBus, bus_endpoints
from hammertime.core.config.loader import load as load_detection_config
from hammertime.core.config.models import DetectionConfig
from hammertime.core.runtime import ConfigPoller, Readiness, connect_with_retry, create_admin_app
from hammertime.core.telemetry.logging import get_logger
from hammertime.core.time.clock import Clock, SystemClock
from hammertime.store.interface import ShardStateStore
from hammertime.store.memory import MemoryShardStateStore
from hammertime.store.redis import RedisShardStateStore
from redis.asyncio import Redis
from redis.exceptions import AuthenticationError as RedisAuthenticationError
from redis.exceptions import AuthorizationError as RedisAuthorizationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

#: ADR-0009 decision 1: the fixed identifier, not a configurable name.
SERVICE_NAME = "aggregator"

#: Spec section 47.2: connectivity is retried, a rejected credential is not.
_TRANSIENT_STORE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    RedisConnectionError,
    RedisTimeoutError,
)
#: redis-py reports a rejected credential as a *subclass* of its
#: `ConnectionError`, so the distinction is drawn in the connect callable,
#: exactly as `services/ingest/.../app.py` draws it (ADR-0009 A1).
_PERMANENT_STORE_ERRORS: tuple[type[BaseException], ...] = (
    RedisAuthenticationError,
    RedisAuthorizationError,
)


class DependencyAuthenticationError(Exception):
    """A dependency rejected the credentials this process was configured with.

    Raised in place of the driver's own authentication error when that error
    is indistinguishable by type from "not up yet". Presenting a wrong
    password once a second for the whole startup deadline helps no operator,
    so this is not retried; the driver's exception is kept as the `__cause__`.
    """


class _RunnerControlledServer(uvicorn.Server):
    """A uvicorn server whose process lifecycle belongs to `run_service`.

    `uvicorn.Server.serve()` normally installs its own `SIGTERM`/`SIGINT`
    handlers, which would displace the ones `run_service` installed on the
    loop (ADR-0009 decision 5 step 4) and leave the drain, the shutdown
    deadline and the exit code to uvicorn instead. Shutdown is requested
    through `should_exit` by `stop()`.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class AggregatorService:
    """The aggregator as ADR-0009 decision 3's `Service`."""

    def __init__(
        self,
        settings: AggregatorSettings,
        worker: AggregatorWorker,
        detection_config: DetectionConfig,
        *,
        transport: NatsBus | None = None,
        store_client: Redis | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = SERVICE_NAME
        self._settings = settings
        self._worker = worker
        self._transport = transport
        self._store_client = store_client
        # The seams every `connect_with_retry` this service makes is given,
        # so a test drives the startup backoff -- a shard-lease wait above
        # all -- without wall time (ADR-0013 Amendment 6, assumption 104).
        self._sleep = sleep
        self._monotonic = monotonic
        self._readiness = Readiness()
        self._log = get_logger(SERVICE_NAME)
        self._poller = ConfigPoller(
            settings.detection_config_path,
            detection_config,
            apply=self._worker.apply_config,
            poll_interval_s=settings.config_poll_interval_s,
            logger=self._log,
        )
        self._app = create_admin_app(self._readiness)
        self._server: _RunnerControlledServer | None = None
        self._stopping = asyncio.Event()

    # --- Service protocol ---------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._readiness.ready

    async def start(self) -> None:
        """Connect, claim shards, become ready (spec section 47.2).

        Readiness for the aggregator is "config loaded; consumer subscribed;
        shard claims held": the detection configuration was loaded in
        `build_service`, and `AggregatorWorker.start()` returns only once
        `subscribe()` has delivered the static assignment and every shard's
        lease is held (ADR-0013 decisions 3 and 7). A shard another member
        holds is `ShardOwnedElsewhereError` out of here -- not transient, so
        `run_service` logs `start_failed` and exits 1.

        A shard held by another *instance* of this member is the one refusal
        that is retried (decision 7 as amended by Amendment 6 ruling 2): the
        claim runs under `connect_with_retry` as the `shard_leases`
        dependency, so a predecessor that died without releasing is waited
        out on ADR-0009 A1's schedule -- each attempt a fresh `subscribe()`,
        each refusal a `dependency_unavailable` record -- and its shards are
        claimed the moment its leases lapse, within
        `HAMMERTIME_AGGREGATOR_LEASE_TTL_S`. A live twin never lets go: the
        startup deadline expires, the last `ShardHeldBySameMemberError` is
        re-raised, and the process exits 1 with `start_failed`.
        """
        if self._transport is not None:
            await connect_with_retry(
                "bus",
                self._transport.start,
                transient=TRANSIENT_ERRORS,
                sleep=self._sleep,
                monotonic=self._monotonic,
            )
        if self._store_client is not None:
            await connect_with_retry(
                "store",
                self._ping_store,
                transient=_TRANSIENT_STORE_ERRORS,
                sleep=self._sleep,
                monotonic=self._monotonic,
            )
        await connect_with_retry(
            "shard_leases",
            self._worker.start,
            transient=(ShardHeldBySameMemberError,),
            sleep=self._sleep,
            monotonic=self._monotonic,
        )
        self._readiness.mark_ready()

    async def run(self) -> None:
        """Serve the admin endpoints, consume, sweep and poll until `stop()`.

        Reports what stopped it (ADR-0013 decision 8 as amended by Amendment
        4 ruling R7; ADR-0009 decision 5 step 7): the exceptions of the
        tasks that completed are collected first, then `stop()` is called
        inside a `try` that logs `stop_failed` and appends its exception to
        the same list, then the remaining tasks are gathered, the clients
        closed and the first collected exception raised -- so `run_exited`
        names the failure that stopped the service, and a `stop()` failing
        on the same outage is reported only when nothing failed before it.
        """
        if self._stopping.is_set():
            # stop() ran before run() got scheduled: nothing left to serve.
            return
        server = _RunnerControlledServer(
            uvicorn.Config(
                self._app,
                host=self._settings.host,
                port=self._settings.port,
                # The admin app answers the lifespan protocol itself; nothing
                # here depends on uvicorn driving it.
                lifespan="off",
                # Leave configure_logging's root handler in place.
                log_config=None,
            )
        )
        self._server = server
        tasks = [
            asyncio.create_task(server.serve(), name="aggregator-admin"),
            asyncio.create_task(self._worker.run(), name="aggregator-consume"),
            asyncio.create_task(self._maintenance_loop(), name="aggregator-maintenance"),
            asyncio.create_task(self._poller.run(), name="aggregator-config-poll"),
        ]
        failures: list[BaseException] = []
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            failures.extend(
                error for error in (task.exception() for task in done) if error is not None
            )
            # Whichever of the four returned first, the rest come down with
            # it: a worker that stopped consuming is not a service that can
            # keep answering (decision 5 step 7). A `stop()` that fails must
            # not mask what stopped the service (ruling R7, assumption 71).
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
            await self._close_clients()
        if failures:
            raise failures[0]

    async def stop(self) -> None:
        """Request shutdown; idempotent, and safe before `start()`.

        ADR-0009 decision 7 / ADR-0013 decision 8: stop fetching, finish the
        in-flight message, flush the producer, acknowledge what was handled
        and release the shard leases -- in that order, so an acknowledgement
        never precedes the transitions it produced and never covers a
        message that was fetched and not yet handled. The bus is closed
        afterwards, by `run()`'s teardown, so the acknowledgement goes out on
        a live consumer. No state-store write is needed: the HOT set is
        always current.
        """
        self._stopping.set()
        self._readiness.mark_stopping()
        self._poller.stop()
        if self._server is not None:
            self._server.should_exit = True
        await self._worker.stop()

    # --- decision 3's per-service additions ---------------------------------

    async def run_maintenance(self) -> None:
        """One lease renewal and expiry/warm-up/retention pass -- what the periodic loop calls."""
        await self._worker.run_maintenance()

    async def reload_config(self) -> DetectionConfig:
        """Poll `HAMMERTIME_CONFIG_PATH` once now; return the config in force.

        The version gate, the `config_rejected` record and the
        `config_applied` record are the poller's; this service adds nothing
        to them and duplicates none of them (item A12).
        """
        return await self._poller.poll_once()

    def startup_fields(self) -> Mapping[str, object]:
        """Decision 5 step 3's `starting` record, with no credential in it.

        `bus_endpoints` stands in for the configured `HAMMERTIME_BUS_BROKERS`:
        a NATS URL may carry userinfo, and no record may carry it (ADR-0013
        decision 3 as amended by Amendment 2, ruling 3).

        `instance_id` stands beside `member_id` (Amendment 6 ruling 2(e)):
        it is half of the token this process's leases are held under, so an
        operator can match it against the `owner` another process reports in
        `shard_held_by_same_member` or `shard_lease_lost`.
        """
        fields: dict[str, object] = {
            "bus_kind": self._settings.bus_kind,
            "bus_endpoints": bus_endpoints(self._settings.bus_brokers),
            "store_kind": self._settings.store_kind,
            "config_path": str(self._settings.detection_config_path),
            "config_version": self._poller.current.config_version,
            "bind": f"{self._settings.host}:{self._settings.port}",
            "consumer_group": CONSUMER_GROUP,
            "member_id": self._settings.member_id,
            "instance_id": self._worker.instance_id,
            "shard_ids": sorted(self._settings.shard_ids),
        }
        if self._settings.store_kind == "redis":
            # HAMMERTIME_REDIS_URL may embed a password: host and db only.
            url = urlsplit(self._settings.redis_url)
            fields["store_endpoint"] = f"{url.hostname}:{url.port}{url.path}"
        return fields

    # --- internals ----------------------------------------------------------

    async def _ping_store(self) -> None:
        """`Redis.from_url` connects lazily; this round trip is "store reachable"."""
        client = self._store_client
        if client is None:  # pragma: no cover - only called when one exists
            return
        try:
            await client.ping()
        except _PERMANENT_STORE_ERRORS as exc:
            # Never include the URL: it carries the password.
            raise DependencyAuthenticationError(
                f"store rejected the configured credentials: {exc}"
            ) from exc

    async def _maintenance_loop(self) -> None:
        """Sleep `HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S`, renew and sweep, repeat."""
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), self._settings.maintenance_interval_s)
            except TimeoutError:
                pass
            else:
                return
            # A sweep that raises -- a state-store outage (ADR-0011
            # assumption 7) or a lost lease (ADR-0013 decision 7) -- takes
            # the process down through `run()`: transitions that cannot be
            # recorded, or a shard another member now owns, must not be
            # silently carried on with.
            await self.run_maintenance()

    async def _close_clients(self) -> None:
        # The bus first: closing it `nak`s what was fetched and never
        # handled, after `stop()` has acknowledged what was (decision 8).
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        client, self._store_client = self._store_client, None
        if client is not None:
            await client.aclose()


def build_service(
    settings: AggregatorSettings,
    *,
    bus: MessageBus | None = None,
    clock: Clock | None = None,
    state_store: ShardStateStore | None = None,
) -> AggregatorService:
    """Build the aggregator from `settings`, with every transport injectable.

    Called by `__main__.py` with nothing but `settings`, and by the
    cross-service tests with a shared `InMemoryBus`, a `ManualClock` and an
    in-memory shard state store. An override left `None` falls back to what
    `settings` says (`bus_kind`, `store_kind`).

    The detection configuration is loaded *here*, before any connection is
    made, so a malformed one is a `ConfigurationError` out of the factory --
    which `run_service` reports as `config_invalid` and exit 2 -- rather than
    a traceback from inside `start()`, which would be exit 1 (ADR-0009
    decisions 2 and 8). The worker's producer and consumer are taken from
    the bus here, before `start()`, which `NatsBus` permits (ADR-0013
    decision 3, Amendment 1 ruling C8).
    """
    resolved_clock = SystemClock() if clock is None else clock
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

    store_client: Redis | None = None
    resolved_store: ShardStateStore
    if state_store is not None:
        resolved_store = state_store
    elif settings.store_kind == "redis":
        store_client = Redis.from_url(settings.redis_url)
        resolved_store = RedisShardStateStore(store_client)
    else:
        resolved_store = MemoryShardStateStore()

    worker = AggregatorWorker(
        bus=resolved_bus,
        state_store=resolved_store,
        clock=resolved_clock,
        config=detection_config,
        metrics=AggregatorMetrics(),
        shard_ids=settings.shard_ids,
        max_tracked_ips=settings.max_tracked_ips,
        commit_interval_s=settings.commit_interval_s,
        reevaluation_batch=settings.reevaluation_batch,
        member_id=settings.member_id,
        lease_ttl_s=settings.lease_ttl_s,
    )
    return AggregatorService(
        settings,
        worker,
        detection_config,
        transport=transport,
        store_client=store_client,
    )
