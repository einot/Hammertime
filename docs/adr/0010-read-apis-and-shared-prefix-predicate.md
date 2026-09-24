# ADR 0010 — One prefix predicate, two read APIs, and the event granularity between trie and detector

Status: accepted; amended 2026-09-21 (see "Amendment 1" at the end — the
replay-position item the Consequences deferred is settled by ADR-0013
decision 9 now that the event log has one stream sequence, and decision
3's "flushed before the consumer position ... is committed" is restated
for a trie that keeps no consumer position; both noted in place); amended
2026-09-23 (see "Amendment 2" at the end — by ADR-0017: the trie's
`event_sequence` is its position in the hot-ip log, and Amendment 1's
two numbers are one; an event that leaves the hot set unchanged publishes
no stats; every `PrefixStatsChanged` carries its prefix as `subject`;
Amendment 1's readiness test also reads the log's first retained offset;
each noted in place); amended again 2026-09-23 (see "Amendment 3" at the
end — by ADR-0017 Amendment 2: decision 3's reporting range is per address
family, and IPv6 reports `/104` through `/128` by default under a key of
its own, `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6`; decision 3 and the
assumption on the minimum length each carry a dated note); amended a fourth
time 2026-09-24 (see "Amendment 4" at the end — by ADR-0017 Amendment 3:
decision 3's 2026-09-21 note says the snapshot alone records the
invariant, and a trie start now also reads how far its stats reached from
the last message of the prefix-stats log; decision 3 carries a dated note);
amended a fifth time 2026-09-24 (see "Amendment 5" at the end — by ADR-0017
Amendment 4: decision 1's predicate refuses arguments outside its domain;
IPv6's `matched_prefixes` are `/104`, `/112` and `/120`; `GET
/prefixes/hot` lists each served family's prefixes from its reporting floor
to the host route; a query for a family the trie does not hold is refused;
the trie keeps no cached `prefix_state`; decisions 1, 2, 4 and 5, one
assumption and one Consequences bullet carry dated notes)

Scope note: this ADR pins down the interfaces the cross-service tests of
issue #26 (`docs/spec/integration-scenarios.md`) observe the pipeline through,
and resolves three ambiguities the spec leaves open on the trie -> detector
edge. It does not design the aggregator's sliding-window internals, the trie's
node layout, or the detector's multi-dimensional scorer (§14) — those stay with
their epics. Wire shapes are specified in `docs/protocol/read-api-v1.md`.

## Context

The pipeline's observable outputs are the trie's read path (§29) and the
detector's "current detections" view (`services/detector/api.py`'s stub:
"Read API for current detections, with classification timestamps and
versions", §22). Both are unspecified beyond §29's two example responses, and
three questions have to be answered before anyone can write a test — or a
service — against them:

1. **Who classifies?** §29 shows `GET /prefix/...` returning
   `"state": "HOT_PREFIX"` from the *trie*, and §9/§12 give the trie node a
   cached `prefix_state`. §14 says "the scoring algorithm SHOULD remain
   separate from trie maintenance", `PrefixStatsChanged` carries no state, and
   the detector's `rules/baseline.py` stub calls itself "the v1 rule". If both
   services evaluate the rule independently, `GET /prefix` and
   `GET /detections` can disagree about the same prefix.
2. **What states exist?** `hammertime.core.state.enums.PrefixState` has
   `NORMAL`, `HOT_PREFIX`, `BOT_NETWORK`. §13/§38 define only the
   `HOT_PREFIX` predicate. §42 says the /24 in the worked example "=
   BOT_NETWORK_CANDIDATE", §37 counts `bot_network_candidates`, and §15
   contrasts an individual hot IP with a "bot network" without defining a
   threshold for the latter.
3. **What does the trie emit, and for which prefixes?** `PrefixStatsChanged`
   is keyed by prefix and carries `(prefix, hot_count, capacity, sequence,
   timestamp[, hot_ratio])`. A single transition changes `hot_count` on 32
   ancestors (§10). The detector can only classify prefixes it hears about.

A fourth question is upstream of all three and blocks the aggregator epic just
as much: an agent message carries `window_start` and `window_seconds`
(1..3600), but the aggregator's bucket is `bucket_seconds` (default 10). Nothing
says whether a 60-second observation is applied to one bucket or spread over
six. `docs/protocol/observation-v1.md` says "a delta for the bucket starting
at `window_start`", ADR-0002 buckets on `window_start` only.

## Decision

### 1. The `HOT_PREFIX` predicate has one implementation, in `hammertime-core`

```python
# hammertime.core.state.prefix   (new module; Spec: section 13, section 38)
def evaluate_prefix_state(hot_count: int, capacity: int, config: DetectionConfig) -> PrefixState:
    """HOT_PREFIX iff hot_count >= config.minimum_hot_ips and
    Fraction(hot_count, capacity) >= config.minimum_hot_ratio; else NORMAL."""
```

It is to prefixes what `evaluate_ip_state` is to IPs (§30): the only place the
comparison is written. The ratio is compared exactly (`Fraction`, as
`Prefix.hot_ratio` already computes it) so that `hot_count / capacity ==
minimum_hot_ratio` qualifies on every platform. Both the trie's read path and
the detector's `rules/baseline.py` call it; `baseline.py` becomes the detector's
adapter over it (feeding it the latest `PrefixStatsChanged` per prefix), not a
second copy of the comparison.

Consequently `GET /prefix/{cidr}` (trie) and `GET /detections` (detector) can
never disagree about whether a prefix qualifies *for the same*
`(hot_count, capacity, config_version)` — they can only differ by the
propagation delay §22 already allows, which both responses expose via
`event_sequence`.

> Amended 2026-09-24 (ADR-0017 Amendment 4 ruling 1; Amendment 5): the
> function checks its arguments before it compares. A `hot_count` or
> `capacity` that is not an `int`, or is a `bool`, is a `TypeError`. A
> `capacity` below 1, or a `hot_count` below 0 or above `capacity`, is a
> `ValueError`. Within that domain the docstring's "iff" is exact. The
> trie's read routes are its first callers. For the detector, a
> `PrefixStatsChanged` whose `hot_count` exceeds its `capacity` raises here,
> and what its worker then does is the detector epic's to rule.

### 2. v1 emits exactly two prefix states; `BOT_NETWORK` is reserved

`PrefixState.NORMAL` and `PrefixState.HOT_PREFIX` are the only values a v1
service produces. `BOT_NETWORK` stays in the enum for §14's multi-dimensional
scorer and is never emitted until that scorer exists and a later ADR defines
its predicate. §42's "BOT_NETWORK_CANDIDATE" is read as *"a most-specific
qualifying prefix"* (§31): the detector's minimal set. §37's metrics are
defined accordingly:

```text
hot_prefixes            number of prefixes currently HOT_PREFIX (all lengths)
bot_network_candidates  number of prefixes in the minimal set (§31)
classification_changes  count of NORMAL <-> HOT_PREFIX edges observed
```

> Noted 2026-09-24 (ADR-0017 Amendment 4, its assumption 111; Amendment 5):
> under the default document a `/28` whose sixteen addresses are all HOT
> qualifies. The most specific qualifying prefixes of §42's example, the
> 156 HOT addresses from `10.20.30.1`, are therefore eight `/28`s, not the
> `/24` §42 calls a candidate. That follows from this decision's reading
> and from §13. Whether §42 should be read otherwise is raised for the
> repository owner with ADR-0017 Amendment 4, and is not ruled here.

### 3. The trie emits `PrefixStatsChanged` for every ancestor from a minimum length down to the host route

On each applied `HotIpAdded`/`HotIpRemoved`, after the single-writer update
(§28), the trie publishes one `PrefixStatsChanged` per ancestor prefix of the
IP whose length is in `[min_prefix_length, bit_length]`, i.e. `/8` through
`/32` for IPv4 with the default `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH=8`. Each
carries that prefix's new `hot_count`, its `capacity`, the computed
`hot_ratio`, and the trie's own monotonically increasing `sequence` (one
counter for the whole service, incremented per applied hot-IP event, shared by
all prefixes of that event so a consumer can group them). Events for one
hot-IP event are published before the next hot-IP event is applied; the
producer is flushed before the consumer position for the hot-ip topic is
committed, so a crash cannot commit an update whose stats were never emitted.

> Amended 2026-09-21 (ADR-0013 decision 9; Amendment 1): the trie reads
> `hammertime.hot-ip.v1` positionally from its snapshot's `replay_position`
> and holds no consumer position on the log. The last sentence therefore
> reads: the producer is flushed before a snapshot records a
> `replay_position` that covers the event, so a restart cannot skip an
> event whose stats never reached the log. The invariant is the same; the
> thing that records it is the snapshot alone.

> Amended 2026-09-23 (ADR-0017 decisions 8 and 14; Amendment 2): three
> qualifications of the paragraph above.
>
> 1. "On each applied `HotIpAdded`/`HotIpRemoved`" means each one that
>    changed the hot set. An event the trie absorbs as a no-op (ADR-0014
>    decision 3) publishes nothing, because no prefix's count moved.
> 2. "The trie's own monotonically increasing `sequence` (one counter for
>    the whole service, incremented per applied hot-IP event ...)" is not
>    a counter. It is the trie's `event_sequence`: one past the stream
>    offset of the hot-ip record that caused the stats. It is still
>    strictly increasing, shared by the stats of one event, and not dense.
> 3. Each of an event's messages sets the envelope's `subject` to the
>    prefix text. Without it, the stats of one event would share one
>    `event_id`, and the log's `Nats-Msg-Id` deduplication (ADR-0013
>    decision 4) would keep one of them.

> Amended 2026-09-23 (ADR-0017 Amendment 2; Amendment 3): the range above
> is per address family. For IPv4 it is as written, `[L, 32]`, with `L`
> from `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH`, default 8. For IPv6 it is
> `[L, 128]`, with `L` from `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6`,
> default 104. A `/104` holds as many addresses as an IPv4 `/8`, 2^24, so
> the capacity argument below gives IPv6 the same floor, and an IPv6
> transition publishes 25 messages as well.

> Noted 2026-09-24 (ADR-0017 Amendment 3 ruling 1; Amendment 4): the
> 2026-09-21 note's "the thing that records it is the snapshot alone" no
> longer holds. The snapshot records where the replay starts. How far the
> stats reached the log is read from the log: at `start()` the trie reads
> the last message of `hammertime.prefix-stats.v1`, and re-publishes the
> events it replays only from that message's `sequence`. The invariant is
> unchanged.

Twenty-five messages per transition is acceptable for the same reason
ADR-0005 gave for not scoring continuously: hysteresis makes transitions rare
relative to observations, and `PrefixStatsChanged` is keyed by prefix so the
detector's consumption parallelises across partitions. Prefixes shorter than
the minimum are not reported because no plausible `minimum_hot_ips`/
`minimum_hot_ratio` pair can be met there (a /7 needs 3.3 million hot IPs at
the default 10 %), and reporting them would only add eight more messages per
transition that are always `NORMAL`.

### 4. The trie's read API

`GET /prefix/{cidr}` returns the node's `hot_ips` (§29's name for
`hot_count`), `capacity`, `hot_ratio`, and `state` computed by decision 1
against the trie's *current* config; `GET /ip/{addr}` returns the IP's state,
the `window_count` carried on its most recent `HotIpAdded` as `request_count`,
its `attributes` while HOT (§46.7), and `matched_prefixes` for the ancestors at
the reporting lengths (assumption below). `GET /prefixes/hot` lists every
`HOT_PREFIX` node, `?minimal=true` reducing it to the most-specific ones (§31).
All responses carry `as_of`, `event_sequence` (the sequence of the last
hot-ip event applied) and `config_version` (§22). A prefix or IP with no node
is a valid, zero-valued, `NORMAL`/`COLD` answer, not a 404: absence of a node
*is* the trie's statement that nothing beneath it is hot.

> Amended 2026-09-23 (ADR-0017 decision 8; Amendment 2): "`event_sequence`
> (the sequence of the last hot-ip event applied)" is the trie's position
> in the hot-ip log: the stream offset of the next record it will read.
> `as_of` is the greatest `timestamp` among the hot-ip events it has
> applied, and `null` until it has applied one.

> Amended 2026-09-24 (ADR-0017 Amendment 4 rulings 2 and 5; Amendment 5):
> three things this decision left open for the trie are settled.
>
> 1. IPv6's `matched_prefixes` are `/104`, `/112` and `/120`. In both
>    families they are the ancestors with 24, 16 and 8 host bits, so IPv4's
>    stay `/8`, `/16` and `/24`.
> 2. `GET /prefixes/hot` lists, for each family the trie serves, every
>    prefix from that family's reporting floor (decision 3, as noted) to the
>    host route that holds a HOT address and is `HOT_PREFIX`, IPv4 before
>    IPv6. "Every `HOT_PREFIX` node" reads "every such prefix": the Patricia
>    trie has no node for every prefix (ADR-0014 decision 6). A request
>    finds the list by a walk that descends only where a prefix could
>    qualify, and the trie keeps no index.
> 3. A query for an address or a prefix of a family the trie does not serve
>    is `400`. The zero-valued answer above is the trie's statement that
>    nothing beneath a prefix is hot, and it can make none about a family it
>    does not hold.
>
> The grammar, the order of the checks and the error texts are
> `read-api-v1.md`'s.

### 5. The detector's read API reflects classification immediately; debounce applies to alerts only

`GET /detections` lists every prefix whose latest known stats qualify, with
`?minimal=true` for the §31 set. The view is updated the moment a
`PrefixStatsChanged` is applied; `worker.py`'s debounce ("so noisy counts do
not spam alerts") governs only what is *published* to the detections topic and
any future alert sink, never what the read API reports. Each item carries the
`event_sequence` of the stats that produced it and `since`, the timestamp at
which the prefix most recently entered `HOT_PREFIX` (reset when it leaves).

On a configuration change (ADR-0009 decision 6) the detector re-runs decision
1 over every prefix it holds stats for; the trie re-evaluates `state` lazily at
read time and eagerly for the cached `prefix_state` (§12).

> Amended 2026-09-24 (ADR-0017 Amendment 4 ruling 5; Amendment 5): the trie
> keeps no cached `prefix_state`. It re-evaluates `state` lazily at read
> time only, and "eagerly for the cached `prefix_state` (§12)" has nothing
> to act on. Adopting the new document is the trie's whole re-evaluation
> (ADR-0017 decision 10).

### 6. An observation's delta lands in exactly one bucket

The aggregator applies a `RequestObservation` entry's `request_count` to the
bucket containing `window_start` — `bucket_start(window_start, bucket_seconds)`
(§25) — in full. `window_seconds` on the message is descriptive of how the
agent counted; it is used by ingest for the dedup TTL (ADR-0003) and by the
aggregator only to reject a message whose `window_seconds` exceeds the
configured `window_seconds` (which cannot be represented in the ring at all).
It is never used to spread the delta. This is what
`docs/protocol/observation-v1.md` already says and what makes the protocol's
"window_start MUST be aligned to `bucket_seconds`" requirement meaningful.

## Assumptions

* **Predicate in core, not a shared import between services.** Services must
  not import each other; `hammertime-core` is the only shared home. The
  alternative — the detector as sole classifier and the trie returning no
  `state` — contradicts §29's response shape verbatim.
* **`BOT_NETWORK` unused in v1.** §13/§38 give one predicate; inventing a
  second threshold for "bot network" without a spec basis would put a number
  nobody asked for into the enum's semantics. §42's wording is treated as
  descriptive.
* **`bot_network_candidates` = size of the minimal set.** One of several
  possible readings of §37; chosen because §31's minimal set is the thing an
  alerting system acts on.
* **Minimum reported prefix length 8 (IPv4), a trie setting.** The number
  comes from the capacity argument in decision 3, not from the spec. IPv6's
  default is left undefined because v1 is IPv4-only (§43); the setting must
  become per-family when §35 is implemented.

  > Settled 2026-09-23 (ADR-0017 Amendment 2; Amendment 3): IPv6's default
  > is `/104`, and the setting is per family.
  > `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` stays IPv4's, and
  > `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6` is IPv6's.
* **One trie-wide `sequence` shared by the stats of one hot-IP event.** The
  schema requires a `sequence`; per-prefix counters would give the detector no
  way to tell which stats belong together. Chosen for groupability; a future
  consumer that needs per-partition monotonicity has it anyway, because all
  stats for one event are emitted before the next.
* **`request_count` on `GET /ip` is the transition-time `window_count`.** The
  trie holds no counters (§18); this is the only count it has ever been told.
  It is stale by design and documented as such in the protocol doc. `0` for a
  COLD IP.
* **`matched_prefixes` reports IPv4 lengths 8, 16 and 24.** §29's example
  shows exactly those three; returning all 32 ancestors is noise. A query
  parameter can widen this later without breaking the default.

  > Settled 2026-09-24 (ADR-0017 Amendment 4 ruling 2; Amendment 5): IPv6
  > reports `/104`, `/112` and `/120`, the ancestors with as many host bits
  > as IPv4's three.
* **Zero-valued answers instead of 404 for unknown prefixes/IPs.** A 404 would
  force every client to special-case "not hot" versus "not found" when the
  trie means the same thing by both.
* **Detector read view is undebounced.** Debounce is about alert fatigue,
  not truth; a read API that lags its own inputs by a debounce interval
  cannot be tested deterministically and gives operators a stale answer.
* **`since` resets on leaving HOT_PREFIX.** §14 lists "persistence over time"
  as a future scoring input; `since` is the minimum needed to compute it
  later without changing the response shape.
* **One bucket per observation.** The protocol doc and ADR-0002 both imply
  it; the alternative (spreading a delta across `window_seconds /
  bucket_seconds` buckets) would make an agent's choice of `window_seconds`
  change when an IP crosses `hot_threshold`, which no spec text supports. An
  agent that wants finer attribution sends finer windows.

## Consequences

* New core module `hammertime.core.state.prefix`; `services/detector/rules/
  baseline.py` and the trie's read path call it. `docs/spec/README.md` maps
  §13/§38 to both.
* `schemas/prefix_stats_event.v1.json` is unchanged (`hot_ratio` was already
  optional; the trie now always sets it). No `CHANGES` entry for that; the
  read APIs themselves get one line each when they ship.

  > Noted 2026-09-24 (ADR-0017 Amendment 4 ruling 11; Amendment 5): the
  > trie's read API ships with four lines, which that ruling gives.
* The trie's outbound volume is ~25 messages per transition. `trie_updates`
  and `hot_transition_to_prefix_update_latency` (§37) remain the signals
  that say when this, or ADR-0001's single writer, needs revisiting.
* A detections *topic* (`HAMMERTIME_TOPIC_DETECTIONS` exists in
  `.env.example` with no schema or `topics.py` entry) is still undefined.
  Nothing in #26's scenarios needs it; it is the detector epic's first
  interface question and will be its own ADR.
* How the trie snapshot records its replay position for a multi-partition
  `hammertime.hot-ip.v1` (§33's single "event sequence number" is not a Kafka
  offset) is likewise deferred to the trie epic; the scenarios only observe
  the result (exact counts after restart), not the format.

> Amended 2026-09-21: settled by ADR-0013 decision 9 — see Amendment 1.

## Amendment 1 (2026-09-21) — the trie's replay position is the stream sequence (ADR-0013)

Why: the last Consequences bullet deferred how the trie snapshot records
its replay position, because with a multi-partition Kafka topic §33's
single "event sequence number" had no counterpart — a position was a
vector of per-partition offsets. ADR-0013 makes the event log a NATS
JetStream stream, which has one monotonic sequence across every subject,
and `ConsumedMessage.offset` is that sequence. The item is answerable and
this amendment answers it; decisions 1, 2, 4, 5 and 6 are untouched, and
decision 3 is restated in one sentence for a trie that keeps no consumer
position.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Was "Status: accepted". Now adds the amended clause.
* **Decision 3, dated blockquote after the first paragraph.** The sentence
  "the producer is flushed before the consumer position for the hot-ip
  topic is committed, so a crash cannot commit an update whose stats were
  never emitted" is restated in the blockquote; the original text is
  unchanged.
* **Consequences, last bullet, dated one-line blockquote** pointing here.

Ruling (ADR-0013 decision 9, restated so it can be read from this ADR):

1. The trie subscribes to `hammertime.hot-ip.v1` positionally —
   `Consumer.subscribe(topic, start_offset=<replay_position + 1>)`, or
   `start_offset=1` with no snapshot — applies messages in the order the
   iterator yields them, and never acknowledges. There is no
   `hammertime-trie` durable consumer; ADR-0009 decision 9's name is
   unused by the trie.

   > Amended 2026-09-23: "`start_offset=1` with no snapshot" is stale.
   > ADR-0013 Amendment 1 ruling C5.7 changed it to `start_offset=0` on
   > 2026-09-21; ADR-0017 decision 3 gives the trie's start as built.
2. The snapshot records `replay_position: int`, the `offset` of the last
   hot-ip message applied before the snapshot was written. That integer is
   §33's "event sequence number"; after loading a snapshot the trie replays
   every message with `offset > replay_position`.
3. The trie's `event_sequence` — its count of applied hot-ip events,
   carried on `PrefixStatsChanged.sequence` and in every read response
   (decision 4; ADR-0001 Amendment 1 clause 4) — is **unchanged** and is a
   different number from `replay_position`. Whether the two should be one
   (the stream sequence satisfies every property decision 4's assumption
   "one trie-wide `sequence` ... chosen for groupability" asks for: strictly
   increasing, shared by the twenty-five stats of one event, not promised
   dense) is left to the trie epic and is named as an open question in
   ADR-0013's hand-off report; it would touch `docs/protocol/read-api-v1.md`
   and ADR-0001 clause 4, which this amendment does not.

   > Amended 2026-09-23 (ADR-0017 decision 8; Amendment 2): decided by
   > the repository owner on 2026-09-23, on ADR-0017's recommendation —
   > the two are one integer, the trie's log position. In ADR-0017's
   > form, `event_sequence` is `replay_position + 1`, the offset of the
   > next hot-ip record the trie will read, and not a count.
   > Ruling 2's "the `offset` of the last hot-ip message
   > applied" now reads "handled or passed". A record the trie skips
   > (malformed, or of a family it does not serve) is handled too. An
   > offset the log no longer holds when the trie starts is passed
   > (ADR-0017 decision 4). Neither is read again after a restore.
   > `docs/protocol/read-api-v1.md` and ADR-0001 clause 4
   > are edited in the same change set.
4. Readiness (ADR-0009 decision 4: replayed "to the log end as it stood
   when `start()` began") uses the bus's `await bus.end_offset(topic)`,
   read at `start()` — the offset the next appended message will receive,
   `1` for an empty stream and `0` for an empty memory log. The service
   has replayed to the log end once the last applied offset is
   `>= end_offset - 1`, or immediately when `end_offset <= start_offset`
   (ADR-0013 decision 9 and assumption 20, as amended).

   > Amended 2026-09-21: was "uses the bus's `last_offset(topic)` read at
   > `start()` (ADR-0013 assumption 20)". ADR-0013 Amendment 1 (ruling
   > C5.2) renamed it `end_offset`, made it `async` on the `MessageBus`
   > protocol, and redefined it as the next offset so that one readiness
   > inequality holds on both buses.

   > Amended 2026-09-23 (ADR-0013 Amendment 10; Amendment 2): the test
   > above never completes on a JetStream stream that holds no record at
   > or after `start_offset` and below `end_offset`. A freshly provisioned
   > stream is one: its `end_offset` is `1`. A positional reader now also
   > reads `first_offset(topic)`, after `end_offset` and before it
   > subscribes, and counts every offset below it as passed. The trie
   > moves its position there before subscribing, so it is caught up once
   > `event_sequence >= end_offset` (ADR-0017 decision 4).
5. The detector is bound by ADR-0013 decision 9's redelivery constraint:
   it holds a durable subscription, so after a crash it may be handed an
   older `PrefixStatsChanged` after a newer one for the same prefix, and
   its "latest known stats" view (decision 5) MUST apply an event only if
   its `sequence` is greater than the one it holds for that prefix.

Assumptions made by this amendment (push back individually):

* **Two numbers rather than one.** Unifying `event_sequence` with the
  stream sequence is attractive (§33 would then name one integer and the
  read APIs would expose a real log position) but widens this amendment
  into the read-API protocol and ADR-0001 clause 4. Kept separate so that
  ADR-0013 lands without a protocol change; raised for the owner.
* **The field name `replay_position`** is chosen here so the trie epic and
  `tools/replay` agree on it; the snapshot format is otherwise the trie
  epic's.
* **No CHANGES entry**: no snapshot format has shipped.

## Amendment 2 (2026-09-23) — the trie's `event_sequence` is its log position; only a change is published; stats carry their prefix as `subject`; readiness reads the first retained offset (ADR-0017)

Why: ADR-0017 designs the trie service (epic #10). It answers three
questions this ADR left open or stated too loosely, and corrects one test
this ADR restated:

* Amendment 1 ruling 3 left open whether the trie's `event_sequence` and
  `replay_position` are one number. The repository owner decided on
  2026-09-23, on ADR-0017's recommendation, that they are one, and
  ADR-0017 decision 8 records it. Amendment 1's assumption "Two numbers
  rather than one" had raised the question for the owner, while ADR-0013
  decision 9 left it to the trie epic; the owner's decision makes that
  disagreement moot.
* Decision 3 said stats are published "on each applied" event, and
  ADR-0011's Consequences left it to the trie epic whether that includes
  an event the trie absorbs as a no-op. ADR-0017 decision 14 rules that it
  does not.
* Decision 3's twenty-five messages share one `sequence`, and nothing here
  said they must differ in `subject`. Without it they share one
  `event_id`, and the log's deduplication keeps one of them.
* Amendment 1 ruling 4 restated ADR-0013 decision 9's readiness test.
  The security audit of ADR-0017's first slice found that the test never
  completes on a JetStream stream that holds no record to replay.
  ADR-0013 Amendment 10 corrects it, and ADR-0017 decision 4 applies it
  to the trie. (Added 2026-09-23, before this amendment was merged.)

Decisions 1, 2, 5 and 6 are unchanged. Decisions 3 and 4 and Amendment 1
rulings 3 and 4 carry dated notes; their text is unchanged.

Every edit outside this section:

* **Status line.** Gained the "amended 2026-09-23" clause.
* **Decision 3.** A dated blockquote after its 2026-09-21 blockquote, with
  three numbered qualifications: only an event that changed the hot set
  publishes; the shared `sequence` is the trie's log position, not a
  counter; each message's `subject` is its prefix.
* **Decision 4.** A dated blockquote after the paragraph. It re-reads
  "`event_sequence` (the sequence of the last hot-ip event applied)" as the
  log position, and defines `as_of`, which nothing here had defined.
* **Amendment 1 ruling 3.** A nested dated blockquote records the owner's
  decision on the open question and re-reads ruling 2's "applied" as
  "handled or passed".
* **Amendment 1 ruling 4.** A dated blockquote, after its 2026-09-21 one,
  points to ADR-0013 Amendment 10's corrected readiness test.

Assumptions made by this amendment (push back individually):

* **Pointer notes, not rewritten decisions.** Every ruling and its
  reasons are recorded in ADR-0017. The unification of the two numbers is
  the owner's decision, taken on ADR-0017's recommendation; the other
  rulings are ADR-0017's own. The notes stop this ADR's text from stating
  what is no longer true.
* **The `subject` rule is recorded here as well as in ADR-0017.** It binds
  whoever implements decision 3's publisher. This ADR is where that
  publisher's shape was fixed, so it is the first place such a reader
  looks.
* **No CHANGES entry.** No trie build that publishes stats has shipped.
  The implementing change of ADR-0017's slice 2 records the publisher.

## Amendment 3 (2026-09-23) — the reporting range is per family; IPv6 reports `/104` to `/128` (ADR-0017 Amendment 2)

Why: decision 3 fixed the reporting range as `[min_prefix_length,
bit_length]` and gave IPv4's default. The assumption "Minimum reported
prefix length 8 (IPv4), a trie setting" left IPv6's default undefined, and
added that "the setting must become per-family when §35 is implemented".
ADR-0017 implemented §35 in its first slice, and its Amendment 2 designs
the publisher. Ruling 1 there settles IPv6's range and the per-family
setting. This amendment records that in the two places.

Decisions 1, 2, 4, 5 and 6 are unchanged, and so is decision 3's text.

Every edit outside this section:

* **Status line.** Gained the "amended again 2026-09-23" clause.
* **Decision 3.** A dated blockquote after its 2026-09-23 one: the range
  is per family, and IPv6's floor is `/104`.
* **Assumptions, "Minimum reported prefix length 8 (IPv4), a trie
  setting."** A dated blockquote under the bullet. The bullet's text is
  unchanged.

Assumptions made by this amendment (push back individually):

* **Pointer notes, not rewritten decisions.** The ruling, its reasons and
  its alternatives are ADR-0017 Amendment 2's: ruling 1 and assumptions 41
  to 43. The notes stop this ADR from leaving open what is now settled.
* **No CHANGES entry from this amendment.** ADR-0017 Amendment 2 ruling 11
  gives the implementing change's lines.

## Amendment 4 (2026-09-24) — how far the trie's stats reached is also read from the prefix-stats log (ADR-0017 Amendment 3)

Why: decision 3's 2026-09-21 note restated the invariant "a restart cannot
skip an event whose stats never reached the log" for a trie with no
consumer position, and added "the thing that records it is the snapshot
alone". ADR-0017 Amendment 3 ruling 1 has every trie start read the last
message of `hammertime.prefix-stats.v1` and re-publish the events it
replays only from that message's `sequence`, so that a start no longer
re-publishes the whole retained log. The snapshot is no longer the only
record. The invariant is unchanged, and so are decisions 1, 2, 4, 5 and 6.

Every edit outside this section:

* **Status line.** Gained the "amended a fourth time 2026-09-24" clause.
* **Decision 3.** A dated blockquote after its Amendment 3 note. The
  2026-09-21 note's text is unchanged.

Assumptions made by this amendment (push back individually):

* **A pointer, no ruling.** The rule, the argument that it keeps the
  invariant, and what it leaves open are ADR-0017 Amendment 3's (ruling 1).
  The note stops this ADR's text from saying what no longer holds.
* **No CHANGES entry from this amendment.** ADR-0017 Amendment 3 ruling 7
  gives the implementing change's line.

## Amendment 5 (2026-09-24) — the trie's read API as designed: the predicate's domain, IPv6's matched prefixes, what `GET /prefixes/hot` lists, a family not served, and no cached `prefix_state` (ADR-0017 Amendment 4)

Why: ADR-0017 Amendment 4 designs slice 3 of epic #10: this ADR's predicate
(decision 1) and the trie's half of its read API (decision 4). It settles
five things this ADR left open, stated for IPv4 only, or stated for a cache
the trie does not keep:

* decision 1 gave the comparison and not its domain;
* the assumption on `matched_prefixes` gave IPv4's lengths only;
* decision 4 said `GET /prefixes/hot` lists "every `HOT_PREFIX` node", and
  said neither which lengths nor which families;
* nothing said what a query for a family the trie does not hold answers;
* decision 5 has the trie re-evaluate "eagerly for the cached
  `prefix_state`", and the trie keeps none.

Designing it also showed what decision 2's reading of §42 yields under the
default document. That is raised for the repository owner, and is not ruled
here.

Decisions 3 and 6 are unchanged, and so is the text of every decision.

Every edit outside this section:

* **Status line.** Gained the "amended a fifth time 2026-09-24" clause.
* **Decision 1.** A dated blockquote after its last paragraph: the argument
  checks.
* **Decision 2.** A dated blockquote after the metrics block: §42's
  minimal set under the default document.
* **Decision 4.** A dated blockquote after its 2026-09-23 note: three
  settlements.
* **Decision 5.** A dated blockquote after its second paragraph: no cached
  `prefix_state`.
* **Assumptions, "`matched_prefixes` reports IPv4 lengths 8, 16 and 24."** A
  dated blockquote under the bullet.
* **Consequences, the bullet on `schemas/prefix_stats_event.v1.json`.** A
  dated blockquote under the bullet.

Assumptions made by this amendment (push back individually):

* **Pointer notes, not rewritten decisions.** The rulings, their reasons and
  their alternatives are ADR-0017 Amendment 4's (rulings 1, 2 and 5;
  assumptions 90 to 94, 98, 100 and 111). The notes stop this ADR from
  leaving open what is now settled.
* **The detector's list inherits the family order.** `read-api-v1.md`
  orders the detector's list "same as the trie's list", so IPv4 before IPv6
  binds it too. The detector epic may revisit that; nothing here designs
  the detector.
* **The note on decision 2 records a consequence, not a ruling.** The
  numbers follow from §13 and the reading decision 2 already made. Whether
  that reading should change is the owner's question.
* **No CHANGES entry from this amendment.** ADR-0017 Amendment 4 ruling 11
  gives the implementing change's lines.
