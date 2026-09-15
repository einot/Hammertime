"""Kafka/Redpanda transport. Partition key = IP shard so per-IP ordering holds.

Spec: section 20, section 32

Structural implementations of `hammertime.bus.interface.Producer`/`Consumer`
on top of aiokafka. Exercising this against a real broker is epic #17's job;
there are no unit tests here by design.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from hammertime.bus.interface import ConsumedMessage


class KafkaProducer:
    """`Producer` backed by `aiokafka.AIOKafkaProducer`.

    Spec section 20 requires stable per-IP partition ownership; callers pass
    a key already derived via `topics.TopicSpec.key_selector`, and aiokafka's
    default partitioner hashes that key to a partition the same way on every
    producer instance.
    """

    def __init__(self, *, bootstrap_servers: str, **client_kwargs: Any) -> None:
        self._client = AIOKafkaProducer(bootstrap_servers=bootstrap_servers, **client_kwargs)
        self._started = False

    async def start(self) -> None:
        """Connect to the cluster. Must be called before `publish`/`flush`."""
        if not self._started:
            await self._client.start()
            self._started = True

    async def stop(self) -> None:
        """Disconnect from the cluster, flushing any buffered messages first."""
        if self._started:
            await self._client.stop()
            self._started = False

    async def publish(self, topic: str, key: bytes | str, value: bytes) -> None:
        raw_key = key.encode("utf-8") if isinstance(key, str) else key
        await self._client.send_and_wait(topic, value, key=raw_key)

    async def flush(self) -> None:
        await self._client.flush()


class KafkaConsumer:
    """`Consumer` backed by `aiokafka.AIOKafkaConsumer`.

    One `KafkaConsumer` is one member of `group_id`; the broker persists
    committed offsets keyed by `(group_id, topic, partition)`, which is what
    lets a new `KafkaConsumer` for the same group resume instead of
    re-reading from the start of the log (spec section 23, ADR-0003).

    Auto-commit is disabled: callers commit explicitly, after they have
    finished acting on a message, so an at-least-once consumer never marks a
    message as done before it is safe to redeliver on crash.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        group_id: str,
        auto_offset_reset: str = "earliest",
        **client_kwargs: Any,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._group_id = group_id
        self._auto_offset_reset = auto_offset_reset
        self._client_kwargs = client_kwargs
        self._client: AIOKafkaConsumer | None = None
        self._subscribed_topics: set[str] = set()

    async def start(self, *topics: str) -> None:
        """Connect to the cluster and join `group_id`, subscribed to `topics`."""
        self._client = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset=self._auto_offset_reset,
            **self._client_kwargs,
        )
        self._subscribed_topics = set(topics)
        await self._client.start()

    async def stop(self) -> None:
        """Leave the group and disconnect."""
        if self._client is not None:
            await self._client.stop()
            self._client = None
            self._subscribed_topics = set()

    async def subscribe(self, topic: str) -> AsyncIterator[ConsumedMessage]:
        """Add `topic` to this consumer's subscription and start yielding from it.

        Calling this again with a topic not already subscribed extends the
        subscription rather than ignoring it -- matching `memory.py`'s
        `MemoryConsumer`, which tracks an independent position per topic on
        one instance (`interface.py`: both backends must be interchangeable).
        """
        if self._client is None:
            await self.start(topic)
        elif topic not in self._subscribed_topics:
            self._subscribed_topics.add(topic)
            self._client.subscribe(topics=list(self._subscribed_topics))
        return self._consume()

    async def _consume(self) -> AsyncIterator[ConsumedMessage]:
        client = self._client
        if client is None:  # pragma: no cover - guarded by subscribe()
            raise RuntimeError("KafkaConsumer is not started")
        async for record in client:
            yield ConsumedMessage(
                topic=record.topic,
                partition=record.partition,
                offset=record.offset,
                key=record.key,
                value=record.value,
            )

    async def seek(self, topic: str, partition: int, offset: int) -> None:
        if self._client is None:
            raise RuntimeError("KafkaConsumer.seek() requires an active subscription")
        self._client.seek(TopicPartition(topic, partition), offset)

    async def commit(self) -> None:
        if self._client is None:
            raise RuntimeError("KafkaConsumer.commit() requires an active subscription")
        await self._client.commit()
