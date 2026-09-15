"""Strict address parsing; malformed addresses fail the whole message.

Spec: section 36
"""

from hammertime.core.addressing.address import Address

__all__ = ["parse_address"]


def parse_address(text: str) -> Address:
    """Parse a wire `ip` field into an `Address`.

    Delegates to `Address.parse`, the same address-parsing entry point
    `hammertime.core.events.codec` uses, so malformed input raises the same
    `InvalidAddressError` (spec section 36) rather than a locally-invented
    error type. A single malformed IP fails the whole message -- callers
    must not catch `InvalidAddressError` here and silently drop the entry.
    """
    return Address.parse(text)
