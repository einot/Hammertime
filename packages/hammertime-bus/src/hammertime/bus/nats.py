"""NATS JetStream transport: one stream per topic; a partition is a subject.

Spec: section 19, section 20, section 32, section 33

Structural implementations of `hammertime.bus.interface.Producer`/`Consumer`
/`MessageBus` on top of nats-py (ADR-0013). Exercising `NatsBus`,
`NatsProducer` and `NatsConsumer` against a real server is the integration
job's business; there are no unit tests for those three classes by design,
the same posture the Kafka transport had. The pure helpers --
`stream_config_for`, `ensure_streams` (over a three-method fake),
`bus_endpoints` and `validate_bus_url` -- are unit-tested (ADR-0013
Amendment 4 ruling R3).

The mapping, from ADR-0013 decisions 1, 2, 4 and 5:

* Every `TopicSpec` is one stream named `TopicSpec.stream_name` with the
  single subject filter `TopicSpec.subject_filter`; partition `p` is the
  subject `TopicSpec.subject(p)`, and `NatsProducer` picks `p` with
  `partition_for(key, spec.partitions)`.
* Streams are provisioned before any service starts (`stream_config_for`,
  `ensure_streams`, run by `hammertime-provision`); `NatsBus.start()` only
  verifies they exist, and a missing one is `StreamNotProvisionedError`, a
  transient startup failure.
* A publish carries `Nats-Msg-Id = message_id` (server-side dedup inside
  the stream's duplicate window) and `Hammertime-Key = key` (so the key
  round-trips; JetStream has no message key of its own), and awaits the
  `PubAck`.
* A durable subscription is one pull consumer per partition, named
  `<group>-<p>` (or `<group>` for the whole topic), with explicit acks;
  progress is per-message `ack_sync`, `close()` naks every `Msg` a fetch
  loop received and did not acknowledge -- yielded, queued, or the unqueued
  remainder of a cancelled batch. What it cannot reach is a message the
  server delivered against the last outstanding pull that never reached the
  loop; at most `FETCH_BATCH` per durable, those surface at the next
  consumer after `ack_wait` (a recorded residual, ADR-0013 decision 3 as
  amended by Amendment 4 ruling R1). A positional subscription is an
  ordered ephemeral consumer starting at `start_offset`, which nats-py
  recreates by itself on a sequence gap or a missed heartbeat.
* On the way out of the queue a `Msg` whose subject's last token is not a
  partition (`TopicSpec.partition_of`), or whose `Hammertime-Key` hashes to
  a different partition than its subject names, is malformed at the
  transport: logged as `malformed_subject`, terminated on a durable and
  skipped on a positional subscription, never yielded (decision 5 as
  amended by Amendment 4 rulings S3 and S4).
* `NatsBus.end_offset(topic)` is `stream_info(...).state.last_seq + 1`: the
  offset the next appended message will receive, the readiness number of
  decision 9, `1` for an empty stream.
* `bus_endpoints(servers)` is the spec section 47.7 reduction for bus URLs:
  the only form in which a log record may name the servers (ADR-0013
  Amendment 2), since a NATS URL may carry a password or token in its
  userinfo. `validate_bus_url(url)` is what `load_settings` and the
  provisioner call per entry, so a URL nats-py's own parse would refuse --
  echoing part of it in a chained `ValueError` -- never reaches nats-py or
  a record (Amendment 4 ruling S2).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import SplitResult, urlsplit

import nats
import nats.errors
from hammertime.bus.interface import AssignmentListener, ConsumedMessage, static_partitions
from hammertime.bus.topics import TOPICS, TopicSpec, partition_for
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.js import JetStreamContext, api
from nats.js.errors import NotFoundError

logger = logging.getLogger(__name__)

#: Header carrying the message key (ADR-0013 assumption 10). JetStream has
#: no message key; the subject carries the partition, not the key, and the
#: aggregator asserts the key against the entry's IP. `Nats-` is reserved.
KEY_HEADER = "Hammertime-Key"

#: Stream settings (ADR-0013 decision 2, assumptions 6-8).
DUPLICATE_WINDOW_S = 120.0

#: Durable consumer settings (ADR-0013 decision 5, assumption 9).
ACK_WAIT_S = 30.0
MAX_ACK_PENDING = 10_000

#: Fetch-loop constants (ADR-0013 decision 5: "implementation constants").
#: One lingering pull per durable of up to `FETCH_BATCH` messages; a timeout
#: on an idle partition is the normal idle path and simply re-issues the
#: pull. The in-process queue is bounded so 128 fetch loops cannot pull a
#: whole stream into memory ahead of a slow consumer.
FETCH_BATCH = 100
FETCH_TIMEOUT_S = 5.0
QUEUE_CAPACITY = 1_000

#: How long `ack()` waits for the server to confirm each acknowledgement.
ACK_TIMEOUT_S = 5.0

#: Initial-connect shape (ADR-0013 assumption 14). nats-py 2.16.0 has no
#: `retry_on_failed_connect` option: its `connect()` loops over the server
#: pool until `max_reconnect_attempts` is exhausted (never, when it is -1),
#: and the same option governs mid-run reconnection. So the client is
#: connected with a small positive attempt count and `allow_reconnect=False`
#: -- an unreachable server surfaces as `NoServersError` after two tries,
#: and a rejected credential surfaces as its own exception type instead of
#: being retried into `NoServersError` -- and the two options are switched
#: to "reconnect forever" on the live client afterwards (`_connect`).
CONNECT_TIMEOUT_S = 2
_INITIAL_CONNECT_ATTEMPTS = 1


class StreamNotProvisionedError(Exception):
    """A registered topic has no stream on the connected server (ADR-0013 decision 2).

    A member of `TRANSIENT_ERRORS`: a service started before the provisioner
    finished retries under ADR-0009's startup deadline and, if the stream
    never appears, exits 1 with `start_failed`. Nothing in a service ever
    creates a stream.
    """

    def __init__(self, topic: str, stream: str) -> None:
        super().__init__(
            f"topic {topic!r} has no JetStream stream {stream!r}; run hammertime-provision"
        )
        self.topic = topic
        self.stream = stream


class StreamConfigConflictError(Exception):
    """An existing stream differs from the declared configuration in an immutable field.

    `retention` and `storage` cannot be changed on a live stream; the
    provisioner reports the conflict and changes nothing.
    """

    def __init__(self, stream: str, field: str, expected: object, actual: object) -> None:
        super().__init__(
            f"stream {stream!r} exists with {field}={actual!r}, declared {expected!r}; "
            f"{field} cannot be changed on an existing stream"
        )
        self.stream = stream
        self.field = field
        self.expected = expected
        self.actual = actual


#: What ingest and the aggregator pass to `connect_with_retry("bus", ...)`
#: (ADR-0009 A1). A rejected credential (`nats.errors.AuthorizationError`,
#: `InvalidUserCredentialsError`) is deliberately not here; whoever adds
#: credentials to a deployment MUST re-read `nats/errors.py` at the pinned
#: version and amend this list (ADR-0013 assumption 13).
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    nats.errors.NoServersError,
    nats.errors.TimeoutError,
    nats.errors.ConnectionClosedError,
    StreamNotProvisionedError,
)


# --- streams (decision 2) ----------------------------------------------------


def stream_config_for(spec: TopicSpec, *, replicas: int = 1) -> api.StreamConfig:
    """The declared JetStream configuration of `spec`'s stream (ADR-0013 decision 2).

    Limits retention (the only policy under which a durable's unacknowledged
    messages and a positional replay coexist), `max_age` from the topic's
    retention, unbounded bytes and messages, discard-old, file storage,
    `replicas` replicas and a 120 s duplicate window.
    """
    if replicas < 1:
        raise ValueError(f"replicas must be positive, got {replicas!r}")
    return api.StreamConfig(
        name=spec.stream_name,
        subjects=[spec.subject_filter],
        retention=api.RetentionPolicy.LIMITS,
        max_age=float(spec.retention_seconds),
        max_bytes=-1,
        max_msgs=-1,
        max_msgs_per_subject=-1,
        discard=api.DiscardPolicy.OLD,
        storage=api.StorageType.FILE,
        num_replicas=replicas,
        duplicate_window=DUPLICATE_WINDOW_S,
        allow_direct=False,
        deny_delete=False,
        deny_purge=False,
    )


#: Fields `ensure_streams` reconciles with `update_stream` when they differ.
_MUTABLE_STREAM_FIELDS = (
    "subjects",
    "max_age",
    "duplicate_window",
    "max_bytes",
    "max_msgs",
    "num_replicas",
)
#: Fields that cannot change on a live stream; a difference is a conflict.
_IMMUTABLE_STREAM_FIELDS = ("retention", "storage")


def _plain(value: object) -> object:
    """An enum's value, a list's tuple, else the value: comparable across the wire."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return tuple(value)
    return value


async def ensure_streams(
    js: JetStreamContext, specs: Iterable[TopicSpec], *, replicas: int = 1
) -> dict[str, str]:
    """Create or reconcile every stream in `specs`; idempotent (ADR-0013 decision 2).

    Returns stream name -> `"created" | "updated" | "unchanged"`. A stream
    that exists with a different immutable field (`retention`, `storage`)
    raises `StreamConfigConflictError` before anything is changed: every
    stream is inspected first, and only then are the creates and updates
    applied.
    """
    plan: list[tuple[str, api.StreamConfig, str]] = []
    for spec in specs:
        config = stream_config_for(spec, replicas=replicas)
        name = spec.stream_name
        try:
            info = await js.stream_info(name)
        except NotFoundError:
            plan.append((name, config, "created"))
            continue
        actual = info.config
        for field in _IMMUTABLE_STREAM_FIELDS:
            expected_value = _plain(getattr(config, field))
            actual_value = _plain(getattr(actual, field))
            if expected_value != actual_value:
                raise StreamConfigConflictError(name, field, expected_value, actual_value)
        drifted = any(
            _plain(getattr(config, field)) != _plain(getattr(actual, field))
            for field in _MUTABLE_STREAM_FIELDS
        )
        plan.append((name, config, "updated" if drifted else "unchanged"))
    outcome: dict[str, str] = {}
    for name, config, action in plan:
        if action == "created":
            await js.add_stream(config)
        elif action == "updated":
            await js.update_stream(config)
        outcome[name] = action
    return outcome


# --- the bus -----------------------------------------------------------------


async def _client_error(error: Exception) -> None:
    """nats-py's asynchronous error hook: one `WARNING nats_client_error` line, no traceback.

    Installed as `error_cb` by `_connect`; the record is `nats_client_error
    error=<exception>`, one per refused connection attempt or client-side
    error (ADR-0013 decision 3, assumption 29). The client's default handler
    logs every refused connection attempt as an ERROR with a full traceback,
    which would bury the `dependency_unavailable` records
    `connect_with_retry` writes during a normal startup wait; the exception
    itself still reaches the caller.
    """
    logger.warning("nats_client_error error=%s", error)


async def _connect(servers: list[str]) -> NATS:
    """Connect fail-fast, then let the live client reconnect forever.

    See the note on `_INITIAL_CONNECT_ATTEMPTS`: nats-py has no separate
    option for the initial attempt, so `connect()` is given a bounded pool
    walk and no reconnection (an authentication failure therefore raises as
    itself rather than being retried), and the reconnection options that the
    client reads at runtime are switched over once the connection is up.
    """
    try:
        nc = await nats.connect(
            servers=servers,
            allow_reconnect=False,
            max_reconnect_attempts=_INITIAL_CONNECT_ATTEMPTS,
            connect_timeout=CONNECT_TIMEOUT_S,
            error_cb=_client_error,
        )
    except nats.errors.Error as exc:
        if isinstance(exc.__context__, ValueError):
            # nats-py wraps its URL parse failure in a fixed-text `Error`
            # without `from None`, so the `ValueError` -- whose text echoes
            # the token it could not cast, possibly a password fragment --
            # rides along as `__context__` and `start_failed`'s traceback
            # would print it. Drop the chain (ADR-0013 Amendment 4 ruling
            # S2, assumption 74); `validate_bus_url` is the first line.
            raise exc from None
        raise
    nc.options["allow_reconnect"] = True
    nc.options["max_reconnect_attempts"] = -1
    return nc


async def _close_quietly(nc: NATS) -> None:
    with contextlib.suppress(Exception):
        await nc.close()


def _split_servers(servers: str) -> list[str]:
    """`HAMMERTIME_BUS_BROKERS` -> entries: split on `,`, stripped, empties dropped."""
    return [url.strip() for url in servers.split(",") if url.strip()]


#: What `bus_endpoints` renders for an entry `urlsplit` refuses (ADR-0013
#: assumption 44): a fixed string, nothing derived from the entry.
UNPARSEABLE_ENDPOINT = "<unparseable>"


def bus_endpoints(servers: str | Iterable[str]) -> list[str]:
    """`<scheme>://<host>[:<port>]` per server URL; userinfo, path, query and fragment dropped.

    The one reduction of a bus server list to something a log record may
    carry (ADR-0013 decision 3 as amended by Amendment 2, rulings 2-3;
    spec section 47.7: no record may contain a credential, and a NATS URL
    may carry `user:password@` or `token@` in its userinfo). A `str` is
    split exactly as `NatsBus.__init__` splits `HAMMERTIME_BUS_BROKERS`;
    an iterable is taken entry by entry, each stripped. An entry with no
    `://` is read as `nats://<entry>` first (nats-py's own normalisation;
    assumption 45 -- no port is invented). The result per entry is the
    scheme plus the authority after its **last** `@` -- the whole authority
    when there is no `@` -- so neither a password nor a username reaches
    the output (a lone username is a token to nats-py; assumption 42).

    Never raises: an entry `urlsplit` refuses (an unbalanced `[`) is the
    fixed string `<unparseable>` (assumption 44), and so is an entry in
    which `urlsplit` leaves an `@` in the path, query or fragment, whatever
    its authority holds (Amendment 4 rulings R8 and S2, assumption 72):
    `urlsplit` ends the authority at the first `/`, `?` or `#`, so a
    userinfo carrying one of them unencoded (`nats://user:pa/ss@nats:4222`)
    puts its `@` -- and a fragment of the password -- outside the authority,
    where the last-`@` rule cannot see it. An `@` there has no meaning in a
    NATS URL; it is the signature of exactly that mistake, and a malformed
    value is not an input a redaction can be trusted on. `SplitResult.port`,
    `.hostname`, `.username` and `.password` are never consulted -- `.port`
    raises a `ValueError` that echoes the text it could not parse, which
    is exactly what must not reach a log line.
    """
    entries = _split_servers(servers) if isinstance(servers, str) else [s.strip() for s in servers]
    endpoints: list[str] = []
    for entry in entries:
        url = entry if "://" in entry else f"nats://{entry}"
        try:
            parts = urlsplit(url)
        except ValueError:
            endpoints.append(UNPARSEABLE_ENDPOINT)
            continue
        if _at_outside_authority(parts):
            endpoints.append(UNPARSEABLE_ENDPOINT)
            continue
        endpoints.append(f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}")
    return endpoints


def _at_outside_authority(parts: SplitResult) -> bool:
    """True when `urlsplit` left an `@` in the path, query or fragment."""
    return "@" in parts.path or "@" in parts.query or "@" in parts.fragment


#: `validate_bus_url`'s message: fixed text, nothing of the URL in it.
_INVALID_BUS_URL = (
    "not a valid NATS server URL (percent-encode '/', '?', '#' and '@' inside a password)"
)


def validate_bus_url(url: str) -> None:
    """`ValueError`, naming nothing of `url`, if nats-py's own parse of it would fail or echo it.

    ADR-0013 decision 3 as amended by Amendment 4 ruling S2. The entry is
    normalised as nats-py normalises a server (one containing `://` as
    given; otherwise `nats://<entry>`) and refused when (i) `urlsplit`
    refuses it, (ii) `SplitResult.port` raises -- the very cast nats-py
    performs in `_parse_server_uri`, whose `ValueError` echoes the token it
    could not cast and rides into `start_failed` as the `__context__` of
    nats-py's fixed-text `Error` -- or (iii) an `@` is left outside the
    authority (`bus_endpoints`'s rule). No host or port grammar of
    Hammertime's own is added (assumption 74): a value that connected
    before is not refused now. The message contains none of the entry's
    text, so `load_settings` and the provisioner can name the variable and
    the entry's position and nothing else.
    """
    normalized = url if "://" in url else f"nats://{url}"
    try:
        parts = urlsplit(normalized)
        _ = parts.port
    except ValueError:
        # The `.port` message repeats the text it could not cast.
        raise ValueError(_INVALID_BUS_URL) from None
    if _at_outside_authority(parts):
        raise ValueError(_INVALID_BUS_URL)


class NatsBus:
    """`MessageBus` over one shared NATS connection (ADR-0013 decision 3).

    `servers` is `HAMMERTIME_BUS_BROKERS`: comma-separated NATS URLs
    (`nats://host:4222`, also `tls://`, `ws://`, `wss://`), not validated
    here -- `load_settings` checks each entry with `validate_bus_url`
    before this is built (ADR-0013 Amendment 4 ruling S2), and nats-py
    parses them again at `connect()`.
    `producer()` and `consumer()` may be called before `start()`; the
    objects they return use the connection lazily and raise `RuntimeError`
    if used before it is up.
    """

    def __init__(self, servers: str) -> None:
        urls = _split_servers(servers)
        if not urls:
            raise ValueError("NatsBus needs at least one server URL, got an empty list")
        self._servers = urls
        self._nc: NATS | None = None
        self._js: JetStreamContext | None = None
        self._producer: NatsProducer | None = None
        self._consumers: list[NatsConsumer] = []

    async def start(self) -> None:
        """Connect and verify every registered stream exists; idempotent.

        A missing stream is `StreamNotProvisionedError` (transient, decision
        2); the connection is closed again first, so the next attempt under
        `connect_with_retry` starts clean.
        """
        if self._nc is not None and not self._nc.is_closed:
            return
        nc = await _connect(self._servers)
        js = nc.jetstream()
        try:
            for spec in TOPICS.values():
                try:
                    await js.stream_info(spec.stream_name)
                except NotFoundError as exc:
                    raise StreamNotProvisionedError(spec.name, spec.stream_name) from exc
        except BaseException:
            await _close_quietly(nc)
            raise
        self._nc = nc
        self._js = js

    async def close(self) -> None:
        """Close every consumer (naking what was not acknowledged), then drain."""
        consumers, self._consumers = self._consumers, []
        for consumer in consumers:
            await consumer.close()
        nc, self._nc, self._js = self._nc, None, None
        if nc is None or nc.is_closed:
            return
        try:
            await nc.drain()
        except (nats.errors.Error, OSError):
            # Draining is refused while reconnecting and after a close; a
            # plain close is the best that can be done then.
            if not nc.is_closed:
                await _close_quietly(nc)

    def producer(self) -> "NatsProducer":
        """The one `NatsProducer` over the shared connection."""
        if self._producer is None:
            self._producer = NatsProducer(self)
        return self._producer

    def consumer(self, group_id: str) -> "NatsConsumer":
        """A new `NatsConsumer` over the shared connection, closed with the bus."""
        consumer = NatsConsumer(self, group_id)
        self._consumers.append(consumer)
        return consumer

    async def end_offset(self, topic: str) -> int:
        """The offset the next appended message will receive: `state.last_seq + 1`.

        `1` for an empty stream (sequences start at 1), so it is the same
        readiness number `InMemoryBus` gives as its log length (ADR-0013
        decision 9, assumption 20). An unregistered topic is a `KeyError`;
        before `start()` it is `RuntimeError("NatsBus is not started")`.
        """
        spec = TOPICS[topic]
        info = await self._require_js().stream_info(spec.stream_name)
        return info.state.last_seq + 1

    def _require_js(self) -> JetStreamContext:
        if self._js is None:
            raise RuntimeError("NatsBus is not started")
        return self._js

    def _connection(self) -> NATS | None:
        return self._nc


class NatsProducer:
    """`Producer` over `NatsBus`: awaited, deduplicated publishes (ADR-0013 decision 4)."""

    def __init__(self, bus: NatsBus) -> None:
        self._bus = bus

    async def publish(
        self, topic: str, key: bytes | str, value: bytes, *, message_id: str | None = None
    ) -> None:
        """Publish to `partition_for(key)`'s subject and await the stream's `PubAck`.

        An unregistered topic is a `KeyError`: there is no stream to publish
        to. A `PubAck` with `duplicate=True` (the stream had seen
        `message_id` inside its duplicate window) is a normal return. The
        key is carried as UTF-8 text in the `Hammertime-Key` header, so a
        `bytes` key must be valid UTF-8.
        """
        spec = TOPICS[topic]
        raw_key = key.encode("utf-8") if isinstance(key, str) else key
        partition = partition_for(raw_key, spec.partitions)
        headers: dict[str, Any] = {KEY_HEADER: raw_key.decode("utf-8")}
        if message_id is not None:
            headers[api.Header.MSG_ID.value] = message_id
        js = self._bus._require_js()
        await js.publish(spec.subject(partition), value, headers=headers, stream=spec.stream_name)

    async def flush(self) -> None:
        """No-op: every `publish` that returned was acknowledged by the stream."""
        return None


class _Closed:
    """Queue sentinel: `close()` ran; the iterator ends."""


@dataclass(frozen=True, slots=True)
class _Failed:
    """Queue sentinel: a fetch loop died; the iterator raises `error`."""

    error: BaseException


class NatsConsumer:
    """`Consumer` over `NatsBus` (ADR-0013 decision 5).

    One subscription per instance. A durable subscription binds one pull
    consumer per partition, `<group>-<p>` (or `<group>` for every partition
    of the topic), and runs one fetch loop per durable feeding one bounded
    in-process queue; the iterator yields from the queue. A message is
    *delivered* when the iterator yields it; its nats-py `Msg` is retained
    under `(topic, partition, offset)` until `ack()` (the most recent
    delivery wins when the server redelivers after `ack_wait`). A positional
    subscription is an ordered ephemeral consumer; nothing is retained and
    `ack()` is refused.
    """

    def __init__(self, bus: NatsBus, group_id: str) -> None:
        self._bus = bus
        self._group_id = group_id
        self._subscribed = False
        self._closed = False
        self._topic: str | None = None
        self._positional = False
        self._queue: asyncio.Queue[Msg | _Failed | _Closed] = asyncio.Queue(maxsize=QUEUE_CAPACITY)
        self._pumps: list[asyncio.Task[None]] = []
        self._pull_subs: list[JetStreamContext.PullSubscription] = []
        self._push_sub: JetStreamContext.PushSubscription | None = None
        self._retained: dict[tuple[str, int, int], Msg] = {}
        # The unqueued remainder of every batch a cancelled `_pump_pull` was
        # still queueing, for `close()` to nak (ADR-0013 Amendment 4 ruling
        # R1, assumption 59). Each `Msg` is in exactly one of: yielded and
        # retained, queued, or here.
        self._unqueued: list[Msg] = []

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        """Bind the consumers, tell `listener`, start fetching, return the iterator.

        Validation (`RuntimeError` for a second call or a closed instance,
        `ValueError` for an empty or negative partition set or a negative
        `start_offset`, `KeyError` for an unregistered topic) happens before
        the broker is contacted and before `listener` is called. The durable
        (or ephemeral) consumers are created or bound, then `on_assigned` is
        awaited, and only then do the fetch loops start -- so no message is
        pulled before the claims are held. If `on_assigned` raises, the
        subscriptions are dropped again and this instance is left as if
        `subscribe()` had never been called; durables created on the broker
        stay (they carry no ownership).
        """
        if self._closed:
            raise RuntimeError("NatsConsumer is closed; it cannot subscribe")
        if self._subscribed:
            raise RuntimeError(
                "NatsConsumer.subscribe() was already called; one subscription per instance"
            )
        claimed = None if partitions is None else static_partitions(topic, partitions)
        if claimed is not None and any(partition < 0 for partition in claimed):
            raise ValueError(f"partitions must be non-negative, got {sorted(claimed)}")
        if start_offset is not None and start_offset < 0:
            raise ValueError(f"start_offset must be non-negative, got {start_offset!r}")
        spec = TOPICS[topic]
        js = self._bus._require_js()
        assignment = frozenset(
            (topic, partition)
            for partition in (claimed if claimed is not None else range(spec.partitions))
        )
        try:
            if start_offset is None:
                await self._bind_durables(js, spec, claimed)
            else:
                await self._open_positional(js, spec, claimed, start_offset)
            if listener is not None:
                await listener.on_assigned(assignment)
        except BaseException:
            await self._unsubscribe_all()
            raise
        self._subscribed = True
        self._topic = topic
        self._positional = start_offset is not None
        self._start_pumps()
        return self._consume()

    async def _bind_durables(
        self, js: JetStreamContext, spec: TopicSpec, claimed: frozenset[int] | None
    ) -> None:
        """Create or bind one durable pull consumer per partition (or one for all)."""
        if claimed is None:
            durables = [(self._group_id, spec.subject_filter)]
        else:
            durables = [
                (f"{self._group_id}-{partition}", spec.subject(partition))
                for partition in sorted(claimed)
            ]
        for durable, filter_subject in durables:
            config = api.ConsumerConfig(
                name=durable,
                durable_name=durable,
                filter_subject=filter_subject,
                deliver_policy=api.DeliverPolicy.ALL,
                ack_policy=api.AckPolicy.EXPLICIT,
                ack_wait=ACK_WAIT_S,
                max_ack_pending=MAX_ACK_PENDING,
                max_deliver=-1,
                inactive_threshold=0.0,
                # num_replicas left unset: the consumer inherits the stream's.
            )
            sub = await js.pull_subscribe(
                filter_subject, durable=durable, stream=spec.stream_name, config=config
            )
            self._pull_subs.append(sub)
            # nats-py binds an existing durable without comparing its
            # configuration; a durable created by an earlier build keeps
            # its settings, so say so (ADR-0013 assumption 12).
            _warn_on_drift(await sub.consumer_info(), config)

    async def _open_positional(
        self,
        js: JetStreamContext,
        spec: TopicSpec,
        claimed: frozenset[int] | None,
        start_offset: int,
    ) -> None:
        """Open an ordered ephemeral consumer starting at `start_offset`."""
        filter_subjects: list[str] | None = None
        if claimed is None:
            subject = spec.subject_filter
        elif len(claimed) == 1:
            subject = spec.subject(next(iter(claimed)))
        else:
            # nats-py fills `filter_subject` from `subject` only when
            # `filter_subjects` is unset, so a multi-partition filter goes
            # in the config and the subject argument is the stream's.
            subject = spec.subject_filter
            filter_subjects = [spec.subject(partition) for partition in sorted(claimed)]
        config = api.ConsumerConfig(
            deliver_policy=api.DeliverPolicy.BY_START_SEQUENCE,
            # Stream sequences start at 1; a lower start means "from the
            # first retained message", which is what the server does with
            # any sequence below the stream's first.
            opt_start_seq=max(start_offset, 1),
            ack_policy=api.AckPolicy.NONE,
            filter_subjects=filter_subjects,
        )
        self._push_sub = await js.subscribe(
            subject,
            stream=spec.stream_name,
            config=config,
            ordered_consumer=True,
            manual_ack=True,
        )

    def _start_pumps(self) -> None:
        for sub in self._pull_subs:
            self._pumps.append(asyncio.create_task(self._pump_pull(sub)))
        if self._push_sub is not None:
            self._pumps.append(asyncio.create_task(self._pump_push(self._push_sub)))

    async def _pump_pull(self, sub: JetStreamContext.PullSubscription) -> None:
        """One durable's fetch loop: lingering pulls into the shared queue, in order.

        Cancelled mid-batch, it hands the not-yet-queued remainder to
        `close()` through `_unqueued`: a `Msg` whose `put` was interrupted
        is in the remainder, one whose `put` returned is in the queue, never
        both (ADR-0013 decision 3 as amended by Amendment 4 ruling R1).
        """
        try:
            while True:
                try:
                    batch = await sub.fetch(FETCH_BATCH, timeout=FETCH_TIMEOUT_S)
                except TimeoutError:
                    # Idle partition (or a broker outage the client is
                    # riding out); re-issue the pull.
                    continue
                queued = 0
                try:
                    for msg in batch:
                        await self._queue.put(msg)
                        queued += 1
                except asyncio.CancelledError:
                    self._unqueued.extend(batch[queued:])
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._queue.put(_Failed(exc))

    async def _pump_push(self, sub: JetStreamContext.PushSubscription) -> None:
        """The positional loop: the ordered consumer's deliveries into the queue."""
        try:
            while True:
                try:
                    msg = await sub.next_msg(timeout=FETCH_TIMEOUT_S)
                except TimeoutError:
                    continue
                if JetStreamContext.is_status_msg(msg):
                    continue
                await self._queue.put(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._queue.put(_Failed(exc))

    async def _consume(self) -> AsyncIterator[ConsumedMessage]:
        assert self._topic is not None
        spec = TOPICS[self._topic]
        while True:
            item = await self._queue.get()
            if isinstance(item, _Closed):
                return
            if isinstance(item, _Failed):
                raise item.error
            partition = spec.partition_of(item.subject)
            reason = _malformed_reason(spec, item, partition)
            if partition is None or reason is not None:
                await self._discard_malformed(item, reason or "not-a-partition")
                continue
            message = self._to_consumed(item, partition)
            if not self._positional:
                self._retained[(message.topic, message.partition, message.offset)] = item
            yield message

    async def _discard_malformed(self, msg: Msg, reason: str) -> None:
        """Log `malformed_subject` (no key, no payload); term on a durable, skip on positional.

        `+TERM` is the server's word for "will not be processed": the
        durable never redelivers it, so a poison subject cannot crash-loop a
        whole-topic durable or a positional replay (ADR-0013 Amendment 4
        assumption 77). Nothing can be sent on a positional subscription
        (`ack_policy none`), so the message is simply skipped.
        """
        logger.warning(
            "malformed_subject subject=%s stream_seq=%d reason=%s",
            msg.subject,
            msg.metadata.sequence.stream,
            reason,
        )
        if not self._positional:
            with contextlib.suppress(nats.errors.Error, OSError):
                await msg.term()

    def _to_consumed(self, msg: Msg, partition: int) -> ConsumedMessage:
        assert self._topic is not None
        metadata = msg.metadata
        headers = msg.headers or {}
        key_text = headers.get(KEY_HEADER)
        return ConsumedMessage(
            topic=self._topic,
            partition=partition,
            offset=metadata.sequence.stream,
            key=None if key_text is None else key_text.encode("utf-8"),
            value=msg.data,
            delivery_count=metadata.num_delivered,
        )

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        """Acknowledge exactly `messages`, each confirmed by the server (`ack_sync`).

        The whole iterable is checked against the retained deliveries before
        the first acknowledgement is sent, so a rejected call acknowledges
        nothing. A message named more than once in one call is acknowledged
        once. If the server does not confirm an acknowledgement in time,
        `nats.errors.TimeoutError` propagates and that message stays
        retained, so a retry can acknowledge it.
        """
        if self._closed:
            raise ValueError("cannot ack: this consumer is closed")
        if self._positional:
            raise ValueError("cannot ack on a positional subscription (start_offset was given)")
        batch = list(messages)
        if not batch:
            return
        keys: list[tuple[str, int, int]] = []
        for message in batch:
            key = (message.topic, message.partition, message.offset)
            if key not in self._retained:
                raise ValueError(
                    f"cannot ack {message.topic!r} partition {message.partition} offset "
                    f"{message.offset}: not delivered by this consumer under its durable "
                    f"subscription, or already acknowledged"
                )
            if key not in keys:
                keys.append(key)
        for key in keys:
            await self._retained[key].ack_sync(timeout=ACK_TIMEOUT_S)
            del self._retained[key]

    async def close(self) -> None:
        """Stop fetching, nak what reached the fetch loops and was not acknowledged, unsubscribe.

        Idempotent and safe before `subscribe()`. Every nats-py `Msg` a
        fetch loop received -- yielded and unacknowledged (retained), still
        queued and never yielded, or the unqueued remainder of the batch a
        loop was queueing when it was cancelled -- is negatively
        acknowledged once, after unsubscribing and before the final flush,
        so the next consumer for the group receives it without waiting for
        `ack_wait`. What this cannot reach is a message the server delivered
        against the loop's last outstanding pull that never reached the
        loop: in flight, or in nats-py's private per-subscription pending
        queue, which only a further `fetch()` drains. Those -- at most
        `FETCH_BATCH` per durable, since one pull asks for at most that many
        -- surface at the next consumer after `ack_wait` (ADR-0013 decision
        3 as amended by Amendment 4 ruling R1; a recorded residual).
        """
        if self._closed:
            return
        self._closed = True
        pumps, self._pumps = self._pumps, []
        for pump in pumps:
            pump.cancel()
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)
        queued: list[Msg] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, Msg):
                queued.append(item)
        unqueued, self._unqueued = self._unqueued, []
        retained, self._retained = self._retained, {}
        # Unsubscribe first, and round-trip a flush, so the server has
        # dropped this instance's outstanding pull requests before the naks
        # arrive; naked first, a redelivery would land on a pull request
        # whose inbox nobody reads and then wait out `ack_wait` after all.
        await self._unsubscribe_all()
        nc = self._bus._connection()
        if self._subscribed and not self._positional:
            await self._flush_quietly(nc)
            for msg in [*queued, *unqueued, *retained.values()]:
                with contextlib.suppress(nats.errors.Error, OSError):
                    await msg.nak()
            await self._flush_quietly(nc)
        self._queue.put_nowait(_Closed())

    @staticmethod
    async def _flush_quietly(nc: NATS | None) -> None:
        if nc is not None and nc.is_connected:
            with contextlib.suppress(nats.errors.Error, OSError):
                await nc.flush()

    async def _unsubscribe_all(self) -> None:
        subs, self._pull_subs = self._pull_subs, []
        for sub in subs:
            with contextlib.suppress(nats.errors.Error, OSError):
                await sub.unsubscribe()
        push_sub, self._push_sub = self._push_sub, None
        if push_sub is not None:
            with contextlib.suppress(nats.errors.Error, OSError):
                await push_sub.unsubscribe()


def _malformed_reason(spec: TopicSpec, msg: Msg, partition: int | None) -> str | None:
    """Decision 5's two transport invariants, checked on the way out of the queue.

    The subject's last token must be a partition (`TopicSpec.partition_of`
    gave `partition`), and a message carrying the key header must sit under
    the partition its key hashes to -- `NatsProducer` computed the subject
    from the key, so a message stored under another partition's subject
    would be applied by that partition's owner and one IP would be owned by
    two shards (spec section 20). A message without the header is left to
    the consumer's own checks (ADR-0013 Amendment 4 rulings S3 and S4,
    assumption 79). Returns the `malformed_subject` reason, or None.
    """
    if partition is None:
        return "not-a-partition"
    key_text = (msg.headers or {}).get(KEY_HEADER)
    if key_text is None:
        return None
    if partition_for(key_text.encode("utf-8"), spec.partitions) != partition:
        return "partition-mismatch"
    return None


#: Editable consumer fields whose drift from the declared configuration is
#: reported when binding an existing durable (ADR-0013 decision 5).
_DRIFT_FIELDS = ("ack_wait", "max_ack_pending", "filter_subject")


def _warn_on_drift(info: api.ConsumerInfo, expected: api.ConsumerConfig) -> None:
    for field in _DRIFT_FIELDS:
        expected_value = getattr(expected, field)
        actual_value = getattr(info.config, field)
        if expected_value != actual_value:
            logger.warning(
                "consumer_config_drift consumer=%s field=%s expected=%r actual=%r",
                info.name,
                field,
                expected_value,
                actual_value,
            )
