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
to its `AggregatorMetrics`), and Amendment 5 item A19 (a message on a
partition this member does not hold is `UNCLAIMED`: logged, counted under no
series, neither decoded nor diverted, and the store untouched).

Two interfaces are under test. The first is pinned exactly by decision 9:

    hammertime.aggregator.config.parse_shard_ids(text) -> frozenset[int] | None

(`None` for `auto`; tokens are `n` or `lo-hi` with `lo <= hi`, inclusive,
comma-separated; a set-but-empty value raises, Amendment 1 item A3).

The second, `hammertime.aggregator.sharding.assignment.ShardClaims`, is
described by decision 5 in prose only -- "the aggregator's
`AssignmentListener`" that, per assigned partition, loads the shard's state
and constructs `ShardWindow(shard=p, config=<in force>, clock,
inherited_hot=state.hot_ips, next_sequence=state.next_sequence,
max_tracked_ips)`, and on revocation calls `producer.flush()` then
`consumer.commit()` and drops the window.

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
   `await worker.stop()`; `shard_ids=None` is decision 1's `auto`. See
   `test_worker.py`, which states the same assumption.

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
  notes.

`BASE` is `1_800_000_000`, the `T0` of `docs/spec/integration-scenarios.md`
section 2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
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
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import Observation, RequestObservation
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import ManualClock
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

IP_A = Address.parse("198.51.100.1")
IP_B = Address.parse("198.51.100.2")
IP_C = Address.parse("198.51.100.3")


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

    async def commit(self) -> None:
        self._trace.append("commit")
        await self._inner.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _claims(
    *,
    clock: ManualClock,
    bus: InMemoryBus | None = None,
    state_store: MemoryShardStateStore | None = None,
    config: DetectionConfig | None = None,
    trace: list[str] | None = None,
    max_tracked_ips: int = 1_000_000,
) -> ShardClaims:
    """ASSUMPTION 1 (see the module docstring).

    The inner consumer is subscribed first, the way the worker's own consumer
    is by the time any assignment callback can run -- `on_revoked` commits on
    it, and committing a position that was never taken is not a scenario
    decision 5 describes.
    """

    resolved_bus = bus if bus is not None else InMemoryBus()
    resolved_trace = trace if trace is not None else []
    inner = resolved_bus.consumer("hammertime-aggregator")
    await inner.subscribe(OBSERVATIONS_TOPIC)
    return ShardClaims(
        state_store=state_store if state_store is not None else MemoryShardStateStore(),
        producer=_RecordingProducer(resolved_bus, resolved_trace),
        consumer=_RecordingConsumer(inner, resolved_trace),
        clock=clock,
        config=config if config is not None else DEFAULTS,
        max_tracked_ips=max_tracked_ips,
    )


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
    """Decision 5, `on_revoked`: `producer.flush()`, `consumer.commit()`, drop
    the window. Nothing is written to the state store -- it is already
    current -- and nothing is emitted."""

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
