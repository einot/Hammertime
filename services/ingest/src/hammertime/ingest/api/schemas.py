"""Request/response models mirroring schemas/observation.v1.json.

Spec: section 4

These are wire-facing models: raw strings/ints, no domain types. They exist
for typed access to a document that has *already* passed
`hammertime.ingest.validation.schema.ObservationSchemaValidator` -- the
jsonschema-based check against schemas/observation.v1.json is what actually
enforces the wire contract (additionalProperties, ranges, required fields),
not these Pydantic models. `hammertime.ingest.api.routes` converts a parsed
`ObservationRequest` into `hammertime.core.events.models.RequestObservation`,
parsing `ip` strings into `Address` along the way.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ObservationEntry(BaseModel):
    """One `{ip, request_count}` pair, mirroring `observations[]`."""

    model_config = ConfigDict(extra="forbid")

    ip: str
    request_count: int = Field(ge=0, le=1_000_000_000)


class ObservationRequest(BaseModel):
    """The full `POST /v1/observations` request body."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    window_start: str
    window_seconds: int = Field(ge=1, le=3600)
    observations: list[ObservationEntry] = Field(min_length=1, max_length=10_000)


class ObservationAccepted(BaseModel):
    """202 response body: accepted, but not yet published (Epic #4 wires publishing)."""

    status: Literal["accepted"] = "accepted"


class HealthStatus(BaseModel):
    """200 response body for `GET /healthz`."""

    status: Literal["ok"] = "ok"
