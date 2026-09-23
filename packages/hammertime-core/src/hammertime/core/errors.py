"""Domain errors shared across services."""


class HammertimeError(Exception):
    """Base class for all Hammertime domain errors."""


class InvalidAddressError(HammertimeError):
    """The supplied string is not a valid address for the declared family."""


class InvalidPrefixError(HammertimeError):
    """Prefix length is outside [0, bit_length] or host bits are set."""


class InvalidAttributesError(HammertimeError):
    """A per-IP attribute document breaks a spec section 46.2 rule.

    Raised by `hammertime.core.events.attributes.validate_ip_attributes`, the
    one implementation of those rules (ADR-0015 decision 5). Deliberately
    neither a `ValueError` nor a `CodecError` (ADR-0015 assumption 31): it is
    bad *input*, which a caller must be able to tell from a bad call (a
    family-mismatch `ValueError`), and a record-map rejection involves no
    envelope. Its message never contains a value from the document.
    """


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
