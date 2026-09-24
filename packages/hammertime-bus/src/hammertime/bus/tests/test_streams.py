"""`stream_config_for`, `ensure_streams` and `first_offset_of`: the stream a topic declares.

`stream_config_for` and `ensure_streams` reconcile it idempotently;
`first_offset_of` reads the first retained offset off its state.

Spec: section 19, section 20, section 32, section 33 (`hammertime.bus.nats`'s own
citations, ADR-0013 decision 3).

Written from ADR-0013 decision 2 alone -- its `ensure_streams` bullet, the
stream-configuration block, the italic "What is compared, and that it is
unit-tested" text appended 2026-09-22 (Amendment 4 ruling R3) and Amendment 4
assumptions 67-68; `nats.py` was not read. The sentences pinned here:

* Surface: "`stream_config_for(spec: TopicSpec, *, replicas: int = 1) ->
  nats.js.api.StreamConfig`" and "`async def ensure_streams(js:
  JetStreamContext, specs: Iterable[TopicSpec], *, replicas: int = 1) ->
  dict[str, str]` (stream name -> `"created" | "updated" | "unchanged"`)".
* The block: `name` is decision 1's stream name; `subjects ["<topic>.*"]`;
  `retention limits`; `max_age TopicSpec.retention_seconds`; `max_bytes -1`,
  `max_msgs -1`, `max_msgs_per_subject -1`; `discard old`; `storage file`;
  `num_replicas` the `--replicas` value; `duplicate_window 120 s`;
  `allow_direct false`, `deny_delete false`, `deny_purge false`,
  `subject_transform none`. "`stream_config_for` raises `ValueError` for
  `replicas < 1`" (assumption 68).
* Reconciliation: "`ensure_streams` calls `stream_info(name)`; on
  `NotFoundError` it calls `add_stream`; when the stream exists it compares
  the mutable fields (`subjects`, `max_age`, `duplicate_window`, `max_bytes`,
  `max_msgs`, `num_replicas`) and calls `update_stream` if any differs; a
  difference in an immutable field (`retention`, `storage`) raises
  `StreamConfigConflictError` and changes nothing. It inspects every stream in
  `specs` before it creates or updates any of them, so a conflict on one
  stream changes nothing on any other either."
* What is compared: "`ensure_streams` reads `stream_info(name).config` -- the
  `nats.js.api.StreamConfig` nats-py builds from the server's response, in
  which durations are seconds and the enum-valued fields (`retention`,
  `storage`, `discard`) are left as the server's strings rather than
  converted to the enums ... -- against `stream_config_for(spec,
  replicas=replicas)`, field by field, after normalising both sides: an
  `Enum` by its `.value`, a `list` by its elements in order, anything else as
  it is; so a stream the server reports with `retention="limits"` equals one
  declared with `RetentionPolicy.LIMITS`, and the comparison does not depend
  on which form the installed nats-py produces. A missing stream is
  `nats.js.errors.NotFoundError` from `stream_info` and nothing else;
  `add_stream` and `update_stream` each receive the declared `StreamConfig`
  whole; the result has one entry per spec."
* The harness: "a fake `JetStreamContext` exposing `stream_info`,
  `add_stream` and `update_stream` is the whole harness" -- assumption 67: a
  three-method object, `stream_info` "raising `nats.js.errors.NotFoundError()`
  (constructible with no arguments ...) or returning an object whose
  `.config` is an `api.StreamConfig`", exercising "the actual side both with
  the server's strings and with the enums".

ASSUMPTIONS -- details the text leaves open; adjust the fake, not the meaning
of the assertions:

1. `stream_info`, `add_stream` and `update_stream` may be called with the
   argument positionally or by keyword (`name=` / `config=`); the fake accepts
   both and records the value. nats-py's `add_stream` also takes the config as
   keyword fields, so the fake builds a `StreamConfig` from `**params` when no
   whole config is given -- and the tests then assert that what arrived *is*
   the declared config, which is what "receive the declared `StreamConfig`
   whole" means.
2. `StreamConfigConflictError` carries `.stream`, `.field`, `.expected` and
   `.actual` (the four fields of the tool's `stream_conflict` record, in the
   order `test_cli.py` already constructs the exception with). Whether
   `.expected`/`.actual` hold the enum or its string is not ruled, so both are
   compared through the same `.value` normalisation the ADR prescribes for the
   comparison itself.
3. "`allow_direct false`" is asserted as falsy: nats-py's field is
   `Optional[bool]` with default `None`, and a config that never sets it is
   as false as one that sets `False`. `subject_transform none` is read with
   `getattr(..., None)` so an installed nats-py without the field still
   collects.
4. The mutable-field list names `subjects`, `max_age`, `duplicate_window`,
   `max_bytes`, `max_msgs` and `num_replicas`; `max_msgs_per_subject` is in
   the block but in neither list, so a difference in it alone is not pinned
   either way.

`first_offset_of` (`TestFirstOffsetOf`) is written from ADR-0013 decision 3
as amended by Amendment 10 -- the `NatsBus` block's "`def
first_offset_of(state: api.StreamState) -> int:` ... not re-exported from
hammertime.bus", whose docstring is "`state.first_seq` when the stream holds
a message; `state.last_seq + 1` when it holds none", and the paragraph after
the "Added:" line: "The message count decides, not `first_seq`: a stream
nothing has been written to reports `first_seq` `0`, one that a purge or
expiry has emptied reports `last_seq + 1`, and neither holds a message there"
-- and Amendment 10 assumptions 134 ("An empty log's first offset is its
end") and 135 ("The message count, not `first_seq`, decides emptiness on
JetStream"). It is imported from `hammertime.bus.nats`, since it is not
re-exported. `nats.js.api.StreamState` is built with its five required
fields (`messages`, `bytes`, `first_seq`, `last_seq`, `consumer_count`), and
`num_deleted` where a test models interior deletions. No server is involved:
`NatsBus.first_offset` against a live stream is the `integration` job's
(#52).

`NatsBus.end_offset` and `NatsBus.first_offset` without a server
(`TestNatsBusOffsetReads`) are written from the same Amendment 10 paragraph
-- "it raises what `end_offset` raises: `KeyError` for an unregistered topic
on `NatsBus`, and `RuntimeError` before `NatsBus.start()`" and
"`NatsBus.first_offset` reads `stream_info(...).state`, as `end_offset` does,
and returns `first_offset_of(state)`" -- the `NatsBus` block's comments
"`stream_info(...).state.last_seq + 1`" and "`first_offset_of(stream_info(
...).state)`", decision 1's stream name per topic, assumption 133 ("a second
broker round trip"), and ADR-0017 decision 7's row for `bus.end_offset`,
`bus.first_offset` ("any exception ... Propagates", so the bus raises rather
than masks). Pinned for both methods: an unregistered topic is a `KeyError`
on a started-looking bus and on an unstarted one; a registered topic before
`start()` is a `RuntimeError`; against a stub JetStream context, `end_offset`
is `last_seq + 1`, `first_offset` is `first_offset_of(state)` for a non-empty
and an empty state, and `stream_info` is asked once, for the topic's stream
name. ASSUMPTIONS for this class:

5. The stub is installed by assigning the private attribute `_js` on an
   unstarted `NatsBus`. No ADR names a seam for the bus's JetStream context;
   this one is a test seam the ADR does not pin (the name comes from the
   review finding that asked for these tests), and if the implementation
   renames it, the fixture changes, not the assertions.
6. RULED 2026-09-23 by ADR-0013 Amendment 11; no longer an assumption, kept
   under its number because Amendment 11 cites it as "their ASSUMPTION 6".
   An unregistered topic on an *unstarted* bus is a `KeyError` rather than a
   `RuntimeError` -- the topic lookup comes first. Decision 3's dated
   paragraph "Which error, when both apply": "An unregistered topic is
   therefore a `KeyError` whether or not the bus has started, and
   `RuntimeError("NatsBus is not started")` is raised only for a registered
   topic before `start()`." This is the order decision 3 already gave
   `NatsConsumer.subscribe()` ("raised with the other argument checks before
   the broker is contacted", Amendment 1 C5.6).

`NatsBus.last_value` without a server (`TestNatsBusLastValue`) is written from
ADR-0013 Amendment 13 and decision 3's dated paragraph of 2026-09-24, and
reached, as ADR-0017 Amendment 3's Test seams says, "like the offset reads,
through a stub JetStream context whose `stream_info` and `get_msg` answer what
the test chooses". The stub is installed on `_js` (ASSUMPTION 5). Whether the
stream name reaches `get_msg` positionally or as `stream_name=` is not pinned,
and the stub accepts both.
"""

import copy
from collections.abc import Iterable, Mapping
from dataclasses import replace
from enum import Enum
from typing import Any, cast

import nats.js.errors
import pytest
from hammertime.bus.nats import (
    NatsBus,
    StreamConfigConflictError,
    ensure_streams,
    first_offset_of,
    stream_config_for,
)
from hammertime.bus.topics import TOPICS, TopicSpec, all_topics
from nats.js import api

OBSERVATIONS = TOPICS["hammertime.observations.v1"]
HOT_IP = TOPICS["hammertime.hot-ip.v1"]
PREFIX_STATS = TOPICS["hammertime.prefix-stats.v1"]

# Decision 2's block: "duplicate_window 120 s".
DUPLICATE_WINDOW_SECONDS = 120


def _plain(value: object) -> object:
    """The ADR's normalisation: "an `Enum` by its `.value`, a `list` by its elements in
    order, anything else as it is"."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _server_form(config: api.StreamConfig) -> api.StreamConfig:
    """`config` as nats-py's `StreamConfig.from_response` would present it: the enum-valued
    fields as the server's strings, `subjects` as a fresh list, everything else as it is."""

    # The strings are what `StreamConfig.from_response` yields for the enum-valued fields.
    server_form_kwargs: dict[str, Any] = {
        "retention": _plain(config.retention),
        "storage": _plain(config.storage),
        "discard": _plain(config.discard),
        "subjects": list(config.subjects or []),
    }
    return replace(config, **server_form_kwargs)


class _StreamInfo:
    """What `stream_info` answers: "an object whose `.config` is an `api.StreamConfig`"."""

    def __init__(self, config: api.StreamConfig) -> None:
        self.config = config


class _FakeJetStream:
    """Assumption 67's three-method fake, recording every call in order.

    `existing` maps a stream name to the config the "server" holds for it;
    `failures` maps a name to an error `stream_info` raises for it instead of
    answering (ASSUMPTION 1 for the argument shapes).
    """

    def __init__(
        self,
        existing: Mapping[str, api.StreamConfig] | None = None,
        *,
        failures: Mapping[str, BaseException] | None = None,
    ) -> None:
        self.existing: dict[str, api.StreamConfig] = dict(existing or {})
        self.failures: dict[str, BaseException] = dict(failures or {})
        self.calls: list[tuple[str, Any]] = []

    async def stream_info(self, name: str | None = None, **params: Any) -> _StreamInfo:
        resolved = name if name is not None else params["name"]
        self.calls.append(("stream_info", resolved))
        if resolved in self.failures:
            raise self.failures[resolved]
        if resolved not in self.existing:
            raise nats.js.errors.NotFoundError()
        return _StreamInfo(self.existing[resolved])

    async def add_stream(
        self, config: api.StreamConfig | None = None, **params: Any
    ) -> _StreamInfo:
        resolved = config if config is not None else api.StreamConfig(**params)
        self.calls.append(("add_stream", resolved))
        assert resolved.name is not None
        self.existing[resolved.name] = resolved
        return _StreamInfo(resolved)

    async def update_stream(
        self, config: api.StreamConfig | None = None, **params: Any
    ) -> _StreamInfo:
        resolved = config if config is not None else api.StreamConfig(**params)
        self.calls.append(("update_stream", resolved))
        assert resolved.name is not None
        self.existing[resolved.name] = resolved
        return _StreamInfo(resolved)

    def methods(self) -> list[str]:
        return [call[0] for call in self.calls]

    def arguments(self, method: str) -> list[Any]:
        return [call[1] for call in self.calls if call[0] == method]


async def _ensure(
    js: _FakeJetStream, specs: Iterable[TopicSpec], *, replicas: int = 1
) -> dict[str, str]:
    """`ensure_streams` over the fake, which stands in for the `JetStreamContext`."""

    return await ensure_streams(cast(Any, js), specs, replicas=replicas)


# --------------------------------------------------------------------------
# stream_config_for: decision 2's block
# --------------------------------------------------------------------------


class TestStreamConfigFor:
    """Decision 2: "`stream_config_for(spec: TopicSpec, *, replicas: int = 1) ->
    nats.js.api.StreamConfig`" carrying the stream-configuration block."""

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    def test_it_is_a_stream_config_named_after_the_topic(self, spec: TopicSpec) -> None:
        config = stream_config_for(spec)

        assert isinstance(config, api.StreamConfig)
        # "name <stream name of decision 1>".
        assert config.name == spec.stream_name

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    def test_the_single_subject_is_the_topic_filter(self, spec: TopicSpec) -> None:
        # "subjects ["<topic>.*"]" -- decision 1: "whose single subject filter
        # is `<topic>.*`".
        config = stream_config_for(spec)

        assert config.subjects == [spec.subject_filter]
        assert config.subjects == [f"{spec.name}.*"]

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    def test_retention_is_limits_for_the_topics_retention_seconds(self, spec: TopicSpec) -> None:
        # "retention limits"; "max_age TopicSpec.retention_seconds (1 d, 7 d,
        # 30 d, 1 d)".
        config = stream_config_for(spec)

        assert config.retention == api.RetentionPolicy.LIMITS
        assert config.max_age == spec.retention_seconds

    def test_the_four_retention_ages_are_the_blocks(self) -> None:
        # The block's parenthesis, in decision 1's table order: 1 d, 7 d, 30 d,
        # 1 d.
        ages = [
            stream_config_for(TOPICS[name]).max_age
            for name in (
                "hammertime.observations.v1",
                "hammertime.observations-reconciliation.v1",
                "hammertime.hot-ip.v1",
                "hammertime.prefix-stats.v1",
            )
        ]

        assert ages == [86_400, 7 * 86_400, 30 * 86_400, 86_400]

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    def test_size_and_count_limits_are_unbounded(self, spec: TopicSpec) -> None:
        # "max_bytes -1 (unbounded) max_msgs -1 max_msgs_per_subject -1".
        config = stream_config_for(spec)

        assert config.max_bytes == -1
        assert config.max_msgs == -1
        assert config.max_msgs_per_subject == -1

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    def test_discard_old_on_file_storage(self, spec: TopicSpec) -> None:
        # "discard old"; "storage file".
        config = stream_config_for(spec)

        assert config.discard == api.DiscardPolicy.OLD
        assert config.storage == api.StorageType.FILE

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    def test_the_duplicate_window_is_two_minutes(self, spec: TopicSpec) -> None:
        # "duplicate_window 120 s" -- decision 4's deduplication by
        # `Nats-Msg-Id` is bounded by it.
        assert stream_config_for(spec).duplicate_window == DUPLICATE_WINDOW_SECONDS

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    def test_the_flags_are_off_and_there_is_no_transform(self, spec: TopicSpec) -> None:
        # "allow_direct false deny_delete false deny_purge false
        # subject_transform none" (ASSUMPTION 3; assumption 76 keeps the two
        # deny flags false on purpose).
        config = stream_config_for(spec)

        assert not config.allow_direct
        assert not config.deny_delete
        assert not config.deny_purge
        assert getattr(config, "subject_transform", None) is None

    def test_replicas_defaults_to_one(self) -> None:
        # "num_replicas 1 (the --replicas flag of hammertime-provision; 3 in a
        # clustered deployment)".
        assert stream_config_for(OBSERVATIONS).num_replicas == 1

    @pytest.mark.parametrize("replicas", [1, 3, 5])
    def test_replicas_is_the_keyword(self, replicas: int) -> None:
        assert stream_config_for(OBSERVATIONS, replicas=replicas).num_replicas == replicas

    @pytest.mark.parametrize("replicas", [0, -1])
    def test_fewer_than_one_replica_is_a_value_error(self, replicas: int) -> None:
        # "`stream_config_for` raises `ValueError` for `replicas < 1`"
        # (assumption 68: the CLI refuses the same value one layer up).
        with pytest.raises(ValueError):
            stream_config_for(OBSERVATIONS, replicas=replicas)

    def test_replicas_does_not_change_anything_else(self) -> None:
        one = stream_config_for(HOT_IP, replicas=1)
        three = stream_config_for(HOT_IP, replicas=3)

        assert replace(three, num_replicas=1) == one

    def test_it_is_pure(self) -> None:
        # "pure over their arguments": two calls agree field for field.
        assert stream_config_for(HOT_IP) == stream_config_for(HOT_IP)


# --------------------------------------------------------------------------
# ensure_streams: created / unchanged / updated / conflict
# --------------------------------------------------------------------------


class TestAMissingStreamIsCreated:
    """Decision 2: "`ensure_streams` calls `stream_info(name)`; on `NotFoundError` it calls
    `add_stream`"."""

    async def test_add_stream_is_called_once_with_the_declared_config(self) -> None:
        js = _FakeJetStream()

        result = await _ensure(js, [HOT_IP])

        assert result == {HOT_IP.stream_name: "created"}
        assert js.methods() == ["stream_info", "add_stream"]
        assert js.arguments("stream_info") == [HOT_IP.stream_name]
        # "`add_stream` and `update_stream` each receive the declared
        # `StreamConfig` whole" (ASSUMPTION 1).
        assert js.arguments("add_stream") == [stream_config_for(HOT_IP)]
        assert js.arguments("update_stream") == []

    async def test_the_created_stream_carries_the_replicas_asked_for(self) -> None:
        js = _FakeJetStream()

        await _ensure(js, [HOT_IP], replicas=3)

        assert js.arguments("add_stream") == [stream_config_for(HOT_IP, replicas=3)]
        assert js.arguments("add_stream")[0].num_replicas == 3

    async def test_a_second_run_over_the_created_stream_is_unchanged(self) -> None:
        # Idempotent: "a day-one step that runs **before** the services, and it
        # is idempotent".
        js = _FakeJetStream()
        await _ensure(js, [HOT_IP])

        result = await _ensure(js, [HOT_IP])

        assert result == {HOT_IP.stream_name: "unchanged"}
        assert js.methods() == ["stream_info", "add_stream", "stream_info"]

    async def test_missing_is_not_found_error_and_nothing_else(self) -> None:
        # "A missing stream is `nats.js.errors.NotFoundError` from `stream_info`
        # and nothing else": another JetStream error propagates as itself and
        # creates nothing.
        js = _FakeJetStream(failures={HOT_IP.stream_name: nats.js.errors.ServiceUnavailableError()})

        with pytest.raises(nats.js.errors.ServiceUnavailableError):
            await _ensure(js, [HOT_IP])

        assert js.arguments("add_stream") == []
        assert js.arguments("update_stream") == []


class TestAnIdenticalStreamIsUnchanged:
    """Decision 2: "when the stream exists it compares the mutable fields ... and calls
    `update_stream` if any differs" -- none differs here."""

    async def test_the_declared_config_itself_is_unchanged(self) -> None:
        declared = stream_config_for(HOT_IP)
        js = _FakeJetStream({HOT_IP.stream_name: declared})

        result = await _ensure(js, [HOT_IP])

        assert result == {HOT_IP.stream_name: "unchanged"}
        assert js.methods() == ["stream_info"]

    async def test_an_equal_config_with_the_enums_is_unchanged(self) -> None:
        # Assumption 67: the actual side "with the enums" -- a distinct object,
        # every field equal, the enum members as nats-py's dataclass declares.
        declared = stream_config_for(HOT_IP)
        with_enums = replace(
            declared,
            retention=api.RetentionPolicy.LIMITS,
            storage=api.StorageType.FILE,
            discard=api.DiscardPolicy.OLD,
            subjects=[HOT_IP.subject_filter],
        )
        assert with_enums is not declared
        js = _FakeJetStream({HOT_IP.stream_name: with_enums})

        result = await _ensure(js, [HOT_IP])

        assert result == {HOT_IP.stream_name: "unchanged"}
        assert js.arguments("add_stream") == []
        assert js.arguments("update_stream") == []

    async def test_the_servers_string_form_is_unchanged(self) -> None:
        # "so a stream the server reports with `retention="limits"` equals one
        # declared with `RetentionPolicy.LIMITS`, and the comparison does not
        # depend on which form the installed nats-py produces": `retention`,
        # `storage` and `discard` as strings, `subjects` as a list, everything
        # else identical.
        server_form = _server_form(stream_config_for(HOT_IP))
        assert server_form.retention == "limits"
        assert server_form.storage == "file"
        assert server_form.discard == "old"
        assert isinstance(server_form.subjects, list)
        js = _FakeJetStream({HOT_IP.stream_name: server_form})

        result = await _ensure(js, [HOT_IP])

        assert result == {HOT_IP.stream_name: "unchanged"}
        assert js.arguments("add_stream") == []
        assert js.arguments("update_stream") == []

    async def test_the_replicas_asked_for_are_what_is_compared(self) -> None:
        # Declared with `replicas=3` against a server holding 3: unchanged.
        js = _FakeJetStream({HOT_IP.stream_name: stream_config_for(HOT_IP, replicas=3)})

        result = await _ensure(js, [HOT_IP], replicas=3)

        assert result == {HOT_IP.stream_name: "unchanged"}
        assert js.arguments("update_stream") == []


class TestMutableDriftIsUpdated:
    """Decision 2: the mutable fields are "`subjects`, `max_age`, `duplicate_window`,
    `max_bytes`, `max_msgs`, `num_replicas`"; a difference in any "calls
    `update_stream`"."""

    @pytest.mark.parametrize(
        ("field", "actual"),
        [
            pytest.param("max_age", 3_600, id="max_age"),
            pytest.param("duplicate_window", 60, id="duplicate_window"),
            pytest.param("max_bytes", 1_000_000, id="max_bytes"),
            pytest.param("max_msgs", 1_000, id="max_msgs"),
            pytest.param("num_replicas", 3, id="num_replicas"),
            pytest.param("subjects", ["hammertime.hot-ip.v1.>"], id="subjects"),
        ],
    )
    async def test_a_mutable_difference_calls_update_stream_with_the_declared_config(
        self, field: str, actual: object
    ) -> None:
        declared = stream_config_for(HOT_IP)
        override: dict[str, Any] = {field: actual}
        drifted = replace(declared, **override)
        js = _FakeJetStream({HOT_IP.stream_name: drifted})

        result = await _ensure(js, [HOT_IP])

        assert result == {HOT_IP.stream_name: "updated"}
        assert js.methods() == ["stream_info", "update_stream"]
        # "each receive the declared `StreamConfig` whole".
        assert js.arguments("update_stream") == [declared]
        assert js.arguments("add_stream") == []

    async def test_drift_in_the_servers_string_form_is_still_updated(self) -> None:
        # The normalisation must not hide a real difference: the server's
        # strings with a different `max_age` is drift, not "unchanged".
        drifted = _server_form(replace(stream_config_for(HOT_IP), max_age=3_600))
        js = _FakeJetStream({HOT_IP.stream_name: drifted})

        result = await _ensure(js, [HOT_IP])

        assert result == {HOT_IP.stream_name: "updated"}
        assert js.arguments("update_stream") == [stream_config_for(HOT_IP)]

    async def test_a_replica_count_below_the_one_asked_for_is_updated(self) -> None:
        # "num_replicas 1 (the --replicas flag ...; 3 in a clustered
        # deployment)": scaling the flag up reconciles the existing stream.
        js = _FakeJetStream({HOT_IP.stream_name: stream_config_for(HOT_IP, replicas=1)})

        result = await _ensure(js, [HOT_IP], replicas=3)

        assert result == {HOT_IP.stream_name: "updated"}
        assert js.arguments("update_stream") == [stream_config_for(HOT_IP, replicas=3)]

    async def test_a_second_run_after_the_update_is_unchanged(self) -> None:
        js = _FakeJetStream({HOT_IP.stream_name: replace(stream_config_for(HOT_IP), max_age=1)})
        await _ensure(js, [HOT_IP])

        result = await _ensure(js, [HOT_IP])

        assert result == {HOT_IP.stream_name: "unchanged"}


class TestAnImmutableDifferenceIsAConflict:
    """Decision 2: "a difference in an immutable field (`retention`, `storage`) raises
    `StreamConfigConflictError` and changes nothing"."""

    @pytest.mark.parametrize(
        ("field", "actual", "expected_plain", "actual_plain"),
        [
            pytest.param(
                "retention",
                api.RetentionPolicy.WORK_QUEUE,
                "limits",
                "workqueue",
                id="retention-enum",
            ),
            pytest.param("retention", "workqueue", "limits", "workqueue", id="retention-string"),
            pytest.param("storage", api.StorageType.MEMORY, "file", "memory", id="storage-enum"),
            pytest.param("storage", "memory", "file", "memory", id="storage-string"),
        ],
    )
    async def test_the_conflict_names_the_stream_field_expected_and_actual(
        self, field: str, actual: object, expected_plain: str, actual_plain: str
    ) -> None:
        override: dict[str, Any] = {field: actual}
        conflicting = replace(stream_config_for(HOT_IP), **override)
        js = _FakeJetStream({HOT_IP.stream_name: conflicting})

        with pytest.raises(StreamConfigConflictError) as excinfo:
            await _ensure(js, [HOT_IP])

        error = excinfo.value
        # ASSUMPTION 2: the four fields of the `stream_conflict` record.
        assert error.stream == HOT_IP.stream_name
        assert error.field == field
        assert _plain(error.expected) == expected_plain
        assert _plain(error.actual) == actual_plain

    async def test_a_conflict_changes_nothing(self) -> None:
        # "and changes nothing": no `add_stream`, no `update_stream`, the
        # server's config as it was.
        conflicting = replace(stream_config_for(HOT_IP), storage=api.StorageType.MEMORY)
        js = _FakeJetStream({HOT_IP.stream_name: conflicting})

        with pytest.raises(StreamConfigConflictError):
            await _ensure(js, [HOT_IP])

        assert js.methods() == ["stream_info"]
        assert js.existing[HOT_IP.stream_name] is conflicting

    async def test_an_immutable_difference_wins_over_a_mutable_one(self) -> None:
        # Both kinds differ: the immutable one is a conflict, and the mutable
        # one is not reconciled on the way to it.
        conflicting = replace(
            stream_config_for(HOT_IP), retention=api.RetentionPolicy.WORK_QUEUE, max_age=1
        )
        js = _FakeJetStream({HOT_IP.stream_name: conflicting})

        with pytest.raises(StreamConfigConflictError) as excinfo:
            await _ensure(js, [HOT_IP])

        assert excinfo.value.field == "retention"
        assert js.arguments("update_stream") == []

    async def test_the_conflict_is_the_exception_the_tool_and_the_bus_export(self) -> None:
        # Decision 3's block: `class StreamConfigConflictError(Exception)`.
        assert issubclass(StreamConfigConflictError, Exception)


# --------------------------------------------------------------------------
# inspect every stream first; one entry per spec
# --------------------------------------------------------------------------


class TestEveryStreamIsInspectedBeforeAnyIsChanged:
    """Decision 2: "It inspects every stream in `specs` before it creates or updates any of
    them, so a conflict on one stream changes nothing on any other either" (Amendment 1
    ruling C9)."""

    async def test_a_conflict_on_the_second_stream_leaves_the_first_uncreated(self) -> None:
        # `[A (missing), B (conflict)]`: both inspected, nothing created, the
        # error names B.
        conflicting = replace(stream_config_for(HOT_IP), storage=api.StorageType.MEMORY)
        js = _FakeJetStream({HOT_IP.stream_name: conflicting})

        with pytest.raises(StreamConfigConflictError) as excinfo:
            await _ensure(js, [OBSERVATIONS, HOT_IP])

        assert excinfo.value.stream == HOT_IP.stream_name
        assert js.methods() == ["stream_info", "stream_info"]
        assert js.arguments("stream_info") == [OBSERVATIONS.stream_name, HOT_IP.stream_name]
        assert js.arguments("add_stream") == []
        assert OBSERVATIONS.stream_name not in js.existing

    async def test_a_conflict_on_the_second_stream_leaves_the_first_unupdated(self) -> None:
        # `[A (drift), B (conflict)]`: A's drift is not reconciled.
        drifted = replace(stream_config_for(OBSERVATIONS), max_age=1)
        conflicting = replace(stream_config_for(HOT_IP), retention=api.RetentionPolicy.WORK_QUEUE)
        js = _FakeJetStream({OBSERVATIONS.stream_name: drifted, HOT_IP.stream_name: conflicting})

        with pytest.raises(StreamConfigConflictError) as excinfo:
            await _ensure(js, [OBSERVATIONS, HOT_IP])

        assert excinfo.value.stream == HOT_IP.stream_name
        assert js.arguments("update_stream") == []
        assert js.existing[OBSERVATIONS.stream_name] is drifted

    async def test_a_conflict_on_the_first_stream_leaves_the_second_uncreated(self) -> None:
        # `[A (conflict), B (missing)]`: the order of `specs` does not matter --
        # nothing on any other stream changes.
        conflicting = replace(stream_config_for(OBSERVATIONS), storage=api.StorageType.MEMORY)
        js = _FakeJetStream({OBSERVATIONS.stream_name: conflicting})

        with pytest.raises(StreamConfigConflictError) as excinfo:
            await _ensure(js, [OBSERVATIONS, HOT_IP])

        assert excinfo.value.stream == OBSERVATIONS.stream_name
        assert js.arguments("add_stream") == []
        assert HOT_IP.stream_name not in js.existing

    async def test_every_stream_info_precedes_every_create_and_update(self) -> None:
        # The happy path shows the same phase order: all inspections, then all
        # changes.
        drifted = replace(stream_config_for(PREFIX_STATS), duplicate_window=1)
        js = _FakeJetStream({PREFIX_STATS.stream_name: drifted})

        result = await _ensure(js, [OBSERVATIONS, HOT_IP, PREFIX_STATS])

        assert result == {
            OBSERVATIONS.stream_name: "created",
            HOT_IP.stream_name: "created",
            PREFIX_STATS.stream_name: "updated",
        }
        methods = js.methods()
        last_inspection = max(i for i, method in enumerate(methods) if method == "stream_info")
        first_change = min(i for i, method in enumerate(methods) if method != "stream_info")
        assert last_inspection < first_change
        assert methods.count("stream_info") == 3


class TestOneEntryPerSpec:
    """Decision 2: "the result has one entry per spec"; the tool "runs `ensure_streams`
    over `all_topics()`"."""

    async def test_all_topics_on_an_empty_server_are_all_created(self) -> None:
        js = _FakeJetStream()
        specs = list(all_topics())

        result = await _ensure(js, specs)

        assert result == {spec.stream_name: "created" for spec in specs}
        assert len(result) == len(specs) == 4
        assert js.arguments("add_stream") == [stream_config_for(spec) for spec in specs]

    async def test_all_topics_on_a_provisioned_server_are_all_unchanged(self) -> None:
        specs = list(all_topics())
        js = _FakeJetStream({spec.stream_name: stream_config_for(spec) for spec in specs})

        result = await _ensure(js, specs)

        assert result == {spec.stream_name: "unchanged" for spec in specs}
        assert js.methods() == ["stream_info"] * len(specs)

    async def test_a_mixed_server_reports_each_stream_by_its_own_action(self) -> None:
        # The four topics of decision 1's table, looked up by name (the order
        # `all_topics()` yields them in is not pinned).
        reconciliation = TOPICS["hammertime.observations-reconciliation.v1"]
        js = _FakeJetStream(
            {
                reconciliation.stream_name: stream_config_for(reconciliation),
                HOT_IP.stream_name: replace(stream_config_for(HOT_IP), max_msgs=10),
                PREFIX_STATS.stream_name: _server_form(stream_config_for(PREFIX_STATS)),
            }
        )

        result = await _ensure(js, list(all_topics()))

        assert result == {
            OBSERVATIONS.stream_name: "created",
            reconciliation.stream_name: "unchanged",
            HOT_IP.stream_name: "updated",
            PREFIX_STATS.stream_name: "unchanged",
        }

    async def test_specs_may_be_a_one_shot_iterable(self) -> None:
        # The signature says `Iterable[TopicSpec]`, and inspect-all-then-apply
        # walks the specs twice, so a generator must still yield one entry per
        # spec.
        js = _FakeJetStream()
        specs = list(all_topics())

        result = await _ensure(js, (spec for spec in specs))

        assert result == {spec.stream_name: "created" for spec in specs}

    async def test_no_specs_is_an_empty_result_and_no_call(self) -> None:
        js = _FakeJetStream()

        assert await _ensure(js, []) == {}
        assert js.calls == []

    async def test_the_result_values_are_the_three_action_words(self) -> None:
        # `dict[str, str]` (stream name -> `"created" | "updated" | "unchanged"`).
        js = _FakeJetStream(
            {
                HOT_IP.stream_name: stream_config_for(HOT_IP),
                PREFIX_STATS.stream_name: replace(stream_config_for(PREFIX_STATS), max_age=1),
            }
        )

        result = await _ensure(js, [OBSERVATIONS, HOT_IP, PREFIX_STATS])

        assert set(result.values()) == {"created", "unchanged", "updated"}


# --------------------------------------------------------------------------
# first_offset_of: the first retained offset, or the end when none is retained
# --------------------------------------------------------------------------


def _state(
    *, messages: int, first_seq: int, last_seq: int, num_deleted: int | None = None
) -> api.StreamState:
    return api.StreamState(
        messages=messages,
        bytes=0,
        first_seq=first_seq,
        last_seq=last_seq,
        consumer_count=0,
        num_deleted=num_deleted,
    )


class TestFirstOffsetOf:
    """ADR-0013 decision 3 as amended by Amendment 10: "`state.first_seq` when
    the stream holds a message; `state.last_seq + 1` when it holds none." "The
    message count decides, not `first_seq`" (assumption 135), and "An empty
    log's first offset is its end" (assumption 134): `end_offset` is
    `state.last_seq + 1`."""

    def test_a_stream_nothing_has_been_written_to(self) -> None:
        # "a stream nothing has been written to reports `first_seq` `0`":
        # no message, so the end, `last_seq + 1`.
        assert first_offset_of(_state(messages=0, first_seq=0, last_seq=0)) == 1

    def test_a_stream_emptied_by_a_purge_or_expiry(self) -> None:
        # "one that a purge or expiry has emptied reports `last_seq + 1`".
        assert first_offset_of(_state(messages=0, first_seq=21, last_seq=20)) == 21

    def test_the_message_count_decides_not_first_seq(self) -> None:
        # Assumption 135: an empty stream's `first_seq` "depends on how it
        # became empty", so `first_offset_of` "reads `first_seq` only when a
        # message is there". `first_seq` 0 with no message is not offset 0.
        assert first_offset_of(_state(messages=0, first_seq=0, last_seq=20)) == 21

    def test_a_stream_that_holds_its_whole_history(self) -> None:
        assert first_offset_of(_state(messages=3, first_seq=1, last_seq=3)) == 1

    def test_a_stream_whose_head_has_aged_out(self) -> None:
        assert first_offset_of(_state(messages=5, first_seq=16, last_seq=20)) == 16

    def test_interior_deletions_do_not_move_it(self) -> None:
        # Two messages left between 5 and 20, fourteen deleted in between:
        # the first retained is still `first_seq`. Assumption 19 (ADR-0017)
        # / Amendment 10 ruling 4: "`first_offset` cannot see a hole behind a
        # retained record".
        state = _state(messages=2, first_seq=5, last_seq=20, num_deleted=14)

        assert first_offset_of(state) == 5

    @pytest.mark.parametrize(
        ("messages", "first_seq", "last_seq"),
        [
            pytest.param(0, 0, 0, id="never-written"),
            pytest.param(0, 21, 20, id="emptied"),
            pytest.param(5, 16, 20, id="head-aged-out"),
        ],
    )
    def test_the_state_is_not_mutated(self, messages: int, first_seq: int, last_seq: int) -> None:
        state = _state(messages=messages, first_seq=first_seq, last_seq=last_seq)
        before = copy.deepcopy(state)

        first_offset_of(state)

        assert state == before
        assert (state.messages, state.first_seq, state.last_seq) == (messages, first_seq, last_seq)


# --------------------------------------------------------------------------
# NatsBus.end_offset / NatsBus.first_offset: errors and stream_info(...).state
# --------------------------------------------------------------------------

# Any syntactically valid server list: nothing here connects.
_SERVERS = "nats://127.0.0.1:4222"

# Not in decision 1's table.
_UNREGISTERED_TOPIC = "hammertime.no-such-topic.v1"

_OFFSET_READS = ["end_offset", "first_offset"]


class _StateInfo:
    """What `stream_info` answers for the offset reads: an object whose `.state` is an
    `api.StreamState` ("reads `stream_info(...).state`")."""

    def __init__(self, state: api.StreamState) -> None:
        self.state = state


class _StateJetStream:
    """A stub JetStream context whose `stream_info(name)` answers one `StreamState`,
    recording the name it was asked for (positional or `name=`, ASSUMPTION 1)."""

    def __init__(self, state: api.StreamState) -> None:
        self.state = state
        self.names: list[str] = []

    async def stream_info(self, name: str | None = None, **params: Any) -> _StateInfo:
        resolved = name if name is not None else params["name"]
        self.names.append(resolved)
        return _StateInfo(self.state)


def _bus_with(js: _StateJetStream) -> NatsBus:
    """An unstarted `NatsBus` with the stub installed.

    TEST SEAM NOT PINNED BY THE ADR (ASSUMPTION 5): no ADR names how a caller
    reaches the bus's JetStream context, so the stub is assigned to the private
    `_js` attribute directly.
    """

    bus = NatsBus(_SERVERS)
    # Through `Any`, so the type checker does not depend on how (or whether)
    # the class declares the attribute.
    cast(Any, bus)._js = js
    return bus


async def _read(bus: NatsBus, method: str, topic: str) -> int:
    reader = getattr(bus, method)
    return cast(int, await reader(topic))


class TestNatsBusOffsetReads:
    """ADR-0013 decision 3 as amended by Amendment 10: `first_offset` "raises what
    `end_offset` raises: `KeyError` for an unregistered topic on `NatsBus`, and
    `RuntimeError` before `NatsBus.start()`"; "`NatsBus.first_offset` reads
    `stream_info(...).state`, as `end_offset` does, and returns
    `first_offset_of(state)`"; the `NatsBus` block: `end_offset` is
    "`stream_info(...).state.last_seq + 1`". ADR-0017 decision 7: an exception
    from either read propagates."""

    @pytest.mark.parametrize("method", _OFFSET_READS)
    async def test_an_unregistered_topic_is_a_key_error_before_start(self, method: str) -> None:
        # ADR-0013 Amendment 11 (formerly ASSUMPTION 6): the topic lookup
        # comes first, so an unstarted bus still answers an unregistered
        # topic with `KeyError`, not `RuntimeError` -- "a `KeyError` whether
        # or not the bus has started".
        bus = NatsBus(_SERVERS)

        with pytest.raises(KeyError):
            await _read(bus, method, _UNREGISTERED_TOPIC)

    @pytest.mark.parametrize("method", _OFFSET_READS)
    async def test_an_unregistered_topic_is_a_key_error_with_a_jetstream_context(
        self, method: str
    ) -> None:
        # "`KeyError` for an unregistered topic on `NatsBus`": there is no
        # stream to ask about, whatever state the connection is in.
        js = _StateJetStream(_state(messages=3, first_seq=1, last_seq=3))

        with pytest.raises(KeyError):
            await _read(_bus_with(js), method, _UNREGISTERED_TOPIC)

    @pytest.mark.parametrize("method", _OFFSET_READS)
    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    async def test_a_registered_topic_before_start_is_a_runtime_error(
        self, method: str, spec: TopicSpec
    ) -> None:
        # "`RuntimeError` before `NatsBus.start()`".
        bus = NatsBus(_SERVERS)

        with pytest.raises(RuntimeError):
            await _read(bus, method, spec.name)

    @pytest.mark.parametrize(
        ("messages", "first_seq", "last_seq"),
        [
            pytest.param(5, 16, 20, id="non-empty"),
            pytest.param(0, 0, 0, id="never-written"),
            pytest.param(0, 21, 20, id="emptied"),
        ],
    )
    async def test_end_offset_is_last_seq_plus_one(
        self, messages: int, first_seq: int, last_seq: int
    ) -> None:
        # "`stream_info(...).state.last_seq + 1`" -- "`1` for an empty
        # stream" (decision 9).
        js = _StateJetStream(_state(messages=messages, first_seq=first_seq, last_seq=last_seq))

        assert await _read(_bus_with(js), "end_offset", HOT_IP.name) == last_seq + 1

    @pytest.mark.parametrize(
        ("messages", "first_seq", "last_seq", "expected"),
        [
            pytest.param(5, 16, 20, 16, id="non-empty"),
            pytest.param(0, 0, 0, 1, id="never-written"),
            pytest.param(0, 21, 20, 21, id="emptied"),
            pytest.param(0, 0, 20, 21, id="empty-first-seq-zero"),
        ],
    )
    async def test_first_offset_is_first_offset_of_the_state(
        self, messages: int, first_seq: int, last_seq: int, expected: int
    ) -> None:
        # "returns `first_offset_of(state)`": the pure function's answer on
        # the very state `stream_info` reported, for a stream that holds a
        # message and for an empty one (assumptions 134 and 135).
        state = _state(messages=messages, first_seq=first_seq, last_seq=last_seq)
        js = _StateJetStream(state)

        result = await _read(_bus_with(js), "first_offset", HOT_IP.name)

        assert result == first_offset_of(state) == expected

    async def test_an_empty_streams_first_offset_is_its_end_offset(self) -> None:
        # "When the log retains no message, it is `end_offset(topic)`".
        js = _StateJetStream(_state(messages=0, first_seq=21, last_seq=20))
        bus = _bus_with(js)

        end = await _read(bus, "end_offset", HOT_IP.name)
        first = await _read(bus, "first_offset", HOT_IP.name)

        assert first == end == 21

    @pytest.mark.parametrize("method", _OFFSET_READS)
    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    async def test_stream_info_is_asked_for_the_topics_stream(
        self, method: str, spec: TopicSpec
    ) -> None:
        # Decision 1's stream name for the topic, one `stream_info` call per
        # read (assumption 133: "a second broker round trip").
        js = _StateJetStream(_state(messages=3, first_seq=1, last_seq=3))

        await _read(_bus_with(js), method, spec.name)

        assert js.names == [spec.stream_name]


# --------------------------------------------------------------------------
# NatsBus.last_value: stream_info(...).state, then get_msg at state.last_seq
# --------------------------------------------------------------------------


class _Msg:
    """What `get_msg` answers: an object with `data: bytes | None`, as nats-py's
    `RawStreamMsg.data: Optional[bytes]` (ADR-0013 Amendment 13, Sources)."""

    def __init__(self, data: bytes | None) -> None:
        self.data = data


class _BrokerBoom(Exception):
    """An error from the broker that is not `NotFoundError`: it must propagate."""


class _LastValueJetStream:
    """A stub JetStream context for `NatsBus.last_value`: `stream_info(name)` answers one
    `StreamState` (or raises `info_error`), and `get_msg(...)` answers `_Msg(data)` (or
    raises `get_error`), recording every call with its arguments.

    `get_msg` accepts the stream name positionally or as `stream_name=`, since
    Amendment 13 does not pin which, and records every keyword it receives so a
    test can check that the default form was used."""

    def __init__(
        self,
        state: api.StreamState,
        *,
        data: bytes | None = b"payload",
        get_error: BaseException | None = None,
        info_error: BaseException | None = None,
    ) -> None:
        self.state = state
        self.data = data
        self.get_error = get_error
        self.info_error = info_error
        self.names: list[str] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    async def stream_info(self, name: str | None = None, **params: Any) -> _StateInfo:
        resolved = name if name is not None else params["name"]
        self.names.append(resolved)
        if self.info_error is not None:
            raise self.info_error
        return _StateInfo(self.state)

    async def get_msg(self, stream_name: str | None = None, *args: Any, **params: Any) -> _Msg:
        # nats-py: `get_msg(stream_name, seq=None, subject=None, direct=False,
        # next=False)`. A positional `seq` after the name is folded into the
        # keywords so the assertions read one shape.
        if args:
            params = {"seq": args[0], **params}
        resolved = stream_name if stream_name is not None else params.pop("stream_name")
        self.get_calls.append((resolved, dict(params)))
        if self.get_error is not None:
            raise self.get_error
        return _Msg(self.data)


def _bus_with_last(js: _LastValueJetStream) -> NatsBus:
    """An unstarted `NatsBus` with the stub installed on `_js` (ASSUMPTION 5)."""

    bus = NatsBus(_SERVERS)
    cast(Any, bus)._js = js
    return bus


class TestNatsBusLastValue:
    """ADR-0013 decision 3 as amended by Amendment 13 (ADR-0017 Amendment 3 ruling
    1). "`MessageBus.last_value(topic)` ... is the value of the message the log
    holds at offset `end_offset(topic) - 1`, and `None` when it holds none
    there." "It raises what `end_offset` raises, in the same order": `KeyError`
    for an unregistered topic "whether or not the bus has started", and
    `RuntimeError("NatsBus is not started")` for a registered topic before
    `start()`. "`NatsBus.last_value` reads `stream_info(<stream name>).state`, as
    `end_offset` does. When `state.messages` is `0` it returns `None` and sends
    no second request. Otherwise it reads the message at `state.last_seq` with
    the JetStream context's `get_msg(<stream name>, seq=state.last_seq)`, in its
    default form ... The direct form is not available ... A
    `nats.js.errors.NotFoundError` from that read ... returns `None`. The result
    is the message's data, or `b""` when it carries none." "Any other error from
    the broker propagates, except the one named below." ADR-0017 decision 7, as
    amended 2026-09-24: an exception from `bus.last_value` propagates out of
    `start()`. Reached through a stub context, as ADR-0017 Amendment 3's Test
    seams says ("like the offset reads")."""

    async def test_an_unregistered_topic_is_a_key_error_before_start(self) -> None:
        bus = NatsBus(_SERVERS)

        with pytest.raises(KeyError):
            await bus.last_value(_UNREGISTERED_TOPIC)

    async def test_an_unregistered_topic_is_a_key_error_with_a_jetstream_context(self) -> None:
        js = _LastValueJetStream(_state(messages=3, first_seq=1, last_seq=3))

        with pytest.raises(KeyError):
            await _bus_with_last(js).last_value(_UNREGISTERED_TOPIC)

        assert js.names == []
        assert js.get_calls == []

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    async def test_a_registered_topic_before_start_is_a_runtime_error(
        self, spec: TopicSpec
    ) -> None:
        bus = NatsBus(_SERVERS)

        with pytest.raises(RuntimeError):
            await bus.last_value(spec.name)

    @pytest.mark.parametrize(
        ("first_seq", "last_seq"),
        [
            pytest.param(0, 0, id="never-written"),
            pytest.param(21, 20, id="emptied"),
        ],
    )
    async def test_no_message_is_none_and_no_second_request(
        self, first_seq: int, last_seq: int
    ) -> None:
        # "When `state.messages` is `0` it returns `None` and sends no second
        # request."
        js = _LastValueJetStream(_state(messages=0, first_seq=first_seq, last_seq=last_seq))

        result = await _bus_with_last(js).last_value(PREFIX_STATS.name)

        assert result is None
        assert js.names == [PREFIX_STATS.stream_name]
        assert js.get_calls == []

    @pytest.mark.parametrize("spec", list(all_topics()), ids=[s.name for s in all_topics()])
    async def test_it_reads_the_message_at_last_seq_in_the_default_form(
        self, spec: TopicSpec
    ) -> None:
        # "`get_msg(<stream name>, seq=state.last_seq)`, in its default form:
        # the `STREAM.MSG.GET` API request. The direct form is not available":
        # no truthy `direct`, no `subject`, no `next`.
        js = _LastValueJetStream(_state(messages=5, first_seq=16, last_seq=20), data=b"last")

        result = await _bus_with_last(js).last_value(spec.name)

        assert result == b"last"
        assert js.names == [spec.stream_name]
        assert len(js.get_calls) == 1
        stream_name, params = js.get_calls[0]
        assert stream_name == spec.stream_name
        assert params.get("seq") == 20
        assert not params.get("direct")
        assert params.get("subject") is None
        assert not params.get("next")

    async def test_a_not_found_error_from_get_msg_is_none(self) -> None:
        # "A `nats.js.errors.NotFoundError` from that read, meaning no message
        # at that sequence, returns `None`" (assumption 148): the last
        # message was deleted on its own.
        js = _LastValueJetStream(
            _state(messages=4, first_seq=16, last_seq=20),
            get_error=nats.js.errors.NotFoundError(),
        )

        assert await _bus_with_last(js).last_value(PREFIX_STATS.name) is None
        assert len(js.get_calls) == 1

    async def test_a_message_with_no_data_is_empty_bytes(self) -> None:
        # "The result is the message's data, or `b""` when it carries none."
        js = _LastValueJetStream(_state(messages=1, first_seq=7, last_seq=7), data=None)

        result = await _bus_with_last(js).last_value(PREFIX_STATS.name)

        assert result is not None
        assert result == b""

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(_BrokerBoom("broker says no"), id="other-exception"),
            pytest.param(TimeoutError(), id="timeout"),
        ],
    )
    async def test_any_other_error_from_get_msg_propagates(self, error: Exception) -> None:
        # "Any other error from the broker propagates, except the one named".
        js = _LastValueJetStream(_state(messages=5, first_seq=16, last_seq=20), get_error=error)

        with pytest.raises(type(error)):
            await _bus_with_last(js).last_value(PREFIX_STATS.name)

    async def test_a_not_found_error_from_stream_info_propagates(self) -> None:
        # The exception is `NotFoundError` from `get_msg` only; a missing
        # stream at `stream_info` is not "no message at that sequence".
        js = _LastValueJetStream(
            _state(messages=5, first_seq=16, last_seq=20),
            info_error=nats.js.errors.NotFoundError(),
        )

        with pytest.raises(nats.js.errors.NotFoundError):
            await _bus_with_last(js).last_value(PREFIX_STATS.name)

        assert js.get_calls == []
