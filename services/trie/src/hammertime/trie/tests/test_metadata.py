"""Prefix metadata inheritance, the upward hot-count view, and the per-IP attribute records.

Spec: section 3 (capacity, hot_ratio), section 9 (`local_metadata`), section 11
(pruning), section 12 (the upward aggregate), section 16 (`combine()` per
metadata type), section 17 (inherited metadata is never materialized), section
27 (a compressed prefix has no node), section 46 (per-IP attributes: 46.1,
46.2, 46.3, 46.5, 46.6, 46.8, 46.9).

The interface under test is ADR-0015 decisions 1-8, as re-exported from
`hammertime.trie.metadata`:

* A (decisions 1, 3): the downward algebra -- `Tags` unions, `Bitmask` ORs,
  `Override` yields the more specific operand; documents combine key-wise with
  absence as the per-key identity; associativity, identity and idempotence
  hold for every kind, commutativity only for `Tags` and `Bitmask`.
* B (decision 4, sections 16-17): `PrefixMetadataStore` keeps `local`,
  `inherited` (strict ancestors) and `effective` apart, is family-scoped, and
  -- epic #9's second acceptance criterion -- is untouched by every add,
  remove and prune in a trie beside it.
* C (decision 2, sections 3 and 12): `PrefixStats` derives `capacity` and
  `hot_ratio` from its own prefix and count; `aggregate` sums counts and is
  order-independent and associative under regrouping (epic #9's first
  acceptance criterion); `prefix_stats` / `ancestor_stats` bind the trie's
  counts element for element.
* D (decisions 5, 6, section 46): `IpAttributeRecords` is a read-only,
  family-scoped `Mapping[Address, IpAttributes]` that validates every write
  and is all-or-nothing; `apply_hot_ip_added` validates, then mutates the
  trie, then the record, so the section 46.5 invariant holds after every
  step and after every exception.
* E (Amendment 1 rulings 3-7, assumptions 44-48): what the metadata surface
  refuses -- `declare`'s malformed document (`TypeError`), a non-`Prefix`
  given to `in` (`False`), a value built with a wrong field type
  (`TypeError`), a key given two kinds on one path (`ValueError` at
  `declare`), and a combine operand that is not one of the three kinds
  (`TypeError`, checked before any key is combined).
* F (Amendment 2 rulings A-D and its follow-ups, assumptions 18, 33, 49-60;
  sections 46.2, 46.5, 46.8, 46.9): `record()` and `apply_hot_ip_added` keep
  `canonicalize_ip_attributes`'s copy, never the caller's object -- a
  subclass is stored as its base type, a lying subclass is judged and stored
  by what it holds, a hostile one gives the plain document's outcome, a
  non-`dict` mapping (`DEFAULT_ATTRIBUTES` included) is refused -- and every
  read decodes a fresh, exact-typed document that a reader cannot use to
  change the map. The validator's own rules, bounds and canonical form are
  tested in `hammertime-core`'s `test_attribute_validation.py`.

ADR-0014 (decisions 1-4, 8, 9; A1, A3, A5, A12) supplies the trie surface, and
its A12 clause 4 forbids comparing implementations, or running the checks, on
a corrupted trie -- the one corruption case here
(`test_a_corrupt_trie_raises_before_the_record_is_written`) does neither.
Nothing here is thread-safe (ADR-0015 decision 8) and no test implies it.

Choices of this file's own, not dictated by the spec or ADR-0015:

* Metadata documents and records are compared as `dict(...)`: the ADR fixes
  them as `Mapping`s and says nothing about the concrete type returned, so a
  test must not depend on `MappingProxyType.__eq__`. For the same reason
  `combine_path([])` is checked for *equality* with `EMPTY_METADATA`, not
  identity.
* "A `ValueError` naming the key and both kinds" is read as: the key appears
  verbatim, and each kind's class name (`Tags`, `Bitmask`, `Override`)
  appears case-insensitively. "Naming both families" is read as the
  `AddressFamily` values `ipv4` / `ipv6`, case-insensitively, as in
  `test_invariants.py`.
* Decision 4's family `ValueError` is applied to
  `PrefixMetadataStore.__contains__` as well as to the other methods, as
  ADR-0015 Amendment 1 ruling 2 (assumption 43) states. The only "other
  argument" that can also be wrong is `declare`'s document, so that is the
  combined case.
* In the hypothesis strategies each metadata key has one fixed kind, so a
  random document never mixes kinds under a key (that is a separate,
  explicit test) -- except in the ruling 6 property test, which draws kinds
  freely on purpose.
* "Names the key" is only meaningful for a key unlikely to occur in a message
  by accident, so wherever an Amendment 1 test pins a key in a message the
  key is a word (`flags`, `zone_marking`, `stray_policy`), never a single
  letter like `k`. "Naming that prefix" (ruling 6) is read as the prefix's
  CIDR text as `Prefix.parse` reads it, e.g. `10.0.0.0/8`, and is pinned for
  IPv4 prefixes only.
* Snapshots of the record map deep-copy every record, so a later nested
  mutation by the code under test could not make "before" and "after" agree
  by aliasing.
* Section F copies `test_attribute_validation.py`'s trap machinery rather
  than importing a test module across packages: every hostile or lying class
  consults one switch, `_Traps`, and misbehaves only inside `_armed()`; a
  document is built first and armed afterwards, because building a `dict`
  hashes its keys. Only `_Impersonator` and `_Twin`, whose lie is their hash
  and equality, lie unconditionally. A hostile document's expected outcome is
  that of its plain twin, built by the same function from the plain types,
  and an `accepted` flag pins which way the twin goes. `str(exc)` of every
  rejection is taken while the traps are still armed.
* Section F writes through two entry points: `record()` over an address that
  already has a record (so a rejection must keep it), and
  `apply_hot_ip_added` for a new address on a `PatriciaTrie` (so a rejection
  must leave the trie as well as the map). Validation runs before the trie is
  touched (decision 6), so the trie implementation does not bear on it and
  only one is used.
* Rule R1 applies to top-level keys only, so the nested lying `dict` hides an
  S4 or S6 violation rather than a `sources` key.
"""

import contextlib
import copy
import itertools
import json
import math
from collections.abc import Callable, Iterable, Iterator, Mapping
from enum import IntEnum, StrEnum
from fractions import Fraction
from types import MappingProxyType
from typing import Any, NamedTuple

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.core.errors import InvalidAttributesError, InvariantViolation
from hammertime.core.events.attributes import CanonicalAttributes, canonicalize_ip_attributes
from hammertime.testkit.generators import address_pools, addresses, prefixes, trie_operations
from hammertime.testkit.invariants import (
    assert_attribute_records_match,
    assert_hot_count_consistent,
    assert_no_negative_counts,
    assert_no_orphaned_nodes,
)
from hammertime.trie.metadata import (
    DEFAULT_ATTRIBUTES,
    EMPTY_METADATA,
    Bitmask,
    IpAttributeRecords,
    Metadata,
    Override,
    PrefixMetadataStore,
    PrefixStats,
    Tags,
    aggregate,
    ancestor_stats,
    apply_hot_ip_added,
    apply_hot_ip_removed,
    combine,
    combine_path,
    combine_values,
    prefix_stats,
)
from hammertime.trie.structure import (
    BinaryTrie,
    HotTrie,
    PatriciaTrie,
    check_attribute_records,
    check_trie,
)
from hypothesis import given, settings
from hypothesis import strategies as st

IPV4 = AddressFamily.IPV4
IPV6 = AddressFamily.IPV6

FAMILIES = [pytest.param(IPV4, id="ipv4"), pytest.param(IPV6, id="ipv6")]
IMPLEMENTATIONS = [
    pytest.param(BinaryTrie, id="binary"),
    pytest.param(PatriciaTrie, id="patricia"),
]

TrieFactory = Callable[[AddressFamily], HotTrie]
Value = Tags | Bitmask | Override

ROOT_V4 = Prefix(family=IPV4, network=0, length=0)
SLASH_8 = Prefix.parse("10.0.0.0/8")
SLASH_16 = Prefix.parse("10.20.0.0/16")
SLASH_24 = Prefix.parse("10.20.30.0/24")
HOST = Address.parse("10.20.30.40")
HOST_ROUTE = Prefix.parse("10.20.30.40/32")

# Per family: a sample address, and a second one far from it.
SAMPLE = {IPV4: Address.parse("192.168.1.42"), IPV6: Address.parse("2001:db8::2a")}
OTHER_SAMPLE = {IPV4: Address.parse("10.0.0.1"), IPV6: Address.parse("fe80::1")}
V4 = SAMPLE[IPV4]
V4_B = Address.parse("192.168.1.43")
V4_C = OTHER_SAMPLE[IPV4]
V6 = SAMPLE[IPV6]


def _prefix_of(address: Address, length: int) -> Prefix:
    host_bits = address.bit_length - length
    network = (address.value >> host_bits) << host_bits
    return Prefix(family=address.family, network=network, length=length)


def _root(family: AddressFamily) -> Prefix:
    return Prefix(family=family, network=0, length=0)


def _assert_names_both_families(error: BaseException) -> None:
    """ADR-0014 decision 1 / ADR-0015 decisions 4-6: "naming both families"."""

    message = str(error).lower()
    for family in (IPV4, IPV6):
        assert family.value in message, message


def _fold(documents: Iterable[Metadata]) -> dict[str, Value]:
    """Section 16's definition, spelled out: a left fold of `combine` from empty."""

    result: Metadata = EMPTY_METADATA
    for document in documents:
        result = combine(result, document)
    return dict(result)


# ==========================================================================
# A. The section 16 algebra (ADR-0015 decisions 1 and 3).
# ==========================================================================

MIXED_KINDS = [
    pytest.param(Tags.of("a"), Bitmask(1), id="tags-then-bitmask"),
    pytest.param(Bitmask(1), Tags.of("a"), id="bitmask-then-tags"),
    pytest.param(Tags.of("a"), Override("a"), id="tags-then-override"),
    pytest.param(Override("a"), Tags.of("a"), id="override-then-tags"),
    pytest.param(Bitmask(1), Override(1), id="bitmask-then-override"),
    pytest.param(Override(1), Bitmask(1), id="override-then-bitmask"),
]

ONE_OF_EACH_KIND = [
    pytest.param(Tags.of("internal", "lab"), id="tags"),
    pytest.param(Tags.of(), id="empty-tags"),
    pytest.param(Bitmask(0b1011), id="bitmask"),
    pytest.param(Bitmask(0), id="zero-bitmask"),
    pytest.param(Override("deny"), id="override-str"),
    pytest.param(Override(7), id="override-int"),
    pytest.param(Override(False), id="override-bool"),
]


class TestValueKinds:
    """Section 16's three examples, one value kind each; the kind carries the rule."""

    def test_tags_combine_by_set_union(self) -> None:
        combined = combine_values(Tags.of("internal", "dmz"), Tags.of("dmz", "lab"))
        assert combined == Tags.of("internal", "dmz", "lab")
        assert combined == Tags(frozenset({"internal", "dmz", "lab"}))

    def test_bitmask_combines_by_bitwise_or(self) -> None:
        assert combine_values(Bitmask(0b0101), Bitmask(0b0011)) == Bitmask(0b0111)
        assert combine_values(Bitmask(0), Bitmask(0b1000)) == Bitmask(0b1000)

    @pytest.mark.parametrize(
        ("less", "more"),
        [
            pytest.param("deny", "allow", id="str"),
            pytest.param(1, 2, id="int"),
            pytest.param(True, False, id="bool"),
        ],
    )
    def test_override_yields_the_more_specific_operand(
        self, less: str | int | bool, more: str | int | bool
    ) -> None:
        assert combine_values(Override(less), Override(more)) == Override(more)
        assert combine_values(Override(more), Override(less)) == Override(less)

    def test_tags_of_builds_the_same_value_as_the_constructor(self) -> None:
        assert Tags.of("a", "b") == Tags(frozenset({"a", "b"}))
        assert Tags.of("a", "b").values == frozenset({"a", "b"})
        assert Tags.of("a", "a") == Tags.of("a")

    def test_a_negative_bitmask_is_a_value_error(self) -> None:
        """ADR-0015 assumption 5."""

        with pytest.raises(ValueError):
            Bitmask(-1)
        assert Bitmask(0).bits == 0
        assert Override("x").value == "x"

    @pytest.mark.parametrize(("less", "more"), MIXED_KINDS)
    def test_two_different_kinds_do_not_combine(self, less: Value, more: Value) -> None:
        with pytest.raises(ValueError):
            combine_values(less, more)

    @pytest.mark.parametrize("value", ONE_OF_EACH_KIND)
    def test_every_kind_is_idempotent(self, value: Value) -> None:
        """ADR-0015 decision 3 / assumption 4: required of every kind."""

        assert combine_values(value, value) == value
        assert dict(combine({"k": value}, {"k": value})) == {"k": value}


class TestDocumentCombine:
    """ADR-0015 decision 3: a document combines key-wise; absence is the identity."""

    def test_a_key_in_one_operand_passes_through_and_a_shared_key_uses_its_rule(self) -> None:
        less: dict[str, Value] = {
            "labels": Tags.of("internal"),
            "flags": Bitmask(0b01),
            "policy": Override("deny"),
            "only_less": Override(7),
        }
        more: dict[str, Value] = {
            "labels": Tags.of("lab"),
            "flags": Bitmask(0b10),
            "policy": Override("allow"),
            "only_more": Bitmask(4),
        }
        assert dict(combine(less, more)) == {
            "labels": Tags.of("internal", "lab"),
            "flags": Bitmask(0b11),
            "policy": Override("allow"),
            "only_less": Override(7),
            "only_more": Bitmask(4),
        }

    @pytest.mark.parametrize(("less", "more"), MIXED_KINDS)
    def test_two_kinds_under_one_key_is_a_value_error_naming_the_key(
        self, less: Value, more: Value
    ) -> None:
        key = "egress_zone_policy"
        with pytest.raises(ValueError) as excinfo:
            combine({key: less, "labels": Tags.of("x")}, {key: more})
        message = str(excinfo.value)
        assert key in message, message
        for kind in (type(less).__name__, type(more).__name__):
            assert kind.lower() in message.lower(), message

    def test_empty_metadata_is_empty(self) -> None:
        assert dict(EMPTY_METADATA) == {}
        assert len(EMPTY_METADATA) == 0

    def test_combine_path_of_nothing_is_empty_metadata(self) -> None:
        assert dict(combine_path([])) == {}
        assert dict(combine_path([])) == dict(EMPTY_METADATA)

    def test_combine_path_folds_least_specific_first_so_the_last_override_wins(self) -> None:
        documents: list[dict[str, Value]] = [
            {"policy": Override("deny"), "labels": Tags.of("root")},
            {"policy": Override("log")},
            {"labels": Tags.of("leaf")},
            {"policy": Override("allow")},
        ]
        assert dict(combine_path(documents)) == {
            "policy": Override("allow"),
            "labels": Tags.of("root", "leaf"),
        }
        assert dict(combine_path(reversed(documents))) == {
            "policy": Override("deny"),
            "labels": Tags.of("root", "leaf"),
        }

    def test_combine_path_accepts_any_iterable(self) -> None:
        documents: list[dict[str, Value]] = [{"flags": Bitmask(1)}, {"flags": Bitmask(2)}]
        assert dict(combine_path(d for d in documents)) == {"flags": Bitmask(3)}

    def test_override_breaks_commutativity_by_design(self) -> None:
        """The named counterexample: ADR-0015 decision 1 -- section 16's policy
        semantics are order-dependent, and must not be made commutative."""

        deny: dict[str, Value] = {"policy": Override("deny")}
        allow: dict[str, Value] = {"policy": Override("allow")}
        assert dict(combine(deny, allow)) == {"policy": Override("allow")}
        assert dict(combine(allow, deny)) == {"policy": Override("deny")}
        assert dict(combine(deny, allow)) != dict(combine(allow, deny))


_TAG_NAMES = ("internal", "dmz", "lab", "vip")
_TAGS: st.SearchStrategy[Tags] = st.frozensets(st.sampled_from(_TAG_NAMES), max_size=3).map(Tags)
_BITMASKS: st.SearchStrategy[Bitmask] = st.integers(min_value=0, max_value=0xFF).map(Bitmask)
_OVERRIDES: st.SearchStrategy[Override] = st.one_of(
    st.sampled_from(("allow", "deny", "log")),
    st.integers(min_value=-3, max_value=3),
    st.booleans(),
).map(Override)

# One fixed kind per key: a random document never mixes kinds under a key.
_ORDER_FREE_KEYS: dict[str, st.SearchStrategy[Value]] = {
    "labels": _TAGS,
    "zones": _TAGS,
    "flags": _BITMASKS,
    "caps": _BITMASKS,
}
_ALL_KEYS: dict[str, st.SearchStrategy[Value]] = {
    **_ORDER_FREE_KEYS,
    "policy": _OVERRIDES,
    "tier": _OVERRIDES,
}
_NO_REQUIRED_KEYS: dict[str, st.SearchStrategy[Value]] = {}
DOCUMENTS = st.fixed_dictionaries(_NO_REQUIRED_KEYS, optional=_ALL_KEYS)


@settings(deadline=None, max_examples=150)
@given(a=DOCUMENTS, b=DOCUMENTS, c=DOCUMENTS)
def test_combine_is_associative_over_every_kind(
    a: dict[str, Value], b: dict[str, Value], c: dict[str, Value]
) -> None:
    """ADR-0015 decision 3's associativity law, Override keys included."""

    left = dict(combine(combine(a, b), c))
    assert left == dict(combine(a, combine(b, c)))
    assert left == dict(combine_path([a, b, c]))


@settings(deadline=None, max_examples=100)
@given(document=DOCUMENTS)
def test_empty_metadata_is_the_identity_on_both_sides(document: dict[str, Value]) -> None:
    assert dict(combine(EMPTY_METADATA, document)) == document
    assert dict(combine(document, EMPTY_METADATA)) == document
    assert dict(combine({}, document)) == document
    assert dict(combine_path([document])) == document


@settings(deadline=None, max_examples=100)
@given(document=DOCUMENTS)
def test_combine_is_idempotent(document: dict[str, Value]) -> None:
    assert dict(combine(document, document)) == document


@settings(deadline=None, max_examples=150)
@given(a=DOCUMENTS, b=DOCUMENTS)
def test_combine_commutes_when_every_shared_key_is_tags_or_bitmask(
    a: dict[str, Value], b: dict[str, Value]
) -> None:
    # An Override key may appear in either operand, just never in both.
    b = {key: value for key, value in b.items() if not (isinstance(value, Override) and key in a)}
    assert dict(combine(a, b)) == dict(combine(b, a))


@settings(deadline=None, max_examples=100)
@given(documents=st.lists(DOCUMENTS, max_size=6))
def test_combine_path_is_the_left_fold_of_combine(documents: list[dict[str, Value]]) -> None:
    assert dict(combine_path(documents)) == _fold(documents)


# ==========================================================================
# B. The prefix-keyed store: local, inherited, effective (decision 4,
#    sections 16 and 17).
# ==========================================================================


def _layered_store() -> PrefixMetadataStore:
    store = PrefixMetadataStore(IPV4)
    store.declare(ROOT_V4, {"labels": Tags.of("everywhere"), "flags": Bitmask(0b0001)})
    store.declare(SLASH_8, {"labels": Tags.of("internal"), "policy": Override("deny")})
    store.declare(SLASH_16, {"flags": Bitmask(0b0100)})
    store.declare(SLASH_24, {"policy": Override("allow"), "labels": Tags.of("lab")})
    store.declare(HOST_ROUTE, {"labels": Tags.of("host")})
    return store


def _declared(store: PrefixMetadataStore) -> list[tuple[Prefix, dict[str, Value]]]:
    return [(prefix, dict(document)) for prefix, document in store.declarations()]


def test_section_17_example_is_stored_once_and_read_through_the_path() -> None:
    """Section 17: `10.0.0.0/8 -> {"internal"}` is stored on the /8 alone and
    accumulated by lookup, however many addresses beneath it are HOT."""

    store = PrefixMetadataStore(IPV4)
    assert store.family is IPV4
    store.declare(SLASH_8, {"labels": Tags.of("internal")})

    trie = PatriciaTrie(IPV4)
    hot = [Address.parse(f"10.{i}.{i}.{i}") for i in range(1, 201)]
    for address in hot:
        assert trie.add_hot_ip(address) is True
    assert trie.hot_ip_count == 200

    for address in [*hot, Address.parse("10.0.0.0"), Address.parse("10.255.255.255")]:
        assert dict(store.effective(address)) == {"labels": Tags.of("internal")}
    assert dict(store.effective(Address.parse("192.168.1.1"))) == {}
    assert dict(store.effective(Address.parse("11.0.0.0"))) == {}

    assert len(store) == 1
    assert _declared(store) == [(SLASH_8, {"labels": Tags.of("internal")})]
    assert SLASH_8 in store
    assert SLASH_16 not in store
    assert dict(store.local(SLASH_16)) == {}


def test_local_is_unchanged_by_declarations_on_ancestors_and_descendants() -> None:
    store = PrefixMetadataStore(IPV4)
    own: dict[str, Value] = {"flags": Bitmask(0b0100), "policy": Override("log")}
    store.declare(SLASH_16, own)
    assert dict(store.local(SLASH_16)) == own

    store.declare(ROOT_V4, {"flags": Bitmask(0b0001)})
    store.declare(SLASH_8, {"policy": Override("deny"), "labels": Tags.of("internal")})
    store.declare(SLASH_24, {"policy": Override("allow")})
    store.declare(HOST_ROUTE, {"labels": Tags.of("host")})
    store.declare(Prefix.parse("10.21.0.0/16"), {"flags": Bitmask(0b1000)})
    assert dict(store.local(SLASH_16)) == own

    for prefix in (ROOT_V4, SLASH_8, SLASH_24, HOST_ROUTE):
        assert store.revoke(prefix) is True
    assert dict(store.local(SLASH_16)) == own


def test_inherited_is_strict_ancestors_and_effective_adds_local() -> None:
    store = _layered_store()

    assert dict(store.inherited(ROOT_V4)) == {}
    assert dict(store.inherited(SLASH_8)) == {
        "labels": Tags.of("everywhere"),
        "flags": Bitmask(0b0001),
    }
    # The /16's own flags and everything declared below it are excluded.
    assert dict(store.inherited(SLASH_16)) == {
        "labels": Tags.of("everywhere", "internal"),
        "flags": Bitmask(0b0001),
        "policy": Override("deny"),
    }
    assert dict(store.effective_for_prefix(SLASH_16)) == {
        "labels": Tags.of("everywhere", "internal"),
        "flags": Bitmask(0b0101),
        "policy": Override("deny"),
    }

    undeclared = [
        Prefix.parse("10.20.0.0/17"),
        Prefix.parse("10.20.30.0/25"),
        Prefix.parse("10.20.30.41/32"),
        Prefix.parse("192.168.0.0/16"),
    ]
    for prefix in [ROOT_V4, SLASH_8, SLASH_16, SLASH_24, HOST_ROUTE, *undeclared]:
        expected = dict(combine(store.inherited(prefix), store.local(prefix)))
        assert dict(store.effective_for_prefix(prefix)) == expected, prefix


def test_effective_folds_slash_0_through_the_host_route_inclusive() -> None:
    store = _layered_store()
    everything = {
        "labels": Tags.of("everywhere", "internal", "lab", "host"),
        "flags": Bitmask(0b0101),
        "policy": Override("allow"),
    }
    assert dict(store.effective(HOST)) == everything
    assert dict(store.effective(HOST)) == dict(store.effective_for_prefix(HOST_ROUTE))

    neighbour = Address.parse("10.20.30.41")
    assert dict(store.effective(neighbour)) == {
        "labels": Tags.of("everywhere", "internal", "lab"),
        "flags": Bitmask(0b0101),
        "policy": Override("allow"),
    }
    # Only the /0 declaration covers an unrelated address.
    assert dict(store.effective(Address.parse("192.168.1.1"))) == {
        "labels": Tags.of("everywhere"),
        "flags": Bitmask(0b0001),
    }


@pytest.mark.parametrize("family", FAMILIES)
def test_effective_for_every_address_is_its_host_route(family: AddressFamily) -> None:
    address = SAMPLE[family]
    store = PrefixMetadataStore(family)
    store.declare(_root(family), {"labels": Tags.of("root")})
    store.declare(_prefix_of(address, family.bit_length), {"labels": Tags.of("host")})
    assert dict(store.effective(address)) == {"labels": Tags.of("root", "host")}
    assert dict(store.effective(address)) == dict(
        store.effective_for_prefix(_prefix_of(address, family.bit_length))
    )
    assert dict(store.inherited(_prefix_of(address, family.bit_length))) == {
        "labels": Tags.of("root")
    }
    assert dict(store.effective(OTHER_SAMPLE[family])) == {"labels": Tags.of("root")}


def test_the_more_specific_override_wins_in_effective_but_not_in_local() -> None:
    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_8, {"policy": Override("deny")})
    store.declare(SLASH_24, {"policy": Override("allow")})

    assert dict(store.effective(HOST)) == {"policy": Override("allow")}
    assert dict(store.effective(Address.parse("10.99.0.1"))) == {"policy": Override("deny")}
    assert dict(store.local(SLASH_8)) == {"policy": Override("deny")}
    assert dict(store.local(SLASH_24)) == {"policy": Override("allow")}
    assert dict(store.inherited(SLASH_24)) == {"policy": Override("deny")}


@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_local_metadata_survives_every_trie_mutation_including_the_prune(
    make_trie: TrieFactory,
) -> None:
    """Epic #9's second acceptance criterion (ADR-0015 decision 4): a
    declaration lives beside the trie, so neither the adds beneath it nor the
    removals that prune its prefix's node (ADR-0014 decision 4) touch it."""

    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_8, {"labels": Tags.of("internal"), "policy": Override("deny")})
    store.declare(SLASH_24, {"policy": Override("allow")})
    hot = [
        Address.parse(text)
        for text in ("10.20.30.1", "10.20.30.2", "10.20.30.200", "10.99.0.1", "10.255.255.255")
    ]
    probes = [ROOT_V4, SLASH_8, SLASH_16, SLASH_24, HOST_ROUTE]

    def reads() -> tuple[object, ...]:
        per_prefix = tuple(
            (dict(store.local(p)), dict(store.inherited(p)), dict(store.effective_for_prefix(p)))
            for p in probes
        )
        per_address = tuple(dict(store.effective(a)) for a in [*hot, HOST])
        return per_prefix, per_address, len(store), _declared(store)

    before = reads()
    trie = make_trie(IPV4)
    for address in hot:
        assert trie.add_hot_ip(address) is True
        assert reads() == before
    assert trie.hot_count(SLASH_24) == 3
    assert trie.hot_count(SLASH_8) == 5

    for address in hot:
        assert trie.remove_hot_ip(address) is True
        assert reads() == before
    assert trie.hot_ip_count == 0
    assert trie.node_count == 0
    assert trie.hot_count(SLASH_8) == 0
    assert reads() == before
    assert dict(store.local(SLASH_8)) == {"labels": Tags.of("internal"), "policy": Override("deny")}
    assert len(store) == 2


def test_a_prefix_that_was_never_hot_can_carry_a_declaration() -> None:
    """Section 27 / decision 4: a prefix need not have (or ever have had) a node."""

    never_hot = Prefix.parse("172.16.0.0/12")
    store = PrefixMetadataStore(IPV4)
    store.declare(never_hot, {"labels": Tags.of("rfc1918")})
    assert never_hot in store
    assert dict(store.local(never_hot)) == {"labels": Tags.of("rfc1918")}
    assert dict(store.effective(Address.parse("172.16.5.5"))) == {"labels": Tags.of("rfc1918")}


STORE_METHODS = [
    "declare",
    "declare-with-a-bad-document",
    "revoke",
    "local",
    "inherited",
    "effective_for_prefix",
    "effective",
    "contains",
]


def _call_store(store: PrefixMetadataStore, method: str, prefix: Prefix, address: Address) -> None:
    if method == "declare":
        store.declare(prefix, {"labels": Tags.of("x")})
    elif method == "declare-with-a-bad-document":
        store.declare(prefix, 42)  # type: ignore[arg-type]
    elif method == "revoke":
        store.revoke(prefix)
    elif method == "local":
        store.local(prefix)
    elif method == "inherited":
        store.inherited(prefix)
    elif method == "effective_for_prefix":
        store.effective_for_prefix(prefix)
    elif method == "effective":
        store.effective(address)
    elif method == "contains":
        _ = prefix in store
    else:
        raise AssertionError(method)


@pytest.mark.parametrize("method", STORE_METHODS)
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
@pytest.mark.parametrize("family", FAMILIES)
def test_every_store_method_rejects_the_other_family(
    family: AddressFamily, populated: bool, method: str
) -> None:
    """Decision 4: a `Prefix` or `Address` of the other family is a
    `ValueError` naming both families, checked before any other argument."""

    store = PrefixMetadataStore(family)
    if populated:
        store.declare(_root(family), {"labels": Tags.of("root")})
        store.declare(_prefix_of(SAMPLE[family], 8), {"flags": Bitmask(1)})
    before = (len(store), _declared(store))

    other_address = SAMPLE[IPV6 if family is IPV4 else IPV4]
    other_prefix = _prefix_of(other_address, 8)
    with pytest.raises(ValueError) as excinfo:
        _call_store(store, method, other_prefix, other_address)
    _assert_names_both_families(excinfo.value)
    assert (len(store), _declared(store)) == before


def test_declare_replaces_and_revoke_reports_whether_anything_was_removed() -> None:
    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_8, {"labels": Tags.of("old")})
    store.declare(SLASH_8, {"flags": Bitmask(2)})
    assert dict(store.local(SLASH_8)) == {"flags": Bitmask(2)}
    assert len(store) == 1

    assert store.revoke(SLASH_8) is True
    assert store.revoke(SLASH_8) is False
    assert SLASH_8 not in store
    assert len(store) == 0
    assert dict(store.local(SLASH_8)) == {}
    assert store.revoke(SLASH_24) is False


def test_an_empty_document_is_a_declaration_not_a_revoke() -> None:
    """ADR-0015 assumption 10."""

    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_8, EMPTY_METADATA)
    assert SLASH_8 in store
    assert len(store) == 1
    assert _declared(store) == [(SLASH_8, {})]


def test_clear_empties_the_store() -> None:
    store = _layered_store()
    assert len(store) == 5
    store.clear()
    assert len(store) == 0
    assert store.declarations() == ()
    assert dict(store.effective(HOST)) == {}
    assert SLASH_8 not in store


def test_declarations_are_sorted_by_length_then_network() -> None:
    store = PrefixMetadataStore(IPV4)
    order = [
        "10.20.30.0/24",
        "10.0.0.0/8",
        "0.0.0.0/0",
        "192.168.0.0/16",
        "10.20.0.0/16",
        "9.0.0.0/8",
    ]
    for text in order:
        store.declare(Prefix.parse(text), {"labels": Tags.of(text)})
    assert [prefix for prefix, _document in store.declarations()] == [
        Prefix.parse(text)
        for text in (
            "0.0.0.0/0",
            "9.0.0.0/8",
            "10.0.0.0/8",
            "10.20.0.0/16",
            "192.168.0.0/16",
            "10.20.30.0/24",
        )
    ]
    assert isinstance(store.declarations(), tuple)
    for prefix, document in store.declarations():
        assert dict(document) == {"labels": Tags.of(str(prefix))}


def test_mutating_the_document_after_declare_changes_nothing_stored() -> None:
    """Decision 4: `declare` stores an immutable copy."""

    document: dict[str, Value] = {"labels": Tags.of("internal")}
    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_8, document)
    document["labels"] = Tags.of("tampered")
    document["flags"] = Bitmask(0xFF)
    del document["labels"]
    assert dict(store.local(SLASH_8)) == {"labels": Tags.of("internal")}
    assert dict(store.effective(HOST)) == {"labels": Tags.of("internal")}


@pytest.mark.parametrize("family", FAMILIES)
@settings(deadline=None, max_examples=60)
@given(data=st.data())
def test_the_store_matches_a_model_of_the_path_fold(
    family: AddressFamily, data: st.DataObject
) -> None:
    """Sections 16-17 against a plain model: `local` is the declaration alone,
    `inherited` folds the strict ancestors least-specific first, and
    `effective` folds `/0` through `/bit_length` inclusive."""

    probe = data.draw(addresses(family))
    lengths = data.draw(st.sets(st.integers(min_value=0, max_value=family.bit_length), max_size=6))
    on_path = sorted((_prefix_of(probe, length) for length in lengths), key=lambda p: p.length)
    elsewhere = data.draw(st.lists(prefixes(family), max_size=4))
    targets = data.draw(st.permutations([*on_path, *elsewhere]))

    store = PrefixMetadataStore(family)
    model: dict[Prefix, dict[str, Value]] = {}
    for prefix in targets:
        document = data.draw(DOCUMENTS)
        store.declare(prefix, document)
        model[prefix] = document

    by_length = sorted(model, key=lambda p: p.length)
    assert dict(store.effective(probe)) == _fold(model[p] for p in by_length if p.contains(probe))
    assert len(store) == len(model)
    assert [p for p, _document in store.declarations()] == sorted(
        model, key=lambda p: (p.length, p.network)
    )

    for prefix in [*model, *on_path, _prefix_of(probe, family.bit_length)]:
        network = Address(family=family, value=prefix.network)
        ancestors = [q for q in by_length if q.length < prefix.length and q.contains(network)]
        assert dict(store.local(prefix)) == model.get(prefix, {})
        assert dict(store.inherited(prefix)) == _fold(model[q] for q in ancestors)
        assert dict(store.effective_for_prefix(prefix)) == dict(
            combine(store.inherited(prefix), store.local(prefix))
        )


# ==========================================================================
# C. The upward aggregate and PrefixStats (decision 2, sections 3 and 12).
# ==========================================================================


def _stats(prefix: Prefix, hot_count: int) -> PrefixStats:
    return PrefixStats(prefix=prefix, hot_count=hot_count)


class TestPrefixStats:
    def test_the_read_api_example(self) -> None:
        """read-api-v1.md's `GET /prefix` example: 156 on a /24 is 0.609375."""

        prefix = Prefix.parse("10.20.30.0/24")
        stats = _stats(prefix, 156)
        assert stats.prefix == prefix
        assert stats.hot_count == 156
        assert stats.capacity == 256
        assert stats.hot_ratio == 0.609375
        assert stats.hot_ratio == prefix.hot_ratio(156)
        assert stats.hot_ratio_exact == Fraction(156, 256)
        assert isinstance(stats.hot_ratio_exact, Fraction)

    @pytest.mark.parametrize("family", FAMILIES)
    def test_capacity_is_two_to_the_host_bits_at_every_length(self, family: AddressFamily) -> None:
        for length in range(family.bit_length + 1):
            prefix = _prefix_of(SAMPLE[family], length)
            stats = _stats(prefix, 0)
            assert stats.capacity == 2 ** (family.bit_length - length)
            assert stats.capacity == prefix.capacity()
            assert stats.hot_ratio == 0.0
            assert stats.hot_ratio_exact == 0

    def test_ipv6_slash_0_is_exact(self) -> None:
        """Section 3: no overflow; the exact ratio keeps what the float loses."""

        prefix = _root(IPV6)
        stats = _stats(prefix, 3)
        assert stats.capacity == 2**128
        assert stats.hot_ratio_exact == Fraction(3, 2**128)
        assert stats.hot_ratio == prefix.hot_ratio(3)

    @pytest.mark.parametrize("family", FAMILIES)
    def test_a_hot_host_route_is_full(self, family: AddressFamily) -> None:
        stats = _stats(_prefix_of(SAMPLE[family], family.bit_length), 1)
        assert stats.capacity == 1
        assert stats.hot_ratio == 1.0
        assert stats.hot_ratio_exact == 1

    def test_a_negative_count_is_a_value_error(self) -> None:
        """Section 11 / decision 2: an argument check, not a clamp."""

        with pytest.raises(ValueError):
            _stats(SLASH_24, -1)


PARENT = Prefix.parse("10.0.0.0/22")


def _slash_28(block: int, index: int) -> Prefix:
    return Prefix(family=IPV4, network=PARENT.network + block * 256 + index * 16, length=28)


def _slash_24(block: int) -> Prefix:
    return Prefix.parse(f"10.0.{block}.0/24")


def _slash_23(half: int) -> Prefix:
    return Prefix.parse(f"10.0.{2 * half}.0/23")


_PARTS = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=3),
        st.integers(min_value=0, max_value=15),
        st.integers(min_value=0, max_value=16),
    ),
    max_size=12,
)


class TestAggregate:
    def test_no_parts_is_zero(self) -> None:
        assert aggregate(PARENT, []) == _stats(PARENT, 0)

    def test_a_part_may_be_the_parent_itself(self) -> None:
        assert aggregate(PARENT, [_stats(PARENT, 3)]) == _stats(PARENT, 3)

    def test_the_ratio_is_recomputed_at_the_parent_never_combined(self) -> None:
        """Decision 2's worked example: a /24 at 128 beside an empty /24."""

        children = [
            _stats(Prefix.parse("10.0.0.0/24"), 128),
            _stats(Prefix.parse("10.0.1.0/24"), 0),
        ]
        assert children[0].hot_ratio_exact == Fraction(1, 2)
        parent = aggregate(Prefix.parse("10.0.0.0/23"), children)
        assert parent.hot_count == 128
        assert parent.hot_ratio_exact == Fraction(128, 512)
        assert parent.hot_ratio == 0.25
        assert parent.hot_ratio_exact != sum(c.hot_ratio_exact for c in children)

    def test_ratios_are_neither_summed_nor_averaged(self) -> None:
        """Decision 2's unequal-parts example (Amendment 1 ruling 1): a /24 at
        128 beside a /25 at 64 under a /23 is 37.5 %, not the sum or the mean."""

        # Parts of unequal size, both at 1/2: sum 1, mean 1/2, true ratio 3/8.
        parts = [_stats(Prefix.parse("10.0.0.0/24"), 128), _stats(Prefix.parse("10.0.1.0/25"), 64)]
        assert [p.hot_ratio_exact for p in parts] == [Fraction(1, 2), Fraction(1, 2)]
        parent = aggregate(Prefix.parse("10.0.0.0/23"), parts)
        assert parent.hot_count == 192
        assert parent.hot_ratio_exact == Fraction(3, 8)
        assert parent.hot_ratio_exact != Fraction(1)
        assert parent.hot_ratio_exact != Fraction(1, 2)

    @pytest.mark.parametrize(
        "part",
        [
            pytest.param(Prefix.parse("10.1.0.0/24"), id="disjoint"),
            pytest.param(Prefix.parse("10.0.0.0/8"), id="shorter-than-the-parent"),
            pytest.param(Prefix.parse("10.0.4.0/22"), id="sibling-of-the-parent"),
            pytest.param(Prefix.parse("2001:db8::/32"), id="other-family"),
        ],
    )
    def test_a_part_outside_the_parent_is_a_value_error(self, part: Prefix) -> None:
        with pytest.raises(ValueError):
            aggregate(PARENT, [_stats(_slash_24(0), 1), _stats(part, 1)])


@settings(deadline=None, max_examples=100)
@given(parts=_PARTS, data=st.data())
def test_aggregate_is_order_independent_and_associative_under_regrouping(
    parts: list[tuple[int, int, int]], data: st.DataObject
) -> None:
    """Epic #9's first acceptance criterion, bound to the upward aggregate
    (ADR-0015 decisions 1 and 2): any order, any grouping, one answer."""

    stats = [_stats(_slash_28(block, index), count) for block, index, count in parts]
    flat = aggregate(PARENT, stats)
    assert flat == _stats(PARENT, sum(count for _block, _index, count in parts))

    shuffled = data.draw(st.permutations(stats))
    assert aggregate(PARENT, shuffled) == flat
    assert aggregate(PARENT, iter(shuffled)) == flat

    groups: dict[int, list[PrefixStats]] = {block: [] for block in range(4)}
    for (block, _index, _count), part in zip(parts, stats, strict=True):
        groups[block].append(part)
    by_24 = [aggregate(_slash_24(block), groups[block]) for block in range(4)]
    assert aggregate(PARENT, by_24) == flat

    by_23 = [aggregate(_slash_23(half), by_24[2 * half : 2 * half + 2]) for half in range(2)]
    assert aggregate(PARENT, by_23) == flat

    # A lopsided grouping: one /23 from its /28s directly, the rest as /24s.
    left = aggregate(_slash_23(0), groups[0] + groups[1])
    assert aggregate(PARENT, [left, by_24[2], by_24[3]]) == flat


def _populated_v4(make_trie: TrieFactory) -> tuple[HotTrie, list[Address]]:
    trie = make_trie(IPV4)
    hot = [
        Address.parse(text)
        for text in ("10.20.30.1", "10.20.30.2", "10.20.30.40", "10.99.0.1", "192.168.1.42")
    ]
    for address in hot:
        assert trie.add_hot_ip(address) is True
    return trie, hot


@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_prefix_stats_reads_the_tries_count(make_trie: TrieFactory) -> None:
    trie, hot = _populated_v4(make_trie)
    no_node = Prefix.parse("172.16.0.0/12")
    probes = {ROOT_V4, SLASH_8, SLASH_16, SLASH_24, no_node, Prefix.parse("10.20.31.0/24")}
    for address in hot:
        probes.update(_prefix_of(address, length) for length in range(33))
    for prefix in probes:
        assert prefix_stats(trie, prefix) == _stats(prefix, trie.hot_count(prefix)), prefix
    assert prefix_stats(trie, no_node) == _stats(no_node, 0)
    assert prefix_stats(trie, SLASH_24).hot_count == 3
    assert prefix_stats(trie, ROOT_V4).hot_count == 5


@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_ancestor_stats_maps_ancestor_counts_element_for_element(make_trie: TrieFactory) -> None:
    """Decision 2 / ADR-0010 decision 3: 25 entries for IPv4 at min_length 8,
    ascending, zero counts included."""

    trie, hot = _populated_v4(make_trie)
    cold_nearby = Address.parse("10.20.30.99")
    cold_far = Address.parse("172.16.0.1")
    for address in (hot[0], hot[-1], cold_nearby, cold_far):
        stats = ancestor_stats(trie, address, min_length=8)
        counts = trie.ancestor_counts(address, min_length=8)
        assert isinstance(stats, tuple)
        assert len(stats) == 25
        assert [s.prefix.length for s in stats] == list(range(8, 33))
        assert [(s.prefix, s.hot_count) for s in stats] == [(c.prefix, c.hot_count) for c in counts]
        for s in stats:
            assert s == _stats(s.prefix, s.hot_count)
    assert all(s.hot_count == 0 for s in ancestor_stats(trie, cold_far, min_length=8))
    assert len(ancestor_stats(trie, hot[0])) == 33


@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_ancestor_stats_covers_every_ipv6_length(make_trie: TrieFactory) -> None:
    trie = make_trie(IPV6)
    assert trie.add_hot_ip(V6) is True
    stats = ancestor_stats(trie, V6)
    assert [s.prefix.length for s in stats] == list(range(129))
    assert all(s.hot_count == 1 for s in stats)
    assert stats[0].capacity == 2**128


@pytest.mark.parametrize(
    ("family", "min_length"),
    [
        pytest.param(IPV4, -1, id="ipv4-minus-1"),
        pytest.param(IPV4, 33, id="ipv4-33"),
        pytest.param(IPV6, -1, id="ipv6-minus-1"),
        pytest.param(IPV6, 129, id="ipv6-129"),
    ],
)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_ancestor_stats_rejects_a_min_length_out_of_range(
    make_trie: TrieFactory, family: AddressFamily, min_length: int
) -> None:
    trie = make_trie(family)
    assert trie.add_hot_ip(SAMPLE[family]) is True
    with pytest.raises(ValueError):
        ancestor_stats(trie, SAMPLE[family], min_length=min_length)
    assert len(ancestor_stats(trie, SAMPLE[family], min_length=family.bit_length)) == 1


# ==========================================================================
# D. The section 46 record map and the coupled step (decisions 5 and 6).
# ==========================================================================


def _compact_size(document: Mapping[str, object]) -> int:
    """Decision 5, S6: the compact, ASCII-escaped encoding the codec writes."""

    return len(json.dumps(dict(document), separators=(",", ":")).encode("utf-8"))


def _records_state(
    records: IpAttributeRecords,
) -> tuple[int, int, dict[Address, dict[str, object]]]:
    stored = {address: copy.deepcopy(dict(records[address])) for address in records}
    return len(records), records.serialized_bytes, stored


def _trie_state(trie: HotTrie) -> tuple[object, ...]:
    return (
        trie.hot_ip_count,
        trie.node_count,
        list(trie.iter_hot_addresses()),
        list(trie.iter_prefix_counts()),
    )


def _check_coupled(trie: HotTrie, records: IpAttributeRecords) -> None:
    """Section 46.5's two lines, by both checkers, plus section 46.8's byte total."""

    check_trie(trie)
    check_attribute_records(trie, records)
    assert_hot_count_consistent(trie)
    assert_no_negative_counts(trie)
    assert_no_orphaned_nodes(trie)
    assert_attribute_records_match(trie, records)
    assert set(records) == set(trie.iter_hot_addresses())
    assert len(records) == trie.hot_ip_count
    assert records.serialized_bytes == sum(_compact_size(records[a]) for a in records)


def _padded(size: int) -> dict[str, object]:
    """A version-1 document whose compact encoding is exactly `size` bytes."""

    base: dict[str, object] = {"attributes_version": 1, "x_blob": ""}
    document: dict[str, object] = {
        "attributes_version": 1,
        "x_blob": "a" * (size - _compact_size(base)),
    }
    assert _compact_size(document) == size
    return document


def _seventeen_keys() -> dict[str, object]:
    document: dict[str, object] = {"attributes_version": 1}
    document.update({f"x_key_{i}": i for i in range(16)})
    assert len(document) == 17
    return document


REJECTED_DOCUMENTS = [
    pytest.param({}, id="empty-dict"),
    pytest.param({"attributes_version": 1, "wieght": 5}, id="misspelt-weight"),
    pytest.param(
        {"attributes_version": 1, "sources": [{"system": "s", "at": "2026-09-14T10:05:00Z"}]},
        id="reserved-sources",
    ),
    pytest.param(_padded(1025), id="1025-bytes"),
    pytest.param(_seventeen_keys(), id="17-keys"),
    pytest.param({"attributes_version": 1, "x_pair": (1, 2)}, id="tuple-value"),
    pytest.param([("attributes_version", 1)], id="list-of-pairs"),
    pytest.param("attributes_version", id="string"),
]

_TEXT = st.text(alphabet="abcxyz019 _-", max_size=12)
_JSON_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    _TEXT,
)
_JSON_VALUES = st.recursive(
    _JSON_LEAVES,
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(_TEXT, inner, max_size=3)),
    max_leaves=6,
)
_REQUIRED_ATTRIBUTES: dict[str, st.SearchStrategy[object]] = {"attributes_version": st.just(1)}
_OPTIONAL_ATTRIBUTES: dict[str, st.SearchStrategy[object]] = {
    "weight": st.integers(min_value=0, max_value=1_000_000),
    "x_note": _TEXT,
    "x_data": _JSON_VALUES,
}
VALID_ATTRIBUTES = st.one_of(
    st.none(),
    st.fixed_dictionaries(_REQUIRED_ATTRIBUTES, optional=_OPTIONAL_ATTRIBUTES),
)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
@settings(deadline=None, max_examples=40)
@given(data=st.data())
def test_a_record_exists_exactly_while_its_address_is_hot(
    make_trie: TrieFactory, family: AddressFamily, data: st.DataObject
) -> None:
    """Section 46.5: `set(record.keys())` is the HOT set and `len(record) ==
    hot_count(root)` after every coupled step, redundant ones included; the
    record is the most recent add's document; section 46.8's byte total is the
    sum of the stored records' compact sizes."""

    pool = data.draw(address_pools(family))
    operations = data.draw(trie_operations(pool))
    trie = make_trie(family)
    records = IpAttributeRecords(family)
    assert records.family is family
    model: dict[Address, dict[str, object]] = {}
    _check_coupled(trie, records)

    for operation in operations:
        address = operation.address
        was_hot = address in model
        if operation.kind == "add":
            document = data.draw(VALID_ATTRIBUTES)
            assert apply_hot_ip_added(trie, records, address, document) is (not was_hot)
            model[address] = (
                {"attributes_version": 1} if document is None else copy.deepcopy(document)
            )
        else:
            assert apply_hot_ip_removed(trie, records, address) is was_hot
            model.pop(address, None)
        _check_coupled(trie, records)
        assert {a: dict(records[a]) for a in records} == model


@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_a_redelivered_add_returns_false_and_still_replaces_the_record(
    make_trie: TrieFactory,
) -> None:
    """ADR-0014 decision 3 / section 46.5: the return value is the trie's;
    the record is written whatever it said."""

    trie = make_trie(IPV4)
    records = IpAttributeRecords(IPV4)
    first = {"attributes_version": 1, "weight": 10}
    second = {"attributes_version": 1, "weight": 12345, "x_note": "redelivered"}
    assert apply_hot_ip_added(trie, records, V4, first) is True
    assert dict(records[V4]) == first
    counts = _trie_state(trie)

    assert apply_hot_ip_added(trie, records, V4, second) is False
    assert dict(records[V4]) == second
    assert _trie_state(trie) == counts
    assert records.serialized_bytes == _compact_size(second)
    _check_coupled(trie, records)


@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_removing_an_unknown_address_deletes_nothing_but_a_stray_record(
    make_trie: TrieFactory,
) -> None:
    trie = make_trie(IPV4)
    records = IpAttributeRecords(IPV4)
    assert apply_hot_ip_added(trie, records, V4, {"attributes_version": 1, "weight": 1}) is True

    before = _records_state(records)
    assert apply_hot_ip_removed(trie, records, V4_B) is False
    assert _records_state(records) == before
    _check_coupled(trie, records)

    # A stray record for a COLD address, written behind the trie's back, is
    # deleted by the next removal for it (decision 6: self-healing).
    records.record(V4_C)
    assert V4_C in records
    counts = _trie_state(trie)
    assert apply_hot_ip_removed(trie, records, V4_C) is False
    assert V4_C not in records
    assert _trie_state(trie) == counts
    assert _records_state(records) == before
    _check_coupled(trie, records)

    assert apply_hot_ip_removed(trie, records, V4) is True
    assert len(records) == 0
    assert records.serialized_bytes == 0
    _check_coupled(trie, records)


def test_no_document_stores_the_default_and_costs_24_bytes() -> None:
    """Section 46.5 and read-api-v1.md: absent is stored as
    `{"attributes_version": 1}`, whose compact encoding is 24 bytes."""

    assert dict(DEFAULT_ATTRIBUTES) == {"attributes_version": 1}
    records = IpAttributeRecords(IPV4)
    records.record(V4_C, {"attributes_version": 1, "weight": 5})
    before = records.serialized_bytes

    records.record(V4)
    assert dict(records[V4]) == {"attributes_version": 1}
    assert records.serialized_bytes == before + 24

    records.record(V4_B, None)
    assert dict(records[V4_B]) == {"attributes_version": 1}
    assert records.serialized_bytes == before + 48


def test_default_attributes_is_read_only() -> None:
    writable: Any = DEFAULT_ATTRIBUTES
    with pytest.raises(TypeError):
        writable["weight"] = 1
    assert dict(DEFAULT_ATTRIBUTES) == {"attributes_version": 1}


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"attributes_version": 1, "x_text": "free text"}, id="x-string"),
        pytest.param({"attributes_version": 1, "x_count": 12345}, id="x-number"),
        pytest.param(
            {"attributes_version": 1, "x_nested": {"a": [1, {"b": None}, "c"], "d": True}},
            id="x-nested",
        ),
        pytest.param({"attributes_version": 1, "x_list": [1, "two", [3]]}, id="x-list"),
        pytest.param(
            {"attributes_version": 2, "severity": 3, "weight": 99_999_999},
            id="higher-version-passes-through",
        ),
    ],
)
def test_valid_documents_are_stored_and_read_back_verbatim(document: dict[str, object]) -> None:
    """Sections 46.2 and 46.9: stored and echoed, never interpreted."""

    records = IpAttributeRecords(IPV4)
    records.record(V4, document)
    assert dict(records[V4]) == document
    assert records.serialized_bytes == _compact_size(document)


@pytest.mark.parametrize("earlier", [False, True], ids=["new-address", "replacing-a-record"])
@pytest.mark.parametrize("document", REJECTED_DOCUMENTS)
def test_record_rejects_an_invalid_document_and_changes_nothing(
    document: object, earlier: bool
) -> None:
    """Decision 5: `record()` validates every write and is all-or-nothing."""

    records = IpAttributeRecords(IPV4)
    records.record(V4_C, {"attributes_version": 1, "weight": 7})
    if earlier:
        records.record(V4, {"attributes_version": 1, "weight": 1450, "x_note": "kept"})
    before = _records_state(records)
    with pytest.raises(InvalidAttributesError):
        records.record(V4, document)  # type: ignore[arg-type]
    assert _records_state(records) == before


def test_a_stored_record_is_a_private_deep_copy() -> None:
    document: dict[str, Any] = {"attributes_version": 1, "x_list": [1, 2], "x_obj": {"k": "v"}}
    records = IpAttributeRecords(IPV4)
    records.record(V4, document)
    size = records.serialized_bytes

    document["x_list"].append("a" * 2000)
    document["x_obj"]["k"] = "changed"
    document["x_added"] = 1
    assert dict(records[V4]) == {"attributes_version": 1, "x_list": [1, 2], "x_obj": {"k": "v"}}
    assert records.serialized_bytes == size


def test_the_record_map_is_a_read_only_mapping() -> None:
    """Decision 5: not a `MutableMapping`; reads are read-only views."""

    records = IpAttributeRecords(IPV4)
    records.record(V4, {"attributes_version": 1, "weight": 3})
    records.record(V4_C)
    before = _records_state(records)

    writable: Any = records
    with pytest.raises(TypeError):
        writable[V4_B] = {"attributes_version": 1}
    with pytest.raises(TypeError):
        del writable[V4]
    view: Any = records[V4]
    with pytest.raises(TypeError):
        view["weight"] = 4
    assert _records_state(records) == before

    assert V4 in records
    assert V4_B not in records
    assert len(records) == 2
    assert records.get(V4_B) is None
    found = records.get(V4)
    assert found is not None
    assert dict(found) == {"attributes_version": 1, "weight": 3}
    keys = list(records)
    assert all(isinstance(key, Address) for key in keys)
    assert sorted(keys, key=lambda a: a.value) == sorted([V4, V4_C], key=lambda a: a.value)
    assert set(records.keys()) == {V4, V4_C}


def test_discard_and_clear_keep_the_byte_total() -> None:
    records = IpAttributeRecords(IPV4)
    kept = {"attributes_version": 1, "weight": 1450}
    records.record(V4, kept)
    records.record(V4_C, {"attributes_version": 1, "x_note": "gone"})

    assert records.discard(V4_C) is True
    assert records.discard(V4_C) is False
    assert records.discard(V4_B) is False
    assert records.serialized_bytes == _compact_size(kept)
    assert len(records) == 1

    records.clear()
    assert len(records) == 0
    assert records.serialized_bytes == 0
    assert list(records) == []


RECORD_METHODS = ["record", "record-with-a-bad-document", "discard", "getitem", "contains", "get"]


def _call_records(records: IpAttributeRecords, method: str, address: Address) -> None:
    if method == "record":
        records.record(address, {"attributes_version": 1})
    elif method == "record-with-a-bad-document":
        records.record(address, {})
    elif method == "discard":
        records.discard(address)
    elif method == "getitem":
        _ = records[address]
    elif method == "contains":
        _ = address in records
    elif method == "get":
        _ = records.get(address)
    else:
        raise AssertionError(method)


@pytest.mark.parametrize("method", RECORD_METHODS)
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_every_record_map_method_rejects_the_other_family(populated: bool, method: str) -> None:
    """Decision 5 / assumption 39: reads included, as for the trie's queries;
    the family is checked before the document."""

    records = IpAttributeRecords(IPV4)
    if populated:
        records.record(V4, {"attributes_version": 1, "weight": 3})
    before = _records_state(records)
    with pytest.raises(ValueError) as excinfo:
        _call_records(records, method, V6)
    _assert_names_both_families(excinfo.value)
    assert _records_state(records) == before


def test_a_key_that_is_not_an_address_is_simply_absent() -> None:
    records = IpAttributeRecords(IPV4)
    records.record(V4)
    # Through `Any`: mypy's strict equality would (rightly) flag the lookup.
    untyped: Any = records
    assert "192.168.1.42" not in untyped
    assert 42 not in untyped
    assert untyped.get("192.168.1.42") is None
    assert V4 in records


MISMATCHES = [
    pytest.param(IPV4, IPV4, IPV6, id="address-of-the-other-family"),
    pytest.param(IPV4, IPV6, IPV4, id="records-of-the-other-family"),
    pytest.param(IPV6, IPV4, IPV4, id="trie-of-the-other-family"),
    pytest.param(IPV6, IPV6, IPV4, id="ipv6-pair-ipv4-address"),
]


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"attributes_version": 1, "weight": 9}, id="valid"),
        pytest.param(None, id="none"),
        pytest.param({}, id="invalid-document"),
    ],
)
@pytest.mark.parametrize(("trie_family", "records_family", "address_family"), MISMATCHES)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_the_coupled_step_rejects_mismatched_families_and_changes_nothing(
    make_trie: TrieFactory,
    trie_family: AddressFamily,
    records_family: AddressFamily,
    address_family: AddressFamily,
    document: dict[str, object] | None,
) -> None:
    """Decision 6 clause 1, ahead of the document (clause 2)."""

    trie = make_trie(trie_family)
    assert trie.add_hot_ip(OTHER_SAMPLE[trie_family]) is True
    records = IpAttributeRecords(records_family)
    records.record(OTHER_SAMPLE[records_family], {"attributes_version": 1, "weight": 2})
    trie_before = _trie_state(trie)
    records_before = _records_state(records)
    address = SAMPLE[address_family]

    with pytest.raises(ValueError) as excinfo:
        apply_hot_ip_added(trie, records, address, document)
    _assert_names_both_families(excinfo.value)
    assert _trie_state(trie) == trie_before
    assert _records_state(records) == records_before

    with pytest.raises(ValueError) as excinfo:
        apply_hot_ip_removed(trie, records, address)
    _assert_names_both_families(excinfo.value)
    assert _trie_state(trie) == trie_before
    assert _records_state(records) == records_before


@pytest.mark.parametrize("document", REJECTED_DOCUMENTS)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_a_rejected_document_for_a_new_address_changes_neither_and_recovers_with_none(
    make_trie: TrieFactory, document: object
) -> None:
    """Decision 6 clause 2 (validate before the trie), then section 46.1's
    recovery path: the worker applies the transition again with no document."""

    trie = make_trie(IPV4)
    records = IpAttributeRecords(IPV4)
    assert apply_hot_ip_added(trie, records, V4_C, {"attributes_version": 1, "weight": 3}) is True
    trie_before = _trie_state(trie)
    records_before = _records_state(records)

    with pytest.raises(InvalidAttributesError):
        apply_hot_ip_added(trie, records, V4, document)  # type: ignore[arg-type]
    assert trie.contains(V4) is False
    assert V4 not in records
    assert _trie_state(trie) == trie_before
    assert _records_state(records) == records_before
    _check_coupled(trie, records)

    assert apply_hot_ip_added(trie, records, V4, None) is True
    assert trie.contains(V4) is True
    assert dict(records[V4]) == {"attributes_version": 1}
    _check_coupled(trie, records)


@pytest.mark.parametrize("document", REJECTED_DOCUMENTS)
@pytest.mark.parametrize("make_trie", IMPLEMENTATIONS)
def test_a_rejected_document_for_a_hot_address_keeps_its_valid_record(
    make_trie: TrieFactory, document: object
) -> None:
    trie = make_trie(IPV4)
    records = IpAttributeRecords(IPV4)
    valid = {"attributes_version": 1, "weight": 1450, "x_note": "valid"}
    assert apply_hot_ip_added(trie, records, V4, valid) is True
    trie_before = _trie_state(trie)
    records_before = _records_state(records)

    with pytest.raises(InvalidAttributesError):
        apply_hot_ip_added(trie, records, V4, document)  # type: ignore[arg-type]
    assert trie.contains(V4) is True
    assert dict(records[V4]) == valid
    assert _trie_state(trie) == trie_before
    assert _records_state(records) == records_before
    _check_coupled(trie, records)


@pytest.mark.parametrize("with_record", [True, False], ids=["recorded", "unrecorded"])
def test_a_corrupt_trie_raises_before_the_record_is_written(with_record: bool) -> None:
    """Decision 6 clause 3 / ADR-0014 A12: `add_hot_ip` raises
    `InvariantViolation` on a leaf whose count is not 1, before mutating; the
    record map is untouched because it is written after the trie. Per A12
    clause 4 no check is run and no implementation is compared on this state."""

    trie = PatriciaTrie(IPV4)
    records = IpAttributeRecords(IPV4)
    if with_record:
        assert apply_hot_ip_added(trie, records, V4, {"attributes_version": 1, "weight": 1}) is True
    else:
        assert trie.add_hot_ip(V4) is True
    assert trie.node_count == 1
    trie.arena.hot_count[trie.root] = 0
    before = _records_state(records)

    with pytest.raises(InvariantViolation):
        apply_hot_ip_added(trie, records, V4, {"attributes_version": 1, "weight": 2000})
    assert _records_state(records) == before


def test_prefix_metadata_and_ip_attributes_share_no_namespace() -> None:
    """Section 46.6: both views of an address are returned separately."""

    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_24, {"weight": Override(5)})
    trie = PatriciaTrie(IPV4)
    records = IpAttributeRecords(IPV4)
    attributes = {"attributes_version": 1, "weight": 1450}
    assert apply_hot_ip_added(trie, records, HOST, attributes) is True
    elsewhere = Address.parse("10.20.31.1")
    assert apply_hot_ip_added(trie, records, elsewhere, {"attributes_version": 1}) is True

    assert dict(records[HOST]) == attributes
    assert dict(store.effective(HOST)) == {"weight": Override(5)}
    assert dict(store.effective(elsewhere)) == {}
    assert dict(store.local(HOST_ROUTE)) == {}
    assert len(store) == 1


# ==========================================================================
# E. What the metadata surface refuses (ADR-0015 Amendment 1, rulings 3-7).
# ==========================================================================

StoreState = tuple[int, list[tuple[Prefix, dict[str, Value]]], dict[str, Value]]


def _store_state(store: PrefixMetadataStore, prefix: Prefix) -> StoreState:
    """Everything a refused `declare` must leave alone (decision 4)."""

    return len(store), _declared(store), dict(store.local(prefix))


def _other(family: AddressFamily) -> AddressFamily:
    return IPV6 if family is IPV4 else IPV4


def _covers(outer: Prefix, inner: Prefix) -> bool:
    """`outer` contains `inner` or is it: same family, no longer, same leading bits."""

    if outer.family is not inner.family or outer.length > inner.length:
        return False
    shift = outer.family.bit_length - outer.length
    return (outer.network >> shift) == (inner.network >> shift)


def _sibling(prefix: Prefix) -> Prefix:
    """The other half of `prefix`'s parent: same length, last network bit flipped."""

    flip = 1 << (prefix.family.bit_length - prefix.length)
    return Prefix(family=prefix.family, network=prefix.network ^ flip, length=prefix.length)


# --- Ruling 3: `declare` refuses a document that is not `Metadata`. --------

# (document, the `str` key whose value is at fault -- or None when the fault
# is not a value under a `str` key)
MALFORMED_DOCUMENTS = [
    pytest.param(42, None, id="int"),
    pytest.param(None, None, id="none"),
    pytest.param("labels", None, id="str"),
    pytest.param([("labels", Tags.of("x"))], None, id="list-of-pairs"),
    pytest.param({1: Tags.of("x")}, None, id="non-str-key"),
    pytest.param({"flags": 3}, "flags", id="bare-int"),
    pytest.param({"policy": "deny"}, "policy", id="bare-str"),
    pytest.param({"policy": None}, "policy", id="bare-none"),
    pytest.param({"labels": {"internal"}}, "labels", id="bare-set"),
    pytest.param({"labels": frozenset({"internal"})}, "labels", id="bare-frozenset"),
    pytest.param({"zone": {"labels": Tags.of("x")}}, "zone", id="nested-document"),
    pytest.param({"labels": Tags.of("ok"), "flags": 3}, "flags", id="one-bad-value-of-two"),
]


@pytest.mark.parametrize(("document", "key"), MALFORMED_DOCUMENTS)
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
@pytest.mark.parametrize("family", FAMILIES)
def test_declare_refuses_a_malformed_document_and_changes_nothing(
    family: AddressFamily, populated: bool, document: object, key: str | None
) -> None:
    """Decision 4 / Amendment 1 ruling 3 (assumption 44): a non-mapping (`None`
    and a list of pairs included), a non-`str` key, or a value that is not a
    `Tags`, `Bitmask` or `Override` is a `TypeError`; the message names a
    faulty value's key; the store's length, its declarations and any earlier
    declaration on the prefix are exactly as they were."""

    target = _prefix_of(SAMPLE[family], 16)
    store = PrefixMetadataStore(family)
    if populated:
        store.declare(target, {"labels": Tags.of("kept"), "flags": Bitmask(1)})
        store.declare(_prefix_of(OTHER_SAMPLE[family], 24), {"policy": Override("deny")})
    before = _store_state(store, target)

    malformed: Any = document
    with pytest.raises(TypeError) as excinfo:
        store.declare(target, malformed)
    if key is not None:
        assert key in str(excinfo.value), str(excinfo.value)
    assert _store_state(store, target) == before


def test_a_malformed_document_is_a_type_error_even_when_it_would_also_conflict() -> None:
    """Decision 4: the path check (ruling 6) runs after the document check
    (ruling 3), so a document that fails both is the `TypeError`."""

    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_8, {"k": Tags.of("a")})
    before = _store_state(store, SLASH_24)
    document: Any = {"k": Bitmask(1), "j": 3}
    with pytest.raises(TypeError):
        store.declare(SLASH_24, document)
    assert _store_state(store, SLASH_24) == before


# --- Ruling 4: anything that is not a `Prefix` is absent from the store. ----


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
@pytest.mark.parametrize("family", FAMILIES)
def test_anything_that_is_not_a_prefix_is_simply_absent(
    family: AddressFamily, populated: bool
) -> None:
    """Decision 4 / Amendment 1 ruling 4 (assumption 45): `False`, never an
    exception -- an `Address` of either family, a `str`, `None`, an
    unhashable object -- and the store is unchanged."""

    address = SAMPLE[family]
    host_route = _prefix_of(address, family.bit_length)
    store = PrefixMetadataStore(family)
    if populated:
        store.declare(_root(family), {"labels": Tags.of("root")})
        store.declare(host_route, {"flags": Bitmask(1)})
    before = (len(store), _declared(store))

    probes: list[object] = [
        address,
        SAMPLE[_other(family)],
        "10.0.0.0/8",
        str(_root(family)),
        str(host_route),
        None,
        42,
        [],
        {},
    ]
    for probe in probes:
        assert probe not in store, probe
        assert (len(store), _declared(store)) == before


@pytest.mark.parametrize("family", FAMILIES)
def test_an_address_is_not_read_as_its_host_route(family: AddressFamily) -> None:
    """Amendment 1 ruling 4: "An `Address` is not read as its host route"."""

    address = SAMPLE[family]
    host_route = _prefix_of(address, family.bit_length)
    store = PrefixMetadataStore(family)
    store.declare(host_route, {"labels": Tags.of("host")})
    assert host_route in store
    # Through `Any`: mypy's strict equality may (rightly) flag the lookup.
    untyped: Any = store
    assert address not in untyped
    assert len(store) == 1
    assert _declared(store) == [(host_route, {"labels": Tags.of("host")})]


# --- Ruling 5: each value type checks its field's type when it is built. ---


class _Level(IntEnum):
    HIGH = 2


REFUSED_CONSTRUCTIONS = [
    pytest.param(Tags, (frozenset({1}),), id="tags-frozenset-of-int"),
    pytest.param(Tags, (frozenset({"a", 1}),), id="tags-frozenset-with-an-int"),
    pytest.param(Tags, ({"a"},), id="tags-set"),
    pytest.param(Tags, (["a"],), id="tags-list"),
    pytest.param(Tags, ("abc",), id="tags-str"),
    pytest.param(Tags, (None,), id="tags-none"),
    pytest.param(Tags.of, ("a", 1), id="tags-of-with-an-int"),
    pytest.param(Bitmask, ("3",), id="bitmask-str"),
    pytest.param(Bitmask, ("-1",), id="bitmask-negative-str"),
    pytest.param(Bitmask, (1.0,), id="bitmask-float"),
    pytest.param(Bitmask, (None,), id="bitmask-none"),
    pytest.param(Bitmask, (True,), id="bitmask-true"),
    pytest.param(Bitmask, (False,), id="bitmask-false"),
    pytest.param(Override, (None,), id="override-none"),
    pytest.param(Override, ([1],), id="override-list"),
    pytest.param(Override, (("a",),), id="override-tuple"),
    pytest.param(Override, ({"a"},), id="override-set"),
    pytest.param(Override, (frozenset({"a"}),), id="override-frozenset"),
    pytest.param(Override, ({"k": 1},), id="override-dict"),
    pytest.param(Override, (1.5,), id="override-float"),
    pytest.param(Override, (b"x",), id="override-bytes"),
]


@pytest.mark.parametrize(("factory", "arguments"), REFUSED_CONSTRUCTIONS)
def test_a_value_built_with_a_wrong_field_type_is_a_type_error(
    factory: Callable[..., object], arguments: tuple[object, ...]
) -> None:
    """Decision 3 / Amendment 1 ruling 5 (assumptions 3, 5, 46): `Tags.values`
    is a `frozenset` of `str`, `Bitmask.bits` an `int` that is not a `bool`,
    `Override.value` a `str` or an `int`. Messages are not pinned."""

    with pytest.raises(TypeError):
        factory(*arguments)


def test_the_edge_values_of_tags_and_bitmask_are_accepted() -> None:
    """Ruling 5: an empty `frozenset`, the empty string as a tag, a zero mask."""

    assert Tags(frozenset()).values == frozenset()
    assert Tags.of().values == frozenset()
    assert Tags.of("").values == frozenset({""})
    assert Bitmask(0).bits == 0


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty-str"),
        pytest.param(0, id="zero"),
        pytest.param(-3, id="negative-int"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(_Level.HIGH, id="int-enum-member"),
    ],
)
def test_override_accepts_every_str_and_int_bool_and_subclasses_included(
    value: str | int,
) -> None:
    """Ruling 5 / assumption 46: "a `bool` included", "subclasses included"."""

    assert Override(value).value == value


# --- Ruling 6: `declare` refuses a key with two kinds on one path. ---------

CONFLICT_KEY = "zone_marking"


def _assert_names_the_conflict(
    error: BaseException, kinds: tuple[str, str], prefix_text: str | None = None
) -> None:
    """Ruling 6: the key verbatim, both kinds (case-insensitively) and the
    conflicting prefix."""

    message = str(error)
    assert CONFLICT_KEY in message, message
    for kind in kinds:
        assert kind in message.lower(), message
    if prefix_text is not None:
        assert prefix_text in message, message


def test_a_descendant_may_not_give_an_ancestors_key_another_kind() -> None:
    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_8, {CONFLICT_KEY: Tags.of("a")})
    before = _store_state(store, SLASH_24)

    with pytest.raises(ValueError) as excinfo:
        store.declare(SLASH_24, {CONFLICT_KEY: Bitmask(1)})
    _assert_names_the_conflict(excinfo.value, ("tags", "bitmask"), "10.0.0.0/8")
    assert _store_state(store, SLASH_24) == before
    assert dict(store.effective(HOST)) == {CONFLICT_KEY: Tags.of("a")}


def test_an_ancestor_may_not_give_a_descendants_key_another_kind() -> None:
    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_24, {CONFLICT_KEY: Tags.of("a")})
    before = _store_state(store, SLASH_8)

    with pytest.raises(ValueError) as excinfo:
        store.declare(SLASH_8, {CONFLICT_KEY: Override("a")})
    _assert_names_the_conflict(excinfo.value, ("tags", "override"), "10.20.30.0/24")
    assert _store_state(store, SLASH_8) == before
    assert dict(store.effective(HOST)) == {CONFLICT_KEY: Tags.of("a")}


@pytest.mark.parametrize("family", FAMILIES)
def test_the_whole_path_is_compared_from_slash_0_to_the_host_route(
    family: AddressFamily,
) -> None:
    root = _root(family)
    host_route = _prefix_of(SAMPLE[family], family.bit_length)
    store = PrefixMetadataStore(family)
    store.declare(root, {CONFLICT_KEY: Override("deny")})
    before = _store_state(store, host_route)

    with pytest.raises(ValueError) as excinfo:
        store.declare(host_route, {CONFLICT_KEY: Tags.of("a")})
    _assert_names_the_conflict(excinfo.value, ("override", "tags"))
    assert _store_state(store, host_route) == before
    assert dict(store.effective(SAMPLE[family])) == {CONFLICT_KEY: Override("deny")}


def test_a_replaced_declaration_is_not_compared_with_its_replacement() -> None:
    """Ruling 6: "`P`'s own current declaration, which the call would replace,
    is not compared"."""

    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_24, {CONFLICT_KEY: Tags.of("a")})
    store.declare(SLASH_24, {CONFLICT_KEY: Bitmask(1)})
    assert dict(store.local(SLASH_24)) == {CONFLICT_KEY: Bitmask(1)}
    assert len(store) == 1
    assert dict(store.effective(HOST)) == {CONFLICT_KEY: Bitmask(1)}


def test_a_replacement_is_still_compared_with_the_rest_of_its_path() -> None:
    store = PrefixMetadataStore(IPV4)
    store.declare(SLASH_8, {CONFLICT_KEY: Tags.of("root")})
    store.declare(SLASH_24, {CONFLICT_KEY: Tags.of("a")})
    before = _store_state(store, SLASH_24)

    with pytest.raises(ValueError) as excinfo:
        store.declare(SLASH_24, {CONFLICT_KEY: Bitmask(1)})
    _assert_names_the_conflict(excinfo.value, ("tags", "bitmask"), "10.0.0.0/8")
    assert _store_state(store, SLASH_24) == before
    assert dict(store.local(SLASH_24)) == {CONFLICT_KEY: Tags.of("a")}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Tags.of("a"), id="tags"),
        pytest.param(Bitmask(1), id="bitmask"),
        pytest.param(Override("a"), id="override"),
    ],
)
def test_disjoint_prefixes_may_differ_but_nothing_containing_both_may_declare_the_key(
    value: Value,
) -> None:
    """Ruling 6: no path passes through two prefixes neither of which contains
    the other, so they may differ -- and a later declaration of that key on a
    prefix containing both must conflict with one of them."""

    left = SLASH_16
    right = Prefix.parse("10.21.0.0/16")
    store = PrefixMetadataStore(IPV4)
    store.declare(left, {CONFLICT_KEY: Tags.of("left")})
    store.declare(right, {CONFLICT_KEY: Bitmask(2)})
    assert len(store) == 2

    assert dict(store.effective(Address.parse("10.20.1.1"))) == {CONFLICT_KEY: Tags.of("left")}
    assert dict(store.effective(Address.parse("10.21.1.1"))) == {CONFLICT_KEY: Bitmask(2)}
    assert dict(store.effective_for_prefix(SLASH_24)) == {CONFLICT_KEY: Tags.of("left")}
    assert dict(store.inherited(Prefix.parse("10.21.7.0/24"))) == {CONFLICT_KEY: Bitmask(2)}
    assert dict(store.effective(Address.parse("10.22.0.1"))) == {}
    assert dict(store.effective_for_prefix(SLASH_8)) == {}

    before = _store_state(store, SLASH_8)
    with pytest.raises(ValueError) as excinfo:
        store.declare(SLASH_8, {CONFLICT_KEY: value})
    assert CONFLICT_KEY in str(excinfo.value), str(excinfo.value)
    assert _store_state(store, SLASH_8) == before


_ANY_KIND: st.SearchStrategy[Value] = st.one_of(_TAGS, _BITMASKS, _OVERRIDES)
# Kinds drawn freely: "k" and "j" may each be any kind in any document.
FREE_DOCUMENTS: st.SearchStrategy[dict[str, Value]] = st.dictionaries(
    st.sampled_from(("k", "j")), _ANY_KIND, max_size=2
)


def _conflicts(
    model: Mapping[Prefix, Mapping[str, Value]], target: Prefix, document: Mapping[str, Value]
) -> bool:
    """Ruling 6 spelled out: another declared prefix on `target`'s path holds a
    key of `document` with a different kind."""

    for other, declared in model.items():
        if other == target or not (_covers(other, target) or _covers(target, other)):
            continue
        for key, value in document.items():
            if key in declared and type(declared[key]) is not type(value):
                return True
    return False


def _assert_reads_match_the_model(
    store: PrefixMetadataStore,
    model: Mapping[Prefix, dict[str, Value]],
    probe_prefixes: Iterable[Prefix],
    probe_addresses: Iterable[Address],
) -> None:
    by_length = sorted(model, key=lambda p: p.length)
    for prefix in probe_prefixes:
        path = [q for q in by_length if _covers(q, prefix)]
        assert dict(store.local(prefix)) == model.get(prefix, {})
        assert dict(store.inherited(prefix)) == _fold(model[q] for q in path if q != prefix)
        assert dict(store.effective_for_prefix(prefix)) == _fold(model[q] for q in path)
    for address in probe_addresses:
        host_route = _prefix_of(address, address.bit_length)
        path = [q for q in by_length if _covers(q, host_route)]
        assert dict(store.effective(address)) == _fold(model[q] for q in path)
    assert _declared(store) == [
        (p, model[p]) for p in sorted(model, key=lambda q: (q.length, q.network))
    ]


@pytest.mark.parametrize("family", FAMILIES)
@settings(deadline=None, max_examples=80)
@given(data=st.data())
def test_declare_refuses_exactly_what_would_give_a_key_two_kinds_on_a_path(
    family: AddressFamily, data: st.DataObject
) -> None:
    """Ruling 6 against a model: a declaration is accepted exactly when no
    other declared prefix containing, or contained by, its target holds a
    shared key with a different kind; a refusal changes nothing; and after
    every step every read returns without raising and matches the section 16
    fold of what was accepted."""

    bits = family.bit_length
    probe = data.draw(addresses(family))
    lengths = data.draw(st.sets(st.integers(min_value=0, max_value=bits), min_size=1, max_size=5))
    on_path = [_prefix_of(probe, length) for length in sorted(lengths)]
    siblings = [_sibling(p) for p in on_path if p.length > 0]
    elsewhere = data.draw(st.lists(prefixes(family), max_size=2))
    pool = [*on_path, *siblings, *elsewhere]
    steps = data.draw(st.lists(st.tuples(st.sampled_from(pool), FREE_DOCUMENTS), max_size=10))

    probe_prefixes = [*pool, _root(family), _prefix_of(probe, bits)]
    probe_addresses = [probe, *(Address(family=family, value=p.network) for p in pool)]

    store = PrefixMetadataStore(family)
    model: dict[Prefix, dict[str, Value]] = {}
    for target, document in steps:
        before = (len(store), _declared(store))
        if _conflicts(model, target, document):
            with pytest.raises(ValueError):
                store.declare(target, document)
            assert (len(store), _declared(store)) == before
        else:
            store.declare(target, document)
            model[target] = document
        _assert_reads_match_the_model(store, model, probe_prefixes, probe_addresses)


# --- Ruling 7: the combine functions check their operands' shape first. ----

NOT_A_KIND = [
    pytest.param(Tags.of("a"), 1, id="tags-and-int"),
    pytest.param(1, Tags.of("a"), id="int-and-tags"),
    pytest.param(1, 1, id="int-and-int"),
    pytest.param(None, Bitmask(1), id="none-and-bitmask"),
    pytest.param(Bitmask(1), None, id="bitmask-and-none"),
    pytest.param("x", Override("x"), id="str-and-override"),
    pytest.param({"a"}, Tags.of("a"), id="set-and-tags"),
    pytest.param(frozenset({"a"}), Tags.of("a"), id="frozenset-and-tags"),
]


@pytest.mark.parametrize(("less", "more"), NOT_A_KIND)
def test_combine_values_refuses_an_operand_that_is_not_a_kind(less: object, more: object) -> None:
    """Decision 3 / Amendment 1 ruling 7: a `TypeError` whatever the other
    operand is -- never the mixed-kind `ValueError`."""

    untyped_less: Any = less
    untyped_more: Any = more
    with pytest.raises(TypeError):
        combine_values(untyped_less, untyped_more)


# (less, more, the `str` key whose value is at fault -- or None)
MALFORMED_OPERANDS = [
    pytest.param({"stray_flags": 1}, {}, "stray_flags", id="bare-value-in-less"),
    pytest.param({}, {"stray_flags": 1}, "stray_flags", id="bare-value-in-more"),
    pytest.param(
        {"labels": Tags.of("a")},
        {"stray_policy": "x"},
        "stray_policy",
        id="bare-value-under-a-key-one-operand-holds",
    ),
    pytest.param(42, {}, None, id="int-document"),
    pytest.param({}, None, None, id="none-document"),
    pytest.param([("labels", Tags.of("a"))], {}, None, id="list-of-pairs"),
    pytest.param({1: Tags.of("a")}, {}, None, id="non-str-key"),
]


@pytest.mark.parametrize(("less", "more", "key"), MALFORMED_OPERANDS)
def test_combine_refuses_a_malformed_document(less: object, more: object, key: str | None) -> None:
    """Ruling 7: both documents are checked whole, by `declare`'s rule, so a
    malformed entry under a key only one operand holds is refused rather than
    carried into the result; the message names a faulty value's key."""

    untyped_less: Any = less
    untyped_more: Any = more
    with pytest.raises(TypeError) as excinfo:
        combine(untyped_less, untyped_more)
    if key is not None:
        assert key in str(excinfo.value), str(excinfo.value)


@pytest.mark.parametrize("mirrored", [False, True], ids=["malformed-first", "malformed-second"])
def test_a_pair_both_malformed_and_mixed_kind_is_the_type_error(mirrored: bool) -> None:
    """Ruling 7: shape is checked before any key is combined."""

    malformed: Any = {"a": Tags.of("x"), "b": 3}
    well_formed: Any = {"a": Bitmask(1)}
    less, more = (well_formed, malformed) if mirrored else (malformed, well_formed)
    with pytest.raises(TypeError):
        combine(less, more)


@pytest.mark.parametrize(
    "documents",
    [
        pytest.param([{"k": Tags.of("a")}, {"j": 3}], id="bare-value"),
        pytest.param([{"k": Tags.of("a")}, 42], id="int-document"),
        pytest.param([None], id="none-document"),
    ],
)
def test_combine_path_refuses_a_malformed_document(documents: list[object]) -> None:
    untyped: Any = documents
    with pytest.raises(TypeError):
        combine_path(untyped)


def test_combine_path_raises_at_its_first_failing_step() -> None:
    """Ruling 7: `combine_path` is the left fold of `combine`, so it raises at
    the first step that fails, in iteration order -- a generator included."""

    mixed_first: Any = [{"k": Tags.of("a")}, {"k": Bitmask(1)}, 42]
    malformed_first: Any = [{"k": Tags.of("a")}, 42, {"k": Bitmask(1)}]
    with pytest.raises(ValueError):
        combine_path(mixed_first)
    with pytest.raises(ValueError):
        combine_path(iter(mixed_first))
    with pytest.raises(TypeError):
        combine_path(malformed_first)
    with pytest.raises(TypeError):
        combine_path(iter(malformed_first))


# ==========================================================================
# F. ADR-0015 Amendment 2: the record map keeps the canonical copy, as text,
#    and every read decodes a fresh document (rulings A-D, follow-ups).
# ==========================================================================


class _Traps:
    """The one switch every trapped or lying class in section F consults.

    Off while a document is built -- building a `dict` hashes, and may
    compare, its keys -- and on only inside `_armed()`, around the write under
    test. Nothing here is thread-safe, and nothing needs to be.
    """

    armed = False
    error: type[BaseException] = RuntimeError
    endless = False


@contextlib.contextmanager
def _armed(error: type[BaseException] = RuntimeError, *, endless: bool = False) -> Iterator[None]:
    _Traps.error = error
    _Traps.endless = endless
    _Traps.armed = True
    try:
        yield
    finally:
        _Traps.armed = False
        _Traps.endless = False


# Every method a hostile class overrides, where its base type has it. Armed,
# each raises `_Traps.error`; in "endless" mode the iterating ones instead
# return an iterator that never ends, and the rest tell the truth.
_TRAPPED = (
    "__getattribute__",
    "__hash__",
    "__eq__",
    "__ne__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__repr__",
    "__str__",
    "__format__",
    "__bool__",
    "__len__",
    "__iter__",
    "__reversed__",
    "__contains__",
    "__getitem__",
    "__reduce__",
    "__reduce_ex__",
    "__sizeof__",
    "__getnewargs__",
    "__add__",
    "__mul__",
    "__mod__",
    "__int__",
    "__index__",
    "__float__",
    "__abs__",
    "__neg__",
    "__trunc__",
    "__round__",
    "__floor__",
    "__ceil__",
    "__rshift__",
    "__floordiv__",
    "__truediv__",
    "__pow__",
    "bit_length",
    "to_bytes",
    "is_integer",
    "as_integer_ratio",
    "hex",
    "encode",
    "isascii",
    "join",
    "startswith",
    "split",
    "items",
    "keys",
    "values",
    "get",
    "copy",
    "count",
    "index",
)
_ITERATING = frozenset({"__iter__", "__reversed__", "items", "keys", "values"})


def _trap(base: type, name: str) -> Callable[..., Any]:
    original = getattr(base, name)

    def method(self: object, *args: Any, **kwargs: Any) -> Any:
        if _Traps.armed:
            if not _Traps.endless:
                raise _Traps.error(name)
            if name in _ITERATING:
                return itertools.repeat(("x_a", 1)) if name == "items" else itertools.count()
        return original(self, *args, **kwargs)

    method.__name__ = name
    return method


def _class_trap(self: object) -> type:
    if _Traps.armed and not _Traps.endless:
        raise _Traps.error("__class__")
    return type(self)


def _hostile(base: type) -> Any:
    """A subclass of `base` overriding every method in `_TRAPPED`, and `__class__`."""

    namespace: dict[str, Any] = {
        name: _trap(base, name) for name in _TRAPPED if getattr(base, name, None) is not None
    }
    namespace["__class__"] = property(_class_trap)
    return type(f"_Hostile{base.__name__.title()}", (base,), namespace)


class _Kinds(NamedTuple):
    """Constructors for the five subclassable JSON-model types: str, int,
    float, list, dict. `bool` and `None` cannot be subclassed."""

    t: Callable[[Any], Any]
    n: Callable[[Any], Any]
    r: Callable[[Any], Any]
    a: Callable[[Any], Any]
    o: Callable[[Any], Any]


_PLAIN = _Kinds(str, int, float, list, dict)
_HOSTILE = _Kinds(_hostile(str), _hostile(int), _hostile(float), _hostile(list), _hostile(dict))


class _LyingList(list[Any]):
    """Holds its own content; armed, every overridable read shows `shown`."""

    shown: list[Any]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.shown) if _Traps.armed else list.__iter__(self)

    def __reversed__(self) -> Iterator[Any]:
        return reversed(self.shown) if _Traps.armed else list.__reversed__(self)

    def __len__(self) -> int:
        return len(self.shown) if _Traps.armed else list.__len__(self)

    def __getitem__(self, index: Any) -> Any:
        return self.shown[index] if _Traps.armed else list.__getitem__(self, index)

    def __contains__(self, item: object) -> bool:
        return item in self.shown if _Traps.armed else list.__contains__(self, item)

    def __eq__(self, other: object) -> bool:
        return self.shown == other if _Traps.armed else list.__eq__(self, other)

    def __repr__(self) -> str:
        return repr(self.shown) if _Traps.armed else list.__repr__(self)

    def copy(self) -> Any:
        return list(self.shown) if _Traps.armed else list.copy(self)


class _LyingDict(dict[Any, Any]):
    """Holds its own entries; armed, every overridable read shows `shown`."""

    shown: dict[Any, Any]

    def items(self) -> Any:
        return self.shown.items() if _Traps.armed else dict.items(self)

    def keys(self) -> Any:
        return self.shown.keys() if _Traps.armed else dict.keys(self)

    def values(self) -> Any:
        return self.shown.values() if _Traps.armed else dict.values(self)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.shown) if _Traps.armed else dict.__iter__(self)

    def __getitem__(self, key: Any) -> Any:
        return self.shown[key] if _Traps.armed else dict.__getitem__(self, key)

    def __contains__(self, key: object) -> bool:
        return key in self.shown if _Traps.armed else dict.__contains__(self, key)

    def __len__(self) -> int:
        return len(self.shown) if _Traps.armed else dict.__len__(self)

    def get(self, key: Any, default: Any = None) -> Any:
        return self.shown.get(key, default) if _Traps.armed else dict.get(self, key, default)

    def copy(self) -> Any:
        return dict(self.shown) if _Traps.armed else dict.copy(self)

    def __eq__(self, other: object) -> bool:
        return self.shown == other if _Traps.armed else dict.__eq__(self, other)

    def __repr__(self) -> str:
        return repr(self.shown) if _Traps.armed else dict.__repr__(self)


class _LyingStr(str):
    """Holds its own characters; armed, every overridable read shows `shown`."""

    shown: str

    def __str__(self) -> Any:
        return self.shown if _Traps.armed else str.__str__(self)

    def __repr__(self) -> Any:
        return repr(self.shown) if _Traps.armed else str.__repr__(self)

    def __iter__(self) -> Any:
        return iter(self.shown) if _Traps.armed else str.__iter__(self)

    def __getitem__(self, key: Any) -> Any:
        return self.shown[key] if _Traps.armed else str.__getitem__(self, key)

    def __len__(self) -> int:
        return len(self.shown) if _Traps.armed else str.__len__(self)

    def __contains__(self, key: Any) -> bool:
        return key in self.shown if _Traps.armed else str.__contains__(self, key)

    def __eq__(self, other: object) -> bool:
        return self.shown == other if _Traps.armed else str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return self.shown != other if _Traps.armed else str.__ne__(self, other)

    def __hash__(self) -> int:
        return hash(self.shown) if _Traps.armed else str.__hash__(self)

    def encode(self, *args: Any, **kwargs: Any) -> Any:
        if _Traps.armed:
            return self.shown.encode(*args, **kwargs)
        return str.encode(self, *args, **kwargs)


class _LyingInt(int):
    """Holds its own value; armed, every comparison answers True and every
    conversion shows `shown`."""

    shown: int

    def __lt__(self, other: Any) -> bool:
        return True if _Traps.armed else int.__lt__(self, other)

    def __le__(self, other: Any) -> bool:
        return True if _Traps.armed else int.__le__(self, other)

    def __gt__(self, other: Any) -> bool:
        return True if _Traps.armed else int.__gt__(self, other)

    def __ge__(self, other: Any) -> bool:
        return True if _Traps.armed else int.__ge__(self, other)

    def __eq__(self, other: object) -> bool:
        return True if _Traps.armed else int.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return True if _Traps.armed else int.__ne__(self, other)

    def __hash__(self) -> int:
        return int.__hash__(self)

    def __int__(self) -> int:
        return self.shown if _Traps.armed else int.__int__(self)

    def __index__(self) -> int:
        return self.shown if _Traps.armed else int.__index__(self)

    def bit_length(self) -> int:
        return self.shown.bit_length() if _Traps.armed else int.bit_length(self)

    def __repr__(self) -> str:
        return repr(self.shown) if _Traps.armed else int.__repr__(self)


class _Impersonator(str):
    """Its characters are whatever it was built from; it hashes and compares
    as "weight" always, and armed it prints and measures as "weight" too."""

    def __hash__(self) -> int:
        return hash("weight")

    def __eq__(self, other: object) -> bool:
        return other is self or other == "weight"

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __str__(self) -> Any:
        return "weight" if _Traps.armed else str.__str__(self)

    def __len__(self) -> int:
        return len("weight") if _Traps.armed else str.__len__(self)


class _Twin(str):
    """Equal only to itself and hashed by identity, so two with the same
    characters are two keys of one `dict`."""

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return other is self

    def __ne__(self, other: object) -> bool:
        return other is not self


def _lying_list(held: list[Any], shown: list[Any]) -> _LyingList:
    value = _LyingList(held)
    value.shown = shown
    return value


def _lying_dict(held: dict[Any, Any], shown: dict[Any, Any]) -> _LyingDict:
    value = _LyingDict(held)
    value.shown = shown
    return value


def _lying_str(held: str, shown: str) -> _LyingStr:
    value = _LyingStr(held)
    value.shown = shown
    return value


def _lying_int(held: int, shown: int) -> _LyingInt:
    value = _LyingInt(held)
    value.shown = shown
    return value


# Objects whose class claims, through a `__class__` property, to be `dict`
# or `str` -- enough to pass `isinstance` -- and which quack like one.
_FAKE_DICT_TYPE: Any = type(
    "_FakeDict",
    (),
    {
        "__class__": property(lambda self: dict),
        "items": lambda self: {"attributes_version": 1}.items(),
        "keys": lambda self: {"attributes_version": 1}.keys(),
        "values": lambda self: {"attributes_version": 1}.values(),
        "__iter__": lambda self: iter(["attributes_version"]),
        "__len__": lambda self: 1,
        "__getitem__": lambda self, key: 1,
        "__contains__": lambda self, key: key == "attributes_version",
    },
)
_FAKE_STR_TYPE: Any = type(
    "_FakeStr",
    (),
    {
        "__class__": property(lambda self: str),
        "__str__": lambda self: "x_fake",
        "__len__": lambda self: 6,
        "__iter__": lambda self: iter("x_fake"),
    },
)


class _PlainMapping(Mapping[str, object]):
    """A correct, read-only `collections.abc.Mapping` that is not a `dict`."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class _Version(IntEnum):
    ONE = 1


class _Weight(IntEnum):
    HEAVY = 1450


class _Name(StrEnum):
    X_COLOUR = "x_colour"
    RED = "red"


class _StrSub(str):
    """A `str` subclass that changes nothing."""


class _FloatSub(float):
    """A `float` subclass that changes nothing."""


class _DictSub(dict[str, Any]):
    """A `dict` subclass that changes nothing."""


class _ListSub(list[Any]):
    """A `list` subclass that changes nothing."""


_LEAF_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))


def _assert_exact(value: object) -> None:
    """Every value reachable from `value` is an exact built-in JSON-model type
    and every key an exact `str` -- read through `type()` and the base types'
    own slots, so nothing a subclass defines runs."""

    pending: list[Any] = [value]
    visited = 0
    while pending:
        visited += 1
        assert visited < 100_000, "not a finite tree"
        item = pending.pop()
        kind = type(item)
        if kind is dict:
            for key, child in dict.items(item):
                assert type(key) is str, type(key)
                pending.append(child)
        elif kind is list:
            pending.extend(list.__iter__(item))
        else:
            assert any(kind is leaf for leaf in _LEAF_TYPES), kind


def _canonical_or_none(document: object) -> CanonicalAttributes | None:
    try:
        return canonicalize_ip_attributes(document)
    except InvalidAttributesError:
        return None


STORE_ENTRIES = ["record", "apply"]
_NEIGHBOUR = {"attributes_version": 1, "weight": 7}
_EARLIER = {"attributes_version": 1, "x_note": "earlier"}


def _write(entry: str, trie: HotTrie, records: IpAttributeRecords, document: object) -> None:
    untyped: Any = document
    if entry == "record":
        records.record(V4, untyped)
    else:
        assert apply_hot_ip_added(trie, records, V4, untyped) is True


def _assert_stored_outcome(
    entry: str,
    document: object,
    expected: CanonicalAttributes | None,
    *,
    error: type[BaseException] = RuntimeError,
    endless: bool = False,
) -> None:
    """Decision 5, "What can come out", and decision 6: `document` written
    through `entry` is stored as `expected`'s canonical copy -- read back
    exact-typed, counted at its canonical size -- or, when `expected` is None,
    rejected with `InvalidAttributesError` alone, leaving the trie and the map
    exactly as they were.

    "record" writes over an address that already has a record; "apply" adds a
    new address to a trie that already holds one."""

    trie = PatriciaTrie(IPV4)
    records = IpAttributeRecords(IPV4)
    assert apply_hot_ip_added(trie, records, V4_C, dict(_NEIGHBOUR)) is True
    if entry == "record":
        records.record(V4, dict(_EARLIER))
    trie_before = _trie_state(trie)
    records_before = _records_state(records)

    if expected is None:
        with _armed(error, endless=endless):
            with pytest.raises(InvalidAttributesError) as excinfo:
                _write(entry, trie, records, document)
            message = str(excinfo.value)
        assert len(message) < 1024
        assert _trie_state(trie) == trie_before
        assert _records_state(records) == records_before
        if entry == "apply":
            _check_coupled(trie, records)
        return

    with _armed(error, endless=endless):
        _write(entry, trie, records, document)
    stored = dict(records[V4])
    _assert_exact(stored)
    assert stored == expected.document
    assert records.serialized_bytes == _compact_size(_NEIGHBOUR) + expected.size
    if entry == "apply":
        assert trie.contains(V4) is True
        _check_coupled(trie, records)


# --- Follow-up E: subclasses are stored as their base types; bool. ---------

# (build the document, its plain equivalent)
SUBCLASS_DOCUMENTS: list[Any] = [
    pytest.param(
        lambda: {"attributes_version": _Version.ONE, "weight": _Weight.HEAVY},
        {"attributes_version": 1, "weight": 1450},
        id="int-enum-version-and-weight",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _Name.X_COLOUR: _Name.RED},
        {"attributes_version": 1, "x_colour": "red"},
        id="str-enum-key-and-value",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _StrSub("x_t"): [_StrSub("v"), {_StrSub("k"): 1}]},
        {"attributes_version": 1, "x_t": ["v", {"k": 1}]},
        id="str-subclass-keys-and-values",
    ),
    pytest.param(
        lambda: {
            "attributes_version": 1,
            _lying_str("x_key", "sources"): _lying_str("café", "\ud800"),
            "x_n": [_lying_str("ok", "\udfff")],
        },
        {"attributes_version": 1, "x_key": "café", "x_n": ["ok"]},
        id="lying-str-key-and-values",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_r": _FloatSub(2.5), "x_l": [_FloatSub(-0.25)]},
        {"attributes_version": 1, "x_r": 2.5, "x_l": [-0.25]},
        id="float-subclass",
    ),
    pytest.param(
        lambda: {
            "attributes_version": 1,
            "x_n": _ListSub([_DictSub({"k": _ListSub([1, _DictSub()])}), _ListSub()]),
        },
        {"attributes_version": 1, "x_n": [{"k": [1, {}]}, []]},
        id="nested-dict-and-list-subclasses",
    ),
    pytest.param(
        lambda: _DictSub({"attributes_version": 1, "weight": 3}),
        {"attributes_version": 1, "weight": 3},
        id="dict-subclass-document",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_t": True, "x_f": False, "x_l": [True, [False]]},
        {"attributes_version": 1, "x_t": True, "x_f": False, "x_l": [True, [False]]},
        id="bools-in-x-values",
    ),
]


@pytest.mark.parametrize(("make", "plain"), SUBCLASS_DOCUMENTS)
@pytest.mark.parametrize("entry", STORE_ENTRIES)
def test_subclasses_are_stored_and_read_back_as_their_base_types(
    entry: str, make: Callable[[], object], plain: dict[str, object]
) -> None:
    """Follow-up E / assumption 33 as amended: the map stores nested subclass
    containers as plain `dict` and `list`, and every read-back value is an
    exact built-in type equal to the plain equivalent."""

    expected = canonicalize_ip_attributes(plain)
    assert expected.document == plain
    _assert_stored_outcome(entry, make(), expected)


def test_a_bool_x_value_reads_back_as_a_bool() -> None:
    records = IpAttributeRecords(IPV4)
    document = {"attributes_version": 1, "x_t": True, "x_f": False, "x_l": [True, [False]]}
    records.record(V4, document)
    stored: Any = records[V4]
    assert dict(stored) == document
    assert stored["x_t"] is True
    assert stored["x_f"] is False
    assert stored["x_l"][0] is True
    assert stored["x_l"][1][0] is False


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"attributes_version": True}, id="version-true"),
        pytest.param({"attributes_version": False}, id="version-false"),
        pytest.param({"attributes_version": 1, "weight": True}, id="weight-true"),
        pytest.param({"attributes_version": 1, "weight": False}, id="weight-false"),
    ],
)
@pytest.mark.parametrize("entry", STORE_ENTRIES)
def test_a_bool_is_not_an_integer_for_the_version_or_the_weight(
    entry: str, document: dict[str, object]
) -> None:
    _assert_stored_outcome(entry, document, None)


# --- Ruling A: one read, into the copy that is stored. ----------------------

NAN = math.nan

# (build the document, the plain document holding the same content -- or None
# where no plain dict can hold it -- and whether that content is accepted)
LYING_DOCUMENTS: list[Any] = [
    pytest.param(
        lambda: {"attributes_version": 1, "x_l": _lying_list([1], [NAN])},
        {"attributes_version": 1, "x_l": [1]},
        True,
        id="list-holds-1-shows-nan",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_l": _lying_list(["a" * 5000], [1])},
        {"attributes_version": 1, "x_l": ["a" * 5000]},
        False,
        id="list-holds-5000-characters-shows-1",
    ),
    pytest.param(
        lambda: _lying_dict({"attributes_version": 1, "sources": [1]}, {"attributes_version": 1}),
        {"attributes_version": 1, "sources": [1]},
        False,
        id="document-hides-sources",
    ),
    pytest.param(
        lambda: _lying_dict(
            {"attributes_version": 1, "x_ok": 1}, {"attributes_version": 1, "sources": [1]}
        ),
        {"attributes_version": 1, "x_ok": 1},
        True,
        id="document-shows-sources-holds-valid",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": _lying_dict({"k": NAN}, {"k": 1})},
        {"attributes_version": 1, "x_d": {"k": NAN}},
        False,
        id="nested-dict-hides-nan",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": _lying_dict({"k": "a" * 2000}, {"k": 1})},
        {"attributes_version": 1, "x_d": {"k": "a" * 2000}},
        False,
        id="nested-dict-hides-oversize",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": _lying_dict({"k": 1}, {"k": NAN})},
        {"attributes_version": 1, "x_d": {"k": 1}},
        True,
        id="nested-dict-shows-nan-holds-1",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_s": _lying_str("ok\ud800", "ok")},
        {"attributes_version": 1, "x_s": "ok\ud800"},
        False,
        id="str-value-hides-surrogate",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": {_lying_str("k\ud800", "k"): 1}},
        {"attributes_version": 1, "x_d": {"k\ud800": 1}},
        False,
        id="nested-str-key-hides-surrogate",
    ),
    pytest.param(
        lambda: {"attributes_version": 2, _lying_str("\ud800", "ok"): 1},
        {"attributes_version": 2, "\ud800": 1},
        False,
        id="top-level-str-key-hides-surrogate-at-v2",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_s": _lying_str("ok", "\ud800")},
        {"attributes_version": 1, "x_s": "ok"},
        True,
        id="str-value-shows-surrogate-holds-ok",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _lying_str("sources", "x_ok"): 1},
        {"attributes_version": 1, "sources": 1},
        False,
        id="key-holds-sources-shows-x-name",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _lying_str("x_ok", "sources"): 1},
        {"attributes_version": 1, "x_ok": 1},
        True,
        id="key-holds-x-name-shows-sources",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _Impersonator("sources"): 5},
        {"attributes_version": 1, "sources": 5},
        False,
        id="key-equal-to-weight-spelt-sources",
    ),
    pytest.param(
        lambda: {"attributes_version": 2, _Impersonator("sources"): 5},
        {"attributes_version": 2, "sources": 5},
        True,
        id="key-equal-to-weight-spelt-sources-at-v2",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "weight": _lying_int(10**9, 5)},
        {"attributes_version": 1, "weight": 10**9},
        False,
        id="int-weight-holds-10-9",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "weight": _lying_int(5, 10**9)},
        {"attributes_version": 1, "weight": 5},
        True,
        id="int-weight-holds-5",
    ),
    pytest.param(
        lambda: {"attributes_version": _lying_int(0, 1)},
        {"attributes_version": 0},
        False,
        id="int-version-holds-0",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, _Twin("x_a"): 1, _Twin("x_a"): 2},
        None,
        False,
        id="two-twin-keys",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_a": 1, _Twin("x_a"): 2},
        None,
        False,
        id="plain-and-twin-key",
    ),
    pytest.param(
        lambda: {"attributes_version": 1, "x_d": {_Twin("k"): 1, _Twin("k"): 2}},
        None,
        False,
        id="nested-twin-keys",
    ),
]


@pytest.mark.parametrize(("make", "held", "accepted"), LYING_DOCUMENTS)
@pytest.mark.parametrize("entry", STORE_ENTRIES)
def test_the_stored_record_is_what_the_document_holds_not_what_it_shows(
    entry: str, make: Callable[[], object], held: dict[str, object] | None, accepted: bool
) -> None:
    """Ruling A: the store keeps the canonical copy, read once through the
    base types' own slots; a rejection leaves the trie and the map as they
    were."""

    expected = None if held is None else _canonical_or_none(held)
    assert (expected is not None) is accepted
    _assert_stored_outcome(entry, make(), expected)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: _FAKE_DICT_TYPE(), id="s1-whole-document-claims-dict"),
        pytest.param(
            lambda: {"attributes_version": 1, "x_d": _FAKE_DICT_TYPE()},
            id="s4-nested-value-claims-dict",
        ),
        pytest.param(
            lambda: {"attributes_version": 1, "x_s": _FAKE_STR_TYPE()},
            id="s4-nested-value-claims-str",
        ),
    ],
)
@pytest.mark.parametrize("entry", STORE_ENTRIES)
def test_an_object_claiming_a_type_through_class_is_not_stored(
    entry: str, make: Callable[[], object]
) -> None:
    assert isinstance(_FAKE_DICT_TYPE(), dict)
    _assert_stored_outcome(entry, make(), None)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: MappingProxyType({"attributes_version": 1}), id="mapping-proxy"),
        pytest.param(lambda: _PlainMapping({"attributes_version": 1}), id="collections-mapping"),
        pytest.param(lambda: DEFAULT_ATTRIBUTES, id="default-attributes"),
    ],
)
@pytest.mark.parametrize("entry", STORE_ENTRIES)
def test_a_top_level_mapping_that_is_not_a_dict_is_refused(
    entry: str, make: Callable[[], object]
) -> None:
    """Ruling A.4 / assumption 53: S1 means a `dict`, `DEFAULT_ATTRIBUTES`
    included; `None` is how a caller asks for the default."""

    _assert_stored_outcome(entry, make(), None)


def test_none_still_stores_the_default_after_the_default_itself_is_refused() -> None:
    trie = PatriciaTrie(IPV4)
    records = IpAttributeRecords(IPV4)
    default: Any = DEFAULT_ATTRIBUTES
    with pytest.raises(InvalidAttributesError):
        records.record(V4, default)
    with pytest.raises(InvalidAttributesError):
        apply_hot_ip_added(trie, records, V4, default)
    assert len(records) == 0
    assert trie.hot_ip_count == 0

    records.record(V4_C, None)
    assert dict(records[V4_C]) == {"attributes_version": 1}
    assert apply_hot_ip_added(trie, records, V4, None) is True
    assert dict(records[V4]) == {"attributes_version": 1}
    assert records.serialized_bytes == 48


# --- Ruling C: a hostile document gives the plain document's outcome. -------


def _full(k: _Kinds) -> object:
    """Every kind at every depth; a valid version-1 document."""

    return k.o(
        {
            k.t("attributes_version"): k.n(1),
            k.t("weight"): k.n(1450),
            k.t("x_text"): k.t("café"),
            k.t("x_real"): k.r(2.5),
            k.t("x_list"): k.a(
                [k.n(-3), k.t("a"), k.a([k.r(0.5), None]), k.o({k.t("k"): k.t("v")})]
            ),
            k.t("x_obj"): k.o(
                {k.t("n"): k.a([]), k.t("m"): k.o({}), k.t("t"): True, k.t("f"): False}
            ),
            k.t("x_none"): None,
        }
    )


def _seventeen(k: _Kinds) -> object:
    entries = {k.t(f"x_key_{i}"): k.n(i) for i in range(16)}
    return k.o({k.t("attributes_version"): k.n(1), **entries})


def _v1(k: _Kinds, key: str, value: object) -> object:
    return k.o({k.t("attributes_version"): k.n(1), k.t(key): value})


# (build from a set of kinds, whether the plain twin is accepted)
SHAPES: list[Any] = [
    pytest.param(_full, True, id="every-kind-nested"),
    pytest.param(lambda k: k.o({k.t("attributes_version"): k.n(1)}), True, id="minimal"),
    pytest.param(
        lambda k: k.o({k.t("attributes_version"): k.r(1.0), k.t("weight"): k.r(500.0)}),
        True,
        id="integral-reals",
    ),
    pytest.param(
        lambda k: k.o({k.t("attributes_version"): k.n(2), k.t("severity"): k.a([k.n(3)])}),
        True,
        id="version-2-pass-through",
    ),
    pytest.param(lambda k: _v1(k, "unregistered", k.n(1)), False, id="r1-unregistered"),
    pytest.param(lambda k: _v1(k, "sources", k.a([])), False, id="r1-sources"),
    pytest.param(lambda k: _v1(k, "weight", k.n(10**9)), False, id="r2-weight-too-big"),
    pytest.param(lambda k: k.o({k.t("attributes_version"): k.n(0)}), False, id="s3-version-0"),
    pytest.param(lambda k: _v1(k, "x_list", k.a([k.r(NAN)])), False, id="s4-nan"),
    pytest.param(lambda k: _v1(k, "x_list", k.a([k.t("\ud800")])), False, id="s5-value"),
    pytest.param(lambda k: _v1(k, "x_obj", k.o({k.t("\udc00"): k.n(1)})), False, id="s5-key"),
    pytest.param(lambda k: _v1(k, "x_text", k.t("a" * 2000)), False, id="s6-long-text"),
    pytest.param(lambda k: _v1(k, "x_list", k.a([k.n(0)] * 600)), False, id="s6-long-list"),
    pytest.param(_seventeen, False, id="s2-17-keys"),
    pytest.param(
        lambda k: k.a([k.o({k.t("attributes_version"): k.n(1)})]), False, id="s1-whole-array"
    ),
]

MODES = [
    pytest.param(RuntimeError, False, id="raise-RuntimeError"),
    pytest.param(OverflowError, False, id="raise-OverflowError"),
    pytest.param(KeyError, False, id="raise-KeyError"),
    pytest.param(StopIteration, False, id="raise-StopIteration"),
    pytest.param(ZeroDivisionError, False, id="raise-ZeroDivisionError"),
    pytest.param(RuntimeError, True, id="never-finish"),
]


@pytest.mark.parametrize(("error", "endless"), MODES)
@pytest.mark.parametrize(("shape", "accepted"), SHAPES)
@pytest.mark.parametrize("entry", STORE_ENTRIES)
def test_hostile_subclasses_are_stored_or_refused_exactly_as_the_plain_document(
    entry: str,
    shape: Callable[[_Kinds], object],
    accepted: bool,
    error: type[BaseException],
    endless: bool,
) -> None:
    """Ruling C: keys, values and containers whose every overridable method
    raises, or whose iteration never ends, are stored as the plain twin's
    canonical copy or refused as it is, with `InvalidAttributesError` alone
    and before the trie is touched. A regression in the never-finish mode
    hangs; that is the signal."""

    expected = _canonical_or_none(shape(_PLAIN))
    assert (expected is not None) is accepted
    _assert_stored_outcome(entry, shape(_HOSTILE), expected, error=error, endless=endless)


# --- Ruling D: every read is a fresh, exact-typed document. -----------------

READ_BACK: dict[str, Any] = {
    "attributes_version": 1,
    "weight": 3,
    "x_list": [1, [2, {"k": "v"}]],
    "x_obj": {"k": {"j": [True, None, 0.5]}},
}
READS = ["getitem", "get", "values", "items"]


def _read(records: IpAttributeRecords, address: Address, how: str) -> Any:
    """One of decision 5's read paths, on a map holding only `address`."""

    if how == "getitem":
        return records[address]
    if how == "get":
        return records.get(address)
    if how == "values":
        (value,) = records.values()
        return value
    ((key, value),) = records.items()
    assert key == address
    return value


@pytest.mark.parametrize("how", READS)
def test_every_read_decodes_a_fresh_exact_typed_document(how: str) -> None:
    """Ruling D / assumptions 57-58: two reads are distinct objects at every
    depth, each a read-only view over exact built-in types."""

    records = IpAttributeRecords(IPV4)
    records.record(V4, copy.deepcopy(READ_BACK))
    first = _read(records, V4, how)
    second = _read(records, V4, how)

    assert first is not second
    assert records[V4] is not records[V4]
    assert first["x_list"] is not second["x_list"]
    assert first["x_list"][1] is not second["x_list"][1]
    assert first["x_list"][1][1] is not second["x_list"][1][1]
    assert first["x_obj"] is not second["x_obj"]
    assert first["x_obj"]["k"]["j"] is not second["x_obj"]["k"]["j"]
    for view in (first, second):
        assert type(view) is MappingProxyType
        _assert_exact(dict(view))
        assert dict(view) == READ_BACK


def test_every_read_of_the_default_record_is_fresh_too() -> None:
    records = IpAttributeRecords(IPV4)
    records.record(V4)
    first, second = records[V4], records[V4]
    assert first is not second
    assert dict(first) == dict(second) == {"attributes_version": 1}
    _assert_exact(dict(first))


@pytest.mark.parametrize("how", READS)
def test_mutating_a_read_at_any_depth_changes_nothing_stored(how: str) -> None:
    """Ruling D: nothing a reader does to a returned record, at any depth,
    reaches the map, its byte total, a later read or the section 46.5 checks.
    The top level still refuses assignment."""

    trie = PatriciaTrie(IPV4)
    records = IpAttributeRecords(IPV4)
    assert apply_hot_ip_added(trie, records, V4, copy.deepcopy(READ_BACK)) is True
    size = records.serialized_bytes
    assert size == _compact_size(READ_BACK)

    view = _read(records, V4, how)
    view["x_list"].append("a" * 2000)
    view["x_list"][1][1]["k"] = "changed"
    view["x_list"][1].insert(0, {"grown": "a" * 2000})
    view["x_obj"]["k"]["j"].clear()
    view["x_obj"]["added"] = {"x": "a" * 2000}
    with pytest.raises(TypeError):
        view["weight"] = 4

    assert dict(records[V4]) == READ_BACK
    assert records.serialized_bytes == size
    _check_coupled(trie, records)
