"""Generic token buckets; 429 rather than silent partial acceptance.

Spec: section 36, section 36.5, section 36.6

Each bucket key gets its own token bucket. A bucket holds up to `capacity`
tokens (default: `limit_rps`, i.e. one second's worth), refills continuously
at `limit_rps` tokens/second, and a call costs `cost` tokens (default 1); a
caller may therefore burst up to `capacity` before being throttled.

This limiter is deliberately decoupled from `hammertime.ingest.auth`: it
takes an explicit `key` and `limit_rps` on every call rather than an agent
record from the auth registry, so it can be built, tested, and reasoned
about independently. `key` is keying-agnostic on purpose -- ADR-0007 keys
two instances of this same type on a source-address prefix and an attempted
`X-Agent-Id` (neither of which is necessarily an authenticated agent
identity), while `api/routes.py` and ADR-0008's observation budget key on
the authenticated `agent_id`.

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

#: Bound on distinct keys tracked at once (LRU-evicted beyond this).
#:
#: This bounds memory only -- it is NOT a budget bound, and MUST NOT be cited
#: as one. `check()` recreates a first-seen key's bucket full, and an evicted
#: key is indistinguishable from one never seen, so a caller free to choose
#: this limiter's `key` can walk a target key out of the LRU and get its
#: budget reset on demand regardless of how `max_keys` is set (ADR-0007
#: Decision 8, which found exactly this against the auth-failure agent
#: bucket). Every caller of this limiter MUST bound its own key space by
#: something other than `max_keys` before `check()` ever sees a
#: caller-controlled key: the authenticated `agent_id` (bounded by the
#: registry), a fixed slot table (ADR-0007 Decision 8), or an address prefix
#: whose budget is per prefix by design (the auth-failure source bucket).
#: `max_keys` remains a defense-in-depth memory backstop against unbounded
#: growth if some future caller's key-space narrowing turns out to be wrong
#: (spec section 36's "request size limits" concern applies to state this
#: limiter itself accumulates, not just request bodies).
_DEFAULT_MAX_KEYS = 100_000


class RateLimitExceeded(HammertimeError):
    """The bucket for `key` is exhausted; caller should respond 429.

    Per the observation protocol's response table
    (docs/protocol/observation-v1.md), a rate-limited request gets a 429,
    not a silently dropped or partially accepted message (spec section 36,
    section 36.7).
    """

    def __init__(self, key: str, limit_rps: float, retry_after: float) -> None:
        super().__init__(
            f"key {key!r} exceeded rate limit of {limit_rps} req/s; retry after {retry_after:.3f}s"
        )
        self.key = key
        self.limit_rps = limit_rps
        self.retry_after = retry_after


@dataclass
class _Bucket:
    """Mutable per-key bucket state: tokens on hand and last refill time."""

    tokens: float
    last_refill: int


class RateLimiter:
    """Token-bucket rate limiter, keyed by an arbitrary caller-supplied string.

    Time comes from an injectable `Clock` (see `hammertime.core.time.clock`)
    rather than `time.time()`/`time.monotonic()` directly, so tests can
    advance time deterministically instead of sleeping.
    """

    def __init__(self, clock: Clock | None = None, *, max_keys: int = _DEFAULT_MAX_KEYS) -> None:
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        self._clock = clock if clock is not None else SystemClock()
        self._max_keys = max_keys
        # Insertion/access order tracks recency for LRU eviction: touching
        # a key's bucket moves it to the end via move_to_end().
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def check(
        self,
        key: str,
        limit_rps: int | float,
        *,
        cost: float = 1.0,
        capacity: float | None = None,
    ) -> None:
        """Consume `cost` tokens (default 1) from `key`'s bucket.

        Raises `RateLimitExceeded` if too few tokens are available. Bucket
        capacity defaults to `limit_rps` (one second's worth) when
        `capacity` is omitted; a first-seen key starts with a full bucket
        (`capacity` tokens) rather than an empty one, so its first call is
        never rejected -- unless `cost` exceeds `capacity` itself, in which
        case that call could *never* succeed at any point in time, so this
        raises `ValueError` immediately rather than let it silently and
        permanently exhaust the key's bucket (a bug this module previously
        had: e.g. `limit_rps=0.5` with the default `cost=1.0` would
        otherwise raise `RateLimitExceeded` forever, contradicting the
        "first call never rejected" guarantee above).
        """
        if limit_rps <= 0 or not math.isfinite(limit_rps):
            raise ValueError(f"limit_rps must be a positive, finite number, got {limit_rps!r}")
        resolved_capacity = float(limit_rps) if capacity is None else capacity
        if resolved_capacity <= 0 or not math.isfinite(resolved_capacity):
            raise ValueError(
                f"capacity must be a positive, finite number, got {resolved_capacity!r}"
            )
        if cost <= 0 or not math.isfinite(cost):
            raise ValueError(f"cost must be a positive, finite number, got {cost!r}")
        if cost > resolved_capacity:
            raise ValueError(
                f"cost {cost} exceeds bucket capacity ({resolved_capacity}); "
                "no request of this size could ever succeed at this rate"
            )

        now = self._clock.now()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=resolved_capacity, last_refill=now)
            self._evict_if_full()
            self._buckets[key] = bucket
        else:
            self._buckets.move_to_end(key)
            elapsed = now - bucket.last_refill
            if elapsed > 0:
                bucket.tokens = min(resolved_capacity, bucket.tokens + elapsed * limit_rps)
                bucket.last_refill = now

        if bucket.tokens < cost:
            shortfall = cost - bucket.tokens
            retry_after = shortfall / limit_rps
            raise RateLimitExceeded(key, limit_rps, retry_after)

        bucket.tokens -= cost

    def _evict_if_full(self) -> None:
        """Drop the least-recently-touched bucket if at `max_keys` capacity."""
        if len(self._buckets) >= self._max_keys:
            self._buckets.popitem(last=False)

    def allow(
        self,
        key: str,
        limit_rps: int | float,
        *,
        cost: float = 1.0,
        capacity: float | None = None,
    ) -> bool:
        """Non-raising variant of `check`: True if the call was allowed."""
        try:
            self.check(key, limit_rps, cost=cost, capacity=capacity)
        except RateLimitExceeded:
            return False
        return True

    def reset(self, key: str | None = None) -> None:
        """Drop bucket state for `key`, or every key if omitted."""
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)
