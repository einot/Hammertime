"""Single-writer positional replay worker applying HotIpAdded / HotIpRemoved.

Spec: section 19, section 22, section 28, section 33, section 46.5; ADR-0017
decisions 3-10, 12 and 17, Amendment 2 (rulings 3-7) and Amendment 3 (rulings 1
and 4) (ADR-0013 decision 9 and Amendment 13, ADR-0014 A12, ADR-0015 decision 6,
ADR-0015 Amendment 5 ruling 7).

Consumption (decision 3). One positional, whole-topic subscription to
`hammertime.hot-ip.v1` (`partitions=None`, no listener), starting at
`state.event_sequence` as it stands after readiness step 3 below. Nothing is
ever acknowledged; the service closes the consumer, with the bus, after
`stop()`.

Readiness (decision 4; ADR-0013 decision 9 as amended by Amendment 10).
`start()` reads `end_offset`, then `first_offset`, both *before* it
subscribes, and keeps `end` as `replay_target`. If `first` is past
`event_sequence`, the log holds no record the trie has not read below
`first` -- aged out, purged, or never written -- so, under the lock and
unless `stop()` has begun, it passes them with `state.note_passed(first -
1)`. Between the two (step 2a, Amendment 3 ruling 1) it reads the last value
of `hammertime.prefix-stats.v1` and sets `republish_from`: that message's
envelope `sequence` when it decodes to a `PrefixStatsChanged` published under
`AGENT_ID` whose `sequence` is at most `replay_target`, and `0` otherwise, the
refusal logged as `prefix_stats_last_ignored`. It then subscribes from
`event_sequence` and hands messages to `handle()` until the worker is caught
up: `event_sequence >= end`. A log that holds nothing to replay is therefore
caught up at once.

One message, one of six outcomes (decision 6), decided in this order:
`REDELIVERED` (offset not past the position), `MALFORMED` (codec refusal,
payload not a hot-ip event, key or subject not the payload's IP),
`FAMILY_NOT_SERVED`, then `APPLIED` / `UNCHANGED` from the coupled apply step;
`STOPPED` once `stop()` has begun. Only `REDELIVERED`, `STOPPED` and an
`InvariantViolation` leave the log position where it was (decision 8).
`InvariantViolation` is logged and propagates (decision 7): the process exits
1 and its restart is the rebuild.

Publishing (Amendment 2 ruling 3, Amendment 3 ruling 1). When the apply step
changed the hot set and `state.event_sequence` is at least `republish_from`
(read as `0` while it is `None`), two steps follow step 5: 5a,
`publisher.prepare()`, in the same synchronous section as the apply and
`note_applied`; and 5b, `await publisher.publish()`, still under the worker's
lock. The outcome is `APPLIED` once the publish has returned. Any other
outcome publishes nothing, and neither does an event that changed the hot set
below `republish_from`: its stats reached the log under an earlier start. The
worker catches only `PrefixStatsPublishError`: it logs `prefix_stats_publish_failed` and re-raises
it (ruling 5), so the process exits 1 and the restart re-publishes. There is
no retry. Startup replay goes through `handle()`, so it publishes from
`republish_from` on, and `start()` returns only once the last replayed
message's publish has returned (ruling 4). `stop()` sets the flag, takes the
lock, and awaits the producer's flush (ruling 6 as amended by Amendment 3
ruling 4); it keeps no "stopped" state.

Atomicity (decision 9). Single-writer ownership on one event loop is the
mechanism; there is no copy-on-write and no versioned snapshot pointer. The
worker changes `TrieState` only inside `handle()` and `apply_config()`, and
`start()`'s step 3 is one `note_passed` call under the lock. The apply step
(`apply_hot_ip_added` / `apply_hot_ip_removed`, both synchronous),
`state.note_applied` and `publisher.prepare()` run with no `await` between
them (R1; `prepare()` only reads). `handle()` awaits the worker's lock before
that section and the publish after it; the publish writes nothing to the
state. Every reader
takes what it reports without an `await` between its first read and its last
(R2), and no other thread touches the state (R3). A reader therefore always
sees the state between two whole events (section 28).

Log records (decision 12) go through the standard-library logger
`hammertime.trie.worker` as `"<event> key=value ..."` and carry fixed tokens
and numbers only -- never a payload value, an attribute document or an
exception's text.

`apply_hot_ip_added` and `apply_hot_ip_removed` are module globals here and
are looked up when called, so a test can replace them (ADR-0017 Test seams).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from enum import Enum, StrEnum
from typing import Any, Final

from hammertime.bus.interface import ConsumedMessage, MessageBus
from hammertime.bus.topics import HOT_IP, PREFIX_STATS
from hammertime.core.addressing.address import AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import CodecError, InvalidAttributesError, InvariantViolation
from hammertime.core.events.codec import decode
from hammertime.core.events.models import HotIpAdded, HotIpRemoved, PrefixStatsChanged
from hammertime.trie.metadata.ip_attributes import apply_hot_ip_added, apply_hot_ip_removed
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.publisher import (
    AGENT_ID,
    DEFAULT_MIN_PREFIX_LENGTHS,
    PrefixStatsPublisher,
    PrefixStatsPublishError,
    PreparedStats,
)
from hammertime.trie.state import FamilyState, TrieState

logger = logging.getLogger(__name__)

#: ADR-0009 decision 9's name for the trie. A positional subscription creates
#: no durable, so the name reaches no broker (ADR-0010 Amendment 1 ruling 1).
CONSUMER_GROUP: Final = "hammertime-trie"


class HotIpOutcome(StrEnum):
    """What `handle()` did with one message (ADR-0017 decision 6)."""

    #: The event changed the hot set.
    APPLIED = "applied"
    #: A redundant add or remove: counts untouched, record replaced or deleted.
    UNCHANGED = "unchanged"
    #: Skipped: undecodable, not a hot-ip event, or key/subject not its IP.
    MALFORMED = "malformed"
    #: Skipped: the event's address family is not held by this process.
    FAMILY_NOT_SERVED = "family_not_served"
    #: Offset not past the position: skipped, position unchanged.
    REDELIVERED = "redelivered"
    #: Handed to `handle()` after `stop()` began: nothing done.
    STOPPED = "stopped"


class _Malformed(StrEnum):
    """Decision 6 step 2's reason tokens, the only text `malformed_hot_ip_event` carries."""

    INVALID_ATTRIBUTES = "invalid_attributes"
    CODEC = "codec"
    PAYLOAD_TYPE = "payload_type"
    KEY_MISMATCH = "key_mismatch"
    SUBJECT_MISMATCH = "subject_mismatch"


class _LastIgnored(StrEnum):
    """Amendment 3 ruling 1's reason tokens, the only text `prefix_stats_last_ignored` carries."""

    CODEC = "codec"
    PAYLOAD_TYPE = "payload_type"
    AGENT_ID = "agent_id"
    AHEAD_OF_LOG = "ahead_of_log"


class _Pump(Enum):
    """How a pass of `_pump` ended."""

    DONE = "done"
    STOPPED = "stopped"
    ENDED = "ended"


class TrieWorker:
    """The trie's single writer: replays and follows the hot-ip log into `TrieState`."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        state: TrieState,
        metrics: TrieMetrics,
        min_prefix_lengths: Mapping[AddressFamily, int] = DEFAULT_MIN_PREFIX_LENGTHS,
    ) -> None:
        self._bus = bus
        self._state = state
        self._metrics = metrics
        self._consumer = bus.consumer(CONSUMER_GROUP)
        # The producer is taken once, as the consumer is (Amendment 2 ruling 3).
        self._publisher = PrefixStatsPublisher(
            bus.producer(), metrics=metrics, min_prefix_lengths=min_prefix_lengths
        )
        # Publishes that returned, for `replay_complete` (ruling 7).
        self._published = 0
        metrics.bind_state(state)
        # One lock for everything that touches the state: `handle()`,
        # `apply_config()` and `stop()` (decision 6).
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._stream: AsyncIterator[ConsumedMessage] | None = None
        self._replay_target: int | None = None
        # Set by `start()`'s step 2a (Amendment 3 ruling 1).
        self._republish_from: int | None = None
        # Families already warned about as not served (decision 5).
        self._unserved_seen: set[AddressFamily] = set()

    # --- reads ---------------------------------------------------------------

    @property
    def state(self) -> TrieState:
        return self._state

    @property
    def metrics(self) -> TrieMetrics:
        return self._metrics

    @property
    def replay_target(self) -> int | None:
        """The log end `start()` read before subscribing; `None` before it."""
        return self._replay_target

    @property
    def republish_from(self) -> int | None:
        """The value `start()`'s step 2a set; `None` before it (Amendment 3 ruling 1).

        Steps 5a and 5b run only for an event whose `event_sequence` is at
        least this, read as `0` while it is `None`.
        """
        return self._republish_from

    @property
    def caught_up(self) -> bool:
        """Decision 4: `False` until subscribed, then `event_sequence >= replay_target`."""
        end = self._replay_target
        if end is None or self._stream is None:
            return False
        return self._state.event_sequence >= end

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Read the log's bounds, pass what it no longer holds, subscribe, replay (decision 4).

        A second call returns at once. If the subscription ends before the
        worker is caught up this is a `RuntimeError`; if `stop()` begins
        first, this returns without being caught up. Exceptions from
        `end_offset`, `first_offset`, `last_value`, `subscribe`, the
        iterator and `handle()` propagate; a last prefix-stats message the
        worker cannot use is not an exception (Amendment 3 ruling 1).
        """
        # 1. Already subscribed.
        if self._stream is not None:
            return
        # 2. The end first, then the first retained offset, both before
        # subscribing ("Why `end` is read first").
        end = await self._bus.end_offset(HOT_IP.name)
        self._replay_target = end
        first = await self._bus.first_offset(HOT_IP.name)
        # 2a. Where the prefix-stats log's stats reach (Amendment 3 ruling 1).
        last = await self._bus.last_value(PREFIX_STATS.name)
        self._republish_from = _republish_from(last, replay_target=end)
        # 3. Pass every offset below `first` the trie has not read (R1: one
        # `note_passed` under the lock), unless `stop()` has begun.
        if first > self._state.event_sequence:
            async with self._lock:
                if not self._stopping.is_set() and first > self._state.event_sequence:
                    self._state.note_passed(first - 1)
        published_before = self._published
        # 4. Subscribe from the next offset the trie will read.
        start_offset = self._state.event_sequence
        stream = await self._consumer.subscribe(HOT_IP.name, start_offset=start_offset)
        self._stream = stream

        # 5. Replay until caught up.
        ended = await self._pump(stream, until=lambda: self.caught_up)
        if ended is _Pump.STOPPED:
            return
        if ended is _Pump.ENDED:
            raise RuntimeError(
                "the hot-ip subscription ended before the replay reached the log end"
            )
        # 6. Replay complete.
        logger.info(
            "replay_complete start_offset=%d first_offset=%d replay_target=%d event_sequence=%d"
            " prefix_stats_published=%d republish_from=%d",
            start_offset,
            first,
            end,
            self._state.event_sequence,
            self._published - published_before,
            self._republish_from,
        )

    async def run(self) -> None:
        """Hand every message to `handle()` until `stop()`, or until the iterator ends.

        A message received in the same wake-up as the stop signal is not
        handed to `handle()`: the next start reads it again from
        `position + 1`. An exception from the iterator in that wake-up is
        re-raised, since nothing redelivers an error.
        """
        stream = self._stream
        if stream is None:
            raise RuntimeError("start() must run before run()")
        await self._pump(stream, until=lambda: False)

    async def stop(self) -> None:
        """Set the stop flag, finish the message in hand, flush; idempotent, safe before `start()`.

        Three steps, taken by every call (Amendment 2 ruling 6 as amended by
        Amendment 3 ruling 4): set the stop flag; take the lock, which waits
        for the message in hand, every publish of its stats included; await
        the producer's flush, whose exception propagates. The worker keeps
        no "stopped" state: everything that changes once `stop()` has begun
        follows from the flag. Does not close the consumer: the service does
        that, with the bus, after this returns (decision 13). The snapshot
        epic adds the final snapshot after the flush.
        """
        self._stopping.set()
        async with self._lock:
            await self._publisher.flush()

    async def handle(self, message: ConsumedMessage) -> HotIpOutcome:
        """Decide and carry out one message's outcome (decision 6, Amendment 2 ruling 3).

        After the lock, steps 1-5 and 5a are one synchronous section
        (decision 9, R1). Step 5b, the publish of an `APPLIED` event's stats,
        is awaited after it, still under the lock.
        """
        async with self._lock:
            if self._stopping.is_set():
                return HotIpOutcome.STOPPED
            outcome, prepared = self._handle(message)
            if prepared is not None:
                # 5b. Publish.
                await self._publish(message, prepared)
            # 6. Outcome.
            return outcome

    async def apply_config(self, config: DetectionConfig) -> None:
        """Adopt `config` under the lock, unless `stop()` has begun (decision 10).

        Unconditional: it compares no versions. The poller alone gates them.
        """
        async with self._lock:
            if self._stopping.is_set():
                return
            self._state.adopt_config(config)

    # --- internals -----------------------------------------------------------

    async def _pump(
        self, stream: AsyncIterator[ConsumedMessage], *, until: Callable[[], bool]
    ) -> _Pump:
        """Take messages and `handle()` each until `until()` holds, `stop()`, or the end."""
        stop_task = asyncio.create_task(self._stopping.wait(), name="trie-stop")
        try:
            while not until():
                if self._stopping.is_set():
                    return _Pump.STOPPED
                receive_task = asyncio.create_task(_receive(stream), name="trie-receive")
                done, _pending = await asyncio.wait(
                    {receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    if not receive_task.done():
                        await _cancel(receive_task)
                    else:
                        # The message is dropped (the next start reads it
                        # again); an exception is not.
                        exc = receive_task.exception()
                        if exc is not None:
                            raise exc
                    return _Pump.STOPPED
                message = receive_task.result()
                if message is None:
                    return _Pump.ENDED
                await self.handle(message)
            return _Pump.DONE
        finally:
            await _cancel(stop_task)

    async def _publish(self, message: ConsumedMessage, prepared: PreparedStats) -> None:
        """Step 5b: publish, and log and re-raise a `PrefixStatsPublishError` (ruling 5).

        The record carries fixed tokens and numbers only: never a prefix, a
        payload value or an exception's text (decision 12, ruling 7).
        """
        try:
            await self._publisher.publish(prepared)
        except PrefixStatsPublishError as exc:
            first = exc.__cause__ if exc.__cause__ is not None else exc
            logger.error(
                "prefix_stats_publish_failed family=%s topic=%s partition=%d offset=%d"
                " sequence=%d attempted=%d failed=%d error_type=%s",
                prepared.family.value,
                message.topic,
                message.partition,
                message.offset,
                exc.sequence,
                exc.attempted,
                exc.failed,
                f"{type(first).__module__}.{type(first).__qualname__}",
            )
            raise
        self._published += len(prepared.messages)

    def _handle(self, message: ConsumedMessage) -> tuple[HotIpOutcome, PreparedStats | None]:
        """Decision 6 steps 1-5, and 5a when the hot set changed. Synchronous (R1).

        Step 5a runs only when, in addition, `event_sequence` is at least
        `republish_from`, read as `0` while it is `None` (Amendment 3 ruling
        1). Returns the outcome, and the stats step 5b publishes, or `None`
        when there is nothing to publish.
        """
        state = self._state
        position = state.position

        # 1. Redelivered.
        if position is not None and message.offset <= position:
            logger.warning(
                "redelivered_hot_ip_event topic=%s partition=%d offset=%d position=%d",
                message.topic,
                message.partition,
                message.offset,
                position,
            )
            return HotIpOutcome.REDELIVERED, None

        # 2. Decode.
        decoded = self._decode(message)
        if isinstance(decoded, _Malformed):
            if decoded is _Malformed.INVALID_ATTRIBUTES:
                self._metrics.increment("attributes_rejected", stage="decode")
            self._metrics.increment("hot_ip_events_skipped", reason="malformed")
            logger.warning(
                "malformed_hot_ip_event topic=%s partition=%d offset=%d reason=%s",
                message.topic,
                message.partition,
                message.offset,
                decoded.value,
            )
            state.note_handled(message.offset)
            return HotIpOutcome.MALFORMED, None
        payload = decoded

        # 3. Family.
        family = payload.ip.family
        if not state.serves(family):
            self._metrics.increment("hot_ip_events_skipped", reason="family_not_served")
            level = logging.DEBUG if family in self._unserved_seen else logging.WARNING
            self._unserved_seen.add(family)
            logger.log(
                level,
                "family_not_served family=%s topic=%s partition=%d offset=%d",
                family.value,
                message.topic,
                message.partition,
                message.offset,
            )
            state.note_handled(message.offset)
            return HotIpOutcome.FAMILY_NOT_SERVED, None

        # 4. Apply.
        fs = state.of(family)
        try:
            if isinstance(payload, HotIpAdded):
                event_type = "HotIpAdded"
                changed = self._apply_added(fs, message, payload)
            else:
                event_type = "HotIpRemoved"
                # A removal's attributes are neither stored nor logged.
                changed = apply_hot_ip_removed(fs.trie, fs.records, payload.ip)
        except InvariantViolation:
            logger.error(
                "trie_invariant_violation family=%s topic=%s partition=%d offset=%d",
                family.value,
                message.topic,
                message.partition,
                message.offset,
            )
            raise

        # 5. Record -- no `await` since the apply step (R1).
        state.note_applied(message.offset, payload.timestamp)
        outcome = HotIpOutcome.APPLIED if changed else HotIpOutcome.UNCHANGED
        self._metrics.increment(
            "trie_updates", family=family, event_type=event_type, result=outcome.value
        )

        # 5a. Prepare -- still no `await` since the apply step (R1). An event
        # below `republish_from` had its stats published under an earlier
        # start (Amendment 3 ruling 1).
        if not changed or state.event_sequence < (self._republish_from or 0):
            return outcome, None
        prepared = self._publisher.prepare(
            fs.trie,
            payload.ip,
            sequence=state.event_sequence,
            config_version=state.config.config_version,
            timestamp=payload.timestamp,
        )
        return outcome, prepared

    def _apply_added(self, fs: FamilyState, message: ConsumedMessage, payload: HotIpAdded) -> bool:
        """`apply_hot_ip_added`; an attributes rejection is retried with the default document.

        Decision 7 / ADR-0015 decision 6: never dropped, retried as is,
        dead-lettered or rebuilt. From the bus this cannot happen -- the
        codec runs the same validator -- so the path serves ADR-0015 decision
        5's other writers.

        ADR-0015 Amendment 5 ruling 7 / ADR-0017 decision 17: both calls,
        including the `attributes=None` retry, pass
        `request_count=payload.window_count`.
        """
        try:
            return apply_hot_ip_added(
                fs.trie,
                fs.records,
                payload.ip,
                payload.attributes,
                request_count=payload.window_count,
            )
        except InvalidAttributesError:
            self._metrics.increment("attributes_rejected", stage="apply")
            logger.warning(
                "hot_ip_attributes_rejected topic=%s partition=%d offset=%d stage=apply",
                message.topic,
                message.partition,
                message.offset,
            )
            return apply_hot_ip_added(
                fs.trie, fs.records, payload.ip, None, request_count=payload.window_count
            )

    def _decode(self, message: ConsumedMessage) -> HotIpAdded | HotIpRemoved | _Malformed:
        """Decision 6 step 2: the payload, or the reason token it is `MALFORMED` for.

        Never keeps or logs the codec's message: it can embed payload values
        (ADR-0017 decision 12, assumption 8).
        """
        try:
            envelope = decode(message.value)
        except CodecError as exc:
            if isinstance(exc.__cause__, InvalidAttributesError):
                return _Malformed.INVALID_ATTRIBUTES
            return _Malformed.CODEC
        payload = envelope.payload
        if not isinstance(payload, HotIpAdded | HotIpRemoved):
            return _Malformed.PAYLOAD_TYPE
        ip_text = str(payload.ip)
        if message.key != ip_text.encode("utf-8"):
            return _Malformed.KEY_MISMATCH
        if envelope.subject != ip_text:
            return _Malformed.SUBJECT_MISMATCH
        return payload


def _republish_from(last: bytes | None, *, replay_target: int) -> int:
    """Amendment 3 ruling 1's table: where a start resumes publishing.

    The rows are checked in order and the first that applies decides. A
    refusal is logged as `prefix_stats_last_ignored` with its reason token
    and `replay_target` only: never the message's `sequence`, nor anything
    else it holds, and never the codec's text (decision 12).
    """
    if last is None:
        return 0
    reason: _LastIgnored
    try:
        envelope = decode(last)
    except CodecError:
        reason = _LastIgnored.CODEC
    else:
        if not isinstance(envelope.payload, PrefixStatsChanged):
            reason = _LastIgnored.PAYLOAD_TYPE
        elif envelope.agent_id != AGENT_ID:
            reason = _LastIgnored.AGENT_ID
        elif envelope.sequence > replay_target:
            reason = _LastIgnored.AHEAD_OF_LOG
        else:
            return envelope.sequence
    logger.warning(
        "prefix_stats_last_ignored reason=%s replay_target=%d", reason.value, replay_target
    )
    return 0


async def _receive(stream: AsyncIterator[ConsumedMessage]) -> ConsumedMessage | None:
    """The next message; `None` once the subscription has ended."""
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None


async def _cancel(task: asyncio.Task[Any]) -> None:
    """Cancel a helper task and absorb its cancellation."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
