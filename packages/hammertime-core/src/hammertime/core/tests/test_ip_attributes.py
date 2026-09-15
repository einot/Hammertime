"""Per-IP attributes on the wire (spec section 46, ADR-0005; schemas/ip_attributes.v1.json,
schemas/hot_ip_event.v1.json).

`attributes` is an optional property added to `HotIpAdded`/`HotIpRemoved` and to
`schemas/hot_ip_event.v1.json`'s payload shape. Per ADR-0005 ("Additive and
non-breaking"):

    `attributes` is optional; the codec omits it when absent, so existing
    encodings are byte-identical. `event_id` derives from `(agent_id,
    sequence, event_type, subject)` (ADR-0004), not from payload contents,
    so identity and redelivery dedup are untouched.

(the `event_id`-invariance guarantee is exercised in `test_envelope.py`, not
here.)

These tests assume the codec (`core/events/codec.py`) is the enforcement
point for the shape/size rules of `schemas/ip_attributes.v1.json`, matching
today's established pattern for hot-ip events: `test_codec.py` already
exercises malformed `ip` fields and an oversized `observations` array as
decode-time `CodecError`s via `_tamper_nested` rather than as
constructor-time errors, and ADR-0005 explicitly says "the codec is the
enforcement point" for `schemas/hot_ip_event.v1.json` today. Where a test
needs an otherwise-valid encoded event with only its `attributes` value
replaced, it reaches for the same tamper-after-encode technique.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from hammertime.core.addressing.address import Address
from hammertime.core.errors import CodecError
from hammertime.core.events.codec import decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded, HotIpRemoved

T0 = datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC)


def _hot_ip_envelope(
    event_type: str, *, attributes: dict[str, Any] | None = None, sequence: int = 1
) -> EventEnvelope[HotIpAdded | HotIpRemoved]:
    payload: HotIpAdded | HotIpRemoved
    if event_type == "HotIpAdded":
        payload = HotIpAdded(
            ip=Address.parse("10.0.0.1"),
            timestamp=T0,
            sequence=sequence,
            window_count=1000,
            config_version=1,
            attributes=attributes,
        )
    else:
        payload = HotIpRemoved(
            ip=Address.parse("10.0.0.1"),
            timestamp=T0,
            sequence=sequence,
            window_count=1000,
            config_version=1,
            attributes=attributes,
        )
    return EventEnvelope(
        agent_id="shard-3",
        sequence=sequence,
        event_type=event_type,
        config_version=1,
        timestamp=T0,
        payload=payload,
    )


def _payload(envelope: EventEnvelope[Any]) -> HotIpAdded | HotIpRemoved:
    # decode() returns EventEnvelope[EventPayload], a union of all four
    # payload types this codec knows -- every test in this file only ever
    # decodes a HotIpAdded/HotIpRemoved envelope (built by _hot_ip_envelope
    # above), so this narrows that back down for `.attributes` access.
    payload = envelope.payload
    assert isinstance(payload, HotIpAdded | HotIpRemoved)
    return payload


def _attributes(envelope: EventEnvelope[Any]) -> dict[str, object]:
    # As `_payload` above, but for tests that index a specific key -- also
    # narrows `attributes` itself away from `| None`, since every caller
    # here already knows (by construction) that it built a non-None one.
    attributes = _payload(envelope).attributes
    assert attributes is not None
    return attributes


def _tamper_nested(data: bytes, *, path: tuple[object, ...], value: object) -> bytes:
    """Like test_codec.py's helper of the same name: override a value nested
    inside `payload` of already-encoded wire bytes, without hard-coding the
    rest of the wire shape."""

    doc = json.loads(data)
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return json.dumps(doc).encode("utf-8")


def _with_attributes(data: bytes, attributes: object) -> bytes:
    return _tamper_nested(data, path=("payload", "attributes"), value=attributes)


_HOT_IP_EVENT_TYPES = ("HotIpAdded", "HotIpRemoved")


class TestAttributesAbsentIsNonBreaking:
    """ADR-0005: absent `attributes` must be indistinguishable, on the wire,
    from encodings produced before this change existed."""

    @pytest.mark.parametrize("event_type", _HOT_IP_EVENT_TYPES)
    def test_omitted_attributes_produces_no_attributes_key_on_the_wire(
        self, event_type: str
    ) -> None:
        envelope = _hot_ip_envelope(event_type)  # attributes defaults to None
        document = json.loads(encode(envelope))
        assert "attributes" not in document["payload"]

    @pytest.mark.parametrize("event_type", _HOT_IP_EVENT_TYPES)
    def test_explicit_none_matches_omitted_attributes_byte_for_byte(self, event_type: str) -> None:
        default_envelope = _hot_ip_envelope(event_type)
        explicit_envelope = _hot_ip_envelope(event_type, attributes=None)
        assert encode(default_envelope) == encode(explicit_envelope)

    def test_payload_key_set_is_exactly_the_pre_attributes_set_when_absent(self) -> None:
        # Locks down that no attributes-shaped key sneaks onto the wire when
        # attributes is absent -- the full pre-change field set from
        # schemas/hot_ip_event.v1.json's non-attributes properties.
        envelope = _hot_ip_envelope("HotIpAdded")
        document = json.loads(encode(envelope))
        assert set(document["payload"].keys()) == {
            "type",
            "ip",
            "family",
            "timestamp",
            "sequence",
            "window_count",
            "config_version",
        }

    @pytest.mark.parametrize("event_type", _HOT_IP_EVENT_TYPES)
    def test_decoding_without_attributes_round_trips_to_attributes_none(
        self, event_type: str
    ) -> None:
        envelope = _hot_ip_envelope(event_type)
        decoded = decode(encode(envelope))
        assert _payload(decoded).attributes is None
        assert decoded == envelope


class TestAttributesRoundTrip:
    def test_valid_attributes_round_trip_exactly_on_hot_ip_added(self) -> None:
        attributes = {"attributes_version": 1, "weight": 500}
        envelope = _hot_ip_envelope("HotIpAdded", attributes=attributes)
        decoded = decode(encode(envelope))
        assert _payload(decoded).attributes == attributes

    def test_valid_attributes_round_trip_exactly_on_hot_ip_removed(self) -> None:
        # Spec section 46.5: attributes on a HotIpRemoved MAY be logged (even
        # though they MUST NOT be *stored* by the trie service) -- the
        # transport itself is unaffected either way.
        attributes = {"attributes_version": 1, "weight": 10}
        envelope = _hot_ip_envelope("HotIpRemoved", attributes=attributes)
        decoded = decode(encode(envelope))
        assert _payload(decoded).attributes == attributes

    def test_minimal_document_of_only_attributes_version_round_trips(self) -> None:
        attributes = {"attributes_version": 1}
        envelope = _hot_ip_envelope("HotIpAdded", attributes=attributes)
        decoded = decode(encode(envelope))
        assert _payload(decoded).attributes == attributes


class TestAttributesVersionRequired:
    def test_attributes_missing_attributes_version_is_rejected(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"weight": 500})
        with pytest.raises(CodecError):
            decode(tampered)

    @pytest.mark.parametrize("attributes_version", [0, -1])
    def test_attributes_version_below_schema_minimum_is_rejected(
        self, attributes_version: int
    ) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": attributes_version})
        with pytest.raises(CodecError):
            decode(tampered)


class TestWeightBounds:
    @pytest.mark.parametrize("weight", [0, 1000000])
    def test_weight_accepted_at_schema_bounds(self, weight: int) -> None:
        attributes = {"attributes_version": 1, "weight": weight}
        envelope = _hot_ip_envelope("HotIpAdded", attributes=attributes)
        decoded = decode(encode(envelope))
        assert _attributes(decoded)["weight"] == weight

    @pytest.mark.parametrize("weight", [-1, 1000001])
    def test_weight_outside_schema_bounds_is_rejected(self, weight: int) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "weight": weight})
        with pytest.raises(CodecError):
            decode(tampered)


class TestExperimentalKeys:
    @pytest.mark.parametrize(
        "value",
        ["a free-text experiment value", 12345, {"nested": ["any", "json", 1], "n": None}],
        ids=["string", "number", "nested-object"],
    )
    def test_x_prefixed_keys_are_preserved_verbatim_for_any_json_value(self, value: object) -> None:
        attributes = {"attributes_version": 1, "x_experiment": value}
        envelope = _hot_ip_envelope("HotIpAdded", attributes=attributes)
        decoded = decode(encode(envelope))
        assert _attributes(decoded)["x_experiment"] == value

    def test_x_prefixed_key_pattern_rejects_uppercase(self) -> None:
        # schemas/ip_attributes.v1.json patternProperties: ^x_[a-z0-9_]{1,48}$
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(
            encode(envelope), {"attributes_version": 1, "x_Experiment": "value"}
        )
        with pytest.raises(CodecError):
            decode(tampered)

    def test_x_prefixed_key_pattern_rejects_a_bare_x_with_no_suffix(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "x_": "value"})
        with pytest.raises(CodecError):
            decode(tampered)


class TestUnregisteredNamesRejected:
    @pytest.mark.parametrize("key", ["foo", "wieght", "Weight"])
    def test_unregistered_bare_name_is_rejected(self, key: str) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(
            encode(envelope), {"attributes_version": 1, key: "irrelevant-value"}
        )
        with pytest.raises(CodecError):
            decode(tampered)

    def test_sources_is_reserved_but_not_yet_enabled_and_must_be_rejected(self) -> None:
        # ADR-0005: "`sources` is designed now and enabled later ... a
        # document carrying `sources` is rejected today." It is defined in
        # `$defs` of schemas/ip_attributes.v1.json but deliberately not
        # referenced from `properties`.
        envelope = _hot_ip_envelope("HotIpAdded")
        sources = [{"system": "hammertime.aggregator", "at": "2026-09-14T10:05:00Z"}]
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "sources": sources})
        with pytest.raises(CodecError):
            decode(tampered)


class TestKeyCountCap:
    def test_exactly_sixteen_keys_is_accepted(self) -> None:
        # schemas/ip_attributes.v1.json: maxProperties: 16.
        attributes: dict[str, Any] = {"attributes_version": 1}
        attributes.update({f"x_key_{i}": i for i in range(15)})
        assert len(attributes) == 16
        envelope = _hot_ip_envelope("HotIpAdded", attributes=attributes)
        decoded = decode(encode(envelope))
        assert _payload(decoded).attributes == attributes

    def test_seventeen_keys_is_rejected(self) -> None:
        attributes: dict[str, Any] = {"attributes_version": 1}
        attributes.update({f"x_key_{i}": i for i in range(16)})
        assert len(attributes) == 17
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), attributes)
        with pytest.raises(CodecError):
            decode(tampered)


def _compact_size(document: dict[str, Any]) -> int:
    # Matches codec.py's own _check_attributes_size measurement exactly
    # (separators=(",", ":")) -- json.dumps's default (spaced) separators
    # measure a different, larger byte count, which would leave a padding
    # test's constructed document short of the codec's real 1024-byte
    # compact-encoding boundary instead of adjacent to it.
    return len(json.dumps(document, separators=(",", ":")).encode("utf-8"))


class TestSerializedSizeCap:
    def test_document_over_1024_bytes_serialized_is_rejected(self) -> None:
        # spec section 46.2 / ADR-0005: "serialized size <= 1024 bytes".
        attributes = {"attributes_version": 1, "x_blob": "a" * 2000}
        assert _compact_size(attributes) > 1024
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), attributes)
        with pytest.raises(CodecError):
            decode(tampered)

    def test_document_exactly_at_1024_bytes_serialized_is_accepted(self) -> None:
        # Boundary companion to the oversized-document test above: pad an
        # x_ value so the whole document serializes to exactly the cap
        # (compact encoding, matching the codec's own measurement) and
        # confirm it is still accepted.
        base_attributes: dict[str, Any] = {"attributes_version": 1, "x_blob": ""}
        overhead = _compact_size(base_attributes)
        padding = "a" * (1024 - overhead)
        attributes = {"attributes_version": 1, "x_blob": padding}
        assert _compact_size(attributes) == 1024
        envelope = _hot_ip_envelope("HotIpAdded", attributes=attributes)
        decoded = decode(encode(envelope))
        assert _payload(decoded).attributes == attributes


class TestMalformedAttributesShape:
    def test_attributes_that_is_not_an_object_is_rejected(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), ["not", "an", "object"])
        with pytest.raises(CodecError):
            decode(tampered)

    def test_weight_of_wrong_type_is_rejected(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "weight": "500"})
        with pytest.raises(CodecError):
            decode(tampered)

    def test_explicit_null_attributes_is_rejected_not_treated_as_absent(self) -> None:
        # Regression: schemas/ip_attributes.v1.json's top-level type is
        # "object" -- an explicit `"attributes": null` on the wire is
        # schema-invalid and must be rejected, not silently collapsed to
        # "absent" the way a plain `data.get("attributes")` truthy-check
        # would (None either way).
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), None)
        with pytest.raises(CodecError):
            decode(tampered)


class TestIntegerLikeFloatsAccepted:
    # Regression: JSON Schema's "type": "integer" accepts any number with
    # no fractional part, independent of its literal form -- 1.0 is a valid
    # integer even though isinstance(1.0, int) is False in Python. A
    # non-Python producer may legitimately emit these.
    def test_attributes_version_as_a_whole_number_float_is_accepted(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1.0})
        decode(tampered)  # must not raise

    def test_weight_as_a_whole_number_float_is_accepted(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "weight": 500.0})
        decode(tampered)  # must not raise

    def test_weight_with_a_fractional_part_is_still_rejected(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "weight": 500.5})
        with pytest.raises(CodecError):
            decode(tampered)


class TestNonFiniteAndSurrogateValuesRejected:
    # Regression: Python's json module accepts NaN/Infinity/-Infinity (not
    # valid JSON per RFC 8259) and lone UTF-16 surrogates in strings (valid
    # Python str, not valid UTF-8) by default -- both would otherwise
    # round-trip silently through an x_-prefixed value, producing bytes
    # that corrupt or crash a strict downstream consumer.
    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_in_an_x_prefixed_value_is_rejected(self, bad_value: float) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(
            encode(envelope), {"attributes_version": 1, "x_score": bad_value}
        )
        with pytest.raises(CodecError):
            decode(tampered)

    def test_non_finite_float_nested_inside_an_x_prefixed_value_is_rejected(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(
            encode(envelope),
            {"attributes_version": 1, "x_nested": {"a": [1, 2, float("nan")]}},
        )
        with pytest.raises(CodecError):
            decode(tampered)

    def test_lone_surrogate_in_an_x_prefixed_string_is_rejected(self) -> None:
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "x_s": "\ud800"})
        with pytest.raises(CodecError):
            decode(tampered)


class TestHigherAttributesVersionIsPassedThrough:
    """Spec section 46.2: "A document carrying an attributes_version higher
    than a consumer understands MUST be stored and echoed verbatim and MUST
    NOT be interpreted." This build only registers version 1's fields."""

    def test_unregistered_name_under_a_higher_version_is_accepted_not_rejected(self) -> None:
        # The exact opposite of TestUnregisteredNamesRejected's bare-name
        # cases -- same unregistered key, but under attributes_version 2 it
        # must round-trip untouched instead of being rejected as a typo.
        attributes = {"attributes_version": 2, "severity": 3, "x_experiment": "value"}
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), attributes)
        decoded = decode(tampered)
        assert _payload(decoded).attributes == attributes

    def test_weight_out_of_range_under_a_higher_version_is_not_interpreted(self) -> None:
        # weight's own bounds only apply when this build actually
        # interprets the document (version <= known) -- under a higher
        # version, weight is just another opaque field.
        attributes = {"attributes_version": 2, "weight": 99_999_999}
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), attributes)
        decoded = decode(tampered)
        assert _payload(decoded).attributes == attributes

    def test_structural_bounds_still_apply_under_a_higher_version(self) -> None:
        # Pass-through relaxes name/semantic checks, not the shared
        # wire-hygiene bounds (size, key count) every version obeys.
        attributes = {"attributes_version": 2, "x_blob": "a" * 2000}
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), attributes)
        with pytest.raises(CodecError):
            decode(tampered)

    def test_a_version_at_the_known_maximum_still_enforces_strict_names(self) -> None:
        # Boundary: version == _KNOWN_ATTRIBUTES_VERSION (1) is NOT "higher
        # than this consumer understands" -- an unregistered name at
        # exactly version 1 is still a rejectable typo, not pass-through.
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "severity": 3})
        with pytest.raises(CodecError):
            decode(tampered)


class TestDeeplyNestedAttributesDoesNotCrash:
    def test_deeply_nested_x_prefixed_value_is_rejected_with_codec_error_not_recursion_error(
        self,
    ) -> None:
        # Regression: the serialized-size check re-serializes the decoded
        # document via json.dumps, which can raise a raw RecursionError for
        # adversarially deep nesting at a shallower call depth than
        # decode()'s own top-level json.loads guard -- must surface as
        # CodecError like every other malformed-input path in this module.
        nested: Any = "leaf"
        for _ in range(3000):
            nested = [nested]
        envelope = _hot_ip_envelope("HotIpAdded")
        tampered = _with_attributes(encode(envelope), {"attributes_version": 1, "x_deep": nested})
        with pytest.raises(CodecError):
            decode(tampered)
