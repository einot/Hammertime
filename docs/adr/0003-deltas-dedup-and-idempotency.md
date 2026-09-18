# ADR 0003 — Time-bucketed deltas with per-agent sequence dedup

Status: accepted; amended (event_id for internally-produced events; Amendment 2,
2026-09-18, on how the aggregator satisfies the at-least-once consequence)

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
  (How the shipped aggregator satisfies this — without an `event_id` filter,
  and with a precise meaning for "committed offsets per shard" — is ruled in
  Amendment 2 below.)
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

## Amendment 2 (2026-09-18) — how the aggregator is at-least-once-safe: redelivery lands in fresh counters, not in a dedup filter

Why: milestone M4 (epic #7) requires the shipped aggregator to match the
consistency guarantees of this ADR, and its Consequences bullet — "aggregator
consumers are therefore at-least-once-safe only for idempotent operations and
must not re-apply counters on redelivery — the consumer tracks committed
offsets per shard" — reads, taken literally, as two requirements the shipped
design (ADR-0011 decision 3, decision 5, decision 6 and Amendment 6 item A20)
does not meet in those words: the aggregator keeps no `event_id` filter and
*does* apply the counters of a redelivered observation, and what it commits
is not "the offset it consumed to" but a handled position. The two texts
were in tension; this amendment rules on it rather than leaving both
standing. The original wording is quoted above and is left in place with a
pointer to this section.

**Ruling.** The *requirement* behind the sentence stands unchanged: a
`RequestObservation` delivered more than once to the aggregator MUST NOT be
counted more than once in any `ShardWindow`, and a `HotIpAdded`/`HotIpRemoved`
MUST NOT be emitted twice for one transition. The *mechanism* the sentence
implied — that the consumer recognises a redelivery and declines to apply its
counters — is not how it is met, and is not required. Under ADR-0011 the
aggregator meets it structurally:

1. **Every redelivery arrives at a `ShardWindow` that has never counted the
   message.** Window counters are process-local and per claim (ADR-0011
   decision 2); a claim always constructs a new `ShardWindow` (decision 5).
   The bus redelivers only (a) after a crash, when the counters that held the
   first copy died with the process, or (b) after a claim changed hands —
   including back to the same member — when the first copy was fetched under
   the old claim and is `UNCLAIMED` by claim identity rather than applied to
   the new window (A20). Within one live claim the bus never hands the same
   offset out twice. So the aggregator applies the counters of every message
   it handles, redelivered or not, and that is the correct behaviour: it is
   the first application in that window. "Must not re-apply counters" is
   satisfied by never having the second copy and the first copy's counters in
   the same window, not by refusing the second copy.
2. **The only state that outlives a window is the durable per-shard HOT set,
   and it is idempotent** (ADR-0011 decision 5, Amendment 1 item A2): an IP
   already recorded HOT is not re-announced; a replayed
   `record_transition` leaves the store as one call would have; the
   persisted `next_sequence` never moves backwards, so a redelivery can never
   reproduce an earlier `event_id`.
3. **"The consumer tracks committed offsets per shard" means the handled
   position.** What the aggregator commits, per `(topic, partition)`, is one
   past the last message `handle()` finished under the current claim —
   `ShardClaims.commit_handled`, the only commit path (A20) — never the
   consumed position. This is what makes (1)(b) hold: the message in hand at
   a revocation is left in the log for the next owner instead of being
   committed past.

What this costs, recorded so it is not mistaken for a gap later: after a
crash, up to one commit interval (`HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S`,
default 1 s) of observations is replayed into empty rings, and after a
handover the new window under-counts every IP until `claim + window_seconds`.
Both are a self-heal that completes within one window and are bounded by
ADR-0011 decision 5's warm-up rule (no spurious demotion of an inherited IP
during it); neither is a double count. No `duplicate_messages`-style counter
exists at the aggregator, because no duplicate is ever *detected* there —
there is nothing to count.

Identity of the aggregator's own events — the first amendment above —
matches the shipped code and needs no change: `agent_id` is
`aggregator-shard-{p}` (ADR-0011 decision 4, assumption 11), `sequence` is
the shard's persisted counter, shared by both event types (assumption 14;
this ADR allows independent counters and a shared one is the stricter
choice, so `event_id`'s `(agent_id, sequence, event_type, subject)`
derivation is unaffected), and `subject` is the IP (ADR-0004). Because both
are properties of the *shard*, not of the worker process, an IP's event
identity and sequence are continuous across a handover: the next owner
continues the same `agent_id` from the same `next_sequence`. That continuity
is what ADR-0001 Amendment 1 relies on for the per-IP ordering guarantee it
states for §22.

Assumptions (each a judgment call; push back individually):

* **The original sentence was about double counting, not about the literal
  act of re-applying.** Read the other way it would forbid the recovery path
  ADR-0011 chose (rebuilding counters from the log after a crash), which no
  spec section asks for; §23's concern is `count += N` happening twice.
* **A byte-identical message handed to `handle()` twice within one live
  claim is not a supported input.** The bus never produces it (a live
  assignment hands out each offset once), so the aggregator does not guard
  against it, and no test should pin what happens if a caller does it
  directly — that would turn an accident of the implementation into a
  contract.
* **Fencing a zombie owner is out of scope.** If a member that has lost a
  partition keeps handling messages it had already fetched while the new
  owner also handles them, both windows count and both may emit; ADR-0011
  Amendment 1 item A2 records that the store does not fence this and that
  keeping a shard single-owner is the consumer group protocol's job. This
  amendment does not add a fencing token.
* **Scope is the aggregator.** The trie's at-least-once handling of
  `hammertime.hot-ip.v1` (ADR-0010 decision 3: flush, then commit) and the
  reconciliation topic's consumer (ADR-0011 decision 3: same `event_id`, so
  it *can* dedupe against the hot path) are unchanged; the reconciliation
  consumer is the one place an `event_id` filter is still the intended
  mechanism, because one observation can be diverted on one delivery and
  applied on a redelivery (a `future` observation that is no longer in the
  future when it comes round again), so that consumer may hold a copy of an
  observation the hot path did count.
* **No CHANGES entry.** This amendment describes shipped behaviour; it
  changes no wire format, schema, config key or default.
