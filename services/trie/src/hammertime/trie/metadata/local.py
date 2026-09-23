"""Metadata attached to the prefix it was declared on -- never copied downward.

Spec: section 16, section 17; ADR-0015 decision 4 (Amendment 1 rulings 2, 3,
4 and 6) and decision 8.

10.0.0.0/8 -> {"internal"} is stored once, keyed by the /8 itself, beside the
trie and never on a node. Materializing it into every descendant would mean an
unbounded update fan-out (section 17); storing it on a node would lose it when
the node is pruned (ADR-0014 decision 4) and leave it nowhere to live inside a
compressed edge (section 27). Local, inherited and effective metadata are
three separate reads, folded along the path at lookup time.

Family-scoped like the trie (ADR-0014 decision 1), operator-declared
configuration rather than derived state (so not part of a section 33
snapshot), and not thread-safe: section 28's single writer is the whole
concurrency model (decision 8).
"""

from collections.abc import Mapping
from types import MappingProxyType

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.trie.metadata.combine import (
    EMPTY_METADATA,
    Metadata,
    MetadataValue,
    _kind,
    _require_metadata,
    combine_path,
)


class PrefixMetadataStore:
    """Prefix-keyed local metadata for one address family (decision 4)."""

    def __init__(self, family: AddressFamily) -> None:
        self._family = family
        self._declarations: dict[Prefix, Metadata] = {}

    @property
    def family(self) -> AddressFamily:
        return self._family

    # -- mutation ----------------------------------------------------------

    def declare(self, prefix: Prefix, metadata: Metadata) -> None:
        """Store an immutable copy of `metadata` as `prefix`'s own declaration.

        In order, and all before the store changes: the family (`ValueError`);
        the document's shape (`TypeError`: a `Mapping` of `str` to `Tags`,
        `Bitmask` or `Override`, checked on the copy that would be stored);
        then the path (`ValueError` if another declared prefix containing, or
        contained by, `prefix` holds one of these keys with a different kind).
        `prefix`'s own current declaration, which this replaces, is not
        compared. An empty document is a declaration, not a revoke.
        """
        self._check_family(prefix.family)
        if not isinstance(metadata, Mapping):
            # Before `dict()`, which would accept a list of pairs.
            raise TypeError(f"a metadata document must be a Mapping, got {type(metadata).__name__}")
        stored: dict[str, MetadataValue] = dict(metadata)
        _require_metadata(stored)
        self._check_path(prefix, stored)
        self._declarations[prefix] = MappingProxyType(stored)

    def revoke(self, prefix: Prefix) -> bool:
        """Remove `prefix`'s own declaration; return whether there was one."""
        self._check_family(prefix.family)
        return self._declarations.pop(prefix, None) is not None

    def clear(self) -> None:
        self._declarations.clear()

    # -- reads -------------------------------------------------------------

    def local(self, prefix: Prefix) -> Metadata:
        """What was declared on `prefix` itself, and nothing else."""
        self._check_family(prefix.family)
        return self._declarations.get(prefix, EMPTY_METADATA)

    def inherited(self, prefix: Prefix) -> Metadata:
        """The fold of the declarations on `prefix`'s strict ancestors."""
        self._check_family(prefix.family)
        return self._fold(prefix, prefix.length - 1)

    def effective_for_prefix(self, prefix: Prefix) -> Metadata:
        """`combine(inherited(prefix), local(prefix))`: the ancestors, then itself."""
        self._check_family(prefix.family)
        return self._fold(prefix, prefix.length)

    def effective(self, address: Address) -> Metadata:
        """Section 16's effective_metadata(ip): the fold from /0 to the host route, inclusive."""
        self._check_family(address.family)
        host_route = Prefix(family=address.family, network=address.value, length=address.bit_length)
        return self._fold(host_route, host_route.length)

    def declarations(self) -> tuple[tuple[Prefix, Metadata], ...]:
        """Every declaration, sorted by `(length, network)` -- the fold order."""
        return tuple(
            sorted(self._declarations.items(), key=lambda item: (item[0].length, item[0].network))
        )

    def __len__(self) -> int:
        return len(self._declarations)

    def __contains__(self, prefix: object) -> bool:
        # Amendment 1 ruling 4: anything that is not a Prefix -- an Address
        # included, and an unhashable object -- is simply absent, so the type
        # test comes before any lookup. Ruling 2: a Prefix of the other family
        # is the family ValueError, as for every other method.
        if not isinstance(prefix, Prefix):
            return False
        self._check_family(prefix.family)
        return prefix in self._declarations

    # -- internals ---------------------------------------------------------

    def _fold(self, prefix: Prefix, through_length: int) -> Metadata:
        """combine_path over the declarations at lengths 0..through_length on `prefix`'s path.

        One dictionary probe per length: independent of the number of
        declarations and of the hot set, and nothing is materialized.
        """
        if not self._declarations:
            return EMPTY_METADATA
        bits = self._family.bit_length
        path: list[Metadata] = []
        for length in range(through_length + 1):
            host_bits = bits - length
            ancestor = Prefix(
                family=self._family,
                network=(prefix.network >> host_bits) << host_bits,
                length=length,
            )
            declared = self._declarations.get(ancestor)
            if declared is not None:
                path.append(declared)
        return combine_path(path)

    def _check_path(self, prefix: Prefix, document: Metadata) -> None:
        """Amendment 1 ruling 6: no key may have two kinds on one path."""
        for other, declared in self._declarations.items():
            if other == prefix or not (_covers(other, prefix) or _covers(prefix, other)):
                continue
            for key, value in document.items():
                present = declared.get(key)
                if present is None or _kind(present) is _kind(value):
                    continue
                raise ValueError(
                    f"metadata key {key!r} would have two kinds on one path: "
                    f"{_kind(value).__name__} on {prefix} and "
                    f"{_kind(present).__name__} on {other}"
                )

    def _check_family(self, family: AddressFamily) -> None:
        if family is not self._family:
            raise ValueError(f"this store holds {self._family} prefixes; got an {family} argument")


def _covers(outer: Prefix, inner: Prefix) -> bool:
    """Whether `outer` contains `inner` or is it (one family assumed)."""
    if outer.length > inner.length:
        return False
    shift = outer.family.bit_length - outer.length
    return (outer.network >> shift) == (inner.network >> shift)
