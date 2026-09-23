"""Slab/arena allocation with integer node ids.

Spec: section 11, section 27; ADR-0014 decision 5 (and Amendment 1, A1 and A4;
Amendment 2, A11).

HOT/COLD oscillation causes allocation churn if nodes are freed eagerly
(section 11). An arena with integer ids also keeps nodes contiguous and
cache-friendly (section 27). `NodeArena` is the Patricia trie's storage and
only the Patricia trie's (ADR-0014 decision 7).

Promises (decision 5):

* A `NodeId` is an index into parallel lists; both children of node `nid`
  live at `child[2 * nid]` and `child[2 * nid + 1]`.
* `allocate` reuses a released id before growing, and grows by exactly one
  node's worth of slots. Released ids wait on the public free list
  `free_ids`, part of the arena's interface like the four storage lists
  (Amendment 2, A11): LIFO, its last entry the next id handed out, and
  `free_count == len(free_ids)`. Only `allocate`, `release` and `clear`
  touch it. A fresh arena pre-allocates nothing, so
  `capacity` is exactly the peak number of simultaneously live nodes since
  construction or the last `clear()`.
* `release` writes `-1` into `length`, so a released slot is detectably dead
  and a double release is a `ValueError`. A released slot's other fields are
  stale and must not be read. `length` is the only record of deadness, so
  `allocate` rejects a negative `length` with a `ValueError`. The free list
  holds exactly the dead slots -- `set(free_ids) == {i : length[i] < 0}`,
  and no id is listed twice (A11); `check_patricia` checks both.
* `live_count` is derived, `len(length) - len(free_ids)`, and stays O(1)
  (A11): it is what `PatriciaTrie.node_count` returns.
* `clear()` truncates every list in place.
"""

from hammertime.trie.structure.node import NO_NODE, NodeId


class NodeArena:
    """Slab of Patricia node records addressed by integer `NodeId` (section 27)."""

    __slots__ = ("child", "free_ids", "hot_count", "length", "network")

    def __init__(self) -> None:
        # Parallel storage, indexed by NodeId. Public: this *is* the arena's
        # interface, and the Patricia trie reads and writes it directly.
        self.network: list[int] = []
        self.length: list[int] = []
        self.hot_count: list[int] = []
        self.child: list[NodeId] = []
        # Released ids, LIFO: the last entry is the next one allocated (A11).
        self.free_ids: list[NodeId] = []

    def allocate(self, *, network: int, length: int, hot_count: int = 0) -> NodeId:
        """Return a live node id with the given fields and no children."""

        # `length < 0` is the only record of deadness, so a live slot may
        # never carry one (Amendment 2, A11).
        if length < 0:
            raise ValueError(f"node length must be >= 0, got {length}")
        if self.free_ids:
            node_id = self.free_ids.pop()
            self.network[node_id] = network
            self.length[node_id] = length
            self.hot_count[node_id] = hot_count
            self.child[2 * node_id] = NO_NODE
            self.child[2 * node_id + 1] = NO_NODE
            return node_id
        node_id = len(self.length)
        self.network.append(network)
        self.length.append(length)
        self.hot_count.append(hot_count)
        self.child.append(NO_NODE)
        self.child.append(NO_NODE)
        return node_id

    def release(self, node_id: NodeId) -> None:
        """Return `node_id` to the free list; releasing a dead id is a `ValueError`."""

        if not self.is_live(node_id):
            raise ValueError(f"node id {node_id} is not live and cannot be released")
        self.length[node_id] = -1
        self.free_ids.append(node_id)

    def is_live(self, node_id: NodeId) -> bool:
        """Whether `node_id` is in range and currently allocated."""

        return 0 <= node_id < len(self.length) and self.length[node_id] >= 0

    def clear(self) -> None:
        """Reset to empty, in place: every storage list and the free list truncated."""

        self.network.clear()
        self.length.clear()
        self.hot_count.clear()
        self.child.clear()
        self.free_ids.clear()

    @property
    def live_count(self) -> int:
        """Ids allocated and not released."""

        return len(self.length) - len(self.free_ids)

    @property
    def capacity(self) -> int:
        """Slots in the slab; always `live_count + free_count`."""

        return len(self.length)

    @property
    def free_count(self) -> int:
        """Released slots awaiting reuse: `len(free_ids)`."""

        return len(self.free_ids)
