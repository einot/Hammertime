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

ADR-0018's fifth amendment adds decisions 19-22. Decision 19: a payload the
guard cannot read as one tool call (not exactly one JSON object, a `tool_input`
that is not an object, a `tool_name` that is not a string, a `cwd` or
`agent_type` that is neither a string nor `null`) is refused with the shape
denial, before the routing, for every caller and under every policy; and a
guard that would end with any status other than 0 or 2 denies instead, with
the backstop denial. Decision 20: under a guarded policy a path must lie inside
the policy's root, which `PATH_ROOT` names (`project`, `cwd`, or unset for the
two bases the guard always used); any other value is a configuration error.
Decision 21: a Glob `pattern` or a Grep `glob` may hold only letters, digits
and `_ - . / * ?`, may not begin with `/` and may not contain `..`. Decision 22
changes the configured lists, which the top-level session applies in step W.
The tests after decision 18's encode them (brief T4). Those for decisions 19-21
fail until brief C6 lands, except their controls; those that read the
configured policies after step W fail until W, except the cases marked as
holding after C6, which fail only until C6 lands.

ADR-0018's sixth amendment (2026-09-25) closes two gaps. Decision 22's read
deny list for the test-author, which decision 14's text carries into step W,
gains the caches of Hypothesis, pytest, ruff and uv, `.git`, coverage's data
files and `snapshots`, each with what lies under it. Decision 20 gains a usable
root: one that begins with `/`, is in plain form by decision 18's four tests,
and is not `/`. Any other root (`/`, `//`, `/.`, one with a `..` component, a
relative one) is an empty root that contains no path, so under PATH_ROOT
`project` or `cwd` every guarded path is refused with the root denial,
unchanged. With PATH_ROOT unset an absolute path is compared only with a usable
base, and a relative path is inside only when `cwd` is usable, or `cwd` is empty
and CLAUDE_PROJECT_DIR is usable. The tests at the end of this module encode it
(brief T5). The read list's refusals fail until step W; the root cases fail
until brief C7 lands, except their controls and the silent passes outside the
root rule's reach.

ADR-0018's seventh amendment (2026-09-25) fixes SA1f's findings. Decision 24:
a path is read from its tool's own field, `file_path` for a Read, Edit or Write
and `path` for a Grep or Glob; a call from any other tool, or one whose other
field is anything but absent, `null` or `false`, is refused, before the NUL
gate. Decision 17 gains a trailing-newline test, directly after the NUL gate.
Decision 18's fourth test refuses a `~` at the start of any component, and a
root with such a component is not usable (decision 20). Decision 23: in a Grep
or a Glob no string may begin with `-`, no component of a Glob pattern or a
Grep glob may begin with `.` and then `*` or `?`, and the search path may hold
only ASCII letters, digits and `_ - . /`. Decision 19 reads at most 8 MiB and
one byte of the payload, keeping a raw NUL as U+0002, which the shape check
refuses with status 0; it turns a status 2 that `deny` did not make into the
backstop denial; and it refuses, with the backstop denial, an extraction that
reads a field as empty that is not, and a `cwd` that ends with a newline or
holds a NUL. Decision 12 (e) and (f) add `tests`, `*/tests` and the testkit to
the coder's fence at step W. The tests at the end of this module encode them
(brief T6). They fail until brief C8 lands, except their controls and silent
passes, the raw U+0002 cases if `jq` already refuses a raw control character,
the `/dev/full` case and the `jq` killed by a signal, which pass today; the
coder's cases for decision 12 fail until step W, except their control.

ADR-0018's eighth amendment (2026-09-26) fixes SA1g's findings. Decision 26:
under a guarded policy, an in-scope call whose path begins or ends with an edge
character (U+0000-U+0020, U+007F-U+00A0, U+1680, U+180E, U+2000-U+200A,
U+2028, U+2029, U+202F, U+205F, U+3000 or U+FEFF) is refused with decision
26's denial, directly after decision 17's trailing-newline test, so before
decisions 18, 20, 21 and 23 and every glob list; a tool that trims its path
would otherwise act on a path no list judged. Such a character inside a path
is judged as written, so decision 17's control for a carriage return at the end
goes. Decision 20, "An empty `cwd`": with PATH_ROOT unset a relative path is
inside the root only when `cwd` is usable, so with `cwd` absent, `null` or
empty it gets the root denial, and T4's follow-up case `cwd-empty-dir-repo`
changes with it. Decision 25's eighth-amendment note: a coder Write whose path
has 200,000 components is decided within the helper's timeout. The tests at the
end of this module encode them (brief T7). Decision 26's refusals and its order
cases, and decision 20's refused cases, fail until brief C9 lands; their
controls and boundaries, and the long path, pass today.
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


# --- decisions 19-22: the fifth amendment ------------------------------------
#
# ADR-0018's fifth amendment (2026-09-25), brief T4. Decision 19 needs payloads
# `run_guard` cannot build, so they go through `run_guard_stdin`, which sends a
# stdin text unchanged. `run_rooted` is `run_guard`'s sibling for the rest: it
# clears `PATH_ROOT` from the inherited environment, as the module's helpers clear
# the other policy variables, and sets it only from the policy it is given. Every
# path out of plain form, and every path with a NUL, is built by string
# concatenation, as in briefs T2 and T3. `str(REPO_ROOT.parent)` stands for the
# directory above the project root, wherever the repository is checked out.


def status_pattern(template: str) -> re.Pattern[str]:
    """`template`, verbatim, with its `status N` matching any status number, which
    brief T4 says not to pin."""
    head, _, tail = template.partition("status N")
    return re.compile(rf"{re.escape(head)}status \d+{re.escape(tail)}")


# Decision 19's shape denial, verbatim, with the ADR's blockquote line breaks
# joined by single spaces and N standing for the status.
SHAPE_TEMPLATE = (
    "Hammertime path guard: the hook payload could not be read as a single tool "
    "call (the check ended with status N). A payload must be one JSON object "
    "whose tool_input is an object, whose tool_name is a string, and whose cwd "
    "and agent_type are strings, null or absent; without that, the guard cannot "
    "tell what the call would act on or who is making it. The tool call is "
    "refused."
)
SHAPE_PATTERN = status_pattern(SHAPE_TEMPLATE)

# Decision 19's backstop denial, the same way.
BACKSTOP_TEMPLATE = (
    "Hammertime path guard: the guard stopped with status N before reaching a "
    "verdict, so it cannot vouch for this tool call. The tool call is refused."
)
BACKSTOP_PATTERN = status_pattern(BACKSTOP_TEMPLATE)

# Decision 20's root denial, verbatim, with the ADR's blockquote line breaks
# joined by single spaces. It is ASCII only.
ROOT_MESSAGE = (
    "Hammertime path guard: the path is not inside this policy's root "
    "directory: the project directory under PATH_ROOT='project', the working "
    "directory under PATH_ROOT='cwd', or either of them when PATH_ROOT is unset. "
    "Under PATH_ROOT='project' a relative path counts as inside only when the "
    "working directory is the project directory. This guard's glob lists judge "
    "only paths inside the root, so it cannot vet this one. Give an absolute "
    "path inside the root. The tool call is refused."
)

# Decision 21's pattern denial, verbatim, the same way.
PATTERN_MESSAGE = (
    "Hammertime path guard: a Glob pattern or a Grep glob may contain only "
    "letters, digits and the characters _ - . / * ?, and may not begin with '/' "
    "or contain '..', because this guard cannot tell where any other pattern "
    "would take the search. Give a pattern in that form, relative to the path "
    "being searched. The tool call is refused."
)

# The phrases decisions 19-21 pin, and the one brief T4 takes from decision 15's
# D3-D8 outcomes for the DENY_GLOBS denial, which the ADR does not quote.
SHAPE_PHRASE = "could not be read as a single tool call"
BACKSTOP_PHRASE = "before reaching a verdict"
ROOT_PHRASE = "is not inside this policy's root directory"
CONFIG_ERROR_PHRASE = "configuration error"
PATTERN_PHRASE = "a Glob pattern or a Grep glob may contain only"
DENY_GLOBS_PHRASE = "matched DENY_GLOBS"

# The policy variables `run_guard_stdin` clears: the module's own, and decision
# 20's `PATH_ROOT`.
ROOTED_POLICY_VARS = (*POLICY_VARS, "PATH_ROOT")

NEEDS_BIN_SH = pytest.mark.skipif(
    not Path("/bin/sh").exists(),
    reason="the failing jq stand-in is a /bin/sh script, and /bin/sh does not exist",
)


def run_guard_stdin(
    stdin: str,
    *,
    policy: Mapping[str, str],
    project_dir: str | None = None,
    jq_dir: Path | None = None,
    script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Send `stdin` to the guard unchanged (brief T4), with `run_guard`'s
    environment handling, `PATH_ROOT` cleared as well.

    With `jq_dir`, `PATH` is that directory, then `:`, then the inherited
    `PATH`, so a `jq` placed there replaces the real one. The guard itself is
    still started with the absolute `bash` found at import.
    """
    guard = GUARD if script is None else script
    assert guard.is_file(), f"{guard} does not exist, so no guard can run"

    env = dict(os.environ)
    for name in ROOTED_POLICY_VARS:
        env.pop(name, None)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT) if project_dir is None else project_dir
    if jq_dir is not None:
        env["PATH"] = f"{jq_dir}:{os.environ.get('PATH', '')}"
    env.update(policy)

    return subprocess.run(
        [BASH or "bash", str(guard)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def tool_payload(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    cwd: str | None = None,
    agent_type: str | None = None,
) -> str:
    """A well-formed PreToolUse payload, as JSON text."""
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": dict(tool_input),
        "cwd": str(REPO_ROOT) if cwd is None else cwd,
    }
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return json.dumps(payload)


def run_rooted(
    tool_name: str,
    *,
    policy: Mapping[str, str],
    file_path: str | None = None,
    search_path: str | None = None,
    tool_input: Mapping[str, Any] | None = None,
    agent_type: str | None = None,
    cwd: str | None = None,
    project_dir: str | None = None,
    script: Path | None = None,
    jq_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """`run_guard`'s sibling for the fifth amendment: the same payload, with any
    further `tool_input` fields, sent through `run_guard_stdin`."""
    fields: dict[str, Any] = dict(tool_input or {})
    if file_path is not None:
        fields["file_path"] = file_path
    if search_path is not None:
        fields["path"] = search_path
    return run_guard_stdin(
        tool_payload(tool_name, fields, cwd=cwd, agent_type=agent_type),
        policy=policy,
        project_dir=project_dir,
        jq_dir=jq_dir,
        script=script,
    )


def run_configured_rooted(
    agent_name: str,
    tools: frozenset[str],
    tool_name: str,
    path: str,
    *,
    cwd: str | None = None,
    project_dir: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """`run_configured` through `run_rooted`: `path` sent by `agent_name`, with
    its own `agent_type`, under its policy for `tools` in settings.json."""
    policy, script = configured_policy(agent_name, tools)
    search = tool_name in SEARCH_TOOLS
    return run_rooted(
        tool_name,
        policy=policy,
        file_path=None if search else path,
        search_path=path if search else None,
        agent_type=agent_name,
        cwd=cwd,
        project_dir=project_dir,
        script=script,
    )


def write_failing_jq(directory: Path, status: int) -> None:
    """An executable `jq` in `directory` that prints nothing and exits `status`."""
    jq = directory / "jq"
    jq.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
    jq.chmod(0o755)


def assert_shape_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 19's shape denial: exit 2, the JSON deny, the prefix, its phrase,
    and its text verbatim but for the status."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert SHAPE_PHRASE in reason, (
        f"expected decision 19's shape denial for {what}, not another refusal.\nreason: {reason!r}"
    )
    assert SHAPE_PATTERN.fullmatch(reason), (
        f"the shape denial for {what} must be decision 19's text verbatim, with N a "
        f"status number.\nreason: {reason!r}"
    )
    return reason


def assert_root_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 20's root denial: exit 2, the JSON deny, the prefix, its phrase."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert ROOT_PHRASE in reason, (
        f"expected decision 20's root denial for {what}, not another refusal.\nreason: {reason!r}"
    )
    return reason


def assert_deny_globs_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """The DENY_GLOBS denial, as brief T4 pins it: the prefix and
    `matched DENY_GLOBS`, and nothing else of its text."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert DENY_GLOBS_PHRASE in reason, (
        f"expected the DENY_GLOBS denial for {what}, not another refusal.\nreason: {reason!r}"
    )
    return reason


def assert_project_root_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """The project-root denial, whose text the ADR pins no phrase of (brief T4):
    a refusal that is not the root, plain-form or shape denial."""
    reason = assert_denied(result, what)
    for phrase in (ROOT_PHRASE, PLAIN_FORM_PHRASE, SHAPE_PHRASE):
        assert phrase not in reason, (
            f"expected the project-root denial for {what}, not a denial containing "
            f"{phrase!r}.\nreason: {reason!r}"
        )
    return reason


def assert_pattern_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 21's pattern denial: exit 2, the JSON deny, the prefix, its phrase."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert PATTERN_PHRASE in reason, (
        f"expected decision 21's pattern denial for {what}, not another refusal.\n"
        f"reason: {reason!r}"
    )
    return reason


# The verdicts the root cases below expect.
VERDICT_ALLOW = "allow"
VERDICT_ROOT = "root"
VERDICT_DENY_GLOBS = "deny-globs"
VERDICT_PLAIN_FORM = "plain-form"
VERDICT_NUL = "nul"
VERDICT_PROJECT_ROOT = "project-root"


def assert_verdict(result: subprocess.CompletedProcess[str], verdict: str, what: str) -> None:
    if verdict == VERDICT_ALLOW:
        assert_allowed_silently(result, what)
    elif verdict == VERDICT_ROOT:
        assert_root_denied(result, what)
    elif verdict == VERDICT_DENY_GLOBS:
        reason = assert_deny_globs_denied(result, what)
        assert ROOT_PHRASE not in reason, reason
    elif verdict == VERDICT_PLAIN_FORM:
        reason = assert_plain_form_denied(result, what)
        assert ROOT_PHRASE not in reason, reason
    elif verdict == VERDICT_NUL:
        reason = assert_nul_denied(result, what)
        assert ROOT_PHRASE not in reason, reason
    elif verdict == VERDICT_PROJECT_ROOT:
        assert_project_root_denied(result, what)
    else:
        raise AssertionError(f"unknown verdict {verdict!r}")


def fill(template: str, tmp_path: Path) -> str:
    """`template` with `{tmp}`, `{repo}` and `{parent}` filled in: `tmp_path`, the
    project root and the directory above it."""
    return template.format(tmp=tmp_path, repo=REPO_ROOT, parent=REPO_ROOT.parent)


def fill_optional(template: str | None, tmp_path: Path) -> str | None:
    return None if template is None else fill(template, tmp_path)


def path_case_id(path: str) -> str:
    """A test id for `path` that does not depend on where the checkout lives."""
    if path == str(REPO_ROOT.parent):
        return "<parent>"
    return repo_relative_id(path)


# Decision 19, brief T4 item 1: payload shape. Every payload below but the
# malformed ones is a Write of IMPLEMENTATION_PATH with no `agent_type`, which each
# of the three policies in SHAPE_POLICIES allows or passes through.

ABSENT: Any = object()


def shape_payload(field: str | None = None, value: Any = None) -> str:
    """The well-formed Write payload, with `field` set to `value`, or removed when
    `value` is ABSENT, as JSON text."""
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": IMPLEMENTATION_PATH},
        "cwd": str(REPO_ROOT),
    }
    if field is not None:
        if value is ABSENT:
            payload.pop(field, None)
        else:
            payload[field] = value
    return json.dumps(payload)


WELL_FORMED_PAYLOAD = shape_payload()

MALFORMED_PAYLOADS: list[tuple[str, str]] = [
    ("not-json", "not json"),
    ("empty", ""),
    ("array", "[]"),
    ("string", '"x"'),
    ("number", "42"),
    ("null", "null"),
    ("two-payloads", f"{WELL_FORMED_PAYLOAD}\n{WELL_FORMED_PAYLOAD}"),
    ("tool_input-string", shape_payload("tool_input", "x")),
    ("tool_input-number", shape_payload("tool_input", 42)),
    ("tool_input-array", shape_payload("tool_input", ["x"])),
    ("tool_input-true", shape_payload("tool_input", True)),
    ("tool_input-false", shape_payload("tool_input", False)),
    ("tool_input-null", shape_payload("tool_input", None)),
    ("tool_input-absent", shape_payload("tool_input", ABSENT)),
    ("tool_name-absent", shape_payload("tool_name", ABSENT)),
    ("tool_name-null", shape_payload("tool_name", None)),
    ("tool_name-number", shape_payload("tool_name", 42)),
    ("cwd-number", shape_payload("cwd", 42)),
    ("cwd-array", shape_payload("cwd", ["x"])),
    ("agent_type-number", shape_payload("agent_type", 42)),
    ("agent_type-object", shape_payload("agent_type", {"a": 1})),
]

# Well formed, so the shape check does not refuse them (decision 19, part 3).
WELL_FORMED_PAYLOADS: list[tuple[str, str]] = [
    ("as-sent", WELL_FORMED_PAYLOAD),
    ("agent_type-null", shape_payload("agent_type", None)),
    ("cwd-null", shape_payload("cwd", None)),
    ("cwd-absent", shape_payload("cwd", ABSENT)),
]

SHAPE_POLICIES: list[tuple[str, Mapping[str, str]]] = [
    ("guarded", DENY_TESTS),
    ("no-policy", {}),
    ("scoped-to-coder", SCOPED_TO_CODER),
]


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in SHAPE_POLICIES],
    ids=[case_id for case_id, _ in SHAPE_POLICIES],
)
@pytest.mark.parametrize(
    "stdin",
    [stdin for _, stdin in MALFORMED_PAYLOADS],
    ids=[case_id for case_id, _ in MALFORMED_PAYLOADS],
)
def test_a_payload_the_guard_cannot_read_gets_the_shape_denial(
    stdin: str, policy: Mapping[str, str]
) -> None:
    """Decision 19 and assumptions 54 and 55: the shape check runs before the
    routing, for every caller and under a policy that constrains nothing, so
    each policy refuses each of these, the scoped one included, although its
    routing would pass a payload with no (or a malformed) `agent_type`."""
    result = run_guard_stdin(stdin, policy=policy)
    assert_shape_denied(result, f"the payload {stdin!r} under {dict(policy)!r}")


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in SHAPE_POLICIES],
    ids=[case_id for case_id, _ in SHAPE_POLICIES],
)
@pytest.mark.parametrize(
    "stdin",
    [stdin for _, stdin in WELL_FORMED_PAYLOADS],
    ids=[case_id for case_id, _ in WELL_FORMED_PAYLOADS],
)
def test_a_well_formed_payload_is_judged_as_before(stdin: str, policy: Mapping[str, str]) -> None:
    """Decision 19's controls: the same Write, well formed, is allowed under the
    guarded policy, allowed under the one that constrains nothing, and passed
    through by the scoped one. A `null` `agent_type` or `cwd`, and an absent
    `cwd`, are well formed."""
    result = run_guard_stdin(stdin, policy=policy)
    assert_allowed_silently(result, f"the payload {stdin!r} under {dict(policy)!r}")


# Decision 19, brief T4 item 2: a failing guard.

FAILING_GUARD_POLICIES: list[tuple[str, Mapping[str, str]]] = [
    ("guarded", DENY_TESTS),
    ("outside-scope", SCOPED_TO_CODER),
]


@NEEDS_BIN_SH
@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in FAILING_GUARD_POLICIES],
    ids=[case_id for case_id, _ in FAILING_GUARD_POLICIES],
)
def test_a_guard_that_fails_after_the_shape_check_gets_the_backstop_denial(
    tmp_path: Path, policy: Mapping[str, str]
) -> None:
    """Decision 19, parts 1 and 3, and assumption 56: with a `jq` that exits 1 and
    prints nothing, the shape check reads status 1 and passes, the first
    extraction line fails, and the `EXIT` trap denies with the backstop text,
    written without `jq`. The extraction lines run before the routing, so a
    caller outside the policy's scope is refused the same way. With the real
    `jq`, the same Write is allowed or passed through."""
    control = run_rooted("Write", policy=policy, file_path=IMPLEMENTATION_PATH)
    assert_allowed_silently(control, f"a Write of {IMPLEMENTATION_FILE} with the real jq")
    write_failing_jq(tmp_path, 1)
    result = run_rooted("Write", policy=policy, file_path=IMPLEMENTATION_PATH, jq_dir=tmp_path)
    what = f"a Write of {IMPLEMENTATION_FILE} with a jq that exits 1"
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert BACKSTOP_PHRASE in reason, (
        f"expected decision 19's backstop denial for {what}.\nreason: {reason!r}"
    )
    assert BACKSTOP_PATTERN.fullmatch(reason), (
        f"the backstop denial for {what} must be decision 19's text verbatim, with N a "
        f"status number.\nreason: {reason!r}"
    )


@NEEDS_BIN_SH
@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in FAILING_GUARD_POLICIES],
    ids=[case_id for case_id, _ in FAILING_GUARD_POLICIES],
)
def test_a_shape_check_that_fails_denies_on_stderr(
    tmp_path: Path, policy: Mapping[str, str]
) -> None:
    """Decision 19, parts 2 and 3, and assumption 57: with a `jq` that exits 3 and
    prints nothing, the shape check does not complete, so the shape denial
    follows; `deny`'s own `jq` fails too, so the reason goes to stderr and the
    exit is still 2. The shape check runs before the routing, for every caller,
    so a payload with no `agent_type` under a policy scoped to the coder is
    refused the same way."""
    write_failing_jq(tmp_path, 3)
    result = run_rooted("Write", policy=policy, file_path=IMPLEMENTATION_PATH, jq_dir=tmp_path)
    what = f"a Write of {IMPLEMENTATION_FILE} with a jq that exits 3"
    assert result.returncode == 2, (
        f"expected the guard to DENY {what} (exit 2), got exit {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout == "", f"expected nothing on stdout for {what}; got {result.stdout!r}"
    assert SHAPE_PATTERN.search(result.stderr), (
        f"expected decision 19's shape denial on stderr for {what}.\nstderr: {result.stderr!r}"
    )


# Decision 20, brief T4 item 3: the root, under explicit policies. `{tmp}`,
# `{repo}` and `{parent}` are filled in by `fill`; a `cwd` or CLAUDE_PROJECT_DIR of
# None is the repository root. No call sets an `agent_type`.

ALLOW_ANY_TESTS = {"ALLOW_GLOBS": "*/tests/*"}
DENY_PACKAGES = {"DENY_GLOBS": "packages packages/*"}
PROJECT_ROOT_POLICY = {"PATH_ROOT": "project", "DENY_GLOBS": "tests/*"}
CWD_ROOT_POLICY = {"PATH_ROOT": "cwd", "DENY_GLOBS": "tests/*"}


def run_root_case(
    tmp_path: Path,
    policy: Mapping[str, str],
    tool_name: str,
    path: str,
    *,
    cwd: str | None = None,
    project_dir: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """`path`, `cwd` and CLAUDE_PROJECT_DIR filled in, then sent as a search's
    `path` or another tool's `file_path`, with no `agent_type`."""
    filled = fill(path, tmp_path)
    search = tool_name in SEARCH_TOOLS
    return run_rooted(
        tool_name,
        policy=policy,
        file_path=None if search else filled,
        search_path=filled if search else None,
        cwd=fill_optional(cwd, tmp_path),
        project_dir=fill_optional(project_dir, tmp_path),
    )


# PATH_ROOT unset, with `cwd` and CLAUDE_PROJECT_DIR both the repository root. Each
# row is an id, the policy, the tool, the path and the verdict. The allowed Write
# is `services/ingest/tests/x.py`, not `tests/x.py`: relativised, the latter has no
# `/` before `tests`, so `*/tests/*` does not match it.
UNSET_ROOT_CASES: list[tuple[str, Mapping[str, str], str, str, str]] = [
    ("outside-allowed-glob", ALLOW_ANY_TESTS, "Write", "{tmp}/tests/x.py", VERDICT_ROOT),
    ("in-repo", ALLOW_ANY_TESTS, "Write", "{repo}/services/ingest/tests/x.py", VERDICT_ALLOW),
    ("grep-tmp", DENY_PACKAGES, "Grep", "{tmp}", VERDICT_ROOT),
    ("grep-parent", DENY_PACKAGES, "Grep", "{parent}", VERDICT_ROOT),
    ("grep-slash", DENY_PACKAGES, "Grep", "/", VERDICT_ROOT),
    ("grep-docs", DENY_PACKAGES, "Grep", "docs", VERDICT_ALLOW),
]


@pytest.mark.parametrize(
    ("policy", "tool_name", "path", "verdict"),
    [case[1:] for case in UNSET_ROOT_CASES],
    ids=[case[0] for case in UNSET_ROOT_CASES],
)
def test_path_root_unset_keeps_both_bases_as_the_root(
    tmp_path: Path, policy: Mapping[str, str], tool_name: str, path: str, verdict: str
) -> None:
    """Decision 20 and assumption 61: with PATH_ROOT unset the root is `cwd` or
    CLAUDE_PROJECT_DIR, and a guarded path under neither is refused with the root
    denial, although `*/tests/*` would match it (gap G1) and a list of repository
    prefixes would not (gap G2). `pytest`'s `tmp_path` lies outside the
    repository."""
    result = run_root_case(tmp_path, policy, tool_name, path)
    assert_verdict(result, verdict, f"a {tool_name} of {path!r} under {dict(policy)!r}")


# PATH_ROOT='project', CLAUDE_PROJECT_DIR the repository root unless stated,
# DENY_GLOBS='tests/*'. Each row is an id, the Write's path, `cwd`,
# CLAUDE_PROJECT_DIR and the verdict.
PROJECT_ROOT_CASES: list[tuple[str, str, str, str | None, str]] = [
    ("cwd-tmp-absolute-tmp", "{tmp}/services/x.py", "{tmp}", None, VERDICT_ROOT),
    ("cwd-tmp-absolute-repo", "{repo}/services/x.py", "{tmp}", None, VERDICT_ALLOW),
    ("cwd-tmp-relative", "services/x.py", "{tmp}", None, VERDICT_ROOT),
    ("cwd-repo-relative", "services/x.py", "{repo}", None, VERDICT_ALLOW),
    ("cwd-repo-relative-tests", "tests/x.py", "{repo}", None, VERDICT_DENY_GLOBS),
    ("cwd-repo-slash-relative", "services/x.py", "{repo}/", None, VERDICT_ALLOW),
    ("cwd-repo-slash-relative-tests", "tests/x.py", "{repo}/", None, VERDICT_DENY_GLOBS),
    ("dir-slash-absolute", "{repo}/services/x.py", "{repo}", "{repo}/", VERDICT_ALLOW),
    ("dir-slash-relative", "services/x.py", "{repo}", "{repo}/", VERDICT_ALLOW),
]


@pytest.mark.parametrize(
    ("path", "cwd", "project_dir", "verdict"),
    [case[1:] for case in PROJECT_ROOT_CASES],
    ids=[case[0] for case in PROJECT_ROOT_CASES],
)
def test_path_root_project_is_the_project_directory(
    tmp_path: Path, path: str, cwd: str, project_dir: str | None, verdict: str
) -> None:
    """Decision 20 and assumption 63: under PATH_ROOT='project' the root is
    CLAUDE_PROJECT_DIR, a relative path counts as inside only when `cwd` is the
    root, and one trailing `/` is removed from the root and from `cwd`."""
    result = run_root_case(
        tmp_path, PROJECT_ROOT_POLICY, "Write", path, cwd=cwd, project_dir=project_dir
    )
    what = f"a Write of {path!r}, cwd {cwd!r}, CLAUDE_PROJECT_DIR {project_dir!r}"
    assert_verdict(result, verdict, f"{what}, under {PROJECT_ROOT_POLICY!r}")


# PATH_ROOT='cwd', `cwd` a worktree, CLAUDE_PROJECT_DIR the repository root,
# DENY_GLOBS='tests/*'. Each row is an id, the Write's path and the verdict.
CWD_ROOT_CASES: list[tuple[str, str, str]] = [
    ("absolute-in-cwd", "{tmp}/services/x.py", VERDICT_ALLOW),
    ("relative", "services/x.py", VERDICT_ALLOW),
    ("absolute-in-cwd-tests", "{tmp}/tests/x.py", VERDICT_DENY_GLOBS),
    ("main-checkout", "{repo}/services/x.py", VERDICT_ROOT),
    ("other-worktree", "{repo}/.claude/worktrees/other/services/x.py", VERDICT_ROOT),
]


@pytest.mark.parametrize(
    ("path", "verdict"),
    [case[1:] for case in CWD_ROOT_CASES],
    ids=[case[0] for case in CWD_ROOT_CASES],
)
def test_path_root_cwd_is_the_working_directory(tmp_path: Path, path: str, verdict: str) -> None:
    """Decision 20 and assumption 62: under PATH_ROOT='cwd' the root is the
    payload's `cwd`, any relative path counts as inside, and the main checkout
    and the other worktrees under it are outside."""
    result = run_root_case(
        tmp_path, CWD_ROOT_POLICY, "Write", path, cwd="{tmp}", project_dir="{repo}"
    )
    assert_verdict(result, verdict, f"a Write of {path!r} from a worktree, under PATH_ROOT='cwd'")


# An empty root. Each row is an id, the policy, the Write's path, `cwd` and
# CLAUDE_PROJECT_DIR.
EMPTY_ROOT_CASES: list[tuple[str, Mapping[str, str], str, str | None, str | None]] = [
    ("project-absolute", PROJECT_ROOT_POLICY, "{repo}/services/x.py", None, ""),
    ("project-relative", PROJECT_ROOT_POLICY, "services/x.py", None, ""),
    ("cwd-absolute", CWD_ROOT_POLICY, "{repo}/services/x.py", "", None),
    ("cwd-relative", CWD_ROOT_POLICY, "services/x.py", "", None),
]


@pytest.mark.parametrize(
    ("policy", "path", "cwd", "project_dir"),
    [case[1:] for case in EMPTY_ROOT_CASES],
    ids=[case[0] for case in EMPTY_ROOT_CASES],
)
def test_an_empty_root_contains_no_path(
    tmp_path: Path,
    policy: Mapping[str, str],
    path: str,
    cwd: str | None,
    project_dir: str | None,
) -> None:
    """Decision 20 and assumption 61: an empty root, its variable unset or empty,
    contains no path, so every guarded path is outside it. That fails closed."""
    result = run_root_case(tmp_path, policy, "Write", path, cwd=cwd, project_dir=project_dir)
    what = f"a Write of {path!r}, cwd {cwd!r}, CLAUDE_PROJECT_DIR {project_dir!r}"
    assert_root_denied(result, f"{what}, under {dict(policy)!r}")


# Brief T4 item 3, "An empty root", its follow-up added after C6 (2026-09-25):
# PATH_ROOT unset, DENY_GLOBS='tests/*', a Write of the relative `docs/x.md`. Each
# row is an id, `cwd`, CLAUDE_PROJECT_DIR and the verdict.
UNSET_ROOT_POLICY = {"DENY_GLOBS": "tests/*"}
UNSET_EMPTY_ROOT_CASES: list[tuple[str, str, str, str]] = [
    ("both-empty", "", "", VERDICT_ROOT),
    ("cwd-repo-dir-empty", "{repo}", "", VERDICT_ALLOW),
    ("cwd-empty-dir-repo", "", "{repo}", VERDICT_ROOT),
]


@pytest.mark.parametrize(
    ("cwd", "project_dir", "verdict"),
    [case[1:] for case in UNSET_EMPTY_ROOT_CASES],
    ids=[case[0] for case in UNSET_EMPTY_ROOT_CASES],
)
def test_an_unset_path_root_with_both_bases_empty_contains_no_relative_path(
    tmp_path: Path, cwd: str, project_dir: str, verdict: str
) -> None:
    """Decision 20 and assumption 61, as the eighth amendment's "An empty `cwd`"
    leaves them: with PATH_ROOT unset a relative path is inside the root only
    when `cwd` is usable. With `cwd` empty the relative Write is refused with
    the root denial, whatever CLAUDE_PROJECT_DIR is; with `cwd` the repository
    root, it is allowed."""
    path = "docs/x.md"
    result = run_root_case(
        tmp_path, UNSET_ROOT_POLICY, "Write", path, cwd=cwd, project_dir=project_dir
    )
    what = f"a Write of {path!r}, cwd {cwd!r}, CLAUDE_PROJECT_DIR {project_dir!r}"
    assert_verdict(result, verdict, f"{what}, under {UNSET_ROOT_POLICY!r}")


def test_a_path_out_of_plain_form_keeps_decision_18s_denial_outside_the_root(
    tmp_path: Path,
) -> None:
    """Decision 20, "Where it sits", item 3: the root rule runs after decision
    18's rule, so a path out of plain form outside the root keeps the plain-form
    denial."""
    result = run_root_case(tmp_path, DENY_TESTS, "Write", "{tmp}/../x")
    assert_verdict(result, VERDICT_PLAIN_FORM, "a Write of <tmp>/../x")


def test_a_nul_keeps_decision_17s_denial_outside_the_root(tmp_path: Path) -> None:
    """Decision 20, and decision 18's order: decision 17's NUL gate runs first."""
    result = run_root_case(tmp_path, DENY_TESTS, "Write", "{tmp}/x\x00y")
    assert_verdict(result, VERDICT_NUL, "a Write of <tmp>/x, a NUL, then y")


def test_the_project_root_keeps_its_own_denial_under_path_root_project(tmp_path: Path) -> None:
    """Decision 20, "Where it sits", item 3: the project root's own spellings are
    handled by the project-root check, before the root rule, as before."""
    policy = {"PATH_ROOT": "project", "DENY_GLOBS": "packages/*"}
    result = run_root_case(tmp_path, policy, "Grep", "{repo}")
    assert_verdict(result, VERDICT_PROJECT_ROOT, f"a Grep of the project root under {policy!r}")


ROOT_BOUNDARY_POLICIES: list[tuple[str, Mapping[str, str]]] = [
    ("outside-scope-unset", SCOPED_TO_CODER),
    ("outside-scope-project", {**SCOPED_TO_CODER, "PATH_ROOT": "project"}),
    ("outside-scope-cwd", {**SCOPED_TO_CODER, "PATH_ROOT": "cwd"}),
    ("path-root-only", {"PATH_ROOT": "project"}),
]


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in ROOT_BOUNDARY_POLICIES],
    ids=[case_id for case_id, _ in ROOT_BOUNDARY_POLICIES],
)
def test_the_root_rule_polices_only_in_scope_calls_under_a_guarded_policy(
    tmp_path: Path, policy: Mapping[str, str]
) -> None:
    """Decision 20, "Where it sits", item 3: the root rule runs after the routing
    and only under a guarded policy. A call with no `agent_type` under a policy
    scoped to the coder, and any call under a policy that sets only PATH_ROOT,
    is allowed silently with a path outside the root."""
    result = run_root_case(tmp_path, policy, "Write", "{tmp}/x.py")
    assert_verdict(result, VERDICT_ALLOW, f"a Write of <tmp>/x.py under {dict(policy)!r}")


INVALID_PATH_ROOTS = [("worktree", "worktree"), ("Project", "Project"), ("cwd-space", "cwd ")]


@pytest.mark.parametrize(
    "value",
    [value for _, value in INVALID_PATH_ROOTS],
    ids=[case_id for case_id, _ in INVALID_PATH_ROOTS],
)
@pytest.mark.parametrize(
    ("tool_name", "extra"),
    [("Write", {"DENY_GLOBS": "tests/*"}), ("Read", {})],
    ids=["Write-guarded", "Read-path-root-only"],
)
def test_an_invalid_path_root_is_a_configuration_error(
    value: str, tool_name: str, extra: dict[str, str]
) -> None:
    """Decision 20 and assumption 61: a value other than empty, `project` or
    `cwd` refuses every in-scope call, guarded or not."""
    policy = {**extra, "PATH_ROOT": value}
    result = run_rooted(tool_name, policy=policy, file_path=under_repo("services/x.py"))
    what = f"a {tool_name} of services/x.py under {policy!r}"
    reason = assert_denied(result, what)
    assert CONFIG_ERROR_PHRASE in reason, reason
    assert "PATH_ROOT" in reason, reason


@pytest.mark.parametrize(
    "value",
    [value for _, value in INVALID_PATH_ROOTS],
    ids=[case_id for case_id, _ in INVALID_PATH_ROOTS],
)
def test_an_invalid_path_root_does_not_police_a_caller_outside_the_scope(value: str) -> None:
    """Decision 20, "Where it sits", item 1: the value is checked after the
    routing, so a caller the policy does not name passes through untouched. The
    caller the policy names is refused, which keeps this from passing
    vacuously."""
    policy = {**SCOPED_TO_CODER, "PATH_ROOT": value}
    path = under_repo("services/x.py")
    in_scope = run_rooted("Write", policy=policy, file_path=path, agent_type="coder")
    reason = assert_denied(in_scope, f"coder writing services/x.py under {policy!r}")
    assert CONFIG_ERROR_PHRASE in reason, reason
    result = run_rooted("Write", policy=policy, file_path=path)
    assert_allowed_silently(result, f"a Write with no agent_type under {policy!r}")


@pytest.mark.parametrize(
    ("policy", "tool_name", "path"),
    [
        (ALLOW_ANY_TESTS, "Write", "{tmp}/tests/x.py"),
        (DENY_PACKAGES, "Grep", "{parent}"),
    ],
    ids=["Write-file_path", "Grep-path"],
)
def test_the_root_denial_is_decision_20s_text_verbatim(
    tmp_path: Path, policy: Mapping[str, str], tool_name: str, path: str
) -> None:
    """Decision 20 and assumption 71: the root denial word for word, the ADR's
    blockquote line breaks read as single spaces. It quotes no path."""
    filled = fill(path, tmp_path)
    search = tool_name in SEARCH_TOOLS
    result = run_rooted(
        tool_name,
        policy=policy,
        file_path=None if search else filled,
        search_path=filled if search else None,
    )
    reason = assert_root_denied(result, f"a {tool_name} of {path!r}")
    assert reason == ROOT_MESSAGE, (
        "the root denial must be decision 20's text verbatim.\n"
        f"expected: {ROOT_MESSAGE!r}\nreason:   {reason!r}"
    )


# Decision 21, brief T4 item 4: search patterns, under the test-author's
# configured read policy, searching the relative `tests`. These hold before step
# W and after it.


def search_input(tool_name: str, value: Any) -> dict[str, Any]:
    """A search of `tests` whose Glob `pattern`, or Grep `glob`, is `value`."""
    if tool_name == "Glob":
        return {"path": "tests", "pattern": value}
    return {"path": "tests", "pattern": "def ", "glob": value}


def run_test_author_search(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    agent_type: str | None = "test-author",
) -> subprocess.CompletedProcess[str]:
    policy, script = configured_policy("test-author", READ_GREP_GLOB)
    return run_rooted(
        tool_name, policy=policy, tool_input=tool_input, agent_type=agent_type, script=script
    )


REFUSED_PATTERNS: list[tuple[str, Any]] = [
    ("dotdot-leading", "../packages/**/*.py"),
    ("dotdot-inner", "**/../packages/*.py"),
    ("absolute", f"{REPO_ROOT}/packages/**/*.py"),
    ("tilde", "~/x"),
    ("dotdot-in-a-name", "x..y"),
    ("braces", "*.{py,pyi}"),
    ("range", "[a-z]*.py"),
    ("negation", "!x"),
    ("space", "x y"),
    ("backslash", "a\\b"),
    ("newline", "a\nb"),
    ("nul", "a\x00b"),
    ("number", 42),
    ("array", ["*.py"]),
    ("object", {"a": 1}),
    ("true", True),
]


@pytest.mark.parametrize("tool_name", ["Glob", "Grep"])
@pytest.mark.parametrize(
    "value",
    [value for _, value in REFUSED_PATTERNS],
    ids=[case_id for case_id, _ in REFUSED_PATTERNS],
)
def test_a_pattern_outside_the_grammar_is_refused(tool_name: str, value: Any) -> None:
    """Decision 21 and assumption 65: a Glob `pattern` or a Grep `glob` that holds
    anything but letters, digits and `_ - . / * ?`, begins with `/` or contains
    `..`, or is not a string, is refused with the pattern denial. The newline
    case checks that the grammar is anchored at the very start and end."""
    result = run_test_author_search(tool_name, search_input(tool_name, value))
    assert_pattern_denied(result, f"test-author {tool_name} of tests with {value!r}")


ALLOWED_GLOB_PATTERNS = ["**/*.py", "config/test_*.py", "*.json", "x-y_z.?y", ".hidden/*", ""]


@pytest.mark.parametrize(
    "value", ALLOWED_GLOB_PATTERNS, ids=[repr(p) for p in ALLOWED_GLOB_PATTERNS]
)
def test_a_glob_pattern_in_the_grammar_is_allowed(value: str) -> None:
    """Decision 21: these patterns are in the grammar."""
    result = run_test_author_search("Glob", search_input("Glob", value))
    assert_allowed_silently(result, f"test-author Glob of tests with pattern {value!r}")


@pytest.mark.parametrize(
    "tool_input",
    [
        {"path": "tests", "pattern": "def ", "glob": "*.py"},
        {"path": "tests", "pattern": "def "},
        {"path": "tests", "pattern": "def ", "glob": None},
        {"path": "tests", "pattern": "def ", "glob": False},
    ],
    ids=["glob-in-grammar", "glob-absent", "glob-null", "glob-false"],
)
def test_a_grep_glob_in_the_grammar_or_absent_is_allowed(tool_input: dict[str, Any]) -> None:
    """Decision 21: a Grep `glob` in the grammar passes, and so does one that is
    absent, `null` or `false`, as a path field does in decision 17."""
    result = run_test_author_search("Grep", tool_input)
    assert_allowed_silently(result, f"test-author Grep with tool_input {tool_input!r}")


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Grep", {"path": "tests", "pattern": "../packages"}),
        ("Glob", {"path": "tests", "pattern": "*.py", "glob": "../x"}),
        ("Read", {"file_path": under_repo(TOP_LEVEL_TEST_FILE), "pattern": "../x"}),
    ],
    ids=["Grep-regex-pattern", "Glob-glob-field", "Read-pattern-field"],
)
def test_only_the_glob_pattern_and_the_grep_glob_are_read(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    """Decision 21: the value is Glob's `pattern` and Grep's `glob`, and nothing
    else. Grep's `pattern` is a regular expression, not a path; a Glob's `glob`
    and a Read's `pattern` are no field the rule reads."""
    result = run_test_author_search(tool_name, tool_input)
    assert_allowed_silently(result, f"test-author {tool_name} with tool_input {tool_input!r}")


def test_the_pattern_rule_runs_before_the_read_exemptions() -> None:
    """Decision 21, "Where it sits": `tests` is exempt, and `EXEMPT_GLOBS` exits 0
    on a match, so the rule must run before it to refuse this pattern."""
    tool_input = {"path": "tests", "pattern": "../packages/*.py"}
    result = run_test_author_search("Glob", tool_input)
    assert_pattern_denied(result, f"test-author Glob with tool_input {tool_input!r}")


def test_a_nul_in_the_path_keeps_the_nul_denial_beside_a_refused_pattern() -> None:
    """Decision 21: the rule runs directly after decision 17's NUL gate, so a
    path with a NUL gets the NUL denial."""
    tool_input = {"path": with_nul_inside("tests"), "pattern": "../x"}
    result = run_test_author_search("Glob", tool_input)
    reason = assert_nul_denied(result, f"test-author Glob with tool_input {tool_input!r}")
    assert PATTERN_PHRASE not in reason, reason


def test_the_pattern_rule_does_not_police_a_caller_outside_the_policy_scope() -> None:
    """Decision 21: the rule runs after the routing, so a call with no
    `agent_type` passes through untouched."""
    tool_input = {"path": "tests", "pattern": "../packages/*.py"}
    result = run_test_author_search("Glob", tool_input, agent_type=None)
    assert_allowed_silently(result, f"a Glob with no agent_type and tool_input {tool_input!r}")


@pytest.mark.parametrize("tool_name", ["Glob", "Grep"])
def test_the_pattern_rule_does_not_run_under_a_policy_that_constrains_no_paths(
    tool_name: str,
) -> None:
    """Decision 21: the rule runs only under a guarded policy."""
    tool_input = search_input(tool_name, "../packages/*.py")
    result = run_rooted(tool_name, policy={}, tool_input=tool_input)
    assert_allowed_silently(result, f"a {tool_name} with tool_input {tool_input!r}, no policy")


@pytest.mark.parametrize("tool_name", ["Glob", "Grep"])
def test_the_pattern_denial_is_decision_21s_text_verbatim(tool_name: str) -> None:
    """Decision 21 and assumption 71: the pattern denial word for word, the ADR's
    blockquote line breaks read as single spaces. It quotes no pattern."""
    result = run_test_author_search(tool_name, search_input(tool_name, "../packages/*.py"))
    reason = assert_pattern_denied(result, f"test-author {tool_name} of ../packages/*.py")
    assert reason == PATTERN_MESSAGE, (
        "the pattern denial must be decision 21's text verbatim.\n"
        f"expected: {PATTERN_MESSAGE!r}\nreason:   {reason!r}"
    )


# Decisions 20 and 22, brief T4 item 5: the configured policies after step W. Each
# call is sent with the agent's own `agent_type`, and `cwd` the repository root
# unless stated. These fail until step W, except the cases marked "holds after
# C6", which fail only until brief C6 lands.

TEST_AUTHOR_WRITE_AFTER_W: list[tuple[str, bool]] = [
    (".claude/skills/tests/SKILL.md", False),
    (".claude/hooks/tests/x.py", False),
    ("tests/CLAUDE.md", False),
    ("services/trie/src/hammertime/trie/tests/CLAUDE.md", False),
    ("tests/config/CLAUDE.local.md", False),
    ("tests/.claude/skills/x/SKILL.md", False),
    ("packages/hammertime-testkit/CLAUDE.md", False),
    ("tests/config/__pycache__/test_x.cpython-312-pytest-9.1.1.pyc", False),
    ("packages/hammertime-core/src/hammertime/core/tests/__pycache__/x.cpython-312.pyc", False),
    ("tests/.venv/x.py", False),
    ("tests/x/.git/config", False),
    ("docs/tests/x.md", False),
    (".venv/lib/python3.12/site-packages/tests/x.py", False),
    (".git/tests/x", False),
    (TOP_LEVEL_TEST_FILE, True),
    (NESTED_TEST_FILE, True),
    ("packages/hammertime-core/src/hammertime/core/tests/test_x.py", True),
    ("tools/provision/src/hammertime/tools/provision/tests/test_x.py", True),
    ("services/trie/src/hammertime/trie/tests/conftest.py", True),
    (TESTKIT_FILE, True),
]


@pytest.mark.parametrize(
    ("path", "allowed"),
    TEST_AUTHOR_WRITE_AFTER_W,
    ids=[f"{'allow' if ok else 'deny'}-{path}" for path, ok in TEST_AUTHOR_WRITE_AFTER_W],
)
def test_configured_test_author_write_policy_after_step_w(path: str, allowed: bool) -> None:
    """Decision 22: the test-author's Edit/Write allowlist is anchored at each
    entry's top-level directory, and its deny list, checked first, refuses agent
    configuration, git's internals and ignored executed state even inside a
    test directory."""
    result = run_configured_rooted("test-author", EDIT_WRITE, "Write", under_repo(path))
    if allowed:
        assert_allowed(result, f"test-author writing {path}")
    else:
        assert_denied(result, f"test-author writing {path}")


def test_configured_test_author_write_policy_refuses_a_tests_directory_elsewhere(
    tmp_path: Path,
) -> None:
    """Decision 20, gap G1's outside path (holds after C6): a `tests` directory
    outside the project is outside the root, whatever the allowlist says."""
    path = f"{tmp_path}/tests/x.py"
    result = run_configured_rooted("test-author", EDIT_WRITE, "Write", path)
    assert_denied(result, "test-author writing <tmp>/tests/x.py")


TEST_AUTHOR_READ_AFTER_W: list[tuple[str, str, bool]] = [
    ("Read", under_repo(".claude/settings.json"), False),
    ("Read", under_repo(".claude/worktrees/agent-x/" + IMPLEMENTATION_FILE), False),
    ("Read", under_repo(".claude/worktrees/agent-x/" + NESTED_TEST_FILE), False),
    ("Read", under_repo(".mypy_cache/3.12/cache.0.db"), False),
    ("Read", under_repo("build/lib/hammertime/core/window.py"), False),
    ("Read", under_repo("dist/x.whl"), False),
    ("Read", under_repo("htmlcov/index.html"), False),
    ("Grep", ".claude", False),
    ("Grep", ".claude/worktrees", False),
    ("Grep", ".mypy_cache", False),
    # These three hold after C6 (decision 20).
    ("Grep", str(REPO_ROOT.parent), False),
    ("Grep", "/", False),
    ("Read", "/proc/self/cwd/" + IMPLEMENTATION_FILE, False),
    ("Read", under_repo(SPEC_FILE), True),
    ("Read", under_repo(SCHEMA_FILE), True),
    ("Read", under_repo(TOP_LEVEL_TEST_FILE), True),
    ("Read", under_repo(NESTED_TEST_FILE), True),
    ("Read", under_repo(TESTKIT_FILE), True),
    ("Read", under_repo("README.md"), True),
    ("Read", under_repo("pyproject.toml"), True),
    ("Read", under_repo("deploy/docker-compose.yml"), True),
    ("Read", under_repo(".venv/lib/python3.12/site-packages/_pytest/python.py"), True),
    ("Grep", "docs", True),
    ("Grep", "tests", True),
    ("Grep", "packages/hammertime-core/src/hammertime/core/tests", True),
]


@pytest.mark.parametrize(
    ("tool_name", "path", "allowed"),
    TEST_AUTHOR_READ_AFTER_W,
    ids=[
        f"{tool}-{'allow' if ok else 'deny'}-{path_case_id(path)}"
        for tool, path, ok in TEST_AUTHOR_READ_AFTER_W
    ],
)
def test_configured_test_author_read_policy_after_step_w(
    tool_name: str, path: str, allowed: bool
) -> None:
    """Decisions 20 and 22: the read deny list gains `.claude/`, `.mypy_cache/`
    and the build and coverage output, the exemptions are anchored, and the root
    rule refuses the directory above the project, `/` and a path through
    `/proc`."""
    result = run_configured_rooted("test-author", READ_GREP_GLOB, tool_name, path)
    if allowed:
        assert_allowed(result, f"test-author {tool_name} of {path}")
    else:
        assert_denied(result, f"test-author {tool_name} of {path}")


ARCHITECT_WRITE_AFTER_W: list[tuple[str, bool]] = [
    ("docs/CLAUDE.md", False),
    ("docs/spec/CLAUDE.local.md", False),
    ("docs/.claude/x.md", False),
    ("schemas/.mcp.json", False),
    (SPEC_FILE, True),
    (SCHEMA_FILE, True),
    ("docs/adr/0019-x.md", True),
    ("README.md", True),
]


@pytest.mark.parametrize(
    ("path", "allowed"),
    ARCHITECT_WRITE_AFTER_W,
    ids=[f"{'allow' if ok else 'deny'}-{path}" for path, ok in ARCHITECT_WRITE_AFTER_W],
)
def test_configured_architect_write_policy_after_step_w(path: str, allowed: bool) -> None:
    """Decision 22: the architect's Edit/Write deny list is the
    agent-configuration list, so `docs/*` no longer admits a nested `CLAUDE.md`
    or `.claude/`."""
    result = run_configured_rooted("architect", EDIT_WRITE, "Write", under_repo(path))
    if allowed:
        assert_allowed(result, f"architect writing {path}")
    else:
        assert_denied(result, f"architect writing {path}")


CODER_WORKTREE_AFTER_W: list[tuple[str, str]] = [
    ("{repo}/" + TRIE_QUERY_FILE, VERDICT_ROOT),
    ("{tmp}/services/.claude/skills/x/SKILL.md", VERDICT_DENY_GLOBS),
    ("{tmp}/" + TRIE_QUERY_FILE, VERDICT_ALLOW),
]


@pytest.mark.parametrize(
    ("template", "verdict"),
    CODER_WORKTREE_AFTER_W,
    ids=[f"{verdict}-{template}" for template, verdict in CODER_WORKTREE_AFTER_W],
)
def test_configured_coder_write_policy_after_step_w(
    tmp_path: Path, template: str, verdict: str
) -> None:
    """Decisions 20 and 22, and Question 2: with `cwd` the coder's worktree and
    CLAUDE_PROJECT_DIR the main checkout, `PATH_ROOT='cwd'` refuses the main
    checkout's copy of a file, the agent-configuration list refuses a nested
    `.claude/`, and the worktree's own copy is allowed."""
    path = fill(template, tmp_path)
    result = run_configured_rooted(
        "coder",
        EDIT_WRITE,
        "Write",
        path,
        cwd=str(tmp_path),
        project_dir=str(REPO_ROOT),
    )
    assert_verdict(result, verdict, f"coder writing {template} from a worktree")


def test_configured_test_author_write_policy_ignores_the_top_level_session() -> None:
    """Decision 20, "Where it sits", and decision 19, part 3: after the shape
    check, a call with no `agent_type` passes the routing untouched."""
    policy, script = configured_policy("test-author", EDIT_WRITE)
    path = under_repo(".claude/skills/tests/SKILL.md")
    result = run_rooted("Write", policy=policy, file_path=path, script=script)
    assert_allowed_silently(result, "a top-level session Write of .claude/skills/tests/SKILL.md")


# --- the sixth amendment: the test-author's read list and a usable root -------
#
# ADR-0018's sixth amendment (2026-09-25), brief T5. Every root and every path out
# of plain form is built by string formatting, never through `pathlib`, which
# collapses `.` components and repeated slashes.

# Decisions 20 and 22, brief T5 item 2: the sixth amendment's names under the
# configured read policy, after step W, with `cwd` and CLAUDE_PROJECT_DIR the
# repository root. Each is refused with the DENY_GLOBS denial, not the root denial.
SIXTH_AMENDMENT_READ_REFUSED: list[tuple[str, str]] = [
    ("Read", under_repo(".hypothesis/constants/fb05b1883236f3ed")),
    ("Read", under_repo(".hypothesis/unicode_data/15.0.0/charmap.json.gz")),
    ("Read", under_repo(".pytest_cache/v/cache/nodeids")),
    ("Read", under_repo(".pytest_cache/v/cache/lastfailed")),
    ("Read", under_repo(".ruff_cache/0.16.7/3614437143458050706")),
    ("Read", under_repo(".git/COMMIT_EDITMSG")),
    ("Read", under_repo(".git/logs/HEAD")),
    ("Read", under_repo(".git/index")),
    ("Read", under_repo(".coverage")),
    ("Read", under_repo(".coverage.host.1.2")),
    ("Read", under_repo(".uv/x")),
    ("Read", under_repo("snapshots/x")),
    ("Grep", ".hypothesis"),
    ("Grep", ".pytest_cache"),
    ("Grep", ".ruff_cache"),
    ("Grep", ".git"),
    ("Grep", ".uv"),
    ("Grep", "snapshots"),
]

# The controls: `.git`, `.git/*`, `.coverage` and `.coverage.*` match no more than
# their names. These hold today and after step W.
SIXTH_AMENDMENT_READ_CONTROLS = [
    under_repo(".gitignore"),
    under_repo(".github/workflows/ci.yml"),
    under_repo(".coveragerc"),
]


@pytest.mark.parametrize(
    ("tool_name", "path"),
    SIXTH_AMENDMENT_READ_REFUSED,
    ids=[f"{tool}-{repo_relative_id(path)}" for tool, path in SIXTH_AMENDMENT_READ_REFUSED],
)
def test_configured_test_author_read_policy_refuses_the_sixth_amendments_names(
    tool_name: str, path: str
) -> None:
    """Decisions 14, 20 and 22, and assumption 76: after step W the test-author's
    read DENY_GLOBS refuse the caches of Hypothesis, pytest, ruff and uv, `.git`,
    coverage's data files and `snapshots`, each by a Read inside it and, for the
    directories, by a Grep of it. The path is inside the root, so the refusal is
    the DENY_GLOBS denial, not the root denial."""
    result = run_configured_rooted(
        "test-author",
        READ_GREP_GLOB,
        tool_name,
        path,
        cwd=str(REPO_ROOT),
        project_dir=str(REPO_ROOT),
    )
    assert_verdict(result, VERDICT_DENY_GLOBS, f"test-author {tool_name} of {path}")


@pytest.mark.parametrize("path", SIXTH_AMENDMENT_READ_CONTROLS, ids=repo_relative_id)
def test_configured_test_author_read_policy_allows_names_beside_the_sixth_amendments(
    path: str,
) -> None:
    """Decision 22, brief T5 item 2's controls: `.gitignore`, `.github/` and
    `.coveragerc` are not `.git`, `.git/*`, `.coverage` or `.coverage.*`."""
    result = run_configured_rooted(
        "test-author",
        READ_GREP_GLOB,
        "Read",
        path,
        cwd=str(REPO_ROOT),
        project_dir=str(REPO_ROOT),
    )
    assert_allowed(result, f"test-author Read of {path}")


# Decision 20, brief T5 items 3-7: a root the guard cannot use. No call sets an
# `agent_type` unless stated. A template's `{tmp}`, `{repo}`, `{parent}` and
# `{dotdot}` are filled in by `fill_root`; `cwd` and CLAUDE_PROJECT_DIR are always
# given, and an empty string sends an empty value.

# The repository root out of plain form: the directory above it, then back down.
REPO_ROOT_VIA_DOTDOT = f"{REPO_ROOT}/../{REPO_ROOT.name}"


def fill_root(template: str, tmp_path: Path) -> str:
    """`fill`'s sibling: `{tmp}`, `{repo}` and `{parent}` as there, and `{dotdot}`,
    REPO_ROOT_VIA_DOTDOT."""
    return template.format(
        tmp=tmp_path, repo=REPO_ROOT, parent=REPO_ROOT.parent, dotdot=REPO_ROOT_VIA_DOTDOT
    )


def run_unusable_root_case(
    tmp_path: Path,
    policy: Mapping[str, str],
    tool_name: str,
    path: str,
    *,
    cwd: str,
    project_dir: str,
    agent_type: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """`run_root_case`'s sibling: `path`, `cwd` and CLAUDE_PROJECT_DIR filled in by
    `fill_root`, then sent as a search's `path` or another tool's `file_path`."""
    filled = fill_root(path, tmp_path)
    search = tool_name in SEARCH_TOOLS
    return run_rooted(
        tool_name,
        policy=policy,
        file_path=None if search else filled,
        search_path=filled if search else None,
        agent_type=agent_type,
        cwd=fill_root(cwd, tmp_path),
        project_dir=fill_root(project_dir, tmp_path),
    )


# Brief T5 item 3: PATH_ROOT='project', DENY_GLOBS='tests/*'. Each row is an id, the
# tool, its path, `cwd` and CLAUDE_PROJECT_DIR. Every one is refused with the root
# denial.
PROJECT_UNUSABLE_ROOT_CASES: list[tuple[str, str, str, str, str]] = [
    ("both-slash-absolute-repo", "Write", "{repo}/services/x.py", "/", "/"),
    ("both-slash-absolute-tmp", "Write", "{tmp}/x.py", "/", "/"),
    ("both-slash-absolute-tests", "Write", "/tests/x.py", "/", "/"),
    ("both-slash-relative", "Write", "services/x.py", "/", "/"),
    ("both-slash-grep-slash", "Grep", "/", "/", "/"),
    ("dir-slash-cwd-empty-relative", "Write", "services/x.py", "", "/"),
    ("dir-slash-cwd-repo-absolute-repo", "Write", "{repo}/services/x.py", "{repo}", "/"),
    ("both-double-slash-relative", "Write", "services/x.py", "//", "//"),
    ("both-dotdot-relative", "Write", "services/x.py", "{dotdot}", "{dotdot}"),
]


@pytest.mark.parametrize(
    ("tool_name", "path", "cwd", "project_dir"),
    [case[1:] for case in PROJECT_UNUSABLE_ROOT_CASES],
    ids=[case[0] for case in PROJECT_UNUSABLE_ROOT_CASES],
)
def test_an_unusable_project_directory_contains_no_path(
    tmp_path: Path, tool_name: str, path: str, cwd: str, project_dir: str
) -> None:
    """Decision 20, "A root the guard cannot use", and assumption 77: under
    PATH_ROOT='project' a CLAUDE_PROJECT_DIR of `/`, `//` or the repository root
    through `..` is not usable, so no path is inside it, absolute or relative,
    whatever `cwd` is, and each call is refused with the root denial."""
    result = run_unusable_root_case(
        tmp_path, PROJECT_ROOT_POLICY, tool_name, path, cwd=cwd, project_dir=project_dir
    )
    what = f"a {tool_name} of {path!r}, cwd {cwd!r}, CLAUDE_PROJECT_DIR {project_dir!r}"
    assert_verdict(result, VERDICT_ROOT, f"{what}, under {PROJECT_ROOT_POLICY!r}")


# Brief T5 item 4: PATH_ROOT='cwd', DENY_GLOBS='tests/*', CLAUDE_PROJECT_DIR the
# repository root. Each row is an id, the Write's path, `cwd` and the verdict.
CWD_UNUSABLE_ROOT_CASES: list[tuple[str, str, str, str]] = [
    ("cwd-slash-absolute-repo", "{repo}/services/x.py", "/", VERDICT_ROOT),
    ("cwd-slash-absolute-tmp", "{tmp}/x.py", "/", VERDICT_ROOT),
    ("cwd-slash-relative", "services/x.py", "/", VERDICT_ROOT),
    ("cwd-double-slash-relative", "services/x.py", "//", VERDICT_ROOT),
    ("cwd-tmp-dot-relative", "services/x.py", "{tmp}/.", VERDICT_ROOT),
    ("cwd-relative-relative", "services/x.py", "tests", VERDICT_ROOT),
    ("control-cwd-tmp-slash-absolute", "{tmp}/services/x.py", "{tmp}/", VERDICT_ALLOW),
    ("control-cwd-tmp-slash-relative", "services/x.py", "{tmp}/", VERDICT_ALLOW),
]


@pytest.mark.parametrize(
    ("path", "cwd", "verdict"),
    [case[1:] for case in CWD_UNUSABLE_ROOT_CASES],
    ids=[case[0] for case in CWD_UNUSABLE_ROOT_CASES],
)
def test_an_unusable_working_directory_contains_no_path(
    tmp_path: Path, path: str, cwd: str, verdict: str
) -> None:
    """Decision 20, "A root the guard cannot use", and assumption 77: under
    PATH_ROOT='cwd' a `cwd` of `/`, `//`, one with a `.` component or a relative
    one is not usable, so no path is inside it and each Write is refused with the
    root denial. The controls: a `cwd` with one trailing `/` is usable."""
    result = run_unusable_root_case(
        tmp_path, CWD_ROOT_POLICY, "Write", path, cwd=cwd, project_dir="{repo}"
    )
    what = f"a Write of {path!r}, cwd {cwd!r}, CLAUDE_PROJECT_DIR the repository root"
    assert_verdict(result, verdict, f"{what}, under {CWD_ROOT_POLICY!r}")


# Brief T5 item 5: PATH_ROOT unset, DENY_GLOBS='tests/*'. Each row is an id, the
# Write's path, `cwd`, CLAUDE_PROJECT_DIR and the verdict.
UNSET_UNUSABLE_ROOT_CASES: list[tuple[str, str, str, str, str]] = [
    ("cwd-slash-absolute-tmp", "{tmp}/x.py", "/", "{repo}", VERDICT_ROOT),
    ("cwd-slash-relative", "services/x.py", "/", "{repo}", VERDICT_ROOT),
    ("cwd-slash-absolute-repo-tests", "{repo}/tests/x.py", "/", "{repo}", VERDICT_DENY_GLOBS),
    ("control-cwd-slash-absolute-repo", "{repo}/services/x.py", "/", "{repo}", VERDICT_ALLOW),
    ("dir-slash-absolute-tmp", "{tmp}/x.py", "{repo}", "/", VERDICT_ROOT),
    ("control-dir-slash-absolute-repo", "{repo}/services/x.py", "{repo}", "/", VERDICT_ALLOW),
    ("control-dir-slash-relative", "services/x.py", "{repo}", "/", VERDICT_ALLOW),
    (
        "control-dir-slash-absolute-repo-tests",
        "{repo}/tests/x.py",
        "{repo}",
        "/",
        VERDICT_DENY_GLOBS,
    ),
    ("cwd-empty-dir-slash-relative", "services/x.py", "", "/", VERDICT_ROOT),
    ("cwd-empty-dir-slash-absolute-repo", "{repo}/services/x.py", "", "/", VERDICT_ROOT),
    ("both-slash-absolute-repo", "{repo}/services/x.py", "/", "/", VERDICT_ROOT),
    ("cwd-double-slash-relative", "services/x.py", "//", "{repo}", VERDICT_ROOT),
    (
        "control-cwd-double-slash-absolute-repo",
        "{repo}/services/x.py",
        "//",
        "{repo}",
        VERDICT_ALLOW,
    ),
]


@pytest.mark.parametrize(
    ("path", "cwd", "project_dir", "verdict"),
    [case[1:] for case in UNSET_UNUSABLE_ROOT_CASES],
    ids=[case[0] for case in UNSET_UNUSABLE_ROOT_CASES],
)
def test_an_unset_path_root_uses_only_usable_bases(
    tmp_path: Path, path: str, cwd: str, project_dir: str, verdict: str
) -> None:
    """Decision 20, the table's row for PATH_ROOT unset and "A root the guard
    cannot use", and assumption 77: an absolute path is compared only with a
    usable base, so a usable base keeps judging the paths inside it through its
    glob lists; a relative path is inside only when `cwd` is usable, or `cwd` is
    empty and CLAUDE_PROJECT_DIR is usable, so a `cwd` of `/` or `//` puts every
    relative path outside; and with no usable base the root is empty."""
    result = run_unusable_root_case(
        tmp_path, UNSET_ROOT_POLICY, "Write", path, cwd=cwd, project_dir=project_dir
    )
    what = f"a Write of {path!r}, cwd {cwd!r}, CLAUDE_PROJECT_DIR {project_dir!r}"
    assert_verdict(result, verdict, f"{what}, under {UNSET_ROOT_POLICY!r}")


# Brief T5 item 6: the boundaries, with `cwd` `/` and CLAUDE_PROJECT_DIR the
# repository root. Each row is an id and the policy.
UNUSABLE_ROOT_SILENT_POLICIES: list[tuple[str, Mapping[str, str]]] = [
    ("path-root-only", {"PATH_ROOT": "cwd"}),
    ("outside-scope-cwd", {**SCOPED_TO_CODER, "PATH_ROOT": "cwd"}),
]


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in UNUSABLE_ROOT_SILENT_POLICIES],
    ids=[case_id for case_id, _ in UNUSABLE_ROOT_SILENT_POLICIES],
)
def test_an_unusable_root_polices_only_in_scope_calls_under_a_guarded_policy(
    tmp_path: Path, policy: Mapping[str, str]
) -> None:
    """Decision 20, "Where it sits", and "A root the guard cannot use", reason 3:
    the empty root adds no place in the order of checks, so a policy that
    constrains no paths, and a policy scoped to the coder with a call that sets
    no `agent_type`, allow a Write outside an unusable root silently."""
    result = run_unusable_root_case(
        tmp_path, policy, "Write", "{tmp}/x.py", cwd="/", project_dir="{repo}"
    )
    assert_verdict(result, VERDICT_ALLOW, f"a Write of <tmp>/x.py, cwd '/', under {dict(policy)!r}")


def test_an_unusable_root_refuses_the_caller_the_policy_names(tmp_path: Path) -> None:
    """Decision 20, "A root the guard cannot use": the coder, whom the scoped
    policy names, is refused the same Write with the root denial, which keeps
    the silent passes above from being vacuous."""
    policy = {**SCOPED_TO_CODER, "PATH_ROOT": "cwd"}
    result = run_unusable_root_case(
        tmp_path, policy, "Write", "{tmp}/x.py", cwd="/", project_dir="{repo}", agent_type="coder"
    )
    assert_verdict(result, VERDICT_ROOT, f"coder writing <tmp>/x.py, cwd '/', under {policy!r}")


# Brief T5 item 7: the message. Each row is an id, the policy, the tool, its path,
# `cwd` and CLAUDE_PROJECT_DIR.
UNUSABLE_ROOT_MESSAGE_CASES: list[tuple[str, Mapping[str, str], str, str, str, str]] = [
    ("cwd-Write", CWD_ROOT_POLICY, "Write", "{tmp}/x.py", "/", "{repo}"),
    ("project-Grep", PROJECT_ROOT_POLICY, "Grep", "/", "/", "/"),
]


@pytest.mark.parametrize(
    ("policy", "tool_name", "path", "cwd", "project_dir"),
    [case[1:] for case in UNUSABLE_ROOT_MESSAGE_CASES],
    ids=[case[0] for case in UNUSABLE_ROOT_MESSAGE_CASES],
)
def test_an_unusable_root_gets_decision_20s_root_denial_verbatim(
    tmp_path: Path,
    policy: Mapping[str, str],
    tool_name: str,
    path: str,
    cwd: str,
    project_dir: str,
) -> None:
    """Decision 20, "A root the guard cannot use", reason 3, and assumption 77:
    the sixth amendment adds no denial, so a call outside an unusable root gets
    the root denial word for word."""
    result = run_unusable_root_case(
        tmp_path, policy, tool_name, path, cwd=cwd, project_dir=project_dir
    )
    what = f"a {tool_name} of {path!r}, cwd {cwd!r}, CLAUDE_PROJECT_DIR {project_dir!r}"
    reason = assert_root_denied(result, f"{what}, under {dict(policy)!r}")
    assert reason == ROOT_MESSAGE, (
        "the root denial must be decision 20's text verbatim.\n"
        f"expected: {ROOT_MESSAGE!r}\nreason:   {reason!r}"
    )


# --- the seventh amendment: SA1f's findings -----------------------------------
#
# ADR-0018's seventh amendment (2026-09-25), brief T6. Every path that carries a
# newline or a NUL is built by string concatenation, and `json.dumps` writes each
# as an escape. A payload with a raw byte in it is built with `json.dumps`, and
# then has the escape replaced by the byte itself (`raw_bytes`); it goes through
# `run_guard_stdin`, which sends it unchanged.

# Decision 24's denial, verbatim, with the ADR's blockquote line breaks joined by
# single spaces. It is ASCII only.
FIELD_MESSAGE = (
    "Hammertime path guard: this guard reads the path of a Read, Edit or Write "
    "from file_path and the path of a Grep or Glob from path, and this call "
    "either comes from another tool or also names a path in the other field, "
    "so the guard cannot tell which path the tool would act on. The tool call "
    "is refused."
)
FIELD_PHRASE = "cannot tell which path the tool would act on"

# Decision 17's trailing-newline denial, the same way.
TRAILING_NEWLINE_MESSAGE = (
    "Hammertime path guard: the path ends with a newline, or could not be "
    "checked for one, and trailing newlines are dropped when the path is "
    "read, so the guard cannot vet the path the tool would actually use. "
    "Give the path without the newline. The tool call is refused."
)
TRAILING_NEWLINE_PHRASE = "the path ends with a newline"

# Decision 23's denial for its rules 1 and 2, the same way.
SEARCH_VALUE_MESSAGE = (
    "Hammertime path guard: no value in a Grep or a Glob may begin with '-', "
    "and no part of a Glob pattern or a Grep glob between slashes may begin "
    "with '.' followed by '*' or '?', because the tool could read such a value "
    "as an option, or match such a part against the '..' entry and search "
    "above the path being searched. Give the value in another form; a Grep "
    "pattern that has to match a leading '-' can begin with '[-]' instead. The "
    "tool call is refused."
)
SEARCH_VALUE_PHRASE = "no value in a Grep or a Glob may begin with '-'"

# Decision 23's denial for its rule 3, the same way.
SEARCH_PATH_MESSAGE = (
    "Hammertime path guard: the path of a Grep or a Glob may contain only "
    "letters, digits and the characters _ - . /, because the tool could read "
    "any other character as part of a pattern or an option and search "
    "somewhere other than the path this guard vetted. Give the path in that "
    "form. The tool call is refused."
)
SEARCH_PATH_PHRASE = "the path of a Grep or a Glob may contain only"


def assert_denied_with(
    result: subprocess.CompletedProcess[str], message: str, phrase: str, what: str
) -> str:
    """A denial that begins with the prefix, contains `phrase`, and is `message`
    word for word (brief T6)."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert phrase in reason, (
        f"expected the denial containing {phrase!r} for {what}, not another refusal.\n"
        f"reason: {reason!r}"
    )
    assert reason == message, (
        f"the denial for {what} must be the ADR's text verbatim.\n"
        f"expected: {message!r}\nreason:   {reason!r}"
    )
    return reason


def assert_field_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 24's denial, verbatim."""
    return assert_denied_with(result, FIELD_MESSAGE, FIELD_PHRASE, what)


def assert_trailing_newline_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 17's trailing-newline denial, verbatim."""
    return assert_denied_with(result, TRAILING_NEWLINE_MESSAGE, TRAILING_NEWLINE_PHRASE, what)


def assert_search_value_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 23's denial for its rules 1 and 2, verbatim."""
    return assert_denied_with(result, SEARCH_VALUE_MESSAGE, SEARCH_VALUE_PHRASE, what)


def assert_search_path_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 23's denial for its rule 3, verbatim."""
    return assert_denied_with(result, SEARCH_PATH_MESSAGE, SEARCH_PATH_PHRASE, what)


# Decision 24, brief T6 item 1: each tool's own field. The Grep's `path` is one
# the test-author's read list refuses, and its `file_path` one its read
# exemptions admit.

GREP_WITH_BOTH_FIELDS: dict[str, Any] = {
    "pattern": "PROBE",
    "path": under_repo("packages"),
    "file_path": under_repo("tests/x"),
}
NOTEBOOK_EDIT_INPUT: dict[str, Any] = {"notebook_path": under_repo("services/x.ipynb")}

OTHER_FIELD_READ_CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("Grep-path-and-file_path", "Grep", GREP_WITH_BOTH_FIELDS),
    (
        "Glob-path-and-file_path",
        "Glob",
        {"pattern": "*.py", "path": under_repo("packages"), "file_path": under_repo("tests")},
    ),
    (
        "Read-file_path-and-path",
        "Read",
        {"file_path": under_repo(TOP_LEVEL_TEST_FILE), "path": under_repo("packages")},
    ),
]


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [case[1:] for case in OTHER_FIELD_READ_CASES],
    ids=[case[0] for case in OTHER_FIELD_READ_CASES],
)
def test_a_test_author_call_that_names_the_other_field_is_refused(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    """Decision 24, rules 2 and 3: a Grep or a Glob is judged on its `path` and a
    Read on its `file_path`, and a call that also names the other kind's field
    is refused, whatever that field holds. Before, the Grep and the Glob were
    judged on the `file_path`, which the read exemptions admit."""
    result = run_test_author_search(tool_name, tool_input)
    assert_field_denied(result, f"test-author {tool_name} with the fields {sorted(tool_input)}")


OTHER_FIELD_DENY_TESTS_CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("Write-path", "Write", {"path": under_repo(SERVICE_FILE)}),
    ("Write-null-and-path", "Write", {"file_path": None, "path": under_repo(SERVICE_FILE)}),
    ("Edit-empty-path", "Edit", {"file_path": under_repo(SERVICE_FILE), "path": ""}),
    ("NotebookEdit", "NotebookEdit", NOTEBOOK_EDIT_INPUT),
    ("MultiEdit", "MultiEdit", {"file_path": under_repo(SERVICE_FILE)}),
]


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [case[1:] for case in OTHER_FIELD_DENY_TESTS_CASES],
    ids=[case[0] for case in OTHER_FIELD_DENY_TESTS_CASES],
)
def test_a_call_from_another_tool_or_naming_the_other_field_is_refused(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    """Decision 24, rules 1 and 2: under a guarded policy, a Write or an Edit whose
    `path` is anything but absent, `null` or `false`, the empty string included,
    and a call from a tool whose path field the script does not know, are
    refused."""
    result = run_rooted(tool_name, policy=DENY_TESTS, tool_input=tool_input)
    assert_field_denied(result, f"a {tool_name} with the fields {sorted(tool_input)}")


@pytest.mark.parametrize("file_path", [ABSENT, None, False], ids=["absent", "null", "false"])
def test_a_grep_whose_file_path_is_absent_null_or_false_is_judged_on_its_path(
    file_path: Any,
) -> None:
    """Decision 24, rule 2's control: a Grep's `file_path` that is absent, `null`
    or `false` names no other path, so the Grep is judged on its `path`, which
    the read list refuses with the DENY_GLOBS denial."""
    tool_input: dict[str, Any] = {"pattern": "PROBE", "path": under_repo("packages")}
    if file_path is not ABSENT:
        tool_input["file_path"] = file_path
    result = run_test_author_search("Grep", tool_input)
    shown = "absent" if file_path is ABSENT else repr(file_path)
    reason = assert_deny_globs_denied(result, f"test-author Grep of packages, file_path {shown}")
    assert FIELD_PHRASE not in reason, reason


def test_a_write_whose_path_field_is_null_is_judged_on_its_file_path() -> None:
    """Decision 24, rule 2's control: a Write's `path` of `null` names no other
    path, so a Write of a path the policy allows is allowed."""
    tool_input = {"file_path": under_repo(SERVICE_FILE), "path": None}
    result = run_rooted("Write", policy=DENY_TESTS, tool_input=tool_input)
    assert_allowed_silently(result, f"a Write with tool_input {tool_input!r}")


def test_the_field_check_runs_before_the_nul_gate() -> None:
    """Decision 24, "Where it sits": before decision 17's NUL gate, so a Write
    with a NUL in its `file_path` and a `path` beside it gets decision 24's
    denial, not the NUL denial."""
    tool_input = {"file_path": under_repo(SERVICE_FILE) + "\x00", "path": "x"}
    result = run_rooted("Write", policy=DENY_TESTS, tool_input=tool_input)
    reason = assert_field_denied(result, "a Write with a NUL in its file_path and a path")
    assert NUL_PHRASE not in reason, reason


FIELD_BOUNDARY_CALLS: list[tuple[str, str, dict[str, Any]]] = [
    ("NotebookEdit", "NotebookEdit", NOTEBOOK_EDIT_INPUT),
    ("Grep-both-fields", "Grep", GREP_WITH_BOTH_FIELDS),
]

FIELD_SILENT_POLICIES: list[tuple[str, Mapping[str, str]]] = [
    ("no-policy", {}),
    ("outside-scope", SCOPED_TO_CODER),
]


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in FIELD_SILENT_POLICIES],
    ids=[case_id for case_id, _ in FIELD_SILENT_POLICIES],
)
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [case[1:] for case in FIELD_BOUNDARY_CALLS],
    ids=[case[0] for case in FIELD_BOUNDARY_CALLS],
)
def test_the_field_check_polices_only_in_scope_calls_under_a_guarded_policy(
    tool_name: str, tool_input: dict[str, Any], policy: Mapping[str, str]
) -> None:
    """Decision 24, "Where it sits": after the routing and only under a guarded
    policy, so a policy that constrains no paths, and a policy scoped to the
    coder with a call that sets no `agent_type`, allow both calls silently."""
    result = run_rooted(tool_name, policy=policy, tool_input=tool_input)
    assert_allowed_silently(result, f"a {tool_name} with no agent_type under {dict(policy)!r}")


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [case[1:] for case in FIELD_BOUNDARY_CALLS],
    ids=[case[0] for case in FIELD_BOUNDARY_CALLS],
)
def test_the_field_check_refuses_the_caller_a_scoped_policy_names(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    """Decision 24: the coder, whom the scoped policy names, is refused both
    calls, which keeps the silent passes above from being vacuous."""
    result = run_rooted(
        tool_name, policy=SCOPED_TO_CODER, tool_input=tool_input, agent_type="coder"
    )
    assert_field_denied(result, f"coder {tool_name} under {SCOPED_TO_CODER!r}")


# Decision 17's trailing newline, brief T6 item 2.

TRAILING_NEWLINE_ARCHITECT_PATHS: list[tuple[str, str]] = [
    ("root", f"{REPO_ROOT}\n"),
    ("root-slash", f"{REPO_ROOT}/\n"),
    ("newline-only", "\n"),
    ("dot", ".\n"),
    ("docs-file", under_repo("docs/x.md") + "\n"),
    ("docs-file-two-newlines", under_repo("docs/x.md") + "\n\n"),
]


@pytest.mark.parametrize(
    "path",
    [path for _, path in TRAILING_NEWLINE_ARCHITECT_PATHS],
    ids=[case_id for case_id, _ in TRAILING_NEWLINE_ARCHITECT_PATHS],
)
def test_an_architect_write_whose_path_ends_with_a_newline_is_refused(path: str) -> None:
    """Decision 17, "The trailing newline": the root, `<root>/` or `.` followed
    by a newline was vetted as the root and let through, and a path of newlines
    only as no path; each names a file no list judged. A path that ends with one
    newline or more is refused, whatever it names."""
    result = run_configured_rooted("architect", EDIT_WRITE, "Write", path)
    assert_trailing_newline_denied(result, f"architect writing {repo_relative_id(path)!r}")


TRAILING_NEWLINE_READS: list[tuple[str, str, str]] = [
    ("Grep-tests", "Grep", "tests\n"),
    ("Read-test-file", "Read", under_repo(TOP_LEVEL_TEST_FILE) + "\n"),
]


@pytest.mark.parametrize(
    ("tool_name", "path"),
    [case[1:] for case in TRAILING_NEWLINE_READS],
    ids=[case[0] for case in TRAILING_NEWLINE_READS],
)
def test_a_test_author_read_whose_path_ends_with_a_newline_is_refused(
    tool_name: str, path: str
) -> None:
    """Decision 17, "The trailing newline": the test covers every in-scope call,
    the test-author's Read and Grep included."""
    result = run_configured_rooted("test-author", READ_GREP_GLOB, tool_name, path)
    assert_trailing_newline_denied(result, f"test-author {tool_name} of {repo_relative_id(path)!r}")


def test_a_guarded_write_whose_path_ends_with_a_newline_is_refused() -> None:
    """Decision 17, "The trailing newline", under an explicit guarded policy."""
    path = under_repo(SERVICE_FILE) + "\n"
    result = run_rooted("Write", policy=DENY_TESTS, file_path=path)
    assert_trailing_newline_denied(result, f"a Write of {SERVICE_FILE} and a newline")


TRAILING_NEWLINE_CONTROLS: list[tuple[str, str]] = [
    ("plain", under_repo("docs/x.md")),
    ("newline-inside-a-name", f"{REPO_ROOT}/docs/a\nb.md"),
]


@pytest.mark.parametrize(
    "path",
    [path for _, path in TRAILING_NEWLINE_CONTROLS],
    ids=[case_id for case_id, _ in TRAILING_NEWLINE_CONTROLS],
)
def test_a_newline_inside_a_path_is_judged_as_written(
    path: str,
) -> None:
    """Decision 17, "The trailing newline": only a newline at the end is
    refused. A newline inside a name is carried intact and judged as written."""
    result = run_configured_rooted("architect", EDIT_WRITE, "Write", path)
    assert_allowed_silently(result, f"architect writing {repo_relative_id(path)!r}")


def test_the_nul_gate_runs_before_the_trailing_newline_test() -> None:
    """Decision 17, "The trailing newline", "Where it sits": directly after the
    NUL gate, so a path with a NUL and a trailing newline keeps the NUL denial."""
    path = under_repo(SERVICE_FILE) + "\x00\n"
    result = run_rooted("Write", policy=DENY_TESTS, file_path=path)
    reason = assert_nul_denied(result, f"a Write of {SERVICE_FILE}, a NUL and a newline")
    assert TRAILING_NEWLINE_PHRASE not in reason, reason


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in FIELD_SILENT_POLICIES],
    ids=[case_id for case_id, _ in FIELD_SILENT_POLICIES],
)
def test_the_trailing_newline_test_polices_only_in_scope_calls_under_a_guarded_policy(
    policy: Mapping[str, str],
) -> None:
    """Decision 17: like the NUL gate, the test runs after the routing and only
    under a guarded policy."""
    result = run_rooted("Write", policy=policy, file_path=f"{REPO_ROOT}\n")
    assert_allowed_silently(result, f"a Write of the root and a newline under {dict(policy)!r}")


# Decision 18's fourth test as amended, and decision 20's usable root, brief T6
# item 3: a `~` at the start of any component.

TILDE_COMPONENT_CASES: list[tuple[str, str, frozenset[str], str, str]] = [
    (
        "test-author-Read-tilde-directory",
        "test-author",
        READ_GREP_GLOB,
        "Read",
        f"{REPO_ROOT}/~/packages/hammertime-core/pyproject.toml",
    ),
    ("test-author-Read-tilde-name", "test-author", READ_GREP_GLOB, "Read", under_repo("tests/~x")),
    ("test-author-Grep-tilde-name", "test-author", READ_GREP_GLOB, "Grep", "tests/~x"),
    ("architect-Write-tilde-name", "architect", EDIT_WRITE, "Write", under_repo("docs/~x.md")),
]


@pytest.mark.parametrize(
    ("agent_name", "tools", "tool_name", "path"),
    [case[1:] for case in TILDE_COMPONENT_CASES],
    ids=[case[0] for case in TILDE_COMPONENT_CASES],
)
def test_a_component_that_begins_with_a_tilde_is_refused(
    agent_name: str, tools: frozenset[str], tool_name: str, path: str
) -> None:
    """Decision 18's fourth test, as the seventh amendment extends it: a path any
    of whose components begins with `~` is out of plain form, and gets the
    plain-form denial, which is unchanged."""
    result = run_configured_rooted(agent_name, tools, tool_name, path)
    what = f"{agent_name} {tool_name} of {repo_relative_id(path)!r}"
    reason = assert_plain_form_denied(result, what)
    assert reason == PLAIN_FORM_MESSAGE, (
        "the plain-form denial must be decision 18's text verbatim.\n"
        f"expected: {PLAIN_FORM_MESSAGE!r}\nreason:   {reason!r}"
    )


TILDE_CONTROL_CASES: list[tuple[str, str, frozenset[str], str, str]] = [
    ("test-author-Read-last-tilde", "test-author", READ_GREP_GLOB, "Read", under_repo("tests/x~")),
    ("architect-Write-inner-tilde", "architect", EDIT_WRITE, "Write", under_repo("docs/a~b.md")),
]


@pytest.mark.parametrize(
    ("agent_name", "tools", "tool_name", "path"),
    [case[1:] for case in TILDE_CONTROL_CASES],
    ids=[case[0] for case in TILDE_CONTROL_CASES],
)
def test_a_tilde_that_does_not_begin_a_component_is_ordinary(
    agent_name: str, tools: frozenset[str], tool_name: str, path: str
) -> None:
    """Decision 18's fourth test: only a component's first character counts."""
    result = run_configured_rooted(agent_name, tools, tool_name, path)
    assert_allowed_silently(result, f"{agent_name} {tool_name} of {repo_relative_id(path)!r}")


@pytest.mark.parametrize(
    ("leaf", "verdict"),
    [("~x", VERDICT_ROOT), ("x~", VERDICT_ALLOW)],
    ids=["tilde-first-root", "tilde-last-control"],
)
def test_a_working_directory_with_a_component_beginning_with_a_tilde_is_not_usable(
    tmp_path: Path, leaf: str, verdict: str
) -> None:
    """Decision 20's usable root, as the seventh amendment amends it: a root with
    a component that begins with `~` is not in plain form, so it is not usable,
    and under PATH_ROOT='cwd' a relative Write is outside it."""
    cwd = f"{tmp_path}/{leaf}"
    result = run_rooted(
        "Write",
        policy=CWD_ROOT_POLICY,
        file_path="services/x.py",
        cwd=cwd,
        project_dir=str(REPO_ROOT),
    )
    assert_verdict(result, verdict, f"a Write of services/x.py, cwd <tmp>/{leaf}")


# Decision 23, rules 1 and 2, brief T6 item 4: a search's values, under the
# test-author's configured read policy.

SEARCH_VALUE_REFUSED: list[tuple[str, str, dict[str, Any]]] = [
    ("Grep-path-dash", "Grep", {"path": "-u", "pattern": "PROBE"}),
    ("Grep-pattern-dash", "Grep", {"path": "tests", "pattern": "--pre=sh"}),
    ("Grep-type-dash", "Grep", {"path": "tests", "pattern": "x", "type": "-u"}),
    ("Grep-glob-dash", "Grep", {"path": "tests", "pattern": "x", "glob": "-*.py"}),
    ("Glob-pattern-dash", "Glob", {"path": "tests", "pattern": "-x*.py"}),
    (
        "Glob-dot-question-first",
        "Glob",
        {"path": "tests", "pattern": ".?/packages/*/pyproject.toml"},
    ),
    ("Glob-dot-star-first", "Glob", {"path": "tests", "pattern": ".*/x"}),
    ("Glob-dot-question-last", "Glob", {"path": "tests", "pattern": "config/.?"}),
    ("Grep-glob-dot-star", "Grep", {"path": "tests", "pattern": "x", "glob": "a/.*.py"}),
]


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [case[1:] for case in SEARCH_VALUE_REFUSED],
    ids=[case[0] for case in SEARCH_VALUE_REFUSED],
)
def test_a_search_value_that_begins_with_a_dash_or_a_dot_wildcard_is_refused(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    """Decision 23, rules 1 and 2: no string in a Grep's or a Glob's
    `tool_input` may begin with `-`, whichever field holds it, and no component
    of a Glob `pattern` or a Grep `glob` may begin with `.` followed by `*` or
    `?`. Each value here is in decision 21's grammar."""
    result = run_test_author_search(tool_name, tool_input)
    assert_search_value_denied(result, f"test-author {tool_name} with tool_input {tool_input!r}")


SEARCH_VALUE_ALLOWED: list[tuple[str, str, dict[str, Any]]] = [
    ("Glob-inner-dot-question", "Glob", {"path": "tests", "pattern": "x.?y"}),
    ("Glob-leading-question", "Glob", {"path": "tests", "pattern": "?*/config/*.py"}),
    ("Grep-pattern-bracketed-dash", "Grep", {"path": "tests", "pattern": "[-]-locked"}),
    ("Grep-pattern-inner-dash", "Grep", {"path": "tests", "pattern": "a-b"}),
    (
        "Read-pattern-dash",
        "Read",
        {"file_path": under_repo(TOP_LEVEL_TEST_FILE), "pattern": "-x"},
    ),
]


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [case[1:] for case in SEARCH_VALUE_ALLOWED],
    ids=[case[0] for case in SEARCH_VALUE_ALLOWED],
)
def test_a_search_value_decision_23_admits_is_allowed(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    """Decision 23's controls: a `.` and a wildcard that do not begin a
    component, a `-` that is not a value's first character, and a Read, which
    the rules do not reach."""
    result = run_test_author_search(tool_name, tool_input)
    assert_allowed_silently(result, f"test-author {tool_name} with tool_input {tool_input!r}")


def test_decision_21s_pattern_denial_comes_before_decision_23s() -> None:
    """Decision 23, "Where it sits": directly after decision 21's check, so a
    value decision 21 refuses keeps decision 21's denial."""
    tool_input = {"path": "tests", "pattern": "-../x"}
    result = run_test_author_search("Glob", tool_input)
    reason = assert_pattern_denied(result, f"test-author Glob with tool_input {tool_input!r}")
    assert SEARCH_VALUE_PHRASE not in reason, reason


def test_the_search_value_rules_police_only_in_scope_calls_under_a_guarded_policy() -> None:
    """Decision 23: every other call keeps its verdict, a call from a caller the
    policy does not name and a call under a policy that constrains no paths
    among them."""
    tool_input = {"path": "-u", "pattern": "PROBE"}
    outside = run_test_author_search("Grep", tool_input, agent_type=None)
    assert_allowed_silently(outside, f"a Grep with no agent_type and tool_input {tool_input!r}")
    unguarded = run_rooted("Grep", policy={}, tool_input=tool_input)
    assert_allowed_silently(unguarded, f"a Grep with tool_input {tool_input!r}, no policy")


# Decision 23, rule 3, brief T6 item 5: a search's path, under the test-author's
# configured read policy.

SEARCH_PATH_REFUSED: list[tuple[str, str, dict[str, Any]]] = [
    ("Glob-star-in-path", "Glob", {"path": f"{REPO_ROOT}/pack*", "pattern": "*.toml"}),
    ("Grep-space", "Grep", {"path": "tests/x y", "pattern": "PROBE"}),
    ("Grep-bracket", "Grep", {"path": "tests/[a]", "pattern": "PROBE"}),
    ("Grep-brace", "Grep", {"path": "tests/a{b,c}", "pattern": "PROBE"}),
    ("Grep-backslash", "Grep", {"path": "tests/a\\b", "pattern": "PROBE"}),
    ("Grep-non-ascii", "Grep", {"path": "tests/é", "pattern": "PROBE"}),
]


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [case[1:] for case in SEARCH_PATH_REFUSED],
    ids=[case[0] for case in SEARCH_PATH_REFUSED],
)
def test_a_search_path_outside_decision_23s_characters_is_refused(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    """Decision 23, rule 3: a Grep's or a Glob's `path` holds only ASCII letters,
    digits and `_ - . /`. The `pack*` case was allowed, because no list matches
    the string `pack*`. The non-ASCII case checks that `jq`'s ranges are
    codepoints."""
    result = run_test_author_search(tool_name, tool_input)
    what = f"test-author {tool_name} of {repo_relative_id(tool_input['path'])!r}"
    assert_search_path_denied(result, what)


SEARCH_PATH_ALLOWED: list[tuple[str, str, dict[str, Any]]] = [
    ("Grep-tests-config", "Grep", {"path": "tests/config", "pattern": "PROBE"}),
    ("Grep-every-allowed-character", "Grep", {"path": "tests/x-y_z.1", "pattern": "PROBE"}),
    ("Glob-absolute", "Glob", {"path": under_repo("tests/config"), "pattern": "*.py"}),
]


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [case[1:] for case in SEARCH_PATH_ALLOWED],
    ids=[case[0] for case in SEARCH_PATH_ALLOWED],
)
def test_a_search_path_in_decision_23s_characters_is_allowed(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    """Decision 23, rule 3's controls: the repository's root is in the grammar,
    so an absolute path under it is judged by the lists as before."""
    result = run_test_author_search(tool_name, tool_input)
    what = f"test-author {tool_name} of {repo_relative_id(tool_input['path'])!r}"
    assert_allowed_silently(result, what)


def test_a_search_path_out_of_plain_form_keeps_decision_18s_denial() -> None:
    """Decision 23, "Where it sits": rule 3 runs after decision 18's rule."""
    tool_input = {"path": "tests/../x y", "pattern": "PROBE"}
    result = run_test_author_search("Grep", tool_input)
    reason = assert_plain_form_denied(result, "test-author Grep of 'tests/../x y'")
    assert SEARCH_PATH_PHRASE not in reason, reason


def test_a_search_path_outside_the_root_keeps_decision_20s_denial() -> None:
    """Decision 23, "Where it sits": rule 3 runs after decision 20's root rule."""
    tool_input = {"path": f"{REPO_ROOT.parent}/x y", "pattern": "PROBE"}
    result = run_test_author_search("Grep", tool_input)
    reason = assert_root_denied(result, "test-author Grep of '<parent>/x y'")
    assert SEARCH_PATH_PHRASE not in reason, reason


# Decision 19, part 3 as amended, and decision 25, rule 1, brief T6 item 6:
# reading the payload. The payloads are T4's well-formed Write of
# IMPLEMENTATION_PATH with no `agent_type`, which each of SHAPE_POLICIES allows or
# passes through, but for the raw byte or the size.

# Decision 25, rule 1: the most of a payload either script reads.
PAYLOAD_BOUND = 8388609
MIB = 1024 * 1024
OVER_8_MIB = 9 * MIB
JUST_UNDER_8_MIB = 8 * MIB - 4096

# Decision 19's table: a raw NUL or U+0002 gets the shape denial naming status 0.
SHAPE_STATUS_0 = "(the check ended with status 0)"


def raw_bytes(text: str) -> str:
    """`text`, a JSON text, with each JSON escape of a NUL or of U+0002 replaced by
    that character itself (brief T6)."""
    return text.replace("\\u0000", "\x00").replace("\\u0002", "\x02")


def write_payload(path: str) -> str:
    """`shape_payload`'s well-formed Write, with `path` as its `file_path`."""
    return shape_payload("tool_input", {"file_path": path})


U0002_PATH = with_nul_inside(IMPLEMENTATION_PATH).replace("\x00", "\x02")

RAW_BYTE_PAYLOADS: list[tuple[str, str]] = [
    ("nul-inside-the-path", raw_bytes(write_payload(with_nul_inside(IMPLEMENTATION_PATH)))),
    ("nul-after-the-closing-brace", WELL_FORMED_PAYLOAD + "\x00"),
    ("only-a-nul", "\x00"),
    ("u0002-inside-the-path", raw_bytes(write_payload(U0002_PATH))),
]


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in SHAPE_POLICIES],
    ids=[case_id for case_id, _ in SHAPE_POLICIES],
)
@pytest.mark.parametrize(
    "stdin",
    [stdin for _, stdin in RAW_BYTE_PAYLOADS],
    ids=[case_id for case_id, _ in RAW_BYTE_PAYLOADS],
)
def test_a_payload_holding_a_raw_nul_or_u0002_gets_the_shape_denial(
    stdin: str, policy: Mapping[str, str]
) -> None:
    """Decision 19, part 3 as amended, and its table: the payload is read with
    each raw NUL kept as U+0002, and a payload holding a raw U+0002 is
    malformed, with status 0, before the routing and under every policy. Before,
    a raw NUL was dropped unseen."""
    assert "\x00" in stdin or "\x02" in stdin, "the payload must hold a raw byte"
    result = run_guard_stdin(stdin, policy=policy)
    reason = assert_shape_denied(result, f"a payload holding a raw byte under {dict(policy)!r}")
    assert SHAPE_STATUS_0 in reason, reason


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in SHAPE_POLICIES],
    ids=[case_id for case_id, _ in SHAPE_POLICIES],
)
def test_a_payload_over_8_mib_gets_the_shape_denial(policy: Mapping[str, str]) -> None:
    """Decision 25, rule 1, and decision 19's table: a payload longer than
    8388609 bytes is cut there, and the cut text is not one JSON value."""
    stdin = tool_payload("Write", {"file_path": IMPLEMENTATION_PATH, "content": "x" * OVER_8_MIB})
    assert len(stdin.encode("utf-8")) > PAYLOAD_BOUND
    result = run_guard_stdin(stdin, policy=policy)
    assert_shape_denied(result, f"a Write payload over 8 MiB under {dict(policy)!r}")


def test_a_raw_nul_after_an_exact_protected_name_gets_the_shape_denial() -> None:
    """Decision 19, part 3 as amended: SA1f's reproduction. `uv.lock`, a raw NUL,
    then `x` was vetted as `uv.lockx`, and allowed."""
    policy = {"DENY_GLOBS": "uv.lock"}
    stdin = raw_bytes(tool_payload("Write", {"file_path": under_repo("uv.lock") + "\x00x"}))
    assert "\x00" in stdin, "the payload must hold a raw NUL"
    result = run_guard_stdin(stdin, policy=policy)
    reason = assert_shape_denied(result, "a Write of uv.lock, a raw NUL, then x")
    assert SHAPE_STATUS_0 in reason, reason


def test_a_payload_just_under_8_mib_is_judged_as_before() -> None:
    """Decision 25, rule 1's control: a well-formed payload under the bound is
    read whole and judged as before, within the helper's timeout."""
    content = "x" * JUST_UNDER_8_MIB
    stdin = tool_payload("Write", {"file_path": under_repo(SERVICE_FILE), "content": content})
    assert len(stdin.encode("utf-8")) < PAYLOAD_BOUND
    result = run_guard_stdin(stdin, policy=DENY_TESTS)
    assert_allowed_silently(result, f"a Write of {SERVICE_FILE} with a payload just under 8 MiB")


# Decision 19, parts 1, 2 and 4, and decision 17, brief T6 items 7-9: wrapper
# `jq`s. Each tells the script's calls apart by their arguments only, as
# decisions 17, 19 and 24 and brief C8 fix them: the shape check is the one call
# with `-s`, an extraction line a `-r` call whose filter names its field (the path
# line's names `file_path`), and the NUL gates' filters contain `any(. == 0)`.

NEEDS_DEV_FULL = pytest.mark.skipif(
    not Path("/dev/full").exists(),
    reason="the failing printf writes to /dev/full, and /dev/full does not exist",
)


def sh_lines(*lines: str) -> str:
    """`lines` as the text of a shell script, one to a line."""
    return "".join(f"{line}\n" for line in lines)


# The start of every wrapper: it notes whether an argument is `-r` or `-s`, alone
# or in a cluster of short options, and defines `has`, true when the arguments,
# joined by spaces, contain the given text.
WRAPPER_JQ_HEAD = sh_lines(
    "#!/bin/sh",
    "raw=0",
    "slurp=0",
    'for arg in "$@"; do',
    '  case "$arg" in',
    "    --raw-output) raw=1 ;;",
    "    --slurp) slurp=1 ;;",
    "    --*) : ;;",
    "    -*)",
    '      case "$arg" in *r*) raw=1 ;; esac',
    '      case "$arg" in *s*) slurp=1 ;; esac',
    "      ;;",
    "  esac",
    "done",
    'all=" $* "',
    "has() {",
    '  case "$all" in *"$1"*) return 0 ;; esac',
    "  return 1",
    "}",
)

# Item 7: prints nothing, and exits 1 for the shape check and 2 for every other call.
STATUS_2_RULE = sh_lines(
    "cat >/dev/null",
    'if [ "$slurp" = 1 ]; then exit 1; fi',
    "exit 2",
)

# Item 9: a NUL gate's `jq` sends itself SIGKILL.
KILL_NUL_GATE_RULE = sh_lines(
    "if has 'any(. == 0)'; then",
    "  cat >/dev/null",
    "  kill -KILL $$",
    "fi",
)

# Item 8: the condition that picks out each extraction line of `path-guard.sh`.
EXTRACTION_LINES = {"agent_type": "has agent_type", "path": "has file_path"}


def blank_extraction_rule(condition: str) -> str:
    """Item 8: print nothing and exit 0 for the `-r` call `condition` picks out."""
    return sh_lines(
        f'if [ "$raw" = 1 ] && {condition}; then',
        "  cat >/dev/null",
        "  exit 0",
        "fi",
    )


def write_wrapper_jq(directory: Path, rule: str) -> None:
    """An executable `jq` in `directory`: WRAPPER_JQ_HEAD, then `rule`, then the
    real `jq` found at import, run with the same arguments and stdin."""
    real = shlex.quote(JQ or "jq")
    jq = directory / "jq"
    jq.write_text(WRAPPER_JQ_HEAD + rule + f'exec {real} "$@"\n', encoding="utf-8")
    jq.chmod(0o755)


def run_guard_stdin_stderr_full(
    stdin: str,
    *,
    policy: Mapping[str, str],
    jq_dir: Path,
) -> subprocess.CompletedProcess[str]:
    """`run_guard_stdin`'s sibling with the guard's stderr opened on `/dev/full`,
    where every write fails, so that `deny`'s fallback `printf` fails (brief T6,
    item 7). Only stdout is captured; `stderr` is None."""
    assert GUARD.is_file(), f"{GUARD} does not exist, so no guard can run"

    env = dict(os.environ)
    for name in ROOTED_POLICY_VARS:
        env.pop(name, None)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    env["PATH"] = f"{jq_dir}:{os.environ.get('PATH', '')}"
    env.update(policy)

    with open("/dev/full", "w", encoding="utf-8") as full:
        return subprocess.run(
            [BASH or "bash", str(GUARD)],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=full,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )


def assert_backstop_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 19's backstop denial on stdout: exit 2, the JSON deny, the prefix,
    its phrase, and its text verbatim but for the status."""
    reason = assert_denied(result, what)
    assert reason.startswith(DENIAL_PREFIX), reason
    assert BACKSTOP_PHRASE in reason, f"expected the backstop denial for {what}: {reason!r}"
    assert BACKSTOP_PATTERN.fullmatch(reason), (
        f"the backstop denial for {what} must be decision 19's text verbatim, with N a "
        f"status number.\nreason: {reason!r}"
    )
    return reason


def assert_backstop_status(
    result: subprocess.CompletedProcess[str], status: int, what: str
) -> None:
    """Decision 19's backstop denial, verbatim, naming `status`."""
    reason = assert_backstop_denied(result, what)
    expected = BACKSTOP_TEMPLATE.replace("status N", f"status {status}")
    assert reason == expected, f"expected {expected!r} for {what}\nreason: {reason!r}"


@NEEDS_BIN_SH
def test_a_command_that_fails_with_status_2_gets_the_backstop_denial(tmp_path: Path) -> None:
    """Decision 19, part 1 as amended: the handler lets through only `deny`'s own
    exit 2. With a `jq` that passes the shape check and then exits 2, the first
    extraction line ends the script with status 2 and no reason, which the
    handler turns into the backstop denial, naming status 2. With the real `jq`,
    the same Write is allowed."""
    path = under_repo(SERVICE_FILE)
    control = run_rooted("Write", policy=DENY_TESTS, file_path=path)
    assert_allowed_silently(control, f"a Write of {SERVICE_FILE} with the real jq")
    write_wrapper_jq(tmp_path, STATUS_2_RULE)
    result = run_rooted("Write", policy=DENY_TESTS, file_path=path, jq_dir=tmp_path)
    assert_backstop_status(result, 2, f"a Write of {SERVICE_FILE} with a jq that exits 2")


@NEEDS_BIN_SH
@NEEDS_DEV_FULL
def test_a_deny_whose_fallback_printf_fails_gets_the_backstop_denial(tmp_path: Path) -> None:
    """Decision 19, parts 1 and 2: with a `jq` that exits 3, the shape check does
    not complete and `deny`'s own `jq` fails; with stderr on `/dev/full`, its
    fallback `printf` fails too, and the handler writes the backstop denial on
    stdout and exits 2."""
    write_failing_jq(tmp_path, 3)
    stdin = tool_payload("Write", {"file_path": under_repo(SERVICE_FILE)})
    result = run_guard_stdin_stderr_full(stdin, policy=DENY_TESTS, jq_dir=tmp_path)
    assert_backstop_denied(result, f"a Write of {SERVICE_FILE}, jq exiting 3, stderr full")


EXTRACTION_CASES: list[tuple[str, Mapping[str, str], str | None]] = [
    ("agent_type", SCOPED_TO_CODER, "coder"),
    ("path", DENY_TESTS, None),
]


@NEEDS_BIN_SH
@pytest.mark.parametrize(
    ("line", "policy", "agent_type"),
    EXTRACTION_CASES,
    ids=[case[0] for case in EXTRACTION_CASES],
)
def test_an_extraction_line_that_reads_its_field_as_empty_gets_the_backstop_denial(
    tmp_path: Path, line: str, policy: Mapping[str, str], agent_type: str | None
) -> None:
    """Decision 19, part 4: the extraction check asks whether each extracted
    field is empty exactly when the payload's is. A `jq` that prints nothing for
    one extraction line makes that field read as empty, and the check refuses
    with the backstop denial. Before, an empty `agent_type` routed the call out
    of scope and an empty path let a Write through. With the real `jq`, the same
    Write gets the DENY_GLOBS denial."""
    path = under_repo("tests/x.py")
    control = run_rooted("Write", policy=policy, file_path=path, agent_type=agent_type)
    assert_deny_globs_denied(control, f"a Write of tests/x.py under {dict(policy)!r}")
    write_wrapper_jq(tmp_path, blank_extraction_rule(EXTRACTION_LINES[line]))
    result = run_rooted(
        "Write", policy=policy, file_path=path, agent_type=agent_type, jq_dir=tmp_path
    )
    assert_backstop_denied(result, f"a Write of tests/x.py with the {line} line read as empty")


BAD_CWDS: list[tuple[str, str]] = [
    ("trailing-newline", f"{REPO_ROOT}\n"),
    ("nul", f"{REPO_ROOT}\x00x"),
]


@pytest.mark.parametrize(
    "cwd",
    [cwd for _, cwd in BAD_CWDS],
    ids=[case_id for case_id, _ in BAD_CWDS],
)
def test_a_cwd_that_cannot_be_read_faithfully_gets_the_backstop_denial(cwd: str) -> None:
    """Decision 19, part 4: `$(...)` would read a `cwd` that ends with a newline
    or holds a NUL as another directory, so the extraction check fails and the
    script ends with status 3, which the handler names."""
    result = run_rooted("Write", policy=DENY_TESTS, file_path=under_repo(SERVICE_FILE), cwd=cwd)
    assert_backstop_status(result, 3, f"a Write of {SERVICE_FILE} with cwd {cwd!r}")


def test_the_cwd_check_runs_before_the_routing() -> None:
    """Decision 19, part 4, "Every caller": the check runs before the routing,
    so a call with no `agent_type` under a policy scoped to the coder is refused
    the same way."""
    cwd = f"{REPO_ROOT}\n"
    path = under_repo(SERVICE_FILE)
    result = run_rooted("Write", policy=SCOPED_TO_CODER, file_path=path, cwd=cwd)
    assert_backstop_status(result, 3, f"a Write with no agent_type and cwd {cwd!r}")


@pytest.mark.parametrize(
    "policy",
    [DENY_TESTS, SCOPED_TO_CODER],
    ids=["guarded", "outside-scope"],
)
def test_a_cwd_read_faithfully_passes_the_extraction_check(policy: Mapping[str, str]) -> None:
    """Decision 19, part 4's controls: the same Writes, with `cwd` the repository
    root, are allowed."""
    path = under_repo(SERVICE_FILE)
    result = run_rooted("Write", policy=policy, file_path=path, cwd=str(REPO_ROOT))
    assert_allowed_silently(result, f"a Write of {SERVICE_FILE} under {dict(policy)!r}")


@NEEDS_BIN_SH
def test_a_nul_gate_killed_by_a_signal_gets_the_not_checked_denial(tmp_path: Path) -> None:
    """Decisions 3 and 17, and assumption 100: a NUL gate whose `jq` is killed by
    SIGKILL ends with status 137, neither 0 nor 1, so the could-not-be-checked
    denial follows, naming 137."""
    write_wrapper_jq(tmp_path, KILL_NUL_GATE_RULE)
    path = under_repo(SERVICE_FILE)
    result = run_rooted("Write", policy=DENY_TESTS, file_path=path, jq_dir=tmp_path)
    reason = assert_not_checked_denied(result, f"a Write of {SERVICE_FILE}, the NUL gate killed")
    assert "(the check ended with status 137 instead" in reason, reason


# Decision 12 (e) and (f), brief T6 item 12: the coder's `tests` names and the
# testkit, after step W, with `cwd` and CLAUDE_PROJECT_DIR the repository root.

CODER_TESTS_AND_TESTKIT_AFTER_W: list[tuple[str, str]] = [
    ("tests", VERDICT_DENY_GLOBS),
    ("services/trie/src/hammertime/trie/structure/tests", VERDICT_DENY_GLOBS),
    ("packages/hammertime-testkit", VERDICT_DENY_GLOBS),
    (TESTKIT_FILE, VERDICT_DENY_GLOBS),
    ("services/trie/src/hammertime/trie/structure/tests.py", VERDICT_ALLOW),
]


@pytest.mark.parametrize(
    ("path", "verdict"),
    CODER_TESTS_AND_TESTKIT_AFTER_W,
    ids=[f"{verdict}-{path}" for path, verdict in CODER_TESTS_AND_TESTKIT_AFTER_W],
)
def test_configured_coder_write_policy_refuses_tests_names_and_the_testkit_after_step_w(
    path: str, verdict: str
) -> None:
    """Decision 12 (e) and (f): the coder's DENY_GLOBS gain `tests` and `*/tests`,
    which refuse a file of that name at any depth, and the testkit's directory
    and everything under it. `tests.py` is another name."""
    result = run_configured_rooted(
        "coder",
        EDIT_WRITE,
        "Write",
        under_repo(path),
        cwd=str(REPO_ROOT),
        project_dir=str(REPO_ROOT),
    )
    assert_verdict(result, verdict, f"coder writing {path}")


# --- the eighth amendment: SA1g's findings ------------------------------------
#
# ADR-0018's eighth amendment (2026-09-26), brief T7. Every path that carries
# whitespace, a control character or a character outside ASCII is built by string
# concatenation, with the character in the Python string (written below as its
# Python escape, so that the source shows which one it is); `json.dumps` writes
# it as a JSON escape, as the harness would send it.

# Decision 26's denial, verbatim, with the ADR's blockquote line breaks joined by
# single spaces. It is ASCII only.
EDGE_MESSAGE = (
    "Hammertime path guard: the path begins or ends with whitespace or a "
    "control character, or could not be checked for one, and a tool that trims "
    "its path would act on a path other than the one this guard vetted. Give "
    "the path without them. The tool call is refused."
)
EDGE_PHRASE = "begins or ends with whitespace or a control character"

# Decision 14's coder Edit|Write DENY_GLOBS, copied from its text. It equals the
# coder's Bash WRITE_DENY_GLOBS there.
DECISION_14_CODER_GLOBS = (
    "tests tests/* */tests */tests/* packages/hammertime-testkit "
    "packages/hammertime-testkit/* docs/spec/* docs/adr/* docs/protocol/* schemas/* "
    ".claude .claude/* */.claude */.claude/* CLAUDE.md */CLAUDE.md CLAUDE.local.md "
    "*/CLAUDE.local.md .mcp.json */.mcp.json /* ../* */../* .git .git/* */.git */.git/* "
    ".venv/* */.venv/* __pycache__/* */__pycache__/* conftest.py */conftest.py "
    "test_*.py */test_*.py *_test.py test*.txt */test*.txt pytest.toml */pytest.toml "
    ".pytest.toml */.pytest.toml pytest.ini */pytest.ini .pytest.ini */.pytest.ini "
    "tox.ini */tox.ini setup.cfg */setup.cfg mypy.ini */mypy.ini .mypy.ini */.mypy.ini "
    ".ruff.toml */.ruff.toml */ruff.toml uv.toml */uv.toml .python-version "
    "*/.python-version sitecustomize.py */sitecustomize.py usercustomize.py "
    "*/usercustomize.py pytest pytest/* ruff ruff/* mypy mypy/* GNUmakefile makefile "
    "uv.lock"
)

DOCS_X_PATH = under_repo("docs/x.md")


def assert_edge_denied(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Decision 26's denial, verbatim."""
    return assert_denied_with(result, EDGE_MESSAGE, EDGE_PHRASE, what)


def edge_id(path: str) -> str:
    """`path` for a message, independent of where the checkout lives, with every
    character outside printable ASCII written as its escape."""
    return ascii(repo_relative_id(path))


# Decision 26, brief T7 item 1: under the test-author's configured read policy.

EDGE_TEST_AUTHOR_READS: list[tuple[str, str, str]] = [
    ("Read-space-first-implementation", "Read", " " + under_repo(IMPLEMENTATION_FILE)),
    ("Read-space-last-test-file", "Read", under_repo(TOP_LEVEL_TEST_FILE) + " "),
    ("Read-tab-first-test-file", "Read", "\t" + under_repo(TOP_LEVEL_TEST_FILE)),
    ("Read-newline-first-implementation", "Read", "\n" + under_repo(IMPLEMENTATION_FILE)),
    ("Grep-space-first-tests", "Grep", " tests"),
    ("Grep-space-last-tests", "Grep", "tests "),
]


@pytest.mark.parametrize(
    ("tool_name", "path"),
    [case[1:] for case in EDGE_TEST_AUTHOR_READS],
    ids=[case[0] for case in EDGE_TEST_AUTHOR_READS],
)
def test_a_test_author_path_with_whitespace_or_a_control_at_either_end_is_refused(
    tool_name: str, path: str
) -> None:
    """Decision 26: a path whose first or last character is an edge character is
    refused, whatever the tool, a Grep's `path` included. The first case is the
    one the session reproduced: one space before an absolute path under
    `packages/` made it look relative, and no glob anchored at the root matched
    it. A newline at the start is one decision 17's trailing-newline test does not
    reach."""
    result = run_configured_rooted("test-author", READ_GREP_GLOB, tool_name, path)
    assert_edge_denied(result, f"test-author {tool_name} of {edge_id(path)}")


# Decision 26, brief T7 item 1: under the architect's configured Edit|Write policy.

EDGE_ARCHITECT_WRITES: list[tuple[str, str]] = [
    ("space-last-docs-claude-md", under_repo("docs/CLAUDE.md") + " "),
    ("carriage-return-last", DOCS_X_PATH + "\r"),
    ("del-last", DOCS_X_PATH + "\x7f"),
    ("u0085-last", DOCS_X_PATH + "\x85"),
    ("no-break-space-last", DOCS_X_PATH + "\N{NO-BREAK SPACE}"),
    ("ideographic-space-last", DOCS_X_PATH + "\N{IDEOGRAPHIC SPACE}"),
    ("byte-order-mark-first", "\N{ZERO WIDTH NO-BREAK SPACE}" + DOCS_X_PATH),
    ("line-separator-first", "\N{LINE SEPARATOR}" + DOCS_X_PATH),
    ("one-space", " "),
]


@pytest.mark.parametrize(
    "path",
    [path for _, path in EDGE_ARCHITECT_WRITES],
    ids=[case_id for case_id, _ in EDGE_ARCHITECT_WRITES],
)
def test_an_architect_write_with_an_edge_character_at_either_end_is_refused(path: str) -> None:
    """Decision 26: a C0 control, the space, DEL, a C1 control, the no-break
    space, U+2028, U+3000 and U+FEFF are each refused at either end. A trailing
    space after `docs/CLAUDE.md` matched no exact-name glob, and a path that is
    one space names no file a list judged."""
    result = run_configured_rooted("architect", EDIT_WRITE, "Write", path)
    assert_edge_denied(result, f"architect writing {edge_id(path)}")


def test_a_write_with_a_leading_space_is_refused_under_an_explicit_guarded_policy() -> None:
    """Decision 26: under DENY_TESTS, one space before `/tmp/...` made the path
    look relative, and so inside `cwd`."""
    path = " /tmp/hammertime-probe.txt"
    result = run_rooted("Write", policy=DENY_TESTS, file_path=path)
    assert_edge_denied(result, f"a Write of {path!r}")


def test_a_write_with_a_leading_space_is_refused_for_the_caller_a_scoped_policy_names() -> None:
    """Decision 26, "Who and what it covers": the coder, whom the scoped policy
    names, is refused."""
    path = " " + under_repo(SERVICE_FILE)
    result = run_rooted("Write", policy=SCOPED_TO_CODER, file_path=path, agent_type="coder")
    assert_edge_denied(result, f"coder writing {edge_id(path)}")


EDGE_CONTROLS: list[tuple[str, str]] = [
    ("space-inside-a-name", under_repo("docs/a b.md")),
    ("tab-inside-a-name", under_repo("docs/a\tb.md")),
    ("non-ascii-letter-last", DOCS_X_PATH + "\xe9"),
]


@pytest.mark.parametrize(
    "path",
    [path for _, path in EDGE_CONTROLS],
    ids=[case_id for case_id, _ in EDGE_CONTROLS],
)
def test_whitespace_inside_a_path_or_a_letter_at_its_end_is_judged_as_written(
    path: str,
) -> None:
    """Decision 26: trimming reaches only the ends, so a space or a tab inside a
    name names exactly what it spells, and a letter outside ASCII is not an edge
    character. Each Write is in the architect's scope and allowed."""
    result = run_configured_rooted("architect", EDIT_WRITE, "Write", path)
    assert_allowed_silently(result, f"architect writing {edge_id(path)}")


# Decision 26, "Where it sits", brief T7 item 1: the order.


def test_the_nul_gate_runs_before_the_edge_check() -> None:
    """Decision 26: a path with a NUL keeps the NUL denial."""
    path = " " + under_repo(SERVICE_FILE) + "\x00"
    result = run_rooted("Write", policy=DENY_TESTS, file_path=path)
    reason = assert_nul_denied(result, f"a Write of {edge_id(path)}")
    assert EDGE_PHRASE not in reason, reason


def test_the_trailing_newline_test_runs_before_the_edge_check() -> None:
    """Decision 26: directly after decision 17's trailing-newline test, so a path
    that ends with a newline keeps the trailing-newline denial."""
    path = " " + under_repo(SERVICE_FILE) + "\n"
    result = run_rooted("Write", policy=DENY_TESTS, file_path=path)
    assert_trailing_newline_denied(result, f"a Write of {edge_id(path)}")


def test_the_edge_check_runs_before_the_plain_form_rule() -> None:
    """Decision 26 and assumption 112: before decision 18's rule, so a path with
    a leading space and a `..` gets decision 26's denial, not decision 18's."""
    path = " " + under_repo("tests/../x.py")
    result = run_rooted("Write", policy=DENY_TESTS, file_path=path)
    reason = assert_edge_denied(result, f"a Write of {edge_id(path)}")
    assert PLAIN_FORM_PHRASE not in reason, reason


def test_the_edge_check_runs_before_the_pattern_rule() -> None:
    """Decision 26: before decision 21's check, so a Glob whose path ends with a
    space and whose pattern decision 21 refuses gets decision 26's denial."""
    tool_input = {"path": "tests ", "pattern": "../x"}
    result = run_test_author_search("Glob", tool_input)
    reason = assert_edge_denied(result, f"test-author Glob with tool_input {tool_input!r}")
    assert PATTERN_PHRASE not in reason, reason


# Decision 26, "Who and what it covers", brief T7 item 1: the boundaries.


@pytest.mark.parametrize(
    "policy",
    [policy for _, policy in FIELD_SILENT_POLICIES],
    ids=[case_id for case_id, _ in FIELD_SILENT_POLICIES],
)
def test_the_edge_check_polices_only_in_scope_calls_under_a_guarded_policy(
    policy: Mapping[str, str],
) -> None:
    """Decision 26: a policy that constrains no paths, and a policy scoped to the
    coder with a call that sets no `agent_type`, are untouched."""
    path = " " + under_repo(SERVICE_FILE)
    result = run_rooted("Write", policy=policy, file_path=path)
    assert_allowed_silently(result, f"a Write of {edge_id(path)} under {dict(policy)!r}")


# Decision 26, brief T7 item 1: the message.


@pytest.mark.parametrize(
    ("tool_name", "path"),
    [("Write", " /tmp/hammertime-probe.txt"), ("Grep", "tests ")],
    ids=["Write-file_path", "Grep-path"],
)
def test_the_edge_denial_is_decision_26s_text_verbatim(tool_name: str, path: str) -> None:
    """Decision 26: the denial word for word, the ADR's blockquote line breaks read
    as single spaces. It quotes no path."""
    search = tool_name in SEARCH_TOOLS
    result = run_rooted(
        tool_name,
        policy=DENY_TESTS,
        file_path=None if search else path,
        search_path=path if search else None,
    )
    reason = assert_edge_denied(result, f"a {tool_name} of {path!r}")
    assert reason.startswith(DENIAL_PREFIX), reason
    assert reason == EDGE_MESSAGE, reason


# Decision 20, "An empty `cwd`", brief T7 item 2: PATH_ROOT unset, DENY_GLOBS
# 'tests/*', CLAUDE_PROJECT_DIR the repository root. The payloads are built with
# `json.dumps` from a dictionary without `cwd`, or with it `None`, and sent
# through `run_guard_stdin`.


def write_payload_with_cwd(file_path: str, cwd: Any) -> str:
    """A Write payload for `file_path`, as JSON text, whose `cwd` is `cwd`, or
    which has no `cwd` when `cwd` is ABSENT."""
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": file_path},
    }
    if cwd is not ABSENT:
        payload["cwd"] = cwd
    return json.dumps(payload)


EMPTY_CWD_REFUSED: list[tuple[str, Any]] = [("cwd-absent", ABSENT), ("cwd-null", None)]


@pytest.mark.parametrize(
    "cwd",
    [cwd for _, cwd in EMPTY_CWD_REFUSED],
    ids=[case_id for case_id, _ in EMPTY_CWD_REFUSED],
)
def test_a_relative_path_without_a_cwd_is_outside_the_root(cwd: Any) -> None:
    """Decision 20, "An empty `cwd`": with PATH_ROOT unset a relative path is
    inside only when `cwd` is usable, so with `cwd` absent or `null` the relative
    Write is refused with the root denial, although CLAUDE_PROJECT_DIR is usable.
    The case with `cwd` empty is T4's `cwd-empty-dir-repo`."""
    stdin = write_payload_with_cwd("docs/x.md", cwd)
    result = run_guard_stdin(stdin, policy=UNSET_ROOT_POLICY, project_dir=str(REPO_ROOT))
    shown = "absent" if cwd is ABSENT else repr(cwd)
    assert_root_denied(result, f"a Write of 'docs/x.md', cwd {shown}")


EMPTY_CWD_CONTROLS: list[tuple[str, str, Any]] = [
    ("absolute-cwd-absent", DOCS_X_PATH, ABSENT),
    ("absolute-cwd-null", DOCS_X_PATH, None),
    ("absolute-cwd-empty", DOCS_X_PATH, ""),
    ("relative-cwd-repo", "docs/x.md", str(REPO_ROOT)),
]


@pytest.mark.parametrize(
    ("path", "cwd"),
    [case[1:] for case in EMPTY_CWD_CONTROLS],
    ids=[case[0] for case in EMPTY_CWD_CONTROLS],
)
def test_an_absolute_path_without_a_cwd_is_still_judged_against_the_project(
    path: str, cwd: Any
) -> None:
    """Decision 20, "An empty `cwd`": an absolute path is unaffected, and is still
    compared with each usable base, CLAUDE_PROJECT_DIR among them; a relative path
    with `cwd` the repository root gets the verdict it got before."""
    stdin = write_payload_with_cwd(path, cwd)
    result = run_guard_stdin(stdin, policy=UNSET_ROOT_POLICY, project_dir=str(REPO_ROOT))
    shown = "absent" if cwd is ABSENT else repr(cwd)
    assert_allowed_silently(result, f"a Write of {repo_relative_id(path)!r}, cwd {shown}")


# Decision 25's eighth-amendment note, brief T7 item 6: a long path.

LONG_PATH_COMPONENTS = 200_000


def test_a_long_coder_path_is_decided_within_the_timeout(tmp_path: Path) -> None:
    """Decision 25, its eighth-amendment note, and assumption 116: matching a path
    against decision 14's coder DENY_GLOBS, two of which have a `*` that is
    neither first nor last, takes time linear in the path's length. A Write
    whose path has 200,000 components `test_a`, and which no glob matches, is
    allowed within the helper's timeout."""
    policy = {
        "SCOPE_AGENT_TYPES": "coder",
        "PATH_ROOT": "cwd",
        "DENY_GLOBS": DECISION_14_CODER_GLOBS,
    }
    path = f"{tmp_path}/a" + "/test_a" * LONG_PATH_COMPONENTS + ".md"
    result = run_rooted(
        "Write",
        policy=policy,
        tool_input={"file_path": path, "content": "x"},
        agent_type="coder",
        cwd=str(tmp_path),
        project_dir=str(REPO_ROOT),
    )
    assert_allowed_silently(
        result, f"a coder Write of <tmp>/a, /test_a {LONG_PATH_COMPONENTS} times, then .md"
    )
