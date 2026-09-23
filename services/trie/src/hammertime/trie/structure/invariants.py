"""Runtime invariant checks, cheap enough for debug builds and the inspect tool.

Spec: section 11, section 12, section 46.5; ADR-0014 decisions 8 and 9 (and
Amendment 1, A2, A7 and A8).

hot_count(node) == number of currently HOT /32 addresses in its subtree, and
hot_count(node) == hot_count(child[0]) + hot_count(child[1]).
This invariant matters more than any cached prefix_state (section 12).

Every check returns `None` or raises `InvariantViolation` naming the spec
section, the prefix or node id at fault and the two values that disagree.
They are side-effect free and never repair anything. Cost is
O(hot_ip_count * bit_length), which is why nothing on the hot path calls
them unconditionally.
"""

from collections import Counter
from collections.abc import Collection

from hammertime.core.addressing.address import Address
from hammertime.core.addressing.bits import bit_at, common_prefix_length
from hammertime.core.addressing.prefix import Prefix
from hammertime.core.errors import InvariantViolation
from hammertime.trie.structure.node import NO_NODE, HotTrie, NodeId
from hammertime.trie.structure.patricia import PatriciaTrie


def check_trie(trie: HotTrie) -> None:
    """Every check that applies to `trie`; the one entry point for debug builds.

    For a `PatriciaTrie` the representation check runs first: the logical
    checks traverse the structure, and on a broken representation their
    behaviour is undefined (Amendment 1, A2).
    """

    if isinstance(trie, PatriciaTrie):
        check_patricia(trie)
    check_hot_counts(trie)
    check_no_orphaned_nodes(trie)


def check_hot_counts(trie: HotTrie) -> None:
    """Recompute section 12 bottom-up from the HOT addresses and compare.

    The recomputed mapping must equal `dict(trie.iter_prefix_counts())` in both
    directions; `hot_ip_count` must equal the number of distinct HOT addresses;
    neither iteration may yield the same key twice.
    """

    hot = list(trie.iter_hot_addresses())
    repeated = sorted(str(address) for address, n in Counter(hot).items() if n > 1)
    if repeated:
        raise InvariantViolation(
            f"section 12: iter_hot_addresses() yielded {repeated} more than once; "
            f"expected each HOT address once"
        )
    foreign = sorted(str(address) for address in hot if address.family != trie.family)
    if foreign:
        raise InvariantViolation(
            f"section 35: iter_hot_addresses() of a {trie.family} trie yielded {foreign}"
        )
    if trie.hot_ip_count != len(hot):
        raise InvariantViolation(
            f"section 12: hot_ip_count (hot_count of the root) is {trie.hot_ip_count} but "
            f"iter_hot_addresses() yielded {len(hot)} distinct HOT addresses"
        )

    stored: dict[Prefix, int] = {}
    for prefix, count in trie.iter_prefix_counts():
        if prefix in stored:
            raise InvariantViolation(
                f"section 12: iter_prefix_counts() yielded {prefix} twice, with hot_count "
                f"{stored[prefix]} and {count}"
            )
        stored[prefix] = count

    expected: dict[Prefix, int] = {}
    bits = trie.bit_length
    for address in hot:
        for length in range(bits + 1):
            host_bits = bits - length
            prefix = Prefix(
                family=address.family,
                network=(address.value >> host_bits) << host_bits,
                length=length,
            )
            expected[prefix] = expected.get(prefix, 0) + 1

    for prefix, count in expected.items():
        actual = stored.get(prefix)
        if actual != count:
            shown = "absent" if actual is None else str(actual)
            raise InvariantViolation(
                f"section 12: hot_count({prefix}) is {shown} in iter_prefix_counts() but "
                f"{count} recomputed from the HOT addresses"
            )
    for prefix, count in stored.items():
        if prefix not in expected:
            raise InvariantViolation(
                f"section 12: iter_prefix_counts() yielded {prefix} with hot_count {count} "
                f"but no HOT address lies beneath it (recomputed 0)"
            )


def check_no_orphaned_nodes(trie: HotTrie) -> None:
    """No zero-count prefix or node survives (section 11, ADR-0014 decision 4)."""

    for prefix, count in trie.iter_prefix_counts():
        if count <= 0:
            raise InvariantViolation(
                f"section 11: {prefix} is present with hot_count {count}; expected > 0 "
                f"(a prefix with nothing HOT beneath it is pruned)"
            )
    materialized = 0
    for node in trie.iter_nodes():
        if node.hot_count <= 0:
            raise InvariantViolation(
                f"section 11: node {node.prefix} is materialized with hot_count "
                f"{node.hot_count}; expected > 0 (an emptied node is pruned)"
            )
        materialized += 1
    node_count = trie.node_count
    if materialized != node_count:
        raise InvariantViolation(
            f"section 11: node_count is {node_count} but iter_nodes() yielded {materialized} nodes"
        )
    if (trie.hot_ip_count == 0) != (node_count == 0):
        raise InvariantViolation(
            f"section 11: hot_ip_count is {trie.hot_ip_count} but node_count is "
            f"{node_count}; an empty trie holds no node and only an empty trie does"
        )


def check_attribute_records(trie: HotTrie, records: Collection[Address]) -> None:
    """The per-IP attribute records mirror the HOT set (section 46.5, decision 9).

    `records` is any collection of addresses -- a set, or the record map
    itself. The family clause is evaluated first, so a record of the other
    family is reported as such (Amendment 1, A7).
    """

    foreign = sorted(str(address) for address in records if address.family != trie.family)
    if foreign:
        raise InvariantViolation(
            f"section 46.5: the invariant is per address family, but records checked "
            f"against a {trie.family} trie include {foreign}"
        )
    if len(records) != trie.hot_ip_count:
        raise InvariantViolation(
            f"section 46.5: len(records) is {len(records)} but hot_count(root) is "
            f"{trie.hot_ip_count}"
        )
    hot = set(trie.iter_hot_addresses())
    keys = set(records)
    if keys != hot:
        missing = sorted(str(address) for address in hot - keys)
        extra = sorted(str(address) for address in keys - hot)
        raise InvariantViolation(
            f"section 46.5: set(records) differs from the HOT addresses: HOT without a "
            f"record {missing}, record without a HOT address {extra}"
        )


def check_patricia(trie: PatriciaTrie) -> None:
    """Decision 6's shape and decision 5's arena accounting, by an independent walk.

    The reachable set is computed here from `trie.root` and `arena.child` --
    never from `iter_nodes()`, `node_count` or `live_count` (Amendment 1, A2).
    Every id is range- and liveness-checked before any of its fields is read,
    and a visited set stops a cycle, so a corrupted arena is always an
    `InvariantViolation`.
    """

    arena = trie.arena
    bits = trie.bit_length
    capacity = len(arena.length)
    sizes = (len(arena.network), len(arena.hot_count), len(arena.child) // 2)
    if any(size != capacity for size in sizes) or len(arena.child) % 2:
        raise InvariantViolation(
            f"section 27: arena storage lists disagree: len(length) is {capacity}, "
            f"len(network) {sizes[0]}, len(hot_count) {sizes[1]}, len(child) "
            f"{len(arena.child)} (expected {2 * capacity})"
        )

    reached: set[NodeId] = set()
    internal: list[tuple[NodeId, NodeId, NodeId]] = []
    leaves = 0
    # (node id, parent id, branch taken from the parent)
    stack: list[tuple[NodeId, NodeId, int]] = []
    if trie.root != NO_NODE:
        stack.append((trie.root, NO_NODE, 0))
    while stack:
        nid, parent, branch = stack.pop()
        where = "root" if parent == NO_NODE else f"child[{branch}] of node {parent}"
        if not 0 <= nid < capacity:
            raise InvariantViolation(
                f"section 27: {where} is node id {nid}, outside the arena's [0, {capacity})"
            )
        if not arena.is_live(nid):
            raise InvariantViolation(
                f"section 27: {where} is node id {nid}, which is released (length "
                f"{arena.length[nid]}); expected a live node"
            )
        if nid in reached:
            raise InvariantViolation(
                f"section 27: node id {nid} is reached twice (again as {where}); expected a tree"
            )
        reached.add(nid)

        length = arena.length[nid]
        network = arena.network[nid]
        if length > bits:
            raise InvariantViolation(
                f"section 27: node {nid} has length {length}; expected <= {bits}"
            )
        if not 0 <= network < (1 << bits) or network & ((1 << (bits - length)) - 1):
            raise InvariantViolation(
                f"section 27: node {nid} has network {network:#x} at length {length}; "
                f"expected a {bits}-bit value with host bits zeroed"
            )
        if parent != NO_NODE:
            parent_length = arena.length[parent]
            if length <= parent_length:
                raise InvariantViolation(
                    f"section 27: node {nid} (length {length}) does not strictly extend "
                    f"its parent {parent} (length {parent_length})"
                )
            shared = common_prefix_length(network, arena.network[parent], bits)
            if shared < parent_length or bit_at(network, parent_length, bits) != branch:
                raise InvariantViolation(
                    f"section 27: node {nid} is {where} but its prefix does not extend "
                    f"its parent's with bit {branch} at position {parent_length}"
                )

        left = arena.child[2 * nid]
        right = arena.child[2 * nid + 1]
        count = arena.hot_count[nid]
        if left == NO_NODE and right == NO_NODE:
            if length != bits:
                raise InvariantViolation(
                    f"section 27: leaf node {nid} has length {length}; expected {bits}"
                )
            if count != 1:
                raise InvariantViolation(
                    f"section 12: leaf node {nid} has hot_count {count}; expected 1"
                )
            leaves += 1
        elif left == NO_NODE or right == NO_NODE:
            raise InvariantViolation(
                f"section 27: node {nid} has exactly one child (child[0] {left}, "
                f"child[1] {right}); expected none or two"
            )
        else:
            internal.append((nid, left, right))
            stack.append((right, nid, 1))
            stack.append((left, nid, 0))

    # Every id in `reached` is live and in range, so these reads are safe.
    for nid, left, right in internal:
        count = arena.hot_count[nid]
        total = arena.hot_count[left] + arena.hot_count[right]
        if count != total:
            raise InvariantViolation(
                f"section 12: node {nid} has hot_count {count} but its children "
                f"{left} and {right} sum to {total}"
            )

    size = len(reached)
    node_count = trie.node_count
    live_count = arena.live_count
    free_count = arena.free_count
    if size != node_count:
        raise InvariantViolation(
            f"section 27: {size} nodes are reachable from the root but node_count is {node_count}"
        )
    if size != live_count:
        raise InvariantViolation(
            f"section 11: {size} nodes are reachable from the root but the arena holds "
            f"{live_count} live slots"
        )
    if arena.capacity != live_count + free_count:
        raise InvariantViolation(
            f"section 11: arena capacity is {arena.capacity} but live_count "
            f"{live_count} + free_count {free_count} is {live_count + free_count}"
        )
    if size:
        if size != 2 * leaves - 1:
            raise InvariantViolation(
                f"section 27: {size} nodes reachable but {leaves} leaves; expected "
                f"{2 * leaves - 1} nodes"
            )
        if leaves != trie.hot_ip_count:
            raise InvariantViolation(
                f"section 12: {leaves} leaves reachable but hot_ip_count is {trie.hot_ip_count}"
            )
