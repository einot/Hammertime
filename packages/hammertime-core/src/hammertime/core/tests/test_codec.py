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
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

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

T0 = datetime(2026, 9, 14, 10, 5, 0, tzinfo=timezone.utc)


def _tamper(data: bytes, **overrides: object) -> bytes:
    """Round-trip `data` through json, override top-level fields, re-serialize.

    Used to construct otherwise-valid wire bytes with a single field pushed
    out of range, without hard-coding the rest of the wire shape.
    """

    doc = json.loads(data)
    doc.update(overrides)
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

    def test_prefix_stats_changed_capacity_round_trips_as_int_even_for_huge_ipv6_capacities(self) -> None:
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

        assert decoded.payload.capacity == huge_capacity
        assert isinstance(decoded.payload.capacity, int)


def _hot_ip_added_envelope(*, event_type: str) -> EventEnvelope:
    payload = HotIpAdded(ip=Address.parse("10.0.0.1"), timestamp=T0, sequence=1, window_count=1000, config_version=1)
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
        except Exception as exc:  # noqa: BLE001 - intentional: prove no other type leaks
            pytest.fail(f"decode() leaked {type(exc).__name__} instead of raising CodecError")
