"""Consume observations, apply counters, evaluate state, emit transitions.

Spec: section 30, section 39

ADR-0011 decision 4 is the whole of this module: the per-observation pipeline
of spec section 39, run under one lock so a configuration swap can never
interleave with a message, with flush-before-commit batching on the consume
loop. ADR-0009 decisions 3/6/7 name the coroutines the (not yet existing)
service process drives -- `run_maintenance`, `apply_config`, `run`, `stop` --
so Epic A composes these rather than reopening them.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from hammertime.aggregator.lateness import Disposition, classify, window_start_epoch
from hammertime.aggregator.reevaluate import ReevaluationReport, reevaluate
from hammertime.aggregator.transitions import TransitionPublisher, build_event, decide
from hammertime.aggregator.window.expiry import evict_idle, expire_buckets
from hammertime.aggregator.window.store import InMemoryWindowStore, IpEntry, StoreFullError
from hammertime.bus.interface import ConsumedMessage, Consumer, Producer
from hammertime.bus.topics import OBSERVATIONS, OBSERVATIONS_RECONCILIATION
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import CodecError
from hammertime.core.events.codec import decode
from hammertime.core.events.models import RequestObservation
from hammertime.core.time.buckets import bucket_start
from hammertime.core.time.clock import Clock

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AggregatorMetrics:
    """The spec section 37 counters this worker maintains in process.

    A plain counter set, not a Prometheus registry: `core/telemetry/metrics.py`
    is still a stub and the telemetry epic maps these onto it (ADR-0011
    decision 8).
    """

    observations_applied: int = 0
    #: Malformed -- undecodable bytes, a payload of the wrong type, an entry
    #: count other than one, or `request_count < 1` -- which is never
    #: republished; plus a store with no room, which is (ADR-0011 decisions
    #: 2 and 3). Never applied to a window.
    observations_rejected: int = 0
    late_messages: int = 0
    future_messages: int = 0
    expired_on_arrival: int = 0
    #: One per message diverted to `hammertime.observations-reconciliation.v1`.
    reconciliation_published: int = 0
    cold_to_hot_transitions: int = 0
    hot_to_cold_transitions: int = 0
    evicted_ips: int = 0
    #: `apply_config` calls that actually applied a document (ADR-0009
    #: decision 6: strictly greater versions only).
    config_reloads: int = 0


class AggregatorWorker:
    """One aggregator shard's domain loop over `hammertime.observations.v1`.

    Owns the window store, the transition publisher and the configuration in
    force. It does **not** own a process: no readiness flag, no HTTP server,
    no maintenance timer and no config poller live here (ADR-0011 decision 8).
    """

    __slots__ = (
        "_clock",
        "_commit_every",
        "_config",
        "_finished",
        "_lock",
        "_producer",
        "_running",
        "_stop_requested",
        "metrics",
        "publisher",
        "store",
    )

    def __init__(
        self,
        *,
        config: DetectionConfig,
        store: InMemoryWindowStore,
        producer: Producer,
        producer_id: str,
        clock: Clock,
        initial_sequence: int = 0,
        commit_every: int = 100,
    ) -> None:
        if commit_every < 1:
            raise ValueError(f"commit_every must be at least 1, got {commit_every}")
        self._config = config
        self.store = store
        self.publisher = TransitionPublisher(
            producer, producer_id=producer_id, initial_sequence=initial_sequence
        )
        self.metrics = AggregatorMetrics()
        self._producer = producer
        self._clock = clock
        self._commit_every = commit_every
        # One lock over apply_message / run_maintenance / apply_config, so a
        # re-evaluation never interleaves with an observation and ADR-0009
        # decision 6's visibility rule holds by construction.
        self._lock = asyncio.Lock()
        self._stop_requested = asyncio.Event()
        self._finished = asyncio.Event()
        self._running = False

    @property
    def config(self) -> DetectionConfig:
        """The detection configuration currently in force."""
        return self._config

    # -- the per-observation pipeline (spec section 39) --------------------

    async def apply_message(self, message: ConsumedMessage) -> None:
        """Apply one consumed observation, or divert it, or reject it.

        A message is **well-formed** when all four of ADR-0011 decision 2's
        rules hold, checked in this order: it decodes; its payload is a
        `RequestObservation`; `payload.observations` carries *exactly one*
        entry (ADR-0004 decision 2's shape, which its decision 4 grants a
        consumer the right to assert); and that entry's `request_count >= 1`.

        A well-formed message is applied to the window **or** republished to
        `hammertime.observations-reconciliation.v1` under its original key and
        original bytes -- exactly one of the two, never both, never neither. A
        message that fails any rule is *rejected* before its lateness is
        judged: `observations_rejected += 1`, one warning, no disposition
        counter, and neither topic. A poison message must not wedge a
        partition, and it is not a well-formed observation the aggregator
        declined, so it has nothing to reconcile.
        """

        async with self._lock:
            await self._apply_message(message)

    async def _apply_message(self, message: ConsumedMessage) -> None:
        now = self._clock.now()
        try:
            envelope = decode(message.value)
        except CodecError:
            self.metrics.observations_rejected += 1
            logger.warning(
                "discarding undecodable observation at %s/%d offset %d",
                message.topic,
                message.partition,
                message.offset,
            )
            return

        payload = envelope.payload
        if not isinstance(payload, RequestObservation):
            self.metrics.observations_rejected += 1
            logger.warning(
                "discarding %s on %s: expected a RequestObservation",
                envelope.event_type,
                message.topic,
            )
            return

        # ADR-0011 decision 2 rule 3: exactly one entry. Every entry other than
        # the key's IP was routed to this shard by a key that is not its own,
        # so a multi-entry payload is rejected whole rather than partly
        # applied -- which is also what makes "applied XOR reconciled" true,
        # since a later entry meeting a full store would otherwise republish a
        # message whose first entry had already been counted.
        if len(payload.observations) != 1:
            self.metrics.observations_rejected += 1
            logger.warning(
                "discarding an observation on %s carrying %d entries: exactly one is required",
                message.topic,
                len(payload.observations),
            )
            return

        entry = payload.observations[0]
        # ADR-0011 decision 2 rule 4: a non-positive delta carries no count, so
        # it is refused before the lateness policy is consulted -- ingest never
        # publishes one (ADR-0008), but the aggregator must not trust that.
        if entry.request_count < 1:
            self.metrics.observations_rejected += 1
            logger.warning(
                "discarding an observation for %s on %s: request_count %d is not positive",
                entry.ip,
                message.topic,
                entry.request_count,
            )
            return

        window_start = window_start_epoch(payload.window_start)
        disposition = classify(window_start, now, self._config)
        if disposition is not Disposition.APPLY:
            self._count_disposition(disposition)
            await self._reconcile(message)
            return

        start = bucket_start(window_start, self._config.bucket_seconds)
        if not await self._apply_entry(entry.ip, entry.request_count, start=start, now=now):
            await self._reconcile(message)

    async def _apply_entry(self, ip: Address, delta: int, *, start: int, now: int) -> bool:
        """Add `delta` to `ip`'s bucket `start`; False if the store had no room."""

        try:
            entry = self.store.get_or_create(ip, now=now)
        except StoreFullError:
            # ADR-0011 decision 3: a full store counts as a rejection *and*
            # diverts the message, so the observation stays recoverable.
            self.metrics.observations_rejected += 1
            logger.warning("window store is full; diverting %s to reconciliation", ip)
            return False

        window_count = entry.counter.observe(start, delta, now=now)
        entry.last_observed = now
        self.metrics.observations_applied += 1
        await self._publish_transition(ip, entry, window_count=window_count, now=now)
        return True

    async def _publish_transition(
        self, ip: Address, entry: IpEntry, *, window_count: int, now: int
    ) -> None:
        """Re-judge one entry at `window_count` and publish its edge, if any."""

        transition = decide(entry.state, window_count, self._config)
        if transition is None:
            return
        entry.state = transition.current
        await self.publisher.publish(
            build_event(ip, transition, window_count=window_count, config=self._config, now=now)
        )
        if transition.became_hot:
            self.metrics.cold_to_hot_transitions += 1
        else:
            self.metrics.hot_to_cold_transitions += 1

    def _count_disposition(self, disposition: Disposition) -> None:
        """Name the cause of a diversion in the spec section 37 counters."""

        if disposition is Disposition.LATE:
            self.metrics.late_messages += 1
        elif disposition is Disposition.FUTURE:
            self.metrics.future_messages += 1
        elif disposition is Disposition.EXPIRED:
            self.metrics.expired_on_arrival += 1

    async def _reconcile(self, message: ConsumedMessage) -> None:
        """Republish the original bytes under the original key, unchanged.

        The reconciliation consumer sees the very envelope the aggregator
        declined, `event_id` and all, so nothing is silently dropped (spec
        section 24, ADR-0002). A message with no key at all -- which ingest
        never publishes, since ADR-0004 keys every observation by its IP -- is
        republished under an empty key, the closest the `Producer` interface
        allows to "no key".
        """

        await self._producer.publish(
            OBSERVATIONS_RECONCILIATION.name,
            message.key if message.key is not None else b"",
            message.value,
        )
        self.metrics.reconciliation_published += 1

    # -- maintenance and configuration (ADR-0009 decisions 3, 6) -----------

    async def run_maintenance(self) -> None:
        """Expire buckets, publish what that moved, then evict idle entries.

        Expiry runs before eviction so a HOT IP whose window has just emptied
        emits its `HotIpRemoved` before it can ever be considered idle
        (ADR-0011 decision 4; spec sections 5 and 26).
        """

        async with self._lock:
            now = self._clock.now()
            for ip in expire_buckets(self.store, now=now):
                entry = self.store.get(ip)
                if entry is None:  # pragma: no cover - expiry never drops an entry
                    continue
                await self._publish_transition(ip, entry, window_count=entry.counter.total, now=now)
            self.metrics.evicted_ips += evict_idle(
                self.store,
                now=now,
                state_retention_seconds=self._config.state_retention_seconds,
            )
            await self.publisher.flush()

    async def apply_config(self, config: DetectionConfig) -> ReevaluationReport | None:
        """Adopt `config` if it advances the version, re-evaluating first.

        ADR-0009 decision 6: a document is applied only when its
        `config_version` is *strictly* greater than the one in force, and the
        new version becomes visible in emitted events only after the
        re-evaluation has been applied -- so `self._config` is swapped once
        `reevaluate` returns, while the transitions that pass emits already
        carry the incoming version (they are built from `current`). Returns
        `None` for a document that does not advance the version: nothing is
        changed and no reload is counted.
        """

        async with self._lock:
            if config.config_version <= self._config.config_version:
                logger.info(
                    "ignoring detection config v%d: v%d is already in force",
                    config.config_version,
                    self._config.config_version,
                )
                return None

            report = await reevaluate(
                self.store,
                previous=self._config,
                current=config,
                now=self._clock.now(),
                publisher=self.publisher,
            )
            self._config = config
            self.metrics.config_reloads += 1
            self.metrics.cold_to_hot_transitions += report.became_hot
            self.metrics.hot_to_cold_transitions += report.became_cold
            return report

    # -- the consume loop (ADR-0011 decision 4, ADR-0009 decision 7) -------

    async def run(self, consumer: Consumer, *, topic: str = OBSERVATIONS.name) -> None:
        """Consume `topic` until `stop()`, committing every `commit_every`.

        Flush-before-commit, the same order ADR-0010 decision 3 requires of
        the trie: the producer is flushed and only then is the consumer
        position committed, so a committed offset never lies ahead of a
        transition that has not reached the log. A final flush and commit run
        on the way out -- except on cancellation, which is abort and not
        shutdown: `CancelledError` propagates, nothing is flushed or
        committed, and the uncommitted batch is redelivered at least once
        (ADR-0011 decision 4, ADR-0009 decision 7's amendment). Whichever way
        this leaves, the worker is left so that a concurrent or later `stop()`
        returns rather than waits forever.
        """

        self._running = True
        self._finished.clear()
        since_commit = 0
        cancelled = False
        stop_waiter: asyncio.Future[Any] = asyncio.ensure_future(self._stop_requested.wait())
        try:
            iterator = await consumer.subscribe(topic)
            while not self._stop_requested.is_set():
                message = await self._next_message(iterator, stop_waiter)
                if message is None:
                    break
                await self.apply_message(message)
                since_commit += 1
                if since_commit >= self._commit_every:
                    await self._flush_and_commit(consumer)
                    since_commit = 0
        except asyncio.CancelledError:
            # Cancelling this task is abort, not shutdown: the error is
            # re-raised untouched and the final drain below is skipped
            # (ADR-0011 decision 4, ADR-0009 decision 7's amendment).
            cancelled = True
            raise
        finally:
            stop_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_waiter
            try:
                # Skipping the commit is always safe: at-least-once redelivery
                # re-applies the uncommitted batch (ADR-0003), while a commit
                # taken here could lie ahead of an unpublished transition.
                if not cancelled:
                    await self._flush_and_commit(consumer)
            finally:
                # Set even when the final flush is itself cancelled, so a
                # concurrent stop() can never wait forever.
                self._running = False
                self._finished.set()

    async def _next_message(
        self, iterator: AsyncIterator[ConsumedMessage], stop_waiter: "asyncio.Future[Any]"
    ) -> ConsumedMessage | None:
        """The next message, or `None` once `stop()` has been requested.

        A bus read blocks until a message exists, so the read is raced against
        the stop signal rather than polled. When the stop wins, the read is
        cancelled -- but a message it had already produced in the same tick is
        still returned, so stopping can never drop a message whose consumer
        position has already advanced.

        Cancelling the *reader* is this method's own business; cancelling the
        task running `run()` is not, and the two are indistinguishable to an
        `except CancelledError` around the read. So there is none: the
        reader's outcome is inspected rather than awaited, which reads a
        cancelled read as `None` while letting an external cancellation
        propagate (ADR-0011 decision 4). Either way no pending `anext` task is
        left behind.
        """

        reader = asyncio.ensure_future(anext(iterator))
        waiters: set[asyncio.Future[Any]] = {reader, stop_waiter}
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if not reader.done():
                reader.cancel()
                await asyncio.wait({reader})
        except asyncio.CancelledError:
            reader.cancel()
            raise
        if reader.cancelled():
            return None
        try:
            return reader.result()
        except StopAsyncIteration:
            return None

    async def _flush_and_commit(self, consumer: Consumer) -> None:
        """Flush every published transition, then commit the read position."""

        await self.publisher.flush()
        await consumer.commit()

    async def stop(self) -> None:
        """Ask `run()` to return, and wait until it has (ADR-0009 decision 7)."""

        self._stop_requested.set()
        if self._running:
            await self._finished.wait()
