# Specification

The authoritative architecture specification is
[`hammertime_spec_1.md`](./hammertime_spec_1.md) — *Distributed IP Activity &
Prefix Detection Service*, 47 sections (1-45 as originally written; §46 added by
ADR-0005, which also adds pointer notes to §9, §12, §16, §29, §33, §34, §37;
§47 added by ADR-0009, which adds pointer notes to §33 and §34, ADR-0010
adds pointer notes to §13 and §29, ADR-0011 adds pointer notes to §5, §20,
§24, §26, §30, §34 and §37, ADR-0001 Amendment 1 — the consistency model
for many aggregator shards feeding one trie — adds pointer notes to §21 and
§22, and ADR-0012 — open-source-only components with named drop-ins — adds
a pointer note to §43; ADR-0013 — NATS JetStream as the event log, static
shard assignment — rewords the §20, §22, §24, §33, §43 and §47.2 notes;
ADR-0014 — the trie structure package: one trie per address family, a pruned
binary reference, an arena-backed Patricia production trie and the invariants
both satisfy — adds pointer notes to §9, §11, §12, §27 and §46.5; ADR-0015 —
prefix metadata in prefix-keyed side maps, the two combine directions and the
per-IP attribute record, validated on write and coupled to the hot-count step
— adds pointer notes to §9, §12, §16, §17, §46.2, §46.5, §46.8 and §46.9).
§36 has since gained subsections: §36.1-36.4 from ADR-0006 (hashed agent
credentials, registry document, rotation, provisioning), §36.5-36.7 from
ADR-0007 (failed-authentication throttling) and ADR-0008 (observation-scaled
rate limiting), which also extend §37's ingestion metrics. §36's original
requirements are unchanged.

Companion documents in this directory:

* [`integration-scenarios.md`](./integration-scenarios.md) — what each file
  under `tests/integration/` and `tests/e2e/` must demonstrate, and the
  in-process harness they share (issue #26). Written for test-author, who
  works from the spec and never from the implementation.

Operator-facing HTTP (trie and detector read APIs, every service's
`/healthz`, `/readyz`, `/metrics`) is specified in
[`../protocol/read-api-v1.md`](../protocol/read-api-v1.md).

Sections are referenced by number throughout the codebase: every module docstring
cites the section it implements, e.g. `Spec: §6, §30` for the HOT/COLD state
machine. When the spec is revised, update this index and re-check the cited
sections in the modules below.

Section index used throughout the code:

| § | Topic | Implemented in |
| --- | --- | --- |
| 4 | Agent ingestion protocol | `services/ingest` |
| 5 | Sliding window / buckets | `services/aggregator/window/counter.py`, `services/aggregator/window/store.py`, `core/time`, `docs/adr/0011` |
| 6, 7, 38 | HOT/COLD hysteresis | `core/state/machine.py`, `tests/property/test_state_machine.py` |
| 8-12 | Binary IP trie & invariants | `services/trie/structure` (`node.py`, `binary_trie.py`, `invariants.py`), `packages/hammertime-testkit` (`invariants.py`), `docs/adr/0014` |
| 13, 38 | `HOT_PREFIX` predicate (single implementation) | `core/state/prefix.py`, `services/detector/rules/baseline.py`, `services/trie/query`, `docs/adr/0010` |
| 13, 14, 31 | Prefix classification & scoring | `services/detector` |
| 16, 17 | Prefix metadata inheritance | `services/trie/metadata` (`combine.py`, `local.py`), `docs/adr/0015` |
| 19 | Event-driven internals | `core/events`, `packages/hammertime-bus` (`interface.py`, `memory.py`, `nats.py`), `tools/provision`, `docs/adr/0004`, `docs/adr/0013`, `docs/adr/0015` (Amendment 3 ruling 3: the codec's integer fields) |
| 20, 21 | Sharding & aggregation | `services/aggregator/sharding/assignment.py`, `packages/hammertime-bus` (`AssignmentListener`, `topics.py` incl. `partition_for`), `packages/hammertime-store` (`ShardStateStore`, incl. the shard lease), `services/ingest/publisher.py`, `docs/adr/0001` (Amendments 1, 2, 3), `docs/adr/0004`, `docs/adr/0011`, `docs/adr/0013` (decisions 1, 6, 7) |
| 22 | Consistency model | `docs/adr/0001` (Amendments 1, 2, 3), `docs/adr/0003` (Amendments 2, 3), `docs/adr/0011` (decisions 4, 5), `docs/adr/0013` (decisions 4, 5, 7, 8), `docs/protocol/read-api-v1.md` (`as_of`, `event_sequence`) |
| 23 | Dedup | `services/ingest/dedup`, `docs/adr/0003`, `docs/adr/0004` |
| 24, 25 | Out-of-order, bucket math | `services/aggregator/lateness.py`, `services/aggregator/worker.py`, `core/time/buckets.py`, `docs/adr/0002` (Amendment 1), `docs/adr/0011` |
| 26 | Memory / retention | `services/aggregator/window/store.py`, `packages/hammertime-store` (`ShardStateStore`), `docs/adr/0011` |
| 27 | Trie representation | `services/trie/structure/patricia.py`, `services/trie/structure/arena.py`, `docs/adr/0014` |
| 28 | Atomicity | `services/trie/worker.py` |
| 29 | Read path | `services/trie/query`, `services/detector/api.py`, `docs/protocol/read-api-v1.md`, `docs/adr/0010` |
| 30, 39 | Processing algorithm (aggregator side) | `services/aggregator/worker.py`, `services/aggregator/transitions.py`, `core/state/transitions.py`, `docs/adr/0011` |
| 39 | Processing algorithm (trie update: `add_hot_ip` / `remove_hot_ip` / pruning) | `services/trie/structure/binary_trie.py`, `services/trie/structure/patricia.py`, `docs/adr/0014` |
| 32, 33 | Persistence & snapshots | `services/trie/snapshot`, `packages/hammertime-bus` (`ConsumedMessage.offset`, positional `subscribe`), `tools/provision`, `docs/adr/0010` (Amendment 1), `docs/adr/0013` (decisions 2, 9) |
| 34 | Versioned configuration | `core/config`, `services/aggregator/reevaluate.py`, `docs/adr/0011` |
| 35 | IPv6 readiness | `core/addressing` |
| 36 | Security | `services/ingest/auth` |
| 36.1-36.4 | Agent credentials (hashed tokens, rotation, provisioning) | `services/ingest/auth/agents.py`, `core/auth/tokens.py`, `tools/agent-token`, `schemas/agent_registry.v2.json`, `docs/adr/0006` |
| 36.5-36.7 | Auth throttling, request cost, throttled responses | `services/ingest/auth`, `services/ingest/ratelimit`, `services/ingest/api/routes.py`, `docs/adr/0007`, `docs/adr/0008` |
| 37 | Observability | `core/telemetry`, `services/aggregator/metrics.py`, `deploy/grafana` |
| 43 | Recommended initial implementation; reference components (event log, store, images) and their licence policy | `deploy/docker-compose.yml`, `docs/adr/0012` (Amendment 2), `docs/adr/0013` (decisions 11, 12) |
| 46 | Per-IP attributes (weight, extensibility) | `services/trie/metadata/ip_attributes.py`, `services/aggregator/transitions.py`, `core/state/weight.py`, `core/events` (`attributes.py`: the one §46.2 validator, called by `codec.py` and the trie's record map), `core/config`, `docs/adr/0005`, `docs/adr/0011`, `docs/adr/0015` |
| 46.5 | The derived count invariant `len(records) == hot_count(root)` | `services/trie/structure/invariants.py`, `packages/hammertime-testkit` (`invariants.py`), `services/trie/metadata/ip_attributes.py` (the record map and the coupled step), `docs/adr/0014`, `docs/adr/0015` |
| 47 | Service process lifecycle (entry points, readiness, config reload, shutdown, exit codes, log records) | `core/runtime.py`, `core/telemetry/logging.py`, `services/*/__main__.py`, `services/*/service.py`, `packages/hammertime-store` (`validate_redis_url`), `packages/hammertime-bus` (`nats.py` `bus_endpoints`, the §47.7 reduction of bus URLs for log records), `docs/adr/0009`, `docs/adr/0013` (Amendment 2), `docs/protocol/read-api-v1.md`, `docs/protocol/observation-v1.md` (not-ready 503), `docs/spec/integration-scenarios.md` |
