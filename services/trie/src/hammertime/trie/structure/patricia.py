"""Path-compressed trie: the production representation.

Spec: section 27, section 39; ADR-0014 decisions 2, 3, 4, 5 and 6 (and
Amendment 1; Amendment 2, A12 and A13).

A run of single-child nodes becomes one compressed edge, so a sparse /32 does
not cost 32 nodes. The logical model MUST stay equivalent to the binary trie
(section 27) -- that equivalence is enforced by differential tests against
`binary_trie.py`, with which this module deliberately shares no traversal code
(decision 7).

Shape (decision 6): every node is either a leaf at `length == bit_length` with
no children, or an internal node with exactly two children whose prefixes
strictly extend it and differ at bit `length`. Its `hot_count` is 1 for a
leaf and the sum of its children otherwise, so `node_count == 2 *
hot_ip_count - 1` for a non-empty trie. `root` is a `NodeId` or `NO_NODE`;
there is no permanent `/0` node.

Storage is the public `arena` (a `NodeArena`, never rebound) plus `root`
(Amendment 1, A1). `add_hot_ip` only allocates and `remove_hot_ip` only
releases, and neither takes a scratch node (A4): a split allocates the new
internal node and the new leaf and keeps both; a collapse releases the leaf
and its now single-child parent and links the surviving sibling into the
grandparent.

`add_hot_ip` raises `InvariantViolation` if its walk reaches a
`bit_length`-long node for the address whose count is not 1 -- a leaf
`contains()` did not accept, which no sequence of public calls produces. It
raises before anything has been mutated, so the trie is left exactly as it
was; this is the package's only such mutator guard (ADR-0014 Amendment 2,
A12).

`iter_prefix_counts` validates `min_length` exactly as `ancestor_counts`
does, when it is called rather than when its iterator is first advanced
(Amendment 2, A13).
"""

from collections.abc import Iterator

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.bits import bit_at, common_prefix_length
from hammertime.core.addressing.prefix import Prefix
from hammertime.core.errors import InvariantViolation
from hammertime.trie.structure.arena import NodeArena
from hammertime.trie.structure.node import NO_NODE, NodeId, NodeView, PrefixCount


class PatriciaTrie:
    """Arena-backed, path-compressed trie for one address family (section 27)."""

    def __init__(self, family: AddressFamily) -> None:
        self._family = family
        self._bit_length = family.bit_length
        self.arena: NodeArena = NodeArena()
        self.root: NodeId = NO_NODE

    # ------------------------------------------------------------------
    # Properties.
    # ------------------------------------------------------------------

    @property
    def family(self) -> AddressFamily:
        return self._family

    @property
    def bit_length(self) -> int:
        return self._bit_length

    @property
    def hot_ip_count(self) -> int:
        """The root's stored count; never a separate counter (Amendment 1, A3)."""

        return 0 if self.root == NO_NODE else self.arena.hot_count[self.root]

    @property
    def node_count(self) -> int:
        """Reachable nodes; in an intact trie that is every live arena slot (A2)."""

        return self.arena.live_count

    # ------------------------------------------------------------------
    # Mutation.
    # ------------------------------------------------------------------

    def add_hot_ip(self, address: Address) -> bool:
        self._check_family(address.family)
        if self.contains(address):
            return False
        arena = self.arena
        bits = self._bit_length
        value = address.value
        if self.root == NO_NODE:
            self.root = arena.allocate(network=value, length=bits, hot_count=1)
            return True

        path: list[NodeId] = []  # nodes wholly above the new leaf, root first
        parent = NO_NODE
        nid = self.root
        while True:
            length = arena.length[nid]
            common = common_prefix_length(arena.network[nid], value, bits)
            if common < length:
                # The paths diverge inside the edge above `nid`: split it with
                # a new internal node at `common`, holding `nid` and a new leaf.
                host_bits = bits - common
                internal = arena.allocate(
                    network=(value >> host_bits) << host_bits,
                    length=common,
                    hot_count=arena.hot_count[nid] + 1,
                )
                leaf = arena.allocate(network=value, length=bits, hot_count=1)
                branch = bit_at(value, common, bits)
                arena.child[2 * internal + branch] = leaf
                arena.child[2 * internal + 1 - branch] = nid
                self._relink(parent, value, internal)
                break
            if length == bits:
                # A leaf for this address exists but contains() said its count
                # is not 1: the trie is corrupt and the walk cannot proceed.
                # Nothing has been mutated at this point, so the trie is left
                # exactly as it was (ADR-0014 Amendment 2, A12; the only such
                # guard in this package).
                raise InvariantViolation(
                    f"section 12: leaf node {nid} for {address} has hot_count "
                    f"{arena.hot_count[nid]}; expected 1"
                )
            # `nid` is a proper prefix of the address, so it is internal and
            # has both children.
            path.append(nid)
            parent = nid
            nid = arena.child[2 * nid + bit_at(value, length, bits)]
        for ancestor in path:
            arena.hot_count[ancestor] += 1
        return True

    def remove_hot_ip(self, address: Address) -> bool:
        self._check_family(address.family)
        if not self.contains(address):
            return False
        arena = self.arena
        bits = self._bit_length
        value = address.value

        path: list[NodeId] = []  # root .. leaf
        nid = self.root
        while True:
            path.append(nid)
            length = arena.length[nid]
            if length == bits:
                break
            nid = arena.child[2 * nid + bit_at(value, length, bits)]

        leaf = path[-1]
        if len(path) == 1:
            # The root is the leaf: the trie becomes empty.
            self.root = NO_NODE
            arena.release(leaf)
            return True

        parent = path[-2]
        branch = bit_at(value, arena.length[parent], bits)
        sibling = arena.child[2 * parent + 1 - branch]
        grandparent = path[-3] if len(path) >= 3 else NO_NODE
        # The parent is left with one child, so it collapses: the grandparent
        # (or the root reference) takes the sibling directly.
        self._relink(grandparent, value, sibling)
        for ancestor in path[:-2]:
            arena.hot_count[ancestor] -= 1
        arena.release(leaf)
        arena.release(parent)
        return True

    def clear(self) -> None:
        self.arena.clear()
        self.root = NO_NODE

    # ------------------------------------------------------------------
    # Queries.
    # ------------------------------------------------------------------

    def contains(self, address: Address) -> bool:
        self._check_family(address.family)
        return self._count_at(address.value, self._bit_length) == 1

    def hot_count(self, prefix: Prefix) -> int:
        self._check_family(prefix.family)
        return self._count_at(prefix.network, prefix.length)

    def ancestor_counts(self, address: Address, *, min_length: int = 0) -> tuple[PrefixCount, ...]:
        self._check_family(address.family)
        self._check_min_length(min_length)
        counts = self._counts_along(address.value)
        return tuple(
            PrefixCount(self._prefix_of(address.value, length), counts[length])
            for length in range(min_length, self._bit_length + 1)
        )

    def longest_matching_prefix(self, address: Address) -> Prefix | None:
        self._check_family(address.family)
        counts = self._counts_along(address.value)
        for length in range(self._bit_length, -1, -1):
            if counts[length] > 0:
                return self._prefix_of(address.value, length)
        return None

    def iter_prefix_counts(self, *, min_length: int = 0) -> Iterator[PrefixCount]:
        # Not a generator function: min_length is validated at call time,
        # not on first advance (Amendment 2, A13).
        self._check_min_length(min_length)
        return self._iter_prefix_counts(min_length)

    def iter_hot_addresses(self) -> Iterator[Address]:
        # Structural: every reachable node at bit_length, counts never read (A3).
        arena = self.arena
        for nid, _parent_length in self._dfs():
            if arena.length[nid] == self._bit_length:
                yield Address(family=self._family, value=arena.network[nid])

    def iter_nodes(self) -> Iterator[NodeView]:
        # Structural: every reachable node, stored count verbatim (A3).
        arena = self.arena
        for nid, _parent_length in self._dfs():
            children = tuple(
                self._node_prefix(child)
                for child in (arena.child[2 * nid], arena.child[2 * nid + 1])
                if child != NO_NODE
            )
            yield NodeView(self._node_prefix(nid), arena.hot_count[nid], children)

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------

    def _count_at(self, network: int, target_length: int) -> int:
        """Decision 6's walk: the count of the prefix `network/target_length`."""

        arena = self.arena
        bits = self._bit_length
        nid = self.root
        while nid != NO_NODE:
            length = arena.length[nid]
            common = common_prefix_length(arena.network[nid], network, bits)
            if common < min(length, target_length):
                return 0  # the paths diverge
            if length >= target_length:
                return arena.hot_count[nid]  # this node's subtree is the prefix's
            nid = arena.child[2 * nid + bit_at(network, length, bits)]
        return 0

    def _counts_along(self, value: int) -> list[int]:
        """`hot_count` of every prefix of `value`, indexed by length 0..bit_length.

        One walk: lengths `parent+1 .. min(node length, divergence)` take the
        node's count, every length past a divergence takes 0 (decision 6).
        """

        arena = self.arena
        bits = self._bit_length
        counts = [0] * (bits + 1)
        filled = -1  # highest length already assigned
        nid = self.root
        while nid != NO_NODE:
            length = arena.length[nid]
            common = common_prefix_length(arena.network[nid], value, bits)
            count = arena.hot_count[nid]
            last = min(length, common)
            for index in range(filled + 1, last + 1):
                counts[index] = count
            filled = max(filled, last)
            if common < length or length == bits:
                break
            nid = arena.child[2 * nid + bit_at(value, length, bits)]
        return counts

    def _iter_prefix_counts(self, min_length: int) -> Iterator[PrefixCount]:
        """`iter_prefix_counts`'s generator; `min_length` is already validated."""

        # Every compressed edge expanded into the prefixes it stands for. The
        # stored count decides only whether a prefix is yielded; descent is by
        # link (Amendment 1, A3).
        arena = self.arena
        for nid, parent_length in self._dfs():
            count = arena.hot_count[nid]
            if count <= 0:
                continue
            network = arena.network[nid]
            for length in range(max(parent_length + 1, min_length), arena.length[nid] + 1):
                yield PrefixCount(self._prefix_of(network, length), count)

    def _dfs(self) -> Iterator[tuple[NodeId, int]]:
        """Pre-order DFS by link, branch 0 first; yields `(nid, parent's length)`."""

        if self.root == NO_NODE:
            return
        arena = self.arena
        stack: list[tuple[NodeId, int]] = [(self.root, -1)]
        while stack:
            nid, parent_length = stack.pop()
            yield nid, parent_length
            length = arena.length[nid]
            for child in (arena.child[2 * nid + 1], arena.child[2 * nid]):
                if child != NO_NODE:
                    stack.append((child, length))

    def _relink(self, parent: NodeId, value: int, new_child: NodeId) -> None:
        """Point `parent`'s branch toward `value` (or the root) at `new_child`."""

        if parent == NO_NODE:
            self.root = new_child
        else:
            branch = bit_at(value, self.arena.length[parent], self._bit_length)
            self.arena.child[2 * parent + branch] = new_child

    def _node_prefix(self, nid: NodeId) -> Prefix:
        return Prefix(
            family=self._family, network=self.arena.network[nid], length=self.arena.length[nid]
        )

    def _prefix_of(self, value: int, length: int) -> Prefix:
        host_bits = self._bit_length - length
        return Prefix(family=self._family, network=(value >> host_bits) << host_bits, length=length)

    def _check_family(self, family: AddressFamily) -> None:
        if family is not self._family:
            raise ValueError(f"this trie holds {self._family} addresses; got an {family} argument")

    def _check_min_length(self, min_length: int) -> None:
        if not 0 <= min_length <= self._bit_length:
            raise ValueError(
                f"min_length must be in [0, {self._bit_length}] for {self._family}, "
                f"got {min_length}"
            )
