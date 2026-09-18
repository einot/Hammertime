# ADR 0002 — Event-time windows with bounded lateness

Status: accepted; amended 2026-09-18 (Amendment 1: what "replay is
deterministic" means now that the aggregator classifies against a live
clock; the diverted set is wider than "late"; two consequences still open)

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
* Replay is deterministic: the same log produces the same windows. (Scoped
  by Amendment 1 below: the same log *and the same clock readings*; a
  wall-clock re-read of the observation log is not an aggregator recovery
  path.)

## Amendment 1 (2026-09-18) — replay determinism scoped to (log, clock); the diverted set; two consequences still open

Why: milestone M4 (epic #7) requires the shipped aggregator to be validated
against this ADR. Three of its five statements match ADR-0011 as built and
are confirmed below; one consequence is true only under a reading the ADR
did not spell out and is scoped here; two consequences are not yet
implemented and are recorded as open rather than amended away. Nothing here
changes shipped behaviour.

**Confirmed, no change.**

* The decision itself — event time from `window_start`, `bucket_start =
  floor(t / bucket_seconds) * bucket_seconds` in UTC — is
  `hammertime.core.time.buckets.bucket_start` and is what the aggregator
  floors with (ADR-0011 decision 3 step 3; Amendment 2 item A9).
* "`allowed_lateness` (default 30s) bounds how far back an observation may
  land" is `allowed_lateness_seconds` in the detection config, and the
  horizon is `now - window_start > window_seconds + allowed_lateness_seconds`
  with the configured window (ADR-0011 context and decision 3). Where this
  ADR says "later observations are counted in `late_messages`, rejected from
  the hot path, and written to a reconciliation topic", ADR-0011 decision 3
  does exactly that and widens the diverted set: `future`, `expired_bucket`
  (inside the horizon but the bucket has left the window) and
  `window_too_long` are diverted the same way, byte-for-byte, with
  `late_messages{reason=late|future|expired_bucket}` and
  `observations_rejected{reason=window_too_long}`. A superset of what this
  ADR asked for, not a departure.
* "The sliding counter must accept writes into any live bucket, not just the
  newest" is `IpCounter.observe` (ADR-0011 decision 2).

**Scoped: "Replay is deterministic: the same log produces the same windows."**
Under ADR-0011 decision 3 the classifier's `now` is the service clock at
processing time — "a lagging aggregator diverts what it can no longer count
rather than counting it into the past". So the sentence holds in this form:

* Bucket assignment is deterministic: every agent, shard and replica maps an
  observation to the same logical bucket (the decision above, unchanged).
* The window state is a deterministic function of the observation log *and
  the sequence of clock readings*. Given both — a `ManualClock`, as every
  hermetic test uses — a replay reproduces the windows exactly.
* Re-consuming the observation log at a later wall time is **not** an
  aggregator recovery path: every observation older than the horizon is
  diverted to reconciliation, by design. The aggregator recovers from a
  restart or handover by inheriting the durable per-shard HOT set and
  warming up for one window (ADR-0011 decision 5), not by replaying
  observations. The deterministic, clock-independent replay that §32/§33
  rely on operationally is the *trie's* replay of `hammertime.hot-ip.v1`
  (ADR-0010 decision 3): transitions carry their own `sequence` and are
  applied without reference to the service clock.

**Still open — recorded, not ruled.**

1. "Ingest records `agent_clock_skew_seconds` per agent." Nothing in the
   repository records it; the identifier appears only in this ADR (repository
   grep, 2026-09-18). It is a §37 telemetry item for ingest and belongs to
   the telemetry epic, not to M4. Left as an unimplemented consequence of
   this ADR so that it is not lost.
2. Making `allowed_lateness_seconds` relative to the agent's window end
   rather than its `window_start`. ADR-0011 assumption 8 deferred this
   deliberately and it stays deferred: with a ring of exactly one window the
   parameter admits nothing the hot path can still use, its remaining
   effects are the dedup TTL (ADR-0003) and where `late_messages{reason=late}`
   begins, and redefining it changes ingest's TTL arithmetic — a behaviour
   change that needs its own pass and a `CHANGES` entry when it is made.

Assumptions (judgment calls, push back individually):

* **"Replay" in the original consequence meant re-consuming the observation
  log through the aggregator.** The trie's replay was not designed when this
  ADR was written; reading the sentence as being about the trie would make
  it trivially true and leave the aggregator's actual behaviour unstated.
* **The horizon keeps the configured window, not the agent's.** This is
  ADR-0011's choice (its context, "the policy boundary this ADR keeps"),
  restated rather than re-decided; item 2 above is where it would change.
* **No CHANGES entry.** Nothing here alters a wire format, schema, config
  key, default or observable behaviour.
