"""The structure's invariant checks accept every valid trie and catch each corruption.

Spec: section 11 (removal keeps `hot_count >= 0`; pruning), section 12 (the
`hot_count` invariant), section 35 (one trie per family), section 46.5 (the
attribute records mirror the HOT set, per address family).

The interface under test is ADR-0014 decisions 1-5 and 8-10 with Amendments 1
and 2: `BinaryTrie`, `PatriciaTrie` and `NodeArena` from
`hammertime.trie.structure`, the checks `check_trie`, `check_hot_counts`,
`check_no_orphaned_nodes`, `check_attribute_records` and `check_patricia`
(which raise `hammertime.core.errors.InvariantViolation`), and testkit's
independently recomputing `assert_*` helpers (which raise `AssertionError`).
Every expectation below comes from those decisions and the spec sections
above, never from the modules.

Pinned by the amendments, and relied on here:

* `PatriciaTrie.arena` is the public `NodeArena`, never rebound (A1), and its
  storage lists -- `network`, `length`, `hot_count`, `child` and the LIFO free
  list `free_ids` (A11) -- are writable. Corruption is injected only through
  that documented storage and through `allocate` / `release`.
* `check_patricia` computes reachability itself, and a bad id -- out of range,
  released, or reached twice -- is an `InvariantViolation`, never an
  `IndexError`, a `ValueError` or a hang (A2). The free list must hold exactly
  the dead slots, each once, and an out-of-range entry is reported rather than
  indexed with (A11).
* `iter_nodes()` and `iter_hot_addresses()` are structural and report stored
  counts verbatim; `contains`, `hot_count`, `iter_prefix_counts` and
  `hot_ip_count` are count-derived (A3). That is what makes a zeroed or
  inflated count visible to the checks.
* The family is checked before any other argument on every address-taking
  method, with a `ValueError` naming both families (decision 1, A9).
* A mutator that finds a corruption it cannot walk past raises
  `InvariantViolation` before mutating anything (A12). `BinaryTrie` need not
  match on a corrupted trie (A12 clause 4), so nothing in this file compares
  the two implementations on a corrupted state.

Choices of this file's own, not dictated by the spec or ADR-0014:

* `BinaryTrie` documents no public storage (A5), so the corruption cases that
  must also run against it do so through `_DoctoredView`: a `HotTrie` that
  answers from a real trie except for the one observable a test overrides.
  Decision 8 states which observables each check reads (`iter_hot_addresses`,
  `iter_prefix_counts`, `hot_ip_count`, `iter_nodes`), so doctoring one of
  them is a faithful corruption as far as the check can tell.
* A record for the other family cannot be told apart from a missing or extra
  record by count alone (A7); the tests only require that such a collection
  is rejected.
* `NodeArena()` takes no constructor arguments. Decision 5's sketch declares
  none, and a fresh arena pre-allocates nothing (A4), so there is nothing to
  configure.
* Where a doctored trie could violate more than one `check_patricia` clause,
  only the exception type is asserted: the ADR does not order the clauses.
* "A family's name" in a `ValueError` message means the `AddressFamily`
  value (`"ipv4"` / `"ipv6"`), matched case-insensitively.
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
    NodeArena,
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


# Decision 1 (A9): every `HotTrie` method that takes an `Address` or a
# `Prefix`, each handed one of the other family. `iter_prefix_counts` takes
# neither and has no family check.
FAMILY_CHECKED_CALLS: dict[str, Callable[[HotTrie, Address], object]] = {
    "add_hot_ip": lambda t, a: t.add_hot_ip(a),
    "remove_hot_ip": lambda t, a: t.remove_hot_ip(a),
    "contains": lambda t, a: t.contains(a),
    "hot_count-host-route": lambda t, a: t.hot_count(_prefix_of(a, a.bit_length)),
    "hot_count-slash-8": lambda t, a: t.hot_count(_prefix_of(a, 8)),
    "hot_count-slash-0": lambda t, a: t.hot_count(_root_prefix(a.family)),
    "ancestor_counts": lambda t, a: t.ancestor_counts(a),
    "ancestor_counts-min-length-8": lambda t, a: t.ancestor_counts(a, min_length=8),
    "longest_matching_prefix": lambda t, a: t.longest_matching_prefix(a),
}
POPULATED = [pytest.param(False, id="empty"), pytest.param(True, id="populated")]


def _assert_names_both_families(error: pytest.ExceptionInfo[ValueError]) -> None:
    """Decision 1: "a `ValueError` naming both families"."""

    message = str(error.value).lower()
    for family in (AddressFamily.IPV4, AddressFamily.IPV6):
        assert family.value in message, message


@pytest.mark.parametrize("call", sorted(FAMILY_CHECKED_CALLS))
@pytest.mark.parametrize("populated", POPULATED)
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_every_address_taking_method_rejects_the_other_family(
    make_trie: TrieFactory, family: AddressFamily, populated: bool, call: str
) -> None:
    """ADR-0014 decision 1 and Amendment 1 A9: a query is as much a routing
    bug as an update. Every method that takes an `Address` or `Prefix` raises
    a `ValueError` naming both families and changes nothing observable."""

    trie = _populated(make_trie, family) if populated else make_trie(family)
    other = SAMPLES[family][3]
    assert other.family is not family
    before = _observe(trie)
    with pytest.raises(ValueError) as excinfo:
        FAMILY_CHECKED_CALLS[call](trie, other)
    _assert_names_both_families(excinfo)
    assert _observe(trie) == before
    check_trie(trie)


@pytest.mark.parametrize("min_length", [99, -1])
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_the_family_is_checked_before_min_length(
    make_trie: TrieFactory, family: AddressFamily, min_length: int
) -> None:
    """A9: the family is checked before any other argument, so a call wrong
    in both ways reports the family. Naming *both* families is what tells
    this error apart from a `min_length` one, which has no reason to mention
    the family the trie does not hold."""

    trie = _populated(make_trie, family)
    other = SAMPLES[family][3]
    before = _observe(trie)
    with pytest.raises(ValueError) as excinfo:
        trie.ancestor_counts(other, min_length=min_length)
    _assert_names_both_families(excinfo)
    assert _observe(trie) == before


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
# NodeArena's promises on a bare arena (decision 5, A4, A11).
# --------------------------------------------------------------------------


def _arena_state(arena: NodeArena) -> tuple[object, ...]:
    """Every storage list and every count, copied."""

    return (
        list(arena.network),
        list(arena.length),
        list(arena.hot_count),
        list(arena.child),
        list(arena.free_ids),
        arena.capacity,
        arena.live_count,
        arena.free_count,
    )


def _assert_storage_agrees(arena: NodeArena) -> None:
    """Decision 5 promise 2: capacity == len(network) == len(length) ==
    len(hot_count) == len(child) // 2 at all times; free_count ==
    len(free_ids); A11: live_count == capacity - free_count."""

    assert arena.capacity == len(arena.network) == len(arena.length) == len(arena.hot_count)
    assert len(arena.child) == 2 * arena.capacity
    assert arena.free_count == len(arena.free_ids)
    assert arena.live_count == arena.capacity - arena.free_count


def _assert_slot(arena: NodeArena, node: int, network: int, length: int, hot_count: int) -> None:
    assert arena.is_live(node) is True
    assert arena.network[node] == network
    assert arena.length[node] == length
    assert arena.hot_count[node] == hot_count
    assert arena.child[2 * node] == NO_NODE
    assert arena.child[2 * node + 1] == NO_NODE


class TestNodeArena:
    def test_a_fresh_arena_preallocates_nothing(self) -> None:
        """Decision 5 promise 2 / A4: capacity == 0, every list empty."""

        arena = NodeArena()
        assert (arena.capacity, arena.live_count, arena.free_count) == (0, 0, 0)
        assert arena.network == []
        assert arena.length == []
        assert arena.hot_count == []
        assert arena.child == []
        assert arena.free_ids == []

    def test_growth_is_by_exactly_one_slot(self) -> None:
        """Decision 5 promise 2: with an empty free list, `allocate` appends one
        node's worth of slots, so capacity rises by exactly 1 and the new id
        is the index just appended."""

        arena = NodeArena()
        for expected in range(6):
            assert arena.free_ids == []
            network, length, hot_count = expected << 8, 24, expected + 1
            node = arena.allocate(network=network, length=length, hot_count=hot_count)
            assert node == expected
            assert arena.capacity == expected + 1
            assert arena.live_count == expected + 1
            assert arena.free_count == 0
            _assert_storage_agrees(arena)
            _assert_slot(arena, node, network, length, hot_count)

        # hot_count defaults to 0.
        node = arena.allocate(network=0, length=0)
        assert arena.capacity == 7
        _assert_storage_agrees(arena)
        _assert_slot(arena, node, 0, 0, 0)

    def test_released_slots_are_reused_last_in_first_out(self) -> None:
        """Decision 5 promises 2 and 3, A11: `release` writes -1 into `length`
        and pushes the id on `free_ids`; `allocate` pops the most recently
        released id before it grows, and hands back a slot holding exactly
        the fields just passed, with no children."""

        arena = NodeArena()
        first = arena.allocate(network=0, length=0, hot_count=3)
        second = arena.allocate(network=0, length=1, hot_count=2)
        third = arena.allocate(network=1 << 31, length=1, hot_count=1)
        assert arena.capacity == 3
        # Give the slots children, so reuse has something stale to clear.
        arena.child[2 * third] = first
        arena.child[2 * third + 1] = second
        arena.child[2 * first] = third

        arena.release(first)
        arena.release(third)
        assert arena.length[first] == -1
        assert arena.length[third] == -1
        assert arena.is_live(first) is False
        assert arena.is_live(third) is False
        assert arena.is_live(second) is True
        assert (arena.capacity, arena.live_count, arena.free_count) == (3, 1, 2)
        assert arena.free_ids[-1] == third
        assert sorted(arena.free_ids) == sorted([first, third])
        _assert_storage_agrees(arena)

        reused = arena.allocate(network=0xC0A80100, length=24, hot_count=7)
        assert reused == third
        assert (arena.capacity, arena.live_count, arena.free_count) == (3, 2, 1)
        _assert_slot(arena, reused, 0xC0A80100, 24, 7)
        _assert_storage_agrees(arena)

        reused = arena.allocate(network=0x0A000000, length=8)
        assert reused == first
        assert (arena.capacity, arena.live_count, arena.free_count) == (3, 3, 0)
        _assert_slot(arena, reused, 0x0A000000, 8, 0)
        _assert_storage_agrees(arena)

        # The free list is empty again, so the next allocation grows by one.
        grown = arena.allocate(network=0, length=32, hot_count=1)
        assert grown == 3
        assert (arena.capacity, arena.live_count, arena.free_count) == (4, 4, 0)
        _assert_slot(arena, grown, 0, 32, 1)
        _assert_storage_agrees(arena)

    @pytest.mark.parametrize("length", [-1, -2])
    @pytest.mark.parametrize("with_free_slot", [False, True], ids=["no-free-slot", "free-slot"])
    def test_allocate_rejects_a_negative_length(self, length: int, with_free_slot: bool) -> None:
        """A11 clause 3: `length < 0` is the only record of deadness, so a
        negative length is a `ValueError` and changes nothing -- not even a
        pop from the free list."""

        arena = NodeArena()
        arena.allocate(network=0, length=0, hot_count=1)
        released = arena.allocate(network=0, length=1)
        if with_free_slot:
            arena.release(released)
        before = _arena_state(arena)
        with pytest.raises(ValueError):
            arena.allocate(network=0, length=length)
        assert _arena_state(arena) == before

    def test_release_rejects_a_dead_or_out_of_range_id(self) -> None:
        """Decision 5 promise 3 and assumption 18: a double release is a
        `ValueError`; so is releasing an id the slab never held. Neither
        changes anything."""

        arena = NodeArena()
        arena.allocate(network=0, length=0, hot_count=2)
        released = arena.allocate(network=0, length=1, hot_count=1)
        arena.allocate(network=1 << 31, length=1, hot_count=1)
        arena.release(released)
        before = _arena_state(arena)
        for node in (released, arena.capacity, arena.capacity + 10):
            with pytest.raises(ValueError):
                arena.release(node)
            assert _arena_state(arena) == before

    def test_is_live_is_false_outside_the_slab(self) -> None:
        """Decision 5 promise 3. The last slot is live, so a negative id that
        were used as a Python index would wrongly read it as live."""

        arena = NodeArena()
        for length in (0, 1, 1):
            arena.allocate(network=0, length=length)
        assert arena.is_live(arena.capacity - 1) is True
        for node in (NO_NODE, -2, arena.capacity, arena.capacity + 100):
            assert arena.is_live(node) is False, node

    def test_clear_truncates_every_list(self) -> None:
        """Decision 5 promise 4: `clear()` resets the slab to empty, the free
        list included, and the bound restarts from scratch."""

        arena = NodeArena()
        for length in (0, 1, 1):
            arena.allocate(network=0, length=length, hot_count=1)
        arena.child[0] = 1
        arena.release(2)
        arena.clear()
        assert (arena.capacity, arena.live_count, arena.free_count) == (0, 0, 0)
        assert arena.network == []
        assert arena.length == []
        assert arena.hot_count == []
        assert arena.child == []
        assert arena.free_ids == []

        assert arena.allocate(network=0, length=0) == 0
        assert arena.capacity == 1
        _assert_storage_agrees(arena)


# --------------------------------------------------------------------------
# check_patricia's representation clauses (decisions 6 and 8, A2).
# --------------------------------------------------------------------------


def _root_children(trie: PatriciaTrie) -> tuple[int, int]:
    arena = trie.arena
    assert trie.root != NO_NODE
    return arena.child[2 * trie.root], arena.child[2 * trie.root + 1]


def _internal_below_root(trie: PatriciaTrie) -> int:
    """The node above SAMPLES' A and B: /31 for IPv4, /127 for IPv6.

    In both families A and C differ in their first bit, so the root of
    `_patricia_with_three` is the /0 with C's leaf on one side and this node
    on the other.
    """

    internal = [n for n in _root_children(trie) if trie.arena.length[n] < trie.bit_length]
    assert len(internal) == 1, internal
    return internal[0]


def _leaf_below_root(trie: PatriciaTrie) -> int:
    leaves = [n for n in _root_children(trie) if trie.arena.length[n] == trie.bit_length]
    assert len(leaves) == 1, leaves
    return leaves[0]


def _corrupt_cycle(trie: PatriciaTrie) -> None:
    # An internal node's child pointer rewritten to the root: the walk would
    # come back round forever without a visited set. The node keeps two
    # non-NO_NODE children, so the one-child clause cannot fire first.
    arena = trie.arena
    internal = _internal_below_root(trie)
    arena.child[2 * internal + 1] = trie.root
    assert NO_NODE not in (arena.child[2 * internal], arena.child[2 * internal + 1])


def _corrupt_shared_subtree(trie: PatriciaTrie) -> None:
    arena = trie.arena
    arena.child[2 * trie.root + 1] = arena.child[2 * trie.root]


def _corrupt_child_id_past_capacity(trie: PatriciaTrie) -> None:
    trie.arena.child[2 * trie.root + 1] = trie.arena.capacity


def _corrupt_child_id_far_past_capacity(trie: PatriciaTrie) -> None:
    trie.arena.child[2 * trie.root + 1] = trie.arena.capacity + 1000


def _corrupt_negative_child_id(trie: PatriciaTrie) -> None:
    # Negative but not NO_NODE; as a Python index it would read a real slot.
    trie.arena.child[2 * trie.root + 1] = -2


def _corrupt_one_child(trie: PatriciaTrie) -> None:
    internal = _internal_below_root(trie)
    trie.arena.child[2 * internal + 1] = NO_NODE


def _corrupt_swapped_children(trie: PatriciaTrie) -> None:
    # A and B stay reachable and still extend their parent, and the sum is
    # unchanged: only the branch bit is wrong.
    arena = trie.arena
    internal = _internal_below_root(trie)
    zero, one = arena.child[2 * internal], arena.child[2 * internal + 1]
    arena.child[2 * internal], arena.child[2 * internal + 1] = one, zero


def _corrupt_host_bits(trie: PatriciaTrie) -> None:
    # The /31 (/127) node's last bit is a host bit; setting it leaves both
    # children extending the node's first `length` bits.
    arena = trie.arena
    internal = _internal_below_root(trie)
    assert arena.length[internal] < trie.bit_length
    arena.network[internal] |= 1


def _corrupt_length_too_long(trie: PatriciaTrie) -> None:
    trie.arena.length[_leaf_below_root(trie)] = trie.bit_length + 1


def _corrupt_network_list_longer(trie: PatriciaTrie) -> None:
    trie.arena.network.append(0)


def _corrupt_hot_count_list_longer(trie: PatriciaTrie) -> None:
    trie.arena.hot_count.append(0)


def _corrupt_child_list_odd(trie: PatriciaTrie) -> None:
    trie.arena.child.append(NO_NODE)


REPRESENTATION_CORRUPTIONS = [
    pytest.param(_corrupt_cycle, id="cycle"),
    pytest.param(_corrupt_shared_subtree, id="shared-subtree"),
    pytest.param(_corrupt_child_id_past_capacity, id="child-id-equal-to-capacity"),
    pytest.param(_corrupt_child_id_far_past_capacity, id="child-id-far-past-capacity"),
    pytest.param(_corrupt_negative_child_id, id="child-id-minus-2"),
    pytest.param(_corrupt_one_child, id="one-child"),
    pytest.param(_corrupt_swapped_children, id="wrong-branch-bit"),
    pytest.param(_corrupt_host_bits, id="host-bits-set"),
    pytest.param(_corrupt_length_too_long, id="length-above-bit-length"),
    pytest.param(_corrupt_network_list_longer, id="network-list-longer"),
    pytest.param(_corrupt_hot_count_list_longer, id="hot-count-list-longer"),
    pytest.param(_corrupt_child_list_odd, id="child-list-odd"),
]


@pytest.mark.parametrize("corrupt", REPRESENTATION_CORRUPTIONS)
@pytest.mark.parametrize("family", FAMILIES)
def test_check_patricia_diagnoses_a_broken_representation(
    family: AddressFamily, corrupt: Callable[[PatriciaTrie], None]
) -> None:
    """Decision 6's shape and decision 5's storage identity, checked by
    decision 8 / A2: each corruption is an `InvariantViolation` from
    `check_patricia` and from `check_trie` (which runs it first) -- never an
    `IndexError`, a `ValueError` or a hang. The cycle case is also the
    demonstration that the walk terminates. Only the type is asserted: more
    than one clause can fire on some of these, and the ADR does not order
    them."""

    trie = _patricia_with_three(family)
    check_patricia(trie)
    corrupt(trie)
    with pytest.raises(InvariantViolation):
        check_patricia(trie)
    with pytest.raises(InvariantViolation):
        check_trie(trie)


# --------------------------------------------------------------------------
# check_patricia's free-list clauses (decision 5 promise 3, A11). Each case
# passes every accounting clause that predates A11, so only a new one can
# catch it.
# --------------------------------------------------------------------------


def _adds_only(family: AddressFamily) -> PatriciaTrie:
    """Three adds and no removal: five live, reachable nodes, nothing free."""

    trie = _patricia_with_three(family)
    arena = trie.arena
    assert arena.free_ids == []
    assert arena.capacity == arena.live_count == trie.node_count == 5
    return trie


def _assert_old_accounting_holds(trie: PatriciaTrie) -> None:
    """The pre-A11 clauses: len(R) == node_count == live_count == 2 * leaves - 1,
    and capacity == live_count + free_count. R is 5 nodes (or 3 after one
    removal) in every case below, so these must all be true."""

    arena = trie.arena
    assert arena.live_count == trie.node_count == 2 * trie.hot_ip_count - 1
    assert arena.capacity == arena.live_count + arena.free_count
    assert arena.free_count == len(arena.free_ids)


@pytest.mark.parametrize("family", FAMILIES)
class TestFreeListClauses:
    def test_a_live_reachable_id_on_the_free_list_is_caught(self, family: AddressFamily) -> None:
        """A11's motivating hole: a live, reachable id queued for reuse,
        cancelled out by an unrelated live slot nobody links to. The order
        matters -- allocating after the append would pop the id back off."""

        trie = _adds_only(family)
        arena = trie.arena
        arena.allocate(network=0, length=0)
        arena.free_ids.append(trie.root)
        assert (arena.capacity, arena.free_count, arena.live_count) == (6, 1, 5)
        assert arena.is_live(trie.root) is True
        _assert_old_accounting_holds(trie)
        with pytest.raises(InvariantViolation):
            check_patricia(trie)
        with pytest.raises(InvariantViolation):
            check_trie(trie)

    def test_a_resurrected_slot_left_on_the_free_list_is_caught(
        self, family: AddressFamily
    ) -> None:
        """A dead slot's `length` set back to a live value while its id stays
        queued: "every id in free_ids is dead" is the only clause it breaks."""

        trie = _adds_only(family)
        assert trie.remove_hot_ip(SAMPLES[family][2]) is True
        arena = trie.arena
        assert len(arena.free_ids) == 2
        arena.length[arena.free_ids[-1]] = 0
        assert (arena.capacity, arena.free_count, arena.live_count) == (5, 2, 3)
        _assert_old_accounting_holds(trie)
        with pytest.raises(InvariantViolation):
            check_patricia(trie)
        with pytest.raises(InvariantViolation):
            check_trie(trie)

    def test_a_dead_id_listed_twice_is_caught(self, family: AddressFamily) -> None:
        """set(free_ids) == {y} == the dead slots, and the live, unreachable x
        compensates for the double-counted entry, so only "no id twice" can
        fire."""

        trie = _adds_only(family)
        arena = trie.arena
        x = arena.allocate(network=0, length=0)
        y = arena.allocate(network=0, length=0)
        arena.release(y)
        arena.free_ids.append(y)
        assert arena.free_ids == [y, y]
        assert arena.is_live(x) is True
        assert (arena.capacity, arena.free_count, arena.live_count) == (7, 2, 5)
        assert {i for i in range(arena.capacity) if arena.length[i] < 0} == {y}
        _assert_old_accounting_holds(trie)
        with pytest.raises(InvariantViolation):
            check_patricia(trie)
        with pytest.raises(InvariantViolation):
            check_trie(trie)

    @pytest.mark.parametrize("where", ["capacity", "no-node"])
    def test_an_out_of_range_free_list_entry_is_caught(
        self, family: AddressFamily, where: str
    ) -> None:
        """A11: an entry outside `[0, capacity)` is reported, not indexed with
        -- an `InvariantViolation`, never an `IndexError`. A `-1` used as an
        index would read the last slot, which here is live."""

        trie = _adds_only(family)
        arena = trie.arena
        arena.allocate(network=0, length=0)
        entry = arena.capacity if where == "capacity" else NO_NODE
        arena.free_ids.append(entry)
        assert (arena.capacity, arena.free_count, arena.live_count) == (6, 1, 5)
        _assert_old_accounting_holds(trie)
        with pytest.raises(InvariantViolation):
            check_patricia(trie)
        with pytest.raises(InvariantViolation):
            check_trie(trie)

    def test_a_valid_trie_after_a_removal_passes(self, family: AddressFamily) -> None:
        """The free-list clauses accept what `remove_hot_ip` and `add_hot_ip`
        actually produce: exactly the dead slots, each once."""

        _a, _b, c, _other = SAMPLES[family]
        trie = _adds_only(family)
        assert trie.remove_hot_ip(c) is True
        arena = trie.arena
        check_patricia(trie)
        check_trie(trie)
        assert arena.free_count == len(arena.free_ids) == 2
        assert len(set(arena.free_ids)) == 2
        for node in arena.free_ids:
            assert arena.is_live(node) is False
            assert arena.length[node] < 0
        dead = {i for i in range(arena.capacity) if arena.length[i] < 0}
        assert set(arena.free_ids) == dead

        # Re-adding reuses both slots and leaves nothing free.
        assert trie.add_hot_ip(c) is True
        check_patricia(trie)
        check_trie(trie)
        assert arena.free_ids == []
        assert arena.capacity == 5


# --------------------------------------------------------------------------
# A mutator raises on corruption it cannot walk past (decision 3, A12).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("corrupted_count", [2, 0])
@pytest.mark.parametrize("family", FAMILIES)
def test_add_on_a_corrupted_leaf_raises_and_changes_nothing(
    family: AddressFamily, corrupted_count: int
) -> None:
    """A12: a single-address Patricia trie is one node, the root, which is
    that address's leaf (decision 6, A8). With its count corrupted away from
    1, `contains()` says the address is not HOT, and `add_hot_ip` then finds
    a leaf it cannot proceed past: it raises `InvariantViolation` before
    mutating anything. The checks diagnose the same state. Patricia only --
    `BinaryTrie` need not match on a corrupted trie (A12 clause 4)."""

    address = SAMPLES[family][0]
    trie = PatriciaTrie(family)
    assert trie.add_hot_ip(address) is True
    arena = trie.arena
    assert trie.node_count == 1
    assert arena.length[trie.root] == trie.bit_length

    arena.hot_count[trie.root] = corrupted_count
    assert trie.contains(address) is False  # count-derived (decision 2, A3)
    with pytest.raises(InvariantViolation):
        check_patricia(trie)
    with pytest.raises(InvariantViolation):
        check_hot_counts(trie)

    before = _observe(trie)
    storage_before = (trie.root, _arena_state(arena))
    with pytest.raises(InvariantViolation):
        trie.add_hot_ip(address)
    assert _observe(trie) == before
    assert (trie.root, _arena_state(arena)) == storage_before

    # Still exactly as broken, and still diagnosed.
    with pytest.raises(InvariantViolation):
        check_patricia(trie)
    with pytest.raises(InvariantViolation):
        check_hot_counts(trie)


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
