"""One consumed observation, end to end, in process.

Spec: section 19 (the aggregator consumes `RequestObservation` and emits
`HotIpAdded`/`HotIpRemoved`), section 20 (the message's partition is the
shard that owns the IP), section 24 (what happens to an observation the hot
path cannot use), section 25 (bucket arithmetic), section 26 (retention),
section 30 (the processing algorithm), section 37 (the counters and their
labels). ADR-0002 (event time), ADR-0003 (at-least-once consumption; Amendment
3: a redelivery within a live claim is recognised by offset and acknowledged
without being applied), ADR-0004 (one single-entry message per IP, keyed and
`subject`-tagged by it), ADR-0009 decisions 3, 7 and 9 (`run_maintenance()`,
shutdown, the `hammertime-aggregator` group), ADR-0010 decision 6 (one bucket
per observation, over-long windows), ADR-0011 decision 3 (the outcomes and
the worker's three steps), decision 6 (maintenance order, acknowledgement
cadence, shutdown), decision 8 (metrics), Amendment 2 items A9 (flooring) and
A11 (a demotion on the observation path), and ADR-0013 decision 8 (the
aggregator acknowledges what it handled; `REDELIVERED` is the eighth outcome;
there is no revocation path -- a shard changes hands by `stop()` and
`start()`), decision 5 (`ack_wait` redelivery is a supported input) and
decision 3 (the `ack()` rule that shapes the harness, ASSUMPTION 4).

Decision 3 of ADR-0011 is the specification this file is written against.
Per consumed message, once the lookup that precedes step 1 has found the
partition's `ShardWindow` -- the case where it does not is Amendment 5 item
A19's `UNCLAIMED`, which `test_sharding.py` owns -- and once the handled
position has been checked -- a message with `offset` below it is ADR-0013
decision 8's `REDELIVERED`, `TestARedeliveredMessage` below:

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

ASSUMPTIONS -- things the decisions do not pin. Each is a judgment call;
adjust the helpers below, not the meaning of the assertions. The first
three are shared with `test_sharding.py` and `test_reevaluate.py`:

1. `AggregatorWorker(*, bus, state_store, clock, config, metrics,
   shard_ids=None, max_tracked_ips=1_000_000, member_id="aggregator",
   lease_ttl_s=30.0)`, all keyword-only, taking the whole `bus` rather than
   a pre-built producer/consumer pair -- the same choice ADR-0009 made for
   `build_service` ("takes an `InMemoryBus`, not a `Producer`/`Consumer`")
   and the same one `services/ingest/.../tests/test_pipeline.py::_build_app`
   relies on, so a test can keep its own reference and read every topic's
   log back. `shard_ids=None` is "every partition as the bus defines it",
   `{0}` on `InMemoryBus` (ADR-0013 decision 6). The two lease keywords and
   their defaults are ADR-0013 decision 8's.
2. `await worker.start()` (claims and leases, per ADR-0013 decision 3
   "`subscribe()` awaits `listener.on_assigned(...)` ... before it
   returns"), `await worker.run()` (the consume loop), `await worker.stop()`
   (ADR-0009 decision 7; ADR-0013 decision 8: stop fetching, finish the
   message in hand, `commit_handled()`, `release()`), `await
   worker.handle(message) -> ObservationOutcome` (decision 9 spells this one
   out), `await worker.run_maintenance()` (ADR-0009 decision 3),
   `worker.window(shard) -> ShardWindow | None`, `worker.shards`,
   `worker.claims`, `worker.config`.
3. `AggregatorMetrics()` takes no required arguments and
   `metrics.get(name, **labels) -> int` reads one series back, `0` for one
   that was never touched. Decision 8 names the series and their labels but
   no Python API.
4. **The message handed to `handle()` is read on the worker's own
   consumer.** ADR-0013 decision 3: `ack()` accepts only a message "delivered
   by this consumer instance under its durable subscription", so a message
   read on a separate test group (the shape the previous version of this
   file used) can no longer be marked handled and acknowledged at `stop()`.
   `_TappedBus` hands back the subscription the worker's consumer opened and
   `_Feed.deliver` reads the next message on it before calling `handle()`.
   Its `consumer()` returns `Any` because a wrapper is not a
   `MemoryConsumer`.
5. **A redelivery is modelled by handing `handle()` the same
   `ConsumedMessage` twice.** `InMemoryBus` "never redelivers a message to a
   live consumer (no `ack_wait`)" (ADR-0013 decision 3), and ADR-0003
   Amendment 3 item 4 says tests SHOULD pin the direct call. The copy the
   worker marks handled therefore has the same `(topic, partition, offset)`
   as the original; the observable that ADR-0013 decision 8 promises --
   "the copy is acknowledged at the next commit" -- is asserted as
   `stop()` succeeding and a fresh member of the group resuming after the
   message. Whether the memory bus's `ack()` sees two equal entries in one
   iterable as one acknowledgement or as a duplicate is not pinned by
   decision 3; a `ValueError` at `stop()` here is a finding for the
   architect, not for this test.
6. **A message fetched into the consumer's queue but never yielded** (ADR-0013
   decision 8: "negatively acknowledged by `close()` and redelivered to the
   next member at once") is modelled by `_PrefetchingConsumer`, which pulls
   a batch from the inner subscription in one step, yields the first and
   keeps the rest, the way `NatsConsumer`'s fetch loop feeds "one in-process
   queue" (decision 5). On the memory bus nothing needs to be sent back:
   "an unacknowledged message is already deliverable to the next consumer".
7. `worker.stop()` does not close the consumer (decision 8: "the service
   closes the bus ... afterwards"); `_drain` cancels the consume loop after
   `stop()` as teardown hygiene, never as part of an assertion.
8. **A message that completes in the same wake-up as the stop signal**
   (ADR-0013 decision 8 as amended 2026-09-22, Amendment 4 ruling R2:
   "`run()`, on a wake-up in which the stop signal and a received message are
   both complete, returns without handing the message to `handle()`";
   assumption 82: the message "was yielded, so it is retained and naked at
   `close()` on `NatsConsumer`, and delivered-unacknowledged on
   `MemoryConsumer`; either way the next member applies it once") is
   modelled by `_GatedBus`: the worker's subscription is held behind an
   `asyncio.Event` with the message already in the log, the gate is set and
   `stop()` awaited in the same tick, so the receive completes on the first
   wake-up that follows -- the same wake-up the stop signal reaches `run()`
   on. The gate is set with no `await` between it and `worker.stop()`, which
   is as close to the same-tick shape as `InMemoryBus` allows; whether
   `run()` observes both in one `asyncio.wait` return or in two consecutive
   ones is not controllable from a test, so what is asserted is the weaker
   invariant that holds either way and that ruling R2 makes total: a message
   that arrives during or after `stop()` is never applied, is never marked
   handled, and is handed to the next consumer of the group
   (`TestAMessageThatCompletesWithTheStopSignal`).

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
* **The `malformed_observation` and `redelivered_observation` log records**
  of decision 3 step 1, ADR-0013 decision 8 and decision 8 of ADR-0011.
  ADR-0009 decision 5 and section 47.7 fix the event names and fields but
  not a record shape a unit test can assert against without a configured
  logger -- the same reason `packages/hammertime-core/.../tests/test_runtime.py`
  gives for omitting its own lifecycle records. What is asserted is the
  counter (or its absence) and that the consumer survives.
* **The 1 s acknowledgement cadence itself.** Decision 6 measures it on the
  *wall* clock ("an I/O cadence, not domain time"), and nothing here may
  sleep for wall time, so what is asserted is the other acknowledgement
  point: shutdown (`TestHandledMessagesAreAcknowledgedAtShutdown`).
* **A real `ack_wait` redelivery with `delivery_count > 1`.** Only a broker
  produces one; `InMemoryBus` reports `delivery_count == 1` always. The
  worker's rule is by `offset`, not by `delivery_count`, so ASSUMPTION 5's
  model exercises the rule exactly.

`BASE` is `1_800_000_000`, the `T0` of `docs/spec/integration-scenarios.md`
section 2 -- a multiple of 300, so every bucket boundary below is exact.
Nothing in this module sleeps for wall time: `_yield_until` only yields to
the event loop with `asyncio.sleep(0)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from hammertime.aggregator.lateness import ObservationOutcome
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.window.store import ShardWindow
from hammertime.aggregator.worker import AggregatorWorker
from hammertime.bus.interface import AssignmentListener, ConsumedMessage, Consumer
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import OBSERVATIONS
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import EventPayload, decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded, HotIpRemoved, Observation, RequestObservation
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import ManualClock
from hammertime.store.interface import ShardState
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

# ADR-0013 decision 7: successive members carry distinct ids.
MEMBER_A = "aggregator-a"
MEMBER_B = "aggregator-b"


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
    member_id: str = MEMBER_A,
) -> AggregatorWorker:
    """ASSUMPTION 1 (see the module docstring)."""

    return AggregatorWorker(
        bus=bus,
        state_store=state_store if state_store is not None else MemoryShardStateStore(),
        clock=clock,
        config=config,
        metrics=metrics,
        shard_ids=None,
        member_id=member_id,
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


async def _drain(worker: AggregatorWorker, task: asyncio.Task[None]) -> None:
    """`stop()` (which acknowledges and releases), then let `run()` return. Never sleeps."""

    await worker.stop()
    for _ in range(1_000):
        if task.done():
            break
        await asyncio.sleep(0)
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class _TappedConsumer:
    """A `Consumer` wrapper that exposes the stream its `subscribe()` returned.

    Delegates everything to a real `MemoryConsumer`, so nothing here depends
    on that class's constructor; `__getattr__` forwards any member this
    wrapper does not name (ASSUMPTION 4).
    """

    def __init__(self, inner: Consumer) -> None:
        self._inner = inner
        self.stream: AsyncIterator[ConsumedMessage] | None = None

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        self.stream = await self._inner.subscribe(
            topic, partitions=partitions, listener=listener, start_offset=start_offset
        )
        return self.stream

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        await self._inner.ack(messages)

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _TappedBus(InMemoryBus):
    """An `InMemoryBus` whose consumers expose their subscription (ASSUMPTION 4).

    `latest_stream` is the subscription of the most recently created
    consumer -- the current worker's, since a worker creates exactly one
    consumer in `start()` and the tests here run one worker at a time.
    """

    def __init__(self) -> None:
        super().__init__()
        self.consumers: list[_TappedConsumer] = []

    def consumer(self, *args: Any, **kwargs: Any) -> Any:
        wrapped = _TappedConsumer(super().consumer(*args, **kwargs))
        self.consumers.append(wrapped)
        return wrapped

    @property
    def latest_stream(self) -> AsyncIterator[ConsumedMessage]:
        assert self.consumers, "no consumer has been created on this bus"
        stream = self.consumers[-1].stream
        assert stream is not None, "the latest consumer has not subscribed"
        return stream


class _Feed:
    """Publishes observations and hands each one to the worker off the
    worker's own subscription (ASSUMPTION 4). `last` is the message most
    recently handed over, so a test can hand it over a second time.

    Observations are published with `message_id=None` on purpose: several
    tests publish byte-identical observations twice, and ADR-0013 assumption
    22 is what keeps the log from deduplicating them.
    """

    def __init__(self, bus: _TappedBus) -> None:
        self._bus = bus
        self._producer = bus.producer()
        self.last: ConsumedMessage | None = None

    async def publish(self, *, key: str, value: bytes) -> None:
        await self._producer.publish(OBSERVATIONS_TOPIC, key=key, value=value, message_id=None)

    async def deliver(
        self, worker: AggregatorWorker, *, key: str, value: bytes
    ) -> ObservationOutcome:
        await self.publish(key=key, value=value)
        self.last = await _take_one(self._bus.latest_stream)
        return await worker.handle(self.last)


class _PrefetchingConsumer:
    """Models `NatsConsumer`'s fetch batch on top of a `MemoryConsumer`
    (ASSUMPTION 6).

    The first `__anext__` pulls `batch` messages from the inner subscription
    -- delivering every one of them as far as the bus is concerned -- and
    yields only the first; the rest sit in `queued` and are never yielded,
    the way a message "fetched into the consumer's queue and never yielded"
    sits when a member stops. The iterator then waits until `close()`.
    """

    def __init__(self, inner: Consumer, batch: int) -> None:
        self._inner = inner
        self._batch = batch
        self._closed = asyncio.Event()
        self.queued: list[ConsumedMessage] = []
        self.yielded: list[ConsumedMessage] = []

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        inner = await self._inner.subscribe(
            topic, partitions=partitions, listener=listener, start_offset=start_offset
        )
        return self._scripted(inner)

    async def _scripted(
        self, inner: AsyncIterator[ConsumedMessage]
    ) -> AsyncIterator[ConsumedMessage]:
        fetched = [await inner.__anext__() for _ in range(self._batch)]
        self.queued = fetched[1:]
        self.yielded.append(fetched[0])
        yield fetched[0]
        await self._closed.wait()

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        await self._inner.ack(messages)

    async def close(self) -> None:
        self._closed.set()
        await self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _PrefetchBus(InMemoryBus):
    """An `InMemoryBus` whose *first* consumer is a `_PrefetchingConsumer`.

    The first one is the worker's own (nothing else takes a consumer before
    `start()` does); the next member of the group gets a plain
    `MemoryConsumer`, so what it reads is decided by the acknowledgements
    alone. `InMemoryBus` is subclassed rather than duck-typed because the
    worker takes a whole bus (ASSUMPTION 1), the same way
    `services/ingest/.../tests/test_pipeline.py::_FailingBus` does.
    """

    def __init__(self, batch: int) -> None:
        super().__init__()
        self._batch = batch
        self.prefetcher: _PrefetchingConsumer | None = None

    def consumer(self, *args: Any, **kwargs: Any) -> Any:
        inner = super().consumer(*args, **kwargs)
        if self.prefetcher is not None:
            return inner
        self.prefetcher = _PrefetchingConsumer(inner, self._batch)
        return self.prefetcher


class _GatedConsumer:
    """Holds a `MemoryConsumer`'s subscription behind an `asyncio.Event` (ASSUMPTION 8).

    Until the gate is set nothing is yielded, however much is in the log;
    once it is, the inner subscription is read as it is. `close()` opens the
    gate so a blocked iterator can end.
    """

    def __init__(self, inner: Consumer, gate: asyncio.Event) -> None:
        self._inner = inner
        self._gate = gate

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        inner = await self._inner.subscribe(
            topic, partitions=partitions, listener=listener, start_offset=start_offset
        )
        return self._gated(inner)

    async def _gated(self, inner: AsyncIterator[ConsumedMessage]) -> AsyncIterator[ConsumedMessage]:
        await self._gate.wait()
        async for message in inner:
            yield message

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        await self._inner.ack(messages)

    async def close(self) -> None:
        self._gate.set()
        await self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _GatedBus(InMemoryBus):
    """An `InMemoryBus` whose *first* consumer -- the worker's own -- is gated (ASSUMPTION 8);
    every later consumer is a plain `MemoryConsumer`, as in `_PrefetchBus`."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self._gated_once = False

    def consumer(self, *args: Any, **kwargs: Any) -> Any:
        inner = super().consumer(*args, **kwargs)
        if self._gated_once:
            return inner
        self._gated_once = True
        return _GatedConsumer(inner, self.gate)


class TestAnAppliedObservation:
    """Decision 3 step 3, and section 19's chain for a single IP."""

    async def test_a_current_observation_is_applied_and_promotes_the_ip(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
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
        bus = _TappedBus()
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
        bus = _TappedBus()
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

    async def test_an_applied_message_raises_the_handled_position_past_it(self) -> None:
        # ADR-0013 decision 8: the worker calls `mark_handled` "at the end of
        # `handle()` for every outcome except `UNCLAIMED`".
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            assert worker.claims.handled_position(0) is None

            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )

            assert feed.last is not None
            assert worker.claims.handled_position(0) == feed.last.offset + 1


class TestDivertedObservations:
    """Decision 3 step 2: everything the hot path cannot use is republished
    byte-for-byte, under the same key, to the reconciliation topic -- "never
    applied and never silently dropped" (section 24's ADR-0011 note)."""

    async def _divert(
        self, *, value: bytes, clock: ManualClock | None = None
    ) -> tuple[InMemoryBus, AggregatorMetrics, ObservationOutcome, ShardWindow]:
        resolved_clock = clock if clock is not None else ManualClock(initial=BASE)
        bus = _TappedBus()
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

    async def test_a_diverted_message_carries_its_event_id_as_the_message_id(self) -> None:
        # ADR-0013 decision 4: the reconciliation divert passes "the diverted
        # observation's own `event_id`" as `message_id`, so the same diverted
        # bytes republished twice inside the window are one record. Two
        # workers, one bus: the second divert of identical bytes is dropped.
        value = _observation(IP_A, 1200, window_start=BASE - 400)
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=AggregatorMetrics())) as worker:
            assert await feed.deliver(worker, key=str(IP_A), value=value) is ObservationOutcome.LATE
        async with _running(
            _worker(bus=bus, clock=clock, metrics=AggregatorMetrics(), member_id=MEMBER_B)
        ) as worker:
            assert await feed.deliver(worker, key=str(IP_A), value=value) is ObservationOutcome.LATE

        assert _records(bus, RECONCILIATION_TOPIC) == [(str(IP_A).encode(), value)]


class TestMalformedObservations:
    """Decision 3 step 1 and assumption 10: a message that fails the codec or
    ADR-0004's producer invariant "cannot be trusted to name the IP it is
    keyed by", so it is dropped rather than diverted."""

    async def _handle(
        self, *, key: str, value: bytes
    ) -> tuple[InMemoryBus, AggregatorMetrics, ObservationOutcome, ShardWindow]:
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
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

        bus, _, outcome, window = await self._handle(key=str(IP_B), value=value)

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
        bus = _TappedBus()
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

    async def test_a_malformed_message_is_acknowledged_like_any_other(self) -> None:
        # ADR-0013 decision 5: "every message the aggregator receives is
        # acknowledged on one of decision 8's paths -- including `MALFORMED`,
        # which is logged, counted and acknowledged -- so a poison message
        # cannot loop." After `stop()` a fresh member is not handed it again.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            assert (
                await feed.deliver(worker, key=str(IP_A), value=b"{not valid json")
                is ObservationOutcome.MALFORMED
            )
            assert feed.last is not None
            assert worker.claims.handled_position(0) == feed.last.offset + 1

        await feed.publish(key=str(IP_B), value=_observation(IP_B, 1, window_start=BASE))
        resumed = bus.consumer(GROUP)
        message = await _take_one(await resumed.subscribe(OBSERVATIONS_TOPIC))

        assert message.key == str(IP_B).encode()


class TestARedeliveredMessage:
    """ADR-0013 decision 8, ruled in decision 5: "after the window lookup
    (`UNCLAIMED` check) and before decoding, `if message.offset <
    claims.handled_position(partition)` ... the worker ... calls
    `mark_handled(message)` (so the copy is acknowledged at the next commit
    and the handled position, already past it, is unchanged), and returns
    `REDELIVERED` -- not decoded, not classified, not diverted, not counted
    under any series, the store untouched." ADR-0003 Amendment 3 item 4:
    this is now a supported input and tests SHOULD pin it (ASSUMPTION 5).
    """

    async def _first_message(self, worker: AggregatorWorker, feed: _Feed) -> ConsumedMessage:
        outcome = await feed.deliver(
            worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
        )
        assert outcome is ObservationOutcome.APPLIED
        assert feed.last is not None
        return feed.last

    async def test_a_message_below_the_handled_position_is_redelivered(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)
            assert first.offset < (worker.claims.handled_position(0) or 0)

            outcome = await worker.handle(first)

            assert outcome is ObservationOutcome.REDELIVERED

    async def test_a_redelivery_applies_nothing(self) -> None:
        # ADR-0003 Amendment 2's requirement, met "by exactly the mechanism
        # Amendment 2 said was not required -- a recognised redelivery
        # declined": the count stays 1200, not 2400.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        state_store = MemoryShardStateStore()
        feed = _Feed(bus)
        async with _running(
            _worker(bus=bus, clock=clock, metrics=metrics, state_store=state_store)
        ) as worker:
            first = await self._first_message(worker, feed)
            window = _window_of(worker)

            await worker.handle(first)

            assert window.total(IP_A) == 1200
            assert window.state(IP_A) is IpState.HOT
            assert window.tracked_count == 1
            assert len(_records(bus, HOT_IP_TOPIC)) == 1
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A}), next_sequence=1
            )

    async def test_a_redelivery_diverts_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)

            await worker.handle(first)

            assert _records(bus, RECONCILIATION_TOPIC) == []

    async def test_a_redelivery_counts_nothing(self) -> None:
        # ADR-0013 assumption 21: "`REDELIVERED` is logged at `WARNING` and
        # not counted" -- no series moves, including the transition counter
        # the first delivery incremented.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)
            promotions = _counter(
                metrics,
                "cold_to_hot_transitions",
                shard=0,
                config_version=1,
                reason="observation",
            )
            assert promotions == 1

            await worker.handle(first)

            assert (
                _counter(
                    metrics,
                    "cold_to_hot_transitions",
                    shard=0,
                    config_version=1,
                    reason="observation",
                )
                == 1
            )
            for reason in ("late", "future", "expired_bucket"):
                assert _counter(metrics, "late_messages", reason=reason) == 0
            for reason in ("window_too_long", "malformed"):
                assert _counter(metrics, "observations_rejected", reason=reason) == 0

    async def test_a_redelivery_leaves_the_handled_position_unchanged(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)
            position = worker.claims.handled_position(0)

            await worker.handle(first)

            assert worker.claims.handled_position(0) == position == first.offset + 1

    async def test_a_redelivery_is_acknowledged_at_the_next_commit(self) -> None:
        # "calls `mark_handled(message)` (so the copy is acknowledged at the
        # next commit ...)" -- ASSUMPTION 5: `stop()`'s `commit_handled()`
        # succeeds and a fresh member of the group resumes after the message.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)
            assert await worker.handle(first) is ObservationOutcome.REDELIVERED

        await feed.publish(key=str(IP_B), value=_observation(IP_B, 1, window_start=BASE))
        resumed = bus.consumer(GROUP)
        message = await _take_one(await resumed.subscribe(OBSERVATIONS_TOPIC))

        assert message.key == str(IP_B).encode()

    async def test_the_check_precedes_decoding(self) -> None:
        # "after the window lookup ... and before decoding": bytes that would
        # be `MALFORMED` are `REDELIVERED` when their offset is below the
        # handled position, and the malformed counter does not tick.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)
            garbage = ConsumedMessage(
                topic=first.topic,
                partition=first.partition,
                offset=first.offset,
                key=first.key,
                value=b"{not valid json",
                delivery_count=2,
            )

            outcome = await worker.handle(garbage)

            assert outcome is ObservationOutcome.REDELIVERED
            assert _counter(metrics, "observations_rejected", reason="malformed") == 0

    async def test_a_redelivery_of_an_earlier_message_is_recognised_too(self) -> None:
        # "a redelivered message is always behind the handled position": any
        # offset below it, not only the last one.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)
            second_outcome = await feed.deliver(
                worker, key=str(IP_B), value=_observation(IP_B, 5, window_start=BASE)
            )
            assert second_outcome is ObservationOutcome.APPLIED
            window = _window_of(worker)

            outcome = await worker.handle(first)

            assert outcome is ObservationOutcome.REDELIVERED
            assert window.total(IP_A) == 1200
            assert window.total(IP_B) == 5

    async def test_identical_bytes_at_a_new_offset_are_a_new_message(self) -> None:
        # ADR-0003 Amendment 3 item 4: "A message with a *new* offset but
        # identical bytes (the same `event_id` published twice outside the
        # dedup window) is a distinct message and is applied".
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)
            window = _window_of(worker)

            outcome = await feed.deliver(worker, key=str(IP_A), value=first.value)

            assert outcome is ObservationOutcome.APPLIED
            assert feed.last is not None
            assert feed.last.offset > first.offset
            assert window.total(IP_A) == 2400

    async def test_a_message_at_the_handled_position_is_new(self) -> None:
        # The comparison is strict: `offset < handled_position`, so the
        # message *at* the position (the next one) is handled normally.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            first = await self._first_message(worker, feed)
            position = worker.claims.handled_position(0)
            assert position == first.offset + 1

            outcome = await feed.deliver(
                worker, key=str(IP_B), value=_observation(IP_B, 5, window_start=BASE)
            )

            assert feed.last is not None
            assert feed.last.offset == position
            assert outcome is ObservationOutcome.APPLIED

    async def test_the_first_message_of_a_claim_is_never_redelivered(self) -> None:
        # `handled_position` is `None` before anything was handled, so no
        # offset is "below" it.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        async with _running(_worker(bus=bus, clock=clock, metrics=metrics)) as worker:
            assert worker.claims.handled_position(0) is None

            outcome = await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )

            assert outcome is ObservationOutcome.APPLIED


class TestTheWorkerFloorsTheWindowStart:
    """Amendment 2 item A9: the aggregator floors `window_start` and never
    treats misalignment as `MALFORMED` -- ADR-0010 decision 6 already lands
    the delta in `bucket_start(window_start, bucket_seconds)`."""

    async def test_a_window_start_off_the_bucket_grid_is_applied(self) -> None:
        clock = ManualClock(initial=BASE)
        clock.advance(7)
        bus = _TappedBus()
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
        bus = _TappedBus()
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
        bus = _TappedBus()
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
        bus = _TappedBus()
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
        bus = _TappedBus()
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
    evaluate with `reason="warmup"` -> `evict_due()`. ADR-0013 decision 7
    puts the lease renewal ahead of all of it (`test_sharding.py`)."""

    async def test_a_sweep_with_nothing_due_emits_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
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
        bus = _TappedBus()
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
        bus = _TappedBus()
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
        bus = _TappedBus()
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


class TestHandledMessagesAreAcknowledgedAtShutdown:
    """ADR-0011 decision 6 / ADR-0009 decision 7 / ADR-0013 decision 8:
    `stop()` stops fetching, finishes the message in hand, flushes and
    acknowledges what was handled -- so a restarted member of
    `hammertime-aggregator` resumes past what it handled rather than
    re-reading the log from the start."""

    async def test_a_new_consumer_for_the_group_resumes_past_the_handled_messages(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        producer = bus.producer()
        await producer.publish(
            OBSERVATIONS_TOPIC,
            key=str(IP_A),
            value=_observation(IP_A, 1200, window_start=BASE),
            message_id="m-a",
        )
        await producer.publish(
            OBSERVATIONS_TOPIC,
            key=str(IP_B),
            value=_observation(IP_B, 1200, window_start=BASE),
            message_id="m-b",
        )

        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        task = asyncio.create_task(worker.run())
        try:
            await _yield_until(lambda: _window_of(worker).is_tracked(IP_B))
        finally:
            # `stop()` is what acknowledges (ADR-0009 decision 7 / ADR-0013
            # decision 8). The loop below only gives `run()` the chance to
            # return; cancelling afterwards is teardown hygiene, not part of
            # the assertion, and never waits on the wall clock.
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
            OBSERVATIONS_TOPIC,
            key=str(IP_C),
            value=_observation(IP_C, 1, window_start=BASE),
            message_id="m-c",
        )
        resumed = bus.consumer(GROUP)
        message = await _take_one(await resumed.subscribe(OBSERVATIONS_TOPIC))

        assert message.key == str(IP_C).encode()

    async def test_nothing_is_acknowledged_before_stop_or_the_periodic_commit(self) -> None:
        # Decision 8: `commit_handled()` "is the aggregator's **only**
        # acknowledgement path: the periodic commit ... and `stop()`". A
        # handled message the worker has not yet committed is still unacked
        # -- a fresh consumer for the group is handed it.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        try:
            await feed.deliver(
                worker, key=str(IP_A), value=_observation(IP_A, 1200, window_start=BASE)
            )

            probe = bus.consumer(GROUP)
            message = await _take_one(await probe.subscribe(OBSERVATIONS_TOPIC))

            assert message.key == str(IP_A).encode()
        finally:
            await worker.stop()


class TestTheMessageInTheQueueReachesTheNextMember:
    """ADR-0013 decision 8: "a message fetched into the consumer's queue and
    never yielded is negatively acknowledged by `close()` and redelivered to
    the next member at once"; ADR-0003 Amendment 3 item 2: the aggregator
    acknowledges "never a message merely fetched". ADR-0001 Amendment 1
    clause 6 ("no double count and no loss at a rebalance", now a handover):
    the message is applied once, and by B.

    `_PrefetchBus` (ASSUMPTION 6) gives member A a consumer that fetches two
    messages in one batch and yields only the first; A handles it, is
    stopped, and member B -- a fresh member with a plain consumer, started
    only after A is gone -- is handed the second first.
    """

    async def _publish_two(self, bus: InMemoryBus) -> None:
        producer = bus.producer()
        await producer.publish(
            OBSERVATIONS_TOPIC,
            key=str(IP_A),
            value=_observation(IP_A, 1200, window_start=BASE),
            message_id="m-a",
        )
        await producer.publish(
            OBSERVATIONS_TOPIC,
            key=str(IP_B),
            value=_observation(IP_B, 1200, window_start=BASE),
            message_id="m-b",
        )

    async def test_the_message_in_the_queue_is_not_acknowledged_at_stop(self) -> None:
        clock = ManualClock(initial=BASE)
        a_metrics = AggregatorMetrics()
        state_store = MemoryShardStateStore()
        bus = _PrefetchBus(batch=2)
        await self._publish_two(bus)

        a = _worker(bus=bus, clock=clock, metrics=a_metrics, state_store=state_store)
        await a.start()
        a_task = asyncio.create_task(a.run())
        try:
            await _yield_until(lambda: _window_of(a).state(IP_A) is IpState.HOT)
            assert bus.prefetcher is not None
            assert [message.key for message in bus.prefetcher.queued] == [str(IP_B).encode()]
            # A never saw IP_B: the message sits in the queue, unhandled.
            assert _window_of(a).is_tracked(IP_B) is False
        finally:
            await _drain(a, a_task)

        next_owner = bus.consumer(GROUP)
        message = await _take_one(await next_owner.subscribe(OBSERVATIONS_TOPIC))
        prefetcher = bus.prefetcher
        assert prefetcher is not None
        assert message.key == str(IP_B).encode()
        assert message == prefetcher.queued[0]

    async def test_the_next_member_applies_the_message_the_first_member_left_in_its_queue(
        self,
    ) -> None:
        clock = ManualClock(initial=BASE)
        a_metrics = AggregatorMetrics()
        b_metrics = AggregatorMetrics()
        state_store = MemoryShardStateStore()
        bus = _PrefetchBus(batch=2)
        await self._publish_two(bus)

        # Member A: handles m1, has m2 in its queue, is stopped.
        a = _worker(bus=bus, clock=clock, metrics=a_metrics, state_store=state_store)
        await a.start()
        a_task = asyncio.create_task(a.run())
        try:
            await _yield_until(lambda: _window_of(a).state(IP_A) is IpState.HOT)
            assert [envelope.subject for _key, envelope in _decoded(bus)] == [str(IP_A)]
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A}), next_sequence=1
            )
        finally:
            await _drain(a, a_task)

        # Member B: a fresh member of the same group with a distinct id,
        # started only after A is gone. Its first fetch is m2.
        b = _worker(
            bus=bus, clock=clock, metrics=b_metrics, state_store=state_store, member_id=MEMBER_B
        )
        await b.start()
        b_task = asyncio.create_task(b.run())
        try:
            # HOT is set after the store write and the publish (decision 4
            # steps 2, 4, 5), so waiting on it covers everything asserted
            # below; `is_tracked` would already hold at `observe`.
            await _yield_until(lambda: _window_of(b).state(IP_B) is IpState.HOT)

            window = _window_of(b)
            assert window.total(IP_B) == 1200
            assert window.state(IP_B) is IpState.HOT
            # m1 was acknowledged by A and not re-read: IP_A is B's inherited
            # entry with an empty ring.
            assert window.total(IP_A) == 0
            assert window.is_inherited(IP_A) is True
            decoded = _decoded(bus)
            identities = [
                (key, envelope.event_type, envelope.subject, envelope.agent_id, envelope.sequence)
                for key, envelope in decoded
            ]
            assert identities == [
                (str(IP_A).encode(), "HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0),
                (str(IP_B).encode(), "HotIpAdded", str(IP_B), SHARD_AGENT_ID, 1),
            ]
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A, IP_B}), next_sequence=2
            )
            # One promotion each, on the observation path.
            for member_metrics in (a_metrics, b_metrics):
                assert (
                    _counter(
                        member_metrics,
                        "cold_to_hot_transitions",
                        shard=0,
                        config_version=1,
                        reason="observation",
                    )
                    == 1
                )
            assert _records(bus, RECONCILIATION_TOPIC) == []
        finally:
            await _drain(b, b_task)


class TestAMessageThatCompletesWithTheStopSignal:
    """ADR-0013 decision 8 as amended (Amendment 4 ruling R2): "`stop()`'s order is
    total ... `run()`, on a wake-up in which the stop signal and a received
    message are both complete, returns without handing the message to
    `handle()`. The message in hand -- one whose `_handle` holds the lock
    when the flag is set -- is finished and covered by the final commit, as
    before; one merely waiting for the lock is not in hand and takes the
    `UNCLAIMED` path." Assumption 82: "The `run()` skip drops the message
    received in the same wake-up unhandled ... either way the next member
    applies it once. Handling it after the final commit is the defect".

    ASSUMPTION 8 (module docstring) explains the `_GatedBus` shape and why the
    invariant asserted is the one that holds whichever way the wake-ups fall:
    never applied, never marked, handed to the next consumer of the group.
    """

    async def test_the_message_is_never_applied_and_reaches_the_next_member(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = _GatedBus()
        metrics = AggregatorMetrics()
        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(20):  # let `run()` reach its receive and block behind the gate
                await asyncio.sleep(0)
            await bus.producer().publish(
                OBSERVATIONS_TOPIC,
                key=str(IP_A),
                value=_observation(IP_A, 1200, window_start=BASE),
                message_id="m-a",
            )
            for _ in range(10):  # the message is in the log, held back by the gate
                await asyncio.sleep(0)
            assert _window_of(worker).is_tracked(IP_A) is False
            assert not task.done()

            # The gate opens and `stop()` begins with no `await` between the
            # two: the receive completes on the first wake-up that follows,
            # the one that also carries the stop signal.
            bus.gate.set()
            await worker.stop()
            await _yield_until(task.done)
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        # `run()` returned on its own: neither cancelled nor failed.
        assert task.done()
        assert not task.cancelled()
        assert task.exception() is None
        # Never applied, never marked, nothing emitted.
        window = _window_of(worker)
        assert window.is_tracked(IP_A) is False
        assert window.tracked_count == 0
        assert worker.claims.handled_position(0) is None
        assert _records(bus, HOT_IP_TOPIC) == []
        assert (
            _counter(
                metrics, "cold_to_hot_transitions", shard=0, config_version=1, reason="observation"
            )
            == 0
        )
        # Delivered-unacknowledged: the next consumer of the group is handed it.
        next_owner = bus.consumer(GROUP)
        message = await _take_one(await next_owner.subscribe(OBSERVATIONS_TOPIC))
        assert message.key == str(IP_A).encode()

    async def test_the_same_message_is_applied_when_the_gate_opens_without_a_stop(self) -> None:
        # The control for the test above: the gate, not the message, is what
        # keeps it out of the window -- opened with the worker live, the
        # message is applied and promotes the IP.
        clock = ManualClock(initial=BASE)
        bus = _GatedBus()
        metrics = AggregatorMetrics()
        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            await bus.producer().publish(
                OBSERVATIONS_TOPIC,
                key=str(IP_A),
                value=_observation(IP_A, 1200, window_start=BASE),
                message_id="m-a",
            )
            for _ in range(10):
                await asyncio.sleep(0)
            assert _window_of(worker).is_tracked(IP_A) is False

            bus.gate.set()

            await _yield_until(lambda: _window_of(worker).state(IP_A) is IpState.HOT)
            assert _window_of(worker).total(IP_A) == 1200
        finally:
            await _drain(worker, task)

    async def test_a_message_published_after_stop_is_never_applied(self) -> None:
        # The plain "after" half of the invariant, with no gate at all:
        # `stop()` has returned, and a message that arrives afterwards is not
        # this member's to apply.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        producer = bus.producer()
        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            await worker.stop()
            await producer.publish(
                OBSERVATIONS_TOPIC,
                key=str(IP_A),
                value=_observation(IP_A, 1200, window_start=BASE),
                message_id="m-a",
            )
            for _ in range(20):
                await asyncio.sleep(0)
        finally:
            await _drain(worker, task)

        assert _window_of(worker).is_tracked(IP_A) is False
        assert worker.claims.handled_position(0) is None
        next_owner = bus.consumer(GROUP)
        message = await _take_one(await next_owner.subscribe(OBSERVATIONS_TOPIC))
        assert message.key == str(IP_A).encode()
