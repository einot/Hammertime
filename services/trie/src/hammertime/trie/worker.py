"""Single-writer positional replay worker applying HotIpAdded / HotIpRemoved.

Spec: section 19, section 22, section 28, section 33, section 46.5; ADR-0017
decisions 3-10 and 12 (ADR-0013 decision 9, ADR-0014 A12, ADR-0015 decision 6).

Consumption (decision 3). One positional, whole-topic subscription to
`hammertime.hot-ip.v1` (`partitions=None`, no listener), starting at `0` on a
fresh state and at `position + 1` on a restored one. Nothing is ever
acknowledged; the service closes the consumer, with the bus, after `stop()`.

Readiness (decision 4). `start()` reads `end_offset` *before* it subscribes
and keeps it as `replay_target`, then hands messages to `handle()` until the
worker is caught up -- `end <= start_offset`, or `event_sequence >= end`.

One message, one of six outcomes (decision 6), decided in this order:
`REDELIVERED` (offset not past the position), `MALFORMED` (codec refusal,
payload not a hot-ip event, key or subject not the payload's IP),
`FAMILY_NOT_SERVED`, then `APPLIED` / `UNCHANGED` from the coupled apply step;
`STOPPED` once `stop()` has begun. Only `REDELIVERED`, `STOPPED` and an
`InvariantViolation` leave the log position where it was (decision 8).
`InvariantViolation` is logged and propagates (decision 7): the process exits
1 and its restart is the rebuild.

Atomicity (decision 9). Single-writer ownership on one event loop is the
mechanism; there is no copy-on-write and no versioned snapshot pointer. The
worker changes `TrieState` only inside `handle()` and `apply_config()`, and
`handle()` awaits nothing but the worker's lock: the apply step
(`apply_hot_ip_added` / `apply_hot_ip_removed`, both synchronous) and
`state.note_applied` run with no `await` between them (R1). Every reader
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
from collections.abc import AsyncIterator, Callable
from enum import Enum, StrEnum
from typing import Any, Final

from hammertime.bus.interface import ConsumedMessage, MessageBus
from hammertime.bus.topics import HOT_IP
from hammertime.core.addressing.address import AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import CodecError, InvalidAttributesError, InvariantViolation
from hammertime.core.events.codec import decode
from hammertime.core.events.models import HotIpAdded, HotIpRemoved
from hammertime.trie.metadata.ip_attributes import apply_hot_ip_added, apply_hot_ip_removed
from hammertime.trie.metrics import TrieMetrics
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


class _Pump(Enum):
    """How a pass of `_pump` ended."""

    DONE = "done"
    STOPPED = "stopped"
    ENDED = "ended"


class TrieWorker:
    """The trie's single writer: replays and follows the hot-ip log into `TrieState`."""

    def __init__(self, *, bus: MessageBus, state: TrieState, metrics: TrieMetrics) -> None:
        self._bus = bus
        self._state = state
        self._metrics = metrics
        self._consumer = bus.consumer(CONSUMER_GROUP)
        metrics.bind_state(state)
        # One lock for everything that touches the state: `handle()`,
        # `apply_config()` and `stop()` (decision 6).
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._stopped = False
        self._stream: AsyncIterator[ConsumedMessage] | None = None
        self._replay_target: int | None = None
        self._start_offset: int | None = None
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
    def caught_up(self) -> bool:
        """Decision 4: `end <= start_offset`, or `event_sequence >= end`."""
        end = self._replay_target
        start = self._start_offset
        if end is None or start is None:
            return False
        return end <= start or self._state.event_sequence >= end

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Read the log end, subscribe positionally, replay until caught up (decision 4).

        A second call returns at once. If the subscription ends before the
        worker is caught up this is a `RuntimeError`; if `stop()` begins
        first, this returns without being caught up. Exceptions from
        `end_offset`, `subscribe`, the iterator and `handle()` propagate.
        """
        if self._stream is not None:
            return
        end = await self._bus.end_offset(HOT_IP.name)
        self._replay_target = end
        position = self._state.position
        start_offset = 0 if position is None else position + 1
        stream = await self._consumer.subscribe(HOT_IP.name, start_offset=start_offset)
        self._start_offset = start_offset
        self._stream = stream

        ended = await self._pump(stream, until=lambda: self.caught_up)
        if ended is _Pump.STOPPED:
            return
        if ended is _Pump.ENDED:
            raise RuntimeError(
                "the hot-ip subscription ended before the replay reached the log end"
            )
        logger.info(
            "replay_complete start_offset=%d replay_target=%d event_sequence=%d",
            start_offset,
            end,
            self._state.event_sequence,
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
        """Set the stop flag, then finish the message in hand; idempotent, safe before `start()`.

        Does not close the consumer: the service does that, with the bus,
        after this returns (decision 13). Slice 2 adds the producer flush
        after the lock is taken, and the snapshot epic the final snapshot.
        """
        self._stopping.set()
        async with self._lock:
            self._stopped = True

    async def handle(self, message: ConsumedMessage) -> HotIpOutcome:
        """Decide and carry out one message's outcome (decision 6).

        The lock is the only thing awaited: everything after it is one
        synchronous section (decision 9, R1).
        """
        async with self._lock:
            if self._stopping.is_set():
                return HotIpOutcome.STOPPED
            return self._handle(message)

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

    def _handle(self, message: ConsumedMessage) -> HotIpOutcome:
        """Decision 6 steps 1-6. Synchronous by construction (R1)."""
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
            return HotIpOutcome.REDELIVERED

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
            return HotIpOutcome.MALFORMED
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
            return HotIpOutcome.FAMILY_NOT_SERVED

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

        # 6. Outcome.
        return outcome

    def _apply_added(self, fs: FamilyState, message: ConsumedMessage, payload: HotIpAdded) -> bool:
        """`apply_hot_ip_added`; an attributes rejection is retried with the default document.

        Decision 7 / ADR-0015 decision 6: never dropped, retried as is,
        dead-lettered or rebuilt. From the bus this cannot happen -- the
        codec runs the same validator -- so the path serves ADR-0015 decision
        5's other writers.
        """
        try:
            return apply_hot_ip_added(fs.trie, fs.records, payload.ip, payload.attributes)
        except InvalidAttributesError:
            self._metrics.increment("attributes_rejected", stage="apply")
            logger.warning(
                "hot_ip_attributes_rejected topic=%s partition=%d offset=%d stage=apply",
                message.topic,
                message.partition,
                message.offset,
            )
            return apply_hot_ip_added(fs.trie, fs.records, payload.ip, None)

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
