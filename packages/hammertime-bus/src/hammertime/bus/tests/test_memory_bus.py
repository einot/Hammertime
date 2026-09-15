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

Since a live `subscribe()` iterator blocks waiting for the next message
(matching real broker semantics), these tests only ever read exactly as
many messages as were published, then stop -- they never iterate past the
end of a bounded publish burst.
"""

from __future__ import annotations

from hammertime.bus.interface import ConsumedMessage
from hammertime.bus.memory import InMemoryBus

TOPIC = "test.topic.v1"


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
