# Hammertime

Distributed IP activity & prefix detection service.

Hammertime ingests request-count observations from distributed agents, maintains a
sliding request-count window per IP address, classifies each IP as `HOT` or `COLD`
with hysteresis, and aggregates the resulting hot-IP set into a binary IP trie so
that hot-IP *density* can be read off at every CIDR prefix level.

> The sliding-window subsystem answers "is this IP currently hot?".
> The trie answers "how densely are hot IPs distributed across this prefix hierarchy?".

See `docs/spec/` for the full architecture specification. Every module in this
repository carries a `Spec:` reference back to the section it implements.

## Pipeline

```text
agents ──▶ ingest ──▶ aggregator ──▶ trie ──▶ detector
           (auth,     (sliding      (hot_count  (classification,
            dedup,     window,       per         scoring,
            validate)  HOT/COLD)     prefix)     alerts)
```

Each stage is a separate deployable service communicating over a durable event
log. The trie is a *derived* index: it can always be rebuilt by replaying
`HotIpAdded` / `HotIpRemoved` from the log (§32).

## Layout

| Path | What lives there |
| --- | --- |
| `packages/hammertime-core` | Address/prefix math, event models, config, the authoritative HOT/COLD state machine, bucket math, telemetry |
| `packages/hammertime-bus` | Event-log abstraction (Kafka/Redpanda, in-memory for tests) |
| `packages/hammertime-store` | Sliding-window and dedup state stores (memory, Redis) |
| `packages/hammertime-testkit` | Generators, fixtures, invariant assertions |
| `services/ingest` | Agent-facing API: TLS, auth, schema validation, rate limits, dedup, publish |
| `services/aggregator` | Per-IP sliding counters, lateness policy, HOT/COLD transitions, sharding |
| `services/trie` | Single-writer binary/Patricia trie, snapshots, prefix & IP read API |
| `services/detector` | Prefix classification and scoring, minimal-prefix selection, alerts |
| `tools/` | Agent simulator, log replay, snapshot inspector, agent-token provisioning (§36.4) |
| `deploy/` | docker-compose, Kubernetes manifests, dashboards |
| `tests/` | Integration, end-to-end, and property tests spanning services |

## Quick start

```bash
make setup         # uv sync the workspace
make test          # unit + property tests
make up            # docker compose: redpanda, redis, all four services
make load          # run the agent simulator against the local stack
```

## Non-negotiables

* `hammertime.core.state.machine.evaluate_ip_state` is the **only** implementation
  of the HOT/COLD state machine. Ingestion, replay, and re-evaluation all call it (§30).
* `node.hot_count` equals the number of currently HOT `/32` descendants — always (§12).
* Readers never observe a partially updated trie path (§28).
* Agents supply observations, never verdicts. The server derives HOT state (§36).

## License

Licensed under the MIT License — see [LICENSE](LICENSE) for details.
