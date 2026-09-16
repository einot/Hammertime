# ADR 0006 — Agent bearer tokens are stored as keyed hashes, never plaintext

Status: accepted

## Context

`services/ingest/src/hammertime/ingest/auth/agents.py` holds each agent's bearer
token as `AgentRecord.token: str` — the live credential, in plaintext, both in
the ingest process's memory and in the on-disk registry
`config/agents.v1.json` that `load_agent_registry_file` reads at startup:

```json
{
  "edge-17": { "token": "REPLACE_ME_local_dev_only_token", "enabled": true, "rate_limit_rps": null }
}
```

Anyone who can read that file — an operator with config-repo access, a backup,
a container image layer, a `kubectl get secret`-adjacent volume mount, a core
dump of the process — recovers every agent's live credential directly and can
impersonate any agent against `POST /v1/observations`. Both the reviewer and
the security auditor flagged this during #28; it was deferred because the fix
changes the on-disk format (a BREAKING change under this repo's `CHANGES`
policy) and needs a provisioning story, not just a hashing call. #32 has since
wired `require_agent` into the live observations route, so the plaintext file is
now the credential store of a running authentication path.

Constraints that shape the fix:

* **Authentication is on the per-request hot path.** `require_agent` runs as a
  FastAPI dependency on every `POST /v1/observations`, at up to
  `HAMMERTIME_INGEST_RATE_LIMIT_RPS` (default 50) *per agent*. This is not a
  human login that happens once a day.
* **Timing safety is already a property of this code.** `authenticate()`
  compares with `hmac.compare_digest` today, on utf-8 bytes, deliberately (a
  latin-1-decoded header byte ≥ 0x80 would raise `TypeError` from a `str`
  comparison). Whatever replaces it must keep both properties.
* **The loader rejects two agents sharing a token.** A copy-pasted entry that
  forgot to change its token would otherwise let one credential authenticate as
  several identities, defeating the per-agent dedup (§23) and rate-limit
  boundaries. That check must survive.
* **`agent_id` is an identity downstream, not just a login.** It participates in
  `event_id` derivation (ADR-0004) and keys dedup and rate-limit state, so
  "rotate by issuing a new `agent_id`" is not a credential rotation — it is a new
  agent as far as the rest of the system is concerned.
* **There is no hot reload.** The registry is read once in `app.py`'s lifespan.
  Any rotation design must work across a restart boundary, not assume a
  `SIGHUP`.

## Decision

### 1. Keyed hash (HMAC-SHA-256 with a deployment key), not a slow hash

The registry stores `token_hash = HMAC-SHA-256(key, token_utf8)`, hex-encoded.
`key` is a deployment-wide secret — a pepper — supplied out of band in
`HAMMERTIME_INGEST_AGENT_TOKEN_KEY`, never in the registry file.

Verification recomputes the HMAC of the presented token and compares the raw
32-byte digests with `hmac.compare_digest`. Digest comparison is exactly as
timing-safe as the current token comparison, and both operands are now fixed-length
bytes, so the utf-8/latin-1 hazard disappears with them.

**Why not a slow hash (scrypt/argon2).** A slow hash exists to buy work factor
against *low-entropy* secrets. Agent tokens are machine-generated 256-bit random
strings (see decision 3), whose hashes are not brute-forceable at any speed. What
a slow hash would cost is concrete: scrypt at defensible parameters is ~50-100 ms
of CPU and tens of MiB, per authenticated request, on the ingest hot path — at
50 rps per agent that is several cores burned on a credential check, or a
verified-token cache that quietly re-introduces the thing this ADR removes.
Rejected.

**Why keyed, and not a bare SHA-256.** With mandated high-entropy tokens, a bare
unsalted SHA-256 would already be safe. The pepper buys the one thing we cannot
otherwise get: defence against an operator who provisions a weak or short token
by hand. Once the registry contains only hashes, ingest can no longer tell a
32-byte random token from `"hunter2"`, and a leaked bare-hash file would let an
attacker grind a dictionary offline at billions of guesses per second. With the
key held outside the file, a leak of the registry alone — the exact threat in
this issue, and the most likely one, since config files travel through git,
backups, and images while runtime secrets usually do not — yields nothing
offline-attackable regardless of token quality. That is the whole reason to pay
for a second secret.

**Why not a per-record salt.** A per-record salt buys nothing here (rainbow
tables against 256-bit randoms are meaningless) and costs something real: equal
tokens would no longer produce equal hashes, silently destroying the loader's
duplicate-credential check. One deployment-wide key keeps that check a byte
comparison.

**What this does not fix.** A memory dump of ingest still yields the key and the
hashes, which is enough to *verify* a guessed token but not to *recover* one —
the dump no longer hands over live credentials that authenticate elsewhere. A
dump also no longer leaks anything an attacker could replay against a different
deployment, since the hashes are key-bound. Full protection of in-process key
material is an HSM/KMS question this repo has no infrastructure for.

**Key handling.** `HAMMERTIME_INGEST_AGENT_TOKEN_KEY` is base64url-encoded
(unpadded), decoding to at least 32 bytes; anything shorter, non-decodable, or
absent is a startup `ConfigurationError`, so a deployment cannot fall back to an
unkeyed or passphrase-keyed mode by accident. The key is required even when the
registry is empty. It is read inside `hammertime.ingest.auth.agents`, not stored
on `IngestSettings`: settings are a struct whose `repr` gets logged, and secrets
do not belong there.

Rotating the *key* rehashes nothing, because we do not hold the plaintext tokens:
it invalidates every agent credential and requires re-provisioning all of them.
That is stated in §36 as an operational consequence, and the key-fingerprint
check in decision 2 makes a mistaken key rotation fail loudly at startup instead
of as a fleet-wide 403 storm.

### 2. On-disk format: `config/agents.v2.json`, a versioned envelope

The registry gains an envelope and a new filename. The bare `agent_id -> entry`
map becomes:

```json
{
  "registry_version": 2,
  "hash_algorithm": "hmac-sha256",
  "key_id": "3f8a1c05d2b74e69",
  "agents": {
    "edge-17": {
      "token_hash": "9f2c...64 hex chars total...1e",
      "enabled": true,
      "rate_limit_rps": null
    },
    "edge-18": {
      "token_hash": "aa10...1e",
      "previous_token_hash": "77bd...c4",
      "previous_token_expires_at": "2026-09-20T00:00:00Z",
      "enabled": true,
      "rate_limit_rps": 200
    }
  }
}
```

* `registry_version` (required, `2`). The plaintext format was v1 — it never had
  a version field, which is precisely why the loader must be able to recognise
  it: a document with no `registry_version`, or any entry carrying `token`, is
  rejected with a message naming `hammertime-agent-token migrate`, so an
  operator who upgrades the binary without migrating the file gets an
  actionable startup error rather than a file of plaintext secrets silently
  treated as hashes (which would fail every request with 403 and leave the
  plaintext on disk).
* `hash_algorithm` (required, currently the single value `"hmac-sha256"`). A
  future algorithm change is then an enum entry, not another envelope bump.
* `key_id` (required): `HMAC-SHA-256(key, "hammertime-agent-token-key-id")`,
  hex, truncated to 16 characters. A fingerprint of the key the hashes were
  computed with. It does not weaken the key (it is an HMAC of a fixed public
  label) and it turns "ingest is configured with the wrong
  `HAMMERTIME_INGEST_AGENT_TOKEN_KEY`" from a total-outage mystery into a
  startup `ConfigurationError` naming both fingerprints.
* `agents` (required object, possibly empty): `agent_id -> record`, with
  `token_hash` (required, 64 hex characters, 32 bytes decoded), optional
  `previous_token_hash` / `previous_token_expires_at` (decision 4), and the
  existing `enabled` (default `true`) and `rate_limit_rps` (default `null`,
  must be a positive integer if set). Unknown keys are rejected at both the
  envelope and record level, as the v1 loader already did per record.

The filename moves to `config/agents.v2.json` because every other format in this
repo carries its version in the filename (`detection.v1.json`,
`observation.v1.json`), and "v2 content in a file called v1" is a trap.
`HAMMERTIME_INGEST_AGENTS_PATH` keeps its name; only its default value changes.

`AgentRecord` stores decoded `bytes`, not hex strings: `token_hash: bytes`,
`previous_token_hash: bytes | None`. Hex case then stops mattering anywhere in
the comparison path, and a malformed hash is a load-time error rather than a
per-request one. `AgentRecord.token` is removed outright — no field on that
dataclass ever holds a credential again.

The loader's duplicate-credential check is preserved and widened: no two agents
may share any hash, current or previous, and an agent's `previous_token_hash`
may not equal its own `token_hash` (a botched rotation that rotated nothing).

### 3. Provisioning: `tools/agent-token`, the supported way to touch this file

New workspace tool `tools/agent-token/`, console script `hammertime-agent-token`
(module `hammertime.tools.agent_token`), matching the existing
`tools/agent-sim` layout. Every subcommand reads
`HAMMERTIME_INGEST_AGENT_TOKEN_KEY` and fails loudly without it.

```text
hammertime-agent-token new-key
    Print a fresh base64url key for HAMMERTIME_INGEST_AGENT_TOKEN_KEY.

hammertime-agent-token key-id
    Print the fingerprint of the currently configured key (diagnoses a
    key_id mismatch reported at ingest startup).

hammertime-agent-token generate --agent-id edge-17 [--rate-limit-rps N]
                               [--disabled] [--registry PATH [--replace]]
    Generate a 256-bit token (secrets.token_urlsafe(32)), print it once —
    this is the only time it exists in readable form — and print the JSON
    registry entry to paste in. With --registry, insert the entry into that
    file atomically (temp file + rename, mode 0600), refusing to clobber an
    existing agent_id without --replace. The plaintext token is never
    written to the registry, to a log, or to any file.

hammertime-agent-token hash --agent-id edge-17 [--allow-weak]
    Read a token on stdin (for an externally issued credential) and print
    the entry. Rejects tokens under 32 characters unless --allow-weak:
    this is the only point in the system that ever sees the plaintext, so
    it is the only place a strength rule can be enforced at all.

hammertime-agent-token rotate --agent-id edge-17 --registry PATH
                              [--overlap-hours 24] [--drop-previous]
    Generate a new token, move the current token_hash to
    previous_token_hash, set previous_token_expires_at to now + overlap,
    print the new token. Refuses if a previous hash is already present
    unless --drop-previous, so an unfinished rotation cannot be stacked.

hammertime-agent-token migrate --in config/agents.v1.json
                               --out config/agents.v2.json
    One-shot conversion of a plaintext v1 registry: hash each token under
    the configured key, carry enabled/rate_limit_rps across, emit the v2
    envelope. Writes out of place and refuses --out == --in, then prints a
    reminder to destroy the plaintext original and its backups. (In-place
    rewriting would leave the plaintext in editor backups and git history
    while making the operator believe it is gone.)
```

The primitives live in `packages/hammertime-core`
(`hammertime.core.auth.tokens`: `generate_token`, `hash_token`,
`derive_key_id`, `decode_key`, plus `REGISTRY_VERSION` / `HASH_ALGORITHM`), so
the tool and `services/ingest` share one implementation and the tool depends on
core only — no tool → service dependency. Registry *parsing and validation*
stays in `services/ingest/.../auth/agents.py`; the tool edits the document as
plain JSON.

### 4. Rotation: exactly one overlapping previous hash, with a mandatory expiry

Supported, deliberately, but bounded at one predecessor.

Not supporting overlap at all would make every routine credential rotation
either an outage (old token dies the moment the file is updated) or an
`agent_id` change — and `agent_id` is a downstream identity (ADR-0004 event
identity, §23 dedup keys, per-agent rate-limit buckets), so swapping it to
rotate a password-equivalent is a far larger blast radius than the problem it
solves. An unbounded `token_hashes: []` array has the opposite failure: nothing
forces old entries out, and a registry accumulates forgotten live credentials —
the exact class of problem this ADR exists to remove.

So: `previous_token_hash` (at most one) is accepted alongside `token_hash`, and
`previous_token_expires_at` (RFC 3339, UTC) is **required whenever
`previous_token_hash` is present**. The overlap window therefore always has a
declared end.

* `authenticate()` computes the presented token's HMAC once and
  `compare_digest`s it against `token_hash` and, if present and unexpired,
  `previous_token_hash`. Both comparisons are evaluated before the results are
  combined, so the number of constant-time comparisons depends on the registry,
  never on the presented token.
* Expiry is evaluated per request against an injected
  `hammertime.core.time.clock.Clock` (the convention `RateLimiter` already
  follows), so the window closes on its own without a second restart — which
  matters because there is no config reload.
* An already-past expiry at load time is **not** a startup failure: the loader
  drops the previous hash and warns. A restart after the window closed must
  succeed, and it must not silently keep honouring the dead credential.
* Rotation procedure (documented in §36.3): `rotate` → restart/redeploy ingest →
  move each agent onto the new token → `rotate --drop-previous` or hand-delete
  the two previous-* keys → restart. Nothing else changes; `agent_id` is stable
  throughout.

### 5. Spec and schema

* **§36 gains subsections 36.1-36.4** (credential storage, registry document,
  rotation, provisioning), stating as normative: tokens MUST be ≥ 256 bits of
  entropy from a CSPRNG; the server MUST NOT persist or log a token in
  recoverable form; the registry MUST store only keyed hashes; comparison MUST
  be constant-time; the deployment key MUST NOT live in the registry file. The
  existing §36 MUST list (TLS, auth, authorization, replay protection, size
  limits, rate limits, schema validation) and the "an agent may not declare
  `IP = HOT`" rule are unchanged.
* **`schemas/agent_registry.v2.json` is added.** Every other wire and config
  format in this repo has a schema (`observation`, `hot_ip_event`,
  `prefix_stats_event`, `detection_config`, `ip_attributes`); a credential store
  whose format is now security-relevant is not the one to leave unspecified. It
  is numbered `.v2` to match `registry_version: 2` and the filename — the v1
  plaintext format simply never had a schema.
* Following the precedent of `detection_config.v1.json` vs
  `core/config/loader.py`, the schema is the normative contract and is exercised
  by tests (the shipped `config/agents.v2.json`, and the tool's output, validate
  against it); ingest's loader keeps its hand-written checks so operators get
  specific `ConfigurationError` messages rather than a jsonschema trace, and
  does not compile the schema at runtime.

## Consequences

* **BREAKING for any running deployment.** An existing
  `config/agents.v1.json` does not load. Upgrading is: set
  `HAMMERTIME_INGEST_AGENT_TOKEN_KEY`, run `hammertime-agent-token migrate`,
  point `HAMMERTIME_INGEST_AGENTS_PATH` at `config/agents.v2.json`, destroy the
  plaintext original. Agents keep their existing tokens and `agent_id`s across
  the migration, so no agent needs reconfiguring — migration and credential
  rotation are separate operations.
* **A new mandatory secret.** Ingest will not start without the key. Every
  ingest replica and anyone provisioning agents needs the same value, and losing
  it means re-provisioning every agent. This is the deliberate price of decision
  1; it is also why `key_id` exists.
* **`AgentRegistry` is no longer constructible from a token alone.**
  `AgentRegistry.from_records` gains the key (and an optional clock), and
  `AgentRecord` carries `token_hash: bytes` instead of `token: str`. Existing
  tests in `services/ingest/.../tests/test_auth.py`, `test_routes.py` and
  `test_pipeline.py` that build records with plaintext tokens must construct
  them through `hash_token(token, key=...)`. Tests keep using a fixed test key;
  they never need a file on disk, exactly as today.
* **Per-request cost is one HMAC-SHA-256 over ≤ a few hundred bytes** — sub-microsecond,
  vs. the current bytes comparison. No measurable change to ingest latency,
  which is the point of not choosing a KDF.
* **The registry file stops being a secret, but does not become public.** It
  still enumerates `agent_id`s and per-agent rate limits, and it is still the
  authorization source of truth — write access to it is full compromise
  (anyone who can write a hash can mint a credential). Read access is no longer
  credential compromise; write access still is, so file permissions and config-repo
  review still matter. §36.2 says so explicitly rather than letting "it's hashed
  now" imply otherwise.
* **Ingest can no longer tell an operator their token is weak**, by construction.
  The `hash` subcommand's length check and `generate`'s CSPRNG are the only
  enforcement points, and `--allow-weak` exists as an explicit, logged escape
  hatch rather than a silent one.
* **`.env.example` gains `HAMMERTIME_INGEST_AGENT_TOKEN_KEY` and changes
  `HAMMERTIME_INGEST_AGENTS_PATH`'s default**, and `config/agents.v1.json` is
  replaced by a committed `config/agents.v2.json`. The sample entry is the hash
  of the existing throwaway dev token `REPLACE_ME_local_dev_only_token` under the
  committed placeholder key
  `REPLACE_ME_local_dev_only_key_do_not_use_00` (43 base64url characters, 32
  bytes decoded), with a matching `key_id`, so `make up` / `make load` keep
  working out of the box. A committed dev key is a publicly known pepper, which
  is exactly as compromised as today's committed plaintext dev token and no more:
  the `REPLACE_ME_` naming and the `.env.example` comment say so, and a real
  deployment replaces both. Making the placeholder deliberately invalid to force
  `new-key` before first start was considered and rejected — it breaks the
  quick start to protect a credential that is public either way.
* **`docs/protocol/observation-v1.md` is unchanged.** The wire protocol still
  sends `Authorization: Bearer <token>` with `X-Agent-Id`; agents are unaffected
  by how the server stores what they present. The 401/403 split is unchanged.
* Deferred, and explicitly not fixed here: mTLS (no PKI in this repo, per
  `auth/middleware.py`), credential expiry independent of rotation, and any
  online reload of the registry. Each is additive to this format — an expiring
  credential would be another optional field, a reload another read of the same
  document.
