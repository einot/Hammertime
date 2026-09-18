"""Validation of `HAMMERTIME_REDIS_URL`, by the driver's own parser.

Spec: section 47

ADR-0009 decision 2 (as corrected by Amendment 3, item A12): a value a
client library would refuse when the client is built from it is invalid
*configuration*, so `load_settings` must refuse it first -- otherwise the
refusal happens inside `start()` and is reported as `start_failed` and exit
1, telling an orchestrator to keep retrying something that cannot come
right.

The only property that matters is "accepted here iff accepted by
`redis.asyncio.Redis.from_url`", which only the driver's parser can
promise. So this module adds no rule of its own -- no scheme list, no host
requirement, no `urllib` pre-check -- and is deliberately permissive:
`redis://` alone is valid (the pool defaults host and port), and a
non-integer path db is silently ignored, exactly as the driver does it. Any
extra rule would be a second definition of validity that the driver does
not share.

`parse_url` is a module-level function of `redis.asyncio.connection`, not
exported from any `redis` `__init__`; isolating that import here means one
line changes if a later redis-py moves it.
"""

from redis.asyncio.connection import parse_url

#: The environment variable this validates, and the default `name` reported.
REDIS_URL_ENV = "HAMMERTIME_REDIS_URL"


def validate_redis_url(value: str, *, name: str = REDIS_URL_ENV) -> None:
    """Raise `ValueError` naming `name` iff `Redis.from_url(value)` would.

    Returns `None` and never alters `value`: the URL stays the single
    representation the client is later built from, so there is no second
    construction path that can drift from the first.

    The driver's exception is *not* chained (A12 item 4). `parse_url` can
    echo a credential -- for `redis://user:secret/0`, with the `@`
    forgotten, `urlparse` reads `secret` as the port and reports "Port could
    not be cast to integer value as 'secret'" -- and a chained cause would
    carry that into any traceback. The accepted grammar in the message is
    the substitute for saying which part was bad.

    Anything `parse_url` raises that is not a `ValueError` propagates
    unchanged: hiding an unexpected exception type behind a configuration
    message would mask a driver bug.
    """
    try:
        parse_url(value)
    except ValueError:
        raise ValueError(
            f"{name} must be a Redis URL (redis://, rediss:// or unix://) with "
            "well-formed query parameters; the value is not repeated here "
            "because it may carry a password"
        ) from None
