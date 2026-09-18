"""Aggregator settings: `HAMMERTIME_REDIS_URL` is validated before anything opens.

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

This is the aggregator's first `load_settings` test file. It follows the
convention of `services/ingest/.../tests/test_config.py`: every test passes
an explicit `env` mapping, so no test touches `os.environ`. The
`parse_shard_ids` tests (ADR-0011 Amendment 1 item A3) stay where they are,
in `test_sharding.py`; nothing was moved here.

`TestRedisUrlValidation` mirrors the ingest file's class of the same name
deliberately: A12 states one rule for both services, and two copies that can
be diffed against each other are the point.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from hammertime.aggregator.config import load_settings
from hammertime.aggregator.service import build_service
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
        # A12 item 2: set-but-empty is an error, not the default -- the same
        # rule ADR-0011 A3 applies to `HAMMERTIME_SHARD_IDS`.
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
