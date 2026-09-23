"""Patricia and binary tries agree on every query, differentially tested.

Spec: section 8 (what the trie must answer), section 12 (hot_count), section
27 ("the logical model MUST remain equivalent to the binary trie"), section
35 (one trie per family).

The interface under test is ADR-0014 decision 2 (`HotTrie`) as satisfied by
`BinaryTrie` -- the bit-by-bit reference that shares no traversal code with
the production path (decision 7) -- and `PatriciaTrie` (decision 6). The
identical operation sequence is applied to one of each, and after every
operation they must agree on:

* the bool returned by `add_hot_ip` / `remove_hot_ip` (decision 3);
* `hot_ip_count`;
* `list(iter_prefix_counts())`, as a full sequence -- this is section 27's
  equivalence made operational (decisions 2 and 6) -- and the same with
  `min_length=8`;
* `list(iter_hot_addresses())`;
* `contains`, `ancestor_counts` (at `min_length` 0 and 8) and
  `longest_matching_prefix` for every probe address, and `hot_count` for
  every ancestor length of every probe address, for sibling prefixes that are
  usually empty, for `/0`, and for randomly drawn prefixes;
* and all of the above again after `clear()`.

`node_count` is deliberately *not* compared: the two representations differ
there by design (decision 2, `iter_nodes`). Section 27's compression claim is
pinned directly instead -- one hot IPv4 address costs the binary trie 33
nodes and the Patricia trie 1.

Because two implementations agreeing proves nothing if both are wrong the
same way, the sequences are also checked against a plain model of the HOT
set: `iter_prefix_counts()` must equal the model's counts in pre-order
depth-first order (decision 2: a prefix before its descendants, branch 0
before branch 1), which is exactly ascending `(network, length)` order.

Assumptions not pinned by the spec or ADR-0014:

* A `PatriciaTrie` exposes its `NodeArena` as the attribute `arena` (decision
  5 describes the arena but names no attribute); it is read only to check that
  `clear()` resets it (decision 2).
* Section 27's example bit string is 32 bits long, i.e. the IPv4 address
  `0.0.0.202`. Its IPv6 counterparts here are `0:ca::` (the same 24-bit zero
  run and `11001010` at the top of a 128-bit address) and `::ca` (a 120-bit
  zero run).
* On the hypothesis-driven test every pool address is probed at every length
  after every step, whether or not it has been touched yet; an untouched one
  is simply another probe that should answer zero.
* On the full-/24 test the per-step comparison probes only the address just
  operated on plus a fixed handful (probing all 256 at 33 lengths after each
  of 512 steps is several million queries); every address is probed at every
  length once the /24 is full.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.testkit.generators import (
    TrieOperation,
    address_pools,
    prefixes,
    trie_operations,
)
from hammertime.trie.structure import BinaryTrie, NodeView, PatriciaTrie, PrefixCount
from hypothesis import given, settings
from hypothesis import strategies as st

FAMILIES = [
    pytest.param(AddressFamily.IPV4, id="ipv4"),
    pytest.param(AddressFamily.IPV6, id="ipv6"),
]

TrieClass = type[BinaryTrie] | type[PatriciaTrie]


def _prefix_of(address: Address, length: int) -> Prefix:
    host_bits = address.bit_length - length
    network = (address.value >> host_bits) << host_bits
    return Prefix(family=address.family, network=network, length=length)


def _sibling(address: Address, length: int) -> Prefix:
    """The length-`length` prefix differing from `address`'s only in its last bit."""

    flip = 1 << (address.bit_length - length)
    flipped = Address(family=address.family, value=address.value ^ flip)
    return _prefix_of(flipped, length)


def _model_prefix_counts(hot: Iterable[Address], family: AddressFamily) -> list[PrefixCount]:
    """Section 12 from the model, in decision 2's pre-order DFS order."""

    counts: dict[Prefix, int] = {}
    for address in hot:
        for length in range(family.bit_length + 1):
            prefix = _prefix_of(address, length)
            counts[prefix] = counts.get(prefix, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (item[0].network, item[0].length))
    return [PrefixCount(prefix, count) for prefix, count in ordered]


def _apply(
    binary: BinaryTrie, patricia: PatriciaTrie, operation: TrieOperation, model: set[Address]
) -> None:
    """Apply one operation to both tries and the model; the returns must agree."""

    address = operation.address
    if operation.kind == "add":
        expected = address not in model
        got_binary = binary.add_hot_ip(address)
        got_patricia = patricia.add_hot_ip(address)
        model.add(address)
    else:
        expected = address in model
        got_binary = binary.remove_hot_ip(address)
        got_patricia = patricia.remove_hot_ip(address)
        model.discard(address)
    assert got_binary is expected, f"binary {operation.kind}({address})"
    assert got_patricia is expected, f"patricia {operation.kind}({address})"


def _assert_equivalent(
    binary: BinaryTrie,
    patricia: PatriciaTrie,
    model: set[Address],
    probes: Sequence[Address],
    extra_prefixes: Iterable[Prefix] = (),
    *,
    check_model: bool = True,
) -> None:
    """Every observable of decision 2 except `node_count` / `iter_nodes`."""

    family = binary.family
    bit_length = family.bit_length

    assert patricia.hot_ip_count == binary.hot_ip_count == len(model)

    binary_counts = list(binary.iter_prefix_counts())
    assert list(patricia.iter_prefix_counts()) == binary_counts
    if check_model:
        assert binary_counts == _model_prefix_counts(model, family)

    binary_from_8 = list(binary.iter_prefix_counts(min_length=8))
    patricia_from_8 = list(patricia.iter_prefix_counts(min_length=8))
    assert patricia_from_8 == binary_from_8
    assert binary_from_8 == [pc for pc in binary_counts if pc.prefix.length >= 8]

    binary_hot = list(binary.iter_hot_addresses())
    assert list(patricia.iter_hot_addresses()) == binary_hot
    assert binary_hot == sorted(model, key=lambda a: a.value)

    root = Prefix(family=family, network=0, length=0)
    assert patricia.hot_count(root) == binary.hot_count(root) == len(model)

    for address in probes:
        assert patricia.contains(address) == binary.contains(address) == (address in model)

        expected_ancestors = binary.ancestor_counts(address)
        assert patricia.ancestor_counts(address) == expected_ancestors, address
        expected_from_8 = binary.ancestor_counts(address, min_length=8)
        assert patricia.ancestor_counts(address, min_length=8) == expected_from_8, address

        expected_lmp = binary.longest_matching_prefix(address)
        assert patricia.longest_matching_prefix(address) == expected_lmp, address

        for length in range(bit_length + 1):
            prefix = _prefix_of(address, length)
            assert patricia.hot_count(prefix) == binary.hot_count(prefix), prefix
        for length in (1, bit_length // 2, bit_length):
            sibling = _sibling(address, length)
            assert patricia.hot_count(sibling) == binary.hot_count(sibling), sibling

    for prefix in extra_prefixes:
        assert patricia.hot_count(prefix) == binary.hot_count(prefix), prefix


def _assert_cleared(binary: BinaryTrie, patricia: PatriciaTrie, probes: Sequence[Address]) -> None:
    binary.clear()
    patricia.clear()
    _assert_equivalent(binary, patricia, set(), probes)
    assert binary.node_count == patricia.node_count == 0
    assert patricia.arena.capacity == 0
    for address in probes:
        assert binary.longest_matching_prefix(address) is None
        assert patricia.longest_matching_prefix(address) is None


# --------------------------------------------------------------------------
# Randomized sequences.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@given(data=st.data())
@settings(deadline=None, max_examples=25)
def test_random_sequences_agree_after_every_operation(
    family: AddressFamily, data: st.DataObject
) -> None:
    pool = data.draw(address_pools(family, min_size=1, max_size=6), label="pool")
    operations = data.draw(trie_operations(pool, max_size=20), label="operations")
    extra = data.draw(st.lists(prefixes(family), max_size=4), label="extra prefixes")

    binary = BinaryTrie(family)
    patricia = PatriciaTrie(family)
    model: set[Address] = set()
    _assert_equivalent(binary, patricia, model, pool, extra)
    for operation in operations:
        _apply(binary, patricia, operation, model)
        _assert_equivalent(binary, patricia, model, pool, extra)

    _assert_cleared(binary, patricia, pool)
    model.clear()

    # A cleared pair is reusable and still agrees.
    for operation in operations:
        _apply(binary, patricia, operation, model)
    _assert_equivalent(binary, patricia, model, pool, extra)


# --------------------------------------------------------------------------
# Hand-written scenarios.
# --------------------------------------------------------------------------


def _parse_all(*texts: str) -> list[Address]:
    return [Address.parse(text) for text in texts]


def _adds(addresses: Iterable[Address]) -> list[TrieOperation]:
    return [TrieOperation("add", a) for a in addresses]


def _removes(addresses: Iterable[Address]) -> list[TrieOperation]:
    return [TrieOperation("remove", a) for a in addresses]


def _churn(pool: Sequence[Address]) -> list[TrieOperation]:
    """Add all; remove every other one; re-add them; remove all in reverse.

    Removing a leaf collapses its parent into a compressed edge and re-adding
    it splits the edge again, so this walks the Patricia trie through both
    edge operations at the divergence points the pool has. One redundant add
    and one redundant remove are included (decision 3).
    """

    odd = pool[1::2]
    return [
        *_adds(pool),
        *_adds(pool[:1]),
        *_removes(odd),
        *_removes(odd[:1]),
        *_adds(odd),
        *_removes(reversed(pool)),
    ]


SCENARIOS = [
    pytest.param(
        _parse_all("192.0.2.4", "192.0.2.5"),
        id="ipv4-last-bit-siblings",
    ),
    pytest.param(
        _parse_all("2001:db8::4", "2001:db8::5"),
        id="ipv6-last-bit-siblings",
    ),
    pytest.param(
        # Share 31, 24, 23 and 0 leading bits with 10.0.0.0 respectively.
        _parse_all("10.0.0.0", "10.0.0.1", "10.0.0.128", "10.0.1.0", "138.0.0.0"),
        id="ipv4-long-shared-prefix",
    ),
    pytest.param(
        # Share 127, 96, 63 and 0 leading bits with 2001:db8:: respectively.
        _parse_all(
            "2001:db8::",
            "2001:db8::1",
            "2001:db8::8000:0",
            "2001:db8:0:1::",
            "a001:db8::",
        ),
        id="ipv6-long-shared-prefix",
    ),
    pytest.param(
        _parse_all("0.0.0.0", "255.255.255.255", "0.0.0.1", "255.255.255.254"),
        id="ipv4-extremes",
    ),
    pytest.param(
        _parse_all("::", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "::1", "8000::"),
        id="ipv6-extremes",
    ),
    pytest.param(
        # Section 27: 00000000 00000000 00000000 11001010.
        _parse_all("0.0.0.202", "0.0.0.203", "0.0.0.0"),
        id="ipv4-section-27-zero-run",
    ),
    pytest.param(
        _parse_all("0:ca::", "0:ca::1", "::ca", "::"),
        id="ipv6-section-27-zero-runs",
    ),
]


@pytest.mark.parametrize("pool", SCENARIOS)
def test_scenario_agrees_after_every_operation(pool: list[Address]) -> None:
    family = pool[0].family
    binary = BinaryTrie(family)
    patricia = PatriciaTrie(family)
    model: set[Address] = set()
    for operation in _churn(pool):
        _apply(binary, patricia, operation, model)
        _assert_equivalent(binary, patricia, model, pool)
    assert model == set()

    for operation in _adds(pool):
        _apply(binary, patricia, operation, model)
    _assert_equivalent(binary, patricia, model, pool)
    _assert_cleared(binary, patricia, pool)


def test_a_full_slash_24_agrees() -> None:
    """All 256 addresses of one /24, added in order and removed in another."""

    pool = [Address.parse(f"203.0.113.{i}") for i in range(256)]
    binary = BinaryTrie(AddressFamily.IPV4)
    patricia = PatriciaTrie(AddressFamily.IPV4)
    model: set[Address] = set()
    fixed = [pool[0], pool[1], pool[128], pool[255], Address.parse("203.0.112.255")]

    for operation in _adds(pool):
        _apply(binary, patricia, operation, model)
        _assert_equivalent(binary, patricia, model, [operation.address, *fixed], check_model=False)
    _assert_equivalent(binary, patricia, model, pool)

    slash_24 = Prefix.parse("203.0.113.0/24")
    assert binary.hot_count(slash_24) == patricia.hot_count(slash_24) == 256
    slash_25 = Prefix.parse("203.0.113.128/25")
    assert binary.hot_count(slash_25) == patricia.hot_count(slash_25) == 128
    # /0 .. /24 is one chain of 25 prefixes; beneath the /24 lies a complete
    # binary tree of depth 8, i.e. 2**9 - 2 further prefixes.
    assert len(list(patricia.iter_prefix_counts())) == 25 + 2**9 - 2

    # 37 is coprime to 256, so this visits every address once, out of order.
    for index in ((i * 37) % 256 for i in range(256)):
        operation = TrieOperation("remove", pool[index])
        _apply(binary, patricia, operation, model)
        _assert_equivalent(binary, patricia, model, [operation.address, *fixed], check_model=False)
    assert model == set()
    _assert_equivalent(binary, patricia, model, fixed)
    assert list(patricia.iter_prefix_counts()) == []


# --------------------------------------------------------------------------
# Section 27's compression claim, pinned directly.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "binary_nodes"),
    [
        pytest.param("0.0.0.202", 33, id="ipv4"),
        pytest.param("0:ca::", 129, id="ipv6"),
    ],
)
def test_one_hot_address_is_one_patricia_node(text: str, binary_nodes: int) -> None:
    """Section 27: a path with only one child is a compressed edge, not one
    node per bit. The binary trie materializes `/0` through `/bit_length`
    (decision 2: its node set is its prefix set); the Patricia trie holds a
    single leaf (decision 6)."""

    address = Address.parse(text)
    binary = BinaryTrie(address.family)
    patricia = PatriciaTrie(address.family)
    assert binary.add_hot_ip(address) is True
    assert patricia.add_hot_ip(address) is True

    assert binary.node_count == binary_nodes
    assert patricia.node_count == 1
    leaf = _prefix_of(address, address.bit_length)
    assert list(patricia.iter_nodes()) == [NodeView(leaf, 1, ())]

    # ... and still the same logical model.
    binary_counts = list(binary.iter_prefix_counts())
    assert list(patricia.iter_prefix_counts()) == binary_counts
    assert len(binary_counts) == binary_nodes


# --------------------------------------------------------------------------
# Query semantics (decision 2), pinned on both representations.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("make_trie", [BinaryTrie, PatriciaTrie], ids=["binary", "patricia"])
class TestQuerySemantics:
    def test_ancestor_counts_report_every_length_including_zero_counts(
        self, make_trie: TrieClass
    ) -> None:
        """ADR-0010 decision 3 needs the zero counts after a removal."""

        address = Address.parse("192.168.1.42")
        trie = make_trie(AddressFamily.IPV4)
        assert trie.add_hot_ip(address) is True
        ones = tuple(PrefixCount(_prefix_of(address, n), 1) for n in range(33))
        assert trie.ancestor_counts(address) == ones

        from_8 = trie.ancestor_counts(address, min_length=8)
        assert len(from_8) == 25
        assert [pc.prefix.length for pc in from_8] == list(range(8, 33))

        assert trie.remove_hot_ip(address) is True
        zeros = tuple(PrefixCount(_prefix_of(address, n), 0) for n in range(8, 33))
        assert trie.ancestor_counts(address, min_length=8) == zeros

    @pytest.mark.parametrize("min_length", [-1, 33])
    def test_ancestor_counts_reject_an_out_of_range_min_length(
        self, make_trie: TrieClass, min_length: int
    ) -> None:
        trie = make_trie(AddressFamily.IPV4)
        with pytest.raises(ValueError):
            trie.ancestor_counts(Address.parse("192.168.1.42"), min_length=min_length)

    def test_hot_count_of_an_absent_prefix_is_zero(self, make_trie: TrieClass) -> None:
        """ADR-0010 decision 4: absence of a node is a zero, not an error."""

        trie = make_trie(AddressFamily.IPV4)
        assert trie.add_hot_ip(Address.parse("192.168.1.42")) is True
        assert trie.hot_count(Prefix.parse("10.0.0.0/8")) == 0
        assert trie.hot_count(Prefix.parse("192.168.1.43/32")) == 0
        assert trie.hot_count(Prefix.parse("192.168.1.0/24")) == 1

    def test_longest_matching_prefix(self, make_trie: TrieClass) -> None:
        """Section 8, decision 2: the longest prefix of the address whose
        hot_count is positive; None only for an empty trie."""

        trie = make_trie(AddressFamily.IPV4)
        assert trie.longest_matching_prefix(Address.parse("192.168.1.42")) is None
        assert trie.add_hot_ip(Address.parse("192.168.1.42")) is True
        assert trie.add_hot_ip(Address.parse("10.0.0.1")) is True
        lmp = trie.longest_matching_prefix
        assert lmp(Address.parse("192.168.1.42")) == Prefix.parse("192.168.1.42/32")
        assert lmp(Address.parse("192.168.1.43")) == Prefix.parse("192.168.1.42/31")
        assert lmp(Address.parse("192.168.1.200")) == Prefix.parse("192.168.1.0/24")
        # 172 = 10101100 shares only its first bit with 192 = 11000000.
        assert lmp(Address.parse("172.16.0.1")) == Prefix.parse("128.0.0.0/1")
        # 0.0.0.0 shares 4 bits with 10.0.0.1 (10 = 00001010).
        assert lmp(Address.parse("0.0.0.0")) == Prefix.parse("0.0.0.0/4")
