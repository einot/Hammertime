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
   for log records.

The detection document on disk is the one `test_reevaluate.py` writes
(`_write_config`); the `DetectionConfig` handed to the service is the same
values as an object.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from hammertime.aggregator.config import AggregatorSettings
from hammertime.aggregator.service import AggregatorService
from hammertime.aggregator.worker import AggregatorWorker
from hammertime.core.config.models import DetectionConfig
from hammertime.core.runtime import Service

WINDOW_SECONDS = 300
BUCKET_SECONDS = 10

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
        member_id="m",
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


def _service(tmp_path: Path, worker: _WorkerDouble) -> AggregatorService:
    """ASSUMPTION 1: the constructor, spelled in one place."""

    path = _write_config(tmp_path / "detection.json")
    # The double stays a double; the cast only satisfies the constructor's annotation.
    return AggregatorService(_settings(path), cast(AggregatorWorker, worker), DETECTION_CONFIG)


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
