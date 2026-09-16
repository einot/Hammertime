# ADR 0008 — Rate limiting charges for observations, not just for requests

Status: accepted

Scope note: this ADR covers the amplification between the throttled unit and
the consumed resource on the *authenticated* ingest path (issue #41). Throttling
of *failed authentication* is ADR-0007. The shared response shape for any
throttled request (status, `Retry-After`, `X-RateLimit-Scope`) is specified once
in Section 36.7.

## Context

`api/routes.py` charges the per-agent limiter a flat `cost=1.0` per HTTP
request. One request may carry up to `max_observations` (default 10 000)
entries, and ADR-0004 fans each distinct IP out into its own bus publish. So one
token buys, in the worst case:

```text
1 rate-limit token  ->  10 000 broker publishes
                    ->  10 000 per-IP counters in the aggregator (Section 5)
                    ->  10 000 candidate insertions into the trie (Sections 10, 26)
```

At the default `HAMMERTIME_INGEST_RATE_LIMIT_RPS=50`, a single authenticated
agent's *permitted* traffic is up to 500 000 per-IP publishes per second. The
limiter is nominally doing its job the entire time; the unit it counts simply
has no relationship to the resource it protects.

`request_count` has `"minimum": 0` in `schemas/observation.v1.json`, so the
entries need not even carry activity. `max_observations` zero-count entries,
naming freshly generated addresses every request, is an attacker-controlled
*cardinality* channel: nothing about it is malformed, nothing is rejected, and
every entry still manufactures per-IP state downstream. Section 26's memory
budget is set by the number of tracked IPs, and this hands that dial to the
agent.

Why a mechanical fix does not exist:
`RateLimiter.check(key, limit_rps, *, cost=1.0)` conflates *rate* with
*capacity* — the bucket holds exactly `limit_rps` tokens — and deliberately
raises `ValueError` (not `RateLimitExceeded`) when `cost > limit_rps`, on the
sound reasoning that such a request could never succeed at any point in time and
silently exhausting the bucket forever would be worse. Charging
`cost=len(observations)` against `limit_rps=50` therefore turns any batch of 51
entries or more into an unhandled `ValueError` — a 500, on a request that is
entirely legal per the published schema. The design question is not "where do we
multiply", it is "what is the capacity, who says a batch is too big, and what
does the caller get told".

## Decision

### 1. Separate a bucket's capacity from its refill rate

`RateLimiter.check` / `allow` gain a keyword-only `capacity: float | None =
None`. When omitted, capacity is `limit_rps` — today's behaviour, unchanged to
the bit, including the existing `ValueError` guards and the "a first-seen key
starts full" property (it starts at `capacity`).

This is the piece that makes a cost-scaled charge expressible at all. A
meaningful sustained entry rate (thousands per second) and the ability to accept
one maximum-size batch from an idle agent are two different numbers, and a
limiter that forces them to be the same number can only be configured wrong in
one direction or the other.

`cost > capacity` remains a `ValueError`. It stays a programming error, and
decision 4 makes it unreachable from the route.

### 2. A second, per-agent budget denominated in observations

A separate `RateLimiter` instance, keyed by authenticated `agent_id`:

```text
refill rate  HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS   default 2000  entries/second
capacity     HAMMERTIME_INGEST_OBSERVATION_BURST            default 10000 entries
```

Two budgets rather than one scaled charge against the existing bucket, because
the two limits answer different questions and a single bucket cannot express
both: `rate_limit_rps` bounds *how often* an agent may call (protecting the HTTP
path, dedup store round-trips, and per-request overhead), while the observation
budget bounds *how much work* it may ask for (protecting the broker, the
aggregator's per-IP state, and the trie). Collapsing them forces a deployment to
choose between "50 requests/s of any size" and "50 entries/s", neither of which
is the intended policy.

Defaults: 2000 entries/s sustained still allows 120 000 entries per minute for
an agent whose windows are minutes long, which is far above any legitimate edge
agent's output, while cutting the worst case from 500 000 to 2000 per-IP
publishes per second — a 250x reduction that no correctly behaving agent can
perceive. A 10 000-entry burst equals `max_observations`, so a full-size batch
from an idle agent is always accepted, and after one the agent waits five
seconds for another.

### 3. The charge is the number of distinct IPs, counted before zero-drop

Cost is `len(publisher.coalesce(observation))` — distinct `Address` values after
ADR-0004's coalescing, which is exactly the number of bus messages the request
will produce and the number of per-IP states it will touch. Not
`len(payload.observations)`: a batch that names one IP 10 000 times produces one
message, and charging it 10 000 would punish a shape ADR-0004 explicitly
declared lossless and legal.

The count is taken *before* decision 5's zero-count drop. An attacker sending
10 000 zero-count entries pays for 10 000, because it asked the service to
consider 10 000 distinct addresses; making the cardinality channel free by
dropping first would reinstate the exact hole this ADR closes.

A batch found to be a duplicate at the dedup claim, and a batch whose publish
then fails (`503`), are both charged too. Charging on request, not on success,
is the only variant that cannot be gamed.

### 4. An unsatisfiable batch is impossible by configuration, and a 413 if it happens anyway

`HAMMERTIME_INGEST_OBSERVATION_BURST` MUST be `>=
HAMMERTIME_INGEST_MAX_OBSERVATIONS`; `load_settings` rejects a configuration
that violates this at startup, with the same `ValueError` treatment every other
malformed ingest setting gets. Since a schema-valid batch has at most
`max_observations` entries and therefore at most `max_observations` distinct
IPs, its cost can never exceed capacity. The `ValueError` path is closed at the
only place it could have been opened: configuration.

Defence in depth, because "unreachable" and "unreachable in every future edit of
this file" are different claims: the route compares the cost against the
configured capacity itself and, if the cost is greater, answers **413** with a
detail naming both the cost and the ceiling — never calling `check()` with an
over-capacity cost, and never surfacing a `ValueError` as a 500.

413, not 429, for that case: it is a statement about the batch, not about
pacing. No `Retry-After` value would be truthful, because no amount of waiting
makes the request succeed; the client must send a smaller batch. That matches
how this API already answers `max_body_bytes` and `max_observations`. An agent
that is merely *going too fast* gets 429 with a real `Retry-After`
(Section 36.7).

### 5. Zero-count entries are never published

After the charge, entries whose coalesced `request_count` total is 0 are dropped
and not published. A zero delta is definitionally a no-op for the sliding window
(Section 5), so publishing one buys nothing but the per-IP state its arrival
creates — which is the entire payload of the cardinality attack.

If every entry drops out, the request is still `202` and the `(agent_id,
sequence)` claim is still taken: the batch was schema-valid and its effect
(nothing) has been applied in full. Rejecting it would invent a new failure mode
for a legal payload and break "a message is accepted or rejected as a whole",
and skipping the claim would make the sequence re-consumable.

The schema keeps `"minimum": 0`. Tightening it to 1 would break agents that
legitimately emit zeroes today for a gain this decision already delivers.

### 6. Placement: after validation, before the dedup claim

```text
auth (ADR-0007)
  -> request-rate check, flat cost 1.0        <- unchanged, still pre-body-read
  -> body read within max_body_bytes
  -> JSON / schema / domain validation
  -> coalesce (400 on request_count overflow)
  -> observation-budget check, cost = distinct IPs   <- new
  -> dedup claim
  -> drop zero-count entries, publish, 202
```

The flat charge stays exactly where it is and keeps its cost of 1.0. It is the
only throttle that runs before the body is read, so it must remain cheap and
header-only; making it depend on the batch size would require reading the body
to decide whether to read the body.

The new charge sits after coalescing (the cost is not knowable before) and
before the claim (so a throttled request does not consume its `(agent_id,
sequence)` claim, and the agent's retry after `Retry-After` is accepted rather
than answered "duplicate" — the same reasoning ADR-0004's amendment applies to
the overflow 400).

## Consequences

* **Wire-visible:** a new `429` reason (`X-RateLimit-Scope: observations`, with
  `Retry-After`) and a new `413` for a batch larger than the configured
  observation capacity. `CHANGES` gets one line:
  `Charge the ingest rate limiter per distinct observed IP as well as per request; over-budget batches get 429 with Retry-After`
  and one for the fan-out change:
  `Drop zero-count observation entries before fan-out; they are no longer published`
  Neither is `BREAKING`: no config key is removed or renamed, and the default
  budget is above any legitimate agent's output.
* **Two new config keys**, both optional with defaults, plus one new startup
  validation (`OBSERVATION_BURST >= MAX_OBSERVATIONS`) that can stop a
  deployment with a deliberately odd configuration from starting. That is the
  intended trade: a loud startup failure instead of a 500 on a legal request.
* **`RateLimiter` grows one keyword argument.** Existing callers and existing
  behaviour are untouched; ADR-0007's renames (`key`, `max_keys`) land in the
  same module at the same time.
* **Per-agent overrides remain request-scoped.** `AgentRecord.rate_limit_rps`
  still overrides only the request budget; the observation budget is global. An
  agent granted 200 rps is still held to 2000 entries/s overall. Adding
  `observation_rate_limit_eps` to the registry record (and to
  `schemas/agent_registry.v2.json`, ADR-0006) is the obvious follow-up and is
  deliberately not bundled into a security fix.
* **Budgets are per ingest process**, like every other bucket here; N replicas
  multiply the effective ceiling by N, and a shared/coordinated limiter is out
  of scope (`ratelimit/__init__.py` already documents this for the request
  budget).
* **The aggregator will never see a zero-delta observation.** Any future
  behaviour that wanted one — a keepalive that refreshes an IP's expiry without
  changing its count — must be an explicit signal, not an incidental
  side effect of a zero in `observations[]`. Section 36.6 states this so the
  aggregator is written against it rather than discovering it.
* **A duplicate retry costs the agent its entry budget.** An agent that retries
  a 10 000-entry batch it already delivered pays 10 000 entries for a `200`.
  This is correct — the work was requested and done — but it means an agent with
  a broken retry loop hits the observation budget before it hits the request
  budget, which is exactly the signal `rate_limited_observations` (Section 37)
  exists to surface.

## Implementation notes

Normative detail (cost formula, statuses, pipeline order, config keys and
defaults) is Section 36.6; this is only the mapping onto existing code.

* `RateLimiter.check` / `RateLimiter.allow` become
  `(self, key, limit_rps, *, cost=1.0, capacity=None)`. `capacity is None` means
  `capacity = limit_rps`. Validation: `capacity` positive and finite;
  `cost > capacity` raises `ValueError` (message updated to say "capacity",
  since it is no longer necessarily `limit_rps`); a first-seen key starts at
  `capacity`; refill clamps to `min(capacity, tokens + elapsed * limit_rps)`;
  `retry_after = (cost - tokens) / limit_rps` is unchanged.
* `IngestSettings` gains `observation_rate_limit_eps: float` (default 2000, via
  `_parse_positive_number`) and `observation_burst: int` (default 10000, via
  `_parse_positive_int`). `load_settings` then raises `ValueError` if
  `observation_burst < max_observations`, naming both keys and both values.
* `IngestState` gains `observation_limiter: RateLimiter`, a second instance
  keyed by `agent_id` (a separate instance, not a key prefix on the existing
  one, so LRU accounting and eviction stay independent).
* `routes.py`, between `state.publisher.coalesce(...)` and `state.dedup.claim`:
  `cost = len(coalesced)`; if `cost > settings.observation_burst` raise
  `HTTPException(413, ...)`; else
  `check(agent_id, settings.observation_rate_limit_eps, cost=cost,
  capacity=settings.observation_burst)`, mapping `RateLimitExceeded` to the 429
  of Section 36.7. `coalesce()` is already called there for its overflow check,
  so no extra pass over the batch is needed — reuse its return value.
* The existing flat `check(agent_id, effective_limit_rps)` call is untouched
  except for gaining `Retry-After` and `X-RateLimit-Scope: requests` on its 429.
* Zero-drop belongs in `publisher.py`'s `publish`, after coalescing: publish only
  entries with `request_count > 0`, and `flush()` exactly once even when the
  filtered list is empty. `coalesce()` itself keeps returning zero entries — it
  is what `routes.py` charges against.
* `CHANGES` needs the two lines quoted above, and `.env.example` the two keys.
