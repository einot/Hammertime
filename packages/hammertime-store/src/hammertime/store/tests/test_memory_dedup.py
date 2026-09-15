"""In-memory dedup store: first-seen/duplicate detection, TTL expiry, out-of-order sequences.

Spec: section 23 (agent duplicates and retries), section 26 (memory
considerations / retention). ADR-0003 (time-bucketed deltas with per-agent
sequence dedup): dedup identity here is `(agent_id, sequence)` for
agent-originated `RequestObservation` messages -- this is deliberately NOT
the same thing as the `(agent_id, sequence, event_type)` `event_id` computed
by `hammertime.core.events.envelope` for generic `EventEnvelope`
idempotency (Epic #2 / issue #21's amendment to ADR-0003). These tests never
call `compute_event_id` and never construct an `EventEnvelope`. Retention is
"a dedup window ... with a retention of `allowed_lateness + window_seconds`"
per the ADR; without dedup, "same observation received twice" must not
double-count (`count += N` twice instead of once) or trigger false HOT
transitions (spec section 23).

Reconciled against the actual implementation (originally written blind to
it, per this repo's test-author convention):

* `hammertime.store.memory.MemoryDedupStore(clock: Clock | None = None)` --
  `ttl_seconds` is NOT fixed at construction; it's passed per-call to
  `mark_seen`, matching `hammertime.store.interface.DedupStore`'s actual
  signature (`mark_seen(agent_id, sequence, *, ttl_seconds)`). This is more
  flexible than the fixed-at-construction assumption these tests originally
  made (e.g. it supports retention varying by detection-config version).
* `async def has_seen(self, agent_id: str, sequence: int) -> bool` --
  named `has_seen`, not `is_duplicate`; same semantics (True iff seen and
  not yet expired).
* `async def mark_seen(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> None`.
"""

import pytest
from hammertime.core.time.clock import ManualClock
from hammertime.store.memory import MemoryDedupStore

DEFAULT_TTL_SECONDS = 60


def _store(*, initial: int = 0) -> tuple[MemoryDedupStore, ManualClock]:
    """A fresh `MemoryDedupStore` plus the `ManualClock` driving its TTL."""
    clock = ManualClock(initial=initial)
    store = MemoryDedupStore(clock=clock)
    return store, clock


class TestFirstSeen:
    async def test_fresh_pair_is_not_a_duplicate(self) -> None:
        store, _clock = _store()

        assert await store.has_seen("agent-1", 1) is False

    async def test_checking_a_fresh_pair_does_not_mark_it_as_seen(self) -> None:
        # has_seen() is a read: calling it should not have the side effect
        # of marking the pair seen for the *next* call.
        store, _clock = _store()

        await store.has_seen("agent-1", 1)

        assert await store.has_seen("agent-1", 1) is False


class TestMarkThenCheck:
    async def test_marking_a_pair_seen_then_checking_reports_a_duplicate(self) -> None:
        store, _clock = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True

    async def test_marking_an_already_seen_pair_again_is_not_an_error(self) -> None:
        # Retries are free per ADR-0003; re-marking must not raise.
        store, _clock = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True


class TestAgentIsolation:
    async def test_same_sequence_number_on_different_agents_is_independent(self) -> None:
        store, _clock = _store()

        await store.mark_seen("agent-1", 42, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 42) is True
        assert await store.has_seen("agent-2", 42) is False

    async def test_marking_one_agent_seen_does_not_affect_another_agents_same_sequence(
        self,
    ) -> None:
        store, _clock = _store()

        await store.mark_seen("agent-1", 42, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-2", 42, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 42) is True
        assert await store.has_seen("agent-2", 42) is True


class TestSequenceIsolation:
    async def test_different_sequences_for_the_same_agent_are_tracked_independently(
        self,
    ) -> None:
        store, _clock = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-1", 2) is False

    async def test_marking_multiple_sequences_for_one_agent_is_not_confused(self) -> None:
        store, _clock = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 2, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 3, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-1", 2) is True
        assert await store.has_seen("agent-1", 3) is True
        assert await store.has_seen("agent-1", 4) is False


class TestTtlExpiry:
    async def test_entry_expires_after_its_ttl_and_is_treated_as_not_seen_again(self) -> None:
        store, clock = _store(initial=1_000)

        await store.mark_seen("agent-1", 1, ttl_seconds=60)
        clock.advance(61)  # comfortably past the 60s TTL

        assert await store.has_seen("agent-1", 1) is False

    async def test_entry_just_before_ttl_expiry_is_still_a_duplicate(self) -> None:
        store, clock = _store(initial=1_000)

        await store.mark_seen("agent-1", 1, ttl_seconds=60)
        clock.advance(59)  # one second short of the 60s TTL

        assert await store.has_seen("agent-1", 1) is True

    async def test_expiry_is_independent_per_agent(self) -> None:
        store, clock = _store(initial=0)

        await store.mark_seen("agent-1", 1, ttl_seconds=60)
        clock.advance(30)
        await store.mark_seen("agent-2", 1, ttl_seconds=60)
        clock.advance(31)  # agent-1's entry (61s old) has expired; agent-2's (31s old) has not

        assert await store.has_seen("agent-1", 1) is False
        assert await store.has_seen("agent-2", 1) is True

    async def test_re_marking_after_expiry_starts_a_fresh_ttl(self) -> None:
        store, clock = _store(initial=0)

        await store.mark_seen("agent-1", 1, ttl_seconds=60)
        clock.advance(61)
        assert await store.has_seen("agent-1", 1) is False  # expired

        await store.mark_seen("agent-1", 1, ttl_seconds=60)  # re-observed as a "new" window

        assert await store.has_seen("agent-1", 1) is True

    async def test_different_ttl_seconds_can_be_passed_on_different_calls(self) -> None:
        # ttl_seconds is a per-call parameter, not fixed at construction --
        # confirm a later mark_seen with a different ttl_seconds still
        # extends this agent's expiry using the new value.
        store, clock = _store(initial=0)

        await store.mark_seen("agent-1", 1, ttl_seconds=10)
        await store.mark_seen("agent-1", 2, ttl_seconds=100)
        clock.advance(50)  # past the first call's 10s ttl, well within the second's 100s

        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-1", 2) is True

    async def test_a_later_call_with_a_smaller_ttl_seconds_does_not_shorten_the_agents_expiry(
        self,
    ) -> None:
        # mark_seen's contract promises a sequence stays seen "for at least
        # ttl_seconds" -- a later call for the same agent with a *smaller*
        # ttl_seconds must not undercut an earlier call's longer promise.
        store, clock = _store(initial=0)

        await store.mark_seen("agent-1", 1, ttl_seconds=3600)
        await store.mark_seen("agent-1", 2, ttl_seconds=60)
        clock.advance(61)  # past the second call's 60s ttl, nowhere near the first's 3600s

        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-1", 2) is True


class TestOutOfOrderSequences:
    async def test_earlier_sequence_marked_after_a_later_one_is_still_detected_as_duplicate(
        self,
    ) -> None:
        # Out-of-order delivery: sequence 10 arrives and is marked first,
        # then sequence 5 (still within the retention window) arrives late.
        store, _clock = _store()

        await store.mark_seen("agent-1", 10, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 5, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 5) is True
        assert await store.has_seen("agent-1", 10) is True

    async def test_unmarked_sequence_below_the_high_water_mark_is_not_a_duplicate(self) -> None:
        # A naive "sequence <= high_water_mark => duplicate" implementation
        # would wrongly report sequence 7 as a duplicate here, even though
        # it was never actually observed -- including if the *first*
        # sequence this store ever sees for an agent is out of order
        # (plausible right after an ingest restart with a fresh store).
        # dedup.py's docstring promises a "bounded out-of-order set", not
        # just a high-water mark, precisely to avoid this.
        store, _clock = _store()

        await store.mark_seen("agent-1", 10, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 7) is False

    async def test_out_of_order_retry_of_the_later_sequence_is_still_a_duplicate(self) -> None:
        store, _clock = _store()

        await store.mark_seen("agent-1", 10, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 5, ttl_seconds=DEFAULT_TTL_SECONDS)

        # A retry of the *later* sequence must still be recognized too.
        assert await store.has_seen("agent-1", 10) is True

    async def test_gap_eventually_fills_in_and_contiguous_sequences_remain_tracked(self) -> None:
        # Once sequence 0 arrives, it directly extends the mark; later
        # sequences already in the out-of-order set should fold into the
        # contiguous run rather than staying separately tracked forever.
        store, _clock = _store()

        await store.mark_seen("agent-1", 2, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 0, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 0) is True
        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-1", 2) is True
        assert await store.has_seen("agent-1", 3) is False


class TestClaim:
    """`claim()`: the atomic check-and-mark `has_seen`+`mark_seen` cannot be.

    Issue #32's ingest pipeline uses this instead of the separate
    `is_duplicate()`/`mark_seen()` pair to close the race where two
    concurrent requests for the same `(agent_id, sequence)` could both
    observe "not seen" before either marks it.
    """

    async def test_first_claim_of_a_fresh_pair_returns_true(self) -> None:
        store, _clock = _store()

        assert await store.claim("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS) is True

    async def test_first_claim_marks_the_pair_seen(self) -> None:
        store, _clock = _store()

        await store.claim("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True

    async def test_second_claim_of_the_same_pair_returns_false(self) -> None:
        # Simulates two concurrent requests for the same sequence: exactly
        # one may proceed to publish.
        store, _clock = _store()

        first = await store.claim("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)
        second = await store.claim("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert first is True
        assert second is False

    async def test_claiming_an_already_mark_seen_pair_returns_false(self) -> None:
        store, _clock = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.claim("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS) is False

    async def test_claim_of_a_different_sequence_for_the_same_agent_is_independent(self) -> None:
        store, _clock = _store()

        await store.claim("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.claim("agent-1", 2, ttl_seconds=DEFAULT_TTL_SECONDS) is True

    async def test_claim_after_the_earlier_claims_ttl_expires_returns_true_again(self) -> None:
        store, clock = _store(initial=0)

        await store.claim("agent-1", 1, ttl_seconds=60)
        clock.advance(61)

        assert await store.claim("agent-1", 1, ttl_seconds=60) is True


class TestBoundedAgentTracking:
    """`max_agents`: defense in depth against unbounded distinct-agent growth."""

    async def test_does_not_evict_while_under_the_limit(self) -> None:
        clock = ManualClock(initial=0)
        store = MemoryDedupStore(clock=clock, max_agents=3)

        for agent_id in ("agent-1", "agent-2", "agent-3"):
            await store.mark_seen(agent_id, 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        for agent_id in ("agent-1", "agent-2", "agent-3"):
            assert await store.has_seen(agent_id, 1) is True

    async def test_exceeding_the_limit_evicts_the_least_recently_touched_agent(self) -> None:
        clock = ManualClock(initial=0)
        store = MemoryDedupStore(clock=clock, max_agents=2)

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-2", 1, ttl_seconds=DEFAULT_TTL_SECONDS)
        # Touching agent-1 again makes agent-2 the least-recently-touched.
        await store.has_seen("agent-1", 1)
        await store.mark_seen("agent-3", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-2", 1) is False
        assert await store.has_seen("agent-3", 1) is True

    async def test_max_agents_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_agents"):
            MemoryDedupStore(max_agents=0)
