"""In-memory Producer/Consumer bus: ordering, consumer groups, seek, commit.

Spec: section 19 (event-driven internal architecture), section 32/33
(durable event log / replayability -- offsets and commit positions are how
recovery-after-crash and replay are modeled).

`interface.py` and `memory.py` are currently docstring-only stubs:

    interface.py: "Producer/Consumer protocols: publish, subscribe,
                   seek(sequence), commit."
    memory.py:    "In-process transport for unit and property tests; same
                   ordering guarantees."

Neither file has any signature yet, so -- as with the core/events tests --
this file doubles as the executable specification the implementation is
expected to satisfy, not just a check against existing code. Assumptions
made below, with their justification:

* All Producer/Consumer I/O methods (`publish`, `subscribe`, `poll`,
  `seek`, `commit`) are `async def`. `hammertime-bus`'s pyproject depends on
  `aiokafka` (an async client) for the real `kafka.py` transport, and the
  repo-root `pyproject.toml` already configures
  `[tool.pytest.ini_options] asyncio_mode = "auto"` and depends on
  `pytest-asyncio` even though no async code exists anywhere else in the
  repo yet -- the only plausible reason for that config to exist ahead of
  any other async module is this package. Since `memory.py`'s docstring
  promises "same ordering guarantees" as the real transport, one shared
  async Protocol for both backends is the natural reading, even though the
  in-memory implementation itself never actually awaits I/O.
* `bus/__init__.py`'s docstring ("the log interface deliberately exposes
  offsets/sequences rather than hiding them") is the basis for `Message`
  exposing a `.sequence` attribute and for `seek`/`commit` being
  offset/sequence-based rather than hiding position from callers.
* A broker-like `InMemoryBus` object is the natural place to hold the
  shared per-topic log and per-(group_id, topic) committed offsets, since
  two independently-constructed `Consumer`s in the same group must be able
  to see each other's committed position (that's the entire point of the
  commit/reconnect test below). `bus.producer()` / `bus.consumer(group_id=...)`
  are assumed factory methods on it.
* `poll()` returns `Message | None`, with `None` meaning "nothing new
  right now" -- chosen over a blocking read so tests stay deterministic
  and don't hang.
* `Consumer.subscribe(topic)` positions the consumer at its group's last
  committed offset for that topic if one exists, else at the start of the
  log -- this is required for the commit/reconnect scenario to be
  expressible at all, and matches standard consumer-group semantics.
* `commit()` takes no arguments and commits "next offset to read" (i.e.
  everything polled so far); `seek(sequence)` repositions so the *next*
  `poll()` returns the message with that sequence.

If the coder's actual signature differs (e.g. sync methods, a different
factory shape, `poll()` returning a list), these tests will fail to
collect (ImportError) or fail outright -- see the test-author's report for
this explicitly flagged as the highest-risk area of this batch.
"""

from __future__ import annotations

from hammertime.bus.memory import InMemoryBus

TOPIC = "test.topic.v1"


class TestOrderingForASingleConsumer:
    async def test_publish_then_subscribe_delivers_messages_in_publish_order(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")
        await producer.publish(TOPIC, key="k3", value=b"third")

        consumer = bus.consumer(group_id="single-consumer")
        await consumer.subscribe(TOPIC)

        received = [await consumer.poll() for _ in range(3)]
        values = [message.value for message in received if message is not None]

        assert values == [b"first", b"second", b"third"]

    async def test_poll_returns_none_once_the_log_is_exhausted(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"only-message")

        consumer = bus.consumer(group_id="drain")
        await consumer.subscribe(TOPIC)

        first = await consumer.poll()
        second = await consumer.poll()

        assert first is not None
        assert first.value == b"only-message"
        assert second is None


class TestIndependentConsumerGroups:
    async def test_two_consumer_groups_each_see_the_full_log_independently(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")

        group_a = bus.consumer(group_id="group-a")
        group_b = bus.consumer(group_id="group-b")
        await group_a.subscribe(TOPIC)
        await group_b.subscribe(TOPIC)

        # Fully drain group A first.
        a_messages = []
        for _ in range(2):
            message = await group_a.poll()
            assert message is not None
            a_messages.append(message.value)

        # Group B's view must be unaffected by group A having consumed
        # everything -- it independently sees the full log from the start.
        b_messages = []
        for _ in range(2):
            message = await group_b.poll()
            assert message is not None
            b_messages.append(message.value)

        assert a_messages == [b"first", b"second"]
        assert b_messages == [b"first", b"second"]


class TestSeek:
    async def test_seek_repositions_the_consumer_to_a_given_sequence(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")
        await producer.publish(TOPIC, key="k3", value=b"third")

        # Learn sequence numbers by reading the log, rather than assuming
        # 0-based/1-based numbering.
        probe = bus.consumer(group_id="probe")
        await probe.subscribe(TOPIC)
        first = await probe.poll()
        second = await probe.poll()
        third = await probe.poll()
        assert first is not None and second is not None and third is not None

        consumer = bus.consumer(group_id="seeker")
        await consumer.subscribe(TOPIC)
        await consumer.seek(third.sequence)

        replayed = await consumer.poll()

        assert replayed is not None
        assert replayed.sequence == third.sequence
        assert replayed.value == b"third"

    async def test_seek_backwards_allows_replaying_already_read_messages(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")

        consumer = bus.consumer(group_id="replayer")
        await consumer.subscribe(TOPIC)
        first = await consumer.poll()
        await consumer.poll()  # consume "second" too
        assert first is not None

        await consumer.seek(first.sequence)
        replayed = await consumer.poll()

        assert replayed is not None
        assert replayed.value == b"first"


class TestCommitAndReconnect:
    async def test_new_consumer_in_a_fresh_group_starts_from_the_beginning(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")

        consumer = bus.consumer(group_id="never-committed-before")
        await consumer.subscribe(TOPIC)
        message = await consumer.poll()

        assert message is not None
        assert message.value == b"first"

    async def test_commit_persists_position_so_a_new_consumer_instance_resumes_after_it(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")
        await producer.publish(TOPIC, key="k3", value=b"third")

        group = "aggregator-shard-0"
        consumer = bus.consumer(group_id=group)
        await consumer.subscribe(TOPIC)
        first = await consumer.poll()
        assert first is not None and first.value == b"first"
        await consumer.commit()

        # Simulate reconnect-after-crash: a brand new Consumer instance,
        # same consumer group, same topic.
        reconnected = bus.consumer(group_id=group)
        await reconnected.subscribe(TOPIC)
        resumed = await reconnected.poll()

        assert resumed is not None
        assert resumed.value == b"second"  # not "first" again, and not the start of the log

    async def test_uncommitted_progress_is_not_persisted_across_new_instances(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="k1", value=b"first")
        await producer.publish(TOPIC, key="k2", value=b"second")

        group = "no-commit-shard"
        consumer = bus.consumer(group_id=group)
        await consumer.subscribe(TOPIC)
        await consumer.poll()  # read "first" but never commit

        reconnected = bus.consumer(group_id=group)
        await reconnected.subscribe(TOPIC)
        resumed = await reconnected.poll()

        assert resumed is not None
        assert resumed.value == b"first"  # crash before commit -> redelivered from the start
