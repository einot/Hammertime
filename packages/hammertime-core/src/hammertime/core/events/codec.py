"""Encode/decode events; schema-version negotiation and forward compatibility.

Spec: section 19, section 32

The wire format is JSON. Every message is an `EventEnvelope` (see
`envelope.py`) with a `payload` object whose field names match the relevant
JSON Schema under `schemas/` exactly:

* `RequestObservation`  -> `schemas/observation.v1.json`
* `HotIpAdded`/`HotIpRemoved` -> `schemas/hot_ip_event.v1.json`
* `PrefixStatsChanged`  -> `schemas/prefix_stats_event.v1.json`

Nothing here ever lets a raw `json.JSONDecodeError`, `KeyError`,
`RecursionError` (from adversarially deep nesting), or
`hammertime.core.errors.InvalidAddressError` (from a malformed `ip` field)
escape: every failure mode -- unknown `event_type`, unknown/unsupported
`schema_version`, or malformed bytes -- surfaces as `CodecError`.
"""

import json
from datetime import UTC, datetime
from typing import Any

from hammertime.core.addressing.address import Address
from hammertime.core.errors import CodecError, InvalidAddressError
from hammertime.core.events.envelope import SCHEMA_VERSION, EventEnvelope
from hammertime.core.events.models import (
    HotIpAdded,
    HotIpRemoved,
    Observation,
    PrefixStatsChanged,
    RequestObservation,
)

#: Every payload type a decoded envelope may carry.
EventPayload = RequestObservation | HotIpAdded | HotIpRemoved | PrefixStatsChanged

_HOT_IP_EVENT_TYPES = frozenset({"HotIpAdded", "HotIpRemoved"})
_KNOWN_EVENT_TYPES = frozenset({"RequestObservation", "PrefixStatsChanged"}) | _HOT_IP_EVENT_TYPES

#: schemas/observation.v1.json: observations.maxItems. Enforced here too as
#: a defense-in-depth backstop -- services/ingest's own limit check
#: (services/ingest/src/hammertime/ingest/validation/limits.py) is not yet
#: implemented, so this is currently the only place that bounds it.
_MAX_OBSERVATIONS = 10_000


def _format_timestamp(value: datetime) -> str:
    """RFC3339/ISO-8601 UTC, `Z`-suffixed, matching the spec's examples."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise CodecError(f"{field} must be a string, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CodecError(f"{field} is not a valid date-time: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _require(data: dict[str, Any], key: str) -> Any:
    try:
        return data[key]
    except KeyError as exc:
        raise CodecError(f"missing required field: {key}") from exc


def _encode_request_observation(payload: RequestObservation) -> dict[str, Any]:
    return {
        "agent_id": payload.agent_id,
        "sequence": payload.sequence,
        "window_start": _format_timestamp(payload.window_start),
        "window_seconds": payload.window_seconds,
        "observations": [
            {"ip": str(obs.ip), "request_count": obs.request_count} for obs in payload.observations
        ],
    }


def _decode_request_observation(data: dict[str, Any]) -> RequestObservation:
    raw_observations = _require(data, "observations")
    if not isinstance(raw_observations, list):
        raise CodecError("observations must be an array")
    if len(raw_observations) > _MAX_OBSERVATIONS:
        raise CodecError(
            f"observations has {len(raw_observations)} entries, exceeding the "
            f"maximum of {_MAX_OBSERVATIONS} (schemas/observation.v1.json maxItems)"
        )
    observations = []
    for item in raw_observations:
        if not isinstance(item, dict):
            raise CodecError("each observation must be an object")
        try:
            ip = Address.parse(str(_require(item, "ip")))
            observations.append(
                Observation(ip=ip, request_count=int(_require(item, "request_count")))
            )
        except (TypeError, ValueError, InvalidAddressError) as exc:
            raise CodecError(f"malformed observation entry: {item!r}") from exc

    try:
        return RequestObservation(
            agent_id=str(_require(data, "agent_id")),
            sequence=int(_require(data, "sequence")),
            window_start=_parse_timestamp(_require(data, "window_start"), field="window_start"),
            window_seconds=int(_require(data, "window_seconds")),
            observations=tuple(observations),
        )
    except (TypeError, ValueError) as exc:
        raise CodecError(f"malformed RequestObservation payload: {data!r}") from exc


def _encode_hot_ip_event(event_type: str, payload: HotIpAdded | HotIpRemoved) -> dict[str, Any]:
    return {
        "type": event_type,
        "ip": str(payload.ip),
        "family": payload.ip.family.value,
        "timestamp": _format_timestamp(payload.timestamp),
        "sequence": payload.sequence,
        "window_count": payload.window_count,
        "config_version": payload.config_version,
    }


def _decode_hot_ip_event(event_type: str, data: dict[str, Any]) -> HotIpAdded | HotIpRemoved:
    try:
        ip = Address.parse(str(_require(data, "ip")))
        timestamp = _parse_timestamp(_require(data, "timestamp"), field="timestamp")
        sequence = int(_require(data, "sequence"))
        window_count = int(_require(data, "window_count"))
        config_version = int(_require(data, "config_version"))
    except (TypeError, ValueError, InvalidAddressError) as exc:
        raise CodecError(f"malformed {event_type} payload: {data!r}") from exc

    if event_type == "HotIpAdded":
        return HotIpAdded(
            ip=ip,
            timestamp=timestamp,
            sequence=sequence,
            window_count=window_count,
            config_version=config_version,
        )
    return HotIpRemoved(
        ip=ip,
        timestamp=timestamp,
        sequence=sequence,
        window_count=window_count,
        config_version=config_version,
    )


def _encode_prefix_stats_changed(payload: PrefixStatsChanged) -> dict[str, Any]:
    return {
        "prefix": payload.prefix,
        "hot_count": payload.hot_count,
        # schemas/prefix_stats_event.v1.json: a decimal string, because IPv6
        # capacities exceed 64 bits. The in-process payload keeps capacity as
        # a Python int (arbitrary precision); only the wire form is a string.
        "capacity": str(payload.capacity),
        "sequence": payload.sequence,
        "timestamp": _format_timestamp(payload.timestamp),
    }


def _decode_prefix_stats_changed(data: dict[str, Any]) -> PrefixStatsChanged:
    try:
        return PrefixStatsChanged(
            prefix=str(_require(data, "prefix")),
            hot_count=int(_require(data, "hot_count")),
            capacity=int(_require(data, "capacity")),
            sequence=int(_require(data, "sequence")),
            timestamp=_parse_timestamp(_require(data, "timestamp"), field="timestamp"),
        )
    except (TypeError, ValueError) as exc:
        raise CodecError(f"malformed PrefixStatsChanged payload: {data!r}") from exc


def _encode_payload(event_type: str, payload: EventPayload) -> dict[str, Any]:
    if event_type == "RequestObservation" and isinstance(payload, RequestObservation):
        return _encode_request_observation(payload)
    if event_type in _HOT_IP_EVENT_TYPES and isinstance(payload, HotIpAdded | HotIpRemoved):
        return _encode_hot_ip_event(event_type, payload)
    if event_type == "PrefixStatsChanged" and isinstance(payload, PrefixStatsChanged):
        return _encode_prefix_stats_changed(payload)
    raise CodecError(
        f"payload type {type(payload).__name__} does not match event_type {event_type!r}"
    )


def _decode_payload(event_type: str, data: dict[str, Any]) -> EventPayload:
    if event_type == "RequestObservation":
        return _decode_request_observation(data)
    if event_type in _HOT_IP_EVENT_TYPES:
        return _decode_hot_ip_event(event_type, data)
    if event_type == "PrefixStatsChanged":
        return _decode_prefix_stats_changed(data)
    raise CodecError(f"unknown event_type: {event_type!r}")


def encode(envelope: EventEnvelope[EventPayload]) -> bytes:
    """Serialize an envelope to its JSON wire form."""
    if envelope.event_type not in _KNOWN_EVENT_TYPES:
        raise CodecError(f"unknown event_type: {envelope.event_type!r}")
    if envelope.schema_version != SCHEMA_VERSION:
        raise CodecError(f"unsupported schema_version: {envelope.schema_version!r}")

    document = {
        "schema_version": envelope.schema_version,
        "event_id": envelope.event_id,
        "agent_id": envelope.agent_id,
        "sequence": envelope.sequence,
        "event_type": envelope.event_type,
        "config_version": envelope.config_version,
        "timestamp": _format_timestamp(envelope.timestamp),
        "payload": _encode_payload(envelope.event_type, envelope.payload),
    }
    if envelope.subject is not None:
        document["subject"] = envelope.subject
    try:
        return json.dumps(document, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CodecError(f"could not serialize envelope: {exc}") from exc


def decode(data: bytes) -> EventEnvelope[EventPayload]:
    """Deserialize an envelope from its JSON wire form.

    Raises `CodecError` for malformed bytes, an unknown `event_type`, or an
    unknown/unsupported `schema_version` -- never a raw `json.JSONDecodeError`
    or `KeyError`.
    """
    try:
        document = json.loads(data)
    except json.JSONDecodeError as exc:
        raise CodecError(f"envelope is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # A deeply nested document (e.g. thousands of nested `[`) blows the
        # interpreter's recursion limit inside json.loads before it ever
        # gets to raise JSONDecodeError. A few KB of adversarial input is
        # enough to trigger this, so it must surface as CodecError too.
        raise CodecError("envelope JSON is nested too deeply to parse") from exc

    if not isinstance(document, dict):
        raise CodecError(f"envelope must be a JSON object, got {type(document).__name__}")

    schema_version = _require(document, "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise CodecError(f"unsupported schema_version: {schema_version!r}")

    event_type = _require(document, "event_type")
    if not isinstance(event_type, str):
        # frozenset membership would otherwise raise a raw TypeError for an
        # unhashable event_type (a JSON array/object), instead of CodecError.
        raise CodecError(f"event_type must be a string, got {type(event_type).__name__}")
    if event_type not in _KNOWN_EVENT_TYPES:
        raise CodecError(f"unknown event_type: {event_type!r}")

    payload_data = _require(document, "payload")
    if not isinstance(payload_data, dict):
        raise CodecError("payload must be a JSON object")

    wire_event_id = str(_require(document, "event_id"))

    raw_subject = document.get("subject")
    if raw_subject is not None and not isinstance(raw_subject, str):
        raise CodecError(f"subject must be a string, got {type(raw_subject).__name__}")

    try:
        envelope = EventEnvelope(
            schema_version=int(schema_version),
            agent_id=str(_require(document, "agent_id")),
            sequence=int(_require(document, "sequence")),
            event_type=str(event_type),
            config_version=int(_require(document, "config_version")),
            timestamp=_parse_timestamp(_require(document, "timestamp"), field="timestamp"),
            payload=_decode_payload(event_type, payload_data),
            subject=raw_subject,
        )
    except (TypeError, ValueError) as exc:
        raise CodecError(f"malformed envelope: {exc}") from exc

    # event_id is never trusted from the wire -- EventEnvelope always
    # (re)derives it from (agent_id, sequence, event_type, subject) in
    # __post_init__. A mismatch means the bytes were corrupted or tampered
    # with in transit (including a subject that was added, removed, or
    # altered after the fact).
    if envelope.event_id != wire_event_id:
        raise CodecError(
            f"event_id {wire_event_id!r} does not match the identity fields "
            f"(agent_id, sequence, event_type, subject); derived {envelope.event_id!r}"
        )
    return envelope
