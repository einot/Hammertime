"""In-memory store; the reference implementation the others must match.

Spec: section 26

`MemoryDedupStore` implements `hammertime.store.interface.DedupStore`
in-process, the way `hammertime.bus.memory.InMemoryBus` implements
`hammertime.bus.interface.Producer`/`Consumer`: it's the reference backend
that unit tests run against, and `redis.py` (issue #31) must be
interchangeable with it behind the same protocol.

Each agent gets one `hammertime.store.dedup.SequenceWindow`, wrapped in a
sliding TTL: every `mark_seen` call refreshes that agent's expiry to
`now + ttl_seconds` (ADR-0003 recommends `ttl_seconds =
allowed_lateness_seconds + window_seconds`). An expired agent's window is
discarded -- both defensively on lookup and via a small amortized sweep on
every call -- so memory is bounded by the number of agents active within the
retention window, not by total history (spec section 26).
"""

import heapq

from hammertime.core.time.clock import Clock, SystemClock
from hammertime.store.dedup import SequenceWindow


class MemoryDedupStore:
    """In-process `DedupStore`: one `SequenceWindow` per agent, TTL-evicted."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._windows: dict[str, SequenceWindow] = {}
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
        return window.contains(sequence)

    async def mark_seen(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> None:
        self._sweep()
        window = self._windows.get(agent_id)
        if window is None:
            window = SequenceWindow()
            self._windows[agent_id] = window
        window.add(sequence)
        expires_at = self._clock.now() + ttl_seconds
        self._expires_at[agent_id] = expires_at
        heapq.heappush(self._expiry_heap, (expires_at, agent_id))

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
