# Distributed IP Activity & Prefix Detection Service

## 1. Purpose

This document specifies an architecture for a distributed service that detects unusually active IP addresses and aggregates those signals hierarchically by IP prefix.

The system receives request-count observations from distributed agents. It maintains a sliding request-count window per IP address, classifies IP addresses as `HOT` or `COLD` based on configurable thresholds, and maintains a binary IP trie containing currently hot IP addresses.

The trie aggregates the number of hot IP addresses at every prefix level. This enables detection of distributed scraper/bot networks where a significant proportion of the address space within a CIDR prefix consists of hot IP addresses.

The architecture MUST support:

* IPv4 initially, with an architecture that can support IPv6.
* Distributed agents.
* High-volume request observations.
* Sliding time windows.
* Incremental HOT/COLD state transitions.
* Efficient prefix aggregation.
* CIDR-aware hierarchical detection.
* Concurrent ingestion.
* Out-of-order and delayed observations.
* Idempotent processing where required.
* Horizontal scaling.

---

# 2. Core Concept

The system consists of three logically separate layers:

```text
Agents
   |
   | request observations
   v
Sliding Window Aggregator
   |
   | HOT/COLD state transitions
   v
Active IP Trie
   |
   | prefix aggregation
   v
Prefix Detector
```

The sliding-window subsystem determines whether an individual IP is currently hot.

The trie does NOT calculate request rates.

The trie maintains the current set of hot IP addresses and aggregates that state across all prefixes.

This separation is fundamental.

---

# 3. Definitions

### IP address

An IP address is represented internally as a fixed-width binary vector.

IPv4:

```text
32 bits
```

IPv6:

```text
128 bits
```

The trie operates directly on these bits.

### Prefix

A CIDR prefix is represented by:

```text
network address + prefix length
```

Examples:

```text
192.168.0.0/16
192.168.42.0/24
2001:db8::/32
```

### Hot IP

An IP is `HOT` when its request count within the configured sliding window exceeds the configured hot threshold.

Example:

```text
window = 5 minutes
hot_threshold = 1000 requests
```

Then:

```text
requests(IP, last 5 minutes) >= 1000
```

means:

```text
IP.state = HOT
```

### Cold IP

An IP is `COLD` when its sliding-window count falls below the configured cold threshold.

The system SHOULD support hysteresis:

```text
HOT threshold  = 1000
COLD threshold = 800
```

This prevents rapid state oscillation near the threshold.

### Hot count

For a prefix `P`:

```text
hot_count(P)
```

is the number of currently hot `/32` IP addresses contained within `P`.

### Hot ratio

For IPv4 prefix `P`:

```text
capacity(P) = 2^(32 - prefix_length)
```

and:

```text
hot_ratio(P) = hot_count(P) / capacity(P)
```

For IPv6:

```text
capacity(P) = 2^(128 - prefix_length)
```

In practice, the system MUST avoid overflowing integer types when calculating capacities.

---

# 4. Functional Requirements

## 4.1 Agent ingestion

Agents periodically send observations to the service.

An observation SHOULD contain:

```json
{
  "agent_id": "edge-17",
  "sequence": 123456,
  "window_start": "2026-09-14T10:00:00Z",
  "window_seconds": 60,
  "observations": [
    {
      "ip": "192.168.1.42",
      "request_count": 183
    }
  ]
}
```

The protocol MUST define:

* agent identity
* message identity or sequence number
* observation timestamp/window
* counting semantics
* retry semantics
* whether counts are deltas or absolute values

The preferred model is that agents send **time-bucketed deltas**, not continuously changing absolute counters.

---

# 5. Sliding Window

The request rate calculation MUST be independent of the trie.

A sliding window SHOULD be implemented using fixed-size time buckets.

For example, a five-minute window may use thirty 10-second buckets:

```text
window = 300 seconds
bucket = 10 seconds
bucket_count = 30
```

For an IP:

```text
IPCounter
    bucket[0]
    bucket[1]
    ...
    bucket[29]

    total
```

When a bucket expires:

```text
total -= expired_bucket.count
```

When a new observation arrives:

```text
total += observation.count
```

The implementation SHOULD maintain the running total rather than summing all buckets on every lookup.

Therefore:

```text
window_count(IP)
```

is O(1) after bucket maintenance.

> ADR-0011: the aggregator's ring holds exactly `window_seconds /
> bucket_seconds` buckets. A bucket starting at `S` is live at time `now` iff
> `0 <= bucket_start(now) - S < window_seconds` (a bucket that has not
> started is not live; ADR-0011 Amendment 2); it leaves the window at exactly
> `now = S + window_seconds`. A delta for a bucket that is not live can
> never affect a future window count and is diverted to reconciliation
> (Section 24) rather than applied. Counters are process-local, one store per
> owned shard (Section 20).

---

## 6. HOT/COLD State Transitions

The system MUST use hysteresis with separate HOT and COLD thresholds.

Let:

```text
count = number of requests observed within the configured sliding window
```

The thresholds MUST satisfy:

```text
cold_threshold < hot_threshold
```

The state transition rules are:

```text
if state == COLD and count >= hot_threshold:
    state = HOT

elif state == HOT and count < cold_threshold:
    state = COLD

else:
    state remains unchanged
```

Therefore:

| Current state |        Window count | New state |
| ------------- | ------------------: | --------- |
| COLD          |   `< hot_threshold` | COLD      |
| COLD          |  `>= hot_threshold` | HOT       |
| HOT           |  `< cold_threshold` | COLD      |
| HOT           | `>= cold_threshold` | HOT       |

The interval:

```text
cold_threshold <= count < hot_threshold
```

is intentionally state-dependent.

For example:

```text
hot_threshold  = 1000
cold_threshold = 800
```

produces:

```text
COLD + 799   → COLD
COLD + 800   → COLD
COLD + 999   → COLD
COLD + 1000  → HOT

HOT + 1000   → HOT
HOT + 900    → HOT
HOT + 800    → HOT
HOT + 799    → COLD
```

This hysteresis prevents state oscillation when the request count fluctuates around the HOT threshold.

The implementation MUST NOT use a single symmetric comparison such as:

```text
count >= threshold
```

for both transitions, because that would eliminate the hysteresis behavior.

---

# 7. Hysteresis

The implementation SHOULD support separate thresholds:

```text
hot_threshold
cold_threshold
```

Example:

```text
hot_threshold  = 1000
cold_threshold = 800
```

State machine:

```text
              count >= hot_threshold
       +------------------------------+
       |                              |
       v                              |
     COLD --------------------------> HOT
       ^                              |
       |                              |
       +------------------------------+
              count < cold_threshold
```

If:

```text
800 <= count < 1000
```

the existing state is retained.

This prevents:

```text
COLD → HOT → COLD → HOT → COLD
```

oscillation caused by normal statistical noise.

---

# 8. Binary IP Trie

The trie is a binary Patricia/radix trie or a simple binary trie depending on implementation requirements.

Each path corresponds to the bits of an IP address.

For IPv4:

```text
root
 |
 +-- 0
 |    |
 |    +-- 1
 |
 +-- 1
      |
      +-- 0
```

A `/24` prefix corresponds to the first 24 bits.

An individual IP corresponds to `/32`.

The trie MUST support:

* inserting a hot IP
* removing a hot IP
* incrementing/decrementing ancestor hot counts
* looking up prefix statistics
* determining the longest matching prefix
* evaluating prefix classification

---

# 9. Trie Node

A conceptual node:

```text
TrieNode
    child[0]
    child[1]

    hot_count

    local_metadata
    prefix_state
```

Where:

### `child[0]`

Pointer/reference to the zero-bit child.

### `child[1]`

Pointer/reference to the one-bit child.

### `hot_count`

Number of currently hot `/32` descendants.

### `local_metadata`

Metadata explicitly attached to this prefix.

> **ADR-0005:** this is *prefix*-scoped metadata (Section 16). Per-IP
> attributes are a separate mechanism stored beside the trie, not in this node;
> the node layout above is unchanged. See Section 46.

### `prefix_state`

Derived classification such as:

```text
NORMAL
HOT_PREFIX
BOT_NETWORK
```

The implementation SHOULD distinguish between explicitly stored state and derived state.

---

# 10. Hot IP Insertion

When:

```text
IP: 192.168.1.42
state: COLD → HOT
```

the system traverses the 32-bit path:

```text
root
 → bit 0
 → bit 1
 → ...
 → /32
```

At every visited node:

```text
node.hot_count += 1
```

Therefore a single state transition requires:

```text
O(address_bit_width)
```

operations.

For IPv4:

```text
O(32)
```

For IPv6:

```text
O(128)
```

The operation is effectively constant-time for either address family.

---

# 11. Hot IP Removal

When:

```text
IP: 192.168.1.42
state: HOT → COLD
```

the same path is traversed.

At every visited node:

```text
node.hot_count -= 1
```

Afterwards:

```text
node.hot_count >= 0
```

MUST remain an invariant.

If the implementation stores only active hot addresses, empty leaf/path nodes MAY be pruned.

However, frequent HOT/COLD oscillation can cause allocation churn.

Implementations SHOULD consider retaining structural nodes or using an arena/slab allocation strategy.

> ADR-0012 decision 2: the production trie prunes a node the moment its
> `hot_count` reaches 0 — unless it carries prefix metadata (Section 16),
> which pins it — and returns the slot to an arena free list that is reused
> before the arena grows, so oscillation costs no allocation after the first
> cycle and live nodes are bounded by `2 * hot_ip_count + pinned + 1`. A
> `HotIpRemoved` for an address the trie does not hold changes nothing
> (outcome `noop`), which is how `hot_count >= 0` survives a redelivery or a
> gap in an IP's stream (ADR-0001 Amendment 1 clause 5).

---

# 12. Trie Invariants

The following invariant MUST always hold:

```text
hot_count(node)
=
number of currently HOT /32 addresses in the subtree rooted at node
```

For a leaf representing an individual IP:

```text
hot_count(/32) ∈ {0, 1}
```

For an internal node:

```text
hot_count(node)
=
hot_count(child[0])
+
hot_count(child[1])
```

assuming both children are represented and the node has no separate `/32` semantic.

> **ADR-0005:** `hot_count` is the trie's only per-node aggregate. Per-IP
> attributes (Section 46), including `weight`, are never summed along the path
> and never enter this invariant. The attribute map adds a derived invariant
> instead: `len(records) == hot_count(root)` per address family.

This invariant is more important than cached `prefix_state`.

> ADR-0012 decision 3 states the invariants the implementation checks
> (`services/trie/structure/invariants.py::check_invariants`, mirrored by
> `hammertime.testkit.invariants`): this section's sum rule and leaf rule,
> `hot_count >= 0`, `hot_count(root) == number of HOT addresses`, and for the
> Patricia representation that every non-root node has `hot_count > 0` or
> prefix metadata, every single-child node has prefix metadata, and no
> arena slot is unreachable. `prefix_state` is not cached at all in v1: it is
> computed at read time from `hot_count`, `prefix_length` and the
> configuration in force, exactly as below.

Prefix state can always be recomputed from:

```text
hot_count
prefix_length
configuration
```

---

# 13. Prefix Classification

A prefix SHOULD NOT become hot merely because one descendant IP is hot.

At minimum, classification SHOULD consider both:

```text
hot_count >= minimum_hot_ips
```

and:

```text
hot_ratio >= minimum_hot_ratio
```

Example:

```text
minimum_hot_ips   = 16
minimum_hot_ratio = 0.10
```

A `/24` with:

```text
hot_count = 37
capacity = 256
ratio = 14.45%
```

would qualify.

A `/32` with:

```text
hot_count = 1
ratio = 100%
```

would not qualify because:

```text
hot_count < minimum_hot_ips
```

This prevents small prefixes from generating false positives.

> ADR-0010: this predicate has exactly one implementation,
> `hammertime.core.state.prefix.evaluate_prefix_state`, called by both the
> trie's read path (Section 29) and the detector — the same single-source rule
> Section 30 imposes on `evaluate_ip_state`. In v1 it yields `NORMAL` or
> `HOT_PREFIX` only; `BOT_NETWORK` is reserved for the Section 14 scorer.

---

# 14. Prefix Classification Should Be Multi-Dimensional

The initial implementation MAY use:

```text
HOT_PREFIX =
    hot_count >= minimum_hot_ips
    AND
    hot_ratio >= minimum_hot_ratio
```

A more advanced classifier SHOULD additionally consider:

* total requests
* requests per hot IP
* average requests/IP
* persistence over time
* rate of new hot IPs
* rate of hot-IP disappearance
* geographical/ASN information
* agent agreement
* known cloud/CDN infrastructure
* historical behavior

Example conceptual score:

```text
score =
    f(
        hot_ratio,
        hot_count,
        request_rate,
        average_requests_per_ip,
        persistence,
        ...
    )
```

The scoring algorithm SHOULD remain separate from trie maintenance.

---

# 15. Important Distinction: Hot IP vs Bot Network

The system MUST distinguish:

```text
individual IP anomaly
```

from:

```text
distributed prefix anomaly
```

Example:

```text
192.168.42.17
500,000 requests/minute
```

could indicate one aggressive scraper.

Whereas:

```text
192.168.42.0/24

hot IPs = 94
hot ratio = 36.7%
```

is evidence of distributed activity.

The latter is a stronger signal for detecting a scraper/bot network.

The system SHOULD therefore preserve both levels of information.

---

# 16. Metadata Inheritance

Metadata attached to prefixes SHOULD be stored as local metadata:

```text
node.local_metadata
```

rather than copied into every descendant.

Effective metadata can be defined as:

```text
effective_metadata(IP)
=
combine(
    root.local_metadata,
    /1.local_metadata,
    /2.local_metadata,
    ...
    /32.local_metadata
)
```

This allows CIDR-level policies or annotations to naturally inherit down the trie.

The `combine()` operation MUST be explicitly defined for each metadata type.

Examples:

### Set metadata

```text
combine(A, B) = A ∪ B
```

### Bitmask

```text
combine(A, B) = A | B
```

### Policy

A policy may use priority/override semantics instead of set union.

The trie SHOULD NOT assume that all metadata is mergeable by simple union.

> **ADR-0005:** this section governs prefix-scoped metadata only. Per-IP
> attributes (Section 46) are not inherited and are not combined along the path;
> the two mechanisms share no namespace.

> ADR-0012 decision 5: `combine()` is a per-name `Combiner` registered in
> `services/trie/metadata/combine.py` (`SetUnion`, `BitmaskOr`,
> `PriorityOverride`; only the first two are commutative, all three are
> associative), folded root-first over the path by `effective_metadata`. On
> the Patricia trie a prefix that gains metadata is materialized and
> *pinned*: never pruned or compressed away while it carries any, because
> prefix metadata's lifetime is independent of hot state (Section 46.6).
> Nothing in v1 declares prefix metadata; the mechanism exists so the
> structure honours this section from the start.

---

# 17. Do Not Materialize Inherited Metadata by Default

The implementation SHOULD NOT copy inherited metadata into every descendant.

For example, if:

```text
10.0.0.0/8 → {"internal"}
```

then the system SHOULD NOT physically copy `"internal"` into every `/32`.

Instead:

```text
/8.local_metadata = {"internal"}
```

and a lookup accumulates metadata along the path.

This avoids potentially enormous update propagation.

---

# 18. Sliding Window and Trie Separation

The architecture MUST maintain a conceptual separation:

```text
Sliding Window:
    IP → request history/count
```

and:

```text
Trie:
    IP/prefix → current hot-state aggregation
```

The trie does not need to know historical request counts.

The sliding-window system does not need to know prefix aggregation.

The only coupling is the state transition:

```text
COLD → HOT
HOT → COLD
```

This is the key scalability boundary.

---

# 19. Event-Driven Internal Architecture

A recommended internal event model:

```text
RequestObservation
        |
        v
SlidingWindowAggregator
        |
        +---- no state change ----> done
        |
        +---- COLD → HOT ----------> HotIpAdded
        |
        +---- HOT → COLD ----------> HotIpRemoved
                                      |
                                      v
                                PrefixTrieUpdater
                                      |
                                      v
                                PrefixClassifier
```

Example:

```json
{
  "type": "HotIpAdded",
  "ip": "192.168.1.42",
  "timestamp": "2026-09-14T10:05:00Z"
}
```

and:

```json
{
  "type": "HotIpRemoved",
  "ip": "192.168.1.42",
  "timestamp": "2026-09-14T10:10:00Z"
}
```

This event boundary is useful for scaling, persistence, debugging, and replay.

---

# 20. Distributed Processing

The system MUST define ownership for each IP.

The simplest strategy is consistent hashing:

```text
hash(IP) → shard
```

The shard owns:

```text
IP sliding-window state
```

and:

```text
IP HOT/COLD state
```

The corresponding trie updates SHOULD preferably be processed by the same logical shard.

This gives:

```text
IP
 |
 +--> one owner
       |
       +--> sliding counter
       |
       +--> HOT/COLD state
       |
       +--> trie update
```

This minimizes distributed coordination for individual IP state.

> ADR-0011: a shard is a partition of `hammertime.observations.v1`.
> `hash(IP) -> shard` is the bus's key partitioner acting on the per-IP key
> ingest publishes under (ADR-0004); the aggregator computes no IP hash of
> its own. Ownership is the `hammertime-aggregator` consumer group's partition
> assignment (ADR-0009 decision 9), delivered through the bus's assignment
> listener; `HAMMERTIME_SHARD_IDS=auto` lets the group coordinator assign,
> an explicit set pins a member to those partitions (an explicitly empty set
> is a configuration error, ADR-0011 Amendment 1). The sliding counters are
> process-local; the set of HOT IPs per shard is kept in a durable state
> store and inherited on claim, so a restart or handover never leaves the
> trie holding an IP no owner remembers. Because the partitioner maps an IP
> to a partition as a function of the partition count, that count is the
> ceiling on aggregator parallelism and is fixed for the life of a
> deployment: changing it once the topic carries data remaps every IP and
> invalidates the persisted per-shard HOT sets (ADR-0011 Amendment 1, A1).

---

# 21. Prefix Aggregation Across Shards

There are two major designs.

## Option A: Global trie

All hot-IP events are routed to a single logical trie service.

Advantages:

* simple semantics
* simple prefix queries
* exact counts

Disadvantages:

* potential bottleneck
* limited horizontal scalability

## Option B: Per-shard tries + aggregation

Each shard maintains local hot counts:

```text
Shard A → trie
Shard B → trie
Shard C → trie
```

A prefix query aggregates:

```text
hot_count(P)
=
Σ shard.hot_count(P)
```

Advantages:

* highly scalable ingestion
* no single trie bottleneck

Disadvantages:

* prefix queries require aggregation
* global prefix state is eventually consistent unless coordinated

The architecture SHOULD initially prefer Option A if throughput permits, because it significantly simplifies correctness.

Option B becomes attractive when the global trie becomes a measurable bottleneck.

> ADR-0001 chooses Option A: one logical trie owner (a single writer per
> address family) fed by every aggregator shard, no cross-shard aggregation
> in v1, exact prefix counts. A "shard" here is a partition of
> `hammertime.observations.v1` (ADR-0011 decision 1; §20's note); all shards
> publish their transitions to `hammertime.hot-ip.v1` keyed by IP, so each
> IP's transitions reach the trie in order under one `agent_id`
> (`aggregator-shard-{p}`) and one `sequence` that is continued — never
> restarted, though not contiguous — whichever worker holds the shard
> (ADR-0001 Amendment 1, clauses 1-3). The two scaling
> ceilings are distinct: the aggregator's is the observations topic's
> partition count (§20's note; ADR-0011 Amendment 1, A1), the trie's is the
> single writer — and only the second is what `trie_updates` and
> `hot_transition_to_prefix_update_latency` are watched for to decide when
> Option B is needed (ADR-0001 Consequences).

---

# 22. Consistency Model

The system SHOULD explicitly define whether prefix detection is:

```text
strongly consistent
```

or:

```text
eventually consistent
```

For large distributed deployments, eventual consistency is likely sufficient.

An individual IP state may be:

```text
HOT
```

while the prefix aggregate catches up milliseconds later.

The system SHOULD expose timestamps/version numbers for derived classifications when operational correctness matters.

> Defined: **eventually consistent** across IPs and therefore for every
> prefix; **ordered per IP**. ADR-0001 Amendment 1 states the model in six
> clauses: one owning shard per IP whose identity (`agent_id`, persisted
> `sequence`) is continued — not restarted, though not contiguous — across
> worker handovers; per IP the trie applies transitions in emission order,
> so its view is a prefix of the published stream and, relative to the IP's
> true history, in order with possible gaps (a transition persisted but
> never published), never a reordering; across IPs there is no ordering,
> and a prefix aggregate catches up as transitions are applied. Every trie
> and detector response carries `as_of`, `event_sequence` (the trie's count
> of applied hot-ip events; the detector reports that counter as carried on
> the newest `PrefixStatsChanged` it applied) and `config_version`
> (`docs/protocol/read-api-v1.md`; ADR-0010 decision 4). A shard handover
> may delay a demotion by up to `window_seconds` plus the rebalance time
> plus one maintenance interval, and may replay a `HotIpAdded` for an IP the
> trie already holds, a `HotIpRemoved` for one it does not, or a
> same-`event_id` duplicate from a producer retry; all are no-ops for the
> trie (§11, §46.5; ADR-0003 Amendment 2). Observations are consumed
> at-least-once with no double count (ADR-0003 Amendment 2) and none lost at
> a rebalance (ADR-0011 Amendment 6, A20). Not promised: strong consistency
> between `GET /ip` and `GET /prefix`, a bound on the catch-up lag, a dense
> `sequence`, exactly-once transport of a transition record, fencing of a
> worker that acts after losing its shard (ADR-0011 Amendment 1, A2),
> detection of overlapping static `HAMMERTIME_SHARD_IDS` sets — in static
> mode disjointness across members is an unenforced operator invariant — or
> any guarantee across a change of the observations topic's partition count
> (A1).

---

# 23. Agent Duplicates and Retries

Agents may retry messages.

The protocol MUST therefore define message identity.

A message SHOULD include:

```text
agent_id
sequence_number
```

or:

```text
event_id
```

The server SHOULD maintain sufficient state to reject duplicate messages.

Without deduplication:

```text
same observation received twice
```

would produce:

```text
count += N
count += N
```

instead of:

```text
count += N
```

and could generate false HOT transitions.

---

# 24. Out-of-Order Events

Distributed agents can produce delayed events.

For example:

```text
10:00:20 observation
10:00:40 observation
10:00:30 observation
```

The system MUST define whether the sliding window is based on:

```text
event time
```

or:

```text
server arrival time
```

For accurate distributed measurement, event time is preferable.

A bounded lateness policy SHOULD be used:

```text
allowed_lateness = 30 seconds
```

Events older than the accepted lateness horizon MAY be dropped, corrected, or sent through a reconciliation path.

> ADR-0011 fixes the aggregator's policy. With `age = now - window_start` on
> the service clock at processing time: `age < 0` (`future`) and
> `age > window_seconds + allowed_lateness_seconds` (`late`, the ADR-0002
> horizon with the configured window) are diverted, byte-for-byte, to
> `hammertime.observations-reconciliation.v1` and counted in `late_messages`.
> An observation inside that horizon whose bucket has already left the
> window (`expired_bucket`) is diverted the same way — it is never applied
> and never silently dropped. A message whose `window_seconds` exceeds the
> configured window is diverted too (ADR-0010 decision 6). Only a message
> that fails decoding or the ADR-0004 one-IP-per-message invariant is
> dropped, with a log record. A message a member fetches for a partition it
> does not hold — a rebalance can revoke one between fetch and handling — is
> not applied, diverted or counted by that member: it is logged and skipped,
> and it is not lost, because a member only ever commits the position after
> the last message it handled, never the position after the last message it
> fetched; the partition's next owner therefore resumes at or before it and
> handles it (ADR-0011 Amendment 5, A19; Amendment 6, A20).

---

# 25. Time Buckets

Bucket timestamps SHOULD be deterministic.

For bucket size `B`:

```text
bucket_start =
    floor(event_timestamp / B) * B
```

All agents therefore map an observation to the same logical bucket.

The service SHOULD use UTC internally.

---

# 26. Memory Considerations

The number of tracked IPs can be enormous.

The implementation SHOULD distinguish:

```text
active IPs with recent observations
```

from:

```text
currently HOT IPs
```

The sliding-window store may contain many more IPs than the trie.

Cold IP state SHOULD expire after sufficient inactivity.

For example:

```text
window = 5 minutes
state retention = 10 minutes
```

An IP with no observations beyond the retention period can be removed from the sliding-window store.

The trie only needs currently hot IPs if the system's purpose is prefix-level hot detection.

> ADR-0011: the sliding-window store is per shard and process-local. It is
> bounded by `state_retention_seconds` (a COLD IP is evicted once
> `last_seen + state_retention_seconds <= now`, whatever its running total
> says — every bucket it holds has necessarily left the window by then, so
> the eviction does not test for an empty window; ADR-0011 Amendment 2, item
> A6), by
> `HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS` (the least-recently-seen COLD IP is
> evicted to make room; a HOT IP is never evicted), and expiry is driven by a
> schedule of next-expiry times so a sweep touches only IPs that have a
> bucket to expire. Only the currently HOT IPs of each shard are persisted
> (`ShardStateStore`), so the HOT set — the part the trie depends on —
> survives a restart or a shard handover while the counters rebuild within
> one window.

---

# 27. Trie Representation

A naive object-per-node implementation can consume substantial memory.

For IPv4, a sparse binary trie can theoretically contain many nodes if arbitrary IPs are inserted.

The implementation SHOULD consider:

* Patricia/radix compression
* packed arrays
* integer node IDs
* slab/arena allocation
* bitmap child representation
* compact metadata storage
* cache-friendly contiguous memory

A Patricia trie is especially attractive because paths containing only one child do not need to be represented bit-by-bit.

For example:

```text
00000000000000000000000011001010
```

can be represented as a compressed edge rather than 32 individual nodes.

However, the logical model MUST remain equivalent to the binary trie described above.

> ADR-0012 decisions 2-4: `services/trie/structure/patricia.py` is the
> production representation (integer node ids in an arena, compressed
> edges, immediate pruning) and `binary_trie.py` the bit-by-bit oracle; the
> two are differentially tested over one public API and a Hypothesis state
> machine. Equivalence is over the *logical* trie: every prefix a compressed
> edge skips exists with the `hot_count` of the node below it, and can be
> `HOT_PREFIX` without being materialized — the read path and the publisher
> answer over those logical prefixes, never over materialized nodes alone. A
> compressed-away prefix is never the *most specific* qualifying one
> (Section 31), because its single logical child has the same count and half
> the capacity.

---

# 28. Atomicity

A HOT/COLD transition MUST update the trie consistently.

For:

```text
COLD → HOT
```

the operation conceptually performs:

```text
for node in path(IP):
    node.hot_count += 1
```

The system MUST NOT expose a partially updated path as a valid global state if concurrent readers can observe the trie.

Possible implementations include:

* single-writer ownership
* shard-local event loops
* locks
* transactional updates
* copy-on-write
* versioned snapshots

The preferred mechanism depends on the runtime and persistence requirements.

A single-writer model is strongly recommended where practical because trie updates are small and deterministic.

> ADR-0012 decision 7: single writer, on one asyncio event loop. The whole
> update — the `hot_count` path of the address's family trie and the per-IP
> attribute record (Section 46.5) — is one synchronous call,
> `TrieState.apply`, with no `await` inside, and every read-API response is
> built inside one synchronous section by an `async def` handler on the same
> loop; a reader therefore cannot observe a partially updated path. The
> outcome of an event is one of `added`, `removed`, `replaced` (a
> `HotIpAdded` for an address already HOT — the record is replaced, no count
> moves) or `noop` (a `HotIpRemoved` for an address not held). The first
> three are *applied* and advance the trie's `event_sequence`; only the first
> two publish `PrefixStatsChanged`.

---

# 29. Read Path

A prefix query:

```text
GET /prefix/192.168.0.0/16
```

should directly locate the `/16` trie node and return:

```json
{
  "prefix": "192.168.0.0/16",
  "hot_ips": 18342,
  "capacity": 65536,
  "hot_ratio": 0.279998,
  "state": "HOT_PREFIX"
}
```

> The complete v1 response shapes (envelope fields `as_of`, `event_sequence`,
> `config_version` per Section 22; `GET /prefixes/hot`; the detector's
> `GET /detections`; zero-valued answers for unknown prefixes) are specified in
> `docs/protocol/read-api-v1.md` (ADR-0010). `state` here is the Section 13
> predicate evaluated against the trie's current configuration.

> ADR-0012 decision 9: the read path is `services/trie/query/views.py`
> (`ReadModel`) behind a FastAPI app; it answers over the logical trie of
> Section 27 for both address families, locates a prefix in
> O(`bit_length`), computes `state` at read time through
> `hammertime.core.state.prefix.evaluate_prefix_state`, and returns
> `request_count` and `attributes` from the per-IP record the most recent
> `HotIpAdded` stored. Parsing rules and the IPv6 `matched_prefixes` lengths
> are in `docs/protocol/read-api-v1.md`.

An IP query — which since ADR-0005 also returns the IP's attributes while it is
HOT (Section 46.7):

```text
GET /ip/192.168.1.42
```

may return:

```json
{
  "ip": "192.168.1.42",
  "state": "HOT",
  "request_count": 1834,
  "matched_prefixes": [
    {
      "prefix": "192.0.0.0/8",
      "state": "NORMAL"
    },
    {
      "prefix": "192.168.0.0/16",
      "state": "HOT_PREFIX"
    },
    {
      "prefix": "192.168.1.0/24",
      "state": "HOT_PREFIX"
    }
  ]
}
```

---

## 30. Recommended Processing Algorithm

The state transition logic MUST use the hysteresis rules defined in Section 6.

Conceptually:

```text
on_observation(message):

    validate(message)

    if duplicate(message):
        return

    for observation in message.observations:

        ip = parse_ip(observation.ip)

        bucket = determine_bucket(observation.timestamp)

        update_sliding_counter(
            ip,
            bucket,
            observation.request_count
        )

        count = get_window_count(ip)

        old_state = get_ip_state(ip)

        if old_state == COLD:

            if count >= hot_threshold:
                new_state = HOT
            else:
                new_state = COLD

        else if old_state == HOT:

            if count < cold_threshold:
                new_state = COLD
            else:
                new_state = HOT

        if old_state == COLD and new_state == HOT:

            set_ip_state(ip, HOT)

            trie.add_hot_ip(ip)

        elif old_state == HOT and new_state == COLD:

            set_ip_state(ip, COLD)

            trie.remove_hot_ip(ip)
```

The implementation SHOULD centralize this logic in a single state-transition function so that threshold semantics cannot diverge between ingestion paths, tests, replay, and re-evaluation.

For example:

```text
evaluate_ip_state(previous_state, count, configuration)
```

MUST be the authoritative implementation of the HOT/COLD state machine.

> ADR-0011: in the aggregator `evaluate_ip_state` is called from exactly one
> place (`services/aggregator/transitions.py`), on four triggers — an
> applied observation (either direction: deltas are non-negative, but
> applying one first subtracts the expired bucket that last occupied the
> ring slot, so the running total can fall and a HOT IP can be demoted on
> this path; ADR-0011 Amendment 2, item A11), an expiry sweep (only HOT ->
> COLD), warm-up end (only HOT -> COLD, for the IPs inherited with a shard
> claim), and a configuration re-evaluation (Section 34, either direction).
> The transition counters of Section 37 are labelled with the trigger
> (`observation` | `expiry` | `warmup` | `config`). Each emitted
> transition is recorded in the shard's durable HOT set *before* the event
> is published, and `HotIpAdded` carries `weight` (Section 46.4) computed
> under the configuration in force at that transition.

---

# 31. Avoiding Nested False Positives

If:

```text
10.20.30.0/24 = BOT_NETWORK
```

then:

```text
10.20.0.0/16
```

may also have a high hot ratio.

The system SHOULD distinguish:

```text
absolute classification
```

from:

```text
minimal/specific classification
```

For example:

```text
10.20.30.0/24 → strong bot-network signal
10.20.0.0/16  → broad aggregate signal
```

A query MAY request the most specific qualifying prefixes rather than every ancestor.

This prevents an alert system from generating hundreds of redundant nested alerts.

> ADR-0012 decision 9: the trie's `GET /prefixes/hot?minimal=true` is this
> query — every `HOT_PREFIX` prefix with no `HOT_PREFIX` descendant, ranging
> over the same prefix lengths the trie reports to the detector (from the
> family's minimum reported length down to the host route), so it and the
> detector's `GET /detections?minimal=true` agree by construction (ADR-0010
> decision 1). Only a materialized trie node can be minimal (Section 27
> note).

---

# 32. Persistence

The trie can be treated as derived state.

The authoritative information is:

```text
IP sliding-window state
+
HOT/COLD state
```

The trie can be reconstructed by replaying HOT/COLD transitions.

Therefore the system SHOULD consider event logging or a durable event stream.

Example:

```text
RequestObservation
       ↓
HotIpAdded
       ↓
HotIpRemoved
       ↓
HotIpAdded
       ...
```

On recovery:

```text
replay current relevant state
        ↓
reconstruct trie
```

This greatly simplifies crash recovery.

If startup reconstruction time is too high, periodic trie snapshots MAY be used.

---

# 33. Persistence Strategy

Recommended architecture:

```text
                 +--------------------+
                 | Durable Event Log  |
                 +---------+----------+
                           |
             +-------------+-------------+
             |                           |
             v                           v
    Sliding State Store          Trie State / Snapshot
```

The event log provides replayability.

The trie is an optimized derived index.

A snapshot may contain:

```text
trie structure
hot_count values
hot IP states
per-IP attribute records   (ADR-0005, Section 46.8)
configuration version
event sequence number
```

After loading a snapshot, events after its sequence number are replayed.

> ADR-0009 / Section 47.2: the trie service is not *ready* — and answers 503 on
> its read API — until that replay has reached the end of the log as it stood
> when the process started. On shutdown it writes a final snapshot after
> committing its consumer position (Section 47.4).

> ADR-0012 decisions 8 and 11 (milestone M5, before snapshots exist): with
> no snapshot the trie seeks every partition of `hammertime.hot-ip.v1` to
> its beginning and replays the whole log to the end captured at start,
> then becomes ready; the consumer group's committed offsets are maintained
> (flushed first) for lag observability but are never the position it
> resumes from. Replayed transitions re-publish their `PrefixStatsChanged`
> with the same `sequence` and `event_id`, which consumers absorb. The
> snapshot of M6 records, per partition, the worker's handled position
> (`TrieWorker.handled_positions`) — this section's "event sequence number"
> is not a single Kafka offset — plus every hot address's record including
> its transition-time `window_count`, every pinned node's prefix metadata,
> the trie's `event_sequence` and the `config_version`; it is taken only
> after a flush-then-commit so it never covers an event whose stats have not
> reached the log. `HAMMERTIME_TRIE_SNAPSHOT_DIR` and
> `HAMMERTIME_TRIE_SNAPSHOT_INTERVAL_S` are parsed and validated from M5 on
> and acted upon from M6 on.

---

# 34. Configuration

Detection configuration SHOULD be versioned.

Example:

```json
{
  "window_seconds": 300,
  "bucket_seconds": 10,
  "hot_threshold": 1000,
  "cold_threshold": 800,
  "minimum_hot_ips": 16,
  "minimum_hot_ratio": 0.10,
  "weight_function": "threshold_ratio",
  "weight_max": 1000000
}
```

`weight_function` and `weight_max` (ADR-0005) select and bound the per-IP
`weight` attribute of Section 46.4. They are optional and descriptive: changing
either alters the `weight` recorded on subsequent transitions and nothing else —
no IP changes state, and no prefix changes classification — so they do not
require the re-evaluation below.

A configuration change MUST define its effect on existing state.

For example, lowering:

```text
hot_threshold
```

can cause many IPs to transition:

```text
COLD → HOT
```

The system SHOULD provide a controlled re-evaluation mechanism instead of silently leaving stale state.

> Section 47.3 (ADR-0009) defines how a running service picks a new document
> up: polled from `HAMMERTIME_CONFIG_PATH`, applied only when `config_version`
> strictly increases, and visible in events and read responses only after the
> re-evaluation has been applied. `test_config_change.py`
> (`docs/spec/integration-scenarios.md`) is the executable form of this section.

> ADR-0011: in the aggregator, applying a new version re-evaluates every
> tracked IP of every owned shard under the new thresholds, with the
> observation stream paused for the pass, so every transition it produces —
> and every event emitted afterwards — carries the new `config_version`. A
> change to `bucket_seconds` or `window_seconds` re-buckets existing counts
> by `bucket_start(S, new_bucket_seconds)` before re-evaluating; no count is
> lost, and counts whose bucket falls outside the new window expire at once.

---

# 35. IPv6

The architecture SHOULD be address-family agnostic.

Define:

```text
Address
    family
    bits
    bit_length
```

IPv4:

```text
bit_length = 32
```

IPv6:

```text
bit_length = 128
```

The same trie abstraction can then be used independently for:

```text
IPv4 trie
IPv6 trie
```

The implementation SHOULD preferably maintain separate roots because IPv4 and IPv6 have different address spaces.

> ADR-0012 decision 1: one trie service process holds one `PatriciaTrie`
> per `AddressFamily` (separate roots, separate arenas) and is the single
> writer for both, consuming both families' transitions from the one
> hot-ip topic under one `event_sequence`. The minimum reported prefix
> length is per family: `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` (IPv4, default
> 8) and `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_V6` (IPv6, default 104 — the
> length whose capacity, 2^24, equals IPv4's /8, so both families report
> the same 25 capacity levels per transition); `GET /ip`'s
> `matched_prefixes` mirror 8/16/24 as 104/112/120 for IPv6.

---

# 36. Security

The ingestion API MUST authenticate agents.

At minimum:

* TLS
* agent authentication
* authorization
* replay protection
* request size limits
* rate limits
* schema validation

An untrusted agent MUST NOT be able to arbitrarily declare:

```text
IP = HOT
```

The server MUST derive HOT state from request observations.

The service SHOULD also protect against malicious agents sending extremely large numbers or malformed IP addresses.

## 36.1 Agent credentials

> Subsections 36.1-36.4 added by ADR-0006. The requirements above are unchanged.

An agent authenticates with `X-Agent-Id` plus `Authorization: Bearer <token>`
(`docs/protocol/observation-v1.md`). The token is a static, long-lived, per-agent
credential; it carries no nonce of its own, so message-level replay protection is
Section 23's `(agent_id, sequence)` dedup, not a property of the credential.

```text
token          MUST be >= 256 bits of entropy from a CSPRNG
server storage MUST be a keyed hash only -- never the token, at rest or in memory
comparison     MUST be constant-time
logs, errors, metrics, traces MUST NOT contain a token or any prefix of one
```

The hash is `HMAC-SHA-256(key, token)`, where `key` is a deployment-wide secret
supplied out of band (`HAMMERTIME_INGEST_AGENT_TOKEN_KEY`, base64url, at least 32
bytes decoded) and never stored in the registry document. Verification recomputes
the HMAC of the presented token and compares digests.

A slow KDF (scrypt, argon2) is deliberately NOT used: the credential is a
high-entropy machine token rather than a human password, and this hash is computed
on every authenticated request rather than once per login. The keyed hash instead
protects against the case the server can no longer detect — an operator who
provisions a weak token by hand — because a registry document that leaks without
its key is not offline-attackable at all. See ADR-0006 for the full argument.

The service MUST refuse to start if the key is missing, undecodable, or shorter
than 32 bytes, rather than falling back to an unkeyed mode.

Rotating the key invalidates every agent credential, because the server does not
hold the plaintext tokens needed to re-derive their hashes. Key rotation is
therefore a fleet-wide re-provisioning, and is not the mechanism for rotating one
agent's credential (Section 36.3).

## 36.2 The agent registry document

Agents are registered in a JSON document read at startup
(`HAMMERTIME_INGEST_AGENTS_PATH`, default `config/agents.v2.json`), specified by
`schemas/agent_registry.v2.json`:

```json
{
  "registry_version": 2,
  "hash_algorithm": "hmac-sha256",
  "key_id": "3f8a1c05d2b74e69",
  "agents": {
    "edge-17": { "token_hash": "<64 hex>", "enabled": true, "rate_limit_rps": null }
  }
}
```

`key_id` is the first 16 hex characters of
`HMAC-SHA-256(key, "hammertime-agent-token-key-id")` — a fingerprint of the key
the hashes were computed under. The service MUST recompute it at startup and
refuse to start on a mismatch, so a misconfigured key fails visibly instead of
rejecting every agent at request time.

The service MUST reject at load:

```text
a document with no registry_version, or any record carrying a plaintext "token"
    (the v1 format -- report it as a migration, not as a parse error)
a key_id that does not match the configured key
two agents sharing any hash, current or previous
a record whose previous_token_hash equals its own token_hash
previous_token_hash without previous_token_expires_at, or the reverse
any unknown key, at envelope or record level
```

The document is no longer secret, but write access to it is still full
compromise: anyone who can add a hash can mint a credential. Read access no
longer discloses credentials; it still discloses the agent roster and per-agent
limits.

A registered agent MAY be disabled (`"enabled": false`). A disabled agent is
known but never authorized; disabling is the way to block an agent, in preference
to deleting its entry (which makes it indistinguishable from a typo'd
`agent_id`) or setting a zero rate limit (which is not a defined state).

## 36.3 Credential rotation

`agent_id` is an identity, not a login: it derives `event_id` (ADR-0004) and keys
dedup and rate-limit state. Rotating a credential MUST NOT require changing it.

A record MAY therefore carry at most one `previous_token_hash`, accepted
alongside `token_hash` until `previous_token_expires_at` (RFC 3339, UTC), which
is REQUIRED whenever a previous hash is present. Expiry is evaluated per request
against the service clock, so the overlap window closes on its own. A window that
has already expired at load time is not a startup failure: the previous hash is
dropped, with a warning.

At most one predecessor is supported by design — an unbounded list of accepted
hashes lets a registry accumulate forgotten live credentials, which is the
problem this section exists to prevent.

```text
rotate:  new token issued, old hash moved to previous_token_hash + expiry
         -> restart ingest
         -> move the agent onto the new token
         -> drop the previous_* keys -> restart
```

## 36.4 Provisioning

Tokens MUST be generated by the provisioning tool
(`tools/agent-token`, `hammertime-agent-token`), which is the only component that
ever sees a token in readable form: it prints the token once, for handing to the
agent, and writes only the hash into the registry. It never writes a token to a
file or a log. The same tool converts a v1 plaintext registry
(`hammertime-agent-token migrate`), which preserves each agent's existing token
and `agent_id`, so migrating the store and rotating credentials stay separate
operations.

Because the server cannot assess the strength of a token it only sees hashed, the
tool is also the only place a strength rule can be enforced: generated tokens are
256-bit CSPRNG values, and an externally supplied token shorter than 32
characters is rejected unless explicitly overridden.

## 36.5 Failed-authentication throttling

> Subsections 36.5-36.7 added by ADR-0007 (failed-authentication throttling) and
> ADR-0008 (observation-scaled rate limiting). The requirements above are
> unchanged. Subsections 36.1-36.4 are ADR-0006's.

Authentication itself MUST be rate limited. A credential check that rejects the
caller before any per-agent limiter is reached is an unbounded online guessing
oracle, and — because Section 36.1 leaves the server unable to judge the strength
of a token it only stores hashed — it is the only bound the server has on
guessing a weak credential at all.

**Only failures are throttled.** A request that authenticates successfully MUST
NOT be rejected by this mechanism, MUST NOT consume failure budget, and MUST NOT
reset it. Exhausting a budget therefore can never deny service to a caller
holding a valid credential.

**Two budgets.** Each failed attempt charges one token to each of two token
buckets, source bucket first:

```text
source bucket  key = client address, IPv4 /32, IPv6 /64
agent bucket   key = a fixed-size slot derived from the attempted X-Agent-Id,
                     or "-" when it is absent or malformed
```

If the source bucket is exhausted the agent bucket MUST NOT be charged and the
request is answered immediately. A bucket that is short of tokens rejects
without consuming any, so repeated attempts while blocked do not extend the
block.

The client address is the socket peer address by default.
`X-Forwarded-For` MUST NOT be consulted unless
`HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS` is greater than zero, in which case the
key is the entry that many positions from the right of that header; an absent,
shorter, or unparsable header falls back to the socket peer. IPv6 sources are
keyed by their `/64` prefix, because a single allocation routinely carries 2^64
addresses.

**The agent bucket's key space MUST be bounded independently of the limiter's
eviction policy.** The attempted `X-Agent-Id` is caller-chosen, so charging a
bucket keyed directly on it lets an attacker manufacture enough distinct keys to
evict a target identity's bucket, which is then recreated with a full budget —
resetting the very limit this bucket exists to impose. The service MUST
therefore map every presented `X-Agent-Id` into a fixed table of 4096 slots
using a keyed hash under a salt drawn once per process, and charge the slot:

```text
slot = HMAC-SHA-256(salt, X-Agent-Id) first 8 bytes, big-endian, mod 4096
```

An absent, empty, or over-long `X-Agent-Id` charges the single `"-"` key
instead, so the agent limiter tracks at most 4097 keys for any input and no
bucket is ever evicted. Registered and unregistered identities MUST be mapped by
the identical function, so that a throttled response never discloses whether an
`agent_id` is registered. Two identities sharing a slot share one budget; since
only failures are charged, that can only reduce the guesses available to an
attacker, never increase them. The slot count is a fixed property of the service
and is not operator-configurable; the salt MUST NOT appear in any response, log,
or metric. An LRU bound on the number of tracked buckets is a memory limit, not
a budget limit, and MUST NOT be relied on as the latter.

**Uniform failure response.** Every authentication failure — missing
`X-Agent-Id`, missing or malformed `Authorization`, an over-long header value, an
unknown `agent_id`, a wrong token — MUST return the identical response:

```text
401  WWW-Authenticate: Bearer
     {"detail": "invalid agent credentials"}
```

`403` is reserved for exactly one outcome: a **correct** credential for a
registered but disabled agent (`{"detail": "agent is disabled"}`). Learning that
requires already holding the agent's live token, so it discloses nothing to an
anonymous caller.

Uniformity MUST hold in timing as well as in content: when the `agent_id` is
unknown, the service MUST still compute the presented token's HMAC and
constant-time-compare it against a fixed dummy digest, discarding the result, so
that an unknown identity is not measurably faster to reject than a known one.

**Length bounds, evaluated before any hashing.** An `X-Agent-Id` longer than 128
characters (`schemas/observation.v1.json`'s `agent_id` maximum) or a bearer token
longer than 512 characters is a failed attempt, answered with the uniform 401
above, and MUST be rejected before the credential is hashed.

**A failure that arrives with either budget exhausted** is answered `429` per
Section 36.7 with `X-RateLimit-Scope: auth-failures` and the fixed body
`{"detail": "too many failed authentication attempts"}`. The 429 body MUST NOT
echo the attempted `agent_id` or reveal whether it is registered.

**Logging.** Every failed attempt MUST emit exactly one `WARNING` record from
the `hammertime.ingest.auth` logger, and a successful authentication MUST emit
none:

```text
auth_failure outcome=%s agent_id=%r source=%s path=%s
```

* `outcome` is one of `missing_credential`, `oversized_credential`,
  `unknown_agent`, `invalid_credential`, `agent_disabled`, `throttled`.
* `agent_id` is the *attempted* identity, truncated to 128 characters and
  rendered with `%r` so control characters cannot forge log lines; `None` when no
  identity was presented.
* `source` is the bucket key from the table above.
* The record MUST NOT contain the presented token, any prefix of it, or its hash
  (Section 36.1).

**Configuration** (`.env.example`, all optional):

```text
HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN         default 30   failures/min per source
HAMMERTIME_INGEST_AUTH_FAILURE_BURST                default 10   source bucket capacity
HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN   default 60   failures/min per attempted agent_id
HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST          default 20   agent bucket capacity
HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS                default 0    0 = trust no X-Forwarded-For
```

Rates are per minute; a limiter denominated in requests per second receives
`rate_per_min / 60`. All five MUST be rejected at startup if non-numeric, and
the four budget values if not positive; `HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS`
MUST be a non-negative integer.

This mechanism bounds credential guessing and the information each failure
discloses. It is NOT a defence against a volumetric flood: an in-process bucket
cannot protect the accept queue. A deployment SHOULD rate limit at the network
edge or reverse proxy in addition to this.

## 36.6 Request cost and batch scaling

A rate limit MUST be denominated in something proportional to the work the
request causes. One `POST /v1/observations` may carry up to
`max_observations_per_message` entries, and ADR-0004 fans each distinct IP out
into its own bus message and its own downstream per-IP state, so charging a flat
one token per request lets a single agent's *permitted* traffic amplify by up to
that factor.

Ingest therefore maintains **two** per-agent budgets:

```text
request budget       cost 1.0 per HTTP request      HAMMERTIME_INGEST_RATE_LIMIT_RPS (existing)
observation budget   cost = distinct IPs in batch   HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS
```

**Cost formula.** The observation charge is the number of *distinct* IP
addresses remaining after ADR-0004's coalescing — exactly the number of bus
messages the request will produce — not the length of `observations[]`. A batch
naming one IP 10 000 times costs 1. The count is taken *before* zero-count
entries are dropped: an entry with `request_count` 0 still asked the service to
consider an address, and MUST be charged for it.

A batch is charged whatever its outcome: a duplicate (`200`) and a failed
publish (`503`) are charged the same as an accepted one.

**Bucket capacity is distinct from refill rate.** The observation budget refills
at `HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS` entries per second and holds
`HAMMERTIME_INGEST_OBSERVATION_BURST` entries.

```text
HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS   default 2000    entries/second, per agent
HAMMERTIME_INGEST_OBSERVATION_BURST            default 10000   entries, per agent
```

`HAMMERTIME_INGEST_OBSERVATION_BURST` MUST be greater than or equal to
`HAMMERTIME_INGEST_MAX_OBSERVATIONS`, and the service MUST refuse to start
otherwise. This is what guarantees every schema-valid batch is satisfiable: a
batch can never cost more than the bucket can ever hold, so "this request could
never succeed at any rate" is unreachable from configuration rather than
surfacing as a `500`.

**An over-capacity batch is `413`, not `429`.** Should a batch's cost
nonetheless exceed the configured capacity, the response MUST be `413` naming
the cost and the ceiling — the request is too large, and no `Retry-After` would
be truthful because no wait makes it succeed. An agent that is merely going too
fast gets `429` (Section 36.7). The service MUST NOT pass a cost above capacity
to the limiter.

**Zero-count entries are never published.** After the charge, an entry whose
coalesced `request_count` is 0 MUST be dropped rather than published: a zero
delta is a no-op for the sliding window (Section 5), so publishing one creates
per-IP state (Section 26) and nothing else — an attacker-controlled cardinality
channel. A batch all of whose entries drop out is still `202` and still consumes
its `(agent_id, sequence)` claim (Section 23); it was valid and its effect has
been applied in full. Consequently the aggregator never receives a zero-delta
observation, and any future "refresh this IP without changing its count"
behaviour must be an explicit signal rather than a zero in `observations[]`.

**Pipeline order** for `POST /v1/observations` is normative:

```text
authenticate (36.5)
  -> request budget, cost 1.0          header-only, still before the body is read
  -> body read within max_body_bytes
  -> JSON / schema / domain validation
  -> coalesce
  -> observation budget, cost = distinct IPs
  -> dedup claim (Section 23)
  -> drop zero-count entries, publish, 202
```

The request budget MUST remain the first throttle and MUST remain a flat,
body-free charge: it is the only one that can run before the body is read. The
observation charge MUST sit after coalescing (the cost is not knowable earlier)
and before the dedup claim, so a throttled request does not consume the claim
its own retry will need.

## 36.7 Throttled responses

Every throttled response in Sections 36.5 and 36.6 has the same shape:

```text
429 Too Many Requests
Retry-After: <integer seconds, max(1, ceil(retry_after))>
X-RateLimit-Scope: auth-failures | requests | observations
{"detail": "<human-readable reason>"}
```

`X-RateLimit-Scope` names which budget rejected the request, so an agent (and an
operator reading a trace) can tell "slow down your request rate" from "send
smaller batches" from "your credential is wrong" without parsing prose.
`Retry-After` MUST be present on all three and MUST be an integer count of
seconds of at least 1. The `auth-failures` body MUST be the fixed string given
in Section 36.5; the other two MAY describe the exceeded budget, and MUST NOT
include a credential.

---

# 37. Observability

The system SHOULD expose:

### Ingestion metrics

```text
observations_received
observations_rejected
duplicate_messages
late_messages
auth_failures                (ADR-0007, Section 36.5; labelled by outcome)
auth_failures_throttled      (ADR-0007, Section 36.5)
rate_limited_requests        (ADR-0008, Section 36.6; the request budget)
rate_limited_observations    (ADR-0008, Section 36.6; the observation budget)
```

### Sliding-window metrics

```text
tracked_ips
active_ips
hot_ips
cold_to_hot_transitions
hot_to_cold_transitions
window_evictions           (ADR-0011; labelled shard and retention | capacity)
shards_claimed             (ADR-0011)
```

> ADR-0011: the aggregator labels the two transition counters by `shard`,
> `config_version` and `reason` (`observation` | `config` for COLD -> HOT;
> `observation` | `expiry` | `warmup` | `config` for HOT -> COLD — an
> observation can lower the running total by a count that had already
> expired, Amendment 2 item A11), and is the emitter of
> `late_messages` (labelled `late` | `future` | `expired_bucket`) and of an
> aggregator-side `observations_rejected` (`window_too_long` | `malformed`),
> since it — not ingest — judges lateness (Section 24). `tracked_ips` counts
> every IP in the window store, `active_ips` those with a non-zero window
> total, `hot_ips` those currently HOT.

### Trie metrics

```text
trie_nodes
hot_ip_count
prefix_queries
trie_updates
ip_attribute_records   (ADR-0005, Section 46.8)
ip_attribute_bytes
attributes_rejected
```

> ADR-0012 decision 11: the trie labels `trie_updates` by `family`
> (`ipv4` | `ipv6` | `unknown`, the last only for a malformed record) and
> `outcome` (`added` | `removed` | `replaced` | `noop` | `malformed`),
> `trie_nodes` and `hot_ip_count` by `family`, and `prefix_queries` by
> `endpoint` (`prefix` | `ip` | `prefixes_hot`); `ip_attribute_records` MUST
> equal the sum of `hot_ip_count` over families. It additionally records
> `trie_recovery_seconds` (ADR-0009 decision 5 step 6; the time `start()`
> spent replaying) and the processing-latency series
> `hot_transition_to_prefix_update_latency` below as a sum and a count.

### Detection metrics

```text
hot_prefixes
bot_network_candidates
classification_changes
```

### Processing latency

```text
observation_to_hot_transition_latency
hot_transition_to_prefix_update_latency
```

---

## 38. Core Invariants

The following invariant applies to individual IP state:

```text
state == COLD
    => count may be below or equal to hot_threshold

state == HOT
    => count is always >= cold_threshold
```

More precisely, under normal state evaluation:

```text
COLD → HOT only when count >= hot_threshold
HOT  → COLD only when count < cold_threshold
```

The following invariant applies to prefix classification:

```text
HOT_PREFIX
    => hot_count >= minimum_hot_ips
    AND
       hot_ratio >= minimum_hot_ratio
```

Unless an explicit prefix-level hysteresis mechanism is introduced, a prefix may transition directly between qualifying and non-qualifying states whenever either criterion changes.

---

# 39. Recommended Processing Algorithm

Conceptually:

```text
on_observation(message):

    validate(message)

    if duplicate(message):
        return

    for observation in message.observations:

        ip = parse_ip(observation.ip)

        bucket = determine_bucket(observation.timestamp)

        update_sliding_counter(
            ip,
            bucket,
            observation.request_count
        )

        count = get_window_count(ip)

        old_state = get_ip_state(ip)

        new_state = evaluate_state(
            old_state,
            count,
            configuration
        )

        if old_state == COLD and new_state == HOT:

            set_ip_state(ip, HOT)

            trie.add_hot_ip(ip)

        elif old_state == HOT and new_state == COLD:

            set_ip_state(ip, COLD)

            trie.remove_hot_ip(ip)
```

Trie update:

```text
add_hot_ip(ip):

    node = root

    node.hot_count += 1

    for bit in ip:

        node = node.child[bit]

        create_node_if_needed(node)

        node.hot_count += 1
```

Removal:

```text
remove_hot_ip(ip):

    node = root

    node.hot_count -= 1

    for bit in ip:

        node = node.child[bit]

        node.hot_count -= 1

    optionally_prune_empty_nodes()
```

The actual implementation SHOULD ensure that readers cannot observe an inconsistent intermediate update.

---

# 40. Performance Characteristics

For an IPv4 address:

### Sliding counter update

Approximately:

```text
O(1)
```

assuming fixed bucket count.

### HOT/COLD evaluation

```text
O(1)
```

### Trie state transition

```text
O(32)
```

### IPv4 prefix lookup

```text
O(prefix_length)
```

### IPv6 equivalents

```text
O(128)
```

The constant factors are small.

The main scalability concern is therefore not the trie traversal itself but:

* number of distinct IPs
* ingestion throughput
* memory footprint
* distributed coordination
* event persistence
* concurrent updates

---

# 41. Important Design Principle

The system SHOULD optimize for:

```text
many observations
few state transitions
cheap prefix aggregation
```

rather than attempting to perform expensive hierarchical processing for every request observation.

The intended flow is:

```text
millions of request observations
             |
             v
cheap counters
             |
             v
relatively few HOT/COLD transitions
             |
             v
32-step trie updates
             |
             v
continuously maintained prefix statistics
```

This is the principal scalability property of the architecture.

---

# 42. Example End-to-End Scenario

Configuration:

```text
window = 5 minutes
bucket = 10 seconds

hot_threshold = 1000
cold_threshold = 800

minimum_hot_ips = 16
minimum_hot_ratio = 10%
```

Suppose the following IPs become hot:

```text
10.20.30.1
10.20.30.2
...
10.20.30.156
```

The trie eventually contains:

```text
10.20.30.0/24

hot_count = 156
capacity = 256
hot_ratio = 60.94%
```

Therefore:

```text
10.20.30.0/24
    = BOT_NETWORK_CANDIDATE
```

If those IPs subsequently fall below the cold threshold:

```text
10.20.30.0/24
    hot_count → 15
```

then:

```text
hot_ratio = 5.86%
```

and the prefix ceases to qualify.

No full rescan of the `/24` is necessary.

The prefix classification changes automatically because the trie count changed incrementally.

---

# 43. Recommended Initial Implementation

For the first production-capable version:

1. IPv4 only.
2. Fixed-size sliding-window buckets.
3. Event-time timestamps.
4. Bounded lateness.
5. Agent sequence numbers for deduplication.
6. Per-IP sliding counters.
7. HOT/COLD hysteresis.
8. Single logical trie owner.
9. Binary or Patricia trie.
10. Incremental `hot_count` propagation.
11. Minimum hot-IP count + hot-ratio prefix classifier.
12. Durable event log.
13. Periodic snapshots.
14. Eventual-consistency tolerance for derived prefix classifications.
15. Metrics and audit logs.

Do not prematurely distribute the trie if a single instance can sustain the required update rate.

The fundamental trie operation is only 32 steps for IPv4, so the likely bottlenecks should be measured before introducing distributed prefix aggregation.

---

# 44. Future Extensions

Potential future capabilities include:

### ASN-aware detection

Combine:

```text
prefix
ASN
```

to distinguish legitimate infrastructure from suspicious concentration.

### Geographic aggregation

Track:

```text
country
region
ASN
datacenter
```

as metadata.

### Temporal persistence

Track how long a prefix remains anomalous.

### Burst detection

Detect sudden increases in:

```text
hot_count
```

rather than only absolute ratios.

### Adaptive thresholds

Different thresholds for:

```text
residential
cloud
datacenter
mobile
```

networks.

### Reputation integration

Attach external reputation information to prefix nodes.

### IPv6 support

Use the same logical model with 128-bit addresses.

### Probabilistic front-end

For extremely high ingestion rates, approximate counters or sketches MAY be used before exact state tracking.

Any probabilistic mechanism MUST NOT silently violate the semantics expected by the exact HOT/COLD state machine.

---

# 45. Architectural Summary

The core architecture is:

```text
                    DISTRIBUTED AGENTS
                           |
                           v
                 +--------------------+
                 | Ingestion / Auth   |
                 +---------+----------+
                           |
                           v
                 +--------------------+
                 | Sliding Window     |
                 | IP → request count |
                 +---------+----------+
                           |
                    state transition
                           |
              +------------+------------+
              |                         |
          COLD → HOT                HOT → COLD
              |                         |
              +------------+------------+
                           |
                           v
                 +--------------------+
                 | Binary IP Trie     |
                 |                    |
                 | hot_count          |
                 | local metadata     |
                 +---------+----------+
                           |
                           v
                 +--------------------+
                 | Prefix Classifier  |
                 |                    |
                 | hot_count          |
                 | hot_ratio          |
                 | persistence        |
                 | request density    |
                 +--------------------+
                           |
                           v
                 BOT / SCRAPER NETWORKS
```

The central design principle is:

> **The sliding-window subsystem answers "is this IP currently hot?", while the trie answers "how densely are hot IPs distributed across this prefix hierarchy?"**

This separation allows the system to maintain prefix-level bot-network signals incrementally without repeatedly scanning all IP addresses.

For IPv4, each HOT/COLD transition updates at most 32 trie levels. Consequently, the cost of maintaining hierarchical hot-IP density is effectively constant per state transition, independent of the number of IPs represented by the prefix.

---

# 46. Per-IP Attributes

> Added by ADR-0005. Sections 1-45 are unchanged; this section only adds a
> descriptive layer beside the trie.

## 46.1 Purpose and non-goals

A HOT IP answers "is this address currently hot?" and nothing else. Operators
also need to know *how* hot it is, and eventually *why* it is in the trie — which
detector rule or alerting system put it there. That is descriptive information
about an address, not a change to the trie's arithmetic.

Per-IP attributes are therefore **descriptive only**. The following MUST NOT
change, and MUST NOT be influenced by any attribute:

```text
hot_count semantics and the Section 12 invariant
hot_ratio
the HOT_PREFIX predicate (Section 13, Section 38)
the HOT/COLD state machine and hysteresis (Section 6, Section 7, Section 30)
the HotIpAdded / HotIpRemoved event types (Section 19)
```

No attribute value is ever summed, averaged, or otherwise aggregated along the
trie path. The trie stores counts of hot descendants; it stores no other
per-node numeric aggregate.

## 46.2 The attribute document

An `IpAttributes` document is an open, versioned JSON object
(`schemas/ip_attributes.v1.json`):

```json
{
  "attributes_version": 1,
  "weight": 1450,
  "x_experiment": "any JSON"
}
```

Names fall into two tiers:

### Registered names

Defined in the registry below with a type, a meaning, and a producer. Validated
strictly: an unregistered bare name is rejected, so a misspelling cannot pass as
a new feature.

### Experimental names

Any key matching `^x_[a-z0-9_]{1,48}$`, carrying any JSON value. Validated for
shape only. A consumer that does not recognise an `x_` key MUST preserve it
verbatim — store it, return it on read-back — and MUST NOT interpret it. This is
what lets a producer ship a new attribute end-to-end before anyone standardizes
it.

Adding a registered name is an entry in the registry below plus a property in
`schemas/ip_attributes.v1.json`. It is not a schema migration: documents without
the new name stay valid, `attributes_version` stays 1 unless the *meaning* of an
existing name changes, and consumers that predate the name fall back to the
pass-through rule.

A document carrying an `attributes_version` higher than a consumer understands
MUST be stored and echoed verbatim and MUST NOT be interpreted.

Bounds, enforced by producers and by the codec:

```text
serialized size   <= 1024 bytes
key count         <= 16
```

## 46.3 Attribute registry

| Name | Type | Produced by | Meaning |
| --- | --- | --- | --- |
| `attributes_version` | integer >= 1 | every producer | Generation of this registry the document was written against. Required. |
| `weight` | integer 0..1000000 | aggregator, at transition | Fixed-point hotness, in thousandths of `hot_threshold` (Section 46.4). |

Reserved, defined but not yet enabled:

| Name | Type | Intended meaning |
| --- | --- | --- |
| `sources` | array (<= 8) of `{system, rule, at, detail}` | Provenance trail: which system or detector rule contributed to this IP being HOT, and when. Specified in `$defs` of `schemas/ip_attributes.v1.json`; a document carrying it is rejected until it is promoted into `properties`. Experiments use `x_sources`. |

## 46.4 Weight

`weight` is a fixed-point integer in thousandths of `hot_threshold`:

```text
weight = 1000   <=>   window count exactly at hot_threshold
weight = 2500   <=>   window count 2.5x hot_threshold
```

It is produced at transition time by the configured `weight_function`
(Section 34). The v1 function is `threshold_ratio`:

```text
threshold_ratio(window_count, config) =
    clamp(
        (1000 * window_count + config.hot_threshold // 2) // config.hot_threshold,
        0,
        config.weight_max
    )
```

Integer arithmetic throughout: replay MUST reproduce the same value bit for bit,
which floating point does not guarantee across platforms. Because the function
depends only on `window_count` and the config version the event already names,
any consumer can recompute and verify `weight`, and a consumer that drops it
loses nothing that cannot be derived again.

A stored record keeps the value computed by the configuration in force at its
transition. After a `weight_function` or `weight_max` change, records written
before and after are therefore on different scales until each IP next
transitions; consumers comparing weights across a configuration change MUST
treat them as approximate. This is deliberate — rewriting stored weights would
mean touching every HOT record on a configuration edit, for information that is
descriptive only.

`weight` is available to the classifier as a ranking or severity input
(Section 14, Section 31). It MUST NOT appear in the `HOT_PREFIX` predicate.
Prefix-level weight aggregates are out of scope for this version: maintaining one
incrementally would introduce a second per-node aggregate that replay would have
to reproduce exactly (ADR-0005).

## 46.5 Transport, storage, and lifecycle

Attributes travel on the HOT/COLD transition event as an optional `attributes`
property (`schemas/hot_ip_event.v1.json`). An absent `attributes` is equivalent
to `{"attributes_version": 1}`.

The trie service stores them in a map keyed by full address, held beside the
trie — **not** inside `TrieNode` (Section 9), whose layout is unchanged. The map
is updated in the same single-writer step as the `hot_count` path (Section 28):

```text
HotIpAdded(ip, attributes)   ->  hot_count += 1 along path ;  record[ip] = attributes
HotIpRemoved(ip)             ->  hot_count -= 1 along path ;  delete record[ip]
```

`HotIpAdded` replaces any existing record for that IP; this makes redelivery
idempotent and replay deterministic under the per-IP ordering Section 20 already
guarantees. Attributes on a `HotIpRemoved` MAY be logged but MUST NOT be stored:
the record is deleted.

The resulting invariant derives from the binary primitive rather than
perturbing it:

```text
set(record.keys()) == the set of currently HOT /32 addresses
len(record)        == hot_count(root)          per address family
```

Because attributes are reconstructed from the same event replay as the trie
(Section 32), they require no separate durability or consistency mechanism.

## 46.6 Relationship to prefix metadata

Per-IP attributes and the `local_metadata` of Section 16 are distinct
mechanisms and share no namespace:

```text
local_metadata   prefix-scoped, operator-declared, inherited down the path,
                 never materialized (Section 17), lifetime independent of hot state

IpAttributes     address-scoped, machine-produced, never inherited,
                 lifetime exactly "currently HOT"
```

A full view of an address is both, returned separately:

```text
effective_metadata(ip) = combine(root.local_metadata, ..., /32.local_metadata)
attributes(ip)         = record[ip]   if the IP is HOT
```

## 46.7 Read path

`GET /ip/{ip}` (Section 29) gains an `attributes` object, present only while the
IP is HOT:

```json
{
  "ip": "192.168.1.42",
  "state": "HOT",
  "request_count": 1834,
  "attributes": { "attributes_version": 1, "weight": 1834 }
}
```

`matched_prefixes` is omitted above for brevity; it is unchanged from
Section 29.

`GET /prefix/{cidr}` is unchanged: no attribute is aggregated to prefix level.

## 46.8 Persistence and observability

A snapshot (Section 33) MUST include the per-IP attribute records alongside hot
IP states, so that loading a snapshot and replaying subsequent events yields the
same records as a full replay would.

Metrics (Section 37):

```text
ip_attribute_records      currently stored records; MUST equal hot_ip_count
ip_attribute_bytes        total serialized size of stored records
attributes_rejected       documents rejected for size or shape
```

## 46.9 Security

A validator compiling `schemas/hot_ip_event.v1.json` MUST resolve its `$ref` to
`ip_attributes.v1.json` from the local `schemas/` directory. The `$id` values in
this project are identifiers, not fetchable URLs; resolving one over the network
would make schema compilation depend on an outbound request.

Attributes are written only by trusted internal producers. `schemas/observation.v1.json`
is unchanged: an agent cannot supply attributes, for the same reason Section 36
forbids an agent from declaring `IP = HOT`. A producer MUST validate size and
shape before storing, and no attribute value may be used in an authorization,
routing, or rate-limiting decision — including unrecognised `x_` values, which
are stored and echoed as opaque data.

---

# 47. Service Process Lifecycle

> Added by ADR-0009. Sections 1-46 are unchanged; this section specifies how a
> service *process* starts, becomes ready, is observed, and stops, so that the
> four deployable services (ADR-0001) behave identically under an orchestrator
> and can be composed in-process by the cross-service tests
> (`docs/spec/integration-scenarios.md`).

## 47.1 Entry point

Each service is started by its console script (`hammertime-ingest`,
`hammertime-aggregator`, `hammertime-trie`, `hammertime-detector`), which
calls `hammertime.<service>.__main__:main()`. `main()` MUST:

1. take no arguments and read no command line;
2. read configuration only from the environment (the keys in `.env.example`)
   and from the detection configuration document at `HAMMERTIME_CONFIG_PATH`
   (Section 34);
3. reject an invalid configuration before opening any network connection,
   exiting with status 2 — where "invalid" includes a connection URL the
   client library would refuse to build a client from (`HAMMERTIME_REDIS_URL`
   is validated when `HAMMERTIME_STORE_KIND` is `redis` and ignored when it
   is `memory`; ADR-0009 A12);
4. delegate the remainder of the lifecycle to the shared runner in
   `hammertime.core.runtime` (ADR-0009).

Secrets MUST NOT be fields of a settings object, because settings are logged at
startup.

## 47.2 Startup and readiness

A service MUST log a structured `starting` record (service name, version, bus
and store kind, configuration path and version, bind address, consumer group)
and a `ready` record (with the time taken) — never a credential.

A service MUST tolerate its dependencies (event log, state store) being
unavailable at startup, retrying with backoff up to a startup deadline
(`HAMMERTIME_STARTUP_TIMEOUT_S`, default 60) before failing with status 1.

A dependency failure is *transient* — and therefore retried — iff it is an
instance of a class the connecting code names as transient for that
dependency, with `OSError` (connection refused/reset, timeout) as the floor
every dependency shares; a client library's own connection-error classes are
added per dependency (ingest: `redis.exceptions.ConnectionError`/`TimeoutError`
for the store, `aiokafka.errors.KafkaConnectionError` for the bus).
Connectivity is what is retried — a refused, reset or timed-out connection,
a dependency still loading, and a hostname that does not resolve yet. A
credential the dependency rejects MUST NOT be retried: it fails the start on
the first attempt, with no `dependency_unavailable` record. Where a client
library reports a rejected credential as a subclass of a class the service
must list as transient (redis-py does), the connecting code MUST catch it
inside the connect callable and re-raise a type outside the transient set,
chaining the original and never including the connection URL in the message
(ADR-0009 A1, "The credential rule"). Any failure of a type outside the
transient set is non-transient and fails the start on the first attempt. The
retry delay starts at 0.5 s and doubles to a cap of 5 s; a delay that would
overrun the deadline is not started, and the last transient error is what is
reported. Each failed attempt is logged as `dependency_unavailable` with the
dependency name and a 1-based attempt number (ADR-0009 A1).

A service is *ready* when it can answer correctly for everything published
before it started:

```text
ingest      config, registry and store loaded; producer connected
aggregator  config loaded; consumer subscribed; shard claims held (Section 20)
trie        newest snapshot loaded and the hot-IP log replayed to its end (Section 33)
detector    config loaded; prefix-stats log consumed to its end
```

Every service exposes `GET /healthz` (liveness), `GET /readyz` (readiness,
200 or 503) and `GET /metrics` (Section 37) on its bind address; the
aggregator's is `HAMMERTIME_AGGREGATOR_BIND` (default port 8083). Domain read
endpoints (Section 29, `docs/protocol/read-api-v1.md`) and the ingestion
endpoint MUST answer 503 while the service is not ready.

Readiness has three states — `starting` (from construction), `ready` (from
the end of `start()`), `stopping` (from the moment `stop()` is called, before
anything is torn down) — carried by one `Readiness` object per service
(`hammertime.core.runtime`). A service's `ready` is `True` iff that state is
`ready`; `/readyz` answers `200 {"status":"ready"}` in that state and `503
{"status":"<state>"}` otherwise; the ingestion endpoint and the domain read
endpoints answer the identical 503 body (`docs/protocol/observation-v1.md`,
`docs/protocol/read-api-v1.md`).

## 47.3 Configuration changes

A service polls `HAMMERTIME_CONFIG_PATH` every `HAMMERTIME_CONFIG_POLL_INTERVAL_S`
seconds (default 1) and applies a document only if its `config_version` is
strictly greater than the version in force. A document that fails validation
is logged and ignored; the previous version stays in force. A new version
becomes visible in emitted events and read responses only after the
re-evaluation it triggers (Section 34) has been applied.

A version is always in force: the document is loaded before the service is
constructed (47.1 step 3), and polling begins one interval after the service's
main loop starts — never during `start()`. The poller is
`hammertime.core.runtime.ConfigPoller`, shared by every service (ADR-0009 A5);
`reload_config()` is one poll of it, performed synchronously, returning the
configuration in force afterwards. A rejected document is logged as
`config_rejected`; an applied one as `config_applied`. If a service's own
re-evaluation raises, the previous version stays in force, the failure is
logged, and the next poll retries.

## 47.4 Shutdown

On `SIGTERM` or `SIGINT` a service MUST stop accepting new work, finish the
message it is applying, flush its producer, then commit its consumer
position, and (trie) write a final snapshot whose recorded position is not
ahead of the committed one — all within `HAMMERTIME_SHUTDOWN_TIMEOUT_S`
(default 8) — then exit 0. The flush MUST precede the commit — here and at
every other point at which a service commits a consumer position — so that a
committed position never covers a message whose emitted events have not
reached the event log: a crash between the two re-delivers messages that were
already applied, which every consumer absorbs (Section 23, ADR-0003), whereas
the opposite order would lose transitions the log can never replay (ADR-0009
decision 7, Amendment 2). A drain that exceeds the deadline, or is
interrupted by a second signal, exits 1.

## 47.5 Exit status

```text
0   clean shutdown after a signal
1   runtime failure (startup deadline, non-transient dependency error,
    main loop exited, drain timed out or aborted)
2   configuration invalid, detected before any connection was made
```

## 47.6 Composition for tests

Each service MUST provide `hammertime.<service>.service.build_service(settings,
*, bus=None, clock=None, ...)` returning an object with `start()`, `run()`,
`stop()` and `ready`, so that all four services can be run in one process on a
shared in-memory event log with a controlled clock (ADR-0009 decision 3). The
maintenance coroutines a test drives directly (`run_maintenance`,
`snapshot_now`, `reload_config`) MUST be the same ones the service's periodic
loops call.

The runtime's own timing is injectable rather than observed by sleeping: the
dependency retry (`connect_with_retry`) takes `sleep=` and `monotonic=`
(and `timeout_s=`, `transient=`, `logger=`), and the configuration poller
takes `poll_interval_s=` and `logger=` (ADR-0009 A1, A2, A5). The `Service`
protocol is `runtime_checkable`, so a conformance test may assert
`isinstance(service, Service)` (ADR-0009 A3). A service's `run()` is what
binds its listening socket; `start()` and `stop()` are complete without it,
and a harness that skips `run()` gets no automatic configuration polling
(ADR-0009 A8).

## 47.7 Structured log records

Every record a service emits is one JSON object per line on standard output,
with sorted keys, carrying at least `event`, `level` (lowercase), `service`
and `timestamp` (ISO 8601 UTC), and `exception` (a formatted traceback) only
when the record was emitted with one. `HAMMERTIME_LOG_LEVEL` is the
threshold. The lifecycle events and their fields are the table in ADR-0009
A7; the ones every service MUST emit are `config_invalid`, `starting`,
`dependency_unavailable`, `start_failed`, `ready`, `run_exited`, `stopping`,
`shutdown_timeout`, `config_rejected` and `config_applied`. No record — of any
event — may contain a credential: not the agent token key, not a bearer token,
not the userinfo of a store URL.
