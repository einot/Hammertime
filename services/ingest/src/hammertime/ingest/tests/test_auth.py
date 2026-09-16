"""Agent authentication & authorization (spec section 36, section 36.1-36.4).

Spec: section 36 ("At minimum: TLS, agent authentication, authorization,
replay protection, request size limits, rate limits, schema validation."
"An untrusted agent MUST NOT be able to arbitrarily declare `IP = HOT`.")
ADR: docs/adr/0006-hashed-agent-credentials.md.

docs/protocol/observation-v1.md's response table, per ADR-0007 decision 4
(the ADR's own "one externally visible behaviour change"): `401 |
Authentication failed -- missing, malformed, unknown or wrong credential`,
uniformly, with body `{"detail": "invalid agent credentials"}` and
`WWW-Authenticate: Bearer` (see `test_auth_throttle.py` for those two
assertions in detail); `403` is retained for exactly one case, a
*correct* credential presented for a registered but disabled agent.

The `AgentAuthError` subclass hierarchy below (`AuthenticationError` /
`AuthorizationError`, i.e. the "401 group" / "403 group" comments on
`UnknownAgentError` / `InvalidCredentialError` / `AgentDisabledError`)
is an internal exception-classification grouping, not a 1:1 mirror of the
current wire status split -- `InvalidCredentialError` is still classified
under `AuthorizationError` for that internal grouping even though
`require_agent` now answers a wrong credential with `401` on the wire, per
ADR-0007. Only `AgentDisabledError` still surfaces as `403` at the HTTP
layer; see `TestRequireAgentDependency` below for the wire-level
assertions.

This file supersedes the pre-ADR-0006 version of itself, which built
`AgentRecord`s from a plaintext `token: str` field. ADR-0006 removes that
field outright (`AgentRecord.token` is gone; `AgentRecord.token_hash:
bytes` replaces it) and makes `AgentRegistry` no longer constructible
without a deployment key. Written blind to the implementation, per this
repo's test-author convention -- the surface below is reconciled from the
ADR text, not read off the coder's code:

* `hammertime.ingest.auth`: unchanged exception hierarchy --
  `AgentAuthError` (base) -> `AuthenticationError` (401 group) ->
  `UnknownAgentError`; `AuthorizationError` (403 group) ->
  `InvalidCredentialError`, `AgentDisabledError`.
* `hammertime.ingest.auth.agents.AgentRecord`: `agent_id`, `token_hash:
  bytes`, optional `previous_token_hash: bytes | None`, optional
  `previous_token_expires_at` (type deliberately not pinned by this file
  for records built directly -- see below), `enabled` (default `True`),
  `rate_limit_rps` (default `None`).
* `AgentRegistry.from_records(records, *, key, clock=None)` -- ADR-0006
  consequences section: "`AgentRegistry.from_records` gains the key (and
  an optional clock)". Guessed as keyword-only `key`/`clock` to mirror
  `hash_token(token, key=...)`'s style; flagged in the handback report.
* `load_agent_registry_document(document, *, key, clock=None)` /
  `load_agent_registry_file(path, *, key, clock=None)` -- guessed as
  threading the same `key`/`clock` through the document-parsing entry
  points, since the loader must recompute `key_id` and evaluate
  `previous_token_expires_at` at load time (ADR-0006 decision 2 and 4).
* `.authenticate(agent_id, token) -> AgentRecord` -- unchanged Request-
  agnostic core logic and unchanged exceptions.
* `hammertime.ingest.auth.middleware.require_agent(registry)` -- unchanged
  FastAPI dependency; wire-level behaviour (headers -> status code) is
  unaffected by how the registry stores credentials internally.

Tests below that only need a *current* `token_hash` build `AgentRecord`
directly (its type, `bytes`, is pinned exactly by ADR-0006 decision 2).
Tests that involve `previous_token_hash`/`previous_token_expires_at`
instead go through `load_agent_registry_document`, so this file never has
to guess the in-memory representation of the expiry field -- only the
wire/document representation (an RFC 3339 string, pinned by
`schemas/agent_registry.v2.json`) is exercised directly.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hammertime.core.auth.tokens import derive_key_id, hash_token
from hammertime.core.errors import ConfigurationError, HammertimeError
from hammertime.core.time.clock import ManualClock
from hammertime.ingest.auth import (
    AgentAuthError,
    AgentDisabledError,
    AuthenticationError,
    AuthorizationError,
    InvalidCredentialError,
    UnknownAgentError,
)
from hammertime.ingest.auth.agents import (
    AgentRecord,
    AgentRegistry,
    load_agent_registry_document,
    load_agent_registry_file,
)
from hammertime.ingest.auth.middleware import require_agent

KNOWN_AGENT_ID = "edge-17"
KNOWN_AGENT_TOKEN = "s3cr3t-token-value"

# A fixed 32-byte deployment key, decoded form -- tests never touch a real
# HAMMERTIME_INGEST_AGENT_TOKEN_KEY env var or a file on disk (mirrors the
# pre-ADR-0006 file's convention of never needing either).
TEST_KEY = b"0" * 32
TEST_KEY_ID = derive_key_id(TEST_KEY)

# Comfortably outside "now" in either direction, for previous-hash expiry
# fields where a test only needs "already expired" / "not expiring any time
# soon" rather than a specific instant.
_FAR_PAST_RFC3339 = "2000-01-01T00:00:00Z"
_FAR_FUTURE_RFC3339 = "2999-01-01T00:00:00Z"


def _hash_hex(token: str, key: bytes = TEST_KEY) -> str:
    return hash_token(token, key=key)


def _hash_bytes(token: str, key: bytes = TEST_KEY) -> bytes:
    return bytes.fromhex(hash_token(token, key=key))


def _rfc3339(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _known_agent(**overrides: object) -> AgentRecord:
    fields: dict[str, object] = {
        "agent_id": KNOWN_AGENT_ID,
        "token_hash": _hash_bytes(KNOWN_AGENT_TOKEN),
        "enabled": True,
    }
    fields.update(overrides)
    return AgentRecord(**fields)  # type: ignore[arg-type]


def _registry(
    *records: AgentRecord, key: bytes = TEST_KEY, clock: ManualClock | None = None
) -> AgentRegistry:
    return AgentRegistry.from_records(records, key=key, clock=clock)


def _agent_entry(token: str, key: bytes = TEST_KEY, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {"token_hash": _hash_hex(token, key=key)}
    entry.update(overrides)
    return entry


def _document(
    agents: dict[str, dict[str, object]] | None = None,
    *,
    registry_version: object = 2,
    hash_algorithm: object = "hmac-sha256",
    key_id: object = None,
) -> dict[str, Any]:
    return {
        "registry_version": registry_version,
        "hash_algorithm": hash_algorithm,
        "key_id": key_id if key_id is not None else TEST_KEY_ID,
        "agents": agents if agents is not None else {},
    }


class TestAuthenticateHappyPath:
    def test_known_agent_with_correct_credential_is_authenticated(self) -> None:
        registry = _registry(_known_agent())

        result = registry.authenticate(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)

        assert result.agent_id == KNOWN_AGENT_ID

    def test_authenticated_result_surfaces_the_registered_rate_limit(self) -> None:
        # Scope note: this only checks the *value* is carried through as
        # data -- no enforcement is exercised or implied (issue #29).
        registry = _registry(_known_agent(rate_limit_rps=50))

        result = registry.authenticate(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)

        assert result.rate_limit_rps == 50


class TestUnknownAgent:
    def test_unregistered_agent_id_is_rejected(self) -> None:
        registry = _registry(_known_agent())

        with pytest.raises(UnknownAgentError):
            registry.authenticate("no-such-agent", "irrelevant-token")

    def test_unknown_agent_error_is_in_the_401_group(self) -> None:
        assert issubclass(UnknownAgentError, AuthenticationError)

    def test_empty_registry_rejects_any_agent_id(self) -> None:
        registry = _registry()

        with pytest.raises(UnknownAgentError):
            registry.authenticate(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)


class TestIncorrectCredential:
    @pytest.mark.parametrize(
        "bad_token",
        [
            "wrong-token",
            "",
            "   ",
            KNOWN_AGENT_TOKEN.upper(),  # case must matter -- not a fuzzy match
            KNOWN_AGENT_TOKEN + "\x00extra",  # malformed/garbage-shaped value
        ],
    )
    def test_known_agent_with_a_bad_credential_is_rejected(self, bad_token: str) -> None:
        registry = _registry(_known_agent())

        with pytest.raises(InvalidCredentialError):
            registry.authenticate(KNOWN_AGENT_ID, bad_token)

    def test_invalid_credential_error_is_in_the_403_group(self) -> None:
        # This is the internal exception-classification grouping only (see
        # this module's docstring) -- as of ADR-0007, a wrong credential for
        # a known agent actually answers `401` on the wire
        # (`test_wrong_credential_is_401` below), not `403`. The
        # `AuthorizationError` grouping itself is unchanged by ADR-0007:
        # `InvalidCredentialError` still means "known identity, denied
        # access, not 'who are you'" as a matter of internal classification,
        # even though `require_agent` no longer maps that classification
        # 1:1 onto the HTTP status it returns.
        assert issubclass(InvalidCredentialError, AuthorizationError)

    def test_non_ascii_token_is_rejected_cleanly_not_a_crash(self) -> None:
        # ADR-0006: comparison now happens on fixed-length HMAC digests
        # (bytes), which is exactly as timing-safe as the old raw-token
        # comparison and removes the old str/latin-1 compare_digest hazard
        # -- but encoding the *presented* token to utf-8 before hashing it
        # must still not crash for a non-ASCII value.
        registry = _registry(_known_agent())

        with pytest.raises(InvalidCredentialError):
            registry.authenticate(KNOWN_AGENT_ID, "caf\xe9-not-the-token")


class TestDisabledAgent:
    def test_disabled_agent_is_rejected_even_with_the_correct_credential(self) -> None:
        registry = _registry(_known_agent(enabled=False))

        with pytest.raises(AgentDisabledError):
            registry.authenticate(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)

    def test_agent_disabled_error_is_in_the_403_group(self) -> None:
        assert issubclass(AgentDisabledError, AuthorizationError)

    def test_disabled_agent_with_a_wrong_credential_is_still_rejected(self) -> None:
        # Whichever specific error fires first between "wrong secret" and
        # "disabled" is left to the coder -- only that *some* auth error
        # fires, never a successful authentication.
        registry = _registry(_known_agent(enabled=False))

        with pytest.raises(AgentAuthError):
            registry.authenticate(KNOWN_AGENT_ID, "wrong-token")


class TestRotation:
    # ADR-0006 decision 4 / spec 36.3: at most one previous_token_hash,
    # accepted alongside token_hash until previous_token_expires_at.
    # Built via load_agent_registry_document rather than AgentRecord
    # directly, so this file never has to guess the in-memory type of
    # previous_token_expires_at -- only the RFC 3339 document
    # representation (schemas/agent_registry.v2.json) is exercised.

    def _rotated_registry(
        self, *, current_token: str, previous_token: str, expires_at_epoch: int, clock: ManualClock
    ) -> AgentRegistry:
        entry = _agent_entry(current_token)
        entry["previous_token_hash"] = _hash_hex(previous_token)
        entry["previous_token_expires_at"] = _rfc3339(expires_at_epoch)
        document = _document(agents={KNOWN_AGENT_ID: entry})
        return load_agent_registry_document(document, key=TEST_KEY, clock=clock)

    def test_current_token_authenticates(self) -> None:
        clock = ManualClock(initial=1_700_000_000)
        registry = self._rotated_registry(
            current_token="new-token",
            previous_token="old-token",
            expires_at_epoch=clock.now() + 3600,
            clock=clock,
        )

        result = registry.authenticate(KNOWN_AGENT_ID, "new-token")

        assert result.agent_id == KNOWN_AGENT_ID

    def test_previous_token_authenticates_before_expiry(self) -> None:
        clock = ManualClock(initial=1_700_000_000)
        registry = self._rotated_registry(
            current_token="new-token",
            previous_token="old-token",
            expires_at_epoch=clock.now() + 3600,
            clock=clock,
        )

        result = registry.authenticate(KNOWN_AGENT_ID, "old-token")

        assert result.agent_id == KNOWN_AGENT_ID

    def test_previous_token_stops_authenticating_after_expiry(self) -> None:
        clock = ManualClock(initial=1_700_000_000)
        expires_at_epoch = clock.now() + 3600
        registry = self._rotated_registry(
            current_token="new-token",
            previous_token="old-token",
            expires_at_epoch=expires_at_epoch,
            clock=clock,
        )

        clock.set(expires_at_epoch - 1)
        registry.authenticate(KNOWN_AGENT_ID, "old-token")  # still within the window

        clock.set(expires_at_epoch + 1)
        with pytest.raises(AgentAuthError):
            registry.authenticate(KNOWN_AGENT_ID, "old-token")

    def test_current_token_keeps_working_regardless_of_previous_hash_expiry(self) -> None:
        clock = ManualClock(initial=1_700_000_000)
        expires_at_epoch = clock.now() + 3600
        registry = self._rotated_registry(
            current_token="new-token",
            previous_token="old-token",
            expires_at_epoch=expires_at_epoch,
            clock=clock,
        )

        clock.set(expires_at_epoch + 1_000_000)  # long past the previous hash's expiry
        result = registry.authenticate(KNOWN_AGENT_ID, "new-token")

        assert result.agent_id == KNOWN_AGENT_ID

    def test_a_wrong_token_is_still_rejected_when_a_previous_hash_is_present(self) -> None:
        clock = ManualClock(initial=1_700_000_000)
        registry = self._rotated_registry(
            current_token="new-token",
            previous_token="old-token",
            expires_at_epoch=clock.now() + 3600,
            clock=clock,
        )

        with pytest.raises(InvalidCredentialError):
            registry.authenticate(KNOWN_AGENT_ID, "some-other-token")


class TestTimingSafetySmokeTest:
    # Best-effort only -- pytest cannot reliably assert timing. This just
    # confirms that the presence of an (unexpired) previous_token_hash
    # doesn't change *which exception type* a wrong token is rejected
    # with, i.e. no code path short-circuits differently depending on
    # whether a previous hash exists (ADR-0006 decision 4: "Both
    # comparisons are evaluated before the results are combined, so the
    # number of constant-time comparisons depends on the registry, never
    # on the presented token").

    def test_wrong_token_rejected_the_same_way_with_and_without_a_previous_hash(self) -> None:
        clock = ManualClock(initial=1_700_000_000)

        without_previous = load_agent_registry_document(
            _document(agents={KNOWN_AGENT_ID: _agent_entry(KNOWN_AGENT_TOKEN)}),
            key=TEST_KEY,
            clock=clock,
        )

        entry_with_previous = _agent_entry(KNOWN_AGENT_TOKEN)
        entry_with_previous["previous_token_hash"] = _hash_hex("some-old-token")
        entry_with_previous["previous_token_expires_at"] = _rfc3339(clock.now() + 3600)
        with_previous = load_agent_registry_document(
            _document(agents={KNOWN_AGENT_ID: entry_with_previous}), key=TEST_KEY, clock=clock
        )

        with pytest.raises(InvalidCredentialError):
            without_previous.authenticate(KNOWN_AGENT_ID, "wrong-token")
        with pytest.raises(InvalidCredentialError):
            with_previous.authenticate(KNOWN_AGENT_ID, "wrong-token")


class TestRegistryExposesPerAgentData:
    # agents.py's own docstring: "identity, credentials, enabled/disabled,
    # per-agent limits." Enforcement of the limit is issue #29's job; this
    # only checks the registry surfaces the value it was constructed with.

    def test_registry_get_returns_the_matching_record(self) -> None:
        record = _known_agent(rate_limit_rps=25)
        registry = _registry(record)

        fetched = registry.get(KNOWN_AGENT_ID)

        assert fetched is not None
        assert fetched.agent_id == KNOWN_AGENT_ID
        assert fetched.rate_limit_rps == 25
        assert fetched.enabled is True

    def test_registry_get_returns_none_for_an_unknown_agent_id(self) -> None:
        registry = _registry(_known_agent())

        assert registry.get("no-such-agent") is None

    def test_registry_constructed_from_an_arbitrary_iterable(self) -> None:
        # from_records takes any Iterable[AgentRecord], not specifically a
        # list -- exercised with a generator to pin that down.
        def _records() -> Iterable[AgentRecord]:
            yield _known_agent(agent_id="edge-1")
            yield _known_agent(agent_id="edge-2", token_hash=_hash_bytes("other-token"))

        registry = AgentRegistry.from_records(_records(), key=TEST_KEY)

        assert registry.get("edge-1") is not None
        assert registry.get("edge-2") is not None


class TestErrorHierarchy:
    # Mirrors validation/__init__.py's pattern of grouping exceptions by
    # the HTTP status they're meant to map to (RequestLimitExceededError
    # -> 413), so a future request handler can catch one base class per
    # status code rather than enumerating every leaf exception.

    def test_auth_error_is_a_hammertime_error(self) -> None:
        assert issubclass(AgentAuthError, HammertimeError)

    def test_authentication_and_authorization_errors_are_auth_errors(self) -> None:
        assert issubclass(AuthenticationError, AgentAuthError)
        assert issubclass(AuthorizationError, AgentAuthError)

    def test_authentication_and_authorization_groups_are_disjoint(self) -> None:
        # A 401-mapping error must never also be a 403-mapping error (or
        # vice versa) -- otherwise a single `except` clause could catch
        # the wrong status group.
        assert not issubclass(AuthenticationError, AuthorizationError)
        assert not issubclass(AuthorizationError, AuthenticationError)


def _app(registry: AgentRegistry) -> FastAPI:
    """A minimal app exposing `require_agent` on one route, for TestClient.

    Exercises the actual wire-level entry point (headers -> HTTP status)
    the same way `test_routes.py` exercises `api/routes.py` in Epic #3,
    rather than calling a private header-parsing helper directly.
    """
    app = FastAPI()

    @app.get("/whoami")
    def whoami(agent_id: str = Depends(require_agent(registry))) -> dict[str, str]:
        return {"agent_id": agent_id}

    return app


class TestRequireAgentDependency:
    # Wire-level behaviour is unaffected by ADR-0006: the caller still
    # presents the plaintext token over the wire; only server-side storage
    # changed from plaintext to a keyed hash.

    def test_correct_credential_succeeds_and_returns_the_agent_id(self) -> None:
        client = TestClient(_app(_registry(_known_agent())))

        response = client.get(
            "/whoami",
            headers={"X-Agent-Id": KNOWN_AGENT_ID, "Authorization": f"Bearer {KNOWN_AGENT_TOKEN}"},
        )

        assert response.status_code == 200
        assert response.json() == {"agent_id": KNOWN_AGENT_ID}

    def test_missing_agent_id_header_is_401(self) -> None:
        client = TestClient(_app(_registry(_known_agent())))

        response = client.get("/whoami", headers={"Authorization": f"Bearer {KNOWN_AGENT_TOKEN}"})

        assert response.status_code == 401

    def test_missing_authorization_header_is_401(self) -> None:
        client = TestClient(_app(_registry(_known_agent())))

        response = client.get("/whoami", headers={"X-Agent-Id": KNOWN_AGENT_ID})

        assert response.status_code == 401

    def test_authorization_header_missing_the_bearer_scheme_is_401(self) -> None:
        client = TestClient(_app(_registry(_known_agent())))

        response = client.get(
            "/whoami",
            headers={"X-Agent-Id": KNOWN_AGENT_ID, "Authorization": KNOWN_AGENT_TOKEN},
        )

        assert response.status_code == 401

    def test_bearer_scheme_with_no_token_is_401(self) -> None:
        client = TestClient(_app(_registry(_known_agent())))

        response = client.get(
            "/whoami", headers={"X-Agent-Id": KNOWN_AGENT_ID, "Authorization": "Bearer "}
        )

        assert response.status_code == 401

    def test_unknown_agent_id_is_401(self) -> None:
        client = TestClient(_app(_registry(_known_agent())))

        response = client.get(
            "/whoami",
            headers={
                "X-Agent-Id": "no-such-agent",
                "Authorization": f"Bearer {KNOWN_AGENT_TOKEN}",
            },
        )

        assert response.status_code == 401

    def test_wrong_credential_is_401(self) -> None:
        # ADR-0007 decision 4 ("the one externally visible behaviour change
        # in this ADR"): a wrong credential for a *known* agent is now 401
        # with the uniform body, not 403 -- 403 is reserved for a correct
        # credential presented to a disabled agent (see test_disabled_agent_is_403).
        client = TestClient(_app(_registry(_known_agent())))

        response = client.get(
            "/whoami",
            headers={"X-Agent-Id": KNOWN_AGENT_ID, "Authorization": "Bearer wrong-token"},
        )

        assert response.status_code == 401

    def test_disabled_agent_is_403(self) -> None:
        client = TestClient(_app(_registry(_known_agent(enabled=False))))

        response = client.get(
            "/whoami",
            headers={"X-Agent-Id": KNOWN_AGENT_ID, "Authorization": f"Bearer {KNOWN_AGENT_TOKEN}"},
        )

        assert response.status_code == 403

    def test_non_ascii_authorization_header_is_401_not_a_crash(self) -> None:
        # Same regression as TestIncorrectCredential, exercised through the
        # actual HTTP dependency rather than calling authenticate() directly.
        # httpx's TestClient rejects a plain non-ASCII str header value
        # outright (it insists on ascii-encoding str header values itself),
        # so the header value is passed pre-encoded as latin-1 bytes here --
        # matching what Starlette actually hands the app for a raw
        # Authorization header byte >= 0x80 on the wire.
        #
        # ADR-0007 decision 4: a bad credential for a known agent is now 401
        # (uniform body), not 403 -- see test_wrong_credential_is_401.
        client = TestClient(_app(_registry(_known_agent())))

        response = client.get(
            "/whoami",
            headers={
                "X-Agent-Id": KNOWN_AGENT_ID,
                "Authorization": "Bearer caf\xe9-not-the-token".encode("latin-1"),
            },
        )

        assert response.status_code == 401


class TestRegistryDocumentEnvelopeValidation:
    # load_agent_registry_document is the production JSON-loading path
    # (load_agent_registry, not exercised here to avoid a real env var /
    # disk dependency) -- these tests target it directly, against the v2
    # envelope (schemas/agent_registry.v2.json, ADR-0006 decision 2, spec
    # 36.2).

    def test_document_missing_registry_version_is_rejected_naming_the_migration_tool(
        self,
    ) -> None:
        document = _document(agents={"edge-1": _agent_entry("token-one")})
        del document["registry_version"]

        with pytest.raises(ConfigurationError, match="hammertime-agent-token migrate"):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_document_with_wrong_registry_version_is_rejected(self) -> None:
        document = _document(agents={"edge-1": _agent_entry("token-one")}, registry_version=1)

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_a_bare_v1_style_document_is_rejected_naming_the_migration_tool(self) -> None:
        # The v1 format had no envelope at all: agent_id -> {"token": ...}
        # directly at the top level.
        document = {"edge-1": {"token": "plaintext-value", "enabled": True}}

        with pytest.raises(ConfigurationError, match="hammertime-agent-token migrate"):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_unknown_hash_algorithm_is_rejected(self) -> None:
        document = _document(agents={"edge-1": _agent_entry("token-one")}, hash_algorithm="bcrypt")

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_key_id_mismatch_is_rejected_naming_both_fingerprints(self) -> None:
        other_key = b"1" * 32
        mismatched_key_id = derive_key_id(other_key)
        document = _document(agents={"edge-1": _agent_entry("token-one")}, key_id=mismatched_key_id)

        with pytest.raises(ConfigurationError) as exc_info:
            load_agent_registry_document(document, key=TEST_KEY)

        message = str(exc_info.value)
        assert mismatched_key_id in message
        assert TEST_KEY_ID in message

    def test_unknown_envelope_level_key_is_rejected(self) -> None:
        document = _document(agents={"edge-1": _agent_entry("token-one")})
        document["unexpected_field"] = "surprise"

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)


class TestRegistryRecordValidation:
    def test_record_with_plaintext_token_key_is_rejected_naming_the_migration_tool(self) -> None:
        document = _document(agents={"edge-1": {"token": "plaintext-value"}})

        with pytest.raises(ConfigurationError, match="hammertime-agent-token migrate"):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_malformed_token_hash_is_rejected(self) -> None:
        document = _document(agents={"edge-1": {"token_hash": "not-64-hex-characters"}})

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_two_agents_sharing_the_same_current_hash_is_rejected(self) -> None:
        shared_entry = _agent_entry("shared-token")
        document = _document(agents={"edge-1": dict(shared_entry), "edge-2": dict(shared_entry)})

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_distinct_tokens_across_agents_are_accepted(self) -> None:
        document = _document(
            agents={
                "edge-1": _agent_entry("token-one"),
                "edge-2": _agent_entry("token-two"),
            }
        )

        registry = load_agent_registry_document(document, key=TEST_KEY)

        assert registry.get("edge-1") is not None
        assert registry.get("edge-2") is not None

    def test_one_agents_previous_hash_equal_to_anothers_current_hash_is_rejected(self) -> None:
        document = _document(
            agents={
                "edge-1": _agent_entry("token-one"),
                "edge-2": {
                    **_agent_entry("token-two"),
                    "previous_token_hash": _hash_hex("token-one"),
                    "previous_token_expires_at": _FAR_FUTURE_RFC3339,
                },
            }
        )

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_previous_token_hash_equal_to_its_own_token_hash_is_rejected(self) -> None:
        entry = _agent_entry("token-one")
        entry["previous_token_hash"] = entry["token_hash"]
        entry["previous_token_expires_at"] = _FAR_FUTURE_RFC3339
        document = _document(agents={"edge-1": entry})

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_previous_token_hash_without_expiry_is_rejected(self) -> None:
        entry = _agent_entry("token-one")
        entry["previous_token_hash"] = _hash_hex("old-token")
        document = _document(agents={"edge-1": entry})

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_previous_token_expires_at_without_hash_is_rejected(self) -> None:
        entry = _agent_entry("token-one")
        entry["previous_token_expires_at"] = _FAR_FUTURE_RFC3339
        document = _document(agents={"edge-1": entry})

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_unknown_record_level_key_is_rejected(self) -> None:
        entry = _agent_entry("token-one")
        entry["unexpected_field"] = "surprise"
        document = _document(agents={"edge-1": entry})

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    @pytest.mark.parametrize("rate_limit_rps", [0, -1])
    def test_non_positive_rate_limit_rps_is_rejected_at_load_time(
        self, rate_limit_rps: int
    ) -> None:
        # Regression: RateLimiter.check() raises a bare ValueError for a
        # non-positive limit_rps (ratelimit/__init__.py) -- an unhandled
        # 500 on every request for this agent if a config typo like this
        # reached the live pipeline instead of failing at startup.
        document = _document(
            agents={"edge-1": _agent_entry("token-one", rate_limit_rps=rate_limit_rps)}
        )

        with pytest.raises(ConfigurationError, match="rate_limit_rps"):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_positive_rate_limit_rps_is_accepted(self) -> None:
        document = _document(agents={"edge-1": _agent_entry("token-one", rate_limit_rps=5)})

        registry = load_agent_registry_document(document, key=TEST_KEY)

        record = registry.get("edge-1")
        assert record is not None
        assert record.rate_limit_rps == 5


class TestExpiredPreviousHashAtLoadTime:
    # ADR-0006 decision 4 / spec 36.3: "An already-past expiry at load time
    # is not a startup failure: the loader drops the previous hash and
    # warns." Verified behaviourally (can the old token still authenticate
    # after load?) since this file has no visibility into internal state.

    def test_load_succeeds_with_an_already_expired_previous_hash(self) -> None:
        entry = _agent_entry("current-token")
        entry["previous_token_hash"] = _hash_hex("old-token")
        entry["previous_token_expires_at"] = _FAR_PAST_RFC3339
        document = _document(agents={"edge-1": entry})

        registry = load_agent_registry_document(document, key=TEST_KEY)  # must not raise

        assert registry.get("edge-1") is not None

    def test_the_old_token_no_longer_authenticates_after_such_a_load(self) -> None:
        entry = _agent_entry("current-token")
        entry["previous_token_hash"] = _hash_hex("old-token")
        entry["previous_token_expires_at"] = _FAR_PAST_RFC3339
        document = _document(agents={"edge-1": entry})

        registry = load_agent_registry_document(document, key=TEST_KEY)

        with pytest.raises(AgentAuthError):
            registry.authenticate("edge-1", "old-token")

    def test_the_current_token_still_authenticates_after_such_a_load(self) -> None:
        entry = _agent_entry("current-token")
        entry["previous_token_hash"] = _hash_hex("old-token")
        entry["previous_token_expires_at"] = _FAR_PAST_RFC3339
        document = _document(agents={"edge-1": entry})

        registry = load_agent_registry_document(document, key=TEST_KEY)

        result = registry.authenticate("edge-1", "current-token")
        assert result.agent_id == "edge-1"


class TestRegistryFileLoading:
    def test_non_utf8_registry_file_is_a_clean_configuration_error(self, tmp_path: Path) -> None:
        # Regression: Path.read_text() raises UnicodeDecodeError for
        # non-UTF-8 bytes, which used to propagate uncaught instead of the
        # module's documented ConfigurationError contract.
        bad_file = tmp_path / "agents.json"
        bad_file.write_bytes(b"\xff\xfe{not valid utf-8")

        with pytest.raises(ConfigurationError):
            load_agent_registry_file(bad_file, key=TEST_KEY)


class TestRegistryFileDuplicateJsonKeys:
    # Fix commit on top of #35: a raw JSON *document* -- not a Python dict
    # built from one, which cannot express this at all -- containing two
    # occurrences of the same key must be rejected outright rather than
    # silently resolved last-wins by the stdlib JSON parser. Only
    # observable through the file-loading entry point, since
    # load_agent_registry_document takes an already-parsed object where
    # the duplicate has necessarily already been collapsed by whatever
    # parsed it.

    def test_duplicate_agent_id_key_under_agents_is_rejected(self, tmp_path: Path) -> None:
        first_hash = _hash_hex("token-one")
        second_hash = _hash_hex("token-two")
        raw = (
            "{\n"
            '  "registry_version": 2,\n'
            '  "hash_algorithm": "hmac-sha256",\n'
            f'  "key_id": "{TEST_KEY_ID}",\n'
            '  "agents": {\n'
            f'    "edge-17": {{"token_hash": "{first_hash}"}},\n'
            f'    "edge-17": {{"token_hash": "{second_hash}"}}\n'
            "  }\n"
            "}\n"
        )
        registry_file = tmp_path / "agents.json"
        registry_file.write_text(raw)

        with pytest.raises(ConfigurationError):
            load_agent_registry_file(registry_file, key=TEST_KEY)

    def test_a_document_without_the_duplicate_loads_fine_as_a_control(self, tmp_path: Path) -> None:
        # Sanity control: the file-loading path itself works for a
        # well-formed document, so the failure above is attributable to the
        # duplicate key and not to some other property of hand-written raw
        # JSON.
        first_hash = _hash_hex("token-one")
        raw = (
            "{\n"
            '  "registry_version": 2,\n'
            '  "hash_algorithm": "hmac-sha256",\n'
            f'  "key_id": "{TEST_KEY_ID}",\n'
            '  "agents": {\n'
            f'    "edge-17": {{"token_hash": "{first_hash}"}}\n'
            "  }\n"
            "}\n"
        )
        registry_file = tmp_path / "agents.json"
        registry_file.write_text(raw)

        registry = load_agent_registry_file(registry_file, key=TEST_KEY)

        assert registry.get("edge-17") is not None


class TestPreviousTokenExpiresAtMustBeTimezoneAware:
    # Fix commit on top of #35, spec 36.3 / schema `format: date-time`
    # (RFC 3339 requires an explicit offset). A naive ISO datetime string
    # must not be silently assumed to mean UTC.

    def test_naive_previous_token_expires_at_is_rejected(self) -> None:
        entry = _agent_entry("current-token")
        entry["previous_token_hash"] = _hash_hex("old-token")
        entry["previous_token_expires_at"] = "2026-09-20T09:00:00"  # no UTC offset
        document = _document(agents={"edge-1": entry})

        with pytest.raises(ConfigurationError):
            load_agent_registry_document(document, key=TEST_KEY)

    def test_offset_aware_previous_token_expires_at_is_accepted(self) -> None:
        # Control: the same shape with an explicit offset must load fine,
        # so the rejection above is attributable to the missing offset and
        # not to some other property of the value.
        entry = _agent_entry("current-token")
        entry["previous_token_hash"] = _hash_hex("old-token")
        entry["previous_token_expires_at"] = "2026-09-20T09:00:00+00:00"
        document = _document(agents={"edge-1": entry})

        registry = load_agent_registry_document(document, key=TEST_KEY)

        assert registry.get("edge-1") is not None


class TestAgentRecordConstructionInvariant:
    # Fix commit on top of #35, defense-in-depth independent of the
    # document loader: `AgentRecord` is a public constructor
    # (`AgentRegistry.from_records`), and must reject a half-set rotation
    # pair from any direct caller, not just from
    # `load_agent_registry_document`'s own equivalent check.

    def test_previous_token_hash_without_expiry_is_rejected_at_construction(self) -> None:
        with pytest.raises(ConfigurationError):
            AgentRecord(
                agent_id=KNOWN_AGENT_ID,
                token_hash=_hash_bytes(KNOWN_AGENT_TOKEN),
                previous_token_hash=_hash_bytes("old-token"),
            )

    def test_previous_token_expires_at_without_hash_is_rejected_at_construction(self) -> None:
        with pytest.raises(ConfigurationError):
            AgentRecord(
                agent_id=KNOWN_AGENT_ID,
                token_hash=_hash_bytes(KNOWN_AGENT_TOKEN),
                previous_token_expires_at=datetime(2999, 1, 1, tzinfo=UTC),
            )

    def test_neither_set_is_accepted(self) -> None:
        record = AgentRecord(agent_id=KNOWN_AGENT_ID, token_hash=_hash_bytes(KNOWN_AGENT_TOKEN))

        assert record.previous_token_hash is None
        assert record.previous_token_expires_at is None

    def test_both_set_is_accepted(self) -> None:
        record = AgentRecord(
            agent_id=KNOWN_AGENT_ID,
            token_hash=_hash_bytes(KNOWN_AGENT_TOKEN),
            previous_token_hash=_hash_bytes("old-token"),
            previous_token_expires_at=datetime(2999, 1, 1, tzinfo=UTC),
        )

        assert record.previous_token_hash is not None
        assert record.previous_token_expires_at is not None

    def test_invalid_record_cannot_be_smuggled_in_through_from_records(self) -> None:
        # AgentRegistry.from_records takes already-built AgentRecords, but
        # the invariant fires at AgentRecord construction time itself, so
        # there is no way to reach from_records with a half-set record in
        # the first place -- pinned here so a future relaxation of
        # AgentRecord's own check doesn't quietly reopen this gap via
        # from_records.
        def _invalid_records() -> Iterable[AgentRecord]:
            yield AgentRecord(
                agent_id=KNOWN_AGENT_ID,
                token_hash=_hash_bytes(KNOWN_AGENT_TOKEN),
                previous_token_hash=_hash_bytes("old-token"),
            )

        with pytest.raises(ConfigurationError):
            AgentRegistry.from_records(_invalid_records(), key=TEST_KEY)
