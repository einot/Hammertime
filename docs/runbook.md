# Runbook

## Dashboards

`deploy/grafana/hammertime.json` — ingestion, window, trie, and detection rows (§37).

## Common situations

### Sudden spike in `cold_to_hot_transitions`
Check whether `hot_threshold` changed (config version in the metric labels) and
whether a re-evaluation job is running (§34). A config rollout mass-transitions
IPs by design; it should appear as a step, not a ramp.

### `hot_ip_count` drifts from the sum of shard states
Invariant breach (§12). Dump the trie with `hammertime-trie-inspect --verify`,
which recomputes `hot_count` bottom-up and reports the first divergent node.
Recovery is a snapshot reload plus replay (§33), not a manual patch.

### `observation_to_hot_transition_latency` climbing
Aggregator lag. Check `num_pending` on each shard's durable consumer (one per
`(group, partition)`, ADR-0013 decision 5) before re-sharding; a single hot
shard usually means a hash-skewed prefix, not global overload. Shard assignment
is static (`HAMMERTIME_SHARD_IDS`), so relief is a change to the members' sets,
not another replica. A shard no member's set covers shows as a durable, or a
subject, whose backlog only grows.

### Trie service restart
Loads the newest snapshot, then replays `hammertime.hot-ip.v1` from the
snapshot's `event_sequence` (§33). Time-to-ready is reported as
`trie_recovery_seconds`.

## Rollback

Detection config is versioned; roll back by publishing the previous version and
running the controlled re-evaluation job. Never edit thresholds in place without
re-evaluation — stale state is worse than a mass transition (§34).
