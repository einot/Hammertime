"""Agent registry: identity, credentials, enabled/disabled, per-agent limits.

Spec: section 36

`AgentRegistry` is constructed explicitly from already-built `AgentRecord`s
(`AgentRegistry.from_records`) so tests never need real credentials on
disk; `load_agent_registry` is the process-startup path, reading a JSON
file the same way `hammertime.ingest.config.load_settings` reads
environment variables and `hammertime.core.config.loader.load` reads
`config/detection.v1.json`.

`AgentRecord.rate_limit_rps` is data only, exposed for issue #29's rate
limiter to read; this module does not enforce it anywhere.
"""

import hmac
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hammertime.core.errors import ConfigurationError
from hammertime.ingest.auth import AgentDisabledError, InvalidCredentialError, UnknownAgentError

_DEFAULT_AGENTS_PATH = "./config/agents.v1.json"


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """One registered ingest agent."""

    agent_id: str
    #: Bearer token this agent must present (see `hammertime.ingest.auth.middleware`).
    token: str
    #: A disabled agent is known but never authorized (spec section 36).
    enabled: bool = True
    #: Per-agent override for HAMMERTIME_INGEST_RATE_LIMIT_RPS; `None` means
    #: "use the global default" (issue #29 owns actually enforcing this).
    rate_limit_rps: int | None = None


class AgentRegistry:
    """Looks up and authenticates agents by `(agent_id, bearer token)`."""

    def __init__(self, records: Mapping[str, AgentRecord]) -> None:
        self._records = dict(records)

    @classmethod
    def from_records(cls, records: Iterable[AgentRecord]) -> "AgentRegistry":
        return cls({record.agent_id: record for record in records})

    def get(self, agent_id: str) -> AgentRecord | None:
        return self._records.get(agent_id)

    def authenticate(self, agent_id: str, token: str) -> AgentRecord:
        """Return the `AgentRecord` for `agent_id` if `token` is correct and it is enabled.

        Raises `UnknownAgentError` if no such agent is registered,
        `InvalidCredentialError` if the token does not match, or
        `AgentDisabledError` if the agent is registered but disabled.
        """
        record = self._records.get(agent_id)
        if record is None:
            raise UnknownAgentError(f"no registered agent {agent_id!r}")
        # Constant-time comparison: token equality must not leak timing
        # information about how many leading bytes matched. Compared as
        # bytes (utf-8), not str: hmac.compare_digest raises TypeError for
        # a non-ASCII str, and Starlette decodes header bytes as latin-1,
        # so any raw Authorization header byte >= 0x80 would otherwise
        # crash this with an uncaught TypeError instead of a clean 401/403.
        if not hmac.compare_digest(record.token.encode("utf-8"), token.encode("utf-8")):
            raise InvalidCredentialError(f"credential mismatch for agent {agent_id!r}")
        if not record.enabled:
            raise AgentDisabledError(f"agent {agent_id!r} is disabled")
        return record


def _record_from_document(agent_id: str, fields: Any) -> AgentRecord:
    if not isinstance(fields, dict):
        raise ConfigurationError(f"agent {agent_id!r} entry must be a JSON object")

    token = fields.get("token")
    if not isinstance(token, str) or not token:
        raise ConfigurationError(f"agent {agent_id!r} is missing a non-empty 'token'")

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

    unknown = fields.keys() - {"token", "enabled", "rate_limit_rps"}
    if unknown:
        raise ConfigurationError(f"agent {agent_id!r} has unknown fields: {sorted(unknown)}")

    return AgentRecord(
        agent_id=agent_id, token=token, enabled=enabled, rate_limit_rps=rate_limit_rps
    )


def load_agent_registry_document(document: Any) -> AgentRegistry:
    """Build an `AgentRegistry` from an already-parsed JSON document.

    The document is a JSON object keyed by `agent_id`, each value an object
    with a required `token`, optional `enabled` (default `true`), and
    optional `rate_limit_rps` (default `null`) -- see `config/agents.v1.json`
    for an example.
    """
    if not isinstance(document, dict):
        raise ConfigurationError("agent registry document must be a JSON object keyed by agent_id")
    records = [_record_from_document(agent_id, fields) for agent_id, fields in document.items()]

    # A copy-pasted entry that forgot to change its token would otherwise
    # silently let one credential authenticate as multiple agent
    # identities, defeating per-agent authorization/rate-limit/dedup
    # boundaries -- catch it at load time instead.
    seen_tokens: dict[str, str] = {}
    for record in records:
        earlier_agent_id = seen_tokens.get(record.token)
        if earlier_agent_id is not None:
            raise ConfigurationError(
                f"agents {earlier_agent_id!r} and {record.agent_id!r} share the same token"
            )
        seen_tokens[record.token] = record.agent_id

    return AgentRegistry.from_records(records)


def load_agent_registry_file(path: Path) -> AgentRegistry:
    """Read and parse the JSON agent registry document at `path`."""
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ConfigurationError(f"cannot read agent registry at {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigurationError(f"agent registry at {path} is not valid UTF-8: {exc}") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"agent registry at {path} is not valid JSON: {exc}") from exc

    return load_agent_registry_document(document)


def load_agent_registry(env: Mapping[str, str] | None = None) -> AgentRegistry:
    """Load the agent registry from HAMMERTIME_INGEST_AGENTS_PATH (see `.env.example`).

    Mirrors `hammertime.ingest.config.load_settings`'s environment-driven
    style. Tests should prefer `AgentRegistry.from_records` over pointing
    this at a file on disk.
    """
    source = env if env is not None else os.environ
    path = Path(source.get("HAMMERTIME_INGEST_AGENTS_PATH", _DEFAULT_AGENTS_PATH))
    return load_agent_registry_file(path)
