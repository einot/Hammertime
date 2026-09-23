"""The per-IP attribute document's rules, implemented once.

Spec: section 46.2, section 46.3, section 46.9; ADR-0015 decision 5
(assumptions 31-36, 49-56, 59, 61-63), Amendment 2 (rulings A-C) and
Amendment 3 (rulings 1 and 2). Schema:
`schemas/ip_attributes.v1.json`.

`canonicalize_ip_attributes` is the only place in the repo that states the
1024-byte cap, the 16-key cap, the registered-name set and the `x_` grammar.
Both enforcement points call it and keep what it returns, never their own
argument: the event codec, on encode and on decode
(`hammertime.core.events.codec`, which sends and decodes the canonical copy
and re-raises as `CodecError` chained from the `InvalidAttributesError`), and
the trie service's record map, on every write
(`hammertime.trie.metadata.ip_attributes`, which stores the canonical text).
`validate_ip_attributes` is its measuring form, for checking only.

One read, into the copy that is used (Amendment 2 ruling A). A document is
read exactly once, into a new tree of exact built-in types. What a value is
comes from `type(value)` tested with `issubclass` (`bool` and `None` are
exact, and tested first); a subclass instance is read only through the base
type's own slot functions -- `dict.__len__` / `dict.items`, `list.__len__` /
`list.__iter__`, `str.__len__` / `str.__str__`, `int.__int__` /
`int.bit_length`, `float.__float__` -- so nothing the value's own class
defines ever runs. Keys are read the same way. The rules below are applied to
that copy, and the compact text is produced from it.

Bounded work (ruling B). While reading, a running lower bound on the copy's
compact size is kept, and the document is rejected under S6 the moment it
passes 1024: every length is checked through the base `__len__` before the
value is read, and an `int` is judged by its bit length before it is
converted. The read is iterative, so at most about 1024 values are ever read,
whatever the input's size, sharing or nesting.

Each container read once (Amendment 3 ruling 2, "Bounded work" rule 5). A
source `dict` or `list` met again after it has been read in full is not read
again: a fresh copy of its canonical copy -- that copy read through the same
reader, so built iteratively -- takes its place, and the running bound grows
exactly as a re-read would have made it. The canonical document is therefore
a tree. A container met again while it is still being read is a cycle (S7).
What the cap does not bound is one pass over the entry table of each distinct
`dict`, deleted slots included; rule 5 makes that one pass however often the
dict is shared.

The rules (ADR-0015 decision 5), on the copy. Structural, on every document
whatever its `attributes_version`:

* S1 the document is a JSON object: a `dict`, a subclass read as one; any
  other mapping is refused;
* S2 at most 16 top-level keys;
* S3 `attributes_version` is present, a JSON integer (an `int` that is not a
  `bool`, or an integral finite `float`) and `>= 1`;
* S4 every value at every depth is in the JSON data model -- `dict` with `str`
  keys, `list`, `str`, `int`, finite `float`, `bool`, `None`; a subclass of
  `dict`, `list`, `str`, `int` or `float` is copied as its base type; the keys
  of one object are distinct as text;
* S5 no `str`, key or value, at any depth, holds an unpaired UTF-16
  surrogate;
* S6 the compact, ASCII-escaped serialization of the copy is at most 1024
  bytes -- the size returned;
* S7 nesting too deep, a self-referencing structure and a shared subtree
  repeated past the cap are rejections, never a `RecursionError` or
  `ValueError`;
* S8 every integer, at any depth, has at most 640 decimal digits, the sign
  not counted: `-10**640 < n < 10**640`, compared exactly on the copied
  integer after its bit-length charge, never by converting it to text
  (Amendment 3 ruling 1, assumptions 61 and 62).

Registry, only when `attributes_version <= 1` (the highest version this build
interprets; a higher one is stored and echoed verbatim, section 46.2):

* R1 every top-level key is registered (`attributes_version`, `weight`) or
  matches `^x_[a-z0-9_]{1,48}$` -- so the reserved `sources` is rejected;
* R2 `weight`, if present, is a JSON integer (not a `bool`) in
  `[0, 1000000]`.

What can come out (ruling C): a result, or `InvalidAttributesError`. There is
no blanket `except Exception`; the one conversion kept is of a
`RecursionError` or `ValueError` from the final serialization of the copy,
and since S8 its `ValueError` half is a backstop only (assumption 55). A
message is composed from the copy only: it names the rule broken and, where
it helps, a key -- at most its first 64 characters -- and never a value from
the document (section 46.9, assumption 36). The order the rules are checked in
is not part of the contract.
"""

import json
import math
import re
from collections.abc import Iterator
from typing import Any, Final, NamedTuple, TypeGuard

from hammertime.core.errors import InvalidAttributesError

#: schemas/ip_attributes.v1.json: the whole document's serialized-size cap
#: (section 46.2), measured on the canonical copy's compact text.
_MAX_BYTES: Final = 1024
#: schemas/ip_attributes.v1.json maxProperties.
_MAX_KEYS: Final = 16
#: schemas/ip_attributes.v1.json weight.maximum.
_MAX_WEIGHT: Final = 1_000_000
#: Highest attributes_version whose registered names this build interprets.
#: Deliberately separate from `hammertime.core.state.weight.ATTRIBUTES_VERSION`,
#: the generation the aggregator writes (ADR-0015 assumption 41).
_KNOWN_VERSION: Final = 1
#: Section 46.3's registry for version 1. `sources` is reserved, not registered.
_REGISTERED_KEYS: Final = frozenset({"attributes_version", "weight"})
#: schemas/ip_attributes.v1.json patternProperties: the experimental namespace.
_EXPERIMENTAL_KEY: Final = re.compile(r"^x_[a-z0-9_]{1,48}$")
#: At most this many characters of a key appear in a message (assumption 36).
_QUOTED_KEY_CHARS: Final = 64
#: S5: a UTF-16 surrogate code point, valid in a Python str, not in UTF-8.
_SURROGATE: Final = re.compile("[\ud800-\udfff]")
#: S8: every integer's magnitude is below this -- at most 640 decimal digits,
#: the most every legal CPython integer-string limit converts both ways
#: (assumption 61).
_S8_BOUND: Final = 10**640
#: A lower bound on log10(2), scaled by 100000, for counting an integer's
#: decimal digits from its bit length without ever over-counting.
_LOG10_2_FLOOR: Final = 30102


class CanonicalAttributes(NamedTuple):
    """A document's canonical copy and its compact JSON text (Amendment 2).

    `document` holds exact built-in types only, in a tree no caller has held;
    `text` is `json.dumps(document, separators=(",", ":"))` with json's
    default `ensure_ascii`, so ASCII only, and a fixed point of `json.loads`
    followed by that same `json.dumps` (assumption 59).
    """

    document: dict[str, object]
    text: str

    @property
    def size(self) -> int:
        """The S6 size, in bytes: the text is ASCII, so one byte per character."""
        return len(self.text)


def canonicalize_ip_attributes(document: object) -> CanonicalAttributes:
    """Read `document` once into a canonical copy, check it, and return it with its text.

    Raises `InvalidAttributesError` for every section 46.2 violation, and no
    other exception can be caused by the document.
    """
    copy = _Reader().read(document)
    _check_version_and_registry(copy)
    try:
        text = json.dumps(copy, separators=(",", ":"))
    except RecursionError:
        # Only a caller already at the edge of the stack: the copy is at most
        # about 512 levels deep (every level costs two bytes of S6).
        raise InvalidAttributesError("attributes is nested too deeply to serialize") from None
    except ValueError:
        # A backstop only (assumption 55, as amended by Amendment 3 ruling 1):
        # S8 admits no integer of more than 640 digits, which every legal
        # integer-to-string limit prints, and S4 no non-finite float.
        raise InvalidAttributesError(
            f"attributes cannot be serialized within {_MAX_BYTES} bytes"
        ) from None
    size = len(text)
    if size > _MAX_BYTES:
        raise InvalidAttributesError(
            f"attributes is {size} bytes serialized, more than the maximum of {_MAX_BYTES}"
        )
    return CanonicalAttributes(document=copy, text=text)


def validate_ip_attributes(document: object) -> int:
    """Check `document` against the section 46.2 rules; return its size.

    `canonicalize_ip_attributes(document).size`: the compact, ASCII-escaped
    size of the canonical copy, section 46.8's per-record `ip_attribute_bytes`.
    For checking and measuring only -- anything that stores or sends a
    document keeps the canonical copy instead.

    Raises `InvalidAttributesError` for every violation, and nothing else.
    """
    return canonicalize_ip_attributes(document).size


class _Frame(NamedTuple):
    """One container being copied: the source's entries still to read."""

    #: `dict.items` or `list.__iter__` of the source, never its own methods.
    entries: Iterator[Any]
    #: The copy being filled: a `dict[str, object]` when `keyed`, else a
    #: `list[object]`.
    target: Any
    keyed: bool
    #: The copied top-level key this container sits under; None for the root.
    top: str | None
    #: id() of the container being read, for naming a self-reference (S7)
    #: and for rule 5's memo once it has been read in full.
    source_id: int


class _Reader:
    """The one read of a document (ruling A), under the running bound (ruling B).

    `total` is a lower bound on the compact size of the whole document, given
    what has been read so far: every value not yet read is counted at its
    smallest possible encoding (1 byte, and 2 for a key, `""`), and replaced
    by a larger figure once it is. Nothing here calls a method of a value
    from the document; every read goes through a base type's slot function.

    `read_in_full` is rule 5's memo: the canonical copy of every container
    read in full, keyed by the container's id(). The document keeps every
    source object alive for the whole call, so no id is reused within it.
    """

    def __init__(self) -> None:
        self.total = 0
        self.read_in_full: dict[int, Any] = {}

    def read(self, document: object) -> dict[str, object]:
        if not issubclass(type(document), dict):
            raise InvalidAttributesError(
                f"attributes must be a JSON object, got {_describe(document)}"
            )
        source: Any = document
        count = dict.__len__(source)
        # S2
        if count > _MAX_KEYS:
            raise InvalidAttributesError(
                f"attributes has {count} keys, more than the maximum of {_MAX_KEYS}"
            )
        self._charge(_dict_minimum(count), None)
        root: dict[str, object] = {}
        stack = [_Frame(iter(dict.items(source)), root, True, None, id(source))]
        on_path = {id(source)}
        while stack:
            frame = stack[-1]
            entry: Any = next(frame.entries, _END)
            if entry is _END:
                stack.pop()
                on_path.discard(frame.source_id)
                self.read_in_full[frame.source_id] = frame.target
                continue
            if frame.keyed:
                # dict.items yields exact (key, value) tuples.
                raw_key, raw_value = entry
                key = self._key(raw_key, frame.top)
                if key in frame.target:
                    raise InvalidAttributesError(
                        f"attributes has two keys spelt {_quote(key)}{_under(frame.top)}"
                    )
                top = key if frame.top is None else frame.top
                value, child = self._value(raw_value, top, on_path)
                frame.target[key] = value
            else:
                # Only the root has no top-level key, and the root is a dict.
                top = frame.top or ""
                value, child = self._value(entry, top, on_path)
                frame.target.append(value)
            if child is not None:
                stack.append(child)
                on_path.add(child.source_id)
        return root

    def _key(self, raw: Any, top: str | None) -> str:
        if not issubclass(type(raw), str):
            raise InvalidAttributesError(f"attributes has a key that is not a string{_under(top)}")
        # The container's minimum counted this key as `""`.
        self._charge(str.__len__(raw), top)
        key = str.__str__(raw)
        if _SURROGATE.search(key):
            raise InvalidAttributesError(
                f"attributes has a key holding an unpaired UTF-16 surrogate{_under(top)}"
            )
        return key

    def _value(self, raw: Any, top: str, on_path: set[int]) -> tuple[object, _Frame | None]:
        """Copy one value; a container comes back empty, with the frame that fills it.

        The enclosing container's minimum already counted this value at one
        byte; each branch charges the rest.
        """
        if raw is None:
            self._charge(3, top)
            return None, None
        kind = type(raw)
        if kind is bool:
            self._charge(3 if raw else 4, top)
            return raw, None
        if issubclass(kind, str):
            self._charge(str.__len__(raw) + 1, top)
            text = str.__str__(raw)
            if _SURROGATE.search(text):
                raise InvalidAttributesError(
                    f"attributes has a string value holding an unpaired UTF-16 "
                    f"surrogate{_under(top)}"
                )
            return text, None
        if issubclass(kind, int):
            # Judged by bit length before any conversion (ruling B.3): a value
            # of b >= 1 bits is at least 2**(b-1), which has at least
            # floor((b-1) * log10(2)) + 1 digits.
            bits = int.bit_length(raw)
            self._charge(max(bits - 1, 0) * _LOG10_2_FLOOR // 100_000, top)
            number = int.__int__(raw)
            if number < 0:
                self._charge(1, top)
            # S8, exactly, on the copy: never by converting it to text.
            if not -_S8_BOUND < number < _S8_BOUND:
                raise InvalidAttributesError(
                    f"attributes contains an integer of more than 640 digits{_under(top)}"
                )
            return number, None
        if issubclass(kind, float):
            real = float.__float__(raw)
            if not math.isfinite(real):
                raise InvalidAttributesError(
                    f"attributes contains a non-finite number{_under(top)}, not valid JSON"
                )
            self._charge(len(float.__repr__(real)) - 1, top)
            return real, None
        if issubclass(kind, list):
            source = self._container(raw, top, on_path)
            self._charge(_list_minimum(list.__len__(source)) - 1, top)
            items: list[object] = []
            return items, _Frame(list.__iter__(source), items, False, top, id(source))
        if issubclass(kind, dict):
            source = self._container(raw, top, on_path)
            self._charge(_dict_minimum(dict.__len__(source)) - 1, top)
            entries: dict[str, object] = {}
            return entries, _Frame(iter(dict.items(source)), entries, True, top, id(source))
        raise InvalidAttributesError(
            f"attributes contains a value{_under(top)} that is not in the JSON data model"
        )

    def _container(self, raw: Any, top: str, on_path: set[int]) -> Any:
        """What to read for the container `raw`: itself, or rule 5's copy.

        A container still on the current path is a cycle (S7; the bound would
        reject it anyway, this only names it). One already read in full is
        not read again (rule 5): its canonical copy is read in its place,
        through this same reader, which builds a fresh tree and charges
        exactly what a re-read of `raw` would -- every charge depends only on
        lengths, bit lengths, signs and float values, which the copy shares.
        """
        identity = id(raw)
        if identity in on_path:
            raise InvalidAttributesError(f"attributes refers to itself{_under(top)}")
        return self.read_in_full.get(identity, raw)

    def _charge(self, amount: int, top: str | None) -> None:
        """Raise the running lower bound; reject under S6 the moment it passes the cap."""
        self.total += amount
        if self.total > _MAX_BYTES:
            raise InvalidAttributesError(
                f"attributes is more than {_MAX_BYTES} bytes serialized{_under(top)}"
            )


#: Sentinel for an exhausted frame.
_END: Final = object()


def _list_minimum(count: int) -> int:
    """The smallest compact encoding of a list of `count` items: `[]`, or
    brackets, `count - 1` commas and one byte per item."""
    return 2 if count == 0 else 2 * count + 1


def _dict_minimum(count: int) -> int:
    """The smallest compact encoding of a dict of `count` entries: `{}`, or
    braces, `count - 1` commas, and per entry `""`, a colon and one byte."""
    return 2 if count == 0 else 5 * count + 1


def _check_version_and_registry(document: dict[str, object]) -> None:
    """S3, R1 and R2, on the canonical copy: every value here is an exact type."""
    # S3
    if "attributes_version" not in document:
        raise InvalidAttributesError("attributes is missing the required attributes_version")
    version = document["attributes_version"]
    if not _is_json_integer(version) or version < 1:
        raise InvalidAttributesError("attributes_version must be a JSON integer >= 1")
    if version > _KNOWN_VERSION:
        return
    # R1
    for key in document:
        if key in _REGISTERED_KEYS or _EXPERIMENTAL_KEY.fullmatch(key):
            continue
        raise InvalidAttributesError(
            f"attributes has unregistered key {_quote(key)}: must be one of "
            f"{sorted(_REGISTERED_KEYS)} or match {_EXPERIMENTAL_KEY.pattern}"
        )
    # R2
    if "weight" in document:
        weight = document["weight"]
        if not _is_json_integer(weight) or not 0 <= weight <= _MAX_WEIGHT:
            raise InvalidAttributesError(f"weight must be a JSON integer in [0, {_MAX_WEIGHT}]")


def _is_json_integer(value: object) -> TypeGuard[int | float]:
    """JSON Schema's `"type": "integer"`, on an exact-typed copy.

    `1.0` qualifies; `True` does not, since its type is `bool`, not `int`.
    """
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value) and value.is_integer()


def _describe(value: object) -> str:
    """What a top-level value is, from its type alone, without reading it."""
    if value is None:
        return "null"
    kind = type(value)
    if kind is bool:
        return "a boolean"
    if issubclass(kind, str):
        return "a string"
    if issubclass(kind, int | float):
        return "a number"
    if issubclass(kind, list):
        return "an array"
    return "a value that is not a dict"


def _under(top: str | None) -> str:
    return "" if top is None else f" under key {_quote(top)}"


def _quote(key: str) -> str:
    # `key` is always an exact str from the canonical copy.
    suffix = "..." if len(key) > _QUOTED_KEY_CHARS else ""
    return repr(key[:_QUOTED_KEY_CHARS]) + suffix
