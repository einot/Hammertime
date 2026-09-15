"""Message identity and provenance: agent_id, sequence, event_id, config_version.

Spec: section 4, section 23

ADR-0003 defines message identity for agent-originated observations as
`(agent_id, sequence)`. Internally-produced events (HotIpAdded, HotIpRemoved,
PrefixStatsChanged, ...) are emitted by producers -- the aggregator shard, the
trie service -- that keep their own, independent per-event-type sequence
counters. Two different internal event types emitted by the same producer can
therefore share a `(agent_id, sequence)` pair without being duplicates of one
another. `event_id` MUST be derived from `(agent_id, sequence, event_type)`,
not `(agent_id, sequence)` alone, or unrelated events could collide and be
mistaken for redeliveries of each other during dedup (spec section 23).

ADR-0004 §4 adds an optional `subject` -- the thing the event is about (for
IP-keyed topics, the published entry's IP text) -- that participates in
`event_id` when set, so every message split out of one multi-IP batch
(`services/ingest/publisher.py`) gets its own distinct, deterministic
`event_id` instead of colliding on `(agent_id, sequence, event_type)` alone.
`subject is None` reproduces today's `event_id` byte-for-byte, so every
event type that doesn't set it (HotIpAdded, HotIpRemoved, PrefixStatsChanged)
is unaffected.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

#: The only wire schema version this codebase currently understands.
SCHEMA_VERSION = 1


def compute_event_id(
    agent_id: str, sequence: int, event_type: str, subject: str | None = None
) -> str:
    """Deterministic event_id for `(agent_id, sequence, event_type[, subject])`.

    Same inputs always produce the same event_id, which is what makes
    redelivery idempotent (spec section 23): a consumer that has already
    processed this event_id can safely discard a redelivered copy without
    re-applying side effects.

    `subject is None` (the default) reproduces the pre-ADR-0004 hash exactly,
    byte-for-byte -- this must never change. When `subject` is set, it is
    appended to the hashed tuple so that messages sharing an `(agent_id,
    sequence, event_type)` but differing by subject (ADR-0004's per-IP
    fan-out) get distinct ids.
    """
    digest = hashlib.sha256()
    # A field separator not legal in agent_id/event_type/subject keeps
    # ("a", 1, "bc") from colliding with ("a", 11, "bc") etc.
    message = f"{agent_id}\x1f{sequence}\x1f{event_type}"
    if subject is not None:
        message = f"{message}\x1f{subject}"
    digest.update(message.encode())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class EventEnvelope[T]:
    """Wire envelope wrapping a payload with provenance and identity.

    `event_id` is derived automatically from `(agent_id, sequence,
    event_type, subject)` in `__post_init__` -- it is not a constructor
    argument, so there is no way to construct an envelope whose `event_id`
    is inconsistent with its own identity fields. `config_version`,
    `timestamp` and `payload` play no part in the derivation (ADR-0003
    amendment). `subject` is the thing the event is about (ADR-0004 §4) --
    for IP-keyed topics, the published entry's IP text; `None` for event
    types that don't need it.
    """

    agent_id: str
    sequence: int
    event_type: str
    config_version: int
    timestamp: datetime
    payload: T
    schema_version: int = SCHEMA_VERSION
    subject: str | None = None
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_id",
            compute_event_id(self.agent_id, self.sequence, self.event_type, self.subject),
        )
