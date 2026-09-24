# ADR 0017 — The trie service: a positional-replay worker on one event loop, the log position as `event_sequence`, and the slices of epic #10

Status: accepted 2026-09-23 (epic #10). It amends three earlier ADRs, each
at a dated note in place plus a short amendment section listing its notes:
ADR-0001 (Amendment 4), ADR-0010 (Amendment 2) and ADR-0013 (Amendments 9
and 10). Spec §22's note is reworded, and §28, §33 and §35 gain notes.
`docs/protocol/read-api-v1.md`, `docs/spec/integration-scenarios.md` §5,
`docs/runbook.md` and `docs/spec/README.md` are edited in step. "Edits to
other documents" at the end lists every edit and quotes what it replaced.
Revised in place on 2026-09-23, before merge, for the two findings of the
slice-1 security audit: a hot-ip log that holds nothing to replay no longer
holds readiness back (decision 4 step 3; ADR-0013 Amendment 10 adds
`MessageBus.first_offset`), and the admin app serves no generated API
documentation (decision 13). "Revision 2026-09-23" at the end lists every
edit of this revision and quotes what it replaced.
Amended 2026-09-23, after merge (see "Amendment 1" at the end), for four
items flagged during slice 1: assumption 19 gains a fourth readiness case,
the hot-ip stream deleted while `start()` runs; `docs/runbook.md` says for
which of assumption 19's cases a longer deadline does not help; ADR-0010
Amendment 1 ruling 1 gains a note on its stale `start_offset=1`; and
ADR-0013 Amendment 11 rules which of their two errors `NatsBus`'s offset
reads raise when both apply.
Amended again 2026-09-23 (see "Amendment 2" at the end), to design slice 2,
the `PrefixStatsChanged` publisher. Decision 14 item 10's four points are
settled: IPv6 reports `/104` to `/128` under a key of its own, the service
keeps accepting `ipv6`, and the publisher does not measure
`hot_transition_to_prefix_update_latency`. Decisions 1, 2, 6, 7, 9, 11, 12,
13, 14 and 16, the Test seams and Consequences carry dated notes. ADR-0010
(Amendment 3), ADR-0013 (Amendment 12) and ADR-0016 (Amendment 1) carry
pointer notes.
Amended a third time 2026-09-24 (see "Amendment 3" at the end), for the
reviewer's and the security auditor's findings on slice 2. A start
re-publishes only from the last `sequence` that
`hammertime.prefix-stats.v1` holds, read through a new
`MessageBus.last_value` (ADR-0013 Amendment 13). The codec returns every
decoded timestamp in UTC and refuses one it could not write again.
`publish()`'s count and wait are pinned for a cancellation, and `stop()`
marks nothing. Whether to cap the streams is left with the owner and is
not ruled. Decisions 1, 4, 6, 7, 12, 13, 14 and 16, the Test seams,
Consequences and Amendment 2 carry dated notes. ADR-0010 (Amendment 4) and
ADR-0013 (Amendment 13) carry notes.
Amended a fourth time 2026-09-24 (see "Amendment 4" at the end), to design
slice 3, the read API. `evaluate_prefix_state` joins `hammertime.core.state`
and checks its arguments. The three routes, their inputs, bodies and
refusals are settled, a family the trie does not hold included. The routes
check readiness before anything else and read `TrieState` with no `await`.
The trie keeps no cached `prefix_state`: `GET /prefixes/hot` walks only the
prefixes whose count could qualify. The read API needs no prefix metadata,
and `prefix_queries` counts by route and result. The port's exposure is left
with the owner and is not ruled. Decisions 1, 2, 9, 10, 12, 13 and 15, the
Test seams and Consequences carry dated notes. ADR-0010 (Amendment 5) and
ADR-0015 carry notes, and so does `docs/protocol/read-api-v1.md`.

Scope note. This ADR settles what epic #10 ("Trie worker, publisher &
read-side query API") is built against. It splits the epic into slices
(decision 1) and designs slice 1 in full (decisions 2-13): the trie
service's settings, lifecycle, worker, read-state seam, metrics and admin
endpoints. Slices 2 (the `PrefixStatsChanged` publisher) and 3 (the read
API) are outlined. Decisions 14 and 15 take every question about them
that slice 1 already forces, and name what is left for their own design
pass. The snapshot (§32, §33, `services/trie/snapshot/`) is a separate
epic; decision 16 says what it inherits.

Two questions raised for this epic were ruled by the repository owner on
2026-09-23 and are tracked as issues of their own:

* a hot-ip event whose attributes are invalid (#115);
* where `request_count` is stored (#116).

This ADR designs neither and stays compatible with both (decision 17).

## Context

What exists. `services/trie/src/hammertime/trie/{__init__,__main__,config,worker,publisher}.py`
and `query/{__init__,app,views}.py` are docstring-only stubs.
`services/trie/Dockerfile` already builds and runs `hammertime-trie`, in
the same shape as the aggregator's. `deploy/docker-compose.yml` already
runs the trie with a `/readyz` healthcheck. The container crash-loops only
because `__main__.py` defines no `main()`. Two epics this one builds on
have shipped:

* epic #8 (ADR-0014): `PatriciaTrie`, `NodeArena` and the invariant checks;
* epic #9 (ADR-0015): `IpAttributeRecords`, `apply_hot_ip_added` and
  `apply_hot_ip_removed`, `PrefixStats` and `ancestor_stats`.

What earlier decisions already fix:

* ADR-0001: one logical trie owner, with a single writer per address
  family. Per IP, the trie applies transitions in emission order
  (Amendment 1 clause 3, as amended by Amendment 2).
* ADR-0009 fixes the trie's lifecycle:
  * `build_service`, the `Service` protocol, `app: FastAPI`,
    `reload_config()` and `snapshot_now()`;
  * readiness: the trie is ready once it has replayed
    `hammertime.hot-ip.v1` "up to the log end as it stood when `start()`
    began" (decision 4);
  * `trie_recovery_seconds` (decision 5 step 6);
  * the drain (decision 7, as amended: for the trie, "flush, then
    snapshot").
* ADR-0010 fixes the trie's outputs:
  * one prefix predicate (decision 1);
  * 25 `PrefixStatsChanged` per transition (decision 3);
  * the read API (decision 4);
  * Amendment 1: the trie replays positionally from its snapshot's
    `replay_position`.
* ADR-0013 decision 9: the trie subscribes to `hammertime.hot-ip.v1`
  positionally with `partitions=None`, never acknowledges, and reads
  readiness off `await bus.end_offset(topic)`.
* ADR-0014 fixes the structure's contract:
  * the `HotTrie` protocol;
  * a redundant add or remove returns `False` and changes nothing
    (decision 3);
  * a mutator that finds corruption it cannot walk past raises
    `InvariantViolation` before mutating anything, and the process must
    then be rebuilt, not kept in service (Amendment 2, A12).
* ADR-0015 fixes the coupled step:
  * decision 6: `apply_hot_ip_added` and `apply_hot_ip_removed` are the
    one way to move the trie and the record map together, and each of the
    three exceptions they can raise has a fixed meaning for the worker;
  * assumption 37: the worker counts `attributes_rejected`.
* ADR-0016: the codec refuses an out-of-range hot-ip integer as a
  `CodecError`.
* §28: readers must never observe a partially updated path, and a single
  writer is "strongly recommended".

What was open, and blocks anyone who wants to write a test or a module:

1. How the epic is split into pieces that can land on their own.
2. What the worker does with each message, each exception, and an event
   for a family it does not hold. ADR-0014 decision 1 left the last to
   "the worker epic".
3. How a reader gets a consistent view. Neither the structure nor the
   record map is thread-safe (ADR-0014 assumption 19; ADR-0015
   decision 8).
4. Whether the trie's `event_sequence` and the log's stream sequence are
   one number (ADR-0010 Amendment 1 ruling 3; ADR-0013 decision 9;
   ADR-0014 Consequences).
5. What `as_of` means. No text defines it.
6. Whether an event the trie absorbs as a no-op publishes
   `PrefixStatsChanged` (ADR-0011 Consequences; ADR-0014 decision 3).
7. What an `InvariantViolation` does to the process (ADR-0014 A12
   clause 5).
8. The trie's configuration keys and metrics.
9. Whether the read API needs the prefix metadata of §16 (ADR-0015
   assumption 7).

## Decision

### 1. The epic lands in three slices, with #116 between the first and the third

| Slice | Contents | Depends on |
| --- | --- | --- |
| 1 | `config.py`, `state.py` (new), `metrics.py` (new), `worker.py`, `service.py` (new, ADR-0009 decision 3), `__main__.py`, `query/app.py` (the three admin endpoints only); `.env.example`; `CHANGES`; in `hammertime-bus`, `MessageBus.first_offset` on both buses (ADR-0013 Amendment 10) | — |
| #116 | the owner's `request_count` ruling: an ADR-0015 amendment, `IpAttributeRecords`, `apply_hot_ip_added`, the invariant checkers, and the worker's two `apply_hot_ip_added` calls (decision 17) | slice 1 |
| 2 | `publisher.py` and its wiring into the worker and the service; in `hammertime-core`, `PrefixStatsChanged.hot_ratio` in the model and the codec; `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` | slice 1 |
| 3 | in `hammertime-core`, `state/prefix.py` (`evaluate_prefix_state`, ADR-0010 decision 1); `query/app.py`'s read routes and `query/views.py`; `prefix_queries` | slice 1, #116 |

* #116 and slice 2 are independent of each other and may run in parallel.
* Slice 3 needs #116, because `GET /ip/{addr}` returns `request_count`.
  `evaluate_prefix_state` has no dependency and may be pulled forward, for
  instance by the detector epic.
* The Dockerfile and the compose service need no change and get no slice.
* The snapshot epic comes after slice 2, because a snapshot must record
  only events whose stats have been flushed (decision 16). It must also
  come before any deployment runs longer than the hot-ip log's retention
  (Consequences).
* #115 is designed and tracked on its own (decision 17). It can land at
  any point after slice 1.

> Noted 2026-09-23 (ADR-0015 Amendment 5): #116 is designed. Its change
> covers `IpAttributeRecords` (now a `Mapping[Address, IpRecord]`),
> `record()` and `apply_hot_ip_added` (a required keyword-only
> `request_count`), the worker's two calls, and
> `schemas/hot_ip_event.v1.json` (`window_count` required). The row's "the
> invariant checkers" do not change: both take the widened map as they are.

> Noted 2026-09-23 (Amendment 2): slice 2 is designed. Its row also covers
> a second key, `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6` (Amendment 2
> ruling 1), and its implementing change edits `.env.example` and
> `CHANGES`. Slice 2 makes every start re-publish the stats of each
> state-changing record it replays, which limits how much one start can
> replay inside its deadline (ruling 4). So the snapshot epic must also come
> before any deployment whose retained hot-ip log holds more such records
> than one start can re-publish in time.

> Amended 2026-09-24 (Amendment 3 ruling 1): the constraint the note above
> adds no longer holds. A start re-publishes only from the `sequence` of
> the last message in `hammertime.prefix-stats.v1`, when that message is
> the trie's own. Then the number of state-changing records the log
> retains no longer limits a start. When it is not, a start that fails
> leaves the next one less to re-publish, so repeated starts finish. The
> snapshot epic's place in the order rests on this decision's own reasons
> again.

> Noted 2026-09-24 (Amendment 4): slice 3 is designed. Its change also
> covers the `prefix_queries` counter in `metrics.py`, the `create_app` call
> in `service.py`, the re-export of `evaluate_prefix_state` from
> `hammertime.core.state`, and `CHANGES` (Amendment 4 ruling 11). It changes
> nothing in `worker.py`, `state.py`, `publisher.py` or `config.py`, and adds
> no key to `.env.example`.

### 2. Slice 1's modules

```text
services/trie/src/hammertime/trie/
  __main__.py     main() -> run_service("trie", ...)                         §33, §47   (ADR-0009 decision 1)
  config.py       TrieSettings, load_settings(env=None)                      §33, §35, §47
  state.py        FamilyState, TrieState                                     §22, §28, §33, §35, §46.5   (new)
  metrics.py      TrieMetrics                                                §37, §46.8   (new)
  worker.py       CONSUMER_GROUP, HotIpOutcome, TrieWorker                   §19, §22, §28, §33, §46.5
  service.py      SERVICE_NAME, TrieService, build_service                   §33, §47   (new; ADR-0009 decision 3)
  query/app.py    create_app(readiness) -> FastAPI: /healthz, /readyz, /metrics   §47 (slice 3 adds §29, §31)
```

`state.py` holds everything a reader may see: the tries, the record maps,
the log position, `as_of` and the configuration in force. The read path,
`/metrics` and the snapshot epic therefore depend on `state.py` and not on
the worker, which depends on the bus. Imports run one way:

* `state` imports `hammertime.trie.structure`, `hammertime.trie.metadata`
  and `hammertime-core`;
* `metrics` imports `state`;
* `worker` imports `state`, `metrics`, `hammertime-bus` and
  `hammertime-core`;
* `service` imports all of them and `query.app`;
* `query.app` imports only `hammertime.core.runtime` and FastAPI in
  slice 1.

Nothing in `structure` or `metadata` imports any of these. Slice 1 does
not touch `publisher.py`, `query/views.py` or `snapshot/`.

> Amended 2026-09-23 (Amendment 2 ruling 2): slice 2 fills in
> `publisher.py`: `AGENT_ID`, `DEFAULT_MIN_PREFIX_LENGTHS`, `StatsMessage`,
> `PreparedStats`, `PrefixStatsPublishError` and `PrefixStatsPublisher`.
> `publisher` imports `metrics`, `hammertime.trie.structure`,
> `hammertime.trie.metadata`, `hammertime-bus` and `hammertime-core`, and
> `worker` imports `publisher`. `state` and `query.app` import none of it.
> The paragraph above describes slice 1 and stays true of it.

> Amended 2026-09-24 (Amendment 4 ruling 8): slice 3 makes `create_app` take
> `state`, `metrics` and `min_prefix_lengths` as required keywords after
> `readiness`, and fills in `query/views.py`. `query.app` then imports
> `hammertime-core`, FastAPI and Starlette, `state`, `metrics` and
> `query.views`. `query.views` imports `state` and `hammertime-core`, and may
> import `hammertime.trie.structure` and `hammertime.trie.metadata`. Neither
> imports `worker`, `publisher`, `service` or `hammertime-bus`. The module
> table's `query/app.py` row and the import list above describe slice 1.

```python
# hammertime.trie.state   Spec: §22, §28, §33, §35, §46.5
@dataclass(frozen=True, slots=True)
class FamilyState:
    trie: PatriciaTrie
    records: IpAttributeRecords
    @property
    def family(self) -> AddressFamily: ...        # the trie's family, which is the records' family

class TrieState:
    def __init__(self, *, families: Iterable[AddressFamily], config: DetectionConfig) -> None: ...
    @property
    def families(self) -> frozenset[AddressFamily]: ...
    def serves(self, family: AddressFamily) -> bool: ...
    def of(self, family: AddressFamily) -> FamilyState: ...   # KeyError for a family not served
    @property
    def config(self) -> DetectionConfig: ...                  # the configuration in force at the trie
    @property
    def position(self) -> int | None: ...                     # the last offset handled or passed (decision 8); None before the first
    @property
    def event_sequence(self) -> int: ...                      # 0 if position is None else position + 1 (decision 8)
    @property
    def as_of(self) -> datetime | None: ...                   # the newest timestamp among applied events (decision 8)
    def note_handled(self, offset: int) -> None: ...
    def note_applied(self, offset: int, timestamp: datetime) -> None: ...
    def note_passed(self, offset: int) -> None: ...
    def adopt_config(self, config: DetectionConfig) -> None: ...
```

* **The constructor** builds one `PatriciaTrie(f)` and one
  `IpAttributeRecords(f)` per family `f`, both empty. An empty `families`
  is a `ValueError`. It starts with `position = None`, `as_of = None` and
  `config` as given.
* **`note_handled(offset)`** sets `position = offset`.
* **`note_applied(offset, timestamp)`** sets `position = offset`, and sets
  `as_of` to `timestamp` when `as_of` is `None` and to
  `max(as_of, timestamp)` otherwise.
* **`note_passed(offset)`** sets `position = offset`, and nothing else:
  the log holds no record at or below `offset` that the trie has not
  read (decision 4 step 3). `as_of` does not move.
* **What all three refuse.** An `offset` below 0, or not greater than
  `position`, is a `ValueError` from `note_handled`, `note_applied` and
  `note_passed`, and nothing changes. `note_applied` also refuses a naive
  `timestamp` in the same way.
* **`adopt_config(config)`** replaces `config`. It compares no versions
  (decision 10).
* **Who writes it.** The worker is the only production caller of the
  four mutators. The snapshot epic restores a `TrieState` by its own
  means (decision 16), and nothing else writes one.

### 3. Consumption: one positional, whole-topic subscription, never acknowledged

* **The consumer.** The worker takes its consumer as
  `bus.consumer(CONSUMER_GROUP)`, where `CONSUMER_GROUP =
  "hammertime-trie"` — the name ADR-0009 decision 9 gives the trie. A
  positional subscription creates no durable, so the name reaches no
  broker (ADR-0010 Amendment 1 ruling 1).
* **The subscription.** It is `subscribe(HOT_IP.name, start_offset=S)`,
  with `partitions=None` and no listener:
  * `S` is `state.event_sequence` as it stands after decision 4 step 3:
    `0` when `state.position` is `None`, and `state.position + 1`
    otherwise;
  * a fresh `TrieState` has no position, so in slice 1 every start
    replays from the first record the log retains. On the memory bus
    that is `start_offset=0`. On JetStream, step 3 has passed the offsets
    below the first retained record, so `S` is that record's offset, or
    the log's end when it retains none (ADR-0013 decision 9 as amended by
    Amendment 10);
  * the snapshot epic restores a `TrieState` whose `position` is its
    `replay_position`, and so gets `S = replay_position + 1` with no
    change here.
* **Order.** Messages are handled one at a time, in the order the
  iterator yields them. For a whole-topic positional subscription that is
  stream-sequence order across every partition (ADR-0013 decisions 5
  and 9). Per IP, the trie's applied history is therefore a prefix of the
  published stream (ADR-0001 Amendment 1 clause 3).
* **No acknowledgement.** Nothing is acknowledged and nothing is
  committed. The service closes the consumer, with the bus, after the
  worker has stopped (decision 13).

### 4. Readiness: replayed up to the log end read when `start()` began

`TrieWorker.start()` takes these steps:

1. If it has already subscribed, it returns.
2. It reads `end = await bus.end_offset(HOT_IP.name)`, then
   `first = await bus.first_offset(HOT_IP.name)`: both *before*
   subscribing, and in that order. It keeps `end` as `replay_target`.
3. **It passes what the log no longer holds.** If `first >
   state.event_sequence`, the log retains no record between the trie's
   position and `first`: those records aged out, were purged, or never
   existed (JetStream has no offset 0). The worker takes its lock and,
   unless `stop()` has begun, calls `state.note_passed(first - 1)`, so
   that `event_sequence` becomes `first`.
4. It subscribes (decision 3), from `S = state.event_sequence`.
5. It takes the next message and awaits `handle()` on it, until the
   worker is *caught up*: `state.event_sequence >= end`.
   * If the iterator ends before that, `start()` raises `RuntimeError`.
   * If `stop()` has begun, `start()` returns without being caught up.
6. It logs `replay_complete` (decision 12).

`caught_up` is `False` until `start()` has subscribed, and
`state.event_sequence >= replay_target` from then on. Because the
subscription starts at `event_sequence`, this is the earlier test
"`end <= S`, or `event_sequence >= end`" with the passed offsets counted.

**What step 3 covers.**

* *A stream nothing has been written to.* `end` and `first` are both
  `1`. Without step 3 the trie waited, until the startup deadline, for a
  record to move its position, and the stream holds none. `make up` on a
  fresh deployment does this, because provisioning creates the stream
  empty.
* *A stream whose every record has aged out or been purged.* `first` is
  `end`, because the stream keeps its last sequence (Sources).
  `max_age` is 30 days for `hammertime.hot-ip.v1`, so any trie that
  restarts after 30 quiet days meets this.
* *A stream whose head has aged out.* `0 < first < end`. The replay
  reads `[first, end)`, as it did before, but subscribes from `first`.

On the memory bus `first_offset` is always `0`, because that bus never
discards (ADR-0013 decision 3, as amended by Amendment 10). Step 3
therefore never runs there, and nothing the memory bus shows changes.

**Why `end` is read first.** JetStream assigns offsets in increasing
order, and never one at or below the stream's last. So once a `first`
read after `end` is `>= end`, no record below `end` can be delivered.
If a record ages out between the two reads, this order counts it as
gone. The other order would count it as present, and the replay would
wait for it. `first` can exceed `end` when records are appended and the
head ages out between the two reads. Step 3 then passes those offsets as
well, and the trie is caught up at once.

**Why step 3 moves the position, and does not only relax the test.**
Suppose the empty case were simply counted as caught up, with
`event_sequence` left at `0`. A trie that had handled the record at
`end - 1` reports `end`. Restarted after that record aged out, it would
report `0` until the next record arrived: backwards, against decision 8
and `read-api-v1.md`. After step 3, `event_sequence` is the offset of the
next record the trie will read in every case. The subscription starts
there, and the snapshot epic's `replay_position` records it
(assumption 26).

**A restored state.** In slice 1 the state is always fresh. For a state
restored from a snapshot, `first > S` means that records after the
snapshot are gone, which is decision 16's first gap case. Step 3 must
not pass them silently, so the snapshot epic runs its gap check before
step 3 (decision 16).

`TrieService.start()` does three things in order:

* when it built a `NatsBus` itself, it starts the bus under
  `connect_with_retry("bus", ..., transient=TRANSIENT_ERRORS, sleep=...,
  monotonic=...)`, as the aggregator does;
* it awaits `worker.start()` and sets `trie_recovery_seconds` to the time
  spent in `start()` (decision 12);
* it marks itself ready — unless `stop()` has begun or the worker is not
  caught up.

The replay runs inside `start()`, under `HAMMERTIME_STARTUP_TIMEOUT_S`.
ADR-0009 decisions 4 and 5 are unchanged. The consequence for a long log
is under Consequences.

> Amended 2026-09-24 (Amendment 3 ruling 1): a step 2a runs after step 2
> and before step 3. The worker reads `last = await
> bus.last_value(PREFIX_STATS.name)`, the value of the last message of
> `hammertime.prefix-stats.v1`, and sets `republish_from` from it: the
> envelope's `sequence` when the value decodes to a `PrefixStatsChanged`
> published under `AGENT_ID` whose `sequence` is at most `replay_target`,
> and `0` otherwise (Amendment 3 ruling 1's table). A replayed event
> below `republish_from` is applied and not re-published. The other steps
> keep their numbers, so that the texts citing them stay right. Step 6's
> `replay_complete` gains the field `republish_from`.

### 5. Address families: the configured set, one trie and one record map each

* **What is held.** `HAMMERTIME_TRIE_FAMILIES` (decision 11) names the
  families this process holds. The default is `ipv4`, §43's first item.
  `TrieState` holds one `PatriciaTrie` and one `IpAttributeRecords` per
  family — §35's separate roots, ADR-0014 decision 1, ADR-0015 decision 5.
* **An event of another family** has the outcome `FAMILY_NOT_SERVED`
  (decision 6). It is not applied, and it is not an error. It is counted,
  and logged as a warning the first time each family is met in a process
  and at debug level after that.
* **Routing.** The worker picks the pair to apply to with
  `state.of(ip.family)`. The family `ValueError` of `apply_hot_ip_added`
  and `apply_hot_ip_removed` therefore cannot be reached; if it is, that
  is a bug in the worker, and it propagates (decision 7).
* **One process.** A deployment runs one trie service process (ADR-0001).
  Two processes reading one hot-ip log — with overlapping families, or
  split by family — are not a supported topology in v1.

### 6. One message, one of six outcomes

```python
# hammertime.trie.worker   Spec: §19, §22, §28, §33, §46.5
CONSUMER_GROUP: Final = "hammertime-trie"

class HotIpOutcome(StrEnum):
    APPLIED = "applied"                     # the event changed the hot set
    UNCHANGED = "unchanged"                 # a redundant add or remove: counts untouched, record replaced or deleted
    MALFORMED = "malformed"                 # skipped: see step 2 below
    FAMILY_NOT_SERVED = "family_not_served" # skipped: decision 5
    REDELIVERED = "redelivered"             # offset not past the position: skipped, position unchanged
    STOPPED = "stopped"                     # handed to handle() after stop() began: nothing done

class TrieWorker:
    def __init__(self, *, bus: MessageBus, state: TrieState, metrics: TrieMetrics) -> None: ...
    @property
    def state(self) -> TrieState: ...
    @property
    def metrics(self) -> TrieMetrics: ...
    @property
    def replay_target(self) -> int | None: ...   # the log end read by start(); None before it
    @property
    def caught_up(self) -> bool: ...             # decision 4
    async def start(self) -> None: ...
    async def run(self) -> None: ...
    async def stop(self) -> None: ...
    async def handle(self, message: ConsumedMessage) -> HotIpOutcome: ...
    async def apply_config(self, config: DetectionConfig) -> None: ...
```

The constructor takes the consumer from `bus` and calls
`metrics.bind_state(state)`. `handle()` holds the worker's lock — an
`asyncio.Lock`, the one `stop()` and `apply_config()` also take — and
returns `STOPPED` if `stop()` has begun. Otherwise it takes these steps in
order, and returns at the first that decides the outcome:

1. **Redelivered.** If `state.position` is not `None` and
   `message.offset <= state.position`, the outcome is `REDELIVERED`. The
   record `redelivered_hot_ip_event` is logged, and nothing else happens.
   `ack_wait` does not redeliver on a positional subscription, but this
   one-comparison check keeps the log position — and so `event_sequence` —
   from counting a record twice (decision 8).
2. **Decode.** The worker calls
   `hammertime.core.events.codec.decode(message.value)`. The outcome is
   `MALFORMED` if any of the following holds; the reason token is in
   brackets.
   * The codec raises `CodecError` whose `__cause__` is an
     `InvalidAttributesError` [`invalid_attributes`]. In this case
     `attributes_rejected{stage="decode"}` is counted as well (ADR-0015
     assumption 37).
   * The codec raises any other `CodecError` [`codec`].
   * The payload is not a `HotIpAdded` or a `HotIpRemoved`
     [`payload_type`].
   * `message.key` is not `str(payload.ip)` encoded as UTF-8
     [`key_mismatch`].
   * The envelope's `subject` is not `str(payload.ip)`
     [`subject_mismatch`].

   On `MALFORMED` the worker counts `hot_ip_events_skipped{reason="malformed"}`,
   logs `malformed_hot_ip_event` with the reason token, and calls
   `state.note_handled(message.offset)`.
3. **Family.** If `state.serves(payload.ip.family)` is false, the outcome
   is `FAMILY_NOT_SERVED`. The worker counts
   `hot_ip_events_skipped{reason="family_not_served"}`, logs the record as
   decision 5 says, and calls `state.note_handled(message.offset)`.
4. **Apply.** With `fs = state.of(payload.ip.family)`:
   * `HotIpAdded`: `changed = apply_hot_ip_added(fs.trie, fs.records,
     payload.ip, payload.attributes)`. If that raises
     `InvalidAttributesError`, the worker counts
     `attributes_rejected{stage="apply"}`, logs
     `hot_ip_attributes_rejected`, and calls `changed =
     apply_hot_ip_added(fs.trie, fs.records, payload.ip, None)`, which
     stores the default document.
   * `HotIpRemoved`: `changed = apply_hot_ip_removed(fs.trie, fs.records,
     payload.ip)`. Its `attributes`, if any, are neither stored nor
     logged. §46.5 allows logging them, and nothing needs it.
   * `InvariantViolation` from either call: the worker logs
     `trie_invariant_violation` and re-raises (decision 7).
5. **Record.** The worker calls `state.note_applied(message.offset,
   payload.timestamp)` and counts `trie_updates{family, event_type,
   result}`.
6. **Outcome.** `APPLIED` if `changed`, else `UNCHANGED`.

Steps 4 and 5 are one synchronous section. No `await` separates the first
write of `apply_*` from the last write of `note_applied` (decision 9,
R1). In slice 1 `handle()` contains no `await` at all besides taking the
lock. Slice 2 publishes after step 5 (decision 14).

> Noted 2026-09-23 (ADR-0015 Amendment 5, issue #116): step 4's two
> `apply_hot_ip_added` calls each also pass
> `request_count=payload.window_count`, as a keyword. The retry with
> `attributes=None` passes it too, so the count survives a rejected
> document. Both calls stay inside the synchronous section above. The
> `HotIpRemoved` call is unchanged.

> Amended 2026-09-23 (Amendment 2 ruling 3): the constructor gains a
> keyword, `min_prefix_lengths: Mapping[AddressFamily, int] =
> DEFAULT_MIN_PREFIX_LENGTHS`. It takes its producer from `bus.producer()`,
> once, as it takes its consumer, and builds its `PrefixStatsPublisher`
> from it. When step 4 returned `changed`, two steps follow step 5 and come
> before step 6:
>
> * **5a. Prepare.** `prepared = publisher.prepare(fs.trie, payload.ip,
>   sequence=state.event_sequence,
>   config_version=state.config.config_version,
>   timestamp=payload.timestamp)`, still inside the synchronous section.
> * **5b. Publish.** `await publisher.publish(prepared)`, still under the
>   lock. A `PrefixStatsPublishError` is logged as
>   `prefix_stats_publish_failed` and re-raised.
>
> `handle()` now awaits the lock and, for an `APPLIED` event, that publish.
> The paragraph above that begins "Steps 4 and 5 are one synchronous
> section" describes slice 1.

> Amended 2026-09-24 (Amendment 3 ruling 1): the interface gains a
> property, `republish_from -> int | None`: the value `start()`'s step 2a
> set (decision 4), and `None` before it. Steps 5a and 5b run only for an
> event that changed the hot set and whose `state.event_sequence` is at
> least `republish_from`, read as `0` while it is `None`. An event below
> it is applied, recorded in step 5 and counted in `trie_updates` as
> before, and its outcome is `APPLIED`, but it prepares and publishes
> nothing.

The key and subject checks mirror the aggregator's ADR-0004 check on
observations. Every in-repo producer of hot-ip events sets both to the
IP's canonical text (ADR-0011 decision 4 step 4). A record that names one
IP in its payload and another in its key or subject was not written by
that producer, and the trie does not guess which is meant
(assumption 7).

### 7. What each exception does

| Raised by | Exception | Response |
| --- | --- | --- |
| `decode` | `CodecError` whose `__cause__` is an `InvalidAttributesError` | `MALFORMED` [`invalid_attributes`] and `attributes_rejected{stage="decode"}`. The transition is lost, which ADR-0015 assumption 42 already records. That is slice 1's interim until #115, which this ADR does not design (decision 17). |
| `decode` | any other `CodecError` | `MALFORMED` [`codec`] |
| `apply_hot_ip_added` | `InvalidAttributesError` | Apply again with `attributes=None`, count `attributes_rejected{stage="apply"}`, log it. Never drop, retry, dead-letter or rebuild (ADR-0015 decision 6). From the bus this cannot happen, because the codec and the record map run the same validator; the path exists for ADR-0015 decision 5's other writers. |
| `apply_*` | `ValueError` | Cannot happen: the worker routes by family (decision 5). If it does, it is a bug, and it propagates. |
| `apply_*` | `InvariantViolation` | Log `trie_invariant_violation` and propagate: out of `handle()`, then out of `start()` (`start_failed`, exit 1) or `run()` (`run_exited`, exit 1). The trie and the records are unchanged (ADR-0014 A12 clause 2) and `position` has not moved. The orchestrator restarts the process, and the new process rebuilds its state from the snapshot plus replay — in slice 1, from replay alone. That is ADR-0014 A12 clause 5's rebuild, done by restarting the process. |
| the subscription's iterator | any exception | Propagates out of `start()` or `run()`, exit 1. The bus already absorbs a fetch timeout (ADR-0013 decision 5, as amended). |
| `bus.end_offset`, `bus.first_offset`, `subscribe` | any exception | Propagates out of `start()`: `start_failed`, exit 1. Not retried: the bus answered a moment before, at `NatsBus.start()`. |

The worker catches no bare `Exception` anywhere.

> Noted 2026-09-23 (ADR-0015 Amendment 5): `apply_hot_ip_added` also raises
> `TypeError` for a `request_count` that is not an exact `int`, and
> `ValueError` for a negative one, before the document and the trie. Neither
> can come from the bus: the codec delivers `window_count` as an exact `int`
> of at least 0 (ADR-0015 Amendment 3 ruling 3, ADR-0016 decision 1). Either
> is a bug and propagates, as the `apply_*` `ValueError` row says. Neither is
> an attributes rejection, so neither takes the `attributes=None` retry or
> counts in `attributes_rejected`.

> Amended 2026-09-23 (Amendment 2 ruling 5): three rows join the table.
>
> * `publisher.publish` raises `PrefixStatsPublishError`: the worker logs
>   `prefix_stats_publish_failed` and propagates the error, out of
>   `start()` (`start_failed`, exit 1) or `run()` (`run_exited`, exit 1).
>   The event is applied and `position` has moved past it.
> * `publisher.prepare` raises `ValueError` or `CodecError`: cannot happen;
>   a bug, which propagates.
> * `publisher.flush` raises, in `stop()`: propagates out of `stop()`.
>
> The worker still catches no bare `Exception`: it catches
> `PrefixStatsPublishError` by name.

> Amended 2026-09-24 (Amendment 3 rulings 1 and 2): the row "`bus.end_offset`,
> `bus.first_offset`, `subscribe`" gains `bus.last_value`: an exception
> from it propagates out of `start()`, `start_failed`, exit 1. A last
> message the worker cannot use is not an exception. It sets
> `republish_from` to `0` and is logged as `prefix_stats_last_ignored`.
> The row "`publisher.prepare` raises `ValueError` or `CodecError`: cannot
> happen" did not hold for a hot-ip timestamp the codec decoded but could
> not write again. Decode now refuses such a timestamp, so the row holds.

### 8. `event_sequence` is the trie's position in the log; `as_of` is the newest applied event time

**The definitions.**

* `position` is the last offset the worker has *handled* or *passed*.
  * A record is handled when its outcome is `APPLIED`, `UNCHANGED`,
    `MALFORMED` or `FAMILY_NOT_SERVED`.
  * The offsets below the first record the log retains are passed when
    `start()` finds that the trie has not read that far. Their records
    aged out, were purged, or never existed (decision 4 step 3).

  It is `None` before the first of either. `REDELIVERED` and `STOPPED` do
  not move it, and neither does an `InvariantViolation`.
* `event_sequence` is `0` when `position` is `None`, and `position + 1`
  otherwise: the offset of the next record the trie will read.

This answers ADR-0010 Amendment 1 ruling 3: the trie's `event_sequence`
and the log's stream sequence are one number. It is what every read
response and every `PrefixStatsChanged.sequence` carries. The snapshot's
`replay_position` is `position`. ADR-0010 Amendment 1 ruling 2 said
"applied"; a skipped record is handled too, and need not be read again,
and a passed offset has nothing to read.

**Who decided.** The repository owner decided on 2026-09-23, on this
ADR's recommendation, that the trie's `event_sequence` is its log
position and not a count of applied events. The reasons below are that
recommendation's. The form the position takes here — `position + 1`,
with `position` counting handled records and passed offsets — is this
ADR's own (assumptions 9 and 26). ADR-0010 Amendment 1 had raised the
question for the owner (its assumption "Two numbers rather than one"),
while ADR-0013 decision 9 left it to the trie epic. The owner's decision
makes that disagreement moot.

**Why one number rather than a count of applied events.**

* *It cannot go backwards.* A count stays monotonic across restarts only
  while it is persisted and the log is whole.
  * A full replay re-derives the count from whatever the log still holds.
    Every start is a full replay until the snapshot epic lands, and so is
    any start after a snapshot is lost.
  * Once the log's head has aged out (`max_age` is 30 days for
    `hammertime.hot-ip.v1`), that replay re-derives a smaller count.
  * The detector must apply a `PrefixStatsChanged` only when its
    `sequence` is greater than the one it holds (ADR-0013 decision 9). It
    would then refuse every later stat for every prefix it has seen.

  A log position re-derives the same value wherever the replay starts,
  as long as the stream exists. It comes from the last record the log
  still holds, or from the log's first retained offset when the log
  holds nothing the trie has not read (decision 4 step 3).
* *§33 names one "event sequence number".* ADR-0013's Context item 3 chose
  JetStream partly for "one monotonic stream sequence". With one number,
  the snapshot records one integer, and the replay's `start_offset` is the
  snapshot's `event_sequence`.
* *It still groups.* ADR-0010 decision 3 needs one value that the stats
  of one event share and that differs between events. The event's own
  position is exactly that.
* *The earlier design wanted it.* ADR-0010 Amendment 1 called the
  unification "attractive". It kept two numbers only so that ADR-0013
  could land without a protocol change.

**What it costs.** The value is not a count, and not dense: it moves past
records the trie skips, and it stays put while the log is quiet. The same
history gives different values on `InMemoryBus`, whose offsets start at
0, and on JetStream, whose sequences start at 1. JetStream's offset 0
holds no record, so a trie started there passes it. Once `start()` has
read the log's bounds, its `event_sequence` is at least `1`, even on a
stream nothing has been written to (decision 4 step 3). Only
`read-api-v1.md`'s preamble promised a client a count, and it is edited
here.

**Why `position + 1` and not `position`.**

* `0` then means "nothing read yet" on both buses: nothing handled and
  nothing passed.
* It is `end_offset`'s own convention — the next offset to read — so
  readiness is `event_sequence >= end_offset` (decision 4).
* On the memory log, which never discards, it equals the number of
  records the trie has handled. That keeps
  `docs/spec/integration-scenarios.md` §5's `256` and `257` exact.

**`as_of`.**

* It is the greatest `timestamp` among the payloads of events whose
  outcome was `APPLIED` or `UNCHANGED`, and `None` before the first.
* That timestamp is the aggregator's clock at each transition (ADR-0011
  decision 4), so `as_of` is event time. Replaying the same log gives the
  same `as_of`, and a restart does not change it. (A replay from a
  snapshot restores `as_of` from the snapshot, decision 16.)
* It is the greatest timestamp rather than the last one applied, so that
  it never goes backwards when two aggregators' clocks disagree
  (assumption 10).
* On the wire it is `null` until the trie has applied its first event
  (`read-api-v1.md`).

**The rest.** `config_version` is `state.config.config_version`
(decision 10). The trie has one `event_sequence` whatever families it
serves, because the log is one. ADR-0001 Amendment 1's assumption that
"each family's trie has its own `event_sequence`" is superseded (ADR-0001
Amendment 4).

### 9. Atomicity and consistent reads: one event loop, and no `await` inside a mutation or a read

§28 lists "single-writer ownership" and "shard-local event loops" among
its mechanisms. The trie uses both, and states them as three rules.

* **R1 — writes.** The worker changes `TrieState` only in `handle()`
  (decision 6, steps 2-5), in `apply_config()`, and in step 3 of
  `start()` (decision 4), which is one `note_passed` call under the lock.
  For one event, no `await` separates the first write from the last. The
  first is `apply_hot_ip_added` or `apply_hot_ip_removed`, both
  synchronous functions (ADR-0015 decision 6); the last is
  `note_applied`.
* **R2 — reads.** Every reader of `TrieState` takes everything it reports
  without an `await` between its first read and its last, and
  materializes it before it yields. The readers are:
  * the read API (slice 3);
  * the derived series of `/metrics`;
  * a snapshot (snapshot epic, taken under the worker's lock).

  No lazy iterator over a trie survives an `await`, so no response
  streams `iter_prefix_counts`.
* **R3 — one thread.** The worker, uvicorn's server and the
  configuration poller run on one event loop, and no other thread touches
  `TrieState`. FastAPI runs a path operation or a dependency declared with
  plain `def` "in an external threadpool" (Sources), so every route and
  dependency the trie app declares is `async def`.

Together these mean that a reader sees `TrieState` between two whole
events. Counts, records, `event_sequence`, `as_of` and `config_version`
come from one consistent state (§28), and the version numbers §22 asks
for describe exactly the data they come with.

No copy-on-write is used, and the `worker.py` stub's "versioned snapshot
pointer" is not built. In Python a copy of the trie per event would cost
O(trie) per transition, for a guarantee R1-R3 give for nothing.

The cost is that a long synchronous read delays the writer for as long as
it takes. In slice 1 no reader is long. Decision 15 names slice 3's one
candidate, `GET /prefixes/hot`.

> Amended 2026-09-23 (Amendment 2 ruling 3): slice 2 keeps all three
> rules. R1's writes still start with `apply_*` and end with
> `note_applied`. For an event that changed the hot set, the synchronous
> section runs on, without an `await`, through the publisher's `prepare()`,
> which only reads. The publish is awaited after the section, under the
> lock, and writes nothing to `TrieState`. R2 gains a fourth reader,
> `prepare()`: it takes the event's ancestor counts and `config_version` in
> that section and encodes every message before the first `await`. While
> the publish is awaited, readers see the event whole, with
> `event_sequence` past it and its stats not yet all in the log (§22).

> Amended 2026-09-24 (Amendment 4 rulings 4 and 5): the read API keeps R2
> and R3. A read route's handler is `async def` with no `await` in it: its
> readiness check, its reads of `TrieState` and the building of its response
> run in one synchronous call. No route, dependency or handler is a plain
> `def`, and nothing is handed to a thread. `GET /prefixes/hot`, the one long
> read, keeps no index: it walks only the prefixes whose `hot_count` is at
> least `max(minimum_hot_ips, 1)`, and Amendment 4 ruling 5 bounds what that
> costs the writer.

### 10. Configuration changes: adopted by the state, under the worker's lock

* **Wiring.** `TrieService` builds
  `ConfigPoller(settings.detection_config_path, config,
  apply=worker.apply_config, poll_interval_s=settings.config_poll_interval_s,
  logger=...)`. `reload_config()` is `poller.poll_once()` (ADR-0009 A5).
* **`TrieWorker.apply_config(config)`** takes the worker's lock. If
  `stop()` has begun it returns without changing anything. Otherwise it
  calls `state.adopt_config(config)`.
* **Unconditional.** It compares no versions: the poller alone gates them,
  the rule ADR-0011 Amendment 3 A12 pins for the aggregator.
* **No pass.** The trie evaluates prefix state lazily at read time
  (ADR-0010 decision 5), so adopting the document is the whole
  re-evaluation in slice 1. The new `config_version` is visible to a
  reader once `poll_once()` has awaited it, and not before (§47.3).
* **Slice 3.** If slice 3 caches `prefix_state`, it extends `apply_config`
  to refresh the cache before it adopts.

> Amended 2026-09-24 (Amendment 4 ruling 5): slice 3 caches nothing. The
> trie keeps no `prefix_state`, `apply_config` is unchanged, and adopting the
> document stays the whole re-evaluation.

### 11. Settings

`hammertime.trie.config.load_settings(env: Mapping[str, str] | None = None) -> TrieSettings`
follows ADR-0009 decision 2's pattern:

* `env=None` means `os.environ`;
* a bad value is a `ValueError` naming the variable, and so
  `config_invalid` and exit 2;
* unknown variables are ignored;
* when several values are bad, which one is reported is not pinned.

```python
@dataclass(frozen=True, slots=True)
class TrieSettings:
    host: str
    port: int
    detection_config_path: Path
    bus_kind: str
    bus_brokers: str
    families: frozenset[AddressFamily] = frozenset({AddressFamily.IPV4})
    config_poll_interval_s: float = 1.0
```

| Key | Field | Default | Rule |
| --- | --- | --- | --- |
| `HAMMERTIME_TRIE_QUERY_BIND` | `host`, `port` | `0.0.0.0:8081` | `host:port`, split at the last `:`; the host non-empty, the port all digits |
| `HAMMERTIME_CONFIG_PATH` | `detection_config_path` | `./config/detection.v1.json` | stored as a `Path`; `build_service` loads it, so a bad document is `config_invalid`, exit 2 |
| `HAMMERTIME_BUS_KIND` | `bus_kind` | `nats` | `nats` or `memory` |
| `HAMMERTIME_BUS_BROKERS` | `bus_brokers` | `nats://localhost:4222` | Under `nats`, every entry of `hammertime.bus.split_bus_servers(value)` passes `hammertime.bus.validate_bus_url`. A refusal is a `ValueError` naming the variable and the entry's position, and nothing of the value (ADR-0013 Amendment 4 ruling S2, Amendment 5 ruling 5). Under `memory` the value is not read. |
| `HAMMERTIME_CONFIG_POLL_INTERVAL_S` | `config_poll_interval_s` | `1.0` | a positive number |
| `HAMMERTIME_TRIE_FAMILIES` | `families` | `ipv4` | Comma-separated. Each entry is stripped and case-folded, and must be `ipv4` or `ipv6`; duplicates collapse. An empty entry, an empty value or a whitespace-only value is a `ValueError`. |

Not read by slice 1:

* `HAMMERTIME_STORE_KIND` and `HAMMERTIME_REDIS_URL`: the trie has no
  store. ADR-0009 A12 item 5 binds only a service that reads the URL.
* `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` (ADR-0009 Consequences, ADR-0010
  decision 3): slice 2 adds it.
* `HAMMERTIME_TRIE_SNAPSHOT_DIR` (default `./snapshots`) and
  `HAMMERTIME_TRIE_SNAPSHOT_INTERVAL_S` (default `300`, a positive
  number). Their names and defaults are fixed now — `.env.example` and
  `integration-scenarios.md` §2.2 already use them. The snapshot epic adds
  them to `load_settings` together with the code that reads them.

> Amended 2026-09-23 (Amendment 2 ruling 1): slice 2 reads
> `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` (field `min_prefix_length`, default
> `8`, an integer from 0 to 32: IPv4's floor) and a new key,
> `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6` (field `min_prefix_length_ipv6`,
> default `104`, an integer from 0 to 128). Each is read with `int()` and
> checked against its range, both ends included. A bad value is a
> `ValueError` naming the variable. Both are checked whatever
> `HAMMERTIME_TRIE_FAMILIES` holds. `TrieSettings` gains the two fields,
> with those defaults, after `config_poll_interval_s`.

### 12. Metrics and log records

**Metrics.** `hammertime.trie.metrics.TrieMetrics` follows
`AggregatorMetrics` (ADR-0011 decision 8, Amendment 3 A13):

* one series is one `(name, label values)` pair, and label values are
  compared as `str(value)`;
* the name must be one of those in the table below, and the label names
  exactly that series' own, or `increment`, `set` and `get` raise
  `ValueError`;
* rendering on `/metrics` is the telemetry epic's. Until then `/metrics`
  is empty, as the aggregator's is.

```python
class TrieMetrics:
    def __init__(self) -> None: ...
    def bind_state(self, state: TrieState) -> None: ...                      # called by TrieWorker.__init__
    def increment(self, name: str, **labels: object) -> None: ...            # counters only
    def set(self, name: str, value: float, **labels: object) -> None: ...    # gauges only
    def get(self, name: str, **labels: object) -> int | float: ...           # any series
```

| Series | Kind | Labels | Meaning |
| --- | --- | --- | --- |
| `trie_updates` | counter | `family`, `event_type` (`HotIpAdded`, `HotIpRemoved`), `result` (`applied`, `unchanged`) | §37: events passed to `apply_*` |
| `hot_ip_events_skipped` | counter | `reason` (`malformed`, `family_not_served`) | records handled without being applied |
| `attributes_rejected` | counter | `stage` (`decode`, `apply`; the snapshot epic adds `snapshot`) | §46.8, ADR-0015 assumption 37 |
| `trie_nodes` | from the state | `family` | §37: `trie.node_count` |
| `hot_ip_count` | from the state | `family` | §37: `trie.hot_ip_count` |
| `ip_attribute_records` | from the state | `family` | §46.8: `len(records)`; equals `hot_ip_count` |
| `ip_attribute_bytes` | from the state | `family` | §46.8: `records.serialized_bytes` |
| `event_sequence` | from the state | none | decision 8 |
| `trie_recovery_seconds` | gauge | none | ADR-0009 decision 5 step 6: wall time spent in `start()`, set by the service when `start()` ends |

`get` returns `0` for:

* a counter never incremented;
* a gauge never set;
* any series read before `bind_state`;
* a `family` the state does not serve.

§37's `prefix_queries` comes with slice 3. `hot_transition_to_prefix_update_latency`
is a latency histogram: slice 2 can measure it, and the telemetry epic
renders it.

**Log records.** They go through the standard-library logger
`hammertime.trie.worker`. Each message starts with the event name,
followed by `key=value` fields — the aggregator's convention. They carry
fixed tokens and numbers only: never a payload value, never an attribute
document, and never an exception's text. That extends ADR-0015 decision
5's rule for attribute messages to every record the trie writes about bus
content, and it is also #115's rule. `InvariantViolation`'s own message
names prefixes and counts and nothing from a document; it reaches the log
through the runner's `exception` field.

| Record | Level | Fields |
| --- | --- | --- |
| `malformed_hot_ip_event` | warning | `topic`, `partition`, `offset`, `reason` (a decision 6 token) |
| `hot_ip_attributes_rejected` | warning | `topic`, `partition`, `offset`, `stage=apply` |
| `family_not_served` | warning the first time per family per process, debug after | `family`, `topic`, `partition`, `offset` |
| `redelivered_hot_ip_event` | warning | `topic`, `partition`, `offset`, `position` |
| `trie_invariant_violation` | error | `family`, `topic`, `partition`, `offset` |
| `replay_complete` | info | `start_offset`, `first_offset`, `replay_target`, `event_sequence` |

> Amended 2026-09-23 (Amendment 2 ruling 7): slice 2 adds a counter,
> `prefix_stats_published` (label `family`): the `PrefixStatsChanged`
> publishes that returned, including one the log dropped as a duplicate.
> It adds a record, `prefix_stats_publish_failed` (error; `family`,
> `topic`, `partition`, `offset`, `sequence`, `attempted`, `failed`,
> `error_type`), and `replay_complete` gains a field,
> `prefix_stats_published`. The sentence above, "slice 2 can measure it",
> is answered no: slice 2 does not measure
> `hot_transition_to_prefix_update_latency`, for ruling 7's reasons, and
> the telemetry epic owns it.

> Amended 2026-09-24 (Amendment 3 ruling 1): `replay_complete` gains a
> field, `republish_from`. A record is added, `prefix_stats_last_ignored`
> (warning; `reason`, one of `codec`, `payload_type`, `agent_id` and
> `ahead_of_log`; `replay_target`), for a last message of
> `hammertime.prefix-stats.v1` that the worker cannot use. It carries fixed
> tokens and numbers only, and nothing the message holds.

> Amended 2026-09-24 (Amendment 4 ruling 9): slice 3 adds `prefix_queries`,
> a counter labelled `route` (`prefix`, `ip`, `prefixes_hot`) and `result`
> (`ok`, `invalid`, `family_not_served`, `not_ready`). The read routes write
> no log record.

### 13. Lifecycle and shutdown

```python
# hammertime.trie.service   Spec: §33, §47
SERVICE_NAME = "trie"

class TrieService:                          # satisfies Service and DescribesStartup (hammertime.core.runtime)
    name: str                               # "trie"
    app: FastAPI                            # query.app.create_app(readiness)
    def __init__(self, settings: TrieSettings, worker: TrieWorker, detection_config: DetectionConfig, *,
                 transport: NatsBus | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None: ...
    @property
    def ready(self) -> bool: ...
    @property
    def state(self) -> TrieState: ...       # worker.state
    @property
    def metrics(self) -> TrieMetrics: ...   # worker.metrics
    async def start(self) -> None: ...
    async def run(self) -> None: ...
    async def stop(self) -> None: ...
    async def reload_config(self) -> DetectionConfig: ...
    def startup_fields(self) -> Mapping[str, object]: ...

def build_service(settings: TrieSettings, *, bus: MessageBus | None = None,
                  clock: Clock | None = None) -> TrieService: ...
```

* **`build_service`** loads the detection document first, so a bad one is
  `config_invalid` and exit 2. It then:
  * takes the bus from `bus`; failing that, builds a
    `NatsBus(settings.bus_brokers)` under `nats`, or a private
    `InMemoryBus` under `memory`;
  * builds `TrieState(families=settings.families, config=...)`, a
    `TrieMetrics` and a `TrieWorker`;
  * passes the `NatsBus` it built, if any, as `transport`.

  `clock` is accepted for ADR-0009 decision 3's signature. It is unused
  until the snapshot epic, which stamps its files with it.
* **`start()`** is decision 4. `monotonic` drives the bus retry, and it
  also measures `trie_recovery_seconds`, from the moment `start()` is
  entered. The service does not call `monotonic` before that: its first
  call is `start()`'s own entry reading. `trie_recovery_seconds` is the
  reading taken when `start()` ends minus that first one.
* **`run()`** returns at once if `stop()` has begun. Otherwise it runs
  three tasks: uvicorn serving `app` on `host:port`, `worker.run()` and
  `poller.run()`. Uvicorn runs as the aggregator's server does: lifespan
  off, and its own log configuration and signal handlers off. When the
  first task completes, `run()`:
  1. collects the exceptions of the tasks that have completed;
  2. calls `stop()` inside a `try` that logs `stop_failed` and adds the
     exception to the list;
  3. gathers the remaining tasks;
  4. closes the transport it built;
  5. raises the first exception collected.

  This is the shape of ADR-0013 decision 8 as amended by Amendment 4
  ruling R7.
* **`stop()`** is idempotent and safe before `start()`. It marks readiness
  `stopping`, stops the poller, sets the server's `should_exit`, and awaits
  `worker.stop()`.
* **`TrieWorker.stop()`** sets the stop flag, then takes the worker's
  lock, so that a message in hand is finished first. It then marks the
  worker stopped. It is idempotent and safe before `start()`, and it does
  not close the consumer. After it has begun:
  * `handle()` returns `STOPPED`, `apply_config()` changes nothing, and
    `start()`'s step 3 passes nothing (decision 4);
  * `run()` returns at its next wake-up. A message that arrived in that
    same wake-up is not handed to `handle()`; the next start reads it
    again, from `position + 1`.

  Later slices add to it. Slice 2 adds `producer.flush()` after the lock
  is taken. The snapshot epic adds the final snapshot after the flush
  (ADR-0009 decision 7 as amended by Amendment 4: "flush, then
  snapshot").
* **`TrieWorker.run()`** raises `RuntimeError` if `start()` has not run.
  It takes messages until `stop()`, handing each to `handle()`, and
  returns when the iterator ends.
* **`startup_fields()`** returns `bus_kind`, `bus_endpoints`,
  `config_path`, `config_version`, `bind` and `families` (the values,
  sorted). `bus_endpoints` is
  `hammertime.bus.nats.bus_endpoints(settings.bus_brokers)`, never the
  value itself (ADR-0013 Amendment 2).
* **`query.app.create_app(readiness)`** answers `GET /healthz`, `/readyz`
  and `/metrics` through `hammertime.core.runtime`'s `healthz_response`,
  `readyz_response` and `metrics_response` (ADR-0009 A4, A6). Every route
  is `async def` (R3). It sets `app.state.readiness`, and registers a
  `ServiceNotReady` handler that renders `not_ready_response`, so slice
  3's routes need only raise it.

  It builds the app as `FastAPI(title="hammertime-trie", openapi_url=None,
  docs_url=None, redoc_url=None)`, so the app serves the routes it
  declares and nothing else. FastAPI's defaults would add
  `/openapi.json`, `/docs`, `/docs/oauth2-redirect` and `/redoc`
  (Sources). The two pages load their scripts from a CDN, at a floating
  major version, into the operator's browser, and the port has no
  authentication (Consequences, *Security posture*). Those four paths
  answer FastAPI's 404, like any other path the app does not declare.
  Slice 3 adds its read routes to this app under the same rule:
  `docs/protocol/read-api-v1.md` is their contract, not a generated
  schema (assumption 31).
* **`__main__.main()`** is `raise SystemExit(run_service(SERVICE_NAME,
  lambda: build_service(load_settings())))`, in ADR-0009 decision 1's
  form.
* **`snapshot_now()`** (ADR-0009 decision 3's table) belongs to the
  snapshot epic. `Service` does not require it.

> Amended 2026-09-23 (Amendment 2 ruling 6): `TrieWorker.stop()` sets the
> stop flag, takes the lock, then awaits `publisher.flush()`, and marks the
> worker stopped whether or not the flush raised. Every call takes all
> three steps. `build_service` passes `min_prefix_lengths={IPV4:
> settings.min_prefix_length, IPV6: settings.min_prefix_length_ipv6}` to
> the worker. `startup_fields()` gains `min_prefix_lengths`: each served
> family's name mapped to its floor, for example `{"ipv4": 8}`.

> Amended 2026-09-24 (Amendment 3 ruling 4): "It then marks the worker
> stopped." describes no behaviour. Nothing reads such a mark: `handle()`,
> `apply_config()`, `start()`'s step 3 and `run()` read the stop flag,
> which is set first. The worker keeps no "stopped" state, and the note
> above's "marks the worker stopped whether or not the flush raised" goes
> with it: `stop()` sets the flag, takes the lock, and awaits the flush,
> whose exception propagates.

> Amended 2026-09-24 (Amendment 4 ruling 8): `TrieService` builds its app as
> `create_app(readiness, state=worker.state, metrics=worker.metrics,
> min_prefix_lengths=...)`, with the floors it passes the worker. The app
> keeps the `create_app` bullet's three `None` URLs, and the read routes are
> declared on it under that bullet's rule.

### 14. The publisher (slice 2): what is decided now

1. **Only a change is published.** An event whose outcome is `APPLIED`
   publishes. `UNCHANGED` and every skipped outcome publish nothing,
   because no prefix's count moved. This closes the question ADR-0011's
   Consequences (*Trie epic*) and ADR-0014 decision 3 left to this epic.
   It matches epic #10's criterion "published on prefix-level state
   changes", and it narrows ADR-0010 decision 3's "on each applied"
   (ADR-0010 Amendment 2).
2. **What is published.** An `APPLIED` event of family `F` publishes one
   `PrefixStatsChanged` per entry of `ancestor_stats(fs.trie, ip,
   min_length=L_F)` (ADR-0015 decision 2), zero counts included: 25 for
   IPv4 with `L = 8`.
3. **The envelope.**
   * `agent_id="trie-primary"`, the ADR-0003 amendment's own example.
   * `sequence` is the event's `event_sequence`.
   * `event_type="PrefixStatsChanged"`.
   * `config_version` is `state.config.config_version`.
   * `timestamp` is the hot-ip event's `timestamp`, so a replay
     re-publishes identical envelopes.
   * `subject=str(prefix)`, which is **required**. ADR-0004's identity is
     `(agent_id, sequence, event_type[, subject])`. The stats of one event
     share the first three, so without `subject` they would share one
     `event_id`. The log's `Nats-Msg-Id` deduplication (ADR-0013 decision
     4), and `MemoryProducer`'s, would then keep one of the 25.
4. **Key and message id.** The key is the prefix text (`topics.py`'s
   `_prefix_key`). The message id is `envelope.event_id`.
5. **`hot_ratio`.** The payload carries it (ADR-0010 Consequences). That
   needs a `hammertime-core` change: `PrefixStatsChanged` gains
   `hot_ratio: float | None = None`, and the codec writes it when set and
   reads it when present. It refuses a non-finite value, or one outside
   `[0, 1]`, as a `CodecError` — `schemas/prefix_stats_event.v1.json`'s
   bounds, in ADR-0016's style.
6. **Ordering.** The worker awaits the publisher inside `handle()`, under
   the lock, after step 5. Every publish for one event is therefore
   acknowledged before the next event is handled (ADR-0010 decision 3).
   The publishes of one event may run concurrently, because they go to
   different prefixes.
7. **Failure.** A publish error propagates out of `handle()`, and the
   process exits 1. After the restart the replay applies and publishes
   again, under the same `event_id`s. The log drops a copy inside its
   120 s window, and the detector's sequence rule absorbs a later one.
8. **Replay publishes too.** An event after the restored position may
   never have had its stats published, and ADR-0010 Amendment 1's
   invariant needs them published again. Until the snapshot epic lands,
   every start therefore re-publishes the stats of the whole retained log.
9. **`stop()` flushes** after taking the lock (decision 13).
10. **Left for slice 2's own design:**
    * the range of lengths reported for IPv6, which ADR-0010 left
      undefined;
    * `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` (IPv4: default 8, range 0-32),
      and whether IPv6 gets a key of its own;
    * whether the service refuses to start with `ipv6` in
      `HAMMERTIME_TRIE_FAMILIES` until that range exists;
    * whether the publisher measures
      `hot_transition_to_prefix_update_latency`.

> Settled 2026-09-23 (Amendment 2): item 10's four points. The IPv6 range
> is `[104, 128]` by default (ruling 1). `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH`
> is IPv4's floor, default 8, from 0 to 32, and IPv6 gets a key of its own,
> `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6`, default 104, from 0 to 128
> (ruling 1). The service keeps accepting `ipv6`, since the range now
> exists (ruling 1). The publisher does not measure
> `hot_transition_to_prefix_update_latency` (ruling 7). Rulings 3 to 6
> detail items 6 to 9.

> Amended 2026-09-24 (Amendment 3 ruling 1): item 8's "every start
> therefore re-publishes the stats of the whole retained log" no longer
> holds. A start re-publishes only from the `sequence` of the last message
> in `hammertime.prefix-stats.v1`, when Amendment 3 ruling 1's table
> accepts that message, and re-publishes everything only when it does not.
> Item 8's reason stands: an event whose stats may be missing from the log
> is published again.

### 15. The read API (slice 3): what is decided now

1. Handlers and dependencies are `async def`, and each reads `TrieState`
   under R2.
2. `event_sequence`, `as_of` and `config_version` are read in the same
   synchronous section as the data they describe (decision 8). `as_of` is
   rendered in RFC 3339 with `Z`, and is `null` until the first applied
   event.
3. `GET /prefix/{cidr}` answers from `trie.hot_count(prefix)`, an
   O(bit_length) walk (ADR-0014 decision 6). It never walks the subtree,
   which is epic #10's criterion.
4. **Prefix metadata is not in the v1 read API** (ADR-0015
   assumption 7):
   * `read-api-v1.md` defines no field for it;
   * nothing can declare any;
   * a field that is always empty is noise.

   ADR-0005's Consequences sentence "`GET /ip/{ip}` returns both,
   separately keyed" comes due, as an additive field, when a declaration
   source is designed. Nothing in this epic holds a `PrefixMetadataStore`.
5. `request_count` is decision 17's.
6. **Left for slice 3's own design:**
   * how `GET /prefixes/hot` avoids a full scan per request, which blocks
     the writer for as long as it takes (decision 9). The alternative is
     an index of `HOT_PREFIX` prefixes — §12's cached `prefix_state` —
     updated for the ancestors of every `APPLIED` event and rebuilt in
     `apply_config`;
   * which lengths `GET /prefixes/hot` covers. The recommendation is
     `[L_F, bit_length]`, so that it agrees with the detector, which hears
     only those;
   * what a query for a family not served answers (recommendation: 400);
   * the IPv6 `matched_prefixes` lengths;
   * `prefix_queries`' labels.

> Settled 2026-09-24 (Amendment 4): item 6's five points. `GET
> /prefixes/hot` keeps no index, and each request walks only the prefixes
> whose count could qualify (ruling 5). It covers each served family's
> `[L_F, bit_length]` (ruling 2). A query for a family the trie does not
> serve is `400` (ruling 2). IPv6's `matched_prefixes` are `/104`, `/112` and
> `/120` (ruling 2). `prefix_queries` is labelled `route` and `result`
> (ruling 9). Item 4 stands: no route needs prefix metadata (ruling 6).

### 16. What the snapshot epic inherits

* **What a snapshot holds.** It serializes `TrieState`:
  * per family, the HOT addresses (the structure is a function of the hot
    set, ADR-0014 decision 4) and the record map, as canonical texts plus
    #116's `request_count`;
  * `position`, which is `replay_position`;
  * `as_of`;
  * `config.config_version` (§33).

  `event_sequence` is derived from `position`, not stored.
* **Restore.** A restore produces a `TrieState` whose `position` is
  `replay_position`, and the worker subscribes from `position + 1`
  (decision 3).
* **`snapshot_now()`** takes the worker's lock, calls `producer.flush()`
  (slice 2), then serializes. A snapshot therefore never covers an event
  whose stats are unpublished (ADR-0010 Amendment 1), and never splits an
  event (R1, R2). This settles the periodic snapshot's relation to the
  flush, which ADR-0009 A11 left to the trie epic.
* **The final snapshot** is written in `stop()`, after the flush.
* **Gaps.** Two cases mean the restored state is not the log's:
  * the first record delivered after a restore has an offset greater
    than `S`, so the log has lost records after the snapshot — to
    retention, to a purge, or to the transport's malformed-subject skip;
  * the snapshot's `position` is at or beyond `end_offset`, so it belongs
    to a log that has since been recreated.

  The epic must refuse such a snapshot or rebuild. A silent start is not
  acceptable.

  Decision 4 step 3 passes every offset below `first_offset` that the
  trie has not read. For a restored state, `first_offset > S` is the
  first case, seen before any record is delivered, and seen even when the
  log has no record left to deliver. The epic runs its gap check before
  step 3, so that step 3 never passes a gap silently.
* **The compose file.** `deploy/docker-compose.yml` mounts
  `trie-snapshots` at `/snapshots`, while `.env.example`'s `./snapshots`
  resolves to `/app/snapshots` in the image. The compose service must set
  `HAMMERTIME_TRIE_SNAPSHOT_DIR=/snapshots`.
* **A restored document that fails validation** is ADR-0015 decision 8's
  question. It is counted as `attributes_rejected{stage="snapshot"}`.

> Amended 2026-09-23 (Amendment 2 ruling 10): two more. A restore
> re-publishes the stats of every state-changing record after the
> snapshot's position (decision 14 item 8). And no snapshot may record a
> state that a `handle()` which raised has left behind: after a
> `PrefixStatsPublishError` the state is past an event whose stats may be
> missing from the log, and after an `InvariantViolation` the trie has been
> found corrupt. That binds `stop()`'s final snapshot and the periodic one
> alike. How is the epic's to design.

> Amended 2026-09-24 (Amendment 3 ruling 1): the first of the two is
> qualified. A restore replays everything after the snapshot's position,
> as before, and the replayed events re-publish under the rule every start
> follows: from `republish_from`. When `hammertime.prefix-stats.v1` has no
> usable last message, that is every state-changing record after the
> snapshot's position.

### 17. #115 and #116

**#115** (the owner's ruling, 2026-09-23). A hot-ip event whose
attributes are invalid still has its HOT change applied, with the default
attributes. The rejection's source information and its reason go to the
logs only, and nothing extra is stored in the trie. Issue #115's text
adds that the logs carry no attacker content.

This ADR does not design #115. It fixes neither the codec's contract for
a rejected document, nor where the source information comes from, nor
what the worker does with such an event once #115 lands. It states
slice 1's interim behaviour and one constraint on slice 1:

* **The interim.** Until #115 lands, a hot-ip record whose attributes the
  codec rejects is `MALFORMED` [`invalid_attributes`] (decision 6 step 2).
  Its transition is not applied, which ADR-0015 assumption 42 already
  records. `attributes_rejected{stage="decode"}` is counted, and
  `malformed_hot_ip_event` is logged with fixed tokens and numbers only.
  The fixed token rests on decision 12's rule, which extends ADR-0015
  decision 5's, and not on #115.
* **The constraint.** Nothing in slice 1 prevents #115 from later
  applying such a transition with the default attributes.
  `apply_hot_ip_added` already accepts `attributes=None` and stores the
  default document (ADR-0015 decision 6), and nothing in slice 1 has to
  be undone.

**#116** (the owner's ruling, 2026-09-23). Each HOT address's record
holds `request_count` beside the canonical attribute text, and both are
written and removed in the same step.

* **What it means** — the sub-question the owner put to this ADR.
  `request_count` is the `window_count` of the most recent `HotIpAdded`
  applied for the address:
  * every `HotIpAdded` replaces it — a redelivered one included, and one
    that a shard's new owner emits for an address the trie already holds
    (ADR-0001 Amendment 1 clause 5);
  * `HotIpRemoved` removes it, and it reads `0` while the address is COLD.

  It is the count at the moment the address became HOT. `read-api-v1.md`
  and ADR-0010's assumption already say so, and `integration-scenarios.md`
  §3 step 6 and §6 step 4 pin it.
* **A live count is not available to the trie.** §18 makes the
  transition the only coupling, and the aggregator publishes nothing
  between transitions: ADR-0005 rejected per-observation events, and
  ADR-0011 assumption 6 rejected re-announcing. A live value would need a
  new event type or a cross-service query, and it is not recommended.
* **The worker's change** under #116 is to pass `payload.window_count` in
  both `apply_hot_ip_added` calls of decision 6 step 4 — the retry with
  `attributes=None` included, because a rejected document does not make
  the count wrong. Both calls stay inside R1's synchronous section.
* **For #116's attention:** the codec requires `window_count` on hot-ip
  events, while `schemas/hot_ip_event.v1.json` lists it as optional.

> Noted 2026-09-23 (ADR-0015 Amendment 5): #116 is designed there. The
> meaning above stands. `read-api-v1.md` now says the count comes from the
> most recent `HotIpAdded` the trie has *applied*, because one the worker
> skips sets nothing. The worker passes the count as
> `request_count=payload.window_count`. The item for #116's attention is
> settled: the codec is right, and the schema now lists `window_count` as
> required on both event types.

## Test seams

* **The apply functions.** `hammertime.trie.worker.apply_hot_ip_added` and
  `hammertime.trie.worker.apply_hot_ip_removed` are module globals of the
  worker, looked up when called. A test can replace them with
  `monkeypatch.setattr`, to reach decision 7's apply-time
  `InvalidAttributesError` path, which the bus cannot produce.
* **The logger.** The worker's log records are on the standard-library
  logger `hammertime.trie.worker` (decision 12), so pytest's `caplog` sees
  them when `configure_logging` has not been called.
* **The corruption path.** `PatriciaTrie.root` and `PatriciaTrie.arena`
  are public (ADR-0014 decision 5, Amendment 1 A1). A test reaches the
  `InvariantViolation` path by corrupting one leaf's stored count
  (ADR-0014 A12).
* **Time.** `TrieService`'s `sleep` and `monotonic` keywords drive the bus
  retry and the `trie_recovery_seconds` measurement without wall time.
* **Messages.** `handle()` takes any `ConsumedMessage`, so a test may
  build one directly rather than read it off a bus. A positional
  subscription acknowledges nothing, so nothing requires the message to
  come from the worker's own consumer — unlike the aggregator's tests.
* **The log's bounds.** `InMemoryBus` never discards: its `first_offset`
  is always `0`, and an empty memory log's `end_offset` is `0`. It
  therefore cannot show decision 4 step 3. A JetStream log that holds
  nothing to replay (`end_offset` `1` and no record, or every record aged
  out), or one whose head has aged out, is modelled by a bus double. Its
  `end_offset` and `first_offset` return what the test chooses, and its
  subscription yields what the test queues.
* **The admin app.** `TrieService.app` can be served in-process through
  `httpx.ASGITransport`, so a test can request any path, including those
  decision 13 says the app does not serve.

> Added 2026-09-23 (Amendment 2): four seams for slice 2 — the producer,
> the publisher on its own, reading what was published on the memory bus,
> and the new log record. Amendment 2's "Test seams" describes them.

> Added 2026-09-24 (Amendment 3): every bus double handed to a worker, or
> to `build_service`, implements `last_value`. A double that returns
> `None` keeps the replay publishing everything. Amendment 3's "Test
> seams" describes the rest.

> Added 2026-09-24 (Amendment 4): the read routes are reached through
> `create_app` with a `TrieState` a test writes itself, or through
> `TrieService.app`. Amendment 4's "Test seams" describes both.

## Questions this ADR closes

| Question | Left open by | Ruled in |
| --- | --- | --- |
| Are `event_sequence` and the stream sequence one number? | ADR-0010 Amendment 1 ruling 3 and its assumption "Two numbers rather than one"; ADR-0013 decision 9 and Consequences; ADR-0014 Consequences | decision 8: yes |
| What does the trie do with an event for a family it does not hold? | ADR-0014 decision 1 and Consequences | decision 5 |
| How is a trie that raised `InvariantViolation` rebuilt? | ADR-0014 A12 clause 5; ADR-0015 Consequences | decision 7: exit 1, and the restart rebuilds |
| Does an event absorbed as a no-op publish stats? | ADR-0011 Consequences (*Trie epic*); ADR-0014 decision 3; ADR-0015 decision 6 clause 5 | decision 14: no |
| How does a periodic snapshot relate to the flush? | ADR-0009 A11's assumptions | decision 16 |
| Does the read API expose prefix metadata? | ADR-0015 assumption 7 | decision 15: not in v1 |
| Does each family have its own `event_sequence`? | ADR-0001 Amendment 1's seventh assumption | decision 8: no (ADR-0001 Amendment 4) |
| What is `as_of`? | never defined | decision 8 |

Only the places whose text is now false carry a pointer note
(assumption 22). The others still read "left to the trie epic", and this
table is where the answer is.

## Assumptions

Each of these is a judgment call that epic #10, the spec and the earlier
ADRs do not make. Push back on them individually.

1. **Three slices, with #116 and slice 2 in parallel.** The epic could land
   in one piece, or be split by module. It is split by what can be tested
   on its own:
   * slice 1, the worker, is exercised through `TrieState`, with no read
     endpoint;
   * the publisher and the read API each add one observable surface;
   * the Dockerfile and compose need nothing.
2. **Two new modules, `state.py` and `metrics.py`,** beyond the epic's
   list. `service.py` is ADR-0009's. `state.py` keeps the read path and the
   snapshot epic off the worker's bus dependency, and `metrics.py` follows
   the aggregator's layout. The alternative — both inside `worker.py` —
   would make the query layer import the bus.
3. **`HAMMERTIME_TRIE_FAMILIES`, default `ipv4`.** §43 says IPv4 first, and
   §35 says the configuration should be ready for IPv6; a set of families
   is the smallest key that is both. The grammar — comma-separated,
   case-folded, duplicates collapsed, empty entries refused — mirrors
   `HAMMERTIME_SHARD_IDS` (ADR-0011 A3).
4. **An event for a family not held is skipped and counted.** It is not
   an error and not a crash: it is a deployment choice, not a malformed
   record. A crash would stop the trie over traffic its configuration
   excludes. Silence would hide a misconfiguration, which is why the first
   occurrence per family is a warning.
5. **One trie process per deployment,** ADR-0001's single logical owner.
   With decision 8's log position a split by family would be coherent.
   Its operational story — two services publishing under one `agent_id` —
   is not designed here.
6. **The trie's consumer carries the group name `hammertime-trie`.**
   `bus.consumer()` needs a name, and no durable is created, so any name
   would do. ADR-0009 decision 9's keeps that decision's table true.
7. **The key and the subject must name the payload's IP; otherwise the
   record is `MALFORMED`.** This mirrors the aggregator's ADR-0004 check,
   and the trie cannot tell which of two IPs a self-contradictory record
   means. The trie's own ordering would survive a wrong key, because it
   reads the whole stream in one order. The check is about trusting the
   record, not about ordering.
8. **Log records carry fixed reason tokens, never exception text or
   payload content.** The aggregator logs `str(exc)` for a malformed
   observation. The codec's messages can embed payload values (ADR-0015
   Amendment 3's first open item), and ADR-0015 decision 5 already keeps
   document values out of attribute messages, so the trie logs a token.
   Issue #115's text asks for the same ("no attacker content"). The
   choice does not rest on it.
9. **The log position's form: `position` counts handled records, skipped
   ones included, and the offsets `start()` passes; `event_sequence` is
   `position + 1`.** That `event_sequence` is the log position at all,
   and not a count, is not an assumption: the repository owner decided it
   on 2026-09-23, on this ADR's recommendation (decision 8). The form is
   this ADR's. "Handled" rather than "applied" means a skipped record is
   not read again after a restore, and a malformed record at the tail of
   the log does not hold readiness back. `position + 1` rather than
   `position` makes `0` mean "nothing read yet" on both buses and matches
   `end_offset`'s convention (decision 8). Counting passed offsets is
   assumption 26.
10. **`as_of` is the greatest applied event timestamp, and `null` before
    the first.** Two alternatives were rejected. The response time is
    always true, but says nothing about how fresh the data is. The apply
    time moves on every replay. The greatest rather than the latest keeps
    `as_of` monotonic, at a price: an aggregator whose clock runs ahead
    pins `as_of` ahead until real time catches up.
11. **`REDELIVERED` exists for a positional subscription.** No known path
    produces one. The check keeps the log position exact if the ordered
    consumer's recreation ever overlaps. It is logged and not counted, as
    ADR-0013 assumption 21 has it for the aggregator.
12. **`InvariantViolation` exits the process, and the restart is the
    rebuild.** An in-process rebuild would need two things: a readiness
    transition from `ready` back to `starting`, which ADR-0009 A4 does not
    have, and a second recovery path to test. The cost: a corruption the
    log itself reproduces crash-loops. It does so visibly — `start_failed`
    or `run_exited` carries the violation — which is ADR-0014 A12's "not
    kept in service".
13. **A `ValueError` from `apply_*` propagates.** The worker's routing
    makes it unreachable. If it is reached, it is a bug and must be loud.
14. **A decode-time attributes rejection is `MALFORMED` until #115.** It is
    folded into `MALFORMED` rather than given an outcome of its own, so
    that #115 retires a reason token and no outcome.
15. **No copy-on-write (decision 9).** R1-R3 give §28's guarantee at no
    cost per event. The price is that a long read blocks the writer, and
    only slice 3 can incur it.
16. **In slice 1, `apply_config` only adopts, and it takes the lock.**
    Lazy evaluation needs no pass. Taking the lock keeps the rule that no
    reader sees a version before the state that goes with it, even once
    slice 3 adds a cache.
17. **Metric names and labels.** The spec supplies `trie_updates`,
    `hot_ip_count`, `trie_nodes` and the three §46.8 series. This ADR
    supplies `hot_ip_events_skipped`, the `event_sequence` series, and the
    `family`, `event_type`, `result`, `reason` and `stage` labels.
    `trie_recovery_seconds` is ADR-0009's.
18. **The replay stays under the startup deadline.** ADR-0009 put it in
    `start()`. Moving it out would mean a service that is live but not
    ready for longer than the deadline, which ADR-0009 A4 does not model.
19. **The readiness residuals are recorded, not closed.** Decision 4
    step 3 closes the cases a stream reaches before the trie starts,
    whether on its own or by a full purge: nothing written, every record
    aged out, everything purged. Three cases remain. In each, `start()`
    waits for a record that does not come, and fails the start at the
    deadline.
    * The record at `end_offset - 1` is one the transport skips. A
      malformed subject is such a record; only a principal publishing to
      the stream directly can produce one (ADR-0013 assumption 13).
    * Every record left below the end is removed after `start()` has read
      the log's bounds and before the replay reaches it: by a full purge,
      or by retention at the age limit. A purge keeps the stream's last
      sequence (Sources), so the next record's offset is past the old end.
    * Records at the end of the log are removed while earlier ones remain.
      Only deleting single messages, or a purge filtered by subject, does
      that. Both are administrative operations on the stream, and
      `first_offset` cannot see a hole behind a retained record.

    A restart clears the second case, because it reads the bounds again.
    The first and third recur on every start until a record is appended
    after them. Closing them needs a bus signal such as "nothing pending
    below this offset", which `hammertime.bus` does not have. JetStream
    reports a pending count with every delivery (ADR-0013 Amendment 10,
    Sources). Adding the signal is a follow-up for the bus, not the trie.

    > Amended 2026-09-23 (Amendment 1): a fourth case, raised by the
    > security re-audit of slice 1. The hot-ip stream is deleted after
    > `start()` has read the log's bounds and before the replay reaches
    > the end. The second case rests on a purge keeping the stream's last
    > sequence, so that the next record lands past the old end. A deleted
    > stream keeps nothing. One provisioned again by `hammertime-provision`
    > numbers its records from 1 (ADR-0013 decision 3), because
    > `stream_config_for` sets no first sequence.
    >
    > * *Gone when `start()` reads `first_offset` or subscribes.* The read
    >   or `subscribe()` raises, and the start fails at once (decision 7).
    >   A stream already provisioned again by then gives the wait of the
    >   next bullet.
    > * *Deleted during the replay, and provisioned again.* nats-py finds
    >   the subscription's consumer gone within 20 s. Every 10 s it tries
    >   to recreate it on the stream of the same name, from one past the
    >   last offset it received (Amendment 1, Sources), and it succeeds
    >   once the stream exists again. That offset is in the old numbering.
    >   The trie applies none of the new stream's records at or below its
    >   position: the server either does not deliver them, or the worker
    >   skips them as `REDELIVERED` (decision 6 step 1). The replay
    >   therefore waits until the new stream grows back to the old end, or
    >   until the deadline. After the deadline, the restart reads the new
    >   stream's bounds, and in slice 1, where every start is a full
    >   replay, that clears the case. If the new stream reaches the old end
    >   first, the start completes. The trie then keeps what it applied
    >   from the old stream and lacks the new stream's records up to its
    >   position, until it restarts.
    > * *Deleted during the replay, and not provisioned again.* Every
    >   attempt to recreate the consumer fails and is logged as
    >   `nats_client_error` (ADR-0013 decision 3). The replay waits,
    >   and the start fails at the deadline. A restart does not clear this
    >   case. The next start waits in `NatsBus.start()` for the missing
    >   stream, with `dependency_unavailable dependency=bus` records, and
    >   fails at the deadline too (ADR-0013 decision 2), until the stream
    >   is provisioned again.
    >
    > Decision 16's recreated-log case does not cover this. It is a check
    > on a restored state, made once before step 3, and it cannot see a
    > stream recreated after the bounds were read. Deleting a stream is an
    > administrative operation, open to any principal that reaches the bus
    > (ADR-0013 assumption 13). The same recreation under a trie that has
    > finished `start()` is not a readiness case, and is not ruled here
    > (assumption 36).
20. **No cap on the hot set.** The trie mirrors what the aggregators hold
    HOT. Refusing a `HotIpAdded` at a cap would break §12 against the true
    hot set, and the aggregator does not evict HOT IPs either (ADR-0011
    decision 2). The hot-ip log is inside the trust boundary (ADR-0013
    assumption 13).
21. **The snapshot keys are named now and land with the snapshot epic.**
    A key that is parsed but read by nothing would tell an operator that
    snapshots exist.
22. **Pointer notes only where earlier text is now false.** The places
    that merely left a question to this epic are answered in "Questions
    this ADR closes". A later dispatch can add closing pointers if they
    are wanted.
23. **`trie_recovery_seconds` covers all of `start()`, bus connection
    included,** as ADR-0009 decision 5 step 6 words it ("the time spent
    in `start()`").
24. **`build_service` accepts `clock` and leaves it unused in slice 1,** so
    that the signature is ADR-0009's from the first slice and the snapshot
    epic changes no caller.
25. **No `CHANGES` entry from this ADR.** It changes documents only. Slice
    1's implementing change writes the three lines under Consequences.

Assumptions 26-32 were added by the revision of 2026-09-23 (see
"Revision 2026-09-23").

26. **`start()` moves the position past what the log no longer holds. It
    does not only count such a log as caught up.** The security audit
    proposed the test `end <= max(S, first)`. On its own, that test leaves
    `event_sequence` at `0` on a log that holds nothing to replay. A
    restarted trie would then report `0` where its predecessor reported
    `end`, which goes backwards, against decision 8 and `read-api-v1.md`.
    Moving the position makes the one test `event_sequence >= end` hold in
    every case, and keeps `event_sequence` "the offset of the next record
    the trie will read". The cost: `position` can name an offset whose
    record the trie never handled, and on JetStream a started trie
    reports at least `1` before it has read anything.
27. **Step 3 runs whenever `first > event_sequence`, not only when the log
    holds nothing below `end`.** Passing a head that has aged out changes
    nothing the replay delivers: a subscription from `0` starts at the
    first retained record anyway (ADR-0013 decision 3). It keeps one rule
    instead of two, because the subscription always starts at
    `event_sequence`. The narrower rule was the alternative. During the
    replay of such a log it leaves `event_sequence` at `0`, which only a
    reader before readiness could see.
28. **`end_offset` first, then `first_offset`, as two calls.** A single
    call returning both from one `stream_info` would be atomic on
    JetStream. The reading order already gives the property that matters
    (decision 4, "Why `end` is read first"), and a second method leaves
    `end_offset` and its tests as they are (ADR-0013 Amendment 10,
    assumption 133). The cost is a second broker round trip per start.
29. **The pass takes the worker's lock, and is skipped once `stop()` has
    begun.** It writes `TrieState`. Once `stop()` has begun, neither
    `handle()` nor `apply_config()` changes the state (decision 13), and
    the pass follows the same rule in the same way as `apply_config`. A
    worker stopped before `start()` therefore stays not caught up on a
    log that holds nothing to replay.
30. **`replay_complete` carries `first_offset`.** An operator can then
    see why a replay took no records, or started past `0`, without
    another record. The pass itself writes no record.
31. **The trie's app serves no generated API documentation, in any
    slice, and all three URLs are `None`.** The security audit asked for
    this (finding 2). The admin port has no authentication. The two
    documentation pages would load third-party scripts, at a floating
    major version, into an operator's browser. And `read-api-v1.md` is
    already the contract. In FastAPI 0.141.1, `openapi_url=None` alone
    also disables the two pages (Sources). Naming all three keeps the
    intent visible in the code, and does not rest on that coupling.
32. **No `CHANGES` change from the revision.** The slice-1 lines are not
    on master yet. The second line already says the trie reports ready
    once its replay has reached the log end as it stood at startup, and a
    log with nothing to replay is at its end. The first line already
    names the only three routes the trie serves. `first_offset` is
    internal to the repository. Nothing that a released build does
    changes.

## Consequences

* **Slice 1 lets the trie start.** The container starts under
  `docker compose` and reports ready once its replay reaches the log end.
  On a fresh deployment that is at once, because the provisioned hot-ip
  stream is empty (decision 4 step 3). That is the trie's share of the
  `integration` job's first re-enable condition (#52; `CLAUDE.md`,
  "Disabled CI coverage").
* **Until the snapshot epic lands, every start replays the whole retained
  hot-ip log,** inside `HAMMERTIME_STARTUP_TIMEOUT_S` (60 s by default). A
  log that takes longer fails the start with exit 1, and raising the
  deadline is the only remedy until snapshots exist.
* **Until the snapshot epic lands, a restarted trie also loses long-lived
  HOT IPs.** Once `hammertime.hot-ip.v1`'s head has aged out (`max_age` 30
  days), a restarted trie lacks every IP that became HOT before the oldest
  retained record and is still HOT. The aggregator does not re-announce
  (ADR-0011 assumption 6), so nothing restores such an IP until it
  transitions again. The snapshot epic is therefore a correctness
  prerequisite for any deployment older than the hot-ip retention, not an
  optimisation. `event_sequence` is unaffected (decision 8).
* **Slice 2 without the snapshot epic republishes.** Every start
  re-publishes the stats of every retained event (decision 14 item 8). The
  log deduplicates inside 120 s, and the detector's sequence rule absorbs
  the rest.

  > Amended 2026-09-23 (Amendment 2 ruling 4): readiness waits for those
  > publishes, and each state-changing record now costs a broker round trip
  > and 25 acknowledged publishes. By an estimate from ADR-0013's measured
  > publish rate, the default 60 s deadline then holds at most about 40,000
  > such records. A longer log fails the start until the deadline is raised
  > or the snapshot epic lands.

  > Amended 2026-09-24 (Amendment 3 ruling 1): no longer so. A start
  > re-publishes only from the `sequence` of the last message in
  > `hammertime.prefix-stats.v1`, when Amendment 3 ruling 1's table
  > accepts that message. It costs about what a start cost before slice 2,
  > and a restart loop no longer appends the stats of the log's head again
  > each time.
  > The full re-publish, and the estimate above, apply only when that
  > stream has no usable last message. A start that then fails keeps what
  > it published, and the next start continues after it.
* **The detector epic can rely on the trie's `event_sequence`** never
  going backwards across trie restarts, while the stream exists
  (decision 8).
* **`CHANGES`.** Slice 1's implementing change adds three lines. None is
  `BREAKING`, because no trie build has shipped:
  * `Add trie service: consumes hammertime.hot-ip.v1 and applies each HotIpAdded/HotIpRemoved to the trie and the address's attribute record in one step, serving GET /healthz, GET /readyz and GET /metrics on HAMMERTIME_TRIE_QUERY_BIND (default 0.0.0.0:8081)`
  * `Trie service replays hammertime.hot-ip.v1 from the oldest retained record on every start and reports ready only once the replay has reached the log end as it stood at startup; a replay that outlasts HAMMERTIME_STARTUP_TIMEOUT_S fails the start`
  * `Add HAMMERTIME_TRIE_FAMILIES (default ipv4): the address families the trie service holds; a hot-ip event for any other family is skipped and counted`

  > Noted 2026-09-24 (Amendment 4 ruling 11): slice 3's implementing change
  > adds four lines, none `BREAKING`.
* **Security posture.**
  * The trie trusts `hammertime.hot-ip.v1`, which is inside the boundary
    ADR-0013 assumption 13 states.
  * It bounds nothing a principal on the bus could send (assumption 20).
  * It logs nothing a principal on the bus wrote (decision 12).
  * Slice 1 exposes no domain endpoint. The compose file publishes the
    admin port 8081 on every interface; ADR-0013 Amendment 4 leaves that
    question with the owner.
  * The admin app serves its three routes and nothing else: no OpenAPI
    schema, and no documentation page that loads scripts from a CDN
    (decision 13).

  > Amended 2026-09-24 (Amendment 4 ruling 7): "Slice 1 exposes no domain
  > endpoint" describes slice 1. Slice 3 serves the read API on the same
  > port, with no authentication. It tells any client that reaches the port
  > which addresses are HOT, with their request counts and attribute
  > documents, and which prefixes are `HOT_PREFIX`. A client that repeats
  > `GET /prefixes/hot` delays the writer by one walk each time. The port's
  > exposure is still the owner's question, raised again with this ADR's
  > Amendment 4 and not ruled there.

## Sources

* FastAPI, `docs/en/docs/async.md` on `master`, read 2026-09-23 through a
  fetch tool that quotes
  (`https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs/async.md`).
  The published site, `fastapi.tiangolo.com`, is blocked by this
  environment's egress proxy.
  * "Path operation functions": "When you declare a *path operation
    function* with normal `def` instead of `async def`, it is run in an
    external threadpool that is then awaited, instead of being called
    directly (as it would block the server)."
  * "Dependencies": "If a dependency is a standard `def` function instead
    of `async def`, it is run in the external threadpool."

  Checked against the installed FastAPI 0.141.1 (`uv.lock`):
  `.venv/.../fastapi/routing.py` lines 344-354, `run_endpoint_function`,
  awaits the endpoint when it is a coroutine function and otherwise returns
  `await run_in_threadpool(dependant.call, **values)`. Taken from it: R3.
* nats-server, `server/memstore.go` on `main`, read 2026-09-23 through the
  same tool, which quoted the lines of `purge` for a full purge:
  `fseq = ms.state.LastSeq + 1` when no sequence is given, then
  `ms.state.FirstSeq = fseq` and `ms.state.LastSeq = fseq - 1`. A full
  purge therefore keeps `LastSeq`, and the next message is stored at the
  old `LastSeq + 1`. Only the memory store was read; the file store the
  reference deployment uses (ADR-0013 decision 2) was not checked. Taken
  from it: assumption 19's claim that a purge does not rewind the offsets
  readiness waits for. That claim is only as good as this reading.

  Read again on 2026-09-23 for the revision, through the same tool, which
  quoted two more places. In `storeRawMsg`: `if ms.state.Msgs == 0 {
  ms.state.FirstSeq = seq ... }`, and later `ms.state.LastSeq = seq`. In
  `updateFirstSeq`, when no message remains: `// Like purge.`, then
  `ms.state.FirstSeq = ms.state.LastSeq + 1`. The tool reported no
  assignment of either field when a store is created, unless the stream's
  configured first sequence is above 0. So a stream nothing has been
  written to reports both as `0`, and one that expiry or a purge has
  emptied reports `first_seq = last_seq + 1`. Taken from it: decision 4's
  three cases, and why ADR-0013 Amendment 10 tests for emptiness with the
  message count and not with `first_seq`.
* nats-server, `server/filestore.go` on `main`, read 2026-09-23 through
  the same tool. Its excerpt did not reach `purge` or `selectNextFirst`.
  It quoted `expireMsgsOnRecover`: the state is reset
  (`fs.state.FirstSeq, fs.state.LastSeq = 0, 0`), the last block's
  `last.seq` is kept, and when no block remains the store calls
  `fs.writeTombstone(last.seq, last.ts)`. The tool concluded that the last
  sequence survives a server restart through that tombstone. Only this
  excerpt was read; the file store's purge was not.
* FastAPI 0.141.1 as installed (`uv.lock`), read 2026-09-23 in
  `.venv/lib/python3.12/site-packages/fastapi/`:
  * `applications.py`: `openapi_url` defaults to `"/openapi.json"`, and
    its docstring says "If you set it to `None`, no OpenAPI schema will
    be served publicly, and the default automatic endpoints `/docs` and
    `/redoc` will also be disabled." `docs_url` defaults to `"/docs"`,
    `redoc_url` to `"/redoc"`, and `swagger_ui_oauth2_redirect_url` to
    `"/docs/oauth2-redirect"`. `setup()` adds the schema route only `if
    self.openapi_url`, the Swagger UI page and its OAuth2 redirect only
    `if self.openapi_url and self.docs_url`, and the ReDoc page only `if
    self.openapi_url and self.redoc_url`.
  * `openapi/docs.py`: the pages' script defaults are
    `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js`,
    with `swagger-ui.css` from the same `swagger-ui-dist@5` path, and
    `https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js`.
    These are major-version tags, which the CDN resolves when the page
    loads.

  Taken from it: decision 13's rule and assumption 31.
* nats-py 2.16.0 as installed, read 2026-09-23. `nats/js/api.py`:
  `StreamState(messages: int, bytes: int, first_seq: int, last_seq: int,
  consumer_count: int, ...)`. `nats/aio/msg.py`: `Msg.Metadata.num_pending`,
  documented as "the number of available messages in the Stream that have
  not been consumed yet". Taken from it: the fields ADR-0013 Amendment
  10's `first_offset_of` reads, and assumption 19's note that JetStream
  reports a pending count with every delivery.
* Blocked on 2026-09-23 by this environment's egress proxy: `docs.nats.io`
  and `www.synadia.com`. The source code above was read in their place.

## Edits to other documents

Every edit is listed here. Where an edit replaces wording, the words it
replaces are quoted.

* **`docs/adr/0001-single-logical-trie.md`** (Amendment 4):
  * the status line gains a clause naming Amendment 4;
  * a dated blockquote after the 2026-09-21 blockquote that follows
    clause 6 re-reads clause 4's "for the trie, its own count of hot-ip
    events applied so far";
  * a dated blockquote after the 2026-09-21 blockquote that follows
    Amendment 1's assumptions supersedes the seventh assumption's "Each
    family's trie has its own `event_sequence`";
  * a new "Amendment 4" section at the end lists these.
* **`docs/adr/0010-read-apis-and-shared-prefix-predicate.md`**
  (Amendment 2):
  * the status line gains a clause naming Amendment 2;
  * decision 3 gains a dated blockquote after its 2026-09-21 one. It
    qualifies "On each applied `HotIpAdded`/`HotIpRemoved`", re-reads "the
    trie's own monotonically increasing `sequence` (one counter for the
    whole service, incremented per applied hot-IP event ...)", and adds
    the `subject` rule;
  * decision 4 gains a dated blockquote on "`event_sequence` (the sequence
    of the last hot-ip event applied)", which also defines the trie's
    `as_of`;
  * Amendment 1 ruling 3 gains a nested dated blockquote. It records the
    owner's decision on the open question and re-reads ruling 2's "the
    `offset` of the last hot-ip message applied";
  * a new "Amendment 2" section at the end lists these.
* **`docs/adr/0013-nats-jetstream-event-log-static-shards.md`**
  (Amendment 9):
  * the status line gains a clause naming Amendment 9;
  * decision 9's second bullet gains an italic dated sentence after
    "whether the trie epic unifies the two is left to it (see the open
    question in the hand-off report)";
  * Consequences' "Not done here, named" bullet gains an italic dated note
    after "unifying `replay_position` and `event_sequence`";
  * a new "Amendment 9" section at the end lists these.
* **`docs/spec/hammertime_spec_1.md`**:
  * §22's note: "`event_sequence` (the trie's count of applied hot-ip
    events; the detector reports that counter as carried on the newest
    `PrefixStatsChanged` it applied)" is reworded to name the trie's log
    position, with a dated parenthesis citing this ADR;
  * §28, §33 and §35 each gain an "ADR-0017" pointer note, after their
    last paragraph (§28, §35) or after the existing ADR-0009 note (§33).
* **`docs/spec/README.md`**:
  * the preamble names this ADR and the notes it adds;
  * the §19, §22, §28, §29, §32/33, §35, §37, §46 and §47 rows gain
    `docs/adr/0017`, and all but §29 and §47 gain the new trie modules;
  * two citations are widened: the §22 row's `docs/adr/0001` "(Amendments
    1, 2, 3)" becomes "(Amendments 1, 2, 3, 4)", and the §32/33 row's
    `docs/adr/0010` "(Amendment 1)" becomes "(Amendments 1, 2)".
* **`docs/protocol/read-api-v1.md`**, the preamble. The sentence
  "`event_sequence` is the responding service's own counter (§22, ADR-0003
  amendment): for the trie, the number of hot-IP events applied so far; for
  the detector, the `sequence` of the newest `PrefixStatsChanged` applied."
  becomes an introduction ("its own position or counter") and two bullets.
  The trie's bullet defines its value as its log position; the detector's
  bullet is unchanged in content. A new sentence after them defines the
  trie's `as_of`.
* **`docs/spec/integration-scenarios.md`** §5 step 7. "`event_sequence ==
  256` = 100 + 100 + 56 applied events" now explains the 256 as a log
  position. The number is unchanged, and so is step 9's 257.
* **`docs/runbook.md`**, "Trie service restart". Three bullets follow the
  paragraph: what the snapshot's `event_sequence` is, the replay's
  deadline, and the `trie_invariant_violation` exit. The paragraph itself
  is unchanged. Under decision 8 its "replays ... from the snapshot's
  `event_sequence`" is exactly right.

Added by the revision of 2026-09-23. On master are `docs/adr/0013`
decisions 3 and 9, its assumption 20 and its Consequences, and
`docs/adr/0010` Amendment 1 ruling 4; each gains a dated note. Everything
else below was written by this ADR's own change set, which is not on
master, and is edited in place.

* **`docs/adr/0013-nats-jetstream-event-log-static-shards.md`** (Amendment
  10):
  * the status line gains the tenth clause;
  * decision 3 gains `first_offset` in the interface block, a paragraph
    defining it after the "Added:" line, `first_offset` and
    `first_offset_of` in the `NatsBus` block, and an italic sentence in
    the `hammertime.bus.memory` paragraph;
  * decision 9's readiness bullet, assumption 20 and the Consequences bus
    bullet each gain a dated italic note;
  * a new "Amendment 10" section at the end lists these.

  In this change set's own text there, decision 9's italic sentence dated
  2026-09-23 said "one past the offset of the last hot-ip record it has
  handled", and Amendment 9's "Why" said "one past the stream offset of
  the last hot-ip record it has handled". Both now say "the (stream)
  offset of the next hot-ip record it will read". Amendment 9's
  assumption 131 gains an italic sentence.
* **`docs/adr/0010-read-apis-and-shared-prefix-predicate.md`**:
  * Amendment 1 ruling 4 gains a dated blockquote after its 2026-09-21
    one;
  * in Amendment 2, which this change set wrote:
    * the status clause gains "Amendment 1's readiness test also reads the
      log's first retained offset";
    * the section title gains "; readiness reads the first retained
      offset";
    * "Why" said "ADR-0017 designs the trie service (epic #10) and answers
      three questions this ADR left open or stated too loosely:". It now
      adds that ADR-0017 "corrects one test this ADR restated", and gains
      a fourth bullet;
    * "Decisions 3 and 4 and Amendment 1 ruling 3 carry dated notes"
      became "Decisions 3 and 4 and Amendment 1 rulings 3 and 4 carry
      dated notes";
    * the edit list's ruling 3 entry says "handled or passed", and a
      ruling 4 entry is added;
  * decision 4's 2026-09-23 note said "one past the stream offset of the
    last record it has handled", and now says "the stream offset of the
    next record it will read";
  * ruling 3's nested 2026-09-23 note said "`replay_position + 1`, one
    past the offset of the last hot-ip record the trie has handled" and
    "now reads "handled": a record the trie skips (malformed, or of a
    family it does not serve) is handled too, and is not read again after
    a restore". It now says "the offset of the next hot-ip record the trie
    will read" and "handled or passed", and says what a passed offset is.
* **`docs/adr/0001-single-logical-trie.md`**: clause 4's 2026-09-23 note
  said "one past the stream offset of the last record it has handled",
  and Amendment 4's "Why" said "one past the stream offset of the last
  record the trie has handled". Both now say "the stream offset of the
  next record" it or the trie "will read".
* **`docs/spec/hammertime_spec_1.md`**:
  * §22's note said "one past the stream offset of the last record it has
    handled", and now says "the stream offset of the next record it will
    read";
  * §33's ADR-0017 note said "that integer is the offset of the last
    hot-ip record the trie handled, whether it applied the record or
    skipped it. The trie's `event_sequence` (Section 22) is that offset
    plus one, so". It now also names "the last offset it passed at startup
    because the log no longer held a record there", calls
    `event_sequence` "the offset of the next record it will read", and
    ends with "A log that retains no record is replayed at once."
* **`docs/protocol/read-api-v1.md`**:
  * the preamble's trie bullet said "one past the stream offset of the
    last hot-ip record it has handled. It only grows, across restarts too,
    and it is not a count of events (ADR-0017 decision 8);". It now
    defines the value as the offset of the next record the trie will read,
    says that offsets whose records are gone at start are skipped over,
    says the value is at least 1 on the reference deployment, bounds "only
    grows" by "while the stream exists" (decision 8's own condition), and
    cites decisions 4 and 8;
  * a new paragraph after the admin section's 404/405 paragraph says the
    trie serves no generated API documentation.
* **`docs/spec/README.md`**: the §32/33 row gains "`MessageBus.end_offset`
  and `first_offset`" and ADR-0013 "Amendment 10". In the §47 row, "the
  trie's settings, readiness and drain" became "the trie's settings,
  readiness, admin routes and drain".

## Revision 2026-09-23 (before merge) — the slice-1 security audit's two findings

Why: `security-auditor` reviewed slice 1 as committed on branch
`claude/eager-gates-lyihfk` (5d5c544) and returned two findings against
this ADR's design.

1. **Medium, availability.** Take a JetStream hot-ip stream that holds no
   record at or after the fresh start offset: never written, or every
   record aged out or purged. On it, `caught_up` could never become true.
   `start()` waited until `HAMMERTIME_STARTUP_TIMEOUT_S`, and the trie
   exited 1 instead of reporting ready. `make up` on a fresh deployment
   reaches this, because provisioning creates the stream empty:
   `end_offset` is `1`, and a fresh trie subscribes from `0`. It needs no
   attacker. It contradicted Consequences ("reports ready") and ADR-0013
   decision 9's claim that one readiness test works on both buses.
   Assumption 19 had recorded the wait only for a transport-skipped tail
   record and a purge during the replay.
2. **Low, attack surface.** `create_app` built `FastAPI` with its default
   `openapi_url`, `docs_url` and `redoc_url`. The unauthenticated admin
   port therefore also served `/openapi.json`, `/docs`,
   `/docs/oauth2-redirect` and `/redoc`, beyond decision 13's three
   endpoints, and the two pages load scripts from a CDN into an operator's
   browser.

**Rulings.**

* *Finding 1.* Decision 4 gains step 3, and ADR-0013 Amendment 10 adds
  `MessageBus.first_offset`. Before subscribing, the worker reads the
  log's first retained offset after its end, and passes every offset
  below it that it has not read; caught up is then `event_sequence >=
  end`. This takes the audit's direction, "treat `end <= max(S, first)`
  as caught up", and extends it: the position moves as well
  (assumption 26).
* *Coherence of `event_sequence` and the resume offset when the first
  retained offset is above 0* (asked by the dispatching session). After
  step 3 they are one number in every case.
  * A fresh replay of a log whose head has aged out subscribes from
    `first`. Each record it handles sets `event_sequence` to one past that
    record: the value a trie that had read the whole log would hold.
  * A log that holds nothing to replay leaves `event_sequence` at `first`,
    which is at least `end`, and so at least what the trie's predecessor
    reported. It is not `0`.
  * The resume offset is `event_sequence` by construction (decision 3),
    and the snapshot's `replay_position` is `event_sequence - 1`.
  * A restored state whose `replay_position + 1` is below `first` is
    decision 16's first gap. Decision 16 now says the snapshot epic
    checks for it before step 3.
* *Finding 2.* Confirmed. Decision 13 now pins `openapi_url=None,
  docs_url=None, redoc_url=None` (assumption 31).

**Convention.** This ADR is committed on this branch and is not on
master, so none of its text is superseded merged text. It is edited in
place, and this section lists each edit with the words it replaced, so
that the audited text can be recovered. Text that is on master is amended
by dated notes: ADR-0013 decisions 3 and 9, its assumption 20 and
Consequences (ADR-0013 Amendment 10), and ADR-0010 Amendment 1 ruling 4
(ADR-0010 Amendment 2). Text that this ADR's change set wrote in other
documents is edited in place, and is listed under "Edits to other
documents".

**Scope.** Ingest's FastAPI app keeps the same four default paths. The
dispatching instruction left it out of scope, and nothing here rules on
it. The detector's readiness on its durable subscription is named in
ADR-0013 Amendment 10, and is not ruled.

**Edits in this ADR**, with the words they replaced:

* **Status line.** "ADR-0013 (Amendment 9)" became "ADR-0013 (Amendments
  9 and 10)", and the closing sentences on this revision were added.
* **Decision 1.** Slice 1's row gained "; in `hammertime-bus`,
  `MessageBus.first_offset` on both buses (ADR-0013 Amendment 10)".
* **Decision 2.** `position`'s comment was "offset of the last hot-ip
  record handled; None before the first". `note_passed` was added to the
  block, with its own bullet. "**What both refuse.** An `offset` below 0,
  or not greater than `position`, is a `ValueError`, and nothing changes."
  became "What all three refuse", naming the three methods. "the only
  production caller of the three mutators" became "four mutators".
* **Decision 3.** The subscription's bullets were "`S` is `0` when
  `state.position` is `None`, and `state.position + 1` otherwise;" and "a
  fresh `TrieState` has no position, so in slice 1 every start replays
  from the first record the log retains (`start_offset=0`, ADR-0013
  decision 9 as amended);".
* **Decision 4.** Steps 2 to 5 were: "2. It reads `end = await
  bus.end_offset(HOT_IP.name)` *before* subscribing, and keeps it as
  `replay_target`. 3. It subscribes (decision 3). 4. It takes the next
  message and awaits `handle()` on it, until the worker is *caught up*.
  Caught up means `end <= S`, or `state.event_sequence >= end` (which is
  `state.position >= end - 1`)." — followed by the two sub-bullets that
  are now under step 5 — "5. It logs `replay_complete` (decision 12)."
  They are now steps 2 to 6. The paragraphs from "`caught_up` is `False`
  until" to "A restored state" are new.
* **Decision 7.** The table row "`bus.end_offset`, `subscribe`" gained
  `bus.first_offset`.
* **Decision 8.**
  * The `position` bullet was: "`position` is the offset of the last
    hot-ip record the worker has *handled* — a record whose outcome was
    `APPLIED`, `UNCHANGED`, `MALFORMED` or `FAMILY_NOT_SERVED`. It is
    `None` before the first."
  * "a skipped record is handled too, and need not be read again" gained
    "and a passed offset has nothing to read".
  * "with `position` counting handled records — is this ADR's own
    (assumption 9)" became "counting handled records and passed offsets —
    is this ADR's own (assumptions 9 and 26)".
  * "A log position re-derives the same value for the same record
    wherever the replay starts, as long as the stream exists." was
    replaced by three sentences.
  * *What it costs* gained the three sentences on JetStream's offset 0.
  * *Why `position + 1`*: "`0` then means "nothing handled" on both
    buses." and "On the memory log it equals the number of records the
    trie has passed." were replaced.
* **Decision 9.** R1's "The worker changes `TrieState` only in `handle()`
  (decision 6, steps 2-5) and in `apply_config()`." now names step 3 of
  `start()` as well.
* **Decision 12.** `replay_complete`'s fields were "`start_offset`,
  `replay_target`, `event_sequence`".
* **Decision 13.** In the `TrieWorker.stop()` bullet, "`handle()` returns
  `STOPPED`, and `apply_config()` changes nothing;" now also says that
  `start()`'s step 3 passes nothing. The `create_app` bullet gained its
  second paragraph.
* **Decision 16.** "Gaps" gained its last paragraph.
* **Test seams.** "The log's bounds" and "The admin app" were added.
* **Assumption 9.** Its title was "The log position's form: `position`
  counts handled records, skipped ones included, and `event_sequence` is
  `position + 1`." In its body, "and readiness can pass a malformed record
  at the tail of the log" and "makes `0` mean "nothing handled"" were
  replaced, and the closing sentence pointing to assumption 26 was added.
* **Assumption 19** was: "**The readiness residuals are recorded, not
  closed.** * If the record at `end_offset - 1` is one the transport
  skips, `start()` waits for the next record, and fails the start if none
  comes in time. A malformed subject is such a record; only a principal
  publishing to the stream directly can produce one (ADR-0013 assumption
  13). * Records purged between reading the log end and replaying it are
  handled the same way. A purge keeps the stream's last sequence
  (Sources), so the next record's offset is past the old end. Closing
  either needs a bus signal such as "nothing pending", which
  `hammertime.bus` does not have. That is a follow-up for the bus, not
  the trie."
* **Assumptions 26 to 32**, and the sentence introducing them, were added.
* **Consequences.** The first bullet gained "On a fresh deployment that is
  at once, because the provisioned hot-ip stream is empty (decision 4
  step 3)." *Security posture* gained its last bullet.
* **Sources.** The `memstore.go` entry gained a second paragraph. The
  `filestore.go`, FastAPI, nats-py and blocked-sites entries were added.
* **Edits to other documents.** The revision's entries were added.

## Amendment 1 (2026-09-23) — four follow-ups from slice 1: a fourth readiness case, the runbook's deadline advice, a stale restatement in ADR-0010, and the offset reads' error order

Why: four items were flagged while slice 1 was reviewed and audited, and
were left for a change to documents only, after this ADR merged. This ADR
and the texts below are on master, so nothing merged is rewritten. Each
item is a dated note, or a paragraph added beside unchanged text.

1. **A fourth readiness case.** The security re-audit of slice 1 raised a
   case that assumption 19 does not list: the hot-ip stream deleted while
   `start()` runs, whether it is provisioned again or not. Assumption 19's
   second case relies on a purge keeping the stream's last sequence, and a
   deleted stream keeps nothing. Decision 16's recreated-log case does not
   cover it, because that is a check on a restored state, made before
   step 3.
2. **The runbook's deadline advice.** `docs/runbook.md` tells an operator
   whose trie keeps failing its start at the deadline to raise the
   deadline. Assumption 19's first and third cases recur on every start
   until the next hot-ip transition, and for them a longer deadline does
   not help.
3. **A stale restatement in ADR-0010.** ADR-0010 Amendment 1 ruling 1
   restates ADR-0013 decision 9 with "`start_offset=1` with no snapshot".
   ADR-0013 Amendment 1 (ruling C5.7) corrected decision 9 to
   `start_offset=0` on 2026-09-21, and the restatement was not updated.
4. **The offset reads' error order.** ADR-0013 decision 3 says that
   `NatsBus.end_offset` and `first_offset` raise `KeyError` for an
   unregistered topic, and `RuntimeError` before `start()`. It does not
   say which they raise when both apply. The bus tests pin `KeyError` as
   their own assumption. ADR-0013 Amendment 11 confirms it.

**Edits in this ADR.**

* **Status line.** Gained the closing sentence on this amendment.
* **Assumption 19.** A dated blockquote after its last paragraph records
  the fourth case. The assumption's own text is unchanged.

**Edits to other documents.** None of them replaces wording.

* **`docs/adr/0010-read-apis-and-shared-prefix-predicate.md`.** Amendment 1
  ruling 1 gains a dated blockquote: its "`start_offset=1` with no
  snapshot" is stale, ADR-0013 Amendment 1 ruling C5.7 changed it to
  `start_offset=0`, and decision 3 of this ADR gives the trie's start as
  built. ADR-0010 gains no status clause and no amendment section
  (assumption 39).
* **`docs/runbook.md`**, "Trie service restart", the "Its deadline" bullet.
  A second paragraph names assumption 19's first and third cases by what
  an operator sees. It says that only the next hot-ip transition ends the
  wait, and that a longer `HAMMERTIME_STARTUP_TIMEOUT_S` does not help in
  those cases. The bullet's first paragraph is unchanged.
* **`docs/adr/0013-nats-jetstream-event-log-static-shards.md`**
  (Amendment 11). The status line gains the eleventh clause. Decision 3
  gains a dated italic paragraph after the `first_offset` paragraph. A new
  "Amendment 11" section at the end records the ruling and its
  assumptions.

Assumptions made by this amendment (push back individually; numbering
continues the ADR's list):

33. **The fourth case is recorded, not closed,** like the other three.
    Closing it needs a way to tell that the stream being read was
    recreated, and the trie has none. The offsets it is handed keep
    rising across the recreation. Designing a signal is left to a
    follow-up.
34. **The consumer's behaviour is read from code, not observed.** The
    note follows nats-py 2.16.0's ordered consumer as installed (Sources
    below). Two details belong to that version: the check every 10 s, and
    the restart one past the last offset received. How the server places
    a consumer whose start lies past a new stream's end was not read. The
    note therefore claims only what holds either way: the trie applies
    none of the new stream's records at or below its position. Nothing was
    run against a server. The integration job (#52) is where the case can
    be observed.
35. **A re-provisioned stream numbers from 1.** This rests on ADR-0013
    decision 3 ("they start at 1") and on `stream_config_for` setting no
    first sequence. nats-server's stream creation was not read for this
    amendment.
36. **A running trie is named, not ruled.** When the stream is recreated
    under a trie that has finished `start()`, its consumer resumes in the
    same way. That is not a readiness case, and this amendment was asked
    only about `start()`.
37. **The runbook names only the first and third cases.** They are the two
    that keep failing the start in the same way as a slow replay, which is
    the failure the bullet's advice is written for. The second clears on a
    restart. The fourth either clears on a restart or shows records of its
    own: `nats_client_error` during the wait, then `dependency_unavailable
    dependency=bus` at every later start.
38. **"Does not help" is written for the operator.** A longer deadline
    helps only if the next hot-ip transition happens to arrive inside it,
    and an operator cannot plan on that. The runbook says what ends the
    wait instead.
39. **ADR-0010's note is an erratum, with no amendment section of its
    own.** It brings a restatement into line with a ruling made on
    2026-09-21, and rules nothing. It is listed here with the rest of this
    change set, and ADR-0010 gains no Amendment 3 for it.
40. **No `CHANGES` entry.** Only documents change. Nothing that a released
    build does changes.

Read on 2026-09-23 for this amendment. No web source was consulted.

* nats-py 2.16.0 as installed (`uv.lock`), in
  `.venv/lib/python3.12/site-packages/nats/`:
  * `js/client.py`, `JetStreamContext.subscribe`: with `stream` given and
    no `durable`, it calls `self._jsm.add_consumer(stream, config=config)`
    directly. For `ordered_consumer=True` it sets `config.idle_heartbeat`
    to `config.idle_heartbeat or 5` when the caller gives none.
    `subscribe_bind` starts `_JSI.activity_check` when
    `config.idle_heartbeat` is set.
  * `js/client.py`, `_JSI.activity_check`: it sleeps `self._hbi *
    hbc_threshold`, with `hbc_threshold = 2`. If no message or heartbeat
    arrived in that time, it calls `reset_ordered_consumer(self._sseq +
    1)`. `reset_ordered_consumer` sets `deliver_policy =
    BY_START_SEQUENCE` and `opt_start_seq = sseq`, and starts
    `recreate_consumer`. That calls `add_consumer(self._stream, ...)` and
    passes any exception to `self._conn._error_cb(err)`.
  * `aio/client.py`, `_process_msg`: any message on the subscription sets
    `jsi._active = True`. For an ordered consumer, each in-order data
    message sets `jsi._sseq = sseq`, the stream sequence of the last
    message the client received.
  * `js/manager.py`: `add_consumer` sends its request through
    `_api_request`, which raises `APIError.from_error(resp["error"])` on
    an error response.

  Taken from it: the fourth case's bullets, and assumption 34's caution.
* Repository facts. In `packages/hammertime-bus/src/hammertime/bus/nats.py`:
  `_open_positional` subscribes with `ordered_consumer=True` and sets no
  `idle_heartbeat`; `_pump_push` treats a `next_msg` timeout as an idle
  log and loops; `_client_error`, the client's `error_cb`, logs
  `nats_client_error`; `stream_config_for` sets no first sequence. In
  `packages/hammertime-core/src/hammertime/core/runtime.py`, `run_service`
  logs `start_failed reason=startup_timeout` when `start()` outlasts the
  deadline, which is the record the runbook names.

## Amendment 2 (2026-09-23) — slice 2 designed: the `PrefixStatsChanged` publisher

Why: decision 14 fixed what slice 2 publishes (items 1-9) and left four
points to slice 2's own design (item 10). The dispatch for that design
asked for those four, and for eight more:

* how the publish keeps decision 9's R1, given that item 6 awaits the
  publisher inside `handle()`, under the lock, after step 5;
* what startup replay does under item 8: how much it publishes, what that
  costs in startup time, and whether readiness waits for it;
* failure handling under item 7, including how a failure during `start()`
  is reported;
* `stop()`'s flush (item 9) and decision 13's shutdown order;
* the new metrics, log records and keys, under decision 12's rules;
* the prefix-stats topic and its producer (ADR-0013): what exists and what
  is new;
* the schema, and the codec's bounds for `hot_ratio` in ADR-0016's style;
* the `CHANGES` lines, and whether any is `BREAKING`.

Items 1-9 are not re-opened: every ruling below builds on them. This ADR,
and the three others this amendment touches, are on master, so nothing
merged is rewritten. Each place whose text is now incomplete or no longer
true carries a dated note, and "Edits" below lists them.

### Ruling 1. The reporting range is per family; IPv6 reports `/104` to `/128`

| Family | Prefixes reported | Key | Default | Accepted values |
| --- | --- | --- | --- | --- |
| IPv4 | `[L4, 32]` | `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` | `8` | an integer from 0 to 32 |
| IPv6 | `[L6, 128]` | `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6` | `104` | an integer from 0 to 128 |

* **Why `/104`.** ADR-0010 decision 3 reports no IPv4 prefix shorter than
  `/8`, because above that capacity no plausible `minimum_hot_ips` and
  `minimum_hot_ratio` pair can be met: a `/7` needs 3.3 million hot IPs at
  10 %. An IPv4 `/8` holds 2^24 addresses, and the IPv6 prefix that holds
  2^24 addresses is `/104`. Both families therefore report prefixes of 2^24
  addresses down to one, and an IPv6 transition publishes 25 messages at
  the default, as an IPv4 one does. An IPv6 `/103` needs as many hot IPs as
  an IPv4 `/7`.
* **IPv6 gets a key of its own.** ADR-0010's assumption said that the
  setting "must become per-family when §35 is implemented", and slice 1
  implemented §35. `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` keeps the name
  ADR-0009 and ADR-0010 gave it, and is IPv4's.
* **How a value is read.** Each value is read with Python's `int()`, as the
  aggregator reads its integer keys, then checked against its range, both
  ends included. A value `int()` refuses, or one outside the range, is a
  `ValueError` naming the variable: `config_invalid`, exit 2 (decision
  11). Both keys are read and checked whatever `HAMMERTIME_TRIE_FAMILIES`
  holds. The value for a family the process does not serve is not used.
* **Settings.** `TrieSettings` gains two fields after
  `config_poll_interval_s`: `min_prefix_length: int = 8` and
  `min_prefix_length_ipv6: int = 104`.
* **Nothing is refused.** `HAMMERTIME_TRIE_FAMILIES` keeps accepting
  `ipv6`. The range exists now, so there is nothing to wait for.
* **What the range does not give IPv6.** The prefixes at which IPv6 traffic
  usually groups — `/64`, `/56`, `/48` — hold at least 2^64 addresses. The
  `hot_ratio` of any realistic count there is far below any
  `minimum_hot_ratio` that also makes sense for IPv4, so no such prefix can
  qualify under the v1 predicate. A lower floor would publish them, always
  `NORMAL`. A scorer that does not rest on the ratio (§14) would change
  that. This is recorded, not addressed (§43: IPv4 first).

### Ruling 2. `hammertime.trie.publisher`

```python
# hammertime.trie.publisher   Spec: §3, §14, §19, §22, §35; ADR-0010 decision 3;
#                             ADR-0017 decision 14 and Amendment 2
AGENT_ID: Final = "trie-primary"
DEFAULT_MIN_PREFIX_LENGTHS: Final[Mapping[AddressFamily, int]]   # read-only: {IPV4: 8, IPV6: 104}

@dataclass(frozen=True, slots=True)
class StatsMessage:
    key: str          # the prefix text: PREFIX_STATS.key_selector(payload)
    value: bytes      # hammertime.core.events.codec.encode(envelope)
    message_id: str   # envelope.event_id

@dataclass(frozen=True, slots=True)
class PreparedStats:
    family: AddressFamily
    sequence: int
    messages: tuple[StatsMessage, ...]   # one per ancestor_stats entry, in its order: shortest prefix first

class PrefixStatsPublishError(Exception):
    sequence: int
    attempted: int
    failed: int
    def __init__(self, *, sequence: int, attempted: int, failed: int) -> None: ...

class PrefixStatsPublisher:
    def __init__(self, producer: Producer, *, metrics: TrieMetrics,
                 min_prefix_lengths: Mapping[AddressFamily, int] = DEFAULT_MIN_PREFIX_LENGTHS) -> None: ...
    def min_prefix_length(self, family: AddressFamily) -> int: ...
    def prepare(self, trie: HotTrie, ip: Address, *, sequence: int, config_version: int,
                timestamp: datetime) -> PreparedStats: ...
    async def publish(self, prepared: PreparedStats) -> None: ...
    async def flush(self) -> None: ...
```

* **The constructor** keeps `producer` and `metrics`, and copies
  `min_prefix_lengths`. That mapping must hold an entry for each
  `AddressFamily`, an `int` from 0 to that family's bit length. A missing
  family, or a length out of range, is a `ValueError`.
* **`min_prefix_length(family)`** returns that family's floor.
* **`prepare(trie, ip, *, sequence, config_version, timestamp)`** is
  synchronous and calls no producer. With `L =
  min_prefix_length(trie.family)`, it makes one `StatsMessage` for each
  entry `s` of `ancestor_stats(trie, ip, min_length=L)` (ADR-0015 decision
  2), zero counts included, in that order:
  * the payload is `PrefixStatsChanged(prefix=str(s.prefix),
    hot_count=s.hot_count, capacity=s.capacity, sequence=sequence,
    timestamp=timestamp, hot_ratio=s.hot_ratio)`;
  * the envelope is `EventEnvelope(agent_id=AGENT_ID, sequence=sequence,
    event_type="PrefixStatsChanged", config_version=config_version,
    timestamp=timestamp, subject=str(s.prefix), payload=<the payload>)`
    (decision 14 item 3);
  * `key` is the prefix text, `value` the encoded envelope, and
    `message_id` the envelope's `event_id` (item 4).

  The same arguments over the same trie give the same messages. A
  `ValueError` from `ancestor_stats` (an address of the other family) and
  a `CodecError` from `encode` propagate. From the worker, neither can
  happen (ruling 5).
* **`publish(prepared)`** starts `producer.publish(PREFIX_STATS.name, key,
  value, message_id=message_id)` for every message before it awaits any of
  them. It returns only once every one of them has returned or raised, so
  no publish outlives it. For each publish that returned, it counts
  `prefix_stats_published` once, with `family=prepared.family`.
  * If any publish raised an `Exception`, `publish()` then raises
    `PrefixStatsPublishError(sequence=prepared.sequence, attempted=<the
    number of messages>, failed=<the number that raised>)`. It raises it
    from the exception of the first failed message in `messages` order,
    which is therefore its `__cause__`. The error's text carries those
    three numbers and nothing else of the event: no prefix, and not the
    cause's text.
  * A publish that raises a `BaseException` that is not an `Exception`,
    such as a cancellation, makes `publish()` raise that same exception.
  * The order in which one event's messages reach the log is not pinned.
* **`flush()`** awaits `producer.flush()`.
* **Imports.** `publisher` imports `metrics`, `hammertime.trie.structure`,
  `hammertime.trie.metadata`, `hammertime-bus` and `hammertime-core`.
  `worker` imports `publisher`. `state` and `query.app` import none of it
  (decision 2).

> Amended 2026-09-24 (Amendment 3 ruling 3): `publish()`'s "returns only
> once every one of them has returned or raised" and "For each publish that
> returned, it counts `prefix_stats_published` once" hold for every way it
> ends, a cancellation included. When it is cancelled, it cancels the
> publishes still in flight, waits until each has finished, and raises
> `CancelledError`; every publish that returned is counted.

### Ruling 3. The worker publishes after step 5, and R1 holds

* **The constructor.** `TrieWorker(*, bus, state, metrics,
  min_prefix_lengths: Mapping[AddressFamily, int] =
  DEFAULT_MIN_PREFIX_LENGTHS)`. It takes its producer from `bus.producer()`,
  once, as it takes its consumer from `bus`, and builds its
  `PrefixStatsPublisher` from that producer, `metrics` and
  `min_prefix_lengths`. Every slice-1 construction stays valid and gets the
  defaults.
* **`handle()`**, with decision 6's steps as they now run. Steps 1 to 5 are
  unchanged. When step 4 returned `changed`, two steps follow step 5, and
  then step 6:
  * **5a. Prepare.** `prepared = publisher.prepare(fs.trie, payload.ip,
    sequence=state.event_sequence,
    config_version=state.config.config_version,
    timestamp=payload.timestamp)`. It runs in the synchronous section that
    step 4 opened: no `await` separates `note_applied` from it.
    `state.event_sequence` is `message.offset + 1` at that point.
  * **5b. Publish.** `await publisher.publish(prepared)`, still holding the
    worker's lock. On `PrefixStatsPublishError` the worker logs
    `prefix_stats_publish_failed` (ruling 7) and re-raises the error.
  * **6. Outcome.** `APPLIED`, once the publish has returned. An
    `UNCHANGED` event, and every skipped outcome, prepares and publishes
    nothing (item 1).
* **R1.** R1 is about writes, and its section is unchanged. It opens with
  the first write of `apply_*`, and its last write is still
  `note_applied`. It now runs on through `prepare()`, still without an
  `await`, and `prepare()` only reads. The publish is awaited after the
  section, and writes nothing to `TrieState`.
* **`prepare()` is an R2 reader.** It takes the event's ancestor counts and
  `config_version` in one synchronous section, and encodes every message
  before the first `await`. The messages therefore describe the state
  between this event and the next, and nothing written later changes them.
* **While the publish is awaited,** the worker holds its lock, so no other
  writer runs: not the next `handle()`, not `apply_config()`, not
  `start()`'s step 3, and not `stop()` beyond its flag (ruling 6). Readers
  can run: `/metrics`, and slice 3's read API. They see the event whole,
  with `event_sequence` past it, while its stats are still on their way to
  the log. That is the lag §22 allows between the trie and the detector,
  and every response's `event_sequence` shows it.
* **Why two calls.** One coroutine that read the trie and then published
  would also read before its first `await`, but only while nobody adds an
  `await` above the read. Two calls make the synchronous read part of the
  interface, and a test can check it: a write to the trie after `prepare()`
  returns does not change what `publish()` sends.

### Ruling 4. Startup replay publishes, and readiness waits for it

* **What happens.** `start()` hands every replayed message to `handle()`,
  so the replay publishes exactly as live consumption does (item 8), one
  event at a time (item 6). `start()` returns once `handle()` has returned
  for the message that brought the worker to `replay_target`, publish
  included. `TrieService` marks itself ready only after `start()` returns
  (decision 4). A ready trie has therefore had every stat of every
  state-changing record it replayed acknowledged by the log.
  `caught_up` is about the position: it becomes true at that last
  message's `note_applied`, before its publish returns, and only the
  service reads it, after `start()` has returned.
* **How much.** A record whose outcome is `APPLIED` publishes 25 messages
  at the defaults; any other outcome publishes nothing. A replay therefore
  publishes 25 times as many messages as the log retains state-changing
  records, and `replay_complete` reports the number (ruling 7). The log
  drops those whose `event_id` it has seen inside its 120 s duplicate
  window (ADR-0013 decision 4), and appends the older ones again. The
  detector's sequence rule ignores them: each carries the `sequence` the
  detector already holds for its prefix, which is not greater (ADR-0013
  decision 9).
* **What a republished message says.** It has the original's `event_id`,
  `sequence`, `timestamp` and payload whenever the replay reads what the
  original run read. Two things can differ. Its `config_version` is the
  configuration in force at the restart. And when the hot-ip log's head
  has aged out, the replay lacks the IPs that became HOT before the oldest
  retained record, so its counts can be lower: the loss Consequences
  already records for a restarted trie. The sequence rule keeps either
  from replacing what the detector holds.
* **How long.** Each state-changing record now costs a broker round trip
  and 25 acknowledged publishes. Before slice 2 it cost no round trip.
  What follows is an estimate, not a measurement (assumption 55).
  * ADR-0013 prerequisite 4 measured about 17,800 synchronous nats-py
    publishes a second, with 500 in flight, on one node, over loopback, on
    a 4 vCPU host.
  * At 25 publishes a record, that is at most about 700 records a second,
    and the replay keeps only 25 publishes in flight.
  * So the default 60 s deadline holds at most about 40,000
    state-changing records, and fewer once decoding, applying and encoding
    take their share.

  A longer log fails the start with `start_failed
  reason=startup_timeout`, exit 1, until `HAMMERTIME_STARTUP_TIMEOUT_S` is
  raised or the snapshot epic bounds the replay to what follows the
  snapshot. The integration job (#52) is where the rate can be measured.
* **Why readiness waits.** Items 6 and 8 put the publish inside `handle()`,
  and `start()` hands `handle()` every message, so waiting takes no
  mechanism of its own. Two alternatives were rejected:
  * publishing each touched prefix's final stats once, at the end of the
    replay (assumption 44);
  * becoming ready after the replay's applies, and re-publishing in the
    background (assumption 45).

> Amended 2026-09-24 (Amendment 3 ruling 1): "the replay publishes exactly
> as live consumption does" now holds from `republish_from` on. Below it
> the replay applies and does not publish. "How much" and "How long"
> describe a start whose `hammertime.prefix-stats.v1` has no usable last
> message; in the normal case a start re-publishes one event's stats. This
> ruling costed one start and said a longer log "fails the start". A start
> that fails has already written what it published, and under this ruling
> as first written every restart wrote it again (finding S1). The next
> start now continues after it.

### Ruling 5. What each failure does (item 7)

| Raised by | Exception | Response |
| --- | --- | --- |
| `publisher.publish` | `PrefixStatsPublishError` | The worker logs `prefix_stats_publish_failed` and re-raises it: out of `handle()`, then out of `start()` (`start_failed`, exit 1, never ready) or `run()` (`run_exited`, exit 1). Unlike an `InvariantViolation`, the event is applied and `position` has moved past it; some of its stats may be in the log. The restart replays it and publishes the same envelopes under the same `event_id`s (item 7). |
| `publisher.prepare` | `ValueError` or `CodecError` | Cannot happen: the worker passes the trie of the event's own family, and every value is in range by construction (`hot_count >= 0`, `capacity >= 1` and `hot_count <= capacity`, so `hot_ratio` lies in `[0, 1]`). If it does, it is a bug, and it propagates like decision 7's other bug rows, with no record of the worker's own. Nothing of the event was published, and the state has moved past it. |
| `publisher.flush`, in `stop()` | any exception | Propagates out of `stop()`, and the process exits 1: `TrieService.run()` logs `stop_failed` when its own call raises, and the runner logs `run_failed` when the call it makes on a signal raises. Both shipped producers' `flush()` return at once. |

* **How a failure during `start()` is reported.** In this order:
  * the worker's `prefix_stats_publish_failed` (error). It carries the
    hot-ip record's coordinates, the event's `sequence`, how many
    publishes were attempted and how many failed, and the first failure's
    exception class;
  * the runner's `start_failed`. Its `error` field is the
    `PrefixStatsPublishError`'s text, numbers only, and its `exception`
    field carries the traceback with the cause, such as nats-py's
    `TimeoutError`.

  `/readyz` never answers 200. Under `run()` it is the same, with
  `run_exited` in place of `start_failed`.
* **No retry in the process.** nats-py already waits up to 5 s for each
  acknowledgement: its JetStream context's default request timeout, which
  `NatsBus` does not override (Sources). A retry would hold the lock, and
  with it the writer, longer still, and it would repeat what the restart
  does anyway. The restart is the retry: assumption 12's reasoning, applied
  to publishing.
* **No bare catch.** `publish()` collects each publish's exception and
  raises one error from the first. It drops none, so it is not a
  catch-all: every failure still ends the process. The worker catches only
  `PrefixStatsPublishError`, by name.

> Amended 2026-09-24 (Amendment 3 ruling 2): the `publisher.prepare` row's
> "cannot happen" did not hold for a hot-ip timestamp whose offset puts it
> outside the years 1 to 9999 in UTC: the codec decoded it, and `encode`
> raised `OverflowError` on it (finding S2). Decode now refuses such a
> value as a `CodecError`, the record is `MALFORMED`, and the row holds.

### Ruling 6. `stop()` flushes; the shutdown order (item 9, decision 13)

* **`TrieWorker.stop()`**:
  1. sets the stop flag;
  2. takes the lock, so that the message in hand is finished — every
     publish of its stats included, because `handle()` holds the lock
     across them;
  3. awaits `publisher.flush()`, and marks the worker stopped whether or
     not the flush raised.

  Every call takes all three steps, a second call included. `stop()` stays
  safe before `start()`: neither shipped producer's `flush()` touches a
  connection.
* **The shutdown order,** end to end for the trie. §47.4 reads "flush,
  then snapshot" for the trie (ADR-0009 Amendment 4):
  1. `TrieService.stop()` marks readiness `stopping`, stops the poller and
     sets the server's `should_exit`;
  2. `worker.stop()` sets the flag, takes the lock once the message in
     hand and its publishes are done, and flushes;
  3. the snapshot epic writes the final snapshot, after the flush
     (decision 16);
  4. `run()`'s teardown gathers its tasks and closes the transport it
     built, the bus, last.
* **The deadline.** `HAMMERTIME_SHUTDOWN_TIMEOUT_S` (8 s by default) must
  cover the publishes in hand, each bounded by the 5 s acknowledgement
  wait, then the flush and the transport's close. A drain during a broker
  outage can therefore end in a publish timeout. That event's stats are
  then not all in the log, `run()` raises, and the process exits 1 with
  `run_exited`. The restart re-publishes them (item 7).

> Amended 2026-09-24 (Amendment 3 ruling 4): step 3's "and marks the worker
> stopped whether or not the flush raised" is dropped. Nothing reads such a
> mark. `stop()` sets the flag, takes the lock and awaits the flush, whose
> exception propagates; every call takes all three steps.

### Ruling 7. Metrics, log records and keys

* **One new counter,** under decision 12's rules:

  | Series | Kind | Labels | Meaning |
  | --- | --- | --- | --- |
  | `prefix_stats_published` | counter | `family` | `PrefixStatsChanged` publishes that returned: acknowledged by the log, including one it dropped as a duplicate |

  There is no failure counter: the first failure ends the process, so the
  record is the signal.
* **Log records,** on the logger `hammertime.trie.worker`, fixed tokens and
  numbers only:

  | Record | Level | Fields |
  | --- | --- | --- |
  | `prefix_stats_publish_failed` | error | `family`, `topic`, `partition`, `offset` (the hot-ip record's), `sequence`, `attempted`, `failed`, `error_type` (the first failure's exception class, as `<module>.<qualified name>`) |
  | `replay_complete` | info | as before, plus `prefix_stats_published`: the publishes that returned during this `start()` |

  The worker does not log an exception's text. That reaches the log only
  through the runner's `start_failed` or `run_exited`, as
  `InvariantViolation`'s does (decision 12).
* **The `starting` record.** `startup_fields()` gains `min_prefix_lengths`,
  each served family's name mapped to its floor, for example
  `{"ipv4": 8}`.
* **Keys.** Ruling 1's two. `.env.example` lists both in its trie block.
* **`hot_transition_to_prefix_update_latency` is not measured by slice 2.**
  Three reasons:
  * *The start point is too coarse.* The only transition time the trie
    receives is the hot-ip payload's `timestamp`. The aggregator takes it
    from `Clock.now()`, which returns whole epoch seconds, on another
    host's clock (Sources). A latency of milliseconds measured from it
    would be up to a second of truncation, plus clock skew.
  * *The replay would swamp it.* A replayed event's timestamp can be days
    old.
  * *There is no histogram.* No service's metrics class has one, and the
    aggregator does not measure §37's other latency either. One facility
    for both is the telemetry epic's.

  An accurate measurement needs a sub-second transition time from the
  aggregator, or a log-side time carried on `ConsumedMessage`, which
  carries none today. Both are named for the telemetry epic. Until then,
  the trie's backlog in records — the hot-ip log's `end_offset` minus the
  `event_sequence` series — is exact, and the telemetry epic can expose it.

> Amended 2026-09-24 (Amendment 3 ruling 1): `replay_complete` also gains
> `republish_from`, and a warning record joins the table,
> `prefix_stats_last_ignored`, with the fields `reason` and
> `replay_target`.

### Ruling 8. The prefix-stats topic and its producer (ADR-0013)

* **What exists, used as it is.**
  * `PREFIX_STATS` in `hammertime.bus.topics`: `hammertime.prefix-stats.v1`,
    4 partitions, one day's retention, keyed by the prefix text
    (`_prefix_key`).
  * Its stream, `hammertime-prefix-stats-v1`, with subjects
    `hammertime.prefix-stats.v1.*`. `hammertime-provision` creates it with
    every registered topic: limits retention, `max_age` one day, no byte
    cap, discard old, file storage, one replica in the reference
    deployment, a 120 s duplicate window (ADR-0013 decision 2).
    `NatsBus.start()` already checks that it exists, so the trie already
    refuses to start without it.
  * `bus.producer()`, on `NatsBus` its one `NatsProducer`. It publishes to
    `hammertime.prefix-stats.v1.<partition_for(prefix text, 4)>`, sends
    `Nats-Msg-Id`, `Hammertime-Key` and the expected stream, awaits the
    `PubAck`, and returns normally for a duplicate. Its `flush()` returns
    at once. On `InMemoryBus` the producer appends to partition 0 and
    deduplicates by `message_id` for the bus's life.
* **What is new.** Nothing in `hammertime-bus`, in provisioning, in the
  stream configuration or in the compose file. The trie becomes the
  topic's only producer. The detector's durable, `hammertime-detector`,
  stays the detector epic's.
* **Order.** One prefix is one key and one subject, so a prefix's stats
  reach the log in the order of their events: every publish of one event
  is acknowledged before the next event is handled (item 6).
* ADR-0013 Amendment 12 records the trie as decision 4's fourth producer.

### Ruling 9. `hot_ratio`: the model, the codec and the schema (item 5)

* **The model.** `PrefixStatsChanged` gains `hot_ratio: float | None =
  None` as its last field, so every existing construction stays valid.
* **Encode.** `None` writes no key. Otherwise the value must be an `int` or
  a `float` and not a `bool`; a `float` must be finite; the value must be
  `>= 0` and `<= 1`. The wire carries `float(value)`. Anything else is a
  `CodecError`.
* **Decode.** An absent key is `None`. A present one must be a JSON number:
  an `int` that is not a `bool`, or a `float`. It must be finite —
  `json.loads` reads `NaN`, `Infinity` and `-Infinity` — and `>= 0` and
  `<= 1`. It is stored as `float(value)`. Anything else, `null` included,
  is a `CodecError`.
* **Order and messages,** in ADR-0016's style. The type is checked first,
  then finiteness, then the range. So `true` is a type error, and a
  400-digit integer is refused by the range before anything converts it to
  a float. The check's own text names the field and the rule it broke,
  never the value. On decode the check runs where the payload's integer
  checks run, so a refusal is a `CodecError` whose `__cause__` is the
  check's own error. The wrapper's text embeds the payload, which is
  ADR-0015 Amendment 3's open item, unchanged. On encode the `CodecError`'s
  own text names no value. The wording is not part of the contract.
* **The bounds** are the schema's: `minimum` 0 and `maximum` 1, both
  inclusive (JSON Schema Validation 2020-12 §6.2.2 and §6.2.4, as ADR-0016
  read them). The codec mirrors them as a module constant, and the tests
  read them from the schema file (ADR-0016 assumption 10).
* **Not checked:** that `hot_ratio` equals `hot_count / capacity`. That is
  cross-field consistency, which ADR-0016 assumption 11 leaves open.
  `hot_ratio` is informational: the predicate compares `hot_count` and
  `capacity` exactly (ADR-0010 decision 1).
* **Not changed:** the eight integer fields of ADR-0016 decision 1 and
  ADR-0015 Amendment 3 ruling 3. `hot_ratio` is a number field, not one of
  them. ADR-0016 Amendment 1 records this.
* **The values the trie writes** come from `Prefix.hot_ratio`, exact and
  narrowed once (ADR-0015 decision 2). Whatever the floor, the smallest
  non-zero one, 2^-128 at an IPv6 `/0`, is a normal double.
* **The schema.** `schemas/prefix_stats_event.v1.json` validates exactly
  what it did: `hot_ratio` stays optional, and no type, bound or
  `required` entry changes. It gains `description` annotations on
  `prefix`, `hot_ratio`, `sequence` and `timestamp`, saying what the trie
  puts there.

> Amended 2026-09-24 (Amendment 3 ruling 5): "a refusal is a `CodecError`
> whose `__cause__` is the check's own error" is made testable. The check's
> own error is a `ValueError`, whose text names `hot_ratio`. For every
> value decode refuses, the cause is therefore a `ValueError`, never a
> `TypeError` or an `OverflowError`. The wording stays outside the
> contract.

### Ruling 10. What the snapshot epic inherits (decision 16)

* **A restore re-publishes** the stats of every state-changing record after
  the snapshot's position (item 8). Since every snapshot follows a flush,
  those at or before the position are already in the log.
* **No snapshot after `handle()` has raised.** A `PrefixStatsPublishError`
  leaves the state past an event whose stats may be missing from the log,
  and an `InvariantViolation` leaves a trie found corrupt. Neither
  `stop()`'s final snapshot nor a periodic one may record such a state.
  The epic designs how, for example by refusing `snapshot_now()` once the
  worker has seen `handle()` raise.
* **The startup cost** of ruling 4 then covers only the records appended
  after the snapshot that a restart loads.

> Amended 2026-09-24 (Amendment 3 ruling 1): a restore re-publishes under
> the rule every start follows, from `republish_from`. Only when
> `hammertime.prefix-stats.v1` has no usable last message does it
> re-publish every state-changing record after the snapshot's position,
> and only then does ruling 4's cost apply to those records.

### Ruling 11. `CHANGES`

The implementing change adds three lines, in this order, at the top. None
is `BREAKING`.

* `Trie service publishes PrefixStatsChanged to hammertime.prefix-stats.v1 for every HotIpAdded/HotIpRemoved that changes the hot set: one message per ancestor prefix of the address, from the family's minimum reporting length to the host route (25 per transition at the defaults), keyed by the prefix and carrying it as the envelope subject, with hot_count, capacity, hot_ratio and the trie's event_sequence as sequence; a redundant or skipped event publishes nothing`
* `Add HAMMERTIME_TRIE_MIN_PREFIX_LENGTH (default 8, 0-32) and HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6 (default 104, 0-128): the shortest IPv4 and IPv6 prefixes the trie service reports in PrefixStatsChanged`
* `Trie service re-publishes the PrefixStatsChanged of every state-changing hot-ip record it replays on start before it reports ready, so a start takes longer the more such records the log retains; a publish that fails stops the service with exit 1 (prefix_stats_publish_failed)`

Why none is `BREAKING`, under `CLAUDE.md`'s rule (a running deployment
needs action to keep working):

* No trie build has shipped (Consequences; assumption 32's reasoning).
* The event schema validates exactly what it did. `hot_ratio` was already
  optional there, with the same bounds (ADR-0010 Consequences), and a
  build from before this change ignores it on decode. Nothing consumes
  `hammertime.prefix-stats.v1` yet, and nothing else produces to it.
* No key is renamed or removed, and `ipv6` stays accepted.

What gets no line (assumption 58): the codec's `hot_ratio` checks, which no
running consumer meets (ADR-0016 assumption 12's reasoning); the counter,
since `/metrics` renders nothing until the telemetry epic; the new fields
of the `starting` and `replay_complete` records; and the schema's
descriptions.

> Amended 2026-09-24 (Amendment 3 ruling 7): the third line is replaced;
> ruling 7 there gives the new text.

### Test seams

* **The producer.** The worker takes its producer from `bus.producer()`
  once, in its constructor. A bus double's `producer()` can therefore
  return a producer that records every call, holds each publish until the
  test releases it, raises for a chosen key, or counts `flush()` calls.
  Whether `publish()` passes `key` and `value` by position or by keyword
  is not pinned, so a double implements the `Producer` protocol's
  signature.
* **The publisher on its own.** `PrefixStatsPublisher` can be built with a
  producer double and a `TrieMetrics`. `prepare()` needs only a trie: a
  `PatriciaTrie(family)` with addresses added through `add_hot_ip`.
* **Reading what was published, on the memory bus.** A second consumer on
  the same `InMemoryBus` subscribes to `PREFIX_STATS.name` positionally
  from `0`. The memory producer deduplicates by `message_id` for the
  bus's life, so a second trie that replays the same log on the same bus
  appends nothing.
* **The record.** `prefix_stats_publish_failed` is on
  `hammertime.trie.worker`, like decision 12's other records.

> Amended 2026-09-24 (Amendment 3 ruling 1): a second trie started on the
> same bus after a first one has published now also re-publishes only from
> the first one's last `sequence`, besides appending nothing.

### Questions this amendment closes

| Question | Left open by | Ruled in |
| --- | --- | --- |
| Which prefix lengths does the trie report for IPv6? | ADR-0010's assumption "Minimum reported prefix length 8 (IPv4), a trie setting"; decision 14 item 10 | ruling 1: `/104` to `/128` by default |
| What is `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH`, and does IPv6 get a key of its own? | ADR-0009 Consequences; ADR-0010 decision 3; decisions 11 and 14 item 10 | ruling 1 |
| Does the service refuse `ipv6` until that range exists? | decision 14 item 10 | ruling 1: no, since the range exists |
| Does the publisher measure `hot_transition_to_prefix_update_latency`? | decision 12; decision 14 item 10 | ruling 7: no |
| How does the publish keep R1? | decision 14 item 6, with decision 9 | ruling 3 |
| What does startup replay publish, what does it cost, and does readiness wait for it? | decision 14 item 8; Consequences | ruling 4 |
| How is a publish failure handled and reported, during `start()` too? | decision 14 item 7 | ruling 5 |
| What does `stop()` do, and in what order does the trie shut down? | decision 13; decision 14 item 9 | ruling 6 |
| May a snapshot record the state a failed `handle()` left? | new with slice 2 | ruling 10: no |

### Edits

**In this ADR.** Each is a dated note; no text is replaced.

* **Status line.** Closing sentences on this amendment.
* **Decision 1.** A note after its 2026-09-23 note: the IPv6 key, the
  `.env.example` and `CHANGES` edits, and the snapshot epic's added
  ordering constraint.
* **Decision 2.** A note after "Slice 1 does not touch `publisher.py`,
  `query/views.py` or `snapshot/`.": the publisher's names and imports.
* **Decision 6.** A note after its 2026-09-23 note: the constructor's
  keyword, and steps 5a and 5b.
* **Decision 7.** A note after its 2026-09-23 note: three rows.
* **Decision 9.** A note at its end: R1 and R2 under slice 2.
* **Decision 11.** A note after the "Not read by slice 1" list: the two
  keys and fields.
* **Decision 12.** A note after the log-record table: the counter, the
  record, `replay_complete`'s field, and the latency answered no.
* **Decision 13.** A note after its last bullet: `stop()`'s flush,
  `build_service` and `startup_fields()`.
* **Decision 14.** A note after item 10: item 10 settled.
* **Decision 16.** A note after its last bullet: two more inheritances.
* **Test seams.** A note after the last bullet, pointing here.
* **Consequences.** A note under "Slice 2 without the snapshot epic
  republishes.": the cost.
* **This section.**

**In other documents.** None replaces wording, except where quoted.

* **`docs/adr/0010-read-apis-and-shared-prefix-predicate.md`** (Amendment
  3): a clause on the status line; a dated note after decision 3's
  2026-09-23 note; a dated note under the assumption "Minimum reported
  prefix length 8 (IPv4), a trie setting."; an "Amendment 3" section.
* **`docs/adr/0013-nats-jetstream-event-log-static-shards.md`** (Amendment
  12): a clause on the status line; a dated italic paragraph after
  decision 4's first paragraph; an "Amendment 12" section.
* **`docs/adr/0016-codec-numeric-bounds-and-encode-before-persist.md`**
  (Amendment 1): a status paragraph after "No schema file changes."; a
  dated note under decision 1's "Not changed" bullet on properties the
  codec does not read; an "Amendment 1" section.
* **`schemas/prefix_stats_event.v1.json`.** `prefix`, `hot_ratio`,
  `sequence` and `timestamp` gain a `description` each. Each of the four
  was written on one line and is now spread over several. They were
  `"prefix": { "type": "string" }`, `"hot_ratio": { "type": "number",
  "minimum": 0, "maximum": 1 }`, `"sequence": { "type": "integer",
  "minimum": 0 }` and `"timestamp": { "type": "string", "format":
  "date-time" }`. Every type, bound and `required` entry is unchanged.
* **`docs/spec/README.md`.** Four rows gain entries; no entry is removed.
  * §19 gains `services/trie/publisher.py`, ADR-0016's Amendment 1, and
    ADR-0017's decision 14 and Amendment 2. Its ADR-0017 entry was
    "`docs/adr/0017` (decisions 3 and 6)".
  * §32/33's ADR-0017 entry was "`docs/adr/0017` (decisions 3, 4, 8 and
    16)", and gains Amendment 2.
  * §35 gains the two keys, `services/trie/publisher.py`, ADR-0010
    Amendment 3 and ADR-0017 Amendment 2. Its entries were
    "`services/trie/config.py` (`HAMMERTIME_TRIE_FAMILIES`)" and
    "`docs/adr/0017` (decision 5)".
  * §37's ADR-0017 entry was "`docs/adr/0017` (decision 12)", and gains
    Amendment 2.

**Not edited here, named** (assumption 62). `docs/runbook.md`'s "Trie
service restart" should say, once slice 2's implementation has merged,
that the replay re-publishes and is slower for it.
`docs/spec/integration-scenarios.md` §3 step 3 already pins slice 2's 25
records; checks on `hot_ratio`, `agent_id` and `subject` may be added.

### Assumptions

Each is a judgment call that decision 14, the spec and the earlier ADRs do
not make. Push back on them individually; numbering continues the ADR's
list.

41. **IPv6's default floor is `/104`, from ADR-0010's capacity argument.**
    `/64`, the usual IPv6 subnet, was the alternative. It would publish 65
    messages per transition, and every prefix from `/64` to about `/103`
    would always be `NORMAL` under the v1 predicate at the default ratio:
    ADR-0010's own reason for not reporting IPv4 above `/8`. `0` would
    publish 129. The plausibility threshold behind `/8`, and so behind
    `/104`, is itself ADR-0010's assumption. An operator who wants `/64`
    sets the key.
42. **Two keys, not one per-family grammar.** One key such as
    `ipv4=8,ipv6=104` would need a grammar of its own, and would change a
    key whose name and IPv4 default ADR-0009 and ADR-0010 already fixed.
    The unsuffixed key stays IPv4's, §43's first family.
43. **Both keys are read and checked whatever `HAMMERTIME_TRIE_FAMILIES`
    holds.** The alternative follows ADR-0009 A12 and ADR-0013 Amendment 5
    ruling 5: ignore a value the configuration does not use. That rule was
    made for connection URLs, whose meaning depends on another key. A
    prefix length's validity does not, and refusing a bad value at once is
    simpler. An operator who set the IPv6 key on an IPv4-only trie loses
    nothing.
44. **The replay publishes per event, not once per prefix.** Coalescing
    publishes each touched prefix's final stats once, at the end of the
    replay, under the sequence of the last event that changed it. The
    detector would end in the same state with fewer messages. It was
    rejected for four reasons:
    * it saves only a constant factor, the average number of changes per
      prefix in the retained log, so it does not close the deadline
      problem;
    * it must hold the set of touched prefixes in memory, up to 25 per
      distinct address;
    * it is a second publish path, with failure rules of its own;
    * a detector reading the stream would lose the intermediate edges its
      `since` and `classification_changes` are built from.
45. **Readiness waits for the replay's publishes.** The alternative marks
    the trie ready after the replay's applies and re-publishes in the
    background. That would make the trie ready sooner, but it cannot
    rebuild each event's stats after the replay has moved on. It would
    publish the final state per prefix under a new sequence, and so could
    not re-publish the zero counts of prefixes the trie no longer holds.
    The snapshot epic would also have to track a republish position beside
    the replay position. It would re-open items 6 and 8 as well.
46. **`prepare()` and `publish()` are two calls** (ruling 3, "Why two
    calls").
47. **All of an event's publishes start together, and every one is awaited
    even after one fails.** Item 6 allowed concurrency; this makes it the
    rule, so that an event costs about one broker round trip, not one per
    message. Waiting for all of them means no publish outlives `handle()`,
    so the lock covers every publish in flight, and neither `stop()`'s
    flush nor a snapshot overlaps one. The cost: after a failure the worker
    waits for the slowest of the others, at most nats-py's 5 s.
48. **One named error type, with the counts, raised from the first
    failure.** The worker needs the counts for its record, and catching
    one named type keeps decision 7's "no bare `Exception`". Re-raising the
    first failure as it is would make the worker catch transport-specific
    types it does not otherwise know. An `ExceptionGroup`, which asyncio's
    `TaskGroup` raises, was the other alternative; it would put a group,
    not a named error, into `start_failed`.
49. **`error_type` names the exception class.** Decision 12 allows fixed
    tokens only, and a class name is one. It tells an operator which
    failure it was — a timeout, a missing stream, a refused publish —
    without the exception's text.
50. **No retry in the process** (ruling 5).
51. **`stop()` flushes on every call.** The service's `run()` and the
    runner both call `stop()`, so a normal drain flushes twice. Both
    producers' `flush()` return at once. Flushing only on the first call
    would save nothing and add a state to test.
52. **`prefix_stats_published` counts publishes that returned, duplicates
    included, and there is no failure counter.** Neither producer tells its
    caller whether a returned publish was appended or dropped: `NatsProducer`
    discards the `PubAck`, and `MemoryProducer` returns `None` either way.
    So the counter measures publishing work, not log growth.
53. **`hot_transition_to_prefix_update_latency` is left to the telemetry
    epic** (ruling 7). Decision 12 said slice 2 "can" measure it; this
    amendment decides it should not. The single-writer signal ADR-0001
    and ADR-0010 name is then unavailable until that epic.
54. **Two log fields are added.** `replay_complete`'s
    `prefix_stats_published` shows what a restart cost in publishes before
    `/metrics` renders anything. `min_prefix_lengths` in the `starting`
    record shows the ranges in force.
55. **The startup estimate is derived, not measured.** It rests on ADR-0013
    prerequisite 4's measurement of a different workload: one node,
    loopback, a 4 vCPU host, 500 publishes in flight. It is an upper bound
    on the replay's publish rate, not a prediction. The integration job
    (#52) is where to measure it.
56. **The codec accepts an `int` for `hot_ratio` and writes a `float`.**
    JSON Schema's `number` includes integers, so `0` and `1` on the wire
    are valid. Writing `float(value)` gives what the trie writes one wire
    form; `Prefix.hot_ratio` returns a `float` anyway.
57. **The schema gains descriptions, and no validation change.** A reader
    of the schema learns what the trie puts in `sequence` and `hot_ratio`
    without the ADRs. Making `hot_ratio` required was not chosen. The trie
    always sets it, and requiring it would change what the codec and the
    schema accept for no consumer's benefit. ADR-0010's Consequences kept
    it optional, and item 5 says "reads it when present".
58. **`CHANGES` gets three lines, none `BREAKING`, and nothing for the
    codec, the schema, the counter or the log fields** (ruling 11).
59. **The lengths mapping must name both families.** A publisher that
    lacked one would fail only at that family's first event. The settings
    always give both.
60. **The IPv6 limitation is recorded, not addressed** (ruling 1). §43
    puts IPv4 first, and §14's scorer is where a predicate without the
    ratio belongs.
61. **The worker takes its producer once from the bus, and exposes no
    publisher.** Tests reach the producer through the bus, as they reach
    the consumer.
62. **The runbook and the integration scenarios are not edited here.** The
    runbook describes the build an operator runs, and slice 2's has not
    merged. The scenarios' existing text stays true.

    > Amended 2026-09-24 (Amendment 3, assumption 86): the runbook is
    > edited now. Slice 2's implementation is on this branch and merges
    > with the edit. The scenarios are still not edited.
63. **A snapshot after a failed `handle()` is forbidden now, though no
    snapshot exists yet** (ruling 10). Slice 2 creates the state that must
    not be recorded, so the constraint is stated with it. How it is kept is
    the snapshot epic's.
64. **A failure in `prepare()` gets no record of the worker's own.** ADR-0016
    decision 4 treats an encode failure in the aggregator's emitter the
    same way: only a bug can cause it, and the runner's record carries it.

### Sources

Read on 2026-09-23 for this amendment. No web source was consulted.

* Repository files:
  * `packages/hammertime-bus/src/hammertime/bus/nats.py`. `NatsBus.start()`
    takes its JetStream context as `nc.jetstream()`, with no timeout, and
    calls `stream_info` for every registered topic. `NatsBus.producer()`
    returns one cached `NatsProducer`. `NatsProducer.publish` calls
    `js.publish(subject, value, headers=headers, stream=...)` with no
    timeout, and discards the `PubAck`. `NatsProducer.flush` returns
    `None`.
  * `packages/hammertime-bus/src/hammertime/bus/memory.py`:
    `MemoryProducer.publish` returns normally, without appending, for a
    `message_id` the topic has seen; `flush` returns `None`.
  * `packages/hammertime-bus/src/hammertime/bus/topics.py`: `PREFIX_STATS`
    (4 partitions, one day, `_prefix_key`).
  * `packages/hammertime-core/src/hammertime/core/time/clock.py`:
    `Clock.now() -> int`, "Current time as a UTC epoch second". And
    `services/aggregator/src/hammertime/aggregator/transitions.py`:
    `timestamp = datetime.fromtimestamp(self._clock.now(), tz=UTC)`.
  * `packages/hammertime-core/src/hammertime/core/runtime.py`:
    `_supervise` logs `start_failed` with `error=str(exc)` and
    `exc_info=True`, and `run_exited` with the exception.
  * `packages/hammertime-core/src/hammertime/core/events/models.py` and
    `codec.py`: `PrefixStatsChanged` has no `hot_ratio`, and
    `_decode_prefix_stats_changed` reads the five required keys inside a
    `try` that re-raises a `CodecError` from the error.
  * `services/trie/src/hammertime/trie/`: `worker.py`, `service.py`,
    `config.py`, `metrics.py`, `state.py`, `metadata/combine.py`
    (`ancestor_stats`, `PrefixStats.hot_ratio`) and
    `structure/patricia.py` (the order of `ancestor_counts`). Its tests
    build `TrieWorker(bus=..., state=..., metrics=...)` and
    `TrieSettings(...)` without the new arguments, and `test_service.py`'s
    `TestStartupFields` pins `startup_fields()`'s exact key set.
  * `services/aggregator/src/hammertime/aggregator/config.py`
    (`_parse_positive_int` reads an integer key with `int(value)`) and
    `metrics.py` (no histogram).
* nats-py 2.16.0 as installed, in `.venv/lib/python3.12/site-packages/nats/`:
  * `js/client.py`: `JetStreamContext.__init__(self, conn, prefix=...,
    domain=None, timeout: float = 5, ...)`; `publish(...)` sets `timeout =
    self._timeout` when none is given, then calls `self._nc.request(subject,
    payload, timeout=timeout, headers=hdr)`. Taken from it: the 5 s bound
    in rulings 5 and 6.
  * `aio/msg.py`: `Msg.Metadata.timestamp`, documented as "the time at
    which the message was delivered". Only that such a time exists was
    taken from it; what it measures was not checked.
* ADR-0013 prerequisite 4's measurement, as that ADR records it. It was
  not re-run.

## Amendment 3 (2026-09-24) — the slice-2 review and audit: a start re-publishes only what the prefix-stats log may lack; a decoded timestamp can be written again; four smaller rulings; the stream cap is left with the owner

Why: `reviewer` and `security-auditor` reviewed slice 2 as committed on
branch `claude/eager-gates-lyihfk`: design 390b978, tests ed50491 and
implementation 98d6c8e. They returned eight findings.

| # | From | Severity | Finding, in short | Ruled in |
| --- | --- | --- | --- | --- |
| S1 | security-auditor | medium | Every start re-publishes the stats of every state-changing record it replays, and does so before the deadline can fail it. Past Amendment 2 ruling 4's threshold of about 40,000 such records, each start fails after appending up to about a million messages to the prefix-stats stream, which has no byte cap. A restart loop repeats this, so the volume grows with the number of restarts, not with the size of the log. One authenticated agent can build such a log in about 15 s of its default budget, and a full shared store makes every stream's publishes fail. | rulings 1 and 6 |
| S2 | security-auditor | low | A hot-ip timestamp whose offset puts it outside the years 1 to 9999 in UTC decodes and is applied. `prepare()` then raises a raw `OverflowError`, so the trie stops at that record on every start. Amendment 2 ruling 5 said a `prepare()` failure cannot happen. | ruling 2 |
| R1 | reviewer | low | If `publish()` is cancelled, the publishes that had returned are not counted in `prefix_stats_published`. | ruling 3 |
| R2 | reviewer | low | `stop()` writes a "stopped" mark that nothing reads. | ruling 4 |
| R3 | reviewer | low | The test of the error's `__cause__` does not tell the order of the messages from the order in which they fail. | ruling 5 |
| R4 | reviewer | low | No test shows `publish()` waiting for the other publishes after a `BaseException` that is not an `Exception`, or when it is itself cancelled. | ruling 3 |
| R5 | reviewer | low | No test shows `build_service` passing the IPv6 floor to the worker. | ruling 5 |
| R6 | reviewer | low | The `hot_ratio` decode tests check only that a cause exists, so they cannot tell Amendment 2 ruling 9's order of checks from another. | ruling 5 |

**The owner question S1 reopens.** Amendment 2 ruling 4 and ADR-0013
assumption 144 accepted two things until the snapshot epic lands: every
start re-publishes the stats of the whole retained log, and no stream gets
a byte cap. This was put to the repository owner as a question. Under the
pre-1.0 standing order the session accepted this ADR's recommendation and
told the owner, who has not replied. S1 shows that the question costed the
re-publish wrongly. It priced one start, and it described a long log only
as "fails the start". But a failed start has already written what it
published, and a restart loop makes the total a function of time. Ruling 1
removes the cost where it arises. The other half of the question, a cap on
the streams, goes back to the owner with S1's evidence (ruling 6) and is
not ruled here.

**Convention.** Amendment 2 and the other texts of slice 2 are on this
branch and not on master, and the ADR's own convention would allow editing
them in place (Revision 2026-09-23). They are not edited in place, because
the reviewer and the auditor read them as they stand and quote them. Each
place whose text no longer holds carries a dated note pointing here, as
merged text does, and "Edits" below lists every note (assumption 83).

### Ruling 1. A start re-publishes from the last sequence the prefix-stats log holds (S1)

**The rule.**

1. **Step 2a.** Before it subscribes, `start()` reads the value of the
   last message of `hammertime.prefix-stats.v1`: `last = await
   bus.last_value(PREFIX_STATS.name)` (ADR-0013 Amendment 13). This runs
   after decision 4's step 2, so that `replay_target` is known, and before
   step 3. It is numbered 2a so that the texts that cite decision 4's
   steps 3 to 6 stay right.
2. **`republish_from`.** `start()` then sets `republish_from` from `last`.
   The rows are checked in order, and the first that applies decides:

   | `last` | `republish_from` | Record |
   | --- | --- | --- |
   | `None`: the log holds no message at its last offset | `0` | none |
   | `decode(last)` raises `CodecError` | `0` | `prefix_stats_last_ignored`, `reason=codec` |
   | its payload is not a `PrefixStatsChanged` | `0` | the same, `reason=payload_type` |
   | its envelope's `agent_id` is not `AGENT_ID` | `0` | the same, `reason=agent_id` |
   | its envelope's `sequence` is greater than `replay_target` | `0` | the same, `reason=ahead_of_log` |
   | anything else | its envelope's `sequence`, `W` | none |

3. **Who publishes.** Steps 5a and 5b run only for an event that changed
   the hot set and whose `state.event_sequence` is at least
   `republish_from`. An event that changed the hot set below it prepares
   and publishes nothing. Everything else about it is as before: it is
   applied, step 5 records it, `trie_updates` counts it, and its outcome
   is `APPLIED`.
4. **Before `start()`, and after the replay.** `republish_from` is `None`
   until step 2a has run, and `handle()` then publishes as if it were `0`.
   Every live event publishes: the value the table keeps is at most
   `replay_target`, and every record appended after `start()` read the
   log's end has a greater `event_sequence`.

**Why the invariant holds.** ADR-0010 decision 3's invariant is that a
restart cannot skip an event whose stats never reached the log. Take `W`
from the table, and an event `E` whose `event_sequence` is below `W` and
which changed the hot set.

* The trie is the prefix-stats topic's only producer (Amendment 2 ruling
  8), one trie process runs (decision 5), and it handles events in
  `event_sequence` order.
* The message at the log's last offset was published by a process that was
  handling the event whose `event_sequence` is `W`. That process had
  already handled `E`, or, once snapshots exist, it restored a snapshot
  taken after `E`, and a snapshot follows a flush (decision 16). Every
  publish of one event returns before the next event is handled (decision
  14 item 6), and a publish that fails ends the process (Amendment 2
  ruling 5).
* So that process either published all of `E`'s stats, every publish
  returning, or skipped them because `E` lay below its own
  `republish_from`. In the second case the same argument applies one start
  earlier. A start whose `republish_from` is `0` publishes everything it
  applies, and ends the regress.

The event at `W` is published again, because a crash may have left it half
published (decision 14 item 7). The log drops the copies it has seen inside
its 120 s window. A later copy carries a `sequence` the detector already
holds, and its sequence rule ignores it.

The argument assumes that the earlier process applied the same events.
"What it leaves open" lists where it did not.

**What it does to a start.**

* *The normal case.* The previous process published up to the last event
  it handled. The replay applies every retained record with no publish
  round trip, as slice 1 did, and publishes from that event on: its 25
  messages, then those of any later event. A start takes about what it took
  before slice 2.
* *A run of failed starts.* A start that fails, at the deadline or on a
  publish, has published only stats at or after its own `republish_from`.
  The next start's `republish_from` is at least as high. So a run of failed
  starts works through the log instead of repeating its head. Each start
  re-appends at most the 25 messages of one event, and only outside the
  120 s window. The volume grows with the log, not with the number of
  restarts.
* *No usable last message.* `republish_from` is `0` on the first start of
  a deployment; when every message of the stream has aged out (`max_age`
  is one day, so after a day with no transition); after a purge; when the
  last message was deleted on its own; and on each of the four refusals.
  The start then re-publishes the stats of every state-changing record it
  replays, as Amendment 2 ruling 4 had every start do. If that takes longer
  than the deadline, the start fails having published a prefix of them, in
  order. The next start reads the last of them and continues from there. In
  all, each retained state-changing record's stats are written about once,
  plus one event's per failed start.
* *Readiness* is unchanged. `start()` returns once `handle()` has returned
  for the message that brought the worker to `replay_target`, its
  publishes included when it has any. A ready trie has had every stat of
  every state-changing record it replayed from `republish_from` on
  acknowledged by the log; those below it were acknowledged under an
  earlier start.

**Why not wait for the snapshot epic.** Amendment 2's interim ran "until
the snapshot epic lands". A restore re-publishes everything after the
snapshot's position (Amendment 2 ruling 10), and snapshots are taken every
300 s by default (`HAMMERTIME_TRIE_SNAPSHOT_INTERVAL_S`). A trie handling
more than about 130 state-changing events a second would therefore have
more than Amendment 2 ruling 4's estimate of 40,000 records to re-publish
after a crash just before a snapshot, and would fail its restart the same
way (assumption 82).
With this ruling a restore replays from the snapshot and re-publishes from
`republish_from` (decision 16, as noted).

**Records.**

* `replay_complete` gains a field, `republish_from`: the value this start
  used.
* A new record, `prefix_stats_last_ignored`, at warning level, with the
  fields `reason` (one of the four tokens above) and `replay_target`. Like
  decision 12's other records, it carries fixed tokens and numbers only. It
  never carries the message's `sequence`, or anything else the message
  holds.

**The property.** `TrieWorker.republish_from -> int | None`: the value step
2a set, and `None` before it.

**What it leaves open.** Each case is recorded, not closed.

* *A family added, or a floor lowered, between two starts.* The earlier
  process did not publish the new family's events, which it skipped as
  `FAMILY_NOT_SERVED`, nor the newly reported prefixes of earlier events.
  Below `republish_from` those are now applied and not published. The
  detector learns such a prefix when it next changes. Purging
  `hammertime.prefix-stats.v1` before the restart makes the start
  re-publish everything, and a detector then loses whatever it had not yet
  read. The snapshot epic records the families it served (decision 16), and
  can close this.
* *A hot-ip log whose head has aged out.* The trie then lacks the IPs that
  became HOT before the oldest retained record (Consequences). It can find
  an event redundant that the earlier process applied, or apply one the
  earlier process found redundant. Below `republish_from` neither
  publishes. The detector keeps the earlier process's counts, which
  included those IPs.
* *A hot-ip stream recreated and grown past the last sequence.* The table
  sees only a `sequence` above `replay_target`. A recreated stream whose new
  end has passed the old last sequence goes unseen, and its events below
  that sequence are not published. This is the class of assumption 19's
  fourth case and decision 16's recreated-log case. The detector's sequence
  rule already refuses the new stream's lower sequences for every prefix
  it holds.
* *What a restart used to refresh.* Every start used to append the stats of
  every retained event older than 120 s again, so a detector that had lost
  its view could rebuild much of it from them. Nothing relied on that, and
  it was never designed as a mechanism. How the detector recovers its view
  is the detector epic's question.
* *Trust.* A principal that can publish to the prefix-stats stream can
  raise `republish_from` as far as `replay_target`, and so keep the stats
  between the true last event and the log's end from being re-published. It
  could publish false stats directly, which is worse. That stream is inside
  ADR-0013 assumption 13's boundary.

**Where it can be seen on JetStream.** The integration job (#52) can run
the auditor's validation. Seed a hot-ip log larger than one start can
re-publish, lower `HAMMERTIME_STARTUP_TIMEOUT_S`, restart the trie several
times more than 120 s apart, and read
`stream_info(hammertime-prefix-stats-v1).state.messages` after each start.
The count should grow by what each start published anew and then stop
growing, and not grow by a fixed amount per restart.

### Ruling 2. A timestamp the codec decodes can be encoded again (S2)

* **Decode.** Every timestamp field is returned in UTC: the envelope's
  `timestamp`, `RequestObservation.window_start`, the `timestamp` of
  `HotIpAdded` and `HotIpRemoved`, and `PrefixStatsChanged.timestamp`. A
  value with an offset is converted to UTC. A value with none is read as
  UTC, as before. A value whose UTC equivalent falls outside the years 1 to
  9999, which Python's `datetime` can hold, is a `CodecError`.
* **Encode.** A timestamp whose conversion to UTC overflows is a
  `CodecError`, not an `OverflowError`. After the decode rule, no decoded
  value can cause this. The rule covers a value built in-process.
* **The message.** The new refusal's text names the field. Its wording is
  not part of the contract.
* **The trie.** Such a hot-ip record is now `MALFORMED` [`codec`] at
  decision 6 step 2, before anything is applied, and the replay goes on
  past it. Amendment 2 ruling 5's row "`publisher.prepare` raises
  `ValueError` or `CodecError`: cannot happen" holds again, because the timestamp
  `prepare()` encodes is one that `decode` returned.
* **Why in the codec, and why refuse.** The codec writes every timestamp in
  UTC with `Z` (`_format_timestamp`), so a value it cannot convert is a
  value it cannot write. Refusing it on decode makes the two directions
  agree, as ADR-0015 assumption 66 asks for the other fields. It also fixes
  every consumer at once, which is ADR-0015 assumption 64's reason for
  putting such rules in the codec. Clamping the value would change the
  instant.
* **Elsewhere.** The aggregator now counts an observation whose
  `window_start` is such a value as malformed, instead of diverting it to
  reconciliation. Only a principal that publishes to the bus directly can
  produce such a message, because ingest's own encode of it fails (next
  bullet).
* **Not ruled here.** Ingest parses `window_start` itself
  (`services/ingest/src/hammertime/ingest/api/routes.py`,
  `_parse_window_start`) and does not check its range. Such a value passes
  ingest's validation, takes the dedup claim, and then fails in `encode`.
  Ingest reports that as a 503 "retry is safe", with the claim consumed.
  Nothing reaches the bus. It should be a 400 before the claim. That belongs
  to the agent protocol and needs a follow-up of its own (assumption 76).

### Ruling 3. `publish()` counts what returned and outlives nothing, also when it is cancelled (R1, R4)

Amendment 2 ruling 2 is made exact for each way `publish()` can end: it
returns; it raises `PrefixStatsPublishError`; it raises a publish's
`BaseException` that is not an `Exception`; or it is itself cancelled.

* **It outlives nothing.** In all four cases, `publish()` completes only
  once every publish it started has returned or raised. When `publish()`
  is cancelled, it cancels the publishes still in flight, waits until each
  has finished, and then raises `CancelledError`. It raises
  `CancelledError` even when some publish raised an `Exception`.
* **It counts what returned.** Once `publish()` has completed, in any of
  the four ways, `prefix_stats_published{family}` has grown by exactly the
  number of its publishes that returned. When the counter moves while
  other publishes are still in flight is not pinned.
* **Not pinned:** which exception `publish()` raises when more than one
  publish raises a `BaseException` that is not an `Exception`.
* **The worker is unchanged.** It catches only `PrefixStatsPublishError`. A
  cancellation therefore propagates with no `prefix_stats_publish_failed`
  record, and `start()` logs no `replay_complete`.

A cancellation of `publish()` comes from the startup deadline, from the
shutdown deadline, or from a test. In the first two the process is ending.
The count is pinned all the same, because Amendment 2 ruling 2's "for each
publish that returned" made no exception for it.

### Ruling 4. `stop()` marks nothing (R2)

Decision 13's "It then marks the worker stopped." and Amendment 2 ruling
6's "and marks the worker stopped whether or not the flush raised" describe
no behaviour.
Nothing reads such a mark. Everything that changes once `stop()` has begun
follows from the stop flag, which is set first: `handle()` returns
`STOPPED`, `apply_config()` changes nothing, step 3 passes nothing, and
`run()` returns. `TrieWorker.stop()` now takes three steps: it sets the
stop flag; it takes the lock; it awaits `publisher.flush()`, whose
exception propagates. Every call takes all three, and the worker keeps no
"stopped" state. No test can see the difference, and none is asked for.

### Ruling 5. Three findings need tests, and no change of design (R3, R5, R6)

* **R3.** Amendment 2 ruling 2 already says the cause is the exception "of
  the first failed message in `messages` order". The test must make the order in
  which the publishes fail differ from the order of the messages.
* **R5.** Decision 13's note already says that `build_service` passes
  `min_prefix_length_ipv6` to the worker. A test must show it reaching the
  publisher.
* **R6.** Amendment 2 ruling 9's order can be observed through the type of
  the cause, and this is now pinned. The check's own error is a `ValueError`. For
  every value decode refuses, the `CodecError`'s `__cause__` is a
  `ValueError` whose text names `hot_ratio`. It is never a `TypeError`,
  which comparing or testing a non-number first would raise, and never an
  `OverflowError`, which converting an oversized integer first would raise.
  The wording stays outside the contract. So the order of the finiteness
  and range checks, which only the wording shows, is not pinned.

### Ruling 6. A cap on the streams is the owner's question, and is not ruled

Ruling 1 removes what made the prefix-stats stream grow with the number of
restarts. It does not bound what live traffic writes. The four streams
share one store, and none has a byte cap (ADR-0013 decision 2, assumption
6). By estimate, one agent at its default budget (§36.6: 2,000 distinct IPs
a second) can drive tens to hundreds of GB a day into them (assumption 82).
By the auditor's reading, once the store is full every stream's publishes
fail: ingest answers 503, and the aggregator and the trie exit 1.

Whether to cap the streams, and whether a full stream drops its oldest
messages or refuses new ones, is a question for the repository owner
(assumption 81). It was raised on 2026-09-24 with this amendment. What each
answer means for the consumers is set out here, so that it can be decided
from this text:

* *Prefix-stats, discard old.* Under limits retention a cap removes the
  oldest messages whether or not the detector's durable has read them. The
  durable then continues from the first message left. A detector that keeps
  up loses nothing, since what goes is what it has already acknowledged. A
  detector that lags by more than the cap loses the stats it had not read.
  For a prefix with a later message still in the stream, it loses only the
  edges in between, in its `since` and `classification_changes`. For a
  prefix whose last message went unread, it keeps older stats, or none,
  until the next transition under that prefix publishes the prefix's stats
  again. Until then a stale detection can persist, or a new one go
  unreported. The trie can re-derive every such stat, and slice 3's read
  API reports it directly.
* *Prefix-stats, discard new.* A full stream refuses the trie's publishes.
  The trie exits 1, and every restart fails the same way until messages age
  out, which can take up to a day. The trie's read API is down all that
  time.
* *Hot-ip or observations, either policy.* Unlike stats, these cannot be
  re-derived. A hot-ip message discarded before the trie reads it is a
  transition the trie never applies, and until the snapshot epic lands the
  replay also loses the log's head sooner. An observation discarded before
  the aggregator reads it is a count the aggregator never adds. Refusing
  new messages instead stops the aggregator, or makes ingest answer 503.
* *No cap.* The status quo: the operator sizes the store and watches it.

Until the owner answers, ADR-0013 decision 2 and assumption 6 stand.
Nothing else in this amendment depends on the answer.

### Ruling 7. `CHANGES`

The implementing change replaces the third of slice 2's lines. That line is
on this branch and has not been released. It reads:

    Trie service re-publishes the PrefixStatsChanged of every state-changing hot-ip record it replays on start before it reports ready, so a start takes longer the more such records the log retains; a publish that fails stops the service with exit 1 (prefix_stats_publish_failed)

It becomes:

    Trie service re-publishes PrefixStatsChanged on start only from the last event whose stats are in hammertime.prefix-stats.v1 (every state-changing record it replays when that stream's last message is missing or not the trie's own), and reports ready once those publishes are acknowledged; a publish that fails stops the service with exit 1 (prefix_stats_publish_failed)

It is not `BREAKING`: no trie build has shipped. Rulings 2 to 5 add no line
(assumption 84).

### Test seams

* **The last value.** Every bus double handed to a `TrieWorker`, or to
  `build_service`, must implement `last_value(topic)`. A double that
  returns `None` models a prefix-stats log with no usable last message. It
  keeps Amendment 2's behaviour: the replay publishes everything.
* **The memory bus.** `InMemoryBus.last_value` returns the value last
  appended to the topic's log. A second trie started on the same bus after
  a first one has published therefore re-publishes from the first one's
  last sequence.
* **`NatsBus.last_value`** can be reached like the offset reads, through a
  stub JetStream context whose `stream_info` and `get_msg` answer what the
  test chooses (ADR-0013 Amendment 13).
* **An out-of-range timestamp** can no longer be written by `encode`. A test
  builds the bytes by editing an encoded envelope, as `test_codec.py`
  already does for out-of-range integers. An envelope's `event_id` does not
  depend on its timestamp, so the edited bytes still pass decode's
  `event_id` check.

### Questions this amendment closes

| Question | Left open by | Ruled in |
| --- | --- | --- |
| Must every start re-publish the whole retained log until the snapshot epic? | Amendment 2 ruling 4; decision 14 item 8 | ruling 1: no, only from the prefix-stats log's last sequence |
| Can a hot-ip timestamp stop the trie? | Amendment 2 ruling 5, which said no | ruling 2: no longer |
| What does `publish()` count, and wait for, when it is cancelled? | Amendment 2 ruling 2 | ruling 3 |
| What does "marks the worker stopped" mean? | decision 13; Amendment 2 ruling 6 | ruling 4: nothing, and it is dropped |
| Should the streams have a size cap? | ADR-0013 assumptions 6 and 144 | not ruled: with the owner (ruling 6) |

### Edits

**In this ADR.** Each is a dated note; no text is replaced.

* **Status line.** A closing paragraph on this amendment.
* **Decision 1.** A note after its Amendment 2 note: that note's added
  ordering constraint no longer holds.
* **Decision 4.** A note after the paragraph that ends "The consequence for
  a long log is under Consequences.": step 2a.
* **Decision 6.** A note after its Amendment 2 note: the property
  `republish_from`, and when steps 5a and 5b run.
* **Decision 7.** A note after its Amendment 2 note: `bus.last_value` joins
  the row of bus reads, and the `prepare` row holds for timestamps.
* **Decision 12.** A note after its Amendment 2 note: `replay_complete`'s
  field and the new record.
* **Decision 13.** A note after its Amendment 2 note: "marks the worker
  stopped" is dropped.
* **Decision 14.** A note after the note that settles item 10: item 8's
  "every start" no longer holds.
* **Decision 16.** A note after its Amendment 2 note: what a restore
  re-publishes.
* **Test seams.** A note after its Amendment 2 note.
* **Consequences.** A note under "Slice 2 without the snapshot epic
  republishes.", after its Amendment 2 note.
* **Amendment 2.** A note at the end of each of rulings 2, 4, 5, 6, 7, 9, 10
  and 11; one after its "Test seams"; and one under assumption 62.
* **This section.**

**In other documents.**

* **`docs/adr/0013-nats-jetstream-event-log-static-shards.md`** (Amendment
  13): a clause on the status line; in decision 3, a line in the interface
  block, an italic paragraph after the Amendment 11 paragraph, a line in the
  `NatsBus` block and an italic sentence after the `hammertime.bus.memory`
  paragraph's Amendment 10 sentence; a dated note under Amendment 12's
  assumption 144; an "Amendment 13" section.
* **`docs/adr/0010-read-apis-and-shared-prefix-predicate.md`** (Amendment
  4): a clause on the status line; a dated note after decision 3's
  Amendment 3 note; an "Amendment 4" section.
* **`docs/runbook.md`**, "Trie service restart": a bullet, "What a start
  re-publishes", between "Its deadline" and "A corrupt trie". Nothing is
  replaced.
* **`docs/spec/README.md`.** Three rows gain entries; nothing is removed.
  * §19's ADR-0017 entry was "`docs/adr/0017` (decisions 3, 6 and 14;
    Amendment 2: the publisher)", and gains Amendment 3.
  * §32/33: "`MessageBus.end_offset` and `first_offset`" became
    "`MessageBus.end_offset`, `first_offset` and `last_value`";
    "`docs/adr/0010` (Amendments 1, 2)" became "(Amendments 1, 2, 4)";
    "`docs/adr/0013` (decisions 2, 9; Amendment 10)" became "(decisions 2,
    9; Amendments 10 and 13)"; and the ADR-0017 entry, "(decisions 3, 4, 8
    and 16; Amendment 2: the replay re-publishes the stats it replays)",
    gains Amendment 3.
  * §37's ADR-0017 entry was "`docs/adr/0017` (decision 12; Amendment 2
    ruling 7: `prefix_stats_published`, and why slice 2 does not measure
    `hot_transition_to_prefix_update_latency`)", and gains Amendment 3.

No spec section, schema or protocol document changes (assumption 85).

### Assumptions

Each is a judgment call that the findings, the spec and the earlier ADRs do
not make. Push back on them individually; numbering continues the ADR's
list.

65. **How far the stats reached is read from the prefix-stats log itself.**
    Four alternatives were weighed.
    * *The snapshot epic alone.* It bounds a restore by the snapshot
      interval, which is still too much for a busy trie (ruling 1, "Why not
      wait").
    * *A local file recording the last sequence published.* Written after
      every event, it costs a synchronous write per event. Written less
      often, it goes stale, and a snapshot is that already.
    * *A byte cap with discard old on the prefix-stats stream, the auditor's
      smallest fix.* It keeps the damage inside that stream. But the
      restart loop still happens, the trie still cannot start, and the
      stream still churns.
    * *A duplicate window as long as the stream's `max_age`.* The log would
      drop every copy re-published within a day. But the server keeps every
      message id of that day in memory (ADR-0013 assumption 7), and each
      start still waits for 25 acknowledged publishes per record.

    The log already records which stats reached it, and the trie is its
    only producer. The cost is one new bus read at start, and the cases
    under "What it leaves open".
66. **`republish_from` is the last message's `sequence`, not one past it.**
    The event may have been half published when the process ended, and the
    trie cannot tell. Publishing it again costs 25 publishes, most of which
    the log drops.
67. **Only the message at the last offset is read.** A missing last message,
    or one that is not the trie's own, falls back to a full re-publish. That
    is safe, and a failed start still makes progress. Finding the trie's
    last message behind a hole, or behind a foreign message, would need a
    scan, for cases only an administrator or a principal on the bus can
    cause.
68. **A last message the trie cannot use falls back to a full re-publish;
    it does not fail the start.** Failing would let one message on the
    prefix-stats stream stop the trie. A full re-publish keeps the
    invariant, and the record says why it happened.
69. **`ahead_of_log` compares with `replay_target`.** Every `sequence` the
    trie wrote from the current hot-ip log is at most that log's end. One
    above it came from another numbering of the log, after the stream was
    recreated, or from another publisher. Re-publishing is then futile for
    the prefixes the detector holds with higher sequences, and harmless.
70. **The envelope's `sequence`, not the payload's.** The trie writes the
    same number in both. The envelope's is the one `event_id` is derived
    from, and decode checks `event_id` against it.
71. **No record when the log holds no message at its last offset.** That is
    the first start of a deployment, or a stream a quiet day has emptied,
    and a full re-publish is what is wanted then. `replay_complete`'s
    `republish_from=0` shows it.
72. **The residuals are recorded, not closed.** Closing the family and floor
    case needs the trie to know what an earlier process served, and only
    the snapshot epic records that. Closing the recreated-stream case needs
    a signal that the stream was recreated, which assumption 33 already
    found missing.
73. **Decode returns UTC, and does not only check the range.** A value
    returned in UTC can be written without another check, and every
    consumer sees one form. No consumer reads the original offset: the
    aggregator uses `timestamp()`, and the trie compares instants and
    writes UTC.
74. **Refuse, do not clamp.** The codec writes UTC with a four-digit year,
    and a clamped value would be a different instant. ADR-0016 refuses an
    out-of-range integer, rather than saturating it, in the same way.
75. **Encode's overflow becomes a `CodecError` too,** so that encode raises
    only `CodecError` for a value it cannot write. That is ADR-0016
    assumption 4's reason: a producer bug made loud at the producer.
76. **The ingest path is named, not fixed.** It is the agent protocol's
    contract, a 400 or a 503. It is not part of either finding, and nothing
    from it reaches the bus.
77. **`publish()`'s count is pinned for a cancellation; the moment it moves
    is not.** The count after completion is what an operator could read.
    When it moves belongs to the mechanism, and leaving it open leaves the
    coder free to choose one.
78. **A cancellation wins over a publish failure.** A cancelled task that
    raised anything but `CancelledError` would break asyncio's contract
    with whoever cancelled it, and the process is ending either way.
79. **"Marks the worker stopped" is dropped, not made a property.** No
    reader needs one. `TrieService` awaits `worker.stop()`, and the
    snapshot epic's final snapshot runs inside it, after the flush.
80. **Amendment 2 ruling 9's order is pinned through the type of the cause
    only.** That ruling kept the wording out of the contract, and this
    amendment does not reverse it.
81. **The cap is the owner's.** It trades a lagging consumer's unread
    messages (discard old), or a stopped producer (discard new), against a
    full shared store, and the right size depends on each deployment's
    disk. ADR-0013 assumption 6 made it a deployment decision, and the
    owner was asked about it once already.
82. **The volumes and rates are estimates, not measurements.** At one
    agent's default budget: about 2,000 observation messages a second (one
    day's retention); at most about 4,000 hot-ip records a second, every IP
    turning hot once and cold one window later (30 days' retention); and
    prefix-stats messages up to the trie's publish rate, at most about
    17,500 a second by Amendment 2 ruling 4's estimate (one day). At a few
    hundred bytes a message, that is tens to hundreds of GB a day. The 130
    events a second of ruling 1 is Amendment 2 ruling 4's 40,000 records
    over the default 300 s snapshot interval. Nothing was measured.
83. **Slice 2's texts get dated notes, not edits in place,** although they
    have not merged. The reviewer and the auditor quote them, and a note
    keeps what they read.
84. **`CHANGES`: one line replaced, none added.** The third line described
    every start re-publishing the whole log. Rulings 2 to 5 change nothing
    a released build does. The codec change reaches the aggregator, which
    is on master, only for an observation that ingest cannot publish and
    only a principal on the bus can. The trie has not shipped. This is
    `CLAUDE.md`'s "if unsure, it does not", and the hand-off report says so.
85. **No spec section, schema or protocol document changes.** No section of
    the spec describes re-publishing. `schemas/prefix_stats_event.v1.json`
    already says the `timestamp` is the hot-ip event's, which it still is,
    in UTC. The read API is unchanged. ADR-0015 and ADR-0016 carry no note:
    no text of theirs becomes false, and `docs/spec/README.md`'s §19 row
    points here.
86. **The runbook is edited now.** Assumption 62 held the runbook back until
    slice 2's implementation merged. That implementation is now on this
    branch and merges with the edit, and the auditor asked for the runbook
    to say what a failed start writes.

### Sources

Read on 2026-09-24 for this amendment. No web source was consulted.

* Repository files:
  * `services/trie/src/hammertime/trie/worker.py` and `publisher.py`, at
    98d6c8e.
  * `packages/hammertime-core/src/hammertime/core/events/codec.py`:
    `_format_timestamp` converts with `astimezone(UTC)`, and
    `_parse_timestamp` returns `datetime.fromisoformat`'s value with its
    own offset.
  * `packages/hammertime-core/src/hammertime/core/runtime.py`: `_supervise`
    wraps `service.start()` in `asyncio.wait_for(..., startup_timeout)`,
    which cancels it at the deadline.
  * `packages/hammertime-bus/src/hammertime/bus/nats.py`, `memory.py` and
    `topics.py`: `stream_config_for`, `DUPLICATE_WINDOW_S`,
    `NatsBus.end_offset`, `InMemoryBus._append`, and the four topics'
    retention.
  * `services/ingest/src/hammertime/ingest/api/routes.py`
    (`_parse_window_start`; `create_observation` answers 503 for any
    exception from `publish`) and `publisher.py` (`encode` runs inside the
    publish).
  * `services/aggregator/src/hammertime/aggregator/worker.py`: it reads
    `window_start.timestamp()`.
  * `deploy/docker-compose.yml`, which sets no restart policy on the trie,
    and `deploy/k8s/README.md`, which runs the trie as a StatefulSet.
* nats-py 2.16.0 as installed, in `.venv/lib/python3.12/site-packages/nats/`:
  * `js/manager.py`: `get_msg(stream_name, seq=None, subject=None,
    direct=False, next=False)`. Its default form sends
    `$JS.API.STREAM.MSG.GET.<stream>` through `_api_request`, which raises
    `APIError.from_error` on an error response. `get_last_msg(stream_name,
    subject)` asks by `last_by_subj`.
  * `js/errors.py`: `APIError.from_error` raises `NotFoundError` for code
    404.
  * `js/api.py`: `RawStreamMsg.data: Optional[bytes]`; `StreamState`'s
    `messages` and `last_seq`.
  * `js/client.py`: `class JetStreamContext(JetStreamManager)`, so the bus's
    context has `get_msg`.
* The findings' own readings, taken as the findings state them and not
  re-checked: the auditor's, that a full JetStream store makes every
  stream's publishes fail, and its restart cadence and volumes; the
  reviewer's, that `asyncio.gather` drops its results when it is cancelled.
  Ruling 3 pins the observable, and does not rest on the second.
* Python's `datetime` holds the years 1 to 9999 (`datetime.MINYEAR` and
  `datetime.MAXYEAR`), and `astimezone` raises `OverflowError` past them, as
  the auditor's finding reports. Nothing was run here.

## Amendment 4 (2026-09-24) — slice 3 designed: the read API

Why: decision 15 fixed five things about slice 3 (items 1 to 5) and left
five to its own design (item 6). The dispatch for this design asked for
those five, and for more:

* the exact routes, parameters and response shapes against
  `read-api-v1.md`, and the answers to a malformed address or CIDR, to one
  of the wrong family, and to a family the trie does not serve;
* how the reads keep decision 9's R2 and R3;
* whether the read API needs §16's prefix metadata (ADR-0015 assumption 7);
* what the read routes answer before `start()` has caught up;
* the port they share with the admin routes, which
  `HAMMERTIME_TRIE_QUERY_BIND` exposes on all interfaces, and whether they
  change the question ADR-0013 left with the owner;
* authentication and rate limiting;
* how the security audit treats untrusted path input;
* metrics and log records under decision 12's rules;
* the `CHANGES` lines, `request_count`'s meaning among them (ADR-0015
  Amendment 5 ruling 11).

Decision 15's items 1 to 5, ADR-0010's decisions and `read-api-v1.md`'s
rules are not re-opened: every ruling below builds on them. This ADR,
ADR-0010, ADR-0015 and `read-api-v1.md` are on master, so nothing merged is
rewritten. Each place whose text is now incomplete or no longer true carries
a dated note, and "Edits" lists them.

### Ruling 1. `evaluate_prefix_state`, in `hammertime.core.state.prefix`

```python
# hammertime.core.state.prefix   Spec: §13, §38; ADR-0010 decision 1; ADR-0017 Amendment 4 ruling 1
def evaluate_prefix_state(hot_count: int, capacity: int, config: DetectionConfig) -> PrefixState: ...
```

* **What it answers.** `PrefixState.HOT_PREFIX` when `hot_count >=
  config.minimum_hot_ips` and `Fraction(hot_count, capacity) >=
  config.minimum_hot_ratio`, and `PrefixState.NORMAL` otherwise, exactly as
  ADR-0010 decision 1 has it. The ratio is compared exactly, against the
  exact value of the configured float, so a ratio equal to that value
  qualifies. It never answers `BOT_NETWORK` (ADR-0010 decision 2). It may
  leave the ratio uncomputed when the count test fails.
* **What it refuses,** before it compares:
  * a `hot_count` or a `capacity` that is not an `int`, or is a `bool`:
    `TypeError`;
  * a `capacity` below 1: `ValueError`;
  * a `hot_count` below 0, or above `capacity`: `ValueError`.

  Types are checked before values. A message names the argument and the
  rule, never the value, and its wording is not part of the contract.
* **The one place the comparison is written.** `hammertime.core.state`
  re-exports it beside `evaluate_ip_state`. Nothing else in the repository
  compares a count with `minimum_hot_ips` or a ratio with
  `minimum_hot_ratio` (§30's rule, applied to prefixes by ADR-0010
  decision 1).
* **Its callers.** In slice 3, the trie's read routes. The detector's
  `rules/baseline.py` adopts it with the detector epic. A
  `PrefixStatsChanged` whose `hot_count` exceeds its `capacity` will then
  raise here. ADR-0016 assumption 11 left that cross-field check open, and
  what the detector does with such a message is that epic's to rule.

### Ruling 2. The three routes

All three are `GET` routes on decision 13's app. Another method on their
paths answers FastAPI's 405, and a path none of them matches gets the
framework's answer, as any undeclared path does.

| Route | Input | Body on `200`: exactly these keys |
| --- | --- | --- |
| `GET /prefix/{cidr}` | `{cidr}`: everything after `/prefix/` (Starlette's `path` convertor). Query parameters are ignored. | `prefix`, `hot_ips`, `capacity`, `hot_ratio`, `state`, `as_of`, `event_sequence`, `config_version` |
| `GET /ip/{addr}` | `{addr}`: everything after `/ip/` (the `path` convertor). Query parameters are ignored. | `ip`, `state`, `request_count`, `attributes` while HOT only, `matched_prefixes`, `as_of`, `event_sequence`, `config_version` |
| `GET /prefixes/hot` | `minimal`, read from the raw query. Other query parameters are ignored. | `prefixes`, `as_of`, `event_sequence`, `config_version` |

An item of `matched_prefixes` or `prefixes` holds exactly `prefix`,
`hot_ips`, `capacity`, `hot_ratio` and `state`.

**Parsing `{cidr}`.** The checks run in this order, and the first that fails
decides the answer:

1. The text holds a `/`, and is split at the first one into an address text
   and a length text. Else `malformed prefix`.
2. The length text is one to three ASCII digits and nothing else. Else
   `malformed prefix`.
3. `Address.parse` accepts the address text. Else `malformed prefix`.
4. The length is at most the address's `bit_length`. Else `prefix length
   out of range`.
5. No bit of the address is set below the length. Else `host bits set`.
6. The trie serves the address's family. Else `address family not served`.

**Parsing `{addr}`.** `Address.parse` accepts the text, else `malformed
address`. Then the trie serves its family, else `address family not
served`.

**Parsing `minimal`.** Absent means `false`. Given once, as exactly `true`
or `false`, it means that. Anything else — another value, an empty one, or
the parameter given more than once — is `minimal must be true or false`.

**A refusal** is `400`, `Content-Type: application/json`, with the body
`{"detail":"<text>"}`, where the text is one of the six above. The texts are
fixed. No response repeats any part of the request's path or query.

**A family the trie does not serve is refused,** and not answered with
zeros. ADR-0010 decision 4's zero-valued answer is the trie's statement that
nothing beneath a prefix is hot, and the trie can make no statement about a
family it does not hold. Decision 15 recommended this.

**The bodies.**

* **`GET /prefix/{cidr}`.** `prefix` is `str(prefix)`. `hot_ips` is
  `trie.hot_count(prefix)` (decision 15 item 3). `capacity` is
  `prefix.capacity()`, `hot_ratio` is `prefix.hot_ratio(hot_ips)`, and
  `state` is `evaluate_prefix_state(hot_ips, capacity, config)`.
* **`GET /ip/{addr}`.** `ip` is `str(address)`. `state` is `"HOT"` when
  `trie.contains(address)`, else `"COLD"`. While HOT, `request_count` is
  `records[address].request_count`, and `attributes` is
  `records[address].attributes` as a JSON object. While COLD,
  `request_count` is `0` and `attributes` is absent (ADR-0015 Amendment 5
  ruling 1).
  * A HOT address with no record is a state §46.5 forbids. The route then
    raises `InvariantViolation`, with a message that names §46.5 and nothing
    of the request, and FastAPI answers `500`. The read path neither repairs
    the state nor ends the process.
  * `matched_prefixes` are the address's ancestors with 24, 16 and 8 host
    bits, shortest first: `/8`, `/16`, `/24` for IPv4, as `read-api-v1.md`
    has them, and `/104`, `/112`, `/120` for IPv6. They are present whether
    the address is HOT or COLD. Each item is shaped as `GET /prefix/{cidr}`'s
    body without the three envelope keys.
* **`GET /prefixes/hot`.** For each family the trie serves, with `L_F` that
  family's reporting floor (`HAMMERTIME_TRIE_MIN_PREFIX_LENGTH`, or
  `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6`; Amendment 2 ruling 1),
  `prefixes` holds every prefix of that family whose length is from `L_F` to
  `bit_length`, whose `hot_ips` is at least 1, and whose `state` is
  `HOT_PREFIX`. With `minimal=true` it keeps only those with no listed
  prefix of the same family strictly inside them. IPv4 entries come before
  IPv6 ones; within a family the order is length descending, then network
  ascending. Ruling 5 says how the list is found.

  For example, under the default document, the 156 HOT addresses
  `10.20.30.1` to `10.20.30.156` of §42 give 21 prefixes: eight `/28`s
  (`10.20.30.16/28` to `10.20.30.128/28`, each 16 of 16), five `/27`s,
  three `/26`s, two `/25`s, `10.20.30.0/24`, `10.20.30.0/23` and
  `10.20.28.0/22` (156 of 1,024). With `minimal=true` the list is the eight
  `/28`s: a fully HOT `/28` meets a `minimum_hot_ips` of 16 at a ratio of 1.
* **The envelope.** `as_of` is `null`, or the codec's rendering of a
  timestamp: `YYYY-MM-DDTHH:MM:SS` in UTC, then `.ffffff` only when the
  microseconds are not zero, then `Z`. `event_sequence` is
  `state.event_sequence`, and `config_version` is
  `state.config.config_version`.

**Rendering.** A route builds its body as plain JSON data and returns a
Starlette `JSONResponse` built from it: compact, UTF-8, `Content-Type:
application/json`. FastAPI then encodes and validates nothing. A body's
parsed value is the contract, not its bytes.

### Ruling 3. The readiness gate comes first

* **Before the input.** Each read route checks readiness before it looks at
  its input. While the service is not ready it raises `ServiceNotReady`,
  which decision 13's handler renders as `503` with `/readyz`'s body
  (ADR-0009 A4): `{"status":"starting"}` from construction until `start()`
  marks the service ready, and `{"status":"stopping"}` from the moment
  `stop()` begins. So while the service is not ready a malformed request is
  `503`, not `400` (§47.2: domain read endpoints "MUST answer 503 while the
  service is not ready").
* **What ready means for a read.** `TrieService.start()` marks the service
  ready only once the worker has caught up (decision 4). A ready trie has
  therefore handled or passed every hot-ip record the log held when
  `start()` began.
* **Who sees `starting`.** `run()` binds the socket after `start()` has
  returned (§47.6; ADR-0009 A8), so over the network the trie answers
  nothing before it is ready. An in-process caller of `TrieService.app`
  sees `starting`. A request in flight when `stop()` begins sees
  `stopping`.

### Ruling 4. R2 and R3 on the read routes

* **One synchronous call per request.** A read route's handler is `async
  def` and holds no `await`. Its readiness check, its parse, every read of
  `TrieState` and the building of its response run in that one call. It
  reads `state.config` once, and every `state` and the `config_version` in
  its body come from that read. So a body describes the state between two
  whole events, with that state's `event_sequence`, `as_of` and
  `config_version` (decision 15 items 1 and 2).
* **Plain functions.** `query.views` holds the parsing and the building of
  the three bodies. Its functions are neither coroutines nor generators, and
  each returns data that holds no reference into the trie or the record
  map.
* **One thread.** No route, dependency or exception handler of the app is a
  plain `def` (R3). Nothing in `query` calls `run_in_threadpool` or
  `asyncio.to_thread`, or uses `BackgroundTasks` or `StreamingResponse`. No
  route returns a value for FastAPI to serialize.
* **No new writer, and no lock.** The worker stays the only writer of
  `TrieState` (decision 2; R1). The read routes take no lock: with no
  `await` inside a read none is needed, and taking the worker's would make a
  read wait behind the publish of an event already applied (Amendment 2
  ruling 3).

### Ruling 5. `GET /prefixes/hot` walks only where a prefix could qualify, and the trie keeps no index

**The walk.** For each family `F` the trie serves, with `m =
max(config.minimum_hot_ips, 1)`:

1. It starts at `F`'s `/0`, and stops at once if its `trie.hot_count` is
   below `m`.
2. At each prefix it visits, it reads each child's count with
   `trie.hot_count`, and visits the child only if that count is at least
   `m`.
3. It evaluates each visited prefix whose length is from `L_F` to
   `bit_length` with `evaluate_prefix_state`, and lists it when the answer
   is `HOT_PREFIX`.

The walk uses the `HotTrie` protocol only (ADR-0014 decision 2). It reads
neither `PatriciaTrie.arena` nor `PatriciaTrie.root`, and calls none of
`iter_prefix_counts`, `iter_hot_addresses` and `iter_nodes`, each of which
runs over the whole hot set.

**Why nothing is lost.** No prefix counts more HOT addresses than a prefix
that contains it (§12). Below a prefix with fewer than `m`, no prefix meets
`minimum_hot_ips`, and when `m` is 1, none holds a HOT address.

**What it costs.** Write `H` for the family's HOT addresses and `N` for its
prefixes, of any length, that hold at least `m` of them. The walk visits
those `N` and reads at most `2N + 1` counts, each an O(`bit_length`) walk of
the structure (ADR-0014 decision 6). The prefixes of one length are
disjoint, so `N <= (bit_length + 1) * floor(H / m)`: about `2H` for IPv4 at
the default `m` of 16. What follows is an estimate, not a measurement
(assumption 97):

* a hundred thousand HOT addresses spread evenly over the IPv4 space give an
  `N` near 8,000, and a request of about a tenth of a second;
* a million give roughly ten times that;
* HOT addresses packed as §42's are give an `N` of the same order as the
  list the request returns.

While a walk runs the writer waits, which is decision 9's stated cost, so a
client that repeats the request delays the writer by one walk each time.
Ruling 7 records that as a residual of the port's exposure.

**Why the trie keeps no index.** Decision 15 named the alternative: §12's
cached `prefix_state`, an index of `HOT_PREFIX` prefixes kept by the writer.
It is not built, for four reasons (assumption 98).

* *It moves the cost onto the writer.* Every event that changes the hot set
  would re-evaluate its `bit_length - L_F + 1` ancestors (25 at the
  defaults) inside R1's section, the startup replay included. The replay is
  the trie's tightest budget until the snapshot epic lands (Consequences).
* *It is derived state to keep exact.* It would be built once the replay
  catches up, updated in R1's section, and rebuilt in `apply_config` and
  after a snapshot restore. Each is a place where the index and the trie
  could disagree, and each needs tests of its own.
* *It saves most where it matters least.* It pays off when many prefixes
  hold `m` HOT addresses and still fail the ratio: HOT addresses spread
  thinly over the space. Where they cluster, as a bot network's do, most
  prefixes the walk visits are listed anyway.
* *No request is cheaper than its answer.* The index would not bound a
  request whose list is long, and a long list is what a clustered bot
  network produces.

So `apply_config` is unchanged (decision 10), and ADR-0010 decision 5's
"eagerly for the cached `prefix_state`" has nothing to act on (ADR-0010
Amendment 5).

**What would change it.** A measurement that the walk dominates, in the
integration job (#52) or in a deployment. The index above is then the next
step. A pruned iterator in the structure package would cut the walk's
constant without an index, and would amend ADR-0014's `HotTrie`. Neither is
designed here.

### Ruling 6. The read API needs no prefix metadata

* No route reads or returns §16's metadata. `create_app` takes no
  `PrefixMetadataStore`, and nothing in epic #10 holds one (decision 15 item
  4, which stands).
* ADR-0015 assumption 7's open question — how an operator declares prefix
  metadata — therefore does not block slice 3, and this amendment raises
  nothing for the owner on it. When a declaration source is designed,
  ADR-0005's "`GET /ip/{ip}` returns both, separately keyed" comes due as an
  additive field (decision 15 item 4).

### Ruling 7. The port: shared, unauthenticated, not rate limited; its exposure stays with the owner

* **The read routes share the admin app,** and so the admin port,
  `HAMMERTIME_TRIE_QUERY_BIND` (default `0.0.0.0:8081`; decision 11). A
  second port would need a second server, a second key and a second answer
  to readiness, and nothing asks for one.
* **What slice 3 changes about the port.** Until now it served `/healthz`,
  `/readyz` and an empty `/metrics`. From slice 3 it tells any client that
  reaches it which addresses are HOT, with each one's `request_count` and
  attribute document, and which prefixes are `HOT_PREFIX`. Each read also
  runs on the event loop the writer shares (ruling 4), so a client that
  repeats `GET /prefixes/hot` delays the writer by one walk per request
  (ruling 5). `read-api-v1.md` expects these endpoints to be reachable only
  inside the deployment. The reference compose file publishes the port on
  every host interface (`8081:8081`).
* **No authentication.** `read-api-v1.md`'s "No authentication in v1" stands
  and is not re-opened. An operator credential would need a design of its
  own — a secret, a key and rotation, as §36.1 to §36.4 are for agents — and
  no requirement asks for one in v1. A deployment that exposes the port
  beyond its trust boundary puts an authenticating proxy or a network policy
  in front of it.
* **No rate limit, and nothing in its place.** §36.5 records that an
  in-process bucket cannot protect the accept queue, and the port knows no
  identity to charge beyond the peer address. Two other mechanisms were
  weighed. A response memo keyed by `event_sequence` and the configuration
  would still walk once per event while events arrive, and so bounds nothing
  under load. A lock around the walk does not by itself let the writer in
  between two walks, since no walk awaits. Ruling 5's bound on one request
  is what the design offers.
* **The port's exposure is the owner's question, and is not ruled.**
  ADR-0013 left "the services' own ports (8080-8083, 9090)" with the owner
  (its assumption 13, as amended by Amendment 4 ruling S1), and this ADR's
  Consequences said so of `8081`. Slice 3 makes the answer matter more. The
  question was raised again on 2026-09-24 with this amendment. What each
  answer means is set out here, so that it can be decided from this text:
  * *`8081` published on `127.0.0.1` only,* as ADR-0013 did for `4222`,
    `8222` and `6379`. The bind inside the container stays `0.0.0.0:8081`:
    Prometheus scrapes `trie:8081` over the compose network
    (`deploy/prometheus.yml`), and the healthcheck calls `127.0.0.1` inside
    the container. Operators on the host keep their access. The same
    reasoning covers `8082`, `8083` and `9090`, but not ingest's `8080`,
    which agents must reach and §36 authenticates.
  * *`8081` kept on every interface.* Any host that reaches the machine
    reads the hot set and can delay the writer, which argues for designing
    authentication for the read routes.
  * *Left to each deployment,* with `.env.example` saying what the port
    discloses.

  Until the owner answers, ADR-0013 decision 11 and this ADR's decision 11
  stand.

### Ruling 8. `create_app`, and the service's wiring

```python
# hammertime.trie.query.app   Spec: §22, §29, §31, §46.7, §47; ADR-0017 decisions 9, 13 and 15, Amendment 4
def create_app(readiness: Readiness, *, state: TrieState, metrics: TrieMetrics,
               min_prefix_lengths: Mapping[AddressFamily, int]) -> FastAPI: ...
```

* **What it keeps.** Decision 13's app: `FastAPI(title="hammertime-trie",
  openapi_url=None, docs_url=None, redoc_url=None)`, `app.state.readiness`,
  the `ServiceNotReady` handler and the three admin routes, unchanged. It
  adds ruling 2's three routes, under decision 13's rule that
  `read-api-v1.md`, not a generated schema, is their contract.
* **What it checks.** `min_prefix_lengths` holds an entry for each family
  `state` serves, an integer from 0 to that family's `bit_length`. Else
  `create_app` raises `ValueError`. It keeps a copy.
* **The service.** `TrieService` builds its app with `state=worker.state`,
  `metrics=worker.metrics` and the floors it passes the worker (decision 13
  as amended by Amendment 2).
* **No defaults.** `query.app` must not import `publisher`, where
  `DEFAULT_MIN_PREFIX_LENGTHS` lives (decision 2 as amended), and the
  service already holds the floors.
* **Imports.** As decision 2's note of 2026-09-24 (this amendment) says.

### Ruling 9. Metrics and log records

| Series | Kind | Labels | Meaning |
| --- | --- | --- | --- |
| `prefix_queries` | counter | `route` (`prefix`, `ip`, `prefixes_hot`), `result` (`ok`, `invalid`, `family_not_served`, `not_ready`) | §37: the requests a read route answered, each counted once. `ok` is a `200`; `family_not_served` the `400` whose text is `address family not served`; `invalid` any other `400`; `not_ready` a `503`. |

* Not counted: a request the framework answers (a `404` or `405`), a `500`,
  and the admin routes.
* Decision 12's rules hold: strict names and label names, label values
  compared as text, rendering the telemetry epic's.
* **No log record.** The read routes write none, per request or per
  refusal. What they would carry is request content, and decision 12 keeps
  the trie's own records to fixed tokens and numbers.
* **uvicorn's access record is unchanged.** The service's uvicorn runs with
  `log_config=None`, so the record reaches the root logger's JSON handler,
  as it already does for the admin routes. It carries the request line.
  uvicorn percent-quotes the path, h11 admits only visible ASCII in the
  request target, and the JSON renderer escapes the rest (Sources).
* A `500` from ruling 2's `InvariantViolation` is logged by the server with
  its traceback, and its message carries nothing of the request.

### Ruling 10. Untrusted input, and how the security audit treats it

The request is untrusted: the port has no authentication (ruling 7). The
server decodes the path's percent-escapes before routing, so `{cidr}` and
`{addr}` may hold any character, a NUL or a newline included, up to the
server's own limit on the request line.

* **Where it goes.** Only into ruling 2's parse: one split at `/`, a check
  of at most three ASCII digits, and `Address.parse`, which calls
  `ipaddress.ip_address`, the parse ingest runs on an agent's address.
  Nothing converts an unbounded digit string, builds a pattern, a path or a
  format string from it, logs it, or keys state on it. Apart from the
  counter, a request leaves nothing behind.
* **Every input a route receives is answered.** Each reaches `200`, `400`
  or `503`. None reaches `422`, because no parameter is declared for FastAPI
  to validate, and none reaches `500`, which only ruling 2's forbidden state
  causes. The bullet on the framework's routing, below, says what never
  reaches a read route.
* **Nothing is echoed.** A refusal carries one of six fixed texts, and a
  `200` body names only the canonical text of what was parsed.
* **What `Address.parse` accepts, the read API accepts,** and it answers for
  the address `Address.parse` returns, as ingest counts it (assumption 88).
* **The framework's routing.** Starlette matches `^/prefix/(?P<cidr>.*)$`
  with `re.match` (Sources). A path with a newline before its end matches no
  route and gets the framework's `404`. A single newline at the very end is
  left out of the captured text.
* **What the audit checks:** that every input is answered and none is echoed
  or logged; that the walk's cost follows ruling 5 and no input steers it
  (only `minimal` is read, and it does not change the walk); what the routes
  disclose; and that no thread reaches `TrieState`. The port's exposure is
  the owner's open question (ruling 7), and its residuals are recorded
  there, not re-found.

### Ruling 11. `CHANGES`

The implementing change adds four lines at the top, in this order. None is
`BREAKING`: no build that serves the trie's read API has shipped, and every
line adds to what the trie serves.

    Add the trie read API on HAMMERTIME_TRIE_QUERY_BIND, with no authentication: GET /prefix/{cidr}, GET /ip/{addr} and GET /prefixes/hot[?minimal=true], each carrying as_of, event_sequence and config_version, answering 503 with the /readyz body until the trie is ready, and 400 for a malformed address or prefix or an address family the trie does not hold
    Trie read API classifies a prefix HOT_PREFIX when hot_count >= minimum_hot_ips and hot_count/capacity >= minimum_hot_ratio under the detection config in force; GET /prefixes/hot lists every such prefix holding a HOT address, from the family's minimum reporting length (HAMMERTIME_TRIE_MIN_PREFIX_LENGTH, HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6) to the host route, IPv4 first, then longest first, and with minimal=true only those with no such prefix inside them
    GET /ip/{addr} returns request_count, the window_count of the most recent HotIpAdded the trie applied for the address: its count at transition time, not a live count, and 0 while the address is COLD
    GET /ip/{addr} returns matched_prefixes for the address's ancestors with 24, 16 and 8 host bits: /8, /16 and /24 for IPv4, /104, /112 and /120 for IPv6

What gets no line (assumption 108): `prefix_queries`, since `/metrics`
renders nothing until the telemetry epic; `evaluate_prefix_state` and
`create_app`'s signature, which nothing outside the repository calls.

### Test seams

* **The app on its own.** `create_app(readiness, state=..., metrics=...,
  min_prefix_lengths=...)` needs no bus and no worker. A test builds a
  `TrieState`, writes it with the worker's own calls — `apply_hot_ip_added`
  or `apply_hot_ip_removed` on `state.of(family)`'s trie and records, then
  `state.note_applied(offset, timestamp)` — marks a `Readiness` ready
  itself, and sends requests through `httpx.ASGITransport`. The worker is
  the only production writer (decision 2), and a test is not production.
* **The configuration.** `state.adopt_config(config)` changes the document
  in force between two requests. A `DetectionConfig` built in the test may
  carry a `minimum_hot_ips` of 0, which the schema forbids and neither the
  loader nor the model refuses. It shows that only prefixes holding a HOT
  address are listed.
* **The service.** `TrieService.app` serves the same routes. A test
  publishes hot-ip events to an `InMemoryBus`, awaits `start()`, and reads
  what the replay applied; `build_service`'s floors reach the app as they
  reach the worker.
* **The forbidden state.** `records.discard(address)` on a HOT address
  leaves the state §46.5 forbids. Through `httpx.ASGITransport`, whose
  default re-raises an application's exception, a request for `GET
  /ip/{addr}` then raises `InvariantViolation` in the test.
* **Independent expectations.** A test can compute a list of HOT prefixes
  from the addresses it made HOT and §3's formulas, with exact fractions,
  without calling into the trie.
* **What no test can see.** R2 and R3 are structural: a handler with no
  `await` cannot interleave with the writer. The reviewer checks them. No
  test measures time (ADR-0014 assumption 17).

### Questions this amendment closes

| Question | Left open by | Ruled in |
| --- | --- | --- |
| How does `GET /prefixes/hot` avoid a full scan per request? | decision 15 item 6 | ruling 5: a walk that descends only where a prefix could qualify; no index |
| Which lengths does `GET /prefixes/hot` cover? | decision 15 item 6 | ruling 2: each served family's `[L_F, bit_length]` |
| What does a query for a family not served answer? | decision 15 item 6 | ruling 2: `400`, `address family not served` |
| Which lengths are IPv6's `matched_prefixes`? | decision 15 item 6; ADR-0010's assumption on `matched_prefixes` | ruling 2: `/104`, `/112`, `/120` |
| What are `prefix_queries`' labels? | decision 12; decision 15 item 6 | ruling 9 |
| Does the trie cache `prefix_state`? | decision 10; ADR-0010 decision 5; ADR-0014 assumption 21; ADR-0015 Consequences | ruling 5: no |
| Does the read API need prefix metadata? | ADR-0015 assumption 7; decision 15 item 4 | ruling 6: no |
| What do the read routes answer before the trie is ready? | §47.2; `read-api-v1.md` | ruling 3 |
| Should the trie's port be reachable beyond its host? | ADR-0013 assumption 13; Consequences | not ruled: with the owner (ruling 7) |

### Edits

**In this ADR.** Each is a dated note; no text is replaced.

* **Status.** A paragraph on this amendment, after Amendment 3's.
* **Decision 1.** A note after its Amendment 3 note: what else slice 3's
  change covers.
* **Decision 2.** A note after its Amendment 2 note: `create_app`'s keywords
  and the imports.
* **Decision 9.** A note after its Amendment 2 note: R2 and R3 on the read
  routes, and `GET /prefixes/hot`.
* **Decision 10.** A note after the "Slice 3." bullet: no cache.
* **Decision 12.** A note after its Amendment 3 note: `prefix_queries`, and
  no record.
* **Decision 13.** A note after its Amendment 3 note: the service's
  `create_app` call.
* **Decision 15.** A note after item 6: item 6 settled, and item 4
  standing.
* **Test seams.** A note after its Amendment 3 note.
* **Consequences.** A note under the `CHANGES` bullet, and one under the
  "Security posture" bullet.
* **This section.**

**In other documents.** No wording is replaced, except in
`docs/spec/README.md`'s index, whose cells gain entries and are quoted
below.

* **`docs/adr/0010-read-apis-and-shared-prefix-predicate.md`** (Amendment
  5): a clause at the end of the status line; dated notes after decision
  1's last paragraph, after decision 2's metrics block, after decision 4's
  2026-09-23 note, after decision 5's second paragraph, under the
  assumption "`matched_prefixes` reports IPv4 lengths 8, 16 and 24.", and
  under the Consequences bullet on `schemas/prefix_stats_event.v1.json`; an
  "Amendment 5" section.
* **`docs/adr/0015-prefix-metadata-inheritance-and-the-attribute-side-map.md`**:
  italic dated notes at the end of assumption 7, of the Consequences bullet
  "**The query epic**" and of the Consequences bullet "**Open, and
  deliberately not settled here:**"; a dated blockquote after Amendment 5
  ruling 11. No status clause and no amendment section (assumption 110).
* **`docs/protocol/read-api-v1.md`**: dated blockquotes after the
  preamble's first paragraph, after the trie's `as_of` paragraph, after the
  paragraph that begins "Every domain endpoint below answers exactly like
  `GET /readyz`", after the first paragraph of `GET /prefix/{cidr}`, after
  the first paragraph and after the `matched_prefixes` bullet of `GET
  /ip/{addr}`, after the paragraph of `GET /prefixes/hot[?minimal=true]`,
  and after the paragraph of "Errors" (assumption 109).
* **`docs/spec/README.md`** (assumption 112). Five rows gain entries; none
  loses one.
  * §13, 38: was "`core/state/prefix.py`, `services/detector/rules/baseline.py`,
    `services/trie/query`, `docs/adr/0010`", and gains "`docs/adr/0017`
    (Amendment 4 ruling 1: the predicate's arguments)".
  * §13, 14, 31: was "`services/detector`", and gains "`services/trie/query`
    (`GET /prefixes/hot`, `?minimal=true` for §31), `docs/adr/0017`
    (Amendment 4 rulings 2 and 5)".
  * §29: its ADR-0017 entry was "`docs/adr/0017` (decisions 9 and 15)", and
    gains "Amendment 4: the read API as designed".
  * §37: its ADR-0017 entry ended "Amendment 3 ruling 1: `replay_complete`'s
    `republish_from` and the `prefix_stats_last_ignored` record", and gains
    "Amendment 4 ruling 9: `prefix_queries`".
  * §47: its ADR-0017 entry was "`docs/adr/0017` (decisions 4, 11 and 13:
    the trie's settings, readiness, admin routes and drain)", and gains
    "Amendment 4 ruling 3: the read routes answer 503 until the trie is
    ready".

No spec section, schema or other protocol document changes.

### Assumptions

Each is a judgment call that decision 15, the spec and the earlier ADRs do
not make. Push back on them individually; numbering continues the ADR's
list.

87. **The `path` convertor for both parameters.** `{cidr}` needs it, since
    an IPv4 CIDR holds a `/`. `{addr}` takes it as well, so that
    `/ip/10.0.0.1/32` meets the read API's `400` rather than the
    framework's `404`: every request under `/prefix/` or `/ip/` is answered
    by the route.
88. **The length grammar is stricter than `Prefix.parse`, and the address is
    exactly `Address.parse`.** `Prefix.parse` reads a missing length as the
    host route, and hands the length to `int()`, which accepts a sign,
    surrounding whitespace, underscores between digits and non-ASCII
    decimal digits (Python's documented behaviour, not run here). A read
    API asked about a prefix should be given the whole of it, so the length
    must be one to three ASCII digits. Leading zeros are allowed, since the
    grammar counts digits, and the answer names the canonical text. The
    address part is left to `Address.parse`, so the read API resolves an
    address text as ingest does. That includes whatever
    `ipaddress.ip_address` accepts beyond the dotted and colon forms; an
    IPv6 scope such as `fe80::1%eth0` is one such form in the Python this
    repository targets, and `Address.parse` drops it. That is recalled from
    Python's documentation and was not re-read for this amendment; the
    audit confirms it (ruling 10).
89. **The order of the checks is fixed, and so are the six texts.** The
    order gives every input one answer. The texts are fixed so that a client
    may show them and a test can pin that nothing is echoed. That makes
    their wording part of this contract, unlike the exception messages of
    ADR-0016 or of ruling 1, which no client reads.
90. **A family not served is `400`, with a text of its own.** Decision 15
    recommended `400`. A zero-valued answer would claim that nothing is hot
    where the trie knows nothing. `404` stays with paths the app does not
    serve, and `422` is FastAPI's validation shape, which `read-api-v1.md`
    does not use. A text of its own lets an operator tell a deployment
    choice from a typing mistake.
91. **IPv6 `matched_prefixes` are `/104`, `/112` and `/120`,** the ancestors
    holding as many addresses as IPv4's `/8`, `/16` and `/24`: Amendment 2
    ruling 1's reasoning for `/104`. The allocation boundaries `/32`, `/48`
    and `/64` were the alternative. Under the v1 predicate they are always
    `NORMAL` (Amendment 2 ruling 1), which ADR-0010's assumption on
    `matched_prefixes` counts as noise. A query parameter can add lengths
    later without changing the default.
92. **`GET /prefixes/hot` covers `[L_F, bit_length]`.** Decision 15
    recommended it, so that the trie's list and the detector's, which hears
    only those lengths, can agree. A prefix shorter than `L_F` that
    qualifies — possible only under a document with a tiny
    `minimum_hot_ratio` — is answered by `GET /prefix/{cidr}` and not
    listed.
93. **Only prefixes that hold a HOT address are listed.** `read-api-v1.md`
    said "every node", and a prefix with no HOT address has none. The rule
    decides something only under a document whose `minimum_hot_ips` is
    below 1. The schema forbids that, but neither the loader nor
    `DetectionConfig` refuses it. `evaluate_prefix_state` then calls an
    empty prefix `HOT_PREFIX` when `minimum_hot_ratio` is 0 as well, and no
    list could hold every such prefix. `GET /prefix/{cidr}` still reports
    what the predicate says.
94. **IPv4 before IPv6, and no family parameter.** Any fixed order would
    do. Grouping by family keeps each family's part in `read-api-v1.md`'s
    order. The detector's list, which `read-api-v1.md` orders "same as the
    trie's list", follows the same rule (ADR-0010 Amendment 5).
95. **`minimal` is exactly `true` or `false`, given at most once; other
    parameters are ignored.** `true` and `false` are how JSON writes
    booleans, and how httpx encodes them in a query (Sources). Accepting
    `1`, `yes` or `True` as well would make the contract whatever FastAPI's
    parser accepts. Ignoring unknown parameters is the common practice, at
    the price that a misspelt `minimal` returns the full list.
96. **The gate comes before the parse.** §47.2 has a domain endpoint answer
    `503` while the service is not ready, whatever it was asked. Gating
    first also keeps a service that is not ready from doing any work for a
    request.
97. **The walk's figures are estimates.** They count point queries, and
    take a few microseconds for each query and each evaluation in CPython.
    Nothing was measured. The integration job (#52) is where to measure
    them.
98. **No index, and no cached `prefix_state`, in v1** (ruling 5). The
    writer-side cost of an index is estimated in the same way: about 25
    re-evaluations and one ancestor walk for each state-changing event,
    tens of microseconds, against a replayed record that costs about as
    much to decode and apply. That estimate carries the first reason, and
    it was not measured either.
99. **The walk reads `hot_count` point by point, and not the arena.** A
    depth-first walk over `PatriciaTrie.arena` would be faster by a
    constant. It would copy ADR-0014 decision 6's traversal outside the
    structure package and bind the read path to the Patricia
    representation, while ADR-0014 decision 5 names the arena's readers as
    the invariant checks, `tools/trie-inspect` and the structure's tests.
    `HotTrie` is the surface later epics may rely on (ADR-0014 decision 2).
    A pruned iterator added to that surface is the way to the faster walk,
    and is left to an amendment of ADR-0014.
100. **`evaluate_prefix_state` checks its arguments.** ADR-0010 decision 1
     gave the comparison, not its domain. Outside `capacity >= 1` and `0 <=
     hot_count <= capacity` the ratio means nothing, and `Fraction(n, 0)`
     would raise `ZeroDivisionError`. The detector will pass it values from
     the wire. `TypeError` for a wrong type and `ValueError` for a wrong
     value follow ADR-0015 assumption 44. A `bool` is refused because it is
     not a count.
101. **A HOT address with no record is a `500`.** The route could answer
     `HOT` with no `attributes`, breaking `read-api-v1.md`'s "present iff
     HOT", or answer with the default document, reporting a record nobody
     stored. Raising names the broken invariant instead. The read path does
     not own recovery: corruption is diagnosed by the worker's mutators and
     `tools/trie-inspect --verify` (ADR-0014 A12). Only a test that corrupts
     the state reaches the case.
102. **No authentication, no rate limit, no memo and no lock** (ruling 7).
     Each would be a mechanism sized for a threat whose size the exposure
     question decides, and that question is the owner's. A deployment that
     exposes the port has standard means to put in front of it.
103. **No record from the read routes, and uvicorn's access record left as
     it is.** Turning the access record off for the trie alone would set it
     apart from ingest's and the aggregator's servers, which run uvicorn
     the same way, and the record is an operator's only trace of who asked
     what. Its content is the request line, quoted as ruling 9 says.
104. **`prefix_queries` counts all three routes, by route and result.** §37
     names the series and nothing more. One series for the read API's load,
     split by `route`, is of more use than a count of `GET /prefix/{cidr}`
     alone. `result` tells refusals from answers without making the status
     code a label.
105. **`create_app` takes the state, the metrics and the floors as required
     keywords.** A default floor would need `publisher`'s constant, which
     `query.app` must not import (decision 2 as amended). Requiring an
     entry only for the families served spares a test that holds IPv4 alone
     from naming IPv6.
106. **Bodies are Starlette `JSONResponse`s built from plain data.**
     Returning a `dict` would run FastAPI's encoder, and a response model
     its validation, over data the route already controls. Starlette's
     rendering is compact, UTF-8 and refuses a non-finite number, and
     nothing the trie stores holds one: ADR-0015's validator refuses them.
107. **`as_of` is rendered as the codec renders timestamps.** One form
     across the event schemas and the read API. The read path writes the
     form itself, since the codec's formatter is private.
108. **`CHANGES`: four lines, none `BREAKING`, and none for the counter,
     `evaluate_prefix_state` or `create_app`.** The third line is ADR-0015
     Amendment 5 ruling 11's: it says that `request_count` is not a live
     count. The fourth records IPv6's lengths, which are new.
109. **Dated notes in `read-api-v1.md`, not edits in place.** The dispatch
     asked for dated notes in merged text. This ADR's first change set and
     ADR-0015 Amendment 5 had edited that document in place. Its readers
     now meet each new rule beside the text it refines.
110. **Pointer notes in ADR-0015, with no status clause and no amendment
     section,** as ADR-0015 assumption 83 and this ADR's assumption 39 did:
     no ruling of ADR-0015 changes. ADR-0010 gains Amendment 5, as it gained
     Amendments 3 and 4, because its decision 4 gains rulings.
111. **The minimal set reaches `/28`, and `integration-scenarios.md` is not
     edited here.** Under the default document a fully HOT `/28` qualifies,
     so §42's 156 addresses have eight `/28`s as their most specific
     qualifying prefixes, not the `/24` §42 calls a candidate. ADR-0010's
     assumption "`BOT_NETWORK` unused in v1" treats §42's wording as
     descriptive, and this amendment follows `read-api-v1.md`'s definition.
     Whether §42 should be read otherwise is raised for the repository owner
     with this amendment, and is not ruled. `docs/spec/integration-scenarios.md`
     §4 step 2, §5 step 7 and §6 steps 2 and 3 expect lists that assume no
     prefix below `/24` qualifies. Those scenarios are #52's, and are
     reported with this amendment's hand-off rather than edited here.
112. **`docs/spec/README.md` gains entries** in the five rows whose mapping
     this amendment extends, as each amendment of this ADR has done, and
     loses none.

### Sources

Read on 2026-09-24 for this amendment. No web source was consulted.

* FastAPI 0.141.1 as installed (`uv.lock`),
  `.venv/lib/python3.12/site-packages/fastapi/routing.py`: a route added
  with `@app.get` gets the methods `{"GET"}` (`methods = ["GET"]` when none
  are given), so `HEAD` answers 405; `APIRoute.matches` calls
  `self.path_regex.match(route_path)`; the request handler passes a returned
  `Response` through and serializes anything else; `run_endpoint_function`
  awaits a coroutine endpoint and runs any other in `run_in_threadpool`.
  Taken from it: rulings 2, 4 and 10.
* Starlette 1.6.0 as installed: `starlette/convertors.py`,
  `PathConvertor.regex = ".*"`; `starlette/routing.py`, `compile_path`
  builds the pattern from `"^"`, the escaped literal parts and each
  convertor's group, and ends it with `"$"`. Taken from it: the `path`
  convertor, and ruling 10's routing note, which also rests on Python's
  `re` semantics for `.` and `$` as recalled, not re-read.
* uvicorn 0.53.0 as installed: `uvicorn/protocols/http/h11_impl.py` sets
  the scope's path to `unquote(raw_path.decode("ascii"))`, and logs the
  access record `'%s - "%s %s HTTP/%s" %d'` over the client, the method,
  `get_path_with_query_string(scope)`, the version and the status when the
  access logger has handlers; `httptools_impl.py` does the same;
  `uvicorn/protocols/utils.py`'s `get_path_with_query_string` percent-quotes
  the path with `urllib.parse.quote`; `uvicorn/config.py` configures the
  `uvicorn.access` logger only from a `log_config`. h11 as installed,
  `h11/_abnf.py`: `request_target = r"{vchar}+"` with `vchar =
  r"[\x21-\x7e]"`. Taken from it: rulings 9 and 10.
* httpx as installed, `httpx/_utils.py`: `primitive_value_to_str` writes
  `True` as `"true"` and `False` as `"false"`. Taken from it: assumption 95.
* Repository files: `packages/hammertime-core/src/hammertime/core/addressing/address.py`
  (`Address.parse` wraps `ipaddress.ip_address`) and `prefix.py`
  (`Prefix.parse` reads a missing length as `bit_length` and converts the
  length with `int()`); `core/config/loader.py` (types checked, the
  schema's minimums not) and `models.py` (no check on `minimum_hot_ips`);
  `core/events/codec.py` (`_format_timestamp`); `core/events/attributes.py`
  (S5 and the refusal of a non-finite number); `core/runtime.py`
  (`Readiness`, `ServiceNotReady`, `not_ready_response`);
  `services/trie/src/hammertime/trie/` `query/app.py`, `service.py`,
  `state.py`, `metrics.py`, `structure/patricia.py` and `metadata/`;
  `deploy/docker-compose.yml` and `deploy/prometheus.yml`;
  `schemas/detection_config.v1.json` (`minimum_hot_ips` has `minimum` 1);
  `docs/spec/integration-scenarios.md`.
