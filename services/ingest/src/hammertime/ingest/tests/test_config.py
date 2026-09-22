"""Ingest settings: failed-auth-throttling and observation-budget config keys.

Spec: section 36.5 ("Configuration" block -- five new keys and their
defaults), section 36.6 (`HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS` /
`_BURST` and the `OBSERVATION_BURST >= MAX_OBSERVATIONS` startup
validation). ADR-0007 "Implementation notes" and ADR-0008 "Implementation
notes" name the exact `IngestSettings` field names and defaults used below.

No `test_config.py` predates this file (there is no earlier
`load_settings` test to reconcile against or extend), so this follows
`config.py`'s own `load_settings(env: Mapping[str, str] | None = None)`
convention directly: every test passes an explicit `env` mapping rather
than depending on process environment variables, the same way
`test_pipeline.py`/`test_routes.py` construct an explicit `IngestSettings`
rather than reading `os.environ`.

`TestRedisUrlValidation` was added later and covers a different section:
spec section 47.1 step 3 (an invalid configuration -- "including a
connection URL the client library would refuse to build a client from" --
is rejected before any network connection is opened), section 47.5 (exit
status 2) and section 47.7 (no record carries the userinfo of a store URL),
i.e. ADR-0009 decision 2 as corrected by Amendment 3 item A12.

`TestBusKind` covers ADR-0013 decision 10's table, written from that text
alone: `HAMMERTIME_BUS_KIND` is "`nats` (default) or `memory`; `kafka` is a
`ValueError` (exit 2)"; `HAMMERTIME_BUS_BROKERS` is "comma-separated NATS
server URLs ..., default `nats://localhost:4222`". That row also said the
value was "not validated by `load_settings`" (assumption 15) -- it was, until
Amendment 4 ruling S2 (2026-09-22) superseded assumption 15 and made every
entry a `validate_bus_url` check, which `TestBusBrokersAreValidated` below
covers. The ADR states one rule for both services, so this class and
`services/aggregator/.../tests/test_config.py`'s `TestBusKind` are
deliberately diffable copies.

ASSUMPTIONS for `TestBusKind`: `IngestSettings.bus_kind` / `bus_brokers` are
the field names `test_pipeline.py::_settings` already constructs; the kind
is matched as written (no claim about case-insensitivity either way).

`TestBusBrokersAreValidated` covers ADR-0013 decision 10's
`HAMMERTIME_BUS_BROKERS` row as superseded 2026-09-22 (Amendment 4 ruling
S2): "every entry is checked by `hammertime.bus.nats.validate_bus_url` in
`load_settings`, and an entry nats-py's own parse would refuse is a
`ValueError` naming the variable and the entry's position, `config_invalid`,
exit 2"; decision 3 (as amended): "`load_settings` in the aggregator and in
ingest calls it for every entry of `HAMMERTIME_BUS_BROKERS` (split as
`NatsBus.__init__` splits it) and turns a refusal into a `ValueError` naming
the variable and the entry's position"; and, since 2026-09-22 (Amendment 5
ruling 5), only "when `bus_kind` is `nats` ...: under `memory` the value is
never interpreted, so a set-but-malformed one is ignored, not rejected",
which `test_a_refused_entry_is_ignored_not_rejected_under_memory` covers.
Assumption 15 ("not validated") is superseded, and with it the name that
asserted it: `TestBusKind`'s
`test_the_brokers_value_is_stored_untouched_and_not_validated` is
`test_a_value_every_entry_of_which_nats_py_accepts_is_stored_as_given` since
2026-09-22 (Amendment 5 ruling 7). It keeps its two values, which are still
accepted -- "no value that connected before is refused now -- with one
deliberate exception, which governs: rule (iii) refuses a value nats-py would
accept, such as `nats://user:pass@host:4222/a@b`" (Amendment 5 ruling 6).
Error precedence between this key and others is not pinned (ruling T7): every
test supplies valid values for every other key. The class is a deliberately
diffable copy of the aggregator file's class of the same name. The needles
`usr7`, `s3cr`, `et@` and `s3cr/et` are the constants
`BROKERS_REFUSED_FRAGMENTS` holds; none of them is a substring of ordinary
English such as "parse" or "address", so a message that carries one carries a
fragment of the entry, and the rejection must name the variable and the
position and nothing more.

`TestStartingRecordCarriesBusEndpoints` covers ADR-0013 Amendment 2 rulings
2-3 and decision 3's `bus_endpoints` paragraph: the `starting` record "logs
`bus_endpoints` **in place of** `bus_brokers`", each entry reduced to
"`<scheme>://` followed by the part of the authority after its **last** `@`"
so that "nothing to the left of that `@` reaches the output: not a password,
and not a username either". That is section 47.7's "no record -- of any event
-- may contain a credential" applied to the userinfo of a bus URL (ruling 2),
which supersedes ADR-0009 A7's "`bus_brokers` is logged verbatim". The seam
is the one A7 names for the record's own fields -- `startup_fields()` on the
object `build_service` returns (`DescribesStartup`) -- and the service is
built on the memory bus with an injected `InMemoryBus` (ADR-0009 decision 3:
"when `bus` is given, the service takes ... from it regardless of
`HAMMERTIME_BUS_KIND`"), because the field is a reduction of the *setting*,
which `load_settings` stores untouched whatever the bus kind (decision 10,
assumption 15), so no NATS server is needed to observe it. The class is a
deliberately diffable copy of the aggregator file's class of the same name.

ASSUMPTIONS for that class:

* **`HAMMERTIME_CONFIG_PATH` is the detection-config key** `load_settings`
  reads into `detection_config_path` (ADR-0009 decision 6, `.env.example`);
  it is pointed at the repository's `config/detection.v1.json` the way
  `test_pipeline.py` resolves it, so `build_service` (which "loads the
  document", ADR-0009 A5) needs no cwd.
* **`build_service(settings, *, bus=..., agent_registry=...)`** is ADR-0009
  decision 3's composition root with ingest's "service-specific extras ...
  `dedup_store`, `agent_registry` (mirroring `create_app` today)". The
  registry is injected exactly as `test_routes.py` injects it into
  `create_app`, so no `agents.v2.json` fixture and no
  `HAMMERTIME_INGEST_AGENT_TOKEN_KEY` (read "directly by the registry
  loader", ADR-0009 decision 2) are needed.
* **`startup_fields()` returns a mapping whose values are JSON-renderable**,
  which is what a `starting` record requires; `json.dumps(..., default=str)`
  is used so a `Path`-valued field would still be rendered and scanned
  rather than fail the test for an unrelated reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from hammertime.bus.memory import InMemoryBus
from hammertime.core.auth.tokens import hash_token
from hammertime.core.runtime import run_service
from hammertime.ingest.auth.agents import AgentRecord, AgentRegistry
from hammertime.ingest.config import load_settings
from hammertime.ingest.service import build_service


class TestAuthFailureThrottlingDefaults:
    """Section 36.5's "Configuration" block."""

    def test_defaults_when_unset(self) -> None:
        settings = load_settings({})

        assert settings.auth_failure_rate_per_min == 30
        assert settings.auth_failure_burst == 10
        assert settings.auth_failure_agent_rate_per_min == 60
        assert settings.auth_failure_agent_burst == 20
        assert settings.trusted_proxy_hops == 0

    def test_configured_values_are_honoured(self) -> None:
        env = {
            "HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN": "15",
            "HAMMERTIME_INGEST_AUTH_FAILURE_BURST": "5",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN": "45",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST": "12",
            "HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS": "2",
        }

        settings = load_settings(env)

        assert settings.auth_failure_rate_per_min == 15
        assert settings.auth_failure_burst == 5
        assert settings.auth_failure_agent_rate_per_min == 45
        assert settings.auth_failure_agent_burst == 12
        assert settings.trusted_proxy_hops == 2

    @pytest.mark.parametrize(
        "key",
        [
            "HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN",
            "HAMMERTIME_INGEST_AUTH_FAILURE_BURST",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST",
        ],
    )
    def test_non_numeric_value_is_rejected_at_startup(self, key: str) -> None:
        # Section 36.5: "All five MUST be rejected at startup if non-numeric".
        with pytest.raises(ValueError, match=key):
            load_settings({key: "not-a-number"})

    @pytest.mark.parametrize(
        "key",
        [
            "HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN",
            "HAMMERTIME_INGEST_AUTH_FAILURE_BURST",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST",
        ],
    )
    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_non_positive_budget_value_is_rejected_at_startup(self, key: str, value: str) -> None:
        # Section 36.5: "the four budget values if not positive".
        with pytest.raises(ValueError, match=key):
            load_settings({key: value})

    def test_negative_trusted_proxy_hops_is_rejected_at_startup(self) -> None:
        # Section 36.5: "HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS MUST be a
        # non-negative integer."
        with pytest.raises(ValueError, match="HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS"):
            load_settings({"HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS": "-1"})

    def test_non_numeric_trusted_proxy_hops_is_rejected_at_startup(self) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS"):
            load_settings({"HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS": "not-a-number"})

    def test_trusted_proxy_hops_zero_is_explicitly_valid(self) -> None:
        # 0 is the default and must not be rejected as "falsy".
        settings = load_settings({"HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS": "0"})

        assert settings.trusted_proxy_hops == 0


class TestObservationBudgetConfig:
    """Section 36.6's config block and its startup validation."""

    def test_defaults_when_unset(self) -> None:
        settings = load_settings({})

        assert settings.observation_rate_limit_eps == 2000
        assert settings.observation_burst == 10000

    def test_configured_values_are_honoured(self) -> None:
        env = {
            "HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS": "500",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "20000",
        }

        settings = load_settings(env)

        assert settings.observation_rate_limit_eps == 500
        assert settings.observation_burst == 20000

    def test_observation_burst_below_max_observations_is_rejected_at_startup(self) -> None:
        # Section 36.6: "HAMMERTIME_INGEST_OBSERVATION_BURST MUST be
        # greater than or equal to HAMMERTIME_INGEST_MAX_OBSERVATIONS, and
        # the service MUST refuse to start otherwise."
        env = {
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS": "10000",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "9999",
        }

        with pytest.raises(ValueError, match="OBSERVATION_BURST"):
            load_settings(env)

    def test_the_rejection_names_both_keys(self) -> None:
        # ADR-0008 implementation notes: "naming both keys and both values".
        env = {
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS": "10000",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "1",
        }

        with pytest.raises(ValueError) as excinfo:
            load_settings(env)

        message = str(excinfo.value)
        assert "OBSERVATION_BURST" in message
        assert "MAX_OBSERVATIONS" in message

    def test_observation_burst_equal_to_max_observations_is_accepted(self) -> None:
        # The boundary itself ("greater than or equal to") must not be
        # rejected -- only strictly-below is invalid.
        env = {
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS": "10000",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "10000",
        }

        settings = load_settings(env)

        assert settings.observation_burst == settings.max_observations == 10000

    def test_observation_burst_above_max_observations_is_accepted(self) -> None:
        env = {
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS": "500",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "10000",
        }

        settings = load_settings(env)

        assert settings.observation_burst == 10000
        assert settings.max_observations == 500

    def test_default_observation_burst_satisfies_default_max_observations(self) -> None:
        # Regression guard: the documented defaults (10000 / 10000) must
        # themselves pass load_settings's own validation -- a service that
        # cannot start with its own advertised defaults would be a bug.
        settings = load_settings({})

        assert settings.observation_burst >= settings.max_observations

    @pytest.mark.parametrize(
        "key",
        [
            "HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS",
            "HAMMERTIME_INGEST_OBSERVATION_BURST",
        ],
    )
    def test_non_numeric_value_is_rejected_at_startup(self, key: str) -> None:
        with pytest.raises(ValueError, match=key):
            load_settings({key: "not-a-number"})

    @pytest.mark.parametrize(
        "key",
        [
            "HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS",
            "HAMMERTIME_INGEST_OBSERVATION_BURST",
        ],
    )
    def test_non_positive_value_is_rejected_at_startup(self, key: str) -> None:
        with pytest.raises(ValueError, match=key):
            load_settings({key: "0"})


# --------------------------------------------------------------------------
# Section 47.1 step 3 / 47.5 / 47.7, ADR-0009 decision 2 and Amendment 3 (A12)
# --------------------------------------------------------------------------

# A URL the redis driver refuses to build a client from (unsupported scheme),
# carrying a password in its userinfo -- the case A12 exists for.
MALFORMED_REDIS_URL = "http://alice:s3cret-pw@db.internal:6379/0"
PASSWORD = "s3cret-pw"

# A12 item 2: an *unset* variable takes this default, which is valid.
DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# Valid, and distinctive in every component -- userinfo, host, port, path db
# and a typed query parameter -- so that "stored untouched" is observable.
VALID_REDIS_URL = "rediss://alice:s3cret-pw@db.internal:6380/2?socket_timeout=1.5"


def _stdout_records(capsys: pytest.CaptureFixture[str]) -> tuple[str, list[dict[str, Any]]]:
    """Captured stdout and its records, one JSON object per line (section 47.7).

    ADR-0009 A7 names this the seam for the runner's own records:
    `configure_logging` runs inside `run_service` and binds whatever
    `sys.stdout` is at that moment, which is the capture fixture's stream.
    """
    out = capsys.readouterr().out
    records: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - failure path
            raise AssertionError(
                f"section 47.7: every record is one JSON object per line; got {line!r}"
            ) from exc
        records.append(record)
    return out, records


class TestRedisUrlValidation:
    """ADR-0009 A12: `HAMMERTIME_REDIS_URL` is validated by `load_settings`,
    through `hammertime.store.validate_redis_url`, whenever the store kind is
    `redis` -- so a URL the driver would refuse is `config_invalid` and exit 2
    (section 47.1 step 3) rather than a half-started service."""

    def test_a_malformed_url_is_rejected_when_the_store_kind_is_unset(self) -> None:
        # A12 item 3: `redis` is the default store kind, so the check applies.
        with pytest.raises(ValueError, match="HAMMERTIME_REDIS_URL"):
            load_settings({"HAMMERTIME_REDIS_URL": MALFORMED_REDIS_URL})

    def test_a_malformed_url_is_rejected_when_the_store_kind_is_redis(self) -> None:
        env = {
            "HAMMERTIME_STORE_KIND": "redis",
            "HAMMERTIME_REDIS_URL": MALFORMED_REDIS_URL,
        }

        with pytest.raises(ValueError, match="HAMMERTIME_REDIS_URL"):
            load_settings(env)

    @pytest.mark.parametrize(
        "component",
        [PASSWORD, MALFORMED_REDIS_URL],
        ids=["password", "whole-value"],
    )
    def test_the_rejection_repeats_no_part_of_the_value(self, component: str) -> None:
        # A12 item 4 / section 47.7: the value may carry a password, so the
        # message names the variable and nothing derived from the value.
        with pytest.raises(ValueError) as excinfo:
            load_settings({"HAMMERTIME_REDIS_URL": MALFORMED_REDIS_URL})

        assert component not in str(excinfo.value)

    def test_a_set_but_empty_url_is_rejected_under_redis(self) -> None:
        # A12 item 2: set-but-empty is an error, not the default.
        env = {"HAMMERTIME_STORE_KIND": "redis", "HAMMERTIME_REDIS_URL": ""}

        with pytest.raises(ValueError, match="HAMMERTIME_REDIS_URL"):
            load_settings(env)

    def test_an_unset_url_takes_the_documented_default(self) -> None:
        settings = load_settings({"HAMMERTIME_STORE_KIND": "redis"})

        assert settings.redis_url == DEFAULT_REDIS_URL

    def test_a_valid_url_is_stored_byte_identically(self) -> None:
        # A12 item 2: "neither it nor `load_settings` strips or normalises it"
        # -- what `Redis.from_url` later receives is the exact string.
        env = {
            "HAMMERTIME_STORE_KIND": "redis",
            "HAMMERTIME_REDIS_URL": VALID_REDIS_URL,
        }

        settings = load_settings(env)

        assert settings.redis_url == VALID_REDIS_URL

    @pytest.mark.parametrize(
        "value",
        [MALFORMED_REDIS_URL, ""],
        ids=["malformed", "set-but-empty"],
    )
    def test_the_url_is_ignored_not_rejected_under_memory(self, value: str) -> None:
        # A12 item 3: under `memory` the value is never interpreted, so a
        # set-but-malformed URL cannot cause a half-start and is not refused.
        env = {"HAMMERTIME_STORE_KIND": "memory", "HAMMERTIME_REDIS_URL": value}

        settings = load_settings(env)

        assert settings.redis_url == value

    def test_an_invalid_store_kind_is_what_is_reported(self) -> None:
        # A12 Assumptions: "Error precedence ... is first-in-load-order ...
        # `store_kind` is parsed before the URL, so an invalid kind is
        # reported and the URL never checked".
        env = {
            "HAMMERTIME_STORE_KIND": "postgres",
            "HAMMERTIME_REDIS_URL": MALFORMED_REDIS_URL,
        }

        with pytest.raises(ValueError, match="HAMMERTIME_STORE_KIND"):
            load_settings(env)


class TestAMalformedRedisUrlExitsTwo:
    """The defect A12 fixes, end to end: section 47.1 step 3 and 47.5.

    Before the fix a malformed URL escaped ingest's `start()` -- after
    `starting` had been logged -- and was reported as `start_failed` and exit
    1, telling an orchestrator to retry something that cannot come right.

    `load_settings` raises before any file is read, so no detection config,
    registry or token key is needed in the environment. `HAMMERTIME_LOG_LEVEL`
    is pinned to `info` (its default) because A7 requires it for the absence
    of the `starting` record to mean anything.
    """

    ENV: ClassVar[dict[str, str]] = {
        "HAMMERTIME_REDIS_URL": MALFORMED_REDIS_URL,
        "HAMMERTIME_LOG_LEVEL": "info",
    }

    def _run(self) -> int:
        env = dict(self.ENV)
        return run_service("ingest", lambda: build_service(load_settings(env)), env=env)

    def test_the_process_exits_2(self) -> None:
        assert self._run() == 2

    def test_config_invalid_names_the_variable(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._run()

        _, records = _stdout_records(capsys)
        invalid = [record for record in records if record.get("event") == "config_invalid"]
        assert len(invalid) == 1
        assert "HAMMERTIME_REDIS_URL" in str(invalid[0].get("error"))

    def test_nothing_started(self, capsys: pytest.CaptureFixture[str]) -> None:
        # "A service must never half-start on a bad configuration": the
        # rejection happens at decision 5 step 2, before step 3's `starting`.
        self._run()

        _, records = _stdout_records(capsys)
        assert [record for record in records if record.get("event") == "starting"] == []

    def test_no_line_of_stdout_carries_the_password(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Section 47.7: "No record -- of any event -- may contain a
        # credential ... not the userinfo of a store URL."
        self._run()

        out, records = _stdout_records(capsys)
        assert PASSWORD not in out
        for record in records:
            assert PASSWORD not in json.dumps(record)


# --------------------------------------------------------------------------
# ADR-0013 decision 10: `HAMMERTIME_BUS_KIND` is `nats` | `memory`
# --------------------------------------------------------------------------

DEFAULT_BUS_BROKERS = "nats://localhost:4222"


class TestBusKind:
    """ADR-0013 decision 10: "`nats` (default) or `memory`; `kafka` is a
    `ValueError` (exit 2)"."""

    def test_the_default_is_nats(self) -> None:
        assert load_settings({}).bus_kind == "nats"

    @pytest.mark.parametrize("kind", ["nats", "memory"])
    def test_nats_and_memory_are_accepted(self, kind: str) -> None:
        assert load_settings({"HAMMERTIME_BUS_KIND": kind}).bus_kind == kind

    def test_kafka_is_a_value_error_naming_the_variable(self) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_BUS_KIND"):
            load_settings({"HAMMERTIME_BUS_KIND": "kafka"})

    @pytest.mark.parametrize("kind", ["", "redpanda", "jetstream"])
    def test_any_other_kind_is_a_value_error_naming_the_variable(self, kind: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_BUS_KIND"):
            load_settings({"HAMMERTIME_BUS_KIND": kind})

    def test_the_brokers_default_is_a_nats_url(self) -> None:
        assert load_settings({}).bus_brokers == DEFAULT_BUS_BROKERS

    def test_a_value_every_entry_of_which_nats_py_accepts_is_stored_as_given(self) -> None:
        # Decision 10 as superseded (Amendment 4 ruling S2): every entry is
        # checked by `validate_bus_url`, which refuses only what nats-py's own
        # parse would refuse plus an `@` outside the authority (assumption 74).
        # Both values pass -- `not a url at all` is `nats://not a url at all`
        # to nats-py, an authority with no port -- and the value is stored as
        # given, unsplit, for `NatsBus` to split.
        for value in ("nats://nats:4222,nats://nats-2:4222", "not a url at all"):
            assert load_settings({"HAMMERTIME_BUS_BROKERS": value}).bus_brokers == value


# --------------------------------------------------------------------------
# ADR-0013 decision 10 as superseded (Amendment 4 ruling S2): every entry of
# `HAMMERTIME_BUS_BROKERS` is checked by `validate_bus_url`
# --------------------------------------------------------------------------

# An entry nats-py's own parse would refuse *and echo*: `urlsplit` ends the
# authority at the `/`, and `.port` is the cast of `s3cr` (decision 3, as
# amended). The password's halves, the whole password and the username must
# reach no message; the tokens are chosen so that none is a substring of the
# fixed refusal text or of ordinary English.
BROKERS_WITH_A_SLASH_IN_THE_PASSWORD = "nats://usr7:s3cr/et@nats:4222"
BROKERS_WITH_AN_UNBALANCED_BRACKET = "nats://[::1"
BROKERS_REFUSED_FRAGMENTS = ("usr7", "s3cr", "et@", "s3cr/et")

# Userinfo in the form nats-py reads, alongside a second server: accepted, and
# stored untouched.
BROKERS_WITH_USERINFO_AND_A_SECOND_SERVER = "nats://user:s3cret@nats:4222, nats://b:4222"


class TestBusBrokersAreValidated:
    """ADR-0013 decision 10 as superseded (Amendment 4 ruling S2): "an entry nats-py's
    own parse would refuse is a `ValueError` naming the variable and the
    entry's position, `config_invalid`, exit 2"."""

    def test_an_entry_with_a_slash_in_its_password_is_a_value_error_naming_the_variable(
        self,
    ) -> None:
        env = {
            "HAMMERTIME_STORE_KIND": "memory",
            "HAMMERTIME_BUS_BROKERS": BROKERS_WITH_A_SLASH_IN_THE_PASSWORD,
        }

        with pytest.raises(ValueError, match="HAMMERTIME_BUS_BROKERS"):
            load_settings(env)

    @pytest.mark.parametrize("fragment", BROKERS_REFUSED_FRAGMENTS)
    def test_the_rejection_repeats_no_part_of_the_entry(self, fragment: str) -> None:
        # Decision 3 (as amended): "the message names none of the entry's
        # text"; section 47.7 / A12 item 4: the variable, never the value.
        assert fragment in BROKERS_WITH_A_SLASH_IN_THE_PASSWORD
        env = {
            "HAMMERTIME_STORE_KIND": "memory",
            "HAMMERTIME_BUS_BROKERS": BROKERS_WITH_A_SLASH_IN_THE_PASSWORD,
        }

        with pytest.raises(ValueError) as excinfo:
            load_settings(env)

        message = str(excinfo.value)
        assert fragment not in message, f"{fragment!r} echoed in {message!r}"
        assert BROKERS_WITH_A_SLASH_IN_THE_PASSWORD not in message

    def test_an_unbalanced_bracket_is_a_value_error_naming_the_variable(self) -> None:
        # (i) "`urlsplit` refuses it" -- the same entry `bus_endpoints` renders
        # `<unparseable>`.
        env = {
            "HAMMERTIME_STORE_KIND": "memory",
            "HAMMERTIME_BUS_BROKERS": BROKERS_WITH_AN_UNBALANCED_BRACKET,
        }

        with pytest.raises(ValueError, match="HAMMERTIME_BUS_BROKERS") as excinfo:
            load_settings(env)

        assert BROKERS_WITH_AN_UNBALANCED_BRACKET not in str(excinfo.value)
        assert "::1" not in str(excinfo.value)

    def test_a_refused_entry_is_refused_wherever_it_sits_in_the_list(self) -> None:
        # "for every entry of `HAMMERTIME_BUS_BROKERS` (split as
        # `NatsBus.__init__` splits it)": a good first entry does not excuse a
        # bad second one.
        env = {
            "HAMMERTIME_STORE_KIND": "memory",
            "HAMMERTIME_BUS_BROKERS": f"nats://nats:4222, {BROKERS_WITH_A_SLASH_IN_THE_PASSWORD}",
        }

        with pytest.raises(ValueError, match="HAMMERTIME_BUS_BROKERS") as excinfo:
            load_settings(env)

        for fragment in BROKERS_REFUSED_FRAGMENTS:
            assert fragment in env["HAMMERTIME_BUS_BROKERS"]
            assert fragment not in str(excinfo.value)

    def test_userinfo_that_nats_py_reads_still_loads(self) -> None:
        # "no value that connected before is refused now": decision 10's two
        # userinfo forms are legal, and the value is stored untouched.
        env = {
            "HAMMERTIME_STORE_KIND": "memory",
            "HAMMERTIME_BUS_BROKERS": BROKERS_WITH_USERINFO_AND_A_SECOND_SERVER,
        }

        settings = load_settings(env)

        assert settings.bus_brokers == BROKERS_WITH_USERINFO_AND_A_SECOND_SERVER

    @pytest.mark.parametrize(
        "value",
        [
            "nats://s3cret-token@nats:4222",
            "tls://user:s3cret@[::1]:4222/?x=1",
            "user:s3cret@nats:4222",
            "nats://nats:4222/",
        ],
    )
    def test_every_pinned_accepted_form_still_loads(self, value: str) -> None:
        env = {"HAMMERTIME_STORE_KIND": "memory", "HAMMERTIME_BUS_BROKERS": value}

        assert load_settings(env).bus_brokers == value

    @pytest.mark.parametrize(
        "value",
        [BROKERS_WITH_A_SLASH_IN_THE_PASSWORD, BROKERS_WITH_AN_UNBALANCED_BRACKET],
        ids=["slash-in-password", "unbalanced-bracket"],
    )
    def test_a_refused_entry_is_ignored_not_rejected_under_memory(self, value: str) -> None:
        # Decision 3 as amended (Amendment 5 ruling 5): `load_settings` checks
        # the entries "when `bus_kind` is `nats` ...: under `memory` the value
        # is never interpreted, so a set-but-malformed one is ignored, not
        # rejected -- ADR-0009 A12's rule for `HAMMERTIME_REDIS_URL` under
        # `store_kind=memory`, applied to the parallel key". Decision 10 says
        # the same in its row: "under `memory` the value is ignored, not
        # rejected".
        env = {
            "HAMMERTIME_STORE_KIND": "memory",
            "HAMMERTIME_BUS_KIND": "memory",
            "HAMMERTIME_BUS_BROKERS": value,
        }

        settings = load_settings(env)

        assert settings.bus_brokers == value


class TestAMalformedBusBrokersExitsTwo:
    """Ruling S2 end to end: "the process exits 2 with `config_invalid` before nats-py
    sees the value" -- and no line of stdout carries the password's halves. Same
    shape as `TestAMalformedRedisUrlExitsTwo`."""

    ENV: ClassVar[dict[str, str]] = {
        "HAMMERTIME_STORE_KIND": "memory",
        "HAMMERTIME_BUS_KIND": "nats",
        "HAMMERTIME_BUS_BROKERS": BROKERS_WITH_A_SLASH_IN_THE_PASSWORD,
        "HAMMERTIME_LOG_LEVEL": "info",
    }

    def _run(self) -> int:
        env = dict(self.ENV)
        return run_service("ingest", lambda: build_service(load_settings(env)), env=env)

    def test_the_process_exits_2(self) -> None:
        assert self._run() == 2

    def test_config_invalid_names_the_variable(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._run()

        _, records = _stdout_records(capsys)
        invalid = [record for record in records if record.get("event") == "config_invalid"]
        assert len(invalid) == 1
        assert "HAMMERTIME_BUS_BROKERS" in str(invalid[0].get("error"))

    def test_nothing_started(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._run()

        _, records = _stdout_records(capsys)
        assert [record for record in records if record.get("event") == "starting"] == []

    @pytest.mark.parametrize("fragment", BROKERS_REFUSED_FRAGMENTS)
    def test_no_line_of_stdout_carries_the_entrys_text(
        self, fragment: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Section 47.7, as extended to the userinfo of a bus URL.
        assert fragment in BROKERS_WITH_A_SLASH_IN_THE_PASSWORD
        self._run()

        out, records = _stdout_records(capsys)
        assert fragment not in out
        for record in records:
            assert fragment not in json.dumps(record)


# --------------------------------------------------------------------------
# ADR-0013 decision 3 `bus_endpoints` / Amendment 2 rulings 2-3; section 47.7
# --------------------------------------------------------------------------

# One entry in each of the two userinfo forms nats-py reads (Amendment 2
# ruling 1): `user:password@` and a lone `token@`. Both are legal values of
# `HAMMERTIME_BUS_BROKERS`, and neither may reach a log record.
BUS_BROKERS_WITH_USERINFO = "nats://user:s3cret@nats:4222,nats://tok3n@other:4222"
BUS_ENDPOINTS = ["nats://nats:4222", "nats://other:4222"]

# Decision 3's examples: the password, the token, the username (nats-py reads
# a lone username as a token, assumption 42) and the `@` that would betray
# that userinfo was present at all.
BUS_USERINFO_FRAGMENTS = ("s3cret", "tok3n", "user:", "@")

# The repository's own detection document, resolved as `test_pipeline.py`
# resolves it.
_CONFIG_PATH = Path(__file__).parents[6] / "config" / "detection.v1.json"

# ADR-0006: a fixed 32-byte test key and one hashed record, exactly as
# `test_routes.py` builds its registry -- never a real env var or file.
_AGENT_TOKEN_KEY = b"0" * 32
_AGENT_ID = "edge-17"
_AGENT_TOKEN = "test-token"


def _registry() -> AgentRegistry:
    token_hash = bytes.fromhex(hash_token(_AGENT_TOKEN, key=_AGENT_TOKEN_KEY))
    return AgentRegistry.from_records(
        [AgentRecord(agent_id=_AGENT_ID, token_hash=token_hash, rate_limit_rps=None)],
        key=_AGENT_TOKEN_KEY,
    )


class TestStartingRecordCarriesBusEndpoints:
    """ADR-0013 Amendment 2 rulings 2-3: the `starting` record "logs
    `bus_endpoints` **in place of** `bus_brokers`", and "the rendered text of
    every record ... MUST NOT contain the userinfo of a bus URL"."""

    def _fields(self) -> dict[str, Any]:
        env = {
            "HAMMERTIME_STORE_KIND": "memory",
            "HAMMERTIME_BUS_KIND": "memory",
            "HAMMERTIME_BUS_BROKERS": BUS_BROKERS_WITH_USERINFO,
            "HAMMERTIME_CONFIG_PATH": str(_CONFIG_PATH),
        }
        settings = load_settings(env)
        # Decision 10 / assumption 15: the setting itself is untouched -- the
        # reduction happens at the record, not at `load_settings`.
        assert settings.bus_brokers == BUS_BROKERS_WITH_USERINFO
        service = build_service(settings, bus=InMemoryBus(), agent_registry=_registry())
        fields: dict[str, Any] = dict(service.startup_fields())
        return fields

    def test_bus_endpoints_is_the_reduced_list(self) -> None:
        # Decision 3: "`<scheme>://` followed by the part of the authority
        # after its **last** `@`", one entry per server, in order.
        fields = self._fields()

        assert fields["bus_endpoints"] == BUS_ENDPOINTS

    def test_bus_brokers_is_no_longer_a_field(self) -> None:
        # Ruling 3 / assumption 47: "field renamed (`bus_endpoints`) rather
        # than value replaced under `bus_brokers`", so "no reader mistakes the
        # field for the configured value". ADR-0009 A7's row is superseded.
        fields = self._fields()

        assert "bus_brokers" not in fields

    def test_the_field_does_not_depend_on_the_bus_kind(self) -> None:
        # The service was built on the memory bus and says so; the reduction
        # of `HAMMERTIME_BUS_BROKERS` is reported regardless.
        fields = self._fields()

        assert fields["bus_kind"] == "memory"
        assert fields["bus_endpoints"] == BUS_ENDPOINTS

    @pytest.mark.parametrize(
        "fragment",
        BUS_USERINFO_FRAGMENTS,
        ids=["password", "token", "username", "at-sign"],
    )
    def test_the_rendered_fields_carry_no_userinfo(self, fragment: str) -> None:
        # Ruling 2 / section 47.7: not the password, not the username (a lone
        # username is a token to nats-py), and no `@` at all -- the `starting`
        # record is rendered from exactly these fields.
        rendered = json.dumps(self._fields(), default=str)

        assert fragment not in rendered

    def test_the_whole_configured_value_is_absent(self) -> None:
        # Decision 3: "no record of any event carries `HAMMERTIME_BUS_BROKERS`
        # ... verbatim or in any other derived form" that keeps the userinfo.
        rendered = json.dumps(self._fields(), default=str)

        assert BUS_BROKERS_WITH_USERINFO not in rendered
        for entry in BUS_BROKERS_WITH_USERINFO.split(","):
            assert entry not in rendered


class TestAKafkaBusKindExitsTwo:
    """Decision 10's `kafka` row, end to end: `config_invalid` naming the
    variable and exit 2, before anything starts. Same shape as
    `TestAMalformedRedisUrlExitsTwo`."""

    ENV: ClassVar[dict[str, str]] = {
        "HAMMERTIME_BUS_KIND": "kafka",
        "HAMMERTIME_STORE_KIND": "memory",
        "HAMMERTIME_LOG_LEVEL": "info",
    }

    def _run(self) -> int:
        env = dict(self.ENV)
        return run_service("ingest", lambda: build_service(load_settings(env)), env=env)

    def test_the_process_exits_2(self) -> None:
        assert self._run() == 2

    def test_config_invalid_names_the_variable(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._run()

        _, records = _stdout_records(capsys)
        invalid = [record for record in records if record.get("event") == "config_invalid"]
        assert len(invalid) == 1
        assert "HAMMERTIME_BUS_KIND" in str(invalid[0].get("error"))

    def test_nothing_started(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._run()

        _, records = _stdout_records(capsys)
        assert [record for record in records if record.get("event") == "starting"] == []
