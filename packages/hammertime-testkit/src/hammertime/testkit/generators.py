"""Hypothesis strategies for addresses, prefixes and hot-set operation streams.

Spec: section 3, section 35

Addresses and prefixes follow section 3's definitions and section 35's
family-explicit `Address` (`bit_length` 32 or 128). The operation streams are
the `add_hot_ip` / `remove_hot_ip` sequences ADR-0014 (assumption 27) says the
trie structure tests need; nothing here is speculative for other consumers.

Every strategy takes the address family explicitly: ADR-0014 decision 1 gives
each family its own trie, so a stream never mixes families.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NamedTuple

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hypothesis import strategies as st

TrieOperationKind = Literal["add", "remove"]


class TrieOperation(NamedTuple):
    """One hot-set mutation: `add_hot_ip(address)` or `remove_hot_ip(address)`."""

    kind: TrieOperationKind
    address: Address


def addresses(family: AddressFamily) -> st.SearchStrategy[Address]:
    """Any address of `family`, biased towards the ends of the address space."""

    top = (1 << family.bit_length) - 1
    edges = st.sampled_from((0, 1, 1 << (family.bit_length - 1), top - 1, top))
    values = st.one_of(edges, st.integers(min_value=0, max_value=top))
    return values.map(lambda value: Address(family=family, value=value))


@st.composite
def prefixes(draw: st.DrawFn, family: AddressFamily) -> Prefix:
    """Any prefix of `family`, `/0` through `/bit_length`, host bits zeroed."""

    bit_length = family.bit_length
    length = draw(st.integers(min_value=0, max_value=bit_length))
    value = draw(st.integers(min_value=0, max_value=(1 << bit_length) - 1))
    host_bits = bit_length - length
    return Prefix(family=family, network=(value >> host_bits) << host_bits, length=length)


def _near(base: Address) -> st.SearchStrategy[Address]:
    """Addresses sharing a (usually long) leading run of bits with `base`.

    Flipping only the low `k` bits keeps the top `bit_length - k` bits, so
    small `k` yields last-bit siblings and long shared prefixes -- the
    compressed-edge cases of a Patricia trie (section 27).
    """

    bit_length = base.bit_length
    low_bits = st.one_of(
        st.integers(min_value=0, max_value=8),
        st.integers(min_value=0, max_value=bit_length),
    )
    noise = st.integers(min_value=0, max_value=(1 << bit_length) - 1)
    return st.tuples(low_bits, noise).map(
        lambda pair: Address(
            family=base.family,
            value=base.value ^ (pair[1] & ((1 << pair[0]) - 1)),
        )
    )


@st.composite
def address_pools(
    draw: st.DrawFn,
    family: AddressFamily,
    *,
    min_size: int = 1,
    max_size: int = 6,
) -> list[Address]:
    """A small set of distinct addresses of `family`, clustered around one base.

    Kept small on purpose: an operation stream drawn from it revisits the same
    addresses, so redundant adds and removes (ADR-0014 decision 3) occur.
    """

    base = draw(addresses(family))
    member = st.one_of(_near(base), addresses(family))
    return draw(st.lists(member, min_size=min_size, max_size=max_size, unique=True))


_KINDS: tuple[TrieOperationKind, ...] = ("add", "add", "remove")


def trie_operations(
    pool: Sequence[Address],
    *,
    min_size: int = 0,
    max_size: int = 24,
) -> st.SearchStrategy[list[TrieOperation]]:
    """A sequence of adds and removes over `pool` (which must be non-empty).

    Adds are drawn twice as often as removes so the hot set grows before it
    shrinks; removes of addresses that are not HOT still occur, as do adds of
    addresses that already are.
    """

    operation = st.builds(TrieOperation, st.sampled_from(_KINDS), st.sampled_from(list(pool)))
    return st.lists(operation, min_size=min_size, max_size=max_size)
