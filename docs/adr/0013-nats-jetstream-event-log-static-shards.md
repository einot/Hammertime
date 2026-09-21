# ADR 0013 — NATS JetStream as the durable event log, with static shard assignment only

Status: accepted 2026-09-21, **pending owner confirmation**. Epic #95's first
reason for the swap — that Kafka's cold start threatens ADR-0009's 60 s
startup deadline — was measured on 2026-09-21 and does not hold (see
Context, prerequisite 5); the epic's own text says the owner "may wish to
revisit the decision" in that case, and the top-level session is reporting
it. This ADR is therefore written so that the owner can reject it cheaply:
nothing outside `docs/` has been changed on its strength, and every
implementation brief it produces is blocked on that confirmation. Amends
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
`subject_filter` (property, `f"{name}.*"`). ADR-0011 assumption 1 rejected
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
  raises `StreamConfigConflictError` and changes nothing.
* A new CLI, `hammertime-provision` (package `tools/provision`, module
  `hammertime.tools.provision`, docstring `Spec: section 19, section 32,
  section 33`), runs `ensure_streams` over `all_topics()`: `hammertime-provision
  --servers <urls> [--replicas N] [--timeout S]`; exit 0 when every stream
  exists with the declared configuration, 1 when the servers are
  unreachable within `--timeout` (default 60 s, retried with ADR-0009 A1's
  schedule) or a stream conflicts, 2 on invalid arguments. It logs one
  `INFO event=stream_provisioned stream=<name> action=<created|updated|unchanged>`
  per stream through `configure_logging("provision", level)`.
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

def static_partitions(topic: str, partitions: Iterable[int]) -> frozenset[int]: ...   # unchanged (ADR-0011 A3)
```

Removed: `Consumer.seek`, `Consumer.commit`, `AssignmentListener.on_revoked`.
Added: `message_id` on `publish`, `start_offset` on `subscribe`,
`Consumer.ack`, `Consumer.close`, `ConsumedMessage.delivery_count`,
`MessageBus`, `partition_for` (decision 1).

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
  `partitions` and a `listener`.

**`Consumer.ack(messages)`** acknowledges exactly the given messages, each
of which MUST have been delivered by this consumer instance under its
durable subscription and not yet acknowledged; anything else — a message
of another topic or partition, one this instance never delivered, one it
already acknowledged, or any message on a positional subscription — is a
`ValueError` from both implementations, raised before anything is
acknowledged (the whole iterable is checked first, so a rejected call
acknowledges nothing). An empty iterable returns normally without touching
the broker. Acknowledging is what advances the group's position: it is the
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
The connection is opened with `retry_on_failed_connect=False` (so
`connect()` fails fast and `connect_with_retry` owns the startup schedule),
`allow_reconnect=True` and `max_reconnect_attempts=-1` (a mid-run outage is
reconnected indefinitely; an in-flight `publish` or `fetch` during it
raises `nats.errors.TimeoutError`, which the calling service treats exactly
as it treated an aiokafka failure today — ingest answers 503, the
aggregator's `run()` fails and the process exits 1; assumption 14).

**`hammertime.bus.memory`** implements the same contract with one
partition per topic: `MemoryProducer` appends to partition 0 and
deduplicates by `message_id` per topic for the lifetime of the
`InMemoryBus` (an unbounded duplicate window — a test never needs the
window to lapse); `MemoryConsumer` keeps, per `(topic, group)`, the set of
acknowledged offsets on the bus, and its own delivered-but-unacknowledged
set on the instance. `InMemoryBus` never redelivers a message to a live
consumer (no `ack_wait`); `delivery_count` is always 1. It keeps ADR-0011
assumption 22's limits (single partition; at most one live member per
group per topic).

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
feeding one in-process queue; the iterator yields from the queue. A
message is *delivered* when the iterator yields it. The consumer retains
the nats.py `Msg` for every delivered, unacknowledged message, keyed by
`(topic, partition, offset)`.

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
  rather than silently claiming nothing.
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
expires_at)}`; an expired entry counts as absent. `load` and
`record_transition` are unchanged, and the lease key is separate from the
HOT set so ADR-0011 A2's atomicity of `record_transition` is untouched.

**Where, by whom.**

* `ShardClaims` is constructed with two new keyword arguments, `member_id:
  str` and `lease_ttl_s: float`. In `on_assigned(partitions)`, for each
  shard in sorted order and **before** `state_store.load(p)`, it calls
  `acquire_lease(p, member_id, lease_ttl_s)`. A non-`None` result is a
  refusal: it logs `ERROR event=shard_owned_elsewhere shard=<p>
  owner=<other> member=<self>`, releases every lease this call acquired
  so far, and raises `hammertime.aggregator.sharding.assignment.ShardOwnedElsewhereError(shard, owner)`.
  The exception propagates out of `subscribe()`, out of `worker.start()`
  and out of `service.start()`; it is not in any transient tuple, so
  `run_service` logs `start_failed` and the process exits **1** (ADR-0009
  decision 8: a runtime failure after connections were made, not a
  configuration error the settings could have caught).
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
  the lease lapsed is not fenced (assumption 18).
* `ShardClaims.release()` calls `release_lease` for every held shard;
  `AggregatorWorker.stop()` calls it after the final flush-and-ack
  (decision 8), so a clean stop hands the shards over immediately. A
  crashed member's leases expire after `lease_ttl_s`; a replacement with
  the same `member_id` (compose gives a restarted container the same
  name; a Kubernetes StatefulSet gives a pod the same ordinal name)
  reacquires at once, and one with a different id waits at most
  `lease_ttl_s`, crash-looping with `shard_owned_elsewhere` until then.

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
    def handled_position(self, shard: int) -> int | None: ...     # one past the last handled offset; None if none
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
  rule reads "flush before ack" and is unchanged in substance.
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
  no-counter rule unchanged).
* **`on_revoked` is gone** from `AssignmentListener`, from `ShardClaims`
  and from `AggregatorWorker`. Static assignment has no revocation; the
  way a shard changes hands is a `stop()` on one member and a `start()` on
  another. `AggregatorWorker.stop()` is: stop fetching, finish the message
  in hand, `commit_handled()`, `release()`; windows are kept in memory
  (nothing reads them after `stop()` except tests), and the service closes
  the bus (`NatsBus.close()`, which `nak`s and unsubscribes) and the store
  client afterwards, as today.
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
  `start_offset=1` with no snapshot), applies messages in the order the
  iterator yields them, and never acknowledges. There is no
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
  ADR-0009 decision 4): the log end is the stream's last sequence, read
  from `stream_info` at `start()` — the bus exposes it as
  `NatsBus.last_offset(topic) -> int` / `InMemoryBus.last_offset(topic)`
  (assumption 20).
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
| `HAMMERTIME_BUS_BROKERS` | Kafka bootstrap list, default `localhost:19092` | comma-separated NATS server URLs (`nats://host:4222`, also `tls://`, `ws://`, `wss://`), default `nats://localhost:4222`; not validated by `load_settings` (unchanged posture; ADR-0009 A12's last assumption; assumption 15) |
| `HAMMERTIME_SHARD_IDS` | `auto` (default) or a set | **required**; `all` or a set within `0..127`; `auto` rejected (decision 6) |
| `HAMMERTIME_AGGREGATOR_MEMBER_ID` | — | new; default `socket.gethostname()` (decision 7) |
| `HAMMERTIME_AGGREGATOR_LEASE_TTL_S` | — | new; default 30 (decision 7) |
| `HAMMERTIME_AGGREGATOR_COMMIT_INTERVAL_S` | offset-commit cadence | acknowledgement cadence; same default 1.0 |

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
  `8222:8222` (monitoring), and `healthcheck: wget -qO-
  http://127.0.0.1:8222/healthz?js-enabled-only=true` (the monitoring
  page documents `js-enabled-only` as "Returns an error if JetStream is
  disabled" and 200/400 as the response codes; Sources), interval 2 s,
  retries 15. The `-alpine` variant is chosen over the 7.3 MB scratch-based
  `nats:2.15.0` because a compose `healthcheck` runs inside the container
  and needs a shell and an HTTP client, which a scratch image does not
  carry (assumption 11).
* `provision`: builds `tools/provision/Dockerfile` (same shape as the
  service Dockerfiles), runs `hammertime-provision --servers nats://nats:4222`,
  `restart: "no"`, `depends_on: nats: condition: service_healthy`.
* `ingest`, `aggregator`, `trie`, `detector`: `HAMMERTIME_BUS_BROKERS:
  nats://nats:4222`; `depends_on: provision: condition:
  service_completed_successfully` and `valkey: condition: service_healthy`
  where applicable; `valkey` gains `healthcheck: valkey-cli ping`. The
  application services keep the `/readyz` healthchecks ADR-0009 decision
  10 specified (added if not yet present).
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
   Consequences, and is marked pending owner confirmation so that the
   owner can reject it before any code changes.
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
   its position.
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
    credential rule of ADR-0009 A1 binds whoever adds them.
14. **Connection options: fail-fast connect, unbounded reconnect.** So
    that `connect_with_retry` owns the startup schedule and a mid-run
    outage is survived by the client; the observable behaviour during an
    outage (a timed-out publish or fetch fails the caller) is unchanged
    from today.
15. **`HAMMERTIME_BUS_BROKERS` is not validated by `load_settings`.**
    ADR-0009 A12 left the parallel question open for Kafka; nats.py parses
    URLs at `connect()`, so a malformed one surfaces as `start_failed` and
    exit 1 rather than exit 2. Adding a grammar of Hammertime's own is the
    second-definition-of-validity A12 argued against; a driver-parser
    helper like `validate_redis_url` is a candidate follow-up, not done
    here.
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
20. **`last_offset(topic)` on the bus objects for readiness.** ADR-0009
    decision 4 needs "the log end as it stood when `start()` began" for
    the trie and the detector; the bus is the only party that can read it.
    Named here so the trie epic does not invent it; `InMemoryBus`
    implements it as the log length.
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
  (dedup by id, acknowledged sets, positional subscriptions, `close`);
  `topics.py` gains `partition_for`, `stream_name`, `subject`,
  `subject_filter`; `nats.py` new; `kafka.py` deleted; `__init__.py`
  re-exports `NatsBus`, `NatsProducer`, `NatsConsumer`, `TRANSIENT_ERRORS`,
  `MessageBus`, `partition_for` and drops the Kafka names;
  `pyproject.toml` swaps `aiokafka` for `nats-py>=2.16`; the root
  `pyproject.toml`'s `aiokafka.*` mypy override goes (nats-py ships type
  hints; if a `py.typed` marker turns out to be missing at 2.16.0 the
  override is re-pointed rather than dropped).
* **Store package**: `ShardStateStore.acquire_lease`/`release_lease`
  (decision 7) on both implementations; `MemoryShardStateStore(clock=...)`.
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
  validation of `HAMMERTIME_BUS_BROKERS`; consumer-config reconciliation
  in the provisioner; a byte cap on streams; a counter for redeliveries;
  unifying `replay_position` and `event_sequence`; the `integration` job's
  re-enable (#52).

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
