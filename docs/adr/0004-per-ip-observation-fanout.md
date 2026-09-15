# ADR 0004 — Ingest fans an accepted batch out into one message per IP

Status: accepted

## Context

§20 requires exactly one owner per IP (`hash(IP) → shard`), and the topic
registry (`packages/hammertime-bus/src/hammertime/bus/topics.py`) encodes that
by keying `hammertime.observations.v1` and
`hammertime.observations-reconciliation.v1` with `_ip_key(event) -> str(event.ip)`,
whose parameter type is the single-IP `Observation`, not the multi-IP
`RequestObservation`.

An agent message (`docs/protocol/observation-v1.md`,
`schemas/observation.v1.json`) is a *batch*: one `(agent_id, sequence,
window_start, window_seconds)` header plus 1..10000 `(ip, request_count)`
entries, which may name IPs owned by different shards. A bus message has
exactly one partition key, so a multi-IP batch cannot be published as a single
IP-keyed message. Either ingest splits the batch, or the whole batch is
published under some non-IP key and a downstream stage re-routes each entry to
its owning shard — reintroducing the cross-shard coordination §20 exists to
avoid, and giving the aggregator a stream it cannot partition-own.

Separately, `hammertime.core.events.codec` can only encode the four payload
types in `schemas/`; there is no wire type for a bare `Observation`. And
`compute_event_id` (ADR-0003 amendment) derives identity from `(agent_id,
sequence, event_type)`, which is *identical* for every message split out of the
same batch — a consumer applying the documented rule "already processed this
event_id, discard the redelivery" would silently discard real observations.

## Decision

**1. Ingest splits, and splits at ingest.** After validation and dedup, ingest
publishes one message per distinct IP in the batch. The split happens in
`services/ingest/publisher.py`; no downstream re-routing stage exists.

**2. The payload of each message is a single-entry `RequestObservation`** —
same `agent_id`, `sequence`, `window_start`, `window_seconds` as the accepted
batch, with `observations=(entry,)`. No new payload type and no schema change:
`schemas/observation.v1.json` already allows `minItems: 1`, and the batch
header is what carries the event-time window (ADR-0002) that the aggregator
needs per entry anyway.

**3. Duplicate IPs within one batch are coalesced before splitting.** Entries
are grouped by `Address` (so the two spellings of one IPv6 address are one
group) and their `request_count`s summed, preserving first-appearance order.
Deltas are additive (ADR-0003), so this is lossless, and it is what makes
"one message per (agent_id, sequence, ip)" an invariant rather than a hope.
A coalesced total above `schemas/observation.v1.json`'s `request_count`
maximum is a 400 for the whole request; a batch is never partially applied.

**4. `EventEnvelope` gains an optional `subject`, and it participates in
`event_id`.** `subject` is the thing the event is about — for IP-keyed topics,
exactly the partition key (`str(ip)`); producers on `hammertime.prefix-stats.v1`
MAY set it to the prefix. Identity becomes:

```text
subject is None  ->  sha256(agent_id \x1f sequence \x1f event_type)            # unchanged
subject is set   ->  sha256(agent_id \x1f sequence \x1f event_type \x1f subject)
```

so already-shipped envelope/codec behaviour is bit-for-bit unchanged when
`subject` is absent, and every message split out of one batch gets a distinct,
*deterministic* `event_id`. The codec writes `subject` only when it is set and
treats an absent key as `None`, and continues to re-derive and verify
`event_id` on decode.

Messages published to the two IP-keyed observation topics MUST set `subject`
to the published entry's IP text. The codec does not enforce that (it mirrors
`schemas/observation.v1.json`, which permits 1..10000 entries, and must stay
topic-agnostic); it is a producer invariant that consumers MAY assert.

**5. Publish-then-mark.** Ingest claims `(agent_id, sequence)` as in-flight,
publishes all N messages, flushes, and only then records the sequence as seen.
A crash between publish and mark leaves the sequence unseen, so the agent's
retry republishes the whole batch with byte-identical envelopes and identical
`event_id`s — at-least-once, which ADR-0003 already requires consumers to
tolerate. The reverse order (mark, then publish) would turn any publish failure
into permanently lost counts and a missed HOT transition, which is not
recoverable by any retry the agent is allowed to make.

## Consequences

* One HTTP request can produce up to `max_observations` bus messages.
  `max_observations_per_message` is now a fan-out knob as well as a size limit,
  and ingest must publish concurrently and `flush()` once per batch, not once
  per message.
* `202` means *all* N messages are durably acknowledged. A publish failure is a
  `503` with nothing recorded, not a partial `202`
  (`docs/protocol/observation-v1.md`).
* The aggregator consumes a stream of single-IP observations it fully owns by
  partition; it never sees an entry for an IP it does not own, and never needs
  to forward one (§20).
* `event_id` remains a true identity: unique per message, stable across
  redelivery and across agent retries of the same batch. Consumers may dedupe
  on it without dropping real data.
* Old builds reject envelopes that carry `subject` (their derived `event_id`
  will not match the wire one), so rolling a producer forward ahead of its
  consumers is a breaking deployment step.
* The per-IP split is what makes `_ip_key`'s `Observation` parameter type
  correct; `topics.py`'s `OBSERVATIONS` description ("published per-IP as
  `Observation`") is accurate in intent but imprecise in wording — the unit on
  the wire is a single-entry `RequestObservation`, not a bare `Observation`.

## Amendment (issue #32): claim before publish, not publish then mark

Point 5 above, taken literally ("publishes... and only then records the
sequence as seen"), has a gap security review caught once ingest actually
wired `has_seen`/`mark_seen` against live traffic: `has_seen()` and
`mark_seen()` are two separate calls with no atomicity between them
(`packages/hammertime-store/src/hammertime/store/interface.py`'s
`DedupStore.has_seen` docstring). Two concurrent requests for the same
`(agent_id, sequence)` can both observe "not seen" before either publishes,
so both publish — exactly the double-counted delta section 23's dedup
exists to prevent, and the window is not microseconds: it spans the full
per-IP publish + flush, i.e. up to `max_observations` broker round trips.

Ingest now uses a new `DedupStore.claim(agent_id, sequence, *, ttl_seconds)
-> bool` instead: a single atomic check-and-mark (`SET ... NX` for Redis; a
same-coroutine check-then-mark for the in-process store, safe because
neither `has_seen` nor `mark_seen` yields to the event loop). Exactly one
concurrent caller for a given `(agent_id, sequence)` gets `True` and may
proceed to publish; every other caller gets `False` and is rejected as a
duplicate. This is now called *before* publishing, with the batch's full
`allowed_lateness_seconds + window_seconds` TTL — not the short-lived,
distinct "in-flight" marker point 5's wording implies.

The tradeoff this reintroduces, which point 5 originally warned against: a
publish failure now leaves the sequence claimed rather than immediately
retryable. This is a bounded delay, not the "permanently lost counts...
not recoverable by any retry" point 5 describes — `mark_seen`'s contract is
"seen for at least `ttl_seconds`", not forever, so a retry of that exact
sequence is accepted again once the claim's TTL lapses. Closing the
concurrent-duplicate race (a correctness bug reachable by any authenticated
agent simply retrying quickly, which manufactures false `COLD -> HOT`
transitions) was judged worse than this bounded retry delay on the rarer
path (a genuine bus/transport failure). A coalesced batch that fails
`coalesce()`'s overflow check (point 3) is validated *before* the claim, so
a 400 never consumes it — only an accepted, claimed batch that then fails
to publish hits this tradeoff.

A future refinement that gives `claim` its own short-lived marker distinct
from the final long-retention "seen" state (closer to point 5's original
"in-flight" wording) would let a publish failure self-heal immediately
instead of waiting out the full TTL, without reopening the race. Deferred:
`MemoryDedupStore`'s `SequenceWindow` has no clean way to "unmark" a single
sequence once folded into its high-water mark, so building this properly
needs its own design pass, not a quick patch alongside #32's other fixes.
