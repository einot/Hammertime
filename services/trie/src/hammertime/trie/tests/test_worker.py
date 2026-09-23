"""The trie worker: positional replay, one outcome per message, and the log position.

Spec: section 19 (the trie consumes `HotIpAdded`/`HotIpRemoved`), section 22
(`event_sequence` and `as_of`), section 28 (a reader never sees a partially
applied event), section 33 (replay), section 35 (one root per family), section
37 and section 46.8 (the counters), section 46.5 (the trie and the attribute
record move in one step).

Written from ADR-0017 decisions 3-10 and 12, its "Test seams" and its
"Revision 2026-09-23", with the documents it cites (ADR-0013 decision 9: the
whole-topic positional subscription and `end_offset`; ADR-0013 decision 3 and
Amendment 10: `first_offset`; ADR-0014 A12: `InvariantViolation`; ADR-0015
decision 6: `apply_hot_ip_added` / `apply_hot_ip_removed`). What is pinned:

* `start()` reads `end_offset`, then `first_offset`, both before subscribing,
  keeps `end` as `replay_target`, and handles messages until caught up
  (`event_sequence >= end`); a second `start()` returns at once (decision 4
  steps 1, 2 and 5);
* decision 4 step 3: when `first > event_sequence`, `start()` passes the
  offsets below `first` (`note_passed(first - 1)`) before subscribing, so a
  log that holds nothing to replay -- never written, every record aged out or
  purged, or a `first` read after `end` that exceeds it -- is caught up at
  once with `event_sequence == first`; a log whose head has aged out is
  replayed from `first`; the step does not run when `first <=
  event_sequence`, and is skipped once `stop()` has begun (assumption 29);
  a restarted trie does not report a smaller `event_sequence` than its
  predecessor (decision 8, assumption 26);
* an exception from `first_offset` propagates out of `start()` before
  anything is subscribed (decision 7's table);
* `start()` subscribes with `subscribe(HOT_IP.name, start_offset=S)`,
  `partitions=None` and no listener, where `S` is `state.event_sequence`
  after step 3: `0` for a fresh state on the memory bus and `position + 1`
  for one that already has a position (decision 3); a stream that ends
  before the worker is caught up is a `RuntimeError`, and once `stop()` has
  begun `start()` returns without being caught up (decision 4 step 5);
* `replay_complete` is logged at INFO with `start_offset`, `first_offset`,
  `replay_target` and `event_sequence` (decision 12's table);
* `handle()` returns one of six outcomes, in decision 6's order:
  REDELIVERED (offset not past `position`), MALFORMED (codec error; payload not
  a hot-ip event; key or subject not the payload's IP), FAMILY_NOT_SERVED,
  then APPLIED / UNCHANGED from the apply step; STOPPED once `stop()` has
  begun;
* `position` moves for APPLIED, UNCHANGED, MALFORMED and FAMILY_NOT_SERVED,
  and not for REDELIVERED, STOPPED or an `InvariantViolation`; `as_of` moves
  only for APPLIED and UNCHANGED (decision 8);
* the counters of decision 12, counted exactly as decision 6 says;
* the apply-time `InvalidAttributesError` retries with `attributes=None`
  (decision 7), reached through the `apply_hot_ip_added` module global of
  `hammertime.trie.worker` (Test seams); both calls pass
  `request_count=payload.window_count` as a keyword (ADR-0015 Amendment 5
  ruling 7, and decisions 6 and 17 as noted);
* the record's `request_count` (ADR-0015 Amendment 5 rulings 1 and 4): an
  applied add stores its `window_count`, an `UNCHANGED` add replaces it, a
  removal deletes it with the record, a skipped add sets nothing, and a
  replay leaves each IP the count of its latest applied add
  (`TestRequestCount`); `_view` compares counts as well as documents;
* log records carry no document content (decision 12,
  `TestLogRecordsCarryNoDocumentContent`);
* the log records of decision 12's table that the reviewer asked for, read
  through `caplog` on the `hammertime.trie.worker` logger (Test seams):
  `malformed_hot_ip_event` at WARNING with each decision 6 reason token,
  `redelivered_hot_ip_event` at WARNING, and `family_not_served` at WARNING
  the first time per family per process and DEBUG after;
* R1 of decision 9: a concurrent reader sees `len(records) ==
  hot_ip_count == event_sequence` at every resume while bursts of distinct
  adds are consumed from offset 0, and it resumes at least once strictly
  between 0 and the last event (`TestReadersSeeWholeEvents`).

Choices of this file's own:

* Messages are built directly as `ConsumedMessage`s for `handle()` ("Test
  seams": "`handle()` takes any `ConsumedMessage`"), and published to an
  `InMemoryBus` for `start()`/`run()`. Memory offsets start at 0 and
  `end_offset` is the log length (ADR-0013 decisions 3 and 9).
* `handle()` is called on a worker that has not been started. Decision 6
  gives `handle()` no precondition other than `stop()`, and the Test seams
  paragraph says nothing requires the message to come from the worker's own
  consumer.
* Waiting is `asyncio.sleep(0)` in a bounded loop; `asyncio.wait_for(...,
  10.0)` appears only as a failure bound on `run()` or `start()` returning.
* The hot-ip envelope is shaped as ADR-0011 decision 4 has the aggregator
  shape it: `agent_id="aggregator-shard-0"`, `subject` and key the IP's
  canonical text, `window_count` present (the codec requires it).
* The order of `start()`'s bus calls and its `subscribe` arguments are read
  off `_SpyBus`, which wraps an `InMemoryBus` and satisfies ADR-0013
  decision 3's `MessageBus` and `Consumer` protocols. The two early exits of
  `start()`, and every JetStream log shape the memory bus cannot show
  ("Test seams", "The log's bounds"), use `_ScriptedBus`, whose `end_offset`
  and `first_offset` return what the test chooses (`first` defaults to `0`,
  the memory bus's value) and whose stream yields exactly the messages a
  test queues and then either ends or waits for the next `feed()`. After
  `stop()` the test feeds one more message rather than ending the stream:
  which of the two rules wins when the stream ends *after* `stop()` has
  begun is not something decision 4 says.
* A log field is read as the `name=value` token after the event name
  (decision 12: "Each message starts with the event name, followed by
  `key=value` fields").
* "The first time per family per process" (decision 12's table) is checked
  in a fresh interpreter, because pytest runs every test in one process and
  another test may already have met IPv6. The child imports this file by
  path and uses its helpers; it runs no event loop longer than the handles.
"""

import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import hammertime.trie.worker as trie_worker
import pytest
from hammertime.bus.interface import (
    AssignmentListener,
    ConsumedMessage,
    Consumer,
    MessageBus,
    Producer,
)
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import HOT_IP
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import InvalidAttributesError, InvariantViolation
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import (
    HotIpAdded,
    HotIpRemoved,
    Observation,
    PrefixStatsChanged,
    RequestObservation,
)
from hammertime.trie.metadata.ip_attributes import apply_hot_ip_added
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.state import TrieState
from hammertime.trie.structure.invariants import check_attribute_records, check_trie
from hammertime.trie.worker import CONSUMER_GROUP, HotIpOutcome, TrieWorker

IPV4 = AddressFamily.IPV4
IPV6 = AddressFamily.IPV6
ONLY_V4 = frozenset({IPV4})
BOTH = frozenset({IPV4, IPV6})

TOPIC = HOT_IP.name
AGENT_ID = "aggregator-shard-0"

# `docs/spec/integration-scenarios.md` section 2's T0.
T0 = datetime.fromtimestamp(1_800_000_000, tz=UTC)

IP_A = Address.parse("10.20.30.1")
IP_B = Address.parse("10.20.30.2")
IP_C = Address.parse("10.20.30.3")
IP_D = Address.parse("10.20.30.4")
IP_V6 = Address.parse("2001:db8::1")

DEFAULT_DOCUMENT: dict[str, Any] = {"attributes_version": 1}

_SAME = "<the payload's ip>"

STEPS = 10_000


def _config(version: int = 1) -> DetectionConfig:
    """`config/detection.v1.json`'s values (integration-scenarios section 2.1)."""

    return DetectionConfig(
        config_version=version,
        window_seconds=300,
        bucket_seconds=10,
        hot_threshold=1000,
        cold_threshold=800,
        allowed_lateness_seconds=30,
        state_retention_seconds=600,
    )


def _ip(i: int) -> Address:
    return Address.parse(f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}")


def _envelope(
    ip: Address,
    n: int,
    *,
    removed: bool = False,
    attributes: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
    subject: str | None = _SAME,
    window_count: int | None = None,
) -> EventEnvelope[Any]:
    """One hot-ip event as the aggregator emits it (ADR-0011 decision 4).

    `n` makes the envelope's identity -- and so its `event_id` -- distinct.
    `window_count` defaults to 750 on a removal and 1200 on an add.
    """

    ts = timestamp if timestamp is not None else T0 + timedelta(seconds=n)
    payload: HotIpAdded | HotIpRemoved
    if removed:
        payload = HotIpRemoved(
            ip=ip,
            timestamp=ts,
            sequence=n + 1,
            window_count=750 if window_count is None else window_count,
            config_version=1,
            attributes=attributes,
        )
    else:
        payload = HotIpAdded(
            ip=ip,
            timestamp=ts,
            sequence=n + 1,
            window_count=1200 if window_count is None else window_count,
            config_version=1,
            attributes=attributes,
        )
    return EventEnvelope(
        agent_id=AGENT_ID,
        sequence=n + 1,
        event_type="HotIpRemoved" if removed else "HotIpAdded",
        config_version=1,
        timestamp=ts,
        subject=str(ip) if subject == _SAME else subject,
        payload=payload,
    )


def _message(value: bytes, offset: int, key: bytes | None) -> ConsumedMessage:
    return ConsumedMessage(topic=TOPIC, partition=0, offset=offset, key=key, value=value)


def _msg(envelope: EventEnvelope[Any], offset: int) -> ConsumedMessage:
    """The envelope encoded and keyed by its payload's IP, at `offset`."""

    return _message(encode(envelope), offset, str(envelope.payload.ip).encode())


def _with_attributes(envelope: EventEnvelope[Any], attributes: object) -> bytes:
    """The envelope's wire bytes with `payload.attributes` replaced after encoding.

    `encode` refuses an invalid document, so a decode-time rejection can only
    be built by editing the bytes; `event_id` does not depend on the payload.
    """

    doc = json.loads(encode(envelope))
    doc["payload"]["attributes"] = attributes
    return json.dumps(doc).encode("utf-8")


async def _publish(bus: InMemoryBus, envelope: EventEnvelope[Any], *, dedupe: bool = True) -> None:
    await bus.producer().publish(
        TOPIC,
        key=str(envelope.payload.ip),
        value=encode(envelope),
        message_id=envelope.event_id if dedupe else None,
    )


def _worker(
    *,
    bus: InMemoryBus | None = None,
    families: frozenset[AddressFamily] = ONLY_V4,
    config: DetectionConfig | None = None,
) -> TrieWorker:
    state = TrieState(families=families, config=config if config is not None else _config())
    return TrieWorker(
        bus=bus if bus is not None else InMemoryBus(), state=state, metrics=TrieMetrics()
    )


def _view(worker: TrieWorker, family: AddressFamily = IPV4) -> tuple[object, ...]:
    """Everything a family's pair holds, deep enough to compare before and
    after: each record's document and its `request_count` (ADR-0015
    Amendment 5 ruling 2)."""

    fs = worker.state.of(family)
    records = {
        address: (dict(record.attributes), record.request_count)
        for address, record in fs.records.items()
    }
    return (fs.trie.hot_ip_count, fs.trie.node_count, records, fs.records.serialized_bytes)


def _stored(worker: TrieWorker, ip: Address) -> tuple[dict[str, object], int]:
    """The IP's record as (document, request_count)."""

    record = worker.state.of(ip.family).records[ip]
    return dict(record.attributes), record.request_count


def _updates(worker: TrieWorker, family: str, event_type: str, result: str) -> int | float:
    return worker.metrics.get("trie_updates", family=family, event_type=event_type, result=result)


def _updates_total(worker: TrieWorker) -> int | float:
    return sum(
        _updates(worker, family, event_type, result)
        for family in ("ipv4", "ipv6")
        for event_type in ("HotIpAdded", "HotIpRemoved")
        for result in ("applied", "unchanged")
    )


def _skipped(worker: TrieWorker, reason: str) -> int | float:
    return worker.metrics.get("hot_ip_events_skipped", reason=reason)


def _rejected(worker: TrieWorker, stage: str) -> int | float:
    return worker.metrics.get("attributes_rejected", stage=stage)


async def _yield_until(predicate: Callable[[], bool], *, steps: int = STEPS) -> None:
    """Yield to the event loop until `predicate` holds. Never waits on the wall clock."""

    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was never reached")


async def _stop_and_join(worker: TrieWorker, task: asyncio.Task[None]) -> None:
    """`stop()`, then `run()` must return; the bound is reached only on failure."""

    await worker.stop()
    await asyncio.wait_for(task, timeout=10.0)


def _corrupt_single_leaf(worker: TrieWorker, ip: Address) -> None:
    """ADR-0014 A12 / ADR-0017 Test seams: a leaf whose stored count is not 1.

    In a single-address trie the root is the leaf.
    """

    trie = worker.state.of(ip.family).trie
    assert trie.add_hot_ip(ip) is True
    assert trie.node_count == 1
    trie.arena.hot_count[trie.root] = 0


LOGGER = "hammertime.trie.worker"


def _logged(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    """The worker's records whose message starts with `event` (decision 12)."""

    found: list[logging.LogRecord] = []
    for record in caplog.records:
        words = record.getMessage().split()
        if record.name == LOGGER and words and words[0] == event:
            found.append(record)
    return found


def _field(record: logging.LogRecord, name: str) -> str | None:
    """The value of the record's `name=value` field, or `None` if it has none."""

    for word in record.getMessage().split()[1:]:
        key, sep, value = word.partition("=")
        if sep and key == name:
            return value.strip("\"'")
    return None


class _SpyConsumer:
    """Delegates to a real consumer and records `subscribe`'s arguments."""

    def __init__(self, inner: Consumer, calls: list[tuple[str, object]]) -> None:
        self._inner = inner
        self._calls = calls

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        arguments = {
            "topic": topic,
            "partitions": partitions,
            "listener": listener,
            "start_offset": start_offset,
        }
        self._calls.append(("subscribe", arguments))
        return await self._inner.subscribe(
            topic, partitions=partitions, listener=listener, start_offset=start_offset
        )

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        await self._inner.ack(messages)

    async def close(self) -> None:
        await self._inner.close()


class _SpyBus:
    """An `InMemoryBus` whose `consumer`, `end_offset`, `first_offset` and
    `subscribe` calls are recorded, in the order they are made."""

    def __init__(self) -> None:
        self.inner = InMemoryBus()
        self.calls: list[tuple[str, object]] = []

    def producer(self) -> Producer:
        return self.inner.producer()

    def consumer(self, group_id: str) -> Consumer:
        self.calls.append(("consumer", group_id))
        return _SpyConsumer(self.inner.consumer(group_id), self.calls)

    async def end_offset(self, topic: str) -> int:
        self.calls.append(("end_offset", topic))
        return await self.inner.end_offset(topic)

    async def first_offset(self, topic: str) -> int:
        self.calls.append(("first_offset", topic))
        return await self.inner.first_offset(topic)


def _subscriptions(calls: list[tuple[str, object]]) -> list[object]:
    return [arguments for name, arguments in calls if name == "subscribe"]


def _positional(start_offset: int) -> dict[str, object]:
    """Decision 3's subscription: the whole topic, no listener, from `start_offset`."""

    return {"topic": TOPIC, "partitions": None, "listener": None, "start_offset": start_offset}


class _ScriptedStream:
    """Yields the queued messages in order. When none is queued it ends, or, if
    `hold`, waits until `feed()` queues another."""

    def __init__(self, messages: Iterable[ConsumedMessage], *, hold: bool) -> None:
        self._queue = list(messages)
        self._hold = hold
        self._arrived = asyncio.Event()
        self.waiting = False
        self.taken = 0

    def feed(self, message: ConsumedMessage) -> None:
        self._queue.append(message)
        self._arrived.set()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ConsumedMessage:
        while not self._queue:
            if not self._hold:
                raise StopAsyncIteration
            self._arrived.clear()
            self.waiting = True
            try:
                await self._arrived.wait()
            finally:
                self.waiting = False
        self.taken += 1
        return self._queue.pop(0)


class _ScriptedConsumer:
    def __init__(self, stream: _ScriptedStream, calls: list[tuple[str, object]]) -> None:
        self._stream = stream
        self._calls = calls

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        arguments = {
            "topic": topic,
            "partitions": partitions,
            "listener": listener,
            "start_offset": start_offset,
        }
        self._calls.append(("subscribe", arguments))
        return self._stream

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        # Decision 3: "Nothing is acknowledged and nothing is committed."
        raise AssertionError("the trie acknowledged a positional subscription")

    async def close(self) -> None:
        return None


class _ScriptedBus:
    """A bus whose log end is `end`, whose first retained offset is `first`, and
    whose one consumer reads `stream`.

    `first` defaults to `0`, the memory bus's value (ADR-0013 decision 3 as
    amended by Amendment 10), so a test that does not name it models a log
    that never discards. A JetStream log that holds nothing to replay, or
    whose head has aged out, names it (ADR-0017 Test seams, "The log's
    bounds")."""

    def __init__(self, stream: _ScriptedStream, *, end: int, first: int = 0) -> None:
        self.stream = stream
        self.calls: list[tuple[str, object]] = []
        self._end = end
        self._first = first
        self._consumer = _ScriptedConsumer(stream, self.calls)

    def producer(self) -> Producer:
        return InMemoryBus().producer()

    def consumer(self, group_id: str) -> Consumer:
        self.calls.append(("consumer", group_id))
        return self._consumer

    async def end_offset(self, topic: str) -> int:
        self.calls.append(("end_offset", topic))
        return self._end

    async def first_offset(self, topic: str) -> int:
        self.calls.append(("first_offset", topic))
        return self._first


class _FirstOffsetFailed(Exception):
    """Raised by `_FailingFirstOffsetBus.first_offset`; no other code raises it."""


class _FailingFirstOffsetBus(_ScriptedBus):
    """A `_ScriptedBus` whose `first_offset` records the call, then raises."""

    async def first_offset(self, topic: str) -> int:
        self.calls.append(("first_offset", topic))
        raise _FirstOffsetFailed


def _worker_on(bus: MessageBus, state: TrieState | None = None) -> TrieWorker:
    if state is None:
        state = TrieState(families=ONLY_V4, config=_config())
    return TrieWorker(bus=bus, state=state, metrics=TrieMetrics())


def _added(i: int) -> ConsumedMessage:
    """A `HotIpAdded` for `_ip(i + 1)` at offset `i`."""

    return _msg(_envelope(_ip(i + 1), i), i)


class TestConstants:
    def test_the_consumer_group(self) -> None:
        assert CONSUMER_GROUP == "hammertime-trie"

    def test_the_six_outcomes(self) -> None:
        assert {outcome.value for outcome in HotIpOutcome} == {
            "applied",
            "unchanged",
            "malformed",
            "family_not_served",
            "redelivered",
            "stopped",
        }

    def test_the_constructor_binds_the_metrics_to_the_state(self) -> None:
        worker = _worker()
        fs = worker.state.of(IPV4)

        fs.trie.add_hot_ip(IP_A)

        assert worker.metrics.get("hot_ip_count", family="ipv4") == 1


class TestReplayAndReadiness:
    async def test_an_empty_log_is_caught_up_at_once(self) -> None:
        # Decision 4: "On the memory bus `first_offset` is always `0` ... Step
        # 3 therefore never runs there": an empty memory log ends at `0`, and
        # nothing is passed.
        worker = _worker()
        assert worker.replay_target is None

        await worker.start()
        try:
            assert worker.caught_up is True
            assert worker.replay_target == 0
            assert worker.state.event_sequence == 0
            assert worker.state.position is None
        finally:
            await worker.stop()

    async def test_start_replays_every_record_published_before_it(self) -> None:
        n = 20
        bus = InMemoryBus()
        documents: dict[Address, dict[str, object]] = {}
        for i in range(n):
            ip = _ip(i + 1)
            documents[ip] = {"attributes_version": 1, "weight": i + 1}
            await _publish(bus, _envelope(ip, i, attributes=documents[ip]))
        worker = _worker(bus=bus)

        await worker.start()
        try:
            state = worker.state
            fs = state.of(IPV4)
            assert worker.caught_up is True
            assert worker.replay_target == n
            assert state.event_sequence == n
            assert state.position == n - 1
            assert fs.trie.hot_ip_count == len(fs.records) == n
            check_trie(fs.trie)
            check_attribute_records(fs.trie, fs.records)
            for ip, document in documents.items():
                assert dict(fs.records[ip].attributes) == document
            assert state.as_of == T0 + timedelta(seconds=n - 1)
        finally:
            await worker.stop()

    async def test_a_second_start_returns_at_once(self) -> None:
        bus = InMemoryBus()
        for i in range(3):
            await _publish(bus, _envelope(_ip(i + 1), i))
        worker = _worker(bus=bus)
        await worker.start()
        before = (worker.replay_target, worker.state.event_sequence, _view(worker))
        # Decision 4 step 1: "If it has already subscribed, it returns" -- the
        # log end is not read again.
        await _publish(bus, _envelope(_ip(99), 3))

        await worker.start()
        try:
            assert (worker.replay_target, worker.state.event_sequence, _view(worker)) == before
            assert worker.caught_up is True
        finally:
            await worker.stop()

    async def test_run_applies_what_is_published_after_start_and_returns_on_stop(self) -> None:
        bus = InMemoryBus()
        worker = _worker(bus=bus)
        await worker.start()
        task = asyncio.create_task(worker.run())
        fs = worker.state.of(IPV4)

        await _publish(bus, _envelope(IP_A, 0, attributes={"attributes_version": 1, "weight": 9}))
        await _yield_until(lambda: fs.trie.hot_ip_count == 1)

        assert dict(fs.records[IP_A].attributes) == {"attributes_version": 1, "weight": 9}
        assert worker.state.event_sequence == 1
        await _stop_and_join(worker, task)
        assert task.exception() is None

    async def test_run_before_start_is_a_runtime_error(self) -> None:
        worker = _worker()

        with pytest.raises(RuntimeError):
            await worker.run()


class TestStartRules:
    """Decision 3 (the subscription and its start offset) and decision 4
    (`start()`'s steps and its two early exits)."""

    async def test_end_offset_is_read_before_subscribing(self) -> None:
        bus = _SpyBus()
        for i in range(3):
            await _publish(bus.inner, _envelope(_ip(i + 1), i))
        worker = _worker_on(bus)
        # Decision 6: "The constructor takes the consumer from `bus`".
        assert bus.calls == [("consumer", CONSUMER_GROUP)]

        await worker.start()
        try:
            # Decision 4 step 2: "It reads `end = await bus.end_offset(HOT_IP.name)`,
            # then `first = await bus.first_offset(HOT_IP.name)`: both *before*
            # subscribing, and in that order"; step 4 subscribes (decision 3).
            assert bus.calls[1:] == [
                ("end_offset", TOPIC),
                ("first_offset", TOPIC),
                ("subscribe", _positional(0)),
            ]
            assert worker.replay_target == 3
            assert worker.caught_up is True
            assert worker.state.event_sequence == 3
        finally:
            await worker.stop()

    async def test_a_state_with_a_position_is_replayed_from_the_next_offset(self) -> None:
        bus = _SpyBus()
        for i in range(5):
            await _publish(bus.inner, _envelope(_ip(i + 1), i))
        state = TrieState(families=ONLY_V4, config=_config())
        state.note_handled(2)
        worker = _worker_on(bus, state)

        await worker.start()
        try:
            # Decision 3: "`S` is ... `state.position + 1` otherwise".
            assert _subscriptions(bus.calls) == [_positional(3)]
            fs = state.of(IPV4)
            assert worker.replay_target == 5
            assert worker.caught_up is True
            assert state.position == 4
            assert state.event_sequence == 5
            assert set(fs.records) == {_ip(4), _ip(5)}
            assert fs.trie.hot_ip_count == 2
        finally:
            await worker.stop()

    async def test_a_state_already_at_the_log_end_takes_no_message(self) -> None:
        # Decision 4: caught up means `state.event_sequence >= end`, and here
        # `event_sequence` is already `end`. The stream ends at once, so taking
        # a message would end the replay in a `RuntimeError`.
        stream = _ScriptedStream([], hold=False)
        bus = _ScriptedBus(stream, end=3)
        state = TrieState(families=ONLY_V4, config=_config())
        state.note_handled(2)
        worker = _worker_on(bus, state)

        await worker.start()
        try:
            assert _subscriptions(bus.calls) == [_positional(3)]
            assert stream.taken == 0
            assert worker.replay_target == 3
            assert worker.caught_up is True
            assert state.event_sequence == 3
        finally:
            await worker.stop()

    async def test_a_stream_that_ends_before_the_log_end_is_a_runtime_error(self) -> None:
        # Decision 4 step 5: "If the iterator ends before that, `start()` raises
        # `RuntimeError`."
        stream = _ScriptedStream([_added(0)], hold=False)
        worker = _worker_on(_ScriptedBus(stream, end=3))

        try:
            with pytest.raises(RuntimeError):
                await worker.start()
            assert stream.taken == 1
            assert worker.state.position == 0
            assert worker.caught_up is False
        finally:
            await worker.stop()

    async def test_stop_during_the_replay_returns_without_catching_up(self) -> None:
        # Decision 4 step 5: "If `stop()` has begun, `start()` returns without
        # being caught up."
        stream = _ScriptedStream([_added(0)], hold=True)
        worker = _worker_on(_ScriptedBus(stream, end=3))
        task = asyncio.create_task(worker.start())
        await _yield_until(lambda: stream.waiting)
        assert worker.state.position == 0

        await worker.stop()
        # Decision 13: once `stop()` has begun, a message that arrives is not
        # applied -- `handle()` returns STOPPED if it is handed one.
        stream.feed(_added(1))
        await asyncio.wait_for(task, timeout=10.0)

        assert worker.replay_target == 3
        assert worker.caught_up is False
        assert worker.state.position == 0
        assert _ip(2) not in worker.state.of(IPV4).records

    async def test_start_after_stop_returns_without_catching_up(self) -> None:
        bus = InMemoryBus()
        for i in range(3):
            await _publish(bus, _envelope(_ip(i + 1), i))
        worker = _worker(bus=bus)

        await worker.stop()
        await asyncio.wait_for(worker.start(), timeout=10.0)

        assert worker.caught_up is False
        assert worker.state.position is None
        assert worker.state.of(IPV4).trie.hot_ip_count == 0


def _held(offsets: Iterable[int]) -> _ScriptedStream:
    """A held stream yielding `_added(i)` for each offset, then waiting."""

    return _ScriptedStream([_added(i) for i in offsets], hold=True)


class TestPassingWhatTheLogNoLongerHolds:
    """Decision 4 step 3: "If `first > state.event_sequence`, the log retains
    no record between the trie's position and `first` ... The worker takes its
    lock and, unless `stop()` has begun, calls `state.note_passed(first - 1)`,
    so that `event_sequence` becomes `first`." Step 4 then subscribes from
    `S = state.event_sequence`, and caught up is `state.event_sequence >= end`.

    The memory bus cannot show this ("Test seams", "The log's bounds"), so
    each JetStream log shape is a `_ScriptedBus` with the test's `end` and
    `first`. Every stream here is held: a worker that tried to take a message
    from an empty one would wait, and `asyncio.wait_for(..., 10.0)` is the
    failure bound on that."""

    @pytest.mark.parametrize(
        "end",
        [
            # "*A stream nothing has been written to.* `end` and `first` are
            # both `1`."
            pytest.param(1, id="never-written"),
            # "*A stream whose every record has aged out or been purged.*
            # `first` is `end`, because the stream keeps its last sequence."
            pytest.param(501, id="every-record-aged-out-or-purged"),
        ],
    )
    async def test_a_log_holding_nothing_to_replay_is_caught_up_at_once(self, end: int) -> None:
        stream = _ScriptedStream([], hold=True)
        bus = _ScriptedBus(stream, end=end, first=end)
        worker = _worker_on(bus)

        try:
            await asyncio.wait_for(worker.start(), timeout=10.0)

            state = worker.state
            fs = state.of(IPV4)
            assert worker.caught_up is True
            assert worker.replay_target == end
            # `note_passed(first - 1)`: "`event_sequence` becomes `first`".
            assert state.event_sequence == end
            assert state.position == end - 1
            # Decision 2: `note_passed` "sets `position = offset`, and nothing
            # else ... `as_of` does not move."
            assert state.as_of is None
            assert stream.taken == 0
            # Step 4: "It subscribes (decision 3), from `S =
            # state.event_sequence`."
            assert _subscriptions(bus.calls) == [_positional(end)]
            assert fs.trie.hot_ip_count == 0
            assert len(fs.records) == 0
        finally:
            await worker.stop()

    async def test_a_log_whose_head_has_aged_out_is_replayed_from_first(self) -> None:
        # "*A stream whose head has aged out.* `0 < first < end`. The replay
        # reads `[first, end)`, as it did before, but subscribes from `first`."
        stream = _held(range(5, 10))
        bus = _ScriptedBus(stream, end=10, first=5)
        worker = _worker_on(bus)

        try:
            await asyncio.wait_for(worker.start(), timeout=10.0)

            state = worker.state
            fs = state.of(IPV4)
            assert _subscriptions(bus.calls) == [_positional(5)]
            assert worker.caught_up is True
            assert worker.replay_target == 10
            assert state.event_sequence == 10
            assert state.position == 9
            assert stream.taken == 5
            assert fs.trie.hot_ip_count == 5
            assert set(fs.records) == {_ip(i + 1) for i in range(5, 10)}
            # Decision 8: `as_of` is "the greatest `timestamp` among the
            # payloads of events whose outcome was `APPLIED` or `UNCHANGED`".
            assert state.as_of == T0 + timedelta(seconds=9)
        finally:
            await worker.stop()

    async def test_a_first_read_after_end_that_exceeds_it_is_passed_too(self) -> None:
        # "Why `end` is read first": "`first` can exceed `end` when records
        # are appended and the head ages out between the two reads. Step 3
        # then passes those offsets as well, and the trie is caught up at
        # once."
        stream = _ScriptedStream([], hold=True)
        bus = _ScriptedBus(stream, end=10, first=12)
        worker = _worker_on(bus)

        try:
            await asyncio.wait_for(worker.start(), timeout=10.0)

            assert worker.caught_up is True
            assert worker.replay_target == 10
            assert worker.state.event_sequence == 12
            assert worker.state.position == 11
            assert stream.taken == 0
            assert _subscriptions(bus.calls) == [_positional(12)]
        finally:
            await worker.stop()

    async def test_a_restarted_trie_does_not_report_a_smaller_event_sequence(self) -> None:
        # Decision 4, "Why step 3 moves the position": without it a restarted
        # trie "would report `0` until the next record arrived: backwards,
        # against decision 8 and `read-api-v1.md`" (assumption 26). Worker A
        # reads offsets 1..10 of a JetStream log; every record then ages out,
        # and worker B starts on a fresh state.
        bus_a = _ScriptedBus(_held(range(1, 11)), end=11, first=1)
        worker_a = _worker_on(bus_a)
        try:
            await asyncio.wait_for(worker_a.start(), timeout=10.0)
            assert worker_a.caught_up is True
            reported_by_a = worker_a.state.event_sequence
            assert reported_by_a == 11
        finally:
            await worker_a.stop()

        bus_b = _ScriptedBus(_ScriptedStream([], hold=True), end=11, first=11)
        worker_b = _worker_on(bus_b)
        try:
            await asyncio.wait_for(worker_b.start(), timeout=10.0)

            assert worker_b.caught_up is True
            assert worker_b.state.event_sequence == 11
            assert worker_b.state.event_sequence >= reported_by_a
            # "After step 3, `event_sequence` is the offset of the next record
            # the trie will read in every case. The subscription starts
            # there".
            assert _subscriptions(bus_b.calls) == [_positional(11)]
        finally:
            await worker_b.stop()

    async def test_nothing_is_passed_when_first_is_not_past_event_sequence(self) -> None:
        # Step 3 runs only "If `first > state.event_sequence`". A state at
        # position 6 (`event_sequence` 7) on a log whose first retained offset
        # is 3 subscribes from 7, decision 3's `state.position + 1`.
        state = TrieState(families=ONLY_V4, config=_config())
        state.note_handled(6)
        stream = _held([7, 8, 9])
        bus = _ScriptedBus(stream, end=10, first=3)
        worker = _worker_on(bus, state)

        try:
            await asyncio.wait_for(worker.start(), timeout=10.0)

            assert _subscriptions(bus.calls) == [_positional(7)]
            assert worker.caught_up is True
            assert state.event_sequence == 10
            assert state.position == 9
            assert stream.taken == 3
        finally:
            await worker.stop()

    async def test_nothing_is_passed_once_stop_has_begun(self) -> None:
        # Step 3: "unless `stop()` has begun"; decision 13: after `stop()` has
        # begun, "`start()`'s step 3 passes nothing"; assumption 29: "A worker
        # stopped before `start()` therefore stays not caught up on a log that
        # holds nothing to replay."
        stream = _ScriptedStream([], hold=True)
        worker = _worker_on(_ScriptedBus(stream, end=1, first=1))

        await worker.stop()
        await asyncio.wait_for(worker.start(), timeout=10.0)

        assert worker.caught_up is False
        assert worker.state.position is None
        assert worker.state.event_sequence == 0
        assert stream.taken == 0

    async def test_an_exception_from_first_offset_propagates_out_of_start(self) -> None:
        # Decision 7's table: "`bus.end_offset`, `bus.first_offset`,
        # `subscribe` | any exception | Propagates out of `start()`". Step 2
        # reads `first` before subscribing, so nothing is subscribed.
        bus = _FailingFirstOffsetBus(_ScriptedStream([], hold=True), end=3, first=0)
        worker = _worker_on(bus)

        try:
            with pytest.raises(_FirstOffsetFailed):
                await asyncio.wait_for(worker.start(), timeout=10.0)

            assert ("first_offset", TOPIC) in bus.calls
            assert _subscriptions(bus.calls) == []
            assert worker.caught_up is False
            assert worker.state.position is None
        finally:
            await worker.stop()


class TestReplayCompleteIsLogged:
    """Decision 4 step 6: "It logs `replay_complete` (decision 12)"; decision
    12's table: `replay_complete` | info | `start_offset`, `first_offset`,
    `replay_target`, `event_sequence`. Assumption 30: "`replay_complete`
    carries `first_offset`"."""

    @staticmethod
    def _fields(record: logging.LogRecord) -> dict[str, str | None]:
        return {
            name: _field(record, name)
            for name in ("start_offset", "first_offset", "replay_target", "event_sequence")
        }

    async def test_after_a_memory_replay(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        bus = InMemoryBus()
        for i in range(3):
            await _publish(bus, _envelope(_ip(i + 1), i))
        worker = _worker(bus=bus)

        await worker.start()
        try:
            records = _logged(caplog, "replay_complete")
            assert len(records) == 1
            assert records[0].levelno == logging.INFO
            assert self._fields(records[0]) == {
                "start_offset": "0",
                "first_offset": "0",
                "replay_target": "3",
                "event_sequence": "3",
            }
        finally:
            await worker.stop()

    async def test_after_passing_a_log_that_holds_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # `start_offset` is step 4's `S`, which step 3 has moved to `first`.
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        worker = _worker_on(_ScriptedBus(_ScriptedStream([], hold=True), end=11, first=11))

        try:
            await asyncio.wait_for(worker.start(), timeout=10.0)

            records = _logged(caplog, "replay_complete")
            assert len(records) == 1
            assert records[0].levelno == logging.INFO
            assert self._fields(records[0]) == {
                "start_offset": "11",
                "first_offset": "11",
                "replay_target": "11",
                "event_sequence": "11",
            }
        finally:
            await worker.stop()


class TestAddsAndRemoves:
    async def test_an_add_for_a_new_address_is_applied(self) -> None:
        worker = _worker()
        document = {"attributes_version": 1, "weight": 10}

        outcome = await worker.handle(_msg(_envelope(IP_A, 0, attributes=document), 0))

        fs = worker.state.of(IPV4)
        assert outcome is HotIpOutcome.APPLIED
        assert fs.trie.hot_ip_count == 1
        assert dict(fs.records[IP_A].attributes) == document
        assert worker.state.position == 0
        assert worker.state.event_sequence == 1
        assert worker.state.as_of == T0
        assert _updates(worker, "ipv4", "HotIpAdded", "applied") == 1

    async def test_an_add_for_an_address_already_hot_is_unchanged(self) -> None:
        worker = _worker()
        first = {"attributes_version": 1, "weight": 10}
        second = {"attributes_version": 1, "weight": 12345, "x_note": "again"}
        await worker.handle(_msg(_envelope(IP_A, 0, attributes=first), 0))
        fs = worker.state.of(IPV4)
        counts = (fs.trie.hot_ip_count, fs.trie.node_count, len(fs.records))

        outcome = await worker.handle(_msg(_envelope(IP_A, 1, attributes=second), 1))

        assert outcome is HotIpOutcome.UNCHANGED
        assert (fs.trie.hot_ip_count, fs.trie.node_count, len(fs.records)) == counts
        assert dict(fs.records[IP_A].attributes) == second
        assert worker.state.event_sequence == 2
        assert _updates(worker, "ipv4", "HotIpAdded", "unchanged") == 1
        assert _updates(worker, "ipv4", "HotIpAdded", "applied") == 1

    async def test_a_remove_for_a_hot_address_is_applied_and_prunes(self) -> None:
        worker = _worker()
        await worker.handle(_msg(_envelope(IP_A, 0), 0))

        outcome = await worker.handle(_msg(_envelope(IP_A, 1, removed=True), 1))

        fs = worker.state.of(IPV4)
        assert outcome is HotIpOutcome.APPLIED
        assert IP_A not in fs.records
        assert len(fs.records) == 0
        assert fs.trie.hot_ip_count == 0
        assert fs.trie.node_count == 0
        assert worker.state.event_sequence == 2
        assert _updates(worker, "ipv4", "HotIpRemoved", "applied") == 1

    async def test_a_remove_for_an_address_not_hot_is_unchanged(self) -> None:
        worker = _worker()
        await worker.handle(_msg(_envelope(IP_A, 0, timestamp=T0 + timedelta(seconds=30)), 0))
        before = _view(worker)
        as_of = worker.state.as_of

        # A timestamp no later than `as_of`, so that only the position moves.
        outcome = await worker.handle(_msg(_envelope(IP_B, 1, removed=True, timestamp=T0), 1))

        assert outcome is HotIpOutcome.UNCHANGED
        assert _view(worker) == before
        assert worker.state.as_of == as_of
        assert worker.state.position == 1
        assert worker.state.event_sequence == 2
        assert _updates(worker, "ipv4", "HotIpRemoved", "unchanged") == 1

    async def test_an_unchanged_event_still_moves_as_of_forward(self) -> None:
        # Decision 8: `as_of` is the greatest timestamp among events whose
        # outcome was APPLIED *or* UNCHANGED.
        worker = _worker()
        later = T0 + timedelta(seconds=90)

        outcome = await worker.handle(_msg(_envelope(IP_B, 0, removed=True, timestamp=later), 0))

        assert outcome is HotIpOutcome.UNCHANGED
        assert worker.state.as_of == later

    async def test_an_add_without_attributes_stores_the_default_document(self) -> None:
        worker = _worker()

        outcome = await worker.handle(_msg(_envelope(IP_A, 0, attributes=None), 0))

        assert outcome is HotIpOutcome.APPLIED
        assert dict(worker.state.of(IPV4).records[IP_A].attributes) == DEFAULT_DOCUMENT

    async def test_a_remove_carrying_attributes_stores_nothing(self) -> None:
        worker = _worker()
        carried = {"attributes_version": 1, "weight": 5}
        await worker.handle(_msg(_envelope(IP_A, 0), 0))

        remove_hot = _envelope(IP_A, 1, removed=True, attributes=carried)
        remove_cold = _envelope(IP_B, 2, removed=True, attributes=carried)

        applied = await worker.handle(_msg(remove_hot, 1))
        unchanged = await worker.handle(_msg(remove_cold, 2))

        fs = worker.state.of(IPV4)
        assert applied is HotIpOutcome.APPLIED
        assert unchanged is HotIpOutcome.UNCHANGED
        assert len(fs.records) == 0
        assert IP_A not in fs.records
        assert IP_B not in fs.records
        assert fs.records.serialized_bytes == 0


def _not_json() -> tuple[bytes, bytes | None]:
    return b"not json", str(IP_A).encode()


def _request_observation() -> tuple[bytes, bytes | None]:
    envelope = EventEnvelope(
        agent_id="edge-17",
        sequence=1,
        event_type="RequestObservation",
        config_version=1,
        timestamp=T0,
        subject=str(IP_A),
        payload=RequestObservation(
            agent_id="edge-17",
            sequence=1,
            window_start=T0,
            window_seconds=10,
            observations=(Observation(ip=IP_A, request_count=1200),),
        ),
    )
    return encode(envelope), str(IP_A).encode()


def _prefix_stats_changed() -> tuple[bytes, bytes | None]:
    envelope = EventEnvelope(
        agent_id="trie-primary",
        sequence=1,
        event_type="PrefixStatsChanged",
        config_version=1,
        timestamp=T0,
        subject="10.20.30.0/24",
        payload=PrefixStatsChanged(
            prefix="10.20.30.0/24", hot_count=1, capacity=256, sequence=1, timestamp=T0
        ),
    )
    return encode(envelope), b"10.20.30.0/24"


def _key_of_another_ip() -> tuple[bytes, bytes | None]:
    return encode(_envelope(IP_A, 1)), str(IP_B).encode()


def _no_key() -> tuple[bytes, bytes | None]:
    return encode(_envelope(IP_A, 1)), None


def _subject_of_another_ip() -> tuple[bytes, bytes | None]:
    return encode(_envelope(IP_A, 1, subject=str(IP_B))), str(IP_A).encode()


def _no_subject() -> tuple[bytes, bytes | None]:
    return encode(_envelope(IP_A, 1, subject=None)), str(IP_A).encode()


# Each case with decision 6 step 2's reason token. Every case fails exactly one
# of the step's checks: the key cases carry the right subject, and the subject
# cases the right key.
MALFORMED_CASES = [
    pytest.param(_not_json, "codec", id="not-json"),
    pytest.param(_request_observation, "payload_type", id="payload-request-observation"),
    pytest.param(_prefix_stats_changed, "payload_type", id="payload-prefix-stats-changed"),
    pytest.param(_key_of_another_ip, "key_mismatch", id="key-names-another-ip"),
    pytest.param(_no_key, "key_mismatch", id="key-is-none"),
    pytest.param(_subject_of_another_ip, "subject_mismatch", id="subject-names-another-ip"),
    pytest.param(_no_subject, "subject_mismatch", id="subject-is-none"),
]

# Rejected by the codec's attribute validator on decode: a key rule R1 refuses.
DECODE_REJECTED = {"attributes_version": 1, "wieght": 5}


class TestMalformed:
    """Decision 6 step 2: MALFORMED is counted, logged, and handled -- the
    position moves -- but nothing is applied and `as_of` does not move."""

    async def _worker_with_one_applied_event(self) -> TrieWorker:
        worker = _worker()
        outcome = await worker.handle(_msg(_envelope(IP_C, 0), 0))
        assert outcome is HotIpOutcome.APPLIED
        return worker

    @staticmethod
    def _assert_logged_once(caplog: pytest.LogCaptureFixture, reason: str) -> None:
        # Decision 12: `malformed_hot_ip_event`, warning, with `topic`,
        # `partition`, `offset` and `reason` (a decision 6 token).
        records = _logged(caplog, "malformed_hot_ip_event")
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert _field(records[0], "reason") == reason
        assert _field(records[0], "offset") == "1"
        assert _field(records[0], "partition") == "0"
        assert _field(records[0], "topic") == TOPIC

    @pytest.mark.parametrize(("build", "reason"), MALFORMED_CASES)
    async def test_it_is_skipped_counted_and_logged_with_its_reason(
        self,
        build: Callable[[], tuple[bytes, bytes | None]],
        reason: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        worker = await self._worker_with_one_applied_event()
        before = _view(worker)
        as_of = worker.state.as_of
        updates = _updates_total(worker)
        value, key = build()

        outcome = await worker.handle(_message(value, 1, key))

        assert outcome is HotIpOutcome.MALFORMED
        assert _skipped(worker, "malformed") == 1
        assert _skipped(worker, "family_not_served") == 0
        assert _view(worker) == before
        assert worker.state.position == 1
        assert worker.state.event_sequence == 2
        assert worker.state.as_of == as_of
        assert _updates_total(worker) == updates
        assert _rejected(worker, "decode") == 0
        self._assert_logged_once(caplog, reason)

    async def test_attributes_rejected_at_decode(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        worker = await self._worker_with_one_applied_event()
        before = _view(worker)
        as_of = worker.state.as_of
        value = _with_attributes(_envelope(IP_A, 1, attributes=DEFAULT_DOCUMENT), DECODE_REJECTED)

        outcome = await worker.handle(_message(value, 1, str(IP_A).encode()))

        assert outcome is HotIpOutcome.MALFORMED
        assert _skipped(worker, "malformed") == 1
        assert _rejected(worker, "decode") == 1
        assert _rejected(worker, "apply") == 0
        assert _view(worker) == before
        assert IP_A not in worker.state.of(IPV4).records
        assert worker.state.position == 1
        assert worker.state.as_of == as_of
        self._assert_logged_once(caplog, "invalid_attributes")


class TestFamilyNotServed:
    async def test_an_ipv6_event_on_an_ipv4_only_trie_is_skipped(self) -> None:
        worker = _worker(families=ONLY_V4)
        before = _view(worker)

        outcome = await worker.handle(_msg(_envelope(IP_V6, 0), 0))

        assert outcome is HotIpOutcome.FAMILY_NOT_SERVED
        assert _skipped(worker, "family_not_served") == 1
        assert _skipped(worker, "malformed") == 0
        assert _updates_total(worker) == 0
        assert _view(worker) == before
        assert worker.state.position == 0
        assert worker.state.event_sequence == 1
        assert worker.state.as_of is None

    async def test_the_same_event_is_applied_when_both_families_are_served(self) -> None:
        worker = _worker(families=BOTH)
        before_v4 = _view(worker, IPV4)

        outcome = await worker.handle(_msg(_envelope(IP_V6, 0), 0))

        v6 = worker.state.of(IPV6)
        assert outcome is HotIpOutcome.APPLIED
        assert v6.trie.hot_ip_count == 1
        assert dict(v6.records[IP_V6].attributes) == DEFAULT_DOCUMENT
        assert _view(worker, IPV4) == before_v4
        assert _updates(worker, "ipv6", "HotIpAdded", "applied") == 1
        assert _updates(worker, "ipv4", "HotIpAdded", "applied") == 0
        assert _skipped(worker, "family_not_served") == 0

    async def test_every_such_event_is_logged_and_a_repeat_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Decision 12: `family_not_served`, with `family`, `topic`, `partition`
        # and `offset`; "debug after" the first time per family per process.
        # Whether this is that first time depends on the tests run before it
        # in this process, so the WARNING is checked in a fresh process below.
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        worker = _worker(families=ONLY_V4)

        for offset in range(2):
            outcome = await worker.handle(_msg(_envelope(IP_V6, offset), offset))
            assert outcome is HotIpOutcome.FAMILY_NOT_SERVED

        records = _logged(caplog, "family_not_served")
        assert [_field(record, "offset") for record in records] == ["0", "1"]
        assert records[1].levelno == logging.DEBUG
        for record in records:
            assert _field(record, "family") == "ipv6"
            assert _field(record, "partition") == "0"
            assert _field(record, "topic") == TOPIC

    def test_the_first_per_family_in_a_process_is_a_warning(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", _FAMILY_LOG_SCRIPT, __file__],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["outcomes"] == ["family_not_served"] * 5
        # An IPv4-only worker meets IPv6 three times, then an IPv6-only worker
        # meets IPv4 twice: the first of each family is a warning.
        assert report["levels"] == ["WARNING", "DEBUG", "DEBUG", "WARNING", "DEBUG"]


# Run by `test_the_first_per_family_in_a_process_is_a_warning` in a fresh
# interpreter; `sys.argv[1]` is this file, imported by path for its helpers.
_FAMILY_LOG_SCRIPT = """
import asyncio
import importlib.util
import json
import logging
import sys

spec = importlib.util.spec_from_file_location("trie_worker_tests", sys.argv[1])
tests = importlib.util.module_from_spec(spec)
sys.modules["trie_worker_tests"] = tests
spec.loader.exec_module(tests)


class Keep(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.levels = []

    def emit(self, record):
        words = record.getMessage().split()
        if words and words[0] == "family_not_served":
            self.levels.append(record.levelname)


async def main():
    keep = Keep()
    logger = logging.getLogger("hammertime.trie.worker")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(keep)
    outcomes = []
    v4_only = tests._worker(families=frozenset({tests.IPV4}))
    for offset in range(3):
        message = tests._msg(tests._envelope(tests.IP_V6, offset), offset)
        outcomes.append(str((await v4_only.handle(message)).value))
    v6_only = tests._worker(families=frozenset({tests.IPV6}))
    for offset in range(2):
        message = tests._msg(tests._envelope(tests.IP_A, offset), offset)
        outcomes.append(str((await v6_only.handle(message)).value))
    print(json.dumps({"levels": keep.levels, "outcomes": outcomes}))


asyncio.run(main())
"""


class TestRedelivered:
    async def _assert_redelivered(self, worker: TrieWorker, message: ConsumedMessage) -> None:
        before = (
            _view(worker),
            worker.state.position,
            worker.state.event_sequence,
            worker.state.as_of,
            _updates_total(worker),
            _skipped(worker, "malformed"),
            _skipped(worker, "family_not_served"),
        )

        outcome = await worker.handle(message)

        assert outcome is HotIpOutcome.REDELIVERED
        after = (
            _view(worker),
            worker.state.position,
            worker.state.event_sequence,
            worker.state.as_of,
            _updates_total(worker),
            _skipped(worker, "malformed"),
            _skipped(worker, "family_not_served"),
        )
        assert after == before

    async def test_the_same_message_twice(self) -> None:
        worker = _worker()
        message = _msg(_envelope(IP_A, 0, attributes={"attributes_version": 1, "weight": 3}), 0)
        assert await worker.handle(message) is HotIpOutcome.APPLIED

        await self._assert_redelivered(worker, message)

    async def test_a_lower_offset(self) -> None:
        worker = _worker()
        assert await worker.handle(_msg(_envelope(IP_A, 0), 0)) is HotIpOutcome.APPLIED
        assert await worker.handle(_msg(_envelope(IP_B, 1), 1)) is HotIpOutcome.APPLIED

        # A different record at an offset already passed is not applied.
        await self._assert_redelivered(worker, _msg(_envelope(IP_C, 2), 0))

        assert IP_C not in worker.state.of(IPV4).records

    async def test_it_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        # Decision 6 step 1: "The record `redelivered_hot_ip_event` is logged";
        # decision 12: warning, with `topic`, `partition`, `offset`, `position`.
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        worker = _worker()
        assert await worker.handle(_msg(_envelope(IP_A, 0), 0)) is HotIpOutcome.APPLIED
        assert await worker.handle(_msg(_envelope(IP_B, 1), 1)) is HotIpOutcome.APPLIED
        assert _logged(caplog, "redelivered_hot_ip_event") == []

        outcome = await worker.handle(_msg(_envelope(IP_C, 2), 0))

        assert outcome is HotIpOutcome.REDELIVERED
        records = _logged(caplog, "redelivered_hot_ip_event")
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert _field(records[0], "offset") == "0"
        assert _field(records[0], "position") == "1"
        assert _field(records[0], "partition") == "0"
        assert _field(records[0], "topic") == TOPIC


class TestAByteIdenticalRecordAtANewOffset:
    async def test_handled_directly_it_is_unchanged_and_moves_the_position(self) -> None:
        worker = _worker()
        envelope = _envelope(IP_A, 0, attributes={"attributes_version": 1, "weight": 4})

        first = await worker.handle(_msg(envelope, 0))
        second = await worker.handle(_msg(envelope, 1))

        assert first is HotIpOutcome.APPLIED
        assert second is HotIpOutcome.UNCHANGED
        assert worker.state.event_sequence == 2
        assert worker.state.of(IPV4).trie.hot_ip_count == 1

    async def test_replayed_from_the_log_it_is_unchanged(self) -> None:
        # `MemoryProducer` deduplicates by `message_id`; `message_id=None`
        # appends the same bytes a second time.
        bus = InMemoryBus()
        envelope = _envelope(IP_A, 0, attributes={"attributes_version": 1, "weight": 4})
        await _publish(bus, envelope)
        await _publish(bus, envelope, dedupe=False)
        worker = _worker(bus=bus)

        await worker.start()
        try:
            assert worker.replay_target == 2
            assert worker.state.event_sequence == 2
            assert worker.state.of(IPV4).trie.hot_ip_count == 1
            assert _updates(worker, "ipv4", "HotIpAdded", "applied") == 1
            assert _updates(worker, "ipv4", "HotIpAdded", "unchanged") == 1
        finally:
            await worker.stop()


class TestStopped:
    async def test_handle_after_stop_does_nothing(self) -> None:
        worker = _worker()
        assert await worker.handle(_msg(_envelope(IP_A, 0), 0)) is HotIpOutcome.APPLIED
        before = (
            _view(worker),
            worker.state.position,
            worker.state.as_of,
            _updates_total(worker),
        )

        await worker.stop()
        outcome = await worker.handle(_msg(_envelope(IP_B, 1), 1))

        assert outcome is HotIpOutcome.STOPPED
        assert (
            _view(worker),
            worker.state.position,
            worker.state.as_of,
            _updates_total(worker),
        ) == before

    async def test_stop_is_idempotent_and_safe_before_start(self) -> None:
        worker = _worker()

        await worker.stop()
        await worker.stop()

        assert await worker.handle(_msg(_envelope(IP_A, 0), 0)) is HotIpOutcome.STOPPED
        assert worker.state.position is None


class TestInvariantViolation:
    """Decision 7: logged and propagated; "The trie and the records are unchanged
    ... and `position` has not moved"."""

    async def test_handle_raises_and_moves_nothing(self) -> None:
        worker = _worker()
        _corrupt_single_leaf(worker, IP_A)
        records = worker.state.of(IPV4).records

        with pytest.raises(InvariantViolation):
            await worker.handle(_msg(_envelope(IP_A, 0), 0))

        assert worker.state.position is None
        assert worker.state.event_sequence == 0
        assert worker.state.as_of is None
        assert len(records) == 0
        assert _updates_total(worker) == 0

    async def test_start_over_such_a_record_raises(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, _envelope(IP_A, 0))
        worker = _worker(bus=bus)
        _corrupt_single_leaf(worker, IP_A)

        try:
            with pytest.raises(InvariantViolation):
                await worker.start()
            assert worker.state.position is None
            assert len(worker.state.of(IPV4).records) == 0
        finally:
            await worker.stop()


class TestApplyTimeAttributesRejection:
    """Decision 7: "Apply again with `attributes=None`, count
    `attributes_rejected{stage="apply"}`, log it." Reached through the module
    global the Test seams paragraph names."""

    @staticmethod
    def _rejecting(
        seen: list[tuple[object, dict[str, Any]]],
    ) -> Callable[..., bool]:
        """A stand-in for `apply_hot_ip_added` that records each call's
        document and keyword arguments, rejects every document that is not
        `None`, and passes the rest to the real function."""

        def rejecting(
            trie: Any,
            records: Any,
            address: Address,
            attributes: Any = None,
            *args: Any,
            **kwargs: Any,
        ) -> bool:
            seen.append((attributes, dict(kwargs)))
            if attributes is not None:
                raise InvalidAttributesError("rejected by the test's stand-in")
            return bool(apply_hot_ip_added(trie, records, address, attributes, *args, **kwargs))

        return rejecting

    async def test_the_default_document_is_stored_and_the_event_is_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[object, dict[str, Any]]] = []
        monkeypatch.setattr(trie_worker, "apply_hot_ip_added", self._rejecting(seen))
        worker = _worker()
        document = {"attributes_version": 1, "weight": 5}

        outcome = await worker.handle(_msg(_envelope(IP_A, 0, attributes=document), 0))

        fs = worker.state.of(IPV4)
        assert outcome is HotIpOutcome.APPLIED
        assert dict(fs.records[IP_A].attributes) == DEFAULT_DOCUMENT
        assert fs.trie.hot_ip_count == 1
        assert _rejected(worker, "apply") == 1
        assert _rejected(worker, "decode") == 0
        assert _updates(worker, "ipv4", "HotIpAdded", "applied") == 1
        assert _updates_total(worker) == 1
        assert worker.state.event_sequence == 1
        # The retry passed no document.
        assert seen[-1][0] is None
        assert len(seen) == 2

    async def test_both_calls_pass_the_window_count_and_the_retry_stores_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0015 Amendment 5 rulings 4 and 7 / ADR-0017 decisions 6 and 17
        as noted: both calls pass `request_count=payload.window_count` as a
        keyword, so the default document is stored with the event's count --
        the document was rejected, not the count."""

        seen: list[tuple[object, dict[str, Any]]] = []
        monkeypatch.setattr(trie_worker, "apply_hot_ip_added", self._rejecting(seen))
        worker = _worker()
        document = {"attributes_version": 1, "weight": 5}

        outcome = await worker.handle(
            _msg(_envelope(IP_A, 0, attributes=document, window_count=1437), 0)
        )

        assert outcome is HotIpOutcome.APPLIED
        assert len(seen) == 2
        assert seen[0] == (document, {"request_count": 1437})
        assert seen[1] == (None, {"request_count": 1437})
        assert _stored(worker, IP_A) == (DEFAULT_DOCUMENT, 1437)
        assert _rejected(worker, "apply") == 1

    async def test_the_retry_over_a_hot_ip_replaces_its_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ruling 4: the recovery replaces the whole earlier record, count
        included, with the default document and the event's count."""

        worker = _worker()
        earlier = {"attributes_version": 1, "weight": 9}
        first = _msg(_envelope(IP_A, 0, attributes=earlier, window_count=1200), 0)
        assert await worker.handle(first) is HotIpOutcome.APPLIED
        seen: list[tuple[object, dict[str, Any]]] = []
        monkeypatch.setattr(trie_worker, "apply_hot_ip_added", self._rejecting(seen))

        second = _envelope(IP_A, 1, attributes={"attributes_version": 1}, window_count=1501)
        outcome = await worker.handle(_msg(second, 1))

        assert outcome is HotIpOutcome.UNCHANGED
        assert [kwargs for _document, kwargs in seen] == [{"request_count": 1501}] * 2
        assert _stored(worker, IP_A) == (DEFAULT_DOCUMENT, 1501)


_COUNT_ERRORS = [
    pytest.param(ValueError("request_count must be at least 0"), id="ValueError"),
    pytest.param(TypeError("request_count must be an int"), id="TypeError"),
]


class TestApplyTimeRequestCountError:
    """ADR-0015 Amendment 5 ruling 3: "A count error therefore means a bug,
    like decision 6's family `ValueError`, and the worker lets it propagate
    (ADR-0017 decision 7). It is not an attributes rejection: it never takes
    decision 6's `attributes=None` retry, and it is never counted in
    `attributes_rejected`." Assumption 75: "The count comes before the
    document so that a bug is never reported as a rejected document."

    ADR-0017 decision 7, as noted for Amendment 5: `apply_hot_ip_added` raises
    "`TypeError` for a `request_count` that is not an exact `int`, and
    `ValueError` for a negative one, before the document and the trie. ...
    Either is a bug and propagates, as the `apply_*` `ValueError` row says.
    Neither is an attributes rejection, so neither takes the `attributes=None`
    retry or counts in `attributes_rejected`." Decision 8: `position` moves
    only for a handled record, and a propagated exception handles nothing.

    The codec never delivers a bad `window_count`, so the error is raised by a
    stand-in reached through the module global the Test seams paragraph
    names, as `TestApplyTimeAttributesRejection` does."""

    @staticmethod
    def _raising(
        error: Exception, seen: list[tuple[object, dict[str, Any]]]
    ) -> Callable[..., bool]:
        """A stand-in for `apply_hot_ip_added` that records each call's
        document and keyword arguments, then raises `error` -- on every call,
        so a retry would show up as a second entry in `seen`. It raises
        before touching anything, as ruling 3's order requires of the real
        function."""

        def raising(
            trie: Any,
            records: Any,
            address: Address,
            attributes: Any = None,
            *args: Any,
            **kwargs: Any,
        ) -> bool:
            seen.append((attributes, dict(kwargs)))
            raise error

        return raising

    @pytest.mark.parametrize("error", _COUNT_ERRORS)
    async def test_on_an_empty_trie_it_propagates_and_changes_nothing(
        self, error: Exception, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[object, dict[str, Any]]] = []
        monkeypatch.setattr(trie_worker, "apply_hot_ip_added", self._raising(error, seen))
        worker = _worker()
        before = _view(worker)
        # A document that would be retried if the error were taken for an
        # attributes rejection.
        document = {"attributes_version": 1, "weight": 5}

        with pytest.raises(type(error), match="request_count") as excinfo:
            await worker.handle(_msg(_envelope(IP_A, 0, attributes=document, window_count=1437), 0))

        # The stand-in's own exception, not a wrapper or a later one.
        assert excinfo.value is error
        # Exactly one call: no `attributes=None` retry.
        assert seen == [(document, {"request_count": 1437})]
        assert _rejected(worker, "apply") == 0
        assert _rejected(worker, "decode") == 0
        assert _updates_total(worker) == 0
        assert worker.state.position is None
        assert worker.state.event_sequence == 0
        assert worker.state.as_of is None
        assert _view(worker) == before
        assert len(worker.state.of(IPV4).records) == 0
        assert worker.state.of(IPV4).trie.hot_ip_count == 0

    @pytest.mark.parametrize("error", _COUNT_ERRORS)
    async def test_over_a_hot_ip_it_propagates_and_leaves_the_earlier_record(
        self, error: Exception, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = _worker()
        earlier = {"attributes_version": 1, "weight": 9}
        first = _msg(_envelope(IP_A, 0, attributes=earlier, window_count=1200), 0)
        assert await worker.handle(first) is HotIpOutcome.APPLIED
        before = (
            _view(worker),
            worker.state.position,
            worker.state.event_sequence,
            worker.state.as_of,
            _updates_total(worker),
        )
        seen: list[tuple[object, dict[str, Any]]] = []
        monkeypatch.setattr(trie_worker, "apply_hot_ip_added", self._raising(error, seen))
        document = {"attributes_version": 1, "weight": 5}

        with pytest.raises(type(error), match="request_count") as excinfo:
            await worker.handle(_msg(_envelope(IP_A, 1, attributes=document, window_count=1501), 1))

        assert excinfo.value is error
        assert seen == [(document, {"request_count": 1501})]
        assert _rejected(worker, "apply") == 0
        assert _rejected(worker, "decode") == 0
        # Only the first event's update was counted.
        assert _updates_total(worker) == 1
        assert (
            _view(worker),
            worker.state.position,
            worker.state.event_sequence,
            worker.state.as_of,
            _updates_total(worker),
        ) == before
        assert worker.state.position == 0
        assert worker.state.event_sequence == 1
        assert _stored(worker, IP_A) == (earlier, 1200)


def _key_mismatched_add(ip: Address, n: int, window_count: int) -> tuple[bytes, bytes | None]:
    """A `HotIpAdded` for `ip` keyed by another IP: MALFORMED [key_mismatch]."""

    other = IP_B if ip != IP_B else IP_C
    return encode(_envelope(ip, n, window_count=window_count)), str(other).encode()


def _decode_rejected_add(ip: Address, n: int, window_count: int) -> tuple[bytes, bytes | None]:
    """A `HotIpAdded` for `ip` whose attributes the codec rejects at decode."""

    envelope = _envelope(ip, n, attributes=DEFAULT_DOCUMENT, window_count=window_count)
    return _with_attributes(envelope, DECODE_REJECTED), str(ip).encode()


class TestRequestCount:
    """ADR-0015 Amendment 5 rulings 1, 4 and 7, with ADR-0017 decisions 6, 7
    and 17 as noted: the record's `request_count` is the `window_count` of the
    most recent `HotIpAdded` the trie has applied for the IP; every applied
    add replaces it, whatever `add_hot_ip` returned; `HotIpRemoved` deletes
    it with the record; a skipped event sets nothing."""

    async def test_an_applied_add_stores_the_window_count(self) -> None:
        worker = _worker()
        document = {"attributes_version": 1, "weight": 10}

        outcome = await worker.handle(
            _msg(_envelope(IP_A, 0, attributes=document, window_count=1437), 0)
        )

        assert outcome is HotIpOutcome.APPLIED
        assert _stored(worker, IP_A) == (document, 1437)
        assert type(worker.state.of(IPV4).records[IP_A].request_count) is int

    async def test_an_unchanged_add_replaces_the_count(self) -> None:
        worker = _worker()
        first = _msg(_envelope(IP_A, 0, window_count=1200), 0)
        assert await worker.handle(first) is HotIpOutcome.APPLIED
        bytes_before = worker.state.of(IPV4).records.serialized_bytes

        outcome = await worker.handle(_msg(_envelope(IP_A, 1, window_count=1033), 1))

        assert outcome is HotIpOutcome.UNCHANGED
        assert _stored(worker, IP_A) == (DEFAULT_DOCUMENT, 1033)
        # Ruling 5: a write that changes only the count leaves the byte total.
        assert worker.state.of(IPV4).records.serialized_bytes == bytes_before

    async def test_a_redelivered_add_writes_its_own_count_again(self) -> None:
        # Ruling 4: "A redelivered `HotIpAdded` writes the same values again."
        worker = _worker()
        envelope = _envelope(IP_A, 0, window_count=1300)
        assert await worker.handle(_msg(envelope, 0)) is HotIpOutcome.APPLIED
        before = _view(worker)

        assert await worker.handle(_msg(envelope, 1)) is HotIpOutcome.UNCHANGED

        assert _view(worker) == before
        assert _stored(worker, IP_A) == (DEFAULT_DOCUMENT, 1300)

    async def test_a_removal_deletes_the_record_and_its_count(self) -> None:
        worker = _worker()
        add_a = _msg(_envelope(IP_A, 0, window_count=1200), 0)
        add_b = _msg(_envelope(IP_B, 1, window_count=1250), 1)
        assert await worker.handle(add_a) is HotIpOutcome.APPLIED
        assert await worker.handle(add_b) is HotIpOutcome.APPLIED

        outcome = await worker.handle(_msg(_envelope(IP_A, 2, removed=True, window_count=790), 2))

        records = worker.state.of(IPV4).records
        assert outcome is HotIpOutcome.APPLIED
        assert IP_A not in records
        assert records.get(IP_A) is None
        # The removal's own `window_count` is decoded and not used (ruling 7).
        assert _stored(worker, IP_B) == (DEFAULT_DOCUMENT, 1250)

        # A later add sets its own count; the old one is gone with the record.
        add_a_again = _msg(_envelope(IP_A, 3, window_count=1100), 3)
        assert await worker.handle(add_a_again) is HotIpOutcome.APPLIED
        assert _stored(worker, IP_A) == (DEFAULT_DOCUMENT, 1100)

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(_key_mismatched_add, id="key-mismatch"),
            pytest.param(_decode_rejected_add, id="attributes-rejected-at-decode"),
        ],
    )
    async def test_a_malformed_add_for_a_hot_ip_leaves_the_earlier_count(
        self, build: Callable[[Address, int, int], tuple[bytes, bytes | None]]
    ) -> None:
        # Ruling 1: "A `HotIpAdded` the worker skips sets nothing."
        worker = _worker()
        earlier = {"attributes_version": 1, "weight": 9}
        first = _msg(_envelope(IP_A, 0, attributes=earlier, window_count=1200), 0)
        assert await worker.handle(first) is HotIpOutcome.APPLIED
        before = _view(worker)
        value, key = build(IP_A, 1, 1600)

        outcome = await worker.handle(_message(value, 1, key))

        assert outcome is HotIpOutcome.MALFORMED
        assert _view(worker) == before
        assert _stored(worker, IP_A) == (earlier, 1200)

    async def test_a_replay_gives_each_ip_the_count_of_its_latest_applied_add(self) -> None:
        # Ruling 4: "After a replay, each record is the most recent applied
        # `HotIpAdded`'s, whatever was stored before."
        n = 6
        bus = InMemoryBus()
        for i in range(n):
            await _publish(bus, _envelope(_ip(i + 1), i, window_count=1000 + 17 * i))
        await _publish(bus, _envelope(_ip(1), n, window_count=4321))
        await _publish(bus, _envelope(_ip(2), n + 1, removed=True, window_count=800))
        worker = _worker(bus=bus)

        await worker.start()
        try:
            fs = worker.state.of(IPV4)
            assert worker.caught_up is True
            expected = {_ip(i + 1): 1000 + 17 * i for i in range(2, n)}
            expected[_ip(1)] = 4321
            assert {ip: record.request_count for ip, record in fs.records.items()} == expected
            for ip in expected:
                assert dict(fs.records[ip].attributes) == DEFAULT_DOCUMENT
            assert fs.trie.hot_ip_count == len(expected)
            check_attribute_records(fs.trie, fs.records)
        finally:
            await worker.stop()


MARKER = "S3CR3T-MARKER"


class TestLogRecordsCarryNoDocumentContent:
    """Decision 12: records "carry fixed tokens and numbers only: never a payload
    value, never an attribute document, and never an exception's text"."""

    async def test_a_decode_rejected_document_is_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="hammertime.trie.worker")
        worker = _worker()
        hostile = {"attributes_version": 1, "bogus": 1, "x_marker": MARKER}
        value = _with_attributes(_envelope(IP_A, 0, attributes=DEFAULT_DOCUMENT), hostile)
        assert MARKER.encode() in value

        outcome = await worker.handle(_message(value, 0, str(IP_A).encode()))

        assert outcome is HotIpOutcome.MALFORMED
        ours = [record for record in caplog.records if record.name == "hammertime.trie.worker"]
        # The rejection is logged (`malformed_hot_ip_event`), so the check
        # below is not vacuous.
        assert any(record.getMessage().startswith("malformed_hot_ip_event") for record in ours)
        for record in caplog.records:
            assert MARKER not in record.getMessage()
            assert MARKER not in repr(vars(record))
        assert MARKER not in caplog.text


class TestReadersSeeWholeEvents:
    """Decision 9 R1 / section 28: no `await` separates an event's first write
    from its last, so a reader resumed at any point sees whole events."""

    async def test_every_resume_sees_a_consistent_state(self) -> None:
        k = 200
        burst = 50
        bus = InMemoryBus()
        worker = _worker(bus=bus)
        await worker.start()
        assert worker.state.event_sequence == 0
        state = worker.state
        fs = state.of(IPV4)
        # Every distinct `event_sequence` the sampler resumed at.
        seen: set[int] = set()

        async def sampler() -> None:
            for _ in range(100_000):
                assert len(fs.records) == fs.trie.hot_ip_count == state.event_sequence
                seen.add(state.event_sequence)
                if state.event_sequence == k:
                    return
                await asyncio.sleep(0)
            raise AssertionError("the burst was never consumed")

        run_task = asyncio.create_task(worker.run())
        sampler_task = asyncio.create_task(sampler())

        async def sampled(target: int) -> None:
            # The worker idles at `target` until the next burst is published,
            # so the sampler resumes there; stop early if the sampler failed.
            await _yield_until(lambda: target in seen or sampler_task.done())

        for first in range(0, k, burst):
            for i in range(first, first + burst):
                await _publish(bus, _envelope(_ip(i + 1), i))
            if first + burst < k:
                await sampled(first + burst)

        await asyncio.wait_for(sampler_task, timeout=10.0)
        await _stop_and_join(worker, run_task)

        # Non-vacuous: the check above ran at states strictly between the
        # first event and the last, not only at 0 and `k`.
        between = {v for v in seen if 0 < v < k}
        assert between, seen
        assert {50, 100, 150} <= between
        assert state.event_sequence == k
        assert len(fs.records) == fs.trie.hot_ip_count == k


class TestApplyConfig:
    async def test_it_adopts_until_stop(self) -> None:
        worker = _worker(config=_config(1))
        cfg2 = _config(2)
        cfg3 = _config(3)

        await worker.apply_config(cfg2)
        assert worker.state.config is cfg2

        await worker.stop()
        await worker.apply_config(cfg3)
        assert worker.state.config is cfg2

    async def test_it_compares_no_versions(self) -> None:
        # Decision 10: "It compares no versions: the poller alone gates them."
        worker = _worker(config=_config(5))
        older = _config(2)

        await worker.apply_config(older)

        assert worker.state.config is older


class TestMetricsMatchTheOutcomes:
    async def test_after_a_mixed_replay(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, _envelope(IP_A, 0))  # 0 applied add
        await _publish(bus, _envelope(IP_B, 1))  # 1 applied add
        await _publish(bus, _envelope(IP_C, 2))  # 2 applied add
        await _publish(bus, _envelope(IP_B, 3, removed=True))  # 3 applied remove
        await _publish(bus, _envelope(IP_A, 4))  # 4 unchanged add
        await bus.producer().publish(TOPIC, key="junk", value=b"not json", message_id=None)  # 5
        await _publish(bus, _envelope(IP_D, 6, removed=True))  # 6 unchanged remove
        await _publish(bus, _envelope(IP_V6, 7))  # 7 family not served
        worker = _worker(bus=bus)

        await worker.start()
        try:
            state = worker.state
            fs = state.of(IPV4)
            metrics = worker.metrics
            assert worker.replay_target == 8
            assert state.event_sequence == 8
            assert metrics.get("event_sequence") == state.event_sequence
            assert metrics.get("hot_ip_count", family="ipv4") == fs.trie.hot_ip_count == 2
            assert metrics.get("ip_attribute_records", family="ipv4") == 2
            assert metrics.get("trie_nodes", family="ipv4") == fs.trie.node_count
            assert metrics.get("ip_attribute_bytes", family="ipv4") == fs.records.serialized_bytes
            assert _updates(worker, "ipv4", "HotIpAdded", "applied") == 3
            assert _updates(worker, "ipv4", "HotIpAdded", "unchanged") == 1
            assert _updates(worker, "ipv4", "HotIpRemoved", "applied") == 1
            assert _updates(worker, "ipv4", "HotIpRemoved", "unchanged") == 1
            assert _skipped(worker, "malformed") == 1
            assert _skipped(worker, "family_not_served") == 1
            assert metrics.get("hot_ip_count", family="ipv6") == 0
        finally:
            await worker.stop()
