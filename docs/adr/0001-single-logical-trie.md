# ADR 0001 — One logical trie owner, four deployable services

Status: accepted

## Context

§21 offers a global trie (Option A) or per-shard tries with query-time
aggregation (Option B). §43 recommends starting with Option A. Separately, §19
describes an event-driven pipeline whose stages have very different scaling
characteristics: observation ingestion is high volume, state transitions are
comparatively rare.

## Decision

Deploy four services from day one — ingest, aggregator, trie, detector — but keep
**one logical trie owner** (a single-writer process per address family). Ingest
and aggregator scale horizontally; the trie service does not, yet.

The service split is about isolating failure domains, deploy cadence, and
resource profiles. It is not a claim that the trie is distributed.

## Consequences

* Prefix counts stay exact; no cross-shard aggregation logic in v1.
* The trie service is a known scaling ceiling. `trie_updates` and
  `hot_transition_to_prefix_update_latency` are the metrics that will tell us
  when Option B is required (§21, §43).
* Prefix classifications are eventually consistent with respect to IP state; all
  classification responses carry `as_of` and `event_sequence` (§22).
* Moving to Option B later changes the trie service's internals and the query
  fan-out — not the event contracts.
