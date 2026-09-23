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

* **Where the replay starts.** The snapshot's `event_sequence` is the offset
  of the next record to read, `replay_position + 1` (ADR-0017). A trie with
  no snapshot — every trie until snapshots ship — replays from the oldest
  record the log still retains.
* **Its deadline.** The replay has to finish inside
  `HAMMERTIME_STARTUP_TIMEOUT_S`. A trie that keeps failing its start with
  `start_failed reason=startup_timeout` needs a longer deadline, or a newer
  snapshot.
* **A corrupt trie.** A trie that finds its own structure corrupt logs
  `trie_invariant_violation` and exits 1. The restart is the rebuild: do
  not patch the state by hand.

### Aggregator will not start: a shard is leased elsewhere
Every shard an aggregator is configured for is taken under a lease in the
state store (ADR-0013 decision 7, as amended by Amendment 6); the lease
value is `<HAMMERTIME_AGGREGATOR_MEMBER_ID>/<instance token>`, the token
being generated per process and logged as `instance_id` in the `starting`
record. Read the refusal record to know which case you are in.

* `shard_owned_elsewhere` (ERROR) then `start_failed`, exit 1 at once: the
  holder's member id is a *different* member. Two members' `HAMMERTIME_SHARD_IDS`
  overlap — fix the sets (they must be pairwise disjoint, union 0-127). A
  replacement that lost its name (the hostname default under a recreated
  container) lands here too; it clears itself within
  `HAMMERTIME_AGGREGATOR_LEASE_TTL_S` of the old process's death, or at
  once if the old process is stopped cleanly.
* `shard_held_by_same_member` (WARNING) and `dependency_unavailable
  dependency=shard_leases`, repeating, `/readyz` 503: the holder has this
  process's member id and another instance token. Either the previous
  process died without releasing — wait; the leases lapse within
  `HAMMERTIME_AGGREGATOR_LEASE_TTL_S` (30 s by default) and the new one
  claims them and reports `ready` — or a second process is running under
  the same member id, in which case the wait ends at
  `HAMMERTIME_STARTUP_TIMEOUT_S` with `start_failed` and exit 1: find the
  other process (its `starting` record carries the `instance_id` named as
  `owner`) and stop one of them. Under the reference compose file this
  cannot be produced by `--scale` (the service carries a `container_name`,
  which Compose refuses to scale); under Kubernetes it means the
  StatefulSet's at-most-one guarantee was broken, usually by a
  force-deleted Pod still running on a partitioned node.
* `shard_lease_lost` (ERROR) then `run_exited`, exit 1 on a running
  member: its lease lapsed — a stall longer than the TTL, typically a
  configuration re-evaluation over a very large `HAMMERTIME_AGGREGATOR_MAX_TRACKED_IPS`
  — and another process took the shard. Raise the TTL with the cap; the
  orchestrator's restart reclaims the shard when the new holder stops or
  if it was a stale twin.

Do not delete `hammertime:agg:*:owner` keys by hand to hurry a start:
the holder may be alive. If a process is known dead and the wait is
intolerable, lower `HAMMERTIME_AGGREGATOR_LEASE_TTL_S` for the deployment
instead (it must exceed `HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S` and
the longest stall, and stay below `HAMMERTIME_STARTUP_TIMEOUT_S`).

## Rollback

Detection config is versioned; roll back by publishing the previous version and
running the controlled re-evaluation job. Never edit thresholds in place without
re-evaluation — stale state is worse than a mass transition (§34).
