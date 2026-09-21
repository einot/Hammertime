"""CLI entry point. Create or reconcile every registered topic's JetStream stream; idempotent.

Spec: section 19, section 32, section 33

    hammertime-provision --servers <urls> [--replicas N] [--timeout S]

ADR-0013 decision 2. JetStream does not create streams on publish, so this
runs once, before the services, as the reference deployment's `provision`
compose service. It connects to `--servers` (comma-separated NATS URLs, the
same shape as `HAMMERTIME_BUS_BROKERS`), retrying an unreachable broker on
ADR-0009 A1's schedule until `--timeout` (default 60 s) expires, then runs
`hammertime.bus.nats.ensure_streams` over `all_topics()` with `--replicas`
replicas (default 1; 3 in a clustered deployment) and logs one
`INFO event=stream_provisioned stream=<name> action=<created|updated|unchanged>`
per stream through `configure_logging("provision", level)`, where `level` is
`HAMMERTIME_LOG_LEVEL` as for the services.

Exit codes: 0 when every stream exists with the declared configuration; 1
when the servers stayed unreachable within `--timeout` or a stream exists
with a different immutable field (`StreamConfigConflictError`: nothing is
changed); 2 on invalid arguments or an invalid `HAMMERTIME_LOG_LEVEL`.

Nothing here knows a stream's configuration: that is
`hammertime.bus.nats.stream_config_for`, so this tool and the services'
startup verification cannot drift apart. Consumers are not provisioned
(decision 5: `Consumer.subscribe()` creates or binds its own durables).
"""

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Sequence

import nats
import nats.errors
import nats.js.errors
from hammertime.bus.nats import (
    CONNECT_TIMEOUT_S,
    TRANSIENT_ERRORS,
    StreamConfigConflictError,
    ensure_streams,
)
from hammertime.bus.topics import all_topics
from hammertime.core.runtime import (
    EXIT_CONFIG_INVALID,
    EXIT_FAILURE,
    EXIT_OK,
    LOG_LEVEL_ENV,
    connect_with_retry,
)
from hammertime.core.telemetry.logging import DEFAULT_LEVEL, configure_logging, get_logger
from nats.aio.client import Client as NATS

#: `configure_logging` name: every record carries `service=provision`.
SERVICE_NAME = "provision"

DEFAULT_REPLICAS = 1
DEFAULT_TIMEOUT_S = 60.0

#: What is retried until `--timeout`. The bus's own transient set (a refused
#: or dropped connection, a request that timed out) plus JetStream answering
#: 503: a server that is up but whose JetStream is not yet serving (still
#: enabling, or a cluster electing its meta-leader) is "unreachable" for the
#: purpose of provisioning, and it becomes reachable without anyone
#: intervening. An authentication failure is deliberately not retried
#: (ADR-0013 assumption 13).
TRANSIENT_PROVISION_ERRORS: tuple[type[BaseException], ...] = (
    *TRANSIENT_ERRORS,
    nats.js.errors.ServiceUnavailableError,
)

logger = logging.getLogger(__name__)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {raw!r}")
    return value


def _server_urls(raw: str) -> list[str]:
    urls = [url.strip() for url in raw.split(",") if url.strip()]
    if not urls:
        raise argparse.ArgumentTypeError("expected at least one NATS URL")
    return urls


def build_parser() -> argparse.ArgumentParser:
    """The argument parser; `parse_args` exits 2 on an invalid invocation."""
    parser = argparse.ArgumentParser(
        prog="hammertime-provision",
        description=(
            "Create or reconcile the JetStream stream of every registered hammertime topic. "
            "Idempotent: run it before the services start, and again after any change to "
            "the topic registry."
        ),
    )
    parser.add_argument(
        "--servers",
        required=True,
        type=_server_urls,
        metavar="URLS",
        help="comma-separated NATS server URLs, e.g. nats://nats:4222",
    )
    parser.add_argument(
        "--replicas",
        type=_positive_int,
        default=DEFAULT_REPLICAS,
        metavar="N",
        help=(
            "stream replica count; 1 for a single server, 3 for a cluster "
            f"(default {DEFAULT_REPLICAS})"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=DEFAULT_TIMEOUT_S,
        metavar="S",
        help=(
            "seconds to keep retrying an unreachable server before exiting 1 "
            f"(default {DEFAULT_TIMEOUT_S:g})"
        ),
    )
    return parser


async def _client_error(error: Exception) -> None:
    """nats-py's error hook: one line, no traceback (see `hammertime.bus.nats`)."""
    logger.warning("nats_client_error error=%s", error)


async def _connect(servers: list[str]) -> NATS:
    """Connect fail-fast: one pool walk, no reconnection.

    nats-py has no separate option for the initial attempt (ADR-0013
    assumption 14), so the client is told not to reconnect at all -- an
    unreachable server surfaces as `NoServersError` after one walk of the
    pool and `connect_with_retry` owns the backoff. `reconnect_time_wait=0`
    drops the client's own 2 s pause between the tries of that walk, which
    would otherwise stretch every attempt past `--timeout`'s schedule. This
    process is one-shot, so nothing switches reconnection back on afterwards.
    """
    return await nats.connect(
        servers=servers,
        allow_reconnect=False,
        max_reconnect_attempts=1,
        reconnect_time_wait=0,
        connect_timeout=CONNECT_TIMEOUT_S,
        error_cb=_client_error,
    )


async def _provision_once(servers: list[str], replicas: int) -> dict[str, str]:
    """One attempt: connect, reconcile every stream, disconnect."""
    nc = await _connect(servers)
    try:
        return await ensure_streams(nc.jetstream(), all_topics(), replicas=replicas)
    finally:
        with contextlib.suppress(Exception):
            await nc.close()


async def provision(servers: list[str], *, replicas: int, timeout_s: float) -> dict[str, str]:
    """Reconcile every registered stream, retrying an unreachable broker until `timeout_s`.

    Returns stream name -> `"created" | "updated" | "unchanged"`. The whole
    attempt (connect and reconcile) is the retried unit: a broker that drops
    the connection half-way is retried from a fresh connection, which is safe
    because `ensure_streams` is idempotent. Raises the last transient error
    when the deadline expires, `StreamConfigConflictError` on an immutable
    difference, and any other broker error as itself.
    """
    log = get_logger(__name__)

    async def attempt() -> dict[str, str]:
        return await _provision_once(servers, replicas)

    return await connect_with_retry(
        "bus",
        attempt,
        timeout_s=timeout_s,
        transient=TRANSIENT_PROVISION_ERRORS,
        logger=log,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = os.environ.get(LOG_LEVEL_ENV, DEFAULT_LEVEL)
    try:
        configure_logging(SERVICE_NAME, level)
    except ValueError as exc:
        # A bad log level is a configuration error (2), reported through the
        # default-level logger rather than as a traceback -- the same shape
        # as `run_service`.
        configure_logging(SERVICE_NAME, DEFAULT_LEVEL)
        get_logger(__name__).error("config_invalid", error=str(exc))
        return EXIT_CONFIG_INVALID
    log = get_logger(__name__)

    try:
        outcome = asyncio.run(
            provision(args.servers, replicas=args.replicas, timeout_s=args.timeout)
        )
    except StreamConfigConflictError as exc:
        log.error(
            "stream_conflict",
            stream=exc.stream,
            field=exc.field,
            expected=exc.expected,
            actual=exc.actual,
            error=str(exc),
        )
        return EXIT_FAILURE
    except TRANSIENT_PROVISION_ERRORS as exc:
        log.error(
            "provision_failed",
            reason="unreachable",
            servers=args.servers,
            timeout_s=args.timeout,
            error=str(exc),
        )
        return EXIT_FAILURE
    except nats.errors.Error as exc:
        log.error("provision_failed", reason="broker_error", servers=args.servers, error=str(exc))
        return EXIT_FAILURE

    for stream, action in outcome.items():
        log.info("stream_provisioned", stream=stream, action=action)
    log.info(
        "provision_complete",
        streams=len(outcome),
        created=sum(action == "created" for action in outcome.values()),
        updated=sum(action == "updated" for action in outcome.values()),
        unchanged=sum(action == "unchanged" for action in outcome.values()),
        replicas=args.replicas,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
