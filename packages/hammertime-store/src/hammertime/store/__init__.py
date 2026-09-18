"""State stores for sliding-window counters, IP state, and dedup records.

Spec: section 20 (ownership), section 23 (dedup), section 26 (retention),
section 32 (the IP state is authoritative; the trie is derived from it).

Two store families so far, each a `Protocol` with an in-process reference
implementation and a Redis production backend:

* **Dedup** (spec section 23, ADR-0003): `DedupStore`, `MemoryDedupStore`,
  `RedisDedupStore`, plus the `SequenceKey`/`SequenceWindow` per-agent
  tracking structure `MemoryDedupStore` builds on.
* **Shard state** (ADR-0011 decision 5): `ShardState`, `ShardStateStore`,
  `MemoryShardStateStore`, `RedisShardStateStore` -- the durable per-shard
  HOT set and transition sequence a restarting aggregator inherits.

Plus one validator, here because only this package knows what URL a Redis
backend can be built from (ADR-0009 A12, spec section 47):
`validate_redis_url`/`REDIS_URL_ENV`, which each service's `load_settings`
calls on `HAMMERTIME_REDIS_URL` when `store_kind == "redis"`.

Sliding-window *counters* are still in-memory only and have no protocol
here: ADR-0011 decision 5 persists just the HOT set, because the counters
self-heal within one `window_seconds` of a claim.
"""

from hammertime.store.dedup import DEFAULT_MAX_OUT_OF_ORDER, SequenceKey, SequenceWindow
from hammertime.store.interface import DedupStore, ShardState, ShardStateStore
from hammertime.store.memory import MemoryDedupStore, MemoryShardStateStore
from hammertime.store.redis import RedisDedupStore, RedisShardStateStore
from hammertime.store.url import REDIS_URL_ENV, validate_redis_url

__all__ = [
    "DEFAULT_MAX_OUT_OF_ORDER",
    "REDIS_URL_ENV",
    "DedupStore",
    "MemoryDedupStore",
    "MemoryShardStateStore",
    "RedisDedupStore",
    "RedisShardStateStore",
    "SequenceKey",
    "SequenceWindow",
    "ShardState",
    "ShardStateStore",
    "validate_redis_url",
]
