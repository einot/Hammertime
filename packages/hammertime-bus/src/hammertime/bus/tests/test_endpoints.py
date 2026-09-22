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

Amendment 4 (2026-09-22) added two italic paragraphs after the `bus_endpoints`
paragraph, each pinned by the class named after it:

* "An `@` outside the authority" (rulings R8 and S2): "an entry in which
  `urlsplit` leaves an `@` in the path, query or fragment is rendered as the
  fixed string `<unparseable>`, whatever its authority holds", with five
  "Additional examples, which tests pin" and "The seven examples above are
  unchanged" (`TestAtSignOutsideTheAuthority`; `PINNED_EXAMPLES` is untouched).
* "The same URL reaches a record by a second path, closed by
  `validate_bus_url`" (ruling S2): "`validate_bus_url(url: str) -> None`,
  re-exported from `hammertime.bus`, which normalises the entry as nats-py
  does (an entry containing `://` as given; otherwise `nats://<entry>`) and
  raises `ValueError` when (i) `urlsplit` refuses it, (ii) `SplitResult.port`
  raises -- the very cast nats-py performs -- or (iii) an `@` is left outside
  the authority (the case above); the message names none of the entry's
  text", and "no value that connected before is refused now"
  (`TestValidateBusUrl`).

Amendment 5 (2026-09-22) added one helper to decision 3's block and qualified
one sentence of the validator paragraph:

* Ruling 4: `split_bus_servers(servers: str) -> list[str]` -- "on `,`, each
  entry stripped, empties dropped; the rule this ADR ties to
  `NatsBus.__init__` -- re-exported from `hammertime.bus` and listed in
  decision 3's block. It is the one definition: `NatsBus.__init__`,
  `bus_endpoints`'s `str` form, both services' `load_settings` and the
  provisioner's `--servers` type function call it" (`TestSplitBusServers`,
  and `TestReExport` for the re-export).
* Ruling 6: "no value that connected before is refused now" is qualified --
  "with one deliberate exception, which governs ...: rule (iii) refuses a
  value nats-py would accept, such as `nats://user:pass@host:4222/a@b`
  (nats-py ignores the path)" (`TestValidateBusUrl`).

Assumption for `TestReExport`'s `__all__` case: the ADR says only
"re-exported from `hammertime.bus`" and never names `__all__`; that the three
helpers are in the package's public list is this test's reading of "public".
A missing `__all__` would be a gap in `hammertime.bus.__init__`, not a
property of any one of the three.

Needle note for the `/`, `?`, `#` cases of `test_no_userinfo_fragment_survives`:
the ruled rendering `<unparseable>` itself contains the substring `pa`, so the
password is looked for as its whole split halves (`pa/ss`, `pa?ss`, `pa#ss`)
and as `ss`, never as the bare `pa` that the fixed string would always match.
"""

from __future__ import annotations

import hammertime.bus
import pytest
from hammertime.bus import bus_endpoints as reexported_bus_endpoints
from hammertime.bus import split_bus_servers as reexported_split_bus_servers
from hammertime.bus import validate_bus_url as reexported_validate_bus_url
from hammertime.bus.nats import bus_endpoints, split_bus_servers, validate_bus_url
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

# Decision 3 as amended 2026-09-22 (Amendment 4 rulings R8 and S2), "Additional
# examples, which tests pin", in the ADR's order. "The seven examples above are
# unchanged."
AMENDMENT_4_EXAMPLES: list[tuple[str, list[str]]] = [
    ("nats://user:pa/ss@nats:4222", ["<unparseable>"]),
    ("nats://user:pa?ss@nats:4222", ["<unparseable>"]),
    ("nats://user:pa#ss@nats:4222", ["<unparseable>"]),
    ("nats://user:p@/ss@nats:4222", ["<unparseable>"]),
    ("nats://nats:4222/", ["nats://nats:4222"]),
]

# `validate_bus_url` (Amendment 4 ruling S2): "no value that connected before
# is refused now" -- every single-entry input of the seven pinned examples,
# plus a bare scheme-less host, which nats-py normalises to `nats://nats`.
ACCEPTED_URLS: list[str] = [
    "nats://nats:4222",
    "nats://user:s3cret@nats:4222",
    "nats://s3cret-token@nats:4222",
    "tls://user:s3cret@[::1]:4222/?x=1",
    "user:s3cret@nats:4222",
    "nats",
]

# The three refusals of ruling S2: (i) `urlsplit` refuses it; (ii)
# `SplitResult.port` raises; (iii) an `@` is left outside the authority. The
# userinfo tokens (`usr7`, `svc7`, `s3cr`, `et@`, `s3cret`) are chosen so that
# none is a substring of the fixed refusal text or of ordinary English.
REFUSED_URLS: list[str] = [
    "nats://[::1",
    "nats://usr7:s3cr/et@nats:4222",
    "nats://usr7:s3cr?et@nats:4222",
    "nats://usr7:s3cr#et@nats:4222",
    "nats://usr7:s3cr@/et@nats:4222",
    "nats://usr7:s3cret/0",
    "nats://svc7:s3cr@x/et@nats:4222",
]

# Every token of the refused entries above that a message must not echo -- the
# username, each half of the password, the whole password, the host: "the
# message names none of the entry's text". Paired below with only the entries
# that actually contain them: a needle absent from the entry proves nothing.
REFUSED_URL_FRAGMENTS: tuple[str, ...] = (
    "usr7",
    "svc7",
    "s3cr",
    "et@",
    "s3cret",
    "::1",
    "nats:4222",
)
REFUSED_URL_ECHO_PAIRS: list[tuple[str, str]] = [
    (url, fragment) for url in REFUSED_URLS for fragment in REFUSED_URL_FRAGMENTS if fragment in url
]

# Decision 3 as amended 2026-09-22 (Amendment 5 ruling 4): the `str` form's
# split, on its own. The first is the ADR's own list example, whose entries are
# *not* reduced here -- this is the split, not `bus_endpoints`.
SPLIT_EXAMPLES: list[tuple[str, list[str]]] = [
    ("nats://a:4222, nats://u:p@b:4222,", ["nats://a:4222", "nats://u:p@b:4222"]),
    ("", []),
    (" , ,", []),
    ("  nats://nats:4222 \t", ["nats://nats:4222"]),
]


class TestReExport:
    def test_hammertime_bus_re_exports_the_same_function(self) -> None:
        # Decision 3: "it is re-exported from `hammertime.bus`".
        assert reexported_bus_endpoints is bus_endpoints

    def test_hammertime_bus_re_exports_validate_bus_url_too(self) -> None:
        # Ruling S2: "`validate_bus_url(url: str) -> None`, re-exported from
        # `hammertime.bus`".
        assert reexported_validate_bus_url is validate_bus_url

    def test_hammertime_bus_re_exports_split_bus_servers_too(self) -> None:
        # Ruling 4: "re-exported from `hammertime.bus`" and "the one
        # definition" -- `hammertime.bus.split_bus_servers` (the left-hand
        # alias) is `hammertime.bus.nats.split_bus_servers` (the right-hand
        # one), the same object and not two copies of the same three-line rule.
        assert reexported_split_bus_servers is split_bus_servers

    @pytest.mark.parametrize("name", ["bus_endpoints", "validate_bus_url", "split_bus_servers"])
    def test_the_re_exported_helper_is_a_public_name_of_the_package(self, name: str) -> None:
        # The module docstring's assumption: "re-exported from
        # `hammertime.bus`" is read as being in the package's public list.
        assert name in hammertime.bus.__all__


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


class TestAtSignOutsideTheAuthority:
    """Decision 3 as amended (Amendment 4 rulings R8 and S2): "`urlsplit` ends the
    authority at the first `/`, `?` or `#` after the `//` ..., so a userinfo
    containing one of them unencoded leaves its `@` -- and a fragment of the
    password -- outside the authority, where the last-`@` rule cannot see it".
    "Ruled: an entry in which `urlsplit` leaves an `@` in the path, query or
    fragment is rendered as the fixed string `<unparseable>`, whatever its
    authority holds." Assumption 72."""

    @pytest.mark.parametrize(("servers", "expected"), AMENDMENT_4_EXAMPLES)
    def test_additional_example(self, servers: str, expected: list[str]) -> None:
        # The five "Additional examples, which tests pin", each exact.
        assert bus_endpoints(servers) == expected

    def test_the_seven_original_examples_are_unchanged(self) -> None:
        # "The seven examples above are unchanged."
        for servers, expected in PINNED_EXAMPLES:
            assert bus_endpoints(servers) == expected

    def test_an_at_sign_inside_and_outside_the_authority_is_unparseable(self) -> None:
        # Assumption 72: "an entry with an `@` inside the authority *and* one
        # outside (`nats://user:p@/ss@nats:4222`) is `<unparseable>` too,
        # although the last-`@` rule alone would render `nats://` for it,
        # because the outside `@` is the signal".
        assert bus_endpoints("nats://user:p@/ss@nats:4222") == ["<unparseable>"]
        assert bus_endpoints("nats://user:p@/ss@nats:4222") != ["nats://"]

    def test_a_path_without_an_at_sign_is_merely_dropped(self) -> None:
        # "`nats://nats:4222/` -> `nats://nats:4222` (a path without an `@` is
        # still merely dropped)".
        assert bus_endpoints("nats://nats:4222/") == ["nats://nats:4222"]
        assert bus_endpoints("nats://nats:4222/some/path?x=1#frag") == ["nats://nats:4222"]

    def test_the_rendering_carries_nothing_of_the_entry(self) -> None:
        # "rendered as the fixed string `<unparseable>`, whatever its
        # authority holds": neither the authority (`user:pa`, which the rule
        # as written would have rendered) nor the host after the stray `@`.
        rendered = bus_endpoints("nats://user:pa/ss@nats:4222")

        assert rendered == ["<unparseable>"]
        assert "user" not in rendered[0]
        assert "nats:4222" not in rendered[0]

    def test_a_list_entry_is_ruled_the_same_way(self) -> None:
        # One `<unparseable>` per bad entry, alongside a good one.
        assert bus_endpoints(["nats://user:pa?ss@nats:4222", "nats://a:4222"]) == [
            "<unparseable>",
            "nats://a:4222",
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


class TestSplitBusServers:
    """Decision 3 as amended (Amendment 5 ruling 4): "`hammertime.bus.nats` gains
    `split_bus_servers(servers: str) -> list[str]` -- on `,`, each entry stripped,
    empties dropped; the rule this ADR ties to `NatsBus.__init__` -- re-exported
    from `hammertime.bus` and listed in decision 3's block", with the docstring
    of that block: "`HAMMERTIME_BUS_BROKERS` / `--servers` -> entries: split on
    `,`, each stripped, empties dropped". The `bus_endpoints` paragraph makes it
    the one definition: "that split is the public `hammertime.bus.split_bus_servers`
    ..., the one definition `NatsBus.__init__`, `bus_endpoints`, both services'
    `load_settings` and the provisioning tool all call"."""

    @pytest.mark.parametrize(("servers", "expected"), SPLIT_EXAMPLES)
    def test_example(self, servers: str, expected: list[str]) -> None:
        assert split_bus_servers(servers) == expected

    def test_the_split_does_not_reduce_an_entry(self) -> None:
        # It splits and nothing else: this is not `bus_endpoints`, and the
        # entries are what `NatsBus.__init__` hands `nats.connect` "as given"
        # (decision 2), userinfo included.
        assert split_bus_servers("nats://a:4222, nats://u:p@b:4222,") == [
            "nats://a:4222",
            "nats://u:p@b:4222",
        ]

    def test_the_result_is_a_list_of_strings(self) -> None:
        result = split_bus_servers("nats://a:4222,nats://b:4222")

        assert isinstance(result, list)
        assert all(isinstance(entry, str) for entry in result)

    @pytest.mark.parametrize(("servers", "expected"), SPLIT_EXAMPLES)
    def test_bus_endpoints_agrees_with_the_split(self, servers: str, expected: list[str]) -> None:
        # "the one definition ... `bus_endpoints` ... all call": the `str` form
        # of `bus_endpoints` is this split followed by the per-entry reduction,
        # so the two agree entry for entry.
        assert bus_endpoints(servers) == bus_endpoints(split_bus_servers(servers))
        assert len(bus_endpoints(servers)) == len(expected)


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
            # Amendment 4 ruling R8: the password split by `urlsplit` at an
            # unencoded `/`, `?` or `#`. The bare `pa` is not a needle here
            # because the ruled rendering `<unparseable>` contains it (module
            # docstring); the halves around the separator, `ss` and `user` are.
            pytest.param(
                "nats://user:pa/ss@nats:4222",
                ("pa/ss", "ss", "user", "@"),
                id="slash-in-password",
            ),
            pytest.param(
                "nats://user:pa?ss@nats:4222",
                ("pa?ss", "ss", "user", "@"),
                id="question-mark-in-password",
            ),
            pytest.param(
                "nats://user:pa#ss@nats:4222",
                ("pa#ss", "ss", "user", "@"),
                id="hash-in-password",
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


class TestValidateBusUrl:
    """Decision 3 as amended (Amendment 4 ruling S2): "The same URL reaches a record
    by a second path, closed by `validate_bus_url`" -- nats-py's own parse of
    `nats://user:pa/ss@nats:4222` raises a `ValueError` echoing the port token,
    chained into `start_failed`. "So `hammertime.bus.nats` gains
    `validate_bus_url(url: str) -> None` ... which normalises the entry as
    nats-py does ... and raises `ValueError` when (i) `urlsplit` refuses it,
    (ii) `SplitResult.port` raises -- the very cast nats-py performs -- or (iii)
    an `@` is left outside the authority ...; the message names none of the
    entry's text." "The validator checks only what nats-py's own parse would
    refuse plus (iii), so no value that connected before is refused now -- with
    one deliberate exception, which governs (qualified 2026-09-22, Amendment 5
    ruling 6): rule (iii) refuses a value nats-py would accept, such as
    `nats://user:pass@host:4222/a@b` (nats-py ignores the path), because an `@`
    outside the authority is the signature of an unencoded password separator
    that (ii) does not catch when the authority happens to parse ... Rule (iii)
    is the rule; the sentence is read with that exception." Assumption 74."""

    @pytest.mark.parametrize("url", ACCEPTED_URLS)
    def test_a_url_that_connected_before_is_accepted(self, url: str) -> None:
        # `validate_bus_url` is declared `-> None`: "accepted" is exactly that
        # the call raises nothing (its result is not bound or compared).
        validate_bus_url(url)

    def test_a_scheme_less_entry_is_normalised_as_nats_py_does(self) -> None:
        # "an entry containing `://` as given; otherwise `nats://<entry>`":
        # `user:s3cret@nats:4222` is read as `nats://user:s3cret@nats:4222`,
        # whose port is `4222`, and is accepted -- not read as scheme `user`.
        validate_bus_url("user:s3cret@nats:4222")
        validate_bus_url("nats:4222")

    @pytest.mark.parametrize("url", REFUSED_URLS)
    def test_a_url_nats_py_would_refuse_or_echo_is_a_value_error(self, url: str) -> None:
        with pytest.raises(ValueError):
            validate_bus_url(url)

    def test_an_unbalanced_bracket_is_refused(self) -> None:
        # (i) "`urlsplit` refuses it" -- `ValueError: Invalid IPv6 URL`.
        with pytest.raises(ValueError):
            validate_bus_url("nats://[::1")

    def test_a_port_that_is_not_an_integer_is_refused(self) -> None:
        # (ii) "`SplitResult.port` raises -- the very cast nats-py performs":
        # the ADR-0009 A12 item 4 hazard, `redis://user:secret/0`, in its NATS
        # form. There is no stray `@` here, so only the port cast can refuse it.
        with pytest.raises(ValueError):
            validate_bus_url("nats://user:secret/0")

    @pytest.mark.parametrize(
        "url",
        [
            "nats://user:pa/ss@nats:4222",
            "nats://user:pa?ss@nats:4222",
            "nats://user:pa#ss@nats:4222",
            "nats://user:p@/ss@nats:4222",
            "nats://svc:pa@x/ss@nats:4222",
            # Amendment 5 ruling 6's own example: the deliberate exception to
            # "no value that connected before is refused now". Its authority
            # parses and nats-py ignores the path, so only (iii) refuses it --
            # and (iii) governs.
            "nats://user:pass@host:4222/a@b",
        ],
    )
    def test_an_at_sign_outside_the_authority_is_refused(self, url: str) -> None:
        # (iii) "an `@` is left outside the authority (the case above)" -- the
        # R8 signature, refused before nats-py can echo anything of it.
        with pytest.raises(ValueError):
            validate_bus_url(url)

    @pytest.mark.parametrize(("url", "fragment"), REFUSED_URL_ECHO_PAIRS)
    def test_the_message_names_none_of_the_entrys_text(self, url: str, fragment: str) -> None:
        # "the message names none of the entry's text": not the username, not
        # the password or any half of it, not the host, not the whole value.
        assert fragment in url  # a needle absent from the entry would prove nothing
        with pytest.raises(ValueError) as excinfo:
            validate_bus_url(url)

        message = str(excinfo.value)
        assert fragment not in message, f"{fragment!r} echoed in {message!r}"
        assert url not in message

    def test_the_message_does_not_echo_the_port_token(self) -> None:
        # The chained text ruling S2 exists to keep out of `start_failed`:
        # `"Port could not be cast to integer value as 's3cr'"`.
        with pytest.raises(ValueError) as excinfo:
            validate_bus_url("nats://usr7:s3cr/et@nats:4222")

        assert "'s3cr'" not in str(excinfo.value)
        assert "s3cr" not in str(excinfo.value)


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
