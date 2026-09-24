"""Trie settings: `load_settings(env)` and the frozen `TrieSettings`.

Spec: section 33 and section 35 (the trie holds one root per address family,
IPv4 first), section 47.1 step 3 (an invalid configuration is rejected before
anything opens, exit 2) and section 47.5.

Written from ADR-0017 decision 11 alone ("Settings"), which follows ADR-0009
decision 2's pattern:

* `env=None` means `os.environ` -- every test here passes an explicit mapping,
  so none touches the process environment;
* "a bad value is a `ValueError` naming the variable";
* "unknown variables are ignored";
* "when several values are bad, which one is reported is not pinned" -- so
  every test that expects one key's error supplies valid values (or none) for
  every other key.

The table of keys, defaults and rules is decision 11's. The
`HAMMERTIME_BUS_BROKERS` row is ADR-0013 Amendment 4 ruling S2 and Amendment 5
ruling 5 as ADR-0017 restates them: under `nats` every entry passes
`validate_bus_url`, a refusal names the variable "and nothing of the value";
under `memory` the value is not read. What the trie stores for
`bus_brokers` under `memory` is therefore not asserted -- only that the load
succeeds.

Slice 2's two keys are ADR-0017 Amendment 2 ruling 1 (and decision 11 as
noted there): `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` (field `min_prefix_length`,
default 8, an integer from 0 to 32) and `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6`
(field `min_prefix_length_ipv6`, default 104, from 0 to 128). "Each value is
read with Python's `int()` ..., then checked against its range, both ends
included. A value `int()` refuses, or one outside the range, is a `ValueError`
naming the variable." "Both keys are read and checked whatever
`HAMMERTIME_TRIE_FAMILIES` holds" (assumption 43). The IPv4 key's name is a
prefix of the IPv6 key's, so an IPv4 error is matched by a pattern that
refuses the `_IPV6` suffix.
"""

import dataclasses
from pathlib import Path

import pytest
from hammertime.core.addressing.address import AddressFamily
from hammertime.trie.config import TrieSettings, load_settings

IPV4 = AddressFamily.IPV4
IPV6 = AddressFamily.IPV6

BIND = "HAMMERTIME_TRIE_QUERY_BIND"
FAMILIES = "HAMMERTIME_TRIE_FAMILIES"
BUS_KIND = "HAMMERTIME_BUS_KIND"
BUS_BROKERS = "HAMMERTIME_BUS_BROKERS"
POLL = "HAMMERTIME_CONFIG_POLL_INTERVAL_S"
MIN_LENGTH = "HAMMERTIME_TRIE_MIN_PREFIX_LENGTH"
MIN_LENGTH_V6 = "HAMMERTIME_TRIE_MIN_PREFIX_LENGTH_IPV6"

# A message naming the IPv4 key itself, not only as the start of the IPv6 key.
NAMES_MIN_LENGTH = MIN_LENGTH + "(?!_IPV6)"


class TestDefaults:
    def test_an_empty_environment_gives_every_documented_default(self) -> None:
        settings = load_settings({})

        assert settings.host == "0.0.0.0"
        assert settings.port == 8081
        assert settings.detection_config_path == Path("./config/detection.v1.json")
        assert settings.bus_kind == "nats"
        assert settings.bus_brokers == "nats://localhost:4222"
        assert settings.families == frozenset({IPV4})
        assert settings.config_poll_interval_s == 1.0

    def test_the_config_path_is_stored_as_a_path(self) -> None:
        settings = load_settings({"HAMMERTIME_CONFIG_PATH": "/etc/hammertime/detection.json"})

        assert settings.detection_config_path == Path("/etc/hammertime/detection.json")


class TestQueryBind:
    def test_a_host_and_port_split_at_the_last_colon(self) -> None:
        settings = load_settings({BIND: "127.0.0.1:0"})

        assert settings.host == "127.0.0.1"
        assert settings.port == 0

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("8081", id="no-colon"),
            pytest.param(":8081", id="empty-host"),
            pytest.param("host:", id="empty-port"),
            pytest.param("host:abc", id="non-digit-port"),
        ],
    )
    def test_a_malformed_bind_is_a_value_error_naming_the_variable(self, value: str) -> None:
        with pytest.raises(ValueError, match=BIND):
            load_settings({BIND: value})


class TestFamilies:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("ipv4", frozenset({IPV4}), id="ipv4"),
            pytest.param("ipv6", frozenset({IPV6}), id="ipv6"),
            pytest.param("ipv4,ipv6", frozenset({IPV4, IPV6}), id="both"),
            pytest.param(" IPv6 , ipv4 ", frozenset({IPV4, IPV6}), id="stripped-and-case-folded"),
            pytest.param("ipv4,ipv4", frozenset({IPV4}), id="duplicates-collapse"),
        ],
    )
    def test_a_valid_list_gives_its_set(
        self, value: str, expected: frozenset[AddressFamily]
    ) -> None:
        assert load_settings({FAMILIES: value}).families == expected

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("", id="empty"),
            pytest.param("  ", id="whitespace-only"),
            pytest.param("ipv4,", id="trailing-empty-entry"),
            pytest.param("ipv4,,ipv6", id="inner-empty-entry"),
            pytest.param("ip4", id="unknown-ip4"),
            pytest.param("ipv5", id="unknown-ipv5"),
        ],
    )
    def test_a_malformed_list_is_a_value_error_naming_the_variable(self, value: str) -> None:
        with pytest.raises(ValueError, match=FAMILIES):
            load_settings({FAMILIES: value})


class TestBusKind:
    def test_memory_is_accepted(self) -> None:
        assert load_settings({BUS_KIND: "memory"}).bus_kind == "memory"

    def test_nats_is_accepted(self) -> None:
        assert load_settings({BUS_KIND: "nats"}).bus_kind == "nats"

    @pytest.mark.parametrize(
        "value", [pytest.param("kafka", id="kafka"), pytest.param("", id="empty")]
    )
    def test_anything_else_is_a_value_error_naming_the_variable(self, value: str) -> None:
        with pytest.raises(ValueError, match=BUS_KIND):
            load_settings({BUS_KIND: value})


class TestBusBrokers:
    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            pytest.param("nats://user:pa/ss@nats:4222", "pa/ss", id="slash-in-password"),
            pytest.param("nats://host:notaport", "notaport", id="non-numeric-port"),
        ],
    )
    def test_a_refused_entry_under_nats_names_the_variable_and_nothing_of_the_value(
        self, value: str, fragment: str
    ) -> None:
        with pytest.raises(ValueError, match=BUS_BROKERS) as excinfo:
            load_settings({BUS_KIND: "nats", BUS_BROKERS: value})

        assert fragment not in str(excinfo.value)

    def test_a_valid_comma_separated_list_is_stored_verbatim(self) -> None:
        value = "nats://nats-a:4222,nats://nats-b:4223"

        assert load_settings({BUS_KIND: "nats", BUS_BROKERS: value}).bus_brokers == value

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("nats://user:pa/ss@nats:4222", id="slash-in-password"),
            pytest.param("nats://host:notaport", id="non-numeric-port"),
        ],
    )
    def test_the_same_value_is_accepted_under_memory(self, value: str) -> None:
        settings = load_settings({BUS_KIND: "memory", BUS_BROKERS: value})

        assert settings.bus_kind == "memory"


class TestConfigPollInterval:
    def test_a_positive_number_is_accepted(self) -> None:
        assert load_settings({POLL: "0.5"}).config_poll_interval_s == 0.5

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("0", id="zero"),
            pytest.param("-1", id="negative"),
            pytest.param("abc", id="not-a-number"),
        ],
    )
    def test_anything_else_is_a_value_error(self, value: str) -> None:
        with pytest.raises(ValueError, match=POLL):
            load_settings({POLL: value})


class TestKeysTheTrieDoesNotRead:
    def test_store_keys_are_not_read(self) -> None:
        # Decision 11: "`HAMMERTIME_STORE_KIND` and `HAMMERTIME_REDIS_URL`:
        # the trie has no store" -- a URL no redis client would accept loads.
        settings = load_settings(
            {"HAMMERTIME_STORE_KIND": "redis", "HAMMERTIME_REDIS_URL": "http://x"}
        )

        assert settings.bus_kind == "nats"

    def test_unknown_keys_are_ignored(self) -> None:
        settings = load_settings(
            {"HAMMERTIME_NOT_A_REAL_KEY": "anything", "UNRELATED": "value", BIND: "127.0.0.1:9"}
        )

        assert settings.port == 9


class TestTrieSettings:
    def test_it_is_frozen(self) -> None:
        settings = TrieSettings(
            host="127.0.0.1",
            port=0,
            detection_config_path=Path("detection.json"),
            bus_kind="memory",
            bus_brokers="nats://localhost:4222",
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            settings.port = 1  # type: ignore[misc]

    def test_the_two_defaulted_fields(self) -> None:
        settings = TrieSettings(
            host="127.0.0.1",
            port=0,
            detection_config_path=Path("detection.json"),
            bus_kind="memory",
            bus_brokers="nats://localhost:4222",
        )

        assert settings.families == frozenset({IPV4})
        assert settings.config_poll_interval_s == 1.0

    def test_the_prefix_length_fields_default_to_8_and_104(self) -> None:
        # ADR-0017 Amendment 2 ruling 1: "`TrieSettings` gains two fields
        # after `config_poll_interval_s`: `min_prefix_length: int = 8` and
        # `min_prefix_length_ipv6: int = 104`."
        settings = TrieSettings(
            host="127.0.0.1",
            port=0,
            detection_config_path=Path("detection.json"),
            bus_kind="memory",
            bus_brokers="nats://localhost:4222",
        )

        assert settings.min_prefix_length == 8
        assert settings.min_prefix_length_ipv6 == 104


class TestMinPrefixLengths:
    """ADR-0017 Amendment 2 ruling 1."""

    def test_an_empty_environment_gives_8_and_104(self) -> None:
        settings = load_settings({})

        assert settings.min_prefix_length == 8
        assert settings.min_prefix_length_ipv6 == 104

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("0", 0, id="0"),
            pytest.param("8", 8, id="8"),
            pytest.param("32", 32, id="32"),
        ],
    )
    def test_ipv4_accepts_0_to_32(self, value: str, expected: int) -> None:
        settings = load_settings({MIN_LENGTH: value})

        assert settings.min_prefix_length == expected
        assert settings.min_prefix_length_ipv6 == 104

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("0", 0, id="0"),
            pytest.param("104", 104, id="104"),
            pytest.param("128", 128, id="128"),
        ],
    )
    def test_ipv6_accepts_0_to_128(self, value: str, expected: int) -> None:
        settings = load_settings({MIN_LENGTH_V6: value})

        assert settings.min_prefix_length_ipv6 == expected
        assert settings.min_prefix_length == 8

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("-1", id="minus-1"),
            pytest.param("33", id="33"),
            pytest.param("eight", id="not-a-number"),
            pytest.param("8.5", id="fractional"),
            pytest.param("", id="empty"),
        ],
    )
    def test_ipv4_refuses_anything_else_naming_the_variable(self, value: str) -> None:
        with pytest.raises(ValueError, match=NAMES_MIN_LENGTH):
            load_settings({MIN_LENGTH: value})

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("-1", id="minus-1"),
            pytest.param("129", id="129"),
            pytest.param("eight", id="not-a-number"),
            pytest.param("8.5", id="fractional"),
            pytest.param("", id="empty"),
        ],
    )
    def test_ipv6_refuses_anything_else_naming_the_variable(self, value: str) -> None:
        with pytest.raises(ValueError, match=MIN_LENGTH_V6):
            load_settings({MIN_LENGTH_V6: value})

    def test_the_ipv6_key_is_checked_on_an_ipv4_only_trie(self) -> None:
        # Ruling 1: "Both keys are read and checked whatever
        # `HAMMERTIME_TRIE_FAMILIES` holds"; assumption 43.
        with pytest.raises(ValueError, match=MIN_LENGTH_V6):
            load_settings({FAMILIES: "ipv4", MIN_LENGTH_V6: "129"})
