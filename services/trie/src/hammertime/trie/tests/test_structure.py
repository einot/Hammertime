"""The trie's public surface, on both representations and both families.

Spec: section 8 (a path is the bits of an address; a /24 is the first 24
bits and an IP is /32; the trie MUST support insert, remove, ancestor
counts, prefix statistics and the longest matching prefix), section 9 (the
node: two children, `hot_count`, `local_metadata`), section 10 (insertion
adds one at every node on the path), section 11 (removal subtracts one; a
removal of something not held changes nothing; empty nodes may be pruned;
an arena is the recommended way to survive oscillation), section 12
(`hot_count(node)` is the number of HOT /32s beneath it; `hot_count(/32) in
{0, 1}`), section 16 and section 17 (metadata is stored on the node where
it is declared and accumulated along the path on lookup, never copied
down), section 27 (the logical model MUST stay the binary trie whatever the
representation), section 35 (IPv6: separate roots, same operations),
section 39 (the `add_hot_ip` / `remove_hot_ip` pseudocode), section 42 (the
156 addresses of `10.20.30.0/24`).

ADR-0012 decision 2 pins the surface these tests call -- `TrieNode`,
`NodeId`, `NO_NODE`, `NodeArena`, and the API `BinaryTrie` and
`PatriciaTrie` share (`add_hot_ip`, `remove_hot_ip`, `is_hot`,
`hot_count`, `path_counts`, `longest_match`, `hot_ips`, `logical_prefixes`,
`edges`, `set_local_metadata`, `clear_local_metadata`, `local_metadata`,
`path_metadata`, `hot_ip_count`, `node_count`), together with the Patricia
shape rules (root always materialized, a leaf is the `/bit_length` node,
every other node has two children or is pinned, pruning is immediate).
Decision 3 defines the logical prefixes a compressed edge stands for.
Decision 5 defines pinning: metadata materializes its prefix and keeps the
node alive whatever its `hot_count`; clearing the last name un-pins it.

Amendment 1 (2026-09-18) settled four things this file asserts, each of
which was an open assumption when it was first written: `edges()` visits
`child0` before `child1` (A7), so the order is fully determined and is
asserted as such; `logical_prefixes()` yields each prefix exactly once
(A8); `NodeArena.allocate` reuses the **most recently** freed slot (A9),
so the reuse test names the id rather than comparing sets; a reused slot
comes back reset (A10); and both metadata setters -- `clear_local_metadata`
included -- validate the name (A2).

Every behavioural test runs against both tries and both families; only
`TestPatriciaShape` (decision 2's representation rules, which the oracle is
free to ignore) and the arena/node tests are representation-specific.

ASSUMPTIONS -- details the ADR does not pin:

1. `Address`, `AddressFamily` and `Prefix` are imported from the
   `hammertime.core.addressing.address` / `.prefix` submodules, the paths the
   core package's own tests and every other service test use.
2. `Address` is hashable and `Prefix` is hashable (the ADR's own
   `set(hot_ips())` and `set(logical_prefixes())` require both).

Two former assumptions are now pinned by the ADR rather than assumed here:
the IPv6 analogue of every IPv4 example (`a.b.c.d/L` ->
`2001:db8::a.b.c.d/(L + 96)`, so section 42's /24 is a /120) is decision 1
as amended by A11, and it is what `_ip` below builds; and metadata-name
validation against `METADATA_NAME` (`^[a-z][a-z0-9_]{0,63}$`), raising
`ValueError` from both setters before any structural change, is A2. No
assertion here compares IPv6 prefix *text*: prefixes are built from
integers and compared as `Prefix` objects, so A11's caveat that CPython
canonicalises `2001:db8::10.20.30.0` to `2001:db8::a14:1e00` does not bite.
"""

from collections.abc import Callable, Iterable
from itertools import pairwise

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.testkit.invariants import ancestor_of, covers, expected_hot_counts, root_prefix
from hammertime.trie.structure.arena import NodeArena
from hammertime.trie.structure.binary_trie import BinaryTrie
from hammertime.trie.structure.node import NO_NODE, TrieNode
from hammertime.trie.structure.patricia import PatriciaTrie

Trie = BinaryTrie | PatriciaTrie

TRIE_CLASSES: tuple[type[BinaryTrie] | type[PatriciaTrie], ...] = (BinaryTrie, PatriciaTrie)
FAMILIES = (AddressFamily.IPV4, AddressFamily.IPV6)

# 2001:db8::/32 is the documentation prefix; IPv6 examples live in its /96.
V6_DOC = 0x2001_0DB8 << 96

# Section 42: 10.20.30.1 .. 10.20.30.156 become HOT.
SECTION_42_NETWORK = 0x0A14_1E00
SECTION_42_HOSTS = range(1, 157)
SECTION_42_HOT_COUNT = 156
# Section 42: "If those IPs subsequently fall below the cold threshold ...
# hot_count -> 15".
SECTION_42_COOLED_COUNT = 15

# Decision 5's METADATA_NAME: ^[a-z][a-z0-9_]{0,63}$
VALID_NAMES = ("policy", "x_1", "a", "a" * 64)
INVALID_NAMES = ("Bad Name", "", "9lives", "Policy", "_x", "a" * 65, "policy-name")


def _ip(family: AddressFamily, low: int) -> Address:
    """`low` are the last 32 bits: the IPv4 address itself, or the same bits
    under 2001:db8::/96 for IPv6 -- decision 1's mapping, pinned by A11."""

    value = low if family is AddressFamily.IPV4 else V6_DOC | low
    return Address(family=family, value=value)


def _other_family(family: AddressFamily) -> AddressFamily:
    return AddressFamily.IPV6 if family is AddressFamily.IPV4 else AddressFamily.IPV4


def _section_42(family: AddressFamily) -> list[Address]:
    return [_ip(family, SECTION_42_NETWORK | host) for host in SECTION_42_HOSTS]


def _add_all(trie: Trie, ips: Iterable[Address]) -> None:
    for ip in ips:
        assert trie.add_hot_ip(ip) is True


def _remove_all(trie: Trie, ips: Iterable[Address]) -> None:
    for ip in ips:
        assert trie.remove_hot_ip(ip) is True


def _values(ips: Iterable[Address]) -> list[int]:
    return [ip.value for ip in ips]


@pytest.fixture(params=TRIE_CLASSES, ids=["binary", "patricia"])
def trie_class(request: pytest.FixtureRequest) -> type[BinaryTrie] | type[PatriciaTrie]:
    param: type[BinaryTrie] | type[PatriciaTrie] = request.param
    return param


@pytest.fixture(params=FAMILIES, ids=["ipv4", "ipv6"])
def family(request: pytest.FixtureRequest) -> AddressFamily:
    param: AddressFamily = request.param
    return param


@pytest.fixture
def trie(trie_class: type[BinaryTrie] | type[PatriciaTrie], family: AddressFamily) -> Trie:
    return trie_class(family)


@pytest.fixture
def patricia(family: AddressFamily) -> PatriciaTrie:
    return PatriciaTrie(family)


class TestHotIpMembership:
    """Sections 10, 11, 39: add and remove, and what they return."""

    def test_family_is_the_one_it_was_built_for(self, trie: Trie, family: AddressFamily) -> None:
        assert trie.family is family

    def test_add_returns_true_the_first_time_and_false_the_second(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        ip = _ip(family, 0xC0A8_012A)  # section 10's 192.168.1.42
        assert trie.add_hot_ip(ip) is True
        assert trie.add_hot_ip(ip) is False

    def test_a_second_add_changes_nothing(self, trie: Trie, family: AddressFamily) -> None:
        ip = _ip(family, 0xC0A8_012A)
        trie.add_hot_ip(ip)
        before = trie.path_counts(ip)
        trie.add_hot_ip(ip)
        assert trie.path_counts(ip) == before
        assert trie.hot_ip_count == 1
        assert list(trie.hot_ips()) == [ip]

    def test_remove_returns_true_then_false(self, trie: Trie, family: AddressFamily) -> None:
        ip = _ip(family, 0xC0A8_012A)
        trie.add_hot_ip(ip)
        assert trie.remove_hot_ip(ip) is True
        assert trie.remove_hot_ip(ip) is False

    def test_removing_an_address_never_added_is_a_no_op(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        # Section 11: hot_count >= 0 MUST remain an invariant; the ADR's
        # reading is that the removal changes nothing at all.
        held = _ip(family, 0xC0A8_0101)
        stranger = _ip(family, 0xC0A8_0102)
        trie.add_hot_ip(held)
        before_held = trie.path_counts(held)
        before_stranger = trie.path_counts(stranger)
        assert trie.remove_hot_ip(stranger) is False
        assert trie.path_counts(held) == before_held
        assert trie.path_counts(stranger) == before_stranger
        assert trie.hot_ip_count == 1
        assert trie.hot_count(root_prefix(family)) == 1

    def test_is_hot_tracks_add_and_remove(self, trie: Trie, family: AddressFamily) -> None:
        ip = _ip(family, 0xC0A8_012A)
        assert trie.is_hot(ip) is False
        trie.add_hot_ip(ip)
        assert trie.is_hot(ip) is True
        trie.remove_hot_ip(ip)
        assert trie.is_hot(ip) is False

    def test_is_hot_is_exact_to_the_address(self, trie: Trie, family: AddressFamily) -> None:
        trie.add_hot_ip(_ip(family, 0xC0A8_012A))
        assert trie.is_hot(_ip(family, 0xC0A8_012B)) is False
        assert trie.is_hot(_ip(family, 0xC0A8_0129)) is False

    def test_hot_ip_count_is_the_root_count(self, trie: Trie, family: AddressFamily) -> None:
        assert trie.hot_ip_count == 0 == trie.hot_count(root_prefix(family))
        ips = _section_42(family)[:10]
        _add_all(trie, ips)
        assert trie.hot_ip_count == 10 == trie.hot_count(root_prefix(family))
        _remove_all(trie, ips[:4])
        assert trie.hot_ip_count == 6 == trie.hot_count(root_prefix(family))

    def test_add_and_remove_are_a_round_trip(self, trie: Trie, family: AddressFamily) -> None:
        ips = _section_42(family)
        _add_all(trie, ips)
        _remove_all(trie, ips)
        assert trie.hot_ip_count == 0
        assert list(trie.hot_ips()) == []
        for ip in ips:
            assert trie.is_hot(ip) is False
            assert trie.path_counts(ip) == (0,) * (family.bit_length + 1)


class TestHotCountsAlongThePath:
    """Section 10 and section 12 over section 42's scenario."""

    def test_section_42_every_ancestor_counts_156(self, trie: Trie, family: AddressFamily) -> None:
        bits = family.bit_length
        ips = _section_42(family)
        _add_all(trie, ips)
        first = ips[0]
        for length in (bits - 8, bits - 16, bits - 24, 0):
            assert trie.hot_count(ancestor_of(first, length)) == SECTION_42_HOT_COUNT, length

    def test_section_42_each_host_route_is_one(self, trie: Trie, family: AddressFamily) -> None:
        ips = _section_42(family)
        _add_all(trie, ips)
        for ip in ips:
            assert trie.hot_count(ancestor_of(ip, family.bit_length)) == 1

    def test_a_prefix_with_no_node_counts_zero(self, trie: Trie, family: AddressFamily) -> None:
        bits = family.bit_length
        _add_all(trie, _section_42(family))
        sibling_24 = ancestor_of(_ip(family, 0x0A14_1F00), bits - 8)  # 10.20.31.0/24
        assert trie.hot_count(sibling_24) == 0
        unused_host = ancestor_of(_ip(family, SECTION_42_NETWORK | 200), bits)  # 10.20.30.200
        assert trie.hot_count(unused_host) == 0
        elsewhere = ancestor_of(_ip(family, 0xC0A8_0000), bits - 16)  # 192.168.0.0/16
        assert trie.hot_count(elsewhere) == 0

    def test_section_42_cooling_to_15_needs_no_rescan(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        ips = _section_42(family)
        _add_all(trie, ips)
        _remove_all(trie, ips[SECTION_42_COOLED_COUNT:])
        slash_24 = ancestor_of(ips[0], bits - 8)
        assert trie.hot_count(slash_24) == SECTION_42_COOLED_COUNT
        assert trie.hot_count(root_prefix(family)) == SECTION_42_COOLED_COUNT
        assert trie.hot_ip_count == SECTION_42_COOLED_COUNT

    def test_every_logical_prefix_counts_its_hot_descendants(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        # Section 12's definition, checked for every ancestor of a few
        # addresses inside and outside the hot set.
        ips = [*_section_42(family)[:20], _ip(family, 0xC0A8_012A), _ip(family, 0x0A14_1F01)]
        _add_all(trie, ips)
        expected = expected_hot_counts(ips)
        probes = [*ips, _ip(family, 0x0A14_1EFF), _ip(family, 0x0B00_0001)]
        for ip in probes:
            for length in range(family.bit_length + 1):
                prefix = ancestor_of(ip, length)
                assert trie.hot_count(prefix) == expected.get(prefix, 0), prefix


class TestPathCounts:
    """`path_counts(ip)`: the whole path's `hot_count`s in one call (decision 2)."""

    def test_has_bit_length_plus_one_entries(self, trie: Trie, family: AddressFamily) -> None:
        ip = _ip(family, 0xC0A8_012A)
        assert len(trie.path_counts(ip)) == family.bit_length + 1
        trie.add_hot_ip(ip)
        assert len(trie.path_counts(ip)) == family.bit_length + 1

    def test_all_zero_on_an_empty_trie(self, trie: Trie, family: AddressFamily) -> None:
        assert trie.path_counts(_ip(family, 0xC0A8_012A)) == (0,) * (family.bit_length + 1)

    def test_entry_l_is_the_count_of_the_l_ancestor(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        ips = [*_section_42(family)[:12], _ip(family, 0x0A14_1F01), _ip(family, 0xC0A8_012A)]
        _add_all(trie, ips)
        for ip in [*ips, _ip(family, 0x0A14_1EFE), _ip(family, 0x7F00_0001)]:
            counts = trie.path_counts(ip)
            for length in range(family.bit_length + 1):
                assert counts[length] == trie.hot_count(ancestor_of(ip, length)), (ip, length)

    def test_last_entry_is_one_iff_hot(self, trie: Trie, family: AddressFamily) -> None:
        hot = _ip(family, 0xC0A8_012A)
        cold = _ip(family, 0xC0A8_012B)
        trie.add_hot_ip(hot)
        assert trie.path_counts(hot)[-1] == 1
        assert trie.path_counts(cold)[-1] == 0
        assert trie.path_counts(hot)[0] == trie.path_counts(cold)[0] == 1

    def test_path_counts_are_non_increasing_along_the_path(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        # A descendant's subtree is contained in its ancestor's (section 12).
        _add_all(trie, _section_42(family))
        counts = trie.path_counts(_ip(family, SECTION_42_NETWORK | 1))
        assert all(a >= b for a, b in pairwise(counts))
        assert counts[0] == SECTION_42_HOT_COUNT


class TestLongestMatch:
    """Section 8: the longest matching prefix -- the deepest prefix containing
    the address with `hot_count > 0`; `None` iff the trie holds nothing."""

    def test_none_on_an_empty_trie(self, trie: Trie, family: AddressFamily) -> None:
        assert trie.longest_match(_ip(family, 0xC0A8_012A)) is None

    def test_a_hot_address_matches_its_own_host_route(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        ip = _ip(family, 0xC0A8_012A)
        trie.add_hot_ip(ip)
        assert trie.longest_match(ip) == ancestor_of(ip, family.bit_length)

    def test_a_neighbour_matches_the_deepest_shared_ancestor(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        # ...00000001 and ...00000010 share everything but their last two
        # bits, so the deepest prefix with a positive count containing .2 is
        # the /30 (IPv6: /126) above them both.
        trie.add_hot_ip(_ip(family, SECTION_42_NETWORK | 1))
        neighbour = _ip(family, SECTION_42_NETWORK | 2)
        assert trie.longest_match(neighbour) == ancestor_of(neighbour, family.bit_length - 2)

    def test_an_unrelated_address_matches_only_the_root(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        # The root has hot_count > 0 as soon as anything is HOT and contains
        # every address; an address differing in the first bit shares no
        # longer prefix with the hot one.
        bits = family.bit_length
        hot = _ip(family, SECTION_42_NETWORK | 1)
        trie.add_hot_ip(hot)
        far = Address(family=family, value=(hot.value ^ (1 << (bits - 1))))
        assert trie.longest_match(far) == root_prefix(family)

    def test_is_the_deepest_prefix_whose_path_count_is_positive(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        ips = [*_section_42(family)[:8], _ip(family, 0xC0A8_012A), _ip(family, 0x0A14_1F01)]
        _add_all(trie, ips)
        strangers = [_ip(family, 0x0A14_1EFF), _ip(family, 0xC0A8_0000), _ip(family, 0x7F00_0001)]
        for ip in [*ips, *strangers]:
            counts = trie.path_counts(ip)
            deepest = max(length for length in range(bits + 1) if counts[length] > 0)
            assert trie.longest_match(ip) == ancestor_of(ip, deepest), ip

    def test_none_again_once_every_address_is_removed(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        ips = _section_42(family)[:5]
        _add_all(trie, ips)
        _remove_all(trie, ips)
        assert trie.longest_match(ips[0]) is None

    def test_metadata_alone_is_not_a_match(self, trie: Trie, family: AddressFamily) -> None:
        # `None iff hot_ip_count == 0`: a pinned node with hot_count 0 is not
        # a prefix with hot_count > 0.
        ip = _ip(family, SECTION_42_NETWORK | 1)
        trie.set_local_metadata(ancestor_of(ip, family.bit_length - 8), "policy", "internal")
        assert trie.longest_match(ip) is None


class TestHotIps:
    """`hot_ips()`: every HOT address, ascending by value."""

    def test_empty_when_nothing_is_hot(self, trie: Trie) -> None:
        assert list(trie.hot_ips()) == []

    def test_ascending_whatever_the_insertion_order(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        ips = [
            _ip(family, 0xC0A8_012A),
            _ip(family, SECTION_42_NETWORK | 156),
            _ip(family, SECTION_42_NETWORK | 1),
            _ip(family, 0x0A14_1F01),
            _ip(family, 0x7F00_0001),
            _ip(family, SECTION_42_NETWORK | 77),
        ]
        _add_all(trie, ips)
        listed = list(trie.hot_ips())
        assert _values(listed) == sorted(_values(ips))
        assert set(listed) == set(ips)

    def test_reflects_removal(self, trie: Trie, family: AddressFamily) -> None:
        ips = _section_42(family)
        _add_all(trie, ips)
        _remove_all(trie, ips[::2])
        assert list(trie.hot_ips()) == ips[1::2]

    def test_each_listed_address_is_hot(self, trie: Trie, family: AddressFamily) -> None:
        _add_all(trie, _section_42(family)[:30])
        for ip in trie.hot_ips():
            assert trie.is_hot(ip)
            assert ip.family is family


class TestLogicalPrefixes:
    """Section 27 / decision 3: every logical prefix with `hot_count > 0`,
    materialized or not, is reported exactly once with its count."""

    @staticmethod
    def _two_siblings(family: AddressFamily) -> tuple[Address, Address]:
        # 10.20.30.1 and 10.20.31.1: they branch at the /23 above them; the
        # /22 above that has one logical child and, on a Patricia trie, no
        # node of its own -- yet both carry count 2.
        return _ip(family, SECTION_42_NETWORK | 1), _ip(family, 0x0A14_1F01)

    def test_empty_trie_reports_nothing(self, trie: Trie) -> None:
        assert list(trie.logical_prefixes()) == []

    def test_reports_exactly_the_prefixes_with_a_positive_count(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        ips = self._two_siblings(family)
        _add_all(trie, ips)
        listed = list(trie.logical_prefixes())
        assert len(listed) == len(set(listed))
        assert set(listed) == set(expected_hot_counts(ips).items())

    def test_compressed_away_prefixes_carry_the_count_beneath_them(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        first, second = self._two_siblings(family)
        _add_all(trie, (first, second))
        reported = set(trie.logical_prefixes())
        branch_23 = ancestor_of(first, bits - 9)
        skipped_22 = ancestor_of(first, bits - 10)
        assert branch_23 == ancestor_of(second, bits - 9)
        assert (branch_23, 2) in reported
        assert (skipped_22, 2) in reported
        assert (ancestor_of(first, bits - 8), 1) in reported
        assert (ancestor_of(second, bits - 8), 1) in reported
        assert (root_prefix(family), 2) in reported

    @pytest.mark.parametrize("offset", [0, 8, -10, -9, -8, -1, None], ids=str)
    def test_min_length_keeps_exactly_the_prefixes_at_least_that_long(
        self, trie: Trie, family: AddressFamily, offset: int | None
    ) -> None:
        bits = family.bit_length
        min_length = bits if offset is None else (offset if offset >= 0 else bits + offset)
        ips = [*self._two_siblings(family), _ip(family, 0xC0A8_012A)]
        _add_all(trie, ips)
        want = {(p, c) for p, c in expected_hot_counts(ips).items() if p.length >= min_length}
        assert set(trie.logical_prefixes(min_length=min_length)) == want

    def test_counts_follow_removal(self, trie: Trie, family: AddressFamily) -> None:
        ips = _section_42(family)
        _add_all(trie, ips)
        _remove_all(trie, ips[SECTION_42_COOLED_COUNT:])
        remaining = ips[:SECTION_42_COOLED_COUNT]
        assert set(trie.logical_prefixes()) == set(expected_hot_counts(remaining).items())

    def test_a_pinned_node_without_hot_descendants_is_not_reported(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        trie.set_local_metadata(prefix, "policy", "internal")
        assert list(trie.logical_prefixes()) == []
        trie.add_hot_ip(_ip(family, 0xC0A8_012A))
        assert all(p != prefix for p, _ in trie.logical_prefixes())


class TestWrongFamily:
    """Decision 2: a trie is built for one family and raises `ValueError`
    for an `Address`/`Prefix` of the other (section 35: separate roots)."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda trie, ip, prefix: trie.add_hot_ip(ip),
            lambda trie, ip, prefix: trie.remove_hot_ip(ip),
            lambda trie, ip, prefix: trie.is_hot(ip),
            lambda trie, ip, prefix: trie.hot_count(prefix),
            lambda trie, ip, prefix: trie.path_counts(ip),
            lambda trie, ip, prefix: trie.longest_match(ip),
            lambda trie, ip, prefix: trie.set_local_metadata(prefix, "policy", "internal"),
            lambda trie, ip, prefix: trie.clear_local_metadata(prefix, "policy"),
            lambda trie, ip, prefix: trie.local_metadata(prefix),
            lambda trie, ip, prefix: list(trie.path_metadata(ip)),
        ],
        ids=[
            "add_hot_ip",
            "remove_hot_ip",
            "is_hot",
            "hot_count",
            "path_counts",
            "longest_match",
            "set_local_metadata",
            "clear_local_metadata",
            "local_metadata",
            "path_metadata",
        ],
    )
    def test_raises_value_error(
        self,
        trie: Trie,
        family: AddressFamily,
        call: Callable[[Trie, Address, Prefix], object],
    ) -> None:
        other = _other_family(family)
        foreign_ip = Address(family=other, value=1)
        foreign_prefix = Prefix(family=other, network=0, length=other.bit_length - 8)
        trie.add_hot_ip(_ip(family, 0xC0A8_012A))
        with pytest.raises(ValueError):
            call(trie, foreign_ip, foreign_prefix)
        # And nothing leaked into this family's trie.
        assert trie.hot_ip_count == 1


class TestLocalMetadata:
    """Sections 16 and 17: stored where declared, accumulated on lookup,
    never copied into descendants. Decision 5 pins the API."""

    def test_visible_only_at_the_prefix_it_was_set_on(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        ip = _ip(family, SECTION_42_NETWORK | 1)
        slash_8 = ancestor_of(ip, bits - 24)
        trie.set_local_metadata(slash_8, "policy", "internal")
        assert dict(trie.local_metadata(slash_8)) == {"policy": "internal"}
        assert dict(trie.local_metadata(ancestor_of(ip, bits - 8))) == {}
        assert dict(trie.local_metadata(ancestor_of(ip, bits))) == {}
        assert dict(trie.local_metadata(root_prefix(family))) == {}
        assert dict(trie.local_metadata(ancestor_of(ip, bits - 25))) == {}

    def test_absent_metadata_is_an_empty_mapping(self, trie: Trie, family: AddressFamily) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        assert dict(trie.local_metadata(prefix)) == {}
        trie.add_hot_ip(_ip(family, SECTION_42_NETWORK | 1))
        assert dict(trie.local_metadata(prefix)) == {}

    def test_path_metadata_is_root_first_and_skips_empty_levels(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        ip = _ip(family, SECTION_42_NETWORK | 1)
        slash_8 = ancestor_of(ip, bits - 24)
        slash_24 = ancestor_of(ip, bits - 8)
        host = ancestor_of(ip, bits)
        # Set deepest first to make sure the order is by depth, not by time.
        trie.set_local_metadata(host, "tags", frozenset({"probe"}))
        trie.set_local_metadata(slash_24, "tags", frozenset({"scanner"}))
        trie.set_local_metadata(slash_8, "policy", "internal")
        trie.set_local_metadata(root_prefix(family), "region", "any")
        path = [(prefix, dict(metadata)) for prefix, metadata in trie.path_metadata(ip)]
        assert path == [
            (root_prefix(family), {"region": "any"}),
            (slash_8, {"policy": "internal"}),
            (slash_24, {"tags": frozenset({"scanner"})}),
            (host, {"tags": frozenset({"probe"})}),
        ]

    def test_path_metadata_of_an_address_outside_the_prefix_is_empty(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        inside = _ip(family, SECTION_42_NETWORK | 1)
        trie.set_local_metadata(ancestor_of(inside, bits - 8), "policy", "internal")
        outside = _ip(family, 0x0A14_1F01)  # 10.20.31.1: shares the /23, not the /24
        assert list(trie.path_metadata(outside)) == []
        assert list(trie.path_metadata(inside)) != []

    def test_path_metadata_does_not_require_the_address_to_be_hot(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        ip = _ip(family, SECTION_42_NETWORK | 1)
        prefix = ancestor_of(ip, family.bit_length - 8)
        trie.set_local_metadata(prefix, "policy", "internal")
        assert [p for p, _ in trie.path_metadata(ip)] == [prefix]

    def test_several_names_on_one_prefix(self, trie: Trie, family: AddressFamily) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        trie.set_local_metadata(prefix, "policy", "internal")
        trie.set_local_metadata(prefix, "mask", 0b0101)
        assert dict(trie.local_metadata(prefix)) == {"policy": "internal", "mask": 0b0101}

    def test_setting_a_name_again_replaces_its_value(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        trie.set_local_metadata(prefix, "policy", "internal")
        trie.set_local_metadata(prefix, "policy", "external")
        assert dict(trie.local_metadata(prefix)) == {"policy": "external"}

    def test_clear_removes_only_that_name(self, trie: Trie, family: AddressFamily) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        trie.set_local_metadata(prefix, "policy", "internal")
        trie.set_local_metadata(prefix, "mask", 1)
        trie.clear_local_metadata(prefix, "policy")
        assert dict(trie.local_metadata(prefix)) == {"mask": 1}
        trie.clear_local_metadata(prefix, "mask")
        assert dict(trie.local_metadata(prefix)) == {}

    def test_clear_of_an_absent_name_is_a_no_op(self, trie: Trie, family: AddressFamily) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        trie.clear_local_metadata(prefix, "policy")
        trie.set_local_metadata(prefix, "mask", 1)
        trie.clear_local_metadata(prefix, "policy")
        assert dict(trie.local_metadata(prefix)) == {"mask": 1}

    @pytest.mark.parametrize("name", INVALID_NAMES, ids=repr)
    def test_clear_with_an_invalid_name_raises_rather_than_no_op(
        self, trie: Trie, family: AddressFamily, name: str
    ) -> None:
        # Amendment 1 item A2: `clear_local_metadata` validates too, because
        # an invalid name can never have been stored, so passing one is a
        # caller bug rather than a no-op. Nothing is cleared.
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        trie.set_local_metadata(prefix, "policy", "internal")
        with pytest.raises(ValueError):
            trie.clear_local_metadata(prefix, name)
        assert dict(trie.local_metadata(prefix)) == {"policy": "internal"}

    @pytest.mark.parametrize("name", VALID_NAMES, ids=repr)
    def test_valid_names_are_accepted(self, trie: Trie, family: AddressFamily, name: str) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        trie.set_local_metadata(prefix, name, 1)
        assert dict(trie.local_metadata(prefix)) == {name: 1}

    @pytest.mark.parametrize("name", INVALID_NAMES, ids=repr)
    def test_invalid_names_raise_value_error(
        self, trie: Trie, family: AddressFamily, name: str
    ) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        with pytest.raises(ValueError):
            trie.set_local_metadata(prefix, name, 1)
        assert dict(trie.local_metadata(prefix)) == {}

    def test_metadata_never_changes_hot_counts(self, trie: Trie, family: AddressFamily) -> None:
        bits = family.bit_length
        ips = _section_42(family)[:10]
        _add_all(trie, ips)
        before = [trie.path_counts(ip) for ip in ips]
        prefix = ancestor_of(ips[0], bits - 8)
        trie.set_local_metadata(prefix, "policy", "internal")
        trie.set_local_metadata(ancestor_of(ips[0], bits - 30), "policy", "internal")
        trie.set_local_metadata(ancestor_of(ips[0], bits), "policy", "internal")
        assert [trie.path_counts(ip) for ip in ips] == before
        assert trie.hot_ip_count == 10
        assert list(trie.hot_ips()) == ips
        trie.clear_local_metadata(prefix, "policy")
        assert [trie.path_counts(ip) for ip in ips] == before

    def test_metadata_survives_removal_of_every_address_beneath_it(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        # Section 46.6: prefix metadata's lifetime is independent of hot state.
        bits = family.bit_length
        ips = _section_42(family)[:6]
        prefix = ancestor_of(ips[0], bits - 8)
        _add_all(trie, ips)
        trie.set_local_metadata(prefix, "policy", "internal")
        _remove_all(trie, ips)
        assert trie.hot_ip_count == 0
        assert dict(trie.local_metadata(prefix)) == {"policy": "internal"}
        assert [(p, dict(m)) for p, m in trie.path_metadata(ips[0])] == [
            (prefix, {"policy": "internal"})
        ]
        # ... and the prefix keeps counting once addresses come back.
        _add_all(trie, ips[:2])
        assert trie.hot_count(prefix) == 2
        assert dict(trie.local_metadata(prefix)) == {"policy": "internal"}

    def test_metadata_set_before_any_address_is_hot(
        self, trie: Trie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        ip = _ip(family, SECTION_42_NETWORK | 1)
        prefix = ancestor_of(ip, bits - 8)
        trie.set_local_metadata(prefix, "policy", "internal")
        assert trie.add_hot_ip(ip) is True
        assert trie.hot_count(prefix) == 1
        assert [p for p, _ in trie.path_metadata(ip)] == [prefix]
        assert trie.remove_hot_ip(ip) is True
        assert trie.hot_count(prefix) == 0
        assert dict(trie.local_metadata(prefix)) == {"policy": "internal"}


class TestPatriciaShape:
    """Decision 2's representation rules for `PatriciaTrie`: root always
    materialized, a leaf is the host route, every other node has two
    children or is pinned, pruning is immediate; decision 5's pinning."""

    def test_an_empty_trie_is_just_the_root(self, patricia: PatriciaTrie) -> None:
        assert patricia.node_count == 1

    def test_one_address_is_one_compressed_edge(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        # Section 27's example: one path is one edge, not `bit_length` nodes.
        ip = _ip(family, SECTION_42_NETWORK | 0xCA)  # ...11001010, section 27's bit string
        patricia.add_hot_ip(ip)
        assert patricia.node_count == 2
        edges = list(patricia.edges())
        leaf = ancestor_of(ip, family.bit_length)
        assert [edge.prefix for edge in edges] == [root_prefix(family), leaf]
        assert [edge.parent_length for edge in edges] == [-1, 0]
        assert [edge.hot_count for edge in edges] == [1, 1]
        assert [edge.pinned for edge in edges] == [False, False]

    def test_a_branch_point_is_materialized_and_a_pass_through_is_not(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        first = _ip(family, SECTION_42_NETWORK | 1)
        second = _ip(family, 0x0A14_1F01)
        _add_all(patricia, (first, second))
        # root, the /23 where the two paths part, two leaves.
        assert patricia.node_count == 4
        lengths = sorted(edge.prefix.length for edge in patricia.edges())
        assert lengths == [0, bits - 9, bits, bits]
        materialized = {edge.prefix for edge in patricia.edges()}
        assert ancestor_of(first, bits - 9) in materialized
        assert ancestor_of(first, bits - 10) not in materialized
        assert ancestor_of(first, bits - 8) not in materialized
        # ... and the pass-through prefixes still answer (decision 3).
        assert patricia.hot_count(ancestor_of(first, bits - 10)) == 2
        assert patricia.hot_count(ancestor_of(first, bits - 8)) == 1

    def test_two_neighbours_materialize_their_shared_slash_30(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        first = _ip(family, SECTION_42_NETWORK | 1)
        second = _ip(family, SECTION_42_NETWORK | 2)
        _add_all(patricia, (first, second))
        assert patricia.node_count == 4
        edge_by_prefix = {edge.prefix: edge for edge in patricia.edges()}
        branch = edge_by_prefix[ancestor_of(first, bits - 2)]
        assert branch.hot_count == 2
        assert branch.parent_length == 0
        assert edge_by_prefix[ancestor_of(first, bits)].parent_length == bits - 2
        assert edge_by_prefix[ancestor_of(second, bits)].parent_length == bits - 2

    def test_pruning_is_immediate(self, patricia: PatriciaTrie, family: AddressFamily) -> None:
        first = _ip(family, SECTION_42_NETWORK | 1)
        second = _ip(family, SECTION_42_NETWORK | 2)
        _add_all(patricia, (first, second))
        assert patricia.node_count == 4
        patricia.remove_hot_ip(second)
        # The leaf goes and the /30 merges back into one edge root -> leaf.
        assert patricia.node_count == 2
        assert {edge.prefix for edge in patricia.edges()} == {
            root_prefix(family),
            ancestor_of(first, family.bit_length),
        }
        patricia.remove_hot_ip(first)
        assert patricia.node_count == 1

    def test_section_42_node_count_is_bounded_by_the_hot_set(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        ips = _section_42(family)
        _add_all(patricia, ips)
        assert patricia.node_count <= 2 * SECTION_42_HOT_COUNT + 1
        _remove_all(patricia, ips[SECTION_42_COOLED_COUNT:])
        assert patricia.node_count <= 2 * SECTION_42_COOLED_COUNT + 1
        _remove_all(patricia, ips[:SECTION_42_COOLED_COUNT])
        assert patricia.node_count == 1

    def test_metadata_materializes_and_pins_a_prefix_on_a_compressed_edge(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        ip = _ip(family, SECTION_42_NETWORK | 1)
        slash_24 = ancestor_of(ip, bits - 8)
        patricia.add_hot_ip(ip)
        assert patricia.node_count == 2
        patricia.set_local_metadata(slash_24, "policy", "internal")
        assert patricia.node_count == 3
        edge_by_prefix = {edge.prefix: edge for edge in patricia.edges()}
        pinned = edge_by_prefix[slash_24]
        assert pinned.pinned is True
        assert pinned.hot_count == 1
        assert pinned.parent_length == 0
        assert edge_by_prefix[ancestor_of(ip, bits)].parent_length == bits - 8

    def test_pinned_node_survives_removal_and_is_pruned_once_cleared(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        floor = patricia.node_count
        ips = _section_42(family)[:4]
        slash_24 = ancestor_of(ips[0], bits - 8)
        _add_all(patricia, ips)
        patricia.set_local_metadata(slash_24, "policy", "internal")
        _remove_all(patricia, ips)
        # Still there, pinned, with nothing beneath it.
        edge_by_prefix = {edge.prefix: edge for edge in patricia.edges()}
        assert slash_24 in edge_by_prefix
        assert edge_by_prefix[slash_24].pinned is True
        assert edge_by_prefix[slash_24].hot_count == 0
        assert patricia.node_count == floor + 1
        assert dict(patricia.local_metadata(slash_24)) == {"policy": "internal"}
        # Clearing the last name un-pins it and it is pruned like any empty node.
        patricia.clear_local_metadata(slash_24, "policy")
        assert patricia.node_count == floor
        assert all(edge.prefix != slash_24 for edge in patricia.edges())
        assert dict(patricia.local_metadata(slash_24)) == {}

    def test_clearing_one_of_two_names_keeps_the_pin(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        floor = patricia.node_count
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        patricia.set_local_metadata(prefix, "policy", "internal")
        patricia.set_local_metadata(prefix, "mask", 1)
        patricia.clear_local_metadata(prefix, "policy")
        assert patricia.node_count == floor + 1
        assert any(edge.prefix == prefix and edge.pinned for edge in patricia.edges())
        patricia.clear_local_metadata(prefix, "mask")
        assert patricia.node_count == floor

    def test_a_pinned_pass_through_node_is_not_merged_away(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        bits = family.bit_length
        first = _ip(family, SECTION_42_NETWORK | 1)
        second = _ip(family, SECTION_42_NETWORK | 2)
        branch = ancestor_of(first, bits - 2)
        _add_all(patricia, (first, second))
        patricia.set_local_metadata(branch, "policy", "internal")
        patricia.remove_hot_ip(second)
        # Without the pin the /30 would merge back into the root -> leaf edge.
        assert patricia.node_count == 3
        edge_by_prefix = {edge.prefix: edge for edge in patricia.edges()}
        assert edge_by_prefix[branch].pinned is True
        assert edge_by_prefix[branch].hot_count == 1
        assert edge_by_prefix[ancestor_of(first, bits)].parent_length == bits - 2

    def test_metadata_on_the_root_pins_the_root(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        root = root_prefix(family)
        patricia.set_local_metadata(root, "region", "any")
        assert patricia.node_count == 1
        edges = list(patricia.edges())
        assert len(edges) == 1
        assert edges[0].prefix == root
        assert edges[0].parent_length == -1
        assert edges[0].pinned is True
        assert edges[0].hot_count == 0

    def test_edges_are_pre_order_child0_before_child1(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        # A7 pins the sibling order: child0 before child1. Pre-order with 0
        # first is exactly ascending `(network, prefix_length)` -- every node
        # in child0's subtree has the parent's network and a network below
        # every node of child1's subtree, and a parent ties with its child0
        # on network and wins on the shorter length.
        bits = family.bit_length
        ips = [*_section_42(family)[:40], _ip(family, 0x0A14_1F01), _ip(family, 0xC0A8_012A)]
        _add_all(patricia, ips)
        patricia.set_local_metadata(ancestor_of(ips[0], bits - 8), "policy", "internal")
        edges = list(patricia.edges())
        prefixes = [edge.prefix for edge in edges]

        assert prefixes[0] == root_prefix(family)
        assert edges[0].parent_length == -1
        assert len(set(prefixes)) == len(prefixes)
        # The full order A7 fixes.
        assert prefixes == sorted(prefixes, key=lambda p: (p.network, p.length))
        # ... which implies, and is stronger than, parent-before-child.
        seen: list[Prefix] = []
        for edge in edges:
            if edge.prefix.length > 0:
                parents = [
                    p for p in seen if p.length == edge.parent_length and covers(p, edge.prefix)
                ]
                assert len(parents) == 1, edge
            seen.append(edge.prefix)

    def test_edges_visit_the_zero_child_subtree_before_the_one_child_subtree(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        # A7 on the smallest structure that has a choice: two sibling leaves
        # under one branch node. The 0-side leaf must come first.
        bits = family.bit_length
        zero_side = _ip(family, SECTION_42_NETWORK | 2)  # ...10
        one_side = _ip(family, SECTION_42_NETWORK | 3)  # ...11
        _add_all(patricia, (one_side, zero_side))  # inserted 1-side first
        prefixes = [edge.prefix for edge in patricia.edges()]
        assert prefixes == [
            root_prefix(family),
            ancestor_of(zero_side, bits - 1),
            ancestor_of(zero_side, bits),
            ancestor_of(one_side, bits),
        ]

    def test_hot_ip_count_and_node_count_after_metadata_on_an_empty_trie(
        self, patricia: PatriciaTrie, family: AddressFamily
    ) -> None:
        prefix = ancestor_of(_ip(family, SECTION_42_NETWORK), family.bit_length - 8)
        patricia.set_local_metadata(prefix, "policy", "internal")
        assert patricia.hot_ip_count == 0
        assert patricia.node_count == 2
        assert list(patricia.hot_ips()) == []


class TestTrieNode:
    """Section 9's node, as decision 2 lays it out."""

    @staticmethod
    def _fresh_root() -> TrieNode:
        return TrieNode(
            network=0,
            prefix_length=0,
            child0=NO_NODE,
            child1=NO_NODE,
            hot_count=0,
            local_metadata={},
        )

    def test_child_and_set_child_by_bit(self) -> None:
        node = self._fresh_root()
        assert node.child(0) == NO_NODE
        assert node.child(1) == NO_NODE
        node.set_child(1, 7)
        assert node.child(1) == 7
        assert node.child(0) == NO_NODE
        assert node.child1 == 7
        node.set_child(0, 3)
        assert node.child0 == 3

    def test_pinned_is_having_any_local_metadata(self) -> None:
        node = self._fresh_root()
        assert node.pinned is False
        node.local_metadata["policy"] = "internal"
        assert node.pinned is True
        del node.local_metadata["policy"]
        assert node.pinned is False

    def test_no_node_is_never_a_valid_index(self) -> None:
        assert NO_NODE == -1


class TestNodeArena:
    """Section 11 / section 27: integer ids into one arena, freed slots
    reused before the arena grows (decision 2)."""

    def test_allocate_returns_a_fresh_zeroed_node(self) -> None:
        arena = NodeArena()
        node_id = arena.allocate(0x0A14_1E00, 24)
        node = arena[node_id]
        assert node.network == 0x0A14_1E00
        assert node.prefix_length == 24
        assert node.child0 == NO_NODE
        assert node.child1 == NO_NODE
        assert node.hot_count == 0
        assert dict(node.local_metadata) == {}
        assert node.pinned is False

    def test_ids_are_distinct_while_live(self) -> None:
        arena = NodeArena()
        ids = [arena.allocate(0, 0) for _ in range(10)]
        assert len(set(ids)) == 10
        assert arena.live_count == 10
        assert arena.capacity == 10
        assert set(arena.live_ids()) == set(ids)

    def test_getitem_returns_the_same_node_object(self) -> None:
        arena = NodeArena()
        node_id = arena.allocate(0, 0)
        arena[node_id].hot_count += 1
        assert arena[node_id].hot_count == 1

    def test_freed_slots_are_reused_most_recent_first(self) -> None:
        # A9 pins the reuse order: LIFO, the slot freed most recently is the
        # next one handed out.
        arena = NodeArena()
        ids = [arena.allocate(0, 0) for _ in range(5)]
        peak = arena.capacity
        arena.free(ids[1])
        arena.free(ids[3])
        assert arena.live_count == 3
        assert arena.capacity == peak
        assert set(arena.live_ids()) == {ids[0], ids[2], ids[4]}
        assert arena.allocate(0, 0) == ids[3]  # freed last, handed out first
        assert arena.allocate(0, 0) == ids[1]
        assert arena.capacity == peak
        assert arena.live_count == 5
        # Only once the free list is empty does the arena grow.
        arena.allocate(0, 0)
        assert arena.capacity == peak + 1

    def test_lifo_reuse_of_a_single_slot_is_stable(self) -> None:
        arena = NodeArena()
        first = arena.allocate(0, 0)
        peak = arena.capacity
        for _ in range(10):
            arena.free(first)
            assert arena.allocate(0, 0) == first
            assert arena.capacity == peak

    def test_oscillation_costs_no_allocation_after_the_first_cycle(self) -> None:
        # Section 11: HOT/COLD oscillation must not churn allocation.
        arena = NodeArena()
        first_cycle = [arena.allocate(0, 32) for _ in range(32)]
        for node_id in first_cycle:
            arena.free(node_id)
        peak = arena.capacity
        for _ in range(20):
            cycle = [arena.allocate(0, 32) for _ in range(32)]
            for node_id in cycle:
                arena.free(node_id)
        assert arena.capacity == peak
        assert arena.live_count == 0

    def test_a_reused_slot_comes_back_zeroed(self) -> None:
        arena = NodeArena()
        node_id = arena.allocate(0x0A00_0000, 8)
        arena[node_id].hot_count = 5
        arena[node_id].set_child(0, 99)
        arena[node_id].local_metadata["policy"] = "x"
        arena.free(node_id)
        again = arena.allocate(0xC0A8_0000, 16)
        node = arena[again]
        assert (node.network, node.prefix_length) == (0xC0A8_0000, 16)
        assert node.hot_count == 0
        assert node.child0 == NO_NODE
        assert node.child1 == NO_NODE
        assert dict(node.local_metadata) == {}

    def test_free_of_a_dead_slot_raises(self) -> None:
        arena = NodeArena()
        node_id = arena.allocate(0, 0)
        arena.free(node_id)
        with pytest.raises(ValueError):
            arena.free(node_id)
        with pytest.raises(ValueError):
            arena.free(node_id + 1)
        with pytest.raises(ValueError):
            arena.free(NO_NODE)

    def test_getitem_of_a_dead_slot_raises(self) -> None:
        arena = NodeArena()
        node_id = arena.allocate(0, 0)
        arena.free(node_id)
        for dead in (node_id, NO_NODE, node_id + 1):
            with pytest.raises(ValueError):
                _ = arena[dead]

    def test_empty_arena(self) -> None:
        arena = NodeArena()
        assert arena.live_count == 0
        assert arena.capacity == 0
        assert list(arena.live_ids()) == []
