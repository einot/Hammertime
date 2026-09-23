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
a reading or state what was unstated); amended again the same day (see
"Amendment 2" at the end — four findings of the post-implementation review
against decision 5 are ruled: the attribute validator reads a document once,
through the built-in types' own slots, into a canonical copy, and that copy —
never the caller's object — is what is checked, measured, stored and sent; its
work is bounded by the size cap before it reads anything; nothing but
`InvalidAttributesError` can come out of a document; and the record map keeps
each record as its canonical JSON text and decodes a fresh document on every
read, which reverses assumption 18's "Reads are not copied". Rulings A, B and
D change shipped code; ruling C is the contract they make true); amended a
third time the same day (see "Amendment 3" at the end — four low-severity items
from the re-review of Amendment 2's implementation are ruled. An integer in an
attribute document has at most 640 decimal digits (new rule S8), so every
stored record decodes whatever the interpreter's integer-string limit. Each
distinct container in a document is read at most once, and Amendment 2's
"bounded work" is narrowed to what is actually bounded. The codec's integer
fields accept JSON integers only, and the encoder writes no non-finite number;
the aggregator therefore skips, as malformed, an observation message with a
non-integer in such a field instead of applying a coerced value or, for an
infinity, stopping, which is this ADR's one `CHANGES` entry. `a in records`
answers without decoding the record)

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
from this module and names the seam. *(Widened 2026-09-23, Amendment 3 ruling
3: this ADR also rules how `hammertime.core.events.codec` converts its integer
fields and `capacity` on decode and what its encoder refuses. That defect
predates epic #9 and is not about attributes. It is ruled here because it
broke decision 5's "`CodecError` only" row, and no other ADR rules the codec's
scalar fields. Assumption 64.)*

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
  2026-09-23, Amendment 1 ruling 6; assumption 47.)* The prefix is named by
  its canonical text, `str(prefix)` — what `Prefix.__str__` renders: the
  network address, `/`, the length, as in `10.0.0.0/8` or `10.20.30.0/24`.
  The same holds for IPv6, whose address part is the `ipaddress` module's
  compressed form, as in `2001:db8::/32`. *(Clarified 2026-09-23, Amendment 1
  ruling 6; assumption 47.)*

### 5. The per-IP attribute records: a read-only `Mapping[Address, IpAttributes]`, family-scoped, validated on every write by the one shared §46.2 validator, never interpreted

```python
# hammertime.core.events.attributes         Spec: §46.2, §46.3, §46.9      (new)
class CanonicalAttributes(NamedTuple):                               # Amendment 2
    document: dict[str, object]   # exact built-in types only; a tree no caller has held
    text: str                     # its compact JSON encoding; ASCII only
    @property
    def size(self) -> int: ...    # len(text): the S6 size, in bytes

def canonicalize_ip_attributes(document: object) -> CanonicalAttributes: ...
    # Reads `document` once, through the built-in types' own slots, into a new
    # tree; applies the §46.2 rules below to that copy; returns the copy and its
    # text. Raises InvalidAttributesError for every violation, and no other
    # exception can be caused by the document.                    (Amendment 2)

def validate_ip_attributes(document: object) -> int: ...
    # canonicalize_ip_attributes(document).size — for checking and measuring.
    # Anything that stores or sends a document keeps the canonical copy.

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

    # Mapping — get, keys, items, values come from the ABC.              (Amendment 3)
    def __getitem__(self, address: Address) -> IpAttributes: ...
    def __contains__(self, key: object) -> bool: ...    # decodes nothing  (Amendment 3)
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
into a new module, `hammertime.core.events.attributes`, whose entry point is
`canonicalize_ip_attributes`; `validate_ip_attributes` is its measuring form.
Both enforcement points call `canonicalize_ip_attributes` and keep what it
returns — never their own argument: *(Amended 2026-09-23, Amendment 2 ruling A;
the superseded sentences are quoted there.)*

* **the codec**, on encode and on decode as today, translating the new
  `InvalidAttributesError` into its own `CodecError` with `raise … from exc`, so
  the codec's contract ("every failure mode surfaces as `CodecError`") and every
  existing codec test hold unchanged, and `CodecError.__cause__` tells a caller
  that the failure was an attributes rejection. Every rejection of a present
  `attributes` value goes through the validator — including an explicit
  `"attributes": null` on decode, which the codec today rejects inline and
  which is simply a document that is not a JSON object (S1) — so that
  `__cause__` identifies all of them and none is invisible to
  `attributes_rejected` (assumption 37). It puts the canonical document —
  not the payload's own object — into the envelope it encodes and into the
  event it decodes, so the bytes on the wire are the bytes that were checked
  *(added 2026-09-23, Amendment 2 ruling A)*;
* **the record map**, on every write, keeping only the canonical text and
  letting `InvalidAttributesError` propagate. *(Amended 2026-09-23, Amendment 2
  rulings A and D; was: "on every write, letting `InvalidAttributesError`
  propagate.")*

Nothing else in the repo may restate the 1024-byte cap, the 16-key cap, the
registered-name set or the `x_` grammar.

The rules. Where a rule goes beyond what the codec enforced before, the right
column says so; everything else is the codec's existing behaviour, moved:

```text
structural — every document, whatever its attributes_version
  S1  a JSON object: a dict, a subclass read through dict's own slots;
      any other Mapping is refused                                       schema "type": "object"
  S2  at most 16 top-level keys                                          schema maxProperties
  S3  attributes_version present, a JSON integer (an int that is not a
      bool, or an integral finite float), and >= 1                       schema required, minimum
  S4  every value at every depth is in the JSON data model — dict with
      str keys, list, str, int, finite float, bool, None; a subclass of
      dict, list, str, int or float is read and copied as its base
      type; the keys of one object are distinct as text; no tuple, set,
      bytes or other object                                              NEW (see below)
  S5  no str, key or value, at any depth, holds an unpaired UTF-16
      surrogate (U+D800-U+DFFF)                                          keys are NEW; values were checked
  S6  serialized size <= 1024 bytes, measured on the canonical copy as
        len(json.dumps(copy, separators=(",", ":")).encode("utf-8"))
      with json's default ensure_ascii — the compact form the codec
      writes; this is the size returned. A document is rejected as
      soon as a lower bound on it exceeds 1024 ("Bounded work" below)    §46.2; the return value is NEW
  S7  nesting too deep to walk or serialize, a self-referencing
      structure, and a shared subtree repeated past the cap are
      rejections — never a RecursionError or ValueError                  cycles are NEW
  S8  every integer, at any depth, has at most 640 decimal digits, the
      sign not counted: -10**640 < n < 10**640                           NEW (Amendment 3)

registry — only when attributes_version <= 1, the highest version this
build knows (§46.2: a higher version is stored and echoed verbatim and
is not interpreted)
  R1  every top-level key is registered (attributes_version, weight) or
      matches ^x_[a-z0-9_]{1,48}$ — so the reserved `sources` (§46.3)
      and a misspelt `wieght` are rejected                               schema properties, patternProperties
  R2  weight, if present, is a JSON integer (not a bool) in [0, 1000000]  schema weight
```

*(Rows S1, S4, S6 and S7 amended 2026-09-23, Amendment 2 rulings A and B; the
superseded rows are quoted there. Every rule applies to the canonical copy, not
to the caller's object.)* *(Row S8 added 2026-09-23, Amendment 3 ruling 1;
assumptions 61 and 62. 640 digits is the most that every legal CPython
configuration converts in both directions, so a stored record can always be
decoded again. The check is an exact comparison on the copied integer, made
after the bit-length check of "Bounded work", and never converts the
integer to text.)*

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

**One read, into the copy that is used** *(added 2026-09-23, Amendment 2
ruling A; assumptions 49-53)*. `canonicalize_ip_attributes` reads the document
exactly once and builds a new tree of exact built-in types from it. It decides
what each value is from `type(value)`, tested with `issubclass` against `dict`,
`list`, `str`, `int` and `float` (`bool` and `None` are exact: neither can be
subclassed). It reads a subclass instance only through the base type's own slot
functions — `dict.__len__` and `dict.items`, `list.__len__` and
`list.__iter__`, `str.__len__` and `str.__str__`, `int.__int__` and
`int.bit_length`, `float.__float__` — and never through `isinstance` (which
consults an overridden `__class__`), `len()`, iteration, comparison, hashing,
`repr`, or any method, property or attribute the value's own class defines. A
key is read the same way, so a key cannot pass the registry as one name and be
serialized as another; two keys of one object whose copies are equal text are a
rejection (S4). The rules are then applied to the copy, the compact text is
produced from the copy, and `CanonicalAttributes(document=copy, text=text)` is
returned. The codec sends that copy and the record map stores that text;
nothing downstream reads the caller's object again, so no value can show one
content to the checks and another to the serializer or the store. For
exact-type input — everything `json.loads` produces, and everything this
project's producer builds — the copy equals the input, and acceptance, size
and wire bytes are unchanged.

**Bounded work** *(added 2026-09-23, Amendment 2 ruling B; assumption 54)*.
While it reads, the function keeps a running total that never exceeds the
compact size of what it has read so far, and rejects the document under S6 the
moment the total passes 1024:

1. Every value read adds at least its minimal compact size: 1 for `null`,
   `true`, `false` or a number; `n + 2` for a string of `n` characters, key or
   value; 2 for a list's brackets or a dict's braces, plus a comma between
   neighbouring items or entries and a colon per entry.
2. Before any string, list or dict is read, its length — measured by the base
   type's own `__len__` — is checked. If the running total plus that value's
   minimum already exceeds 1024, the document is rejected without the value
   being read. The minimum is `n + 2` for a string, `2n + 1` for a list of
   `n >= 1` items, `5n + 1` for a dict of `n >= 1` entries, and 2 for an empty
   list or dict.
3. An `int` is rejected without being converted to text when its bit length
   alone shows that its decimal form would push the total past 1024.
4. The read is iterative: it holds an explicit stack of pending containers and
   never recurses per nesting level.
5. Each distinct source container — a `dict` or a `list`, by identity — is
   read at most once. A container met again after it has been read in full is
   not read again: its canonical copy is copied in its place. The copy is a
   fresh tree, so the canonical document never shares one object between two
   positions, and the running total grows exactly as if the container had been
   read again. A container met again while it is still being read is a cycle,
   rejected under S7. *(Added 2026-09-23, Amendment 3 ruling 2; assumption 63.)*

Each figure under-counts the real compact size, so no document whose compact
encoding is at most 1024 bytes can be rejected by the bound; the exact size is
still measured afterwards, on the copy's text. The consequences are that at
most 1024 values are ever read, whatever the input's size or shape. A cycle,
a subtree shared by many branches, `[0] * 10**8` or a 50 MB string is rejected
after O(1024) work. Nesting cannot pass 512 levels, since each level costs two
bytes, so neither the iterative read nor the final (recursive) serialization of
the copy can meet an unbounded depth. And no rejection depends on CPython's
integer-to-string conversion limit.

**What the cap does not bound** *(added 2026-09-23, Amendment 3 ruling 2;
assumption 63)*. The cap bounds what is *read*. It does not bound one other
cost: reading a `dict` means passing over its entry table, and that table can
hold far more slots than the dict has live entries. CPython keeps the slot of
a deleted entry until the dict is next resized, and iteration steps over it.
One call's work is therefore O(1024) values read and copied, plus one pass over
the entry table of each *distinct* dict in the document. Rule 5 makes it one
pass however many times the dict is shared; without it the pass repeated with
every occurrence. The pass is proportional to memory the caller has already
allocated for that dict, not to the document's size. No read through the
dict's own slots can skip it, and refusing a dict for its allocation history
would reject documents §46.2 accepts.

**What can come out** *(added 2026-09-23, Amendment 2 ruling C; assumptions 55
and 56)*. Because nothing a document's own types define is ever called, the
document itself can cause no exception but `InvalidAttributesError`. Per
function:

```text
canonicalize_ip_attributes, validate_ip_attributes
    return, or raise InvalidAttributesError
codec.encode, codec.decode
    CodecError only, as before; an attributes rejection is a CodecError whose
    __cause__ is the InvalidAttributesError
IpAttributeRecords.record
    ValueError (the other family, checked first) or InvalidAttributesError
records[a], records.get(a), a in records
    ValueError (the other family); KeyError from records[a] when absent
IpAttributeRecords.discard
    ValueError (the other family)
apply_hot_ip_added
    ValueError (families) or InvalidAttributesError, both before the trie is
    touched; InvariantViolation from a corrupt trie (ADR-0014 A12)
apply_hot_ip_removed
    ValueError (families)
```

There is **no blanket `except Exception`**: nothing converts an arbitrary
exception into `InvalidAttributesError`. `MemoryError` and the
`BaseException`s that are not `Exception`s (`KeyboardInterrupt`, `SystemExit`)
pass through unchanged. The one narrow conversion kept covers the final
serialization of the canonical copy: a `RecursionError` or `ValueError` from
`json.dumps` there becomes an `InvalidAttributesError`. On an exact-typed,
finite, bounded copy, that can only mean an interpreter configured with an
integer-to-string limit below what S6 admits, or a caller already at the edge
of the stack. *(Amended 2026-09-23, Amendment 3 ruling 1: with S8 the first
case cannot arise, because every legal limit converts every integer S8 admits.
The `ValueError` half of the conversion stays as a backstop.)*

**Three rows that hold because of Amendment 3** *(added 2026-09-23, Amendment 3
rulings 1, 3 and 4)*:
* `records[a]` cannot raise the integer-string limit's `ValueError` on a
  stored record, because S8 admits only integers that every legal
  configuration parses. `ValueError` from a read therefore still means only
  "the other family".
* `a in records` answers from the stored keys without decoding anything.
* The codec's "`CodecError` only" holds for `decode` over every field of the
  bytes it is given, not just `attributes`: an integer field now accepts only
  a JSON integer, so a non-finite number can no longer escape its conversion
  as an `OverflowError`. On `encode`, an integer field that does not hold an
  `int`, a `capacity` that cannot be written as a decimal string, and a
  non-finite float anywhere are each a `CodecError` too (ruling 3).

**An `InvalidAttributesError` message never contains a value from the
document.** It names the rule broken and, where that helps, the offending key —
at most its first 64 characters. §46.9 makes values opaque, the codec already
redacts `attributes` from its malformed-scalar message for the same reason, and
an echoed value is exactly how an oversized `x_` payload or a 5,000-digit
integer (whose `repr` itself raises) would turn a rejection into a flood or a
crash. The order in which the rules are checked is not part of the contract; a
document that breaks several may be reported for any of them. A message is
composed only from the canonical copy, never from the caller's objects, so
nothing a subclass defines — a `__len__` that raises, a `__str__` that lies —
can run while a rejection is described. *(Added 2026-09-23, Amendment 2 ruling
C.)*

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
  check (`ValueError`); `None` selects the default document (§46.5, and
  `read-api-v1.md`'s "an absent `attributes` on the event is stored, and
  returned, as `{"attributes_version": 1}`"), so a record exists for every HOT
  address unconditionally and §46.5's count invariant needs no special case;
  anything else goes to `canonicalize_ip_attributes`, which refuses a
  document that is not a `dict` (S1 — `DEFAULT_ATTRIBUTES` itself included:
  pass `None` for the default) or that breaks any other rule, with
  `InvalidAttributesError`; and only then does the map change — the canonical
  text replaces any earlier record for that address (§46.5's replace-on-add)
  and `serialized_bytes` moves by the difference. Every step that can raise
  comes before the map changes, so a rejected call leaves the map, its length,
  its byte total and any earlier record for that address exactly as they were.
  *(Amended 2026-09-23, Amendment 2 rulings A and D; the superseded bullet is
  quoted there.)*
* **The map keeps each record as its canonical text, and decodes a fresh
  document on every read.** Per address it holds the compact JSON text that
  `canonicalize_ip_attributes` returned, and nothing else of the document.
  `records[a]` — and `get`, `values` and `items`, which go through it —
  decodes that text anew on each call and returns a read-only view
  (`MappingProxyType`) over the freshly decoded document. Two reads return
  distinct objects, every value in them is an exact built-in type, and nothing
  a reader does to a returned record, at any depth, reaches the map, its byte
  total or any later read. Replay determinism and §46.8's snapshot fidelity
  rest on it: what is stored is exactly what was checked, and nobody holds a
  reference into it. *(Amended 2026-09-23, Amendment 2 ruling D, which
  replaces the bullet "Stored records are private deep copies", quoted there;
  assumptions 57-59.)* Membership, `a in records`, is answered from the stored
  keys and decodes nothing: `False` for anything that is not an `Address`,
  the family `ValueError` for an `Address` of the other family, and otherwise
  whether a record is stored. *(Added 2026-09-23, Amendment 3 ruling 4;
  assumption 68.)*
* **`serialized_bytes` is §46.8's `ip_attribute_bytes`**: the total length of
  the stored canonical texts — each the size S6 measured — so a default record
  counts 24 bytes, the length of `{"attributes_version":1}`; kept up to date by
  every replace, `discard` and `clear`, and O(1) to read. §46.8's
  `ip_attribute_records` is `len(records)`. *(Amended 2026-09-23, Amendment 2
  ruling D; was: "the sum of the sizes `validate_ip_attributes` returned for
  the records currently stored".)*
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
  imports `json` only to decode its own stored text on read; it never
  serializes a document and states no rule — texts and sizes come from the
  validator. *(Amended 2026-09-23, Amendment 2 ruling D; was: "The module
  imports neither `json` nor the schema: sizes come back from the
  validator.")*

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
   it — `None` selects the default, anything else goes through
   `canonicalize_ip_attributes`, which raises `InvalidAttributesError` for a
   non-`dict` or any §46.2 violation, and the canonical text is kept —
   **before the trie is touched**. Validation runs once, here; clause 4 does
   not repeat it. *(Amended 2026-09-23, Amendment 2 rulings A and D; was:
   "`None` becomes `DEFAULT_ATTRIBUTES`, a non-`Mapping` or any §46.2
   violation is an `InvalidAttributesError`, the validated document is
   deep-copied and its size kept".)*
3. **The trie.** `trie.add_hot_ip(address)`. ADR-0014 Amendment 2 A12 allows it
   to raise `InvariantViolation` on an already-corrupt trie, and requires it to
   raise before mutating anything.
4. **The record.** The prepared text replaces any record for the address, and
   `serialized_bytes` moves by the difference. Nothing in this step can raise:
   everything fallible ran in clauses 1-3. *(Amended 2026-09-23, Amendment 2
   ruling D; was: "The prepared document replaces any record for the
   address".)*
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

No exception outside these three can be caused by the document (decision 5,
"What can come out"), so a worker built to this table meets every
document-caused failure in it — including one produced by a document whose
types override every method they have. *(Added 2026-09-23, Amendment 2 ruling
C.)*

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
  events/attributes.py   canonicalize_ip_attributes, CanonicalAttributes,
                     validate_ip_attributes — the §46.2 rules, once        (§46.2, §46.3, §46.9)   new
  events/codec.py    calls canonicalize_ip_attributes and sends the copy;
                     its private copies of the rules are removed           (§19, §32, §46.2)

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
cites ADR-0015. *(Layout rows for `events/attributes.py` and `events/codec.py`
amended 2026-09-23, Amendment 2 ruling A; they read "validate_ip_attributes —
the §46.2 rules, once" and "calls validate_ip_attributes; its private copies of
the rules are removed".)*

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
    document that was stored. *(Superseded 2026-09-23 by Amendment 2 ruling D
    in three places. The heading, "Stored documents are deep copies; reads are
    shallow read-only views.", is superseded: records are no longer stored as
    deep copies, and reads are no longer views over what is stored. The
    write-side claim, "a deep copy per write costs nothing that matters", is
    superseded: no deep copy is made on write, and the map keeps each record
    as its canonical text. "Reads are not copied" is superseded: it let a
    reader grow a stored record through a nested container while
    `serialized_bytes` stayed stale, and let a snapshot save a record no replay
    can reproduce. Records are now stored as canonical text and every read
    decodes a fresh document; the trade-off is assumption 57, and the
    deep-freeze argument above still stands.)*
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
    mechanical. *(Amended 2026-09-23, Amendment 2 ruling D: the per-record size
    is now the length of the stored canonical text, and the store does import
    `json` — to decode that text on read, never to serialize.)*
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
    so the implementing change does not have to re-derive it.) *(Qualified
    2026-09-23, Amendment 3: decode now tightens in two more places, S8 and
    the codec's integer fields, and the second changes what a running
    aggregator does with a malformed observation message. That one change
    carries a `CHANGES` entry; assumption 69 gives it and says why. What this
    assumption says about epic #9's own modules stands.)*
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
    *(Amended 2026-09-23, Amendment 2 ruling A. The test is no longer
    `isinstance`, which consults an overridden `__class__`, but `type()` with
    `issubclass`. A subclass is read only through its base type's own slots and
    is stored and sent as the base type — which "passes as its base type" had
    promised and the shipped code did not deliver: it validated through, and
    stored, the subclass objects themselves. Assumptions 50 and 51.)*
34. **S5 covers keys and S7 covers cycles.** The codec's walk checked string
    *values* for lone surrogates; a key can carry one just as well, including
    on the wire (decision 5), and is just as unencodable as UTF-8. A
    self-referencing document can only be built in-process; it would otherwise
    surface as a `RecursionError` from the walk or a `ValueError` ("Circular
    reference detected") from `json.dumps`, and S7 makes both an
    `InvalidAttributesError`. *(Amended 2026-09-23, Amendment 2 ruling B: a
    cycle — and a subtree shared by many branches, which is acyclic but
    exponential to walk — is now stopped by S6's running bound before it can
    repeat or recurse.)*
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
    everything else. *(Amended 2026-09-23, Amendment 2 ruling C: a message is
    composed from the canonical copy only, so a key's own `__len__` or
    `__str__` cannot run while the key is quoted.)*
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
    iterate and take `len`, so neither is affected. *(Amended 2026-09-23,
    Amendment 3 ruling 4: `in` no longer goes through `Mapping.__contains__`.
    The map's own `__contains__` raises the same `ValueError` on purpose
    (assumption 68), so the outcome is unchanged; `get` is as described.)*
40. **Validation runs once per write, and the step after the trie mutation
    cannot fail.** Nothing dictated how `apply_hot_ip_added` avoids validating
    twice. The contract is what decision 6 states; the obvious implementation is
    a module-private prepare/commit pair on `IpAttributeRecords` that `record()`
    and `apply_hot_ip_added` both use — prepare (normalize, validate,
    deep-copy, size) may raise and changes nothing, commit (assign, adjust the
    byte total) cannot raise. The names and shape are `coder`'s; only those two
    properties are contract. *(Amended 2026-09-23, Amendment 2 rulings A and D:
    prepare is now `canonicalize_ip_attributes`, and commit stores the
    canonical text.)*
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
    *(Added 2026-09-23, on the message's form: the prefix appears as
    `str(prefix)` for both families. That is the text `Prefix.parse` reads
    back, it is readable where `repr` would print the network as an integer,
    and a test can build the expected text with `str()` from the same `Prefix`
    instead of spelling it out. `Prefix.__str__` renders the network through
    `Address.__str__`, which uses Python's `ipaddress` for both families
    (`packages/hammertime-core/src/hammertime/core/addressing/address.py`,
    lines 55-59). For IPv6 that is lowercase hexadecimal without leading
    zeros, with the longest run of two or more zero groups — the first, on a
    tie — collapsed to `::`: read in the installed CPython 3.12 standard
    library, `/usr/lib/python3.12/ipaddress.py`, `_compress_hextets` and
    `_string_from_ip_int`, lines 1782-1853. The project requires Python
    `>=3.12`, and whether a later CPython renders any IPv6 address
    differently was not checked — one more reason for a test to compute the
    text rather than hard-code it.)*
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
49. **The canonical copy is made inside `hammertime-core`, once, for both
    callers; `validate_ip_attributes` stays as its measuring form** (Amendment
    2 ruling A). The findings offered this or a copy in each caller. Two
    callers would mean two copies of the one routine whose correctness is
    subtle — which reads are safe — and `codec.encode` had the same hole as the
    record map, so a caller-side fix would have to be made twice to be made at
    all. Keeping `validate_ip_attributes`, with the same contract and the same
    size for every exact-type input, spares every existing validator test; what
    it no longer offers is a licence to check a document and then keep the
    argument. No production caller may do that, and a reviewer can check it.
50. **Subclasses stay accepted; they are not refused** (ruling A). The
    alternative the findings name — exact types only — is just as safe once
    nothing reads a value through its own methods, and simpler to state. I kept
    assumption 33's "a subclass passes as its base type" because it was an
    accepted ruling that tests were about to pin (an `IntEnum` weight, a
    `str`-subclass key), and because the canonical copy finally delivers what it
    promised: the subclass never reaches the store or the wire. If the
    base-slot reads prove too subtle to keep correct, exact types only is a
    small change and should be made by amendment, not quietly.
51. **`type()` with `issubclass`, never `isinstance`; the slot functions named
    in decision 5 are the mechanism.** `isinstance(obj, cls)` falls back to an
    instance's `__class__` attribute when the direct type test fails, which is
    how `unittest.mock` makes mocks "pass `isinstance` tests"
    (`/usr/lib/python3.12/unittest/mock.py`, lines 1229-1230, and line 69: "can't
    use isinstance on Mock objects because they override `__class__`") — so a
    value can claim a type it does not have. `issubclass(type(v), dict)` asks
    the type object and runs nothing the value defines. The reference JSON
    encoder likewise reads a `dict` through its own `items()` and a `list`
    through its own iteration (`/usr/lib/python3.12/json/encoder.py`, lines
    297 and 354-356), which is why the shipped code's validation, sizing and
    copying could each see different content. That the slot functions
    (`dict.items(v)`, `list.__iter__(v)`, `str.__str__(v)`, `int.__int__(v)`,
    `float.__float__(v)` and the base `__len__`s) read the built-in storage and
    call nothing a subclass overrides is CPython's behaviour, whose C source is
    not installed here, so I did not read it. The contract is therefore stated
    as an observable — no method of the value's own class is called, and the
    copy holds exact built-in types — and the tests pin it with subclasses
    whose every override raises or lies.
52. **Two keys that copy to the same text are refused, not merged.** Only a
    `str` subclass with its own `__hash__`/`__eq__` can put two such keys into
    one `dict`. Keeping the last would silently drop an entry the caller
    supplied, and a JSON object as this project writes it cannot hold both. A
    rejection is the only answer that neither loses data silently nor invents a
    precedence rule.
53. **At the top level only a `dict` is accepted, and `record()` keeps its
    `Mapping[str, object] | None` annotation.** Reading any other mapping — a
    `MappingProxyType`, a user `Mapping` — means calling its own methods, which
    ruling A exists to avoid. The shipped `record()` copied any `Mapping` with
    `dict(...)`, so this is the one place the amendment refuses a document the
    old code accepted from an in-process caller. `DEFAULT_ATTRIBUTES` is a
    `MappingProxyType`, so passing it is refused too: it is the value a default
    record reads back as, and `None` is how a caller asks for one. The
    annotation stays `Mapping` because `dict` is invariant in its value type: a
    caller holding a `dict[str, int]` would otherwise fail type-checking on a
    call that is valid at run time. The run-time refusal is loud, and decision 5
    states it.
54. **The early bound under-counts by construction, and its figures are mine.**
    Each figure in "Bounded work" is the smallest compact encoding a value of
    that kind and length can have, so the bound can only reject documents whose
    real size is over 1024; tighter figures are allowed as long as they never
    over-count. Judging an `int` by `int.bit_length` before converting it avoids
    both the cost of converting a huge integer and CPython's integer-to-string
    limit (4,300 digits by default, adjustable with `PYTHONINTMAXSTRDIGITS`),
    whose `ValueError` the shipped code had to catch. One edge remains: an
    interpreter configured with a limit below the roughly 1,020 digits S6 can
    admit will reject such an integer through ruling C's narrow conversion —
    as `InvalidAttributesError`, never as a `ValueError`. *(Superseded
    2026-09-23 by Amendment 3 ruling 1: "One edge remains" no longer holds.
    S8 admits at most 640 digits, and no legal configuration converts fewer.
    Worse, the edge also struck reads: a record stored under a higher limit
    could not be decoded after the limit was lowered, a `ValueError` the table
    reserves for the family. Assumption 61.)*
55. **No blanket `except Exception`.** Once nothing a document defines is
    called, the only exceptions a document can cause are the rules' own. A
    catch-all would add nothing for documents. What it would do is turn a bug in
    the validator — a `TypeError` from a coding slip — into "document rejected",
    send the worker down decision 6's recovery path and replace the attributes
    with the default: the bug hidden, the data lost. Letting such a bug surface
    is the better failure. `MemoryError` and the `BaseException`s that are not
    `Exception`s mean the process is failing, not that a document is bad, and
    must not be answered by storing a default. The one narrow conversion — a
    `RecursionError` or `ValueError` from serializing the canonical copy — stays
    because both are conditions of the interpreter, which a document of legal
    shape can still meet. *(Qualified 2026-09-23, Amendment 3 ruling 1: since
    S8 a document of legal shape can no longer meet the `ValueError`. Every
    legal limit prints every integer S8 admits, and the copy holds no cycle
    and no non-finite float, so that half is a backstop only. The
    `RecursionError` half is unchanged.)*
56. **The threat model is data, not code.** The guarantees hold for any value
    an in-process producer can build — any nesting, sharing, cycle, size,
    subclass or override — and they hold because none of that value's code
    runs. They do not extend to a producer that is itself hostile code. Such a
    producer could mutate a document from another thread while it is read
    (§28 gives the trie one writer, and a document is read on that writer's
    thread), have a finalizer mutate it mid-read, or simply never return. No
    pure-Python validator can bound those, and §46.9's trust boundary —
    trusted internal producers — already excludes them.
57. **Canonical text with a decode per read, rather than copy-on-read or
    anything else** (ruling D). There were three candidates: (a) keep the
    canonical copy and return a deep copy on every read; (b) keep the
    canonical JSON text and decode it on every read; (c) keep objects and
    document nested read-only-ness, the shipped state that the finding showed
    to be unsafe. (b) wins on every axis that matters here:
    * *The hot path.* The single writer's `apply_hot_ip_added` never decodes.
      With (b) a write stores a string the validator has already produced, so
      writes get cheaper than the shipped deep copy, which (a) would keep.
    * *Read cost.* One `json.loads` of at most 1,024 bytes — microseconds — on
      `GET /ip/{addr}` and in the snapshot writer's pass, neither of which is on
      the writer's path. A `deepcopy` of the same document under (a) is not
      cheaper.
    * *Memory.* A record becomes its compact text, typically far smaller than
      the equivalent dict and list objects, so ADR-0005's "one bounded record
      per HOT IP (≤ 1 KiB serialized)" comes close to the literal resident size.
    * *Exactness.* `serialized_bytes` is the sum of the stored lengths and
      cannot drift from what is stored, because nothing is shared.
    * *Snapshot fidelity.* The stored form is the wire form, which is what
      §46.8 needs a snapshot to reproduce.

    The cost is that every read allocates a new document — which is the point.
58. **Reads still return a `MappingProxyType`, now over a fresh document.**
    Isolation comes from the freshness, not from the view: the reader owns its
    copy. The view is kept so that decision 5's read-only top level stays true
    for existing callers and tests, and so that `records[a]["weight"] = …` stays
    the loud mistake it was, rather than a silent write to a throwaway copy.
59. **The canonical text is a fixed point, so the snapshot epic needs no
    accessor.** For every text `t` the function returns,
    `json.dumps(json.loads(t), separators=(",", ":")) == t`, and canonicalizing
    a decoded canonical text returns the same text. Key order is kept, a finite
    float's `repr` round-trips, integers are exact, and `ensure_ascii`
    escaping is deterministic. A snapshot may therefore write `records[a]` back
    through the codec's serialization, or keep texts, and either way a load
    reproduces the stored bytes. I added no public accessor for the raw text:
    nothing needs one yet, and it would widen the map's surface. *(Amended
    2026-09-23, Amendment 3 ruling 1: as written this held only while the
    interpreter's integer-to-string limit stayed where it was when the text was
    produced. S8 removes that condition: every integer in a stored text has at
    most 640 digits, and every legal configuration parses and prints those.)*
60. **Still no `CHANGES` entry** (assumption 27). For exact-type input — all
    wire input, and everything this project's producer builds — acceptance,
    sizes and encoded bytes are unchanged. What changes is observable only to
    in-process callers passing subclasses, non-`dict` mappings, or documents
    built to exhaust the old walk, and none of that is a deployment's
    behaviour. *(Qualified 2026-09-23, Amendment 3: this covers Amendment 2's
    changes only. Amendment 3 narrows what decode accepts, and one of its
    changes, to the codec's integer fields, is visible to a running aggregator
    and carries a `CHANGES` entry; assumption 69.)*
61. **S8's number is 640, taken from CPython, not from §46.2** (Amendment 3
    ruling 1). Primary sources, all from CPython's 3.12 branch:
    * `Include/internal/pycore_long.h` defines `_PY_LONG_MAX_STR_DIGITS_THRESHOLD`
      as 640 and the default limit, `_PY_LONG_DEFAULT_MAX_STR_DIGITS`, as 4300.
    * `Python/sysmodule.c` (`sys.set_int_max_str_digits`) and
      `Python/initconfig.c` (`PYTHONINTMAXSTRDIGITS`,
      `-X int_max_str_digits`) accept only a limit of 0 or of at least that
      threshold.
    * `Objects/longobject.c` raises when printing an integer only if its
      unsigned digit count is greater than the limit (`strlen_nosign >
      max_str_digits`), and when parsing one only if `digits >
      max_str_digits`. Both comparisons sit inside an outer test against
      `_PY_LONG_MAX_STR_DIGITS_THRESHOLD` (on the printing side that count
      includes the sign), so an integer of at most 640 digits is never
      refused, whatever the limit.

    So an integer of at most 640 digits converts in both directions under
    every configuration any process can have, and one of 641 does not under
    the lowest. I read these files through a fetch tool that quotes and
    summarizes; the constants and comparisons above are quoted, and the tests
    pin the one premise they can see, `sys.int_info.str_digits_check_threshold
    >= 640`. *(Checked again 2026-09-23 while completing this amendment. The
    header is installed here and was read directly:
    `/usr/include/python3.12/internal/pycore_long.h`, lines 29-42, defines
    both constants and describes the threshold as one that "Acts as a
    guaranteed minimum size limit for bignums that applications can expect
    from CPython". The three `.c` files were fetched again from
    `raw.githubusercontent.com/python/cpython/3.12/`, through the same kind of
    quoting tool, and each quoted condition above was found as stated,
    including the outer test.)* Alternatives rejected:
    * *Documenting the edge instead.* `ValueError` from a read would then mean
      either a routing bug or an interpreter setting changed since the record
      was written, and a reader could not tell which. §46.8's snapshot fidelity
      would also depend on how the loading process was configured.
    * *A tighter "interoperable" cap* (2**53, 64 bits). There is no spec basis
      for one, and it would refuse integers §46.2 admits for the sake of other
      languages' parsers, which the pass-through rule leaves to consumers.
    * *Reading the threshold from `sys.int_info` at run time.* A wire rule must
      not vary with the interpreter; the test pins the premise instead.
    * *Decoding stored text with a `parse_int` that converts a long digit
      string in pieces* (added while completing this amendment). That would
      make the map's own reads independent of the limit with no new rule. It
      was rejected for three reasons. It puts a hand-written integer parser on
      the read path. It does nothing for a snapshot read back through the
      codec, or by another consumer. And it leaves the next point open.

    One more thing S8 settles (also added while completing this amendment).
    Before it, whether `decode` accepted an attribute integer of 641 to about
    1,020 digits depended on the decoding process's limit: the default
    accepted it, and a limit of 640 failed it at parse. With S8 every process
    refuses it. What still varies is the *cause*. Under a limit below the
    integer's length, `json.loads` fails before the validator runs, so the
    `CodecError` carries no `InvalidAttributesError` and assumption 37's worker
    does not count it in `attributes_rejected`. That was already so above
    4,300 digits under the default limit, and it changes no outcome.
62. **S8 narrows what decode accepts, like S5 on keys, at no cost to any
    producer** (ruling 1). A hot-ip event whose attributes hold an integer of
    641 to about 1,020 digits used to be accepted and is now a `CodecError`,
    and — assumption 42 — the event is lost at decode. R2 caps `weight` at
    1,000,000, this project's aggregator writes no other number, and an integer
    that long is nothing the §46.2 document exists to carry. No stored record
    predates S8 and needs migrating: the record map lives in memory, no
    snapshot format has shipped, and the trie service has no worker yet.
    *(Added while completing this amendment.)*
    * *S8 is a bound, like the size cap, not a narrowing of "any JSON value".*
      §46.2 says an `x_` key carries "any JSON value", and its "Bounds" list
      already caps how large that value may be. S8 adds a second bound of the
      same kind and still admits every kind of JSON value it admitted before.
      That is why it is recorded as a pointer note under §46.2's bounds and
      the section's own text is unchanged. I tried to check RFC 8259 on
      whether JSON itself lets an implementation limit the numbers it
      accepts, but its hosts are blocked by this environment's egress proxy.
      That was not checked, and nothing here rests on it.
    * *It reaches the registered names too.* An `attributes_version` of 641
      to about 1,020 digits used to pass S3 as a version above 1 and was
      stored verbatim. It is now refused. A `weight` that long was already
      refused by R2.
63. **Each distinct container is read once, and the work claim is narrowed to
    match** (ruling 2).
    * *The evidence.* The auditor's reproduction, repeated by the session, is a
      plain dict with 10**6 deleted slots and one live entry, shared 140 times
      under an `x_` key: 1,012 compact bytes, so accepted. It took 0.17 s,
      against 0.0015 s for one `dict(E)`, because the pass over the entry table
      repeated with every occurrence. CPython's `Objects/dictobject.c` (3.12)
      shows why. Deleting an item marks its hash-index slot `DKIX_DUMMY` and
      sets the key and value of its entry to NULL. The entry stays in the
      entries array, still counted in `dk_nentries`, until the dict is
      resized, and iteration steps over it. `ma_used`, what `len()` reports,
      counts only the live ones. I read that through a summarizing fetch, so
      the measurement, not my reading, is the evidence. *(Reworded while
      completing this amendment, after a second fetch of the file. The draft
      quoted "Dummy slots cannot be made Unused again", which describes the
      hash index, as though it described the entries iteration walks. It also
      said that only `.popitem()` shrinks the entries count; the file shows
      `popitem()` decrementing `dk_nentries`, and a resize rebuilding the
      array, but the fetch did not show that nothing else does.)*
    * *Why both fix and narrow.* Reading each container once removes the factor
      sharing puts on the pass: up to about 500, since an emptied dict costs 2
      bytes per occurrence. Narrowing the claim is honest about the pass that
      remains. Nothing that reads a dict through its own slots can avoid it,
      and refusing a dict for its allocation history would be a rule §46.2 does
      not have.
    * *Why strings are not included.* A string's cost is its length, which the
      bound already charges on every occurrence.
    * *The memo.* It is keyed by the `id()` of containers read in full. The
      document keeps every source object alive for the whole call, so no id is
      reused within it. The copy made for a repeat is itself iterative — for
      instance, the stored canonical copy read through the same reader, whose
      dicts are freshly built and so hold no deleted slots.
    * *What is tested.* The once-per-container property shows only in time, so
      it is reviewed rather than tested; ADR-0014 assumption 17 already rules
      out wall-clock assertions in CI. The tests pin that the result is
      unchanged and that the canonical document stays a tree.
    * *What a repeat charges* (added while completing this amendment). It
      charges what reading the container again would have charged, meaning
      the running bound's own figure for it, not its exact compact size.
      Charging the exact size would also be sound, since neither figure
      over-counts, but it would need a second measure of size beside the
      running one. The choice cannot be seen from outside. Both figures
      reject nothing of at most 1024 bytes, and the exact size is measured
      afterwards either way.
    * *Lists as well as dicts* (added while completing this amendment).
      Iterating a list touches exactly its items, which the bound already
      charges, so only dicts need rule 5 for the work claim. It covers lists
      too so that there is one rule for "met again", one memo, and one reason
      the canonical document is a tree.
    * *Sharing is not refused* (added while completing this amendment). A
      document that repeats a container is valid JSON-model input once
      copied, because its copy is a tree. A producer can build one without
      meaning to, for instance one constant dict placed under two keys.
      Refusing it would reject documents §46.2 accepts.
64. **The codec's integer fields accept exactly a JSON integer, by S3's
    definition** (ruling 3): an `int` that is not a `bool`, or a finite `float`
    with no fractional part. That is JSON Schema's `"integer"`, which every
    schema here uses for these fields. *(Corrected 2026-09-23, while
    completing this amendment. The payload schemas use it; the envelope has no
    schema file. Its three integer fields are `int` in `EventEnvelope`
    (`hammertime.core.events.envelope`), and the same rule is applied to
    them. JSON Schema Validation 2020-12, §6.1.1, defines `"integer"` as
    matching "any number with a zero fractional part". That was read at
    `json-schema.org/draft/2020-12/json-schema-validation` through a quoting
    fetch tool.)*
    * *The minimal fix* — add `OverflowError` to the `except` tuples — was
      rejected. `int()` also silently accepts what the schemas forbid: `"5"`,
      `true` (as 1) and `5.9` (truncated to 5). The last is worse than lax:
      `sequence` feeds `event_id` (ADR-0003, ADR-0004), so a truncated sequence
      can take another event's identity.
    * *Exact `int` only* was rejected too. JSON Schema admits `5.0`, a
      non-Python producer may write it, and S3 and R2 already accept it.
    * *Which fields* (added while completing this amendment). The rule covers
      every field the codec converts to `int` on decode, and no other: the
      three envelope fields and the eight payload fields ruling 3 lists. A
      payload property the codec does not read, such as
      `hot_ip_event.v1.json`'s `shard`, is not checked, because it is
      ignored.
    * *In the codec, not in the aggregator* (added while completing this
      amendment). ADR-0011 decision 3 step 1 already says that any decode
      failure is `MALFORMED` and that "A poison message never stops the
      consumer". The aggregator's `_decode` relies on the codec's contract and
      catches only `CodecError`. Keeping that contract in the codec fixes
      every consumer at once. Widening the aggregator's `except` would fix
      one, and a catch-all there would also turn a codec bug into a silently
      dropped message (assumption 55's reason, applied to the consumer).
    * *`OverflowError` joins the `except` tuples anyway* (added while
      completing this amendment). Once the type check is in, nothing in those
      blocks can raise it. It is added so that a field added later without
      the check still fails as a `CodecError`. This is my call, and it costs
      nothing.
65. **`capacity` accepts only a string of ASCII decimal digits.** The schema
    says "decimal string". Python's `int()` also accepts a sign, surrounding
    whitespace, underscores and non-ASCII decimal digits, none of which the
    schema means, and a JSON number, which it forbids. No length cap is added:
    an over-long string meets `int()`'s own limit, whose `ValueError` is a
    `CodecError`, and the largest capacity §3 defines, 2**128, has 39 digits.
    *(Added while completing this amendment.)* At least one digit is
    required, so `""` is refused. Leading zeros are accepted, so `"007"`
    decodes as 7. "Decimal string" does not forbid them, and refusing them
    would add a rule the schema does not state. The encoder never writes one,
    because it writes the decimal string of an `int`.
66. **The encoder refuses what the decoder would refuse, and writes no
    non-finite number** (ruling 3).
    * Each integer field must hold an `int` that is not a `bool`. This is
      stricter than decode, which also admits an integral `float`, because a
      float would break the `event_id` round trip: the id is derived from the
      value's text, and `5.0` is not `5`. *(Added while completing this
      amendment.)* JSON Schema Core 2020-12, §6.3 ("Mathematical Integers"),
      says the same for any producer: "integer JSON numbers SHOULD NOT be
      encoded with a fractional part". That was read at
      `json-schema.org/draft/2020-12/json-schema-core` through a quoting fetch
      tool. A subclass of `int` other than `bool`, such as an `IntEnum`
      member, is accepted, as the payload models' `int` annotations allow,
      and `json` writes it as its number. This is my call. Refusing
      subclasses would buy nothing here, because the envelope is built by
      this project's own code. Decision 5's hostile-subclass threat model is
      about attribute documents, not envelope fields.
    * `capacity` must be such an `int` and `>= 0`, so that its decimal string
      is digits only. *(Added while completing this amendment.)* That string
      is produced where the encoder turns failures into a `CodecError`, so a
      capacity too long for the interpreter's integer-string limit is a
      `CodecError`, not the `ValueError` that `str()` raises. Decode refuses
      such a string through `int()`'s own limit (assumption 65), so the
      encoder refuses what the decoder would. No producer comes near it:
      §3's largest capacity has 39 digits.
    * `json.dumps(..., allow_nan=False)` makes a non-finite float anywhere a
      `ValueError`, which the encoder already turns into a `CodecError`. The
      json module's own docstring says the flag refuses `nan`, `inf` and
      `-inf` "in strict compliance of the JSON specification"
      (`/usr/lib/python3.12/json/__init__.py`, lines 200-203). Canonical
      attributes never hold one (S4), so the flag is a backstop for every
      other field.
67. **Decode does not reject `NaN` or `Infinity` at parse time.**
    `json.loads(..., parse_constant=...)` could; the same docstring, lines
    325-328, offers it for exactly this, and it would make decode strict JSON.
    I did not take it, because those tokens matter in only two places. In an
    integer field ruling 3 now rejects them. Inside `attributes` S4 rejects
    them, with the `InvalidAttributesError` cause that assumption 37's
    `attributes_rejected` count depends on; rejecting them at parse would turn
    that into an unattributed envelope error. In an ignored field they change
    nothing.
68. **Overriding `__contains__` is consistent with decision 5's table**
    (ruling 4). The inherited `Mapping.__contains__` goes through
    `__getitem__`, which decodes a whole record to answer yes or no. The
    override answers from the stored keys:
    * `False` for anything that is not an `Address` (decision 5: "a key that
      is not an `Address` at all is simply absent");
    * the family `ValueError` for the other family (assumption 39);
    * otherwise, whether a record is stored.

    The outcomes are the same, without the decode. `get`, `values` and `items`
    still go through `__getitem__`, because they need the document.
    *(Added while completing this amendment.)* The type test comes before any
    lookup, so an unhashable argument — a `list`, a `dict` — answers `False`,
    as it did through the inherited method and as assumption 45 rules for
    `prefix in store`. `a in records.keys()` gives the same answers, because
    the ABC's keys view asks the map's own `__contains__`
    (`/usr/lib/python3.12/_collections_abc.py`, lines 865-866:
    `return key in self._mapping`).
69. **One `CHANGES` entry for Amendment 3, for the aggregator** (assumptions
    27, 60). *(Reversed 2026-09-23, while completing this amendment. The
    draft this amendment began with was headed "No `CHANGES` entry for
    Amendment 3". Its list and its last paragraph are kept below, and the
    reasons for reversing it follow them.)* Rulings 1 and 3 newly reject only
    values no producer in this project writes:
    * an integer of more than 640 digits in an attribute document;
    * a string, boolean, fractional or non-finite number in an integer field;
    * a `capacity` that is not a string of digits.

    The bus is internal. What changes for a deployment is that a malformed
    message on it is now a `CodecError` — logged, counted and skipped, as the
    aggregator's consume loop already promises for poison messages — rather
    than an `OverflowError` that stopped the loop.

    Why that last paragraph is a reason *for* an entry:
    * *Who runs it.* The codec's decode has one consumer running today: the
      aggregator, on `hammertime.observations.v1`. The trie service, which
      will read hot-ip events, and the detector, which will read prefix-stats
      events, do not run yet. The record map has no production caller. So S8,
      and everything ruling 3 changes for hot-ip and prefix-stats messages,
      changes nothing a deployment runs, and assumption 27's reasoning holds
      for them.
    * *What the aggregator did before.* A string, boolean or fraction in an
      integer field was coerced and applied. An infinity raised
      `OverflowError` out of `decode`. That ended the consume task, and with
      it the service: `run_exited`, exit 1. The message stayed
      unacknowledged, to be delivered again.
    * *What it does now.* Each is `MALFORMED`: logged, counted in
      `observations_rejected`, and acknowledged. ADR-0011 decision 3 step 1
      had already promised this.
    * *Why that qualifies.* It is changed behaviour of a running service,
      visible in its exit status, its log and a metric, which is `CLAUDE.md`'s
      "changed behaviour". `CHANGES` already records how the aggregator
      treats its bus input (a redelivered observation acknowledged without
      being applied again), and failures that became clean errors (a
      malformed registry file reported without a traceback). The draft's
      reason, that the bus is internal and no producer here writes such a
      message, bounds how often this happens. It does not stop an operator
      seeing it when it does.
    * *Why not `BREAKING`.* No deployment needs to act. Every producer here
      already writes integers, and the schemas already required them.

    The entry is one line, for the change that implements ruling 3:

        Aggregator counts an observation message whose integer field holds a string, boolean, fraction or infinity as malformed (observations_rejected) and skips it, instead of coercing the value or, for an infinity, stopping

    `CHANGES` is outside this ADR's scope. The line is recorded here so the
    implementing change does not have to re-derive it, as assumption 27 did
    for epic #9.

## Consequences

* Epic #9 becomes implementable against a fixed surface: four modules, three
  value kinds, three combine entry points, one metadata store, one record map,
  two coupled apply functions, one `PrefixStats` view — and, in
  `hammertime-core`, one public validator and one error type that the codec and
  the record map share. *(Amended 2026-09-23, Amendment 2 ruling A: the
  validator's entry point is `canonicalize_ip_attributes`, returning
  `CanonicalAttributes`; `validate_ip_attributes` is its measuring form.)*
* **ADR-0005 decision 5 and ADR-0014 decision 9 are discharged**, not
  departed from: the trie validates shape and size before storing, with the
  same rules the codec applies, and no copy of those rules exists outside
  `hammertime.core.events.attributes`.
* **`hammertime-core` changes shape but not wire behaviour** — apart from S5's
  surrogate-in-a-key tightening (assumption 27): `events/attributes.py` is new,
  `errors.py` gains `InvalidAttributesError`, and `events/codec.py` loses its
  private copies of the §46.2 rules and calls the shared function instead.
  `test_ip_attributes.py` must pass unmodified. *(Amended 2026-09-23,
  Amendment 2: the codec now sends and decodes the canonical copy, and for
  exact-type input — all wire input — the bytes it produces are unchanged.)*
  *(Amended 2026-09-23, Amendment 3: two more exceptions to "not wire
  behaviour". Decode refuses an attribute integer of more than 640 digits
  (S8) and a non-integer in any integer field (ruling 3). Encode refuses an
  integer field that does not hold an `int`, and never writes `NaN` or
  `Infinity`. The bytes every producer here writes are unchanged, and
  `test_ip_attributes.py` must still pass unmodified.)*
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
  *(Amended 2026-09-23, Amendment 3. Decode narrows twice more, both times
  only by values no producer here writes: S8 caps an attribute integer at 640
  digits, and the codec's integer fields accept JSON integers only. The
  encoder no longer writes a non-finite number. Still no schema or
  wire-format change. There is now one `CHANGES` entry, because the second
  narrowing changes what the running aggregator does with such a message;
  assumptions 62 and 69.)*
* Spec pointer notes added by this ADR: §9 (where `local_metadata` lives), §12
  (what is combined upward, and that `hot_ratio` is not), §16 and §17 (the
  prefix-keyed store and the two fold directions), §46.5 (the module that holds
  the record map, validates it and applies the coupled step), §46.8 (which
  object answers each metric, and who counts rejections) and §46.9 (validation
  on write; no values in messages). `docs/spec/README.md`'s index is updated
  for §16/§17, §46 and §46.5. *(Amended 2026-09-23, Amendment 3: a pointer
  note on §46.2 is added — where the bounds are enforced, and S8 — and the
  README's list of ADR-0015's notes now includes §46.2. The README's §19 row
  also names this ADR, for ruling 3's rules on the codec's integer fields.
  §19 itself gets no note, because it says nothing about wire types.)*
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
   Assumption 47. Clarified the same day: the bullet's "raises `ValueError`
   naming the key, both kinds and that prefix" did not say in what form the
   prefix appears; an appended sentence with a dated note now says it is
   `str(prefix)`, for IPv4 and IPv6 alike, and a dated note on assumption 47
   records why.
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

## Amendment 2 (2026-09-23) — the attribute validator reads a document once, into the copy that is checked, stored and sent; its work is bounded before it reads; nothing but `InvalidAttributesError` comes out of a document; the record map keeps canonical text

Why: epic #9 is implemented (branch `claude/eager-gates-lyihfk`, head
`d67e2c5`; `hammertime.core.events.attributes`, `codec.py`, `errors.py`,
`hammertime.trie.metadata`), with all four gates green. `reviewer` and
`security-auditor`, each under `supervisor`, then raised five findings against
decision 5's validator and record map. All are low severity, and all are
reachable only from in-process code, since `json.loads` produces nothing but
exact built-in types. Four (A-D) are interface questions and are ruled here,
in place, with dated notes; the fifth (E) is test coverage and is listed under
*Follow-ups*. Unlike Amendment 1, which ruled points ahead of the code, these
bind code that exists: rulings A, B and D require changes to it, and ruling C
is the contract that A and B make true. Nothing changes for exact-type input,
which is all wire input and everything this project's producer builds, so
there is still no `CHANGES` entry (assumption 60).

**A. One read, into a canonical copy — and that copy, never the caller's
object, is what is checked, measured, stored and sent.** The shipped validator
read the caller's objects three times, each time through methods a subclass
can override. It checked them with `isinstance`, `len`, `.items()`, iteration
and comparisons; `json.dumps` read them again to size them; and the record
map's copier read them a third time to build what it stored. `codec.encode`
had the same shape. A value could therefore show different content to each
read, for instance:

* a `list` subclass whose `__iter__` shows `[1]` to the check while its
  storage holds a 50 MB string;
* a `str` subclass whose `__iter__` hides a lone surrogate;
* an `int` subclass whose comparisons pass `10**9` as a `weight`;
* a key that equals `"weight"` by `__eq__`/`__hash__` but serializes as
  `"sources"`.

Ruled:

1. A new public `canonicalize_ip_attributes(document: object) ->
   CanonicalAttributes` in `hammertime.core.events.attributes` reads the
   document once and builds a new tree of exact built-in types. It dispatches
   on `type()` with `issubclass`, and reads a subclass instance only through
   the base type's own slot functions. It then applies the rules to that
   copy, serializes that copy, and returns both. The mechanism and its
   observable contract are in decision 5, "One read, into the copy that is
   used".
2. `CanonicalAttributes` is a `NamedTuple` with two fields and one property.
   `document: dict[str, object]` holds exact built-in types only, and no
   caller has held it. `text: str` is its compact encoding: ASCII only, and a
   fixed point of `json.loads` followed by compact `json.dumps` (assumption
   59). `size` is `len(text)`.
3. Two keys of one object that copy to the same text are a rejection (S4).
4. S1 means a `dict`. Any other mapping, `DEFAULT_ATTRIBUTES` included, is
   refused (assumption 53).
5. `validate_ip_attributes` stays, as `canonicalize_ip_attributes(d).size`:
   same contract, and the same number for every exact-type input. It is for
   checking and measuring only. No production code may call it and then keep
   its argument.
6. **The copy happens inside `hammertime-core`, for both callers**
   (assumption 49). The codec puts `result.document` into the envelope it
   encodes and into the event it decodes; the record map keeps `result.text`
   (ruling D). So `codec.encode`'s version of the hole is closed by the same
   change as the store's.
7. Subclasses stay accepted and are stored and sent as their base types
   (assumptions 50 and 51). That is what assumption 33 had promised; the
   shipped code did not deliver it, because it stored the subclass objects
   themselves.

Behaviour: for exact-type input, acceptance, sizes and encoded bytes are
unchanged. For in-process input:

* a subclass is now read from its built-in storage and copied as its base type;
* documents that used to slip through are now rejected — the hidden surrogate,
  the impersonating key, the lying comparison;
* two keys equal as text are rejected;
* a non-`dict` mapping, which `record()` used to copy, is now refused.

**B. The work is bounded by the size cap before anything is read.** The shipped
walk tracked only the current path. A subtree shared by many branches
(`x = [x, x]`, 40-65 levels deep) was therefore re-walked every time it was
met, about 2**40-2**65 visits, and would hang the single writer. A flat
`[0] * 10**8` was walked in full before S6 was reached. Ruled: decision 5,
"Bounded work".

* A running lower bound on the compact size rejects the document under S6 the
  moment it passes 1024.
* A string's, list's or dict's length, measured by its base type, is checked
  before its contents are read.
* An integer is judged by its bit length before it is converted to text.
* The read is iterative.

At most 1024 values are ever read, and no document of at most 1024 bytes can
be rejected by the bound (assumption 54). This is S6 applied early, not a new
limit. *(Narrowed 2026-09-23 by Amendment 3 ruling 2. The cap bounds what is
read, but reading a dict also passes over its entry table, deleted slots
included, and that pass is not bounded by the cap. Amendment 3 makes it one
pass per distinct dict and says so in decision 5, "What the cap does not
bound". The heading's "The work is bounded" should be read in that sense.)*

**C. Nothing but `InvalidAttributesError` comes out of a document.** The
shipped code converted only `RecursionError`, `TypeError` and `ValueError`.
Everything else escaped: a subclass `__len__` raising `RuntimeError` or
`OverflowError` while `_quote` built a message, or `items()` or `__iter__`
raising anything, or never ending. Those escaped `record()`,
`apply_hot_ip_added()` (before the trie was touched, so without desync) and
`codec.encode` (not as a `CodecError`). A worker built to decision 6's table
would then not have taken the `attributes=None` recovery path, and the §46.1
transition would have been lost. Ruled: decision 5, "What can come out",
states per function which exceptions may escape.

* Ruling A makes that table true by construction: nothing the document's own
  types define is ever called.
* Messages are composed from the canonical copy only.
* There is **no blanket `except Exception`** (assumption 55). `MemoryError` and
  non-`Exception` `BaseException`s pass through.
* One narrow conversion is kept: a `RecursionError` or `ValueError` from
  serializing the canonical copy becomes `InvalidAttributesError`.
* Decision 6 gains a sentence saying its table is complete for
  document-caused failures.

**D. The record map keeps each record as canonical text and decodes a fresh
document on every read.** The shipped map returned a `MappingProxyType` over
the stored top level, whose nested dicts and lists *were* the stored objects.
A reader could therefore grow a record past 1024 bytes while
`serialized_bytes` stayed stale, and a snapshot would then save a record that
no replay can reproduce (§46.8). Decision 5 ("nested containers inside a
returned record are read-only by contract") and assumption 18 ("Reads are not
copied") had accepted this. Ruled, reversing both:

* The map keeps, per address, the canonical text that
  `canonicalize_ip_attributes` returned.
* Every read decodes that text into a fresh document and returns a read-only
  view over it. Two reads are distinct objects; every value is an exact
  built-in type; nothing a reader does reaches the map.
* `serialized_bytes` is the sum of the stored lengths.
* The store imports `json`, to decode only.

Stored text was chosen over copy-on-read and over keeping objects under a
read-only contract. The trade-offs — the writer's hot path, read cost,
memory, exactness and snapshot fidelity — are set out in assumption 57, and
why reads still return a view in assumption 58.

### Follow-ups (not part of this amendment; for the top-level session to dispatch)

* `test-author` (E): no test yet shows that subclasses are accepted as their
  base type. That covers an `IntEnum` `weight` and `attributes_version`, a
  `str`-subclass key and value, a `float` subclass, `dict` and `list`
  subclasses nested and at the top level, and the canonical copy and every
  read-back holding exact built-in types only.
* `test-author` (E): no test yet shows that `bool` is refused where S3 or R2
  need an integer, yet accepted and read back as a JSON boolean inside an
  `x_` value.
* `test-author` (E): no test yet shows that the map stores nested subclass
  containers as plain `dict` and `list`.
* `test-author` (E): both new test files still carry `from __future__ import
  annotations`, against #24: `test_metadata.py` line 75 and
  `test_attribute_validation.py` line 40.
* `test-author` (A-D): the new contract. That means hostile subclasses whose
  overrides lie, raise or never return, each with the same outcome as the
  plain document; the impersonating key, the lying comparison, the hidden
  surrogate and duplicate text keys; the shared-subtree, flat-list,
  long-string, huge-integer and cycle cases, and the deepest nesting and
  many-small-items documents at exactly 1024 bytes that must be *accepted*;
  canonical idempotence; the codec sending and decoding the canonical copy;
  non-`dict` mappings refused at the top level; and fresh, isolated,
  exact-typed reads.
* `coder` (A-D): `canonicalize_ip_attributes` and `CanonicalAttributes`, with
  `validate_ip_attributes` delegating to them; the codec and the record map
  using the canonical copy; the map storing text; and removal of the
  now-unused copier.

### Every edit outside this section, with the superseded wording quoted

* **Status line.** Gained a clause naming this amendment; nothing removed.
* **Decision 5, the interface sketch.** The `hammertime.core.events.attributes`
  block gains `CanonicalAttributes` and `canonicalize_ip_attributes`.
  `validate_ip_attributes`'s comment was: "The §46.2 rules below; returns the
  document's serialized size in bytes. / Raises InvalidAttributesError for
  every violation, and nothing else." It now describes the function as
  `canonicalize_ip_attributes(document).size`.
* **Decision 5, "The rules exist once".** Was: "The codec's private checks
  (`_validate_attributes` and its helpers in `hammertime.core.events.codec`)
  move into one new public function, `validate_ip_attributes`, in a new module
  `hammertime.core.events.attributes`. Both enforcement points call it:". Now
  names `canonicalize_ip_attributes` as the entry point both callers use, and
  keeps what it returns.
* **Decision 5, the codec bullet.** One sentence appended: the codec sends and
  decodes the canonical document. Nothing removed.
* **Decision 5, the record-map bullet.** Was: "**the record map**, on every
  write, letting `InvalidAttributesError` propagate." Now also "keeping only
  the canonical text".
* **Decision 5, the rules table.** Four rows were rewritten; the old text of
  each follows.
  * S1 was: "a JSON object: a dict".
  * S4 was: "every value at every depth is in the JSON data model — dict with
    str keys, list, str, int, finite float, bool, None; no tuple, set, bytes,
    non-str key or other object".
  * S6 was: "serialized size <= 1024 bytes, measured as
    len(json.dumps(document, separators=(",", ":")).encode("utf-8")) with
    json's default ensure_ascii — the compact form the codec writes; this is
    the size the function returns".
  * S7 was: "nesting too deep to walk or serialize, and a self-referencing
    structure, are rejections — never a RecursionError or ValueError".

  A dated note follows the table.
* **Decision 5, three new paragraphs** after the one ending "A key is no safer
  than a value.": "One read, into the copy that is used" (ruling A), "Bounded
  work" (ruling B) and "What can come out" (ruling C). Nothing removed.
* **Decision 5, the messages paragraph.** One sentence appended (ruling C).
* **Decision 5, the `record()` bullet.** Its middle was: "`None` becomes
  `DEFAULT_ATTRIBUTES` (…), so a record exists for every HOT address
  unconditionally and §46.5's count invariant needs no special case; a
  document that is not a `Mapping` is an `InvalidAttributesError` (S1);
  `validate_ip_attributes` runs on a plain-`dict` copy of it; the validated
  document is deep-copied; and only then does the map change — the new record
  replaces any earlier one for that address (§46.5's replace-on-add) and
  `serialized_bytes` moves by the difference." Now `None` selects the default,
  anything else goes through `canonicalize_ip_attributes` (a non-`dict` is
  refused under S1), and the canonical text is what replaces the record. The
  bullet's first and last sentences are unchanged.
* **Decision 5, the "private deep copies" bullet, replaced.** Was: "**Stored
  records are private deep copies**, so nothing a caller does to the document
  afterwards — at any depth — can make a stored record invalid or different
  from what was validated; replay determinism depends on it. Reads return a
  read-only view of the stored top level (`MappingProxyType`); nested
  containers inside a returned record are read-only by contract." Now "The map
  keeps each record as its canonical text, and decodes a fresh document on
  every read" (ruling D).
* **Decision 5, the `serialized_bytes` bullet.** Was: "the sum of the sizes
  `validate_ip_attributes` returned for the records currently stored"; now
  "the total length of the stored canonical texts — each the size S6
  measured".
* **Decision 5, the last bullet's last sentence.** Was: "The module imports
  neither `json` nor the schema: sizes come back from the validator." Now the
  module imports `json` to decode its stored text on read only.
* **Decision 6, clause 2.** Was: "`None` becomes `DEFAULT_ATTRIBUTES`, a
  non-`Mapping` or any §46.2 violation is an `InvalidAttributesError`, the
  validated document is deep-copied and its size kept". Now names
  `canonicalize_ip_attributes` and the canonical text.
* **Decision 6, clause 4.** Was: "The prepared document replaces any record for
  the address"; now "The prepared text".
* **Decision 6**, a paragraph after the worker's exception table, saying the
  table is complete for document-caused failures (ruling C). Nothing removed.
* **Decision 7, the module layout.** The `events/attributes.py` row read
  "validate_ip_attributes — the §46.2 rules, once" and the `events/codec.py`
  row "calls validate_ip_attributes; its private copies of the rules are
  removed". Both now name `canonicalize_ip_attributes`, and the codec row says
  it sends the copy. A dated note follows the import paragraph.
* **Assumptions 18, 21, 33, 34, 36 and 40** each gain a dated note. Nothing in
  any of them was reworded. Ruling D supersedes assumption 18 in three places:
  its heading, "Stored documents are deep copies; reads are shallow read-only
  views."; its write-side claim, "a deep copy per write costs nothing that
  matters"; and "Reads are not copied". Records are now stored as canonical
  text (assumption 57). The other five are qualified.
* **Assumptions 49-60** are new.
* **Consequences**, the first bullet and the "`hammertime-core` changes shape"
  bullet, each gain a dated note. Nothing removed.
* **Spec §46.5's ADR-0015 note.** Was: "It validates every document before
  storing it, whatever path the document arrived by, with
  `hammertime.core.events.attributes.validate_ip_attributes` — the same
  function the codec calls, so the Section 46.2 rules exist once (ADR-0005
  decision 5, ADR-0014 decision 9)." Now names `canonicalize_ip_attributes`,
  and adds one sentence: the map stores the canonical text and decodes a fresh
  document on every read.
* **Spec §46.9's ADR-0015 note.** Was: "its record map runs every document
  through `hammertime.core.events.attributes.validate_ip_attributes`, the
  codec's own rules moved into one public function, so a snapshot file or an
  in-process producer is no way around Section 46.2. The validator accepts only
  the JSON data model and never puts a document value into an exception
  message." Now names `canonicalize_ip_attributes`, says the document is read
  once into a canonical copy and that only the copy is checked, stored and
  sent, and adds that the work is bounded by the size cap before reading.

## Amendment 3 (2026-09-23) — integers capped at 640 digits, each container read once, the codec's integer fields made strict, `in` without a decode

Why: Amendment 2 is implemented (head `a2810d8`, all four gates green,
3388 tests passing). `reviewer`'s re-review and `security-auditor`'s
supervised re-audit confirm that its rulings A-D are closed, and they raised
four low-severity items. Each item is ruled below, in place, with dated
notes. All four change shipped code; ruling 4's change leaves every outcome
decision 5's table states as it was. *(This amendment was drafted by one
architect session and completed by a second the same day, before either was
committed. "Completed by a second session", at the end of this section, lists
what the second added or changed.)*

**1. An attribute integer has at most 640 decimal digits: new rule S8**
(reviewer; assumptions 61 and 62).
* *The finding.* `records[a]` decodes stored text with `json.loads`. S6
  admitted integers the default limit prints (about 1,020 digits) but a
  lower legal limit cannot parse, which fails in two ways:
  - If `sys.set_int_max_str_digits` lowers the limit after such a record is
    stored, the read raises `ValueError`, which decision 5's table reserves
    for a family mismatch.
  - A snapshot holding such a text cannot be parsed by a process started
    with `PYTHONINTMAXSTRDIGITS=640`.

  Either way assumption 59's fixed point fails.
* *The ruling.* S8 (decision 5's table) refuses any integer of more than 640
  decimal digits, sign not counted. 640 is the lowest limit any configuration
  can set, and CPython raises only when a digit count *exceeds* the limit, so
  every stored integer now prints and parses under every legal setting.
  - Reads cannot raise that `ValueError`.
  - Assumption 59's fixed point holds whatever the limit.
  - `decode` refuses such an integer under every limit, where before
    whether it was accepted depended on the decoding process's limit
    (assumption 61).
  - The `ValueError` half of ruling C's narrow conversion becomes
    unreachable, and stays as a backstop.
  - Assumption 54's "One edge remains" is superseded.
  - The check is an exact comparison on the copied integer, after "Bounded
    work"'s bit-length check.
* *What it costs.* S6 admitted integers of up to about 1,020 digits; 641 to
  1,020 are now refused, on the wire too. That includes an
  `attributes_version` that long, which S3 alone passed (assumption 62). No
  producer writes one.

**2. Each distinct container is read at most once, and the work guarantee is
narrowed to what that achieves** (auditor, reproduced by the session;
assumption 63).
* *The finding.* The running bound charges a nested dict by `dict.__len__`,
  its live entries. But `dict.items` walks CPython's whole entry table,
  deleted slots included, and walks it again for every shared occurrence.
  "At most 1024 values read" held; "work bounded by the size cap" (decision 5,
  Amendment 2 ruling B, the §46.9 note) did not. The session's measurement is
  in assumption 63.
* *The fix.* "Bounded work" gains rule 5. A container, dict or list, met again
  after it has been read in full is not read again; a fresh copy of its
  canonical copy takes its place, and the running total grows as though it
  had been read. The canonical document stays a tree.
* *The narrowing.* The new paragraph "What the cap does not bound" states the
  one cost that remains: one pass over each *distinct* dict's entry table.
  That pass is proportional to memory the caller already allocated, and it is
  paid once however often the dict is shared.
* *Why both.* The fix removes the factor that sharing put on the pass. The
  narrowing is honest about the pass that no read through a dict's own slots
  can avoid.

**3. The codec's integer fields accept JSON integers only, and the encoder
writes no non-finite number** (auditor; pre-existing on master `dd5bd6b`;
assumptions 64-67).
* *The finding.* `decode` converted integer fields with `int()` and caught
  only `TypeError` and `ValueError`. `json.loads` accepts `Infinity`, and
  `int(float("inf"))` raises `OverflowError`. That escaped `decode` as a
  non-`CodecError`, against decision 5's "`CodecError` only" row, and the
  aggregator's `_decode`, which catches only `CodecError`, would lose its
  consume loop with the message unacknowledged. *(Stated exactly while
  completing this amendment: the exception ends the aggregator's consume
  task, and with it the service, `run_exited` with exit 1, against ADR-0011
  decision 3 step 1's "A poison message never stops the consumer".)*

*Decode.* Each integer field accepts exactly a JSON integer — S3's
definition: an `int` that is not a `bool`, or a finite `float` with no
fractional part — and converts it to `int`. The integer fields are every field
the codec converts to `int`: the payload fields the schemas type `"integer"`,
and the envelope's three, which `EventEnvelope` types as `int` since no schema
file describes the envelope. *(Worded so while completing this amendment; the
draft read "Each field the schemas type `"integer"`", which left out the
envelope. Assumption 64.)* Anything else is a `CodecError`: a string, a
boolean, a fractional or non-finite number, `null`, an array or an object. The
fields are:
* in the envelope, `schema_version`, `sequence` and `config_version`;
* in `RequestObservation`, `sequence`, `window_seconds` and each
  observation's `request_count`;
* in `HotIpAdded` and `HotIpRemoved`, `sequence`, `window_count` and
  `config_version`;
* in `PrefixStatsChanged`, `hot_count` and `sequence`.

`capacity`, a decimal string in the schema, accepts exactly a `str` of ASCII
digits `[0-9]+`, converted with `int()`, and nothing else. So `""` is refused
and leading zeros are accepted (assumption 65). With these checks
`OverflowError` cannot arise. The conversions' `except` tuples add it anyway,
so that a field added later without the check still fails as a `CodecError`.

*Encode.*
* Each integer field must hold an `int` that is not a `bool`, and `capacity`
  must hold such an `int` `>= 0`; otherwise a `CodecError`. A subclass of
  `int` other than `bool`, such as an `IntEnum` member, is accepted
  (assumption 66).
* `capacity`'s decimal string is produced where a failure becomes a
  `CodecError`, so a capacity too long for the interpreter's integer-string
  limit is a `CodecError`, not a `ValueError` (assumption 66).
* The envelope is serialized with `allow_nan=False`, so a non-finite float
  anywhere is a `CodecError`, not `NaN` on the wire.
* Canonical attributes never hold one (S4), so this is a backstop for the
  other fields.

*`CHANGES`.* One entry, because the running aggregator now skips, as
malformed, an observation message it used to apply with a coerced value or
stop on. The line is in assumption 69. *(Added while completing this
amendment. It reverses the draft's "No `CHANGES` entry"; assumption 69 says
why.)*

*What is not changed.* Decode does not reject `NaN` or `Infinity` tokens at
parse time (assumption 67). Schema minimums and maximums are not added
(below).

**4. `a in records` answers without decoding the record** (reviewer). The
override — `False` for a non-`Address`, the family `ValueError`, then key
membership — gives the same outcomes decision 5's table and assumption 39
already require. It is recorded in decision 5's reads bullet and in
assumption 68, and, since the second session, in decision 5's interface
sketch, whose comment had listed `__contains__` among the methods the ABC
supplies.

### Found while ruling, not ruled here (for the top-level session to schedule)

* **`decode`'s error messages embed `repr` of whole payloads.** Examples are
  `malformed RequestObservation payload: {data!r}` and `malformed observation
  entry: {item!r}`, and the hot-ip path redacts only `attributes`. A large
  payload therefore makes an equally large message. This predates Amendment 2
  and is outside these findings; it wants the same "bounded, never the value"
  rule decision 5 applies to attribute messages.
* **String fields are coerced with `str()`.** `agent_id`, `event_id` and
  `prefix` accept any JSON value; a number `5` becomes `"5"`. This is lax in
  the same way ruling 3 fixes for integers, but it cannot raise, so it is not
  part of these findings.
* **The schemas' `minimum` and `maximum` are not enforced by the codec.**
  Examples: `sequence >= 0`, `window_seconds` 1-3600, `request_count <= 10**9`.
  Out of scope here.
* **The description of `x_` values in `schemas/ip_attributes.v1.json`** could
  mention S8 beside the 1024-byte cap it already names ("not expressible
  here"). It is a description-only edit to `schemas/`, which this dispatch was
  limited to `docs/` and did not make.

### Follow-ups (for the top-level session to dispatch)

* `test-author`, covering rulings 1-4 and the boundary cases the coordinator
  listed: documents of exactly 1024 compact bytes built from `null`, `true`,
  `false`, negative floats, negative ints and escaped non-ASCII strings, each
  accepted, with one more item rejected. Rulings 1, 2 and the boundary cases
  belong in `test_attribute_validation.py` (and, for the record map,
  `test_metadata.py`); ruling 4 in `test_metadata.py`; ruling 3 in
  `test_codec.py`, since it concerns the codec's own fields, not attributes.
* `coder`, for rulings 1-4, with assumption 69's `CHANGES` line in the same
  change as ruling 3.

### Every edit outside this section, with the superseded wording quoted

* **Status line.** Gained a clause naming this amendment; nothing removed.
* **Scope note.** Gains a dated note: ruling 3 reaches the codec's integer
  fields and `capacity`, beyond epic #9's files. Nothing removed.
* **Decision 5, the interface sketch.** The comment above `__getitem__` was
  "# Mapping — __contains__, get, keys, items, values come from the ABC.". It
  no longer names `__contains__`, and a `__contains__` line is added
  (ruling 4).
* **Decision 5, the rules table.** Row S8 is added after S7. A second dated
  note follows the Amendment 2 note after the table; that note is unchanged.
* **Decision 5, "Bounded work".** Rule 5 is added after rule 4, and the new
  paragraph "What the cap does not bound" follows the consequences
  paragraph. Nothing in rules 1-4 or in that paragraph was reworded.
* **Decision 5, the narrow-conversion paragraph under "What can come out".**
  It still reads "that can only mean an interpreter configured with an
  integer-to-string limit below what S6 admits, or a caller already at the
  edge of the stack", and now has a dated note that S8 removes the first case.
  It is followed by a new paragraph, "Three rows that hold because of
  Amendment 3".
* **Decision 5, the reads bullet** ("The map keeps each record as its
  canonical text, and decodes a fresh document on every read"). One sentence
  on membership is appended; nothing removed.
* **Assumption 54.** Gains a dated note: "One edge remains: an interpreter
  configured with a limit below the roughly 1,020 digits S6 can admit will
  reject such an integer through ruling C's narrow conversion — as
  `InvalidAttributesError`, never as a `ValueError`." is superseded.
* **Assumption 59.** Gains a dated note: the fixed point now holds whatever
  the interpreter's integer-string limit.
* **Assumptions 27, 39, 55 and 60** each gain a dated note; nothing in them
  was reworded. The conclusions of 27 and 60, "No `CHANGES` entry for this
  epic" and "Still no `CHANGES` entry", now cover their own changes only,
  because assumption 69 gives this amendment one entry. In 39, "because
  `Mapping.__contains__` and `get` only absorb `KeyError`" no longer explains
  `in`, which the map now answers itself, with the same `ValueError`. In 55,
  the `ValueError` half of "both are conditions of the interpreter, which a
  document of legal shape can still meet" no longer holds, and that half is a
  backstop only.
* **Assumptions 61-69** are new.
* **Amendment 2, ruling B.** The paragraph ending "This is S6 applied early,
  not a new limit." gains a dated note narrowing the heading "The work is
  bounded by the size cap before anything is read."
* **Consequences, the "`hammertime-core` changes shape but not wire
  behaviour" bullet.** Gains a second dated note naming this amendment's
  exceptions to "not wire behaviour"; nothing removed.
* **Consequences, the "No schema changes; no wire-format changes" bullet.**
  Gains a dated note; nothing removed. The bullet's own "no `CHANGES` entry
  (assumptions 27, 28)" is left as written; the note says this amendment
  carries one.
* **Consequences, the "Spec pointer notes added by this ADR" bullet.** Gains a
  dated note naming the §46.2 note and the README's §19 row; nothing removed.
* **Spec §46.2** gains an ADR-0015 pointer note for S8.
* **Spec §46.9's ADR-0015 note.** Was: "The validator accepts only the JSON data
  model, bounds its work by the size cap before reading, and never puts a
  document value into an exception message." Now: it accepts only the JSON
  data model, bounds what it reads by the size cap, reads each container of
  the document at most once, and never puts a document value into an
  exception message.
* **`docs/spec/README.md`, the preamble.** ADR-0015's list of sections with
  pointer notes gains §46.2.
* **`docs/spec/README.md`, the §19 row.** Its last column was "`core/events`,
  `packages/hammertime-bus` (`interface.py`, `memory.py`, `nats.py`),
  `tools/provision`, `docs/adr/0004`, `docs/adr/0013`". It gains
  "`docs/adr/0015` (Amendment 3 ruling 3: the codec's integer fields)".

### Completed by a second session

The first session was stopped just after writing the text above, before
anything was committed. A second architect session checked the draft against
decisions 1-8, Amendments 1-2, ADR-0005, ADR-0014 and the code at `a2810d8`,
kept every ruling, and changed the following:
* **Reversed one conclusion: the `CHANGES` entry.** The draft's assumption 69
  said "No `CHANGES` entry for Amendment 3". It now gives one line, for the
  aggregator, and says why. The status line and ruling 3 now name the entry.
  The draft's note on the Consequences bullet "No schema changes; no
  wire-format changes" read "Still no schema, wire-format or `CHANGES`
  change"; it now says there is one entry. New notes on assumptions 27 and 60
  point to it.
* **Corrected.**
  - Ruling 3 and assumption 64 said the integer fields were those "the
    schemas type `"integer"`". The envelope's three have no schema, so the
    fields are now defined as those the codec converts to `int`.
  - The status line's "under every legal interpreter configuration", ruling
    1's "holds unconditionally" and assumption 59's note's "S8 makes it
    unconditional" claimed more than S8 gives. They now say "whatever the
    interpreter's integer-string limit" or "removes that condition".
  - "Three rows"' codec bullet said "holds for every field". It now says
    what holds for `decode` and what for `encode`.
  - Ruling 3's finding said the aggregator "would lose its consume loop". A
    note now says what that means: the service stops with exit 1, against
    ADR-0011 decision 3 step 1.
  - The "Why" paragraph said "Rulings 1-3 change shipped code; ruling 4
    confirms an implementation change against decision 5's table". Ruling 4
    needs code too, so it now says all four change shipped code.
  - The §46.2 pointer note's last sentence read "No producer writes an
    integer anywhere near that long". It now says "No producer in this
    project".
  - Assumption 63's evidence bullet quoted CPython's comment on the hash
    index as though it described the entries array. It now says what the
    source shows about deleted entries.
* **Stated what the draft left open.**
  - Encode accepts an `int` subclass, and `capacity`'s decimal string cannot
    escape as a `ValueError` (assumption 66; both also in ruling 3).
  - `capacity` requires a digit and accepts leading zeros (assumption 65).
  - A repeated container is charged as a re-read would be. Lists are covered
    by rule 5 too, and sharing is not refused (assumption 63).
  - An unhashable argument to `in` answers `False` (assumption 68).
  - Ruling 3 is fixed in the codec, not in the aggregator's `except`
    (assumption 64). The draft's ruling 3 already added `OverflowError` to
    the `except` tuples; assumption 64 now records that as a judgment call.
* **Added evidence and one more alternative.**
  - Assumption 61: the installed CPython header and a second fetch of the
    three `.c` files; the outer threshold test; the chunked-`parse_int`
    alternative; and the fact that S8 also makes decode's outcome independent
    of the limit. Ruling 1 gains that last point, and names
    `attributes_version` under "What it costs".
  - Assumption 62: S8 read as a bound, not a narrowing of "any JSON value",
    and its reach to `attributes_version`.
  - Assumptions 64 and 66: JSON Schema 2020-12's definitions.
  - The follow-ups name the test file for each ruling, and put the `CHANGES`
    line with `coder`.
* **Edits the draft missed.** The scope note; the interface sketch's
  `__contains__`; dated notes on assumptions 27, 39, 55 and 60; the second note on
  the Consequences bullet "`hammertime-core` changes shape but not wire
  behaviour"; and the README's §19 row. All are listed above.
