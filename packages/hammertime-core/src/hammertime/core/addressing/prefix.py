"""CIDR prefixes, capacity, and hot ratio.

Spec: section 3 (prefix, hot ratio), section 13 (classification inputs).

capacity() returns a Python int on purpose: an IPv6 /0 capacity is 2**128 and
must never be squeezed into a 64-bit type (spec section 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.errors import InvalidPrefixError


@dataclass(frozen=True, slots=True)
class Prefix:
    family: AddressFamily
    network: int
    length: int

    def __post_init__(self) -> None:
        bits = self.family.bit_length
        if not 0 <= self.length <= bits:
            raise InvalidPrefixError(f"prefix length {self.length} invalid for {self.family}")
        host_bits = bits - self.length
        if host_bits and (self.network & ((1 << host_bits) - 1)):
            raise InvalidPrefixError("host bits set in network address")

    @classmethod
    def parse(cls, text: str) -> Prefix:
        addr_text, _, length_text = text.partition("/")
        addr = Address.parse(addr_text)
        try:
            length = int(length_text) if length_text else addr.bit_length
        except ValueError as exc:
            raise InvalidPrefixError(text) from exc
        return cls(family=addr.family, network=addr.value, length=length)

    def capacity(self) -> int:
        """Number of /32 (or /128) addresses contained in this prefix."""
        return 1 << (self.family.bit_length - self.length)

    def hot_ratio(self, hot_count: int) -> float:
        """hot_count / capacity, computed exactly then narrowed once."""
        return float(Fraction(hot_count, self.capacity()))

    def contains(self, addr: Address) -> bool:
        if addr.family is not self.family:
            return False
        shift = self.family.bit_length - self.length
        return (addr.value >> shift) == (self.network >> shift)

    def __str__(self) -> str:
        return f"{Address(self.family, self.network)}/{self.length}"
