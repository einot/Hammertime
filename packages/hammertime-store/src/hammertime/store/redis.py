"""Redis-backed stores: dedup keys with TTLs, shard state without.

Spec: section 20, section 26, section 32

Two backends, one Redis. `RedisDedupStore` (spec section 26) holds ingest's
dedup records, where TTL-based expiry matching `state_retention_seconds` is
the whole point. `RedisShardStateStore` (spec section 20, section 32;
ADR-0011 decision 5) holds the aggregator's per-shard HOT set and sequence
counter, which carry *no* TTL at all -- see that class's docstring for why
the two opposite retention rules are both correct, and why the second one
raises the stakes on the deployment's eviction policy.

`RedisDedupStore` implements `hammertime.store.interface.DedupStore` against
a real Redis (or Redis-compatible) backend -- the production counterpart to
`memory.py`'s in-process reference implementation, the same pattern as
issue #23's `kafka.py` in Epic #2: a structural implementation against a
real client, exercised against a live (or faked) Redis by whoever owns that
test coverage, not by unit tests in this module.

Unlike `memory.py`'s `SequenceWindow` (a compact high-water-mark-plus-gap
structure, sized specifically to bound *in-process* memory), this store
keys directly off `hammertime.store.dedup.SequenceKey.cache_key()` -- one
Redis key per `(agent_id, sequence)` -- and lets Redis's native per-key
expiry do the eviction `SequenceWindow` otherwise has to do by hand. That is
a different storage shape than `memory.py`'s, but the two remain
interchangeable behind `DedupStore`: callers only ever observe
`has_seen`/`mark_seen`, never how retention is represented internally.

`mark_seen`'s contract (interface.py) promises a sequence stays seen "for
at least `ttl_seconds`", and `ttl_seconds` is a per-call parameter rather
than fixed at construction -- so, exactly as `memory.py` documents for its
own agent-wide expiry, a later `mark_seen` call for the same key with a
*smaller* `ttl_seconds` (e.g. a detection-config change mid-flight) must
never shorten an already-set expiry, only ever extend it. A plain
unconditional `SET key value EX ttl_seconds` would reintroduce that exact
TTL-shortening bug for #31's backend right after #30 fixed it for
`memory.py`. Instead: `SET key value EX ttl_seconds NX` to create the key
with its first TTL, followed by `EXPIRE key ttl_seconds GT` (Redis 7+) to
extend -- and only ever extend -- an existing key's TTL. Each of those two
commands is atomic on its own and each individually preserves "never
shorten"; a caller-visible race between them (two concurrent `mark_seen`
calls for the same key) can only interleave into a larger resulting TTL,
never a smaller one, so the combination preserves the invariant even
though the two commands are not wrapped in a transaction together. This is
on top of -- not instead of -- the already-documented, accepted
`has_seen`-then-`mark_seen` non-atomicity (interface.py's `DedupStore`
docstring); it does not attempt to close that separate window.

One more race the two-command sequence must guard against: the existing
key's TTL can itself lapse in the (tiny, but nonzero) gap between the
failed `SET ... NX` and the following `EXPIRE ... GT` -- a short-lived
scheduling delay, or simply a `ttl_seconds` close to expiry from an
earlier call. `EXPIRE` on a key that no longer exists returns falsy and
does nothing, silently leaving the pair unmarked and violating "seen for
at least `ttl_seconds`" for that call, exactly the failure mode #30 fixed
for `memory.py`. `mark_seen` below checks `EXPIRE`'s return value and
retries via `SET ... NX` if it was falsy -- correct either way: if the key
had genuinely expired, the retry recreates it fresh; if `EXPIRE` failed
for the (impossible in single-writer Redis, but defensive) reason of the
key still existing with `gt` false, the retried `SET NX` correctly no-ops
against the still-live, already-sufficient TTL.
"""

from hammertime.core.addressing.address import Address
from hammertime.core.state.enums import IpState
from hammertime.store.dedup import SequenceKey
from hammertime.store.interface import ShardState
from redis.asyncio import Redis
from redis.exceptions import WatchError

#: Placeholder value stored at each dedup key. Only the key's existence and
#: TTL matter; the value itself carries no information.
_SEEN_VALUE = b"1"

#: Namespaces every dedup key this module writes. `SequenceKey.cache_key()` has no
#: prefix of its own, and this store is deployed against a shared Redis-protocol
#: keyspace (Valkey in the reference deployment, ADR-0012) --
#: `deploy/docker-compose.yml` points both ingest and the aggregator at the same
#: `redis://valkey:6379/0`, and the aggregator's `hammertime:agg:*` shard-state
#: keys (ADR-0011) live alongside -- so an unprefixed key would collide with an
#: unrelated key scheme in the same keyspace.
_KEY_PREFIX = "hammertime:dedup:"

#: Namespaces every key `RedisShardStateStore` writes, one pair per shard:
#: `hammertime:agg:{shard}:hot` and `hammertime:agg:{shard}:seq`. ADR-0011
#: names this keyspace explicitly -- the deploy epic's `noeviction`
#: requirement is written against this prefix, so it is load-bearing beyond
#: tidiness.
_SHARD_KEY_PREFIX = "hammertime:agg:"

#: How many times `RedisShardStateStore.record_transition` re-reads and
#: retries its WATCH/MULTI/EXEC before giving up. Each attempt loses only
#: to a concurrent writer on the *same shard's* sequence key, which spec
#: section 20's one-owner-per-shard rule makes rare; this is a guard
#: against livelock, not a throughput knob.
_MAX_SEQUENCE_CAS_ATTEMPTS = 16


def _hot_key(shard: int) -> str:
    """The SET of IP text this shard currently has as HOT."""
    return f"{_SHARD_KEY_PREFIX}{shard}:hot"


def _sequence_key(shard: int) -> str:
    """The sequence this shard's next transition will use."""
    return f"{_SHARD_KEY_PREFIX}{shard}:seq"


def _as_text(value: bytes | str) -> str:
    """Decode a SET member, whichever `decode_responses` the client was built with."""
    return value.decode() if isinstance(value, bytes) else value


class RedisDedupStore:
    """`DedupStore` backed by a real Redis, one key per `(agent_id, sequence)`.

    Takes an already-constructed `redis.asyncio.Redis` client (dependency
    injection) rather than connection parameters, matching `memory.py`'s
    injectable `Clock` -- this module does not own connection lifecycle
    (creation, pooling, retry, TLS, auth); that is the caller's
    responsibility, the same as it would be for any other shared client.

    Unlike `MemoryDedupStore` (`max_agents`) and `RateLimiter`
    (`_DEFAULT_MAX_AGENTS`), this store has no application-level cap on the
    number of keys it can create: `ttl_seconds`-based expiry bounds how long
    a key lives, not how many an attacker-influenced caller could create
    within one TTL window (up to ~2h at ADR-0003's `allowed_lateness_seconds
    + window_seconds`, before #28/#29 narrow `agent_id` to a small,
    authenticated, rate-limited set). Operating this store safely therefore
    requires the deployment to bound Redis memory itself (a `maxmemory`
    limit) -- but a naive `allkeys-lru`/`allkeys-lfu` eviction policy would
    let Redis silently evict dedup keys before their TTL, defeating this
    module's whole premise (a replayed observation would then be
    re-accepted and double-counted with no error or metric). A deployment
    running this store MUST either give the dedup keyspace (this module's
    `hammertime:dedup:` prefix) a dedicated, non-evicting Redis
    instance/logical DB, or use `maxmemory-policy noeviction` (or `volatile-*`
    coupled with every other keyspace in the same Redis being similarly
    TTL'd) so an out-of-memory condition surfaces as a loud write failure
    rather than a silent dedup bypass. Tracked for the deploy/integration
    epic (#17), not fixed at the application level here.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def has_seen(self, agent_id: str, sequence: int) -> bool:
        key = _KEY_PREFIX + SequenceKey(agent_id, sequence).cache_key()
        return bool(await self._client.exists(key))

    async def mark_seen(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds!r}")
        key = _KEY_PREFIX + SequenceKey(agent_id, sequence).cache_key()
        created = await self._client.set(key, _SEEN_VALUE, ex=ttl_seconds, nx=True)
        if created:
            return
        # Key already existed: only ever extend its expiry, never shorten
        # it (see module docstring). If the key expired in the gap between
        # the failed SET above and this EXPIRE, EXPIRE is a no-op on a
        # nonexistent key (returns falsy) -- retry via SET NX so the pair
        # ends up marked seen either way (see module docstring's second
        # race note).
        extended = await self._client.expire(key, ttl_seconds, gt=True)
        if not extended:
            await self._client.set(key, _SEEN_VALUE, ex=ttl_seconds, nx=True)

    async def claim(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> bool:
        # A single `SET ... NX` is atomic on its own -- no separate
        # `EXPIRE ... GT` follow-up like `mark_seen` needs, because a
        # `False` return here means "someone else already holds this key"
        # and its existing TTL is left untouched (not shortened, per the
        # "never shorten" invariant -- simply not touched at all).
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds!r}")
        key = _KEY_PREFIX + SequenceKey(agent_id, sequence).cache_key()
        created = await self._client.set(key, _SEEN_VALUE, ex=ttl_seconds, nx=True)
        return bool(created)


class RedisShardStateStore:
    """`ShardStateStore` backed by a real Redis: a SET plus a counter per shard.

    Two keys per shard under `hammertime:agg:{shard}:` -- `:hot`, a SET of
    IP text (`str(Address)`, the canonical form `Address.parse`
    round-trips and the same form ADR-0011 decision 4 puts in an envelope's
    `subject`), and `:seq`, the sequence the shard's next transition will
    use. Takes an already-constructed client by injection, exactly as
    `RedisDedupStore` does, for the same reason: connection lifecycle is
    the caller's.

    **No TTL, deliberately** (ADR-0011 decision 5): a shard's HOT set has
    to outlive every process that touches it. That makes the deployment
    warning in `RedisDedupStore`'s docstring apply here with more force. An
    expiring or evicted dedup key costs a re-accepted duplicate; an
    expiring or evicted `hammertime:agg:*` key costs the aggregator its
    memory of which IPs it announced as HOT, so it never emits the matching
    `HotIpRemoved` and the trie holds them forever -- silently recreating
    the exact permanent divergence (spec section 12) this store exists to
    close, with nothing to detect it. A deployment running this store MUST
    either give the `hammertime:agg:` keyspace a dedicated, non-evicting
    Redis instance/logical DB, or run `maxmemory-policy noeviction`, so
    running out of memory surfaces as a loud write failure. An
    `allkeys-lru`/`allkeys-lfu` policy is specifically unsafe here:
    `volatile-*` would at least leave these (TTL-less) keys alone, but
    `allkeys-*` will not. Tracked for the deploy/integration epic (#17),
    not fixed at the application level here.

    `load` is a pure read (two commands in one MULTI/EXEC, so the HOT set
    and the sequence are a coherent snapshot of one instant) and never
    creates a key: `SMEMBERS` on a missing key returns an empty set and
    `GET` returns `None`, which is exactly `ShardState(frozenset(), 0)`.

    `record_transition` is one atomic server-side step per call (ADR-0011
    Amendment 1 item A2 relaxes decision 5's original "one MULTI/EXEC" to
    that, precisely because the clamp it mandates is a compare-and-set and
    MULTI/EXEC cannot express one). See its body for why that is a
    WATCH-based optimistic transaction here.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def load(self, shard: int) -> ShardState:
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.smembers(_hot_key(shard))
            pipe.get(_sequence_key(shard))
            members, sequence = await pipe.execute()
        return ShardState(
            hot_ips=frozenset(Address.parse(_as_text(member)) for member in members),
            next_sequence=int(sequence) if sequence is not None else 0,
        )

    async def record_transition(
        self, shard: int, ip: Address, state: IpState, sequence: int
    ) -> None:
        if sequence < 0:
            # Write nothing: `schemas/hot_ip_event.v1.json` has
            # `"minimum": 0`, and a negative sequence would drive the
            # clamp's floor below zero.
            raise ValueError(f"sequence must be non-negative, got {sequence!r}")
        hot_key = _hot_key(shard)
        sequence_key = _sequence_key(shard)
        candidate = sequence + 1
        # ADR-0011 Amendment 1 item A2: the stored next sequence is RAISED
        # to `sequence + 1` only if that is higher, never lowered, while
        # the membership change is applied regardless -- both in one atomic
        # step. A plain MULTI/EXEC cannot express that: its queued commands
        # are sent before any of them run, so none of them can read `:seq`
        # and decide whether to write it. Hence WATCH-based optimistic
        # concurrency -- `EVAL` would do too, and A2 permits it, but it
        # would cost a Lua runtime (`lupa`) in the test environment purely
        # so the in-process fake can emulate server-side scripting.
        #
        # The comparison stays in Python on exact `int`s, which is better
        # than the Lua alternative rather than a concession: `sequence` is
        # bounded by 2**63-1, and Lua numbers are doubles that silently
        # lose integer precision above 2**53.
        async with self._client.pipeline(transaction=True) as pipe:
            for _ in range(_MAX_SEQUENCE_CAS_ATTEMPTS):
                try:
                    await pipe.watch(sequence_key)
                    stored = await pipe.get(sequence_key)
                    # redis-py leaves `multi()` unannotated, so strict
                    # mypy calls it untyped; the narrow ignore is the
                    # same shape the repo already uses for third-party
                    # typing gaps.
                    pipe.multi()  # type: ignore[no-untyped-call]
                    if state is IpState.HOT:
                        pipe.sadd(hot_key, str(ip))
                    else:
                        # SREM of an absent member is a no-op that still
                        # leaves the clamp below to run: decision 4's
                        # recovery path demotes inherited IPs that may
                        # never have been recorded.
                        pipe.srem(hot_key, str(ip))
                    if stored is None or candidate > int(stored):
                        pipe.set(sequence_key, candidate)
                    # EXEC aborts if any other client touched `:seq` since
                    # the WATCH, so the read above cannot go stale
                    # underneath the write. Nothing is applied on abort --
                    # not even the membership change, which is why it sits
                    # inside the same MULTI.
                    await pipe.execute()
                    return
                except WatchError:
                    # Another writer won the race; re-read and re-decide.
                    # Retrying is safe because SADD/SREM are idempotent and
                    # an aborted EXEC applied nothing.
                    continue
        # Bounded, not `while True`: livelock here would hang the
        # aggregator's transition path silently. Contention is expected to
        # be nil in practice (spec section 20 gives a shard exactly one
        # owner), so exhausting these attempts means something is wrong
        # that a retry loop should not paper over. Raising aborts the
        # caller's publish, which is the safe direction under decision 4:
        # better an un-announced transition than one announced but not
        # recorded.
        raise WatchError(
            f"could not record transition for shard {shard} after "
            f"{_MAX_SEQUENCE_CAS_ATTEMPTS} attempts: {sequence_key} is under contention"
        )
