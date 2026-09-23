# Read and admin API v1 (trie, detector, and every service's admin endpoints)

Operator-facing HTTP, not agent-facing. Spec §22, §29, §31, §46.7, §47;
ADR-0009 (lifecycle, admin endpoints), ADR-0010 (shared prefix predicate,
response shapes). No authentication in v1: these endpoints are expected to be
reachable only inside the deployment (they are not TLS-terminated by the
services and expose no write operations).

All timestamps are RFC 3339 UTC with a `Z` suffix, as in every event schema.
`event_sequence` is the responding service's own position or counter (§22,
ADR-0003 amendment):

* for the trie, its position in `hammertime.hot-ip.v1`: the stream offset
  of the next hot-ip record it will read. Normally that is one past the
  last record it has handled. Offsets whose records the log no longer
  holds when the trie starts (aged out or purged) are skipped over, so a
  trie that starts on a log holding no record reports the log's end. On
  the reference deployment's log, whose offsets start at 1, it is at least
  1 once the trie is ready. It only grows, across restarts too, while the
  stream exists, and it is not a count of events (ADR-0017 decisions 4
  and 8);
* for the detector, the `sequence` of the newest `PrefixStatsChanged` applied.

The trie's `as_of` is the greatest `timestamp` among the hot-ip events it has
applied. It is `null` until the trie has applied one (ADR-0017 decision 8).

## Admin endpoints — every service (§47)

| Method, path | Ready? | Status, body |
| --- | --- | --- |
| `GET /healthz` | any | `200 {"status":"ok"}` — the process is up and its socket is open |
| `GET /readyz` | yes | `200 {"status":"ready"}` |
| `GET /readyz` | no | `503 {"status":"starting"}` before `start()` has completed, `503 {"status":"stopping"}` after a shutdown was requested |
| `GET /metrics` | any | `200`, `text/plain; version=0.0.4`, Prometheus exposition (§37). May be empty until the telemetry epic lands. |

Bind addresses: ingest `HAMMERTIME_INGEST_BIND` (8080), trie
`HAMMERTIME_TRIE_QUERY_BIND` (8081), detector `HAMMERTIME_DETECTOR_BIND`
(8082), aggregator `HAMMERTIME_AGGREGATOR_BIND` (8083). The aggregator serves
only the three endpoints above.

Every domain endpoint below answers exactly like `GET /readyz` (503 with the
same body) while the service is not ready, and so does ingest's
`POST /v1/observations` (`docs/protocol/observation-v1.md`). The JSON bodies
in the table are byte-exact — compact, no whitespace, `Content-Type:
application/json` — because every service renders the same
`hammertime.core.runtime.AdminResponse` verbatim (ADR-0009 A4, A6). The
readiness state is three-valued (`starting` from construction, `ready` from
the end of startup, `stopping` from the moment shutdown is requested — before
any connection is closed), so a 503 always says which side of `ready` the
service is on.

The aggregator's pure-ASGI admin app additionally answers
`404 {"status":"not_found"}` for any other path and
`405 {"status":"method_not_allowed"}` for a non-GET on the three above; the
FastAPI services answer their frameworks' own 404/405 for the same cases.

The trie serves no generated API documentation. `GET /openapi.json`,
`/docs`, `/docs/oauth2-redirect` and `/redoc` answer 404, like any other
path it does not serve (ADR-0017 decision 13). This document, not a
generated schema, is the trie's contract.

## Trie — port 8081 (§29, §31, §46.7)

### `GET /prefix/{cidr}`

`{cidr}` is `address/length`, e.g. `/prefix/10.20.30.0/24`. Host bits set, an
invalid address, or a length outside `[0, bit_length]` -> `400 {"detail": ...}`.

```json
{
  "prefix": "10.20.30.0/24",
  "hot_ips": 156,
  "capacity": 256,
  "hot_ratio": 0.609375,
  "state": "HOT_PREFIX",
  "as_of": "2026-09-14T10:05:00Z",
  "event_sequence": 156,
  "config_version": 1
}
```

* `hot_ips` — the node's `hot_count` (§29 uses this name on the wire; §12's
  invariant is about the same value).
* `capacity` — `2 ** (bit_length - length)` as a JSON integer (§29's example
  shape). For IPv6 prefixes shorter than /75 this exceeds 2**53; clients must
  parse it with arbitrary precision. (The event schema uses a decimal string
  for the same value; the read API follows §29.)
* `hot_ratio` — `hot_ips / capacity`, computed exactly then narrowed once
  (`Prefix.hot_ratio`).
* `state` — `evaluate_prefix_state(hot_ips, capacity, config)`: `"NORMAL"` or
  `"HOT_PREFIX"` (ADR-0010). `"BOT_NETWORK"` is never returned in v1.
* A prefix with no trie node is `200` with `hot_ips: 0`, `hot_ratio: 0.0`,
  `state: "NORMAL"`.

### `GET /ip/{addr}`

Malformed address -> `400`.

```json
{
  "ip": "10.20.30.1",
  "state": "HOT",
  "request_count": 1200,
  "attributes": { "attributes_version": 1, "weight": 1200 },
  "matched_prefixes": [
    { "prefix": "10.0.0.0/8",     "hot_ips": 156, "capacity": 16777216, "hot_ratio": 0.0000093, "state": "NORMAL" },
    { "prefix": "10.20.0.0/16",   "hot_ips": 156, "capacity": 65536,    "hot_ratio": 0.00238,   "state": "NORMAL" },
    { "prefix": "10.20.30.0/24",  "hot_ips": 156, "capacity": 256,      "hot_ratio": 0.609375,  "state": "HOT_PREFIX" }
  ],
  "as_of": "2026-09-14T10:05:00Z",
  "event_sequence": 156,
  "config_version": 1
}
```

* `state` — `"HOT"` iff the IP's `/32` (or `/128`) node has `hot_count == 1`,
  else `"COLD"` (§12).
* `request_count` — the `window_count` carried on the IP's most recent
  `HotIpAdded`; `0` while COLD. The trie keeps no counters (§18), so this is
  the count *at transition time*, not a live value.
* `attributes` — present iff `state == "HOT"`: the stored `IpAttributes`
  record for the IP (§46.5-46.7). An absent `attributes` on the event is
  stored, and returned, as `{"attributes_version": 1}`.
* `matched_prefixes` — the ancestors at lengths 8, 16 and 24 (IPv4), in that
  order, each shaped like a `GET /prefix` body minus the envelope fields.

### `GET /prefixes/hot[?minimal=true]`

```json
{
  "prefixes": [ { "prefix": "10.20.30.0/24", "hot_ips": 156, "capacity": 256, "hot_ratio": 0.609375, "state": "HOT_PREFIX" } ],
  "as_of": "…", "event_sequence": 156, "config_version": 1
}
```

Every node whose `state` is `HOT_PREFIX`, ordered by prefix length descending
then address ascending. With `minimal=true`, only those with no qualifying
descendant (§31: the most specific qualifying prefixes).

## Detector — port 8082 (§22, §31, ADR-0010)

### `GET /detections[?minimal=true]`

```json
{
  "as_of": "2026-09-14T10:05:00Z",
  "event_sequence": 156,
  "config_version": 1,
  "detections": [
    {
      "prefix": "10.20.30.0/24",
      "state": "HOT_PREFIX",
      "hot_count": 156,
      "capacity": 256,
      "hot_ratio": 0.609375,
      "since": "2026-09-14T10:04:12Z",
      "event_sequence": 16,
      "config_version": 1
    }
  ]
}
```

* One item per prefix whose *latest known* `PrefixStatsChanged` satisfies
  `evaluate_prefix_state(...) == HOT_PREFIX` under the config version in force.
  `minimal=true` keeps only items with no qualifying descendant in the list
  (§31).
* `since` — when the prefix most recently entered `HOT_PREFIX`; reset each
  time it leaves.
* Item-level `event_sequence` — the `sequence` of the *latest*
  `PrefixStatsChanged` applied to this prefix, so a client can correlate an
  item with the trie's own `event_sequence`.
* The list reflects every applied stats event immediately; alert debounce
  never delays it (ADR-0010 decision 5).
* Ordering: prefix length descending, then address ascending (same as the
  trie's list).

## Errors

`400` bodies are `{"detail": "<message>"}` (FastAPI's default). No endpoint
returns `404` for an unknown prefix or address — see the zero-valued answers
above. `503` is reserved for not-ready.
