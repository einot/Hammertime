"""Producer/Consumer protocols: publish, subscribe, seek(sequence), commit.

Spec: section 19, section 32

These `Protocol`s are the boundary between the event-driven pipeline (spec
section 19) and whatever durable log backs it. `memory.py` implements them
in-process for tests; `kafka.py` implements them against a real broker. Both
implementations MUST be interchangeable behind this interface -- code that
depends only on `Producer`/`Consumer` should not care which one it got.
"""

from collections.abc import AsyncIterator, Iterable
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
class AssignmentListener(Protocol):
    """Told which `(topic, partition)` pairs this consumer owns, and when.

    A partition of `hammertime.observations.v1` *is* a shard (spec section
    20: `hash(IP) -> shard`, one owner per IP), so the consumer group's
    assignment is the set of shard claims this member holds (ADR-0011
    decision 1). Consumers never compute an IP hash of their own -- they
    learn an IP's shard from `ConsumedMessage.partition` and learn which
    shards are theirs from here.

    Every rebalance calls `on_revoked` (before the partitions move) and then
    `on_assigned` (after). Both are coroutines, so a listener may flush or
    load durable per-shard state before the claim changes hands.
    """

    async def on_revoked(self, partitions: frozenset[tuple[str, int]]) -> None:
        """Give up `partitions`; called before the broker moves them away."""
        ...

    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None:
        """Take ownership of `partitions`; called once they are held."""
        ...


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


def static_partitions(topic: str, partitions: Iterable[int]) -> frozenset[int]:
    """Normalise a static partition set, rejecting an empty one.

    Every `Consumer` implementation runs this before it touches a broker or
    calls an `AssignmentListener`, so `subscribe(topic, partitions=<empty>)`
    is refused identically on every transport (ADR-0011 amendment 1, item
    A3). A static set is written configuration that no rebalance will ever
    change, so an empty one is a permanently idle worker reporting itself
    healthy -- far more likely a templating accident than an intent. Runtime
    emptiness under group management (`partitions=None`, broker assigns
    nothing) stays legal, because a later rebalance can still fill it.

    Returns the requested partitions, so a caller may pass a one-shot
    iterable without the set being consumed twice.
    """
    requested = frozenset(partitions)
    if not requested:
        raise ValueError(
            f"static partition set for {topic!r} is empty; pass partitions=None "
            f"for group-managed assignment"
        )
    return requested


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

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        """Yield messages from `topic`, starting after this group's committed position.

        `partitions=None` is group-managed assignment: the broker decides
        which partitions this member owns and may move them at any time --
        including assigning none, which is a legal state a later rebalance
        may change. Passing `partitions` is static assignment -- this member
        owns exactly those partitions of `topic` and no group coordination
        takes place, though offsets are still committed under the group name.
        An empty static set is a `ValueError`, raised before any broker is
        contacted and before `listener` is called (ADR-0011 amendment 1, item
        A3); see `static_partitions`. A deployment MUST NOT mix static and
        group-managed members in one group: the coordinator would hand a
        statically owned partition to a dynamic member as well, giving an IP
        two owners (ADR-0011 decision 1).

        When `listener` is given, this coroutine does not return until
        `on_assigned` has been awaited with the initial assignment (which may
        be empty under group management). That is what lets a service report
        "shard claims held" as soon as `subscribe()` returns (spec section 47,
        ADR-0009 readiness).
        """
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
