"""Codec encode/decode round-trip and error handling (spec section 19, section 32).

`codec.py` is currently a docstring-only stub (`Encode/decode events;
schema-version negotiation and forward compatibility.`), so -- as with
test_envelope.py -- this file also defines the assumed public surface, per
architect clarification (issue #21's own wording: `EventEnvelope[T]` carries
`agent_id`/`sequence`/`event_id`/`config_version`/`timestamp` *plus* the
payload, nested inside the envelope rather than flattened alongside it):

    encode(envelope: EventEnvelope) -> bytes
    decode(data: bytes) -> EventEnvelope

`decode` is self-describing: it reconstructs the whole `EventEnvelope`
(including its nested `.payload`) from `data` alone, dispatching on a
top-level `event_type` discriminator and a top-level `schema_version` tag.
Those two field names are taken directly from ADR-0003's amendment wording
("`event_type` here is the discriminator already carried by the codec's
schema-version-tagged wire format (core/events/codec.py)"). Per the
architect's clarification, the wire shape looks like:

    {
      "schema_version": <int>,
      "event_type": "HotIpAdded" | "HotIpRemoved" | "PrefixStatsChanged" | "RequestObservation",
      "event_id": "<deterministic id>",
      "agent_id": "<producer id>",
      "sequence": <int>,
      "config_version": <int>,
      "timestamp": "<iso8601>",
      "payload": { ...fields shaped against the type-specific schema, e.g.
                    schemas/hot_ip_event.v1.json for HotIpAdded/HotIpRemoved... }
    }

The `additionalProperties: false` / no-`agent_id` constraints in
schemas/*.json apply only to that inner `"payload"` object, not to the
envelope wrapper -- envelope-level metadata lives alongside `"payload"`,
not inside it. These tests don't hard-code the exact wire shape beyond
those two top-level discriminator fields (see `_tamper`); round-trip
fidelity is checked black-box (`decode(encode(envelope)) == envelope`)
rather than by asserting the literal JSON structure, so they stay valid
even if the coder's exact key layout inside `"payload"` differs from the
sketch above.

ADR-0015 Amendment 3 ruling 3 (assumptions 64-67; ADR-0015 decision 5's
"Three rows that hold because of Amendment 3"; ADR-0011 decision 3 step 1,
"A poison message never stops the consumer") is tested in the section at the
end of this file. On decode, each of the codec's integer fields accepts exactly
a JSON integer -- an `int` that is not a `bool`, or a finite `float` with no
fractional part -- and `capacity` exactly a string of ASCII digits; anything
else is a `CodecError`, never an `OverflowError` or `ValueError`. On encode,
each integer field must hold an `int` that is not a `bool` (an `IntEnum` member
is one), `capacity` such an `int` `>= 0`, and no non-finite float is written
anywhere. The ruling names the fields, so that section does address them by
their wire keys: the envelope's `schema_version`, `sequence` and
`config_version`, and the payload keys the schemas name. Each test first
checks that the key it tampers with is on the wire as an integer, so a key
that is not there fails the test rather than being added and ignored.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

import pytest
from hammertime.core.addressing.address import Address
from hammertime.core.errors import CodecError
from hammertime.core.events.codec import decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import (
    HotIpAdded,
    HotIpRemoved,
    Observation,
    PrefixStatsChanged,
    RequestObservation,
)

T0 = datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC)


def _tamper(data: bytes, **overrides: object) -> bytes:
    """Round-trip `data` through json, override top-level fields, re-serialize.

    Used to construct otherwise-valid wire bytes with a single field pushed
    out of range, without hard-coding the rest of the wire shape.
    """

    doc = json.loads(data)
    doc.update(overrides)
    return json.dumps(doc).encode("utf-8")


def _tamper_nested(data: bytes, *, path: tuple[object, ...], value: object) -> bytes:
    """Like `_tamper`, but overrides a value nested inside `payload`.

    `path` is a sequence of dict keys / list indices, e.g.
    `("payload", "observations", 0, "ip")`.
    """

    doc = json.loads(data)
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return json.dumps(doc).encode("utf-8")


class TestRoundTrip:
    def test_request_observation_round_trips(self) -> None:
        payload = RequestObservation(
            agent_id="edge-17",
            sequence=123456,
            window_start=T0,
            window_seconds=60,
            observations=(Observation(ip=Address.parse("192.168.1.42"), request_count=183),),
        )
        envelope = EventEnvelope(
            agent_id="edge-17",
            sequence=123456,
            event_type="RequestObservation",
            config_version=1,
            timestamp=T0,
            payload=payload,
        )

        data = encode(envelope)
        decoded = decode(data)

        assert decoded == envelope
        assert decoded.payload == payload

    def test_hot_ip_added_round_trips(self) -> None:
        payload = HotIpAdded(
            ip=Address.parse("192.168.1.42"),
            timestamp=T0,
            sequence=7,
            window_count=1000,
            config_version=2,
        )
        envelope = EventEnvelope(
            agent_id="shard-3",
            sequence=7,
            event_type="HotIpAdded",
            config_version=2,
            timestamp=T0,
            payload=payload,
        )

        data = encode(envelope)
        decoded = decode(data)

        assert decoded == envelope
        assert decoded.payload == payload

    def test_hot_ip_removed_round_trips(self) -> None:
        payload = HotIpRemoved(
            ip=Address.parse("192.168.1.42"),
            timestamp=T0,
            sequence=8,
            window_count=750,
            config_version=2,
        )
        envelope = EventEnvelope(
            agent_id="shard-3",
            sequence=8,
            event_type="HotIpRemoved",
            config_version=2,
            timestamp=T0,
            payload=payload,
        )

        data = encode(envelope)
        decoded = decode(data)

        assert decoded == envelope
        assert decoded.payload == payload

    def test_prefix_stats_changed_round_trips(self) -> None:
        payload = PrefixStatsChanged(
            prefix="10.20.30.0/24",
            hot_count=156,
            capacity=256,
            sequence=99,
            timestamp=T0,
        )
        envelope = EventEnvelope(
            agent_id="trie-primary",
            sequence=99,
            event_type="PrefixStatsChanged",
            config_version=1,
            timestamp=T0,
            payload=payload,
        )

        data = encode(envelope)
        decoded = decode(data)

        assert decoded == envelope
        assert decoded.payload == payload

    def test_prefix_stats_changed_capacity_round_trips_as_int_even_for_huge_ipv6_capacities(
        self,
    ) -> None:
        # schemas/prefix_stats_event.v1.json encodes capacity as a decimal
        # *string* on the wire ("IPv6 capacities exceed 64-bit"), but
        # models.PrefixStatsChanged.capacity is a plain int -- the codec is
        # responsible for the str<->int conversion so callers never see the
        # wire representation.
        huge_capacity = 2**128
        payload = PrefixStatsChanged(
            prefix="2001:db8::/32",
            hot_count=12,
            capacity=huge_capacity,
            sequence=1,
            timestamp=T0,
        )
        envelope = EventEnvelope(
            agent_id="trie-primary",
            sequence=1,
            event_type="PrefixStatsChanged",
            config_version=1,
            timestamp=T0,
            payload=payload,
        )

        data = encode(envelope)
        decoded = decode(data)

        assert isinstance(decoded.payload, PrefixStatsChanged)
        assert decoded.payload.capacity == huge_capacity
        assert isinstance(decoded.payload.capacity, int)


def _hot_ip_added_envelope(*, event_type: str) -> EventEnvelope[HotIpAdded]:
    payload = HotIpAdded(
        ip=Address.parse("10.0.0.1"), timestamp=T0, sequence=1, window_count=1000, config_version=1
    )
    return EventEnvelope(
        agent_id="shard-3",
        sequence=1,
        event_type=event_type,
        config_version=1,
        timestamp=T0,
        payload=payload,
    )


class TestUnknownEventType:
    def test_encode_rejects_an_envelope_with_an_unrecognized_event_type(self) -> None:
        envelope = _hot_ip_added_envelope(event_type="NotARealEventType")

        with pytest.raises(CodecError):
            encode(envelope)

    def test_decode_rejects_wire_bytes_with_an_unrecognized_event_type(self) -> None:
        envelope = _hot_ip_added_envelope(event_type="HotIpAdded")
        data = encode(envelope)

        tampered = _tamper(data, event_type="SomeFutureEventTypeNotYetKnown")

        with pytest.raises(CodecError):
            decode(tampered)


class TestUnknownSchemaVersion:
    def test_decode_rejects_a_future_schema_version(self) -> None:
        envelope = _hot_ip_added_envelope(event_type="HotIpAdded")
        data = encode(envelope)

        tampered = _tamper(data, schema_version=999999)

        with pytest.raises(CodecError):
            decode(tampered)


class TestMalformedBytes:
    def test_decode_rejects_truncated_json(self) -> None:
        with pytest.raises(CodecError):
            decode(b'{"event_type": "HotIpAdded", "schema_version": 1, "payload": {')

    def test_decode_rejects_non_json_garbage(self) -> None:
        with pytest.raises(CodecError):
            decode(b"\x00\x01\xffnot json at all")

    def test_decode_rejects_empty_bytes(self) -> None:
        with pytest.raises(CodecError):
            decode(b"")

    def test_decode_does_not_leak_the_underlying_json_decode_error(self) -> None:
        # The point of CodecError is that callers only need to catch one
        # exception type regardless of the underlying serialization format.
        try:
            decode(b"{not valid json")
        except CodecError:
            pass
        except Exception as exc:
            pytest.fail(f"decode() leaked {type(exc).__name__} instead of raising CodecError")

    def test_decode_rejects_pathologically_deep_nesting_as_codec_error(self) -> None:
        # A few KB of nested arrays blows CPython's json parser recursion
        # limit (RecursionError) well before JSONDecodeError would fire --
        # that must still surface as CodecError, not a raw RecursionError.
        deeply_nested = b"[" * 10_000 + b"]" * 10_000
        with pytest.raises(CodecError):
            decode(deeply_nested)

    def test_decode_rejects_an_oversized_numeric_literal_as_codec_error(self) -> None:
        # CPython's int-string conversion limit (default 4300 digits)
        # raises a bare ValueError from inside json.loads for a
        # many-thousand-digit integer literal -- not JSONDecodeError, so it
        # needs its own guard; must still surface as CodecError.
        huge_digits = b"9" * 5000
        document = b'{"schema_version": ' + huge_digits + b", " + b'"event_type": "HotIpAdded"}'
        with pytest.raises(CodecError):
            decode(document)

    def test_decode_rejects_malformed_ip_in_observation_as_codec_error(self) -> None:
        envelope = EventEnvelope(
            agent_id="edge-17",
            sequence=1,
            event_type="RequestObservation",
            config_version=1,
            timestamp=T0,
            payload=RequestObservation(
                agent_id="edge-17",
                sequence=1,
                window_start=T0,
                window_seconds=60,
                observations=(Observation(ip=Address.parse("10.0.0.1"), request_count=1),),
            ),
        )
        data = encode(envelope)
        tampered = _tamper_nested(
            data, path=("payload", "observations", 0, "ip"), value="not-an-ip-address"
        )

        with pytest.raises(CodecError):
            decode(tampered)

    def test_decode_rejects_malformed_ip_in_hot_ip_event_as_codec_error(self) -> None:
        envelope = _hot_ip_added_envelope(event_type="HotIpAdded")
        data = encode(envelope)
        tampered = _tamper_nested(data, path=("payload", "ip"), value="999.999.999.999")

        with pytest.raises(CodecError):
            decode(tampered)

    def test_decode_rejects_an_observations_array_over_the_schema_limit(self) -> None:
        envelope = EventEnvelope(
            agent_id="edge-17",
            sequence=1,
            event_type="RequestObservation",
            config_version=1,
            timestamp=T0,
            payload=RequestObservation(
                agent_id="edge-17",
                sequence=1,
                window_start=T0,
                window_seconds=60,
                observations=(Observation(ip=Address.parse("10.0.0.1"), request_count=1),),
            ),
        )
        data = encode(envelope)
        # schemas/observation.v1.json: observations.maxItems is 10_000.
        oversized = _tamper_nested(
            data,
            path=("payload", "observations"),
            value=[{"ip": "10.0.0.1", "request_count": 1}] * 10_001,
        )

        with pytest.raises(CodecError):
            decode(oversized)


# ==========================================================================
# ADR-0015 Amendment 3, ruling 3: the codec's integer fields accept exactly a
# JSON integer, `capacity` exactly a string of ASCII digits, and the encoder
# writes no non-finite number (assumptions 64-67).
# ==========================================================================

EVENT_TYPES = ["RequestObservation", "HotIpAdded", "HotIpRemoved", "PrefixStatsChanged"]

_ENVELOPE_FIELDS: dict[str, Any] = {
    "agent_id": "edge-17",
    "sequence": 7,
    "config_version": 2,
    "timestamp": T0,
}
_OBSERVATION_FIELDS: dict[str, Any] = {"ip": Address.parse("192.168.1.42"), "request_count": 183}
_HOT_IP_FIELDS: dict[str, Any] = {
    "ip": Address.parse("10.0.0.1"),
    "timestamp": T0,
    "sequence": 7,
    "window_count": 1000,
    "config_version": 2,
}
_PAYLOAD_FIELDS: dict[str, dict[str, Any]] = {
    "RequestObservation": {
        "agent_id": "edge-17",
        "sequence": 7,
        "window_start": T0,
        "window_seconds": 60,
    },
    "HotIpAdded": _HOT_IP_FIELDS,
    "HotIpRemoved": _HOT_IP_FIELDS,
    "PrefixStatsChanged": {
        "prefix": "10.20.30.0/24",
        "hot_count": 156,
        "capacity": 256,
        "sequence": 7,
        "timestamp": T0,
    },
}


def _envelope(
    event_type: str, where: str | None = None, field: str | None = None, value: object = None
) -> EventEnvelope[Any]:
    """A valid envelope of `event_type`. Given `where` ("envelope", "payload"
    or "observation", the one entry of a `RequestObservation`) and `field`,
    that constructor argument is replaced by `value`, which may be
    deliberately wrong-typed."""

    fields: dict[str, dict[str, Any]] = {
        "envelope": dict(_ENVELOPE_FIELDS),
        "payload": dict(_PAYLOAD_FIELDS[event_type]),
        "observation": dict(_OBSERVATION_FIELDS),
    }
    if where is not None and field is not None:
        assert field in fields[where], (where, field)
        fields[where][field] = value
    payload: Any
    if event_type == "RequestObservation":
        observation = Observation(**fields["observation"])
        payload = RequestObservation(**fields["payload"], observations=(observation,))
    elif event_type == "HotIpAdded":
        payload = HotIpAdded(**fields["payload"])
    elif event_type == "HotIpRemoved":
        payload = HotIpRemoved(**fields["payload"])
    else:
        payload = PrefixStatsChanged(**fields["payload"])
    return EventEnvelope(event_type=event_type, payload=payload, **fields["envelope"])


def _wire_value(data: bytes, path: tuple[str | int, ...]) -> object:
    """The value at `path` in the wire JSON, failing if a key is not there."""

    target: Any = json.loads(data)
    for key in path:
        if isinstance(key, str):
            assert key in target, f"{key!r} is not on the wire at {path!r}"
        target = target[key]
    return target


def _tamper_field(data: bytes, path: tuple[str | int, ...], value: object) -> bytes:
    if len(path) == 1:
        return _tamper(data, **{str(path[0]): value})
    return _tamper_nested(data, path=path, value=value)


def _decoded_field(decoded: Any, path: tuple[str | int, ...]) -> object:
    """The decoded envelope's value for the wire field at `path`: an envelope
    field is its attribute, a payload field the payload's attribute."""

    target = decoded
    for key in path:
        target = target[key] if isinstance(key, int) else getattr(target, key)
    return target


# Ruling 3's list: the envelope's three, and the payload fields the codec
# converts to `int` -- the fields the schemas type "integer" and the codec
# reads (`hot_ip_event.v1.json`'s `shard` is not read; assumption 64).
_ENVELOPE_INTEGER_FIELDS = ("schema_version", "sequence", "config_version")
_PAYLOAD_INTEGER_FIELDS: list[tuple[str, tuple[str | int, ...]]] = [
    ("RequestObservation", ("payload", "sequence")),
    ("RequestObservation", ("payload", "window_seconds")),
    ("RequestObservation", ("payload", "observations", 0, "request_count")),
    ("HotIpAdded", ("payload", "sequence")),
    ("HotIpAdded", ("payload", "window_count")),
    ("HotIpAdded", ("payload", "config_version")),
    ("HotIpRemoved", ("payload", "sequence")),
    ("HotIpRemoved", ("payload", "window_count")),
    ("HotIpRemoved", ("payload", "config_version")),
    ("PrefixStatsChanged", ("payload", "hot_count")),
    ("PrefixStatsChanged", ("payload", "sequence")),
]
INTEGER_FIELDS: list[Any] = [
    *(
        pytest.param(event_type, (name,), id=f"{event_type}-envelope-{name}")
        for event_type in EVENT_TYPES
        for name in _ENVELOPE_INTEGER_FIELDS
    ),
    *(
        pytest.param(event_type, path, id="-".join([event_type, *map(str, path)]))
        for event_type, path in _PAYLOAD_INTEGER_FIELDS
    ),
]

NOT_JSON_INTEGERS: list[Any] = [
    pytest.param("5", id="string"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(5.5, id="fractional"),
    pytest.param(None, id="null"),
    pytest.param([], id="array"),
    pytest.param({}, id="object"),
    pytest.param(float("inf"), id="Infinity"),
    pytest.param(float("-inf"), id="minus-Infinity"),
    pytest.param(float("nan"), id="NaN"),
]


def test_the_eleven_integer_fields_are_the_rulings_list() -> None:
    """Assumption 64: three envelope fields and eight payload fields."""

    names = {(event_type, str(path[-1])) for event_type, path in _PAYLOAD_INTEGER_FIELDS}
    per_model = {
        "RequestObservation": {"sequence", "window_seconds", "request_count"},
        "HotIpAdded": {"sequence", "window_count", "config_version"},
        "HotIpRemoved": {"sequence", "window_count", "config_version"},
        "PrefixStatsChanged": {"hot_count", "sequence"},
    }
    for event_type, expected in per_model.items():
        assert {name for model, name in names if model == event_type} == expected
    # HotIpAdded and HotIpRemoved share one schema, so their fields count once.
    shared = {"HotIpAdded": "hot_ip_event", "HotIpRemoved": "hot_ip_event"}
    schema_fields = {(shared.get(model, model), name) for model, name in names}
    assert len(schema_fields) == 8
    assert len(_ENVELOPE_INTEGER_FIELDS) + len(schema_fields) == 11


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_every_base_envelope_round_trips(event_type: str) -> None:
    """Control for the tests below: the untampered envelope is valid."""

    envelope = _envelope(event_type)
    assert decode(encode(envelope)) == envelope


@pytest.mark.parametrize("bad", NOT_JSON_INTEGERS)
@pytest.mark.parametrize(("event_type", "path"), INTEGER_FIELDS)
def test_decode_rejects_anything_but_a_json_integer_in_an_integer_field(
    event_type: str, path: tuple[str | int, ...], bad: object
) -> None:
    """Ruling 3: a string, a boolean, a fractional or non-finite number, null,
    an array or an object is a `CodecError`. `pytest.raises` lets an
    `OverflowError` (from `int(float("inf"))`) or any other type fail."""

    data = encode(_envelope(event_type))
    assert type(_wire_value(data, path)) is int
    with pytest.raises(CodecError):
        decode(_tamper_field(data, path, bad))


@pytest.mark.parametrize(("event_type", "path"), INTEGER_FIELDS)
def test_decode_accepts_an_integral_float_in_an_integer_field_and_yields_an_int(
    event_type: str, path: tuple[str | int, ...]
) -> None:
    """Assumption 64: JSON Schema's "integer" admits `7.0`. The tampered value
    is the one the envelope was built with, because `event_id` depends on
    the envelope's `sequence`."""

    envelope = _envelope(event_type)
    data = encode(envelope)
    original = _wire_value(data, path)
    assert type(original) is int
    tampered = _tamper_field(data, path, float(original))
    assert f"{original}.0".encode() in tampered

    decoded: Any = decode(tampered)
    assert decoded == envelope
    if path != ("schema_version",):
        # The ruling fixes `schema_version`'s wire key, not an attribute name.
        value = _decoded_field(decoded, path)
        assert type(value) is int
        assert value == original


def _capacity_wire() -> bytes:
    # hot_count 0, so that no capacity below is smaller than the count.
    data = encode(_envelope("PrefixStatsChanged", "payload", "hot_count", 0))
    assert _wire_value(data, ("payload", "capacity")) == "256"
    return data


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("0", 0, id="zero"),
        pytest.param("256", 256, id="256"),
        pytest.param("007", 7, id="leading-zeros"),
        pytest.param(str(2**128), 2**128, id="2-to-the-128"),
    ],
)
def test_decode_accepts_capacity_as_a_string_of_ascii_digits(text: str, expected: int) -> None:
    """Assumption 65: `[0-9]+`, leading zeros included."""

    tampered = _tamper_nested(_capacity_wire(), path=("payload", "capacity"), value=text)
    decoded: Any = decode(tampered)
    assert type(decoded.payload.capacity) is int
    assert decoded.payload.capacity == expected


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(256, id="json-number"),
        pytest.param(True, id="true"),
        pytest.param(None, id="null"),
        pytest.param("", id="empty"),
        pytest.param(" 256", id="leading-space"),
        pytest.param("256 ", id="trailing-space"),
        pytest.param("+256", id="plus-sign"),
        pytest.param("-1", id="minus-one"),
        pytest.param("2_56", id="underscore"),
        pytest.param("1e3", id="exponent"),
        pytest.param("256.0", id="decimal-point"),
        pytest.param(chr(0x0663), id="arabic-indic-digit-three"),
    ],
)
def test_decode_rejects_a_capacity_that_is_not_a_string_of_ascii_digits(bad: object) -> None:
    """Assumption 65: no sign, whitespace, underscore, non-ASCII digit or JSON
    number -- everything `int()` would accept beyond the schema's "decimal
    string"."""

    tampered = _tamper_nested(_capacity_wire(), path=("payload", "capacity"), value=bad)
    with pytest.raises(CodecError):
        decode(tampered)


_ENCODE_INTEGER_FIELDS: list[Any] = [
    *(
        pytest.param(event_type, "envelope", name, id=f"{event_type}-envelope-{name}")
        for event_type in EVENT_TYPES
        for name in _ENVELOPE_INTEGER_FIELDS
    ),
    pytest.param("RequestObservation", "payload", "sequence", id="observation-sequence"),
    pytest.param("RequestObservation", "payload", "window_seconds", id="window_seconds"),
    pytest.param("RequestObservation", "observation", "request_count", id="request_count"),
    *(
        pytest.param(event_type, "payload", name, id=f"{event_type}-{name}")
        for event_type in ("HotIpAdded", "HotIpRemoved")
        for name in ("sequence", "window_count", "config_version")
    ),
    pytest.param("PrefixStatsChanged", "payload", "hot_count", id="hot_count"),
    pytest.param("PrefixStatsChanged", "payload", "sequence", id="prefix-stats-sequence"),
]


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(True, id="true"),
        pytest.param(1.0, id="float"),
        pytest.param("1", id="string"),
        pytest.param(None, id="none"),
    ],
)
@pytest.mark.parametrize(("event_type", "where", "field"), _ENCODE_INTEGER_FIELDS)
def test_encode_rejects_anything_but_an_int_in_an_integer_field(
    event_type: str, where: str, field: str, bad: object
) -> None:
    """Assumption 66: stricter than decode -- an integral float would break
    the `event_id` round trip. `schema_version` is not in `_ENVELOPE_FIELDS`
    (the valid envelope leaves it to its default), so it is passed here."""

    envelope: EventEnvelope[Any]
    if (where, field) == ("envelope", "schema_version"):
        payload = _envelope(event_type).payload
        wrong: Any = bad
        envelope = EventEnvelope(
            event_type=event_type, payload=payload, schema_version=wrong, **_ENVELOPE_FIELDS
        )
    else:
        envelope = _envelope(event_type, where, field, bad)
    with pytest.raises(CodecError):
        encode(envelope)


def _huge_capacity() -> int:
    # 5001 digits: above CPython's default integer-string limit, so `str()`
    # of it raises ValueError. Built arithmetically, never parsed or printed.
    return 10**5000


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: -1, id="minus-one"),
        pytest.param(lambda: True, id="true"),
        pytest.param(lambda: 1.0, id="float"),
        pytest.param(lambda: "256", id="string"),
        pytest.param(_huge_capacity, id="5001-digits"),
    ],
)
def test_encode_rejects_a_capacity_that_is_not_a_non_negative_int(
    make: Callable[[], object],
) -> None:
    """Assumption 66: `capacity` is an `int >= 0`, and its decimal string is
    produced where a failure is a `CodecError` -- never the `ValueError` that
    `str()` of an integer over the limit raises."""

    envelope = _envelope("PrefixStatsChanged", "payload", "capacity", make())
    with pytest.raises(CodecError):
        encode(envelope)


class _Count(IntEnum):
    WINDOW = 1000
    HOT = 156


@pytest.mark.parametrize(
    ("event_type", "field", "member"),
    [
        pytest.param("HotIpAdded", "window_count", _Count.WINDOW, id="window_count"),
        pytest.param("PrefixStatsChanged", "hot_count", _Count.HOT, id="hot_count"),
    ],
)
def test_encode_accepts_an_int_enum_member_and_writes_its_number(
    event_type: str, field: str, member: _Count
) -> None:
    """Assumption 66: an `int` subclass other than `bool` is accepted."""

    data = encode(_envelope(event_type, "payload", field, member))
    written = _wire_value(data, ("payload", field))
    assert type(written) is int
    assert written == int(member)
    assert b"_Count" not in data

    decoded: Any = decode(data)
    value = getattr(decoded.payload, field)
    assert type(value) is int
    assert value == int(member)
    assert decoded == _envelope(event_type, "payload", field, int(member))


@pytest.mark.parametrize(
    "non_finite",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="minus-inf"),
    ],
)
@pytest.mark.parametrize(
    ("event_type", "field"),
    [
        pytest.param("PrefixStatsChanged", "prefix", id="prefix"),
        pytest.param("RequestObservation", "agent_id", id="observation-agent_id"),
    ],
)
def test_encode_writes_no_non_finite_float_in_any_field(
    event_type: str, field: str, non_finite: float
) -> None:
    """Ruling 3: the envelope is serialized with `allow_nan=False`, so a
    non-finite float anywhere is a `CodecError`, not `NaN` on the wire."""

    envelope = _envelope(event_type, "payload", field, non_finite)
    with pytest.raises(CodecError):
        encode(envelope)
