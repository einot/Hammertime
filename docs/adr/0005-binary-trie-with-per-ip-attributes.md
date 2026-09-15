# ADR 0005 — The trie stays binary; hotness lives in an extensible per-IP attribute record

Status: accepted

## Context

The question that prompted this was whether HOT/COLD could become a configurable
numeric "hotness" score instead of a binary classification.

It could not, cheaply. The trie's load-bearing property is the §12 invariant:

```text
hot_count(node) = number of currently HOT /32 addresses in the subtree
hot_count(/32)  ∈ {0, 1}
hot_count(node) = hot_count(child[0]) + hot_count(child[1])
```

Everything downstream is built on that being an exact, replayable integer:
`hot_ratio = hot_count / capacity` (§3, §13), the `HOT_PREFIX` predicate (§38),
the O(bit_width) increment/decrement path (§10, §11), reconstruction of the trie
by replaying `HotIpAdded`/`HotIpRemoved` (§32, §33), and the snapshot format
(§33). Replacing the leaf primitive with a score would mean:

* a per-node **sum of scores** rather than a count — a second aggregate that has
  to survive replay exactly. With float scores, `add` then `remove` does not
  return a node to its previous value, so the invariant degrades to "approximately
  equal" and `services/trie/structure/invariants.py` has nothing exact to assert.
* `hot_ratio` losing its meaning (a ratio of what to capacity?), which takes
  §13's two-condition classification and §38's invariant with it.
* per-level score-change events instead of per-transition events: today the trie
  emits work only on an actual COLD↔HOT edge, which hysteresis (§6, §7) makes
  deliberately rare. A continuously-varying score changes on nearly every
  observation, multiplying `hammertime.hot-ip.v1` volume by roughly the
  observation rate and destroying the damping hysteresis exists to provide.

`services/trie/`, `services/aggregator/`'s transition path, and
`services/detector/` are all still stubs, so none of this is expensive *yet* —
but the binary primitive is also not the thing that was actually wanted. What
was wanted was: an operator looking at a HOT IP should be able to see *how* hot,
and eventually *why* it is in the trie — which alerting system or detector rule
put it there.

That is descriptive information about an IP, not a change to the trie's
arithmetic.

## Decision

**1. HOT/COLD stays the trie's binary primitive.** §12's invariant, `hot_ratio`,
§38's `HOT_PREFIX` predicate, the hysteresis rules of §6/§7, and the
`HotIpAdded`/`HotIpRemoved` event types are unchanged. No numeric quantity is
ever summed along the trie path.

**2. A HOT IP may carry an `IpAttributes` document** —
`schemas/ip_attributes.v1.json`, specified at spec level in §46. It is an open,
versioned JSON object: a small set of *registered* names with defined types and
meanings, plus an `x_`-prefixed namespace for anything not yet registered. The
whole document is descriptive: nothing in it may influence `hot_count`,
`hot_ratio`, prefix classification, or the state machine.

**3. `weight` is the first (and only) registered attribute.** A fixed-point
integer in thousandths of `hot_threshold` — 1000 means "window count was exactly
at the hot threshold". Integer, so replay is exact and there is no float drift
to reason about. Produced by `detection_config`'s `weight_function` (v1:
`threshold_ratio`), which is a pure function of the event's own `window_count`
and the config version it names — so any consumer can recompute and verify it,
and a consumer that drops it loses nothing irrecoverable.

**4. The transport is the existing HOT/COLD event; the storage is a side map
owned by the trie service.** `schemas/hot_ip_event.v1.json` gains one optional
property, `attributes`. The trie service keeps `ip -> IpAttributes` in a map
beside the trie, updated in the same single-writer step as the `hot_count` path
(§28):

```text
HotIpAdded(ip, attributes)    ->  hot_count += 1 along path;  record[ip] = attributes
HotIpRemoved(ip)              ->  hot_count -= 1 along path;  del record[ip]
```

which yields a new invariant that *derives from* the binary primitive instead of
perturbing it:

```text
set(record.keys()) == the set of currently HOT /32 addresses
len(record)        == root.hot_count      (per address family)
```

An absent `attributes` on a `HotIpAdded` means the default document
(`{"attributes_version": 1}`), so the record exists either way and the count
invariant holds unconditionally.

**5. Attributes are trusted-producer-only.** `schemas/observation.v1.json` is
unchanged: an agent cannot supply attributes, for the same reason §36 says it
cannot declare `IP = HOT`. Only the aggregator (and, later, other internal
producers) writes them, and the trie validates shape and size before storing.

## Why this shape, and not the alternatives

**Not a field on `TrieNode`.** §9's node layout is `child[0]`, `child[1]`,
`hot_count`, `local_metadata`, `prefix_state`, and only the `/32` leaf would
ever carry per-IP data — the other 31 (or 127) levels on every path would grow a
permanently-`None` slot. A Patricia trie (§27) may not even materialize the leaf
as a distinct node. And node lifetime is not IP lifetime: §11 lets structural
nodes be retained or arena-allocated across HOT/COLD oscillation, whereas an
attribute record's lifetime is exactly "currently HOT". A side map keyed by the
full address sidesteps all three, at the cost of one O(1) lookup that is not on
the O(bit_width) update path.

**Not only in the event.** §32 makes the trie derived state, rebuilt by
replaying HOT/COLD transitions. If attributes existed only in flight, the read
path could not answer "why is this IP hot?" after a restart without re-reading
the log. Carrying them on the transition event *and* materializing them in the
trie service's own state (and in its §33 snapshot) keeps one durability story:
replay the same topic, get the same trie *and* the same records.

**Not a separate external side-store.** A second store with its own
availability and consistency envelope can disagree with `hot_count` — an IP that
is HOT in the trie but has no record, or a record for an IP that is COLD. That
is a new failure mode for information that is already fully determined by the
event stream the trie consumes. Same process, same writer, same snapshot, same
replay.

**Registry + `x_` namespace, not a wide-open map.** A fully open object with
`additionalProperties: true` is cheap to extend and impossible to depend on:
producers drift, consumers start relying on keys nobody defined, and typos are
indistinguishable from new features. A fixed struct is the opposite failure —
every new field is a schema migration. The two-tier split gives both: registered
names are typed, documented in §46, and validated (a misspelled `wieght` is
rejected); `x_`-prefixed names are free, validated only for shape, and MUST be
preserved verbatim by consumers that do not understand them, so a producer can
ship an experiment through the whole pipeline before anyone standardizes it.

**`sources` is designed now and enabled later.** The provenance trail the owner
actually wants — a bounded list of `(system, rule, at, detail)` entries saying
which alerting system or detector rule put this IP in the trie — is written out
in `$defs` of `schemas/ip_attributes.v1.json` but deliberately not referenced
from `properties`, so a document carrying `sources` is rejected today.
Turning it on is one line in `properties` plus a §46 registry entry; until then
experiments use `x_sources`. This is the concrete test of whether the mechanism
is open enough, and it passes without shipping a field nothing populates.

## Consequences

* **Additive and non-breaking.** `attributes` is optional; the codec omits it
  when absent, so existing encodings are byte-identical. `event_id` derives from
  `(agent_id, sequence, event_type, subject)` (ADR-0004), not from payload
  contents, so identity and redelivery dedup are untouched. A build that does
  not know the key ignores it and derives exactly the same `hot_count`. The trie
  snapshot format changes, but `services/trie/snapshot/` is still a stub, so
  there is no deployed snapshot to migrate.
* A strict JSON-Schema validator pinned to the *previous* `hot_ip_event.v1.json`
  (which has `additionalProperties: false`) rejects an event carrying
  `attributes`. Nothing in the repo validates hot-ip events against the schema at
  runtime today — the codec is the enforcement point — but a downstream consumer
  that does must take the schema update before a producer starts emitting.
* `schemas/hot_ip_event.v1.json` now has the repo's first cross-file `$ref`.
  Any validator that compiles it MUST resolve
  `https://hammertime.dev/schemas/ip_attributes.v1.json` from the local
  `schemas/` directory via a preloaded registry — those `$id`s are identifiers,
  not fetchable URLs, and a validator left to resolve them over the network
  would turn schema compilation into an outbound request on a hostname the
  project does not control. `services/ingest`'s
  `validation/schema.py` loads `schemas/` from disk and only ever compiles
  `observation.v1.json` (which has no `$ref`), so nothing today is affected.
* Because `event_id` ignores the payload, attributes cannot be *revised* by
  re-emitting the same transition: a consumer deduping on `event_id` would drop
  it. Updating attributes for an already-HOT IP therefore needs its own event
  type (`IpAttributesChanged`, IP-keyed, with its own sequence) whenever a second
  writer appears. Point 4's replace-on-add rule is exactly right for one writer
  and deliberately does not pretend to solve multi-writer merge; the per-attribute
  `combine()` discipline §16 already requires for prefix metadata is the model to
  follow when that day comes.
* Memory grows by one bounded record per HOT IP (≤ 1 KiB serialized, ≤ 16 keys),
  i.e. `hot_ip_count × record_size`, not `trie_nodes × record_size` (§26).
* Per-IP attributes and §16's prefix `local_metadata` are two different
  mechanisms with two different lifetimes and no shared namespace. Prefix
  metadata is operator-declared, inherited down the path, and never materialized
  (§17); per-IP attributes are machine-produced, not inherited, and live exactly
  as long as the IP is HOT. `GET /ip/{ip}` returns both, separately keyed.
* `weight` is available to the detector as a ranking/severity input (§14, §31)
  but is explicitly *not* an input to the `HOT_PREFIX` predicate. Prefix-level
  weight aggregates (e.g. mean weight of a /24's hot members) are deliberately
  deferred: maintaining one incrementally would recreate exactly the second
  replayable per-node aggregate this ADR exists to avoid. A detector that wants
  one can compute it on demand from the records of a prefix's hot descendants.
* `schemas/detection_config.v1.json` gains `weight_function` and `weight_max`
  ahead of the code that reads them. `core/config/loader.py` derives its
  accepted-key set from `DetectionConfig`'s dataclass fields and rejects
  anything else, so until both fields are added to
  `core/config/models.py` a config document that is valid per the schema is
  rejected by the loader — and `weight_function` must additionally be excluded
  from that module's `_INTEGER_KEYS`. Closing that gap is the first task of the
  implementing issue.
