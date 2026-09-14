# Kubernetes

* `ingest` and `aggregator` are Deployments with an HPA; the aggregator scales on
  consumer-group lag, not CPU.
* `trie` is a StatefulSet with a single replica per address family and a
  PersistentVolume for snapshots (§28: single-writer ownership, §33: snapshots).
* `detector` is a Deployment; it is stateless apart from classification history.
* PodDisruptionBudget on `trie` set to zero voluntary disruptions during a
  snapshot write.
