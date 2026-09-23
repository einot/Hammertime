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
* Decision 4's "a `Prefix` or `Address` of the other family is a
  `ValueError`" is applied to `PrefixMetadataStore.__contains__` as well as to
  the other methods: the ADR states it without exception, and decision 5 does
  the same for `a in records`. The only "other argument" that can also be
  wrong is `declare`'s document, so that is the combined case.
* In the hypothesis strategies each metadata key has one fixed kind, so a
  random document never mixes kinds under a key (that is a separate,
  explicit test).
* ADR-0015 decision 2 motivates `PrefixStats` with a `/23` whose `/24`
  children are at 50 % and 0 %, calling the parent's 25 % "neither the sum nor
  the mean" -- but the mean of 50 % and 0 % *is* 25 %. The worked example is
  pinned exactly as stated (Fraction(128, 512)), and ratio-averaging is ruled
  out separately with parts of unequal size, where sum, mean and the true
  ratio all differ.
* Snapshots of the record map deep-copy every record, so a later nested
  mutation by the code under test could not make "before" and "after" agree
  by aliasing.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Mapping
from fractions import Fraction
from typing import Any

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.core.errors import InvalidAttributesError, InvariantViolation
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
