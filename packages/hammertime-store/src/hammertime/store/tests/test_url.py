"""`HAMMERTIME_REDIS_URL` is valid iff the redis driver would build a client from it.

Spec: section 47.1 step 3 (an invalid configuration -- "including a
connection URL the client library would refuse to build a client from" --
is rejected before any network connection is opened), section 47.5 (exit
status 2 is for a configuration rejected "before any connection was made"),
section 47.7 (no record of any event may carry "the userinfo of a store
URL").

ADR-0009 Amendment 3 item A12 is the contract under test here:

* item 1 -- the check is one shared helper,
  `hammertime.store.validate_redis_url(value, *, name="HAMMERTIME_REDIS_URL")
  -> None`, re-exported from `hammertime.store`, called by each service's
  `load_settings`; it returns nothing and never alters `value`;
* item 2 -- what counts as valid is "exactly what the driver accepts": the
  helper adds no scheme list, no host requirement and no `urllib.parse`
  pre-check of its own, so `validate_redis_url(v)` raises `ValueError` iff
  `redis.asyncio.Redis.from_url(v)` would raise `ValueError` while building
  the client. That equivalence is asserted directly in
  `TestAgreementWithTheDriver` -- it is the property the whole design rests
  on, and it is why the permissive cases below (`redis://` alone, a
  non-integer path db) are *accepted* on purpose;
* item 4 -- the error is a `ValueError` whose message names the variable
  (`name`) and repeats no part of the value: not the whole URL, not its
  userinfo, host, port, path or query, and not a redacted form. The driver's
  own exception is not chained (`raise ... from None`), because for a URL
  with a forgotten `@` the parser's message echoes the password as a "port".
  Both halves are asserted, the second through the formatted traceback and
  not only through `str(exc)`.

The exact wording of the message is deliberately *not* asserted: A12 records
it as recommended text, not pinned ("Tests assert the variable name is
present and no component of a distinctive test value is").

No connection is opened anywhere in this file. `Redis.from_url` parses the
URL and builds a client at construction; the clients built in
`TestAgreementWithTheDriver` are closed with `aclose()` and never pinged.
"""

from __future__ import annotations

import traceback

import pytest
import redis.asyncio
from hammertime.store import validate_redis_url

# A12 item 2, "Accepted". `redis://` alone is valid because the pool defaults
# the host and the port; `redis://host/abc` is valid because the driver
# silently ignores a non-integer path db.
ACCEPTED = [
    "redis://localhost:6379/0",
    "rediss://user:pw@host:6380/1",
    "unix:///tmp/redis.sock?db=0",
    "redis://",
    "redis://host/abc",
]

# A12 item 2, "Rejected": no scheme (empty, whitespace-only, bare host:port),
# a scheme the driver does not serve, a query parameter it cannot cast, and a
# port `urlparse` itself refuses.
REJECTED = [
    "",
    "   ",
    "localhost:6379",
    "http://localhost:6379/0",
    "redis://localhost:6379/0?db=notanumber",
    "redis://host:notaport/0",
]

# A URL carrying a password in every position a message could leak it from.
CREDENTIALED = "http://alice:s3cret-pw@db.internal:6379/7?db=x"

# A12 item 4's port-echo case: the `@` is forgotten, so `urlparse` reads the
# password as the port and the driver's own message quotes it.
FORGOTTEN_AT_SIGN = "redis://alice:s3cret-pw/0"

PASSWORD = "s3cret-pw"


def _rejects(value: str) -> bool:
    """Whether the helper refuses `value`."""
    try:
        validate_redis_url(value)
    except ValueError:
        return True
    return False


async def _driver_rejects(value: str) -> bool:
    """Whether `Redis.from_url` refuses `value` at construction.

    Building the client opens no connection (A12: `from_url` parses the URL
    on the way to `ConnectionPool.from_url`). The client is closed rather
    than pinged.
    """
    try:
        client = redis.asyncio.Redis.from_url(value)
    except ValueError:
        return True
    await client.aclose()
    return False


class TestAcceptedValues:
    """A12 item 2: what the driver builds a client from, the helper accepts."""

    @pytest.mark.parametrize(
        "value",
        ACCEPTED,
        ids=["tcp", "tls-with-userinfo", "unix-socket", "scheme-only", "non-integer-path-db"],
    )
    def test_an_accepted_value_returns_without_raising(self, value: str) -> None:
        # The contract is "raise `ValueError` iff the driver would": returning
        # normally *is* the acceptance. The helper returns nothing.
        validate_redis_url(value)

    def test_a_custom_name_does_not_change_what_is_accepted(self) -> None:
        # `name` only names the variable in the message (A12 item 1).
        validate_redis_url("redis://localhost:6379/0", name="OTHER_VAR")


class TestRejectedValues:
    """A12 item 2: what the driver refuses, the helper refuses -- as `ValueError`."""

    @pytest.mark.parametrize(
        "value",
        REJECTED,
        ids=[
            "empty",
            "whitespace-only",
            "no-scheme",
            "unsupported-scheme",
            "uncastable-query-parameter",
            "non-numeric-port",
        ],
    )
    def test_a_rejected_value_raises_value_error(self, value: str) -> None:
        with pytest.raises(ValueError):
            validate_redis_url(value)


class TestTheErrorNamesTheVariable:
    """A12 item 4: "the message contains the variable name (`name`, default
    `HAMMERTIME_REDIS_URL`)"."""

    @pytest.mark.parametrize("value", REJECTED)
    def test_the_default_name_is_the_environment_key(self, value: str) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_REDIS_URL"):
            validate_redis_url(value)

    @pytest.mark.parametrize("value", REJECTED)
    def test_a_supplied_name_is_the_one_reported(self, value: str) -> None:
        with pytest.raises(ValueError, match="OTHER_VAR"):
            validate_redis_url(value, name="OTHER_VAR")


class TestTheErrorRepeatsNoPartOfTheValue:
    """A12 item 4 and section 47.7: the URL may carry a password, so no
    component of it -- and no redacted form of it -- reaches the message."""

    @pytest.mark.parametrize(
        "component",
        ["alice", PASSWORD, "db.internal", "6379", "/7", "db=x", CREDENTIALED],
        ids=["user", "password", "host", "port", "path", "query", "whole-value"],
    )
    def test_no_component_of_a_credentialed_url_appears(self, component: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_redis_url(CREDENTIALED)

        assert component not in str(excinfo.value)

    def test_the_forgotten_at_sign_case_keeps_the_password_out_of_the_message(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_redis_url(FORGOTTEN_AT_SIGN)

        assert PASSWORD not in str(excinfo.value)

    def test_the_forgotten_at_sign_case_keeps_the_password_out_of_the_traceback(self) -> None:
        # A12 item 4: the driver's exception is *not* chained (`from None`),
        # because for this value the parser's own message quotes the password
        # as the port it could not cast. A chained cause would put it into any
        # rendered traceback, which is what this asserts against.
        with pytest.raises(ValueError) as excinfo:
            validate_redis_url(FORGOTTEN_AT_SIGN)

        rendered = "".join(traceback.format_exception(excinfo.value))
        assert PASSWORD not in rendered

    def test_the_variable_name_is_still_reported_for_the_forgotten_at_sign_case(self) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_REDIS_URL"):
            validate_redis_url(FORGOTTEN_AT_SIGN)


class TestAgreementWithTheDriver:
    """A12 item 2: "accepted here iff accepted there".

    This is the whole point of the helper -- it performs no check of its own
    beyond the driver's parser -- so it is asserted as an equivalence over
    every value above rather than as two separate lists.
    """

    @pytest.mark.parametrize("value", ACCEPTED + REJECTED)
    async def test_the_helper_refuses_exactly_what_the_driver_refuses(self, value: str) -> None:
        assert _rejects(value) == await _driver_rejects(value)

    @pytest.mark.parametrize("value", [CREDENTIALED, FORGOTTEN_AT_SIGN])
    async def test_the_credentialed_examples_are_refused_by_the_driver_too(
        self, value: str
    ) -> None:
        # The two values the no-credential assertions above are built from are
        # only meaningful if the driver really would refuse them.
        assert await _driver_rejects(value) is True
        assert _rejects(value) is True
