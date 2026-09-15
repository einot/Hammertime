"""Per-agent rate limiting -- token buckets, 429 rather than silent partial
acceptance.

Spec: section 36 (security: "rate limits" is one of the minimum controls
an ingestion API MUST provide). Wire-level contract:
docs/protocol/observation-v1.md's response table ("429 | Per-agent rate
limit"). Documented default: .env.example's
`HAMMERTIME_INGEST_RATE_LIMIT_RPS=50`.

Public surface (`hammertime.ingest.ratelimit`), reconciled against the
actual implementation:

* `class RateLimiter`
    * `__init__(self, clock: Clock | None = None) -> None` -- follows the
      injectable-clock convention documented in `hammertime.core.time.clock`
      (production omits `clock` and gets a `SystemClock()`; tests always
      pass a `ManualClock`). The limiter holds no notion of any single
      agent's configured rate -- per the design boundary that this
      component does not depend on `hammertime.ingest.auth`, the limit is
      passed in explicitly on every call.
    * `check(self, agent_id: str, limit_rps: int | float, *, cost: float = 1.0) -> None`
      -- consumes `cost` tokens (default 1) from `agent_id`'s bucket,
      refilling it first based on elapsed clock time at `limit_rps`
      tokens/second. Returns `None` on success; raises `RateLimitExceeded`
      if too few tokens are available.
    * Bucket capacity equals `limit_rps` (one second's worth of tokens) and
      starts full for a never-before-seen `agent_id`, so an idle agent's
      first burst up to its configured rate always succeeds.
* `class RateLimitExceeded(HammertimeError)` -- raised by `check` on
  exhaustion; carries `.agent_id`, `.limit_rps`, and `.retry_after`
  (seconds until enough tokens are available), though this file only
  asserts the exception type, not those attributes, since neither the
  spec nor the protocol doc pins the error's shape beyond "429 semantics".

Explicit gap, not resolved here: neither the spec nor the protocol doc
says what should happen if `limit_rps` itself changes between calls for
the same `agent_id` (e.g. a config reload). This file does not test that
case; it always passes a fixed `limit_rps` for a given `agent_id` within
a single test.

Clock-resolution note: `hammertime.core.time.clock.Clock.now()` returns
an int (whole UTC epoch seconds), and `ManualClock.advance()` only
accepts whole seconds. That means this test file cannot exercise
sub-second partial refills (e.g. "half a second's worth of tokens") at
all -- only whole-second steps are expressible through the clock
contract. Where the task description suggests checking a *partial*
refill, this file instead asserts the invariant that must hold
regardless of the exact refill arithmetic: accepted requests per unit
time can never exceed `limit_rps`, never asserting an exact intermediate
token count.
"""

from __future__ import annotations

import pytest
from hammertime.core.errors import HammertimeError
from hammertime.core.time.clock import ManualClock
from hammertime.ingest.ratelimit import RateLimiter, RateLimitExceeded

# .env.example: HAMMERTIME_INGEST_RATE_LIMIT_RPS=50 -- the documented
# production default, exercised once as a sanity check. Most tests below
# use small values so bucket exhaustion can be driven in a handful of
# calls rather than fifty.
DOCUMENTED_DEFAULT_RATE_LIMIT_RPS = 50


def _exhaust(limiter: RateLimiter, agent_id: str, limit_rps: float, *, max_attempts: int) -> int:
    """Call `check` for `agent_id` until it raises, or `max_attempts` is hit.

    Returns the number of calls that succeeded before the first rejection.
    Raises AssertionError if the bucket is never exhausted within
    `max_attempts` -- a generous bound so this is a real assertion, not
    an infinite loop, regardless of the exact (unknown) capacity.
    """
    for accepted in range(max_attempts):
        try:
            limiter.check(agent_id=agent_id, limit_rps=limit_rps)
        except RateLimitExceeded:
            return accepted
    raise AssertionError(
        f"bucket for {agent_id!r} was not exhausted within {max_attempts} attempts "
        f"at limit_rps={limit_rps}"
    )


class TestRateLimiterHappyPath:
    def test_requests_within_the_limit_succeed(self) -> None:
        clock = ManualClock(initial=1_000)
        limiter = RateLimiter(clock=clock)
        # A never-before-seen agent's bucket is assumed to start full, so
        # the first `limit_rps` requests must all succeed without the
        # clock moving at all.
        for _ in range(5):
            limiter.check(agent_id="edge-17", limit_rps=5)  # must not raise

    def test_a_single_request_well_under_the_limit_succeeds(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limiter.check(agent_id="edge-17", limit_rps=DOCUMENTED_DEFAULT_RATE_LIMIT_RPS)


class TestRateLimiterRejection:
    def test_a_request_exceeding_the_limit_is_rejected(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 3
        # Drain whatever the bucket started with, without the clock
        # moving, so no refill can mask exhaustion.
        _exhaust(limiter, "edge-17", limit_rps, max_attempts=1000)
        with pytest.raises(RateLimitExceeded):
            limiter.check(agent_id="edge-17", limit_rps=limit_rps)

    def test_rate_limit_exceeded_is_a_hammertime_error(self) -> None:
        # Follows the repo-wide convention (hammertime.core.errors) that
        # domain errors are catchable via the common base, the same way
        # SchemaValidationError etc. are HammertimeError subclasses.
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 1
        _exhaust(limiter, "edge-17", limit_rps, max_attempts=1000)
        with pytest.raises(HammertimeError):
            limiter.check(agent_id="edge-17", limit_rps=limit_rps)

    def test_rejection_does_not_raise_a_bare_exception_type(self) -> None:
        # Guards against an implementation that raises ValueError or
        # similar instead of a named, catchable violation type -- the
        # module docstring explicitly promises "429 rather than silent
        # partial acceptance", i.e. a distinguishable rejection.
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 1
        _exhaust(limiter, "edge-17", limit_rps, max_attempts=1000)
        try:
            limiter.check(agent_id="edge-17", limit_rps=limit_rps)
        except RateLimitExceeded:
            pass
        else:
            pytest.fail("expected RateLimitExceeded to be raised")


class TestRateLimiterRefillOverTime:
    def test_the_limiter_refills_after_the_clock_advances(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 4

        _exhaust(limiter, "edge-17", limit_rps, max_attempts=1000)
        with pytest.raises(RateLimitExceeded):
            limiter.check(agent_id="edge-17", limit_rps=limit_rps)

        # A full second at `limit_rps` tokens/second is, by any
        # reasonable token-bucket arithmetic, enough for at least one
        # more token to become available -- the limiter must not stay
        # permanently jammed once real time (as reported by the clock)
        # has passed.
        clock.advance(1)
        limiter.check(agent_id="edge-17", limit_rps=limit_rps)  # must not raise

    def test_no_refill_occurs_while_the_clock_is_unchanged(self) -> None:
        # Sanity check on the injectable-clock convention itself: two
        # calls to check back-to-back, with the clock never advanced,
        # must be treated as simultaneous -- no time-based refill can
        # have occurred between them.
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 2

        accepted = _exhaust(limiter, "edge-17", limit_rps, max_attempts=1000)
        assert accepted >= 1  # the bucket allowed at least the first request

        with pytest.raises(RateLimitExceeded):
            limiter.check(agent_id="edge-17", limit_rps=limit_rps)
        with pytest.raises(RateLimitExceeded):
            limiter.check(agent_id="edge-17", limit_rps=limit_rps)


class TestRateLimiterPerAgentIsolation:
    def test_one_agent_being_rate_limited_does_not_affect_another(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 3

        _exhaust(limiter, "edge-17", limit_rps, max_attempts=1000)
        with pytest.raises(RateLimitExceeded):
            limiter.check(agent_id="edge-17", limit_rps=limit_rps)

        # A different agent_id, same clock, same configured rate, no
        # time having passed -- must still get its own fresh bucket.
        limiter.check(agent_id="edge-99", limit_rps=limit_rps)  # must not raise

    def test_two_agents_can_have_different_configured_limits(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)

        low_rps_accepted = _exhaust(limiter, "low-rate-agent", 1, max_attempts=1000)
        high_rps_accepted = _exhaust(limiter, "high-rate-agent", 20, max_attempts=1000)

        # Whatever the exact capacity arithmetic, an agent configured
        # with a higher limit_rps must not be exhausted in fewer (or
        # equal) accepted requests than one configured with a much
        # lower limit_rps.
        assert high_rps_accepted > low_rps_accepted


class TestRateLimiterBurstThenRefill:
    # Per the task description: since the exact refill arithmetic isn't
    # knowable without reading the implementation, these tests assert
    # the invariant that must hold -- accepted requests per unit time
    # cannot exceed the configured rate -- rather than an exact
    # intermediate token count.

    def test_burst_capacity_is_bounded_by_a_generous_multiple_of_the_rate(self) -> None:
        # However capacity is sized, an agent that has done nothing
        # else must not be able to burst past some sane bound tied to
        # limit_rps -- otherwise "rate limit" is meaningless. A 10x
        # multiple is deliberately generous so this doesn't encode a
        # specific, unverified capacity constant.
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 5

        accepted = _exhaust(limiter, "edge-17", limit_rps, max_attempts=10 * limit_rps + 1)
        assert 1 <= accepted <= 10 * limit_rps

    def test_requests_accepted_after_a_refill_do_not_exceed_the_configured_rate(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 6

        # Drain the initial allowance completely.
        _exhaust(limiter, "edge-17", limit_rps, max_attempts=1000)
        with pytest.raises(RateLimitExceeded):
            limiter.check(agent_id="edge-17", limit_rps=limit_rps)

        # Advance by exactly one second -- the clock's finest available
        # resolution (see module docstring's clock-resolution note) --
        # and see how many requests the refill newly admits.
        clock.advance(1)
        newly_accepted = _exhaust(limiter, "edge-17", limit_rps, max_attempts=10 * limit_rps + 1)

        # Invariant: over a one-second window, an agent cannot be
        # granted more than `limit_rps` additional requests, no matter
        # how capacity/refill is implemented internally.
        assert 1 <= newly_accepted <= limit_rps

    def test_cumulative_acceptance_rate_does_not_exceed_the_configured_limit(self) -> None:
        # Broader version of the invariant above, run across several
        # one-second steps: total accepted requests for an agent must
        # never outpace limit_rps * elapsed_seconds by more than one
        # second's worth of initial burst allowance.
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)
        limit_rps = 4
        agent_id = "edge-17"

        total_accepted = 0
        for elapsed_seconds in range(1, 6):
            total_accepted += _exhaust(
                limiter, agent_id, limit_rps, max_attempts=10 * limit_rps + 1
            )
            clock.advance(1)
            # Allow exactly one extra second's worth of budget for the
            # initial full-bucket burst at t=0.
            assert total_accepted <= limit_rps * (elapsed_seconds + 1)


class TestInvalidInput:
    # Regression tests for reviewer/security-auditor findings on this
    # component: (1) cost > limit_rps used to permanently exhaust the
    # bucket forever instead of failing loudly -- a real, easily hit case
    # (any limit_rps < 1 with the default cost=1.0), directly
    # contradicting the "first request never rejected" guarantee in this
    # module's own docstring; (2) NaN/inf limit_rps or cost silently
    # disabled rate limiting entirely instead of being rejected.

    def test_cost_exceeding_capacity_raises_immediately_rather_than_locking_out_the_agent(
        self,
    ) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)

        with pytest.raises(ValueError, match="cost"):
            limiter.check(agent_id="edge-17", limit_rps=0.5)  # default cost=1.0 > capacity

    def test_a_sub_one_limit_rps_with_explicit_matching_cost_still_works(self) -> None:
        # The bug wasn't "fractional limit_rps is broken" -- it was
        # specifically cost > capacity. A cost sized to fit still succeeds.
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)

        limiter.check(agent_id="edge-17", limit_rps=0.5, cost=0.5)  # must not raise

    @pytest.mark.parametrize("bad_limit", [float("nan"), float("inf"), -float("inf")])
    def test_non_finite_limit_rps_is_rejected(self, bad_limit: float) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)

        with pytest.raises(ValueError):
            limiter.check(agent_id="edge-17", limit_rps=bad_limit)

    @pytest.mark.parametrize("bad_cost", [float("nan"), float("inf")])
    def test_non_finite_cost_is_rejected(self, bad_cost: float) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock)

        with pytest.raises(ValueError):
            limiter.check(agent_id="edge-17", limit_rps=5, cost=bad_cost)


class TestBoundedAgentTracking:
    # Regression test for the unbounded-memory-growth finding: without a
    # cap, self._buckets grows one entry per distinct agent_id forever.

    def test_bucket_count_is_bounded_by_max_agents(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock, max_agents=2)

        limiter.check(agent_id="agent-a", limit_rps=5)
        limiter.check(agent_id="agent-b", limit_rps=5)
        # A third distinct agent must evict the least-recently-touched
        # bucket (agent-a) rather than growing unboundedly.
        limiter.check(agent_id="agent-c", limit_rps=5)

        assert len(limiter._buckets) == 2
        assert "agent-a" not in limiter._buckets

    def test_evicted_agent_gets_a_fresh_full_bucket_on_return(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock, max_agents=1)
        limit_rps = 3

        _exhaust(limiter, "agent-a", limit_rps, max_attempts=1000)
        with pytest.raises(RateLimitExceeded):
            limiter.check(agent_id="agent-a", limit_rps=limit_rps)

        # A different agent evicts agent-a's (exhausted) bucket entirely.
        limiter.check(agent_id="agent-b", limit_rps=limit_rps)

        # agent-a is now a "new" agent as far as the limiter is concerned
        # -- its bucket starts full again, not still-exhausted.
        limiter.check(agent_id="agent-a", limit_rps=limit_rps)  # must not raise

    def test_touching_a_bucket_protects_it_from_lru_eviction(self) -> None:
        clock = ManualClock(initial=0)
        limiter = RateLimiter(clock=clock, max_agents=2)

        limiter.check(agent_id="agent-a", limit_rps=5)
        limiter.check(agent_id="agent-b", limit_rps=5)
        # Re-touch agent-a so it's no longer the least-recently-used.
        limiter.check(agent_id="agent-a", limit_rps=5)
        # Now agent-b is the LRU entry and should be evicted, not agent-a.
        limiter.check(agent_id="agent-c", limit_rps=5)

        assert "agent-a" in limiter._buckets
        assert "agent-b" not in limiter._buckets
