"""A configuration change re-evaluates every tracked IP of every owned shard.

Spec: section 34 (a change MUST define its effect on existing state, and
re-evaluation is controlled), section 38 (hysteresis is re-applied under the
new thresholds), section 46.4 (`weight` is computed under the configuration
in force at the transition), section 47.3 (the new version is visible only
after the re-evaluation it triggered has been applied). ADR-0009 decision 6
and Amendment item A5 (strictly greater version; rejected documents ignored),
ADR-0011 decision 7 (the pass), decision 2's `apply_config` (geometry),
decision 5 (the warm-up exemption still applies) and Amendment 3 item A12
(the version gate lives in `ConfigPoller.poll_once()` alone; the worker's
`apply_config` is unconditional).

The interface under test is ADR-0011 decision 9, which pins one signature
exactly:

    reevaluate_shard(window, emitter, *, config, ips=None, batch_size=1000) -> int

and decision 7's three steps, which the worker performs under its lock:
`window.apply_config(v)` per claimed shard, then
`emitter.evaluate(window, ip, config=v, reason="config")` for each IP in a
snapshot of `window.tracked_ips()`, then adopt `v`.

ASSUMPTIONS -- things decisions 7 and 9 do not pin. Adjust the helpers below,
not the meaning of the assertions:

1. Ruled rather than assumed since Amendment 3 item A12, and kept here
   because the rest of the list is numbered against it:
   `await worker.apply_config(config) -> None` takes an already-validated
   `DetectionConfig` (the loader/`ConfigPoller` is what turns a document into
   one, ADR-0009 A5) and applies it *unconditionally* -- it compares no
   versions and returns nothing, so calling it with the version already in
   force simply runs the pass again. The version gate (a `config_version`
   that is not strictly greater is ignored, silently) belongs to
   `ConfigPoller.poll_once()` and to nothing else, and `apply_config` is the
   `Callable[[DetectionConfig], Awaitable[None]]` hook that poller calls.
   `AggregatorService.reload_config()` is one `poll_once()` of that poller
   and returns the poller's `current`, so the gate is asserted here through a
   poller wired the way `reload_config()` wires one (`TestTheVersionGate`),
   and the absence of a second gate inside `apply_config` is asserted by
   calling the worker's method directly with a version that is equal to, and
   then lower than, the one in force (`TestApplyConfigComparesNoVersions`).
2. `reevaluate_shard`'s `int` return is the number of transitions the pass
   emitted -- the `transitions` field of the worker's own
   `config_reevaluated` log record (decision 8, renamed by A12). It is *not*
   `config_applied`: after A12 that name is the poller's record, and
   ADR-0009 A7 gives it `path`, `config_version` and
   `previous_config_version` only.
3. `AggregatorWorker(*, bus, state_store, clock, config, metrics,
   shard_ids=None, max_tracked_ips=..., member_id="aggregator",
   lease_ttl_s=30.0)`, `start()`/`stop()`, `worker.window(shard)`,
   `worker.config`. Same assumption as `test_sharding.py` and
   `test_worker.py`. `shard_ids=None` is "every partition as the bus defines
   it" -- `{0}` on `InMemoryBus` (ADR-0013 decision 6; it is no longer
   `auto`, which ADR-0013 removed); the two lease keywords keep their
   defaults here because nothing in this file runs two members.
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

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
from hammertime.core.runtime import ConfigPoller
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

# The same configuration in document form: what an operator publishes and what
# `ConfigPoller` reads (`schemas/detection_config.v1.json`,
# `config/detection.v1.json`). The shape is the one
# `packages/hammertime-core/.../tests/test_runtime.py::_write_config` writes.
_DOCUMENT: dict[str, Any] = {
    "config_version": 1,
    "window_seconds": WINDOW_SECONDS,
    "bucket_seconds": BUCKET_SECONDS,
    "hot_threshold": 1000,
    "cold_threshold": 800,
    "minimum_hot_ips": 16,
    "minimum_hot_ratio": 0.10,
    "allowed_lateness_seconds": 30,
    "state_retention_seconds": 600,
    "weight_max": 1_000_000,
}


def _write_config(path: Path, **overrides: Any) -> Path:
    path.write_text(json.dumps({**_DOCUMENT, **overrides}))
    return path


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


def _poller(path: Path, worker: AggregatorWorker) -> ConfigPoller:
    """The wiring `AggregatorService.reload_config()` performs (A12).

    `worker.apply_config` is the poller's unconditional `apply` hook, and this
    line is also where A12's signature is checked statically: mypy runs strict
    over these tests, and a hook returning anything but `None` is not
    assignable to `Callable[[DetectionConfig], Awaitable[None]]` (`Awaitable`
    is covariant), so an `apply_config` that still returned the configuration
    in force would fail type-checking here. The version in force at the poller
    is the one in force at the worker.
    """

    return ConfigPoller(path, current=worker.config, apply=worker.apply_config)


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

            # A12: `apply_config` returns nothing, so the version in force is
            # read off the worker. The `-> None` half of that is checked
            # statically instead of here -- binding the result of a call that
            # returns `None` is itself a mypy error, and `_poller` pins the
            # hook type (see the comment there).
            await worker.apply_config(V2)

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
    """Section 47.3 / ADR-0009 decision 6 and A5: only a strictly greater
    `config_version` is applied.

    The gate is `ConfigPoller.poll_once()`'s and nobody else's (A12), so the
    three tests that exercise it -- the lower version, the version already in
    force, and the strictly greater one -- put the candidate on disk and poll
    for it through `_poller`, which is the wiring
    `AggregatorService.reload_config()` performs. `apply_config` appears in
    those three only as setup (putting a version in force before the poll) and
    as the hook the poller itself calls. The fourth test,
    `test_a_descriptive_only_change_is_adopted_and_produces_no_transitions`,
    is about what the pass does once the gate has let a document through, not
    about the gate, so it calls `apply_config` directly.
    """

    async def test_a_lower_version_is_ignored(self, tmp_path: Path) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)
            await worker.apply_config(V3)
            before = len(_records(bus))
            path = _write_config(
                tmp_path / "detection.json",
                config_version=2,
                hot_threshold=500,
                cold_threshold=400,
            )
            poller = _poller(path, worker)

            current = await poller.poll_once()

            assert current.config_version == 3
            assert current.hot_threshold == 1000
            assert worker.config.config_version == 3
            assert worker.config.hot_threshold == 1000
            assert len(_records(bus)) == before
            assert _window_of(worker).hot_count == 0

    async def test_the_version_in_force_is_ignored_even_with_different_thresholds(
        self, tmp_path: Path
    ) -> None:
        # Section 4 step 4 of the integration scenario: the same
        # `config_version` carrying thresholds that *would* promote every
        # tracked IP must have no effect at all. The scenario publishes it and
        # calls `reload_config()`, which is the poll performed here.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)
            path = _write_config(
                tmp_path / "detection.json",
                config_version=1,
                hot_threshold=100,
                cold_threshold=50,
            )
            poller = _poller(path, worker)

            current = await poller.poll_once()

            assert current.config_version == 1
            assert current.hot_threshold == 1000
            assert worker.config.config_version == 1
            assert worker.config.hot_threshold == 1000
            assert _records(bus) == []
            assert _window_of(worker).hot_count == 0

    async def test_a_strictly_greater_version_reaches_the_worker(self, tmp_path: Path) -> None:
        # The open side of the same gate: the poller calls the hook, the pass
        # runs under the new thresholds, and both the poller's `current` and
        # the worker's `config` move to the new document.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            _seed(_window_of(worker), IPS, 600)
            path = _write_config(
                tmp_path / "detection.json",
                config_version=2,
                hot_threshold=500,
                cold_threshold=400,
            )
            poller = _poller(path, worker)

            current = await poller.poll_once()

            assert current.config_version == 2
            assert current.hot_threshold == 500
            assert worker.config.config_version == 2
            assert worker.config.hot_threshold == 500
            assert len(_records(bus)) == 32
            assert _window_of(worker).hot_count == 32

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

            await worker.apply_config(v4)

            assert worker.config.config_version == 4
            assert worker.config.weight_max == 5000
            assert _records(bus) == []
            assert _window_of(worker).total(IP_A) == 600


class TestApplyConfigComparesNoVersions:
    """A12, the other half: `AggregatorWorker.apply_config` is *unconditional*.

    `TestTheVersionGate` above covers the gate where A12 puts it --
    `ConfigPoller.poll_once()`. These tests cover the seam A12 says has no gate
    at all: the worker's own method "compares no versions", so a direct caller
    (a test, a tool) passing the version already in force, or a strictly lower
    one, gets it applied -- thresholds adopted at the worker and the
    re-evaluation pass run. Nothing here goes through a poller.

    Each test would fail against an `apply_config` that kept a version
    comparison of its own: a gated implementation would skip the pass, leaving
    the bus empty and `worker.config` where it was, and every assertion below
    about emitted transitions and about the version/thresholds in force
    afterwards is written to catch exactly that.
    """

    async def test_reapplying_the_version_in_force_runs_the_pass_again(self) -> None:
        # A12's assumption: "a direct caller re-applying the version in force is
        # a useful way to force a full re-evaluation pass (nothing else exposes
        # one)". `observe` moves counts without evaluating hysteresis, so 1200
        # against v1's `hot_threshold` of 1000 is a promotion that only the
        # forced pass can make.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        async with _running(_worker(bus=bus, clock=clock, state_store=state_store)) as worker:
            window = _window_of(worker)
            assert window.observe(IP_A, BASE, 1200) is not None
            assert window.hot_count == 0
            assert _records(bus) == []
            assert worker.config.config_version == 1

            # The exact `DetectionConfig` already in force, applied again.
            await worker.apply_config(V1)

            decoded = _decoded(bus)
            assert [envelope.event_type for _key, envelope in decoded] == ["HotIpAdded"]
            assert decoded[0][1].subject == str(IP_A)
            assert decoded[0][1].config_version == 1
            assert window.hot_count == 1
            assert (await state_store.load(0)).hot_ips == frozenset({IP_A})
            assert worker.config.config_version == 1

    async def test_reapplying_the_version_in_force_demotes_drifted_state(self) -> None:
        # The other direction of the same forced pass: `set_state` marks IP_B
        # HOT without emitting anything, and 600 is below v1's `cold_threshold`
        # of 800, so re-applying v1 demotes it.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            window = _window_of(worker)
            assert window.observe(IP_B, BASE, 600) is not None
            window.set_state(IP_B, IpState.HOT)
            assert window.hot_count == 1
            assert _records(bus) == []

            await worker.apply_config(V1)

            decoded = _decoded(bus)
            assert [envelope.event_type for _key, envelope in decoded] == ["HotIpRemoved"]
            assert decoded[0][1].subject == str(IP_B)
            assert decoded[0][1].config_version == 1
            assert window.hot_count == 0
            assert worker.config.config_version == 1

    async def test_a_strictly_lower_version_is_applied_at_the_worker(self) -> None:
        # The inverse of `test_a_lower_version_is_ignored` above: the same
        # v3 -> v2 step, handed to the worker instead of published for the
        # poller. The gate is the poller's, so here v2 takes effect -- its
        # thresholds become the ones in force and the pass promotes under them.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore()
        async with _running(_worker(bus=bus, clock=clock, state_store=state_store)) as worker:
            window = _window_of(worker)
            _seed(window, IPS, 600)
            await worker.apply_config(V3)
            assert worker.config.config_version == 3
            # v3 restates v1's thresholds, so putting it in force emits nothing.
            assert _records(bus) == []

            await worker.apply_config(V2)

            assert worker.config.config_version == 2
            assert worker.config.hot_threshold == 500
            assert window.config.hot_threshold == 500
            decoded = _decoded(bus)
            assert len(decoded) == 32
            assert {envelope.subject for _key, envelope in decoded} == {str(ip) for ip in IPS}
            for _key, envelope in decoded:
                assert envelope.event_type == "HotIpAdded"
                assert envelope.config_version == 2
            assert window.hot_count == 32
            assert (await state_store.load(0)).hot_ips == frozenset(IPS)

    async def test_the_same_version_with_new_thresholds_is_applied_at_the_worker(self) -> None:
        # The worker-level inverse of the equal-version test above: through the
        # poller that document is dropped, but handed straight to
        # `apply_config` its thresholds become the ones in force and the pass
        # runs under them.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        async with _running(_worker(bus=bus, clock=clock)) as worker:
            window = _window_of(worker)
            _seed(window, IPS, 600)

            await worker.apply_config(
                _config(config_version=1, hot_threshold=100, cold_threshold=50)
            )

            assert worker.config.config_version == 1
            assert worker.config.hot_threshold == 100
            assert window.config.hot_threshold == 100
            decoded = _decoded(bus)
            assert len(decoded) == 32
            for _key, envelope in decoded:
                assert envelope.event_type == "HotIpAdded"
                assert envelope.config_version == 1
            assert window.hot_count == 32


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
