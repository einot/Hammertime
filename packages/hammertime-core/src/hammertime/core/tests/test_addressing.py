"""Address and Prefix (spec section 3, section 35)."""

from __future__ import annotations

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.core.errors import InvalidAddressError, InvalidPrefixError


class TestAddressFamily:
    def test_ipv4_bit_length(self) -> None:
        assert AddressFamily.IPV4.bit_length == 32

    def test_ipv6_bit_length(self) -> None:
        assert AddressFamily.IPV6.bit_length == 128


class TestAddress:
    def test_parse_ipv4(self) -> None:
        addr = Address.parse("192.168.1.1")
        assert addr.family is AddressFamily.IPV4
        assert addr.value == 0xC0A80101
        assert addr.bit_length == 32

    def test_parse_ipv6(self) -> None:
        addr = Address.parse("::1")
        assert addr.family is AddressFamily.IPV6
        assert addr.value == 1
        assert addr.bit_length == 128

    def test_parse_rejects_garbage(self) -> None:
        with pytest.raises(InvalidAddressError):
            Address.parse("not-an-ip")

    def test_construction_rejects_out_of_range_value(self) -> None:
        with pytest.raises(InvalidAddressError):
            Address(family=AddressFamily.IPV4, value=1 << 32)

    def test_construction_rejects_negative_value(self) -> None:
        with pytest.raises(InvalidAddressError):
            Address(family=AddressFamily.IPV4, value=-1)

    def test_str_round_trips_ipv4(self) -> None:
        assert str(Address.parse("10.20.30.40")) == "10.20.30.40"

    def test_str_round_trips_ipv6(self) -> None:
        assert str(Address.parse("2001:db8::1")) == "2001:db8::1"

    def test_bits_most_significant_first(self) -> None:
        # 128.0.0.1: MSB set, then 30 zero bits, then LSB set.
        addr = Address.parse("128.0.0.1")
        bits = list(addr.bits())
        assert len(bits) == 32
        assert bits[0] == 1
        assert bits[-1] == 1
        assert bits[1:-1] == [0] * 30

    def test_bits_length_matches_family(self) -> None:
        assert len(list(Address.parse("::1").bits())) == 128


class TestPrefix:
    def test_parse_with_explicit_length(self) -> None:
        p = Prefix.parse("10.0.0.0/8")
        assert p.family is AddressFamily.IPV4
        assert p.length == 8
        assert p.network == 0x0A000000

    def test_parse_without_length_defaults_to_host_route(self) -> None:
        p = Prefix.parse("10.0.0.1")
        assert p.length == 32

    def test_rejects_host_bits_set(self) -> None:
        with pytest.raises(InvalidPrefixError):
            Prefix.parse("10.0.0.1/8")

    def test_rejects_length_out_of_range(self) -> None:
        with pytest.raises(InvalidPrefixError):
            Prefix(family=AddressFamily.IPV4, network=0, length=33)

    def test_rejects_negative_length(self) -> None:
        with pytest.raises(InvalidPrefixError):
            Prefix(family=AddressFamily.IPV4, network=0, length=-1)

    def test_zero_length_prefix_is_the_whole_address_space(self) -> None:
        p = Prefix(family=AddressFamily.IPV4, network=0, length=0)
        assert p.capacity() == 2**32

    def test_capacity_ipv4(self) -> None:
        assert Prefix.parse("10.0.0.0/24").capacity() == 256

    def test_capacity_ipv6_is_a_python_int_not_narrowed(self) -> None:
        p = Prefix.parse("::/0")
        assert p.capacity() == 2**128
        assert isinstance(p.capacity(), int)

    def test_hot_ratio(self) -> None:
        p = Prefix.parse("10.0.0.0/24")
        assert p.hot_ratio(64) == pytest.approx(0.25)

    def test_hot_ratio_exact_fraction_for_large_prefixes(self) -> None:
        # 1 hot IP in a /8 should not underflow to 0.0 via naive float division.
        p = Prefix.parse("10.0.0.0/8")
        assert p.hot_ratio(1) > 0.0

    def test_contains_address_inside_prefix(self) -> None:
        p = Prefix.parse("10.0.0.0/24")
        assert p.contains(Address.parse("10.0.0.42"))

    def test_contains_address_outside_prefix(self) -> None:
        p = Prefix.parse("10.0.0.0/24")
        assert not p.contains(Address.parse("10.0.1.42"))

    def test_contains_rejects_mismatched_family(self) -> None:
        p = Prefix.parse("10.0.0.0/24")
        assert not p.contains(Address.parse("::1"))

    def test_str_round_trips(self) -> None:
        assert str(Prefix.parse("192.168.0.0/16")) == "192.168.0.0/16"

    def test_frozen_and_hashable(self) -> None:
        p1 = Prefix.parse("10.0.0.0/24")
        p2 = Prefix.parse("10.0.0.0/24")
        assert p1 == p2
        assert hash(p1) == hash(p2)
        with pytest.raises(AttributeError):
            p1.length = 16  # type: ignore[misc]
