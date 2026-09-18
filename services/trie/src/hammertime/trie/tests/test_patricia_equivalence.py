"""Patricia and binary tries agree on every query, differentially tested.

Spec: section 27 ("the logical model MUST remain equivalent to the binary
trie described above" whatever compression the representation uses),
section 10 and section 11 (the two mutations), section 12 (the counts being
compared), section 16 and section 17 (metadata stored where declared and
accumulated on lookup -- on both representations alike), section 35 (both
families).

ADR-0012 decision 3 states the contract these tests execute: after the same
sequence of `add_hot_ip`, `remove_hot_ip`, `set_local_metadata` and
`clear_local_metadata` calls on a `BinaryTrie` and a `PatriciaTrie` of the
same family, every one of

    is_hot(ip)            hot_count(prefix)       path_counts(ip)
    longest_match(ip)     list(hot_ips())         set(logical_prefixes(min_length=m))
    local_metadata(prefix)                        list(path_metadata(ip))
    hot_ip_count          the return value of every mutating call

is equal on both, for every `ip`, `prefix` and `m`. `node_count` and
`edges()` may differ -- that is the point of compression -- but
`set(logical_prefixes(edges(...)))` (the module-level expansion from
`hammertime.trie.structure.invariants`) must not, and must equal what each
trie's own `logical_prefixes()` reports.

The operation streams come from `hammertime.testkit.generators.hot_ip_streams`
(decision 4), so they share ancestors and bias removes onto HOT addresses;
metadata is set on random prefixes and on ancestors of the stream's own
addresses so that pins land on compressed edges.

Two checking depths, because "for every ip and prefix" is unbounded and an
IPv6 address has 129 ancestors:

* `assert_pointwise` compares the per-address and per-prefix queries over
  an explicit, bounded probe set. It runs after *every* operation.
* `assert_equivalent` adds every ancestor of every probe and the global
  logical view (`logical_prefixes` at each `m`, and the expansion of
  `edges()`). It runs at the boundaries -- before the first operation,
  after the last, and around each metadata change.

The logical view is the global statement: it ranges over every logical
prefix with a positive count in either trie, so nothing that differs
between the representations can hide between two probes.

ASSUMPTIONS -- details the ADR does not pin:

1. `Address`, `AddressFamily`, `Prefix` come from the
   `hammertime.core.addressing.address` / `.prefix` submodules.
2. `path_metadata` yields `(Prefix, Mapping)` pairs; the mappings are
   compared as `dict(...)` so that two different `Mapping` types holding
   the same items count as equal.
3. The `min_length` values compared are decision 3's `{0, 8, 24,
   bit_length}`.
4. `test_compressed_away_prefixes_answer_like_the_oracle` asserts
   `patricia.node_count < binary.node_count`. That rests on decision 2's
   "`BinaryTrie` is the oracle: one node per visited bit", not on the
   equivalence contract, which explicitly excludes `node_count`. It is
   here so that "the Patricia trie compresses" is asserted somewhere
   rather than assumed.
"""

from collections.abc import Iterable

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.testkit.generators import HotIpOp, addresses, hot_ip_streams, prefixes
from hammertime.testkit.invariants import ancestor_of, root_prefix
from hammertime.trie.structure.binary_trie import BinaryTrie
from hammertime.trie.structure.invariants import logical_prefixes
from hammertime.trie.structure.patricia import PatriciaTrie
from hypothesis import given, settings
from hypothesis import strategies as st

FAMILIES = (AddressFamily.IPV4, AddressFamily.IPV6)
NAMES = ("policy", "tags", "mask")

V6_DOC = 0x2001_0DB8 << 96
SECTION_42_NETWORK = 0x0A14_1E00

MAX_EXAMPLES = 50
MAX_STREAM = 24

Trie = BinaryTrie | PatriciaTrie
MetadataOp = tuple[Prefix, str, int]
MetadataPath = list[tuple[Prefix, dict[str, object]]]


def _ip(family: AddressFamily, low: int) -> Address:
    value = low if family is AddressFamily.IPV4 else V6_DOC | low
    return Address(family=family, value=value)


def _min_lengths(family: AddressFamily) -> tuple[int, ...]:
    """Decision 3's `m` values (assumption 3)."""

    return tuple(sorted({0, 8, 24, family.bit_length}))


def _probe_depths(family: AddressFamily) -> tuple[int, ...]:
    """Depths a bounded per-step check looks at: the reported minimums, the
    branch points the generators cluster around, and the host route."""

    bits = family.bit_length
    candidates = {0, 1, 8, 24, bits - 24, bits - 10, bits - 9, bits - 8, bits - 2, bits - 1, bits}
    return tuple(sorted(length for length in candidates if 0 <= length <= bits))


def _ancestors(ips: Iterable[Address]) -> set[Prefix]:
    return {ancestor_of(ip, length) for ip in ips for length in range(ip.bit_length + 1)}


def _probe_prefixes(family: AddressFamily, ips: Iterable[Address]) -> set[Prefix]:
    depths = _probe_depths(family)
    return {ancestor_of(ip, length) for ip in ips for length in depths}


def _metadata_path(trie: Trie, ip: Address) -> MetadataPath:
    return [(prefix, dict(metadata)) for prefix, metadata in trie.path_metadata(ip)]


def assert_pointwise(
    binary: BinaryTrie,
    patricia: PatriciaTrie,
    ips: Iterable[Address],
    prefixes_to_check: Iterable[Prefix],
) -> None:
    """Decision 3's per-address and per-prefix queries over a bounded set."""

    assert binary.family is patricia.family
    assert binary.hot_ip_count == patricia.hot_ip_count
    assert list(binary.hot_ips()) == list(patricia.hot_ips())

    for ip in ips:
        assert binary.is_hot(ip) == patricia.is_hot(ip), ip
        assert binary.path_counts(ip) == patricia.path_counts(ip), ip
        assert binary.longest_match(ip) == patricia.longest_match(ip), ip
        assert _metadata_path(binary, ip) == _metadata_path(patricia, ip), ip

    for prefix in prefixes_to_check:
        assert binary.hot_count(prefix) == patricia.hot_count(prefix), prefix
        binary_local = dict(binary.local_metadata(prefix))
        assert binary_local == dict(patricia.local_metadata(prefix)), prefix


def assert_logical_view(binary: BinaryTrie, patricia: PatriciaTrie) -> None:
    """The global half of decision 3: every logical prefix with a positive
    count, at each `m`, and the expansion of `edges()` agreeing with it."""

    for min_length in _min_lengths(binary.family):
        binary_logical = list(binary.logical_prefixes(min_length=min_length))
        patricia_logical = list(patricia.logical_prefixes(min_length=min_length))
        # Reported once each, and the same set on both.
        assert len(set(binary_logical)) == len(binary_logical)
        assert len(set(patricia_logical)) == len(patricia_logical)
        assert set(binary_logical) == set(patricia_logical), min_length
        # The module-level expansion of edges() says the same.
        from_binary_edges = set(logical_prefixes(binary.edges(), min_length=min_length))
        from_patricia_edges = set(logical_prefixes(patricia.edges(), min_length=min_length))
        assert from_patricia_edges == set(patricia_logical), min_length
        assert from_binary_edges == set(binary_logical), min_length


def assert_equivalent(
    binary: BinaryTrie,
    patricia: PatriciaTrie,
    ips: Iterable[Address],
    extra_prefixes: Iterable[Prefix] = (),
) -> None:
    """The whole contract over `ips`, every ancestor of every one of them,
    `extra_prefixes`, and the global logical view."""

    ips = list(ips)
    covered = _ancestors(ips) | set(extra_prefixes) | {root_prefix(binary.family)}
    assert_pointwise(binary, patricia, ips, covered)
    assert_logical_view(binary, patricia)


def _apply(binary: BinaryTrie, patricia: PatriciaTrie, op: HotIpOp) -> None:
    kind, ip = op
    if kind == "add":
        assert binary.add_hot_ip(ip) == patricia.add_hot_ip(ip), op
    else:
        assert binary.remove_hot_ip(ip) == patricia.remove_hot_ip(ip), op


def _metadata_ops(
    family: AddressFamily, stream: list[HotIpOp]
) -> st.SearchStrategy[list[MetadataOp]]:
    """Metadata targets: random prefixes plus ancestors of the stream's own
    addresses, so pins fall on paths that are also being added/removed."""

    bits = family.bit_length
    depths = (0, bits - 24, bits - 8, bits - 1, bits)
    on_path: list[Prefix] = sorted(
        {ancestor_of(ip, length) for _, ip in stream for length in depths},
        key=lambda p: (p.length, p.network),
    )
    targets = prefixes(family)
    if on_path:
        targets = st.one_of(targets, st.sampled_from(on_path))
    return st.lists(st.tuples(targets, st.sampled_from(NAMES), st.integers()), max_size=6)


@pytest.mark.parametrize("family", FAMILIES, ids=["ipv4", "ipv6"])
@settings(deadline=None, max_examples=MAX_EXAMPLES)
@given(data=st.data())
def test_every_query_agrees_after_every_operation(
    family: AddressFamily, data: st.DataObject
) -> None:
    """Decision 3: the streams of decision 4 leave both tries answering
    identically after each step, with metadata declared before, during and
    cleared after."""

    stream = data.draw(hot_ip_streams(family, max_size=MAX_STREAM), label="stream")
    metadata = data.draw(_metadata_ops(family, stream), label="metadata")
    drawn = data.draw(st.lists(addresses(family), max_size=3), label="probes")
    extra = data.draw(st.lists(prefixes(family), max_size=6), label="prefixes")

    binary = BinaryTrie(family)
    patricia = PatriciaTrie(family)
    probes: list[Address] = list(dict.fromkeys([ip for _, ip in stream] + drawn))[:8]
    watched = _probe_prefixes(family, probes) | set(extra) | {root_prefix(family)}

    assert_equivalent(binary, patricia, probes, extra)

    # Metadata declared up front (before anything is HOT) and half-way.
    midpoint = len(metadata) // 2
    for prefix, name, value in metadata[:midpoint]:
        binary.set_local_metadata(prefix, name, value)
        patricia.set_local_metadata(prefix, name, value)
        assert_equivalent(binary, patricia, probes, extra)

    for index, op in enumerate(stream):
        _apply(binary, patricia, op)
        assert_pointwise(binary, patricia, probes, watched)
        if index == len(stream) // 2:
            for prefix, name, value in metadata[midpoint:]:
                binary.set_local_metadata(prefix, name, value)
                patricia.set_local_metadata(prefix, name, value)
                assert_equivalent(binary, patricia, probes, extra)

    assert_equivalent(binary, patricia, probes, extra)

    for prefix, name, _ in metadata:
        binary.clear_local_metadata(prefix, name)
        patricia.clear_local_metadata(prefix, name)
        assert_equivalent(binary, patricia, probes, extra)

    # Draining both leaves both empty.
    for ip in list(binary.hot_ips()):
        _apply(binary, patricia, ("remove", ip))
    assert_equivalent(binary, patricia, probes, extra)
    assert binary.hot_ip_count == patricia.hot_ip_count == 0


@pytest.mark.parametrize("family", FAMILIES, ids=["ipv4", "ipv6"])
@settings(deadline=None, max_examples=MAX_EXAMPLES)
@given(data=st.data())
def test_mutating_return_values_agree_on_redundant_operations(
    family: AddressFamily, data: st.DataObject
) -> None:
    """Every add is repeated and every remove is repeated: the second call's
    `False` must come from both tries (section 11's no-op, decision 2)."""

    stream = data.draw(hot_ip_streams(family, max_size=MAX_STREAM), label="stream")
    binary = BinaryTrie(family)
    patricia = PatriciaTrie(family)
    for kind, ip in stream:
        _apply(binary, patricia, (kind, ip))
        _apply(binary, patricia, (kind, ip))
        if kind == "add":
            assert binary.add_hot_ip(ip) is False
            assert patricia.add_hot_ip(ip) is False
        else:
            assert binary.remove_hot_ip(ip) is False
            assert patricia.remove_hot_ip(ip) is False
    probes = list(dict.fromkeys(ip for _, ip in stream))[:8]
    assert_equivalent(binary, patricia, probes)


@pytest.mark.parametrize("family", FAMILIES, ids=["ipv4", "ipv6"])
def test_compressed_away_prefixes_answer_like_the_oracle(family: AddressFamily) -> None:
    """Decision 3's worked example: 10.20.30.1 and 10.20.31.1 branch at the
    /23; the /22 above it is one compressed edge on the Patricia trie and a
    real node on the oracle, and both say 2."""

    bits = family.bit_length
    first = _ip(family, SECTION_42_NETWORK | 1)
    second = _ip(family, 0x0A14_1F01)
    binary = BinaryTrie(family)
    patricia = PatriciaTrie(family)
    for ip in (first, second):
        _apply(binary, patricia, ("add", ip))
    for length in (bits - 10, bits - 9, bits - 8, bits - 1, bits, 0, 1, 8):
        prefix = ancestor_of(first, length)
        assert binary.hot_count(prefix) == patricia.hot_count(prefix)
    assert patricia.hot_count(ancestor_of(first, bits - 10)) == 2
    assert patricia.hot_count(ancestor_of(first, bits - 9)) == 2
    assert patricia.hot_count(ancestor_of(first, bits - 8)) == 1
    assert_equivalent(binary, patricia, [first, second, _ip(family, 0x0A14_1CFF)])
    # Compression is visible in the representation (assumption 4) ...
    assert patricia.node_count < binary.node_count
    # ... and invisible in the logical view.
    assert set(logical_prefixes(patricia.edges())) == set(logical_prefixes(binary.edges()))


@pytest.mark.parametrize("family", FAMILIES, ids=["ipv4", "ipv6"])
def test_section_42_agrees_at_every_step(family: AddressFamily) -> None:
    """Section 42's 156 addresses go HOT one by one, then 141 go COLD."""

    bits = family.bit_length
    ips = [_ip(family, SECTION_42_NETWORK | host) for host in range(1, 157)]
    binary = BinaryTrie(family)
    patricia = PatriciaTrie(family)
    probes = [ips[0], ips[77], ips[155], _ip(family, SECTION_42_NETWORK), _ip(family, 0x0A14_1F01)]
    watched = _probe_prefixes(family, probes)
    for ip in ips:
        _apply(binary, patricia, ("add", ip))
        assert_pointwise(binary, patricia, probes, watched)
    assert_equivalent(binary, patricia, probes)
    assert patricia.hot_count(ancestor_of(ips[0], bits - 8)) == 156

    for ip in ips[15:]:
        _apply(binary, patricia, ("remove", ip))
        assert_pointwise(binary, patricia, probes, watched)
    assert_equivalent(binary, patricia, probes)
    assert patricia.hot_count(ancestor_of(ips[0], bits - 8)) == 15


@pytest.mark.parametrize("family", FAMILIES, ids=["ipv4", "ipv6"])
def test_metadata_on_a_compressed_edge_agrees(family: AddressFamily) -> None:
    """Section 17's example -- `10.0.0.0/8 -> {"internal"}` -- declared on a
    prefix that, on the Patricia trie, sits in the middle of a compressed
    edge; lookups below it accumulate it on both representations."""

    bits = family.bit_length
    ip = _ip(family, SECTION_42_NETWORK | 1)
    slash_8 = ancestor_of(ip, bits - 24)
    binary = BinaryTrie(family)
    patricia = PatriciaTrie(family)
    _apply(binary, patricia, ("add", ip))
    for trie in (binary, patricia):
        trie.set_local_metadata(slash_8, "tags", frozenset({"internal"}))
    probes = [ip, _ip(family, 0x0A00_0001), _ip(family, 0x0B00_0001), _ip(family, 0xC0A8_012A)]
    assert_equivalent(binary, patricia, probes, [slash_8])
    assert _metadata_path(patricia, probes[1]) == [(slash_8, {"tags": frozenset({"internal"})})]
    assert _metadata_path(patricia, probes[2]) == []
    _apply(binary, patricia, ("remove", ip))
    assert_equivalent(binary, patricia, probes, [slash_8])
    for trie in (binary, patricia):
        trie.clear_local_metadata(slash_8, "tags")
    assert_equivalent(binary, patricia, probes, [slash_8])
    assert _metadata_path(patricia, probes[1]) == []
