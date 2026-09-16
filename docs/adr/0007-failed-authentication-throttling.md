# ADR 0007 — Failed authentication is throttled, logged, and answered uniformly

Status: accepted

Scope note: this ADR covers *credential guessing* against the ingest API
(issue #40). The sibling problem — one authenticated request buying up to
`max_observations` bus publishes for a single rate-limit token — is
ADR-0008. They are deliberately separate documents: different threat,
different keying, different configuration, and a reader chasing one will
almost never need the other. What they *do* share is the shape of a
throttled response (status, `Retry-After`, scope header), which is
specified once in Section 36.7 rather than in either ADR.

## Context

`api/routes.py` declares `agent_id: str = Depends(_authenticate)`, so
`auth/middleware.py`'s `require_agent` closure rejects a bad credential with
`HTTPException(401)` / `HTTPException(403)` before the handler body runs. The
only throttle in the service — `state.rate_limiter.check(agent_id, ...)` — is
inside that handler body and is keyed on the *authenticated* `agent_id`. A
request that never authenticates therefore never touches a limiter of any kind:

* there is no lockout, no backoff, and no cooldown on wrong credentials;
* there is no log line anywhere in `services/ingest/` for a failed attempt, so a
  sustained guessing campaign is invisible in the service's own telemetry;
* `middleware.py` maps "unknown `agent_id`" to 401 and "known `agent_id`, wrong
  or disabled credential" to 403, which is an oracle: an attacker enumerates the
  agent roster for free, then spends its whole guess budget on identities it
  knows exist.

Two facts constrain the fix.

**ADR-0006 removed the server's ability to judge token strength.** The registry
holds `HMAC-SHA-256(key, token)` digests; ingest cannot tell a 256-bit CSPRNG
token from `hunter2`, which is exactly why `hammertime-agent-token hash` carries
the `--allow-weak` escape hatch at *provisioning* time. Issue #40 asks whether
`auth/agents.py` should re-add a minimum length/entropy check at load time. It
cannot: there is no plaintext at load time, by construction. That makes a
runtime guess-rate bound the only remaining defence for a deployment that
provisioned a weak token, which raises this from "nice hardening" to "the
compensating control ADR-0006 assumed existed".

**Ingest is not the volumetric-DoS layer.** An in-process token bucket cannot
protect against a flood that saturates the accept queue; a reverse proxy or
network edge does that. What ingest *can* bound is how fast a credential can be
guessed and how much information each failure discloses. Confusing the two
leads to designs that lock out legitimate agents in the name of stopping a flood
they were never going to stop.

The collateral-damage trap is the one worth naming explicitly: the obvious
design — "block this source IP once it has failed N times" — checked *before*
authentication, means a single misbehaving process behind a shared NAT or an
ingress egress IP can deny service to every legitimate agent that shares that
address. In this deployment shape (agents behind NAT, ingest behind an ingress),
that is the likely outcome, not the edge case.

## Decision

### 1. Only failures are throttled; a correct credential is never rejected

Authentication is evaluated first. A request that authenticates successfully is
never rejected by this mechanism, never consumes failure budget, and — equally
deliberately — never *resets* it (an attacker who holds one valid credential
must not be able to clear its own penalty by alternating a good request in).

A request that fails authentication charges the budget, and a failure that
arrives with the budget already exhausted is answered `429` instead of
`401`/`403`.

This inverts the usual objection to lockouts. Because only failures are
throttled, exhausting any budget cannot deny service to anyone holding a valid
token — so the per-`agent_id` dimension below is not a weapon against that
agent, and a noisy neighbour on a shared source address cannot lock out its
co-tenants. The residual cost of a guess from a blocked source is a header parse
and one HMAC (sub-microsecond, per ADR-0006), which is far below the cost of the
TLS handshake that carried it. Bounding *that* is the edge's job, and
Section 36.5 says so.

### 2. Two budgets: source address and attempted `agent_id`

Each failed attempt charges one token to each of two token buckets, in this
order:

```text
source bucket   key = client address, IPv4 /32 or IPv6 /64
agent bucket    key = the attempted X-Agent-Id, or "-" if absent/malformed
```

> **Corrected by Decision 8.** Keying the agent bucket on the raw attempted
> `X-Agent-Id` gives the limiter an attacker-chosen key space; the key is now a
> fixed-size salted slot derived from that value. The budget this decision
> describes — one per attempted identity, source bucket charged first — is
> unchanged.

The source bucket bounds one host grinding many identities. The agent bucket
bounds a botnet grinding one identity from thousands of hosts — the case the
source bucket structurally cannot see, and the case a roster leak (the registry
document is not a secret post-ADR-0006, only its hashes' key is) makes
attractive.

IPv6 is keyed by `/64`, not by address: a single allocation routinely carries
2^64 addresses, so per-address keying is free evasion. IPv4 is keyed exactly.

The source bucket is charged first; if it rejects, the agent bucket is left
alone and the request is answered immediately. This keeps the charge
deterministic and stops a blocked source from continuing to drain a shared
`agent_id` bucket at no cost to itself.

### 3. Reuse `RateLimiter`, do not invent a second limiter type

`ratelimit/__init__.py`'s token bucket is already the right algorithm and
already LRU-bounds its key space (`max_agents`, default 100 000) precisely
because a caller-controlled key was foreseeable. Two further instances are
constructed at startup, one per bucket above.

> **The sentence above is wrong and Decision 8 corrects it.** `max_keys` LRU
> eviction bounds *memory*; it does not make a caller-controlled key safe,
> because an evicted bucket is recreated full. Reusing `RateLimiter` unchanged
> is still the decision; bounding the key space before it reaches the limiter
> is Decision 8's job.

`check()` raises `RateLimitExceeded` *without consuming* when the bucket is
short, so "charge on failure, reject when empty" is the existing API used
as-is — an over-budget attacker cannot dig its own hole deeper, and
`RateLimitExceeded.retry_after` is exactly the `Retry-After` value the response
needs. No penalty stacking, no new class.

Two renames fall out and are worth doing now rather than living with a limiter
whose parameters lie: `check`/`allow`'s first parameter becomes `key`
(it is an `agent_id`, a source prefix, or an attempted identity depending on the
instance), `RateLimiter.__init__`'s `max_agents` becomes `max_keys`, and
`RateLimitExceeded.agent_id` becomes `RateLimitExceeded.key`. These are
in-process Python names, not wire or config names; nothing an operator or agent
can observe changes. ADR-0008 adds the one genuinely new parameter
(`capacity`).

### 4. The failure response no longer distinguishes "no such agent"

Every *authentication* failure — missing header, malformed `Authorization`,
over-long token, unknown `agent_id`, wrong token — returns the same `401`, the
same body `{"detail": "invalid agent credentials"}`, and `WWW-Authenticate:
Bearer` (RFC 9110 requires a challenge on 401; the current code omits it).

`403` is retained for exactly one case: a **correct** credential for a
registered but disabled agent. That is an authorization outcome, not an
authentication one, and learning it requires already holding the agent's live
token — so it discloses nothing to an anonymous attacker. This is the honest
version of the split `middleware.py` documents today, not its removal.

Uniform status and body are not enough on their own, because ADR-0006's
verification path is *faster* for an unknown `agent_id` (dictionary miss, no
HMAC) than for a known one (HMAC + `compare_digest`). The timing oracle would
reconstruct the roster that the status-code change just removed. So: when the
`agent_id` is unknown, the service MUST still compute the HMAC of the presented
token and `compare_digest` it against a fixed, process-lifetime dummy digest,
discarding the result. One HMAC per rejected request is a price worth paying to
make the two paths indistinguishable.

`docs/protocol/observation-v1.md` is updated accordingly. This is the one
externally visible behaviour change in this ADR: an agent presenting a bad token
for a registered `agent_id` now sees `401` where it saw `403`.

### 5. Two length bounds, checked before any hashing

* `X-Agent-Id` longer than 128 characters (`schemas/observation.v1.json`'s
  `agent_id` `maxLength`) is a failure.
* A bearer token longer than 512 characters is a failure.

Both are evaluated before the HMAC, so an attacker cannot make ingest hash
megabytes of attacker-supplied header per request, and both bound the size of
the keys the agent bucket stores. Both count as failed attempts and return the
standard uniform 401.

### 6. Failures are logged; successes are not

One `WARNING` per failed attempt, from `hammertime.ingest.auth`, with a fixed
set of fields (Section 36.5 fixes the exact line). A successful authentication
logs nothing: at `HAMMERTIME_INGEST_RATE_LIMIT_RPS` per agent that would be a
log line per observation batch, and the useful signal is the failures.

The log MUST NOT contain the presented token, any prefix of it, or its hash —
Section 36.1 already forbids the first two, and the hash is a verifier for the
credential, so it is not log material either.

### 7. Source-address resolution is explicit, never implicitly trusted

`X-Forwarded-For` is attacker-controlled unless something trustworthy rewrites
it, and a limiter keyed on a spoofable header is a limiter with an infinite key
space. Default (`HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS=0`) is the socket peer
address and nothing else. A deployment that terminates behind N trusted proxies
sets the hop count and gets the Nth-from-the-right `X-Forwarded-For` entry;
anything shorter, absent or unparsable falls back to the socket peer rather than
to a caller-supplied value.

### 8. The agent bucket is keyed by a fixed-size salted slot, not by the attempted `agent_id`

*Added after implementation, correcting Decisions 2 and 3. A security review of
the implementation found that the budget the agent bucket promises to enforce
was resettable on demand.*

**The vulnerability.** Decision 3 asserted that `RateLimiter` "already LRU-bounds
its key space … precisely because a caller-controlled key was foreseeable". That
conflates two different properties. `max_keys` bounds *memory*. It does not bound
*budget integrity*, because `check()` recreates a first-seen key's bucket **full**
(`tokens=resolved_capacity`) and `_evict_if_full` drops the least-recently-touched
bucket — and a bucket that was evicted is indistinguishable from one never seen.
With the key space attacker-chosen (Decision 2 keys on the raw `X-Agent-Id`;
Decision 5 accepts any value up to 128 characters, registered or not), a target's
budget is restored at will:

1. spend the 20 burst tokens guessing against `edge-17`;
2. send ~100 000 failing requests carrying distinct junk `X-Agent-Id` values,
   walking `edge-17`'s bucket out of the `OrderedDict`;
3. guess `edge-17` again — `bucket is None`, so it is recreated with a full
   20-token budget. Repeat.

At roughly 5 000 junk requests per recovered guess this is expensive for a single
host, which the source bucket caps at 10 + 30/min. But it is precisely *not* a
single host: the junk is spread across the botnet, each member spending its own
source budget, while the guesses are aimed at one identity. A botnet sustaining
millions of failures a minute lifts the guess rate against one `agent_id` from a
fixed 60/min to hundreds per minute, growing without bound as the botnet grows.
That is exactly the threat — "a botnet grinding one identity from thousands of
hosts" — the agent bucket exists to stop, so the mechanism failed at its stated
purpose rather than merely at some edge of it.

The mistake generalizes, which is why it is worth naming: **LRU eviction turns an
unbounded key space into a bounded memory footprint, and at the same time turns a
persistent budget into a resettable one.** A limiter whose key an attacker
chooses inherits that key space's cardinality as its attack surface, whatever
`max_keys` is set to.

**The fix: the agent limiter never sees an attacker-chosen key.** Every presented
`X-Agent-Id` is mapped into a fixed-size table of slots before it reaches the
limiter:

```text
AGENT_SLOT_COUNT = 4096          fixed constant; not operator-configurable
slot_salt        = 32 random bytes, drawn once per process

agent_bucket_key(attempted):
    if attempted is absent, empty, or longer than 128 characters:
        return "-"
    digest = HMAC-SHA-256(slot_salt, attempted.encode("utf-8"))
    return "agent-slot:" + str(int.from_bytes(digest[:8], "big") % AGENT_SLOT_COUNT)
```

The properties this buys, all of which the implementation must preserve:

* **The key space is exactly `AGENT_SLOT_COUNT + 1` = 4097 keys for any input
  whatsoever.** Memory is bounded at 4097 `_Bucket`s (two floats and a
  dictionary entry each — well under a megabyte) *by construction*, without
  eviction ever running: 4097 is a factor of ~24 below `max_keys`'s 100 000
  default, so `_evict_if_full` is unreachable on this instance and a bucket, once
  created, lives for the life of the process. The original reason `max_keys`
  exists is satisfied more strongly than before, not weakened.
* **An identity always lands in the same slot,** so its budget cannot be reset at
  all; the only way to get tokens back is to wait for refill. The 20 burst +
  60/min ceiling Decision 2 promised is now actually a ceiling.
* **Collisions can only subtract budget, never add it.** If junk id `q` shares a
  slot with `edge-17`, `q`'s failures drain `edge-17`'s slot — the attacker
  tightens its own bound. No input sequence yields more than 20 + 60/min against
  a chosen identity.
* **Registered and unregistered identities are mapped by the identical
  function,** so a `429` discloses nothing about the roster (see below).
* **The salt is per process, random, and never leaves it.** An attacker cannot
  work out which junk ids share a slot with a target, so it cannot choose
  collisions deliberately — which would not help it anyway, per the previous
  point; this is belt and braces. The salt is not a secret anyone has to manage:
  it may change on every restart, since bucket state is per process already
  (see the "Deferred" bullet on cross-replica state).

**Why not key on the *registered* `agent_id` only.** The obvious alternative —
ask the registry whether the attempted id exists, give registered ids their own
bucket and drop everything else into the shared `"-"` bucket — also bounds the
key space (at the registry size, which is finite and operator-controlled) and is
simpler. It is rejected because it rebuilds, in the response code, the roster
oracle Decision 4 spends an HMAC per failed request to close. With registered ids
on private buckets and everything else sharing one, that shared bucket sits
permanently empty under any attack, so an attacker tests any candidate identity
with two requests: drain `"-"` with junk, then present the candidate. A `401`
means the candidate had a private bucket, i.e. it is registered; a `429` means it
fell into the drained shared bucket, i.e. it is not. That is a cheaper, more
reliable, and far less subtle
enumeration oracle than the timing one Decision 4 removed. A fixed slot table has
no such asymmetry: a `429` says only "this slot is drained".

**Why not start a recreated bucket empty.** Telling "recreated after eviction"
apart from "first seen" requires remembering evicted keys — the unbounded state
eviction existed to avoid. Starting *every* new bucket empty instead would answer
an agent's first mistyped-token attempt with `429`, which destroys the "one
failure, one 401, one log line" property the logging and misconfigured-rollout
consequences depend on, and it would still leave the key space unbounded. The
slot table removes the cause instead of blunting the symptom.

**The source bucket keeps its keying** (Decision 2: IPv4 /32, IPv6 /64) even
though its key space is large enough that eviction is reachable. The difference
is what eviction buys an attacker. To walk one /32's bucket out of a
100 000-entry LRU, the attacker must *hold* 100 000 distinct source prefixes —
and each of those already carries its own full 10 + 30/min budget. Refilling one
by evicting it gains nothing that sending from the other 99 999 did not already
gain: the source budget is deliberately per prefix, so it scales with the address
space the attacker controls, eviction or no eviction. The agent bucket was
different exactly because its keys were free to manufacture while its budget was
supposed to be per identity.

**Rule for every future `RateLimiter` instance.** The key space MUST be bounded
by something other than `max_keys`: the authenticated `agent_id` (bounded by the
registry — the request and observation budgets of ADR-0008), a fixed slot table
(this decision), or an address prefix whose budget is per prefix by design (the
source bucket). `max_keys` is a memory backstop and MUST NOT be cited as a
security bound again.

**Sizing.** 4096 slots is chosen to sit above the agent count of any deployment
this spec contemplates while keeping the resident set trivial. Collisions matter
only when two colliding identities are failing authentication at the same time,
and then only to diagnosis — never to a caller holding a valid credential
(Decision 1). It is a constant rather than a config key because no operator has
the information needed to tune it and the security property depends on it being
bounded; a deployment large enough to care should revisit this ADR, not an
environment variable.

## Consequences

* **Wire-visible:** a wrong token for a known agent is `401`, not `403`; all
  authentication failures carry `WWW-Authenticate: Bearer`; a throttled failure
  is `429` with `Retry-After` and `X-RateLimit-Scope: auth-failures`
  (Section 36.7). `CHANGES` gets, as one line each:
  * `Return 401 with a uniform body for every failed agent authentication; 403 is now only a correct credential for a disabled agent`
  * `Throttle failed agent authentication per source prefix and per attempted agent_id, answering 429 with Retry-After`
  Neither is `BREAKING`: a correctly configured agent's behaviour is unchanged,
  and no config key is removed or renamed.
* **Four new config keys plus a proxy-hop key**, all with defaults that leave a
  correctly configured deployment untouched (Section 36.5). The defaults are
  sized for "a legitimate agent occasionally gets its token wrong during a
  rollout", not for "an attacker gets a reasonable number of attempts".
* **A misconfigured fleet now fails loudly.** Roll out a bad token to 500 agents
  and they will trip the per-`agent_id` bucket and see `429` rather than a
  uniform `401` — which is correct (something *is* wrong) and is now visible in
  both logs and metrics instead of silence.
* **Two agents whose `agent_id`s collide in the slot table share one failure
  budget** (Decision 8). With 1 000 registered agents in 4 096 slots, roughly one
  agent in nine shares a slot with another; with a fleet in the tens, collisions
  are rare. This costs nothing to a valid credential and only ever *reduces* an
  attacker's guesses, but during a botched rollout two colliding agents reach
  `429` sooner than one alone would. The log line still names the attempted
  `agent_id` on every throttled failure, so the operator loses no diagnosis.
* **A junk-failure flood can hold slots drained,** so a genuinely misconfigured
  agent may see `429` where it would otherwise have seen `401`. That is the
  correct outcome under attack — the service is refusing to evaluate credentials
  at that rate — and, per Decision 1, it still cannot reject a correct one.
* **Shared-source collateral is bounded but not zero.** Two legitimate agents
  behind one NAT still share a source bucket; because only failures charge it,
  one of them has to be misconfigured for the other to notice, and even then the
  healthy agent's own successful requests are never rejected.
* **The 401/403 collapse costs an operator some diagnosis by response code.**
  That is exactly what Section 36.5's log line restores, to the party entitled
  to it: the operator reading server logs, not the caller presenting the guess.
* **No runtime token-strength check is added**, here or in `auth/agents.py`.
  ADR-0006 made that impossible, not merely redundant. `hammertime-agent-token`
  stays the sole enforcement point; this ADR is what keeps a `--allow-weak`
  token from being brute-forceable online.
* **This does not stop a volumetric flood, and is not meant to.** Section 36.5
  states the edge requirement explicitly so no one reads this ADR as having
  covered it.
* **Deferred:** persistent/cross-replica failure state (buckets are per process,
  so N replicas multiply every budget by N — the same limitation
  `ratelimit/__init__.py` already documents for per-agent limits), alerting
  rules on the new metrics, and any notion of a durable lockout that outlives a
  restart.

## Implementation notes

Normative detail (exact statuses, bodies, log line, bucket keys, config keys and
defaults) is Section 36.5; it is the contract, this list is only the mapping
onto the existing code.

* `IngestSettings` (`services/ingest/.../config.py`) gains, in `.env.example`
  order: `auth_failure_rate_per_min: float` (30), `auth_failure_burst: int`
  (10), `auth_failure_agent_rate_per_min: float` (60),
  `auth_failure_agent_burst: int` (20), `trusted_proxy_hops: int` (0). The first
  four reuse `_parse_positive_number` / `_parse_positive_int`; the last needs a
  non-negative-integer parse (0 is valid and is the default).
* `IngestState` (`app.py`) gains two `RateLimiter` instances,
  `auth_failure_source_limiter` and `auth_failure_agent_limiter`, constructed in
  the lifespan alongside the existing one. They are charged with
  `check(key, rate_per_min / 60.0, capacity=burst)` — `capacity` is ADR-0008's
  new keyword — inside `except`/failure paths only.
* The throttle lives in `auth/middleware.py`'s `require_agent` closure (or a
  sibling built from the same state), not in `routes.py`: it must cover every
  failure mode of `authenticate_request`, including the header-shape failures
  that never reach the registry. `require_agent` therefore needs the two
  limiters, a `Clock`, and the resolved source key; keep it a closure built once
  in the lifespan, as today.
* `AgentRegistry.authenticate` is the only place that can distinguish
  `unknown_agent` from `invalid_credential` from `agent_disabled`; the
  existing `hammertime.ingest.auth` exception hierarchy already carries that
  distinction, so the outcome label comes from the exception type and the
  response no longer does.
* The dummy-digest comparison for an unknown `agent_id` belongs in
  `AgentRegistry.authenticate`, computed against a fixed digest derived once at
  registry construction; `UnknownAgentError` is still what it raises.
* Only `RateLimiter` needs the rename to `key` / `max_keys` /
  `RateLimitExceeded.key`; every existing call site passes positionally.
* `CHANGES` needs the two lines quoted above, and `.env.example` the five keys.

Decision 8 was added after the above landed; it is a change to
`auth/middleware.py` only, and deliberately touches no configuration, no wiring,
and no response.

* `middleware.py` gains `_AGENT_SLOT_COUNT = 4096` and an `"agent-slot:"` key
  prefix alongside the existing `_NO_AGENT_ID_KEY = "-"`.
* `_agent_bucket_key` becomes
  `_agent_bucket_key(attempted_agent_id: str | None, *, slot_salt: bytes, slot_count: int) -> str`.
  Absent, empty, or longer than `_MAX_AGENT_ID_LENGTH` still returns `"-"`
  (unchanged semantics, and unchanged reason: an over-long value can never be a
  real `agent_id`). Every other value returns
  `f"agent-slot:{int.from_bytes(hmac.new(slot_salt, value.encode('utf-8'), hashlib.sha256).digest()[:8], 'big') % slot_count}"`.
  The `"-"` key stays *outside* the table: the standing flood of missing-header
  failures must not permanently drain some arbitrary real agent's slot, and a
  caller learns nothing from a key it chose by omitting its own header.
* `.encode("utf-8")` cannot fail here: Starlette decodes header bytes as latin-1,
  so every value is a string of codepoints <= 0xFF with no surrogates (contrast
  `agents.py`'s note on `hmac.compare_digest` and non-ASCII `str`). One extra
  HMAC per *failed* request is noise next to the credential HMAC already spent on
  the same path, and successes never reach it.
* `require_agent` gains two keyword-only parameters, both defaulted so no caller
  must change: `agent_slot_salt: bytes | None = None` (when `None`, draw
  `secrets.token_bytes(32)` **once**, at dependency-construction time, not per
  request) and `agent_slot_count: int = _AGENT_SLOT_COUNT` (reject a non-positive
  value with `ValueError` at construction). They exist so tests can pin the
  mapping; `app.py` passes neither, and no `IngestSettings` field, `.env.example`
  key, or environment variable is added.
* Nothing changes in `ratelimit/__init__.py` except its `_DEFAULT_MAX_KEYS`
  comment, which must now say that LRU eviction bounds memory only — never a
  budget — and point at Decision 8. That comment is what Decision 3 misread.
* `_evict_if_full`, `check()`'s full-bucket-on-first-sight behaviour, `app.py`'s
  limiter construction (default `max_keys`), the charge order, the responses, and
  the log line are all unchanged.
* Regression coverage worth naming explicitly: the same attempted `agent_id` maps
  to the same key across calls; an arbitrary stream of distinct attempted
  `agent_id`s produces at most `slot_count + 1` distinct limiter keys; and a
  target identity's exhausted budget is *not* restored by any number of
  intervening distinct attempted identities (the finding itself).
* No new `CHANGES` line: Decision 8 corrects an unreleased change whose existing
  entry ("Throttle failed agent authentication per source prefix and per
  attempted agent_id, answering 429 with Retry-After") still describes the
  behaviour operators see.
