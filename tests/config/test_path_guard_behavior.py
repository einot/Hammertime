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
"""

import json
import os
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
