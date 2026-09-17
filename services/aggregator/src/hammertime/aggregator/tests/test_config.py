"""Aggregator settings: the environment keys, their defaults, and rejection.

Spec: section 20 (shard ownership -- `HAMMERTIME_SHARD_COUNT` /
`HAMMERTIME_SHARD_IDS`), section 47.1 ("read configuration only from the
environment ... reject an invalid configuration before opening any network
connection"), section 47.3 (`HAMMERTIME_CONFIG_POLL_INTERVAL_S`), section 26
(`HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS`).

The field names, keys and defaults asserted here are the table in ADR-0011
section 9 (`hammertime.aggregator.config`), which folds in ADR-0009
decision 2's lifecycle keys. Nothing in M3 acts on the shard keys; they are
parsed because they are settings (ADR-0011 decision 8).

Every test passes an explicit `env=` mapping -- the same convention as
`services/ingest/.../tests/test_config.py` -- so the suite never depends on
the process environment, and one test below pins that independence down.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from hammertime.aggregator.config import load_settings

# Every key whose value must be a positive number (ADR-0011 section 9's
# "> 0" column). Floats and integers alike: both reject non-numeric text and
# both reject zero or less, with a ValueError that names the variable so an
# operator can see which one they got wrong (section 47.1: the process must
# refuse to start, and exit code 2 is only useful if the log says why).
POSITIVE_NUMERIC_KEYS = [
    "HAMMERTIME_SHARD_COUNT",
    "HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S",
    "HAMMERTIME_CONFIG_POLL_INTERVAL_S",
    "HAMMERTIME_STARTUP_TIMEOUT_S",
    "HAMMERTIME_SHUTDOWN_TIMEOUT_S",
    "HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS",
    "HAMMERTIME_AGGREGATOR_COMMIT_EVERY",
]


class TestDefaults:
    """ADR-0011 section 9's default column, with `env={}`."""

    def test_bus_and_store_defaults(self) -> None:
        settings = load_settings({})

        assert settings.bus_kind == "kafka"
        assert settings.bus_brokers == "localhost:19092"
        assert settings.store_kind == "redis"
        assert settings.redis_url == "redis://localhost:6379/0"

    def test_detection_config_path_default(self) -> None:
        settings = load_settings({})

        assert settings.detection_config_path == Path("config/detection.v1.json")

    def test_bind_default_is_the_aggregator_port(self) -> None:
        # ADR-0009 / docs/protocol/read-api-v1.md: ingest 8080, trie 8081,
        # detector 8082, aggregator 8083.
        settings = load_settings({})

        assert settings.host == "0.0.0.0"
        assert settings.port == 8083

    def test_shard_defaults_cover_the_whole_ring(self) -> None:
        settings = load_settings({})

        assert settings.shard_count == 64
        assert settings.shard_ids == tuple(range(64))

    def test_lifecycle_interval_defaults(self) -> None:
        settings = load_settings({})

        assert settings.maintenance_interval_s == 1.0
        assert settings.config_poll_interval_s == 1.0
        assert settings.startup_timeout_s == 60
        assert settings.shutdown_timeout_s == 8

    def test_window_store_and_commit_defaults(self) -> None:
        settings = load_settings({})

        assert settings.max_tracked_ips == 1_000_000
        assert settings.commit_every == 100

    def test_settings_are_frozen(self) -> None:
        # ADR-0011 section 9 declares AggregatorSettings frozen: the loaded
        # document is what gets logged at startup and must not drift.
        settings = load_settings({})
        field = "shard_count"

        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(settings, field, 1)


class TestExplicitValues:
    def test_every_key_is_honoured(self) -> None:
        env = {
            "HAMMERTIME_CONFIG_PATH": "/etc/hammertime/detection.json",
            "HAMMERTIME_BUS_KIND": "memory",
            "HAMMERTIME_BUS_BROKERS": "broker-1:9092,broker-2:9092",
            "HAMMERTIME_STORE_KIND": "memory",
            "HAMMERTIME_REDIS_URL": "redis://cache:6380/3",
            "HAMMERTIME_SHARD_COUNT": "8",
            "HAMMERTIME_SHARD_IDS": "2,3",
            "HAMMERTIME_AGGREGATOR_BIND": "127.0.0.1:9083",
            "HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S": "0.25",
            "HAMMERTIME_CONFIG_POLL_INTERVAL_S": "5",
            "HAMMERTIME_STARTUP_TIMEOUT_S": "30",
            "HAMMERTIME_SHUTDOWN_TIMEOUT_S": "2.5",
            "HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS": "50000",
            "HAMMERTIME_AGGREGATOR_COMMIT_EVERY": "1",
        }

        settings = load_settings(env)

        assert settings.detection_config_path == Path("/etc/hammertime/detection.json")
        assert settings.bus_kind == "memory"
        assert settings.bus_brokers == "broker-1:9092,broker-2:9092"
        assert settings.store_kind == "memory"
        assert settings.redis_url == "redis://cache:6380/3"
        assert settings.shard_count == 8
        assert settings.shard_ids == (2, 3)
        assert settings.host == "127.0.0.1"
        assert settings.port == 9083
        assert settings.maintenance_interval_s == 0.25
        assert settings.config_poll_interval_s == 5
        assert settings.startup_timeout_s == 30
        assert settings.shutdown_timeout_s == 2.5
        assert settings.max_tracked_ips == 50_000
        assert settings.commit_every == 1

    def test_bind_accepts_an_ephemeral_port(self) -> None:
        # docs/spec/integration-scenarios.md binds every service to
        # 127.0.0.1:0, so port 0 must survive the "> 0" validation that
        # applies to the timing keys.
        settings = load_settings({"HAMMERTIME_AGGREGATOR_BIND": "127.0.0.1:0"})

        assert settings.host == "127.0.0.1"
        assert settings.port == 0

    def test_unknown_keys_are_ignored(self) -> None:
        env = {
            "HAMMERTIME_TOTALLY_MADE_UP": "1",
            "PATH": "/usr/bin",
            "HAMMERTIME_TRIE_SNAPSHOT_DIR": "./snapshots",
        }

        assert load_settings(env) == load_settings({})

    def test_the_process_environment_is_not_consulted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # load_settings(env) reads the mapping it was given and nothing else,
        # which is what makes the rest of this file deterministic.
        monkeypatch.setenv("HAMMERTIME_SHARD_COUNT", "8")
        monkeypatch.setenv("HAMMERTIME_AGGREGATOR_COMMIT_EVERY", "7")

        settings = load_settings({})

        assert settings.shard_count == 64
        assert settings.commit_every == 100


class TestShardIds:
    """ADR-0011 section 9: "a-b" ranges and commas, sorted, deduplicated,
    non-empty, each in [0, shard_count)."""

    def test_a_range_and_a_single_id(self) -> None:
        settings = load_settings({"HAMMERTIME_SHARD_IDS": "0-3,7"})

        assert settings.shard_ids == (0, 1, 2, 3, 7)

    def test_duplicates_are_removed_and_the_result_is_sorted(self) -> None:
        settings = load_settings({"HAMMERTIME_SHARD_IDS": "5,5,1"})

        assert settings.shard_ids == (1, 5)

    def test_overlapping_ranges_are_merged(self) -> None:
        settings = load_settings({"HAMMERTIME_SHARD_IDS": "4-6,5-7"})

        assert settings.shard_ids == (4, 5, 6, 7)

    def test_a_single_id_range_is_that_id(self) -> None:
        # "a-b" is rejected only when it is reversed, so a == b is legal.
        settings = load_settings({"HAMMERTIME_SHARD_IDS": "3-3"})

        assert settings.shard_ids == (3,)

    def test_the_result_is_a_tuple(self) -> None:
        settings = load_settings({"HAMMERTIME_SHARD_IDS": "1"})

        assert settings.shard_ids == (1,)
        assert isinstance(settings.shard_ids, tuple)

    @pytest.mark.parametrize("value", ["", "   ", ","])
    def test_an_empty_set_of_shards_is_rejected(self, value: str) -> None:
        # A shard that owns nothing has nothing to consume (section 20).
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS"):
            load_settings({"HAMMERTIME_SHARD_IDS": value})

    @pytest.mark.parametrize("value", ["abc", "1,x", "1-", "-", "1-two", "1..3"])
    def test_non_numeric_shard_ids_are_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS"):
            load_settings({"HAMMERTIME_SHARD_IDS": value})

    def test_an_id_at_shard_count_is_rejected(self) -> None:
        # The range is half-open: [0, shard_count).
        env = {"HAMMERTIME_SHARD_COUNT": "4", "HAMMERTIME_SHARD_IDS": "4"}

        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS"):
            load_settings(env)

    def test_a_range_that_runs_past_shard_count_is_rejected(self) -> None:
        env = {"HAMMERTIME_SHARD_COUNT": "4", "HAMMERTIME_SHARD_IDS": "0-4"}

        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS"):
            load_settings(env)

    def test_the_highest_valid_id_is_accepted(self) -> None:
        env = {"HAMMERTIME_SHARD_COUNT": "4", "HAMMERTIME_SHARD_IDS": "0-3"}

        assert load_settings(env).shard_ids == (0, 1, 2, 3)

    def test_a_reversed_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS"):
            load_settings({"HAMMERTIME_SHARD_IDS": "7-3"})


class TestNumericValidation:
    """Section 47.1: an invalid configuration is rejected before any network
    connection is opened, and the message names the offending variable."""

    @pytest.mark.parametrize("key", POSITIVE_NUMERIC_KEYS)
    def test_non_numeric_value_is_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match=key):
            load_settings({key: "not-a-number"})

    @pytest.mark.parametrize("key", POSITIVE_NUMERIC_KEYS)
    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_non_positive_value_is_rejected(self, key: str, value: str) -> None:
        with pytest.raises(ValueError, match=key):
            load_settings({key: value})

    def test_fractional_intervals_are_accepted(self) -> None:
        env = {
            "HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S": "0.1",
            "HAMMERTIME_CONFIG_POLL_INTERVAL_S": "0.5",
            "HAMMERTIME_STARTUP_TIMEOUT_S": "90.5",
            "HAMMERTIME_SHUTDOWN_TIMEOUT_S": "0.25",
        }

        settings = load_settings(env)

        assert settings.maintenance_interval_s == 0.1
        assert settings.config_poll_interval_s == 0.5
        assert settings.startup_timeout_s == 90.5
        assert settings.shutdown_timeout_s == 0.25


class TestKindValidation:
    """`bus_kind` and `store_kind` are closed sets (ADR-0011 section 9)."""

    @pytest.mark.parametrize("kind", ["kafka", "memory"])
    def test_known_bus_kinds_are_accepted(self, kind: str) -> None:
        assert load_settings({"HAMMERTIME_BUS_KIND": kind}).bus_kind == kind

    @pytest.mark.parametrize("kind", ["redis", "memory"])
    def test_known_store_kinds_are_accepted(self, kind: str) -> None:
        assert load_settings({"HAMMERTIME_STORE_KIND": kind}).store_kind == kind

    @pytest.mark.parametrize("kind", ["other", "", "KAFKA", "rabbitmq"])
    def test_unknown_bus_kind_is_rejected(self, kind: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_BUS_KIND"):
            load_settings({"HAMMERTIME_BUS_KIND": kind})

    @pytest.mark.parametrize("kind", ["other", "", "REDIS", "postgres"])
    def test_unknown_store_kind_is_rejected(self, kind: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_STORE_KIND"):
            load_settings({"HAMMERTIME_STORE_KIND": kind})
