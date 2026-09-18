"""Hypothesis strategies for addresses, prefixes and hot-IP operation streams.

Spec: section 3 (two address families), section 8 (a path is the bits of an
address; a /24 is the first 24 bits, an IP is /32), section 10 and section 11
(the two operations a stream is made of), section 35 (IPv6 readiness: every
strategy is per family), section 42 (the `10.20.30.0/24` example the
clustered addresses are drawn around).

ADR-0012 decision 4 names the three strategies -- `addresses(family)`,
`prefixes(family)`, `hot_ip_streams(family)` -- and decision 1 restricts the
testkit to `hammertime-core` and `hypothesis`; nothing here imports from a
service.

Design of the strategies, stated because nothing pins it:

* `addresses` mixes uniform draws over the whole family with draws
  clustered into a few /24-sized (IPv4) or /120-sized (IPv6) networks, so
  that random streams actually share ancestors -- otherwise a Patricia trie
  is almost always a root fanning straight out to leaves and neither
  compression nor branching gets exercised.
* `prefixes` draws a length biased onto the ones the spec talks about
  (0, 8, 16, 24, the host route, and their IPv6 mirrors from ADR-0012
  decision 1) and masks a clustered address down to it, so prefixes land on
  the paths the addresses take.
* `hot_ip_streams` produces `("add" | "remove", Address)` pairs drawn from a
  small pool, with a remove biased (80 %) onto an address that is HOT at that
  point in the stream, so removal, oscillation and section 11's no-op
  (removing an address that is not held) all occur.
"""

from collections.abc import Mapping
from typing import Final, Literal

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.addressing.prefix import Prefix
from hammertime.testkit.invariants import ancestor_of
from hypothesis import strategies as st

Operation = Literal["add", "remove"]
HotIpOp = tuple[Operation, Address]

OPERATIONS: Final[tuple[Operation, ...]] = ("add", "remove")

# 2001:db8::/32 is the documentation prefix; the IPv6 clusters sit under it.
_V6_DOC = 0x2001_0DB8 << 96

# Small networks the clustered draws fall into. IPv4: section 42's
# 10.20.30.0/24, its sibling 10.20.31.0/24 (so the /23 above them branches),
# section 10's 192.168.1.0/24 and its neighbour. IPv6: three /120s, two of
# them siblings for the same reason.
_CLUSTER_BASES: Final[Mapping[AddressFamily, tuple[int, ...]]] = {
    AddressFamily.IPV4: (0x0A14_1E00, 0x0A14_1F00, 0xC0A8_0100, 0xC0A8_0000),
    AddressFamily.IPV6: (_V6_DOC, _V6_DOC | 0x100, _V6_DOC | (1 << 64)),
}


def _interesting_lengths(family: AddressFamily) -> tuple[int, ...]:
    bits = family.bit_length
    candidates = {0, 8, 16, 24, bits - 24, bits - 16, bits - 9, bits - 8, bits - 1, bits}
    return tuple(sorted(length for length in candidates if 0 <= length <= bits))


def addresses(family: AddressFamily) -> st.SearchStrategy[Address]:
    """Addresses of `family`: half uniform over the family, half clustered
    into a few small networks so paths share ancestors."""

    limit = (1 << family.bit_length) - 1
    uniform = st.integers(min_value=0, max_value=limit)
    bases = st.sampled_from(_CLUSTER_BASES[family])
    hosts = st.integers(min_value=0, max_value=255)
    clustered = st.tuples(bases, hosts).map(lambda pair: pair[0] | pair[1])
    return st.one_of(clustered, uniform).map(lambda value: Address(family=family, value=value))


@st.composite
def prefixes(draw: st.DrawFn, family: AddressFamily) -> Prefix:
    """Prefixes of `family`, with lengths biased onto the spec's boundaries."""

    bits = family.bit_length
    lengths = st.one_of(
        st.sampled_from(_interesting_lengths(family)), st.integers(min_value=0, max_value=bits)
    )
    return ancestor_of(draw(addresses(family)), draw(lengths))


@st.composite
def hot_ip_streams(draw: st.DrawFn, family: AddressFamily, *, max_size: int = 48) -> list[HotIpOp]:
    """A sequence of `("add" | "remove", Address)` operations over a small
    pool of addresses, removes biased onto addresses HOT at that point."""

    pool = draw(st.lists(addresses(family), min_size=1, max_size=12))
    size = draw(st.integers(min_value=0, max_value=max_size))
    stream: list[HotIpOp] = []
    hot: list[Address] = []
    for _ in range(size):
        kind: Operation = draw(st.sampled_from(OPERATIONS))
        if kind == "remove" and hot and draw(st.integers(min_value=0, max_value=9)) < 8:
            ip = draw(st.sampled_from(hot))
        else:
            ip = draw(st.sampled_from(pool))
        stream.append((kind, ip))
        if kind == "add":
            if ip not in hot:
                hot.append(ip)
        elif ip in hot:
            hot.remove(ip)
    return stream
