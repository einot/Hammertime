"""State stores for sliding-window counters, IP state, and dedup records.

Spec: section 20 (ownership), section 23 (dedup), section 26 (retention).

Only dedup (spec section 23, ADR-0003) is implemented so far: `DedupStore`
is the protocol, `MemoryDedupStore` its in-process reference implementation,
and `SequenceKey`/`SequenceWindow` the underlying per-agent tracking
structure. `redis.py`'s `DedupStore` implementation is a separate issue
(#31) and is not exported here yet. Sliding-window counter and IP-state
protocols belong to a different epic and are not yet designed.
"""

from hammertime.store.dedup import DEFAULT_MAX_OUT_OF_ORDER, SequenceKey, SequenceWindow
from hammertime.store.interface import DedupStore
from hammertime.store.memory import MemoryDedupStore

__all__ = [
    "DEFAULT_MAX_OUT_OF_ORDER",
    "DedupStore",
    "MemoryDedupStore",
    "SequenceKey",
    "SequenceWindow",
]
