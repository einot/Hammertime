"""`hammertime-agent-token` CLI (spec 36.4, ADR-0006 decision 3 "provisioning tool design").

This package (`tools/agent-token`) had no `tests/` directory before this
file; it is added here mirroring the package-internal `tests/` convention
already used elsewhere in this repo (e.g.
`services/ingest/src/hammertime/ingest/tests/`), since neither
`tools/agent-sim` nor `tools/replay` has an established test layout of its
own to follow (both are unimplemented stubs at the time of writing).

Written blind to `__main__.py`'s implementation logic, per this repo's
test-author convention; the CLI surface below (subcommands, flags,
`main(argv=...) -> int`) is taken from ADR-0006 decision 3's provisioning
tool listing and spec 36.4, cross-checked against `__main__.py` only for
exact flag/subcommand names (`--key`, `--key-file`, `--allow-weak`,
`--force`, `--drop-previous`, `--registry`, `--in`/`--out`), not for the
behavior under test:

* `main(argv: Sequence[str] | None = None) -> int` -- exit code, no
  exception escapes for any `HammertimeError` (ADR-0006 decision 3 /
  `hammertime.core.errors.HammertimeError` hierarchy); invoked directly and
  its stdout/stderr captured with `capsys`, matching this repo's other
  CLI-adjacent tests.
* Every subcommand that reads a deployment key accepts `--key VALUE` or
  `--key-file PATH` as alternatives to `HAMMERTIME_INGEST_AGENT_TOKEN_KEY`
  ("Every subcommand reads HAMMERTIME_INGEST_AGENT_TOKEN_KEY and fails
  loudly without it" -- ADR-0006 decision 3; `--key-file` is this file's
  own addition on top of the ADR text, per the fix commit, so it is the
  primary thing under test here).
* `hash --allow-weak` is spec 36.4's "explicit... escape hatch": "an
  externally supplied token shorter than 32 characters is rejected unless
  explicitly overridden."
* `migrate --in ... --out ...`: "Writes out of place and refuses
  `--out == --in`" (ADR-0006 decision 3); refusing to clobber a non-empty
  `--out` without `--force` is the fix commit's addition on top of that.
* The duplicate-JSON-key rejection is the same loader-level check as
  `hammertime.ingest.auth.agents`'s (see `test_auth.py`'s
  `TestRegistryFileDuplicateJsonKeys`), exercised here through the CLI's
  own registry-reading path instead.

If any exact flag/subcommand name above turns out to differ, that is a
reconciliation gap to flag back rather than resolve by reading the
implementation's logic.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from hammertime.core.auth.tokens import derive_key_id
from hammertime.tools.agent_token.__main__ import main

TEST_KEY_RAW = b"7" * 32
TEST_KEY_B64 = base64.urlsafe_b64encode(TEST_KEY_RAW).rstrip(b"=").decode("ascii")
TEST_KEY_ID = derive_key_id(TEST_KEY_RAW)

# A token shorter than the 32-character strength floor (spec 36.4).
WEAK_TOKEN = "too-short"

# Long enough to pass the strength floor -- used only where a plausible v1
# plaintext token is needed and its exact value is irrelevant.
_V1_TOKEN = "a-plausible-v1-plaintext-token-1234567890"


def _v1_document(agent_id: str = "edge-1", token: str = _V1_TOKEN) -> dict[str, object]:
    return {agent_id: {"token": token, "enabled": True}}


class TestKeyFileAsAlternativeToKey:
    def test_key_file_works_for_key_id(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text(TEST_KEY_B64 + "\n")  # trailing whitespace must be stripped

        exit_code = main(["key-id", "--key-file", str(key_file)])

        assert exit_code == 0

    def test_key_file_prints_the_same_key_id_as_key(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text(TEST_KEY_B64)

        exit_code = main(["key-id", "--key-file", str(key_file)])
        via_file = capsys.readouterr().out.strip()

        assert exit_code == 0
        assert via_file == TEST_KEY_ID

        exit_code = main(["key-id", "--key", TEST_KEY_B64])
        via_key = capsys.readouterr().out.strip()

        assert exit_code == 0
        assert via_key == via_file

    def test_key_file_works_for_generate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text(TEST_KEY_B64)

        exit_code = main(["generate", "--agent-id", "edge-1", "--key-file", str(key_file)])

        assert exit_code == 0
        assert capsys.readouterr().out.strip() != ""

    def test_key_file_works_for_migrate(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text(TEST_KEY_B64)
        in_path = tmp_path / "agents.v1.json"
        in_path.write_text(json.dumps(_v1_document()))
        out_path = tmp_path / "agents.v2.json"

        exit_code = main(
            ["migrate", "--in", str(in_path), "--out", str(out_path), "--key-file", str(key_file)]
        )

        assert exit_code == 0
        assert out_path.exists()


class TestKeyAndKeyFileAreMutuallyExclusive:
    def test_passing_both_key_and_key_file_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text(TEST_KEY_B64)

        exit_code = main(["key-id", "--key", TEST_KEY_B64, "--key-file", str(key_file)])

        assert exit_code != 0
        assert "Traceback" not in capsys.readouterr().err


class TestHashAllowWeak:
    def test_allow_weak_permits_a_short_token_and_warns_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(WEAK_TOKEN))

        exit_code = main(["hash", "--agent-id", "edge-1", "--allow-weak", "--key", TEST_KEY_B64])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "allow-weak" in captured.err
        assert captured.out.strip() != ""

    def test_without_allow_weak_the_same_input_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(WEAK_TOKEN))

        exit_code = main(["hash", "--agent-id", "edge-1", "--key", TEST_KEY_B64])

        captured = capsys.readouterr()
        assert exit_code != 0
        assert "Traceback" not in captured.err
        # No hash entry is printed for a rejected input.
        assert captured.out.strip() == ""


class TestMigrateRefusesToClobberANonEmptyOutFile:
    def test_refuses_without_force(self, tmp_path: Path) -> None:
        in_path = tmp_path / "agents.v1.json"
        in_path.write_text(json.dumps(_v1_document()))
        out_path = tmp_path / "agents.v2.json"
        original_contents = '{"not": "empty"}'
        out_path.write_text(original_contents)

        exit_code = main(
            ["migrate", "--in", str(in_path), "--out", str(out_path), "--key", TEST_KEY_B64]
        )

        assert exit_code != 0
        # The pre-existing file must survive untouched.
        assert out_path.read_text() == original_contents

    def test_succeeds_with_force(self, tmp_path: Path) -> None:
        in_path = tmp_path / "agents.v1.json"
        in_path.write_text(json.dumps(_v1_document(agent_id="edge-1")))
        out_path = tmp_path / "agents.v2.json"
        out_path.write_text('{"not": "empty"}')

        exit_code = main(
            [
                "migrate",
                "--in",
                str(in_path),
                "--out",
                str(out_path),
                "--key",
                TEST_KEY_B64,
                "--force",
            ]
        )

        assert exit_code == 0
        written = json.loads(out_path.read_text())
        assert written["registry_version"] == 2
        assert "edge-1" in written["agents"]

    def test_an_empty_existing_out_file_does_not_need_force(self, tmp_path: Path) -> None:
        # Control: the refusal above is about non-emptiness specifically,
        # not mere pre-existence of the path.
        in_path = tmp_path / "agents.v1.json"
        in_path.write_text(json.dumps(_v1_document(agent_id="edge-1")))
        out_path = tmp_path / "agents.v2.json"
        out_path.write_text("")

        exit_code = main(
            ["migrate", "--in", str(in_path), "--out", str(out_path), "--key", TEST_KEY_B64]
        )

        assert exit_code == 0


class TestMigrateMissingInputFile:
    def test_missing_in_file_is_a_clean_cli_error_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing_in = tmp_path / "does-not-exist.json"
        out_path = tmp_path / "agents.v2.json"

        exit_code = main(
            ["migrate", "--in", str(missing_in), "--out", str(out_path), "--key", TEST_KEY_B64]
        )

        captured = capsys.readouterr()
        assert exit_code != 0
        assert exit_code == 1
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out
        assert not out_path.exists()


class TestDuplicateJsonKeyRejectedThroughTheCli:
    # Same loader-level duplicate-key check as
    # hammertime.ingest.auth.agents's (test_auth.py's
    # TestRegistryFileDuplicateJsonKeys), exercised here through the CLI's
    # own v2-registry-reading path (`rotate --registry`), which is the
    # tool's read-an-existing-v2-registry entry point.

    def _hand_crafted_registry_with_duplicate_agent_id(self) -> str:
        return (
            "{\n"
            '  "registry_version": 2,\n'
            '  "hash_algorithm": "hmac-sha256",\n'
            f'  "key_id": "{TEST_KEY_ID}",\n'
            '  "agents": {\n'
            f'    "edge-17": {{"token_hash": "{"a" * 64}"}},\n'
            f'    "edge-17": {{"token_hash": "{"b" * 64}"}}\n'
            "  }\n"
            "}\n"
        )

    def test_rotate_rejects_a_registry_with_a_duplicate_agent_id_json_key(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        raw = self._hand_crafted_registry_with_duplicate_agent_id()
        registry_path = tmp_path / "agents.v2.json"
        registry_path.write_text(raw)

        exit_code = main(
            [
                "rotate",
                "--agent-id",
                "edge-17",
                "--registry",
                str(registry_path),
                "--key",
                TEST_KEY_B64,
            ]
        )

        captured = capsys.readouterr()
        assert exit_code != 0
        assert "Traceback" not in captured.err
        # The file must be untouched -- rotation must fail before any write.
        assert registry_path.read_text() == raw
