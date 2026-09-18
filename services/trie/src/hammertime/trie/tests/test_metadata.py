"""Prefix metadata is stored where declared, combined on lookup, never
materialized; per-IP attributes live beside the trie and are never
interpreted.

Spec: section 16 (local metadata, `effective_metadata(IP) = combine(root,
/1, ..., /32)`, `combine()` defined per metadata type: set union, bitmask,
priority/override), section 17 (inherited metadata is not copied into
descendants; a lookup accumulates along the path), section 46.2 (`x_` keys
and a higher `attributes_version` are stored and echoed verbatim, never
interpreted), section 46.5 (the per-IP map is keyed by full address, a
`HotIpAdded` replaces the record, absent attributes == `{"attributes_version":
1}`), section 46.6 (the two mechanisms are distinct: prefix metadata's lifetime
is independent of hot state), section 46.7 (the stored mapping is what
`GET /ip` returns), section 46.8 (`ip_attribute_bytes` is the total serialized
size of stored records).
ADR-0012 decision 5 (`Policy`, `validate_metadata_name`, `METADATA_NAME`;
`SetUnion`/`BitmaskOr` associative and commutative, `PriorityOverride`
associative and NOT commutative with a tie won by `local`; `MetadataRegistry`;
`effective_metadata` folds `path_metadata` root-first), decision 6
(`DEFAULT_ATTRIBUTES`, `HotIpRecord`, `IpAttributeStore`), decision 2 (the
trie constructors and `set_local_metadata` / `local_metadata` /
`path_metadata` / `clear_local_metadata`). ADR-0010 decisions 1-2 are covered
in `hammertime.core.tests.test_prefix_state`, not here.

Both `BinaryTrie` (the oracle) and `PatriciaTrie` (production) are driven
through the same tests: sections 16/17 are statements about the *logical*
trie, and ADR-0012 decision 3 makes `local_metadata` and `path_metadata` part
of the equivalence contract.

ADR-0012 Amendment 1 rules three points this file rests on: A1 (`combine_path`
raises `KeyError` for any unregistered name at *any* level, the single-level
case included; only a *registered* single-level name passes through, with its
combiner not called; which unregistered name is reported first is deliberately
not pinned, so no test here asserts an ordering), A3 (`records()` yields every
IPv4 record ascending by `Address.value`, then every IPv6 record ascending),
and A5 (the two "confirmed" assumptions below).

ASSUMPTIONS (stated so a reader can push back on each):

* `MetadataRegistry.combiner(name)` returns the very object that was
  registered under `name` -- confirmed by Amendment 1 A5 (identity, not a
  copy or wrapper: a registry that wrapped combiners would make
  `PriorityOverride`'s tie rule untestable through the registry).
* `DEFAULT_ATTRIBUTES` is read-only at both type and runtime -- confirmed by
  A5: the annotation is `Final[Mapping[str, object]]` (never `dict`), so the
  item assignment below is a mypy `[index]` error and the `# type:
  ignore[index]` on it is a *used* ignore under `warn_unused_ignores`; the
  runtime object is a `MappingProxyType`, so the same statement raises
  `TypeError`. Both halves are asserted; neither may be relaxed.
* `Address` is hashable and usable as a dict key (ADR-0012 decision 6 keys
  the store by address; ADR-0005 says "a map keyed by full address").
* A metadata *value* is an arbitrary object; the spec's `{"internal"}` is
  written as a `frozenset` here because `SetUnion` yields frozensets. One
  test passes plain `set`s through the trie to show section 17's spelling
  also works.
* The combiners are only ever bound to locals annotated `Combiner[Any]`, so
  the tests do not depend on whether `SetUnion` and friends are generic.
* Name validation in the trie's own setters (Amendment 1 A2) is T1's to
  cover, not this file's: nothing here passes an invalid name to
  `set_local_metadata` or `clear_local_metadata`.
"""

import json
from collections.abc import Mapping
from typing import Any

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.trie.metadata.combine import (
    BitmaskOr,
    Combiner,
    MetadataRegistry,
    PriorityOverride,
    SetUnion,
    effective_metadata,
)
from hammertime.trie.metadata.ip_attributes import DEFAULT_ATTRIBUTES, HotIpRecord, IpAttributeStore
from hammertime.trie.metadata.local import METADATA_NAME, Policy, validate_metadata_name
from hammertime.trie.structure.binary_trie import BinaryTrie
from hammertime.trie.structure.patricia import PatriciaTrie
from hypothesis import given, settings
from hypothesis import strategies as st

AnyTrie = BinaryTrie | PatriciaTrie

# Section 17's example, extended by one level so that a lookup has two
# ancestors to accumulate.
SLASH_8 = Prefix.parse("10.0.0.0/8")
SLASH_16 = Prefix.parse("10.20.0.0/16")
SLASH_24 = Prefix.parse("10.20.30.0/24")
HOST = Prefix.parse("10.20.30.1/32")
INSIDE_BOTH = Address.parse("10.20.30.1")
INSIDE_8_ONLY = Address.parse("10.99.0.1")
OUTSIDE = Address.parse("192.0.2.1")

INTERNAL = frozenset({"internal"})
VPN = frozenset({"vpn"})
INTERNAL_AND_VPN = frozenset({"internal", "vpn"})


@pytest.fixture(params=[BinaryTrie, PatriciaTrie], ids=["binary", "patricia"])
def trie(request: pytest.FixtureRequest) -> AnyTrie:
    trie_class: type[BinaryTrie] | type[PatriciaTrie] = request.param
    return trie_class(AddressFamily.IPV4)


@pytest.fixture(params=[BinaryTrie, PatriciaTrie], ids=["binary", "patricia"])
def trie_v6(request: pytest.FixtureRequest) -> AnyTrie:
    trie_class: type[BinaryTrie] | type[PatriciaTrie] = request.param
    return trie_class(AddressFamily.IPV6)


def _registry() -> MetadataRegistry:
    registry = MetadataRegistry()
    registry.register("tags", SetUnion())
    registry.register("flags", BitmaskOr())
    registry.register("policy", PriorityOverride())
    return registry


def _ancestor(ip: Address, length: int) -> Prefix:
    """The /length prefix containing `ip`."""

    bit_length = ip.family.bit_length
    mask = ((1 << length) - 1) << (bit_length - length) if length else 0
    return Prefix(family=ip.family, network=ip.value & mask, length=length)


def _path(trie: AnyTrie, ip: Address) -> list[tuple[Prefix, dict[str, object]]]:
    """`path_metadata` as a comparable list."""

    return [(prefix, dict(metadata)) for prefix, metadata in trie.path_metadata(ip)]


# --- metadata names (section 16; ADR-0012 decision 5) ------------------------------


class TestMetadataName:
    """`METADATA_NAME = ^[a-z][a-z0-9_]{0,63}$`; `validate_metadata_name` raises
    `ValueError` for anything else."""

    @pytest.mark.parametrize("name", ["a", "tags", "policy_v2", "x1", "a" * 64, "abc_" + "9" * 60])
    def test_accepts_a_well_formed_name(self, name: str) -> None:
        validate_metadata_name(name)  # must not raise
        assert METADATA_NAME.fullmatch(name) is not None

    @pytest.mark.parametrize(
        "name",
        ["", "A", "Tags", "1tag", "_tag", "tag-name", "tag name", "tag.name", "a" * 65, "tags\n"],
        ids=[
            "empty",
            "uppercase-single",
            "uppercase-initial",
            "digit-initial",
            "underscore-initial",
            "hyphen",
            "space",
            "dot",
            "too-long",
            "trailing-newline",
        ],
    )
    def test_rejects_a_malformed_name(self, name: str) -> None:
        with pytest.raises(ValueError):
            validate_metadata_name(name)

    def test_the_pattern_and_the_validator_agree(self) -> None:
        # `$` alone would let a trailing newline through; the validator must
        # not be looser than the compiled pattern.
        for name in ("tags", "tags\n", "TAGS", "a" * 64, "a" * 65):
            matches = METADATA_NAME.fullmatch(name) is not None
            try:
                validate_metadata_name(name)
            except ValueError:
                assert not matches
            else:
                assert matches


class TestPolicy:
    """Section 16's "priority/override" example carrier."""

    def test_is_a_value_type(self) -> None:
        assert Policy(priority=1, value="a") == Policy(priority=1, value="a")
        assert Policy(priority=1, value="a") != Policy(priority=2, value="a")
        assert Policy(priority=1, value="a") != Policy(priority=1, value="b")
        assert hash(Policy(priority=1, value="a")) == hash(Policy(priority=1, value="a"))

    def test_is_immutable(self) -> None:
        policy = Policy(priority=1, value="a")
        with pytest.raises(AttributeError):
            policy.priority = 2  # type: ignore[misc]


# --- combiners (section 16; ADR-0012 decision 5) -----------------------------------

_SETS = st.frozensets(st.integers(0, 15))
_MASKS = st.integers(0, 2**64 - 1)
_POLICIES = st.builds(Policy, priority=st.integers(-3, 3), value=st.sampled_from("abcd"))


class TestSetUnion:
    def test_is_set_union(self) -> None:
        union: Combiner[Any] = SetUnion()
        assert union.combine(frozenset({"a"}), frozenset({"b"})) == frozenset({"a", "b"})

    def test_empty_is_the_identity(self) -> None:
        union: Combiner[Any] = SetUnion()
        assert union.combine(frozenset(), frozenset({"a"})) == frozenset({"a"})
        assert union.combine(frozenset({"a"}), frozenset()) == frozenset({"a"})

    @given(a=_SETS, b=_SETS)
    @settings(deadline=None, max_examples=100)
    def test_commutative(self, a: frozenset[int], b: frozenset[int]) -> None:
        union: Combiner[Any] = SetUnion()
        assert union.combine(a, b) == union.combine(b, a)

    @given(a=_SETS, b=_SETS, c=_SETS)
    @settings(deadline=None, max_examples=100)
    def test_associative(self, a: frozenset[int], b: frozenset[int], c: frozenset[int]) -> None:
        union: Combiner[Any] = SetUnion()
        assert union.combine(union.combine(a, b), c) == union.combine(a, union.combine(b, c))

    @given(a=_SETS, b=_SETS)
    @settings(deadline=None, max_examples=100)
    def test_is_the_mathematical_union(self, a: frozenset[int], b: frozenset[int]) -> None:
        union: Combiner[Any] = SetUnion()
        assert union.combine(a, b) == a | b


class TestBitmaskOr:
    def test_is_bitwise_or(self) -> None:
        bitmask: Combiner[Any] = BitmaskOr()
        assert bitmask.combine(0b0101, 0b0011) == 0b0111

    def test_zero_is_the_identity(self) -> None:
        bitmask: Combiner[Any] = BitmaskOr()
        assert bitmask.combine(0, 0b1010) == 0b1010
        assert bitmask.combine(0b1010, 0) == 0b1010

    @given(a=_MASKS, b=_MASKS)
    @settings(deadline=None, max_examples=100)
    def test_commutative(self, a: int, b: int) -> None:
        bitmask: Combiner[Any] = BitmaskOr()
        assert bitmask.combine(a, b) == bitmask.combine(b, a)

    @given(a=_MASKS, b=_MASKS, c=_MASKS)
    @settings(deadline=None, max_examples=100)
    def test_associative(self, a: int, b: int, c: int) -> None:
        bitmask: Combiner[Any] = BitmaskOr()
        left = bitmask.combine(bitmask.combine(a, b), c)
        right = bitmask.combine(a, bitmask.combine(b, c))
        assert left == right

    @given(a=_MASKS, b=_MASKS)
    @settings(deadline=None, max_examples=100)
    def test_is_the_mathematical_or(self, a: int, b: int) -> None:
        bitmask: Combiner[Any] = BitmaskOr()
        assert bitmask.combine(a, b) == a | b


class TestPriorityOverride:
    """`combine(inherited, local)`: the higher `Policy.priority` wins; a tie is
    won by `local` (the more specific prefix). Associative, NOT commutative."""

    def test_higher_priority_wins_when_local_is_higher(self) -> None:
        override: Combiner[Any] = PriorityOverride()
        assert override.combine(Policy(1, "root"), Policy(2, "leaf")) == Policy(2, "leaf")

    def test_higher_priority_wins_when_inherited_is_higher(self) -> None:
        # The ancestor can override the descendant: this is what makes the
        # combiner more than "the most specific prefix wins".
        override: Combiner[Any] = PriorityOverride()
        assert override.combine(Policy(9, "root"), Policy(1, "leaf")) == Policy(9, "root")

    def test_a_tie_is_won_by_local(self) -> None:
        override: Combiner[Any] = PriorityOverride()
        assert override.combine(Policy(1, "root"), Policy(1, "leaf")) == Policy(1, "leaf")

    def test_not_commutative(self) -> None:
        override: Combiner[Any] = PriorityOverride()
        root, leaf = Policy(1, "root"), Policy(1, "leaf")
        assert override.combine(root, leaf) != override.combine(leaf, root)

    @given(a=_POLICIES, b=_POLICIES)
    @settings(deadline=None, max_examples=100)
    def test_result_is_an_input_with_the_maximum_priority(self, a: Policy, b: Policy) -> None:
        override: Combiner[Any] = PriorityOverride()
        result = override.combine(a, b)
        assert result in (a, b)
        assert result.priority == max(a.priority, b.priority)

    @given(a=_POLICIES, b=_POLICIES)
    @settings(deadline=None, max_examples=100)
    def test_local_wins_every_tie(self, a: Policy, b: Policy) -> None:
        override: Combiner[Any] = PriorityOverride()
        if a.priority == b.priority:
            assert override.combine(a, b) == b

    @given(a=_POLICIES, b=_POLICIES, c=_POLICIES)
    @settings(deadline=None, max_examples=200)
    def test_associative(self, a: Policy, b: Policy, c: Policy) -> None:
        override: Combiner[Any] = PriorityOverride()
        left = override.combine(override.combine(a, b), c)
        right = override.combine(a, override.combine(b, c))
        assert left == right

    @given(a=_POLICIES, b=_POLICIES)
    @settings(deadline=None, max_examples=100)
    def test_order_matters_exactly_on_ties_between_distinct_policies(
        self, a: Policy, b: Policy
    ) -> None:
        # Commutativity fails precisely when both orders pick a different
        # policy: same priority, different value.
        override: Combiner[Any] = PriorityOverride()
        commutes = override.combine(a, b) == override.combine(b, a)
        assert commutes == (a.priority != b.priority or a == b)


# --- the registry (ADR-0012 decision 5) ---------------------------------------------


class TestMetadataRegistry:
    def test_register_then_combiner_returns_it(self) -> None:
        # Identity, not a copy or wrapper (Amendment 1 A5): a registry that
        # wrapped combiners would make PriorityOverride's tie rule
        # untestable through the registry.
        registry = MetadataRegistry()
        union: Combiner[Any] = SetUnion()
        registry.register("tags", union)
        assert registry.combiner("tags") is union

    def test_register_rejects_a_duplicate_name(self) -> None:
        registry = MetadataRegistry()
        registry.register("tags", SetUnion())
        with pytest.raises(ValueError):
            registry.register("tags", SetUnion())

    def test_register_rejects_a_duplicate_even_with_a_different_combiner(self) -> None:
        registry = MetadataRegistry()
        registry.register("tags", SetUnion())
        with pytest.raises(ValueError):
            registry.register("tags", BitmaskOr())

    def test_a_rejected_duplicate_leaves_the_first_registration_in_place(self) -> None:
        registry = MetadataRegistry()
        union: Combiner[Any] = SetUnion()
        registry.register("tags", union)
        with pytest.raises(ValueError):
            registry.register("tags", BitmaskOr())
        assert registry.combiner("tags") is union

    @pytest.mark.parametrize("name", ["", "Tags", "1tag", "tag-name", "a" * 65])
    def test_register_rejects_an_invalid_name(self, name: str) -> None:
        registry = MetadataRegistry()
        with pytest.raises(ValueError):
            registry.register(name, SetUnion())

    def test_an_invalid_name_is_not_registered(self) -> None:
        registry = MetadataRegistry()
        with pytest.raises(ValueError):
            registry.register("Tags", SetUnion())
        with pytest.raises(KeyError):
            registry.combiner("Tags")

    def test_combiner_raises_key_error_for_an_unregistered_name(self) -> None:
        registry = MetadataRegistry()
        with pytest.raises(KeyError):
            registry.combiner("tags")

    def test_registrations_are_independent_per_registry(self) -> None:
        first, second = MetadataRegistry(), MetadataRegistry()
        first.register("tags", SetUnion())
        with pytest.raises(KeyError):
            second.combiner("tags")


class TestCombinePath:
    """`combine_path(path)`: every name appearing at any level must be
    registered, else `KeyError` (Amendment 1 A1). A *registered* name present
    at only one level is returned as-is, its combiner not called; a registered
    name at several levels is folded root-first.

    Every pass-through test below uses `_registry()`, in which `tags`, `flags`
    and `policy` are all registered -- that is what makes pass-through the
    right expectation for them rather than `KeyError`."""

    def test_empty_path_is_empty(self) -> None:
        assert _registry().combine_path([]) == {}

    def test_path_of_empty_levels_is_empty(self) -> None:
        assert _registry().combine_path([{}, {}, {}]) == {}

    def test_returns_a_dict(self) -> None:
        result = _registry().combine_path([{"tags": INTERNAL}])
        assert isinstance(result, dict)

    def test_a_registered_name_present_at_one_level_passes_through(self) -> None:
        # "tags" is registered in _registry(); A1's pass-through applies to
        # registered names only.
        assert _registry().combine_path([{}, {"tags": INTERNAL}, {}]) == {"tags": INTERNAL}

    def test_registered_names_at_one_level_pass_through_for_every_combiner(self) -> None:
        # All three names are registered; each appears once, so no combiner
        # runs and every value is handed back unchanged.
        policy = Policy(3, "only")
        level: dict[str, object] = {"tags": INTERNAL, "flags": 0b100, "policy": policy}
        assert _registry().combine_path([level]) == level

    def test_set_union_over_three_levels(self) -> None:
        path = [{"tags": INTERNAL}, {"tags": VPN}, {"tags": frozenset({"eu"})}]
        assert _registry().combine_path(path) == {"tags": frozenset({"internal", "vpn", "eu"})}

    def test_bitmask_or_over_three_levels(self) -> None:
        path = [{"flags": 0b001}, {"flags": 0b100}, {"flags": 0b001}]
        assert _registry().combine_path(path) == {"flags": 0b101}

    def test_folds_root_first_so_a_tie_goes_to_the_deepest_level(self) -> None:
        path = [
            {"policy": Policy(1, "root")},
            {"policy": Policy(1, "mid")},
            {"policy": Policy(1, "leaf")},
        ]
        assert _registry().combine_path(path) == {"policy": Policy(1, "leaf")}

    def test_folds_root_first_so_reversing_the_path_changes_a_tie(self) -> None:
        # The same two levels, leaf-first: if the registry folded in the
        # wrong direction the two calls would agree.
        root_first = [{"policy": Policy(1, "root")}, {"policy": Policy(1, "leaf")}]
        leaf_first = list(reversed(root_first))
        assert _registry().combine_path(root_first) == {"policy": Policy(1, "leaf")}
        assert _registry().combine_path(leaf_first) == {"policy": Policy(1, "root")}

    def test_a_higher_priority_ancestor_overrides_a_descendant(self) -> None:
        path = [{"policy": Policy(9, "root")}, {}, {"policy": Policy(1, "leaf")}]
        assert _registry().combine_path(path) == {"policy": Policy(9, "root")}

    def test_names_are_combined_independently(self) -> None:
        path = [
            {"tags": INTERNAL, "flags": 0b01},
            {"tags": VPN, "policy": Policy(2, "mid")},
            {"flags": 0b10, "policy": Policy(1, "leaf")},
        ]
        assert _registry().combine_path(path) == {
            "tags": INTERNAL_AND_VPN,
            "flags": 0b11,
            "policy": Policy(2, "mid"),
        }

    def test_an_unregistered_name_at_two_levels_is_a_key_error(self) -> None:
        registry = MetadataRegistry()
        registry.register("tags", SetUnion())
        with pytest.raises(KeyError):
            registry.combine_path([{"tags": INTERNAL, "owner": "a"}, {"owner": "b"}])

    def test_an_unregistered_name_at_exactly_one_level_is_a_key_error(self) -> None:
        # Amendment 1 A1: the registry check is unconditional, so the
        # single-level case fails exactly like the two-level one. A
        # pass-through here would mean an undefined `combine()` succeeded
        # until a second level happened to appear (section 16: "the
        # combine() operation MUST be explicitly defined for each metadata
        # type").
        registry = MetadataRegistry()
        registry.register("tags", SetUnion())
        with pytest.raises(KeyError):
            registry.combine_path([{"tags": INTERNAL}, {"owner": "a"}])

    def test_an_unregistered_name_is_a_key_error_on_a_single_level_path(self) -> None:
        with pytest.raises(KeyError):
            MetadataRegistry().combine_path([{"owner": "a"}])

    def test_an_unregistered_name_is_a_key_error_even_beside_registered_ones(self) -> None:
        # The registered names would combine cleanly; one unknown name at one
        # level is still enough to reject the whole lookup. (Which name is
        # reported is deliberately unpinned by A1, so only the type is
        # asserted.)
        registry = _registry()
        path = [{"tags": INTERNAL}, {"tags": VPN, "owner": "a"}, {"flags": 0b1}]
        with pytest.raises(KeyError):
            registry.combine_path(path)

    def test_accepts_any_iterable_of_mappings(self) -> None:
        levels = (level for level in [{"tags": INTERNAL}, {"tags": VPN}])
        assert _registry().combine_path(levels) == {"tags": INTERNAL_AND_VPN}

    def test_does_not_mutate_its_input(self) -> None:
        root: dict[str, object] = {"tags": INTERNAL}
        leaf: dict[str, object] = {"tags": VPN}
        _registry().combine_path([root, leaf])
        assert root == {"tags": INTERNAL}
        assert leaf == {"tags": VPN}


# --- effective_metadata over the tries (sections 16, 17, 46.6) ---------------------


class TestEffectiveMetadata:
    """Section 16: `effective_metadata(IP) = combine(root.local_metadata, ...,
    /32.local_metadata)`. Section 17: `/8.local_metadata = {"internal"}` and
    nothing is copied into the /32s."""

    def test_section_17_example_accumulates_down_the_path(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": INTERNAL_AND_VPN}

    def test_section_17_example_with_plain_sets(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", {"internal"})
        trie.set_local_metadata(SLASH_16, "tags", {"vpn"})
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": {"internal", "vpn"}}

    def test_inherited_metadata_is_never_materialized_on_the_host_route(
        self, trie: AnyTrie
    ) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        assert trie.local_metadata(HOST) == {}

    def test_inherited_metadata_is_never_materialized_on_any_intermediate_prefix(
        self, trie: AnyTrie
    ) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        for length in range(33):
            expected: Mapping[str, object] = {}
            if length == 8:
                expected = {"tags": INTERNAL}
            elif length == 16:
                expected = {"tags": VPN}
            assert trie.local_metadata(_ancestor(INSIDE_BOTH, length)) == expected

    def test_local_metadata_is_stored_exactly_where_declared(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        assert trie.local_metadata(SLASH_8) == {"tags": INTERNAL}
        assert trie.local_metadata(SLASH_16) == {"tags": VPN}

    def test_an_address_under_only_the_slash_8_sees_only_its_metadata(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        assert effective_metadata(trie, INSIDE_8_ONLY, _registry()) == {"tags": INTERNAL}

    def test_an_address_outside_every_declared_prefix_sees_nothing(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        assert effective_metadata(trie, OUTSIDE, _registry()) == {}

    def test_an_empty_trie_yields_nothing(self, trie: AnyTrie) -> None:
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {}
        assert trie.local_metadata(SLASH_8) == {}
        assert _path(trie, INSIDE_BOTH) == []

    def test_path_metadata_is_root_to_leaf_and_non_empty_only(self, trie: AnyTrie) -> None:
        # Declared leaf-first on purpose: the order of `path_metadata` is the
        # path's, not the declaration's.
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        expected = [(SLASH_8, {"tags": INTERNAL}), (SLASH_16, {"tags": VPN})]
        assert _path(trie, INSIDE_BOTH) == expected

    def test_effective_metadata_is_combine_path_over_path_metadata(self, trie: AnyTrie) -> None:
        # Section 16's definition, literally.
        registry = _registry()
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        trie.set_local_metadata(SLASH_8, "policy", Policy(9, "root"))
        trie.set_local_metadata(SLASH_24, "policy", Policy(1, "leaf"))
        levels = [metadata for _, metadata in trie.path_metadata(INSIDE_BOTH)]
        expected = registry.combine_path(levels)
        assert effective_metadata(trie, INSIDE_BOTH, registry) == expected
        assert expected == {"tags": INTERNAL_AND_VPN, "policy": Policy(9, "root")}

    def test_policy_tie_goes_to_the_more_specific_prefix(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "policy", Policy(1, "root"))
        trie.set_local_metadata(SLASH_16, "policy", Policy(1, "leaf"))
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"policy": Policy(1, "leaf")}

    def test_root_metadata_applies_to_every_address(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(Prefix.parse("0.0.0.0/0"), "tags", frozenset({"all"}))
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": frozenset({"all"})}
        assert effective_metadata(trie, OUTSIDE, _registry()) == {"tags": frozenset({"all"})}

    def test_host_route_metadata_applies_to_that_address_only(self, trie: AnyTrie) -> None:
        pinned = frozenset({"pinned"})
        trie.set_local_metadata(HOST, "tags", pinned)
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": pinned}
        assert effective_metadata(trie, Address.parse("10.20.30.2"), _registry()) == {}

    def test_set_local_metadata_replaces_the_value_for_that_name(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_8, "tags", VPN)
        assert trie.local_metadata(SLASH_8) == {"tags": VPN}
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": VPN}

    def test_names_on_one_prefix_are_independent(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_8, "flags", 0b10)
        assert trie.local_metadata(SLASH_8) == {"tags": INTERNAL, "flags": 0b10}

    def test_an_unregistered_name_on_two_levels_is_a_key_error(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "owner", "root-team")
        trie.set_local_metadata(SLASH_16, "owner", "vpn-team")
        with pytest.raises(KeyError):
            effective_metadata(trie, INSIDE_BOTH, MetadataRegistry())

    def test_an_unregistered_name_on_one_level_is_a_key_error(self, trie: AnyTrie) -> None:
        # Amendment 1 A1 through the whole lookup: the trie has no registry
        # and stores any valid name, so an operator-declared name nobody
        # registered a `combine()` for surfaces here, at the one place the
        # registry exists -- not silently as a pass-through.
        trie.set_local_metadata(SLASH_8, "owner", "root-team")
        with pytest.raises(KeyError):
            effective_metadata(trie, INSIDE_BOTH, MetadataRegistry())

    def test_a_registered_name_on_one_level_is_returned_by_effective_metadata(
        self, trie: AnyTrie
    ) -> None:
        # The other half of A1: registered, single level, combiner not needed.
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": INTERNAL}

    def test_ipv6_accumulates_the_same_way(self, trie_v6: AnyTrie) -> None:
        trie_v6.set_local_metadata(Prefix.parse("2001:db8::/32"), "tags", INTERNAL)
        trie_v6.set_local_metadata(Prefix.parse("2001:db8:1::/48"), "tags", VPN)
        inside_both = Address.parse("2001:db8:1::42")
        inside_32_only = Address.parse("2001:db8:2::1")
        assert effective_metadata(trie_v6, inside_both, _registry()) == {"tags": INTERNAL_AND_VPN}
        assert effective_metadata(trie_v6, inside_32_only, _registry()) == {"tags": INTERNAL}
        assert trie_v6.local_metadata(Prefix.parse("2001:db8:1::42/128")) == {}

    def test_the_other_family_is_rejected(self, trie: AnyTrie) -> None:
        with pytest.raises(ValueError):
            trie.set_local_metadata(Prefix.parse("2001:db8::/32"), "tags", INTERNAL)
        with pytest.raises(ValueError):
            trie.local_metadata(Prefix.parse("2001:db8::/32"))
        with pytest.raises(ValueError):
            effective_metadata(trie, Address.parse("2001:db8::1"), _registry())


class TestClearLocalMetadata:
    def test_clearing_removes_the_name_from_the_path(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        trie.clear_local_metadata(SLASH_8, "tags")
        assert trie.local_metadata(SLASH_8) == {}
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": VPN}

    def test_clearing_one_name_leaves_the_others(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_8, "flags", 0b10)
        trie.clear_local_metadata(SLASH_8, "tags")
        assert trie.local_metadata(SLASH_8) == {"flags": 0b10}

    def test_clearing_an_absent_name_is_a_no_op(self, trie: AnyTrie) -> None:
        trie.clear_local_metadata(SLASH_8, "tags")  # nothing there at all
        trie.set_local_metadata(SLASH_8, "flags", 0b10)
        trie.clear_local_metadata(SLASH_8, "tags")  # node exists, name does not
        assert trie.local_metadata(SLASH_8) == {"flags": 0b10}
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"flags": 0b10}

    def test_clearing_everything_restores_the_empty_answer(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        trie.clear_local_metadata(SLASH_8, "tags")
        trie.clear_local_metadata(SLASH_16, "tags")
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {}
        assert _path(trie, INSIDE_BOTH) == []


class TestMetadataIsIndependentOfHotState:
    """Section 46.6: prefix metadata's lifetime is independent of hot state;
    section 46.1: nothing about a hot IP is aggregated along the path except
    `hot_count`."""

    def test_declaring_metadata_makes_nothing_hot(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(HOST, "tags", VPN)
        assert trie.hot_ip_count == 0
        assert not trie.is_hot(INSIDE_BOTH)
        assert trie.hot_count(SLASH_8) == 0
        assert trie.hot_count(HOST) == 0

    def test_metadata_survives_its_last_hot_descendant_leaving(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        assert trie.add_hot_ip(INSIDE_BOTH)
        assert trie.remove_hot_ip(INSIDE_BOTH)
        assert trie.hot_ip_count == 0
        assert trie.local_metadata(SLASH_16) == {"tags": VPN}
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": VPN}

    def test_metadata_declared_while_hot_survives_cooling(self, trie: AnyTrie) -> None:
        assert trie.add_hot_ip(INSIDE_BOTH)
        trie.set_local_metadata(SLASH_24, "tags", VPN)
        assert trie.remove_hot_ip(INSIDE_BOTH)
        assert trie.local_metadata(SLASH_24) == {"tags": VPN}
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": VPN}

    def test_hot_ips_do_not_acquire_local_metadata(self, trie: AnyTrie) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        hosts = [Address.parse(f"10.20.30.{host}") for host in range(1, 9)]
        for host in hosts:
            assert trie.add_hot_ip(host)
        for host in hosts:
            assert trie.local_metadata(_ancestor(host, 32)) == {}
            assert effective_metadata(trie, host, _registry()) == {"tags": INTERNAL}

    def test_adding_and_removing_hot_ips_leaves_the_path_metadata_alone(
        self, trie: AnyTrie
    ) -> None:
        trie.set_local_metadata(SLASH_8, "tags", INTERNAL)
        trie.set_local_metadata(SLASH_16, "tags", VPN)
        before = _path(trie, INSIDE_BOTH)
        for host in range(1, 5):
            trie.add_hot_ip(Address.parse(f"10.20.30.{host}"))
        trie.add_hot_ip(INSIDE_8_ONLY)
        trie.remove_hot_ip(Address.parse("10.20.30.2"))
        assert _path(trie, INSIDE_BOTH) == before
        assert effective_metadata(trie, INSIDE_BOTH, _registry()) == {"tags": INTERNAL_AND_VPN}


@st.composite
def _declarations(draw: st.DrawFn) -> list[tuple[int, str]]:
    """`(length, tag)` pairs, each declaring `{tag}` on the /length ancestor
    of INSIDE_BOTH -- so every declaration lies on one lookup path."""

    return draw(
        st.lists(
            st.tuples(st.integers(0, 32), st.sampled_from(["a", "b", "c", "d"])),
            max_size=12,
        )
    )


@given(declarations=_declarations(), decoys=st.lists(st.integers(1, 32), max_size=6))
@settings(deadline=None, max_examples=100)
def test_effective_metadata_is_the_union_over_the_containing_prefixes(
    declarations: list[tuple[int, str]], decoys: list[int]
) -> None:
    """Section 16 with `combine = set union`: the effective set at an IP is the
    union of the sets declared on the prefixes that contain it, on both
    tries, and nothing is materialized on any other prefix (section 17)."""

    registry = MetadataRegistry()
    registry.register("tags", SetUnion())
    expected: dict[int, frozenset[str]] = {}
    for length, tag in declarations:
        expected[length] = expected.get(length, frozenset()) | {tag}
    union = frozenset[str]().union(*expected.values())

    for candidate in (BinaryTrie(AddressFamily.IPV4), PatriciaTrie(AddressFamily.IPV4)):
        for length, tags in expected.items():
            candidate.set_local_metadata(_ancestor(INSIDE_BOTH, length), "tags", tags)
        # Decoys: the same lengths on the path of an address outside 10/8
        # (which shares only the /0 with INSIDE_BOTH, and 0 is excluded).
        for length in decoys:
            candidate.set_local_metadata(_ancestor(OUTSIDE, length), "tags", frozenset({"decoy"}))

        result = effective_metadata(candidate, INSIDE_BOTH, registry)
        assert result == ({"tags": union} if expected else {})

        for length in range(33):
            local = candidate.local_metadata(_ancestor(INSIDE_BOTH, length))
            assert local == ({"tags": expected[length]} if length in expected else {})

        path = _path(candidate, INSIDE_BOTH)
        assert [prefix.length for prefix, _ in path] == sorted(expected)
        assert all(metadata for _, metadata in path)


# --- per-IP attributes (sections 46.2, 46.5-46.8; ADR-0012 decision 6) -----------

IP_A = Address.parse("10.0.0.1")
IP_B = Address.parse("10.0.0.2")
IP_C = Address.parse("192.0.2.9")
IP_V6 = Address.parse("2001:db8::1")
IP_V6_LOW = Address.parse("::1")  # integer value 1: below every IPv4 value

WEIGHT_1450: Mapping[str, object] = {"attributes_version": 1, "weight": 1450}
WEIGHT_1000: Mapping[str, object] = {"attributes_version": 1, "weight": 1000}
WEIGHT_2500: Mapping[str, object] = {"attributes_version": 1, "weight": 2500}
WEIGHT_5: Mapping[str, object] = {"attributes_version": 1, "weight": 5}


def _compact(attributes: Mapping[str, object]) -> bytes:
    # ADR-0012 decision 6 / spec section 46.8: `ip_attribute_bytes` is the sum
    # of `len(json.dumps(attributes, separators=(",", ":")).encode())`.
    return json.dumps(dict(attributes), separators=(",", ":")).encode()


def _compact_size(attributes: Mapping[str, object]) -> int:
    return len(_compact(attributes))


def _default_record(window_count: int = 1000) -> HotIpRecord:
    return HotIpRecord(attributes=DEFAULT_ATTRIBUTES, window_count=window_count)


class TestDefaultAttributes:
    def test_is_section_46_5_default_document(self) -> None:
        # "An absent `attributes` is equivalent to {"attributes_version": 1}."
        assert DEFAULT_ATTRIBUTES == {"attributes_version": 1}
        assert dict(DEFAULT_ATTRIBUTES) == {"attributes_version": 1}

    def test_is_immutable(self) -> None:
        # Amendment 1 A5 confirms both halves: the annotation is
        # `Final[Mapping[str, object]]`, so this assignment is a mypy
        # `[index]` error and the ignore below is a *used* ignore; the
        # runtime object is a `MappingProxyType`, so it raises `TypeError`.
        # A plain frozen dict would satisfy the runtime half and not the type.
        with pytest.raises(TypeError):
            DEFAULT_ATTRIBUTES["x_extra"] = 1  # type: ignore[index]
        assert DEFAULT_ATTRIBUTES == {"attributes_version": 1}

    def test_is_not_a_plain_dict(self) -> None:
        # A shared dict would let one record's mutation leak into every
        # default record; A5 fixes a MappingProxyType.
        assert not isinstance(DEFAULT_ATTRIBUTES, dict)

    def test_serializes_to_24_bytes(self) -> None:
        assert _compact(DEFAULT_ATTRIBUTES) == b'{"attributes_version":1}'
        assert _compact_size(DEFAULT_ATTRIBUTES) == 24


class TestHotIpRecord:
    def test_carries_the_document_and_the_window_count(self) -> None:
        record = HotIpRecord(attributes=WEIGHT_1450, window_count=1834)
        assert record.attributes == {"attributes_version": 1, "weight": 1450}
        assert record.window_count == 1834

    def test_is_a_value_type(self) -> None:
        left = _default_record(1000)
        right = HotIpRecord(attributes={"attributes_version": 1}, window_count=1000)
        assert left == right
        assert left != _default_record(1001)

    def test_is_immutable(self) -> None:
        record = _default_record()
        with pytest.raises(AttributeError):
            record.window_count = 5  # type: ignore[misc]


class TestIpAttributeStoreEmpty:
    def test_empty_store(self) -> None:
        store = IpAttributeStore()
        assert len(store) == 0
        assert store.count(AddressFamily.IPV4) == 0
        assert store.count(AddressFamily.IPV6) == 0
        assert list(store.records()) == []
        assert store.serialized_bytes == 0
        assert store.get(IP_A) is None
        assert IP_A not in store

    def test_delete_from_empty_is_false(self) -> None:
        store = IpAttributeStore()
        assert store.delete(IP_A) is False
        assert len(store) == 0


class TestIpAttributeStorePutGetDelete:
    def test_put_then_get(self) -> None:
        store = IpAttributeStore()
        record = HotIpRecord(attributes=WEIGHT_1450, window_count=1834)
        store.put(IP_A, record)
        assert store.get(IP_A) == record
        assert IP_A in store
        assert len(store) == 1
        assert store.count(AddressFamily.IPV4) == 1
        assert store.count(AddressFamily.IPV6) == 0

    def test_get_of_another_address_is_none(self) -> None:
        store = IpAttributeStore()
        store.put(IP_A, _default_record())
        assert store.get(IP_B) is None
        assert IP_B not in store

    def test_put_replaces(self) -> None:
        # Section 46.5: "HotIpAdded replaces any existing record for that IP".
        store = IpAttributeStore()
        first = HotIpRecord(attributes=WEIGHT_1000, window_count=1000)
        second = HotIpRecord(attributes=WEIGHT_2500, window_count=2500)
        store.put(IP_A, first)
        store.put(IP_A, second)
        assert store.get(IP_A) == second
        assert len(store) == 1
        assert store.count(AddressFamily.IPV4) == 1
        assert [record for _, record in store.records()] == [second]

    def test_put_of_an_identical_record_is_idempotent(self) -> None:
        # A redelivered HotIpAdded (ADR-0012 decision 7: REPLACED with an
        # identical record) leaves the store observably unchanged.
        store = IpAttributeStore()
        record = HotIpRecord(attributes=WEIGHT_1000, window_count=1000)
        store.put(IP_A, record)
        before = (len(store), store.serialized_bytes, list(store.records()))
        store.put(IP_A, record)
        assert (len(store), store.serialized_bytes, list(store.records())) == before

    def test_delete_returns_true_then_false(self) -> None:
        store = IpAttributeStore()
        store.put(IP_A, _default_record())
        assert store.delete(IP_A) is True
        assert store.get(IP_A) is None
        assert IP_A not in store
        assert len(store) == 0
        assert store.delete(IP_A) is False

    def test_delete_leaves_other_records_alone(self) -> None:
        store = IpAttributeStore()
        keep = _default_record(1)
        store.put(IP_A, _default_record())
        store.put(IP_B, keep)
        assert store.delete(IP_A) is True
        assert store.get(IP_B) == keep
        assert len(store) == 1

    def test_a_record_can_be_re_added_after_deletion(self) -> None:
        # HOT -> COLD -> HOT: the third transition's record is the one stored.
        store = IpAttributeStore()
        store.put(IP_A, _default_record())
        store.delete(IP_A)
        again = HotIpRecord(attributes=WEIGHT_5, window_count=7)
        store.put(IP_A, again)
        assert store.get(IP_A) == again
        assert len(store) == 1


class TestIpAttributeStoreCountAndRecords:
    def test_count_is_per_family_and_len_is_the_sum(self) -> None:
        store = IpAttributeStore()
        for ip in (IP_A, IP_B, IP_V6):
            store.put(ip, _default_record())
        assert store.count(AddressFamily.IPV4) == 2
        assert store.count(AddressFamily.IPV6) == 1
        assert len(store) == 3

    def test_records_are_ascending_by_family_then_value(self) -> None:
        # Amendment 1 A3: every IPv4 record ascending by Address.value, then
        # every IPv6 record ascending -- the family order assumption 20 also
        # gives GET /prefixes/hot, and the order M6's snapshot writer emits.
        store = IpAttributeStore()
        # Inserted deliberately out of order; ::1 has the smallest integer
        # value of all but sorts after every IPv4 because family comes first.
        for ip in (IP_C, IP_V6, IP_B, IP_V6_LOW, IP_A):
            store.put(ip, _default_record(ip.value % 97))
        assert [ip for ip, _ in store.records()] == [IP_A, IP_B, IP_C, IP_V6_LOW, IP_V6]

    def test_records_yield_the_stored_record_for_each_address(self) -> None:
        store = IpAttributeStore()
        records = {
            IP_A: HotIpRecord(attributes=WEIGHT_1000, window_count=1),
            IP_B: HotIpRecord(attributes=WEIGHT_2500, window_count=2),
        }
        for ip, record in records.items():
            store.put(ip, record)
        assert dict(store.records()) == records

    def test_records_of_an_empty_store_is_empty(self) -> None:
        assert list(IpAttributeStore().records()) == []


class TestIpAttributeStoreSerializedBytes:
    """Section 46.8 `ip_attribute_bytes`: total serialized size of stored
    records, compact JSON."""

    def test_one_default_record_is_24_bytes(self) -> None:
        store = IpAttributeStore()
        store.put(IP_A, _default_record())
        assert store.serialized_bytes == 24

    def test_is_the_sum_over_records(self) -> None:
        store = IpAttributeStore()
        documents: list[Mapping[str, object]] = [
            DEFAULT_ATTRIBUTES,
            WEIGHT_1450,
            {"attributes_version": 1, "x_experiment": {"nested": [1, 2, 3]}},
        ]
        for ip, document in zip((IP_A, IP_B, IP_V6), documents, strict=True):
            store.put(ip, HotIpRecord(attributes=document, window_count=1000))
        assert store.serialized_bytes == sum(_compact_size(d) for d in documents)

    def test_tracks_replacement(self) -> None:
        store = IpAttributeStore()
        small: Mapping[str, object] = {"attributes_version": 1}
        large: Mapping[str, object] = {"attributes_version": 1, "x_blob": "a" * 100}
        store.put(IP_A, HotIpRecord(attributes=small, window_count=1000))
        store.put(IP_A, HotIpRecord(attributes=large, window_count=1000))
        assert store.serialized_bytes == _compact_size(large)
        store.put(IP_A, HotIpRecord(attributes=small, window_count=1000))
        assert store.serialized_bytes == _compact_size(small)

    def test_tracks_deletion(self) -> None:
        store = IpAttributeStore()
        store.put(IP_A, _default_record())
        store.put(IP_B, HotIpRecord(attributes=WEIGHT_5, window_count=5))
        store.delete(IP_A)
        assert store.serialized_bytes == _compact_size(WEIGHT_5)
        store.delete(IP_B)
        assert store.serialized_bytes == 0

    def test_a_no_op_delete_changes_nothing(self) -> None:
        store = IpAttributeStore()
        store.put(IP_A, _default_record())
        store.delete(IP_B)
        assert store.serialized_bytes == 24


class TestAttributesAreNeverInterpreted:
    """Section 46.2: `x_` keys and a document with a higher `attributes_version`
    MUST be stored and echoed verbatim; section 46.7: `GET /ip` returns the
    stored mapping. ADR-0012 decision 6: the store validates nothing."""

    def test_an_x_key_is_stored_and_returned_byte_identically(self) -> None:
        store = IpAttributeStore()
        document = {"attributes_version": 1, "x_experiment": {"any": ["json", 1, None, 2.5]}}
        store.put(IP_A, HotIpRecord(attributes=document, window_count=1000))
        stored = store.get(IP_A)
        assert stored is not None
        assert stored.attributes == document
        assert _compact(stored.attributes) == _compact(document)

    def test_a_higher_attributes_version_is_stored_and_returned_byte_identically(self) -> None:
        store = IpAttributeStore()
        document = {"attributes_version": 2, "severity": 3, "weight": 99_999_999}
        store.put(IP_A, HotIpRecord(attributes=document, window_count=1000))
        stored = store.get(IP_A)
        assert stored is not None
        assert stored.attributes == document
        assert _compact(stored.attributes) == _compact(document)

    def test_key_order_is_preserved(self) -> None:
        # Byte-identical echo means the key order the codec decoded is the
        # order handed back, not a re-sorted copy.
        store = IpAttributeStore()
        document = {"x_zeta": 1, "attributes_version": 1, "x_alpha": 2}
        store.put(IP_A, HotIpRecord(attributes=document, window_count=1000))
        stored = store.get(IP_A)
        assert stored is not None
        assert list(stored.attributes) == ["x_zeta", "attributes_version", "x_alpha"]
        assert _compact(stored.attributes) == b'{"x_zeta":1,"attributes_version":1,"x_alpha":2}'

    def test_the_store_does_not_validate(self) -> None:
        # The codec already enforced schemas/ip_attributes.v1.json; a document
        # the codec would never have produced is still just stored.
        store = IpAttributeStore()
        document = {"not_a_registered_name": True}
        store.put(IP_A, HotIpRecord(attributes=document, window_count=0))
        stored = store.get(IP_A)
        assert stored is not None
        assert stored.attributes == document

    def test_the_default_document_round_trips(self) -> None:
        store = IpAttributeStore()
        store.put(IP_A, _default_record(1000))
        stored = store.get(IP_A)
        assert stored is not None
        assert stored.attributes == {"attributes_version": 1}
        assert stored.window_count == 1000


# --- IpAttributeStore against a dict model (section 46.5's invariant shape) ---------

_ADDRESSES = st.sampled_from(
    [
        Address.parse("10.0.0.1"),
        Address.parse("10.0.0.2"),
        Address.parse("10.0.0.3"),
        Address.parse("192.0.2.9"),
        Address.parse("255.255.255.255"),
        Address.parse("::1"),
        Address.parse("2001:db8::1"),
        Address.parse("2001:db8::2"),
    ]
)
_DOCUMENTS: st.SearchStrategy[Mapping[str, object]] = st.sampled_from(
    [
        DEFAULT_ATTRIBUTES,
        WEIGHT_1000,
        {"attributes_version": 1, "weight": 2500, "x_note": "n"},
        {"attributes_version": 2, "severity": 3},
    ]
)
_RECORDS = st.builds(HotIpRecord, attributes=_DOCUMENTS, window_count=st.integers(0, 10**6))
_OPS = st.lists(
    st.one_of(
        st.tuples(st.just("put"), _ADDRESSES, _RECORDS),
        st.tuples(st.just("delete"), _ADDRESSES, st.none()),
    ),
    max_size=40,
)

# Amendment 1 A3's order: IPv4 first, then IPv6, each ascending by value.
_FAMILY_RANK = {AddressFamily.IPV4: 0, AddressFamily.IPV6: 1}


@given(ops=_OPS)
@settings(deadline=None, max_examples=150)
def test_store_matches_a_dict_model(ops: list[tuple[str, Address, HotIpRecord | None]]) -> None:
    """`put` is dict assignment, `delete` is dict pop; `len`, `count`,
    `records()` order, `serialized_bytes` and membership all follow."""

    store = IpAttributeStore()
    model: dict[Address, HotIpRecord] = {}
    for op, ip, record in ops:
        if op == "put":
            assert record is not None
            store.put(ip, record)
            model[ip] = record
        else:
            assert store.delete(ip) is (ip in model)
            model.pop(ip, None)

        assert len(store) == len(model)
        for family in AddressFamily:
            assert store.count(family) == sum(1 for a in model if a.family is family)
        assert sum(store.count(family) for family in AddressFamily) == len(store)
        for ip_in_model, expected in model.items():
            assert ip_in_model in store
            assert store.get(ip_in_model) == expected
        for candidate in (IP_A, IP_V6_LOW):
            if candidate not in model:
                assert candidate not in store
                assert store.get(candidate) is None
        expected_order = sorted(model, key=lambda a: (_FAMILY_RANK[a.family], a.value))
        assert [a for a, _ in store.records()] == expected_order
        assert dict(store.records()) == model
        assert store.serialized_bytes == sum(_compact_size(r.attributes) for r in model.values())
