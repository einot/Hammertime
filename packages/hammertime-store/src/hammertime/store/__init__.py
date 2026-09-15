"""State stores for sliding-window counters, IP state, and dedup records.

Spec: section 20 (ownership), section 23 (dedup), section 26 (retention).

Only dedup (spec section 23, ADR-0003) is implemented so far: `DedupStore`
is the protocol, `MemoryDedupStore` its in-process reference implementation,
`RedisDedupStore` its production backend, and `SequenceKey`/`SequenceWindow`
the underlying per-agent tracking structure `MemoryDedupStore` builds on.
Sliding-window counter and IP-state protocols belong to a different epic
and are not yet designed.
"""

from hammertime.store.dedup import DEFAULT_MAX_OUT_OF_ORDER, SequenceKey, SequenceWindow
from hammertime.store.interface import DedupStore
from hammertime.store.memory import MemoryDedupStore
from hammertime.store.redis import RedisDedupStore

__all__ = [
    "DEFAULT_MAX_OUT_OF_ORDER",
    "DedupStore",
    "MemoryDedupStore",
    "RedisDedupStore",
    "SequenceKey",
    "SequenceWindow",
]
