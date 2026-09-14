# ADR 0002 — Event-time windows with bounded lateness

Status: accepted

## Context

Agents are distributed and retry; observations arrive out of order (§24). The
window can be driven by event time or server arrival time.

## Decision

Event time, derived from `window_start` on the agent message. Buckets are
deterministic: `bucket_start = floor(event_timestamp / bucket_seconds) * bucket_seconds`,
UTC (§25), so every agent maps an observation to the same logical bucket.

`allowed_lateness` (default 30s) bounds how far back an observation may land.
Later observations are counted in `late_messages`, rejected from the hot path,
and written to a reconciliation topic rather than silently dropped.

## Consequences

* Agent clock skew becomes an operational concern; ingest records
  `agent_clock_skew_seconds` per agent.
* The sliding counter must accept writes into any live bucket, not just the
  newest, so the running total is maintained on arbitrary-bucket updates (§5).
* Replay is deterministic: the same log produces the same windows.
