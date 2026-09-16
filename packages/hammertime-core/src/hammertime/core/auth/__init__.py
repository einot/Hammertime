"""Credential primitives shared by ingest and the agent-token provisioning tool.

Spec: section 36.1-36.4, ADR-0006
"""

from hammertime.core.auth.tokens import (
    HASH_ALGORITHM,
    REGISTRY_VERSION,
    decode_key,
    derive_key_id,
    generate_token,
    hash_token,
)

__all__ = [
    "HASH_ALGORITHM",
    "REGISTRY_VERSION",
    "decode_key",
    "derive_key_id",
    "generate_token",
    "hash_token",
]
