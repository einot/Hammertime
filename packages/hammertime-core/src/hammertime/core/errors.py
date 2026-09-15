"""Domain errors shared across services."""

from __future__ import annotations


class HammertimeError(Exception):
    """Base class for all Hammertime domain errors."""


class InvalidAddressError(HammertimeError):
    """The supplied string is not a valid address for the declared family."""


class InvalidPrefixError(HammertimeError):
    """Prefix length is outside [0, bit_length] or host bits are set."""


class ConfigurationError(HammertimeError):
    """Detection configuration violates a spec invariant (e.g. cold >= hot)."""


class LateEventError(HammertimeError):
    """Observation fell outside the allowed lateness horizon (spec section 24)."""


class InvariantViolation(HammertimeError):
    """A structural invariant was broken, e.g. hot_count < 0 (spec section 12)."""


class CodecError(HammertimeError):
    """An event envelope could not be encoded or decoded.

    Covers an unknown `event_type`, an unknown or unsupported
    `schema_version`, and malformed/undecodable bytes (spec section 19,
    spec section 32) -- callers should never see a raw `json.JSONDecodeError`
    or `KeyError` escape the codec.
    """
