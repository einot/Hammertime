# ADR 0013 — NATS JetStream as the durable event log, with static shard assignment only

Status: accepted 2026-09-21, confirmed by the repository owner on
2026-09-21; amended 2026-09-21 (see "Amendment 1" at the end — the gaps
that `coder` (brief C1) and `test-author` (brief T1) each hit while working
from this ADR independently are ruled: negative `start_offset`, duplicates
within one `ack()` call, `last_offset` renamed `end_offset` with one meaning
on both buses, the connection-option mechanism and the `nats_client_error`
record, a fetch timeout during an outage, `handled_position` on an unheld
partition and after a commit, `on_assigned(frozenset())`, the settings
field names, the exact `auto` message, and decision 7's Lua under fakeredis.
Every edit is in place with a dated note and is listed there; no decision
changes in substance except decision 9's no-snapshot `start_offset`, which
was wrong for the memory bus); amended again 2026-09-21 (see "Amendment
2" — bus URLs may carry userinfo and no log record may carry it, reduced
by `hammertime.bus.nats.bus_endpoints` and logged as `bus_endpoints`; the
provisioning tool's records and test seams are pinned; assumption 30's
"no sdist for lupa 2.8" is corrected); amended a third time 2026-09-21
(see "Amendment 3" — four points the C2 and T1-followup workers ruled on
alone are ruled: `ack([])` on a consumer closed before it ever subscribed
is a `ValueError`; `bus_endpoints` on an iterable is one-in-one-out, an
empty entry included; the held-lease set is kept apart from the windows
across `stop()` and a `run_maintenance()` after `stop()` does nothing; a
`load()` failure inside `on_assigned` needs no rollback. The pointer edits
Amendment 2 left pending — ADR-0009 A7, spec §47.7,
`integration-scenarios.md` §7 — are made in the same change set); amended
a fourth time 2026-09-22 (see "Amendment 4" — the nine findings of the R1
review of the epic branch are ruled, each in place with a dated note:
`close()` naks the unqueued remainder of a cancelled fetch batch and the
in-flight residual is recorded with its bound; `stop()`'s order is made
total — `handle()`, `run_maintenance()`, `apply_config()` and the periodic
commit all stand down once `stop()` has begun, `run()` hands no message to
`handle()` after the stop signal, `stop()` releases the leases in a
`finally` and runs once; `ensure_streams` is unit-tested against a fake;
`on_assigned` skips a shard only while its lease is held; the `load()`
fault ruling is reaffirmed with the replacement-id consequence stated;
`AggregatorService.run()` reports the failure that stopped it, not a
failing `stop()`; `bus_endpoints` renders an entry with an `@` outside its
authority as `<unparseable>`; the `uv` image is pinned in all five
Dockerfiles; the Redis lease tests widen their TTL to 2 s. Assumption 54
is closed). Epic #95's first reason for the swap — that Kafka's cold start
threatens ADR-0009's 60 s startup deadline — was measured on 2026-09-21 and
does not hold (see Context, prerequisite 5); the epic's own text says the
owner "may wish to revisit the decision" in that case, so the top-level
session reported the measurement and sought confirmation before any
implementation brief was dispatched. The owner confirmed the ADR with
knowledge of prerequisite 5's result ("Confirm ADR-0013, proceed with T1
and C1"), and it proceeds on the remaining reasons and the design gains
under Consequences. Amends
ADR-0001, ADR-0003, ADR-0009, ADR-0010 and ADR-0012 (each carries a dated
amendment pointing here) and supersedes ADR-0011 decision 1, the callback
bullets of decision 5, the commit paragraph of decision 6, the mechanism of
Amendment 6 / A20 and assumptions 1-2 (ADR-0011 Amendment 7).

Scope note: this ADR replaces Apache Kafka with NATS JetStream as the
durable event log behind `hammertime.bus`, settles the revised bus
interface, how shards map onto JetStream subjects and consumers, how the
aggregator's "commit what was handled" requirement is expressed in ack
terms, how overlapping static shard sets are detected (#90), how streams are
provisioned, what the configuration keys now mean, and what the reference
deployment runs. Two things are settled constraints from epic #95 and are
**not** reopened here: shard assignment is static only (no group-managed
`auto` mode and no lease-based assignment protocol), and JetStream KV does
not replace Valkey as the state store (the section "JetStream KV does not
replace Valkey" records why, with the primary-source quotes the epic
required). It does not design the trie or the detector; where it constrains
them (the replay position, redelivery order) it says so under Consequences.

## Context

`hammertime.bus` has two implementations: `memory.py` for tests and
`kafka.py` (aiokafka) for the reference deployment. Every service-level
design so far — ADR-0011's "a shard is a partition; a claim is the consumer
group's assignment", ADR-0001 Amendment 1's ordering argument, ADR-0003
Amendment 2's transport note, ADR-0009's group names and broker healthcheck,
ADR-0012's Kafka/Redpanda drop-in pair — is written against Kafka's
primitives: partitions, consumer groups, rebalances, offset commits. Epic
#95 replaces the broker. Its operational case, as filed:

1. Broker startup versus the `integration` gate (ADR-0012 assumption 14's
   unmeasured 60 s claim; `docker compose up --wait` as re-enable
   condition 3).
2. Configuration surface: fourteen `KAFKA_*` variables plus a hard-coded
   `CLUSTER_ID` in `deploy/docker-compose.yml` versus an image, `-js -sd
   /data`, and a port.
3. A real event sequence number: one monotonic stream sequence, which makes
   spec §33's "event sequence number" an actual number and answers the item
   ADR-0010 deferred.
4. Transport-level deduplication: `Nats-Msg-Id` plus a per-stream duplicate
   window answers #88 without forcing `acks=all`.
5. Nothing is in production: no image in this repository has ever been
   started (ADR-0012 inventory preamble), and ADR-0011 decision 1 records
   that the shard variables "were never read by any shipped build". There
   is no data to migrate and no snapshot format to carry forward.

### Prerequisites, as verified

The epic listed five blocking prerequisites. Their state on 2026-09-21,
with numbers, method and caveats, so that the "prerequisites verified"
acceptance criterion is met by this text:

1. **`nats-py` licence.** PyPI JSON for nats-py 2.16.0 (`https://pypi.org/pypi/nats-py/json`,
   read by the top-level session on 2026-09-21 and by the architect the same
   day): `license_expression = "Apache-2.0"`, `license_files = ["LICENSE"]`,
   and the legacy `license` field is `null` — a tool that reads only the old
   field reports "no licence"; the licence is carried by `license_expression`.
   (The architect's fetch was summarised by a tool that reported
   `license: "Apache-2.0"`; the top-level session's direct read of `null`
   is the one recorded, and the Class 2 inventory row in ADR-0012 says so.)
2. **JetStream KV per-key TTL is create-only, and KV has no
   read-after-write consistency.** Verified by the epic against NATS ADR-8
   and re-read by the architect on 2026-09-21 from
   `https://raw.githubusercontent.com/nats-io/nats-architecture-and-design/main/adr/ADR-8.md`
   ("JetStream based Key-Value Stores", 2021-06-30, status Implemented):
   *"When using the `Create()` behavior and the `allow_msg_ttl` setting is
   enabled on the Bucket clients can accept a duration for how long the
   created value should stay in the bucket. We cannot accept this on the
   Put operation as that might have the effect of surfacing history once
   the latest value is removed using Per-Message TTLs."* and *"We do not
   provide read-after-write consistency. Reads are performed directly to
   any replica, including out of date ones. If those replicas do not catch
   up multiple reads of the same key can give different values between
   reads."* ADR-48 ("TTL Support for Key-Value Buckets", 2025-04-09,
   Implemented, tag 2.11), which updates ADR-8, keeps the restriction: *"Do
   not accept a TTL for other API. Some are currently undefined, and some
   are understood to create improper state. For instance a TTL on `Put()`
   might mean older revisions could come back from the dead once the TTL
   expires."* — only `Create()` and `Purge()` take a TTL. See "JetStream KV
   does not replace Valkey".
3. **CNCF/Synadia settlement — NOT primary-verified.** `www.cncf.io`
   (`https://www.cncf.io/announcements/2025/05/01/cncf-and-synadia-align-on-securing-the-future-of-the-nats-io-project/`),
   `www.synadia.com`, `www.linuxfoundation.org`, `lists.cncf.io`,
   `docs.nats.io`, `nats.io`, `nats-io.github.io` and `web.archive.org` are
   blocked by this environment's egress policy (the architect's own
   `WebFetch` of the CNCF URL returned `EGRESS_BLOCKED` on 2026-09-21).
   Two primary facts were verified from `raw.githubusercontent.com`: the
   `nats-io/nats-server` `main` branch `LICENSE` is the Apache License,
   Version 2.0 (read by the top-level session and again by the architect
   on 2026-09-21: "Apache License / Version 2.0, January 2004"), and the
   same repository's `README.md` line 5 states "NATS is part of the Cloud
   Native Computing Foundation ([CNCF](https://cncf.io))" (top-level
   session, 2026-09-21). Neither establishes who holds the NATS trademarks
   or that the 2025 BUSL relicensing proposal was withdrawn. **The terms of
   the settlement — trademarks assigned to the Linux Foundation, the
   nats.io domain and GitHub repositories with CNCF, Apache-2.0 continuing,
   the relicensing withdrawn — are recorded as an assumption pending primary
   verification (assumption 1), the CNCF announcement above is the source to
   check, and ADR-0012's compliance verdict for `nats` is explicitly
   conditional on it (ADR-0012 Amendment 2).**
4. **Cost of 128 subject-filtered durable consumers on one stream —
   measured** by the top-level session, 2026-09-21, in the Claude Code
   remote environment (4 vCPU, 16 GiB, x86_64, Linux 6.18). Method:
   nats-server v2.15.0 built from source with Go 1.24.7 (`go install
   github.com/nats-io/nats-server/v2@latest`), single node, `-js`, file
   storage, default limits; client nats-py 2.16.0; one stream
   `hammertime-observations` with subjects `hammertime.observations.v1.*`,
   `duplicate_window = 120 s`; 128 durable pull consumers, one per subject
   `hammertime.observations.v1.{0..127}`, `AckPolicy.EXPLICIT`,
   `DeliverPolicy.ALL`, `max_ack_pending = 1000`; 128 000 messages of
   300 bytes published round-robin across the 128 subjects with
   `Nats-Msg-Id` headers and synchronous publish acks (500 in flight); then
   all 128 consumers drained concurrently with `fetch(100)` and per-message
   `ack()`. Results:
   * Server RSS (`/varz` `mem`): 16.5 MiB idle -> 17.9 MiB after stream
     creation -> 25.5 MiB after 128 consumers -> 68.4 MiB after 128 000
     messages stored -> 130.0 MiB during/after the concurrent drain ->
     33.4 MiB idle again seven minutes later.
   * **About 61 KiB of server RSS per idle filtered consumer.** Creating
     all 128 took 0.125 s.
   * Publish: 7.20 s for 128 000 messages, about 17 800 msg/s with sync
     acks and dedup headers.
   * Consume: 6.14 s for 128 000 messages across 128 concurrent pull
     consumers, about 20 800 msg/s; every consumer received exactly its
     1 000 messages (no cross-subject leakage).
   * Dedup: republishing 100 already-seen `Nats-Msg-Id` values inside the
     duplicate window added 0 messages to the stream.
   * Stream storage on disk after 128 000 x 300 B: 50.7 MB.
   * nats-server cold start to `/healthz?js-enabled-only=true` answering
     200: **0.08 s**.
   Conclusion: 128 filtered consumers on one stream is cheap on a single
   node; the partition-count ceiling is not constrained by per-consumer
   memory. Caveat: single node, no replication (R=1), no TLS, no auth, one
   client connection, loopback; a clustered R=3 deployment will differ.
5. **Kafka cold start against ADR-0009's 60 s deadline — measured, and it
   comfortably clears it.** Same session and host as item 4. Apache Kafka
   4.3.1 (`kafka_2.13-4.3.1.tgz` from `archive.apache.org`, SHA-512
   verified) on OpenJDK 21.0.10, run directly on the host (no Docker daemon
   is available there), KRaft combined mode with a `kraft.properties`
   mirroring every setting of `deploy/docker-compose.yml`'s `broker`
   service (same `CLUSTER_ID`, listeners, replication factors, min ISR).
   Three cold runs, each from a freshly formatted log dir:

   | run | `kafka-storage format` | `kafka-server-start` -> "Kafka Server started" | total from format start |
   | --- | --- | --- | --- |
   | 1 | 2.54 s | 3.28 s | 5.82 s |
   | 2 | 2.04 s | 3.27 s | 5.31 s |
   | 3 | 1.94 s | 3.16 s | 5.11 s |

   After the "started" line, `kafka-broker-api-versions.sh` succeeded on
   the first attempt every run (the ~2 s it took is the tool's own JVM
   launch). Caveats: bare JVM on a 4 vCPU/16 GiB host, not the
   `apache/kafka:4.3.1` image under `docker compose`; image pull time
   excluded; CI runners are typically 2 vCPU. Even with a generous 3x
   container/CI penalty this is about 15-20 s, a 3-4x margin under 60 s.
   **Consequence: the epic's first "Why" bullet (ADR-0012 assumption 14's
   unmeasured 60 s claim) is now measured and does not, on its own, justify
   the swap.** The operational case rests on the remaining reasons —
   configuration surface (item 2), a real event sequence number (item 3),
   transport-level deduplication answering #88 (item 4), and nothing being
   in production (item 5) — and on the design gains recorded under
   Consequences (one integer replay position, one exception type per
   failure class, dedup that makes every publish retry safe). Whether those
   are worth the loss of the broker drop-in and of `auto` assignment is the
   owner's call; this ADR is the cheapest form in which to make it.

### What the bus interface cannot express on JetStream

Read against `packages/hammertime-bus/src/hammertime/bus/interface.py`,
`memory.py` and `kafka.py`, the aggregator's `service.py`,
`sharding/assignment.py`, `worker.py` and `config.py`, and ingest's `app.py`
and `config.py` (all at the current `master`):

* `Consumer.commit(offsets)` (`interface.py:146-166`): JetStream has no
  offset-commit API; progress is a per-message acknowledgement on the
  delivered message's reply subject. `ConsumedMessage` is frozen/slots
  with nowhere to carry an ack handle.
* `Consumer.seek(topic, partition, offset)` (`interface.py:138-144`): a
  consumer's `deliver_policy`/`opt_start_seq` are fixed at creation
  (nats.docs consumers page: `DeliverPolicy` is not editable; Sources), so
  seeking means creating a new consumer and invalidating the iterator
  `subscribe()` already returned. There are no production callers of
  `seek()` — the trie worker, snapshot writer and `tools/replay` are stubs.
* `AssignmentListener.on_revoked` (`interface.py:44-46`): promises "called
  before the broker moves them away". No such event exists: nothing moves
  a subject-filtered consumer between clients. Today a commit from a
  consumer that lost its partition raises `IllegalStateError`
  (`kafka.py:256`); under JetStream that ack would be accepted silently.
* `subscribe(partitions=None)`: group-managed assignment has no backend
  under static-only.
* `AckWait` redelivery: JetStream redelivers an unacknowledged message
  after `ack_wait` — to the same live consumer — which ADR-0003 Amendment
  2 explicitly assumed never happens ("a byte-identical message handed to
  `handle()` twice within one live claim is not a supported input").
* Stream provisioning: JetStream has no stream auto-creation; a publish to
  a subject no stream captures gets no acknowledgement and nats.py raises
  `NoStreamResponseError` after its timeout (Sources). The compose stack
  today relies on Kafka auto-creating topics.
* The IP -> shard hash: JetStream routes by subject, not by key. Either a
  server-side subject transform owns the hash (`{{partition(n, ...)}}`,
  load-bearing broker configuration outside this repository's diff
  surface, and an algorithm the NATS ADR describes only as "deterministic
  hashing" — Sources), or the publisher computes it, which ADR-0011
  assumption 1 rejected for Kafka.

## Decision

### 1. One JetStream stream per topic; a partition is a subject; the hash lives in `hammertime.bus`

Every `TopicSpec` in `hammertime.bus.topics` maps to exactly one JetStream
stream whose name is the topic name with every `.` replaced by `-`
(JetStream stream and consumer names may not contain `.`, `*`, `>`, `/`
or `\`; NATS ADR-6, Sources), and whose single subject filter is
`<topic>.*`:

| topic (`TopicSpec.name`) | stream name | subjects | partitions |
| --- | --- | --- | --- |
| `hammertime.observations.v1` | `hammertime-observations-v1` | `hammertime.observations.v1.*` | 128 |
| `hammertime.observations-reconciliation.v1` | `hammertime-observations-reconciliation-v1` | `hammertime.observations-reconciliation.v1.*` | 8 |
| `hammertime.hot-ip.v1` | `hammertime-hot-ip-v1` | `hammertime.hot-ip.v1.*` | 32 |
| `hammertime.prefix-stats.v1` | `hammertime-prefix-stats-v1` | `hammertime.prefix-stats.v1.*` | 4 |

**A partition `p` of topic `T` is the subject `T.<p>`**, `0 <= p <
TopicSpec.partitions`. The word "partition" is kept throughout the bus
interface, the aggregator and the ADRs: it is the integer a message is
stored under, the unit of static ownership, and what `ConsumedMessage.partition`
reports. The partition counts in `topics.py` are unchanged (128 for the
observations topic remains the shard count and the parallelism ceiling of
ADR-0011 Amendment 1, A1; the other three are unchanged because nothing here
needs them changed — assumption 5).

**The hash is in this repository.** `hammertime.bus.topics` gains

```python
def partition_for(key: bytes | str, partitions: int) -> int:
    """FNV-1a 32-bit over the UTF-8 bytes of `key`, modulo `partitions`."""
```

and every `Producer` implementation routes `publish(topic, key, value)` to
partition `partition_for(key, TOPICS[topic].partitions)`. `TopicSpec` gains
`stream_name` (property), `subject(partition: int) -> str` and
`subject_filter` (property, `f"{name}.*"`). *It also gains the inverse,
`partition_of(subject: str) -> int | None` (added 2026-09-22, Amendment 4
ruling S3): `p` when `subject == self.subject(p)` for some `p >= 0` — the
canonical decimal form, so `<name>.7` is `7` and `<name>.07`, `<name>.-1`,
`<name>.x`, `<name>.7.8` and `<name>` are all `None` — and nothing else;
it is what `NatsConsumer` uses to read a delivered message's partition
from its subject, and a `None` is a malformed message at the transport
(decision 5, as amended).* ADR-0011 assumption 1 rejected
an in-repo hash for Kafka because the broker's client partitioner already
did the job and a second implementation was "risk without functional gain";
on JetStream someone has to compute the subject, and the alternative — a
server-side `subject_transform` on the stream using `{{partition(128,1)}}`
— was rejected because (i) it makes the IP -> shard mapping a property of
broker configuration that no unit test, tool or reviewer in this repository
can see; (ii) the NATS ADR on subject transforms specifies only "a
deterministic hashing of the value of wildcard-tokens" and the server's
implementation (FNV-1a 32-bit, `server/subject_transform.go`, Sources)
carries no compatibility comment, so the mapping would rest on an
undocumented contract; and (iii) a stream transform runs after the subject
is published, so the publisher would still have to publish to a
per-message subject. FNV-1a 32-bit was chosen so that the in-repo mapping
coincides with what the server's own function would produce for a
single-token key, should a deployment ever want a server-side transform to
reproduce it (assumption 4).

Consequences for identity: nothing about `event_id`, `agent_id` or
`sequence` changes. An IP's shard is `partition_for(str(ip), 128)`, stable
for the life of the deployment for the reason ADR-0011 A1 gives (changing
the count remaps every IP).

### 2. Streams are provisioned before any service starts; a missing stream is a transient startup failure

JetStream does not create streams on publish. Provisioning is therefore a
day-one step that runs **before** the services, and it is idempotent:

* `hammertime.bus.nats` exposes `stream_config_for(spec: TopicSpec, *,
  replicas: int = 1) -> nats.js.api.StreamConfig` and `async def
  ensure_streams(js: JetStreamContext, specs: Iterable[TopicSpec], *,
  replicas: int = 1) -> dict[str, str]` (stream name -> `"created" |
  "updated" | "unchanged"`). `ensure_streams` calls `stream_info(name)`;
  on `NotFoundError` it calls `add_stream`; when the stream exists it
  compares the mutable fields (`subjects`, `max_age`, `duplicate_window`,
  `max_bytes`, `max_msgs`, `num_replicas`) and calls `update_stream` if
  any differs; a difference in an immutable field (`retention`, `storage`)
  raises `StreamConfigConflictError` and changes nothing. It inspects every
  stream in `specs` before it creates or updates any of them, so a conflict
  on one stream changes nothing on any other either (amended 2026-09-21,
  Amendment 1 ruling C9). *What is compared, and that it is unit-tested
  (amended 2026-09-22, Amendment 4 ruling R3): `ensure_streams` reads
  `stream_info(name).config` — the `nats.js.api.StreamConfig` nats-py builds
  from the server's response, in which durations are seconds and the
  enum-valued fields (`retention`, `storage`, `discard`) are left as the
  server's strings rather than converted to the enums (nats-py
  `StreamConfig.from_response`, read from `main`; Sources) — against
  `stream_config_for(spec, replicas=replicas)`, field by field, after
  normalising both sides: an `Enum` by its `.value`, a `list` by its
  elements in order, anything else as it is; so a stream the server reports
  with `retention="limits"` equals one declared with
  `RetentionPolicy.LIMITS`, and the comparison does not depend on which
  form the installed nats-py produces. A missing stream is
  `nats.js.errors.NotFoundError` from `stream_info` and nothing else;
  `add_stream` and `update_stream` each receive the declared `StreamConfig`
  whole; the result has one entry per spec. `stream_config_for` raises
  `ValueError` for `replicas < 1`. None of this needs a broker: the
  no-broker exemption of `hammertime.bus.nats` covers `NatsBus`,
  `NatsProducer` and `NatsConsumer`, which need a live connection, and not
  `stream_config_for`, `ensure_streams`, `bus_endpoints` or
  `validate_bus_url`, which are pure over their arguments — a fake
  `JetStreamContext` exposing `stream_info`, `add_stream` and
  `update_stream` is the whole harness, and the tests live in the bus
  package's `tests/test_streams.py`.*
* A new CLI, `hammertime-provision` (package `tools/provision`, module
  `hammertime.tools.provision`, docstring `Spec: section 19, section 32,
  section 33`), runs `ensure_streams` over `all_topics()`: `hammertime-provision
  --servers <urls> [--replicas N] [--timeout S]`; exit 0 when every stream
  exists with the declared configuration, 1 when the servers are
  unreachable within `--timeout` (default 60 s, retried with ADR-0009 A1's
  schedule) or a stream conflicts, 2 on invalid arguments. It logs one
  `INFO event=stream_provisioned stream=<name> action=<created|updated|unchanged>`
  per stream through `configure_logging("provision", level)`.
  *(Added 2026-09-21, Amendment 2 — the tool's surface, records and test
  seams, so that its tests are written from this text.)* The module
  `hammertime.tools.provision.__main__` exposes `build_parser() ->
  argparse.ArgumentParser`, `async def provision(servers: list[str], *,
  replicas: int, timeout_s: float) -> dict[str, str]` (stream name ->
  action; raises the last transient error when `timeout_s` expires,
  `StreamConfigConflictError` on an immutable difference, any other
  `nats.errors.Error` as itself), `main(argv: Sequence[str] | None = None)
  -> int`, `TRANSIENT_PROVISION_ERRORS` (a superset of
  `hammertime.bus.nats.TRANSIENT_ERRORS` that adds
  `nats.js.errors.ServiceUnavailableError` — a server whose JetStream is
  not yet serving is "unreachable" for provisioning), `DEFAULT_REPLICAS =
  1` and `DEFAULT_TIMEOUT_S = 60.0`. `--servers` is comma-separated,
  entries stripped, empties dropped, at least one required; `--replicas`
  MUST be a positive integer and `--timeout` a positive number; an invalid
  invocation is argparse's `SystemExit(2)`, and an invalid
  `HAMMERTIME_LOG_LEVEL` is a `config_invalid` record and a returned 2.
  Its records, all through the logger configured by `main()`: `ERROR
  stream_conflict stream=<name> field=<f> expected=<e> actual=<a>` (exit
  1); `ERROR provision_failed reason=unreachable bus_endpoints=<list>
  timeout_s=<s> error=<str>` when the deadline expired (exit 1); `ERROR
  provision_failed reason=broker_error bus_endpoints=<list> error=<str>`
  for any other `nats.errors.Error` (exit 1). **The `--servers` value never
  appears in a record: every record that names the servers carries
  `bus_endpoints = hammertime.bus.nats.bus_endpoints(servers)` (decision 3)
  and nothing else derived from the value**, because a NATS URL may carry
  a password or a token in its userinfo (decision 10, as amended). Test
  seams: `main()` calls `provision` by looking it up on its own module at
  call time; `provision` connects with `nats.connect(servers=<the list, as
  given>, ...)` looked up on the `nats` module at call time and reconciles
  with `ensure_streams`, imported by name into the tool's module — so a
  test replaces `hammertime.tools.provision.__main__.provision`,
  `nats.connect` and `hammertime.tools.provision.__main__.ensure_streams`
  respectively, and no test needs a broker. *Every `--servers` entry is
  checked with `hammertime.bus.nats.validate_bus_url` (decision 3, as
  amended 2026-09-22, Amendment 4 ruling S2) before anything is connected;
  an entry it refuses is an argparse error — `SystemExit(2)`, a message
  naming the entry's position and not its text — so a URL nats-py could
  not parse never reaches nats-py or a record.*
* The reference deployment runs it as a one-shot compose service
  (`provision`) that depends on the broker being healthy, and every
  application service depends on `provision` having completed successfully
  (decision 11).
* **At startup a service verifies every registered stream exists.**
  `NatsBus.start()` connects and calls `stream_info` for every
  `TopicSpec` in `TOPICS`; a missing one raises
  `hammertime.bus.nats.StreamNotProvisionedError`, which is a member of
  `hammertime.bus.nats.TRANSIENT_ERRORS`, so a service started before the
  provisioner has finished retries under ADR-0009's startup deadline with
  `dependency_unavailable dependency=bus` records and, if the stream never
  appears, exits 1 with `start_failed` — the same shape as a broker that is
  not up yet. Nothing in a service ever creates a stream.
* Consumers are **not** provisioned by the tool: a durable consumer is
  created, or bound if it already exists, by `Consumer.subscribe()`
  (decision 5). The tool does not know which member owns which shard.

Stream configuration (every value is an assumption unless a prior ADR fixes
it; see assumptions 6-9):

```text
name              <stream name of decision 1>
subjects          ["<topic>.*"]
retention         limits
max_age           TopicSpec.retention_seconds   (1 d, 7 d, 30 d, 1 d)
max_bytes         -1 (unbounded)                 max_msgs -1   max_msgs_per_subject -1
discard           old
storage           file
num_replicas      1 (the --replicas flag of hammertime-provision; 3 in a clustered deployment)
duplicate_window  120 s
allow_direct      false     deny_delete false     deny_purge false     subject_transform none
```

### 3. The `hammertime.bus` interface

The revised `hammertime.bus.interface`, complete. `test-author` writes the
bus package's tests from this block and the semantics below alone.

```python
# hammertime.bus.interface   (Spec: section 19, section 32, section 33)
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

@dataclass(frozen=True, slots=True)
class ConsumedMessage:
    topic: str
    partition: int          # the integer suffix of the subject the message is stored under
    offset: int             # the stream sequence: unique and strictly increasing across the whole
                            # topic, not contiguous per partition (InMemoryBus: the log index)
    key: bytes | None
    value: bytes
    delivery_count: int = 1 # JetStream num_delivered; always 1 on InMemoryBus

@runtime_checkable
class AssignmentListener(Protocol):
    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None: ...

@runtime_checkable
class Producer(Protocol):
    async def publish(self, topic: str, key: bytes | str, value: bytes,
                      *, message_id: str | None = None) -> None: ...
    async def flush(self) -> None: ...

@runtime_checkable
class Consumer(Protocol):
    async def subscribe(self, topic: str, *,
                        partitions: Iterable[int] | None = None,
                        listener: AssignmentListener | None = None,
                        start_offset: int | None = None,
                        ) -> AsyncIterator[ConsumedMessage]: ...
    async def ack(self, messages: Iterable[ConsumedMessage]) -> None: ...
    async def close(self) -> None: ...

@runtime_checkable
class MessageBus(Protocol):         # moved here from hammertime.aggregator.worker
    def producer(self) -> Producer: ...
    def consumer(self, group_id: str) -> Consumer: ...
    async def end_offset(self, topic: str) -> int: ...   # amended 2026-09-21: the log end, see decision 9

def static_partitions(topic: str, partitions: Iterable[int]) -> frozenset[int]: ...   # unchanged (ADR-0011 A3)
```

Removed: `Consumer.seek`, `Consumer.commit`, `AssignmentListener.on_revoked`.
Added: `message_id` on `publish`, `start_offset` on `subscribe`,
`Consumer.ack`, `Consumer.close`, `ConsumedMessage.delivery_count`,
`MessageBus` (with `end_offset`, amended 2026-09-21 — it was `last_offset`
and off the protocol; Amendment 1 ruling C3), `partition_for` (decision 1).

**`Producer.publish(topic, key, value, *, message_id=None)`** appends
`value` to partition `partition_for(key, TOPICS[topic].partitions)` of
`topic` and returns only once the log has durably acknowledged it (this is
what ADR-0011 decision 4 step 4 and ADR-0009 A11 rely on: a returned
`publish` is a flushed publish). With `message_id`, the log deduplicates:
a second publish to the same topic carrying an id the log has seen inside
its duplicate window is acknowledged and **not** appended, and `publish`
returns normally. With `message_id=None` no deduplication is attempted.
`key` is `str` or `bytes`; a `str` is encoded as UTF-8 before hashing and
before it becomes the message key. An unregistered topic is a `KeyError`
from `NatsProducer` (there is no stream to publish to); `MemoryProducer`
creates topics on demand as today.

**`Producer.flush()`** returns once every `publish` that has returned has
been acknowledged — a no-op in both implementations, retained because
ADR-0009 decision 7's flush-before-commit rule is stated against the
interface, not the implementations (ADR-0009 A11).

**`Consumer.subscribe(topic, *, partitions=None, listener=None,
start_offset=None)`** — exactly one call per `Consumer` instance; a second
call raises `RuntimeError`. It returns an `AsyncIterator[ConsumedMessage]`
that yields messages of the subscribed partitions in stream-sequence order
(within a partition; across partitions of one topic the interleaving is
unspecified) and ends (raises `StopAsyncIteration`) after `close()`.

* `partitions=None` means **every partition of the topic** — on
  `NatsConsumer` one whole-topic durable (the detector's shape, decision
  5), on `InMemoryBus` `{0}` (the test harness's shape). There is no
  group-managed mode. The aggregator in production always passes an
  explicit set (decision 6). `partitions=<iterable>` means exactly those
  partitions; an empty iterable is a `ValueError` raised before any broker
  is contacted and before `listener` is called (ADR-0011 A3, unchanged);
  `InMemoryBus` has one partition per topic, so any static set other than
  `{0}` is a `ValueError` there (unchanged), and `partitions=None` on it
  is `{0}`. `NatsConsumer` accepts any non-negative partition (a subject
  that never receives a message is simply empty).
* When `listener` is given, `subscribe()` awaits
  `listener.on_assigned(frozenset((topic, p) for p in <the set>))` exactly
  once — with the full static set, or for `partitions=None` with every
  partition of a registered topic (`{0}` on `InMemoryBus`) — before it
  returns; that is what keeps ADR-0009's "shard claims held" readiness
  observable at the end of `start()`. If `on_assigned` raises, `subscribe()`
  propagates the exception and holds nothing: the consumer is left as if
  `subscribe()` had never been called, and durable consumers created on the
  broker in the meantime are left in place (they carry no ownership).
* `start_offset=None` is a **durable subscription**: the read position is
  kept by the log under `group_id` (decision 5) and advanced only by
  `ack()`. A fresh `Consumer` for the same group and partitions is
  delivered every message that was never acknowledged, in order, however
  many earlier consumers read it. `start_offset=<int>` is a **positional
  subscription**: delivery starts at the first message whose `offset >=
  start_offset` (at the first retained message if that sequence is no
  longer in the log), no position is kept anywhere, and `ack()` on it is a
  `ValueError`. The trie replays from its snapshot this way (decision 9);
  `tools/replay` will too. A positional subscription may still name
  `partitions` and a `listener`. `start_offset` MUST be `>= 0`: a negative
  value is a `ValueError` raised before any broker is contacted and before
  `listener` is called, on both implementations (amended 2026-09-21,
  Amendment 1 rulings C7 and T2). `start_offset=0` is the portable "from
  the first retained message": it is the first index of the memory log,
  and it is below every JetStream sequence (they start at 1), so
  `NatsConsumer` passes `opt_start_seq = max(start_offset, 1)`. "Below the
  first offset" therefore cannot be exercised with a negative number; on
  `InMemoryBus`, which never discards, it cannot be exercised at all.
* An unregistered topic is a `KeyError` from `NatsConsumer.subscribe()`
  (there is no stream to bind a consumer on), raised with the other
  argument checks before the broker is contacted and before `listener` is
  called; `MemoryConsumer` subscribes to any topic, which exists from its
  first publish (amended 2026-09-21, ruling C6).

**`Consumer.ack(messages)`** acknowledges exactly the given messages, each
of which MUST have been delivered by this consumer instance under its
durable subscription and not yet acknowledged; anything else — a message
of another topic or partition, one this instance never delivered, one it
already acknowledged, or any message on a positional subscription — is a
`ValueError` from both implementations, raised before anything is
acknowledged (the whole iterable is checked first, so a rejected call
acknowledges nothing). An empty iterable returns normally without touching
the broker. *(Amended 2026-09-21, Amendment 1 rulings C4, C5 and T1.)* The
checks are ordered: a closed consumer is a `ValueError` and a positional
subscription is a `ValueError` whatever the iterable holds, an empty one
included; only a live durable subscription — or an instance that has not
subscribed yet, which has delivered nothing and so accepts exactly the
empty iterable — returns normally on an empty iterable. A message named
more than once in one call is acknowledged once and is not an error:
`ConsumedMessage` is a value, the identity of an acknowledgement is
`(topic, partition, offset)`, and "already acknowledged" means acknowledged
by an *earlier* call. That is what lets `commit_handled()` (decision 8)
hand `ack()` the handled list as it is, a `REDELIVERED` copy alongside its
original, without deduplicating; on `NatsConsumer` the one retained `Msg`
for that key (the most recent delivery) is acknowledged, and only once, so
nats.py's `MsgAlreadyAckdError` is never reached. *Closed is checked first
and is absorbing (amended 2026-09-21, Amendment 3 ruling (a)): an instance
whose `close()` ran before it ever subscribed is a closed consumer, not a
not-yet-subscribed one, and `ack()` on it is a `ValueError` for any
iterable, the empty one included; the not-yet-subscribed clause above
describes an instance that is neither closed nor subscribed.* Acknowledging is what advances the group's position: it is the
only "commit" there is, and it is per message, so a caller acknowledges
what it has handled and nothing else — which is exactly the requirement of
ADR-0011 Amendment 6 / A20, now expressed without an offset. `NatsConsumer`
waits for the server to confirm each ack (`ack_sync`), so an `ack()` that
has returned is durable.

**`Consumer.close()`** stops delivery, makes the iterator end, and gives
back every message this instance delivered under a durable subscription
and did not acknowledge, so that the next consumer for the group receives
it without waiting: `NatsConsumer` sends a negative acknowledgement
(`nak`) for each such message, then unsubscribes; `MemoryConsumer` has
nothing to send (an unacknowledged message is already deliverable to the
next consumer) and only marks itself closed. `close()` is idempotent and
safe before `subscribe()`. After `close()`, `ack()` is a `ValueError`.
*What "every message this instance delivered" reaches, and what it does
not (amended 2026-09-22, Amendment 4 ruling R1): it is every nats-py `Msg`
a fetch loop received — one yielded and unacknowledged (retained), one
sitting in the in-process queue, and one of a fetched batch that the loop
had not yet queued when `close()` cancelled it; `_pump_pull` keeps the
unqueued remainder of its current batch on cancellation (a `Msg` whose
`put` was interrupted is in the remainder, one whose `put` returned is in
the queue, never both), and `close()` naks the three sets once each,
after unsubscribing and before the final flush, as before. What `close()`
cannot reach is a message the server delivered against the loop's last
outstanding pull request that never reached the loop: in flight on the
wire, or in nats-py's private per-subscription pending queue, which only
a further `fetch()` drains and which no public call empties (Sources).
Those surface at the next consumer only after `ack_wait` (30 s), and there
are at most `FETCH_BATCH` (100) of them per durable, because one pull asks
for at most that many. This is a recorded residual: the "without waiting"
above holds for what reached the client's loops, and a clean handover pays
up to `ack_wait` for the rest. The integration job (#52) is where the bound
is measured; no unit test can reach it.*

**`ConsumedMessage`** keeps its five fields with the meanings above and
gains `delivery_count` (default 1). It carries no acknowledgement handle:
each `Consumer` implementation retains, per delivered and unacknowledged
`(topic, partition, offset)`, whatever it needs to acknowledge it (the
nats.py `Msg`; nothing at all for memory), keeping the most recent delivery
when the log redelivers. The dataclass stays frozen and `slots=True`, and
every existing construction with the five keyword fields remains valid.

**`hammertime.bus.nats`** (new module, docstring `Spec: section 19, section
20, section 32, section 33`):

```python
class NatsBus:                                   # satisfies MessageBus
    def __init__(self, servers: str) -> None     # HAMMERTIME_BUS_BROKERS: comma-separated nats:// URLs
    async def start(self) -> None                # connect; verify every registered stream exists
    async def close(self) -> None                # close every consumer (nak unacked), drain the connection
    def producer(self) -> Producer               # one NatsProducer over the shared connection
    def consumer(self, group_id: str) -> Consumer   # a new NatsConsumer over the shared connection
    async def end_offset(self, topic: str) -> int   # stream_info(...).state.last_seq + 1 (amended 2026-09-21)

def bus_endpoints(servers: str | Iterable[str]) -> list[str]:   # added 2026-09-21, Amendment 2
    """`<scheme>://<host>[:<port>]` per server URL; userinfo, path, query and fragment dropped."""

def validate_bus_url(url: str) -> None:   # added 2026-09-22, Amendment 4 ruling S2
    """ValueError, naming nothing of `url`, if nats-py's own parse of it would fail or echo it."""
```

**`bus_endpoints(servers)`** *(added 2026-09-21, Amendment 2)* is the one
reduction of a server list to something a log record may carry, and it is
re-exported from `hammertime.bus`. A `str` argument is split exactly as
`NatsBus.__init__` splits `HAMMERTIME_BUS_BROKERS` — on `,`, entries
stripped, empties dropped — and an iterable is taken entry by entry, each
stripped. *One result element per input element (amended 2026-09-21,
Amendment 3 ruling (b)): an iterable entry that is empty after stripping
is not dropped — it takes the `nats://` prefix like any other scheme-less
entry and renders as `nats://`, the scheme over an empty authority —
because only the `str` form's split is Hammertime's own; a caller that
passes a list has already decided what its entries are, and the record
shows as many endpoints as the caller passed.* An entry containing no `://` is read as `nats://<entry>` first
(nats-py's own normalisation of a scheme-less server: Sources, Amendment
2). Each entry is then `urlsplit`, and the result is `<scheme>://` followed
by the part of the authority after its **last** `@` — the whole authority
when there is no `@`. Nothing to the left of that `@` reaches the output:
not a password, and not a username either, because nats-py reads a
username with no password as an auth token (`nats://token@host:4222`;
Sources, Amendment 2). Path, query and fragment are dropped (they carry
nothing NATS uses). The function never raises: an entry `urlsplit` refuses
(an unbalanced `[`, `ValueError: Invalid IPv6 URL`) is rendered as the
fixed string `<unparseable>`, with nothing derived from the entry; an
empty list is `[]`. It does not consult `SplitResult.port`, which raises a
`ValueError` echoing the text it could not parse (the hazard ADR-0009
A12 item 4 records for `redis://user:secret/0`). Examples, which tests
pin: `nats://nats:4222` -> `nats://nats:4222`;
`nats://user:s3cret@nats:4222` -> `nats://nats:4222`;
`nats://s3cret-token@nats:4222` -> `nats://nats:4222`;
`tls://user:s3cret@[::1]:4222/?x=1` -> `tls://[::1]:4222`;
`user:s3cret@nats:4222` -> `nats://nats:4222`;
`"nats://a:4222, nats://u:p@b:4222,"` -> `["nats://a:4222",
"nats://b:4222"]`; `nats://[::1` -> `<unparseable>`. Every log record that
names the bus servers carries this list under the field name
`bus_endpoints` — the services' `starting` record (in place of
`bus_brokers`; ADR-0009 A7's row is superseded, Amendment 2) and the
provisioning tool's `provision_failed` records (decision 2) — and no
record of any event carries `HAMMERTIME_BUS_BROKERS` or `--servers`
verbatim or in any other derived form.

*An `@` outside the authority (amended 2026-09-22, Amendment 4 rulings R8
and S2). `urlsplit` ends the authority at the first `/`, `?` or `#` after
the `//` (CPython `urlsplit`, Sources), so a userinfo containing one of
them unencoded leaves its `@` — and a fragment of the password — outside
the authority, where the last-`@` rule cannot see it:
`nats://user:pa/ss@nats:4222` splits into authority `user:pa` and path
`/ss@nats:4222`, and the rule as written would render `nats://user:pa`.
Ruled: an entry in which `urlsplit` leaves an `@` in the path, query or
fragment is rendered as the fixed string `<unparseable>`, whatever its
authority holds. An `@` there has no meaning in a NATS URL, it is the
signature of exactly that mistake, and a malformed value is the input a
redaction cannot be trusted on (assumption 44). Additional examples,
which tests pin: `nats://user:pa/ss@nats:4222` -> `<unparseable>`;
`nats://user:pa?ss@nats:4222` -> `<unparseable>`;
`nats://user:pa#ss@nats:4222` -> `<unparseable>`;
`nats://user:p@/ss@nats:4222` -> `<unparseable>` (an `@` inside the
authority and another outside it); `nats://nats:4222/` ->
`nats://nats:4222` (a path without an `@` is still merely dropped). The
seven examples above are unchanged.*

*The same URL reaches a record by a second path, closed by
`validate_bus_url` (Amendment 4 ruling S2, on the S1 audit's finding 2).
nats-py 2.16.0's `_parse_server_uri` reads `urlparse(...).port`, which
raises `ValueError("Port could not be cast to integer value as 'pa'")` —
the port token echoed — and nats-py wraps it in
`nats.errors.Error("nats: invalid connect url option")` without `from
None`, so the `ValueError` is the chained `__context__`; `run_service`
logs `start_failed` with `exc_info=True`, and structlog's
`format_exc_info` prints the chain, token included, into the `exception`
field. So `hammertime.bus.nats` gains `validate_bus_url(url: str) ->
None`, re-exported from `hammertime.bus`, which normalises the entry as
nats-py does (an entry containing `://` as given; otherwise
`nats://<entry>`) and raises `ValueError` when (i) `urlsplit` refuses it,
(ii) `SplitResult.port` raises — the very cast nats-py performs — or (iii)
an `@` is left outside the authority (the case above); the message names
none of the entry's text. `load_settings` in the aggregator and in ingest
calls it for every entry of `HAMMERTIME_BUS_BROKERS` (split as
`NatsBus.__init__` splits it) and turns a refusal into a `ValueError`
naming the variable and the entry's position, so the process exits 2
with `config_invalid` before nats-py sees the value — the store side's
`validate_redis_url` shape, which ADR-0009 A12 chose for the same `.port`
hazard, and the helper assumption 46 recommended. The tool checks
`--servers` the same way (decision 2). As a second line, `_connect`
catches a `nats.errors.Error` whose `__context__` is a `ValueError` and
re-raises it `from None`, so that a parse failure the validator did not
anticipate still logs nats-py's fixed text and no chained token.
Assumption 15's "not validated by `load_settings`" is superseded; the
validator checks only what nats-py's own parse would refuse plus (iii),
so no value that connected before is refused now. Whether nats-py
percent-decodes userinfo is not verified here; the operator guidance is
to keep `/`, `?`, `#` and `@` out of bus passwords.*

`producer()` and `consumer()` are callable before `start()`: the objects
they return use the connection lazily and raise `RuntimeError("NatsBus is
not started")` if used before it is up. This is required, not merely
allowed — `build_service` assembles the object graph, including the
worker's producer and consumer, before `run_service` calls `start()`
(ADR-0009 decision 3); amended 2026-09-21, ruling C8.

```python
class NatsProducer: ...      # decision 4
class NatsConsumer: ...      # decision 5

class StreamNotProvisionedError(Exception): ...  # a registered topic has no stream (decision 2)
class StreamConfigConflictError(Exception): ...  # an existing stream differs in an immutable field

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    OSError, nats.errors.NoServersError, nats.errors.TimeoutError,
    nats.errors.ConnectionClosedError, StreamNotProvisionedError,
)
```

`TRANSIENT_ERRORS` is what ingest and the aggregator pass to
`connect_with_retry("bus", bus.start, transient=...)` (ADR-0009 A1). A
rejected credential is **not** in it: nats.py raises
`nats.errors.AuthorizationError` and `InvalidUserCredentialsError`, which
subclass `nats.errors.Error` and none of the classes listed (Sources), so
the type-level distinction ADR-0009 A1 had to construct by hand for redis
and could not construct for aiokafka exists natively here. The reference
deployment configures no credentials (assumption 13); whoever adds them
MUST re-read `nats/errors.py` at the pinned version and amend this list.
The connection contract is: the initial `connect()` fails fast, so that
`connect_with_retry` owns the startup schedule — an unreachable server is
`NoServersError` within a few seconds, and a rejected credential raises as
its own type instead of being retried into `NoServersError` — and, once
connected, the client reconnects to a mid-run outage indefinitely.
*(Rewritten 2026-09-21, Amendment 1 ruling C1. The original sentence
named a `retry_on_failed_connect=False` option, which nats-py does not
have — it is a nats.go option; in nats-py 2.16 the initial connect loop
and mid-run reconnection are governed by the same
`allow_reconnect`/`max_reconnect_attempts` pair, so the two halves of the
contract cannot be expressed in one `connect()` call; Sources.)* The
mechanism, isolated in `hammertime.bus.nats._connect`:
`nats.connect(servers, allow_reconnect=False, max_reconnect_attempts=1,
connect_timeout=2, error_cb=_client_error)`, then on the live client
`nc.options["allow_reconnect"] = True` and
`nc.options["max_reconnect_attempts"] = -1`. `Client.options` is a public
dict that the reconnect path reads at runtime, but it is not documented
API, so the pin `nats-py>=2.16,<3` is load-bearing and whoever bumps the
client MUST re-verify those two reads in `nats/aio/client.py` (assumption
14, as rewritten). What an outage does to in-flight calls: a `publish` or
an `ack` during it raises `nats.errors.TimeoutError` (or
`ConnectionClosedError`), which the calling service treats exactly as it
treated an aiokafka failure — ingest answers 503, the aggregator's `run()`
fails and the process exits 1; a `fetch` during it times out and is
re-issued, indistinguishably from an idle partition (decision 5, as
amended). The client's asynchronous error hook is `_client_error`: one
`WARNING nats_client_error error=<exception>` line per refused connection
attempt or client-side error, no traceback. nats-py's default hook logs
each at `ERROR` with a full traceback under the `nats.aio.client` logger,
which would bury `connect_with_retry`'s `dependency_unavailable` records
during a normal startup wait (ruling C2; assumption 29).

**`hammertime.bus.memory`** implements the same contract with one
partition per topic: `MemoryProducer` appends to partition 0 and
deduplicates by `message_id` per topic for the lifetime of the
`InMemoryBus` (an unbounded duplicate window — a test never needs the
window to lapse); `MemoryConsumer` keeps, per `(topic, group)`, the set of
acknowledged offsets on the bus, and its own delivered-but-unacknowledged
set on the instance. `InMemoryBus` never redelivers a message to a live
consumer (no `ack_wait`); `delivery_count` is always 1. It keeps ADR-0011
assumption 22's limits (single partition; at most one live member per
group per topic). `InMemoryBus.end_offset(topic)` is the length of the
topic's log (`0` for a topic never published to), `async` like
`NatsBus.end_offset` so that the trie and the detector call it the same
way on both (amended 2026-09-21, ruling C3; decision 9 defines it).

### 4. Publishing: every event carries `Nats-Msg-Id = event_id`; ordering is by awaited acknowledgement

`NatsProducer.publish` sends `js.publish(subject, value, headers={"Nats-Msg-Id":
message_id})` when `message_id` is given, and awaits the `PubAck`; a
`PubAck` with `duplicate=True` is a normal return. The message key is
carried in a header, `Hammertime-Key`, so `ConsumedMessage.key` round-trips
(assumption 10). Every producer in this repository passes the envelope's
`event_id` as `message_id`: ingest's `ObservationPublisher._publish_one`
(the per-IP envelope's `event_id`), the aggregator's `TransitionEmitter`
(decision 4 step 4 of ADR-0011: the hot-ip envelope's `event_id`), and the
aggregator's reconciliation divert (`worker._divert`: the diverted
observation's own `event_id`, which it has because the message was decoded
before it was classified — a diverted message is byte-identical to the
consumed one, so its `event_id` is the same one ingest published under).

Effect, and what it closes: the stream drops a second copy of any record
whose `event_id` it has seen in the last 120 s. That is the transport
matching the emitter's promise — exactly one record with one `event_id`
per transition — within the window, which is what #88 asked
("enable idempotence?"). The answer is: yes, by `Nats-Msg-Id`, on every
publish, with no `acks=all` trade-off, because JetStream's acknowledgement
is already the stream's durable append. Outside the window a retried
record is appended again and carries the same `event_id`, so the trie's
same-`event_id` no-op requirement (ADR-0001 Amendment 1 clause 5; ADR-0011
Consequences, *Trie epic*) stands; it is now a window-bounded rarity
instead of the normal retry outcome. No producer in this repository
retries a failed publish today; the dedup header is what makes it safe for
one to start (Consequences).

**Ordering (ADR-0001 Amendment 1 clause 3).** A JetStream stream is one
sequenced log: each acknowledged publish has a stream sequence greater
than every acknowledged publish before it. The emitter awaits every
`publish` before starting the next (`transitions.py`, unchanged), so an
IP's transitions are appended in emission order with strictly increasing
sequences, whatever subject they land on; the trie applies the stream in
sequence order (decision 9). The client-side argument ADR-0001 Amendment 1
made from aiokafka's sender internals is no longer needed: the guarantee
is the awaited acknowledgement itself.

### 5. Consuming: one durable per `(group, partition)`; explicit acks; redelivery is a supported input

**Durable subscriptions.** For `subscribe(topic, partitions=<set>)` under
group `G`, `NatsConsumer` creates or binds one durable pull consumer per
partition on the topic's stream:

```text
durable name        <G>-<p>                    e.g. hammertime-aggregator-17
filter_subject      <topic>.<p>
ack_policy          explicit
deliver_policy      all                        (the durable's own position resumes; first creation starts at the stream's first message)
ack_wait            30 s
max_ack_pending     10 000
max_deliver         -1 (unlimited)
inactive_threshold  0 (never expires)
num_replicas        the stream's
```

and for `subscribe(topic, partitions=None)` one durable named `<G>` with
`filter_subject = <topic>.*` and the same settings. nats.py's
`pull_subscribe` binds an existing durable **without comparing its
configuration** (Sources), so a durable created under an earlier build with
different settings keeps them; `NatsConsumer` therefore reads
`consumer_info` after binding and logs `WARNING event=consumer_config_drift
consumer=<name> field=<f> expected=<e> actual=<a>` for each of `ack_wait`,
`max_ack_pending`, `filter_subject` that differs (editable fields, which the
provisioning story of a later release may reconcile; assumption 12).

**The per-partition durable is the unit of position.** A shard's read
position belongs to the shard, not to the member holding it, so an
operator moving shard 17 from member A to member B (restart both with new
`HAMMERTIME_SHARD_IDS`) resumes exactly where A left off. This is what
"committed offsets are keyed by group name on the broker" (ADR-0009
decision 9) becomes: acknowledged positions are kept by durables named
after the group, and renaming the group still orphans them (still a
`BREAKING` `CHANGES` entry).

**Fetching.** `NatsConsumer` runs one fetch loop per durable
(`fetch(batch, timeout)`, batch and timeout being implementation
constants, `TimeoutError` on an idle partition being the normal idle path)
feeding one in-process queue; the iterator yields from the queue. A fetch
that times out during a broker outage is indistinguishable at the client
from an idle partition, so it is treated the same way: the loop re-issues
the pull and the consumer rides the outage out; the iterator raises only
on a failure that is not a timeout (amended 2026-09-21, Amendment 1
ruling C3(5.3), superseding the sentence of assumption 14 that had a
fetch during an outage fail the aggregator's `run()`). A member whose
shards are idle for the whole outage therefore survives it; one that
publishes or acknowledges during it fails on that call, exits 1 and is
restarted by the orchestrator. A
message is *delivered* when the iterator yields it. The consumer retains
the nats.py `Msg` for every delivered, unacknowledged message, keyed by
`(topic, partition, offset)`.

*Malformed at the transport (added 2026-09-22, Amendment 4 rulings S3 and
S4). Two invariants every in-repo producer keeps are checked on the way
out of the queue, before a `Msg` becomes a `ConsumedMessage`, because a
principal that can publish to the stream directly need not keep them: the
subject's last token must be a partition — `TopicSpec.partition_of(subject)`
is not `None` (decision 1, as amended); the stream filter `<topic>.*`
admits any single token, and `int()` on an arbitrary one raised out of
the iterator, failed the service's `run()` and, the message never having
been acknowledged, crash-looped every whole-topic durable and every
positional replay that met it — and, when the message carries a
`Hammertime-Key` header, `partition_for(key, spec.partitions)` must equal
that partition, the hash decision 1 puts in this package; a message
stored under another partition's subject would otherwise be applied by
that partition's owner, and one IP would be owned by two shards (spec
section 20's invariant). A message failing either check is not yielded:
the consumer logs `WARNING event=malformed_subject subject=<s>
stream_seq=<n> reason=<not-a-partition|partition-mismatch>` (no key, no
payload), on a durable subscription terminates it (`Msg.term()`, so the
server never redelivers it) and on a positional subscription skips it,
and continues with the next. A message without the key header is yielded
with `key=None` and left to the consumer's own checks (the aggregator's
`_decode` refuses it). The aggregator's per-partition durables filter on
the exact numeric subject and cannot receive a non-partition token; the
checks protect the whole-topic and positional shapes (the detector, the
trie, `tools/replay`) and, for the key check, every shape. Only the pure
helpers (`partition_of`, `partition_for`) are unit-tested; the term-and-
continue path is the integration job's (#52).*

**`ack_wait` redelivery is a supported input.** JetStream redelivers a
delivered, unacknowledged message to the same live consumer after
`ack_wait` (30 s), with `delivery_count` incremented. With the aggregator's
1 s acknowledgement cadence that happens only when the process stalls —
a configuration re-evaluation pass over a large window (ADR-0011 decision
7 holds the worker lock for its duration) is the realistic case — but it
happens, so ADR-0003 Amendment 2's assumption that "a byte-identical
message handed to `handle()` twice within one live claim is not a
supported input" is withdrawn (ADR-0003 Amendment 3). The rule that
replaces it is decision 8's `REDELIVERED` outcome: a consumer that keeps a
handled position per partition recognises a redelivery as `offset <= <last
handled offset>` and acknowledges it without applying it. Delivery within
one durable is in sequence order, so a redelivered message is always
behind the handled position and a new one always ahead of it; nothing else
is needed to tell them apart. A redelivery of a message that was fetched
but never handled (a copy sitting in the queue when the stall began) is
ahead of the handled position and is handled normally — its first copy is
then the redelivery when it surfaces, and is skipped. `max_deliver` is
unlimited because every message the aggregator receives is acknowledged on
one of decision 8's paths — including `MALFORMED`, which is logged, counted
and acknowledged — so a poison message cannot loop.

**Positional subscriptions** (`start_offset` given) use an ephemeral
consumer with `deliver_policy = by_start_sequence`, `opt_start_seq =
start_offset`, `ack_policy = none`, and the filter of the requested
partitions; if the broker drops the ephemeral (a server restart; ephemerals
are not persisted) the consumer transparently recreates it from the last
delivered `offset + 1`, which is what nats.py's ordered consumer
(`subscribe(..., ordered_consumer=True)`, NATS ADR-17) already does on a
sequence gap or missed heartbeat (Sources). The implementation may use the
ordered consumer for exactly that reason; the contract is the delivery
order and the recreation, not the mechanism. A pull consumer with
`ack_policy = none` is legal on a limits-retention stream — the server
requires an explicit ack policy for pull consumers only on work-queue
streams (`JSConsumerPullRequiresAckErr`, Sources).

### 6. Shard assignment is static: `HAMMERTIME_SHARD_IDS` is required, `all` or an explicit set

`hammertime.aggregator.config.parse_shard_ids(text) -> frozenset[int]`
(it no longer returns `None`):

* `all` (case-insensitive) -> `frozenset(range(OBSERVATIONS.partitions))`,
  the explicit full set `0..127`. It is expanded at parse time, not passed
  to the bus as `partitions=None`, so that the aggregator always subscribes
  with an explicit set and gets one durable per shard (decision 5); a
  whole-topic durable would keep one position for all 128 shards, which
  could not follow a shard to another member later.
* An explicit set with the existing grammar (`0`, `0-3`, `0,2,5-7`;
  inclusive ranges; duplicates collapse) -> the set. Every id MUST satisfy
  `0 <= id < OBSERVATIONS.partitions`; an id at or above the count is a
  `ValueError` naming the variable and the count (this closes ADR-0011 A1's
  "a static id outside `0..127` is not ruled on here").
* `auto` -> `ValueError` whose message says that shard assignment is
  static since ADR-0013 and names the two accepted forms, so a deployment
  carrying the old default fails loudly with `config_invalid` and exit 2
  rather than silently claiming nothing. The message (amended 2026-09-21,
  Amendment 1 ruling T10) is
  `HAMMERTIME_SHARD_IDS='auto' is not accepted: shard assignment is static since ADR-0013; set it to 'all' or an explicit partition set (e.g. '0', '0-3', '0,2,5-7')`,
  with the value as given in place of `'auto'` (`'AUTO'` is rejected the
  same way); it MUST contain the variable name, the word `all`, `ADR-0013`
  and at least one explicit-set example, and tests pin those four
  substrings rather than the whole sentence.
* Set-but-empty or whitespace-only -> `ValueError` (ADR-0011 A3,
  unchanged).

`load_settings` treats an **unset** `HAMMERTIME_SHARD_IDS` as a
`ValueError` naming the variable: there is no safe default under static
assignment — `all` on every replica of a scaled deployment is the
two-owners misconfiguration of #90, and the previous default `auto` no
longer exists — so the operator states the set. `.env.example` and
`deploy/docker-compose.yml` set `HAMMERTIME_SHARD_IDS=all` for the
reference single-member stack. `AggregatorSettings.shard_ids` becomes
`frozenset[int]` (never `None`). `AggregatorWorker(shard_ids=None)` keeps
its constructor: `None` is passed to the bus as `partitions=None`, "every
partition as the bus defines it" — `{0}` on `InMemoryBus`, which is what
every in-process test and the harness of `docs/spec/integration-scenarios.md`
rely on; on `NatsBus` it would be the whole-topic durable and is **not** a
production configuration, because `build_service` always passes the
settings' explicit set. `startup_fields()` logs `shard_ids` as the sorted
list, and gains `member_id` (decision 7).

Members of a deployment MUST carry pairwise-disjoint sets whose union is
`0..127`; a partition in no member's set accumulates messages until
`max_age` discards them. Disjointness is enforced (decision 7); coverage is
an operator invariant, observable as a growing `num_pending` on the
unowned partition's durable (or, before any durable exists, as messages
on the subject with no consumer) — no service-side check is added
(assumption 17).

### 7. Overlapping shard sets are detected and refused: a per-shard lease in the state store (#90)

**What is checked.** Before a member builds a `ShardWindow` for shard
`p`, it must hold the shard's lease. The lease lives in the
`ShardStateStore` — the store that already holds the shard's HOT set and
sequence, is reachable by every member, and is the one dependency every
aggregator already has — under the owner id of the member. The interface
gains two methods:

```python
# hammertime.store.interface   (additions; Spec: section 20, section 32)
class ShardStateStore(Protocol):
    async def acquire_lease(self, shard: int, owner: str, ttl_seconds: float) -> str | None:
        """Take or renew shard's lease for owner. Returns None on success; otherwise the id of the
        member that holds it. Atomic: a lease is granted iff no live lease exists or the live
        lease is owner's own, in which case its expiry becomes now + ttl_seconds."""
    async def release_lease(self, shard: int, owner: str) -> None:
        """Drop shard's lease iff owner holds it; a no-op otherwise. Never raises for an unheld lease."""
```

`RedisShardStateStore`: key `hammertime:agg:{shard}:owner`, value the
owner id, one atomic server-side step per call (a Lua script: `GET`;
grant if absent or equal to `owner`, then `SET key owner EX ttl`; return
the current value otherwise), TTL in whole seconds rounded up.
`MemoryShardStateStore` gains an optional `clock: Clock | None = None`
constructor keyword (default `SystemClock()`, so every existing
argument-free construction is unchanged) and keeps `{shard: (owner,
expires_at)}`; an expired entry counts as absent. A lease is live iff
`now < expires_at` — at exactly `expires_at` it has expired — on both
backends (amended 2026-09-21, ruling T11). Running `RedisShardStateStore`
against fakeredis needs fakeredis's `lua` extra, which pulls `lupa`:
fakeredis 2.38 without it answers `EVAL` with `unknown command 'eval'`.
The root `pyproject.toml`'s `dev` group therefore carries `fakeredis[lua]`
— a Class 2, test-process-only dependency, MIT, recorded in ADR-0012
Amendment 3 — and the Lua form is kept rather than relaxed to a
`WATCH`/`MULTI` compare-and-set (amended 2026-09-21, ruling C5.1;
assumption 30 gives the reasons). `load` and
`record_transition` are unchanged, and the lease key is separate from the
HOT set so ADR-0011 A2's atomicity of `record_transition` is untouched.

**Where, by whom.**

* `ShardClaims` is constructed with two new keyword arguments, `member_id:
  str` and `lease_ttl_s: float`. In `on_assigned(partitions)`, for each
  shard in sorted order and **before** `state_store.load(p)`, it calls
  `acquire_lease(p, member_id, lease_ttl_s)` — strictly sequentially per
  shard: acquire `p`, load `p`, build `p`'s window, and only then the next
  shard's acquire, so a refusal on shard `q` has loaded nothing for `q` and
  every shard before `q` is leased and windowed at that moment (amended
  2026-09-21, ruling T6). A non-`None` result is a
  refusal: it logs `ERROR event=shard_owned_elsewhere shard=<p>
  owner=<other> member=<self>`, releases every lease this call acquired
  so far, and raises `hammertime.aggregator.sharding.assignment.ShardOwnedElsewhereError(shard, owner)`.
  The exception propagates out of `subscribe()`, out of `worker.start()`
  and out of `service.start()`; it is not in any transient tuple, so
  `run_service` logs `start_failed` and the process exits **1** (ADR-0009
  decision 8: a runtime failure after connections were made, not a
  configuration error the settings could have caught). *A `load()` that
  raises inside this call is a fault, not a refusal (amended 2026-09-21,
  Amendment 3 ruling (d)): `ShardClaims` logs nothing, releases no lease
  inside the call, and lets the exception propagate the same way, to exit
  1. The leases the call acquired — the failed shard's included, since it
  was acquired before its `load()` — stay in the held set, so `release()`
  drops them when `run_service` calls `stop()` on the failed start
  (ADR-0009 A7's `stop_failed` path), and they lapse after `lease_ttl_s`
  when the store cannot be reached for that either.* *Reaffirmed
  2026-09-22 (Amendment 4 ruling R5) with the replacement consequence
  stated: `release()` now runs in `stop()`'s `finally` (decision 8, as
  amended), so a `load()` — or window construction — that raises while
  the store answers is followed by an immediate release from the runner's
  `stop()`, and a replacement under any `member_id` starts at once. Only
  when the store is unreachable for that release too do the leases lapse,
  and then a replacement with a *different* id is refused with
  `shard_owned_elsewhere` and exits 1 on each attempt for at most
  `lease_ttl_s` — the crash outcome, which a rollback inside `on_assigned`
  could not improve, since its `release_lease` calls would go to the same
  unreachable store; a replacement with the same id reacquires at once
  either way.* *Which shards `on_assigned` skips (amended 2026-09-22,
  Amendment 4 ruling R6): a shard is skipped only while this member holds
  its lease — the held-lease set is the test, not the windows. A shard
  whose window survived a `release()` but whose lease is gone is claimed
  afresh: lease acquired, state loaded, a new window built in place of the
  old one; its handled position is kept, as nothing ever lowers it.
  Reachable only by a direct call after `stop()` — `worker.start()`
  subscribes once — but a member must never report a shard as held
  (`shards`, `window()`) on the strength of a window alone while another
  member may hold the lease.*
* `ShardClaims.renew_leases()` calls `acquire_lease` for every held shard;
  `AggregatorWorker.run_maintenance()` calls it **first**, before the
  expiry sweep, under the worker lock, so a member that has lost a shard
  emits nothing more for it. A non-`None` result logs `ERROR
  event=shard_lease_lost shard=<p> owner=<other> member=<self>` and raises
  `ShardLeaseLostError(shard, owner)`, which propagates out of
  `run_maintenance()`, out of the service's maintenance loop and out of
  `run()`, so the process exits 1 with `run_exited`. This is the narrow
  form of the fencing ADR-0011 A2 and ADR-0003 Amendment 2 declined: a
  member acts on a shard only while it holds the lease, checked every
  maintenance interval; the store itself still carries no fencing token on
  `record_transition`, so a transition emitted inside one interval after
  the lease lapsed is not fenced (assumption 18). *After `stop()` has
  released the leases, `run_maintenance()` renews nothing and sweeps
  nothing: it returns normally without touching the store, the windows or
  the producer (amended 2026-09-21, Amendment 3 ruling (c)).*
* `ShardClaims.release()` calls `release_lease` for every held shard;
  `AggregatorWorker.stop()` calls it after the final flush-and-ack
  (decision 8), so a clean stop hands the shards over immediately. A
  crashed member's leases expire after `lease_ttl_s`; a replacement with
  the same `member_id` (compose gives a restarted container the same
  name; a Kubernetes StatefulSet gives a pod the same ordinal name)
  reacquires at once, and one with a different id waits at most
  `lease_ttl_s`, crash-looping with `shard_owned_elsewhere` until then.
  *Corrected 2026-09-22 (Amendment 4 ruling N, on the S1 audit's note):
  the default `member_id` is `socket.gethostname()`, which under compose
  is the container ID unless `hostname:` is set — a *restarted* container
  keeps it, a *recreated* one (`docker compose up --build`, or a
  replacement after an unclean death) does not, and would wait out the
  TTL. The reference compose file therefore sets
  `HAMMERTIME_AGGREGATOR_MEMBER_ID: aggregator-0` on its one aggregator
  (decision 11), and its comment no longer says "keyed by the container
  name".*
  *`release()` empties the set of shards whose lease this member holds and
  leaves the windows where they are; `ShardClaims` keeps that set apart
  from its windows for exactly this reason, `renew_leases()` renews only
  what the set holds, and the set covers every lease an `on_assigned` call
  acquired whether or not the call completed (amended 2026-09-21,
  Amendment 3 rulings (c) and (d)).*

**Configuration.** `HAMMERTIME_AGGREGATOR_MEMBER_ID` (default
`socket.gethostname()`; set-but-empty is a `ValueError`) and
`HAMMERTIME_AGGREGATOR_LEASE_TTL_S` (default 30; a positive number that
MUST exceed `HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S`, else
`ValueError`, since renewal happens once per maintenance interval).

**Failure shape, summarised.** Overlap at start: `shard_owned_elsewhere`
(ERROR), `start_failed`, exit 1, `/readyz` never 200. Overlap discovered
later (a lease lapsed and another member took the shard): `shard_lease_lost`
(ERROR), `run_exited`, exit 1. No new metric series: ADR-0011 A13's list of
nine is closed and the two records plus `shards_claimed` reading 0 are the
observables (assumption 19).

**What is enforced versus what stays an operator invariant.** Enforced:
no two live members with distinct `member_id`s hold the same shard for
longer than one maintenance interval past a lapsed lease. Operator
invariants: `member_id`s are unique per live member (two members sharing
an id share its leases and are not told apart); every partition `0..127` is
in some member's set (decision 6); `lease_ttl_s` comfortably exceeds the
longest stall a member can suffer (a configuration re-evaluation pass over
`max_tracked_ips` IPs holds the lock and delays renewal — with the
defaults, 30 s against a pass that yields every 1000 IPs; an operator who
raises `max_tracked_ips` should raise the TTL with it).

### 8. The aggregator: acknowledge what was handled; `REDELIVERED` is the eighth outcome; no revocation path

`ShardClaims` (`hammertime.aggregator.sharding.assignment`), the public
surface after this ADR:

```python
class ShardClaims:                                   # satisfies AssignmentListener
    def __init__(self, *, state_store, producer, consumer, clock, config,
                 member_id: str, lease_ttl_s: float,
                 max_tracked_ips: int = 1_000_000) -> None: ...
    config: DetectionConfig                          # property (ADR-0011 A18)
    shards: frozenset[int]                           # property
    def window(self, shard: int) -> ShardWindow | None: ...
    def windows(self) -> tuple[ShardWindow, ...]: ...
    def adopt_config(self, config: DetectionConfig) -> None: ...   # ADR-0011 A18, unchanged
    def handled_position(self, shard: int) -> int | None: ...     # one past the last handled offset; None if none, or if shard is not held; never reset by a commit (amended 2026-09-21)
    def mark_handled(self, message: ConsumedMessage) -> None: ...  # records the message for the next ack
    async def commit_handled(self) -> None: ...                    # producer.flush(), then consumer.ack(<handled, unacked>)
    async def renew_leases(self) -> None: ...                      # decision 7
    async def release(self) -> None: ...                           # decision 7
    async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None: ...
```

* **`mark_handled(message)`** appends `message` to the claim's list of
  handled, unacknowledged messages for `(message.topic, message.partition)`
  and raises the claim's handled position to `max(<current>, message.offset
  + 1)`. A partition this object does not hold is a `KeyError` (ADR-0011
  A20, unchanged). The worker calls it under the lock at the end of
  `handle()` for every outcome except `UNCLAIMED`.
* **`commit_handled()`** is `await producer.flush()` then `await
  consumer.ack(<every handled, unacknowledged message, in delivery order>)`
  then clears the list. Both calls are made even when the list is empty
  (`ack([])` returns normally without touching the broker), so the
  flush-then-ack trace is uniform and testable. It is the aggregator's
  **only** acknowledgement path: the periodic commit (every
  `HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S` of wall time, unchanged) and
  `stop()` both go through it. ADR-0009 decision 7's flush-before-commit
  rule reads "flush before ack" and is unchanged in substance. The list is
  handed to `ack()` as it is, in delivery order, a `REDELIVERED` copy
  alongside its original: `ack()` acknowledges a message named twice once
  (decision 3, as amended), so `commit_handled()` does not deduplicate
  (amended 2026-09-21, rulings C5 and T1).
* **`handled_position(shard)`** returns `None` for a shard this object
  does not hold, exactly as for a held shard nothing has been handled on:
  the read surface (`window`, `handled_position`) is total and the write
  surface (`mark_handled`) raises `KeyError` (amended 2026-09-21, ruling
  T3). The position belongs to the claim for the life of the process:
  `commit_handled()` clears the handled *list* and leaves the position
  where it is — ADR-0003 Amendment 3 item 2 keeps it for the redelivery
  test, not for any commit — and nothing ever lowers it (ruling T4).
* **`on_assigned(frozenset())` is a `ValueError`** (amended 2026-09-21,
  ruling T5). It is unreachable through the bus — `static_partitions`
  refuses an empty set before the listener is called, and
  `partitions=None` always resolves to a non-empty set — so only a direct
  call can reach it; a member holding nothing must not report itself
  ready, which is ADR-0011 A3's reason for refusing the empty set at the
  bus, and the direct call is refused for the same reason. The
  `no_shards_assigned` record that the old empty-assignment path wrote is
  gone with it (log records bullet).
* **The requirement of ADR-0011 A20 is preserved without its mechanism.**
  A message is acknowledged iff `handle()` finished with it. The message in
  hand at `stop()` is finished before the final `commit_handled()` (the
  worker's loop already does this); a message fetched into the consumer's
  queue and never yielded is negatively acknowledged by `close()` and
  redelivered to the next member at once. There is no revocation any more,
  so A20's ruling part 3 — the `ShardWindow` identity captured in the
  fetch step, the `fetched_under` comparison, `_Fetched` — is removed: a
  claim is never replaced while the process lives, and the capture was
  only ever there to detect a re-claim. `UNCLAIMED` stays as the outcome
  for a message on a partition this member holds no window for (reachable
  only through a direct `handle()` call now; ADR-0011 A19's record and
  no-counter rule unchanged), *and (amended 2026-09-22, Amendment 4 ruling
  R2) for any message handed to `handle()` after `stop()` has begun,
  whatever its partition: after `stop()` the member holds no lease, so
  every partition is one it must not act on, and the `unclaimed_partition`
  record is logged as for any other `UNCLAIMED`.*
* **`on_revoked` is gone** from `AssignmentListener`, from `ShardClaims`
  and from `AggregatorWorker`. Static assignment has no revocation; the
  way a shard changes hands is a `stop()` on one member and a `start()` on
  another. `AggregatorWorker.stop()` is: stop fetching, finish the message
  in hand, `commit_handled()`, `release()`; windows are kept in memory
  (nothing reads them after `stop()` except tests) but the leases are not
  — `release()` empties the held-lease set, which `ShardClaims` keeps
  apart from its windows, and a `run_maintenance()` that runs after
  `stop()` re-takes nothing and sweeps nothing (amended 2026-09-21,
  Amendment 3 ruling (c)) — and the service closes
  the bus (`NatsBus.close()`, which `nak`s and unsubscribes) and the store
  client afterwards, as today. `stop()` does not call `consumer.close()`
  itself (amended 2026-09-21, ruling T8): closing is the service's step,
  after `stop()` has returned, so that the final acknowledgement is sent
  on a live consumer and the `nak` of whatever was fetched but never
  yielded follows it. *`stop()`'s order is total (amended 2026-09-22,
  Amendment 4 rulings R2 and R4). From the moment `stop()` sets the stop
  flag, every path that could act on a shard checks the flag under the
  worker lock and stands down: `handle()` returns `UNCLAIMED` without
  decoding, applying, emitting or marking, so the message stays
  unacknowledged and `close()` hands it to the next member;
  `run_maintenance()` and `apply_config()` return without renewing,
  sweeping, re-evaluating, emitting or writing (`apply_config()` leaves
  the worker's `config` as it was and logs no `config_reevaluated`; this
  closes assumption 54); the periodic commit (`_commit_if_due`) is
  skipped, so the `commit_handled()` inside `stop()` is the last
  acknowledgement this worker sends; and `run()`, on a wake-up in which
  the stop signal and a received message are both complete, returns
  without handing the message to `handle()`. The message in hand — one
  whose `_handle` holds the lock when the flag is set — is finished and
  covered by the final commit, as before; one merely waiting for the lock
  is not in hand and takes the `UNCLAIMED` path. `stop()` itself runs its
  sequence once: under the lock, `commit_handled()` inside a `try` whose
  `finally` is `release()`, so the leases are released even when the final
  acknowledgement fails — the exception propagates after the release, and
  the messages it failed to acknowledge reach the next member after
  `ack_wait`, at-least-once as ADR-0003 has it. A later `stop()` — the
  runner's `_stop_quietly` after a crash exit, by which time the service
  has closed the bus and `ack(())` would be a `ValueError` — acquires the
  lock and returns without touching the bus or the store; that is what
  "idempotent" means for it. `AggregatorService.stop()` is idempotent by
  the same token, its other steps (readiness, poller, server) being so
  already.*

  *`AggregatorService.run()` reports what stopped it (amended 2026-09-22,
  Amendment 4 ruling R7; ADR-0009 decision 5 step 7): when the first of
  its four tasks completes, `run()` collects the exceptions of the
  completed tasks first, then calls `stop()` inside a `try` that appends a
  raised exception to the same list and logs `WARNING stop_failed
  error=<str>` through the service's logger, then gathers the remaining
  tasks, closes the clients (the bus first) and raises the first collected
  exception. `run_exited` therefore names the failure that stopped the
  service; a `stop()` that fails on the same outage (the final `ack_sync`
  timing out after the periodic one did) is reported only when nothing
  failed before it.*
* **`REDELIVERED`**, the eighth `ObservationOutcome` (`"redelivered"`),
  ruled in decision 5: after the window lookup (`UNCLAIMED` check) and
  before decoding, `if message.offset < claims.handled_position(partition)`
  (equivalently `offset <= last handled offset`) the worker logs `WARNING
  event=redelivered_observation topic=<t> partition=<p> offset=<o>
  delivery_count=<n>`, calls `mark_handled(message)` (so the copy is
  acknowledged at the next commit and the handled position, already past
  it, is unchanged), and returns `REDELIVERED` — not decoded, not
  classified, not diverted, not counted under any series, the store
  untouched. Never returned by `classify_observation`.
* **Log records** (ADR-0011 decision 8's list) gain `redelivered_observation`
  (above), `shard_owned_elsewhere`, `shard_lease_lost` (decision 7) and
  lose `shard_revoked`, `no_shards_assigned` (an empty assignment is now
  impossible: a static set is never empty and `all` is never empty).
  `shard_claimed shard=<p> inherited_hot=<n>` is unchanged.

`AggregatorWorker`'s constructor gains `member_id: str` and `lease_ttl_s:
float` (keyword-only; forwarded to `ShardClaims`), with defaults
`member_id="aggregator"` and `lease_ttl_s=30.0` so that every existing
test construction stays valid; `build_service` passes the settings' values.
`AggregatorService` loses `_KafkaBus` and `_TRANSIENT_BUS_ERRORS` in
favour of `hammertime.bus.nats.NatsBus` and `TRANSIENT_ERRORS`; the
`MessageBus` protocol moves to `hammertime.bus.interface` (decision 3).

### 9. The trie's replay position is the stream sequence; the detector must tolerate redelivery order

ADR-0010's Consequences deferred "how the trie snapshot records its replay
position for a multi-partition `hammertime.hot-ip.v1` (§33's single 'event
sequence number' is not a Kafka offset)". It is now answerable: a
JetStream stream has one sequence across all its subjects, and
`ConsumedMessage.offset` is it. Ruling:

* The trie subscribes to `hammertime.hot-ip.v1` **positionally**:
  `subscribe(topic, start_offset=<snapshot.replay_position + 1>)` (or
  `start_offset=0` with no snapshot — corrected 2026-09-21, Amendment 1
  ruling C7: the original `1` would skip the first message of the memory
  log, whose offsets start at 0; `0` is below every JetStream sequence and
  delivers from the first retained message on both), applies messages in
  the order the iterator yields them, and never acknowledges. There is no
  `hammertime-trie` durable consumer.
* The snapshot records `replay_position: int` — the `offset` of the last
  hot-ip message applied before the snapshot was taken — as §33's "event
  sequence number". The trie's `event_sequence` (its count of applied
  hot-ip events, carried on `PrefixStatsChanged.sequence` and in read
  responses; ADR-0001 Amendment 1 clause 4, ADR-0010 decision 4) is
  **unchanged** and is a different number; whether the trie epic unifies
  the two is left to it (see the open question in the hand-off report).
* ADR-0010 decision 3's "the producer is flushed before the consumer
  position for the hot-ip topic is committed" becomes "the producer is
  flushed before a snapshot records a `replay_position` that covers the
  event": the trie has no consumer position to commit, and the invariant
  the sentence protects — a restart never skips an event whose
  `PrefixStatsChanged` never reached the log — is carried by the snapshot
  alone. ADR-0009 decision 7's drain list reads, for the trie, "flush,
  then write the final snapshot"; for services with durable subscriptions
  (aggregator, detector) "flush, then acknowledge".
* Readiness ("replayed to the log end as it stood when `start()` began",
  ADR-0009 decision 4): the log end is read from the bus at `start()` as
  `await bus.end_offset(topic)` — on `NatsBus`
  `stream_info(...).state.last_seq + 1`, on `InMemoryBus` the log length.
  On both it is **the offset the next appended message will receive**
  (`1` for an empty stream, `0` for an empty memory log), so one readiness
  test works on both: the service has replayed to the log end once every
  delivered message with `offset < end_offset` has been applied, i.e. once
  the last applied offset is `>= end_offset - 1`, or immediately when
  `end_offset <= start_offset`. *(Renamed from `last_offset` 2026-09-21,
  Amendment 1 ruling C3(5.2): the original was sync in this ADR and had
  to be async on NATS, and "the log length" on memory was one past the
  last offset while `state.last_seq` on NATS was the last offset itself,
  so a check written as `offset >= last_offset` behaved differently on
  the two buses. Assumption 20 as rewritten.)* A positional subscription
  filtered to a subset of partitions cannot use this test — the last
  message in the stream may be on a subject it does not receive — which
  is why the trie and the detector subscribe with `partitions=None`.
* **The detector** uses a durable subscription (`hammertime-detector`,
  `partitions=None`) and acknowledges what it applied, so after a crash
  the log may redeliver a `PrefixStatsChanged` **after** newer stats for
  the same prefix have been applied (unacknowledged messages come back
  after `ack_wait`, behind whatever was appended since). The detector's
  view is "latest known stats" (ADR-0010 decision 5), so its epic MUST
  apply a `PrefixStatsChanged` only if its `sequence` is greater than the
  one it holds for that prefix — a constraint recorded here so that the
  epic does not learn it from a flapping detection.

### 10. Configuration keys

| key | before | now |
| --- | --- | --- |
| `HAMMERTIME_BUS_KIND` | `kafka` (default) or `memory` | `nats` (default) or `memory`; `kafka` is a `ValueError` (exit 2) |
| `HAMMERTIME_BUS_BROKERS` | Kafka bootstrap list, default `localhost:19092` | comma-separated NATS server URLs (`nats://host:4222`, also `tls://`, `ws://`, `wss://`), default `nats://localhost:4222`; not validated by `load_settings` (unchanged posture; ADR-0009 A12's last assumption; assumption 15) — *superseded 2026-09-22 (Amendment 4 ruling S2): every entry is checked by `hammertime.bus.nats.validate_bus_url` in `load_settings`, and an entry nats-py's own parse would refuse is a `ValueError` naming the variable and the entry's position, `config_invalid`, exit 2*. *Amended 2026-09-21 (Amendment 2): an entry MAY carry userinfo in the two forms nats-py reads — `nats://user:password@host:4222` and `nats://token@host:4222` — and so may `hammertime-provision --servers`; the value is passed to `nats.connect` as given, and no log record of any service or tool carries it: records name the servers only as `bus_endpoints = bus_endpoints(value)` (decision 3). This extends spec §47.7's "no record may contain a credential" to the userinfo of a bus URL and supersedes ADR-0009 A7's "`bus_brokers` is logged verbatim", which was written for Kafka's `host:port` bootstrap list.* |
| `HAMMERTIME_SHARD_IDS` | `auto` (default) or a set | **required**; `all` or a set within `0..127`; `auto` rejected (decision 6) |
| `HAMMERTIME_AGGREGATOR_MEMBER_ID` | — | new; default `socket.gethostname()` (decision 7) |
| `HAMMERTIME_AGGREGATOR_LEASE_TTL_S` | — | new; default 30 (decision 7) |
| `HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S` | offset-commit cadence | acknowledgement cadence; same default 1.0 |

`AggregatorSettings` field names, for the record (amended 2026-09-21,
Amendment 1 ruling T9): `bus_kind` and `bus_brokers` (the names
`IngestSettings` already uses for the same two keys), `shard_ids:
frozenset[int]`, `member_id: str`, `lease_ttl_s: float`, alongside the
existing `host`, `port`, `detection_config_path`, `store_kind`,
`redis_url`, `maintenance_interval_s`, `config_poll_interval_s`,
`commit_interval_s`, `max_tracked_ips` and `reevaluation_batch`. Error
precedence between keys in `load_settings` is first-in-load-order, as
ADR-0009 A12 has it, and is not pinned (ruling T7): a test that expects
one key's error supplies valid values for every other key.

No key is removed. `.env.example`'s `HAMMERTIME_TOPIC_*` lines remain
unread dead text (ADR-0010 Consequences). The `CHANGES` lines these imply
are given in the hand-off report; the `BREAKING` ones are
`HAMMERTIME_BUS_KIND`/`HAMMERTIME_BUS_BROKERS` (semantics) and
`HAMMERTIME_SHARD_IDS` (required; `auto` gone).

### 11. The reference deployment

`deploy/docker-compose.yml` (a later dispatch makes these edits; recorded
here as the contract):

* `nats`: `image: nats:2.15.0-alpine` (Docker Official Image, linux/amd64
  digest `sha256:eda962d67930eda338222072d9a9f3818855d922ad224c399b0b01d251e9b91b`,
  11.5 MB, pushed 2026-09-18, confirmed by the top-level session and again
  by the architect from
  `https://hub.docker.com/v2/repositories/library/nats/tags/2.15.0-alpine`
  on 2026-09-21), `command: ["-js", "-sd", "/data", "-m", "8222"]`, a
  named volume `nats-data:/data`, ports `4222:4222` (clients) and
  `8222:8222` (monitoring) — *published on the loopback interface only,
  `127.0.0.1:4222:4222` and `127.0.0.1:8222:8222`, and `valkey`'s
  `127.0.0.1:6379:6379` likewise (amended 2026-09-22, Amendment 4 ruling
  S1): the server runs with no `authorization` block and the store with no
  `requirepass`, so anything that can reach those ports can publish
  observations that bypass every control ingest applies, purge or delete
  the streams, acknowledge or delete the durables, and rewrite the shard
  leases and HOT sets; the host's own tooling still reaches them, the
  healthchecks run inside the containers and need no host mapping, and
  the trust boundary is stated in assumption 13* — and `healthcheck: wget -qO-
  http://127.0.0.1:8222/healthz?js-enabled-only=true` (the monitoring
  page documents `js-enabled-only` as "Returns an error if JetStream is
  disabled" and 200/400 as the response codes; Sources), interval 2 s,
  retries 15. The `-alpine` variant is chosen over the 7.3 MB scratch-based
  `nats:2.15.0` because a compose `healthcheck` runs inside the container
  and needs a shell and an HTTP client, which a scratch image does not
  carry (assumption 11).
* `provision`: builds `tools/provision/Dockerfile` (same shape as the
  service Dockerfiles), runs `hammertime-provision --servers nats://nats:4222`,
  `restart: "no"`, `depends_on: nats: condition: service_healthy`. *Its
  `COPY --from=ghcr.io/astral-sh/uv:<tag>` line is pinned to `0.12.17`
  (amended 2026-09-22, Amendment 4 ruling R9; the release current on
  2026-09-22 — PyPI `uv` 0.12.17, `license_expression` `MIT OR
  Apache-2.0`; the image tag form `ghcr.io/astral-sh/uv:{major}.{minor}.{patch}`
  and the "pin to a specific uv version" guidance are uv's own; Sources),
  and the four `services/*/Dockerfile`s, which carried the same `:latest`
  since before this epic, are pinned to the same tag in the same change
  set so the five files keep one shape. ADR-0012 Amendment 4 carries the
  inventory row.*
* `ingest`, `aggregator`, `trie`, `detector`: `HAMMERTIME_BUS_BROKERS:
  nats://nats:4222`; `depends_on: provision: condition:
  service_completed_successfully` and `valkey: condition: service_healthy`
  where applicable; `valkey` gains `healthcheck: valkey-cli ping`. The
  application services keep the `/readyz` healthchecks ADR-0009 decision
  10 specified (added if not yet present). *The `aggregator` service sets
  `HAMMERTIME_AGGREGATOR_MEMBER_ID: aggregator-0` (amended 2026-09-22,
  Amendment 4 ruling N; decision 7, as corrected), so a recreated
  container reacquires its shards at once instead of waiting out the
  lease TTL under a fresh container ID.*
* `broker` (Kafka) is removed; `deploy/docker-compose.redpanda.yml` is
  deleted; the `KAFKA_*` block and `CLUSTER_ID` go with them.
* `Makefile` `up` becomes `docker compose -f deploy/docker-compose.yml up
  --build -d --wait`; `.env.example` gets the new key values;
  `README.md` line 34 says "NATS JetStream" and line 50 "nats, valkey,
  prometheus, all four services".
* `.github/workflows/ci.yml`'s `integration` job stays `if: false`: this
  epic delivers re-enable condition 3 (`--wait`), but conditions 1 (trie
  and detector implemented and starting) and 2 (real tests under
  `tests/integration` and `tests/e2e`) are #52's. `CLAUDE.md`'s "Disabled
  CI coverage" entry is the session's to update.

### 12. Licence and drop-in policy

NATS server (Apache-2.0) and nats-py (Apache-2.0) are open source under
ADR-0012 definition 1. NATS has no wire-compatible second implementation,
so there is no drop-in in ADR-0012's sense; ADR-0012 Amendment 2 adds a
rule under which a wire-protocol-unique component may nevertheless be the
reference — foundation-governed so that no single vendor can relicense it,
and confined behind one in-repo interface (`hammertime.bus`) with an
in-process implementation used by the whole test suite, so that replacing
it is a bounded change to one package plus `deploy/` rather than to the
services — and records that `nats`'s compliance under that rule is
conditional on prerequisite 3 above being verified. If the trademark or
relicensing facts turn out otherwise, NATS is a single-vendor component
with a paid tier and no drop-in, non-compliant under decision 2 items 1-2,
and ADR-0012 decision 7's pre-1.0 remediation (replace before 1.0) applies.

## JetStream KV does not replace Valkey

Settled by the epic, on merit, and confirmed against the primary source
(prerequisite 2); recorded so nobody re-litigates it mid-implementation:

* KV has no set type. The HOT set is a real Redis `SET`
  (`packages/hammertime-store/src/hammertime/store/redis.py` lines 226,
  270-276: `smembers`, `sadd`, `srem`). Flattening it into one key per IP
  makes `load(shard)` a wildcard scan of up to `max_tracked_ips`
  (1 000 000) entries; keeping it as one value makes every transition a
  compare-and-swap read-modify-write of a million-entry blob.
* `mark_seen`'s never-shorten-a-TTL invariant is `EXPIRE key ttl GT`
  (`redis.py` line 164). KV has no `GT` analogue: per ADR-8 as updated by
  ADR-48, a TTL is accepted on `Create()` and `Purge()` only, "not ... for
  other API", so an existing key's TTL cannot be extended at all, let alone
  monotonically; and ADR-8 disclaims read-after-write consistency, so a
  read-then-write cannot even see its own last write reliably.
* `record_transition`'s atomic "membership change + clamped sequence"
  (`redis.py` lines 259-290; ADR-0011 A2) is one server-side step in Redis
  and would be two KV operations with no transaction between them.
* `fakeredis` lets every store test run container-free in the `check` job.
  There is no in-process fake for JetStream KV, so store tests would move
  into the disabled `integration` job — the coverage hole this epic is
  partly meant to close.

Also settled: no second production backend behind one interface. A second
real backend forces the interface down to the intersection of the two and
costs the explicit acknowledgement model of decision 5. `memory.py` for
tests plus one real backend remains the only supported shape.

## Assumptions

Each of these is a judgment call the epic, the spec and the earlier ADRs do
not make. Push back on them individually.

1. **The CNCF/Synadia settlement terms.** That the NATS trademarks are
   held by the Linux Foundation, that the nats.io domain and the GitHub
   repositories are with CNCF, that Apache-2.0 continues, and that the
   BUSL relicensing proposal was withdrawn, is taken from secondary
   coverage the epic lists (The Register, The New Stack, DEVOPSdigest,
   LinuxSecurity) and is **not** primary-verified (prerequisite 3). The
   ADR-0012 verdict is conditional on it. Two primary facts hold
   regardless: the server's `LICENSE` is Apache-2.0 and its README says it
   is a CNCF project.
2. **Accepting the swap although its first reason fell away.** Prerequisite
   5 shows Kafka clears the 60 s deadline by a wide margin. This ADR
   proceeds on the remaining reasons and on the design gains under
   Consequences. It was held pending owner confirmation, so that the owner
   could reject it before any code changes, and the owner confirmed it on
   2026-09-21 with knowledge of that result (see Status).
3. **Stream per topic; subjects `<topic>.<partition>`; stream names with
   `-` for `.`.** The alternative — one stream for everything — would make
   one retention policy serve four topics with retentions from one to
   thirty days. Naming follows ADR-6's rule mechanically.
4. **FNV-1a 32-bit modulo N as the partition hash.** Any stable hash would
   do. FNV-1a is chosen because it is a few lines with no dependency and
   matches the algorithm the NATS server's `{{partition()}}` transform
   uses (Sources), so the in-repo mapping and a hypothetical server-side
   one agree for single-token keys. No mapping compatibility with the
   Kafka partitioner is attempted: no deployment carries data.
5. **Partition counts unchanged (128 / 8 / 32 / 4).** Only the
   observations count is load-bearing (ADR-0011 A1). The other three could
   be 1 now that nothing parallelises per partition, but reducing them is
   not required by anything and is a separate decision.
6. **Retention `limits`, `max_age = retention_seconds`, `max_bytes`
   unbounded, `discard old`, file storage.** `limits` is the only policy
   under which a durable's unacknowledged messages and a positional
   replay coexist (work-queue deletes on ack; interest deletes when no
   consumer wants a message, which would delete hot-ip events the trie
   has not read because it holds no durable). Unbounded bytes mirrors
   today's Kafka retention (time only). A byte cap is a deployment
   decision the provisioner does not yet take a flag for.
7. **`duplicate_window` 120 s.** The server default (2 minutes); the
   measurement used it. Longer windows cost tracking memory per stored id;
   a producer retry that takes more than two minutes is not one this
   repository performs.
8. **`num_replicas` 1 in the reference deployment; a `--replicas` flag on
   the provisioner.** Single node; a clustered deployment sets 3 at
   provisioning time. The measurement was R=1 (prerequisite 4's caveat).
9. **Consumer settings: `ack_wait` 30 s (server default), `max_ack_pending`
   10 000, `max_deliver` unlimited, `inactive_threshold` 0.** 30 s bounds
   the delay before an unacknowledged message reaches the next member after
   a crash (a clean stop `nak`s and pays nothing). 10 000 rather than the
   1 000 the measurement used: with a 1 s acknowledgement cadence,
   `max_ack_pending` is the per-shard throughput cap in messages per
   second, and 1 000/s per shard is too close to what one busy shard can
   see; 10 000 costs pending-state memory only when pending. Unlimited
   redelivery because every message is acknowledged on some path
   (decision 5). Durables never expire because an unowned shard must keep
   its position. *"A clean stop `nak`s and pays nothing" is qualified
   2026-09-22 (Amendment 4 ruling R1): it pays nothing for what reached
   the client's fetch loops; up to `FETCH_BATCH` messages per durable that
   the server delivered against the last outstanding pull and that never
   reached a loop wait out `ack_wait` (decision 3, `close()`).*
10. **The message key rides in a `Hammertime-Key` header.** JetStream has
    no message key; the subject carries the partition, not the key, and
    the aggregator asserts the key against the entry's IP (ADR-0004
    invariant, `worker._decode`). A header is the cheapest carrier; the
    name is Hammertime's (NATS reserves the `Nats-` prefix).
11. **`nats:2.15.0-alpine` over `nats:2.15.0`.** The scratch variant is
    assumed to carry no shell or HTTP client, which a compose
    `healthcheck` needs; that assumption comes from the image's known
    `FROM scratch` build, not from inspecting the image (the Docker
    Official Image page fetched says nothing about variants). If a
    reviewer prefers the smaller image, the healthcheck has to come from
    outside the container. Provenance is ADR-0012 decision 5 class (b),
    Docker Official Image; that its Dockerfile is maintained by the NATS
    project (`nats-io/nats-docker`) is from recall.
12. **Binding a durable that already exists does not reconcile its
    configuration; drift is logged.** nats.py binds without comparing
    (Sources). Editing the durable from a service would let two builds
    fight over it; leaving it and warning is the conservative choice
    pre-1.0. A later release may move consumer settings into the
    provisioner.
13. **No credentials or TLS in the reference deployment.** As with Kafka
    today. `TRANSIENT_ERRORS` is correct for that configuration; the
    credential rule of ADR-0009 A1 binds whoever adds them. *Amended
    2026-09-21 (Amendment 2): "no credentials in the reference deployment"
    is not "no credentials anywhere" — `HAMMERTIME_BUS_BROKERS` and
    `--servers` MAY carry userinfo (decision 10, as amended), which is why
    the no-userinfo-in-logs rule and `bus_endpoints` are settled now,
    before any deployment carries one, rather than when one does.*
    *The trust boundary this implies, stated (amended 2026-09-22,
    Amendment 4 ruling S1, on the S1 audit's finding 1): the bus and the
    store are inside the trust boundary, and the reference deployment
    trusts everything that can open a TCP connection to them. With no
    NATS `authorization` block and no Valkey `requirepass`, a principal
    that reaches 4222 can publish a well-formed observation under any
    subject — applied by the aggregator as if ingest had authenticated,
    rate-limited and deduplicated it, so spec section 36's controls are
    bypassed entirely — pre-publish a legitimate agent's next `event_id`
    so that ingest's real publish is a silent duplicate, purge or delete a
    stream (`deny_delete`/`deny_purge` are `false`, decision 2) and
    acknowledge or delete the `hammertime-aggregator-<p>` durables; one
    that reaches 6379 can rewrite `hammertime:agg:<p>:owner` to evict a
    live member (`ShardLeaseLostError`, exit 1) or delete a HOT set; and
    8222 (`/connz`, `/jsz`, `/varz`) discloses client addresses, subjects
    and stream state. The reference compose file therefore publishes
    4222, 8222 and 6379 on `127.0.0.1` only (decision 11), `.env.example`'s
    event-log block says so, and any deployment in which a host other
    than the one running the stack can reach those ports MUST put NATS
    `authorization` and Valkey `requirepass` in front of them — the step
    ADR-0009 A1's credential rule and the `TRANSIENT_ERRORS` comment
    already anticipate — before it is exposed. This is a statement of the
    boundary, not a mechanism: no authentication is added to the bus or
    the store here (a follow-up, Consequences), and the services' own
    ports (8080-8083, 9090) are unchanged and are the owner's to reconsider
    (open question in the Amendment 4 report).*
14. **Connection options: fail-fast connect, unbounded reconnect, by
    switching the client's options after the initial connect.**
    *(Rewritten 2026-09-21, Amendment 1.)* So that `connect_with_retry`
    owns the startup schedule and a mid-run outage is survived by the
    client. nats-py has no option that separates the initial attempt from
    reconnection, so `_connect` connects with `allow_reconnect=False,
    max_reconnect_attempts=1, connect_timeout=2` and then sets
    `nc.options["allow_reconnect"] = True` and
    `nc.options["max_reconnect_attempts"] = -1` on the live client. The
    observable behaviour during an outage: a timed-out `publish` or `ack`
    fails the caller (ingest 503; the aggregator exits 1); a timed-out
    `fetch` is the idle path and is re-issued (decision 5, as amended), so
    a member with nothing to publish or acknowledge survives the outage.
    The `options` mutation was verified by `coder` against a real server
    restart on 2026-09-21; the claim that a rejected credential raises as
    its own type under these options comes from reading
    `nats/aio/client.py`, not from a test, and is not load-bearing while
    assumption 13 holds. Original text: "So that `connect_with_retry` owns
    the startup schedule and a mid-run outage is survived by the client;
    the observable behaviour during an outage (a timed-out publish or
    fetch fails the caller) is unchanged from today."
15. **`HAMMERTIME_BUS_BROKERS` is not validated by `load_settings`.**
    ADR-0009 A12 left the parallel question open for Kafka; nats.py parses
    URLs at `connect()`, so a malformed one surfaces as `start_failed` and
    exit 1 rather than exit 2. Adding a grammar of Hammertime's own is the
    second-definition-of-validity A12 argued against; a driver-parser
    helper like `validate_redis_url` is a candidate follow-up, not done
    here. *Superseded 2026-09-22 (Amendment 4 ruling S2): the helper is
    `validate_bus_url`, called from `load_settings` and the tool; it
    checks only what nats-py's own parse would refuse (plus an `@` outside
    the authority), so it is the driver's definition of validity, not a
    second one, and a malformed URL is now exit 2 with `config_invalid`
    and no echo of the value. Decision 3, `bus_endpoints` paragraph.*
16. **`HAMMERTIME_SHARD_IDS` is required rather than defaulting to
    `all`.** A default of `all` plus lease detection would be safe for one
    member and would make every scale-out fail at start; requiring the
    value makes the operator state the intended set. The precedent for a
    required key is `HAMMERTIME_INGEST_AGENT_TOKEN_KEY`.
17. **Coverage of `0..127` is not checked.** A member cannot see other
    members' sets without the coordination static mode exists to avoid;
    the lease detects overlap, not gaps. Gaps are visible in the
    provisioner's stream/consumer listing and as pending counts.
18. **A lease in the state store, TTL 30 s, renewed each maintenance
    interval, loss fatal.** The epic rejected a KV lease as an *assignment*
    protocol; this is a *detection* mechanism on the store every member
    already has, and it assigns nothing. Loss-is-fatal is the narrowest
    fencing that makes #90's "two live owners" state bounded in time; a
    fencing token on `record_transition` (ADR-0011 A2) is still not added.
    30 s is chosen to cover a re-evaluation pass with a margin; it is not
    derived from a requirement.
19. **No new metric series.** ADR-0011 A13 closes the list at nine; the
    two lease records and `shards_claimed` are enough for an operator, and
    the telemetry epic can add counters when it renders `/metrics`.
20. **`end_offset(topic)` on `MessageBus` for readiness.** *(Rewritten
    2026-09-21, Amendment 1; was `last_offset`.)* ADR-0009 decision 4
    needs "the log end as it stood when `start()` began" for the trie and
    the detector; the bus is the only party that can read it. It is
    `async` on both implementations (a broker round trip on NATS), it is
    on the `MessageBus` protocol so that the trie epic types its bus by
    the interface rather than as `InMemoryBus | NatsBus`, and it is
    defined as the offset the next appended message will receive —
    `state.last_seq + 1` on NATS, the log length on memory — because that
    is the one number with the same meaning on a 1-based stream and a
    0-based list. Named here so the trie epic does not invent it. Original
    text: "`last_offset(topic)` on the bus objects for readiness. ADR-0009
    decision 4 needs "the log end as it stood when `start()` began" for
    the trie and the detector; the bus is the only party that can read it.
    Named here so the trie epic does not invent it; `InMemoryBus`
    implements it as the log length."
21. **`REDELIVERED` is logged at `WARNING` and not counted.** Same
    reasoning as ADR-0011 A19 for `UNCLAIMED`: it is a delivery event, not
    a content event; the record names the offset and delivery count.
22. **`message_id=None` publishes without deduplication.** Every producer
    in this repository passes an id; the parameter is optional so that a
    tool or test can publish raw bytes. Deriving an id from a hash of
    `value` when none is given was considered and rejected: silent dedup
    of identical bytes is a surprise for a test that publishes the same
    payload twice on purpose.
23. **One `subscribe()` per consumer instance.** The Kafka path allowed
    extending a subscription; nothing used it, and one-subscription-per-
    instance makes `close()` and `ack()` unambiguous.
24. **`ack()` rejects, rather than ignores, a message not delivered by
    this instance or already acknowledged, on both implementations.**
    Unifying on `ValueError` (unlike A20's `ValueError` vs
    `IllegalStateError` split) is possible because the consumer's own
    bookkeeping detects both cases before nats.py is touched.
25. **The trie is positional; the detector is durable.** The trie already
    owns a replay position (its snapshot) and must apply in log order
    after a restart, which a durable cannot promise (decision 9's
    redelivery-order point); the detector has no snapshot and its view is
    order-insensitive given the `sequence` rule. Both are constraints on
    unbuilt services and their epics may push back.
26. **Provisioner as a `tools/` package with its own Dockerfile.** The
    service images carry only their own dependencies; a tool image is the
    smallest thing that can run before the services. Alternatives (a
    `--provision` flag on every service; a `docker run` of the `nats`
    CLI image) were rejected as either a race between four provisioners
    or a second image whose licence the inventory would have to carry.
27. **`aiokafka` and every Kafka name are removed, not kept behind a
    flag.** The epic's "one real backend" constraint; keeping `kafka.py`
    would keep an untested module and its dependency in the lockfile.
28. **nats.py's API was read from the `main` branch, not the 2.16.0
    tag.** The tag path returned 404 from this environment (Sources); the
    `main` branch is the 2.x line (workspace layout, 2021 copyright,
    `PubAck` with batch fields), and the coder brief requires the
    signatures to be re-checked against the installed 2.16.0 before use.

## Consequences

* **Given up, as the epic priced in.** `auto` assignment and HPA-on-lag
  scaling (`deploy/k8s/README.md` must say the aggregator is scaled by
  disjoint shard sets, a StatefulSet being the natural fit); the broker
  drop-in (ADR-0012 Amendment 2); Kafka-specific ordering arguments
  (replaced by a simpler one). Single ownership per IP is now an operator
  invariant with detection (decision 7), not without.
* **Gained.** One integer replay position (decision 9); transport dedup on
  every publish, which makes a future in-process publish retry safe —
  ingest today maps a failed publish to 503 and relies on the agent to
  retry, and ADR-0003's `CHANGES` line records that such a retry is
  reported as a duplicate until the dedup claim lapses; a bounded retry
  inside `ObservationPublisher.publish` is now correct and is a named
  follow-up, not part of this ADR; rejected credentials distinguishable by
  exception type; a broker that starts in under a second; a compose file
  with one flag line instead of fourteen variables.
* **Bus package**: `interface.py` per decision 3; `memory.py` reworked
  (dedup by id, acknowledged sets, positional subscriptions, `close`,
  `end_offset` — Amendment 1);
  `topics.py` gains `partition_for`, `stream_name`, `subject`,
  `subject_filter`; `nats.py` new; `kafka.py` deleted; `__init__.py`
  re-exports `NatsBus`, `NatsProducer`, `NatsConsumer`, `TRANSIENT_ERRORS`,
  `MessageBus`, `partition_for` and drops the Kafka names;
  `pyproject.toml` swaps `aiokafka` for `nats-py>=2.16`; the root
  `pyproject.toml`'s `aiokafka.*` mypy override goes (nats-py ships type
  hints; if a `py.typed` marker turns out to be missing at 2.16.0 the
  override is re-pointed rather than dropped).
* **Store package**: `ShardStateStore.acquire_lease`/`release_lease`
  (decision 7) on both implementations; `MemoryShardStateStore(clock=...)`;
  the root `pyproject.toml` `dev` group carries `fakeredis[lua]` so that
  the Lua lease runs under fakeredis (Amendment 1; ADR-0012 Amendment 3
  carries the `lupa` inventory row).
* **Aggregator**: decision 8. The seven-outcome enum becomes eight. Tests
  written against rebalances (`_FetchGap`, `_GapConsumer`, `_GapBus`,
  `TestAMessageFetchedUnderARevokedClaim`, `TestTheMessageInHandReachesTheNextMember`,
  `TestRevokingAShard`, `TestTheRevocationCommitIsTheHandledPosition`,
  `_RecordingConsumer.commit`, `parse_shard_ids("auto")`) are rewritten
  against `stop()`/`start()` handovers and acknowledgements; the
  `test-author` brief lists them.
* **Ingest**: `app.py` uses `NatsBus`; `config.py` accepts `nats | memory`;
  `publisher.py` passes `message_id=envelope.event_id`.
* **Trie and detector epics**: decision 9's constraints; `tools/replay`
  uses positional subscriptions.
* **Spec.** `docs/spec/hammertime_spec_1.md` pointer notes that named
  Kafka mechanisms are reworded (§20, §22, §24, §33, §43, §47.2);
  `docs/spec/README.md` maps §19, §20/21, §22, §32/33 and §43 to this
  ADR. `docs/spec/integration-scenarios.md` §7 and `docs/runbook.md`
  ("consumer group lag") still mention aiokafka/consumer groups and are
  named in the hand-off report for their own edit.
* **`CHANGES`**: the lines are in the hand-off report; this ADR itself
  gets none (ADR-0012 assumption 16).
* **Not done here, named.** A publish retry inside `ObservationPublisher`;
  validation of `HAMMERTIME_BUS_BROKERS` (*done 2026-09-22, Amendment 4
  ruling S2: `validate_bus_url`*); consumer-config reconciliation
  in the provisioner; a byte cap on streams; a counter for redeliveries;
  unifying `replay_position` and `event_sequence`; the `integration` job's
  re-enable (#52). *Added 2026-09-22 (Amendment 4): authentication on the
  bus and the store (NATS `authorization`, Valkey `requirepass`) plumbed
  through the services — assumption 13 states the boundary and decision 11
  binds the ports to loopback, but no credential is configured; a public
  drain of a pull subscription's pending messages at `close()`, should
  nats-py expose one (ruling R1's residual); a counter or record for
  `PubAck.duplicate=True` at ingest, so a pre-published `event_id` is at
  least observable (the S1 audit's optional suggestion, not ruled on).*

## Sources

Read on 2026-09-21 unless stated. `docs.nats.io`, `nats.io`,
`nats-io.github.io`, `www.cncf.io`, `www.synadia.com`,
`www.linuxfoundation.org`, `lists.cncf.io` and `web.archive.org` are
blocked by this environment's egress proxy (`EGRESS_BLOCKED`); the NATS
documentation was therefore read from its source repository on GitHub
(`nats-io/nats.docs`), the ADRs from `nats-io/nats-architecture-and-design`,
and the client from `nats-io/nats.py`. Where a page was blocked or a path
404'd, that is stated.

* Measurements (prerequisites 4 and 5): performed by the top-level session
  on 2026-09-21 in the Claude Code remote environment (4 vCPU, 16 GiB,
  x86_64, Linux 6.18); the numbers, method and caveats are reproduced in
  Context from that session's report and were not re-run by the architect,
  who has no Bash.
* nats-py licence: `https://pypi.org/pypi/nats-py/json` — top-level session
  read `license_expression = "Apache-2.0"`, `license = null`,
  `license_files = ["LICENSE"]`, version 2.16.0; the architect's fetch the
  same day (summarised) reported version 2.16.0, `license_expression`
  Apache-2.0, `license_files ["LICENSE"]`, project URL
  `https://github.com/nats-io/nats.py`, `requires_python >=3.7`, and an
  optional `nkeys` extra.
* NATS ADR-8 (`.../adr/ADR-8.md`, "JetStream based Key-Value Stores",
  2021-06-30): the two quotes in prerequisite 2; bucket-to-stream mapping
  ("The main write bucket must be called `KV_<Bucket Name>`", subjects
  `$KV.<Bucket Name>.>`). NATS ADR-48 (`.../adr/ADR-48.md`, "TTL Support for
  Key-Value Buckets", 2025-04-09, Implemented, tag 2.11): "Do not accept a
  TTL for other API ... a TTL on `Put()` might mean older revisions could
  come back from the dead"; `Create()` and `Purge()` accept a TTL;
  `LimitMarkerTTL`.
* NATS ADR-42 (`.../adr/ADR-42.md`, "Pull Consumer Priority Groups",
  2024-05-14, Approved, tag 2.11): "We should not describe this in terms of
  exclusivity as there is no such guarantee, there will be times when one
  client thinks it is pinned while processing messages when it isn't
  anymore because the server switched." and "If no pulls from the pinned
  client are received within `PriorityTimeout` the server will switch
  again" — why `pinned_client` is not an ownership mechanism.
* NATS ADR-6 (`.../adr/ADR-6.md`, "Naming Rules", 2021-08-17, Approved):
  stream, consumer and account names are "filename-safe = (printable
  except dot, asterisk, gt, fwd-slash, backslash)+ maximum 255 characters";
  spaces not allowed. Taken from it: `-` for `.` in stream names.
* NATS ADR-30 (`.../adr/ADR-30.md`, "Subject Transform", 2022-07-17,
  Implemented): "`{{Partition(x,a,b,c,...)}}` ouputs a partition number
  between `0` and `x-1` assigned from a deterministic hashing of the value
  of wildcard-tokens `a`, `b` and `c`"; no algorithm named. NATS ADR-36
  (`.../adr/ADR-36.md`, "Subject Mapping Transforms in Streams",
  2023-02-10): a stream transform applies "at the ingres (input) of the
  stream ... and before limits are applied (and it gets persisted)";
  `subject_transform` with `src`/`dest` in the stream config. nats-server
  `server/subject_transform.go` (`main`): `getHashPartition` uses
  `fnv.New32a()` over the concatenated source tokens (`strings.Join(tokens,
  ".")` for the shorthand form) and `h.Sum32() % uint32(numBuckets)`; no
  compatibility comment. Taken from them: decision 1's choice of an
  in-repo FNV-1a hash and its rejection of a server-side transform.
* NATS ADR index (`.../main/README.md`): ADR numbers and titles used above
  (ADR-13 pull subscribe internals, ADR-17 ordered consumer, ADR-37
  simplification, ADR-43 per-message TTL, ADR-44 asset versioning, ADR-54,
  ADR-57).
* nats.docs, `nats-concepts/jetstream/consumers.md`: `DeliverPolicy`
  values (`DeliverAll`, `DeliverLast`, `DeliverNew`, `DeliverByStartSequence`
  via `OptStartSeq`, `DeliverByStartTime`, `DeliverLastPerSubject`);
  `AckPolicy` values (`AckExplicit`, `AckNone`, `AckAll`, `AckFlowControl`);
  "If an acknowledgment is not received in time, the message will be
  redelivered" (`AckWait`, `Backoff`, `MaxDeliver`); "The `Editable` column
  indicates the option can be edited after the consumer is created" —
  `Durable`, `DeliverPolicy`, `AckPolicy`, `MemoryStorage`, `ReplayPolicy`
  not editable; `AckWait`, `MaxAckPending`, `MaxDeliver`, `Backoff`,
  `Description`, `InactiveThreshold`, `Replicas`, `FilterSubjects`,
  `DeliverGroup` editable; pull consumers recommended "for scalability,
  detailed flow control or error handling". Taken from it: `seek()` cannot
  exist on a live consumer; decision 5's drift warning covers editable
  fields only.
* nats.docs, `nats-concepts/jetstream/streams.md`: retention policies
  (`LimitsPolicy` "based on the various limits ... `MaxMsgs`, `MaxBytes`,
  `MaxAge`, and `MaxMsgsPerSubject`"; `WorkQueuePolicy` "Each message can
  be consumed only once"; `InterestPolicy` "based on the consumer
  _interest_"); `DiscardOld`/`DiscardNew`; `MaxAge` in nanoseconds; storage
  `File`/`Memory`; `Replicas` "maximum 5"; "Streams support deduplication
  using a `Nats-Msg-Id` header and a sliding window"; `SubjectTransform`;
  non-editable after creation: `Name`, `Storage`, `Retention`,
  `MaxConsumers`, `DenyDelete`, `DenyPurge`, `AllowMsgTTL`,
  `AllowMsgCounter`, `AllowMsgSchedules`. Taken from it: decision 2's
  mutable/immutable split.
* nats.docs, `nats-concepts/jetstream/headers.md`: `Nats-Msg-Id`
  "Client-defined unique identifier for a message that will be used by the
  server apply de-duplication within the configured `Duplicate Window`";
  `Nats-Expected-Last-Subject-Sequence` "the server will reject a publish
  if the current sequence does not match for the message's subject";
  `Nats-TTL` "Requires the per message ttl flag to be set on the stream";
  `Nats-Sequence`, `Nats-Stream`, `Nats-Subject`, `Nats-Time-Stamp` as
  republish/direct-get headers.
* nats.docs, `using-nats/jetstream/model_deep_dive.md`: "set a
  `Nats-Msg-Id:1` header which tells JetStream to ensure we do not have
  duplicates of this message - we only consult the message ID not the
  body"; "default window to track duplicates in is 2 minutes";
  `AckExplicit` "requires every message to be specifically acknowledged,
  it's the only supported option for pull-based Consumers" (an older
  statement; the server's own error table, next item, narrows it to
  work-queue streams); `+ACK`, `-NAK` ("will not be processed now and
  processing can move onto the next message"), `+WPI`, `+TERM`; "exactly
  once" as dedup plus `AckSync()`.
* nats-server `server/errors.json` (`main`): `JSConsumerPullRequiresAckErr`
  (10084) "consumer in pull mode requires explicit ack policy on workqueue
  stream"; `JSStreamNotFoundErr` (10059) "stream not found";
  `JSConsumerNotFoundErr` (10014); `JSStreamNameExistErr` (10058) "stream
  name already in use with different configuration";
  `JSConsumerNameExistErr` (10013); `JSConsumerAlreadyExists` (10148);
  `JSConsumerFilterNotSubsetErr` (10093). Taken from it: decision 5's
  ack-free positional consumer is legal; decision 2's error mapping.
* nats.docs, `running-a-nats-service/nats_admin/monitoring/README.md`:
  `/healthz` with `js-enabled-only` "Returns an error if JetStream is
  disabled", `js-server-only`, deprecated `js-enabled`; responses "Success
  | 200 (OK)" and "Error | 400 (Bad Request)" with body `{"status":"ok"}`;
  monitoring port 8222 via `http_port` or `-m 8222`; `/varz` `mem` "the
  server's memory usage in bytes". Docker Official Image documentation
  (`docker-library/docs`, `nats/content.md`): ports "4222 is for clients",
  "8222 is an HTTP management port", "6222 is a routing port for
  clustering"; JetStream via "the -js flag" with "-v and -sd" for a
  persisted store; nothing about image variants or shells.
* Docker Hub `https://hub.docker.com/v2/repositories/library/nats/tags/2.15.0-alpine`:
  tag pushed 2026-09-18T01:57:51Z, 11 481 954 bytes, linux/amd64 digest
  `sha256:eda962d67930eda338222072d9a9f3818855d922ad224c399b0b01d251e9b91b`
  (architect's read agrees with the top-level session's). The scratch
  `nats:2.15.0` at 7.3 MB is the top-level session's read, not re-fetched.
* nats-server `LICENSE` (`main`): "Apache License / Version 2.0, January
  2004"; `README.md` line 5 (top-level session): "NATS is part of the Cloud
  Native Computing Foundation ([CNCF](https://cncf.io))". `GOVERNANCE.md`
  (`main`) only points at the separate NATS governance document and names
  no foundation, vendor or trademark.
* nats.py (`nats-io/nats.py`, `main`; the workspace layout
  `nats/src/nats/...` from its `pyproject.toml`; the paths
  `v2.16.0/nats/js/api.py` etc. returned 404): `nats/src/nats/js/api.py` —
  `StreamConfig` (`max_age`, `duplicate_window` in seconds, converted to
  nanoseconds; `discard` default `OLD`; `max_msgs_per_subject` default -1),
  `ConsumerConfig` (`deliver_policy` default `ALL`, `ack_policy` default
  `EXPLICIT`, `ack_wait`/`inactive_threshold` in seconds, `opt_start_seq`,
  `filter_subject`, `filter_subjects`, `max_ack_pending`, `max_deliver`,
  `num_replicas`), enums `DeliverPolicy` {`ALL`, `LAST`, `NEW`,
  `BY_START_SEQUENCE`, `BY_START_TIME`, `LAST_PER_SUBJECT`}, `AckPolicy`
  {`NONE`, `ALL`, `EXPLICIT`, `FLOW_CONTROL`}, `RetentionPolicy` {`LIMITS`,
  `INTEREST`, `WORK_QUEUE`}, `StorageType` {`FILE`, `MEMORY`},
  `DiscardPolicy` {`OLD`, `NEW`}; `PubAck(stream, seq, domain, duplicate,
  ...)`; `Header.MSG_ID = "Nats-Msg-Id"` and the `Nats-Expected-*` and
  `Nats-Rollup` constants. `nats/src/nats/js/client.py` — `publish(subject,
  payload, timeout, stream, headers, msg_ttl) -> PubAck`, raising
  `NoStreamResponseError` after the timeout when no stream responds;
  `pull_subscribe(subject, durable, stream, config, ...)` binds an existing
  durable via `consumer_info` "without config comparison" and otherwise
  `add_consumer`s; `pull_subscribe_bind`; `PullSubscription.fetch(batch,
  timeout, heartbeat, ...)` raising `asyncio.TimeoutError` with no server
  response and `FetchTimeoutError` after a heartbeat; ordered consumers
  reset on a gap or missed heartbeat by recreating with
  `DeliverPolicy.BY_START_SEQUENCE` "at the last seen stream sequence plus
  one"; `JetStreamManager.add_stream/update_stream/stream_info/
  find_stream_name_by_subject/add_consumer/consumer_info/delete_consumer`,
  `NotFoundError` (10059) and `BadRequestError` (10058).
  `nats/src/nats/aio/msg.py` — `ack`, `ack_sync` ("waits for the
  acknowledgement to be processed by the server"), `nak(delay)`,
  `in_progress`, `term`; `MsgAlreadyAckdError` on a second ack;
  `Metadata(sequence.stream, sequence.consumer, num_delivered, num_pending,
  timestamp, stream, consumer, domain)` parsed from the `$JS.ACK` reply
  subject. `nats/src/nats/errors.py` — `TimeoutError(Error,
  asyncio.TimeoutError)`, `NoServersError`, `ConnectionClosedError`,
  `ConnectionReconnectingError`, `StaleConnectionError`,
  `MsgAlreadyAckdError`, `NotJSMessageError`, and the separate
  `AuthorizationError`, `InvalidUserCredentialsError`, `SecureConn*`
  classes, all subclassing `Error`. `nats/src/nats/js/errors.py` —
  `APIError.from_error` mapping 503/500/423/404/400 to
  `ServiceUnavailableError`/`ServerError`/`PinIdMismatchError`/`NotFoundError`/
  `BadRequestError`; `NoStreamResponseError` "nats: no response from
  stream"; `FetchTimeoutError(nats.errors.TimeoutError)`. These readings
  are of `main`, not the pinned 2.16.0 (assumption 28).
* Repository facts: `packages/hammertime-bus/src/hammertime/bus/interface.py`
  (lines 17-25, 44-46, 138-166), `memory.py`, `kafka.py` (line 256),
  `topics.py`; `services/aggregator/src/hammertime/aggregator/service.py`
  (`_KafkaBus`, `_TRANSIENT_BUS_ERRORS`), `sharding/assignment.py`,
  `worker.py` (`_Fetched`, `_handle`), `config.py` (`parse_shard_ids`,
  `_ALLOWED_BUS_KINDS`); `services/ingest/src/hammertime/ingest/app.py`,
  `config.py`, `publisher.py`;
  `packages/hammertime-store/src/hammertime/store/redis.py` (lines 164,
  226, 259-290), `memory.py`, `interface.py`;
  `packages/hammertime-core/src/hammertime/core/events/envelope.py`
  (`event_id`); `deploy/docker-compose.yml`, `.env.example`, `Makefile`,
  `.github/workflows/ci.yml`, `deploy/k8s/README.md`, `docs/runbook.md`,
  `docs/spec/integration-scenarios.md`; the aggregator and bus test files
  named under Consequences.

Read on 2026-09-21 for Amendment 1:

* `https://raw.githubusercontent.com/nats-io/nats.py/main/nats/src/nats/aio/client.py`
  (summarised by the fetch tool; the `main` branch, not the 2.16.0 tag —
  assumption 28's caveat applies): `Client.connect()`'s keyword list
  (`servers`, `error_cb`, ..., `allow_reconnect: bool = True`,
  `connect_timeout`, `reconnect_time_wait`, `max_reconnect_attempts`,
  ...) has no `retry_on_failed_connect`; the initial connect loop is
  `while True: try: await self._select_next_server(); await
  self._process_connect_init(); ... break; except errors.NoServersError
  as e: if self.options["max_reconnect_attempts"] < 0: continue;
  self._err = e; raise e`; `self.options: Dict[str, Any] = {}` is a plain
  attribute, and the reconnect path reads
  `self.options["allow_reconnect"]` and
  `self.options["max_reconnect_attempts"]` at runtime; the default error
  callback is `_logger.error("nats: encountered error", exc_info=ex)`.
  Taken from it: ruling C1's mechanism and ruling C2's hook.
* `https://pypi.org/pypi/fakeredis/json`: version 2.38.0,
  `license_expression` `BSD-3-Clause`, `provides_extra` includes `lua`,
  `requires_dist` carries `lupa>=2.1; extra == "lua"`.
* `https://pypi.org/pypi/lupa/json` and `.../lupa/2.8/json`: version
  2.8, legacy `license` `"MIT style"`, no `license_expression`,
  `license_files = ["LICENSE.txt"]`, author Stefan Behnel; wheels
  `lupa-2.8-cp312-cp312-manylinux2014_x86_64...` and the `aarch64`
  equivalent are present (uploaded 2026-04-15) and no sdist is listed for
  2.8 — *corrected 2026-09-21 (Amendment 2): the page does list
  `lupa-2.8.tar.gz`; the summarised fetch omitted it. See the Amendment 2
  Sources block.* `https://raw.githubusercontent.com/scoder/lupa/master/LICENSE.txt`
  (summarised): the MIT licence, "Copyright (c) 2010-2017 Stefan Behnel",
  followed by the MIT licence of the bundled Lua, "Copyright © 1994–2017
  Lua.org, PUC-Rio". Taken from them: ruling C5.1 and ADR-0012 Amendment
  3's inventory row.
* The C1 and T1 reports as relayed by the top-level session (the pytest
  counts, the fakeredis `unknown command 'eval'` cause, the two gap
  lists) and the branch's `nats.py`, `memory.py`, `redis.py`,
  `memory.py` (store), `test_memory_bus.py`, `test_shard_state.py`,
  `test_sharding.py`, `test_worker.py` and `test_config.py` as read by
  the architect; the architect did not run the suite.

## Amendment 1 (2026-09-21) — the gaps C1 and T1 hit, ruled

Why: `coder` (brief C1) and `test-author` (brief T1) each worked from this
ADR without reading the other's output, as the process requires, and each
resolved a set of under-specified points by its own reading. When T1's
tests were run against C1's code, 338 passed and 29 failed: 28 because
decision 7's Lua `EVAL` cannot run under fakeredis without its `lua`
extra, and 1 because T1 read "below the first offset" as `-1` on the
0-based memory log while C1 rejects a negative `start_offset`. The coder
reported nine further points it had ruled on alone and the test-author
fifteen; several of them are interface questions the next briefs (C2, C3)
would otherwise inherit unsettled. This amendment rules on every one,
records the ruling in place, and lists the edits here. Where both readings
were defensible the tie is broken toward the reading that keeps the two
implementations interchangeable behind the interface — the property
ADR-0012 decision 2 item 5(c) makes load-bearing.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Gained the "amended 2026-09-21" clause.
* **Decision 2, `ensure_streams` bullet.** Appended: "It inspects every
  stream in `specs` before it creates or updates any of them ...".
* **Decision 3, interface block.** `MessageBus` gained `async def
  end_offset(self, topic: str) -> int`; the "Added:" line names it.
* **Decision 3, `subscribe` bullets.** The `start_offset` bullet gained
  the negative-value rule and the `start_offset=0` sentence; a new bullet
  rules the unregistered topic on `NatsConsumer.subscribe()`.
* **Decision 3, `ack` paragraph.** Appended the check order, the
  duplicate-within-one-call rule and the "earlier call" reading of
  "already acknowledged".
* **Decision 3, `NatsBus` block.** Gained `end_offset`; a new paragraph
  after it rules `producer()`/`consumer()` before `start()`.
* **Decision 3, connection paragraph, rewritten.** Was: "The connection
  is opened with `retry_on_failed_connect=False` (so `connect()` fails
  fast and `connect_with_retry` owns the startup schedule),
  `allow_reconnect=True` and `max_reconnect_attempts=-1` (a mid-run
  outage is reconnected indefinitely; an in-flight `publish` or `fetch`
  during it raises `nats.errors.TimeoutError`, which the calling service
  treats exactly as it treated an aiokafka failure today — ingest answers
  503, the aggregator's `run()` fails and the process exits 1; assumption
  14)."
* **Decision 3, `hammertime.bus.memory` paragraph.** Appended the
  `InMemoryBus.end_offset` sentence.
* **Decision 5, fetching paragraph.** Appended the outage-timeout rule.
* **Decision 6, `auto` bullet.** Appended the exact message and the four
  substrings tests pin.
* **Decision 7.** The `on_assigned` bullet gained the strictly-sequential
  per-shard sentence; the store paragraph gained the lease-liveness
  boundary and the `fakeredis[lua]` sentence.
* **Decision 8.** The `handled_position` line of the block gained "or if
  shard is not held; never reset by a commit"; the `commit_handled`
  bullet gained the no-deduplication sentence; two bullets were added
  (`handled_position`, `on_assigned(frozenset())`); the `stop()` bullet
  gained the "does not call `consumer.close()`" sentence.
* **Decision 9, trie bullet.** Was: "(or `start_offset=1` with no
  snapshot)". Now `start_offset=0`, with the correction note.
* **Decision 9, readiness bullet, rewritten.** Was: "the log end is the
  stream's last sequence, read from `stream_info` at `start()` — the bus
  exposes it as `NatsBus.last_offset(topic) -> int` /
  `InMemoryBus.last_offset(topic)` (assumption 20)."
* **Decision 10.** A paragraph naming the `AggregatorSettings` fields and
  the unpinned error precedence was inserted before "No key is removed."
* **Assumptions 14 and 20, rewritten**; each quotes its original text.
* **Consequences.** The bus bullet names `end_offset`; the store bullet
  names `fakeredis[lua]`.
* **Sources.** A dated block for this amendment.

**Rulings.** "C" items are the coder's report, numbered as it numbered
them ((2).1, (2).4, (5).1-(5).9, written C1, C2, C5.1-C5.9); "T" items
are the test-author's fifteen.

* **C1 (connect options).** C1's mechanism is right and is now the
  decision 3 text: fail-fast connect via `allow_reconnect=False,
  max_reconnect_attempts=1, connect_timeout=2`, then the two `options`
  keys switched on the live client. The ADR was wrong: no
  `retry_on_failed_connect` exists in nats-py.
* **C2 (`nats_client_error`).** Named and kept: `WARNING nats_client_error
  error=<exception>`, one line, no traceback, installed as `error_cb`.
* **C5.1 (Lua under fakeredis).** Keep decision 7's Lua; add
  `fakeredis[lua]` to the `dev` group; `lupa` gets its Class 2 row
  (ADR-0012 Amendment 3). `WATCH`/`MULTI` was not chosen — assumption 30.
* **C5.2 (`last_offset`).** Neither reading survives: renamed
  `end_offset`, `async` on both, on the `MessageBus` protocol, defined as
  the offset the next appended message will receive (`last_seq + 1` on
  NATS, the log length on memory). C1's `async` was right; its NATS value
  (`last_seq`) was off by one against its own memory value.
* **C5.3 (fetch during an outage).** C1 right: a fetch timeout is always
  the idle path; the iterator raises only on a non-timeout failure.
  Assumption 14's "fetch fails the caller" is withdrawn.
* **C5.4 (`ack([])` precedence).** C1 right: closed -> `ValueError`,
  positional -> `ValueError`, then empty -> return. Before `subscribe()`
  an empty iterable returns normally and any message is a `ValueError`.
* **C5.5 / T1 (duplicates in one `ack()`).** C1 and T1 both right: a
  message named twice in one call is acknowledged once; "already
  acknowledged" means by an earlier call; `commit_handled()` does not
  deduplicate.
* **C5.6 (unregistered topic on `NatsConsumer.subscribe()`).** C1 right:
  `KeyError`, before the broker is contacted and before the listener.
* **C5.7 / T2 (negative `start_offset`).** C1 right: `ValueError` on
  both; `opt_start_seq = max(start_offset, 1)` on NATS; `start_offset=0`
  is the portable start. T1's `first.offset - 1` test is withdrawn (it
  reads the ADR literally, but the literal reading makes `-1` a valid
  argument on one backend and a meaningless one on the other). Decision
  9's `start_offset=1` for the no-snapshot trie was a genuine ADR error
  the same rule exposes and is corrected to `0`.
* **C5.8 (`producer()`/`consumer()` before `start()`).** C1 right, and
  required by ADR-0009's object-graph-then-start shape.
* **C5.9 (`ensure_streams` inspects all first).** C1 right.
* **T3 (`handled_position` on an unheld partition).** `None`; the read
  surface is total, the write surface raises.
* **T4 (`handled_position` after `commit_handled()`).** T1 right: it
  persists; only the list is cleared.
* **T5 (`on_assigned(frozenset())`).** `ValueError` (assumption 31).
* **T6 (acquire/load interleaving).** Strictly sequential per shard:
  acquire, load, build, then the next shard. T1's weaker assertion is
  compatible and may stay.
* **T7 (error precedence in `load_settings`).** Not pinned;
  first-in-load-order per ADR-0009 A12. T1 right to leave it.
* **T8 (`stop()` and `consumer.close()`).** T1 right: `stop()` does not
  close the consumer; the service closes the bus afterwards.
* **T9 (settings field names).** T1 right: `bus_kind`, `bus_brokers`,
  `member_id`, `lease_ttl_s`, `maintenance_interval_s`; recorded in
  decision 10.
* **T10 (`parse_shard_ids("auto")` message).** Exact text given in
  decision 6; tests pin `HAMMERTIME_SHARD_IDS`, `all`, `ADR-0013` and one
  explicit-set example. T1's two substrings were right and remain
  sufficient; the ADR now also names the other two.
* **T11 (liveness at exactly `expires_at`).** Expired: live iff `now <
  expires_at`, both backends. T1 need not pin it.
* **T12 (real sleeps against fakeredis; `EX ceil`).** Accepted; the
  precedent is `test_dedup.py`, and `ceil` is decision 7's "rounded up".
* **T13 (`_TappedBus`/`_PrefetchBus`).** Accepted; that harness shape is
  what decision 3's `ack()` rule implies for any test that acknowledges.
* **T14 (`bus._logs` read by test helpers).** Tolerated as the
  pre-existing pattern; a public read helper on `InMemoryBus` is a
  candidate follow-up, not required here.
* **T15 (`:4222` in `_FailingProducer`).** Cosmetic; accepted.

Assumptions made by this amendment (push back individually; numbering
continues the ADR's list):

29. **`nats_client_error` at `WARNING`, one line per event.** It fires
    once per refused attempt during a startup wait, alongside
    `connect_with_retry`'s own `dependency_unavailable` record, so a
    startup wait produces two records per attempt. `DEBUG` would hide the
    mid-run case (a reconnect attempt during an outage), which has no
    other record; the duplication at startup is accepted rather than
    adding logic to tell the two apart.
30. **Lua kept over `WATCH`/`MULTI`, at the cost of `lupa` in the test
    process.** `redis.py`'s `record_transition` chose `WATCH` partly to
    avoid `lupa`; that reason lapses now that `lupa` is present (its
    other reason, exact `int` comparison above 2**53, still holds for the
    sequence and does not apply to a string-valued owner key). The lease
    is kept in Lua because its correctness under contention is the case
    it exists for (#90, two members racing for one shard) and a single
    server-side step has no retry loop to reason about; `release_lease`'s
    "delete iff mine" is the standard scripted idiom; and the cost is one
    MIT, test-process dependency. *Corrected 2026-09-21 (Amendment 2):
    the parenthesis that followed — "wheel-only ... (no sdist for 2.8 is
    listed on PyPI, so a platform without a `cp312` manylinux/macOS/Windows
    wheel cannot install it — CI runners and the reference
    `python:3.12-slim` build have x86_64 wheels; the service images do not
    install the `dev` group)" — was wrong: PyPI lists `lupa-2.8.tar.gz`
    (sha256 `d8022641...`, 6 156 370 bytes, uploaded 2026-04-15), which
    `uv lock` recorded, so a platform without a wheel builds the C
    extension from source rather than failing to install. The lock
    resolves lupa 2.8; CI installs the `cp312-manylinux_2_17_x86_64`
    wheel; the service images still do not install the `dev` group.
    ADR-0012 Amendment 3 assumption 2 is corrected the same way.*
31. **`on_assigned(frozenset())` is a `ValueError`, not a no-op.** A no-op
    would let a directly constructed `ShardClaims` hold nothing and sit
    idle "ready", the state ADR-0011 A3 refuses at the bus; refusing the
    direct call keeps the two paths consistent. The old code's
    `no_shards_assigned` warning-and-return was ADR-0011 assumption 21's
    group-managed steady state, which no longer exists.
32. **`handled_position(p)` is `None` for an unheld `p`.** Chosen over
    `KeyError` to match `window(p)`, the other read on the same object,
    and because the worker only ever asks after the `UNCLAIMED` check.
33. **`end_offset` as "the next offset", on the protocol.** "Last offset"
    has no value for an empty log on a 0-based list (`-1` would be a
    sentinel) and a different one on a 1-based stream (`0`); "next offset"
    is `0` and `1` respectively and makes the readiness test one
    inequality on both. Putting it on `MessageBus` widens the protocol by
    one method; every implementation and test double in the repository
    subclasses `InMemoryBus`, so nothing else needs to change.
34. **The exact `auto` message.** The ADR pinned four facts the message
    must carry; the wording is the architect's, and tests pin substrings
    so the wording can be polished without a test change.
35. **Strictly sequential per-shard claiming.** The ADR said acquire
    before load per shard and sorted order for acquires; batching all
    acquires then all loads would also satisfy that text. Sequential is
    chosen because it is what C1 implemented and because it bounds what a
    refusal has to undo to "the shards before this one".
36. **A lease expires at exactly `expires_at`.** Matches Redis `EX`
    semantics and the memory implementation; the boundary is a
    second-granularity detail no caller depends on.
37. **A member idle through an outage survives it.** Follows from C5.3;
    it means a member can be "running" against a dead broker for as long
    as its shards are idle, with only the store lease renewals proving it
    alive. ADR-0009's readiness endpoint does not probe the bus
    connection today; whether it should is left to the telemetry epic.
38. **`stop()` does not close the consumer.** Decision 8 already implied
    it; stated so that C2 does not add a `close()` inside `stop()` and
    break `_TappedBus`-style tests that read the stream after `stop()`.
39. **Error precedence in `load_settings` stays unpinned.** As ADR-0009
    A12 chose for ingest.
40. **T1's harness choices are accepted as test-side judgement**, not
    interface: `_TappedBus`, `_PrefetchBus`, the `bus._logs` reads and the
    0.6-1.3 s sleeps against fakeredis. None constrains the code.

## Amendment 2 (2026-09-21) — bus URLs with userinfo in log records; the provisioning tool's surface; two corrections

Why: three follow-ups from Amendment 1's dispatches. (1) The supervisor
review of the provisioning tool (brief C4) found that both its
`provision_failed` records log `servers=args.servers` verbatim, so a
`--servers nats://user:pass@host:4222` would put the password in a log
line; the services' `starting` record logs `bus_brokers` verbatim under
ADR-0009 A7's "broker addresses are not secrets", which was true of a
Kafka `host:port` list and is not true of a NATS URL. Nothing in the spec
or the ADRs said whether a bus URL may carry credentials at all, and the
tool's records and test seams were not pinned anywhere, so its tests had
nothing to be written from. (2) The C1-followup coder found that `uv lock`
recorded an sdist for lupa 2.8, which ADR-0012 Amendment 3 assumption 2
and this ADR's assumption 30 said did not exist. (3) ADR-0010 Amendment 1
item 4 still said `last_offset(topic)`.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Gained the "amended again 2026-09-21" clause.
* **Decision 2, CLI bullet.** Appended the paragraph that names the
  tool's public surface (`build_parser`, `provision`, `main`,
  `TRANSIENT_PROVISION_ERRORS`, `DEFAULT_REPLICAS`, `DEFAULT_TIMEOUT_S`),
  its argument rules, its three failure records with `bus_endpoints`, and
  its three test seams. Nothing before it changed.
* **Decision 3, `hammertime.bus.nats` block.** Gained `bus_endpoints`, and
  a paragraph after the block defines it with examples.
* **Decision 10, `HAMMERTIME_BUS_BROKERS` row.** Appended the dated
  userinfo ruling.
* **Assumption 13.** Appended the dated note.
* **Assumption 30.** Was "... the cost is one MIT, wheel-only,
  test-process dependency (no sdist for 2.8 is listed on PyPI, so a
  platform without a `cp312` manylinux/macOS/Windows wheel cannot install
  it — CI runners and the reference `python:3.12-slim` build have x86_64
  wheels; the service images do not install the `dev` group)." Now
  corrected in place, the original quoted there.
* **Sources, Amendment 1 block, lupa item.** Dated correction appended
  to "no sdist is listed for 2.8".
* **Outside this file** (same day, same change set): ADR-0012 Amendment 3
  assumption 2, its Sources lupa item, the Class 2 `lupa` row and
  assumption 4 (corrected; the row now records the locked version);
  ADR-0010 Amendment 1 item 4 (`last_offset` -> `end_offset`, with the
  readiness inequality of decision 9). **Not edited, and needing a dated
  pointer in a later dispatch that may touch them:** ADR-0009 A7's table
  row for `starting` (`bus_brokers`) and its sentence "`bus_brokers` is
  logged verbatim (broker addresses are not secrets, decision 5 step
  3)", and spec §47.7's enumeration "not the userinfo of a store URL" —
  both superseded by ruling 1 below. This dispatch's scope excluded them.

**Rulings.**

1. **`HAMMERTIME_BUS_BROKERS` and `--servers` MAY carry userinfo.** In
   the two forms nats-py reads from a server URL — `user:password@` and
   `token@` (a username with no password is sent as `auth_token`;
   Sources) — and in no other. The value is handed to `nats.connect` as
   given; nothing in this repository parses it for any other purpose than
   ruling 2. The alternative, refusing userinfo at `load_settings` and
   requiring credentials through separate keys, was rejected: it needs a
   validator that assumption 15 deliberately does not add, it would
   diverge from the store side (`HAMMERTIME_REDIS_URL` may carry a
   password and is reduced for logging, not refused), and it would still
   need ruling 2 for the value's journey from environment to refusal.
2. **No log record may carry it.** The rendered text of every record —
   any service, any tool, any event — MUST NOT contain the userinfo of a
   bus URL. This is spec §47.7's "no record — of any event — may contain
   a credential" applied to a credential the enumeration did not name:
   the general clause already binds; the enumeration ("not the userinfo
   of a store URL") is extended by one item, and ADR-0009 A7's
   "`bus_brokers` is logged verbatim" is **superseded**. So it is not a
   new principle, but it is a new obligation on two existing records and
   one new tool.
3. **The reduction is `hammertime.bus.nats.bus_endpoints`, and the field
   is `bus_endpoints`.** Defined in decision 3 (as amended): scheme plus
   the authority after its last `@`, never raising, `<unparseable>` for
   an entry `urlsplit` refuses. One helper in the bus package rather than
   three inline `urlsplit` expressions — the store side's `store_endpoint`
   is inline in two services and ADR-0009 A12 item 4 already lists its
   duplication and its `hostname:port/path` quirks as a ticket; the bus
   has three callers (ingest, aggregator, the tool) from the start and
   the tool already imports from `hammertime.bus.nats`. The store's
   inline reduction is not reused because it keeps the path and consults
   `.port`, both wrong here (ruling 3's "never raising"). The services'
   `starting` record logs `bus_endpoints` **in place of** `bus_brokers`:
   a distinct name for a reduced value, as `store_endpoint` is distinct
   from the URL, so no reader mistakes the field for the configured
   value. The tool's two `provision_failed` records log `bus_endpoints`
   in place of `servers`.
4. **Where the edits land.** The tool and the helper are one `coder`
   dispatch now (the tool is on the branch and logs the value today).
   The services' `startup_fields()` changes belong in the C2 (aggregator)
   and C3 (ingest) briefs, which have not been dispatched and already
   rewrite those services' bus wiring and their `test_config.py`/service
   tests; a separate dispatch would touch the same files twice. Both
   briefs gain the paragraph in the hand-off report, and their
   test-author counterparts gain the corresponding `starting` assertion.
   The `CHANGES` line for the rename is written by C2/C3 (one line,
   whichever lands first): "Log `bus_endpoints` (bus URLs without
   userinfo) in the `starting` record in place of `bus_brokers`". The
   tool gets no `CHANGES` line: it has not reached `master`, so its
   records have never been observable.
5. **The tool's surface, records and seams are pinned in decision 2** so
   that `test-author` writes its tests from this ADR. The names are the
   ones C4 implemented; recording them changes no code.
6. **lupa sdist.** Assumption 30 and ADR-0012 Amendment 3 assumption 2
   are corrected: an sdist exists, so a wheel-less platform builds from
   source. The lease stays in Lua; nothing else follows.
7. **ADR-0010 Amendment 1 item 4** is corrected to `end_offset` with
   decision 9's inequality.

Assumptions made by this amendment (push back individually; numbering
continues the ADR's list):

41. **Userinfo is allowed rather than refused.** Ruling 1's reasons; the
    judgement call is that parity with the store side and no new
    validator outweigh the operational preference for credentials in
    separate variables. A deployment that wants the latter can still use
    nats-py's `user=`/`password=`/`token=`/`user_credentials=` connect
    arguments (Sources) once someone plumbs them through `NatsBus`; that
    is a follow-up, not ruled here, and would not change ruling 2.
42. **The username is dropped along with the password.** Because nats-py
    treats a lone username as a token. The store's reduction keeps no
    username either.
43. **Scheme and host are kept; path, query and fragment are dropped.**
    The scheme matters to an operator (`tls://` versus `nats://`); the
    rest carries nothing NATS reads. A query parameter could in principle
    carry a secret in some other URL grammar; dropping it costs nothing.
44. **`<unparseable>` for an entry `urlsplit` refuses.** A fixed string
    rather than a best-effort rendering, on ADR-0009 A12 item 4's
    argument that a malformed value is exactly the input a redaction
    cannot be trusted on. The entry count is still visible (one
    `<unparseable>` per bad entry).
45. **Scheme-less entries are read as `nats://<entry>`**, mirroring
    nats-py's `_parse_server_uri` (`elif ":" in connect_url: nats://...`;
    a bare host also gets `:4222` there, which `bus_endpoints` does not
    add — the port is not invented for a log line). Read from `main`,
    not the 2.16.0 tag (assumption 28's caveat).
46. **Exception text is not scanned.** Ruling 2 binds the fields the code
    writes; `error=str(exc)` in `dependency_unavailable`,
    `provision_failed`, `start_failed` and `nats_client_error` carries
    whatever nats-py put in the exception. For an unreachable or
    refusing server that text names no URL (`nats: no servers available
    for connection`, an `OSError` with host and port). For a **malformed**
    URL nats-py's own `urlparse(...).port` can raise a `ValueError`
    echoing the userinfo (`redis://user:secret/0`'s hazard, ADR-0009 A12
    item 4, applies verbatim to `nats://user:secret/0`), and that
    `ValueError` is not in any transient tuple, so a service reports it
    as `start_failed error=... exception=<traceback>`. That residual is
    named, not closed: it is the reason the "validation of
    `HAMMERTIME_BUS_BROKERS`" follow-up under Consequences now has a
    security argument as well as an exit-code one, and a driver-parser
    helper in the shape of `validate_redis_url` (raising a message that
    repeats nothing of the value) is the recommended form. *Closed
    2026-09-22 (Amendment 4 ruling S2): the S1 audit confirmed the path at
    the pinned version — nats-py's fixed-text `errors.Error` chains the
    `ValueError` as `__context__`, and `format_exc_info` prints the chain
    — and `validate_bus_url` plus `_connect`'s `from None` re-raise close
    both the value's route to nats-py and the chained text's route to
    `start_failed`.*
47. **Field renamed (`bus_endpoints`) rather than value replaced under
    `bus_brokers`.** A reader of `starting` should not have to know
    whether the field is the configured value or a reduction of it. The
    cost is one `CHANGES` line and the test updates C2/C3 make anyway.
48. **The tool's `provision_complete` record is not pinned.** It is the
    coder's summary line (`streams`, `created`, `updated`, `unchanged`,
    `replicas`) and may change; tests pin `stream_provisioned`, the three
    failure records and the exit codes.
49. **The tool's tests live in
    `tools/provision/src/hammertime/tools/provision/tests/`**, the
    package-internal layout `tools/agent-token` established (the only
    tool with tests today; `tools/replay` has none), collected through
    the root `testpaths`' `tools` entry.
50. **The sdist digest and size are the coder's reading of `uv.lock`.**
    The architect's fetch of `https://pypi.org/pypi/lupa/2.8/json`
    confirmed the filename `lupa-2.8.tar.gz` and was truncated before the
    digest; the eight-character prefix `d8022641`, 6 156 370 bytes and
    the 2026-04-15 upload are recorded as relayed.

Read on 2026-09-21 for Amendment 2:

* `https://raw.githubusercontent.com/nats-io/nats.py/main/nats/src/nats/aio/client.py`
  (summarised by the fetch tool; `main`, not the 2.16.0 tag):
  `_setup_server_pool` calls `_parse_server_uri` per entry for both a
  `str` and a `list`; `_parse_server_uri` keeps an entry containing
  `nats://`, `tls://`, `ws://` or `wss://` as given, prefixes `nats://`
  to one containing `:`, and renders a bare host as `nats://<host>:4222`;
  the CONNECT options read `uri.username`/`uri.password` from the
  current server's URL — `if self._current_server.uri.password is None:
  options["auth_token"] = self._current_server.uri.username` else
  `options["user"]`/`options["pass"]`; `connect()` also takes `user`,
  `password`, `token`, `user_credentials`, `nkeys_seed`,
  `nkeys_seed_str`, all defaulting to `None`. Taken from it: rulings 1
  and 3, assumptions 42 and 45.
* `https://pypi.org/pypi/lupa/2.8/json` (summarised and truncated by the
  fetch tool): the `urls` array contains an sdist entry `lupa-2.8.tar.gz`
  and, among the wheels, `lupa-2.8-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl`
  and `lupa-2.8-cp312-abi3-musllinux_1_2_x86_64.whl`; the sdist's digest,
  size and upload time were cut off. Taken from it: ruling 6, assumption
  50.
* Repository facts: `tools/provision/src/hammertime/tools/provision/__main__.py`
  (the two `provision_failed` records at the `TRANSIENT_PROVISION_ERRORS`
  and `nats.errors.Error` handlers; `_connect` calling `nats.connect`;
  `provision`'s signature); `packages/hammertime-bus/src/hammertime/bus/nats.py`
  (`NatsBus.__init__`'s split of `servers`; `TRANSIENT_ERRORS`);
  `services/aggregator/src/hammertime/aggregator/service.py` lines
  261-279 and `services/ingest/src/hammertime/ingest/service.py` lines
  183-197 (`startup_fields`, `bus_brokers` verbatim, `store_endpoint`
  inline); `packages/hammertime-store/src/hammertime/store/url.py`
  (`validate_redis_url`: validation only, no reduction helper);
  `packages/hammertime-core/src/hammertime/core/runtime.py`
  (`connect_with_retry`: 0.5 s initial delay doubling to 5 s; raises
  when `remaining <= delay`); `packages/hammertime-core/src/hammertime/core/telemetry/logging.py`
  (`configure_logging` is idempotent and binds `sys.stdout` at call
  time); `tools/agent-token/src/hammertime/tools/agent_token/tests/test_cli.py`
  (the tools' test layout); ADR-0009 A7 (the `starting` row and the
  verbatim sentence), A12 item 4 (the `.port` hazard); spec §47.7.

## Amendment 3 (2026-09-21) — four points the workers ruled on alone after Amendment 2; the pending pointer edits

Why: the C2 (aggregator) and T1-followup (bus tests) dispatches each
resolved a point this ADR under-specified, by their own reading, and the
top-level session brought the four to the architect for a ruling rather
than let them stand as implementation facts. The same change set makes
the three pointer edits Amendment 2 listed as "not edited, and needing a
dated pointer in a later dispatch": ADR-0009 A7, spec §47.7 and
`docs/spec/integration-scenarios.md` §7. Two of the four rulings confirm
the worker's reading (a, b); one confirms it and closes the residual it
left open, which is a two-line change in the worker (c); one confirms it
and records the mechanism it silently relied on (d). C2's judgement 1 is
confirmed as well. No decision changes in substance.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Gained the "amended a third time 2026-09-21" clause.
* **Decision 3, `ack` paragraph.** Appended the italic "Closed is checked
  first and is absorbing" sentence after "`MsgAlreadyAckdError` is never
  reached." Nothing before it changed.
* **Decision 3, `bus_endpoints` paragraph.** Appended the italic "One
  result element per input element" sentence after "each stripped."
* **Decision 7, `on_assigned` bullet.** Appended the italic "A `load()`
  that raises inside this call is a fault, not a refusal" sentence.
* **Decision 7, `renew_leases` bullet.** Appended the italic "After
  `stop()` has released the leases, `run_maintenance()` renews nothing and
  sweeps nothing" sentence.
* **Decision 7, `release()` bullet.** Appended the italic "`release()`
  empties the set of shards whose lease this member holds" sentence.
* **Decision 8, `stop()` bullet.** Was: "windows are kept in memory
  (nothing reads them after `stop()` except tests), and the service closes
  the bus". Now the parenthesis is followed by "but the leases are not —
  `release()` empties the held-lease set ... re-takes nothing and sweeps
  nothing (amended 2026-09-21, Amendment 3 ruling (c)) — and the service
  closes the bus".
* **Outside this file** (same change set):
  * ADR-0009 gains Amendment 5, which edits its status line, A7's
    `starting` row (`bus_brokers` -> `bus_endpoints`, with the value and
    the date in a parenthesis) and A7's no-credential paragraph (the
    sentence "`bus_brokers` is logged verbatim (broker addresses are not
    secrets, decision 5 step 3)." removed and quoted in a dated blockquote
    that extends the rule to `HAMMERTIME_BUS_BROKERS`). Amendment 2's
    pending item is closed.
  * Spec §47.7, one sentence. Was: "No record — of any event — may contain
    a credential: not the agent token key, not a bearer token, not the
    userinfo of a store URL." Now continues ", and not the userinfo of a
    bus URL — the `starting` record and the provisioning tool carry
    `bus_endpoints`, the reduction that drops it, in place of the
    configured value (amended 2026-09-21; ADR-0013 Amendment 2 ruling 2)."
  * `docs/spec/integration-scenarios.md` §7, two bullets, wording only.
    The first bullet's "Kafka, Redis and the compose stack" is now "NATS
    JetStream, Valkey and the compose stack". The second bullet, which
    described a coordinator-driven handover (member A fetches, "the group
    rebalances", "the revoke driven directly", "resuming at the committed
    handled position", `TestAMessageFetchedUnderARevokedClaim`, "the test
    brief M4 dispatches", "aiokafka accepts `commit(offsets)` inside
    `on_partitions_revoked`", "a reassigned partition's fetch resumes from
    the committed offset on a *live* consumer, and the rebalance ordering
    itself", "disabled pending #26", "M4's closure"), now describes the
    same scenario in this ADR's terms: A is stopped and B started with A's
    shard; `stop()` is the final flush-and-acknowledge then the lease
    release; the next owner takes the leases and is delivered what was
    never acknowledged; the memory-bus tests are named by their present
    class names (`TestHandledMessagesAreAcknowledgedAtShutdown`,
    `TestTheMessageInTheQueueReachesTheNextMember`,
    `TestHandoverBetweenTwoMembers`); the broker-only facts are the `nak`
    from `close()`, the `<group>-<shard>` durable resuming at its
    acknowledged position under the next member, and `ack_wait`
    redelivery (`REDELIVERED`); the CI pointer is #52 (per `CLAUDE.md`
    "Disabled CI coverage"). The guarantee the scenario proves — the
    `HotIpAdded` is emitted exactly once across the handover — is
    unchanged; no scenario is added or removed.

**Rulings.** Lettered as the top-level session put them.

* **(a) `ack([])` on a consumer whose `close()` ran before it subscribed:
  `ValueError`.** Decision 3's `ack` paragraph gives the checks in order
  — closed, positional, then the iterable — and "closed" is the first;
  the "not-yet-subscribed" clause is the description of the fourth state
  an instance can be in (neither closed nor subscribed), not a second
  rule competing with the first. `close()` is terminal ("After `close()`,
  `ack()` is a `ValueError`" is unconditional) and is explicitly "safe
  before `subscribe()`", so an instance can reach closed without ever
  subscribing, and it is then closed. Both implementations already test
  `_closed` before anything else (`memory.py`, `nats.py`), so no code
  changes. No test pins the case today; one may be added (brief below,
  optional).
* **(b) `bus_endpoints` on an iterable: one result element per input
  element, an empty entry rendering as `nats://`.** Confirmed as the
  intended reading. "Taken entry by entry, each stripped" was written to
  contrast with the `str` form's "empties dropped": the `str` split is
  Hammertime's own (it mirrors `NatsBus.__init__`), so Hammertime decides
  what an entry is; a list is the caller's, and the reduction reports one
  endpoint per entry the caller passed — which is also assumption 44's
  argument for `<unparseable>` ("the entry count is still visible"). An
  empty entry gets the `nats://` prefix like any scheme-less entry and
  `urlsplit("nats://")` yields scheme `nats` and an empty authority, so
  the element is the string `nats://`; nothing of the entry is echoed
  because there is nothing to echo. `test_endpoints.py`'s property test
  (one element for any `[s]`) and `nats.py`'s `[s.strip() for s in
  servers]` are both correct as they stand; no test and no line change.
* **(c) The held-lease set is tracked apart from the windows; a
  `run_maintenance()` after `stop()` re-takes nothing — and, ruled here,
  sweeps nothing.** C2's separation is the intended behaviour and is now
  in decisions 7 and 8: windows survive `stop()` (decision 8 already said
  nothing reads them afterwards except tests), leases do not, and
  `renew_leases()` renews only the set `release()` emptied. The residual
  C2 left is the rest of the sweep: `run_maintenance()` after `stop()`
  would still run `expire_due()`, `finish_warmup_if_due()` and
  `evict_due()` over the surviving windows and could emit a
  `HotIpRemoved` and write `record_transition` for a shard whose lease
  this member no longer holds — after the next owner may have loaded that
  shard's HOT set. Decision 7's rule is that "a member acts on a shard
  only while it holds the lease"; a sweep after release breaks it. Ruled:
  after `stop()`, `run_maintenance()` returns normally without renewing,
  sweeping, emitting or writing. In `run_service` the case is unreachable
  — the service's maintenance loop checks the stop flag before every
  sweep, and a sweep already queued on the worker lock ahead of `stop()`
  runs before the release — so this binds direct callers (tests, the
  integration harness's `advance()`), and the guard lives in the worker
  rather than in an argument about lock order. One code change (brief
  C6 below) and one test (brief T4 below). Not a `CHANGES` entry: nothing
  an operator can observe changes.
* **(d) A `load()` failure inside `on_assigned` needs no rollback.**
  Confirmed, with the mechanism recorded. A refusal is a *decision* —
  another live member holds the shard, and this member must hold nothing
  while it does, so the rollback matters; a `load()` failure is a *fault*
  whose realistic cause (the store unreachable, ADR-0011 assumption 7)
  would make the rollback's `release_lease` calls fail too, so a rollback
  there adds a second failure to report and no guarantee. The leases are
  this member's own; nobody is wronged by their being held for the
  moments until the process exits. What actually clears them: the
  exception propagates out of `subscribe()` and `start()`; `run_service`
  logs `start_failed` and calls `stop()` on the failed start (ADR-0009
  A7's `stop_failed` path), which is `worker.stop()`: `commit_handled()`
  (a flush that is a no-op and an `ack(())` that returns normally on a
  consumer whose `subscribe()` raised — decision 3 leaves it "as if
  `subscribe()` had never been called") and then `release()`, which drops
  every shard in the held set — the failed shard's included, because C2
  adds to the set before it loads. If the store answers, the leases are
  released at once; if it does not, `stop_failed` is logged and they
  lapse after `lease_ttl_s`, exactly as after a crash. A replacement with
  the same `member_id` reacquires either way. No code change; a test to
  pin the mechanism (brief T5 below).
* **C2 judgement 1, confirmed.** `shard_owned_elsewhere` and
  `shard_lease_lost` are logged by `ShardClaims`, where the refusal is
  detected, as decision 7 says ("it logs `ERROR event=...`" under the
  `on_assigned` and `renew_leases` bullets); the service logs
  `start_failed`/`run_exited` from the propagated exception and nothing
  more.

Assumptions made by this amendment (push back individually; numbering
continues the ADR's list):

51. **Closed absorbs not-yet-subscribed rather than the reverse.** The
    alternative — a never-subscribed instance stays "empty" after
    `close()` and accepts `ack([])` — would make `close()` non-terminal
    for one state only, and would need both implementations to keep a
    "was ever subscribed" bit that nothing else reads. The order decision
    3 already gave is the tie-breaker.
52. **An empty iterable entry renders as `nats://` rather than being
    dropped or rendered `<unparseable>`.** It is not unparseable
    (`urlsplit` accepts it), and dropping it would make the iterable form
    second-guess its caller; `nats://` is honest — an entry with no host —
    and echoes nothing. No caller in this repository passes an empty
    entry: the tool's argparse and `NatsBus.__init__` both drop empties
    before any list exists.
53. **`run_maintenance()` after `stop()` is a silent no-op rather than an
    error.** Raising would turn a benign teardown race in a test harness
    into a failure and would give `stop()` a new post-condition; the
    integration harness's `advance()` never runs after teardown, but a
    unit test that calls `run_maintenance()` after `stop()` to prove
    nothing happens must be able to. The guard is the worker's own stop
    flag, checked under the lock, so it costs one branch.
54. **`apply_config()` after `stop()` is not ruled.** The same principle
    would make the re-evaluation pass a no-op after `stop()`. It is
    unreachable through the service (`stop()` stops the poller before the
    worker) and reachable only by a direct `reload_config()` call after
    `stop()`; it is listed as an open question rather than widened into
    this ruling, because a direct caller of `apply_config()` after `stop()`
    in a test may be asserting the pass's arithmetic on the surviving
    windows on purpose. *Closed 2026-09-22 (Amendment 4 ruling R2): it is
    a no-op after `stop()`; no test called it after `stop()`.*
55. **No rollback on a `load()` fault, relying on `run_service`'s
    `stop()`-on-failed-start.** If a future runner stopped calling
    `stop()` after a failed `start()`, the leases would lapse after
    `lease_ttl_s` instead of being released at once — the crash outcome,
    still bounded. The dependency on ADR-0009's `_stop_quietly` is named
    here so that a change to it re-reads this ruling.
56. **`worker.stop()` after a failed `start()` acknowledges nothing and
    cannot raise from the bus.** `commit_handled()` hands `ack()` an empty
    tuple on a consumer that is neither closed nor subscribed, which
    decision 3 (as amended by ruling (a)) says returns normally; the flush
    is a no-op on both producers. If a later bus implementation made
    `ack([])` reach the broker, the failed-start path would need a guard.
57. **The `integration-scenarios.md` §7 rewording keeps the scenario's
    guarantee and changes the list of broker-only facts.** A mechanism
    swap necessarily changes what "only a broker can show"; the three
    facts now listed are the ones decision 5 and decision 8 rest on
    (`nak` on `close()`, durable position by name, `ack_wait`
    redelivery). Whether a #90 overlap refusal against a real store, or
    an `ack_wait` redelivery, deserves a scenario of its own is raised as
    an open question, not decided.
58. **The `#26` -> `#52` pointer in that bullet is a factual update, not a
    model change.** `CLAUDE.md` "Disabled CI coverage" records that #52
    carries the remaining work; the scenario document is not an ADR, so
    the "historical record" reason for leaving `#26` in ADRs does not
    apply to it.

Read on 2026-09-21 for Amendment 3 (repository facts only; no external
source was consulted):

* `packages/hammertime-bus/src/hammertime/bus/memory.py` (`MemoryConsumer.ack`:
  `_closed` then `_positional` then the empty check; `close()` idempotent,
  `_positional` initially `False`; `MemoryProducer.flush` a no-op) and
  `nats.py` (`NatsConsumer.ack`/`close()` in the same order; `bus_endpoints`
  lines 292-323: `[s.strip() for s in servers]` for an iterable, the
  `nats://` prefix for an entry without `://`, `urlsplit` per entry,
  `UNPARSEABLE_ENDPOINT`; `NatsProducer.flush` a no-op).
* `packages/hammertime-bus/src/hammertime/bus/tests/test_endpoints.py`
  (the docstring's stated assumption and
  `test_a_single_list_entry_never_raises_and_yields_one_element`) and
  `test_memory_bus.py` (`TestAckPrecedence`: the five existing cases; none
  closes before subscribing).
* `services/aggregator/src/hammertime/aggregator/sharding/assignment.py`
  (`_leased` kept apart from `_windows` with C2's comment; `on_assigned`
  adds to `_leased` before `load()` and rolls back on a refusal only;
  `release()` empties `_leased`; `renew_leases()` iterates `_leased`;
  the two `ERROR` records logged there), `worker.py` (`start()`,
  `stop()`, `run_maintenance()` — no stop-flag check today —
  `_flush_and_ack()`), `service.py` (`start()`, `stop()` calling
  `worker.stop()` unconditionally, `_maintenance_loop()` checking
  `_stopping` before each sweep, `_close_clients()`), and the aggregator
  tests (`TestHandoverBetweenTwoMembers`, `TestRenewingLeases`,
  `TestHandledMessagesAreAcknowledgedAtShutdown`,
  `TestTheMessageInTheQueueReachesTheNextMember`; no test calls
  `run_maintenance()` after `stop()` or makes `load()` raise).
* `packages/hammertime-core/src/hammertime/core/runtime.py`
  (`run_service`: `_stop_quietly(service)` after a failed `start()`,
  logging `stop_failed` at `WARNING` if `stop()` raises).
* `CLAUDE.md` "Disabled CI coverage" (#52), `docs/spec/README.md` (the
  §47 row already maps `bus_endpoints`; no mapping change needed),
  ADR-0009 A7 and Amendment 4, spec §47.7,
  `docs/spec/integration-scenarios.md` §7.

## Amendment 4 (2026-09-22) — the R1 review's nine findings and the S1 audit's four, ruled

Why: the `reviewer` (report R1, branch `claude/gallant-pasteur-fkehtc` at
091716c) returned three medium and six low findings against the epic
branch, and the `security-auditor` (report S1, at 49cbc5a) one medium and
three low, plus two notes. Every finding is either a defect this ADR's
text already forbids, a gap in the text that let a defect through, or a
residual the text promised more about than the mechanism can deliver.
Each is ruled below — fixed in this epic, recorded as a residual with its
bound, or not a defect with the reason — and every ruling that changes a
decision's text is made in place with a dated italic note. The Amendment 3
briefs C6, T4, T5 and T6 are folded into the two consolidated briefs
(C7 for `coder`, T7 for `test-author`) in the hand-off report, so that
the session dispatches exactly one of each. Nothing here changes a
decision in substance except the two residuals it names (rulings R1 and
S1) and the validator ruling S2 supersedes assumption 15 with.

Every edit outside this section, with the superseded wording quoted or
the insertion point named:

* **Status line.** Gained the "amended a fourth time 2026-09-22" clause.
* **Decision 1, `TopicSpec` sentence.** Appended the italic
  `partition_of(subject)` definition (ruling S3).
* **Decision 2, `ensure_streams` bullet.** Appended the italic "What is
  compared, and that it is unit-tested" text (ruling R3).
* **Decision 2, CLI bullet.** Appended the italic `--servers` validation
  sentence (ruling S2).
* **Decision 3, `close()` paragraph.** Appended the italic "What 'every
  message this instance delivered' reaches" text (ruling R1).
* **Decision 3, `hammertime.bus.nats` block.** Gained `validate_bus_url`.
* **Decision 3, after the `bus_endpoints` paragraph.** Two italic
  paragraphs: "An `@` outside the authority" (rulings R8 and S2) and "The
  same URL reaches a record by a second path" (ruling S2).
* **Decision 5, after the fetching paragraph.** The italic "Malformed at
  the transport" paragraph (rulings S3 and S4).
* **Decision 7, `on_assigned` bullet.** Two italic sentences appended
  after Amendment 3's: the R5 reaffirmation and the R6 skip rule.
* **Decision 7, `release()` bullet.** Appended the italic correction of
  "compose gives a restarted container the same name" (ruling N).
* **Decision 8, `stop()` bullet.** Appended the italic "`stop()`'s order
  is total" text (rulings R2 and R4) and a second italic paragraph on
  `AggregatorService.run()` (ruling R7).
* **Decision 8, `UNCLAIMED` sentence.** Appended the italic post-`stop()`
  clause (ruling R2).
* **Decision 10, `HAMMERTIME_BUS_BROKERS` row.** "not validated by
  `load_settings` (unchanged posture ...)" gained the dated "superseded"
  clause (ruling S2).
* **Decision 11, `nats` bullet.** The port list gained the italic
  loopback ruling (S1); the `provision` bullet the italic `uv` pin (R9);
  the application-services bullet the italic `member_id` sentence (N).
* **Assumption 9.** Appended the italic qualification of "a clean stop
  `nak`s and pays nothing" (ruling R1).
* **Assumption 13.** Appended the italic trust-boundary paragraph (S1).
* **Assumption 15.** Appended the italic "Superseded" note (S2).
* **Assumption 46.** Appended the italic "Closed" note (S2).
* **Assumption 54.** Appended the italic "Closed" note (R2).
* **Consequences, "Not done here, named".** `validation of
  HAMMERTIME_BUS_BROKERS` marked done; three follow-ups added
  (bus/store authentication, a pending-queue drain, a duplicate counter).
* **Outside this file** (same change set): ADR-0012 gains Amendment 4 —
  decision 5 item 2 covers `COPY --from=` image references, the Class 4
  table gains the `ghcr.io/astral-sh/uv:0.12.17` row and its preamble is
  corrected, the Class 2 `uv` row records the current version (ruling
  R9). `docs/spec/README.md` is unchanged: no section-to-file mapping
  moves (the new test file sits under a package the §19 row already
  maps).

**Rulings.** R1-R9 are the reviewer's findings in its order; F is its
flake note; S1-S4 are the auditor's findings in its order; N is the
auditor's `member_id` note.

* **R1 (`close()` nak gap) — path (1) real and fixed here; path (2) a
  residual, recorded with its bound.** Decision 3 promised "every message
  this instance delivered" back to the group "without waiting" and
  decision 8 "at once"; the mechanism reached the retained and the queued
  `Msg`s and dropped the unqueued remainder of a cancelled batch on the
  floor, and cannot reach what the server delivered against a pull that
  the loop never read. The remainder is the loop's own local — a
  `CancelledError` handler that hands it to `close()` closes path (1)
  with no new API. Path (2) has no clean fix: nats-py's per-subscription
  pending queue is private and is drained only by a further `fetch()`
  (Sources), and issuing one at `close()` would pull yet more messages to
  nak. Recorded: at most `FETCH_BATCH` per durable, surfacing after
  `ack_wait`; "without waiting" now means "for what reached the client".
  Measured, if at all, by the integration job (#52).
* **R2 (work after `stop()`) — real, fixed here, and widened to every
  path.** Ruling (c) guarded `run_maintenance()` and left `apply_config()`
  open (assumption 54); the reviewer's scenario (a) — a sweep queued on
  the lock behind `stop()` emitting `HotIpRemoved` for a shard the next
  owner has already loaded — is decision 7's rule broken by lock order,
  and (b) — `run()` handling a message that completed in the same wake-up
  as the stop signal, after the final commit — is decision 8's `stop()`
  order broken by `asyncio.wait` returning two done tasks. One principle
  covers all of it: once the stop flag is set, nothing acts on a shard.
  So `handle()`, `run_maintenance()`, `apply_config()` and
  `_commit_if_due()` each check the flag under the lock and stand down,
  and `run()` does not call `handle()` when the stop task is in `done`.
  `handle()`'s stand-down is `UNCLAIMED` (assumption 61); the periodic
  commit's is a skip (assumption 66); `apply_config()`'s is a total
  no-op (assumption 63). Assumption 54 is closed.
* **R3 (`ensure_streams` untested) — real; tests are due.** The
  exemption in `nats.py`'s docstring ("no unit tests here by design") was
  written for the classes that need a live connection and was read as
  covering the whole module; `ensure_streams` and `stream_config_for`
  are pure over a three-method fake, and `test_cli.py` stubs them out.
  Decision 2 now states what is compared, in what form nats-py presents
  the server's config, and that the tests live in `test_streams.py`; the
  tests are specified in brief T7 from that text.
* **R4 (`stop()` after the bus is closed) — real, fixed here.** The
  docstring said "idempotent"; the second call reached `ack(())` on a
  closed consumer and raised, and the runner makes that call at every
  crash exit. Ruled: `stop()` runs its sequence once and a later call
  returns after taking the lock; and, since the same failure path showed
  that a `commit_handled()` that raises leaves the leases held, the
  release moves into a `finally` (assumption 64). The alternative — an
  `ack` skipped when nothing was handled — was rejected because decision
  8's uniform flush-then-ack trace is pinned by tests and is what makes
  the commit path observable.
* **R5 (rollback on a `load()` fault) — reaffirmed.** The reviewer's
  scenario, a replacement with a different id crash-looping for up to
  `lease_ttl_s`, arises only when the store is unreachable for the
  release too, and a rollback inside `on_assigned` would fail on the same
  store; with R4's `finally` the reachable-store case releases at once
  even when the final commit fails. The consequence for a replacement is
  now stated in decision 7 rather than implied.
* **R6 (`on_assigned` skipping a windowed-but-unleased shard) — real,
  fixed here.** The skip tested the windows; the lease is the claim. The
  held-lease set is the test, and a shard with a surviving window and no
  lease is claimed afresh with a new window (assumption 70). `shards`,
  `window()` and `windows()` keep reporting the windows (assumption 69).
* **R7 (`service.run()` masking the original failure) — real, fixed
  here.** ADR-0009 decision 5 step 7's `run_exited` must name why the
  service stopped; collecting `done`'s exceptions before `stop()` and
  catching `stop()`'s own preserves that. The `stop_failed` record is
  logged by the service at `WARNING` (assumption 71).
* **R8 (`bus_endpoints` with an unencoded `/`, `?` or `#`) — real; the
  definition changes.** The rule "the authority after its last `@`" was
  implemented exactly and still leaked, because `urlsplit` ends the
  authority before the `@`. An `@` left outside the authority is the
  signature of the mistake and renders as `<unparseable>` (assumption
  72). The seven pinned examples stand; five are added. The test file is
  `test_endpoints.py`; the behaviour change is in `bus_endpoints` alone.
* **R9 (`uv:latest` in the provisioner Dockerfile) — this epic's to fix,
  in all five files.** ADR-0012 decision 6 binds every image reference in
  the diff, and the provisioner Dockerfile is in the diff; the four
  service Dockerfiles predate it, but they are one line each, the
  provisioner file was written to match their shape, and leaving four
  unpinned next to one pinned would be a worse state than either. The
  tag is `0.12.17` (assumption 73). ADR-0012's Amendment 1 said every
  `FROM` line was pinned and was right about `FROM`; `COPY --from=` was
  not in its sweep, and ADR-0012 Amendment 4 extends the rule's wording
  so that it is.
* **F (Redis lease timing tests) — widen now.** The `RedisShardStateStore`
  contract sleeps for real against fakeredis with `LEASE_TTL = 1.0`; the
  two "still live" assertions (`0.6 x TTL`, and the renewal test's second
  `0.6 x TTL`) leave a 0.4 s margin, which a loaded CI runner can eat. The
  TTL becomes `2.0` (margins 0.8 s; assumption 75). The fractions are
  unchanged: the renewal test needs the two sleeps to sum past one TTL,
  so `0.6` cannot drop below `0.5`.
* **S1 (unauthenticated bus and store on every host interface) — real,
  fixed here as far as the boundary can be stated; authentication itself
  is a follow-up.** The reference stack published 4222, 8222 and 6379 on
  `0.0.0.0` with no credential and no statement anywhere that the bus
  and the store are inside the trust boundary; the auditor's scenarios
  (a)-(e) all follow. Ruled: the three ports are published on
  `127.0.0.1` only (decision 11); assumption 13 states the boundary and
  the obligation on any non-local deployment; `.env.example`'s event-log
  block carries the same paragraph (brief C7). Not done: NATS
  `authorization` and Valkey `requirepass` plumbed through the services
  (Consequences), `deny_delete`/`deny_purge` on the streams (assumption
  76), and the services' own ports (open question).
* **S2 (`bus_endpoints` residual plus the chained `ValueError` in
  `start_failed`) — real, fixed here on both paths.** The first path is
  R8's. The second — nats-py's `errors.Error` chaining the `ValueError`
  whose text carries the port token — is closed at the source by
  `validate_bus_url` in `load_settings` and the tool (exit 2, no echo)
  and, as a second line, by `_connect` re-raising `from None`
  (assumption 74). Assumption 15 is superseded and assumption 46 closed.
* **S3 (`int(suffix)` on the subject's last token) — real, fixed here.**
  Decision 5's "a poison message cannot loop" assumed the message reached
  the worker; a non-partition token failed the iterator first, and on a
  whole-topic durable or a positional replay the same message came back
  on every restart. Ruled: `TopicSpec.partition_of` is the parse, a
  `None` is malformed at the transport, terminated on a durable and
  skipped on a positional subscription, logged as `malformed_subject`
  (assumptions 77-78). Only the helper is unit-tested.
* **S4 (partition not checked against the key's hash) — real, fixed
  here, in the bus rather than the worker.** The auditor's fix — a fifth
  `_decode` check — would mark every message on `InMemoryBus` malformed,
  because the memory bus has one partition and every message's partition
  is `0` whatever its key (decision 3), so the whole in-process suite and
  the integration harness would fail. The invariant is the transport's:
  `NatsProducer` computes the subject from the key, so `NatsConsumer`
  checks that `partition_for(key, spec.partitions)` equals the subject's
  partition on the way out of the queue, and a mismatch is malformed at
  the transport exactly as in S3. With the worker's existing key-equals-IP
  check that gives subject-equals-IP's-partition end to end, and the
  memory bus is untouched (assumption 79).
* **N (the compose comment "keyed by the container name by default") —
  inaccurate; fixed here.** The default is `socket.gethostname()`, the
  container ID under compose; a recreated container would wait out the
  TTL. Ruled: the compose file sets `HAMMERTIME_AGGREGATOR_MEMBER_ID:
  aggregator-0` (assumption 80) and the comment is corrected;
  `.env.example`'s "which a restarted container ... keeps" is qualified
  the same way. Decision 7's parenthesis is corrected in place.

No `CHANGES` line follows from any of these: the NATS transport, the
provisioner, the lease and the compose stack this branch introduces have
not reached `master`, so none of their behaviour has been observable, and
the epic's own `BREAKING` lines already cover the swap (Amendment 2
ruling 4, ADR-0012 Amendment 2 assumption 5). The four service
Dockerfiles' `uv` pin is a build-input change with no observable effect.

Assumptions made by this amendment (push back individually; numbering
continues the ADR's list):

59. **The batch remainder is kept per consumer instance, not per pump.**
    A list on the instance that every cancelled `_pump_pull` appends to
    is the smallest bookkeeping that lets `close()` nak it after the
    pumps are gathered; the order among the three sets naked (queued,
    remainder, retained) does not matter, since each `Msg` is in exactly
    one of them.
60. **The residual's bound is `FETCH_BATCH` per durable and `ack_wait`.**
    From the pull request's `batch` (`_fetch_n` asks for at most the
    batch, less what it drained) and the server's redelivery rule; a
    request's `expires` (5 s) bounds how long after cancellation the
    server can still deliver against it, and an `UNSUB` on the inbox
    stops delivery sooner, but neither shortens the wait for a message
    already delivered. Not measured; #52's.
61. **`handle()` after `stop()` returns `UNCLAIMED` with the
    `unclaimed_partition` record rather than raising or adding a
    record.** Raising would turn a benign teardown race into a failure
    (assumption 53's argument); a new record would widen ADR-0011
    decision 8's list for a case whose meaning `UNCLAIMED` already has
    ("belongs to whichever member holds the partition, not to this one").
    The `run()` skip keeps the record off the common race; it can still
    fire when `handle()` was queued on the lock before `stop()`.
62. **A message waiting for the lock at `stop()` is not "in hand".** Only
    an executing `_handle` is; the waiting one takes the `UNCLAIMED` path
    and is naked at `close()`, so it is applied once, by the next owner.
    The alternative — letting it through because it was received before
    the flag — would put it after the final commit, which is the defect.
63. **`apply_config()` after `stop()` is a total no-op**, leaving the
    worker's `config` unchanged and logging nothing. Adopting the
    configuration without re-evaluating would leave the windows and the
    worker disagreeing; nothing reads the worker's config after `stop()`
    except tests. A direct `reload_config()` after `stop()` still logs the
    poller's `config_applied`, which is the poller's record and is
    accepted.
64. **`release()` in `stop()`'s `finally`.** When the final acknowledgement
    fails the shard is orphaned until the TTL either way; releasing at
    once lets the next member take it now and re-apply the unacknowledged
    messages after `ack_wait`, which is at-least-once. The exception
    still propagates, so `run_exited` reports it.
65. **A second `stop()` waits on the lock, then returns.** Rather than
    returning immediately: a caller of the second `stop()` may reasonably
    expect the first to have finished when it returns, and the runner's
    `_stop_quietly` is exactly such a caller.
66. **`_commit_if_due()` is skipped once the flag is set**, checked under
    the lock. It costs one branch and makes "the final commit is the last
    acknowledgement" literally true; a periodic commit already holding
    the lock when the flag is set runs to completion before `stop()`'s.
67. **The `ensure_streams` fake is a three-method object** — `stream_info`
    raising `nats.js.errors.NotFoundError()` (constructible with no
    arguments; `APIError.__init__` defaults every field) or returning an
    object whose `.config` is an `api.StreamConfig`, `add_stream`,
    `update_stream` — and the tests exercise the actual side both with
    the server's strings and with the enums, so the comparison does not
    depend on which form the installed nats-py's `from_response` yields
    (read from `main`, assumption 28's caveat).
68. **`stream_config_for(replicas < 1)` is a `ValueError`.** Recorded from
    the code; the CLI already refused a non-positive `--replicas` and
    the helper refuses the same value one layer down.
69. **`shards`, `window()` and `windows()` keep reporting the windows
    after `release()`.** Decision 8 says the windows survive `stop()` for
    tests to read; changing `shards` to the held-lease set would change
    what `shards_claimed` reads after `stop()` (moot: the process exits)
    and what tests read (not moot). R6 changes only which shards
    `on_assigned` skips.
70. **A re-claimed shard gets a new window**, not the surviving one. The
    surviving window's counters are stale by however long the lease was
    gone and its inherited set is not the store's current HOT set; a
    fresh load and warm-up is what any new claim gets.
71. **`stop_failed` is logged by `AggregatorService.run()` at `WARNING`
    with `error=str(exc)`**, the runner's own record name reused for the
    same event one layer down; it is not pinned by a test.
72. **An `@` in the path, query or fragment renders `<unparseable>`**
    rather than, say, the authority up to its first `:`. A rendering
    that guessed at the host would be a redaction of an input it cannot
    parse (assumption 44's argument); and an entry with an `@` inside the
    authority *and* one outside (`nats://user:p@/ss@nats:4222`) is
    `<unparseable>` too, although the last-`@` rule alone would render
    `nats://` for it, because the outside `@` is the signal.
73. **`uv` pinned to `0.12.17`, the release current on 2026-09-22.** The
    version and licence are from PyPI's JSON; the image tag form and the
    "pin to a specific uv version" guidance are from uv's own Docker
    guide, read from the `astral-sh/uv` repository on `main`
    (`docs.astral.sh` is blocked from this environment), whose example
    tag is `0.12.17`; the GitHub releases API answered 403. The architect
    cannot pull the image; if the tag does not resolve at build time the
    coder reports it rather than falling back to `latest`, and
    `major.minor` (`0.12`) is the documented next-narrowest tag.
74. **Both the validator and the `from None` re-raise.** The validator
    alone closes the path for the URLs it recognises; the re-raise
    covers a parse failure it did not anticipate at the cost of hiding a
    chained `ValueError`'s text in `start_failed` — which is the text
    that must not be logged. The validator's three checks are nats-py's
    two parses plus the `@`-outside-authority signature; it adds no host
    or port grammar of Hammertime's own.
75. **`LEASE_TTL = 2.0` for the Redis lease contract.** Roughly six extra
    seconds per suite run (four lapse sleeps of 2.3 s, three "still live"
    sleeps of 1.2 s, against 1.3 s and 0.6 s today) bought against a flaky
    gate; a flake costs a rerun of the whole gate.
76. **`deny_delete`/`deny_purge` stay `false`.** They are not editable
    after creation (Sources, streams page), so flipping them later is a
    stream recreation; inside the trust boundary an operator may need to
    purge, and outside it the flag is no substitute for authentication.
    Named, not changed.
77. **A malformed subject is terminated (`Msg.term()`) on a durable, not
    acknowledged.** `+TERM` is the server's word for "will not be
    processed"; both stop redelivery. On a positional subscription
    (`ack_policy none`) nothing can be sent and the message is skipped.
78. **Only the canonical decimal token is a partition**; `07` and `+7` are
    not, although `int()` accepts them, because `subject(p)` never
    produces them and a non-canonical token is not one of ours. Whether
    the parsed partition is below the topic's count is not checked, as
    decision 3 says `NatsConsumer` accepts any non-negative partition.
79. **The key-hash check lives in `NatsConsumer`, not the worker, and a
    message without the key header is not checked there.** The worker's
    `_decode` already refuses a `None` key (`message.key !=
    ip_text.encode()`), so a keyless message reaches `MALFORMED` by the
    existing path; adding the header check to the memory bus would be
    vacuous (one partition). The `malformed_subject` record carries the
    subject and the stream sequence and not the key, so that a record
    never carries an attacker-chosen value beyond the subject token.
80. **`HAMMERTIME_AGGREGATOR_MEMBER_ID: aggregator-0` in compose rather
    than `hostname: aggregator-0`.** The variable is what decision 7
    defines and what `.env.example` documents; a `hostname:` would work
    through the default and hide the dependency.
81. **The five `Dockerfile`s are this epic's change set**, although four
    predate it. Scope judgement: one line each, no behaviour change, and
    the alternative leaves the tree inconsistent with its own ADR-0012
    rule until a separate issue is filed and landed.
82. **The `run()` skip drops the message received in the same wake-up
    unhandled.** It was yielded, so it is retained and naked at `close()`
    on `NatsConsumer`, and delivered-unacknowledged on `MemoryConsumer`;
    either way the next member applies it once. Handling it after the
    final commit is the defect; handling it before would need `stop()` to
    wait for a message that may never come.

Read on 2026-09-22 for Amendment 4:

* `https://raw.githubusercontent.com/nats-io/nats.py/main/nats/src/nats/js/client.py`
  (summarised by the fetch tool; `main`, not the 2.16.0 tag): `Subscription`
  exposes `pending_msgs` ("Number of delivered messages by the NATS Server
  that are being buffered in the pending queue") and `pending_bytes`, both
  read-only counts; `_fetch_one` "Checks the queue first: `while not
  queue.empty(): ... queue.get_nowait()`" and discards status messages
  "meant for other fetch requests"; `_fetch_n` "Drains existing queue
  messages initially, then sends a no-wait request" ("First request: Use
  no_wait to synchronously get as many available based on the batch
  size"), then "a lingering request with remaining deadline". Taken from
  it: ruling R1's residual (no public drain; only a fetch empties the
  pending queue, and a fetch sends a request).
* `https://raw.githubusercontent.com/nats-io/nats.py/main/nats/src/nats/js/errors.py`:
  `APIError.__init__(self, code=None, description=None, err_code=None,
  stream=None, seq=None)`; `NotFoundError(APIError)` "A 404 error",
  `BadRequestError` "A 400 error", `ServiceUnavailableError` "A 503
  error", each `pass`. Taken from it: assumption 67's constructible fake.
* `https://raw.githubusercontent.com/nats-io/nats.py/main/nats/src/nats/js/api.py`
  (summarised): `StreamConfig` fields (`retention: Optional[RetentionPolicy]
  = None`, `discard ... = DiscardPolicy.OLD`, `max_age: Optional[float]`,
  `storage: Optional[StorageType]`, `duplicate_window: float = 0`, ...);
  `from_response` "performs nanosecond conversions and nested object
  conversions but does not convert enum string values. Fields like
  `retention`, `storage`, and `discard` remain as strings"; `StreamInfo`
  (`config: StreamConfig`, `state: StreamState`, ...). Taken from it:
  ruling R3's statement of what is compared.
* `https://raw.githubusercontent.com/python/cpython/3.12/Lib/urllib/parse.py`:
  `urlsplit` — `if url[:2] == '//': netloc, url = _splitnetloc(url, 2)`,
  then the bracket check raising `ValueError("Invalid IPv6 URL")`, then
  `#` and `?` splits; `_NetlocResultMixinStr._userinfo` uses
  `netloc.rpartition('@')` and `_hostinfo` partitions the host part on
  `[`/`]` and `:` (so `.port` is `int()` of whatever follows the last
  `:`). `_splitnetloc` (not quoted by the tool) is the search for the
  first of `/?#` from position 2, which is the delimiting the ruling
  relies on. Taken from it: rulings R8 and S2.
* `https://raw.githubusercontent.com/astral-sh/uv/main/docs/guides/integration/docker.md`
  (summarised): "Available images" lists `ghcr.io/astral-sh/uv:latest`,
  `ghcr.io/astral-sh/uv:{major}.{minor}.{patch}` "e.g.
  `ghcr.io/astral-sh/uv:0.12.17`" and `ghcr.io/astral-sh/uv:{major}.{minor}`;
  "Installing uv": `COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx
  /bin/` and "it is best practice to pin to a specific uv version, e.g.,
  with: `COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/`";
  pinning a SHA256 digest is also recommended "as tags can be moved
  across different commit SHAs". Taken from it: ruling R9's tag.
* `https://pypi.org/pypi/uv/json`: version `0.12.17`, `license_expression`
  `MIT OR Apache-2.0`, `license_files` `LICENSE-APACHE`, `LICENSE-MIT`,
  author "Astral Software Inc.". Taken from it: ruling R9 and ADR-0012
  Amendment 4's row.
* Blocked or refused: `docs.python.org` and `docs.astral.sh`
  (`EGRESS_BLOCKED`); `api.github.com/repos/astral-sh/uv/releases/latest`
  (HTTP 403). The primary sources above were read in their place.
* Repository facts: `packages/hammertime-bus/src/hammertime/bus/nats.py`
  (`_pump_pull`'s `for msg in batch: await self._queue.put(msg)` with no
  `CancelledError` bookkeeping; `close()` naking `[*queued,
  *retained.values()]`; `ensure_streams`, `_plain`, `_MUTABLE_STREAM_FIELDS`,
  `_IMMUTABLE_STREAM_FIELDS`, `stream_config_for`'s `replicas < 1`;
  `bus_endpoints` lines 292-323; `_to_consumed`'s `int(suffix)`;
  `_connect`); `services/aggregator/src/hammertime/aggregator/worker.py`
  (`run()`'s `asyncio.wait` and `if receive_task not in done`; `stop()`
  with no idempotency guard and no `finally`; `run_maintenance()` and
  `apply_config()` with no stop-flag check; `_commit_if_due`; `_handle`);
  `sharding/assignment.py` (`on_assigned`'s `if shard in self._windows:
  continue`; the refusal-only rollback); `service.py` (`run()` calling
  `stop()` before collecting `done`'s exceptions; `startup_fields`);
  `packages/hammertime-core/src/hammertime/core/runtime.py`
  (`_stop_quietly`, `_supervise`'s crash-exit `stop()`, `ConfigPoller`);
  `deploy/docker-compose.yml` (ports `4222:4222`, `8222:8222`,
  `6379:6379`; the aggregator comment); `.env.example` lines 16-24 and
  71-73; the five `Dockerfile`s (`COPY --from=ghcr.io/astral-sh/uv:latest`);
  `packages/hammertime-store/src/hammertime/store/tests/test_shard_state.py`
  (`ShardLeaseContract`, `LEASE_TTL = 1.0`, the `0.6`/`+ 0.3` sleeps);
  `packages/hammertime-bus/src/hammertime/bus/tests/test_endpoints.py`
  (the seven pinned examples); `tools/provision/.../tests/test_cli.py`
  (`_FakeEnsureStreams`); the aggregator tests' harness classes
  (`_TappedBus`, `_Feed`, `_Members`, `_PrefetchBus`); ADR-0012 decisions
  5 and 6 and its Class 2/4 tables; the R1 and S1 reports as relayed by
  the top-level session.
