"""hot_count stays exact and non-negative across add/remove streams.

Spec: section 11 (`hot_count >= 0` MUST remain an invariant after removal;
empty nodes MAY be pruned; oscillation must not churn allocation), section
12 (`hot_count(node)` is the number of HOT /32s beneath it, `hot_count(/32)
in {0, 1}`, an internal node's count is the sum of its children's -- "more
important than cached `prefix_state`"), section 27 (the logical model stays
the binary trie under compression), section 38 (core invariants), section
46.5 (the attribute map's keys are exactly the HOT addresses of a family),
section 46.6 (prefix metadata outlives hot state).

ADR-0012 decision 3 lists the invariants `check_invariants` enforces:

    I1  hot_count(node) == hot_count(child0) + hot_count(child1)   (both tries)
    I2  a leaf has hot_count in {0, 1}                              (both)
    I3  hot_count >= 0 everywhere                                    (both)
    I4  hot_count(root) == hot_ip_count == len(list(hot_ips()))      (both)
    I5  a child's network/prefix_length agree with its parent's      (both)
    I6  every non-root node has hot_count > 0 or is pinned           (Patricia)
    I7  every non-root node with exactly one child is pinned         (Patricia)
    I8  node_count == arena.live_count, no orphans                   (Patricia; A6)
    I9  node_count <= 2 * hot_ip_count + pinned_count + 1            (Patricia)

and the `hammertime.testkit.invariants` helpers that restate I1-I4 and
section 46.5 against a structural protocol. Decision 2 pins the arena
behaviour these tests observe through `node_count` alone: a freed slot is
reused before the arena grows, so oscillation is bounded by the peak.

What is and is not covered here:

* `check_invariants` and the testkit helpers are shown to *pass* on every
  state a correct trie can reach through its public API: empty, after
  adds, after removes, after oscillation, with metadata set and cleared.
* I6, I7 and I9 are re-derived here from `edges()` so that a Patricia trie
  that violated them would fail even if `check_invariants` were lenient.
* I8 (`node_count == arena.live_count`, and every live slot reachable from
  the root) is asserted directly, through the two read-only properties
  Amendment 1 item A6 added for exactly this purpose: `PatriciaTrie.arena`
  and `PatriciaTrie.root`. `TestPatriciaArenaReachability` walks the
  materialized tree by id from `root` through `arena[node].child(bit)` and
  compares the reachable set with `set(arena.live_ids())`. `BinaryTrie`
  gains neither property (A6: it need not use an arena at all, assumption
  25), so those tests are Patricia-only.
* The negative path of `check_invariants` (that it *raises*
  `InvariantViolation` on a broken trie) is not covered: no public call
  breaks an invariant, and these tests read no implementation internals.
  The negative path of the testkit helpers is covered instead, with a fake
  `HotCountTrie` that lies.

ASSUMPTIONS -- details the ADR does not pin:

1. `Address`, `AddressFamily`, `Prefix` come from the
   `hammertime.core.addressing.address` / `.prefix` submodules (as in every
   other test in this repo).
2. IPv6 examples put the IPv4 example's 32 bits under `2001:db8::/96`.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.testkit.invariants import (
    HotCountTrie,
    ancestor_of,
    assert_attribute_records_consistent,
    assert_hot_count_consistent,
    assert_no_negative_counts,
    covers,
    expected_hot_counts,
    root_prefix,
)
from hammertime.trie.structure.binary_trie import BinaryTrie
from hammertime.trie.structure.invariants import check_invariants
from hammertime.trie.structure.node import NO_NODE
from hammertime.trie.structure.patricia import PatriciaTrie

Trie = BinaryTrie | PatriciaTrie

TRIE_CLASSES: tuple[type[BinaryTrie] | type[PatriciaTrie], ...] = (BinaryTrie, PatriciaTrie)
FAMILIES = (AddressFamily.IPV4, AddressFamily.IPV6)

V6_DOC = 0x2001_0DB8 << 96
SECTION_42_NETWORK = 0x0A14_1E00

OSCILLATION_ADDRESSES = 50
OSCILLATION_CYCLES = 20


def _ip(family: AddressFamily, low: int) -> Address:
    value = low if family is AddressFamily.IPV4 else V6_DOC | low
    return Address(family=family, value=value)


def _section_42(family: AddressFamily, count: int = 156) -> list[Address]:
    return [_ip(family, SECTION_42_NETWORK | host) for host in range(1, count + 1)]


def _spread(family: AddressFamily, count: int) -> list[Address]:
    """`count` addresses spread over the whole family so paths branch high
    up as well as low down (a mix of clustered and far-apart values)."""

    bits = family.bit_length
    out: list[Address] = []
    for i in range(count):
        if i % 3 == 0:
            out.append(_ip(family, SECTION_42_NETWORK | (i & 0xFF)))
        elif i % 3 == 1:
            out.append(_ip(family, 0xC0A8_0000 | ((i * 7919) & 0xFFFF)))
        else:
            out.append(Address(family=family, value=(i * 0x9E37_79B9_7F4A_7C15) % (1 << bits)))
    return list(dict.fromkeys(out))


def _pinned_count(trie: Trie) -> int:
    return sum(1 for edge in trie.edges() if edge.pinned)


def _check_all(trie: Trie) -> None:
    """The implementation's own check plus the testkit's restatements."""

    check_invariants(trie)
    assert_hot_count_consistent(trie)
    assert_no_negative_counts(trie)
    assert_attribute_records_consistent(trie, list(trie.hot_ips()))


def _assert_patricia_shape(trie: PatriciaTrie) -> None:
    """I6, I7 and I9 re-derived from `edges()`."""

    bits = trie.family.bit_length
    edges = list(trie.edges())
    prefixes = [edge.prefix for edge in edges]
    assert len(set(prefixes)) == len(prefixes)
    for edge in edges:
        if edge.prefix.length == 0:
            assert edge.parent_length == -1
            continue
        assert -1 < edge.parent_length < edge.prefix.length, edge
        # I6: immediate pruning.
        assert edge.hot_count > 0 or edge.pinned, edge
        children = [
            child
            for child in edges
            if child.parent_length == edge.prefix.length and covers(edge.prefix, child.prefix)
        ]
        assert len(children) <= 2, edge
        # I7: path compression -- an unpinned interior node is a branch point.
        if edge.prefix.length < bits and not edge.pinned:
            assert len(children) == 2, edge
        if edge.prefix.length == bits:
            assert not children
            assert edge.hot_count in (0, 1)
    # I9
    assert trie.node_count <= 2 * trie.hot_ip_count + _pinned_count(trie) + 1


@pytest.fixture(params=TRIE_CLASSES, ids=["binary", "patricia"])
def trie_class(request: pytest.FixtureRequest) -> type[BinaryTrie] | type[PatriciaTrie]:
    param: type[BinaryTrie] | type[PatriciaTrie] = request.param
    return param


@pytest.fixture(params=FAMILIES, ids=["ipv4", "ipv6"])
def family(request: pytest.FixtureRequest) -> AddressFamily:
    param: AddressFamily = request.param
    return param


@pytest.fixture
def trie(trie_class: type[BinaryTrie] | type[PatriciaTrie], family: AddressFamily) -> Trie:
    return trie_class(family)


@pytest.fixture
def patricia(family: AddressFamily) -> PatriciaTrie:
    return PatriciaTrie(family)


class TestInvariantsHoldOnEveryReachableState:
    """`check_invariants` and the testkit helpers pass wherever the public
    API can take a trie."""

    def test_on_an_empty_trie(self, trie: Trie) -> None:
        _check_all(trie)

    def test_after_adds(self, trie: Trie, family: AddressFamily) -> None:
        for ip in _section_42(family):
            assert trie.add_hot_ip(ip)
            _check_all(trie)

    def test_after_spread_adds(self, trie: Trie, family: AddressFamily) -> None:
        for ip in _spread(family, 60):
            assert trie.add_hot_ip(ip)
        _check_all(trie)

    def test_after_removes(self, trie: Trie, family: AddressFamily) -> None:
        ips = _section_42(family)
        for ip in ips:
            trie.add_hot_ip(ip)
        for ip in ips[::2]:
            assert trie.remove_hot_ip(ip)
            _check_all(trie)
        for ip in ips[1::2]:
            assert trie.remove_hot_ip(ip)
        _check_all(trie)
        assert trie.hot_ip_count == 0

    def test_after_redundant_adds_and_removes(self, trie: Trie, family: AddressFamily) -> None:
        ips = _spread(family, 20)
        for ip in ips:
            trie.add_hot_ip(ip)
            assert trie.add_hot_ip(ip) is False
            _check_all(trie)
        for ip in ips:
            assert trie.remove_hot_ip(ip) is True
            assert trie.remove_hot_ip(ip) is False
            _check_all(trie)
        for ip in ips:
            assert trie.remove_hot_ip(ip) is False
        _check_all(trie)

    def test_after_oscillation(self, trie: Trie, family: AddressFamily) -> None:
        # Section 11: frequent HOT/COLD oscillation. The same 50 addresses
        # go HOT and COLD 20 times; every intermediate state is checked.
        ips = _spread(family, OSCILLATION_ADDRESSES)
        for _ in range(OSCILLATION_CYCLES):
            for ip in ips:
                assert trie.add_hot_ip(ip) is True
            _check_all(trie)
            assert trie.hot_ip_count == len(ips)
            for ip in ips:
                assert trie.remove_hot_ip(ip) is True
            _check_all(trie)
            assert trie.hot_ip_count == 0

    def test_after_interleaved_oscillation(self, trie: Trie, family: AddressFamily) -> None:
        ips = _spread(family, OSCILLATION_ADDRESSES)
        hot: set[Address] = set()
        for cycle in range(OSCILLATION_CYCLES):
            for index, ip in enumerate(ips):
                if (index + cycle) % 3 == 0:
                    assert trie.add_hot_ip(ip) is (ip not in hot)
                    hot.add(ip)
                else:
                    assert trie.remove_hot_ip(ip) is (ip in hot)
                    hot.discard(ip)
            _check_all(trie)
            assert set(trie.hot_ips()) == hot

    def test_after_metadata_set_and_clear(self, trie: Trie, family: AddressFamily) -> None:
        bits = family.bit_length
        ips = _section_42(family, 12)
        for ip in ips[:6]:
            trie.add_hot_ip(ip)
        targets = [
            root_prefix(family),
            ancestor_of(ips[0], bits - 24),
            ancestor_of(ips[0], bits - 8),
            ancestor_of(ips[0], bits - 2),
            ancestor_of(ips[0], bits),
            ancestor_of(ips[11], bits),  # not hot
            ancestor_of(_ip(family, 0xC0A8_0000), bits - 16),  # nothing hot beneath
        ]
        for prefix in targets:
            trie.set_local_metadata(prefix, "policy", "internal")
            _check_all(trie)
        for ip in ips[6:]:
            trie.add_hot_ip(ip)
            _check_all(trie)
        for ip in ips:
            trie.remove_hot_ip(ip)
            _check_all(trie)
        for prefix in targets:
            assert dict(trie.local_metadata(prefix)) == {"policy": "internal"}
            trie.clear_local_metadata(prefix, "policy")
            _check_all(trie)
        assert trie.hot_ip_count == 0

    def test_helpers_accept_both_tries_as_hot_count_tries(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        # The testkit protocol is satisfied structurally (decision 3).
        checked: HotCountTrie = trie
        for ip in _section_42(family, 5):
            trie.add_hot_ip(ip)
        assert_hot_count_consistent(checked)
        assert_no_negative_counts(checked)
        assert_attribute_records_consistent(checked, _section_42(family, 5))


class TestAttributeRecordsInvariant:
    """Section 46.5: `{a for a in records if a.family is trie.family} ==
    set(hot_ips())`."""

    def test_passes_when_records_match(self, trie: Trie, family: AddressFamily) -> None:
        ips = _section_42(family, 10)
        for ip in ips:
            trie.add_hot_ip(ip)
        assert_attribute_records_consistent(trie, ips)
        assert_attribute_records_consistent(trie, reversed(ips))

    def test_records_of_the_other_family_are_ignored(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        other = AddressFamily.IPV6 if family is AddressFamily.IPV4 else AddressFamily.IPV4
        ips = _section_42(family, 3)
        for ip in ips:
            trie.add_hot_ip(ip)
        foreign = [Address(family=other, value=1), Address(family=other, value=2)]
        other_trie = BinaryTrie(other)
        for ip in foreign:
            other_trie.add_hot_ip(ip)
        # One record list, two families, both consistent.
        assert_attribute_records_consistent(trie, [*ips, *foreign])
        assert_attribute_records_consistent(other_trie, [*ips, *foreign])

    def test_a_stale_record_fails(self, trie: Trie, family: AddressFamily) -> None:
        ips = _section_42(family, 3)
        for ip in ips:
            trie.add_hot_ip(ip)
        with pytest.raises(AssertionError):
            assert_attribute_records_consistent(trie, [*ips, _ip(family, 0xC0A8_012A)])

    def test_a_missing_record_fails(self, trie: Trie, family: AddressFamily) -> None:
        ips = _section_42(family, 3)
        for ip in ips:
            trie.add_hot_ip(ip)
        with pytest.raises(AssertionError):
            assert_attribute_records_consistent(trie, ips[1:])

    def test_empty_trie_and_no_records(self, trie: Trie) -> None:
        assert_attribute_records_consistent(trie, [])


class TestPatriciaBounds:
    """Decision 2 / I9: live nodes bounded by the hot set plus pins plus the
    root; freed slots reused so oscillation does not grow the structure."""

    def test_i9_after_every_add_and_remove(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        ips = _spread(family, 60)
        for ip in ips:
            patricia.add_hot_ip(ip)
            _assert_patricia_shape(patricia)
        for ip in ips:
            patricia.remove_hot_ip(ip)
            _assert_patricia_shape(patricia)
        assert patricia.node_count == 1

    def test_i9_with_pinned_nodes(self, patricia: PatriciaTrie, family: AddressFamily) -> None:
        bits = family.bit_length
        ips = _section_42(family, 20)
        pins = [ancestor_of(ips[0], length) for length in (0, bits - 24, bits - 8, bits - 3, bits)]
        for prefix in pins:
            patricia.set_local_metadata(prefix, "policy", "internal")
            _assert_patricia_shape(patricia)
        assert _pinned_count(patricia) == len(pins)
        for ip in ips:
            patricia.add_hot_ip(ip)
            _assert_patricia_shape(patricia)
        for ip in ips:
            patricia.remove_hot_ip(ip)
            _assert_patricia_shape(patricia)
        assert _pinned_count(patricia) == len(pins)
        assert patricia.node_count == len(pins)
        for prefix in pins:
            patricia.clear_local_metadata(prefix, "policy")
            _assert_patricia_shape(patricia)
        assert patricia.node_count == 1

    def test_node_count_after_churn_never_exceeds_the_first_peak(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        # Decision 2: a freed slot is handed out again before the arena
        # grows, so re-filling a drained trie with different addresses
        # cannot need more live nodes than the first fill did.
        first = _spread(family, 40)
        flipped = [Address(family=family, value=ip.value ^ 0x5555) for ip in first]
        already = set(first)
        second = [ip for ip in flipped if ip not in already]
        assert second
        for ip in first:
            patricia.add_hot_ip(ip)
        first_peak = patricia.node_count
        assert 1 < first_peak <= 2 * len(first) + 1
        for ip in first:
            patricia.remove_hot_ip(ip)
        assert patricia.node_count == 1
        _check_all(patricia)
        for ip in second:
            patricia.add_hot_ip(ip)
        _check_all(patricia)
        _assert_patricia_shape(patricia)
        assert patricia.node_count <= first_peak

    def test_oscillation_returns_to_the_floor_every_cycle(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        ips = _spread(family, OSCILLATION_ADDRESSES)
        peaks: set[int] = set()
        for _ in range(OSCILLATION_CYCLES):
            for ip in ips:
                patricia.add_hot_ip(ip)
            peaks.add(patricia.node_count)
            _assert_patricia_shape(patricia)
            for ip in ips:
                patricia.remove_hot_ip(ip)
            assert patricia.node_count == 1
        # The same hot set always materializes the same nodes.
        assert len(peaks) == 1

    def test_node_count_tracks_the_hot_set_not_history(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        ips = _spread(family, 30)
        for ip in ips:
            patricia.add_hot_ip(ip)
        steady = patricia.node_count
        for _ in range(5):
            for ip in ips[:10]:
                patricia.remove_hot_ip(ip)
            for ip in ips[:10]:
                patricia.add_hot_ip(ip)
        assert patricia.node_count == steady

    def test_shape_rules_hold_under_metadata_churn(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        ips = _section_42(family, 8)
        for ip in ips:
            patricia.add_hot_ip(ip)
        for length in range(0, bits + 1, 3):
            prefix = ancestor_of(ips[3], length)
            patricia.set_local_metadata(prefix, "policy", length)
            _assert_patricia_shape(patricia)
            _check_all(patricia)
        for ip in ips:
            patricia.remove_hot_ip(ip)
            _assert_patricia_shape(patricia)
        for length in range(0, bits + 1, 3):
            patricia.clear_local_metadata(ancestor_of(ips[3], length), "policy")
            _assert_patricia_shape(patricia)
            _check_all(patricia)
        assert patricia.node_count == 1


def _reachable_ids(patricia: PatriciaTrie) -> set[int]:
    """Every node id reachable from `root` by following `child(0)`/`child(1)`
    through the arena (A6). A cycle or a repeated id would be a structural
    corruption, so revisiting an id is an error rather than a skip."""

    arena = patricia.arena
    seen: set[int] = set()
    stack = [patricia.root]
    while stack:
        node_id = stack.pop()
        assert node_id not in seen, f"node {node_id} reached twice: the trie is not a tree"
        seen.add(node_id)
        node = arena[node_id]
        for bit in (0, 1):
            child = node.child(bit)
            if child != NO_NODE:
                stack.append(child)
    return seen


class TestPatriciaArenaReachability:
    """Invariant I8, through the properties A6 added: `node_count ==
    arena.live_count`, and every live slot is reachable from `root` (no
    orphans). Patricia-only -- `BinaryTrie` has neither property."""

    def test_empty_trie_has_one_live_reachable_node(self, patricia: PatriciaTrie) -> None:
        assert patricia.node_count == patricia.arena.live_count == 1
        assert _reachable_ids(patricia) == set(patricia.arena.live_ids())
        assert patricia.root in set(patricia.arena.live_ids())

    def test_arena_is_the_same_object_throughout(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        # A6: "the trie's own arena, the same object for the trie's lifetime".
        arena = patricia.arena
        for ip in _section_42(family, 8):
            patricia.add_hot_ip(ip)
        assert patricia.arena is arena
        for ip in _section_42(family, 8):
            patricia.remove_hot_ip(ip)
        assert patricia.arena is arena

    def test_i8_after_every_add_and_remove(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        ips = _spread(family, 40)
        for ip in ips:
            patricia.add_hot_ip(ip)
            live = set(patricia.arena.live_ids())
            assert patricia.node_count == patricia.arena.live_count == len(live)
            assert _reachable_ids(patricia) == live
        for ip in ips:
            patricia.remove_hot_ip(ip)
            live = set(patricia.arena.live_ids())
            assert patricia.node_count == patricia.arena.live_count == len(live)
            assert _reachable_ids(patricia) == live
        assert patricia.node_count == 1

    def test_i8_holds_with_pinned_nodes(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        ips = _section_42(family, 12)
        pins = [ancestor_of(ips[0], length) for length in (0, bits - 24, bits - 8, bits)]
        for ip in ips:
            patricia.add_hot_ip(ip)
        for prefix in pins:
            patricia.set_local_metadata(prefix, "policy", "internal")
            assert _reachable_ids(patricia) == set(patricia.arena.live_ids())
        for ip in ips:
            patricia.remove_hot_ip(ip)
            assert _reachable_ids(patricia) == set(patricia.arena.live_ids())
        # Only the pinned nodes (and the root, which is one of them) survive.
        assert patricia.node_count == patricia.arena.live_count == len(pins)
        for prefix in pins:
            patricia.clear_local_metadata(prefix, "policy")
        assert patricia.node_count == patricia.arena.live_count == 1
        assert _reachable_ids(patricia) == set(patricia.arena.live_ids())

    def test_pruned_slots_leave_no_orphans(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        # A freed slot must leave both the live set and the reachable set;
        # an orphan would show as live-but-unreachable.
        ips = _spread(family, 24)
        for ip in ips:
            patricia.add_hot_ip(ip)
        peak = patricia.arena.live_count
        for ip in ips[::2]:
            patricia.remove_hot_ip(ip)
        live = set(patricia.arena.live_ids())
        assert patricia.arena.live_count < peak
        assert _reachable_ids(patricia) == live
        assert patricia.node_count == len(live)
        # The arena keeps the freed slots for reuse, so capacity does not drop.
        assert patricia.arena.capacity >= peak


@dataclass(frozen=True)
class _FakeEdge:
    prefix: Prefix
    parent_length: int
    hot_count: int
    pinned: bool


class _FakeTrie:
    """A `HotCountTrie` whose answers come from a table, so the testkit's
    helpers can be shown to reject inconsistent structures."""

    def __init__(
        self,
        family: AddressFamily,
        hot: Iterable[Address],
        *,
        counts: dict[Prefix, int] | None = None,
        edges: Iterable[_FakeEdge] = (),
    ) -> None:
        self._family = family
        self._hot = list(hot)
        self._counts = expected_hot_counts(self._hot) if counts is None else counts
        self._edges = list(edges)

    @property
    def family(self) -> AddressFamily:
        return self._family

    @property
    def hot_ip_count(self) -> int:
        return len(self._hot)

    def hot_ips(self) -> Iterator[Address]:
        return iter(self._hot)

    def hot_count(self, prefix: Prefix) -> int:
        return self._counts.get(prefix, 0)

    def edges(self) -> Iterator[_FakeEdge]:
        return iter(self._edges)


def _edge(prefix: Prefix, parent_length: int, hot_count: int, *, pinned: bool = False) -> _FakeEdge:
    return _FakeEdge(prefix=prefix, parent_length=parent_length, hot_count=hot_count, pinned=pinned)


def _faithful_edges(hot: Iterable[Address]) -> list[_FakeEdge]:
    """Edges of an uncompressed binary trie over `hot`: every positive
    logical prefix, parent one bit shorter."""

    counts = expected_hot_counts(hot)
    ordered = sorted(counts, key=lambda p: (p.length, p.network))
    return [_edge(p, p.length - 1, counts[p]) for p in ordered]


class TestTestkitHelpersRejectInconsistentTries:
    """The negative path of the testkit helpers, through a fake
    `HotCountTrie`, since no public call on a real trie can break I1-I4."""

    def test_a_faithful_fake_passes(self, family: AddressFamily) -> None:
        hot = _section_42(family, 5)
        fake = _FakeTrie(family, hot, edges=_faithful_edges(hot))
        assert_hot_count_consistent(fake)
        assert_no_negative_counts(fake)
        assert_attribute_records_consistent(fake, hot)

    def test_root_count_disagreeing_with_hot_ips_fails_i4(self, family: AddressFamily) -> None:
        hot = _section_42(family, 5)
        counts = expected_hot_counts(hot)
        counts[root_prefix(family)] = 4
        fake = _FakeTrie(family, hot, counts=counts, edges=_faithful_edges(hot))
        with pytest.raises(AssertionError, match="I4"):
            assert_hot_count_consistent(fake)

    def test_a_stored_count_disagreeing_with_the_recompute_fails_i1(
        self, family: AddressFamily
    ) -> None:
        hot = _section_42(family, 5)
        target = ancestor_of(hot[0], family.bit_length - 8)
        edges = _faithful_edges(hot)
        broken = [replace(e, hot_count=7) if e.prefix == target else e for e in edges]
        fake = _FakeTrie(family, hot, edges=broken)
        with pytest.raises(AssertionError, match="I1"):
            assert_hot_count_consistent(fake)

    def test_a_query_disagreeing_with_the_recompute_fails_i1(self, family: AddressFamily) -> None:
        # `hot_count(prefix)` answers one number, the node behind it stores
        # another. Since A12 the helper no longer queries every logical
        # prefix, but it still queries each prefix `edges()` reports, and
        # `_faithful_edges` reports this one.
        hot = _section_42(family, 5)
        counts = expected_hot_counts(hot)
        counts[ancestor_of(hot[0], family.bit_length - 10)] = 1
        fake = _FakeTrie(family, hot, counts=counts, edges=_faithful_edges(hot))
        with pytest.raises(AssertionError, match="I1"):
            assert_hot_count_consistent(fake)

    def test_a_duplicate_in_hot_ips_fails(self, family: AddressFamily) -> None:
        bits = family.bit_length
        ip = _ip(family, 0xC0A8_012A)
        # Every node stores 2 and hot_ips() yields the address twice: the
        # sum rule and I4 are self-consistent with each other, and only the
        # fact that a leaf stands for one address (I2, and a HOT set that is
        # a set) says this is wrong.
        counts = {ancestor_of(ip, length): 2 for length in range(bits + 1)}
        edges = [_edge(ancestor_of(ip, length), length - 1, 2) for length in range(bits + 1)]
        fake = _FakeTrie(family, [ip, ip], counts=counts, edges=edges)
        with pytest.raises(AssertionError):
            assert_hot_count_consistent(fake)

    def test_a_negative_count_fails_i3(self, family: AddressFamily) -> None:
        edges = [_edge(root_prefix(family), -1, -1)]
        fake = _FakeTrie(family, [], counts={root_prefix(family): -1}, edges=edges)
        with pytest.raises(AssertionError, match="I3"):
            assert_no_negative_counts(fake)

    def test_a_negative_root_query_fails_i3(self, family: AddressFamily) -> None:
        fake = _FakeTrie(family, [], counts={root_prefix(family): -3})
        with pytest.raises(AssertionError, match="I3"):
            assert_no_negative_counts(fake)

    def test_children_not_summing_to_the_parent_fails_i1(self, family: AddressFamily) -> None:
        bits = family.bit_length
        first = _ip(family, SECTION_42_NETWORK | 1)
        second = _ip(family, SECTION_42_NETWORK | 2)
        hot = [first, second]
        counts = expected_hot_counts(hot)
        branch = ancestor_of(first, bits - 2)
        # A Patricia-shaped edge list: root -> /30 -> two leaves, but one
        # leaf stores 0 for an address hot_ips() reports.
        edges = [
            _edge(root_prefix(family), -1, 2),
            _edge(branch, 0, 2),
            _edge(ancestor_of(first, bits), bits - 2, 1),
            _edge(ancestor_of(second, bits), bits - 2, 0),
        ]
        fake = _FakeTrie(family, hot, counts=counts, edges=edges)
        with pytest.raises(AssertionError, match="I1"):
            assert_hot_count_consistent(fake)

    def test_a_branch_point_compressed_away_fails_i1(self, family: AddressFamily) -> None:
        # The bug class the A12 rewrite's coverage check exists to catch: a
        # trie that ran two diverging paths into one compressed edge each,
        # skipping the branch node they share. Every *local* check passes --
        # each edge stores the right count for its own prefix, the leaves
        # sum to the root -- but the levels the two edges both span are
        # claimed with count 1 when two addresses lie under them.
        bits = family.bit_length
        first = _ip(family, SECTION_42_NETWORK | 1)
        second = _ip(family, SECTION_42_NETWORK | 2)
        hot = [first, second]
        edges = [
            _edge(root_prefix(family), -1, 2),
            _edge(ancestor_of(first, bits), 0, 1),
            _edge(ancestor_of(second, bits), 0, 1),
        ]
        fake = _FakeTrie(family, hot, edges=edges)
        with pytest.raises(AssertionError, match="I1"):
            assert_hot_count_consistent(fake)

    def test_a_compressed_but_consistent_edge_list_passes_every_check(
        self, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        first = _ip(family, SECTION_42_NETWORK | 1)
        second = _ip(family, SECTION_42_NETWORK | 2)
        hot = [first, second]
        branch = ancestor_of(first, bits - 2)
        edges = [
            _edge(root_prefix(family), -1, 2),
            _edge(branch, 0, 2),
            _edge(ancestor_of(first, bits), bits - 2, 1),
            _edge(ancestor_of(second, bits), bits - 2, 1),
        ]
        fake = _FakeTrie(family, hot, edges=edges)
        assert_hot_count_consistent(fake)
        assert_no_negative_counts(fake)
