"""`hammertime.bus.nats.bus_endpoints`: the one reduction of a bus server list a record may carry.

Spec: section 47.7 ("No record -- of any event -- may contain a credential"),
which ADR-0013 Amendment 2 ruling 2 extends to the userinfo of a bus URL;
sections 19, 20, 32, 33 are `hammertime.bus.nats`'s own citations.

Written from ADR-0013 decision 3's `bus_endpoints` paragraph (added
2026-09-21, Amendment 2), decision 10's `HAMMERTIME_BUS_BROKERS` row and
Amendment 2 rulings 2-3 with assumptions 42-45; `nats.py` was not read. The
sentences pinned here:

* Signature and re-export: `def bus_endpoints(servers: str | Iterable[str])
  -> list[str]` in `hammertime.bus.nats`, "re-exported from `hammertime.bus`".
* Splitting: "A `str` argument is split exactly as `NatsBus.__init__` splits
  `HAMMERTIME_BUS_BROKERS` -- on `,`, entries stripped, empties dropped -- and
  an iterable is taken entry by entry, each stripped."
* Scheme-less entries: "An entry containing no `://` is read as
  `nats://<entry>` first".
* The reduction: "the result is `<scheme>://` followed by the part of the
  authority after its **last** `@` -- the whole authority when there is no
  `@`. Nothing to the left of that `@` reaches the output: not a password,
  and not a username either, because nats-py reads a username with no
  password as an auth token ... Path, query and fragment are dropped".
* Never raises: "The function never raises: an entry `urlsplit` refuses (an
  unbalanced `[`, `ValueError: Invalid IPv6 URL`) is rendered as the fixed
  string `<unparseable>`, with nothing derived from the entry; an empty list
  is `[]`."
* The seven "Examples, which tests pin", each exact.

Not pinned, because the ADR does not rule on them: scheme/host casing, and
what a whitespace-only *list* entry becomes (the `str` form drops empties;
the iterable form only says "each stripped").

Assumption: the single-entry property test reads "an iterable is taken entry
by entry" as one result element per input element, so `bus_endpoints([s])`
has length 1 for any `s`; the element's *value* is not asserted there.
"""

from __future__ import annotations

import pytest
from hammertime.bus import bus_endpoints as reexported_bus_endpoints
from hammertime.bus.nats import bus_endpoints
from hypothesis import given, settings
from hypothesis import strategies as st

# Decision 3, "Examples, which tests pin", in the ADR's order.
PINNED_EXAMPLES: list[tuple[str, list[str]]] = [
    ("nats://nats:4222", ["nats://nats:4222"]),
    ("nats://user:s3cret@nats:4222", ["nats://nats:4222"]),
    ("nats://s3cret-token@nats:4222", ["nats://nats:4222"]),
    ("tls://user:s3cret@[::1]:4222/?x=1", ["tls://[::1]:4222"]),
    ("user:s3cret@nats:4222", ["nats://nats:4222"]),
    ("nats://a:4222, nats://u:p@b:4222,", ["nats://a:4222", "nats://b:4222"]),
    ("nats://[::1", ["<unparseable>"]),
]


class TestReExport:
    def test_hammertime_bus_re_exports_the_same_function(self) -> None:
        # Decision 3: "it is re-exported from `hammertime.bus`".
        assert reexported_bus_endpoints is bus_endpoints


class TestPinnedExamples:
    """Decision 3's seven examples, each exact."""

    @pytest.mark.parametrize(("servers", "expected"), PINNED_EXAMPLES)
    def test_example(self, servers: str, expected: list[str]) -> None:
        assert bus_endpoints(servers) == expected

    def test_unparseable_entry_is_the_fixed_string_and_nothing_else(self) -> None:
        # "rendered as the fixed string `<unparseable>`, with nothing derived
        # from the entry" -- assumption 44: "The entry count is still visible
        # (one `<unparseable>` per bad entry)".
        assert bus_endpoints(["nats://[::1", "nats://user:s3cret@[::1"]) == [
            "<unparseable>",
            "<unparseable>",
        ]


class TestSplitting:
    """Decision 3: "on `,`, entries stripped, empties dropped"; "an iterable is taken

    entry by entry, each stripped".
    """

    def test_a_list_and_the_equivalent_comma_separated_string_agree(self) -> None:
        as_string = "nats://a:4222, nats://u:p@b:4222,"
        as_list = ["nats://a:4222", " nats://u:p@b:4222"]

        assert bus_endpoints(as_string) == bus_endpoints(as_list)
        assert bus_endpoints(as_list) == ["nats://a:4222", "nats://b:4222"]

    def test_a_tuple_is_an_iterable_too(self) -> None:
        # The signature is `str | Iterable[str]`, not `str | list[str]`.
        assert bus_endpoints(("nats://a:4222", "nats://b:4222")) == [
            "nats://a:4222",
            "nats://b:4222",
        ]

    def test_string_entries_are_stripped(self) -> None:
        assert bus_endpoints("  nats://nats:4222 \t") == ["nats://nats:4222"]

    def test_list_entries_are_stripped(self) -> None:
        assert bus_endpoints(["  nats://nats:4222 \t"]) == ["nats://nats:4222"]

    def test_empty_string_is_an_empty_list(self) -> None:
        assert bus_endpoints("") == []

    def test_empty_list_is_an_empty_list(self) -> None:
        # "an empty list is `[]`".
        assert bus_endpoints([]) == []

    def test_a_string_of_only_separators_and_whitespace_is_an_empty_list(self) -> None:
        # `str` form: "entries stripped, empties dropped".
        assert bus_endpoints(" , ,") == []


class TestNoUserinfoInTheResult:
    """Ruling 2: the rendered text "MUST NOT contain the userinfo of a bus URL";

    decision 3: "Nothing to the left of that `@` reaches the output: not a
    password, and not a username either" (assumption 42).
    """

    @pytest.mark.parametrize(
        ("servers", "needles"),
        [
            pytest.param(
                "nats://user:s3cret@nats:4222", ("s3cret", "user", "@"), id="user-password"
            ),
            pytest.param("nats://s3cret-token@nats:4222", ("s3cret", "tok", "@"), id="token"),
            pytest.param(
                "tls://user:s3cret@[::1]:4222/?x=1",
                ("s3cret", "user", "@", "x=1"),
                id="tls-ipv6-with-query",
            ),
            pytest.param("user:s3cret@nats:4222", ("s3cret", "user", "@"), id="scheme-less"),
            pytest.param(
                "nats://a:4222, nats://u:p@b:4222,",
                ("u:p", "p", "@"),
                id="list-with-password-p",
            ),
        ],
    )
    def test_no_userinfo_fragment_survives(self, servers: str, needles: tuple[str, ...]) -> None:
        joined = " ".join(bus_endpoints(servers))

        for needle in needles:
            assert needle not in joined, f"{needle!r} survived in {joined!r}"

    def test_the_last_at_sign_is_the_boundary(self) -> None:
        # "the part of the authority after its **last** `@`": an `@` inside the
        # password does not move the boundary left.
        assert bus_endpoints("nats://user:p@ss@nats:4222") == ["nats://nats:4222"]


# --- never raises (decision 3: "The function never raises") ---------------------------


@given(entry=st.text())
@settings(deadline=None, max_examples=200)
def test_a_single_list_entry_never_raises_and_yields_one_element(entry: str) -> None:
    # "an iterable is taken entry by entry" -- one in, one out, whatever the
    # text (an entry `urlsplit` refuses becomes `<unparseable>`). The value is
    # deliberately not pinned (casing and whitespace-only entries are not ruled).
    result = bus_endpoints([entry])

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], str)


@given(entries=st.lists(st.text()))
@settings(deadline=None, max_examples=200)
def test_any_list_never_raises_and_returns_a_list_of_strings(entries: list[str]) -> None:
    result = bus_endpoints(entries)

    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)


@given(servers=st.text())
@settings(deadline=None, max_examples=200)
def test_any_string_never_raises_and_returns_a_list_of_strings(servers: str) -> None:
    result = bus_endpoints(servers)

    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)
