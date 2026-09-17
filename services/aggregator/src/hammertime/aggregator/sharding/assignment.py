"""Shard claim, rebalance, and handover without double-counting.

Spec: section 20, section 26, section 32, section 47.2

ADR-0011 decision 1 (a shard *is* a partition of
`hammertime.observations.v1`, so ownership is the consumer group's
assignment and there is no in-repo IP hash) and decision 5 (the shard's HOT
set is durable; a claim inherits it and warms up).

`ShardClaims` is the aggregator's `AssignmentListener`. Claiming a partition
means loading that shard's `ShardState` and building its `ShardWindow` from
it; revoking one means flushing the producer, committing the consumer
position and dropping the window -- nothing is written to the state store,
which is already current, and nothing is emitted, because the next owner
inherits the HOT set and warms up.

The window counters start empty at a claim: every bucket older than the
claim has expired by `claim + window_seconds`, so a new owner self-heals
within one window. Until then it under-counts every inherited IP, which is
why decision 5 exempts those IPs from HOT -> COLD for that window
(`ShardWindow.in_warmup` / `is_inherited`, honoured in `transitions.py`).
"""

import logging

from hammertime.aggregator.window.store import ShardWindow
from hammertime.bus.interface import Consumer, Producer
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
        transitions it produced.
        """
        await self._producer.flush()
        await self._consumer.commit()
        for _topic, shard in sorted(partitions):
            if self._windows.pop(shard, None) is not None:
                logger.info("shard_revoked shard=%d", shard)
