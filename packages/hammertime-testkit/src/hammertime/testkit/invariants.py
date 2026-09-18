"""Executable spec invariants, used by tests and by the trie-inspect tool.

Spec: section 11 (`hot_count >= 0` MUST remain an invariant after removal),
section 12 (`hot_count(node)` is the number of currently HOT `/32`
addresses under `node`; `hot_count(/32) in {0, 1}`; an internal node's count
is the sum of its children's), section 38 (core invariants), section 46.5
(the attribute map's derived invariant: its keys are exactly the HOT
addresses of the family).

ADR-0012 decision 3 lists what lives here and pins the shape:

    class HotCountTrie(Protocol)                     # satisfied structurally
    assert_hot_count_consistent(trie)                # I1, I2, I4 recomputed from hot_ips()
    assert_no_negative_counts(trie)                  # I3 over edges()
    assert_attribute_records_consistent(trie, records)   # section 46.5
    assert_hysteresis_holds(history)                 # unchanged stub (section 38 is
                                                     # covered by test_state_machine.py)

The checks are written against the protocol only, so this module imports
nothing from `hammertime.trie` (ADR-0012 decision 1: the testkit depends on
`hammertime-core` and `hypothesis` alone) and works for the `BinaryTrie`
oracle, the `PatriciaTrie`, and any fake a test builds.

Every helper raises `AssertionError` with a message naming the offending
prefix; none of them uses a bare `assert`, so they keep working under `-O`.

`assert_hot_count_consistent` is O(`|hot| * bit_length` + `node_count *
bit_length` + `node_count**2`), as Amendment 1 item A12 asks: the expected
count of every ancestor is accumulated in **one** pass over `hot_ips()`
(`expected_hot_counts`) and then compared against `edges()`, rather than
re-querying `hot_count(prefix)` for each of the `|hot| * (bit_length + 1)`
logical prefixes, which made it O(`|hot| * bit_length**2`) and dominated
the property machine's per-step cost. The dropped per-prefix query --
"every compressed-away logical prefix answers with the count of the node
below it" -- is still covered, by `test_structure.py`'s
`test_every_logical_prefix_counts_its_hot_descendants` and by
`test_patricia_equivalence.py`'s `path_counts` comparison, neither of which
runs per step.

ASSUMPTIONS -- details decision 3 does not pin:

1. `HotCountTrie.family` is declared as a read-only property rather than the
   plain attribute the ADR sketch shows. A read-only protocol property is
   satisfied by a plain attribute *and* by a `@property`, so this is the
   strictly more permissive spelling of the same requirement.
2. `ancestor_of`, `root_prefix` and `expected_hot_counts` are exposed as
   public helpers beyond the ADR's list because the trie tests and the
   Hypothesis strategies all need "the /L ancestor of this address" and
   "the counts section 12 says every prefix must carry", and the recompute
   is the same arithmetic these assertions use.
"""

from collections.abc import Iterable, Iterator
from typing import Any, Protocol

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix


class HotCountTrie(Protocol):
    """The structural slice of a trie the invariant checks need.

    Satisfied by `BinaryTrie` and `PatriciaTrie` without either importing
    this module. `edges()` yields items carrying `.prefix` (a `Prefix`),
    `.parent_length` (the materialized parent's length, -1 for the root),
    `.hot_count` and `.pinned`.
    """

    @property
    def family(self) -> AddressFamily: ...

    @property
    def hot_ip_count(self) -> int: ...

    def hot_ips(self) -> Iterator[Address]: ...

    def hot_count(self, prefix: Prefix) -> int: ...

    def edges(self) -> Iterator[Any]: ...


def ancestor_of(ip: Address, length: int) -> Prefix:
    """The `/length` prefix containing `ip` -- the logical node at depth
    `length` on the address's path (section 8: a /24 is the first 24 bits,
    an individual IP is /32)."""

    bits = ip.bit_length
    if not 0 <= length <= bits:
        raise ValueError(f"prefix length {length} out of range for {ip.family.name}")
    shift = bits - length
    return Prefix(family=ip.family, network=(ip.value >> shift) << shift, length=length)


def root_prefix(family: AddressFamily) -> Prefix:
    """The /0 of `family`: the whole address space, the trie's root."""

    return Prefix(family=family, network=0, length=0)


def truncate(prefix: Prefix, length: int) -> Prefix:
    """`prefix` masked back to `length` bits -- the ancestor of `prefix` at
    that depth. Used to expand a compressed edge into the logical prefixes
    it spans (ADR-0012 decision 3)."""

    bits = prefix.family.bit_length
    if not 0 <= length <= prefix.length:
        raise ValueError(f"cannot truncate a /{prefix.length} to /{length}")
    shift = bits - length
    return Prefix(family=prefix.family, network=(prefix.network >> shift) << shift, length=length)


def covers(parent: Prefix, child: Prefix) -> bool:
    """True iff `child` lies in `parent`'s subtree (same family, not shorter,
    agreeing on `parent`'s network bits)."""

    if child.family is not parent.family or child.length < parent.length:
        return False
    shift = parent.family.bit_length - parent.length
    return (child.network >> shift) == (parent.network >> shift)


def expected_hot_counts(hot: Iterable[Address]) -> dict[Prefix, int]:
    """Section 12 recomputed from first principles: for every distinct HOT
    address, every one of its `bit_length + 1` ancestors gains one. The
    result maps each logical prefix with a non-zero count to that count;
    a prefix absent from the mapping has count 0."""

    counts: dict[Prefix, int] = {}
    for ip in set(hot):
        for length in range(ip.bit_length + 1):
            prefix = ancestor_of(ip, length)
            counts[prefix] = counts.get(prefix, 0) + 1
    return counts


def assert_hot_count_consistent(trie: HotCountTrie) -> None:
    """Recompute hot_count bottom-up from `hot_ips()` and compare with the
    stored values (section 12): ADR-0012 invariants I1, I2 and I4."""

    bits = trie.family.bit_length
    hot = list(trie.hot_ips())
    if len(set(hot)) != len(hot):
        raise AssertionError("hot_ips() yields the same address more than once")
    for ip in hot:
        if ip.family is not trie.family:
            raise AssertionError(f"hot_ips() yields {ip} of the wrong family for {trie.family}")

    # I4: hot_count(root) == hot_ip_count == len(list(hot_ips()))
    root = root_prefix(trie.family)
    root_count = trie.hot_count(root)
    if not (trie.hot_ip_count == len(hot) == root_count):
        raise AssertionError(
            f"I4: hot_ip_count={trie.hot_ip_count}, len(hot_ips())={len(hot)}, "
            f"hot_count({root})={root_count} disagree"
        )

    expected = expected_hot_counts(hot)
    edges = list(trie.edges())

    # I1 as section 12 states it: every materialized node stores exactly the
    # number of HOT addresses in its subtree, and the query agrees with it.
    for edge in edges:
        want = expected.get(edge.prefix, 0)
        if edge.hot_count != want:
            raise AssertionError(
                f"I1: {edge.prefix} stores hot_count {edge.hot_count} "
                f"but {want} HOT address(es) lie under it"
            )
        queried = trie.hot_count(edge.prefix)
        if queried != edge.hot_count:
            raise AssertionError(
                f"I1: hot_count({edge.prefix}) returns {queried}, the node stores {edge.hot_count}"
            )
        # I2: a leaf is a single address.
        if edge.prefix.length == bits and edge.hot_count not in (0, 1):
            raise AssertionError(f"I2: leaf {edge.prefix} has hot_count {edge.hot_count}")

    # I1 as the sum over the materialized children (an absent child counts 0;
    # for a compressed representation the materialized children carry the
    # whole count of the logical subtrees they stand for).
    for edge in edges:
        if edge.prefix.length == bits:
            continue
        children = [
            child
            for child in edges
            if child.parent_length == edge.prefix.length
            and child.prefix.length > edge.prefix.length
            and covers(edge.prefix, child.prefix)
        ]
        if len(children) > 2:
            raise AssertionError(f"I5: {edge.prefix} has {len(children)} children; at most 2")
        total = sum(child.hot_count for child in children)
        if total != edge.hot_count:
            raise AssertionError(
                f"I1: {edge.prefix} has hot_count {edge.hot_count} but its children sum to {total}"
            )

    # Every logical prefix with a positive count, expanded from edges() the
    # way decision 3 defines the logical view, must be exactly the set the
    # recompute produced. This is the statement the dropped per-prefix
    # `hot_count()` query made, in one pass over the edges instead of one
    # descent per prefix (A12).
    derived: dict[Prefix, int] = {}
    for edge in edges:
        if edge.hot_count <= 0:
            continue
        for length in range(edge.parent_length + 1, edge.prefix.length + 1):
            derived[truncate(edge.prefix, length)] = edge.hot_count
    if derived != expected:
        differing = {p for p in set(derived) | set(expected) if derived.get(p) != expected.get(p)}
        first = sorted(differing, key=lambda p: (p.length, p.network))[:3]
        detail = "; ".join(
            f"{p}: edges say {derived.get(p, 0)}, hot_ips() say {expected.get(p, 0)}" for p in first
        )
        raise AssertionError(
            f"I1: the logical view of edges() disagrees with the recompute from hot_ips(); {detail}"
        )


def assert_no_negative_counts(trie: HotCountTrie) -> None:
    """`hot_count >= 0` at every node (section 11): ADR-0012 invariant I3."""

    if trie.hot_ip_count < 0:
        raise AssertionError(f"I3: hot_ip_count is {trie.hot_ip_count}")
    root = root_prefix(trie.family)
    if trie.hot_count(root) < 0:
        raise AssertionError(f"I3: hot_count({root}) is {trie.hot_count(root)}")
    for edge in trie.edges():
        if edge.hot_count < 0:
            raise AssertionError(f"I3: {edge.prefix} has hot_count {edge.hot_count}")


def assert_attribute_records_consistent(trie: HotCountTrie, records: Iterable[Address]) -> None:
    """Section 46.5: the per-IP attribute map's keys of this family are
    exactly the currently HOT addresses (`len(records) == hot_count(root)`
    per address family)."""

    of_family = {ip for ip in records if ip.family is trie.family}
    hot = set(trie.hot_ips())
    if of_family != hot:
        missing = sorted(str(ip) for ip in hot - of_family)
        stale = sorted(str(ip) for ip in of_family - hot)
        raise AssertionError(
            f"section 46.5: attribute records disagree with the HOT set for {trie.family.name}: "
            f"HOT without a record {missing}, record without HOT {stale}"
        )


def assert_hysteresis_holds(history: object) -> None:
    """No COLD->HOT below hot_threshold, no HOT->COLD at or above cold_threshold (section 38)."""
    raise NotImplementedError
