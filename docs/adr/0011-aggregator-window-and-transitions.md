# ADR 0011 — Aggregator windows: live buckets, observation disposition, transition emission, and re-evaluation

Status: accepted

Scope note: this ADR fixes the interfaces of the aggregator's domain modules
for milestone M3 (issues #5, #6, #48) — `services/aggregator/.../window/`,
`lateness.py`, `transitions.py`, `reevaluate.py`, `worker.py`, `config.py`
— and the one core function they share with every other consumer
(`threshold_ratio`, §46.4). It does not define the aggregator *process*
(`__main__.py`, `service.py`, readiness, HTTP admin endpoints: ADR-0009),
shard ownership (§20, issue #7), or a durable window store; those are named
in "What this ADR does not cover" so that nobody mistakes their absence for
an oversight. Normative summary: pointer notes added to spec §5, §24, §26,
§34 and §46.4.

Revision note (2026-09-17): a second pass closed eight gaps that surfaced
while the M3 test suites were written against §9 — the `EXPIRED` row of
decision 2 (bucket liveness, not raw age), the `shard_ids` default
(derived from `shard_count`), `get_or_create`'s treatment of
`last_observed`, `rebucket`'s effect on the store's own geometry, empty
environment values, malformed `HAMMERTIME_AGGREGATOR_BIND`, the recorded
shape of `StateTransition`, and the payload `sequence` on a published
transition. Nothing else changed; each new judgement call is listed under
"Assumptions".

## Context

Spec §5 says the window SHOULD be fixed-size buckets with a maintained
running total, §24 that a bounded-lateness policy SHOULD be used, §25 how a
bucket start is computed, §26 that cold state SHOULD expire, §30/§39 the
per-observation algorithm, and §34 that a configuration change MUST define
its effect on existing state. ADR-0002 chose event time and the
`allowed_lateness` horizon; ADR-0003 chose deltas and made ingest the only
dedup point; ADR-0004 made every `hammertime.observations.v1` message a
single-IP `RequestObservation`; ADR-0005/§46.4 defined `weight` and named
`threshold_ratio` without saying where it lives; ADR-0009 decision 3 named
`run_maintenance()` and `reload_config()` and decision 6 the version rule;
ADR-0010 decision 6 put an observation's whole delta in the one bucket
containing `window_start`.

What none of them say, and what a test cannot be written without:

1. **When exactly is a bucket "in the window"?** §5's example (300 s / 30 ×
   10 s) does not say whether the edge is judged against the clock or the
   newest observation, nor whether the boundary is inclusive.
2. **What happens to an observation that passes the lateness gate but whose
   bucket has already left the window?** `is_within_lateness`
   (`core/time/buckets.py`, M1) accepts an age up to
   `window_seconds + allowed_lateness_seconds` (330 s by default) while the
   window is only 300 s wide, so a 30-second zone exists where an observation
   is neither late nor useful.
3. **What does the aggregator emit, with which identity fields?** ADR-0003's
   amendment says `agent_id` is the producing shard and `sequence` the
   producer's own counter, but not the format, the counter's scope, or what
   happens on restart.
4. **What does a configuration change re-evaluate?** §34 says lowering
   `hot_threshold` mass-transitions IPs; it does not say what a change to
   `window_seconds` or `bucket_seconds` does to counters that already exist.
5. **Where does `threshold_ratio` live?** It is computed by the aggregator
   and, per §46.4, recomputable by any consumer.
6. **Can M3 close without the ADR-0009 runtime?** #5 lists `__main__.py`
   among its modules, and `main()` is a three-line adapter over
   `hammertime.core.runtime.run_service`, which does not exist.

Existing state on `master` (verified, not assumed): `core/state/{enums,
machine,transitions}.py` are implemented — `evaluate_ip_state` is the single
§6/§7 comparison and is *not* reimplemented here. Every aggregator module
named above has zero `def`/`class`. `threshold_ratio` exists nowhere.
`hammertime-store` defines only `DedupStore`; its `interface.py` explicitly
leaves the window-counter protocol to this epic.

## Decision

### 1. A bucket is live while `S <= now < S + window_seconds`, judged against the service clock

For bucket size `B = bucket_seconds`, window `W = window_seconds`
(`W % B == 0`, enforced by `DetectionConfig`), and the injected `Clock`'s
`now` (UTC epoch seconds, §25):

```text
S            = bucket_start(window_start, B)          # §25, core/time/buckets.py
live(S, now) = S <= now < S + W
window_count = sum(count[S] for every live S)          # exactly W // B buckets at most
```

A bucket therefore expires at `now == S + W`, not later; the newest live
bucket is `bucket_start(now, B)`, so the window always includes the current,
still-filling bucket. With the defaults, at `now = 1000` the live buckets
are `710, 720, …, 1000` — thirty of them — and `700` has just expired. This
is the reading under which `docs/spec/integration-scenarios.md` §2.4's
"certainly left the window once `now >= S + W + B`" is true, and under
which its `advance(310)` steps drive `HOT -> COLD`.

Consequences for the per-IP counter (`window/counter.py`, decision 9):

* `total` is maintained incrementally (`+= delta` on observe, `-= count` on
  expiry) and read in O(1); the buckets are never summed on read (§5).
* At most `W // B` buckets are retained per IP. Whether that is a ring
  indexed by `bucket_index` or a small map keyed by `S` is the
  implementer's choice; the ring is the natural fit because a live bucket
  `S` and the expired bucket `S - W` share a slot exactly when the former
  replaces the latter.
* An observation may land in *any* live bucket, not only the newest
  (ADR-0002); out-of-order delivery within the window is the normal case,
  not an error path.
* `expire(now)` is idempotent and is run before every `observe`, so the
  total is correct at the moment a state decision is taken.

### 2. Every consumed observation is either applied to the window or published to the reconciliation topic — never both, never neither

`lateness.py` classifies an observation from `window_start` (as an epoch
second) and `now`, in this order:

| Disposition | Condition | Effect |
| --- | --- | --- |
| `FUTURE` | `window_start > now` | not applied; published to `hammertime.observations-reconciliation.v1`; `future_messages += 1` |
| `LATE` | `not is_within_lateness(window_start, now, W, L)` i.e. `now - window_start > W + L` | not applied; published to reconciliation; `late_messages += 1` (§37) |
| `EXPIRED` | `is_within_lateness(window_start, now, W, L)` holds but `not live(S, now)`, where `S = bucket_start(window_start, B)`: the bucket containing the observation has already left the window | not applied; published to reconciliation; `expired_on_arrival += 1` |
| `APPLY` | otherwise: `live(S, now)` | applied to bucket `S` in full (ADR-0010 decision 6) |

`EXPIRED` versus `APPLY` is judged on the **bucket** `S`, never on the raw
age of `window_start`. `FUTURE` and `LATE` remain raw-age tests on
`window_start` itself (the lateness horizon is a policy on event age, not
on bucket geometry). For an aligned `window_start` — the only kind ingest
publishes (`docs/protocol/observation-v1.md`: unaligned windows are
rejected with 400) — the two readings coincide and `EXPIRED` is exactly
the age band `W <= now - window_start <= W + L`. For an unaligned
`window_start` they diverge and the bucket rule wins: at `now = 1000`,
`W = 300`, `B = 10`, `window_start = 705` has raw age 295 but `S = 700`
and `700 + 300 <= 1000`, so it is `EXPIRED`; `window_start = 715` has
`S = 710`, which is live, so it is `APPLY`. This is not a free choice:
decision 4 applies an `APPLY` observation with `counter.observe(S, …)`,
which raises `ValueError` for a dead `S`, so an age-based `classify` would
turn a valid observation into a crash on the per-observation path. The
four dispositions remain total and disjoint under this rule: a live `S`
implies `now - window_start < W`, so nothing is both `LATE` and live.

"Published to reconciliation" means the *original* consumed bytes under the
*original* key are republished unchanged, so the reconciliation consumer
sees the same envelope, `event_id` and all, that the aggregator declined.
Nothing is silently dropped (§24, ADR-0002, #5's acceptance criterion).

`allowed_lateness_seconds` therefore does **not** widen what is counted: a
bucket that has left the window cannot contribute to `window_count`
whatever the policy says. Its load-bearing role is ingest's dedup TTL
(`W + L`, ADR-0003): because the aggregator's acceptance horizon equals the
dedup retention, every observation that is applied was necessarily
dedup-checked. In the aggregator it only separates `late_messages` from
`expired_on_arrival`. Widening the counted horizon would change §5's window
definition and needs its own ADR; it is not done here.

`FUTURE` is checked before `LATE` only so the metric names the cause;
`is_within_lateness` already rejects a negative age.

### 3. The window store is in-process, keyed by `Address`, and bounded by retention plus a hard cap

`window/store.py` defines `InMemoryWindowStore` (decision 9 has the
signatures). One `IpEntry` per tracked IP: its `IpCounter`, its `IpState`,
and `last_observed` — the service-clock time at which an observation was
last *applied* (not the observation's event time, and not refreshed by an
observation that was diverted to reconciliation).

`last_observed` is written in exactly two places. `get_or_create(ip,
now=)` sets it to `now` when it **creates** an entry, so a brand-new entry
is never immediately evictable; on an **existing** entry `get_or_create`
does not touch it — the `now=` argument is used only for creation. The
worker sets it after a successful `counter.observe` (decision 4). Nothing
else refreshes it: a lookup, a re-evaluation, a `rebucket`, an expiry sweep
or a diverted observation all leave it alone. The cap-eviction victim and
the retention check below therefore both read "time of the last applied
observation, or creation if none was ever applied".

Retention (§26, `expiry.py::evict_idle`): an entry is evicted when
`state is COLD and now - last_observed >= state_retention_seconds`. HOT
entries are never evicted by retention: an IP that goes quiet first has its
buckets expire, is re-evaluated to COLD on a maintenance sweep (emitting
`HotIpRemoved`), and is evicted on a later sweep. Because
`state_retention_seconds >= window_seconds` is enforced by
`DetectionConfig`, an entry that qualifies for eviction always has an empty
window, so eviction never discards a count that could still matter. (If
`cold_threshold == 0` a HOT IP can never satisfy `count < 0` and stays HOT
forever; that is §6 read literally, and such an entry is simply never
evicted.)

Hard cap (`max_tracked_ips`, settings key
`HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS`, default 1 000 000): `get_or_create`
on a full store evicts the COLD entry with the oldest `last_observed` before
inserting; if no COLD entry exists it raises `StoreFullError`, which the
worker maps to a reconciliation publish and `observations_rejected += 1`.
This is defence in depth of the same kind as `MemoryDedupStore.max_agents`
— retention alone bounds memory only by observation rate × retention, which
an authenticated agent controls up to ingest's rate limits (ADR-0008).

`tracked_ips` (`len(store)`), `hot_ips` and `active_ips` (entries whose
`window_count > 0`) are the §37 sliding-window gauges. `tracked_ips` is
O(1); the other two may be O(tracked_ips) and are for metric scrapes, not
the per-observation path.

**No durable store in M3.** `window/store.py`'s original docstring
anticipated "Redis when state must outlive the process". That is deferred,
deliberately, because it is a recovery-story decision and not a storage
decision: an aggregator that restarts with an empty store believes every IP
is COLD, so an IP the trie still holds as HOT will never receive a
`HotIpRemoved` unless the aggregator either (a) rehydrates HOT state from
`hammertime.hot-ip.v1` on start, or (b) keeps its state in a store that
survives the process. Either choice also requires the consumer to dedupe
redelivered observations by `event_id` (ADR-0003, ADR-0004), which is moot
while state and committed offsets die together. Both belong in one ADR with
issue #7's shard handover, which faces the identical question.

### 4. The per-observation pipeline is §39, applied under one lock, committed in batches

`worker.py::AggregatorWorker.apply_message(ConsumedMessage)`:

```text
now = clock.now()
envelope = decode(message.value)                 # CodecError -> observations_rejected += 1, log, return
payload must be RequestObservation               # otherwise the same
for entry in payload.observations:               # ADR-0004: exactly one in practice; all are applied if more
    if entry.request_count < 1:                  # observations_rejected += 1; continue
    disposition = classify(window_start_epoch(payload.window_start), now, config)
    if disposition is not APPLY:                 # decision 2; the whole message is republished once
        continue
    S = bucket_start(window_start_epoch, config.bucket_seconds)
    ip_entry = store.get_or_create(entry.ip, now=now)   # StoreFullError -> reconciliation, rejected += 1
    count = ip_entry.counter.observe(S, entry.request_count, now=now)
    ip_entry.last_observed = now
    observations_applied += 1
    transition = decide(ip_entry.state, count, config)   # decision 5
    if transition: ip_entry.state = transition.current; publish event; count the edge
```

* `apply_message`, `run_maintenance` and `apply_config` all acquire the
  same `asyncio.Lock`, so a re-evaluation never interleaves with an
  observation and ADR-0009 decision 6's visibility rule ("the new version
  is visible in emitted events only after the re-evaluation has been
  applied") holds by construction.
* `run(consumer)` iterates `consumer.subscribe(topic)`, applies each
  message, and every `commit_every` messages (default 100) flushes the
  producer *then* commits the consumer — flush-before-commit, the same
  order ADR-0010 decision 3 requires of the trie, so a committed position
  never lies ahead of a published transition. `stop()` makes `run()`
  return after the in-flight message, with a final flush and commit
  (ADR-0009 decision 7). Batched commits mean a crash can redeliver up to
  `commit_every` messages; in M3 they are re-applied to an empty store, so
  no double count is possible (decision 3's deferral note covers the
  durable case).
* A message that cannot be decoded, or decodes to a payload type other
  than `RequestObservation`, is counted and skipped, not retried: a poison
  message must not wedge a partition. It is *not* republished to
  reconciliation, because the reconciliation topic is defined as
  well-formed observations the aggregator declined.
* The worker does not check shard ownership. ADR-0004 guarantees it only
  sees IPs it owns by partition; enforcing that is issue #7's.

`run_maintenance()` (ADR-0009 decision 3, the coroutine the periodic loop
calls) is, under the lock:

```text
now = clock.now()
changed = expire_buckets(store, now=now)                   # §5: total -= expired
for ip in changed: decide(...) as above; publish HotIpRemoved / HotIpAdded
evicted_ips += evict_idle(store, now=now, state_retention_seconds=config.state_retention_seconds)   # §26
await publisher.flush()
```

Expiry runs before eviction so a HOT IP whose window emptied emits its
`HotIpRemoved` before it can ever be considered idle.

### 5. Transitions: one decision function, one event shape, one identity rule

`transitions.py::decide(previous, window_count, config)` returns a
`StateTransition` iff `evaluate_ip_state(previous, window_count, config)`
differs from `previous`, else `None`. It calls `evaluate_ip_state`; it
does not restate the comparison (§30). Both the observation path and
re-evaluation go through it.

`StateTransition` is the M1 value object in `core/state/transitions.py`
(verified on `master`, recorded in §9, not changed here): a frozen
dataclass with two fields, `previous: IpState` and `current: IpState`, and
two derived properties, `became_hot` (`previous is COLD and current is
HOT`) and `became_cold` (`previous is HOT and current is COLD`). `decide`
returns `StateTransition(previous=previous, current=<the evaluated
state>)`. `build_event` dispatches on those properties: `became_hot`
yields `HotIpAdded`, `became_cold` yields `HotIpRemoved`, and a transition
with `previous is current` (which the type does not forbid but `decide`
never produces) raises `ValueError`, because §19 has no event for a
non-transition.

`build_event(ip, transition, window_count=, config=, now=)`:

| Field | `HotIpAdded` | `HotIpRemoved` |
| --- | --- | --- |
| `ip` | the address | the address |
| `timestamp` | `now` as an aware UTC `datetime` (the transition time) | same |
| `sequence` | `0` as built — a placeholder meaning "not yet published"; `TransitionPublisher.publish` replaces it (below) | same |
| `window_count` | the count that produced the edge | the count after the expiry/observation that produced it (0 when the whole window expired) |
| `config_version` | the config in force at the decision | same |
| `attributes` | `build_attributes(window_count, config)` = `{"attributes_version": 1, "weight": threshold_ratio(...)}` (§46.4, #48) | `None` — absent on the wire (§46.5: never stored) |

`TransitionPublisher(producer, *, producer_id, initial_sequence=0)` takes
the next sequence `n`, makes a copy of the event with that sequence
(`dataclasses.replace(event, sequence=n)` — the event passed in is frozen
and is not mutated), and wraps the **copy** in an `EventEnvelope` with
`agent_id=producer_id`, `sequence=n`, `event_type="HotIpAdded"|"HotIpRemoved"`,
`config_version=event.config_version`, `timestamp=event.timestamp`,
`subject=str(event.ip)`, encodes it with `hammertime.core.events.codec.encode`
and publishes to `hammertime.hot-ip.v1` under `HOT_IP.key_selector(event)`.
So on the wire and on the returned envelope `envelope.sequence ==
envelope.payload.sequence == n`, the same relation ingest maintains between
a `RequestObservation` envelope and its payload
(`services/ingest/publisher.py`); the codec encodes the two fields
separately, so the rewrite is what keeps them from disagreeing. The
placeholder `0` that `build_event` wrote is never published. A caller that
compares the returned `envelope.payload` to the event it passed in must
compare through `replace(event, sequence=envelope.sequence)`, not by
identity or plain equality.

* **One sequence counter per publisher for the whole hot-ip stream**, shared
  by both event types and starting at `initial_sequence`. ADR-0003's
  amendment says counters are independent *per producer and per event
  type*; one counter for both types satisfies it (uniqueness per type
  holds) and additionally gives a per-shard total order that the trie's
  `event_sequence` and a debugging engineer can both use.
* `subject = str(ip)` extends ADR-0004 decision 4's rule for the two
  observation topics to the third IP-keyed topic. `event_id` is then
  unique per `(producer, sequence, type, ip)`.
* `producer_id` is supplied by the composition root (ADR-0009). The
  recommended format is `aggregator-<shard_id>`; the worker treats it as
  opaque. The counter restarts at `initial_sequence` on every process
  start. Cross-restart `event_id` uniqueness is therefore **not**
  guaranteed and **not relied upon** in v1: the trie's redelivery
  idempotence is by state (`HotIpAdded` replaces, `HotIpRemoved` deletes;
  §46.5, `integration-scenarios.md` §3 step 4b), not by `event_id` dedup.
  The durable-store ADR (decision 3) owns making the sequence survive.

### 6. `threshold_ratio` lives in `hammertime-core`, as `hammertime.core.attributes`

§46.4 says any consumer can recompute and verify `weight`, and services
never import each other, so the function has one home: the shared package.
`hammertime.core.attributes` (`Spec: section 46.2, section 46.4`) exports:

```python
ATTRIBUTES_VERSION: Final = 1

def threshold_ratio(window_count: int, config: DetectionConfig) -> int:
    """clamp((1000 * window_count + config.hot_threshold // 2) // config.hot_threshold,
             0, config.weight_max). Integer arithmetic only. ValueError if window_count < 0."""

def compute_weight(window_count: int, config: DetectionConfig) -> int:
    """Dispatch on config.weight_function. v1 knows only "threshold_ratio";
    any other value raises ConfigurationError (unreachable through DetectionConfig,
    which validates the enum, but the dispatch must not silently default)."""

def build_attributes(window_count: int, config: DetectionConfig) -> dict[str, object]:
    """{"attributes_version": ATTRIBUTES_VERSION, "weight": compute_weight(window_count, config)}
    — the document the aggregator attaches to HotIpAdded (§46.5)."""
```

Worked values the tests must reproduce (defaults `hot_threshold=1000`,
`weight_max=1_000_000` unless stated): `threshold_ratio(1000) == 1000`,
`(0) == 0`, `(1200) == 1200`, `(2500) == 2500`, `(1) == 1`
(`(1000 + 500) // 1000`), `(1499) == 1499`; with `hot_threshold=500`:
`(600) == 1200` (`(600000 + 250) // 500`); with `hot_threshold=3`: `(1) == 333`
(`(1000 + 1) // 3`), `(2) == 667`; clamping: with `weight_max=5000`,
`(10_000) == 5000`; `(10**9)` at the default clamps to `1_000_000`, which is
exactly the schema's maximum, so a clamped weight is always encodable.

### 7. Re-evaluation walks every entry under the new thresholds and re-buckets on a geometry change

`reevaluate.py::reevaluate(store, *, previous, current, now, publisher)`
(an `async` function; `ReevaluationReport` in decision 9):

1. `current.config_version > previous.config_version` is a precondition;
   otherwise `ValueError`. The worker's `apply_config` enforces ADR-0009
   decision 6 by returning `None` for an equal-or-lower version without
   calling this.
2. If `window_seconds` or `bucket_seconds` changed, every counter is
   rebuilt first: each retained `(S, count)` pair is re-placed at
   `bucket_start(S, new_bucket_seconds)` if that bucket is live under the
   new geometry at `now`, and dropped otherwise (`store.rebucket`). Counts
   are neither invented nor scaled — the same one-bucket rule as
   observations (ADR-0010 decision 6). This keeps a running service's
   state instead of resetting it (which would mass-transition every HOT IP
   to COLD for no reason) or refusing the document (which would leave the
   aggregator's `config_version` behind the trie's and detector's).
   `store.rebucket` also replaces the store's **own** geometry — the
   `window_seconds`/`bucket_seconds` it was constructed with and exposes as
   read-only properties — so every entry `get_or_create` makes afterwards
   gets a counter of the new shape. Without that, an observation arriving
   for a new IP after a re-evaluation would be classified under the new
   geometry by `lateness.classify` (which reads `config`) and then
   validated against the old one by its counter. `rebucket` validates the
   new geometry up front, with the same rule as `IpCounter.__init__`
   (`ValueError` unless both `> 0` and `window_seconds % bucket_seconds ==
   0`), even when the store is empty and no counter would be rebuilt. It
   touches only counters: `state`, `last_observed` and first-seen order are
   preserved, and no entry is evicted, however many of its buckets die.
3. If any of `hot_threshold`, `cold_threshold`, `window_seconds`,
   `bucket_seconds` changed, every entry is visited in store order (first
   observed first) and `decide(entry.state, counter.total, current)` is
   applied; each resulting edge is published through the same
   `TransitionPublisher` with `config_version = current.config_version`
   and, for `HotIpAdded`, `attributes` computed under `current`.
4. Otherwise (`weight_function`, `weight_max`, `minimum_hot_ips`,
   `minimum_hot_ratio`, `allowed_lateness_seconds`,
   `state_retention_seconds` only) no entry is visited: §34 says the
   descriptive fields need no re-evaluation, the prefix-predicate fields
   are the trie's and detector's, and the two aggregator-only durations
   simply govern subsequent decisions.
5. `publisher.flush()` once at the end; return the report.

There is no rate bound on the emitted transitions. The reconciliation
comment in the original `reevaluate.py` stub said "at a bounded rate"; that
is not in the spec. The bus is the buffer, hysteresis makes mass transitions
an operator-initiated event (`docs/runbook.md`: "it should appear as a step,
not a ramp"), and a throttle here would only delay the trie's view of a
change the operator deliberately made.

### 8. What this ADR does not cover, and what that means for closing M3

* **`__main__.py` stays a stub.** It is ADR-0009's three-line adapter over
  `run_service`, which does not exist. Writing it now would either import
  a missing module (crash-loop, exactly as today) or duplicate the runner.
  It moves to Epic A; #5's module list should be annotated accordingly.
  `services/aggregator/Dockerfile` is already correct for that future and
  is not touched.
* **No `service.py`, no readiness, no HTTP admin endpoints, no config
  polling loop, no maintenance timer.** All ADR-0009 (Epic A). The worker
  exposes exactly the coroutines that ADR names —
  `run_maintenance()`, `apply_config()` (what `reload_config()` calls
  after loading the file), `run()`, `stop()` — so Epic A composes rather
  than reopens these modules.
* **No sharding** (`sharding/*`, issue #7). `config.py` parses
  `HAMMERTIME_SHARD_COUNT`/`HAMMERTIME_SHARD_IDS` because they are
  settings; nothing in M3 acts on them.
* **No durable window store, no consumer-side `event_id` dedup, no HOT
  state rehydration on restart** (decision 3).
* **No Prometheus registration.** `AggregatorMetrics` is a plain
  in-process counter set with §37's names; `core/telemetry/metrics.py` is
  a stub and the telemetry epic maps these onto it.
* **No cross-service test.** Everything here is unit-testable through
  `InMemoryBus`, `ManualClock` and the in-memory store inside
  `services/aggregator/.../tests/` and `packages/hammertime-core/.../tests/`.
  `tests/integration` and `tests/e2e` remain untouched (CI guard).

With those carve-outs, #5, #6 and #48 can close on domain modules plus
their unit tests; #6's third acceptance criterion
(`tests/property/test_state_machine.py`) is satisfiable today against
`hammertime.core.state` alone.

### 9. Interface reference

These are the names both test-author (who cannot read the implementation)
and coder build against. Docstrings cite the sections shown.

```python
# hammertime.aggregator.lateness            Spec: section 24, section 25
class Disposition(StrEnum):
    APPLY = "apply"; EXPIRED = "expired"; LATE = "late"; FUTURE = "future"

def window_start_epoch(window_start: datetime) -> int          # naive datetimes are UTC
def is_live(bucket_start: int, now: int, window_seconds: int) -> bool
def classify(window_start: int, now: int, config: DetectionConfig) -> Disposition
    # FUTURE: window_start > now.  LATE: not is_within_lateness(window_start, now, W, L).
    # Otherwise S = bucket_start(window_start, config.bucket_seconds):
    # APPLY iff is_live(S, now, W), else EXPIRED (decision 2: judged on S, not raw age)


# hammertime.aggregator.window.counter      Spec: section 5, section 25
class IpCounter:
    def __init__(self, *, window_seconds: int, bucket_seconds: int) -> None
        # ValueError unless both > 0 and window_seconds % bucket_seconds == 0
    window_seconds: int; bucket_seconds: int; bucket_count: int   # read-only properties
    @property
    def total(self) -> int                                        # window_count, O(1)
    def observe(self, bucket_start: int, delta: int, *, now: int) -> int
        # runs expire(now) first; ValueError if delta < 1, bucket_start % bucket_seconds != 0,
        # or not is_live(bucket_start, now, window_seconds); returns the new total
    def expire(self, now: int) -> int                             # returns the count removed
    def buckets(self) -> tuple[tuple[int, int], ...]
        # retained (bucket_start, count) pairs with count > 0, ascending by bucket_start,
        # as of the last observe/expire call
    @classmethod
    def rebuild(cls, buckets: Iterable[tuple[int, int]], *, window_seconds: int,
                bucket_seconds: int, now: int) -> IpCounter


# hammertime.aggregator.window.store        Spec: section 5, section 20, section 26
class StoreFullError(HammertimeError): ...        # defined here; not added to core/errors.py

@dataclass(slots=True)
class IpEntry:
    counter: IpCounter
    state: IpState              # IpState.COLD on creation
    last_observed: int          # service-clock time of the last applied observation, or of
                                #   creation if none was applied yet (decision 3)

class InMemoryWindowStore:
    def __init__(self, *, window_seconds: int, bucket_seconds: int,
                 max_tracked_ips: int = 1_000_000) -> None
        # same geometry rule as IpCounter: ValueError unless both > 0 and W % B == 0
    window_seconds: int; bucket_seconds: int   # read-only properties: the geometry a new
                                               #   entry's counter gets; replaced by rebucket
    def __len__(self) -> int
    def __contains__(self, ip: Address) -> bool
    def get(self, ip: Address) -> IpEntry | None
    def get_or_create(self, ip: Address, *, now: int) -> IpEntry
        # existing ip: returns the same IpEntry object untouched -- last_observed is NOT
        #   refreshed, nothing is evicted, first-seen order is unchanged; `now` is ignored
        # new ip: IpEntry(counter=IpCounter(<store geometry>), state=COLD, last_observed=now),
        #   appended in first-seen order; when full, first evicts the COLD entry with the
        #   oldest last_observed (ties: the earlier-seen one); StoreFullError if no COLD entry
    def remove(self, ip: Address) -> None                          # no-op when absent
    def entries(self) -> list[tuple[Address, IpEntry]]             # snapshot in first-seen order
    def rebucket(self, *, window_seconds: int, bucket_seconds: int, now: int) -> None
        # validates the geometry first (ValueError as __init__), then sets the store's own
        # window_seconds/bucket_seconds and replaces every entry's counter with
        # IpCounter.rebuild(counter.buckets(), ...) at `now`; state, last_observed and order
        # are kept; nothing is evicted; harmless on an empty store or an unchanged geometry
    @property
    def tracked_ips(self) -> int
    @property
    def hot_ips(self) -> int
    @property
    def active_ips(self) -> int


# hammertime.aggregator.window.expiry       Spec: section 5, section 26
def expire_buckets(store: InMemoryWindowStore, *, now: int) -> list[Address]
    # every entry's counter.expire(now); returns the IPs whose total changed, in store order
def evict_idle(store: InMemoryWindowStore, *, now: int, state_retention_seconds: int) -> int
    # removes COLD entries with now - last_observed >= state_retention_seconds; returns how many


# hammertime.core.state.transitions         Spec: section 19, section 30
# (M1, already on master; recorded here because decision 5 builds on it -- not changed)
@dataclass(frozen=True, slots=True)
class StateTransition:
    previous: IpState
    current: IpState
    @property
    def became_hot(self) -> bool       # previous is COLD and current is HOT
    @property
    def became_cold(self) -> bool      # previous is HOT and current is COLD


# hammertime.aggregator.transitions         Spec: section 6, section 19, section 30, section 46.4
def decide(previous: IpState, window_count: int, config: DetectionConfig) -> StateTransition | None
    # StateTransition(previous=previous, current=evaluate_ip_state(...)) iff they differ
def build_event(ip: Address, transition: StateTransition, *, window_count: int,
                config: DetectionConfig, now: int) -> HotIpAdded | HotIpRemoved
    # became_hot -> HotIpAdded; became_cold -> HotIpRemoved; neither -> ValueError.
    # sequence=0 (placeholder; the publisher assigns the real one), timestamp=now as aware UTC

class TransitionPublisher:
    def __init__(self, producer: Producer, *, producer_id: str, initial_sequence: int = 0) -> None
    @property
    def next_sequence(self) -> int
    async def publish(self, event: HotIpAdded | HotIpRemoved) -> EventEnvelope[HotIpAdded | HotIpRemoved]
        # n = next_sequence; payload = dataclasses.replace(event, sequence=n); the returned
        # (and published) envelope has sequence == payload.sequence == n; `event` is not mutated
    async def flush(self) -> None


# hammertime.aggregator.reevaluate          Spec: section 34
@dataclass(frozen=True, slots=True)
class ReevaluationReport:
    previous_version: int
    config_version: int
    rebucketed: bool
    evaluated: int          # entries visited (0 when only descriptive fields changed)
    became_hot: int
    became_cold: int

async def reevaluate(store: InMemoryWindowStore, *, previous: DetectionConfig,
                     current: DetectionConfig, now: int,
                     publisher: TransitionPublisher) -> ReevaluationReport


# hammertime.aggregator.worker              Spec: section 30, section 39; ADR-0009 decisions 3, 6, 7
@dataclass(slots=True)
class AggregatorMetrics:
    observations_applied: int = 0
    observations_rejected: int = 0      # undecodable, wrong payload type, request_count < 1, store full
    late_messages: int = 0              # §37
    future_messages: int = 0
    expired_on_arrival: int = 0
    reconciliation_published: int = 0   # messages republished (one per diverted message)
    cold_to_hot_transitions: int = 0    # §37
    hot_to_cold_transitions: int = 0    # §37
    evicted_ips: int = 0
    config_reloads: int = 0             # apply_config calls that applied a document

class AggregatorWorker:
    def __init__(self, *, config: DetectionConfig, store: InMemoryWindowStore,
                 producer: Producer, producer_id: str, clock: Clock,
                 initial_sequence: int = 0, commit_every: int = 100) -> None
    @property
    def config(self) -> DetectionConfig
    metrics: AggregatorMetrics
    store: InMemoryWindowStore
    publisher: TransitionPublisher
    async def apply_message(self, message: ConsumedMessage) -> None
    async def run_maintenance(self) -> None
    async def apply_config(self, config: DetectionConfig) -> ReevaluationReport | None
    async def run(self, consumer: Consumer, *, topic: str = OBSERVATIONS.name) -> None
    async def stop(self) -> None


# hammertime.aggregator.config              Spec: section 20, section 47.1; ADR-0009 decision 2
@dataclass(frozen=True, slots=True)
class AggregatorSettings:
    detection_config_path: Path     # HAMMERTIME_CONFIG_PATH        default ./config/detection.v1.json
    bus_kind: str                   # HAMMERTIME_BUS_KIND            kafka | memory, default kafka
    bus_brokers: str                # HAMMERTIME_BUS_BROKERS         default localhost:19092
    store_kind: str                 # HAMMERTIME_STORE_KIND          redis | memory, default redis
    redis_url: str                  # HAMMERTIME_REDIS_URL           default redis://localhost:6379/0
    shard_count: int                # HAMMERTIME_SHARD_COUNT         default 64, > 0
    shard_ids: tuple[int, ...]      # HAMMERTIME_SHARD_IDS           default: every id in [0, shard_count),
                                    #   i.e. tuple(range(shard_count)) of the *parsed* shard_count --
                                    #   derived, never the literal "0-63", so a lowered shard_count
                                    #   never invalidates its own default. When set: split on ",",
                                    #   each part stripped, a part is "a" or "a-b" (a <= b, inclusive);
                                    #   result sorted, deduplicated, non-empty, each in [0, shard_count).
                                    #   Malformed: an empty part (so "" / "   " / "," are all rejected),
                                    #   a non-integer, "a-" / "-b" / "-", a reversed range "7-3".
    host: str; port: int            # HAMMERTIME_AGGREGATOR_BIND     default 0.0.0.0:8083; split on the
                                    #   LAST ":" (rpartition); malformed unless a ":" is present, host
                                    #   is non-empty and port is a decimal integer in [0, 65535]
                                    #   (0 = ephemeral, integration-scenarios.md). Host is not
                                    #   otherwise validated; "[::1]:8083" passes through as-is.
    maintenance_interval_s: float   # HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S  default 1.0, > 0
    config_poll_interval_s: float   # HAMMERTIME_CONFIG_POLL_INTERVAL_S             default 1.0, > 0
    startup_timeout_s: float        # HAMMERTIME_STARTUP_TIMEOUT_S                  default 60, > 0
    shutdown_timeout_s: float       # HAMMERTIME_SHUTDOWN_TIMEOUT_S                 default 8, > 0
    max_tracked_ips: int            # HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS         default 1000000, > 0
    commit_every: int               # HAMMERTIME_AGGREGATOR_COMMIT_EVERY            default 100, > 0

def load_settings(env: Mapping[str, str] | None = None) -> AggregatorSettings
    # env=None -> os.environ; unknown keys ignored. Only a MISSING key takes its default
    # (env.get(key, default) semantics, as services/ingest/config.py): an empty or
    # whitespace-only value is a value, and every key -- including the free-text ones
    # (HAMMERTIME_CONFIG_PATH, HAMMERTIME_BUS_BROKERS, HAMMERTIME_REDIS_URL) -- rejects it as
    # malformed. Malformed/out-of-range -> ValueError whose message names the variable.
    # No other trimming or case-folding: bus_kind/store_kind must match exactly ("KAFKA" is
    # rejected); "> 0" keys reject 0 and negatives; the interval/timeout keys accept decimals.
```

## Assumptions

Each of these is a judgment call, not a requirement traceable to the spec
or a prior ADR. Push back on them individually.

* **Live-bucket edge `S <= now < S + W`, clock-judged.** §5 gives an
  example, not an edge. Chosen so the window is exactly `W` seconds of
  aligned buckets including the current one, and so that
  `integration-scenarios.md` §2.4's `advance(310)` steps are correct. The
  alternative (judge liveness against the newest observation) would let a
  silent IP stay HOT forever.
* **`EXPIRED` observations go to reconciliation.** ADR-0002 only routes
  observations beyond the lateness horizon there. Extending it to the
  `[W, W + L]` zone is what makes "applied XOR reconciled" an invariant a
  test can assert; the cost is a few extra reconciliation records.
* **Future-dated observations are rejected outright, no skew tolerance.**
  `is_within_lateness` already rejects negative age; a tolerance would be
  a new number with no source. Ingest's `agent_clock_skew_seconds`
  (ADR-0002) is the operator's signal.
* **Retention is judged on arrival time, not event time.** Simpler and
  monotone; an IP with a live bucket always has `last_observed` within
  the last `W + L` seconds anyway.
* **Only COLD entries are evicted by retention; HOT never.** §26 says
  "cold IP state SHOULD expire"; evicting a HOT entry would silently
  orphan a HOT record in the trie.
* **`max_tracked_ips` default 1 000 000 with oldest-COLD eviction.** Sized
  for roughly 0.5-1 GiB per process at a few hundred bytes per entry; not
  derived from any requirement. Evicting a COLD entry with a non-empty
  window loses counts, accepted as the lesser evil versus OOM.
* **`commit_every` default 100.** A throughput/redelivery trade-off with
  no spec basis; harmless in M3 because state and offsets die together.
* **Undecodable / wrong-type messages are skipped and counted, not
  republished.** A poison-pill policy; the alternative (halt the
  partition) turns one bad byte into an outage.
* **`request_count < 1` is rejected.** Ingest never publishes such an
  entry (ADR-0008), but the aggregator must not trust that a negative
  delta cannot arrive.
* **Multi-entry payloads are applied entry by entry.** ADR-0004 says
  consumers MAY assert single-entry; applying all is the more forgiving
  reading and costs nothing.
* **One sequence counter for both hot-ip event types; restarts at
  `initial_sequence` (0) per process; `subject = ip`.** See decision 5.
  `producer_id` format `aggregator-<shard_id>` is a recommendation to
  Epic A, not enforced.
* **Geometry changes are applied by re-bucketing, not rejected or reset.**
  See decision 7. The rounding rule (`bucket_start(S, new_B)`) is the
  same one observations already follow.
* **No rate bound on re-evaluation output.** Contradicts the stub
  comment, not the spec; see decision 7.
* **`threshold_ratio` raises `ValueError` on negative `window_count`.**
  A count can never be negative (deltas are `>= 1`, expiry subtracts what
  was added); a negative input is a programming error and must not be
  clamped to 0 silently.
* **`hot_ips`/`active_ips` may be O(n).** Gauges for scrapes; an
  incremental count is an optimisation the store may add without changing
  the contract.
* **`config.py` includes ADR-0009's lifecycle keys now.** They are
  trivially parsed and defined already; including them means Epic A does
  not reopen this module. Nothing in M3 reads them.
* **`__main__.py` deferred to Epic A** rather than pulling
  `core/runtime.py` into M3 (decision 8).

Added by the 2026-09-17 revision:

* **`EXPIRED`/`APPLY` are judged on the bucket `S`, not on raw age.** The
  original table gave both readings joined by "i.e."; they differ only for
  an unaligned `window_start`, which ingest never publishes. The bucket
  reading is the one decision 4's pipeline can survive (an age-based
  `APPLY` with a dead `S` would make `counter.observe` raise); the raw-age
  band `[W, W + L]` is kept only as the description for aligned starts.
  `LATE`/`FUTURE` stay raw-age tests — no source says a lateness policy
  should be rounded to bucket edges, and rounding them would change ADR-0002.
* **`shard_ids` defaults to `range(shard_count)` of the parsed
  `shard_count`.** `.env.example` writes the default as the literal `0-63`;
  a literal default breaks the moment `HAMMERTIME_SHARD_COUNT` is lowered,
  so the loader derives it instead. Same observable value at the defaults.
* **`get_or_create` never refreshes `last_observed` on an existing entry;
  `now=` seeds a new entry only.** Follows from "time of the last *applied*
  observation" plus the worker setting it after `observe`; nothing in M3
  calls `get_or_create` without applying, so no behaviour depends on the
  alternative. Cap-eviction ties on `last_observed` break towards the
  earlier-seen entry — an arbitrary but deterministic choice.
* **`rebucket` replaces the store's own geometry and validates it up
  front.** The alternative (rebuilt counters carry the new shape while new
  entries get the old one) would make `classify` and `IpCounter.observe`
  disagree for every IP first seen after a re-evaluation. Validating even
  on an empty store is so that an invalid geometry cannot be latched
  silently and surface only on the next `get_or_create`.
  `window_seconds`/`bucket_seconds` are exposed as read-only store
  properties so that this is observable without creating an entry.
* **An empty or whitespace-only environment value is malformed for every
  key, never "unset".** `services/ingest/config.py` already has these
  semantics for its validated keys (`env.get(k, default)` then parse); the
  aggregator extends the same rule to its three free-text keys, which
  ingest passes through, because an empty broker list, Redis URL or config
  path is never a working configuration and failing at load is what §47.1
  asks for. Diverges from ingest only in rejecting those three.
* **`HAMMERTIME_AGGREGATOR_BIND` follows ingest's `_parse_bind`
  (`rpartition(":")`, non-empty host, digit-only port) plus an upper bound
  of 65535.** The bound is not in ingest; it is the only value a port can
  never take and costs one comparison. `0` is accepted because
  `integration-scenarios.md` binds every service to `127.0.0.1:0`. The host
  is not validated (bracketed IPv6 literals pass through unchanged).
* **`build_event` raises `ValueError` for a `StateTransition` whose
  `previous is current`.** The M1 type does not forbid constructing one
  and `decide` never returns one; refusing it is safer than picking an
  event type arbitrarily. `StateTransition`'s shape itself is recorded, not
  designed, here.
* **`build_event` writes `sequence=0` as a placeholder and
  `TransitionPublisher.publish` rewrites it with `dataclasses.replace`.**
  `HotIpAdded`/`HotIpRemoved` require a `sequence` and `build_event` has
  no access to the counter; passing the sequence into `build_event` would
  change a signature the tests already use. Rewriting is what makes
  `envelope.sequence == payload.sequence`, the relation ingest already
  keeps for observations; publishing the placeholder would put a constant
  `0` on every hot-ip payload. Consequence: the envelope's `payload` is a
  copy, not the caller's object.

## Consequences

* New core module `hammertime.core.attributes` (with tests under
  `packages/hammertime-core/.../tests/test_weight.py`). `docs/spec/README.md`
  maps §46 to it.
* Seven aggregator modules gain bodies: `window/{__init__,counter,expiry,
  store}.py`, `lateness.py`, `transitions.py`, `reevaluate.py`,
  `worker.py`, `config.py`. `__init__.py`'s docstring stands.
  `__main__.py`, `sharding/*` and `Dockerfile` are untouched.
* `hammertime-store`'s `interface.py` stays dedup-only; its note that the
  window-counter protocol "belongs to the aggregator epic" is answered by
  decision 3: there is no cross-package protocol until a durable variant
  is designed.
* Hot-ip envelopes now carry `subject`. Every build in the repo already
  understands `subject` (ADR-0004), so this is not a breaking change on
  the wire, but a consumer that hard-codes the pre-ADR-0004 `event_id`
  derivation would reject them — the same caveat ADR-0004 already gives.
* `CHANGES`: the behaviours here become user-visible only when the
  aggregator process exists (Epic A). Recommended lines, to ride with
  whichever PR first makes the service runnable, are listed in the M3
  hand-off report; none is `BREAKING`.
* Epic A's `build_service` for the aggregator becomes a composition of
  `load_settings`, `loader.load`, `InMemoryWindowStore(window_seconds=…,
  bucket_seconds=…, max_tracked_ips=settings.max_tracked_ips)`,
  `AggregatorWorker(…)`, a periodic `run_maintenance` timer, a config
  poller that calls `apply_config`, and the three admin endpoints —
  nothing in these modules needs to change for it.
* Issue #7 (sharding) and the durable-store ADR inherit two open
  questions from decision 3 and decision 5: HOT-state rehydration on
  restart/handover, and cross-restart `sequence` continuity.
