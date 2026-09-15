"""Producer/Consumer protocols: publish, subscribe, seek(sequence), commit.

Spec: section 19, section 32

These `Protocol`s are the boundary between the event-driven pipeline (spec
section 19) and whatever durable log backs it. `memory.py` implements them
in-process for tests; `kafka.py` implements them against a real broker. Both
implementations MUST be interchangeable behind this interface -- code that
depends only on `Producer`/`Consumer` should not care which one it got.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ConsumedMessage:
    """One message read off the bus, with enough position info to seek/commit."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes


@runtime_checkable
class Producer(Protocol):
    """Publishes messages to a topic, partitioned by an explicit key.

    `key` selects the partition (spec section 20: ownership/shard assignment
    must be stable per IP), so callers pass a key already derived via
    `topics.TOPICS[topic].key_selector(event)`, not raw domain objects.
    """

    async def publish(self, topic: str, key: bytes | str, value: bytes) -> None:
        """Append `value` to `topic`, routed to a partition derived from `key`."""
        ...

    async def flush(self) -> None:
        """Wait until every message published so far is durably acknowledged."""
        ...


@runtime_checkable
class Consumer(Protocol):
    """Reads messages from a topic as one member of a named consumer group.

    A consumer group's read position is independent of every other group's
    reading the same topic. `commit()` persists that position so that a new
    `Consumer` constructed later for the same group resumes from the
    committed position rather than the start of the log (spec section 23,
    ADR-0003: aggregator consumers are at-least-once and must not re-apply
    counters on redelivery, which depends on committed offsets being
    meaningful across process restarts).
    """

    async def subscribe(self, topic: str) -> AsyncIterator[ConsumedMessage]:
        """Yield messages from `topic`, starting after this group's committed position."""
        ...

    async def seek(self, topic: str, partition: int, offset: int) -> None:
        """Move this consumer's read position for `(topic, partition)` to `offset`.

        Used for replay (spec section 32, section 33): the trie service seeks
        back to a snapshot's `event_sequence` before resuming consumption.
        """
        ...

    async def commit(self) -> None:
        """Durably persist this consumer group's current read position."""
        ...
