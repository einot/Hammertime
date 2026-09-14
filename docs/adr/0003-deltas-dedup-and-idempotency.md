# ADR 0003 — Time-bucketed deltas with per-agent sequence dedup

Status: accepted

## Context

§4 requires the protocol to state whether counts are deltas or absolute values,
and §23 requires message identity so retries do not double-count.

## Decision

Agents send **time-bucketed deltas** keyed by `(agent_id, sequence)`. Ingest
maintains a dedup window (`agent_id` → seen sequence set / high-water mark) with
a retention of `allowed_lateness + window_seconds`, and rejects duplicates before
publishing.

## Consequences

* A retry is free; the agent may resend indefinitely within the dedup window.
* Ingest is the only deduplication point; aggregator consumers are therefore
  at-least-once-safe only for idempotent operations and must not re-apply
  counters on redelivery — the consumer tracks committed offsets per shard.
* `duplicate_messages` is a first-class metric (§37).
