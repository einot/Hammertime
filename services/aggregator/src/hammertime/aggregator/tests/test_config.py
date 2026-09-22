"""Aggregator settings: `HAMMERTIME_REDIS_URL` is validated before anything opens;
`HAMMERTIME_SHARD_IDS` is required; the bus is `nats` or `memory`; the member
id and lease TTL of ADR-0013 decision 7.

Spec: section 47.1 step 3 (an invalid configuration -- "including a
connection URL the client library would refuse to build a client from
(`HAMMERTIME_REDIS_URL` is validated when `HAMMERTIME_STORE_KIND` is `redis`
and ignored when it is `memory`)" -- is rejected before any network
connection is opened, exiting with status 2), section 47.5 (the exit status
table) and section 47.7 (one JSON object per stdout line; no record of any
event may carry the userinfo of a store URL).

ADR-0009 decision 2 as corrected by Amendment 3 item A12: each service's
`load_settings` calls `hammertime.store.validate_redis_url(value)` when the
store kind resolves to `redis`, and stores the identical string. ADR-0011
decision 9 is where `hammertime.aggregator.config.load_settings(env)` and
its `HAMMERTIME_STORE_KIND` / `HAMMERTIME_REDIS_URL` keys are listed.

ADR-0013 decisions 6, 7 and 10 (`TestShardIdsAreRequired`, `TestBusKind`,
`TestMemberIdAndLeaseTtl`, `TestAutoShardIdsExitsTwo` below), written from
that ADR's text alone:

* Decision 6: "`load_settings` treats an **unset** `HAMMERTIME_SHARD_IDS` as
  a `ValueError` naming the variable"; `all` -> `frozenset(range(128))`;
  `auto` -> `ValueError` "so a deployment carrying the old default fails
  loudly with `config_invalid` and exit 2", whose message (Amendment 1
  ruling T10) "MUST contain the variable name, the word `all`, `ADR-0013`
  and at least one explicit-set example"; an id at or above the count is a
  `ValueError` naming the variable and the count. `AggregatorSettings.shard_ids`
  "becomes `frozenset[int]` (never `None`)". The grammar itself is
  `test_sharding.py::TestParseShardIds`'s.
* Decision 10's table: `HAMMERTIME_BUS_KIND` is "`nats` (default) or
  `memory`; `kafka` is a `ValueError` (exit 2)"; `HAMMERTIME_BUS_BROKERS`
  defaults to `nats://localhost:4222` and is "not validated by
  `load_settings`" (assumption 15).
* Decision 7: "`HAMMERTIME_AGGREGATOR_MEMBER_ID` (default
  `socket.gethostname()`; set-but-empty is a `ValueError`) and
  `HAMMERTIME_AGGREGATOR_LEASE_TTL_S` (default 30; a positive number that
  MUST exceed `HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S`, else
  `ValueError`, since renewal happens once per maintenance interval)."

This follows the convention of `services/ingest/.../tests/test_config.py`:
every test passes an explicit `env` mapping, so no test touches
`os.environ`. `TestRedisUrlValidation` mirrors the ingest file's class of
the same name deliberately: A12 states one rule for both services, and two
copies that can be diffed against each other are the point. The one
difference is `SHARD_IDS`: every aggregator `env` below carries
`HAMMERTIME_SHARD_IDS=all`, because ADR-0013 decision 6 makes the key
required and the tests about *other* keys must not trip over it.

ASSUMPTIONS -- things ADR-0013 does not pin. Adjust the test, not the
meaning:

1. **Field names.** `AggregatorSettings.shard_ids` is the ADR's own name.
   `bus_kind` / `bus_brokers` are taken from `IngestSettings`, which
   `services/ingest/.../tests/test_pipeline.py` constructs with exactly those
   names and the ADR treats the two services' bus keys as one rule;
   `member_id` and `lease_ttl_s` are the `ShardClaims` keyword names of
   decision 7 ("constructed with two new keyword arguments, `member_id: str`
   and `lease_ttl_s: float`") and the `startup_fields()` key decision 6
   names (`member_id`); `maintenance_interval_s` follows the same
   `<key without prefix>` pattern for `HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S`
   (ADR-0011 decision 9 names the key, not the field).
2. **Error precedence between `HAMMERTIME_SHARD_IDS` and other keys** is
   "first-in-load-order" per A12's assumptions and not pinned here; every
   test that expects a *different* key's error supplies a valid shard set,
   and every test that expects the shard-id error supplies nothing else
   that could fail.
3. **`HAMMERTIME_AGGREGATOR_LEASE_TTL_S` equal to the maintenance interval
   is rejected**: "MUST exceed" is strict. A non-numeric or non-positive
   value is a `ValueError` naming the variable, the pattern every numeric
   key in this repository follows.
4. **`HAMMERTIME_BUS_KIND` is matched as written** (`nats`, `memory`); no
   claim is made about case-insensitivity either way.

`TestBusBrokersAreValidated` covers ADR-0013 decision 10's
`HAMMERTIME_BUS_BROKERS` row as superseded 2026-09-22 (Amendment 4 ruling
S2): "every entry is checked by `hammertime.bus.nats.validate_bus_url` in
`load_settings`, and an entry nats-py's own parse would refuse is a
`ValueError` naming the variable and the entry's position, `config_invalid`,
exit 2"; decision 3 (as amended): "`load_settings` in the aggregator and in
ingest calls it for every entry of `HAMMERTIME_BUS_BROKERS` (split as
`NatsBus.__init__` splits it) and turns a refusal into a `ValueError` naming
the variable and the entry's position, so the process exits 2 with
`config_invalid` before nats-py sees the value -- the store side's
`validate_redis_url` shape". Assumption 15 ("not validated") is superseded;
`test_the_brokers_value_is_stored_untouched_and_not_validated` above stays
as it is because both of its values are still accepted ("no value that
connected before is refused now"). Error precedence between this key and
others is not pinned (ruling T7): every test here supplies valid values for
every other key. The needles `pa`, `ss` and `user` are the dispatch brief's;
they exclude ordinary words such as "parse" and "address" from the message,
which must therefore name the variable and the position and nothing more.

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
assumption 15), so no NATS server is needed to observe it.

ASSUMPTIONS for that class:

5. **`HAMMERTIME_CONFIG_PATH` is the detection-config key** `load_settings`
   reads into `detection_config_path` (ADR-0009 decision 6, `.env.example`);
   it is pointed at the repository's `config/detection.v1.json` the same way
   `services/ingest/.../tests/test_pipeline.py` resolves it, so `build_service`
   (which "loads the document", ADR-0009 A5) needs no cwd.
6. **`startup_fields()` returns a mapping whose values are JSON-renderable**,
   which is what a `starting` record requires; `json.dumps(..., default=str)`
   is used so a `Path`-valued field would still be rendered and scanned
   rather than fail the test for an unrelated reason.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, ClassVar

import pytest
from hammertime.aggregator.config import load_settings
from hammertime.aggregator.service import build_service
from hammertime.bus.memory import InMemoryBus
from hammertime.bus.topics import OBSERVATIONS
from hammertime.core.runtime import run_service

# A URL the redis driver refuses to build a client from (unsupported scheme),
# carrying a password in its userinfo -- the case A12 exists for.
MALFORMED_REDIS_URL = "http://alice:s3cret-pw@db.internal:6379/0"
PASSWORD = "s3cret-pw"

# A12 item 2: an *unset* variable takes this default, which is valid.
DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# Valid, and distinctive in every component -- userinfo, host, port, path db
# and a typed query parameter -- so that "stored untouched" is observable.
VALID_REDIS_URL = "rediss://alice:s3cret-pw@db.internal:6380/2?socket_timeout=1.5"

# ADR-0013 decision 6: the key is required; `all` is the reference
# single-member setting (`.env.example`, `deploy/docker-compose.yml`).
SHARD_IDS = {"HAMMERTIME_SHARD_IDS": "all"}
ALL_SHARDS = frozenset(range(OBSERVATIONS.partitions))

# ADR-0013 decision 10's table.
DEFAULT_BUS_BROKERS = "nats://localhost:4222"


def _env(**overrides: str) -> dict[str, str]:
    """An `env` with the required shard set plus `overrides`."""

    return {**SHARD_IDS, **overrides}


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
    (section 47.1 step 3) rather than an error from wherever the client
    happens to be constructed."""

    def test_a_malformed_url_is_rejected_when_the_store_kind_is_unset(self) -> None:
        # A12 item 3: `redis` is the default store kind, so the check applies.
        with pytest.raises(ValueError, match="HAMMERTIME_REDIS_URL"):
            load_settings(_env(HAMMERTIME_REDIS_URL=MALFORMED_REDIS_URL))

    def test_a_malformed_url_is_rejected_when_the_store_kind_is_redis(self) -> None:
        env = _env(HAMMERTIME_STORE_KIND="redis", HAMMERTIME_REDIS_URL=MALFORMED_REDIS_URL)

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
            load_settings(_env(HAMMERTIME_REDIS_URL=MALFORMED_REDIS_URL))

        assert component not in str(excinfo.value)

    def test_a_set_but_empty_url_is_rejected_under_redis(self) -> None:
        # A12 item 2: set-but-empty is an error, not the default -- the same
        # rule ADR-0011 A3 applies to `HAMMERTIME_SHARD_IDS`.
        env = _env(HAMMERTIME_STORE_KIND="redis", HAMMERTIME_REDIS_URL="")

        with pytest.raises(ValueError, match="HAMMERTIME_REDIS_URL"):
            load_settings(env)

    def test_an_unset_url_takes_the_documented_default(self) -> None:
        settings = load_settings(_env(HAMMERTIME_STORE_KIND="redis"))

        assert settings.redis_url == DEFAULT_REDIS_URL

    def test_a_valid_url_is_stored_byte_identically(self) -> None:
        # A12 item 2: "neither it nor `load_settings` strips or normalises it"
        # -- what `Redis.from_url` later receives is the exact string.
        env = _env(HAMMERTIME_STORE_KIND="redis", HAMMERTIME_REDIS_URL=VALID_REDIS_URL)

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
        env = _env(HAMMERTIME_STORE_KIND="memory", HAMMERTIME_REDIS_URL=value)

        settings = load_settings(env)

        assert settings.redis_url == value

    def test_an_invalid_store_kind_is_what_is_reported(self) -> None:
        # A12 Assumptions: "Error precedence ... is first-in-load-order ...
        # `store_kind` is parsed before the URL, so an invalid kind is
        # reported and the URL never checked".
        env = _env(HAMMERTIME_STORE_KIND="postgres", HAMMERTIME_REDIS_URL=MALFORMED_REDIS_URL)

        with pytest.raises(ValueError, match="HAMMERTIME_STORE_KIND"):
            load_settings(env)


class TestAMalformedRedisUrlExitsTwo:
    """The defect A12 fixes, end to end: section 47.1 step 3 and 47.5.

    For the aggregator the URL was first interpreted inside `build_service`,
    so exit 2 held "by accident of where `build_service` happens to construct
    the client, not by any rule; a later reordering of that function would
    silently move it". After A12 the rejection is `load_settings`'s, before
    the factory builds anything.

    `load_settings` raises before any file is read, so no detection config or
    registry is needed in the environment. `HAMMERTIME_LOG_LEVEL` is pinned to
    `info` (its default) because A7 requires it for the absence of the
    `starting` record to mean anything.
    """

    ENV: ClassVar[dict[str, str]] = {
        **SHARD_IDS,
        "HAMMERTIME_REDIS_URL": MALFORMED_REDIS_URL,
        "HAMMERTIME_LOG_LEVEL": "info",
    }

    def _run(self) -> int:
        env = dict(self.ENV)
        return run_service("aggregator", lambda: build_service(load_settings(env)), env=env)

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
# ADR-0013 decision 6: `HAMMERTIME_SHARD_IDS` is required, `all` or a set
# --------------------------------------------------------------------------


class TestShardIdsAreRequired:
    """ADR-0013 decision 6: "there is no safe default under static assignment
    -- `all` on every replica of a scaled deployment is the two-owners
    misconfiguration of #90, and the previous default `auto` no longer
    exists -- so the operator states the set" (assumption 16)."""

    def test_an_unset_variable_is_a_value_error_naming_it(self) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS"):
            load_settings({})

    def test_an_unset_variable_is_rejected_even_when_everything_else_is_valid(self) -> None:
        env = {"HAMMERTIME_STORE_KIND": "memory", "HAMMERTIME_BUS_KIND": "memory"}

        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS"):
            load_settings(env)

    def test_all_is_the_explicit_full_set(self) -> None:
        # "`all` (case-insensitive) -> `frozenset(range(OBSERVATIONS.partitions))`,
        # the explicit full set `0..127`. It is expanded at parse time".
        settings = load_settings(_env(HAMMERTIME_SHARD_IDS="all"))

        assert settings.shard_ids == ALL_SHARDS
        assert settings.shard_ids == frozenset(range(128))
        assert isinstance(settings.shard_ids, frozenset)

    @pytest.mark.parametrize("text", ["ALL", "All", " all "])
    def test_all_is_case_insensitive(self, text: str) -> None:
        assert load_settings(_env(HAMMERTIME_SHARD_IDS=text)).shard_ids == ALL_SHARDS

    def test_an_explicit_set_is_stored_as_a_frozenset(self) -> None:
        settings = load_settings(_env(HAMMERTIME_SHARD_IDS="0,2,5-7"))

        assert settings.shard_ids == frozenset({0, 2, 5, 6, 7})

    def test_shard_ids_is_never_none(self) -> None:
        # "`AggregatorSettings.shard_ids` becomes `frozenset[int]` (never
        # `None`)".
        for text in ("all", "0", "0-127"):
            assert load_settings(_env(HAMMERTIME_SHARD_IDS=text)).shard_ids is not None

    def test_auto_is_a_value_error_naming_the_variable(self) -> None:
        # "`auto` -> `ValueError` whose message says that shard assignment is
        # static since ADR-0013 and names the two accepted forms". As amended
        # (Amendment 1 ruling T10), the message "MUST contain the variable
        # name, the word `all`, `ADR-0013` and at least one explicit-set
        # example, and tests pin those four substrings rather than the whole
        # sentence". The examples the ADR's own wording carries are `'0'`,
        # `'0-3'` and `'0,2,5-7'`; the bare `0` would be satisfied by
        # `ADR-0013` itself, so the single-id example is matched quoted.
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS") as excinfo:
            load_settings(_env(HAMMERTIME_SHARD_IDS="auto"))

        message = str(excinfo.value)
        assert "all" in message
        assert "ADR-0013" in message
        assert any(example in message for example in ("0-3", "0,2,5-7", "'0'"))

    @pytest.mark.parametrize("text", ["", "   "], ids=["empty", "whitespace-only"])
    def test_set_but_empty_is_a_value_error(self, text: str) -> None:
        # ADR-0011 A3, unchanged.
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS"):
            load_settings(_env(HAMMERTIME_SHARD_IDS=text))

    @pytest.mark.parametrize("text", ["128", "0-128", "0,200", "127-130"])
    def test_an_id_at_or_above_the_partition_count_is_a_value_error(self, text: str) -> None:
        # "an id at or above the count is a `ValueError` naming the variable
        # and the count".
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS") as excinfo:
            load_settings(_env(HAMMERTIME_SHARD_IDS=text))

        assert "128" in str(excinfo.value)

    def test_the_highest_valid_id_is_accepted(self) -> None:
        assert load_settings(_env(HAMMERTIME_SHARD_IDS="127")).shard_ids == frozenset({127})


class TestAutoShardIdsExitsTwo:
    """ADR-0013 decision 6: a deployment carrying the old `auto` default
    "fails loudly with `config_invalid` and exit 2 rather than silently
    claiming nothing". Same shape as `TestAMalformedRedisUrlExitsTwo`."""

    ENV: ClassVar[dict[str, str]] = {
        "HAMMERTIME_SHARD_IDS": "auto",
        "HAMMERTIME_STORE_KIND": "memory",
        "HAMMERTIME_BUS_KIND": "memory",
        "HAMMERTIME_LOG_LEVEL": "info",
    }

    def _run(self) -> int:
        env = dict(self.ENV)
        return run_service("aggregator", lambda: build_service(load_settings(env)), env=env)

    def test_the_process_exits_2(self) -> None:
        assert self._run() == 2

    def test_config_invalid_names_the_variable(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._run()

        _, records = _stdout_records(capsys)
        invalid = [record for record in records if record.get("event") == "config_invalid"]
        assert len(invalid) == 1
        assert "HAMMERTIME_SHARD_IDS" in str(invalid[0].get("error"))

    def test_nothing_started(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._run()

        _, records = _stdout_records(capsys)
        assert [record for record in records if record.get("event") == "starting"] == []


class TestAnUnsetShardIdsExitsTwo:
    """Decision 6's unset case, end to end: exit 2 and `config_invalid` naming
    the variable, before anything starts."""

    ENV: ClassVar[dict[str, str]] = {
        "HAMMERTIME_STORE_KIND": "memory",
        "HAMMERTIME_BUS_KIND": "memory",
        "HAMMERTIME_LOG_LEVEL": "info",
    }

    def _run(self) -> int:
        env = dict(self.ENV)
        return run_service("aggregator", lambda: build_service(load_settings(env)), env=env)

    def test_the_process_exits_2(self) -> None:
        assert self._run() == 2

    def test_config_invalid_names_the_variable(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._run()

        _, records = _stdout_records(capsys)
        invalid = [record for record in records if record.get("event") == "config_invalid"]
        assert len(invalid) == 1
        assert "HAMMERTIME_SHARD_IDS" in str(invalid[0].get("error"))


# --------------------------------------------------------------------------
# ADR-0013 decision 10: `HAMMERTIME_BUS_KIND` is `nats` | `memory`
# --------------------------------------------------------------------------


class TestBusKind:
    """ADR-0013 decision 10: "`nats` (default) or `memory`; `kafka` is a
    `ValueError` (exit 2)"."""

    def test_the_default_is_nats(self) -> None:
        assert load_settings(_env()).bus_kind == "nats"

    @pytest.mark.parametrize("kind", ["nats", "memory"])
    def test_nats_and_memory_are_accepted(self, kind: str) -> None:
        assert load_settings(_env(HAMMERTIME_BUS_KIND=kind)).bus_kind == kind

    def test_kafka_is_a_value_error_naming_the_variable(self) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_BUS_KIND"):
            load_settings(_env(HAMMERTIME_BUS_KIND="kafka"))

    @pytest.mark.parametrize("kind", ["", "redpanda", "jetstream"])
    def test_any_other_kind_is_a_value_error_naming_the_variable(self, kind: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_BUS_KIND"):
            load_settings(_env(HAMMERTIME_BUS_KIND=kind))

    def test_the_brokers_default_is_a_nats_url(self) -> None:
        assert load_settings(_env()).bus_brokers == DEFAULT_BUS_BROKERS

    def test_the_brokers_value_is_stored_untouched_and_not_validated(self) -> None:
        # Assumption 15: "not validated by `load_settings`"; a malformed
        # value surfaces at `connect()` as `start_failed`, not here.
        for value in ("nats://nats:4222,nats://nats-2:4222", "not a url at all"):
            assert load_settings(_env(HAMMERTIME_BUS_BROKERS=value)).bus_brokers == value


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
        with pytest.raises(ValueError, match="HAMMERTIME_BUS_BROKERS"):
            load_settings(
                _env(
                    HAMMERTIME_STORE_KIND="memory",
                    HAMMERTIME_BUS_BROKERS=BROKERS_WITH_A_SLASH_IN_THE_PASSWORD,
                )
            )

    @pytest.mark.parametrize("fragment", BROKERS_REFUSED_FRAGMENTS)
    def test_the_rejection_repeats_no_part_of_the_entry(self, fragment: str) -> None:
        # Decision 3 (as amended): "the message names none of the entry's
        # text"; section 47.7 / A12 item 4: the variable, never the value.
        assert fragment in BROKERS_WITH_A_SLASH_IN_THE_PASSWORD
        with pytest.raises(ValueError) as excinfo:
            load_settings(
                _env(
                    HAMMERTIME_STORE_KIND="memory",
                    HAMMERTIME_BUS_BROKERS=BROKERS_WITH_A_SLASH_IN_THE_PASSWORD,
                )
            )

        message = str(excinfo.value)
        assert fragment not in message, f"{fragment!r} echoed in {message!r}"
        assert BROKERS_WITH_A_SLASH_IN_THE_PASSWORD not in message

    def test_an_unbalanced_bracket_is_a_value_error_naming_the_variable(self) -> None:
        # (i) "`urlsplit` refuses it" -- the same entry `bus_endpoints` renders
        # `<unparseable>`.
        with pytest.raises(ValueError, match="HAMMERTIME_BUS_BROKERS") as excinfo:
            load_settings(
                _env(
                    HAMMERTIME_STORE_KIND="memory",
                    HAMMERTIME_BUS_BROKERS=BROKERS_WITH_AN_UNBALANCED_BRACKET,
                )
            )

        assert BROKERS_WITH_AN_UNBALANCED_BRACKET not in str(excinfo.value)
        assert "::1" not in str(excinfo.value)

    def test_a_refused_entry_is_refused_wherever_it_sits_in_the_list(self) -> None:
        # "for every entry of `HAMMERTIME_BUS_BROKERS` (split as
        # `NatsBus.__init__` splits it)": a good first entry does not excuse a
        # bad second one.
        value = f"nats://nats:4222, {BROKERS_WITH_A_SLASH_IN_THE_PASSWORD}"

        with pytest.raises(ValueError, match="HAMMERTIME_BUS_BROKERS") as excinfo:
            load_settings(_env(HAMMERTIME_STORE_KIND="memory", HAMMERTIME_BUS_BROKERS=value))

        for fragment in BROKERS_REFUSED_FRAGMENTS:
            assert fragment in value
            assert fragment not in str(excinfo.value)

    def test_userinfo_that_nats_py_reads_still_loads(self) -> None:
        # "no value that connected before is refused now": decision 10's two
        # userinfo forms are legal, and the value is stored untouched.
        settings = load_settings(
            _env(
                HAMMERTIME_STORE_KIND="memory",
                HAMMERTIME_BUS_BROKERS=BROKERS_WITH_USERINFO_AND_A_SECOND_SERVER,
            )
        )

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
        env = _env(HAMMERTIME_STORE_KIND="memory", HAMMERTIME_BUS_BROKERS=value)

        assert load_settings(env).bus_brokers == value


class TestAMalformedBusBrokersExitsTwo:
    """Ruling S2 end to end: "the process exits 2 with `config_invalid` before nats-py
    sees the value" -- and no line of stdout carries the password's halves."""

    ENV: ClassVar[dict[str, str]] = {
        **SHARD_IDS,
        "HAMMERTIME_STORE_KIND": "memory",
        "HAMMERTIME_BUS_KIND": "nats",
        "HAMMERTIME_BUS_BROKERS": BROKERS_WITH_A_SLASH_IN_THE_PASSWORD,
        "HAMMERTIME_LOG_LEVEL": "info",
    }

    def _run(self) -> int:
        env = dict(self.ENV)
        return run_service("aggregator", lambda: build_service(load_settings(env)), env=env)

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

# ASSUMPTION 5: the repository's own detection document, resolved as
# `services/ingest/.../tests/test_pipeline.py` resolves it.
_CONFIG_PATH = Path(__file__).parents[6] / "config" / "detection.v1.json"


class TestStartingRecordCarriesBusEndpoints:
    """ADR-0013 Amendment 2 rulings 2-3: the `starting` record "logs
    `bus_endpoints` **in place of** `bus_brokers`", and "the rendered text of
    every record ... MUST NOT contain the userinfo of a bus URL"."""

    def _fields(self) -> dict[str, Any]:
        env = _env(
            HAMMERTIME_STORE_KIND="memory",
            HAMMERTIME_BUS_KIND="memory",
            HAMMERTIME_BUS_BROKERS=BUS_BROKERS_WITH_USERINFO,
            HAMMERTIME_CONFIG_PATH=str(_CONFIG_PATH),
        )
        settings = load_settings(env)
        # Decision 10 / assumption 15: the setting itself is untouched -- the
        # reduction happens at the record, not at `load_settings`.
        assert settings.bus_brokers == BUS_BROKERS_WITH_USERINFO
        service = build_service(settings, bus=InMemoryBus())
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


# --------------------------------------------------------------------------
# ADR-0013 decision 7: member id and lease TTL
# --------------------------------------------------------------------------


class TestMemberIdAndLeaseTtl:
    """ADR-0013 decision 7's two configuration keys."""

    def test_the_member_id_defaults_to_the_hostname(self) -> None:
        assert load_settings(_env()).member_id == socket.gethostname()

    def test_an_explicit_member_id_is_honoured(self) -> None:
        assert load_settings(_env(HAMMERTIME_AGGREGATOR_MEMBER_ID="agg-3")).member_id == "agg-3"

    def test_a_set_but_empty_member_id_is_a_value_error(self) -> None:
        # "set-but-empty is a `ValueError`": two members sharing an id share
        # its leases and are not told apart, and "" is the templating
        # accident ADR-0011 A3 describes.
        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_MEMBER_ID"):
            load_settings(_env(HAMMERTIME_AGGREGATOR_MEMBER_ID=""))

    def test_the_lease_ttl_defaults_to_thirty_seconds(self) -> None:
        assert load_settings(_env()).lease_ttl_s == 30

    def test_an_explicit_lease_ttl_is_honoured(self) -> None:
        assert load_settings(_env(HAMMERTIME_AGGREGATOR_LEASE_TTL_S="45")).lease_ttl_s == 45

    def test_the_default_lease_ttl_exceeds_the_default_maintenance_interval(self) -> None:
        # Regression guard: the documented defaults (30 against 1.0) must
        # pass `load_settings`'s own validation.
        settings = load_settings(_env())

        assert settings.lease_ttl_s > settings.maintenance_interval_s

    def test_a_lease_ttl_below_the_maintenance_interval_is_a_value_error(self) -> None:
        env = _env(
            HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S="10",
            HAMMERTIME_AGGREGATOR_LEASE_TTL_S="5",
        )

        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_LEASE_TTL_S"):
            load_settings(env)

    def test_a_lease_ttl_equal_to_the_maintenance_interval_is_a_value_error(self) -> None:
        # ASSUMPTION 3: "MUST exceed" is strict.
        env = _env(
            HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S="10",
            HAMMERTIME_AGGREGATOR_LEASE_TTL_S="10",
        )

        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_LEASE_TTL_S"):
            load_settings(env)

    def test_a_lease_ttl_just_above_the_maintenance_interval_is_accepted(self) -> None:
        env = _env(
            HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S="10",
            HAMMERTIME_AGGREGATOR_LEASE_TTL_S="10.5",
        )

        assert load_settings(env).lease_ttl_s == 10.5

    def test_the_default_lease_ttl_is_checked_against_a_raised_maintenance_interval(
        self,
    ) -> None:
        # The rule binds the *effective* pair, not only explicit values: an
        # operator who raises the interval past 30 s without raising the TTL
        # is refused.
        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_LEASE_TTL_S"):
            load_settings(_env(HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S="60"))

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_a_non_positive_lease_ttl_is_a_value_error(self, value: str) -> None:
        # "a positive number".
        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_LEASE_TTL_S"):
            load_settings(_env(HAMMERTIME_AGGREGATOR_LEASE_TTL_S=value))

    def test_a_non_numeric_lease_ttl_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_LEASE_TTL_S"):
            load_settings(_env(HAMMERTIME_AGGREGATOR_LEASE_TTL_S="thirty"))
