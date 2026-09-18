# ADR 0012 — Free and open-source components only, with a named drop-in for every single-vendor dependency

Status: accepted 2026-09-18.

Scope note: this ADR records a policy the repository owner gave verbatim
and turns it into a rule that can be checked in review: which licences
count as open source, what a drop-in alternative is, which classes of
dependency the rule reaches and how far, what a PR that changes a
dependency must state, and what happens when a component is found in
breach. It also records the inventory as it stands today and rules on the
two live breaches (the broker image and the store image). It designs no
mechanism of the system; it changes what the reference deployment runs, not
what the services do. It amends no earlier ADR; where it supersedes a
command name or an image an earlier ADR mentions in passing, that is stated
under Consequences and left for a later amendment of that ADR.

## Context

The owner's direction, quoted in full:

> "Always make sure we are using free open source tools, and in case we use
> a software from a company that has enterprise licensing or a pro version
> of their product, there is always a free open source drop-in alternative.
> Like Redpanda and Kafka."

The concern is concrete and has already happened to two of the three
third-party images `deploy/docker-compose.yml` runs today:

* **Redis relicensed.** Redis 7.2 and earlier are BSD-3-Clause. From
  7.4 (20 March 2024) Redis is offered under the Redis Source Available
  License v2 or the Server Side Public License v1, neither of which is
  OSI-approved; Redis 8 (2025) added AGPLv3 as a third option. The compose
  file runs `redis:7-alpine`, a floating tag that today resolves to
  7.4.11-alpine — the licence change reached the reference deployment
  without anyone changing a line of this repository. (Sources: the
  `COPYING`/`LICENSE.txt` files of the 7.2, 7.4 and 8.0 branches and the
  `redis` Docker Hub page; see Sources.)
* **Redpanda was never open source in the OSI sense.** The compose file's
  broker, `redpandadata/redpanda:latest` (unpinned), is licensed under the
  Business Source License 1.1 with a four-year change date to Apache-2.0
  and an additional-use grant that forbids offering it as a streaming
  service. It is free to run and its source is public, but BSL 1.1 is not
  OSI-approved. It is acceptable in this repository only because Apache
  Kafka (Apache-2.0, Apache Software Foundation) speaks the same wire
  protocol — the exact relationship the owner named.
* **A free image catalogue ended.** In 2025 Bitnami (Broadcom) withdrew
  its free public container catalogue behind a paid "Secure Images" tier,
  moved the old version tags to a `bitnamilegacy` repository with "no
  further updates or support", and kept only `latest` tags of a reduced
  set free. Hammertime uses no Bitnami image, but the pattern — a
  third-party repackaging whose free tier ends — is the image-level form
  of the same risk.

The rest of the inventory is in good shape: every Python dependency is
under an OSI-approved permissive or weak-copyleft licence (verified per
package below), the build and check tooling is MIT/Apache, and the only
proprietary pieces are hosted services (GitHub Actions, CodeQL) that the
repository does not depend on for correctness.

## Decision

### 1. Definitions

* **Open source** means licensed under a licence the Open Source Initiative
  has approved. The identifiers this ADR accepts without further lookup
  are `Apache-2.0`, `MIT`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `0BSD`,
  `MPL-2.0`, `PSF-2.0`, `EPL-2.0`, `LGPL-2.1`/`LGPL-3.0`,
  `GPL-2.0`/`GPL-3.0`, `AGPL-3.0` and `Unlicense` (with or without
  `-only`/`-or-later`). Any other identifier is checked against the SPDX
  licence list's `isOsiApproved` flag before it is accepted, and the check
  is cited. A dual- or multi-licensed component counts as open source iff
  at least one option is OSI-approved and Hammertime uses it under that
  option (Redis 8 under AGPLv3 would qualify; Redis 7.4 does not).
* **Source-available is not open source.** Business Source License
  (`BUSL-1.1`), Server Side Public License (`SSPL-1.0`), Redis Source
  Available License (RSALv1/v2), Elastic License (`Elastic-2.0`), the
  Redpanda Community License, "fair source", Commons Clause, PolyForm and
  any licence that restricts the field of use or who may offer the software
  as a service do not count, however free of charge the software is.
* **Component**: anything the system runs, imports, builds with, or is
  checked by — a broker, a store, a dashboard, a Python package, a build
  backend, a linter, a container image, a CI action or a hosted service.
* **Single-vendor with a paid tier**: a component whose licence and
  trademark are controlled by one company (not a foundation such as the
  ASF, CNCF, Linux Foundation or PSF) and which that company also sells as
  an enterprise, pro, or cloud edition of the same product. Redpanda Data,
  Redis Ltd and Grafana Labs qualify. Apache Kafka does not, even though
  companies sell hosted Kafka, because no single company can relicense it;
  the same holds for Valkey and Prometheus.
* **Drop-in alternative**: a replacement that speaks the same wire
  protocol or API such that no Hammertime source file changes — only
  deployment configuration (image, command, address, credentials). The
  test is mechanical: the swap is a diff confined to `deploy/`,
  `.env*`, or an orchestrator manifest. Apache Kafka for Redpanda is the
  model: `packages/hammertime-bus/src/hammertime/bus/kafka.py` uses
  aiokafka with consumer groups and a `ConsumerRebalanceListener`, manual
  `assign()`, explicit offset commits with auto-commit disabled, and the
  client's default key partitioner — all of them core Kafka protocol
  features, none of them broker-specific.
* **Reference deployment**: `deploy/docker-compose.yml` as shipped, which
  is what `make up` starts and what the broker-backed integration suite
  (#52, pending) will run against. The images named there are the
  reference components; anything else is a documented substitute.
* **Pinned**: an image reference whose tag names at least a major and a
  minor version (`valkey/valkey:9.1-alpine` is pinned; `redis:7-alpine`
  and `:latest` are not). A full `major.minor.patch` tag is recommended
  and a digest may be added, but `major.minor` is the floor, because
  licence changes have so far landed on minor boundaries (Redis 7.2 to
  7.4).

### 2. Class 1 — runtime infrastructure the services depend on at run time

Brokers, stores, metrics collectors, dashboards: everything a container in
the reference deployment runs that is not built from this repository.

1. MUST be open source (definition 1), with the licence recorded per
   version in the inventory of this ADR.
2. If the component is single-vendor with a paid tier, a free open-source
   drop-in MUST be named in the inventory, and the drop-in MUST be
   exercised: the broker-backed integration suite runs against it at least
   once per release, or it is the reference component itself. A drop-in
   that has never been run is a claim, not a plan.
3. The reference deployment MUST use the open-source option whenever one
   exists. A source-available product (Redpanda) may be documented as a
   substitute — with a pinned image and a deployment-only diff that
   demonstrates the drop-in relation — but never as the reference.
4. Exception for components nothing depends on for correctness: a
   single-vendor, OSI-licensed component with no true drop-in may be used
   where its loss would cost only an operator convenience (a dashboard
   definition), never data, ordering or a service's ability to start. Every
   such exception is listed by name in the inventory with the reason. Today
   the list is Grafana OSS (decision 8).

Strong-copyleft licences (GPL, AGPL) are acceptable in this class. The
services connect to these components over a network protocol; they do not
link, embed or modify them, so AGPLv3's network clause obliges Hammertime
to nothing beyond what running an unmodified upstream binary already
satisfies. See Assumptions.

### 3. Class 2 — Python dependencies

Every entry of `dependencies` in the `pyproject.toml` files under
`packages/`, `services/` and `tools/`, the `dev` dependency group in the
root `pyproject.toml`, and the build backend.

1. MUST be open source (definition 1).
2. MUST be permissive or weak copyleft (MIT, BSD, Apache, ISC, MPL-2.0,
   LGPL, PSF, EPL). A GPL or AGPL library is NOT acceptable in this class:
   Hammertime is MIT-licensed and imports its dependencies into the same
   process, and this ADR does not take a position on whether that makes a
   combined work — it simply keeps the question from arising. Admitting
   one requires an amendment of this ADR.
3. The drop-in requirement does NOT apply. A library swap is a code change
   by nature, so "drop-in" has no meaning here; the licence requirement is
   absolute instead. A dependency maintained by a single vendor with a paid
   tier (`redis` by Redis Inc.; `ruff` and `uv` by Astral; `pydantic` by
   Pydantic Services Inc.) is fine as long as its licence is.
4. Direct dependencies are checked per PR (decision 6). Transitive
   dependencies are covered by the same licence rule but are checked at
   release time, not per PR: the 1.0 release and every minor release after
   it audits the resolved lockfile once (see Assumptions for the tooling).

### 4. Class 3 — development and CI tooling

1. Every tool the merge bar runs locally — `uv`, `ruff`, `mypy`, `pytest`,
   `hypothesis`, `hatchling` and anything added to the four commands in
   `CLAUDE.md` (`uv run pytest -q`, `ruff check`, `ruff format --check`,
   `mypy`) or to the `Makefile` — MUST be open source. Same permissive/weak
   copyleft restriction as class 2 for anything imported into the test
   process; a standalone tool that only runs as a subprocess may be any
   OSI licence.
2. Proprietary **hosted** services are tolerated — GitHub itself, GitHub
   Actions with GitHub-hosted runners, GitHub's default-setup CodeQL,
   Docker Hub as an image registry — on one condition: nothing in the
   repository depends on them for correctness. Concretely, every check that
   gates a merge MUST be runnable locally with class-3 tooling, and CI's
   role is to re-run those same commands, never to run a check that has no
   local equivalent. `.github/workflows/ci.yml` satisfies this today: its
   `check` job runs `uv sync`, `uv lock --check`, `ruff check`,
   `ruff format --check`, `mypy` and `pytest -q`. CodeQL is advisory; it is
   not part of the merge bar and no rule may make it one.
3. Third-party actions the workflow uses (`actions/checkout`,
   `astral-sh/setup-uv`) are class-3 components: MUST be open source (both
   are MIT), pinned to at least a major version tag as they are today, and
   listed in the inventory.
4. Should any hosted service end its free tier, the fallback is running
   the four commands on any Linux host; no migration is needed and none is
   planned. That is the point of condition 2.

### 5. Class 4 — container images

1. Provenance: prefer, in order, (a) an image published by the project
   itself or its foundation (`apache/kafka`, `valkey/valkey`,
   `prom/prometheus`, the `python` Docker Official Image), (b) a Docker
   Official Image, (c) anything else — and (c) requires a reason in the
   PR. Third-party repackagings of a project by a company that is not the
   project (Bitnami-style) are (c).
2. Every image reference in `deploy/`, in every `Dockerfile` `FROM` line,
   and in any future orchestrator manifest MUST be pinned (definition 1).
   `:latest`, a bare major tag, and a missing tag are each a review
   finding. Rationale: a licence change applies to versions released after
   it, so an unpinned tag is the path by which a licence change reaches a
   running deployment silently — which is exactly how `redis:7-alpine`
   became RSALv2 without a diff.
3. Bumping a pinned image is a dependency change under decision 6: the PR
   states the licence of the *new* version, because that is when a licence
   change becomes visible.

### 6. Process: every dependency change states its licence; `reviewer` checks it

A PR that adds, removes, bumps or swaps any component in classes 1–4 MUST
carry a `Licences` section in its description with one line per component:

```text
<name> <version> — <SPDX id> — <URL of the licence file or PyPI metadata consulted> — single-vendor with paid tier: yes|no — drop-in: <name or "n/a">
```

`reviewer` checks, and reports as a finding (`category: licence-policy`)
when any of these fails: the section is present and covers every component
the diff touches; each licence is OSI-approved and, for class 2/3
in-process libraries, permissive or weak copyleft; each class-1 single-vendor
component names its drop-in; every image reference in the diff is pinned;
and the inventory in this ADR is updated in the same change set (by an
architect amendment, since `docs/adr/` is architect-owned; see
Assumptions).

### 7. Remediation: what happens when a component is found non-compliant

1. **Pre-1.0** (now): the component is replaced or pinned to a compliant
   version before 1.0 ships. An issue in the 1.0 milestone tracks it; the
   inventory row reads `non-compliant` until the fix merges. The two
   current cases are the broker image and the store image (decisions 9
   and 10) and their fix is briefed under Follow-through.
2. **Post-1.0**: an issue is opened when the breach is found, the
   inventory row is marked in the same week, and the swap lands in the next
   minor release with a `CHANGES` entry. The entry is `BREAKING` if an
   operator must act to keep a running deployment working — a store swap
   whose persisted data does not load in the replacement, a broker swap
   that changes the advertised address, or a renamed configuration key —
   and the runbook gains the migration step in the same PR.
3. A licence change discovered on a version bump is handled by not
   bumping: the last compliant version stays pinned while the replacement
   is chosen. Staying pinned is a stopgap, not a resolution; the row still
   reads `non-compliant` and rule 1 or 2 applies.

### 8. Copyleft infrastructure and Grafana

AGPLv3 infrastructure is acceptable under class 1 (decision 2) when run
unmodified over a network protocol. Grafana OSS (AGPL-3.0-only; Grafana
Labs sells Enterprise and Cloud) may therefore be used for the §37
dashboards. It has no true drop-in: Perses (Apache-2.0, CNCF sandbox) reads
the same Prometheus data but does not load Grafana's dashboard JSON, so a
move would mean rewriting the dashboard, not a deployment change. Grafana
is accepted under decision 2's exception 4: it is a display of metrics that
Prometheus already holds, no service reads from it, and losing it costs
one dashboard file. Perses is recorded as the nearest alternative, not as
a drop-in. Note that `deploy/grafana/README.md` describes a
`hammertime.json` that does not exist yet and no Grafana service is in the
compose file; the ruling applies when it is added.

### 9. Reference broker: Apache Kafka; Redpanda a documented substitute

The reference broker is **Apache Kafka** (Apache-2.0, ASF), run from the
foundation's own image `apache/kafka`, pinned to a 4.x release, in KRaft
combined mode (one container acting as broker and controller; Kafka 4.0
removed ZooKeeper). The compose service is named `broker` and the
application services reach it as `broker:9092`, so `HAMMERTIME_BUS_BROKERS`
is the same under every broker.

Redpanda is kept as a **documented substitute**, not removed: an override
file `deploy/docker-compose.redpanda.yml` redefines only the `broker`
service (image `redpandadata/redpanda`, pinned; its `redpanda start`
command; its own healthcheck). Starting the stack with both files is the
drop-in demonstration — the diff between the two brokers is confined to
that file, and nothing under `packages/`, `services/`, `tools/` or `.env*`
differs. The broker-backed integration suite (#52) runs against the
reference (Kafka). Running it against the override is not required by
this ADR; if a later change makes Redpanda-specific behaviour matter
(ADR-0001 Amendment 1 notes that its per-partition ordering is asserted of
Redpanda only via protocol compatibility), that is when the override earns
a CI run.

Consequences for earlier text: ADR-0001 Amendment 1's Redpanda bullet
("the reference deployment's broker is Redpanda, and the citation above is
Kafka's") is resolved in Kafka's favour once the compose change lands — the
citation and the deployment then agree. ADR-0009 decision 10 names `rpk
cluster health` as the broker healthcheck; under Kafka that becomes the
Kafka CLI's own probe (e.g. `kafka-broker-api-versions.sh
--bootstrap-server localhost:9092`). Neither ADR is edited here.

### 10. Reference store: Valkey; Redis 8 (AGPLv3) and Redis 7.2 as alternatives

The reference store is **Valkey** (BSD-3-Clause, a Linux Foundation
project, forked from Redis immediately before the 7.4 relicensing), run
from the project's own image `valkey/valkey`, pinned. The service is named
`valkey` and the URL becomes `redis://valkey:6379/0`. It is started with
`--maxmemory-policy noeviction`, which ADR-0011 and
`packages/hammertime-store/src/hammertime/store/redis.py` require of the
`hammertime:agg:*` and `hammertime:dedup:*` keyspaces (that is Redis's
default policy too, but the reference deployment states it rather than
relying on a default).

Why Valkey and not the other two compliant options:

* **Redis 8 under AGPLv3** is open source by definition 1 and would be
  acceptable. It is not chosen as the reference because the AGPL option
  is a per-release choice of a single vendor that changed its licence once
  already; under decision 2 it would need Valkey named and exercised as
  its drop-in anyway, so making Valkey the reference removes one moving
  part. Redis 8.x (AGPLv3 option) is recorded as the drop-in for Valkey in
  the other direction, and the same protocol keeps `redis-py` unchanged.
* **Redis 7.2 pinned** is BSD-3-Clause but frozen: it receives no
  features and its maintenance window is the vendor's to close. A store
  that can only ever move to a non-compliant version is a breach deferred,
  not avoided.

What does not change: the environment keys `HAMMERTIME_STORE_KIND=redis`
and `HAMMERTIME_REDIS_URL`, the `redis://` scheme, the `redis` Python
package and `fakeredis` in tests. "Redis" in those names is the protocol
and client, which Valkey implements; renaming them would be a `BREAKING`
configuration change for nothing. The Hammertime store code uses `SET NX
EX`, `EXPIRE ... GT` (Redis 7.0+), `EXISTS`, `SADD`/`SREM`/`SMEMBERS`,
`GET`/`SET`, `WATCH`/`MULTI`/`EXEC` — all present in the 7.2 line Valkey
forked from.

### 11. Prometheus and the Python base image

`prom/prometheus` (Apache-2.0, CNCF; the project's own Docker Hub
organisation) is compliant and needs only a pin to a `v3.x` release.
`python:3.12-slim` (Docker Official Image; CPython under the PSF licence)
is compliant and already pinned to `major.minor`.

## Inventory (as of 2026-09-18)

Status values: `compliant`; `compliant-with-drop-in` (single-vendor, OSI
licence, drop-in named); `exception` (decision 2 item 4);
`non-compliant`; `needs verification`. "Verified" means the licence was
read from the source named under Sources for the version shown; anything
else is marked as an assumption.

### Class 1 — runtime infrastructure (`deploy/docker-compose.yml`)

| Component | Image as deployed | Licence | Single-vendor, paid tier | Drop-in | Status | Pin | Verified |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Redpanda | `redpandadata/redpanda:latest` (line 5; `latest` = v26.2.3 today) | BUSL-1.1, change to Apache-2.0 four years after each release; enterprise features separately licensed | yes (Redpanda Data: Enterprise, Cloud) | Apache Kafka | **non-compliant** (not OSI; unpinned) — becomes the documented substitute under decision 9 | none | yes (`licenses/bsl.md`) |
| Apache Kafka (reference after decision 9) | `apache/kafka:<4.x>` (to be added) | Apache-2.0 | no (ASF) | n/a; Redpanda is the substitute | compliant once pinned and landed | to be pinned | yes (`LICENSE`; Docker Hub publisher = The Apache Software Foundation) |
| Redis | `redis:7-alpine` (line 15; resolves to 7.4.11-alpine today) | RSALv2 OR SSPLv1 (7.4.x–7.8.x); BSD-3-Clause up to 7.2.x; RSALv2 OR SSPLv1 OR AGPLv3 from 8.0 | yes (Redis Ltd: Enterprise, Cloud) | Valkey | **non-compliant** (7.4 is not OSI; floating tag) | major only | yes (branch `LICENSE.txt`/`COPYING`; Docker Hub tag map) |
| Valkey (reference after decision 10) | `valkey/valkey:<9.x-alpine>` (to be added) | BSD-3-Clause | no (Linux Foundation) | Redis 8.x under AGPLv3; Redis 7.2 | compliant once landed | to be pinned | yes (`COPYING`; Docker Hub publisher = Valkey community) |
| Prometheus | `prom/prometheus:latest` (line 53) | Apache-2.0 | no (CNCF) | n/a | compliant once pinned (**pin missing**) | none | yes (`LICENSE`) |
| Grafana OSS | not deployed (`deploy/grafana/README.md` only) | AGPL-3.0-only | yes (Grafana Labs: Enterprise, Cloud) | none true; Perses (Apache-2.0, CNCF sandbox) nearest | exception (decision 8) | n/a | yes (`LICENSE`); Enterprise/Cloud tiers from recall |
| CPython base image | `python:3.12-slim` (four `Dockerfile`s) | PSF-2.0 (+ Debian) | no (PSF) | n/a | compliant | `major.minor` | assumption: licence not fetched |

### Class 2 — Python dependencies (versions from `uv.lock`)

| Package | Version | Licence (PyPI `license_expression` / `license`) | Maintainer with paid tier | Status |
| --- | --- | --- | --- | --- |
| aiokafka | 0.14.0 | Apache-2.0 | no | compliant |
| fastapi | 0.141.1 | MIT | no | compliant |
| uvicorn | 0.53.0 | BSD-3-Clause | no | compliant |
| jsonschema | 4.26.0 | MIT | no | compliant |
| pydantic | 2.13.5 | MIT (from the repository `LICENSE`; PyPI JSON too large for the fetch tool to extract) | yes (Pydantic Services Inc.: Logfire) — irrelevant under decision 3 | compliant |
| prometheus-client | 0.26.0 | Apache-2.0 AND BSD-2-Clause | no | compliant |
| structlog | 26.1.0 | MIT OR Apache-2.0 | no | compliant |
| redis (redis-py) | 8.1.0 | MIT (author "Redis Inc.") | yes (Redis Ltd) — irrelevant under decision 3 | compliant |
| httpx | 0.28.1 | BSD-3-Clause | no | compliant |
| hypothesis | 6.168.0 | MPL-2.0 (weak copyleft, file-level) | no | compliant |
| pytest | 9.1.1 | MIT | no | compliant |
| pytest-asyncio | 1.4.0 | Apache-2.0 | no | compliant |
| pytest-benchmark | 5.3.0 | BSD-2-Clause | no | compliant |
| fakeredis | 2.38.0 | BSD-3-Clause | no | compliant |
| types-jsonschema | 4.26.0.20260518 | Apache-2.0 | no | compliant |
| ruff | 0.16.7 (lock; 0.16.8 on PyPI) | MIT | yes (Astral) — irrelevant under decision 3 | compliant |
| mypy | 2.3.1 | MIT | no | compliant |
| hatchling (build backend) | 1.32.3 on PyPI | MIT | no | compliant |
| uv (package manager) | 0.12.16 on PyPI | MIT OR Apache-2.0 | yes (Astral) — class 3; `pip` + `venv` are the non-drop-in fallback (a lockfile regeneration, no code) | compliant |

Transitive dependencies (e.g. starlette, pydantic-core, anyio, attrs) are
not itemised here; decision 3 item 4 audits them at release time.

### Class 3 — CI

| Component | Reference | Licence / nature | Status |
| --- | --- | --- | --- |
| GitHub Actions, GitHub-hosted runners | `.github/workflows/ci.yml` | proprietary hosted service, free for public repositories | tolerated (decision 4 item 2): every gating check has a local equivalent |
| CodeQL default setup | no workflow file | proprietary hosted service, free for public repositories | tolerated; advisory only, never a merge gate |
| `actions/checkout` | `@v4` | MIT | compliant |
| `astral-sh/setup-uv` | `@v3` | MIT | compliant |
| Docker Hub | image pulls | proprietary registry | tolerated; every image used is also buildable from its project's source |

### Class 4 — image pinning summary

| Reference | Pinned? | Action |
| --- | --- | --- |
| `redpandadata/redpanda:latest` | no | replaced as reference (decision 9); the substitute override pins it |
| `redis:7-alpine` | major only | replaced (decision 10) |
| `prom/prometheus:latest` | no | pin to a `v3.x` release |
| `python:3.12-slim` × 4 | `major.minor` | none |

## Assumptions

Each of these is a judgment call the owner's direction, the spec and the
earlier ADRs do not make. Push back on them individually.

1. **"Open source" = OSI-approved, checked via SPDX.** The owner said "free
   open source"; OSI approval is the only widely accepted operational
   definition. `opensource.org` and `spdx.org` are blocked from this
   environment, so the approval flags were read from SPDX's own
   licence-list XML source on GitHub (`isOsiApproved` for `AGPL-3.0-only`
   and `MPL-2.0` = true; `BUSL-1.1` and `SSPL-1.0` = false). That is
   SPDX's mirror of OSI's list, i.e. secondhand for OSI itself. RSALv2 and
   `Elastic-2.0` were not looked up; that they are not OSI-approved is
   stated from recall.
2. **Copyleft is fine for run-unmodified infrastructure, not for imported
   libraries.** Decision 2 admits AGPL infrastructure; decision 3 excludes
   GPL/AGPL libraries. The line is drawn at "runs in Hammertime's process"
   because that is where a combined-work question could arise and the
   repository's MIT licence should not have to answer it. LGPL libraries
   are admitted on the common reading that a Python import is not static
   linking; nothing in the inventory is LGPL today, so the reading is
   untested.
3. **Foundation-governed projects are not "single-vendor" even when
   vendors sell them.** Otherwise Kafka (Confluent), Valkey (AWS, Google)
   and Prometheus (Grafana Labs) would all need drop-ins and the rule would
   have no fixed point. The criterion chosen is "can one company relicense
   it": a foundation project cannot be relicensed by a vendor.
4. **The drop-in is proven by the reference deployment being the
   open-source option, not by running both in CI.** Running the Redpanda
   override in CI would double broker-backed integration time to prove a
   substitute nobody is required to use. If Redpanda-specific behaviour
   ever matters, decision 9 says that is when the override earns a run.
5. **Redpanda is kept as an override rather than deleted.** The owner named
   it; a developer who wants its faster cold start can have it with one
   extra `-f`. Cost: a second file to keep pinned. Push back if one broker
   is simpler.
6. **`major.minor` is the pinning floor; exact patch recommended.** An
   exact patch pin is safest but turns every security release into a PR.
   `major.minor` held the line in the only licence change that has
   actually happened here (Redis 7.2 to 7.4); a vendor relicensing on a
   patch boundary would defeat it, and nothing prevents a PR from pinning
   tighter.
7. **Valkey over Redis 8 AGPL.** Both are compliant; the tie-break is
   governance (foundation vs single vendor that has relicensed once) and
   removing the need for an exercised drop-in. Valkey's fork point is
   stated by its README as "right before the transition to their new
   source available licenses"; that this was 7.2.4 is from recall
   (`valkey.io` is blocked). That Valkey's configuration directives
   (`maxmemory-policy`), `EXPIRE ... GT` and RDB compatibility with Redis
   7.2 are inherited unchanged is assumed from the fork point, not read
   from Valkey's documentation. That a Redis 7.4 RDB file would *not* load
   in Valkey — the reason the store swap is cheap now and would be
   `BREAKING` after 1.0 — is from recall and should be checked when the
   runbook gains a migration note.
8. **Existing `redis` names in configuration stay.** `HAMMERTIME_REDIS_URL`,
   `HAMMERTIME_STORE_KIND=redis`, `redis://` and the `redis` package are
   protocol/client names; renaming them would be `BREAKING` for no
   compliance gain. A `valkey`-branded client (valkey-py / GLIDE) exists
   but is not required and is not adopted.
9. **Proprietary hosted CI is tolerated on the "runnable locally"
   condition.** The owner asked for open-source tools; GitHub Actions and
   CodeQL are neither tools we run nor components the system depends on.
   The alternative — self-hosted open-source CI — is real work for a
   public repository whose merge bar is four local commands. Whether any
   branch-protection rule currently requires CodeQL could not be read
   from this environment; decision 4 item 2 forbids it either way.
10. **Transitive dependencies are audited per release, not per PR.** A
    per-PR transitive audit needs tooling in the merge bar (e.g.
    `pip-licenses` or `uv`'s own metadata queries — not chosen here); a
    per-release audit needs a person and a lockfile. The lighter rule is
    chosen pre-1.0; push back if a gating tool is preferred.
11. **The living inventory is this ADR, maintained by architect
    amendment.** A dependency PR by `coder` cannot edit `docs/adr/`, so
    decision 6 implies an architect amendment per dependency change. A
    separate `docs/dependency-inventory.md` that `coder` may edit would be
    lighter; it is not created here because it is outside this task's
    scope, and it would need the same ownership decision. Push back if the
    amendment overhead proves too high.
12. **Remediation timescales.** "Before 1.0" and "next minor release" are
    chosen, not required; the owner set no deadline.
13. **Grafana's exception.** The owner's rule, read strictly, has no
    exception category. Decision 2 item 4 creates one for display-only,
    OSI-licensed, single-vendor components with no drop-in because the
    alternative is to forbid the §37 dashboards or to require a rewrite
    to Perses now, for a component that is not even deployed yet.
14. **Kafka's cold start fits ADR-0009's 60 s startup deadline.** ADR-0009
    chose 60 s "to cover Redpanda's cold start in CI comfortably"; Kafka in
    KRaft combined mode is assumed to start within that on a CI runner. If
    #52 finds otherwise, the deadline is ADR-0009's to amend, not this
    ADR's.
15. **Kafka versions.** "A 4.x release" is required rather than a specific
    patch because Docker Hub's `apache/kafka` page lists only `latest` by
    name; ADR-0001 Amendment 1 cites 4.3.1's documentation, so 4.3.1 is
    the expected pin. The coder brief asks for the tag to be confirmed on
    the registry, not assumed.
16. **`CHANGES` entry for the compose swap.** Changing the reference
    broker and store images is a changed default and gets one non-`BREAKING`
    line: pre-1.0 there is no supported running deployment to migrate.
    This ADR itself gets no entry (an ADR is not a user-visible change).

## Consequences

* **Apache Kafka is heavier than Redpanda.** A JVM broker image several
  times Redpanda's size and a slower cold start, paid on every `make up`
  and every broker-backed CI run. Accepted: it is the cost of the reference
  being the open-source component; the Redpanda override exists for local
  loops that care.
* **Valkey is younger than Redis.** Forked in 2024; its own features
  (9.x) are not yet what Hammertime uses, and Hammertime uses nothing
  newer than the 7.2 feature set. `redis-py` is maintained by Redis Inc.
  and could grow Redis-only behaviours; `fakeredis` in unit tests and the
  broker/store-backed integration suite (#52) against Valkey are what
  would catch that.
* **A policy needs upkeep.** Every dependency PR carries a `Licences`
  section and an inventory update; every release audits the lockfile.
  That is a recurring cost with no automation today.
* **Two compose files.** `deploy/docker-compose.yml` (reference) and
  `deploy/docker-compose.redpanda.yml` (substitute override) must both
  stay pinned and both keep working; only the first is exercised by CI.
* **Earlier ADRs mention the old images.** ADR-0001 Amendment 1's
  Redpanda bullet (addressed by a one-sentence pointer added with this
  ADR) and ADR-0009 decision 10's `rpk cluster health` / `redis-cli ping`
  healthcheck names (superseded by the Kafka CLI and `valkey-cli`; to be
  amended when #17 briefs the healthchecks). `README.md` lines describing
  `make up` as starting "redpanda, redis" change with the compose PR.
* **A pre-existing gap this ADR does not close.** Nothing in the
  repository creates topics: `packages/hammertime-bus/src/hammertime/bus/topics.py`
  declares 128 partitions for `hammertime.observations.v1`, but there is
  no provisioning call, so the compose stack relies on the broker
  auto-creating topics — with Kafka's default of one partition, exactly as
  with Redpanda today. The swap keeps behaviour parity (auto-create left
  on); provisioning the declared partition counts is #17's, and the coder
  brief says so rather than solving it in passing.
* **Spec pointer.** §43 (Recommended Initial Implementation) gains a
  pointer note naming this ADR and the reference components;
  `docs/spec/README.md` records it.

## Follow-through (briefs for the top-level session to dispatch)

Nothing below has been dispatched; the architect has no `Agent` tool. Order:
the coder brief first; the reviewer brief on its diff; the test-author
constraint is for #52's brief, not a dispatch of its own.

### Brief C1 — `coder`: make the reference deployment comply with ADR-0012

Files you may touch: `deploy/docker-compose.yml`,
`deploy/docker-compose.redpanda.yml` (new), `deploy/prometheus.yml` (only
if a target name changes — it should not), `README.md` (lines 34 and 50
only), `CHANGES`. Nothing under `packages/`, `services/`, `tools/`,
`tests/`, `docs/`, `schemas/` or `.github/` changes: ADR-0012 defines a
drop-in as a swap with no code change, and this PR is the proof.

Work from: ADR-0012 decisions 5, 9, 10, 11 and the inventory;
ADR-0011 Consequences (*Store package*, the `noeviction` requirement);
ADR-0009 decision 10 for context only (its healthchecks are #17's, do not
add them here).

Do:

1. In `deploy/docker-compose.yml` replace the `redpanda` service with a
   service named `broker` running `apache/kafka:<tag>` where `<tag>` is a
   4.x release tag you confirm exists on Docker Hub (`4.3.1` expected).
   Configure single-node KRaft combined mode via the image's `KAFKA_*`
   environment variables (node id, `process.roles=broker,controller`,
   controller quorum voters pointing at itself, listener map) so that the
   broker advertises `broker:9092` on the compose network and
   `localhost:19092` on the host, matching the two addresses the file
   advertises today; keep port `19092` published. Leave topic auto-creation
   at its default (on): partition provisioning is #17's and is unchanged
   by this swap. Offsets/transaction-state replication factors must be 1
   for a single node.
2. Change every `HAMMERTIME_BUS_BROKERS: redpanda:9092` to `broker:9092`
   and every `depends_on` entry `redpanda` to `broker`.
3. Replace the `redis` service with a service named `valkey` running
   `valkey/valkey:<tag>-alpine` where `<tag>` is the newest stable
   (non-rc) release on Docker Hub (`9.1.2` at the time of writing),
   with `command: ["valkey-server", "--maxmemory-policy", "noeviction"]`,
   port `6379` published as today. Change `HAMMERTIME_REDIS_URL` in both
   services to `redis://valkey:6379/0` and `depends_on` `redis` to
   `valkey`. Do not rename any `HAMMERTIME_*` key.
4. Pin `prom/prometheus` to the newest `v3.x.y` release tag on Docker Hub.
5. Create `deploy/docker-compose.redpanda.yml` containing only a `broker`
   service override: `image: redpandadata/redpanda:v26.2.3` (or the newest
   `vXX.Y.Z` tag you confirm), the existing `redpanda start ...` command
   with `--advertise-kafka-addr internal://broker:9092,external://localhost:19092`,
   and the same `19092` port. A one-line comment at the top says it is the
   ADR-0012 decision 9 substitute, used as
   `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.redpanda.yml up`.
6. Verify with Bash: `docker compose -f deploy/docker-compose.yml config`
   and the two-file variant both validate; if Docker is available, `up -d`
   each variant and confirm `broker` and `valkey` reach a running state
   (the application services may still crash-loop for reasons #26
   records; that is not this PR's concern). Report what you could and
   could not run.
7. `README.md`: line 50's comment becomes "docker compose: kafka, valkey,
   prometheus, all four services"; line 34 may stay ("Kafka/Redpanda") as
   both remain true.
8. `CHANGES`, newest-first, one line, not `BREAKING`:
   `Reference deployment runs Apache Kafka (KRaft) and Valkey instead of Redpanda and Redis; every image is pinned; Redpanda remains available via deploy/docker-compose.redpanda.yml`.
9. PR description carries the `Licences` section of ADR-0012 decision 6
   with a line each for `apache/kafka`, `valkey/valkey`,
   `prom/prometheus`, `redpandadata/redpanda`.

Done when: `docker compose config` validates for both file sets; no file
outside the list above changed; the four checks in `CLAUDE.md` still pass
(they do not touch `deploy/`, so this is a no-op confirmation); the PR
description has the `Licences` section.

Do not: add healthchecks or `--wait` (ADR-0009 decision 10 / #17); create
topics; touch `.env.example` (its `localhost:19092` and
`redis://localhost:6379/0` are still correct); rename `HAMMERTIME_*`
keys; touch `.github/workflows/ci.yml`.

### Brief R1 — `reviewer`: review C1's diff against ADR-0012

Examine the diff of `deploy/docker-compose.yml`,
`deploy/docker-compose.redpanda.yml`, `README.md` and `CHANGES` from C1,
and the PR description. Judge against ADR-0012 decisions 5 (every image
reference pinned to at least `major.minor`; provenance is the project's
own image), 6 (the `Licences` section covers all four images with SPDX
ids and URLs), 9 (service named `broker`; `broker:9092` everywhere; the
override file redefines only `broker`; nothing outside `deploy/`,
`README.md`, `CHANGES` changed), 10 (`valkey` service, `noeviction`
stated, `HAMMERTIME_REDIS_URL` points at `valkey`, no `HAMMERTIME_*` key
renamed), and ADR-0011's `noeviction` requirement. Report any `latest`,
bare-major or missing tag as `severity: high`, `category: licence-policy`.
Also report if `deploy/prometheus.yml` targets no longer match compose
service names (they should be unchanged).

### Constraint for the #52 test-author brief (no dispatch now)

When the broker-backed integration suite is briefed, it MUST run against
the reference `deploy/docker-compose.yml` (Apache Kafka, Valkey) —
that run is what turns the drop-in claims of ADR-0012 decisions 9 and 10
(the same aiokafka and redis-py code paths against the open-source
broker and store) from assumed into exercised, which is what decision 2
item 2 demands of any drop-in. Running against the Redpanda override is
optional and, if done, is a second job, never a replacement.

### For the top-level session (not a subagent brief)

Decision 6's `Licences` section is easiest to enforce with a
`.github/PULL_REQUEST_TEMPLATE.md` carrying the heading and the one-line
format; that file is governance, outside the architect's write scope, and
is left to the session to add if wanted.

## Sources

Read on 2026-09-18. `kafka.apache.org`, `docs.confluent.io`, `redis.io`,
`valkey.io`, `opensource.org` and `spdx.org` are blocked by this
environment's egress proxy; where a primary site was blocked the
project's own licence file on GitHub was read instead, and that is noted.

* Redpanda licence: `https://raw.githubusercontent.com/redpanda-data/redpanda/dev/licenses/bsl.md`
  — "BSL 1.1", Licensor "Redpanda Data, Inc.", "Change date is four years
  from release date", Change License "Apache License, Version 2.0", and
  the Additional Use Grant forbidding use "for a Streaming or Queuing
  Service". Which directories fall under BSL versus the Redpanda Community
  License was not found in the README fetched and is stated from recall.
  Tags: `https://hub.docker.com/r/redpandadata/redpanda/tags` — `latest`
  = `v26.2.3`.
* Redis licences: `https://raw.githubusercontent.com/redis/redis/7.2/COPYING`
  (BSD 3-Clause, "Copyright (c) 2006-2020, Salvatore Sanfilippo");
  `https://raw.githubusercontent.com/redis/redis/7.4/LICENSE.txt`
  ("Starting on March 20th, 2024, Redis follows a dual-licensing model
  with all Redis project code contributions under version 7.4 and
  subsequent releases ... RSALv2 ... or the Server Side Public License");
  `https://raw.githubusercontent.com/redis/redis/8.0/LICENSE.txt`
  ("Starting with Redis 8, Redis Open Source is moving to a tri-licensing
  model ... (a) RSALv2; or (b) SSPLv1; or (c) AGPLv3 ... Redis Open
  Source 7.2 and prior releases remain subject to the BSDv3 clause
  license"). Tag map: `https://hub.docker.com/_/redis` — `7-alpine` ->
  `7.4.11-alpine`, `7.2-alpine` -> `7.2.16-alpine`, `8-alpine` ->
  `8.10.1-alpine`, `latest` -> `8.10.1`; the page's own licence note:
  "Redis (<=7.2.4) are licensed under 3-Clause BSD, and Redis 7.4.x-7.8.x
  are licensed under the dual RSALv2 or SSPLv1 license".
* Valkey: `https://raw.githubusercontent.com/valkey-io/valkey/unstable/COPYING`
  (BSD 3-Clause; "Copyright (c) 2024-present, Valkey contributors /
  Copyright (c) 2006-2020, Redis Ltd."); `.../unstable/README.md` ("This
  project was forked from the open source Redis project right before the
  transition to their new source available licenses"; "Valkey a Series of
  LF Projects, LLC"); `https://hub.docker.com/r/valkey/valkey`
  ("Maintained by: the Valkey Community"; tags 9.1.2, 9.1, 9, `-alpine`
  variants, 8.1.x, 8.0.x, 7.2.x).
* Apache Kafka: `https://raw.githubusercontent.com/apache/kafka/trunk/LICENSE`
  (Apache License Version 2.0); `https://hub.docker.com/r/apache/kafka`
  ("By The Apache Software Foundation"; "KRaft combined mode (meaning that
  the broker handling client requests and the controller handling cluster
  coordination both run in the same container)");
  `https://archive.apache.org/dist/kafka/4.0.0/RELEASE_NOTES.html`
  (KAFKA-17611 "Remove ZK from Kafka 4.0" among the tracked items; no
  narrative sentence). The broker-side ordering citation is ADR-0001
  Amendment 1's, from the 4.3.1 site-docs tarball.
* Prometheus: `https://raw.githubusercontent.com/prometheus/prometheus/main/LICENSE`
  (Apache License Version 2.0).
* Grafana: `https://raw.githubusercontent.com/grafana/grafana/main/LICENSE`
  (GNU Affero General Public License Version 3). Perses:
  `https://raw.githubusercontent.com/perses/perses/main/LICENSE` (Apache
  2.0) and `.../main/README.md` ("Perses is a Cloud Native Computing
  Foundation sandbox project"; supports "Prometheus metrics, Tempo traces,
  Loki for logs, Pyroscope"; nothing about Grafana dashboard import).
* Bitnami: `https://github.com/bitnami/containers/issues/83267` —
  brownouts from 28 August 2025, public catalogue removal 29 September
  2025, paid "Bitnami Secure Images", versioned tags moved to
  `bitnamilegacy` with "no further updates or support", a reduced free
  set limited to `latest` tags; "source code for containers and Helm
  charts remains available on GitHub under the Apache 2.0 license".
* OSI approval flags (SPDX source XML, secondhand for OSI):
  `https://raw.githubusercontent.com/spdx/license-list-XML/main/src/AGPL-3.0-only.xml`
  (`isOsiApproved="true"`), `.../MPL-2.0.xml` (`true`),
  `.../BUSL-1.1.xml` (`false`), `.../SSPL-1.0.xml` (`false`).
* PyPI metadata, `https://pypi.org/pypi/<name>/json`, `info.license_expression`
  (or `info.license` where the expression is absent): aiokafka 0.14.0
  Apache-2.0; fastapi 0.141.1 MIT; uvicorn 0.53.0 BSD-3-Clause; jsonschema
  4.26.0 MIT; prometheus-client 0.26.0 "Apache-2.0 AND BSD-2-Clause";
  structlog 26.1.0 "MIT OR Apache-2.0"; redis 8.1.0 MIT (author "Redis
  Inc." <oss@redis.com>); httpx 0.28.1 BSD-3-Clause; hypothesis 6.168.0
  MPL-2.0; pytest 9.1.1 MIT; pytest-asyncio 1.4.0 Apache-2.0; ruff 0.16.8
  MIT; mypy 2.3.1 MIT; pytest-benchmark 5.3.0 BSD-2-Clause; fakeredis
  2.38.0 BSD-3-Clause; types-jsonschema 4.26.0.20260518 Apache-2.0;
  hatchling 1.32.3 MIT; uv 0.12.16 "MIT OR Apache-2.0". Pydantic:
  `https://raw.githubusercontent.com/pydantic/pydantic/main/LICENSE` ("The
  MIT License (MIT) / Copyright (c) 2017 to present Pydantic Services Inc.
  and individual contributors").
* GitHub Actions: `https://raw.githubusercontent.com/actions/checkout/main/LICENSE`
  (MIT, "Copyright (c) 2018 GitHub, Inc. and contributors");
  `https://raw.githubusercontent.com/astral-sh/setup-uv/main/LICENSE` (MIT,
  "Copyright (c) 2024 Kevin Stillhammer").
* Repository facts: `deploy/docker-compose.yml` lines 5, 15, 53;
  `services/*/Dockerfile` line 1 (`FROM python:3.12-slim`);
  `packages/hammertime-bus/src/hammertime/bus/kafka.py` (aiokafka
  features used); `packages/hammertime-store/src/hammertime/store/redis.py`
  (`noeviction` requirement; commands used); `uv.lock` (versions);
  `.github/workflows/ci.yml`; `LICENSE` (MIT).
