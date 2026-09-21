# ADR 0001 — One logical trie owner, four deployable services

Status: accepted; amended 2026-09-18 (Amendment 1: the consistency model
under which many aggregator shards feed the one logical trie — §21, §22);
amended 2026-09-21 (Amendment 2: the event log is NATS JetStream and shard
assignment is static — clauses 1, 3 and 6 of Amendment 1's model, its
"not promised" list and four of its assumptions are superseded by ADR-0013;
the original text is kept in place with dated notes, and Amendment 2 at the
end states each replacement)

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

> Amended 2026-09-21 (ADR-0013; Amendment 2 below): clauses 1, 3 and 6 are
> superseded. Clause 1: there is no `auto` mode and no consumer group;
> ownership is the static `HAMMERTIME_SHARD_IDS` set, disjointness is
> still an operator invariant, but a violation is now **detected and
> refused** by a per-shard lease in the state store (ADR-0013 decision 7).
> Clause 3: the ordering guarantee rests on the JetStream stream sequence
> and the emitter's awaited acknowledgement, not on Kafka partitions
> (ADR-0013 decision 4). Clause 6: there are no rebalances; at an
> operator-driven handover a member acknowledges only what it handled and
> negatively acknowledges what it fetched and did not handle, so nothing
> is lost or double-counted (ADR-0013 decision 8). Clauses 2, 4 and 5 stand
> as written ("rebalance time" in clause 5 now means handover time).

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

> Amended 2026-09-21 (ADR-0013): two items of that list have moved.
> Detection of overlapping static `HAMMERTIME_SHARD_IDS` sets **is** now
> promised (a second live owner of a shard fails to start; a member whose
> lease lapses stops — ADR-0013 decision 7). Fencing of a zombie owner is
> promised in that narrow form only: a member acts on a shard for at most
> one maintenance interval after its lease has lapsed, and the store still
> carries no fencing token on `record_transition`. Exactly-once transport
> of a transition record is still not promised, but a retried record is
> now deduplicated by the log inside its duplicate window (ADR-0013
> decision 4). The rest of the list stands.

**Relation to Option B.** Clauses 2-6 are the contract Option B would have to
preserve: per-shard tries could each hold a prefix of their own IPs'
histories and a query-time sum would still be eventually consistent with
`as_of`/`event_sequence` semantics per shard. Nothing above precludes it,
which is what the last consequence of this ADR claimed.

Assumptions (each a judgment call, push back individually):

* **Per-partition ordering of the hot-ip topic is relied on; the Kafka
  guarantee behind it was verified against a primary source on
  2026-09-18, and clause 3 stands as written.** Source: the Apache Kafka
  4.3.1 release's own site documentation, `kafka_2.13-4.3.1-site-docs.tgz`
  from `https://archive.apache.org/dist/kafka/4.3.1/` (sha512 checked
  against the published `.sha512`; this tarball is what
  kafka.apache.org/documentation serves — the site itself is blocked by
  this environment's egress proxy, so the archive copy was read instead).
  Paths below are inside that tarball.
  *Consumer side* — `getting-started/introduction.md` line 83: "Events with
  the same event key ... are written to the same partition, and Kafka
  guarantees that any consumer of a given topic-partition will always read
  that partition's events in exactly the same order as they were written";
  `design/design.md` line 197 ("Message Delivery Semantics"): "All replicas
  have the exact same log with the same offsets. The consumer controls its
  position in this log"; line 285 (Replication): "All writes go to the
  leader of the partition ... The logs on the followers are identical to
  the leader's log--all have the same offsets and messages in the same
  order". *Producer side* — `design/protocol.md` line 45: "on a single TCP
  connection, requests will be processed in the order they are sent ...
  The broker's request processing allows only a single in-flight request
  per connection in order to guarantee this ordering."
  **The qualification #89 anticipated is real but does not apply here.**
  `generated/producer_config.html`, entry
  `max.in.flight.requests.per.connection` (line 674): "if this configuration
  is set to be greater than 1 and `enable.idempotence` is set to false,
  there is a risk of message reordering after a failed send due to retries
  (i.e., if retries are enabled); if retries are disabled or if
  `enable.idempotence` is set to true, ordering will be preserved." (Those
  are the Java client's knobs; the same file's `retries` entry, line 54,
  spells out the failure: "if two batches are sent to a single partition,
  and the first fails and is retried but the second succeeds, then the
  records in the second batch may appear first".) So "appended
  in send order" holds unconditionally only with at most one produce
  request in flight per connection, or with idempotence on. The installed
  `aiokafka==0.14.0` (`uv.lock`) has no `max_in_flight_requests` setting,
  leaves `enable_idempotence=False` (ADR-0003 Amendment 2) and does retry
  (`producer/sender.py:884-890`, `_can_retry`: every retriable error is
  retried until the batch expires), so it meets the condition
  structurally rather than by configuration, in two independent ways:
  (i) its sender allows **one produce request in flight per broker node** —
  `producer/sender.py:65` keeps `self._in_flight` as a set of node ids,
  `drain_by_nodes(ignore_nodes=self._in_flight, ...)` (`sender.py:140-143`;
  the skip is `message_accumulator.py:493-494`) never drains a partition
  whose leader has a request outstanding, the node is added at
  `sender.py:148` and removed only after the request handler completes
  (`sender.py:290`); a batch that fails retriably is collected in
  `_to_reenqueue` (`sender.py:792`, `:879`) and put back at the **head** of
  its partition's queue (`message_accumulator.py:464-468`, `reenqueue`:
  `self._batches[tp].appendleft(batch)`), ahead of anything accumulated
  since — the equivalent of `max.in.flight.requests.per.connection=1`;
  (ii) the emitter never has two hot-ip records outstanding anyway:
  `KafkaProducer.publish` (`packages/hammertime-bus/.../kafka.py:79`)
  awaits `send_and_wait` per record — in `aiokafka==0.14.0` that is
  `future = await self.send(...); return await future`
  (`producer/producer.py:512-523`), and a re-enqueued retry keeps that
  future pending — and every path that emits a transition awaits
  `TransitionEmitter.evaluate` one IP at a time (`worker.py:244`, `:253`,
  `:374`; `reevaluate.py:53`), which awaits the publish at
  `transitions.py:142` before returning. Hence, for this client and this
  emitter, the guarantee is exactly as clause 3 states, clause 3 is **not**
  amended, and `enable_idempotence` (ADR-0003 Amendment 2's follow-up, #88)
  is **not load-bearing for ordering** — it remains only the question of
  removing at the source the same-`event_id` duplicate that clause 5 and
  `design/design.md` line 193 ("the message may be written to the log again
  during resending") already account for. (Ingest publishes its per-IP
  observation messages concurrently with `asyncio.gather`,
  `services/ingest/.../publisher.py:116`; that is the observations topic,
  whose ordering clause 3 does not claim.) `InMemoryBus` gives the same
  order trivially (one log per topic).
* **The reference deployment's broker is Redpanda, and the citation above
  is Kafka's.** `deploy/docker-compose.yml:5` runs `redpandadata/redpanda:latest`
  (unpinned), not Apache Kafka. Redpanda's own documentation is not
  reachable from this environment and was not checked. The client-side
  half of the argument (one request in flight per node; awaited per-record
  publish) is a property of aiokafka and of this emitter and holds against
  any broker; the broker-side half (a partition is an append-only log read
  in offset order) is asserted here of Redpanda only on the strength of its
  serving the Kafka wire protocol, which is what the trie epic's integration
  tests will exercise. If that turns out not to hold, clause 3 is the clause
  affected. ADR-0012 (decision 9) makes Apache Kafka the reference broker
  and Redpanda a deployment-only substitute, so once that compose change
  lands the citation above and the reference deployment agree.
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

> Amended 2026-09-21 (ADR-0013; Amendment 2 below): the first, second,
> third and fourth assumptions above — the Kafka per-partition ordering
> citation, the Redpanda-versus-Kafka broker note, the aiokafka
> `DefaultPartitioner` argument, and "disjoint static shard sets are an
> operator invariant, not enforced" — describe the Kafka-backed design and
> are superseded; Amendment 2 states what replaces each. The remaining
> assumptions stand.

## Amendment 2 (2026-09-21) — the model under NATS JetStream and static shard assignment (ADR-0013)

Why: ADR-0013 replaces Apache Kafka with NATS JetStream as the durable
event log and drops group-managed (`auto`) shard assignment. Amendment 1
drew every clause of the consistency model from a decision in force, and
four of those decisions were Kafka's: the consumer group protocol as the
ownership mechanism (clause 1), Kafka's per-partition log order as the
basis of clause 3, aiokafka's partitioner as the basis of "deterministic
shard assignment", and the absence of any overlap detection in static mode
(clause 1, the "not promised" list, and the fourth assumption). This
amendment records what each becomes. Decision and Consequences above are
unchanged; the model is still "eventually consistent across IPs, ordered
per IP", and the per-IP identity rule (clause 2) is untouched.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Gained the "amended 2026-09-21" clause.
* **After clause 6, a dated blockquote** summarising the replacements
  below. No clause's text was edited.
* **After the "not promised" paragraph, a dated blockquote** stating that
  detection of overlapping sets and a narrow fencing are now promised.
* **After the Assumptions list, a dated blockquote** naming the four
  superseded assumptions.

The replacements:

1. **Clause 1** — was: "Single ownership is guaranteed only in `auto`
   mode, by the consumer group protocol. In static mode
   (`HAMMERTIME_SHARD_IDS=<set>`, `KafkaConsumer` calls `assign()` with no
   coordination) it is an **operator invariant, unenforced**: the static
   sets of all members of a deployment MUST be pairwise disjoint, and
   ADR-0011 decision 1 already forbids mixing static and group-managed
   members. Two members that violate either rule are two live owners of one
   shard: both load the same `next_sequence` and publish under the same
   `agent_id`, so different transitions get identical `event_id`s. The
   design does not detect this, and does not fence a worker that keeps
   acting after losing a partition (ADR-0011 A2 records that gap)." Now:
   an IP's shard is `partition_for(str(ip), 128)` (ADR-0013 decision 1),
   the subject `hammertime.observations.v1.<p>`; ownership is the static
   set in `HAMMERTIME_SHARD_IDS` (required; `all` or a set; ADR-0013
   decision 6); the sets of a deployment's members MUST be pairwise
   disjoint, and a violation is detected: before claiming a shard a member
   takes the shard's lease in the state store under its `member_id`, a
   refused lease fails the start with `shard_owned_elsewhere` and exit 1,
   the lease is renewed every maintenance interval, and a member whose
   lease has lapsed to another owner stops with `shard_lease_lost` and exit
   1 (ADR-0013 decision 7). The two-owners hazard therefore lasts at most
   one maintenance interval past a lapsed lease; the store still carries no
   fencing token on `record_transition`.
2. **Clause 3** — the sentence "so they land in one partition of that
   topic in publish order, and the single-writer trie (this ADR) applies
   each partition in log order" is now: all of an IP's transitions are
   published to `hammertime.hot-ip.v1.<partition_for(ip, 32)>` on one
   stream whose sequence is monotonic across every subject; the emitter
   awaits each publish's acknowledgement before the next (ADR-0011
   decision 4 step 4), so an IP's transitions carry strictly increasing
   stream sequences in emission order; the trie applies the stream in
   sequence order from its snapshot's `replay_position` (ADR-0013
   decision 9). Everything the clause says about prefixes, gaps and holes
   is unchanged.
3. **Clause 6** — was: "no double count and no loss at a rebalance. ADR-0003
   Amendment 2 (every redelivery lands in a window that never counted it)
   and ADR-0011 A20 (a member commits only handled positions, so the
   message in hand at a revocation reaches the next owner)." Now: no double
   count and no loss at a handover. A member acknowledges exactly the
   messages it has handled (ADR-0013 decision 8); a message it fetched and
   did not handle is negatively acknowledged when its consumer closes and
   is redelivered to the next owner at once; a copy the log redelivers of
   a message the member already handled under its current claim is
   recognised by its offset and acknowledged without being applied
   (ADR-0013 decision 5, `REDELIVERED`; ADR-0003 Amendment 3).
4. **Assumptions.** The Kafka ordering citation is replaced by ADR-0013
   decision 4's argument (one stream sequence; awaited acknowledgement; no
   client-internals reasoning needed). The Redpanda bullet is moot: the
   reference deployment runs `nats:2.15.0-alpine` and there is no
   substitute broker (ADR-0012 Amendment 2). The `DefaultPartitioner`
   bullet is replaced by `hammertime.bus.topics.partition_for` (FNV-1a
   32-bit over the UTF-8 key, modulo the count — ADR-0013 decision 1,
   assumption 4), which is deterministic by construction and testable in
   process. The "not enforced" bullet is replaced by decision 7's lease;
   the cost of a violation is now "refused at start, or stopped within one
   maintenance interval" rather than "colliding `event_id`s indefinitely".

Assumptions made by this amendment (push back individually):

* **Clause 5's "rebalance time" is read as handover time** — the interval
  between one member's `stop()` releasing a shard and another's `start()`
  claiming it — rather than being reworded in place. The bound it gives
  (`window_seconds` plus that interval plus one maintenance interval) is
  unchanged in form.
* **The narrow fencing is stated as a promise.** It is a consequence of
  ADR-0013 decision 7 rather than a new mechanism here; listing it under
  "promised" with its bound is judged more useful than leaving "fencing
  of a zombie owner" in the "not promised" list unqualified.
* **No CHANGES entry.** The observable changes (required
  `HAMMERTIME_SHARD_IDS`, the lease records) are ADR-0013's and are
  recorded by the change that implements it.
