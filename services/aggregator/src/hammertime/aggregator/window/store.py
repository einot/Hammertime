"""Counter storage: in-memory for a shard-local loop, Redis when state must outlive the process.

Spec: section 5, section 20, section 26

ADR-0011 decision 2, as amended by Amendment 2 (items A4-A9, A11): every
claimed shard has one `ShardWindow` in process memory holding, per tracked
IP, an `IpCounter` (the section 5 ring), the IP's `IpState`, `last_seen` (the
event time of the newest applied bucket) and an `inherited` flag (decision
5). Nothing per-observation touches Redis; only the HOT set outlives the
process, and that is the state store's job.

The store never evaluates a threshold: it reports what it holds and lets the
caller decide (decision 4). What bounds it is bucket expiry, retention
(section 26) and the `max_tracked_ips` cap.
"""

import heapq
import itertools
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from hammertime.aggregator.window.counter import IpCounter
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.state.enums import IpState
from hammertime.core.time import buckets
from hammertime.core.time.clock import Clock

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TRACKED_IPS = 1_000_000


@dataclass(frozen=True, slots=True)
class WindowChange:
    """One IP's window total moving, reported to whoever must evaluate it."""

    ip: Address
    #: The IP's state before any evaluation; the store never evaluates.
    state: IpState
    total_before: int
    total_after: int


@dataclass(slots=True)
class _Entry:
    """Everything the store holds for one tracked IP."""

    counter: IpCounter
    state: IpState
    #: Event time of the newest applied bucket (`clock.now()` for an inherited
    #: entry that has not been observed yet, item A5).
    last_seen: int
    inherited: bool


class ShardWindow:
    """The window counters of one shard, bounded by expiry, retention and a cap."""

    def __init__(
        self,
        *,
        shard: int,
        config: DetectionConfig,
        clock: Clock,
        inherited_hot: Iterable[Address] = (),
        next_sequence: int = 0,
        max_tracked_ips: int = _DEFAULT_MAX_TRACKED_IPS,
    ) -> None:
        if max_tracked_ips <= 0:
            raise ValueError(f"max_tracked_ips must be positive, got {max_tracked_ips!r}")
        self.shard = shard
        #: Decision 4; loaded from the state store at claim time (decision 5).
        self.next_sequence = next_sequence
        #: Decision 8: `window_evictions{shard,reason="capacity"|"retention"}`.
        self.capacity_evictions = 0
        self.retention_evictions = 0
        self._config = config
        self._clock = clock
        self._max_tracked_ips = max_tracked_ips
        self._entries: dict[Address, _Entry] = {}
        # Heap tie-breaker, so two entries with equal keys never compare
        # `Address` objects (which are unordered).
        self._tie_break = itertools.count()
        # Expiry schedule, in the style of `MemoryDedupStore._expiry_heap`:
        # `_expiry_scheduled[ip]` is the earliest expiry pushed for that IP and
        # not yet consumed, so a sweep costs O(expiring IPs * log n) instead of
        # a scan of the store.
        self._expiry_heap: list[tuple[int, int, Address]] = []
        self._expiry_scheduled: dict[Address, int] = {}
        # Retention/capacity schedule, ordered by `last_seen`. An entry whose
        # key no longer matches `entry.last_seen` is re-pushed with the current
        # value when it surfaces, so each tracked IP keeps exactly one pending
        # heap entry however often it is observed.
        self._last_seen_heap: list[tuple[int, int, Address]] = []
        now = clock.now()
        for ip in frozenset(inherited_hot):
            # Item A5: an inherited HOT IP is a tracked entry from
            # construction -- empty ring, `last_seen = now`, a cap slot. The
            # cap is not enforced here: a shard whose inherited set alone
            # exceeds it starts over capacity, and the capacity rule deals with
            # that on the next new IP.
            self._track(ip, state=IpState.HOT, last_seen=now, inherited=True)
        self._warm_until: int | None = now + config.window_seconds if self._entries else None

    # --- geometry and warm-up ------------------------------------------------

    @property
    def config(self) -> DetectionConfig:
        return self._config

    @property
    def warm_until(self) -> int | None:
        """`clock.now() + window_seconds` at construction iff a HOT set was inherited."""
        return self._warm_until

    @property
    def in_warmup(self) -> bool:
        return self._warm_until is not None and self._clock.now() < self._warm_until

    def finish_warmup_if_due(self) -> frozenset[Address] | None:
        """The inherited IPs that are still HOT, once, at `warm_until`; None otherwise.

        Clears every `inherited` flag and `warm_until` on the one call that
        returns a set, so the caller (decision 5) announces the shard's
        inherited HOT set exactly once.
        """
        if self._warm_until is None or self._clock.now() < self._warm_until:
            return None
        still_hot = frozenset(
            ip
            for ip, entry in self._entries.items()
            if entry.inherited and entry.state is IpState.HOT
        )
        for entry in self._entries.values():
            entry.inherited = False
        self._warm_until = None
        return still_hot

    # --- reads ---------------------------------------------------------------

    def total(self, ip: Address) -> int:
        """The IP's running window total; 0 when untracked."""
        entry = self._entries.get(ip)
        return 0 if entry is None else entry.counter.total

    def state(self, ip: Address) -> IpState:
        """The IP's state; COLD when untracked."""
        entry = self._entries.get(ip)
        return IpState.COLD if entry is None else entry.state

    def is_tracked(self, ip: Address) -> bool:
        return ip in self._entries

    def is_inherited(self, ip: Address) -> bool:
        entry = self._entries.get(ip)
        return entry is not None and entry.inherited

    def hot_ips(self) -> frozenset[Address]:
        return frozenset(ip for ip, entry in self._entries.items() if entry.state is IpState.HOT)

    def tracked_ips(self) -> frozenset[Address]:
        return frozenset(self._entries)

    @property
    def tracked_count(self) -> int:
        return len(self._entries)

    @property
    def active_count(self) -> int:
        """Tracked IPs whose window total is above zero (section 37)."""
        return sum(1 for entry in self._entries.values() if entry.counter.total > 0)

    @property
    def hot_count(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.state is IpState.HOT)

    # --- writes --------------------------------------------------------------

    def observe(self, ip: Address, bucket_start: int, delta: int) -> WindowChange | None:
        """Apply `delta` to `ip`'s bucket at `bucket_start`, creating the entry if needed.

        `bucket_start` reaches the counter unchanged: the worker floors it with
        this window's own `bucket_seconds` (decision 3), and an unaligned value
        is the counter's `ValueError` (item A9). A negative delta is the same.
        Neither changes anything.

        Returns the change -- including for `delta == 0`, which creates the
        entry and refreshes `last_seen` like any other applied observation
        (item A8) -- or None when the bucket is not live, past or future, in
        which case the store is untouched: no entry created, no `last_seen`
        refresh (item A4). `total_after` may be *below* `total_before` when the
        slot held a bucket that had already left the window (item A11).
        """
        entry = self._entries.get(ip)
        counter = self._new_counter() if entry is None else entry.counter
        total_before = counter.total
        if not counter.observe(bucket_start, delta, self._clock.now()):
            return None
        total_after = counter.total
        if entry is None:
            self._evict_for_capacity()
            entry = self._track(ip, state=IpState.COLD, last_seen=bucket_start, counter=counter)
        else:
            entry.last_seen = max(entry.last_seen, bucket_start)
        self._schedule_expiry(ip, entry)
        return WindowChange(
            ip=ip, state=entry.state, total_before=total_before, total_after=total_after
        )

    def set_state(self, ip: Address, state: IpState) -> None:
        """Record the state the caller evaluated; COLD clears the `inherited` flag."""
        entry = self._entries[ip]
        entry.state = state
        if state is IpState.COLD:
            entry.inherited = False
            # A demoted IP is a retention candidate again, and its schedule
            # entry may have been discarded while it was HOT.
            self._push_last_seen(ip, entry)

    def expire_due(self) -> list[WindowChange]:
        """Expire the buckets that left the window; one change per IP whose total dropped."""
        now = self._clock.now()
        changes: list[WindowChange] = []
        while self._expiry_heap and self._expiry_heap[0][0] <= now:
            expires_at, _tie_break, ip = heapq.heappop(self._expiry_heap)
            if self._expiry_scheduled.get(ip) != expires_at:
                continue  # superseded by an earlier expiry, or the IP is gone
            del self._expiry_scheduled[ip]
            entry = self._entries.get(ip)
            if entry is None:
                continue
            total_before = entry.counter.total
            removed = entry.counter.expire(now)
            self._schedule_expiry(ip, entry)
            if removed:
                changes.append(
                    WindowChange(
                        ip=ip,
                        state=entry.state,
                        total_before=total_before,
                        total_after=entry.counter.total,
                    )
                )
        return changes

    def evict_due(self) -> int:
        """Drop every COLD IP idle for `state_retention_seconds`; return how many (section 26).

        Self-sufficient (item A6): it needs no preceding `expire_due()` and
        does not consult the running total. An IP that meets the condition
        holds no live bucket -- `state_retention_seconds >= window_seconds`, so
        every bucket at or below `last_seen` left the window at or before the
        deadline -- and a non-zero total is only a sweep the schedule had not
        run yet. The entry is dropped whole: no `WindowChange` is produced and
        nothing is counted as expiry. A HOT IP is never evicted.
        """
        deadline = self._clock.now() - self._config.state_retention_seconds
        removed = 0
        deferred: list[tuple[int, int, Address]] = []
        while self._last_seen_heap:
            last_seen, tie_break, ip = self._last_seen_heap[0]
            entry = self._entries.get(ip)
            if entry is None:
                heapq.heappop(self._last_seen_heap)
                continue
            if last_seen != entry.last_seen:
                heapq.heapreplace(self._last_seen_heap, (entry.last_seen, tie_break, ip))
                continue
            if last_seen > deadline:
                break  # ordered by last_seen: nothing behind this one is due
            heapq.heappop(self._last_seen_heap)
            if entry.state is IpState.HOT:
                deferred.append((last_seen, tie_break, ip))
                continue
            self._drop(ip)
            self.retention_evictions += 1
            removed += 1
        for item in deferred:
            heapq.heappush(self._last_seen_heap, item)
        return removed

    def apply_config(self, config: DetectionConfig) -> None:
        """Adopt `config`, re-bucketing existing counts when the geometry changed (section 34).

        Each live `(S, count)` of the old ring is re-observed at
        `bucket_start(S, B')` on a new one; counts whose new bucket is not live
        under the new window are dropped (they would have expired under it),
        and a merge into a coarser bucket is exact. States, `last_seen`, the
        eviction counters and `warm_until` are untouched.
        """
        old = self._config
        rebuild = (
            config.bucket_seconds != old.bucket_seconds or config.bucket_count != old.bucket_count
        )
        self._config = config
        if not rebuild:
            return
        now = self._clock.now()
        for ip, entry in self._entries.items():
            rebuilt = self._new_counter()
            for start, count in entry.counter.live_buckets(now):
                rebuilt.observe(buckets.bucket_start(start, config.bucket_seconds), count, now)
            entry.counter = rebuilt
            self._expiry_scheduled.pop(ip, None)
            self._schedule_expiry(ip, entry)

    # --- internals -----------------------------------------------------------

    def _new_counter(self) -> IpCounter:
        return IpCounter(
            bucket_seconds=self._config.bucket_seconds, bucket_count=self._config.bucket_count
        )

    def _track(
        self,
        ip: Address,
        *,
        state: IpState,
        last_seen: int,
        inherited: bool = False,
        counter: IpCounter | None = None,
    ) -> _Entry:
        entry = _Entry(
            counter=self._new_counter() if counter is None else counter,
            state=state,
            last_seen=last_seen,
            inherited=inherited,
        )
        self._entries[ip] = entry
        self._push_last_seen(ip, entry)
        return entry

    def _drop(self, ip: Address) -> None:
        """Remove an entry; its stale schedule entries are ignored lazily."""
        del self._entries[ip]
        self._expiry_scheduled.pop(ip, None)

    def _push_last_seen(self, ip: Address, entry: _Entry) -> None:
        heapq.heappush(self._last_seen_heap, (entry.last_seen, next(self._tie_break), ip))

    def _schedule_expiry(self, ip: Address, entry: _Entry) -> None:
        """Keep the earliest pending expiry for `ip` on the heap (nothing if its ring is empty)."""
        expires_at = entry.counter.next_expiry()
        if expires_at is None:
            return
        scheduled = self._expiry_scheduled.get(ip)
        if scheduled is not None and scheduled <= expires_at:
            return
        self._expiry_scheduled[ip] = expires_at
        heapq.heappush(self._expiry_heap, (expires_at, next(self._tie_break), ip))

    def _evict_for_capacity(self) -> None:
        """Make room for one new IP: the least recently seen COLD entry goes first.

        If every tracked IP is HOT the store grows past the cap rather than
        drop a HOT IP; decision 8's `store_over_capacity` records that.
        """
        if len(self._entries) < self._max_tracked_ips:
            return
        deferred: list[tuple[int, int, Address]] = []
        victim: Address | None = None
        while self._last_seen_heap:
            last_seen, tie_break, ip = heapq.heappop(self._last_seen_heap)
            entry = self._entries.get(ip)
            if entry is None:
                continue
            if last_seen != entry.last_seen:
                heapq.heappush(self._last_seen_heap, (entry.last_seen, tie_break, ip))
                continue
            if entry.state is IpState.HOT:
                deferred.append((last_seen, tie_break, ip))
                continue
            victim = ip
            break
        for item in deferred:
            heapq.heappush(self._last_seen_heap, item)
        if victim is None:
            logger.warning(
                "store_over_capacity shard=%d tracked=%d max_tracked_ips=%d",
                self.shard,
                len(self._entries),
                self._max_tracked_ips,
            )
            return
        self._drop(victim)
        self.capacity_evictions += 1
