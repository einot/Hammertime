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

Two rules from ADR-0011 section 9 run through the whole file. First, only a
*missing* key takes its default (`env.get(key, default)`): an empty or
whitespace-only value is a value, and a malformed one, for every key --
including the three free-text keys that are otherwise passed through
verbatim. Second, every rejection raises a `ValueError` whose message names
the offending variable, which is what makes section 47.1's "refuse to start"
actionable, so every `pytest.raises` below matches on the key.
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

# The keys whose value is carried through as text with no further validation
# (ADR-0011 section 9). They still reject an empty value: an empty broker
# list, Redis URL or detection-config path is never a working configuration.
FREE_TEXT_KEYS = [
    "HAMMERTIME_CONFIG_PATH",
    "HAMMERTIME_BUS_BROKERS",
    "HAMMERTIME_REDIS_URL",
]

# Every value that is present but carries no information. ADR-0011 section 9:
# these are malformed, never "unset".
EMPTY_VALUES = ["", "   "]


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


class TestShardIdsDefaultIsDerived:
    """ADR-0011 section 9: the default is `tuple(range(shard_count))` of the
    *parsed* `shard_count`, never the literal `0-63` `.env.example` prints.

    A literal default breaks the moment `HAMMERTIME_SHARD_COUNT` is lowered:
    the loader would reject its own default for holding ids outside
    `[0, shard_count)`. Deriving it means a lowered ring still owns every
    shard in it.
    """

    def test_a_lowered_shard_count_narrows_the_default(self) -> None:
        settings = load_settings({"HAMMERTIME_SHARD_COUNT": "4"})

        assert settings.shard_count == 4
        assert settings.shard_ids == (0, 1, 2, 3)

    def test_a_raised_shard_count_widens_the_default(self) -> None:
        settings = load_settings({"HAMMERTIME_SHARD_COUNT": "128"})

        assert settings.shard_count == 128
        assert settings.shard_ids == tuple(range(128))

    def test_a_single_shard_owns_only_shard_zero(self) -> None:
        settings = load_settings({"HAMMERTIME_SHARD_COUNT": "1"})

        assert settings.shard_ids == (0,)

    @pytest.mark.parametrize("shard_count", ["1", "4", "64", "128"])
    def test_the_derived_default_is_always_in_range(self, shard_count: str) -> None:
        # The property the derivation exists to guarantee: the default set is
        # non-empty and every id in it satisfies 0 <= id < shard_count, so it
        # would survive the same validation an explicit value gets.
        settings = load_settings({"HAMMERTIME_SHARD_COUNT": shard_count})

        assert settings.shard_ids
        assert all(0 <= shard_id < settings.shard_count for shard_id in settings.shard_ids)
        assert settings.shard_ids == tuple(sorted(set(settings.shard_ids)))

    def test_an_explicit_value_still_wins_over_the_derived_default(self) -> None:
        settings = load_settings({"HAMMERTIME_SHARD_COUNT": "4", "HAMMERTIME_SHARD_IDS": "1-2"})

        assert settings.shard_ids == (1, 2)


class TestFreeTextKeys:
    """ADR-0011 section 9: passed through verbatim, but never empty."""

    @pytest.mark.parametrize("key", FREE_TEXT_KEYS)
    @pytest.mark.parametrize("value", EMPTY_VALUES)
    def test_an_empty_value_is_rejected(self, key: str, value: str) -> None:
        # This is where the aggregator diverges from
        # `services/ingest/config.py`, which passes its free-text keys
        # through unchecked: section 47.1 asks for an invalid configuration
        # to be rejected at load, and none of these three is usable empty.
        with pytest.raises(ValueError, match=key):
            load_settings({key: value})

    @pytest.mark.parametrize(
        ("key", "field", "expected"),
        [
            ("HAMMERTIME_CONFIG_PATH", "detection_config_path", Path("config/detection.v1.json")),
            ("HAMMERTIME_BUS_BROKERS", "bus_brokers", "localhost:19092"),
            ("HAMMERTIME_REDIS_URL", "redis_url", "redis://localhost:6379/0"),
        ],
    )
    def test_a_missing_key_still_takes_its_default(
        self, key: str, field: str, expected: object
    ) -> None:
        # The contrast that gives "empty is malformed" its meaning: absence is
        # the only thing that selects a default. The other two free-text keys
        # are supplied, so only the one under test is missing.
        env = {other: "supplied" for other in FREE_TEXT_KEYS if other != key}

        assert getattr(load_settings(env), field) == expected

    def test_a_value_is_not_trimmed_or_rewritten(self) -> None:
        env = {
            "HAMMERTIME_CONFIG_PATH": "/etc/hammertime/detection.json",
            "HAMMERTIME_BUS_BROKERS": "broker-1:9092,broker-2:9092",
            "HAMMERTIME_REDIS_URL": "redis://cache:6380/3",
        }

        settings = load_settings(env)

        assert settings.detection_config_path == Path(env["HAMMERTIME_CONFIG_PATH"])
        assert settings.bus_brokers == env["HAMMERTIME_BUS_BROKERS"]
        assert settings.redis_url == env["HAMMERTIME_REDIS_URL"]


class TestBindParsing:
    """ADR-0011 section 9: `HAMMERTIME_AGGREGATOR_BIND` splits on the LAST
    ":"; the host must be non-empty and is not otherwise validated; the port
    must be a decimal integer in `[0, 65535]`.

    Port `0` is accepted (integration-scenarios.md binds every service to
    `127.0.0.1:0`); that case is asserted by
    `TestExplicitValues::test_bind_accepts_an_ephemeral_port` above and is not
    repeated here.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "8083",  # no colon at all
            ":8083",  # empty host
            "host:",  # empty port
            "host:abc",  # non-decimal port
            "host:65536",  # one past the largest port there is
        ],
    )
    def test_a_malformed_bind_is_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_BIND"):
            load_settings({"HAMMERTIME_AGGREGATOR_BIND": value})

    @pytest.mark.parametrize("value", EMPTY_VALUES)
    def test_an_empty_bind_is_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_BIND"):
            load_settings({"HAMMERTIME_AGGREGATOR_BIND": value})

    @pytest.mark.parametrize("value", ["host:-1", "host: 8083", "host:8083 ", "host:0x1f"])
    def test_a_port_that_is_not_plain_digits_is_rejected(self, value: str) -> None:
        # "a decimal integer": a sign, surrounding space or a radix prefix is
        # not one, even where int() would accept it.
        with pytest.raises(ValueError, match="HAMMERTIME_AGGREGATOR_BIND"):
            load_settings({"HAMMERTIME_AGGREGATOR_BIND": value})

    def test_a_bracketed_ipv6_literal_passes_through_as_is(self) -> None:
        # Splitting on the *last* colon keeps the address together, and the
        # host is not validated any further.
        settings = load_settings({"HAMMERTIME_AGGREGATOR_BIND": "[::1]:8083"})

        assert settings.host == "[::1]"
        assert settings.port == 8083

    def test_the_highest_port_is_accepted(self) -> None:
        settings = load_settings({"HAMMERTIME_AGGREGATOR_BIND": "127.0.0.1:65535"})

        assert settings.host == "127.0.0.1"
        assert settings.port == 65535

    def test_a_hostname_is_accepted_unvalidated(self) -> None:
        settings = load_settings({"HAMMERTIME_AGGREGATOR_BIND": "aggregator.internal:8083"})

        assert settings.host == "aggregator.internal"
        assert settings.port == 8083


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

    @pytest.mark.parametrize("key", POSITIVE_NUMERIC_KEYS)
    @pytest.mark.parametrize("value", EMPTY_VALUES)
    def test_an_empty_value_is_malformed_never_unset(self, key: str, value: str) -> None:
        # ADR-0011 section 9: only a *missing* key takes its default. An
        # exported-but-empty variable -- the usual shape of a broken
        # deployment template -- is a value, and not a number.
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

    @pytest.mark.parametrize("kind", ["other", "", "   ", "KAFKA", "rabbitmq"])
    def test_unknown_bus_kind_is_rejected(self, kind: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_BUS_KIND"):
            load_settings({"HAMMERTIME_BUS_KIND": kind})

    @pytest.mark.parametrize("kind", ["other", "", "   ", "REDIS", "postgres"])
    def test_unknown_store_kind_is_rejected(self, kind: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_STORE_KIND"):
            load_settings({"HAMMERTIME_STORE_KIND": kind})
