"""Executable spec invariants, used by tests and by the trie-inspect tool.

Spec: section 11, section 12, section 38, section 46.5

The trie assertions are ADR-0014 decision 10. They take any object shaped
like `TrieView` -- a structural `Protocol`, so this package never imports
`hammertime.trie` and keeps depending on `hammertime-core` and hypothesis
alone. They recompute every expected value from scratch from the HOT
addresses the trie reports and never call the trie service's own
`invariants.py`: that duplication is the independence (decision 10, rule 2),
so a checker bug and a structure bug in the same direction cannot cancel out.

Each failure is an `AssertionError` whose message names the spec section and
the two values that disagree.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterator
from typing import Protocol

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix


class TrieView(Protocol):
    """What the assertions read from a trie (ADR-0014 decision 10)."""

    @property
    def family(self) -> AddressFamily: ...

    @property
    def hot_ip_count(self) -> int: ...

    @property
    def node_count(self) -> int: ...

    def iter_hot_addresses(self) -> Iterator[Address]: ...

    def iter_prefix_counts(self) -> Iterator[tuple[Prefix, int]]: ...


def _prefix_of(address: Address, length: int) -> Prefix:
    """The length-`length` prefix containing `address`."""

    host_bits = address.bit_length - length
    network = (address.value >> host_bits) << host_bits
    return Prefix(family=address.family, network=network, length=length)


def _recompute(hot: list[Address]) -> dict[Prefix, int]:
    """Section 12 from scratch: every hot address counts once under each of
    its `bit_length + 1` prefixes, `/0` through `/bit_length`."""

    counts: dict[Prefix, int] = {}
    for address in hot:
        for length in range(address.bit_length + 1):
            prefix = _prefix_of(address, length)
            counts[prefix] = counts.get(prefix, 0) + 1
    return counts


def assert_hot_count_consistent(trie: TrieView) -> None:
    """Recompute hot_count bottom-up and compare with stored values (section 12).

    The recomputed mapping must equal `dict(trie.iter_prefix_counts())` in
    both directions, `hot_ip_count` must equal the number of distinct HOT
    addresses, and neither iteration may yield the same key twice.
    """

    hot = list(trie.iter_hot_addresses())
    repeated = sorted(str(address) for address, n in Counter(hot).items() if n > 1)
    if repeated:
        raise AssertionError(
            f"section 12: iter_hot_addresses() yielded {repeated} more than once; "
            f"expected each HOT address exactly once"
        )
    foreign = sorted(str(address) for address in hot if address.family != trie.family)
    if foreign:
        raise AssertionError(
            f"section 35: iter_hot_addresses() of a {trie.family} trie yielded "
            f"addresses of another family: {foreign}"
        )
    if trie.hot_ip_count != len(hot):
        raise AssertionError(
            f"section 12: hot_ip_count is {trie.hot_ip_count} but iter_hot_addresses() "
            f"yielded {len(hot)} distinct HOT addresses"
        )

    stored: dict[Prefix, int] = {}
    for prefix, count in trie.iter_prefix_counts():
        if prefix in stored:
            raise AssertionError(
                f"section 12: iter_prefix_counts() yielded {prefix} twice, with hot_count "
                f"{stored[prefix]} and then {count}"
            )
        stored[prefix] = count

    expected = _recompute(hot)
    for prefix, count in expected.items():
        actual = stored.get(prefix)
        if actual != count:
            shown = "absent" if actual is None else str(actual)
            raise AssertionError(
                f"section 12: hot_count({prefix}) is {shown} in iter_prefix_counts() "
                f"but {count} recomputed from the HOT addresses"
            )
    for prefix, count in stored.items():
        if prefix not in expected:
            raise AssertionError(
                f"section 12: iter_prefix_counts() yielded {prefix} with hot_count {count} "
                f"but no HOT address lies beneath it (recomputed 0)"
            )


def assert_no_negative_counts(trie: TrieView) -> None:
    """hot_count >= 0 at every node (section 11)."""

    if trie.hot_ip_count < 0:
        raise AssertionError(
            f"section 11: hot_ip_count (hot_count of the root) is {trie.hot_ip_count}; "
            f"expected >= 0"
        )
    for prefix, count in trie.iter_prefix_counts():
        if count < 0:
            raise AssertionError(f"section 11: hot_count({prefix}) is {count}; expected >= 0")


def assert_no_orphaned_nodes(trie: TrieView) -> None:
    """No zero-count prefix survives a removal (section 11, ADR-0014 decision 4).

    Pruning is mandatory, so every yielded prefix count is positive, an empty
    trie holds no nodes, and -- because each node of either representation
    stands for a distinct prefix with a positive count -- `node_count` can
    never exceed the number of prefixes whose count is positive.
    """

    positive = 0
    for prefix, count in trie.iter_prefix_counts():
        if count <= 0:
            raise AssertionError(
                f"section 11 (ADR-0014 decision 4): {prefix} is present with hot_count "
                f"{count}; expected > 0, a prefix with nothing HOT beneath it is pruned"
            )
        positive += 1
    if trie.hot_ip_count == 0 and trie.node_count != 0:
        raise AssertionError(
            f"section 11 (ADR-0014 decision 4): the trie holds no HOT address but "
            f"node_count is {trie.node_count}; expected 0"
        )
    if trie.node_count > positive:
        raise AssertionError(
            f"section 11 (ADR-0014 decision 4): node_count is {trie.node_count} but only "
            f"{positive} prefixes have hot_count > 0; some node is orphaned"
        )


def assert_attribute_records_match(trie: TrieView, records: Collection[Address]) -> None:
    """The per-IP attribute records mirror the HOT set (section 46.5, ADR-0005).

    `records` is any collection of addresses -- a `set`, or the record map
    itself, whose iteration yields its keys.
    """

    foreign = sorted(str(address) for address in records if address.family != trie.family)
    if foreign:
        raise AssertionError(
            f"section 46.5: the invariant is per address family, but the records checked "
            f"against a {trie.family} trie include {foreign}"
        )
    if len(records) != trie.hot_ip_count:
        raise AssertionError(
            f"section 46.5: len(records) is {len(records)} but hot_count(root) is "
            f"{trie.hot_ip_count}"
        )
    hot = set(trie.iter_hot_addresses())
    keys = set(records)
    if keys != hot:
        missing = sorted(str(address) for address in hot - keys)
        extra = sorted(str(address) for address in keys - hot)
        raise AssertionError(
            f"section 46.5: set(records) differs from the currently HOT addresses: "
            f"HOT without a record {missing}, record without a HOT address {extra}"
        )


def assert_hysteresis_holds(history: object) -> None:
    """No COLD->HOT below hot_threshold, no HOT->COLD at or above cold_threshold (section 38)."""
    raise NotImplementedError
