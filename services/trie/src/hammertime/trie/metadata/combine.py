"""Per-type combine() used to accumulate metadata along a lookup path, and the upward view.

Spec: section 3, section 12, section 16, section 17; ADR-0015 decisions 1, 2
and 3 (Amendment 1 rulings 1, 5 and 7).

Two combines move in opposite directions through the trie (ADR-0015 decision 1):

* **Downward** (sections 16, 17): effective_metadata(ip) = combine(root, /1,
  /2, ..., /32) along the path. A metadata value carries its own rule -- sets
  union (`Tags`), bitmasks OR (`Bitmask`), policies use priority/override
  (`Override`, the more specific wins) -- so the trie never assumes that
  everything merges by union. `combine` is associative, idempotent and has
  `EMPTY_METADATA` as its identity; it is commutative only for `Tags` and
  `Bitmask`, and the path supplies the order (least specific first).
* **Upward** (sections 10-12): a parent's `hot_count` is the sum of its parts'.
  `PrefixStats` derives `capacity` and `hot_ratio` from its own prefix and
  count, so a ratio is recomputed at every level and never summed or averaged.
  Nothing here evaluates the section 13 predicate (ADR-0010 decision 1).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Final, Self

from hammertime.core.addressing.address import Address
from hammertime.core.addressing.prefix import Prefix
from hammertime.trie.structure.node import HotTrie

# --------------------------------------------------------------------------
# The downward algebra (decision 3).
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tags:
    """Section 16 "Set metadata": combine(A, B) = A | B (set union)."""

    values: frozenset[str]

    def __post_init__(self) -> None:
        # Amendment 1 ruling 5: checked when built, so no malformed value
        # exists for `declare` or `combine` to meet. No coercion: `Tags("abc")`
        # must not read as three tags, nor a mutable `set` sneak in.
        if not isinstance(self.values, frozenset):
            raise TypeError(
                f"Tags.values must be a frozenset of str, got {type(self.values).__name__}"
            )
        for name in self.values:
            if not isinstance(name, str):
                raise TypeError(f"Tags.values must hold only str, got a {type(name).__name__}")

    @classmethod
    def of(cls, *names: str) -> Self:
        """`Tags.of("internal", "lab")` is `Tags(frozenset({"internal", "lab"}))`."""
        return cls(frozenset(names))


@dataclass(frozen=True, slots=True)
class Bitmask:
    """Section 16 "Bitmask": combine(A, B) = A | B (bitwise OR)."""

    bits: int

    def __post_init__(self) -> None:
        # Amendment 1 ruling 5: a wrong type is a TypeError -- a bool is not a
        # mask -- and a negative mask stays assumption 5's ValueError.
        if isinstance(self.bits, bool) or not isinstance(self.bits, int):
            raise TypeError(f"Bitmask.bits must be an int, got {type(self.bits).__name__}")
        if self.bits < 0:
            raise ValueError("Bitmask.bits must be >= 0")


@dataclass(frozen=True, slots=True)
class Override:
    """Section 16 "Policy": priority/override -- the more specific declaration wins."""

    value: str | int | bool

    def __post_init__(self) -> None:
        # Amendment 1 ruling 5 / assumption 3: str or int, bool included
        # (it is an int), subclasses included; None, containers and floats
        # are refused.
        if not isinstance(self.value, str | int):
            raise TypeError(
                f"Override.value must be a str, int or bool, got {type(self.value).__name__}"
            )


MetadataValue = Tags | Bitmask | Override
Metadata = Mapping[str, MetadataValue]
EMPTY_METADATA: Final[Metadata] = MappingProxyType({})

_KINDS: Final = (Tags, Bitmask, Override)


def combine_values(less_specific: MetadataValue, more_specific: MetadataValue) -> MetadataValue:
    """Combine two values of one kind by that kind's rule.

    An operand that is not a `Tags`, `Bitmask` or `Override` is a `TypeError`
    whatever the other operand is (Amendment 1 ruling 7); two valid values of
    different kinds are a `ValueError` naming both kinds (decision 3).
    """
    _require_value(less_specific, None)
    _require_value(more_specific, None)
    return _combine_checked(None, less_specific, more_specific)


def combine(less_specific: Metadata, more_specific: Metadata) -> Metadata:
    """Key-wise combine of two documents; absence is the per-key identity.

    Both documents are checked whole -- each a `Mapping` of `str` to one of
    the three kinds, else `TypeError` -- before any key is combined
    (Amendment 1 ruling 7). A key in both is `combine_values` of the two, and
    two kinds under one key is a `ValueError` naming the key and both kinds.
    """
    _require_metadata(less_specific)
    _require_metadata(more_specific)
    result: dict[str, MetadataValue] = dict(less_specific)
    for key, value in more_specific.items():
        present = result.get(key)
        result[key] = value if present is None else _combine_checked(key, present, value)
    return MappingProxyType(result)


def combine_path(documents: Iterable[Metadata]) -> Metadata:
    """Left fold of `combine` from `EMPTY_METADATA`, least specific first.

    That order is section 16's path order and is what gives `Override` its
    meaning. Raises at the first step that fails, in iteration order.
    """
    result: Metadata = EMPTY_METADATA
    for document in documents:
        result = combine(result, document)
    return result


def _combine_checked(
    key: str | None, less_specific: MetadataValue, more_specific: MetadataValue
) -> MetadataValue:
    less_kind = _kind(less_specific)
    more_kind = _kind(more_specific)
    if less_kind is not more_kind:
        under = "" if key is None else f"metadata key {key!r} has "
        raise ValueError(
            f"{under}two different kinds do not combine: "
            f"{less_kind.__name__} and {more_kind.__name__}"
        )
    if isinstance(less_specific, Tags) and isinstance(more_specific, Tags):
        return Tags(less_specific.values | more_specific.values)
    if isinstance(less_specific, Bitmask) and isinstance(more_specific, Bitmask):
        return Bitmask(less_specific.bits | more_specific.bits)
    return more_specific


def _kind(value: MetadataValue) -> type[MetadataValue]:
    """Which of the three kinds `value` is (a subclass counts as its kind)."""
    for kind in _KINDS:
        if isinstance(value, kind):
            return kind
    raise TypeError(f"not a metadata value: {type(value).__name__}")


def _require_value(value: object, key: str | None) -> None:
    if not isinstance(value, _KINDS):
        under = "" if key is None else f" under metadata key {key!r}"
        raise TypeError(
            f"a metadata value must be a Tags, Bitmask or Override; got a "
            f"{type(value).__name__}{under}"
        )


def _require_metadata(document: object) -> None:
    """Amendment 1 rulings 3 and 7: a `Mapping` of `str` to one of the kinds.

    Checks each value's kind, not its contents: every value's own rules were
    enforced when it was built.
    """
    if not isinstance(document, Mapping):
        raise TypeError(f"a metadata document must be a Mapping, got {type(document).__name__}")
    for key, value in document.items():
        if not isinstance(key, str):
            raise TypeError(f"metadata keys must be str, got a {type(key).__name__}")
        _require_value(value, key)


# --------------------------------------------------------------------------
# The upward view (decision 2).
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrefixStats:
    """One prefix's section 3 view: hot_count, capacity, hot_ratio.

    `capacity` and `hot_ratio` are functions of the prefix and the count,
    never of any parts, so a ratio is recomputed at each level and never
    combined. `hot_ratio` is for presentation; a comparison uses `hot_count`
    and `capacity`, or `hot_ratio_exact`.
    """

    prefix: Prefix
    hot_count: int

    def __post_init__(self) -> None:
        # Section 11 / decision 2: an argument check, not a clamp.
        if self.hot_count < 0:
            raise ValueError(f"hot_count must be >= 0 for {self.prefix}")

    @property
    def capacity(self) -> int:
        return self.prefix.capacity()

    @property
    def hot_ratio(self) -> float:
        return self.prefix.hot_ratio(self.hot_count)

    @property
    def hot_ratio_exact(self) -> Fraction:
        return Fraction(self.hot_count, self.prefix.capacity())


def aggregate(parent: Prefix, parts: Iterable[PrefixStats]) -> PrefixStats:
    """The upward aggregate: `PrefixStats(parent, sum of the parts' counts)`.

    Order-independent by construction, identity `aggregate(parent, [])`, and
    associative under regrouping through intermediate prefixes. Every part's
    prefix must lie within `parent` (same family, no shorter, network under
    it), else `ValueError`. That no HOT address is counted by two parts is
    the caller's precondition and is not checked (ADR-0015 assumption 15).
    """
    total = 0
    for part in parts:
        if not _within(part.prefix, parent):
            raise ValueError(f"part {part.prefix} does not lie within {parent}")
        total += part.hot_count
    return PrefixStats(prefix=parent, hot_count=total)


def prefix_stats(trie: HotTrie, prefix: Prefix) -> PrefixStats:
    """`PrefixStats(prefix, trie.hot_count(prefix))`; the family error is the trie's."""
    return PrefixStats(prefix=prefix, hot_count=trie.hot_count(prefix))


def ancestor_stats(
    trie: HotTrie, address: Address, *, min_length: int = 0
) -> tuple[PrefixStats, ...]:
    """`trie.ancestor_counts(address, min_length=...)` element for element, in its order."""
    return tuple(
        PrefixStats(prefix=entry.prefix, hot_count=entry.hot_count)
        for entry in trie.ancestor_counts(address, min_length=min_length)
    )


def _within(inner: Prefix, outer: Prefix) -> bool:
    if inner.family is not outer.family or inner.length < outer.length:
        return False
    return outer.contains(Address(family=inner.family, value=inner.network))
