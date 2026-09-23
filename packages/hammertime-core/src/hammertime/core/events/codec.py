"""Encode/decode events; schema-version negotiation and forward compatibility.

Spec: section 19, section 32, section 46.2; ADR-0015 decision 5,
Amendment 2 (ruling A) and Amendment 3 (ruling 3, assumptions 64-67); ADR-0011
decision 3 step 1; ADR-0016 decisions 1 and 2

The wire format is JSON. Every message is an `EventEnvelope` (see
`envelope.py`) with a `payload` object whose field names match the relevant
JSON Schema under `schemas/` exactly:

* `RequestObservation`  -> `schemas/observation.v1.json`
* `HotIpAdded`/`HotIpRemoved` -> `schemas/hot_ip_event.v1.json`
* `PrefixStatsChanged`  -> `schemas/prefix_stats_event.v1.json`

Nothing here ever lets a raw `json.JSONDecodeError`, `KeyError`,
`RecursionError` (from adversarially deep nesting), a bare `ValueError`
(from CPython's int-string conversion limit on an oversized numeric
literal), or `hammertime.core.errors.InvalidAddressError` (from a malformed
`ip` field) escape: every failure mode -- unknown `event_type`,
unknown/unsupported `schema_version`, or malformed bytes -- surfaces as
`CodecError`.

The codec's integer fields -- the envelope's `schema_version`, `sequence` and
`config_version`, and every payload field it converts to `int` -- accept on
decode exactly a JSON integer: an `int` that is not a `bool`, or a finite
`float` with no fractional part, converted to `int`. A string, a boolean, a
fractional or non-finite number, `null`, an array or an object is a
`CodecError`, so no `OverflowError` from `int(float("inf"))` can escape and
stop a consumer (ADR-0011 decision 3 step 1: "A poison message never stops
the consumer"). `capacity`, a decimal string on the wire, accepts exactly a
string of ASCII digits `[0-9]+`. On encode each integer field must hold an
`int` that is not a `bool` (a subclass such as an `IntEnum` member is
accepted), `capacity` such an `int` `>= 0`, and the envelope is serialized
with `allow_nan=False`, so no non-finite number reaches the wire.

The eight payload integer fields are also held to the inclusive `minimum` and
`maximum` their schema states, on decode and on encode alike (ADR-0016
decisions 1 and 2): `RequestObservation`'s `sequence` (0 to 2**63 - 1),
`window_seconds` (1 to 3600) and each `request_count` (0 to 1,000,000,000);
`HotIpAdded`/`HotIpRemoved`'s `sequence` and `window_count` (>= 0) and
`config_version` (>= 1); `PrefixStatsChanged`'s `hot_count` and `sequence`
(>= 0). Where the schema states no maximum, none is added. A value outside
its bounds is a `CodecError`; on decode the type check comes first, so an
integral float such as `-1.0` is converted and then refused by the bound. The
envelope's `sequence` and `config_version` gain no bounds (ADR-0016 assumption
3), its `schema_version` stays pinned to 1 (ADR-0016 decision 1), and
`capacity` keeps its `[0-9]+` rule. Each bound is a module constant mirroring
its schema (ADR-0016 assumption 10).

A `HotIpAdded`/`HotIpRemoved` payload's `attributes` document goes through
`hammertime.core.events.attributes.canonicalize_ip_attributes`, the one
implementation of the section 46.2 rules, on encode and on decode (ADR-0015
decision 5). What is kept is its canonical copy, never the payload's own
object: encode puts the copy into the envelope it sends, and decode puts it
into the event it returns, so the bytes on the wire are the bytes that were
checked (ADR-0015 Amendment 2 ruling A). An `InvalidAttributesError` becomes a
`CodecError` chained `from` it, so `CodecError.__cause__` identifies every
attributes rejection (ADR-0015 assumption 32).
"""

import json
import math
import re
from datetime import UTC, datetime
from typing import Any, NamedTuple

from hammertime.core.addressing.address import Address
from hammertime.core.errors import CodecError, InvalidAddressError, InvalidAttributesError
from hammertime.core.events.attributes import canonicalize_ip_attributes
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

#: schemas/prefix_stats_event.v1.json: `capacity` is a decimal string -- ASCII
#: digits only, not everything `int()` accepts (ADR-0015 assumption 65).
_DECIMAL_STRING = re.compile(r"[0-9]+")


class _Range(NamedTuple):
    """A field's inclusive `minimum` and `maximum`; `None` where the schema
    states no maximum (ADR-0016 decision 1)."""

    minimum: int
    maximum: int | None


#: schemas/observation.v1.json: properties.sequence minimum and maximum.
_OBSERVATION_SEQUENCE = _Range(0, 9_223_372_036_854_775_807)
#: schemas/observation.v1.json: properties.window_seconds minimum and maximum.
_OBSERVATION_WINDOW_SECONDS = _Range(1, 3600)
#: schemas/observation.v1.json: observations.items.request_count minimum and
#: maximum.
_OBSERVATION_REQUEST_COUNT = _Range(0, 1_000_000_000)
#: schemas/hot_ip_event.v1.json: properties.sequence minimum.
_HOT_IP_SEQUENCE = _Range(0, None)
#: schemas/hot_ip_event.v1.json: properties.window_count minimum.
_HOT_IP_WINDOW_COUNT = _Range(0, None)
#: schemas/hot_ip_event.v1.json: properties.config_version minimum.
_HOT_IP_CONFIG_VERSION = _Range(1, None)
#: schemas/prefix_stats_event.v1.json: properties.hot_count minimum.
_PREFIX_STATS_HOT_COUNT = _Range(0, None)
#: schemas/prefix_stats_event.v1.json: properties.sequence minimum.
_PREFIX_STATS_SEQUENCE = _Range(0, None)


def _range_violation(value: int, field: str, bounds: _Range) -> str | None:
    """Why `value` is outside `bounds`, or None if it is inside.

    The text names the field and the bound it broke, never the value
    (ADR-0016 decision 1).
    """
    if value < bounds.minimum:
        return f"{field} must be >= {bounds.minimum}"
    if bounds.maximum is not None and value > bounds.maximum:
        return f"{field} must be <= {bounds.maximum}"
    return None


def _bounded_json_integer(value: Any, field: str, bounds: _Range) -> int:
    """Decode a bounded payload integer: a JSON integer, then its range.

    Raises `ValueError`, which every caller turns into a `CodecError`
    (ADR-0016 decision 1).
    """
    number = _json_integer(value, field)
    violation = _range_violation(number, field, bounds)
    if violation is not None:
        raise ValueError(violation)
    return number


def _bounded_integer_field(value: object, field: str, bounds: _Range) -> int:
    """Encode a bounded payload integer: `_integer_field`, then its range,
    else `CodecError` (ADR-0016 decision 2)."""
    number = _integer_field(value, field)
    violation = _range_violation(number, field, bounds)
    if violation is not None:
        raise CodecError(violation)
    return number


def _json_integer(value: Any, field: str) -> int:
    """Decode an integer field: exactly a JSON integer, as an `int`.

    An `int` that is not a `bool`, or a finite `float` with no fractional part
    (JSON Schema's "integer"; ADR-0015 assumption 64). Anything else raises
    `ValueError`, which every caller turns into a `CodecError`; the message
    names the field and the value's JSON kind, never the value.
    """
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError(f"{field} must be a JSON integer, got {_json_kind(value)}")


def _decimal_string(value: Any, field: str) -> int:
    """Decode a decimal-string field (`capacity`): `[0-9]+`, as an `int`.

    `int()` itself may still refuse an over-long string under the
    interpreter's integer-string limit, with a `ValueError` (assumption 65).
    """
    if not isinstance(value, str) or _DECIMAL_STRING.fullmatch(value) is None:
        raise ValueError(f"{field} must be a string of ASCII decimal digits")
    return int(value)


def _json_kind(value: object) -> str:
    """What a decoded JSON value is, from its type alone."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, float):
        return "a fractional or non-finite number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


def _integer_field(value: object, field: str) -> int:
    """Encode an integer field: an `int` that is not a `bool`, else `CodecError`.

    An `int` subclass such as an `IntEnum` member is accepted, and `json`
    writes it as its number (ADR-0015 assumption 66).
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise CodecError(f"{field} must be an int, got {type(value).__name__}")
    return value


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
        "sequence": _bounded_integer_field(payload.sequence, "sequence", _OBSERVATION_SEQUENCE),
        "window_start": _format_timestamp(payload.window_start),
        "window_seconds": _bounded_integer_field(
            payload.window_seconds, "window_seconds", _OBSERVATION_WINDOW_SECONDS
        ),
        "observations": [
            {
                "ip": str(obs.ip),
                "request_count": _bounded_integer_field(
                    obs.request_count, "request_count", _OBSERVATION_REQUEST_COUNT
                ),
            }
            for obs in payload.observations
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
            request_count = _bounded_json_integer(
                _require(item, "request_count"), "request_count", _OBSERVATION_REQUEST_COUNT
            )
            observations.append(Observation(ip=ip, request_count=request_count))
        except (TypeError, ValueError, OverflowError, InvalidAddressError) as exc:
            raise CodecError(f"malformed observation entry: {item!r}") from exc

    try:
        return RequestObservation(
            agent_id=str(_require(data, "agent_id")),
            sequence=_bounded_json_integer(
                _require(data, "sequence"), "sequence", _OBSERVATION_SEQUENCE
            ),
            window_start=_parse_timestamp(_require(data, "window_start"), field="window_start"),
            window_seconds=_bounded_json_integer(
                _require(data, "window_seconds"), "window_seconds", _OBSERVATION_WINDOW_SECONDS
            ),
            observations=tuple(observations),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise CodecError(f"malformed RequestObservation payload: {data!r}") from exc


def _canonical_attributes(attributes: object) -> dict[str, object]:
    """The canonical copy of `attributes` (ADR-0015 decision 5, Amendment 2).

    Every section 46.2 violation is a `CodecError`. The validator's message
    never contains a document value, so repeating it here is safe; the
    `InvalidAttributesError` itself is the cause.
    """
    try:
        return canonicalize_ip_attributes(attributes).document
    except InvalidAttributesError as exc:
        raise CodecError(f"invalid attributes: {exc}") from exc


def _encode_hot_ip_event(event_type: str, payload: HotIpAdded | HotIpRemoved) -> dict[str, Any]:
    document: dict[str, Any] = {
        "type": event_type,
        "ip": str(payload.ip),
        "family": payload.ip.family.value,
        "timestamp": _format_timestamp(payload.timestamp),
        "sequence": _bounded_integer_field(payload.sequence, "sequence", _HOT_IP_SEQUENCE),
        "window_count": _bounded_integer_field(
            payload.window_count, "window_count", _HOT_IP_WINDOW_COUNT
        ),
        "config_version": _bounded_integer_field(
            payload.config_version, "config_version", _HOT_IP_CONFIG_VERSION
        ),
    }
    if payload.attributes is not None:
        document["attributes"] = _canonical_attributes(payload.attributes)
    return document


def _decode_hot_ip_event(event_type: str, data: dict[str, Any]) -> HotIpAdded | HotIpRemoved:
    try:
        ip = Address.parse(str(_require(data, "ip")))
        timestamp = _parse_timestamp(_require(data, "timestamp"), field="timestamp")
        sequence = _bounded_json_integer(_require(data, "sequence"), "sequence", _HOT_IP_SEQUENCE)
        window_count = _bounded_json_integer(
            _require(data, "window_count"), "window_count", _HOT_IP_WINDOW_COUNT
        )
        config_version = _bounded_json_integer(
            _require(data, "config_version"), "config_version", _HOT_IP_CONFIG_VERSION
        )
    except (TypeError, ValueError, OverflowError, InvalidAddressError) as exc:
        # attributes is deliberately excluded from this error message: it is
        # not yet validated at this point (that happens below) and has no
        # size bound applied yet, so echoing it verbatim here would let an
        # oversized x_ value blow up this exception's message on every such
        # malformed-scalar rejection.
        redacted = {k: ("<omitted>" if k == "attributes" else v) for k, v in data.items()}
        raise CodecError(f"malformed {event_type} payload: {redacted!r}") from exc

    attributes: dict[str, object] | None = None
    if "attributes" in data:
        raw_attributes = data["attributes"]
        # An explicit `"attributes": null` is not the key being absent: it is
        # a document that is not a JSON object (S1), and goes through the
        # validator like every other rejection so that its CodecError carries
        # the InvalidAttributesError cause too (ADR-0015 decision 5).
        attributes = _canonical_attributes(raw_attributes)

    if event_type == "HotIpAdded":
        return HotIpAdded(
            ip=ip,
            timestamp=timestamp,
            sequence=sequence,
            window_count=window_count,
            config_version=config_version,
            attributes=attributes,
        )
    return HotIpRemoved(
        ip=ip,
        timestamp=timestamp,
        sequence=sequence,
        window_count=window_count,
        config_version=config_version,
        attributes=attributes,
    )


def _encode_capacity(capacity: object) -> str:
    """`capacity`'s decimal string: an `int` that is not a `bool`, `>= 0`.

    The string is produced here, where a failure is a `CodecError`: a capacity
    too long for the interpreter's integer-string limit makes the conversion
    raise `ValueError` (ADR-0015 assumption 66). `int.__repr__` rather than
    `str()`, so that an `int` subclass's own `__str__` cannot put anything but
    digits on the wire; for an exact `int` the two give the same text.
    """
    value = _integer_field(capacity, "capacity")
    if value < 0:
        raise CodecError("capacity must be >= 0")
    try:
        return int.__repr__(value)
    except ValueError as exc:
        raise CodecError(f"capacity cannot be written as a decimal string: {exc}") from exc


def _encode_prefix_stats_changed(payload: PrefixStatsChanged) -> dict[str, Any]:
    return {
        "prefix": payload.prefix,
        "hot_count": _bounded_integer_field(
            payload.hot_count, "hot_count", _PREFIX_STATS_HOT_COUNT
        ),
        # schemas/prefix_stats_event.v1.json: a decimal string, because IPv6
        # capacities exceed 64 bits. The in-process payload keeps capacity as
        # a Python int (arbitrary precision); only the wire form is a string.
        "capacity": _encode_capacity(payload.capacity),
        "sequence": _bounded_integer_field(payload.sequence, "sequence", _PREFIX_STATS_SEQUENCE),
        "timestamp": _format_timestamp(payload.timestamp),
    }


def _decode_prefix_stats_changed(data: dict[str, Any]) -> PrefixStatsChanged:
    try:
        return PrefixStatsChanged(
            prefix=str(_require(data, "prefix")),
            hot_count=_bounded_json_integer(
                _require(data, "hot_count"), "hot_count", _PREFIX_STATS_HOT_COUNT
            ),
            capacity=_decimal_string(_require(data, "capacity"), "capacity"),
            sequence=_bounded_json_integer(
                _require(data, "sequence"), "sequence", _PREFIX_STATS_SEQUENCE
            ),
            timestamp=_parse_timestamp(_require(data, "timestamp"), field="timestamp"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
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
    schema_version = _integer_field(envelope.schema_version, "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise CodecError(f"unsupported schema_version: {schema_version!r}")

    document = {
        "schema_version": schema_version,
        "event_id": envelope.event_id,
        "agent_id": envelope.agent_id,
        "sequence": _integer_field(envelope.sequence, "sequence"),
        "event_type": envelope.event_type,
        "config_version": _integer_field(envelope.config_version, "config_version"),
        "timestamp": _format_timestamp(envelope.timestamp),
        "payload": _encode_payload(envelope.event_type, envelope.payload),
    }
    if envelope.subject is not None:
        document["subject"] = envelope.subject
    try:
        # allow_nan=False: a non-finite float anywhere is a CodecError, not
        # NaN or Infinity on the wire (ADR-0015 Amendment 3 ruling 3).
        return json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
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
    except UnicodeDecodeError as exc:
        # Also a ValueError subclass, so it must precede the clause below or
        # it would be misreported as an oversized numeric literal. The
        # exception text quotes offending bytes, so it is left out here.
        raise CodecError("envelope is not valid UTF-8") from exc
    except ValueError as exc:
        # CPython's int-string conversion limit (default 4300 digits)
        # raises a bare ValueError from inside json.loads for an oversized
        # numeric literal -- not JSONDecodeError (a subclass this except
        # clause would otherwise shadow, so it must stay after that one),
        # and reachable anywhere a JSON number appears, including inside an
        # x_-prefixed attribute value.
        raise CodecError(f"envelope JSON contains an oversized numeric literal: {exc}") from exc

    if not isinstance(document, dict):
        raise CodecError(f"envelope must be a JSON object, got {type(document).__name__}")

    try:
        # A JSON integer first, so that `true` is refused although True == 1.
        schema_version = _json_integer(_require(document, "schema_version"), "schema_version")
    except (ValueError, OverflowError) as exc:
        raise CodecError(f"unsupported schema_version: {exc}") from exc
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
            schema_version=schema_version,
            agent_id=str(_require(document, "agent_id")),
            sequence=_json_integer(_require(document, "sequence"), "sequence"),
            event_type=str(event_type),
            config_version=_json_integer(_require(document, "config_version"), "config_version"),
            timestamp=_parse_timestamp(_require(document, "timestamp"), field="timestamp"),
            payload=_decode_payload(event_type, payload_data),
            subject=raw_subject,
        )
    except (TypeError, ValueError, OverflowError) as exc:
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
