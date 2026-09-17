# Agent observation protocol v1

`POST /v1/observations` — TLS required, agent authenticated (§36).

```json
{
  "agent_id": "edge-17",
  "sequence": 123456,
  "window_start": "2026-09-14T10:00:00Z",
  "window_seconds": 60,
  "observations": [
    { "ip": "192.168.1.42", "request_count": 183 }
  ]
}
```

## Semantics

* `request_count` is a **delta** for the bucket starting at `window_start`, not a
  running counter.
* `window_start` MUST be aligned to `bucket_seconds`; ingest rejects unaligned
  windows rather than silently re-bucketing.
* `(agent_id, sequence)` is the message identity. Resending an identical message
  is a no-op (§23).
* Agents never assert state. `"state": "HOT"` in a payload is a schema violation (§36).
  The same applies to the per-IP attributes of §46, including `weight` and any
  future provenance entries: they are derived server-side from observations, and
  an agent-supplied `attributes` (or any other unknown key) is a 400 (ADR-0005).
* A message is accepted or rejected as a whole; entries are never partially
  applied. If the same IP appears more than once in `observations`, the counts
  are summed (deltas are additive) — ingest does not reject the message for it,
  and does not apply it twice (ADR-0004).
* Internally, ingest fans the batch out into one event per IP so each IP is
  owned by a single shard (§20). This is invisible to the agent except that a
  `202` is returned only once *every* one of those events is durably recorded.

## Responses

| Status | Meaning |
| --- | --- |
| 202 | Accepted and published |
| 200 | Duplicate — previously accepted, no action taken |
| 400 | Schema violation, malformed IP, unaligned window, count out of range |
| 401 | Authentication failed — missing, malformed, unknown or wrong credential. The body is always `{"detail": "invalid agent credentials"}` and the response carries `WWW-Authenticate: Bearer`; it never reveals whether the `agent_id` is registered (§36.5) |
| 403 | A **correct** credential for a disabled agent, or a body `agent_id` that disagrees with the authenticated one |
| 413 | Body limit, observation-array limit, or a batch whose distinct-IP count exceeds the per-agent observation capacity (§36.6) — send a smaller batch; retrying unchanged will not help |
| 429 | A budget was exceeded. `X-RateLimit-Scope` names which one — `requests` (per-agent request rate), `observations` (per-agent observation rate, charged per distinct IP), or `auth-failures` (repeated failed authentication). Always carries `Retry-After` in whole seconds (§36.7) |
| 503 | Accepted but could not be published; nothing was recorded. Body `{"detail": "failed to publish observation; retry is safe"}`. Retry with the **same** `sequence` — a retry after a partial publish is deduplicated downstream, never double-counted (ADR-0004) |
| 503 | **Service not ready** (§47.2, ADR-0009 A9). Body is exactly what `GET /readyz` answers: `{"status":"starting"}` before ingest has completed startup, `{"status":"stopping"}` once a shutdown has begun. `Content-Type: application/json`. Nothing was read, authenticated, charged or recorded — this gate runs before authentication and both rate budgets. Retry with the **same** `sequence` after `/readyz` answers 200 |

The two 503 bodies are deliberately different shapes: `{"status": ...}` is the
readiness body every Hammertime service shares (`docs/protocol/read-api-v1.md`),
`{"detail": ...}` is the shape of every other ingest error. A client that only
needs "retry later" can treat them alike; a probe can tell them apart.

## Limits

`max_body_bytes`, `max_observations_per_message`, and `max_request_count` are
enforced at the edge; an agent exceeding them is throttled and reported, never
partially applied.

Two rate budgets apply per agent (§36.6): one charged 1 per request, one charged
the number of *distinct* IPs the batch resolves to after duplicate IPs are
summed. A batch is charged that cost whatever its outcome, including a duplicate
`200` and a failed-publish `503` — the one exception is the not-ready `503`
above, which is answered before either budget is consulted. Entries with
`request_count: 0` are charged
but never published — a zero delta changes nothing, so it is dropped rather than
allowed to create per-IP state. An `agent_id` or bearer token longer than 128 /
512 characters respectively is rejected as an authentication failure (§36.5).
