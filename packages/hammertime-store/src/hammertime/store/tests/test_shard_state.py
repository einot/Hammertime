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
  change and the sequence update to happen atomically (one MULTI/EXEC
  transaction for Redis). A torn write is only observable if the process or
  connection dies between the two commands, which an in-process fake cannot
  produce. `test_concurrent_transitions_do_not_lose_updates` covers the
  weaker, observable property (no lost update to the HOT set under
  interleaved awaits) and says so; it is not a substitute.
* **An out-of-order `sequence`.** "Sets the shard's next sequence to
  `sequence + 1`" reads as an unconditional assignment, but decision 4's
  only caller never goes backwards, so whether a lower `sequence` must
  lower `next_sequence` or be clamped with `max()` is genuinely unpinned.
  Left untested rather than guessed at; flagged for the architect.
"""

import asyncio

import fakeredis.aioredis
import pytest
from hammertime.core.addressing.address import Address
from hammertime.core.state.enums import IpState
from hammertime.store.interface import ShardState, ShardStateStore
from hammertime.store.memory import MemoryShardStateStore
from hammertime.store.redis import RedisShardStateStore

IP_A = Address.parse("198.51.100.7")
IP_B = Address.parse("203.0.113.19")
IP_C = Address.parse("192.0.2.200")


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
