# ADR 0009 — One service process lifecycle, one shared runner, four `main()`s

Status: accepted; amended 2026-09-17 (see "Amendment 1" at the end — the
decisions above it are unchanged in substance, the amendment pins the
*surface* through which each one is observed and tested); amended
2026-09-18 (see "Amendment 2" — decision 7's drain order is corrected in
place: a service flushes its producer *before* it commits its consumer
position, and that order binds every commit point of every service);
amended 2026-09-18 (see "Amendment 3" — decision 2's "before any bus,
store or socket is opened" guarantee is corrected in place to cover a
connection URL the client library would refuse at construction:
`HAMMERTIME_REDIS_URL` is validated in `load_settings`, through
`hammertime.store.validate_redis_url`, whenever the store kind is `redis`);
amended 2026-09-21 (see "Amendment 4" — under ADR-0013 the event log is
NATS JetStream: decision 9's group names become durable-consumer names and
members are told apart by their static shard sets, decision 10's broker
healthcheck is the NATS `/healthz` endpoint, the 60 s startup deadline is
kept on a measured rather than assumed basis, and A1's bus transient tuple
is `hammertime.bus.nats.TRANSIENT_ERRORS`; each is noted in place);
amended 2026-09-21 (see "Amendment 5" — A7's `starting` record carries
`bus_endpoints`, the reduced form of `HAMMERTIME_BUS_BROKERS`, in place of
`bus_brokers`, and A7's no-credential rule covers the userinfo of a bus URL;
both are pointer edits recording ADR-0013 Amendment 2, noted in place)

Scope note: this ADR defines what a Hammertime service *process* is — how it
starts, becomes ready, is observed, stops, and what it exits with — and the
seams that let the same composition be run in-process by the cross-service
tests under `tests/integration` and `tests/e2e` (issue #26). It does not
define any service's domain behaviour (sliding window, trie, classification);
those remain with §5-§34 and their own epics. Normative summary: spec §47.

## Context

`deploy/docker-compose.yml` starts four application containers whose
`Dockerfile`s all end in `ENTRYPOINT ["uv", "run", "hammertime-<service>"]`,
and each service's `pyproject.toml` maps that script to
`hammertime.<service>.__main__:main`. Only ingest defines `main()`; the
aggregator, trie and detector `__main__.py` files are stubs, so three of the
four containers crash-loop with `ImportError: cannot import name 'main'`. That
is failure 2 of #26.

Nothing in the spec or an earlier ADR says what `main()` is responsible for.
Ingest's existing `main()` is one reasonable answer (`load_settings()` from the
environment, `uvicorn.run(create_app(settings))`), but it was written for a
service that is *only* an HTTP server. The other three are primarily bus
consumers, one of them (trie) has a recovery phase that must complete before it
may serve reads (§33), and two of them have no HTTP server at all in the
current deploy files — yet §37 expects metrics from every stage and the compose
file has no way to tell a crash-looping consumer from a healthy one.

Two further facts shape the decision:

* `pyproject.toml`'s `[tool.pytest.ini_options] testpaths` includes `tests/`,
  and the CI `check` job runs a bare `uv run pytest -q`. Whatever lands in
  `tests/integration` and `tests/e2e` is therefore collected in a job that has
  no Docker, no broker and no Redis. Those tests cannot depend on the compose
  stack, and skipping them is not an option (#26's constraints). They have to
  run the real services *in-process*, which means every service needs a
  composition root that accepts an injected transport, store and clock —
  exactly the seam `services/ingest/src/hammertime/ingest/app.py::create_app`
  already exposes for ingest.
* The runtime pieces every service needs (signal handling, startup deadline,
  readiness, drain on shutdown, exit codes, the startup log line) are
  service-agnostic. Writing them four times invites four slightly different
  answers to "what does SIGTERM do".

## Decision

### 1. `main()` is a thin adapter over one shared runner in `hammertime-core`

Every service's `hammertime.<service>.__main__` module defines:

```python
def main() -> None:
    raise SystemExit(run_service(SERVICE_NAME, build_from_env))

if __name__ == "__main__":
    main()
```

`run_service` lives in a new module `hammertime.core.runtime`
(`packages/hammertime-core/src/hammertime/core/runtime.py`, docstring
`Spec: section 47`). `SERVICE_NAME` is the fixed identifier `ingest`,
`aggregator`, `trie` or `detector`. `build_from_env` is the service's own
composition root (decision 3) partially applied to `os.environ`.

`main()` takes no arguments and reads no `argv`. There is deliberately no CLI:
every knob is an environment variable (decision 2), which is what the compose
file, the Kubernetes notes in `deploy/k8s/README.md` and `.env.example` already
assume.

### 2. Configuration is environment-only, validated before any connection

Each service has `hammertime.<service>.config.load_settings(env: Mapping[str,
str] | None = None) -> <Service>Settings` — a frozen dataclass built from the
`.env.example` keys, following the pattern `services/ingest/.../config.py`
established: `env=None` means `os.environ`; a malformed or out-of-range value
raises `ValueError` naming the variable; unknown variables are ignored.

Every service also loads the detection configuration document at
`HAMMERTIME_CONFIG_PATH` via `hammertime.core.config.loader.load` at startup,
and re-polls it thereafter (decision 6). All four need it: ingest for
`bucket_seconds` and `allowed_lateness_seconds`, the aggregator for thresholds,
the trie and detector for the prefix predicate (ADR-0010) and for the
`config_version` recorded in snapshots (§33) and read responses (§22).

Secrets are never fields of a settings dataclass, because settings are logged
at startup (decision 5). `HAMMERTIME_INGEST_AGENT_TOKEN_KEY` stays read
directly by the registry loader (ADR-0006) and that rule now applies to any
future secret.

A `ValueError`/`ConfigurationError` from `load_settings`, from the detection
config loader, or from the registry loader terminates the process with exit
code 2 (decision 8) *before* any bus, store or socket is opened. A service must
never half-start on a bad configuration.

That guarantee covers the *construction* of a client, not only the
connection it later makes. A value that a client library would refuse when
the client is built from it is invalid configuration, and `load_settings`
MUST refuse it first — otherwise the refusal happens inside `start()`, after
`starting` has been logged, and is reported as `start_failed` and exit 1,
which tells an orchestrator to keep retrying something that cannot come
right. Concretely, `HAMMERTIME_REDIS_URL` is meaningful only when
`store_kind == "redis"`, and in that case `load_settings` MUST validate it by
calling `hammertime.store.validate_redis_url(value)` — a helper in the
`hammertime-store` package that accepts exactly what
`redis.asyncio.Redis.from_url` accepts and raises `ValueError` naming the
variable without repeating any part of the value — before it returns. When
`store_kind == "memory"` the value is stored untouched and never parsed, so a
set-but-malformed URL is ignored, not rejected. The settings dataclass itself
does not validate: `load_settings` is the boundary, and an object built
directly (as the tests do) is a plain carrier. See A12.

> Amended 2026-09-18: the paragraph above was added by Amendment 3, which
> corrects the guarantee the previous paragraph states — it was violated by
> a malformed `HAMMERTIME_REDIS_URL` in both services that had one — and
> settles where and how the URL is validated (A12).

### 3. Every service exposes a composition root that accepts injected transports

```python
# hammertime.<service>.service  (new module in each service; Spec: section 47)
def build_service(
    settings: <Service>Settings,
    *,
    bus: InMemoryBus | None = None,
    clock: Clock | None = None,
    # service-specific extras, all keyword-only, all optional:
    #   ingest:  dedup_store, agent_registry (mirroring create_app today)
) -> <Service>Service
```

When `bus` is given, the service takes `bus.producer()` / `bus.consumer(group)`
from it regardless of `HAMMERTIME_BUS_KIND`; when it is `None`, the transport is
chosen by `settings.bus_kind` (`kafka` -> `hammertime.bus.kafka`, `memory` -> a
private `InMemoryBus`). Same rule for `clock` (`None` -> `SystemClock`) and for
ingest's store/registry overrides. This is precisely the seam
`create_app(bus=..., dedup_store=..., agent_registry=...)` already provides for
ingest; `build_service` wraps it rather than replacing it.

The returned object implements the `Service` protocol from
`hammertime.core.runtime`:

```python
class Service(Protocol):
    name: str
    async def start(self) -> None: ...   # connect, recover, become ready; returns when ready
    async def run(self) -> None: ...     # serve until stop() is called; returns after drain
    async def stop(self) -> None: ...    # request shutdown; idempotent; safe before start()
    @property
    def ready(self) -> bool: ...         # decision 4
```

Per-service additions the cross-service tests rely on
(`docs/spec/integration-scenarios.md`), all `async` unless noted:

| Service | Member | Meaning |
| --- | --- | --- |
| all | `reload_config() -> DetectionConfig` | Force one poll of `HAMMERTIME_CONFIG_PATH` now and apply it (decision 6); returns the config in force afterwards |
| ingest, trie, detector | `app: FastAPI` (attribute) | The ASGI application, usable through `httpx.ASGITransport` without a socket. Its lifespan has run once `start()` returns |
| aggregator | `run_maintenance() -> None` | One expiry sweep (§5, §26) plus re-evaluation of every IP whose window total changed, emitting transitions (§30) |
| trie | `snapshot_now() -> Path` | Write a snapshot immediately (§33) and return its path; the periodic writer is unaffected |

`run_maintenance` and `snapshot_now` are the same coroutines the service's own
periodic loops call, not test-only code paths — the periodic loop is "sleep
interval, call the coroutine". That is what makes an in-process test of them
a test of production behaviour.

> Amended 2026-09-17: `Service` is `@runtime_checkable` (A3); ingest's
> `start()`/`stop()` are complete without `run()`, and `run()` is what binds
> the socket (A8). See Amendment 1.

### 4. Readiness has one meaning per service, and it gates the read APIs

`ready` becomes `True` at the end of `start()` and `False` once `stop()` is
called. What `start()` waits for:

| Service | `start()` completes when |
| --- | --- |
| ingest | detection config and agent registry loaded, dedup store reachable, producer connected (the existing lifespan) |
| aggregator | detection config loaded, consumer subscribed to `hammertime.observations.v1` and shard claims (§20) established |
| trie | newest snapshot loaded, `hammertime.hot-ip.v1` replayed from the snapshot's recorded position up to the log end as it stood when `start()` began (§33), query app lifespan run |
| detector | detection config loaded, `hammertime.prefix-stats.v1` consumed up to the log end as it stood when `start()` began, so the current-detections view is complete before it is served |

Every service answers three HTTP endpoints on its bind address (§37, §47):

```text
GET /healthz   200 {"status":"ok"} as soon as the socket is open (liveness)
GET /readyz    200 {"status":"ready"} iff service.ready, else 503 {"status":"starting"|"stopping"}
GET /metrics   200 text/plain; version=0.0.4  (Prometheus exposition; content is §37's, owned by the telemetry epic — may be empty until then)
```

Domain read endpoints (`GET /prefix/...`, `GET /ip/...`, `GET /detections`,
ADR-0010) answer 503 with the same body as `/readyz` while not ready. Ingest's
`POST /v1/observations` likewise answers 503 (the protocol already reserves 503
for "retry with the same sequence").

The aggregator currently has no HTTP server and no port in
`deploy/docker-compose.yml` or `deploy/prometheus.yml`; §37's sliding-window
metrics have nowhere to be scraped from. It gets the same three endpoints on
`HAMMERTIME_AGGREGATOR_BIND` (default `0.0.0.0:8083`), served by a minimal
pure-ASGI app provided by `hammertime.core.runtime` (no FastAPI dependency in
core; `uvicorn` is added to the aggregator's dependencies). The FastAPI
services implement the same three routes through the same core helpers so the
bodies and status codes cannot drift.

> Amended 2026-09-17: the object the admin app and the helpers take is a
> three-state `Readiness`, of which `service.ready` is the two-valued view
> (A4); the helpers return an `AdminResponse` whose fields are pinned (A6).
> See Amendment 1.

### 5. Startup order, retries, and the startup log line

`run_service(name, factory)`:

1. `hammertime.core.telemetry.logging.configure_logging(name, level)` with
   `level` from `HAMMERTIME_LOG_LEVEL` (default `info`). Structured, one event
   per line, every record carrying `service=<name>`.
2. `service = factory()` — settings, detection config, registry. Any
   `ValueError`/`ConfigurationError` -> one `ERROR` record `event=config_invalid`
   with the message -> exit 2.
3. Log `INFO event=starting` with: `service`, the package version, `bus_kind`,
   `bus_brokers` (broker addresses are not secrets), `store_kind` where
   applicable, `config_path`, `config_version`, `bind`, and the service's
   consumer group where applicable. Never a token, key or URL with credentials
   (`HAMMERTIME_REDIS_URL` may embed a password: log its host and db only).
4. Install `SIGTERM` and `SIGINT` handlers that set a stop event.
5. `await service.start()` under a deadline of `HAMMERTIME_STARTUP_TIMEOUT_S`
   (default 60). `start()` itself retries *transient* connection failures to
   the bus and the store with exponential backoff (0.5 s doubling, capped at
   5 s), logging `WARNING event=dependency_unavailable dependency=bus|store
   attempt=N` each time. Compose's `depends_on` orders container start but not
   broker readiness, so a service that fails fast on the first refused
   connection would crash-loop for the first seconds of every deploy. Deadline
   exceeded, or a non-transient error -> `ERROR event=start_failed` -> exit 1.
6. Log `INFO event=ready` with the time spent in `start()`; the trie
   additionally reports it as the `trie_recovery_seconds` metric named in
   `docs/runbook.md`.
7. `await service.run()` concurrently with the stop event. `run()` returning
   on its own is a failure (a service that has nothing left to do has crashed):
   `ERROR event=run_exited` with the exception if any -> exit 1.

> Amended 2026-09-17: step 5's "transient" is defined, and the retry
> schedule's clock and sleep are injectable (A1, A2); the carrier of every
> log record named in steps 1-7 is pinned (A7). See Amendment 1.

### 6. Configuration changes are polled, versioned, and applied by re-evaluation

Each service polls `HAMMERTIME_CONFIG_PATH` every
`HAMMERTIME_CONFIG_POLL_INTERVAL_S` seconds (default 1.0) using
`hammertime.core.config.loader.load`, applying a document only if its
`config_version` is *greater* than the one in force. A document that fails to
load or validate is logged at `ERROR event=config_rejected` and ignored — the
previous version stays in force (§34: "a configuration change MUST define its
effect on existing state"; a broken file has none). A document whose version is
equal or lower is ignored silently; an operator rolling back republishes the
old thresholds under a *new* version, as `docs/runbook.md` already says.

What "apply" means per service is the domain epics' business (aggregator:
`reevaluate.py`, §34; trie/detector: re-run the prefix predicate, ADR-0010),
but the lifecycle rule is uniform: the version becomes visible in emitted
events and read responses only after the re-evaluation it triggered has been
applied to in-memory state. `reload_config()` (decision 3) performs one poll
synchronously and returns.

> Amended 2026-09-17: the poller is a named core type, `ConfigPoller`, with
> a pinned constructor and semantics — including that a version is always in
> force (A5). See Amendment 1.

### 7. Shutdown: drain, then exit 0

On the first `SIGTERM`/`SIGINT`, `run_service` logs `INFO event=stopping
signal=<name>`, calls `service.stop()` and waits for `run()` to return, under a
deadline of `HAMMERTIME_SHUTDOWN_TIMEOUT_S` (default 8). During the drain a
service:

* stops accepting new work (HTTP servers stop accepting connections; consumers
  stop fetching);
* finishes the message it is currently applying — never stops mid-message,
  so at-least-once redelivery after a crash re-applies at most one message
  per partition (ADR-0003);
* flushes its producer;
* then commits its consumer position (`Consumer.commit`) — after the flush,
  never before it (the rule below);
* trie: writes a final snapshot (§33) *after* the commit above, so the
  snapshot's recorded position is never ahead of the committed one;
* closes bus, store and socket resources.

**Flush before commit — one rule, every service, every commit point.** A
service that consumes one topic and produces to another MUST have
`Producer.flush()` return before it calls `Consumer.commit` for a position
that covers the messages whose handling produced those events — at the
drain above, at every periodic commit, and at every rebalance revocation
(ADR-0011 decision 6 for the aggregator; ADR-0010 decision 3 for the
trie). The two orders fail differently, and only one failure is
recoverable. *Commit, then flush:* a process that dies between the two has
committed past messages whose emitted events never reached the event log;
the consumer group will not re-read them, and nothing downstream can
re-derive the transitions — the aggregator's window state is
process-local, and §32 names it the authoritative information the trie is
reconstructed from. *Flush, then commit:* a death between the two
re-delivers messages that were already applied, which is exactly the
at-least-once redelivery ADR-0003 commits every consumer to absorbing
(process-local counters and an idempotent durable HOT set, ADR-0011
decision 3; replace-on-add and `hot_count >= 0` in the trie, §46.5 and
§11). The trie's final snapshot keeps its place in the list — after the
commit, so its recorded position is never ahead of the committed one — and
is therefore also after the flush, so no event the snapshot covers has
stats that never reached the log.

Deadline met -> exit 0. Deadline exceeded -> `WARNING event=shutdown_timeout`
-> exit 1. A second signal during the drain aborts it immediately -> exit 1.

The 8 s default is chosen because Docker Compose's default `stop_grace_period`
is 10 s (Kubernetes' default `terminationGracePeriodSeconds` is 30 s, so the
tighter of the two governs); the drain must finish inside that or the
container is SIGKILLed mid-snapshot, which is exactly the outcome the trie's
final snapshot exists to prevent.

> Amended 2026-09-18: the drain flushes the producer *before* committing
> the consumer position, and that order binds every commit point of every
> service; the first version of the bullet list had commit before flush
> (A11). See Amendment 2.

### 8. Exit codes

```text
0   clean shutdown after a signal, drain completed
1   runtime failure: start() deadline/non-transient error, run() exited, drain timed out or was aborted
2   configuration invalid: settings, detection config or registry rejected before any connection was made
```

Nothing else. Uncaught exceptions inside `run_service` map to 1 with the
traceback logged; `main()` never lets a traceback be the only diagnostic.

### 9. Consumer groups are fixed names

```text
aggregator   hammertime-aggregator     hammertime.observations.v1
trie         hammertime-trie           hammertime.hot-ip.v1
detector     hammertime-detector       hammertime.prefix-stats.v1
```

Committed offsets are keyed by group name on the broker (and in `InMemoryBus`),
so renaming a group orphans a deployment's read position. There is no
environment override in v1; changing a name is a `BREAKING` `CHANGES` entry.
Sharded aggregators (§20) share the one group; members are distinguished by
the broker's group membership, not by `HAMMERTIME_SHARD_IDS` (which cannot
differ per member under the HPA-scaled Deployment in `deploy/k8s/README.md`,
where every replica gets the same environment), and how shards map onto
partitions and members is the aggregator epic's design (A10).

> Amended 2026-09-21 (ADR-0013; Amendment 4): the three names stand, as
> the names of JetStream durable consumers — the aggregator's per-shard
> durables are `hammertime-aggregator-<partition>`, the detector's is
> `hammertime-detector`, and the trie holds no durable at all (it replays
> positionally from its snapshot, ADR-0013 decision 9). Acknowledged
> positions live on those durables, so renaming still orphans a
> deployment's position and is still `BREAKING`. The paragraph's second
> half is superseded: there is no group membership, and members of a
> deployment ARE distinguished by `HAMMERTIME_SHARD_IDS`, which is now
> required and MUST be disjoint per member (ADR-0013 decisions 6 and 7);
> the HPA-scaled Deployment of `deploy/k8s/README.md` is gone with `auto`
> mode. A10's correction is therefore reversed (see Amendment 4).

### 10. The compose stack must be able to tell healthy from crash-looping

`deploy/docker-compose.yml` gains a `healthcheck` per application service that
probes `GET /readyz` (via `python -c` with `urllib`, since the images have no
`curl`), `depends_on` conditions of `service_healthy` on `redpanda` (`rpk
cluster health`) and `redis` (`redis-cli ping`), the aggregator's port, and
the new environment keys with their defaults. `deploy/prometheus.yml` gains
`aggregator:8083`. With those in place `docker compose up -d --build --wait`
fails when a service never becomes ready instead of returning 0 and leaving a
crash loop behind — but adding `--wait` is a `.github/workflows/ci.yml` change
and is not made by this ADR.

> Amended 2026-09-21 (ADR-0013 decision 11; Amendment 4): the broker
> service is `nats` and its healthcheck is `wget -qO-
> http://127.0.0.1:8222/healthz?js-enabled-only=true` (the `-alpine`
> image carries the shell and client that needs); the store service is
> `valkey` with `valkey-cli ping` (ADR-0012 decision 10 already renamed it);
> a one-shot `provision` service creates the streams and every application
> service depends on it completing successfully as well as on the two
> `service_healthy` conditions. `make up` gains `--wait`; the CI job's
> `--wait` is still #52's to add when it re-enables the job.

## Assumptions

Each of these is a judgment call not dictated by #26, the spec, or a prior ADR.
Push back on them individually.

* **One shared runner rather than per-service copies.** The spec says nothing
  about process structure; ingest's existing `main()` was the only precedent.
  Chosen so that signal handling, exit codes and readiness cannot drift
  between four services. Cost: ingest must migrate off `uvicorn.run` to a
  `uvicorn.Server` task inside the runner (with uvicorn's own signal handlers
  disabled) — a small, non-urgent conformance task, since ingest already
  starts, serves and stops cleanly today. Its only observable non-conformances
  are the missing `starting`/`ready` log records, no `/readyz`, and exit 1
  (a `ValueError` traceback) instead of 2 on a bad setting.
* **Environment-only configuration, no CLI.** Follows `.env.example`,
  compose and the k8s notes; `tools/` CLIs are unaffected.
* **Aggregator gets an HTTP admin port, default 8083.** Nothing in the spec
  assigns it one; 8080-8082 are taken by the compose file. Needed for §37's
  sliding-window metrics and for a compose healthcheck.
* **Startup deadline 60 s, backoff 0.5 s doubling to a 5 s cap.** Chosen to
  cover Redpanda's cold start in CI comfortably; not derived from any
  requirement. (Amended 2026-09-21, Amendment 4: the deadline is kept at
  60 s, now against measured cold starts — Kafka 4.3.1 in KRaft combined
  mode 5.1-5.8 s bare-JVM on a 4 vCPU host, nats-server 2.15.0 0.08 s to a
  200 from `/healthz?js-enabled-only=true` — so it is headroom for a slow
  CI host and for the stream-provisioning job the services now wait on,
  not a broker-startup budget; ADR-0013 Context, prerequisites 4 and 5.)
* **Shutdown deadline 8 s.** Derived from Docker's 10 s default grace period,
  which is itself an assumption about the deployment.
* **Exit code 2 for configuration errors.** Mirrors the conventional
  usage-error code; no spec requirement.
* **Poll interval 1 s for configuration; version must strictly increase.**
  `core/config/loader.py::watch` already polls at 1 s; the "greater than"
  rule is new, chosen so that two replicas rolling at different times cannot
  flap between versions, and so a rollback is always a new version (matching
  `docs/runbook.md`).
* **Trie readiness = replay caught up to the log end at start.** §33 says
  events after the snapshot are replayed; it does not say reads must wait for
  that. Chosen so a `GET /prefix` after `/readyz` reflects everything
  published before the process started — the property `test_recovery.py`
  asserts.
* **Detector readiness = caught up to the log end at start.** Same
  reasoning; the alternative (serve an empty view immediately) makes the read
  API lie after every restart.
* **Consumer group names fixed, no override.** Simplicity over flexibility; a
  rename is a breaking deployment step and should look like one.
* **Domain read endpoints return 503 while not ready.** The alternative
  (serve partial state) contradicts §22's "expose timestamps/version numbers
  for derived classifications" — a partially replayed trie has no honest
  `event_sequence` to report.
* **`run_maintenance`/`snapshot_now`/`reload_config` are public members.**
  They exist so `ManualClock`-driven tests can advance time deterministically
  instead of sleeping; they are the same coroutines the periodic loops call.
* **`build_service` takes an `InMemoryBus`, not a `Producer`/`Consumer`
  pair.** Mirrors `create_app(bus=...)` and the reason given in its docstring:
  a test keeps one bus reference and reads every topic back.

## Consequences

* Three new `__main__.py` bodies, four new `service.py` modules, one new core
  module (`hammertime.core.runtime`), and a real body for
  `hammertime.core.telemetry.logging.configure_logging`. `__main__.py`
  docstrings cite `Spec: section 47` in addition to their current sections.
* New environment keys, all optional with defaults:
  `HAMMERTIME_AGGREGATOR_BIND`, `HAMMERTIME_DETECTOR_BIND` (default
  `0.0.0.0:8082`, which the compose file already assumes but `.env.example`
  never listed), `HAMMERTIME_STARTUP_TIMEOUT_S`, `HAMMERTIME_SHUTDOWN_TIMEOUT_S`,
  `HAMMERTIME_CONFIG_POLL_INTERVAL_S`, `HAMMERTIME_AGGREGATOR_MAINTENANCE_INTERVAL_S`
  (default 1.0; the aggregator's expiry-sweep period),
  `HAMMERTIME_TRIE_MIN_PREFIX_LENGTH` (ADR-0010). `CHANGES` gets one line per
  user-visible addition (`/readyz`, the aggregator port, the config-reload
  rule); none is `BREAKING`.
* `deploy/docker-compose.yml`, `deploy/prometheus.yml` and `.env.example`
  change as in decision 10. Those files are not the architect's; the epic
  that implements this ADR carries them.
* The cross-service tests get a stable, documented surface to build on
  (`docs/spec/integration-scenarios.md`) without reading any service's source.
* The `integration` CI job still cannot pass until the aggregator, trie and
  detector services exist. This ADR makes their entry points and lifecycle
  uniform; it does not shrink that work.

## Amendment 1 (2026-09-17) — the surface through which the lifecycle is observed

Why: a `test-author` writing `hammertime.core` runtime tests purely from this
ADR and §47 could not construct the inputs or observe the outputs for several
decisions, because the ADR pinned *behaviour* without naming the *surface*
that exposes it. In every case below the implementation
(`packages/hammertime-core/src/hammertime/core/runtime.py`,
`services/ingest/src/hammertime/ingest/service.py`,
`services/ingest/src/hammertime/ingest/app.py`) was read first. Each item is
marked either **documents what exists** — the seam was already there and this
text makes it part of the contract — or **requires an implementation change**,
which is carried by a separate `coder` brief, never by silently redefining
the promise to match the code.

The decisions numbered 1-10 above are unchanged. Where an item narrows an
ambiguity in one of them, the narrower reading is now the contract.

### A1. "Transient" is defined by the caller, per dependency, with `OSError` as the floor — documents what exists, with one correction that required an implementation change

> Corrected 2026-09-17, same day, after a supervisor review: the first
> version of this item claimed that "a bad URL, an authentication failure or
> a protocol error is not in either set and fails the start on the first
> attempt". That was false of the code it was marked as documenting.
> `redis.exceptions.AuthenticationError` and `AuthorizationError` *subclass*
> `redis.exceptions.ConnectionError` (verified in the installed
> `redis/exceptions.py`, lines 35-44), which ingest lists as transient, so a
> rejected store credential was retried until the startup deadline. The user
> ruled that the code should change to match the stated intent; that change
> is on branch `claude/adr-0009-a1-a3-code` (commit `62990f5`) and is
> described under "The credential rule" below. **This item and A3 are true
> of the code only once that branch merges.** The "bad URL" half of the old
> sentence was also wrong for a different reason, ruled on under "What is
> transient" below.

Decision 5 step 5's retry lives in one core coroutine:

```python
async def connect_with_retry[T](
    dependency: str,                                     # "bus" | "store" (the log field)
    connect: Callable[[], Awaitable[T]],
    *,
    timeout_s: float | None = None,                      # None -> HAMMERTIME_STARTUP_TIMEOUT_S from os.environ
    transient: tuple[type[BaseException], ...] = (OSError,),
    logger: Any = None,                                  # None -> get_logger()
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> T
```

A failure is *transient* iff the exception raised by `connect()` is an
instance of a class in `transient`. There is no named "transient" exception
type and no wrapping: the caller says which classes mean "not up *yet*" for
its dependency, and the original exception is what propagates.

* The default `(OSError,)` covers the socket-level refusals every client
  library surfaces unwrapped — `ConnectionRefusedError`,
  `ConnectionResetError`, `BrokenPipeError`, and the builtin `TimeoutError`.
  On the Python this repo requires (>= 3.12) `asyncio.TimeoutError` is the
  builtin `TimeoutError` (verified in `/usr/lib/python3.12/asyncio/exceptions.py`,
  `TimeoutError = TimeoutError`), and the builtin is an `OSError` subclass, so
  a `connect()` that times out via `asyncio.wait_for` is transient by default.
* Ingest's lifespan (`app.py`) widens the set per dependency, because
  neither client library derives its connection errors from `OSError`
  (`redis.exceptions.RedisError(Exception)`; `aiokafka.errors.KafkaError(RuntimeError)`,
  both verified in the installed packages):

  ```text
  store   (OSError, redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
  bus     (OSError, aiokafka.errors.KafkaConnectionError)
  ```

* A non-transient failure propagates from the first attempt unchanged; no
  `dependency_unavailable` record is logged for it.

**What is transient: connectivity, including name resolution.** The
rationale in decision 5 — `depends_on` orders container start, not
dependency readiness — covers everything that can come right by waiting:
a refused or reset connection, a connect timeout, a store still loading its
dataset (`redis.exceptions.BusyLoadingError`, a `ConnectionError` subclass),
and a hostname that does not resolve *yet*, because during a deploy the
dependency's DNS record can legitimately appear after the service's
container starts. An unresolvable host is therefore **retried for the whole
startup deadline**, and that is the intended behaviour, not an oversight:
`socket.gaierror` subclasses `OSError` (the typeshed stub the repo's mypy
ships, `mypy/typeshed/stdlib/socket.pyi` lines 1217-1220: `error = OSError`,
`class gaierror(error)`; the Python docs at `docs.python.org` were not
reachable from this environment); redis-py additionally rewraps any `OSError`
from the socket connect as its own `ConnectionError`
(`redis/asyncio/connection.py` lines 865-866) and aiokafka's `bootstrap()`
swallows `OSError` per host and raises a bare `KafkaConnectionError`
(`aiokafka/client.py` lines 215-217, 241-242), so an unresolvable host
reaches `connect_with_retry` as a listed transient type on both
dependencies. A *malformed* URL is a different case and never reaches the
retry — nor, since Amendment 3, `start()` — at all: `load_settings` rejects
it with `ValueError` through `hammertime.store.validate_redis_url`, which
runs `Redis.from_url`'s own parser (`redis/asyncio/connection.py::parse_url`)
before any client exists, so it is `config_invalid` and exit 2 (A12).

**The credential rule (the correction).** A credential the dependency
actively rejects will never become valid by waiting, so it MUST fail the
start on the first attempt: no retry, no sleep, no `dependency_unavailable`
record, straight to `start_failed` -> exit 1. `connect_with_retry` cannot
express that on its own — it classifies strictly by `isinstance` against
`transient` and offers no exclusion — and the redis driver reports a rejected
credential as a *subclass* of the very class ingest has to list as transient.
The distinction is therefore drawn **inside the `connect` callable, before
the helper sees the exception**: ingest's `ping_store` catches
`_PERMANENT_STORE_ERRORS = (redis.exceptions.AuthenticationError,
redis.exceptions.AuthorizationError)` and re-raises a module-level
`DependencyAuthenticationError(Exception)` `from exc`. That type matches no
transient tuple, so the helper propagates it on attempt 1; the driver's own
exception is preserved as `__cause__` and appears in `start_failed`'s
traceback; the message deliberately excludes `HAMMERTIME_REDIS_URL`, which
carries the password (A7's no-credential rule).

This translation is the contract for **every** service that connects to a
credentialed dependency, and the aggregator, trie and detector epics MUST
apply it: when a client library's rejected-credential error is, by type, a
member of the tuple the caller must list as transient, the caller catches
it inside `connect()` and re-raises a type outside the tuple, chaining the
original. Listing the driver's connection-error class as transient and
assuming "auth is something else" is exactly the bug this correction fixes;
a new store backend written to the old wording would reintroduce it.

Ruling on the remaining `redis.exceptions.ConnectionError` subclasses, so
none is left implicit (the full set in the installed redis-py:
`AuthenticationError`, `AuthorizationError`, `BusyLoadingError`,
`ExternalAuthProviderError`, `MaxConnectionsError` in `exceptions.py`;
`MasterNotFoundError`, `SlaveNotFoundError` in `sentinel.py`):

| class | ruling | why |
| --- | --- | --- |
| `AuthenticationError`, `AuthorizationError` | permanent (translated) | the rule above |
| `BusyLoadingError` | transient | the server is up but loading its dataset — the canonical "not up yet" |
| `ExternalAuthProviderError` | transient (left as-is) | raised when a configured external credential provider "returns an error" (its docstring); a provider round trip can fail temporarily, and it is unreachable today because ingest builds its client from a plain URL with no `credential_provider` |
| `MaxConnectionsError` | transient (left as-is) | pool exhaustion, unreachable during the one-ping startup probe on a fresh pool; not worth a special case |
| `MasterNotFoundError`, `SlaveNotFoundError` | transient (left as-is) | Sentinel-only; ingest does not use Sentinel, and "no master known yet" during a failover is a wait-and-retry condition if it ever does |

**The bus has no counterpart today, and a known landmine if it ever
needs one.** aiokafka reports a rejected SASL credential as a
`BrokerResponseError` subclass built from the broker's error code
(`aiokafka/conn.py::_do_sasl_handshake`, lines 305-309 — e.g.
`SaslAuthenticationFailed`), and none of its authentication errors is or
subclasses `KafkaConnectionError` (`aiokafka/errors.py`: `AuthenticationFailedError(KafkaError)`,
`AuthenticationMethodNotSupported(KafkaError)`,
`UnsupportedSaslMechanismError(BrokerResponseError)`, ...). So the bus
tuple is correct as written for ingest's current configuration, which has no
`security_protocol` or SASL setting at all (no such key exists in
`hammertime.bus.kafka` or `.env.example`). **But** the translation approach
above will not carry over: `aiokafka/client.py::bootstrap()` catches
`(OSError, KafkaError, asyncio.TimeoutError)` per bootstrap host, logs and
`continue`s, and on exhausting the host list raises a bare
`KafkaConnectionError("Unable to bootstrap from ...")` with no `__cause__`.
A SASL rejection is therefore indistinguishable *by type* from a broker
that is still starting by the time `KafkaProducer.start()` raises. Whoever
adds bus credentials MUST decide explicitly how a rejected credential is
told apart from an unready broker — the options this ADR can see are a
pre-flight connection made outside `bootstrap()` so the real error is
visible, or an explicit, documented acceptance that a bad SASL credential
is retried until the startup deadline — and record the decision as an
amendment here. Silently inheriting the current tuple is not an option.

> Amended 2026-09-21 (ADR-0013 decision 3; Amendment 4): the bus tuple is
> no longer enumerated by each service. It is `hammertime.bus.nats.TRANSIENT_ERRORS`
> — `(OSError, nats.errors.NoServersError, nats.errors.TimeoutError,
> nats.errors.ConnectionClosedError, StreamNotProvisionedError)` — and
> ingest and the aggregator pass it as `transient=`. A registered stream
> that does not exist yet is transient (the provisioner may still be
> running). The landmine paragraph above does not apply to nats.py: its
> `AuthorizationError` and `InvalidUserCredentialsError` subclass
> `nats.errors.Error`, not any listed class, so a rejected credential is
> non-transient by type without a caller-side translation. The reference
> deployment configures no credentials; whoever adds them re-reads
> `nats/errors.py` at the pinned version and amends the tuple (ADR-0013
> assumption 13).

The schedule, exactly:

1. `deadline = monotonic() + timeout`, evaluated once on entry. `delay = 0.5`
   (`RETRY_INITIAL_DELAY_S`), `attempt = 0`.
2. `attempt += 1`; `await connect()`; on success return its value.
3. On a transient failure: log `WARNING event=dependency_unavailable
   dependency=<dependency> attempt=<attempt> error=<str(exc)>
   retry_in_s=<delay rounded to 3 places>`.
4. If `deadline - monotonic() <= delay`: re-raise that same exception. The
   sleep that would overrun the deadline is never started, so the last
   attempt is always made *before* the deadline, and what escapes is the
   last transient error, never a `TimeoutError` of the retry helper's own.
5. Otherwise `await sleep(delay)`; `delay = min(delay * 2, 5.0)`
   (`RETRY_MAX_DELAY_S`); go to 2.

So the sleeps are `0.5, 1, 2, 4, 5, 5, 5, ...` and attempt numbers are
1-based. The two constants are module-level names in `hammertime.core.runtime`.

Assumptions (push back individually):

* *Caller-supplied tuple rather than a named exception type.* Chosen because
  the redis and aiokafka errors share no base with each other or with
  `OSError`, and wrapping them into a Hammertime type would lose the original
  in `start_failed`. The aggregator, trie and detector epics MUST pass their
  own consumer's connection errors the same way; this ADR does not enumerate
  them because those clients are not written yet.
* *The final attempt's warning still carries `retry_in_s`.* It does today (the
  record is emitted before the deadline check). Kept as-is rather than
  special-casing the last record; an operator reading a `retry_in_s` followed
  by `start_failed` can tell what happened.
* *Which deadline fires first is unspecified.* `run_service` wraps the whole
  of `start()` in `asyncio.wait_for(..., HAMMERTIME_STARTUP_TIMEOUT_S)` and
  `connect_with_retry` defaults its own `timeout_s` to the same value but
  starts its clock later (inside `start()`), so either can win: the outer one
  produces `start_failed reason=startup_timeout`, the inner one `start_failed
  error=<last transient error>`. Both are exit 1; a test must not depend on
  which.
* *`timeout_s=None` reads `os.environ`, not a `run_service` `env=`.* A caller
  outside the runner (ingest's lifespan) has no other source; a test should
  pass `timeout_s` explicitly rather than rely on the environment.
* *Name resolution is transient.* Nothing in decision 5 or §47 mentions DNS;
  classifying an unresolvable host with a refused connection rather than
  with a rejected credential is this correction's judgment, on the grounds
  that both are "the dependency is not reachable yet" and both are what
  `depends_on` fails to order. Push back if a deploy environment is known
  where a missing record never appears and 60 s of retry is worse than an
  immediate crash-loop.
* *The translation lives in the caller, not as an exclusion parameter on
  `connect_with_retry`.* A `permanent=` tuple on the helper would have worked
  too. The caller-side catch was kept because it leaves the helper's
  contract (this item, A2, and every test written against it) untouched, it
  is where the knowledge of a given driver's hierarchy already lives, and
  it is the only place that can substitute a credential-free message for
  the driver's. Push back if a second service ends up copying the same
  three lines and a helper parameter would be cleaner.
* *`DependencyAuthenticationError` is ingest-local.* It is defined in
  ingest's `app.py`, not in `hammertime.core.runtime`, because nothing
  outside ingest raises or catches it yet; a shared type is a judgment for
  the first service that needs a second one.
* *`ExternalAuthProviderError` stays transient.* Chosen because it cannot
  be raised under today's configuration and its documented meaning is a
  provider error, not a verdict on the credential. If a `credential_provider`
  is ever configured for the store, that work MUST revisit this row: a
  provider that answers "these credentials are invalid" will look transient
  under the current classification.
* *The driver's error text is assumed credential-free.* `start_failed`'s
  traceback includes the chained redis error, whose message is the server's
  `AUTH`/`ACL` response text; the redis server does not echo the password
  in it. That is a property of the server, not of this code, and A7's
  no-credential rule is what a test should assert.

### A2. The backoff clock is injectable — documents what exists

`sleep=` and `monotonic=` in the signature above are part of the contract,
following the precedent of
`hammertime.core.config.loader.watch(path, poll_interval_seconds=..., sleep=...)`
(exercised by `test_config.py::TestWatch`). `connect_with_retry` MUST route
every wait through `sleep` and every clock read through `monotonic`; it never
calls `asyncio.sleep` or `time.monotonic` directly. A test that injects both
can therefore assert the whole schedule — the exact delay sequence, the
1-based `attempt` values, the deadline check in A1 step 4, and that a
non-transient error is never retried — without a real wait.

`logger=` is the third seam: any object with `info`, `warning` and `error`
methods of shape `(event: str, **fields)` (the structlog `BoundLogger`
method shape) is accepted, so the `dependency_unavailable` records can be
captured without touching process-wide logging.

### A3. `Service` is `@runtime_checkable` — requires an implementation change

Decision 3 declared `Service` as a plain `Protocol`. It is now
`@runtime_checkable`, for three reasons: every other cross-package protocol
in this repo is (`DedupStore`, `Producer`, `Consumer`, and this module's own
`DescribesStartup`), precisely so a conformance test can `assert
isinstance(impl, Proto)` (precedent:
`services/ingest/.../tests/test_dedup.py::TestProtocolConformance`);
`Service` is the widest such boundary — four implementations, one consumer;
and the cost is nil at runtime. Change: add the decorator to `Service` in
`runtime.py`. Nothing else moves.

Caveats that are part of the contract: `isinstance(x, Service)` checks that
the four members *exist* (on 3.12 via `inspect.getattr_static`, so the
`ready` property is not evaluated), not that `start`/`run`/`stop` are
coroutine functions or that `ready` is a `bool`. Static typing
(`run_service(name, factory: Callable[[], Service])`) remains the signature
check.

Assumption: the check is a test and tooling aid, not a runtime guard.
`run_service` does not `isinstance` its factory's result; adding that would
turn an error mypy already catches into an exit code, and nothing asked for
it.

### A4. The admin app takes a three-state `Readiness`; `service.ready` is its two-valued view — documents what exists

Decision 4 said `/readyz` is "200 iff `service.ready`" while its 503 body
distinguishes `starting` from `stopping`. Both are true of one object:

```python
class Readiness:
    state: str          # property: "starting" | "ready" | "stopping"; initially "starting"
    ready: bool         # property: state == "ready"
    def mark_ready(self) -> None      # end of start()
    def mark_stopping(self) -> None   # stop() called

def create_admin_app(readiness: Readiness, *, render_metrics: Callable[[], bytes] | None = None) -> ASGIApp
def healthz_response() -> AdminResponse                       # 200 {"status":"ok"}
def readyz_response(readiness: Readiness) -> AdminResponse    # 200 {"status":"ready"} | 503 {"status":<state>}
def not_ready_response(readiness: Readiness) -> AdminResponse # 503 {"status":<state>} — domain endpoints
def metrics_response(render: Callable[[], bytes] | None = None) -> AdminResponse
class ServiceNotReady(Exception)                              # raised by a FastAPI readiness gate; rendered via not_ready_response
```

A service's `ready` property MUST equal `readiness.ready` for the `Readiness`
its HTTP app reports (ingest: `app.state.readiness`, created in `create_app`
before the lifespan so `/readyz` answers before, during and after it). The
transitions the runner drives are `starting -> ready -> stopping` and, when a
signal arrives during `start()`, `starting -> stopping`. `stop()` marks
`stopping` *before* tearing anything down, so `/readyz` and the domain
endpoints flip to 503 while the connections they depend on are still open
(decision 7).

The pure-ASGI app also answers `404 {"status":"not_found"}` off its three
paths and `405 {"status":"method_not_allowed"}` for a non-GET on them, and
completes the ASGI lifespan protocol so a plain `uvicorn.Server` can drive
it.

Assumptions: the two error bodies above use the same `status` key as the
admin bodies rather than FastAPI's `{"detail": ...}` because the app exists
for a service with no FastAPI; nothing depends on them. `mark_ready()` after
`mark_stopping()` is unspecified — the runner never calls `start()` after
`stop()` — and a test must not exercise it.

### A5. `ConfigPoller` is the named carrier of decision 6 — documents what exists

```python
class ConfigPoller:
    def __init__(
        self,
        path: Path,                       # HAMMERTIME_CONFIG_PATH
        current: DetectionConfig,         # the version in force — positional, never absent
        *,
        apply: Callable[[DetectionConfig], Awaitable[None]] | None = None,
        poll_interval_s: float = 1.0,     # DEFAULT_CONFIG_POLL_INTERVAL_S
        logger: Any = None,
    ) -> None
    current: DetectionConfig              # property
    async def poll_once(self) -> DetectionConfig
    async def run(self) -> None
    def stop(self) -> None
```

Semantics, in the order a test would exercise them:

* **A version is always in force.** `build_service` loads the document
  before the service exists (decision 2), and that `DetectionConfig` is the
  poller's `current` from construction. There is no "no configuration yet"
  state and no poll during `start()`.
* **`run()` waits one interval before its first poll**, then polls every
  `poll_interval_s` until `stop()`; the version in force when `run()` begins
  is the one loaded at startup. `stop()` is idempotent and makes `run()`
  return at its next boundary (immediately if it is sleeping; after the
  current `poll_once()` completes if it is mid-poll).
* **`poll_once()`** reads the document once via `hammertime.core.config.loader.load`:
  * load or validation failure (`ConfigurationError` — which `load` raises
    for an unreadable file, invalid JSON, a schema violation or a broken
    invariant — or `ValueError`): `ERROR event=config_rejected path=<path>
    error=<str>`; returns `current` unchanged;
  * `candidate.config_version <= current.config_version`: returns `current`
    unchanged, silently;
  * otherwise `await apply(candidate)` (if given), *then* `current =
    candidate`, then `INFO event=config_applied path=<path>
    config_version=<new> previous_config_version=<old>`; returns the new
    `current`. The version is therefore never observable before the
    re-evaluation it triggered has been applied (decision 6, §47.3).
  * If `apply` raises, the exception propagates out of `poll_once()` (and so
    out of `reload_config()`), `current` is unchanged and no
    `config_applied` is logged. `run()` catches it, logs `ERROR
    event=config_apply_failed error=<str>` with the traceback, and keeps
    polling: a broken apply hook must not take the process down, and the
    next poll retries the same document.
* `reload_config()` on a service is exactly `await poller.poll_once()`.

The aggregator, trie and detector MUST use `ConfigPoller` for decision 6
rather than reimplementing it (their `apply` is their re-evaluation:
`reevaluate.py`, the prefix predicate re-run).

Assumptions: the "MUST use `ConfigPoller`" on the three unbuilt services is
this amendment's, made so the rejection/version rules cannot drift; push
back if a service genuinely needs a different poller. `ValueError` is in the
rejected set alongside `ConfigurationError` defensively (the loader
documents only `ConfigurationError`); it is kept because a rejected document
must never escape `poll_once()`. Whether `run()` should poll *immediately*
on entry (it does not) was not specified anywhere; the current behaviour is
pinned because the harness in `docs/spec/integration-scenarios.md` relies on
polling never happening on its own.

### A6. `AdminResponse` fields and body bytes — documents what exists

```python
@dataclass(frozen=True, slots=True)
class AdminResponse:
    status_code: int
    body: bytes
    media_type: str

METRICS_CONTENT_TYPE = "text/plain; version=0.0.4"
```

JSON bodies are `json.dumps(payload, separators=(",", ":")).encode("utf-8")`
with `media_type == "application/json"`, so the bodies in decision 4 and
`docs/protocol/read-api-v1.md` are byte-exact: `b'{"status":"ok"}'`,
`b'{"status":"ready"}'`, `b'{"status":"starting"}'`, `b'{"status":"stopping"}'`.
`/metrics` is `status_code == 200`, `media_type == METRICS_CONTENT_TYPE`, and
`body == render()` — or `b""` when `render` is `None`. The FastAPI services
send `body` verbatim with `content-type: <media_type>`; the pure-ASGI app
additionally sets `content-length`.

Assumption: byte-exactness (compact separators, key order as written) is
pinned rather than "any JSON encoding of this object" because the protocol
document already displays the compact form and both renderers pass the bytes
through untouched; a test may still compare parsed JSON if it prefers.

### A7. The structured log record — documents what exists

The carrier for every record named in decisions 5 and 7 is
`hammertime.core.telemetry.logging.configure_logging(name, level)`:

* It installs one handler on the **root stdlib logger**, writing to the
  `sys.stdout` object bound *at the time it is called*, and configures
  structlog to route through it. Records from stdlib loggers (uvicorn,
  `logging.getLogger(...)` call sites) are rendered by the same chain.
* Each record is **one JSON object per line**, keys sorted. Every record
  carries:

  | key | value |
  | --- | --- |
  | `event` | the event name (`starting`, `ready`, ...) |
  | `level` | lowercase: `debug`, `info`, `warning`, `error`, `critical` |
  | `service` | the `name` given to `configure_logging` |
  | `timestamp` | ISO 8601, UTC |
  | `exception` | present **iff** the record was emitted with a truthy `exc_info`: the formatted traceback as one string. The key `exc_info` itself never appears |

  plus the event's own fields, as given. (Key names follow structlog's
  `add_log_level`, `TimeStamper(fmt="iso", utc=True)` and `format_exc_info`
  processors, verified in the installed `structlog/_log_levels.py` and
  `structlog/processors.py`; the structlog site was not reachable from this
  environment, so those are the sources cited.)
* `HAMMERTIME_LOG_LEVEL` is a threshold: at `warning` no `info` record is
  written. A test asserting `starting`/`ready` must run at `info` (the
  default).

The events and their fields, as emitted today (decision 5/7 names first, the
implementation's additional ones after; a field in brackets is conditional):

| event | level | fields |
| --- | --- | --- |
| `config_invalid` | error | `error` |
| `starting` | info | `version` (from `importlib.metadata.version("hammertime-<name>")`) plus whatever `DescribesStartup.startup_fields()` returns — ingest: `bus_kind`, `bus_endpoints` (= `hammertime.bus.nats.bus_endpoints(HAMMERTIME_BUS_BROKERS)`, scheme and host[:port] only; was `bus_brokers` verbatim until 2026-09-21 — Amendment 5, ADR-0013 Amendment 2 ruling 3), `store_kind`, `config_path`, `config_version`, `bind`, [`store_endpoint` = `host:port/db` of `HAMMERTIME_REDIS_URL`, redis only] |
| `dependency_unavailable` | warning | `dependency`, `attempt`, `error`, `retry_in_s` |
| `start_failed` | error | either `reason="startup_timeout"`, `timeout_s` (outer deadline) or `error`, `exception` (any exception from `start()`, including the last transient error re-raised by A1 step 4) |
| `ready` | info | `startup_seconds` |
| `run_exited` | error | `error` (`null` if `run()` returned without raising), [`exception`] |
| `stopping` | info | `signal` (`"SIGTERM"` / `"SIGINT"`) |
| `shutdown_timeout` | warning | `timeout_s` |
| `shutdown_aborted` | warning | `signal` (the second signal, logged when it arrives) and then `reason="second_signal"` (when the drain is given up) — two records |
| `stopped` | info | `exit_code` (always `0`; exit 1 paths log their own reason instead) |
| `stop_failed` | warning | `error`, `exception` — `stop()` itself raised while cleaning up a failed start |
| `run_failed` | error | `error`, `exception` — an exception escaped the runner (decision 8's "nothing else maps") |
| `config_rejected` | error | `path`, `error` |
| `config_applied` | info | `path`, `config_version`, `previous_config_version` |
| `config_apply_failed` | error | `error`, `exception` |
| `config_poll_failed` (ingest) | error | `exception` — the poller task itself failed, logged from `run()`'s `finally` |

**The no-credential rule, as a testable statement (§47.2):** the rendered
text of every record — not just `starting` — MUST NOT contain the value of
`HAMMERTIME_INGEST_AGENT_TOKEN_KEY`, any agent bearer token, or the userinfo
component of `HAMMERTIME_REDIS_URL`. `startup_fields()` implementations MUST
therefore never return a settings object or a URL wholesale; ingest reduces
the Redis URL to `store_endpoint`.

> Amended 2026-09-21 (ADR-0013 Amendment 2 ruling 3; Amendment 5): the
> sentence that ended the paragraph above — "`bus_brokers` is logged
> verbatim (broker addresses are not secrets, decision 5 step 3)." — is
> superseded and removed. It was written for Kafka's `host:port` bootstrap
> list; a NATS URL may carry `user:password@` or `token@` in its userinfo
> (ADR-0013 decision 10, as amended), so the `starting` record carries
> `bus_endpoints = hammertime.bus.nats.bus_endpoints(HAMMERTIME_BUS_BROKERS)`
> — `<scheme>://<host>[:<port>]` per entry, userinfo dropped, never raising
> — in place of `bus_brokers`, and the rule above reads with "or of
> `HAMMERTIME_BUS_BROKERS`" after "the userinfo component of
> `HAMMERTIME_REDIS_URL`". Only the log field is renamed: the settings
> field keeps its name `bus_brokers` (ADR-0013 decision 10, Amendment 1
> ruling T9).

Test seams, in order of preference:

1. **`run_service(name, factory, env=...)`** — capture stdout (pytest
   `capsys`). Because `configure_logging` runs *inside* `run_service` and
   binds whatever `sys.stdout` is at that moment, the capture fixture's
   stream receives the lines; parse each with `json.loads`. This is the
   operator-visible contract and the only seam for the runner's own
   records.
2. **`connect_with_retry(logger=...)` / `ConfigPoller(logger=...)`** — a
   recording object with `info`/`warning`/`error(event, **fields)`.
3. `structlog.testing.capture_logs()` does **not** work around
   `run_service`: it operates by calling `structlog.configure`, which
   `configure_logging` calls again and overrides. Do not use it there.

Assumptions: stdout (not stderr), sorted keys, the `timestamp`/`level`/
`exception` key names and lowercase levels are all inherited from
`configure_logging` as written; they are pinned because a log pipeline will
match on them, so changing any of them later is a `CHANGES` entry (this
amendment makes no ruling on whether such a change would be `BREAKING`).
The events beyond decisions 5/7 (`stopped`, `stop_failed`, `run_failed`,
`shutdown_aborted`, `config_applied`, `config_apply_failed`,
`config_poll_failed`) are documented because they are observable, not
because a requirement asked for them; their names and fields are now
contract too.

### A8. `run()` binds the socket; in-process composition does not need it for ingest — documents what exists

Decision 3's reading of `Service.run()` ("serve until `stop()`") means the
service, not the runner, owns its HTTP server, so **every `run()` binds the
service's bind address** — ingest's is a `uvicorn.Server` on
`HAMMERTIME_INGEST_BIND`, and the aggregator/trie/detector `run()`s will bind
`HAMMERTIME_AGGREGATOR_BIND`/`HAMMERTIME_TRIE_QUERY_BIND`/`HAMMERTIME_DETECTOR_BIND`
the same way. `docs/spec/integration-scenarios.md` §1 said "no sockets are
opened" while §2 ran every `run()`; that contradiction is resolved in that
document (sockets are bound on `127.0.0.1:0` — loopback, ephemeral — and
never connected to; HTTP goes through `httpx.ASGITransport`).

For ingest specifically, the following is contract:

* `start()` runs the FastAPI lifespan and returns ready; `app` is usable
  through `httpx.ASGITransport` from that moment with no socket.
* `stop()` is complete without `run()`: it marks `stopping`, stops the
  poller, and unwinds the lifespan directly (closing the producer and the
  store) when no server is running. If `run()` is running, `stop()` sets
  uvicorn's `should_exit` and `run()`'s `finally` unwinds the lifespan after
  the drain instead.
* `run()` after `stop()` returns immediately without binding.
* The config poller runs **only inside `run()`**. A harness that never calls
  ingest's `run()` gets no automatic polling and MUST drive
  `reload_config()` explicitly — which the harness's `publish_config`
  already does for all four services.

Assumption: whether to run ingest's `run()` in the in-process harness is
left to the harness (both are supported); the scenarios document recommends
running it for uniformity of teardown. Ingest's `IngestService.__init__`
accepts `poll_interval_s=` to override `settings.config_poll_interval_s`;
that is a construction-time detail of ingest, not part of the `Service`
contract.

### A9. The not-ready 503 on `POST /v1/observations` — documents what exists

Decision 4 said ingest's ingestion endpoint "likewise answers 503" while not
ready. The body is now specified in `docs/protocol/observation-v1.md`: it is
deliberately `/readyz`'s body — `{"status":"starting"}` or
`{"status":"stopping"}`, `application/json` — rendered through
`not_ready_response`, and *not* the `{"detail": ...}` shape every other
ingest error (including the publish-failure 503) uses, so an agent or a probe
can tell "not ready, come back" from "ready but the bus refused". The gate is
a route-level dependency that runs before authentication, rate limiting and
the body read, so a not-ready request costs the agent nothing from either
budget (§36.6). Both 503s mean "retry with the same `sequence`".

Assumption: "costs nothing from either budget" is pinned from the
implementation (the gate is resolved before the limiters are touched); the
protocol document previously said a batch is charged "whatever its outcome",
which was written before the not-ready path existed. If the budget SHOULD be
charged even then, that is a `coder` change and a protocol-doc change
together.

### A10. Decision 9's shard parenthetical — corrected in place

The sentence "the shard id is what distinguishes members" presumed each
aggregator member carries a distinct static `HAMMERTIME_SHARD_IDS`. Under the
HPA-scaled Deployment `deploy/k8s/README.md` describes, every replica runs
the same environment, so that cannot be how members are told apart. Decision
9 now says members are distinguished by the broker's consumer-group
membership and leaves the shard/partition/member mapping to the aggregator
epic. No implementation exists yet for this; nothing changes in code.

### What this amendment requires of the implementation

Two changes, both on branch `claude/adr-0009-a1-a3-code` (commit `62990f5`),
which this amendment depends on — A1 and A3 describe the code truthfully
only once it merges:

1. `@runtime_checkable` on `Service` (A3).
2. Ingest's `ping_store` translates `redis.exceptions.AuthenticationError`/
   `AuthorizationError` into `DependencyAuthenticationError` so a rejected
   store credential fails the start on the first attempt (A1, "The
   credential rule"). `connect_with_retry` itself is unchanged.

Everything else above documents behaviour that already exists and is now
contract. Spec §47.2, §47.3, §47.6 and the new §47.7 are updated in step;
`docs/protocol/observation-v1.md`, `docs/protocol/read-api-v1.md` and
`docs/spec/integration-scenarios.md` carry A8/A9.

## Amendment 2 (2026-09-18) — the drain flushes the producer before it commits the consumer position

Why: decision 7's bullet list, and spec §47.4 which restates it, had a
draining service commit its consumer position and *then* flush its
producer. ADR-0010 decision 3 (the trie: "the producer is flushed before
the consumer position for the hot-ip topic is committed, so a crash cannot
commit an update whose stats were never emitted") and ADR-0011 decision 6
(the aggregator: the position is committed "always after
`producer.flush()`, so a committed position never precedes the transitions
it produced") require the opposite order, and ADR-0011 decision 6's
shutdown sentence — "`stop()` follows ADR-0009 decision 7: stop fetching,
finish the in-flight message, flush, commit, close bus and store clients"
— claimed to follow decision 7 while listing the steps in the other order.
ADR-0011 Amendment 6 noticed the discrepancy, left it alone as not its
question, and named it for a separate ruling. This is that ruling.

This amendment follows Amendment 1's convention: the decision is corrected
in place and carries a dated blockquote pointing here, and every edit made
outside this section is listed below with the superseded wording quoted.
Unlike Amendment 1, it changes a decision in substance — the order of two
steps — so the sentence in the status line that Amendment 1 leaves the
decisions "unchanged in substance" applies to Amendment 1 only.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Was: "Status: accepted; amended 2026-09-17 (see
  "Amendment 1" at the end — the decisions above it are unchanged in
  substance, the amendment pins the *surface* through which each one is
  observed and tested)". Now adds "; amended 2026-09-18 (see "Amendment 2"
  — decision 7's drain order is corrected in place: a service flushes its
  producer *before* it commits its consumer position, and that order binds
  every commit point of every service)".
* **Decision 7, the drain bullet list, bullets two to four.** Was:

  > * finishes the message it is currently applying, then commits its
  >   consumer position (`Consumer.commit`) — never mid-message, so
  >   at-least-once redelivery after a crash re-applies at most one message
  >   per partition (ADR-0003);
  > * flushes its producer;
  > * trie: writes a final snapshot (§33) *after* the commit above, so the
  >   snapshot's recorded position is never ahead of the committed one;

  Now:

  > * finishes the message it is currently applying — never stops
  >   mid-message, so at-least-once redelivery after a crash re-applies at
  >   most one message per partition (ADR-0003);
  > * flushes its producer;
  > * then commits its consumer position (`Consumer.commit`) — after the
  >   flush, never before it (the rule below);
  > * trie: writes a final snapshot (§33) *after* the commit above, so the
  >   snapshot's recorded position is never ahead of the committed one;

  The first and last bullets ("stops accepting new work ..." and "closes
  bus, store and socket resources") are unchanged. The "never mid-message
  ... at most one message per partition" clause moved from the commit
  bullet to the finish-the-message bullet, where the thing it explains
  (not stopping mid-message) now lives; its wording was carried over and
  not re-examined (see Assumptions).
* **Decision 7, new paragraph.** Between the bullet list and "Deadline met
  -> exit 0" — which were consecutive — the paragraph headed "**Flush
  before commit — one rule, every service, every commit point.**" was
  inserted. It is the rule this amendment makes, stated once; A11 below is
  its record.
* **Decision 7, blockquote.** A dated "Amended 2026-09-18" pointer was
  appended after the 8 s paragraph, in the form the Amendment 1
  blockquotes use.
* **`docs/spec/hammertime_spec_1.md`, §47.4, first paragraph.** Was: "On
  `SIGTERM` or `SIGINT` a service MUST stop accepting new work, finish the
  message it is applying, commit its consumer position, flush its
  producer, and (trie) write a final snapshot whose recorded position is
  not ahead of the committed one — all within
  `HAMMERTIME_SHUTDOWN_TIMEOUT_S` (default 8) — then exit 0. A drain that
  exceeds the deadline, or is interrupted by a second signal, exits 1."
  Now: the same sentence with "flush its producer, then commit its
  consumer position" in place of "commit its consumer position, flush its
  producer", plus one sentence stating that the flush MUST precede the
  commit at every commit point and why, citing this amendment. The final
  "A drain that exceeds ..." sentence is unchanged.

Touched nowhere else, and why — before closing this list `docs/spec/`,
`docs/adr/` and `docs/protocol/` were grepped (case-insensitively) for
`flush`, `commit`, `drain`, `snapshot.*position` / `position.*snapshot`,
and `docs/runbook.md` for `snapshot`, `shutdown`, `SIGTERM` and `drain`:

* `docs/spec/hammertime_spec_1.md` §33's ADR-0009 note ("On shutdown it
  writes a final snapshot after committing its consumer position (Section
  47.4)") orders only the snapshot relative to the commit, which is
  unchanged, and defers to §47.4 for the rest. Untouched: adding the flush
  there would be a second statement of the rule. §24's ADR-0011 note says
  which position is committed, not when relative to the flush. Untouched.
* ADR-0010 decision 3 already states the ruled order for the trie, with
  its reason. Untouched.
* ADR-0011 decision 6 already states the ruled order for the aggregator
  and its three commit points; its sentence "`stop()` follows ADR-0009
  decision 7: stop fetching, finish the in-flight message, flush, commit,
  close bus and store clients" is now literally true of decision 7.
  Decision 5's `commit_handled` and `on_revoked` bullets and Amendment 6
  (A20) restate flush-then-commit for the aggregator and are consistent.
  Amendment 6's grep note records that it noticed this discrepancy and
  deferred it; that is accurate history and is untouched. Editing ADR-0011
  to cite this decision instead of ADR-0010 for the general rule would cost
  an ADR-0011 amendment for no change in rule, and was not done.
* ADR-0003's Consequences (at-least-once; "the consumer tracks committed
  offsets per shard") and ADR-0004's flush-then-record-sequence rule for
  ingest are the posture this amendment relies on. Untouched.
* `docs/spec/integration-scenarios.md` mentions commit only in the
  `kill_trie()` harness row ("no final snapshot, no commit"), which is
  about skipping the drain, not its order. Untouched.
* `docs/protocol/` and `docs/runbook.md` contain no restatement of the
  drain sequence or of commit/flush ordering. `docs/spec/README.md`'s §47
  row maps the same sections to the same modules. Untouched.

### A11. Flush before commit — documents what exists; corrects decision 7 and §47.4

**Classification: (a) already determined and missed.** The order was
settled before this ADR listed the steps: ADR-0003 fixes at-least-once
consumption as the posture every consumer is built to absorb, and ADR-0010
decision 3 — written alongside this ADR, which cites it in decision 2 —
states flush-before-commit for the trie with the reason ("a crash cannot
commit an update whose stats were never emitted"). Decision 7 gave no
reason for putting the commit first; the only rationale attached to its
commit bullet ("never mid-message ...") is about finishing the in-flight
message before committing, which both orders satisfy. §47.4 copied the
list. ADR-0011 decision 6 then followed ADR-0010 and cited decision 7 for
the drain skeleton while listing the flush first. What is new here, and
is a (b) ruling rather than a correction, is scope: the rule is now stated
once, in decision 7, as binding every service and every commit point,
rather than per service in two other ADRs.

**The ruling.** Flush, then commit. The reasoning is the paragraph
inserted in decision 7, and it is not a close call: commit-then-flush turns
a crash between the two steps into silent, unrecoverable loss of
transitions — the same class of loss ADR-0011 Amendment 6 (A20) closed at
revocation, arriving from the other direction — while flush-then-commit
turns the same crash into a redelivery every consumer is already required
to absorb.

**The trie's snapshot constraint holds.** Decision 7's trie bullet is
unchanged: the final snapshot is written after the commit, so its recorded
position is never ahead of the committed one — at a clean drain the two
are equal, because the snapshot is taken immediately after a commit of the
handled position with fetching stopped. Moving the flush ahead of the
commit does not disturb that; it adds that the snapshot is also after the
flush, so a restart that loads the snapshot and replays from its position
(decision 4) cannot skip an event whose `PrefixStatsChanged` never reached
the log. That is the property the snapshot actually needs from the drain
order, and under the old order it held only by accident of the flush
happening before the snapshot as well.

**Shipped code — no change.** Read at `78936af` (branch
`claude/flush-before-commit`):

* `services/aggregator/src/hammertime/aggregator/sharding/assignment.py`:
  `ShardClaims.commit_handled` is `await self._producer.flush()` then
  `await self._consumer.commit(offsets)` (lines 148-149); `on_revoked`
  calls it (line 185). The aggregator's only commit path.
* `services/aggregator/src/hammertime/aggregator/worker.py`:
  `stop()` sets the stop event, takes the lock and calls
  `_flush_and_commit` (lines 209-211), which is `commit_handled()` (line
  452); the periodic commit goes the same way (line 443). Its docstrings
  already say "ADR-0009 decision 7 and ADR-0011 decision 6: always flush
  before committing" — a citation of decision 7 for an order decision 7
  did not state until now; it is accurate after this amendment and needs
  no edit.
* `services/aggregator/src/hammertime/aggregator/service.py`: `stop()`'s
  docstring (lines 232-236) already says "flush the producer and commit
  the handled position -- in that order". Accurate; no edit.
* `services/ingest/`: no consumer, no commit; `stop()` unwinds the lifespan
  which flushes and closes the producer. Bound trivially.
* `services/trie/` and `services/detector/`: no `flush` or `commit` in any
  source file — `worker.py` and `snapshot/writer.py` are stubs. The rule
  binds their epics; there is nothing to change yet.
* `packages/hammertime-core/src/hammertime/core/runtime.py`: the runner
  calls `service.stop()` and waits for `run()`; it does not itself flush,
  commit or snapshot, so the order is each service's to implement.

No committed test changes. `test_sharding.py::TestRevokingAShard::
test_a_revoke_flushes_before_it_commits` (`assert trace == ["flush",
"commit"]`, line 509) and
`test_commit_handled_flushes_and_commits_even_with_nothing_handled` (line
655) assert the ruled order; `test_worker.py::TestOffsetsAreCommittedAtShutdown`
(lines 984-1033) asserts that `stop()` commits, without asserting an order
relative to the flush, and stays valid. No test asserts commit-then-flush.

No `CHANGES` entry: no shipped behaviour changes (the code already does
what the corrected text says), and the aggregator has not shipped in any
release; this is a documentation correction.

Assumptions (push back individually):

* *The rule covers every commit point, not only the drain.* Decision 7 is
  about shutdown; ADR-0011 decision 6 and ADR-0010 decision 3 already apply
  the order to periodic and revocation commits per service. Stating it
  here for all commit points is this amendment's generalisation, made so
  the aggregator's and the trie's rule have one origin. Ingest is bound
  vacuously (no consumer). The detector's commit point does not exist yet
  and is bound when it does.
* *"Flush" is the `Producer.flush()` contract — "wait until every message
  published so far is durably acknowledged" (`hammertime.bus.interface`,
  line 67) — not what the shipped producers happen to do.* Both shipped
  `publish` implementations already wait for the acknowledgement
  (`KafkaProducer.publish` is `send_and_wait`, `kafka.py` line 79;
  `MemoryProducer.publish` appends synchronously and its `flush` is a
  documented no-op, `memory.py` lines 78-84; ADR-0011 decision 4 step 4
  relies on this), so for the aggregator today the flush carries nothing
  and the order has no observable effect. The rule is stated against the
  interface because a producer that batches — the trie's twenty-five
  `PrefixStatsChanged` per transition are the case ADR-0010 designed for,
  and aiokafka's `send()` without `_and_wait` is one line away — is
  exactly where the order decides between loss and redelivery.
* *Redelivery after a crash between flush and commit is the accepted
  failure, and every consumer is assumed idempotent under it.* For the
  aggregator that is ADR-0011 decision 3's at-least-once paragraph; for
  the trie, §46.5 and §11 as ADR-0011's Consequences restate them. For the
  detector it is the "latest known stats" reading ADR-0010 decision 5
  gives its view, which is idempotent by construction — but the detector
  is unbuilt, so that is a requirement on its epic, not a verified
  property.
* *The trie's periodic (non-drain) snapshot is not ruled here.* The same
  reasoning says a periodic snapshot must not cover an event whose stats
  have not been flushed, or a restart from it skips re-emitting them.
  ADR-0010's Consequences already defer how the snapshot records its
  position to the trie epic; that epic should settle the periodic
  snapshot's relation to the flush at the same time. Named, not decided,
  because it is the trie's design and outside this amendment's one item.
* *The "at most one message per partition" clause was moved, not
  re-examined.* It travelled with the "never mid-message" reasoning it
  belongs to. Whether it is exactly right under a 1 s periodic commit
  (ADR-0011 decision 6), where a crash re-delivers everything handled
  since the last commit rather than one message, is a separate question
  about that sentence and is not this amendment's; it is named in the
  hand-off report.
* *The 2026-09-18 date and the "A11" numbering* continue Amendment 1's
  sequence; nothing else in this ADR's numbering moves.

## Amendment 3 (2026-09-18) — a connection URL the client would refuse is invalid configuration

Why: decision 2 and spec §47.1 step 3 promise that an invalid configuration
is rejected before any bus, store or socket is opened and exits 2. A
malformed `HAMMERTIME_REDIS_URL` did not behave that way (issue #73).
Neither `services/ingest/src/hammertime/ingest/config.py` (line 192) nor
`services/aggregator/src/hammertime/aggregator/config.py` (line 146) parses
the value; each `load_settings` stores it as an opaque string, and the first
thing to interpret it is `Redis.from_url(...)` — inside ingest's lifespan
(`app.py` line 209, entered by `start()`) and inside the aggregator's
`build_service` (`service.py` line 357). `Redis.from_url` parses the URL at
construction (`redis/asyncio/client.py` line 206 ->
`ConnectionPool.from_url`, `redis/asyncio/connection.py` line 2613 ->
`parse_url`, line 1768; redis-py 8.1.0 per `uv.lock`), raising `ValueError`
for an unsupported scheme (lines 1815-1819) or an ill-typed query parameter
(lines 1780-1783). For ingest that `ValueError` escapes `start()` after
`starting` has been logged and is reported as `start_failed` and exit 1 —
the code that means "the dependency was unreachable, retry". For the
aggregator it escapes the factory and is caught as `config_invalid`/exit 2
by accident of where `build_service` happens to construct the client, not
by any rule; a later reordering of that function would silently move it.
The issue left one question to this ADR: whether the check belongs in each
service's `load_settings` or in a shared helper, given that the trie and
detector will need it too. Between filing and this amendment the aggregator
gained store configuration, so the defect exists in two services and the
"will need it" is no longer hypothetical.

This amendment follows Amendment 1's convention: the decision is corrected
in place with a dated blockquote pointing here, and every edit made outside
this section is listed below with the superseded wording quoted. Like
Amendment 2 it changes contract — it adds a validation `load_settings` did
not previously perform — so the status line's "unchanged in substance"
applies to Amendment 1 only.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Appended "; amended 2026-09-18 (see "Amendment 3" —
  decision 2's "before any bus, store or socket is opened" guarantee is
  corrected in place to cover a connection URL the client library would
  refuse at construction: `HAMMERTIME_REDIS_URL` is validated in
  `load_settings`, through `hammertime.store.validate_redis_url`, whenever
  the store kind is `redis`)" after Amendment 2's clause.
* **Decision 2, new paragraph and blockquote.** Between the paragraph
  ending "A service must never half-start on a bad configuration." and the
  heading of decision 3 — which were consecutive — the paragraph beginning
  "That guarantee covers the *construction* of a client" and a dated
  "Amended 2026-09-18" blockquote were inserted. No existing sentence of
  decision 2 was changed.
* **A1, the "malformed URL" sentence.** Was: "A *malformed* URL is a
  different case and never reaches the retry at all: `Redis.from_url`
  rejects it with `ValueError` at construction
  (`redis/asyncio/connection.py::parse_url`, line 1817), before `connect()`
  is first called." Now: "A *malformed* URL is a different case and never
  reaches the retry — nor, since Amendment 3, `start()` — at all:
  `load_settings` rejects it with `ValueError` through
  `hammertime.store.validate_redis_url`, which runs `Redis.from_url`'s own
  parser (`redis/asyncio/connection.py::parse_url`) before any client
  exists, so it is `config_invalid` and exit 2 (A12)." The old sentence was
  literally true and is what made the defect invisible: it described the
  driver rejecting the URL without saying that, for ingest, this happened
  inside `start()`.
* **`docs/spec/hammertime_spec_1.md`, §47.1, step 3.** Was: "reject an
  invalid configuration before opening any network connection, exiting
  with status 2;". Now: "reject an invalid configuration before opening
  any network connection, exiting with status 2 — where "invalid" includes
  a connection URL the client library would refuse to build a client from
  (`HAMMERTIME_REDIS_URL` is validated when `HAMMERTIME_STORE_KIND` is
  `redis` and ignored when it is `memory`; ADR-0009 A12);". §47.5 is
  unchanged: its "2 configuration invalid, detected before any connection
  was made" is what the correction restores.
* **`docs/spec/README.md`, the §47 row.** `packages/hammertime-store`
  (`validate_redis_url`) is added to the "Implemented in" column, because
  the helper's module docstring cites §47.

Touched nowhere else, and why — before closing this list `docs/` was
grepped for `before any (bus|store|socket|network)`, `exit code`, `exit 2`,
`exit 1`, `status 2`, `status 1`, `config_invalid`, `start_failed`,
`half-start`, `malformed`, `invalid configuration`, `HAMMERTIME_REDIS_URL`
and `redis_url`:

* ADR-0009 decision 5 step 2 ("Any `ValueError`/`ConfigurationError` ->
  one `ERROR` record `event=config_invalid` with the message -> exit 2"),
  decision 8's table and A7's `config_invalid` row describe the path the
  corrected behaviour takes; they were already right and are untouched.
  Decision 5 step 3 and A7's `starting` row (the `store_endpoint`
  reduction of `HAMMERTIME_REDIS_URL`) are about logging a URL that has
  been accepted, unchanged. The first Assumptions bullet's "exit 1 (a
  `ValueError` traceback) instead of 2 on a bad setting" is history about
  ingest's pre-runner `main()`; untouched.
* ADR-0011 decision 9 lists `HAMMERTIME_REDIS_URL` among the keys the
  aggregator's `load_settings` reads without saying how; the new rule
  applies to it through decision 2 and needs no restatement there. ADR-0011
  A3 (line 1319: `parse_shard_ids` lets `load_settings` "exit 2 before any
  bus, store or socket is opened") is an instance of the guarantee, not a
  restatement of its scope; untouched. ADR-0011 A20 (`startup_fields`) is
  about the `starting` record; untouched.
* `docs/protocol/`, `docs/runbook.md` and `docs/spec/integration-scenarios.md`
  contain no statement of the exit codes or of configuration validation.
  `deploy/k8s/README.md` (outside `docs/`, read only) likewise.
* `.env.example` line 26 documents the key with its default and no
  grammar; the default is valid under the rule and nothing there is wrong.
  It is not the architect's file; whether to add a comment naming the
  accepted schemes is left to the coder brief.

### A12. `HAMMERTIME_REDIS_URL` is validated in `load_settings`, by the driver's parser, through one helper in `hammertime-store` — requires an implementation change

**Classification of the defect: (a) already determined and missed.**
Decision 2 and §47.1 step 3 fixed the guarantee; A1 even named the exact
call that rejects a malformed URL; what nobody wrote down is that for
ingest that call sits inside `start()`. The five questions the issue and
the dispatch left open are classified individually below.

**1. Where the validation lives — (b) genuinely unspecified, now ruled: a
shared helper in `hammertime-store`, called from each service's
`load_settings`.**

```python
# packages/hammertime-store/src/hammertime/store/url.py   (new; docstring `Spec: section 47`)
REDIS_URL_ENV = "HAMMERTIME_REDIS_URL"

def validate_redis_url(value: str, *, name: str = REDIS_URL_ENV) -> None:
    """Raise `ValueError` naming `name` iff `redis.asyncio.Redis.from_url(value)`
    would raise `ValueError` while building the client. Returns nothing and
    never alters `value`."""
```

Re-exported from `hammertime.store` (`__init__.py`, alongside the store
classes) so a service imports it from the same place it imports its store.
Each `load_settings` calls it after `store_kind` has been parsed:

```python
redis_url = source.get("HAMMERTIME_REDIS_URL", _DEFAULT_REDIS_URL)
if store_kind == "redis":
    validate_redis_url(redis_url)
```

and stores the identical string in `redis_url: str`, which `Redis.from_url`
later receives unchanged. Nothing about the settings dataclasses, `app.py`
or `service.py` changes.

Why the store package and not `hammertime-core` or per-service copies: the
issue's own trade-off is decisive. The property that matters is "accepts
exactly what `Redis.from_url` will accept", which only the driver's parser
can promise, and `hammertime-core` does not depend on `redis`
(`packages/hammertime-core/pyproject.toml`: `pydantic`, `prometheus-client`,
`structlog`); adding it there would pull a driver into `hammertime-bus`,
`hammertime-testkit` and every consumer of core for one function.
`hammertime-store` already depends on `redis>=5.0`, already owns the Redis
backends the URL is for, and is already a dependency of both services that
have the setting (`services/ingest/pyproject.toml`,
`services/aggregator/pyproject.toml`) and of any future service that gains
one — a service cannot have a Redis store without it. Per-service copies
were rejected for the reason the issue gave: four `load_settings` would
hold four answers to "what is a valid URL", and the two that exist today
already diverged from the guarantee in the same way.

Why it returns `None` rather than the parsed form: the URL stays the single
representation the client is built from. Returning `parse_url`'s kwargs
would invite a caller to construct the client from them instead of from
the string, creating a second construction path that can drift from the
first, and would change the settings field's type for no consumer that
needs it. The check is validate-and-discard by design.

**2. What counts as valid — (b) now ruled: exactly what the driver
accepts.** `validate_redis_url` calls `redis.asyncio.connection.parse_url`
and translates its `ValueError` into its own (item 4). It performs no check
of its own beyond that call — no scheme list, no host requirement, no
`urllib.parse` pre-check — because any rule the helper adds is a second
definition of validity that the driver does not share. The consequences of
that choice are stated so nobody mistakes the check for more than it is:

* Accepted: `redis://`, `rediss://` and `unix://` URLs, with or without
  userinfo, host, port, path-db and query parameters — including
  `redis://` alone (the pool defaults host and port) and `redis://h/abc`
  (a non-integer path db is silently ignored, `connection.py` lines
  1806-1810). Validity means "the driver will build a client from this",
  not "this is well-formed by any RFC" and not "a server answers there".
* Rejected: the empty string and whitespace-only (no scheme); a bare
  `host:port` (no scheme); any other scheme (`http://`, `redis+sentinel://`,
  ...); a typed query parameter the driver cannot cast (`?db=abc`,
  `?socket_timeout=x`); and whatever `urllib.parse.urlparse` itself refuses
  (an unbalanced IPv6 bracket, a non-numeric or out-of-range port, which
  `parsed.port` raises `ValueError` for).
* Set-but-empty `HAMMERTIME_REDIS_URL=` with `store_kind == "redis"` is
  rejected, consistent with ADR-0011 A3's rule that a set-but-empty value
  is an error, not the default; an *unset* variable takes the default
  `redis://localhost:6379/0`, which is valid.
* The helper is given the exact string that will later be passed to
  `from_url`, and neither it nor `load_settings` strips or normalises it.
  That is what makes "accepted here iff accepted there" hold; a caller that
  transforms the value between the two calls breaks it.
* `parse_url` is a module-level, unprefixed function of
  `redis.asyncio.connection` (and of `redis.connection`), not exported from
  any `redis` `__init__`. The asyncio one is chosen because both services
  build `redis.asyncio.Redis`. Isolating the import in one helper is also
  the mitigation: if a later redis-py moves or renames it, one line changes.

**3. When it applies — (a) for the scope, (b) for the edge: only when
`store_kind == "redis"`; under `"memory"` a set-but-malformed URL is
ignored.** The scope was already determined: both settings dataclasses
document the field as "meaningful only when store_kind == redis", the
`bus_brokers` field carries the parallel "meaningful only when bus_kind ==
kafka" and is not validated under `memory`, and the three ingest tests that
build `IngestSettings` directly do so with `store_kind="memory",
redis_url=""` (`test_routes.py` line 56, `test_pipeline.py` line 170,
`test_auth_throttle.py` line 148), which is also what an operator does when
flipping a local stack to `memory` without clearing the URL. What was not
determined is the set-but-malformed case under `memory`, ruled *ignored*: a
value that is never interpreted cannot cause a half-start, so rejecting it
would refuse a configuration that works. The check is placed in
`load_settings` after `store_kind` is parsed, and the dataclasses gain no
`__post_init__`: direct construction stays unchecked, as it is for every
other field (`port` is not range-checked there either).

**4. The error's shape — (a) for the requirements, (b) for two details.**
Required by decision 2 ("raises `ValueError` naming the variable") and by
A7's no-credential rule and #71 (the URL carries the password):
`ValueError`; the message contains the variable name (`name`, default
`HAMMERTIME_REDIS_URL`) and states the accepted grammar; and it contains no
part of the value — not the whole URL, not its userinfo, host, port, path or
query, and not a redacted form either. Recommended text, not pinned
byte-for-byte: `HAMMERTIME_REDIS_URL must be a Redis URL (redis://, rediss://
or unix://) with well-formed query parameters; the value is not repeated here
because it may carry a password`. Two details are this amendment's ruling:

* *The driver's exception is not chained* (`raise ... from None`). A1's
  credential rule chains the driver's error because the server's `AUTH`
  text never echoes the password. Here the parser can: for
  `redis://user:secret/0` (an `@` forgotten) `urlparse` reads `secret` as
  the port and `parsed.port` raises `Port could not be cast to integer
  value as 'secret'`. `config_invalid` renders `str(exc)` only
  (`runtime.py` line 603, no `exc_info`), so a chained cause would not reach
  the log today, but a traceback anywhere else — a test failure, a future
  `exception` field on the record — would. Suppressing the context closes
  that path at the cost of not telling the operator *which* part was bad;
  the grammar in the message is the substitute.
* *The `startup_fields` redaction is paralleled, not reused.* Both services
  reduce the URL to `hostname:port/path` with `urlsplit` for the `starting`
  record (ingest `service.py` lines 193-196, aggregator lines 275-278).
  That runs only on a URL `load_settings` has already accepted, and it is
  the right tool there. It is the wrong tool for the error path: a
  malformed URL is precisely the input on which a redaction cannot be
  trusted (the example above puts the password where `urlsplit` reports
  the port). So the error carries nothing derived from the value, and the
  two code paths stay separate on purpose.

**5. The trie and detector — (b) now ruled: bound when they gain the
setting; nothing now.** `services/trie/src/hammertime/trie/config.py` and
`services/detector/src/hammertime/detector/config.py` are `TODO` stubs with
no store setting. The corrected decision 2 is general — any `load_settings`
that reads `HAMMERTIME_REDIS_URL` MUST call `validate_redis_url` when its
store kind is `redis` — so their epics inherit the rule and the helper
without a further amendment. No code changes for them here.

**Shipped code — what changes.** All carried by the coder brief, none by
this amendment: the new `hammertime.store.url` module and its re-export;
one `validate_redis_url(redis_url)` call under `store_kind == "redis"` in
each of ingest's and the aggregator's `load_settings`; and a `CHANGES`
entry, because the exit status and log record an operator sees for a
malformed URL change (not `BREAKING`: a deployment with a malformed URL was
not running). `app.py` line 209 and `service.py` line 357 keep calling
`Redis.from_url(settings.redis_url)`; after the fix that call cannot raise
`ValueError` for a settings object that came through `load_settings`.

**Existing tests — none must change.** No test calls `load_settings` with
`HAMMERTIME_REDIS_URL` set (grepped `services/`, `packages/`, `tests/`);
the three direct constructions with `redis_url=""` use `store_kind="memory"`
and are unaffected because the dataclass does not validate;
`test_runtime.py::TestExitCodeTwoOnInvalidConfiguration` (lines 434-472)
tests the runner with a fake factory and stays valid. The new tests are the
test-author brief's.

Assumptions (push back individually):

* *`hammertime-store` rather than a new `hammertime-config` package or the
  service layer.* Chosen for the dependency reasons above; the cost is that
  a URL-validation function lives in a package whose docstring is about
  stores. Judged acceptable because "what URL can this backend be built
  from" is the backend's knowledge.
* *Module name `hammertime.store.url` and function name
  `validate_redis_url`.* No precedent in the repo for a validator module;
  named for what it validates. A `name=` keyword with the env-key default
  is provided so a tool or a differently named future variable gets a
  correct message; nothing today passes it.
* *Returns `None`.* Reasoned above; push back if a consumer for the parsed
  form appears (none exists).
* *No validation beyond the driver's.* The permissive cases listed under
  item 2 (`redis://` alone, ignored non-integer path db) are accepted on
  purpose. A stricter rule — require a host, require an integer db — would
  reject values the driver serves happily and would be Hammertime's own
  grammar to maintain; nothing asked for one.
* *Only `ValueError` is translated.* Anything else `parse_url` raises
  propagates unchanged; none was found in redis-py 8.1.0's code path
  (`urlparse`, `parse_qs`, `unquote`, the typed casts, all `ValueError`),
  and hiding an unexpected exception type behind a configuration message
  would mask a driver bug.
* *`from None`.* Reasoned under item 4; the alternative (`from exc`,
  matching A1) was rejected for the port-echo case. Push back if the loss
  of the driver's "Invalid value for 'db'" detail is judged worse.
* *The message text is recommended, not pinned.* Tests assert the
  variable name is present and no component of a distinctive test value
  is; the exact wording is the coder's, as it is for every other
  `load_settings` message.
* *Ignored, not rejected, under `memory`.* Reasoned under item 3; the
  alternative would be consistent with "refuse the silent
  misconfiguration" but would refuse a working one.
* *Error precedence when several settings are invalid is first-in-load-
  order, as today.* `store_kind` is parsed before the URL, so an invalid
  kind is reported and the URL never checked; unchanged behaviour, stated
  so nobody tests for the URL error in that case.
* *Nothing is ruled about `HAMMERTIME_BUS_BROKERS`.* It is likewise stored
  unparsed, but aiokafka does not parse it at construction, so the same
  defect does not arise in the same way; whether a malformed broker list
  should be rejected by `load_settings` is a separate question, named in
  the hand-off report and not settled here.
* *Nothing is ruled about the `startup_fields` redaction itself.* Its
  `hostname:port/path` rendering (`None` for a URL with no port, no
  hostname for `unix://`) and its duplication across two services are
  observations for a separate ticket, not this amendment.
* *The 2026-09-18 date and the "A12" numbering* continue the sequence;
  ADR-0011 has its own "A12" and the two are unrelated.

## Amendment 4 (2026-09-21) — the lifecycle under NATS JetStream (ADR-0013)

Why: ADR-0013 replaces Apache Kafka with NATS JetStream and drops
group-managed shard assignment. Four statements of this ADR were written
against Kafka's primitives — decision 9 (consumer groups and group
membership), decision 10 (the broker healthcheck), the 60 s startup
deadline's rationale, and A1's bus transient tuple and its "landmine"
paragraph — and one Amendment 1 item (A10) corrected decision 9 in a
direction ADR-0013 reverses. This amendment follows Amendment 1's
convention: each statement is corrected in place with a dated note, and
every edit outside this section is listed here with the superseded wording
quoted. The decisions' substance — one runner, environment-only
configuration, readiness, drain order, exit codes — is unchanged; the
flush-before-commit rule of Amendment 2 reads "flush before acknowledge"
for services with durable subscriptions and "flush before snapshot" for
the trie (ADR-0013 decision 9), which is the same rule.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Appended the "amended 2026-09-21" clause.
* **Decision 9, dated blockquote after the paragraph.** Superseded
  wording, second half of the paragraph: "Sharded aggregators (§20) share
  the one group; members are distinguished by the broker's group
  membership, not by `HAMMERTIME_SHARD_IDS` (which cannot differ per
  member under the HPA-scaled Deployment in `deploy/k8s/README.md`, where
  every replica gets the same environment), and how shards map onto
  partitions and members is the aggregator epic's design (A10)." Now:
  members are distinguished by their disjoint, required
  `HAMMERTIME_SHARD_IDS` sets; the group names are durable-consumer name
  prefixes; the trie holds no durable. The first half of the paragraph
  (fixed names, no override, rename is `BREAKING`) stands with "committed
  offsets" read as "acknowledged positions on the durables".
* **A10** is thereby reversed in effect but left as written: it was a
  correct correction of the sentence it addressed under the HPA design,
  and its own text says "No implementation exists yet for this". The
  blockquote at decision 9 says so.
* **Decision 10, dated blockquote after the paragraph.** Superseded
  wording: "`depends_on` conditions of `service_healthy` on `redpanda`
  (`rpk cluster health`) and `redis` (`redis-cli ping`)". Now: `nats`
  (`/healthz?js-enabled-only=true` via `wget` in the `-alpine` image),
  `valkey` (`valkey-cli ping`), plus `provision` with
  `service_completed_successfully`. The healthcheck and `--wait` are
  ADR-0013 decision 11's deployment contract; this ADR still does not edit
  `.github/workflows/ci.yml`.
* **Assumptions, the 60 s bullet.** Gained the parenthetical quoting the
  measurements. The value is unchanged.
* **A1, dated blockquote after the "landmine" paragraph.** Superseded
  wording, the bus line of the per-dependency table: "bus (OSError,
  aiokafka.errors.KafkaConnectionError)". Now
  `hammertime.bus.nats.TRANSIENT_ERRORS`. The store line is unchanged. The
  landmine paragraph is left as history of why the credential rule exists;
  the blockquote records that nats.py does not present the same trap.

Touched nowhere else, and why: spec §47.2's sentence naming
`aiokafka.errors.KafkaConnectionError` is reworded by ADR-0013 (listed in
its hand-off report); §47.4's "commit its consumer position" is left as
written, because the aggregator and the detector still commit one (by
acknowledgement) and the trie's case is stated by §33's note; decisions
1-8 name no broker; `docs/protocol/` names none.

Assumptions made by this amendment (push back individually):

* **Keep 60 s rather than lower it.** Both measured cold starts are far
  under it, but the deadline now also covers the provisioning job and
  `dependency_unavailable` retries against a stream that is "not there
  yet"; lowering it buys nothing and would make a slow CI host fail a
  deploy. The number stays a chosen value.
* **The trie's drain is "flush, then snapshot".** ADR-0013 decision 9
  removes the trie's consumer position; decision 7's list is not rewritten
  because "commit its consumer position" is vacuous for a service that has
  none, exactly as Amendment 2 already said of ingest. Push back if a
  literal rewrite of the bullet list is preferred.
* **No CHANGES entry**: the deployment and configuration changes are
  ADR-0013's.

## Amendment 5 (2026-09-21) — the `starting` record carries `bus_endpoints`, not `bus_brokers` (ADR-0013 Amendment 2)

Why: ADR-0013 Amendment 2 ruled that `HAMMERTIME_BUS_BROKERS` may carry
userinfo (`nats://user:password@host:4222`, `nats://token@host:4222`) and
that no log record of any service or tool may contain it, reducing the
value through `hammertime.bus.nats.bus_endpoints` and logging the result as
`bus_endpoints`. A7 said the opposite — "`bus_brokers` is logged verbatim
(broker addresses are not secrets)" — for the Kafka bootstrap list it was
written against, and Amendment 2 named that row and that sentence as
"needing a dated pointer in a later dispatch". This is that dispatch: two
pointer edits, both noted in place, following Amendment 4's convention.
Nothing else in this ADR changes; the decision (no credential in any
record) is ADR-0013 Amendment 2 ruling 2's, not re-decided here.

Every edit outside this section, with the superseded wording quoted:

* **Status line.** Appended the "amended 2026-09-21 (see "Amendment 5"
  ...)" clause.
* **A7, the `starting` row of the events table.** Was: "ingest:
  `bus_kind`, `bus_brokers`, `store_kind`, `config_path`,
  `config_version`, `bind`, [...]". Now `bus_endpoints` with a dated
  parenthesis giving the value (`bus_endpoints(HAMMERTIME_BUS_BROKERS)`,
  scheme and host[:port] only) and the field it replaces.
* **A7, the no-credential paragraph.** Its last sentence — "`bus_brokers`
  is logged verbatim (broker addresses are not secrets, decision 5 step
  3)." — is removed, and a dated blockquote after the paragraph quotes it,
  gives the replacement and extends the rule's enumeration ("or of
  `HAMMERTIME_BUS_BROKERS`").

Touched nowhere else, and why: Amendment 3's sentence "the `bus_brokers`
field carries the parallel 'meaningful only when bus_kind == kafka'" is
about the *settings* field, which keeps its name (ADR-0013 decision 10 and
Amendment 1 ruling T9; the kind is `nats` now), and is left as the history
of that decision; decision 5 step 3 itself names no field, so it is not
edited. The aggregator's own `startup_fields()` (`shard_ids`, `member_id`,
`bus_endpoints`) is not added to the row: the row records ingest's fields,
as it says, and ADR-0013 decisions 6-7 and Amendment 2 ruling 4 record the
aggregator's. Spec §47.7's matching sentence is edited in the same change
set (listed in ADR-0013 Amendment 3).

Assumptions made by this amendment (push back individually):

* **The row keeps listing only ingest's fields**, as the original did,
  rather than becoming a per-service table. ADR-0013 records the
  aggregator's; a per-service table is a later tidy-up if anyone wants it.
* **The rule's extension is phrased as a variable, not a URL grammar.** "Or
  of `HAMMERTIME_BUS_BROKERS`" binds every entry of the comma-separated
  value, whatever scheme it uses (`nats://`, `tls://`, `ws://`, `wss://`
  or scheme-less), because `bus_endpoints` treats them all alike.
* **No CHANGES entry from this ADR.** The one line for the rename ("Log
  `bus_endpoints` (bus URLs without userinfo) in the `starting` record in
  place of `bus_brokers`") is ADR-0013 Amendment 2 ruling 4's and is
  written by whichever of C2/C3 landed first.
