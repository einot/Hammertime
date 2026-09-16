"""hammertime.core.auth.tokens: the shared HMAC-SHA-256 credential primitives.

Spec: section 36.1-36.4. ADR: docs/adr/0006-hashed-agent-credentials.md
(decisions 1-3).

Written blind to the implementation, per this repo's test-author
convention -- `hammertime.core.auth.tokens` does not exist yet. Guessed
surface, taken directly from ADR-0006 decision 3 ("The primitives live in
packages/hammertime-core (hammertime.core.auth.tokens: generate_token,
hash_token, derive_key_id, decode_key, plus REGISTRY_VERSION /
HASH_ALGORITHM)"):

* `generate_token() -> str` -- a fresh 256-bit CSPRNG token.
* `decode_key(key: str) -> bytes` -- base64url (unpadded) decode of the
  deployment key, requiring >= 32 decoded bytes; raises
  `ConfigurationError` otherwise (spec 36.1: "MUST refuse to start if the
  key is missing, undecodable, or shorter than 32 bytes").
* `hash_token(token: str, *, key: bytes) -> str` -- HMAC-SHA-256(key,
  token_utf8), hex-encoded, lowercase (ADR-0006 decision 1/2; schema
  `agent_registry.v2.json`'s `token_hash` pattern). Guessed as a
  keyword-only `key` per the task description's `hash_token(token,
  key=...)`.
* `derive_key_id(key: bytes) -> str` -- first 16 hex characters of
  HMAC-SHA-256(key, "hammertime-agent-token-key-id") (ADR-0006 decision
  2, schema `key_id` description -- both spell out the exact label and
  truncation, so this file pins the *exact* value, not just determinism).
* `REGISTRY_VERSION`, `HASH_ALGORITHM` -- the constants mirrored in
  `schemas/agent_registry.v2.json` (`registry_version: 2`,
  `hash_algorithm: "hmac-sha256"`).

If the real signatures differ (e.g. `key` positional, or `hash_token`
taking the raw base64url string rather than decoded bytes), that's a
reconciliation gap to close against whatever the coder actually built --
see the handback report.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import string

import pytest
from hammertime.core.auth.tokens import (
    HASH_ALGORITHM,
    REGISTRY_VERSION,
    decode_key,
    derive_key_id,
    generate_token,
    hash_token,
)
from hammertime.core.errors import ConfigurationError

# ADR-0006 decision 2 / schema: the exact public label HMAC'd to fingerprint
# a key. Pinned here so derive_key_id's *value*, not just its determinism,
# is under test.
_KEY_ID_LABEL = b"hammertime-agent-token-key-id"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class TestGenerateToken:
    def test_returns_a_string(self) -> None:
        assert isinstance(generate_token(), str)

    def test_two_calls_produce_different_values(self) -> None:
        # Basic entropy sanity check, not a rigorous randomness test.
        assert generate_token() != generate_token()

    def test_is_reasonably_long(self) -> None:
        # 256 bits of CSPRNG output, however encoded, cannot reasonably
        # collapse to a short string. secrets.token_urlsafe(32) (ADR-0006's
        # own example) yields 43 characters; this only pins a generous
        # lower bound, not the exact encoding.
        assert len(generate_token()) >= 32


class TestDecodeKey:
    def test_accepts_a_valid_32_byte_base64url_key(self) -> None:
        raw = os.urandom(32)
        decoded = decode_key(_b64url(raw))
        assert decoded == raw

    def test_accepts_a_key_longer_than_32_bytes(self) -> None:
        raw = os.urandom(48)
        decoded = decode_key(_b64url(raw))
        assert decoded == raw
        assert len(decoded) >= 32

    def test_rejects_a_key_shorter_than_32_bytes(self) -> None:
        too_short = _b64url(os.urandom(16))
        with pytest.raises(ConfigurationError):
            decode_key(too_short)

    def test_rejects_an_empty_key(self) -> None:
        with pytest.raises(ConfigurationError):
            decode_key("")

    def test_rejects_invalid_base64url(self) -> None:
        with pytest.raises(ConfigurationError):
            decode_key("not-valid-base64!!! contains spaces and bangs")


class TestHashToken:
    def test_is_deterministic(self) -> None:
        key = os.urandom(32)
        token = "s3cr3t-agent-token-value"
        assert hash_token(token, key=key) == hash_token(token, key=key)

    def test_matches_a_hand_computed_hmac_sha256(self) -> None:
        # ADR-0006 decision 1: "token_hash = HMAC-SHA-256(key, token_utf8),
        # hex-encoded." Pins the exact algorithm, not just "some hash".
        key = os.urandom(32)
        token = "s3cr3t-agent-token-value"
        expected = hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()
        assert hash_token(token, key=key) == expected

    def test_output_is_64_lowercase_hex_characters(self) -> None:
        digest = hash_token("some-token", key=os.urandom(32))
        assert len(digest) == 64
        assert digest == digest.lower()
        assert all(c in string.hexdigits.lower() for c in digest)

    def test_different_tokens_under_the_same_key_produce_different_hashes(self) -> None:
        key = os.urandom(32)
        assert hash_token("token-one", key=key) != hash_token("token-two", key=key)

    def test_the_same_token_under_different_keys_produces_different_hashes(self) -> None:
        token = "s3cr3t-agent-token-value"
        key_a = os.urandom(32)
        key_b = os.urandom(32)
        assert hash_token(token, key=key_a) != hash_token(token, key=key_b)


class TestDeriveKeyId:
    def test_matches_the_specified_construction(self) -> None:
        # ADR-0006 decision 2 / schemas/agent_registry.v2.json: first 16 hex
        # characters of HMAC-SHA-256(key, "hammertime-agent-token-key-id").
        key = os.urandom(32)
        expected = hmac.new(key, _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:16]
        assert derive_key_id(key) == expected

    def test_is_deterministic(self) -> None:
        key = os.urandom(32)
        assert derive_key_id(key) == derive_key_id(key)

    def test_is_16_hex_characters(self) -> None:
        key_id = derive_key_id(os.urandom(32))
        assert len(key_id) == 16
        assert all(c in string.hexdigits.lower() for c in key_id.lower())

    def test_different_keys_produce_different_key_ids(self) -> None:
        # Not a collision proof -- just that a handful of independently
        # generated keys don't happen to fingerprint identically.
        keys = [os.urandom(32) for _ in range(8)]
        key_ids = {derive_key_id(key) for key in keys}
        assert len(key_ids) == len(keys)


class TestRegistryConstants:
    # schemas/agent_registry.v2.json: registry_version const 2,
    # hash_algorithm enum ["hmac-sha256"].
    def test_registry_version_matches_the_schema(self) -> None:
        assert REGISTRY_VERSION == 2

    def test_hash_algorithm_matches_the_schema(self) -> None:
        assert HASH_ALGORITHM == "hmac-sha256"
