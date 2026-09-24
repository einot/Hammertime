"""The trie service: composition, admin endpoints, startup replay, reload, run and stop.

Spec: section 33 (replay on start), section 47.1 (entry point and
`config_invalid`), section 47.2 (readiness and the admin endpoints), section
47.3 (configuration changes), section 47.4 (shutdown), section 47.5 (exit
status), section 47.7 (no credential in any record).

Written from ADR-0017 decision 13 ("Lifecycle and shutdown"), decision 4
(`TrieService.start()`: replay, `trie_recovery_seconds`, then ready), decision
10 (`reload_config()` is `poller.poll_once()`, `apply_config` adopts
unconditionally), and ADR-0009 decisions 3-6 and A5 (`build_service`, the
`Service` protocol, the three-valued readiness behind `/readyz`, `ConfigPoller`:
a document is applied only when its version is greater, and a rejected one
leaves the previous version in force). The admin answers are
`docs/protocol/read-api-v1.md`'s table.

From ADR-0017's "Revision 2026-09-23", for the security audit's two findings:

* finding 1: on a log that holds nothing to replay (`end_offset ==
  first_offset`, a JetStream stream never written to or emptied by age or a
  purge), `start()` returns at once and the service is ready, with
  `event_sequence == first` (decision 4 step 3;
  `TestReadyOnALogThatHoldsNothingToReplay`);
* finding 2: the admin app serves no generated API documentation --
  `/openapi.json`, `/docs`, `/docs/oauth2-redirect` and `/redoc` answer 404
  before and after `start()`, while `/healthz`, `/readyz` and `/metrics`
  answer as before (decision 13; read-api-v1.md;
  `TestNoGeneratedApiDocumentation`).

Choices of this file's own:

* The detection document is integration-scenarios section 2.1's, written to
  `tmp_path` (that section: "Written to `tmp_path / "detection.json"` before
  any service is built").
* `bus_kind="memory"` and an injected `InMemoryBus`, as ADR-0009 decision 3
  allows, so no NATS is needed; `config_poll_interval_s` is an hour so the
  poller never fires on its own during a test.
* `asyncio.wait_for(..., 10.0)` is a failure bound only, reached when `run()`
  never returns; waiting for the worker is a bounded `asyncio.sleep(0)` loop.
* `startup_fields()`'s `config_path` and `config_version` are asserted as
  present only; their value types are not pinned by decision 13.
* Readiness after an interrupted replay (decision 4: the service "marks
  itself ready -- unless `stop()` has begun or the worker is not caught up")
  uses `_ScriptedBus`, whose stream yields one message and then waits for
  the test to `feed()` another. The worker is built by the test and handed
  to `TrieService`, so the test can stop the worker alone. `_ScriptedBus`'s
  `first_offset` is `first`, `0` unless a test names it (the memory bus's
  value, ADR-0013 Amendment 10); `_ClosableBus` delegates it to its
  `InMemoryBus`.
* Who closes what (decision 13): `build_service` "passes the `NatsBus` it
  built, if any, as `transport`", and `run()` "closes the transport it
  built". So a `transport=` handed to `TrieService` is started by `start()`
  and closed by `run()`, and a bus injected into `build_service` is never
  closed. The transport is a `_FakeTransport` with `start()` and `close()`,
  and the injected bus is an `InMemoryBus` wrapper with a counting
  `close()`. Whether `stop()` alone, with no `run()`, closes the transport
  is not stated, and is not asserted.

Slice 2, from ADR-0017 Amendment 2 (rulings 4-7, with decision 13 as noted
there) and section 47.2 and 47.4:

* `startup_fields()` gains `min_prefix_lengths`, each served family's name
  mapped to its floor (ruling 7) -- the one change to an existing assertion,
  `STARTUP_FIELD_NAMES`;
* `build_service` passes `min_prefix_lengths={IPV4: settings.min_prefix_length,
  IPV6: settings.min_prefix_length_ipv6}` to the worker (decision 13 as
  noted), seen as 9 records per APPLIED event at `min_prefix_length=24`;
* readiness waits for the replay's publishes, and a publish failure keeps the
  service unready or ends `run()` (rulings 4 and 5);
* the shutdown order: every producer `flush()` comes before the transport's
  `close()` (ruling 6, section 47.4).

Producer doubles reach the worker through a bus double's `producer()`
(Amendment 2, "Test seams"); the hot-ip log is the double's inner
`InMemoryBus`.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self, cast

import httpx
import pytest
from hammertime.bus.interface import (
    AssignmentListener,
    ConsumedMessage,
    Consumer,
    MessageBus,
    Producer,
)
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import HOT_IP, PREFIX_STATS
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import ConfigurationError, InvariantViolation
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded
from hammertime.core.runtime import Service
from hammertime.trie.config import TrieSettings
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.publisher import PrefixStatsPublishError
from hammertime.trie.service import SERVICE_NAME, TrieService, build_service
from hammertime.trie.state import TrieState
from hammertime.trie.worker import TrieWorker

IPV4 = AddressFamily.IPV4
IPV6 = AddressFamily.IPV6
TOPIC = HOT_IP.name
STATS_TOPIC = PREFIX_STATS.name
AN_HOUR = 3600.0

T0 = datetime.fromtimestamp(1_800_000_000, tz=UTC)
IP_A = Address.parse("10.20.30.1")
IP_B = Address.parse("10.20.30.2")

# `docs/spec/integration-scenarios.md` section 2.1, identical to
# `config/detection.v1.json`.
_DOCUMENT: dict[str, Any] = {
    "config_version": 1,
    "window_seconds": 300,
    "bucket_seconds": 10,
    "hot_threshold": 1000,
    "cold_threshold": 800,
    "minimum_hot_ips": 16,
    "minimum_hot_ratio": 0.10,
    "allowed_lateness_seconds": 30,
    "state_retention_seconds": 600,
    "weight_function": "threshold_ratio",
    "weight_max": 1000000,
}

DETECTION_CONFIG = DetectionConfig(
    config_version=1,
    window_seconds=300,
    bucket_seconds=10,
    hot_threshold=1000,
    cold_threshold=800,
    allowed_lateness_seconds=30,
    state_retention_seconds=600,
)

STARTUP_FIELD_NAMES = {
    "bus_kind",
    "bus_endpoints",
    "config_path",
    "config_version",
    "bind",
    "families",
    # ADR-0017 Amendment 2 ruling 7: "`startup_fields()` gains
    # `min_prefix_lengths`".
    "min_prefix_lengths",
}


def _write(path: Path, **overrides: Any) -> Path:
    path.write_text(json.dumps({**_DOCUMENT, **overrides}))
    return path


def _settings(
    path: Path, *, bus_brokers: str = "nats://localhost:4222", **extra: Any
) -> TrieSettings:
    """`extra` passes further `TrieSettings` fields, such as `families` or the
    slice-2 `min_prefix_length` and `min_prefix_length_ipv6`."""

    return TrieSettings(
        host="127.0.0.1",
        port=0,
        detection_config_path=path,
        bus_kind="memory",
        bus_brokers=bus_brokers,
        config_poll_interval_s=AN_HOUR,
        **extra,
    )


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return _write(tmp_path / "detection.json")


@pytest.fixture
def bus() -> InMemoryBus:
    return InMemoryBus()


def _envelope(ip: Address, n: int) -> EventEnvelope[HotIpAdded]:
    ts = T0 + timedelta(seconds=n)
    return EventEnvelope(
        agent_id="aggregator-shard-0",
        sequence=n + 1,
        event_type="HotIpAdded",
        config_version=1,
        timestamp=ts,
        subject=str(ip),
        payload=HotIpAdded(
            ip=ip, timestamp=ts, sequence=n + 1, window_count=1200, config_version=1
        ),
    )


async def _publish(bus: InMemoryBus, envelope: EventEnvelope[HotIpAdded]) -> None:
    await bus.producer().publish(
        TOPIC,
        key=str(envelope.payload.ip),
        value=encode(envelope),
        message_id=envelope.event_id,
    )


async def _yield_until(predicate: Callable[[], bool], *, steps: int = 10_000) -> None:
    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was never reached")


def _client(service: TrieService) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url="http://test")


def _media_type(response: httpx.Response) -> str:
    return response.headers["content-type"].split(";")[0].strip()


def _message(ip: Address, offset: int) -> ConsumedMessage:
    return ConsumedMessage(
        topic=TOPIC,
        partition=0,
        offset=offset,
        key=str(ip).encode(),
        value=encode(_envelope(ip, offset)),
    )


def _worker_on(bus: MessageBus) -> TrieWorker:
    state = TrieState(families=frozenset({IPV4}), config=DETECTION_CONFIG)
    return TrieWorker(bus=bus, state=state, metrics=TrieMetrics())


class _HeldStream:
    """Yields its queued messages, then waits until `feed()` queues another."""

    def __init__(self, messages: Iterable[ConsumedMessage]) -> None:
        self._queue = list(messages)
        self._arrived = asyncio.Event()
        self.waiting = False

    def feed(self, message: ConsumedMessage) -> None:
        self._queue.append(message)
        self._arrived.set()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ConsumedMessage:
        while not self._queue:
            self._arrived.clear()
            self.waiting = True
            try:
                await self._arrived.wait()
            finally:
                self.waiting = False
        return self._queue.pop(0)


class _HeldConsumer:
    def __init__(self, stream: _HeldStream) -> None:
        self._stream = stream

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        return self._stream

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        raise AssertionError("the trie acknowledged a positional subscription")

    async def close(self) -> None:
        return None


class _ScriptedBus:
    """A bus whose log end is `end`, whose first retained offset is `first`
    (default `0`, the memory bus's value), and whose one consumer reads
    `stream`."""

    def __init__(self, stream: _HeldStream, *, end: int, first: int = 0) -> None:
        self._end = end
        self._first = first
        self._consumer = _HeldConsumer(stream)

    def producer(self) -> Producer:
        return InMemoryBus().producer()

    def consumer(self, group_id: str) -> Consumer:
        return self._consumer

    async def end_offset(self, topic: str) -> int:
        return self._end

    async def first_offset(self, topic: str) -> int:
        return self._first


class _ClosableBus:
    """An `InMemoryBus` with a `close()` that counts the calls made to it."""

    def __init__(self) -> None:
        self.inner = InMemoryBus()
        self.closed = 0

    def producer(self) -> Producer:
        return self.inner.producer()

    def consumer(self, group_id: str) -> Consumer:
        return self.inner.consumer(group_id)

    async def end_offset(self, topic: str) -> int:
        return await self.inner.end_offset(topic)

    async def first_offset(self, topic: str) -> int:
        return await self.inner.first_offset(topic)

    async def close(self) -> None:
        self.closed += 1


class _FakeTransport:
    """Stands in for the `NatsBus` that `build_service` builds under `nats`."""

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1


class TestConstruction:
    def test_build_service_returns_a_service_that_is_not_ready(
        self, config_path: Path, bus: InMemoryBus
    ) -> None:
        service = build_service(_settings(config_path), bus=bus)

        assert isinstance(service, Service)
        assert isinstance(service, TrieService)
        assert SERVICE_NAME == "trie"
        assert service.name == SERVICE_NAME
        assert service.ready is False
        assert service.state.config.config_version == 1
        assert service.state.families == frozenset({IPV4})
        assert isinstance(service.metrics, TrieMetrics)

    def test_an_invalid_document_fails_build_service(
        self, tmp_path: Path, bus: InMemoryBus
    ) -> None:
        path = _write(tmp_path / "detection.json", hot_threshold=1000, cold_threshold=1000)

        with pytest.raises(ConfigurationError):
            build_service(_settings(path), bus=bus)


class TestAdminEndpointsAcrossTheLifecycle:
    async def test_starting_ready_stopping(self, config_path: Path, bus: InMemoryBus) -> None:
        service = build_service(_settings(config_path), bus=bus)

        async with _client(service) as client:
            readyz = await client.get("/readyz")
            assert readyz.status_code == 503
            assert readyz.content == b'{"status":"starting"}'
            assert _media_type(readyz) == "application/json"

            healthz = await client.get("/healthz")
            assert healthz.status_code == 200
            assert healthz.content == b'{"status":"ok"}'

            await service.start()
            try:
                assert service.ready is True
                readyz = await client.get("/readyz")
                assert readyz.status_code == 200
                assert readyz.content == b'{"status":"ready"}'

                metrics = await client.get("/metrics")
                assert metrics.status_code == 200
                assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")
            finally:
                await service.stop()

            readyz = await client.get("/readyz")
            assert readyz.status_code == 503
            assert readyz.content == b'{"status":"stopping"}'
            assert service.ready is False


# FastAPI's default documentation paths (ADR-0017 decision 13).
GENERATED_DOCUMENTATION_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


class TestNoGeneratedApiDocumentation:
    """Decision 13: `create_app` builds `FastAPI(..., openapi_url=None,
    docs_url=None, redoc_url=None)`, "so the app serves the routes it declares
    and nothing else ... Those four paths answer FastAPI's 404, like any other
    path the app does not declare." `docs/protocol/read-api-v1.md`: "The trie
    serves no generated API documentation. `GET /openapi.json`, `/docs`,
    `/docs/oauth2-redirect` and `/redoc` answer 404". Only the status is
    pinned, not FastAPI's 404 body; the body is checked only for not being a
    documentation page."""

    @staticmethod
    async def _assert_no_documentation(client: httpx.AsyncClient) -> None:
        for path in GENERATED_DOCUMENTATION_PATHS:
            response = await client.get(path)
            assert response.status_code == 404, path
            body = response.text.lower()
            assert "swagger" not in body, path
            assert "redoc" not in body, path

    async def test_the_four_paths_answer_404_before_and_after_start(
        self, config_path: Path, bus: InMemoryBus
    ) -> None:
        service = build_service(_settings(config_path), bus=bus)

        async with _client(service) as client:
            await self._assert_no_documentation(client)
            # The three admin routes still answer as read-api-v1.md's table
            # says (and `TestAdminEndpointsAcrossTheLifecycle` pins).
            healthz = await client.get("/healthz")
            assert healthz.status_code == 200
            assert healthz.content == b'{"status":"ok"}'
            readyz = await client.get("/readyz")
            assert readyz.status_code == 503
            assert readyz.content == b'{"status":"starting"}'
            metrics = await client.get("/metrics")
            assert metrics.status_code == 200
            assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")

            await service.start()
            try:
                await self._assert_no_documentation(client)
                healthz = await client.get("/healthz")
                assert healthz.status_code == 200
                assert healthz.content == b'{"status":"ok"}'
                readyz = await client.get("/readyz")
                assert readyz.status_code == 200
                assert readyz.content == b'{"status":"ready"}'
                metrics = await client.get("/metrics")
                assert metrics.status_code == 200
                assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")
            finally:
                await service.stop()


class TestStartReplays:
    async def test_start_replays_what_was_published_before_it(
        self, config_path: Path, bus: InMemoryBus
    ) -> None:
        n = 12
        for i in range(n):
            await _publish(bus, _envelope(Address.parse(f"10.0.0.{i + 1}"), i))
        service = build_service(_settings(config_path), bus=bus)

        await service.start()
        try:
            assert service.ready is True
            assert service.state.event_sequence == n
            assert service.state.of(IPV4).trie.hot_ip_count == n
        finally:
            await service.stop()

    async def test_trie_recovery_seconds_is_the_time_spent_in_start(
        self, config_path: Path, bus: InMemoryBus
    ) -> None:
        # Decision 13: "The service does not call `monotonic` before that: its
        # first call is `start()`'s own entry reading."
        readings: list[float] = []

        def monotonic() -> float:
            reading = 10.0 if not readings else 12.0
            readings.append(reading)
            return reading

        state = TrieState(families=frozenset({IPV4}), config=DETECTION_CONFIG)
        worker = TrieWorker(bus=bus, state=state, metrics=TrieMetrics())
        settings = _settings(config_path)
        service = TrieService(settings, worker, DETECTION_CONFIG, monotonic=monotonic)

        await service.start()
        try:
            assert service.metrics.get("trie_recovery_seconds") == 2.0
            assert service.ready is True
        finally:
            await service.stop()


class TestReloadConfig:
    async def test_only_a_valid_greater_version_is_adopted(
        self, config_path: Path, bus: InMemoryBus
    ) -> None:
        service = build_service(_settings(config_path), bus=bus)
        await service.start()
        try:
            _write(config_path, config_version=2, hot_threshold=1200)
            in_force = await service.reload_config()
            assert in_force.config_version == 2
            assert service.state.config.config_version == 2
            assert service.state.config.hot_threshold == 1200

            # An invalid version 3 is rejected; version 2 stays in force.
            _write(config_path, config_version=3, hot_threshold=900, cold_threshold=900)
            in_force = await service.reload_config()
            assert in_force.config_version == 2
            assert service.state.config.config_version == 2
            assert service.state.config.hot_threshold == 1200

            # An equal version, and a lower one, are ignored.
            _write(config_path, config_version=2, hot_threshold=1500)
            in_force = await service.reload_config()
            assert in_force.config_version == 2
            assert service.state.config.hot_threshold == 1200

            _write(config_path, config_version=1, hot_threshold=1500)
            in_force = await service.reload_config()
            assert in_force.config_version == 2
            assert service.state.config.config_version == 2
            assert service.state.config.hot_threshold == 1200
        finally:
            await service.stop()


class TestStartupFields:
    def test_the_fields(self, config_path: Path, bus: InMemoryBus) -> None:
        service = build_service(_settings(config_path), bus=bus)

        fields = service.startup_fields()

        assert set(fields) == STARTUP_FIELD_NAMES
        assert fields["bind"] == "127.0.0.1:0"
        assert fields["families"] == ["ipv4"]
        assert fields["bus_kind"] == "memory"
        # Ruling 7: "each served family's name mapped to its floor, for
        # example `{"ipv4": 8}`".
        assert fields["min_prefix_lengths"] == {"ipv4": 8}

    def test_min_prefix_lengths_names_every_served_family(
        self, config_path: Path, bus: InMemoryBus
    ) -> None:
        settings = _settings(
            config_path,
            families=frozenset({IPV4, IPV6}),
            min_prefix_length=24,
            min_prefix_length_ipv6=64,
        )
        service = build_service(settings, bus=bus)

        fields = service.startup_fields()

        assert set(fields) == STARTUP_FIELD_NAMES
        assert fields["min_prefix_lengths"] == {"ipv4": 24, "ipv6": 64}

    def test_the_brokers_userinfo_never_appears(self, config_path: Path, bus: InMemoryBus) -> None:
        settings = _settings(config_path, bus_brokers="nats://user:s3cret@nats:4222")
        service = build_service(settings, bus=bus)

        fields = service.startup_fields()

        assert fields["bus_endpoints"] == ["nats://nats:4222"]
        assert "s3cret" not in repr(fields)
        assert "s3cret" not in json.dumps(dict(fields), default=str)


class TestRunAndStop:
    async def test_run_consumes_and_returns_cleanly_on_stop(
        self, config_path: Path, bus: InMemoryBus
    ) -> None:
        service = build_service(_settings(config_path), bus=bus)
        await service.start()
        task = asyncio.create_task(asyncio.wait_for(service.run(), timeout=10.0))
        trie = service.state.of(IPV4).trie

        await _publish(bus, _envelope(IP_A, 0))
        await _yield_until(lambda: trie.hot_ip_count == 1)
        assert service.state.event_sequence == 1

        await service.stop()
        # `run()` returns without an exception: `await task` would raise it.
        await task
        assert service.ready is False
        await service.stop()

    async def test_stop_before_start_returns(self, config_path: Path, bus: InMemoryBus) -> None:
        service = build_service(_settings(config_path), bus=bus)

        await service.stop()
        await service.stop()

        assert service.ready is False

    async def test_run_reports_a_worker_failure(self, config_path: Path, bus: InMemoryBus) -> None:
        # Decision 7: an `InvariantViolation` propagates out of `run()`
        # (`run_exited`, exit 1). The leaf's count is corrupted as ADR-0014
        # A12 and ADR-0017's Test seams describe.
        service = build_service(_settings(config_path), bus=bus)
        await service.start()
        trie = service.state.of(IPV4).trie
        assert trie.add_hot_ip(IP_A) is True
        trie.arena.hot_count[trie.root] = 0

        await _publish(bus, _envelope(IP_A, 0))
        try:
            with pytest.raises(InvariantViolation):
                await asyncio.wait_for(service.run(), timeout=10.0)
        finally:
            await service.stop()


class TestReadinessAfterAnInterruptedReplay:
    """Decision 4: `TrieService.start()` "marks itself ready -- unless `stop()`
    has begun or the worker is not caught up"."""

    async def _assert_not_ready(self, service: TrieService) -> None:
        assert service.ready is False
        async with _client(service) as client:
            readyz = await client.get("/readyz")
        assert readyz.status_code == 503
        assert readyz.content != b'{"status":"ready"}'

    async def test_stop_during_the_replay_leaves_the_service_not_ready(
        self, config_path: Path
    ) -> None:
        stream = _HeldStream([_message(IP_A, 0)])
        worker = _worker_on(_ScriptedBus(stream, end=3))
        service = TrieService(_settings(config_path), worker, DETECTION_CONFIG)
        task = asyncio.create_task(service.start())
        await _yield_until(lambda: stream.waiting)

        await service.stop()
        stream.feed(_message(IP_B, 1))
        await asyncio.wait_for(task, timeout=10.0)

        assert worker.caught_up is False
        await self._assert_not_ready(service)

    async def test_a_worker_not_caught_up_leaves_the_service_not_ready(
        self, config_path: Path
    ) -> None:
        # Only the worker is stopped: the service's own `stop()` has not begun,
        # so what keeps it unready is that the worker is not caught up.
        stream = _HeldStream([_message(IP_A, 0)])
        worker = _worker_on(_ScriptedBus(stream, end=3))
        service = TrieService(_settings(config_path), worker, DETECTION_CONFIG)
        task = asyncio.create_task(service.start())
        await _yield_until(lambda: stream.waiting)

        await worker.stop()
        stream.feed(_message(IP_B, 1))
        try:
            await asyncio.wait_for(task, timeout=10.0)

            assert worker.caught_up is False
            await self._assert_not_ready(service)
        finally:
            await service.stop()


class TestReadyOnALogThatHoldsNothingToReplay:
    """The security audit's finding 1 (ADR-0017 "Revision 2026-09-23"): on a
    JetStream hot-ip stream that holds no record at or after the fresh start
    offset, `start()` waited until the startup deadline. Decision 4 step 3 now
    passes the offsets below `first`, so the worker is caught up at once, and
    `TrieService.start()` "marks itself ready". Consequences: "On a fresh
    deployment that is at once, because the provisioned hot-ip stream is
    empty". The log is modelled by `_ScriptedBus` with `end == first` ("Test
    seams", "The log's bounds"), over a stream that yields nothing;
    `asyncio.wait_for(..., 10.0)` is the failure bound on a `start()` that
    waits for a record."""

    @pytest.mark.parametrize(
        "end",
        [
            # "*A stream nothing has been written to.* `end` and `first` are
            # both `1`."
            pytest.param(1, id="never-written"),
            # "*A stream whose every record has aged out or been purged.*
            # `first` is `end`".
            pytest.param(10_000, id="every-record-aged-out-or-purged"),
        ],
    )
    async def test_start_returns_and_the_service_is_ready(
        self, config_path: Path, end: int
    ) -> None:
        worker = _worker_on(_ScriptedBus(_HeldStream([]), end=end, first=end))
        service = TrieService(_settings(config_path), worker, DETECTION_CONFIG)

        try:
            await asyncio.wait_for(service.start(), timeout=10.0)

            assert service.ready is True
            async with _client(service) as client:
                readyz = await client.get("/readyz")
            assert readyz.status_code == 200
            assert readyz.content == b'{"status":"ready"}'
            # Decision 4 step 3: "`event_sequence` becomes `first`".
            assert service.state.event_sequence == end
        finally:
            await service.stop()


class TestWhoClosesTheBus:
    """Decision 13: `run()` "closes the transport it built" -- the `NatsBus`
    that `build_service` passes as `transport` -- and nothing else."""

    async def test_a_transport_is_started_by_start_and_closed_by_run(
        self, config_path: Path
    ) -> None:
        bus = InMemoryBus()
        transport = _FakeTransport()
        service = TrieService(
            _settings(config_path),
            _worker_on(bus),
            DETECTION_CONFIG,
            transport=transport,  # type: ignore[arg-type]
        )

        await service.start()
        # Decision 4: "when it built a `NatsBus` itself, it starts the bus".
        assert transport.started == 1
        task = asyncio.create_task(asyncio.wait_for(service.run(), timeout=10.0))
        trie = service.state.of(IPV4).trie
        await _publish(bus, _envelope(IP_A, 0))
        await _yield_until(lambda: trie.hot_ip_count == 1)

        await service.stop()
        await task

        assert transport.closed >= 1

    async def test_an_injected_bus_is_not_closed(self, config_path: Path) -> None:
        bus = _ClosableBus()
        service = build_service(_settings(config_path), bus=bus)

        await service.start()
        task = asyncio.create_task(asyncio.wait_for(service.run(), timeout=10.0))
        trie = service.state.of(IPV4).trie
        await _publish(bus.inner, _envelope(IP_A, 0))
        await _yield_until(lambda: trie.hot_ip_count == 1)

        await service.stop()
        await task

        assert bus.closed == 0


# ==========================================================================
# Slice 2: ADR-0017 Amendment 2 -- the publisher's wiring into the service.
# ==========================================================================


class _StatsProducer:
    """A producer double (Amendment 2, "Test seams"). A publish whose key
    `hold(key)` accepts waits until `release` is set; then, if `fail` is set,
    it raises it. `flush()` appends `"flush"` to `trace` if one is given."""

    def __init__(
        self,
        *,
        hold: Callable[[object], bool] | None = None,
        fail: BaseException | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self.calls = 0
        self.release = asyncio.Event()
        self.in_flight = 0
        self.flushes = 0
        self._hold = hold if hold is not None else (lambda key: False)
        self._fail = fail
        self._trace = trace

    async def publish(
        self, topic: object, key: object, value: object, *, message_id: object = None
    ) -> None:
        self.calls += 1
        if self._hold(key):
            self.in_flight += 1
            try:
                await self.release.wait()
            finally:
                self.in_flight -= 1
        if self._fail is not None:
            raise self._fail

    async def flush(self) -> None:
        self.flushes += 1
        if self._trace is not None:
            self._trace.append("flush")


class _ProducerBus:
    """An `InMemoryBus` (`inner`, which holds the hot-ip log) whose
    `producer()` returns the given double."""

    def __init__(self, producer: _StatsProducer) -> None:
        self.inner = InMemoryBus()
        self.stats_producer = producer

    def producer(self) -> Producer:
        return cast(Producer, self.stats_producer)

    def consumer(self, group_id: str) -> Consumer:
        return self.inner.consumer(group_id)

    async def end_offset(self, topic: str) -> int:
        return await self.inner.end_offset(topic)

    async def first_offset(self, topic: str) -> int:
        return await self.inner.first_offset(topic)


class _TracingTransport(_FakeTransport):
    """A `_FakeTransport` whose `close()` also appends `"close"` to `trace`."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__()
        self._trace = trace

    async def close(self) -> None:
        self._trace.append("close")
        await super().close()


class _PublishFailed(Exception):
    """Raised by `_StatsProducer.publish` when a test sets it."""


def _published(service: TrieService, family: str = "ipv4") -> int | float:
    return service.metrics.get("prefix_stats_published", family=family)


class TestBuildServicePassesTheFloors:
    """Decision 13 as noted by Amendment 2 ruling 6: "`build_service` passes
    `min_prefix_lengths={IPV4: settings.min_prefix_length, IPV6:
    settings.min_prefix_length_ipv6}` to the worker". At an IPv4 floor of 24,
    an APPLIED event publishes `/24` to `/32`: 9 records."""

    async def test_an_event_replayed_in_start(self, config_path: Path, bus: InMemoryBus) -> None:
        await _publish(bus, _envelope(IP_A, 0))
        service = build_service(_settings(config_path, min_prefix_length=24), bus=bus)

        await service.start()
        try:
            assert service.ready is True
            assert await bus.end_offset(STATS_TOPIC) == 9
            assert _published(service) == 9
        finally:
            await service.stop()

    async def test_an_event_consumed_in_run(self, config_path: Path, bus: InMemoryBus) -> None:
        service = build_service(_settings(config_path, min_prefix_length=24), bus=bus)
        await service.start()
        task = asyncio.create_task(asyncio.wait_for(service.run(), timeout=10.0))
        try:
            await _publish(bus, _envelope(IP_A, 0))
            await _yield_until(lambda: _published(service) == 9)

            assert await bus.end_offset(STATS_TOPIC) == 9
        finally:
            await service.stop()
        await task


class TestReadinessWaitsForThePublishes:
    """Ruling 4: "`TrieService` marks itself ready only after `start()`
    returns ... A ready trie has therefore had every stat of every
    state-changing record it replayed acknowledged by the log." Section
    47.2: `/readyz` answers 503 until the service is ready."""

    async def test_not_ready_until_the_held_publishes_are_released(self, config_path: Path) -> None:
        producer = _StatsProducer(hold=lambda key: True)
        bus = _ProducerBus(producer)
        await _publish(bus.inner, _envelope(IP_A, 0))
        service = build_service(_settings(config_path), bus=bus)

        async with _client(service) as client:
            task = asyncio.create_task(service.start())
            try:
                try:
                    await _yield_until(lambda: producer.in_flight == 25)

                    assert service.ready is False
                    readyz = await client.get("/readyz")
                    assert readyz.status_code == 503
                    assert readyz.content != b'{"status":"ready"}'
                    assert not task.done()
                finally:
                    producer.release.set()
                await asyncio.wait_for(task, timeout=10.0)

                assert service.ready is True
                readyz = await client.get("/readyz")
                assert readyz.status_code == 200
                assert readyz.content == b'{"status":"ready"}'
            finally:
                await service.stop()


class TestAPublishFailure:
    """Ruling 5: `PrefixStatsPublishError` goes "out of `handle()`, then out of
    `start()` (`start_failed`, exit 1, never ready) or `run()` (`run_exited`,
    exit 1)"; "`/readyz` never answers 200"."""

    async def test_start_raises_and_the_service_stays_unready(self, config_path: Path) -> None:
        producer = _StatsProducer(fail=_PublishFailed("refused"))
        bus = _ProducerBus(producer)
        await _publish(bus.inner, _envelope(IP_A, 0))
        service = build_service(_settings(config_path), bus=bus)

        try:
            with pytest.raises(PrefixStatsPublishError):
                await asyncio.wait_for(service.start(), timeout=10.0)

            assert service.ready is False
            async with _client(service) as client:
                readyz = await client.get("/readyz")
            assert readyz.status_code == 503
        finally:
            await service.stop()

    async def test_a_failure_during_run_ends_run(self, config_path: Path) -> None:
        producer = _StatsProducer(fail=_PublishFailed("refused"))
        bus = _ProducerBus(producer)
        service = build_service(_settings(config_path), bus=bus)
        await service.start()
        assert service.ready is True

        try:
            task = asyncio.create_task(asyncio.wait_for(service.run(), timeout=10.0))
            await _publish(bus.inner, _envelope(IP_A, 0))

            with pytest.raises(PrefixStatsPublishError):
                await task
        finally:
            await service.stop()


class TestShutdownOrder:
    """Ruling 6, "The shutdown order": `worker.stop()` "sets the flag, takes
    the lock once the message in hand and its publishes are done, and
    flushes"; then "`run()`'s teardown gathers its tasks and closes the
    transport it built, the bus, last". Section 47.4: flush before close."""

    async def test_every_flush_comes_before_the_transports_close(self, config_path: Path) -> None:
        trace: list[str] = []
        producer = _StatsProducer(trace=trace)
        bus = _ProducerBus(producer)
        transport = _TracingTransport(trace)
        service = TrieService(
            _settings(config_path),
            _worker_on(bus),
            DETECTION_CONFIG,
            transport=transport,  # type: ignore[arg-type]
        )

        await service.start()
        task = asyncio.create_task(asyncio.wait_for(service.run(), timeout=10.0))
        await _publish(bus.inner, _envelope(IP_A, 0))
        await _yield_until(lambda: _published(service) == 25)

        await service.stop()
        await task

        assert "close" in trace
        flushes = [index for index, entry in enumerate(trace) if entry == "flush"]
        assert flushes
        assert max(flushes) < trace.index("close")
