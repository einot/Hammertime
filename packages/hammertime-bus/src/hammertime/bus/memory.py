"""In-process transport for unit and property tests; same ordering guarantees.

Spec: section 19

`InMemoryBus` is the shared broker: one append-only log per topic, held in
process memory. `MemoryProducer`/`MemoryConsumer` are thin views onto it that
implement `hammertime.bus.interface.Producer`/`Consumer`, so tests can swap a
real Kafka transport for this one without changing calling code.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass

from hammertime.bus.interface import ConsumedMessage


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
    dedup, and replay semantics without needing a real partitioned log for
    unit tests. `topics.py`'s partition counts describe the Kafka-backed
    deployment, not this in-memory one.
    """

    def __init__(self) -> None:
        self._logs: dict[str, list[_Record]] = defaultdict(list)
        # (topic, group_id) -> committed next-offset-to-read
        self._committed: dict[tuple[str, str], int] = {}
        self._condition = asyncio.Condition()

    def producer(self) -> MemoryProducer:
        """A new producer view onto this bus."""
        return MemoryProducer(self)

    def consumer(self, group_id: str) -> MemoryConsumer:
        """A new consumer view onto this bus, resuming `group_id`'s committed position."""
        return MemoryConsumer(self, group_id)

    async def _append(self, topic: str, key: bytes | None, value: bytes) -> None:
        async with self._condition:
            self._logs[topic].append(_Record(key=key, value=value))
            self._condition.notify_all()

    async def _read_at(self, topic: str, offset: int) -> _Record:
        """Block until offset `offset` exists in `topic`'s log, then return it."""
        async with self._condition:
            while offset >= len(self._logs[topic]):
                await self._condition.wait()
            return self._logs[topic][offset]

    def _committed_offset(self, topic: str, group_id: str) -> int:
        return self._committed.get((topic, group_id), 0)

    def _set_committed_offset(self, topic: str, group_id: str, offset: int) -> None:
        self._committed[(topic, group_id)] = offset


class MemoryProducer:
    """`Producer` writing into an `InMemoryBus`."""

    def __init__(self, bus: InMemoryBus) -> None:
        self._bus = bus

    async def publish(self, topic: str, key: bytes | str, value: bytes) -> None:
        raw_key = key.encode("utf-8") if isinstance(key, str) else key
        await self._bus._append(topic, raw_key, value)

    async def flush(self) -> None:
        """No-op: `publish` already appends synchronously within the event loop."""
        return None


class MemoryConsumer:
    """`Consumer` reading from an `InMemoryBus` as member of `group_id`.

    Each topic's read position is tracked independently per `group_id`
    (spec section 19); `commit()` writes the position back to the shared bus
    so a `MemoryConsumer` constructed later for the same `group_id` resumes
    rather than re-reading from the start of the log (spec section 23,
    ADR-0003).
    """

    def __init__(self, bus: InMemoryBus, group_id: str) -> None:
        self._bus = bus
        self._group_id = group_id
        self._positions: dict[str, int] = {}

    def _position(self, topic: str) -> int:
        if topic not in self._positions:
            self._positions[topic] = self._bus._committed_offset(topic, self._group_id)
        return self._positions[topic]

    async def subscribe(self, topic: str) -> AsyncIterator[ConsumedMessage]:
        return self._consume(topic)

    async def _consume(self, topic: str) -> AsyncIterator[ConsumedMessage]:
        while True:
            offset = self._position(topic)
            record = await self._bus._read_at(topic, offset)
            self._positions[topic] = offset + 1
            yield ConsumedMessage(
                topic=topic,
                partition=0,
                offset=offset,
                key=record.key,
                value=record.value,
            )

    async def seek(self, topic: str, partition: int, offset: int) -> None:
        # The in-memory bus keeps one partition (0) per topic; `partition` is
        # accepted for interface parity with the Kafka-backed consumer.
        del partition
        self._positions[topic] = offset

    async def commit(self) -> None:
        for topic, offset in self._positions.items():
            self._bus._set_committed_offset(topic, self._group_id, offset)
