"""Random add/remove/metadata sequences preserve every trie invariant.

Spec: section 11 (`hot_count >= 0` after removal; oscillation), section 12
(`hot_count(node)` is the number of HOT /32s beneath it; the leaf rule; the
sum rule -- "more important than cached `prefix_state`"), section 16 and
section 17 (metadata stored where declared, accumulated on lookup), section
27 (the logical model stays the binary trie under compression), section
46.5 (the attribute map's keys are exactly the HOT addresses), section 46.6
(prefix metadata outlives hot state).

ADR-0012 decision 4, as amended by Amendment 1 item A12, fixes the shape of
this file: a `hypothesis.stateful.RuleBasedStateMachine` whose rules are
`add_hot_ip`, `remove_hot_ip` (biased towards addresses already HOT so
removal and oscillation are exercised), `set_local_metadata` and
`clear_local_metadata`, applied to a `BinaryTrie` and a `PatriciaTrie` of
one family in lock-step, plus:

* **Sampling.** Addresses come from a fixed per-family pool, never from the
  whole address space -- a random 32- or 128-bit address almost never
  shares a prefix with another, so an unconstrained draw exercises neither
  branching, nor compression, nor the merge that follows a prune. The pool
  must contain at least 8 addresses with four structural properties; they
  are asserted by `test_pool_satisfies_a12`, so an edit to `_IPV4_LOW` or
  `_IPV6_LOW` cannot silently break one. Metadata rules draw prefixes from
  the pool's ancestors at lengths {8, 16, 23, 24, 32} (IPv6: +96).
* **Check cadence.** After every rule: `check_invariants` on both tries,
  the I9 bound and the Patricia shape rules, and a *bounded probe* of the
  equivalence contract -- the address the rule touched, the prefixes it
  touched, and a fixed sample of 8 pool members. The **full** contract of
  decision 3 (every pool member, `set(logical_prefixes())` at each
  `min_length`, `list(hot_ips())`, `path_metadata`) and the testkit helpers
  (`assert_hot_count_consistent` and friends) run at `@initialize` and in
  `teardown()`, not per step, because `assert_hot_count_consistent` is
  O(|hot| x bit_length) at best and the probe must stay cheap enough that
  the module fits A12's 30 s CI budget.

The bounded probe is cheaper than it looks because `path_counts(ip)`
returns the count of *every* ancestor of `ip` in one call, so probing 8
addresses compares 8 x (bit_length + 1) ancestor counts for 8 calls per
trie rather than one descent per ancestor.

Besides the two tries, the machine keeps a *model*: the set of HOT
addresses and a `prefix -> {name: value}` map. Both tries are compared to
the model as well as to each other, so a bug shared by both
representations (say, a wrong `path_counts` index) is still caught.

Removal bias: `add` feeds a `Bundle` of addresses that have been HOT;
`remove_hot` consumes from it, so most removes hit an address that is (or
was recently) HOT, while `remove_any` keeps section 11's not-held no-op in
play.

ASSUMPTIONS -- details the ADR does not pin:

1. `Address`, `AddressFamily`, `Prefix` come from the
   `hammertime.core.addressing.address` / `.prefix` submodules.
2. The machine class is built by a factory per family so that the rule
   strategies can close over the family; the ADR only asks for "a
   `RuleBasedStateMachine` ... of one family". Both families are run, the
   IPv4 machine as `TestTrieMachine` and the IPv6 one as
   `TestTrieMachineV6`, with A12's shipped bounds (IPv4 25 x 25, IPv6
   10 x 12).
3. The IPv6 pool is 8 members to IPv4's 12. A12 sets the floor at 8 per
   family and notes the IPv6 pool may be smaller because its per-step cost
   is 4x; 8 is the smallest size that still carries all four structural
   properties (it takes 4 members to make three /120-sharing pairs, 2 more
   for the second sibling pair, and 2 isolated ones).

No assertion here compares prefix or address *text*: every address is built
from an integer and every prefix from `ancestor_of`, so A11's caveat that
CPython canonicalises `2001:db8::10.20.30.0` to `2001:db8::a14:1e00` does
not bite.
"""

import pytest
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.testkit.invariants import (
    ancestor_of,
    assert_attribute_records_consistent,
    assert_hot_count_consistent,
    assert_no_negative_counts,
    covers,
    expected_hot_counts,
    root_prefix,
)
from hammertime.trie.structure.binary_trie import BinaryTrie
from hammertime.trie.structure.invariants import check_invariants, logical_prefixes
from hammertime.trie.structure.patricia import PatriciaTrie
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    precondition,
    rule,
)

NAMES = ("policy", "tags", "mask")

V6_DOC = 0x2001_0DB8 << 96

# A12's fixed pool, as low 32 bits (IPv6 maps them under 2001:db8::/96 --
# decision 1 as amended by A11, so a /24 here is a /120 there). The comment
# on each member names the structural property it provides; the four A12
# requires are checked by `test_pool_satisfies_a12` below, so a later edit
# to this tuple cannot silently break one.
_IPV4_LOW = (
    0x0A14_1E01,  # 10.20.30.1      section 42's /24; shares it with the next three
    0x0A14_1E02,  # 10.20.30.2      sibling leaf of .3
    0x0A14_1E03,  # 10.20.30.3      sibling leaf of .2 -- pruning one merges the other
    0x0A14_1E81,  # 10.20.30.129    the same /24's other half (a different /25)
    0x0A14_1F2A,  # 10.20.31.42     10.20.31.0/24, so 10.20.30.0/23 branches
    0x0A14_1F2B,  # 10.20.31.43     sibling leaf of .42
    0x0A14_1C01,  # 10.20.28.1      a third /24, so 10.20.28.0/22 has two /23 children
    0xC633_6401,  # 198.51.100.1    198.51.100.0/24, A12's third required network
    0xC633_642A,  # 198.51.100.42   sibling leaf of .43
    0xC633_642B,  # 198.51.100.43   sibling leaf of .42
    0x0000_0000,  # 0.0.0.0         all-zero path; shares at most /4 with any other
    0xFFFF_FFFF,  # 255.255.255.255 all-ones path; shares at most /2 with any other
)

# The IPv6 pool keeps every structural property with fewer members, since
# its per-step cost is 4x: the four 10.20.30.0/120 members, the sibling
# pair in 10.20.31.0/120, one 198.51.100.0/120 member (isolated at /96 from
# the rest), and ::1 (isolated at /2 from everything under the doc prefix).
_IPV6_LOW = (
    0x0A14_1E01,
    0x0A14_1E02,
    0x0A14_1E03,
    0x0A14_1E81,
    0x0A14_1F2A,
    0x0A14_1F2B,
    0xC633_6401,
)

# A12: metadata rules draw from the pool's ancestors at these lengths.
_METADATA_LENGTHS_V4 = (8, 16, 23, 24, 32)

FAMILIES = (AddressFamily.IPV4, AddressFamily.IPV6)

# How many pool members the per-step bounded probe compares (A12: at most 8).
PROBE_SAMPLE_SIZE = 8


def _pool(family: AddressFamily) -> tuple[Address, ...]:
    if family is AddressFamily.IPV4:
        return tuple(Address(family=family, value=low) for low in _IPV4_LOW)
    members = [Address(family=family, value=V6_DOC | low) for low in _IPV6_LOW]
    members.append(Address(family=family, value=1))  # ::1, isolated from the rest
    return tuple(members)


_POOLS = {family: _pool(family) for family in FAMILIES}

# (max_examples, stateful_step_count) per family -- A12's shipped bounds.
_SETTINGS = {
    AddressFamily.IPV4: (25, 25),
    AddressFamily.IPV6: (10, 12),
}


def _metadata_lengths(family: AddressFamily) -> tuple[int, ...]:
    shift = 0 if family is AddressFamily.IPV4 else 96
    return tuple(length + shift for length in _METADATA_LENGTHS_V4)


def _metadata_prefixes(family: AddressFamily) -> tuple[Prefix, ...]:
    """The pool's ancestors at A12's lengths, deduplicated and ordered."""

    found = {
        ancestor_of(ip, length) for ip in _POOLS[family] for length in _metadata_lengths(family)
    }
    return tuple(sorted(found, key=lambda p: (p.length, p.network)))


def _min_lengths(family: AddressFamily) -> tuple[int, ...]:
    """Decision 3's `m` values."""

    return tuple(sorted({0, 8, 24, family.bit_length}))


def _by_depth(prefix: Prefix) -> tuple[int, int]:
    return (prefix.length, prefix.network)


def _common_prefix_length(first: Address, second: Address) -> int:
    """How many leading bits `first` and `second` share."""

    bits = first.bit_length
    difference = first.value ^ second.value
    return bits if difference == 0 else bits - difference.bit_length()


def _address(family: AddressFamily, low: int) -> Address:
    value = low if family is AddressFamily.IPV4 else V6_DOC | low
    return Address(family=family, value=value)


@pytest.mark.parametrize("family", FAMILIES, ids=["ipv4", "ipv6"])
def test_pool_satisfies_a12(family: AddressFamily) -> None:
    """A12's four structural properties, asserted so that editing the pool
    cannot silently remove one."""

    bits = family.bit_length
    pool = _POOLS[family]
    pairs = [(a, b) for i, a in enumerate(pool) for b in pool[i + 1 :]]
    shared = {pair: _common_prefix_length(*pair) for pair in pairs}

    assert len(pool) >= 8
    assert len(set(pool)) == len(pool)
    assert all(ip.family is family for ip in pool)

    # (1) at least three pairs share a /24 (IPv6 /120) but not a /32 (/128).
    slash_24 = [pair for pair, common in shared.items() if bits - 8 <= common < bits]
    assert len(slash_24) >= 3

    # (2) at least two pairs differ only in their last bit (sibling leaves).
    siblings = [pair for pair, common in shared.items() if common == bits - 1]
    assert len(siblings) >= 2

    # (3) at least two members share no prefix longer than /8 (IPv6 /104)
    #     with any other member.
    isolated = [
        ip
        for ip in pool
        if max(
            (_common_prefix_length(ip, other) for other in pool if other != ip),
            default=0,
        )
        <= bits - 24
    ]
    assert len(isolated) >= 2

    # (4) the three networks section 42's shape needs are all represented.
    for low in (0x0A14_1E00, 0x0A14_1F00, 0xC633_6400):
        network = ancestor_of(_address(family, low), bits - 8)
        assert any(covers(network, ancestor_of(ip, bits)) for ip in pool), network


def build_machine(family: AddressFamily) -> type[RuleBasedStateMachine]:
    """A lock-step machine over both tries of `family` (decision 4)."""

    bits = family.bit_length
    min_lengths = _min_lengths(family)
    pool_members = _POOLS[family]
    probe_sample = pool_members[:PROBE_SAMPLE_SIZE]
    pool = st.sampled_from(pool_members)
    metadata_prefixes = st.sampled_from(_metadata_prefixes(family))
    names = st.sampled_from(NAMES)
    values = st.integers()

    class TrieMachine(RuleBasedStateMachine):
        hot_addresses: Bundle[Address] = Bundle("hot_addresses")

        def __init__(self) -> None:
            super().__init__()
            self.binary = BinaryTrie(family)
            self.patricia = PatriciaTrie(family)
            self.hot: set[Address] = set()
            self.metadata: dict[Prefix, dict[str, object]] = {}
            self.touched: dict[Prefix, None] = {}
            self.last_ip: Address | None = None

        # -- helpers ---------------------------------------------------------

        def _set(self, prefix: Prefix, name: str, value: object) -> None:
            self.binary.set_local_metadata(prefix, name, value)
            self.patricia.set_local_metadata(prefix, name, value)
            self.metadata.setdefault(prefix, {})[name] = value
            self.touched[prefix] = None

        def _clear(self, prefix: Prefix, name: str) -> None:
            self.binary.clear_local_metadata(prefix, name)
            self.patricia.clear_local_metadata(prefix, name)
            entry = self.metadata.get(prefix)
            if entry is not None:
                entry.pop(name, None)
                if not entry:
                    del self.metadata[prefix]
            self.touched[prefix] = None

        def _apply(self, ip: Address, *, add: bool) -> None:
            expected = (ip not in self.hot) if add else (ip in self.hot)
            if add:
                assert self.binary.add_hot_ip(ip) is expected
                assert self.patricia.add_hot_ip(ip) is expected
                self.hot.add(ip)
            else:
                assert self.binary.remove_hot_ip(ip) is expected
                assert self.patricia.remove_hot_ip(ip) is expected
                self.hot.discard(ip)
            self.last_ip = ip

        # -- rules -----------------------------------------------------------

        @rule(target=hot_addresses, ip=pool)
        def add(self, ip: Address) -> Address:
            self._apply(ip, add=True)
            return ip

        @rule(ip=consumes(hot_addresses))
        def remove_hot(self, ip: Address) -> None:
            self._apply(ip, add=False)

        @rule(ip=pool)
        def remove_any(self, ip: Address) -> None:
            self._apply(ip, add=False)

        @rule(prefix=metadata_prefixes, name=names, value=values)
        def set_metadata(self, prefix: Prefix, name: str, value: int) -> None:
            self._set(prefix, name, value)

        @rule(prefix=metadata_prefixes, name=names)
        def clear_metadata(self, prefix: Prefix, name: str) -> None:
            self._clear(prefix, name)

        @precondition(lambda self: bool(self.metadata))
        @rule(index=st.integers(min_value=0), name=names)
        def clear_declared_metadata(self, index: int, name: str) -> None:
            declared = sorted(self.metadata, key=_by_depth)
            self._clear(declared[index % len(declared)], name)

        # -- per-step checks (A12's cadence) ----------------------------------

        @invariant()
        def structure_is_sound(self) -> None:
            check_invariants(self.binary)
            check_invariants(self.patricia)

        @invariant()
        def patricia_is_bounded_and_shaped(self) -> None:
            patricia = self.patricia
            edges = list(patricia.edges())
            pinned = sum(1 for edge in edges if edge.pinned)
            # I9
            assert patricia.node_count <= 2 * patricia.hot_ip_count + pinned + 1
            # Decision 5: exactly the prefixes holding metadata are pinned.
            assert {edge.prefix for edge in edges if edge.pinned} == set(self.metadata)
            # I6 / I7 from edges(); the root's parent_length is -1.
            for edge in edges:
                if edge.prefix.length == 0:
                    assert edge.parent_length == -1
                    continue
                assert -1 < edge.parent_length < edge.prefix.length, edge
                assert edge.hot_count > 0 or edge.pinned, edge
                if edge.prefix.length < bits and not edge.pinned:
                    children = [
                        child
                        for child in edges
                        if child.parent_length == edge.prefix.length
                        and covers(edge.prefix, child.prefix)
                    ]
                    assert len(children) == 2, edge
            # No metadata and nothing HOT means the root alone.
            if not self.hot and not self.metadata:
                assert patricia.node_count == 1

        @invariant()
        def bounded_equivalence_probe(self) -> None:
            """A12's per-step probe: what the rule touched plus a fixed
            sample of pool members. `path_counts` covers every ancestor of
            each probed address in one call."""

            binary, patricia = self.binary, self.patricia
            assert binary.hot_ip_count == patricia.hot_ip_count == len(self.hot)
            assert list(binary.hot_ips()) == list(patricia.hot_ips())

            probes: list[Address] = list(probe_sample)
            if self.last_ip is not None and self.last_ip not in probes:
                probes.append(self.last_ip)
            for ip in probes:
                assert binary.is_hot(ip) == patricia.is_hot(ip), ip
                assert binary.path_counts(ip) == patricia.path_counts(ip), ip
                assert binary.longest_match(ip) == patricia.longest_match(ip), ip
                assert self._path_metadata(binary, ip) == self._path_metadata(patricia, ip), ip
            for prefix in self.touched:
                assert binary.hot_count(prefix) == patricia.hot_count(prefix), prefix
                binary_local = dict(binary.local_metadata(prefix))
                assert binary_local == dict(patricia.local_metadata(prefix)), prefix

        # -- boundary checks (A12: at @initialize and teardown, not per step) --

        @initialize()
        def full_contract_at_start(self) -> None:
            self._full_contract()

        def teardown(self) -> None:
            self._full_contract()

        @staticmethod
        def _path_metadata(
            trie: BinaryTrie | PatriciaTrie, ip: Address
        ) -> list[tuple[Prefix, dict[str, object]]]:
            return [(prefix, dict(m)) for prefix, m in trie.path_metadata(ip)]

        def _full_contract(self) -> None:
            expected = expected_hot_counts(self.hot)
            ordered = sorted(self.hot, key=lambda ip: ip.value)
            declared = sorted(self.metadata, key=_by_depth)

            for trie in (self.binary, self.patricia):
                assert_hot_count_consistent(trie)
                assert_no_negative_counts(trie)
                assert_attribute_records_consistent(trie, self.hot)

                assert trie.hot_ip_count == len(self.hot)
                assert list(trie.hot_ips()) == ordered
                assert trie.hot_count(root_prefix(family)) == len(self.hot)

                for ip in pool_members:
                    assert trie.is_hot(ip) is (ip in self.hot), ip
                    counts = trie.path_counts(ip)
                    assert len(counts) == bits + 1
                    want_counts = tuple(
                        expected.get(ancestor_of(ip, length), 0) for length in range(bits + 1)
                    )
                    assert counts == want_counts, ip
                    match = trie.longest_match(ip)
                    if self.hot:
                        deepest = max(length for length in range(bits + 1) if counts[length] > 0)
                        assert match == ancestor_of(ip, deepest), ip
                    else:
                        assert match is None, ip
                    want_path = [
                        (prefix, self.metadata[prefix])
                        for prefix in declared
                        if covers(prefix, ancestor_of(ip, bits))
                    ]
                    assert self._path_metadata(trie, ip) == want_path, ip

                for prefix, entry in self.metadata.items():
                    assert dict(trie.local_metadata(prefix)) == entry, prefix
                for prefix in self.touched:
                    if prefix not in self.metadata:
                        assert dict(trie.local_metadata(prefix)) == {}, prefix

                for min_length in min_lengths:
                    reported = list(trie.logical_prefixes(min_length=min_length))
                    assert len(set(reported)) == len(reported)
                    want_logical = {
                        (prefix, count)
                        for prefix, count in expected.items()
                        if prefix.length >= min_length
                    }
                    assert set(reported) == want_logical, min_length

            from_binary = set(logical_prefixes(self.binary.edges()))
            from_patricia = set(logical_prefixes(self.patricia.edges()))
            assert from_binary == from_patricia
            assert from_patricia == set(self.patricia.logical_prefixes())

    return TrieMachine


def _machine_settings(family: AddressFamily) -> settings:
    max_examples, steps = _SETTINGS[family]
    return settings(
        max_examples=max_examples,
        stateful_step_count=steps,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )


TrieMachine = build_machine(AddressFamily.IPV4)
TrieMachineV6 = build_machine(AddressFamily.IPV6)

TestTrieMachine = TrieMachine.TestCase
TestTrieMachine.settings = _machine_settings(AddressFamily.IPV4)

TestTrieMachineV6 = TrieMachineV6.TestCase
TestTrieMachineV6.settings = _machine_settings(AddressFamily.IPV6)
