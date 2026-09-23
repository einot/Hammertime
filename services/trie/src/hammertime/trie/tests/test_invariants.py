"""The structure's invariant checks accept every valid trie and catch each corruption.

Spec: section 11 (removal keeps `hot_count >= 0`; pruning), section 12 (the
`hot_count` invariant), section 35 (one trie per family), section 46.5 (the
attribute records mirror the HOT set, per address family).

The interface under test is ADR-0014 decisions 1-5 and 8-10:
`BinaryTrie`, `PatriciaTrie` and `NodeArena` from
`hammertime.trie.structure`, the checks `check_trie`, `check_hot_counts`,
`check_no_orphaned_nodes`, `check_attribute_records` and `check_patricia`
(which raise `hammertime.core.errors.InvariantViolation`), and testkit's
independently recomputing `assert_*` helpers (which raise `AssertionError`).
Every expectation below comes from those decisions and the spec sections
above, never from the modules.

Assumptions not pinned by the spec or ADR-0014:

* A `PatriciaTrie` exposes its `NodeArena` as the public attribute `arena`.
  Decision 5 makes the arena's storage lists public and says the Patricia trie
  owns it, and decision 6 names `PatriciaTrie.root`, but no decision spells
  the attribute name. Corruption is injected only through that documented
  storage (`arena.hot_count`, `arena.child`, `allocate`, `release`).
* `iter_nodes()` yields every materialized node whatever its stored count
  (decision 2: "yields the *materialized* nodes"), so a live node whose count
  has been zeroed is visible to `check_no_orphaned_nodes`.
* `BinaryTrie` documents no public storage, so the corruption cases that must
  also run against it do so through `_DoctoredView`: a `HotTrie` that answers
  from a real trie except for the one observable a test overrides. Decision 8
  states which observables each check reads (`iter_hot_addresses`,
  `iter_prefix_counts`, `hot_ip_count`, `iter_nodes`), so doctoring one of
  them is a faithful corruption as far as the check can tell.
* A record for the other family cannot be told apart from a missing or extra
  record by count alone, because an `Address`'s family is part of its
  identity; the tests only require that such a collection is rejected.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.core.errors import InvariantViolation
from hammertime.testkit.invariants import (
    assert_attribute_records_match,
    assert_hot_count_consistent,
    assert_no_negative_counts,
    assert_no_orphaned_nodes,
)
from hammertime.trie.structure import (
    NO_NODE,
    BinaryTrie,
    HotTrie,
    NodeView,
    PatriciaTrie,
    PrefixCount,
    check_attribute_records,
    check_hot_counts,
    check_no_orphaned_nodes,
    check_patricia,
    check_trie,
)

TrieFactory = Callable[[AddressFamily], HotTrie]

IMPLEMENTATIONS = [
    pytest.param(BinaryTrie, id="binary"),
    pytest.param(PatriciaTrie, id="patricia"),
]
FAMILIES = [
    pytest.param(AddressFamily.IPV4, id="ipv4"),
    pytest.param(AddressFamily.IPV6, id="ipv6"),
]

# Per family: A (section 10's example for IPv4), B (A's last-bit sibling),
# C (far from both), and OTHER (the other family, for decision 1 / 46.5).
SAMPLES: dict[AddressFamily, tuple[Address, Address, Address, Address]] = {
    AddressFamily.IPV4: (
        Address.parse("192.168.1.42"),
        Address.parse("192.168.1.43"),
        Address.parse("10.0.0.1"),
        Address.parse("2001:db8::2a"),
    ),
    AddressFamily.IPV6: (
        Address.parse("2001:db8::2a"),
        Address.parse("2001:db8::2b"),
        Address.parse("fe80::1"),
        Address.parse("192.168.1.42"),
    ),
}


def _prefix_of(address: Address, length: int) -> Prefix:
    host_bits = address.bit_length - length
    network = (address.value >> host_bits) << host_bits
    return Prefix(family=address.family, network=network, length=length)


def _root_prefix(family: AddressFamily) -> Prefix:
    return Prefix(family=family, network=0, length=0)


def _observe(trie: HotTrie) -> tuple[object, ...]:
    """Everything a caller can see about the trie's current state."""

    probes = SAMPLES[trie.family][:3]
    observed: list[object] = [
        trie.hot_ip_count,
        trie.node_count,
        list(trie.iter_prefix_counts()),
        list(trie.iter_hot_addresses()),
        list(trie.iter_nodes()),
        [trie.contains(a) for a in probes],
        [trie.ancestor_counts(a) for a in probes],
        [trie.longest_matching_prefix(a) for a in probes],
    ]
    if isinstance(trie, PatriciaTrie):
        arena = trie.arena
        observed.append((arena.live_count, arena.free_count, arena.capacity))
    return tuple(observed)


def _run_testkit(trie: HotTrie) -> None:
    assert_hot_count_consistent(trie)
    assert_no_negative_counts(trie)
    assert_no_orphaned_nodes(trie)


def _populated(make_trie: TrieFactory, family: AddressFamily) -> HotTrie:
    a, b, c, _ = SAMPLES[family]
    trie = make_trie(family)
    for address in (a, b, c):
        assert trie.add_hot_ip(address) is True
    return trie


def _patricia_with_three(family: AddressFamily) -> PatriciaTrie:
    a, b, c, _ = SAMPLES[family]
    trie = PatriciaTrie(family)
    for address in (a, b, c):
        assert trie.add_hot_ip(address) is True
    return trie


class _DoctoredView:
    """A `HotTrie` that answers from `inner` except where a test overrides it."""

    def __init__(
        self,
        inner: HotTrie,
        *,
        prefix_counts: list[PrefixCount] | None = None,
        nodes: list[NodeView] | None = None,
        hot_addresses: list[Address] | None = None,
        hot_ip_count: int | None = None,
    ) -> None:
        self._inner = inner
        self._prefix_counts = prefix_counts
        self._nodes = nodes
        self._hot_addresses = hot_addresses
        self._hot_ip_count = hot_ip_count

    @property
    def family(self) -> AddressFamily:
        return self._inner.family

    @property
    def bit_length(self) -> int:
        return self._inner.bit_length

    @property
    def hot_ip_count(self) -> int:
        if self._hot_ip_count is None:
            return self._inner.hot_ip_count
        return self._hot_ip_count

    @property
    def node_count(self) -> int:
        return self._inner.node_count

    def add_hot_ip(self, address: Address) -> bool:
        return self._inner.add_hot_ip(address)

    def remove_hot_ip(self, address: Address) -> bool:
        return self._inner.remove_hot_ip(address)

    def clear(self) -> None:
        self._inner.clear()

    def contains(self, address: Address) -> bool:
        return self._inner.contains(address)

    def hot_count(self, prefix: Prefix) -> int:
        return self._inner.hot_count(prefix)

    def ancestor_counts(self, address: Address, *, min_length: int = 0) -> tuple[PrefixCount, ...]:
        return self._inner.ancestor_counts(address, min_length=min_length)

    def longest_matching_prefix(self, address: Address) -> Prefix | None:
        return self._inner.longest_matching_prefix(address)

    def iter_prefix_counts(self, *, min_length: int = 0) -> Iterator[PrefixCount]:
        if self._prefix_counts is None:
            return self._inner.iter_prefix_counts(min_length=min_length)
        return iter([pc for pc in self._prefix_counts if pc.prefix.length >= min_length])

    def iter_hot_addresses(self) -> Iterator[Address]:
        if self._hot_addresses is None:
            return self._inner.iter_hot_addresses()
        return iter(self._hot_addresses)

    def iter_nodes(self) -> Iterator[NodeView]:
        if self._nodes is None:
            return self._inner.iter_nodes()
        return iter(self._nodes)


# --------------------------------------------------------------------------
# Valid tries pass every check.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
class TestFreshTrie:
    """ADR-0014 decisions 2 and 4: an empty trie holds no node and no count."""

    def test_passes_every_check(self, make_trie: TrieFactory, family: AddressFamily) -> None:
        trie = make_trie(family)
        check_trie(trie)
        check_hot_counts(trie)
        check_no_orphaned_nodes(trie)
        empty_set: set[Address] = set()
        empty_map: dict[Address, dict[str, object]] = {}
        check_attribute_records(trie, empty_set)
        check_attribute_records(trie, empty_map)
        _run_testkit(trie)
        assert_attribute_records_match(trie, empty_set)
        assert_attribute_records_match(trie, empty_map)

    def test_is_empty(self, make_trie: TrieFactory, family: AddressFamily) -> None:
        trie = make_trie(family)
        a = SAMPLES[family][0]
        assert trie.family is family
        assert trie.bit_length == family.bit_length
        assert trie.hot_ip_count == 0
        assert trie.node_count == 0
        assert list(trie.iter_prefix_counts()) == []
        assert list(trie.iter_hot_addresses()) == []
        assert list(trie.iter_nodes()) == []
        assert trie.longest_matching_prefix(a) is None
        assert trie.contains(a) is False
        assert trie.hot_count(_root_prefix(family)) == 0


@pytest.mark.parametrize("family", FAMILIES)
def test_a_fresh_patricia_arena_is_empty(family: AddressFamily) -> None:
    """Decision 5: the arena is the Patricia trie's storage, empty at birth."""

    trie = PatriciaTrie(family)
    check_patricia(trie)
    assert trie.root == NO_NODE
    assert trie.arena.live_count == 0
    assert trie.arena.capacity == 0


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_hand_written_sequence_keeps_every_invariant(
    make_trie: TrieFactory, family: AddressFamily
) -> None:
    """Sections 10-12 and ADR-0014 decision 3: every applied operation returns
    True and keeps the checks silent; every redundant one returns False and
    changes nothing observable."""

    a, b, c, _ = SAMPLES[family]
    trie = make_trie(family)

    def ok(expected_hot: set[Address]) -> None:
        check_trie(trie)
        check_attribute_records(trie, expected_hot)
        _run_testkit(trie)
        assert_attribute_records_match(trie, expected_hot)
        assert trie.hot_ip_count == len(expected_hot)
        assert trie.hot_count(_root_prefix(family)) == len(expected_hot)

    assert trie.add_hot_ip(a) is True
    ok({a})

    # Redundant add: the address is already HOT.
    before = _observe(trie)
    assert trie.add_hot_ip(a) is False
    assert _observe(trie) == before
    ok({a})

    # Redundant remove: the address was never HOT.
    assert trie.remove_hot_ip(b) is False
    assert _observe(trie) == before
    ok({a})

    before_nodes = list(trie.iter_nodes())
    before_counts = list(trie.iter_prefix_counts())
    assert trie.add_hot_ip(b) is True
    ok({a, b})
    assert trie.remove_hot_ip(b) is True
    ok({a})
    # Decision 4: back to the same HOT set means back to the same structure
    # (the Patricia arena's capacity, by contrast, keeps its peak).
    assert list(trie.iter_nodes()) == before_nodes
    assert list(trie.iter_prefix_counts()) == before_counts

    # Redundant remove: the address was HOT, and no longer is.
    before = _observe(trie)
    assert trie.remove_hot_ip(b) is False
    assert _observe(trie) == before
    ok({a})

    # Add-remove-add of the same address.
    assert trie.add_hot_ip(b) is True
    assert trie.contains(b) is True
    ok({a, b})
    assert trie.add_hot_ip(c) is True
    ok({a, b, c})
    assert trie.hot_count(_prefix_of(a, a.bit_length - 1)) == 2  # A and B are siblings

    for address, remaining in ((a, {b, c}), (c, {b}), (b, set())):
        assert trie.remove_hot_ip(address) is True
        ok(remaining)

    assert trie.node_count == 0
    assert list(trie.iter_prefix_counts()) == []


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_other_family_is_rejected_and_changes_nothing(
    make_trie: TrieFactory, family: AddressFamily
) -> None:
    """ADR-0014 decision 1: an address of the other family is a `ValueError`
    and changes nothing, so the invariants still hold afterwards."""

    trie = _populated(make_trie, family)
    other = SAMPLES[family][3]
    before = _observe(trie)
    with pytest.raises(ValueError):
        trie.add_hot_ip(other)
    with pytest.raises(ValueError):
        trie.remove_hot_ip(other)
    assert _observe(trie) == before
    check_trie(trie)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_clear_leaves_a_trie_that_passes_every_check(
    make_trie: TrieFactory, family: AddressFamily
) -> None:
    """ADR-0014 decision 2: `clear()` empties the trie and resets the arena."""

    trie = _populated(make_trie, family)
    trie.clear()
    check_trie(trie)
    _run_testkit(trie)
    assert trie.hot_ip_count == 0
    assert trie.node_count == 0
    assert list(trie.iter_prefix_counts()) == []
    if isinstance(trie, PatriciaTrie):
        assert trie.arena.capacity == 0


# --------------------------------------------------------------------------
# check_hot_counts (section 12, and section 11's non-negativity).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
class TestHotCountCorruptionInTheArena:
    """Counts corrupted through the arena's public storage (decision 5)."""

    def test_inflated_root_count_is_caught(self, family: AddressFamily) -> None:
        trie = _patricia_with_three(family)
        trie.arena.hot_count[trie.root] += 1
        with pytest.raises(InvariantViolation):
            check_hot_counts(trie)
        with pytest.raises(InvariantViolation):
            check_trie(trie)
        with pytest.raises(AssertionError):
            assert_hot_count_consistent(trie)

    def test_negative_root_count_is_caught(self, family: AddressFamily) -> None:
        trie = _patricia_with_three(family)
        trie.arena.hot_count[trie.root] = -1
        with pytest.raises(InvariantViolation):
            check_hot_counts(trie)
        with pytest.raises(InvariantViolation):
            check_trie(trie)

    def test_inflated_leaf_count_breaks_the_leaf_rule(self, family: AddressFamily) -> None:
        # Section 12: hot_count(/bit_length) is 0 or 1. A single-address
        # Patricia trie is one leaf node, which is therefore the root.
        trie = PatriciaTrie(family)
        assert trie.add_hot_ip(SAMPLES[family][0]) is True
        trie.arena.hot_count[trie.root] = 2
        with pytest.raises(InvariantViolation):
            check_hot_counts(trie)
        with pytest.raises(AssertionError):
            assert_hot_count_consistent(trie)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
class TestHotCountCorruptionAsObserved:
    """Decision 8: `check_hot_counts` compares the recomputed mapping with
    `dict(iter_prefix_counts())` in both directions, and `hot_ip_count` with
    the number of distinct HOT addresses."""

    def test_a_wrong_count_is_caught(self, make_trie: TrieFactory, family: AddressFamily) -> None:
        inner = _populated(make_trie, family)
        counts = list(inner.iter_prefix_counts())
        counts[0] = PrefixCount(counts[0].prefix, counts[0].hot_count + 1)
        view = _DoctoredView(inner, prefix_counts=counts)
        with pytest.raises(InvariantViolation):
            check_hot_counts(view)
        with pytest.raises(AssertionError):
            assert_hot_count_consistent(view)

    def test_a_negative_count_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        inner = _populated(make_trie, family)
        counts = list(inner.iter_prefix_counts())
        counts[-1] = PrefixCount(counts[-1].prefix, -1)
        view = _DoctoredView(inner, prefix_counts=counts)
        with pytest.raises(InvariantViolation):
            check_hot_counts(view)
        with pytest.raises(AssertionError):
            assert_no_negative_counts(view)

    def test_a_missing_prefix_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        inner = _populated(make_trie, family)
        counts = list(inner.iter_prefix_counts())
        view = _DoctoredView(inner, prefix_counts=counts[:-1])
        with pytest.raises(InvariantViolation):
            check_hot_counts(view)
        with pytest.raises(AssertionError):
            assert_hot_count_consistent(view)

    def test_a_prefix_with_nothing_beneath_it_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        inner = _populated(make_trie, family)
        # C's last-bit sibling as a host route: nothing HOT lies beneath it.
        c = SAMPLES[family][2]
        stray = Prefix(family=family, network=c.value ^ 1, length=family.bit_length)
        assert inner.hot_count(stray) == 0
        counts = [*inner.iter_prefix_counts(), PrefixCount(stray, 1)]
        view = _DoctoredView(inner, prefix_counts=counts)
        with pytest.raises(InvariantViolation):
            check_hot_counts(view)
        with pytest.raises(AssertionError):
            assert_hot_count_consistent(view)

    def test_a_wrong_hot_ip_count_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        inner = _populated(make_trie, family)
        view = _DoctoredView(inner, hot_ip_count=inner.hot_ip_count + 1)
        with pytest.raises(InvariantViolation):
            check_hot_counts(view)
        with pytest.raises(AssertionError):
            assert_hot_count_consistent(view)

    def test_a_hot_address_yielded_twice_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        inner = _populated(make_trie, family)
        hot = list(inner.iter_hot_addresses())
        view = _DoctoredView(inner, hot_addresses=[*hot, hot[0]])
        with pytest.raises(InvariantViolation):
            check_hot_counts(view)
        with pytest.raises(AssertionError):
            assert_hot_count_consistent(view)


# --------------------------------------------------------------------------
# check_no_orphaned_nodes (section 11, decision 4).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
def test_a_live_node_with_zero_count_is_an_orphan(family: AddressFamily) -> None:
    trie = _patricia_with_three(family)
    trie.arena.hot_count[trie.root] = 0
    with pytest.raises(InvariantViolation):
        check_no_orphaned_nodes(trie)
    with pytest.raises(InvariantViolation):
        check_trie(trie)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
class TestOrphansAsObserved:
    def test_a_zero_count_prefix_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        inner = _populated(make_trie, family)
        counts = list(inner.iter_prefix_counts())
        counts[-1] = PrefixCount(counts[-1].prefix, 0)
        view = _DoctoredView(inner, prefix_counts=counts)
        with pytest.raises(InvariantViolation):
            check_no_orphaned_nodes(view)
        with pytest.raises(AssertionError):
            assert_no_orphaned_nodes(view)

    def test_a_zero_count_node_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        inner = _populated(make_trie, family)
        nodes = list(inner.iter_nodes())
        assert nodes, "a non-empty trie materializes at least one node"
        nodes[-1] = NodeView(nodes[-1].prefix, 0, nodes[-1].children)
        view = _DoctoredView(inner, nodes=nodes)
        with pytest.raises(InvariantViolation):
            check_no_orphaned_nodes(view)


# --------------------------------------------------------------------------
# check_patricia (decisions 5, 6 and 8).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
class TestCheckPatricia:
    def test_a_valid_trie_passes(self, family: AddressFamily) -> None:
        trie = _patricia_with_three(family)
        check_patricia(trie)
        assert trie.arena.live_count == trie.node_count
        assert trie.arena.capacity == trie.arena.live_count + trie.arena.free_count

    def test_an_unreachable_live_slot_breaks_the_accounting(self, family: AddressFamily) -> None:
        # live_count == node_count must fail without the trie itself being
        # touched: the extra slot is allocated but never linked in.
        trie = _patricia_with_three(family)
        trie.arena.allocate(network=0, length=0)
        with pytest.raises(InvariantViolation):
            check_patricia(trie)
        with pytest.raises(InvariantViolation):
            check_trie(trie)

    def test_a_released_reachable_child_is_caught(self, family: AddressFamily) -> None:
        # Decision 5.3: every reachable child id must be live. With three
        # addresses the root is internal (decision 6: exactly two children).
        trie = _patricia_with_three(family)
        child = trie.arena.child[2 * trie.root + 1]
        assert trie.arena.is_live(child)
        trie.arena.release(child)
        with pytest.raises(InvariantViolation):
            check_patricia(trie)

    def test_a_count_that_is_not_the_sum_of_the_children_is_caught(
        self, family: AddressFamily
    ) -> None:
        trie = _patricia_with_three(family)
        trie.arena.hot_count[trie.root] += 1
        with pytest.raises(InvariantViolation):
            check_patricia(trie)

    def test_a_double_release_is_a_value_error(self, family: AddressFamily) -> None:
        trie = PatriciaTrie(family)
        node = trie.arena.allocate(network=0, length=0)
        trie.arena.release(node)
        assert trie.arena.is_live(node) is False
        with pytest.raises(ValueError):
            trie.arena.release(node)


# --------------------------------------------------------------------------
# check_attribute_records (section 46.5, decision 9).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
class TestAttributeRecords:
    def test_a_matching_set_or_map_passes(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        a, b, c, _ = SAMPLES[family]
        trie = _populated(make_trie, family)
        as_set = {a, b, c}
        as_map: dict[Address, dict[str, object]] = {
            address: {"attributes_version": 1} for address in (a, b, c)
        }
        check_attribute_records(trie, as_set)
        check_attribute_records(trie, as_map)
        assert_attribute_records_match(trie, as_set)
        assert_attribute_records_match(trie, as_map)

    @pytest.mark.parametrize("case", ["missing", "extra", "swapped", "other-family"])
    def test_a_mismatch_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily, case: str
    ) -> None:
        a, b, c, other = SAMPLES[family]
        trie = make_trie(family)
        assert trie.add_hot_ip(a) is True
        assert trie.add_hot_ip(c) is True
        records: set[Address] = {
            "missing": {a},
            "extra": {a, b, c},
            "swapped": {a, b},  # same length as the hot set, different keys
            "other-family": {a, c, other},
        }[case]
        as_map: dict[Address, dict[str, object]] = {r: {"attributes_version": 1} for r in records}
        collections: tuple[Collection[Address], ...] = (records, as_map)
        for collection in collections:
            with pytest.raises(InvariantViolation):
                check_attribute_records(trie, collection)
            with pytest.raises(AssertionError):
                assert_attribute_records_match(trie, collection)

    def test_a_same_size_collection_of_the_other_family_is_caught(
        self, make_trie: TrieFactory, family: AddressFamily
    ) -> None:
        a, _b, _c, other = SAMPLES[family]
        trie = make_trie(family)
        assert trie.add_hot_ip(a) is True
        with pytest.raises(InvariantViolation):
            check_attribute_records(trie, {other})
        with pytest.raises(AssertionError):
            assert_attribute_records_match(trie, {other})


# --------------------------------------------------------------------------
# Testkit and service checks agree (decision 10).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_testkit_and_service_checks_agree_on_a_valid_trie(
    make_trie: TrieFactory, family: AddressFamily
) -> None:
    trie = _populated(make_trie, family)
    check_trie(trie)
    _run_testkit(trie)
    hot = set(trie.iter_hot_addresses())
    check_attribute_records(trie, hot)
    assert_attribute_records_match(trie, hot)


@pytest.mark.parametrize("family", FAMILIES)
def test_testkit_and_service_checks_agree_on_a_corrupted_count(family: AddressFamily) -> None:
    trie = _patricia_with_three(family)
    trie.arena.hot_count[trie.root] += 1
    with pytest.raises(InvariantViolation):
        check_trie(trie)
    with pytest.raises(AssertionError):
        assert_hot_count_consistent(trie)
