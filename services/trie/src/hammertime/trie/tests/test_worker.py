"""The trie worker: positional replay, one outcome per message, and the log position.

Spec: section 19 (the trie consumes `HotIpAdded`/`HotIpRemoved`), section 22
(`event_sequence` and `as_of`), section 28 (a reader never sees a partially
applied event), section 33 (replay), section 35 (one root per family), section
37 and section 46.8 (the counters), section 46.5 (the trie and the attribute
record move in one step).

Written from ADR-0017 decisions 3-10 and 12 and its "Test seams", with the
documents it cites (ADR-0013 decision 9: the whole-topic positional
subscription and `end_offset`; ADR-0014 A12: `InvariantViolation`; ADR-0015
decision 6: `apply_hot_ip_added` / `apply_hot_ip_removed`). What is pinned:

* `start()` reads `end_offset` before subscribing, keeps it as
  `replay_target`, and handles messages until caught up (`end <= S`, or
  `event_sequence >= end`); a second `start()` returns at once (decision 4);
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
  `hammertime.trie.worker` (Test seams);
* log records carry no document content (decision 12) -- the one log
  assertion in this file, `TestLogRecordsCarryNoDocumentContent`;
* R1 of decision 9: a concurrent reader sees `len(records) ==
  hot_ip_count == event_sequence` at every resume while a burst of distinct
  adds is consumed from offset 0 (`TestReadersSeeWholeEvents`).

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
  10.0)` appears only as a failure bound on `run()` returning.
* The hot-ip envelope is shaped as ADR-0011 decision 4 has the aggregator
  shape it: `agent_id="aggregator-shard-0"`, `subject` and key the IP's
  canonical text, `window_count` present (the codec requires it).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import hammertime.trie.worker as trie_worker
import pytest
from hammertime.bus.interface import ConsumedMessage
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
) -> EventEnvelope[Any]:
    """One hot-ip event as the aggregator emits it (ADR-0011 decision 4).

    `n` makes the envelope's identity -- and so its `event_id` -- distinct.
    """

    ts = timestamp if timestamp is not None else T0 + timedelta(seconds=n)
    payload: HotIpAdded | HotIpRemoved
    if removed:
        payload = HotIpRemoved(
            ip=ip,
            timestamp=ts,
            sequence=n + 1,
            window_count=750,
            config_version=1,
            attributes=attributes,
        )
    else:
        payload = HotIpAdded(
            ip=ip,
            timestamp=ts,
            sequence=n + 1,
            window_count=1200,
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
    """Everything a family's pair holds, deep enough to compare before and after."""

    fs = worker.state.of(family)
    documents = {address: dict(fs.records[address]) for address in fs.records}
    return (fs.trie.hot_ip_count, fs.trie.node_count, documents, fs.records.serialized_bytes)


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
                assert dict(fs.records[ip]) == document
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

        assert dict(fs.records[IP_A]) == {"attributes_version": 1, "weight": 9}
        assert worker.state.event_sequence == 1
        await _stop_and_join(worker, task)
        assert task.exception() is None

    async def test_run_before_start_is_a_runtime_error(self) -> None:
        worker = _worker()

        with pytest.raises(RuntimeError):
            await worker.run()


class TestAddsAndRemoves:
    async def test_an_add_for_a_new_address_is_applied(self) -> None:
        worker = _worker()
        document = {"attributes_version": 1, "weight": 10}

        outcome = await worker.handle(_msg(_envelope(IP_A, 0, attributes=document), 0))

        fs = worker.state.of(IPV4)
        assert outcome is HotIpOutcome.APPLIED
        assert fs.trie.hot_ip_count == 1
        assert dict(fs.records[IP_A]) == document
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
        assert dict(fs.records[IP_A]) == second
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
        assert dict(worker.state.of(IPV4).records[IP_A]) == DEFAULT_DOCUMENT

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


MALFORMED_CASES = [
    pytest.param(_not_json, id="not-json"),
    pytest.param(_request_observation, id="payload-request-observation"),
    pytest.param(_prefix_stats_changed, id="payload-prefix-stats-changed"),
    pytest.param(_key_of_another_ip, id="key-names-another-ip"),
    pytest.param(_no_key, id="key-is-none"),
    pytest.param(_subject_of_another_ip, id="subject-names-another-ip"),
    pytest.param(_no_subject, id="subject-is-none"),
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

    @pytest.mark.parametrize("build", MALFORMED_CASES)
    async def test_it_is_skipped_and_counted(
        self, build: Callable[[], tuple[bytes, bytes | None]]
    ) -> None:
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

    async def test_attributes_rejected_at_decode(self) -> None:
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
        assert dict(v6.records[IP_V6]) == DEFAULT_DOCUMENT
        assert _view(worker, IPV4) == before_v4
        assert _updates(worker, "ipv6", "HotIpAdded", "applied") == 1
        assert _updates(worker, "ipv4", "HotIpAdded", "applied") == 0
        assert _skipped(worker, "family_not_served") == 0


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

    async def test_the_default_document_is_stored_and_the_event_is_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []

        def rejecting(
            trie: Any,
            records: Any,
            address: Address,
            attributes: Any = None,
            *args: Any,
            **kwargs: Any,
        ) -> bool:
            seen.append(attributes)
            if attributes is not None:
                raise InvalidAttributesError("rejected by the test's stand-in")
            return bool(apply_hot_ip_added(trie, records, address, attributes, *args, **kwargs))

        monkeypatch.setattr(trie_worker, "apply_hot_ip_added", rejecting)
        worker = _worker()
        document = {"attributes_version": 1, "weight": 5}

        outcome = await worker.handle(_msg(_envelope(IP_A, 0, attributes=document), 0))

        fs = worker.state.of(IPV4)
        assert outcome is HotIpOutcome.APPLIED
        assert dict(fs.records[IP_A]) == DEFAULT_DOCUMENT
        assert fs.trie.hot_ip_count == 1
        assert _rejected(worker, "apply") == 1
        assert _rejected(worker, "decode") == 0
        assert _updates(worker, "ipv4", "HotIpAdded", "applied") == 1
        assert _updates_total(worker) == 1
        assert worker.state.event_sequence == 1
        # The retry passed no document.
        assert seen[-1] is None
        assert len(seen) == 2


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
        bus = InMemoryBus()
        worker = _worker(bus=bus)
        await worker.start()
        assert worker.state.event_sequence == 0
        state = worker.state
        fs = state.of(IPV4)
        samples = 0

        async def sampler() -> None:
            nonlocal samples
            for _ in range(100_000):
                samples += 1
                assert len(fs.records) == fs.trie.hot_ip_count == state.event_sequence
                if state.event_sequence == k:
                    return
                await asyncio.sleep(0)
            raise AssertionError("the burst was never consumed")

        run_task = asyncio.create_task(worker.run())
        sampler_task = asyncio.create_task(sampler())
        for i in range(k):
            await _publish(bus, _envelope(_ip(i + 1), i))

        await asyncio.wait_for(sampler_task, timeout=10.0)
        await _stop_and_join(worker, run_task)

        assert samples >= 1
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
