"""Agent authentication & authorization (spec section 36).

Spec: section 36 ("At minimum: TLS, agent authentication, authorization,
replay protection, request size limits, rate limits, schema validation."
"An untrusted agent MUST NOT be able to arbitrarily declare `IP = HOT`.")

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

Reconciled against the actual implementation (originally written blind to
it, per this repo's test-author convention):

* `hammertime.ingest.auth`: `AgentAuthError` (base) -> `AuthenticationError`
  (401 group) -> `UnknownAgentError` (covers both "no such agent_id" and
  "no credential presented at all" -- the implementation does not
  distinguish those as separate exception types, since both mean "no valid
  identity claim was established"); `AuthorizationError` (403 group) ->
  `InvalidCredentialError`, `AgentDisabledError`.
* `hammertime.ingest.auth.agents.AgentRegistry` takes a `Mapping[str,
  AgentRecord]` in its constructor, or `AgentRegistry.from_records(...)`
  for an arbitrary `Iterable[AgentRecord]`. `.authenticate(agent_id,
  token) -> AgentRecord` is the Request-agnostic core logic (raises
  `UnknownAgentError`/`InvalidCredentialError`/`AgentDisabledError`);
  there is no separate `AuthenticatedAgent` value object -- the returned
  `AgentRecord` itself carries `.agent_id` and `.rate_limit_rps`.
* `hammertime.ingest.auth.middleware.require_agent(registry)` builds a
  FastAPI dependency (`Callable[[Request], str]`) that reads identity from
  an `X-Agent-Id` header and the credential from `Authorization: Bearer
  <token>`, raising a clean `HTTPException(401)`/`HTTPException(403)` --
  this is the actual wire-level entry point, tested here via FastAPI's
  `TestClient` (mirroring `test_routes.py`'s convention from Epic #3)
  rather than by calling a private header-parsing helper directly.
"""

from collections.abc import Iterable
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hammertime.core.errors import ConfigurationError, HammertimeError
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


def _known_agent(**overrides: object) -> AgentRecord:
    fields: dict[str, object] = {
        "agent_id": KNOWN_AGENT_ID,
        "token": KNOWN_AGENT_TOKEN,
        "enabled": True,
    }
    fields.update(overrides)
    return AgentRecord(**fields)  # type: ignore[arg-type]


def _registry(*records: AgentRecord) -> AgentRegistry:
    return AgentRegistry.from_records(records)


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
        # docs/protocol/observation-v1.md: 401 for "unknown ... agent".
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
        # Regression: hmac.compare_digest raises TypeError for a non-ASCII
        # str, and Starlette decodes raw header bytes as latin-1, so any
        # Authorization header byte >= 0x80 reaching here used to crash
        # with an uncaught TypeError instead of a clean InvalidCredentialError.
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
        # "disabled" is left to the coder (the task did not specify an
        # ordering for this combination) -- only that *some* auth error
        # fires, never a successful authentication.
        registry = _registry(_known_agent(enabled=False))

        with pytest.raises(AgentAuthError):
            registry.authenticate(KNOWN_AGENT_ID, "wrong-token")


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
            yield _known_agent(agent_id="edge-2", token="other-token")

        registry = AgentRegistry.from_records(_records())

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


class TestRegistryLoading:
    # load_agent_registry_document/_file is the production JSON-loading
    # path (load_agent_registry, not exercised here to avoid a real env
    # var / disk dependency) -- these tests target it directly.

    def test_duplicate_token_across_two_agents_is_rejected(self) -> None:
        document = {
            "edge-1": {"token": "shared-token"},
            "edge-2": {"token": "shared-token"},
        }

        with pytest.raises(ConfigurationError, match="share the same token"):
            load_agent_registry_document(document)

    def test_distinct_tokens_across_agents_are_accepted(self) -> None:
        document = {
            "edge-1": {"token": "token-one"},
            "edge-2": {"token": "token-two"},
        }

        registry = load_agent_registry_document(document)

        assert registry.get("edge-1") is not None
        assert registry.get("edge-2") is not None

    @pytest.mark.parametrize("rate_limit_rps", [0, -1])
    def test_non_positive_rate_limit_rps_is_rejected_at_load_time(
        self, rate_limit_rps: int
    ) -> None:
        # Regression: RateLimiter.check() raises a bare ValueError for a
        # non-positive limit_rps (ratelimit/__init__.py) -- an unhandled
        # 500 on every request for this agent if a config typo like this
        # reached the live pipeline instead of failing at startup.
        document = {"edge-1": {"token": "token-one", "rate_limit_rps": rate_limit_rps}}

        with pytest.raises(ConfigurationError, match="rate_limit_rps"):
            load_agent_registry_document(document)

    def test_positive_rate_limit_rps_is_accepted(self) -> None:
        document = {"edge-1": {"token": "token-one", "rate_limit_rps": 5}}

        registry = load_agent_registry_document(document)

        record = registry.get("edge-1")
        assert record is not None
        assert record.rate_limit_rps == 5

    def test_non_utf8_registry_file_is_a_clean_configuration_error(self, tmp_path: Path) -> None:
        # Regression: Path.read_text() raises UnicodeDecodeError for
        # non-UTF-8 bytes, which used to propagate uncaught instead of the
        # module's documented ConfigurationError contract.
        bad_file = tmp_path / "agents.json"
        bad_file.write_bytes(b"\xff\xfe{not valid utf-8")

        with pytest.raises(ConfigurationError):
            load_agent_registry_file(bad_file)
