"""`AggregatorService.run()` reports what stopped it; `stop()` is idempotent.

Spec: section 47.1 (the lifecycle), section 47.4 (drain), section 47.5 (exit
status 1 for `run_exited`). ADR-0009 decision 3 (the `Service` protocol:
`stop()` is "idempotent; safe before start()"), decision 5 step 7 ("`run()`
returning on its own is a failure ...: `ERROR event=run_exited` with the
exception if any -> exit 1"), decision 7 (drain). ADR-0013 decision 8 as
amended 2026-09-22 (Amendment 4 rulings R4 and R7), written from that text
alone; `service.py` was not read:

* "`AggregatorService.run()` reports what stopped it (amended 2026-09-22,
  Amendment 4 ruling R7; ADR-0009 decision 5 step 7): when the first of its
  four tasks completes, `run()` collects the exceptions of the completed
  tasks first, then calls `stop()` inside a `try` that appends a raised
  exception to the same list and logs `WARNING stop_failed error=<str>`
  through the service's logger, then gathers the remaining tasks, closes the
  clients (the bus first) and raises the first collected exception.
  `run_exited` therefore names the failure that stopped the service; a
  `stop()` that fails on the same outage ... is reported only when nothing
  failed before it." (`TestRunReportsWhatStoppedIt`.)
* "`AggregatorService.stop()` is idempotent by the same token, its other
  steps (readiness, poller, server) being so already." (Ruling R4;
  `TestStopIsIdempotent`.)

ADR-0013 decision 7 as amended 2026-09-22 (Amendment 6 ruling 2) adds the
wait this file's last two classes are written against:

* "`AggregatorService.start()` calls `worker.start()` through
  `connect_with_retry("shard_leases", self._worker.start,
  transient=(ShardHeldBySameMemberError,), sleep=..., monotonic=...)`:
  ADR-0009 A1's schedule (0.5 s doubling to a 5 s cap) under the startup
  deadline `HAMMERTIME_STARTUP_TIMEOUT_S` (60 s) ... A dead predecessor's
  leases lapse inside the deadline whenever `lease_ttl_s` is below the
  startup timeout ...; the replacement then claims, logs `shard_claimed`,
  and becomes ready without a process exit. A live twin never lets go: the
  deadline expires, the last `ShardHeldBySameMemberError` is re-raised ... A
  refusal by another member is not in the transient tuple and exits 1 at
  once, as today. `AggregatorService.__init__` gains `sleep` and `monotonic`
  keywords (defaults `asyncio.sleep`, `time.monotonic`) passed to every
  `connect_with_retry` it makes, so a test drives the wait without wall
  time." (`TestStartWaitsOutAHolderOfTheSameMember`.)
* "`startup_fields()` gains `instance_id`, so a process's `starting` record
  can be matched against the token in another process's
  `shard_held_by_same_member` or `shard_lease_lost` record" (ruling 2(e),
  assumption 103). (`TestStartupFields`.)

ASSUMPTIONS -- what the ADRs do not pin; adjust the helpers, not the meaning
of the assertions:

1. **The constructor.** `AggregatorService(settings, worker, detection_config)`
   with no transport or store client -- the shape the dispatch brief gave;
   no ADR text this file may cite pins the constructor's parameters (ADR-0013
   decision 8 only says the service "loses `_KafkaBus` and
   `_TRANSIENT_BUS_ERRORS` in favour of `NatsBus` and `TRANSIENT_ERRORS`"),
   and `build_service` (ADR-0009 decision 3) is the composition root the
   other tests go through. `_service()` is the one place this is spelled.
2. **`AggregatorSettings`' fields** are the fifteen ADR-0013 decision 10
   names "for the record": `bus_kind`, `bus_brokers`, `shard_ids`,
   `member_id`, `lease_ttl_s`, `host`, `port`, `detection_config_path`,
   `store_kind`, `redis_url`, `maintenance_interval_s`,
   `config_poll_interval_s`, `commit_interval_s`, `max_tracked_ips`,
   `reevaluation_batch`. Every one is passed, so no default of the class is
   relied on; the values are the documented defaults except `port=0` (an
   ephemeral bind), `bus_kind`/`store_kind` `memory`, the two intervals at an
   hour (so neither periodic loop fires during a test) and a `lease_ttl_s`
   above the maintenance interval (decision 7's "MUST exceed").
3. **`start()` is awaited before `run()`**, as `run_service` does (ADR-0009
   decision 5 steps 5 and 7); with no clients to connect it is the worker
   double's `start()` and the readiness flip. `run()` is bounded by
   `asyncio.wait_for(..., timeout=10.0)` -- a wall-clock bound reached only
   when the test fails (a `run()` that never returns), as
   `test_memory_bus.py`'s assumption 5 has it.
4. **The worker double** exposes what a worker exposes to its service:
   `start()`, `run()`, `stop()`, `run_maintenance()`, `apply_config()`, and
   `config` (the poller's `current`, ADR-0011 decision 9). Nothing else is
   read by the paths under test; if the service reads more, that is a seam
   to report, not to guess at.
5. **The port.** `port=0` is assumed to make uvicorn bind an ephemeral port
   on `127.0.0.1`; the brief says to report, not skip, if it cannot.
6. **How many times `worker.stop()` is called is not pinned.** `run()`
   itself calls `stop()` (ruling R7) and the test calls it twice more; what
   is pinned is that every call returns normally and `run()` returns
   cleanly. The double counts calls so that "at least once" is observable.
7. The `stop_failed` record itself is not asserted (assumption 71: "it is
   not pinned by a test"), for the reason every aggregator test file gives
   for log records. Neither are `dependency_unavailable
   dependency=shard_leases` and `shard_held_by_same_member`, the two records
   Amendment 6's wait emits: same reason, and the wait's observables are the
   recorded sleeps, the exception and `ready`.
8. **The lease tests use a real `AggregatorWorker`**, on an `InMemoryBus`
   (partition `{0}`) and a `MemoryShardStateStore` over a `ManualClock`, not
   `_WorkerDouble`: "the worker holds the shard" and "each attempt a fresh
   `subscribe()`" are the worker's own behaviour, and a double asserting them
   would be asserting itself. Its keywords are `test_worker.py`'s
   ASSUMPTION 1, with `member_id` set to the settings' own so the two agree
   as `build_service` makes them agree.
9. **The startup deadline is set through the environment.** ADR-0009 A1:
   `connect_with_retry`'s `timeout_s=None` "reads `os.environ`", so
   `HAMMERTIME_STARTUP_TIMEOUT_S` is monkeypatched to a few seconds. Nothing
   waits on it in wall time -- the injected `monotonic` is what the deadline
   is measured against -- and the assertions on the schedule are written to
   hold for the default 60 s too, so a service that passes its own
   `timeout_s` instead does not silently fail them.
10. **`_FakeWait` advances the store's clock by the delay it is asked to
    sleep.** That is how a lease TTL passes: the retry schedule is the only
    thing that moves time in these tests. `ManualClock` is integer-valued
    (`packages/hammertime-store/.../test_shard_state.py`), so the advance is
    the delay rounded up -- which can only make the predecessor's lease lapse
    sooner, never later, so "at least one retry" stays a real assertion.
11. **A twin is modelled by a store that never grants.** Ruling 2(b)'s live
    twin "keeps renewing", so its holder never lapses; `_TwinHoldsEveryLease`
    answers every `acquire_lease` with the twin's token and counts the
    attempts, which is what makes "each attempt is a full `subscribe()`"
    observable.

The detection document on disk is the one `test_reevaluate.py` writes
(`_write_config`); the `DetectionConfig` handed to the service is the same
values as an object.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, cast

import pytest
from hammertime.aggregator.config import AggregatorSettings
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.service import AggregatorService
from hammertime.aggregator.sharding.assignment import (
    ShardHeldBySameMemberError,
    ShardOwnedElsewhereError,
)
from hammertime.aggregator.worker import AggregatorWorker
from hammertime.bus.memory import InMemoryBus
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.runtime import Service
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import ManualClock
from hammertime.store.interface import ShardState, ShardStateStore
from hammertime.store.memory import MemoryShardStateStore

WINDOW_SECONDS = 300
BUCKET_SECONDS = 10

# The `T0` of `docs/spec/integration-scenarios.md` section 2, as every other
# aggregator test file uses it.
BASE = 1_800_000_000

# ADR-0013 Amendment 6 ruling 2(a): one member id, two per-process tokens --
# this process's and the one a predecessor (or a live twin) left in the store.
MEMBER_ID = "m"
INSTANCE_SELF = "instance-self"
INSTANCE_OTHER = "instance-other"
OWNER_SELF = f"{MEMBER_ID}/{INSTANCE_SELF}"
OWNER_OTHER = f"{MEMBER_ID}/{INSTANCE_OTHER}"
ANOTHER_MEMBER = "aggregator-elsewhere"

# Ruling 2(c): "A dead predecessor's leases lapse inside the deadline whenever
# `lease_ttl_s` is below the startup timeout (30 s against 60 s by default)" --
# the same relation, scaled to the retry schedule this test drives by hand.
WORKER_LEASE_TTL_S = 2.0
STARTUP_TIMEOUT_S = 5.0

# ADR-0013 decision 7's default TTL exceeds decision 9's default maintenance
# interval; with both intervals raised to an hour the TTL is raised with them.
AN_HOUR = 3600.0
LEASE_TTL_SECONDS = 2 * AN_HOUR

# The shape `packages/hammertime-core/.../tests/test_runtime.py::_write_config`
# and `test_reevaluate.py::_DOCUMENT` write (`schemas/detection_config.v1.json`).
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

DETECTION_CONFIG = DetectionConfig(
    config_version=1,
    window_seconds=WINDOW_SECONDS,
    bucket_seconds=BUCKET_SECONDS,
    hot_threshold=1000,
    cold_threshold=800,
    allowed_lateness_seconds=30,
    state_retention_seconds=600,
    weight_max=1_000_000,
)


def _write_config(path: Path) -> Path:
    path.write_text(json.dumps(_DOCUMENT))
    return path


def _settings(detection_config_path: Path) -> AggregatorSettings:
    """ASSUMPTION 2: every decision 10 field, spelled out."""

    return AggregatorSettings(
        host="127.0.0.1",
        port=0,
        detection_config_path=detection_config_path,
        bus_kind="memory",
        bus_brokers="nats://localhost:4222",
        store_kind="memory",
        redis_url="redis://localhost:6379/0",
        shard_ids=frozenset({0}),
        member_id=MEMBER_ID,
        lease_ttl_s=LEASE_TTL_SECONDS,
        maintenance_interval_s=AN_HOUR,
        config_poll_interval_s=AN_HOUR,
        commit_interval_s=1.0,
        max_tracked_ips=1_000_000,
        reevaluation_batch=1000,
    )


class _WorkerDouble:
    """ASSUMPTION 4: the worker surface a service reaches, with no bus and no store.

    `run()` waits until `stop()` has been called and then returns -- the
    shape of a consume loop whose stop signal arrived -- and `stop()` counts
    its calls; subclasses change one of the two.
    """

    def __init__(self, config: DetectionConfig) -> None:
        self.config = config
        self.starts = 0
        self.stops = 0
        self.runs = 0
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        self.starts += 1

    async def run(self) -> None:
        self.runs += 1
        await self._stopped.wait()

    async def stop(self) -> None:
        self.stops += 1
        self._stopped.set()

    async def run_maintenance(self) -> None:
        return None

    async def apply_config(self, config: DetectionConfig) -> None:
        self.config = config


class _CrashingWorker(_WorkerDouble):
    """`run()` fails after one tick -- the consume loop's failure ruling R7 names
    (a `TimeoutError` from `ack`, decision 3) -- and `stop()` fails too, the way a
    final `ack_sync` on the same outage would."""

    async def run(self) -> None:
        self.runs += 1
        await asyncio.sleep(0)
        raise RuntimeError("consume failed")

    async def stop(self) -> None:
        self.stops += 1
        self._stopped.set()
        raise ValueError("stop failed")


class _FakeWait:
    """ADR-0009 A2 / ADR-0013 Amendment 6 ruling 2(c): the `sleep` and
    `monotonic` seams, driven by one recorded elapsed time.

    Every delay the retry schedule asks for is recorded, added to the injected
    `monotonic` -- so the startup deadline arrives without wall time -- and,
    when a clock is given, applied to it as well, so a lease TTL passes
    exactly as the schedule spends it (ASSUMPTION 10).
    """

    def __init__(self, clock: ManualClock | None = None) -> None:
        self.delays: list[float] = []
        self.elapsed = 0.0
        self._clock = clock

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.elapsed += delay
        if self._clock is not None:
            self._clock.advance(math.ceil(delay))
        await asyncio.sleep(0)

    def monotonic(self) -> float:
        return self.elapsed


class _TwinHoldsEveryLease:
    """ASSUMPTION 11: a `ShardStateStore` that never grants a lease, because
    the holder is "a live twin, which keeps renewing" (ruling 2(b))."""

    def __init__(self, clock: ManualClock, holder: str) -> None:
        self._inner = MemoryShardStateStore(clock=clock)
        self._holder = holder
        self.attempts = 0

    async def load(self, shard: int) -> ShardState:
        return await self._inner.load(shard)

    async def record_transition(
        self, shard: int, ip: Address, state: IpState, sequence: int
    ) -> None:
        await self._inner.record_transition(shard, ip, state, sequence)

    async def acquire_lease(self, shard: int, owner: str, ttl_seconds: float) -> str | None:
        self.attempts += 1
        return self._holder

    async def release_lease(self, shard: int, owner: str) -> None:
        return None


def _service(tmp_path: Path, worker: _WorkerDouble) -> AggregatorService:
    """ASSUMPTION 1: the constructor, spelled in one place."""

    path = _write_config(tmp_path / "detection.json")
    # The double stays a double; the cast only satisfies the constructor's annotation.
    return AggregatorService(_settings(path), cast(AggregatorWorker, worker), DETECTION_CONFIG)


def _aggregator_worker(
    *,
    clock: ManualClock,
    state_store: ShardStateStore,
    instance_id: str | None = INSTANCE_SELF,
    lease_ttl_s: float = WORKER_LEASE_TTL_S,
) -> AggregatorWorker:
    """ASSUMPTION 8: a real worker, whose claims take the real leases."""

    return AggregatorWorker(
        bus=InMemoryBus(),
        state_store=state_store,
        clock=clock,
        config=DETECTION_CONFIG,
        metrics=AggregatorMetrics(),
        shard_ids=None,
        member_id=MEMBER_ID,
        lease_ttl_s=lease_ttl_s,
        instance_id=instance_id,
    )


def _service_with_wait(
    tmp_path: Path, worker: AggregatorWorker, wait: _FakeWait
) -> AggregatorService:
    """The constructor with Amendment 6's two seams: "`sleep` and `monotonic`
    keywords ... passed to every `connect_with_retry` it makes"."""

    path = _write_config(tmp_path / "detection.json")
    return AggregatorService(
        _settings(path),
        worker,
        DETECTION_CONFIG,
        sleep=wait.sleep,
        monotonic=wait.monotonic,
    )


async def _run_bounded(service: AggregatorService) -> None:
    """ASSUMPTION 3: `run()` under a bound that only a hung `run()` reaches."""

    await asyncio.wait_for(service.run(), timeout=10.0)


class TestTheServiceProtocol:
    def test_it_satisfies_the_service_protocol(self, tmp_path: Path) -> None:
        # ADR-0009 decision 3 / A3: `Service` is `@runtime_checkable`.
        service = _service(tmp_path, _WorkerDouble(DETECTION_CONFIG))

        assert isinstance(service, Service)


class TestRunReportsWhatStoppedIt:
    """Ruling R7: "`run_exited` therefore names the failure that stopped the service;
    a `stop()` that fails on the same outage ... is reported only when nothing
    failed before it"."""

    async def test_the_consume_loops_failure_is_raised_not_the_failing_stop(
        self, tmp_path: Path
    ) -> None:
        worker = _CrashingWorker(DETECTION_CONFIG)
        service = _service(tmp_path, worker)
        await service.start()

        with pytest.raises(RuntimeError, match="consume failed") as excinfo:
            await _run_bounded(service)

        # "raises the first collected exception": the `RuntimeError` from the
        # task that completed first, not the `ValueError` `stop()` raised on
        # the way down.
        assert not isinstance(excinfo.value, ValueError)
        assert worker.runs == 1
        # `stop()` was called and failed, and that did not mask the cause.
        assert worker.stops >= 1

    async def test_the_failing_stop_does_not_escape_run(self, tmp_path: Path) -> None:
        # "calls `stop()` inside a `try` that appends a raised exception to the
        # same list": the `ValueError` is collected, not propagated, so
        # `run()` ends on the `RuntimeError` and nothing else.
        worker = _CrashingWorker(DETECTION_CONFIG)
        service = _service(tmp_path, worker)
        await service.start()

        try:
            await _run_bounded(service)
        except RuntimeError:
            pass
        except ValueError as exc:  # pragma: no cover - the defect R7 closes
            raise AssertionError(f"stop()'s failure escaped run(): {exc!r}") from exc
        else:  # pragma: no cover - the defect ADR-0009 decision 5 step 7 names
            raise AssertionError("run() returned normally after the consume loop failed")

    async def test_a_failing_stop_is_reported_when_nothing_failed_before_it(
        self, tmp_path: Path
    ) -> None:
        # "reported only when nothing failed before it": the consume loop
        # returns on its own without an exception (ADR-0009 decision 5 step
        # 7's "a service that has nothing left to do has crashed"), so the
        # first task completes with nothing to collect, `stop()`'s
        # `ValueError` is the only entry in the list, and `run()` raises it
        # rather than swallowing it.
        class _ReturnsThenStopFails(_WorkerDouble):
            async def run(self) -> None:
                self.runs += 1
                await asyncio.sleep(0)

            async def stop(self) -> None:
                self.stops += 1
                self._stopped.set()
                raise ValueError("stop failed")

        worker = _ReturnsThenStopFails(DETECTION_CONFIG)
        service = _service(tmp_path, worker)
        await service.start()

        with pytest.raises(ValueError, match="stop failed"):
            await _run_bounded(service)

        assert worker.runs == 1
        assert worker.stops >= 1


class TestStopIsIdempotent:
    """Ruling R4: "`AggregatorService.stop()` is idempotent by the same token, its
    other steps (readiness, poller, server) being so already"; ADR-0009
    decision 3: `stop()` is "idempotent; safe before start()"."""

    async def test_stop_twice_then_run_returns_cleanly(self, tmp_path: Path) -> None:
        worker = _WorkerDouble(DETECTION_CONFIG)
        service = _service(tmp_path, worker)
        await service.start()
        task = asyncio.create_task(_run_bounded(service))
        for _ in range(50):  # let `run()` start its tasks
            await asyncio.sleep(0)

        await service.stop()
        await service.stop()

        # `run()` returns cleanly: `await task` raising would fail the test.
        await task
        assert task.exception() is None
        assert worker.stops >= 1

    async def test_stop_before_start_is_safe(self, tmp_path: Path) -> None:
        # ADR-0009 decision 3: "safe before start()".
        worker = _WorkerDouble(DETECTION_CONFIG)
        service = _service(tmp_path, worker)

        await service.stop()
        await service.stop()

    async def test_ready_is_false_after_stop(self, tmp_path: Path) -> None:
        # ADR-0009 decision 4: "`ready` becomes `True` at the end of `start()`
        # and `False` once `stop()` is called" -- and a second `stop()` leaves
        # it there.
        worker = _WorkerDouble(DETECTION_CONFIG)
        service = _service(tmp_path, worker)
        await service.start()
        assert service.ready is True

        await service.stop()
        assert service.ready is False
        await service.stop()

        assert service.ready is False


class TestStartWaitsOutAHolderOfTheSameMember:
    """ADR-0013 decision 7 as amended (Amendment 6 ruling 2(c)): `start()`
    retries `worker.start()` on `ShardHeldBySameMemberError` only, on ADR-0009
    A1's schedule, under the startup deadline; a dead predecessor's lease is
    taken the moment it lapses, a live twin's never is, and another member's
    holder is not retried at all.

    Nothing here waits on wall time: every delay goes through the injected
    `sleep`, which is also what moves the store's clock (ASSUMPTION 10).
    """

    async def test_a_dead_predecessors_lease_is_waited_out_and_then_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "a dead predecessor's leases, lapsing within `lease_ttl_s`, are
        # taken without the process exiting": `start()` returns, the shard is
        # this process's, and `/readyz` can answer 200.
        monkeypatch.setenv("HAMMERTIME_STARTUP_TIMEOUT_S", str(STARTUP_TIMEOUT_S))
        clock = ManualClock(initial=BASE)
        store = MemoryShardStateStore(clock=clock)
        assert await store.acquire_lease(0, OWNER_OTHER, WORKER_LEASE_TTL_S) is None
        wait = _FakeWait(clock)
        worker = _aggregator_worker(clock=clock, state_store=store)
        service = _service_with_wait(tmp_path, worker, wait)

        await service.start()

        try:
            assert service.ready is True
            assert worker.shards == frozenset({0})
            assert await store.acquire_lease(0, ANOTHER_MEMBER, 1.0) == OWNER_SELF
            # At least one refusal was retried, on A1's schedule.
            assert wait.delays
            assert wait.delays[0] == 0.5
        finally:
            await service.stop()

    async def test_a_live_twin_is_retried_until_the_startup_deadline_and_then_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "a live twin's never lapse, the deadline expires, and the process
        # exits 1 with `start_failed`": what `start()` owes the runner is the
        # last `ShardHeldBySameMemberError` and a service that is not ready.
        monkeypatch.setenv("HAMMERTIME_STARTUP_TIMEOUT_S", str(STARTUP_TIMEOUT_S))
        clock = ManualClock(initial=BASE)
        store = _TwinHoldsEveryLease(clock, OWNER_OTHER)
        wait = _FakeWait(clock)
        worker = _aggregator_worker(clock=clock, state_store=store)
        service = _service_with_wait(tmp_path, worker, wait)

        with pytest.raises(ShardHeldBySameMemberError):
            await service.start()

        try:
            assert service.ready is False
            assert worker.shards == frozenset()
            # ADR-0009 A1: "the sleeps are `0.5, 1, 2, 4, 5, 5, 5, ...`" and
            # "the last attempt is always made *before* the deadline".
            assert wait.delays[:3] == [0.5, 1.0, 2.0]
            # Each attempt is a full `subscribe()`, so each asked the store again.
            assert store.attempts >= len(wait.delays) + 1
        finally:
            await service.stop()

    async def test_another_members_holder_is_refused_without_a_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "A refusal by another member is not in the transient tuple and exits
        # 1 at once, as today": the plain `ShardOwnedElsewhereError`, and not
        # one second of wait.
        monkeypatch.setenv("HAMMERTIME_STARTUP_TIMEOUT_S", str(STARTUP_TIMEOUT_S))
        clock = ManualClock(initial=BASE)
        store = MemoryShardStateStore(clock=clock)
        assert await store.acquire_lease(0, ANOTHER_MEMBER, 3600.0) is None
        wait = _FakeWait(clock)
        worker = _aggregator_worker(clock=clock, state_store=store)
        service = _service_with_wait(tmp_path, worker, wait)

        with pytest.raises(ShardOwnedElsewhereError) as excinfo:
            await service.start()

        try:
            assert not isinstance(excinfo.value, ShardHeldBySameMemberError)
            assert wait.delays == []
            assert service.ready is False
            assert await store.acquire_lease(0, MEMBER_ID, 1.0) == ANOTHER_MEMBER
        finally:
            await service.stop()


class TestStartupFields:
    """ADR-0013 Amendment 6 ruling 2(e): "`startup_fields()` gains
    `instance_id`, so a process's `starting` record can be matched against the
    token in another process's `shard_held_by_same_member` or
    `shard_lease_lost` record" (assumption 103). The record itself is
    `run_service`'s and is not asserted (ASSUMPTION 7); the fields it is built
    from are the service's."""

    def test_the_fields_carry_the_workers_own_instance_id(self, tmp_path: Path) -> None:
        clock = ManualClock(initial=BASE)
        # Built as a shipped process builds it -- "`build_service` passes
        # nothing for it, so every process generates its own".
        worker = _aggregator_worker(
            clock=clock, state_store=MemoryShardStateStore(clock=clock), instance_id=None
        )
        service = _service_with_wait(tmp_path, worker, _FakeWait())

        fields = service.startup_fields()

        assert fields["instance_id"] == worker.instance_id
        assert isinstance(fields["instance_id"], str)
        assert fields["instance_id"] != ""

    def test_member_id_is_still_there(self, tmp_path: Path) -> None:
        # Decision 7's "Failure shape, summarised" as amended: "`instance_id`
        # joins `member_id` in the `starting` record" -- joins, not replaces.
        clock = ManualClock(initial=BASE)
        worker = _aggregator_worker(
            clock=clock, state_store=MemoryShardStateStore(clock=clock), instance_id=None
        )
        service = _service_with_wait(tmp_path, worker, _FakeWait())

        fields = service.startup_fields()

        assert "member_id" in fields
        assert fields["member_id"] == MEMBER_ID
