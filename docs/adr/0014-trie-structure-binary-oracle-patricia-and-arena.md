# ADR 0014 — The trie structure package: one trie per family, an unshared binary oracle, an arena-backed Patricia trie, and the invariants both satisfy

Status: accepted, amended 2026-09-23 (Amendment 1 — the arena's name, what makes
the accounting checks non-vacuous, structural versus derived observables, no
transient over-allocation, the derived node counts; Amendment 2 — the free list
is readable and the arena accounting is checked against it, a mutator may raise
`InvariantViolation` for corruption it cannot walk past, `iter_prefix_counts`
validates `min_length` eagerly; Amendment 2, correction — the reverse
free-list clause is kept for diagnosis rather than coverage, and decision 8
pins that the free-list clauses are checked before the `len(R)` comparisons)

Scope note: this ADR settles the interfaces epic #8 implements against —
`services/trie/src/hammertime/trie/structure/{__init__,node,binary_trie,patricia,arena,invariants}.py`
and the invariant helpers in `packages/hammertime-testkit` — covering §8-§12,
§27, §39's trie-update half, and the structural half of §46.5. It does **not**
design the trie service's worker (§28), read API (§29, ADR-0010), snapshot
(§32, §33), prefix metadata (§16, §17), the per-IP attribute side map (§46,
epic #9), or the `PrefixStatsChanged` publisher (ADR-0010 decision 3); it
states only what those need from the structure and leaves each decision to its
own epic. It amends no prior ADR. ADR-0005 (the trie stays binary; attributes
live beside it), ADR-0001 (one logical trie owner per address family) and
ADR-0011 (what the aggregator's recovery paths hand the trie) are taken as
given and are cited where they bind.

## Context

`services/trie/structure/` is six docstring-only stubs. The spec fixes a
surprising amount and leaves the rest open.

Fixed by the spec:

* §8: a binary trie whose paths are address bits; it MUST support inserting a
  hot IP, removing one, incrementing/decrementing ancestor hot counts, looking
  up prefix statistics, and determining the longest matching prefix.
* §9: a node is `child[0]`, `child[1]`, `hot_count`, `local_metadata`,
  `prefix_state`; stored state SHOULD be distinguishable from derived state.
* §10, §11, §39: insertion and removal walk the path and adjust `hot_count` by
  one at every visited node; after a removal `hot_count >= 0` MUST hold; empty
  nodes MAY be pruned; implementations SHOULD consider retaining structural
  nodes or an arena/slab allocator because oscillation causes allocation churn.
* §12: `hot_count(node)` is the number of currently HOT `/32` addresses in that
  subtree; `hot_count(/32) ∈ {0, 1}`; an internal node is the sum of its
  children. This matters more than any cached `prefix_state`.
* §27: a Patricia/radix representation, packed arrays, integer node ids and
  slab allocation are all recommended — and "the logical model MUST remain
  equivalent to the binary trie described above".
* §46.5 (via ADR-0005): `set(record.keys())` is the set of currently HOT
  addresses and `len(record) == hot_count(root)` per address family.
* ADR-0001: one single-writer trie owner per address family; §28: readers never
  observe a half-updated path.
* ADR-0011 Consequences (*Trie epic*), restating §11 and §46.5: a
  `HotIpRemoved` for an IP the trie does not hold MUST be a no-op that keeps
  `hot_count >= 0`, and a `HotIpAdded` for an IP it already holds replaces the
  attribute record. Both are what make the aggregator's persist-before-publish
  recovery safe.

Open, and blocking anyone who wants to write a test or a module:

1. **One trie per address family, or one trie for both?** §35 and
   `core/addressing` make the family explicit on every `Address`; ADR-0001
   assumes a writer per family but the structure's shape was never stated.
2. **What is the public interface?** The stubs name `add_hot_ip`,
   `remove_hot_ip`, "lookup" and "longest match" and nothing else. The read
   API (ADR-0010 decision 4) and the stats publisher (decision 3) need more
   than that, and a differential test needs a single surface both
   implementations answer on.
3. **What happens on a redundant add or remove?** §11 says `hot_count >= 0`
   MUST hold; ADR-0011 requires both to be no-ops. Whether they raise, return,
   or silently do nothing is unstated.
4. **Prune or retain?** §11 offers both and decides neither. "No orphaned
   nodes" is one of epic #8's acceptance criteria, which is only assertable
   once the policy is fixed.
5. **What exactly is the arena, and what does it promise?** §27 lists
   ingredients, not semantics. Epic #8's third acceptance criterion is
   "pruning without fragmentation blowing up memory", which needs a stated
   bound to test against.
6. **What does "equivalent to the binary trie" mean operationally?** A
   Patricia trie does not materialize a node for every prefix, so node-for-node
   comparison is meaningless and some other observable has to carry §27's
   equivalence requirement.
7. **What does `invariants.py` expose, and what does it raise?**
8. **How is §46.5's `len(records) == hot_count(root)` asserted here**, when the
   record map itself belongs to epic #9?

## Decision

### 1. One trie instance per address family; the family is fixed at construction

`BinaryTrie(family)` and `PatriciaTrie(family)` each hold exactly one address
family, and expose `family` and `bit_length` (32 or 128, from
`AddressFamily.bit_length`). Nothing in the package is family-agnostic at
runtime: an `Address` or `Prefix` of the other family passed to any method is a
`ValueError` naming both families, and changes nothing. "Any method" is every
method of `HotTrie` that takes one — `add_hot_ip`, `remove_hot_ip`, `contains`,
`hot_count`, `ancestor_counts` and `longest_matching_prefix` — not only the two
mutators; a query is as much a routing bug as an update. The family is checked
before any other argument, so `ancestor_counts(other_family_address,
min_length=99)` reports the family (Amendment 1, A9).

This follows ADR-0001 ("a single-writer process per address family"), the
`core/addressing` docstring ("IPv4 and IPv6 use the same type and separate trie
roots"), and §46.5's "per address family" phrasing — all three of which read the
family as a partition of the whole trie, not as a bit of every key. A single
mixed trie would also make `hot_count(/0)` meaningless (the two families share
no address space) and §46.5's invariant unstatable without a filter.

`ValueError`, not `InvalidAddressError`: reaching the structure with the wrong
family is a routing bug in the caller, not a malformed input from the wire.
`core.errors.InvalidAddressError`/`InvalidPrefixError` are what `Address.parse`
and `Prefix.__post_init__` raise for bad *text*, and `IpCounter` already uses
plain `ValueError` for arguments its caller should not have passed. What the
trie *service* does when an event for the other family arrives (drop, log,
crash) is the worker epic's call, not this package's.

### 2. The public interface both representations satisfy

`HotTrie` is a `typing.Protocol` declared in `structure/node.py`. Both classes
satisfy it structurally; neither inherits it. This is the contract epic #8's
tests are written against and the one later epics (query, snapshot, worker,
`tools/trie-inspect`) may rely on.

```python
# hammertime.trie.structure.node          Spec: §9, §12, §27
NodeId = int
NO_NODE: Final[NodeId] = -1


class PrefixCount(NamedTuple):
    """A prefix and the number of currently HOT addresses beneath it (§12)."""
    prefix: Prefix
    hot_count: int


class NodeView(NamedTuple):
    """One materialized node, as invariants and `trie-inspect` see it (§9)."""
    prefix: Prefix
    hot_count: int
    children: tuple[Prefix, ...]     # 0, 1 or 2 entries, ascending by branch bit


@dataclass(slots=True)
class TrieNode:
    """§9's conceptual node, as the reference implementation stores it."""
    hot_count: int = 0
    child: list[TrieNode | None] = field(default_factory=lambda: [None, None])


class HotTrie(Protocol):
    """§8's trie. Two representations, one observable behaviour (§27)."""

    @property
    def family(self) -> AddressFamily: ...
    @property
    def bit_length(self) -> int: ...
    @property
    def hot_ip_count(self) -> int: ...          # §37 hot_ip_count; == hot_count(/0)
    @property
    def node_count(self) -> int: ...            # §37 trie_nodes

    # Mutation — §10, §11, §39. Single writer (§28); not thread-safe.
    def add_hot_ip(self, address: Address) -> bool: ...
    def remove_hot_ip(self, address: Address) -> bool: ...
    def clear(self) -> None: ...

    # Queries — §8, §12, §29.
    def contains(self, address: Address) -> bool: ...
    def hot_count(self, prefix: Prefix) -> int: ...
    def ancestor_counts(
        self, address: Address, *, min_length: int = 0
    ) -> tuple[PrefixCount, ...]: ...
    def longest_matching_prefix(self, address: Address) -> Prefix | None: ...
    def iter_prefix_counts(self, *, min_length: int = 0) -> Iterator[PrefixCount]: ...
    def iter_hot_addresses(self) -> Iterator[Address]: ...
    def iter_nodes(self) -> Iterator[NodeView]: ...
```

Semantics, in the order a reader needs them:

* **`add_hot_ip` / `remove_hot_ip` return whether the hot set changed**
  (decision 3). `add` increments `hot_count` by one on every prefix of the
  address, `/0` through `/bit_length` inclusive, exactly as §10 and §39
  describe; `remove` decrements the same path and prunes (decision 4).
* **`contains(address)`** is `hot_count(address as a /bit_length prefix) == 1`.
  There is no separate membership set: §12's leaf rule *is* the membership
  test, so the count and the set cannot drift apart. (`contains` is therefore
  count-derived while `iter_hot_addresses()` is structural — in an intact trie
  a leaf exists exactly when its count is 1, and where the two disagree the
  trie is corrupt and `check_hot_counts` says so. Amendment 1, A3.)
* **`hot_count(prefix)`** is the number of currently HOT addresses under
  `prefix`, and is `0` for a prefix with no node — which is the trie's positive
  statement that nothing beneath it is hot (ADR-0010 decision 4: a zero-valued
  answer, never a 404).
* **`ancestor_counts(address, min_length=L)`** returns one `PrefixCount` for
  every length from `L` to `bit_length` **inclusive, including zero counts**,
  ordered by ascending length, in a single O(bit_length) walk. Zero counts are
  the point: after a removal empties a subtree the publisher still has to emit
  `PrefixStatsChanged` with `hot_count = 0` for prefixes whose nodes have just
  been pruned (ADR-0010 decision 3). With `min_length=8` on IPv4 it returns
  exactly the 25 entries that decision names. `L` outside `[0, bit_length]` is
  a `ValueError`.
* **`longest_matching_prefix(address)`** is the longest prefix of `address`
  whose `hot_count` is greater than zero, or `None` when the trie holds no hot
  address at all (§8's "longest matching prefix"). For a non-empty trie the
  answer is never `None`, because every address is under `/0`.
* **`iter_prefix_counts(min_length=L)`** yields **every prefix whose
  `hot_count` is greater than zero** and whose length is at least `L`, each
  exactly once, in pre-order depth-first order — a prefix before its
  descendants, branch `0` before branch `1`. For the binary trie this is its
  node set; for the Patricia trie it is the node set with every compressed edge
  expanded back into the prefixes it stands for. Decision 6 makes the two
  sequences identical, and that identity is what carries §27's "the logical
  model MUST remain equivalent to the binary trie". Descent is by child link:
  the stored count decides only whether a prefix is *yielded*, never whether
  the walk continues past it, so a node whose count has been corrupted to zero
  hides its own prefixes and nothing else (Amendment 1, A3). `L` outside
  `[0, bit_length]` is a `ValueError`, exactly as on `ancestor_counts`, and it
  is raised **when the method is called**, not when the returned iterator is
  first advanced — so neither implementation's `iter_prefix_counts` may itself
  be a generator function: it validates, then returns an iterator (Amendment 2,
  A13).
* **`iter_hot_addresses()`** yields the currently HOT addresses in ascending
  numeric order (the same DFS, at leaf level). It is what the snapshot epic
  serializes and what the invariants recompute from. It is **structural**: it
  yields the address of every materialized node at `length == bit_length`
  reachable from the root and never consults `hot_count` to decide what to
  yield or where to descend. That is what makes `check_hot_counts` a comparison
  of two independent sources rather than of the stored counts with themselves
  (Amendment 1, A3).
* **`iter_nodes()`** yields the *materialized* nodes in the same DFS order —
  the representation, not the logical model. The two implementations
  deliberately differ here, and only the invariant checks and `trie-inspect`
  should care. It is structural in the same sense: **every** node reachable
  from the root is yielded, whatever its stored count, and `NodeView.hot_count`
  reports that stored count verbatim — including `0` or a negative value. A
  live node whose count has been zeroed is therefore visible to
  `check_no_orphaned_nodes`, which is the only way that corruption can be
  caught at all (Amendment 1, A3).
* **Stored versus derived (§9's "stored state SHOULD be distinguishable from
  derived state").** The only stored state is the node record — `child[0]`,
  `child[1]`, `hot_count`, as `TrieNode` above and as the arena's parallel
  lists in decision 5 — plus the root reference. Everything else on `HotTrie`
  is derived from it. In particular `hot_ip_count` is **never** a maintained
  counter: it is `0` for an empty trie and otherwise the root node's stored
  `hot_count` (`arena.hot_count[root]` for the Patricia trie), read on each
  call, which is why a root count corrupted to `-1` surfaces as
  `hot_ip_count == -1`. `node_count` is *defined* as the number of materialized
  nodes reachable from the root, so `len(list(iter_nodes())) == node_count`
  always; unlike `hot_ip_count` it **may** be cached or maintained in O(1)
  (`arena.live_count` for the Patricia trie), because `check_no_orphaned_nodes`
  and `check_patricia` both cross-check it against an independent count of the
  reachable nodes and so keep a maintained value honest (Amendment 1, A2 and
  A3).
* **`clear()`** empties the trie: `hot_ip_count == 0`, `node_count == 0`, and
  the arena is reset in place (decision 5; the same `NodeArena` object is kept,
  so a reference held across `clear()` stays valid).

### 3. A redundant add or remove is a no-op that returns `False`, never an error

```text
add_hot_ip(ip)      already HOT  ->  False, nothing changes
remove_hot_ip(ip)   not HOT      ->  False, nothing changes
```

ADR-0011 decision 4 persists a transition before publishing it, so the trie is
*expected* to see a `HotIpAdded` for an IP it already holds and a
`HotIpRemoved` for one it does not; ADR-0001 Amendment 1 clause 5 lists both,
plus byte-identical redeliveries, as things the trie must absorb; ADR-0013
decision 4's dedup window shrinks the last case but does not remove it. An
exception would turn a normal recovery path into a crash loop, and a silent
`None` would leave the worker unable to tell an applied event from an absorbed
one.

**`False` does not mean "ignore the event".** §46.5's replace-on-add applies to
the attribute record whatever `add_hot_ip` returned: the record map is updated
unconditionally on `HotIpAdded` and deleted unconditionally on `HotIpRemoved`,
which is exactly what keeps `len(records) == hot_count(root)` true through a
redelivery. Whether a no-op event still emits `PrefixStatsChanged` remains the
worker epic's call (ADR-0011 Consequences says so explicitly); `False` is the
signal that lets it decide.

**"Never an error" is a statement about intact tries.** On a trie that already
satisfies §12, no add or remove — redundant or not — raises anything but
decision 1's family `ValueError`. A mutator that *discovers*, mid-walk, that
the invariant is already broken in a way it cannot proceed past without
corrupting the structure raises `InvariantViolation` instead, before mutating
anything (Amendment 2, A12). That is not a second kind of no-op event: it
cannot be produced by any sequence of `add_hot_ip`/`remove_hot_ip` calls, only
by a prior bug or by external writes to the storage, and the event that
triggered it is not at fault.

### 4. Pruning is mandatory in both implementations: the structure is a pure function of the current hot set

A node whose `hot_count` reaches zero is removed, in the binary trie and in the
Patricia trie alike, on the same removal that emptied it. The root is a
reference, not a permanent node: an empty trie has `node_count == 0`.
Consequently, for a given hot set, each implementation's node set is uniquely
determined — the same hot set reached by any order of adds and removes gives
byte-identical structure, counts and iteration order.

§11 permits retaining structural nodes and warns about allocation churn under
oscillation. The arena (decision 5) answers the churn worry directly: a pruned
node's slot is recycled through a free list, so a HOT→COLD→HOT cycle allocates
no Python object at all after the first pass. Retention would additionally:
make "no orphaned nodes" — epic #8's second acceptance criterion — unassertable,
because a zero-count node would be legal and an actually-leaked one
indistinguishable from it; bound memory by the *historical* hot set rather than
the current one, against §26's "the trie only needs currently hot IPs"; and
make `node_count` (§37's `trie_nodes`) a metric of history rather than of
state. Path-independence is also what lets the snapshot epic store only the hot
set and rebuild.

### 5. The arena: integer ids into parallel lists, a LIFO free list, capacity bounded by peak live nodes

`NodeArena` (`structure/arena.py`) is the Patricia trie's storage and only the
Patricia trie's (decision 7). `PatriciaTrie` exposes it as the public attribute
**`arena`**, constructed in `__init__` and never replaced (Amendment 1, A1);
together with `root` (decision 6) that is the whole of the Patricia trie's
public storage surface, and it is what `check_patricia`, `tools/trie-inspect`
and the structure's tests read and write.

```python
# hammertime.trie.structure.arena         Spec: §11, §27
class NodeArena:
    """Slab of Patricia node records addressed by integer NodeId (§27)."""

    # Parallel storage, indexed by NodeId. Public: this *is* the arena's
    # interface, and the Patricia trie reads and writes it directly.
    network: list[int]       # the node's prefix network, host bits zeroed
    length: list[int]        # the node's prefix length; -1 marks a free slot
    hot_count: list[int]     # §12's count for that node
    child: list[NodeId]      # two entries per node: child[2 * nid + bit]
    free_ids: list[NodeId]   # released slots, LIFO: the last is allocated next

    def allocate(self, *, network: int, length: int, hot_count: int = 0) -> NodeId: ...
    def release(self, node_id: NodeId) -> None: ...
    def is_live(self, node_id: NodeId) -> bool: ...
    def clear(self) -> None: ...

    @property
    def live_count(self) -> int: ...     # allocated and not released
    @property
    def capacity(self) -> int: ...       # slots in the slab == live_count + free_count
    @property
    def free_count(self) -> int: ...
```

What it promises:

1. **O(1) node access, structurally.** A `NodeId` is an index into a Python
   list; every field read or write is one indexing operation, independent of
   the number of nodes. Both children of a node are at `2 * nid` and
   `2 * nid + 1`, contiguously, which is §27's "packed arrays / integer node
   IDs / cache-friendly contiguous memory" as far as CPython lets one go. This
   is a structural claim, discharged by the representation, **not** by a timing
   test (see Assumptions).
2. **`allocate` reuses before it grows.** Released ids go on the LIFO free list
   `free_ids` — `free_count == len(free_ids)`, its last entry the next one
   handed out — and are handed back first; the slab grows only when that list is
   empty, i.e. only when `live_count == capacity`, and then by **exactly one
   node's worth of slots** — one entry appended to `network`, `length` and
   `hot_count`, two to `child` — so `capacity` rises by exactly 1 per growth
   and `capacity == len(network) == len(length) == len(hot_count) ==
   len(child) // 2` at all times. A fresh arena pre-allocates nothing
   (`capacity == 0`). Therefore, exactly:

   ```text
   capacity == the maximum number of simultaneously live nodes
               since construction or the last clear()
   ```

   That equality is epic #8's third acceptance criterion made testable: churn
   from HOT/COLD oscillation re-uses slots forever and never grows the slab,
   and the only thing that can grow it is genuinely holding more nodes at once
   than ever before. There is no compaction and the slab never shrinks except
   on `clear()`.

   **No operation allocates a node it does not keep** (Amendment 1, A4).
   `add_hot_ip` only allocates and `remove_hot_ip` only releases; neither does
   both, and neither takes a scratch node. The node sets before and after a
   single operation are therefore nested, and `live_count` never exceeds
   `max(live_count before, live_count after)` at any instant *within* an
   operation. That is what makes "the maximum number of simultaneously live
   nodes" a quantity a caller can measure: sampling `node_count` at operation
   boundaries and taking the running maximum gives the same number, exactly,
   and `arena.capacity` must equal it.
3. **A released slot is detectably dead, and the free list holds exactly the
   dead slots.** `release` writes `-1` into `length`, so `is_live` is one
   comparison, a double `release` is a `ValueError`, and the invariant checks
   can assert that every reachable child id is live. A released slot's other
   fields are stale by design and must not be read. `length` is therefore the
   *only* record of liveness, which is why `allocate` rejects a negative
   `length` with a `ValueError`: a slot that was allocated but reads as dead
   would make `is_live` meaningless. The free list is bookkeeping *over* that
   record rather than a second copy of it, and must agree with it exactly:

   ```text
   set(free_ids) == {i : 0 <= i < capacity and length[i] < 0}
   free_ids contains no id twice
   ```

   Neither clause is implied by `live_count == capacity - free_count`, which is
   an identity of this implementation and cannot fail however corrupt the arena
   is; these two are what make `check_patricia`'s arena accounting non-vacuous,
   and they are the reason `free_ids` is readable at all (Amendment 2, A11).
4. **`clear()` resets the slab to empty** — `capacity == 0`, `live_count == 0`,
   `free_count == 0`, every storage list truncated — so a trie reused after
   `clear()` re-establishes the bound above from scratch. It resets the
   existing `NodeArena` in place rather than constructing a new one.

### 6. The Patricia representation, stated exactly enough to be equivalent

Each node stores the prefix it represents (`network` with host bits zeroed,
`length`), its `hot_count`, and two child ids, in `PatriciaTrie.arena`
(decision 5). `PatriciaTrie.root` is a `NodeId` (or `NO_NODE`), so the topmost
node may sit at any length; there is no permanent `/0` node. Invariants
(decision 8) pin the shape: every node is either a leaf at
`length == bit_length` with no children, or an internal node with **exactly
two** children whose prefixes both strictly extend it and differ at bit
`length`; no node has exactly one child (that is the compression), and
`hot_count` is the sum of the children for an internal node and `1` for a leaf.

That shape fixes the size exactly (Amendment 1, A8): the leaves are in
bijection with the HOT addresses and every internal node is binary, so

```text
node_count == 2 * hot_ip_count - 1     for hot_ip_count >= 1
node_count == 0                        for an empty trie (root == NO_NODE)
```

One HOT address is therefore one node — §27's compressed edge, stated as a
number — and `iter_nodes()` yields a single `NodeView(prefix=/bit_length,
hot_count=1, children=())` for it.

Every query resolves against the *logical* prefix set, not the node set. Let
`c = common_prefix_length(node.network, target, bit_length)` at each step:

```text
walk(target_prefix P):
    nid = root
    while nid != NO_NODE:
        c = common_prefix_length(node[nid].network, P.network, bit_length)
        if c < min(node[nid].length, P.length):     # the paths diverge
            return 0
        if node[nid].length >= P.length:            # this node is under P
            return node[nid].hot_count
        nid = child[2 * nid + bit_at(P.network, node[nid].length, bit_length)]
    return 0
```

Reading it out: for a prefix `P` no shorter than the node's, the node's whole
subtree lies under `P`, so the node's count *is* `P`'s count — that is precisely
why an edge compressed over lengths `pl+1 .. nl` reports the same count for
every length in that range, and why expanding an edge yields the binary trie's
counts exactly. `ancestor_counts` and `iter_prefix_counts` are the same walk
with the intermediate lengths filled in: lengths `pl+1 .. min(nl, c)` take the
child's count, and every length past a divergence takes `0`.
`common_prefix_length` and `bit_at` are `hammertime.core.addressing.bits`, which
exist for this (their docstring already says "shared by trie traversal and
Patricia edge compression").

### 7. The oracle shares no traversal code with the production path

`binary_trie.py` is the bit-by-bit reference of §10/§11/§39: plain `TrieNode`
objects, one node per bit level, a child reference per branch. It does **not**
use `NodeArena`, and no counting, walking or pruning logic is shared between it
and `patricia.py`. Unlike `PatriciaTrie.root` / `PatriciaTrie.arena`, its
storage is **not** part of the contract: no attribute name is promised, nothing
outside `binary_trie.py` may reach into its nodes, and everything anyone is
entitled to know about it is on the `HotTrie` surface (Amendment 1, A5). Its
node set *is* its positive-count prefix set, so `node_count ==
len(list(iter_prefix_counts()))` and the `NodeView.prefix` sequence of
`iter_nodes()` equals the `PrefixCount.prefix` sequence of
`iter_prefix_counts()`, element for element — 33 nodes for one HOT IPv4
address, 129 for one IPv6 (Amendment 1, A8). The only code both import is `hammertime.core.addressing`
and the type declarations in `node.py`, neither of which contains a traversal.

A differential test is worth exactly as much as the independence of its oracle.
If both sides allocated from the same arena or walked through a shared helper,
the bug classes most likely to hurt here — an off-by-one on the compressed
edge, a double decrement, a slot recycled while still referenced — would
corrupt both answers identically and the test would agree on the wrong result.
The cost is one duplicated traversal in a module whose whole purpose is to be
obviously correct.

### 8. `invariants.py` exposes named checks that raise `InvariantViolation`

```python
# hammertime.trie.structure.invariants    Spec: §11, §12, §46.5
def check_trie(trie: HotTrie) -> None: ...
def check_hot_counts(trie: HotTrie) -> None: ...
def check_no_orphaned_nodes(trie: HotTrie) -> None: ...
def check_attribute_records(trie: HotTrie, records: Collection[Address]) -> None: ...
def check_patricia(trie: PatriciaTrie) -> None: ...
```

Every one of them returns `None` or raises
`hammertime.core.errors.InvariantViolation` (which already exists, and whose
docstring already names §12) with a message that states the spec section, the
prefix or node id at fault, and the two values that disagree. They are
side-effect free and never repair anything.

* **`check_hot_counts`** recomputes §12 bottom-up from
  `iter_hot_addresses()` — for each hot address, increment every one of its
  `bit_length + 1` prefixes — and requires the resulting mapping to equal
  `dict(iter_prefix_counts())` exactly, in both directions. That single
  comparison covers the whole of §12: a wrong count, a missing prefix, a prefix
  that should not exist, and (because every count in the recomputed mapping is
  at least 1) `hot_count >= 0`. It also requires `hot_ip_count` to equal the
  number of distinct hot addresses, and `iter_hot_addresses()` to yield no
  address twice. This is `tools/trie-inspect --verify`'s "recomputes hot_count
  bottom-up".
* **`check_no_orphaned_nodes`** requires every count yielded by
  `iter_prefix_counts()` to be greater than zero (decision 4: a zero-count node
  is an orphan by construction) and every node yielded by `iter_nodes()` to
  have a positive count, and requires `len(list(iter_nodes())) == node_count`.
* **`check_patricia`** adds the representation checks of decision 6 — exactly
  two children or none, children strictly extending and branching correctly,
  leaves at `bit_length`, sum-of-children — plus the arena accounting. It
  computes the reachable node set **itself**, by walking `arena.child` from
  `PatriciaTrie.root`, and never through `iter_nodes()`, `node_count` or
  `live_count`; a check that took the implementation's own answer for the
  quantity it is checking would pass by construction (Amendment 1, A2). Writing
  `R` for the ids that walk reaches, it requires:

  ```text
  root == NO_NODE  <->  R is empty  <->  live_count == 0  <->  node_count == 0
  every id in R is in range [0, capacity) and arena.is_live(id)
  no id is reached twice                    (no cycle, no shared subtree)
  len(R) == node_count                      (iter_nodes / the metric agree with reachability)
  len(R) == arena.live_count                (no live slot is unreachable: a leak)
  capacity == live_count + free_count       (an identity of decision 5's arena; kept
                                             for an arena that maintains counters)
  set(arena.free_ids) == the dead slots     (i.e. == {i : arena.length[i] < 0}, both
  no id in arena.free_ids appears twice      directions; Amendment 2, A11)
  len(R) == 2 * leaves - 1                  (non-empty trie; leaves counted by the walk)
  leaves == hot_ip_count                    (non-empty trie)
  ```

  An id that is out of range, not live, or reached twice is an
  `InvariantViolation` — never an `IndexError`, a `ValueError` or a hang: the
  walk tests liveness before reading any field and carries a visited set. The
  same holds for the free list: an entry outside `[0, capacity)` is reported,
  not indexed with. The two free-list clauses are what turn `len(R) ==
  live_count` into a real statement — without them a live slot wrongly on the
  free list and a live slot wrongly unreachable cancel out and every accounting
  equality still holds (Amendment 2, A11).

  The block is a set of requirements, not an evaluation order, with one
  exception: **the free-list clauses are discharged before `len(R) ==
  node_count` and `len(R) == arena.live_count`** (Amendment 2, correction). A
  dead slot missing from `free_ids` fails those two count clauses as well, so
  in the other order the failure is reported as a count mismatch and the leaked
  slot is never named; in this one the message is the one that identifies it.
* **`check_trie`** runs `check_patricia` **first** when handed a
  `PatriciaTrie`, then `check_hot_counts` and `check_no_orphaned_nodes`. The
  order matters: the logical checks traverse the structure, and their behaviour
  on a trie whose *representation* is broken — a released id still linked in, a
  cycle — is undefined, so the representation check goes first and reports it
  as an `InvariantViolation` (Amendment 1, A2). Corruption of a *count* alone
  leaves every traversal well defined, and every check remains meaningful on
  it. `check_trie` is the one entry point a debug build or the inspect tool
  calls.

Cost is O(hot_ip_count × bit_length), plus O(capacity) for `check_patricia`'s
scan of `arena.length` (capacity is decision 5's peak live node count, so that
term is bounded by the hot set too). That is why the module docstring's "cheap
enough for debug builds" is honest for a trie whose size §26 bounds by the hot
set, and why nothing on the hot path calls it unconditionally.

### 9. §46.5's derived invariant is expressed as a `Collection[Address]`, so this epic never touches the record map

```python
def check_attribute_records(trie: HotTrie, records: Collection[Address]) -> None
```

asserts, for one address family:

```text
every address in records has family == trie.family     §46.5 "per address family"
len(records) == trie.hot_ip_count                      §46.5, ADR-0005 decision 4
set(records) == set(trie.iter_hot_addresses())         §46.5's first line
```

in that order. The family clause is deliberately first and is a **diagnostic**
refinement, not an independently falsifiable one: an `Address` carries its
family as part of its identity, so a record of the wrong family can never equal
a HOT address of this trie and always breaks the set comparison too. Checking
it first is what makes the message say "these records belong to the other
family" instead of "these records are missing and those are extra"
(Amendment 1, A7).

`Collection[Address]` is the entire coupling. A `Mapping[Address, IpAttributes]`
— which is what epic #9 will actually hold — *is* a `Collection[Address]`: it
is sized, iterable over its keys, and supports `in`. So the check accepts the
real side map with no import, no knowledge of the attribute document, and no
dependency in either direction; a plain `set[Address]` works equally well in a
test. Epic #9 owns the map, its schema validation, and its lifecycle; this epic
owns the one arithmetic statement §46.5 makes about it. When one process holds
both families, the caller passes that family's subset — the check's family
assertion is what catches getting that wrong.

### 10. `hammertime-testkit` gets test-facing assertions that recompute independently

`packages/hammertime-testkit/src/hammertime/testkit/invariants.py` keeps its
three existing names and gains one:

```python
class TrieView(Protocol):                       # structural; no trie import
    @property
    def family(self) -> AddressFamily: ...
    @property
    def hot_ip_count(self) -> int: ...
    @property
    def node_count(self) -> int: ...
    def iter_hot_addresses(self) -> Iterator[Address]: ...
    def iter_prefix_counts(self) -> Iterator[tuple[Prefix, int]]: ...


def assert_hot_count_consistent(trie: TrieView) -> None: ...
def assert_no_negative_counts(trie: TrieView) -> None: ...
def assert_no_orphaned_nodes(trie: TrieView) -> None: ...
def assert_attribute_records_match(trie: TrieView, records: Collection[Address]) -> None: ...
def assert_hysteresis_holds(history: object) -> None: ...     # unchanged; not this epic
```

Two rules make this work:

1. **testkit does not depend on a service.** `hammertime-testkit`'s
   dependencies are `hammertime-core` and hypothesis, and they stay that way:
   `TrieView` is a structural `Protocol`, so `BinaryTrie` and `PatriciaTrie`
   satisfy it without importing anything from either side. The direction is
   load-bearing in two ways. It keeps a shared, dev-only package from dragging
   one service into the dependency graph of every consumer of testkit — the
   detector's and the aggregator's tests will use these helpers too. And the
   trie service's own tests live inside `hammertime-trie` and import testkit,
   so declaring the reverse edge would make the two workspace members
   mutually dependent in practice, even though today's metadata reaches
   testkit through the root dev group rather than through
   `services/trie/pyproject.toml`. `PrefixCount` being a `NamedTuple` is what
   lets `TrieView` spell `Iterator[tuple[Prefix, int]]` and stay honest.
2. **The testkit assertions recompute; they never call `invariants.py`.** A
   test that checked the trie by calling the trie package's own checker would
   pass whenever the checker was wrong in the same direction as the structure.
   This is decision 7's argument applied to the checker: the duplication is the
   independence. The two differ in audience and failure mode as well — the
   service's `check_*` raise `InvariantViolation` for a debug build and
   `trie-inspect`; testkit's `assert_*` raise `AssertionError` with a
   pytest-readable message.

### 11. Module layout

```text
services/trie/src/hammertime/trie/structure/
  __init__.py        re-exports: HotTrie, TrieNode, NodeView, PrefixCount, NodeId, NO_NODE,
                     BinaryTrie, PatriciaTrie, NodeArena, and the invariants checks
                     (§8, §9, §27)
  node.py            NodeId, NO_NODE, PrefixCount, NodeView, TrieNode, HotTrie   (§9, §12, §27)
  binary_trie.py     BinaryTrie — the reference; object per node, no arena       (§10, §11, §39)
  patricia.py        PatriciaTrie — production; arena-backed, path-compressed    (§27, §39)
  arena.py           NodeArena                                                   (§11, §27)
  invariants.py      check_trie, check_hot_counts, check_no_orphaned_nodes,
                     check_attribute_records, check_patricia                     (§11, §12, §46.5)
```

Imports run one way: `node.py` imports only `hammertime.core`; `binary_trie.py`
and `patricia.py` import `node.py` (and `patricia.py` also `arena.py`);
`invariants.py` imports all of them; `__init__.py` imports everything and is
imported by none of them, so there is no cycle. Every module keeps its existing
`Spec:` docstring line, extended where this ADR adds a section, and cites
ADR-0014.

Tests (epic #8, written from this ADR and the spec, never from the modules):
`services/trie/src/hammertime/trie/tests/test_invariants.py`,
`services/trie/src/hammertime/trie/tests/test_patricia_equivalence.py`,
`tests/property/test_trie_properties.py`.

## Assumptions

Each of these is a judgment call that the epic, the spec and the prior ADRs do
not dictate. Push back on them individually. Amendment 1 adds ten more, stated
inside the item each informs (A1-A10) rather than appended here, so that a
reader sees the assumption next to the ruling it is an assumption of.

1. **`HotTrie` lives in `node.py` rather than a new `structure/interface.py`.**
   `hammertime-store` and `hammertime-bus` both put their Protocols in an
   `interface.py`, which would be the repo convention here; epic #8 fixes the
   package at six named modules, and inventing a seventh silently is the worse
   of the two deviations. `node.py` is where the package's type vocabulary
   already lives. Renaming it later is mechanical.
2. **`HotTrie` is not `@runtime_checkable`.** The store's Protocols are, but
   they are method-only; `HotTrie` has property members, and nothing in this
   epic needs `isinstance`. mypy checks conformance at every use site, which is
   stronger than an attribute-presence check. (I could not consult the typing
   specification or PEP 544 directly — `peps.python.org`, `typing.python.org`,
   `docs.python.org` and `mypy.readthedocs.io` are all blocked by this
   environment's egress proxy, and the only summary I could reach was a web
   search result, i.e. secondhand. I therefore avoided depending on any subtle
   runtime-protocol semantics and relied on the in-repo precedent —
   `packages/hammertime-store/src/hammertime/store/interface.py` — which the
   existing test suite exercises.)
3. **Method names.** `add_hot_ip`/`remove_hot_ip` are §39's, verbatim.
   `hot_ip_count` and `node_count` are §37's metric names (`hot_ip_count`,
   `trie_nodes`) so the metrics epic has nothing to translate. `hot_count`,
   `contains`, `ancestor_counts`, `longest_matching_prefix`,
   `iter_prefix_counts`, `iter_hot_addresses`, `iter_nodes` and `clear` are
   mine.
4. **`bool` returns rather than a richer result object.** A named result
   (`applied`, `pruned_nodes`, the ancestor counts) would save the worker one
   O(bit_length) walk for the stats it publishes. Rejected: it welds the
   publisher's concern (ADR-0010 decision 3) into the structure's API, and the
   second walk is 32 steps against an operation §7's hysteresis already makes
   rare. If profiling ever says otherwise, adding a combined method is
   additive.
5. **`ancestor_counts` returns a tuple, the iterators return iterators.** The
   ancestors are a bounded 33 (or 129) entries a caller will index and re-read;
   the iterations are unbounded in the hot-set size and should not materialize.
6. **Pre-order DFS, branch 0 before branch 1, as the iteration order.** Any
   deterministic order would do for equivalence; this one makes the Patricia
   expansion land exactly where the binary trie's nodes do, sorts ascending by
   address, and puts a prefix before its descendants — which is the order
   `GET /prefixes/hot?minimal=true` (§31) wants to scan.
7. **`longest_matching_prefix` returns `None` on an empty trie** rather than
   `/0`. `/0` would be a prefix with `hot_count == 0` presented as a match,
   which contradicts the method's own definition.
8. **`iter_prefix_counts` yields logical prefixes, not nodes.** This is the
   operational reading of §27's equivalence requirement, and it is what makes
   `GET /prefixes/hot` answerable: a `/24` with 156 hot IPs has no Patricia
   node of its own when those IPs branch below it, but it is exactly the prefix
   an operator is asking about.
9. **A zero-count prefix is never yielded** — including `/0` on an empty trie.
   Consumers that want "this prefix is not hot" ask `hot_count(prefix)`.
10. **Pruning is mandatory rather than merely permitted (§11 says MAY).**
    Decision 4 gives the reasoning. The retaining alternative is the one §11
    names first, so this is a real choice against a spec-offered option, taken
    because the acceptance criteria and §26 both point the other way and
    because the arena removes its motivation.
11. **`clear()` exists at all.** Nothing in the spec asks for it; the snapshot
    epic will want to reset before a load, and the property tests want a cheap
    reset. It is three lines.
12. **Parallel lists, not an array module or a struct-of-bytes.** `array('q')`
    or a `bytearray` would be denser and would bound integer width — which is
    wrong for IPv6, where a `network` is a 128-bit Python int. Lists of ints
    keep the arbitrary precision `Address`/`Prefix` already rely on, at the cost
    of pointer indirection CPython would impose on any object-per-node design
    anyway.
13. **The arena's storage lists are public.** Encapsulating them behind
    accessor methods would add a Python call per field access to the very thing
    that exists to be O(1), and the invariant checks and `trie-inspect` both
    need to read them. Documented as the arena's interface, which is what §27's
    "packed arrays, integer node IDs" implies.
14. **LIFO free list.** FIFO would spread reuse across the slab and cost a
    `deque`; LIFO reuses the most recently freed slot, which is the one most
    likely to still be in cache. The `capacity == peak live` bound holds for
    either.
15. **No compaction, and the slab never shrinks.** A trie whose hot set
    collapses from a million to a thousand keeps a million slots until
    `clear()`. Compaction would have to rewrite every stored `NodeId`, and §26
    already bounds the trie by the hot set. Named here as a real limitation:
    if a deployment sees a large, permanent drop, this is the thing to revisit.
16. **`capacity == peak live nodes` is stated as an exact equality**, not a
    bound. It follows from "grow only when the free list is empty" and makes a
    sharper test; if a future implementation pre-allocates a slab, this becomes
    `<=` and the test changes with it.

    > Amended 2026-09-23: "peak live" left open whether the peak is taken over
    > every instant or over operation boundaries. Amendment 1 A4 closes it —
    > no operation allocates a node it does not keep, so the two readings
    > coincide and a caller can measure the peak between operations.
17. **O(1) node access is discharged structurally, not by a timing test.** A
    wall-clock assertion on a garbage-collected interpreter in CI is a flake
    generator. `tests/bench/test_throughput.py` (§40) is where numbers belong,
    and it is not in this epic.
18. **`ValueError` for a family mismatch, an out-of-range `min_length`, and a
    double `release`; no new error types.** `InvariantViolation` is reserved for
    a broken structural invariant, i.e. for `invariants.py`.

    > Amended 2026-09-23: "i.e. for `invariants.py`" was too strong. Amendment 2
    > A12 also allows a *mutator* to raise `InvariantViolation` when it detects
    > a broken invariant it cannot walk past, which is still "a broken
    > structural invariant" and is never raised for bad arguments. A11 adds a
    > third `ValueError`: `NodeArena.allocate` with a negative `length`. The
    > `min_length` clause now covers `iter_prefix_counts` as well as
    > `ancestor_counts` (A13).
19. **The structure is not thread-safe and does not try to be.** §28's
    single-writer model (ADR-0001) is the whole mechanism; how readers get a
    consistent view — a versioned snapshot pointer, per the `worker.py` stub's
    own docstring — is the worker epic's decision, and nothing here forecloses
    it.
20. **`TrieNode` keeps `child` as a two-element list** (§9's `child[0]` /
    `child[1]`) rather than two attributes, so the reference implementation
    reads like §39's pseudocode.
21. **The node record carries `child[0]`, `child[1]` and `hot_count` only.**
    §9 also lists `local_metadata` and `prefix_state`. Neither can be a node
    slot in the production representation: a Patricia node does not exist for
    every prefix, so metadata declared on a prefix that is compressed away
    would have nowhere to live, and §12 calls `prefix_state` a cache derivable
    from `hot_count`, `prefix_length` and configuration. ADR-0005 made the same
    argument for per-IP attributes and put them in a side map. I therefore
    leave both **outside** the structure package and take no position on where
    they go — that is the metadata epic's and the query epic's call
    respectively, and the constraint above is this ADR's only input to it.
22. **Node identity is not part of the contract.** Nothing outside the package
    may hold a `NodeId` across a mutation: pruning recycles ids. `NodeView` and
    `PrefixCount` are values, which is why the checks and the tool take those.
    Amendment 1 A1 names `PatriciaTrie.arena` and so makes ids *readable* from
    outside; it does not make them stable. Read `root`, walk, and use the
    result before the next `add_hot_ip` or `remove_hot_ip`.
23. **The family check is per call, not per event batch.** One comparison
    against a `StrEnum` per operation is noise against an O(32) walk.
24. **Both implementations ship, permanently.** The oracle is not scaffolding
    to be deleted once Patricia works: it is what `test_patricia_equivalence.py`
    keeps comparing against, and §8 explicitly allows either representation.
25. **No `CHANGES` entry for this epic.** The package is internal structure
    with no user-visible behaviour until the trie service consumes it; per
    `CLAUDE.md`'s "if you are unsure, it does not", the entry belongs to the
    change that makes the trie service actually do something. Recorded here so
    the implementing change does not have to re-derive it.
26. **Testkit's `assert_hysteresis_holds` is untouched.** It is §38's IP-state
    invariant, not a trie-structure one, and its `history` argument is
    undesigned; leaving it a stub is deliberate, not an oversight.
27. **`generators.py` gains only what these tests need.** It is a shared
    module with future consumers (§34 configs, observation streams); this epic
    adds address/prefix/operation strategies and nothing speculative.

## Consequences

* Epic #8 becomes implementable against a fixed surface: six modules, one
  Protocol, five invariant checks, four testkit assertions.
* The three acceptance criteria map to statements a test can assert without
  reading the implementation: (1) `list(patricia.iter_prefix_counts()) ==
  list(binary.iter_prefix_counts())` plus agreement on every point query and on
  every `add`/`remove` return value; (2) `check_trie` after every step of a
  randomized sequence, with "no orphaned nodes" meaning "no yielded count is
  zero" (decision 4); (3) `arena.capacity == peak live node count` — the
  running maximum of `node_count` sampled between operations, which Amendment 1
  A4 makes exact — and `node_count == 0` after removing everything.
* **The snapshot epic (§33) can store the hot set alone** and rebuild the
  structure by replaying adds, because decision 4 makes the structure a pure
  function of that set. It still needs the attribute records (§46.8) and the
  `replay_position` (ADR-0010 Amendment 1); neither is here.
* **The worker epic** gets `ancestor_counts(ip, min_length=...)` for ADR-0010
  decision 3's 25 messages, and `add_hot_ip`/`remove_hot_ip` returning whether
  anything changed so it can decide what a no-op event emits — the question
  ADR-0011 Consequences left it.
* **The query epic** gets `hot_count(prefix)` (zero for an absent node, as
  `read-api-v1.md` requires), `ancestor_counts` for `matched_prefixes`, and
  `iter_prefix_counts(min_length=...)` for `GET /prefixes/hot`. It still owns
  `evaluate_prefix_state` (ADR-0010 decision 1, `core/state/prefix.py`, which
  does not exist yet) and any caching of `prefix_state`.
* **Epic #9** gets the §46.5 arithmetic asserted for it, against whatever
  `Collection[Address]` it ends up holding, and keeps every other attribute
  concern.
* `tools/trie-inspect` (`--verify`, §12, §33) has its verification entry point:
  `check_trie`. It already depends on `hammertime-trie`.
* `hammertime-testkit` acquires its first real content and no new dependency.
* No schema changes; no wire-format changes; no `CHANGES` entry (assumption
  25).
* Spec pointer notes added by this ADR: §9 (what the structure's node record
  holds), §11 (pruning is mandatory; the arena), §12 (where the invariant is
  checked), §27 (the two representations and what makes them equivalent),
  §46.5 (where the derived invariant is asserted). `docs/spec/README.md`'s
  index is updated for §8-12, §27, §39 and §46.
* **Open, and deliberately not settled here:** whether the trie's
  `event_sequence` and the log's stream sequence should be one number
  (ADR-0010 Amendment 1, assumption "Two numbers rather than one") — it touches
  the read protocol, not the structure; and what the worker does with an event
  for the family it does not serve (decision 1).

## Amendment 1 (2026-09-23) — the arena's name, what makes the accounting checks non-vacuous, structural versus derived observables, no transient over-allocation, and the derived node counts

Why: the `test-author` writing epic #8's tests
(`services/trie/src/hammertime/trie/tests/test_invariants.py`,
`test_patricia_equivalence.py`, `tests/property/test_trie_properties.py`, and
the testkit content in
`packages/hammertime-testkit/src/hammertime/testkit/{invariants,generators}.py`)
worked from decisions 1-10 and surfaced ten places where the text either did
not decide a case, decided it only by implication, or left a check that could
be satisfied vacuously. Five of them (A1, A2, A3, A4, A8) determine what
`coder` must implement and are ruled below; the other five are recorded with
the classification they deserve so that the next reader does not have to
re-derive them. Each item says whether the point was (a) already determined by
this ADR as written, (b) genuinely unspecified and ruled now, or (c)
deliberately left open, and whether it changes any shipped code. **None
changes shipped code**: `services/trie/src/hammertime/trie/structure/` is still
six docstring-only stubs (grepped for a module-level `class` or `def`: none),
so every ruling binds the epic #8 `coder` brief rather than correcting code.
The test files named above are consistent with every ruling; where a ruling
goes beyond what they assert, the gap is listed under *Follow-ups*.

As with the amendments to ADR-0011 and ADR-0013, decision bodies were rewritten
in place so that a reader sees the rule now in force. Every edit outside this
section, with the superseded wording quoted:

* **Status line.** Was "Status: accepted". Now names this amendment.
* **Decision 1, first paragraph, closing sentence.** Was: "an `Address` or
  `Prefix` of the other family passed to any method is a `ValueError` naming
  both families, and changes nothing." Now adds the explicit list of methods
  that covers, and that the family is checked before any other argument (A9).
* **Decision 2, `contains` bullet.** Gains a parenthesis: `contains` is
  count-derived and `iter_hot_addresses()` structural, and where the two
  disagree the trie is corrupt (A3).
* **Decision 2, `iter_prefix_counts` bullet.** Gains a closing sentence:
  descent is by child link and the stored count decides only whether a prefix
  is yielded (A3).
* **Decision 2, `iter_hot_addresses` bullet.** Gains: it is structural — every
  materialized node at `length == bit_length`, `hot_count` never consulted —
  and why that is what makes `check_hot_counts` a comparison of two
  independent sources (A3).
* **Decision 2, `iter_nodes` bullet.** Was: "yields the *materialized* nodes in
  the same DFS order — the representation, not the logical model. The two
  implementations deliberately differ here, and only the invariant checks and
  `trie-inspect` should care." Now adds that *every* reachable node is yielded
  whatever its stored count and that `NodeView.hot_count` reports that count
  verbatim (A3).
* **Decision 2, new bullet "Stored versus derived (§9's …)"**, before the
  `clear()` bullet: what is stored, that `hot_ip_count` and `node_count` are
  derived and never separately maintained counters (A2, A3).
* **Decision 2, `clear()` bullet.** Was: "`clear()` empties the trie:
  `hot_ip_count == 0`, `node_count == 0`, and the arena is reset (decision 5)."
  Now says the arena is reset *in place* and a held reference stays valid (A1).
* **Decision 5, opening sentence.** Was: "`NodeArena` (`structure/arena.py`) is
  the Patricia trie's storage and only the Patricia trie's (decision 7)." Now
  also names the attribute `PatriciaTrie.arena` and says what reads it (A1).
* **Decision 5, promise 2.** Was: "Released ids go on a LIFO free list and are
  handed back first; the slab grows only when the free list is empty, i.e. only
  when `live_count == capacity`. Therefore, exactly:" Now adds that a growth
  appends exactly one node's worth of slots, the storage-list length identity,
  that a fresh arena pre-allocates nothing, and a closing paragraph "**No
  operation allocates a node it does not keep**" (A4).
* **Decision 5, promise 4.** Was: "**`clear()` resets the slab to empty** —
  `capacity == 0` — so a trie reused after `clear()` re-establishes the bound
  above from scratch." Now also gives `live_count`, `free_count` and the list
  truncation, and says the reset is in place (A1, A4).
* **Decision 6, first paragraph and a new block after it.** Was: "Each node
  stores the prefix it represents (`network` with host bits zeroed, `length`),
  its `hot_count`, and two child ids." Now says where they are stored
  (`PatriciaTrie.arena`) and adds the exact node count `2 * hot_ip_count - 1`
  (or `0`) with the single-address `NodeView` (A1, A8).
* **Decision 7, first paragraph.** Gains: the binary trie's storage is *not*
  part of the contract and no attribute name is promised, plus its own derived
  counts — `node_count == len(list(iter_prefix_counts()))`, the
  element-for-element prefix identity between `iter_nodes` and
  `iter_prefix_counts`, 33 nodes for one HOT IPv4 address and 129 for one IPv6
  (A5, A8).
* **Decision 8, `check_no_orphaned_nodes` bullet.** Was: "requires every count
  yielded by `iter_prefix_counts()` to be greater than zero (decision 4: a
  zero-count node is an orphan by construction) and every node yielded by
  `iter_nodes()` to have a positive count." Now also requires
  `len(list(iter_nodes())) == node_count` (A8).
* **Decision 8, `check_patricia` bullet.** Was: "plus the arena accounting:
  `live_count == node_count`, `capacity == live_count + free_count`, every
  reachable child id live, no id reachable twice." Now requires the check to
  compute the reachable set itself from `root` and `arena.child`, lists the
  equalities it discharges against it, and requires `InvariantViolation` rather
  than `IndexError`, `ValueError` or a hang for a bad id (A2, A8).
* **Decision 8, `check_trie` bullet.** Was: "runs `check_hot_counts` and
  `check_no_orphaned_nodes`, and additionally `check_patricia` when handed a
  `PatriciaTrie`." Now runs `check_patricia` **first**, with the reason (A2).
* **Decision 9, the three-line assertion block.** The family clause was listed
  third; it is now first, followed by a paragraph saying it is a diagnostic
  refinement and cannot be violated on its own (A7).
* **Assumptions, preamble** — a pointer to A1-A10. **Assumption 16** — a dated
  blockquote pointing to A4. **Assumption 22** — two sentences saying
  Amendment 1 makes node ids readable but not stable (A1).
* **Consequences, acceptance-criteria bullet, clause (3).** Was:
  "`arena.capacity == peak live node count` and `node_count == 0` after
  removing everything." Now names the measurement that makes "peak" well
  defined (A4).

### A1. The Patricia trie's arena is the public attribute `PatriciaTrie.arena`

**Classification: (b), genuinely unspecified.** Decision 5 made the arena's
storage lists public and said the Patricia trie owns them, and decision 6 named
`PatriciaTrie.root`, but nothing named the arena itself. Every corruption and
capacity test needs a name, and so does `tools/trie-inspect`.

Ruling: `PatriciaTrie.arena` is a public attribute of type `NodeArena`,
constructed in `__init__` and never rebound. With `root` it is the whole of the
Patricia trie's public storage surface; `BinaryTrie` has no counterpart (A5).
`clear()` resets that same arena in place rather than constructing a new one,
so a reference taken before a `clear()` is still the trie's arena after it.

Assumptions:

* **`arena`, not `_arena` or `nodes`.** Decision 5 already argued the storage
  is public because the invariant checks and the tool must read it; a private
  name would contradict that and force every reader through a convention. The
  name matches the module (`arena.py`) and the type (`NodeArena`).
* **Never rebound, and `clear()` resets in place.** Nothing required this — the
  alternative (allocate a fresh `NodeArena` on `clear()`) is equally correct
  for a caller that re-reads `trie.arena`. I chose stability because a
  debugging session or an inspect tool that holds the arena across a `clear()`
  otherwise silently watches a dead object, and because truncating four lists
  is no more work than allocating them.
* **Writable, not read-only.** The tests inject corruption by writing
  `arena.hot_count[...]` and calling `arena.allocate`/`release` directly. That
  is the intended way to test a check that exists to catch corruption; there is
  no other way to produce a corrupted trie through the public API. It follows
  from decision 5's "this *is* the arena's interface" but had never been said
  of a *writer* outside the package.

### A2. `check_patricia` computes reachability itself; `node_count` is defined by reachability

**Classification: (b).** Decision 8 required `live_count == node_count` without
saying where `node_count` comes from. If an implementation defines
`PatriciaTrie.node_count` as `arena.live_count` — the obvious O(1) choice — the
check compares a value with itself and passes on a trie with an arbitrary
number of leaked live slots. Epic #8's third acceptance criterion depends on
exactly that accounting.

Ruling, in two parts.

1. **`node_count` is defined as the number of materialized nodes reachable from
   the root**, for both implementations, and therefore
   `len(list(iter_nodes())) == node_count` identically. An implementation may
   still compute it in O(1) — the binary trie by maintaining a counter, the
   Patricia trie by returning `arena.live_count` — but only because those
   values *equal* the reachable count in an intact trie; the definition is
   reachability, and where the two disagree the implementation is wrong. That
   permission is safe only because two checks now cross-examine it:
   `check_no_orphaned_nodes` requires `len(list(iter_nodes())) == node_count`
   for any trie, and `check_patricia` requires both to equal the size of its
   own reachability walk. `hot_ip_count` gets no such permission (A3) — nothing
   independently recomputes a hot-address counter except `check_hot_counts`,
   which is a debug-build check rather than an always-on one.
2. **`check_patricia` does its own walk.** It starts at `PatriciaTrie.root`,
   follows `arena.child`, and reaches the set `R` without calling
   `iter_nodes()`, reading `node_count` or reading `live_count` for the
   traversal. It then discharges, against `R`:

   ```text
   root == NO_NODE  <->  R empty  <->  live_count == 0  <->  node_count == 0
   every id in R in range [0, capacity) and arena.is_live(id)
   no id reached twice
   len(R) == node_count
   len(R) == arena.live_count
   capacity == arena.live_count + arena.free_count
   len(R) == 2 * leaves - 1  and  leaves == hot_ip_count   (non-empty trie)
   ```

   `len(R) == arena.live_count` is the clause that catches a live slot nobody
   links to, which is what `arena.allocate(...)` on an otherwise healthy trie
   produces. A reachable id that is out of range, released, or already visited
   raises `InvariantViolation`; the walk tests `is_live` before reading any
   field and carries a visited set, so a corrupted arena is a diagnosed failure
   and never an `IndexError`, a stale-field misreading or a non-terminating
   walk.

   > Amended 2026-09-23: the `capacity == arena.live_count + arena.free_count`
   > line of that block is an identity of decision 5's arena and can never
   > fail — this item's own reasoning about vacuous checks applies to it, and
   > was missed. Amendment 2 A11 keeps the line and adds the two clauses that
   > give the arena accounting its force, comparing `arena.free_ids` against
   > the `length` array. The rest of the block stands as written.

   Because the logical checks traverse the structure too, and their behaviour
   on a broken *representation* is undefined, `check_trie` runs `check_patricia`
   first for a `PatriciaTrie`.

Assumptions:

* **Reachability, not `live_count`, is the definition of `node_count`.** The
  other way round — define `node_count` as `live_count` and have the check
  compare `live_count` against a reachability walk — discharges the same
  obligation. I chose reachability because `node_count` is §37's `trie_nodes`,
  an operator-facing metric, and "nodes the trie is actually using" is the
  number an operator means; a leaked slot inflating the published metric would
  be the bug reporting itself as capacity.
* **The `leaves == hot_ip_count` and `2 * leaves - 1` clauses are in
  `check_patricia`.** They are redundant given the shape checks it already
  performs, and nothing asked for them. I included them because the traversal
  has already counted the leaves, so they cost nothing, and because they turn a
  shape bug into a one-line message naming two integers.
* **Visited-set traversal rather than a recursion depth limit.** A cycle in
  `child` is the corruption a recycled-while-referenced id produces, which
  decision 7 names as one of the bug classes this package exists to catch; a
  `RecursionError` from a 128-deep trie would not distinguish it from a deep
  one.

### A3. What the observables do on a corrupted trie: structural iterators, derived counts

**Classification: (b).** The corruption tests inject a bad count and then ask a
check to catch it. Whether that works depends on which observables read the
stored counts and which do not — and the ADR never said. If
`iter_hot_addresses()` filtered leaves on `hot_count == 1`, `check_hot_counts`
would be comparing the stored counts with themselves; if `iter_nodes()` skipped
zero-count nodes, `check_no_orphaned_nodes` could never see one.

Ruling: the package has exactly one source of truth — the stored node records
(§9: `child[0]`, `child[1]`, `hot_count`) and the root reference — and the
observables split cleanly into those that read the *links* and those that read
the *counts*.

```text
structural (links only, counts never consulted for what to yield or where to descend):
    iter_nodes()          every materialized node reachable from the root,
                          pre-order DFS, NodeView.hot_count verbatim
                          (0 and negative values are yielded, not hidden)
    iter_hot_addresses()  every materialized node at length == bit_length,
                          same order
    node_count            == len(list(iter_nodes()))

count-derived (report the stored counts; descent is still by link):
    hot_count(prefix), ancestor_counts(), contains()
    iter_prefix_counts()  the count decides only whether a prefix is yielded
    hot_ip_count          0 for an empty trie, else the root node's stored
                          hot_count -- never a separate counter
```

That split is what makes the checks meaningful: `check_hot_counts` recomputes
§12 from the structural side and compares against the count-derived side, so a
count corrupted anywhere shows up as a disagreement. It also fixes the three
corruption behaviours the tests depend on — a live node whose count is zeroed
is still yielded by `iter_nodes()` (caught by `check_no_orphaned_nodes`); a
root count of `-1` is visible as `hot_ip_count == -1` (caught by
`check_hot_counts` and by testkit's `assert_no_negative_counts`); and a node
whose count is corrupted to zero or below hides its own expanded prefixes from
`iter_prefix_counts()` but not its descendants', so the corruption reads as a
*missing prefix* rather than as an empty trie.

Assumptions:

* **`hot_ip_count` is derived from the root's stored count rather than
  maintained.** A maintained integer would be O(1) either way and would survive
  a corrupted root — but that is precisely the problem: it would be a second
  source of truth that could drift from the counts, and §9 asks for stored and
  derived state to be distinguishable. One list index is not a cost.
* **Undefined behaviour is scoped to representation corruption only.** I define
  the observables' behaviour when a *count* is wrong, because that is
  cheap and the checks depend on it. I explicitly do **not** define what
  `iter_nodes()` or any other traversal does when a *link* is wrong — a
  released id still linked in, a cycle — because making every traversal robust
  to that would cost a liveness test per step on the hot path. `check_patricia`
  is the diagnosis for that class, which is why A2 puts it first in
  `check_trie`.
* **`NodeView.hot_count` is verbatim, not clamped.** Clamping a negative count
  to zero in the view would hide exactly the §11 violation the view exists to
  surface.

### A4. No operation allocates a node it does not keep, so `capacity == peak live` is measurable between operations

**Classification: (b).** Decision 5's "capacity == the maximum number of
simultaneously live nodes" reads on every instant. A test can only sample
between operations. An implementation that allocated a replacement node before
releasing the one it replaces would satisfy the ADR's wording and permanently
carry a slab one or two slots wider than anything the caller can observe — and
epic #8's third acceptance criterion would be unfalsifiable.

Ruling: transient over-allocation within an operation is **not allowed**.
`add_hot_ip` performs allocations and no releases; `remove_hot_ip` performs
releases and no allocations; neither takes a scratch node. The node sets before
and after one operation are therefore nested, `live_count` never exceeds
`max(live_count before, live_count after)` at any instant inside the operation,
and the two readings of "peak" coincide. A caller may take the running maximum
of `node_count` sampled at operation boundaries and require `arena.capacity` to
equal it exactly.

This costs nothing, because decision 6's algorithms need no scratch node: a
split allocates the new internal node and the new leaf and keeps both; a
collapse releases the emptied leaf and its now-single-child parent and relinks
the surviving sibling, which is an existing node, into the grandparent.

**The existing tests match this ruling** and need no change:
`tests/property/test_trie_properties.py::test_every_step_keeps_every_invariant`
compares `arena.capacity` with the running maximum of `node_count` sampled
after each operation, and `test_oscillation_never_grows_the_arena` pins
`(node_count, capacity)` to `(1, 1)`, `(3, 3)` and `(5, 5)` at the operation
boundaries — which additionally requires the "grow by exactly one slot, and
pre-allocate nothing" clause now added to decision 5's promise 2. That test
file's module docstring already flags the gap and says an implementation that
over-allocated transiently "would satisfy the ADR's wording and fail this test,
and that would be worth raising, not hiding". It was right to raise it; this
item rules that the test, not the permissive wording, is what `coder` builds
to.

Assumptions:

* **The strict reading, not the permissive one.** Saying instead "the bound is
  measured between operations" would also have made the test correct and would
  have left implementers freer. I rejected it because the freedom has no use
  here — no algorithm in decision 6 wants a scratch node — and because the
  looser rule would let the slab exceed every number an operator can see, which
  is a memory-bound promise that cannot be audited. If some future operation
  genuinely needs a temporary node, this is the clause to amend, and the
  amendment should say by how much.
* **Growth is by exactly one slot.** Nothing asked for it, and the natural
  `list.append` gives it; but a chunked or doubling growth is the other obvious
  way to write a slab and would break the equality outright. Stated so that it
  is a rule rather than an accident of implementation.
* **A fresh arena pre-allocates nothing.** Assumption 16 already anticipated
  the opposite ("if a future implementation pre-allocates a slab, this becomes
  `<=`"); this makes the current state explicit rather than implied.

### A5. `BinaryTrie` has no documented storage, deliberately

**Classification: (a), already determined; stated explicitly now.** Decision 7
said the binary trie uses plain `TrieNode` objects and decision 5 gave the
arena to the Patricia trie alone, so there was never a promised binary-trie
attribute. Decision 7 now says so in as many words: no attribute name is
promised, and everything a caller is entitled to know is on the `HotTrie`
surface. The asymmetry with `PatriciaTrie.root` / `.arena` is intended — the
Patricia trie's storage is exposed because §27's accounting claims are about
the storage, and the binary trie makes no such claim.

Consequently the tests are right to corrupt the binary trie only through
observables (`test_invariants.py`'s `_DoctoredView`, which overrides one
`HotTrie` observable and delegates the rest). No change to the ADR beyond the
sentence, and none to the tests. Shipped code: none affected.

### A6. §27's example is 32 bits; the brief that called it IPv6 was wrong

**Classification: (a), and a correction to a brief rather than to this ADR.**
`docs/spec/hammertime_spec_1.md` §27 (lines 1296-1302) gives

```text
00000000000000000000000011001010
```

— 24 zero bits then `11001010` — and says it "can be represented as a
compressed edge rather than 32 individual nodes". That is 32 bits, i.e. the
IPv4 address `0.0.0.202`. This ADR never claimed otherwise; the epic brief that
described the example as IPv6 was mistaken, and the `ipv4-section-27-zero-run`
scenario in `test_patricia_equivalence.py` (`0.0.0.202`, `0.0.0.203`,
`0.0.0.0`) is the faithful reading. Its IPv6 companions (`0:ca::`, `::ca`) are
a reasonable extrapolation and are labelled as one in that file's docstring;
they are not in the spec and nothing requires them. No ADR change, no test
change, no spec change.

### A7. A record of the other family is not isolable, and does not need to be

**Classification: (a).** Decision 9's three clauses stand. Because an
`Address`'s family is part of its identity, a records collection containing an
address of the other family can never equal `set(trie.iter_hot_addresses())`,
so the family clause is never the *only* clause a bad collection violates. That
is a property of the type, not a gap.

Ruling, as a refinement only: the family clause is evaluated **first**, so the
failure message names the family error instead of reporting the same records as
simultaneously missing and extra. `packages/hammertime-testkit`'s
`assert_attribute_records_match` already does this; decision 9's assertion
block is reordered to match, and now says why. The tests are right to require
only that such a collection is rejected. Shipped code: none affected (the
testkit file already conforms).

### A8. The derived counts, stated as equalities a test may assert

**Classification: (b) as statements, (a) as derivations.** Each follows from
decisions 2, 4, 6 and 7, but none was written down as a number, so a test
asserting them was asserting something the ADR only implied. They are now in
the decisions themselves; collected here for review:

```text
both:      len(list(iter_nodes())) == node_count                       (A2)
           node_count == 0  <->  hot_ip_count == 0                     (decision 4)
binary:    node_count == len(list(iter_prefix_counts()))
           [nv.prefix for nv in iter_nodes()] == [pc.prefix for pc in iter_prefix_counts()]
           node_count == 33 (IPv4) / 129 (IPv6) for one HOT address
patricia:  node_count == 2 * hot_ip_count - 1   (hot_ip_count >= 1)
           node_count == 0                      (hot_ip_count == 0)
           arena.live_count == node_count
           arena.capacity == arena.live_count + arena.free_count
           iter_nodes() of a one-address trie == [NodeView(/bit_length, 1, ())]
```

The binary identity holds because decision 4 prunes every zero-count node and
the binary trie materializes one node per prefix on each HOT address's path, so
its node set *is* its positive-count prefix set — which is also why the same
DFS order makes the two prefix sequences equal element for element. The
Patricia identity holds because decision 6 admits only leaves and two-child
internal nodes, and the leaves are in bijection with the HOT addresses.

Assumptions:

* **These are contract, not commentary.** I state them as equalities the tests
  may assert rather than as consequences a reader may derive, because a test
  that asserts `2n - 1` is pinning a *shape* decision (decision 6's "no node
  has exactly one child") through a number, and a later implementer who relaxed
  the shape would otherwise see only a mysterious failing arithmetic assertion.
* **The binary trie's 33 / 129 are named.** Nothing required a per-family
  number; it is the most direct test of §27's compression claim (33 nodes
  against 1) and §27's own example is exactly this case (A6).

### A9. Decision 1's `ValueError` covers the queries too

**Classification: (a).** Decision 1 said "any method", which already includes
`contains`, `hot_count`, `ancestor_counts` and `longest_matching_prefix`; the
tests exercise it only on `add_hot_ip` and `remove_hot_ip`. Decision 1 now
lists the methods explicitly so nothing rests on the reader's parse of "any",
and adds that the family is validated before any other argument, so a call that
is wrong in two ways reports the family. That ordering is new (there was no
rule) and is a judgment call: the family mismatch is the caller's more serious
bug, being a routing error rather than a bad parameter.

The missing coverage is a test gap, not a test error — see *Follow-ups*.

### A10. The cost of the IPv6 equivalence sweep is test-author's call, not the ADR's

**Classification: (c), deliberately left open.** Nothing in this ADR fixes a
test budget, and nothing in it requires every ancestor length of every probe to
be queried after every operation; `_assert_equivalent`'s 129-length sweep is
one faithful way to compare the two implementations, and sampling lengths (say
`/0`, the divergence points, and `/bit_length`) would be another. I have no
`Bash` tool and did not run or time the suite, so I am not in a position to say
whether it is actually slow. If it is, narrowing the per-step sweep and keeping
one exhaustive sweep at the end of each sequence is consistent with every
decision here. Shipped code: none affected.

### Follow-ups (not part of this amendment; for the top-level session to dispatch)

* `test-author`: decision 1's `ValueError` is asserted only for `add_hot_ip`
  and `remove_hot_ip`. Add the other-family case for `contains`, `hot_count`,
  `ancestor_counts` and `longest_matching_prefix`, and the case where the
  family and `min_length` are both wrong (the family must be reported) (A9).
* `test-author`: no test pins `arena.capacity == len(arena.network) ==
  len(arena.child) // 2`, or that a growth adds exactly one slot, other than
  indirectly through `test_oscillation_never_grows_the_arena`'s
  `(node_count, capacity)` pairs (A4).
* `test-author`: `check_patricia`'s cycle clause ("no id reached twice") has no
  test; a `child` pointer rewritten to an ancestor would exercise it (A2).
* `test-author`, optional: the module docstrings of the three test files list
  the assumptions A1-A4 and A8 as unpinned. They are pinned now, and the
  docstrings could cite the amendment instead — cosmetic, and only worth a pass
  if those files are being edited anyway.

## Amendment 2 (2026-09-23) — the free list is readable and the arena accounting is checked against it, a mutator may raise `InvariantViolation` for corruption it cannot walk past, `iter_prefix_counts` validates `min_length`

Why: epic #8's structure is now implemented
(`services/trie/src/hammertime/trie/structure/`, six modules with content)
against decisions 1-11 and Amendment 1, with tests, and `reviewer` raised five
non-blocking findings. Two are test gaps and are recorded under *Follow-ups*;
the other three turn on interface questions this ADR had not settled — one of
them a hole in a check Amendment 1 itself specified — and are ruled here as
A11-A13. Unlike Amendment 1, which bound a `coder` brief against six empty
stubs, **all three bind code that exists**: A11 and A13 require changes to it,
A12 ratifies what is already there. Each item says whether the point was (a)
already determined, (b) genuinely unspecified and ruled now, or (c)
deliberately left open.

As with Amendment 1, decision bodies were rewritten in place so that a reader
sees the rule now in force, and earlier amendment sections are left as the
historical record they are, with a dated pointer where a ruling has moved on.
Every edit outside this section, with the superseded wording quoted:

* **Status line.** Named Amendment 1 only; now names this one too.
* **Decision 2, `iter_prefix_counts` bullet.** Said nothing about
  `min_length` validation. Now: out of `[0, bit_length]` is a `ValueError`
  raised at call time, and the method may not be a generator function (A13).
* **Decision 3, new closing paragraph** (*"Never an error" is a statement about
  intact tries*). Decision 3's title and body said a redundant add or remove is
  "never an error" without saying what a mutator does when it finds the trie
  already broken (A12).
* **Decision 5, the `NodeArena` sketch.** Listed four storage lists; now lists
  `free_ids` as the fifth (A11).
* **Decision 5, promise 2.** Was: "Released ids go on a LIFO free list and are
  handed back first; the slab grows only when the free list is empty". Now
  names `free_ids`, `free_count == len(free_ids)` and which end is reused (A11).
* **Decision 5, promise 3.** Was titled "**A released slot is detectably
  dead.**" and ended "A released slot's other fields are stale by design and
  must not be read." Now also states that `length` is the only liveness record,
  that `allocate` rejects a negative `length`, and the two clauses tying
  `free_ids` to the dead slots (A11).
* **Decision 8, `check_patricia` bullet.** The clause block's
  `capacity == live_count + free_count` line was annotated "(the free list
  accounts for the rest)"; it is now marked as an identity kept for a
  counter-maintaining arena, and two `free_ids` clauses are added, with a
  closing sentence on what they catch (A11). *Amendment 2, correction* later
  added one further paragraph there, pinning that the free-list clauses are
  discharged before the two `len(R) ==` comparisons.
* **Decision 8, cost paragraph.** Was: "Cost is O(hot_ip_count × bit_length),
  which is why…". Now adds the O(capacity) arena scan (A11).
* **Assumption 18** — a dated blockquote pointing to A11, A12 and A13.
* **Amendment 1, A2** — a dated blockquote recording that the
  `capacity == live_count + free_count` line of its own clause block is
  vacuous, and pointing to A11.

### A11. The free list is readable as `arena.free_ids`, and `check_patricia` checks it against the `length` array

**Classification: (b), genuinely unspecified — and a defect in Amendment 1 A2.**
`check_patricia`'s clause `capacity == live_count + free_count` cannot fail.
`live_count` is `capacity - free_count` by construction
(`len(length) - len(free_ids)`), so the equality is an identity, and A2's own
argument against vacuous checks — "a check that took the implementation's own
answer for the quantity it is checking would pass by construction" — applies to
it. Nothing in the package checks that the free list holds exactly the dead
slots.

The hole is not theoretical. Put a live, *reachable* id on the free list and
leave one unrelated live slot unreachable: `live_count` under-counts the live
slots by one and the reachability walk misses one, the two errors cancel,
`len(R) == live_count` passes, `capacity == live_count + free_count` passes
because it always does, and the next `allocate()` hands back an id that is
still linked into the trie and overwrites it. That is exactly the
"slot recycled while still referenced" bug class decision 7 names as one of the
reasons this package has an oracle at all.

Ruling, in three parts.

1. **The free list is part of the arena's interface, named `free_ids`**, a
   `list[NodeId]` alongside `network`, `length`, `hot_count` and `child`, LIFO
   with its last entry the next one handed out, and `free_count ==
   len(free_ids)`. It is readable and writable for the same reason the other
   storage lists are (assumption 13, Amendment 1 A1's third assumption): the
   invariant checks and `tools/trie-inspect` must read it, and the tests inject
   corruption by writing the arena's documented storage. `PatriciaTrie` itself
   never touches it — only `allocate`, `release` and `clear` do.
2. **`check_patricia` discharges the new free-list clauses**, stated in
   decision 5's promise 3 and decision 8's block: every id in `free_ids` is in
   range and has `length < 0`, every slot with `length < 0` is in `free_ids`
   (i.e. set equality both ways), and no id appears in `free_ids` twice. The existing
   `capacity == live_count + free_count` line stays — it costs one comparison
   and is not an identity for an arena that maintained its counters — but its
   annotation now says it cannot fail for decision 5's arena, so nobody reads
   it as the accounting check again. With the new clauses, `len(R) ==
   live_count` finally says what it was meant to say: the reachable set is
   *exactly* the live set.
3. **`NodeArena.allocate` rejects a negative `length` with a `ValueError`.**
   This is what makes clause 2 well founded: `length < 0` is the only record of
   deadness, so a slot that is allocated and simultaneously reads as dead would
   make both `is_live` and the dead-slot set meaningless. The shipped
   implementation already does this; it was never written down.

Assumptions:

* **`free_ids`, not `free`, and not a read accessor.** `arena.free` reads like
  a method (`arena.free(nid)`) at a call site; `free_ids` pairs with
  `free_count`. A `free_ids()` accessor returning a tuple would also close the
  hole, and would be the more defensive design — but it would make the
  "no id twice" clause untestable through documented storage, since nothing
  outside the arena could then produce a duplicated entry, and Amendment 1 A1
  already ruled that corruption is injected by writing the arena's public
  storage. A `dead_count` property instead of a readable list would be cheaper
  still and would catch most of the hole, but not all of it: with
  `dead = {1, 2}` and `free_ids = [1, 3]` where 3 is live, the counts agree and
  a live id is queued for reuse. I took completeness over encapsulation, in a
  package whose storage is already deliberately public.
* **Both directions of the set equality — but the reverse direction is kept
  for diagnosis, not for coverage.** (Corrected 2026-09-23; the superseded
  wording is quoted under *Amendment 2, correction*.) "Every id in `free_ids`
  is in range and dead" and "no id twice" are what close the hole above. The
  reverse direction — every dead slot is on the free list — catches nothing
  that the clauses already there would let through, for decision 5's arena.
  Write `F` for `set(free_ids)`, `D` for `{i : 0 <= i < capacity and
  length[i] < 0}`, and `R` for the reachable set. The forward clauses give
  `F ⊆ D` and `free_count == len(free_ids) == |F|`; the walk gives
  `R ⊆ [0, capacity) \ D` with no id reached twice; and `live_count ==
  capacity - len(free_ids)` by construction. So `|R| <= capacity - |D| <=
  capacity - |F| == live_count`, and the clause `len(R) == live_count` forces
  `|D| == |F|`, hence `F == D`. A dead slot left off `free_ids` therefore
  always breaks `len(R) == live_count` too. What the reverse clause adds is the
  *message*: discharged first (decision 8), it names the slot — "node id 41 has
  length -1 (dead) but is not in arena.free_ids" — where the count clause says
  only "5 nodes are reachable from the root but the arena holds 6 live slots",
  which reads like a leaked *live* slot and sends the reader to the wrong bug.
  I keep it for that, and because it is free once the scan exists and
  `capacity == peak live` is a promise this package makes (decision 5,
  assumption 16), so silently losing a slot is a broken promise even when no
  query answers wrongly.
* **The reverse clause's diagnosis is only available if the free-list clauses
  run first, so decision 8 now pins that order.** (Added 2026-09-23 with the
  correction below.) `check_patricia` discharges the free-list clauses before
  `len(R) == node_count` and `len(R) == arena.live_count`; the shipped check
  already did, and says so in its docstring. Pinning it is what makes the
  naming of the leaked slot a property a test may assert rather than an
  accident of statement order. Nothing else in the block is ordered — the
  remaining clauses may be discharged in any order, subject to the walk
  necessarily preceding anything stated about `R`.
* **An O(capacity) scan in `check_patricia` is acceptable.** `capacity` is
  bounded by the peak live node count, the check is already O(|R|) and is
  called only by debug builds and `trie-inspect --verify`. Stated in decision
  8's cost paragraph so it is a considered cost, not an accident.
* **No sixth check.** Decision 8 fixes five names and `__init__.py` re-exports
  them; a `check_arena` would be a public-surface change for arena accounting
  that decision 8 already assigns to `check_patricia`. Nothing changes for
  `BinaryTrie`, which has no arena (A5).
* **`live_count` stays O(1) and derived.** Defining it as a count of slots with
  `length >= 0` would make it independently true but turn
  `PatriciaTrie.node_count` — which returns it — into an O(capacity) property
  on a metrics path (§37's `trie_nodes`). Keeping it derived and checking it is
  the same trade A2 made for `node_count`.
* **`tools/trie-inspect` is not in scope.** Whether it prints the free list is
  that tool's call; this item only makes the list readable.

### A12. A mutator may raise `InvariantViolation` for corruption it cannot walk past, before mutating anything

**Classification: (b).** `PatriciaTrie.add_hot_ip` reaches a node at
`length == bit_length` whose network equals the address only when `contains()`
has already said the address is not HOT — i.e. only when that leaf's stored
count is not 1, which no sequence of public calls can produce. The shipped code
raises `InvariantViolation` there. Assumption 18 reserved that exception for
`invariants.py`, so the guard was outside the ADR as written, and `BinaryTrie`
in the same state does not raise (it increments the corrupted leaf and its
path). Neither behaviour was decided.

Ruling: **allowed, and required**, narrowly.

1. A mutator that discovers, mid-walk, a broken structural invariant it cannot
   proceed past without corrupting the structure or reading an unallocated slot
   raises `InvariantViolation`, with decision 8's message content (the spec
   section, the node id at fault, and the two values that disagree). It does
   **not** repair, does not return `False`, and does not continue.
2. **It raises before mutating anything.** The trie is left exactly as it was,
   so no half-updated path is observable (§28) and the caller may fail without
   having applied part of an event.
3. **There is exactly one such site in this package**:
   `PatriciaTrie.add_hot_ip` finding a `bit_length`-long node for the address
   whose count is not 1. `remove_hot_ip` needs none — its walk stops at the
   first node of length `bit_length`, which is the node `contains()` answered
   from, so a `True` from `contains()` guarantees the walk is well defined —
   and the query walks return `0` rather than descending past a divergence. No
   implementation should add speculative checks elsewhere: this is a ruling
   about a case that has nowhere to go, not a licence to validate on the hot
   path.
4. **`BinaryTrie` is not required to match.** §27's equivalence, and every
   differential test that carries it, bind tries that satisfy §12. On a trie
   that is already corrupt the two implementations may differ, and no test may
   assert equivalence across a corrupted state.
5. **For the worker epic (§28):** an `InvariantViolation` out of a mutator is
   not a per-event validation error. The event that triggered it is not
   malformed, retrying it will not help, and dead-lettering it hides a
   structure that is already wrong. It means this process's in-memory trie is
   untrustworthy and must be rebuilt from the snapshot plus replay (§33,
   ADR-0011, ADR-0013) rather than kept in service. Whether that is a crash, a
   shard stop with an alert, or an automatic reload is the worker epic's
   decision, exactly as decision 1 left the other-family event to it.

Assumptions:

* **Allowing the raise rather than removing it.** The alternatives, all
  rejected: returning `False` (reports a corrupt leaf as an ordinary redundant
  add, and the trie stays broken); setting the count to 1 and carrying on
  (repair — decision 8 already rules that the checks never repair, and a
  mutator is a worse place to do it silently); dropping the guard (the walk
  then steps to `NO_NODE` and indexes the storage lists with `-1`, which in
  Python reads the last slot instead of failing, so the operation would mutate
  an unrelated node's counts); and incrementing the leaf and its path to match
  `BinaryTrie` (makes the corruption worse and still leaves a leaf whose count
  is not 1). A loud failure that changes nothing is the only one of the five
  that neither hides the bug nor compounds it.
* **`InvariantViolation`, not `ValueError`.** Nothing is wrong with the
  argument; §12's invariant is broken.
  `hammertime.core.errors.InvariantViolation` already exists and its docstring
  already names §12.
* **"Exactly one site" is a statement about the current algorithms.** I derived
  it by reading the shipped walks rather than by proof: `contains()` and
  `remove_hot_ip` follow the same links and stop at the same node. An
  implementation that made those two walks differ would need this clause
  re-examined, and should say so rather than adding a second raise site
  quietly.
* **Asymmetry with the oracle is accepted rather than removed.** Requiring
  `BinaryTrie` to raise too would make the two agree everywhere, including on
  corrupt input, at the cost of a count comparison in the oracle's add path and
  of code whose only purpose is to reproduce a failure mode. Decision 7 wants
  the oracle obviously correct and minimal, and equivalence over corrupt tries
  is not a property anything needs. The consequence — a differential test must
  not compare corrupted tries — is a test rule, listed under *Follow-ups*.
* **The worker's response is constrained, not chosen.** I state what it must
  not do (swallow, retry, dead-letter as malformed) because those are the
  readings that would turn a corrupt structure into silent wrong answers, and
  leave the mechanism to the worker epic. Nothing in the spec dictates either.

### A13. `iter_prefix_counts(min_length=...)` validates its argument, at call time

**Classification: (b).** Decision 2 gives `ancestor_counts` an explicit "`L`
outside `[0, bit_length]` is a `ValueError`" and says nothing of the kind for
`iter_prefix_counts`, which takes a `min_length` with the same name, the same
meaning and the same natural range. Both shipped implementations therefore
accept anything: a negative value yields every prefix, an oversized one yields
none, and in both cases the caller is silently answered a question it did not
ask.

Ruling: `iter_prefix_counts` validates `min_length` exactly as
`ancestor_counts` does — `0 <= min_length <= bit_length`, otherwise a
`ValueError` naming the range and the family — and validates it **when the
method is called**, not when the returned iterator is first advanced.
Consequently neither implementation's `iter_prefix_counts` may be a generator
function; each validates and then returns an iterator (for example by
delegating to a private generator). `iter_hot_addresses` and `iter_nodes` take
no arguments and are unchanged, as is testkit's `TrieView.iter_prefix_counts()`
(decision 10), which declares no `min_length`.

Assumptions:

* **Validate rather than clamp.** Clamping (`max(min_length, 0)`, and an empty
  result above `bit_length`) is defensible and is what the code does today by
  accident. I chose the error because the same parameter on the same Protocol
  already raises on `ancestor_counts`, and an interface whose two
  length-filtered methods disagree about a bad argument is a trap for the query
  epic that will call both.
* **Eagerly, at the cost of the methods no longer being generator functions.**
  A lazy check surfaces the `ValueError` at whatever later point first advances
  the iterator, which may be inside a serializer or a comprehension far from
  the mistake; decision 1's "the family is checked before any other argument"
  (A9) already sets call-time validation as this package's habit. The cost is
  one extra private method per implementation.
* **`bit_length` itself stays a legal value** (it selects the host routes
  alone), as it is for `ancestor_counts`.
* **No `CHANGES` entry.** Assumption 25 still holds: nothing outside this
  package's own tests calls `iter_prefix_counts` yet, so no deployment
  behaviour changes. The entry belongs to the change that makes the trie
  service consume the structure.
* **Nothing is ruled about the read API.** `docs/protocol/read-api-v1.md`'s
  `GET /prefixes/hot` takes `?minimal=true` and no length parameter today; if
  the query epic ever exposes one, translating a user value into a 4xx is its
  job, not this package's.

### Follow-ups (not part of this amendment; for the top-level session to dispatch)

Amendment 1's four follow-ups are **still open and still wanted** — none of
them was done when the structure landed. Restated with what each must show, and
joined by what A11-A13 add:

* `test-author`: no randomized test calls `check_trie` or `check_patricia`.
  Consequence (2) of this ADR says the second acceptance criterion is
  "`check_trie` after every step of a randomized sequence", and only the
  testkit `assert_*` helpers run there today. The service-side checks must run
  on every step of the property-test sequences as well; they are a different
  audience and a different failure mode (decision 10, rule 2), and
  `check_patricia`'s representation clauses have no other randomized coverage.
* `test-author`: decision 1's `ValueError` is asserted only for `add_hot_ip`
  and `remove_hot_ip`; `contains`, `hot_count`, `ancestor_counts` and
  `longest_matching_prefix` need it too, as does the both-wrong call where the
  family must be reported ahead of a bad `min_length` (A9).
* `test-author`: `arena.capacity == len(network) == len(hot_count) ==
  len(child) // 2`, growth by exactly one slot, and LIFO reuse order are
  pinned only indirectly, through `test_oscillation_never_grows_the_arena`'s
  `(node_count, capacity)` pairs (A4, decision 5 promise 2).
* `test-author`: `check_patricia`'s representation clauses are largely
  untested — the cycle and shared-subtree clauses ("no id reached twice"), an
  out-of-range or negative child id, a one-child node, a wrong branch bit, a
  network with host bits set, and the storage-list-length clause (A2,
  decision 6).
* `test-author` (A11): the three forward free-list clauses — an entry outside
  `[0, capacity)`, an entry that is live, and an entry listed twice — each with
  a corruption that only it can catch. For each that means pairing the bad
  entry with one live slot nobody links to (an `arena.allocate` on an otherwise
  healthy trie, done *before* the entry is appended), so that `len(R) ==
  live_count` still holds and cannot be what fires. The reverse clause has no
  such corruption — it is implied by the forward clauses together with
  `len(R) == live_count` (A11's second assumption, as corrected) — so pin its
  **diagnosis** instead: release a slot, take that id back off `free_ids`, and
  assert that `check_patricia` raises `InvariantViolation` with a message
  naming that slot id, rather than the count mismatch the same state also
  produces. Plus `allocate(length=-1)` raising `ValueError`.
* `test-author` (A12): `PatriciaTrie.add_hot_ip` against a leaf whose count has
  been corrupted raises `InvariantViolation` and leaves every observable
  unchanged. No test may assert Patricia/binary equivalence on a corrupted
  trie.
* `test-author` (A13): both implementations reject a `min_length` outside
  `[0, bit_length]` on `iter_prefix_counts`, at call time rather than on first
  advance, and `min_length == bit_length` is accepted.
* `coder` (A11, A13): the arena's `free_ids`, `check_patricia`'s free-list
  clauses, and eager `min_length` validation in both implementations. A12
  requires no code change — the shipped guard already conforms — beyond citing
  the amendment where the guard is written.
* `test-author`, optional, unchanged from Amendment 1: the three test files'
  module docstrings list assumptions that later amendments have pinned, and
  could cite the amendments instead.

### Amendment 2, correction (2026-09-23) — the reverse free-list clause is kept for diagnosis, and its order is pinned

`reviewer` raised, non-blocking, that A11's second assumption justified the
reverse direction of the free-list set equality ("every slot with `length < 0`
is in `free_ids`") as the clause that catches a leaked dead slot, and that the
Follow-ups accordingly asked for a test in which each new clause has "a
corruption that only it can catch" — while in fact the reverse direction is
implied by the forward clauses together with `len(R) == live_count`, so no such
corruption exists for it. The argument holds; it is reproduced in A11's
assumption as rewritten. Its consequence is that the reverse clause's only
value is the message it produces, which is available only if it is discharged
before the count clauses — so that order, which the shipped `check_patricia`
already used, is now pinned in decision 8.

**No decision's required behaviour changes.** `check_patricia` discharges the
same clauses, with the same messages, in the order the implementation already
had; what changes is why the reverse clause is there, what a test of it must
show, and that the order is now stated rather than incidental.

Four edits, with the superseded wording quoted:

* **Status line.** Named Amendment 1 and Amendment 2; now names this correction
  too.
* **A11, second assumption.** Was titled "**Both directions of the set
  equality, not just "every free id is dead".**" and read: "The reverse
  direction (every dead slot is on the free list) catches a leaked dead slot,
  which is a leak of capacity rather than a correctness bug. I included it
  because it is free once the scan exists and because `capacity == peak live`
  is a promise this package makes (decision 5, assumption 16), so silently
  losing a slot is a broken promise even when no query answers wrongly." Now
  gives the derivation, keeps the clause for its message, and is followed by a
  new assumption on the order the clauses are discharged in.
* **Decision 8, `check_patricia` bullet.** A paragraph is appended after "…and
  every accounting equality still holds (Amendment 2, A11)." stating that the
  clause block is unordered except that the free-list clauses run before
  `len(R) == node_count` and `len(R) == arena.live_count`. Nothing already
  there was removed or reworded.
* **Follow-ups, the `test-author` (A11) bullet.** Was: "the new free-list
  clauses, each with a corruption that only it can catch, plus
  `allocate(length=-1)` raising `ValueError`." Now asks for an isolating
  corruption for the three forward clauses only, says what makes one isolating,
  and asks for the reverse clause to be pinned by its diagnosis.

Assumptions:

* **The redundancy is conditional on decision 5's arena, and I state it that
  way.** The derivation uses `live_count == capacity - len(free_ids)` and
  `free_count == len(free_ids)`, which hold because both are derived. For an
  arena that maintained those counters independently — the case the
  `capacity == live_count + free_count` line is expressly kept for (A11,
  ruling 2) — the derivation does not go through, and the reverse clause would
  carry coverage of its own. Nobody asked which arena the claim was about; I
  took the narrower, checkable one.
* **Keeping the clause rather than deleting it.** Dropping the reverse
  direction is defensible: it is implied, and the check would be shorter. I
  keep it because the message is the only thing at the point of failure that
  distinguishes a leaked *dead* slot from a leaked *live* one, and because
  removing a clause would be a change to required behaviour, which a
  correction of rationale should not smuggle in.
* **The order is pinned narrowly.** Only "free-list clauses before the two
  `len(R) ==` comparisons" is fixed — not a total order over the block —
  because that is the only ordering the diagnosis depends on, and because the
  shipped `check_patricia` already satisfies it, so no code has to change. I
  assume the rest of the block stays an unordered set of requirements.
* **What makes a forward-clause corruption isolating.** The Follow-ups now say
  that each of the three forward clauses needs a compensating leaked live slot.
  I derived that rather than being told it: without one, removing or adding a
  `free_ids` entry moves `live_count` by one and `len(R) == live_count` fires
  as well, so the corruption is no longer caught by that clause alone.
* **No `CHANGES` entry.** Nothing a deployment can observe changes — same
  clauses, same messages, same order as the code already ran in. Per the repo's
  rule, an entry I am unsure about does not qualify, and this one is not close.
