"""FastAPI application factory and lifespan (bus producer, dedup store).

Spec: section 4

The lifespan loads ingest settings and the detection config once at
startup (not per request): the compiled JSON Schema validator and the
detection config's `bucket_seconds` (needed to check `window_start`
alignment) are both expensive-ish to (re)build and never change between
requests within a process. Wiring a bus producer or dedup store into this
lifespan is Epic #4's job -- this app validates observations but does not
yet publish them anywhere.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from hammertime.core.config.loader import load as load_detection_config
from hammertime.ingest.api.routes import router
from hammertime.ingest.config import IngestSettings, load_settings
from hammertime.ingest.validation.schema import ObservationSchemaValidator


@dataclass(frozen=True, slots=True)
class IngestState:
    """Process-lifetime singletons the API layer reads on every request."""

    settings: IngestSettings
    schema_validator: ObservationSchemaValidator
    bucket_seconds: int


def create_app(settings: IngestSettings | None = None) -> FastAPI:
    """Build the ingest FastAPI application.

    `settings` is normally left `None` so the lifespan loads it (and the
    detection config it references) from the environment at startup; tests
    may pass an explicit `IngestSettings` to avoid depending on process
    environment variables.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings if settings is not None else load_settings()
        detection_config = load_detection_config(resolved_settings.detection_config_path)
        app.state.ingest = IngestState(
            settings=resolved_settings,
            schema_validator=ObservationSchemaValidator.from_file(),
            bucket_seconds=detection_config.bucket_seconds,
        )
        yield

    app = FastAPI(title="hammertime-ingest", lifespan=lifespan)
    app.include_router(router)
    return app
