"""Failed-authentication throttling (issue #40).

Spec: section 36.5 (failed-authentication throttling), section 36.7
(throttled-response shape shared with ADR-0008). ADR-0007 is the design
authority for every behaviour asserted below; `docs/protocol/observation-v1.md`'s
response table documents the wire-visible outcome this file pins down: a
uniform `401` for every authentication failure mode (previously split
401/403 -- see `test_auth.py`'s own pre-ADR-0007 documentation of that
split, which this file supersedes for the wrong-credential case), `403`
reserved for a *correct* credential presented for a disabled agent, and a
`429` with `Retry-After` / `X-RateLimit-Scope: auth-failures` once either of
the two failure budgets (source address, attempted `agent_id`) is
exhausted.

Exercised at the full HTTP pipeline level (`create_app` + `TestClient`),
mirroring `test_pipeline.py`'s convention, rather than by calling
`hammertime.ingest.auth.middleware.require_agent` directly the way
`test_auth.py`'s pre-existing `TestRequireAgentDependency` class does: per
ADR-0007's implementation notes, `require_agent`'s closure needs two new
`RateLimiter` instances, a `Clock`, and the resolved source key threaded
into it from `app.py`'s lifespan, and this file is written blind to that
new constructor shape (per this repo's test-author convention). Going
through `create_app`/`IngestSettings` -- whose new field names and
defaults *are* pinned down explicitly by ADR-0007's "Implementation notes"
section -- avoids depending on a signature this file cannot know.
`test_auth.py`'s own `TestRequireAgentDependency` tests may need
reconciliation once `require_agent`'s actual new signature lands, since
they call it with a single `registry` argument today.

Source-address bucketing (spec section 36.5: "IPv4 /32... IPv6 /64... the
client address is the socket peer address by default... `X-Forwarded-For`
MUST NOT be consulted unless `HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS` is
greater than zero") is exercised by varying `X-Forwarded-For` against a
single, long-lived `TestClient`/app instance with `trusted_proxy_hops`
configured, rather than by constructing multiple `TestClient`s with
different `client=` socket-peer tuples against the *same* app:
re-entering a second `TestClient` against one shared FastAPI `app` object
would re-run its lifespan and silently reset every limiter's accumulated
state, defeating exactly the cross-request accumulation these tests need
to observe.

Real-clock note: like `test_pipeline.py`'s ADR-0008 additions, the two new
limiters (`auth_failure_source_limiter`, `auth_failure_agent_limiter`) have
no `create_app` clock-injection point per ADR-0007's implementation notes,
so every test below relies on real wall-clock time between back-to-back
`TestClient` calls, choosing small bursts and slow (per-minute) refill
rates to keep a generous margin against real-time drift during a test run.

ADR-0007 Decision 8 (added after this file's first pass, post-implementation
security finding): the agent bucket's key is a fixed-size salted slot
(`agent-slot:N`, `N` in `[0, 4096)`) derived from the attempted `X-Agent-Id`
via `HMAC-SHA-256(slot_salt, agent_id)`, not the raw `agent_id` string --
see spec section 36.5's "The agent bucket's key space MUST be bounded
independently of the limiter's eviction policy" paragraph. This does not
change any behaviour asserted by `TestAgentKeyedThrottle` or
`TestMissingAgentIdUsesASharedSentinelBucket` above (those tests only ever
observe HTTP-level 401/429 outcomes, never a literal bucket key), so
nothing above this point needed updating for Decision 8. The classes near
the bottom of this file cover Decision 8's own named regression properties
specifically: deterministic same-id keying, the bounded/forceable-collision
key space (via the test-only `agent_slot_salt`/`agent_slot_count`
keyword-only parameters Decision 8 adds to `require_agent`), the finding
itself (an exhausted target budget is not restored by flooding distinct
junk ids), the anti-enumeration property (registered and unregistered ids
throttle identically), and the `"-"` sentinel's isolation from the slot
table.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hammertime.ingest.app import create_app
from hammertime.ingest.auth import AgentAuthError
from hammertime.ingest.auth.agents import AgentRecord, AgentRegistry
from hammertime.ingest.config import IngestSettings

_CONFIG_PATH = Path(__file__).parents[6] / "config" / "detection.v1.json"

KNOWN_AGENT_ID = "edge-17"
KNOWN_AGENT_TOKEN = "s3cr3t-token-value"

# Known-aligned per test_routes.py's/test_pipeline.py's own happy-path fixture.
WINDOW_START = "2026-09-14T10:00:00Z"
WINDOW_SECONDS = 60

# Generous enough that no test below accidentally trips a budget it isn't
# specifically testing.
_AMPLE = 1_000_000

# Deliberately slow (6 failures/min = 0.1 tokens/sec) baseline default for
# the two failure *rates* (as opposed to burst/capacity). A test that only
# overrides `auth_failure_burst`/`auth_failure_agent_burst` downward to
# force exhaustion, without also slowing the corresponding rate, would
# otherwise inherit `_AMPLE` as the refill rate too -- refilling most of
# the bucket back within a single millisecond and undoing the exhaustion
# before the next `TestClient` call lands. Real-clock note in this file's
# module docstring applies: at 0.1 tokens/sec even whole seconds of test
# slowness refill a negligible fraction of one token.
_SLOW_RATE_PER_MIN = 6.0


def _known_agent(**overrides: object) -> AgentRecord:
    fields: dict[str, object] = {
        "agent_id": KNOWN_AGENT_ID,
        "token": KNOWN_AGENT_TOKEN,
        "enabled": True,
        "rate_limit_rps": _AMPLE,
    }
    fields.update(overrides)
    return AgentRecord(**fields)  # type: ignore[arg-type]


def _settings(**overrides: object) -> IngestSettings:
    fields: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 0,
        "max_body_bytes": 1_048_576,
        "max_observations": 10_000,
        "detection_config_path": _CONFIG_PATH,
        "rate_limit_rps": _AMPLE,
        "agents_path": Path("unused -- agent_registry is injected directly"),
        "bus_kind": "memory",
        "bus_brokers": "",
        "store_kind": "memory",
        "redis_url": "",
        "observation_rate_limit_eps": _AMPLE,
        "observation_burst": _AMPLE,
        "auth_failure_rate_per_min": _SLOW_RATE_PER_MIN,
        "auth_failure_burst": _AMPLE,
        "auth_failure_agent_rate_per_min": _SLOW_RATE_PER_MIN,
        "auth_failure_agent_burst": _AMPLE,
        "trusted_proxy_hops": 0,
    }
    fields.update(overrides)
    return IngestSettings(**fields)  # type: ignore[arg-type]


def _headers(agent_id: str, token: str) -> dict[str, str]:
    return {"X-Agent-Id": agent_id, "Authorization": f"Bearer {token}"}


def _body(*, sequence: int = 1, agent_id: str = KNOWN_AGENT_ID) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "sequence": sequence,
        "window_start": WINDOW_START,
        "window_seconds": WINDOW_SECONDS,
        "observations": [{"ip": "192.168.1.42", "request_count": 1}],
    }


def _client(
    *,
    agents: list[AgentRecord] | None = None,
    client_address: tuple[str, int] = ("203.0.113.1", 51000),
    agent_slot_salt: bytes | None = None,
    agent_slot_count: int | None = None,
    **settings_overrides: object,
) -> TestClient:
    """A single, long-lived `TestClient` over a fresh app + registry.

    `__enter__` (not a bare `TestClient(app)`) runs the app's lifespan,
    which populates `request.app.state.ingest` -- mirrors
    `test_pipeline.py`'s `_build_app` convention. `client=` fixes the
    ASGI-level socket peer address Starlette's `TestClient` reports for
    every request made through this one instance.

    `agent_slot_salt`/`agent_slot_count` are ADR-0007 Decision 8's
    test-only, keyword-only parameters on `require_agent`
    ("they exist so tests can pin the mapping"; `app.py` itself passes
    neither and no `IngestSettings` field exists for them). This file
    cannot read `create_app`'s actual signature (guarded, per this
    repo's test-author convention), so forwarding them here -- the same
    way `agent_registry` is already threaded through as an extra
    `create_app` keyword rather than an `IngestSettings` field -- is a
    judgment call about the plumbing shape, not a confirmed fact; tests
    that rely on this (the `TestAgentBucketKeySpaceIsBoundedToTheSlotTable`
    and `TestExhaustedAgentBudgetIsNotRestoredByFloodingDistinctIds`
    classes below) may need reconciling once the real signature lands, per
    the same note this file's module docstring already makes about
    `require_agent`. Only forwarded when explicitly given, so every
    existing call site (which never passes them) is untouched.
    """
    registry = AgentRegistry.from_records(agents if agents is not None else [_known_agent()])
    create_app_kwargs: dict[str, object] = {"agent_registry": registry}
    if agent_slot_salt is not None:
        create_app_kwargs["agent_slot_salt"] = agent_slot_salt
    if agent_slot_count is not None:
        create_app_kwargs["agent_slot_count"] = agent_slot_count
    app = create_app(_settings(**settings_overrides), **create_app_kwargs)
    return TestClient(app, client=client_address).__enter__()


def _slot_for(agent_id: str, *, salt: bytes, slot_count: int) -> int:
    """Reimplements -- from the spec, not the implementation -- ADR-0007
    Decision 8 / spec section 36.5's slot formula exactly:
    `slot = HMAC-SHA-256(salt, agent_id)[:8]`, read big-endian, mod
    `slot_count`. Used only to *search* for attempted `agent_id`s that are
    guaranteed (by this same published formula) to collide or not collide
    under a given salt/count, so the forced-collision tests below don't
    need to guess -- the HTTP-level assertions are what actually pin down
    behaviour, this is just test setup.
    """
    digest = hmac.new(salt, agent_id.encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") % slot_count


def _find_id_with_slot(
    target_slot: int,
    *,
    salt: bytes,
    slot_count: int,
    prefix: str,
    exclude: str = "",
    search_space: int = 100_000,
) -> str:
    for i in range(search_space):
        candidate = f"{prefix}-{i}"
        if candidate == exclude:
            continue
        if _slot_for(candidate, salt=salt, slot_count=slot_count) == target_slot:
            return candidate
    raise AssertionError(
        f"no {prefix!r}-prefixed id landing in slot {target_slot} found in "
        f"{search_space} candidates"
    )


def _find_id_with_different_slot(
    avoid_slot: int,
    *,
    salt: bytes,
    slot_count: int,
    prefix: str,
    search_space: int = 100_000,
) -> str:
    for i in range(search_space):
        candidate = f"{prefix}-{i}"
        if _slot_for(candidate, salt=salt, slot_count=slot_count) != avoid_slot:
            return candidate
    raise AssertionError(
        f"no {prefix!r}-prefixed id outside slot {avoid_slot} found in {search_space} candidates"
    )


# 32 fixed bytes (not `secrets.token_bytes`) so the collision search above is
# reproducible across runs -- this is standing in for "a salt drawn once per
# process" per Decision 8, not for anything that needs to be unpredictable
# from a test's own point of view.
_FIXED_SLOT_SALT = bytes(range(32))


_WRONG_TOKEN_HEADERS = _headers(KNOWN_AGENT_ID, "wrong-token")
_UNKNOWN_AGENT_HEADERS = _headers("no-such-agent", "irrelevant-token")


class TestUniformAuthenticationFailureResponse:
    """Section 36.5: every authentication failure mode collapses onto one
    identical response. This is the wire-visible break from the previous
    split (a wrong token for a *known* agent used to be `403`; per
    ADR-0007 it is now `401`, identical to every other failure mode)."""

    @pytest.mark.parametrize(
        "case_headers",
        [
            pytest.param({}, id="no_credentials_at_all"),
            pytest.param({"Authorization": f"Bearer {KNOWN_AGENT_TOKEN}"}, id="missing_agent_id"),
            pytest.param({"X-Agent-Id": KNOWN_AGENT_ID}, id="missing_authorization"),
            pytest.param(
                {"X-Agent-Id": KNOWN_AGENT_ID, "Authorization": KNOWN_AGENT_TOKEN},
                id="malformed_authorization_scheme",
            ),
            pytest.param(_headers("no-such-agent", "irrelevant-token"), id="unknown_agent_id"),
            pytest.param(_headers(KNOWN_AGENT_ID, "wrong-token"), id="wrong_token_for_known_agent"),
            pytest.param(_headers("a" * 129, KNOWN_AGENT_TOKEN), id="oversized_agent_id"),
            pytest.param(_headers(KNOWN_AGENT_ID, "t" * 513), id="oversized_token"),
        ],
    )
    def test_every_failure_mode_returns_the_identical_401(
        self, case_headers: dict[str, str]
    ) -> None:
        client = _client()
        response = client.post("/v1/observations", json={}, headers=case_headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "invalid agent credentials"}
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_the_429_body_never_reveals_the_agent_id_is_unregistered(self) -> None:
        # Section 36.5: uniformity holds for the *content* of the 401 body
        # regardless of whether the agent_id is registered -- an unknown
        # agent_id and a known one with a wrong token are indistinguishable.
        client = _client()
        unknown = client.post("/v1/observations", json={}, headers=_UNKNOWN_AGENT_HEADERS)
        known_wrong = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)

        assert unknown.status_code == known_wrong.status_code == 401
        assert unknown.json() == known_wrong.json() == {"detail": "invalid agent credentials"}


class TestDisabledAgentStillReturns403:
    """Section 36.5: '403 is reserved for exactly one outcome: a correct
    credential for a registered but disabled agent.'"""

    def test_correct_credential_for_disabled_agent_is_403_with_the_fixed_body(self) -> None:
        client = _client(agents=[_known_agent(enabled=False)])

        response = client.post(
            "/v1/observations", json={}, headers=_headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)
        )

        assert response.status_code == 403
        assert response.json() == {"detail": "agent is disabled"}


class TestSourceKeyedThrottle:
    """ADR-0007 decision 2: source bucket, keyed by client address."""

    def test_enough_failures_from_one_source_trips_429_with_headers(self) -> None:
        burst = 3
        client = _client(auth_failure_burst=burst, auth_failure_rate_per_min=30)

        for _ in range(burst):
            response = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
            assert response.status_code == 401

        response = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Scope"] == "auth-failures"
        assert int(response.headers["Retry-After"]) >= 1
        assert response.json() == {"detail": "too many failed authentication attempts"}

    def test_a_blocked_source_stays_blocked_without_the_block_getting_worse(self) -> None:
        # "A bucket that is short of tokens rejects without consuming any,
        # so repeated attempts while blocked do not extend the block."
        burst = 2
        client = _client(auth_failure_burst=burst)

        for _ in range(burst):
            client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
        first_block = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
        assert first_block.status_code == 429

        second_block = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
        assert second_block.status_code == 429
        # Real time only moves forward and no attempt here consumes a
        # token, so the wait remaining can only shrink or stay the same --
        # never grow, which is what "repeated attempts do not extend the
        # block" rules out.
        assert int(second_block.headers["Retry-After"]) <= int(first_block.headers["Retry-After"])


class TestSuccessfulAuthNeverThrottledAndDoesNotTouchTheFailureBudget:
    """Section 36.5: 'A request that authenticates successfully MUST NOT
    be rejected by this mechanism, MUST NOT consume failure budget, and
    MUST NOT reset it.'"""

    def test_successes_are_never_rejected_and_the_failure_budget_is_untouched(self) -> None:
        burst = 3
        client = _client(auth_failure_burst=burst)

        # Spend all but one token on failures.
        for _ in range(burst - 1):
            response = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
            assert response.status_code == 401

        # A run of successful, correctly authenticated requests: none may
        # ever be rejected by this mechanism.
        good_headers = _headers(KNOWN_AGENT_ID, KNOWN_AGENT_TOKEN)
        for sequence in range(1, 6):
            response = client.post(
                "/v1/observations", json=_body(sequence=sequence), headers=good_headers
            )
            assert response.status_code == 202

        # Exactly one failure token was left from the original burst: this
        # failure must still be an ordinary 401 (not yet throttled)...
        response = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
        assert response.status_code == 401

        # ...and the very next one must be throttled -- proving neither
        # the five successful requests above consumed the budget early,
        # nor reset it back up to full.
        response = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
        assert response.status_code == 429


class TestAgentKeyedThrottle:
    """ADR-0007 decision 2: agent bucket, keyed by the attempted
    `X-Agent-Id`, independent of source address."""

    def test_enough_failures_against_one_agent_id_from_many_sources_trips_429(self) -> None:
        agent_burst = 3
        client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,  # only the agent bucket can trip below
            trusted_proxy_hops=1,
        )
        target_headers = _headers("edge-99", "irrelevant")

        for i in range(agent_burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**target_headers, "X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert response.status_code == 401

        # A brand-new source, still guessing the same agent_id, finds the
        # agent bucket -- not any one source's bucket -- already empty.
        response = client.post(
            "/v1/observations",
            json={},
            headers={**target_headers, "X-Forwarded-For": "203.0.113.250"},
        )
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Scope"] == "auth-failures"
        assert int(response.headers["Retry-After"]) >= 1


class TestMissingAgentIdUsesASharedSentinelBucket:
    """Section 36.5: 'agent bucket key = the attempted X-Agent-Id, or "-"
    when absent or malformed.'"""

    def test_repeated_requests_with_no_agent_id_share_one_agent_bucket(self) -> None:
        agent_burst = 3
        client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,
            trusted_proxy_hops=1,
        )
        no_agent_id_headers = {"Authorization": "Bearer irrelevant"}

        for i in range(agent_burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**no_agent_id_headers, "X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert response.status_code == 401

        response = client.post(
            "/v1/observations",
            json={},
            headers={**no_agent_id_headers, "X-Forwarded-For": "203.0.113.250"},
        )
        assert response.status_code == 429


class TestBucketOrderingSourceCheckedBeforeAgent:
    """ADR-0007 decision 2: the source bucket is charged first; if it
    rejects, the agent bucket MUST NOT be charged. Tested at the
    observable-behaviour level (per the task brief) rather than by
    inspecting limiter internals: if many failed attempts against a
    *fresh* agent_id, all from an already-blocked source, never touch that
    agent_id's bucket, then the very first attempt against that same
    agent_id from a *different*, unblocked source must still see the
    uniform 401 (not 429) -- proving none of the blocked source's attempts
    were ever charged to it.
    """

    def test_a_blocked_sources_failures_do_not_charge_the_attempted_agents_bucket(self) -> None:
        source_burst = 3
        client = _client(
            auth_failure_burst=source_burst,
            auth_failure_agent_burst=_AMPLE,  # only the source bucket can trip below
            trusted_proxy_hops=1,
        )
        blocked_source = "203.0.113.77"
        fresh_source = "203.0.113.88"
        agent_a_headers = _headers("agent-a", "irrelevant")
        agent_b_headers = _headers("agent-b", "irrelevant")

        for _ in range(source_burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**agent_a_headers, "X-Forwarded-For": blocked_source},
            )
            assert response.status_code == 401
        blocked = client.post(
            "/v1/observations",
            json={},
            headers={**agent_a_headers, "X-Forwarded-For": blocked_source},
        )
        assert blocked.status_code == 429  # confirms the source bucket is now exhausted

        # Many more attempts from the SAME blocked source, now guessing a
        # brand-new agent_id that has never been attempted before. If the
        # source bucket really is checked first and its rejection skips
        # the agent charge, none of these touch agent-b's bucket at all.
        for _ in range(50):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**agent_b_headers, "X-Forwarded-For": blocked_source},
            )
            assert response.status_code == 429  # still just the source block

        # A fresh, never-blocked source guessing the SAME agent_id must
        # see the ordinary uniform 401 -- not 429 -- which is only
        # possible if agent-b's bucket is still completely untouched
        # despite the 50 attempts above.
        response = client.post(
            "/v1/observations",
            json={},
            headers={**agent_b_headers, "X-Forwarded-For": fresh_source},
        )
        assert response.status_code == 401


class TestUntrustedForwardedForIsIgnored:
    """Section 36.5: 'X-Forwarded-For MUST NOT be consulted unless
    HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS is greater than zero' -- the
    default is 0."""

    def test_forged_x_forwarded_for_does_not_evade_the_source_bucket_when_untrusted(self) -> None:
        burst = 3
        client = _client(auth_failure_burst=burst, trusted_proxy_hops=0)

        for i in range(burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert response.status_code == 401

        # A brand-new forged X-Forwarded-For value must not matter -- the
        # real bucket key is the (fixed, single) socket peer address, which
        # is now exhausted regardless of what this header claims.
        response = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": "203.0.113.999"},
        )
        assert response.status_code == 429


class TestTrustedProxyHopsSourceResolution:
    """Section 36.5: 'a deployment that terminates behind N trusted
    proxies sets the hop count and gets the Nth-from-the-right
    X-Forwarded-For entry; anything shorter, absent or unparsable falls
    back to the socket peer.'"""

    def test_trusted_forwarded_for_creates_genuinely_separate_buckets(self) -> None:
        burst = 3
        client = _client(auth_failure_burst=burst, trusted_proxy_hops=1)
        exhausted_source = "203.0.113.10"
        fresh_source = "203.0.113.20"

        for _ in range(burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": exhausted_source},
            )
            assert response.status_code == 401
        blocked = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": exhausted_source},
        )
        assert blocked.status_code == 429

        # A different trusted source must have its own, still-fresh bucket.
        response = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": fresh_source},
        )
        assert response.status_code == 401

    def test_hops_selects_the_nth_from_right_entry(self) -> None:
        burst = 3
        client = _client(auth_failure_burst=burst, trusted_proxy_hops=2)
        # Left-to-right: farthest hop first, nearest (closest to ingest)
        # last -- 2nd-from-the-right is "203.0.113.5".
        xff = "198.51.100.1, 203.0.113.5, 192.0.2.9"

        for _ in range(burst):
            headers = {**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": xff}
            response = client.post("/v1/observations", json={}, headers=headers)
            assert response.status_code == 401
        blocked = client.post(
            "/v1/observations", json={}, headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": xff}
        )
        assert blocked.status_code == 429

        # Changing only the 2nd-from-right entry reaches a fresh bucket.
        different_2nd_from_right = "198.51.100.1, 203.0.113.6, 192.0.2.9"
        response = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": different_2nd_from_right},
        )
        assert response.status_code == 401

        # Changing only entries *other than* the 2nd-from-right must not
        # matter -- same bucket, still exhausted.
        same_2nd_from_right = "10.0.0.1, 203.0.113.5, 172.16.0.1"
        response = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": same_2nd_from_right},
        )
        assert response.status_code == 429

    def test_missing_or_too_short_forwarded_for_falls_back_to_the_socket_peer(self) -> None:
        burst = 3
        client = _client(auth_failure_burst=burst, trusted_proxy_hops=2)

        for _ in range(burst):
            response = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
            assert response.status_code == 401
        blocked = client.post("/v1/observations", json={}, headers=_WRONG_TOKEN_HEADERS)
        assert blocked.status_code == 429

        # A header with fewer than 2 entries also falls back to the socket
        # peer, so it lands in the SAME (already-exhausted) bucket, not a
        # fresh one.
        response = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": "203.0.113.1"},
        )
        assert response.status_code == 429


class TestIpv6SourcesAreKeyedBySlash64:
    """Section 36.5: 'IPv6 sources are keyed by their /64 prefix, because
    a single allocation routinely carries 2^64 addresses.'"""

    def test_addresses_sharing_a_64_prefix_share_one_bucket(self) -> None:
        burst = 3
        client = _client(auth_failure_burst=burst, trusted_proxy_hops=1)
        same_prefix = ["2001:db8:abcd:1::1", "2001:db8:abcd:1::2", "2001:db8:abcd:1::3"]

        for address in same_prefix:
            response = client.post(
                "/v1/observations",
                json={},
                headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": address},
            )
            assert response.status_code == 401

        # A fourth distinct address within the SAME /64 must find the
        # bucket already exhausted.
        response = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": "2001:db8:abcd:1::4"},
        )
        assert response.status_code == 429

    def test_addresses_in_different_64_prefixes_get_separate_buckets(self) -> None:
        burst = 3
        client = _client(auth_failure_burst=burst, trusted_proxy_hops=1)

        for _ in range(burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": "2001:db8:abcd:1::1"},
            )
            assert response.status_code == 401
        blocked = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": "2001:db8:abcd:1::1"},
        )
        assert blocked.status_code == 429

        # A different /64 prefix (differs in the 4th hextet, still part of
        # the network portion) must not be affected.
        response = client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": "2001:db8:abcd:2::1"},
        )
        assert response.status_code == 401


class TestUnknownAgentTimingIsNotAShortcut:
    """Section 36.5: 'when the agent_id is unknown, the service MUST still
    compute the presented token's HMAC and constant-time-compare it
    against a fixed dummy digest, discarding the result, so that an
    unknown identity is not measurably faster to reject than a known one.'

    No timing test predates this ADR in `test_auth.py` to mirror exactly,
    so this is a straightforward `time.perf_counter`-based smoke test, in
    the spirit of this repo's general "assert an invariant, not an exact
    value" style for anything timing/arithmetic-sensitive (see
    `test_ratelimit.py`'s `TestRateLimiterBurstThenRefill`). It targets
    `AgentRegistry.authenticate` directly -- the location ADR-0007's
    implementation notes name for the dummy-digest comparison -- rather
    than the full HTTP path, to keep the measurement free of unrelated
    ASGI/ratelimiting overhead. Deliberately generous bound: sized to
    catch a naive implementation that skips hashing entirely for an
    unknown agent_id (which would be orders of magnitude cheaper), not to
    pin down a precise timing budget.
    """

    def test_unknown_agent_and_wrong_credential_paths_are_comparably_expensive(self) -> None:
        registry = AgentRegistry.from_records([_known_agent()])
        iterations = 200

        def _measure(agent_id: str, token: str) -> float:
            start = time.perf_counter()
            for _ in range(iterations):
                with contextlib.suppress(AgentAuthError):
                    registry.authenticate(agent_id, token)
            return time.perf_counter() - start

        # Warm up (module/attribute lookups, etc.) before the measured runs.
        _measure(KNOWN_AGENT_ID, "wrong-token")
        _measure("no-such-agent", "wrong-token")

        known_wrong_elapsed = _measure(KNOWN_AGENT_ID, "wrong-token")
        unknown_elapsed = _measure("no-such-agent", "wrong-token")

        # Neither path may be catastrophically cheaper than the other -- a
        # factor of 5 is generous enough to absorb ordinary scheduling
        # noise while still catching an implementation that does no
        # hashing at all for an unknown agent_id.
        assert unknown_elapsed >= known_wrong_elapsed / 5


# ---------------------------------------------------------------------------
# ADR-0007 Decision 8: the agent bucket is keyed by a fixed-size salted slot,
# not by the attempted `agent_id` directly. Decision 8 itself names three
# regression properties explicitly ("the same attempted agent_id maps to the
# same key across calls"; "an arbitrary stream of distinct attempted
# agent_ids produces at most slot_count + 1 distinct limiter keys"; "a target
# identity's exhausted budget is not restored by any number of intervening
# distinct attempted identities") plus the anti-enumeration property
# ("registered and unregistered identities are mapped by the identical
# function"). The classes below cover each in turn, plus the "-" sentinel
# staying outside the slot table.
# ---------------------------------------------------------------------------


class TestAgentBucketKeyIsDeterministic:
    """ADR-0007 Decision 8, first named regression property: 'the same
    attempted `agent_id` maps to the same key across calls.' Exercised
    behaviourally: repeated failures against one `agent_id`, from
    different source addresses (so only the agent bucket -- never any one
    source's bucket -- can be accumulating), must all draw from a single
    shared budget, and a fresh, never-before-attempted `agent_id` must
    still see a full budget afterwards -- which is only possible if the
    two attempted ids consistently map to two different keys (true with
    overwhelming probability at the production 4096-slot default; the
    forced-collision case is exercised separately below)."""

    def test_repeated_attempts_against_one_agent_id_share_a_single_budget(self) -> None:
        agent_burst = 4
        client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,
            trusted_proxy_hops=1,
        )
        target_headers = _headers("edge-deterministic-target", "irrelevant-token")

        for i in range(agent_burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**target_headers, "X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert response.status_code == 401

        # The same identity, from yet another fresh source, finds its
        # shared budget already spent.
        exhausted = client.post(
            "/v1/observations",
            json={},
            headers={**target_headers, "X-Forwarded-For": "203.0.113.250"},
        )
        assert exhausted.status_code == 429

        # A different, never-before-attempted identity is unaffected.
        fresh_headers = _headers("edge-deterministic-fresh", "irrelevant-token")
        fresh = client.post(
            "/v1/observations",
            json={},
            headers={**fresh_headers, "X-Forwarded-For": "203.0.113.251"},
        )
        assert fresh.status_code == 401


class TestAgentBucketKeySpaceIsBoundedToTheSlotTable:
    """ADR-0007 Decision 8, second named regression property: 'an
    arbitrary stream of distinct attempted agent_ids produces at most
    slot_count + 1 distinct limiter keys' -- and 'collisions can only
    subtract budget, never add it.' A small, pinned `agent_slot_count`
    (via the test-only `agent_slot_salt`/`agent_slot_count` keyword-only
    parameters Decision 8 adds to `require_agent`) makes a forced
    collision between two genuinely *different* attempted `agent_id`s
    deterministic and observable from outside the process: draining one
    drains the other, because the limiter never sees either id, only the
    slot they share.
    """

    def test_two_distinct_agent_ids_forced_into_the_same_slot_share_one_budget(self) -> None:
        slot_count = 4
        target_slot = 0
        target_id = _find_id_with_slot(
            target_slot, salt=_FIXED_SLOT_SALT, slot_count=slot_count, prefix="target"
        )
        colliding_junk_id = _find_id_with_slot(
            target_slot,
            salt=_FIXED_SLOT_SALT,
            slot_count=slot_count,
            prefix="junk",
            exclude=target_id,
        )

        agent_burst = 3
        client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,
            trusted_proxy_hops=1,
            agent_slot_salt=_FIXED_SLOT_SALT,
            agent_slot_count=slot_count,
        )

        # Spend the whole budget guessing the JUNK id -- the target id is
        # never itself attempted.
        for i in range(agent_burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={
                    **_headers(colliding_junk_id, "irrelevant-token"),
                    "X-Forwarded-For": f"203.0.113.{i}",
                },
            )
            assert response.status_code == 401

        # The TARGET id is already throttled on its very first attempt,
        # because it shares the junk id's slot.
        response = client.post(
            "/v1/observations",
            json={},
            headers={
                **_headers(target_id, "irrelevant-token"),
                "X-Forwarded-For": "203.0.113.250",
            },
        )
        assert response.status_code == 429

    def test_an_id_landing_in_a_genuinely_different_slot_is_unaffected(self) -> None:
        slot_count = 4
        target_slot = 0
        target_id = _find_id_with_slot(
            target_slot, salt=_FIXED_SLOT_SALT, slot_count=slot_count, prefix="target"
        )
        colliding_junk_id = _find_id_with_slot(
            target_slot,
            salt=_FIXED_SLOT_SALT,
            slot_count=slot_count,
            prefix="junk",
            exclude=target_id,
        )
        other_slot_id = _find_id_with_different_slot(
            target_slot, salt=_FIXED_SLOT_SALT, slot_count=slot_count, prefix="other"
        )

        agent_burst = 3
        client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,
            trusted_proxy_hops=1,
            agent_slot_salt=_FIXED_SLOT_SALT,
            agent_slot_count=slot_count,
        )

        for i in range(agent_burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={
                    **_headers(colliding_junk_id, "irrelevant-token"),
                    "X-Forwarded-For": f"203.0.113.{i}",
                },
            )
            assert response.status_code == 401
        blocked = client.post(
            "/v1/observations",
            json={},
            headers={
                **_headers(colliding_junk_id, "irrelevant-token"),
                "X-Forwarded-For": "203.0.113.250",
            },
        )
        assert blocked.status_code == 429

        # An id landing in a genuinely different slot has its own,
        # completely untouched budget.
        response = client.post(
            "/v1/observations",
            json={},
            headers={
                **_headers(other_slot_id, "irrelevant-token"),
                "X-Forwarded-For": "203.0.113.251",
            },
        )
        assert response.status_code == 401


class TestExhaustedAgentBudgetIsNotRestoredByFloodingDistinctIds:
    """ADR-0007 Decision 8's own vulnerability walkthrough and its third
    named regression property: 'a target identity's exhausted budget is
    not restored by any number of intervening distinct attempted
    identities' -- the finding itself. Pins `agent_slot_count=1` so every
    non-empty `X-Agent-Id` (the real target and every junk id alike)
    shares the single slot deterministically: under the OLD raw-string
    keying this ADR replaces, a large enough flood of distinct junk ids
    would eventually walk the target's own dedicated bucket out of the
    limiter's LRU and have it recreated full on the next guess; under
    Decision 8's fixed slot table there is no separate bucket to walk
    out of in the first place, so the flood can only ever keep draining
    the one shared bucket, never refill it.
    """

    def test_a_large_flood_of_distinct_junk_ids_does_not_reset_the_targets_budget(self) -> None:
        agent_burst = 5
        client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,
            trusted_proxy_hops=1,
            agent_slot_count=1,
        )
        target_headers = _headers("edge-flood-target", "irrelevant-token")

        for i in range(agent_burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**target_headers, "X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert response.status_code == 401
        exhausted = client.post(
            "/v1/observations",
            json={},
            headers={**target_headers, "X-Forwarded-For": "203.0.113.200"},
        )
        assert exhausted.status_code == 429

        # A flood of hundreds of distinct junk agent_ids, each attempted
        # from its own distinct source (so only the agent bucket -- never
        # any one source's bucket -- could plausibly be what is rejecting
        # these): the intervening attempts the OLD raw-string keying's LRU
        # eviction would eventually have recreated the target's bucket
        # full in response to.
        for i in range(300):
            response = client.post(
                "/v1/observations",
                json={},
                headers={
                    **_headers(f"junk-flood-{i}", "irrelevant-token"),
                    "X-Forwarded-For": f"198.51.100.{i % 256}",
                },
            )
            assert response.status_code == 429

        # The target's budget must still be exhausted -- not reset back to
        # a fresh 401 the way pre-Decision-8 LRU eviction would have
        # allowed.
        still_exhausted = client.post(
            "/v1/observations",
            json={},
            headers={**target_headers, "X-Forwarded-For": "203.0.113.201"},
        )
        assert still_exhausted.status_code == 429


class TestAgentBucketKeyingDoesNotDistinguishRegisteredFromUnregistered:
    """ADR-0007 Decision 8, anti-enumeration property: 'registered and
    unregistered identities are mapped by the identical function, so a
    429 discloses nothing about the roster.' Distinct from the
    uniform-401-body/`WWW-Authenticate` property covered in
    `TestUniformAuthenticationFailureResponse` above: this is specifically
    about the *throttle bucket* not leaking registration status. Exercised
    by confirming a registered agent_id (presented with the wrong token)
    and a never-registered agent_id trip their respective agent buckets at
    the identical burst count and receive byte-identical `429` responses --
    there is no observable difference in throttling behaviour that would
    let a caller infer which kind of identity it presented.
    """

    def test_registered_and_unregistered_ids_trip_the_agent_bucket_identically(self) -> None:
        agent_burst = 3
        registered_client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,
            trusted_proxy_hops=1,
        )
        unregistered_client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,
            trusted_proxy_hops=1,
        )

        for i in range(agent_burst):
            registered_response = registered_client.post(
                "/v1/observations",
                json={},
                headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert registered_response.status_code == 401
            unregistered_response = unregistered_client.post(
                "/v1/observations",
                json={},
                headers={**_UNKNOWN_AGENT_HEADERS, "X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert unregistered_response.status_code == 401

        # Both trip on the exact same, agent_burst-th, attempt -- neither
        # identity got any extra slack for being (un)registered.
        registered_blocked = registered_client.post(
            "/v1/observations",
            json={},
            headers={**_WRONG_TOKEN_HEADERS, "X-Forwarded-For": "203.0.113.250"},
        )
        unregistered_blocked = unregistered_client.post(
            "/v1/observations",
            json={},
            headers={**_UNKNOWN_AGENT_HEADERS, "X-Forwarded-For": "203.0.113.250"},
        )

        assert registered_blocked.status_code == unregistered_blocked.status_code == 429
        assert registered_blocked.json() == unregistered_blocked.json()
        assert (
            registered_blocked.headers["X-RateLimit-Scope"]
            == unregistered_blocked.headers["X-RateLimit-Scope"]
            == "auth-failures"
        )
        assert int(registered_blocked.headers["Retry-After"]) >= 1
        assert int(unregistered_blocked.headers["Retry-After"]) >= 1


class TestMissingAgentIdSentinelIsIsolatedFromRealAgentSlots:
    """ADR-0007 Decision 8: the `"-"` key for a missing, empty, or
    over-long `X-Agent-Id` 'stays outside the [slot] table: the standing
    flood of missing-header failures must not permanently drain some
    arbitrary real agent's slot, and a caller learns nothing from a key it
    chose by omitting its own header.' Confirms a flood that drains the
    shared `"-"` bucket leaves a specific, named real agent_id's own
    bucket completely untouched -- extending
    `TestMissingAgentIdUsesASharedSentinelBucket` above (which only checks
    that missing-agent_id attempts share a bucket with *each other*) to
    check the sentinel bucket's isolation from a real identity's slot.
    """

    def test_a_missing_agent_id_flood_does_not_drain_a_real_agents_bucket(self) -> None:
        agent_burst = 3
        client = _client(
            auth_failure_agent_burst=agent_burst,
            auth_failure_burst=_AMPLE,
            trusted_proxy_hops=1,
        )
        no_agent_id_headers = {"Authorization": "Bearer irrelevant-token"}

        for i in range(agent_burst):
            response = client.post(
                "/v1/observations",
                json={},
                headers={**no_agent_id_headers, "X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert response.status_code == 401
        blocked = client.post(
            "/v1/observations",
            json={},
            headers={**no_agent_id_headers, "X-Forwarded-For": "203.0.113.250"},
        )
        assert blocked.status_code == 429

        # A REAL agent_id, never itself attempted, must still see the
        # ordinary uniform 401 -- its bucket is untouched by the drained
        # "-" sentinel bucket.
        target_headers = _headers("edge-untouched-by-sentinel-flood", "irrelevant-token")
        response = client.post(
            "/v1/observations",
            json={},
            headers={**target_headers, "X-Forwarded-For": "203.0.113.251"},
        )
        assert response.status_code == 401
