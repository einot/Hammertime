"""In-memory Producer/Consumer bus: dedup by id, acknowledgements, positional reads, close.

Spec: section 19 (event-driven internal architecture), section 32/33
(durable event log / replayability -- the acknowledged position and the
stream sequence are how recovery-after-crash and replay are modeled).

ADR-0013 decision 3 is the interface this file is written against, and
nothing else: `test-author` "writes the bus package's tests from this block
and the semantics below alone". What that block pins, and is asserted here:

* `Producer.publish(topic, key, value, *, message_id=None)`: with a
  `message_id` the log deduplicates -- "a second publish to the same topic
  carrying an id the log has seen inside its duplicate window is acknowledged
  and **not** appended, and `publish` returns normally"; "with
  `message_id=None` no deduplication is attempted" (assumption 22).
  `hammertime.bus.memory` "deduplicates by `message_id` per topic for the
  lifetime of the `InMemoryBus` (an unbounded duplicate window)".
* `Producer.flush()` is "a no-op in both implementations".
* `Consumer.subscribe(...)` is "exactly one call per `Consumer` instance; a
  second call raises `RuntimeError`" (assumption 23). Its iterator "ends
  (raises `StopAsyncIteration`) after `close()`".
* `start_offset=None` is a durable subscription: "A fresh `Consumer` for the
  same group and partitions is delivered every message that was never
  acknowledged, in order, however many earlier consumers read it."
  `start_offset=<int>` is positional: "delivery starts at the first message
  whose `offset >= start_offset` (at the first retained message if that
  sequence is no longer in the log), no position is kept anywhere, and
  `ack()` on it is a `ValueError`".
* `Consumer.ack(messages)` "acknowledges exactly the given messages, each of
  which MUST have been delivered by this consumer instance under its durable
  subscription and not yet acknowledged; anything else -- a message of another
  topic or partition, one this instance never delivered, one it already
  acknowledged, or any message on a positional subscription -- is a
  `ValueError` ..., raised before anything is acknowledged (the whole
  iterable is checked first, so a rejected call acknowledges nothing). An
  empty iterable returns normally without touching the broker." (assumption
  24). "After `close()`, `ack()` is a `ValueError`."
* `Consumer.close()` "stops delivery, makes the iterator end, and gives back
  every message this instance delivered under a durable subscription and did
  not acknowledge, so that the next consumer for the group receives it";
  "`close()` is idempotent and safe before `subscribe()`".
* `ConsumedMessage` "keeps its five fields ... and gains `delivery_count`
  (default 1)"; "The dataclass stays frozen and `slots=True`, and every
  existing construction with the five keyword fields remains valid";
  "`delivery_count` is always 1" on `InMemoryBus`; `partition` is "the
  integer suffix of the subject the message is stored under" and
  `InMemoryBus` "implements the same contract with one partition per topic".
* "Removed: `Consumer.seek`, `Consumer.commit`". `MessageBus` is a
  `runtime_checkable` Protocol in `hammertime.bus.interface` ("moved here
  from `hammertime.aggregator.worker`"), with `producer()` and
  `consumer(group_id)`.

ASSUMPTIONS -- things decision 3 implies but does not spell out. Adjust the
helper, not the meaning of the assertion:

1. `ConsumedMessage.offset` on `InMemoryBus` is "the log index", so two
   consecutive appends have consecutive offsets; that is how "not appended"
   is made observable for a deduplicated publish
   (`test_the_deduplicated_publish_leaves_no_gap_in_the_log`). A bounded
   read is otherwise used: a sentinel with a fresh id is published after the
   duplicate, and exactly as many messages as should exist are read.
2. "`start_offset` below the first offset delivers from the start" is
   exercised with `first.offset - 1`, which on a 0-based log is `-1`. The ADR
   pins only "the first message whose `offset >= start_offset`"; a negative
   `start_offset` satisfies that literally for every message. If the
   implementation rejects negatives, that is a gap for the architect, not a
   reason to bend this test.
3. A positional subscription keeps yielding messages published after it
   was opened (it "ends ... after `close()`", not at the log end); the trie's
   replay-then-tail shape (decision 9) depends on it.
4. Equal `ConsumedMessage` values are indistinguishable (frozen dataclass
   equality), so "one this instance never delivered" is tested with a message
   the instance genuinely never received, not with another instance's copy of
   one it did.
5. `asyncio.wait_for(..., timeout=1.0)` in the blocked-iterator `close()`
   test is the only wall-clock bound in this file, and it is reached only when
   the test fails (a `close()` that does not wake a waiting iterator).

Since a live `subscribe()` iterator blocks waiting for the next message
(matching real broker semantics), these tests only ever read exactly as
many messages as were published, then stop -- they never iterate past the
end of a bounded publish burst, except after `close()`, where the iterator
must end on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from hammertime.bus.interface import ConsumedMessage, Consumer, MessageBus, Producer
from hammertime.bus.memory import InMemoryBus

TOPIC = "test.topic.v1"
# A topic no consumer below ever subscribes to: the "another topic" half of
# decision 3's `ack()` rejection list.
OTHER_TOPIC = "test.other.v1"
GROUP = "test-group"


async def _take(stream: AsyncIterator[ConsumedMessage], n: int) -> list[ConsumedMessage]:
    """Read exactly `n` messages from a live subscription, then stop."""

    messages: list[ConsumedMessage] = []
    async for message in stream:
        messages.append(message)
        if len(messages) == n:
            break
    return messages


async def _read_group(
    bus: InMemoryBus, group_id: str, topic: str, n: int
) -> list[ConsumedMessage]:
    """Read exactly `n` messages from `topic` on a *fresh* consumer for `group_id`."""

    consumer = bus.consumer(group_id)
    return await _take(await consumer.subscribe(topic), n)


async def _publish_three(bus: InMemoryBus, topic: str = TOPIC) -> None:
    producer = bus.producer()
    await producer.publish(topic, key="k1", value=b"first", message_id="id-1")
    await producer.publish(topic, key="k2", value=b"second", message_id="id-2")
    await producer.publish(topic, key="k3", value=b"third", message_id="id-3")


class TestOrderingForASingleConsumer:
    async def test_publish_then_subscribe_delivers_messages_in_publish_order(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)

        received = await _read_group(bus, "single-consumer", TOPIC, 3)

        assert [message.value for message in received] == [b"first", b"second", b"third"]

    async def test_offsets_are_strictly_increasing_in_delivery_order(self) -> None:
        # Decision 3: `offset` is "unique and strictly increasing across the
        # whole topic".
        bus = InMemoryBus()
        await _publish_three(bus)

        received = await _read_group(bus, "single-consumer", TOPIC, 3)

        offsets = [message.offset for message in received]
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == 3

    async def test_a_message_published_after_subscribing_is_delivered_too(self) -> None:
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC)

        await bus.producer().publish(TOPIC, key="k1", value=b"late", message_id="id-late")

        assert [message.value for message in await _take(stream, 1)] == [b"late"]


class TestIndependentConsumerGroups:
    async def test_two_consumer_groups_each_see_the_full_log_independently(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)

        # Fully drain and acknowledge group A first.
        consumer_a = bus.consumer("group-a")
        a_messages = await _take(await consumer_a.subscribe(TOPIC), 3)
        await consumer_a.ack(a_messages)

        # Group B's view must be unaffected by group A having acknowledged
        # everything -- it independently sees the full log from the start.
        b_messages = await _read_group(bus, "group-b", TOPIC, 3)

        assert [m.value for m in a_messages] == [b"first", b"second", b"third"]
        assert [m.value for m in b_messages] == [b"first", b"second", b"third"]


class TestPublishDeduplicatesByMessageId:
    """Decision 3, `Producer.publish`: with a `message_id` "a second publish to
    the same topic carrying an id the log has seen ... is acknowledged and
    **not** appended, and `publish` returns normally"; `InMemoryBus` keeps an
    unbounded duplicate window. Decision 4: that is what closes #88."""

    async def test_the_same_id_twice_appends_once(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")
        await producer.publish(TOPIC, key="k2", value=b"sentinel", message_id="event-2")

        received = await _read_group(bus, GROUP, TOPIC, 2)

        # ASSUMPTION 1: the sentinel bounds the read; if the duplicate had
        # been appended it would come second and the sentinel third.
        assert [message.value for message in received] == [b"first", b"sentinel"]

    async def test_the_deduplicated_publish_leaves_no_gap_in_the_log(self) -> None:
        # ASSUMPTION 1: offsets are the log index, so "not appended" means
        # the sentinel sits directly after the first copy.
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")
        await producer.publish(TOPIC, key="k2", value=b"sentinel", message_id="event-2")

        first, sentinel = await _read_group(bus, GROUP, TOPIC, 2)

        assert sentinel.offset == first.offset + 1

    async def test_the_duplicate_is_dropped_by_id_not_by_body(self) -> None:
        # "we only consult the message ID not the body" (ADR-0013 Sources,
        # model_deep_dive): a different payload under a seen id is still a
        # duplicate.
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")
        await producer.publish(TOPIC, key="k1", value=b"different", message_id="event-1")
        await producer.publish(TOPIC, key="k2", value=b"sentinel", message_id="event-2")

        received = await _read_group(bus, GROUP, TOPIC, 2)

        assert [message.value for message in received] == [b"first", b"sentinel"]

    async def test_different_ids_append_twice(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"same", message_id="event-1")
        await producer.publish(TOPIC, key="k1", value=b"same", message_id="event-2")

        received = await _read_group(bus, GROUP, TOPIC, 2)

        assert [message.value for message in received] == [b"same", b"same"]

    async def test_message_id_none_never_deduplicates(self) -> None:
        # Assumption 22: "silent dedup of identical bytes is a surprise for a
        # test that publishes the same payload twice on purpose."
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"same", message_id=None)
        await producer.publish(TOPIC, key="k1", value=b"same", message_id=None)
        await producer.publish(TOPIC, key="k1", value=b"same")

        received = await _read_group(bus, GROUP, TOPIC, 3)

        assert [message.value for message in received] == [b"same", b"same", b"same"]

    async def test_deduplication_is_per_topic(self) -> None:
        # "a second publish to the same topic": the same id on another topic
        # is that topic's first sighting of it.
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"on-topic", message_id="event-1")
        await producer.publish(OTHER_TOPIC, key="k1", value=b"on-other", message_id="event-1")

        on_topic = await _read_group(bus, GROUP, TOPIC, 1)
        on_other = await _read_group(bus, GROUP, OTHER_TOPIC, 1)

        assert [message.value for message in on_topic] == [b"on-topic"]
        assert [message.value for message in on_other] == [b"on-other"]

    async def test_the_duplicate_window_never_lapses_on_the_memory_bus(self) -> None:
        # "for the lifetime of the `InMemoryBus` (an unbounded duplicate
        # window -- a test never needs the window to lapse)".
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")
        for index in range(50):
            await producer.publish(TOPIC, key="k", value=b"filler", message_id=f"filler-{index}")
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")
        await producer.publish(TOPIC, key="k2", value=b"sentinel", message_id="event-2")

        received = await _read_group(bus, GROUP, TOPIC, 52)

        assert [message.value for message in received[:1]] == [b"first"]
        assert [message.value for message in received[1:51]] == [b"filler"] * 50
        assert received[51].value == b"sentinel"

    async def test_the_duplicate_publish_returns_normally(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")

        result = await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")

        assert result is None


class TestFlush:
    async def test_flush_is_a_no_op_that_returns_normally(self) -> None:
        # Decision 3: "a no-op in both implementations, retained because
        # ADR-0009 decision 7's flush-before-commit rule is stated against the
        # interface".
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first", message_id="event-1")

        assert await producer.flush() is None
        assert await producer.flush() is None

        assert [m.value for m in await _read_group(bus, GROUP, TOPIC, 1)] == [b"first"]


class TestOneSubscriptionPerConsumerInstance:
    """Decision 3 / assumption 23: "exactly one call per `Consumer` instance;
    a second call raises `RuntimeError`"."""

    async def test_a_second_subscribe_on_the_same_topic_is_a_runtime_error(self) -> None:
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)
        await consumer.subscribe(TOPIC)

        with pytest.raises(RuntimeError):
            await consumer.subscribe(TOPIC)

    async def test_a_second_subscribe_on_another_topic_is_a_runtime_error_too(self) -> None:
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)
        await consumer.subscribe(TOPIC)

        with pytest.raises(RuntimeError):
            await consumer.subscribe(OTHER_TOPIC)

    async def test_a_second_positional_subscribe_is_a_runtime_error_too(self) -> None:
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)
        await consumer.subscribe(TOPIC, start_offset=0)

        with pytest.raises(RuntimeError):
            await consumer.subscribe(TOPIC, start_offset=0)


class TestAcknowledgementIsThePosition:
    """Decision 3, `Consumer.ack`: "Acknowledging is what advances the group's
    position: it is the only 'commit' there is, and it is per message". A
    fresh consumer for the group "is delivered every message that was never
    acknowledged, in order, however many earlier consumers read it"."""

    async def test_a_new_consumer_in_a_fresh_group_starts_from_the_beginning(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)

        received = await _read_group(bus, "never-acked-before", TOPIC, 1)

        assert received[0].value == b"first"

    async def test_an_acknowledged_message_is_skipped_by_a_fresh_consumer(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        first = await _take(await consumer.subscribe(TOPIC), 1)

        await consumer.ack(first)

        resumed = await _read_group(bus, GROUP, TOPIC, 2)
        assert [message.value for message in resumed] == [b"second", b"third"]

    async def test_acknowledgement_is_per_message_not_a_position(self) -> None:
        # Acknowledging the *second* message alone leaves the first and third
        # for the next consumer -- an offset commit could not express this.
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 3)

        await consumer.ack([read[1]])

        resumed = await _read_group(bus, GROUP, TOPIC, 2)
        assert [message.value for message in resumed] == [b"first", b"third"]

    async def test_several_messages_can_be_acknowledged_in_one_call(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 3)

        await consumer.ack(read[:2])

        resumed = await _read_group(bus, GROUP, TOPIC, 1)
        assert resumed[0].value == b"third"

    async def test_unacknowledged_messages_are_redelivered_to_a_fresh_consumer_in_order(
        self,
    ) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        await _take(await consumer.subscribe(TOPIC), 3)  # read everything, ack nothing

        resumed = await _read_group(bus, GROUP, TOPIC, 3)

        assert [message.value for message in resumed] == [b"first", b"second", b"third"]

    async def test_however_many_earlier_consumers_read_them(self) -> None:
        # Three successive consumers of one group each read the log and
        # acknowledge nothing; the fourth still starts at the first message.
        bus = InMemoryBus()
        await _publish_three(bus)
        for _ in range(3):
            await _read_group(bus, GROUP, TOPIC, 3)

        resumed = await _read_group(bus, GROUP, TOPIC, 3)

        assert [message.value for message in resumed] == [b"first", b"second", b"third"]

    async def test_a_redelivered_message_keeps_its_offset(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        first_read = await _read_group(bus, GROUP, TOPIC, 3)

        resumed = await _read_group(bus, GROUP, TOPIC, 3)

        assert [m.offset for m in resumed] == [m.offset for m in first_read]

    async def test_an_empty_iterable_is_a_no_op(self) -> None:
        # "An empty iterable returns normally without touching the broker":
        # nothing becomes acknowledged.
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        await _take(await consumer.subscribe(TOPIC), 2)

        assert await consumer.ack([]) is None

        resumed = await _read_group(bus, GROUP, TOPIC, 1)
        assert resumed[0].value == b"first"

    async def test_acknowledgements_are_per_group(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer_a = bus.consumer("group-a")
        read = await _take(await consumer_a.subscribe(TOPIC), 3)
        await consumer_a.ack(read)

        b_messages = await _read_group(bus, "group-b", TOPIC, 3)

        assert [message.value for message in b_messages] == [b"first", b"second", b"third"]


class TestAcknowledgementRejections:
    """Decision 3 / assumption 24: the rejection list, each a `ValueError`
    "raised before anything is acknowledged (the whole iterable is checked
    first, so a rejected call acknowledges nothing)"."""

    async def test_a_message_this_instance_never_delivered_is_a_value_error(self) -> None:
        # ASSUMPTION 4: consumer A read only the first message; the second was
        # delivered to another group's consumer and never to A.
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer_a = bus.consumer(GROUP)
        await _take(await consumer_a.subscribe(TOPIC), 1)
        other = await _read_group(bus, "other-group", TOPIC, 2)

        with pytest.raises(ValueError):
            await consumer_a.ack([other[1]])

    async def test_a_message_already_acknowledged_is_a_value_error(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 1)
        await consumer.ack(read)

        with pytest.raises(ValueError):
            await consumer.ack(read)

    async def test_a_message_of_another_topic_is_a_value_error(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 1)
        foreign = ConsumedMessage(
            topic=OTHER_TOPIC,
            partition=read[0].partition,
            offset=read[0].offset,
            key=read[0].key,
            value=read[0].value,
        )

        with pytest.raises(ValueError):
            await consumer.ack([foreign])

    async def test_a_message_of_another_partition_is_a_value_error(self) -> None:
        # `InMemoryBus` has one partition per topic, so partition 1 is a
        # partition this consumer does not hold.
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 1)
        foreign = ConsumedMessage(
            topic=TOPIC, partition=1, offset=read[0].offset, key=read[0].key, value=read[0].value
        )

        with pytest.raises(ValueError):
            await consumer.ack([foreign])

    async def test_any_message_on_a_positional_subscription_is_a_value_error(self) -> None:
        # "no position is kept anywhere, and `ack()` on it is a `ValueError`"
        # -- even for a message this very instance delivered.
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC, start_offset=0), 1)

        with pytest.raises(ValueError):
            await consumer.ack(read)

    async def test_ack_after_close_is_a_value_error(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 1)
        await consumer.close()

        with pytest.raises(ValueError):
            await consumer.ack(read)

    async def test_a_rejected_call_acknowledges_nothing(self) -> None:
        # "the whole iterable is checked first": the valid first element is
        # not acknowledged when a later element is rejected, so a fresh
        # consumer is still handed it -- and the same instance may still
        # acknowledge it afterwards.
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 2)
        foreign = ConsumedMessage(
            topic=TOPIC, partition=1, offset=read[1].offset, key=read[1].key, value=read[1].value
        )

        with pytest.raises(ValueError):
            await consumer.ack([read[0], foreign])

        resumed = await _read_group(bus, GROUP, TOPIC, 1)
        assert resumed[0].value == b"first"
        await consumer.ack([read[0]])
        resumed_again = await _read_group(bus, GROUP, TOPIC, 1)
        assert resumed_again[0].value == b"second"

    async def test_a_rejected_call_with_an_already_acked_element_acknowledges_nothing(
        self,
    ) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 2)
        await consumer.ack([read[0]])

        with pytest.raises(ValueError):
            await consumer.ack([read[1], read[0]])

        resumed = await _read_group(bus, GROUP, TOPIC, 1)
        assert resumed[0].value == b"second"


class TestPositionalSubscriptions:
    """Decision 3, `start_offset=<int>`: "delivery starts at the first message
    whose `offset >= start_offset` ..., no position is kept anywhere". The
    trie replays from its snapshot this way (decision 9)."""

    async def test_start_offset_delivers_every_message_at_or_after_it(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        probe = await _read_group(bus, "probe", TOPIC, 3)
        second = probe[1]

        consumer = bus.consumer("replayer")
        replayed = await _take(await consumer.subscribe(TOPIC, start_offset=second.offset), 2)

        assert [message.value for message in replayed] == [b"second", b"third"]
        assert replayed[0].offset == second.offset

    async def test_a_positional_read_ignores_the_groups_acknowledgements(self) -> None:
        # "regardless of acks": the group has acknowledged everything, and a
        # positional consumer of the same group is still handed it all.
        bus = InMemoryBus()
        await _publish_three(bus)
        durable = bus.consumer(GROUP)
        read = await _take(await durable.subscribe(TOPIC), 3)
        await durable.ack(read)

        positional = bus.consumer(GROUP)
        replayed = await _take(await positional.subscribe(TOPIC, start_offset=read[0].offset), 3)

        assert [message.value for message in replayed] == [b"first", b"second", b"third"]

    async def test_start_offset_below_the_first_offset_delivers_from_the_start(self) -> None:
        # ASSUMPTION 2: `first.offset - 1` is the only way to be below the
        # first retained message on a log that never discards anything.
        bus = InMemoryBus()
        await _publish_three(bus)
        first = (await _read_group(bus, "probe", TOPIC, 1))[0]

        consumer = bus.consumer("replayer")
        replayed = await _take(await consumer.subscribe(TOPIC, start_offset=first.offset - 1), 3)

        assert [message.value for message in replayed] == [b"first", b"second", b"third"]

    async def test_start_offset_zero_delivers_from_the_start(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)

        consumer = bus.consumer("replayer")
        replayed = await _take(await consumer.subscribe(TOPIC, start_offset=0), 3)

        assert [message.value for message in replayed] == [b"first", b"second", b"third"]

    async def test_a_positional_read_keeps_no_position(self) -> None:
        # Two positional consumers of one group, one after the other, are
        # both handed everything from `start_offset`: nothing was recorded.
        bus = InMemoryBus()
        await _publish_three(bus)
        first = (await _read_group(bus, "probe", TOPIC, 1))[0]

        one = bus.consumer("replayer")
        await _take(await one.subscribe(TOPIC, start_offset=first.offset), 3)
        two = bus.consumer("replayer")
        replayed = await _take(await two.subscribe(TOPIC, start_offset=first.offset), 3)

        assert [message.value for message in replayed] == [b"first", b"second", b"third"]

    async def test_a_positional_read_does_not_disturb_the_durable_position(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        first = (await _read_group(bus, "probe", TOPIC, 1))[0]

        positional = bus.consumer(GROUP)
        await _take(await positional.subscribe(TOPIC, start_offset=first.offset), 3)

        durable = await _read_group(bus, GROUP, TOPIC, 3)
        assert [message.value for message in durable] == [b"first", b"second", b"third"]

    async def test_a_positional_read_keeps_tailing_the_log(self) -> None:
        # ASSUMPTION 3: replay, then tail.
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer("replayer")
        stream = await consumer.subscribe(TOPIC, start_offset=0)
        await _take(stream, 3)

        await bus.producer().publish(TOPIC, key="k4", value=b"fourth", message_id="id-4")

        assert [message.value for message in await _take(stream, 1)] == [b"fourth"]

    async def test_a_positional_subscription_beyond_the_log_end_waits_for_new_messages(
        self,
    ) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        last = (await _read_group(bus, "probe", TOPIC, 3))[2]
        consumer = bus.consumer("replayer")
        stream = await consumer.subscribe(TOPIC, start_offset=last.offset + 1)

        await bus.producer().publish(TOPIC, key="k4", value=b"fourth", message_id="id-4")

        assert [message.value for message in await _take(stream, 1)] == [b"fourth"]


class TestClose:
    """Decision 3, `Consumer.close()`: "stops delivery, makes the iterator end,
    and gives back every message this instance delivered under a durable
    subscription and did not acknowledge"; "idempotent and safe before
    `subscribe()`"."""

    async def test_close_ends_the_iterator(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC)
        await _take(stream, 1)

        await consumer.close()

        # Two messages remain unread; a closed iterator yields none of them.
        assert [message async for message in stream] == []

    async def test_close_raises_stop_async_iteration_on_the_next_anext(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC)

        await consumer.close()

        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    async def test_close_wakes_an_iterator_blocked_on_an_empty_log(self) -> None:
        # ASSUMPTION 5: the timeout is reached only if `close()` fails to end
        # a waiting iterator.
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC)

        async def drain() -> list[ConsumedMessage]:
            return [message async for message in stream]

        task = asyncio.create_task(drain())
        for _ in range(10):  # let the task reach the point where it blocks on the empty log
            await asyncio.sleep(0)
        await consumer.close()

        assert await asyncio.wait_for(task, timeout=1.0) == []

    async def test_close_is_idempotent(self) -> None:
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)
        await consumer.subscribe(TOPIC)

        await consumer.close()
        await consumer.close()

    async def test_close_is_safe_before_subscribe(self) -> None:
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)

        await consumer.close()
        await consumer.close()

    async def test_unacknowledged_messages_go_to_the_next_consumer_after_close(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 3)
        await consumer.ack([read[0]])

        await consumer.close()

        resumed = await _read_group(bus, GROUP, TOPIC, 2)
        assert [message.value for message in resumed] == [b"second", b"third"]

    async def test_acknowledgements_made_before_close_stand(self) -> None:
        bus = InMemoryBus()
        await _publish_three(bus)
        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 3)
        await consumer.ack(read)

        await consumer.close()

        await bus.producer().publish(TOPIC, key="k4", value=b"fourth", message_id="id-4")
        resumed = await _read_group(bus, GROUP, TOPIC, 1)
        assert resumed[0].value == b"fourth"


class TestConsumedMessageValue:
    """Decision 3: five fields plus `delivery_count` (default 1), frozen,
    `slots=True`; "every existing construction with the five keyword fields
    remains valid"."""

    def test_the_five_keyword_fields_still_construct_a_message(self) -> None:
        message = ConsumedMessage(topic=TOPIC, partition=0, offset=7, key=b"k", value=b"v")

        assert message.topic == TOPIC
        assert message.partition == 0
        assert message.offset == 7
        assert message.key == b"k"
        assert message.value == b"v"

    def test_delivery_count_defaults_to_one(self) -> None:
        message = ConsumedMessage(topic=TOPIC, partition=0, offset=7, key=b"k", value=b"v")

        assert message.delivery_count == 1

    def test_delivery_count_can_be_given(self) -> None:
        message = ConsumedMessage(
            topic=TOPIC, partition=0, offset=7, key=b"k", value=b"v", delivery_count=3
        )

        assert message.delivery_count == 3

    def test_key_may_be_none(self) -> None:
        message = ConsumedMessage(topic=TOPIC, partition=0, offset=0, key=None, value=b"v")

        assert message.key is None

    def test_is_frozen(self) -> None:
        # `dataclasses.FrozenInstanceError` subclasses `AttributeError`.
        message = ConsumedMessage(topic=TOPIC, partition=0, offset=7, key=b"k", value=b"v")

        with pytest.raises(AttributeError):
            message.offset = 8  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        message = ConsumedMessage(topic=TOPIC, partition=0, offset=7, key=b"k", value=b"v")

        assert not hasattr(message, "__dict__")

    async def test_delivered_messages_carry_delivery_count_one_on_the_memory_bus(self) -> None:
        # "`InMemoryBus` never redelivers a message to a live consumer (no
        # `ack_wait`); `delivery_count` is always 1" -- also on a fresh
        # consumer re-reading what an earlier one left unacknowledged.
        bus = InMemoryBus()
        await _publish_three(bus)
        await _read_group(bus, GROUP, TOPIC, 3)

        resumed = await _read_group(bus, GROUP, TOPIC, 3)

        assert [message.delivery_count for message in resumed] == [1, 1, 1]

    async def test_delivered_messages_carry_the_key_as_bytes(self) -> None:
        # Decision 3: a `str` key "is encoded as UTF-8 before hashing and
        # before it becomes the message key".
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"str-key", message_id="id-1")
        await producer.publish(TOPIC, key=b"10.0.0.2", value=b"bytes-key", message_id="id-2")

        received = await _read_group(bus, GROUP, TOPIC, 2)

        assert [message.key for message in received] == [b"10.0.0.1", b"10.0.0.2"]

    async def test_every_message_is_on_partition_zero_whatever_its_key(self) -> None:
        # `InMemoryBus` "implements the same contract with one partition per
        # topic": keys that would hash to different partitions on a
        # 128-partition topic all land on 0 here.
        bus = InMemoryBus()
        producer = bus.producer()
        keys = ["10.0.0.1", "10.0.0.2", "203.0.113.9", "2001:db8::1", "k1", "k2", "k3", "k4"]
        for index, key in enumerate(keys):
            await producer.publish(
                TOPIC, key=key, value=f"m{index}".encode(), message_id=str(index)
            )

        received = await _read_group(bus, GROUP, TOPIC, len(keys))

        assert [message.partition for message in received] == [0] * len(keys)
        assert [message.topic for message in received] == [TOPIC] * len(keys)


class TestTheInterfaceAfterAdr0013:
    """Decision 3: "Removed: `Consumer.seek`, `Consumer.commit`"; `MessageBus`
    moved to `hammertime.bus.interface`; the protocols are runtime-checkable
    and `InMemoryBus` and its producer/consumer satisfy them."""

    def test_the_consumer_protocol_has_no_seek(self) -> None:
        assert not hasattr(Consumer, "seek")

    def test_the_consumer_protocol_has_no_commit(self) -> None:
        assert not hasattr(Consumer, "commit")

    def test_the_memory_consumer_has_no_seek_or_commit(self) -> None:
        consumer = InMemoryBus().consumer(GROUP)

        assert not hasattr(consumer, "seek")
        assert not hasattr(consumer, "commit")

    def test_the_memory_bus_satisfies_message_bus(self) -> None:
        assert isinstance(InMemoryBus(), MessageBus)

    def test_an_unrelated_object_is_not_a_message_bus(self) -> None:
        # Guards against a Protocol with no members, which everything would
        # satisfy.
        assert not isinstance(object(), MessageBus)

    def test_the_memory_producer_satisfies_producer(self) -> None:
        assert isinstance(InMemoryBus().producer(), Producer)

    def test_the_memory_consumer_satisfies_consumer(self) -> None:
        assert isinstance(InMemoryBus().consumer(GROUP), Consumer)

    def test_a_message_bus_hands_out_a_producer_and_a_consumer(self) -> None:
        bus: MessageBus = InMemoryBus()

        assert isinstance(bus.producer(), Producer)
        assert isinstance(bus.consumer(GROUP), Consumer)
