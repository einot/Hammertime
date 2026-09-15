"""Per-agent token buckets; 429 rather than silent partial acceptance.

Spec: section 36

Each agent gets its own token bucket keyed by `agent_id`. A bucket holds up
to `limit_rps` tokens, refills continuously at `limit_rps` tokens/second,
and a request costs one token; an agent may therefore burst up to one
second's worth of its configured rate before being throttled.

This limiter is deliberately decoupled from `hammertime.ingest.auth`: it
takes an explicit `agent_id` and `limit_rps` on every call rather than an
agent record from the auth registry, so it can be built, tested, and
reasoned about independently. Looking up an agent's configured limit and
calling this limiter with it is a later integration step, not this one.
That integration step (issue #32) is expected to call `check()` only for
an already-authenticated `agent_id` (i.e. after issue #28's auth check has
run) -- this module cannot and does not enforce that ordering itself, so
`_buckets` is still bounded independently (see `max_agents` below) as
defense-in-depth against a caller that doesn't hold to it.

State is in-process and in-memory: this limiter enforces a rate limit from
the point of view of a single ingest process only. There is no
coordination across multiple ingest replicas.

Concurrency: `check()` is synchronous with no `await` points, so it is
race-free under a single asyncio event loop (this repo's FastAPI routes
are all `async def`, e.g. `api/routes.py`'s `create_observation`). It is
NOT safe to call concurrently from multiple OS threads (e.g. a sync `def`
route handler run in Starlette's threadpool) -- the bucket read-modify-
write is not atomic. Don't use this from a sync route handler without
adding a lock.
"""

import math
from collections import OrderedDict
from dataclasses import dataclass

from hammertime.core.errors import HammertimeError
from hammertime.core.time.clock import Clock, SystemClock

__all__ = [
    "RateLimitExceeded",
    "RateLimiter",
]

#: Bound on distinct agent_id buckets tracked at once (LRU-evicted beyond
#: this). Defense-in-depth against unbounded memory growth if `check()` is
#: ever called with a caller-controlled agent_id before authentication has
#: narrowed it to a known, bounded set (spec section 36's "request size
#: limits" concern applies to state this limiter itself accumulates, not
#: just request bodies).
_DEFAULT_MAX_AGENTS = 100_000


class RateLimitExceeded(HammertimeError):
    """The agent has exhausted its token bucket; caller should respond 429.

    Per the observation protocol's response table
    (docs/protocol/observation-v1.md), a rate-limited agent gets a 429, not
    a silently dropped or partially accepted message (spec section 36).
    """

    def __init__(self, agent_id: str, limit_rps: float, retry_after: float) -> None:
        super().__init__(
            f"agent {agent_id!r} exceeded rate limit of {limit_rps} req/s; "
            f"retry after {retry_after:.3f}s"
        )
        self.agent_id = agent_id
        self.limit_rps = limit_rps
        self.retry_after = retry_after


@dataclass
class _Bucket:
    """Mutable per-agent bucket state: tokens on hand and last refill time."""

    tokens: float
    last_refill: int


class RateLimiter:
    """Per-agent token-bucket rate limiter.

    Time comes from an injectable `Clock` (see `hammertime.core.time.clock`)
    rather than `time.time()`/`time.monotonic()` directly, so tests can
    advance time deterministically instead of sleeping.
    """

    def __init__(
        self, clock: Clock | None = None, *, max_agents: int = _DEFAULT_MAX_AGENTS
    ) -> None:
        if max_agents <= 0:
            raise ValueError("max_agents must be positive")
        self._clock = clock if clock is not None else SystemClock()
        self._max_agents = max_agents
        # Insertion/access order tracks recency for LRU eviction: touching
        # an agent's bucket moves it to the end via move_to_end().
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def check(self, agent_id: str, limit_rps: int | float, *, cost: float = 1.0) -> None:
        """Consume `cost` tokens (default 1) from `agent_id`'s bucket.

        Raises `RateLimitExceeded` if too few tokens are available. Bucket
        capacity is `limit_rps`; a first-seen agent starts with a full
        bucket rather than an empty one, so an idle agent's first request
        is never rejected -- unless `cost` exceeds `limit_rps` itself, in
        which case that request could *never* succeed at any capacity, so
        this raises `ValueError` immediately rather than let it silently
        and permanently exhaust the agent's bucket (a bug this module
        previously had: e.g. `limit_rps=0.5` with the default `cost=1.0`
        would otherwise raise `RateLimitExceeded` forever, contradicting
        the "first request never rejected" guarantee above).
        """
        if limit_rps <= 0 or not math.isfinite(limit_rps):
            raise ValueError(f"limit_rps must be a positive, finite number, got {limit_rps!r}")
        if cost <= 0 or not math.isfinite(cost):
            raise ValueError(f"cost must be a positive, finite number, got {cost!r}")
        if cost > limit_rps:
            raise ValueError(
                f"cost {cost} exceeds bucket capacity (limit_rps={limit_rps}); "
                "no request of this size could ever succeed at this rate"
            )

        now = self._clock.now()
        bucket = self._buckets.get(agent_id)
        if bucket is None:
            bucket = _Bucket(tokens=float(limit_rps), last_refill=now)
            self._evict_if_full()
            self._buckets[agent_id] = bucket
        else:
            self._buckets.move_to_end(agent_id)
            elapsed = now - bucket.last_refill
            if elapsed > 0:
                bucket.tokens = min(float(limit_rps), bucket.tokens + elapsed * limit_rps)
                bucket.last_refill = now

        if bucket.tokens < cost:
            shortfall = cost - bucket.tokens
            retry_after = shortfall / limit_rps
            raise RateLimitExceeded(agent_id, limit_rps, retry_after)

        bucket.tokens -= cost

    def _evict_if_full(self) -> None:
        """Drop the least-recently-touched bucket if at `max_agents` capacity."""
        if len(self._buckets) >= self._max_agents:
            self._buckets.popitem(last=False)

    def allow(self, agent_id: str, limit_rps: int | float, *, cost: float = 1.0) -> bool:
        """Non-raising variant of `check`: True if the request was allowed."""
        try:
            self.check(agent_id, limit_rps, cost=cost)
        except RateLimitExceeded:
            return False
        return True

    def reset(self, agent_id: str | None = None) -> None:
        """Drop bucket state for `agent_id`, or every agent if omitted."""
        if agent_id is None:
            self._buckets.clear()
        else:
            self._buckets.pop(agent_id, None)
