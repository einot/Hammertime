"""One consumed observation, end to end, in process.

Spec: section 19 (the aggregator consumes `RequestObservation` and emits
`HotIpAdded`/`HotIpRemoved`), section 20 (the message's partition is the
shard that owns the IP), section 24 (what happens to an observation the hot
path cannot use), section 25 (bucket arithmetic), section 26 (retention),
section 30 (the processing algorithm), section 37 (the counters and their
labels). ADR-0002 (event time), ADR-0003 (at-least-once consumption),
ADR-0004 (one single-entry message per IP, keyed and `subject`-tagged by it),
ADR-0009 decisions 3, 7 and 9 (`run_maintenance()`, shutdown, the
`hammertime-aggregator` group), ADR-0010 decision 6 (one bucket per
observation, over-long windows), ADR-0011 decision 3 (the six outcomes and
the worker's three steps), decision 6 (maintenance order, commit cadence,
shutdown), decision 8 (metrics) and Amendment 2 items A9 (flooring) and A11
(a demotion on the observation path).

Decision 3 is the specification this file is written against. Per consumed
message:

1. `codec.decode`; the payload MUST be a `RequestObservation` with exactly one
   entry whose IP text equals the envelope `subject` and the message key. Any
   failure, including `CodecError`, is `MALFORMED`: logged, counted, skipped.
   A poison message never stops the consumer.
2. `classify_observation(...)` with `window_start` as the whole-second floor
   of the payload's aware `datetime`, `now = clock.now()` and the config in
   force. `LATE`, `FUTURE`, `EXPIRED_BUCKET` and `WINDOW_TOO_LONG` are
   diverted -- the consumed bytes republished unchanged, under the same key,
   to `hammertime.observations-reconciliation.v1` -- and counted. The window
   store is not touched.
3. `APPLIED`: `window.observe(ip, bucket_start(window_start,
   window.config.bucket_seconds), request_count)` on the `ShardWindow` of
   `message.partition`, then the transition evaluation with
   `reason="observation"`, which may yield a HOT -> COLD (A11).

ASSUMPTIONS -- things decision 3, decision 6 and decision 9 do not pin. Each
is a judgment call; adjust the helpers below, not the meaning of the
assertions. The first three are shared with `test_sharding.py` and
`test_reevaluate.py`:

1. `AggregatorWorker(*, bus, state_store, clock, config, metrics,
   shard_ids=None, max_tracked_ips=1_000_000)`, all keyword-only, taking the
   whole `bus` rather than a pre-built producer/consumer pair -- the same
   choice ADR-0009 made for `build_service` ("takes an `InMemoryBus`, not a
   `Producer`/`Consumer`") and the same one
   `services/ingest/.../tests/test_pipeline.py::_build_app` relies on, so a
   test can keep its own reference and read every topic's log back.
   `shard_ids=None` is decision 1's `auto`.
2. `await worker.start()` (claims, per decision 1 "subscribe() does not
   return until on_assigned has been awaited"), `await worker.run()` (the
   consume loop), `await worker.stop()` (ADR-0009 decision 7),
   `await worker.handle(message) -> ObservationOutcome` (decision 9 spells
   this one out), `await worker.run_maintenance()` (ADR-0009 decision 3),
   `worker.window(shard) -> ShardWindow | None`, `worker.shards`,
   `worker.config`.
3. `AggregatorMetrics()` takes no required arguments and
   `metrics.get(name, **labels) -> int` reads one series back, `0` for one
   that was never touched. Decision 8 names the series and their labels but
   no Python API.

NOT asserted here, and why:

* **Which `bucket_seconds` the worker floors with** (A9: "the target window's
  `config.bucket_seconds`, not the service's config in force"). A9's own
  assumptions say the two are identical outside decision 7's locked pass and
  identical inside it as well, so there is no state in which the choice is
  observable; what is asserted is that the floor happens and uses the
  geometry in force.
* **The exact-horizon outcome and bucket-versus-raw age.**
  `test_lateness.py` owns those; every age used below (0, 310, 400) is
  unambiguous under either reading.
* **The `malformed_observation` log record** of decision 3 step 1 and
  decision 8. ADR-0009 decision 5 and section 47.7 fix the event name and
  fields but not a record shape a unit test can assert against without a
  configured logger -- the same reason
  `packages/hammertime-core/.../tests/test_runtime.py` gives for omitting its
  own lifecycle records. What is asserted is the counter and that the
  consumer survives.
* **The 1 s commit cadence itself.** Decision 6 measures it on the *wall*
  clock ("an I/O cadence, not domain time"), and nothing here may sleep for
  wall time, so what is asserted is the other two commit points decision 6
  names: `on_revoked` (`test_sharding.py`) and shutdown (below).

`BASE` is `1_800_000_000`, the `T0` of `docs/spec/integration-scenarios.md`
section 2 -- a multiple of 300, so every bucket boundary below is exact.
Nothing in this module sleeps for wall time: `_yield_until` only yields to
the event loop with `asyncio.sleep(0)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from hammertime.aggregator.lateness import ObservationOutcome
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.window.store import ShardWindow
from hammertime.aggregator.worker import AggregatorWorker
from hammertime.bus.interface import ConsumedMessage
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import OBSERVATIONS
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import EventPayload, decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded, HotIpRemoved, Observation, RequestObservation
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import ManualClock
from hammertime.store.memory import MemoryShardStateStore

HOT_IP_TOPIC = "hammertime.hot-ip.v1"
OBSERVATIONS_TOPIC = OBSERVATIONS.name
# Spelled out rather than imported: section 24's ADR-0011 note and
# docs/spec/integration-scenarios.md section 2.4 both name this literal.
RECONCILIATION_TOPIC = "hammertime.observations-reconciliation.v1"
# ADR-0009 decision 9: one fixed consumer group for the aggregator.
GROUP = "hammertime-aggregator"

WINDOW_SECONDS = 300
BUCKET_SECONDS = 10
STATE_RETENTION_SECONDS = 600
ALLOWED_LATENESS_SECONDS = 30

BASE = 1_800_000_000

IP_A = Address.parse("10.20.30.1")
IP_B = Address.parse("10.20.30.2")
IP_C = Address.parse("10.20.30.3")

AGENT_ID = "edge-17"
SHARD_AGENT_ID = "aggregator-shard-0"


def _config(
    *,
    config_version: int = 1,
    window_seconds: int = WINDOW_SECONDS,
    bucket_seconds: int = BUCKET_SECONDS,
    hot_threshold: int = 1000,
    cold_threshold: int = 800,
    allowed_lateness_seconds: int = ALLOWED_LATENESS_SECONDS,
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


def _observation(
    ip: Address,
    count: int,
    *,
    window_start: float = BASE,
    window_seconds: int = BUCKET_SECONDS,
    subject: str | None = None,
    sequence: int = 1,
    entries: tuple[tuple[Address, int], ...] | None = None,
) -> bytes:
    """One message shaped exactly as `services/ingest/publisher.py` shapes it.

    ADR-0004: a single-entry `RequestObservation` carrying the accepted
    batch's header, with the envelope's `subject` set to the entry's IP text.
    `entries` and `subject` are overridable so a test can build the two
    producer-invariant violations decision 3 step 1 names.
    """

    observations = entries if entries is not None else ((ip, count),)
    timestamp = datetime.fromtimestamp(window_start, tz=UTC)
    payload = RequestObservation(
        agent_id=AGENT_ID,
        sequence=sequence,
        window_start=timestamp,
        window_seconds=window_seconds,
        observations=tuple(
            Observation(ip=entry_ip, request_count=entry_count)
            for entry_ip, entry_count in observations
        ),
    )
    return encode(
        EventEnvelope(
            agent_id=AGENT_ID,
            sequence=sequence,
            event_type="RequestObservation",
            config_version=1,
            timestamp=timestamp,
            subject=subject if subject is not None else str(ip),
            payload=payload,
        )
    )


def _hot_ip_event(ip: Address) -> bytes:
    """A well-formed envelope whose payload is not a `RequestObservation`."""

    timestamp = datetime.fromtimestamp(BASE, tz=UTC)
    return encode(
        EventEnvelope(
            agent_id=SHARD_AGENT_ID,
            sequence=0,
            event_type="HotIpAdded",
            config_version=1,
            timestamp=timestamp,
            subject=str(ip),
            payload=HotIpAdded(
                ip=ip, timestamp=timestamp, sequence=0, window_count=1200, config_version=1
            ),
        )
    )


def _records(bus: InMemoryBus, topic: str) -> list[tuple[bytes | None, bytes]]:
    return [(record.key, record.value) for record in bus._logs.get(topic, [])]


def _decoded(
    bus: InMemoryBus, topic: str = HOT_IP_TOPIC
) -> list[tuple[bytes | None, EventEnvelope[EventPayload]]]:
    return [(key, decode(value)) for key, value in _records(bus, topic)]


def _counter(metrics: AggregatorMetrics, name: str, **labels: object) -> int:
    """ASSUMPTION 3 (see the module docstring): the metrics read surface."""

    return metrics.get(name, **labels)


def _worker(
    *,
    bus: InMemoryBus,
    clock: ManualClock,
    metrics: AggregatorMetrics,
    state_store: MemoryShardStateStore | None = None,
    config: DetectionConfig = DEFAULTS,
) -> AggregatorWorker:
    """ASSUMPTION 1 (see the module docstring)."""

    return AggregatorWorker(
        bus=bus,
        state_store=state_store if state_store is not None else MemoryShardStateStore(),
        clock=clock,
        config=config,
        metrics=metrics,
        shard_ids=None,
    )


@asynccontextmanager
async def _running(worker: AggregatorWorker) -> AsyncIterator[AggregatorWorker]:
    await worker.start()
    try:
        yield worker
    finally:
        await worker.stop()


def _window_of(worker: AggregatorWorker) -> ShardWindow:
    window = worker.window(0)
    assert window is not None, "the memory bus always assigns partition 0"
    return window


async def _take_one(stream: AsyncIterator[ConsumedMessage]) -> ConsumedMessage:
    async for message in stream:
        return message
    raise AssertionError("the subscription ended without yielding a message")


async def _yield_until(predicate: Callable[[], bool], *, steps: int = 10_000) -> None:
    """Yield to the event loop until `predicate` holds. Never waits on the wall clock."""

    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was never reached")


class _Feed:
    """Publishes observations and hands each consumed message to the worker.

    Reads on its own consumer group so the worker's own offsets (and the
    `hammertime-aggregator` group's committed position) are untouched.
    """

    def __init__(self, bus: InMemoryBus) -> None:
        self._producer = bus.producer()
        self._consumer = bus.consumer("test-feed")
        self._stream: AsyncIterator[ConsumedMessage] | None = None

    async def _next(self) -> ConsumedMessage:
        if self._stream is None:
            self._stream = await self._consumer.subscribe(OBSERVATIONS_TOPIC)
        return await _take_one(self._stream)

    async def deliver(
        self, worker: AggregatorWorker, *, key: str, value: bytes
    ) -> ObservationOutcome:
        await self._producer.publish(OBSERVATIONS_TOPIC, key=key, value=value)
        message = await self._next()
        return await worker.handle(message)


class TestAnAppliedObservation:
    """Decision 3 step 3, and section 19's chain for a single IP."""

    async def test_a_current_observation_is_applied_and_promotes_the_ip(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        async with _running(
            _worker(bus=bus, clock=clock, metrics=metrics, state_store=state_store)
        ) as worker:
            outcome = await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )

            assert outcome is ObservationOutcome.APPLIED
            window = _window_of(worker)
            assert window.total(IP_A) == 1200
            assert window.state(IP_A) is IpState.HOT

            decoded = _decoded(bus)
            assert len(decoded) == 1
            key, envelope = decoded[0]
            assert key == str(IP_A).encode()
            assert envelope.event_type == "HotIpAdded"
            assert envelope.agent_id == SHARD_AGENT_ID
            assert envelope.subject == str(IP_A)
            assert envelope.config_version == 1
            payload = envelope.payload
            assert isinstance(payload, HotIpAdded)
            assert payload.ip == IP_A
            assert payload.window_count == 1200
            assert payload.attributes == {"attributes_version": 1, "weight": 1200}
            assert (await state_store.load(0)).hot_ips == frozenset({IP_A})

    async def test_a_further_observation_below_the_threshold_emits_nothing_new(self) -> None:
        # Section 6: an already-HOT IP whose count stays at or above
        # `cold_threshold` produces no transition at all.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )

            outcome = await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 100, window_start=BASE, sequence=2)
            )

            assert outcome is ObservationOutcome.APPLIED
            window = _window_of(worker)
            assert window.total(IP_A) == 1300
            assert window.state(IP_A) is IpState.HOT
            assert len(_records(bus, HOT_IP_TOPIC)) == 1

    async def test_an_applied_observation_is_never_diverted_or_rejected(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )

            assert _records(bus, RECONCILIATION_TOPIC) == []
            for reason in ("late", "future", "expired_bucket"):
                assert _counter(metrics, "late_messages", reason=reason) == 0
            for reason in ("window_too_long", "malformed"):
                assert _counter(metrics, "observations_rejected", reason=reason) == 0


class TestDivertedObservations:
    """Decision 3 step 2: everything the hot path cannot use is republished
    byte-for-byte, under the same key, to the reconciliation topic -- "never
    applied and never silently dropped" (section 24's ADR-0011 note)."""

    async def _divert(
        self, *, value: bytes, clock: ManualClock | None = None
    ) -> tuple[InMemoryBus, AggregatorMetrics, ObservationOutcome, ShardWindow]:
        resolved_clock = clock if clock is not None else ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=resolved_clock, metrics=metrics)) as worker:
            outcome = await feed.deliver(worker, key=str(IP_A), value=value)
            window = _window_of(worker)
        return bus, metrics, outcome, window

    async def test_an_observation_past_the_lateness_horizon_is_late(self) -> None:
        # 400 s > `window_seconds + allowed_lateness_seconds` (330 s).
        value = _observation(IP_A, 1200, window_start=BASE - 400)

        bus, metrics, outcome, window = await self._divert(value=value)

        assert outcome is ObservationOutcome.LATE
        assert _records(bus, RECONCILIATION_TOPIC) == [(str(IP_A).encode(), value)]
        assert _records(bus, HOT_IP_TOPIC) == []
        # "The window store is not touched": no entry, so no retention slot.
        assert window.is_tracked(IP_A) is False
        assert window.tracked_count == 0
        assert _counter(metrics, "late_messages", reason="late") == 1

    async def test_an_observation_from_the_future_is_diverted(self) -> None:
        value = _observation(IP_A, 1200, window_start=BASE + 10)

        bus, metrics, outcome, window = await self._divert(value=value)

        assert outcome is ObservationOutcome.FUTURE
        assert _records(bus, RECONCILIATION_TOPIC) == [(str(IP_A).encode(), value)]
        assert window.is_tracked(IP_A) is False
        assert _counter(metrics, "late_messages", reason="future") == 1

    async def test_an_observation_whose_bucket_has_left_the_window_is_diverted(self) -> None:
        # Inside the 330 s horizon, but the bucket is 310 s back: it can never
        # affect a future window count (section 5's ADR-0011 note).
        value = _observation(IP_A, 1200, window_start=BASE - 310)

        bus, metrics, outcome, window = await self._divert(value=value)

        assert outcome is ObservationOutcome.EXPIRED_BUCKET
        assert _records(bus, RECONCILIATION_TOPIC) == [(str(IP_A).encode(), value)]
        assert window.is_tracked(IP_A) is False
        assert _counter(metrics, "late_messages", reason="expired_bucket") == 1

    async def test_an_over_long_window_is_diverted_and_counted_as_a_rejection(self) -> None:
        # ADR-0010 decision 6: `window_seconds` on the message is descriptive
        # and is only used to reject a window longer than the configured one.
        value = _observation(IP_A, 1200, window_start=BASE, window_seconds=600)

        bus, metrics, outcome, window = await self._divert(value=value)

        assert outcome is ObservationOutcome.WINDOW_TOO_LONG
        assert _records(bus, RECONCILIATION_TOPIC) == [(str(IP_A).encode(), value)]
        assert window.is_tracked(IP_A) is False
        # Section 37: `observations_rejected`, not `late_messages`.
        assert _counter(metrics, "observations_rejected", reason="window_too_long") == 1
        assert _counter(metrics, "late_messages", reason="late") == 0


class TestMalformedObservations:
    """Decision 3 step 1 and assumption 10: a message that fails the codec or
    ADR-0004's producer invariant "cannot be trusted to name the IP it is
    keyed by", so it is dropped rather than diverted."""

    async def _handle(
        self, *, key: str, value: bytes
    ) -> tuple[InMemoryBus, AggregatorMetrics, ObservationOutcome, ShardWindow]:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            outcome = await feed.deliver(worker, key=key, value=value)
            window = _window_of(worker)
        return bus, metrics, outcome, window

    async def test_undecodable_bytes_are_malformed_and_dropped(self) -> None:
        bus, metrics, outcome, window = await self._handle(
            key=str(IP_A), value=b"\x00\x01\xffnot an event at all"
        )

        assert outcome is ObservationOutcome.MALFORMED
        # Assumption 10: dropped, not diverted -- forwarding it would
        # propagate the corruption.
        assert _records(bus, RECONCILIATION_TOPIC) == []
        assert _records(bus, HOT_IP_TOPIC) == []
        assert window.tracked_count == 0
        assert _counter(metrics, "observations_rejected", reason="malformed") == 1

    async def test_a_multi_entry_payload_is_malformed(self) -> None:
        # ADR-0004: ingest publishes exactly one entry per message; anything
        # else is a producer the aggregator must not trust.
        value = _observation(
            IP_A, 0, entries=((IP_A, 1200), (IP_B, 5)), subject=str(IP_A), window_start=BASE
        )

        bus, metrics, outcome, window = await self._handle(key=str(IP_A), value=value)

        assert outcome is ObservationOutcome.MALFORMED
        assert _records(bus, RECONCILIATION_TOPIC) == []
        assert window.tracked_count == 0
        assert _counter(metrics, "observations_rejected", reason="malformed") == 1

    async def test_a_subject_that_disagrees_with_the_entry_is_malformed(self) -> None:
        value = _observation(IP_A, 1200, subject=str(IP_B), window_start=BASE)

        bus, metrics, outcome, window = await self._handle(key=str(IP_B), value=value)

        assert outcome is ObservationOutcome.MALFORMED
        assert _records(bus, RECONCILIATION_TOPIC) == []
        assert window.tracked_count == 0

    async def test_a_key_that_disagrees_with_the_entry_is_malformed(self) -> None:
        # Decision 3 step 1 names all three: entry IP, envelope `subject` and
        # the message key must agree.
        value = _observation(IP_A, 1200, window_start=BASE)

        bus, _metrics, outcome, window = await self._handle(key=str(IP_B), value=value)

        assert outcome is ObservationOutcome.MALFORMED
        assert _records(bus, RECONCILIATION_TOPIC) == []
        assert window.tracked_count == 0

    async def test_a_payload_that_is_not_a_request_observation_is_malformed(self) -> None:
        bus, _metrics, outcome, window = await self._handle(
            key=str(IP_A), value=_hot_ip_event(IP_A)
        )

        assert outcome is ObservationOutcome.MALFORMED
        assert _records(bus, RECONCILIATION_TOPIC) == []
        assert window.tracked_count == 0

    async def test_a_poison_message_never_stops_the_consumer(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            poison = await feed.deliver(worker, key=str(IP_A), value=b"{not valid json")
            assert poison is ObservationOutcome.MALFORMED

            outcome = await feed.deliver(
                worker, key=str(IP_B), value=_observation(IP_B, 1200, window_start=BASE)
            )

            assert outcome is ObservationOutcome.APPLIED
            window = _window_of(worker)
            assert window.total(IP_B) == 1200
            assert window.is_tracked(IP_A) is False
            assert [envelope.subject for _key, envelope in _decoded(bus)] == [str(IP_B)]


class TestTheWorkerFloorsTheWindowStart:
    """Amendment 2 item A9: the aggregator floors `window_start` and never
    treats misalignment as `MALFORMED` -- ADR-0010 decision 6 already lands
    the delta in `bucket_start(window_start, bucket_seconds)`."""

    async def test_a_window_start_off_the_bucket_grid_is_applied(self) -> None:
        clock = ManualClock(initial=BASE)
        clock.advance(7)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            outcome = await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE + 7)
            )

            assert outcome is ObservationOutcome.APPLIED
            assert _window_of(worker).total(IP_A) == 1200
            assert _records(bus, RECONCILIATION_TOPIC) == []
            # Amendment 2 item A9 / decision 3 step 1: `MALFORMED` is reserved
            # for codec and ADR-0004 failures. Misalignment is neither.
            assert _counter(metrics, "observations_rejected", reason="malformed") == 0

    async def test_the_delta_lands_in_the_floored_bucket(self) -> None:
        # The bucket is `bucket_start(BASE + 7, 10) == BASE`, so it leaves the
        # window at exactly `BASE + 300` -- not at `BASE + 307`, which is what
        # an unfloored `window_start` would imply.
        clock = ManualClock(initial=BASE)
        clock.advance(7)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE + 7)
            )
            window = _window_of(worker)

            clock.advance(WINDOW_SECONDS - 1 - 7)  # now = BASE + 299
            await worker.run_maintenance()
            assert window.total(IP_A) == 1200

            clock.advance(1)  # now = BASE + 300
            await worker.run_maintenance()
            assert window.total(IP_A) == 0

    async def test_a_sub_second_window_start_is_floored_to_its_second(self) -> None:
        # The codec admits sub-second precision; `classify_observation` and
        # `Clock.now()` are whole seconds, so the worker passes the floor.
        # Floored to `BASE` this is a current observation, not a `FUTURE` one.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            outcome = await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE + 0.5)
            )

            assert outcome is ObservationOutcome.APPLIED
            assert _window_of(worker).total(IP_A) == 1200
            assert _records(bus, RECONCILIATION_TOPIC) == []


class TestDemotionOnTheObservationPath:
    """Amendment 2 item A11: `IpCounter.observe` subtracts the slot's expired
    occupant before adding the delta, so an observation can lower the running
    total and the evaluation that follows it can demote the IP. The label
    names the path, so it is `reason="observation"`."""

    async def test_an_observation_into_a_stale_slot_can_demote(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        async with _running(
            _worker(bus=bus, clock=clock, metrics=metrics, state_store=state_store)
        ) as worker:
            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )
            assert _window_of(worker).state(IP_A) is IpState.HOT

            # One whole window later, *without* a maintenance sweep: the
            # bucket at `BASE + 300` reuses the slot the bucket at `BASE`
            # holds, so applying 5 to it leaves an exact total of 5.
            clock.advance(WINDOW_SECONDS)
            outcome = await feed.deliver(
                worker,
                key=str(IP_A),
                value=_observation(IP_A, 5, window_start=BASE + WINDOW_SECONDS, sequence=2),
            )

            assert outcome is ObservationOutcome.APPLIED
            window = _window_of(worker)
            assert window.total(IP_A) == 5
            assert window.state(IP_A) is IpState.COLD

            decoded = _decoded(bus)
            assert [envelope.event_type for _key, envelope in decoded] == [
                "HotIpAdded",
                "HotIpRemoved",
            ]
            payload = decoded[1][1].payload
            assert isinstance(payload, HotIpRemoved)
            assert payload.window_count == 5
            assert payload.attributes is None
            assert (await state_store.load(0)).hot_ips == frozenset()
            assert (
                _counter(
                    metrics,
                    "hot_to_cold_transitions",
                    shard=0,
                    config_version=1,
                    reason="observation",
                )
                == 1
            )

    async def test_an_inherited_ip_is_not_demoted_on_the_observation_path(self) -> None:
        # Decision 4's exemption holds "whatever the trigger", and an
        # observation is a trigger (A11's closing paragraph).
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        feed = _Feed(bus)
        async with _running(
            _worker(bus=bus, clock=clock, metrics=metrics, state_store=state_store)
        ) as worker:
            window = _window_of(worker)
            assert window.in_warmup is True

            outcome = await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 5, window_start=BASE)
            )

            assert outcome is ObservationOutcome.APPLIED
            assert window.total(IP_A) == 5
            assert window.state(IP_A) is IpState.HOT
            assert _records(bus, HOT_IP_TOPIC) == []
            assert (await state_store.load(0)).hot_ips == frozenset({IP_A})


class TestMaintenance:
    """ADR-0011 decision 6: per claimed shard, `expire_due()` -> evaluate the
    HOT changes with `reason="expiry"` -> `finish_warmup_if_due()` ->
    evaluate with `reason="warmup"` -> `evict_due()`."""

    async def test_a_sweep_with_nothing_due_emits_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )

            await worker.run_maintenance()

            assert len(_records(bus, HOT_IP_TOPIC)) == 1
            assert _window_of(worker).total(IP_A) == 1200

    async def test_expiry_empties_the_window_and_demotes_in_one_sweep(self) -> None:
        # "expiring before evaluating is what turns an expired count into a
        # `HotIpRemoved` in the same sweep" (decision 6).
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        async with _running(
            _worker(bus=bus, clock=clock, metrics=metrics, state_store=state_store)
        ) as worker:
            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )
            clock.advance(310)

            await worker.run_maintenance()

            decoded = _decoded(bus)
            assert len(decoded) == 2
            key, envelope = decoded[1]
            assert key == str(IP_A).encode()
            assert envelope.event_type == "HotIpRemoved"
            assert envelope.agent_id == SHARD_AGENT_ID
            assert envelope.sequence == 1
            payload = envelope.payload
            assert isinstance(payload, HotIpRemoved)
            assert payload.window_count == 0
            assert payload.attributes is None
            window = _window_of(worker)
            assert window.state(IP_A) is IpState.COLD
            assert window.total(IP_A) == 0
            assert (await state_store.load(0)).hot_ips == frozenset()
            assert (
                _counter(
                    metrics, "hot_to_cold_transitions", shard=0, config_version=1, reason="expiry"
                )
                == 1
            )

    async def test_retention_evicts_the_cold_ip_a_full_retention_later(self) -> None:
        # Section 26: a COLD IP is evicted once
        # `last_seen + state_retention_seconds <= now`; `last_seen` is the
        # bucket at `BASE`, so the deadline is `BASE + 600`.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )
            clock.advance(310)
            await worker.run_maintenance()
            window = _window_of(worker)
            assert window.tracked_count == 1
            assert window.retention_evictions == 0

            clock.advance(600)
            await worker.run_maintenance()

            assert window.is_tracked(IP_A) is False
            assert window.tracked_count == 0
            assert window.retention_evictions == 1
            # An eviction is not a transition: nothing new was emitted.
            assert len(_records(bus, HOT_IP_TOPIC)) == 2

    async def test_warm_up_end_is_evaluated_after_the_expiry_sweep(self) -> None:
        # Decision 6's order, made observable: at `BASE + 300` both are due --
        # the self-promoted IP's only bucket has just left the window (step 1
        # and 2) and warm-up is over (step 3 and 4). The expiry demotion is
        # therefore sequenced before the warm-up demotion.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_C, IpState.HOT, 0)
        feed = _Feed(bus)
        async with _running(
            _worker(bus=bus, clock=clock, metrics=metrics, state_store=state_store)
        ) as worker:
            await feed.deliver(
                worker, key=str(IP_B), value=_observation(IP_B, 1200, window_start=BASE)
            )
            assert _window_of(worker).state(IP_B) is IpState.HOT

            clock.advance(WINDOW_SECONDS)
            await worker.run_maintenance()

            decoded = _decoded(bus)
            assert [(envelope.event_type, envelope.subject) for _key, envelope in decoded] == [
                ("HotIpAdded", str(IP_B)),
                ("HotIpRemoved", str(IP_B)),
                ("HotIpRemoved", str(IP_C)),
            ]
            window = _window_of(worker)
            assert window.state(IP_B) is IpState.COLD
            assert window.state(IP_C) is IpState.COLD
            assert window.in_warmup is False
            assert (
                _counter(
                    metrics, "hot_to_cold_transitions", shard=0, config_version=1, reason="expiry"
                )
                == 1
            )
            assert (
                _counter(
                    metrics, "hot_to_cold_transitions", shard=0, config_version=1, reason="warmup"
                )
                == 1
            )


class TestOffsetsAreCommittedAtShutdown:
    """ADR-0011 decision 6 / ADR-0009 decision 7: `stop()` stops fetching,
    finishes the in-flight message, flushes and commits -- so a restarted
    member of `hammertime-aggregator` resumes past what it handled rather
    than re-reading the log from the start."""

    async def test_a_new_consumer_for_the_group_resumes_past_the_handled_messages(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        producer = bus.producer()
        await producer.publish(
            OBSERVATIONS_TOPIC, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
        )
        await producer.publish(
            OBSERVATIONS_TOPIC, key=str(IP_B), value=_observation(IP_B, 1200, window_start=BASE)
        )

        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        task = asyncio.create_task(worker.run())
        try:
            await _yield_until(lambda: _window_of(worker).is_tracked(IP_B))
        finally:
            # `stop()` is what commits (ADR-0009 decision 7). The loop below
            # only gives `run()` the chance to return; cancelling afterwards
            # is teardown hygiene, not part of the assertion, and never waits
            # on the wall clock.
            await worker.stop()
            for _ in range(1_000):
                if task.done():
                    break
                await asyncio.sleep(0)
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert _window_of(worker).total(IP_A) == 1200
        assert len(_records(bus, HOT_IP_TOPIC)) == 2

        # Published after shutdown: the first thing a fresh member of the same
        # group must see.
        await producer.publish(
            OBSERVATIONS_TOPIC, key=str(IP_C), value=_observation(IP_C, 1, window_start=BASE)
        )
        resumed = bus.consumer(GROUP)
        message = await _take_one(await resumed.subscribe(OBSERVATIONS_TOPIC))

        assert message.key == str(IP_C).encode()
