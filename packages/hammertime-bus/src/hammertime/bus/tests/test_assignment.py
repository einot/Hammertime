"""Assignment listener: the static partition set is delivered before `subscribe()` returns.

Spec: section 20 (distributed processing -- ownership of each IP belongs to
exactly one shard), section 47 (readiness: "config loaded; consumer
subscribed; shard claims held"). ADR-0013 decision 3 (the bus interface:
`AssignmentListener` has `on_assigned` only; `subscribe(topic, *,
partitions=None, listener=None, start_offset=None)`), decision 6 (shard
assignment is static), ADR-0011 Amendment 1 item A3 (an empty static set is
a `ValueError` at the bus; unchanged), ADR-0009 decision 9 (one fixed
consumer group).

This file is written blind to `interface.py`/`memory.py`, per this
package's test-author convention: the API is pinned here from ADR-0013
decision 3's code block and the paragraphs under it alone.

What decision 3 pins, and is asserted below:

* `AssignmentListener` is a `runtime_checkable` `Protocol` with exactly one
  coroutine method, `on_assigned(frozenset[tuple[str, int]]) -> None`.
  "Removed: ... `AssignmentListener.on_revoked`" -- static assignment has no
  revocation (decision 8: "the way a shard changes hands is a `stop()` on one
  member and a `start()` on another").
* "When `listener` is given, `subscribe()` awaits
  `listener.on_assigned(frozenset((topic, p) for p in <the set>))` exactly
  once -- with the full static set, or for `partitions=None` with every
  partition of a registered topic (`{0}` on `InMemoryBus`) -- before it
  returns; that is what keeps ADR-0009's 'shard claims held' readiness
  observable at the end of `start()`." Asserted in three ways: on an empty
  log, ahead of the first yielded message, and exactly once.
* "`partitions=None` means every partition of the topic -- ... on
  `InMemoryBus` `{0}`"; "`partitions=<iterable>` means exactly those
  partitions; an empty iterable is a `ValueError` raised before any broker
  is contacted and before `listener` is called (ADR-0011 A3, unchanged);
  `InMemoryBus` has one partition per topic, so any static set other than
  `{0}` is a `ValueError` there (unchanged), and `partitions=None` on it is
  `{0}`."
* "If `on_assigned` raises, `subscribe()` propagates the exception and holds
  nothing: the consumer is left as if `subscribe()` had never been called".
* "A positional subscription may still name `partitions` and a `listener`."
* Acknowledgements are kept under the group name whichever way the
  partitions were named (decision 5: "acknowledged positions are kept by
  durables named after the group").
* `ConsumedMessage.partition` is the shard id the aggregator reads ownership
  from (ADR-0011 decision 1's surviving rule, Amendment 7), so for the
  single-partition memory bus it is `0` for every message.

ASSUMPTIONS -- things decision 3 implies but does not name or pin. Each is
a judgment call; adjust the test, not the meaning, if the implementation
settles them differently:

1. `AssignmentListener` is importable from `hammertime.bus.interface`,
   taken from the ADR block's own `# hammertime.bus.interface` header.
   `InMemoryBus` / `bus.producer()` / `bus.consumer(group_id)` /
   `await consumer.subscribe(...)`-returns-an-async-iterator are carried
   over from `test_memory_bus.py`.
2. `partitions`, `listener` and `start_offset` are keyword-only (the ADR
   block writes them after a bare `*`), and all default to `None`.
3. The `ValueError` for a rejected static set surfaces when the `subscribe`
   coroutine is awaited (`subscribe` is `async def`, so it cannot surface
   earlier). `pytest.raises` wraps the whole `await` expression, so the
   test passes under either timing.
4. The signature says `Iterable[int]`, so a list `[0]` is accepted exactly
   like `{0}`, and `[]` is refused exactly like `set()`. The ADR only ever
   writes set literals.
5. "Left as if `subscribe()` had never been called" after a failing
   `on_assigned` is read literally: the same instance may `subscribe()`
   again without the `RuntimeError` a second call otherwise raises. Nothing
   in the ADR says this in words; it is the plain meaning of the sentence.
6. `RecordingListener` below is this file's own test double; the ADR names
   no concrete implementation (the aggregator's is `ShardClaims`, out of
   scope here).
7. A static set refused with `ValueError` does not count as the instance's
   one `subscribe()`: the refusal happens "before any broker is contacted
   and before `listener` is called", i.e. before a subscription exists, so
   the same instance may subscribe again
   (`test_a_rejected_static_set_leaves_the_instance_unsubscribed`). The ADR
   states this only for the failing-listener case; extending it to the
   earlier, cheaper refusal is this file's reading.

NOT TESTABLE against `InMemoryBus`, and not tested anywhere in this file:

* `partitions=None` on `NatsConsumer` (a whole-topic durable) and a static
  set with more than one partition; the memory bus has one partition.
* `InMemoryBus`'s "at most one live member per group per topic" limit
  (ADR-0011 assumption 22, kept by ADR-0013 decision 3) -- a constraint on
  callers, not a behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from hammertime.bus.interface import AssignmentListener, ConsumedMessage
from hammertime.bus.memory import InMemoryBus

# Plain strings handed to `InMemoryBus`, which creates topics on demand.
# TOPIC is spelled like the observations topic only for readability -- it is
# never looked up in `TOPICS`, and nothing here asserts that literal (see
# `test_topics.py`, which pins the registry). GROUP is ADR-0009 decision 9's
# fixed aggregator consumer group.
TOPIC = "hammertime.observations.v1"
OTHER_TOPIC = "test.other.v1"
GROUP = "hammertime-aggregator"


class RecordingListener:
    """`AssignmentListener` that records what it was called with, in order."""

    def __init__(self) -> None:
        self.assigned: list[frozenset[tuple[str, int]]] = []
        # Interleaves listener callbacks with whatever the test appends
        # (message reads), so "before the first message" is checkable as an
        # ordering rather than only as a count.
        self.trace: list[str] = []

    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None:
        self.assigned.append(partitions)
        self.trace.append("on_assigned")


class FailingListener:
    """An `AssignmentListener` whose `on_assigned` always raises."""

    def __init__(self) -> None:
        self.calls = 0

    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None:
        self.calls += 1
        raise RuntimeError("claim refused by the listener")


class OnlyOnRevoked:
    """Has the method ADR-0013 removed and not the one it kept."""

    async def on_revoked(self, partitions: frozenset[tuple[str, int]]) -> None:  # pragma: no cover
        return None


class NotAListener:
    """Has neither coroutine method; used for the negative `isinstance` case."""

    async def on_something_else(self) -> None:  # pragma: no cover - never called
        return None


async def _take(stream: AsyncIterator[ConsumedMessage], n: int) -> list[ConsumedMessage]:
    """Read exactly `n` messages from a live subscription, then stop.

    A live subscription blocks waiting for the next message (real broker
    semantics), so every read in this file is bounded by what was
    published, exactly as `test_memory_bus.py` does.
    """

    messages: list[ConsumedMessage] = []
    async for message in stream:
        messages.append(message)
        if len(messages) == n:
            break
    return messages


async def _publish(bus: InMemoryBus, *values: bytes) -> None:
    producer = bus.producer()
    for index, value in enumerate(values):
        await producer.publish(
            TOPIC, key=f"10.0.0.{index + 1}", value=value, message_id=f"m{index}"
        )


class TestInitialAssignmentIsDeliveredBeforeSubscribeReturns:
    """ADR-0013 decision 3: `subscribe()` awaits `on_assigned` "before it
    returns; that is what keeps ADR-0009's 'shard claims held' readiness
    observable at the end of `start()`"."""

    async def test_subscribe_without_partitions_awaits_on_assigned_before_returning(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, b"first")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        await consumer.subscribe(TOPIC, listener=listener)

        # Nothing has been iterated yet: if the claim is visible here, it
        # was delivered by subscribe() itself.
        assert listener.assigned == [frozenset({(TOPIC, 0)})]

    async def test_on_assigned_is_awaited_even_when_the_log_is_empty(self) -> None:
        # Nothing was ever published to TOPIC. A service that only became
        # ready once a message arrived could never report "shard claims
        # held" on a quiet stream.
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        await consumer.subscribe(TOPIC, listener=listener)

        assert listener.assigned == [frozenset({(TOPIC, 0)})]

    async def test_on_assigned_precedes_the_first_yielded_message(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, b"first", b"second")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, listener=listener)

        async for message in stream:
            listener.trace.append(f"message:{message.value.decode()}")
            if len(listener.trace) == 3:  # on_assigned + two messages
                break

        assert listener.trace == ["on_assigned", "message:first", "message:second"]

    async def test_the_assignment_names_the_subscribed_topic(self) -> None:
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        await consumer.subscribe(OTHER_TOPIC, listener=listener)

        assert listener.assigned == [frozenset({(OTHER_TOPIC, 0)})]

    async def test_on_assigned_is_called_exactly_once_across_subscribe_consume_ack_and_close(
        self,
    ) -> None:
        bus = InMemoryBus()
        await _publish(bus, b"first", b"second", b"third")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, listener=listener)
        read = await _take(stream, 2)
        await consumer.ack(read)
        await _take(stream, 1)
        await consumer.ack([])
        await consumer.close()

        assert listener.assigned == [frozenset({(TOPIC, 0)})]
        assert listener.trace == ["on_assigned"]

    async def test_a_reconnecting_member_is_assigned_afresh(self) -> None:
        # The claim-then-reconnect path the aggregator takes after a restart:
        # a brand new consumer for the same group is assigned the same set;
        # nothing is ever taken from the abandoned instance (there is no
        # coordinator to do so, and no `on_revoked` to tell it).
        bus = InMemoryBus()
        await _publish(bus, b"first", b"second")

        first_listener = RecordingListener()
        first = bus.consumer(GROUP)
        read = await _take(await first.subscribe(TOPIC, listener=first_listener), 1)
        await first.ack(read)

        second_listener = RecordingListener()
        second = bus.consumer(GROUP)
        await second.subscribe(TOPIC, listener=second_listener)

        assert first_listener.assigned == [frozenset({(TOPIC, 0)})]
        assert second_listener.assigned == [frozenset({(TOPIC, 0)})]


class TestStaticAssignment:
    """ADR-0013 decision 3: "`partitions=<iterable>` means exactly those
    partitions"; on `InMemoryBus` "any static set other than `{0}` is a
    `ValueError` ..., and `partitions=None` on it is `{0}`"."""

    async def test_static_partition_zero_assigns_the_same_single_partition(self) -> None:
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        await consumer.subscribe(TOPIC, partitions={0}, listener=listener)

        assert listener.assigned == [frozenset({(TOPIC, 0)})]

    async def test_partitions_none_and_static_zero_assign_the_same_set(self) -> None:
        bus = InMemoryBus()
        none_listener = RecordingListener()
        zero_listener = RecordingListener()

        await bus.consumer("group-none").subscribe(TOPIC, partitions=None, listener=none_listener)
        await bus.consumer("group-zero").subscribe(TOPIC, partitions={0}, listener=zero_listener)

        assert none_listener.assigned == zero_listener.assigned == [frozenset({(TOPIC, 0)})]

    async def test_static_partition_zero_delivers_messages_like_partitions_none(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, b"first", b"second")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, partitions={0}, listener=listener)
        received = await _take(stream, 2)

        assert [message.value for message in received] == [b"first", b"second"]
        assert listener.trace[0] == "on_assigned"

    async def test_static_partitions_accept_any_iterable_of_ints(self) -> None:
        # ASSUMPTION 4: the signature says `Iterable[int]`, not `set[int]`.
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        await consumer.subscribe(TOPIC, partitions=[0], listener=listener)

        assert listener.assigned == [frozenset({(TOPIC, 0)})]

    async def test_static_assignment_keeps_acknowledgements_under_the_group_name(self) -> None:
        # Decision 5: "acknowledged positions are kept by durables named
        # after the group" -- a statically assigned member resumes where the
        # group left off.
        bus = InMemoryBus()
        await _publish(bus, b"first", b"second")

        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, partitions={0})
        read = await _take(stream, 1)
        await consumer.ack(read)

        reconnected = bus.consumer(GROUP)
        resumed = await _take(await reconnected.subscribe(TOPIC, partitions={0}), 1)

        assert resumed[0].value == b"second"

    @pytest.mark.parametrize("partitions", [{1}, {2}, {0, 1}, {7}])
    async def test_any_static_set_other_than_zero_is_a_value_error(
        self, partitions: set[int]
    ) -> None:
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions=partitions)

    @pytest.mark.parametrize("partitions", [{1}, {0, 1}])
    async def test_a_rejected_static_set_never_reports_a_claim(self, partitions: set[int]) -> None:
        # A refused assignment was never held, so readiness must not be able
        # to observe it: the `ValueError` is raised "before `listener` is
        # called".
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions=partitions, listener=listener)

        assert listener.assigned == []

    async def test_an_empty_static_set_is_a_value_error(self) -> None:
        # ADR-0011 Amendment 1 item A3, carried unchanged by ADR-0013: "An
        # empty static set is not a way of saying 'nothing'". A member that
        # owned nothing for its whole life while /readyz reported it healthy
        # is the silent misconfiguration A3 refuses.
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions=set())

    async def test_an_empty_static_list_is_a_value_error_too(self) -> None:
        # ASSUMPTION 4: A3 refuses "an empty iterable", not an empty set.
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions=[])

    async def test_a_rejected_empty_static_set_never_reports_a_claim(self) -> None:
        # A3: the refusal happens "before contacting any broker and without
        # calling the listener".
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions=set(), listener=listener)

        assert listener.assigned == []

    async def test_a_rejected_static_set_leaves_the_instance_unsubscribed(self) -> None:
        # The refusal happens before the subscription exists, so the instance
        # may still subscribe; a `RuntimeError` here would mean the rejected
        # call had counted as the one subscription per instance.
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions={1})
        await consumer.subscribe(TOPIC, partitions={0}, listener=listener)

        assert listener.assigned == [frozenset({(TOPIC, 0)})]


class TestAFailingListenerHoldsNothing:
    """ADR-0013 decision 3: "If `on_assigned` raises, `subscribe()` propagates
    the exception and holds nothing: the consumer is left as if `subscribe()`
    had never been called"."""

    async def test_the_listeners_exception_propagates_out_of_subscribe(self) -> None:
        bus = InMemoryBus()
        listener = FailingListener()
        consumer = bus.consumer(GROUP)

        with pytest.raises(RuntimeError, match="claim refused"):
            await consumer.subscribe(TOPIC, listener=listener)

        assert listener.calls == 1

    async def test_the_instance_may_subscribe_again_afterwards(self) -> None:
        # ASSUMPTION 5: "as if `subscribe()` had never been called".
        bus = InMemoryBus()
        await _publish(bus, b"first")
        consumer = bus.consumer(GROUP)

        with pytest.raises(RuntimeError):
            await consumer.subscribe(TOPIC, listener=FailingListener())
        listener = RecordingListener()
        received = await _take(await consumer.subscribe(TOPIC, listener=listener), 1)

        assert listener.assigned == [frozenset({(TOPIC, 0)})]
        assert received[0].value == b"first"

    async def test_nothing_is_acknowledged_or_delivered_under_a_refused_claim(self) -> None:
        # The group's position is untouched: a later consumer sees the log
        # from the start.
        bus = InMemoryBus()
        await _publish(bus, b"first")
        consumer = bus.consumer(GROUP)

        with pytest.raises(RuntimeError):
            await consumer.subscribe(TOPIC, listener=FailingListener())

        later = bus.consumer(GROUP)
        received = await _take(await later.subscribe(TOPIC), 1)
        assert received[0].value == b"first"


class TestPositionalSubscriptionsMayNameAListener:
    """ADR-0013 decision 3: "A positional subscription may still name
    `partitions` and a `listener`."""

    async def test_a_positional_subscription_delivers_the_assignment(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, b"first")
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        stream = await consumer.subscribe(TOPIC, partitions={0}, listener=listener, start_offset=0)

        assert listener.assigned == [frozenset({(TOPIC, 0)})]
        received = await _take(stream, 1)
        assert received[0].value == b"first"
        assert listener.trace == ["on_assigned"]

    async def test_a_positional_subscription_rejects_a_bad_static_set_before_the_listener(
        self,
    ) -> None:
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions={1}, listener=listener, start_offset=0)

        assert listener.assigned == []


class TestSubscribeWithoutAListener:
    """The listener is optional (`listener: AssignmentListener | None = None`);
    every `test_memory_bus.py` call site must keep working."""

    async def test_subscribe_with_no_listener_still_delivers_messages_in_order(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, b"first", b"second")

        consumer = bus.consumer(GROUP)
        received = await _take(await consumer.subscribe(TOPIC), 2)

        assert [message.value for message in received] == [b"first", b"second"]

    async def test_subscribe_with_no_listener_still_acknowledges_and_resumes(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, b"first", b"second")

        consumer = bus.consumer(GROUP)
        read = await _take(await consumer.subscribe(TOPIC), 1)
        await consumer.ack(read)

        reconnected = bus.consumer(GROUP)
        resumed = await _take(await reconnected.subscribe(TOPIC), 1)

        assert resumed[0].value == b"second"


class TestConsumedMessagePartitionIsTheShardId:
    """ADR-0011 decision 1's surviving rule (Amendment 7): a shard is a
    partition and `ConsumedMessage.partition` is the shard id. On the
    single-partition memory bus that shard is always 0."""

    async def test_every_message_carries_partition_zero(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        for index, key in enumerate(["10.0.0.1", "10.0.0.2", "203.0.113.9"]):
            await producer.publish(
                TOPIC, key=key, value=f"m{index}".encode(), message_id=str(index)
            )

        consumer = bus.consumer(GROUP)
        received = await _take(await consumer.subscribe(TOPIC), 3)

        assert [message.partition for message in received] == [0, 0, 0]

    async def test_partition_is_zero_under_static_assignment_too(self) -> None:
        bus = InMemoryBus()
        await _publish(bus, b"first")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, partitions={0}, listener=listener)
        received = await _take(stream, 1)

        # The partition the message arrives on is the partition that was
        # claimed -- the aggregator indexes its ShardWindow by it.
        assert received[0].partition == 0
        assert listener.assigned == [frozenset({(TOPIC, received[0].partition)})]

    async def test_partition_is_an_int_not_a_string(self) -> None:
        # frozenset[tuple[str, int]] on one side, ConsumedMessage.partition
        # on the other: they must be the same kind of value for the
        # aggregator to look a window up by it.
        bus = InMemoryBus()
        await _publish(bus, b"first")

        consumer = bus.consumer(GROUP)
        received = await _take(await consumer.subscribe(TOPIC), 1)

        assert isinstance(received[0].partition, int)


class TestAssignmentListenerProtocol:
    """ADR-0013 decision 3: `AssignmentListener` is declared
    `@runtime_checkable` with `on_assigned` only, so a structural
    implementation passes `isinstance` without inheriting from it, and
    `on_revoked` is neither required nor part of the protocol."""

    def test_a_class_with_on_assigned_only_is_an_instance(self) -> None:
        assert isinstance(RecordingListener(), AssignmentListener)

    def test_a_class_without_on_assigned_is_not_an_instance(self) -> None:
        assert not isinstance(NotAListener(), AssignmentListener)
        assert not isinstance(object(), AssignmentListener)

    def test_a_class_with_only_the_removed_on_revoked_is_not_an_instance(self) -> None:
        assert not isinstance(OnlyOnRevoked(), AssignmentListener)

    def test_the_protocol_has_no_on_revoked(self) -> None:
        # "Removed: ... `AssignmentListener.on_revoked`".
        assert not hasattr(AssignmentListener, "on_revoked")

    def test_a_structural_implementation_type_checks_as_the_protocol(self) -> None:
        # Static counterpart to the isinstance check above: this assignment
        # is what mypy verifies, and it is also how the listener reaches
        # `subscribe(..., listener=...)` in the tests above.
        listener: AssignmentListener = RecordingListener()

        assert listener is not None
