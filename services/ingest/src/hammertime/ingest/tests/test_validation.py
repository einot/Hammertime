"""Malformed IPs, oversized bodies, unaligned windows, client-asserted state.

Spec: section 4 (protocol), section 23 (message identity, context only --
dedup itself is a separate epic), section 36 (security: TLS/auth/authz are
out of scope here, but schema validation, address validation, and size
limits are this epic's job).

Explicitly NOT covered here (confirmed out of scope for this epic, per
both epics' GitHub issue bodies): 401/403 auth, 429 rate limiting,
dedup/200-duplicate, and actual bus publishing.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from hammertime.core.errors import InvalidAddressError
from hammertime.ingest.validation import (
    BodyTooLargeError,
    SchemaValidationError,
    TooManyObservationsError,
    UnalignedWindowError,
)
from hammertime.ingest.validation.addresses import parse_address
from hammertime.ingest.validation.limits import (
    check_body_size,
    check_observation_count,
    check_window_alignment,
)
from hammertime.ingest.validation.schema import ObservationSchemaValidator

# .env.example: HAMMERTIME_INGEST_MAX_BODY_BYTES=1048576,
# HAMMERTIME_INGEST_MAX_OBSERVATIONS=10000.
MAX_BODY_BYTES = 1_048_576
MAX_OBSERVATIONS = 10_000

# config/detection.v1.json is real committed config, not a stub -- read its
# actual bucket_seconds rather than guessing.
_DETECTION_CONFIG = json.loads(
    (Path(__file__).parents[6] / "config" / "detection.v1.json").read_text()
)
BUCKET_SECONDS = _DETECTION_CONFIG["bucket_seconds"]

# ObservationSchemaValidator compiles schemas/observation.v1.json once, the
# same way app.py's lifespan does -- exercising the real, committed schema
# file rather than a hand-rolled copy of it.
_VALIDATOR = ObservationSchemaValidator.from_file()


def _validate_schema(document: dict[str, Any]) -> None:
    _VALIDATOR.validate(document)


def _parse_window_start(value: str) -> datetime:
    return datetime.fromisoformat(value)


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
        _validate_schema(_valid_document())  # must not raise


class TestSchemaValidationRequiredFields:
    @pytest.mark.parametrize(
        "field", ["agent_id", "sequence", "window_start", "window_seconds", "observations"]
    )
    def test_rejects_a_missing_required_field(self, field: str) -> None:
        doc = _valid_document()
        del doc[field]
        with pytest.raises(SchemaValidationError):
            _validate_schema(doc)


class TestSchemaValidationUnknownFields:
    def test_rejects_an_unknown_top_level_field(self) -> None:
        doc = _valid_document(unexpected_extra_field="nope")
        with pytest.raises(SchemaValidationError):
            _validate_schema(doc)

    def test_rejects_an_unknown_field_within_an_observation_item(self) -> None:
        doc = _valid_document(
            observations=[{"ip": "192.168.1.42", "request_count": 183, "extra": 1}]
        )
        with pytest.raises(SchemaValidationError):
            _validate_schema(doc)

    def test_rejects_a_client_asserted_state_field(self) -> None:
        # Spec section 36's explicit named example: an untrusted agent
        # MUST NOT be able to arbitrarily declare `IP = HOT`. Mechanically
        # this is just `additionalProperties: false` rejecting an unknown
        # top-level key, but it gets its own test because the spec calls
        # it out by name as a security requirement, not an accident of
        # schema strictness.
        doc = _valid_document(state="HOT")
        with pytest.raises(SchemaValidationError):
            _validate_schema(doc)


class TestSchemaValidationWindowSeconds:
    @pytest.mark.parametrize("value", [1, 3600])
    def test_accepts_boundary_values(self, value: int) -> None:
        _validate_schema(_valid_document(window_seconds=value))  # must not raise

    @pytest.mark.parametrize("value", [0, 3601])
    def test_rejects_out_of_range_values(self, value: int) -> None:
        with pytest.raises(SchemaValidationError):
            _validate_schema(_valid_document(window_seconds=value))


class TestSchemaValidationObservationsArrayLength:
    def test_accepts_a_single_observation(self) -> None:
        _validate_schema(
            _valid_document(observations=[{"ip": "10.0.0.1", "request_count": 1}])
        )  # must not raise

    def test_accepts_exactly_ten_thousand_observations(self) -> None:
        observations = [{"ip": "10.0.0.1", "request_count": 1}] * 10_000
        _validate_schema(_valid_document(observations=observations))  # must not raise

    def test_rejects_ten_thousand_and_one_observations(self) -> None:
        observations = [{"ip": "10.0.0.1", "request_count": 1}] * 10_001
        with pytest.raises(SchemaValidationError):
            _validate_schema(_valid_document(observations=observations))

    def test_rejects_an_empty_observations_array(self) -> None:
        with pytest.raises(SchemaValidationError):
            _validate_schema(_valid_document(observations=[]))


class TestSchemaValidationRequestCount:
    @pytest.mark.parametrize("value", [0, 1_000_000_000])
    def test_accepts_boundary_values(self, value: int) -> None:
        doc = _valid_document(observations=[{"ip": "10.0.0.1", "request_count": value}])
        _validate_schema(doc)  # must not raise

    @pytest.mark.parametrize("value", [1_000_000_001, -1])
    def test_rejects_out_of_range_values(self, value: int) -> None:
        doc = _valid_document(observations=[{"ip": "10.0.0.1", "request_count": value}])
        with pytest.raises(SchemaValidationError):
            _validate_schema(doc)


class TestSchemaValidationAgentId:
    def test_rejects_an_empty_agent_id(self) -> None:
        with pytest.raises(SchemaValidationError):
            _validate_schema(_valid_document(agent_id=""))

    def test_accepts_a_128_character_agent_id(self) -> None:
        _validate_schema(_valid_document(agent_id="a" * 128))  # must not raise

    def test_rejects_a_129_character_agent_id(self) -> None:
        with pytest.raises(SchemaValidationError):
            _validate_schema(_valid_document(agent_id="a" * 129))


class TestAddressValidation:
    # Schema validation only constrains `ip` to `type: string` -- address
    # parsing (per-entry, per validation/addresses.py) is the only layer
    # that can reject a structurally-valid-but-garbage IP.

    def test_accepts_a_valid_ipv4_address(self) -> None:
        parse_address("192.168.1.42")  # must not raise

    def test_accepts_a_valid_ipv6_address(self) -> None:
        parse_address("2001:db8::1")  # must not raise

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
        with pytest.raises(InvalidAddressError):
            parse_address(bad_ip)


class TestWindowAlignment:
    def test_accepts_a_window_start_aligned_to_bucket_seconds(self) -> None:
        # 10:00:00Z: seconds-since-epoch is a multiple of BUCKET_SECONDS
        # for any sane bucket size (10s per config/detection.v1.json).
        window_start = _parse_window_start("2026-09-14T10:00:00Z")
        check_window_alignment(window_start, bucket_seconds=BUCKET_SECONDS)  # must not raise

    def test_rejects_a_window_start_not_aligned_to_bucket_seconds(self) -> None:
        # 10:00:07Z is not a multiple of a 10-second bucket.
        window_start = _parse_window_start("2026-09-14T10:00:07Z")
        with pytest.raises(UnalignedWindowError):
            check_window_alignment(window_start, bucket_seconds=BUCKET_SECONDS)


class TestBodySizeLimit:
    def test_accepts_a_body_at_the_configured_max(self) -> None:
        # Pad a valid, otherwise-undersized document up to exactly the
        # limit with bytes appended after the JSON -- check_body_size only
        # looks at length, not JSON validity.
        raw = json.dumps(_valid_document()).encode("utf-8")
        padded = raw + b" " * (MAX_BODY_BYTES - len(raw))
        assert len(padded) == MAX_BODY_BYTES
        check_body_size(padded, max_body_bytes=MAX_BODY_BYTES)  # must not raise

    def test_rejects_a_body_over_the_configured_max(self) -> None:
        oversized = b"x" * (MAX_BODY_BYTES + 1)
        with pytest.raises(BodyTooLargeError):
            check_body_size(oversized, max_body_bytes=MAX_BODY_BYTES)


class TestObservationCountLimit:
    # limits.py's own "observation-array length" responsibility --
    # distinct from schema.py's maxItems, per the protocol doc's 413
    # (edge limit) vs 400 (schema violation) split, even though both are
    # 10000 by default.

    def test_accepts_exactly_the_configured_max(self) -> None:
        check_observation_count(MAX_OBSERVATIONS, max_observations=MAX_OBSERVATIONS)  # not raise

    def test_rejects_one_more_than_the_configured_max(self) -> None:
        with pytest.raises(TooManyObservationsError):
            check_observation_count(MAX_OBSERVATIONS + 1, max_observations=MAX_OBSERVATIONS)
