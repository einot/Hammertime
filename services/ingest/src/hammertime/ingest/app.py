"""FastAPI application factory and lifespan (auth, rate limit, dedup, bus).

Spec: section 4

The lifespan loads ingest settings and the detection config once at
startup (not per request), and constructs every other process-lifetime
singleton the request pipeline needs (issue #32): the `AgentRegistry` and
the `require_agent` dependency built from it, the `RateLimiter`, the
`DedupStore` (memory or Redis per `IngestSettings.store_kind`), and the bus
`Producer` (memory or Kafka per `IngestSettings.bus_kind`) wrapped in an
`ObservationPublisher`. Kafka's producer needs an explicit `start()`/
`stop()`; the in-memory one needs neither.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from hammertime.bus.interface import Producer
from hammertime.bus.kafka import KafkaProducer
from hammertime.bus.memory import InMemoryBus
from hammertime.core.config.loader import load as load_detection_config
from hammertime.ingest.api.routes import router
from hammertime.ingest.auth.agents import AgentRegistry, load_agent_registry_file
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


@dataclass(frozen=True, slots=True)
class IngestState:
    """Process-lifetime singletons the API layer reads on every request."""

    settings: IngestSettings
    schema_validator: ObservationSchemaValidator
    bucket_seconds: int
    registry: AgentRegistry
    #: Built once via `require_agent(registry)` in the lifespan below, not
    #: rebuilt per request -- `api/routes.py` calls this directly as the
    #: body of its own `Depends` dependency.
    authenticate: Callable[[Request], str]
    rate_limiter: RateLimiter
    dedup: DedupService
    publisher: ObservationPublisher


def create_app(
    settings: IngestSettings | None = None,
    *,
    agent_registry: AgentRegistry | None = None,
    rate_limiter: RateLimiter | None = None,
    dedup_store: DedupStore | None = None,
    bus: InMemoryBus | None = None,
) -> FastAPI:
    """Build the ingest FastAPI application.

    `settings` is normally left `None` so the lifespan loads it (and the
    detection config it references) from the environment at startup; tests
    may pass an explicit `IngestSettings` to avoid depending on process
    environment variables. The four keyword-only overrides let a test
    substitute its own `AgentRegistry` (skipping `HAMMERTIME_INGEST_AGENTS_PATH`
    disk access), `RateLimiter`/`DedupStore` (e.g. built on a shared
    `ManualClock` for deterministic TTL/refill assertions), and `InMemoryBus`
    (so the test keeps a reference to read a topic's log back afterwards --
    `bus.producer()` is what actually gets wired into `ObservationPublisher`,
    the same relationship `InMemoryBus.consumer(group_id)` has elsewhere in
    this repo). Any override left `None` falls back to the normal
    settings/environment-driven construction below.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings if settings is not None else load_settings()
        detection_config = load_detection_config(resolved_settings.detection_config_path)
        registry = (
            agent_registry
            if agent_registry is not None
            else load_agent_registry_file(resolved_settings.agents_path)
        )
        resolved_rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()

        redis_client: Redis | None = None
        resolved_dedup_store: DedupStore
        if dedup_store is not None:
            resolved_dedup_store = dedup_store
        elif resolved_settings.store_kind == "redis":
            redis_client = Redis.from_url(resolved_settings.redis_url)
            resolved_dedup_store = RedisDedupStore(redis_client)
        else:
            resolved_dedup_store = MemoryDedupStore()

        producer: Producer
        kafka_producer: KafkaProducer | None = None
        if bus is not None:
            producer = bus.producer()
        elif resolved_settings.bus_kind == "kafka":
            kafka_producer = KafkaProducer(bootstrap_servers=resolved_settings.bus_brokers)
            await kafka_producer.start()
            producer = kafka_producer
        else:
            producer = InMemoryBus().producer()

        app.state.ingest = IngestState(
            settings=resolved_settings,
            schema_validator=ObservationSchemaValidator.from_file(),
            bucket_seconds=detection_config.bucket_seconds,
            registry=registry,
            authenticate=require_agent(registry),
            rate_limiter=resolved_rate_limiter,
            dedup=DedupService(
                resolved_dedup_store,
                allowed_lateness_seconds=detection_config.allowed_lateness_seconds,
            ),
            publisher=ObservationPublisher(
                producer, config_version=detection_config.config_version
            ),
        )
        try:
            yield
        finally:
            if kafka_producer is not None:
                await kafka_producer.stop()
            if redis_client is not None:
                await redis_client.aclose()

    app = FastAPI(title="hammertime-ingest", lifespan=lifespan)
    app.include_router(router)
    return app
