"""The one shared section 46.2 validator, and the codec's use of it.

Spec: section 46.2 (the attribute document: registered and experimental names,
the pass-through rule for a higher `attributes_version`, 1024 bytes and 16
keys), section 46.3 (the registry: `attributes_version`, `weight`, the reserved
`sources`), section 46.5 and section 46.8 (what is stored is the canonical
copy, and its size is `ip_attribute_bytes`), section 46.9 (validate size and
shape before storing; values are opaque; nothing a document's own types
define is run). Schema: `schemas/ip_attributes.v1.json`.

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

ADR-0015 Amendment 2 (rulings A-D, assumptions 49-60) adds
`canonicalize_ip_attributes(document) -> CanonicalAttributes`, of which
`validate_ip_attributes` is the measuring form, and the tests after the
"Amendment 2" banner pin it:

* A -- one read, into the copy that is used: a document is read once, through
  the built-in types' own slots, into a tree of exact built-in types; the
  rules, the size and the codec's wire bytes all come from that copy, so a
  subclass whose overrides lie is judged by what it holds.
* B -- bounded work: a running lower bound on the compact size rejects a
  shared subtree, a huge list, dict, string or integer after O(1024) work, and
  never rejects a document of at most 1024 bytes.
* C -- nothing but `InvalidAttributesError` comes out of a document (and
  `CodecError` chained from it out of the codec), whatever its types' own
  methods do.
* E -- subclasses (an `IntEnum`, a `StrEnum`, `str`, `float`, `dict` and `list`
  subclasses) are accepted and copied as their base types; `bool` is refused
  where S3 or R2 need an integer and kept as a JSON boolean elsewhere.
* The canonical text is a fixed point (assumption 59), checked by hypothesis.

Ruling D (the record map) is tested in the trie service's `test_metadata.py`.

ADR-0015 Amendment 3 (rulings 1 and 2, assumptions 61-63) is pinned by the
tests after the "Amendment 3" banners, and by the boundary test beside the
exact-1024 tests of ruling B:

* Ruling 1 -- rule S8: every integer, at any depth, has at most 640 decimal
  digits, sign not counted. 640 digits are accepted and 641 refused under the
  default integer-string limit, under 0 (no limit) and under 640 (the lowest
  legal one), always as `InvalidAttributesError`, never `ValueError`; the
  rule reaches `attributes_version`; the codec chains its `CodecError` from
  the rejection. `sys.int_info.str_digits_check_threshold >= 640` is the one
  premise of assumption 61 a test can see.
* Ruling 2 -- "Bounded work" rule 5: a container met again is not read again,
  but a fresh copy of its canonical copy takes its place. What is pinned is
  the result, not the work (assumption 63; ADR-0014 assumption 17 rules out
  timing assertions): a shared document gives exactly the document, text and
  size of its unshared equivalent, the canonical document is a tree holding
  no input object, sharing counts in full against the size cap, and a cycle
  through two containers is an S7 rejection. These hold before and after the
  fix.
* The coordinator's boundary cases: a list of `null`, `true`, `false`, a
  negative float, a negative int or an escaped `"é"` at exactly 1024 compact
  bytes is accepted, and one more item is not.

`test_ip_attributes.py` is deliberately untouched: it must pass unchanged
against the refactored codec (ADR-0015 assumption 30). `test_codec.py` was
untouched until Amendment 3 ruling 3, whose tests of the codec's own integer
fields live there because they concern the codec, not attributes. This file
copies their envelope-building and tampering pattern rather than importing
it.

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
* Amendment 2: every hostile or lying class consults one switch, `_Traps`,
  and misbehaves only inside `_armed()`. A document is built first and the
  traps are armed afterwards, because building a `dict` hashes (and may
  compare) its keys. Only the two key classes whose lie *is* their hash and
  equality (`_Impersonator`, `_Twin`) lie unconditionally.
* The raising and never-finishing classes are built with `type()` from one
  trap per method name, and each hostile document has a plain twin built by
  the same function from the plain types; "the same outcome as the plain
  document" is then the twin's canonical copy, or its rejection. Which rule
  each twin breaks is pinned by an `accepted` flag.
* `str(exc)` of every rejection in the hostile tests is computed while the
  traps are still armed, so a message composed lazily from a caller's object
  would fail the test.
* Rule R1 applies to top-level keys only, so the nested variant of the lying
  `dict` (ADR-0015 Amendment 2 follow-up A) hides an S4 or S6 violation
  rather than a `sources` key.
* Through `codec.encode`, only documents whose top level is a `dict` are
  sent, because `HotIpAdded.attributes` is typed `dict | None`; the envelope
  is built before the traps are armed, so nothing the event model does at
  construction can spring one.
* The canonical-form property draws exact-type documents only, where the
  ADR promises that the copy equals the input.
* Amendment 3: a test that changes CPython's integer-string limit sets only 0
  or 640 -- 1 to 639 are not legal limits -- and restores the previous one in
  a `finally`. S8's integers are built arithmetically and their digits are
  counted without converting them to text, so no helper depends on the limit
  in force. A rejection's message must not contain a run of 20 zeros, which
  any echo of `10**640` would.
* Amendment 3: every boundary document is built to its exact size by
  measuring its compact encoding; no size is hard-coded but the target.
"""

import contextlib
import copy
import itertools
import json
import math
import sys
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any, NamedTuple

import pytest
from hammertime.core.addressing.address import Address
from hammertime.core.errors import CodecError, HammertimeError, InvalidAttributesError
from hammertime.core.events.attributes import (
    CanonicalAttributes,
    canonicalize_ip_attributes,
    validate_ip_attributes,
)
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


# ==========================================================================
# Amendment 2: the machinery. Everything below builds a document first and
# only then arms the traps (see the module docstring).
# ==========================================================================


class _Traps:
    """The one switch every trapped or lying class in this file consults.

    Off while a document is built -- building a `dict` hashes, and may
    compare, its keys -- and on only inside `_armed()`, around the call under
    test. Nothing here is thread-safe, and nothing needs to be.
    """

    armed = False
    error: type[BaseException] = RuntimeError
    endless = False


@contextlib.contextmanager
def _armed(error: type[BaseException] = RuntimeError, *, endless: bool = False) -> Iterator[None]:
    _Traps.error = error
    _Traps.endless = endless
    _Traps.armed = True
    try:
        yield
    finally:
        _Traps.armed = False
        _Traps.endless = False


# Every method a hostile class overrides, where its base type has it. Armed,
# each raises `_Traps.error`; in "endless" mode the iterating ones instead
# return an iterator that never ends, and the rest tell the truth.
_TRAPPED = (
    "__getattribute__",
    "__hash__",
    "__eq__",
    "__ne__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__repr__",
    "__str__",
    "__format__",
    "__bool__",
    "__len__",
    "__iter__",
    "__reversed__",
    "__contains__",
    "__getitem__",
    "__reduce__",
    "__reduce_ex__",
    "__sizeof__",
    "__getnewargs__",
    "__add__",
    "__mul__",
    "__mod__",
    "__int__",
    "__index__",
    "__float__",
    "__abs__",
    "__neg__",
    "__trunc__",
    "__round__",
    "__floor__",
    "__ceil__",
    "__rshift__",
    "__floordiv__",
    "__truediv__",
    "__pow__",
    "bit_length",
    "to_bytes",
    "is_integer",
    "as_integer_ratio",
    "hex",
    "encode",
    "isascii",
    "join",
    "startswith",
    "split",
    "items",
    "keys",
    "values",
    "get",
    "copy",
    "count",
    "index",
)
_ITERATING = frozenset({"__iter__", "__reversed__", "items", "keys", "values"})


def _trap(base: type, name: str) -> Callable[..., Any]:
    original = getattr(base, name)

    def method(self: object, *args: Any, **kwargs: Any) -> Any:
        if _Traps.armed:
            if not _Traps.endless:
                raise _Traps.error(name)
            if name in _ITERATING:
                return itertools.repeat(("x_a", 1)) if name == "items" else itertools.count()
        return original(self, *args, **kwargs)

    method.__name__ = name
    return method


def _class_trap(self: object) -> type:
    if _Traps.armed and not _Traps.endless:
        raise _Traps.error("__class__")
    return type(self)


def _hostile(base: type) -> Any:
    """A subclass of `base` overriding every method in `_TRAPPED`, and `__class__`."""

    namespace: dict[str, Any] = {
        name: _trap(base, name) for name in _TRAPPED if getattr(base, name, None) is not None
    }
    namespace["__class__"] = property(_class_trap)
    return type(f"_Hostile{base.__name__.title()}", (base,), namespace)


class _Kinds(NamedTuple):
    """Constructors for the five subclassable JSON-model types: str, int,
    float, list, dict. `bool` and `None` cannot be subclassed."""

    t: Callable[[Any], Any]
    n: Callable[[Any], Any]
    r: Callable[[Any], Any]
    a: Callable[[Any], Any]
    o: Callable[[Any], Any]


_PLAIN = _Kinds(str, int, float, list, dict)
_HOSTILE = _Kinds(_hostile(str), _hostile(int), _hostile(float), _hostile(list), _hostile(dict))


class _LyingList(list[Any]):
    """Holds its own content; armed, every overridable read shows `shown`."""

    shown: list[Any]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.shown) if _Traps.armed else list.__iter__(self)

    def __reversed__(self) -> Iterator[Any]:
        return reversed(self.shown) if _Traps.armed else list.__reversed__(self)

    def __len__(self) -> int:
        return len(self.shown) if _Traps.armed else list.__len__(self)

    def __getitem__(self, index: Any) -> Any:
        return self.shown[index] if _Traps.armed else list.__getitem__(self, index)

    def __contains__(self, item: object) -> bool:
        return item in self.shown if _Traps.armed else list.__contains__(self, item)

    def __eq__(self, other: object) -> bool:
        return self.shown == other if _Traps.armed else list.__eq__(self, other)

    def __repr__(self) -> str:
        return repr(self.shown) if _Traps.armed else list.__repr__(self)

    def copy(self) -> Any:
        return list(self.shown) if _Traps.armed else list.copy(self)


class _LyingDict(dict[Any, Any]):
    """Holds its own entries; armed, every overridable read shows `shown`."""

    shown: dict[Any, Any]

    def items(self) -> Any:
        return self.shown.items() if _Traps.armed else dict.items(self)

    def keys(self) -> Any:
        return self.shown.keys() if _Traps.armed else dict.keys(self)

    def values(self) -> Any:
        return self.shown.values() if _Traps.armed else dict.values(self)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.shown) if _Traps.armed else dict.__iter__(self)

    def __getitem__(self, key: Any) -> Any:
        return self.shown[key] if _Traps.armed else dict.__getitem__(self, key)

    def __contains__(self, key: object) -> bool:
        return key in self.shown if _Traps.armed else dict.__contains__(self, key)

    def __len__(self) -> int:
        return len(self.shown) if _Traps.armed else dict.__len__(self)

    def get(self, key: Any, default: Any = None) -> Any:
        return self.shown.get(key, default) if _Traps.armed else dict.get(self, key, default)

    def copy(self) -> Any:
        return dict(self.shown) if _Traps.armed else dict.copy(self)

    def __eq__(self, other: object) -> bool:
        return self.shown == other if _Traps.armed else dict.__eq__(self, other)

    def __repr__(self) -> str:
        return repr(self.shown) if _Traps.armed else dict.__repr__(self)


class _LyingStr(str):
    """Holds its own characters; armed, every overridable read shows `shown`."""

    shown: str

    def __str__(self) -> Any:
        return self.shown if _Traps.armed else str.__str__(self)

    def __repr__(self) -> Any:
        return repr(self.shown) if _Traps.armed else str.__repr__(self)

    def __format__(self, spec: str) -> Any:
        return format(self.shown, spec) if _Traps.armed else str.__format__(self, spec)

    def __iter__(self) -> Any:
        return iter(self.shown) if _Traps.armed else str.__iter__(self)

    def __getitem__(self, key: Any) -> Any:
        return self.shown[key] if _Traps.armed else str.__getitem__(self, key)

    def __len__(self) -> int:
        return len(self.shown) if _Traps.armed else str.__len__(self)

    def __contains__(self, key: Any) -> bool:
        return key in self.shown if _Traps.armed else str.__contains__(self, key)

    def __eq__(self, other: object) -> bool:
        return self.shown == other if _Traps.armed else str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return self.shown != other if _Traps.armed else str.__ne__(self, other)

    def __hash__(self) -> int:
        return hash(self.shown) if _Traps.armed else str.__hash__(self)

    def encode(self, *args: Any, **kwargs: Any) -> Any:
        if _Traps.armed:
            return self.shown.encode(*args, **kwargs)
        return str.encode(self, *args, **kwargs)

    def isascii(self) -> bool:
        return self.shown.isascii() if _Traps.armed else str.isascii(self)


class _LyingInt(int):
    """Holds its own value; armed, every comparison answers True and every
    conversion shows `shown`."""

    shown: int

    def __lt__(self, other: Any) -> bool:
        return True if _Traps.armed else int.__lt__(self, other)

    def __le__(self, other: Any) -> bool:
        return True if _Traps.armed else int.__le__(self, other)

    def __gt__(self, other: Any) -> bool:
        return True if _Traps.armed else int.__gt__(self, other)

    def __ge__(self, other: Any) -> bool:
        return True if _Traps.armed else int.__ge__(self, other)

    def __eq__(self, other: object) -> bool:
        return True if _Traps.armed else int.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return True if _Traps.armed else int.__ne__(self, other)

    def __hash__(self) -> int:
        return int.__hash__(self)

    def __int__(self) -> int:
        return self.shown if _Traps.armed else int.__int__(self)

    def __index__(self) -> int:
        return self.shown if _Traps.armed else int.__index__(self)

    def __float__(self) -> float:
        return float(self.shown) if _Traps.armed else int.__float__(self)

    def bit_length(self) -> int:
        return self.shown.bit_length() if _Traps.armed else int.bit_length(self)

    def __repr__(self) -> str:
        return repr(self.shown) if _Traps.armed else int.__repr__(self)

    def __str__(self) -> str:
        return repr(self.shown) if _Traps.armed else int.__repr__(self)


class _Impersonator(str):
    """Its characters are whatever it was built from; it hashes and compares
    as "weight" always, and armed it prints and measures as "weight" too."""

    def __hash__(self) -> int:
        return hash("weight")

    def __eq__(self, other: object) -> bool:
        return other is self or other == "weight"

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __str__(self) -> Any:
        return "weight" if _Traps.armed else str.__str__(self)

    def __repr__(self) -> Any:
        return repr("weight") if _Traps.armed else str.__repr__(self)

    def __len__(self) -> int:
        return len("weight") if _Traps.armed else str.__len__(self)

    def __iter__(self) -> Any:
        return iter("weight") if _Traps.armed else str.__iter__(self)


class _Twin(str):
    """Equal only to itself and hashed by identity, so two with the same
    characters are two keys of one `dict`."""

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return other is self

    def __ne__(self, other: object) -> bool:
        return other is not self


def _lying_list(held: list[Any], shown: list[Any]) -> _LyingList:
    value = _LyingList(held)
    value.shown = shown
    return value


def _lying_dict(held: dict[Any, Any], shown: dict[Any, Any]) -> _LyingDict:
    value = _LyingDict(held)
    value.shown = shown
    return value


def _lying_str(held: str, shown: str) -> _LyingStr:
    value = _LyingStr(held)
    value.shown = shown
    return value


def _lying_int(held: int, shown: int) -> _LyingInt:
    value = _LyingInt(held)
    value.shown = shown
    return value


# Objects whose class claims, through a `__class__` property, to be `dict`
# or `str` -- enough to pass `isinstance` -- and which quack like one.
_FAKE_DICT_TYPE: Any = type(
    "_FakeDict",
    (),
    {
        "__class__": property(lambda self: dict),
        "items": lambda self: {"attributes_version": 1}.items(),
        "keys": lambda self: {"attributes_version": 1}.keys(),
        "values": lambda self: {"attributes_version": 1}.values(),
        "__iter__": lambda self: iter(["attributes_version"]),
        "__len__": lambda self: 1,
        "__getitem__": lambda self, key: 1,
        "__contains__": lambda self, key: key == "attributes_version",
    },
)
_FAKE_STR_TYPE: Any = type(
    "_FakeStr",
    (),
    {
        "__class__": property(lambda self: str),
        "__str__": lambda self: "x_fake",
        "__len__": lambda self: 6,
        "__iter__": lambda self: iter("x_fake"),
    },
)


class _PlainMapping(Mapping[str, object]):
    """A correct, read-only `collections.abc.Mapping` that is not a `dict`."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class _Version(IntEnum):
    ONE = 1


class _Weight(IntEnum):
    HEAVY = 1450


class _Name(StrEnum):
    X_COLOUR = "x_colour"
    RED = "red"


class _Text(str):
    """A `str` subclass that changes nothing."""


class _Real(float):
    """A `float` subclass that changes nothing."""


class _Obj(dict[str, Any]):
    """A `dict` subclass that changes nothing."""


class _Arr(list[Any]):
    """A `list` subclass that changes nothing."""


_LEAF_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))


def _assert_exact(value: object) -> None:
    """Every value reachable from `value` is an exact built-in JSON-model type
    and every key an exact `str` -- read through `type()` and the base types'
    own slots, so nothing a subclass defines runs."""

    pending: list[Any] = [value]
    visited = 0
    while pending:
        visited += 1
        assert visited < 100_000, "not a finite tree"
        item = pending.pop()
        kind = type(item)
        if kind is dict:
            for key, child in dict.items(item):
                assert type(key) is str, type(key)
                pending.append(child)
        elif kind is list:
            pending.extend(list.__iter__(item))
        else:
            assert any(kind is leaf for leaf in _LEAF_TYPES), kind


def _canonical_or_none(document: object) -> CanonicalAttributes | None:
    try:
        return canonicalize_ip_attributes(document)
    except InvalidAttributesError:
        return None


ENTRY_POINTS = ["canonicalize", "validate", "encode"]


def _through(entry: str, document: object) -> Callable[[], object]:
    """The call under test, bound to `document`. Anything built around the
    document -- the envelope, for `encode` -- is built now, before arming."""

    if entry == "canonicalize":
        return lambda: canonicalize_ip_attributes(document)
    if entry == "validate":
        return lambda: validate_ip_attributes(document)
    untyped: Any = document
    envelope = _hot_ip_added_envelope(attributes=untyped)
    return lambda: encode(envelope)


def _assert_outcome(
    entry: str,
    document: object,
    expected: CanonicalAttributes | None,
    *,
    error: type[BaseException] = RuntimeError,
    endless: bool = False,
) -> None:
    """Decision 5, "What can come out": `document` through `entry` gives
    `expected` -- its canonical copy, size or decoded wire content -- or, when
    `expected` is None, a rejection and nothing else."""

    call = _through(entry, document)
    rejection: type[Exception] = CodecError if entry == "encode" else InvalidAttributesError
    if expected is None:
        with _armed(error, endless=endless):
            with pytest.raises(rejection) as excinfo:
                call()
            message = str(excinfo.value)
        assert len(message) < 1024
        if entry == "encode":
            assert isinstance(excinfo.value.__cause__, InvalidAttributesError)
        return

    with _armed(error, endless=endless):
        result = call()
    if entry == "canonicalize":
        assert isinstance(result, CanonicalAttributes)
        _assert_exact(result.document)
        assert type(result.text) is str
        assert result == expected
    elif entry == "validate":
        assert type(result) is int
        assert result == expected.size
    else:
        assert isinstance(result, bytes)
        decoded: Any = decode(result)
        attributes = decoded.payload.attributes
        _assert_exact(attributes)
        assert attributes == expected.document


# ==========================================================================
# Amendment 2, ruling A: one read, into the copy that is used.
# ==========================================================================

NAN = math.nan

# (build the document, the plain document holding the same content -- or None
# where no plain dict can hold it -- and whether that content is accepted)
LYING_DOCUMENTS: list[Any] = [
    pytest.param(
        lambda: {"attributes_version": 1, "x_l": _lying_list([1], [NAN])},
        {"attributes_version": 1, "x_l": [1]},
        True,
        id="list-holds-1-shows-nan",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_l": _lying_list(["a" * 5000], [1])},
        {"attributes_version": 1, "x_l": ["a" * 5000]},
        False,
        id="list-holds-5000-characters-shows-1",
    ),
    pytest.param(
        lambda: _lying_dict({"attributes_version": 1, "sources": [1]}, {"attributes_version": 1}),
        {"attributes_version": 1, "sources": [1]},
        False,
        id="document-hides-sources",
    ),
    pytest.param(
        lambda: _lying_dict(
            {"attributes_version": 1, "x_ok": 1}, {"attributes_version": 1, "sources": [1]}
        ),
        {"attributes_version": 1, "x_ok": 1},
        True,
        id="document-shows-sources-holds-valid",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": _lying_dict({"k": NAN}, {"k": 1})},
        {"attributes_version": 1, "x_d": {"k": NAN}},
        False,
        id="nested-dict-hides-nan",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": _lying_dict({"k": "a" * 2000}, {"k": 1})},
        {"attributes_version": 1, "x_d": {"k": "a" * 2000}},
        False,
        id="nested-dict-hides-oversize",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": _lying_dict({"k": 1}, {"k": NAN})},
        {"attributes_version": 1, "x_d": {"k": 1}},
        True,
        id="nested-dict-shows-nan-holds-1",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_s": _lying_str("ok\ud800", "ok")},
        {"attributes_version": 1, "x_s": "ok\ud800"},
        False,
        id="str-value-hides-surrogate",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": {_lying_str("k\ud800", "k"): 1}},
        {"attributes_version": 1, "x_d": {"k\ud800": 1}},
        False,
        id="nested-str-key-hides-surrogate",
    ),
    pytest.param(
        lambda: {"attributes_version": 2, _lying_str("\ud800", "ok"): 1},
        {"attributes_version": 2, "\ud800": 1},
        False,
        id="top-level-str-key-hides-surrogate-at-v2",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_s": _lying_str("ok", "\ud800")},
        {"attributes_version": 1, "x_s": "ok"},
        True,
        id="str-value-shows-surrogate-holds-ok",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _lying_str("sources", "x_ok"): 1},
        {"attributes_version": 1, "sources": 1},
        False,
        id="key-holds-sources-shows-x-name",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _lying_str("x_ok", "sources"): 1},
        {"attributes_version": 1, "x_ok": 1},
        True,
        id="key-holds-x-name-shows-sources",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _Impersonator("sources"): 5},
        {"attributes_version": 1, "sources": 5},
        False,
        id="key-equal-to-weight-spelt-sources",
    ),
    pytest.param(
        lambda: {"attributes_version": 2, _Impersonator("sources"): 5},
        {"attributes_version": 2, "sources": 5},
        True,
        id="key-equal-to-weight-spelt-sources-at-v2",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "weight": _lying_int(10**9, 5)},
        {"attributes_version": 1, "weight": 10**9},
        False,
        id="int-weight-holds-10-9",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "weight": _lying_int(5, 10**9)},
        {"attributes_version": 1, "weight": 5},
        True,
        id="int-weight-holds-5",
    ),
    pytest.param(
        lambda: {"attributes_version": _lying_int(0, 1)},
        {"attributes_version": 0},
        False,
        id="int-version-holds-0",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _Twin("x_a"): 1, _Twin("x_a"): 2},
        None,
        False,
        id="two-twin-keys",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_a": 1, _Twin("x_a"): 2},
        None,
        False,
        id="plain-and-twin-key",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": {_Twin("k"): 1, _Twin("k"): 2}},
        None,
        False,
        id="nested-twin-keys",
    ),
]


def test_every_lie_is_live_while_armed_and_the_truth_is_held() -> None:
    """Preconditions for the tests below: each lying type shows its lie to
    ordinary reads while armed and its content otherwise, and the key types
    really do what their names say."""

    listed = _lying_list([1], ["shown"])
    mapped = _lying_dict({"held": 1}, {"shown": 2})
    text = _lying_str("held", "shown")
    number = _lying_int(10**9, 5)
    assert list(listed) == [1]
    assert dict(mapped) == {"held": 1}
    assert str(text) == "held"
    assert int(number) == 10**9
    with _armed():
        assert list(listed) == ["shown"]
        assert len(listed) == 1
        assert dict(mapped) == {"shown": 2}
        assert "held" not in mapped
        assert str(text) == "shown"
        assert list(text) == list("shown")
        assert int(number) == 5
        assert number < 0
        assert number > 10**10
        assert number.bit_length() == 3
    assert str.__str__(text) == "held"

    impersonated = {"attributes_version": 1, _Impersonator("sources"): 5}
    assert "weight" in impersonated
    assert impersonated["weight"] == 5
    assert '"sources"' in json.dumps(impersonated)

    twins = {"attributes_version": 1, _Twin("x_a"): 1, _Twin("x_a"): 2}
    assert len(twins) == 3
    assert [str.__str__(key) for key in twins] == ["attributes_version", "x_a", "x_a"]


@pytest.mark.parametrize(("make", "held", "accepted"), LYING_DOCUMENTS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_the_outcome_follows_the_held_content_not_the_overrides(
    entry: str, make: Factory, held: dict[str, object] | None, accepted: bool
) -> None:
    """Ruling A (assumptions 51-52): a value is read once, through its base
    type's own slots; the rules, the size and the bytes sent all come from
    that copy. Two keys that copy to the same text are refused (S4)."""

    expected = None if held is None else _canonical_or_none(held)
    assert (expected is not None) is accepted
    _assert_outcome(entry, make(), expected)


FAKE_CLASS_DOCUMENTS: list[Any] = [
    pytest.param(lambda: _FAKE_DICT_TYPE(), id="s1-whole-document-claims-dict"),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": _FAKE_DICT_TYPE()}, id="s4-nested-claims-dict"
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_s": _FAKE_STR_TYPE()}, id="s4-nested-claims-str"
    ),
    pytest.param(
        lambda: {"attributes_version": 2, _FAKE_STR_TYPE(): 1}, id="s4-key-claims-str-at-v2"
    ),
    pytest.param(lambda: _FAKE_STR_TYPE(), id="s1-whole-document-claims-str"),
]


def test_a_class_property_is_enough_to_fool_isinstance() -> None:
    """Precondition for the test below (assumption 51)."""

    assert isinstance(_FAKE_DICT_TYPE(), dict)
    assert isinstance(_FAKE_STR_TYPE(), str)


@pytest.mark.parametrize("make", FAKE_CLASS_DOCUMENTS)
def test_an_object_claiming_a_type_through_class_is_not_taken_for_it(make: Factory) -> None:
    """Ruling A: the type is `type(value)`, never `isinstance` (S1, S4)."""

    for entry in ("canonicalize", "validate"):
        _assert_outcome(entry, make(), None)


# ==========================================================================
# Amendment 2, ruling C: nothing but InvalidAttributesError comes out.
# ==========================================================================


def _full(k: _Kinds) -> object:
    """Every kind at every depth; a valid version-1 document."""

    return k.o(
        {
            k.t("attributes_version"): k.n(1),
            k.t("weight"): k.n(1450),
            k.t("x_text"): k.t("café"),
            k.t("x_real"): k.r(2.5),
            k.t("x_list"): k.a(
                [k.n(-3), k.t("a"), k.a([k.r(0.5), None]), k.o({k.t("k"): k.t("v")})]
            ),
            k.t("x_obj"): k.o(
                {k.t("n"): k.a([]), k.t("m"): k.o({}), k.t("t"): True, k.t("f"): False}
            ),
            k.t("x_none"): None,
        }
    )


def _seventeen(k: _Kinds) -> object:
    entries = {k.t(f"x_key_{i}"): k.n(i) for i in range(16)}
    return k.o({k.t("attributes_version"): k.n(1), **entries})


def _v1(k: _Kinds, key: str, value: object) -> object:
    return k.o({k.t("attributes_version"): k.n(1), k.t(key): value})


# (build from a set of kinds, whether the plain twin is accepted). Every top
# level is a dict (see the module docstring); S1 is tested separately below.
SHAPES: list[Any] = [
    pytest.param(_full, True, id="every-kind-nested"),
    pytest.param(lambda k: k.o({k.t("attributes_version"): k.n(1)}), True, id="minimal"),
    pytest.param(
        lambda k: k.o({k.t("attributes_version"): k.r(1.0), k.t("weight"): k.r(500.0)}),
        True,
        id="integral-reals",
    ),
    pytest.param(
        lambda k: k.o({k.t("attributes_version"): k.n(2), k.t("severity"): k.a([k.n(3)])}),
        True,
        id="version-2-pass-through",
    ),
    pytest.param(lambda k: _v1(k, "unregistered", k.n(1)), False, id="r1-unregistered"),
    pytest.param(lambda k: _v1(k, "sources", k.a([])), False, id="r1-sources"),
    pytest.param(lambda k: _v1(k, "weight", k.n(10**9)), False, id="r2-weight-too-big"),
    pytest.param(lambda k: k.o({k.t("attributes_version"): k.n(0)}), False, id="s3-version-0"),
    pytest.param(lambda k: _v1(k, "x_list", k.a([k.r(NAN)])), False, id="s4-nan"),
    pytest.param(lambda k: _v1(k, "x_list", k.a([k.t("\ud800")])), False, id="s5-value"),
    pytest.param(lambda k: _v1(k, "x_obj", k.o({k.t("\udc00"): k.n(1)})), False, id="s5-key"),
    pytest.param(lambda k: _v1(k, "x_text", k.t("a" * 2000)), False, id="s6-long-text"),
    pytest.param(lambda k: _v1(k, "x_list", k.a([k.n(0)] * 600)), False, id="s6-long-list"),
    pytest.param(_seventeen, False, id="s2-17-keys"),
]

MODES = [
    pytest.param(RuntimeError, False, id="raise-RuntimeError"),
    pytest.param(OverflowError, False, id="raise-OverflowError"),
    pytest.param(KeyError, False, id="raise-KeyError"),
    pytest.param(StopIteration, False, id="raise-StopIteration"),
    pytest.param(ZeroDivisionError, False, id="raise-ZeroDivisionError"),
    pytest.param(RuntimeError, True, id="never-finish"),
]


@pytest.mark.parametrize(("error", "endless"), MODES)
@pytest.mark.parametrize(("shape", "accepted"), SHAPES)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_hostile_subclasses_give_exactly_the_plain_documents_outcome(
    entry: str,
    shape: Callable[[_Kinds], object],
    accepted: bool,
    error: type[BaseException],
    endless: bool,
) -> None:
    """Ruling C: keys, values and containers whose every overridable method
    raises, or whose iteration never ends, give the outcome of the plain
    document holding the same content -- nothing else escapes. A regression
    in the never-finish mode hangs; that is the signal."""

    expected = _canonical_or_none(shape(_PLAIN))
    assert (expected is not None) is accepted
    _assert_outcome(entry, shape(_HOSTILE), expected, error=error, endless=endless)


@pytest.mark.parametrize(("error", "endless"), MODES)
@pytest.mark.parametrize("entry", ["canonicalize", "validate"])
def test_a_hostile_list_as_the_whole_document_is_s1(
    entry: str, error: type[BaseException], endless: bool
) -> None:
    document = _HOSTILE.a([_HOSTILE.o({_HOSTILE.t("attributes_version"): _HOSTILE.n(1)})])
    _assert_outcome(entry, document, None, error=error, endless=endless)


@pytest.mark.parametrize("error", [RuntimeError, OverflowError])
def test_a_key_whose_len_raises_is_rejected_and_described_safely(
    error: type[BaseException],
) -> None:
    """Ruling C: a message is composed from the canonical copy, so a key's
    own `__len__` cannot run while the rejection is described."""

    key = _HOSTILE.t("unregistered_name")
    document = {"attributes_version": 1, key: 1}
    with _armed(error):
        with pytest.raises(InvalidAttributesError) as excinfo:
            canonicalize_ip_attributes(document)
        message = str(excinfo.value)
        with pytest.raises(InvalidAttributesError) as validated:
            validate_ip_attributes(document)
        validated_message = str(validated.value)
    assert len(message) < 1024
    assert len(validated_message) < 1024


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: MappingProxyType({"attributes_version": 1}), id="mapping-proxy"),
        pytest.param(lambda: _PlainMapping({"attributes_version": 1}), id="collections-mapping"),
    ],
)
def test_a_top_level_mapping_that_is_not_a_dict_is_refused(make: Factory) -> None:
    """Ruling A.4 / assumption 53: S1 means a `dict`."""

    for entry in ("canonicalize", "validate"):
        _assert_outcome(entry, make(), None)


# ==========================================================================
# Amendment 2, ruling B: bounded work.
# ==========================================================================


def _shared_subtree() -> dict[str, object]:
    shared: list[object] = [0]
    for _ in range(64):
        shared = [shared, shared]
    return {"attributes_version": 1, "x_shared": shared}


BIG_DOCUMENTS: list[Any] = [
    pytest.param(_shared_subtree, id="shared-subtree-64-levels"),
    pytest.param(lambda: {"attributes_version": 1, "x_flat": [0] * 10**6}, id="flat-list-10-6"),
    pytest.param(
        lambda: {"attributes_version": 1, "x_map": {f"k{i}": 0 for i in range(10**5)}},
        id="dict-of-10-5-entries",
    ),
    pytest.param(lambda: {"attributes_version": 1, "x_text": "a" * 10**7}, id="string-10-7"),
    pytest.param(lambda: {"attributes_version": 2, "a" * 10**7: 1}, id="key-10-7-at-v2"),
]


@pytest.mark.parametrize("make", BIG_DOCUMENTS)
@pytest.mark.parametrize("entry", ["canonicalize", "validate"])
def test_a_huge_or_exponential_document_is_rejected_and_the_call_returns(
    entry: str, make: Factory
) -> None:
    """Ruling B: a running lower bound on the compact size rejects these
    after O(1024) work. A regression on the shared subtree hangs (about 2**64
    visits); that is the signal."""

    _assert_outcome(entry, make(), None)


HUGE_INTEGERS: list[Any] = [
    pytest.param(lambda: {"attributes_version": 1, "x_big": 10**5000}, id="x-value"),
    pytest.param(lambda: {"attributes_version": 1, "x_big": [-(10**5000)]}, id="x-negative"),
    pytest.param(lambda: {"attributes_version": 1, "weight": 10**5000}, id="weight"),
    pytest.param(lambda: {"attributes_version": 10**5000}, id="attributes-version"),
]


@pytest.mark.parametrize("make", HUGE_INTEGERS)
@pytest.mark.parametrize("limit", [None, 0], ids=["default-limit", "no-limit"])
def test_a_huge_integer_is_invalid_attributes_never_value_error(
    make: Factory, limit: int | None
) -> None:
    """Ruling B.3 / assumption 54: judged by bit length, so no rejection
    depends on CPython's integer-to-string limit."""

    previous = sys.get_int_max_str_digits()
    try:
        if limit is not None:
            sys.set_int_max_str_digits(limit)
        for entry in ("canonicalize", "validate"):
            _assert_outcome(entry, make(), None)
    finally:
        sys.set_int_max_str_digits(previous)


def _small_ints_at(target: int) -> dict[str, Any]:
    """A list of one- and two-digit ints under an `x_` key, exactly `target` bytes."""

    items: list[int] = []
    document: dict[str, Any] = {"attributes_version": 1, "x_items": items}
    while _size(document) < target:
        items.append(0)
    if _size(document) > target:
        items.pop()
        items[-1] = 10
    assert _size(document) == target
    return document


def _short_keys_at(target: int) -> dict[str, Any]:
    """A dict of many short keys under an `x_` key, exactly `target` bytes."""

    entries: dict[str, int] = {}
    document: dict[str, Any] = {"attributes_version": 1, "x_entries": entries}
    count = 0
    while True:
        entries[f"k{count}"] = 0
        if _size(document) > target:
            del entries[f"k{count}"]
            break
        count += 1
    entries[f"k{count - 1}"] = 10 ** (target - _size(document))
    assert _size(document) == target
    return document


def _nested(depth: int, leaf: list[Any]) -> dict[str, Any]:
    """`depth` levels of list, the innermost being `leaf`, under an `x_` key."""

    value: list[Any] = leaf
    for _ in range(depth - 1):
        value = [value]
    return {"attributes_version": 1, "x_deep": value}


def _deepest_at(target: int) -> tuple[int, list[Any]]:
    """The greatest nesting depth whose document is exactly `target` bytes,
    and the innermost list that makes it exact."""

    spare = target - _size(_nested(1, []))
    leaf: list[Any] = [] if spare % 2 == 0 else [0]
    depth = 1 + spare // 2
    assert _size(_nested(depth, leaf)) == target
    return depth, leaf


def test_many_small_items_at_exactly_1024_bytes_are_accepted_and_one_more_is_not() -> None:
    """Ruling B / assumption 54: the bound under-counts, so it never rejects
    a document of at most 1024 bytes."""

    listed = _small_ints_at(1024)
    assert len(listed["x_items"]) > 400
    assert validate_ip_attributes(listed) == 1024
    assert canonicalize_ip_attributes(listed).size == 1024
    longer = copy.deepcopy(listed)
    longer["x_items"].append(0)
    _assert_outcome("validate", longer, None)

    keyed = _short_keys_at(1024)
    assert len(keyed["x_entries"]) > 100
    assert validate_ip_attributes(keyed) == 1024
    assert canonicalize_ip_attributes(keyed).size == 1024
    wider = copy.deepcopy(keyed)
    count = len(keyed["x_entries"])
    wider["x_entries"][f"k{count}"] = 0
    _assert_outcome("validate", wider, None)


def _one_kind_at(target: int, item: object) -> tuple[dict[str, Any], str]:
    """A list holding only `item`, under an `x_` key, whose document is exactly
    `target` compact bytes; and that key. The list is the longest that fits,
    and the key's name takes up the few bytes an item cannot."""

    items: list[object] = []
    while _size({"attributes_version": 1, "x_a": items}) <= target:
        items.append(item)
    items.pop()
    spare = target - _size({"attributes_version": 1, "x_a": items})
    key = "x_" + "a" * (1 + spare)
    document: dict[str, Any] = {"attributes_version": 1, key: items}
    assert _size(document) == target
    assert len(key) <= 50
    return document, key


BOUNDARY_ITEMS: list[Any] = [
    pytest.param(None, id="null"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(-0.5, id="negative-float"),
    pytest.param(-7, id="negative-int"),
    pytest.param("é", id="escaped-e-acute"),
]


@pytest.mark.parametrize("item", BOUNDARY_ITEMS)
def test_a_list_of_one_kind_at_exactly_1024_bytes_is_accepted_and_one_more_item_is_not(
    item: object,
) -> None:
    """Amendment 3's follow-up boundary cases, by S6's definition of size:
    the running bound under-counts every kind of item, so none rejects a
    document of 1024 bytes, and the exact size rejects 1025 or more."""

    if item == "é":
        assert json.dumps(item) == '"\\u00e9"'
    document, key = _one_kind_at(1024, item)
    assert len(document[key]) > 100
    canonical = canonicalize_ip_attributes(document)
    assert validate_ip_attributes(document) == canonical.size == 1024
    assert canonical.document == document
    assert canonical.text == json.dumps(document, separators=(",", ":"))

    longer = copy.deepcopy(document)
    longer[key].append(item)
    assert _size(longer) > 1024
    for entry in ("canonicalize", "validate"):
        _assert_outcome(entry, longer, None)


def test_the_deepest_nesting_that_fits_is_accepted_and_one_level_more_is_not() -> None:
    depth, leaf = _deepest_at(1024)
    assert depth > 400
    assert validate_ip_attributes(_nested(depth, leaf)) == 1024
    assert canonicalize_ip_attributes(_nested(depth, leaf)).size == 1024
    assert _size(_nested(depth + 1, leaf)) > 1024
    _assert_outcome("validate", _nested(depth + 1, leaf), None)
    _assert_outcome("canonicalize", _nested(depth + 1, leaf), None)


# ==========================================================================
# Follow-up E: subclasses are accepted as their base types; bool.
# ==========================================================================

# (build the document, its plain equivalent)
SUBCLASS_DOCUMENTS: list[Any] = [
    pytest.param(
        lambda: {"attributes_version": _Version.ONE, "weight": _Weight.HEAVY},
        {"attributes_version": 1, "weight": 1450},
        id="int-enum-version-and-weight",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _Name.X_COLOUR: _Name.RED},
        {"attributes_version": 1, "x_colour": "red"},
        id="str-enum-key-and-value",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _Text("x_t"): [_Text("v"), {_Text("k"): _Text("w")}]},
        {"attributes_version": 1, "x_t": ["v", {"k": "w"}]},
        id="str-subclass-keys-and-values",
    ),
    pytest.param(
        lambda: {
            "attributes_version": 1,
            _lying_str("x_key", "sources"): _lying_str("café", "\ud800"),
            "x_n": [_lying_str("ok", "\udfff")],
        },
        {"attributes_version": 1, "x_key": "café", "x_n": ["ok"]},
        id="lying-str-key-and-values",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_r": _Real(2.5), "x_l": [_Real(-0.25)]},
        {"attributes_version": 1, "x_r": 2.5, "x_l": [-0.25]},
        id="float-subclass",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_n": _Arr([_Obj({"k": _Arr([1, _Obj()])}), _Arr()])},
        {"attributes_version": 1, "x_n": [{"k": [1, {}]}, []]},
        id="nested-dict-and-list-subclasses",
    ),
    pytest.param(
        lambda: _Obj({"attributes_version": 1, "weight": 3}),
        {"attributes_version": 1, "weight": 3},
        id="dict-subclass-document",
    ),
    pytest.param(
        lambda: _Obj(
            {
                "attributes_version": _Version.ONE,
                "weight": _Weight.HEAVY,
                _Name.X_COLOUR: _Arr([_Real(0.5), True, None, _Name.RED]),
            }
        ),
        {"attributes_version": 1, "weight": 1450, "x_colour": [0.5, True, None, "red"]},
        id="all-of-them",
    ),
]


@pytest.mark.parametrize(("make", "plain"), SUBCLASS_DOCUMENTS)
def test_subclasses_are_accepted_and_copied_as_their_base_types(
    make: Factory, plain: dict[str, object]
) -> None:
    """Follow-up E / assumption 33 as amended: a subclass passes as its base
    type and is copied as it; the size is the plain equivalent's."""

    document = make()
    with _armed():
        canonical = canonicalize_ip_attributes(document)
        size = validate_ip_attributes(document)
    _assert_exact(canonical.document)
    assert canonical.document == plain
    assert canonical == canonicalize_ip_attributes(plain)
    assert canonical.text == json.dumps(plain, separators=(",", ":"))
    assert size == canonical.size == _size(plain)


@pytest.mark.parametrize(("make", "plain"), SUBCLASS_DOCUMENTS)
def test_the_codec_sends_and_decodes_the_plain_equivalent(
    make: Factory, plain: dict[str, Any]
) -> None:
    """Ruling A.6: `encode` sends the canonical copy, and `decode` carries it."""

    untyped: Any = make()
    envelope = _hot_ip_added_envelope(attributes=untyped)
    with _armed():
        data = encode(envelope)
    assert data == encode(_hot_ip_added_envelope(attributes=plain))
    decoded: Any = decode(data)
    _assert_exact(decoded.payload.attributes)
    assert decoded.payload.attributes == plain
    assert decoded == _hot_ip_added_envelope(attributes=plain)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"attributes_version": True}, id="version-true"),
        pytest.param({"attributes_version": False}, id="version-false"),
        pytest.param({"attributes_version": 1, "weight": True}, id="weight-true"),
        pytest.param({"attributes_version": 1, "weight": False}, id="weight-false"),
    ],
)
def test_a_bool_is_not_an_integer_for_s3_or_r2(document: dict[str, object]) -> None:
    for entry in ENTRY_POINTS:
        _assert_outcome(entry, document, None)


BOOLEANS = {
    "attributes_version": 1,
    "x_true": True,
    "x_false": False,
    "x_list": [True, False, [False]],
    "x_obj": {"b": True},
}


def _assert_booleans_kept(document: Any) -> None:
    assert document == BOOLEANS
    assert document["x_true"] is True
    assert document["x_false"] is False
    assert [type(v) for v in document["x_list"][:2]] == [bool, bool]
    assert document["x_list"][2][0] is False
    assert document["x_obj"]["b"] is True


def test_a_bool_is_a_json_boolean_inside_an_x_value() -> None:
    canonical = canonicalize_ip_attributes(copy.deepcopy(BOOLEANS))
    _assert_booleans_kept(canonical.document)
    assert '"x_true":true' in canonical.text
    assert validate_ip_attributes(BOOLEANS) == _size(BOOLEANS)

    decoded: Any = decode(encode(_hot_ip_added_envelope(attributes=copy.deepcopy(BOOLEANS))))
    _assert_booleans_kept(decoded.payload.attributes)


# ==========================================================================
# The canonical form (assumption 59), over valid exact-type documents.
# ==========================================================================


def _mutate_everywhere(document: dict[str, Any]) -> None:
    """Add to every dict and list in `document`, at every depth."""

    containers: list[Any] = []
    pending: list[Any] = [document]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            containers.append(item)
            pending.extend(item.values())
        elif type(item) is list:
            containers.append(item)
            pending.extend(item)
    for container in containers:
        if type(container) is dict:
            container["zz_mutated"] = "a" * 2000
        else:
            container.append("a" * 2000)


@settings(deadline=None, max_examples=150)
@given(document=st.fixed_dictionaries(_REQUIRED, optional=_OPTIONAL))
def test_the_canonical_copy_is_exact_equal_detached_and_a_fixed_point(
    document: dict[str, Any],
) -> None:
    assume(_size(document) <= 1024)
    canonical = canonicalize_ip_attributes(document)

    assert canonical.size == len(canonical.text) == validate_ip_attributes(document)
    assert canonical.size == _size(document)
    assert canonical.text.isascii()
    assert canonical.text == json.dumps(canonical.document, separators=(",", ":"))
    assert json.dumps(json.loads(canonical.text), separators=(",", ":")) == canonical.text
    assert canonicalize_ip_attributes(json.loads(canonical.text)) == canonical
    assert canonicalize_ip_attributes(canonical.document) == canonical
    _assert_exact(canonical.document)
    assert canonical.document == document
    assert canonical.document is not document

    text = canonical.text
    snapshot = copy.deepcopy(canonical.document)
    _mutate_everywhere(document)
    assert canonical.document == snapshot
    assert canonical.text == text
    assert json.dumps(canonical.document, separators=(",", ":")) == text


# ==========================================================================
# Amendment 3, ruling 1: S8 -- every integer, at any depth, has at most 640
# decimal digits, sign not counted (assumptions 61 and 62).
# ==========================================================================

S8_LARGEST = 10**640 - 1  # 640 nines: the largest magnitude S8 admits
S8_REFUSED = 10**640  # 641 digits: the smallest magnitude S8 refuses


def _digits(value: int) -> int:
    """Decimal digits of `value`, sign not counted, never converting it to text."""

    magnitude = abs(value)
    count = 1
    while magnitude >= 10**count:
        count += 1
    return count


@contextlib.contextmanager
def _int_str_limit(limit: int | None) -> Iterator[None]:
    """CPython's integer-string limit set to `limit` (None: left as it is),
    and the previous limit restored however the block ends."""

    assert limit is None or limit == 0 or limit >= 640
    previous = sys.get_int_max_str_digits()
    try:
        if limit is not None:
            sys.set_int_max_str_digits(limit)
        yield
    finally:
        sys.set_int_max_str_digits(previous)


def _s8_top_level(value: int) -> dict[str, object]:
    return {"attributes_version": 1, "x_n": value}


def _s8_in_a_list(value: int) -> dict[str, object]:
    return {"attributes_version": 1, "x_l": [0, value, "a"]}


def _s8_in_a_nested_dict(value: int) -> dict[str, object]:
    return {"attributes_version": 1, "x_d": {"k": {"j": value}, "m": 1}}


S8_PLACES: list[Any] = [
    pytest.param(_s8_top_level, id="top-level"),
    pytest.param(_s8_in_a_list, id="in-a-list"),
    pytest.param(_s8_in_a_nested_dict, id="in-a-nested-dict"),
]
S8_SIGNS: list[Any] = [pytest.param(1, id="positive"), pytest.param(-1, id="negative")]
INT_STR_LIMITS: list[Any] = [
    pytest.param(None, id="default-limit"),
    pytest.param(0, id="no-limit"),
    pytest.param(640, id="limit-640"),
]


def test_s8_rests_on_cpythons_threshold_of_640() -> None:
    """Assumption 61: the one premise of S8's number a test can see; and this
    file's own arithmetic."""

    assert sys.int_info.str_digits_check_threshold >= 640
    assert _digits(S8_LARGEST) == 640
    assert _digits(-S8_LARGEST) == 640
    assert _digits(S8_REFUSED) == 641
    assert [_digits(n) for n in (0, 9, 10, -99, 100)] == [1, 1, 2, 2, 3]


@pytest.mark.parametrize("limit", INT_STR_LIMITS)
@pytest.mark.parametrize("sign", S8_SIGNS)
@pytest.mark.parametrize("place", S8_PLACES)
def test_s8_640_digits_are_accepted_at_any_depth_under_every_limit(
    place: Callable[[int], dict[str, object]], sign: int, limit: int | None
) -> None:
    """S8: -10**640 < n < 10**640 is admitted anywhere. The canonical document
    equals the input, its size is the compact size, and -- whatever the
    limit, which is what S8 buys (assumption 59 as amended) -- its text
    decodes and canonicalizes back to itself."""

    value = sign * S8_LARGEST
    assert _digits(value) == 640
    document = place(value)
    with _int_str_limit(limit):
        canonical = canonicalize_ip_attributes(document)
        size = validate_ip_attributes(document)
        compact = json.dumps(document, separators=(",", ":"))
        reparsed = json.loads(canonical.text)
        again = canonicalize_ip_attributes(reparsed)
    _assert_exact(canonical.document)
    assert canonical.document == document
    assert canonical.text == compact
    assert size == canonical.size == len(compact.encode("utf-8"))
    assert reparsed == document
    assert again.text == canonical.text


@pytest.mark.parametrize("limit", INT_STR_LIMITS)
@pytest.mark.parametrize("sign", S8_SIGNS)
@pytest.mark.parametrize("place", S8_PLACES)
@pytest.mark.parametrize("entry", ["canonicalize", "validate"])
def test_s8_641_digits_are_invalid_attributes_at_any_depth_under_every_limit(
    entry: str, place: Callable[[int], dict[str, object]], sign: int, limit: int | None
) -> None:
    """S8 and "What can come out": `InvalidAttributesError`, never the
    `ValueError` of the integer-string limit -- `pytest.raises` lets any other
    type fail the test. The message echoes no value: `str(exc)` does not raise
    and holds no run of 20 zeros."""

    value = sign * S8_REFUSED
    assert _digits(value) == 641
    document = place(value)
    with _int_str_limit(limit):
        with pytest.raises(InvalidAttributesError) as excinfo:
            _through(entry, document)()
        message = str(excinfo.value)
    assert "0" * 20 not in message
    assert len(message) < 1024


@pytest.mark.parametrize("entry", ["canonicalize", "validate"])
def test_s8_reaches_attributes_version(entry: str) -> None:
    """Assumption 62: a 641-digit `attributes_version`, which S3 alone passed,
    is refused; a 640-digit one is still a version above 1, stored verbatim
    and not interpreted (section 46.2's pass-through rule)."""

    with pytest.raises(InvalidAttributesError) as excinfo:
        _through(entry, {"attributes_version": S8_REFUSED})()
    message = str(excinfo.value)
    assert "0" * 20 not in message

    version = 10**639
    assert _digits(version) == 640
    higher: dict[str, object] = {"attributes_version": version, "severity": 3, "Weight": "x"}
    text = json.dumps(higher, separators=(",", ":"))
    _assert_outcome(entry, higher, CanonicalAttributes(document=higher, text=text))


@pytest.mark.parametrize(
    "limit", [pytest.param(None, id="default-limit"), pytest.param(0, id="no-limit")]
)
def test_decode_refuses_a_641_digit_attribute_integer_as_an_attributes_rejection(
    limit: int | None,
) -> None:
    """Assumption 61: where the decoding process can parse the integer at all,
    S8 refuses it, and the `CodecError` is chained from the
    `InvalidAttributesError` that assumption 37's `attributes_rejected` count
    relies on. 640 digits decode to equal attributes. (Under a limit of 640,
    `json.loads` refuses 641 digits first, with no such cause; the ADR says
    so, and that case is not pinned here.)"""

    refused = {"attributes_version": 1, "x_l": [S8_REFUSED]}
    admitted = {"attributes_version": 1, "x_l": [-S8_LARGEST]}
    with _int_str_limit(limit):
        refused_wire = _wire_with_attributes(refused)
        with pytest.raises(CodecError) as excinfo:
            decode(refused_wire)
        decoded: Any = decode(_wire_with_attributes(admitted))
    assert isinstance(excinfo.value.__cause__, InvalidAttributesError)
    _assert_exact(decoded.payload.attributes)
    assert decoded.payload.attributes == admitted


def test_encode_refuses_a_641_digit_attribute_integer_as_an_attributes_rejection() -> None:
    envelope = _hot_ip_added_envelope(attributes={"attributes_version": 1, "x_n": -S8_REFUSED})
    with pytest.raises(CodecError) as excinfo:
        encode(envelope)
    assert isinstance(excinfo.value.__cause__, InvalidAttributesError)

    admitted = {"attributes_version": 1, "x_n": S8_LARGEST}
    decoded: Any = decode(encode(_hot_ip_added_envelope(attributes=admitted)))
    assert decoded.payload.attributes == admitted


# ==========================================================================
# Amendment 3, ruling 2: "Bounded work" rule 5 -- each distinct container is
# read at most once, and a repeat is a fresh copy of its canonical copy. Only
# the result is pinned, never the work (assumption 63; ADR-0014 assumption
# 17): these hold before the fix as well as after it.
# ==========================================================================


def _shared_plain() -> dict[str, Any]:
    leaf: list[Any] = [1, "a", None]
    inner: dict[str, Any] = {"k": leaf, "j": [leaf, leaf]}
    return {
        "attributes_version": 1,
        "x_a": inner,
        "x_b": inner,
        "x_c": [inner, leaf, inner],
    }


def _shared_subclasses() -> dict[str, Any]:
    leaf = _Arr([1, "a", None])
    inner = _Obj({"k": leaf, "j": _Arr([leaf, leaf])})
    return {
        "attributes_version": 1,
        "x_a": inner,
        "x_b": inner,
        "x_c": _Arr([inner, leaf, inner]),
    }


def _shared_nested() -> dict[str, Any]:
    """Sharing inside sharing: `a` twice in `b`, `c` holding both, `c` twice in `d`."""

    a: list[Any] = [0, {"z": None}]
    b = _Arr([a, a])
    c: dict[str, Any] = {"b": b, "a": a}
    d = [c, c, b]
    return {"attributes_version": 1, "x_d": d, "x_c": c, "x_a": a}


SHARED_DOCUMENTS: list[Any] = [
    pytest.param(_shared_plain, id="exact-types"),
    pytest.param(_shared_subclasses, id="dict-and-list-subclasses"),
    pytest.param(_shared_nested, id="nested-sharing"),
]


def _containers(value: object) -> list[object]:
    """Every dict and list reachable from `value`, once per position: a
    container shared by three positions is listed three times."""

    found: list[object] = []
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            found.append(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            found.append(item)
            pending.extend(item)
        assert len(found) < 100_000, "not a finite tree"
    return found


@pytest.mark.parametrize("make", SHARED_DOCUMENTS)
def test_rule_5_a_shared_container_gives_its_unshared_equivalents_result(make: Factory) -> None:
    document = make()
    unshared = json.loads(json.dumps(document))
    assert _size(unshared) <= 1024
    expected = canonicalize_ip_attributes(unshared)

    canonical = canonicalize_ip_attributes(document)
    _assert_exact(canonical.document)
    assert canonical.document == unshared == expected.document
    assert canonical.text == expected.text == json.dumps(unshared, separators=(",", ":"))
    assert canonical.size == expected.size == _size(unshared)
    assert validate_ip_attributes(document) == expected.size


@pytest.mark.parametrize("make", SHARED_DOCUMENTS)
def test_rule_5_the_canonical_document_is_a_tree_of_new_containers(make: Factory) -> None:
    """Rule 5: "the canonical document never shares one object between two
    positions" -- and, by ruling A, holds none of the caller's."""

    document = make()
    sources = {id(container) for container in _containers(document)}
    canonical = canonicalize_ip_attributes(document)
    positions = [id(container) for container in _containers(canonical.document)]
    assert len(positions) == len(_containers(document))
    assert len(set(positions)) == len(positions)
    assert sources.isdisjoint(positions)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(_shared_plain, id="exact-types"),
        pytest.param(_shared_subclasses, id="dict-and-list-subclasses"),
    ],
)
def test_rule_5_mutating_one_position_of_the_copy_leaves_every_other_alone(
    make: Callable[[], dict[str, Any]],
) -> None:
    document = make()
    unshared = json.loads(json.dumps(document))
    canonical = canonicalize_ip_attributes(document)

    copied: Any = canonical.document
    copied["x_a"]["k"].append("changed")
    copied["x_a"]["added"] = 1
    assert copied["x_a"]["j"] == unshared["x_a"]["j"]
    assert copied["x_b"] == unshared["x_b"]
    assert copied["x_c"] == unshared["x_c"]
    assert json.loads(json.dumps(document)) == unshared
    assert canonical.text == json.dumps(unshared, separators=(",", ":"))


def _repeated_at(target: int, shared: object) -> dict[str, Any]:
    """`shared` repeated as often as fits in a list under `x_shared`, with an
    `x_pad` string taking up the rest: exactly `target` compact bytes."""

    items: list[object] = []
    document: dict[str, Any] = {"attributes_version": 1, "x_pad": "", "x_shared": items}
    while _size(document) <= target:
        items.append(shared)
    items.pop()
    document["x_pad"] = "a" * (target - _size(document))
    assert _size(document) == target
    return document


REPEATED_CONTAINERS: list[Any] = [
    pytest.param(lambda: {"k": [1, 2]}, id="dict"),
    pytest.param(lambda: [None, {"j": "v"}], id="list"),
    pytest.param(lambda: _Obj({"k": _Arr([1, 2])}), id="dict-subclass"),
    pytest.param(lambda: _Arr([None, _Obj({"j": "v"})]), id="list-subclass"),
]


@pytest.mark.parametrize("make", REPEATED_CONTAINERS)
def test_rule_5_every_repeat_counts_in_full_toward_the_size_cap(make: Factory) -> None:
    """Rule 5: "the running total grows exactly as if the container had been
    read again" -- so one container repeated to exactly 1024 bytes is
    accepted, and one more occurrence is not. Sharing is not refused."""

    shared = make()
    document = _repeated_at(1024, shared)
    assert len(document["x_shared"]) > 50
    canonical = canonicalize_ip_attributes(document)
    assert validate_ip_attributes(document) == canonical.size == 1024
    assert canonical.document == json.loads(json.dumps(document))

    document["x_shared"].append(shared)
    assert _size(document) > 1024
    for entry in ("canonicalize", "validate"):
        _assert_outcome(entry, document, None)


def _emptied_dict() -> dict[str, int]:
    """One live entry, `"": 0`, in a dict that held about 10**5: CPython keeps
    a deleted entry's slot until the dict is next resized (assumption 63)."""

    emptied = {f"k{i}": 0 for i in range(10**5)}
    emptied[""] = 0
    for i in range(10**5):
        del emptied[f"k{i}"]
    assert emptied == {"": 0}
    return emptied


def test_rule_5_a_dict_of_deleted_slots_shared_many_times_is_its_live_entries() -> None:
    """Assumption 63's reproduction, as a result: the auditor's document --
    one emptied dict shared 140 times under an `x_` key -- is accepted and is
    the document built from `dict(emptied)`. No timing is asserted."""

    emptied = _emptied_dict()
    document: dict[str, object] = {"attributes_version": 1, "x_shared": [emptied] * 140}
    rebuilt: dict[str, object] = {"attributes_version": 1, "x_shared": [dict(emptied)] * 140}
    assert _size(document) <= 1024

    canonical = canonicalize_ip_attributes(document)
    assert canonical == canonicalize_ip_attributes(rebuilt)
    assert canonical.document == {"attributes_version": 1, "x_shared": [{"": 0}] * 140}
    assert validate_ip_attributes(document) == canonical.size == _size(document)


def _cycle_through_two_lists() -> dict[str, object]:
    a: list[object] = []
    b = [a, a]
    a.append(b)
    return {"attributes_version": 1, "x_c": a}


def _cycle_entered_at_the_shared_list() -> dict[str, object]:
    a: list[object] = []
    b = [a, a]
    a.append(b)
    return {"attributes_version": 1, "x_c": b}


def _cycle_through_two_dicts() -> dict[str, object]:
    d: dict[str, object] = {}
    e = {"x": d, "y": d}
    d["e"] = e
    return {"attributes_version": 1, "x_c": e}


CYCLES: list[Any] = [
    pytest.param(_cycle_through_two_lists, id="a-holds-b-holds-a-twice"),
    pytest.param(_cycle_entered_at_the_shared_list, id="entered-at-b"),
    pytest.param(_cycle_through_two_dicts, id="two-dicts"),
]


@pytest.mark.parametrize("make", CYCLES)
@pytest.mark.parametrize("entry", ["canonicalize", "validate"])
def test_rule_5_a_container_met_again_while_it_is_being_read_is_a_cycle(
    entry: str, make: Factory
) -> None:
    """Rule 5 / S7: an InvalidAttributesError, never a RecursionError --
    `pytest.raises` lets any other type fail the test."""

    _assert_outcome(entry, make(), None)
