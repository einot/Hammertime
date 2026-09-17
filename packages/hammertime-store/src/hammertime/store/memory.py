"""In-memory stores; the reference implementations the others must match.

Spec: section 20, section 26, section 32

Two of them: `MemoryDedupStore` (ingest's dedup records, spec section 26)
and `MemoryShardStateStore` (the aggregator's per-shard HOT set and
sequence counter, spec section 20, section 32; ADR-0011 decision 5). They
share only this module and the reference-implementation role; their
retention rules are opposites -- dedup entries expire, shard state never
does.

`MemoryDedupStore` implements `hammertime.store.interface.DedupStore`
in-process, the way `hammertime.bus.memory.InMemoryBus` implements
`hammertime.bus.interface.Producer`/`Consumer`: it's the reference backend
that unit tests run against, and `redis.py` (issue #31) must be
interchangeable with it behind the same protocol.

Each agent gets one `hammertime.store.dedup.SequenceWindow`, wrapped in a
sliding TTL: every `mark_seen` call extends that agent's expiry to
`max(current expiry, now + ttl_seconds)` (ADR-0003 recommends `ttl_seconds =
allowed_lateness_seconds + window_seconds`). The `max` is deliberate, not
just `now + ttl_seconds`: `DedupStore.mark_seen`'s contract promises a
sequence stays seen "for at least `ttl_seconds`", and `ttl_seconds` is a
per-call parameter rather than fixed at construction, so a later call for
the same agent with a *smaller* `ttl_seconds` (e.g. a detection-config
change mid-flight) must never shorten -- only ever extend -- the whole
agent window's expiry; doing so would silently evict earlier sequences
before their own promised retention elapsed. An expired agent's window is
discarded -- both defensively on lookup and via a small amortized sweep on
every call -- so memory is bounded by the number of agents active within the
retention window, not by total history (spec section 26).

TTL expiry alone bounds memory only if the number of *distinct* agents
active within one retention window stays reasonable; nothing in this module
enforces that on its own (that's ingest's job, via auth (#28) and per-agent
rate limiting (#29), once #32 wires this store in). As defense in depth --
matching `hammertime.ingest.ratelimit.RateLimiter`'s LRU-bounded bucket
tracking -- `_windows` is capped at `max_agents`: once full, the
least-recently-touched agent's window is evicted to make room for a new
one, same as a TTL expiry would have done to it eventually anyway.
"""

import heapq
from collections import OrderedDict

from hammertime.core.addressing.address import Address
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import Clock, SystemClock
from hammertime.store.dedup import SequenceWindow
from hammertime.store.interface import ShardState

_DEFAULT_MAX_AGENTS = 100_000


class MemoryDedupStore:
    """In-process `DedupStore`: one `SequenceWindow` per agent, TTL-evicted."""

    def __init__(
        self, clock: Clock | None = None, *, max_agents: int = _DEFAULT_MAX_AGENTS
    ) -> None:
        if max_agents <= 0:
            raise ValueError(f"max_agents must be positive, got {max_agents!r}")
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._max_agents = max_agents
        self._windows: OrderedDict[str, SequenceWindow] = OrderedDict()
        self._expires_at: dict[str, int] = {}
        # (expires_at, agent_id) entries, possibly stale if that agent's
        # expiry has since been refreshed; `_sweep` reconciles against
        # `_expires_at` before evicting.
        self._expiry_heap: list[tuple[int, str]] = []

    async def has_seen(self, agent_id: str, sequence: int) -> bool:
        self._sweep()
        window = self._windows.get(agent_id)
        if window is None:
            return False
        self._windows.move_to_end(agent_id)
        return window.contains(sequence)

    async def mark_seen(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> None:
        self._sweep()
        window = self._windows.get(agent_id)
        if window is None:
            window = SequenceWindow()
            self._evict_if_full()
            self._windows[agent_id] = window
        else:
            self._windows.move_to_end(agent_id)
        window.add(sequence)
        expires_at = self._clock.now() + ttl_seconds
        current_expiry = self._expires_at.get(agent_id)
        if current_expiry is None or expires_at > current_expiry:
            self._expires_at[agent_id] = expires_at
            heapq.heappush(self._expiry_heap, (expires_at, agent_id))

    async def claim(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> bool:
        # No `await` between the check and the mark below (`has_seen`/
        # `mark_seen` themselves contain none either), so this coroutine
        # runs to completion without ever yielding to the event loop --
        # atomic in practice for a single-process asyncio deployment, the
        # same reasoning `ratelimit/__init__.py` documents for `check()`.
        if await self.has_seen(agent_id, sequence):
            return False
        await self.mark_seen(agent_id, sequence, ttl_seconds=ttl_seconds)
        return True

    def _evict_if_full(self) -> None:
        if len(self._windows) < self._max_agents:
            return
        evicted_agent_id, _evicted_window = self._windows.popitem(last=False)
        self._expires_at.pop(evicted_agent_id, None)

    def _sweep(self) -> None:
        """Evict every agent window whose most recent expiry has passed."""
        now = self._clock.now()
        while self._expiry_heap and self._expiry_heap[0][0] <= now:
            expires_at, agent_id = heapq.heappop(self._expiry_heap)
            if self._expires_at.get(agent_id) == expires_at:
                # Still the current expiry for this agent, i.e. not
                # superseded by a later mark_seen -- actually expired.
                del self._windows[agent_id]
                del self._expires_at[agent_id]


class MemoryShardStateStore:
    """In-process `ShardStateStore`: a set of HOT IPs and a counter per shard.

    The reference implementation `RedisShardStateStore` must match, exactly
    as `MemoryDedupStore` is for `DedupStore`. Nothing here expires: ADR-0011
    decision 5 gives this state no TTL, because a shard's HOT set has to
    outlive the process that wrote it. Memory is therefore bounded by the
    HOT sets of the shards this process has claimed, which
    `ShardWindow`'s own `max_tracked_ips` cap bounds upstream -- there is no
    `max_agents`-style eviction here, because evicting an entry would
    reintroduce the very leak the store closes (a forgotten HOT IP the trie
    is never told to drop).

    Being in-process, this backend loses everything on restart. That is
    fine for tests and a single-process development run (a restarted
    process also loses the trie it published to), but a deployment that
    must survive a restart needs `RedisShardStateStore`.

    `record_transition` contains no `await`, so it runs to completion
    without yielding to the event loop: atomic in practice for a
    single-process asyncio deployment, the same reasoning `MemoryDedupStore.
    claim` documents. That is what makes its read-then-clamp of the
    sequence counter safe without any compare-and-set machinery -- the
    Redis backend, which has no such guarantee, needs a WATCH loop for the
    same three lines.
    """

    def __init__(self) -> None:
        self._hot_ips: dict[int, set[Address]] = {}
        self._next_sequence: dict[int, int] = {}

    async def load(self, shard: int) -> ShardState:
        # `.get`, not `setdefault`: loading a never-seen shard must not
        # create it (interface.py's `load` contract).
        hot_ips = self._hot_ips.get(shard)
        return ShardState(
            hot_ips=frozenset(hot_ips) if hot_ips else frozenset(),
            next_sequence=self._next_sequence.get(shard, 0),
        )

    async def record_transition(
        self, shard: int, ip: Address, state: IpState, sequence: int
    ) -> None:
        if sequence < 0:
            # Write nothing: `schemas/hot_ip_event.v1.json` has
            # `"minimum": 0`, and a negative sequence would drive the
            # clamp's floor below zero.
            raise ValueError(f"sequence must be non-negative, got {sequence!r}")
        if state is IpState.HOT:
            self._hot_ips.setdefault(shard, set()).add(ip)
        else:
            # A demotion of an IP this shard does not hold is a membership
            # no-op that still advances the sequence, and must not create
            # an empty set for a shard that has none.
            self._hot_ips.get(shard, set()).discard(ip)
        # ADR-0011 Amendment 1 item A2: raise the counter to
        # `sequence + 1` only if that is higher, never lower it. The
        # membership change above is applied either way. Decision 4's
        # "never reproduces an earlier `event_id`" promise is made about
        # this persisted counter, so a replayed or out-of-order transition
        # must not hand an already-used sequence back to the next caller.
        self._next_sequence[shard] = max(self._next_sequence.get(shard, 0), sequence + 1)
