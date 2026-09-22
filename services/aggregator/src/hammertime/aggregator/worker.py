"""Consume observations, apply counters, evaluate state, emit transitions.

Spec: section 19, section 20, section 24, section 30, section 37, section 39

ADR-0011 decision 3 as amended by Amendment 5 item A19 and ADR-0013 decision
8 (one observation, one of eight outcomes; everything the hot path cannot
use goes to reconciliation, a message is `UNCLAIMED` when this member holds
no window for its partition, and `REDELIVERED` when its offset is below the
claim's handled position), decision 6 as ADR-0013 reworks it (maintenance
order with the lease renewal first, acknowledgement cadence, shutdown) and
decision 7 as amended by Amendment 3 item A12 (`apply_config` is
unconditional; the version gate is `ConfigPoller.poll_once()`'s alone).

One `asyncio.Lock` serialises everything that touches a `ShardWindow`:
message handling, the maintenance sweep, the configuration pass, and the
assignment callback. That is what makes ADR-0009's rule -- a new
`config_version` is visible in emitted events only after the re-evaluation it
triggered -- hold without qualification.

Consumption is at-least-once (ADR-0003). The counters are process-local, so a
redelivery after a crash rebuilds counters that died with the process rather
than double-counting them, and a redelivery after a handover lands in a
window that never held the first copy. A redelivery *within* a live claim --
the log handing out a delivered, unacknowledged message again after
`ack_wait` -- is recognised by its offset and acknowledged without being
applied (ADR-0003 Amendment 3 item 1(c)). The only state that survives a
process is the HOT set, which is idempotent under redelivery.
"""

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from hammertime.aggregator.lateness import ObservationOutcome, classify_observation
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.reevaluate import DEFAULT_BATCH_SIZE, reevaluate_shard
from hammertime.aggregator.sharding.assignment import DEFAULT_MAX_TRACKED_IPS, ShardClaims
from hammertime.aggregator.transitions import TransitionEmitter
from hammertime.aggregator.window.store import ShardWindow
from hammertime.bus.interface import ConsumedMessage, MessageBus
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

#: ADR-0013 decision 8's defaults for the lease keywords, so every existing
#: construction stays valid; `build_service` passes the settings' values.
DEFAULT_MEMBER_ID = "aggregator"
DEFAULT_LEASE_TTL_S = 30.0

#: `late_messages` takes these three; `observations_rejected` the other two.
_LATE_OUTCOMES = frozenset(
    {
        ObservationOutcome.LATE,
        ObservationOutcome.FUTURE,
        ObservationOutcome.EXPIRED_BUCKET,
    }
)


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
        member_id: str = DEFAULT_MEMBER_ID,
        lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
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
            member_id=member_id,
            lease_ttl_s=lease_ttl_s,
            max_tracked_ips=max_tracked_ips,
        )
        self._emitter = TransitionEmitter(
            producer=self._producer, state_store=state_store, clock=clock, metrics=metrics
        )
        # Item A13: the derived series are computed on read from the windows
        # claimed at that moment.
        metrics.bind_windows(self._claims.windows)
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        # `stop()` runs its sequence once; a later call takes the lock and
        # returns (ADR-0013 decision 8 as amended by Amendment 4 ruling R4).
        self._stopped = False
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
        """Subscribe and hold the shard claims and leases (section 47.2 readiness).

        `subscribe()` does not return until `on_assigned` has been awaited
        with the static set (ADR-0013 decision 3), so a caller that has
        finished `start()` is holding its claims and their leases. A shard
        another member holds is `ShardOwnedElsewhereError` out of here
        (decision 7). `shard_ids=None` is passed through as `partitions=None`
        -- every partition as the bus defines it, `{0}` on `InMemoryBus` --
        which is the in-process test shape, never a production one (decision
        6): `build_service` always passes the settings' explicit set.
        """
        if self._stream is not None:
            return
        self._stream = await self._consumer.subscribe(
            OBSERVATIONS.name, partitions=self._shard_ids, listener=self
        )

    async def run(self) -> None:
        """Consume until `stop()`; the in-flight message is always finished.

        On a wake-up in which the stop signal and a received message are
        both complete, the message is not handed to `handle()`: it was
        yielded, so the consumer's `close()` hands it to the next member
        (ADR-0013 decision 8 as amended by Amendment 4 ruling R2, assumption
        82). Handling it here would put it after the final commit.

        That skip drops the *message* only. A receive that completed with an
        *exception* in the same wake-up is re-raised as itself (Amendment 5
        ruling 2; assumption 82 as narrowed): a message is redelivered, an
        error is not, so swallowing it would let `run_exited` report a clean
        stop for a process that lost its transport.
        """
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
                if stop_task in done:
                    if not receive_task.done():
                        await _cancel(receive_task)
                    else:
                        # Completed in the same wake-up: its message is
                        # dropped (close() hands it to the next member), but
                        # an exception is re-raised -- nothing redelivers an
                        # error (Amendment 5 ruling 2). Reading it also marks
                        # the outcome retrieved, so asyncio reports no
                        # unretrieved exception at teardown.
                        exc = receive_task.exception()
                        if exc is not None:
                            raise exc
                    return
                message = receive_task.result()
                if message is None:
                    return
                await self.handle(message)
                await self._commit_if_due()
        finally:
            await _cancel(stop_task)

    async def stop(self) -> None:
        """Stop fetching, finish the message in hand, `commit_handled()`, `release()`.

        ADR-0013 decision 8: the final flush-and-ack covers exactly what
        `handle()` finished, and the lease release right after it is what
        lets the next owner take the shards at once (decision 7). The
        consumer is *not* closed here: closing is the service's step after
        `stop()` returns, so the final acknowledgement goes out on a live
        consumer and the `nak` of whatever was fetched but never yielded
        follows it.

        `stop()`'s order is total (Amendment 4 rulings R2 and R4): from the
        moment the stop flag is set, every path that could act on a shard --
        `handle()`, `run_maintenance()`, `apply_config()`, the periodic
        commit -- checks it under the lock and stands down, so the
        `commit_handled()` here is the last acknowledgement this worker
        sends. The message in hand (a `_handle` holding the lock when the
        flag is set) is finished and covered by it; one merely waiting for
        the lock is not in hand and takes the `UNCLAIMED` path. The sequence
        runs once, with `release()` in a `finally` so the leases are freed
        even when the final acknowledgement fails (the exception propagates
        after the release); a later call -- the runner's `_stop_quietly`
        after a crash exit, by which time the bus is closed and `ack(())`
        would raise -- takes the lock and returns without touching the bus
        or the store. That is what "idempotent" means for it. Safe before
        `start()`.
        """
        self._stopping.set()
        async with self._lock:
            if self._stopped:
                return
            try:
                await self._flush_and_ack()
            finally:
                self._stopped = True
                await self._claims.release()

    # --- AssignmentListener (delegated to ShardClaims under the lock) ---------

    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None:
        async with self._lock:
            await self._claims.on_assigned(partitions)

    # --- the three coroutines the periodic loops and the tests share ---------

    async def handle(self, message: ConsumedMessage) -> ObservationOutcome:
        """Apply one consumed message; decision 3's outcomes, plus `REDELIVERED`.

        After `stop()` has begun the outcome is `UNCLAIMED` whatever the
        partition (ADR-0013 decision 8 as amended by Amendment 4 ruling R2):
        the member holds no lease, so every partition is one it must not act
        on. Not decoded, not applied, not emitted, not marked handled -- the
        message stays unacknowledged and `close()` hands it to the next
        member.
        """
        async with self._lock:
            if self._stopping.is_set():
                logger.warning(
                    "unclaimed_partition topic=%s partition=%d offset=%d",
                    message.topic,
                    message.partition,
                    message.offset,
                )
                return ObservationOutcome.UNCLAIMED
            return await self._handle(message)

    async def run_maintenance(self) -> None:
        """Renew the leases, then one expiry sweep, warm-up end and retention pass per shard.

        ADR-0013 decision 7 puts the renewal first, so a member that has lost
        a shard emits nothing more for it (`ShardLeaseLostError` propagates
        and the sweep never runs). Decision 6's order after that is
        normative: expiring before evaluating is what turns an expired count
        into a `HotIpRemoved` in the same sweep.

        After `stop()` has begun this renews nothing and sweeps nothing: it
        returns without touching the store, the windows or the producer
        (Amendment 3 ruling (c); Amendment 4 ruling R2).
        """
        async with self._lock:
            if self._stopping.is_set():
                return
            await self._claims.renew_leases()
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

        After `stop()` has begun it is a total no-op: the worker's `config`
        is left as it was and no `config_reevaluated` is logged (Amendment 4
        ruling R2, assumption 63).
        """
        async with self._lock:
            if self._stopping.is_set():
                return
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

    async def _receive(self, stream: AsyncIterator[ConsumedMessage]) -> ConsumedMessage | None:
        """The next message; None once the subscription has ended."""
        try:
            return await anext(stream)
        except StopAsyncIteration:
            return None

    async def _handle(self, message: ConsumedMessage) -> ObservationOutcome:
        window = self._claims.window(message.partition)
        if window is None:
            # Decision 1: an IP's shard is its message's partition, so a
            # message for a partition this member holds no window for has
            # nothing to be applied to. Item A19: the outcome is `UNCLAIMED`
            # -- reachable only through a direct `handle()` call under static
            # assignment (ADR-0013 decision 8) -- and the message is logged
            # and skipped, deliberately counted under no series, not decoded,
            # not diverted and not marked handled: it belongs to whichever
            # member holds the partition, not to this one.
            logger.warning(
                "unclaimed_partition topic=%s partition=%d offset=%d",
                message.topic,
                message.partition,
                message.offset,
            )
            return ObservationOutcome.UNCLAIMED

        position = self._claims.handled_position(message.partition)
        if position is not None and message.offset < position:
            # ADR-0013 decision 8 / ADR-0003 Amendment 3 item 1(c): delivery
            # within a partition is in stream-sequence order, so a copy at
            # or below the last handled offset is the log redelivering a
            # delivered, unacknowledged message after `ack_wait`. It is
            # marked handled so the copy is acknowledged at the next commit
            # (the position, already past it, is unchanged) and otherwise
            # untouched: not decoded, not classified, not diverted, not
            # counted, the store untouched.
            logger.warning(
                "redelivered_observation topic=%s partition=%d offset=%d delivery_count=%d",
                message.topic,
                message.partition,
                message.offset,
                message.delivery_count,
            )
            self._claims.mark_handled(message)
            return ObservationOutcome.REDELIVERED

        decoded = self._decode(message)
        if decoded is None:
            self._claims.mark_handled(message)
            return ObservationOutcome.MALFORMED
        event_id, payload, entry = decoded

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
            await self._divert(message, entry_ip=str(entry.ip), event_id=event_id, outcome=outcome)
            self._claims.mark_handled(message)
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
        self._claims.mark_handled(message)
        return ObservationOutcome.APPLIED

    def _decode(
        self, message: ConsumedMessage
    ) -> tuple[str, RequestObservation, Observation] | None:
        """Decode and check ADR-0004's producer invariant; None is `MALFORMED`.

        Returns the envelope's `event_id` alongside the payload and its one
        entry: a divert republishes under that id (ADR-0013 decision 4).

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
        return envelope.event_id, payload, entry

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
        self,
        message: ConsumedMessage,
        *,
        entry_ip: str,
        event_id: str,
        outcome: ObservationOutcome,
    ) -> None:
        """Republish the consumed bytes unchanged, under the same key (decision 3).

        The window store is not touched. Keeping the bytes -- and therefore
        the `event_id` -- lets a reconciliation consumer dedupe against the
        hot path (assumption 9); passing that `event_id` as the `message_id`
        lets the log itself drop a second divert of the same observation
        inside its duplicate window (ADR-0013 decision 4).
        """
        await self._producer.publish(
            OBSERVATIONS_RECONCILIATION.name,
            key=entry_ip,
            value=message.value,
            message_id=event_id,
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
            if self._stopping.is_set():
                # The commit inside `stop()` is the last acknowledgement
                # this worker sends (Amendment 4 ruling R2, assumption 66).
                return
            await self._flush_and_ack()

    async def _flush_and_ack(self) -> None:
        """Flush, then acknowledge every handled message (ADR-0013 decision 8).

        `ShardClaims.commit_handled` is the only acknowledgement path: the
        periodic commit and `stop()` arrive here, so a message in hand at a
        commit is never covered by it.
        """
        await self._claims.commit_handled()
        self._last_commit = self._monotonic()


async def _cancel(task: "asyncio.Task[Any]") -> None:
    """Cancel a helper task and absorb its cancellation."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
