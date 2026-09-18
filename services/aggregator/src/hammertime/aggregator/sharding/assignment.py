"""Shard claim, rebalance, and handover without double-counting.

Spec: section 20, section 26, section 32, section 47.2

ADR-0011 decision 1 (a shard *is* a partition of
`hammertime.observations.v1`, so ownership is the consumer group's
assignment and there is no in-repo IP hash) and decision 5 as amended by
Amendment 6 item A20 (the shard's HOT set is durable; a claim inherits it
and warms up, and carries the handled position a commit may name for it).

`ShardClaims` is the aggregator's `AssignmentListener`. Claiming a partition
means loading that shard's `ShardState` and building its `ShardWindow` from
it; revoking one means flushing the producer, committing the handled
position and dropping the window -- nothing is written to the state store,
which is already current, and nothing is emitted, because the next owner
inherits the HOT set and warms up.

`commit_handled` (decision 6, A20) is the aggregator's only commit path: the
periodic commit, `stop()` and every revocation go through it, so a committed
position never covers a message `handle()` has not finished. The consumed
position the bus tracks is always one message ahead of that whenever a
message has been fetched and not yet handled.

The window counters start empty at a claim: every bucket older than the
claim has expired by `claim + window_seconds`, so a new owner self-heals
within one window. Until then it under-counts every inherited IP, which is
why decision 5 exempts those IPs from HOT -> COLD for that window
(`ShardWindow.in_warmup` / `is_inherited`, honoured in `transitions.py`).
"""

import logging
from collections.abc import Iterable

from hammertime.aggregator.window.store import ShardWindow
from hammertime.bus.interface import ConsumedMessage, Consumer, Producer
from hammertime.core.config.models import DetectionConfig
from hammertime.core.time.clock import Clock
from hammertime.store.interface import ShardStateStore

logger = logging.getLogger(__name__)

#: ADR-0011 decision 2 / decision 9's default (HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS).
DEFAULT_MAX_TRACKED_IPS = 1_000_000


class ShardClaims:
    """The shards this member owns, and the window each one carries."""

    __slots__ = (
        "_clock",
        "_config",
        "_consumer",
        "_handled",
        "_max_tracked_ips",
        "_producer",
        "_state_store",
        "_windows",
    )

    def __init__(
        self,
        *,
        state_store: ShardStateStore,
        producer: Producer,
        consumer: Consumer,
        clock: Clock,
        config: DetectionConfig,
        max_tracked_ips: int = DEFAULT_MAX_TRACKED_IPS,
    ) -> None:
        self._state_store = state_store
        self._producer = producer
        self._consumer = consumer
        self._clock = clock
        self._config = config
        self._max_tracked_ips = max_tracked_ips
        self._windows: dict[int, ShardWindow] = {}
        # A20: per claim, the offset a commit may name for it -- one past the
        # last message `handle()` finished under it. A claim starts without
        # one and loses it at revocation.
        self._handled: dict[tuple[str, int], int] = {}

    # --- reads ---------------------------------------------------------------

    @property
    def config(self) -> DetectionConfig:
        """The configuration a newly claimed shard's window is built with."""
        return self._config

    @property
    def shards(self) -> frozenset[int]:
        """The partitions claimed right now."""
        return frozenset(self._windows)

    def window(self, shard: int) -> ShardWindow | None:
        """The claimed shard's window, or None once it has been revoked."""
        return self._windows.get(shard)

    def windows(self) -> tuple[ShardWindow, ...]:
        """A snapshot of every claimed window; the source of the derived metrics."""
        return tuple(self._windows.values())

    def adopt_config(self, config: DetectionConfig) -> None:
        """Adopt the configuration later claims build their windows with.

        Called by `AggregatorWorker.apply_config` at the end of decision 7's
        pass, so a shard claimed after a configuration change starts on the
        version in force rather than the one this object was built with. The
        windows already claimed were re-pointed by the pass itself.
        """
        self._config = config

    # --- commits (decision 6, A20) -------------------------------------------

    def mark_handled(self, message: ConsumedMessage) -> None:
        """Record `message.offset + 1` as this claim's handled position.

        The worker calls it under the lock at the end of `handle()` for every
        outcome except `UNCLAIMED` -- `APPLIED`, the four diverted outcomes
        and `MALFORMED` all mean the worker is done with the message
        (decision 3). `UNCLAIMED` never advances it: the message was not
        handled under this claim, and advancing would commit past the
        redelivery A20 relies on.

        A partition this object does not hold is a `KeyError`, as item A15
        chose for `set_state` on an untracked IP: a missing claim is a
        missing key and a caller bug.
        """
        if message.partition not in self._windows:
            raise KeyError(message.partition)
        self._handled[message.topic, message.partition] = message.offset + 1

    async def commit_handled(self, partitions: Iterable[tuple[str, int]] | None = None) -> None:
        """Flush the producer, then commit the handled positions (decision 6).

        `partitions` defaults to every held partition; any that has no handled
        position yet is omitted, so the mapping may be empty. Both calls are
        made regardless -- the flush-before-commit rule is "every commit is
        preceded by a flush", not "every non-empty one", and a committed
        position must never precede the transitions it produced.

        This is the aggregator's only commit path (A20): the periodic commit,
        `stop()` and `on_revoked` all arrive here, so what is committed is
        never the bus's consumed position, which already sits past a message
        that has been fetched and not yet handled.
        """
        keys = self._handled if partitions is None else partitions
        offsets = {key: self._handled[key] for key in keys if key in self._handled}
        await self._producer.flush()
        await self._consumer.commit(offsets)

    # --- AssignmentListener --------------------------------------------------

    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None:
        """Claim each new partition: load its HOT set, build its window (decision 5)."""
        if not partitions:
            # Assumption 21: a group-managed empty assignment is a healthy
            # steady state (more members than partitions), not a failure.
            logger.warning("no_shards_assigned")
            return
        for _topic, shard in sorted(partitions):
            if shard in self._windows:
                continue
            state = await self._state_store.load(shard)
            self._windows[shard] = ShardWindow(
                shard=shard,
                config=self._config,
                clock=self._clock,
                inherited_hot=state.hot_ips,
                next_sequence=state.next_sequence,
                max_tracked_ips=self._max_tracked_ips,
            )
            logger.info("shard_claimed shard=%d inherited_hot=%d", shard, len(state.hot_ips))

    async def on_revoked(self, partitions: frozenset[tuple[str, int]]) -> None:
        """Give up each partition: flush, commit, drop the window (decision 5).

        The flush precedes the commit for the reason decision 6 gives for
        every commit: a committed position must never precede the
        transitions it produced. What is committed is each revoked
        partition's handled position, which by construction excludes a
        message fetched but not yet handled -- so that message is delivered
        to whichever member holds the partition next (A20). The handled
        position is dropped with the window: the claim is over.
        """
        await self.commit_handled(partitions)
        for topic, shard in sorted(partitions):
            self._handled.pop((topic, shard), None)
            if self._windows.pop(shard, None) is not None:
                logger.info("shard_revoked shard=%d", shard)
