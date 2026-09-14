"""Event payloads. Mirrors of schemas/*.json; the JSON Schema files are authoritative.

Spec: section 4, section 19, section 32
"""

from __future__ import annotations


from dataclasses import dataclass
from datetime import datetime

from hammertime.core.addressing.address import Address


@dataclass(frozen=True, slots=True)
class Observation:
    """One (ip, delta) pair inside an agent message."""

    ip: Address
    request_count: int


@dataclass(frozen=True, slots=True)
class RequestObservation:
    """A validated, deduplicated agent message ready for the aggregator."""

    agent_id: str
    sequence: int
    window_start: datetime
    window_seconds: int
    observations: tuple[Observation, ...]


@dataclass(frozen=True, slots=True)
class HotIpAdded:
    ip: Address
    timestamp: datetime
    sequence: int
    window_count: int
    config_version: int


@dataclass(frozen=True, slots=True)
class HotIpRemoved:
    ip: Address
    timestamp: datetime
    sequence: int
    window_count: int
    config_version: int


@dataclass(frozen=True, slots=True)
class PrefixStatsChanged:
    """Emitted by the trie service so classification stays out of trie maintenance.

    Spec section 14: the scoring algorithm SHOULD remain separate from trie maintenance.
    """

    prefix: str
    hot_count: int
    capacity: int
    sequence: int
    timestamp: datetime
