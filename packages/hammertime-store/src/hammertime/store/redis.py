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
"""

from hammertime.store.dedup import SequenceKey
from redis.asyncio import Redis

#: Placeholder value stored at each dedup key. Only the key's existence and
#: TTL matter; the value itself carries no information.
_SEEN_VALUE = b"1"


class RedisDedupStore:
    """`DedupStore` backed by a real Redis, one key per `(agent_id, sequence)`.

    Takes an already-constructed `redis.asyncio.Redis` client (dependency
    injection) rather than connection parameters, matching `memory.py`'s
    injectable `Clock` -- this module does not own connection lifecycle
    (creation, pooling, retry, TLS, auth); that is the caller's
    responsibility, the same as it would be for any other shared client.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def has_seen(self, agent_id: str, sequence: int) -> bool:
        key = SequenceKey(agent_id, sequence).cache_key()
        return bool(await self._client.exists(key))

    async def mark_seen(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> None:
        key = SequenceKey(agent_id, sequence).cache_key()
        created = await self._client.set(key, _SEEN_VALUE, ex=ttl_seconds, nx=True)
        if not created:
            # Key already existed: only ever extend its expiry, never
            # shorten it (see module docstring).
            await self._client.expire(key, ttl_seconds, gt=True)
