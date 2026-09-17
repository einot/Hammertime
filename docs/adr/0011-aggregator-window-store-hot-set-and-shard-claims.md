# ADR 0011 — Aggregator: process-local window store, durable per-shard HOT set, partition-as-shard claims, and config re-evaluation

Status: accepted

Scope note: this ADR settles the interfaces milestone M3 (epics #5, #6, #7
and issue #48) implements against — what the aggregator keeps per IP, how
expiry and retention bound it, what a shard and a shard claim are, how the
aggregator's HOT set survives a restart or a handover, where `weight` is
computed, and what a configuration change re-evaluates. It builds on
ADR-0009 (process lifecycle: `build_service`, `Service`, readiness,
`run_maintenance`, the `hammertime-aggregator` group) and ADR-0010 (one
bucket per observation) as written and amends neither. It does not design
the trie or the detector; the two things it needs from the trie are already
spec (§11's `hot_count >= 0`, §46.5's replace-on-add) and are restated under
Consequences.

## Context

`services/aggregator/` is stubs. The spec fixes the arithmetic (§5 ring with
a running total, §6/§7 hysteresis via `evaluate_ip_state`, §25 bucket math)
and the pipeline position (§19: consume `RequestObservation`, emit
`HotIpAdded`/`HotIpRemoved`), and leaves four things open that have to be
settled before anyone can write a test or a module:

1. **The window store.** §5 gives the per-IP ring; §26 says the store may
   hold far more IPs than the trie and that cold state SHOULD expire after
   `state_retention_seconds`. Nothing says how expiry is driven, what bounds
   the store against a large address space, or whether counters live in the
   process or in Redis (the `window/store.py` stub offers both).
2. **Shards and claims.** §20 says `hash(IP) -> shard`, one owner per IP.
   ADR-0004 already makes ingest publish one message per IP keyed by the IP,
   so the bus partitions the stream by IP. ADR-0009 fixes the consumer group
   name and readiness ("shard claims held") and explicitly leaves "how
   [`HAMMERTIME_SHARD_IDS`] maps onto partitions" to this epic.
   `.env.example` says `HAMMERTIME_SHARD_COUNT=64`; `topics.py` provisions
   32 partitions; `deploy/k8s/README.md` scales the aggregator with an HPA on
   consumer-group lag, i.e. identical replicas that cannot each carry a
   distinct static shard list.
3. **State that must not be lost.** §32 names "IP sliding-window state +
   HOT/COLD state" as *the authoritative information*; the trie is derived
   from the transitions the aggregator emits. If an aggregator restarts with
   empty memory it forgets which IPs it told the trie were HOT, never emits
   the matching `HotIpRemoved`, and the trie keeps them HOT forever — a
   permanent §12 divergence reachable by a plain `docker compose restart`.
   This holds with a single shard and an in-memory store; it is not a
   scale-out problem.
4. **Configuration changes.** §34 requires a change to define its effect on
   existing state and asks for controlled re-evaluation; ADR-0009 decision 6
   says the new `config_version` becomes visible only after that
   re-evaluation is applied. What is re-evaluated, and what happens when
   `bucket_seconds`/`window_seconds` change the ring's geometry, is unstated.

Two facts about the shipped code constrain the answers. First,
`hammertime.core.time.buckets.is_within_lateness` (with its tests) already
defines the acceptance horizon as `0 <= now - event <= window_seconds +
allowed_lateness_seconds` with the *configured* window, and
`docs/spec/integration-scenarios.md` §2.4 restates it — so that is the
policy boundary this ADR keeps. Second, the ring holds exactly one window's
worth of buckets, so a delta whose bucket has already left the window cannot
influence any future `window_count` no matter when it arrives; the policy
must say what happens to such an observation rather than let it vanish.

## Decision

### 1. A shard is a partition of `hammertime.observations.v1`; a claim is the consumer group's assignment

`hash(IP) -> shard` (§20) is realised by the bus: ingest publishes each
observation keyed by its IP (ADR-0004), the producer's key partitioner maps
the key to a partition, and that partition index **is** the shard id. The
aggregator never computes an IP hash of its own; it learns an IP's shard
from `ConsumedMessage.partition`. There is no `sharding/hashing.py`.

Ownership is the consumer group's partition assignment, delivered through a
listener the bus interface gains:

```python
# hammertime.bus.interface  (additions)
@runtime_checkable
class AssignmentListener(Protocol):
    async def on_revoked(self, partitions: frozenset[tuple[str, int]]) -> None: ...
    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None: ...

class Consumer(Protocol):
    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
    ) -> AsyncIterator[ConsumedMessage]: ...
    # seek(), commit() unchanged
```

* `partitions=None` (the default) is group-managed assignment: the broker
  decides, and every rebalance calls `on_revoked` (before partitions move)
  then `on_assigned` (after). `partitions={...}` is static assignment: this
  member owns exactly those partitions of `topic`, no group coordination
  takes place, and offsets are still committed under the group name. A
  deployment MUST NOT mix static and group-managed members in one group —
  the coordinator would hand a statically owned partition to a dynamic
  member as well, giving an IP two owners.
* When a listener is given, `subscribe()` does not return until
  `on_assigned` has been awaited with the initial assignment (which may be
  empty under group management). That is what makes ADR-0009's readiness
  rule ("shard claims held") observable: `start()` returns after
  `subscribe()`.
* `InMemoryBus` has one partition per topic: group-managed and static
  `{0}` both assign `{(topic, 0)}` immediately; any other static set is a
  `ValueError`. `InMemoryBus` supports at most one live member per group per
  topic; a second `MemoryConsumer` for the same group would re-read the same
  log, which no test relies on.
* `KafkaConsumer` implements the group-managed path with aiokafka's
  `subscribe(topics, listener=ConsumerRebalanceListener)` and the static
  path with `assign([TopicPartition(...)])`, synthesising the single
  `on_assigned` call itself in the static case (aiokafka does not invoke
  rebalance listeners for manual assignment). See Sources.

`HAMMERTIME_SHARD_IDS` is the only sharding setting: `auto` (default,
group-managed) or an explicit set (`0`, `0-3`, `0,2,5-7`; inclusive ranges;
static assignment). `HAMMERTIME_SHARD_COUNT` is retired: with ownership
decided by the partitioner there is nothing for the aggregator to do with a
declared count, and the value in `.env.example` (64) already disagreed with
`topics.py` (32). Both variables were never read by any shipped build, so
retiring one and redefining the other is not a breaking change for a
running deployment.

ADR-0009 decision 9 stands: one fixed group, `hammertime-aggregator`; what
distinguishes members is the set of partitions they hold — chosen by the
operator in static mode and by the coordinator in `auto` mode.

### 2. Window counters are process-local, per shard, and bounded by expiry, retention and a cap

Every claimed shard has one `ShardWindow` in process memory holding, per
tracked IP: an `IpCounter` (the §5 ring), the IP's `IpState`, `last_seen`
(event time of the newest applied bucket) and an `inherited` flag (decision
5). Nothing per-observation touches Redis.

```python
# hammertime.aggregator.window.counter   (Spec: section 5, section 24, section 25)
class IpCounter:
    def __init__(self, *, bucket_seconds: int, bucket_count: int) -> None: ...
    bucket_seconds: int; bucket_count: int
    @property
    def window_seconds(self) -> int: ...          # bucket_seconds * bucket_count
    @property
    def total(self) -> int: ...                   # the running total; never recomputed by summing on read
    def is_live(self, bucket_start: int, now: int) -> bool: ...
    def observe(self, bucket_start: int, delta: int, now: int) -> bool: ...
    def expire(self, now: int) -> int: ...
    def next_expiry(self) -> int | None: ...
    def live_buckets(self, now: int) -> tuple[tuple[int, int], ...]: ...
```

Ring semantics (`B = bucket_seconds`, `N = bucket_count`, `W = B * N`;
`bucket_start` is `hammertime.core.time.buckets.bucket_start`):

* A bucket starting at `S` (always a multiple of `B`) is **live** at time
  `now` iff `bucket_start(now, B) - S < W`. It enters the ring when its
  first delta arrives and leaves the window at exactly `now = S + W`. The
  live buckets at any `now` are the `N` buckets ending with the one that
  contains `now`, so two live buckets never share a slot
  (`slot = (S // B) % N`, `core.time.buckets.bucket_index`).
* `observe(S, delta, now)`: if `S` is not live, return `False` and change
  nothing. Otherwise, if the slot holds an older bucket, subtract that
  bucket's count from `total` and reset the slot to `S`; then add `delta`
  to the slot and to `total`; return `True`. `delta < 0` is a `ValueError`;
  `delta == 0` is applied (returns `True`, changes nothing).
* `expire(now)`: zero every slot whose bucket is no longer live, subtract
  the zeroed counts from `total`, and return the amount removed.
* `next_expiry()`: `min(S) + W` over slots with a non-zero count, `None`
  if every slot is zero. Lets the sweep (decision 6) find the IPs that
  actually have something to expire instead of scanning the store.
* `live_buckets(now)`: the `(S, count)` pairs of non-zero live slots in
  ascending `S`; used by `ShardWindow.apply_config` and by tests.

```python
# hammertime.aggregator.window.store   (Spec: section 5, section 20, section 26)
@dataclass(frozen=True, slots=True)
class WindowChange:
    ip: Address
    state: IpState          # the IP's state before any evaluation; the store never evaluates
    total_before: int
    total_after: int

class ShardWindow:
    def __init__(
        self,
        *,
        shard: int,
        config: DetectionConfig,
        clock: Clock,
        inherited_hot: Iterable[Address] = (),
        next_sequence: int = 0,
        max_tracked_ips: int = 1_000_000,
    ) -> None: ...
    shard: int
    next_sequence: int                              # decision 4; loaded from the state store at claim
    @property
    def config(self) -> DetectionConfig: ...
    @property
    def warm_until(self) -> int | None: ...         # clock.now() + window_seconds at construction iff inherited_hot is non-empty
    @property
    def in_warmup(self) -> bool: ...                # warm_until is not None and clock.now() < warm_until
    def observe(self, ip: Address, bucket_start: int, delta: int) -> WindowChange | None: ...
    def total(self, ip: Address) -> int: ...        # 0 when untracked
    def state(self, ip: Address) -> IpState: ...    # COLD when untracked
    def is_tracked(self, ip: Address) -> bool: ...
    def is_inherited(self, ip: Address) -> bool: ...
    def set_state(self, ip: Address, state: IpState) -> None: ...
    def finish_warmup_if_due(self) -> frozenset[Address] | None: ...
    def expire_due(self) -> list[WindowChange]: ...
    def evict_due(self) -> int: ...
    def apply_config(self, config: DetectionConfig) -> None: ...
    def hot_ips(self) -> frozenset[Address]: ...
    def tracked_ips(self) -> frozenset[Address]: ...
    @property
    def tracked_count(self) -> int: ...
    @property
    def active_count(self) -> int: ...              # tracked IPs whose total > 0
    @property
    def hot_count(self) -> int: ...
    capacity_evictions: int                         # counter, decision 8
    retention_evictions: int
```

* `observe` creates the entry for an untracked IP (state `COLD`, empty
  ring), applies the delta with `now = clock.now()`, sets
  `last_seen = max(last_seen, bucket_start)`, and returns the change. It
  returns `None` and leaves the store untouched — no entry created, no
  `last_seen` refresh — when the bucket is not live; the caller has already
  classified that case (decision 3).
* `expire_due()` calls `IpCounter.expire(now)` only on IPs whose
  `next_expiry() <= now` (an expiry schedule ordered by `next_expiry`, in
  the style of `MemoryDedupStore._expiry_heap`) and returns one
  `WindowChange` per IP whose total actually dropped. A sweep therefore
  costs O(expiring IPs · log n), never O(tracked IPs).
* `evict_due()` removes every IP that is `COLD`, has `total == 0`, and
  whose `last_seen + config.state_retention_seconds <= now` (§26); returns
  the number removed. A HOT IP is never evicted; because
  `state_retention_seconds >= window_seconds` (`DetectionConfig`), a HOT IP
  always reaches `total == 0` and is demoted (decision 6) before its
  retention deadline can pass.
* Capacity: when a new IP would make `tracked_count` exceed
  `max_tracked_ips`, the `COLD` IP with the smallest `last_seen` is evicted
  first (`capacity_evictions += 1`). If every tracked IP is HOT the store
  grows past the cap rather than drop a HOT IP; that condition is logged.
* `set_state(ip, COLD)` clears the `inherited` flag for that IP;
  `finish_warmup_if_due()` returns — exactly once, the first time it is
  called with `clock.now() >= warm_until` — the set of inherited IPs that
  are still HOT, clears every `inherited` flag and sets `warm_until` to
  `None`; every other call returns `None`.
* `apply_config(new)`: if `bucket_seconds` or `bucket_count` differ from
  the current config, rebuild every counter: for each `(S, count)` in
  `live_buckets(now)` of the old counter, `observe(bucket_start(S, B'),
  count, now)` on a new one. Counts whose new bucket is not live under the
  new window are dropped (they would have expired under it); otherwise no
  count is lost, and a merge into a coarser bucket is exact. Then adopt
  `new` for every subsequent decision. The window's `warm_until` is not
  recomputed.

The shipped defaults (300 s / 10 s, retention 600 s) bound the store to the
distinct IPs a shard sees in any ten-minute period, and the cap bounds it
absolutely. A HOT IP costs the same as any other tracked IP here; the
per-HOT-IP cost that outlives the process is in decision 5.

### 3. One observation, one of six outcomes; everything the hot path cannot use goes to reconciliation

```python
# hammertime.aggregator.lateness   (Spec: section 24; ADR-0002; ADR-0010 decision 6)
class ObservationOutcome(StrEnum):
    APPLIED = "applied"
    LATE = "late"                        # now - window_start > window_seconds + allowed_lateness_seconds
    FUTURE = "future"                    # window_start > now
    EXPIRED_BUCKET = "expired_bucket"    # inside the horizon, but the bucket has already left the window
    WINDOW_TOO_LONG = "window_too_long"  # payload.window_seconds > config.window_seconds
    MALFORMED = "malformed"              # never returned by classify_observation; a worker outcome

def classify_observation(
    *, window_start: int, window_seconds: int, now: int, config: DetectionConfig
) -> ObservationOutcome: ...
```

`classify_observation` is pure and checks, in this order: `WINDOW_TOO_LONG`
(ADR-0010 decision 6), `FUTURE`, `LATE` (the shipped
`is_within_lateness` horizon, config window), `EXPIRED_BUCKET`
(`bucket_start(now) - bucket_start(window_start) >= config.window_seconds`,
i.e. `IpCounter.is_live` is false), else `APPLIED`. `now` is the service
clock at processing time, not the envelope timestamp: a lagging aggregator
diverts what it can no longer count rather than counting it into the past.

The worker (`hammertime.aggregator.worker`) handles one consumed message as:

1. `codec.decode`; the payload MUST be a `RequestObservation` with exactly
   one entry whose IP text equals the envelope `subject` and the message key
   (ADR-0004's producer invariant, asserted here). Any failure, including
   `CodecError`, is `MALFORMED`: logged at `WARNING event=malformed_observation`
   with the topic, partition and offset, counted, and skipped. A poison
   message never stops the consumer.
2. `classify_observation(...)`. `LATE`, `FUTURE`, `EXPIRED_BUCKET` and
   `WINDOW_TOO_LONG` are **diverted**: the consumed bytes are republished
   unchanged, under the same key, to
   `hammertime.observations-reconciliation.v1` (same `event_id`, so a
   reconciliation consumer can dedupe against the hot path), and counted
   (decision 8). The window store is not touched.
3. `APPLIED`: `window.observe(ip, bucket_start(window_start), request_count)`
   on the `ShardWindow` of `message.partition`, then decision 4 for that IP
   with `reason="observation"`.

Consumption is at-least-once (ADR-0003). Counters are process-local, so a
redelivery after a crash rebuilds counters that died with the process
rather than double-counting them, and a redelivery after a handover lands
in a `ShardWindow` that never held the first copy. The only state that
survives is the HOT set (decision 5), which is idempotent under
redelivery: an IP the store already has as HOT is not re-announced.

### 4. Transitions: one call site for `evaluate_ip_state`, persist before publish, `weight` from core

```python
# hammertime.core.state.weight   (new; Spec: section 46.4; ADR-0005)
def threshold_ratio(window_count: int, config: DetectionConfig) -> int:
    """clamp((1000 * window_count + config.hot_threshold // 2) // config.hot_threshold, 0, config.weight_max)"""
def compute_weight(window_count: int, config: DetectionConfig) -> int:
    """Dispatch on config.weight_function; v1 knows only "threshold_ratio"."""
def transition_attributes(window_count: int, config: DetectionConfig) -> dict[str, object]:
    """{"attributes_version": 1, "weight": compute_weight(window_count, config)}"""
```

```python
# hammertime.aggregator.transitions   (Spec: section 6, section 19, section 30, section 46.4)
@dataclass(frozen=True, slots=True)
class EmittedTransition:
    ip: Address
    transition: StateTransition
    sequence: int
    window_count: int
    config_version: int

class TransitionEmitter:
    def __init__(
        self, *, producer: Producer, state_store: ShardStateStore, clock: Clock, metrics: AggregatorMetrics
    ) -> None: ...
    async def evaluate(
        self, window: ShardWindow, ip: Address, *, config: DetectionConfig, reason: str
    ) -> EmittedTransition | None: ...
```

`evaluate` is the aggregator's only caller of
`hammertime.core.state.machine.evaluate_ip_state` (§30). It reads
`previous = window.state(ip)`, `count = window.total(ip)`, computes
`new = evaluate_ip_state(previous, count, config)` and returns `None` when
nothing changed. A HOT -> COLD result is also returned as `None` — with no
side effects — while `window.in_warmup and window.is_inherited(ip)`
(decision 5). Otherwise, in this order:

1. `sequence = window.next_sequence`; `window.next_sequence += 1`.
2. `await state_store.record_transition(window.shard, ip, new, sequence)`
   — the durable HOT set is updated **before** the event exists. A failure
   here propagates: the transition is not emitted and the state is not
   changed in memory (the sequence number is consumed; gaps are fine).
3. Build the payload: `HotIpAdded(ip, timestamp, sequence, window_count=count,
   config_version=config.config_version, attributes=transition_attributes(count, config))`
   or `HotIpRemoved(...)` with `attributes=None`; `timestamp` is
   `datetime.fromtimestamp(clock.now(), tz=UTC)`.
4. Envelope: `agent_id=f"aggregator-shard-{window.shard}"`,
   `sequence=sequence`, `event_type`, `config_version=config.config_version`,
   the same `timestamp`, `subject=str(ip)`; publish to
   `TOPICS["hammertime.hot-ip.v1"]` under `key_selector(payload)` and
   `await` it (both bus producers return only once the broker has
   acknowledged; no separate flush per transition).
5. `window.set_state(ip, new)`; count the transition under `reason`
   (`observation`, `expiry`, `warmup`, `config`); return the
   `EmittedTransition`.

Identity follows the ADR-0003 amendment and ADR-0004: `agent_id` is the
producing shard, `sequence` is the shard's own counter (persisted, decision
5, so it never restarts at 0 after a crash and can never reproduce an
earlier `event_id`), and `subject` is the IP. The optional `shard` property
of `schemas/hot_ip_event.v1.json` is not populated — the codec has no field
for it and the envelope's `agent_id` already carries the shard.

Why persist before publish: if the process dies between the two, the store
says HOT but the trie was never told. The next owner inherits the IP as HOT
and, after warm-up, either keeps it (it is still busy; the trie learns
about it at its next COLD -> HOT edge) or demotes it (the trie applies a
`HotIpRemoved` for an IP it does not hold, which §11's `hot_count >= 0`
already makes a no-op). The opposite order would leave the trie holding an
IP no owner knows about — the permanent leak this ADR exists to close. A
duplicate `HotIpAdded` from the recovery path is harmless (§46.5:
replace-on-add).

### 5. The HOT set of each shard is durable; a claim inherits it and warms up

```python
# hammertime.store.interface   (additions; Spec: section 20, section 26, section 32)
@dataclass(frozen=True, slots=True)
class ShardState:
    hot_ips: frozenset[Address]
    next_sequence: int              # 0 for a shard that has never recorded a transition

@runtime_checkable
class ShardStateStore(Protocol):
    async def load(self, shard: int) -> ShardState: ...
    async def record_transition(
        self, shard: int, ip: Address, state: IpState, sequence: int
    ) -> None: ...
```

`record_transition` adds `ip` to the shard's HOT set when `state` is `HOT`,
removes it when `COLD`, and sets the shard's next sequence to
`sequence + 1` — atomically. `MemoryShardStateStore` (reference; dicts,
no TTL) and `RedisShardStateStore` (keys `hammertime:agg:{shard}:hot`, a
SET of IP text, and `hammertime:agg:{shard}:seq`; one MULTI/EXEC
transaction per call; no TTL) are interchangeable behind it, chosen by
`HAMMERTIME_STORE_KIND` exactly as ingest chooses its `DedupStore`.

Claims (`hammertime.aggregator.sharding.assignment.ShardClaims`, the
aggregator's `AssignmentListener`):

* **`on_assigned(p)`** for each new partition: `state = await
  state_store.load(p)`; construct `ShardWindow(shard=p, config=<in force>,
  clock, inherited_hot=state.hot_ips, next_sequence=state.next_sequence,
  max_tracked_ips)`; log `INFO event=shard_claimed shard=p inherited_hot=N`.
  The window counters start empty: they self-heal within one window,
  because every bucket older than the claim has expired by
  `claim + window_seconds`.
* **Warm-up.** Until `claim + window_seconds` the new owner under-counts
  every inherited IP (observations that arrived before the claim are not
  in its ring), so demoting one would be a spurious `HotIpRemoved`.
  Inherited IPs are therefore exempt from HOT -> COLD until
  `warm_until`, whatever the trigger. When the next maintenance sweep
  finds warm-up due, `finish_warmup_if_due()` yields the inherited IPs
  still HOT and each is evaluated once under the config in force with
  `reason="warmup"`; a quiet one is demoted then, a busy one stays. IPs
  this process itself promoted are evaluated normally throughout.
* **`on_revoked(p)`**: under the worker lock (so never mid-message):
  `producer.flush()`, `consumer.commit()`, drop the `ShardWindow`, log
  `INFO event=shard_revoked shard=p`. Nothing is written to the state
  store — it is already current — and nothing is emitted; the next owner
  inherits the HOT set and warms up.
* Readiness: `start()` returns once `subscribe()` has delivered the initial
  assignment. An empty assignment under `auto` is ready (the process is
  healthy, the group has more members than partitions) and logs
  `WARNING event=no_shards_assigned`.

### 6. Maintenance, commit cadence, shutdown

`run_maintenance()` (ADR-0009 decision 3; the same coroutine the periodic
loop calls every `HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S`) does, per
claimed shard, under the worker lock:

1. `for change in window.expire_due()`: if `change.state is HOT`,
   `emitter.evaluate(window, ip, config, reason="expiry")`.
2. `for ip in window.finish_warmup_if_due() or ()`: `emitter.evaluate(...,
   reason="warmup")`.
3. `window.evict_due()`.

Because deltas are non-negative, an observation can only raise a count and
a sweep can only lower one: COLD -> HOT happens on the observation path (or
config re-evaluation), HOT -> COLD only on the sweep, warm-up end, or config
re-evaluation. `hot_to_cold_transitions` is labelled by `reason`
accordingly.

The consumer position is committed every
`HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S` seconds of wall time (default 1.0;
checked after each message), on every `on_revoked`, and at shutdown —
always after `producer.flush()`, so a committed position never precedes the
transitions it produced (the same rule ADR-0010 decision 3 gives the trie).
The interval is an I/O cadence, not domain time: it is measured on the wall
clock even when the service clock is a `ManualClock`.

`stop()` follows ADR-0009 decision 7: stop fetching, finish the in-flight
message, flush, commit, close bus and store clients. No state-store write
is needed at shutdown; the HOT set is always current.

### 7. A configuration change re-evaluates every tracked IP, and the version is visible only afterwards

`reload_config()` / the poll loop apply a document per ADR-0009 decision 6
(strictly greater version; rejected documents ignored). Applying version
`v` means, under the worker lock so that no observation is processed
mid-pass:

1. For each claimed shard: `window.apply_config(v)` (geometry, decision 2).
2. For each claimed shard, for each IP in a snapshot of
   `window.tracked_ips()`: `emitter.evaluate(window, ip, config=v,
   reason="config")`. The warm-up exemption of decision 5 still applies.
   Every `HOT_THRESHOLD`-crossing this produces carries `config_version=v`
   and a `weight` computed under `v`. The pass yields to the event loop
   (`await asyncio.sleep(0)`) every `HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH`
   IPs (default 1000) so the admin endpoints stay responsive; it is not
   otherwise rate-limited — the log between aggregator and trie is the
   back-pressure.
3. Adopt `v` as the config in force; `reload_config()` returns it.

Holding the lock is what makes ADR-0009's rule ("visible in emitted events
only after the re-evaluation it triggered has been applied") hold without
qualification: no event carrying `v` can be emitted before the pass, and no
event can be emitted during it except by the pass. A change confined to
`weight_function`/`weight_max`, `allowed_lateness_seconds` or
`state_retention_seconds` runs the same pass (it is cheap and produces no
transitions) — §34's "no re-evaluation needed" is a statement about
effects, and this keeps one code path.

### 8. Metrics and log events

Maintained as plain counters in `hammertime.aggregator.metrics`
(`AggregatorMetrics`); exporting them on `/metrics` is the telemetry
epic's (§37, ADR-0009 decision 4 says `/metrics` may be empty until then):

```text
tracked_ips{shard}                      ShardWindow.tracked_count
active_ips{shard}                       ShardWindow.active_count
hot_ips{shard}                          ShardWindow.hot_count
cold_to_hot_transitions{shard,config_version,reason}    reason = observation | config
hot_to_cold_transitions{shard,config_version,reason}    reason = expiry | warmup | config
late_messages{reason}                   reason = late | future | expired_bucket
observations_rejected{reason}           reason = window_too_long | malformed   (aggregator-side)
window_evictions{reason}                reason = retention | capacity
shards_claimed                          gauge
```

Structured log events (ADR-0009 decision 5 conventions): `shard_claimed`,
`shard_revoked`, `no_shards_assigned`, `warmup_complete`, `config_applied`
(with `transitions=N`), `malformed_observation`, `store_over_capacity`.

### 9. Settings and module layout

`hammertime.aggregator.config.load_settings(env)` -> `AggregatorSettings`
(ADR-0009 decision 2 pattern) reads: `HAMMERTIME_CONFIG_PATH`,
`HAMMERTIME_BUS_KIND`, `HAMMERTIME_BUS_BROKERS`, `HAMMERTIME_STORE_KIND`,
`HAMMERTIME_REDIS_URL`, `HAMMERTIME_AGGREGATOR_BIND` (ADR-0009),
`HAMMERTIME_SHARD_IDS` (`auto` | set), `HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S`
(ADR-0009, 1.0), `HAMMERTIME_CONFIG_POLL_INTERVAL_S` (ADR-0009, 1.0),
`HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S` (1.0),
`HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS` (1000000),
`HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH` (1000). Shard-id parsing lives in
`config.py` as `parse_shard_ids(text) -> frozenset[int] | None` (`None` for
`auto`).

```text
services/aggregator/src/hammertime/aggregator/
  __init__.py, __main__.py, service.py        ADR-0009 (composition root; wraps the worker)
  config.py                                    AggregatorSettings, load_settings, parse_shard_ids
  lateness.py                                  ObservationOutcome, classify_observation
  window/counter.py                            IpCounter
  window/store.py                              ShardWindow, WindowChange (expiry, retention, capacity)
  sharding/assignment.py                       ShardClaims (AssignmentListener; claim, warm-up, revoke)
  transitions.py                               TransitionEmitter, EmittedTransition
  reevaluate.py                                reevaluate_shard(window, emitter, *, config, ips=None, batch_size=1000) -> int
  worker.py                                    AggregatorWorker: consume loop, handle(message) -> ObservationOutcome,
                                               run_maintenance(), apply_config(), commit cadence
  metrics.py                                   AggregatorMetrics
  tests/                                       test_window.py, test_lateness.py, test_hysteresis.py,
                                               test_sharding.py, test_reevaluate.py, test_worker.py
```

`window/expiry.py` and `sharding/hashing.py` are removed: expiry is a
property of the counter and the store, not a separate job, and there is no
in-repo IP hash (decision 1).

## Assumptions

Each of these is a judgment call not dictated by the epics, the spec or a
prior ADR. Push back on them individually.

1. **Shard = partition; no in-repo hash function.** §20 says "consistent
   hashing: hash(IP) -> shard" and epic #7 asks for "bounded key movement
   when a shard is added/removed". With a fixed partition count no key ever
   moves between shards; what moves on a rebalance is which *worker* owns
   a shard, and how much moves is the group assignor's property (sticky
   assignors minimise it), not ours. The alternative — a custom partitioner
   in `KafkaProducer` so that a Python `shard_for(ip)` is provably the
   partition — would touch ingest's publish path for a function whose only
   consumers would be diagnostics and tests. Rejected as risk without
   functional gain; revisit if a tool needs to compute an IP's shard
   offline.
2. **`HAMMERTIME_SHARD_IDS` keeps its name with `auto` as default; static
   assignment retained.** `auto` is what an HPA-scaled Deployment needs;
   static mode exists for the compose stack, for tests, and for pinning a
   hot shard while debugging. Both because ADR-0009 decision 9 names the
   variable.
3. **`HAMMERTIME_SHARD_COUNT` retired.** No reader, no role once ownership
   is the partitioner's, and its `.env.example` value contradicted
   `topics.py`.
4. **Counters process-local; only the HOT set durable.** Redis-backed
   counters would cost a round trip per observation to avoid, at most, one
   window of under-counting after a restart or handover. The warm-up rule
   turns that under-count into a bounded delay (a demotion deferred by
   up to `window_seconds`, a promotion possibly deferred until the new
   owner's own count crosses `hot_threshold`), never a wrong permanent
   state.
5. **Warm-up = `window_seconds` on the service clock, exempting inherited
   IPs only, whatever the trigger.** IPs this process promoted itself are
   not exempt: a strict analysis shows they can be under-counted by at most
   `allowed_lateness_seconds` of pre-claim observations for a short
   interval before `claim + window_seconds`, which can only cause a
   HOT -> COLD -> HOT flap near `cold_threshold` right after a restart.
   Accepted for simplicity; it also keeps `test_config_change.py`
   (`docs/spec/integration-scenarios.md` §4 step 3) valid, which expects
   config-driven demotions immediately after start. `warm_until` is not
   recomputed if `window_seconds` changes mid-warm-up.
6. **Warm-up exemption is not a re-announce.** Re-emitting `HotIpAdded` for
   every inherited IP at claim time would close the persist-before-publish
   crash window completely (§46.5 makes it idempotent), but would cost the
   trie ~25 `PrefixStatsChanged` per inherited IP on every restart and
   would overwrite `GET /ip`'s transition-time `request_count`/`weight`
   with values the new owner does not have. Deferred; the crash window it
   leaves open is a single awaited publish.
7. **Persist before publish; a store failure aborts the transition.** A
   Redis outage therefore stops transitions (and, propagating out of
   `run()`, terminates the process with exit 1 per ADR-0009 decision 5,
   step 7). Accepted: the alternative — emitting without recording — is the
   permanent-leak path.
8. **Lateness horizon kept as shipped (`window_seconds + allowed_lateness_seconds`, config window), with `expired_bucket` added.**
   With a ring of exactly one window, `allowed_lateness_seconds` cannot
   admit anything the hot path can still use; its remaining effects are
   the dedup TTL (ADR-0003) and where `late_messages{reason="late"}` starts.
   Redefining it (e.g. relative to the agent's window end, which is what
   ingest's TTL arithmetic already assumes) is a real ADR-0002 amendment
   and is deliberately *not* made here; see the hand-off notes.
9. **Diverted observations are republished byte-for-byte.** Keeps
   `event_id` and the key, so a reconciliation consumer can correlate with
   the hot path; a re-encode would be equivalent but slower.
10. **`MALFORMED` is dropped, not diverted.** A message that fails the codec
    or the ADR-0004 invariant cannot be trusted to name the IP it is keyed
    by; forwarding it would propagate the corruption.
11. **`agent_id = "aggregator-shard-{p}"`.** Any non-empty, non-agent string
    satisfies ADR-0003; this one is greppable.
12. **`HotIpRemoved` carries no `attributes`.** §46.5 lets the trie log them
    but forbids storing them; nothing consumes them.
13. **The `shard` payload property stays unpopulated.** The codec would
    need a field for it; the envelope already identifies the shard.
14. **One per-shard sequence counter shared by both event types.** ADR-0003
    allows independent counters per type; sharing one keeps a single
    persisted integer per shard and only produces gaps, which `subject`-
    qualified `event_id`s make harmless.
15. **Commit every 1 s of wall time.** Per-message commits are a broker
    round trip each; a longer interval only lengthens the redelivery span
    after a crash, which is harmless to process-local counters. Same reason
    ADR-0009 gave for its 1 s maintenance interval.
16. **`max_tracked_ips` default 1 000 000 per process; capacity eviction
    takes the least-recently-seen COLD IP.** No spec figure. Chosen as
    roughly 1 GiB of Python objects at ~1 KiB per entry — the same
    defense-in-depth stance as `MemoryDedupStore.max_agents`. Evicting a
    COLD IP early can delay a HOT detection; never evicting a HOT IP is what
    keeps §12 honest.
17. **Re-evaluation batch of 1000 IPs between yields, no rate limit.** The
    `reevaluate.py` stub spoke of "a bounded rate"; a token-bucket would
    delay the harness's expected transitions and the log already
    buffers. Yielding keeps `/readyz` and `/healthz` answering.
18. **Geometry change re-buckets by `bucket_start(S, B')`.** Exact for a
    coarser bucket; for a finer one the whole count lands in the first
    sub-bucket, shifting its expiry earlier by less than the old bucket
    size. Rejecting such documents was the alternative, but every other
    service accepts them and versions must not diverge across services.
19. **A change that needs no re-evaluation still runs the pass.** One code
    path; the pass is O(tracked IPs) of pure arithmetic and produces no
    events.
20. **IPv6 observations are accepted.** §43 says v1 is IPv4-only, but the
    codec, `Address` and the ring are family-agnostic and rejecting would
    need a policy nobody asked for. The trie epic decides what it does with
    them.
21. **Empty assignment is ready.** The alternative (not ready) would make
    scaling past the partition count roll back a deployment.
22. **`InMemoryBus` stays single-partition, single-member-per-group.** No
    scenario needs more; documenting the limit is cheaper than building a
    partitioned memory bus nobody consumes.
23. **Redis keys carry no TTL.** A shard's HOT set must outlive any process;
    an abandoned deployment leaves a bounded number of keys behind
    (documented under Consequences).

## Consequences

* **Bus package** (`hammertime-bus`): `AssignmentListener`, the widened
  `Consumer.subscribe`, `MemoryConsumer` calling the listener with `{(topic,
  0)}`, `KafkaConsumer` constructed without topics and subscribing with a
  `ConsumerRebalanceListener` adapter or `assign()`.
* **Store package** (`hammertime-store`): `ShardState`, `ShardStateStore`,
  `MemoryShardStateStore`, `RedisShardStateStore`. The Redis deployment
  warning in `redis.py`'s docstring applies with more force here: an
  `allkeys-*` eviction policy that dropped `hammertime:agg:*` keys would
  silently recreate the trie leak this ADR closes. Same `maxmemory-policy
  noeviction` requirement, same tracking under the deploy epic (#17).
* **Core package**: `hammertime.core.state.weight` (three pure functions).
  `docs/spec/README.md` maps §46 to it.
* **Trie epic** (restating spec, not adding to it): `HotIpRemoved` for an
  IP the trie does not hold MUST be a no-op that keeps §11's
  `hot_count >= 0`; `HotIpAdded` for an IP it already holds replaces the
  attribute record (§46.5). Both are what makes the recovery paths above
  safe. Whether the trie emits `PrefixStatsChanged` for such no-op events
  is the trie epic's call (ADR-0010 decision 3 says "each applied" event).
* **Environment**: `.env.example` drops `HAMMERTIME_SHARD_COUNT`, sets
  `HAMMERTIME_SHARD_IDS=auto`, and gains
  `HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S`,
  `HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS`,
  `HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH`. `docs/spec/integration-scenarios.md`
  §2.2 drops `HAMMERTIME_SHARD_COUNT` and keeps `HAMMERTIME_SHARD_IDS=0`
  (static, the memory bus's only partition).
* **`CHANGES`** (recorded by the implementing change): the aggregator's
  arrival (consumes observations, emits transitions with `weight`),
  reconciliation of late/future/expired/over-long observations, the
  `HAMMERTIME_SHARD_IDS` redefinition and `HAMMERTIME_SHARD_COUNT` removal,
  the three new environment keys, the Redis `hammertime:agg:*` keyspace.
  None is `BREAKING`: no shipped build read the shard variables and no
  shipped build emitted hot-ip events.
* **Reconciliation topic semantics** sharpen: it now receives every
  observation the hot path could not use, not only those past the
  `allowed_lateness` horizon. `topics.py`'s description of
  `OBSERVATIONS_RECONCILIATION` should be widened to say so (docstring-only).
* **Deferred**: re-announcing the inherited HOT set at claim (assumption 6);
  Redis-backed counters; a `shard` payload field; an ADR-0002 amendment
  making `allowed_lateness_seconds` relative to the agent's window end;
  `InMemoryBus` partitioning; a snapshot of the aggregator's counters for
  faster warm-up.

## Sources

Consulted for the Kafka client behaviour decision 1 relies on; cited so the
reading can be checked against the original.

* aiokafka `AIOKafkaConsumer.subscribe` (source,
  `https://raw.githubusercontent.com/aio-libs/aiokafka/master/aiokafka/consumer/consumer.py`):
  "Partitions will be dynamically assigned via a group coordinator ... This
  method is incompatible with `assign`"; the `listener` argument is a
  `ConsumerRebalanceListener` "which will be called before and after each
  rebalance operation". `assignment()` "may be empty if the assignment
  hasn't happened yet". `commit()` with no arguments "Defaults to current
  consumed offsets for all subscribed partitions". `seek()` should be used
  "either on rebalance listeners or after all pending messages are
  processed".
* aiokafka `ConsumerRebalanceListener` (source,
  `https://raw.githubusercontent.com/aio-libs/aiokafka/master/aiokafka/abc.py`):
  `on_partitions_revoked(revoked)` and `on_partitions_assigned(assigned)`
  receive lists of `TopicPartition` and may be plain functions or
  coroutines — the adapter in `KafkaConsumer` wraps `AssignmentListener`'s
  coroutines.
* aiokafka `AIOKafkaProducer` (source,
  `https://raw.githubusercontent.com/aio-libs/aiokafka/master/aiokafka/producer/producer.py`):
  the `partitioner` argument "Called (after key serialization):
  `partitioner(key_bytes, all_partitions, available_partitions)`", default
  `DefaultPartitioner` — the hook assumption 1 chose not to use.
* The readthedocs rendering of the same API was not reachable from this
  environment; the raw source above is the primary reference.
