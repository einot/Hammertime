"""common_prefix_length and bit_at (spec section 8, section 27)."""

from __future__ import annotations

import pytest
from hammertime.core.addressing.bits import bit_at, common_prefix_length


class TestCommonPrefixLength:
    def test_identical_addresses_share_the_whole_width(self) -> None:
        assert common_prefix_length(0xC0A80001, 0xC0A80001, 32) == 32

    def test_completely_different_addresses_share_nothing(self) -> None:
        assert common_prefix_length(0x00000000, 0xFFFFFFFF, 32) == 0

    def test_differ_only_in_the_last_bit(self) -> None:
        # 10.0.0.0 vs 10.0.0.1
        assert common_prefix_length(0x0A000000, 0x0A000001, 32) == 31

    def test_differ_in_the_first_bit(self) -> None:
        # 0.0.0.0 vs 128.0.0.0
        assert common_prefix_length(0x00000000, 0x80000000, 32) == 0

    def test_shared_16_bit_prefix(self) -> None:
        # 192.168.1.0 vs 192.168.2.0 share 192.168.0.0/22, not /24
        assert common_prefix_length(0xC0A80100, 0xC0A80200, 32) == 22

    def test_ignores_bits_outside_bit_length(self) -> None:
        # High bits set beyond bit_length must not affect the result.
        a = 0xAAAA0001
        b = 0x55550001 | (1 << 40)
        assert common_prefix_length(a, b, 32) == common_prefix_length(a, b & 0xFFFFFFFF, 32)


class TestBitAt:
    def test_most_significant_bit_first(self) -> None:
        value = 0b1000_0000_0000_0000_0000_0000_0000_0000  # 128.0.0.0
        assert bit_at(value, 0, 32) == 1
        assert bit_at(value, 1, 32) == 0

    def test_least_significant_bit_is_last_index(self) -> None:
        value = 0b0000_0000_0000_0000_0000_0000_0000_0001
        assert bit_at(value, 31, 32) == 1
        assert bit_at(value, 30, 32) == 0

    def test_matches_manual_walk(self) -> None:
        value = 0xC0A80101  # 192.168.1.1
        walked = [bit_at(value, i, 32) for i in range(32)]
        expected = [(value >> shift) & 1 for shift in range(31, -1, -1)]
        assert walked == expected

    @pytest.mark.parametrize("bit_length", [32, 128])
    def test_all_zero_address_has_no_set_bits(self, bit_length: int) -> None:
        assert all(bit_at(0, i, bit_length) == 0 for i in range(bit_length))
