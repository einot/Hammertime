# Specification

The authoritative architecture specification is
`hammertime_spec_1.md` (Distributed IP Activity & Prefix Detection Service).

Drop the current revision into this directory and reference sections by number;
every module docstring in this repository cites the section it implements, e.g.
`Spec: §6, §30` for the HOT/COLD state machine.

Section index used throughout the code:

| § | Topic | Implemented in |
| --- | --- | --- |
| 4 | Agent ingestion protocol | `services/ingest` |
| 5 | Sliding window / buckets | `services/aggregator/window`, `core/time` |
| 6, 7, 38 | HOT/COLD hysteresis | `core/state/machine.py` |
| 8-12 | Binary IP trie & invariants | `services/trie/structure` |
| 13, 14, 31 | Prefix classification & scoring | `services/detector` |
| 16, 17 | Metadata inheritance | `services/trie/metadata` |
| 19 | Event-driven internals | `core/events`, `packages/hammertime-bus` |
| 20, 21 | Sharding & aggregation | `services/aggregator/sharding` |
| 22 | Consistency model | `docs/adr/0001` |
| 23 | Dedup | `services/ingest/dedup` |
| 24, 25 | Out-of-order, bucket math | `services/aggregator/lateness.py`, `core/time/buckets.py` |
| 26 | Memory / retention | `services/aggregator/window/expiry.py` |
| 27 | Trie representation | `services/trie/structure/patricia.py` |
| 28 | Atomicity | `services/trie/worker.py` |
| 29 | Read path | `services/trie/query` |
| 32, 33 | Persistence & snapshots | `services/trie/snapshot` |
| 34 | Versioned configuration | `core/config`, `services/aggregator/reevaluate.py` |
| 35 | IPv6 readiness | `core/addressing` |
| 36 | Security | `services/ingest/auth` |
| 37 | Observability | `core/telemetry`, `deploy/grafana` |
