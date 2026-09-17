"""FastAPI application factory and lifespan (auth, rate limit, dedup, bus).

Spec: section 4, section 47

The lifespan loads ingest settings and the detection config once at
startup (not per request), and constructs every other process-lifetime
singleton the request pipeline needs (issue #32): the `AgentRegistry` and
the `require_agent` dependency built from it, the `RateLimiter`, the
`DedupStore` (memory or Redis per `IngestSettings.store_kind`), and the bus
`Producer` (memory or Kafka per `IngestSettings.bus_kind`) wrapped in an
`ObservationPublisher`. Kafka's producer needs an explicit `start()`/
`stop()`; the in-memory one needs neither.

This lifespan *is* ingest's readiness (ADR-0009 decision 4): the app is
ready exactly from the moment it has completed to the moment it starts
unwinding, which is what `app.state.readiness` records and what
`/readyz` -- and the 503 gate on the ingestion endpoint -- report. The two
connections it makes (Redis, Kafka) are retried with backoff under the
startup deadline, because `depends_on` in the compose file orders
container start but not broker readiness (decision 5 step 5). A rejected
credential is not that kind of failure and fails the start at once.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiokafka.errors import KafkaConnectionError
from fastapi import FastAPI, Request, Response
from hammertime.bus.interface import Producer
from hammertime.bus.kafka import KafkaProducer
from hammertime.bus.memory import InMemoryBus
from hammertime.core.config.loader import load as load_detection_config
from hammertime.core.runtime import (
    Readiness,
    ServiceNotReady,
    connect_with_retry,
    not_ready_response,
)
from hammertime.core.time.clock import SystemClock
from hammertime.ingest.api.routes import router
from hammertime.ingest.auth.agents import (
    AgentRegistry,
    load_agent_registry_file,
    read_agent_token_key,
)
from hammertime.ingest.auth.middleware import require_agent
from hammertime.ingest.config import IngestSettings, load_settings
from hammertime.ingest.dedup.service import DedupService
from hammertime.ingest.publisher import ObservationPublisher
from hammertime.ingest.ratelimit import RateLimiter
from hammertime.ingest.validation.schema import ObservationSchemaValidator
from hammertime.store.interface import DedupStore
from hammertime.store.memory import MemoryDedupStore
from hammertime.store.redis import RedisDedupStore
from redis.asyncio import Redis
from redis.exceptions import AuthenticationError as RedisAuthenticationError
from redis.exceptions import AuthorizationError as RedisAuthorizationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

#: Failures that mean "the dependency is not up *yet*" and are worth
#: retrying under the startup deadline (ADR-0009 decision 5). A failure of
#: any other type -- a bad URL, a protocol error -- is non-transient and
#: fails the start immediately rather than being retried for 60 seconds.
_TRANSIENT_STORE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    RedisConnectionError,
    RedisTimeoutError,
)

#: The bus needs no counterpart to `_PERMANENT_STORE_ERRORS` below:
#: aiokafka raises its authentication failures (`AuthenticationFailedError`,
#: `UnsupportedSaslMechanismError`, ...) as plain `KafkaError`s, none of
#: which is or subclasses `KafkaConnectionError`.
_TRANSIENT_BUS_ERRORS: tuple[type[BaseException], ...] = (OSError, KafkaConnectionError)

#: Type alone is not enough on the store side: redis-py reports a rejected
#: credential as `AuthenticationError`/`AuthorizationError`, both
#: *subclasses* of `redis.exceptions.ConnectionError`, so listing
#: `RedisConnectionError` above as transient sweeps them in too.
#: `connect_with_retry` classifies strictly by `isinstance` against its
#: `transient` tuple and offers no exclusions (ADR-0009 Amendment 1 item
#: A1), so the distinction is drawn here instead, in the callable passed to
#: it: `ping_store` re-raises these as `DependencyAuthenticationError`,
#: which matches no transient tuple and so propagates on the first attempt.
#: Everything else redis-py raises keeps its existing classification -- a
#: store that is still starting is still retried on the usual schedule.
_PERMANENT_STORE_ERRORS: tuple[type[BaseException], ...] = (
    RedisAuthenticationError,
    RedisAuthorizationError,
)


class DependencyAuthenticationError(Exception):
    """A dependency rejected the credentials this process was configured with.

    Raised in place of the driver's own authentication error when that error
    is indistinguishable by type from "not up yet" (see
    `_PERMANENT_STORE_ERRORS`). Presenting a wrong password once a second
    for the whole startup deadline helps no operator, so this is not
    retried; the driver's own exception is kept as the `__cause__`.
    """


@dataclass(frozen=True, slots=True)
class IngestState:
    """Process-lifetime singletons the API layer reads on every request."""

    settings: IngestSettings
    schema_validator: ObservationSchemaValidator
    bucket_seconds: int
    registry: AgentRegistry
    #: Built once via `require_agent(registry, ...)` in the lifespan below,
    #: not rebuilt per request -- `api/routes.py` calls this directly as the
    #: body of its own `Depends` dependency.
    authenticate: Callable[[Request], Awaitable[str]]
    rate_limiter: RateLimiter
    #: ADR-0007 (spec section 36.5): failed-auth budgets, keyed by source
    #: address prefix and by attempted `X-Agent-Id` respectively. Charged
    #: only inside `require_agent`'s failure path, never here directly.
    auth_failure_source_limiter: RateLimiter
    auth_failure_agent_limiter: RateLimiter
    #: ADR-0008 (spec section 36.6): per-agent budget denominated in
    #: distinct observed IPs, not requests. Charged in `api/routes.py`.
    observation_limiter: RateLimiter
    dedup: DedupService
    publisher: ObservationPublisher
    #: The store and producer `dedup`/`publisher` above were built on.
    #: Held so a configuration reload (ADR-0009 decision 6) can rebuild
    #: those two -- whose settings are `allowed_lateness_seconds` and
    #: `config_version` -- around the *same* connections instead of
    #: reconnecting, which would drop the dedup window on the floor.
    dedup_store: DedupStore
    producer: Producer


def create_app(
    settings: IngestSettings | None = None,
    *,
    agent_registry: AgentRegistry | None = None,
    rate_limiter: RateLimiter | None = None,
    auth_failure_source_limiter: RateLimiter | None = None,
    auth_failure_agent_limiter: RateLimiter | None = None,
    observation_limiter: RateLimiter | None = None,
    dedup_store: DedupStore | None = None,
    bus: InMemoryBus | None = None,
    agent_slot_salt: bytes | None = None,
    agent_slot_count: int | None = None,
) -> FastAPI:
    """Build the ingest FastAPI application.

    `settings` is normally left `None` so the lifespan loads it (and the
    detection config it references) from the environment at startup; tests
    may pass an explicit `IngestSettings` to avoid depending on process
    environment variables. The keyword-only overrides let a test substitute
    its own `AgentRegistry` (skipping `HAMMERTIME_INGEST_AGENTS_PATH` disk
    access), `RateLimiter`s/`DedupStore` (e.g. built on a shared
    `ManualClock` for deterministic TTL/refill assertions), and `InMemoryBus`
    (so the test keeps a reference to read a topic's log back afterwards --
    `bus.producer()` is what actually gets wired into `ObservationPublisher`,
    the same relationship `InMemoryBus.consumer(group_id)` has elsewhere in
    this repo). Any override left `None` falls back to the normal
    settings/environment-driven construction below.

    `agent_slot_salt`/`agent_slot_count` are forwarded to `require_agent`
    (ADR-0007 Decision 8) only when non-`None`; they exist purely so a test
    can pin the attempted-id -> slot mapping deterministically and are never
    driven by settings/environment -- production always gets `require_agent`'s
    own defaults (a fresh random per-process salt and the default slot count).
    """
    # "starting" until the lifespan completes; ADR-0009 decision 4.
    readiness = Readiness()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings if settings is not None else load_settings()
        detection_config = load_detection_config(resolved_settings.detection_config_path)
        registry = (
            agent_registry
            if agent_registry is not None
            # HAMMERTIME_INGEST_AGENT_TOKEN_KEY is read directly here, not via
            # IngestSettings: that dataclass's repr gets logged, and this
            # value is a secret (ADR-0006, spec section 36.1).
            else load_agent_registry_file(
                resolved_settings.agents_path,
                key=read_agent_token_key(),
                clock=SystemClock(),
            )
        )
        resolved_rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()
        resolved_auth_failure_source_limiter = (
            auth_failure_source_limiter
            if auth_failure_source_limiter is not None
            else RateLimiter()
        )
        resolved_auth_failure_agent_limiter = (
            auth_failure_agent_limiter if auth_failure_agent_limiter is not None else RateLimiter()
        )
        resolved_observation_limiter = (
            observation_limiter if observation_limiter is not None else RateLimiter()
        )

        redis_client: Redis | None = None
        resolved_dedup_store: DedupStore
        if dedup_store is not None:
            resolved_dedup_store = dedup_store
        elif resolved_settings.store_kind == "redis":
            redis_client = Redis.from_url(resolved_settings.redis_url)
            client = redis_client

            async def ping_store() -> None:
                # `Redis.from_url` connects lazily, so this round trip is
                # what "dedup store reachable" in decision 4 actually means.
                try:
                    await client.ping()
                except _PERMANENT_STORE_ERRORS as exc:
                    # Never include the URL: it carries the password.
                    raise DependencyAuthenticationError(
                        f"store rejected the configured credentials: {exc}"
                    ) from exc

            await connect_with_retry("store", ping_store, transient=_TRANSIENT_STORE_ERRORS)
            resolved_dedup_store = RedisDedupStore(redis_client)
        else:
            resolved_dedup_store = MemoryDedupStore()

        producer: Producer
        kafka_producer: KafkaProducer | None = None
        if bus is not None:
            producer = bus.producer()
        elif resolved_settings.bus_kind == "kafka":
            kafka_producer = KafkaProducer(bootstrap_servers=resolved_settings.bus_brokers)
            await connect_with_retry("bus", kafka_producer.start, transient=_TRANSIENT_BUS_ERRORS)
            producer = kafka_producer
        else:
            producer = InMemoryBus().producer()

        # `agent_slot_salt` accepts `None` in `require_agent` itself (meaning
        # "draw a fresh random salt"), so it can always be forwarded as-is.
        # `agent_slot_count` has a non-`None` default there, so it is only
        # passed through when this caller actually overrides it -- otherwise
        # `require_agent`'s own default applies.
        authenticate = (
            require_agent(
                registry,
                source_limiter=resolved_auth_failure_source_limiter,
                agent_limiter=resolved_auth_failure_agent_limiter,
                auth_failure_rate_per_min=resolved_settings.auth_failure_rate_per_min,
                auth_failure_burst=resolved_settings.auth_failure_burst,
                auth_failure_agent_rate_per_min=(resolved_settings.auth_failure_agent_rate_per_min),
                auth_failure_agent_burst=resolved_settings.auth_failure_agent_burst,
                trusted_proxy_hops=resolved_settings.trusted_proxy_hops,
                agent_slot_salt=agent_slot_salt,
                agent_slot_count=agent_slot_count,
            )
            if agent_slot_count is not None
            else require_agent(
                registry,
                source_limiter=resolved_auth_failure_source_limiter,
                agent_limiter=resolved_auth_failure_agent_limiter,
                auth_failure_rate_per_min=resolved_settings.auth_failure_rate_per_min,
                auth_failure_burst=resolved_settings.auth_failure_burst,
                auth_failure_agent_rate_per_min=(resolved_settings.auth_failure_agent_rate_per_min),
                auth_failure_agent_burst=resolved_settings.auth_failure_agent_burst,
                trusted_proxy_hops=resolved_settings.trusted_proxy_hops,
                agent_slot_salt=agent_slot_salt,
            )
        )

        app.state.ingest = IngestState(
            settings=resolved_settings,
            schema_validator=ObservationSchemaValidator.from_file(),
            bucket_seconds=detection_config.bucket_seconds,
            registry=registry,
            authenticate=authenticate,
            rate_limiter=resolved_rate_limiter,
            auth_failure_source_limiter=resolved_auth_failure_source_limiter,
            auth_failure_agent_limiter=resolved_auth_failure_agent_limiter,
            observation_limiter=resolved_observation_limiter,
            dedup=DedupService(
                resolved_dedup_store,
                allowed_lateness_seconds=detection_config.allowed_lateness_seconds,
            ),
            publisher=ObservationPublisher(
                producer, config_version=detection_config.config_version
            ),
            dedup_store=resolved_dedup_store,
            producer=producer,
        )
        readiness.mark_ready()
        try:
            yield
        finally:
            # Stop answering 200 on /readyz (and start answering 503 on the
            # ingestion endpoint) *before* the connections those answers
            # depend on are torn down.
            readiness.mark_stopping()
            if kafka_producer is not None:
                await kafka_producer.stop()
            if redis_client is not None:
                await redis_client.aclose()

    async def service_not_ready(request: Request, exc: Exception) -> Response:
        """Render `ServiceNotReady` as decision 4's 503, not FastAPI's `detail` shape."""
        response = not_ready_response(_readiness_of(request.app))
        return Response(
            content=response.body,
            status_code=response.status_code,
            headers={"content-type": response.media_type},
        )

    app = FastAPI(title="hammertime-ingest", lifespan=lifespan)
    app.state.readiness = readiness
    app.add_exception_handler(ServiceNotReady, service_not_ready)
    app.include_router(router)
    return app


def _readiness_of(app: FastAPI) -> Readiness:
    # Set by `create_app` below, the same way `IngestState` is set by its
    # lifespan and read back through `routes.py::_get_ingest_state`.
    return app.state.readiness  # type: ignore[no-any-return]
