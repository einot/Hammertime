"""HTTP-level pipeline coverage for `POST /v1/observations` (issue #32).

Spec: section 4, section 19, section 20, section 23, section 36.
ADR-0002 (event-time windows), ADR-0003 (deltas, dedup, event_id identity),
ADR-0004 (per-IP observation fan-out). Wire contract:
`docs/protocol/observation-v1.md`.

This file targets the *new* pipeline wiring issue #32 adds ahead of the
already-covered validation layer (`test_validation.py`, `test_routes.py`):

    auth (401/403) -> rate limit (429) -> validation (400/413, not
    re-tested here) -> dedup check -> publish -> 202/503

and, per ADR-0004, that an accepted request is fanned out into one bus
message per *distinct* IP (duplicate IPs within one request coalesced,
deltas summed) on `hammertime.observations.v1`, each message a
single-entry `RequestObservation` envelope keyed (and `subject`-tagged) by
that entry's IP, with a deterministic, per-message-distinct `event_id`.

Written blind to `publisher.py`, `dedup/service.py`, and the coder's actual
diff to `api/routes.py` / `app.py` / `config.py` /
`hammertime.core.events.envelope` / `hammertime.core.events.codec`, per
this repo's test-author convention (see `test_dedup.py`'s equivalent note
for issue #31). `hammertime.core.events.models` / `envelope.EventEnvelope`
/ `codec.decode` are used here exactly as already pinned down by
`packages/hammertime-core/src/hammertime/core/tests/test_envelope.py` and
`test_codec.py` (also blind-written, reconciled once already), plus
ADR-0004's new `subject` field on `EventEnvelope`.

ASSUMED `create_app` signature (not yet known -- `app.py`/`routes.py`/
`config.py` are being written in parallel by the coder; reconcile
`_build_app` below against the merged implementation first if these tests
fail to even collect/construct an app):

    create_app(
        settings: IngestSettings | None = None,
        *,
        agent_registry: AgentRegistry | None = None,
        rate_limiter: RateLimiter | None = None,
        dedup_store: MemoryDedupStore | None = None,
        bus: InMemoryBus | None = None,
    ) -> FastAPI

mirroring `app.py`'s existing `settings: IngestSettings | None = None`
optional-override pattern (see `test_routes.py`) and the task's own
suggestion to pass an explicit `AgentRegistry.from_records(...)`,
`IngestSettings(...)`, and `InMemoryBus`/`MemoryDedupStore`. In particular:

* `bus` (rather than a pre-built `Producer`) is assumed so a test can keep
  its own reference to the same `InMemoryBus` handed to `create_app` and
  read every topic's log back afterwards -- `create_app` is assumed to
  call `bus.producer()` internally the same way a `MemoryConsumer` is
  built via `InMemoryBus.consumer(group_id)` elsewhere in this repo.
* `AgentRecord.rate_limit_rps` is assumed to be read by the new pipeline
  wiring (per `auth/agents.py`'s and `ratelimit/__init__.py`'s own
  docstrings, both of which explicitly defer "look up the configured
  limit and call `check()`" to issue #32) -- every `AgentRecord` built
  below sets `rate_limit_rps` explicitly so no assumption about an
  `IngestSettings` default-rate-limit field name is needed.
* If `create_app` instead takes a pre-built `Producer` (e.g. `producer=`)
  rather than a whole `bus=`, or folds these onto `IngestState` some other
  way, only `_build_app` below should need to change.

Explicit gap, not resolved here: `docs/protocol/observation-v1.md`'s
response table documents `200` for a duplicate `(agent_id, sequence)` but
does not pin down the response body's exact JSON shape (unlike `202`,
whose `{"status": "accepted"}` body is already established by
`test_routes.py`). `TestDuplicateSequenceDoesNotRepublish` below only
asserts the documented status code, not a body shape not written down
anywhere -- if the coder settles on a specific body, add that assertion
during reconciliation rather than guessing it here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import OBSERVATIONS
from hammertime.core.addressing.address import Address
from hammertime.core.events.codec import EventPayload, decode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import RequestObservation
from hammertime.core.time.clock import ManualClock
from hammertime.ingest.app import create_app
from hammertime.ingest.auth.agents import AgentRecord, AgentRegistry
from hammertime.ingest.config import IngestSettings
from hammertime.ingest.ratelimit import RateLimiter
from hammertime.store.memory import MemoryDedupStore

_CONFIG_PATH = Path(__file__).parents[6] / "config" / "detection.v1.json"

# Known-aligned per test_routes.py's own happy-path fixture.
WINDOW_START_TEXT = "2026-09-14T10:00:00Z"
WINDOW_START = datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC)
WINDOW_SECONDS = 60

KNOWN_AGENT_ID = "edge-17"
KNOWN_AGENT_TOKEN = "s3cr3t-token-value"
OTHER_AGENT_ID = "edge-99"
OTHER_AGENT_TOKEN = "other-s3cr3t-token"

# Generously high so happy-path/dedup/fan-out tests never accidentally trip
# the rate limiter; rate-limit-focused tests override this explicitly.
_AMPLE_RATE_LIMIT_RPS = 1_000


def _agent(agent_id: str, token: str, *, rate_limit_rps: int, enabled: bool = True) -> AgentRecord:
    return AgentRecord(
        agent_id=agent_id, token=token, enabled=enabled, rate_limit_rps=rate_limit_rps
    )


def _settings(**overrides: object) -> IngestSettings:
    fields: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 0,
        "max_body_bytes": 1_048_576,
        "max_observations": 10_000,
        "detection_config_path": _CONFIG_PATH,
        # rate_limit_rps/agents_path/bus_kind/bus_brokers/store_kind/redis_url
        # are all unused by _build_app below -- it injects agent_registry,
        # rate_limiter, dedup_store, and bus directly instead -- but
        # IngestSettings requires every field regardless.
        "rate_limit_rps": _AMPLE_RATE_LIMIT_RPS,
        "agents_path": Path("unused -- agent_registry is injected directly"),
        "bus_kind": "memory",
        "bus_brokers": "",
        "store_kind": "memory",
        "redis_url": "",
    }
    fields.update(overrides)
    return IngestSettings(**fields)  # type: ignore[arg-type]


def _build_app(
    *,
    agents: list[AgentRecord] | None = None,
    settings: IngestSettings | None = None,
    clock: ManualClock | None = None,
) -> tuple[TestClient, InMemoryBus]:
    """A pipeline-ready app plus the `InMemoryBus` it publishes to.

    Every dependency the pipeline needs (auth registry, rate limiter, dedup
    store, bus) is explicit and freshly constructed per call, so tests
    never share state with each other.
    """
    default_agent = _agent(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN, rate_limit_rps=_AMPLE_RATE_LIMIT_RPS)
    registry = AgentRegistry.from_records(agents if agents is not None else [default_agent])
    resolved_clock = clock if clock is not None else ManualClock(initial=0)
    bus = InMemoryBus()
    app = create_app(
        settings if settings is not None else _settings(),
        agent_registry=registry,
        rate_limiter=RateLimiter(clock=resolved_clock),
        dedup_store=MemoryDedupStore(clock=resolved_clock),
        bus=bus,
    )
    # __enter__ (not a bare TestClient(app)) runs the app's lifespan, which
    # is what actually populates request.app.state.ingest -- every route
    # handler depends on it. Deliberately not paired with __exit__: these
    # are short-lived, per-test clients with no external resources (the
    # in-memory bus/store need no teardown), and callers here call
    # client.post(...) directly rather than via `with`.
    return TestClient(app).__enter__(), bus


def _headers(agent_id: str, token: str) -> dict[str, str]:
    return {"X-Agent-Id": agent_id, "Authorization": f"Bearer {token}"}


def _body(
    *,
    agent_id: str = KNOWN_AGENT_ID,
    sequence: int = 1,
    observations: list[dict[str, object]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    doc: dict[str, object] = {
        "agent_id": agent_id,
        "sequence": sequence,
        "window_start": WINDOW_START_TEXT,
        "window_seconds": WINDOW_SECONDS,
        "observations": (
            observations
            if observations is not None
            else [{"ip": "192.168.1.42", "request_count": 1}]
        ),
    }
    doc.update(overrides)
    return doc


def _topic_records(
    bus: InMemoryBus, topic: str = OBSERVATIONS.name
) -> list[tuple[bytes | None, bytes]]:
    """Read a topic's whole log directly off the bus.

    `InMemoryBus.consumer().subscribe()` blocks forever waiting for the
    *next* message past what has already been read -- awkward to drive
    from a synchronous `TestClient`-based test. Reading the private
    `_logs` mapping directly mirrors this repo's own precedent for
    inspecting private test-double state (`test_ratelimit.py` reads
    `limiter._buckets` the same way).
    """
    return [(record.key, record.value) for record in bus._logs[topic]]


def _decoded_records(
    bus: InMemoryBus, topic: str = OBSERVATIONS.name
) -> list[tuple[bytes | None, EventEnvelope[EventPayload]]]:
    return [(key, decode(value)) for key, value in _topic_records(bus, topic)]


def _canonical(ip_text: str) -> str:
    return str(Address.parse(ip_text))


def _as_request_observation(envelope: EventEnvelope[EventPayload]) -> RequestObservation:
    # Every message this file decodes comes off hammertime.observations.v1,
    # which per ADR-0004 only ever carries single-entry RequestObservation
    # payloads -- narrows the codec's general EventPayload union so callers
    # can access RequestObservation-specific fields without a mypy complaint.
    payload = envelope.payload
    assert isinstance(payload, RequestObservation)
    return payload


class TestAuthGateRunsBeforeValidation:
    """auth MUST run before the body is even parsed (spec section 36)."""

    def _invalid_body(self, *, agent_id: str = KNOWN_AGENT_ID) -> dict[str, object]:
        # Malformed IP: on its own, always a 400 (test_routes.py already
        # covers that). Used here only to prove auth short-circuits first.
        return _body(agent_id=agent_id, observations=[{"ip": "not-an-ip", "request_count": 1}])

    def test_no_credentials_at_all_with_invalid_body_is_401_not_400(self) -> None:
        client, _bus = _build_app()
        response = client.post("/v1/observations", json=self._invalid_body())
        assert response.status_code == 401

    def test_unknown_agent_with_invalid_body_is_401_not_400(self) -> None:
        client, _bus = _build_app()
        response = client.post(
            "/v1/observations",
            json=self._invalid_body(agent_id="no-such-agent"),
            headers=_headers("no-such-agent", "irrelevant-token"),
        )
        assert response.status_code == 401

    def test_wrong_credential_with_invalid_body_is_403_not_400(self) -> None:
        client, _bus = _build_app()
        response = client.post(
            "/v1/observations",
            json=self._invalid_body(),
            headers=_headers(KNOWN_AGENT_ID, "wrong-token"),
        )
        assert response.status_code == 403

    def test_disabled_agent_with_invalid_body_is_403_not_400(self) -> None:
        disabled = _agent(
            KNOWN_AGENT_ID,
            KNOWN_AGENT_TOKEN,
            rate_limit_rps=_AMPLE_RATE_LIMIT_RPS,
            enabled=False,
        )
        client, _bus = _build_app(agents=[disabled])
        response = client.post(
            "/v1/observations",
            json=self._invalid_body(),
            headers=_headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN),
        )
        assert response.status_code == 403


class TestRateLimit:
    """docs/protocol/observation-v1.md: `429 | Per-agent rate limit`."""

    def test_agent_over_its_configured_limit_gets_429(self) -> None:
        limit_rps = 2
        client, bus = _build_app(
            agents=[_agent(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN, rate_limit_rps=limit_rps)]
        )
        headers = _headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)

        accepted = 0
        for sequence in range(1, limit_rps + 2):  # one more than the bucket holds
            response = client.post(
                "/v1/observations", json=_body(sequence=sequence), headers=headers
            )
            if response.status_code == 202:
                accepted += 1
            else:
                assert response.status_code == 429
                break
        else:
            raise AssertionError("bucket was never exhausted")

        assert accepted == limit_rps
        # A 429 must not be a silent partial success: only the accepted
        # requests may have reached the bus.
        assert len(_topic_records(bus)) == accepted

    def test_distinct_agent_within_its_own_limit_still_succeeds(self) -> None:
        exhausted_agent = _agent(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN, rate_limit_rps=1)
        fresh_agent = _agent(OTHER_AGENT_ID, OTHER_AGENT_TOKEN, rate_limit_rps=1)
        client, _bus = _build_app(agents=[exhausted_agent, fresh_agent])

        # Exhaust edge-17's single token.
        known_headers = _headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)
        client.post("/v1/observations", json=_body(sequence=1), headers=known_headers)
        limited = client.post("/v1/observations", json=_body(sequence=2), headers=known_headers)
        assert limited.status_code == 429

        # A different, never-touched agent must still get its own full bucket.
        isolated = client.post(
            "/v1/observations",
            json=_body(agent_id=OTHER_AGENT_ID, sequence=1),
            headers=_headers(OTHER_AGENT_ID, OTHER_AGENT_TOKEN),
        )
        assert isolated.status_code == 202

    def test_rate_limit_is_enforced_before_validation(self) -> None:
        # Pipeline order: auth -> rate limit -> validation. An
        # already-exhausted agent sending a body that would otherwise fail
        # validation must still see 429, not 400.
        client, _bus = _build_app(
            agents=[_agent(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN, rate_limit_rps=1)]
        )
        headers = _headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)
        # Exhausts the single token.
        client.post("/v1/observations", json=_body(sequence=1), headers=headers)

        response = client.post(
            "/v1/observations",
            json=_body(sequence=2, observations=[{"ip": "not-an-ip", "request_count": 1}]),
            headers=headers,
        )
        assert response.status_code == 429


class TestValidRequestFansOutPerDistinctIP:
    """ADR-0004: one bus message per distinct IP, keyed and subject-tagged by that IP."""

    def test_new_valid_request_is_202_and_publishes_one_message_per_ip(self) -> None:
        client, bus = _build_app()
        observations = [
            {"ip": "10.0.0.1", "request_count": 5},
            {"ip": "10.0.0.2", "request_count": 7},
            {"ip": "10.0.0.3", "request_count": 9},
        ]
        response = client.post(
            "/v1/observations",
            json=_body(sequence=1, observations=observations),
            headers=_headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN),
        )

        assert response.status_code == 202
        assert response.json() == {"status": "accepted"}

        decoded = _decoded_records(bus)
        assert len(decoded) == len(observations)

        by_ip = {_canonical(str(entry["ip"])): entry["request_count"] for entry in observations}
        seen_ips: set[str] = set()
        for key, envelope in decoded:
            assert key is not None
            key_text = key.decode("utf-8")
            assert key_text in by_ip
            seen_ips.add(key_text)

            payload = _as_request_observation(envelope)
            assert payload.agent_id == KNOWN_AGENT_ID  # the *authenticated* agent_id
            assert payload.sequence == 1
            assert payload.window_start == WINDOW_START
            assert payload.window_seconds == WINDOW_SECONDS
            assert len(payload.observations) == 1
            entry = payload.observations[0]
            assert str(entry.ip) == key_text
            assert entry.request_count == by_ip[key_text]

            # ADR-0004 point 4: subject MUST be set to the published
            # entry's IP text on IP-keyed topics.
            assert envelope.subject == key_text

        assert seen_ips == set(by_ip)


class TestDuplicateIpsWithinOneRequestAreCoalesced:
    """ADR-0004 point 3: repeated IPs in one batch sum, not double-publish."""

    def test_repeated_ip_within_one_request_produces_one_summed_message(self) -> None:
        client, bus = _build_app()
        observations = [
            {"ip": "10.0.0.5", "request_count": 4},
            {"ip": "10.0.0.5", "request_count": 6},
        ]
        response = client.post(
            "/v1/observations",
            json=_body(sequence=1, observations=observations),
            headers=_headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN),
        )

        assert response.status_code == 202

        decoded = _decoded_records(bus)
        assert len(decoded) == 1  # not two -- coalesced before fan-out

        key, envelope = decoded[0]
        expected_ip = _canonical("10.0.0.5")
        assert key is not None
        assert key.decode("utf-8") == expected_ip
        payload = _as_request_observation(envelope)
        assert len(payload.observations) == 1
        entry = payload.observations[0]
        assert str(entry.ip) == expected_ip
        assert entry.request_count == 10  # 4 + 6, additive deltas


class TestIpv6SpellingsCoalesceIntoOneGroup:
    """ADR-0004 point 3: entries are grouped by parsed `Address`, not IP text.

    Two different textual spellings of the same IPv6 address must coalesce
    into one group ("the two spellings of one IPv6 address are one
    group") -- a coder who coalesces by naive string equality of the raw
    `ip` field, rather than by parsed `Address`, would wrongly publish two
    messages here instead of one.
    """

    def test_two_spellings_of_same_address_coalesce_into_one_summed_message(self) -> None:
        client, bus = _build_app()
        compact = "2001:db8::1"
        expanded = "2001:0DB8:0000:0000:0000:0000:0000:0001"
        observations = [
            {"ip": compact, "request_count": 3},
            {"ip": expanded, "request_count": 4},
        ]
        response = client.post(
            "/v1/observations",
            json=_body(sequence=1, observations=observations),
            headers=_headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN),
        )

        assert response.status_code == 202

        decoded = _decoded_records(bus)
        assert len(decoded) == 1

        key, envelope = decoded[0]
        expected_ip = _canonical(compact)
        assert key is not None
        assert key.decode("utf-8") == expected_ip
        entry = _as_request_observation(envelope).observations[0]
        assert entry.request_count == 7


class TestDistinctEventIdsAcrossFanOut:
    """ADR-0004 point 4: `subject` makes every split message's event_id distinct."""

    def test_event_ids_for_a_multi_ip_fan_out_are_pairwise_distinct(self) -> None:
        client, bus = _build_app()
        observations = [
            {"ip": "172.16.0.1", "request_count": 1},
            {"ip": "172.16.0.2", "request_count": 2},
            {"ip": "172.16.0.3", "request_count": 3},
        ]
        response = client.post(
            "/v1/observations",
            json=_body(sequence=1, observations=observations),
            headers=_headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN),
        )
        assert response.status_code == 202

        decoded = _decoded_records(bus)
        assert len(decoded) == len(observations)
        event_ids = [envelope.event_id for _key, envelope in decoded]
        assert len(event_ids) == len(set(event_ids))


class TestDuplicateSequenceDoesNotRepublish:
    """docs/protocol/observation-v1.md: `200 | Duplicate` -- no action taken.

    Dedup identity is `(agent_id, sequence)` keyed by the *authenticated*
    `agent_id` (the `X-Agent-Id` header value `require_agent` resolves),
    not necessarily the request body's own `agent_id` field -- every body
    below sets `agent_id` equal to the authenticated identity, consistent
    with `auth/middleware.py`'s documented integration contract that #32
    must reject the two disagreeing rather than silently trusting the body.
    """

    def test_second_request_reusing_a_seen_sequence_is_a_no_op(self) -> None:
        client, bus = _build_app()
        headers = _headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)
        body = _body(sequence=1, observations=[{"ip": "10.1.1.1", "request_count": 3}])

        first = client.post("/v1/observations", json=body, headers=headers)
        assert first.status_code == 202
        after_first = len(_topic_records(bus))
        assert after_first == 1

        second = client.post("/v1/observations", json=body, headers=headers)

        assert second.status_code == 200
        # No republish, not even a "harmless" resend of the same message.
        assert len(_topic_records(bus)) == after_first
