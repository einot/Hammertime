"""HTTP-level status-code coverage for POST /v1/observations, /healthz, /metrics.

Spec: section 4, section 36, section 37

`test_validation.py` covers the `validation/*` functions in isolation;
this file exercises the actual route handler (check ordering, HTTP status
mapping, and the two resource-exhaustion regressions security-auditor and
reviewer found on this epic: an oversized body being fully buffered before
rejection, and RecursionError/UnicodeDecodeError escaping json.loads as an
unhandled 500 instead of a clean 400) via FastAPI's `TestClient`.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from hammertime.core.auth.tokens import hash_token
from hammertime.ingest.app import create_app
from hammertime.ingest.auth.agents import AgentRecord, AgentRegistry
from hammertime.ingest.config import IngestSettings

# ADR-0006: AgentRegistry now requires a deployment key and stores hashed
# credentials, not plaintext tokens -- a fixed 32-byte test key, exactly
# as services/ingest/.../tests/test_auth.py uses, never a real env var or
# file on disk.
_AGENT_TOKEN_KEY = b"0" * 32

_CONFIG_PATH = Path(__file__).parents[6] / "config" / "detection.v1.json"
_BUCKET_SECONDS = json.loads(_CONFIG_PATH.read_text())["bucket_seconds"]

# issue #32 put auth/rate-limit/dedup/publish ahead of the validation layer
# this file exercises -- every request below must be authenticated (and not
# rate-limited) to actually reach the status codes these tests check for.
_AGENT_ID = "edge-17"
_AGENT_TOKEN = "test-token"

# ADR-0007 (issue #40) / ADR-0008 (issue #41) each add several required
# IngestSettings fields (config.py has no dataclass-level defaults of its
# own -- load_settings() is where env-var defaults are applied); every
# value below is deliberately generous/permissive so none of this file's
# existing status-code assertions can be accidentally defeated by tripping
# a new throttle or budget this file isn't testing. See
# test_auth_throttle.py and test_ratelimit.py/test_pipeline.py's
# observation-budget coverage for the tests that actually exercise these.
_SETTINGS = IngestSettings(
    host="127.0.0.1",
    port=0,
    max_body_bytes=1_048_576,
    max_observations=10_000,
    detection_config_path=_CONFIG_PATH,
    rate_limit_rps=1_000,
    agents_path=Path("unused -- agent_registry is injected directly below"),
    bus_kind="memory",
    bus_brokers="",
    store_kind="memory",
    redis_url="",
    observation_rate_limit_eps=1_000_000,
    observation_burst=1_000_000,
    auth_failure_rate_per_min=1_000_000,
    auth_failure_burst=1_000_000,
    auth_failure_agent_rate_per_min=1_000_000,
    auth_failure_agent_burst=1_000_000,
    trusted_proxy_hops=0,
)


def _client() -> TestClient:
    # create_app(settings=...) avoids depending on process environment
    # variables, per app.py's own docstring. agent_registry is injected
    # directly (rather than read from agents_path) so this file needs no
    # config/agents.v2.json fixture on disk; dedup_store/bus are left
    # unset so create_app builds a fresh in-memory one per client (no
    # cross-test state, since store_kind/bus_kind above are both "memory").
    token_hash = bytes.fromhex(hash_token(_AGENT_TOKEN, key=_AGENT_TOKEN_KEY))
    registry = AgentRegistry.from_records(
        [AgentRecord(agent_id=_AGENT_ID, token_hash=token_hash, rate_limit_rps=None)],
        key=_AGENT_TOKEN_KEY,
    )
    return TestClient(create_app(_SETTINGS, agent_registry=registry))


def _auth_headers() -> dict[str, str]:
    return {"X-Agent-Id": _AGENT_ID, "Authorization": f"Bearer {_AGENT_TOKEN}"}


def _valid_body(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "agent_id": _AGENT_ID,
        "sequence": 1,
        "window_start": "2026-09-14T10:00:00Z",
        "window_seconds": 60,
        "observations": [{"ip": "192.168.1.42", "request_count": 183}],
    }
    doc.update(overrides)
    return doc


class TestHappyPath:
    def test_valid_observation_is_accepted(self) -> None:
        with _client() as client:
            response = client.post("/v1/observations", json=_valid_body(), headers=_auth_headers())
        assert response.status_code == 202
        assert response.json() == {"status": "accepted"}

    def test_healthz_is_ok(self) -> None:
        with _client() as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_metrics_is_reachable(self) -> None:
        with _client() as client:
            response = client.get("/metrics")
        assert response.status_code == 200


class TestSchemaAndAddressRejection:
    def test_malformed_ip_is_rejected(self) -> None:
        body = _valid_body(observations=[{"ip": "not-an-ip", "request_count": 1}])
        with _client() as client:
            response = client.post("/v1/observations", json=body, headers=_auth_headers())
        assert response.status_code == 400

    def test_client_asserted_state_is_rejected(self) -> None:
        # Spec section 36's named example: an agent must never be able to
        # assert "IP = HOT" directly.
        body = _valid_body(state="HOT")
        with _client() as client:
            response = client.post("/v1/observations", json=body, headers=_auth_headers())
        assert response.status_code == 400

    def test_unaligned_window_start_is_rejected(self) -> None:
        assert _BUCKET_SECONDS != 1  # otherwise every window is "aligned"
        body = _valid_body(window_start="2026-09-14T10:00:00.5Z")
        with _client() as client:
            response = client.post("/v1/observations", json=body, headers=_auth_headers())
        assert response.status_code == 400


class TestSizeLimits:
    def test_oversized_observations_array_is_rejected_with_413(self) -> None:
        body = _valid_body(observations=[{"ip": "10.0.0.1", "request_count": 1}] * 10_001)
        with _client() as client:
            response = client.post("/v1/observations", json=body, headers=_auth_headers())
        assert response.status_code == 413

    def test_oversized_body_is_rejected_with_413_not_buffered_unbounded(self) -> None:
        # Regression: check_body_size used to run only after
        # `await request.body()` had already buffered the whole body.
        # _read_body_within_limit must reject this while streaming, well
        # before accumulating the full padded payload.
        oversized = json.dumps(_valid_body(agent_id="a" * 2_000_000))  # > _SETTINGS.max_body_bytes
        with _client() as client:
            response = client.post(
                "/v1/observations",
                content=oversized.encode("utf-8"),
                headers={"content-type": "application/json", **_auth_headers()},
            )
        assert response.status_code == 413


class TestMalformedBytesDoNotCrash:
    # Regression: these two failure modes previously escaped json.loads as
    # unhandled exceptions (RecursionError, UnicodeDecodeError), producing
    # a generic 500 instead of the documented 400.

    def test_pathologically_nested_json_is_rejected_with_400_not_500(self) -> None:
        deeply_nested = b"[" * 10_000 + b"]" * 10_000
        with _client() as client:
            response = client.post(
                "/v1/observations",
                content=deeply_nested,
                headers={"content-type": "application/json", **_auth_headers()},
            )
        assert response.status_code == 400

    def test_invalid_utf8_body_is_rejected_with_400_not_500(self) -> None:
        with _client() as client:
            response = client.post(
                "/v1/observations",
                content=b'\xff\xfe{"agent_id": "a"',
                headers={"content-type": "application/json", **_auth_headers()},
            )
        assert response.status_code == 400

    def test_oversized_integer_literal_is_rejected_with_400_not_500(self) -> None:
        # Regression: CPython's int-string conversion limit (default 4300
        # digits) makes json.loads raise a bare ValueError -- neither
        # json.JSONDecodeError nor UnicodeDecodeError -- for a body
        # containing a many-thousand-digit integer literal, which used to
        # fall through the handler's exception guards as an unhandled 500.
        # Built by text substitution, not `int("9" * 5000)`: constructing
        # that Python int would itself hit the same conversion limit before
        # the request body even exists.
        huge_digits = "9" * 5000
        oversized_literal = json.dumps(_valid_body()).replace(
            '"sequence": 1', f'"sequence": {huge_digits}', 1
        )
        with _client() as client:
            response = client.post(
                "/v1/observations",
                content=oversized_literal.encode("utf-8"),
                headers={"content-type": "application/json", **_auth_headers()},
            )
        assert response.status_code == 400
