"""Redis-backed store with TTLs matching state_retention_seconds.

Spec: section 26

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

from hammertime.store.dedup import SequenceKey
from redis.asyncio import Redis

#: Placeholder value stored at each dedup key. Only the key's existence and
#: TTL matter; the value itself carries no information.
_SEEN_VALUE = b"1"

#: Namespaces every key this module writes. `SequenceKey.cache_key()` has no
#: prefix of its own, and this store is deployed against a shared Redis --
#: `deploy/docker-compose.yml` points ingest and the (not yet implemented)
#: aggregator Redis counter store at the same `redis://redis:6379/0` -- so an
#: unprefixed key risks colliding with a future, unrelated key scheme in the
#: same keyspace.
_KEY_PREFIX = "hammertime:dedup:"


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
