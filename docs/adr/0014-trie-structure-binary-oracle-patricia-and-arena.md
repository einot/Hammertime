# ADR 0014 — The trie structure package: one trie per family, an unshared binary oracle, an arena-backed Patricia trie, and the invariants both satisfy

Status: accepted

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
`ValueError` naming both families, and changes nothing.

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
  test, so the count and the set cannot drift apart.
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
  model MUST remain equivalent to the binary trie".
* **`iter_hot_addresses()`** yields the currently HOT addresses in ascending
  numeric order (the same DFS, at leaf level). It is what the snapshot epic
  serializes and what the invariants recompute from.
* **`iter_nodes()`** yields the *materialized* nodes in the same DFS order —
  the representation, not the logical model. The two implementations
  deliberately differ here, and only the invariant checks and `trie-inspect`
  should care.
* **`clear()`** empties the trie: `hot_ip_count == 0`, `node_count == 0`, and
  the arena is reset (decision 5).

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
Patricia trie's (decision 7).

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
2. **`allocate` reuses before it grows.** Released ids go on a LIFO free list
   and are handed back first; the slab grows only when the free list is empty,
   i.e. only when `live_count == capacity`. Therefore, exactly:

   ```text
   capacity == the maximum number of simultaneously live nodes
               since construction or the last clear()
   ```

   That equality is epic #8's third acceptance criterion made testable: churn
   from HOT/COLD oscillation re-uses slots forever and never grows the slab,
   and the only thing that can grow it is genuinely holding more nodes at once
   than ever before. There is no compaction and the slab never shrinks except
   on `clear()`.
3. **A released slot is detectably dead.** `release` writes `-1` into
   `length`, so `is_live` is one comparison, a double `release` is a
   `ValueError`, and the invariant checks can assert that every reachable child
   id is live. A released slot's other fields are stale by design and must not
   be read.
4. **`clear()` resets the slab to empty** — `capacity == 0` — so a trie reused
   after `clear()` re-establishes the bound above from scratch.

### 6. The Patricia representation, stated exactly enough to be equivalent

Each node stores the prefix it represents (`network` with host bits zeroed,
`length`), its `hot_count`, and two child ids. `PatriciaTrie.root` is a
`NodeId` (or `NO_NODE`), so the topmost node may sit at any length; there is no
permanent `/0` node. Invariants (decision 8) pin the shape: every node is
either a leaf at `length == bit_length` with no children, or an internal node
with **exactly two** children whose prefixes both strictly extend it and differ
at bit `length`; no node has exactly one child (that is the compression), and
`hot_count` is the sum of the children for an internal node and `1` for a leaf.

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
and `patricia.py`. The only code both import is `hammertime.core.addressing`
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
  have a positive count.
* **`check_patricia`** adds the representation checks of decision 6 — exactly
  two children or none, children strictly extending and branching correctly,
  leaves at `bit_length`, sum-of-children — plus the arena accounting:
  `live_count == node_count`, `capacity == live_count + free_count`, every
  reachable child id live, no id reachable twice.
* **`check_trie`** runs `check_hot_counts` and `check_no_orphaned_nodes`, and
  additionally `check_patricia` when handed a `PatriciaTrie`. It is the one
  entry point a debug build or the inspect tool calls.

Cost is O(hot_ip_count × bit_length), which is why the module docstring's
"cheap enough for debug builds" is honest for a trie whose size §26 bounds by
the hot set, and why nothing on the hot path calls it unconditionally.

### 9. §46.5's derived invariant is expressed as a `Collection[Address]`, so this epic never touches the record map

```python
def check_attribute_records(trie: HotTrie, records: Collection[Address]) -> None
```

asserts, for one address family:

```text
len(records) == trie.hot_ip_count                      §46.5, ADR-0005 decision 4
set(records) == set(trie.iter_hot_addresses())         §46.5's first line
every address in records has family == trie.family     §46.5 "per address family"
```

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
not dictate. Push back on them individually.

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
17. **O(1) node access is discharged structurally, not by a timing test.** A
    wall-clock assertion on a garbage-collected interpreter in CI is a flake
    generator. `tests/bench/test_throughput.py` (§40) is where numbers belong,
    and it is not in this epic.
18. **`ValueError` for a family mismatch, an out-of-range `min_length`, and a
    double `release`; no new error types.** `InvariantViolation` is reserved for
    a broken structural invariant, i.e. for `invariants.py`.
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
  zero" (decision 4); (3) `arena.capacity == peak live node count` and
  `node_count == 0` after removing everything.
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
