"""Consume observations, apply counters, evaluate state, emit transitions.

Spec: section 19, section 20, section 24, section 30, section 37, section 39

ADR-0011 decision 3 as amended by Amendment 5 item A19 (one observation, one
of seven outcomes; everything the hot path cannot use goes to reconciliation,
and a message on a partition this member does not hold is `UNCLAIMED`),
decision 6 (maintenance order, commit cadence, shutdown) and decision 7 as
amended by Amendment 3 item A12 (`apply_config` is unconditional; the version
gate is `ConfigPoller.poll_once()`'s alone).

One `asyncio.Lock` serialises everything that touches a `ShardWindow`:
message handling, the maintenance sweep, the configuration pass, and the
assignment callbacks. That is what makes ADR-0009's rule -- a new
`config_version` is visible in emitted events only after the re-evaluation it
triggered -- hold without qualification, and what keeps a rebalance from
landing in the middle of a message.

Consumption is at-least-once (ADR-0003). The counters are process-local, so a
redelivery after a crash rebuilds counters that died with the process rather
than double-counting them, and a redelivery after a handover lands in a
window that never held the first copy. The only state that survives is the
HOT set, which is idempotent under redelivery.
"""

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from hammertime.aggregator.lateness import ObservationOutcome, classify_observation
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.reevaluate import DEFAULT_BATCH_SIZE, reevaluate_shard
from hammertime.aggregator.sharding.assignment import DEFAULT_MAX_TRACKED_IPS, ShardClaims
from hammertime.aggregator.transitions import TransitionEmitter
from hammertime.aggregator.window.store import ShardWindow
from hammertime.bus.interface import ConsumedMessage, Consumer, Producer
from hammertime.bus.topics import OBSERVATIONS, OBSERVATIONS_RECONCILIATION
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import CodecError
from hammertime.core.events.codec import decode
from hammertime.core.events.models import Observation, RequestObservation
from hammertime.core.state.enums import IpState
from hammertime.core.time import buckets
from hammertime.core.time.clock import Clock
from hammertime.store.interface import ShardStateStore

logger = logging.getLogger(__name__)

#: ADR-0009 decision 9: one fixed consumer group for the aggregator. What
#: distinguishes members is the set of partitions they hold.
CONSUMER_GROUP = "hammertime-aggregator"

#: HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S's default (decision 6). Wall time:
#: it is an I/O cadence, not domain time, so it is measured on the monotonic
#: clock even when the service clock is a `ManualClock`.
DEFAULT_COMMIT_INTERVAL_S = 1.0

#: `late_messages` takes these three; `observations_rejected` the other two.
_LATE_OUTCOMES = frozenset(
    {
        ObservationOutcome.LATE,
        ObservationOutcome.FUTURE,
        ObservationOutcome.EXPIRED_BUCKET,
    }
)


class MessageBus(Protocol):
    """What the worker needs from a bus: one producer and one group consumer.

    `InMemoryBus` satisfies it directly; `service.py` adapts the Kafka
    producer/consumer pair to it, so the object graph is identical either
    way (ADR-0009 decision 3).
    """

    def producer(self) -> Producer: ...

    def consumer(self, group_id: str) -> Consumer: ...


class AggregatorWorker:
    """The aggregator's domain loop: claims, observations, maintenance, config."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        state_store: ShardStateStore,
        clock: Clock,
        config: DetectionConfig,
        metrics: AggregatorMetrics,
        shard_ids: frozenset[int] | None = None,
        max_tracked_ips: int = DEFAULT_MAX_TRACKED_IPS,
        commit_interval_s: float = DEFAULT_COMMIT_INTERVAL_S,
        reevaluation_batch: int = DEFAULT_BATCH_SIZE,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._config = config
        self._metrics = metrics
        self._shard_ids = shard_ids
        self._commit_interval_s = commit_interval_s
        self._reevaluation_batch = reevaluation_batch
        self._monotonic = monotonic
        self._producer = bus.producer()
        self._consumer = bus.consumer(CONSUMER_GROUP)
        self._claims = ShardClaims(
            state_store=state_store,
            producer=self._producer,
            consumer=self._consumer,
            clock=clock,
            config=config,
            max_tracked_ips=max_tracked_ips,
        )
        self._emitter = TransitionEmitter(
            producer=self._producer, state_store=state_store, clock=clock, metrics=metrics
        )
        # Item A13: the derived series are computed on read from the windows
        # claimed at that moment, so a revoked shard's series vanish with it.
        metrics.bind_windows(self._claims.windows)
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._stream: AsyncIterator[ConsumedMessage] | None = None
        self._last_commit = monotonic()

    # --- reads ---------------------------------------------------------------

    @property
    def config(self) -> DetectionConfig:
        """The configuration in force at the worker (item A12)."""
        return self._config

    @property
    def claims(self) -> ShardClaims:
        return self._claims

    @property
    def shards(self) -> frozenset[int]:
        return self._claims.shards

    def window(self, shard: int) -> ShardWindow | None:
        return self._claims.window(shard)

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Subscribe and hold the initial shard claims (section 47.2 readiness).

        `subscribe()` does not return until `on_assigned` has been awaited
        with the initial assignment (ADR-0011 decision 1), so a caller that
        has finished `start()` is holding its claims.
        """
        if self._stream is not None:
            return
        self._stream = await self._consumer.subscribe(
            OBSERVATIONS.name, partitions=self._shard_ids, listener=self
        )

    async def run(self) -> None:
        """Consume until `stop()`; the in-flight message is always finished."""
        stream = self._stream
        if stream is None:
            raise RuntimeError("start() must run before run()")
        stop_task = asyncio.create_task(self._stopping.wait(), name="aggregator-stop")
        try:
            while not self._stopping.is_set():
                receive_task = asyncio.create_task(self._receive(stream), name="aggregator-receive")
                done, _pending = await asyncio.wait(
                    {receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if receive_task not in done:
                    await _cancel(receive_task)
                    return
                message = receive_task.result()
                if message is None:
                    return
                await self.handle(message)
                await self._commit_if_due()
        finally:
            await _cancel(stop_task)

    async def stop(self) -> None:
        """Stop fetching, finish the in-flight message, flush and commit.

        ADR-0009 decision 7 and ADR-0011 decision 6: always flush before
        committing, so a committed position never precedes the transitions it
        produced. Idempotent, and safe before `start()`.
        """
        self._stopping.set()
        async with self._lock:
            await self._flush_and_commit()

    # --- AssignmentListener (delegated to ShardClaims under the lock) ---------

    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None:
        async with self._lock:
            await self._claims.on_assigned(partitions)

    async def on_revoked(self, partitions: frozenset[tuple[str, int]]) -> None:
        async with self._lock:
            await self._claims.on_revoked(partitions)

    # --- the three coroutines the periodic loops and the tests share ---------

    async def handle(self, message: ConsumedMessage) -> ObservationOutcome:
        """Apply one consumed message; decision 3's seven outcomes."""
        async with self._lock:
            return await self._handle(message)

    async def run_maintenance(self) -> None:
        """One expiry sweep, warm-up end and retention pass per claimed shard.

        Decision 6's order is normative: expiring before evaluating is what
        turns an expired count into a `HotIpRemoved` in the same sweep.
        """
        async with self._lock:
            for window in self._claims.windows():
                for change in window.expire_due():
                    if change.state is IpState.HOT:
                        await self._emitter.evaluate(
                            window, change.ip, config=self._config, reason="expiry"
                        )
                warmed = window.finish_warmup_if_due()
                if warmed is not None:
                    logger.info(
                        "warmup_complete shard=%d inherited_hot=%d", window.shard, len(warmed)
                    )
                    for ip in warmed:
                        await self._emitter.evaluate(
                            window, ip, config=self._config, reason="warmup"
                        )
                window.evict_due()

    async def apply_config(self, config: DetectionConfig) -> None:
        """Apply `config` and re-evaluate every tracked IP of every claimed shard.

        Unconditional (item A12): it compares no versions and returns
        nothing, so it is the `Callable[[DetectionConfig], Awaitable[None]]`
        hook `ConfigPoller` calls, and the gate -- a version that is not
        strictly greater is ignored -- lives in `poll_once()` and nowhere
        else. A direct caller passing the version already in force gets the
        pass run again, which is the only way to force one.

        Under the lock, so no observation is processed mid-pass and no event
        can carry the new version before the pass (spec section 47.3).
        """
        async with self._lock:
            windows = self._claims.windows()
            for window in windows:
                window.apply_config(config)
            transitions = 0
            for window in windows:
                transitions += await reevaluate_shard(
                    window, self._emitter, config=config, batch_size=self._reevaluation_batch
                )
            self._config = config
            self._claims.adopt_config(config)
            logger.info(
                "config_reevaluated config_version=%d transitions=%d",
                config.config_version,
                transitions,
            )

    # --- internals -----------------------------------------------------------

    @staticmethod
    async def _receive(stream: AsyncIterator[ConsumedMessage]) -> ConsumedMessage | None:
        """The next message, or None once the subscription ends."""
        try:
            return await anext(stream)
        except StopAsyncIteration:
            return None

    async def _handle(self, message: ConsumedMessage) -> ObservationOutcome:
        window = self._claims.window(message.partition)
        if window is None:
            # Decision 1: an IP's shard is its message's partition, so a
            # message for a partition this member does not hold has nothing
            # to be applied to. Item A19: the outcome is `UNCLAIMED` --
            # reachable in normal operation, because a rebalance can revoke a
            # partition between a message being fetched and being handled --
            # and the message is logged and skipped, deliberately counted
            # under no series, not decoded and not diverted. It belongs to
            # whichever member holds the partition, not to this one.
            logger.warning(
                "unclaimed_partition topic=%s partition=%d offset=%d",
                message.topic,
                message.partition,
                message.offset,
            )
            return ObservationOutcome.UNCLAIMED

        decoded = self._decode(message)
        if decoded is None:
            return ObservationOutcome.MALFORMED
        payload, entry = decoded

        # Item A9: the codec admits sub-second precision, which no
        # `bucket_seconds >= 1` can distinguish; the floor is what
        # `classify_observation` and `Clock.now()` compare in.
        window_start = math.floor(payload.window_start.timestamp())
        outcome = classify_observation(
            window_start=window_start,
            window_seconds=payload.window_seconds,
            now=self._clock.now(),
            config=self._config,
        )
        if outcome is not ObservationOutcome.APPLIED:
            await self._divert(message, entry_ip=str(entry.ip), outcome=outcome)
            return outcome

        # Item A9: floored with the *target window's* own geometry, so the
        # store always receives an aligned bucket start.
        window.observe(
            entry.ip,
            buckets.bucket_start(window_start, window.config.bucket_seconds),
            entry.request_count,
        )
        # The evaluation may yield a HOT -> COLD: `observe` subtracts a
        # slot's expired occupant before adding the delta (item A11).
        await self._emitter.evaluate(window, entry.ip, config=self._config, reason="observation")
        return ObservationOutcome.APPLIED

    def _decode(self, message: ConsumedMessage) -> tuple[RequestObservation, Observation] | None:
        """Decode and check ADR-0004's producer invariant; None is `MALFORMED`.

        Assumption 10: a message that fails the codec or the invariant cannot
        be trusted to name the IP it is keyed by, so it is dropped rather
        than diverted -- forwarding it would propagate the corruption. It is
        logged, counted and skipped; a poison message never stops the
        consumer.
        """
        try:
            envelope = decode(message.value)
        except CodecError as exc:
            self._malformed(message, str(exc))
            return None
        payload = envelope.payload
        if not isinstance(payload, RequestObservation):
            self._malformed(message, f"payload is {type(payload).__name__}")
            return None
        if len(payload.observations) != 1:
            self._malformed(
                message, f"expected exactly one observation, got {len(payload.observations)}"
            )
            return None
        entry = payload.observations[0]
        ip_text = str(entry.ip)
        if envelope.subject != ip_text:
            self._malformed(message, "envelope subject does not name the entry's IP")
            return None
        if message.key != ip_text.encode("utf-8"):
            self._malformed(message, "message key does not name the entry's IP")
            return None
        return payload, entry

    def _malformed(self, message: ConsumedMessage, reason: str) -> None:
        logger.warning(
            "malformed_observation topic=%s partition=%d offset=%d reason=%s",
            message.topic,
            message.partition,
            message.offset,
            reason,
        )
        self._metrics.increment("observations_rejected", reason=ObservationOutcome.MALFORMED.value)

    async def _divert(
        self, message: ConsumedMessage, *, entry_ip: str, outcome: ObservationOutcome
    ) -> None:
        """Republish the consumed bytes unchanged, under the same key (decision 3).

        The window store is not touched. Keeping the bytes -- and therefore
        the `event_id` -- lets a reconciliation consumer dedupe against the
        hot path (assumption 9).
        """
        await self._producer.publish(
            OBSERVATIONS_RECONCILIATION.name, key=entry_ip, value=message.value
        )
        if outcome in _LATE_OUTCOMES:
            self._metrics.increment("late_messages", reason=outcome.value)
        else:
            self._metrics.increment("observations_rejected", reason=outcome.value)

    async def _commit_if_due(self) -> None:
        now = self._monotonic()
        if now - self._last_commit < self._commit_interval_s:
            return
        async with self._lock:
            await self._flush_and_commit()

    async def _flush_and_commit(self) -> None:
        await self._producer.flush()
        await self._consumer.commit()
        self._last_commit = self._monotonic()


async def _cancel(task: "asyncio.Task[Any]") -> None:
    """Cancel a helper task and absorb its cancellation."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
