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
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

#: The only wire schema version this codebase currently understands.
SCHEMA_VERSION = 1


def compute_event_id(agent_id: str, sequence: int, event_type: str) -> str:
    """Deterministic event_id for `(agent_id, sequence, event_type)`.

    Same inputs always produce the same event_id, which is what makes
    redelivery idempotent (spec section 23): a consumer that has already
    processed this event_id can safely discard a redelivered copy without
    re-applying side effects.
    """
    digest = hashlib.sha256()
    # A field separator not legal in agent_id/event_type keeps
    # ("a", 1, "bc") from colliding with ("a", 11, "bc") etc.
    digest.update(f"{agent_id}\x1f{sequence}\x1f{event_type}".encode())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class EventEnvelope[T]:
    """Wire envelope wrapping a payload with provenance and identity.

    `event_id` is derived automatically from `(agent_id, sequence,
    event_type)` in `__post_init__` -- it is not a constructor argument, so
    there is no way to construct an envelope whose `event_id` is
    inconsistent with its own identity fields. `config_version`, `timestamp`
    and `payload` play no part in the derivation (ADR-0003 amendment).
    """

    agent_id: str
    sequence: int
    event_type: str
    config_version: int
    timestamp: datetime
    payload: T
    schema_version: int = SCHEMA_VERSION
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_id", compute_event_id(self.agent_id, self.sequence, self.event_type)
        )
