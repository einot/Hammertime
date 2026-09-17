"""Sliding-window aggregation and HOT/COLD transitions.

Spec: section 5, section 6, section 18, section 20, section 24, section 26.

Owns per-IP state: the bucketed counter, the running total, and the current
HOT/COLD state. Emits HotIpAdded / HotIpRemoved -- and nothing else. It knows
nothing about prefixes; that separation is the scalability boundary (section 18).

ADR-0011 fixes how those pieces fit together. A shard *is* a partition of
`hammertime.observations.v1`, so ownership arrives through the consumer
group's assignment (`sharding/assignment.py`) and the aggregator never hashes
an IP itself. Each claimed shard keeps its counters in process memory
(`window/`), bounded by bucket expiry, retention and a cap; only the shard's
HOT set and its sequence counter are durable, which is what lets a restart or
a handover inherit what the trie was told rather than leaking it (decision 5).
`transitions.py` is the single call site of `evaluate_ip_state` (section 30),
persisting each edge before publishing it; `worker.py` drives the whole thing
and `service.py` composes it for the shared runner (ADR-0009).
"""
