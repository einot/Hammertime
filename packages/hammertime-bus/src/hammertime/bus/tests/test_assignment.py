"""Assignment listener: shard claims delivered before `subscribe()` returns.

Spec: section 20 (distributed processing -- ownership of each IP belongs to
exactly one shard), section 47 (readiness: "config loaded; consumer
subscribed; shard claims held"). ADR-0011 decision 1 (a shard *is* a
partition of `hammertime.observations.v1`; a claim *is* the consumer group's
assignment), ADR-0009 decision 9 (one fixed consumer group).

This file is written blind to `interface.py`/`memory.py`, per this
package's test-author convention (see `test_topics.py`'s equivalent note):
the API under test does not exist yet and is pinned here from ADR-0011
decision 1's code block alone.

What decision 1 pins, and is asserted below:

* `AssignmentListener` is a `runtime_checkable` `Protocol` with two
  coroutine methods, `on_revoked(frozenset[tuple[str, int]]) -> None` and
  `on_assigned(frozenset[tuple[str, int]]) -> None`.
* `Consumer.subscribe(topic, *, partitions: Iterable[int] | None = None,
  listener: AssignmentListener | None = None)`.
* "When a listener is given, `subscribe()` does not return until
  `on_assigned` has been awaited with the initial assignment." This is the
  property that makes ADR-0009's "shard claims held" observable from inside
  `start()`, which is why it is asserted here in three ways: on an empty
  log, ahead of the first yielded message, and exactly once.
* "`InMemoryBus` has one partition per topic: group-managed and static
  `{0}` both assign `{(topic, 0)}` immediately; any other static set --
  including the empty set, for every consumer implementation (Amendment 1,
  item A3) -- is a `ValueError`."
* An explicitly empty static set is refused, and refused early: ADR-0011
  Amendment 1 item A3 rules that `Consumer.subscribe(topic,
  partitions=<empty iterable>)` "raises `ValueError` for every
  implementation ..., before contacting any broker and without calling the
  listener. An empty static set is not a way of saying 'nothing';
  `partitions=None` is the only way of saying 'let the group decide'."
  Asserted below for `set()` and for `[]` (the parameter is an
  `Iterable[int]`, so the refusal must not depend on the concrete type),
  and for the listener never seeing a claim. The `partitions=None` half is
  every listener test in
  `TestInitialAssignmentIsDeliveredBeforeSubscribeReturns`, which subscribes
  without `partitions` and is assigned `{(TOPIC, 0)}`; A3 leaves that path
  and the group-managed empty *initial* assignment (ready, `WARNING
  event=no_shards_assigned`) unchanged -- the latter is not observable
  against the memory bus, see "NOT TESTABLE" below.
* Static assignment still commits offsets under the group name.
* `ConsumedMessage.partition` is the shard id the aggregator reads
  ownership from ("The aggregator never computes an IP hash of its own"),
  so for the single-partition memory bus it is `0` for every message.

ASSUMPTIONS -- things decision 1 implies but does not name or pin. Each is
a judgment call; adjust the test, not the meaning, if the implementation
settles them differently:

1. `AssignmentListener` is importable from `hammertime.bus.interface`,
   taken from the ADR block's own `# hammertime.bus.interface (additions)`
   header. `InMemoryBus` / `bus.producer()` / `bus.consumer(group_id)` /
   `await consumer.subscribe(...)`-returns-an-async-iterator are carried
   over from `test_memory_bus.py`, not from the ADR.
2. `partitions` and `listener` are keyword-only (the ADR block writes them
   after a bare `*`), and both default to `None`.
3. The `ValueError` for a rejected static set surfaces when the `subscribe`
   coroutine is awaited (`subscribe` is `async def`, so it cannot surface
   earlier). `pytest.raises` wraps the whole `await` expression, so the
   test passes under either timing.
4. A rejected static set must not invoke the listener at all: an assignment
   that was refused was never held, and ADR-0009 readiness must not be able
   to observe a claim the bus rejected. The ADR does not say this in words.
5. The signature says `Iterable[int]`, so a list `[0]` is accepted exactly
   like `{0}`. The ADR only ever writes set literals.
6. Whether a *second* `subscribe()` on the same consumer re-delivers
   `on_assigned` is unstated, so no test subscribes twice with a listener;
   "exactly once" is asserted across one subscribe plus consumption and
   commit.
7. `RecordingListener` below is this file's own test double; the ADR names
   no concrete implementation (the aggregator's is `ShardClaims`, out of
   scope here).

(The empty static set used to be assumption 5 here -- "deliberately *not*
tested ... nothing in the ADR disambiguates. Flagged rather than guessed."
ADR-0011 Amendment 1 item A3 disambiguated it, so it is no longer an
assumption of this file but one of the properties pinned above.)

NOT TESTABLE against `InMemoryBus`, and not tested anywhere in this file:

* Real rebalance ordering ("every rebalance calls `on_revoked` (before
  partitions move) then `on_assigned` (after)") and an initially empty
  group-managed assignment. The memory bus has no coordinator and always
  assigns `{(topic, 0)}`; only a broker-backed `KafkaConsumer` could show
  either, and CI has no broker.
* The deployment rule that static and group-managed members MUST NOT be
  mixed in one group, and `InMemoryBus`'s "at most one live member per
  group per topic" limit -- both are stated as constraints on callers,
  which the ADR itself says "no test relies on".
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from hammertime.bus.interface import AssignmentListener, ConsumedMessage
from hammertime.bus.memory import InMemoryBus

# Plain strings handed to `InMemoryBus`, which creates topics on demand.
# TOPIC is spelled like the observations topic only for readability -- it is
# never looked up in `TOPICS`, and nothing here asserts that literal (see
# `test_topics.py`, which deliberately does not pin that name). GROUP is
# ADR-0009 decision 9's fixed aggregator consumer group.
TOPIC = "hammertime.observations.v1"
OTHER_TOPIC = "test.other.v1"
GROUP = "hammertime-aggregator"


class RecordingListener:
    """`AssignmentListener` that records what it was called with, in order."""

    def __init__(self) -> None:
        self.assigned: list[frozenset[tuple[str, int]]] = []
        self.revoked: list[frozenset[tuple[str, int]]] = []
        # Interleaves listener callbacks with whatever the test appends
        # (message reads), so "before the first message" is checkable as an
        # ordering rather than only as a count.
        self.trace: list[str] = []

    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None:
        self.assigned.append(partitions)
        self.trace.append("on_assigned")

    async def on_revoked(self, partitions: frozenset[tuple[str, int]]) -> None:
        self.revoked.append(partitions)
        self.trace.append("on_revoked")


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


class TestInitialAssignmentIsDeliveredBeforeSubscribeReturns:
    """ADR-0011 decision 1: "`subscribe()` does not return until `on_assigned`
    has been awaited with the initial assignment"; that is what makes
    ADR-0009's "shard claims held" observable inside `start()`."""

    async def test_group_managed_subscribe_awaits_on_assigned_before_returning(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")

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
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")
        await producer.publish(TOPIC, key="10.0.0.2", value=b"second")

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

    async def test_on_assigned_is_called_exactly_once_across_subscribe_consume_commit(
        self,
    ) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")
        await producer.publish(TOPIC, key="10.0.0.2", value=b"second")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, listener=listener)
        await _take(stream, 2)
        await consumer.commit()

        assert listener.assigned == [frozenset({(TOPIC, 0)})]


class TestStaticAssignment:
    """ADR-0011 decision 1: "group-managed and static `{0}` both assign
    `{(topic, 0)}` immediately; any other static set -- including the empty
    set, for every consumer implementation (Amendment 1, item A3) -- is a
    `ValueError`"."""

    async def test_static_partition_zero_assigns_the_same_single_partition(self) -> None:
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        await consumer.subscribe(TOPIC, partitions={0}, listener=listener)

        assert listener.assigned == [frozenset({(TOPIC, 0)})]

    async def test_static_partition_zero_delivers_messages_like_group_managed(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")
        await producer.publish(TOPIC, key="10.0.0.2", value=b"second")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, partitions={0}, listener=listener)
        received = await _take(stream, 2)

        assert [message.value for message in received] == [b"first", b"second"]
        assert listener.trace[0] == "on_assigned"

    async def test_static_partitions_accept_any_iterable_of_ints(self) -> None:
        # ASSUMPTION 5: the signature says `Iterable[int]`, not `set[int]`.
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        await consumer.subscribe(TOPIC, partitions=[0], listener=listener)

        assert listener.assigned == [frozenset({(TOPIC, 0)})]

    async def test_static_assignment_still_commits_offsets_under_the_group_name(self) -> None:
        # "offsets are still committed under the group name" -- a statically
        # assigned member resumes where the group left off.
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")
        await producer.publish(TOPIC, key="10.0.0.2", value=b"second")

        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, partitions={0})
        await _take(stream, 1)
        await consumer.commit()

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

    async def test_a_rejected_static_set_never_reports_a_claim(self) -> None:
        # ASSUMPTION 4: a refused assignment was never held, so readiness
        # must not be able to observe it.
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions={1}, listener=listener)

        assert listener.assigned == []
        assert listener.revoked == []

    async def test_an_empty_static_set_is_a_value_error(self) -> None:
        # ADR-0011 Amendment 1 item A3: "An empty static set is not a way of
        # saying 'nothing'; `partitions=None` is the only way of saying 'let
        # the group decide'." A member that owned nothing for its whole life
        # while /readyz reported it healthy is the silent misconfiguration
        # A3 refuses. (`partitions=None` itself is unaffected -- see
        # TestInitialAssignmentIsDeliveredBeforeSubscribeReturns, which
        # subscribes without `partitions` throughout.)
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions=set())

    async def test_an_empty_static_list_is_a_value_error_too(self) -> None:
        # ASSUMPTION 5: `partitions` is an `Iterable[int]`, so the refusal
        # must not depend on the concrete type any more than the acceptance
        # of `[0]` does. A3 refuses "an empty iterable", not an empty set.
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions=[])

    async def test_a_rejected_empty_static_set_never_reports_a_claim(self) -> None:
        # A3: the refusal happens "before contacting any broker and without
        # calling the listener" -- the same rule ASSUMPTION 4 states for any
        # other rejected static set, and the reason the empty case cannot be
        # read as a quiet, ready-but-idle claim.
        bus = InMemoryBus()
        listener = RecordingListener()
        consumer = bus.consumer(GROUP)

        with pytest.raises(ValueError):
            await consumer.subscribe(TOPIC, partitions=set(), listener=listener)

        assert listener.assigned == []
        assert listener.revoked == []


class TestSubscribeWithoutAListener:
    """The listener is optional (`listener: AssignmentListener | None = None`);
    every existing `test_memory_bus.py` call site must keep working."""

    async def test_subscribe_with_no_listener_still_delivers_messages_in_order(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")
        await producer.publish(TOPIC, key="10.0.0.2", value=b"second")

        consumer = bus.consumer(GROUP)
        received = await _take(await consumer.subscribe(TOPIC), 2)

        assert [message.value for message in received] == [b"first", b"second"]

    async def test_subscribe_with_no_listener_still_commits_and_resumes(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")
        await producer.publish(TOPIC, key="10.0.0.2", value=b"second")

        consumer = bus.consumer(GROUP)
        await _take(await consumer.subscribe(TOPIC), 1)
        await consumer.commit()

        reconnected = bus.consumer(GROUP)
        resumed = await _take(await reconnected.subscribe(TOPIC), 1)

        assert resumed[0].value == b"second"


class TestTheMemoryBusNeverRevokes:
    """ADR-0011 decision 1 / assumption 22: `InMemoryBus` is single-partition
    and single-member-per-group; it has no rebalances, so a claim taken from
    it is never handed back on the bus's own initiative."""

    async def test_on_revoked_is_not_called_across_subscribe_consume_and_commit(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")
        await producer.publish(TOPIC, key="10.0.0.2", value=b"second")
        await producer.publish(TOPIC, key="10.0.0.3", value=b"third")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, listener=listener)
        await _take(stream, 2)
        await consumer.commit()
        await _take(stream, 1)
        await consumer.commit()

        assert listener.revoked == []
        assert listener.trace == ["on_assigned"]

    async def test_on_revoked_is_not_called_for_a_static_member_either(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")

        listener = RecordingListener()
        consumer = bus.consumer(GROUP)
        stream = await consumer.subscribe(TOPIC, partitions={0}, listener=listener)
        await _take(stream, 1)
        await consumer.commit()

        assert listener.revoked == []

    async def test_on_revoked_is_not_called_on_a_reconnecting_member(self) -> None:
        # The claim-then-reconnect path the aggregator takes after a
        # restart: a brand new consumer for the same group is assigned, and
        # nothing revokes the abandoned instance's claim (there is no
        # coordinator to do so). Only one member is live at a time, per
        # ADR-0011 assumption 22.
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")
        await producer.publish(TOPIC, key="10.0.0.2", value=b"second")

        first_listener = RecordingListener()
        first = bus.consumer(GROUP)
        await _take(await first.subscribe(TOPIC, listener=first_listener), 1)
        await first.commit()

        second_listener = RecordingListener()
        second = bus.consumer(GROUP)
        await second.subscribe(TOPIC, listener=second_listener)

        assert first_listener.revoked == []
        assert second_listener.revoked == []
        assert second_listener.assigned == [frozenset({(TOPIC, 0)})]


class TestConsumedMessagePartitionIsTheShardId:
    """ADR-0011 decision 1: "The aggregator never computes an IP hash of its
    own; it learns an IP's shard from `ConsumedMessage.partition`." On the
    single-partition memory bus that shard is always 0, which is why
    `HAMMERTIME_SHARD_IDS=0` is the compose/integration setting."""

    async def test_every_message_carries_partition_zero(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        for index, key in enumerate(["10.0.0.1", "10.0.0.2", "203.0.113.9"]):
            await producer.publish(TOPIC, key=key, value=f"m{index}".encode())

        consumer = bus.consumer(GROUP)
        received = await _take(await consumer.subscribe(TOPIC), 3)

        assert [message.partition for message in received] == [0, 0, 0]

    async def test_partition_is_zero_under_static_assignment_too(self) -> None:
        bus = InMemoryBus()
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")

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
        producer = bus.producer()
        await producer.publish(TOPIC, key="10.0.0.1", value=b"first")

        consumer = bus.consumer(GROUP)
        received = await _take(await consumer.subscribe(TOPIC), 1)

        assert isinstance(received[0].partition, int)


class TestAssignmentListenerProtocol:
    """ADR-0011 decision 1: `AssignmentListener` is declared
    `@runtime_checkable`, so a structural implementation passes
    `isinstance` without inheriting from it."""

    def test_a_class_with_both_coroutine_methods_is_an_instance(self) -> None:
        assert isinstance(RecordingListener(), AssignmentListener)

    def test_a_class_without_the_methods_is_not_an_instance(self) -> None:
        assert not isinstance(NotAListener(), AssignmentListener)
        assert not isinstance(object(), AssignmentListener)

    def test_a_structural_implementation_type_checks_as_the_protocol(self) -> None:
        # Static counterpart to the isinstance check above: this assignment
        # is what mypy verifies, and it is also how the listener reaches
        # `subscribe(..., listener=...)` in the tests above.
        listener: AssignmentListener = RecordingListener()

        assert listener is not None
