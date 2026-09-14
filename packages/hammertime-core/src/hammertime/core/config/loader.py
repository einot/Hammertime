"""Load, validate, and watch detection configuration.

Spec: section 34
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import ConfigurationError

# Mirrors schemas/detection_config.v1.json: the required keys, the full set
# of keys the schema allows (additionalProperties: false), and which of
# those must be a JSON integer (as opposed to any JSON number). Kept here
# rather than parsing the schema file at runtime so this package has no
# jsonschema-library dependency for one small, fixed document shape.
_REQUIRED_KEYS = frozenset(
    {
        "config_version",
        "window_seconds",
        "bucket_seconds",
        "hot_threshold",
        "cold_threshold",
        "minimum_hot_ips",
        "minimum_hot_ratio",
    }
)
_KNOWN_KEYS = frozenset({f.name for f in dataclasses.fields(DetectionConfig)})
_INTEGER_KEYS = _KNOWN_KEYS - {"minimum_hot_ratio"}


def _validate_document(data: Any) -> dict[str, Any]:
    """Structural validation against schemas/detection_config.v1.json.

    Threshold-ordering and other cross-field invariants are validated once,
    by `DetectionConfig.__post_init__`, not duplicated here.
    """
    if not isinstance(data, dict):
        raise ConfigurationError("detection config document must be a JSON object")

    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        raise ConfigurationError(f"detection config missing required keys: {sorted(missing)}")

    unknown = data.keys() - _KNOWN_KEYS
    if unknown:
        raise ConfigurationError(f"detection config has unknown keys: {sorted(unknown)}")

    for key in _INTEGER_KEYS & data.keys():
        value = data[key]
        # bool is a subclass of int in Python; the schema means a real integer.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(f"detection config field {key!r} must be an integer")

    return data


def load(path: Path) -> DetectionConfig:
    """Read and validate a config document against schemas/detection_config.v1.json."""
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ConfigurationError(f"cannot read detection config at {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"detection config at {path} is not valid JSON: {exc}") from exc

    data = _validate_document(data)

    try:
        return DetectionConfig(**data)
    except TypeError as exc:
        # DetectionConfig's own cross-field checks raise ConfigurationError
        # directly and pass through unchanged; this only catches a
        # constructor-arity mismatch, which _validate_document should
        # already have ruled out.
        raise ConfigurationError(
            f"detection config at {path} does not match DetectionConfig: {exc}"
        ) from exc


def watch(
    path: Path,
    *,
    poll_interval_seconds: float = 1.0,
    sleep: Any = time.sleep,
) -> Iterator[DetectionConfig]:
    """Yield a new DetectionConfig whenever the document changes on disk.

    A version bump MUST be accompanied by a controlled re-evaluation
    (services/aggregator/reevaluate.py); silently swapping thresholds leaves
    stale HOT/COLD state behind.

    This polls file content on a plain interval rather than depending on a
    filesystem-event library, keeping this package free of an I/O-framework
    dependency; `sleep` is injectable so tests can drive iterations without
    a real wall-clock wait. The generator runs forever — bound consumption
    with e.g. `itertools.islice` in tests or a cancellation point in
    production callers.
    """
    last: DetectionConfig | None = None
    while True:
        current = load(path)
        if current != last:
            last = current
            yield current
        sleep(poll_interval_seconds)
