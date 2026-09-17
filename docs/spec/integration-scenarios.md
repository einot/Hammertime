# Integration and end-to-end scenarios

What each file under `tests/integration/` and `tests/e2e/` must demonstrate,
and the in-process harness they share. This is the hand-off document for
test-author (who works from `docs/spec/`, `docs/adr/`, `docs/protocol/` and
`schemas/` and never reads a service's source) and the acceptance bar for the
service epics that close issue #26.

Spec: §19, §22, §23, §24, §29, §31, §32, §33, §34, §38, §42, §46, §47.
ADR-0002 (event time, lateness), ADR-0003 (dedup, event identity), ADR-0004
(per-IP fan-out), ADR-0005 (attributes, `weight`), ADR-0009 (lifecycle,
`build_service`), ADR-0010 (prefix predicate, read APIs, stats granularity,
one bucket per observation). Wire shapes: `docs/protocol/observation-v1.md`,
`docs/protocol/read-api-v1.md`, `schemas/*.json`.

## 1. Ground rules

1. **Hermetic.** `pyproject.toml`'s `testpaths` includes `tests/`, so these
   files are collected by the plain `uv run pytest -q` the CI `check` job runs
   — a job with no Docker, broker or Redis. Every test therefore runs all four
   services *in the test process* on one `InMemoryBus`, with in-memory stores,
   a `ManualClock`, and a `tmp_path` for the detection config and the trie's
   snapshot directory. No sockets are opened: HTTP goes through
   `httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app))`.
2. **No skips, no gates.** No `pytest.mark.skip`/`skipif`, no environment
   variable that turns a test off, no `xfail`. A scenario that cannot be
   written from the documents above is a spec gap to report, not a test to
   weaken.
3. **No sleeping for time to pass.** Time is the shared `ManualClock`. The
   only waiting is `wait_until(predicate, timeout=10.0, interval=0.02)` on a
   read endpoint, which exists because the four services hand messages to
   each other asynchronously in the same event loop.
4. **Deterministic numbers.** Every assertion below is exact. Thresholds,
   counts, ratios and weights are computed from the spec formulas
   (§13, §46.4), not observed.
5. **Landing rule.** These tests fail — not error — until the aggregator,
   trie and detector services exist. They must be merged together with, or
   after, the last of those epics, never before: merging them first turns the
   `check` job red on every PR.

## 2. The harness

Lives in `tests/stack.py` (test-author's domain; `tests/__init__.py` already
makes `tests.stack` importable from the repo root) with fixtures in
`tests/conftest.py`. It builds each service through the composition roots
ADR-0009 decision 3 defines:

```python
from hammertime.ingest.service     import build_service as build_ingest
from hammertime.aggregator.service import build_service as build_aggregator
from hammertime.trie.service       import build_service as build_trie
from hammertime.detector.service   import build_service as build_detector
```

each called with that service's `load_settings(env)` result (`env` a plain
dict the harness assembles — see §2.2) plus `bus=<the shared InMemoryBus>` and
`clock=<the shared ManualClock>`; ingest additionally gets
`dedup_store=MemoryDedupStore(clock)` and an `agent_registry` (§2.3). The
harness then awaits `start()` on all four in pipeline order (ingest,
aggregator, trie, detector), asserts each `.ready` is `True`, and runs each
`run()` as an `asyncio.Task` for the test's duration; teardown awaits `stop()`
on each and then the tasks.

Suggested surface (names are the harness author's to choose; behaviour is
not):

| Member | Behaviour |
| --- | --- |
| `clock` | the shared `ManualClock`, initialised to `T0 = 1_800_000_000` (2027-01-15T08:00:00Z, aligned to `bucket_seconds`) |
| `bus` | the shared `InMemoryBus` |
| `post(sequence, entries, *, window_start=None, window_seconds=10, agent="edge-17") -> httpx.Response` | `POST /v1/observations` on ingest with headers `X-Agent-Id` and `Authorization: Bearer …`; `window_start` defaults to `clock.now()` rendered RFC 3339; `entries` is `[(ip_text, request_count), …]` |
| `prefix(cidr)`, `ip(addr)`, `hot_prefixes(minimal)`, `detections(minimal)` | `GET` the trie/detector read endpoints (`docs/protocol/read-api-v1.md`) and return the parsed JSON |
| `advance(seconds)` | `clock.advance(seconds)` then `await aggregator.run_maintenance()` |
| `publish_config(document)` | write `document` (a dict) as JSON to the config path, then `await reload_config()` on ingest, aggregator, trie and detector in that order; returns the aggregator's returned `DetectionConfig` |
| `settle()` | post one observation **as the second agent `sentinel`** (§2.3) for a fresh IP from `203.0.113.0/24` with `request_count=1200` under that agent's next sequence, then `wait_until` the trie reports it `HOT`. Because `hammertime.observations.v1` is a single ordered log in `InMemoryBus`, this proves every earlier observation has been applied by the aggregator and its transitions applied by the trie. It does *not* prove the detector has caught up — poll `detections()` for that. Fewer than 16 sentinels are ever posted per test, so `203.0.113.0/24` can never qualify as `HOT_PREFIX` |
| `kill_trie()` | cancel the trie's `run()` task and drop the object **without** calling `stop()` (no final snapshot, no commit) |
| `start_trie()` | build a fresh trie service from the same settings and bus, `await start()`, resume its `run()` task |
| `records(topic)` | every `(key, decoded EventEnvelope)` on `topic`, read straight off `bus._logs[topic]` — the precedent set by `services/ingest/src/hammertime/ingest/tests/test_pipeline.py::_topic_records` — **excluding sentinel traffic**: records whose key is an address in `203.0.113.0/24`, or a prefix that contains any address of it (i.e. every `203.…` prefix the trie reports for a sentinel), or an envelope with `agent_id == "sentinel"`. Every "exactly N records" assertion in §3-§6 is over this filtered view |
| `wait_until(predicate, timeout=10.0)` | poll an `async` predicate every 20 ms; raise `AssertionError` with the last observed value on timeout |

### 2.1 Detection configuration in force at start (`v1`)

Identical to `config/detection.v1.json`:

```json
{ "config_version": 1, "window_seconds": 300, "bucket_seconds": 10,
  "hot_threshold": 1000, "cold_threshold": 800,
  "minimum_hot_ips": 16, "minimum_hot_ratio": 0.10,
  "allowed_lateness_seconds": 30, "state_retention_seconds": 600,
  "weight_function": "threshold_ratio", "weight_max": 1000000 }
```

Written to `tmp_path / "detection.json"` before any service is built.

### 2.2 Environment the harness assembles

```text
HAMMERTIME_ENV=test            HAMMERTIME_LOG_LEVEL=warning
HAMMERTIME_CONFIG_PATH=<tmp_path>/detection.json
HAMMERTIME_BUS_KIND=memory     HAMMERTIME_STORE_KIND=memory
HAMMERTIME_INGEST_BIND=127.0.0.1:0      HAMMERTIME_TRIE_QUERY_BIND=127.0.0.1:0
HAMMERTIME_DETECTOR_BIND=127.0.0.1:0    HAMMERTIME_AGGREGATOR_BIND=127.0.0.1:0
HAMMERTIME_INGEST_RATE_LIMIT_RPS=100000
HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS=1000000
HAMMERTIME_INGEST_OBSERVATION_BURST=1000000
HAMMERTIME_SHARD_COUNT=1       HAMMERTIME_SHARD_IDS=0
HAMMERTIME_TRIE_SNAPSHOT_DIR=<tmp_path>/snapshots
HAMMERTIME_TRIE_SNAPSHOT_INTERVAL_S=1000000
HAMMERTIME_CONFIG_POLL_INTERVAL_S=1000000
HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S=1000000
HAMMERTIME_STARTUP_TIMEOUT_S=10  HAMMERTIME_SHUTDOWN_TIMEOUT_S=5
```

Bind addresses are never listened on (ASGI transport); they only have to
parse. The huge periodic intervals make every periodic action happen only
when a test calls `advance`, `snapshot_now` or `publish_config` explicitly —
so a wall-clock timer can never race a `ManualClock`-driven assertion. The
rate limits are large so that none of these scenarios can touch a 429; the
rate-limiter clock is the shared `ManualClock` (it never refills), and
throttling has its own tests under `services/ingest`.

### 2.3 Agents

Two agents, built exactly as
`services/ingest/src/hammertime/ingest/tests/test_pipeline.py::_build_app`
builds its registry: `AgentRegistry.from_records([AgentRecord(agent_id=…,
token_hash=bytes.fromhex(hash_token(token, key=KEY)), enabled=True,
rate_limit_rps=None), …], key=KEY)` with any 32-byte `KEY` and any token
strings.

* `edge-17` — the scenario's agent. The sequences written in §3-§6 are its
  sequences, explicit in every `post` call.
* `sentinel` — used only by `settle()`, with its own independent sequence
  counter, so sentinel traffic never interferes with `edge-17`'s dedup
  window (§23) or with the scenario's explicit sequence numbers.

### 2.4 Time model the scenarios rely on

* An observation's whole `request_count` lands in the bucket containing its
  `window_start` (ADR-0010 decision 6). `window_start` must be a multiple of
  `bucket_seconds` (protocol).
* The aggregator judges lateness against the shared clock: an observation is
  counted iff `0 <= now - window_start <= window_seconds + allowed_lateness_seconds`
  (= 330 s); otherwise it is published to
  `hammertime.observations-reconciliation.v1` and not counted (ADR-0002).
* A bucket starting at `S` has certainly left the window once
  `now >= S + window_seconds + bucket_seconds`. Scenarios advance by at least
  310 s past the newest relevant `window_start` and never probe the exact
  boundary.
* `weight` on a `HotIpAdded` is `threshold_ratio(window_count, config)` from
  §46.4: `clamp((1000 * window_count + hot_threshold // 2) // hot_threshold, 0, weight_max)`.

## 3. `tests/integration/test_pipeline.py` — one observation through every stage (§19, §42, §23, §24, §46.5)

Purpose: prove the event chain of §19 end to end for a single IP, that each
hop emits exactly what the schemas and ADRs say, that dedup and lateness stop
what they must, and that expiry drives the reverse transition.

1. `post(1, [("10.20.30.1", 1200)])` -> **202**.
2. `wait_until(ip("10.20.30.1")["state"] == "HOT")`. Then assert the body:
   `request_count == 1200`, `attributes == {"attributes_version": 1, "weight": 1200}`,
   `config_version == 1`, `matched_prefixes` has three entries for
   `10.0.0.0/8`, `10.20.0.0/16`, `10.20.30.0/24`, each with `hot_ips == 1`
   and `state == "NORMAL"`.
3. Bus contents, decoded with `hammertime.core.events.codec.decode`:
   * `hammertime.observations.v1`: exactly one record; key `b"10.20.30.1"`;
     envelope `event_type == "RequestObservation"`, `agent_id == "edge-17"`,
     `sequence == 1`, `subject == "10.20.30.1"`, `config_version == 1`;
     payload has one entry `(10.20.30.1, 1200)`, `window_seconds == 10`.
   * `hammertime.hot-ip.v1`: exactly one record; key `b"10.20.30.1"`;
     `event_type == "HotIpAdded"`; payload `ip == 10.20.30.1`,
     `window_count == 1200`, `config_version == 1`,
     `attributes == {"attributes_version": 1, "weight": 1200}`; envelope
     `agent_id` is non-empty and is **not** `"edge-17"` (ADR-0003 amendment:
     the producing shard, not the originating agent).
   * `hammertime.prefix-stats.v1`: exactly 25 records, one per prefix
     `10.0.0.0/8` … `10.20.30.1/32`; each `hot_count == 1`,
     `capacity == 2 ** (32 - length)`; all 25 share one `sequence`
     (ADR-0010 decision 3). Keys are the prefix text.
4. `prefix("10.20.30.0/24")` -> `hot_ips == 1`, `capacity == 256`,
   `hot_ratio == 1/256`, `state == "NORMAL"` (1 < `minimum_hot_ips`, §13).
   `detections()["detections"] == []`.
4b. **Redelivery is idempotent** (§23, §46.5, ADR-0003): take the hot-ip
   record's raw key and bytes from `bus._logs["hammertime.hot-ip.v1"]` and
   `await bus.producer().publish("hammertime.hot-ip.v1", key, value)` again —
   a byte-identical `HotIpAdded` with the same `event_id`, delivered while
   the IP is already HOT. `settle()`. Assert `prefix("10.20.30.0/24")["hot_ips"]
   is still `1` (not 2), `ip("10.20.30.1")["state"] == "HOT"`, and the
   prefix-stats log gained no records with `hot_count == 2`.
5. **Duplicate** (§23, ADR-0003): `post(1, [("10.20.30.1", 1200)])` again ->
   **200** with `{"status": "duplicate"}`; `settle()`; the observations log
   and the hot-ip log are unchanged in length; `ip("10.20.30.1")` unchanged.
6. **Same IP, no transition** (§6): `post(2, [("10.20.30.1", 100)])` ->
   202; `settle()`; hot-ip log still has one record; `ip(...)["request_count"]`
   is still `1200` (the transition-time value, `read-api-v1.md`).
7. **Late observation** (§24, ADR-0002): `post(3, [("10.20.30.1", 5000)],
   window_start=T0 - 400)` -> **202** at ingest (alignment is all ingest
   checks). `settle()`; `hammertime.observations-reconciliation.v1` has
   exactly one record whose payload names `10.20.30.1`; the observations
   topic gained the record too (ingest publishes it; the aggregator diverts
   it); hot-ip log still one record; the IP is still HOT with
   `request_count == 1200`.
8. **Expiry drives HOT -> COLD** (§5, §6, §26): `advance(310)`; then
   `wait_until(ip("10.20.30.1")["state"] == "COLD")`; the body has no
   `attributes` key and `request_count == 0`; the hot-ip log has exactly two
   records, the second `HotIpRemoved` with `window_count == 0`,
   `config_version == 1`; `prefix("10.20.30.0/24")["hot_ips"] == 0`;
   `prefix-stats` gained 25 more records with `hot_count == 0`.

Step 4b is placed while the IP is HOT on purpose: a redelivered `HotIpAdded`
for a COLD IP legitimately makes it HOT again (§46.5, "replaces any existing
record"), so idempotence is only observable against an already-HOT IP.

## 4. `tests/integration/test_config_change.py` — a threshold change re-evaluates, never leaves stale state (§34, §47.3, §38)

Purpose: prove the version rule and the re-evaluation in both directions,
and that a rejected or non-increasing document has no effect.

1. `post(1, [(f"10.20.30.{i}", 600) for i in 1..32])` -> 202. `settle()`.
   Assert `prefix("10.20.30.0/24")["hot_ips"] == 0`, hot-ip log empty (600 <
   1000), `detections()["detections"] == []`.
2. `publish_config(v2)` where `v2` = v1 with `config_version: 2,
   hot_threshold: 500, cold_threshold: 400`.
   `wait_until(prefix("10.20.30.0/24")["hot_ips"] == 32)`; assert
   `state == "HOT_PREFIX"` (32 >= 16 and 32/256 = 0.125 >= 0.10),
   `config_version == 2`. Hot-ip log: exactly 32 `HotIpAdded`, every one
   with `config_version == 2`, `window_count == 600`, `weight == 1200`
   (`(1000*600 + 250) // 500`). `wait_until(detections()` lists
   `10.20.30.0/24` with `state == "HOT_PREFIX"`, `hot_count == 32`,
   `config_version == 2`)`; `detections(minimal=True)` lists exactly that
   one prefix; `10.20.0.0/16` is absent from both (32/65536).
3. `publish_config(v3)` = v1 thresholds under `config_version: 3`
   (`hot_threshold: 1000, cold_threshold: 800`).
   `wait_until(prefix("10.20.30.0/24")["hot_ips"] == 0)`; hot-ip log has
   exactly 32 more records, all `HotIpRemoved` with `config_version == 3`
   and `window_count == 600` (600 < 800; hysteresis re-evaluated under the
   new config, §38); `state == "NORMAL"`, `config_version == 3`;
   `wait_until(detections()["detections"] == [])`.
4. **Non-increasing version is ignored** (§47.3): `publish_config(v3_again)`
   = `v3` but with `hot_threshold: 100, cold_threshold: 50` and the same
   `config_version: 3`. `settle()`. Hot-ip log length unchanged;
   `prefix(...)["config_version"] == 3`; `hot_ips == 0`.
5. **Invalid document is rejected, previous stays in force**:
   `publish_config({... "config_version": 4, "hot_threshold": 800,
   "cold_threshold": 800 ...})` (cold not strictly below hot). `settle()`.
   Hot-ip log length unchanged; every read endpoint still reports
   `config_version == 3`.
6. **Descriptive-only change needs no re-evaluation** (§34): `publish_config(v4)`
   = `v3` with `config_version: 4, weight_max: 5000`. `settle()`. Hot-ip
   log length unchanged; `wait_until(prefix(...)["config_version"] == 4)`
   and `detections()["config_version"] == 4`.

## 5. `tests/integration/test_recovery.py` — kill the trie mid-stream; snapshot + replay restore exact counts (§32, §33, §46.8, §47.2, §47.4)

Purpose: prove the trie is derived state that snapshot-plus-replay
reconstructs exactly — including removals and attribute records — that the
restarted service is not ready until it has caught up, and that a graceful
stop leaves a newer snapshot.

1. `post(1, [(f"10.20.30.{i}", 1200) for i in 1..100])`. `wait_until(prefix(
   "10.20.30.0/24")["hot_ips"] == 100)`.
2. `snap1 = await trie.snapshot_now()`; assert `snap1` exists under the
   snapshot dir.
3. `advance(310)`; `wait_until(prefix(...)["hot_ips"] == 0)` (100
   `HotIpRemoved`).
4. `post(2, [(f"10.20.30.{i}", 1200) for i in 101..156])`; `wait_until(
   prefix(...)["hot_ips"] == 56)`. Record `before = prefix("10.20.30.0/24")`
   and `ip_before = ip("10.20.30.156")`.
5. `kill_trie()`. Assert the newest file in the snapshot dir is still
   `snap1` (no snapshot was written on the way down).
6. `new = start_trie()` — before `await new.start()`, `new.ready is False`;
   after, `True`. Assert `trie.app`'s `GET /readyz` is 200.
7. Exactness after replay: `prefix("10.20.30.0/24") == before` field for
   field (`hot_ips == 56`, `state == "HOT_PREFIX"`, `event_sequence ==
   256` = 100 + 100 + 56 applied events, `config_version == 1`);
   `ip("10.20.30.156") == ip_before` (HOT, `weight == 1200`);
   `ip("10.20.30.1")["state"] == "COLD"` with no `attributes`;
   `hot_prefixes(minimal=True)["prefixes"]` is exactly `[10.20.30.0/24]`.
8. Consumption resumes at the right place: `post(3, [("10.20.30.1", 1200)])`;
   `wait_until(prefix(...)["hot_ips"] == 57)` — not 58 or more (no
   double-application of the 56 replayed adds) and not stuck at 56.
9. Graceful stop writes a newer snapshot (§47.4): `await trie.stop()`;
   assert the newest snapshot file is not `snap1`. `start_trie()` again;
   `prefix(...)["hot_ips"] == 57`, `event_sequence == 257`.

## 6. `tests/e2e/test_bot_network_scenario.py` — §42 worked example, plus §15 and §31

Purpose: the spec's own narrative: 156 hot IPs in a /24 make a bot-network
candidate; one aggressive IP does not; nested ancestors collapse to the most
specific; decay below threshold clears it without a rescan.

1. **Individual anomaly is not a prefix anomaly (§15).**
   `post(1, [("198.51.100.7", 500_000)])`; `wait_until(ip("198.51.100.7")
   ["state"] == "HOT")`; `attributes["weight"] == 500_000`;
   `prefix("198.51.100.0/24")` -> `hot_ips == 1`, `state == "NORMAL"`;
   `detections()["detections"] == []`.
2. **The /24 (§42).** `post(2, [(f"10.20.30.{i}", 1200) for i in 1..156])`.
   `wait_until(prefix("10.20.30.0/24")["hot_ips"] == 156)`; assert
   `hot_ratio == 0.609375`, `state == "HOT_PREFIX"`;
   `prefix("10.20.0.0/16")` -> `hot_ips == 156`, `state == "NORMAL"`.
   `wait_until(detections(minimal=True)["detections"]` is exactly
   `[10.20.30.0/24]`)`; `198.51.100.0/24` absent.
3. **Nested ancestors and the minimal set (§31).**
   `post(3, [(f"10.20.31.{i}", 1200) for i in 1..156])`;
   `wait_until(prefix("10.20.31.0/24")["hot_ips"] == 156)`.
   Then `wait_until` the full `detections()` list is exactly, in order
   (length descending, address ascending):
   `10.20.30.0/24 (156/256)`, `10.20.31.0/24 (156/256)`,
   `10.20.30.0/23 (312/512)`, `10.20.28.0/22 (312/1024)`,
   `10.20.24.0/21 (312/2048 = 0.15234375)`; `10.20.16.0/20` is absent
   (312/4096 < 0.10). `detections(minimal=True)` is exactly the two /24s.
   `hot_prefixes(minimal=True)` on the trie agrees with the detector's
   minimal list (same predicate, ADR-0010 decision 1).
4. **Hysteresis (§7) and decay (§42).** `advance(200)` (nothing expires:
   200 < 300). Then, at `window_start = clock.now()`:
   `post(4, [(f"10.20.30.{i}", 1200) for i in 1..15] + [("198.51.100.7", 900)])`.
   `settle()`. Now `advance(110)` (total 310 past `T0`): the original
   buckets expire.
   * `wait_until(prefix("10.20.30.0/24")["hot_ips"] == 15)`; assert
     `hot_ratio == 0.05859375`, `state == "NORMAL"`;
   * `prefix("10.20.31.0/24")["hot_ips"] == 0`;
   * `wait_until(detections()["detections"] == [])`;
   * `ip("198.51.100.7")["state"] == "HOT"` (900 >= `cold_threshold` 800:
     the refreshed window keeps it hot, §7) and `request_count == 500_000`
     (transition-time value unchanged);
   * hot-ip log: exactly 1 + 156 + 156 `HotIpAdded` and 141 + 156
     `HotIpRemoved`, every `HotIpRemoved` with `window_count == 0`.
5. `advance(310)`: `wait_until(ip("198.51.100.7")["state"] == "COLD")` and
   `prefix("10.20.30.0/24")["hot_ips"] == 0`.

## 7. What is deliberately not covered here

* Kafka, Redis and the compose stack. A `HAMMERTIME_TEST_STACK=compose`
  driver that points the same harness surface at `localhost:8080-8083` is a
  natural follow-up once `deploy/docker-compose.yml` mounts the config file
  and `ci.yml` sets the variable; it is not part of #26.
* Throttling, auth failures, schema rejection — `services/ingest`'s own tests.
* Trie invariants under random streams — `tests/property/test_trie_properties.py`.
* Metrics content (§37) — the telemetry epic; only the endpoints' existence
  is asserted (`/metrics` returns 200 with the Prometheus content type on
  every service, including the aggregator).
