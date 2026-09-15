"""Malformed IPs, oversized bodies, unaligned windows, client-asserted state.

Spec: section 4 (protocol), section 23 (message identity, context only --
dedup itself is a separate epic), section 36 (security: TLS/auth/authz are
out of scope here, but schema validation, address validation, and size
limits are this epic's job).

`validation/schema.py`, `validation/addresses.py`, `validation/limits.py`,
and `validation/__init__.py` are currently docstring-only stubs, so -- as
with `packages/hammertime-core/.../test_codec.py` before `codec.py` existed
-- this file also defines the assumed public surface. None of the names
below are confirmed by any issue text; they are inferred purely from each
module's one-line docstring, `schemas/observation.v1.json`,
`docs/protocol/observation-v1.md`, and spec section 36. The coder may land
on different names/signatures; reconciling that is the coordinating
session's job, not this file's.

Assumed surface:

    hammertime.ingest.validation.schema.validate_schema(document: dict) -> None
        Validates `document` against schemas/observation.v1.json (required
        fields, types, `additionalProperties: false` at both the top level
        and per-observation, `window_seconds` in [1, 3600], `request_count`
        in [0, 1_000_000_000], `agent_id` length in [1, 128],
        `observations` array length in [1, 10000]). Raises
        SchemaViolationError on any mismatch, including a client-asserted
        `"state"` field (spec section 36's named example of something an
        agent must never be allowed to assert). Deliberately does NOT
        parse `ip` as an address -- the schema only constrains it to
        `type: string` -- so a structurally-valid-but-garbage IP passes
        this layer and is caught by validate_addresses instead.

    hammertime.ingest.validation.addresses.validate_addresses(document: dict) -> None
        Strictly parses every observation's `ip` field as an IPv4 or IPv6
        address. Raises InvalidAddressError if any address is malformed
        (garbage string, out-of-range octet, etc). Assumed to run only
        after validate_schema has already confirmed `ip` is a string.
        Family-agnostic: the schema does not restrict IPv4 vs IPv6, and
        neither does this layer.

    hammertime.ingest.validation.limits.validate_body_size(raw_body: bytes, *, max_body_bytes: int) -> None
        Raises PayloadTooLargeError if `len(raw_body) > max_body_bytes`.
        Operates on the raw, not-yet-JSON-decoded request body, since the
        point is to reject oversized bodies cheaply (protocol doc: 413,
        "never partially applied"). `max_body_bytes` is assumed to be
        caller-supplied (from HAMMERTIME_INGEST_MAX_BODY_BYTES, default
        1_048_576 per .env.example) rather than read from the environment
        internally.

    hammertime.ingest.validation.limits.validate_observation_count(document: dict, *, max_observations: int) -> None
        Raises PayloadTooLargeError if `len(document["observations"]) >
        max_observations`. This is the operational/edge counterpart to
        schema.py's own `maxItems: 10000` (protocol doc distinguishes a
        413 "limit exceeded" edge rejection from a 400 schema violation);
        both are exercised below since both are real per their respective
        module docstrings, and at the documented default
        (HAMMERTIME_INGEST_MAX_OBSERVATIONS=10000) they share a boundary.

    hammertime.ingest.validation.validate_window_alignment(document: dict, *, bucket_seconds: int) -> None
        Raises UnalignedWindowError if `document["window_start"]` is not
        aligned to `bucket_seconds` (docs/protocol/observation-v1.md:
        "ingest rejects unaligned windows rather than silently
        re-bucketing"). Lives in `validation/__init__.py` rather than one
        of the three submodules because alignment depends on external
        config (`bucket_seconds`, from `config/detection.v1.json`) that
        neither the wire schema nor address parsing knows about.

    Exceptions -- SchemaViolationError, InvalidAddressError,
    PayloadTooLargeError, UnalignedWindowError -- are assumed importable
    from `hammertime.ingest.validation` (re-exported from their owning
    submodules), mirroring how `hammertime.core.errors` centralizes
    CodecError/ConfigurationError for hammertime-core.

Explicitly NOT covered here (confirmed out of scope for this epic, per
both epics' GitHub issue bodies): 401/403 auth, 429 rate limiting,
dedup/200-duplicate, and actual bus publishing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hammertime.ingest.validation import (
    InvalidAddressError,
    PayloadTooLargeError,
    SchemaViolationError,
    UnalignedWindowError,
    validate_window_alignment,
)
from hammertime.ingest.validation.addresses import validate_addresses
from hammertime.ingest.validation.limits import validate_body_size, validate_observation_count
from hammertime.ingest.validation.schema import validate_schema

# .env.example: HAMMERTIME_INGEST_MAX_BODY_BYTES=1048576,
# HAMMERTIME_INGEST_MAX_OBSERVATIONS=10000. Treated as caller-supplied
# limits (see module docstring) rather than read from the environment by
# the functions under test.
MAX_BODY_BYTES = 1_048_576
MAX_OBSERVATIONS = 10_000

# config/detection.v1.json is real committed config, not a stub -- read
# its actual bucket_seconds rather than guessing.
_DETECTION_CONFIG = json.loads(
    (Path(__file__).parents[6] / "config" / "detection.v1.json").read_text()
)
BUCKET_SECONDS = _DETECTION_CONFIG["bucket_seconds"]


def _valid_document(**overrides: Any) -> dict[str, Any]:
    """A schema-valid observation message (schemas/observation.v1.json)."""

    doc: dict[str, Any] = {
        "agent_id": "edge-17",
        "sequence": 123456,
        # 10:00:00Z -- seconds-since-epoch is a multiple of every
        # bucket_seconds this repo is likely to configure (10, 30, 60...).
        "window_start": "2026-09-14T10:00:00Z",
        "window_seconds": 60,
        "observations": [{"ip": "192.168.1.42", "request_count": 183}],
    }
    doc.update(overrides)
    return doc


class TestSchemaValidationHappyPath:
    def test_accepts_the_spec_example_message(self) -> None:
        validate_schema(_valid_document())  # must not raise


class TestSchemaValidationRequiredFields:
    @pytest.mark.parametrize(
        "field", ["agent_id", "sequence", "window_start", "window_seconds", "observations"]
    )
    def test_rejects_a_missing_required_field(self, field: str) -> None:
        doc = _valid_document()
        del doc[field]
        with pytest.raises(SchemaViolationError):
            validate_schema(doc)


class TestSchemaValidationUnknownFields:
    def test_rejects_an_unknown_top_level_field(self) -> None:
        doc = _valid_document(unexpected_extra_field="nope")
        with pytest.raises(SchemaViolationError):
            validate_schema(doc)

    def test_rejects_an_unknown_field_within_an_observation_item(self) -> None:
        doc = _valid_document(
            observations=[{"ip": "192.168.1.42", "request_count": 183, "extra": 1}]
        )
        with pytest.raises(SchemaViolationError):
            validate_schema(doc)

    def test_rejects_a_client_asserted_state_field(self) -> None:
        # Spec section 36's explicit named example: an untrusted agent
        # MUST NOT be able to arbitrarily declare `IP = HOT`. Mechanically
        # this is just `additionalProperties: false` rejecting an unknown
        # top-level key, but it gets its own test because the spec calls
        # it out by name as a security requirement, not an accident of
        # schema strictness.
        doc = _valid_document(state="HOT")
        with pytest.raises(SchemaViolationError):
            validate_schema(doc)


class TestSchemaValidationWindowSeconds:
    @pytest.mark.parametrize("value", [1, 3600])
    def test_accepts_boundary_values(self, value: int) -> None:
        validate_schema(_valid_document(window_seconds=value))  # must not raise

    @pytest.mark.parametrize("value", [0, 3601])
    def test_rejects_out_of_range_values(self, value: int) -> None:
        with pytest.raises(SchemaViolationError):
            validate_schema(_valid_document(window_seconds=value))


class TestSchemaValidationObservationsArrayLength:
    def test_accepts_a_single_observation(self) -> None:
        validate_schema(
            _valid_document(observations=[{"ip": "10.0.0.1", "request_count": 1}])
        )  # must not raise

    def test_accepts_exactly_ten_thousand_observations(self) -> None:
        observations = [{"ip": "10.0.0.1", "request_count": 1}] * 10_000
        validate_schema(_valid_document(observations=observations))  # must not raise

    def test_rejects_ten_thousand_and_one_observations(self) -> None:
        observations = [{"ip": "10.0.0.1", "request_count": 1}] * 10_001
        with pytest.raises(SchemaViolationError):
            validate_schema(_valid_document(observations=observations))

    def test_rejects_an_empty_observations_array(self) -> None:
        with pytest.raises(SchemaViolationError):
            validate_schema(_valid_document(observations=[]))


class TestSchemaValidationRequestCount:
    @pytest.mark.parametrize("value", [0, 1_000_000_000])
    def test_accepts_boundary_values(self, value: int) -> None:
        doc = _valid_document(observations=[{"ip": "10.0.0.1", "request_count": value}])
        validate_schema(doc)  # must not raise

    @pytest.mark.parametrize("value", [1_000_000_001, -1])
    def test_rejects_out_of_range_values(self, value: int) -> None:
        doc = _valid_document(observations=[{"ip": "10.0.0.1", "request_count": value}])
        with pytest.raises(SchemaViolationError):
            validate_schema(doc)


class TestSchemaValidationAgentId:
    def test_rejects_an_empty_agent_id(self) -> None:
        with pytest.raises(SchemaViolationError):
            validate_schema(_valid_document(agent_id=""))

    def test_accepts_a_128_character_agent_id(self) -> None:
        validate_schema(_valid_document(agent_id="a" * 128))  # must not raise

    def test_rejects_a_129_character_agent_id(self) -> None:
        with pytest.raises(SchemaViolationError):
            validate_schema(_valid_document(agent_id="a" * 129))


class TestAddressValidation:
    # Schema validation only constrains `ip` to `type: string` -- these
    # documents are otherwise schema-valid, so address parsing is the only
    # layer that can reject them.

    def test_accepts_a_valid_ipv4_address(self) -> None:
        doc = _valid_document(observations=[{"ip": "192.168.1.42", "request_count": 1}])
        validate_addresses(doc)  # must not raise

    def test_accepts_a_valid_ipv6_address(self) -> None:
        doc = _valid_document(observations=[{"ip": "2001:db8::1", "request_count": 1}])
        validate_addresses(doc)  # must not raise

    @pytest.mark.parametrize(
        "bad_ip",
        [
            "not-an-ip-address",
            "999.999.999.999",
            "192.168.1",
            "192.168.1.1.1",
            "gggg::1",
            "",
        ],
    )
    def test_rejects_malformed_ip_addresses(self, bad_ip: str) -> None:
        doc = _valid_document(observations=[{"ip": bad_ip, "request_count": 1}])
        with pytest.raises(InvalidAddressError):
            validate_addresses(doc)

    def test_rejects_the_whole_message_when_any_single_observation_has_a_bad_ip(self) -> None:
        # "Strict address parsing; malformed addresses fail the whole
        # message" (validation/addresses.py docstring) -- a valid address
        # alongside a bad one must still fail.
        doc = _valid_document(
            observations=[
                {"ip": "10.0.0.1", "request_count": 1},
                {"ip": "999.999.999.999", "request_count": 1},
            ]
        )
        with pytest.raises(InvalidAddressError):
            validate_addresses(doc)


class TestWindowAlignment:
    def test_accepts_a_window_start_aligned_to_bucket_seconds(self) -> None:
        # 10:00:00Z: seconds-since-epoch is a multiple of BUCKET_SECONDS
        # for any sane bucket size (10s per config/detection.v1.json).
        doc = _valid_document(window_start="2026-09-14T10:00:00Z")
        validate_window_alignment(doc, bucket_seconds=BUCKET_SECONDS)  # must not raise

    def test_rejects_a_window_start_not_aligned_to_bucket_seconds(self) -> None:
        # 10:00:07Z is not a multiple of a 10-second bucket.
        doc = _valid_document(window_start="2026-09-14T10:00:07Z")
        with pytest.raises(UnalignedWindowError):
            validate_window_alignment(doc, bucket_seconds=BUCKET_SECONDS)


class TestBodySizeLimit:
    def test_accepts_a_body_at_the_configured_max(self) -> None:
        # Pad a valid, otherwise-undersized document up to exactly the
        # limit with bytes appended after the JSON -- validate_body_size
        # only looks at length, not JSON validity.
        raw = json.dumps(_valid_document()).encode("utf-8")
        padded = raw + b" " * (MAX_BODY_BYTES - len(raw))
        assert len(padded) == MAX_BODY_BYTES
        validate_body_size(padded, max_body_bytes=MAX_BODY_BYTES)  # must not raise

    def test_rejects_a_body_over_the_configured_max(self) -> None:
        oversized = b"x" * (MAX_BODY_BYTES + 1)
        with pytest.raises(PayloadTooLargeError):
            validate_body_size(oversized, max_body_bytes=MAX_BODY_BYTES)


class TestObservationCountLimit:
    # limits.py's own "observation-array length" responsibility --
    # distinct from schema.py's maxItems, per the protocol doc's 413
    # (edge limit) vs 400 (schema violation) split, even though both are
    # 10000 by default.

    def test_accepts_exactly_the_configured_max(self) -> None:
        doc = _valid_document(
            observations=[{"ip": "10.0.0.1", "request_count": 1}] * MAX_OBSERVATIONS
        )
        validate_observation_count(doc, max_observations=MAX_OBSERVATIONS)  # must not raise

    def test_rejects_one_more_than_the_configured_max(self) -> None:
        doc = _valid_document(
            observations=[{"ip": "10.0.0.1", "request_count": 1}] * (MAX_OBSERVATIONS + 1)
        )
        with pytest.raises(PayloadTooLargeError):
            validate_observation_count(doc, max_observations=MAX_OBSERVATIONS)
