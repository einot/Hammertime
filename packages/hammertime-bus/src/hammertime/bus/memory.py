"""In-process transport for unit and property tests; same contract as `nats.py`.

Spec: section 19, section 32, section 33

`InMemoryBus` is the shared broker: one append-only log per topic, held in
process memory. `MemoryProducer`/`MemoryConsumer` are thin views onto it that
implement `hammertime.bus.interface.Producer`/`Consumer`, so tests can swap
the JetStream transport for this one without changing calling code.

What it models, per ADR-0013 decision 3 (`hammertime.bus.memory`):

* one partition per topic, `0`, so `partition_for` is never consulted and
  `partitions=None` means `{0}`;
* deduplication by `message_id` per topic for the lifetime of the bus -- an
  unbounded duplicate window, because a test never needs the window to
  lapse;
* per `(topic, group)` the set of acknowledged offsets, kept on the bus so a
  fresh consumer for the same group is delivered every message that was
  never acknowledged, in order, however many earlier consumers read it;
* per consumer instance its own delivered-but-unacknowledged set, which is
  what `ack()` checks against;
* no redelivery to a live consumer (there is no `ack_wait`), so
  `delivery_count` is always 1; and at most one live member per group per
  topic (ADR-0011 assumption 22, unchanged);
* `end_offset(topic)`, the offset the next appended message will receive,
  is the log length (`0` for a topic never published to), the same
  readiness number `NatsBus` derives from `last_seq + 1` (decision 9);
* `first_offset(topic)` is `0` for every topic: the log never discards, so
  `0` is a non-empty log's first index and an empty log's `end_offset`
  (Amendment 10). No retention or purge is modelled.
* `last_value(topic)` is the value of the last record in the topic's log,
  `None` for an empty log or a topic never published to, registered or not;
  a publish dropped as a duplicate appends nothing and so does not change it
  (Amendment 13).
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass

from hammertime.bus.interface import AssignmentListener, ConsumedMessage, static_partitions

#: The one partition every `InMemoryBus` topic has.
_PARTITION = 0


@dataclass(frozen=True, slots=True)
class _Record:
    key: bytes | None
    value: bytes


class InMemoryBus:
    """A shared, in-process broker: one append-only log per topic.

    Construct a single `InMemoryBus` per test/process and hand out
    `producer()`/`consumer(group_id)` instances from it; every instance
    shares the same underlying logs, the way independent producer/consumer
    processes share one real broker.

    Every topic is a single partition here -- enough to exercise ordering,
    dedup, acknowledgement and replay semantics without a real partitioned
    log. `topics.py`'s partition counts describe the JetStream-backed
    deployment, not this in-memory one.
    """

    def __init__(self) -> None:
        self._logs: dict[str, list[_Record]] = defaultdict(list)
        # topic -> every message_id ever published to it (the unbounded
        # duplicate window of ADR-0013 decision 3).
        self._seen_ids: dict[str, set[str]] = defaultdict(set)
        # (topic, group_id) -> acknowledged offsets.
        self._acked: dict[tuple[str, str], set[int]] = defaultdict(set)
        self._condition = asyncio.Condition()

    def producer(self) -> "MemoryProducer":
        """A new producer view onto this bus."""
        return MemoryProducer(self)

    def consumer(self, group_id: str) -> "MemoryConsumer":
        """A new consumer view onto this bus as a member of `group_id`."""
        return MemoryConsumer(self, group_id)

    async def end_offset(self, topic: str) -> int:
        """The offset the next appended message will receive: the log length.

        `0` for a topic never published to (ADR-0013 decision 9, assumption
        20). `async` so that the trie and detector call it the same way on
        `NatsBus`, where it is a round trip to the broker.
        """
        # A `defaultdict` read: an unknown topic gets an empty log entry,
        # which is exactly what its first publish or subscribe would create.
        return len(self._logs[topic])

    async def first_offset(self, topic: str) -> int:
        """The first offset the log retains: always `0`, since it never discards.

        For a non-empty log that is its first index; for an empty one it is
        also its `end_offset` (ADR-0013 decision 3, Amendment 10). `async`
        like `NatsBus.first_offset`, a broker round trip there.
        """
        return 0

    async def last_value(self, topic: str) -> bytes | None:
        """The value of the last record in `topic`'s log, or `None` when it is empty.

        `None` for a topic never published to, registered or not; a publish
        dropped as a duplicate appends nothing and so does not change it
        (ADR-0013 decision 3, Amendment 13). `async` like
        `NatsBus.last_value`, a broker round trip there.
        """
        log = self._logs.get(topic)
        if not log:
            return None
        return log[-1].value

    async def _append(
        self, topic: str, key: bytes | None, value: bytes, message_id: str | None
    ) -> None:
        async with self._condition:
            if message_id is not None:
                if message_id in self._seen_ids[topic]:
                    # Acknowledged and not appended: the log has seen this id.
                    return
                self._seen_ids[topic].add(message_id)
            self._logs[topic].append(_Record(key=key, value=value))
            self._condition.notify_all()

    async def _wait_for(
        self, topic: str, offset: int, closed: Callable[[], bool]
    ) -> _Record | None:
        """Block until offset `offset` exists in `topic`'s log, or `closed()`.

        Returns `None` once the consumer waiting here has been closed, which
        is how a live iterator ends after `close()`.
        """
        async with self._condition:
            while not closed() and offset >= len(self._logs[topic]):
                await self._condition.wait()
            if closed():
                return None
            return self._logs[topic][offset]

    async def _wake(self) -> None:
        """Wake every waiting consumer so a closed one can notice and end."""
        async with self._condition:
            self._condition.notify_all()

    def _is_acked(self, topic: str, group_id: str, offset: int) -> bool:
        return offset in self._acked[(topic, group_id)]

    def _mark_acked(self, topic: str, group_id: str, offsets: Iterable[int]) -> None:
        self._acked[(topic, group_id)].update(offsets)


class MemoryProducer:
    """`Producer` writing into an `InMemoryBus`: partition 0, dedup by `message_id`."""

    def __init__(self, bus: InMemoryBus) -> None:
        self._bus = bus

    async def publish(
        self, topic: str, key: bytes | str, value: bytes, *, message_id: str | None = None
    ) -> None:
        """Append to `topic`'s single partition, unless `message_id` was seen before.

        Creates the topic on demand (unregistered topics are fine here, unlike
        `NatsProducer`). A repeated `message_id` is acknowledged and not
        appended; `message_id=None` never deduplicates.
        """
        raw_key = key.encode("utf-8") if isinstance(key, str) else key
        await self._bus._append(topic, raw_key, value, message_id)

    async def flush(self) -> None:
        """No-op: `publish` already appends synchronously within the event loop."""
        return None


class MemoryConsumer:
    """`Consumer` reading from an `InMemoryBus` as member of `group_id`.

    One subscription per instance (ADR-0013 assumption 23). Under a durable
    subscription the iterator walks the log from the start, skipping every
    offset the group has already acknowledged, so a consumer constructed
    after a crash is handed exactly what its predecessor never acknowledged
    (spec section 23, ADR-0003). Under a positional subscription it walks
    from `start_offset` and acknowledges nothing.
    """

    def __init__(self, bus: InMemoryBus, group_id: str) -> None:
        self._bus = bus
        self._group_id = group_id
        self._subscribed = False
        self._closed = False
        self._topic: str | None = None
        self._positional = False
        self._position = 0
        # Offsets delivered by this instance and not yet acknowledged
        # (durable subscriptions only).
        self._unacked: set[int] = set()

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        """Claim `topic`'s single partition, tell `listener`, then yield messages.

        Every `InMemoryBus` topic has exactly one partition, 0, so both
        `partitions=None` and a static `{0}` claim `{(topic, 0)}`; any other
        static set names a partition this bus does not have and is a
        `ValueError`, as is an empty one (ADR-0011 A3) -- both raised before
        the listener is called. The listener is awaited here, before the
        iterator is handed back, so a caller that has finished `subscribe()`
        is holding its shard claims; if `on_assigned` raises, this consumer
        is left as if `subscribe()` had never been called.
        """
        if self._closed:
            raise RuntimeError("MemoryConsumer is closed; it cannot subscribe")
        if self._subscribed:
            raise RuntimeError(
                "MemoryConsumer.subscribe() was already called; one subscription per instance"
            )
        assignment = self._assignment(topic, partitions)
        if start_offset is not None and start_offset < 0:
            raise ValueError(f"start_offset must be non-negative, got {start_offset!r}")
        if listener is not None:
            await listener.on_assigned(assignment)
        self._subscribed = True
        self._topic = topic
        self._positional = start_offset is not None
        self._position = start_offset if start_offset is not None else 0
        return self._consume(topic)

    @staticmethod
    def _assignment(topic: str, partitions: Iterable[int] | None) -> frozenset[tuple[str, int]]:
        """The `(topic, partition)` pairs this subscription claims."""
        if partitions is None:
            return frozenset({(topic, _PARTITION)})
        requested = static_partitions(topic, partitions)
        if requested != {_PARTITION}:
            raise ValueError(
                f"InMemoryBus has one partition per topic; cannot statically "
                f"assign {sorted(requested)} of {topic!r}"
            )
        return frozenset({(topic, _PARTITION)})

    async def _consume(self, topic: str) -> AsyncIterator[ConsumedMessage]:
        while True:
            offset = self._position
            record = await self._bus._wait_for(topic, offset, lambda: self._closed)
            if record is None:
                return
            self._position = offset + 1
            if not self._positional:
                if self._bus._is_acked(topic, self._group_id, offset):
                    continue
                self._unacked.add(offset)
            yield ConsumedMessage(
                topic=topic,
                partition=_PARTITION,
                offset=offset,
                key=record.key,
                value=record.value,
            )

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        """Acknowledge exactly `messages` for this consumer's group.

        The whole iterable is checked before any offset is recorded, so a
        rejected call acknowledges nothing. A message named more than once
        in one call is acknowledged once.
        """
        if self._closed:
            raise ValueError("cannot ack: this consumer is closed")
        if self._positional:
            raise ValueError("cannot ack on a positional subscription (start_offset was given)")
        batch = list(messages)
        if not batch:
            return
        offsets: set[int] = set()
        for message in batch:
            if (
                message.topic != self._topic
                or message.partition != _PARTITION
                or message.offset not in self._unacked
            ):
                raise ValueError(
                    f"cannot ack {message.topic!r} partition {message.partition} offset "
                    f"{message.offset}: not delivered by this consumer under its durable "
                    f"subscription, or already acknowledged"
                )
            offsets.add(message.offset)
        assert self._topic is not None
        self._bus._mark_acked(self._topic, self._group_id, offsets)
        self._unacked -= offsets

    async def close(self) -> None:
        """Stop delivery and end the iterator; idempotent, safe before `subscribe()`.

        Nothing is sent anywhere: an unacknowledged message is already
        deliverable to the next consumer for the group.
        """
        if self._closed:
            return
        self._closed = True
        self._unacked.clear()
        await self._bus._wake()
