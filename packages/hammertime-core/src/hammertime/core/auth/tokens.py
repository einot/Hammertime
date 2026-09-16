"""Shared token/credential crypto primitives for agent bearer-token authentication.

Spec: section 36.1-36.4, ADR-0006

`services/ingest/.../auth/agents.py` (request-time verification) and
`tools/agent-token` (provisioning: generating, hashing, rotating, and
migrating agent credentials) both build on these primitives, so there is
exactly one implementation of "how an agent token becomes what the registry
stores" rather than two that could drift apart.
"""

import base64
import binascii
import hmac
import secrets
from hashlib import sha256

from hammertime.core.errors import ConfigurationError

#: The agent registry document version these hashes are written for
#: (`config/agents.v2.json`, `schemas/agent_registry.v2.json`).
REGISTRY_VERSION = 2

#: The single supported `hash_algorithm` value in the registry envelope.
HASH_ALGORITHM = "hmac-sha256"

#: Fixed, public label HMAC'd under the deployment key to fingerprint it
#: without exposing it (`key_id` in the registry envelope). This is an HMAC
#: of a constant string, not a secret, so publishing it is harmless.
_KEY_ID_LABEL = b"hammertime-agent-token-key-id"

#: Minimum decoded key length, in bytes (256 bits) -- spec section 36.1.
_MIN_KEY_BYTES = 32

#: base64url -> base64 alphabet translation, used so key decoding can be
#: strict (`validate=True`) about rejecting characters outside the base64url
#: alphabet instead of `base64.urlsafe_b64decode`'s default of silently
#: discarding them.
_URLSAFE_TO_STANDARD = str.maketrans("-_", "+/")


def generate_token() -> str:
    """A fresh agent bearer token: 256 bits of entropy from a CSPRNG.

    `secrets.token_urlsafe(32)` draws 32 random bytes (256 bits) and
    base64url-encodes them; the resulting string is longer than 32
    characters, but the entropy behind it is exactly 256 bits either way.
    """
    return secrets.token_urlsafe(32)


def decode_key(value: str) -> bytes:
    """Decode a base64url-encoded deployment key (`HAMMERTIME_INGEST_AGENT_TOKEN_KEY`).

    Accepts unpadded base64url, the convention this repo's `.env.example`
    uses for the value. Raises `ConfigurationError` -- never a bare
    `binascii.Error` -- if `value` is not valid base64url, or decodes to
    fewer than 32 bytes (256 bits): a deployment must not be able to fall
    back to an unkeyed or under-keyed mode by accident (spec section 36.1).
    """
    if not value:
        raise ConfigurationError("agent token key is empty")
    padded = value + "=" * (-len(value) % 4)
    try:
        key = base64.b64decode(padded.translate(_URLSAFE_TO_STANDARD), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigurationError(f"agent token key is not valid base64url: {exc}") from exc
    if len(key) < _MIN_KEY_BYTES:
        raise ConfigurationError(
            f"agent token key decodes to {len(key)} bytes, need at least {_MIN_KEY_BYTES}"
        )
    return key


def hash_token(token: str, *, key: bytes) -> str:
    """`HMAC-SHA-256(key, token)`, hex-encoded (64 lowercase hex characters).

    Not a slow KDF: agent tokens are 256-bit CSPRNG values (`generate_token`),
    not human passwords, and this is computed on every authenticated request
    on ingest's hot path (ADR-0006).
    """
    return hmac.new(key, token.encode("utf-8"), sha256).hexdigest()


def derive_key_id(key: bytes) -> str:
    """Fingerprint `key` without exposing it: first 16 hex chars of an HMAC of a fixed label.

    Lets ingest detect "configured with the wrong
    `HAMMERTIME_INGEST_AGENT_TOKEN_KEY`" as a loud startup `ConfigurationError`
    (a `key_id` mismatch against the registry document) instead of a
    fleet-wide 403 storm.
    """
    return hmac.new(key, _KEY_ID_LABEL, sha256).hexdigest()[:16]
