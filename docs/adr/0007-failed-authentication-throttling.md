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
