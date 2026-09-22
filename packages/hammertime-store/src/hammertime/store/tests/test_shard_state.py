"""Durable per-shard HOT set and sequence counter (`ShardStateStore`).

Spec: section 20 (one owner per IP; the shard owns the IP's HOT/COLD
state), section 26 (the window store may hold far more IPs than the trie;
only the HOT set is kept beyond it), section 32 (the IP sliding-window
state plus the HOT/COLD state is the *authoritative* information -- the
trie is derived from the transitions the aggregator emits).
ADR-0011 decision 5 (`ShardState`, `ShardStateStore`,
`MemoryShardStateStore`, `RedisShardStateStore`), with decision 4 for why
the store is written *before* the event is published.

Why this store exists, and therefore what these tests are really about: an
aggregator that restarts with empty memory forgets which IPs it told the
trie were HOT, never emits the matching `HotIpRemoved`, and the trie keeps
them HOT forever -- a permanent section 12 divergence reachable by a plain
`docker compose restart` (ADR-0011 context item 3). The durable HOT set is
what closes that leak. It is not a scale-out mechanism, so nothing here
tests distribution; it tests that a shard's HOT membership and its
sequence counter survive, stay per-shard, and round-trip as `Address`
objects.

Written blind to `interface.py`, `memory.py` and `redis.py`, per this
repo's test-author convention (see `test_dedup.py`'s equivalent note for
issue #31): the API is taken from ADR-0011 decision 5 only, and the coder
implements against these tests.

ASSUMPTIONS, none of them stated outright by ADR-0011 -- push back on any
of them individually rather than bending the assertions:

1. **Module layout.** The ADR names `hammertime.store.interface` for
   `ShardState`/`ShardStateStore` explicitly. It does not say where the two
   implementations live; `MemoryShardStateStore` is assumed to sit in
   `hammertime.store.memory` and `RedisShardStateStore` in
   `hammertime.store.redis`, mirroring the existing
   `MemoryDedupStore`/`RedisDedupStore` split behind `DedupStore`, which
   decision 5 explicitly points at ("chosen by `HAMMERTIME_STORE_KIND`
   exactly as ingest chooses its `DedupStore`").
2. **Constructors.** `MemoryShardStateStore()` takes no required
   arguments (the ADR calls it "reference; dicts, no TTL" -- there is no
   clock to inject, since nothing here has a TTL).
   `RedisShardStateStore(client)` takes its client by positional
   constructor injection, exactly as `RedisDedupStore(client)` already
   does. If the real constructor differs, only `make_store()` in the two
   concrete test classes below should need to change.
3. **`load()` is a read.** Loading a shard that has never recorded a
   transition must not create it (asserted for Redis by checking the
   keyspace stays empty after a `load`), and two loads in a row must agree.
   The ADR only says what `load` returns.
4. **Round-trip type, and what "IP text" is.** `ShardState.hot_ips` is
   typed `frozenset[Address]`, while the Redis backend stores "a SET of IP
   text" -- so parsing the text back into `Address` is the store's job, not
   its caller's. Asserted for both backends so the two really are
   interchangeable behind the Protocol. The text form itself is assumed to
   be `str(ip)` (the canonical form `Address.parse` round-trips, and the
   same form decision 4 puts in the envelope's `subject`); only
   `test_the_hot_key_holds_a_set_of_ip_text` depends on that.
5. **`COLD` for an IP that is not in the set.** Decision 5 says
   `record_transition` "removes it when `COLD`" and "sets the shard's next
   sequence to `sequence + 1`" as one atomic step. Set semantics make the
   removal a no-op when the IP is absent, and nothing conditions the
   sequence update on the membership change -- so the sequence still
   advances. This is a real path, not a hypothetical: decision 4's recovery
   story has the next owner demoting an inherited IP the trie may not hold.
6. **No TTL in Redis.** Decision 5 says "no TTL" and assumption 23 explains
   why (a shard's HOT set must outlive any process). Asserted as
   `TTL key == -1` ("key exists, no expiry"), which is the only observable
   form of that promise.
7. **fakeredis stands in for a real server**, as it already does for
   `RedisDedupStore` in `services/ingest/.../tests/test_dedup.py`; no live
   Redis is available here or in plain CI. It returns `bytes` key names
   (no `decode_responses=True`), so `_key_names` normalises them.

NOT tested here, deliberately:

* **Atomicity of `record_transition`.** Decision 5 requires the membership
  change and the sequence update to happen atomically (for Redis, "one
  atomic server-side step per call" -- Amendment 1 item A2 relaxed
  decision 5's original "one MULTI/EXEC transaction per call" to that,
  because MULTI/EXEC cannot express the compare-and-set the clamp needs;
  the atomicity requirement itself is unchanged). A torn write is only
  observable if the process or connection dies between the two commands,
  which an in-process fake cannot produce.
  `test_concurrent_transitions_do_not_lose_updates` covers the weaker,
  observable property (no lost update to the HOT set under interleaved
  awaits) and says so; it is not a substitute.

**Per-shard leases (ADR-0013 decision 7)** -- `ShardLeaseContract` and the
two `Test*Lease*` classes below, written from that decision's text alone:

    async def acquire_lease(self, shard: int, owner: str, ttl_seconds: float) -> str | None:
        # Take or renew shard's lease for owner. Returns None on success; otherwise
        # the id of the member that holds it. Atomic: a lease is granted iff no live
        # lease exists or the live lease is owner's own, in which case its expiry
        # becomes now + ttl_seconds.
    async def release_lease(self, shard: int, owner: str) -> None:
        # Drop shard's lease iff owner holds it; a no-op otherwise. Never raises
        # for an unheld lease.

`RedisShardStateStore`: "key `hammertime:agg:{shard}:owner`, value the owner
id, one atomic server-side step per call ..., TTL in whole seconds rounded
up". `MemoryShardStateStore` "gains an optional `clock: Clock | None = None`
constructor keyword (default `SystemClock()`, so every existing
argument-free construction is unchanged) and keeps `{shard: (owner,
expires_at)}`; an expired entry counts as absent. `load` and
`record_transition` are unchanged, and the lease key is separate from the
HOT set".

Lease ASSUMPTIONS (numbered on from the list above):

8. **How a lease lapses per backend.** The memory store takes the test's
   `ManualClock`, so `let_time_pass(seconds)` is `clock.advance(seconds)`
   and no lease test on it touches the wall clock. fakeredis keeps its own
   expiry on `time.time()` with no clock to inject, so the Redis lapse tests
   use a 2 s TTL and a real `asyncio.sleep`, as
   `services/ingest/.../tests/test_dedup.py` already does for `mark_seen`'s
   TTL; they are the only wall-clock sleeps in this file. The TTL was 1 s
   until 2026-09-22; ADR-0013 Amendment 4 ruling F widened it because the
   two "still live" assertions at `0.6 x TTL` "leave a 0.4 s margin, which a
   loaded CI runner can eat" -- now 0.8 s (assumption 75: "Roughly six extra
   seconds per suite run ... bought against a flaky gate"). The fractions
   are unchanged: "the renewal test needs the two sleeps to sum past one
   TTL, so `0.6` cannot drop below `0.5`".
9. **`str | None` literally.** A refused acquire returns the holder's id as
   a `str` (asserted with `isinstance`), never a falsy sentinel; a granted
   one returns `None`, not `True`.
10. **Renewal is observable as a later expiry.** "Its expiry becomes now +
    ttl_seconds": a renewal at 60 % of the TTL keeps the lease alive past
    the original expiry, which is what `renew_leases()` (ADR-0013 decision
    7) relies on every maintenance interval.
11. **Whole seconds rounded up** is asserted on the Redis key's `TTL`: a
    `ttl_seconds` of `0.4` yields `TTL == 1` and one of `2.5` yields `3`
    (fakeredis reports whole seconds; a value of `2` would mean rounding
    down or to nearest, both of which the ADR rules out).
12. **The lease key of a never-recorded shard is the only key written**, so
    a lease is observable in the keyspace without a `:hot`/`:seq` pair, and
    releasing it deletes the key rather than leaving an empty value.

Formerly listed here as unpinned, now tested: **an out-of-order
`sequence`**. Whether a lower `sequence` lowers `next_sequence` was left
untested and flagged for the architect; ADR-0011 Amendment 1 item A2 ruled
it. `record_transition` MUST clamp -- the stored next sequence becomes
`max(<value before the call>, sequence + 1)` and is never lowered, so
`load(shard).next_sequence == 1 + max(every sequence ever recorded for
shard)` (or `0` if none has been) whatever the call order and however often
a call was repeated; the membership change is applied regardless of how
`sequence` compares to the stored counter (A2's assumptions: a sequence
fence here would be "a half-built split-brain guard"); and `sequence < 0`
is a `ValueError` that writes nothing. Pinned in `ShardStateStoreContract`
below, so both backends are held to it
(`test_a_lower_sequence_does_not_lower_next_sequence` onwards), plus
`TestRedisKeyspace.test_a_refused_negative_sequence_writes_no_keys`.
"""

import asyncio
import math
from typing import ClassVar

import fakeredis.aioredis
import pytest
from hammertime.core.addressing.address import Address
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import ManualClock
from hammertime.store.interface import ShardState, ShardStateStore
from hammertime.store.memory import MemoryShardStateStore
from hammertime.store.redis import RedisShardStateStore

IP_A = Address.parse("198.51.100.7")
IP_B = Address.parse("203.0.113.19")
IP_C = Address.parse("192.0.2.200")

# ADR-0013 decision 7: leases are keyed by member id.
MEMBER_A = "aggregator-0"
MEMBER_B = "aggregator-1"
MEMBER_C = "aggregator-2"

BASE = 1_800_000_000


async def _key_names(client: fakeredis.aioredis.FakeRedis, pattern: str) -> set[str]:
    """Key names matching `pattern`, normalised to `str` (fakeredis yields bytes)."""
    raw = await client.keys(pattern)
    return {name.decode() if isinstance(name, bytes) else str(name) for name in raw}


class ShardStateStoreContract:
    """Behaviour every `ShardStateStore` owes ADR-0011 decision 5.

    Subclassed once per backend so both are held to the same contract; not
    collected itself (no `Test` prefix).
    """

    def make_store(self) -> ShardStateStore:
        raise NotImplementedError

    def test_satisfies_the_shard_state_store_protocol(self) -> None:
        assert isinstance(self.make_store(), ShardStateStore)

    async def test_never_seen_shard_loads_as_empty_with_sequence_zero(self) -> None:
        # Decision 5: next_sequence is "0 for a shard that has never
        # recorded a transition", and its HOT set is empty -- a first-ever
        # claim inherits nothing and starts numbering at 0.
        store = self.make_store()

        assert await store.load(0) == ShardState(hot_ips=frozenset(), next_sequence=0)

    async def test_loading_a_never_seen_shard_does_not_create_it(self) -> None:
        store = self.make_store()

        await store.load(0)

        assert await store.load(0) == ShardState(hot_ips=frozenset(), next_sequence=0)

    async def test_recording_hot_adds_the_ip_and_advances_the_sequence(self) -> None:
        # "record_transition adds ip to the shard's HOT set when state is
        # HOT ... and sets the shard's next sequence to sequence + 1".
        store = self.make_store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)

        state = await store.load(0)
        assert state.hot_ips == frozenset({IP_A})
        assert state.next_sequence == 1

    async def test_recording_cold_removes_the_ip_and_advances_the_sequence(self) -> None:
        # The demotion half of the same rule, with a sequence that is not
        # simply "one more": next_sequence follows the recorded sequence,
        # not a count of calls.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 0)

        await store.record_transition(0, IP_A, IpState.COLD, 5)

        state = await store.load(0)
        assert state.hot_ips == frozenset()
        assert state.next_sequence == 6

    async def test_recording_hot_twice_for_the_same_ip_is_idempotent_for_membership(self) -> None:
        # Consumption is at-least-once (decision 3): "an IP the store
        # already has as HOT is not re-announced", so a redelivered
        # promotion must leave the set with exactly one entry.
        store = self.make_store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(0, IP_A, IpState.HOT, 1)

        state = await store.load(0)
        assert state.hot_ips == frozenset({IP_A})
        assert state.next_sequence == 2

    async def test_recording_cold_for_an_ip_that_is_not_hot_is_a_no_op_for_membership(self) -> None:
        # Assumption 5 above: set semantics make the removal a no-op, and
        # the sequence still advances. Decision 4's recovery path reaches
        # this (a new owner demoting an IP the trie never learned about).
        store = self.make_store()

        await store.record_transition(0, IP_A, IpState.COLD, 3)

        state = await store.load(0)
        assert state.hot_ips == frozenset()
        assert state.next_sequence == 4

    async def test_several_hot_ips_accumulate_in_one_shards_set(self) -> None:
        store = self.make_store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(0, IP_B, IpState.HOT, 1)
        await store.record_transition(0, IP_C, IpState.HOT, 2)

        state = await store.load(0)
        assert state.hot_ips == frozenset({IP_A, IP_B, IP_C})
        assert state.next_sequence == 3

    async def test_demoting_one_ip_leaves_the_others_hot(self) -> None:
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(0, IP_B, IpState.HOT, 1)

        await store.record_transition(0, IP_A, IpState.COLD, 2)

        state = await store.load(0)
        assert state.hot_ips == frozenset({IP_B})

    async def test_an_ip_can_be_promoted_again_after_being_demoted(self) -> None:
        # Section 32's example transition history: HotIpAdded, HotIpRemoved,
        # HotIpAdded, ... -- the store must follow it without sticking.
        store = self.make_store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(0, IP_A, IpState.COLD, 1)
        await store.record_transition(0, IP_A, IpState.HOT, 2)

        state = await store.load(0)
        assert state.hot_ips == frozenset({IP_A})
        assert state.next_sequence == 3

    async def test_shards_hot_sets_are_independent(self) -> None:
        # Section 20: each IP has exactly one owner, and a shard owns its
        # own HOT/COLD state. A transition on shard 0 must be invisible to
        # shard 1, whose window is claimed by a different worker.
        store = self.make_store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)

        assert await store.load(1) == ShardState(hot_ips=frozenset(), next_sequence=0)

    async def test_shards_sequence_counters_are_independent(self) -> None:
        store = self.make_store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(1, IP_B, IpState.HOT, 41)

        shard_0 = await store.load(0)
        shard_1 = await store.load(1)
        assert shard_0.hot_ips == frozenset({IP_A})
        assert shard_0.next_sequence == 1
        assert shard_1.hot_ips == frozenset({IP_B})
        assert shard_1.next_sequence == 42

    async def test_the_same_ip_can_be_hot_on_two_shards_without_interference(self) -> None:
        # Ownership means no IP is ever really in two shards at once, but
        # the store must not conflate them if a handover records the same
        # IP under a different shard id -- demoting it on one shard must
        # not demote it on the other.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(1, IP_A, IpState.HOT, 0)

        await store.record_transition(0, IP_A, IpState.COLD, 1)

        assert (await store.load(0)).hot_ips == frozenset()
        assert (await store.load(1)).hot_ips == frozenset({IP_A})

    async def test_loaded_hot_ips_is_a_frozenset_of_addresses(self) -> None:
        # ShardState.hot_ips is typed frozenset[Address]; a backend that
        # stores IP text (Redis does) must parse it back, so that
        # ShardWindow(inherited_hot=state.hot_ips) works either way.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 0)

        hot_ips = (await store.load(0)).hot_ips

        assert isinstance(hot_ips, frozenset)
        assert all(isinstance(ip, Address) for ip in hot_ips)

    async def test_ipv6_addresses_round_trip(self) -> None:
        # ADR-0011 assumption 20: IPv6 observations are accepted; Address
        # and the ring are family-agnostic, so the HOT set must be too.
        store = self.make_store()
        ipv6 = Address.parse("2001:db8::dead:beef")

        await store.record_transition(0, ipv6, IpState.HOT, 0)

        assert (await store.load(0)).hot_ips == frozenset({ipv6})

    async def test_load_does_not_consume_the_state(self) -> None:
        # A claim loads the shard once, but a re-claim after a revoke loads
        # it again; loading must not drain the HOT set.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 7)

        first = await store.load(0)
        second = await store.load(0)

        assert first == second
        assert second.hot_ips == frozenset({IP_A})
        assert second.next_sequence == 8

    async def test_concurrent_transitions_do_not_lose_updates(self) -> None:
        # NOT an atomicity test (see the module docstring): a torn
        # MULTI/EXEC is not observable in-process. This only pins the
        # weaker property that interleaved awaits do not lose a membership
        # update -- e.g. a read-modify-write of the whole set would.
        store = self.make_store()
        ips = [Address.parse(f"198.51.100.{octet}") for octet in range(1, 11)]

        await asyncio.gather(
            *(
                store.record_transition(0, ip, IpState.HOT, sequence)
                for sequence, ip in enumerate(ips)
            )
        )

        state = await store.load(0)
        assert state.hot_ips == frozenset(ips)
        # Which call finished last is up to the interleaving, so only the
        # range is pinned: some transition's sequence + 1.
        assert 1 <= state.next_sequence <= len(ips)

    # --- ADR-0011 Amendment 1, item A2: `record_transition` never lowers
    # `next_sequence`. "After `record_transition(shard, ip, state,
    # sequence)` returns, the shard's stored next sequence is
    # `max(<value before the call>, sequence + 1)`; it is never lowered."
    # The store is the only party that outlives the process, so it is where
    # decision 4's promise ("never reproduces an earlier `event_id`") is
    # made unconditional.

    async def test_a_lower_sequence_does_not_lower_next_sequence(self) -> None:
        # max(10, 3) == 10. An unconditional assignment would hand the next
        # claimant of the shard a `next_sequence` it had already used.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 9)

        await store.record_transition(0, IP_B, IpState.HOT, 2)

        assert (await store.load(0)).next_sequence == 10

    async def test_a_stale_sequence_still_applies_the_hot_set_change(self) -> None:
        # A2: "The membership update (add on HOT, remove on COLD) is applied
        # regardless of how `sequence` compares to the stored counter." Its
        # assumptions section says using the sequence as a fence here would
        # be "a half-built split-brain guard" -- the store has no fencing
        # token, so it must not behave as though it had one.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 9)

        await store.record_transition(0, IP_B, IpState.HOT, 0)

        state = await store.load(0)
        assert state.hot_ips == frozenset({IP_A, IP_B})
        assert state.next_sequence == 10

    async def test_a_stale_cold_transition_still_removes_the_ip(self) -> None:
        # The demotion half of the same rule: a stale sequence must not
        # suppress the removal, or the shard would keep announcing an IP as
        # inherited-HOT that its owner has already demoted.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 4)
        await store.record_transition(0, IP_B, IpState.HOT, 9)

        await store.record_transition(0, IP_A, IpState.COLD, 5)

        state = await store.load(0)
        assert state.hot_ips == frozenset({IP_B})
        assert state.next_sequence == 10

    async def test_next_sequence_is_one_past_the_highest_sequence_ever_recorded(self) -> None:
        # A2's observable contract: "load(shard).next_sequence == 1 + max(
        # every sequence ever recorded for shard) ... whatever order the
        # calls came in and however many times any of them was repeated."
        # (The "or 0 if none has been" half is
        # `test_never_seen_shard_loads_as_empty_with_sequence_zero`.)
        store = self.make_store()

        for sequence in (3, 11, 0, 11, 7, 1, 11, 2):
            await store.record_transition(0, IP_A, IpState.HOT, sequence)

        assert (await store.load(0)).next_sequence == 12

    async def test_the_high_water_mark_does_not_depend_on_call_order(self) -> None:
        # Same multiset of recorded sequences, opposite orders, identical
        # resulting state -- "whatever order the calls came in".
        ascending = self.make_store()
        descending = self.make_store()

        for sequence in (0, 4, 9):
            await ascending.record_transition(0, IP_A, IpState.HOT, sequence)
        for sequence in (9, 4, 0):
            await descending.record_transition(0, IP_A, IpState.HOT, sequence)

        assert await ascending.load(0) == await descending.load(0)
        assert await ascending.load(0) == ShardState(hot_ips=frozenset({IP_A}), next_sequence=10)

    async def test_replaying_the_same_transition_leaves_the_store_unchanged(self) -> None:
        # A2: "Recording the same (shard, ip, state, sequence) twice leaves
        # the store exactly as one call would have (idempotent under
        # replay)" -- the retried call whose first attempt the server
        # applied but whose reply was lost. The clamp is what makes the
        # replay a no-op instead of a regression.
        once = self.make_store()
        twice = self.make_store()
        for store in (once, twice):
            await store.record_transition(0, IP_A, IpState.HOT, 0)
            await store.record_transition(0, IP_B, IpState.HOT, 5)

        await twice.record_transition(0, IP_B, IpState.HOT, 5)

        assert await twice.load(0) == await once.load(0)
        assert await twice.load(0) == ShardState(hot_ips=frozenset({IP_A, IP_B}), next_sequence=6)

    async def test_replaying_a_demotion_is_a_no_op(self) -> None:
        # The COLD half of replay idempotence: re-applying an already
        # applied removal neither resurrects the IP nor advances the
        # counter past `sequence + 1`.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(0, IP_A, IpState.COLD, 1)
        before = await store.load(0)

        await store.record_transition(0, IP_A, IpState.COLD, 1)

        assert await store.load(0) == before
        assert before == ShardState(hot_ips=frozenset(), next_sequence=2)

    async def test_a_negative_sequence_is_a_value_error(self) -> None:
        # A2: "`sequence < 0` is a ValueError and writes nothing."
        # `schemas/hot_ip_event.v1.json` declares `sequence` as an integer
        # with "minimum": 0, so a negative value could never be emitted and
        # can only be a bug; the store is where it is cheapest to catch.
        store = self.make_store()

        with pytest.raises(ValueError):
            await store.record_transition(0, IP_A, IpState.HOT, -1)

    async def test_a_negative_sequence_writes_nothing(self) -> None:
        # The other half of the same sentence, asserted on both the HOT set
        # and the counter: the refused promotion must not add IP_B, the
        # refused demotion must not remove IP_A, and neither may touch
        # `next_sequence`.
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 4)
        before = await store.load(0)

        with pytest.raises(ValueError):
            await store.record_transition(0, IP_B, IpState.HOT, -1)
        with pytest.raises(ValueError):
            await store.record_transition(0, IP_A, IpState.COLD, -7)

        assert await store.load(0) == before
        assert before == ShardState(hot_ips=frozenset({IP_A}), next_sequence=5)

    async def test_a_negative_sequence_does_not_create_a_never_seen_shard(self) -> None:
        # "Writes nothing" includes not bringing the shard into existence:
        # a refused call leaves the shard exactly as unclaimed as before.
        store = self.make_store()

        with pytest.raises(ValueError):
            await store.record_transition(0, IP_A, IpState.HOT, -1)

        assert await store.load(0) == ShardState(hot_ips=frozenset(), next_sequence=0)


class TestMemoryShardStateStore(ShardStateStoreContract):
    def make_store(self) -> ShardStateStore:
        return MemoryShardStateStore()


class TestRedisShardStateStore(ShardStateStoreContract):
    def make_store(self) -> ShardStateStore:
        # A brand new FakeRedis per store keeps each test isolated, the way
        # a fresh MemoryShardStateStore does.
        return RedisShardStateStore(fakeredis.aioredis.FakeRedis())


class TestShardStateValue:
    """`ShardState` itself: `@dataclass(frozen=True, slots=True)`."""

    def test_is_frozen(self) -> None:
        # dataclasses.FrozenInstanceError subclasses AttributeError; the
        # repo's other frozen-value tests assert AttributeError, which holds
        # for a slotted frozen dataclass either way.
        state = ShardState(hot_ips=frozenset({IP_A}), next_sequence=3)

        with pytest.raises(AttributeError):
            state.next_sequence = 4  # type: ignore[misc]

    def test_hot_ips_cannot_be_rebound(self) -> None:
        state = ShardState(hot_ips=frozenset({IP_A}), next_sequence=3)

        with pytest.raises(AttributeError):
            state.hot_ips = frozenset()  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        # slots=True: one ShardState per claimed shard is cheap, and a
        # typo'd attribute is an error rather than a silent new field.
        state = ShardState(hot_ips=frozenset(), next_sequence=0)

        assert not hasattr(state, "__dict__")

    def test_equal_states_compare_equal(self) -> None:
        # Value semantics: the tests above compare whole ShardStates.
        one = ShardState(hot_ips=frozenset({IP_A}), next_sequence=1)
        other = ShardState(hot_ips=frozenset({IP_A}), next_sequence=1)

        assert one == other

    def test_states_differing_only_in_sequence_are_not_equal(self) -> None:
        one = ShardState(hot_ips=frozenset({IP_A}), next_sequence=1)
        other = ShardState(hot_ips=frozenset({IP_A}), next_sequence=2)

        assert one != other


class TestShardStateStoreProtocol:
    """The Protocol is `@runtime_checkable`, so `isinstance` is meaningful."""

    def test_memory_store_is_an_instance_of_the_protocol(self) -> None:
        assert isinstance(MemoryShardStateStore(), ShardStateStore)

    def test_redis_store_is_an_instance_of_the_protocol(self) -> None:
        assert isinstance(RedisShardStateStore(fakeredis.aioredis.FakeRedis()), ShardStateStore)

    def test_an_unrelated_object_is_not_an_instance_of_the_protocol(self) -> None:
        # Guards against a Protocol with no members, which every object
        # would satisfy and the two tests above would not catch.
        assert not isinstance(object(), ShardStateStore)


class TestRedisKeyspace:
    """Decision 5's Redis keys: `hammertime:agg:{shard}:hot` / `:seq`, no TTL.

    The `hammertime:agg:` prefix is load-bearing beyond tidiness: ADR-0011's
    Consequences section warns that an `allkeys-*` eviction policy dropping
    these keys would silently recreate the trie leak this store closes, and
    the deploy epic tracks that keyspace by name.
    """

    def _store(self) -> tuple[RedisShardStateStore, fakeredis.aioredis.FakeRedis]:
        client = fakeredis.aioredis.FakeRedis()
        return RedisShardStateStore(client), client

    async def test_a_transition_writes_only_keys_under_the_hammertime_agg_prefix(self) -> None:
        store, client = self._store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)

        prefixed = await _key_names(client, "hammertime:agg:*")
        assert prefixed
        assert prefixed == await _key_names(client, "*")

    async def test_the_hot_set_and_sequence_keys_are_named_per_shard(self) -> None:
        store, client = self._store()

        await store.record_transition(3, IP_A, IpState.HOT, 0)

        assert await _key_names(client, "hammertime:agg:*") == {
            "hammertime:agg:3:hot",
            "hammertime:agg:3:seq",
        }

    async def test_two_shards_use_disjoint_keys(self) -> None:
        store, client = self._store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(1, IP_B, IpState.HOT, 0)

        assert await _key_names(client, "hammertime:agg:0:*") == {
            "hammertime:agg:0:hot",
            "hammertime:agg:0:seq",
        }
        assert await _key_names(client, "hammertime:agg:1:*") == {
            "hammertime:agg:1:hot",
            "hammertime:agg:1:seq",
        }

    async def test_the_hot_key_holds_a_set_of_ip_text(self) -> None:
        store, client = self._store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)
        await store.record_transition(0, IP_B, IpState.HOT, 1)

        members = await client.smembers("hammertime:agg:0:hot")
        assert {m.decode() if isinstance(m, bytes) else str(m) for m in members} == {
            str(IP_A),
            str(IP_B),
        }

    async def test_keys_carry_no_ttl(self) -> None:
        # Decision 5 / assumption 23: no TTL -- a shard's HOT set must
        # outlive any process. -1 is "key exists, never expires"; -2 would
        # mean the key is missing, so this also asserts both keys exist.
        store, client = self._store()

        await store.record_transition(0, IP_A, IpState.HOT, 0)

        assert await client.ttl("hammertime:agg:0:hot") == -1
        assert await client.ttl("hammertime:agg:0:seq") == -1

    async def test_loading_a_never_seen_shard_writes_nothing(self) -> None:
        store, client = self._store()

        await store.load(11)

        assert await _key_names(client, "*") == set()

    async def test_a_refused_negative_sequence_writes_no_keys(self) -> None:
        # ADR-0011 Amendment 1 item A2: "`sequence < 0` is a ValueError and
        # writes nothing". The contract tests above assert that through
        # `load`; here it is asserted against the keyspace itself, so a
        # backend that created the `:seq` key before validating would fail.
        store, client = self._store()

        with pytest.raises(ValueError):
            await store.record_transition(0, IP_A, IpState.HOT, -1)

        assert await _key_names(client, "*") == set()

    async def test_demoting_the_last_hot_ip_keeps_the_sequence_key(self) -> None:
        # The HOT set may legitimately empty out; the shard's sequence must
        # not restart at 0 afterwards, or a later transition could reproduce
        # an earlier event_id (decision 4's identity rule).
        store, client = self._store()
        await store.record_transition(0, IP_A, IpState.HOT, 0)

        await store.record_transition(0, IP_A, IpState.COLD, 1)

        state = await store.load(0)
        assert state.hot_ips == frozenset()
        assert state.next_sequence == 2
        assert "hammertime:agg:0:seq" in await _key_names(client, "*")


class TestRedisStateSurvivesTheProcess:
    """The restart the whole store exists for (ADR-0011 context item 3).

    A new `RedisShardStateStore` over the same server stands in for a new
    aggregator process claiming the shard: it must inherit the HOT set and
    resume the sequence, so the IPs it told the trie were HOT can still be
    demoted and `event_id`s cannot repeat.
    """

    async def test_a_fresh_store_over_the_same_server_inherits_the_hot_set(self) -> None:
        client = fakeredis.aioredis.FakeRedis()
        await RedisShardStateStore(client).record_transition(0, IP_A, IpState.HOT, 0)
        await RedisShardStateStore(client).record_transition(0, IP_B, IpState.HOT, 1)

        state = await RedisShardStateStore(client).load(0)

        assert state.hot_ips == frozenset({IP_A, IP_B})
        assert state.next_sequence == 2

    async def test_a_fresh_store_can_demote_an_inherited_ip(self) -> None:
        client = fakeredis.aioredis.FakeRedis()
        await RedisShardStateStore(client).record_transition(0, IP_A, IpState.HOT, 0)

        successor = RedisShardStateStore(client)
        inherited = await successor.load(0)
        await successor.record_transition(0, IP_A, IpState.COLD, inherited.next_sequence)

        state = await successor.load(0)
        assert inherited.hot_ips == frozenset({IP_A})
        assert state.hot_ips == frozenset()
        assert state.next_sequence == 2


# --------------------------------------------------------------------------
# ADR-0013 decision 7: the per-shard lease (#90)
# --------------------------------------------------------------------------


class ShardLeaseContract:
    """Behaviour every `ShardStateStore` owes ADR-0013 decision 7's lease.

    Subclassed once per backend so both are held to the same contract; not
    collected itself (no `Test` prefix). `LEASE_TTL` and `let_time_pass` are
    the backend's (ASSUMPTION 8): the memory store advances an injected
    `ManualClock`, the Redis store sleeps for real against fakeredis's own
    expiry.
    """

    LEASE_TTL: ClassVar[float]

    def make_store(self) -> ShardStateStore:
        raise NotImplementedError

    async def let_time_pass(self, seconds: float) -> None:
        raise NotImplementedError

    async def test_a_free_shard_is_granted(self) -> None:
        # "Returns None on success".
        store = self.make_store()

        assert await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL) is None

    async def test_the_holder_may_acquire_again(self) -> None:
        # "a lease is granted iff no live lease exists or the live lease is
        # owner's own" -- the renewal path `renew_leases()` takes.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        assert await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL) is None

    async def test_another_owner_is_refused_while_the_lease_is_live(self) -> None:
        # "otherwise the id of the member that holds it" -- ASSUMPTION 9: a
        # `str`, the holder's own id.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        holder = await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL)

        assert holder == MEMBER_A
        assert isinstance(holder, str)

    async def test_a_refused_acquire_does_not_take_the_lease(self) -> None:
        # The holder is still the holder afterwards: a third member is
        # refused with MEMBER_A's id, not MEMBER_B's.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)
        await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL)

        assert await store.acquire_lease(0, MEMBER_C, self.LEASE_TTL) == MEMBER_A
        assert await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL) is None

    async def test_leases_are_per_shard(self) -> None:
        # Section 20: a shard is the unit of ownership. Holding shard 0 says
        # nothing about shard 1.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        assert await store.acquire_lease(1, MEMBER_B, self.LEASE_TTL) is None
        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) == MEMBER_A
        assert await store.acquire_lease(1, MEMBER_A, self.LEASE_TTL) == MEMBER_B

    async def test_release_by_the_holder_frees_the_shard(self) -> None:
        # "Drop shard's lease iff owner holds it".
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        await store.release_lease(0, MEMBER_A)

        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) is None

    async def test_release_by_a_non_holder_is_a_no_op(self) -> None:
        # "a no-op otherwise": the holder keeps the lease and nothing is
        # raised.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        await store.release_lease(0, MEMBER_B)

        assert await store.acquire_lease(0, MEMBER_C, self.LEASE_TTL) == MEMBER_A

    async def test_release_of_an_unheld_lease_never_raises(self) -> None:
        # "Never raises for an unheld lease."
        store = self.make_store()

        await store.release_lease(0, MEMBER_A)
        await store.release_lease(7, MEMBER_B)

        assert await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL) is None

    async def test_release_is_idempotent(self) -> None:
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        await store.release_lease(0, MEMBER_A)
        await store.release_lease(0, MEMBER_A)

        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) is None

    async def test_release_frees_only_that_shard(self) -> None:
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)
        await store.acquire_lease(1, MEMBER_A, self.LEASE_TTL)

        await store.release_lease(0, MEMBER_A)

        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) is None
        assert await store.acquire_lease(1, MEMBER_B, self.LEASE_TTL) == MEMBER_A

    async def test_an_expired_lease_counts_as_absent(self) -> None:
        # Decision 7: "A crashed member's leases expire after `lease_ttl_s`;
        # ... one with a different id waits at most `lease_ttl_s`".
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        await self.let_time_pass(self.LEASE_TTL + 0.3)

        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) is None

    async def test_after_expiry_the_old_holder_is_the_one_refused(self) -> None:
        # The narrow fencing decision 7 buys: once another member has taken
        # the lapsed lease, the old holder's renewal is refused with the new
        # holder's id, which is what turns into `ShardLeaseLostError`.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)
        await self.let_time_pass(self.LEASE_TTL + 0.3)
        await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL)

        assert await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL) == MEMBER_B

    async def test_a_lease_is_still_live_just_before_it_expires(self) -> None:
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        await self.let_time_pass(self.LEASE_TTL * 0.6)

        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) == MEMBER_A

    async def test_renewal_extends_the_expiry(self) -> None:
        # ASSUMPTION 10: "its expiry becomes now + ttl_seconds". Renewed at
        # 60 % of the TTL, the lease is still held at 120 % of the original
        # TTL, where an un-renewed one would have lapsed.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)
        await self.let_time_pass(self.LEASE_TTL * 0.6)

        assert await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL) is None
        await self.let_time_pass(self.LEASE_TTL * 0.6)

        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) == MEMBER_A

    async def test_the_same_id_reacquires_a_lapsed_lease_at_once(self) -> None:
        # Decision 7: "a replacement with the same `member_id` ... reacquires
        # at once" -- true before and after expiry alike.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)
        await self.let_time_pass(self.LEASE_TTL + 0.3)

        assert await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL) is None
        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) == MEMBER_A

    async def test_load_and_record_transition_are_unaffected_by_a_lease(self) -> None:
        # "`load` and `record_transition` are unchanged, and the lease key is
        # separate from the HOT set".
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        assert await store.load(0) == ShardState(hot_ips=frozenset(), next_sequence=0)
        await store.record_transition(0, IP_A, IpState.HOT, 4)
        assert await store.load(0) == ShardState(hot_ips=frozenset({IP_A}), next_sequence=5)

        await store.release_lease(0, MEMBER_A)
        assert await store.load(0) == ShardState(hot_ips=frozenset({IP_A}), next_sequence=5)

    async def test_a_transition_recorded_by_a_non_holder_is_not_fenced(self) -> None:
        # Decision 7 / ADR-0011 A2: "the store itself still carries no
        # fencing token on `record_transition`" -- the lease is detection,
        # not enforcement, so a write under another member's lease lands.
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL)

        await store.record_transition(0, IP_B, IpState.HOT, 0)

        assert (await store.load(0)).hot_ips == frozenset({IP_B})

    async def test_the_hot_set_does_not_grant_or_block_a_lease(self) -> None:
        store = self.make_store()
        await store.record_transition(0, IP_A, IpState.HOT, 0)

        assert await store.acquire_lease(0, MEMBER_A, self.LEASE_TTL) is None
        assert await store.acquire_lease(0, MEMBER_B, self.LEASE_TTL) == MEMBER_A


class TestMemoryShardStateStoreLease(ShardLeaseContract):
    """`MemoryShardStateStore(clock=...)`: expiry follows the injected clock."""

    LEASE_TTL: ClassVar[float] = 30.0
    # Set by `make_store`, which every test calls first; a pytest test class
    # may not define `__init__`.
    _clock: ManualClock

    def make_store(self) -> ShardStateStore:
        # A fresh clock per store, so tests never share time.
        self._clock = ManualClock(initial=BASE)
        return MemoryShardStateStore(clock=self._clock)

    async def let_time_pass(self, seconds: float) -> None:
        # `ManualClock` is integer-valued (`now() -> int`); rounding up keeps
        # "past the TTL" past it and "60 % of the TTL" (18 of 30) exact.
        self._clock.advance(math.ceil(seconds))

    def test_the_argument_free_construction_is_unchanged(self) -> None:
        # "default `SystemClock()`, so every existing argument-free
        # construction is unchanged" -- the contract above already builds
        # `MemoryShardStateStore()` throughout.
        assert isinstance(MemoryShardStateStore(), ShardStateStore)

    async def test_an_argument_free_store_grants_and_refuses_leases_too(self) -> None:
        store = MemoryShardStateStore()

        assert await store.acquire_lease(0, MEMBER_A, 30.0) is None
        assert await store.acquire_lease(0, MEMBER_B, 30.0) == MEMBER_A

    async def test_a_lease_does_not_lapse_before_its_ttl_on_the_clock(self) -> None:
        store = self.make_store()
        await store.acquire_lease(0, MEMBER_A, 30.0)

        self._clock.advance(29)

        assert await store.acquire_lease(0, MEMBER_B, 30.0) == MEMBER_A


class TestRedisShardStateStoreLease(ShardLeaseContract):
    """`RedisShardStateStore` on fakeredis: expiry is the key's TTL.

    ASSUMPTION 8: fakeredis has no injectable clock, so `let_time_pass` is a
    real `asyncio.sleep` and `LEASE_TTL` is two seconds (ADR-0013 Amendment 4
    ruling F, assumption 75; the precedent for real sleeps is
    `services/ingest/.../tests/test_dedup.py`). The 60 % renewal test
    therefore sleeps 1.2 s twice and the lapse tests 2.3 s, with 0.8 s of
    margin on each "still live" assertion.
    """

    LEASE_TTL: ClassVar[float] = 2.0

    def make_store(self) -> ShardStateStore:
        return RedisShardStateStore(fakeredis.aioredis.FakeRedis())

    async def let_time_pass(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class TestRedisLeaseKeyspace:
    """ADR-0013 decision 7's Redis key: `hammertime:agg:{shard}:owner`, value
    the owner id, "TTL in whole seconds rounded up"."""

    def _store(self) -> tuple[RedisShardStateStore, fakeredis.aioredis.FakeRedis]:
        client = fakeredis.aioredis.FakeRedis()
        return RedisShardStateStore(client), client

    async def test_the_lease_lives_under_the_owner_key(self) -> None:
        store, client = self._store()

        await store.acquire_lease(3, MEMBER_A, 30.0)

        value = await client.get("hammertime:agg:3:owner")
        assert (value.decode() if isinstance(value, bytes) else value) == MEMBER_A

    async def test_the_owner_key_is_the_only_key_a_lease_writes(self) -> None:
        # ASSUMPTION 12: separate from the HOT set and the sequence, and no
        # `:hot`/`:seq` pair is created by taking a lease.
        store, client = self._store()

        await store.acquire_lease(3, MEMBER_A, 30.0)

        assert await _key_names(client, "*") == {"hammertime:agg:3:owner"}

    async def test_the_owner_key_carries_the_ttl_in_whole_seconds(self) -> None:
        store, client = self._store()

        await store.acquire_lease(0, MEMBER_A, 30.0)

        ttl = await client.ttl("hammertime:agg:0:owner")
        assert 1 <= ttl <= 30

    async def test_a_fractional_ttl_is_rounded_up(self) -> None:
        # ASSUMPTION 11: 0.4 s -> `EX 1`; 2.5 s -> `EX 3`.
        store, client = self._store()

        await store.acquire_lease(0, MEMBER_A, 0.4)
        await store.acquire_lease(1, MEMBER_A, 2.5)

        assert await client.ttl("hammertime:agg:0:owner") == 1
        assert await client.ttl("hammertime:agg:1:owner") == 3

    async def test_renewal_rewrites_the_ttl(self) -> None:
        # A renewal with a longer TTL is visible as a longer remaining TTL:
        # "its expiry becomes now + ttl_seconds", not the earlier of the two.
        store, client = self._store()
        await store.acquire_lease(0, MEMBER_A, 5.0)

        await store.acquire_lease(0, MEMBER_A, 60.0)

        assert await client.ttl("hammertime:agg:0:owner") > 5

    async def test_a_refused_acquire_rewrites_nothing(self) -> None:
        store, client = self._store()
        await store.acquire_lease(0, MEMBER_A, 60.0)

        assert await store.acquire_lease(0, MEMBER_B, 5.0) == MEMBER_A

        value = await client.get("hammertime:agg:0:owner")
        assert (value.decode() if isinstance(value, bytes) else value) == MEMBER_A
        assert await client.ttl("hammertime:agg:0:owner") > 5

    async def test_release_deletes_the_owner_key(self) -> None:
        store, client = self._store()
        await store.acquire_lease(0, MEMBER_A, 30.0)

        await store.release_lease(0, MEMBER_A)

        assert await _key_names(client, "*") == set()

    async def test_release_by_a_non_holder_leaves_the_key(self) -> None:
        store, client = self._store()
        await store.acquire_lease(0, MEMBER_A, 30.0)

        await store.release_lease(0, MEMBER_B)

        value = await client.get("hammertime:agg:0:owner")
        assert (value.decode() if isinstance(value, bytes) else value) == MEMBER_A

    async def test_the_lease_key_does_not_touch_the_hot_and_seq_keys(self) -> None:
        store, client = self._store()
        await store.record_transition(0, IP_A, IpState.HOT, 0)

        await store.acquire_lease(0, MEMBER_A, 30.0)
        await store.release_lease(0, MEMBER_A)

        assert await _key_names(client, "*") == {"hammertime:agg:0:hot", "hammertime:agg:0:seq"}
        assert await client.ttl("hammertime:agg:0:hot") == -1
        assert await client.ttl("hammertime:agg:0:seq") == -1

    async def test_a_fresh_store_over_the_same_server_sees_the_lease(self) -> None:
        # The lease is in the store, "reachable by every member": a second
        # process (a second client over the same server) is refused.
        client = fakeredis.aioredis.FakeRedis()
        await RedisShardStateStore(client).acquire_lease(0, MEMBER_A, 30.0)

        assert await RedisShardStateStore(client).acquire_lease(0, MEMBER_B, 30.0) == MEMBER_A
