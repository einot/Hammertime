# Kubernetes

* `ingest` is a Deployment with an HPA.
* `aggregator` is a StatefulSet, not autoscaled: shard assignment is static
  (ADR-0013 decision 6), so each pod carries its own disjoint
  `HAMMERTIME_SHARD_IDS` set and the sets' union must be 0-127. A pod takes its
  shards under a lease keyed by `HAMMERTIME_AGGREGATOR_MEMBER_ID` (default the
  pod's stable name, decision 7), so a restart reacquires them at once and a
  second owner is refused. Re-sharding is a change to those sets.
* `trie` is a StatefulSet with a single replica per address family and a
  PersistentVolume for snapshots (§28: single-writer ownership, §33: snapshots).
* `detector` is a Deployment; it is stateless apart from classification history.
* PodDisruptionBudget on `trie` set to zero voluntary disruptions during a
  snapshot write.
