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
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import HOT_IP
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import ConfigurationError, InvariantViolation
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded
from hammertime.core.runtime import Service
from hammertime.trie.config import TrieSettings
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.service import SERVICE_NAME, TrieService, build_service
from hammertime.trie.state import TrieState
from hammertime.trie.worker import TrieWorker

IPV4 = AddressFamily.IPV4
TOPIC = HOT_IP.name
AN_HOUR = 3600.0

T0 = datetime.fromtimestamp(1_800_000_000, tz=UTC)
IP_A = Address.parse("10.20.30.1")

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
}


def _write(path: Path, **overrides: Any) -> Path:
    path.write_text(json.dumps({**_DOCUMENT, **overrides}))
    return path


def _settings(path: Path, *, bus_brokers: str = "nats://localhost:4222") -> TrieSettings:
    return TrieSettings(
        host="127.0.0.1",
        port=0,
        detection_config_path=path,
        bus_kind="memory",
        bus_brokers=bus_brokers,
        config_poll_interval_s=AN_HOUR,
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
