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

State is in-process and in-memory: this limiter enforces a rate limit from
the point of view of a single ingest process only. There is no
coordination across multiple ingest replicas.
"""

from dataclasses import dataclass

from hammertime.core.errors import HammertimeError
from hammertime.core.time.clock import Clock, SystemClock

__all__ = [
    "RateLimitExceeded",
    "RateLimiter",
]


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

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._buckets: dict[str, _Bucket] = {}

    def check(self, agent_id: str, limit_rps: int | float, *, cost: float = 1.0) -> None:
        """Consume `cost` tokens (default 1) from `agent_id`'s bucket.

        Raises `RateLimitExceeded` if too few tokens are available. Bucket
        capacity is `limit_rps`; a first-seen agent starts with a full
        bucket rather than an empty one, so an idle agent's first request
        is never rejected.
        """
        if limit_rps <= 0:
            raise ValueError("limit_rps must be positive")
        if cost <= 0:
            raise ValueError("cost must be positive")

        now = self._clock.now()
        bucket = self._buckets.get(agent_id)
        if bucket is None:
            bucket = _Bucket(tokens=float(limit_rps), last_refill=now)
            self._buckets[agent_id] = bucket
        else:
            elapsed = now - bucket.last_refill
            if elapsed > 0:
                bucket.tokens = min(float(limit_rps), bucket.tokens + elapsed * limit_rps)
                bucket.last_refill = now

        if bucket.tokens < cost:
            shortfall = cost - bucket.tokens
            retry_after = shortfall / limit_rps
            raise RateLimitExceeded(agent_id, limit_rps, retry_after)

        bucket.tokens -= cost

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
