"""Random add/remove streams preserve every trie invariant.

Spec: section 10 and section 11 (insertion and removal walk the path; after a
removal `hot_count >= 0`; pruning and the arena), section 12 (`hot_count(node)`
is the number of HOT addresses beneath it), section 26 (the trie holds only
currently HOT addresses), section 27 (arena/slab allocation), section 35 (one
trie per family), section 46.5 (the attribute records mirror the HOT set).

Epic #8's acceptance criteria 2 ("no orphaned nodes" after every step of a
randomized sequence) and 3 ("pruning without fragmentation blowing up
memory") as ADR-0014 makes them testable:

* every step passes the service's own `check_trie` and
  `check_attribute_records` (Consequences clause (2): "`check_trie` after
  every step of a randomized sequence"; for a `PatriciaTrie`, decision 8 runs
  `check_patricia` first, so this is the only randomized coverage its
  representation and arena-accounting clauses get);
* every step also keeps testkit's `assert_*` helpers silent. That is not a
  duplicate of the line above: decision 10 rule 2 makes testkit a deliberately
  independent recomputation that never calls the service's checks, so a
  checker wrong in the same direction as the structure is caught by the
  other one;
* a plain model set agrees with `hot_ip_count`, `iter_hot_addresses()`,
  `iter_prefix_counts()` and sampled `hot_count`s;
* the structure is a pure function of the HOT set (decision 4);
* a redundant add or remove returns False and changes nothing (decision 3);
* removing everything leaves no node, and for the Patricia trie no live arena
  slot (decisions 4 and 5);
* `arena.capacity` equals the greatest `node_count` seen since construction,
  exactly (decision 5, assumption 16), so oscillation never grows the slab;
* an attribute-record model updated by decision 3's rule -- set on every add
  event, deleted on every remove event, whatever the call returned -- always
  satisfies section 46.5.

Both representations and both families are covered. Streams draw from a small
address pool so that redundant operations actually happen.

What earlier versions of this docstring listed as unpinned assumptions is now
pinned by ADR-0014's amendments:

* `PatriciaTrie.arena` is the public name of the Patricia trie's `NodeArena`
  (Amendment 1, A1).
* `node_count` is defined by reachability, so `len(list(iter_nodes())) ==
  node_count` (A2), and the derived counts -- the binary trie's node set is
  its positive-count prefix set, a non-empty Patricia trie has
  `2 * hot_ip_count - 1` nodes -- are contract (A8).
* No operation allocates a node it does not keep, so comparing
  `arena.capacity` with the running maximum of `node_count` sampled between
  operations is exact (A4).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.testkit.generators import (
    TrieOperation,
    address_pools,
    prefixes,
    trie_operations,
)
from hammertime.testkit.invariants import (
    assert_attribute_records_match,
    assert_hot_count_consistent,
    assert_no_negative_counts,
    assert_no_orphaned_nodes,
)
from hammertime.trie.structure import (
    BinaryTrie,
    HotTrie,
    PatriciaTrie,
    PrefixCount,
    check_attribute_records,
    check_trie,
)
from hypothesis import given, settings
from hypothesis import strategies as st

TrieFactory = Callable[[AddressFamily], HotTrie]

IMPLEMENTATIONS = [
    pytest.param(BinaryTrie, id="binary"),
    pytest.param(PatriciaTrie, id="patricia"),
]
FAMILIES = [
    pytest.param(AddressFamily.IPV4, id="ipv4"),
    pytest.param(AddressFamily.IPV6, id="ipv6"),
]

MAX_EXAMPLES = 20


def _prefix_of(address: Address, length: int) -> Prefix:
    host_bits = address.bit_length - length
    network = (address.value >> host_bits) << host_bits
    return Prefix(family=address.family, network=network, length=length)


def _model_prefix_counts(model: Iterable[Address], family: AddressFamily) -> list[PrefixCount]:
    """Section 12 from the model, in decision 2's pre-order DFS order, which
    is ascending `(network, length)`: a prefix before its descendants, branch
    0 before branch 1."""

    counts: dict[Prefix, int] = {}
    for address in model:
        for length in range(family.bit_length + 1):
            prefix = _prefix_of(address, length)
            counts[prefix] = counts.get(prefix, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (item[0].network, item[0].length))
    return [PrefixCount(prefix, count) for prefix, count in ordered]


def _apply(trie: HotTrie, operation: TrieOperation, model: set[Address]) -> bool:
    """Apply `operation`, check its return value against the model, update the model."""

    address = operation.address
    if operation.kind == "add":
        expected = address not in model
        applied = trie.add_hot_ip(address)
        model.add(address)
    else:
        expected = address in model
        applied = trie.remove_hot_ip(address)
        model.discard(address)
    assert applied is expected, f"{operation.kind}({address}) returned {applied}"
    return applied


def _observe(trie: HotTrie, pool: Sequence[Address]) -> tuple[object, ...]:
    """Everything a caller can see about the trie, probed at the pool."""

    observed: list[object] = [
        trie.hot_ip_count,
        trie.node_count,
        list(trie.iter_prefix_counts()),
        list(trie.iter_hot_addresses()),
        list(trie.iter_nodes()),
        [trie.contains(a) for a in pool],
        [trie.ancestor_counts(a) for a in pool],
        [trie.longest_matching_prefix(a) for a in pool],
    ]
    if isinstance(trie, PatriciaTrie):
        arena = trie.arena
        observed.append((arena.live_count, arena.free_count, arena.capacity))
    return tuple(observed)


def _check_step(
    trie: HotTrie,
    model: set[Address],
    records: Collection[Address],
    probes: Iterable[Prefix],
) -> None:
    """Everything that must hold after any operation."""

    # The service's checks (Consequences clause (2); check_patricia runs first
    # for a PatriciaTrie, decision 8) ...
    check_trie(trie)
    check_attribute_records(trie, records)
    # ... and testkit's independent recomputation (decision 10 rule 2).
    assert_hot_count_consistent(trie)
    assert_no_negative_counts(trie)
    assert_no_orphaned_nodes(trie)
    assert_attribute_records_match(trie, records)

    assert trie.hot_ip_count == len(model)
    assert set(trie.iter_hot_addresses()) == model
    assert list(trie.iter_prefix_counts()) == _model_prefix_counts(model, trie.family)

    for prefix in probes:
        count = trie.hot_count(prefix)
        assert count >= 0, prefix
        assert count == sum(1 for a in model if prefix.contains(a)), prefix

    assert len(list(trie.iter_nodes())) == trie.node_count
    if isinstance(trie, PatriciaTrie):
        assert trie.node_count == (2 * len(model) - 1 if model else 0)
        assert trie.arena.live_count == trie.node_count
    else:
        assert trie.node_count == len(list(trie.iter_prefix_counts()))


def _run(trie: HotTrie, operations: Iterable[TrieOperation]) -> tuple[set[Address], int]:
    """Apply `operations`; return the final HOT set and the peak `node_count`."""

    model: set[Address] = set()
    peak = trie.node_count
    for operation in operations:
        _apply(trie, operation, model)
        peak = max(peak, trie.node_count)
    return model, peak


def _draw_stream(
    data: st.DataObject, family: AddressFamily
) -> tuple[list[Address], list[TrieOperation]]:
    pool = data.draw(address_pools(family, min_size=1, max_size=6), label="pool")
    operations = data.draw(trie_operations(pool, max_size=20), label="operations")
    return pool, operations


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
@given(data=st.data())
@settings(deadline=None, max_examples=MAX_EXAMPLES)
def test_every_step_keeps_every_invariant(
    make_trie: TrieFactory, family: AddressFamily, data: st.DataObject
) -> None:
    """Sections 11, 12, 46.5 and decision 5's capacity bound, after every step."""

    _pool, operations = _draw_stream(data, family)
    extra = data.draw(st.lists(prefixes(family), max_size=3), label="extra probes")
    root = Prefix(family=family, network=0, length=0)

    trie = make_trie(family)
    model: set[Address] = set()
    records: dict[Address, object] = {}
    peak = 0
    _check_step(trie, model, records, [root, *extra])

    for step, operation in enumerate(operations):
        _apply(trie, operation, model)
        # Section 46.5 / decision 3: the record map follows the event, not
        # the bool -- replace on every add, delete on every remove.
        if operation.kind == "add":
            records[operation.address] = {"attributes_version": 1, "step": step}
        else:
            records.pop(operation.address, None)

        ancestors = [_prefix_of(operation.address, n) for n in range(family.bit_length + 1)]
        _check_step(trie, model, records, [*ancestors, *extra])

        peak = max(peak, trie.node_count)
        if isinstance(trie, PatriciaTrie):
            assert trie.arena.capacity == peak
            assert trie.arena.capacity == trie.arena.live_count + trie.arena.free_count


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
@given(data=st.data())
@settings(deadline=None, max_examples=MAX_EXAMPLES)
def test_structure_depends_only_on_the_hot_set(
    make_trie: TrieFactory, family: AddressFamily, data: st.DataObject
) -> None:
    """Decision 4: the same HOT set reached by any order of adds and removes
    gives identical structure, counts and iteration order."""

    _pool, operations = _draw_stream(data, family)
    trie = make_trie(family)
    model, _peak = _run(trie, operations)

    order = data.draw(st.permutations(sorted(model, key=lambda a: a.value)), label="order")
    rebuilt = make_trie(family)
    for address in order:
        assert rebuilt.add_hot_ip(address) is True

    assert list(rebuilt.iter_prefix_counts()) == list(trie.iter_prefix_counts())
    assert rebuilt.node_count == trie.node_count
    assert list(rebuilt.iter_nodes()) == list(trie.iter_nodes())
    assert list(rebuilt.iter_hot_addresses()) == list(trie.iter_hot_addresses())


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
@given(data=st.data())
@settings(deadline=None, max_examples=MAX_EXAMPLES)
def test_a_repeated_operation_is_an_observable_no_op(
    make_trie: TrieFactory, family: AddressFamily, data: st.DataObject
) -> None:
    """Decision 3 and section 11: repeating an add or a remove returns False
    and leaves every observable -- counts, nodes, arena -- exactly as it was."""

    pool, operations = _draw_stream(data, family)
    trie = make_trie(family)
    model: set[Address] = set()
    for operation in operations:
        _apply(trie, operation, model)
        before = _observe(trie, pool)
        repeated = _apply(trie, operation, model)
        assert repeated is False
        assert _observe(trie, pool) == before

    # Every pool address, from the final state: an add of a HOT one and a
    # remove of a non-HOT one are both no-ops.
    for address in pool:
        before = _observe(trie, pool)
        if address in model:
            assert trie.add_hot_ip(address) is False
        else:
            assert trie.remove_hot_ip(address) is False
        assert _observe(trie, pool) == before


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
@given(data=st.data())
@settings(deadline=None, max_examples=MAX_EXAMPLES)
def test_removing_every_address_empties_the_trie(
    make_trie: TrieFactory, family: AddressFamily, data: st.DataObject
) -> None:
    """Decision 4: pruning is mandatory, so an empty HOT set is an empty trie.
    Decision 5: the slab keeps its peak capacity until `clear()`."""

    _pool, operations = _draw_stream(data, family)
    trie = make_trie(family)
    model, peak = _run(trie, operations)

    order = data.draw(st.permutations(sorted(model, key=lambda a: a.value)), label="order")
    for address in order:
        assert trie.remove_hot_ip(address) is True
        model.discard(address)
        _check_step(trie, model, set(model), [])

    assert trie.hot_ip_count == 0
    assert trie.node_count == 0
    assert list(trie.iter_prefix_counts()) == []
    assert list(trie.iter_hot_addresses()) == []
    assert list(trie.iter_nodes()) == []
    if isinstance(trie, PatriciaTrie):
        assert trie.arena.live_count == 0
        assert trie.arena.capacity == peak
        assert trie.arena.free_count == peak
        trie.clear()
        assert trie.arena.capacity == 0


# Per family: two far-apart addresses and a third that diverges from both.
SAMPLES: dict[AddressFamily, tuple[Address, Address, Address]] = {
    AddressFamily.IPV4: (
        Address.parse("192.168.1.42"),
        Address.parse("10.0.0.1"),
        Address.parse("172.16.0.1"),
    ),
    AddressFamily.IPV6: (
        Address.parse("2001:db8::2a"),
        Address.parse("fe80::1"),
        Address.parse("2001:db8:1::1"),
    ),
}


@pytest.mark.parametrize("family", FAMILIES)
def test_oscillation_never_grows_the_arena(family: AddressFamily) -> None:
    """Section 11's HOT/COLD churn, absorbed by decision 5's free list: a
    released slot is reused before the slab grows, so capacity tracks the peak
    number of live nodes and nothing else. Node counts are `2n - 1` for `n`
    HOT addresses (decision 6)."""

    a, b, c = SAMPLES[family]
    trie = PatriciaTrie(family)

    assert trie.add_hot_ip(a) is True
    assert (trie.node_count, trie.arena.capacity) == (1, 1)
    assert trie.add_hot_ip(b) is True
    assert (trie.node_count, trie.arena.capacity) == (3, 3)

    for _ in range(100):
        assert trie.remove_hot_ip(b) is True
        assert (trie.node_count, trie.arena.capacity, trie.arena.free_count) == (1, 3, 2)
        assert trie.add_hot_ip(b) is True
        assert (trie.node_count, trie.arena.capacity, trie.arena.free_count) == (3, 3, 0)

    assert trie.add_hot_ip(c) is True
    assert (trie.node_count, trie.arena.capacity) == (5, 5)
    for _ in range(50):
        assert trie.remove_hot_ip(c) is True
        assert trie.add_hot_ip(c) is True
        assert (trie.node_count, trie.arena.capacity) == (5, 5)

    for address in (a, b, c):
        assert trie.remove_hot_ip(address) is True
    assert (trie.node_count, trie.arena.live_count) == (0, 0)
    assert (trie.arena.capacity, trie.arena.free_count) == (5, 5)

    trie.clear()
    assert trie.arena.capacity == 0
    # The bound is re-established from scratch after clear() (decision 5.4).
    assert trie.add_hot_ip(a) is True
    assert (trie.node_count, trie.arena.capacity) == (1, 1)
