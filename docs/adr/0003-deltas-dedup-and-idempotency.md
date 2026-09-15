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

## Amendment: event_id for internally-produced events

The `(agent_id, sequence)` identity above is defined for agent-originated
`RequestObservation` messages. The internal event types emitted downstream
(`HotIpAdded`, `HotIpRemoved`, `PrefixStatsChanged`, §19) are not produced by
an end-user agent, but the generic `EventEnvelope` (§23, core/events/envelope.py)
reuses the same `agent_id` + `sequence` fields for all four payload types so
that one envelope/codec implementation covers every topic in §32/§33.

For these internal events, `agent_id` on the envelope MUST be populated with
the identifier of the producing shard/service instance (e.g. the aggregator
shard id that owns the IP, or `trie-primary` for the single-writer trie
service — ADR-0001), and `sequence` is that producer's own monotonic event
sequence, not the originating agent's.

Because `sequence` numbering is independent per producer *and* per event
type (an aggregator shard's `HotIpAdded` sequence and a trie service's
`PrefixStatsChanged` sequence are unrelated counters), `event_id` MUST be
derived deterministically from `(agent_id, sequence, event_type)` — not from
`(agent_id, sequence)` alone — so that redelivery of two different event
types that happen to share an `(agent_id, sequence)` pair cannot collide.
`event_type` here is the discriminator already carried by the codec's
schema-version-tagged wire format (core/events/codec.py).
