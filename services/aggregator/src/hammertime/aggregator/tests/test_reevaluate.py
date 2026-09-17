"""A configuration change re-evaluates every tracked IP of every owned shard.

Spec: section 34 (a change MUST define its effect on existing state, and
re-evaluation is controlled), section 38 (hysteresis is re-applied under the
new thresholds), section 46.4 (`weight` is computed under the configuration
in force at the transition), section 47.3 (the new version is visible only
after the re-evaluation it triggered has been applied). ADR-0009 decision 6
and Amendment item A5 (strictly greater version; rejected documents ignored),
ADR-0011 decision 7 (the pass), decision 2's `apply_config` (geometry) and
decision 5 (the warm-up exemption still applies).

The interface under test is ADR-0011 decision 9, which pins one signature
exactly:

    reevaluate_shard(window, emitter, *, config, ips=None, batch_size=1000) -> int

and decision 7's three steps, which the worker performs under its lock:
`window.apply_config(v)` per claimed shard, then
`emitter.evaluate(window, ip, config=v, reason="config")` for each IP in a
snapshot of `window.tracked_ips()`, then adopt `v`.

ASSUMPTIONS -- things decisions 7 and 9 do not pin. Adjust the helpers below,
not the meaning of the assertions:

1. `await worker.apply_config(candidate) -> DetectionConfig` takes an
   already-validated `DetectionConfig` (the loader/`ConfigPoller` is what
   turns a document into one, ADR-0009 A5) and returns the configuration in
   force *after* the call -- so a rejected candidate returns the unchanged
   one. The version gate itself (`candidate.config_version <=
   current.config_version` is ignored) is asserted on `apply_config` because
   section 47.3 states it of the service as a whole; ADR-0009 A5 also places
   it in `ConfigPoller`, and the two agreeing is the point.
2. `reevaluate_shard`'s `int` return is the number of transitions the pass
   emitted -- the `transitions=N` field decision 8 gives the `config_applied`
   log record.
3. `AggregatorWorker(*, bus, state_store, clock, config, metrics,
   shard_ids=None, max_tracked_ips=...)`, `start()`/`stop()`,
   `worker.window(shard)`, `worker.config`. Same assumption as
   `test_sharding.py` and `test_worker.py`.
4. `TransitionEmitter(*, producer, state_store, clock, metrics)` and
   `ShardWindow(shard=..., config=..., clock=..., inherited_hot=...,
   next_sequence=...)` are decision 4's and decision 2's own code blocks.

The pass's yield cadence (`await asyncio.sleep(0)` every
`HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH` IPs) is not observable from
outside, so what is asserted about `batch_size` is only that it cannot change
the outcome.

`BASE` is `1_800_000_000`, the `T0` of `docs/spec/integration-scenarios.md`
section 2; the 32-IP scenario is that document's section 4 step 1-3.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.reevaluate import reevaluate_shard
from hammertime.aggregator.transitions import TransitionEmitter
from hammertime.aggregator.window.store import ShardWindow
from hammertime.aggregator.worker import AggregatorWorker
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

HOT_IP_TOPIC = "hammertime.hot-ip.v1"

WINDOW_SECONDS = 300
BUCKET_SECONDS = 10

BASE = 1_800_000_000

# docs/spec/integration-scenarios.md section 4: 32 IPs in one /24 at 600
# requests each -- below v1's hot threshold, above v2's.
IPS = tuple(Address.parse(f"10.20.30.{octet}") for octet in range(1, 33))
IP_A = IPS[0]
IP_B = IPS[1]
OTHER_IP = Address.parse("198.51.100.9")


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


V1 = _config()
# Section 4 step 2: the same thresholds the integration scenario uses.
V2 = _config(config_version=2, hot_threshold=500, cold_threshold=400)
# Section 4 step 3: v1's thresholds under a higher version.
V3 = _config(config_version=3, hot_threshold=1000, cold_threshold=800)


def _records(bus: InMemoryBus, topic: str = HOT_IP_TOPIC) -> list[tuple[bytes | None, bytes]]:
    return [(record.key, record.value) for record in bus._logs.get(topic, [])]


def _decoded(
    bus: InMemoryBus, topic: str = HOT_IP_TOPIC
) -> list[tuple[bytes | None, EventEnvelope[EventPayload]]]:
    return [(key, decode(value)) for key, value in _records(bus, topic)]


def _worker(
    *,
    bus: InMemoryBus,
    clock: ManualClock,
    state_store: MemoryShardStateStore | None = None,
    config: DetectionConfig = V1,
    metrics: AggregatorMetrics | None = None,
) -> AggregatorWorker:
    """ASSUMPTION 3 (see the module docstring)."""

    return AggregatorWorker(
        bus=bus,
        state_store=state_store if state_store is not None else MemoryShardStateStore(),
        clock=clock,
        config=config,
        metrics=metrics if metrics is not None else AggregatorMetrics(),
        shard_ids=None,
    )


@asynccontextmanager
async def _running(worker: AggregatorWorker) -> AsyncIterator[AggregatorWorker]:
    """`start()` claims the memory bus's only partition; `stop()` always runs."""

    await worker.start()
    try:
        yield worker
    finally:
        await worker.stop()


def _window_of(worker: AggregatorWorker) -> ShardWindow:
    window = worker.window(0)
    assert window is not None, "the memory bus always assigns partition 0"
    return window


def _seed(window: ShardWindow, ips: tuple[Address, ...], count: int, *, at: int = BASE) -> None:
    for ip in ips:
        assert window.observe(ip, at, count) is not None


def _emitter(
    *,
    bus: InMemoryBus,
    clock: ManualClock,
    state_store: MemoryShardStateStore | None = None,
) -> TransitionEmitter:
    return TransitionEmitter(
        producer=bus.producer(),
        state_store=state_store if state_store is not None else MemoryShardStateStore(),
        clock=clock,
        metrics=AggregatorMetrics(),
    )


class TestNoChangeUntilTheThresholdsMove:
    """Section 34 step 1 of the scenario: 32 IPs at 600 under v1 are below
    `hot_threshold` (1000), so nothing is HOT and nothing has been emitted."""

    async def test_seeded_counts_produce_no_events_under_the_config_in_force(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            window = _window_of(worker)
            _seed(window, IPS, 600)

            assert window.tracked_count == 32
            assert window.hot_count == 0
            assert _records(bus) == []

    async def test_a_pass_under_the_same_config_emits_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = ShardWindow(shard=0, config=V1, clock=clock)
        _seed(window, IPS, 600)

        transitions = await reevaluate_shard(window, emitter, config=V1)

        assert transitions == 0
        assert _records(bus) == []
        assert window.hot_count == 0


class TestLoweringTheThresholdsPromotesEveryTrackedIp:
    """Section 34 / section 38: applying v2 (`hot_threshold` 500) re-applies
    hysteresis to state that already exists."""

    async def test_every_tracked_ip_is_promoted_under_the_new_version(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        async with _running(_worker(bus=bus, clock=clock, state_store=state_store)) as worker:
            window = _window_of(worker)
            _seed(window, IPS, 600)

            await worker.apply_config(V2)

            decoded = _decoded(bus)
            assert len(decoded) == 32
            assert {envelope.subject for _key, envelope in decoded} == {str(ip) for ip in IPS}
            assert window.hot_count == 32
            assert (await state_store.load(0)).hot_ips == frozenset(IPS)

    async def test_every_promotion_carries_the_new_version_count_and_weight(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)

            await worker.apply_config(V2)

            for _key, envelope in _decoded(bus):
                assert envelope.event_type == "HotIpAdded"
                assert envelope.config_version == 2
                payload = envelope.payload
                assert isinstance(payload, HotIpAdded)
                assert payload.window_count == 600
                assert payload.config_version == 2
                # 600 requests against a hot threshold of 500 is 1200
                # thousandths of the threshold (section 46.4).
                attributes = payload.attributes
                assert attributes is not None
                assert attributes == {"attributes_version": 1, "weight": 1200}
                assert attributes["weight"] == threshold_ratio(600, V2)

    async def test_the_pass_numbers_its_transitions_consecutively_and_distinctly(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)

            await worker.apply_config(V2)

            decoded = _decoded(bus)
            assert sorted(envelope.sequence for _key, envelope in decoded) == list(range(32))
            # ADR-0004: `subject` qualifies the identity, so 32 events from
            # one shard are 32 distinct events.
            assert len({envelope.event_id for _key, envelope in decoded}) == 32

    async def test_the_version_in_force_afterwards_is_the_new_one(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)

            returned = await worker.apply_config(V2)

            assert returned.config_version == 2
            assert returned.hot_threshold == 500
            assert worker.config.config_version == 2
            assert worker.config.hot_threshold == 500
            assert _window_of(worker).config.hot_threshold == 500


class TestRaisingTheThresholdsDemotesEveryTrackedIp:
    """Section 34 step 3: the reverse direction, under a third version."""

    async def _promoted(self, bus: InMemoryBus, worker: AggregatorWorker) -> None:
        _seed(_window_of(worker), IPS, 600)
        await worker.apply_config(V2)
        assert len(_records(bus)) == 32

    async def test_every_tracked_ip_is_demoted_under_the_third_version(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        async with _running(_worker(bus=bus, clock=clock, state_store=state_store)) as worker:
            await self._promoted(bus, worker)

            await worker.apply_config(V3)

            decoded = _decoded(bus)[32:]
            assert len(decoded) == 32
            assert {envelope.subject for _key, envelope in decoded} == {str(ip) for ip in IPS}
            for _key, envelope in decoded:
                assert envelope.event_type == "HotIpRemoved"
                assert envelope.config_version == 3
                payload = envelope.payload
                assert isinstance(payload, HotIpRemoved)
                # 600 is below v3's cold_threshold of 800, and the window
                # itself did not move -- only the thresholds did.
                assert payload.window_count == 600
                assert payload.config_version == 3
                assert payload.attributes is None
            assert _window_of(worker).hot_count == 0
            assert (await state_store.load(0)).hot_ips == frozenset()


class TestTheVersionGate:
    """Section 47.3 / ADR-0009 decision 6: only a strictly greater
    `config_version` is applied."""

    async def test_a_lower_version_is_ignored(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)
            await worker.apply_config(V3)
            before = len(_records(bus))

            returned = await worker.apply_config(V2)

            assert returned.config_version == 3
            assert worker.config.config_version == 3
            assert len(_records(bus)) == before

    async def test_the_version_in_force_is_ignored_even_with_different_thresholds(self) -> None:
        # Section 4 step 4 of the integration scenario: the same
        # `config_version` carrying thresholds that *would* promote every
        # tracked IP must have no effect at all.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)
            restated = _config(config_version=1, hot_threshold=100, cold_threshold=50)

            returned = await worker.apply_config(restated)

            assert returned.config_version == 1
            assert returned.hot_threshold == 1000
            assert worker.config.config_version == 1
            assert worker.config.hot_threshold == 1000
            assert _records(bus) == []
            assert _window_of(worker).hot_count == 0

    async def test_a_descriptive_only_change_is_adopted_and_produces_no_transitions(self) -> None:
        # ADR-0011 decision 7 / assumption 19: a change confined to
        # `weight_max` (or `weight_function`, `allowed_lateness_seconds`,
        # `state_retention_seconds`) runs the same pass, which is cheap and
        # produces nothing -- section 34's "no re-evaluation needed" is a
        # statement about effects.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)
            v4 = _config(config_version=4, weight_max=5000)

            returned = await worker.apply_config(v4)

            assert returned.config_version == 4
            assert returned.weight_max == 5000
            assert worker.config.config_version == 4
            assert worker.config.weight_max == 5000
            assert _records(bus) == []
            assert _window_of(worker).total(IP_A) == 600


class TestGeometryChanges:
    """Decision 2's `apply_config`: a change to `bucket_seconds` or
    `bucket_count` rebuilds every counter by re-bucketing each live
    `(S, count)`; counts whose new bucket is not live are dropped."""

    async def test_a_coarser_bucket_preserves_every_total_and_emits_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            window = _window_of(worker)
            assert window.observe(IP_A, BASE - 100, 400) is not None
            assert window.observe(IP_A, BASE, 200) is not None
            assert window.observe(IP_B, BASE - 50, 600) is not None

            await worker.apply_config(_config(config_version=2, bucket_seconds=30))

            assert worker.config.bucket_seconds == 30
            assert window.config.bucket_seconds == 30
            assert window.total(IP_A) == 600
            assert window.total(IP_B) == 600
            assert window.tracked_count == 2
            assert window.active_count == 2
            # Nothing crossed a threshold: the thresholds did not move and no
            # count was lost.
            assert _records(bus) == []

    async def test_a_shorter_window_drops_stale_counts_and_demotes(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        async with _running(_worker(bus=bus, clock=clock, state_store=state_store)) as worker:
            window = _window_of(worker)
            assert window.observe(IP_A, BASE, 1200) is not None
            assert window.observe(IP_B, BASE, 1200) is not None
            # v2 restates v1's thresholds, so the pass only promotes.
            await worker.apply_config(_config(config_version=2))
            assert [envelope.event_type for _key, envelope in _decoded(bus)] == [
                "HotIpAdded",
                "HotIpAdded",
            ]

            clock.advance(200)
            await worker.apply_config(_config(config_version=3, window_seconds=120))

            assert worker.config.window_seconds == 120
            # The bucket at BASE is 200 s old: outside a 120 s window.
            assert window.total(IP_A) == 0
            assert window.total(IP_B) == 0
            demotions = _decoded(bus)[2:]
            assert len(demotions) == 2
            for _key, envelope in demotions:
                assert envelope.event_type == "HotIpRemoved"
                assert envelope.config_version == 3
                payload = envelope.payload
                assert isinstance(payload, HotIpRemoved)
                assert payload.window_count == 0
            assert window.hot_count == 0
            assert (await state_store.load(0)).hot_ips == frozenset()


class TestTheWarmUpExemptionDuringAConfigPass:
    """Decision 7 step 2: "The warm-up exemption of decision 5 still applies"
    -- an inherited IP the new owner has not yet counted must not be demoted
    by a configuration pass either."""

    async def test_an_inherited_ip_is_not_demoted_by_the_pass(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        async with _running(_worker(bus=bus, clock=clock, state_store=state_store)) as worker:
            window = _window_of(worker)
            assert window.in_warmup is True
            assert window.total(IP_A) == 0

            await worker.apply_config(_config(config_version=2))

            assert _records(bus) == []
            assert window.state(IP_A) is IpState.HOT
            assert worker.config.config_version == 2

    async def test_a_self_promoted_ip_is_still_demoted_by_the_pass(self) -> None:
        # The exemption is for inherited IPs only, even while the window as a
        # whole is warming up (decision 5 / assumption 5).
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        async with _running(_worker(bus=bus, clock=clock, state_store=state_store)) as worker:
            window = _window_of(worker)
            assert window.observe(OTHER_IP, BASE, 1200) is not None
            await worker.apply_config(_config(config_version=2))
            assert [envelope.subject for _key, envelope in _decoded(bus)] == [str(OTHER_IP)]

            # v3 puts 1200 below the cold threshold, so the self-promoted IP
            # is demoted while the inherited one is still exempt.
            await worker.apply_config(
                _config(config_version=3, hot_threshold=5000, cold_threshold=2000)
            )

            decoded = _decoded(bus)
            assert [envelope.event_type for _key, envelope in decoded] == [
                "HotIpAdded",
                "HotIpRemoved",
            ]
            assert decoded[1][1].subject == str(OTHER_IP)
            assert window.state(IP_A) is IpState.HOT
            assert window.is_inherited(IP_A) is True


class TestReevaluateShardDirectly:
    """The function decision 9 names, exercised without the worker."""

    def _window(self, clock: ManualClock, config: DetectionConfig = V1) -> ShardWindow:
        return ShardWindow(shard=0, config=config, clock=clock)

    async def test_it_returns_the_number_of_transitions_it_emitted(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = self._window(clock, V2)
        _seed(window, IPS, 600)

        transitions = await reevaluate_shard(window, emitter, config=V2)

        assert transitions == 32
        assert len(_records(bus)) == 32

    async def test_a_second_pass_under_the_same_config_changes_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = self._window(clock, V2)
        _seed(window, IPS, 600)
        await reevaluate_shard(window, emitter, config=V2)

        transitions = await reevaluate_shard(window, emitter, config=V2)

        assert transitions == 0
        assert len(_records(bus)) == 32

    async def test_an_explicit_ip_list_restricts_the_pass(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = self._window(clock, V2)
        _seed(window, IPS, 600)

        transitions = await reevaluate_shard(window, emitter, config=V2, ips=[IP_A, IP_B])

        assert transitions == 2
        assert {envelope.subject for _key, envelope in _decoded(bus)} == {str(IP_A), str(IP_B)}
        assert window.hot_count == 2

    async def test_the_batch_size_cannot_change_the_outcome(self) -> None:
        # The yield cadence is an event-loop courtesy (decision 7), not a
        # semantic knob: 32 IPs in batches of 1 must produce exactly what one
        # batch of 1000 produces.
        clock = ManualClock(initial=BASE)
        one_at_a_time = InMemoryBus()
        all_at_once = InMemoryBus()
        small_window = self._window(clock, V2)
        large_window = self._window(clock, V2)
        _seed(small_window, IPS, 600)
        _seed(large_window, IPS, 600)

        small = await reevaluate_shard(
            small_window, _emitter(bus=one_at_a_time, clock=clock), config=V2, batch_size=1
        )
        large = await reevaluate_shard(
            large_window, _emitter(bus=all_at_once, clock=clock), config=V2, batch_size=1000
        )

        assert small == large == 32
        assert small_window.hot_ips() == large_window.hot_ips() == frozenset(IPS)
        assert len(_records(one_at_a_time)) == len(_records(all_at_once)) == 32

    async def test_the_pass_evaluates_in_both_directions(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        emitter = _emitter(bus=bus, clock=clock)
        window = self._window(clock, V1)
        assert window.observe(IP_A, BASE, 1200) is not None
        assert window.observe(IP_B, BASE, 600) is not None
        window.set_state(IP_B, IpState.HOT)

        transitions = await reevaluate_shard(window, emitter, config=V1)

        assert transitions == 2
        by_subject = {envelope.subject: envelope.event_type for _key, envelope in _decoded(bus)}
        assert by_subject == {str(IP_A): "HotIpAdded", str(IP_B): "HotIpRemoved"}
