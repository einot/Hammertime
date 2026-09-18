# ADR 0012 — Trie service: arena-backed Patricia trie with a bit-trie oracle, prefix metadata, the per-IP attribute map, the single-writer worker, `PrefixStatsChanged` publishing, and the read API

Status: accepted; amended 2026-09-18 (see "Amendment 1" at the end — four
interface gaps the M5 test-author raised while writing `test_metadata.py`
and `test_prefix_state.py` are ruled: `combine_path` on an unregistered
name, name validation in the trie's metadata setters, the family order of
`IpAttributeStore.records()`, and `evaluate_prefix_state` with
`hot_count > capacity`; three test-author assumptions are confirmed. The
amendment is open-ended — later items continue the A-numbering. In the body
below, numbered Assumptions are referred to as "assumption N"; "A<n>" names
an amendment item.)

Scope note: this ADR settles the interfaces milestone M5 (epics #8, #9,
#10) implements against — the node and arena representation, the reference
`BinaryTrie` and the production `PatriciaTrie` and the contract that makes
them interchangeable, the structural invariants, prefix-scoped metadata and
its `combine()`, the per-IP attribute side map, the single-writer worker and
its one atomic apply step, what the trie publishes and when, the read API's
views, the service's settings, metrics, log records, readiness, consumer
group and commit cadence, and what a configuration change means to it. It
builds on ADR-0001 (one logical trie owner; Amendment 1's consistency
model), ADR-0003 (Amendment 2: at-least-once, same-`event_id` duplicates),
ADR-0005 (binary trie, per-IP attributes beside it), ADR-0009 (process
lifecycle) and ADR-0010 (one prefix predicate, the read API, the stats
granularity) as written, and amends none of them; where it narrows an
ambiguity one of them left, it says so. It does not design snapshots
(`services/trie/snapshot/`, M6, epic #11) or the detector (M7, epics
#12/#13); the boundary with each is stated in decisions 11 and 12.

## Context

`services/trie/` is stubs whose docstrings record the original design intent
(a bit-by-bit oracle differentially tested against a Patricia trie; integer
node ids in an arena because HOT/COLD oscillation churns allocation; a
writer that never lets a reader see a torn path; metadata stored where it is
declared and combined on lookup). The spec fixes the arithmetic (§10-§12:
`hot_count` along the path, `hot_count(/32) ∈ {0,1}`, the sum invariant,
`hot_count >= 0`), the representation freedom (§27: Patricia is welcome, the
*logical* model must stay the binary trie), the atomicity requirement (§28)
and the read path's shape (§29, `docs/protocol/read-api-v1.md`). Prior ADRs
fix the pipeline position: the trie is the single consumer-group member of
`hammertime.hot-ip.v1` under the fixed group `hammertime-trie` (ADR-0009
decision 9, ADR-0001 Amendment 1), it publishes one `PrefixStatsChanged` per
ancestor from a minimum length to the host route with one service-wide
`sequence` (ADR-0010 decision 3), it flushes before it commits (ADR-0009
Amendment 2), and it must absorb a `HotIpAdded` for an IP it already holds,
a `HotIpRemoved` for one it does not, and a same-`event_id` duplicate as
no-ops (ADR-0001 Amendment 1 clause 5; ADR-0011 Consequences, *Trie epic*).

Seven things remain open and have to be settled before a test or a module
can be written:

1. **What a Patricia node is** and what happens to the logical prefixes a
   compressed edge skips — they have a `hot_count` (§27's equivalence says
   so) and can therefore be `HOT_PREFIX` without being materialized.
2. **Pruning.** §11 allows pruning empty nodes and warns about churn. The
   node count must be bounded by the hot set, and prefix metadata (§16) has a
   lifetime independent of hot state (§46.6), so a node carrying it cannot be
   pruned.
3. **Atomicity's mechanism.** The `worker.py` stub proposes a versioned
   snapshot pointer. The service is one asyncio event loop; readers and the
   writer never run concurrently unless something awaits mid-update.
4. **Which events count.** ADR-0010 decision 3 says stats are emitted on
   "each applied" event; ADR-0011 left open whether a no-op event emits.
   `event_sequence` on read responses is "the number of hot-IP events applied
   so far" and needs a definition of *applied*.
5. **Where replay starts without a snapshot.** ADR-0009 decision 4 makes the
   trie ready only once it has replayed to the log end as it stood at start.
   M6 owns snapshots; M5 must still start correctly from an empty state, and
   the bus `Consumer` has no way to ask where the log ends.
6. **IPv6.** The aggregator accepts IPv6 observations (ADR-0011 assumption
   20) and emits transitions for them; ADR-0010 left the IPv6 minimum
   reported length undefined; ADR-0001 says one writer per address family;
   §35 says separate roots.
7. **`hammertime.core.state.prefix` does not exist.** ADR-0010 decision 1
   specifies it; the read API cannot return `state` without it.

## Decision

### 1. Layout: one process, one consumer, one trie per address family, one `event_sequence`

```text
packages/hammertime-core/src/hammertime/core/state/prefix.py     evaluate_prefix_state (ADR-0010 decision 1; Spec: section 13, section 38)
packages/hammertime-bus/src/hammertime/bus/interface.py         + Consumer.end_offsets, + MessageBus protocol (decision 8)
packages/hammertime-bus/src/hammertime/bus/kafka.py             + KafkaBus (the producer/consumer pair behind MessageBus)
packages/hammertime-core/src/hammertime/core/events/{models,codec}.py   + PrefixStatsChanged.hot_ratio (optional), + AttributesError(CodecError)

services/trie/src/hammertime/trie/
  __init__.py                       Spec: sections 8-12, 27, 28, 29, 33 (unchanged)
  __main__.py                       Spec: section 33, section 47   main() -> run_service("trie", build_from_env)
  config.py                         Spec: section 33, section 35, section 47   TrieSettings, load_settings
  service.py                        Spec: section 47   (new) TrieService, build_service(settings, *, bus=None, clock=None)
  state.py                          Spec: section 12, section 28, section 46.5   (new) TrieState, HotIpRecord, ApplyOutcome, Applied
  worker.py                         Spec: section 28, section 32, section 47.2   TrieWorker (consume, replay, commit)
  publisher.py                      Spec: section 14, section 19, section 22   PrefixStatsPublisher
  metrics.py                        Spec: section 37, section 46.8   (new) TrieMetrics
  structure/node.py                 Spec: section 9   TrieNode, NodeId, NO_NODE
  structure/arena.py                Spec: section 11, section 27   NodeArena
  structure/binary_trie.py          Spec: section 10, section 11, section 39   BinaryTrie (oracle)
  structure/patricia.py             Spec: section 27   PatriciaTrie (production)
  structure/invariants.py           Spec: section 11, section 12   check_invariants, Edge, logical_prefixes
  metadata/local.py                 Spec: section 16, section 17   Policy, validate_metadata_name
  metadata/combine.py               Spec: section 16   Combiner, SetUnion, BitmaskOr, PriorityOverride, MetadataRegistry, effective_metadata
  metadata/ip_attributes.py         Spec: section 46.5, section 46.7, section 46.8   IpAttributeStore, DEFAULT_ATTRIBUTES
  query/app.py                      Spec: section 29, section 31, section 47.2   create_app(read_model, readiness, *, render_metrics=None) -> FastAPI
  query/views.py                    Spec: section 22, section 29, section 31   ReadModel, QueryError, render_timestamp
  snapshot/                         M6 (epic #11); stubs untouched by M5
  tests/                            test_structure.py (new), test_invariants.py, test_patricia_equivalence.py, test_metadata.py,
                                    test_worker.py (new), test_publisher.py (new), test_query.py (new), test_config.py (new),
                                    test_service.py (new); test_snapshot.py stays a stub (M6)
tests/property/test_trie_properties.py                            Spec: section 12 (Hypothesis stateful machine, decision 4)
packages/hammertime-testkit/src/hammertime/testkit/invariants.py  assert_hot_count_consistent, assert_no_negative_counts,
                                                                  assert_attribute_records_consistent (new); assert_hysteresis_holds untouched
packages/hammertime-testkit/src/hammertime/testkit/generators.py  addresses(), prefixes(), hot_ip_streams() (Hypothesis strategies)
```

The service is **one process** that is the single writer for **both**
address families (ADR-0001: "a single-writer process per address family" —
the same process may be the writer for both; §35: "separate roots"). It
holds one `PatriciaTrie` per `AddressFamily` inside one `TrieState`, consumes
`hammertime.hot-ip.v1` as the one member of group `hammertime-trie`
(group-managed assignment, `partitions=None`, so it holds every partition),
and keeps **one** `event_sequence` for the whole service, incremented per
applied event of either family (ADR-0010 decision 3: "one counter for the
whole service"). ADR-0001 Amendment 1's assumption bullet "each family's
trie has its own `event_sequence`" is superseded by this ruling (recorded
there as a dated pointer); a per-family counter would leave the detector's
`event_sequence` — "the `sequence` of the newest `PrefixStatsChanged`
applied" — undefined across families.

The minimum reported prefix length (ADR-0010 decision 3) becomes
per-family: `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` (IPv4, default 8) and
`HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_V6` (IPv6, default 104). 104 is the
IPv6 length whose capacity (2^24) equals IPv4's /8, so both families report
the same 25 capacity levels per transition (assumption 6).

### 2. Node, arena, and the two tries

```python
# hammertime.trie.structure.node   (Spec: section 9)
NodeId = int
NO_NODE: Final[NodeId] = -1

@dataclass(slots=True)
class TrieNode:
    network: int                    # the prefix's network bits; host bits are zero
    prefix_length: int              # depth in the *logical* binary trie; 0 is the root
    child0: NodeId                  # NO_NODE when absent   (spec section 9: child[0])
    child1: NodeId                  # NO_NODE when absent   (child[1])
    hot_count: int                  # stored; the load-bearing value (section 12)
    local_metadata: dict[str, object]   # section 16; {} when none, never None
    def child(self, bit: int) -> NodeId: ...
    def set_child(self, bit: int, node: NodeId) -> None: ...
    @property
    def pinned(self) -> bool: ...   # bool(self.local_metadata)
```

`prefix_state` (§9) is **not stored**: it is derived at read time from
`(hot_count, capacity, config)` by `evaluate_prefix_state` (decision 9). §9
asks the implementation to distinguish stored from derived state; the
cleanest distinction is to store nothing derived (assumption 3).

```python
# hammertime.trie.structure.arena   (Spec: section 11, section 27)
class NodeArena:
    def __init__(self) -> None: ...
    def allocate(self, network: int, prefix_length: int) -> NodeId   # hot_count 0, no children, no metadata; reuses a freed slot before growing
    def free(self, node: NodeId) -> None                              # ValueError if the slot is not live
    def __getitem__(self, node: NodeId) -> TrieNode                    # O(1); ValueError if the slot is not live
    @property
    def live_count(self) -> int: ...
    @property
    def capacity(self) -> int: ...          # slots ever allocated: live_count + free-list length
    def live_ids(self) -> Iterator[NodeId]: ...
```

Node ids are indices into one list per arena; a freed slot goes onto a free
list and is handed out again before the list grows. That is what turns
HOT/COLD oscillation (§11) into no allocation at all after the first cycle:
`capacity` is bounded by the peak live count, never by the number of
transitions (assumption 4).

Both tries expose the same public API (the equivalence contract of decision
3 is over these methods); each is built for exactly one family and raises
`ValueError` for an `Address`/`Prefix` of the other family:

```python
# hammertime.trie.structure.binary_trie   (Spec: section 10, section 11, section 39)
# hammertime.trie.structure.patricia      (Spec: section 27)
class BinaryTrie:            # and PatriciaTrie, identically
    def __init__(self, family: AddressFamily) -> None: ...
    family: AddressFamily
    def add_hot_ip(self, ip: Address) -> bool        # True iff the IP was not HOT (hot_count(/32) went 0 -> 1); False changes nothing
    def remove_hot_ip(self, ip: Address) -> bool     # True iff the IP was HOT (1 -> 0); False is section 11's no-op, nothing changes
    def is_hot(self, ip: Address) -> bool
    def hot_count(self, prefix: Prefix) -> int       # section 12 over the logical trie; 0 for a prefix with no node
    def path_counts(self, ip: Address) -> tuple[int, ...]   # length bit_length + 1; index L is hot_count of ip's /L ancestor
    def longest_match(self, ip: Address) -> Prefix | None   # longest prefix containing ip with hot_count > 0; None iff hot_ip_count == 0
    def hot_ips(self) -> Iterator[Address]           # every HOT address, ascending by value
    def logical_prefixes(self, *, min_length: int = 0) -> Iterator[tuple[Prefix, int]]
                                                     # every logical prefix with hot_count > 0 and length >= min_length, any order
    def edges(self) -> Iterator[Edge]                # every materialized node with hot_count > 0 or pinned, pre-order (decision 3)
    def set_local_metadata(self, prefix: Prefix, name: str, value: object) -> None   # section 16; validate_metadata_name(name) first
                                                     # (ValueError, nothing stored); materializes the node (decision 5)   -- A2
    def clear_local_metadata(self, prefix: Prefix, name: str) -> None                # validate_metadata_name(name) first (ValueError);
                                                     # no-op if absent; may un-pin and prune   -- A2
    def local_metadata(self, prefix: Prefix) -> Mapping[str, object]                  # {} when none; never inherited values
    def path_metadata(self, ip: Address) -> Iterator[tuple[Prefix, Mapping[str, object]]]   # root -> leaf, non-empty entries only
    @property
    def hot_ip_count(self) -> int: ...               # == hot_count(root)
    @property
    def node_count(self) -> int: ...                 # materialized nodes (the trie_nodes metric)
```

`BinaryTrie` is the oracle: one node per visited bit, `hot_count += 1` at
every node on the path (§10, §39), `-= 1` on removal (§11). It MAY retain
zero-count nodes; its `node_count` and `edges()` are not part of the
equivalence contract. `PatriciaTrie` is production: a run of single-child
nodes is one compressed edge, the root is always materialized, a leaf is the
`/bit_length` node, every other materialized node either has two children
or is pinned by local metadata, and a node whose `hot_count` drops to 0 and
is not pinned is pruned **immediately** — its slot returned to the arena and
its parent merged back into a compressed edge when that leaves the parent
with one child and no metadata (assumptions 4, 5).

> Amended 2026-09-18 (A2): the two metadata setters above validate `name`
> with `validate_metadata_name` before touching the trie; the first version
> of this block said nothing about validation there.

### 3. The logical view, the equivalence contract, and the invariants

§27 makes the *logical* binary trie the model and the Patricia trie a
representation of it. Every logical prefix on a compressed edge exists,
with a `hot_count`: for a materialized node `C` at depth `d'` whose
materialized parent is at depth `d`, each logical prefix of length `L` in
`(d, d')` has `network = C.network` masked to `L` bits and `hot_count =
C.hot_count`. Such a prefix can be `HOT_PREFIX` while not materialized (a
/23 whose only hot /24 has 156 hot IPs is 156/512 = 30 %). The read API
(decision 9) and the publisher (decision 7) therefore answer over the
logical trie, never over materialized nodes alone.

```python
# hammertime.trie.structure.invariants   (Spec: section 11, section 12)
@dataclass(frozen=True, slots=True)
class Edge:
    prefix: Prefix          # the materialized node's own prefix
    parent_length: int      # the materialized parent's prefix length; -1 for the root
    hot_count: int
    pinned: bool

def logical_prefixes(edges: Iterable[Edge], *, min_length: int = 0) -> Iterator[tuple[Prefix, int]]
    # expands each edge to the logical prefixes of lengths (parent_length, prefix.length] with hot_count > 0

def check_invariants(trie: BinaryTrie | PatriciaTrie) -> None   # raises InvariantViolation naming the first offending prefix
```

**The equivalence contract** (`test_patricia_equivalence.py`): after the
same sequence of `add_hot_ip`, `remove_hot_ip`, `set_local_metadata` and
`clear_local_metadata` calls on a `BinaryTrie` and a `PatriciaTrie` of the
same family, every one of `is_hot(ip)`, `hot_count(prefix)`,
`path_counts(ip)`, `longest_match(ip)`, `list(hot_ips())`,
`set(logical_prefixes(min_length=m))`, `local_metadata(prefix)`,
`list(path_metadata(ip))`, `hot_ip_count` and the return value of every
mutating call is equal on both, for every `ip`, `prefix` and `m`.
`node_count` and `edges()` may differ (that is the point of compression);
`set(logical_prefixes(edges(...)))` must not.

**The invariants** `check_invariants` enforces:

| # | Holds for | Statement |
| --- | --- | --- |
| I1 | both | `hot_count(node) == hot_count(child0) + hot_count(child1)` for every materialized node that is not a leaf, where an absent child counts 0 (§12) — for Patricia the children are the materialized children, which is equivalent because every compressed-away node has exactly one logical child with the same count |
| I2 | both | a leaf (`prefix_length == bit_length`) has `hot_count ∈ {0, 1}` (§12) |
| I3 | both | `hot_count >= 0` at every node (§11) |
| I4 | both | `hot_count(root) == hot_ip_count == len(list(hot_ips()))` |
| I5 | both | every child's `network` agrees with its parent's on the parent's `prefix_length` bits, its `prefix_length` is greater, and the bit at the parent's `prefix_length` selects the slot it hangs from |
| I6 | Patricia | every non-root node has `hot_count > 0` or is pinned (immediate pruning) |
| I7 | Patricia | every non-root node with exactly one child is pinned (path compression) |
| I8 | Patricia | `node_count == arena.live_count`, and every live arena slot is reachable from the root (no orphans) |
| I9 | Patricia | `node_count <= 2 * hot_ip_count + pinned_count + 1` |

The `hammertime-testkit` helpers are the same checks written against a
structural protocol, so they need no import from the service:

```python
# hammertime.testkit.invariants   (Spec: section 12, section 38)
class HotCountTrie(Protocol):     # satisfied structurally by BinaryTrie and PatriciaTrie
    family: AddressFamily
    @property
    def hot_ip_count(self) -> int: ...
    def hot_ips(self) -> Iterator[Address]: ...
    def hot_count(self, prefix: Prefix) -> int: ...
    def edges(self) -> Iterator[Any]: ...      # items with .prefix, .parent_length, .hot_count, .pinned

def assert_hot_count_consistent(trie: HotCountTrie) -> None        # I1, I2, I4 recomputed bottom-up from hot_ips()
def assert_no_negative_counts(trie: HotCountTrie) -> None          # I3 over edges()
def assert_attribute_records_consistent(trie: HotCountTrie, records: Iterable[Address]) -> None
                                                                    # section 46.5: {a for a in records if a.family is trie.family} == set(hot_ips())
def assert_hysteresis_holds(history: object) -> None               # unchanged stub; not M5's (section 38 is covered by tests/property/test_state_machine.py)
```

### 4. Property testing is a Hypothesis state machine over both tries

`tests/property/test_trie_properties.py` drives a
`hypothesis.stateful.RuleBasedStateMachine` whose rules are `add_hot_ip`,
`remove_hot_ip` (biased towards addresses already hot, so removal and
oscillation are exercised), `set_local_metadata`, `clear_local_metadata`,
against a `BinaryTrie` and a `PatriciaTrie` of one family in lock-step, with
`@invariant()` methods running `check_invariants` on both, the equivalence
contract of decision 3, and `node_count <= 2 * hot_ip_count + pinned + 1`.
Hypothesis 6.168.0 runs an `@invariant()` "after every rule" (installed
`hypothesis/stateful.py` line 1114) and exposes the machine as a test through
its `.TestCase` attribute, whose default settings already disable the
deadline (line 504). The strategies live in `hammertime.testkit.generators`
(`addresses(family)`, `prefixes(family)`, `hot_ip_streams(family)`), so the
service's own tests can reuse them.

### 5. Prefix metadata: stored where declared, combined on lookup, pins its node

```python
# hammertime.trie.metadata.local   (Spec: section 16, section 17)
METADATA_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
def validate_metadata_name(name: str) -> None          # ValueError otherwise

@dataclass(frozen=True, slots=True)
class Policy:                 # section 16's "priority/override" example
    priority: int
    value: object

# hammertime.trie.metadata.combine   (Spec: section 16)
class Combiner(Protocol[T]):
    def combine(self, inherited: T, local: T) -> T: ...   # `inherited` is the ancestor side, `local` the more specific

class SetUnion:            # combine(A, B) = frozenset(A) | frozenset(B)         associative, commutative
class BitmaskOr:           # combine(A, B) = A | B  over ints                    associative, commutative
class PriorityOverride:    # combine(A, B) = the higher Policy.priority; a tie is won by `local`   associative, NOT commutative

class MetadataRegistry:
    def register(self, name: str, combiner: Combiner[Any]) -> None   # ValueError on a duplicate name or an invalid name
    def combiner(self, name: str) -> Combiner[Any]                    # KeyError if unregistered; returns the very object register() was given (identity)
    def combine_path(self, path: Iterable[Mapping[str, object]]) -> dict[str, object]
        # every name that appears at any level MUST be registered, else KeyError -- including a name present at exactly
        # one level (A1). A *registered* name present at only one level is returned as-is (its combiner is not called);
        # a registered name present at several levels is folded root-first with its combiner.

def effective_metadata(trie: BinaryTrie | PatriciaTrie, ip: Address, registry: MetadataRegistry) -> dict[str, object]
    # registry.combine_path(metadata for _, metadata in trie.path_metadata(ip))   -- section 16's definition, never materialized
```

`set_local_metadata(prefix, name, value)` stores `value` on the node for
`prefix` and nowhere else (§17): a lookup at a descendant accumulates it
through `path_metadata`; `local_metadata(descendant)` never shows it. On the
Patricia trie the call **materializes** the prefix if it lies on a
compressed edge (splitting the edge) and **pins** the node: a pinned node is
never pruned and never merged away, whatever its `hot_count`, because
prefix metadata's lifetime is independent of hot state (§46.6).
`clear_local_metadata` that empties the node un-pins it, and the node is
then pruned or merged exactly as if its last hot descendant had just left.
Both setters validate `name` against `METADATA_NAME` first and raise
`ValueError` without touching the trie (A2): the trie is the store of
record for names, and a name the registry could never accept must not be
storable.

> Amended 2026-09-18 (A1, A2): `combine_path`'s comment in the block above
> was "fold root-first; a name present at only one level is returned as-is;
> a name with no combiner is a KeyError", which left the one-level
> unregistered case undetermined; it is now a `KeyError`. The paragraph's
> last sentence (name validation in the setters) was added.

Nothing in v1 declares prefix metadata: no event carries it, no endpoint
sets it, and `GET /ip` does not return it (assumption 10). The module exists so that
the structure honours §16/§17 from the start and so that M6's snapshot has
a defined thing to persist (decision 11).

### 6. Per-IP attributes live in `IpAttributeStore`, keyed by address, with the transition's `window_count`

```python
# hammertime.trie.metadata.ip_attributes   (Spec: section 46.5, section 46.7, section 46.8)
DEFAULT_ATTRIBUTES: Final[Mapping[str, object]] = MappingProxyType({"attributes_version": 1})   # section 46.5: absent == this

@dataclass(frozen=True, slots=True)
class HotIpRecord:
    attributes: Mapping[str, object]   # the event's document verbatim, or DEFAULT_ATTRIBUTES; never interpreted (section 46.2)
    window_count: int                  # the HotIpAdded's window_count -> GET /ip request_count (ADR-0010 decision 4)

class IpAttributeStore:
    def __init__(self) -> None: ...
    def put(self, ip: Address, record: HotIpRecord) -> None     # insert or replace (section 46.5)
    def delete(self, ip: Address) -> bool                       # True iff a record existed
    def get(self, ip: Address) -> HotIpRecord | None: ...
    def __contains__(self, ip: Address) -> bool: ...
    def __len__(self) -> int: ...
    def count(self, family: AddressFamily) -> int: ...
    def records(self) -> Iterator[tuple[Address, HotIpRecord]]  # IPv4 records first, then IPv6; within a family ascending by
                                                                # Address.value (A3); M6's snapshot source
    @property
    def serialized_bytes(self) -> int: ...   # sum of len(json.dumps(attributes, separators=(",", ":")).encode()) -- ip_attribute_bytes
```

The store validates nothing: the codec has already enforced
`schemas/ip_attributes.v1.json` on decode (`_validate_attributes`), and §46.2
forbids interpreting `x_` keys or a higher `attributes_version`. It stores
the mapping it is given and `GET /ip` returns that mapping unchanged.
`DEFAULT_ATTRIBUTES` is annotated `Final[Mapping[str, object]]` and bound to
a `MappingProxyType`, so item assignment is a mypy `[index]` error and a
runtime `TypeError` (A5).

> Amended 2026-09-18 (A3): `records()`'s comment was "ascending by (family,
> value)" without saying which family sorts first; IPv4 does.

### 7. `TrieState.apply` is the one atomic step; four outcomes; what `event_sequence` counts

```python
# hammertime.trie.state   (Spec: section 12, section 28, section 46.5)
class ApplyOutcome(StrEnum):
    ADDED = "added"          # COLD -> HOT: path += 1, record inserted, sequence += 1, stats published
    REMOVED = "removed"      # HOT -> COLD: path -= 1, record deleted,  sequence += 1, stats published
    REPLACED = "replaced"    # HotIpAdded for an IP already HOT: record replaced (section 46.5), counts unchanged, sequence += 1, no stats
    NOOP = "noop"            # HotIpRemoved for an IP not held: nothing changes (section 11), sequence unchanged, no stats
    MALFORMED = "malformed"  # never returned by TrieState.apply; a worker outcome (decision 8)

@dataclass(frozen=True, slots=True)
class Applied:
    ip: Address
    outcome: ApplyOutcome
    event_sequence: int                  # the trie's counter after this event
    path_counts: tuple[int, ...] | None  # ADDED/REMOVED: hot_count of every ancestor /0../bit_length after the update; else None

class TrieState:
    def __init__(self, *, config: DetectionConfig, event_sequence: int = 0) -> None: ...
    def trie(self, family: AddressFamily) -> PatriciaTrie: ...
    attributes: IpAttributeStore
    @property
    def event_sequence(self) -> int: ...
    @property
    def config(self) -> DetectionConfig: ...
    def set_config(self, config: DetectionConfig) -> None    # decision 10; synchronous reference swap
    def apply(self, payload: HotIpAdded | HotIpRemoved) -> Applied     # a plain `def`: no await anywhere inside
    def restore(self, records: Iterable[tuple[Address, HotIpRecord]], *, event_sequence: int) -> None
                                                            # M6's seam: rebuild from a snapshot's records; only on an empty state
    @property
    def hot_ip_count(self) -> int: ...                       # summed over families
```

`apply` performs, in one synchronous section: for `HotIpAdded`, `added =
trie.add_hot_ip(ip)`, `attributes.put(ip, HotIpRecord(attributes or
DEFAULT_ATTRIBUTES, window_count))`, outcome `ADDED` if `added` else
`REPLACED`; for `HotIpRemoved`, `removed = trie.remove_hot_ip(ip)`, `deleted
= attributes.delete(ip)`, and `removed != deleted` is an
`InvariantViolation` (the two structures are updated in the same step and
can never disagree), outcome `REMOVED` if `removed` else `NOOP`. Attributes
on a `HotIpRemoved` are never stored (§46.5). After every `apply`,
`attributes.count(f) == trie(f).hot_ip_count` for each family — §46.5's
derived invariant.

**Atomicity (§28) is by construction, not by copy-on-write.** The service is
one asyncio event loop: the writer (`TrieWorker.handle`) and every reader
(the FastAPI handlers of decision 9) are coroutines on that loop, and a
coroutine cannot be interrupted except at an `await`. `TrieState.apply` is a
synchronous function with no `await` inside it, so no reader can run between
its first mutation and its last; every read handler builds its whole
response from `TrieState` inside one synchronous section for the same
reason, and is declared `async def` so FastAPI runs it on the loop rather
than in a worker thread. The stub's versioned snapshot pointer is not built
(assumption 2): it buys nothing on one loop and would cost a copy of up to 128 nodes
per transition.

**What "applied" means, and therefore what `event_sequence` counts:** an
event is *applied* iff it changed the trie's state — the `hot_count` path,
or the attribute record. `ADDED`, `REMOVED` and `REPLACED` are applied and
increment `event_sequence`; `NOOP` does not. `PrefixStatsChanged` is
published only for `ADDED` and `REMOVED`, because only they change a prefix's
statistics (epic #10: "on prefix-level state changes"); this answers the
question ADR-0011's Consequences left to this epic. A byte-identical
redelivery of a `HotIpAdded` (same `event_id`) is a `REPLACED` whose record
is identical, and a redelivered `HotIpRemoved` is a `NOOP`; neither publishes
anything, so the property `docs/spec/integration-scenarios.md` §3 step 4b
asserts holds (assumption 7).

**Per-IP order, gaps, cross-IP order.** ADR-0001 Amendment 1 clause 3 gives
the trie every IP's transitions in emission order, possibly with gaps, never
reordered; clause 4 gives no order across IPs. The trie therefore keeps no
per-IP sequence and rejects nothing as out of order: a gap manifests as a
`REPLACED` or a `NOOP`, both absorbed above.

### 8. The worker: consumer group, replay to the log end, commit cadence, malformed events

```python
# hammertime.bus.interface   (additions)
class MessageBus(Protocol):                          # what the aggregator's worker.py already declares privately; now shared
    def producer(self) -> Producer: ...
    def consumer(self, group_id: str) -> Consumer: ...

class Consumer(Protocol):
    async def end_offsets(self, topic: str) -> Mapping[tuple[str, int], int]: ...
        # for every partition of `topic` this consumer currently holds: the offset the next published record will get
        # (one past the last record; 0 for an empty partition). Requires an active subscription to `topic`.
        # MemoryConsumer: {(topic, 0): len(bus._logs[topic])}; ValueError if not subscribed.
        # KafkaConsumer: aiokafka end_offsets(...) over assignment() filtered to `topic` (installed aiokafka 0.14.0,
        # consumer.py line 983: "the offset of the upcoming message, i.e. the offset of the last available message + 1").

# hammertime.bus.kafka   (addition)
class KafkaBus:                                       # MessageBus over one KafkaProducer and N KafkaConsumers
    def __init__(self, brokers: str) -> None: ...
    def producer(self) -> Producer: ...
    def consumer(self, group_id: str) -> Consumer: ...
    async def start(self) -> None     # connect_with_retry("bus", ..., transient=(OSError, KafkaConnectionError)) for each client
    async def close(self) -> None
```

```python
# hammertime.trie.worker   (Spec: section 28, section 32, section 47.2)
CONSUMER_GROUP = "hammertime-trie"          # ADR-0009 decision 9
TRIE_AGENT_ID = "trie-primary"              # ADR-0003 amendment 1
DEFAULT_COMMIT_INTERVAL_S = 1.0

class TrieWorker:                           # satisfies AssignmentListener
    def __init__(
        self, *, bus: MessageBus, state: TrieState, publisher: PrefixStatsPublisher, clock: Clock, metrics: TrieMetrics,
        commit_interval_s: float = DEFAULT_COMMIT_INTERVAL_S,
        replay_start: Callable[[frozenset[tuple[str, int]]], Mapping[tuple[str, int], int]] | None = None,   # M6's seam
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None: ...
    @property
    def config(self) -> DetectionConfig: ...                 # state.config
    @property
    def partitions(self) -> frozenset[tuple[str, int]]: ...  # held right now
    @property
    def handled_positions(self) -> Mapping[tuple[str, int], int]: ...   # next offset to read per held partition; M6's snapshot position
    async def start(self) -> None      # subscribe, seek, replay to the end, commit; returns when caught up
    async def run(self) -> None        # consume until stop(); commit every commit_interval_s of wall time
    async def stop(self) -> None       # stop fetching, finish the in-flight message, flush, commit; idempotent, safe before start()
    async def handle(self, message: ConsumedMessage) -> ApplyOutcome
    async def apply_config(self, config: DetectionConfig) -> None    # unconditional (ADR-0011 A12 pattern); state.set_config under the lock
    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None
    async def on_revoked(self, partitions: frozenset[tuple[str, int]]) -> None
```

**`start()`** (ADR-0009 decision 4: ready = "replayed to the log end as it
stood when `start()` began"):

1. `stream = await consumer.subscribe(HOT_IP.name, partitions=None, listener=self)` — group-managed; `on_assigned` records the held partitions. Log `INFO event=partitions_assigned partitions=[...]`.
2. `starts = replay_start(partitions)` — **M5: `{p: 0 for p in partitions}`**, the beginning of every partition, because the trie is derived state (§32) and without a snapshot the only correct reconstruction is a full replay. M6 supplies the snapshot's positions here. `await consumer.seek(topic, p, starts[p])` for each. The consumer group's *committed* offsets are never used to choose where to resume (assumption 8).
3. `ends = await consumer.end_offsets(HOT_IP.name)` — captured **after** the seek, so a message published during the seek is inside the replay, not after it.
4. Log `INFO event=replay_started partitions=N events=sum(ends[p] - starts[p])`. Read from `stream` and `handle()` each message until, for every held partition `p`, `handled_positions[p] >= ends[p]` (a partition with `ends[p] == starts[p]` is caught up before the first read). Because every partition below its end still has a record in the log, this loop never blocks on an empty log.
5. `await commit_handled()`; log `INFO event=replay_complete events=<applied+noop+malformed count> seconds=<wall time>`; set `trie_recovery_seconds`. Return.

Replayed events go through the same `handle()` as live ones, so a state
change replayed at start **re-publishes** its `PrefixStatsChanged` with the
same `sequence`, `subject` and therefore the same `event_id` as before the
restart. That is deliberate: a stats event applied before a crash but not
flushed can only be recovered by re-emitting, and the detector applies
"latest known stats per prefix" (ADR-0010 decision 5), which is idempotent
under a same-`event_id` duplicate. In M5 a restart re-emits the whole
history; M6's snapshot bounds the re-emission to the post-snapshot tail
(decision 11; assumption 9).

**`handle(message)`**, under one `asyncio.Lock` shared with `apply_config`,
`stop` and `on_revoked`:

1. `decode(message.value)`. `CodecError` -> `MALFORMED`: `WARNING
   event=malformed_hot_ip_event topic partition offset reason`,
   `trie_updates{family="unknown", outcome="malformed"} += 1`, and if the
   error is an `AttributesError` (a `CodecError` subclass the codec raises
   from `_validate_attributes`; a core addition this ADR requires) also
   `attributes_rejected += 1` (§46.8). The payload MUST be a `HotIpAdded`
   or `HotIpRemoved`; `message.key` MUST equal `str(payload.ip).encode()`
   (per-IP ordering rests on the key, ADR-0001 Amendment 1 clause 3); the
   envelope `subject`, when not `None`, MUST equal `str(payload.ip)`.
   Anything else is `MALFORMED` too. A malformed message is dropped —
   never applied, never diverted — and its position is marked handled, so a
   poison record never stops the consumer (ADR-0011 assumption 10's
   reasoning).
2. `applied = state.apply(payload)` — synchronous (decision 7).
3. If `ADDED`/`REMOVED`: `await publisher.publish(applied,
   config_version=state.config.config_version)` (ADR-0010 decision 3:
   published before the next hot-IP event is applied — the lock guarantees
   it); `observe("hot_transition_to_prefix_update_latency", clock.now() -
   floor(payload.timestamp.timestamp()))`.
4. `trie_updates{family=payload.ip.family.value, outcome} += 1`; mark
   `handled_positions[(topic, partition)] = offset + 1`; return the outcome.

A publish failure propagates out of `handle()` and therefore out of
`run()`: the runner logs `run_exited` and exits 1 (ADR-0009 decision 5 step
7). Stats that cannot reach the log must not be silently skipped, and the
next start replays and re-emits them (assumption 11).

**Commit** — `commit_handled(partitions=None)`: `producer.flush()`, then
`consumer.commit({(topic, p): handled_positions[p] ...})` over the given
partitions (default all held) omitting any with nothing handled; flush
first, always (ADR-0009 Amendment 2 A11, ADR-0010 decision 3). Called every
`HAMMERTIME_TRIE_COMMIT_INTERVAL_S` of wall time (checked after each
message), at the end of `start()`, in `on_revoked` for the revoked
partitions, and in `stop()`. Committing the handled position rather than
the consumed one keeps a message fetched-but-unhandled at `stop()` out of
the commit (ADR-0011 A20's reasoning), even though the trie's commits are
informational for resume (assumption 8) — they are what an operator's consumer-lag
dashboard reads, so they must not lie.

**`on_revoked(partitions)`**: `commit_handled(partitions)`, drop their
handled positions, and log `WARNING event=partitions_revoked
partitions=[...]` when the set is non-empty — on a single-writer topic a
revocation means a second `hammertime-trie` member has joined, which
ADR-0001 forbids and this design does not fence (assumption 12). **`on_assigned`**
records the partitions; a re-assignment after a rebalance resumes at the
broker's committed offset, which is at or before the handled position, so
the only effect is redelivery of an already-applied tail (absorbed by
decision 7).

**`stop()`**: set the stop flag, take the lock (so the in-flight message is
finished), `commit_handled()`, release. No snapshot in M5 (decision 11).

### 9. `hammertime.core.state.prefix`, the read model, and the FastAPI app

```python
# hammertime.core.state.prefix   (Spec: section 13, section 38; ADR-0010 decision 1)
def evaluate_prefix_state(hot_count: int, capacity: int, config: DetectionConfig) -> PrefixState:
    """HOT_PREFIX iff hot_count >= config.minimum_hot_ips
       and Fraction(hot_count, capacity) >= Fraction(config.minimum_hot_ratio); else NORMAL.
       ValueError if capacity < 1, hot_count < 0, or hot_count > capacity (A4).
       Never BOT_NETWORK (ADR-0010 decision 2)."""
```

> Amended 2026-09-18 (A4): the docstring's `ValueError` clause was
> "ValueError if capacity < 1 or hot_count < 0."; `hot_count > capacity` —
> impossible for a §12-consistent trie, so only reachable through a corrupt
> or forged `PrefixStatsChanged` — is now rejected too rather than given a
> verdict.

`Fraction(config.minimum_hot_ratio)` is the float's exact binary value —
the same comparison Python performs for `Fraction >= float` (installed
`fractions.py`, `_richcmp` lines 945-949: `op(self, self.from_float(other))`)
— so `hot_count / capacity == minimum_hot_ratio` qualifies exactly when the
configured ratio is representable (0.125, 0.5), and a ratio like 0.10 that
is not representable compares against what the operator's JSON actually
parsed to. Both services get the identical verdict for the identical
inputs (ADR-0010 decision 1); `docs/spec/README.md` already maps §13/§38 to
this module.

```python
# hammertime.trie.query.views   (Spec: section 22, section 29, section 31)
MATCHED_PREFIX_LENGTHS: Final[Mapping[AddressFamily, tuple[int, ...]]] = {IPV4: (8, 16, 24), IPV6: (104, 112, 120)}

class QueryError(Exception):           # rendered as 400 {"detail": <reason>}; reason is a fixed string, never echoing input
    reason: str

def render_timestamp(epoch_seconds: int) -> str      # RFC 3339 UTC with a Z suffix, e.g. "2026-09-14T10:05:00Z"

class ReadModel:
    def __init__(self, *, state: TrieState, clock: Clock, min_prefix_length: Mapping[AddressFamily, int], metrics: TrieMetrics) -> None: ...
    def prefix(self, cidr: str) -> dict[str, object]                 # GET /prefix/{cidr}
    def ip(self, addr: str) -> dict[str, object]                     # GET /ip/{addr}
    def hot_prefixes(self, *, minimal: bool) -> dict[str, object]    # GET /prefixes/hot
    def parse_prefix(cidr: str) -> Prefix        # static; QueryError("prefix must be address/length"), ("invalid address"),
                                                 # ("prefix length out of range"), ("host bits set")
    def parse_address(addr: str) -> Address      # static; QueryError("invalid address")

# hammertime.trie.query.app   (Spec: section 29, section 31, section 47.2)
def create_app(read_model: ReadModel, readiness: Readiness, *, render_metrics: Callable[[], bytes] | None = None) -> FastAPI
```

Every `ReadModel` method is synchronous and reads every field it returns
from `TrieState` without yielding (decision 7); the FastAPI handlers are
`async def` one-liners around them. Rules, all from
`docs/protocol/read-api-v1.md` unless marked *(this ADR)*:

* **Parsing.** `{cidr}` is declared `{cidr:path}` because it contains a
  `/`: Starlette's `path` convertor matches `.*` where the default `str`
  convertor matches `[^/]+` (installed `starlette/convertors.py` lines 21
  and 34; Starlette's routing document: the `path` convertor "returns the
  rest of the path, including any additional `/` characters"). *(this
  ADR)* The `/length` part is required — a bare address is
  `400 prefix must be address/length` — and the length must match
  `^[0-9]{1,3}$` before `Prefix.parse` sees it. An address carrying an IPv6
  scope id (`fe80::1%eth0`, which CPython's `IPv6Address` accepts via
  `_split_scope_id`, `ipaddress.py` line 1886) is `400 invalid address`:
  the trie is keyed on bits and a zone has none. `Address.parse` /
  `Prefix.parse` do the rest; `InvalidAddressError`/`InvalidPrefixError`
  map to the fixed reasons above. Text is canonicalised by `str(Address)`
  — CPython's RFC 5952-style compression (`_compress_hextets`, line 1782;
  lowercase hex, line 1850) — so `10.020.030.000/24` is not accepted
  (CPython rejects leading zeros) and `2001:DB8::/32` is answered as
  `2001:db8::/32`.
* **`GET /prefix/{cidr}`**: `{"prefix": str(prefix), "hot_ips":
  hot_count(prefix), "capacity": prefix.capacity(), "hot_ratio":
  prefix.hot_ratio(hot_count), "state": evaluate_prefix_state(...).value,
  "as_of", "event_sequence", "config_version"}`. A prefix with no node is
  the zero-valued `NORMAL` answer, not 404.
* **`GET /ip/{addr}`**: `state` is `"HOT"` iff `trie.is_hot(ip)`;
  `request_count` is the record's `window_count` (0 while COLD);
  `attributes` present iff HOT, the stored mapping verbatim;
  `matched_prefixes` are the ancestors at `MATCHED_PREFIX_LENGTHS[family]`,
  shortest first, each a `GET /prefix` body minus the envelope. *(this ADR)*
  The IPv6 lengths mirror the IPv4 ones by capacity (assumption 6).
* **`GET /prefixes/hot[?minimal=true]`**: every logical prefix (decision 3)
  whose length is in `[min_prefix_length[family], bit_length]` and whose
  state is `HOT_PREFIX`, over both families, ordered by length descending
  then network ascending (IPv4 before IPv6 at equal length *(this ADR)*).
  With `minimal=true`, only those with no `HOT_PREFIX` logical descendant
  (§31). Two consequences of decision 3 that make this cheap and that
  test-author can pin: a compressed-away logical prefix can appear in the
  full list; it can **never** appear in the minimal list, because its one
  logical child has the same `hot_count` and half the capacity and so also
  qualifies. So the minimal set is a subset of the materialized nodes and
  `minimal(C) ⇔ HOT_PREFIX(C) and no materialized descendant of C is
  HOT_PREFIX`. *(this ADR)* Lengths below the family's minimum reported
  length are excluded so the list and the detector's `GET /detections`
  range over the same prefixes (M7 hears of nothing shorter). An
  unparseable `minimal` is FastAPI's default `422`.
* **Envelope** on every body: `as_of = render_timestamp(clock.now())`,
  `event_sequence = state.event_sequence`, `config_version =
  state.config.config_version` — all read in the same synchronous section
  as the body.
* **Not ready**: a route-level dependency raises `ServiceNotReady`, rendered
  by `not_ready_response` — ingest's `routes.py::_require_ready` pattern.
  `/healthz`, `/readyz`, `/metrics` use the core helpers through the same
  raw-header `Response` wrapper ingest uses (`_as_response`), so the bodies
  are byte-identical to the aggregator's pure-ASGI app.
* **Metrics**: each domain request increments
  `prefix_queries{endpoint=prefix|ip|prefixes_hot}` whether or not it is a
  400. `/metrics` renders `render_metrics()` or `b""` (the telemetry epic
  owns exposition, as for the aggregator).

Complexity: `GET /prefix` and `GET /ip` are O(`bit_length`) descents (epic
#10's "without walking the whole subtree"); `GET /prefixes/hot` walks the
materialized nodes once, O(`node_count` + result) — accepted for v1 (assumption 3).

### 10. Configuration: the trie depends on three fields and re-evaluates nothing

The trie reads `minimum_hot_ips` and `minimum_hot_ratio` (the predicate, at
read time) and `config_version` (responses, published envelopes). Nothing
else in `DetectionConfig` affects it. `TrieWorker.apply_config(config)` is
`ConfigPoller`'s `apply` hook (ADR-0009 A5; unconditional, no version
comparison, ADR-0011 A12's pattern): under the lock it calls
`state.set_config(config)`, and that is the whole re-evaluation — with no
cached `prefix_state` (decision 2) there is nothing to recompute, so §34's
rule is met trivially: every `state` read and every event published after
the swap uses the new document, none before it. `TrieService.reload_config()`
is `await poller.poll_once()`. ADR-0010 decision 5's "eagerly for the cached
`prefix_state`" describes a cache this design does not materialize (assumption 3).

### 11. Settings, service, metrics, log records; the M5/M6 boundary

```python
# hammertime.trie.config   (Spec: section 33, section 35, section 47)
@dataclass(frozen=True, slots=True)
class TrieSettings:
    host: str; port: int                       # HAMMERTIME_TRIE_QUERY_BIND, default 0.0.0.0:8081
    detection_config_path: Path                # HAMMERTIME_CONFIG_PATH
    bus_kind: str; bus_brokers: str            # HAMMERTIME_BUS_KIND (kafka|memory), HAMMERTIME_BUS_BROKERS
    snapshot_dir: Path                         # HAMMERTIME_TRIE_SNAPSHOT_DIR, default ./snapshots   (parsed; unused until M6)
    snapshot_interval_s: float                 # HAMMERTIME_TRIE_SNAPSHOT_INTERVAL_S, default 300, > 0 (parsed; unused until M6)
    min_prefix_length: int                     # HAMMERTIME_TRIE_MIN_PREFIX_LENGTH, default 8, in [0, 32]
    min_prefix_length_v6: int                  # HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_V6, default 104, in [0, 128]
    commit_interval_s: float = 1.0             # HAMMERTIME_TRIE_COMMIT_INTERVAL_S, > 0
    config_poll_interval_s: float = 1.0        # HAMMERTIME_CONFIG_POLL_INTERVAL_S, > 0

def load_settings(env: Mapping[str, str] | None = None) -> TrieSettings   # ValueError naming the variable; unknown keys ignored
```

No store setting: the trie has no store (ADR-0009 A12 item 5 binds it only
if it ever gains one).

```python
# hammertime.trie.service   (Spec: section 47)
SERVICE_NAME = "trie"
class TrieService:                # Service (runtime_checkable) + DescribesStartup
    name: str; app: FastAPI
    async def start(self) -> None        # transport.start() (KafkaBus, retried), worker.start() (replay), readiness.mark_ready()
    async def run(self) -> None          # uvicorn on HAMMERTIME_TRIE_QUERY_BIND (lifespan="off", log_config=None, signals left to the runner),
                                         # worker.run(), poller.run(); first to return brings the rest down (aggregator pattern)
    async def stop(self) -> None         # mark_stopping, poller.stop, server.should_exit, worker.stop; idempotent
    @property
    def ready(self) -> bool: ...
    async def reload_config(self) -> DetectionConfig
    def startup_fields(self) -> Mapping[str, object]
        # bus_kind, bus_brokers, config_path, config_version, bind, consumer_group, snapshot_dir, min_prefix_length, min_prefix_length_v6

def build_service(settings: TrieSettings, *, bus: MessageBus | None = None, clock: Clock | None = None) -> TrieService
    # loads the detection config first (ConfigurationError -> exit 2), then TrieState, TrieMetrics (bind_state), PrefixStatsPublisher,
    # TrieWorker, ReadModel, create_app; bus None -> KafkaBus(settings.bus_brokers) if bus_kind == "kafka" else a private InMemoryBus
```

`snapshot_now()` is **not** defined in M5: there is no writer to call. M6
adds it together with `snapshot/writer.py`, and `stop()` gains its final
snapshot then. `.env.example` gains `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH`,
`HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_V6` and `HAMMERTIME_TRIE_COMMIT_INTERVAL_S`;
the two snapshot keys it already carries are accepted and validated so the
compose stack starts unchanged.

```python
# hammertime.trie.metrics   (Spec: section 37, section 46.8)
EVENT_COUNTERS = {
    "trie_updates": ("family", "outcome"),     # family ipv4|ipv6|unknown (unknown only with malformed); outcome = ApplyOutcome values
    "prefix_queries": ("endpoint",),           # prefix|ip|prefixes_hot
    "attributes_rejected": (),
}
DERIVED = {                                    # computed on get() from the bound TrieState, never stored
    "trie_nodes": ("family",),                 # trie(family).node_count
    "hot_ip_count": ("family",),               # trie(family).hot_ip_count
    "ip_attribute_records": (),                # len(state.attributes)  -- MUST equal the sum of hot_ip_count over families (section 46.8)
    "ip_attribute_bytes": (),                  # state.attributes.serialized_bytes
}
GAUGES = {"trie_recovery_seconds": ()}         # set() once at the end of start()
OBSERVED = {"hot_transition_to_prefix_update_latency": ()}   # observe(); get() returns the sum, get_count() the count

class TrieMetrics:
    def bind_state(self, state: TrieState) -> None
    def increment(self, name: str, **labels: object) -> None
    def set(self, name: str, value: float, **labels: object) -> None
    def observe(self, name: str, value: float, **labels: object) -> None
    def get(self, name: str, **labels: object) -> int | float          # 0 for an untouched series / unbound state
    def get_count(self, name: str, **labels: object) -> int
```

The same strictness as `AggregatorMetrics` (ADR-0011 A13): an unknown name
or a wrong label *set* is a `ValueError`; label values are `str()`-compared.

Structured log events (ADR-0009 A7 conventions; the runner's own records
are unchanged): `partitions_assigned`, `partitions_revoked` (WARNING),
`replay_started`, `replay_complete`, `malformed_hot_ip_event` (WARNING).
`config_applied`/`config_rejected` are the poller's.

**M5/M6 boundary.** M6 (epic #11) adds `snapshot/writer.py`,
`snapshot/loader.py`, `snapshot_now()`, the periodic writer on
`snapshot_interval_s` under `snapshot_dir`, the final snapshot in `stop()`
after the commit (ADR-0009 decision 7), and `test_snapshot.py`. It builds on
three seams this ADR fixes and M6 MUST NOT redesign: `TrieState.restore`
and `IpAttributeStore.records()` (what a snapshot holds: every
`HotIpRecord` including `window_count`, every pinned node's
`local_metadata` via `edges()`/`local_metadata()`, `event_sequence`,
`config_version`), `TrieWorker.handled_positions` (the per-partition
positions a snapshot records — §33's single "event sequence number" is not
a Kafka offset, ADR-0010 Consequences), and `TrieWorker(replay_start=...)`
(where the loader tells the worker to seek). A periodic snapshot MUST be
taken only after a `commit_handled()` — hence after a flush — so its
positions never cover an event whose stats have not reached the log
(ADR-0009 A11's named follow-up, answered here). Until M6 lands, every start
replays `hammertime.hot-ip.v1` from the beginning (decision 8 step 2).

### 12. The M5/M7 boundary: what the detector may rely on

The detector (M7) consumes `hammertime.prefix-stats.v1` and may rely on: one
`PrefixStatsChanged` per logical ancestor of length in `[min_prefix_length
(family), bit_length]` per `ADDED`/`REMOVED` (25 per family at the defaults);
envelope `agent_id == "trie-primary"`, `sequence` = the trie's
`event_sequence` (strictly increasing, **not dense** — `REPLACED` consumes
a number and emits nothing), `event_type == "PrefixStatsChanged"`,
`subject == payload.prefix` (so the 25 events of one transition have
distinct `event_id`s; without `subject` they would collide on
`(agent_id, sequence, event_type)` — the envelope docstring's remark that
`PrefixStatsChanged` does not set `subject` predates any producer and is
superseded), `config_version` = the version in force when the transition
was applied; payload `prefix` (canonical text), `hot_count`, `capacity`
(decimal string on the wire, `int` in the dataclass), `hot_ratio`
(`Prefix.hot_ratio`, now always set — ADR-0010 Consequences; an optional
`float | None` field added to the dataclass and codec), `sequence`,
`timestamp` (the trie's clock at publish). Stats are keyed by prefix text
(`topics.py`). After a trie restart the detector will see re-emitted stats
with `event_id`s it has already applied (decision 8) and MUST treat them
idempotently ("latest known stats per prefix", ADR-0010 decision 5 — a
requirement on M7, not a verified property). `GET /prefixes/hot` and
`GET /detections` range over the same prefixes and use the same predicate
(`hammertime.core.state.prefix`, built here; `rules/baseline.py` is M7's
adapter over it).

## Assumptions

Each of these is a judgment call not dictated by the epics, the spec or a
prior ADR. Push back on them individually.

1. **One process for both address families; one `event_sequence`.**
   ADR-0001 requires at most one writer per family and does not forbid one
   process being both. Splitting by family would make two consumers read
   the whole topic and discard half, for a partition of memory nobody has
   asked for. The single counter follows ADR-0010 decision 3 over the
   ADR-0001 Amendment 1 assumption bullet; the detector's `event_sequence`
   needs one counter to be meaningful.
2. **Atomicity by a synchronous apply on one event loop, not a versioned
   snapshot pointer.** The stub's mechanism is right for a multi-threaded
   runtime; this one has none (uvicorn, the consumer and the handlers share
   the loop). The obligation moves to two rules a reviewer can check: no
   `await` inside `TrieState.apply` or inside a `ReadModel` method, and
   `async def` handlers. If a thread ever reads the trie (a `def` handler,
   a snapshot writer in a thread), this assumption fails and copy-on-write
   is the fix.
3. **No cached `prefix_state`; `GET /prefixes/hot` walks the materialized
   nodes.** Chosen because a cache is a second derived structure that
   config changes must invalidate and M6 must persist or rebuild, for a
   query that is O(hot set) in its output anyway. An incremental
   `HOT_PREFIX` set (update the ancestors on the path per apply, full walk
   on config change) is the upgrade if the walk ever shows on
   `prefix_queries` latency; the read-API contract does not change.
4. **Prune immediately; reuse arena slots.** §11 offers "retain structural
   nodes" or "arena". Retaining zero-count nodes bounds memory by the
   number of distinct IPs ever hot — unbounded over days. Immediate pruning
   plus a free list bounds live nodes by `2n + p + 1` (I9) and total slots
   by the peak. The free list is never shrunk (epic #8's "without
   fragmentation blowing up memory" is met by reuse, not by compaction); a `compact()`
   is a later addition if a deployment's peak is far above its steady
   state.
5. **Pinned nodes.** §46.6 says prefix metadata's lifetime is independent
   of hot state; a Patricia node is the only place to keep it, so a node
   with metadata must survive pruning and compression, and setting
   metadata on a compressed-away prefix must materialize it. The
   alternative — a separate `prefix -> metadata` map — would put `path_metadata`
   off the O(bit_length) descent and make "which prefixes on this path
   carry metadata" a second lookup per level.
6. **IPv6 minimum reported length 104; matched lengths (104, 112, 120).**
   ADR-0010 left the IPv6 default undefined. 104 mirrors IPv4's /8 by
   capacity (2^24), so both families report 25 levels per transition and
   the same capacity range; the `matched_prefixes` lengths mirror 8/16/24
   the same way. The conventional IPv6 boundaries (/32, /48, /64) were
   rejected because at any plausible `minimum_hot_ratio` a /64 (1.8e19
   addresses) can never qualify, and reporting them would add 40 always-
   `NORMAL` messages per transition. §43's "v1 is IPv4-only" is honoured in
   spirit — the defaults make IPv6 prefix detection inert — without
   dropping IPv6 events the aggregator already emits (ADR-0011 assumption
   20). Push back if an operator-visible IPv6 length is preferred.
7. **`REPLACED` increments `event_sequence` and emits nothing; `NOOP`
   does neither.** "Applied" is defined as "changed the trie's state",
   and a replaced record is a change (`request_count` on `GET /ip` moves).
   Alternatives: (a) count only `hot_count` changes — then `GET /ip` can
   change under a constant `event_sequence`; (b) emit stats for `REPLACED`
   — 25 messages saying nothing changed, per redelivery. A dense
   `sequence` was never promised (ADR-0001 Amendment 1 clause 2 says the
   same of shard sequences).
8. **Committed offsets are never the resume position.** Without a
   snapshot, resuming an *empty* trie at a committed offset would be
   silently wrong, and with one the snapshot's own positions are
   authoritative (M6). Commits are still made, flush-first, of handled
   positions, because consumer lag is how an operator sees the trie fall
   behind (ADR-0001 Consequences name that signal) and because ADR-0009
   decision 7's "snapshot position not ahead of the committed one" needs
   a commit to be relative to.
9. **Replay re-emits `PrefixStatsChanged`.** The alternative — a silent
   replay — would lose the stats of any event applied but not flushed
   before a crash, which is exactly the window flush-before-commit exists
   to make recoverable. In M5 the cost is a full re-emission per restart;
   accepted because M6 bounds it and because the detector must be
   idempotent under same-`event_id` duplicates regardless (ADR-0003
   Amendment 2's transport note applies to every topic).
10. **Prefix metadata has no producer, no API and no read-API field in
    v1.** ADR-0005's Consequences say `GET /ip` returns both kinds of
    metadata "separately keyed"; `read-api-v1.md` (ADR-0010) lists no such
    field, and nothing can set one. Adding `metadata` to `GET /ip` is an
    additive protocol change for the change that introduces a way to
    declare prefix metadata; it is not made here.
11. **A publish failure takes the process down.** Same stance as ADR-0011
    assumption 7 for the aggregator's store: skipping stats silently would
    leave the detector permanently behind for that prefix; exit 1 and a
    replay re-emit them.
12. **A second `hammertime-trie` member is not fenced.** ADR-0001 makes
    single membership an operator invariant; the `partitions_revoked`
    warning is the only detection. A fencing scheme is a design pass of
    its own (ADR-0011 A2 records the same gap for aggregators).
13. **`MessageBus` and `KafkaBus` move into `hammertime-bus`; the
    aggregator keeps its private copies for now.** Two services declaring
    the same protocol and the same adapter is the duplication a reviewer
    would flag; putting the shared shape in the bus package is additive.
    Migrating the aggregator to `KafkaBus` is a follow-up, not M5.
14. **`Consumer.end_offsets(topic)` is added to the bus protocol.** The
    only deterministic way to know "the log end as it stood when `start()`
    began" (ADR-0009 decision 4). A time-based "no message for N ms"
    heuristic was rejected: it cannot be made exact on the in-process
    harness and would make readiness depend on wall time.
15. **`AttributesError(CodecError)`.** §46.8's `attributes_rejected` needs
    the codec to say *why* a document was rejected; matching on the error
    text was rejected as brittle. A subclass changes no caller (every
    `except CodecError` still catches it). The whole event is dropped
    rather than applied with default attributes, because the codec
    validates the envelope as a unit and a partial decode would be a new
    codec mode for a producer bug that §46.9 already forbids.
16. **Key and subject checks on the hot-ip topic.** The key is what per-IP
    ordering rests on (ADR-0001 Amendment 1 clause 3), so a record keyed by
    something other than its IP has lost that guarantee and is treated as
    malformed. `subject` is accepted absent because the codec allows it
    and ADR-0004 named `HotIpAdded` among the types that need not set it,
    even though ADR-0011 decision 4 does set it.
17. **`PrefixStatsChanged.hot_ratio` becomes a real (optional) field.**
    ADR-0010 said the trie "now always sets it" but the dataclass and codec
    never had it. Added as `float | None = None`, encoded when set, decoded
    when present; descriptive only — the predicate uses `hot_count` and
    `capacity` exactly.
18. **400 reasons are fixed strings and never echo the input.** The client
    knows what it sent; echoing a path segment into a JSON body is a log-
    and response-injection surface for no diagnostic gain. The four
    reasons are pinned so tests can match them.
19. **`minimal` parse failure is FastAPI's default 422.** Pinned rather
    than remapped to 400 so the trie behaves like any FastAPI service
    for a query-string type error; `read-api-v1.md` records it.
20. **`GET /prefixes/hot` excludes lengths below the family's minimum
    reported length; IPv4 sorts before IPv6 at equal length.** The first
    keeps the trie's list and the detector's over the same prefixes; the
    second is an arbitrary total order for a list that mixes families
    (`read-api-v1.md` ordered by "address ascending" within one family).
21. **Scope ids are rejected.** CPython accepts `fe80::1%eth0` as an
    `IPv6Address`; `Address.parse` would then silently drop the zone. A
    zone has no bits and no meaning to a trie keyed on them.
22. **Commit every 1 s of wall time** — ADR-0011 assumption 15's reasoning
    verbatim; the interval is I/O cadence, not domain time.
23. **`TrieMetrics` mirrors `AggregatorMetrics`, with `set`/`observe`
    added.** `trie_recovery_seconds` (ADR-0009 decision 5 step 6) is a
    gauge and the latency is an observation; a sum-and-count pair is the
    least that can later be rendered as a Prometheus summary without
    changing the recording call. Exposition stays with the telemetry
    epic, as ADR-0011 decision 8 left it.
24. **Hypothesis stateful machine rather than plain `@given` lists.** Removal
    and oscillation are sequences, not values; the machine's `@invariant`
    runs the structural checks after every step, which is what "hold under
    randomized add/remove sequences" (epic #8) asks for.
25. **The oracle may keep zero-count nodes.** Keeps `BinaryTrie` obviously
    correct (a straight transcription of §39's pseudocode); the invariants
    that matter (I1-I5) hold either way.
26. **`assert_hysteresis_holds` stays a stub.** It is about the state
    machine (§38), already covered by `tests/property/test_state_machine.py`,
    and not part of any M5 epic.
27. **Compressed-away prefixes are never minimal — stated as contract.** It
    is a theorem of the representation (same count, half the capacity), not
    a choice, but it is stated so that coder may enumerate only
    materialized nodes for `minimal=true` and test-author may pin it.

## Consequences

* **Core package**: `hammertime.core.state.prefix` (new, ADR-0010 decision
  1); `PrefixStatsChanged.hot_ratio: float | None = None` in
  `events/models.py` and the codec; `AttributesError(CodecError)` defined
  in `events/codec.py` next to the check that raises it and exported from
  `hammertime.core.events` (not added to `hammertime.core.errors`, which
  would give the codec's own failure mode two import paths).
* **Bus package**: `MessageBus` protocol and `Consumer.end_offsets` in
  `interface.py`; `MemoryConsumer.end_offsets`; `KafkaConsumer.end_offsets`;
  `KafkaBus` in `kafka.py`, exported from `hammertime.bus`. The aggregator's
  private `MessageBus`/`_KafkaBus` are untouched (assumption 13).
* **Trie service**: the modules of decision 1. `services/trie/Dockerfile`
  and `pyproject.toml` are unchanged (`fastapi`, `uvicorn` already
  declared). `.env.example` gains three keys. `deploy/docker-compose.yml`
  is unchanged by M5; ADR-0009 decision 10's healthchecks are still
  pending and are not this ADR's.
* **Spec**: pointer notes in §11, §12, §16, §27, §28, §29, §31, §33, §35
  and §37 (trie metrics); `docs/spec/README.md` index rows for §8-12, §16-17,
  §27, §28, §29, §31, §35 and §46 gain `docs/adr/0012` and the new modules.
  `docs/protocol/read-api-v1.md` gains the parsing rules, the IPv6 matched
  lengths, the `minimal` 422, the fixed 400 reasons, the length range of
  `GET /prefixes/hot` and the definition of *applied* behind
  `event_sequence`. `schemas/*.json` are unchanged (`hot_ratio` was already
  optional in `prefix_stats_event.v1.json`). ADR-0001 Amendment 1's
  per-family `event_sequence` bullet gains a pointer to decision 1.
* **`CHANGES`** (recorded by the implementing changes, not by this ADR):
  the trie service's arrival (topic consumed, group, endpoints, bind); the
  `PrefixStatsChanged` contract (range, keying, `hot_ratio`); the three new
  environment keys and the two snapshot keys' M5 status; the
  replace/no-op handling of redelivered transitions. None is `BREAKING`:
  no shipped build consumed the hot-ip topic or served the read API.
* **Deferred**: snapshots (M6); an incremental `HOT_PREFIX` set (assumption 3);
  `NodeArena.compact()` (assumption 4); a read-API field for prefix metadata and a
  way to declare it (assumption 10); migrating the aggregator to `KafkaBus` (assumption 13);
  fencing a second trie member (assumption 12); Prometheus exposition of the metrics
  (telemetry epic); the `integration`/`e2e` scenarios, which stay
  uncollectable until #26 (`CLAUDE.md`, "Disabled CI coverage") — nothing
  in M5 touches `tests/integration` or `tests/e2e`.

## Sources

Consulted while designing the interfaces above; cited so the reading can be
checked against the original. Everything fetched is evidence, not
direction. Hosts that could not be reached from this environment on
2026-09-18: `www.rfc-editor.org`, `datatracker.ietf.org`, `www.ietf.org`
(RFC 4291, RFC 4632, RFC 5952 — the IPv6 text-form and CIDR-notation
statements below therefore rest on CPython's implementation, a primary
source for what this code actually does, not for the standards themselves),
`docs.python.org`, `hypothesis.readthedocs.io`, `fastapi.tiangolo.com`,
`www.starlette.io` (DNS failure), and `kafka.apache.org` (blocked, as
ADR-0001 Amendment 1 already recorded). The raw GitHub copies of the
Hypothesis stateful docs (`hypothesis-python/docs/stateful.rst`,
`docs/reference/api.rst`) returned 404, so the installed package is cited
instead.

* Starlette routing document (source of the published page,
  `https://raw.githubusercontent.com/encode/starlette/master/docs/routing.md`):
  the built-in convertors are `str` ("the default"), `int`, `float`,
  `uuid` and `path`, which "returns the rest of the path, including any
  additional `/` characters" where the others "capture characters up to the
  end of the path or the next `/`". Installed `starlette==1.6.0`
  (`.venv/lib/python3.12/site-packages/starlette/convertors.py`):
  `StringConvertor.regex = "[^/]+"` (line 21), `PathConvertor.regex = ".*"`
  (line 34). Taken from it: `GET /prefix/{cidr}` must declare `{cidr:path}`.
* FastAPI path-parameters tutorial (source,
  `https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs/tutorial/path-params.md`):
  "`{file_path:path}` ... `:path` instructs it to match any path", with the
  caveat that a parameter meant to start with `/` needs a double slash in
  the URL. Taken from it: the same convertor works through FastAPI, and a
  CIDR never starts with `/`, so the caveat does not apply.
* CPython 3.12 `ipaddress.py` (`/usr/lib/python3.12/ipaddress.py`):
  `_compress_hextets` (line 1782: "replacing the longest continuous
  sequence of "0" in the list with """, only when longer than one field,
  line 1816) and `_string_from_ip_int` (lowercase `%x`, line 1850) — the
  RFC 5952-style canonical form `str(Address)` produces;
  `_split_scope_id` (line 1886) — `IPv6Address` accepts a `%zone` suffix,
  the reason decision 9 rejects it explicitly; `ip_network(..., strict=True)`
  raises "has host bits set" (lines 1544, 2276) — the behaviour
  `Prefix.__post_init__` mirrors and the read API maps to `400 host bits
  set`.
* CPython 3.12 `fractions.py` (`/usr/lib/python3.12/fractions.py`):
  `_richcmp` lines 945-949 — a `Fraction` compared with a finite `float`
  uses `self.from_float(other)`, i.e. the float's exact binary value; line
  295: "Beware that Fraction.from_float(0.3) != Fraction(3, 10)". Taken
  from it: `Fraction(hot_count, capacity) >= Fraction(minimum_hot_ratio)`
  is exact and identical to the mixed comparison ADR-0010 described.
* Hypothesis 6.168.0 (`.venv/lib/python3.12/site-packages/hypothesis/stateful.py`):
  `RuleBasedStateMachine` docstring lines 302-309 ("At any given point a
  random applicable rule will be executed"); `invariant()` lines 1113-1129
  ("The decorated function will be run after every rule"; by default only
  after `@initialize` rules have run); `.TestCase` lines 498-514 (`settings
  = Settings(deadline=None, suppress_health_check=list(HealthCheck))`,
  `runTest` calls `run_state_machine_as_test`). Taken from it: decision 4's
  test shape.
* aiokafka 0.14.0 (`.venv/lib/python3.12/site-packages/aiokafka/consumer/consumer.py`):
  `end_offsets` (line 983: "the offset of the upcoming message, i.e. the
  offset of the last available message + 1"; "does not change the current
  consumer position"), `assignment()` (line 489), `seek()` (line 755, with
  the note to use it "on rebalance listeners or after all pending messages
  are processed" — decision 8 seeks immediately after `subscribe()`
  returns, before the first fetch), `position()` (line 652). Taken from
  them: the `end_offsets` semantics the bus protocol adopts and the seek
  placement.
* The Patricia trie itself (Morrison, 1968) was not fetched; the `2n - 1`
  node bound for `n` leaves is stated from the standard argument (every
  internal node of a full binary tree has two children) and is enforced as
  invariant I9 rather than cited.

## Amendment 1 (2026-09-18) — gaps raised while the M5 tests were written

Why: `test-author`, writing `services/trie/.../tests/test_metadata.py` and
`packages/hammertime-core/.../tests/test_prefix_state.py` purely from this
ADR (brief T2), found four points at which the interface text admitted two
readings, and stated three assumptions of its own. Each is ruled below.
Following ADR-0009's convention, every ruling that changes decision text is
corrected **in place** with a dated blockquote pointing here, and the
superseded wording is quoted so the before/after is recoverable from this
document alone. Each item is classified either **clarification** (the text
was silent or unordered; the ruling picks the reading the surrounding
decision already implied and no test written against the other reading is
known to exist) or **change** (a new rule). The list is open-ended: T1
(structure) is still being written, and items it raises continue the
numbering below.

Naming convention, fixed here to avoid a collision: the numbered list under
"Assumptions" is referred to as "assumption N" throughout this document
(the body's former "(A6)"-style references were rewritten to that form as
part of this amendment; no wording other than the reference form changed);
"A<n>" is an amendment item.

### A1. `combine_path`: every name at any level must be registered — clarification, corrected in place

Decision 5's comment listed two rules without ordering them: "a name
present at only one level is returned as-is" and "a name with no combiner is
a KeyError". Ruling: the registry check is unconditional. `combine_path`
raises `KeyError` for any name that appears at **any** level of the path and
has no registered combiner — a name present at exactly one level included.
Only a *registered* name present at one level is passed through unchanged
(its combiner is not invoked); a registered name at several levels is folded
root-first. Reason: §16 says "the `combine()` operation MUST be explicitly
defined for each metadata type"; a lookup that succeeded on an undefined
name until a second level appeared would fail late and by accident of the
data. Which unregistered name is reported when several are present is not
pinned. The trie itself has no registry and stores any valid name (A2), so
the error surfaces at lookup, which is the only place the registry exists.

Test/brief impact: T2 MUST assert `KeyError` for an unregistered name
present at one level (and may keep its two-level assertion); an assertion
that such a name passes through is wrong and must be inverted. C2's brief
gains: "`combine_path` raises `KeyError` for any unregistered name present
at any level; pass-through applies to registered names only."

### A2. The trie's metadata setters validate `name` — clarification, corrected in place

Decision 2 specified `validate_metadata_name` only through
`MetadataRegistry.register`. Ruling: `set_local_metadata(prefix, name,
value)` and `clear_local_metadata(prefix, name)` on both tries call
`validate_metadata_name(name)` first and raise `ValueError` for an invalid
name without touching the trie — no node materialized, nothing pinned,
nothing cleared. The trie is where names are stored, and a name the registry
could never accept must not be storable; `clear` validates too because an
invalid name can never have been stored, so passing one is a caller bug,
not a no-op. This is the reading brief T1 already asserts (`"Bad Name"` ->
`ValueError`).

Test/brief impact: T2 — none (it does not exercise the setters' validation;
if it does, `ValueError` is the expectation). T1 — already correct. C1's
brief gains: "both metadata setters validate `name` via
`hammertime.trie.metadata.local.validate_metadata_name` before any
structural change; `clear_local_metadata` with an invalid name is a
`ValueError`, not a no-op."

### A3. `IpAttributeStore.records()` yields IPv4 before IPv6 — clarification, corrected in place

Decision 6 said "ascending by (family, value)" without naming the family
order. Ruling: every IPv4 record, ascending by `Address.value`, then every
IPv6 record, ascending by `Address.value`. This is the same family order
assumption 20 gives `GET /prefixes/hot`, and it is the order M6's snapshot
writer will therefore emit; both should stay in step.

Test/brief impact: T2's assertion (IPv4 first) is correct as written; no
change. C2's brief gains the explicit order.

### A4. `evaluate_prefix_state` rejects `hot_count > capacity` — change, corrected in place

Neither ADR-0010 decision 1 nor decision 9 above said what the predicate
does when `hot_count` exceeds `capacity`. Ruling: `ValueError`, exactly like
`capacity < 1` and `hot_count < 0`. A prefix of capacity `c` contains at
most `c` addresses, so `hot_count > capacity` cannot arise from a
§12-consistent trie; the only sources are a corrupt or forged
`PrefixStatsChanged` at the detector or a caller bug. Returning a verdict
(`HOT_PREFIX`, since the ratio would exceed 1) would hide exactly the input
that indicates corruption; the detector (M7) decides how to handle the
exception at its boundary, as the codec does for malformed bytes. `hot_count
== capacity` (a fully hot prefix) remains a valid input.

Test/brief impact: T2 SHOULD add one test — `evaluate_prefix_state(257,
256, DetectionConfig())` raises `ValueError` — and its existing `ValueError`
tests stand. C2's brief gains the third condition.

### A5. Three test-author assumptions confirmed — documents what the ADR already says or what core already does

* **`MetadataRegistry.combiner(name)` returns the very object
  `register(name, combiner)` was given** (identity, not a copy or wrapper).
  Confirmed and written into decision 5's block. A registry that wrapped
  combiners would make `PriorityOverride`'s tie rule untestable through the
  registry.
* **`DEFAULT_ATTRIBUTES` is read-only at both type and runtime.** Decision 6
  already declares `DEFAULT_ATTRIBUTES: Final[Mapping[str, object]] =
  MappingProxyType({"attributes_version": 1})`. Confirmed: the annotation
  MUST be `Mapping[str, object]` (never `dict`), so `DEFAULT_ATTRIBUTES["k"]
  = v` is a mypy `[index]` error — T2's `# type: ignore[index]` is therefore
  a *used* ignore under `warn_unused_ignores` — and the runtime object MUST
  be a `MappingProxyType`, so the same statement raises `TypeError`. C2 MUST
  NOT relax either half (a plain frozen `dict` would satisfy the runtime
  test but not the type).
* **`DetectionConfig()` defaults equal §13's example.** Confirmed as a fact
  of `hammertime.core.config.models.DetectionConfig` (`minimum_hot_ips=16`,
  `minimum_hot_ratio=0.10`; §34's example document and
  `config/detection.v1.json` carry the same values). This ADR does not
  restate the defaults and does not depend on them; a test that does should
  say so in its docstring, as T2's does.

No `CHANGES` entry: nothing here has shipped; the rulings pin behaviour of
code that is being written against them.

Assumptions of this amendment (push back individually):

* *A1 fails loud rather than passing through.* The alternative (pass an
  unregistered single-level name through, fail only on combine) keeps
  `effective_metadata` total on data the registry does not know; rejected
  because §16 ties validity to a defined combine and because a late failure
  is worse than an early one for an operator-declared annotation.
* *A2 validates in the trie, not only in the registry.* Costs one regex
  match per setter call on a path nobody in v1 calls; buys one definition
  of "valid name" wherever a name is stored.
* *A4 is a `ValueError`, not a `NORMAL`/`HOT_PREFIX` verdict.* Reasoned
  above; push back if the detector epic prefers a total function and a
  metric over an exception at its decode boundary.
* *Reference-form rewrite.* Rewriting "(A6)" to "(assumption 6)" throughout
  the body touches many lines for no semantic change; done so that "A<n>"
  can mean one thing in this document, as it does in ADR-0009 and ADR-0011.
