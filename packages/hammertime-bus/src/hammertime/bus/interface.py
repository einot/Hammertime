"""Producer/Consumer protocols: publish with dedup, subscribe, ack, close.

Spec: section 19, section 32, section 33

These `Protocol`s are the boundary between the event-driven pipeline (spec
section 19) and whatever durable log backs it. `memory.py` implements them
in-process for tests; `nats.py` implements them against NATS JetStream
(ADR-0013). Both implementations MUST be interchangeable behind this
interface -- code that depends only on `Producer`/`Consumer` should not care
which one it got.

ADR-0013 decision 3 is the authority for this module. What it settled, and
why the shape is what it is: the log has no offset-commit API -- progress is
a per-message acknowledgement, so `Consumer.commit(offsets)` became
`Consumer.ack(messages)`; a consumer's read position is fixed at creation,
so `Consumer.seek` became the `start_offset` argument of `subscribe()`;
nothing ever moves a partition between static members, so
`AssignmentListener.on_revoked` is gone; and the log deduplicates by a
client-supplied id inside a window, so `publish()` takes `message_id`.
"""

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ConsumedMessage:
    """One message read off the bus, with enough position info to ack and replay.

    It carries no acknowledgement handle: each `Consumer` implementation
    retains whatever it needs to acknowledge a delivered message, keyed by
    `(topic, partition, offset)`, keeping the most recent delivery when the
    log redelivers (ADR-0013 decision 3).
    """

    topic: str
    #: The integer suffix of the subject the message is stored under
    #: (ADR-0013 decision 1: partition `p` of topic `T` is subject `T.<p>`).
    partition: int
    #: The stream sequence: unique and strictly increasing across the whole
    #: topic, not contiguous per partition. On `InMemoryBus` it is the log
    #: index.
    offset: int
    key: bytes | None
    value: bytes
    #: JetStream `num_delivered`; always 1 on `InMemoryBus`. Greater than 1
    #: means the log redelivered this message after `ack_wait` (ADR-0013
    #: decision 5), which a consumer that keeps a handled position detects
    #: as `offset <= <last handled offset>`.
    delivery_count: int = 1


@runtime_checkable
class AssignmentListener(Protocol):
    """Told which `(topic, partition)` pairs this consumer owns.

    A partition of `hammertime.observations.v1` *is* a shard (spec section
    20: `hash(IP) -> shard`, one owner per IP), so the set handed here is the
    set of shard claims this member holds (ADR-0011 decision 1, as ADR-0013
    reworked it for static assignment). Consumers never compute an IP hash
    of their own -- they learn an IP's shard from `ConsumedMessage.partition`
    and learn which shards are theirs from here.

    `on_assigned` is awaited exactly once, from inside `subscribe()`, before
    it returns; there is no revocation (ADR-0013 decision 8): the way a
    shard changes hands is a `close()` on one member and a `subscribe()` on
    another.
    """

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

    async def publish(
        self, topic: str, key: bytes | str, value: bytes, *, message_id: str | None = None
    ) -> None:
        """Append `value` to `topic`, routed to `partition_for(key, <partitions>)`.

        Returns only once the log has durably acknowledged the append: a
        returned `publish` is a flushed publish (ADR-0011 decision 4 step 4,
        ADR-0009 A11). With `message_id`, the log deduplicates: a second
        publish to the same topic carrying an id the log has seen inside its
        duplicate window is acknowledged and NOT appended, and this returns
        normally. With `message_id=None` no deduplication is attempted. A
        `str` key is encoded as UTF-8 before hashing and before it becomes
        the message key. An unregistered topic is a `KeyError` from
        `NatsProducer`; `MemoryProducer` creates topics on demand.
        """
        ...

    async def flush(self) -> None:
        """Return once every `publish` that has returned has been acknowledged.

        A no-op in both implementations (`publish` already awaits the
        acknowledgement); retained because ADR-0009 decision 7's
        flush-before-ack rule is stated against the interface (ADR-0009 A11).
        """
        ...


def static_partitions(topic: str, partitions: Iterable[int]) -> frozenset[int]:
    """Normalise a static partition set, rejecting an empty one.

    Every `Consumer` implementation runs this before it touches a broker or
    calls an `AssignmentListener`, so `subscribe(topic, partitions=<empty>)`
    is refused identically on every transport (ADR-0011 amendment 1, item
    A3; unchanged by ADR-0013). A static set is written configuration, so
    an empty one is a permanently idle worker reporting itself healthy --
    far more likely a templating accident than an intent. `partitions=None`
    is the only way of saying "every partition of the topic".

    Returns the requested partitions, so a caller may pass a one-shot
    iterable without the set being consumed twice.
    """
    requested = frozenset(partitions)
    if not requested:
        raise ValueError(
            f"static partition set for {topic!r} is empty; pass partitions=None "
            f"for every partition of the topic"
        )
    return requested


@runtime_checkable
class Consumer(Protocol):
    """Reads messages from a topic as one member of a named consumer group.

    A group's read position is independent of every other group's reading
    the same topic, is kept by the log under the group name, and is advanced
    only by `ack()` -- the only "commit" there is (spec section 23, ADR-0003:
    aggregator consumers are at-least-once and must not re-apply counters on
    redelivery, which depends on acknowledged positions being meaningful
    across process restarts).
    """

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        """Claim partitions of `topic`, tell `listener`, then yield messages.

        Exactly one call per `Consumer` instance; a second call raises
        `RuntimeError`. The iterator yields messages of the subscribed
        partitions in stream-sequence order within a partition (across
        partitions of one topic the interleaving is unspecified) and ends
        after `close()`.

        `partitions=None` means every partition of the topic: on
        `NatsConsumer` one whole-topic durable, on `InMemoryBus` `{0}`.
        There is no group-managed mode (ADR-0013: static assignment only).
        `partitions=<iterable>` means exactly those partitions; an empty
        iterable is a `ValueError` raised before any broker is contacted and
        before `listener` is called (ADR-0011 A3); `InMemoryBus` has one
        partition per topic, so any static set other than `{0}` is a
        `ValueError` there.

        When `listener` is given, `on_assigned` is awaited exactly once, with
        the full set, before this returns -- that is what keeps ADR-0009's
        "shard claims held" readiness observable at the end of `start()`. If
        `on_assigned` raises, the exception propagates and the consumer is
        left as if `subscribe()` had never been called.

        `start_offset=None` is a durable subscription: the read position is
        kept by the log under the group and advanced only by `ack()`; a
        fresh `Consumer` for the same group and partitions is delivered every
        message that was never acknowledged, in order. `start_offset=<int>`
        is a positional subscription: delivery starts at the first message
        whose `offset >= start_offset` (at the first retained message if that
        sequence is no longer in the log), no position is kept anywhere, and
        `ack()` on it is a `ValueError` (spec section 32, section 33: the
        trie replays from its snapshot this way).
        """
        ...

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        """Acknowledge exactly `messages`, advancing the group's position past them.

        Each message MUST have been delivered by this consumer instance under
        its durable subscription and not yet acknowledged; anything else -- a
        message of another topic or partition, one this instance never
        delivered, one it already acknowledged, any message on a positional
        subscription, or any call after `close()` -- is a `ValueError`,
        raised before anything is acknowledged (the whole iterable is checked
        first). An empty iterable returns normally without touching the
        broker. A caller acknowledges what it has handled and nothing else
        (ADR-0011 A20's requirement, now per message). On `NatsConsumer` an
        `ack()` that has returned is durable (the server confirmed each ack).
        """
        ...

    async def close(self) -> None:
        """Stop delivery, end the iterator, and give back what was not acknowledged.

        Every message this instance delivered under a durable subscription
        and did not acknowledge becomes deliverable to the next consumer for
        the group without waiting for `ack_wait` (`NatsConsumer` sends a
        negative acknowledgement for each; `MemoryConsumer` has nothing to
        send). Idempotent and safe before `subscribe()`. After `close()`,
        `ack()` is a `ValueError`.
        """
        ...


@runtime_checkable
class MessageBus(Protocol):
    """What a service needs from a bus: one producer and group consumers.

    `InMemoryBus` and `NatsBus` both satisfy it, so a service's object graph
    is identical either way (ADR-0009 decision 3). Moved here from
    `hammertime.aggregator.worker` by ADR-0013 decision 3.
    """

    def producer(self) -> Producer: ...

    def consumer(self, group_id: str) -> Consumer: ...
