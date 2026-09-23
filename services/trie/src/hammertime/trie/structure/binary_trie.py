"""Reference bit-by-bit trie: add_hot_ip, remove_hot_ip, lookup, longest match.

Spec: section 10, section 11, section 39; ADR-0014 decisions 2, 3, 4 and 7.

add_hot_ip:    walk the address bits, node.hot_count += 1 at every visited node
remove_hot_ip: same path, node.hot_count -= 1, then prune every emptied node

Simple and obviously correct. It is the oracle the Patricia implementation is
differentially tested against, so it deliberately shares no counting, walking
or pruning code with `patricia.py` and does not use `NodeArena` (decision 7):
one plain `TrieNode` per bit level, a child reference per branch.

Membership is decided before anything is mutated, so a redundant add or
remove changes nothing and returns `False` (decision 3), and section 11's
`hot_count >= 0` holds by construction. Pruning is mandatory (decision 4):
the node set is exactly the set of prefixes with a positive count, so
`node_count == len(list(iter_prefix_counts()))`. Its storage is private; no
attribute name is part of the contract (Amendment 1, A5).
"""

from collections.abc import Iterator

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.trie.structure.node import NodeView, PrefixCount, TrieNode


class BinaryTrie:
    """One node per prefix on every HOT address's path (sections 10, 11, 39)."""

    def __init__(self, family: AddressFamily) -> None:
        self._family = family
        self._bit_length = family.bit_length
        self._root: TrieNode | None = None
        # Allowed to be maintained (Amendment 1, A2): equals the number of
        # nodes reachable from the root, which check_no_orphaned_nodes checks.
        self._node_count = 0

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

        return 0 if self._root is None else self._root.hot_count

    @property
    def node_count(self) -> int:
        return self._node_count

    # ------------------------------------------------------------------
    # Mutation.
    # ------------------------------------------------------------------

    def add_hot_ip(self, address: Address) -> bool:
        self._check_family(address.family)
        if self.contains(address):
            return False
        if self._root is None:
            self._root = TrieNode()
            self._node_count += 1
        node = self._root
        node.hot_count += 1
        for bit in address.bits():
            nxt = node.child[bit]
            if nxt is None:
                nxt = TrieNode()
                node.child[bit] = nxt
                self._node_count += 1
            nxt.hot_count += 1
            node = nxt
        return True

    def remove_hot_ip(self, address: Address) -> bool:
        self._check_family(address.family)
        if not self.contains(address):
            return False
        # contains() is True, so every node on the path exists.
        node = self._root
        assert node is not None
        node.hot_count -= 1
        if node.hot_count == 0:
            self._root = None
            self._node_count -= self._bit_length + 1
            return True
        for depth, bit in enumerate(address.bits(), start=1):
            nxt = node.child[bit]
            assert nxt is not None
            nxt.hot_count -= 1
            if nxt.hot_count == 0:
                # Everything below an emptied node on this path is empty too:
                # detaching it prunes the whole tail, depth .. bit_length.
                node.child[bit] = None
                self._node_count -= self._bit_length - depth + 1
                return True
            node = nxt
        return True

    def clear(self) -> None:
        self._root = None
        self._node_count = 0

    # ------------------------------------------------------------------
    # Queries.
    # ------------------------------------------------------------------

    def contains(self, address: Address) -> bool:
        self._check_family(address.family)
        node = self._root
        for bit in address.bits():
            if node is None:
                return False
            node = node.child[bit]
        return node is not None and node.hot_count == 1

    def hot_count(self, prefix: Prefix) -> int:
        self._check_family(prefix.family)
        node = self._root
        for index in range(prefix.length):
            if node is None:
                return 0
            bit = (prefix.network >> (self._bit_length - 1 - index)) & 1
            node = node.child[bit]
        return 0 if node is None else node.hot_count

    def ancestor_counts(self, address: Address, *, min_length: int = 0) -> tuple[PrefixCount, ...]:
        self._check_family(address.family)
        self._check_min_length(min_length)
        counts: list[int] = []
        node = self._root
        counts.append(0 if node is None else node.hot_count)
        for bit in address.bits():
            node = None if node is None else node.child[bit]
            counts.append(0 if node is None else node.hot_count)
        return tuple(
            PrefixCount(self._prefix(address.value, length), counts[length])
            for length in range(min_length, self._bit_length + 1)
        )

    def longest_matching_prefix(self, address: Address) -> Prefix | None:
        self._check_family(address.family)
        best: int | None = None
        node = self._root
        if node is not None and node.hot_count > 0:
            best = 0
        for length, bit in enumerate(address.bits(), start=1):
            if node is None:
                break
            node = node.child[bit]
            if node is not None and node.hot_count > 0:
                best = length
        return None if best is None else self._prefix(address.value, best)

    def iter_prefix_counts(self, *, min_length: int = 0) -> Iterator[PrefixCount]:
        # Descent is by link; the count decides only what is yielded (A3).
        for node, network, length in self._walk():
            if node.hot_count > 0 and length >= min_length:
                yield PrefixCount(self._make_prefix(network, length), node.hot_count)

    def iter_hot_addresses(self) -> Iterator[Address]:
        # Structural: every materialized leaf, counts never consulted (A3).
        for _node, network, length in self._walk():
            if length == self._bit_length:
                yield Address(family=self._family, value=network)

    def iter_nodes(self) -> Iterator[NodeView]:
        # Structural: every reachable node, its stored count verbatim (A3).
        for node, network, length in self._walk():
            children = tuple(
                self._make_prefix(network | (bit << (self._bit_length - 1 - length)), length + 1)
                for bit in (0, 1)
                if node.child[bit] is not None
            )
            yield NodeView(self._make_prefix(network, length), node.hot_count, children)

    # ------------------------------------------------------------------
    # Helpers private to the reference implementation.
    # ------------------------------------------------------------------

    def _walk(self) -> Iterator[tuple[TrieNode, int, int]]:
        """Pre-order DFS by child link, branch 0 before branch 1."""

        if self._root is None:
            return
        stack: list[tuple[TrieNode, int, int]] = [(self._root, 0, 0)]
        while stack:
            node, network, length = stack.pop()
            yield node, network, length
            for bit in (1, 0):  # pushed 1 first so 0 is visited first
                child = node.child[bit]
                if child is not None:
                    shift = self._bit_length - 1 - length
                    stack.append((child, network | (bit << shift), length + 1))

    def _prefix(self, value: int, length: int) -> Prefix:
        host_bits = self._bit_length - length
        return self._make_prefix((value >> host_bits) << host_bits, length)

    def _make_prefix(self, network: int, length: int) -> Prefix:
        return Prefix(family=self._family, network=network, length=length)

    def _check_family(self, family: AddressFamily) -> None:
        if family is not self._family:
            raise ValueError(f"this trie holds {self._family} addresses; got an {family} argument")

    def _check_min_length(self, min_length: int) -> None:
        if not 0 <= min_length <= self._bit_length:
            raise ValueError(
                f"min_length must be in [0, {self._bit_length}] for {self._family}, "
                f"got {min_length}"
            )
