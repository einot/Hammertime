"""Service settings: bind address and request validation limits.

Spec: section 4, section 36

Bus and store wiring is explicitly out of scope here -- that is Epic #4's
job, once agent auth, dedup, and publishing land. Nothing in this module
constructs a bus producer or a dedup store.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_BIND = "0.0.0.0:8080"
_DEFAULT_MAX_BODY_BYTES = 1_048_576
_DEFAULT_MAX_OBSERVATIONS = 10_000
_DEFAULT_CONFIG_PATH = "./config/detection.v1.json"


@dataclass(frozen=True, slots=True)
class IngestSettings:
    """Ingest service settings, sourced from the `.env.example` ingest keys."""

    #: Interface to bind the HTTP server to (from HAMMERTIME_INGEST_BIND).
    host: str
    port: int
    #: HAMMERTIME_INGEST_MAX_BODY_BYTES: request body size limit (413 above it).
    max_body_bytes: int
    #: HAMMERTIME_INGEST_MAX_OBSERVATIONS: observations[] length limit (413 above it).
    max_observations: int
    #: HAMMERTIME_CONFIG_PATH: detection config, read for its bucket_seconds
    #: (window_start alignment -- docs/protocol/observation-v1.md).
    detection_config_path: Path


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port_text = bind.rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise ValueError(f"HAMMERTIME_INGEST_BIND must be 'host:port', got {bind!r}")
    return host, int(port_text)


def _parse_positive_int(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer, got {parsed}")
    return parsed


def load_settings(env: Mapping[str, str] | None = None) -> IngestSettings:
    """Load ingest settings from the environment (see `.env.example`)."""
    source = env if env is not None else os.environ
    host, port = _parse_bind(source.get("HAMMERTIME_INGEST_BIND", _DEFAULT_BIND))
    return IngestSettings(
        host=host,
        port=port,
        max_body_bytes=_parse_positive_int(
            "HAMMERTIME_INGEST_MAX_BODY_BYTES",
            source.get("HAMMERTIME_INGEST_MAX_BODY_BYTES", str(_DEFAULT_MAX_BODY_BYTES)),
        ),
        max_observations=_parse_positive_int(
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS",
            source.get("HAMMERTIME_INGEST_MAX_OBSERVATIONS", str(_DEFAULT_MAX_OBSERVATIONS)),
        ),
        detection_config_path=Path(source.get("HAMMERTIME_CONFIG_PATH", _DEFAULT_CONFIG_PATH)),
    )
