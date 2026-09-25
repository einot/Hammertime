"""Behaviour of `.claude/hooks/bash-guard.sh` under ADR-0018's policy features.

ADR-0018 (`docs/adr/0018-coder-bash-policy-literal-commands-and-a-tripwire.md`)
gives the `coder` agent a Bash policy. It adds five knobs to the guard --
`LITERAL_ONLY`, `DENY_ADVICE`, `ALLOW_UV_RUN_TARGETS`, `ALLOW_MAKE_TARGETS` and
`WRITE_DENY_GLOBS` -- together with a built-in list of the commands the script
knows how to vet, and rules for `uv`, `make`, pytest, ruff and git's writing
subcommands. Every expectation here is taken from the ADR's decisions 3-15, never
from the script's code: the ADR is the specification, and where it and the
script's header disagree, the ADR wins.

The guard is run as a real subprocess, fed a PreToolUse JSON payload on stdin
and parametrised through the same env-var assignments a policy in
`.claude/settings.json` puts on its command line -- the same harness
`test_path_guard_behavior.py` uses for the path guard.

Groups, each citing the decision it encodes:

* A -- contract and routing (decision 11, "The guard never emits an allow
  decision"; `SCOPE_AGENT_TYPES` routing);
* B -- configuration hygiene (decisions 3, 4, 5 and 7);
* C -- literal mode (decision 3);
* D -- `uv` (decision 5), including `--locked` (step 6) and shadowing (step 4);
* E -- pytest's options and operands (decision 6);
* F -- ruff, and `WRITE_DENY_GLOBS` (decision 7);
* G -- make (decision 8);
* H -- git for the coder (decision 9);
* I -- denial advice, `DENY_ADVICE` (decision 11);
* J -- the incident (decision 1's table), against the configured coder policy;
* K -- the policies as configured in `.claude/settings.json` (decision 14);
* O -- the NUL gate and literal-mode control characters (decisions 2, 3 and 11,
  second amendment of 2026-09-24).

Groups J and K read `.claude/settings.json`, so the coder half of them fails
until the top-level session applies decision 14 (step W). Everything else
fails until brief C1 lands. That is intended; nothing here is xfailed.

ADR-0018's fifth amendment (2026-09-25) adds decision 19, which group P
encodes (brief T4, item 7). A payload the guard cannot read as one tool call
-- not exactly one JSON object, a `tool_input` that is not an object, a
`tool_name` that is not a string, a `cwd` or `agent_type` that is neither a
string nor `null` -- is refused with the bash shape denial. The check runs
before the `SCOPE_AGENT_TYPES` routing, for every caller and under every
policy, and the denial goes through `deny`, so it carries decision 11's
paragraph exactly when the policy's `DENY_ADVICE` asks for it. A guard that
would end with any status other than 0 or 2 denies instead, with the backstop
denial, which never carries the paragraph. When `deny`'s own `jq` fails, the
reason goes to stderr and the exit is still 2. Group P fails until brief C6
lands, except its controls, which pass today. A `command` that is not a
string still meets decision 3's could-not-be-checked denial (group O).
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_DIR = REPO_ROOT / ".claude"
GUARD = CLAUDE_DIR / "hooks" / "bash-guard.sh"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"

BASH = shutil.which("bash")
JQ = shutil.which("jq")

pytestmark = pytest.mark.skipif(
    BASH is None or JQ is None,
    reason="bash-guard.sh is a bash script that shells out to jq; both must be installed",
)

# Policy variables the guard reads (ADR-0018 brief T1). They are cleared from the
# inherited environment before every run, so a stray value in a developer's
# shell cannot change a verdict.
POLICY_VARS = (
    "ALLOW_CMDS",
    "ALLOW_GIT_SUBCMDS",
    "ALLOW_NODE_SCRIPTS",
    "SCOPE_AGENT_TYPES",
    "LITERAL_ONLY",
    "DENY_ADVICE",
    "ALLOW_UV_RUN_TARGETS",
    "ALLOW_MAKE_TARGETS",
    "WRITE_DENY_GLOBS",
)

# Every message begins with this (decision 11). It is how a live guard is told
# apart from the harness's refusal and from the platform sandbox.
PREFIX = "Hammertime bash guard: "

# Decision 11's paragraph, verbatim, with the ADR's blockquote line breaks
# joined by single spaces.
FINAL_PARAGRAPH = (
    "This refusal is final for this task. Do not retry the same effect another "
    "way: not with a different spelling, quoting or option order, not with "
    "another program or interpreter, and not by writing a script, test, config "
    "file or Makefile target and then running a command that picks it up. Each "
    "of those is circumventing this guard, whatever the intent, and must be "
    "reported as such. If this message names a supported form and that form "
    "does what you need, use exactly that form. Otherwise stop the part of your "
    "work that needs this, finish anything that does not, and put in your "
    "report: the command you ran, what you needed it for, and this refusal word "
    "for word. Whoever dispatched you decides what happens next."
)
FINAL_SENTENCE = "This refusal is final for this task."

# Decision 12's glob list, which decision 14 uses both as the coder's Edit/Write
# DENY_GLOBS and as its Bash WRITE_DENY_GLOBS.
DECISION_12_GLOBS = (
    "tests/* */tests/* docs/spec/* docs/adr/* docs/protocol/* schemas/* "
    ".claude/* CLAUDE.md */CLAUDE.md CLAUDE.local.md */CLAUDE.local.md .mcp.json "
    "/* ../* */../* .git .git/* */.git */.git/* .venv/* */.venv/* "
    "__pycache__/* */__pycache__/* "
    "conftest.py */conftest.py test_*.py */test_*.py *_test.py test*.txt */test*.txt "
    "pytest.toml */pytest.toml .pytest.toml */.pytest.toml pytest.ini */pytest.ini "
    ".pytest.ini */.pytest.ini tox.ini */tox.ini setup.cfg */setup.cfg "
    "mypy.ini */mypy.ini .mypy.ini */.mypy.ini .ruff.toml */.ruff.toml */ruff.toml "
    "uv.toml */uv.toml .python-version */.python-version "
    "sitecustomize.py */sitecustomize.py usercustomize.py */usercustomize.py "
    "pytest pytest/* ruff ruff/* mypy mypy/* GNUmakefile makefile uv.lock"
)

CODER_ALLOW_CMDS = "ls cat head tail wc stat find grep rg jq diff cmp pwd git uv make"

# Decision 14's coder Bash policy, as knob values.
CODER_POLICY: dict[str, str] = {
    "SCOPE_AGENT_TYPES": "coder",
    "LITERAL_ONLY": "1",
    "DENY_ADVICE": "stop-and-report",
    "ALLOW_CMDS": CODER_ALLOW_CMDS,
    "ALLOW_GIT_SUBCMDS": "status diff log show rev-parse ls-files add commit merge",
    "ALLOW_UV_RUN_TARGETS": "pytest ruff",
    "ALLOW_MAKE_TARGETS": "typecheck",
    "WRITE_DENY_GLOBS": DECISION_12_GLOBS,
}

# The security-auditor's policy as decision 14 leaves it: no new knob set.
AUDITOR_POLICY: dict[str, str] = {
    "SCOPE_AGENT_TYPES": "security-auditor",
    "ALLOW_CMDS": "ls cat head tail wc stat find grep rg jq diff cmp git node",
    "ALLOW_GIT_SUBCMDS": (
        "log show diff status ls-files ls-tree cat-file blame rev-parse rev-list "
        "shortlog grep describe"
    ),
    "ALLOW_NODE_SCRIPTS": (
        ".claude/skills/security-audit/validate-findings.cjs "
        ".claude/skills/security-audit/validate-coverage-ledger.cjs"
    ),
}


# --- running the guard ---------------------------------------------------


def run_guard(
    command: str,
    *,
    policy: Mapping[str, str],
    cwd: str | Path,
    agent_type: str | None,
    project_dir: str | None = None,
    script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the guard exactly as a PreToolUse hook on the Bash tool would."""
    guard = GUARD if script is None else script
    assert guard.is_file(), f"{guard} does not exist, so no guard can run"

    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }
    if agent_type is not None:
        payload["agent_type"] = agent_type

    env = dict(os.environ)
    for name in POLICY_VARS:
        env.pop(name, None)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT) if project_dir is None else project_dir
    env.update(policy)

    return subprocess.run(
        [BASH or "bash", str(guard)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def with_changes(base: Mapping[str, str], changes: Mapping[str, str | None]) -> dict[str, str]:
    """`base` with some knobs replaced; a value of None removes the knob."""
    policy = dict(base)
    for name, value in changes.items():
        if value is None:
            policy.pop(name, None)
        else:
            policy[name] = value
    return policy


def coder(command: str, cwd: str | Path, **changes: str | None) -> subprocess.CompletedProcess[str]:
    """Run `command` as the coder, under decision 14's coder policy."""
    return run_guard(
        command, policy=with_changes(CODER_POLICY, changes), cwd=cwd, agent_type="coder"
    )


def auditor(
    command: str, cwd: str | Path, **changes: str | None
) -> subprocess.CompletedProcess[str]:
    """Run `command` as the security-auditor, under its unchanged policy."""
    return run_guard(
        command,
        policy=with_changes(AUDITOR_POLICY, changes),
        cwd=cwd,
        agent_type="security-auditor",
    )


def assert_allowed(result: subprocess.CompletedProcess[str], what: str) -> None:
    """Allowed means silent: exit 0 and no output (decision 11)."""
    assert result.returncode == 0, (
        f"expected the guard to stay silent on {what} (exit 0), got exit "
        f"{result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "", (
        f"the guard must never emit an allow decision (ADR-0018 decision 11), but it "
        f"printed output for {what}.\nstdout: {result.stdout}"
    )


def assert_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    assert result.returncode == 2, (
        f"expected the guard to DENY {what} (exit 2), got exit {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse", decision
    assert decision["permissionDecision"] == "deny", decision
    reason = str(decision.get("permissionDecisionReason", ""))
    assert reason.startswith(PREFIX), (
        "every bash-guard message begins 'Hammertime bash guard: ' (ADR-0018 decision "
        "11); it is the only way to tell a live guard from the harness or the sandbox.\n"
        f"reason: {reason!r}"
    )
    return reason


def assert_phrase(reason: str, phrase: str, what: str) -> None:
    assert phrase in reason, (
        f"the denial of {what} must contain {phrase!r} (ADR-0018 decision 11's table of "
        f"required phrases).\nreason: {reason!r}"
    )


def make_shadow(cwd: Path, name: str) -> None:
    """Create an entry that would shadow a tool or the Makefile (decision 5 step 4)."""
    entry = cwd / name
    if name == "pytest":
        entry.mkdir()
        (entry / "__main__.py").write_text("", encoding="utf-8")
    elif name == "mypy":
        entry.mkdir()
    else:
        entry.write_text("", encoding="utf-8")


# --- A. contract and routing (decision 11) -------------------------------


def test_a_denial_is_exit_2_with_a_json_deny_and_the_prefix(tmp_path: Path) -> None:
    assert_denied(coder("python3 -V", tmp_path), "python3 -V under the coder policy")


def test_an_allowed_command_is_silent_under_a_literal_policy(tmp_path: Path) -> None:
    assert_allowed(coder("git status", tmp_path), "git status under a literal policy")


def test_an_allowed_command_is_silent_under_a_non_literal_policy(tmp_path: Path) -> None:
    assert_allowed(auditor("git status", tmp_path), "git status under the auditor's policy")


@pytest.mark.parametrize(
    ("agent_type", "policed"),
    [("coder", True), ("test-author", False), (None, False)],
    ids=["coder", "test-author", "no-agent-type"],
)
def test_scope_routes_the_coder_policy_to_the_coder_only(
    tmp_path: Path, agent_type: str | None, policed: bool
) -> None:
    result = run_guard("python3 -V", policy=CODER_POLICY, cwd=tmp_path, agent_type=agent_type)
    if policed:
        assert_denied(result, f"python3 -V from agent_type={agent_type!r}")
    else:
        assert_allowed(result, f"python3 -V from agent_type={agent_type!r} (not in scope)")


# --- B. configuration hygiene (decisions 3, 4, 5, 7) ---------------------


@pytest.mark.parametrize("base", ["coder", "auditor"])
def test_a_command_the_script_has_no_rule_for_is_a_configuration_error(
    tmp_path: Path, base: str
) -> None:
    """Decision 4: a command on ALLOW_CMDS but not on the built-in list."""
    if base == "coder":
        result = coder("python3 -V", tmp_path, ALLOW_CMDS=f"{CODER_ALLOW_CMDS} python3")
    else:
        allow = f"{AUDITOR_POLICY['ALLOW_CMDS']} python3"
        result = auditor("python3 -V", tmp_path, ALLOW_CMDS=allow)
    reason = assert_denied(result, "python3 -V with python3 on ALLOW_CMDS")
    assert_phrase(reason, "configuration error", "an unknown command on ALLOW_CMDS")


@pytest.mark.parametrize("command", ["ls", "git status", "pwd"])
def test_a_bad_literal_only_value_refuses_every_command(tmp_path: Path, command: str) -> None:
    """Decision 3: a LITERAL_ONLY other than empty or 1 is a configuration error."""
    reason = assert_denied(coder(command, tmp_path, LITERAL_ONLY="yes"), command)
    assert_phrase(reason, "configuration error", f"{command} with LITERAL_ONLY='yes'")


NON_LITERAL_FEATURE_CASES = [
    (
        "uv",
        {"ALLOW_CMDS": "ls uv", "ALLOW_UV_RUN_TARGETS": "pytest ruff"},
        "uv run --locked pytest -q",
    ),
    ("make", {"ALLOW_CMDS": "ls make", "ALLOW_MAKE_TARGETS": "typecheck"}, "make typecheck"),
    (
        "git-commit",
        {"ALLOW_CMDS": "ls git", "ALLOW_GIT_SUBCMDS": "status commit"},
        "git commit -F x",
    ),
    ("git-add", {"ALLOW_CMDS": "ls git", "ALLOW_GIT_SUBCMDS": "status add"}, "git add x"),
    (
        "git-merge",
        {"ALLOW_CMDS": "ls git", "ALLOW_GIT_SUBCMDS": "status merge"},
        "git merge --ff-only x",
    ),
    ("coder-uv", {}, "uv run --locked pytest -q"),
    ("coder-make", {}, "make typecheck"),
    ("coder-commit", {}, "git commit -F x"),
]


@pytest.mark.parametrize(
    ("policy", "command"),
    [(policy, command) for _, policy, command in NON_LITERAL_FEATURE_CASES],
    ids=[case_id for case_id, _, _ in NON_LITERAL_FEATURE_CASES],
)
def test_a_literal_only_rule_without_literal_mode_is_a_configuration_error(
    tmp_path: Path, policy: dict[str, str], command: str
) -> None:
    """Decision 3: every uv and make rule, and git add/commit/merge, need literal mode.

    The `coder-*` cases take decision 14's coder policy and drop LITERAL_ONLY.
    """
    if policy:
        full = {"SCOPE_AGENT_TYPES": "coder", "DENY_ADVICE": "stop-and-report", **policy}
    else:
        full = with_changes(CODER_POLICY, {"LITERAL_ONLY": None})
    result = run_guard(command, policy=full, cwd=tmp_path, agent_type="coder")
    reason = assert_denied(result, f"{command} under a policy without LITERAL_ONLY='1'")
    assert_phrase(reason, "configuration error", f"{command} without literal mode")


def test_a_uv_target_the_script_has_no_rule_for_is_a_configuration_error(
    tmp_path: Path,
) -> None:
    """Decision 5 step 3: a target on the knob but not on the built-in list."""
    result = coder("uv run --locked mypy x", tmp_path, ALLOW_UV_RUN_TARGETS="pytest mypy")
    reason = assert_denied(result, "uv run --locked mypy x with mypy on ALLOW_UV_RUN_TARGETS")
    assert_phrase(reason, "configuration error", "mypy on ALLOW_UV_RUN_TARGETS")


def test_ruff_write_mode_is_refused_when_write_deny_globs_is_unset(tmp_path: Path) -> None:
    """Decision 7: with WRITE_DENY_GLOBS empty, write mode is refused altogether."""
    result = coder("uv run --locked ruff format a.py", tmp_path, WRITE_DENY_GLOBS=None)
    assert_denied(result, "ruff format a.py with WRITE_DENY_GLOBS unset")


def test_ruff_read_only_mode_is_allowed_when_write_deny_globs_is_unset(tmp_path: Path) -> None:
    result = coder("uv run --locked ruff format --check .", tmp_path, WRITE_DENY_GLOBS=None)
    assert_allowed(result, "ruff format --check . with WRITE_DENY_GLOBS unset")


# --- C. literal mode (decision 3) ----------------------------------------

LITERAL_REFUSALS = [
    ("newline", "git status\ngit log -1"),
    ("dollar", "ls $HOME"),
    ("backtick", "ls `pwd`"),
    ("backslash", "ls a\\ b"),
    ("single-quote", "ls 'a b'"),
    ("double-quote", 'ls "a b"'),
    ("open-brace", "ls a{b"),
    ("close-brace", "ls a}b"),
    ("open-bracket", "ls a[b"),
    ("close-bracket", "ls a]b"),
    ("open-paren", "ls a(b"),
    ("close-paren", "ls a)b"),
    ("star", "ls a*"),
    ("question-mark", "ls a?"),
    ("less-than", "cat < a"),
    ("greater-than", "ls > a"),
    ("hash", "ls # a"),
    ("lone-ampersand", "ls & pwd"),
    ("trailing-ampersand", "ls &"),
    ("pipe-ampersand", "ls |& cat"),
    ("tilde-word", "ls ~"),
    ("tilde-path", "ls ~/x"),
    ("equals-tilde", "ls a=~b"),
    ("colon-tilde", "ls a:~b"),
]


@pytest.mark.parametrize(
    "command",
    [command for _, command in LITERAL_REFUSALS],
    ids=[case_id for case_id, _ in LITERAL_REFUSALS],
)
def test_literal_mode_refuses_a_non_literal_character(tmp_path: Path, command: str) -> None:
    """Decisions 3 and 11: the literal-mode denial says `must be literal` and
    names the sanctioned forms, `git commit -F` among them."""
    reason = assert_denied(coder(command, tmp_path), repr(command))
    assert_phrase(reason, "must be literal", repr(command))
    assert_phrase(reason, "git commit -F", repr(command))


def test_a_quoted_commit_message_is_refused_and_pointed_at_a_file(tmp_path: Path) -> None:
    reason = assert_denied(coder('git commit -m "a b"', tmp_path), 'git commit -m "a b"')
    assert_phrase(reason, "git commit -F", 'git commit -m "a b"')


@pytest.mark.parametrize(
    "command",
    [
        "git status && git log -1",
        "git status | head -5",
        "git status; git log -1",
        "git diff HEAD~1",
    ],
)
def test_literal_mode_allows_separators_and_an_inner_tilde(tmp_path: Path, command: str) -> None:
    assert_allowed(coder(command, tmp_path), command)


def test_every_segment_of_a_literal_command_is_vetted(tmp_path: Path) -> None:
    assert_denied(coder("git status; python3 -V", tmp_path), "git status; python3 -V")


@pytest.mark.parametrize("command", ['rg -n "x" services', "ls *"])
def test_without_literal_mode_quotes_and_globs_are_not_refused(
    tmp_path: Path, command: str
) -> None:
    """Regression: the auditor's policy sets no LITERAL_ONLY, so nothing changes."""
    assert_allowed(auditor(command, tmp_path), f"{command} under the auditor's policy")


# --- D. uv (decision 5) --------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "uv run --locked pytest -q",
        "uv run --locked --offline pytest -q",
        "uv run --offline --locked pytest -q",
        "uv run --locked ruff check .",
        "uv run --locked ruff format --check .",
    ],
)
def test_uv_allows_the_locked_gates(tmp_path: Path, command: str) -> None:
    assert_allowed(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "command",
    [
        # installs and other subcommands
        "uv pip list",
        "uv add x",
        "uv sync",
        "uv lock",
        "uv tool run x",
        # shape
        "uv --directory /tmp run pytest",
        "uv run",
        "uv run --locked",
        # uv options
        "uv run --locked --with x pytest",
        "uv run --locked -m pytest",
        "uv run --locked --script x.py",
        "uv run --locked --python 3.12 pytest",
        "uv run --locked --project /tmp pytest",
        "uv run --locked --env-file .env pytest",
        "uv run --locked -- pytest",
        "uv run --locked -",
        "uv run --locked --frozen pytest -q",
        "uv run --frozen pytest -q",
        "uv run --locked --no-sync pytest -q",
        "uv run --no-sync pytest -q",
    ],
)
def test_uv_refuses_subcommands_shapes_and_options(tmp_path: Path, command: str) -> None:
    assert_denied(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "command",
    [
        "uv run --locked python -V",
        "uv run --locked PYTHON -V",
        "uv run --locked mypy x",
        "uv run --locked hammertime-trie",
        "uv run --locked scratch.py",
        "uv run --locked https://example.com/x.py",
    ],
)
def test_uv_refuses_a_target_not_on_the_list(tmp_path: Path, command: str) -> None:
    """Written with --locked, so the target is the only reason to refuse."""
    reason = assert_denied(coder(command, tmp_path), command)
    assert_phrase(reason, "ALLOW_UV_RUN_TARGETS", command)


def test_uv_mypy_target_denial_names_make_typecheck(tmp_path: Path) -> None:
    """Decision 5 step 3: "`mypy` -- the message names `make typecheck`"."""
    reason = assert_denied(coder("uv run --locked mypy x", tmp_path), "uv run --locked mypy x")
    assert_phrase(reason, "make typecheck", "uv run --locked mypy x")


@pytest.mark.parametrize(
    ("command", "corrected"),
    [
        ("uv run pytest -q", "uv run --locked pytest -q"),
        ("uv run ruff check .", "uv run --locked ruff check ."),
    ],
)
def test_uv_run_without_locked_is_refused_with_the_corrected_form(
    tmp_path: Path, command: str, corrected: str
) -> None:
    """Decision 5 step 6: the denial names the same command with --locked added."""
    reason = assert_denied(coder(command, tmp_path), command)
    assert_phrase(reason, "must carry --locked", command)
    assert_phrase(reason, corrected, command)


@pytest.mark.parametrize(
    ("command", "phrase"),
    [
        ("uv run python -V", "ALLOW_UV_RUN_TARGETS"),
        ("uv run pytest -p x", None),
        ("uv run pytest --locked -q", None),
        ("uv run --frozen pytest -q", None),
        ("uv run --no-sync pytest -q", None),
    ],
)
def test_capability_refusals_come_before_the_missing_locked_refusal(
    tmp_path: Path, command: str, phrase: str | None
) -> None:
    """Decision 5 step 6 is checked last: it is given only to a command that
    would be allowed as soon as --locked is added. `--locked` after the target is
    a pytest word, refused by pytest's own rule."""
    reason = assert_denied(coder(command, tmp_path), command)
    assert "must carry --locked" not in reason, (
        f"{command!r} is refused for a capability, so the form correction of decision 5 "
        f"step 6 must not be what it gets.\nreason: {reason!r}"
    )
    if phrase is not None:
        assert_phrase(reason, phrase, command)


SHADOW_ENTRIES = ["pytest", "ruff", "mypy", "GNUmakefile", "makefile"]
SHADOWED_COMMANDS = ["uv run --locked pytest -q", "make typecheck"]


@pytest.mark.parametrize("entry", SHADOW_ENTRIES)
@pytest.mark.parametrize("command", SHADOWED_COMMANDS)
def test_an_entry_shadowing_a_tool_refuses_uv_and_make(
    tmp_path: Path, command: str, entry: str
) -> None:
    make_shadow(tmp_path, entry)
    reason = assert_denied(coder(command, tmp_path), f"{command} with {entry} in cwd")
    assert_phrase(reason, entry, f"{command} with {entry} in cwd (the shadow check)")


@pytest.mark.parametrize("command", SHADOWED_COMMANDS)
def test_a_clean_cwd_allows_uv_and_make(tmp_path: Path, command: str) -> None:
    assert_allowed(coder(command, tmp_path), f"{command} in a clean cwd")


@pytest.mark.parametrize("command", SHADOWED_COMMANDS)
def test_an_empty_cwd_refuses_uv_and_make(command: str) -> None:
    """Decision 5 step 4: "An empty `cwd` refuses the command"."""
    assert_denied(coder(command, ""), f"{command} with an empty cwd")


# --- E. pytest (decision 6) ----------------------------------------------

TEST_FILE = "services/trie/src/hammertime/trie/tests/test_query.py"


@pytest.mark.parametrize(
    "words",
    [
        "-q",
        "-qx",
        "-vv",
        "-x --lf",
        "-k word",
        "-m word",
        "-rA",
        "--tb=short",
        "--tb short",
        "--maxfail=1",
        "--durations=5",
        "--co -q",
        "--hypothesis-seed=1",
        "--benchmark-only",
    ],
)
def test_pytest_allows_the_listed_options(tmp_path: Path, words: str) -> None:
    command = f"uv run --locked pytest {words}"
    assert_allowed(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "words",
    [
        "tests/config",
        ".",
        "./",
        TEST_FILE,
        f"{TEST_FILE}::test_x",
        f"{TEST_FILE}::TestA::test_b",
        "x_test.py",
    ],
)
def test_pytest_allows_directory_and_test_file_operands(tmp_path: Path, words: str) -> None:
    command = f"uv run --locked pytest -q {words}"
    assert_allowed(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "words",
    [
        "-p x",
        "-c x",
        "-o x=y",
        "-W error",
        "--pyargs",
        "--rootdir=.",
        "--confcutdir=.",
        "--basetemp=x",
        "--junitxml=x",
        "--junit-xml=x",
        "--debug",
        "--log-file=x",
        "--pastebin=all",
        "--pdb",
        "--trace",
        "--pdbcls=x:y",
        "--doctest-modules",
        "--doctest-glob=x",
        "--import-mode=append",
        "--override-ini=x=y",
        "--collectonly",
        "--tb=bogus",
        "--maxfail=x",
        "--",
        "--help",
        "-kword",
    ],
)
def test_pytest_refuses_options_not_on_the_list(tmp_path: Path, words: str) -> None:
    command = f"uv run --locked pytest {words}"
    assert_denied(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "words",
    [
        "@args",
        "/tmp/test_x.py",
        "../tests",
        "tests/../x",
        "scratch.py",
        "notes.txt",
        "README.rst",
        "conftest.py",
        "services/x/conftest.py",
        ".claude",
        "x.py::test_y",
    ],
)
def test_pytest_refuses_operands_that_are_not_tests(tmp_path: Path, words: str) -> None:
    command = f"uv run --locked pytest -q {words}"
    assert_denied(coder(command, tmp_path), command)


def test_pytest_vets_an_option_value_as_an_operand(tmp_path: Path) -> None:
    """Decision 6: no word is skipped as some option's value."""
    command = "uv run --locked pytest -k scratch.py"
    assert_denied(coder(command, tmp_path), command)


# --- F. ruff and WRITE_DENY_GLOBS (decision 7) ---------------------------

APP_FILE = "services/trie/src/hammertime/trie/query/app.py"
OTHER_APP_FILE = "services/trie/src/hammertime/trie/query/routes.py"


@pytest.mark.parametrize(
    "words",
    [
        "check .",
        "check -q services",
        "check --diff .",
        "format --check .",
        "format --diff services",
        f"format {APP_FILE}",
        f"format {APP_FILE} {OTHER_APP_FILE}",
    ],
)
def test_ruff_allows_checks_and_formatting_named_files(tmp_path: Path, words: str) -> None:
    command = f"uv run --locked ruff {words}"
    assert_allowed(coder(command, tmp_path), command)


@pytest.mark.parametrize("words", ["format .", "format", "format services"])
def test_ruff_write_mode_needs_explicit_python_files(tmp_path: Path, words: str) -> None:
    command = f"uv run --locked ruff {words}"
    reason = assert_denied(coder(command, tmp_path), command)
    assert_phrase(reason, "uv run --locked ruff format", command)
    assert_phrase(reason, ".py", command)


@pytest.mark.parametrize(
    "words",
    [
        # WRITE_DENY_GLOBS
        "format tests/config/test_x.py",
        "format services/x/src/y/tests/test_z.py",
        "format pytest/__main__.py",
        # operand shape
        "format ../x.py",
        "format /abs/x.py",
        "format @x",
        # options
        "check --fix .",
        "check --unsafe-fixes .",
        "check --output-file=x .",
        "check --config=x .",
        "check --cache-dir=/tmp .",
        "check --watch .",
        "check --add-noqa .",
        "format -- .",
        # subcommand
        "--config x check .",
        "rule E501",
        "clean",
        "server",
    ],
)
def test_ruff_refuses_protected_files_bad_operands_options_and_subcommands(
    tmp_path: Path, words: str
) -> None:
    command = f"uv run --locked ruff {words}"
    assert_denied(coder(command, tmp_path), command)


# --- G. make (decision 8) ------------------------------------------------


def test_make_allows_the_typecheck_target(tmp_path: Path) -> None:
    assert_allowed(coder("make typecheck", tmp_path), "make typecheck in a clean cwd")


@pytest.mark.parametrize(
    "command",
    [
        "make",
        "make up",
        "make test",
        "make typecheck lint",
        "make -C /tmp typecheck",
        "make -f x typecheck",
        "make typecheck X=1",
        "make -n typecheck",
        "make --eval=x typecheck",
    ],
)
def test_make_refuses_anything_but_one_listed_target(tmp_path: Path, command: str) -> None:
    reason = assert_denied(coder(command, tmp_path), command)
    assert_phrase(reason, "ALLOW_MAKE_TARGETS", command)


# --- H. git for the coder (decision 9) -----------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff --stat",
        "git log --oneline -3",
        "git show HEAD",
        "git rev-parse HEAD",
        "git ls-files",
        "git add -A",
        "git add services/x.py",
        "git add -u",
        "git commit -F .commit-msg",
        "git commit -a -F .commit-msg",
        "git commit -aF .commit-msg",
        "git commit -m Probe",
        "git commit --file=.commit-msg",
        "git merge --ff-only master",
        "git merge -q --ff-only HEAD",
    ],
)
def test_git_allows_the_coder_subcommands(tmp_path: Path, command: str) -> None:
    assert_allowed(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -F x",
        "git commit -n -F x",
        "git commit -an -F x",
        "git commit --amend -F x",
        "git commit -e -F x",
        "git commit -S -F x",
        "git commit --gpg-sign -F x",
        "git commit -C HEAD",
        "git commit -c HEAD",
        "git commit --trailer x -F y",
        "git commit --author=x -F y",
        "git commit -p",
        "git commit -- x",
        "git commit -m -n",
    ],
)
def test_git_commit_refuses_options_not_on_the_list(tmp_path: Path, command: str) -> None:
    assert_denied(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "command",
    [
        "git add -f x",
        "git add --force x",
        "git add -p",
        "git add -i",
        "git add -e",
        "git add --chmod=+x x",
        "git add --pathspec-from-file=x",
        "git add -- x",
    ],
)
def test_git_add_refuses_options_not_on_the_list(tmp_path: Path, command: str) -> None:
    assert_denied(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "command",
    [
        "git merge master",
        "git merge --no-ff master",
        "git merge --ff-only -s ours master",
        "git merge --ff-only -X theirs master",
        "git merge --ff-only -- master",
        "git merge --squash master",
    ],
)
def test_git_merge_is_fast_forward_only(tmp_path: Path, command: str) -> None:
    assert_denied(coder(command, tmp_path), command)


@pytest.mark.parametrize(
    "command",
    [
        "git fetch",
        "git pull",
        "git push",
        "git checkout x",
        "git switch x",
        "git restore x",
        "git reset --hard",
        "git stash",
        "git rebase x",
        "git cherry-pick x",
        "git rm x",
        "git mv a b",
        "git clean -fd",
        "git config x y",
        "git worktree add x",
        "git branch x",
        "git tag x",
    ],
)
def test_git_refuses_other_subcommands_and_names_rev_parse(tmp_path: Path, command: str) -> None:
    reason = assert_denied(coder(command, tmp_path), command)
    assert_phrase(reason, "git rev-parse --abbrev-ref HEAD", command)


@pytest.mark.parametrize(
    "command",
    [
        "git -C /tmp status",
        "git -c core.pager=cat log",
        "git --git-dir=x status",
        "git --work-tree=x status",
        "git log --output=x",
        "git diff --output=x",
        "git show --help",
    ],
)
def test_git_refuses_global_and_output_options(tmp_path: Path, command: str) -> None:
    assert_denied(coder(command, tmp_path), command)


def test_auditor_git_log_patch_is_still_allowed(tmp_path: Path) -> None:
    assert_allowed(auditor("git log -p", tmp_path), "git log -p under the auditor's policy")


def test_auditor_git_global_option_is_still_refused(tmp_path: Path) -> None:
    assert_denied(auditor("git --namespace log", tmp_path), "git --namespace log (auditor)")


# --- I. advice (decision 11) ---------------------------------------------

# One denial per rule family: (id, command, knob changes, shadowing entry).
ADVICE_SAMPLE: list[tuple[str, str, dict[str, str | None], str | None]] = [
    ("literal", "ls *", {}, None),
    ("command", "python3 -V", {}, None),
    ("command-cd", "cd /tmp", {}, None),
    (
        "configuration-error-command",
        "python3 -V",
        {"ALLOW_CMDS": f"{CODER_ALLOW_CMDS} python3"},
        None,
    ),
    ("configuration-error-literal-only", "ls", {"LITERAL_ONLY": "yes"}, None),
    ("uv-target", "uv run --locked python -V", {}, None),
    ("uv-subcommand", "uv pip list", {}, None),
    ("uv-missing-locked", "uv run pytest -q", {}, None),
    ("uv-shadow", "uv run --locked pytest -q", {}, "pytest"),
    ("pytest", "uv run --locked pytest -q -p x", {}, None),
    ("ruff", "uv run --locked ruff format .", {}, None),
    ("make", "make up", {}, None),
    ("git-subcommand", "git fetch", {}, None),
    ("git-global-option", "git -C /tmp status", {}, None),
    ("git-commit-option", "git commit --no-verify -F x", {}, None),
    ("find-exec", "find . -exec pwd +", {}, None),
]
ADVICE_IDS = [case_id for case_id, _, _, _ in ADVICE_SAMPLE]


def run_advice_sample(
    tmp_path: Path,
    command: str,
    changes: dict[str, str | None],
    shadow: str | None,
    advice: str | None,
) -> str:
    if shadow is not None:
        make_shadow(tmp_path, shadow)
    result = coder(command, tmp_path, **{**changes, "DENY_ADVICE": advice})
    return assert_denied(result, f"{command!r} with DENY_ADVICE={advice!r}")


@pytest.mark.parametrize("advice", ["stop-and-report", "bogus"])
@pytest.mark.parametrize(
    ("command", "changes", "shadow"),
    [(command, changes, shadow) for _, command, changes, shadow in ADVICE_SAMPLE],
    ids=ADVICE_IDS,
)
def test_stop_and_report_denials_end_with_the_final_paragraph(
    tmp_path: Path,
    command: str,
    changes: dict[str, str | None],
    shadow: str | None,
    advice: str,
) -> None:
    """Every denial ends with decision 11's paragraph after one space. Any
    non-empty DENY_ADVICE other than `needs-validation` behaves the same way."""
    reason = run_advice_sample(tmp_path, command, changes, shadow, advice)
    assert reason.endswith(f" {FINAL_PARAGRAPH}"), (
        f"with DENY_ADVICE={advice!r}, the denial of {command!r} must end with decision "
        f"11's paragraph, verbatim, after one space.\nreason: {reason!r}"
    )


@pytest.mark.parametrize("advice", [None, "needs-validation"])
@pytest.mark.parametrize(
    ("command", "changes", "shadow"),
    [(command, changes, shadow) for _, command, changes, shadow in ADVICE_SAMPLE],
    ids=ADVICE_IDS,
)
def test_default_advice_never_carries_the_final_paragraph(
    tmp_path: Path,
    command: str,
    changes: dict[str, str | None],
    shadow: str | None,
    advice: str | None,
) -> None:
    reason = run_advice_sample(tmp_path, command, changes, shadow, advice)
    assert FINAL_SENTENCE not in reason, (
        f"with DENY_ADVICE={advice!r}, no denial may carry decision 11's paragraph.\n"
        f"reason: {reason!r}"
    )


@pytest.mark.parametrize(
    "command",
    [
        "python3 -V",
        "sed -n 1,2p CHANGES",
        "uv run pytest -q",
        "git fetch",
        "git -C /tmp status",
        "find . -exec pwd +",
    ],
)
def test_auditor_denials_carry_no_final_paragraph(tmp_path: Path, command: str) -> None:
    reason = assert_denied(auditor(command, tmp_path), f"{command} (auditor)")
    assert FINAL_SENTENCE not in reason, reason


@pytest.mark.parametrize("command", ["python3 -V", "sed -n 1,2p CHANGES", "uv run pytest -q"])
def test_auditor_not_allowed_command_still_says_needs_validation(
    tmp_path: Path, command: str
) -> None:
    reason = assert_denied(auditor(command, tmp_path), f"{command} (auditor)")
    assert_phrase(reason, "needs-validation", f"{command} under the auditor's policy")


@pytest.mark.parametrize(
    ("command", "phrase"),
    [("cd /tmp", "worktree root"), ("python3 -V", "uv run --locked pytest")],
)
def test_not_allowed_command_hints(tmp_path: Path, command: str, phrase: str) -> None:
    """Decision 4's two required hints, in stop-and-report mode."""
    reason = assert_denied(coder(command, tmp_path), command)
    assert_phrase(reason, phrase, command)


# --- the policies as they are actually configured ------------------------
#
# These run the real command lines out of .claude/settings.json. The settings
# parsing is duplicated from the sibling modules rather than imported, so each
# file in this package stays runnable on its own.


def _split_command(command: str) -> tuple[dict[str, str], Path]:
    tokens = shlex.split(command)
    env: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        name, separator, value = tokens[index].partition("=")
        if not separator or not name.isidentifier():
            break
        env[name] = value
        index += 1
    raw = tokens[index] if index < len(tokens) else ""
    expanded = raw.replace("${CLAUDE_PROJECT_DIR}", str(REPO_ROOT))
    expanded = expanded.replace("$CLAUDE_PROJECT_DIR", str(REPO_ROOT))
    path = Path(expanded)
    return env, path if path.is_absolute() else REPO_ROOT / path


def configured_policy(agent_name: str) -> tuple[dict[str, str], Path]:
    """The live Bash policy for one agent, read from settings.json."""
    if not SETTINGS_PATH.is_file():
        pytest.fail(f"{SETTINGS_PATH} does not exist; no policy is wired for {agent_name}")
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        matched = {part.strip() for part in entry.get("matcher", "").split("|")}
        if "Bash" not in matched:
            continue
        for hook in entry.get("hooks", []):
            env, script = _split_command(hook.get("command", ""))
            if script.name != "bash-guard.sh":
                continue
            if agent_name in env.get("SCOPE_AGENT_TYPES", "").split():
                return env, script
    pytest.fail(
        f"no PreToolUse Bash policy in {SETTINGS_PATH} runs bash-guard.sh and names "
        f"'{agent_name}' in SCOPE_AGENT_TYPES (ADR-0018 decision 14)"
    )


def run_configured(agent_name: str, command: str) -> subprocess.CompletedProcess[str]:
    policy, script = configured_policy(agent_name)
    return run_guard(command, policy=policy, cwd=REPO_ROOT, agent_type=agent_name, script=script)


# --- J. the incident (decision 1's table) --------------------------------

HEREDOC_BODY = "import sys\nprint(sys.path)\nEOF"

INCIDENT_FORMS = [
    ("uv-run-python-heredoc", f"uv run python - <<'EOF'\n{HEREDOC_BODY}", "must be literal"),
    ("venv-python-heredoc", f".venv/bin/python - <<'EOF'\n{HEREDOC_BODY}", "must be literal"),
    ("python3-heredoc", f"python3 - <<'EOF'\n{HEREDOC_BODY}", "must be literal"),
    (
        "uv-run-python-c-multiline",
        'uv run python -c "\nimport sys\nprint(sys.path)\n"',
        "must be literal",
    ),
    ("uv-run-python-c-one-line", 'uv run python -c "print(1)"', "must be literal"),
    ("uv-run-python-c-pass", "uv run python -c pass", "ALLOW_UV_RUN_TARGETS"),
    ("python3-c-pass", "python3 -c pass", "uv run --locked pytest"),
]


@pytest.mark.parametrize(
    ("command", "phrase"),
    [(command, phrase) for _, command, phrase in INCIDENT_FORMS],
    ids=[case_id for case_id, _, _ in INCIDENT_FORMS],
)
def test_configured_coder_policy_refuses_every_incident_form(command: str, phrase: str) -> None:
    reason = assert_denied(run_configured("coder", command), f"incident form {command!r}")
    assert_phrase(reason, phrase, repr(command))
    assert reason.endswith(f" {FINAL_PARAGRAPH}"), reason


# --- K. configured policies (decision 14) --------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "uv run --locked pytest -q",
        "uv run --locked ruff check .",
        "uv run --locked ruff format --check .",
        "make typecheck",
        f"uv run --locked ruff format {APP_FILE}",
        "git add -A",
        "git commit -F .commit-msg",
        "git merge --ff-only HEAD",
    ],
)
def test_configured_coder_policy_allows_the_gates_and_commits(command: str) -> None:
    assert_allowed(run_configured("coder", command), f"{command} (configured coder policy)")


@pytest.mark.parametrize(
    "command",
    [
        # interpreters
        "python -V",
        "python3 -V",
        ".venv/bin/python -V",
        "uv run python -V",
        "uv run --locked python -c pass",
        "uv run --locked pytest -q scratch.py",
        # installs
        "pip install x",
        "uvx ruff",
        "uv pip install x",
        "uv add requests",
        "uv sync",
        "uv lock",
        "uv run --locked --with x pytest -q",
        # network clients
        "curl https://example.com",
        "wget https://example.com",
        "nc example.com 80",
        "ssh example.com",
        "git fetch",
        "git push",
        "uv run --locked pytest -q --pastebin=all",
        # heredocs
        "cat <<EOF\nx\nEOF",
        "uv run --locked pytest -q <<EOF\nx\nEOF",
        # the owner's --locked, ruff write mode, make
        "uv run pytest -q",
        "uv run --locked ruff format .",
        "make up",
    ],
)
def test_configured_coder_policy_refuses_escapes(command: str) -> None:
    reason = assert_denied(run_configured("coder", command), f"{command!r} (configured coder)")
    assert reason.endswith(f" {FINAL_PARAGRAPH}"), reason


def test_configured_coder_policy_asks_for_locked() -> None:
    reason = assert_denied(run_configured("coder", "uv run pytest -q"), "uv run pytest -q")
    assert_phrase(reason, "must carry --locked", "uv run pytest -q (configured coder)")


@pytest.mark.parametrize(
    "command",
    [
        'rg -n "x" services',
        "ls *",
        "git log -p",
        "node .claude/skills/security-audit/validate-findings.cjs",
    ],
)
def test_configured_auditor_policy_still_allows(command: str) -> None:
    assert_allowed(run_configured("security-auditor", command), f"{command} (auditor)")


def test_configured_auditor_policy_still_refuses_sed() -> None:
    reason = assert_denied(
        run_configured("security-auditor", "sed -n 1,2p CHANGES"), "sed -n 1,2p CHANGES"
    )
    assert FINAL_SENTENCE not in reason, reason


def test_configured_auditor_policy_refuses_uv_as_needs_validation() -> None:
    reason = assert_denied(run_configured("security-auditor", "uv run pytest -q"), "uv (auditor)")
    assert_phrase(reason, "needs-validation", "uv run pytest -q (configured auditor)")
    assert FINAL_SENTENCE not in reason, reason


# --- O. the NUL gate and control characters (decisions 3 and 11) ---------
#
# ADR-0018's second amendment (2026-09-24). Decision 3 adds a NUL gate that runs
# in every mode, before the literal check and before the empty-command exit, and
# makes literal mode refuse every C0 control character other than tab and
# newline, and DEL. Decision 11 gives both NUL-gate messages verbatim. A NUL or a
# control byte is put in the Python command string; `json.dumps` in `run_guard`
# encodes it as the JSON escape the payload needs. The non-string and missing
# command cases need a payload `run_guard` cannot build, so they go through
# `run_guard_tool_input`.

# Decision 11's NUL denial, verbatim, with the ADR's blockquote line breaks joined
# by single spaces. U+2014 is the em dash in the ADR's text.
NUL_MESSAGE = (
    "Hammertime bash guard: the command contains a NUL byte (U+0000), which "
    "cannot be carried through this guard intact — the byte is dropped when the "
    "command is read, so the guard cannot vet the command that would actually "
    "run. The command is refused."
)

# Decision 11's could-not-be-checked denial, verbatim except for the status N,
# which the brief says not to pin.
NOT_CHECKED_HEAD = (
    "Hammertime bash guard: the command could not be checked for a NUL byte "
    "(the check ended with status "
)
NOT_CHECKED_TAIL = (
    " instead of a result), so the guard cannot confirm that the command it "
    "would vet is the command that would run. The command is refused."
)
NOT_CHECKED_PATTERN = re.compile(rf"{re.escape(NOT_CHECKED_HEAD)}\d+{re.escape(NOT_CHECKED_TAIL)}")

NUL_GATE_POLICIES: dict[str, dict[str, str]] = {
    "coder": CODER_POLICY,
    "security-auditor": AUDITOR_POLICY,
}


def run_guard_tool_input(
    tool_input: Mapping[str, Any],
    *,
    policy: Mapping[str, str],
    cwd: str | Path,
    agent_type: str,
) -> subprocess.CompletedProcess[str]:
    """Like `run_guard`, but with the whole `tool_input` given (brief T1, group O).

    `run_guard` always sends `{"command": <str>}`. This sends whatever JSON
    `tool_input` holds: a command that is not a string, or no command at all.
    """
    assert GUARD.is_file(), f"{GUARD} does not exist, so no guard can run"

    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": dict(tool_input),
        "cwd": str(cwd),
        "agent_type": agent_type,
    }

    env = dict(os.environ)
    for name in POLICY_VARS:
        env.pop(name, None)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    env.update(policy)

    return subprocess.run(
        [BASH or "bash", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


NUL_POSITIONS = [
    ("start", "\x00git status"),
    ("middle", "git\x00 status"),
    ("end", "git status\x00"),
    ("only-a-nul", "\x00"),
]


@pytest.mark.parametrize(
    "command",
    [command for _, command in NUL_POSITIONS],
    ids=[case_id for case_id, _ in NUL_POSITIONS],
)
def test_a_nul_anywhere_is_refused_under_the_coder_policy(tmp_path: Path, command: str) -> None:
    """Decisions 3 and 11: a NUL anywhere, even a command that is only a NUL, is
    refused rather than read as the command with the NUL dropped (or as empty)."""
    reason = assert_denied(coder(command, tmp_path), repr(command))
    assert_phrase(reason, "NUL byte (U+0000)", repr(command))
    assert reason.endswith(f" {FINAL_PARAGRAPH}"), (
        f"the coder is stop-and-report, so the NUL denial of {command!r} must end with "
        f"decision 11's paragraph after one space.\nreason: {reason!r}"
    )


@pytest.mark.parametrize("agent", ["coder", "security-auditor"])
def test_the_nul_denial_is_decision_11s_text_verbatim(tmp_path: Path, agent: str) -> None:
    """Decision 11: one base text in every mode, with the final paragraph added
    only under stop-and-report (the coder), never under the auditor."""
    command = "git status\x00"
    result = run_guard(command, policy=NUL_GATE_POLICIES[agent], cwd=tmp_path, agent_type=agent)
    reason = assert_denied(result, f"{command!r} from {agent}")
    expected = f"{NUL_MESSAGE} {FINAL_PARAGRAPH}" if agent == "coder" else NUL_MESSAGE
    assert reason == expected, (
        f"the NUL denial for {agent} must be decision 11's text verbatim.\n"
        f"expected: {expected!r}\nreason:   {reason!r}"
    )


@pytest.mark.parametrize(
    ("command", "without_nul"),
    [
        ("uv run --locked ruff format\x00 --check .", "uv run --locked ruff format --check ."),
        ("git merge\x00 --ff-only HEAD", "git merge --ff-only HEAD"),
    ],
    ids=["ruff-format-check", "git-merge-ff-only"],
)
def test_the_nul_gate_refuses_a_command_allowed_without_the_nul(
    tmp_path: Path, command: str, without_nul: str
) -> None:
    """Decision 3: the guard reads the command with the NUL dropped, so without
    the gate these would be vetted, and allowed, as `without_nul`. The allowed
    half keeps the refusal from passing vacuously."""
    assert_allowed(coder(without_nul, tmp_path), without_nul)
    reason = assert_denied(coder(command, tmp_path), repr(command))
    assert_phrase(reason, "NUL byte (U+0000)", repr(command))


@pytest.mark.parametrize(
    "command",
    [command for _, command in NUL_POSITIONS],
    ids=[case_id for case_id, _ in NUL_POSITIONS],
)
def test_the_nul_gate_runs_under_the_non_literal_auditor_policy(
    tmp_path: Path, command: str
) -> None:
    """Decision 3: the gate runs in every mode, because the extraction is shared.
    The auditor sets no DENY_ADVICE, so no final paragraph (decision 11)."""
    reason = assert_denied(auditor(command, tmp_path), f"{command!r} (auditor)")
    assert_phrase(reason, "NUL byte (U+0000)", f"{command!r} under the auditor's policy")
    assert FINAL_SENTENCE not in reason, reason


# Decision 3: every C0 control character except tab (0x09) and newline (0x0A),
# which are dealt with separately, and DEL.
CONTROL_CODES = [*range(0x01, 0x09), *range(0x0B, 0x20), 0x7F]


@pytest.mark.parametrize("code", CONTROL_CODES, ids=[f"0x{code:02x}" for code in CONTROL_CODES])
def test_literal_mode_refuses_a_control_character(tmp_path: Path, code: int) -> None:
    """Decisions 3 and 11: a control character in an otherwise-allowed command is
    refused through the literal-mode denial. CR, VT, FF, ESC, BEL and DEL (brief
    T1) are among the cases."""
    command = f"ls a{chr(code)}b"
    reason = assert_denied(coder(command, tmp_path), repr(command))
    assert_phrase(reason, "must be literal", repr(command))
    assert_phrase(reason, "git commit -F", repr(command))
    if code != 0x7F:
        # Decisions 3 and 11 name a C0 byte this way. Whether DEL is named the
        # same way is not stated, so it is not pinned for DEL.
        assert_phrase(reason, "a control character other than tab", repr(command))
    assert reason.endswith(f" {FINAL_PARAGRAPH}"), reason


def test_literal_mode_refuses_a_trailing_carriage_return(tmp_path: Path) -> None:
    """Decision 3: `git status\\r` is refused outright, not vetted as `git status`."""
    command = "git status\r"
    reason = assert_denied(coder(command, tmp_path), repr(command))
    assert_phrase(reason, "must be literal", repr(command))


@pytest.mark.parametrize("command", ["ls\tservices", "git\tstatus"])
def test_literal_mode_allows_a_tab_as_a_blank(tmp_path: Path, command: str) -> None:
    """Decision 3: tab stays allowed; it is one of bash's blanks."""
    assert_allowed(coder(command, tmp_path), repr(command))


def test_control_characters_are_not_a_literal_refusal_under_the_auditor(tmp_path: Path) -> None:
    """Decision 3: control characters are refused only in literal mode. The
    auditor may still refuse the command for another reason, but never as
    `must be literal`."""
    command = "ls a\rb"
    result = auditor(command, tmp_path)
    if result.returncode == 0:
        assert_allowed(result, f"{command!r} (auditor)")
        return
    reason = assert_denied(result, f"{command!r} (auditor)")
    assert "must be literal" not in reason, (
        f"the auditor's policy runs no literal check, so {command!r} must not be "
        f"refused as non-literal.\nreason: {reason!r}"
    )


NON_STRING_COMMANDS: list[tuple[str, Any]] = [
    ("number", 42),
    ("array", ["ls"]),
    ("object", {"a": 1}),
]


@pytest.mark.parametrize("agent", ["coder", "security-auditor"])
@pytest.mark.parametrize(
    "value",
    [value for _, value in NON_STRING_COMMANDS],
    ids=[case_id for case_id, _ in NON_STRING_COMMANDS],
)
def test_a_command_that_is_not_a_string_fails_closed(
    tmp_path: Path, agent: str, value: Any
) -> None:
    """Decision 3: only a clean false passes the gate. A non-string command makes
    the check fail, and decision 11's could-not-be-checked denial follows."""
    result = run_guard_tool_input(
        {"command": value}, policy=NUL_GATE_POLICIES[agent], cwd=tmp_path, agent_type=agent
    )
    what = f"a command of {value!r} from {agent}"
    reason = assert_denied(result, what)
    assert_phrase(reason, "could not be checked for a NUL byte", what)
    if agent == "coder":
        assert reason.endswith(f" {FINAL_PARAGRAPH}"), reason
    else:
        assert FINAL_SENTENCE not in reason, reason


@pytest.mark.parametrize("agent", ["coder", "security-auditor"])
def test_the_not_checked_denial_is_decision_11s_text(tmp_path: Path, agent: str) -> None:
    """Decision 11: the could-not-be-checked text verbatim, with any status N,
    and the final paragraph only for the coder."""
    result = run_guard_tool_input(
        {"command": 42}, policy=NUL_GATE_POLICIES[agent], cwd=tmp_path, agent_type=agent
    )
    reason = assert_denied(result, f"a command of 42 from {agent}")
    body = reason
    if agent == "coder":
        assert reason.endswith(f" {FINAL_PARAGRAPH}"), reason
        body = reason.removesuffix(f" {FINAL_PARAGRAPH}")
    assert NOT_CHECKED_PATTERN.fullmatch(body), (
        f"the could-not-be-checked denial for {agent} must be decision 11's text "
        f"verbatim, with N a status number.\nreason: {reason!r}"
    )


@pytest.mark.parametrize(
    "tool_input",
    [{}, {"command": None}],
    ids=["absent", "null"],
)
def test_a_missing_command_passes_the_gate_as_empty(
    tmp_path: Path, tool_input: dict[str, Any]
) -> None:
    """Decision 3: `// ""` makes an absent or null command the empty string, the
    check exits 1, and the guard exits 0 at the empty-command check."""
    result = run_guard_tool_input(tool_input, policy=CODER_POLICY, cwd=tmp_path, agent_type="coder")
    assert_allowed(result, f"tool_input {tool_input!r} under the coder policy")


# --- P. a payload the guard cannot read, and a guard that fails (decision 19) --
#
# ADR-0018's fifth amendment (2026-09-25), brief T4, item 7. The payloads decision
# 19 needs cannot be built by `run_guard`, so they go through `run_guard_stdin`,
# which sends a stdin text unchanged. Wherever a payload has a well-formed
# `tool_input` and `tool_name`, they are `{"command": "git status"}` and `"Bash"`,
# a command every policy below allows or passes through.


def status_pattern(template: str) -> re.Pattern[str]:
    """`template`, verbatim, with its `status N` matching any status number, which
    brief T4 says not to pin."""
    head, _, tail = template.partition("status N")
    return re.compile(rf"{re.escape(head)}status \d+{re.escape(tail)}")


# Decision 19's bash shape denial, verbatim, with the ADR's blockquote line breaks
# joined by single spaces and N standing for the status.
BASH_SHAPE_TEMPLATE = (
    "Hammertime bash guard: the hook payload could not be read as a single tool "
    "call (the check ended with status N). A payload must be one JSON object "
    "whose tool_input is an object, whose tool_name is a string, and whose cwd "
    "and agent_type are strings, null or absent; without that, the guard cannot "
    "tell what command would run or who sent it. The command is refused."
)
BASH_SHAPE_PATTERN = status_pattern(BASH_SHAPE_TEMPLATE)

# Decision 19's bash backstop denial, the same way.
BASH_BACKSTOP_TEMPLATE = (
    "Hammertime bash guard: the guard stopped with status N before reaching a "
    "verdict, so it cannot vouch for this command. The command is refused."
)
BASH_BACKSTOP_PATTERN = status_pattern(BASH_BACKSTOP_TEMPLATE)

# Decision 19's table of phrases the tests pin.
SHAPE_PHRASE = "could not be read as a single tool call"
BACKSTOP_PHRASE = "before reaching a verdict"

# The policy variables `run_guard_stdin` clears: the module's own, and the path
# guard's `PATH_ROOT` (brief T4 asks every new helper to clear it).
ROOTED_POLICY_VARS = (*POLICY_VARS, "PATH_ROOT")

NEEDS_BIN_SH = pytest.mark.skipif(
    not Path("/bin/sh").exists(),
    reason="the failing jq stand-in is a /bin/sh script, and /bin/sh does not exist",
)


def run_guard_stdin(
    stdin: str,
    *,
    policy: Mapping[str, str],
    jq_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Send `stdin` to the guard unchanged (brief T4), with `run_guard`'s
    environment handling, `PATH_ROOT` cleared as well.

    With `jq_dir`, `PATH` is that directory, then `:`, then the inherited
    `PATH`, so a `jq` placed there replaces the real one. The guard itself is
    still started with the absolute `bash` found at import.
    """
    assert GUARD.is_file(), f"{GUARD} does not exist, so no guard can run"

    env = dict(os.environ)
    for name in ROOTED_POLICY_VARS:
        env.pop(name, None)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    if jq_dir is not None:
        env["PATH"] = f"{jq_dir}:{os.environ.get('PATH', '')}"
    env.update(policy)

    return subprocess.run(
        [BASH or "bash", str(GUARD)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def write_failing_jq(directory: Path, status: int) -> None:
    """An executable `jq` in `directory` that prints nothing and exits `status`."""
    jq = directory / "jq"
    jq.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
    jq.chmod(0o755)


ABSENT: Any = object()


def bash_payload(agent_type: str | None, field: str | None = None, value: Any = None) -> str:
    """A well-formed Bash payload for `git status`, from `agent_type`, with `field`
    set to `value`, or removed when `value` is ABSENT, as JSON text."""
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "cwd": str(REPO_ROOT),
    }
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if field is not None:
        if value is ABSENT:
            payload.pop(field, None)
        else:
            payload[field] = value
    return json.dumps(payload)


# Decision 19's payloads (brief T4, items 1 and 7). A stdin that is not one JSON
# object is sent as it stands; the rest are the well-formed payload with one
# field changed, or sent twice.
RAW_PAYLOADS: dict[str, str] = {
    "not-json": "not json",
    "empty": "",
    "array": "[]",
    "string": '"x"',
    "number": "42",
    "null": "null",
}
TWO_PAYLOADS = "two-payloads"
FIELD_CHANGES: dict[str, tuple[str, Any]] = {
    "tool_input-string": ("tool_input", "x"),
    "tool_input-number": ("tool_input", 42),
    "tool_input-array": ("tool_input", ["x"]),
    "tool_input-true": ("tool_input", True),
    "tool_input-false": ("tool_input", False),
    "tool_input-null": ("tool_input", None),
    "tool_input-absent": ("tool_input", ABSENT),
    "tool_name-absent": ("tool_name", ABSENT),
    "tool_name-null": ("tool_name", None),
    "tool_name-number": ("tool_name", 42),
    "cwd-number": ("cwd", 42),
    "cwd-array": ("cwd", ["x"]),
    "agent_type-number": ("agent_type", 42),
    "agent_type-object": ("agent_type", {"a": 1}),
}
MALFORMED_CASES = [*RAW_PAYLOADS, TWO_PAYLOADS, *FIELD_CHANGES]


def malformed_stdin(case: str, agent_type: str | None) -> str:
    """The stdin for one of MALFORMED_CASES, from `agent_type` wherever the
    payload has a well-formed one."""
    if case in RAW_PAYLOADS:
        return RAW_PAYLOADS[case]
    if case == TWO_PAYLOADS:
        one = bash_payload(agent_type)
        return f"{one}\n{one}"
    field, value = FIELD_CHANGES[case]
    return bash_payload(agent_type, field, value)


# Each scenario is an id, the policy and the `agent_type`. `policy={}` sets no
# knob: with no ALLOW_CMDS it polices no well-formed command, and with no
# DENY_ADVICE its denials carry no paragraph (decisions 11 and 19).
SHAPE_SCENARIOS: list[tuple[str, Mapping[str, str], str | None]] = [
    ("coder", CODER_POLICY, "coder"),
    ("security-auditor", AUDITOR_POLICY, "security-auditor"),
    ("no-policy", {}, "coder"),
    ("coder-policy-no-agent-type", CODER_POLICY, None),
    ("auditor-policy-no-agent-type", AUDITOR_POLICY, None),
]


@pytest.mark.parametrize(
    ("policy", "agent_type"),
    [scenario[1:] for scenario in SHAPE_SCENARIOS],
    ids=[scenario[0] for scenario in SHAPE_SCENARIOS],
)
@pytest.mark.parametrize("case", MALFORMED_CASES)
def test_a_payload_the_guard_cannot_read_gets_the_bash_shape_denial(
    case: str, policy: Mapping[str, str], agent_type: str | None
) -> None:
    """Decision 19 and assumptions 54 and 55: the shape check runs before the
    routing, for every caller and under a policy that constrains nothing. The
    denial goes through `deny`, so it ends with decision 11's paragraph, after
    one space, exactly when the policy sets DENY_ADVICE (the coder's), whether
    or not the payload names the agent the policy is scoped to."""
    result = run_guard_stdin(malformed_stdin(case, agent_type), policy=policy)
    what = f"the {case} payload from agent_type={agent_type!r} under {dict(policy)!r}"
    reason = assert_denied(result, what)
    assert SHAPE_PHRASE in reason, reason
    body = reason
    if policy.get("DENY_ADVICE"):
        assert reason.endswith(f" {FINAL_PARAGRAPH}"), reason
        body = reason.removesuffix(f" {FINAL_PARAGRAPH}")
    else:
        assert FINAL_SENTENCE not in reason, reason
    assert BASH_SHAPE_PATTERN.fullmatch(body), (
        f"the shape denial for {what} must be decision 19's text verbatim, with N a "
        f"status number.\nreason: {reason!r}"
    )


@pytest.mark.parametrize(
    ("policy", "agent_type"),
    [scenario[1:] for scenario in SHAPE_SCENARIOS],
    ids=[scenario[0] for scenario in SHAPE_SCENARIOS],
)
def test_the_well_formed_payload_is_judged_as_before(
    policy: Mapping[str, str], agent_type: str | None
) -> None:
    """Decision 19's controls: the well-formed `git status` payload is allowed by
    the coder's and the auditor's policies, passed through by `policy={}`, and
    passed through by each scoped policy when it carries no `agent_type`."""
    result = run_guard_stdin(bash_payload(agent_type), policy=policy)
    assert_allowed(result, f"git status from agent_type={agent_type!r} under {dict(policy)!r}")


@NEEDS_BIN_SH
def test_a_bash_guard_that_fails_after_the_shape_check_gets_the_backstop_denial(
    tmp_path: Path,
) -> None:
    """Decision 19, parts 1 and 3, and assumption 56: with a `jq` that exits 1 and
    prints nothing, the shape check reads status 1 and passes, the first
    extraction line fails, and the `EXIT` trap denies with the backstop text. It
    carries no decision 11 paragraph under any DENY_ADVICE. With the real `jq`,
    the same command is allowed."""
    payload = bash_payload("coder")
    assert_allowed(run_guard_stdin(payload, policy=CODER_POLICY), "git status with the real jq")
    write_failing_jq(tmp_path, 1)
    result = run_guard_stdin(payload, policy=CODER_POLICY, jq_dir=tmp_path)
    what = "git status under the coder policy with a jq that exits 1"
    reason = assert_denied(result, what)
    assert BACKSTOP_PHRASE in reason, reason
    assert FINAL_SENTENCE not in reason, reason
    assert BASH_BACKSTOP_PATTERN.fullmatch(reason), (
        f"the backstop denial for {what} must be decision 19's text verbatim, with N a "
        f"status number, and no paragraph.\nreason: {reason!r}"
    )


@NEEDS_BIN_SH
def test_a_bash_shape_check_that_fails_denies_on_stderr(tmp_path: Path) -> None:
    """Decision 19, parts 2 and 3, and assumption 57: with a `jq` that exits 3 and
    prints nothing, the shape check does not complete, so the shape denial
    follows; `deny`'s own `jq` fails too, so the reason `deny` builds, decision
    11's paragraph included, goes to stderr, and the exit is still 2."""
    write_failing_jq(tmp_path, 3)
    result = run_guard_stdin(bash_payload("coder"), policy=CODER_POLICY, jq_dir=tmp_path)
    what = "git status under the coder policy with a jq that exits 3"
    assert result.returncode == 2, (
        f"expected the guard to DENY {what} (exit 2), got exit {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout == "", f"expected nothing on stdout for {what}; got {result.stdout!r}"
    expected = re.compile(BASH_SHAPE_PATTERN.pattern + re.escape(f" {FINAL_PARAGRAPH}"))
    assert expected.search(result.stderr), (
        f"expected the bash shape denial and decision 11's paragraph on stderr for {what}.\n"
        f"stderr: {result.stderr!r}"
    )
