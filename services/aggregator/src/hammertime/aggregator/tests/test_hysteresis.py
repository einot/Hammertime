"""The COLD/HOT table from spec section 6, case by case.

Spec: section 5, section 6, section 7, section 19, section 24, section 26,
section 30, section 34, section 38, section 39, section 46.4, section 46.5,
section 47.3.
ADR-0011 (live buckets, observation disposition, transition emission,
re-evaluation; its section 9 is the interface reference these tests build
against), ADR-0009 decisions 3/6/7 (`run_maintenance`, the strictly-greater
version rule, flush-before-commit), ADR-0010 decision 6 (one bucket per
observation), ADR-0005/section 46.4 (`weight`), ADR-0003's amendment and
ADR-0004 decision 4 (producer identity, `subject`).
Wire shapes: `schemas/hot_ip_event.v1.json`, `schemas/ip_attributes.v1.json`.

Written blind to every module under `services/aggregator/.../` per this
repo's test-author convention (see `services/ingest/.../tests/
test_pipeline.py`): the surface exercised here is exactly ADR-0011's
section 9 interface reference plus the M1 names it composes
(`hammertime.core.state`, `hammertime.core.events`, `hammertime.bus`,
`hammertime.core.time.clock`). Every expected number is computed in-test
from the spec formulas -- the section 6 threshold table and section 46.4's
`threshold_ratio` -- never copied from an implementation.

Assumptions that are *not* pinned by spec/ADR/schema, flagged so they can be
reconciled rather than re-derived:

* `StateTransition` is only ever inspected through `.current`, the one
  attribute ADR-0011 decision 4 names (`ip_entry.state = transition.current`).
  Its other field names are not documented anywhere, so the *direction* of a
  transition is additionally asserted through the event `build_event`
  produces for it (section 19: COLD -> HOT is `HotIpAdded`, HOT -> COLD is
  `HotIpRemoved`), which is documented.
* `TransitionPublisher.publish` is asserted to assign the *envelope*
  `sequence` (ADR-0011 decision 5). ADR-0011's `build_event` table says the
  payload's own `sequence` is "assigned by the publisher below" without
  saying whether the publisher rewrites the payload it was handed, so no
  assertion is made about `payload.sequence`.
* Topic logs are read back through `bus._logs[...]`, the precedent set by
  `services/ingest/.../tests/test_pipeline.py::_topic_records` (a live
  `subscribe()` iterator blocks on the *next* message, which is awkward to
  drive from a test that published a bounded burst).
* `ManualClock(initial=...)` is constructed by keyword, as
  `packages/hammertime-core/.../tests/test_clock.py` does.
* The flush-before-commit ordering of ADR-0011 decision 4 is not directly
  observable through `InMemoryBus` (nothing exposes "a commit happened
  here"), so `TestRunAndStop` asserts the observable half: the transition
  records are already on `hammertime.hot-ip.v1` before `stop()` is called
  and before the committed position is read back by a fresh consumer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from hammertime.aggregator.reevaluate import ReevaluationReport
from hammertime.aggregator.transitions import TransitionPublisher, build_event, decide
from hammertime.aggregator.window.store import InMemoryWindowStore
from hammertime.aggregator.worker import AggregatorWorker
from hammertime.bus.interface import ConsumedMessage
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import HOT_IP, OBSERVATIONS, OBSERVATIONS_RECONCILIATION
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import (
    HotIpAdded,
    HotIpRemoved,
    Observation,
    RequestObservation,
)
from hammertime.core.state import IpState, evaluate_ip_state
from hammertime.core.time.clock import ManualClock

# docs/spec/integration-scenarios.md section 2: T0 = 1_800_000_000
# (2027-01-15T08:00:00Z), aligned to bucket_seconds.
T0 = 1_800_000_000

PRODUCER_ID = "aggregator-0"
AGENT_ID = "edge-17"
CONSUMER_GROUP = "hammertime-aggregator"

# Spec section 6's worked example, and config/detection.v1.json's own values.
HOT_THRESHOLD = 1000
COLD_THRESHOLD = 800

ATTRIBUTES_VERSION = 1  # schemas/ip_attributes.v1.json


def _config(**overrides: object) -> DetectionConfig:
    """`config/detection.v1.json` (integration-scenarios.md section 2.1), overridable."""

    fields: dict[str, object] = {
        "config_version": 1,
        "window_seconds": 300,
        "bucket_seconds": 10,
        "hot_threshold": HOT_THRESHOLD,
        "cold_threshold": COLD_THRESHOLD,
        "minimum_hot_ips": 16,
        "minimum_hot_ratio": 0.10,
        "allowed_lateness_seconds": 30,
        "state_retention_seconds": 600,
        "weight_function": "threshold_ratio",
        "weight_max": 1_000_000,
    }
    fields.update(overrides)
    return DetectionConfig(**fields)  # type: ignore[arg-type]


def _expected_weight(window_count: int, config: DetectionConfig) -> int:
    """Spec section 46.4's `threshold_ratio`, restated here as the oracle.

    clamp((1000 * window_count + hot_threshold // 2) // hot_threshold, 0, weight_max),
    integer arithmetic throughout -- deliberately written out from the spec
    rather than imported, so these tests cannot agree with a wrong
    implementation of the same formula.
    """

    raw = (1000 * window_count + config.hot_threshold // 2) // config.hot_threshold
    return max(0, min(raw, config.weight_max))


def _expected_attributes(window_count: int, config: DetectionConfig) -> dict[str, object]:
    """Spec section 46.5 / ADR-0011 decision 5: the document on a `HotIpAdded`."""

    return {
        "attributes_version": ATTRIBUTES_VERSION,
        "weight": _expected_weight(window_count, config),
    }


def _records(bus: InMemoryBus, topic: str) -> list[tuple[bytes | None, bytes]]:
    """Every `(key, value)` on `topic`, read straight off the bus's log."""

    log = bus._logs.get(topic)
    return [(record.key, record.value) for record in log] if log else []


def _payloads(bus: InMemoryBus, topic: str | None = None) -> list[HotIpAdded | HotIpRemoved]:
    """Decoded hot-ip payloads, in publish order."""

    name = topic if topic is not None else HOT_IP.name
    payloads: list[HotIpAdded | HotIpRemoved] = []
    for _key, value in _records(bus, name):
        payload = decode(value).payload
        assert isinstance(payload, HotIpAdded | HotIpRemoved)
        payloads.append(payload)
    return payloads


def _observation_envelope(
    ip: Address,
    request_count: int,
    *,
    window_start: int,
    sequence: int,
    window_seconds: int = 10,
    config_version: int = 1,
) -> EventEnvelope[RequestObservation]:
    """A single-entry `RequestObservation` envelope, exactly as ingest publishes one.

    ADR-0004: one bus message per IP, keyed and `subject`-tagged by that IP.
    """

    started = datetime.fromtimestamp(window_start, UTC)
    payload = RequestObservation(
        agent_id=AGENT_ID,
        sequence=sequence,
        window_start=started,
        window_seconds=window_seconds,
        observations=(Observation(ip=ip, request_count=request_count),),
    )
    return EventEnvelope(
        agent_id=AGENT_ID,
        sequence=sequence,
        event_type="RequestObservation",
        config_version=config_version,
        timestamp=started,
        payload=payload,
        subject=str(ip),
    )


def _message(
    ip: Address,
    request_count: int,
    *,
    window_start: int,
    sequence: int = 1,
    window_seconds: int = 10,
) -> ConsumedMessage:
    envelope = _observation_envelope(
        ip,
        request_count,
        window_start=window_start,
        sequence=sequence,
        window_seconds=window_seconds,
    )
    return _consumed(str(ip).encode("utf-8"), encode(envelope), offset=sequence)


def _consumed(key: bytes, value: bytes, *, offset: int = 0) -> ConsumedMessage:
    return ConsumedMessage(
        topic=OBSERVATIONS.name, partition=0, offset=offset, key=key, value=value
    )


def _tamper_nested(data: bytes, *, path: tuple[object, ...], value: object) -> bytes:
    """Override a value nested inside `payload` of already-encoded wire bytes.

    The same helper `packages/hammertime-core/.../tests/test_codec.py` uses, for
    the same reason: it builds otherwise-valid bytes with one field out of
    range without hard-coding the rest of the wire shape.
    """

    doc: Any = json.loads(data)
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return json.dumps(doc).encode("utf-8")


def _build(
    *,
    config: DetectionConfig | None = None,
    clock: ManualClock | None = None,
    commit_every: int = 100,
    bus: InMemoryBus | None = None,
) -> tuple[AggregatorWorker, InMemoryBus, ManualClock]:
    """An `AggregatorWorker` over an in-memory store, bus and `ManualClock`.

    ADR-0011 section 9's constructor, with the store's geometry taken from
    the same `DetectionConfig` the worker is given (ADR-0011 consequences:
    that is how `build_service` will compose it).
    """

    resolved_config = config if config is not None else _config()
    resolved_clock = clock if clock is not None else ManualClock(initial=T0)
    resolved_bus = bus if bus is not None else InMemoryBus()
    worker = AggregatorWorker(
        config=resolved_config,
        store=InMemoryWindowStore(
            window_seconds=resolved_config.window_seconds,
            bucket_seconds=resolved_config.bucket_seconds,
        ),
        producer=resolved_bus.producer(),
        producer_id=PRODUCER_ID,
        clock=resolved_clock,
        commit_every=commit_every,
    )
    return worker, resolved_bus, resolved_clock


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.01
) -> None:
    """Poll `predicate` until true, or fail.

    The only waiting anywhere in this file, and it is never a *time source*:
    the domain clock is always the `ManualClock`. It exists solely because
    `AggregatorWorker.run()` is driven as a task (ADR-0011 decision 4) and a
    test cannot otherwise tell when that task has caught up.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("predicate never became true within the timeout")


# ---------------------------------------------------------------------------
# 1. Spec section 6's table, case by case (issue #6).
# ---------------------------------------------------------------------------


class TestSectionSixTableCaseByCase:
    """`hot_threshold = 1000`, `cold_threshold = 800` -- the spec's own example.

    | Current state | Window count | New state |
    | COLD | `< hot_threshold`  | COLD |
    | COLD | `>= hot_threshold` | HOT  |
    | HOT  | `< cold_threshold` | COLD |
    | HOT  | `>= cold_threshold`| HOT  |
    """

    @pytest.mark.parametrize("count", [799, 800, 999])
    def test_cold_below_hot_threshold_stays_cold(self, count: int) -> None:
        assert decide(IpState.COLD, count, _config()) is None

    def test_cold_at_hot_threshold_becomes_hot(self) -> None:
        config = _config()
        transition = decide(IpState.COLD, HOT_THRESHOLD, config)

        assert transition is not None
        assert transition.current is IpState.HOT
        # Section 19: a COLD -> HOT edge is a HotIpAdded, never a removal.
        event = build_event(
            Address.parse("10.0.0.1"),
            transition,
            window_count=HOT_THRESHOLD,
            config=config,
            now=T0,
        )
        assert isinstance(event, HotIpAdded)

    @pytest.mark.parametrize("count", [1000, 900, 800])
    def test_hot_at_or_above_cold_threshold_stays_hot(self, count: int) -> None:
        assert decide(IpState.HOT, count, _config()) is None

    def test_hot_below_cold_threshold_becomes_cold(self) -> None:
        config = _config()
        transition = decide(IpState.HOT, COLD_THRESHOLD - 1, config)

        assert transition is not None
        assert transition.current is IpState.COLD
        event = build_event(
            Address.parse("10.0.0.1"),
            transition,
            window_count=COLD_THRESHOLD - 1,
            config=config,
            now=T0,
        )
        assert isinstance(event, HotIpRemoved)

    @pytest.mark.parametrize(
        ("previous", "count"),
        [
            (IpState.COLD, 799),
            (IpState.COLD, 800),
            (IpState.COLD, 999),
            (IpState.COLD, 1000),
            (IpState.HOT, 1000),
            (IpState.HOT, 900),
            (IpState.HOT, 800),
            (IpState.HOT, 799),
        ],
    )
    def test_decide_agrees_with_the_single_authoritative_state_machine(
        self, previous: IpState, count: int
    ) -> None:
        # Spec section 30 / ADR-0011 decision 5: `decide` MUST route through
        # `evaluate_ip_state` rather than restate the comparison, so the two
        # can never disagree on any cell of the table above.
        config = _config()
        expected = evaluate_ip_state(previous, count, config)
        transition = decide(previous, count, config)

        if expected is previous:
            assert transition is None
        else:
            assert transition is not None
            assert transition.current is expected


class TestHysteresisHasNoSingleFlippingBoundary:
    """Section 6: the implementation MUST NOT use one symmetric comparison.

    Issue #6's anti-flap criterion, stated as a property of the table: no
    single window count can both promote a COLD IP and demote a HOT one, so
    a count that hovers at a threshold cannot oscillate.
    """

    @pytest.mark.parametrize("count", [0, 1, 400, 799, 800, 801, 999, 1000, 1001, 5000])
    def test_no_count_transitions_in_both_directions(self, count: int) -> None:
        config = _config()
        from_cold = decide(IpState.COLD, count, config)
        from_hot = decide(IpState.HOT, count, config)

        assert not (from_cold is not None and from_hot is not None)

    @pytest.mark.parametrize("count", [COLD_THRESHOLD, 850, 900, 999, HOT_THRESHOLD - 1])
    def test_the_intervening_band_is_state_dependent_and_never_moves(self, count: int) -> None:
        # Section 7: "If 800 <= count < 1000 the existing state is retained."
        config = _config()
        assert decide(IpState.COLD, count, config) is None
        assert decide(IpState.HOT, count, config) is None

    def test_a_count_repeatedly_crossing_the_cold_threshold_never_oscillates(self) -> None:
        # Section 7's COLD -> HOT -> COLD -> HOT oscillation, denied: noise
        # in [cold_threshold, hot_threshold) moves nothing at all.
        config = _config()
        noise = (999, 800, 999, 801, 998, 800)

        # Neither state moves anywhere under any of these counts, so a stream
        # of them leaves the IP exactly where it started, whichever that was.
        for count in noise:
            assert decide(IpState.COLD, count, config) is None
            assert decide(IpState.HOT, count, config) is None


# ---------------------------------------------------------------------------
# 2. build_event (ADR-0011 decision 5, spec sections 19, 46.4, 46.5).
# ---------------------------------------------------------------------------


class TestBuildEvent:
    _IP = Address.parse("192.168.1.42")

    def test_became_hot_builds_a_hot_ip_added_carrying_the_count_and_weight(self) -> None:
        config = _config()
        window_count = 1200
        transition = decide(IpState.COLD, window_count, config)
        assert transition is not None

        event = build_event(self._IP, transition, window_count=window_count, config=config, now=T0)

        assert isinstance(event, HotIpAdded)
        assert event.ip == self._IP
        assert event.window_count == window_count
        assert event.config_version == config.config_version
        assert event.timestamp == datetime.fromtimestamp(T0, UTC)
        # Section 46.4: weight = threshold_ratio(1200, hot_threshold=1000) = 1200.
        assert event.attributes == _expected_attributes(window_count, config)
        assert _expected_weight(window_count, config) == 1200

    def test_weight_is_computed_under_the_config_in_force_not_the_default(self) -> None:
        # integration-scenarios.md section 4 step 2: at hot_threshold 500 a
        # window count of 600 weighs 1200 -- (1000*600 + 250) // 500.
        config = _config(config_version=2, hot_threshold=500, cold_threshold=400)
        transition = decide(IpState.COLD, 600, config)
        assert transition is not None

        event = build_event(self._IP, transition, window_count=600, config=config, now=T0)

        assert isinstance(event, HotIpAdded)
        assert event.config_version == 2
        assert event.attributes == {"attributes_version": 1, "weight": 1200}
        assert _expected_weight(600, config) == 1200

    def test_weight_is_clamped_to_weight_max(self) -> None:
        config = _config(weight_max=5000)
        transition = decide(IpState.COLD, 10_000, config)
        assert transition is not None

        event = build_event(self._IP, transition, window_count=10_000, config=config, now=T0)

        assert isinstance(event, HotIpAdded)
        assert event.attributes == {"attributes_version": 1, "weight": 5000}

    def test_became_cold_builds_a_hot_ip_removed_with_no_attributes(self) -> None:
        # Section 46.5: attributes are never stored for a removal; ADR-0011
        # decision 5 makes them absent on the wire.
        config = _config()
        transition = decide(IpState.HOT, 0, config)
        assert transition is not None

        event = build_event(self._IP, transition, window_count=0, config=config, now=T0 + 310)

        assert isinstance(event, HotIpRemoved)
        assert event.ip == self._IP
        assert event.window_count == 0
        assert event.config_version == config.config_version
        assert event.timestamp == datetime.fromtimestamp(T0 + 310, UTC)
        assert event.attributes is None


# ---------------------------------------------------------------------------
# 3. TransitionPublisher (ADR-0011 decision 5, ADR-0003 amendment, ADR-0004).
# ---------------------------------------------------------------------------


class TestTransitionPublisher:
    _IP = Address.parse("10.20.30.1")

    def _added(self, *, window_count: int = 1200, config_version: int = 1) -> HotIpAdded:
        config = _config(config_version=config_version)
        transition = decide(IpState.COLD, window_count, config)
        assert transition is not None
        event = build_event(self._IP, transition, window_count=window_count, config=config, now=T0)
        assert isinstance(event, HotIpAdded)
        return event

    def _removed(self, *, window_count: int = 0, config_version: int = 1) -> HotIpRemoved:
        config = _config(config_version=config_version)
        transition = decide(IpState.HOT, window_count, config)
        assert transition is not None
        event = build_event(
            self._IP, transition, window_count=window_count, config=config, now=T0 + 310
        )
        assert isinstance(event, HotIpRemoved)
        return event

    async def test_envelope_identity_fields_come_from_the_producer_and_the_event(self) -> None:
        bus = InMemoryBus()
        publisher = TransitionPublisher(bus.producer(), producer_id=PRODUCER_ID)
        event = self._added(config_version=3)

        envelope = await publisher.publish(event)

        # ADR-0003 amendment: agent_id is the producing shard, not the agent.
        assert envelope.agent_id == PRODUCER_ID
        assert envelope.event_type == "HotIpAdded"
        # ADR-0011 decision 5 / ADR-0004 decision 4: subject is the IP text.
        assert envelope.subject == str(self._IP)
        assert envelope.config_version == event.config_version == 3
        assert envelope.timestamp == event.timestamp
        assert envelope.payload == event

    async def test_removed_envelope_names_its_own_event_type(self) -> None:
        bus = InMemoryBus()
        publisher = TransitionPublisher(bus.producer(), producer_id=PRODUCER_ID)

        envelope = await publisher.publish(self._removed())

        assert envelope.event_type == "HotIpRemoved"
        assert envelope.agent_id == PRODUCER_ID
        assert envelope.subject == str(self._IP)

    async def test_one_sequence_counter_runs_consecutively_across_both_event_types(self) -> None:
        # ADR-0011 decision 5: one counter per publisher for the whole hot-ip
        # stream, shared by both event types, starting at initial_sequence.
        bus = InMemoryBus()
        publisher = TransitionPublisher(bus.producer(), producer_id=PRODUCER_ID)
        assert publisher.next_sequence == 0

        sequences = [
            (await publisher.publish(self._added())).sequence,
            (await publisher.publish(self._removed())).sequence,
            (await publisher.publish(self._added())).sequence,
            (await publisher.publish(self._removed())).sequence,
        ]

        assert sequences == [0, 1, 2, 3]
        assert publisher.next_sequence == 4

    async def test_sequences_start_at_initial_sequence(self) -> None:
        bus = InMemoryBus()
        publisher = TransitionPublisher(
            bus.producer(), producer_id=PRODUCER_ID, initial_sequence=17
        )
        assert publisher.next_sequence == 17

        first = await publisher.publish(self._added())
        second = await publisher.publish(self._removed())

        assert [first.sequence, second.sequence] == [17, 18]

    async def test_record_lands_on_the_hot_ip_topic_keyed_by_the_ip(self) -> None:
        bus = InMemoryBus()
        publisher = TransitionPublisher(bus.producer(), producer_id=PRODUCER_ID)

        envelope = await publisher.publish(self._added())
        await publisher.flush()

        records = _records(bus, HOT_IP.name)
        assert len(records) == 1
        key, value = records[0]
        assert key == str(self._IP).encode("utf-8")
        # The stored bytes are the envelope, decodable by any consumer.
        assert decode(value) == envelope

    async def test_event_ids_are_distinct_per_sequence_and_type(self) -> None:
        # ADR-0011 decision 5: event_id is unique per (producer, sequence,
        # type, ip), so a mixed stream never collides.
        bus = InMemoryBus()
        publisher = TransitionPublisher(bus.producer(), producer_id=PRODUCER_ID)

        envelopes = [
            await publisher.publish(self._added()),
            await publisher.publish(self._removed()),
            await publisher.publish(self._added()),
        ]

        event_ids = [envelope.event_id for envelope in envelopes]
        assert len(set(event_ids)) == len(event_ids)


# ---------------------------------------------------------------------------
# 4. The per-observation pipeline (spec sections 30/39, ADR-0011 decision 4).
# ---------------------------------------------------------------------------


class TestWorkerSingleMessagePipeline:
    _IP = Address.parse("10.20.30.1")

    async def test_one_observation_over_the_hot_threshold_emits_hot_ip_added(self) -> None:
        worker, bus, _clock = _build()
        config = _config()

        await worker.apply_message(_message(self._IP, 1200, window_start=T0, sequence=1))

        records = _records(bus, HOT_IP.name)
        assert len(records) == 1
        key, value = records[0]
        assert key == str(self._IP).encode("utf-8")
        envelope = decode(value)
        payload = envelope.payload
        assert isinstance(payload, HotIpAdded)
        assert payload.ip == self._IP
        assert payload.window_count == 1200
        assert payload.config_version == 1
        assert payload.attributes == _expected_attributes(1200, config)
        assert payload.attributes == {"attributes_version": 1, "weight": 1200}
        assert envelope.agent_id == PRODUCER_ID
        assert envelope.agent_id != AGENT_ID

        assert worker.metrics.observations_applied == 1
        assert worker.metrics.cold_to_hot_transitions == 1
        assert worker.metrics.hot_to_cold_transitions == 0
        entry = worker.store.get(self._IP)
        assert entry is not None
        assert entry.state is IpState.HOT

    async def test_a_second_observation_that_changes_no_state_emits_nothing(self) -> None:
        # Section 6: HOT + more traffic is still HOT -- no edge, no event.
        worker, bus, _clock = _build()
        await worker.apply_message(_message(self._IP, 1200, window_start=T0, sequence=1))
        assert len(_records(bus, HOT_IP.name)) == 1

        await worker.apply_message(_message(self._IP, 100, window_start=T0, sequence=2))

        assert len(_records(bus, HOT_IP.name)) == 1
        assert worker.metrics.observations_applied == 2
        assert worker.metrics.cold_to_hot_transitions == 1
        entry = worker.store.get(self._IP)
        assert entry is not None
        assert entry.state is IpState.HOT

    async def test_request_counts_accumulate_across_buckets_until_the_threshold(self) -> None:
        # Section 5: window_count is the sum over live buckets; ADR-0010
        # decision 6 puts each observation's whole delta in one bucket.
        worker, bus, clock = _build()

        await worker.apply_message(_message(self._IP, 600, window_start=T0, sequence=1))
        assert _records(bus, HOT_IP.name) == []
        entry = worker.store.get(self._IP)
        assert entry is not None
        assert entry.state is IpState.COLD

        clock.set(T0 + 10)
        await worker.apply_message(_message(self._IP, 600, window_start=T0 + 10, sequence=2))

        payloads = _payloads(bus)
        assert len(payloads) == 1
        added = payloads[0]
        assert isinstance(added, HotIpAdded)
        assert added.window_count == 1200  # 600 + 600, two live buckets
        assert added.attributes == _expected_attributes(1200, _config())
        entry = worker.store.get(self._IP)
        assert entry is not None
        assert entry.state is IpState.HOT
        assert worker.metrics.observations_applied == 2


# ---------------------------------------------------------------------------
# 5. Disposition: applied XOR reconciled (ADR-0011 decision 2, sections 24/25).
# ---------------------------------------------------------------------------


class TestObservationDisposition:
    """Every well-formed observation is applied or republished -- never both, never neither."""

    _LATE = Address.parse("10.40.0.1")
    _FUTURE = Address.parse("10.40.0.2")
    _EXPIRED = Address.parse("10.40.0.3")
    _APPLIED = Address.parse("10.40.0.4")

    async def test_an_observation_beyond_the_lateness_horizon_is_reconciled(self) -> None:
        # W + L = 330; an age of 400 is LATE.
        worker, bus, _clock = _build()
        message = _message(self._LATE, 5000, window_start=T0 - 400, sequence=1)

        await worker.apply_message(message)

        assert worker.metrics.late_messages == 1
        assert worker.metrics.observations_applied == 0
        assert worker.store.get(self._LATE) is None
        assert _records(bus, HOT_IP.name) == []

        reconciled = _records(bus, OBSERVATIONS_RECONCILIATION.name)
        assert len(reconciled) == 1
        key, value = reconciled[0]
        # ADR-0011 decision 2: the *original* bytes under the *original* key,
        # republished unchanged, event_id and all.
        assert value == message.value
        assert key == message.key
        assert worker.metrics.reconciliation_published == 1

    async def test_a_future_dated_observation_is_reconciled(self) -> None:
        worker, bus, _clock = _build()
        message = _message(self._FUTURE, 5000, window_start=T0 + 10, sequence=1)

        await worker.apply_message(message)

        assert worker.metrics.future_messages == 1
        assert worker.metrics.observations_applied == 0
        assert worker.store.get(self._FUTURE) is None
        reconciled = _records(bus, OBSERVATIONS_RECONCILIATION.name)
        assert len(reconciled) == 1
        assert reconciled[0] == (message.key, message.value)

    async def test_an_observation_whose_bucket_already_left_the_window_is_reconciled(self) -> None:
        # ADR-0011 decision 1: live(S, now) is `S <= now < S + W`, so a bucket
        # starting exactly W ago has just expired -- inside the lateness
        # horizon (300 <= 330) but no longer countable.
        worker, bus, _clock = _build()
        message = _message(self._EXPIRED, 5000, window_start=T0 - 300, sequence=1)

        await worker.apply_message(message)

        assert worker.metrics.expired_on_arrival == 1
        assert worker.metrics.late_messages == 0
        assert worker.metrics.observations_applied == 0
        assert worker.store.get(self._EXPIRED) is None
        reconciled = _records(bus, OBSERVATIONS_RECONCILIATION.name)
        assert len(reconciled) == 1
        assert reconciled[0] == (message.key, message.value)

    async def test_an_observation_in_the_oldest_live_bucket_is_applied(self) -> None:
        worker, bus, _clock = _build()

        await worker.apply_message(_message(self._APPLIED, 5000, window_start=T0 - 290, sequence=1))

        assert worker.metrics.observations_applied == 1
        assert worker.metrics.expired_on_arrival == 0
        assert worker.metrics.late_messages == 0
        assert worker.metrics.future_messages == 0
        assert _records(bus, OBSERVATIONS_RECONCILIATION.name) == []
        entry = worker.store.get(self._APPLIED)
        assert entry is not None
        assert entry.state is IpState.HOT

    async def test_every_well_formed_message_is_applied_exactly_once_or_reconciled_once(
        self,
    ) -> None:
        worker, bus, _clock = _build()
        messages = [
            _message(self._LATE, 100, window_start=T0 - 400, sequence=1),
            _message(self._FUTURE, 100, window_start=T0 + 10, sequence=2),
            _message(self._EXPIRED, 100, window_start=T0 - 300, sequence=3),
            _message(self._APPLIED, 100, window_start=T0 - 290, sequence=4),
        ]
        for message in messages:
            await worker.apply_message(message)

        metrics = worker.metrics
        assert metrics.observations_applied + metrics.reconciliation_published == len(messages)
        assert metrics.observations_applied == 1
        assert metrics.reconciliation_published == 3
        assert metrics.late_messages == 1
        assert metrics.future_messages == 1
        assert metrics.expired_on_arrival == 1
        assert len(_records(bus, OBSERVATIONS_RECONCILIATION.name)) == 3
        assert metrics.observations_rejected == 0


# ---------------------------------------------------------------------------
# 6. Rejects (ADR-0011 decision 4: counted, skipped, never reconciled).
# ---------------------------------------------------------------------------


class TestWorkerRejects:
    _IP = Address.parse("10.50.0.1")

    async def test_undecodable_bytes_are_counted_and_published_nowhere(self) -> None:
        worker, bus, _clock = _build()

        await worker.apply_message(_consumed(b"10.50.0.1", b"\x00\x01\xffnot json at all"))

        assert worker.metrics.observations_rejected == 1
        assert worker.metrics.observations_applied == 0
        # A poison message is not "a well-formed observation the aggregator
        # declined", so it must not reach the reconciliation topic either.
        assert _records(bus, OBSERVATIONS_RECONCILIATION.name) == []
        assert _records(bus, HOT_IP.name) == []

    async def test_a_payload_of_the_wrong_type_is_rejected(self) -> None:
        worker, bus, _clock = _build()
        config = _config()
        transition = decide(IpState.COLD, 1200, config)
        assert transition is not None
        event = build_event(self._IP, transition, window_count=1200, config=config, now=T0)
        envelope = EventEnvelope(
            agent_id=PRODUCER_ID,
            sequence=1,
            event_type="HotIpAdded",
            config_version=1,
            timestamp=datetime.fromtimestamp(T0, UTC),
            payload=event,
            subject=str(self._IP),
        )

        await worker.apply_message(_consumed(str(self._IP).encode("utf-8"), encode(envelope)))

        assert worker.metrics.observations_rejected == 1
        assert worker.metrics.observations_applied == 0
        assert worker.store.get(self._IP) is None
        assert _records(bus, OBSERVATIONS_RECONCILIATION.name) == []
        assert _records(bus, HOT_IP.name) == []

    @pytest.mark.parametrize("request_count", [0, -5])
    async def test_a_non_positive_request_count_is_rejected(self, request_count: int) -> None:
        # ADR-0011 decision 4: `entry.request_count < 1` is rejected. The
        # envelope is built by tampering the encoded JSON, because a
        # zero/negative delta may also be refused by the codec itself
        # (ADR-0008: ingest never publishes one) -- either way the worker
        # must count exactly one rejection and apply nothing.
        worker, bus, _clock = _build()
        valid = _message(self._IP, 1, window_start=T0, sequence=1)
        tampered = _tamper_nested(
            valid.value,
            path=("payload", "observations", 0, "request_count"),
            value=request_count,
        )

        await worker.apply_message(_consumed(valid.key or b"", tampered))

        assert worker.metrics.observations_rejected == 1
        assert worker.metrics.observations_applied == 0
        assert worker.store.get(self._IP) is None
        assert _records(bus, HOT_IP.name) == []
        assert _records(bus, OBSERVATIONS_RECONCILIATION.name) == []


# ---------------------------------------------------------------------------
# 7. Maintenance: expiry drives HOT -> COLD, then retention evicts (sections 5, 26).
# ---------------------------------------------------------------------------


class TestMaintenance:
    _IP = Address.parse("10.60.0.1")

    async def test_whole_window_expiry_emits_hot_ip_removed_with_a_zero_count(self) -> None:
        worker, bus, clock = _build()
        await worker.apply_message(_message(self._IP, 1200, window_start=T0, sequence=1))

        clock.set(T0 + 310)
        await worker.run_maintenance()

        payloads = _payloads(bus)
        assert len(payloads) == 2
        removed = payloads[1]
        assert isinstance(removed, HotIpRemoved)
        assert removed.ip == self._IP
        assert removed.window_count == 0
        assert removed.config_version == 1
        assert removed.attributes is None
        assert removed.timestamp == datetime.fromtimestamp(T0 + 310, UTC)
        assert worker.metrics.hot_to_cold_transitions == 1
        entry = worker.store.get(self._IP)
        assert entry is not None
        assert entry.state is IpState.COLD

    async def test_a_cold_entry_is_evicted_once_retention_lapses(self) -> None:
        # Section 26 / ADR-0011 decision 3: eviction only ever touches COLD
        # entries, and only after state_retention_seconds of silence.
        worker, _bus, clock = _build()
        await worker.apply_message(_message(self._IP, 1200, window_start=T0, sequence=1))
        clock.set(T0 + 310)
        await worker.run_maintenance()
        assert worker.metrics.evicted_ips == 0
        assert self._IP in worker.store

        clock.set(T0 + 310 + 600)
        await worker.run_maintenance()

        assert worker.metrics.evicted_ips == 1
        assert worker.store.get(self._IP) is None

    async def test_partial_expiry_emits_the_count_that_survived(self) -> None:
        worker, bus, clock = _build()

        await worker.apply_message(_message(self._IP, 900, window_start=T0, sequence=1))
        clock.set(T0 + 100)
        await worker.apply_message(_message(self._IP, 300, window_start=T0 + 100, sequence=2))

        payloads = _payloads(bus)
        assert len(payloads) == 1
        added = payloads[0]
        assert isinstance(added, HotIpAdded)
        assert added.window_count == 1200  # 900 + 300

        # The T0 bucket expires at T0 + window_seconds; the T0+100 one does not.
        clock.set(T0 + 300)
        await worker.run_maintenance()

        payloads = _payloads(bus)
        assert len(payloads) == 2
        removed = payloads[1]
        assert isinstance(removed, HotIpRemoved)
        assert removed.window_count == 300  # 300 < cold_threshold 800
        entry = worker.store.get(self._IP)
        assert entry is not None
        assert entry.state is IpState.COLD


# ---------------------------------------------------------------------------
# 8. Hysteresis under decay (section 7).
# ---------------------------------------------------------------------------


class TestHysteresisUnderDecay:
    """A HOT IP whose window decays into the band keeps its state (section 7)."""

    _IP = Address.parse("10.70.0.1")

    async def test_decay_into_the_band_keeps_the_ip_hot_and_emits_nothing(self) -> None:
        worker, bus, clock = _build()

        await worker.apply_message(_message(self._IP, 1200, window_start=T0, sequence=1))
        assert len(_payloads(bus)) == 1

        clock.set(T0 + 200)
        await worker.apply_message(_message(self._IP, 900, window_start=T0 + 200, sequence=2))
        assert len(_payloads(bus)) == 1  # 2100 is still HOT: no edge

        # The T0 bucket expires; 900 remains, which is >= cold_threshold 800.
        clock.set(T0 + 310)
        await worker.run_maintenance()

        assert len(_payloads(bus)) == 1
        assert worker.metrics.hot_to_cold_transitions == 0
        entry = worker.store.get(self._IP)
        assert entry is not None
        assert entry.state is IpState.HOT

    async def test_only_a_count_below_the_cold_threshold_finally_demotes_it(self) -> None:
        worker, bus, clock = _build()
        await worker.apply_message(_message(self._IP, 1200, window_start=T0, sequence=1))
        clock.set(T0 + 200)
        await worker.apply_message(_message(self._IP, 900, window_start=T0 + 200, sequence=2))
        clock.set(T0 + 310)
        await worker.run_maintenance()

        # The T0+200 bucket expires at T0+500; by T0+620 nothing is live.
        clock.set(T0 + 620)
        await worker.run_maintenance()

        payloads = _payloads(bus)
        assert len(payloads) == 2
        removed = payloads[1]
        assert isinstance(removed, HotIpRemoved)
        assert removed.window_count == 0
        assert worker.metrics.hot_to_cold_transitions == 1
        entry = worker.store.get(self._IP)
        assert entry is not None
        assert entry.state is IpState.COLD


# ---------------------------------------------------------------------------
# 9. Configuration changes (section 34, section 47.3, ADR-0011 decision 7).
# ---------------------------------------------------------------------------


_REEVALUATED_IPS = tuple(Address.parse(f"10.20.30.{i}") for i in range(1, 33))

# integration-scenarios.md section 4: v2 lowers both thresholds (600 becomes
# HOT), v3 restores section 6's own example values under a greater version.
_V2 = _config(config_version=2, hot_threshold=500, cold_threshold=400)
_V3 = _config(config_version=3, hot_threshold=1000, cold_threshold=800)


async def _observe_all(worker: AggregatorWorker, *, request_count: int) -> None:
    for sequence, ip in enumerate(_REEVALUATED_IPS, start=1):
        await worker.apply_message(_message(ip, request_count, window_start=T0, sequence=sequence))


class TestApplyConfig:
    """integration-scenarios.md section 4's numbers, at unit level."""

    async def test_lowering_the_thresholds_mass_promotes_every_tracked_ip(self) -> None:
        worker, bus, _clock = _build()
        await _observe_all(worker, request_count=600)
        assert _records(bus, HOT_IP.name) == []  # 600 < hot_threshold 1000

        report = await worker.apply_config(_V2)

        assert report == ReevaluationReport(
            previous_version=1,
            config_version=2,
            rebucketed=False,
            evaluated=len(_REEVALUATED_IPS),
            became_hot=len(_REEVALUATED_IPS),
            became_cold=0,
        )
        payloads = _payloads(bus)
        assert len(payloads) == len(_REEVALUATED_IPS)
        for payload in payloads:
            assert isinstance(payload, HotIpAdded)
            assert payload.config_version == 2
            assert payload.window_count == 600
            assert payload.attributes == _expected_attributes(600, _V2)
            assert payload.attributes == {"attributes_version": 1, "weight": 1200}
        assert worker.config.config_version == 2
        assert worker.metrics.config_reloads == 1
        assert worker.metrics.cold_to_hot_transitions == len(_REEVALUATED_IPS)

    async def test_transitions_are_emitted_in_first_seen_ip_order(self) -> None:
        # ADR-0011 decision 7 step 3: every entry visited in store order.
        worker, bus, _clock = _build()
        await _observe_all(worker, request_count=600)

        await worker.apply_config(_V2)

        keys = [key for key, _value in _records(bus, HOT_IP.name)]
        assert keys == [str(ip).encode("utf-8") for ip in _REEVALUATED_IPS]
        assert [str(ip) for ip, _entry in worker.store.entries()] == [
            str(ip) for ip in _REEVALUATED_IPS
        ]

    async def test_raising_the_thresholds_back_mass_demotes_every_tracked_ip(self) -> None:
        worker, bus, _clock = _build()
        await _observe_all(worker, request_count=600)
        await worker.apply_config(_V2)
        before = len(_records(bus, HOT_IP.name))

        report = await worker.apply_config(_V3)

        assert report == ReevaluationReport(
            previous_version=2,
            config_version=3,
            rebucketed=False,
            evaluated=len(_REEVALUATED_IPS),
            became_hot=0,
            became_cold=len(_REEVALUATED_IPS),
        )
        payloads = _payloads(bus)[before:]
        assert len(payloads) == len(_REEVALUATED_IPS)
        for payload in payloads:
            assert isinstance(payload, HotIpRemoved)
            assert payload.config_version == 3
            assert payload.window_count == 600  # 600 < cold_threshold 800
            assert payload.attributes is None
        assert worker.config.config_version == 3

    async def test_a_document_at_the_same_version_is_ignored_entirely(self) -> None:
        # Section 47.3 / ADR-0009 decision 6: applied only if config_version
        # is *strictly* greater than the version in force.
        worker, bus, _clock = _build()
        await _observe_all(worker, request_count=600)
        await worker.apply_config(_V2)
        await worker.apply_config(_V3)
        before = len(_records(bus, HOT_IP.name))
        reloads = worker.metrics.config_reloads

        report = await worker.apply_config(
            _config(config_version=3, hot_threshold=100, cold_threshold=50)
        )

        assert report is None
        assert len(_records(bus, HOT_IP.name)) == before
        assert worker.config.config_version == 3
        # The previous version stays in force, thresholds and all.
        assert worker.config.hot_threshold == 1000
        assert worker.config.cold_threshold == 800
        assert worker.metrics.config_reloads == reloads

    async def test_a_descriptive_only_change_visits_no_entry(self) -> None:
        # Section 34: weight_function/weight_max "alter the weight recorded on
        # subsequent transitions and nothing else", so they need no
        # re-evaluation (ADR-0011 decision 7 step 4).
        worker, bus, _clock = _build()
        await _observe_all(worker, request_count=600)
        await worker.apply_config(_V2)
        await worker.apply_config(_V3)
        before = len(_records(bus, HOT_IP.name))

        report = await worker.apply_config(
            _config(config_version=4, hot_threshold=1000, cold_threshold=800, weight_max=5000)
        )

        assert report == ReevaluationReport(
            previous_version=3,
            config_version=4,
            rebucketed=False,
            evaluated=0,
            became_hot=0,
            became_cold=0,
        )
        assert len(_records(bus, HOT_IP.name)) == before
        assert worker.config.config_version == 4
        assert worker.config.weight_max == 5000

    async def test_a_bucket_geometry_change_rebuckets_and_preserves_every_count(self) -> None:
        # ADR-0011 decision 7 step 2: counts are re-placed, never invented,
        # scaled or reset.
        worker, bus, _clock = _build()
        await _observe_all(worker, request_count=600)
        before = len(_records(bus, HOT_IP.name))

        report = await worker.apply_config(_config(config_version=2, bucket_seconds=5))

        assert report is not None
        assert report.rebucketed is True
        assert report.previous_version == 1
        assert report.config_version == 2
        assert report.evaluated == len(_REEVALUATED_IPS)
        assert (report.became_hot, report.became_cold) == (0, 0)
        for ip in _REEVALUATED_IPS:
            entry = worker.store.get(ip)
            assert entry is not None
            assert entry.counter.total == 600
            assert entry.state is IpState.COLD
        assert len(_records(bus, HOT_IP.name)) == before

    async def test_shrinking_the_window_drops_counts_that_are_no_longer_live(self) -> None:
        worker, bus, clock = _build()
        await _observe_all(worker, request_count=600)
        await worker.apply_config(_config(config_version=2, bucket_seconds=5))
        before = len(_records(bus, HOT_IP.name))

        # 200 s past every observation, under a 100 s window: nothing is live.
        clock.set(T0 + 200)
        hot_before = [ip for ip, entry in worker.store.entries() if entry.state is IpState.HOT]

        report = await worker.apply_config(
            _config(config_version=3, window_seconds=100, bucket_seconds=5)
        )

        assert report is not None
        assert report.rebucketed is True
        assert report.evaluated == len(_REEVALUATED_IPS)
        for ip in _REEVALUATED_IPS:
            entry = worker.store.get(ip)
            assert entry is not None
            assert entry.counter.total == 0

        # Any IP that was HOT under the thresholds then in force can no longer
        # satisfy `count >= cold_threshold` at zero, so it must be removed.
        assert report.became_cold == len(hot_before)
        emitted = _payloads(bus)[before:]
        assert len(emitted) == len(hot_before)
        for payload in emitted:
            assert isinstance(payload, HotIpRemoved)
            assert payload.window_count == 0
            assert payload.config_version == 3


# ---------------------------------------------------------------------------
# 10. run()/stop() (ADR-0011 decision 4, ADR-0009 decision 7).
# ---------------------------------------------------------------------------


class TestRunAndStop:
    _FIRST = Address.parse("10.80.0.1")
    _SECOND = Address.parse("10.80.0.2")
    _THIRD = Address.parse("10.80.0.3")

    async def _publish(self, bus: InMemoryBus, ip: Address, count: int, *, sequence: int) -> bytes:
        envelope = _observation_envelope(ip, count, window_start=T0, sequence=sequence)
        value = encode(envelope)
        await bus.producer().publish(OBSERVATIONS.name, key=str(ip).encode("utf-8"), value=value)
        return value

    async def _read_one(self, bus: InMemoryBus, group: str) -> ConsumedMessage:
        consumer = bus.consumer(group)

        async def _first() -> ConsumedMessage:
            async for message in await consumer.subscribe(OBSERVATIONS.name):
                return message
            raise AssertionError("the subscription ended without delivering a message")

        return await asyncio.wait_for(_first(), timeout=5.0)

    async def test_run_consumes_commits_and_stops_cleanly(self) -> None:
        bus = InMemoryBus()
        worker, _bus, _clock = _build(bus=bus, commit_every=1)
        await self._publish(bus, self._FIRST, 1200, sequence=1)
        await self._publish(bus, self._SECOND, 1200, sequence=2)

        task = asyncio.create_task(worker.run(bus.consumer(CONSUMER_GROUP)))
        try:
            await _wait_until(lambda: worker.metrics.observations_applied == 2)
            # Flush-before-commit (ADR-0011 decision 4): whatever the consumer
            # position now is, the transitions it implies are already on the
            # log -- observable here as the records existing before stop().
            await _wait_until(lambda: len(_records(bus, HOT_IP.name)) == 2)
            assert worker.metrics.cold_to_hot_transitions == 2

            await worker.stop()
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            if not task.done():
                task.cancel()

        assert task.done()
        payloads = _payloads(bus)
        assert len(payloads) == 2
        assert all(isinstance(payload, HotIpAdded) for payload in payloads)
        assert [key for key, _value in _records(bus, HOT_IP.name)] == [
            str(self._FIRST).encode("utf-8"),
            str(self._SECOND).encode("utf-8"),
        ]

    async def test_a_fresh_consumer_in_the_same_group_resumes_past_both_messages(self) -> None:
        bus = InMemoryBus()
        worker, _bus, _clock = _build(bus=bus, commit_every=1)
        await self._publish(bus, self._FIRST, 1200, sequence=1)
        await self._publish(bus, self._SECOND, 1200, sequence=2)

        task = asyncio.create_task(worker.run(bus.consumer(CONSUMER_GROUP)))
        try:
            await _wait_until(lambda: worker.metrics.observations_applied == 2)
            await worker.stop()
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            if not task.done():
                task.cancel()

        third = await self._publish(bus, self._THIRD, 1200, sequence=3)

        # The committed position is past both messages, so a brand-new
        # consumer in the same group sees only the third one.
        message = await self._read_one(bus, CONSUMER_GROUP)
        assert message.value == third
        assert worker.metrics.observations_applied == 2  # the stopped worker took no more
