"""In-memory store; the reference implementation the others must match.

Spec: section 26

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

from hammertime.core.time.clock import Clock, SystemClock
from hammertime.store.dedup import SequenceWindow

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
