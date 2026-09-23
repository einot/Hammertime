"""The one shared section 46.2 validator, and the codec's use of it.

Spec: section 46.2 (the attribute document: registered and experimental names,
the pass-through rule for a higher `attributes_version`, 1024 bytes and 16
keys), section 46.3 (the registry: `attributes_version`, `weight`, the reserved
`sources`), section 46.9 (validate size and shape before storing; values are
opaque). Schema: `schemas/ip_attributes.v1.json`.

The interface under test is ADR-0015 decision 5:
`hammertime.core.events.attributes.validate_ip_attributes(document) -> int`
checks the structural rules S1-S7 on every document and the registry rules
R1-R2 when `attributes_version <= 1`, returns the document's compact
ASCII-escaped size, and raises `InvalidAttributesError` -- a
`HammertimeError` that is neither a `ValueError` nor a `CodecError`
(assumption 31) -- for every violation and nothing else. The codec calls it on
encode and on decode and chains its `CodecError` from the
`InvalidAttributesError` (assumption 32), including for an explicit
`"attributes": null`. The one wire tightening is S5 on keys (assumption 34).

`test_ip_attributes.py` and `test_codec.py` are deliberately untouched: they
must pass unchanged against the refactored codec (ADR-0015 assumption 30).
This file copies their envelope-building and tampering pattern rather than
importing it.

Choices of this file's own, not dictated by the spec or ADR-0015:

* Every rejected document is built by a factory at test time, so a
  3000-deep or self-referencing structure, or a 5000-digit integer (whose
  `str` raises), never reaches pytest's parameter-id or repr machinery.
* "The message never contains a document value" is checked with a marker
  string and a distinctive out-of-range weight; the "only
  `InvalidAttributesError` escapes" test also requires `str(exc)` itself not
  to raise and to stay under 1024 characters. That bound is mine: ADR-0015
  says a message names the rule and at most 64 characters of a key, so
  anything near a document's own size would mean a value was echoed.
* Rule check order is not contract, so each rejected document breaks the
  rule under test and, where it can, no other.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from hammertime.core.addressing.address import Address
from hammertime.core.errors import CodecError, HammertimeError, InvalidAttributesError
from hammertime.core.events.attributes import validate_ip_attributes
from hammertime.core.events.codec import decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded
from hypothesis import assume, given, settings
from hypothesis import strategies as st

T0 = datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC)

Factory = Callable[[], object]


def _hot_ip_added_envelope(
    *, attributes: dict[str, Any] | None = None, sequence: int = 1
) -> EventEnvelope[HotIpAdded]:
    payload = HotIpAdded(
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
        event_type="HotIpAdded",
        config_version=1,
        timestamp=T0,
        payload=payload,
    )


def _tamper_nested(data: bytes, *, path: tuple[object, ...], value: object) -> bytes:
    """As in test_codec.py / test_ip_attributes.py: override a value nested
    inside already-encoded wire bytes. `json.dumps`'s default `ensure_ascii`
    writes a lone surrogate as a `\\udXXX` escape."""

    doc = json.loads(data)
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return json.dumps(doc).encode("utf-8")


def _wire_with_attributes(attributes: object) -> bytes:
    return _tamper_nested(
        encode(_hot_ip_added_envelope()), path=("payload", "attributes"), value=attributes
    )


def _size(document: object) -> int:
    """ADR-0015 S6 / assumption 35: compact separators, default `ensure_ascii`."""

    return len(json.dumps(document, separators=(",", ":")).encode("utf-8"))


def _padded(size: int, *, version: int = 1) -> dict[str, object]:
    """A document whose compact encoding is exactly `size` ASCII bytes."""

    base: dict[str, object] = {"attributes_version": version, "x_blob": ""}
    document: dict[str, object] = {
        "attributes_version": version,
        "x_blob": "a" * (size - _size(base)),
    }
    assert _size(document) == size
    return document


def _keys(count: int, *, version: int = 1) -> dict[str, object]:
    """`count` top-level keys in all, `attributes_version` included."""

    document: dict[str, object] = {"attributes_version": version}
    document.update({f"x_key_{i}": i for i in range(count - 1)})
    assert len(document) == count
    return document


def _deep_list(depth: int) -> dict[str, object]:
    nested: object = "leaf"
    for _ in range(depth):
        nested = [nested]
    return {"attributes_version": 1, "x_deep": nested}


def _deep_dict(depth: int) -> dict[str, object]:
    nested: object = "leaf"
    for _ in range(depth):
        nested = {"a": nested}
    return {"attributes_version": 1, "x_deep": nested}


def _self_referencing_dict() -> dict[str, object]:
    inner: dict[str, object] = {}
    inner["again"] = inner
    return {"attributes_version": 1, "x_loop": inner}


def _self_referencing_list() -> dict[str, object]:
    inner: list[object] = []
    inner.append(inner)
    return {"attributes_version": 1, "x_loop": inner}


def _self_referencing_top_level() -> dict[str, object]:
    document: dict[str, object] = {"attributes_version": 1}
    document["x_self"] = document
    return document


def _huge_integer() -> int:
    # 5000 digits: above CPython's default int-to-str limit (4300), so its
    # `str` and `repr` raise ValueError. Built arithmetically, never parsed.
    return 10**4999


# ==========================================================================
# The return value (S6's measure; section 46.8's ip_attribute_bytes).
# ==========================================================================

VALID_DOCUMENTS = [
    pytest.param({"attributes_version": 1}, id="minimal"),
    pytest.param(
        {"attributes_version": 1, "weight": 1450, "x_experiment": "any JSON"},
        id="section-46-2-example",
    ),
    pytest.param(
        {"attributes_version": 1, "weight": 0, "x_nested": {"a": [1, 2.5, None, True, "s"]}},
        id="nested-x-value",
    ),
    pytest.param({"attributes_version": 1.0, "weight": 500.0}, id="integral-floats"),
    pytest.param(
        {"attributes_version": 2, "severity": 3, "weight": 99_999_999},
        id="higher-version",
    ),
    pytest.param({"attributes_version": 1, "x_text": "café \U0001f600"}, id="non-ascii"),
    pytest.param(_keys(16), id="sixteen-keys"),
    pytest.param(_padded(1024), id="exactly-1024-bytes"),
]


@pytest.mark.parametrize("document", VALID_DOCUMENTS)
def test_the_return_value_is_the_compact_ascii_escaped_size(document: dict[str, object]) -> None:
    assert validate_ip_attributes(document) == _size(document)


def test_the_minimal_document_is_24_bytes() -> None:
    assert validate_ip_attributes({"attributes_version": 1}) == 24


def test_a_character_outside_the_bmp_counts_as_an_escaped_surrogate_pair() -> None:
    """Assumption 35: twelve bytes (`\\ud83d\\ude00`), not its four UTF-8 bytes."""

    empty = validate_ip_attributes({"attributes_version": 1, "x_e": ""})
    emoji = validate_ip_attributes({"attributes_version": 1, "x_e": "\U0001f600"})
    assert emoji - empty == 12
    accented = validate_ip_attributes({"attributes_version": 1, "x_e": "é"})
    assert accented - empty == 6


_JSON_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=3),
)
_JSON_VALUES = st.recursive(
    _JSON_LEAVES,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.text(max_size=2), inner, max_size=3),
    ),
    max_leaves=6,
)
_REQUIRED: dict[str, st.SearchStrategy[object]] = {"attributes_version": st.just(1)}
_OPTIONAL: dict[str, st.SearchStrategy[object]] = {
    "weight": st.integers(min_value=0, max_value=1_000_000),
    "x_text": st.text(max_size=10),
    "x_data": _JSON_VALUES,
}


@settings(deadline=None, max_examples=100)
@given(document=st.fixed_dictionaries(_REQUIRED, optional=_OPTIONAL))
def test_every_small_valid_document_returns_its_size(document: dict[str, object]) -> None:
    # hypothesis's default text excludes surrogates, and the sizes above keep
    # a draw well under 1024 bytes even at twelve bytes a character; the
    # `assume` only guards that arithmetic.
    assume(_size(document) <= 1024)
    assert validate_ip_attributes(document) == _size(document)


# ==========================================================================
# Structural rules: every document, whatever its attributes_version.
# ==========================================================================

S1_NOT_AN_OBJECT: list[Any] = [
    pytest.param(lambda: None, id="none"),
    pytest.param(lambda: [{"attributes_version": 1}], id="list"),
    pytest.param(lambda: '{"attributes_version": 1}', id="string"),
    pytest.param(lambda: 1, id="int"),
    pytest.param(lambda: (("attributes_version", 1),), id="tuple-of-pairs"),
]

S2_TOO_MANY_KEYS: list[Any] = [
    pytest.param(lambda: _keys(17), id="17-keys"),
    pytest.param(lambda: _keys(17, version=2), id="17-keys-at-version-2"),
]

S3_BAD_VERSION: list[Any] = [
    pytest.param(lambda: {"x_a": 1}, id="missing"),
    pytest.param(lambda: {"attributes_version": None}, id="none"),
    pytest.param(lambda: {"attributes_version": 0}, id="zero"),
    pytest.param(lambda: {"attributes_version": -1}, id="minus-one"),
    pytest.param(lambda: {"attributes_version": "1"}, id="string"),
    pytest.param(lambda: {"attributes_version": True}, id="bool"),
    pytest.param(lambda: {"attributes_version": 1.5}, id="fractional"),
    pytest.param(lambda: {"attributes_version": float("nan")}, id="nan"),
]

S4_NOT_JSON_MODEL: list[Any] = [
    pytest.param(lambda: {"attributes_version": 1, "x_v": (1, 2)}, id="tuple"),
    pytest.param(lambda: {"attributes_version": 1, "x_v": {1, 2}}, id="set"),
    pytest.param(lambda: {"attributes_version": 1, "x_v": b"ab"}, id="bytes"),
    pytest.param(lambda: {"attributes_version": 1, "x_v": object()}, id="object"),
    pytest.param(lambda: {"attributes_version": 1, "x_v": {1: "a"}}, id="nested-int-key"),
    pytest.param(lambda: {"attributes_version": 1, "x_v": (float("nan"),)}, id="tuple-with-nan"),
    pytest.param(lambda: {"attributes_version": 2, 1: "a"}, id="top-level-int-key-at-v2"),
    pytest.param(lambda: {"attributes_version": 1, "x_v": float("inf")}, id="infinity"),
]

S5_LONE_SURROGATE: list[Any] = [
    pytest.param(lambda: {"attributes_version": 1, "x_s": "\ud800"}, id="top-level-value"),
    pytest.param(
        lambda: {"attributes_version": 1, "x_n": {"a": ["ok", "\ud800"]}}, id="nested-value"
    ),
    pytest.param(lambda: {"attributes_version": 1, "x_n": {"\udfff": 1}}, id="nested-key"),
    pytest.param(lambda: {"attributes_version": 2, "\ud800": 1}, id="top-level-key-at-v2"),
]

S6_OVERSIZE: list[Any] = [
    pytest.param(lambda: _padded(1025), id="1025-bytes"),
    pytest.param(lambda: _padded(1025, version=2), id="1025-bytes-at-version-2"),
    pytest.param(lambda: {"attributes_version": 1, "x_e": "é" * 200}, id="200-e-acute"),
]

S7_UNWALKABLE: list[Any] = [
    pytest.param(lambda: _deep_list(3000), id="3000-deep-list"),
    pytest.param(lambda: _deep_dict(3000), id="3000-deep-dict"),
    pytest.param(_self_referencing_dict, id="self-referencing-dict"),
    pytest.param(_self_referencing_list, id="self-referencing-list"),
    pytest.param(_self_referencing_top_level, id="self-referencing-document"),
]


@pytest.mark.parametrize("make", S1_NOT_AN_OBJECT)
def test_s1_a_document_must_be_a_json_object(make: Factory) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(make())


def test_s2_sixteen_keys_are_accepted() -> None:
    assert validate_ip_attributes(_keys(16)) == _size(_keys(16))
    assert validate_ip_attributes(_keys(16, version=2)) == _size(_keys(16, version=2))


@pytest.mark.parametrize("make", S2_TOO_MANY_KEYS)
def test_s2_more_than_sixteen_keys_is_rejected(make: Factory) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(make())


@pytest.mark.parametrize("make", S3_BAD_VERSION)
def test_s3_attributes_version_must_be_a_json_integer_of_at_least_1(make: Factory) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(make())


@pytest.mark.parametrize(
    "version", [pytest.param(1, id="int"), pytest.param(1.0, id="integral-float")]
)
def test_s3_accepts_1_and_1_point_0(version: float) -> None:
    document = {"attributes_version": version}
    assert validate_ip_attributes(document) == _size(document)


@pytest.mark.parametrize("make", S4_NOT_JSON_MODEL)
def test_s4_only_the_json_data_model_is_accepted(make: Factory) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(make())


@pytest.mark.parametrize("make", S5_LONE_SURROGATE)
def test_s5_no_lone_surrogate_in_any_key_or_value(make: Factory) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(make())


def test_s6_exactly_1024_bytes_is_accepted() -> None:
    assert validate_ip_attributes(_padded(1024)) == 1024
    assert validate_ip_attributes(_padded(1024, version=2)) == 1024


@pytest.mark.parametrize("make", S6_OVERSIZE)
def test_s6_more_than_1024_bytes_is_rejected(make: Factory) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(make())


def test_s6_measures_the_escaped_form_not_raw_utf_8() -> None:
    document = {"attributes_version": 1, "x_e": "é" * 200}
    raw = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(raw) < 1024
    assert _size(document) > 1024
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(document)


@pytest.mark.parametrize("make", S7_UNWALKABLE)
def test_s7_unwalkable_structures_are_rejections_not_crashes(make: Factory) -> None:
    """Never a RecursionError, nor json's "Circular reference detected"
    ValueError: `pytest.raises` lets any other exception fail the test."""

    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(make())


# ==========================================================================
# Registry rules: only when attributes_version <= 1.
# ==========================================================================

R1_UNREGISTERED: list[Any] = [
    pytest.param("foo", id="foo"),
    pytest.param("wieght", id="misspelt-weight"),
    pytest.param("Weight", id="capitalised-weight"),
    pytest.param("sources", id="reserved-sources"),
    pytest.param("x_", id="bare-x"),
    pytest.param("x_Upper", id="x-uppercase"),
    pytest.param("x_" + "a" * 49, id="x-suffix-49"),
    pytest.param("x-dash", id="x-dash"),
]


@pytest.mark.parametrize("key", R1_UNREGISTERED)
@pytest.mark.parametrize("version", [pytest.param(1, id="v1"), pytest.param(1.0, id="v1-float")])
def test_r1_an_unregistered_name_is_rejected_at_version_1(key: str, version: float) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes({"attributes_version": version, key: 1})


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("x_" + "a" * 48, id="x-suffix-48"),
        pytest.param("x_a", id="x-suffix-1"),
        pytest.param("x_0_9_z", id="x-digits-and-underscores"),
        pytest.param("weight", id="weight"),
    ],
)
def test_r1_registered_and_experimental_names_are_accepted(key: str) -> None:
    document = {"attributes_version": 1, key: 5}
    assert validate_ip_attributes(document) == _size(document)


@pytest.mark.parametrize(
    "weight",
    [
        pytest.param(0, id="zero"),
        pytest.param(1_000_000, id="max"),
        pytest.param(500.0, id="integral-float"),
    ],
)
def test_r2_weight_in_range_is_accepted(weight: float) -> None:
    document = {"attributes_version": 1, "weight": weight}
    assert validate_ip_attributes(document) == _size(document)


R2_BAD_WEIGHT: list[Any] = [
    pytest.param(-1, id="minus-one"),
    pytest.param(1_000_001, id="above-max"),
    pytest.param(500.5, id="fractional"),
    pytest.param(True, id="bool"),
    pytest.param("500", id="string"),
    pytest.param(None, id="null"),
]


@pytest.mark.parametrize("weight", R2_BAD_WEIGHT)
def test_r2_weight_out_of_range_or_not_an_integer_is_rejected(weight: object) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes({"attributes_version": 1, "weight": weight})


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"attributes_version": 2, "severity": 3}, id="unregistered-name"),
        pytest.param({"attributes_version": 2, "weight": 99_999_999}, id="weight-out-of-range"),
        pytest.param({"attributes_version": 2, "weight": "heavy"}, id="weight-a-string"),
        pytest.param({"attributes_version": 3, "sources": [1], "Weight": 1}, id="version-3"),
        pytest.param({"attributes_version": 2, "x_": [], "x_Upper": {}}, id="bad-x-names"),
    ],
)
def test_names_and_weight_pass_through_above_version_1(document: dict[str, object]) -> None:
    """Section 46.2: a higher version is stored and echoed, not interpreted."""

    assert validate_ip_attributes(document) == _size(document)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: _keys(17, version=2), id="17-keys"),
        pytest.param(lambda: _padded(1025, version=2), id="oversize"),
        pytest.param(lambda: {"attributes_version": 2, "x_v": (1,)}, id="tuple"),
        pytest.param(lambda: {"attributes_version": 2, "any": {"\ud800": 1}}, id="surrogate"),
        pytest.param(lambda: {"attributes_version": 2, "x_loop": _deep_list(3000)}, id="deep"),
    ],
)
def test_structural_rules_still_apply_above_version_1(make: Factory) -> None:
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(make())


# ==========================================================================
# Only InvalidAttributesError escapes, and its message carries no value.
# ==========================================================================

EVERY_BAD_DOCUMENT: list[Any] = [
    *S1_NOT_AN_OBJECT,
    *S2_TOO_MANY_KEYS,
    *S3_BAD_VERSION,
    *S4_NOT_JSON_MODEL,
    *S5_LONE_SURROGATE,
    *S6_OVERSIZE,
    *S7_UNWALKABLE,
    pytest.param(lambda: {"attributes_version": 1, "x_big": _huge_integer()}, id="huge-x-value"),
    pytest.param(
        lambda: {"attributes_version": 1, "x_big": [-_huge_integer()]}, id="huge-negative-nested"
    ),
    pytest.param(lambda: {"attributes_version": _huge_integer()}, id="huge-version"),
    pytest.param(lambda: {"attributes_version": 1, "weight": _huge_integer()}, id="huge-weight"),
    pytest.param(lambda: {"attributes_version": 1, "foo": 1}, id="r1-foo"),
    pytest.param(lambda: {"attributes_version": 1, "weight": -1}, id="r2-negative"),
]


@pytest.mark.parametrize("make", EVERY_BAD_DOCUMENT)
def test_only_invalid_attributes_error_escapes(make: Factory) -> None:
    # `pytest.raises` lets any other exception type -- RecursionError,
    # ValueError, TypeError -- propagate and fail the test.
    with pytest.raises(InvalidAttributesError) as excinfo:
        validate_ip_attributes(make())
    message = str(excinfo.value)
    assert len(message) < 1024


MARKER = "zq-SECRET-MARKER-4471"
BIG_WEIGHT = 987654321

# Invalid documents that each carry the marker (and some the big weight) as a
# value. The first group is plain JSON, so it can also travel on the wire.
_MARKED_ON_THE_WIRE: list[Any] = [
    pytest.param(
        lambda: {"attributes_version": 1, "weight": BIG_WEIGHT, "x_secret": MARKER},
        id="r2-out-of-range",
    ),
    pytest.param(lambda: {"attributes_version": 1, "weight": MARKER}, id="r2-weight-is-marker"),
    pytest.param(lambda: {"attributes_version": 1, "foo": MARKER}, id="r1-value-is-marker"),
    pytest.param(lambda: {"attributes_version": MARKER}, id="s3-version-is-marker"),
    pytest.param(
        lambda: {"attributes_version": 1, "x_secret": MARKER + "a" * 1100}, id="s6-oversize"
    ),
    pytest.param(
        lambda: {**_keys(17), "x_key_0": MARKER, "x_w": BIG_WEIGHT}, id="s2-seventeen-keys"
    ),
]
_MARKED_IN_PROCESS: list[Any] = [
    pytest.param(lambda: {"attributes_version": 1, "x_secret": (MARKER,)}, id="s4-tuple"),
    pytest.param(lambda: {"attributes_version": 1, "x_secret": [MARKER, "\ud800"]}, id="s5"),
    pytest.param(lambda: [MARKER, BIG_WEIGHT], id="s1-list"),
]


def _assert_no_value_in(message: str) -> None:
    assert MARKER not in message, message
    assert str(BIG_WEIGHT) not in message, message


@pytest.mark.parametrize("make", [*_MARKED_ON_THE_WIRE, *_MARKED_IN_PROCESS])
def test_the_message_never_contains_a_document_value(make: Factory) -> None:
    """ADR-0015 decision 5 / section 46.9: values are opaque."""

    with pytest.raises(InvalidAttributesError) as excinfo:
        validate_ip_attributes(make())
    _assert_no_value_in(str(excinfo.value))


def test_a_long_unregistered_key_is_never_quoted_whole() -> None:
    """At most the first 64 characters of a key may appear."""

    key = "unregistered_" + "q" * 187
    assert len(key) == 200
    with pytest.raises(InvalidAttributesError) as excinfo:
        validate_ip_attributes({"attributes_version": 1, key: 1})
    assert key not in str(excinfo.value)


def test_invalid_attributes_error_is_its_own_kind_of_hammertime_error() -> None:
    """Assumption 31: bad input, not a bad call, and not an envelope failure."""

    assert issubclass(InvalidAttributesError, HammertimeError)
    assert not issubclass(InvalidAttributesError, ValueError)
    assert not issubclass(InvalidAttributesError, CodecError)


# ==========================================================================
# The codec: every attributes rejection is a CodecError chained from an
# InvalidAttributesError (assumption 32).
# ==========================================================================


@pytest.mark.parametrize(
    "attributes",
    [
        pytest.param(_keys(17), id="s2-17-keys"),
        pytest.param(_padded(1025), id="s6-oversize"),
        pytest.param({"attributes_version": 1, "foo": 1}, id="r1-foo"),
        pytest.param({"attributes_version": 1, "sources": []}, id="r1-sources"),
        pytest.param({"attributes_version": 1, "weight": 1_000_001}, id="r2-above-max"),
        pytest.param({"attributes_version": 1, "weight": True}, id="r2-bool"),
        pytest.param({"weight": 5}, id="s3-missing-version"),
        pytest.param(["not", "an", "object"], id="s1-list"),
        pytest.param(None, id="explicit-null"),
    ],
)
def test_decode_chains_every_attributes_rejection(attributes: object) -> None:
    with pytest.raises(CodecError) as excinfo:
        decode(_wire_with_attributes(attributes))
    assert isinstance(excinfo.value.__cause__, InvalidAttributesError)


@pytest.mark.parametrize(
    "attributes",
    [
        pytest.param(_keys(17), id="s2-17-keys"),
        pytest.param(_padded(1025), id="s6-oversize"),
        pytest.param({"attributes_version": 1, "foo": 1}, id="r1-foo"),
        pytest.param({"attributes_version": 1, "weight": -1}, id="r2-negative"),
        pytest.param({"attributes_version": 1, "x_t": (1, 2)}, id="s4-tuple"),
        pytest.param({"attributes_version": 1, "x_m": {1: "a"}}, id="s4-int-key"),
        pytest.param({"attributes_version": 1, "x_t": (float("nan"),)}, id="s4-nan-in-tuple"),
    ],
)
def test_encode_chains_every_attributes_rejection(attributes: dict[str, Any]) -> None:
    envelope = _hot_ip_added_envelope(attributes=attributes)
    with pytest.raises(CodecError) as excinfo:
        encode(envelope)
    assert isinstance(excinfo.value.__cause__, InvalidAttributesError)


def test_decode_rejects_a_lone_surrogate_in_a_nested_key() -> None:
    """The one wire tightening (assumption 34): an escaped `\\ud800` in a
    key is as unencodable as UTF-8 as it is in a value."""

    attributes = {"attributes_version": 1, "x_a": {"\ud800": 1}}
    wire = _wire_with_attributes(attributes)
    assert b"\\ud800" in wire
    with pytest.raises(CodecError) as excinfo:
        decode(wire)
    assert isinstance(excinfo.value.__cause__, InvalidAttributesError)
    with pytest.raises(InvalidAttributesError):
        validate_ip_attributes(attributes)


def test_decode_rejects_a_lone_surrogate_in_a_top_level_key_above_version_1() -> None:
    wire = _wire_with_attributes({"attributes_version": 2, "\ud800": 1})
    with pytest.raises(CodecError) as excinfo:
        decode(wire)
    assert isinstance(excinfo.value.__cause__, InvalidAttributesError)


@pytest.mark.parametrize("make", _MARKED_ON_THE_WIRE)
def test_the_codec_message_never_contains_a_document_value(make: Factory) -> None:
    with pytest.raises(CodecError) as excinfo:
        decode(_wire_with_attributes(make()))
    _assert_no_value_in(str(excinfo.value))
    assert isinstance(excinfo.value.__cause__, InvalidAttributesError)
    _assert_no_value_in(str(excinfo.value.__cause__))


def test_the_codec_message_on_encode_never_contains_a_document_value() -> None:
    attributes = {"attributes_version": 1, "weight": BIG_WEIGHT, "x_secret": MARKER}
    envelope = _hot_ip_added_envelope(attributes=attributes)
    with pytest.raises(CodecError) as excinfo:
        encode(envelope)
    _assert_no_value_in(str(excinfo.value))


def test_a_valid_document_still_round_trips_through_the_codec() -> None:
    attributes = {"attributes_version": 1, "weight": 1450, "x_experiment": {"k": [1, "v"]}}
    envelope = _hot_ip_added_envelope(attributes=attributes)
    decoded = decode(encode(envelope))
    assert decoded == envelope
