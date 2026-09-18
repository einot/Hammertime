"""Every IP maps to exactly one owner; a claim inherits the shard's HOT set.

Spec: section 20 (`hash(IP) -> shard`, one owner per IP), section 26
(retention bounds the store), section 32 (the HOT/COLD state is the
authoritative information), section 47.2 (readiness is "shard claims held").
ADR-0011 decision 1 (a shard *is* a partition of
`hammertime.observations.v1`; `HAMMERTIME_SHARD_IDS` is `auto` or an explicit
set), Amendment 1 item A3 (a set-but-empty value is a configuration error),
decision 5 (`ShardClaims`: claim, warm-up, revoke), Amendment 2 item A5
(an inherited HOT IP is a tracked entry from construction) and item A7 (the
two per-window eviction counters), Amendment 3 item A13 (`window_evictions`
and `shards_claimed` are computed on read from the windows the worker binds
to its `AggregatorMetrics`), Amendment 5 item A19 (a message on a
partition this member does not hold is `UNCLAIMED`: logged, counted under no
series, neither decoded nor diverted, and the store untouched), and
Amendment 6 item A20 (`Consumer.commit` takes an explicit
`Mapping[tuple[str, int], int]` of next offsets to read; a claim keeps a
handled position set by `ShardClaims.mark_handled(message)` to
`message.offset + 1`, and `ShardClaims.commit_handled(partitions=None)` --
flush, then commit those handled positions -- is the aggregator's only
commit path, so a committed position never covers a message that has not
been handled). ADR-0001 Amendment 1 (the consistency model: clause 1, the
owner is a shard and not a process; clause 2, a shard's transition stream is
totally ordered and its identity -- one `agent_id`, one persisted `sequence`
-- is continued, not restarted, across owners, the sequence being strictly
increasing but not promised dense; clause 5, what a handover does to the
stream; clause 6, no double count and no loss at a rebalance) and ADR-0003
Amendment 2 (points 1-3: a redelivery lands in a window that never counted
it, the HOT set is idempotent, and "committed offsets" means the handled
position) are what `TestHandoverBetweenTwoMembers` is written against.

Two interfaces are under test. The first is pinned exactly by decision 9:

    hammertime.aggregator.config.parse_shard_ids(text) -> frozenset[int] | None

(`None` for `auto`; tokens are `n` or `lo-hi` with `lo <= hi`, inclusive,
comma-separated; a set-but-empty value raises, Amendment 1 item A3).

The second, `hammertime.aggregator.sharding.assignment.ShardClaims`, is
described by decision 5 in prose only -- "the aggregator's
`AssignmentListener`" that, per assigned partition, loads the shard's state
and constructs `ShardWindow(shard=p, config=<in force>, clock,
inherited_hot=state.hot_ips, next_sequence=state.next_sequence,
max_tracked_ips)`, and on revocation calls
`commit_handled(<the revoked partitions>)` -- `producer.flush()` then
`consumer.commit(<those partitions' handled positions>)`, Amendment 6 item
A20 -- and drops the window.

ASSUMPTIONS -- constructor and accessor details decision 5 does not pin.
Adjust `_claims`/`_worker` below, not the meaning of the assertions:

1. `ShardClaims(*, state_store, producer, consumer, clock, config,
   max_tracked_ips=1_000_000)`, all keyword-only: exactly the collaborators
   decision 5's two bullets name (the state store it loads from, the producer
   it flushes and the consumer it commits on revoke, the clock and config it
   hands to each `ShardWindow`, and the cap).
2. `claims.window(shard) -> ShardWindow | None` and
   `claims.shards -> frozenset[int]` are how a caller reaches a claimed
   shard's window; `None` / absence is what "drops the `ShardWindow`" means.
   `AggregatorWorker` exposes the same two, plus `claims`, since decision 6's
   `run_maintenance()` iterates "per claimed shard".
3. `AggregatorWorker(*, bus, state_store, clock, config, metrics,
   shard_ids=None, max_tracked_ips=1_000_000)` and
   `await worker.start()` / `await worker.handle(message)` /
   `await worker.run()` / `await worker.run_maintenance()` /
   `await worker.stop()`; `shard_ids=None` is decision 1's `auto`. See
   `test_worker.py`, which states the same assumption.
4. `await worker.on_revoked(partitions)` and `await worker.on_assigned(
   partitions)` are the worker's own `AssignmentListener` methods, forwarding
   to its `ShardClaims` under the worker lock -- the path Amendment 6 item
   A20's trace of the shipped code names ("takes the worker lock through
   `AggregatorWorker.on_revoked`") and the one `test_worker.py::_FetchGap`
   drives. `TestHandoverBetweenTwoMembers` revokes through it so that the
   revoke holds the same lock `handle()` does, which is what decision 5's
   "`on_revoked(p)`: under the worker lock (so never mid-message)" requires
   of a revoke driven from outside the bus.

NOT asserted here, and why:

* **The `shard_claimed` / `shard_revoked` / `no_shards_assigned` log
  records** of decision 8. ADR-0009 decision 5 and section 47.7 fix the event
  names and fields (`shard_claimed shard=p inherited_hot=N`) but not a record
  shape a unit test can assert against without a configured logger -- the
  same reason `packages/hammertime-core/.../tests/test_runtime.py` gives for
  omitting its own lifecycle records. The observable half of A5's claim
  assertion (`inherited_hot=2`) is asserted on the window instead:
  `hot_count == 2`.
* **The `unclaimed_partition` log record** of Amendment 5 item A19 and
  decision 8 (`WARNING`, with the topic, partition and offset), for the same
  reason the `shard_claimed` family is left out above. Its counterpart -- that
  A19's record comes with no counter -- *is* asserted, on the metrics object:
  see `test_a_message_for_a_revoked_partition_is_not_applied`.
* **Real rebalance ordering.** `InMemoryBus` has one partition and no
  coordinator (ADR-0011 assumption 22), so `on_revoked` is only ever driven
  directly here, exactly as `packages/hammertime-bus/.../tests/test_assignment.py`
  notes. `TestHandoverBetweenTwoMembers` does exercise two (and once three)
  members of the `hammertime-aggregator` group over one bus and one state
  store -- but *sequentially*: member A is revoked and stopped before member
  B is constructed, which is inside the memory bus's contract ("at most one
  live member per group per topic", decision 1's `InMemoryBus` bullet). Two
  members holding claims concurrently, and the coordinator's revoke-then-
  assign across them, need a real broker (issue #26).

`BASE` is `1_800_000_000`, the `T0` of `docs/spec/integration-scenarios.md`
section 2. Nothing in this module sleeps for wall time: `_yield_until` and
`_Members.stop_all` only yield to the event loop with `asyncio.sleep(0)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import pytest
from hammertime.aggregator.config import parse_shard_ids
from hammertime.aggregator.lateness import ObservationOutcome
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.sharding.assignment import ShardClaims
from hammertime.aggregator.worker import AggregatorWorker
from hammertime.bus.interface import AssignmentListener, ConsumedMessage, Consumer
from hammertime.bus.memory import InMemoryBus, MemoryProducer
from hammertime.bus.topics import OBSERVATIONS
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import EventPayload, decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpRemoved, Observation, RequestObservation
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import ManualClock
from hammertime.store.interface import ShardState
from hammertime.store.memory import MemoryShardStateStore

HOT_IP_TOPIC = "hammertime.hot-ip.v1"
OBSERVATIONS_TOPIC = OBSERVATIONS.name
# Spelled out rather than imported: section 24's ADR-0011 note and
# docs/spec/integration-scenarios.md section 2.4 both name this literal.
RECONCILIATION_TOPIC = "hammertime.observations-reconciliation.v1"

WINDOW_SECONDS = 300
BUCKET_SECONDS = 10
STATE_RETENTION_SECONDS = 600

BASE = 1_800_000_000
# How long the shard sits unowned between member A's revoke and member B's
# claim in `TestHandoverBetweenTwoMembers`: ADR-0001 Amendment 1 clause 5's
# "rebalance time". Non-zero so that B's `warm_until` is visibly B's claim
# time and not A's start.
HANDOVER_SECONDS = 40

IP_A = Address.parse("198.51.100.1")
IP_B = Address.parse("198.51.100.2")
IP_C = Address.parse("198.51.100.3")

# ADR-0011 decision 4 step 4 / ADR-0003 Amendment 2: the envelope `agent_id`
# of every transition shard 0 emits, whichever worker holds the shard.
SHARD_AGENT_ID = "aggregator-shard-0"


def _config(
    *,
    config_version: int = 1,
    window_seconds: int = WINDOW_SECONDS,
    bucket_seconds: int = BUCKET_SECONDS,
    hot_threshold: int = 1000,
    cold_threshold: int = 800,
    allowed_lateness_seconds: int = 30,
    state_retention_seconds: int = STATE_RETENTION_SECONDS,
) -> DetectionConfig:
    """The shipped defaults of `config/detection.v1.json`, overridable."""

    return DetectionConfig(
        config_version=config_version,
        window_seconds=window_seconds,
        bucket_seconds=bucket_seconds,
        hot_threshold=hot_threshold,
        cold_threshold=cold_threshold,
        allowed_lateness_seconds=allowed_lateness_seconds,
        state_retention_seconds=state_retention_seconds,
    )


DEFAULTS = _config()


class _RecordingProducer(MemoryProducer):
    """A `MemoryProducer` that appends `"flush"` to a shared trace."""

    def __init__(self, bus: InMemoryBus, trace: list[str]) -> None:
        super().__init__(bus)
        self._trace = trace

    async def flush(self) -> None:
        self._trace.append("flush")
        await super().flush()


class _RecordingConsumer:
    """A `Consumer` that appends `"commit"` to a shared trace.

    Delegates everything else to a real `MemoryConsumer`, so nothing here
    depends on that class's constructor; `__getattr__` forwards any member
    this wrapper does not name.
    """

    def __init__(self, inner: Consumer, trace: list[str]) -> None:
        self._inner = inner
        self._trace = trace

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        return await self._inner.subscribe(topic, partitions=partitions, listener=listener)

    async def seek(self, topic: str, partition: int, offset: int) -> None:
        await self._inner.seek(topic, partition, offset)

    async def commit(self, offsets: Mapping[tuple[str, int], int] | None = None) -> None:
        self._trace.append("commit")
        await self._inner.commit(offsets)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _claims_and_stream(
    *,
    clock: ManualClock,
    bus: InMemoryBus | None = None,
    state_store: MemoryShardStateStore | None = None,
    config: DetectionConfig | None = None,
    trace: list[str] | None = None,
    max_tracked_ips: int = 1_000_000,
) -> tuple[ShardClaims, AsyncIterator[ConsumedMessage]]:
    """ASSUMPTION 1 (see the module docstring), with the claim's own stream.

    The inner consumer is subscribed first, the way the worker's own consumer
    is by the time any assignment callback can run -- `on_revoked` commits on
    it, and committing a position that was never taken is not a scenario
    decision 5 describes.

    Its subscription is handed back because Amendment 6 item A20's revocation
    tests have to read on *that* consumer: reading is what moves its consumed
    position past a message the claim has not handled, which is the position
    a bare `consumer.commit()` would have committed.
    """

    resolved_bus = bus if bus is not None else InMemoryBus()
    resolved_trace = trace if trace is not None else []
    inner = resolved_bus.consumer("hammertime-aggregator")
    stream = await inner.subscribe(OBSERVATIONS_TOPIC)
    claims = ShardClaims(
        state_store=state_store if state_store is not None else MemoryShardStateStore(),
        producer=_RecordingProducer(resolved_bus, resolved_trace),
        consumer=_RecordingConsumer(inner, resolved_trace),
        clock=clock,
        config=config if config is not None else DEFAULTS,
        max_tracked_ips=max_tracked_ips,
    )
    return claims, stream


async def _claims(
    *,
    clock: ManualClock,
    bus: InMemoryBus | None = None,
    state_store: MemoryShardStateStore | None = None,
    config: DetectionConfig | None = None,
    trace: list[str] | None = None,
    max_tracked_ips: int = 1_000_000,
) -> ShardClaims:
    """ASSUMPTION 1 (see the module docstring); `_claims_and_stream` without
    the stream, for the tests that never read a message."""

    claims, _stream = await _claims_and_stream(
        clock=clock,
        bus=bus,
        state_store=state_store,
        config=config,
        trace=trace,
        max_tracked_ips=max_tracked_ips,
    )
    return claims


def _worker(
    *,
    bus: InMemoryBus,
    clock: ManualClock,
    state_store: MemoryShardStateStore | None = None,
    config: DetectionConfig | None = None,
    metrics: AggregatorMetrics | None = None,
) -> AggregatorWorker:
    """ASSUMPTION 3 (see the module docstring); mirrored in `test_worker.py`."""

    return AggregatorWorker(
        bus=bus,
        state_store=state_store if state_store is not None else MemoryShardStateStore(),
        clock=clock,
        config=config if config is not None else DEFAULTS,
        metrics=metrics if metrics is not None else AggregatorMetrics(),
        shard_ids=None,
    )


def _observation(ip: Address, count: int, *, window_start: int) -> bytes:
    """One single-entry `RequestObservation`, exactly as ingest publishes it.

    ADR-0004: same header as the accepted batch, `observations=(entry,)`,
    `subject` set to the entry's IP text.
    """

    payload = RequestObservation(
        agent_id="edge-17",
        sequence=1,
        window_start=datetime.fromtimestamp(window_start, tz=UTC),
        window_seconds=BUCKET_SECONDS,
        observations=(Observation(ip=ip, request_count=count),),
    )
    return encode(
        EventEnvelope(
            agent_id="edge-17",
            sequence=1,
            event_type="RequestObservation",
            config_version=1,
            timestamp=datetime.fromtimestamp(window_start, tz=UTC),
            subject=str(ip),
            payload=payload,
        )
    )


async def _take_one(stream: AsyncIterator[ConsumedMessage]) -> ConsumedMessage:
    async for message in stream:
        return message
    raise AssertionError("the subscription ended without yielding a message")


def _records(bus: InMemoryBus, topic: str) -> list[tuple[bytes | None, bytes]]:
    return [(record.key, record.value) for record in bus._logs.get(topic, [])]


def _hot_ip_events(bus: InMemoryBus) -> list[EventEnvelope[EventPayload]]:
    """Every transition on `hammertime.hot-ip.v1`, decoded, in log order."""

    return [decode(value) for _key, value in _records(bus, HOT_IP_TOPIC)]


def _identities(bus: InMemoryBus) -> list[tuple[str, str, str, int]]:
    """`(event_type, subject, agent_id, sequence)` per emitted transition.

    ADR-0003 (amended): `event_id` derives from `(agent_id, sequence,
    event_type, subject)`, so these four are the identity a handover has to
    continue rather than restart (ADR-0001 Amendment 1 clause 2). The
    envelope's `subject` is optional in general (ADR-0004 decision 4), but
    decision 4 step 4 sets `subject=str(ip)` on every transition, so a
    missing one is a failure here.
    """

    identities: list[tuple[str, str, str, int]] = []
    for envelope in _hot_ip_events(bus):
        assert envelope.subject is not None, "a hot-ip event always carries its IP as subject"
        identities.append(
            (envelope.event_type, envelope.subject, envelope.agent_id, envelope.sequence)
        )
    return identities


def _is_hot(worker: AggregatorWorker, ip: Address) -> bool:
    """The predicate a consume-loop test waits on.

    Decision 4 orders one transition as `record_transition` -> publish ->
    `set_state`, so an IP reading HOT means the store write and the hot-ip
    record it asserts on already exist; `is_tracked` would become true at
    `observe`, before any of them.
    """

    window = worker.window(0)
    return window is not None and window.state(ip) is IpState.HOT


async def _yield_until(predicate: Callable[[], bool], *, steps: int = 10_000) -> None:
    """Yield to the event loop until `predicate` holds. Never waits on the wall clock."""

    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was never reached")


class _Feed:
    """Publishes observations and hands each consumed message to a worker.

    Reads on its own consumer group (`test-reader`), as
    `test_worker.py::_Feed` does, so the `hammertime-aggregator` group's
    committed position is decided by the workers alone -- which is the thing
    a handover test observes. `publish` without `deliver` leaves a message
    in the log that no member has fetched.
    """

    def __init__(self, bus: InMemoryBus) -> None:
        self._producer = bus.producer()
        self._consumer = bus.consumer("test-reader")
        self._stream: AsyncIterator[ConsumedMessage] | None = None

    async def publish(self, ip: Address, count: int) -> None:
        await self._producer.publish(
            OBSERVATIONS_TOPIC, key=str(ip), value=_observation(ip, count, window_start=BASE)
        )

    async def deliver(
        self, worker: AggregatorWorker, ip: Address, count: int
    ) -> ObservationOutcome:
        await self.publish(ip, count)
        if self._stream is None:
            self._stream = await self._consumer.subscribe(OBSERVATIONS_TOPIC)
        message = await _take_one(self._stream)
        return await worker.handle(message)


class _Members:
    """The successive members of one `hammertime-aggregator` group.

    One bus, one state store, one clock; a fresh `AggregatorMetrics` per
    member, so a transition can be attributed to the worker that emitted it.
    Members are strictly sequential (see the module docstring): `hand_over`
    revokes and stops a member, and only then may the next be started.
    `stop_all` is the `finally` -- it stops exactly the members still
    running and lets their consume loops return, never sleeping for wall
    time.
    """

    def __init__(
        self, *, bus: InMemoryBus, clock: ManualClock, state_store: MemoryShardStateStore
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._state_store = state_store
        self._running: list[AggregatorWorker] = []
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self, *, metrics: AggregatorMetrics | None = None) -> AggregatorWorker:
        """ASSUMPTION 3: construct and `start()` the next member."""

        worker = _worker(
            bus=self._bus,
            clock=self._clock,
            state_store=self._state_store,
            metrics=metrics if metrics is not None else AggregatorMetrics(),
        )
        await worker.start()
        self._running.append(worker)
        return worker

    def run(self, worker: AggregatorWorker) -> None:
        """ASSUMPTION 3: the member's consume loop, as a task `stop_all` drains."""

        self._tasks.append(asyncio.create_task(worker.run()))

    async def hand_over(self, worker: AggregatorWorker) -> None:
        """Decision 5's `on_revoked` for shard 0 (ASSUMPTION 4), then `stop()`.

        The revoke is what commits the handled position and drops the
        window; `stop()` afterwards is ADR-0009 decision 7's shutdown of a
        member that now holds nothing. The next owner may be started once
        this returns.
        """

        await worker.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await worker.stop()
        self._running = [member for member in self._running if member is not worker]

    async def stop_all(self) -> None:
        for worker in self._running:
            await worker.stop()
        self._running = []
        for _ in range(1_000):
            if all(task.done() for task in self._tasks):
                break
            await asyncio.sleep(0)
        for task in self._tasks:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._tasks = []


class TestParseShardIds:
    """ADR-0011 decision 1 / decision 9: `HAMMERTIME_SHARD_IDS` is the only
    sharding setting -- `auto` (group-managed) or an explicit set."""

    def test_auto_means_group_managed_assignment(self) -> None:
        assert parse_shard_ids("auto") is None

    def test_a_single_id(self) -> None:
        assert parse_shard_ids("0") == frozenset({0})

    def test_an_inclusive_range(self) -> None:
        assert parse_shard_ids("0-3") == frozenset({0, 1, 2, 3})

    def test_a_mixture_of_ids_and_ranges(self) -> None:
        assert parse_shard_ids("0,2,5-7") == frozenset({0, 2, 5, 6, 7})

    def test_a_single_element_range_is_that_element(self) -> None:
        # "inclusive ranges" with `lo <= hi`; `3-3` is not a special case.
        assert parse_shard_ids("3-3") == frozenset({3})

    def test_the_result_is_a_frozenset(self) -> None:
        parsed = parse_shard_ids("0-3")
        assert isinstance(parsed, frozenset)

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "a", "3-1", "-1"],
        ids=["empty", "whitespace-only", "not-a-number", "descending-range", "negative"],
    )
    def test_a_rejected_value_raises(self, text: str) -> None:
        # Amendment 1 item A3 rules the two empty cases: "`parse_shard_ids("")`
        # and `parse_shard_ids("   ")` raise `ValueError`", so that
        # `load_settings` can exit 2 before any bus, store or socket is opened.
        # An *unset* variable still means `auto`; set-but-empty does not.
        with pytest.raises(ValueError):
            parse_shard_ids(text)


class TestShardClaimsIsTheAssignmentListener:
    """Decision 5: `ShardClaims` is "the aggregator's `AssignmentListener`",
    the protocol ADR-0011 decision 1 adds to the bus."""

    async def test_it_satisfies_the_assignment_listener_protocol(self) -> None:
        claims = await _claims(clock=ManualClock(initial=BASE))
        assert isinstance(claims, AssignmentListener)


class TestClaimingAShard:
    """Decision 5, `on_assigned`: load the shard's state, build its window."""

    async def test_a_claim_creates_a_window_for_the_partition(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.shard == 0
        assert claims.shards == frozenset({0})

    async def test_a_never_claimed_shard_starts_empty_and_never_warms_up(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.tracked_count == 0
        assert window.hot_ips() == frozenset()
        assert window.next_sequence == 0
        # "warm_until is clock.now() + window_seconds at construction iff
        # inherited_hot is non-empty".
        assert window.warm_until is None
        assert window.in_warmup is False

    async def test_a_claim_inherits_the_persisted_hot_set_and_sequence(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 4)
        await state_store.record_transition(0, IP_B, IpState.HOT, 9)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.hot_ips() == frozenset({IP_A, IP_B})
        assert window.next_sequence == 10

    async def test_every_inherited_ip_is_a_tracked_entry_from_construction(self) -> None:
        # Amendment 2 item A5, and the observable half of the `shard_claimed
        # ... inherited_hot=2` log record.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        await state_store.record_transition(0, IP_B, IpState.HOT, 1)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.tracked_count == 2
        assert window.hot_count == 2
        # Not active: an inherited IP's ring is empty until its first applied
        # observation, so its total is 0.
        assert window.active_count == 0
        assert window.is_inherited(IP_A) is True
        assert window.is_inherited(IP_B) is True
        assert window.state(IP_A) is IpState.HOT
        assert window.total(IP_A) == 0

    async def test_warm_up_runs_for_one_window_from_the_claim(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.warm_until == BASE + WINDOW_SECONDS
        assert window.in_warmup is True

    async def test_the_claim_time_is_the_clock_not_the_process_start(self) -> None:
        # A later claim warms up from *its* moment: `warm_until` is
        # `clock.now() + window_seconds` at construction.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, state_store=state_store)
        clock.advance(1_000)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.warm_until == BASE + 1_000 + WINDOW_SECONDS

    async def test_several_partitions_are_claimed_independently(self) -> None:
        # Section 20: a shard owns its own state. Two claimed partitions get
        # two windows, each seeded from its own `ShardState`.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        await state_store.record_transition(1, IP_B, IpState.HOT, 3)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))

        assert claims.shards == frozenset({0, 1})
        first = claims.window(0)
        second = claims.window(1)
        assert first is not None
        assert second is not None
        assert first.shard == 0
        assert second.shard == 1
        assert first.hot_ips() == frozenset({IP_A})
        assert second.hot_ips() == frozenset({IP_B})
        assert first.next_sequence == 1
        assert second.next_sequence == 4

    async def test_an_empty_initial_assignment_claims_nothing(self) -> None:
        # Decision 5 / assumption 21: an empty *group-managed* assignment is a
        # healthy steady state (the group has more members than partitions),
        # not an error -- the process holds zero shards and carries on.
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)

        await claims.on_assigned(frozenset())

        assert claims.shards == frozenset()
        assert claims.window(0) is None


class TestRevokingAShard:
    """Decision 5, `on_revoked`: `commit_handled(<the revoked partitions>)` --
    `producer.flush()`, then `consumer.commit()` of each revoked partition's
    handled position (Amendment 6 item A20) -- then drop the window. Nothing is
    written to the state store -- it is already current -- and nothing is
    emitted."""

    async def test_a_revoke_flushes_before_it_commits(self) -> None:
        # The ordering rule decision 6 states for every commit: "always after
        # `producer.flush()`, so a committed position never precedes the
        # transitions it produced".
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        claims = await _claims(clock=clock, trace=trace)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        trace.clear()

        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert trace == ["flush", "commit"]

    async def test_a_revoke_drops_the_window(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert claims.window(0) is None
        assert claims.shards == frozenset()

    async def test_a_revoke_writes_nothing_to_the_state_store(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        before = await state_store.load(0)

        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert await state_store.load(0) == before

    async def test_a_revoke_emits_nothing(self) -> None:
        # "nothing is emitted; the next owner inherits the HOT set and warms
        # up" -- an inherited IP is not demoted on the way out.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, bus=bus, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert _records(bus, HOT_IP_TOPIC) == []

    async def test_revoking_one_shard_leaves_the_other_claimed(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))

        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 1)}))

        assert claims.shards == frozenset({0})
        assert claims.window(0) is not None
        assert claims.window(1) is None

    async def test_a_reclaim_after_a_revoke_inherits_what_was_recorded(self) -> None:
        # The handover path: the next owner loads the same durable HOT set and
        # warms up again from its own claim.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 2)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        clock.advance(50)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.hot_ips() == frozenset({IP_A})
        assert window.next_sequence == 3
        assert window.warm_until == BASE + 50 + WINDOW_SECONDS


class TestTheRevocationCommitIsTheHandledPosition:
    """Amendment 6 item A20, and decision 5's `mark_handled` /
    `commit_handled` bullets: what a revocation commits is the claim's
    *handled* position -- `message.offset + 1` for the last message the
    worker finished under this claim -- and nothing else.

    Each test below reads one message on the claim's *own* consumer, which is
    what advances that consumer's consumed position past it (A20's "the gap,
    traced in the shipped code", step 2: the memory bus sets
    `_positions[topic] = offset + 1` before it yields). The observable
    difference is what a fresh consumer for the `hammertime-aggregator` group
    is handed afterwards -- the same way
    `test_worker.py::TestOffsetsAreCommittedAtShutdown` observes a commit.
    """

    async def _publish_two(self, bus: InMemoryBus) -> None:
        producer = bus.producer()
        await producer.publish(
            OBSERVATIONS_TOPIC, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
        )
        await producer.publish(
            OBSERVATIONS_TOPIC, key=str(IP_B), value=_observation(IP_B, 1200, window_start=BASE)
        )

    async def test_an_unhandled_message_is_left_for_the_next_owner(self) -> None:
        # The loss A20 closes: the claim fetched the message and never marked
        # it handled, so the revocation commit names no position for the
        # partition and the next owner starts at or before it. A bare
        # `consumer.commit()` here would commit `offset + 1` and nobody would
        # ever process this observation.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, stream = await _claims_and_stream(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        fetched = await _take_one(stream)
        assert fetched.key == str(IP_A).encode()

        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        next_owner = bus.consumer("hammertime-aggregator")
        received = await _take_one(await next_owner.subscribe(OBSERVATIONS_TOPIC))
        assert received.offset == fetched.offset
        assert received.key == str(IP_A).encode()

    async def test_a_handled_message_moves_the_next_owner_past_it(self) -> None:
        # The converse, and what keeps the commit a commit: once
        # `mark_handled` has recorded `offset + 1`, the revocation commits
        # exactly that and the next owner resumes after the message.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, stream = await _claims_and_stream(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        fetched = await _take_one(stream)
        claims.mark_handled(fetched)

        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        next_owner = bus.consumer("hammertime-aggregator")
        received = await _take_one(await next_owner.subscribe(OBSERVATIONS_TOPIC))
        assert received.offset == fetched.offset + 1
        assert received.key == str(IP_B).encode()

    async def test_commit_handled_flushes_and_commits_even_with_nothing_handled(self) -> None:
        # A20: `commit_handled` is "`producer.flush()` followed by
        # `consumer.commit(...)`", and "both calls are made even when the
        # mapping is empty" -- so decision 6's "every commit is preceded by a
        # flush" holds without a "every non-empty commit" qualifier.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        claims = await _claims(clock=clock, trace=trace)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        trace.clear()

        await claims.commit_handled()

        assert trace == ["flush", "commit"]

    async def test_mark_handled_for_an_unheld_partition_is_a_key_error(self) -> None:
        # A20: "a partition this object does not hold is a `KeyError`" -- the
        # choice Amendment 2 item A15 made for `set_state` on an untracked IP.
        # A claim also "loses it when revoked", which is why the message
        # fetched under the old claim cannot be marked handled after the fact.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, stream = await _claims_and_stream(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        fetched = await _take_one(stream)
        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        with pytest.raises(KeyError):
            claims.mark_handled(fetched)


class TestInheritedRetention:
    """Amendment 2 item A5: an inherited IP's `last_seen` is `clock.now()` at
    construction, so retention runs from the claim -- an IP demoted at warm-up
    end becomes evictable at `claim + state_retention_seconds`, the same
    deadline an IP observed at claim time would get."""

    async def _claimed_window(self, clock: ManualClock) -> ShardClaims:
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        return claims

    async def test_a_demoted_inherited_ip_survives_until_the_claim_plus_retention(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await self._claimed_window(clock)
        window = claims.window(0)
        assert window is not None

        clock.advance(WINDOW_SECONDS)
        assert window.finish_warmup_if_due() == frozenset({IP_A})
        window.set_state(IP_A, IpState.COLD)  # what the warm-up evaluation does

        clock.advance(STATE_RETENTION_SECONDS - WINDOW_SECONDS - 1)  # claim + 599
        window.expire_due()
        assert window.evict_due() == 0
        assert window.is_tracked(IP_A) is True

    async def test_a_demoted_inherited_ip_is_evicted_at_the_claim_plus_retention(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await self._claimed_window(clock)
        window = claims.window(0)
        assert window is not None

        clock.advance(WINDOW_SECONDS)
        window.finish_warmup_if_due()
        window.set_state(IP_A, IpState.COLD)

        clock.advance(STATE_RETENTION_SECONDS - WINDOW_SECONDS)  # claim + 600
        window.expire_due()
        assert window.evict_due() == 1
        assert window.is_tracked(IP_A) is False
        assert window.tracked_count == 0

    async def test_an_inherited_ip_still_hot_is_never_evicted(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await self._claimed_window(clock)
        window = claims.window(0)
        assert window is not None

        clock.advance(STATE_RETENTION_SECONDS * 2)
        window.expire_due()

        assert window.evict_due() == 0
        assert window.is_tracked(IP_A) is True


class TestEvictionCountersArePerClaimedShard:
    """Amendment 2 item A7: `window_evictions{shard,reason}` is read from the
    two counters of each claimed shard, so the series lives and dies with the
    window. Amendment 3 item A13 pins the read path A7 left open -- the series
    is computed on each `metrics.get`, from the windows the worker bound --
    so the last test here reads it through `AggregatorMetrics`."""

    async def test_each_claimed_shard_counts_its_own_evictions(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))
        first = claims.window(0)
        second = claims.window(1)
        assert first is not None
        assert second is not None

        assert first.observe(IP_A, BASE, 5) is not None
        clock.advance(STATE_RETENTION_SECONDS)
        first.expire_due()
        first.evict_due()

        assert first.retention_evictions == 1
        assert first.capacity_evictions == 0
        assert second.retention_evictions == 0
        assert second.capacity_evictions == 0

    async def test_a_revoked_shards_counters_go_away_with_its_window(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        window = claims.window(0)
        assert window is not None
        assert window.observe(IP_A, BASE, 5) is not None
        clock.advance(STATE_RETENTION_SECONDS)
        window.expire_due()
        window.evict_due()
        assert window.retention_evictions == 1

        await claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert claims.window(0) is None

    async def test_a_claimed_shards_counters_are_readable_through_the_metrics(self) -> None:
        # A13: `AggregatorWorker.__init__` binds the claimed windows as the
        # source of the derived series, and `get` computes the answer from
        # them on the call -- so an eviction needs no bookkeeping of its own to
        # become readable, and `shards_claimed` is the number of windows the
        # bound source yields.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        worker = _worker(bus=bus, clock=clock, metrics=metrics)

        await worker.start()
        try:
            window = worker.window(0)
            assert window is not None
            assert window.observe(IP_A, BASE, 5) is not None
            clock.advance(STATE_RETENTION_SECONDS)
            window.expire_due()
            assert window.evict_due() == 1

            assert metrics.get("window_evictions", shard=0, reason="retention") == 1
            assert metrics.get("window_evictions", shard=0, reason="capacity") == 0
            assert metrics.get("shards_claimed") == 1
        finally:
            await worker.stop()


class TestTheWorkerClaimsShardZero:
    """ADR-0009's readiness rule made observable: `start()` returns once
    `subscribe()` has delivered the initial assignment (decision 1), which on
    the single-partition `InMemoryBus` is always `{(topic, 0)}`."""

    async def test_start_completes_with_shard_zero_claimed(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        worker = _worker(bus=bus, clock=clock)

        await worker.start()

        try:
            assert worker.shards == frozenset({0})
            assert worker.window(0) is not None
        finally:
            await worker.stop()

    async def test_the_claim_inherits_the_shards_persisted_hot_set(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 6)
        worker = _worker(bus=bus, clock=clock, state_store=state_store)

        await worker.start()

        try:
            window = worker.window(0)
            assert window is not None
            assert window.hot_ips() == frozenset({IP_A})
            assert window.next_sequence == 7
            assert window.in_warmup is True
        finally:
            await worker.stop()

    async def test_a_message_for_a_revoked_partition_is_not_applied(self) -> None:
        # Decision 1: the aggregator "learns an IP's shard from
        # `ConsumedMessage.partition`". Once that partition's window is gone
        # there is nothing to apply the delta to, and nothing may be emitted
        # or diverted on its behalf. Amendment 5 item A19 pins the rest: the
        # outcome is `UNCLAIMED`, the seventh member -- the message is not
        # this member's to judge, so it is neither decoded nor counted under
        # any series, and in particular is *not* `MALFORMED`.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        reader = bus.consumer("test-reader")
        stream = await reader.subscribe(OBSERVATIONS_TOPIC)

        await worker.claims.on_revoked(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        assert worker.window(0) is None

        await bus.producer().publish(
            OBSERVATIONS_TOPIC, key=str(IP_C), value=_observation(IP_C, 1200, window_start=BASE)
        )
        message = await _take_one(stream)
        assert message.partition == 0
        outcome = await worker.handle(message)

        try:
            assert outcome is ObservationOutcome.UNCLAIMED
            assert worker.window(0) is None
            assert _records(bus, HOT_IP_TOPIC) == []
            assert _records(bus, RECONCILIATION_TOPIC) == []
            # A19: no counter increment under any series. The message is well
            # formed, so `observations_rejected{reason=malformed}` -- a signal
            # about producers -- must not tick on a rebalance; the series keeps
            # its two reasons, and this message is neither of them.
            assert metrics.get("observations_rejected", reason="malformed") == 0
            assert metrics.get("observations_rejected", reason="window_too_long") == 0
            for reason in ("late", "future", "expired_bucket"):
                assert metrics.get("late_messages", reason=reason) == 0
        finally:
            await worker.stop()


class TestHandoverBetweenTwoMembers:
    """Shard 0 changes hands from worker A to worker B over one bus and one
    state store: ADR-0011 decision 1 (a shard is a partition; keys never
    move, workers move between shards via claim/revoke) made observable end
    to end, and the handover clauses of ADR-0001 Amendment 1 (1, 2, 5, 6)
    and ADR-0003 Amendment 2 (points 1-3) checked against what B actually
    inherits, applies and emits.

    The two members are sequential -- A is revoked (through the worker, so
    the lock is held) and stopped before B is constructed -- which is the
    only shape the memory bus supports (module docstring, "Real rebalance
    ordering"). The clock is advanced by `HANDOVER_SECONDS` between the two
    so that B's claim time is distinguishable from A's.

    Deliberately not asserted anywhere in this class: what `handle()` does
    when given the same message twice under one live claim. ADR-0003
    Amendment 2 says a byte-identical message handed to `handle()` twice
    within one claim "is not a supported input" and that "no test should pin
    what happens if a caller does it directly". Every redelivery below
    crosses a claim boundary.
    """

    async def _first_owner_records_ip_a(
        self, a: AggregatorWorker, feed: _Feed, bus: InMemoryBus
    ) -> None:
        """Member A handles m1 -- `IP_A`, 1200 requests at `BASE` -- and, per
        decision 4, records `IP_A` HOT in the store (`sequence` 0) and
        announces it once. Every test in this class starts from here."""

        outcome = await feed.deliver(a, IP_A, 1200)
        assert outcome is ObservationOutcome.APPLIED
        window = a.window(0)
        assert window is not None
        assert window.state(IP_A) is IpState.HOT
        assert _identities(bus) == [("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0)]

    async def test_the_next_owner_inherits_the_hot_set_the_first_owner_recorded(self) -> None:
        # ADR-0011 decision 5 `on_assigned`: B loads shard 0's `ShardState`
        # and builds its window with `inherited_hot=state.hot_ips`,
        # `next_sequence=state.next_sequence`; Amendment 2 item A5 makes the
        # inherited IP a tracked HOT entry with an empty ring; `warm_until`
        # is `clock.now() + window_seconds` at *B's* construction. ADR-0001
        # Amendment 1 clause 5: "the new owner inherits the shard's durable
        # HOT set, exempts inherited IPs from demotion for `window_seconds`".
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)

            b = await members.start()

            assert a.window(0) is None
            window = b.window(0)
            assert window is not None
            assert window.hot_ips() == frozenset({IP_A})
            assert window.next_sequence == 1
            assert window.is_inherited(IP_A) is True
            assert window.in_warmup is True
            assert window.warm_until == BASE + HANDOVER_SECONDS + WINDOW_SECONDS
            # A5: inherited means an empty ring -- A's 1200 did not travel.
            assert window.total(IP_A) == 0
            assert window.state(IP_A) is IpState.HOT
            # Decision 5 `on_revoked` / assumption 6: the handover itself
            # emitted nothing -- no demotion on the way out, no re-announce
            # on the way in.
            assert _identities(bus) == [("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0)]
        finally:
            await members.stop_all()

    async def test_the_next_owner_resumes_at_the_first_owners_handled_position_and_applies_what_follows(  # noqa: E501
        self,
    ) -> None:
        # Amendment 6 item A20 / ADR-0003 Amendment 2 point 3: A's revoke
        # commits its *handled* position -- `offset + 1` of m1, i.e. 1 -- so
        # B's consume loop starts at m2, which A never fetched. ADR-0001
        # Amendment 1 clause 2: B continues shard 0's `agent_id` and
        # `sequence` where A left them -- continued, not restarted; strictly
        # increasing, not promised dense -- so no `event_id` is reused across
        # the handover. The exact values, #0 then #1, follow from decision 4
        # step 1 (`sequence = window.next_sequence`, then `+= 1`), decision 5
        # (`next_sequence=state.next_sequence` at B's claim) and Amendment 1
        # item A2 (the store holds `sequence + 1` after A's record). Clause 6:
        # m1 is not counted again (B's ring for IP_A stays empty) and m2 is
        # not lost.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)  # m1, offset 0, handled
            await feed.publish(IP_B, 1200)  # m2, offset 1; A never fetches it
            await members.hand_over(a)  # commits {(topic, 0): 1}
            clock.advance(HANDOVER_SECONDS)

            b = await members.start()
            members.run(b)
            await _yield_until(lambda: _is_hot(b, IP_B))

            window = b.window(0)
            assert window is not None
            assert window.total(IP_B) == 1200
            assert window.state(IP_B) is IpState.HOT
            # B resumed *at* position 1, not before it: m1 was not re-read
            # into B's window, whose entry for IP_A is still the inherited
            # one with an empty ring.
            assert window.total(IP_A) == 0
            assert window.is_inherited(IP_A) is True
            assert _identities(bus) == [
                ("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0),
                ("HotIpAdded", str(IP_B), SHARD_AGENT_ID, 1),
            ]
            assert len({envelope.event_id for envelope in _hot_ip_events(bus)}) == 2
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A, IP_B}), next_sequence=2
            )
        finally:
            await members.stop_all()

    async def test_an_ip_that_stays_busy_across_a_handover_is_announced_exactly_once(self) -> None:
        # ADR-0011 decision 3, last paragraph: the HOT set "is idempotent
        # under redelivery: an IP the store already has as HOT is not
        # re-announced"; ADR-0003 Amendment 2 point 2 says the same of the
        # durable set. B counts IP_A from scratch (A5: the ring starts empty)
        # and reaches `hot_threshold` again, but IP_A is already HOT in the
        # window it inherited, so there is no COLD -> HOT edge to emit.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)
            b = await members.start()

            outcome = await feed.deliver(b, IP_A, 1200)

            assert outcome is ObservationOutcome.APPLIED
            window = b.window(0)
            assert window is not None
            assert window.total(IP_A) == 1200
            assert window.state(IP_A) is IpState.HOT
            for_ip_a = [
                envelope.event_type
                for envelope in _hot_ip_events(bus)
                if envelope.subject == str(IP_A)
            ]
            assert for_ip_a == ["HotIpAdded"]
            assert len(_records(bus, HOT_IP_TOPIC)) == 1
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A}), next_sequence=1
            )
        finally:
            await members.stop_all()

    async def test_the_next_owner_does_not_demote_an_inherited_ip_before_its_warm_up_ends(
        self,
    ) -> None:
        # Decision 5, warm-up: "Inherited IPs are therefore exempt from
        # HOT -> COLD until `warm_until`, whatever the trigger." Two triggers
        # that would each demote IP_A outside warm-up are fired inside it.
        # First an observation of 5 -- well below `cold_threshold` -- on the
        # observation path, Amendment 2 item A11's HOT -> COLD with
        # `reason="observation"`. Then, one second before `warm_until`, a
        # sweep in which that bucket has left the window (BASE + 339: bucket
        # age 330 >= 300), so `expire_due()` reports IP_A's total dropping to
        # 0 and decision 6 step 1 evaluates it with `reason="expiry"`. Neither
        # may demote: the under-count is B's, not the IP's. (A bare sweep with
        # an empty ring would evaluate nothing and prove nothing.)
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        b_metrics = AggregatorMetrics()
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)
            b = await members.start(metrics=b_metrics)
            window = b.window(0)
            assert window is not None

            outcome = await feed.deliver(b, IP_A, 5)

            assert outcome is ObservationOutcome.APPLIED
            assert window.total(IP_A) == 5
            assert window.state(IP_A) is IpState.HOT
            assert window.is_inherited(IP_A) is True

            clock.advance(WINDOW_SECONDS - 1)  # BASE + 339: the bucket at BASE has expired
            await b.run_maintenance()

            assert window.total(IP_A) == 0
            assert [envelope.event_type for envelope in _hot_ip_events(bus)] == ["HotIpAdded"]
            assert window.state(IP_A) is IpState.HOT
            assert window.is_inherited(IP_A) is True
            assert window.in_warmup is True
            for reason in ("observation", "expiry", "warmup"):
                demotions = b_metrics.get(
                    "hot_to_cold_transitions", shard=0, config_version=1, reason=reason
                )
                assert demotions == 0
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A}), next_sequence=1
            )
        finally:
            await members.stop_all()

    async def test_the_next_owner_demotes_a_quiet_inherited_ip_after_warm_up_with_the_shards_next_sequence(  # noqa: E501
        self,
    ) -> None:
        # Decision 5: at `warm_until` the sweep's `finish_warmup_if_due()`
        # yields the inherited IPs still HOT and each is evaluated once with
        # `reason="warmup"`; decision 6 orders that step inside
        # `run_maintenance()`; decision 8 labels the counter
        # `hot_to_cold_transitions{shard,config_version,reason="warmup"}`.
        # ADR-0001 Amendment 1 clause 2: the `HotIpRemoved` carries shard 0's
        # `agent_id` and the *next* sequence after A's `HotIpAdded` -- the
        # counter B loaded from the store -- so the two events, from two
        # workers, have distinct `event_id`s and form one ordered stream;
        # clause 5: the demotion arrives `window_seconds` plus the rebalance
        # time after A last counted the IP, and no earlier.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        a_metrics = AggregatorMetrics()
        b_metrics = AggregatorMetrics()
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start(metrics=a_metrics)
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)
            b = await members.start(metrics=b_metrics)

            clock.advance(WINDOW_SECONDS)
            await b.run_maintenance()

            assert _identities(bus) == [
                ("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0),
                ("HotIpRemoved", str(IP_A), SHARD_AGENT_ID, 1),
            ]
            events = _hot_ip_events(bus)
            assert len({envelope.event_id for envelope in events}) == 2
            removed = events[1].payload
            assert isinstance(removed, HotIpRemoved)
            assert removed.ip == IP_A
            assert removed.sequence == 1
            assert removed.window_count == 0  # B never counted IP_A
            assert removed.attributes is None  # decision 4 step 3
            assert await state_store.load(0) == ShardState(hot_ips=frozenset(), next_sequence=2)
            window = b.window(0)
            assert window is not None
            assert window.state(IP_A) is IpState.COLD
            assert window.is_inherited(IP_A) is False
            assert window.in_warmup is False
            assert (
                b_metrics.get("hot_to_cold_transitions", shard=0, config_version=1, reason="warmup")
                == 1
            )
            # The demotion is B's: A's counters never saw it.
            assert (
                a_metrics.get("hot_to_cold_transitions", shard=0, config_version=1, reason="warmup")
                == 0
            )
        finally:
            await members.stop_all()

    async def test_a_third_owner_continues_the_sequence(self) -> None:
        # After B's warm-up demotion the store holds no HOT IP and
        # `next_sequence == 2`. Decision 5: C inherits exactly that -- an
        # empty set, so "`warm_until` is `clock.now() + window_seconds` at
        # construction iff `inherited_hot` is non-empty" gives no warm-up --
        # and the shard's next transition, whoever emits it, would be #2
        # (ADR-0001 Amendment 1 clause 2; ADR-0011 Amendment 1 item A2: the
        # persisted counter never moves backwards).
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)
            b = await members.start()
            clock.advance(WINDOW_SECONDS)
            await b.run_maintenance()
            assert await state_store.load(0) == ShardState(hot_ips=frozenset(), next_sequence=2)
            await members.hand_over(b)
            clock.advance(HANDOVER_SECONDS)

            c = await members.start()

            assert b.window(0) is None
            window = c.window(0)
            assert window is not None
            assert window.next_sequence == 2
            assert window.hot_ips() == frozenset()
            assert window.tracked_count == 0
            assert window.warm_until is None
            assert window.in_warmup is False
            # Two handovers, two workers' worth of transitions, one stream.
            assert _identities(bus) == [
                ("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0),
                ("HotIpRemoved", str(IP_A), SHARD_AGENT_ID, 1),
            ]
        finally:
            await members.stop_all()
