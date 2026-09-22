"""`hammertime-provision` CLI (ADR-0013 decision 2, CLI bullet as amended 2026-09-21).

Spec: section 19, section 32, section 33 (the tool's own docstring citation,
ADR-0013 decision 2); section 47.7 (structured log records: one JSON object
per line on stdout, sorted keys, `event`/`level`/`service`; "No record -- of
any event -- may contain a credential", which ADR-0013 Amendment 2 ruling 2
extends to the userinfo of a bus URL).

This package (`tools/provision`) had no `tests/` directory before this file;
it uses the package-internal layout `tools/agent-token` established
(ADR-0013 Amendment 2 assumption 49), collected through the root
`testpaths`' `tools` entry.

Written from ADR-0013 alone -- the decision 2 CLI bullet and its dated
Amendment 2 addendum, decision 3's `bus_endpoints` paragraph, decision 10's
`HAMMERTIME_BUS_BROKERS` row, Amendment 2 rulings 2-3 and assumption 48 --
plus ADR-0009 A1 (the retry schedule) and A7 (the log-record carrier and its
test seams). `__main__.py` was not read. Every name used here is one the ADR
pins:

* Surface: `build_parser() -> argparse.ArgumentParser`, `async def
  provision(servers: list[str], *, replicas: int, timeout_s: float) ->
  dict[str, str]` ("stream name -> action; raises the last transient error
  when `timeout_s` expires, `StreamConfigConflictError` on an immutable
  difference, any other `nats.errors.Error` as itself"), `main(argv:
  Sequence[str] | None = None) -> int`, `TRANSIENT_PROVISION_ERRORS` ("a
  superset of `hammertime.bus.nats.TRANSIENT_ERRORS` that adds
  `nats.js.errors.ServiceUnavailableError`"), `DEFAULT_REPLICAS = 1` and
  `DEFAULT_TIMEOUT_S = 60.0`.
* Arguments: "`--servers` is comma-separated, entries stripped, empties
  dropped, at least one required; `--replicas` MUST be a positive integer
  and `--timeout` a positive number; an invalid invocation is argparse's
  `SystemExit(2)`, and an invalid `HAMMERTIME_LOG_LEVEL` is a
  `config_invalid` record and a returned 2."
* Exit codes: "exit 0 when every stream exists with the declared
  configuration, 1 when the servers are unreachable within `--timeout`
  (default 60 s, retried with ADR-0009 A1's schedule) or a stream conflicts,
  2 on invalid arguments."
* Records, "all through the logger configured by `main()`": `INFO
  event=stream_provisioned stream=<name> action=<created|updated|unchanged>`
  per stream, through `configure_logging("provision", level)`; `ERROR
  stream_conflict stream=<name> field=<f> expected=<e> actual=<a>`; `ERROR
  provision_failed reason=unreachable bus_endpoints=<list> timeout_s=<s>
  error=<str>`; `ERROR provision_failed reason=broker_error
  bus_endpoints=<list> error=<str>`. "The `--servers` value never appears in
  a record: every record that names the servers carries `bus_endpoints =
  hammertime.bus.nats.bus_endpoints(servers)` (decision 3) and nothing else
  derived from the value" -- Amendment 2 ruling 3: "The tool's two
  `provision_failed` records log `bus_endpoints` in place of `servers`."
* Seams: "`main()` calls `provision` by looking it up on its own module at
  call time; `provision` connects with `nats.connect(servers=<the list, as
  given>, ...)` looked up on the `nats` module at call time and reconciles
  with `ensure_streams`, imported by name into the tool's module -- so a test
  replaces `hammertime.tools.provision.__main__.provision`, `nats.connect`
  and `hammertime.tools.provision.__main__.ensure_streams` respectively, and
  no test needs a broker." ADR-0009 A7: `configure_logging` writes "to the
  `sys.stdout` object bound *at the time it is called*", so `capsys`
  captures the records; it is idempotent, so `main()` may run repeatedly.
* Not asserted: the `provision_complete` summary record (Amendment 2
  assumption 48: "not pinned ... tests pin `stream_provisioned`, the three
  failure records and the exit codes"). Records other than the ones named
  above are ignored, never counted or forbidden.

Assumptions this file had to make (each is a detail the text leaves open;
none contradicts it):

1. For an invalid `HAMMERTIME_LOG_LEVEL`, `main()` still emits the
   `config_invalid` record as a JSON line on stdout (the ADR says the
   records go "through the logger configured by `main()`", and A7's carrier
   writes to stdout), so it is captured like every other record.
2. argparse's usage error goes to stderr, so stdout holds only JSON records
   (possibly none) when `SystemExit(2)` escapes; the helper parses every
   non-empty stdout line with `json.loads` and fails loudly otherwise.
3. `ensure_streams`'s `specs` argument may arrive as any iterable (the ADR
   signature says `Iterable[TopicSpec]`); the fake materialises it before
   comparing `.name`s, and accepts `js`/`specs` positionally or by keyword.
4. The fake connection's `jetstream()` is synchronous and returns a plain
   sentinel, since `provision` hands it to `ensure_streams` (stubbed here);
   no JetStream API is exercised. `nats.connect` and `jetstream()` may be
   called with any extra keyword arguments (`**_`).
5. `TestProvision` sleeps for real: exactly the 0.5 s first retry delay of
   ADR-0009 A1, once. ADR-0013 Amendment 1 ruling T12 accepts real sleeps
   of that size.
6. `TestAServerUrlNatsPyCouldNotParse` (decision 2's CLI bullet as amended
   2026-09-22, Amendment 4 ruling S2: "Every `--servers` entry is checked
   with `hammertime.bus.nats.validate_bus_url` ... before anything is
   connected; an entry it refuses is an argparse error -- `SystemExit(2)`, a
   message naming the entry's position and not its text -- so a URL nats-py
   could not parse never reaches nats-py or a record") reads argparse's
   message off stderr, where assumption 2 already places it, and the whole
   of stdout as well. The needles `pa`, `ss` and `user` are the dispatch
   brief's; they exclude ordinary words such as "parse" from the message,
   which must therefore name the position and nothing of the entry.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import nats
import nats.errors
import nats.js.errors
import pytest
from hammertime.bus.nats import TRANSIENT_ERRORS, StreamConfigConflictError
from hammertime.bus.topics import all_topics
from hammertime.tools.provision.__main__ import (
    DEFAULT_REPLICAS,
    DEFAULT_TIMEOUT_S,
    TRANSIENT_PROVISION_ERRORS,
    build_parser,
    main,
    provision,
)

_SERVERS = "nats://nats:4222"
_ENDPOINTS = ["nats://nats:4222"]

# Decision 10 (as amended): the two userinfo forms nats-py reads, `user:password@`
# and `token@`. The raw list is what `provision` must receive; the endpoints
# are what any record may carry.
_SECRET_SERVERS = "nats://user:s3cret@nats:4222,nats://tok3n@other:4222"
_SECRET_RAW = ["nats://user:s3cret@nats:4222", "nats://tok3n@other:4222"]
_SECRET_ENDPOINTS = ["nats://nats:4222", "nats://other:4222"]
_SECRET_NEEDLES = ("s3cret", "tok3n", "user:", "@")

# One entry per `ensure_streams` action value (decision 2: "created" |
# "updated" | "unchanged"), using decision 1's stream names.
_OK_RESULT = {
    "hammertime-observations-v1": "created",
    "hammertime-hot-ip-v1": "unchanged",
    "hammertime-prefix-stats-v1": "updated",
}


@pytest.fixture(autouse=True)
def _default_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    # A7: `HAMMERTIME_LOG_LEVEL` is a threshold and `info` is the default;
    # `stream_provisioned` is an INFO record, so the tests run at the default
    # unless one sets the variable itself.
    monkeypatch.delenv("HAMMERTIME_LOG_LEVEL", raising=False)


# --- the `main()`-level seam: replace `provision` on the tool's module ---------


class _ProvisionStub:
    """Stands in for `provision`; records its arguments, then returns or raises."""

    def __init__(
        self,
        *,
        result: dict[str, str] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.result = dict(_OK_RESULT) if result is None else dict(result)
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, servers: list[str], *, replicas: int, timeout_s: float
    ) -> dict[str, str]:
        self.calls.append({"servers": list(servers), "replicas": replicas, "timeout_s": timeout_s})
        if self.raises is not None:
            raise self.raises
        return dict(self.result)


def _install(monkeypatch: pytest.MonkeyPatch, stub: _ProvisionStub) -> None:
    # Decision 2: "`main()` calls `provision` by looking it up on its own
    # module at call time".
    monkeypatch.setattr("hammertime.tools.provision.__main__.provision", stub)


@dataclass(frozen=True)
class _Run:
    code: int | str | None
    out: str
    records: list[dict[str, Any]] = field(default_factory=list)


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> _Run:
    """Run `main(argv)`, absorbing argparse's `SystemExit`; parse stdout as JSON lines."""
    capsys.readouterr()
    code: int | str | None
    try:
        code = main(argv)
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr().out
    # Spec 47.7: "one JSON object per line on standard output".
    lines = [line for line in out.splitlines() if line.strip()]
    records: list[dict[str, Any]] = [json.loads(line) for line in lines]
    return _Run(code=code, out=out, records=records)


def _only(records: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("event") == event]


def _one(records: list[dict[str, Any]], event: str) -> dict[str, Any]:
    matching = _only(records, event)
    assert len(matching) == 1, f"expected exactly one {event!r} record, got {matching!r}"
    return matching[0]


# --- 1. invalid arguments: argparse's SystemExit(2), `provision` never called ---


class TestInvalidArgumentsExitTwo:
    """Decision 2: "2 on invalid arguments"; "an invalid invocation is argparse's

    `SystemExit(2)`".
    """

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-servers"),
            pytest.param(["--servers", ""], id="servers-empty"),
            pytest.param(["--servers", " , "], id="servers-only-separators"),
            pytest.param(["--servers", _SERVERS, "--replicas", "0"], id="replicas-zero"),
            pytest.param(["--servers", _SERVERS, "--replicas", "-1"], id="replicas-negative"),
            pytest.param(["--servers", _SERVERS, "--replicas", "1.5"], id="replicas-float"),
            pytest.param(["--servers", _SERVERS, "--replicas", "x"], id="replicas-text"),
            pytest.param(["--servers", _SERVERS, "--timeout", "0"], id="timeout-zero"),
            pytest.param(["--servers", _SERVERS, "--timeout", "-1"], id="timeout-negative"),
            pytest.param(["--servers", _SERVERS, "--timeout", "x"], id="timeout-text"),
        ],
    )
    def test_invalid_invocation_is_system_exit_two_and_provision_is_never_called(
        self,
        argv: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # "`--servers` is comma-separated, entries stripped, empties dropped, at
        # least one required; `--replicas` MUST be a positive integer and
        # `--timeout` a positive number".
        stub = _ProvisionStub()
        _install(monkeypatch, stub)

        with pytest.raises(SystemExit) as excinfo:
            main(argv)

        assert excinfo.value.code == 2
        assert stub.calls == []
        capsys.readouterr()

    def test_parser_defaults_are_the_pinned_constants(self) -> None:
        # Decision 2: "`DEFAULT_REPLICAS = 1` and `DEFAULT_TIMEOUT_S = 60.0`";
        # "`--timeout` (default 60 s)". argparse's default `dest` per flag.
        namespace = build_parser().parse_args(["--servers", _SERVERS])

        assert namespace.servers == ["nats://nats:4222"]
        assert namespace.replicas == DEFAULT_REPLICAS == 1
        assert namespace.timeout == DEFAULT_TIMEOUT_S == 60.0

    def test_servers_are_split_stripped_and_empties_dropped(self) -> None:
        # Decision 2: "`--servers` is comma-separated, entries stripped,
        # empties dropped".
        namespace = build_parser().parse_args(["--servers", "nats://a:4222, nats://b:4222,"])

        assert namespace.servers == ["nats://a:4222", "nats://b:4222"]


# The entry decision 3 (as amended, Amendment 4 ruling S2) names: nats-py's
# own parse raises `ValueError("Port could not be cast to integer value as
# 's3cr'")`, echoing the port token. Neither half of the password, nor the
# whole of it, nor the username, may reach stderr or stdout; the tokens are
# chosen so that none is a substring of argparse's usage text, of the fixed
# refusal text or of ordinary English.
_UNPARSEABLE_SERVERS = "nats://usr7:s3cr/et@nats:4222"
_UNPARSEABLE_NEEDLES = ("usr7", "s3cr", "et@", "s3cr/et")


class TestAServerUrlNatsPyCouldNotParse:
    """Decision 2's CLI bullet as amended (Amendment 4 ruling S2): "an entry it refuses
    is an argparse error -- `SystemExit(2)`, a message naming the entry's
    position and not its text -- so a URL nats-py could not parse never reaches
    nats-py or a record" (assumption 6 in the module docstring)."""

    def test_it_is_system_exit_two_and_provision_is_never_called(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub = _ProvisionStub()
        _install(monkeypatch, stub)

        with pytest.raises(SystemExit) as excinfo:
            main(["--servers", _UNPARSEABLE_SERVERS])

        assert excinfo.value.code == 2
        # "never reaches nats-py": `provision` -- and so `nats.connect` -- is
        # not called with it.
        assert stub.calls == []
        capsys.readouterr()

    @pytest.mark.parametrize("needle", _UNPARSEABLE_NEEDLES)
    def test_nothing_of_the_entry_reaches_stderr_or_stdout(
        self, needle: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "a message naming the entry's position and not its text"; spec 47.7
        # for whatever records were written.
        assert needle in _UNPARSEABLE_SERVERS
        _install(monkeypatch, _ProvisionStub())
        capsys.readouterr()

        with pytest.raises(SystemExit):
            main(["--servers", _UNPARSEABLE_SERVERS])

        captured = capsys.readouterr()
        assert needle not in captured.err, f"{needle!r} leaked into stderr: {captured.err!r}"
        assert needle not in captured.out, f"{needle!r} leaked into stdout: {captured.out!r}"
        assert _UNPARSEABLE_SERVERS not in captured.err
        assert _UNPARSEABLE_SERVERS not in captured.out

    def test_a_refused_entry_after_a_good_one_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "Every `--servers` entry is checked": the position named is the
        # second's, and the first's validity does not excuse it.
        stub = _ProvisionStub()
        _install(monkeypatch, stub)

        with pytest.raises(SystemExit) as excinfo:
            main(["--servers", f"{_SERVERS},{_UNPARSEABLE_SERVERS}"])

        assert excinfo.value.code == 2
        assert stub.calls == []
        captured = capsys.readouterr()
        for needle in _UNPARSEABLE_NEEDLES:
            assert needle in _UNPARSEABLE_SERVERS
            assert needle not in captured.err

    def test_userinfo_nats_py_reads_is_not_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "no value that connected before is refused now" (decision 3, as
        # amended): the two userinfo forms of decision 10 still reach
        # `provision` as given.
        stub = _ProvisionStub()
        _install(monkeypatch, stub)

        run = _run(["--servers", _SECRET_SERVERS], capsys)

        assert run.code == 0
        assert stub.calls == [{"servers": _SECRET_RAW, "replicas": 1, "timeout_s": 60.0}]


# --- 2. invalid HAMMERTIME_LOG_LEVEL: config_invalid record, returned 2 -------------


class TestInvalidLogLevel:
    """Decision 2: "an invalid `HAMMERTIME_LOG_LEVEL` is a `config_invalid` record and a

    returned 2".
    """

    def test_returns_two_with_a_config_invalid_record_and_never_provisions(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HAMMERTIME_LOG_LEVEL", "bogus")
        stub = _ProvisionStub()
        _install(monkeypatch, stub)
        capsys.readouterr()

        # Called directly, not through `_run`: a `SystemExit` here must fail
        # the test -- the ADR says *returned* 2, not argparse's exit.
        code = main(["--servers", _SERVERS])

        out = capsys.readouterr().out
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        assert code == 2
        record = _one(records, "config_invalid")
        # A7's table: `config_invalid` is `error` with an `error` field;
        # `service` is "the `name` given to `configure_logging`" -- "provision".
        assert record["level"] == "error"
        assert "error" in record
        assert record["service"] == "provision"
        assert stub.calls == []


# --- 3. exit 0: one stream_provisioned record per stream ---------------------------


class TestSuccess:
    """Decision 2: "exit 0 when every stream exists with the declared configuration"."""

    def test_returns_zero_and_logs_one_stream_provisioned_record_per_stream(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "It logs one `INFO event=stream_provisioned stream=<name>
        # action=<created|updated|unchanged>` per stream through
        # `configure_logging("provision", level)`."
        _install(monkeypatch, _ProvisionStub(result=_OK_RESULT))

        run = _run(["--servers", _SERVERS], capsys)

        assert run.code == 0
        records = _only(run.records, "stream_provisioned")
        assert len(records) == len(_OK_RESULT)
        assert {(r["stream"], r["action"]) for r in records} == set(_OK_RESULT.items())
        for record in records:
            assert record["level"] == "info"
            assert record["service"] == "provision"

    def test_provision_receives_the_defaults(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `provision(servers, *, replicas, timeout_s)` with `DEFAULT_REPLICAS =
        # 1` and `DEFAULT_TIMEOUT_S = 60.0` when neither flag is given.
        stub = _ProvisionStub()
        _install(monkeypatch, stub)

        run = _run(["--servers", _SERVERS], capsys)

        assert run.code == 0
        assert stub.calls == [{"servers": ["nats://nats:4222"], "replicas": 1, "timeout_s": 60.0}]

    def test_provision_receives_replicas_and_timeout_flags(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `hammertime-provision --servers <urls> [--replicas N] [--timeout S]`.
        stub = _ProvisionStub()
        _install(monkeypatch, stub)

        run = _run(["--servers", _SERVERS, "--replicas", "3", "--timeout", "2.5"], capsys)

        assert run.code == 0
        assert stub.calls == [{"servers": ["nats://nats:4222"], "replicas": 3, "timeout_s": 2.5}]


# --- 4. exit 1, unreachable ----------------------------------------------------------


_UNREACHABLE_ERRORS = [
    pytest.param(nats.errors.NoServersError(), id="NoServersError"),
    pytest.param(OSError("connection refused"), id="OSError"),
    pytest.param(nats.js.errors.ServiceUnavailableError(), id="ServiceUnavailableError"),
]


class TestUnreachable:
    """Decision 2: "1 when the servers are unreachable within `--timeout`" and its record.

    "`ERROR provision_failed reason=unreachable bus_endpoints=<list> timeout_s=<s>
    error=<str>` when the deadline expired (exit 1)". All three exception types are
    members of `TRANSIENT_PROVISION_ERRORS` (`OSError` and `NoServersError` via
    `TRANSIENT_ERRORS`; `ServiceUnavailableError` is the one the tool adds).
    """

    @pytest.mark.parametrize("error", _UNREACHABLE_ERRORS)
    def test_returns_one_with_a_provision_failed_unreachable_record(
        self,
        error: BaseException,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _install(monkeypatch, _ProvisionStub(raises=error))

        run = _run(["--servers", _SERVERS, "--timeout", "2.5"], capsys)

        assert run.code == 1
        record = _one(run.records, "provision_failed")
        assert record["level"] == "error"
        assert record["service"] == "provision"
        assert record["reason"] == "unreachable"
        assert record["bus_endpoints"] == _ENDPOINTS
        assert record["timeout_s"] == 2.5
        assert "error" in record
        # Ruling 3: `bus_endpoints` "in place of `servers`".
        assert "servers" not in record

    def test_timeout_s_is_the_default_when_no_flag_is_given(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _install(monkeypatch, _ProvisionStub(raises=nats.errors.NoServersError()))

        run = _run(["--servers", _SERVERS], capsys)

        assert run.code == 1
        record = _one(run.records, "provision_failed")
        assert record["reason"] == "unreachable"
        assert record["timeout_s"] == DEFAULT_TIMEOUT_S


# --- 5. exit 1, broker error ---------------------------------------------------------


class TestBrokerError:
    """Decision 2: "`ERROR provision_failed reason=broker_error bus_endpoints=<list> error=<str>`

    for any other `nats.errors.Error` (exit 1)".
    """

    def test_returns_one_with_a_provision_failed_broker_error_record(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _install(monkeypatch, _ProvisionStub(raises=nats.errors.Error("boom")))

        run = _run(["--servers", _SERVERS], capsys)

        assert run.code == 1
        record = _one(run.records, "provision_failed")
        assert record["level"] == "error"
        assert record["service"] == "provision"
        assert record["reason"] == "broker_error"
        assert record["bus_endpoints"] == _ENDPOINTS
        assert record["error"] == "boom"
        assert "servers" not in record


# --- 6. exit 1, stream conflict --------------------------------------------------------


class TestStreamConflict:
    """Decision 2: exit 1 when "a stream conflicts"; the `stream_conflict` record.

    "`ERROR stream_conflict stream=<name> field=<f> expected=<e> actual=<a>` (exit 1)".
    """

    def test_returns_one_with_a_stream_conflict_record(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = StreamConfigConflictError(
            "hammertime-observations-v1", "retention", "limits", "workqueue"
        )
        _install(monkeypatch, _ProvisionStub(raises=error))

        run = _run(["--servers", _SERVERS], capsys)

        assert run.code == 1
        record = _one(run.records, "stream_conflict")
        assert record["level"] == "error"
        assert record["service"] == "provision"
        assert record["stream"] == "hammertime-observations-v1"
        assert record["field"] == "retention"
        assert record["expected"] == "limits"
        assert record["actual"] == "workqueue"


# --- 7. redaction: no record carries the userinfo of a bus URL ---------------------


class TestRedaction:
    """Spec 47.7 / Amendment 2 ruling 2: "The rendered text of every record ... MUST NOT

    contain the userinfo of a bus URL." Decision 2: "The `--servers` value never
    appears in a record: every record that names the servers carries `bus_endpoints
    = hammertime.bus.nats.bus_endpoints(servers)` (decision 3) and nothing else
    derived from the value". Decision 10 (as amended): "the value is passed to
    `nats.connect` as given" -- the reduction is for logs only.
    """

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            pytest.param(nats.errors.NoServersError(), "unreachable", id="unreachable"),
            pytest.param(nats.errors.Error("boom"), "broker_error", id="broker_error"),
        ],
    )
    def test_no_userinfo_reaches_stdout_and_provision_gets_the_raw_list(
        self,
        error: BaseException,
        reason: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stub = _ProvisionStub(raises=error)
        _install(monkeypatch, stub)

        run = _run(["--servers", _SECRET_SERVERS], capsys)

        assert run.code == 1
        # The *entire* stdout, not just the failing record: every record of
        # every event is bound by 47.7.
        for needle in _SECRET_NEEDLES:
            assert needle not in run.out, f"{needle!r} leaked into stdout"
        record = _one(run.records, "provision_failed")
        assert record["reason"] == reason
        # Decision 3's examples: `nats://user:s3cret@nats:4222` -> `nats://nats:4222`;
        # `nats://s3cret-token@nats:4222` -> `nats://nats:4222`.
        assert record["bus_endpoints"] == _SECRET_ENDPOINTS
        assert "servers" not in record
        # "as given": userinfo intact on the way to the client.
        assert stub.calls == [{"servers": _SECRET_RAW, "replicas": 1, "timeout_s": 60.0}]


# --- 8. TRANSIENT_PROVISION_ERRORS -------------------------------------------------------


class TestTransientProvisionErrors:
    """Decision 2: "`TRANSIENT_PROVISION_ERRORS` (a superset of

    `hammertime.bus.nats.TRANSIENT_ERRORS` that adds
    `nats.js.errors.ServiceUnavailableError` -- a server whose JetStream is not yet
    serving is 'unreachable' for provisioning)". Decision 3: "A rejected credential
    is **not** in it: nats.py raises `nats.errors.AuthorizationError` ... which
    subclass `nats.errors.Error` and none of the classes listed".
    """

    def test_is_a_tuple_of_exception_types(self) -> None:
        assert isinstance(TRANSIENT_PROVISION_ERRORS, tuple)
        assert all(
            isinstance(member, type) and issubclass(member, BaseException)
            for member in TRANSIENT_PROVISION_ERRORS
        )

    def test_contains_every_member_of_the_bus_transient_tuple(self) -> None:
        for member in TRANSIENT_ERRORS:
            assert member in TRANSIENT_PROVISION_ERRORS, member

    def test_adds_service_unavailable_error(self) -> None:
        assert nats.js.errors.ServiceUnavailableError in TRANSIENT_PROVISION_ERRORS

    def test_an_authentication_failure_is_not_transient(self) -> None:
        # ADR-0009 A1's credential rule: "A credential the dependency actively
        # rejects will never become valid by waiting, so it MUST fail ... on
        # the first attempt: no retry".
        assert not issubclass(nats.errors.AuthorizationError, TRANSIENT_PROVISION_ERRORS)


# --- 9. provision() itself: nats.connect and ensure_streams stubbed ---------------------


class _FakeConnection:
    """What `fake_connect` returns: `jetstream()` and an awaitable `close()`."""

    def __init__(self) -> None:
        self.js = object()
        self.close_calls = 0

    def jetstream(self, **_: Any) -> object:
        return self.js

    async def close(self) -> None:
        self.close_calls += 1


class _FakeConnect:
    """Stands in for `nats.connect`; raises per scheduled failure, else connects."""

    def __init__(
        self,
        *,
        failures: list[BaseException] | None = None,
        always: BaseException | None = None,
    ) -> None:
        self.failures = list(failures or [])
        self.always = always
        self.calls: list[list[str]] = []
        self.connections: list[_FakeConnection] = []

    async def __call__(self, servers: Iterable[str], **_: Any) -> _FakeConnection:
        self.calls.append(list(servers))
        if self.always is not None:
            raise self.always
        if self.failures:
            raise self.failures.pop(0)
        connection = _FakeConnection()
        self.connections.append(connection)
        return connection


class _FakeEnsureStreams:
    """Stands in for `ensure_streams(js, specs, *, replicas)` on the tool's module."""

    def __init__(
        self,
        *,
        result: dict[str, str] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.result = dict(_OK_RESULT) if result is None else dict(result)
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, js: object, specs: Iterable[Any], *, replicas: int = DEFAULT_REPLICAS
    ) -> dict[str, str]:
        names = [spec.name for spec in specs]
        self.calls.append({"js": js, "names": names, "replicas": replicas})
        if self.raises is not None:
            raise self.raises
        return dict(self.result)


def _install_provision_seams(
    monkeypatch: pytest.MonkeyPatch, connect: _FakeConnect, ensure: _FakeEnsureStreams
) -> None:
    # Decision 2: "`provision` connects with `nats.connect(servers=<the list, as
    # given>, ...)` looked up on the `nats` module at call time and reconciles
    # with `ensure_streams`, imported by name into the tool's module".
    monkeypatch.setattr(nats, "connect", connect)
    monkeypatch.setattr("hammertime.tools.provision.__main__.ensure_streams", ensure)


class TestProvision:
    """`provision(servers, *, replicas, timeout_s)`: decision 2, with ADR-0009 A1's schedule."""

    async def test_success_returns_ensure_streams_mapping_over_all_topics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decision 2: the tool "runs `ensure_streams` over `all_topics()`";
        # `provision(...) -> dict[str, str]` is "stream name -> action".
        connect = _FakeConnect()
        ensure = _FakeEnsureStreams(result=_OK_RESULT)
        _install_provision_seams(monkeypatch, connect, ensure)

        result = await provision(["nats://nats:4222"], replicas=3, timeout_s=5.0)

        assert result == _OK_RESULT
        assert len(connect.connections) == 1
        connection = connect.connections[0]
        assert len(ensure.calls) == 1
        call = ensure.calls[0]
        assert call["js"] is connection.js
        assert call["names"] == [spec.name for spec in all_topics()]
        assert call["replicas"] == 3
        assert connection.close_calls == 1

    async def test_a_transient_first_failure_is_retried_after_the_first_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decision 2: "retried with ADR-0009 A1's schedule"; A1: "`delay = 0.5`
        # ... `await sleep(delay)`; `delay = min(delay * 2, 5.0)`" -- one real
        # 0.5 s sleep here (assumption 5 in the module docstring).
        connect = _FakeConnect(failures=[nats.errors.NoServersError()])
        ensure = _FakeEnsureStreams(result=_OK_RESULT)
        _install_provision_seams(monkeypatch, connect, ensure)

        started = time.monotonic()
        result = await provision(["nats://nats:4222"], replicas=1, timeout_s=5.0)
        elapsed = time.monotonic() - started

        assert result == _OK_RESULT
        assert len(connect.calls) == 2
        assert 0.4 <= elapsed < 5.0
        assert connect.connections[0].close_calls == 1

    async def test_gives_up_with_the_last_transient_error_when_the_budget_is_below_the_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A1 step 4: "If `deadline - monotonic() <= delay`: re-raise that same
        # exception. The sleep that would overrun the deadline is never
        # started" -- with `timeout_s=0.1 < 0.5` there is one attempt and no
        # sleep. Decision 2: `provision` "raises the last transient error when
        # `timeout_s` expires".
        error = nats.errors.NoServersError()
        connect = _FakeConnect(always=error)
        ensure = _FakeEnsureStreams()
        _install_provision_seams(monkeypatch, connect, ensure)

        started = time.monotonic()
        with pytest.raises(nats.errors.NoServersError) as excinfo:
            await provision(["nats://nats:4222"], replicas=1, timeout_s=0.1)
        elapsed = time.monotonic() - started

        assert excinfo.value is error
        assert elapsed < 0.4
        assert len(connect.calls) == 1
        assert ensure.calls == []

    async def test_a_stream_conflict_propagates_unchanged_without_a_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decision 2: `provision` raises "`StreamConfigConflictError` on an
        # immutable difference"; it is not in any transient tuple (decision 3
        # lists `TRANSIENT_ERRORS`; decision 2 adds only
        # `ServiceUnavailableError`), so A1's "A non-transient failure
        # propagates from the first attempt unchanged" applies.
        error = StreamConfigConflictError(
            "hammertime-observations-v1", "retention", "limits", "workqueue"
        )
        connect = _FakeConnect()
        ensure = _FakeEnsureStreams(raises=error)
        _install_provision_seams(monkeypatch, connect, ensure)

        with pytest.raises(StreamConfigConflictError) as excinfo:
            await provision(["nats://nats:4222"], replicas=1, timeout_s=5.0)

        assert excinfo.value is error
        assert len(connect.calls) == 1
        assert connect.connections[0].close_calls == 1

    async def test_servers_reach_nats_connect_as_given_with_userinfo_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decision 10 (as amended): "the value is passed to `nats.connect` as
        # given"; decision 2: "`nats.connect(servers=<the list, as given>, ...)`".
        connect = _FakeConnect()
        ensure = _FakeEnsureStreams()
        _install_provision_seams(monkeypatch, connect, ensure)

        await provision(list(_SECRET_RAW), replicas=1, timeout_s=5.0)

        assert connect.calls == [_SECRET_RAW]
