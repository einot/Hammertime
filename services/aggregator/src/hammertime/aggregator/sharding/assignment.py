"""Shard claims, leases, and handover without double-counting.

Spec: section 20, section 26, section 32, section 47.2

ADR-0011 decision 1 (a shard *is* a partition of
`hammertime.observations.v1`, so there is no in-repo IP hash) and decision 5
(the shard's HOT set is durable; a claim inherits it and warms up), as
ADR-0013 reworks them for static assignment (ADR-0011 Amendment 7 records
what stands): decision 7 (the per-shard lease in the state store) and
decision 8 (acknowledge what was handled; no revocation path).

`ShardClaims` is the aggregator's `AssignmentListener`. Claiming a partition
means taking its lease under this member's id, loading that shard's
`ShardState` and building its `ShardWindow` from it -- strictly in that
order, one shard at a time, so a refusal on shard `q` has loaded nothing for
`q`. A refusal (another live member holds the lease) rolls back every claim
this call made and raises `ShardOwnedElsewhereError`; it is how #90's
two-owners misconfiguration is caught at start rather than as a
double-counted shard.

There is no revocation: a shard changes hands by `stop()` on one member and
`start()` on another. `release()` frees the leases at a clean stop so the
next owner can take them at once; a crashed member's leases lapse after
their TTL. `renew_leases()` runs first in every maintenance sweep, so a
member that lost a shard (its lease lapsed during a stall and another
member took it) emits nothing more for it and exits with
`ShardLeaseLostError`.

`commit_handled()` (decision 8) is the aggregator's only acknowledgement
path: the periodic commit and `stop()` both go through it, so what is
acknowledged is exactly the set of messages `handle()` finished -- never a
message merely fetched (ADR-0003 Amendment 3 item 2). The handled position
it also keeps is what recognises an `ack_wait` redelivery (ADR-0003
Amendment 3 item 1(c)); it belongs to the claim for the life of the process
and no commit resets it.

The window counters start empty at a claim: every bucket older than the
claim has expired by `claim + window_seconds`, so a new owner self-heals
within one window. Until then it under-counts every inherited IP, which is
why decision 5 exempts those IPs from HOT -> COLD for that window
(`ShardWindow.in_warmup` / `is_inherited`, honoured in `transitions.py`).
"""

import logging

from hammertime.aggregator.window.store import ShardWindow
from hammertime.bus.interface import ConsumedMessage, Consumer, Producer
from hammertime.core.config.models import DetectionConfig
from hammertime.core.time.clock import Clock
from hammertime.store.interface import ShardStateStore

logger = logging.getLogger(__name__)

#: ADR-0011 decision 2 / decision 9's default (HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS).
DEFAULT_MAX_TRACKED_IPS = 1_000_000


class ShardOwnedElsewhereError(Exception):
    """A shard this member is configured for is leased to another live member.

    Raised from `on_assigned`, so it propagates out of `subscribe()`,
    `worker.start()` and `service.start()`; it is in no transient tuple, so
    the process exits 1 with `start_failed` (ADR-0013 decision 7).
    """

    def __init__(self, shard: int, owner: str) -> None:
        super().__init__(f"shard {shard} is leased to member {owner!r}")
        self.shard = shard
        self.owner = owner


class ShardLeaseLostError(Exception):
    """A held shard's lease lapsed and another member took it.

    Raised from `renew_leases()`, so it propagates out of
    `run_maintenance()`, the service's maintenance loop and `run()`; the
    process exits 1 with `run_exited` (ADR-0013 decision 7).
    """

    def __init__(self, shard: int, owner: str) -> None:
        super().__init__(f"lease on shard {shard} was lost to member {owner!r}")
        self.shard = shard
        self.owner = owner


class ShardClaims:
    """The shards this member owns, the window and the lease each one carries."""

    __slots__ = (
        "_clock",
        "_config",
        "_consumer",
        "_handled",
        "_lease_ttl_s",
        "_leased",
        "_max_tracked_ips",
        "_member_id",
        "_positions",
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
        member_id: str,
        lease_ttl_s: float,
        max_tracked_ips: int = DEFAULT_MAX_TRACKED_IPS,
    ) -> None:
        self._state_store = state_store
        self._producer = producer
        self._consumer = consumer
        self._clock = clock
        self._config = config
        self._member_id = member_id
        self._lease_ttl_s = lease_ttl_s
        self._max_tracked_ips = max_tracked_ips
        self._windows: dict[int, ShardWindow] = {}
        # The shards whose lease this member currently holds. Windows outlive
        # a `release()` (decision 8: nothing reads them after `stop()` except
        # tests), so this is tracked apart from `_windows`: a renewal after a
        # release must not quietly take the shards back from the next owner.
        self._leased: set[int] = set()
        # Decision 8: every handled, unacknowledged message, in delivery
        # order, for the next `commit_handled()`.
        self._handled: list[ConsumedMessage] = []
        # Per shard, one past the highest handled offset. Never reset by a
        # commit and never lowered (Amendment 1 ruling T4).
        self._positions: dict[int, int] = {}

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
        """The claimed shard's window, or None for a shard this member holds no window for."""
        return self._windows.get(shard)

    def windows(self) -> tuple[ShardWindow, ...]:
        """A snapshot of every claimed window; the source of the derived metrics."""
        return tuple(self._windows.values())

    def handled_position(self, shard: int) -> int | None:
        """One past the last handled offset on `shard`; None if none, or if `shard` is not held.

        The read surface is total (Amendment 1 ruling T3): an unheld shard
        answers `None` exactly like a held shard nothing has been handled on,
        matching `window()`; only the write surface (`mark_handled`) raises.
        The position outlives every `commit_handled()` (ruling T4): it is kept
        for the redelivery test of ADR-0003 Amendment 3, not for any commit.
        """
        return self._positions.get(shard)

    def adopt_config(self, config: DetectionConfig) -> None:
        """Adopt the configuration later claims build their windows with.

        Called by `AggregatorWorker.apply_config` at the end of decision 7's
        pass, so a shard claimed after a configuration change starts on the
        version in force rather than the one this object was built with. The
        windows already claimed were re-pointed by the pass itself.
        """
        self._config = config

    # --- acknowledgements (ADR-0013 decision 8) ------------------------------

    def mark_handled(self, message: ConsumedMessage) -> None:
        """Record `message` for the next acknowledgement; raise the handled position past it.

        The worker calls it under the lock at the end of `handle()` for every
        outcome except `UNCLAIMED` -- `APPLIED`, the four diverted outcomes,
        `MALFORMED` and `REDELIVERED` all mean the worker is done with the
        message. `UNCLAIMED` never reaches here: the message was not handled
        under this claim and must be redelivered to whoever holds it.

        The position becomes `max(<current>, message.offset + 1)`, so a
        redelivered copy (whose offset is already below it) leaves it where
        it is. A partition this object does not hold is a `KeyError` (ADR-0011
        A20, unchanged): a missing claim is a missing key and a caller bug.
        """
        if message.partition not in self._windows:
            raise KeyError(message.partition)
        self._handled.append(message)
        position = message.offset + 1
        current = self._positions.get(message.partition)
        if current is None or position > current:
            self._positions[message.partition] = position

    async def commit_handled(self) -> None:
        """Flush the producer, then acknowledge every handled message; then clear the list.

        Both calls are made even when the list is empty (`ack([])` returns
        normally without touching the broker), so the flush-then-ack trace
        is uniform: ADR-0009 decision 7's rule is "every commit is preceded
        by a flush", and an acknowledgement must never precede the
        transitions its messages produced.

        The list is handed to `ack()` as it is, in delivery order, a
        `REDELIVERED` copy alongside its original: the bus acknowledges a
        message named twice once (ADR-0013 decision 3 as amended), so nothing
        is deduplicated here. This is the aggregator's only acknowledgement
        path: the periodic commit and `stop()` both arrive here, so a message
        fetched but not yet handled is never covered.
        """
        await self._producer.flush()
        await self._consumer.ack(tuple(self._handled))
        self._handled.clear()

    # --- leases (ADR-0013 decision 7) ----------------------------------------

    async def renew_leases(self) -> None:
        """Re-take the lease of every held shard; a refusal is `ShardLeaseLostError`.

        `AggregatorWorker.run_maintenance()` calls this first, before the
        expiry sweep, under the worker lock, so a member that has lost a
        shard emits nothing more for it. This is the narrow fencing ADR-0011
        A2 declined: checked once per maintenance interval, with no fencing
        token on `record_transition` itself.
        """
        for shard in sorted(self._leased):
            owner = await self._state_store.acquire_lease(shard, self._member_id, self._lease_ttl_s)
            if owner is not None:
                logger.error(
                    "shard_lease_lost shard=%d owner=%s member=%s", shard, owner, self._member_id
                )
                raise ShardLeaseLostError(shard, owner)

    async def release(self) -> None:
        """Drop the lease of every held shard; the windows stay in memory.

        `AggregatorWorker.stop()` calls this after the final
        `commit_handled()`, so a clean stop hands the shards over at once.
        Nothing is written to the state store beyond the lease keys and
        nothing is emitted: the next owner inherits the HOT set and warms up.
        """
        leased, self._leased = sorted(self._leased), set()
        for shard in leased:
            await self._state_store.release_lease(shard, self._member_id)

    # --- AssignmentListener --------------------------------------------------

    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None:
        """Claim each partition: take its lease, load its HOT set, build its window.

        Strictly sequential per shard, in sorted order: acquire `p`, load
        `p`, build `p`'s window, and only then the next shard's acquire
        (Amendment 1 ruling T6), so a refusal on shard `q` has loaded nothing
        for `q` and can undo exactly the shards before it. A non-`None`
        result from `acquire_lease` is a refusal: it is logged, every lease
        and window this call made is rolled back, and
        `ShardOwnedElsewhereError` propagates out of `subscribe()`.

        An empty set is a `ValueError` (ruling T5): it is unreachable through
        the bus, which refuses an empty static set before calling the
        listener, and a member holding nothing must not report itself ready.

        A shard is skipped only while this member holds its lease **and**
        has its window: a claim is a lease plus a window, and anything less
        is claimed by the one path every claim takes (ADR-0013 decision 7 as
        amended by Amendment 4 ruling R6 and corrected by Amendment 5 ruling
        1). So a shard left leased but unwindowed by a `load()` that raised
        (Amendment 3 ruling (d) keeps that lease) is claimed to completion
        here, rather than skipped for ever with no window. The re-acquire on
        a shard this member already leases is a renewal -- `acquire_lease`
        grants iff no live lease exists or the live one is the owner's own
        -- and it is kept rather than skipped because the lease may have
        lapsed and moved between the fault and this call, in which case the
        acquire refuses and the member exits 1 with `shard_owned_elsewhere`
        (assumption 84). A shard whose window survived a `release()` but
        whose lease is gone is claimed afresh: lease acquired, state
        loaded, a new window built in place of the old one
        (its counters are stale and its inherited set is not the store's
        current HOT set; assumption 70); its handled position is kept, as
        nothing ever lowers it.
        """
        if not partitions:
            raise ValueError(
                "on_assigned() with an empty partition set: a member must hold a shard"
            )
        claimed: list[int] = []
        for _topic, shard in sorted(partitions):
            if shard in self._leased and shard in self._windows:
                continue
            owner = await self._state_store.acquire_lease(shard, self._member_id, self._lease_ttl_s)
            if owner is not None:
                logger.error(
                    "shard_owned_elsewhere shard=%d owner=%s member=%s",
                    shard,
                    owner,
                    self._member_id,
                )
                for taken in claimed:
                    self._windows.pop(taken, None)
                    self._leased.discard(taken)
                    await self._state_store.release_lease(taken, self._member_id)
                raise ShardOwnedElsewhereError(shard, owner)
            claimed.append(shard)
            self._leased.add(shard)
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
