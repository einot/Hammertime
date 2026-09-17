# ADR 0009 — One service process lifecycle, one shared runner, four `main()`s

Status: accepted

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

### 7. Shutdown: drain, then exit 0

On the first `SIGTERM`/`SIGINT`, `run_service` logs `INFO event=stopping
signal=<name>`, calls `service.stop()` and waits for `run()` to return, under a
deadline of `HAMMERTIME_SHUTDOWN_TIMEOUT_S` (default 8). During the drain a
service:

* stops accepting new work (HTTP servers stop accepting connections; consumers
  stop fetching);
* finishes the message it is currently applying, then commits its consumer
  position (`Consumer.commit`) — never mid-message, so at-least-once redelivery
  after a crash re-applies at most one message per partition (ADR-0003);
* flushes its producer;
* trie: writes a final snapshot (§33) *after* the commit above, so the
  snapshot's recorded position is never ahead of the committed one;
* closes bus, store and socket resources.

Deadline met -> exit 0. Deadline exceeded -> `WARNING event=shutdown_timeout`
-> exit 1. A second signal during the drain aborts it immediately -> exit 1.

The 8 s default is chosen because Docker Compose's default `stop_grace_period`
is 10 s (Kubernetes' default `terminationGracePeriodSeconds` is 30 s, so the
tighter of the two governs); the drain must finish inside that or the
container is SIGKILLed mid-snapshot, which is exactly the outcome the trie's
final snapshot exists to prevent.

Amendment (2026-09-17, with ADR-0011's third pass): "aborts it immediately"
— on a second signal, or when the drain deadline is exceeded — is
implemented by **cancelling the task running `service.run()`**, the same
thing `docs/spec/integration-scenarios.md` §2's `kill_trie()` does to
simulate a crash. A `Service.run()` that is cancelled from outside MUST let
`CancelledError` propagate (never return normally as though `stop()` had
completed), MUST NOT commit its consumer position, write a snapshot, or
otherwise complete any part of the drain, and MUST leave the service in a
state where a concurrent or later `stop()` returns rather than waits
forever. Skipping the commit is always safe under ADR-0003's at-least-once
redelivery; completing it "immediately" is not always possible. The
aggregator worker's statement of this is ADR-0011 decision 4.

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
Sharded aggregators (§20, `HAMMERTIME_SHARD_IDS`) share the one group — the
shard id is what distinguishes members, and how it maps onto partitions is the
aggregator epic's design.

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
  requirement.
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

Added by the 2026-09-17 amendment to decision 7:

* **Abort is task cancellation, and a cancelled `run()` commits nothing.**
  Nothing in the spec names a mechanism for "abort immediately"; task
  cancellation is the only one asyncio offers, and `integration-scenarios.md`
  already uses it for `kill_trie()`. "Commits nothing" was chosen over
  "commits what it can" because a commit racing a second cancellation can
  leave the service half-stopped, and because an uncommitted batch costs
  only a redelivery (ADR-0003) whereas a commit ahead of an unpublished
  transition would lose one.

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
