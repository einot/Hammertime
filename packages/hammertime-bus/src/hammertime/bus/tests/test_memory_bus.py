"""In-memory Producer/Consumer bus: ordering, consumer groups, seek, commit.

Spec: section 19 (event-driven internal architecture), section 32/33
(durable event log / replayability -- offsets and commit positions are how
recovery-after-crash and replay are modeled).

`interface.py` defines `Producer`/`Consumer` as async `Protocol`s:
`Producer.publish(topic, key, value)` / `flush()`, and
`Consumer.subscribe(topic) -> AsyncIterator[ConsumedMessage]` / `seek(topic,
partition, offset)` / `commit()` -- consumption is pull-by-iteration (`async
for message in await consumer.subscribe(topic)`), not polling; this mirrors
`kafka.py`'s real `aiokafka.AIOKafkaConsumer`, which is itself an async
iterator, so both backends stay interchangeable behind the same interface.
`memory.py`'s in-process `InMemoryBus` is the shared broker: one append-only
log per topic, with each consumer group tracking its own read position;
`commit()` persists that position so a fresh `Consumer` for the same group
resumes rather than re-reading from the start of the log.

`Consumer.commit` also takes an optional explicit mapping (ADR-0011
decision 1, as Amendment 6 item A20 rewrote it):

    async def commit(self, offsets: Mapping[tuple[str, int], int] | None = None) -> None

With `offsets`, exactly the given `(topic, partition) -> next offset to
read` pairs are committed -- those partitions only, at those offsets,
nothing else; an empty mapping is a no-op that returns normally; a
partition this consumer does not hold is a `ValueError` from
`MemoryConsumer` (a partition other than `0`, or a topic it has not
subscribed). `commit()` with no argument keeps its original meaning, the
*consumed* position, which is what every other test in this file uses.
`TestCommitWithExplicitOffsets` below is written from that bullet alone.

Since a live `subscribe()` iterator blocks waiting for the next message
(matching real broker semantics), these tests only ever read exactly as
many messages as were published, then stop -- they never iterate past the
end of a bounded publish burst.
"""

from __future__ import annotations

import pytest
from hammertime.bus.interface import ConsumedMessage, Consumer
from hammertime.bus.memory import InMemoryBus

TOPIC = "test.topic.v1"
# A topic no consumer below ever subscribes to: the second half of A20's
# "a partition this consumer does not hold is an error".
OTHER_TOPIC = "test.other.v1"


async def _read_n(bus: InMemoryBus, group_id: str, topic: str, n: int) -> list[ConsumedMessage]:
    """Read exactly `n` messages from `topic` as `group_id`, then stop."""
    consumer = bus.consumer(group_id)
    messages = []
    async for message in await consumer.subscribe(topic):
        messages.append(message)
        if len(messages) == n:
            break
    return messages


class TestOrderingForASingleConsumer:
    async def test_publish_then_subscribe_delivers_messages_in_publish_order(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")
        await producer.publish(TOPIC, key="k3", value=b"third")

        received = await _read_n(bus, "single-consumer", TOPIC, 3)

        assert [message.value for message in received] == [b"first", b"second", b"third"]


class TestIndependentConsumerGroups:
    async def test_two_consumer_groups_each_see_the_full_log_independently(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")

        # Fully drain group A first.
        a_messages = await _read_n(bus, "group-a", TOPIC, 2)

        # Group B's view must be unaffected by group A having consumed
        # everything -- it independently sees the full log from the start.
        b_messages = await _read_n(bus, "group-b", TOPIC, 2)

        assert [m.value for m in a_messages] == [b"first", b"second"]
        assert [m.value for m in b_messages] == [b"first", b"second"]


class TestSeek:
    async def test_seek_repositions_the_consumer_to_a_given_offset(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")
        await producer.publish(TOPIC, key="k3", value=b"third")

        # Learn offsets by reading the log, rather than assuming 0-based
        # numbering.
        probed = await _read_n(bus, "probe", TOPIC, 3)
        third = probed[2]

        consumer = bus.consumer("seeker")
        await consumer.subscribe(TOPIC)
        await consumer.seek(TOPIC, third.partition, third.offset)

        replayed = []
        async for message in await consumer.subscribe(TOPIC):
            replayed.append(message)
            break

        assert replayed[0].offset == third.offset
        assert replayed[0].value == b"third"

    async def test_seek_backwards_allows_replaying_already_read_messages(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")

        # One consumer instance throughout: its in-memory read position
        # (not yet committed anywhere) is what seek() rewinds.
        consumer = bus.consumer("replayer")
        read_so_far = []
        async for message in await consumer.subscribe(TOPIC):
            read_so_far.append(message)
            if len(read_so_far) == 2:  # consumed "first" and "second"
                break
        first = read_so_far[0]

        await consumer.seek(TOPIC, first.partition, first.offset)

        replayed = []
        async for message in await consumer.subscribe(TOPIC):
            replayed.append(message)
            break

        assert replayed[0].value == b"first"


class TestCommitAndReconnect:
    async def test_new_consumer_in_a_fresh_group_starts_from_the_beginning(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")

        messages = await _read_n(bus, "never-committed-before", TOPIC, 1)

        assert messages[0].value == b"first"

    async def test_commit_persists_position_so_a_new_consumer_instance_resumes_after_it(
        self,
    ) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")
        await producer.publish(TOPIC, key="k3", value=b"third")

        group = "aggregator-shard-0"
        consumer = bus.consumer(group)
        first_seen = []
        async for message in await consumer.subscribe(TOPIC):
            first_seen.append(message)
            break
        assert first_seen[0].value == b"first"
        await consumer.commit()

        # Simulate reconnect-after-crash: a brand new Consumer instance,
        # same consumer group, same topic.
        reconnected = bus.consumer(group)
        resumed = []
        async for message in await reconnected.subscribe(TOPIC):
            resumed.append(message)
            break

        assert resumed[0].value == b"second"  # not "first" again, and not the start of the log

    async def test_uncommitted_progress_is_not_persisted_across_new_instances(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")

        group = "no-commit-shard"
        consumer = bus.consumer(group)
        async for _message in await consumer.subscribe(TOPIC):
            break  # read "first" but never commit

        reconnected = bus.consumer(group)
        resumed = []
        async for message in await reconnected.subscribe(TOPIC):
            resumed.append(message)
            break

        assert resumed[0].value == b"first"  # crash before commit -> redelivered from the start


class TestCommitWithExplicitOffsets:
    """ADR-0011 decision 1's `commit(offsets)` bullet, added by Amendment 6
    item A20.

    A commit may now name the position it commits, so a consumer can commit
    the position after the last message it *handled* instead of the one after
    the last message it *fetched*. The aggregator needs exactly that: a
    message fetched and not yet handled when a claim is revoked must stay in
    the log for the partition's next owner (A20). Offsets are the "next offset
    to read", i.e. the last handled `offset + 1`.

    Offsets are learned by reading the log rather than assumed to be 0-based,
    the way `TestSeek` above already does.
    """

    async def _publish_three(self, bus: InMemoryBus) -> None:
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")
        await producer.publish(TOPIC, key="k3", value=b"third")

    async def _drain(self, consumer: Consumer, topic: str, n: int) -> list[ConsumedMessage]:
        """Read exactly `n` messages on an *existing* consumer, then stop."""

        read: list[ConsumedMessage] = []
        async for message in await consumer.subscribe(topic):
            read.append(message)
            if len(read) == n:
                break
        return read

    async def test_an_explicit_offset_is_what_a_fresh_consumer_resumes_at(self) -> None:
        # "exactly the pairs given -- those partitions only, at those offsets":
        # the committing consumer had read the whole log, and the commit still
        # names only the position after the first message.
        bus = InMemoryBus()
        await self._publish_three(bus)
        group = "explicit-offset"
        consumer = bus.consumer(group)
        read = await self._drain(consumer, TOPIC, 3)

        await consumer.commit({(TOPIC, read[0].partition): read[0].offset + 1})

        resumed = await _read_n(bus, group, TOPIC, 1)
        assert resumed[0].value == b"second"

    async def test_an_empty_mapping_commits_nothing(self) -> None:
        # "An empty mapping is a no-op that returns normally": the position
        # the previous commit left stands, even though the consumer has read
        # further since.
        bus = InMemoryBus()
        await self._publish_three(bus)
        group = "empty-mapping"
        consumer = bus.consumer(group)
        await self._drain(consumer, TOPIC, 1)
        await consumer.commit()  # committed: after "first"
        await self._drain(consumer, TOPIC, 2)  # read "second" and "third"

        await consumer.commit({})

        resumed = await _read_n(bus, group, TOPIC, 1)
        assert resumed[0].value == b"second"

    async def test_a_partition_other_than_zero_is_a_value_error(self) -> None:
        # `InMemoryBus` has one partition per topic, so partition 1 is a
        # partition this consumer does not hold.
        bus = InMemoryBus()
        await self._publish_three(bus)
        consumer = bus.consumer("bad-partition")
        read = await self._drain(consumer, TOPIC, 1)

        with pytest.raises(ValueError):
            await consumer.commit({(TOPIC, 1): read[0].offset + 1})

    async def test_a_topic_this_consumer_has_not_subscribed_is_a_value_error(self) -> None:
        bus = InMemoryBus()
        await self._publish_three(bus)
        consumer = bus.consumer("bad-topic")
        read = await self._drain(consumer, TOPIC, 1)

        with pytest.raises(ValueError):
            await consumer.commit({(OTHER_TOPIC, 0): read[0].offset + 1})

    async def test_a_rejected_commit_commits_nothing(self) -> None:
        # "raise `ValueError` and commit nothing": the group's committed
        # position is the one the last accepted commit left.
        bus = InMemoryBus()
        await self._publish_three(bus)
        group = "rejected-commit"
        consumer = bus.consumer(group)
        read = await self._drain(consumer, TOPIC, 2)
        await consumer.commit({(TOPIC, read[0].partition): read[0].offset + 1})

        with pytest.raises(ValueError):
            await consumer.commit({(TOPIC, 1): read[1].offset + 1})

        resumed = await _read_n(bus, group, TOPIC, 1)
        assert resumed[0].value == b"second"

    async def test_commit_with_no_argument_still_commits_the_consumed_position(self) -> None:
        # The `None` half of the same bullet: unchanged, and still the
        # position after every message this consumer has been handed.
        bus = InMemoryBus()
        await self._publish_three(bus)
        group = "bare-commit"
        consumer = bus.consumer(group)
        await self._drain(consumer, TOPIC, 2)

        await consumer.commit()

        resumed = await _read_n(bus, group, TOPIC, 1)
        assert resumed[0].value == b"third"
