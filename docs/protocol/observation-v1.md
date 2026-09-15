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
| 401 / 403 | Unknown or unauthorized agent |
| 413 | Body or observation-array limit exceeded |
| 429 | Per-agent rate limit |
| 503 | Accepted but could not be published; nothing was recorded. Retry with the **same** `sequence` — a retry after a partial publish is deduplicated downstream, never double-counted (ADR-0004) |

## Limits

`max_body_bytes`, `max_observations_per_message`, and `max_request_count` are
enforced at the edge; an agent exceeding them is throttled and reported, never
partially applied.
