# ADR 0017 — The trie service: a positional-replay worker on one event loop, the log position as `event_sequence`, and the slices of epic #10

Status: accepted 2026-09-23 (epic #10). It amends three earlier ADRs, each
at a dated note in place plus a short amendment section listing its notes:
ADR-0001 (Amendment 4), ADR-0010 (Amendment 2) and ADR-0013 (Amendment 9).
Spec §22's note is reworded, and §28, §33 and §35 gain notes.
`docs/protocol/read-api-v1.md`, `docs/spec/integration-scenarios.md` §5,
`docs/runbook.md` and `docs/spec/README.md` are edited in step. "Edits to
other documents" at the end lists every edit and quotes what it replaced.

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
| 1 | `config.py`, `state.py` (new), `metrics.py` (new), `worker.py`, `service.py` (new, ADR-0009 decision 3), `__main__.py`, `query/app.py` (the three admin endpoints only); `.env.example`; `CHANGES` | — |
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
    def position(self) -> int | None: ...                     # offset of the last hot-ip record handled; None before the first
    @property
    def event_sequence(self) -> int: ...                      # 0 if position is None else position + 1 (decision 8)
    @property
    def as_of(self) -> datetime | None: ...                   # the newest timestamp among applied events (decision 8)
    def note_handled(self, offset: int) -> None: ...
    def note_applied(self, offset: int, timestamp: datetime) -> None: ...
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
* **What both refuse.** An `offset` below 0, or not greater than
  `position`, is a `ValueError`, and nothing changes. `note_applied` also
  refuses a naive `timestamp` in the same way.
* **`adopt_config(config)`** replaces `config`. It compares no versions
  (decision 10).
* **Who writes it.** The worker is the only production caller of the
  three mutators. The snapshot epic restores a `TrieState` by its own
  means (decision 16), and nothing else writes one.

### 3. Consumption: one positional, whole-topic subscription, never acknowledged

* **The consumer.** The worker takes its consumer as
  `bus.consumer(CONSUMER_GROUP)`, where `CONSUMER_GROUP =
  "hammertime-trie"` — the name ADR-0009 decision 9 gives the trie. A
  positional subscription creates no durable, so the name reaches no
  broker (ADR-0010 Amendment 1 ruling 1).
* **The subscription.** It is `subscribe(HOT_IP.name, start_offset=S)`,
  with `partitions=None` and no listener:
  * `S` is `0` when `state.position` is `None`, and `state.position + 1`
    otherwise;
  * a fresh `TrieState` has no position, so in slice 1 every start
    replays from the first record the log retains (`start_offset=0`,
    ADR-0013 decision 9 as amended);
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
2. It reads `end = await bus.end_offset(HOT_IP.name)` *before*
   subscribing, and keeps it as `replay_target`.
3. It subscribes (decision 3).
4. It takes the next message and awaits `handle()` on it, until the
   worker is *caught up*. Caught up means `end <= S`, or
   `state.event_sequence >= end` (which is `state.position >= end - 1`).
   * If the iterator ends before that, `start()` raises `RuntimeError`.
   * If `stop()` has begun, `start()` returns without being caught up.
5. It logs `replay_complete` (decision 12).

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
| `bus.end_offset`, `subscribe` | any exception | Propagates out of `start()`: `start_failed`, exit 1. Not retried: the bus answered a moment before, at `NatsBus.start()`. |

The worker catches no bare `Exception` anywhere.

### 8. `event_sequence` is the trie's position in the log; `as_of` is the newest applied event time

**The definitions.**

* `position` is the offset of the last hot-ip record the worker has
  *handled* — a record whose outcome was `APPLIED`, `UNCHANGED`,
  `MALFORMED` or `FAMILY_NOT_SERVED`. It is `None` before the first.
  `REDELIVERED` and `STOPPED` do not move it, and neither does an
  `InvariantViolation`.
* `event_sequence` is `0` when `position` is `None`, and `position + 1`
  otherwise: the offset of the next record the trie will read.

This answers ADR-0010 Amendment 1 ruling 3: the trie's `event_sequence`
and the log's stream sequence are one number. It is what every read
response and every `PrefixStatsChanged.sequence` carries. The snapshot's
`replay_position` is `position`. ADR-0010 Amendment 1 ruling 2 said
"applied"; a skipped record is handled too, and need not be read again.

**Who decided.** The repository owner decided on 2026-09-23, on this
ADR's recommendation, that the trie's `event_sequence` is its log
position and not a count of applied events. The reasons below are that
recommendation's. The form the position takes here — `position + 1`,
with `position` counting handled records — is this ADR's own
(assumption 9). ADR-0010 Amendment 1 had raised the question for the
owner (its assumption "Two numbers rather than one"), while ADR-0013
decision 9 left it to the trie epic. The owner's decision makes that
disagreement moot.

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

  A log position re-derives the same value for the same record wherever
  the replay starts, as long as the stream exists.
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
0, and on JetStream, whose sequences start at 1. Only `read-api-v1.md`'s
preamble promised a client a count, and it is edited here.

**Why `position + 1` and not `position`.**

* `0` then means "nothing handled" on both buses.
* It is `end_offset`'s own convention — the next offset to read — so
  readiness is `event_sequence >= end_offset` (decision 4).
* On the memory log it equals the number of records the trie has passed.
  That keeps `docs/spec/integration-scenarios.md` §5's `256` and `257`
  exact.

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
  (decision 6, steps 2-5) and in `apply_config()`. For one event, no
  `await` separates the first write from the last. The first is
  `apply_hot_ip_added` or `apply_hot_ip_removed`, both synchronous
  functions (ADR-0015 decision 6); the last is `note_applied`.
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
| `replay_complete` | info | `start_offset`, `replay_target`, `event_sequence` |

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
  * `handle()` returns `STOPPED`, and `apply_config()` changes nothing;
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
* **`__main__.main()`** is `raise SystemExit(run_service(SERVICE_NAME,
  lambda: build_service(load_settings())))`, in ADR-0009 decision 1's
  form.
* **`snapshot_now()`** (ADR-0009 decision 3's table) belongs to the
  snapshot epic. `Service` does not require it.

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
* **The compose file.** `deploy/docker-compose.yml` mounts
  `trie-snapshots` at `/snapshots`, while `.env.example`'s `./snapshots`
  resolves to `/app/snapshots` in the image. The compose service must set
  `HAMMERTIME_TRIE_SNAPSHOT_DIR=/snapshots`.
* **A restored document that fails validation** is ADR-0015 decision 8's
  question. It is counted as `attributes_rejected{stage="snapshot"}`.

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
   ones included, and `event_sequence` is `position + 1`.** That
   `event_sequence` is the log position at all, and not a count, is not
   an assumption: the repository owner decided it on 2026-09-23, on this
   ADR's recommendation (decision 8). The form is this ADR's. "Handled"
   rather than "applied" means a skipped record is not read again after a
   restore, and readiness can pass a malformed record at the tail of the
   log. `position + 1` rather than `position` makes `0` mean "nothing
   handled" on both buses and matches `end_offset`'s convention
   (decision 8).
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
19. **The readiness residuals are recorded, not closed.**
    * If the record at `end_offset - 1` is one the transport skips,
      `start()` waits for the next record, and fails the start if none
      comes in time. A malformed subject is such a record; only a
      principal publishing to the stream directly can produce one
      (ADR-0013 assumption 13).
    * Records purged between reading the log end and replaying it are
      handled the same way. A purge keeps the stream's last sequence
      (Sources), so the next record's offset is past the old end.

    Closing either needs a bus signal such as "nothing pending", which
    `hammertime.bus` does not have. That is a follow-up for the bus, not
    the trie.
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

## Consequences

* **Slice 1 lets the trie start.** The container starts under
  `docker compose` and reports ready once its replay reaches the log end.
  That is the trie's share of the `integration` job's first re-enable
  condition (#52; `CLAUDE.md`, "Disabled CI coverage").
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
* **The detector epic can rely on the trie's `event_sequence`** never
  going backwards across trie restarts, while the stream exists
  (decision 8).
* **`CHANGES`.** Slice 1's implementing change adds three lines. None is
  `BREAKING`, because no trie build has shipped:
  * `Add trie service: consumes hammertime.hot-ip.v1 and applies each HotIpAdded/HotIpRemoved to the trie and the address's attribute record in one step, serving GET /healthz, GET /readyz and GET /metrics on HAMMERTIME_TRIE_QUERY_BIND (default 0.0.0.0:8081)`
  * `Trie service replays hammertime.hot-ip.v1 from the oldest retained record on every start and reports ready only once the replay has reached the log end as it stood at startup; a replay that outlasts HAMMERTIME_STARTUP_TIMEOUT_S fails the start`
  * `Add HAMMERTIME_TRIE_FAMILIES (default ipv4): the address families the trie service holds; a hot-ip event for any other family is skipped and counted`
* **Security posture.**
  * The trie trusts `hammertime.hot-ip.v1`, which is inside the boundary
    ADR-0013 assumption 13 states.
  * It bounds nothing a principal on the bus could send (assumption 20).
  * It logs nothing a principal on the bus wrote (decision 12).
  * Slice 1 exposes no domain endpoint. The compose file publishes the
    admin port 8081 on every interface; ADR-0013 Amendment 4 leaves that
    question with the owner.

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
