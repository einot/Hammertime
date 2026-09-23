# ADR 0015 — Prefix metadata lives in prefix-keyed side maps: two combine directions, and the per-IP attribute record coupled to the hot-count step

Status: accepted; amended 2026-09-23 (see "Amendment 1" at the end — three
gaps `test-author` hit while writing epic #9's tests from this ADR, and four
more found while ruling them, are ruled: decision 2's worked example is
corrected, because a `/23` at 25 % *is* the mean of its two `/24` halves at
50 % and 0 %, and a `/24` beside a `/25` now shows that averaging is wrong;
`prefix in store` raises the family `ValueError` for a `Prefix` of the other
family and answers `False` for anything that is not a `Prefix`; `declare`
refuses a document that is not a `Mapping` of `str` to
`Tags`/`Bitmask`/`Override` with a `TypeError`, and a declaration that would
give a key two kinds on one path with a `ValueError`; each value type checks
its field's type when it is built; and the combine functions refuse an
operand that is not one of the three kinds with a `TypeError`. Each edit is
in place with a dated note and is listed there. Only the path ruling changes
a behaviour the text implied — such a declaration used to be accepted, and
every read through it failed; the other rulings correct an example, confirm
a reading or state what was unstated)

Scope note: this ADR settles the interfaces epic #9 ("Metadata inheritance &
hot-count aggregation") implements against —
`services/trie/src/hammertime/trie/metadata/{__init__,combine,local,ip_attributes}.py`
and `services/trie/src/hammertime/trie/tests/test_metadata.py`, plus the one
public §46.2 validator that the codec and the record map share
(`packages/hammertime-core/src/hammertime/core/events/attributes.py`, new, with
`InvalidAttributesError` added to `hammertime.core.errors`) — covering §16,
§17, the derived-view half of §3/§12, and the storage, validation and lifecycle
half of §46 (§46.2, §46.3, §46.5-§46.9). It does **not** design the trie
service's worker (§28), its read API (§29, ADR-0010 decision 4), the
`PrefixStatsChanged` publisher (ADR-0010 decision 3), the snapshot (§32, §33,
§46.8) or the cached `prefix_state` (§9, §12) — it states only what those need
from this module and names the seam.

It amends no prior ADR. It **implements** two that bind it: ADR-0005 decision 5
("the trie validates shape and size before storing") and ADR-0014 decision 9
("Epic #9 owns the map, its schema validation, and its lifecycle"). Decision 5
below is where both are discharged. ADR-0014 (the trie structure package, with
Amendments 1 and 2), ADR-0005 (the trie stays binary; attributes live beside
it), ADR-0010 (the single prefix predicate and the read shapes) and ADR-0001
(one single-writer owner per address family) are taken as given and cited
where they bind.

## Context

`services/trie/metadata/` is three docstring-only stubs (`__init__.py`,
`local.py`, `combine.py`); `ip_attributes.py` does not exist yet. Two different
things are called "metadata" in the material this epic is scoped to, and they
move in opposite directions through the trie:

* **Up.** §10-§12: every prefix reports `hot_count`, the number of currently
  HOT `/32`s beneath it, an internal node's count being the sum of its
  children's. §3 derives `capacity` and `hot_ratio` from it. Epic #9's headline
  sentence ("aggregate per-node hot-IP metadata up the trie so every prefix
  level reports how many hot IPs it contains") and its first acceptance
  criterion ("combining child metadata into a parent is associative /
  order-independent") are about this one.
* **Down.** §16, §17: metadata *declared on a prefix* is stored once, on that
  prefix, and accumulated along a lookup path —
  `effective_metadata(IP) = combine(root.local_metadata, …, /32.local_metadata)`
  — never copied into descendants. `combine()` "MUST be explicitly defined for
  each metadata type", and the trie "SHOULD NOT assume that all metadata is
  mergeable by simple union". Epic #9's second acceptance criterion ("local vs.
  inherited metadata are clearly separated so a node's own state survives child
  pruning") is about this one.

A third mechanism shares the epic and neither direction: §46's per-IP
`IpAttributes`, which ADR-0005 put in a side map keyed by full address,
descriptive only, never inherited and never aggregated (§46.6 contrasts the two
in as many words).

Fixed by the spec and by prior ADRs:

* §9's node is `child[0]`, `child[1]`, `hot_count`, `local_metadata`,
  `prefix_state`. ADR-0014 assumption 21 stores only the first three and leaves
  `local_metadata` and `prefix_state` outside the structure package, taking no
  position on where they go: "that is the metadata epic's and the query epic's
  call respectively". This ADR answers the first half; `prefix_state` stays the
  query epic's.
* ADR-0014 decision 4 makes pruning mandatory: a node whose `hot_count` reaches
  zero is removed, and the structure is a pure function of the current hot set.
  Anything whose lifetime is *not* the hot set's therefore cannot live on a
  node.
* §27/ADR-0014 decision 6: a Patricia node does not exist for every prefix. A
  prefix an operator wants to annotate may have no node at all, now or ever.
* §46.5, ADR-0005 decision 4: `HotIpAdded(ip, attributes)` sets
  `record[ip] = attributes` in the same single-writer step that increments the
  path; `HotIpRemoved(ip)` deletes it. `set(record.keys())` is the set of
  currently HOT addresses and `len(record) == hot_count(root)` per family.
* ADR-0014 decision 9: `check_attribute_records(trie, records:
  Collection[Address])` already asserts that arithmetic against anything
  `Collection[Address]`-shaped, so the structure package stays ignorant of the
  map — and the same decision gives this epic "the map, its schema validation,
  and its lifecycle".
* ADR-0005 decision 5 and §46.9: attributes are written by trusted internal
  producers only, "the trie validates shape and size before storing", and "a
  producer MUST validate size and shape before storing, and no attribute value
  may be used in an authorization, routing, or rate-limiting decision". The
  §46.2 rules are implemented today exactly once, privately, inside
  `hammertime.core.events.codec` (`_validate_attributes` and its helpers), which
  applies them on both encode and decode.
* §46.1: attributes are descriptive only; `hot_count` and the §12 invariant
  "MUST NOT be influenced by any attribute".
* ADR-0014 decision 3 and Amendment 2 A12: a redundant `add_hot_ip` /
  `remove_hot_ip` returns `False` and changes nothing, and §46.5's
  replace-on-add applies to the record map whatever that `bool` said; a mutator
  that finds corruption it cannot walk past raises `InvariantViolation`
  **before mutating anything**.
* `docs/protocol/read-api-v1.md`: `GET /ip/{addr}` returns `attributes` iff the
  IP is HOT, and "an absent `attributes` on the event is stored, and returned,
  as `{"attributes_version": 1}`"; `GET /prefix/{cidr}` returns `hot_ips`,
  `capacity` and `hot_ratio` "computed exactly then narrowed once
  (`Prefix.hot_ratio`)"; `state` is ADR-0010 decision 1's
  `evaluate_prefix_state(hot_ips, capacity, config)` and nothing else
  classifies.

Open, and blocking anyone who wants to write a test or a module:

1. Where does `local_metadata` live, keyed by what, and what makes it survive
   pruning and path compression?
2. What *is* a metadata value, given §16's "the trie SHOULD NOT assume that all
   metadata is mergeable by simple union", and what are the algebraic laws the
   tests must pin?
3. The first acceptance criterion demands order-independence; §16 explicitly
   permits priority/override semantics, which are order-*dependent*. Which
   operation does the criterion bind?
4. How does `hot_ratio` relate to `hot_count` and `Prefix.hot_ratio`, and is it
   itself aggregated?
5. What is the per-IP record map's type and API, how does it validate the
   documents it stores without a second copy of the codec's rules, what happens
   to a transition whose document is rejected, and does the single-writer
   coupling of §46.5 live in this epic or in the worker epic?

## Decision

### 1. Two combines, two directions, two different sets of laws

```text
upward    §10-§12   hot_count of the children  ->  hot_count of the parent
                    integer addition: associative, commutative, identity 0,
                    and it is the ONLY per-node aggregate (ADR-0005 decision 1)

downward  §16, §17  local metadata of the ancestors  ->  effective metadata
                    document-wise combine(): associative, identity = the empty
                    document, idempotent — commutative only for value kinds
                    that are (decision 3)
```

Epic #9's "associative / order-independent (safe under concurrent updates)"
binds the **upward** aggregate in full: two children have no order between
them, a subtree may be summed in any grouping, and (§21 Option B, ADR-0001
"Relation to Option B") a future query-time sum across per-shard tries must
land on the same number. It binds the **downward** combine only in its
associativity and identity: §16's own "Policy … may use priority/override
semantics" is a rule where the more specific declaration wins, which is not
commutative and must not be made so. The path supplies the order — always
least-specific first, `/0` through `/bit_length` — so the fold is deterministic
without being commutative. Anything that needs order-independence downward uses
only the order-independent kinds.

"Order-independent" here is a statement about the algebra, not about
concurrency control: §28 and ADR-0001 give the trie one writer, ADR-0014
assumption 19 makes the structure explicitly not thread-safe, and neither store
in this ADR is thread-safe either (decision 8).

**The upward aggregate is already implemented.** `add_hot_ip`/`remove_hot_ip`
maintain it along the path (§10, §11, ADR-0014 decision 2) and
`hot_count(prefix)`, `ancestor_counts` and `iter_prefix_counts` report it.
This epic adds no second counting path. What it adds is the derived view
(decision 2) and the statement of the law, which the tests pin as an algebra
rather than by re-walking a trie.

### 2. `PrefixStats`: the one derivation of `capacity` and `hot_ratio`, and `hot_ratio` is never combined

```python
# hammertime.trie.metadata.combine          Spec: §3, §12, §16, §17
@dataclass(frozen=True, slots=True)
class PrefixStats:
    """One prefix's §3 view: hot_count, capacity, hot_ratio."""
    prefix: Prefix
    hot_count: int                      # < 0 is a ValueError (§11)

    @property
    def capacity(self) -> int: ...      # prefix.capacity()
    @property
    def hot_ratio(self) -> float: ...   # prefix.hot_ratio(hot_count)
    @property
    def hot_ratio_exact(self) -> Fraction: ...


def aggregate(parent: Prefix, parts: Iterable[PrefixStats]) -> PrefixStats: ...
def prefix_stats(trie: HotTrie, prefix: Prefix) -> PrefixStats: ...
def ancestor_stats(
    trie: HotTrie, address: Address, *, min_length: int = 0
) -> tuple[PrefixStats, ...]: ...
```

* **`capacity` and `hot_ratio` are functions of the prefix and the count, never
  of the parts.** `hot_ratio` is recomputed at each level from that level's
  `hot_count` and `capacity`; ratios are never summed or averaged upward. This
  is the mistake the type exists to prevent. A `/24` holding 128 HOT addresses
  is at 50 %; beside an empty sibling `/24`, their parent `/23` holds 128 of
  512 addresses, 25 % — not the sum of the two ratios (50 %). It is their
  mean, though, as it always is for the two equal halves of a prefix (halves
  holding `h0` and `h1` under a parent of capacity `c` give
  `(h0 / (c/2) + h1 / (c/2)) / 2 == (h0 + h1) / c`), so equal halves cannot
  tell recomputing from averaging. Parts of unequal size can: under a `/23`, a
  `/24` holding 128 HOT addresses (50 %) and a `/25` beside it holding 64
  (50 %) give the parent 192 of 512, 37.5 % — neither the sum (100 %) nor the
  mean (50 %) of the parts' ratios, nor their capacity-weighted mean (192 of
  the 384 addresses the parts cover, 50 %). Summing and averaging are each
  right only for particular shapes of parts — the mean for two equal halves,
  the sum for shards that report the same prefix (assumption 15) — whereas
  the summed `hot_count` over the parent's own `capacity` is right for every
  shape, and is all that `aggregate` and `PrefixStats` compute. *(Corrected
  2026-09-23, Amendment 1 ruling 1; the superseded sentence is quoted
  there.)*
* **`hot_ratio` (float) is for presentation only** — `read-api-v1.md`'s
  `hot_ratio`, `schemas/prefix_stats_event.v1.json`'s optional `hot_ratio`. It
  delegates to `Prefix.hot_ratio`, which computes the ratio exactly as a
  `Fraction` and narrows once, so there is no second copy of the arithmetic.
  Any *comparison* uses `hot_count` and `capacity` (ADR-0010 decision 1's
  `evaluate_prefix_state(hot_count, capacity, config)` takes exactly those two
  integers) or `hot_ratio_exact`. **This module never evaluates the §13
  predicate**: one implementation, in `hammertime.core.state.prefix`, is
  ADR-0010 decision 1 and is the query epic's to build.
* **`aggregate(parent, parts)`** returns `PrefixStats(parent, sum(part.hot_count
  for part in parts))` — order-independent by construction, identity
  `aggregate(parent, []) == PrefixStats(parent, 0)`, and associative in the
  sense the acceptance criterion needs: regrouping the parts through any
  intermediate prefixes under `parent` gives the same answer. Every part's
  prefix must be contained in `parent` (same family, `part.length >=
  parent.length`, network under `parent`), else `ValueError`. That a HOT
  address is counted by at most one part is the **caller's precondition, not
  checked**: it holds by construction for the two structural children of a node
  and for per-shard parts under ADR-0001's one-owner-per-IP rule, and checking
  it would mean enumerating addresses.
* **A negative `hot_count` is a `ValueError` at construction** (§11). That is
  an argument check on a value type anyone may build, not a clamp: ADR-0014
  Amendment 1 A3 requires a corrupted count to stay *visible* through the
  structure's own observables (`NodeView.hot_count` verbatim, `hot_ip_count ==
  -1`), and refusing to build a view around it neither hides nor repairs it —
  it stops a schema-invalid `PrefixStatsChanged` (`hot_count` has `minimum: 0`,
  `hot_ratio` `[0, 1]`) from being published in its place. Diagnosing the
  corruption stays `check_trie`'s job, and the error type stays `ValueError`
  per ADR-0014 assumption 18: `InvariantViolation` belongs to the checks and to
  a mutator that finds corruption, not to a constructor rejecting an argument.
* **`prefix_stats` and `ancestor_stats`** bind the structure's counts into the
  view: `prefix_stats` is `PrefixStats(prefix, trie.hot_count(prefix))`;
  `ancestor_stats` maps `trie.ancestor_counts(address, min_length=…)` element
  for element, preserving its order (ascending length, zero counts included —
  ADR-0014 decision 2). They exist so that the publisher (ADR-0010 decision 3's
  25 messages) and the read path do not each rewrite the capacity/ratio
  derivation; `min_length` is passed through and its `ValueError` is the
  structure's.

### 3. A metadata value carries its own `combine()`; three kinds, matching §16's three examples

```python
@dataclass(frozen=True, slots=True)
class Tags:                       # §16 "Set metadata":  combine(A, B) = A ∪ B
    values: frozenset[str]
    @classmethod
    def of(cls, *names: str) -> "Tags": ...

@dataclass(frozen=True, slots=True)
class Bitmask:                    # §16 "Bitmask":       combine(A, B) = A | B
    bits: int                     # < 0 is a ValueError

@dataclass(frozen=True, slots=True)
class Override:                   # §16 "Policy":        the more specific wins
    value: str | int | bool

MetadataValue = Tags | Bitmask | Override
Metadata = Mapping[str, MetadataValue]
EMPTY_METADATA: Final[Metadata] = MappingProxyType({})

def combine_values(
    less_specific: MetadataValue, more_specific: MetadataValue
) -> MetadataValue: ...
def combine(less_specific: Metadata, more_specific: Metadata) -> Metadata: ...
def combine_path(documents: Iterable[Metadata]) -> Metadata: ...
```

§16 requires `combine()` to be "explicitly defined for each metadata type", so
the **type carries the rule**: dispatch is on the value's kind, not on a
registry keyed by name and not on a guess from the underlying Python type (an
`int` is a bitmask or a priority depending on what the operator meant, and only
a declaration can say which). Two different kinds under one key is a
declaration error — `ValueError` naming the key and both kinds — not a merge.

**Every value is checked when it is built**, so no malformed value exists for
`declare` or `combine` to meet — which is why both ask only which kind a value
is. `Tags.values` must be a `frozenset` whose every member is a `str`
(`Tags.of(*names)` builds one); `Bitmask.bits` must be an `int` that is not a
`bool`, and `>= 0`; `Override.value` must be a `str` or an `int`, a `bool`
included (assumption 3's three types). A wrong type is a `TypeError` from the
constructor — `Tags(frozenset({1}))`, `Tags({"a"})`, `Tags.of("a", 1)`,
`Bitmask("3")`, `Bitmask(True)`, `Override(None)`, `Override([1])` and
`Override(1.5)` all raise one — while `Bitmask(-1)`, the right type with a bad
value, stays assumption 5's `ValueError`. The messages' wording is not part of
the contract. *(Added 2026-09-23, Amendment 1 ruling 5; assumption 46.)*

A document is a mapping from name to value. `combine(a, b)` takes the union of
the keys; a key present in both is `combine_values(a[k], b[k])`, a key present
in one is carried through unchanged — so **absence is the per-key identity**,
which is exactly why §17's "do not materialize" is sound: an undeclared
ancestor contributes nothing and can be skipped rather than stored as an empty
document. `combine_path(documents)` is the left fold of `combine` over an
iterable ordered **least-specific first**, starting from `EMPTY_METADATA`;
that order is §16's path order and is what gives `Override` its meaning.

**The combine functions check their operands' shape before they combine
anything**, with the `TypeError` `declare` uses (decision 4). An operand of
`combine_values` that is not a `Tags`, `Bitmask` or `Override` is a
`TypeError` whatever the other operand is; the mixed-kind `ValueError` is only
for two valid values of different kinds. `combine` checks both documents whole
— each must be a `Mapping` of `str` to one of the three kinds — before it
combines any key, so a malformed entry under a key only one operand holds is
refused rather than carried into the result, and a pair of documents that is
both malformed and mixed-kind is the `TypeError`. When the fault is a value
under a `str` key, the message names that key. `combine_path` is the left fold
of `combine`, so it raises at the first step that fails, in iteration order.
*(Added 2026-09-23, Amendment 1 ruling 7; assumption 48.)*

Laws every kind must satisfy, and which the tests pin:

```text
associativity   combine(combine(a, b), c) == combine(a, combine(b, c))     all kinds
identity        combine(EMPTY_METADATA, d) == d == combine(d, EMPTY_METADATA)
idempotence     combine(d, d) == d                                         all kinds
commutativity   combine(a, b) == combine(b, a)   Tags and Bitmask only —
                NOT Override, deliberately (decision 1)
```

Metadata is a domain object here and nothing else: there is no wire schema, no
config key, no event type and no `x_` namespace for it (contrast §46.2, whose
document *is* wire-borne and therefore is). Nothing populates the store in v1 —
see decision 4 and assumption 7.

### 4. `local_metadata` is a prefix-keyed side map, which is what makes it survive pruning and compression

```python
# hammertime.trie.metadata.local            Spec: §16, §17
class PrefixMetadataStore:
    def __init__(self, family: AddressFamily) -> None: ...
    @property
    def family(self) -> AddressFamily: ...

    def declare(self, prefix: Prefix, metadata: Metadata) -> None: ...
    def revoke(self, prefix: Prefix) -> bool: ...
    def clear(self) -> None: ...

    def local(self, prefix: Prefix) -> Metadata: ...                  # this prefix alone
    def inherited(self, prefix: Prefix) -> Metadata: ...              # strict ancestors
    def effective_for_prefix(self, prefix: Prefix) -> Metadata: ...   # ancestors + itself
    def effective(self, address: Address) -> Metadata: ...            # §16's effective_metadata(IP)

    def declarations(self) -> tuple[tuple[Prefix, Metadata], ...]: ...
    def __len__(self) -> int: ...
    def __contains__(self, prefix: object) -> bool: ...
```

* **Keyed by `Prefix`, held beside the trie, never on a node.** The store has
  no reference to a trie and a trie has no reference to it. That single choice
  answers epic #9's second acceptance criterion and ADR-0014 assumption 21's
  open half at once: ADR-0014 decision 4 deletes a node the moment its
  `hot_count` reaches zero, and §27 never materializes a node for a compressed
  prefix at all, so a declaration stored on a node would vanish when the last
  HOT address under it went COLD and would have nowhere to live when the prefix
  is inside a compressed edge. A declaration on `10.0.0.0/8` is unaffected by
  every add, remove, prune, split and collapse in the trie, and exists whether
  or not anything under it has ever been HOT. This is §46.6's "lifetime
  independent of hot state", stated for the storage rather than for the
  document.
* **Local, inherited and effective are three separate reads.** `local(P)` is
  what was declared on `P` and nothing else — declaring on an ancestor or a
  descendant never changes it. `inherited(P)` is `combine_path` over the
  declarations on `P`'s strict ancestors (lengths `0 .. P.length - 1`).
  `effective_for_prefix(P)` is `combine(inherited(P), local(P))`, and
  `effective(address)` is `effective_for_prefix` of the address's host route,
  i.e. §16's `effective_metadata(IP)` verbatim — the fold runs `/0` through
  `/bit_length` **inclusive**.
* **Nothing is materialized (§17).** `declare` writes exactly one key. A lookup
  costs `bit_length + 1` dictionary probes and is independent both of the
  number of declarations and of the hot set; no declaration is ever copied to a
  descendant, and there is no propagation to bound.
* **Family-scoped**, like everything else in this service (ADR-0014 decision
  1): a `Prefix` or `Address` of the other family is a `ValueError` naming both
  families, checked before any other argument. `prefix in store` is included:
  `__contains__` raises that `ValueError` for a `Prefix` of the other family
  rather than answering `False`, as `a in records` does (decision 5,
  assumption 39), because `False` would read as "nothing declared" — the
  routing bug assumption 8 scopes the store per family to catch. *(Stated
  explicitly 2026-09-23, Amendment 1 ruling 2; assumption 43.)* The family
  rule applies to a `Prefix` or `Address` in the place a method expects one,
  and `__contains__` expects a `Prefix`: an argument that is not a `Prefix` at
  all — an `Address` of either family, a `str`, `None`, an unhashable object
  — is not a key of any family and is simply absent: `False`, never an
  exception, as a non-`Address` is for `a in records` (decision 5). An
  `Address` is not read as its host route. *(Added 2026-09-23, Amendment 1
  ruling 4; assumption 45.)*
* `declare` replaces any existing declaration for that exact prefix and stores
  an immutable copy of the document; `revoke` returns whether anything was
  removed; `declarations()` yields every declaration sorted ascending by
  `(length, network)`, which is the fold order and is deterministic across
  runs.
* **`declare` refuses a document that is not `Metadata`, with a `TypeError`.**
  The document must be a `collections.abc.Mapping` whose every key is a `str`
  and every value an instance of `Tags`, `Bitmask` or `Override`. Anything
  else is a `TypeError`: a non-mapping, a non-`str` key, or a bare value such
  as an `int`, a `str` or a `set`. A non-mapping includes `None` — unlike
  `record()`, `declare` has no default document, and `revoke` is the explicit
  removal (assumption 10) — and a list of `(name, value)` pairs, even though
  `dict()` would accept one. When the fault is a value under a `str` key, the
  message names that key. A bare value is refused rather than wrapped for
  decision 3's reason: an `int` is a `Bitmask` or an `Override` only because a
  declaration says which. The check runs after the family check, so a
  `Prefix` of the other family with a bad document is the family
  `ValueError`. It runs on the copy `declare` would store and completes
  before the store changes: a refused `declare` leaves the store's length, its
  `declarations()` and any earlier declaration on that prefix exactly as they
  were. It checks each value's kind, not its contents: every value's own rules
  are enforced when it is built (decision 3). A document with several faults
  may be reported for any of them. *(Added 2026-09-23, Amendment 1 ruling 3;
  assumption 44.)*
* **`declare` refuses a declaration that would give a key two kinds on one
  path**, so that no read of the store can meet decision 3's mixed-kind
  `ValueError`. After the family and document checks, and before the store
  changes, `declare(P, document)` compares each key of `document` with the
  declaration on every other prefix that contains `P` or that `P` contains; if
  one holds the same key with a different kind, it raises `ValueError` naming
  the key, both kinds and that prefix, and the store is unchanged. `P`'s own
  current declaration, which the call would replace, is not compared. Two
  prefixes neither of which contains the other may use one key with different
  kinds, since no path passes through both — though a later declaration of
  that key on a prefix containing both is then refused, since it must
  conflict with one of them. With values checked when they are built
  (decision 3) and documents at `declare`, it follows that `local`,
  `inherited`, `effective_for_prefix` and `effective` raise nothing but the
  family `ValueError` for an argument of the type they take. The check costs
  a pass over the existing declarations; `declare` is an operator action on a
  hand-written map (assumption 9), and lookups are unaffected. *(Added
  2026-09-23, Amendment 1 ruling 6; assumption 47.)*

### 5. The per-IP attribute records: a read-only `Mapping[Address, IpAttributes]`, family-scoped, validated on every write by the one shared §46.2 validator, never interpreted

```python
# hammertime.core.events.attributes         Spec: §46.2, §46.3, §46.9      (new)
def validate_ip_attributes(document: object) -> int: ...
    # The §46.2 rules below; returns the document's serialized size in bytes.
    # Raises InvalidAttributesError for every violation, and nothing else.

# hammertime.core.errors                                                     (added)
class InvalidAttributesError(HammertimeError): ...

# hammertime.trie.metadata.ip_attributes    Spec: §46.2, §46.5-§46.9
IpAttributes = Mapping[str, object]
DEFAULT_ATTRIBUTES: Final[IpAttributes] = MappingProxyType({"attributes_version": 1})


class IpAttributeRecords(Mapping[Address, IpAttributes]):
    def __init__(self, family: AddressFamily) -> None: ...
    @property
    def family(self) -> AddressFamily: ...
    @property
    def serialized_bytes(self) -> int: ...          # §46.8 ip_attribute_bytes

    # Mutation — §46.5. Single writer (§28); not thread-safe.
    def record(self, address: Address, attributes: Mapping[str, object] | None = None) -> None: ...
    def discard(self, address: Address) -> bool: ...
    def clear(self) -> None: ...

    # Mapping — __contains__, get, keys, items, values come from the ABC.
    def __getitem__(self, address: Address) -> IpAttributes: ...
    def __iter__(self) -> Iterator[Address]: ...
    def __len__(self) -> int: ...
```

**That the map validates is a requirement, not a choice made here.** ADR-0005
decision 5 says "the trie validates shape and size before storing", ADR-0014
decision 9 gives this epic "the map, its schema validation, and its
lifecycle", and §46.9 says a producer "MUST validate size and shape before
storing". An earlier draft of this ADR left validation to the codec alone, on
the grounds that every event reaches the trie through `decode`; a `supervisor`
review found that this departed from both ADRs without saying so, and the repo
owner ruled (2026-09-23) that the store validates **as well as** the codec —
defence in depth, so that the guarantee holds for any path that does not go
through the codec: a snapshot loader, a future in-process producer, a test
harness. Everything below is about *how*.

**The rules exist once, in `hammertime-core`.** The codec's private checks
(`_validate_attributes` and its helpers in `hammertime.core.events.codec`) move
into one new public function, `validate_ip_attributes`, in a new module
`hammertime.core.events.attributes`. Both enforcement points call it:

* **the codec**, on encode and on decode as today, translating the new
  `InvalidAttributesError` into its own `CodecError` with `raise … from exc`, so
  the codec's contract ("every failure mode surfaces as `CodecError`") and every
  existing codec test hold unchanged, and `CodecError.__cause__` tells a caller
  that the failure was an attributes rejection. Every rejection of a present
  `attributes` value goes through the validator — including an explicit
  `"attributes": null` on decode, which the codec today rejects inline and
  which is simply a document that is not a JSON object (S1) — so that
  `__cause__` identifies all of them and none is invisible to
  `attributes_rejected` (assumption 37);
* **the record map**, on every write, letting `InvalidAttributesError`
  propagate.

Nothing else in the repo may restate the 1024-byte cap, the 16-key cap, the
registered-name set or the `x_` grammar.

The rules. Where a rule goes beyond what the codec enforced before, the right
column says so; everything else is the codec's existing behaviour, moved:

```text
structural — every document, whatever its attributes_version
  S1  a JSON object: a dict                                              schema "type": "object"
  S2  at most 16 top-level keys                                          schema maxProperties
  S3  attributes_version present, a JSON integer (an int that is not a
      bool, or an integral finite float), and >= 1                       schema required, minimum
  S4  every value at every depth is in the JSON data model — dict with
      str keys, list, str, int, finite float, bool, None; no tuple,
      set, bytes, non-str key or other object                            NEW (see below)
  S5  no str, key or value, at any depth, holds an unpaired UTF-16
      surrogate (U+D800-U+DFFF)                                          keys are NEW; values were checked
  S6  serialized size <= 1024 bytes, measured as
        len(json.dumps(document, separators=(",", ":")).encode("utf-8"))
      with json's default ensure_ascii — the compact form the codec
      writes; this is the size the function returns                      §46.2; the return value is NEW
  S7  nesting too deep to walk or serialize, and a self-referencing
      structure, are rejections — never a RecursionError or ValueError   cycles are NEW

registry — only when attributes_version <= 1, the highest version this
build knows (§46.2: a higher version is stored and echoed verbatim and
is not interpreted)
  R1  every top-level key is registered (attributes_version, weight) or
      matches ^x_[a-z0-9_]{1,48}$ — so the reserved `sources` (§46.3)
      and a misspelt `wieght` are rejected                               schema properties, patternProperties
  R2  weight, if present, is a JSON integer (not a bool) in [0, 1000000]  schema weight
```

S4 and S7-on-cycles change nothing for wire input: `json.loads` produces only
JSON-model types, string keys and acyclic structures. They exist because the
record map now receives documents that were never parsed from JSON, and a
Python `tuple` stored as a record would read back as a `list` after a snapshot
or a replay — two different records for the same history, against §46.8's
"loading a snapshot and replaying subsequent events yields the same records as
a full replay would". On the codec's *encode* path S4 also closes a hole: its
value walk descended only into `dict` and `list`, so a `NaN` or a lone surrogate
inside a `tuple` reached the wire.

S5-on-keys is different: it **does** tighten what decode accepts, deliberately.
JSON text can spell a lone surrogate as an escape (`"\ud800"`) in a key as
easily as in a value — CPython's reference scanner decodes an unpaired
`\uD800`-range escape to `chr(uni)` whichever it is scanning
(`/usr/lib/python3.12/json/decoder.py`, `py_scanstring`, lines 116-125) — and
today's decode lets one through in a nested key, or in a top-level key under
an `attributes_version` above 1, where no name check applies. That is the
defect the codec already rejects in values, for the reason its own docstring
gives: such strings "corrupt or crash any strict downstream consumer (a schema
validator, a non-Python parser, a UTF-8 store)". A key is no safer than a value.

**An `InvalidAttributesError` message never contains a value from the
document.** It names the rule broken and, where that helps, the offending key —
at most its first 64 characters. §46.9 makes values opaque, the codec already
redacts `attributes` from its malformed-scalar message for the same reason, and
an echoed value is exactly how an oversized `x_` payload or a 5,000-digit
integer (whose `repr` itself raises) would turn a rejection into a flood or a
crash. The order in which the rules are checked is not part of the contract; a
document that breaks several may be reported for any of them.

The record map itself:

* **It is a `Mapping`, so it is already a `Collection[Address]`.** Verified
  against a primary source — the installed CPython 3.12 standard library,
  `/usr/lib/python3.12/_collections_abc.py`: line 431 `class
  Collection(Sized, Iterable, Container)`, line 787 `class Mapping(Collection)`
  with the docstring "This class provides concrete generic implementations of
  all methods except for `__getitem__`, `__iter__`, and `__len__`" (`docs.python.org`
  is blocked by this environment's egress proxy, as ADR-0014 assumption 2 also
  records, so the shipped module was read instead of the manual). So
  `check_attribute_records(trie, records)` and testkit's
  `assert_attribute_records_match(trie, records)` take this object directly,
  exactly as ADR-0014 decision 9 anticipated, with no import in either
  direction.
* **Not a `MutableMapping`.** There is no `__setitem__` and no `__delitem__`:
  `record` and `discard` are the only ways to change it, so the coupling in
  decision 6 is the only documented path by which the map moves.
* **`record(address, attributes)` is all-or-nothing.** In order: the family
  check (`ValueError`); `None` becomes `DEFAULT_ATTRIBUTES` (§46.5, and
  `read-api-v1.md`'s "an absent `attributes` on the event is stored, and
  returned, as `{"attributes_version": 1}`"), so a record exists for every HOT
  address unconditionally and §46.5's count invariant needs no special case; a
  document that is not a `Mapping` is an `InvalidAttributesError` (S1);
  `validate_ip_attributes` runs on a plain-`dict` copy of it; the validated
  document is deep-copied; and only then does the map change — the new record
  replaces any earlier one for that address (§46.5's replace-on-add) and
  `serialized_bytes` moves by the difference. Every step that can raise comes
  before the map changes, so a rejected call leaves the map, its length, its
  byte total and any earlier record for that address exactly as they were.
* **Stored records are private deep copies**, so nothing a caller does to the
  document afterwards — at any depth — can make a stored record invalid or
  different from what was validated; replay determinism depends on it. Reads
  return a read-only view of the stored top level (`MappingProxyType`); nested
  containers inside a returned record are read-only by contract.
* **`serialized_bytes` is §46.8's `ip_attribute_bytes`**: the sum of the sizes
  `validate_ip_attributes` returned for the records currently stored — so a
  default record counts 24 bytes, the length of `{"attributes_version":1}` —
  kept up to date by every replace, `discard` and `clear`, and O(1) to read.
  §46.8's `ip_attribute_records` is `len(records)`.
* **`discard` never raises** for an address with no record (the family check
  aside); it returns whether one was removed. A `HotIpRemoved` for an IP the
  trie does not hold (ADR-0011, ADR-0014 decision 3) therefore deletes nothing
  and leaves the invariant intact.
* **Family-scoped on every method that takes an `Address`, reads included.**
  An `Address` of the other family is a `ValueError` naming both families from
  `record`, `discard`, `records[a]`, `a in records` and `records.get(a)`, as
  ADR-0014 decision 1 rules for the trie's own queries. A key that is not an
  `Address` at all is simply absent, as the `Mapping` contract has it.
* **The store never interprets the document.** Validation checks shape and the
  registered names' types and ranges (§46.2's "validated strictly"); nothing
  else reads a stored value. Experimental `x_` values, and documents whose
  `attributes_version` is higher than this build knows, are stored and returned
  verbatim once they pass the structural rules (§46.2's pass-through rule;
  §46.9's "stored and echoed as opaque data"), and no stored value reaches
  `hot_count`, the §13 predicate, or any
  authorization, routing or rate-limiting decision (§46.1, §46.9). The module
  imports neither `json` nor the schema: sizes come back from the validator.

### 6. The §46.5 single-writer step lives in this epic, as two pure functions: validate, then the trie, then the record

```python
def apply_hot_ip_added(
    trie: HotTrie,
    records: IpAttributeRecords,
    address: Address,
    attributes: Mapping[str, object] | None = None,
) -> bool: ...

def apply_hot_ip_removed(
    trie: HotTrie, records: IpAttributeRecords, address: Address
) -> bool: ...
```

They are the whole of "never independently" (§46.5, ADR-0005 decision 4), and
nothing else. `apply_hot_ip_added` runs in exactly this order:

1. **Arguments.** `trie.family`, `records.family` and `address.family` must
   agree; a mismatch is a `ValueError` naming them.
2. **The document.** `attributes` is prepared exactly as `record()` prepares
   it — `None` becomes `DEFAULT_ATTRIBUTES`, a non-`Mapping` or any §46.2
   violation is an `InvalidAttributesError`, the validated document is
   deep-copied and its size kept — **before the trie is touched**. Validation
   runs once, here; clause 4 does not repeat it.
3. **The trie.** `trie.add_hot_ip(address)`. ADR-0014 Amendment 2 A12 allows it
   to raise `InvariantViolation` on an already-corrupt trie, and requires it to
   raise before mutating anything.
4. **The record.** The prepared document replaces any record for the address,
   and `serialized_bytes` moves by the difference. Nothing in this step can
   raise: everything fallible ran in clauses 1-3.
5. **The return value is the trie's**, unchanged: whether the HOT set changed.
   That is the signal ADR-0014 decision 3 preserved so the worker can decide
   what a no-op event emits — a question ADR-0011's Consequences and ADR-0010
   decision 3 leave to the worker epic, and this ADR does not take.

Every exception therefore leaves the trie and the map exactly as they were: a
family mismatch or a rejected document because nothing has been touched yet,
an `InvariantViolation` because A12 guarantees the trie is unchanged and the
record has not been written. The order is the only one that satisfies both
constraints at once. **Validation before the trie**: otherwise a rejected
document for a newly HOT address would leave it with no record (`len(records)
< hot_count(root)`). **The trie before the record** (A12): otherwise a corrupt
trie would leave a record for an address that is not HOT (`len(records) >
hot_count(root)`), turning a diagnosed corruption into a second one.

`apply_hot_ip_removed` checks the families, calls `trie.remove_hot_ip(address)`
(which A12 clause 3 gives no raise site), then `records.discard(address)`. It
takes no document, so there is nothing to validate: attributes on a
`HotIpRemoved` "MAY be logged but MUST NOT be stored" (§46.5).

**The record is written, or deleted, unconditionally** — whatever
`add_hot_ip` / `remove_hot_ip` returned. ADR-0014 decision 3 says so in as many
words: `False` does not mean "ignore the event"; a redelivered `HotIpAdded`
replaces the record while leaving every count untouched, and a `HotIpRemoved`
for an unknown address deletes whatever is there. Deleting unconditionally is
also self-healing: a stray record for a COLD address is removed by the next
removal for it.

**What each exception means to the worker (#10).** The mechanism is the worker
epic's; these are the constraints it inherits:

```text
ValueError              A routing bug: the families disagree. ADR-0014 decision 1
                        leaves the response to the worker.

InvalidAttributesError  A per-event rejection of the DOCUMENT — not of the
                        transition. §46.1: hot_count "MUST NOT be influenced by
                        any attribute", so a bad document must not stop the HOT
                        transition from being applied. The worker applies it
                        again with no document —
                            apply_hot_ip_added(trie, records, ip, None)
                        which stores DEFAULT_ATTRIBUTES — counts the rejection in
                        attributes_rejected (§46.8), logs it without the
                        document's content, and moves on. It must not drop the
                        event, retry the same document, dead-letter the event,
                        or rebuild: nothing is corrupt.

InvariantViolation      This process's trie is untrustworthy: rebuild from
                        snapshot plus replay (ADR-0014 A12 clause 5). Unchanged.
```

They take `(address, attributes)` rather than a `HotIpAdded`/`HotIpRemoved`
object: the pair is everything §46.5 uses, it keeps this module independent of
the event models (`hammertime.core.events.models`), and it is also the shape
the snapshot epic's restore path has (an address and a stored document, with no
event anywhere). The worker passes `event.ip, event.attributes`.

Everything else about applying an event stays outside: ordering, the event
loop, sequence numbers, publishing `PrefixStatsChanged`, choosing which
family's trie an event belongs to, the mechanism behind each response in the
block above, readiness and snapshots.

### 7. Module layout

```text
packages/hammertime-core/src/hammertime/core/
  errors.py          + InvalidAttributesError                              (§46.2)
  events/attributes.py   validate_ip_attributes — the §46.2 rules, once    (§46.2, §46.3, §46.9)   new
  events/codec.py    calls validate_ip_attributes; its private copies of the
                     rules are removed                                     (§19, §32, §46.2)

services/trie/src/hammertime/trie/metadata/
  __init__.py        re-exports the names of the other three modules   (§16, §17, §46)
  combine.py         Tags, Bitmask, Override, Metadata, EMPTY_METADATA,
                     combine_values, combine, combine_path;
                     PrefixStats, aggregate, prefix_stats, ancestor_stats  (§3, §12, §16, §17)
  local.py           PrefixMetadataStore                                   (§16, §17)
  ip_attributes.py   IpAttributes, DEFAULT_ATTRIBUTES, IpAttributeRecords,
                     apply_hot_ip_added, apply_hot_ip_removed              (§46)
```

Imports run one way. In `hammertime-core`, `events/attributes.py` imports only
the standard library and `hammertime.core.errors`, and `events/codec.py`
imports it. In the trie service, `combine.py` imports `hammertime.core` and
`hammertime.trie.structure.node` (for the `HotTrie` protocol, in annotations
only); `local.py` imports `combine.py`; `ip_attributes.py` imports
`hammertime.core` (including `hammertime.core.events.attributes`) and
`hammertime.trie.structure.node`; `__init__.py` imports all three and is
imported by none of them. **Nothing in `hammertime.trie.structure` imports
`hammertime.trie.metadata`**, which is what keeps ADR-0014 decision 9's "the
trie structure holds no reference to the map" true. Every module keeps its
existing `Spec:` docstring line, extended where this ADR adds a section, and
cites ADR-0015.

### 8. Neither store is thread-safe, and neither is snapshot-shaped by this epic

§28's single-writer model (ADR-0001) is the whole concurrency mechanism, as it
is for the structure (ADR-0014 assumption 19). Decision 1's order-independence
is algebraic and buys no thread safety.

For persistence the two maps differ, and the difference is worth stating
because it is not symmetric:

* **The attribute records are derived state and MUST be snapshotted** (§46.8:
  a snapshot includes the records so that load-plus-replay equals full replay).
  The snapshot epic can rebuild them through `record()` or
  `apply_hot_ip_added()`; no bulk-load API is added here for a caller that does
  not exist yet. Either path validates every loaded document (decision 5), so a
  snapshot file is never a way around §46.2; what the loader does when one is
  rejected — refuse the snapshot and replay from the log, or restore that
  address with the default document — is the snapshot epic's decision.
* **The local metadata store is not derived state and is not part of a §33
  snapshot.** It is operator-declared configuration; a replay of
  `hammertime.hot-ip.v1` neither creates nor destroys a declaration, and a
  snapshot that carried declarations would be recording configuration into a
  derived-state file. Where declarations come from and how they survive a
  restart is the open question of assumption 7, not a snapshot format.

## Assumptions

Each of these is a judgment call that the epic, the spec and the prior ADRs do
not dictate. Push back on them individually.

1. **The acceptance criterion's "order-independent" binds the upward
   aggregate, and only associativity/identity bind the downward one**
   (decision 1). The alternative reading — require the downward combine to be
   commutative too — is possible, and it would mean dropping `Override`, which
   §16 names explicitly as a legitimate policy semantic. I took the reading
   that keeps §16 whole and said so where the two appear to conflict, rather
   than silently choosing one.
2. **Three value kinds, no more, and the kind carries the rule.** §16 gives
   exactly three examples (set, bitmask, policy) and I implemented exactly
   those. The main alternative is a registry mapping key name to rule, like
   §46.3's attribute registry; rejected because it needs a registry document,
   a rule for unregistered names, and a validation story, for a mechanism with
   no wire format and no producer yet. A fourth kind later is one dataclass
   plus one case in `combine_values`.
3. **`Override.value` is `str | int | bool`.** Nothing specified a type. `None`
   is excluded so that "declared as nothing" and "not declared" stay distinct;
   containers are excluded because a container with an override rule is almost
   always a `Tags` in disguise. *(Enforcement stated 2026-09-23, Amendment 1
   ruling 5: any other type — `None`, a container, a `float` — is a
   `TypeError` from the constructor; assumption 46.)*
4. **Idempotence is required of every kind.** Nothing asked for it; all three
   kinds have it for free, and requiring it means a declaration applied twice,
   or a path folded twice, cannot drift. It does bar a future "sum" or "append"
   kind — which is deliberate, and is the point at which someone should amend
   this ADR rather than quietly add one.
5. **`Bitmask.bits >= 0`, enforced with `ValueError`.** A negative mask ORs to
   a nonsense width in Python's arbitrary-precision integers. *(Amendment 1
   ruling 5, 2026-09-23, adds a type check ahead of it: a `bits` that is not an
   `int`, or that is a `bool`, is a `TypeError`; assumption 46.)*
6. **No key grammar for metadata names, and no `x_` namespace.** §46 has both
   because its documents cross the wire and are produced by another service;
   metadata does neither yet. Adding a grammar later invalidates no stored
   document, because there are none.
7. **Nothing populates `PrefixMetadataStore` in v1, and this ADR does not
   design a source.** The spec says what metadata *is* (§16, §17) and never
   says how an operator declares it: there is no config key, no schema, no
   event type and no endpoint for it anywhere in the repo. Inventing one would
   mean a `CHANGES` entry, a schema and a protocol change well outside an epic
   whose file list is four modules. So the store ships with a programmatic API
   and no wire source — the same "designed now, enabled later" shape ADR-0005
   used for `sources`. Named as an open item in Consequences.
8. **The store is family-scoped.** Nothing forces it: a `Prefix` carries its
   family, so one store could hold both and simply never match across them.
   I scoped it per family for consistency with ADR-0014 decision 1 and because
   a `ValueError` on a foreign prefix catches a routing bug that would
   otherwise read as "no metadata declared". A dual-family process holds two
   stores, two record maps and two tries.
9. **`declarations()` is sorted by `(length, network)` and returns a tuple.**
   Insertion order would leak the declaration history into every reader;
   sorting is O(n log n) on a map an operator writes by hand, and the order
   matches the fold.
10. **`declare` stores whatever document it is given, including
    `EMPTY_METADATA`.** Treating an empty document as a revoke would be a
    convenience that makes `len(store)` mean two things. `revoke` is explicit.
    *(Qualified 2026-09-23, Amendment 1 rulings 3 and 6: "whatever document"
    now means any well-formed document that gives no key a second kind on its
    path — `declare` refuses a malformed one with a `TypeError` and a
    path-conflicting one with a `ValueError`. The point of this assumption
    stands: `EMPTY_METADATA`, which is well-formed and conflicts with nothing,
    is stored as a declaration, never read as a revoke.)*
11. **`local`, `inherited`, `effective_for_prefix` and `effective` are four
    separate methods.** Two would do (`local` and `effective`). I kept all four
    because the epic's acceptance criterion is precisely that local and
    inherited are *separable*, and a name for each makes that a property a test
    can assert rather than an implementation detail.
12. **`PrefixStats` exists at all, and lives in `combine.py`.** The epic fixes
    the module list, so a fifth module was not available; `combine.py` is the
    aggregation module and the upward aggregate belongs in it. The alternative
    — let the publisher and the read path each derive capacity and ratio from
    `(prefix, hot_count)` — is two copies of one line of arithmetic in two
    epics, which is the thing ADR-0010 decision 1 already refused for the
    predicate.
13. **`hot_ratio_exact` is exposed as well as `hot_ratio`.** Nothing requires
    it; `Prefix.hot_ratio` narrows to a float, and for an IPv6 `/0` that float
    has lost information. Any future comparison that is handed a ratio rather
    than the two integers should use the exact one. If it stays unused, it is
    two lines.
14. **`PrefixStats` rejects a negative `hot_count` rather than carrying it.**
    The alternative is ADR-0014 A3's rule for `NodeView` — report the stored
    count verbatim — which is right for a view whose purpose is diagnosis and
    wrong for one whose consumers are a wire schema with `minimum: 0` and an
    HTTP response. Consequence, stated so it is not a surprise:
    `prefix_stats(trie, …)` on a trie whose counts are already corrupt raises
    `ValueError` rather than returning a negative ratio.
15. **`aggregate` checks containment but not disjointness.** Stated as a
    precondition in decision 2. Checking it properly means enumerating
    addresses; checking it partially (pairwise prefix disjointness) would
    wrongly reject the per-shard case, where several parts legitimately carry
    the *same* prefix from different owners.
16. **`prefix_stats` and `ancestor_stats` take a `HotTrie` and live here rather
    than on the trie.** Putting them on the trie would make the structure
    package import a `PrefixStats` it has no use for; ADR-0014 decision 2
    deliberately returns bare `PrefixCount` pairs.
17. **The record map is a read-only `Mapping` with two mutators, not a
    `MutableMapping`.** `records[ip] = doc` would be a documented way to break
    §46.5 without touching the trie.
18. **Stored documents are deep copies; reads are shallow read-only views.**
    (Revised: an earlier draft stored shallow copies and put nested mutation
    "out of contract".) Once the map promises that every stored record passed
    §46.2, that promise cannot depend on callers leaving nested values alone —
    a later `append` to a nested `x_` list could push a stored record past 1024
    bytes. A validated document is at most 1024 bytes of JSON-model values and
    transitions are rare (§7), so a deep copy per write costs nothing that
    matters. Reads are not copied: a `MappingProxyType` over the stored top
    level stops the obvious mistake, and the readers are the trie service's own
    query and snapshot code, not an untrusted party. Deep-freezing (tuples for
    lists) was rejected because a read-back would then no longer equal the
    document that was stored.
19. **`attributes=None` normalizes to `DEFAULT_ATTRIBUTES`; `{}` is
    rejected.** (Revised: an earlier draft stored `{}` verbatim.) `None` is the
    event's "absent" (§46.5). An empty dict is a document missing the schema's
    required `attributes_version`, so under decision 5 it is an
    `InvalidAttributesError` like any other invalid document; there is no
    longer an exception to "never store what §46.2 rejects".
20. **Where the one copy of the rules lives: a new public function in
    `hammertime-core`, not a public name inside the codec, and not in the trie
    package.** (Replaces an earlier draft's "no validation in the store".) The
    owner's ruling fixes *that* the store validates; the placement is mine. The
    trie package cannot own the rules, because the codec must use them too and
    `hammertime-core` may not depend on a service. Making `_validate_attributes`
    public inside `codec.py` would work, but it would make the record map import
    the envelope codec to check a document that has nothing to do with an
    envelope, and would leave the rules raising `CodecError` in a place where no
    codec is involved. A module of its own, `hammertime.core.events.attributes`,
    sits beside the codec (the document is carried on hot-ip events, §46.5), is
    importable by both, and is the obvious place a future registered name
    (§46.3's `sources`) gets enabled. It is not re-exported from
    `hammertime.core.events.__init__`: nothing needs the shorter path, and
    `hammertime.core.events.attributes` is the one import path the tests and
    the store use.
21. **`ip_attribute_bytes` is maintained by the map, after all.** (Reverses an
    earlier draft, which deferred it for two reasons that no longer hold: it
    would have put `json` in the store, and it would have added an exception
    path after the trie was mutated.) `validate_ip_attributes` must serialize
    the document to enforce S6 anyway, it returns that size, and decision 6 runs
    it before the trie is touched — so the store gets an exact per-record size
    with no second serialization, no `json` import and no new failure point.
    The name `serialized_bytes` describes the quantity; the §46.8 metric name
    `ip_attribute_bytes` is the obvious alternative (ADR-0014 assumption 3 chose
    metric names for `hot_ip_count`) and I did not take it only because
    `records.ip_attribute_bytes` repeats its own receiver. Renaming is
    mechanical.
22. **No iteration order is promised for the record map** (it is a dict, so
    insertion order in practice). §46.5's checks are set-based. A snapshot that
    needs determinism sorts, or writes in `trie.iter_hot_addresses()` order,
    which ADR-0014 decision 2 already promises is ascending.
23. **The coupled step lives in this epic rather than in the worker.** The
    alternative — the worker calls `add_hot_ip` and `records.record` itself —
    leaves §46.5 as a convention a reviewer has to spot at the call site, and
    leaves the validate-then-trie-then-record ordering (decision 6, clauses
    2-4) undiscoverable; now that validation can fail, calling `record()` after
    `add_hot_ip` is precisely the order that breaks §46.5. As two pure
    functions with no bus, no clock and no sequence number, they constrain
    nothing about how the worker is built.
24. **They are functions, not a class that owns both maps.** A `TrieState`
    object holding trie, records and metadata would be the worker epic's
    aggregate root, and designing one here would be designing the worker.
25. **`request_count` is not stored by this epic.** `read-api-v1.md`'s
    `GET /ip/{addr}` returns the `window_count` of the IP's most recent
    `HotIpAdded`, which has exactly the attribute record's lifetime but is not
    part of the `IpAttributes` document (§46.3 registers `attributes_version`
    and `weight` only, and `weight` is in thousandths of `hot_threshold`, not a
    raw count). ADR-0005 and §46.5 both name the map `ip -> IpAttributes`, so I
    kept it to that. The query epic therefore needs a decision: widen this
    record (an amendment here) or hold a second per-IP map with the same
    lifetime (two things to keep in step). I recommend the amendment, and
    flag it rather than pre-empting it.
26. **The local metadata store is excluded from the §33 snapshot** (decision
    8). It follows from the store holding configuration rather than derived
    state, but nothing said so, and a snapshot epic could reasonably have
    assumed "everything in the trie service's memory".
27. **No `CHANGES` entry for this epic.** Nothing a deployment can observe
    changes: the modules have no caller until the trie service's worker and
    read API land, there is no wire format, no config key and no schema change.
    The codec refactor in decision 5 does not change that. Its error type is
    unchanged. What it accepts from the wire tightens in one place only —
    S5 now rejects a lone UTF-16 surrogate in a *key* (assumption 34), a string
    that cannot be encoded as UTF-8 at all and that the codec already rejects
    in a value — and the hot-ip stream is internal, written only by this
    project's aggregator, whose `hammertime.core.state.weight` emits exactly two
    integer fields. The newly rejected encode-path inputs (a `tuple`, a
    non-`str` key) are likewise ones no shipped producer builds. Reworded error
    messages are not a behaviour anyone can depend on. Per `CLAUDE.md`'s "if you are unsure whether a change qualifies,
    it does not". The entry belongs to the change that makes the trie service
    do something. (Same reasoning as ADR-0014 assumption 25, and recorded here
    so the implementing change does not have to re-derive it.)
28. **No schema changes.** `schemas/ip_attributes.v1.json` and
    `schemas/hot_ip_event.v1.json` are unchanged and were re-read for this
    design; `validate_ip_attributes` implements the attribute schema's rules
    and the record map consumes them, and neither authors anything in
    `schemas/`. S4's data-model rule is what the schema's JSON types already
    mean once a document can arrive as Python objects rather than JSON text.
29. **`Tags.of(*names)` exists as a convenience constructor.** Pure ergonomics
    — `Tags.of("internal")` against `Tags(frozenset({"internal"}))` — and one
    line.
30. **The trie tests go in the one file the epic names; the shared validator
    gets its own core test file.** Everything in the trie package is
    deterministic and in-process, so the hypothesis-driven law checks
    (associativity, commutativity, identity) sit in
    `services/trie/src/hammertime/trie/tests/test_metadata.py` alongside the
    worked examples, rather than in `tests/property/`; if that file gets
    unwieldy, splitting it is a later, mechanical change.
    `validate_ip_attributes` is a `hammertime-core` function with callers in
    two packages, so its direct tests belong beside it, in a new
    `packages/hammertime-core/src/hammertime/core/tests/test_attribute_validation.py`.
    The existing `test_ip_attributes.py` is deliberately left untouched: it
    exercises every §46.2 rule through the codec, and passing it unmodified is
    the evidence that moving the rules out of the codec changed nothing the
    codec accepts or rejects.
31. **`InvalidAttributesError` is a new `HammertimeError` subclass — neither a
    `ValueError` nor a `CodecError`.** It follows `InvalidAddressError` and
    `InvalidPrefixError`: bad *input*, as opposed to a bad *call*. Not a
    `ValueError`, because the worker must tell a rejected document (apply the
    transition with the default document) from a family mismatch (a routing
    bug), and a subclass relationship would let one `except ValueError` swallow
    both. Not a `CodecError`, because a record-map rejection involves no
    envelope, and `CodecError`'s own docstring defines it as a failure to
    encode or decode one.
32. **The codec keeps raising `CodecError`, now chained `from` the
    `InvalidAttributesError`.** That preserves its module contract ("every
    failure mode surfaces as `CodecError`") and every existing test, and it
    gives the worker a way to recognise an attributes rejection at decode —
    `isinstance(exc.__cause__, InvalidAttributesError)` — without parsing a
    message (assumption 37). Routing the explicit-`null` case through the
    validator as well is my call, made so that the signal has no hole; it
    changes nothing observable except the chained cause, since the result was
    a `CodecError` either way. The messages may be reworded: no test pins them
    (checked — neither `test_codec.py` nor `test_ip_attributes.py` passes
    `match=` or names the codec's private helpers).
33. **S4: only the JSON data model is accepted.** Neither §46.2 nor the schema
    says so, because both were written for JSON text, where it is automatic; it
    stops being automatic the moment a document can arrive as Python objects,
    which is decision 5's whole point, and §46.8's determinism is the reason to
    enforce it. The test is `isinstance`: a subclass of `str`, `int`, `float`,
    `dict` or `list` passes as its base type, and `bool` passes as a JSON
    boolean but not where S3 or R2 need an integer. I accepted that looseness
    rather than demand exact types, which would reject harmless subclasses
    such as an `IntEnum` value. On the codec's encode path this newly rejects a
    `tuple` (previously written as an array) and a non-`str` nested key
    (previously coerced to a string); a `set` was already rejected.
34. **S5 covers keys and S7 covers cycles.** The codec's walk checked string
    *values* for lone surrogates; a key can carry one just as well, including
    on the wire (decision 5), and is just as unencodable as UTF-8. A
    self-referencing document can only be built in-process; it would otherwise
    surface as a `RecursionError` from the walk or a `ValueError` ("Circular
    reference detected") from `json.dumps`, and S7 makes both an
    `InvalidAttributesError`.
35. **S6 measures the compact, ASCII-escaped encoding, and the validator
    returns that number.** §46.2 says "serialized size <= 1024 bytes" without
    naming a serialization. The codec has always measured
    `json.dumps(..., separators=(",", ":"))` with the default
    `ensure_ascii=True`, which is also what `encode()` puts on the wire, so a
    non-ASCII character costs six bytes (twelve outside the Basic Multilingual
    Plane). I kept that as the definition — changing it would move the cap
    under existing producers — and made the function return it, so that the
    cap and §46.8's byte total can never disagree about what a document weighs.
36. **Messages never contain a document value, and quote at most the first 64
    characters of a key.** §46.9 makes values opaque, and the codec already
    redacts `attributes` from its malformed-scalar message for this reason, but
    its validator's own messages echoed values (`got {weight!r}`). Beyond the
    flooding risk, `repr` of a sufficiently large Python integer raises
    `ValueError` (CPython's integer-string conversion limit), which would turn
    a rejection into a crash of the error path. 64 is my number: it shows every
    name the `x_` grammar can produce (at most 50 characters) in full and bounds
    everything else.
37. **`attributes_rejected` (§46.8) is counted by whoever handles a rejection
    inside the trie service — never by the validator, the codec or the record
    map.** §37 lists it among the *trie* metrics, and the codec is shared by
    every service, so the codec cannot own it. `hammertime-core` and this
    package have no metrics dependency and stay pure functions of their inputs,
    which is also what lets them be tested without a registry. Only the handler
    knows that the rejection happened in the trie service and that it was
    final. Concretely: the worker (#10) counts an `InvalidAttributesError` from
    `apply_hot_ip_added`, and a `CodecError` on a hot-ip message whose
    `__cause__` is an `InvalidAttributesError` (assumption 32); the snapshot
    epic counts a document `record()` rejects while it is loading. One owner per
    path means nothing is counted twice, which is also why the record map keeps
    no rejection counter of its own.
38. **A rejected document is replaced by the default, not by whatever record it
    would have replaced.** That the transition is still applied is not a choice:
    §46.1 forbids any attribute from influencing `hot_count`. How it is applied
    is: decision 6 has the worker call `apply_hot_ip_added` again with
    `attributes=None`, which for an address already HOT with a valid record
    replaces that record with `DEFAULT_ATTRIBUTES` rather than keeping it. §46.5
    makes the record the attributes of the most recent `HotIpAdded`, and a
    replay of the same log must reach the same record whatever was stored
    before; keeping the older one would make the result depend on history the
    event does not carry. I routed the recovery through a second call, rather
    than a flag on `apply_hot_ip_added` or a richer return value, so that the
    function stays strict and all-or-nothing and counting and logging stay with
    the caller (assumption 37).
39. **Reads raise `ValueError` for an address of the other family.** A
    `Mapping` conventionally answers "absent" for a key it does not hold, and
    `KeyError` / `False` / `None` would be defensible here — an IPv6 address is
    never HOT in an IPv4 trie. I followed ADR-0014 decision 1 instead ("a query
    is as much a routing bug as an update"), so that the record map and the trie
    it mirrors reject the same calls. The cost is that `ipv6_address in
    ipv4_records` raises rather than returning `False`, because
    `Mapping.__contains__` and `get` only absorb `KeyError` — which is the
    point. `check_attribute_records` and `assert_attribute_records_match` only
    iterate and take `len`, so neither is affected.
40. **Validation runs once per write, and the step after the trie mutation
    cannot fail.** Nothing dictated how `apply_hot_ip_added` avoids validating
    twice. The contract is what decision 6 states; the obvious implementation is
    a module-private prepare/commit pair on `IpAttributeRecords` that `record()`
    and `apply_hot_ip_added` both use — prepare (normalize, validate,
    deep-copy, size) may raise and changes nothing, commit (assign, adjust the
    byte total) cannot raise. The names and shape are `coder`'s; only those two
    properties are contract.
41. **The producer's generation and the validator's highest known version stay
    two constants.** `hammertime.core.state.weight.ATTRIBUTES_VERSION` is the
    generation the aggregator writes; the validator's highest known version is
    the generation this build can interpret. Both are 1, and before this change
    the second already lived separately in the codec. They mean different
    things even if they will usually move together, so I did not merge them; a
    reviewer who prefers one constant can have `weight.py` import the
    validator's, a one-line follow-up outside this epic.
42. **A `HotIpAdded` whose document the codec rejects is lost whole, and this
    ADR does not change that.** On decode, invalid `attributes` make the entire
    envelope a `CodecError` (shipped behaviour since #45, pinned by
    `test_ip_attributes.py`), so the transition never reaches
    `apply_hot_ip_added` and decision 6's recovery path never runs. By §46.1's
    letter that is an attribute influencing `hot_count`. It cannot come from
    this project's own producer, which validates with the same function before
    it publishes, so it takes transport corruption or a foreign producer to
    happen — but it is a real asymmetry with decision 6. Resolving it (for
    instance, decoding the event and reporting its document as rejected instead
    of failing the envelope) is a change to the codec's contract and its tests,
    and needs its own decision; it is listed as open under Consequences.
43. **`prefix in store` raises for a `Prefix` of the other family**
    (Amendment 1 ruling 2). Decision 4 stated the family rule without
    exception, and this confirms that reading rather than choosing a new one;
    it is recorded because the question was a fair one. `False` — what a
    container conventionally answers for something it does not hold — is
    defensible, since an IPv6 prefix is never declared in an IPv4 store. I
    kept the `ValueError` because assumption 8 scopes the store per family
    precisely so that a foreign prefix is caught rather than read as "nothing
    declared"; because decision 5 and assumption 39 made the same call for
    `a in records`; and because a store that raised from `local(p)` but not
    from `p in store` would reject one read and answer another about the same
    prefix. The cost is assumption 39's: a caller holding a prefix of unknown
    family checks `prefix.family` before asking.
44. **A document `declare` cannot store is a `TypeError`, checked after the
    family and before anything changes, by each value's kind rather than its
    contents** (Amendment 1 ruling 3). The ADR fixed the document's type
    (`Metadata`) and that the family is checked first, and nothing else about
    a bad document. Refusing a bare value rather than wrapping it is decision
    3's rule, not a new call. The rest is mine:
    * *`TypeError`, not `ValueError`.* The argument is of the wrong type, and
      Python's own convention is `TypeError` for that and `ValueError` for a
      right-typed argument with a bad value — the side `Bitmask(-1)` and all
      of ADR-0014 assumption 18's `ValueError`s fall on. It also lets a caller
      or a test tell a malformed document from the family `ValueError` (a
      routing bug) by type rather than by message. Decision 3's mixed-kind
      `ValueError` is a different case: there each value is a valid kind, and
      the fault is a conflict between two declarations.
    * *Neither `InvalidAttributesError` nor a new `HammertimeError`.* The
      former is the error for §46.2's wire-borne document, and metadata has
      no wire form. A domain error type would signal bad *input*; until a
      declaration source exists (assumption 7) every caller is in-process
      code, so a malformed document is a bad *call* — and whatever source is
      designed later has to parse its own format before it can build a value
      at all.
    * *All-or-nothing, checked on the copy that is stored.* Nothing required
      it; it mirrors `record()` (decision 5), so every stored declaration is
      one that passed the check and a refused call leaves no trace.
    * *Kind, not contents.* Each kind's rules live in one place, where the
      value is built, rather than being repeated by the store.
    * *`None` is refused rather than read as "empty".* `record()` has a
      default document because §46.5 and `read-api-v1.md` define one; nothing
      defines a default declaration, and assumption 10 already keeps "declared
      empty" (`EMPTY_METADATA`) distinct from "not declared".
    * *The message names the offending key when that key is a `str`.* Its
      wording is otherwise not part of the contract, and neither is which
      fault is reported for a document with several — the terms decision 5
      sets for the attribute validator.
45. **`__contains__` answers `False` for anything that is not a `Prefix`**
    (Amendment 1 ruling 4) — an `Address` of either family, a `str`, `None`,
    an unhashable object. Nothing specified it, and `__contains__` takes
    `object` (Python's container protocol), so it needs an answer for every
    object. `False` mirrors decision 5's "a key that is not an `Address` at all
    is simply absent", so the store and the record map treat a wrong-typed
    probe alike. The alternative, a `TypeError`, would catch an `Address`
    passed where a `Prefix` was meant; I did not take it because `in` asks
    about membership, and something that can never be a member is not a
    routing bug the way a right-typed key of the wrong family is
    (assumption 43). An unhashable argument answers `False` as well, not with
    the `TypeError` a bare `dict` lookup would raise, so the type test comes
    before any lookup. Reading an `Address` as its host route was rejected: it
    would give `in` a meaning no other store method has.
46. **Each value type checks its field's type when it is built — `TypeError`
    for a wrong type, `ValueError` only for `Bitmask(-1)`** (Amendment 1
    ruling 5). Decision 3 and assumptions 3 and 5 gave the types and one range
    rule, not how the types are enforced. My calls:
    * *At construction, not at `declare` or `combine`.* Each value is checked
      once, where it is made, so whatever holds a `Tags`, `Bitmask` or
      `Override` holds a valid one, and `declare` and `combine` need only ask
      which kind a value is (rulings 3 and 7).
    * *`TypeError`*, by the convention assumption 44 sets out.
    * *`Tags.values` must already be a `frozenset`, not any iterable of
      `str`.* Coercing would read `Tags("abc")` as the three tags `a`, `b` and
      `c`, and a mutable `set` inside a `Tags` would make decision 4's
      "immutable copy" of a document mutable after all, since the copy shares
      its values. `Tags.of(*names)` is the convenience for everything else.
      No grammar applies to a member, as none applies to a key
      (assumption 6).
    * *`Bitmask` refuses a `bool`*, as the §46.2 rules do where an integer is
      meant (S3, R2; assumption 33): a truth value is not a mask, and
      `Bitmask(True)` would otherwise compare equal to `Bitmask(1)`.
    * *`Override` accepts exactly assumption 3's types*, subclasses included
      — an `IntEnum` or `StrEnum` member passes, as assumption 33 lets
      subclasses through S4 — and refuses a `float` because the declared type
      has none.
47. **Two kinds for one key on one path are refused at `declare`, rather than
    left to fail at read** (Amendment 1 ruling 6). This changes what the text
    implied: `declare` accepted each well-formed document on its own, and
    every read through two declarations that gave a key different kinds
    raised decision 3's `ValueError`. Decision 3 calls that "a declaration
    error", and left there it would surface at every later read of a subtree
    — in a query, far from the declaration and from whoever could fix it —
    rather than at the one call that made it. The alternative, keeping the
    implied behaviour, is defensible for a store nothing populates yet
    (assumption 7) and adds no code; I chose the refusal so that the query
    epic inherits a store whose reads fail only for a routing bug. The rule is
    path-scoped because that is exactly the set of stores whose reads can
    fail; a store-wide "one kind per key" rule would be simpler to check but
    would refuse declarations no read ever combines, and it is a registry by
    first use, which assumption 2 declined. It costs a pass over the
    declarations per `declare` — cheap for assumption 9's hand-written map —
    and nothing per lookup. Naming the conflicting prefix in the message is
    what lets an operator find the other half of the conflict. Nothing running
    changes, since the store has no caller.
48. **The combine functions refuse a non-kind with a `TypeError`, check both
    documents whole, and check shape before combining** (Amendment 1 ruling
    7). Nothing specified what they do with a malformed operand. `TypeError`
    follows assumption 44. Checking every entry of both documents, not only
    the keys they share, means a malformed entry cannot pass through
    `combine` into a result that looks valid; the union already visits every
    entry, so the check costs nothing that matters. Checking shape first makes
    the exception's type independent of iteration order when a pair is both
    malformed and mixed-kind. `combine_path` is not required to check every
    document before it folds, because it takes any iterable, a generator
    included, and would have to materialize it to do so.

## Consequences

* Epic #9 becomes implementable against a fixed surface: four modules, three
  value kinds, three combine entry points, one metadata store, one record map,
  two coupled apply functions, one `PrefixStats` view — and, in
  `hammertime-core`, one public validator and one error type that the codec and
  the record map share.
* **ADR-0005 decision 5 and ADR-0014 decision 9 are discharged**, not
  departed from: the trie validates shape and size before storing, with the
  same rules the codec applies, and no copy of those rules exists outside
  `hammertime.core.events.attributes`.
* **`hammertime-core` changes shape but not wire behaviour** — apart from S5's
  surrogate-in-a-key tightening (assumption 27): `events/attributes.py` is new,
  `errors.py` gains `InvalidAttributesError`, and `events/codec.py` loses its
  private copies of the §46.2 rules and calls the shared function instead.
  `test_ip_attributes.py` must pass unmodified.
* Both acceptance criteria map to statements a test can assert without reading
  the implementation: (1) `aggregate` is order-independent, associative under
  regrouping and has an identity, and `combine` is associative/idempotent with
  an identity — commutative exactly for `Tags` and `Bitmask`; (2) `local(P)` is
  unchanged by every mutation of the trie beneath `P`, including the removal
  that prunes `P`'s node, and by declarations on ancestors and descendants.
* **ADR-0014 assumption 21's open half is closed for `local_metadata`**: it
  lives in `metadata/local.py`, keyed by `Prefix`, outside the structure
  package. `prefix_state` remains open and remains the query epic's.
* **The worker epic (#10)** gets `apply_hot_ip_added` / `apply_hot_ip_removed`
  as the one way to apply a hot-IP event to the pair, and
  `ancestor_stats(trie, ip, min_length=…)` for ADR-0010 decision 3's 25
  `PrefixStatsChanged` messages. It inherits decision 6's constraints on the
  three exceptions — above all, that an `InvalidAttributesError` is answered by
  applying the transition with the default document, never by dropping it —
  and owns `attributes_rejected` for the event path (assumption 37). It still
  owns everything about *when* and *whether* to emit, the trie's
  `event_sequence`, family routing, and the mechanism of the response to an
  `InvariantViolation` (ADR-0014 A12 clause 5).
* **The metrics epic** reads §46.8's `ip_attribute_records` as `len(records)`
  and `ip_attribute_bytes` as `records.serialized_bytes`, both O(1).
* **The query epic** gets `PrefixStats` for `GET /prefix/{cidr}` and
  `matched_prefixes`, and `IpAttributeRecords.get(ip)` for §46.7's `attributes`
  on `GET /ip/{addr}`. It still owns `evaluate_prefix_state`
  (`hammertime.core.state.prefix`, which does not exist yet), any caching of
  `prefix_state`, and the `request_count` decision of assumption 25.
* **The snapshot epic** must persist the record map (§46.8) and must not
  persist the metadata store (decision 8). Every document it restores is
  validated by `record()` or `apply_hot_ip_added()` (decision 5); it decides
  what a rejection does to the load, and counts it in `attributes_rejected`
  (assumption 37).
* `check_attribute_records(trie, records)` and
  `assert_attribute_records_match(trie, records)` accept `IpAttributeRecords`
  with no change to either, which is the whole of ADR-0014 decision 9's
  promise coming due.
* No schema changes; no wire-format changes (S5's key rule narrows what decode
  accepts, but only by strings that cannot be encoded as UTF-8 in the first
  place); no new dependency; no `CHANGES` entry (assumptions 27, 28).
* Spec pointer notes added by this ADR: §9 (where `local_metadata` lives), §12
  (what is combined upward, and that `hot_ratio` is not), §16 and §17 (the
  prefix-keyed store and the two fold directions), §46.5 (the module that holds
  the record map, validates it and applies the coupled step), §46.8 (which
  object answers each metric, and who counts rejections) and §46.9 (validation
  on write; no values in messages). `docs/spec/README.md`'s index is updated
  for §16/§17, §46 and §46.5.
* **Open, and deliberately not settled here:** how an operator declares prefix
  metadata, and whether declarations are durable (assumption 7) — that is a
  config/protocol design of its own, and until it exists the store has no
  production caller; whether `request_count` joins the per-IP record
  (assumption 25); and whether a `HotIpAdded` whose document fails validation
  at decode should still deliver its transition, which would change the
  codec's contract (assumption 42).

## Amendment 1 (2026-09-23) — the three gaps `test-author` hit writing epic #9's tests, and four more found while ruling them

Why: `test-author`, writing
`services/trie/src/hammertime/trie/tests/test_metadata.py` from this ADR
alone, reported one statement that was false, one rule whose reach was
unclear and one behaviour the ADR left open, each with the reading its tests
had taken (rulings 1-3). Ruling those turned up four more points the ADR left
to whoever implemented it — `__contains__` given something that is not a
`Prefix`, how the value types enforce their field types, one key with two
kinds on one path, and the combine functions given something that is not one
of the three kinds — and the coordinating session asked for them to be ruled
here as well, so that `coder` is not left to guess (rulings 4-7). Each is
ruled in place with a dated note, and every edit is listed here with the
superseded wording quoted, following ADR-0009 Amendment 1's convention.
Ruling 1 corrects an illustration and ruling 2 confirms decision 4's literal
reading; rulings 3, 4, 5 and 7 state behaviour the ADR had left unstated;
ruling 6 changes one behaviour the text implied (assumption 47). No schema,
wire format or config key changes, and the store has no caller, so there is
still no `CHANGES` entry (assumption 27).

1. **Decision 2, first bullet: the worked example is corrected.** Was: "This
   is the mistake the type exists to prevent: a `/24` holding 128 HOT
   addresses is at 50 %, and with an empty sibling `/24` its parent `/23` is
   at 25 % — which is neither the sum nor the mean of its children's
   ratios." The mean of 50 % and 0 % is 25 %, and for the two equal halves of
   any prefix the mean of their ratios always equals the parent's. The bullet
   now keeps that example for what it does show (the sum is wrong), states
   the identity, and adds a `/24` at 128 beside a `/25` at 64 under a `/23`:
   37.5 %, against a sum of 100 % and a mean, plain or capacity-weighted over
   the parts, of 50 %. The bullet's first two sentences are unchanged, and so
   is the rule: ratios are recomputed, never combined.
2. **Decision 4, "Family-scoped" bullet: `prefix in store` raises.** One
   sentence and the dated note are appended: `__contains__` raises the
   family `ValueError` for a `Prefix` of the other family rather than
   answering `False`. Nothing already there changed. Assumption 43.
3. **Decision 4, new bullet: `declare` refuses a malformed document with a
   `TypeError`** — a non-mapping (`None` and a list of pairs included), a
   non-`str` key, or a value that is not a `Tags`, `Bitmask` or `Override` —
   after the family check, on the copy it would store, and before the store
   changes. Inserted after the bullet beginning "`declare` replaces any
   existing declaration"; nothing already there changed. Assumption 44.
4. **Decision 4, "Family-scoped" bullet: anything that is not a `Prefix` is
   absent from the store.** Two sentences and a dated note are appended after
   ruling 2's: `__contains__` answers `False`, and never raises, for an
   argument that is not a `Prefix` — an `Address` of either family included —
   and does not read an `Address` as its host route. Assumption 45.
5. **Decision 3, new paragraph after the one ending "not a merge": each value
   type checks its field's type when it is built.** `Tags.values` is a
   `frozenset` of `str`; `Bitmask.bits` an `int` that is not a `bool`, and
   `>= 0`; `Override.value` a `str` or an `int`, a `bool` included. A wrong
   type is a `TypeError` from the constructor; `Bitmask(-1)` stays a
   `ValueError`. Dated notes are appended to assumptions 3 and 5; nothing
   already in either changed. Assumption 46.
6. **Decision 4, new bullet after ruling 3's: `declare` refuses a key with two
   kinds on one path**, with a `ValueError` naming the key, both kinds and the
   conflicting prefix, after the family and document checks and before the
   store changes. `P`'s own replaced declaration is not compared, and
   prefixes neither of which contains the other may differ; reads then raise
   only the family `ValueError`. Before this, the text implied that such a
   declaration was accepted and that every read through both raised.
   Assumption 47.
7. **Decision 3, new paragraph after the one ending "what gives `Override` its
   meaning": the combine functions check shape first.** An operand of
   `combine_values` that is not one of the three kinds is a `TypeError`
   whatever the other is; `combine` checks both documents whole, by
   `declare`'s rule, before it combines any key, so a pair both malformed and
   mixed-kind is the `TypeError`; `combine_path` raises at its first failing
   step. Assumption 48.

Also: the status line, which read "Status: accepted", gained this
amendment's clause; assumptions 43 to 48 are new; and assumption 10, which
reads "`declare` stores whatever document it is given, including
`EMPTY_METADATA`." and on its own would now contradict rulings 3 and 6, is
left as written with a dated note appended: "whatever document" means any
well-formed document that gives no key a second kind on its path, and
`EMPTY_METADATA` is still stored as a declaration.
