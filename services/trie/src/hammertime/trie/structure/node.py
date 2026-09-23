"""Node layout and the trie's shared type vocabulary.

Spec: section 9, section 12, section 27; ADR-0014 decisions 2, 6 and 11.

Section 9's node is `child[0]`, `child[1]`, `hot_count`, `local_metadata`,
`prefix_state`. The structure stores only the first three (ADR-0014
assumption 21): `hot_count` is the load-bearing value, and `prefix_state` is
a cache that can always be recomputed from `hot_count`, the prefix length and
configuration (section 12), so it lives outside this package, as does
`local_metadata`.

This module holds declarations only -- no traversal -- so that both
`binary_trie.py` and `patricia.py` can import it without sharing any
counting, walking or pruning logic (ADR-0014 decision 7).
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final, NamedTuple, Protocol

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix

NodeId = int
"""An index into a `NodeArena` (ADR-0014 decision 5)."""

NO_NODE: Final[NodeId] = -1
"""The `NodeId` meaning "no node": an absent child, or an empty trie's root."""


class PrefixCount(NamedTuple):
    """A prefix and the number of currently HOT addresses beneath it (section 12)."""

    prefix: Prefix
    hot_count: int


class NodeView(NamedTuple):
    """One materialized node, as invariants and `trie-inspect` see it (section 9).

    `hot_count` is the node's stored count, verbatim -- never clamped
    (ADR-0014 Amendment 1, A3). `children` holds 0, 1 or 2 prefixes, ascending
    by branch bit.
    """

    prefix: Prefix
    hot_count: int
    children: tuple[Prefix, ...]


@dataclass(slots=True)
class TrieNode:
    """Section 9's conceptual node, as the reference implementation stores it."""

    hot_count: int = 0
    # Quoted: without `from __future__ import annotations` a dataclass field
    # annotation is evaluated in the class body, before the name exists.
    child: list["TrieNode | None"] = field(default_factory=lambda: [None, None])


class HotTrie(Protocol):
    """Section 8's trie. Two representations, one observable behaviour (section 27).

    The contract is ADR-0014 decision 2 (with Amendment 1): every method taking
    an `Address` or `Prefix` raises `ValueError` for the other family before
    looking at any other argument; `add_hot_ip`/`remove_hot_ip` return whether
    the HOT set changed; `iter_nodes`/`iter_hot_addresses`/`node_count` are
    structural and everything else reads the stored counts.
    """

    @property
    def family(self) -> AddressFamily: ...

    @property
    def bit_length(self) -> int: ...

    @property
    def hot_ip_count(self) -> int: ...

    @property
    def node_count(self) -> int: ...

    # Mutation -- sections 10, 11, 39. Single writer (section 28); not thread-safe.
    def add_hot_ip(self, address: Address) -> bool: ...

    def remove_hot_ip(self, address: Address) -> bool: ...

    def clear(self) -> None: ...

    # Queries -- sections 8, 12, 29.
    def contains(self, address: Address) -> bool: ...

    def hot_count(self, prefix: Prefix) -> int: ...

    def ancestor_counts(
        self, address: Address, *, min_length: int = 0
    ) -> tuple[PrefixCount, ...]: ...

    def longest_matching_prefix(self, address: Address) -> Prefix | None: ...

    def iter_prefix_counts(self, *, min_length: int = 0) -> Iterator[PrefixCount]: ...

    def iter_hot_addresses(self) -> Iterator[Address]: ...

    def iter_nodes(self) -> Iterator[NodeView]: ...
