"""Redis-backed dedup store (issue #31): conformance to the `DedupStore` contract.

Spec: section 23 (agent duplicates and retries), section 26 (memory
considerations / retention). ADR-0003 (time-bucketed deltas with per-agent
sequence dedup): `ttl_seconds` SHOULD be `allowed_lateness_seconds +
window_seconds` so a retry within the dedup retention window is always
caught, and entries outside it are free to expire.

These tests are written directly from
`packages/hammertime-store/src/hammertime/store/interface.py`'s
`DedupStore` Protocol docstrings, NOT from `hammertime.store.redis`'s
implementation -- this repo's test-author convention has the coder and the
test author work blind to each other, reconciled afterward. They
deliberately do not assume any of `memory.py`/`dedup.py`'s internal
`SequenceWindow` "bounded out-of-order set" behavior (high-water mark plus
a small set of sequences seen ahead of a gap): that is documented as a
`memory.py`-internal data structure, not part of the `DedupStore` Protocol
itself, and nothing in `interface.py` requires a Redis backend to
replicate it. What these tests *do* assume, from `dedup.py`'s
`SequenceKey.cache_key()` docstring ("a stable string form, e.g. for use
as a Redis key" -- built with an `\x1f` separator specifically so
`(agent_id, sequence)` pairs can't collide under naive string
concatenation), is that whatever key scheme the Redis backend actually
uses preserves that same collision-safety -- exercised here only through
the public `has_seen`/`mark_seen` interface, never `cache_key()` itself.

No live Redis is available in this environment (and none is expected in
plain CI either). `fakeredis`'s async fake (`fakeredis.aioredis.FakeRedis`)
stands in for a real Redis server, per issue #31's acceptance criterion:
cover the Redis-backed store with a well-maintained in-process fake rather
than skipping this coverage. `fakeredis` is a dev dependency added to the
root `pyproject.toml`'s `[dependency-groups] dev` list alongside `httpx`
et al.

TTL expiry in Redis is real wall-clock time, unlike `memory.py`'s
injectable `Clock` -- there is no `ManualClock` equivalent to inject here.
This environment has no Bash/network access to confirm whether the
installed `fakeredis` version supports manually advancing its internal
clock (some versions do), so the safe, always-correct fallback the issue
explicitly allows is used instead: short real TTLs (1-5s) with real
`asyncio.sleep`. If manual clock control is confirmed available later,
these TTL tests are good candidates to speed up.

ASSUMED constructor signature (not yet known -- `redis.py` is being
written in parallel by the coder; reconcile `_store()` below against the
merged implementation):

    from hammertime.store.redis import RedisDedupStore
    RedisDedupStore(client: redis.asyncio.Redis)

mirroring `MemoryDedupStore(clock: Clock | None = None)`'s pattern of
taking its single external dependency (there, a `Clock`; here, a Redis
client) by constructor injection. `ttl_seconds` is NOT assumed to be fixed
at construction -- it's passed per-call to `mark_seen`, per
`DedupStore`'s actual Protocol signature. If the real constructor differs
(e.g. it takes a connection URL/DSN instead of a client instance, or a
required `key_prefix`), only `_store()` below should need to change.
"""

import asyncio

import fakeredis.aioredis
from hammertime.store.interface import DedupStore
from hammertime.store.redis import RedisDedupStore

DEFAULT_TTL_SECONDS = 30


def _store() -> tuple[RedisDedupStore, fakeredis.aioredis.FakeRedis]:
    """A fresh `RedisDedupStore` backed by an isolated in-process fake Redis.

    A brand new `FakeRedis` instance per call (no `FakeServer` shared across
    tests) keeps each test's data isolated from every other test, the same
    way a fresh `MemoryDedupStore` isolates in-memory tests.
    """
    client = fakeredis.aioredis.FakeRedis()
    store = RedisDedupStore(client)
    return store, client


class TestProtocolConformance:
    def test_redis_dedup_store_satisfies_the_dedup_store_protocol(self) -> None:
        store, _client = _store()

        assert isinstance(store, DedupStore)


class TestFirstSeen:
    async def test_fresh_pair_is_not_a_duplicate(self) -> None:
        store, _client = _store()

        assert await store.has_seen("agent-1", 1) is False

    async def test_checking_a_fresh_pair_does_not_mark_it_as_seen(self) -> None:
        # has_seen() is a read: calling it should not have the side effect
        # of marking the pair seen for the *next* call.
        store, _client = _store()

        await store.has_seen("agent-1", 1)

        assert await store.has_seen("agent-1", 1) is False


class TestMarkThenCheck:
    async def test_marking_a_pair_seen_then_checking_reports_a_duplicate(self) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True

    async def test_marking_an_already_seen_pair_again_is_not_an_error(self) -> None:
        # Retries are free per ADR-0003; re-marking must not raise.
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True


class TestAgentIsolation:
    async def test_same_sequence_number_on_different_agents_is_independent(self) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 42, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 42) is True
        assert await store.has_seen("agent-2", 42) is False

    async def test_marking_one_agent_seen_does_not_affect_another_agents_same_sequence(
        self,
    ) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 42, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-2", 42, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 42) is True
        assert await store.has_seen("agent-2", 42) is True


class TestSequenceIsolation:
    async def test_different_sequences_for_the_same_agent_are_tracked_independently(
        self,
    ) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-1", 2) is False

    async def test_marking_multiple_sequences_for_one_agent_is_not_confused(self) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 2, ttl_seconds=DEFAULT_TTL_SECONDS)
        await store.mark_seen("agent-1", 3, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-1", 2) is True
        assert await store.has_seen("agent-1", 3) is True
        assert await store.has_seen("agent-1", 4) is False


class TestKeyCollisionSafety:
    """`dedup.py`'s `SequenceKey.cache_key()` exists specifically because a
    naive `agent_id + str(sequence)` concatenation can collide across
    different pairs; it joins them with `\\x1f` instead. Whatever key
    format the Redis backend actually uses internally, it must preserve
    that same collision-safety -- checked here only through
    `has_seen`/`mark_seen`, never by inspecting the backend's raw keys.
    """

    async def test_pairs_that_would_collide_under_naive_concatenation_stay_independent(
        self,
    ) -> None:
        # "agent-1" + "23" == "agent-12" + "3" == "agent-123" under plain
        # string concatenation -- these two distinct pairs must not be
        # treated as the same dedup entry.
        store, _client = _store()

        await store.mark_seen("agent-1", 23, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 23) is True
        assert await store.has_seen("agent-12", 3) is False


class TestTtlExpiry:
    async def test_entry_expires_after_its_ttl_and_is_treated_as_not_seen_again(self) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=1)
        await asyncio.sleep(1.3)  # comfortably past the 1s TTL

        assert await store.has_seen("agent-1", 1) is False

    async def test_entry_well_before_ttl_expiry_is_still_a_duplicate(self) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=2)
        await asyncio.sleep(0.3)  # well short of the 2s TTL

        assert await store.has_seen("agent-1", 1) is True

    async def test_expiry_is_independent_per_agent(self) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=1)
        await asyncio.sleep(0.1)
        await store.mark_seen("agent-2", 1, ttl_seconds=5)
        # By now agent-1's entry is ~1.3s old (past its 1s ttl); agent-2's
        # is ~1.2s old (well within its 5s ttl).
        await asyncio.sleep(1.2)

        assert await store.has_seen("agent-1", 1) is False
        assert await store.has_seen("agent-2", 1) is True

    async def test_re_marking_after_expiry_starts_a_fresh_ttl(self) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=1)
        await asyncio.sleep(1.3)
        assert await store.has_seen("agent-1", 1) is False  # expired

        await store.mark_seen("agent-1", 1, ttl_seconds=DEFAULT_TTL_SECONDS)

        assert await store.has_seen("agent-1", 1) is True

    async def test_different_ttl_seconds_can_be_passed_on_different_calls(self) -> None:
        # ttl_seconds is a per-call parameter, not fixed at construction --
        # confirm mark_seen accepts a different ttl_seconds per call without
        # the calls interfering with each other.
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=1)
        await store.mark_seen("agent-1", 2, ttl_seconds=5)

        assert await store.has_seen("agent-1", 1) is True
        assert await store.has_seen("agent-1", 2) is True

    async def test_a_later_call_with_a_smaller_ttl_seconds_does_not_shorten_the_pairs_expiry(
        self,
    ) -> None:
        # mark_seen's contract promises a pair stays seen "for at least
        # ttl_seconds" (interface.py), not "for exactly ttl_seconds until
        # overwritten" -- a later call for the *same* (agent_id, sequence)
        # pair with a *smaller* ttl_seconds must not undercut an earlier
        # call's longer promise. Mirrors test_memory_dedup.py's regression
        # test for the same "at least ttl_seconds" contract.
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=5)
        await store.mark_seen("agent-1", 1, ttl_seconds=1)
        # Past the second call's 1s ttl, nowhere near the first's 5s.
        await asyncio.sleep(1.3)

        assert await store.has_seen("agent-1", 1) is True

    async def test_a_later_call_with_a_larger_ttl_seconds_extends_the_pairs_expiry(self) -> None:
        store, _client = _store()

        await store.mark_seen("agent-1", 1, ttl_seconds=1)
        await store.mark_seen("agent-1", 1, ttl_seconds=5)
        # Past the first call's 1s ttl; within the second call's 5s ttl.
        await asyncio.sleep(1.3)

        assert await store.has_seen("agent-1", 1) is True
