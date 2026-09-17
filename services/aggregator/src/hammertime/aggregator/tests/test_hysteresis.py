"""The COLD/HOT table from spec section 6, case by case, through the emitter.

Spec: section 6 (the transition table and its worked example), section 7 (the
dead band), section 19 (the aggregator emits `HotIpAdded`/`HotIpRemoved`),
section 30 (`evaluate_ip_state` is called from exactly one place), section 38
(core invariants), section 46.4 (`weight`), section 46.5 (`HotIpRemoved`
carries no attributes). ADR-0003 amendment and ADR-0004 (envelope identity:
`agent_id`, `sequence`, `subject`), ADR-0005 (`attributes`), ADR-0011
decisions 4 and 5 (persist before publish, warm-up exemption) and decision 8
(the transition counters and their `reason` label sets, as amended by
Amendment 2 item A11). Wire shape: `schemas/hot_ip_event.v1.json`.

The interface under test is ADR-0011 decision 4, quoted here in full because
the module does not exist yet and these tests are what it is written against:

    @dataclass(frozen=True, slots=True)
    class EmittedTransition:
        ip: Address
        transition: StateTransition
        sequence: int
        window_count: int
        config_version: int

    class TransitionEmitter:
        def __init__(
            self, *, producer: Producer, state_store: ShardStateStore,
            clock: Clock, metrics: AggregatorMetrics
        ) -> None: ...
        async def evaluate(
            self, window: ShardWindow, ip: Address, *, config: DetectionConfig, reason: str
        ) -> EmittedTransition | None: ...

and decision 4's five ordered steps: take `window.next_sequence` (and
increment it), `await state_store.record_transition(...)` *before* the event
exists, build the payload, publish the envelope to
`TOPICS["hammertime.hot-ip.v1"]` under `key_selector(payload)`, then
`window.set_state(ip, new)` and count the transition under `reason`.

ASSUMPTIONS -- things decision 4 does not pin. Each is a judgment call;
adjust the test, not the meaning, if the implementation settles them
differently:

1. **`AggregatorMetrics`' read surface.** Decision 8 names the counters and
   their labels (`cold_to_hot_transitions{shard,config_version,reason}`,
   `hot_to_cold_transitions{shard,config_version,reason}`, ...) but no Python
   API for reading one back. Assumed: `AggregatorMetrics()` takes no required
   arguments and `metrics.get(name, **labels) -> int` returns the value of a
   series, `0` for one that was never touched. Only `_counter()` below and
   the handful of call sites it has depend on that spelling.
2. **`EmittedTransition.transition`'s type.** `StateTransition` is named by
   decision 4's code block and defined nowhere in the spec, the ADRs or any
   shipped test, so nothing below names a member of it. The direction of a
   transition is asserted through the emitted `event_type` and through the
   two directions comparing unequal, which holds for any enum/value type the
   implementation picks.
3. **The `shard_claimed`/`warmup_complete` log records of decision 8 are not
   asserted** here or in `test_sharding.py`: ADR-0009 decision 5 and section
   47.7 fix the event names and fields but not the record shape a unit test
   could assert against without a configured logger, which is the same reason
   `packages/hammertime-core/.../tests/test_runtime.py` gives for omitting
   its lifecycle records.

`BASE` is `1_800_000_000`, the `T0` of `docs/spec/integration-scenarios.md`
section 2 -- a multiple of 300, hence of every bucket size used here, so the
bucket arithmetic in the assertions stays exact. Bus records are read off
`bus._logs` and decoded with `hammertime.core.events.codec.decode`, the
precedent set by `services/ingest/.../tests/test_pipeline.py::_topic_records`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.transitions import EmittedTransition, TransitionEmitter
from hammertime.aggregator.window.store import ShardWindow
from hammertime.bus.memory import InMemoryBus
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import EventPayload, decode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded, HotIpRemoved
from hammertime.core.state.enums import IpState
from hammertime.core.state.weight import threshold_ratio
from hammertime.core.time.clock import ManualClock
from hammertime.store.memory import MemoryShardStateStore

# The one pinned topic literal in this repo (see
# packages/hammertime-bus/.../tests/test_topics.py).
HOT_IP_TOPIC = "hammertime.hot-ip.v1"

# Section 5's worked geometry: window 300 s, bucket 10 s.
WINDOW_SECONDS = 300
BUCKET_SECONDS = 10

# `T0` from docs/spec/integration-scenarios.md section 2.
BASE = 1_800_000_000

IP_A = Address.parse("198.51.100.1")
IP_B = Address.parse("198.51.100.2")

SHARD = 0
AGENT_ID = f"aggregator-shard-{SHARD}"


def _config(
    *,
    config_version: int = 1,
    window_seconds: int = WINDOW_SECONDS,
    bucket_seconds: int = BUCKET_SECONDS,
    hot_threshold: int = 1000,
    cold_threshold: int = 800,
    allowed_lateness_seconds: int = 30,
    state_retention_seconds: int = 600,
    weight_max: int = 1_000_000,
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
        weight_max=weight_max,
    )


DEFAULTS = _config()


class _FailingShardStateStore(MemoryShardStateStore):
    """A state store whose `record_transition` always raises.

    Mirrors `services/ingest/.../tests/test_pipeline.py::_FailingProducer`.
    The named endpoint exists so a test can assert it never leaks.
    """

    async def record_transition(
        self, shard: int, ip: Address, state: IpState, sequence: int
    ) -> None:
        raise RuntimeError("simulated failure: redis hammertime-store.internal:6379 unreachable")


def _window(
    *,
    clock: ManualClock,
    config: DetectionConfig | None = None,
    inherited_hot: tuple[Address, ...] = (),
    next_sequence: int = 0,
) -> ShardWindow:
    return ShardWindow(
        shard=SHARD,
        config=config if config is not None else DEFAULTS,
        clock=clock,
        inherited_hot=inherited_hot,
        next_sequence=next_sequence,
    )


def _emitter(
    *,
    bus: InMemoryBus,
    clock: ManualClock,
    state_store: MemoryShardStateStore | None = None,
    metrics: AggregatorMetrics | None = None,
) -> TransitionEmitter:
    return TransitionEmitter(
        producer=bus.producer(),
        state_store=state_store if state_store is not None else MemoryShardStateStore(),
        clock=clock,
        metrics=metrics if metrics is not None else AggregatorMetrics(),
    )


def _records(bus: InMemoryBus, topic: str = HOT_IP_TOPIC) -> list[tuple[bytes | None, bytes]]:
    """Every `(key, value)` on `topic`, read straight off the bus's log."""

    return [(record.key, record.value) for record in bus._logs.get(topic, [])]


def _decoded(
    bus: InMemoryBus, topic: str = HOT_IP_TOPIC
) -> list[tuple[bytes | None, EventEnvelope[EventPayload]]]:
    return [(key, decode(value)) for key, value in _records(bus, topic)]


def _only(bus: InMemoryBus) -> tuple[bytes | None, EventEnvelope[EventPayload]]:
    decoded = _decoded(bus)
    assert len(decoded) == 1, f"expected exactly one hot-ip record, got {len(decoded)}"
    return decoded[0]


def _counter(metrics: AggregatorMetrics, name: str, **labels: object) -> int:
    """ASSUMPTION 1 (see the module docstring): the metrics read surface."""

    return metrics.get(name, **labels)


def _seed(window: ShardWindow, ip: Address, *, count: int, state: IpState, at: int = BASE) -> None:
    """Give `ip` a window total of `count` and a previous state of `state`."""

    assert window.observe(ip, at, count) is not None
    window.set_state(ip, state)


class TestSectionSixTableThroughTheEmitter:
    """Section 6's worked example, row by row, with
    `hot_threshold = 1000` / `cold_threshold = 800`.

    Decision 4: `evaluate` "returns `None` when nothing changed", and only a
    real edge reaches the store, the bus and the counters.
    """

    @pytest.mark.parametrize(
        ("previous", "count"),
        [
            (IpState.COLD, 799),
            (IpState.COLD, 800),
            (IpState.COLD, 999),
            (IpState.HOT, 1000),
            (IpState.HOT, 900),
            (IpState.HOT, 800),
        ],
        ids=["cold-799", "cold-800", "cold-999", "hot-1000", "hot-900", "hot-800"],
    )
    async def test_a_row_that_changes_nothing_emits_nothing(
        self, previous: IpState, count: int
    ) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        emitter = _emitter(bus=bus, clock=clock, state_store=state_store)
        window = _window(clock=clock)
        _seed(window, IP_A, count=count, state=previous)

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        assert result is None
        assert _records(bus) == []
        assert window.state(IP_A) is previous
        # No edge, no sequence consumed and nothing written durably.
        assert window.next_sequence == 0
        persisted = await state_store.load(SHARD)
        assert persisted.hot_ips == frozenset()
        assert persisted.next_sequence == 0

    async def test_cold_at_the_hot_threshold_becomes_hot(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock)
        _seed(window, IP_A, count=1000, state=IpState.COLD)

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        assert isinstance(result, EmittedTransition)
        assert result.ip == IP_A
        assert result.sequence == 0
        assert result.window_count == 1000
        assert result.config_version == DEFAULTS.config_version
        assert window.state(IP_A) is IpState.HOT
        _key, envelope = _only(bus)
        assert envelope.event_type == "HotIpAdded"

    async def test_hot_below_the_cold_threshold_becomes_cold(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock)
        _seed(window, IP_A, count=799, state=IpState.HOT)

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="expiry")

        assert isinstance(result, EmittedTransition)
        assert result.ip == IP_A
        assert result.window_count == 799
        assert window.state(IP_A) is IpState.COLD
        _key, envelope = _only(bus)
        assert envelope.event_type == "HotIpRemoved"

    async def test_the_two_directions_are_distinguishable_on_the_transition_field(self) -> None:
        # ASSUMPTION 2: `StateTransition`'s members are not named anywhere, so
        # only the inequality of the two directions is asserted.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock)

        _seed(window, IP_A, count=1000, state=IpState.COLD)
        promotion = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")
        _seed(window, IP_B, count=799, state=IpState.HOT)
        demotion = await emitter.evaluate(window, IP_B, config=DEFAULTS, reason="expiry")

        assert promotion is not None
        assert demotion is not None
        assert promotion.transition != demotion.transition


class TestTheEmittedAddEvent:
    """What a COLD -> HOT edge puts on `hammertime.hot-ip.v1`.

    Identity per ADR-0003's amendment and ADR-0004 (`agent_id` is the
    producing shard, `sequence` is the shard's own counter, `subject` is the
    IP); payload per `schemas/hot_ip_event.v1.json` and section 46.4.
    """

    async def _promote(
        self, *, count: int = 1200, config: DetectionConfig | None = None
    ) -> tuple[InMemoryBus, ShardWindow, EmittedTransition]:
        cfg = config if config is not None else DEFAULTS
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock, config=cfg)
        _seed(window, IP_A, count=count, state=IpState.COLD)
        result = await emitter.evaluate(window, IP_A, config=cfg, reason="observation")
        assert result is not None
        return bus, window, result

    async def test_exactly_one_record_keyed_by_the_ip(self) -> None:
        bus, _, _result = await self._promote()

        key, envelope = _only(bus)

        # `key_selector` for the hot-ip topic is `str(event.ip)`.
        assert key == str(IP_A).encode()
        assert envelope.subject == str(IP_A)

    async def test_the_envelope_names_the_producing_shard_not_an_agent(self) -> None:
        bus, _, _result = await self._promote()

        _key, envelope = _only(bus)

        assert envelope.event_type == "HotIpAdded"
        assert envelope.agent_id == AGENT_ID
        assert envelope.sequence == 0
        assert envelope.config_version == DEFAULTS.config_version

    async def test_the_envelope_and_payload_timestamps_are_the_service_clock(self) -> None:
        # Decision 4 step 3: `timestamp` is
        # `datetime.fromtimestamp(clock.now(), tz=UTC)`, and step 4 reuses
        # "the same timestamp" on the envelope.
        bus, _, _result = await self._promote()

        _key, envelope = _only(bus)
        payload = envelope.payload
        assert isinstance(payload, HotIpAdded)

        expected = datetime.fromtimestamp(BASE, tz=UTC)
        assert envelope.timestamp == expected
        assert payload.timestamp == expected

    async def test_the_payload_carries_the_window_count_and_config_version(self) -> None:
        bus, _, _result = await self._promote(count=1200)

        _key, envelope = _only(bus)
        payload = envelope.payload
        assert isinstance(payload, HotIpAdded)

        assert payload.ip == IP_A
        assert payload.sequence == 0
        assert payload.window_count == 1200
        assert payload.config_version == DEFAULTS.config_version

    async def test_the_payload_carries_the_section_46_4_attributes(self) -> None:
        # Issue #48 / section 46.4: `weight` is `threshold_ratio(count,
        # config)`; 1200 requests against `hot_threshold` 1000 is 1200
        # thousandths of the threshold.
        bus, _, _result = await self._promote(count=1200)

        _key, envelope = _only(bus)
        payload = envelope.payload
        assert isinstance(payload, HotIpAdded)

        attributes = payload.attributes
        assert attributes is not None
        assert attributes == {"attributes_version": 1, "weight": 1200}
        assert attributes["weight"] == threshold_ratio(1200, DEFAULTS)

    async def test_the_weight_is_computed_under_the_config_in_force(self) -> None:
        # The same 600 requests are 1200 thousandths of a `hot_threshold` of
        # 500 -- the unit is thousandths *of the threshold*, not of 1000.
        config = _config(config_version=2, hot_threshold=500, cold_threshold=400)
        bus, _, _result = await self._promote(count=600, config=config)

        _key, envelope = _only(bus)
        payload = envelope.payload
        assert isinstance(payload, HotIpAdded)

        assert payload.window_count == 600
        assert payload.config_version == 2
        assert envelope.config_version == 2
        attributes = payload.attributes
        assert attributes is not None
        assert attributes == {"attributes_version": 1, "weight": 1200}
        assert attributes["weight"] == threshold_ratio(600, config)


class TestTheEmittedRemoveEvent:
    """Section 46.5 / ADR-0011 assumption 12: `HotIpRemoved` carries no
    attributes -- the trie is forbidden from storing them and nothing
    consumes them."""

    async def test_a_demotion_carries_no_attributes_and_the_current_count(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock)
        _seed(window, IP_A, count=500, state=IpState.HOT)

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="expiry")

        assert result is not None
        assert result.window_count == 500
        key, envelope = _only(bus)
        payload = envelope.payload
        assert isinstance(payload, HotIpRemoved)
        assert key == str(IP_A).encode()
        assert envelope.event_type == "HotIpRemoved"
        assert envelope.agent_id == AGENT_ID
        assert payload.ip == IP_A
        assert payload.window_count == 500
        assert payload.attributes is None

    async def test_a_demotion_of_an_emptied_window_reports_zero(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock)
        _seed(window, IP_A, count=1200, state=IpState.HOT)
        clock.advance(WINDOW_SECONDS + BUCKET_SECONDS)
        window.expire_due()

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="expiry")

        assert result is not None
        assert result.window_count == 0
        _key, envelope = _only(bus)
        payload = envelope.payload
        assert isinstance(payload, HotIpRemoved)
        assert payload.window_count == 0


class TestPersistBeforePublish:
    """Decision 4 step 2: "the durable HOT set is updated **before** the event
    exists"; a failure there aborts the transition.

    Why it matters (decision 4's own reasoning): the opposite order can leave
    the trie holding an IP no owner knows about -- the permanent section 12
    leak ADR-0011 exists to close.
    """

    async def test_the_hot_set_and_sequence_are_recorded_for_a_promotion(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        emitter = _emitter(bus=bus, clock=clock, state_store=state_store)
        window = _window(clock=clock)
        _seed(window, IP_A, count=1200, state=IpState.COLD)

        await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        state = await state_store.load(SHARD)
        assert IP_A in state.hot_ips
        assert state.next_sequence == 1

    async def test_a_demotion_removes_the_ip_from_the_durable_hot_set(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        emitter = _emitter(bus=bus, clock=clock, state_store=state_store)
        window = _window(clock=clock)
        _seed(window, IP_A, count=1200, state=IpState.COLD)
        await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        clock.advance(WINDOW_SECONDS + BUCKET_SECONDS)
        window.expire_due()
        await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="expiry")

        state = await state_store.load(SHARD)
        assert state.hot_ips == frozenset()
        assert state.next_sequence == 2

    async def test_a_store_failure_publishes_nothing_and_propagates(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock, state_store=_FailingShardStateStore())
        window = _window(clock=clock)
        _seed(window, IP_A, count=1200, state=IpState.COLD)

        with pytest.raises(RuntimeError):
            await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        assert _records(bus) == []
        # "the transition is not emitted and the state is not changed in
        # memory (the sequence number is consumed; gaps are fine)".
        assert window.state(IP_A) is IpState.COLD
        assert window.next_sequence == 1

    async def test_a_store_failure_does_not_leak_the_store_endpoint(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock, state_store=_FailingShardStateStore())
        window = _window(clock=clock)
        _seed(window, IP_A, count=1200, state=IpState.COLD)

        with pytest.raises(RuntimeError) as caught:
            await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        # The exception propagates as-is (decision 4); what must not happen is
        # a *published* event describing a transition the store refused.
        assert "hammertime-store.internal" in str(caught.value)
        assert _records(bus) == []


class TestSequenceNumbering:
    """ADR-0011 decision 4 / assumption 14: one per-shard counter shared by
    both event types, persisted (decision 5) so it never restarts at 0 and can
    never reproduce an earlier `event_id`."""

    async def test_consecutive_transitions_number_from_zero(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock)

        _seed(window, IP_A, count=1200, state=IpState.COLD)
        first = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")
        _seed(window, IP_B, count=1200, state=IpState.COLD)
        second = await emitter.evaluate(window, IP_B, config=DEFAULTS, reason="observation")

        assert first is not None
        assert second is not None
        assert (first.sequence, second.sequence) == (0, 1)
        sequences = [envelope.sequence for _key, envelope in _decoded(bus)]
        assert sequences == [0, 1]
        assert window.next_sequence == 2

    async def test_a_claim_resumes_from_the_persisted_sequence(self) -> None:
        # Decision 5: the window is constructed with the `next_sequence` the
        # state store loaded, so the first transition after a restart
        # continues the shard's numbering rather than restarting it.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        emitter = _emitter(bus=bus, clock=clock, state_store=state_store)
        window = _window(clock=clock, next_sequence=7)
        _seed(window, IP_A, count=1200, state=IpState.COLD)

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        assert result is not None
        assert result.sequence == 7
        _key, envelope = _only(bus)
        assert envelope.sequence == 7
        assert window.next_sequence == 8
        assert (await state_store.load(SHARD)).next_sequence == 8

    async def test_two_shards_at_the_same_sequence_produce_distinct_event_ids(self) -> None:
        # ADR-0004: `subject` participates in `event_id`, so two transitions
        # that share `(agent_id, sequence, event_type)` but name different IPs
        # are still distinct events. Without subject-qualification the two
        # windows below would collide.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        first_window = _window(clock=clock)
        second_window = _window(clock=clock)

        _seed(first_window, IP_A, count=1200, state=IpState.COLD)
        _seed(second_window, IP_B, count=1200, state=IpState.COLD)
        first = await emitter.evaluate(first_window, IP_A, config=DEFAULTS, reason="observation")
        second = await emitter.evaluate(second_window, IP_B, config=DEFAULTS, reason="observation")

        assert first is not None
        assert second is not None
        assert first.sequence == second.sequence == 0
        event_ids = [envelope.event_id for _key, envelope in _decoded(bus)]
        assert len(event_ids) == 2
        assert event_ids[0] != event_ids[1]


class TestWarmUpExemption:
    """Decision 5: an inherited IP is exempt from HOT -> COLD until
    `warm_until`, "whatever the trigger" -- the new owner under-counts it for
    one window, so demoting it would be a spurious `HotIpRemoved`."""

    async def test_an_inherited_ip_is_not_demoted_during_warm_up(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        emitter = _emitter(bus=bus, clock=clock, state_store=state_store)
        window = _window(clock=clock, inherited_hot=(IP_A,))

        assert window.in_warmup is True
        assert window.total(IP_A) == 0

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        assert result is None
        assert _records(bus) == []
        assert window.state(IP_A) is IpState.HOT
        # "with no side effects": no sequence consumed, nothing written.
        assert window.next_sequence == 0
        assert (await state_store.load(SHARD)).next_sequence == 0

    async def test_the_exemption_holds_for_every_trigger(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock, inherited_hot=(IP_A,))

        for reason in ("observation", "expiry", "config"):
            assert await emitter.evaluate(window, IP_A, config=DEFAULTS, reason=reason) is None

        assert _records(bus) == []
        assert window.state(IP_A) is IpState.HOT

    async def test_a_quiet_inherited_ip_is_demoted_once_warm_up_ends(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock, inherited_hot=(IP_A,))

        clock.advance(WINDOW_SECONDS)
        assert window.finish_warmup_if_due() == frozenset({IP_A})

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="warmup")

        assert result is not None
        assert result.window_count == 0
        _key, envelope = _only(bus)
        payload = envelope.payload
        assert isinstance(payload, HotIpRemoved)
        assert envelope.event_type == "HotIpRemoved"
        assert payload.window_count == 0
        assert payload.attributes is None
        assert window.state(IP_A) is IpState.COLD

    async def test_a_busy_inherited_ip_survives_warm_up(self) -> None:
        # "a quiet one is demoted then, a busy one stays."
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock, inherited_hot=(IP_A,))
        clock.advance(WINDOW_SECONDS - BUCKET_SECONDS)
        assert window.observe(IP_A, BASE + WINDOW_SECONDS - BUCKET_SECONDS, 1200) is not None
        clock.advance(BUCKET_SECONDS)
        assert window.finish_warmup_if_due() == frozenset({IP_A})

        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="warmup")

        assert result is None
        assert _records(bus) == []
        assert window.state(IP_A) is IpState.HOT

    async def test_an_ip_this_process_promoted_is_demoted_during_warm_up(self) -> None:
        # Decision 5 / assumption 5: "IPs this process itself promoted are
        # evaluated normally throughout" -- the exemption is for inherited IPs
        # only, even while the window as a whole is still warming up.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock, inherited_hot=(IP_A,))
        _seed(window, IP_B, count=500, state=IpState.HOT)

        assert window.in_warmup is True
        assert window.is_inherited(IP_B) is False

        result = await emitter.evaluate(window, IP_B, config=DEFAULTS, reason="observation")

        assert result is not None
        assert result.ip == IP_B
        assert result.window_count == 500
        _key, envelope = _only(bus)
        assert envelope.event_type == "HotIpRemoved"
        assert envelope.subject == str(IP_B)
        assert window.state(IP_B) is IpState.COLD

    async def test_an_inherited_ip_may_still_be_promoted_during_warm_up(self) -> None:
        # The exemption is one-directional: it suppresses HOT -> COLD only.
        # An inherited IP is already HOT, so the readable form of "promotion
        # is unaffected" is a re-promoted IP: demote it after warm-up ends,
        # then let it cross the hot threshold again.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = _window(clock=clock, inherited_hot=(IP_A,))
        clock.advance(WINDOW_SECONDS)
        window.finish_warmup_if_due()
        await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="warmup")
        assert window.state(IP_A) is IpState.COLD

        assert window.observe(IP_A, BASE + WINDOW_SECONDS, 1200) is not None
        result = await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation")

        assert result is not None
        assert result.sequence == 1
        assert [envelope.event_type for _key, envelope in _decoded(bus)] == [
            "HotIpRemoved",
            "HotIpAdded",
        ]


class TestTransitionCounters:
    """Decision 8 as amended by Amendment 2 item A11: COLD -> HOT is labelled
    `observation | config`; HOT -> COLD is labelled
    `observation | expiry | warmup | config`.

    See ASSUMPTION 1 in the module docstring for the read surface these four
    tests depend on.
    """

    @pytest.mark.parametrize("reason", ["observation", "config"])
    async def test_a_promotion_is_counted_under_its_reason(self, reason: str) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        emitter = _emitter(bus=bus, clock=clock, metrics=metrics)
        window = _window(clock=clock)
        _seed(window, IP_A, count=1200, state=IpState.COLD)

        await emitter.evaluate(window, IP_A, config=DEFAULTS, reason=reason)

        assert (
            _counter(
                metrics,
                "cold_to_hot_transitions",
                shard=SHARD,
                config_version=DEFAULTS.config_version,
                reason=reason,
            )
            == 1
        )

    @pytest.mark.parametrize("reason", ["observation", "expiry", "warmup", "config"])
    async def test_a_demotion_is_counted_under_its_reason(self, reason: str) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        emitter = _emitter(bus=bus, clock=clock, metrics=metrics)
        window = _window(clock=clock)
        _seed(window, IP_A, count=500, state=IpState.HOT)

        await emitter.evaluate(window, IP_A, config=DEFAULTS, reason=reason)

        assert (
            _counter(
                metrics,
                "hot_to_cold_transitions",
                shard=SHARD,
                config_version=DEFAULTS.config_version,
                reason=reason,
            )
            == 1
        )

    async def test_a_non_transition_counts_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        emitter = _emitter(bus=bus, clock=clock, metrics=metrics)
        window = _window(clock=clock)
        _seed(window, IP_A, count=900, state=IpState.HOT)

        assert await emitter.evaluate(window, IP_A, config=DEFAULTS, reason="observation") is None

        for name in ("cold_to_hot_transitions", "hot_to_cold_transitions"):
            assert (
                _counter(
                    metrics,
                    name,
                    shard=SHARD,
                    config_version=DEFAULTS.config_version,
                    reason="observation",
                )
                == 0
            )

    async def test_the_counters_carry_the_config_version_of_the_transition(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        emitter = _emitter(bus=bus, clock=clock, metrics=metrics)
        config = _config(config_version=4)
        window = _window(clock=clock, config=config)
        _seed(window, IP_A, count=1200, state=IpState.COLD)

        await emitter.evaluate(window, IP_A, config=config, reason="config")

        assert (
            _counter(
                metrics, "cold_to_hot_transitions", shard=SHARD, config_version=4, reason="config"
            )
            == 1
        )
        assert (
            _counter(
                metrics, "cold_to_hot_transitions", shard=SHARD, config_version=1, reason="config"
            )
            == 0
        )
