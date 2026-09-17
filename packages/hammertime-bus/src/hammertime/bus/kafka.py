"""Kafka/Redpanda transport. Partition key = IP shard so per-IP ordering holds.

Spec: section 20, section 32

Structural implementations of `hammertime.bus.interface.Producer`/`Consumer`
on top of aiokafka. Exercising this against a real broker is epic #17's job;
there are no unit tests here by design.
"""

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.abc import ConsumerRebalanceListener
from hammertime.bus.interface import AssignmentListener, ConsumedMessage, static_partitions


def _shard_claims(partitions: Iterable[Any]) -> frozenset[tuple[str, int]]:
    """aiokafka `TopicPartition`s as the `(topic, partition)` pairs we hand out."""
    return frozenset((tp.topic, tp.partition) for tp in partitions)


class _RebalanceAdapter(ConsumerRebalanceListener):  # type: ignore[misc]
    """Drives an `AssignmentListener` from aiokafka's rebalance callbacks.

    aiokafka checks `isinstance(listener, ConsumerRebalanceListener)` before
    accepting it, so the adapter really does subclass the ABC (which is
    untyped -- hence the `misc` ignore, see the `aiokafka.*` mypy override).
    Its callbacks may be coroutines, which is what lets `AssignmentListener`'s
    coroutines be awaited from inside a rebalance.

    `first_assignment` is set once aiokafka has delivered an assignment,
    empty or not, so `subscribe()` can block until the group's initial claim
    is held (ADR-0011 decision 1).
    """

    def __init__(self, listener: AssignmentListener | None) -> None:
        self.listener = listener
        self.first_assignment = asyncio.Event()

    async def on_partitions_revoked(self, revoked: Iterable[Any]) -> None:
        if self.listener is not None:
            await self.listener.on_revoked(_shard_claims(revoked))

    async def on_partitions_assigned(self, assigned: Iterable[Any]) -> None:
        if self.listener is not None:
            await self.listener.on_assigned(_shard_claims(assigned))
        self.first_assignment.set()


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
        self._static_assignment: set[Any] = set()
        self._adapter: _RebalanceAdapter | None = None
        self._listener: AssignmentListener | None = None

    async def start(self) -> None:
        """Connect to the cluster and join `group_id`, subscribed to nothing yet.

        The client is deliberately constructed without topics: which
        partitions this member owns is decided by `subscribe()`, either by
        the group coordinator or statically (ADR-0011 decision 1), and
        aiokafka rejects `assign()` on a consumer already subscribed.
        """
        if self._client is not None:
            return
        self._client = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset=self._auto_offset_reset,
            **self._client_kwargs,
        )
        await self._client.start()

    async def stop(self) -> None:
        """Leave the group and disconnect."""
        if self._client is not None:
            await self._client.stop()
            self._client = None
            self._subscribed_topics = set()
            self._static_assignment = set()
            self._adapter = None
            self._listener = None

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        """Claim partitions of `topic`, tell `listener`, then start yielding.

        Calling this again with a topic not already subscribed extends the
        subscription rather than ignoring it -- matching `memory.py`'s
        `MemoryConsumer`, which tracks an independent position per topic on
        one instance (`interface.py`: both backends must be interchangeable).

        With `partitions=None` the group coordinator assigns; with an explicit
        set this member calls `assign()` and owns exactly those partitions,
        while still committing offsets under `group_id`. aiokafka refuses to
        mix the two on one client, which is the per-process form of ADR-0011
        decision 1's "MUST NOT mix static and group-managed members".

        An empty static set raises `ValueError` before the client is even
        started, so no `assign([])` is ever issued and no listener is called
        (ADR-0011 amendment 1, item A3). A group-managed member the
        coordinator happens to give nothing is untouched by that rule: it
        still becomes ready, because a later rebalance can hand it shards.

        When a listener is in force, this coroutine waits for the initial
        assignment before returning, so a caller that has finished
        `subscribe()` is holding its shard claims (spec section 47 readiness).
        """
        # Validated before `start()`: a bad static set must not reach a broker.
        claimed = None if partitions is None else static_partitions(topic, partitions)
        await self.start()
        if listener is not None:
            self._listener = listener
        if claimed is None:
            await self._subscribe_group_managed(topic)
        else:
            await self._assign_statically(topic, claimed)
        return self._consume()

    async def _subscribe_group_managed(self, topic: str) -> None:
        """Join/extend the group subscription and wait for the first assignment."""
        client = self._require_client()
        already_registered = (
            topic in self._subscribed_topics
            and self._adapter is not None
            and self._adapter.listener is self._listener
        )
        if already_registered:
            return
        self._subscribed_topics.add(topic)
        # `subscribe()` replaces any listener registered by a previous call,
        # so the adapter (and its "assignment delivered" event) is rebuilt
        # here and the rebalance it triggers re-announces every claim.
        adapter = _RebalanceAdapter(self._listener)
        self._adapter = adapter
        client.subscribe(topics=sorted(self._subscribed_topics), listener=adapter)
        if self._listener is not None:
            await adapter.first_assignment.wait()

    async def _assign_statically(self, topic: str, partitions: frozenset[int]) -> None:
        """Take `partitions` of `topic` directly and announce them ourselves.

        `partitions` has already been through `static_partitions`, so it is
        never empty and `assign()` is never called with nothing.
        """
        client = self._require_client()
        claimed = {TopicPartition(topic, partition) for partition in partitions}
        self._static_assignment |= claimed
        client.assign(sorted(self._static_assignment))
        # aiokafka never runs a rebalance listener for a manual assignment,
        # so the single `on_assigned` call is synthesised here -- before
        # `subscribe()` returns, exactly as in the group-managed path.
        if self._listener is not None:
            await self._listener.on_assigned(_shard_claims(claimed))

    def _require_client(self) -> AIOKafkaConsumer:
        client = self._client
        if client is None:  # pragma: no cover - guarded by start()
            raise RuntimeError("KafkaConsumer is not started")
        return client

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

    async def commit(self, offsets: Mapping[tuple[str, int], int] | None = None) -> None:
        """Commit this group's offsets, either the consumed ones or `offsets`.

        With no argument aiokafka commits the consumed position of every
        assigned partition -- unchanged behaviour.

        With `offsets` exactly the given `(topic, partition) -> next offset to
        read` pairs are committed. aiokafka's explicit-commit convention is
        the same one: the value is the offset of the next record to read, the
        last handled `offset + 1` (ADR-0011 amendment 6, item A20). A
        partition this consumer is not assigned raises aiokafka's
        `IllegalStateError`, which is left to propagate.

        An empty mapping returns before the client is touched at all, so no
        commit request reaches the broker and an unstarted consumer is not an
        error -- there is nothing to commit and nothing to fail on.
        """
        if offsets is not None and not offsets:
            return
        if self._client is None:
            raise RuntimeError("KafkaConsumer.commit() requires an active subscription")
        if offsets is None:
            await self._client.commit()
            return
        await self._client.commit(
            {
                TopicPartition(topic, partition): offset
                for (topic, partition), offset in offsets.items()
            }
        )
