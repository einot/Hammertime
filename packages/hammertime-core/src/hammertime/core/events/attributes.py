"""The per-IP attribute document's rules, implemented once.

Spec: section 46.2, section 46.3, section 46.9; ADR-0015 decision 5
(assumptions 31-36). Schema: `schemas/ip_attributes.v1.json`.

`validate_ip_attributes` is the only place in the repo that states the
1024-byte cap, the 16-key cap, the registered-name set and the `x_` grammar.
Both enforcement points call it: the event codec, on encode and on decode
(`hammertime.core.events.codec`, which re-raises as `CodecError` chained from
the `InvalidAttributesError`), and the trie service's record map, on every
write (`hammertime.trie.metadata.ip_attributes`).

The rules (ADR-0015 decision 5). Structural, on every document whatever its
`attributes_version`:

* S1 the document is a JSON object (a `dict`);
* S2 at most 16 top-level keys;
* S3 `attributes_version` is present, a JSON integer (an `int` that is not a
  `bool`, or an integral finite `float`) and `>= 1`;
* S4 every value at every depth is in the JSON data model -- `dict` with `str`
  keys, `list`, `str`, `int`, finite `float`, `bool`, `None`; subclasses pass
  as their base type (assumption 33);
* S5 no `str`, key or value, at any depth, holds an unpaired UTF-16
  surrogate;
* S6 the compact, ASCII-escaped serialization is at most 1024 bytes -- the
  size this function returns (assumption 35);
* S7 nesting too deep to walk, and a self-referencing structure, are
  rejections, never a `RecursionError` or `ValueError`.

Registry, only when `attributes_version <= 1` (the highest version this build
interprets; a higher one is stored and echoed verbatim, section 46.2):

* R1 every top-level key is registered (`attributes_version`, `weight`) or
  matches `^x_[a-z0-9_]{1,48}$` -- so the reserved `sources` is rejected;
* R2 `weight`, if present, is a JSON integer (not a `bool`) in
  `[0, 1000000]`.

Every violation is an `InvalidAttributesError` and nothing else escapes. A
message names the rule broken and, where it helps, the top-level key involved
-- at most its first 64 characters -- and never a value from the document
(section 46.9, assumption 36). The order the rules are checked in is not part
of the contract.
"""

import json
import math
import re
from typing import Final, TypeGuard

from hammertime.core.errors import InvalidAttributesError

#: schemas/ip_attributes.v1.json: the whole document's serialized-size cap
#: (section 46.2), measured as `_serialized_size` does.
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
#: Every container level costs at least two bytes of the compact form (`[]`
#: or `{}`), so a document nested deeper than this is necessarily over
#: `_MAX_BYTES`: refusing it early is S6, not a new rule, and it bounds the
#: walk below regardless of the interpreter's recursion limit (S7).
_MAX_DEPTH: Final = _MAX_BYTES // 2


def validate_ip_attributes(document: object) -> int:
    """Check `document` against the section 46.2 rules; return its size.

    The size is `len(json.dumps(document, separators=(",", ":")).encode())`
    with json's default `ensure_ascii` -- the compact form the codec writes,
    and section 46.8's per-record `ip_attribute_bytes`.

    Raises `InvalidAttributesError` for every violation, and nothing else.
    """
    try:
        return _validate(document)
    except RecursionError:
        # S7's backstop. The walk is iterative and depth-bounded, so this is
        # only reachable from a caller already at the edge of the stack.
        # `from None`: the internal error's text is not ours to vouch for,
        # and a message here must never carry a document value.
        raise InvalidAttributesError("attributes is nested too deeply to validate") from None
    except (TypeError, ValueError):
        raise InvalidAttributesError("attributes could not be validated") from None


def _validate(document: object) -> int:
    # S1
    if not isinstance(document, dict):
        raise InvalidAttributesError(
            f"attributes must be a JSON object, got {_type_name(document)}"
        )
    # S2
    if len(document) > _MAX_KEYS:
        raise InvalidAttributesError(
            f"attributes has {len(document)} keys, more than the maximum of {_MAX_KEYS}"
        )
    # S3
    if "attributes_version" not in document:
        raise InvalidAttributesError("attributes is missing the required attributes_version")
    version = document["attributes_version"]
    if not _is_json_integer(version) or version < 1:
        raise InvalidAttributesError("attributes_version must be a JSON integer >= 1")
    # S4, S5, S7 -- before the registry, so that R1 only ever sees str keys.
    _walk(document)
    if version <= _KNOWN_VERSION:
        _check_registry(document)
    # S6
    size = _serialized_size(document)
    if size > _MAX_BYTES:
        raise InvalidAttributesError(
            f"attributes is {size} bytes serialized, more than the maximum of {_MAX_BYTES}"
        )
    return size


def _check_registry(document: dict[str, object]) -> None:
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


def _walk(document: dict[object, object]) -> None:
    """S4, S5 and S7 over every key and value, iteratively.

    `on_path` holds the ids of the containers between the top level and the
    value being visited, so a container met again while it is still on the
    path is a cycle; one merely shared between two branches is not. Each
    stack entry is `(value, depth, top-level key it sits under, leaving)`.
    """
    on_path: set[int] = set()
    stack: list[tuple[object, int, str | None, bool]] = [(document, 1, None, False)]
    while stack:
        value, depth, top, leaving = stack.pop()
        if leaving:
            on_path.discard(id(value))
        elif isinstance(value, str):
            _check_text(value, "a string value", top)
        elif value is None or isinstance(value, int):
            # int includes bool; both are JSON as they stand.
            continue
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise InvalidAttributesError(
                    f"attributes contains a non-finite number{_under(top)}, not valid JSON"
                )
        elif isinstance(value, dict | list):
            if depth > _MAX_DEPTH:
                raise InvalidAttributesError(f"attributes is nested too deeply{_under(top)}")
            if id(value) in on_path:
                raise InvalidAttributesError(f"attributes refers to itself{_under(top)}")
            on_path.add(id(value))
            # Pushed first, so popped only after the whole subtree.
            stack.append((value, depth, top, True))
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise InvalidAttributesError(
                            f"attributes has a {_type_name(key)} key{_under(top)}, not a string"
                        )
                    item_top = key if depth == 1 else top
                    _check_text(key, "a key", item_top)
                    stack.append((item, depth + 1, item_top, False))
            else:
                stack.extend((item, depth + 1, top, False) for item in value)
        else:
            raise InvalidAttributesError(
                f"attributes contains a {_type_name(value)}{_under(top)}, "
                "which is not in the JSON data model"
            )


def _check_text(text: str, what: str, top: str | None) -> None:
    # S5: valid in a Python str, not encodable as UTF-8.
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        raise InvalidAttributesError(
            f"attributes has {what} holding an unpaired UTF-16 surrogate{_under(top)}"
        )


def _serialized_size(document: dict[str, object]) -> int:
    try:
        encoded = json.dumps(document, separators=(",", ":"))
    except (TypeError, ValueError):
        # After the walk, only an integer past CPython's int-to-str digit
        # limit gets here -- thousands of digits, so necessarily over S6.
        raise InvalidAttributesError(
            f"attributes cannot be serialized within {_MAX_BYTES} bytes"
        ) from None
    return len(encoded.encode("utf-8"))


def _is_json_integer(value: object) -> TypeGuard[int | float]:
    """JSON Schema's `"type": "integer"`: any number with no fractional part.

    `1.0` qualifies although `isinstance(1.0, int)` is False; `bool` does not
    although `isinstance(True, int)` is True.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def _under(top: str | None) -> str:
    return "" if top is None else f" under key {_quote(top)}"


def _quote(key: str) -> str:
    # The base-class methods, so that a `str` subclass cannot put anything
    # but the key's own first characters into a message.
    head = str.__getitem__(key, slice(0, _QUOTED_KEY_CHARS))
    suffix = "..." if len(key) > _QUOTED_KEY_CHARS else ""
    return str.__repr__(head) + suffix


def _type_name(value: object) -> str:
    return type(value).__name__[:_QUOTED_KEY_CHARS]
