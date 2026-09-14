"""Fixed-width binary address vectors.

Spec: section 3 (IP address), section 35 (IPv6 readiness).

The trie walks bits, not dotted quads, so an Address is an integer plus a
declared bit_length. IPv4 and IPv6 use the same type and separate trie roots.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import Enum
from typing import Iterator

from hammertime.core.errors import InvalidAddressError


class AddressFamily(str, Enum):
    IPV4 = "ipv4"
    IPV6 = "ipv6"

    @property
    def bit_length(self) -> int:
        return 32 if self is AddressFamily.IPV4 else 128


@dataclass(frozen=True, slots=True)
class Address:
    family: AddressFamily
    value: int

    def __post_init__(self) -> None:
        if not 0 <= self.value < (1 << self.family.bit_length):
            raise InvalidAddressError(f"value out of range for {self.family}")

    @property
    def bit_length(self) -> int:
        return self.family.bit_length

    @classmethod
    def parse(cls, text: str) -> Address:
        try:
            addr = ipaddress.ip_address(text)
        except ValueError as exc:
            raise InvalidAddressError(text) from exc
        family = AddressFamily.IPV4 if addr.version == 4 else AddressFamily.IPV6
        return cls(family=family, value=int(addr))

    def bits(self) -> Iterator[int]:
        """Yield bits most-significant first; this is the trie traversal order."""
        for shift in range(self.bit_length - 1, -1, -1):
            yield (self.value >> shift) & 1

    def __str__(self) -> str:
        version = 4 if self.family is AddressFamily.IPV4 else 6
        return str(ipaddress.ip_address(self.value) if version == 4 else ipaddress.IPv6Address(self.value))
