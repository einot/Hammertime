# Kubernetes

* `ingest` is a Deployment with an HPA.
* `aggregator` is a StatefulSet, not autoscaled: shard assignment is static
  (ADR-0013 decision 6), so each pod carries its own disjoint
  `HAMMERTIME_SHARD_IDS` set and the sets' union must be 0-127. A pod takes its
  shards under a lease keyed by `HAMMERTIME_AGGREGATOR_MEMBER_ID` (default the
  pod's stable name) plus a token generated per process (decision 7, as amended
  by Amendment 6). A clean restart releases its leases and reacquires them at
  once; after an unclean death the replacement Pod waits in process for the old
  leases to lapse — at most `HAMMERTIME_AGGREGATOR_LEASE_TTL_S` — and then
  claims them. A second owner is refused, a Pod bearing the same name included:
  that case means the at-most-one guarantee the StatefulSet is built on was
  broken (a force-deleted Pod still running), and it now fails to start rather
  than silently sharing the shards. Re-sharding is a change to those sets.
* `trie` is a StatefulSet with a single replica per address family and a
  PersistentVolume for snapshots (§28: single-writer ownership, §33: snapshots).
* `detector` is a Deployment; it is stateless apart from classification history.
* PodDisruptionBudget on `trie` set to zero voluntary disruptions during a
  snapshot write.
