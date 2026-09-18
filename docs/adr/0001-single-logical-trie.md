# ADR 0001 — One logical trie owner, four deployable services

Status: accepted; amended 2026-09-18 (Amendment 1: the consistency model
under which many aggregator shards feed the one logical trie — §21, §22)

## Context

§21 offers a global trie (Option A) or per-shard tries with query-time
aggregation (Option B). §43 recommends starting with Option A. Separately, §19
describes an event-driven pipeline whose stages have very different scaling
characteristics: observation ingestion is high volume, state transitions are
comparatively rare.

## Decision

Deploy four services from day one — ingest, aggregator, trie, detector — but keep
**one logical trie owner** (a single-writer process per address family). Ingest
and aggregator scale horizontally; the trie service does not, yet.

The service split is about isolating failure domains, deploy cadence, and
resource profiles. It is not a claim that the trie is distributed.

## Consequences

* Prefix counts stay exact; no cross-shard aggregation logic in v1.
* The trie service is a known scaling ceiling. `trie_updates` and
  `hot_transition_to_prefix_update_latency` are the metrics that will tell us
  when Option B is required (§21, §43).
* Prefix classifications are eventually consistent with respect to IP state; all
  classification responses carry `as_of` and `event_sequence` (§22).
* Moving to Option B later changes the trie service's internals and the query
  fan-out — not the event contracts.

## Amendment 1 (2026-09-18) — the consistency model: many aggregator shards, one logical trie (§21, §22)

Why: milestone M4 names "the consistency model that lets multiple aggregator
shards feed one logical trie" as a deliverable, and §22 says the system
SHOULD define it explicitly. This ADR chose Option A and said "eventually
consistent"; ADR-0011 fixed what a shard is, how it identifies its events,
and what happens at a handover; ADR-0003 Amendment 2 fixed what
at-least-once means at the aggregator. No single text stated the resulting
model. This amendment does, drawing every clause from a decision already in
force and citing it; it adds no new mechanism. The decision and consequences
above are unchanged.

**The model.**

1. **One owner per IP at any time; the owner is a shard, not a process.** An
   IP's shard is the `hammertime.observations.v1` partition its key maps to
   (ADR-0011 decision 1; ADR-0004's per-IP key). The partition count is fixed
   for the life of the deployment (ADR-0011 Amendment 1, A1), so an IP's
   shard never changes; what changes at a rebalance is which worker holds
   the shard. Single ownership is guaranteed only in `auto` mode, by the
   consumer group protocol. In static mode (`HAMMERTIME_SHARD_IDS=<set>`,
   `KafkaConsumer` calls `assign()` with no coordination) it is an
   **operator invariant, unenforced**: the static sets of all members of a
   deployment MUST be pairwise disjoint, and ADR-0011 decision 1 already
   forbids mixing static and group-managed members. Two members that
   violate either rule are two live owners of one shard: both load the same
   `next_sequence` and publish under the same `agent_id`, so different
   transitions get identical `event_id`s. The design does not detect this,
   and does not fence a worker that keeps acting after losing a partition
   (ADR-0011 A2 records that gap).
2. **A shard's transition stream is totally ordered, and its identity is
   continued — not restarted — across owners.** Every
   `HotIpAdded`/`HotIpRemoved` for an IP carries
   `agent_id = "aggregator-shard-{p}"` and the shard's persisted, monotonic
   `sequence` (ADR-0011 decisions 4 and 5; ADR-0003 Amendment 2). A new owner
   loads that counter and continues from it, so no `event_id` is ever
   reused across a handover even though the window counters restart empty.
   The sequence is **not contiguous**: a number is consumed before the
   transition is persisted, a store failure or a crash between persist and
   publish leaves a hole, and one counter serves both event types
   (ADR-0011 decision 4 step 2, assumption 14). Consumers may rely on
   `sequence` being strictly increasing per `agent_id`, never on it being
   dense.
3. **Per IP, the trie sees transitions in emission order and never sees a
   reordering.** All of an IP's transitions are published under the IP as
   key to `hammertime.hot-ip.v1` (`topics.py`, `_ip_key`), so they land in
   one partition of that topic in publish order, and the single-writer trie
   (this ADR) applies each partition in log order. Per IP, the trie's
   applied history is therefore always a *prefix of the published stream*:
   it may be behind, never out of order, never ahead. Relative to the IP's
   *true* history — every transition the shard recorded in its durable HOT
   set — the published stream is order-preserving but may have holes: a
   transition persisted and never published (a crash between ADR-0011
   decision 4's steps 2 and 4) is part of the true history and reaches the
   trie only through its consequence, the next owner's post-warm-up
   `HotIpRemoved` or a later `HotIpAdded` (clause 5). So the trie's view is
   "in order, possibly with gaps", and every gap is one the trie can absorb
   without knowing it exists.
4. **Across IPs, and therefore for every prefix, consistency is eventual.**
   Transitions for different IPs come from different shards (and land in
   different hot-ip partitions) with no ordering between them; a prefix's
   `hot_count` is the count of HOT IPs the trie has applied *so far*. Reads
   expose the lag: every trie and detector response carries `as_of` and
   `event_sequence` (ADR-0010 decision 4; `docs/protocol/read-api-v1.md`):
   for the trie, its own count of hot-ip events applied so far; for the
   detector, the `sequence` of the newest `PrefixStatsChanged` it has
   applied, which is that same trie counter as carried on the event
   (`read-api-v1.md`, preamble). Neither is a shard's `sequence`, which is
   meaningful only within one `agent_id`.
5. **What a handover does to the stream, and what the trie must absorb.**
   Between `on_revoked` and the next `on_assigned` a shard emits nothing
   (bounded by the rebalance, not by domain time). The new owner inherits the
   shard's durable HOT set, exempts inherited IPs from demotion for
   `window_seconds`, then evaluates them once at the next maintenance sweep
   (ADR-0011 decision 5, decision 6; warm-up completion is checked only in
   `run_maintenance`). Hence: a demotion may arrive up to `window_seconds`
   plus the rebalance time plus one maintenance interval
   (`HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S`, default 1 s) late; a
   promotion for an IP already busy before the claim may arrive once the
   new owner's own count crosses `hot_threshold`; and the trie may receive a
   `HotIpAdded` for an IP it already holds (persist-before-publish recovery,
   decision 4), a `HotIpRemoved` for one it does not, or a byte-identical
   duplicate of a record the producer retried (same `event_id`; ADR-0003
   Amendment 2, transport note) — §46.5's replace-on-add and §11's
   `hot_count >= 0` make all three no-ops, and ADR-0011 Consequences (*Trie
   epic*) makes them requirements on the trie.
6. **Observations are at-least-once with no double count and no loss at a
   rebalance.** ADR-0003 Amendment 2 (every redelivery lands in a window
   that never counted it) and ADR-0011 A20 (a member commits only handled
   positions, so the message in hand at a revocation reaches the next
   owner).

**What is deliberately not promised.** Strong consistency between
`GET /ip/{addr}` and `GET /prefix/{cidr}`; agreement at any instant between a
prefix's `hot_count` and the union of the aggregators' HOT sets; any bound on
"eventually" beyond what `trie_updates` and
`hot_transition_to_prefix_update_latency` (Consequences above) measure; any
guarantee across a change of the observations topic's partition count
(ADR-0011 A1: a migration this design does not cover); fencing of a zombie
owner, or detection of overlapping static `HAMMERTIME_SHARD_IDS` sets across
members — disjointness is the operator's to keep (clause 1); a dense
`sequence` per shard (clause 2); that every recorded transition is published
(clause 3); exactly-once delivery of a transition record to the hot-ip topic
(clause 5; ADR-0003 Amendment 2).

**Relation to Option B.** Clauses 2-6 are the contract Option B would have to
preserve: per-shard tries could each hold a prefix of their own IPs'
histories and a query-time sum would still be eventually consistent with
`as_of`/`event_sequence` semantics per shard. Nothing above precludes it,
which is what the last consequence of this ADR claimed.

Assumptions (each a judgment call, push back individually):

* **Per-partition ordering of the hot-ip topic is relied on, and the Kafka
  documentation stating it could not be fetched from this environment**
  (kafka.apache.org is blocked by the egress proxy; the GitHub mirrors of
  the docs tree returned 404 on 2026-09-18). Clause 3 rests on the
  well-known guarantee that a producer's records to one partition are
  appended in send order and a consumer reads a partition in log order, and
  on `KafkaProducer.publish` (`packages/hammertime-bus/.../kafka.py:79`)
  awaiting `send_and_wait` per message — which in the installed
  `aiokafka==0.14.0` is `future = await self.send(...); return await
  future` (`.venv/lib/python3.12/site-packages/aiokafka/producer/producer.py:512-523`)
  — so the emitter's publish order is its send order. The broker-side
  ordering clause itself **remains unverified against a primary source**:
  the R-M4-0 reviewer also found kafka.apache.org blocked and the GitHub
  docs paths 404 from this environment. It is stated from recall and
  should be checked against Kafka's "Guarantees" documentation by whoever
  next has access. `InMemoryBus` gives the same order trivially (one log per
  topic).
* **Shard assignment is deterministic because the producer's partitioner
  is.** aiokafka's `DefaultPartitioner`, read from the installed
  `aiokafka==0.14.0`
  (`.venv/lib/python3.12/site-packages/aiokafka/partitioner.py`, lines
  4-29; the pinned version, not `master` as an earlier draft of this
  amendment said): docstring "Hashes key to partition using murmur2 hashing
  (from java client)"; body for a non-`None` key, lines 26-29:
  `idx = murmur2(key)`, `idx &= 0x7FFFFFFF`, `idx %= len(all_partitions)`,
  `return all_partitions[idx]`, where `murmur2` (lines 33-96) is the pure
  Python port of `org.apache.kafka.common.utils.Utils.murmur2`. Taken from
  it: the same key bytes and the same partition count give the same
  partition on every producer instance, which — with `_ip_key` publishing
  the canonical `str(Address)` (ADR-0004 decision 3 normalises spellings) —
  is what makes `hash(IP) -> shard` deterministic without any in-repo hash.
* **Disjoint static shard sets are an operator invariant, not enforced.**
  Static mode has no coordinator by definition, so a member cannot learn
  another's set without adding the coordination static mode exists to
  avoid; a store-side fencing token was rejected in ADR-0011 A2. The cost of
  a violation — two owners, colliding `event_id`s — is stated in clause 1
  so that it is a known hazard rather than a surprise. An enforced
  alternative (e.g. a per-shard lease in the state store) is a design pass
  of its own and is not started here.
* **The trie consumes every partition of `hammertime.hot-ip.v1` as one
  member.** This ADR's single writer implies it and ADR-0010 decision 3
  assumes it; the trie is not yet implemented, so clause 3 is a constraint
  on the trie epic rather than a description of shipped code.
* **"Eventually" is left unbounded.** A latency SLO would be an operational
  target, not an architecture decision; the metrics named in Consequences
  are where it would be measured.
* **The address-family split (one writer per family) does not change the
  model.** Each family's trie has its own `event_sequence`; nothing above is
  cross-family.
* **No CHANGES entry.** This amendment states shipped and already-decided
  behaviour; it changes nothing observable.
