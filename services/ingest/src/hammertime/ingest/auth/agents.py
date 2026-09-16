"""Agent registry: identity, hashed credentials, enabled/disabled, per-agent limits.

Spec: section 36, section 36.1-36.4; ADR-0006

`AgentRegistry` is constructed explicitly from already-built `AgentRecord`s
(`AgentRegistry.from_records`) so tests never need real credentials on
disk; `load_agent_registry` is the process-startup path, reading a JSON
file the same way `hammertime.ingest.config.load_settings` reads
environment variables and `hammertime.core.config.loader.load` reads
`config/detection.v1.json`.

Per ADR-0006, the registry stores only keyed hashes of agent bearer tokens
(`AgentRecord.token_hash`), never the tokens themselves: `HMAC-SHA-256(key,
token)`, where `key` is a deployment-wide secret supplied out of band
(`HAMMERTIME_INGEST_AGENT_TOKEN_KEY`) and never persisted in the registry
document. The shared hashing/key primitives live in
`hammertime.core.auth.tokens` so this module and `tools/agent-token` (the
only supported way to provision an entry) share one implementation.

`AgentRecord.rate_limit_rps` is data only, exposed for issue #29's rate
limiter to read; this module does not enforce it anywhere.
"""

import hmac
import json
import logging
import os
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hammertime.core.auth import tokens
from hammertime.core.errors import ConfigurationError
from hammertime.core.time.clock import Clock, SystemClock
from hammertime.ingest.auth import AgentDisabledError, InvalidCredentialError, UnknownAgentError

logger = logging.getLogger(__name__)

_DEFAULT_AGENTS_PATH = "./config/agents.v2.json"
_AGENT_TOKEN_KEY_ENV = "HAMMERTIME_INGEST_AGENT_TOKEN_KEY"

_MIGRATE_HINT = "convert it with `hammertime-agent-token migrate`"

_ENVELOPE_REQUIRED_KEYS = {"registry_version", "hash_algorithm", "key_id", "agents"}
_RECORD_ALLOWED_KEYS = {
    "token_hash",
    "previous_token_hash",
    "previous_token_expires_at",
    "enabled",
    "rate_limit_rps",
}
_HASH_HEX_LENGTH = 64  # 32-byte HMAC-SHA-256 digest, hex-encoded

#: Size in bytes of the fixed per-registry dummy digest compared against an
#: unknown agent_id's presented token (ADR-0007, spec section 36.5). Matches
#: the size of a real `tokens.hash_token(...)` digest decoded from hex (see
#: `_HASH_HEX_LENGTH` above). `authenticate()`'s unknown-agent branch computes
#: a real HMAC-SHA-256 of the presented token via `tokens.hash_token` -- the
#: same computation the known-agent path performs -- and spends the same
#: `hmac.compare_digest` call, against a value fixed once per registry
#: instance (drawn with `secrets.token_bytes`) rather than per request, so an
#: unknown identity costs exactly as much work as a known one with a wrong
#: token.
_DUMMY_HASH_LENGTH = 32


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """One registered ingest agent."""

    agent_id: str
    #: `HMAC-SHA-256(key, token)`, decoded from hex to 32 raw bytes -- never
    #: the token itself (see `hammertime.ingest.auth.middleware`, ADR-0006).
    token_hash: bytes
    #: Hash of the immediately previous token, accepted alongside
    #: `token_hash` for the duration of a rotation overlap window. Requires
    #: `previous_token_expires_at` (both-or-neither).
    previous_token_hash: bytes | None = None
    #: RFC 3339 UTC instant after which `previous_token_hash` stops being
    #: accepted. Required whenever `previous_token_hash` is set.
    previous_token_expires_at: datetime | None = None
    #: A disabled agent is known but never authorized (spec section 36).
    enabled: bool = True
    #: Per-agent override for HAMMERTIME_INGEST_RATE_LIMIT_RPS; `None` means
    #: "use the global default" (issue #29 owns actually enforcing this).
    rate_limit_rps: int | None = None

    def __post_init__(self) -> None:
        # Defense-in-depth for the both-or-neither invariant (spec section
        # 36.3): `_record_from_document` already enforces this for the
        # document-loading path with a more specific message, but
        # `AgentRecord` is a public constructor (`AgentRegistry.from_records`)
        # and must not accept a half-set rotation pair from any caller.
        has_hash = self.previous_token_hash is not None
        has_expiry = self.previous_token_expires_at is not None
        if has_hash != has_expiry:
            raise ConfigurationError(
                f"agent {self.agent_id!r} must set both 'previous_token_hash' and "
                "'previous_token_expires_at', or neither"
            )


class AgentRegistry:
    """Looks up and authenticates agents by `(agent_id, bearer token)`.

    `key` is the deployment-wide secret every `token_hash`/`previous_token_hash`
    in the registry was computed under (ADR-0006); `authenticate` recomputes
    the presented token's HMAC under this same key before comparing. `clock`
    is used only to evaluate `previous_token_expires_at` (defaults to
    `SystemClock`, matching `hammertime.ingest.ratelimit.RateLimiter`'s
    injectable-clock convention).
    """

    def __init__(
        self,
        records: Mapping[str, AgentRecord],
        *,
        key: bytes,
        clock: Clock | None = None,
    ) -> None:
        self._records = dict(records)
        self._key = key
        self._clock = clock if clock is not None else SystemClock()
        # Fixed for the lifetime of this registry instance -- see
        # _DUMMY_HASH_LENGTH above.
        self._dummy_hash = secrets.token_bytes(_DUMMY_HASH_LENGTH)

    @classmethod
    def from_records(
        cls,
        records: Iterable[AgentRecord],
        *,
        key: bytes,
        clock: Clock | None = None,
    ) -> "AgentRegistry":
        return cls({record.agent_id: record for record in records}, key=key, clock=clock)

    def get(self, agent_id: str) -> AgentRecord | None:
        return self._records.get(agent_id)

    def authenticate(self, agent_id: str, token: str) -> AgentRecord:
        """Return the `AgentRecord` for `agent_id` if `token` is correct and it is enabled.

        Raises `UnknownAgentError` if no such agent is registered,
        `InvalidCredentialError` if the token does not match, or
        `AgentDisabledError` if the agent is registered but disabled.

        Timing uniformity (ADR-0007, spec section 36.5): an unknown
        `agent_id` still computes a real HMAC-SHA-256 of the presented token
        (the same `tokens.hash_token` call the known-agent path below makes)
        and performs the same `hmac.compare_digest` call, against a fixed
        per-registry dummy digest, so an unknown identity is not measurably
        faster to reject than a known one with a wrong token.
        """
        record = self._records.get(agent_id)
        if record is None:
            presented_hash = bytes.fromhex(tokens.hash_token(token, key=self._key))
            hmac.compare_digest(presented_hash, self._dummy_hash)
            raise UnknownAgentError(f"no registered agent {agent_id!r}")

        # Recompute the presented token's HMAC once, then compare the fixed-
        # length (32-byte) digest against token_hash and, structurally, against
        # previous_token_hash. Both comparisons decode hex to raw bytes first
        # and use hmac.compare_digest on those bytes: comparing decoded bytes
        # (rather than the hex strings) means case in the presented digest
        # never matters, and compare_digest is timing-safe either way since
        # both operands are always exactly 32 bytes regardless of the
        # presented token's content.
        presented_hash = bytes.fromhex(tokens.hash_token(token, key=self._key))
        current_matches = hmac.compare_digest(presented_hash, record.token_hash)

        previous_matches = False
        if record.previous_token_hash is not None:
            # Always run this comparison when a previous hash is structurally
            # present, regardless of whether current_matches already
            # succeeded -- so the number of constant-time comparisons
            # performed depends only on the registry, never on the presented
            # token (ADR-0006). Whether an expired window is honoured is
            # decided afterwards, and is not itself timing-sensitive.
            hash_matches = hmac.compare_digest(presented_hash, record.previous_token_hash)
            not_expired = (
                record.previous_token_expires_at is None
                or self._clock.now() < record.previous_token_expires_at.timestamp()
            )
            previous_matches = hash_matches and not_expired

        if not (current_matches or previous_matches):
            raise InvalidCredentialError(f"credential mismatch for agent {agent_id!r}")
        if not record.enabled:
            raise AgentDisabledError(f"agent {agent_id!r} is disabled")
        return record


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """`object_pairs_hook` for `json.loads`: raise on a duplicate key in any JSON object.

    Plain `json.loads` silently keeps only the last occurrence of a
    duplicate key (e.g. two `"some-agent-id": {...}` entries under
    `"agents"`), which would shadow the first entry with no error and no
    trace of it in the loader's duplicate-hash check -- an operator reading
    the top of the file would never see the entry actually in effect.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"agent registry document has a duplicate key {key!r}")
        result[key] = value
    return result


def _parse_hash_hex(agent_id: str, field_name: str, value: Any) -> bytes:
    if not isinstance(value, str) or len(value) != _HASH_HEX_LENGTH:
        raise ConfigurationError(
            f"agent {agent_id!r} field {field_name!r} must be {_HASH_HEX_LENGTH} hex characters"
        )
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise ConfigurationError(
            f"agent {agent_id!r} field {field_name!r} is not valid hex: {exc}"
        ) from exc


def _parse_expires_at(agent_id: str, value: Any) -> datetime:
    if not isinstance(value, str):
        raise ConfigurationError(
            f"agent {agent_id!r} field 'previous_token_expires_at' must be an RFC 3339 string"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(
            f"agent {agent_id!r} field 'previous_token_expires_at' is not a valid "
            f"RFC 3339 date-time: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise ConfigurationError(
            f"agent {agent_id!r} field 'previous_token_expires_at' must include an "
            f"explicit UTC offset (RFC 3339 date-time), got {value!r} with no offset"
        )
    return parsed


def _record_from_document(agent_id: str, fields: Any, *, clock: Clock) -> AgentRecord:
    if not isinstance(fields, dict):
        raise ConfigurationError(f"agent {agent_id!r} entry must be a JSON object")

    if "token" in fields:
        raise ConfigurationError(
            f"agent {agent_id!r} entry carries a plaintext 'token' field; this is the "
            f"v1 registry format and is no longer accepted -- {_MIGRATE_HINT}"
        )

    if "token_hash" not in fields:
        raise ConfigurationError(f"agent {agent_id!r} is missing required field 'token_hash'")
    token_hash = _parse_hash_hex(agent_id, "token_hash", fields["token_hash"])

    has_previous_hash = "previous_token_hash" in fields
    has_previous_expiry = "previous_token_expires_at" in fields
    if has_previous_hash != has_previous_expiry:
        raise ConfigurationError(
            f"agent {agent_id!r} must set both 'previous_token_hash' and "
            "'previous_token_expires_at', or neither"
        )

    previous_token_hash: bytes | None = None
    previous_token_expires_at: datetime | None = None
    if has_previous_hash:
        previous_token_hash = _parse_hash_hex(
            agent_id, "previous_token_hash", fields["previous_token_hash"]
        )
        if previous_token_hash == token_hash:
            raise ConfigurationError(
                f"agent {agent_id!r} has previous_token_hash equal to token_hash "
                "-- this rotation did not rotate anything"
            )
        previous_token_expires_at = _parse_expires_at(agent_id, fields["previous_token_expires_at"])
        # An overlap window that had already closed before this process even
        # started is not a startup failure: drop the stale predecessor and
        # keep going (ADR-0006) -- but a restart after the window closed must
        # not silently keep honouring the dead credential.
        if clock.now() >= previous_token_expires_at.timestamp():
            logger.warning(
                "agent %r: previous_token_hash expired at %s, dropping it at load time",
                agent_id,
                previous_token_expires_at.isoformat(),
            )
            previous_token_hash = None
            previous_token_expires_at = None

    enabled = fields.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigurationError(f"agent {agent_id!r} field 'enabled' must be a boolean")

    rate_limit_rps = fields.get("rate_limit_rps")
    if rate_limit_rps is not None and (
        isinstance(rate_limit_rps, bool) or not isinstance(rate_limit_rps, int)
    ):
        raise ConfigurationError(
            f"agent {agent_id!r} field 'rate_limit_rps' must be an integer or null"
        )
    if rate_limit_rps is not None and rate_limit_rps <= 0:
        # RateLimiter.check() raises ValueError for a non-positive limit_rps
        # (ratelimit/__init__.py) -- an unhandled 500 on every request for
        # this agent, rather than the clean startup-time rejection an
        # operator's typo deserves. There is no "rate-limited to zero"
        # concept; disable the agent instead (`"enabled": false`).
        raise ConfigurationError(
            f"agent {agent_id!r} field 'rate_limit_rps' must be a positive "
            f'integer, got {rate_limit_rps!r} -- set "enabled": false to '
            "block an agent instead of a zero or negative rate limit"
        )

    unknown = fields.keys() - _RECORD_ALLOWED_KEYS
    if unknown:
        raise ConfigurationError(f"agent {agent_id!r} has unknown fields: {sorted(unknown)}")

    return AgentRecord(
        agent_id=agent_id,
        token_hash=token_hash,
        previous_token_hash=previous_token_hash,
        previous_token_expires_at=previous_token_expires_at,
        enabled=enabled,
        rate_limit_rps=rate_limit_rps,
    )


def load_agent_registry_document(
    document: Any, *, key: bytes, clock: Clock | None = None
) -> AgentRegistry:
    """Build an `AgentRegistry` from an already-parsed JSON document.

    The document is the versioned `config/agents.v2.json` envelope
    (`schemas/agent_registry.v2.json`, ADR-0006): `registry_version` (must be
    `2`), `hash_algorithm` (must be `"hmac-sha256"`), `key_id` (must match
    `key`'s fingerprint), and `agents`, an object keyed by `agent_id` whose
    values carry a required `token_hash`, optional `previous_token_hash` +
    `previous_token_expires_at`, and the existing `enabled`/`rate_limit_rps`.
    """
    resolved_clock = clock if clock is not None else SystemClock()

    if not isinstance(document, dict):
        raise ConfigurationError("agent registry document must be a JSON object")

    if "registry_version" not in document:
        raise ConfigurationError(
            "agent registry document has no 'registry_version' field; this looks like "
            f"the v1 plaintext-token format and is no longer accepted -- {_MIGRATE_HINT}"
        )
    registry_version = document["registry_version"]
    if registry_version != tokens.REGISTRY_VERSION:
        raise ConfigurationError(
            f"agent registry document has registry_version={registry_version!r}, "
            f"expected {tokens.REGISTRY_VERSION}"
        )

    hash_algorithm = document.get("hash_algorithm")
    if hash_algorithm != tokens.HASH_ALGORITHM:
        raise ConfigurationError(
            f"agent registry document has hash_algorithm={hash_algorithm!r}, "
            f"expected {tokens.HASH_ALGORITHM!r}"
        )

    key_id = document.get("key_id")
    expected_key_id = tokens.derive_key_id(key)
    if key_id != expected_key_id:
        raise ConfigurationError(
            f"agent registry document key_id={key_id!r} does not match the configured "
            f"HAMMERTIME_INGEST_AGENT_TOKEN_KEY (expected key_id={expected_key_id!r}) -- "
            "the registry was hashed under a different key"
        )

    agents = document.get("agents")
    if not isinstance(agents, dict):
        raise ConfigurationError("agent registry document field 'agents' must be a JSON object")

    unknown = document.keys() - _ENVELOPE_REQUIRED_KEYS
    if unknown:
        raise ConfigurationError(f"agent registry document has unknown fields: {sorted(unknown)}")

    records = [
        _record_from_document(agent_id, fields, clock=resolved_clock)
        for agent_id, fields in agents.items()
    ]

    # A copy-pasted entry that forgot to change its token would otherwise
    # silently let one credential authenticate as multiple agent
    # identities, defeating per-agent authorization/rate-limit/dedup
    # boundaries -- catch it at load time instead. Checked across both
    # current and (still-live) previous hashes, and across the whole file.
    seen_hashes: dict[bytes, str] = {}
    for record in records:
        for hash_value in (record.token_hash, record.previous_token_hash):
            if hash_value is None:
                continue
            earlier_agent_id = seen_hashes.get(hash_value)
            if earlier_agent_id is not None:
                raise ConfigurationError(
                    f"agents {earlier_agent_id!r} and {record.agent_id!r} share the same token hash"
                )
            seen_hashes[hash_value] = record.agent_id

    return AgentRegistry.from_records(records, key=key, clock=resolved_clock)


def load_agent_registry_file(
    path: Path, *, key: bytes, clock: Clock | None = None
) -> AgentRegistry:
    """Read and parse the JSON agent registry document at `path`."""
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ConfigurationError(f"cannot read agent registry at {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigurationError(f"agent registry at {path} is not valid UTF-8: {exc}") from exc

    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"agent registry at {path} is not valid JSON: {exc}") from exc

    return load_agent_registry_document(document, key=key, clock=clock)


def read_agent_token_key(env: Mapping[str, str] | None = None) -> bytes:
    """Read and decode `HAMMERTIME_INGEST_AGENT_TOKEN_KEY` (see `.env.example`).

    Deliberately not read via `hammertime.ingest.config.IngestSettings`:
    that dataclass's `repr` gets logged, and this value is a secret
    (ADR-0006, spec section 36.1). Ingest MUST refuse to start without it,
    rather than falling back to an unkeyed mode.
    """
    source = env if env is not None else os.environ
    raw = source.get(_AGENT_TOKEN_KEY_ENV)
    if not raw:
        raise ConfigurationError(f"{_AGENT_TOKEN_KEY_ENV} is required to build the agent registry")
    return tokens.decode_key(raw)


def load_agent_registry(
    env: Mapping[str, str] | None = None, *, clock: Clock | None = None
) -> AgentRegistry:
    """Load the agent registry from HAMMERTIME_INGEST_AGENTS_PATH and
    HAMMERTIME_INGEST_AGENT_TOKEN_KEY (see `.env.example`).

    Mirrors `hammertime.ingest.config.load_settings`'s environment-driven
    style. Tests should prefer `AgentRegistry.from_records` over pointing
    this at a file on disk.
    """
    source = env if env is not None else os.environ
    path = Path(source.get("HAMMERTIME_INGEST_AGENTS_PATH", _DEFAULT_AGENTS_PATH))
    key = read_agent_token_key(source)
    return load_agent_registry_file(path, key=key, clock=clock)
