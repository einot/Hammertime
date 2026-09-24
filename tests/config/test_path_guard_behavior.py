"""Behaviour of `.claude/hooks/path-guard.sh`, the subagent path guard.

The companion module `test_agent_hook_wiring.py` checks that the guard is wired
somewhere it actually fires. This one checks what it does once it is: the script
is run as a real subprocess, fed a PreToolUse JSON payload on stdin and
parametrised through the same env-var assignments a policy in
`.claude/settings.json` puts on its command line.

The contract (see the script's own header, and issue #102):

* exit 0 allows the tool call; exit 2 denies it and writes a JSON object on
  stdout whose `hookSpecificOutput.permissionDecision` is `"deny"`.
* `EXEMPT_GLOBS` wins over `DENY_GLOBS`; `ALLOW_GLOBS`, when set, turns the
  policy into an allowlist. Paths are matched relative to the payload's `cwd`
  or to `CLAUDE_PROJECT_DIR`, with `[[ str == pattern ]]` glob semantics, so a
  bare `*` matches across `/` too.
* An unscoped `Grep`/`Glob` -- no `path` in the payload, or a `path` that is the
  project root -- is denied for a guarded agent. Without that, a guarded agent
  reads restricted content simply by searching the whole project.
* `SCOPE_AGENT_TYPES` routes a session-wide policy at the agent it was written
  for, matching the payload's `agent_type`. Routing is deliberately FAIL-OPEN:
  an absent or unrecognised `agent_type` means "not my policy", not "deny". An
  exit 0 for an out-of-scope caller is therefore not approval, and the tests
  that encode it are encoding a design decision rather than a bug.

The last group of tests runs the policies as they are actually configured in
`.claude/settings.json`, so a typo in a glob list fails here even though the
wiring tests see a perfectly well-formed entry.

ADR-0018 (`docs/adr/0018-coder-bash-policy-literal-commands-and-a-tripwire.md`)
decision 12 widens the coder's Edit/Write fence: agent configuration and
governance, git's internals, ignored executed state (`.venv/`, `__pycache__/`),
files that tools find by name, and `uv.lock`. The coder cases added to
`WRITE_CASES` and the worktree cases below encode it; they fail until the
top-level session applies decision 14 (step W).

ADR-0018 decision 17 (third amendment) adds a NUL gate to the script. The path
is read through a command substitution, which drops NUL bytes, so the guard used
to vet a path with every NUL removed: `uv.lock`, a NUL, then `.py` was vetted,
and allowed, as `uv.lock.py`. The gate runs after the `SCOPE_AGENT_TYPES`
routing, only under a guarded policy, and before the empty-path check and every
glob list. A path containing a NUL is refused with the NUL denial; a path that
is `true`, a number, an array or an object is refused with the
could-not-be-checked denial; an absent, `null` or `false` path behaves as
before. The tests at the end of this module encode it. Those that expect a NUL
or non-string refusal fail until brief C4 lands.

ADR-0018 decision 18 (fourth amendment) requires a guarded path to be in plain
form. The guard matches a path exactly as written and resolves nothing, so a
path such as `<repo>/tests/../packages/x.py` used to match the test-author's
`tests/*` and reach a file its policy guards. A path is out of plain form when
one of its `/`-separated components is exactly `.` or `..`, when it contains
`//`, or when its first character is `~`. Such a path is refused with the
plain-form denial, whatever it would resolve to, even when its target is in
scope; `..foo`, `x..y`, `...` and `.hidden` are ordinary names. The rule runs
after the routing, only under a guarded policy, after decision 17's NUL gate
and the project-root check, and before every glob list. The tests after
decision 17's encode it. Those that expect the plain-form denial fail until
brief C5 lands.
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
GUARD = CLAUDE_DIR / "hooks" / "path-guard.sh"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"

BASH = shutil.which("bash")
JQ = shutil.which("jq")

pytestmark = pytest.mark.skipif(
    BASH is None or JQ is None,
    reason="path-guard.sh is a bash script that shells out to jq; both must be installed",
)

# Policy variables the guard reads. They are cleared from the inherited
# environment before every run, so a stray value in a developer's shell cannot
# change a verdict.
POLICY_VARS = ("EXEMPT_GLOBS", "DENY_GLOBS", "ALLOW_GLOBS", "SCOPE_AGENT_TYPES")

# Representative repository paths. None of them need to exist: the guard matches
# the path as a string against glob lists and never touches the filesystem.
IMPLEMENTATION_FILE = "packages/hammertime-core/src/hammertime/core/window.py"
TESTKIT_FILE = "packages/hammertime-testkit/src/hammertime/testkit/generators.py"
SERVICE_FILE = "services/ingest/src/hammertime/ingest/app.py"
TOOL_FILE = "tools/replay/src/hammertime/replay/main.py"
NESTED_TEST_FILE = "services/ingest/tests/test_auth.py"
TOP_LEVEL_TEST_FILE = "tests/config/test_path_guard_behavior.py"
SPEC_FILE = "docs/spec/hammertime_spec_1.md"
SCHEMA_FILE = "schemas/observation.json"


# --- running the guard ---------------------------------------------------


def under_repo(relative: str) -> str:
    """An absolute path, which is the shape the tools actually pass."""
    return str(REPO_ROOT / relative)


def run_guard(
    tool_name: str,
    *,
    policy: Mapping[str, str] | None = None,
    file_path: str | None = None,
    search_path: str | None = None,
    agent_type: str | None = None,
    cwd: str | None = None,
    project_dir: str | None = None,
    script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the guard exactly as a PreToolUse hook would."""
    guard = GUARD if script is None else script
    assert guard.is_file(), f"{guard} does not exist, so no guard can run"

    tool_input: dict[str, Any] = {}
    if file_path is not None:
        tool_input["file_path"] = file_path
    if search_path is not None:
        tool_input["path"] = search_path
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(REPO_ROOT) if cwd is None else cwd,
    }
    if agent_type is not None:
        payload["agent_type"] = agent_type

    env = dict(os.environ)
    for name in POLICY_VARS:
        env.pop(name, None)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT) if project_dir is None else project_dir
    env.update(policy or {})

    return subprocess.run(
        [BASH or "bash", str(guard)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def assert_allowed(result: subprocess.CompletedProcess[str], what: str) -> None:
    assert result.returncode == 0, (
        f"expected the guard to ALLOW {what} (exit 0), got exit {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def assert_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    assert result.returncode == 2, (
        f"expected the guard to DENY {what} (exit 2), got exit {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse", decision
    assert decision["permissionDecision"] == "deny", decision
    reason = decision.get("permissionDecisionReason", "")
    assert "Hammertime path guard" in reason, (
        "a denial must be attributable to this guard: the refusal text is the only way "
        "to tell a live guard from the platform's own sandbox refusal, so a denial that "
        "does not name the guard is not evidence the guard ran.\n"
        f"reason: {reason!r}"
    )
    return str(reason)


DENY_TESTS = {"DENY_GLOBS": "tests/* */tests/*"}
ALLOW_DOCS = {"ALLOW_GLOBS": "docs/* schemas/*"}


# --- denylist and allowlist ----------------------------------------------


def test_path_matching_deny_globs_is_denied() -> None:
    result = run_guard("Edit", policy=DENY_TESTS, file_path=under_repo(TOP_LEVEL_TEST_FILE))
    assert_denied(result, TOP_LEVEL_TEST_FILE)


def test_path_not_matching_deny_globs_is_allowed() -> None:
    result = run_guard("Edit", policy=DENY_TESTS, file_path=under_repo(IMPLEMENTATION_FILE))
    assert_allowed(result, IMPLEMENTATION_FILE)


def test_nested_test_directory_matches_a_star_crossing_slashes() -> None:
    """`*` spans `/` under `[[ str == pattern ]]`, which is what `*/tests/*`
    relies on to reach a service's own test directory."""
    result = run_guard("Write", policy=DENY_TESTS, file_path=under_repo(NESTED_TEST_FILE))
    assert_denied(result, NESTED_TEST_FILE)


def test_path_given_relative_to_the_project_is_matched_too() -> None:
    result = run_guard("Edit", policy=DENY_TESTS, file_path=TOP_LEVEL_TEST_FILE)
    assert_denied(result, f"the relative path {TOP_LEVEL_TEST_FILE}")


def test_allow_globs_denies_a_path_outside_the_allowlist() -> None:
    result = run_guard("Write", policy=ALLOW_DOCS, file_path=under_repo(IMPLEMENTATION_FILE))
    reason = assert_denied(result, IMPLEMENTATION_FILE)
    assert "ALLOW_GLOBS" in reason, reason


def test_allow_globs_allows_a_path_inside_the_allowlist() -> None:
    result = run_guard("Write", policy=ALLOW_DOCS, file_path=under_repo(SPEC_FILE))
    assert_allowed(result, SPEC_FILE)


def test_exempt_globs_beats_deny_globs() -> None:
    policy = {
        "EXEMPT_GLOBS": "packages/hammertime-testkit/*",
        "DENY_GLOBS": "packages/*",
    }
    result = run_guard("Read", policy=policy, file_path=under_repo(TESTKIT_FILE))
    assert_allowed(result, f"{TESTKIT_FILE} (exempt from a denied subtree)")


def test_deny_globs_still_applies_to_a_non_exempt_sibling() -> None:
    policy = {
        "EXEMPT_GLOBS": "packages/hammertime-testkit/*",
        "DENY_GLOBS": "packages/*",
    }
    result = run_guard("Read", policy=policy, file_path=under_repo(IMPLEMENTATION_FILE))
    assert_denied(result, IMPLEMENTATION_FILE)


def test_agent_with_no_path_policy_is_not_guarded() -> None:
    """A hook entry that constrains no paths must not start denying things."""
    result = run_guard("Read", policy={}, file_path=under_repo(IMPLEMENTATION_FILE))
    assert_allowed(result, "any path when neither DENY_GLOBS nor ALLOW_GLOBS is set")


def test_grep_path_field_is_used_when_there_is_no_file_path() -> None:
    result = run_guard("Grep", policy={"DENY_GLOBS": "packages/*"}, search_path="packages/x")
    assert_denied(result, "a Grep whose scope is given as tool_input.path")


# --- unscoped searches ---------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Grep", "Glob"])
def test_unscoped_search_is_denied_for_a_guarded_agent(tool_name: str) -> None:
    """An unscoped Grep/Glob searches the whole project.

    Grep with `output_mode: content` then returns the very lines the guard
    exists to hide, so passing such a call through would make the guard
    advisory rather than enforced.
    """
    result = run_guard(tool_name, policy={"DENY_GLOBS": "packages/*"})
    reason = assert_denied(result, f"an unscoped {tool_name}")
    assert tool_name in reason, reason
    assert "path" in reason, (
        f"the refusal has to tell the agent how to proceed (name an in-scope path): {reason!r}"
    )


@pytest.mark.parametrize("tool_name", ["Grep", "Glob"])
def test_unscoped_search_is_allowed_when_no_policy_constrains_paths(tool_name: str) -> None:
    result = run_guard(tool_name, policy={})
    assert_allowed(result, f"an unscoped {tool_name} for an agent with no path policy")


@pytest.mark.parametrize("root_form", ["absolute", "trailing-slash", "dot", "dot-slash"])
def test_project_root_search_is_denied_like_an_unscoped_one(root_form: str) -> None:
    """A `path` that resolves to the project root has the same exposure as no
    path at all, and must be refused the same way."""
    forms = {
        "absolute": str(REPO_ROOT),
        "trailing-slash": f"{REPO_ROOT}/",
        "dot": ".",
        "dot-slash": "./",
    }
    result = run_guard("Grep", policy={"DENY_GLOBS": "packages/*"}, search_path=forms[root_form])
    assert_denied(result, f"a project-root Grep written as {forms[root_form]!r}")


def test_write_tool_without_a_path_is_not_treated_as_a_search() -> None:
    """The unscoped-search denial covers the tools whose *results* can disclose
    files anywhere. A malformed Edit is not one of them."""
    result = run_guard("Edit", policy={"DENY_GLOBS": "packages/*"})
    assert_allowed(result, "an Edit payload carrying no path")


# --- path normalisation --------------------------------------------------


def test_path_is_relativised_against_the_payload_cwd(tmp_path: Path) -> None:
    """coder runs in a git worktree, so its cwd is not the project checkout.

    A policy's globs are written relative to the repository, so they have to be
    matched against the path relative to whichever root the call came from.
    """
    result = run_guard(
        "Edit",
        policy=DENY_TESTS,
        file_path=str(tmp_path / "tests" / "config" / "test_x.py"),
        cwd=str(tmp_path),
        project_dir=str(REPO_ROOT),
    )
    assert_denied(result, "a guarded path inside a worktree checkout")


def test_path_is_relativised_against_claude_project_dir() -> None:
    result = run_guard(
        "Edit",
        policy=DENY_TESTS,
        file_path=under_repo(TOP_LEVEL_TEST_FILE),
        cwd="",
        project_dir=str(REPO_ROOT),
    )
    assert_denied(result, "a guarded path when only CLAUDE_PROJECT_DIR is known")


# --- scoping -------------------------------------------------------------

SCOPED_TO_CODER = {"SCOPE_AGENT_TYPES": "coder", "DENY_GLOBS": "tests/* */tests/*"}


def test_scoped_policy_applies_to_the_agent_it_names() -> None:
    result = run_guard(
        "Edit",
        policy=SCOPED_TO_CODER,
        file_path=under_repo(TOP_LEVEL_TEST_FILE),
        agent_type="coder",
    )
    assert_denied(result, "coder writing to a test file")


def test_scoped_policy_ignores_a_different_agent() -> None:
    """Fail-open routing, by design: another agent is governed by its own
    policy, and this one knows nothing about what that agent may do."""
    result = run_guard(
        "Edit",
        policy=SCOPED_TO_CODER,
        file_path=under_repo(TOP_LEVEL_TEST_FILE),
        agent_type="test-author",
    )
    assert_allowed(result, "test-author against a policy scoped to coder")


def test_scoped_policy_ignores_a_call_with_no_agent_type() -> None:
    """A top-level session call carries no `agent_type`.

    Denying it would break the session the hook is installed in, and would not
    be a security boundary for it either. "Not my policy" is the right answer,
    not "deny" -- so read this exit 0 as routing, never as approval.
    """
    result = run_guard(
        "Edit",
        policy=SCOPED_TO_CODER,
        file_path=under_repo(TOP_LEVEL_TEST_FILE),
    )
    assert_allowed(result, "a call with no agent_type against a scoped policy")


@pytest.mark.parametrize(
    ("agent_type", "denied"),
    [("coder", True), ("test-author", True), ("architect", False)],
    ids=["coder", "test-author", "architect"],
)
def test_scope_agent_types_takes_a_list(agent_type: str, denied: bool) -> None:
    policy = {"SCOPE_AGENT_TYPES": "coder test-author", "DENY_GLOBS": "docs/*"}
    result = run_guard(
        "Edit", policy=policy, file_path=under_repo(SPEC_FILE), agent_type=agent_type
    )
    if denied:
        assert_denied(result, f"{agent_type} (named in SCOPE_AGENT_TYPES)")
    else:
        assert_allowed(result, f"{agent_type} (not named in SCOPE_AGENT_TYPES)")


def test_scoping_also_gates_the_unscoped_search_denial() -> None:
    policy = {"SCOPE_AGENT_TYPES": "test-author", "DENY_GLOBS": "packages/*"}
    assert_denied(
        run_guard("Grep", policy=policy, agent_type="test-author"),
        "an unscoped Grep from the agent the policy names",
    )
    assert_allowed(
        run_guard("Grep", policy=policy, agent_type="reviewer"),
        "an unscoped Grep from an agent the policy does not name",
    )


@pytest.mark.parametrize("agent_type", [None, "coder", "architect"])
def test_unset_scope_applies_to_every_caller(agent_type: str | None) -> None:
    """Legacy behaviour, unchanged: a policy naming no agent polices everyone
    that reaches it. That is why the wiring tests insist on a scope."""
    result = run_guard(
        "Edit",
        policy=DENY_TESTS,
        file_path=under_repo(TOP_LEVEL_TEST_FILE),
        agent_type=agent_type,
    )
    assert_denied(result, f"agent_type={agent_type!r} against an unscoped policy")


# --- the policies as they are actually configured ------------------------
#
# These run the real command lines out of .claude/settings.json, so a glob list
# that is well-formed but wrong fails here. The settings parsing is deliberately
# duplicated from test_agent_hook_wiring.py rather than imported: each file in
# this package stays runnable on its own.


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


def configured_policy(agent_name: str, tools: frozenset[str]) -> tuple[dict[str, str], Path]:
    """The live policy for one agent and tool set, read from settings.json."""
    if not SETTINGS_PATH.is_file():
        pytest.fail(f"{SETTINGS_PATH} does not exist; no policy is wired for {agent_name}")
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        matched = {part.strip() for part in entry.get("matcher", "").split("|")}
        if not tools <= matched:
            continue
        for hook in entry.get("hooks", []):
            env, script = _split_command(hook.get("command", ""))
            if agent_name in env.get("SCOPE_AGENT_TYPES", "").split():
                return env, script
    pytest.fail(
        f"no PreToolUse policy in {SETTINGS_PATH} matches {sorted(tools)} and names "
        f"'{agent_name}' in SCOPE_AGENT_TYPES; see test_agent_hook_wiring.py"
    )


WRITE_CASES = [
    ("coder", IMPLEMENTATION_FILE, True),
    ("coder", SERVICE_FILE, True),
    ("coder", TOP_LEVEL_TEST_FILE, False),
    ("coder", NESTED_TEST_FILE, False),
    ("coder", SPEC_FILE, False),
    ("coder", SCHEMA_FILE, False),
    ("architect", SPEC_FILE, True),
    ("architect", SCHEMA_FILE, True),
    ("architect", IMPLEMENTATION_FILE, False),
    ("architect", TOP_LEVEL_TEST_FILE, False),
    ("test-author", TOP_LEVEL_TEST_FILE, True),
    ("test-author", NESTED_TEST_FILE, True),
    ("test-author", TESTKIT_FILE, True),
    ("test-author", IMPLEMENTATION_FILE, False),
    ("test-author", SPEC_FILE, False),
    # ADR-0018 decision 12 (a): agent configuration and governance.
    ("coder", ".claude/settings.json", False),
    ("coder", ".claude/hooks/bash-guard.sh", False),
    ("coder", ".claude/agents/coder.md", False),
    ("coder", "CLAUDE.md", False),
    ("coder", "services/trie/CLAUDE.md", False),
    ("coder", ".mcp.json", False),
    # Decision 12 (b): git's internals and executed state git does not show.
    ("coder", ".git/config", False),
    ("coder", ".venv/lib/python3.12/site-packages/x.pth", False),
    ("coder", "services/trie/src/hammertime/trie/__pycache__/x.cpython-312.pyc", False),
    # Decision 12 (c): pytest collection and conftest loading.
    ("coder", "packages/hammertime-core/conftest.py", False),
    ("coder", "packages/hammertime-core/src/hammertime/core/test_scratch.py", False),
    ("coder", "packages/hammertime-core/src/hammertime/core/scratch_test.py", False),
    ("coder", "test_notes.txt", False),
    ("coder", "services/trie/test_notes.txt", False),
    # Decision 12 (c): tool configuration found by name.
    ("coder", "pytest.ini", False),
    ("coder", "pytest.toml", False),
    ("coder", ".pytest.ini", False),
    ("coder", "services/trie/tox.ini", False),
    ("coder", "services/trie/setup.cfg", False),
    ("coder", "mypy.ini", False),
    ("coder", ".mypy.ini", False),
    ("coder", "services/trie/ruff.toml", False),
    ("coder", "uv.toml", False),
    ("coder", ".python-version", False),
    ("coder", "packages/hammertime-core/src/sitecustomize.py", False),
    # Decision 12 (c): entries that shadow a tool or the Makefile.
    ("coder", "pytest/__main__.py", False),
    ("coder", "ruff/__main__.py", False),
    ("coder", "mypy/__main__.py", False),
    ("coder", "GNUmakefile", False),
    ("coder", "makefile", False),
    # Decision 12 (d), and (b)'s `/*` for a path outside both checkouts, passed
    # as given (joining an absolute path onto REPO_ROOT leaves it unchanged).
    ("coder", "uv.lock", False),
    ("coder", "/tmp/x.txt", False),
    # Decision 12: what the coder legitimately edits stays writable.
    ("coder", "Makefile", True),
    ("coder", "pyproject.toml", True),
    ("coder", "packages/hammertime-core/pyproject.toml", True),
    ("coder", "ruff.toml", True),
    ("coder", ".commit-msg", True),
    ("coder", "deploy/docker-compose.yml", True),
    ("coder", "README.md", True),
    ("coder", "CHANGES", True),
    ("coder", ".github/workflows/ci.yml", True),
    ("coder", "services/trie/src/hammertime/trie/query/app.py", True),
]


@pytest.mark.parametrize(
    ("agent_name", "path", "allowed"),
    WRITE_CASES,
    ids=[f"{agent}-{'allow' if ok else 'deny'}-{path}" for agent, path, ok in WRITE_CASES],
)
def test_configured_write_policy(agent_name: str, path: str, allowed: bool) -> None:
    """Each agent's configured Edit|Write policy admits exactly its own domain."""
    policy, script = configured_policy(agent_name, frozenset({"Edit", "Write"}))
    result = run_guard(
        "Write",
        policy=policy,
        file_path=under_repo(path),
        agent_type=agent_name,
        script=script,
    )
    if allowed:
        assert_allowed(result, f"{agent_name} writing {path}")
    else:
        assert_denied(result, f"{agent_name} writing {path}")


# ADR-0018 decision 12, from inside a worktree: `cwd` is the coder's worktree and
# CLAUDE_PROJECT_DIR the main checkout. `{worktree}` and `{repo}` are filled in
# with tmp_path and REPO_ROOT.
WORKTREE_WRITE_CASES = [
    ("{worktree}/.claude/settings.json", False),
    ("{repo}/.claude/settings.json", False),
    ("{repo}/.claude/worktrees/other/services/x.py", False),
    ("{worktree}/../x.txt", False),
    ("{worktree}/services/x.py", True),
]


@pytest.mark.parametrize(
    ("template", "allowed"),
    WORKTREE_WRITE_CASES,
    ids=[f"{'allow' if ok else 'deny'}-{template}" for template, ok in WORKTREE_WRITE_CASES],
)
def test_configured_coder_write_policy_from_a_worktree(
    tmp_path: Path, template: str, allowed: bool
) -> None:
    """The coder's fence holds for its own worktree, the main checkout's
    `.claude/`, other agents' worktrees, and a traversal the Write tool did not
    normalise (`../*`)."""
    path = template.format(worktree=tmp_path, repo=REPO_ROOT)
    policy, script = configured_policy("coder", frozenset({"Edit", "Write"}))
    result = run_guard(
        "Write",
        policy=policy,
        file_path=path,
        agent_type="coder",
        cwd=str(tmp_path),
        project_dir=str(REPO_ROOT),
        script=script,
    )
    if allowed:
        assert_allowed(result, f"coder writing {path} from a worktree")
    else:
        assert_denied(result, f"coder writing {path} from a worktree")


READ_CASES = [
    ("Read", SPEC_FILE, True),
    ("Read", SCHEMA_FILE, True),
    ("Read", TOP_LEVEL_TEST_FILE, True),
    ("Read", NESTED_TEST_FILE, True),
    ("Read", TESTKIT_FILE, True),
    ("Read", IMPLEMENTATION_FILE, False),
    ("Read", SERVICE_FILE, False),
    ("Read", TOOL_FILE, False),
    ("Grep", "docs", True),
    ("Grep", "tests", True),
    ("Grep", "packages", False),
    ("Grep", "packages/hammertime-core/src", False),
    ("Grep", "services", False),
    ("Grep", "tools", False),
]


@pytest.mark.parametrize(
    ("tool_name", "path", "allowed"),
    READ_CASES,
    ids=[f"{tool}-{'allow' if ok else 'deny'}-{path}" for tool, path, ok in READ_CASES],
)
def test_configured_test_author_read_policy(tool_name: str, path: str, allowed: bool) -> None:
    """test-author's configured read guard hides the implementation it tests.

    The directory cases matter as much as the file ones: a Grep with
    `output_mode: content` aimed at an ancestor of a denied subtree recurses
    into it and returns the guarded lines, so the ancestors are denied too.
    """
    policy, script = configured_policy("test-author", frozenset({"Read", "Grep", "Glob"}))
    kwargs: dict[str, Any] = (
        {"file_path": under_repo(path)} if tool_name == "Read" else {"search_path": path}
    )
    result = run_guard(
        tool_name,
        policy=policy,
        agent_type="test-author",
        script=script,
        **kwargs,
    )
    if allowed:
        assert_allowed(result, f"test-author reading {path}")
    else:
        assert_denied(result, f"test-author reading {path}")


def test_configured_test_author_read_policy_denies_an_unscoped_search() -> None:
    policy, script = configured_policy("test-author", frozenset({"Read", "Grep", "Glob"}))
    result = run_guard("Grep", policy=policy, agent_type="test-author", script=script)
    assert_denied(result, "test-author running an unscoped Grep")


def test_configured_policies_ignore_the_top_level_session() -> None:
    """Every policy is scoped, so none of them polices the session itself.

    A settings-level hook is session-wide; if one of these ever stopped naming
    its agent, the top-level session would start being refused by a policy
    written for somebody else.
    """
    policy, script = configured_policy("coder", frozenset({"Edit", "Write"}))
    result = run_guard("Write", policy=policy, file_path=under_repo(SPEC_FILE), script=script)
    assert_allowed(result, "a top-level session write against coder's policy")


# --- decision 17: the NUL gate --------------------------------------------
#
# ADR-0018's third amendment (2026-09-24), brief T2. The NUL goes in the Python
# string; `json.dumps` in `run_guard` encodes it as the \u0000 escape the payload
# needs (assumption 30). A NUL-bearing absolute path is built by string
# concatenation, not through `pathlib`, so nothing on the way can drop or reject
# the NUL. The null, false and non-string cases need a payload `run_guard` cannot
# build, so they go through `run_guard_tool_input`.

# Decision 17's NUL denial, verbatim, with the ADR's blockquote line breaks joined
# by single spaces. The dash is a literal em dash character (U+2014).
NUL_MESSAGE = (
    "Hammertime path guard: the path contains a NUL byte (U+0000), which cannot "
    "be carried through this guard intact — the byte is dropped when the path is "
    "read, so the guard cannot vet the path the tool would actually use. The "
    "tool call is refused."
)

# Decision 17's could-not-be-checked denial, verbatim except for the status N,
# which brief T2 says not to pin.
NOT_CHECKED_HEAD = (
    "Hammertime path guard: the path could not be checked for a NUL byte "
    "(the check ended with status "
)
NOT_CHECKED_TAIL = (
    " instead of a result), so the guard cannot confirm that the path it would "
    "vet is the path the tool would use. The tool call is refused."
)
NOT_CHECKED_PATTERN = re.compile(rf"{re.escape(NOT_CHECKED_HEAD)}\d+{re.escape(NOT_CHECKED_TAIL)}")

# Decision 17's table of phrases the tests pin.
DENIAL_PREFIX = "Hammertime path guard: "
NUL_PHRASE = "NUL byte (U+0000)"
NOT_CHECKED_PHRASE = "could not be checked for a NUL byte"

EDIT_WRITE = frozenset({"Edit", "Write"})
READ_GREP_GLOB = frozenset({"Read", "Grep", "Glob"})
SEARCH_TOOLS = frozenset({"Grep", "Glob"})
TRIE_QUERY_FILE = "services/trie/src/hammertime/trie/query/app.py"
EXACT_NAMES = {"DENY_GLOBS": "uv.lock CLAUDE.md"}


def run_guard_tool_input(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    policy: Mapping[str, str],
    agent_type: str | None = None,
    script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Like `run_guard`, but with the whole `tool_input` given (brief T2).

    `run_guard` sends only string paths and leaves out `None`. This sends
    whatever JSON `tool_input` holds: a path that is `null`, `false`, or not a
    string at all.
    """
    guard = GUARD if script is None else script
    assert guard.is_file(), f"{guard} does not exist, so no guard can run"

    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": dict(tool_input),
        "cwd": str(REPO_ROOT),
    }
    if agent_type is not None:
        payload["agent_type"] = agent_type

    env = dict(os.environ)
    for name in POLICY_VARS:
        env.pop(name, None)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
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


def run_with_path(
    tool_name: str,
    path: str,
    *,
    policy: Mapping[str, str],
    agent_type: str | None = None,
    script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """`path` as a Grep or Glob `path`, or as any other tool's `file_path`."""
    if tool_name in SEARCH_TOOLS:
        return run_guard(
            tool_name,
            policy=policy,
            search_path=path,
            agent_type=agent_type,
            script=script,
        )
    return run_guard(
        tool_name,
        policy=policy,
        file_path=path,
        agent_type=agent_type,
        script=script,
    )


def with_nul_inside(path: str) -> str:
    """`path` with a NUL inserted halfway, so that its NUL-stripped form, the
    string the guard vetted before the gate, is `path` itself."""
    middle = len(path) // 2
    return f"{path[:middle]}\x00{path[middle:]}"


def assert_nul_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 17's NUL denial: exit 2, the JSON deny, and its phrase."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert NUL_PHRASE in reason, (
        f"expected decision 17's NUL denial for {what}, not another refusal.\nreason: {reason!r}"
    )
    return reason


def assert_not_checked_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 17's could-not-be-checked denial, with any status number."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert NOT_CHECKED_PHRASE in reason, (
        f"expected decision 17's could-not-be-checked denial for {what}.\nreason: {reason!r}"
    )
    assert NOT_CHECKED_PATTERN.fullmatch(reason), (
        f"the could-not-be-checked denial for {what} must be decision 17's text "
        f"verbatim, with N a status number.\nreason: {reason!r}"
    )
    return reason


def assert_allowed_silently(result: subprocess.CompletedProcess[str], what: str) -> None:
    """Exit 0 with nothing on stdout: no decision at all."""
    assert_allowed(result, what)
    assert result.stdout == "", f"expected empty stdout for {what}; got {result.stdout!r}"


# Decision 17, brief T2 item 1: a NUL anywhere is refused.

IMPLEMENTATION_PATH = under_repo(IMPLEMENTATION_FILE)

NUL_POSITIONS = [
    ("start", f"\x00{IMPLEMENTATION_PATH}"),
    ("middle", with_nul_inside(IMPLEMENTATION_PATH)),
    ("end", f"{IMPLEMENTATION_PATH}\x00"),
]


@pytest.mark.parametrize(
    "path",
    [path for _, path in NUL_POSITIONS],
    ids=[case_id for case_id, _ in NUL_POSITIONS],
)
def test_a_nul_anywhere_in_an_allowed_path_is_refused(path: str) -> None:
    """Decision 17: a NUL at the start, in the middle or at the end of a path the
    policy allows is refused with the NUL denial. The same path without the NUL
    is allowed, which keeps the refusal from passing vacuously."""
    clean = run_guard("Write", policy=DENY_TESTS, file_path=IMPLEMENTATION_PATH)
    assert_allowed(clean, IMPLEMENTATION_FILE)
    result = run_guard("Write", policy=DENY_TESTS, file_path=path)
    assert_nul_denied(result, f"a Write of {path!r}")


def test_a_write_whose_path_is_only_a_nul_is_refused() -> None:
    """Decision 17: a path that is only a NUL comes out of the extraction empty,
    and a Write carrying no path is allowed. The gate runs before that check, so
    the NUL is refused instead."""
    assert_allowed(run_guard("Write", policy=DENY_TESTS), "a Write carrying no path")
    result = run_guard("Write", policy=DENY_TESTS, file_path="\x00")
    assert_nul_denied(result, "a Write whose file_path is only a NUL")


def test_a_grep_whose_path_is_only_a_nul_gets_the_nul_denial() -> None:
    """Decision 17: refused as a NUL, not as an unscoped search, because the gate
    runs before the empty-path check."""
    result = run_guard("Grep", policy=DENY_TESTS, search_path="\x00")
    assert_nul_denied(result, "a Grep whose path is only a NUL")


# Decision 17, brief T2 item 2: an exact-name protected file.

EXACT_NAME_NUL_CASES = [
    ("uv.lock\x00.py", "uv.lock.py"),
    ("CLAUDE.md\x00x", "CLAUDE.mdx"),
]


def test_an_exact_protected_name_is_refused_by_its_deny_glob() -> None:
    """Decision 17's premise: `uv.lock` itself is refused by `DENY_GLOBS`, and
    not by the gate."""
    result = run_guard("Write", policy=EXACT_NAMES, file_path=under_repo("uv.lock"))
    reason = assert_denied(result, "uv.lock under DENY_GLOBS='uv.lock CLAUDE.md'")
    assert NUL_PHRASE not in reason, reason
    assert NOT_CHECKED_PHRASE not in reason, reason


@pytest.mark.parametrize(
    ("name", "stripped"),
    EXACT_NAME_NUL_CASES,
    ids=["uv.lock-NUL-.py", "CLAUDE.md-NUL-x"],
)
def test_a_nul_after_an_exact_protected_name_is_refused(name: str, stripped: str) -> None:
    """Decision 17: before the gate, `name` was vetted as `stripped`, which no
    exact-name glob matches, and so allowed. `stripped` is still allowed, which
    keeps the refusal of `name` from passing vacuously."""
    allowed = run_guard("Write", policy=EXACT_NAMES, file_path=under_repo(stripped))
    assert_allowed(allowed, stripped)
    path = f"{REPO_ROOT}/{name}"
    result = run_guard("Write", policy=EXACT_NAMES, file_path=path)
    assert_nul_denied(result, f"a Write of {path!r}")


def test_the_configured_coder_policy_refuses_uv_lock_followed_by_a_nul() -> None:
    """Decision 17, and decision 12's first bullet: the gate refuses the path for
    every agent the script serves, before step W adds the coder's exact-name
    globs and after."""
    policy, script = configured_policy("coder", EDIT_WRITE)
    path = f"{REPO_ROOT}/uv.lock\x00x"
    result = run_guard(
        "Write",
        policy=policy,
        file_path=path,
        agent_type="coder",
        script=script,
    )
    assert_nul_denied(result, f"coder writing {path!r}")


# Decision 17, brief T2 item 3: every agent and tool path-guard serves.

CONFIGURED_NUL_CASES: list[tuple[str, frozenset[str], str, str]] = [
    ("coder", EDIT_WRITE, "Write", TRIE_QUERY_FILE),
    ("coder", EDIT_WRITE, "Edit", TRIE_QUERY_FILE),
    ("architect", EDIT_WRITE, "Write", SPEC_FILE),
    ("architect", EDIT_WRITE, "Edit", SPEC_FILE),
    ("test-author", EDIT_WRITE, "Write", TOP_LEVEL_TEST_FILE),
    ("test-author", EDIT_WRITE, "Edit", TOP_LEVEL_TEST_FILE),
    ("test-author", READ_GREP_GLOB, "Read", TOP_LEVEL_TEST_FILE),
    ("test-author", READ_GREP_GLOB, "Grep", "tests"),
    ("test-author", READ_GREP_GLOB, "Glob", "tests"),
]


@pytest.mark.parametrize(
    ("agent_name", "tools", "tool_name", "path"),
    CONFIGURED_NUL_CASES,
    ids=[f"{agent}-{tool}-{path}" for agent, _, tool, path in CONFIGURED_NUL_CASES],
)
def test_every_configured_policy_refuses_a_nul_in_a_path_it_allows(
    agent_name: str, tools: frozenset[str], tool_name: str, path: str
) -> None:
    """Decision 17 and assumption 28: every agent and every tool path-guard
    serves. `path` is allowed, before step W and after it; the same path with a
    NUL inside, which the guard would otherwise vet as `path`, is refused."""
    policy, script = configured_policy(agent_name, tools)
    clean = path if tool_name in SEARCH_TOOLS else under_repo(path)
    allowed = run_with_path(
        tool_name,
        clean,
        policy=policy,
        agent_type=agent_name,
        script=script,
    )
    assert_allowed(allowed, f"{agent_name} {tool_name} of {clean}")
    nul_path = with_nul_inside(clean)
    result = run_with_path(
        tool_name,
        nul_path,
        policy=policy,
        agent_type=agent_name,
        script=script,
    )
    assert_nul_denied(result, f"{agent_name} {tool_name} of {nul_path!r}")


def test_the_nul_gate_runs_before_the_test_author_read_exemptions() -> None:
    """Decision 17: the NUL-stripped form of this path matches the `*/tests/*`
    exemption, which exits 0 on a match, so the gate must run before
    `EXEMPT_GLOBS` to see the NUL at all."""
    policy, script = configured_policy("test-author", READ_GREP_GLOB)
    path = f"{REPO_ROOT}/{TRIE_QUERY_FILE}\x00/tests/x"
    result = run_guard(
        "Read",
        policy=policy,
        file_path=path,
        agent_type="test-author",
        script=script,
    )
    assert_nul_denied(result, f"test-author reading {path!r}")


def test_the_nul_gate_does_not_police_a_caller_outside_the_policy_scope() -> None:
    """Decision 17 and assumption 29: the gate runs after the routing, so a call
    with no `agent_type` (the top-level session) passes through untouched. As
    everywhere in this module, that exit 0 is routing, not approval."""
    policy, script = configured_policy("coder", EDIT_WRITE)
    path = with_nul_inside(under_repo(SPEC_FILE))
    result = run_guard("Write", policy=policy, file_path=path, script=script)
    assert_allowed_silently(result, f"a top-level session Write of {path!r}")


@pytest.mark.parametrize("tool_name", ["Read", "Write"])
def test_the_nul_gate_does_not_run_under_a_policy_that_constrains_no_paths(
    tool_name: str,
) -> None:
    """Decision 17 and assumption 29: the gate runs only under a guarded policy,
    so an entry that constrains no paths still denies nothing, as
    `test_agent_with_no_path_policy_is_not_guarded` requires for any path."""
    path = with_nul_inside(IMPLEMENTATION_PATH)
    result = run_guard(tool_name, policy={}, file_path=path)
    assert_allowed(result, f"a {tool_name} of {path!r} with no path policy")


# Decision 17, brief T2 item 4: a non-string path fails closed.

NON_STRING_PATHS: list[tuple[str, Any]] = [
    ("number", 42),
    ("array", ["uv.lock"]),
    ("object", {"a": 1}),
    ("true", True),
]

PATH_FIELDS = [("Write", "file_path"), ("Grep", "path")]


@pytest.mark.parametrize(("tool_name", "field"), PATH_FIELDS, ids=["Write", "Grep"])
@pytest.mark.parametrize(
    "value",
    [value for _, value in NON_STRING_PATHS],
    ids=[case_id for case_id, _ in NON_STRING_PATHS],
)
def test_a_non_string_path_fails_closed(tool_name: str, field: str, value: Any) -> None:
    """Decision 17 and assumption 32: only a clean false passes the gate. The
    check fails on `true`, a number, an array or an object, and the
    could-not-be-checked denial follows."""
    result = run_guard_tool_input(tool_name, {field: value}, policy=DENY_TESTS)
    assert_not_checked_denied(result, f"a {tool_name} whose {field} is {value!r}")


# Decision 17, brief T2 item 5: an absent, null or false path behaves as before.


@pytest.mark.parametrize(
    "tool_input",
    [{}, {"file_path": None}, {"file_path": False}],
    ids=["absent", "null", "false"],
)
def test_a_write_with_no_path_passes_the_gate(tool_input: dict[str, Any]) -> None:
    """Decision 17 and assumption 32: `// ""` makes the value the empty string,
    the check passes, and an Edit or Write with no path exits 0, as before."""
    result = run_guard_tool_input("Write", tool_input, policy=DENY_TESTS)
    assert_allowed_silently(result, f"a Write with tool_input {tool_input!r}")


@pytest.mark.parametrize(
    "tool_input",
    [{}, {"path": None}, {"path": False}],
    ids=["absent", "null", "false"],
)
def test_a_grep_with_no_path_gets_the_unscoped_search_denial(tool_input: dict[str, Any]) -> None:
    """Decision 17 and assumption 32: the check passes, and a Grep with no path
    under a guarded policy is refused as an unscoped search, as before, and by
    neither of the gate's denials."""
    result = run_guard_tool_input("Grep", tool_input, policy=DENY_TESTS)
    reason = assert_denied(result, f"a Grep with tool_input {tool_input!r}")
    assert "Grep" in reason, reason
    assert "path" in reason, reason
    assert NUL_PHRASE not in reason, reason
    assert NOT_CHECKED_PHRASE not in reason, reason


# Decision 17, brief T2 item 6: the messages.


@pytest.mark.parametrize(("tool_name", "field"), PATH_FIELDS, ids=["Write", "Grep"])
def test_the_nul_denial_is_decision_17s_text_verbatim(tool_name: str, field: str) -> None:
    """Decision 17 and assumption 34: the NUL denial word for word, the ADR's
    blockquote line breaks read as single spaces. It quotes no path and carries
    no advice paragraph."""
    path = f"{REPO_ROOT}/uv.lock\x00.py"
    result = run_guard_tool_input(tool_name, {field: path}, policy=DENY_TESTS)
    reason = assert_nul_denied(result, f"a {tool_name} whose {field} is {path!r}")
    assert reason == NUL_MESSAGE, (
        "the NUL denial must be decision 17's text verbatim.\n"
        f"expected: {NUL_MESSAGE!r}\nreason:   {reason!r}"
    )


@pytest.mark.parametrize(("tool_name", "field"), PATH_FIELDS, ids=["Write", "Grep"])
def test_the_not_checked_denial_is_decision_17s_text(tool_name: str, field: str) -> None:
    """Decision 17 and assumption 34: the could-not-be-checked denial word for
    word, except for the status number, which is not pinned."""
    result = run_guard_tool_input(tool_name, {field: 42}, policy=DENY_TESTS)
    reason = assert_not_checked_denied(result, f"a {tool_name} whose {field} is 42")
    assert reason.startswith(DENIAL_PREFIX), reason
    assert NOT_CHECKED_PHRASE in reason, reason


# --- decision 18: paths in plain form ---------------------------------------
#
# ADR-0018's fourth amendment (2026-09-24), brief T3. Every path below that is
# not in plain form is built by string concatenation, never through `pathlib`:
# `pathlib` collapses `.` components and repeated slashes, so the guard would be
# handed a plain path and the test would pass or fail for the wrong reason.
# `json.dumps` in the helpers sends each string unchanged. `under_repo` is used
# only for paths that are already plain.

# Decision 18's plain-form denial, verbatim, with the ADR's blockquote line breaks
# joined by single spaces. It is ASCII only.
PLAIN_FORM_MESSAGE = (
    "Hammertime path guard: the path contains a '.' or '..' component, a '//' "
    "or a leading '~'. This guard matches a path exactly as written and resolves "
    "none of these, so it cannot vet the file or directory the tool would "
    "actually use. Give the path without any of them. The tool call is refused."
)

# Decision 18's table of phrases the tests pin.
PLAIN_FORM_PHRASE = "a '.' or '..' component"

# Decision 18's three confirmed cases, and the relative spelling of the first.
TESTKIT_TRAVERSAL = "packages/hammertime-testkit/../hammertime-core/src/hammertime/core/window.py"
TESTKIT_TRAVERSAL_PATH = f"{REPO_ROOT}/{TESTKIT_TRAVERSAL}"
DOCS_TRAVERSAL_PATH = f"{REPO_ROOT}/docs/../services/ingest/x.py"
TESTS_TRAVERSAL_PATH = f"{REPO_ROOT}/tests/../packages/x.py"


def repo_relative_id(path: str) -> str:
    """A test id for `path` that does not depend on where the checkout lives."""
    return path.replace(str(REPO_ROOT), "<repo>")


def run_configured(
    agent_name: str,
    tools: frozenset[str],
    tool_name: str,
    path: str,
) -> subprocess.CompletedProcess[str]:
    """`path` sent by `agent_name`, with its own `agent_type`, under its policy
    for `tools` as configured in `.claude/settings.json`."""
    policy, script = configured_policy(agent_name, tools)
    return run_with_path(tool_name, path, policy=policy, agent_type=agent_name, script=script)


def assert_plain_form_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 18's plain-form denial: exit 2, the JSON deny, and its phrase."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert PLAIN_FORM_PHRASE in reason, (
        f"expected decision 18's plain-form denial for {what}, not another refusal.\n"
        f"reason: {reason!r}"
    )
    return reason


def assert_denied_otherwise(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Refused, and by something other than decision 18's plain-form denial."""
    reason = assert_denied(result, what)
    assert PLAIN_FORM_PHRASE not in reason, (
        f"expected {what} to be refused, but not with decision 18's plain-form denial.\n"
        f"reason: {reason!r}"
    )
    return reason


# Decision 18, brief T3 item 1: the three confirmed cases. Each row is an id, the
# agent, its policy's tools, the tool, the path out of plain form, a path in the
# policy's scope that shares its leading part, and the target's plain path.

CONFIRMED_CASES: list[tuple[str, str, frozenset[str], str, str, str, str]] = [
    (
        "test-author-Read-testkit-to-core",
        "test-author",
        READ_GREP_GLOB,
        "Read",
        TESTKIT_TRAVERSAL_PATH,
        TESTKIT_FILE,
        IMPLEMENTATION_FILE,
    ),
    (
        "architect-Write-docs-to-services",
        "architect",
        EDIT_WRITE,
        "Write",
        DOCS_TRAVERSAL_PATH,
        SPEC_FILE,
        "services/ingest/x.py",
    ),
    (
        "architect-Edit-docs-to-services",
        "architect",
        EDIT_WRITE,
        "Edit",
        DOCS_TRAVERSAL_PATH,
        SPEC_FILE,
        "services/ingest/x.py",
    ),
    (
        "test-author-Write-tests-to-packages",
        "test-author",
        EDIT_WRITE,
        "Write",
        TESTS_TRAVERSAL_PATH,
        TOP_LEVEL_TEST_FILE,
        "packages/x.py",
    ),
]


@pytest.mark.parametrize(
    ("agent_name", "tools", "tool_name", "path", "in_scope", "target"),
    [case[1:] for case in CONFIRMED_CASES],
    ids=[case[0] for case in CONFIRMED_CASES],
)
def test_a_confirmed_traversal_gets_the_plain_form_denial(
    agent_name: str,
    tools: frozenset[str],
    tool_name: str,
    path: str,
    in_scope: str,
    target: str,
) -> None:
    """Decision 18: a `..` after a prefix the policy allows or exempts used to
    match that prefix's glob wherever the `..` then led. The two controls keep
    the refusal from passing vacuously: the leading part is in scope, and the
    target's plain path is refused by the glob lists, not by the rule."""
    allowed = run_configured(agent_name, tools, tool_name, under_repo(in_scope))
    assert_allowed(allowed, f"{agent_name} {tool_name} of {in_scope}")
    direct = run_configured(agent_name, tools, tool_name, under_repo(target))
    assert_denied_otherwise(direct, f"{agent_name} {tool_name} of {target}")
    result = run_configured(agent_name, tools, tool_name, path)
    assert_plain_form_denied(result, f"{agent_name} {tool_name} of {path!r}")


# Decision 18, brief T3 item 2: a Grep's or a Glob's `path`.

SEARCH_CASES = [
    ("Grep", "tests/../packages"),
    ("Glob", f"{REPO_ROOT}/tests/../services"),
    ("Grep", "tests/.."),
    ("Grep", ".."),
    ("Glob", f"{REPO_ROOT}/.."),
    ("Grep", "./tests"),
    ("Glob", "tests/."),
    ("Grep", f"{REPO_ROOT}//packages"),
    ("Glob", "tests//config"),
    ("Grep", "~"),
    ("Grep", "~/x"),
]


@pytest.mark.parametrize(
    ("tool_name", "path"),
    SEARCH_CASES,
    ids=[f"{tool}-{repo_relative_id(path)}" for tool, path in SEARCH_CASES],
)
def test_a_search_path_out_of_plain_form_is_refused(tool_name: str, path: str) -> None:
    """Decision 18: the rule reads the value the script vets, `file_path` falling
    back to `path`, so a search's `path` is held to it as a `file_path` is. A
    `tests/..` would otherwise pass the `tests/*` exemption and search the whole
    project."""
    result = run_configured("test-author", READ_GREP_GLOB, tool_name, path)
    assert_plain_form_denied(result, f"test-author {tool_name} of {path!r}")


def test_a_search_path_with_a_single_trailing_slash_is_plain() -> None:
    """Decision 18: a single trailing `/` is plain, so `tests/` gets the verdict
    the glob lists give it."""
    result = run_configured("test-author", READ_GREP_GLOB, "Grep", "tests/")
    assert_allowed(result, "test-author Grep of 'tests/'")


# Decision 18, brief T3 item 3: the coder.

CODER_CASES = [
    ("services-to-docs-adr", f"{REPO_ROOT}/services/../docs/adr/x.md"),
    ("dot-uv-lock", f"{REPO_ROOT}/./uv.lock"),
]


@pytest.mark.parametrize(
    "path",
    [path for _, path in CODER_CASES],
    ids=[case_id for case_id, _ in CODER_CASES],
)
def test_the_coder_is_refused_a_path_out_of_plain_form(path: str) -> None:
    """Decision 18: the rule covers the coder's Edit and Write as it covers every
    in-scope call under a guarded policy."""
    result = run_configured("coder", EDIT_WRITE, "Write", path)
    assert_plain_form_denied(result, f"coder writing {path!r}")


def test_the_coder_is_refused_its_own_target_spelled_with_a_dotdot() -> None:
    """Decision 18: the rule resolves nothing, so a target in the coder's scope
    spelled with a `..` is refused too, while its plain path is allowed."""
    allowed = run_configured("coder", EDIT_WRITE, "Write", under_repo(TRIE_QUERY_FILE))
    assert_allowed(allowed, f"coder writing {TRIE_QUERY_FILE}")
    path = f"{REPO_ROOT}/services/trie/../trie/src/hammertime/trie/query/app.py"
    result = run_configured("coder", EDIT_WRITE, "Write", path)
    assert_plain_form_denied(result, f"coder writing {path!r}")


def test_the_coder_is_refused_a_dotdot_from_its_worktree(tmp_path: Path) -> None:
    """Decision 18: from a worktree, with `cwd` the worktree and
    CLAUDE_PROJECT_DIR the main checkout, as in the module's worktree cases. The
    rule looks only at the path's own components."""
    policy, script = configured_policy("coder", EDIT_WRITE)
    path = f"{tmp_path}/services/../.claude/settings.json"
    result = run_guard(
        "Write",
        policy=policy,
        file_path=path,
        agent_type="coder",
        cwd=str(tmp_path),
        project_dir=str(REPO_ROOT),
        script=script,
    )
    assert_plain_form_denied(result, "coder writing <worktree>/services/../.claude/settings.json")


# Decision 18, brief T3 item 4: `.`, `//` and a leading `~` in file paths.

FORM_CASES: list[tuple[str, str, frozenset[str], str, str]] = [
    (
        "test-author-Read-dot",
        "test-author",
        READ_GREP_GLOB,
        "Read",
        f"{REPO_ROOT}/./{IMPLEMENTATION_FILE}",
    ),
    (
        "test-author-Read-double-slash",
        "test-author",
        READ_GREP_GLOB,
        "Read",
        f"{REPO_ROOT}//{IMPLEMENTATION_FILE}",
    ),
    (
        "test-author-Read-leading-double-slash",
        "test-author",
        READ_GREP_GLOB,
        "Read",
        "/" + f"{REPO_ROOT}/{IMPLEMENTATION_FILE}",
    ),
    (
        "test-author-Read-relative-dotdot",
        "test-author",
        READ_GREP_GLOB,
        "Read",
        TESTKIT_TRAVERSAL,
    ),
    (
        "architect-Write-dot-in-scope",
        "architect",
        EDIT_WRITE,
        "Write",
        f"{REPO_ROOT}/docs/./x.md",
    ),
    (
        "architect-Write-double-slash-in-scope",
        "architect",
        EDIT_WRITE,
        "Write",
        f"{REPO_ROOT}/docs//x.md",
    ),
    (
        "test-author-Write-leading-tilde",
        "test-author",
        EDIT_WRITE,
        "Write",
        "~/tests/x.py",
    ),
]


@pytest.mark.parametrize(
    ("agent_name", "tools", "tool_name", "path"),
    [case[1:] for case in FORM_CASES],
    ids=[case[0] for case in FORM_CASES],
)
def test_a_dot_a_double_slash_or_a_leading_tilde_is_refused(
    agent_name: str,
    tools: frozenset[str],
    tool_name: str,
    path: str,
) -> None:
    """Decision 18: a `.` component, a `//` anywhere, a leading `~`, and a `..` in
    a relative path are each out of plain form. The architect's two paths name
    targets in its own scope and are refused all the same."""
    result = run_configured(agent_name, tools, tool_name, path)
    assert_plain_form_denied(result, f"{agent_name} {tool_name} of {path!r}")


def test_the_architect_may_write_that_docs_file_in_plain_form() -> None:
    """Decision 18: the control for the architect's `docs/./x.md` and
    `docs//x.md`. Their plain form is in its scope and allowed."""
    result = run_configured("architect", EDIT_WRITE, "Write", under_repo("docs/x.md"))
    assert_allowed(result, "architect writing docs/x.md")


# Decision 18, brief T3 item 5: order and boundaries.


def test_the_nul_gate_runs_before_the_plain_form_rule() -> None:
    """Decision 18, and decision 17: a path with both a NUL and a `..` gets the
    NUL denial, because the rule runs after the NUL gate."""
    path = f"{REPO_ROOT}/tests/../x\x00y"
    result = run_guard("Write", policy=DENY_TESTS, file_path=path)
    reason = assert_nul_denied(result, f"a Write of {path!r}")
    assert PLAIN_FORM_PHRASE not in reason, reason


PROJECT_ROOT_SPELLINGS = [
    ("dot", "."),
    ("dot-slash", "./"),
    ("absolute", str(REPO_ROOT)),
    ("trailing-slash", f"{REPO_ROOT}/"),
    ("trailing-dot", f"{REPO_ROOT}/."),
]


@pytest.mark.parametrize(
    "path",
    [path for _, path in PROJECT_ROOT_SPELLINGS],
    ids=[case_id for case_id, _ in PROJECT_ROOT_SPELLINGS],
)
def test_the_project_root_keeps_its_own_denial(path: str) -> None:
    """Decision 18 and assumption 41: the rule runs after the project-root check,
    so each spelling of the root keeps the handling it had, and a Grep of it is
    refused, but not with the plain-form denial."""
    result = run_configured("test-author", READ_GREP_GLOB, "Grep", path)
    assert_denied_otherwise(result, f"test-author Grep of {path!r}")


def test_the_plain_form_rule_does_not_police_a_caller_outside_the_policy_scope() -> None:
    """Decision 18: the rule runs after the routing, so a call with no
    `agent_type` (the top-level session) passes through untouched. As everywhere
    in this module, that exit 0 is routing, not approval."""
    policy, script = configured_policy("coder", EDIT_WRITE)
    path = f"{REPO_ROOT}/services/../docs/adr/x.md"
    result = run_guard("Write", policy=policy, file_path=path, script=script)
    assert_allowed_silently(result, f"a top-level session Write of {path!r}")


@pytest.mark.parametrize("tool_name", ["Read", "Write"])
def test_the_plain_form_rule_does_not_run_under_a_policy_that_constrains_no_paths(
    tool_name: str,
) -> None:
    """Decision 18: the rule runs only under a guarded policy, so an entry that
    constrains no paths still denies nothing."""
    result = run_guard(tool_name, policy={}, file_path=TESTS_TRAVERSAL_PATH)
    assert_allowed(result, f"a {tool_name} of {TESTS_TRAVERSAL_PATH!r} with no path policy")


# Decision 18, brief T3 item 6: ordinary names are not components.

ORDINARY_NAME_CASES: list[tuple[str, frozenset[str], str, str]] = [
    ("architect", EDIT_WRITE, "Write", f"{REPO_ROOT}/docs/..notes.md"),
    ("architect", EDIT_WRITE, "Write", f"{REPO_ROOT}/docs/x..y.md"),
    ("architect", EDIT_WRITE, "Write", f"{REPO_ROOT}/docs/.../x.md"),
    ("architect", EDIT_WRITE, "Write", f"{REPO_ROOT}/docs/.hidden.md"),
    ("test-author", READ_GREP_GLOB, "Read", f"{REPO_ROOT}/tests/config/..x.py"),
]


@pytest.mark.parametrize(
    ("agent_name", "tools", "tool_name", "path"),
    ORDINARY_NAME_CASES,
    ids=[
        f"{agent}-{tool}-{repo_relative_id(path)}" for agent, _, tool, path in ORDINARY_NAME_CASES
    ],
)
def test_a_name_that_merely_contains_dots_is_ordinary(
    agent_name: str,
    tools: frozenset[str],
    tool_name: str,
    path: str,
) -> None:
    """Decision 18: only a component that is exactly `.` or `..` counts, so
    `..notes.md`, `x..y.md`, `...`, `.hidden.md` and `..x.py` are ordinary names,
    and each path gets the verdict the glob lists give it."""
    result = run_configured(agent_name, tools, tool_name, path)
    assert_allowed(result, f"{agent_name} {tool_name} of {path!r}")


# Decision 18, brief T3 item 7: the message.

MESSAGE_CASES: list[tuple[frozenset[str], str, str]] = [
    (EDIT_WRITE, "Write", TESTS_TRAVERSAL_PATH),
    (READ_GREP_GLOB, "Grep", "tests/../packages"),
]


@pytest.mark.parametrize(
    ("tools", "tool_name", "path"),
    MESSAGE_CASES,
    ids=["Write-file_path", "Grep-path"],
)
def test_the_plain_form_denial_is_decision_18s_text_verbatim(
    tools: frozenset[str],
    tool_name: str,
    path: str,
) -> None:
    """Decision 18 and assumption 44: the plain-form denial word for word, the
    ADR's blockquote line breaks read as single spaces. It quotes no path and
    carries no advice paragraph."""
    result = run_configured("test-author", tools, tool_name, path)
    reason = assert_plain_form_denied(result, f"test-author {tool_name} of {path!r}")
    assert reason.startswith(DENIAL_PREFIX), reason
    assert PLAIN_FORM_PHRASE in reason, reason
    assert reason == PLAIN_FORM_MESSAGE, (
        "the plain-form denial must be decision 18's text verbatim.\n"
        f"expected: {PLAIN_FORM_MESSAGE!r}\nreason:   {reason!r}"
    )
