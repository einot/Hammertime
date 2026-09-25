"""The subagent guard hooks are wired where the CLI actually reads them.

These are configuration regression tests rather than spec tests. They exist
because of issue #102: every write-capable subagent (`coder`, `architect`,
`test-author`) had its guard declared under a `hooks:` key in the agent file's
own YAML frontmatter, and a guard declared that way did not fire in this
environment. It was probed three times, once with an absolute script path, and
each time there was no error, no warning, nothing to notice. From outside, that
is indistinguishable from a guard that runs and allows everything.

The Claude Code docs do list `hooks` as a supported subagent frontmatter field,
and say that project-level frontmatter hooks run only once the workspace trust
dialog has been accepted for the folder. This session has no trust record for
the project. That is the best-supported explanation, but it has not been
confirmed directly, and the ban does not rest on it. It rests on the
consequence: whether a frontmatter guard fires depends on environment state
that is invisible from the repository, so the same agent file can look enforced
on one machine and silently do nothing on another. (An earlier probe appeared
to show the frontmatter block firing; that denial has since been traced to the
main checkout's `settings.json`, not to the frontmatter.)

`.claude/settings.json` is the documented wiring location, and the hooks
declared there fired in every probe, which is why it is the only one permitted
here. The CLI reads hook configuration from the main project checkout, not from
a subagent's worktree, so a worktree's copy of the file is inert. Hooks declared
there are *session-wide*, so each policy names the agent(s) it applies to with a
`SCOPE_AGENT_TYPES='<name>'` assignment on the hook's command line; a policy
that constrains paths but names no agent would police every caller, including
the top-level session.

Every invariant below is derived by globbing `.claude/agents/*.md` and reading
`.claude/settings.json`, so a newly added agent that can write or execute fails
these tests without anyone having to remember to update them.

ADR-0018 (`docs/adr/0018-coder-bash-policy-literal-commands-and-a-tripwire.md`)
adds the coder's Bash policy. The last section of this module pins it: decision
14's exact knob values, decision 3's rule that a policy fencing a writing agent
or enabling a write-capable rule is literal, decision 4's refusal to list generic
interpreters and writers, and decision 12's Edit/Write globs, which decision 7
requires to equal the Bash policy's `WRITE_DENY_GLOBS`. Those coder items fail
until the top-level session applies decision 14 (step W).

ADR-0018's fifth amendment (2026-09-25) extends decision 14's text with
decisions 20 and 22, and the module's final section pins them (brief T4, item
6): every `path-guard.sh` policy sets `PATH_ROOT`, to `cwd` for the coder and
to `project` for the architect and the test-author; no `ALLOW_GLOBS` or
`EXEMPT_GLOBS` glob begins with `*`; every Edit/Write policy's `DENY_GLOBS`
carries decision 22's agent-configuration list; the test-author's lists are
decision 22's; and the architect's allowlist is unchanged. Those items fail
until step W.

See also `.claude/hooks/path-guard.sh` and `.claude/hooks/bash-guard.sh`, whose
own headers record the probes of the frontmatter wiring, and
`tests/config/test_path_guard_behavior.py`, which exercises the guard script
itself.
"""

import dataclasses
import json
import os
import shlex
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_DIR = REPO_ROOT / ".claude"
AGENTS_DIR = CLAUDE_DIR / "agents"
HOOKS_DIR = CLAUDE_DIR / "hooks"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"

# Tools that let an agent change the repository or run a command. An agent
# holding any of these has to be named by a policy in settings.json.
GUARDED_TOOLS = frozenset({"Edit", "Write", "Bash"})

# Env assignments on a hook command line that actually constrain something. A
# policy setting none of these is inert, whatever else it says.
CONSTRAINT_VARS = ("DENY_GLOBS", "ALLOW_GLOBS", "ALLOW_CMDS")

WHY_FRONTMATTER_HOOKS_ARE_BANNED = (
    "A frontmatter 'hooks:' key is not a reliable way to wire a guard. A policy declared "
    "there did not fire in this environment -- probed three times, once with an absolute "
    "script path -- with no error, no warning, nothing to notice (issue #102). The Claude "
    "Code docs say project-level frontmatter hooks run only once workspace trust has been "
    "accepted for the folder, which this session has no record of; whether or not that is "
    "the cause, a frontmatter guard depends on environment state invisible from the "
    "repository, so it can look enforced on one machine and silently do nothing on another. "
    "An agent configured that way can run unfenced while its own file claims it is guarded, "
    "which is worse than having no guard at all. Declare the policy in .claude/settings.json "
    "under hooks.PreToolUse instead: that is the documented location, and hooks declared "
    "there fired in every probe. Scope it with SCOPE_AGENT_TYPES='<agent name>' on the hook "
    "command line."
)


# --- parsing -------------------------------------------------------------


def parse_frontmatter(text: str) -> dict[str, str]:
    """Return the top-level YAML frontmatter keys of an agent markdown file.

    Deliberately line-based rather than a real YAML parse. All these tests need
    is which top-level keys are present and the raw text of the scalar ones, and
    this keeps the suite runnable without PyYAML, which this project does not
    depend on. Nested lines (the body of a `hooks:` block, for instance) are
    indented and are skipped; the presence of the top-level key is the point.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            continue
        fields[key.strip()] = value.strip()
    return fields


def expand_project_dir(raw: str) -> Path:
    """Resolve a hook command's script path the way the CLI would."""
    expanded = raw.replace("${CLAUDE_PROJECT_DIR}", str(REPO_ROOT))
    expanded = expanded.replace("$CLAUDE_PROJECT_DIR", str(REPO_ROOT))
    path = Path(expanded)
    return path if path.is_absolute() else REPO_ROOT / path


def split_hook_command(command: str) -> tuple[dict[str, str], str]:
    """Split `VAR='x' VAR2='y' /path/to/script.sh` into its env and its script.

    The env assignments are how one shared guard script is parametrised per
    policy, so they are the payload of a settings entry, not decoration.
    """
    tokens = shlex.split(command)
    env: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        name, separator, value = tokens[index].partition("=")
        if not separator or not name.isidentifier():
            break
        env[name] = value
        index += 1
    script = tokens[index] if index < len(tokens) else ""
    return env, script


@dataclasses.dataclass(frozen=True, eq=False)
class AgentFile:
    path: Path
    fields: dict[str, str]

    @property
    def name(self) -> str:
        return self.fields.get("name", self.path.stem)

    @property
    def tools(self) -> tuple[str, ...]:
        raw = self.fields.get("tools", "")
        return tuple(tool.strip() for tool in raw.split(",") if tool.strip())

    @property
    def guarded_tools(self) -> tuple[str, ...]:
        return tuple(tool for tool in self.tools if tool in GUARDED_TOOLS)

    @property
    def needs_policy(self) -> bool:
        return bool(self.guarded_tools)


@dataclasses.dataclass(frozen=True, eq=False)
class HookCommand:
    matcher: str
    command: str
    env: dict[str, str]
    script: str

    @property
    def matched_tools(self) -> frozenset[str]:
        return frozenset(part.strip() for part in self.matcher.split("|") if part.strip())

    @property
    def scoped_agents(self) -> frozenset[str]:
        return frozenset(self.env.get("SCOPE_AGENT_TYPES", "").split())

    @property
    def script_path(self) -> Path:
        return expand_project_dir(self.script)


def load_agents() -> list[AgentFile]:
    if not AGENTS_DIR.is_dir():
        return []
    return [
        AgentFile(path=path, fields=parse_frontmatter(path.read_text(encoding="utf-8")))
        for path in sorted(AGENTS_DIR.glob("*.md"))
    ]


def load_pretooluse_entries() -> list[dict[str, Any]]:
    assert SETTINGS_PATH.is_file(), (
        f"{SETTINGS_PATH} does not exist, and it is the only place a subagent "
        f"PreToolUse hook may be wired from. {WHY_FRONTMATTER_HOOKS_ARE_BANNED}"
    )
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    hooks = settings.get("hooks")
    assert isinstance(hooks, dict), f"{SETTINGS_PATH} has no 'hooks' object; found {hooks!r}"
    entries = hooks.get("PreToolUse")
    assert isinstance(entries, list) and entries, (
        f"{SETTINGS_PATH} has no non-empty hooks.PreToolUse array, so no guard is wired "
        f"for any subagent at all. {WHY_FRONTMATTER_HOOKS_ARE_BANNED}"
    )
    return entries


def load_hook_commands() -> list[HookCommand]:
    commands: list[HookCommand] = []
    for entry in load_pretooluse_entries():
        matcher = entry.get("matcher", "")
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            env, script = split_hook_command(command)
            commands.append(HookCommand(matcher=matcher, command=command, env=env, script=script))
    return commands


def _hook_commands_or_empty() -> list[HookCommand]:
    # Collection must not blow up on a missing or malformed settings file: the
    # shape tests below report that far more clearly than an import error does.
    try:
        return load_hook_commands()
    except (AssertionError, OSError, ValueError):
        return []


AGENTS = load_agents()
AGENT_IDS = [agent.path.stem for agent in AGENTS]
HOOK_COMMANDS = _hook_commands_or_empty()
HOOK_IDS = [
    f"{index}-{command.matcher}-{Path(command.script).name}"
    for index, command in enumerate(HOOK_COMMANDS)
]


def policies_for(agent_name: str) -> list[HookCommand]:
    return [command for command in HOOK_COMMANDS if agent_name in command.scoped_agents]


# --- the derivation itself, so nothing below passes vacuously ------------


def test_agent_files_are_discovered() -> None:
    """Every parametrised test here is derived from this glob."""
    assert AGENTS_DIR.is_dir(), f"{AGENTS_DIR} does not exist"
    assert AGENTS, f"no agent definitions found under {AGENTS_DIR}"


def test_hook_commands_were_parsed() -> None:
    """A settings file that parses to no hook commands makes the policy tests
    below pass without testing anything."""
    assert HOOK_COMMANDS, (
        f"no PreToolUse hook commands could be parsed out of {SETTINGS_PATH}; every "
        "policy test in this module would pass vacuously"
    )


def test_agent_capability_classification_is_not_vacuous() -> None:
    """Both halves of the Edit/Write/Bash split must be populated.

    If the frontmatter parser ever stops finding `tools:` lines, every agent
    looks read-only, and the coverage tests below stop testing anything.
    """
    needs = sorted(agent.name for agent in AGENTS if agent.needs_policy)
    read_only = sorted(agent.name for agent in AGENTS if not agent.needs_policy)
    assert needs, (
        "no agent was classified as write/execution capable; the 'tools:' frontmatter "
        "line is probably no longer being parsed"
    )
    assert read_only, (
        "no agent was classified as read-only; the 'tools:' frontmatter line is "
        "probably no longer being parsed"
    )


def test_settings_declares_well_formed_pretooluse_hooks() -> None:
    entries = load_pretooluse_entries()
    for index, entry in enumerate(entries):
        where = f"{SETTINGS_PATH} hooks.PreToolUse[{index}]"
        matcher = entry.get("matcher")
        assert isinstance(matcher, str) and matcher, (
            f"{where} declares no 'matcher' naming the tools it applies to, so it is "
            f"not clear what it guards: {entry!r}"
        )
        hooks = entry.get("hooks")
        assert isinstance(hooks, list) and hooks, f"{where} has no non-empty 'hooks' array"
        for hook_index, hook in enumerate(hooks):
            hook_where = f"{where}.hooks[{hook_index}]"
            assert isinstance(hook, dict), f"{hook_where} is not an object: {hook!r}"
            assert hook.get("type") == "command", f"{hook_where} is not a command hook: {hook!r}"
            command = hook.get("command")
            assert isinstance(command, str) and command.strip(), (
                f"{hook_where} has an empty or missing 'command', so the hook runs nothing"
            )


# --- the invariants ------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS, ids=AGENT_IDS)
def test_agent_file_declares_no_frontmatter_hooks(agent: AgentFile) -> None:
    assert "hooks" not in agent.fields, (
        f"{agent.path} declares a frontmatter 'hooks:' key. "
        f"{WHY_FRONTMATTER_HOOKS_ARE_BANNED} Delete the block from the agent file: "
        "leaving it beside a working settings.json policy is a second copy of the "
        "policy that nothing reliably enforces and nothing keeps in sync."
    )


@pytest.mark.parametrize("agent", AGENTS, ids=AGENT_IDS)
def test_agent_name_matches_its_filename(agent: AgentFile) -> None:
    """Scoping matches the payload's `agent_type` against SCOPE_AGENT_TYPES.

    Keeping the frontmatter `name` and the filename identical means there is
    only one spelling of an agent for a policy to get wrong.
    """
    assert agent.fields.get("name") == agent.path.stem, (
        f"{agent.path} declares name={agent.fields.get('name')!r}, which differs from its "
        "filename. A settings.json policy is scoped by agent name, so two spellings is "
        "one more than can be checked."
    )


@pytest.mark.parametrize("agent", AGENTS, ids=AGENT_IDS)
def test_write_or_exec_capable_agent_is_named_by_a_policy(agent: AgentFile) -> None:
    if not agent.needs_policy:
        pytest.skip(f"{agent.name} holds none of {sorted(GUARDED_TOOLS)}")
    assert policies_for(agent.name), (
        f"agent '{agent.name}' ({agent.path}) holds {list(agent.guarded_tools)} but no "
        f"PreToolUse entry in {SETTINGS_PATH} names it in SCOPE_AGENT_TYPES, so nothing "
        f"fences it. {WHY_FRONTMATTER_HOOKS_ARE_BANNED}"
    )


@pytest.mark.parametrize("agent", AGENTS, ids=AGENT_IDS)
def test_policy_for_a_capable_agent_constrains_something(agent: AgentFile) -> None:
    """A scoped entry that sets no glob or command list is an inert guard.

    It is wired, it runs, and it exits 0 for everything -- from outside,
    identical to a working guard. That is the same silent-success failure mode
    as the frontmatter wiring, one level down.
    """
    if not agent.needs_policy:
        pytest.skip(f"{agent.name} holds none of {sorted(GUARDED_TOOLS)}")
    scoped = policies_for(agent.name)
    if not scoped:
        pytest.skip("no policy at all; reported by the coverage test above")
    constrained = [
        command
        for command in scoped
        if any(command.env.get(var, "").strip() for var in CONSTRAINT_VARS)
    ]
    assert constrained, (
        f"every PreToolUse policy scoped to '{agent.name}' sets none of "
        f"{list(CONSTRAINT_VARS)}, so the guard runs and allows everything. Both guard "
        "scripts exit 0 when unconfigured, which nothing outside this file can tell "
        "apart from a guard that is working."
    )


@pytest.mark.parametrize("agent", AGENTS, ids=AGENT_IDS)
def test_unscoped_agent_has_no_write_or_exec_tools(agent: AgentFile) -> None:
    """The contrapositive of the coverage rule, stated over the agent list.

    Read-only agents (`tools:` with no Edit/Write/Bash) legitimately need no
    entry. An agent that gains Bash or Edit without gaining a policy fails here,
    and this failure names the tools it just acquired.
    """
    if policies_for(agent.name):
        pytest.skip(f"{agent.name} is named by at least one policy")
    assert not agent.guarded_tools, (
        f"agent '{agent.name}' ({agent.path}) holds {list(agent.guarded_tools)} and is "
        f"named by no policy in {SETTINGS_PATH}. Only agents whose tools are limited to "
        "reading (Read/Grep/Glob) may go unfenced."
    )


@pytest.mark.parametrize("command", HOOK_COMMANDS, ids=HOOK_IDS)
def test_policy_with_constraints_names_the_agents_it_applies_to(command: HookCommand) -> None:
    """Settings hooks are session-wide, so an unscoped policy polices everyone.

    That includes the top-level session and every other subagent, none of which
    the policy was written for.
    """
    constraints = [var for var in CONSTRAINT_VARS if command.env.get(var, "").strip()]
    if not constraints:
        pytest.skip("policy sets no path or command constraints")
    assert command.env.get("SCOPE_AGENT_TYPES", "").strip(), (
        f"the PreToolUse policy matching {command.matcher!r} sets {constraints} but no "
        "SCOPE_AGENT_TYPES. Hooks in settings.json fire for every caller in the session, "
        "so this policy would also police the top-level session and every other agent. "
        "Add SCOPE_AGENT_TYPES='<agent name>' to the command line.\n"
        f"command: {command.command}"
    )


@pytest.mark.parametrize("command", HOOK_COMMANDS, ids=HOOK_IDS)
def test_hook_command_runs_a_script_that_exists(command: HookCommand) -> None:
    assert command.script, f"no script found on hook command line: {command.command}"
    script = command.script_path
    assert script.is_file(), (
        f"the PreToolUse policy matching {command.matcher!r} runs {script}, which does not "
        "exist. A hook whose command cannot be executed is not a guard.\n"
        f"command: {command.command}"
    )
    assert script.parent == HOOKS_DIR, (
        f"the PreToolUse policy matching {command.matcher!r} runs {script}, which is "
        f"outside {HOOKS_DIR}. The guard scripts live there and are reviewed there."
    )


@pytest.mark.parametrize("command", HOOK_COMMANDS, ids=HOOK_IDS)
def test_hook_script_is_executable(command: HookCommand) -> None:
    script = command.script_path
    if not script.is_file():
        pytest.skip("missing script; reported by the existence test above")
    assert os.access(script, os.X_OK), (
        f"{script} is not executable, so the hook cannot run it. Restore the executable "
        "bit (git tracks it) -- a hook that fails to start is a guard that is not there."
    )


@pytest.mark.parametrize("command", HOOK_COMMANDS, ids=HOOK_IDS)
def test_scope_agent_types_names_known_agents(command: HookCommand) -> None:
    """A typo in SCOPE_AGENT_TYPES silently disables the policy it names.

    Scoping is fail-open by design -- an unrecognised caller means "not my
    policy", never "deny" -- so a misspelled agent name matches nobody and the
    guard quietly does nothing.
    """
    known = {agent.name for agent in AGENTS}
    unknown = sorted(command.scoped_agents - known)
    assert not unknown, (
        f"the PreToolUse policy matching {command.matcher!r} is scoped to {unknown}, which "
        f"match no agent under {AGENTS_DIR} (known: {sorted(known)}). Scoping is fail-open, "
        "so a name that matches nothing leaves this policy inert."
    )


# --- the coverage this repo is expected to have after issue #102 ---------

EXPECTED_POLICIES = [
    ("coder", frozenset({"Edit", "Write"}), "DENY_GLOBS", "path-guard.sh"),
    ("architect", frozenset({"Edit", "Write"}), "ALLOW_GLOBS", "path-guard.sh"),
    ("test-author", frozenset({"Edit", "Write"}), "ALLOW_GLOBS", "path-guard.sh"),
    ("test-author", frozenset({"Read", "Grep", "Glob"}), "DENY_GLOBS", "path-guard.sh"),
    ("test-author", frozenset({"Read", "Grep", "Glob"}), "EXEMPT_GLOBS", "path-guard.sh"),
    ("security-auditor", frozenset({"Bash"}), "ALLOW_CMDS", "bash-guard.sh"),
    # ADR-0018 decision 14: the coder's Bash policy.
    ("coder", frozenset({"Bash"}), "ALLOW_CMDS", "bash-guard.sh"),
]


@pytest.mark.parametrize(
    ("agent_name", "tools", "variable", "script_name"),
    EXPECTED_POLICIES,
    ids=[
        f"{agent_name}-{'|'.join(sorted(tools))}-{variable}"
        for agent_name, tools, variable, _ in EXPECTED_POLICIES
    ],
)
def test_expected_policy_is_present(
    agent_name: str, tools: frozenset[str], variable: str, script_name: str
) -> None:
    """Pin the specific fences this repo relies on.

    The derived tests above catch an agent fenced by nothing. This one catches a
    fence quietly replaced by a different, weaker one: a denylist turning into
    an allowlist, the read guard disappearing while the write guard stays, or a
    policy losing the variable that carries its scope.
    """
    matching = [
        command
        for command in HOOK_COMMANDS
        if agent_name in command.scoped_agents
        and tools <= command.matched_tools
        and Path(command.script).name == script_name
        and command.env.get(variable, "").strip()
    ]
    present = [
        f"{sorted(command.scoped_agents)} {command.matcher} {Path(command.script).name}"
        for command in HOOK_COMMANDS
    ]
    assert matching, (
        f"expected a PreToolUse policy in {SETTINGS_PATH} scoped to '{agent_name}', "
        f"matching {sorted(tools)}, running {script_name} and setting a non-empty "
        f"{variable}; found none.\npolicies present: {present}"
    )


# --- ADR-0018: bash-guard.sh policies and the coder's fences -------------

BASH_GUARD = "bash-guard.sh"
PATH_GUARD = "path-guard.sh"

# Decision 14's coder Bash policy, knob by knob. Compared as sets of words.
ADR_0018_CODER_BASH_POLICY = {
    "ALLOW_CMDS": "ls cat head tail wc stat find grep rg jq diff cmp pwd git uv make",
    "ALLOW_GIT_SUBCMDS": "status diff log show rev-parse ls-files add commit merge",
    "ALLOW_UV_RUN_TARGETS": "pytest ruff",
    "ALLOW_MAKE_TARGETS": "typecheck",
    "DENY_ADVICE": "stop-and-report",
    "LITERAL_ONLY": "1",
}

# Decision 12's glob list: the coder's Edit/Write DENY_GLOBS after step W.
ADR_0018_DECISION_12_GLOBS = frozenset(
    [
        "tests/*",
        "*/tests/*",
        "docs/spec/*",
        "docs/adr/*",
        "docs/protocol/*",
        "schemas/*",
        ".claude/*",
        "CLAUDE.md",
        "*/CLAUDE.md",
        "CLAUDE.local.md",
        "*/CLAUDE.local.md",
        ".mcp.json",
        "/*",
        "../*",
        "*/../*",
        ".git",
        ".git/*",
        "*/.git",
        "*/.git/*",
        ".venv/*",
        "*/.venv/*",
        "__pycache__/*",
        "*/__pycache__/*",
        "conftest.py",
        "*/conftest.py",
        "test_*.py",
        "*/test_*.py",
        "*_test.py",
        "test*.txt",
        "*/test*.txt",
        "pytest.toml",
        "*/pytest.toml",
        ".pytest.toml",
        "*/.pytest.toml",
        "pytest.ini",
        "*/pytest.ini",
        ".pytest.ini",
        "*/.pytest.ini",
        "tox.ini",
        "*/tox.ini",
        "setup.cfg",
        "*/setup.cfg",
        "mypy.ini",
        "*/mypy.ini",
        ".mypy.ini",
        "*/.mypy.ini",
        ".ruff.toml",
        "*/.ruff.toml",
        "*/ruff.toml",
        "uv.toml",
        "*/uv.toml",
        ".python-version",
        "*/.python-version",
        "sitecustomize.py",
        "*/sitecustomize.py",
        "usercustomize.py",
        "*/usercustomize.py",
        "pytest",
        "pytest/*",
        "ruff",
        "ruff/*",
        "mypy",
        "mypy/*",
        "GNUmakefile",
        "makefile",
        "uv.lock",
    ]
)

# Commands that no bash-guard.sh policy may list: interpreters, launchers,
# network clients, container tooling and file writers (brief T1, group L).
FORBIDDEN_BASH_COMMANDS = frozenset(
    [
        "python",
        "python3",
        "pip",
        "pip3",
        "uvx",
        "sh",
        "bash",
        "zsh",
        "dash",
        "env",
        "xargs",
        "awk",
        "perl",
        "ruby",
        "curl",
        "wget",
        "nc",
        "ssh",
        "scp",
        "rsync",
        "docker",
        "npm",
        "npx",
        "tee",
        "cp",
        "mv",
        "rm",
        "chmod",
        "ln",
        "dd",
        "cd",
        "timeout",
    ]
)

# Settings that turn on a rule only literal mode may run (decision 3).
LITERAL_ONLY_COMMANDS = frozenset({"uv", "make"})
LITERAL_ONLY_GIT_SUBCMDS = frozenset({"add", "commit", "merge"})


def bash_guard_policies() -> list[HookCommand]:
    return [command for command in HOOK_COMMANDS if Path(command.script).name == BASH_GUARD]


def words(command: HookCommand, variable: str) -> frozenset[str]:
    return frozenset(command.env.get(variable, "").split())


def coder_policy(tool: str, script_name: str) -> HookCommand:
    matching = [
        command
        for command in HOOK_COMMANDS
        if "coder" in command.scoped_agents
        and tool in command.matched_tools
        and Path(command.script).name == script_name
    ]
    assert len(matching) == 1, (
        f"expected exactly one PreToolUse policy in {SETTINGS_PATH} scoped to 'coder', "
        f"matching {tool} and running {script_name} (ADR-0018 decision 14); found "
        f"{len(matching)}"
    )
    return matching[0]


def test_bash_guard_policies_are_found() -> None:
    """The bash-guard invariants below must not pass vacuously."""
    assert bash_guard_policies(), f"no PreToolUse policy in {SETTINGS_PATH} runs {BASH_GUARD}"


def test_bash_policy_for_an_agent_that_writes_is_literal() -> None:
    """ADR-0018 decision 3: a Bash policy fencing an agent with Edit or Write
    must set LITERAL_ONLY='1'. Without it, a written file named like an option
    reaches a tool through a glob (Context item 2)."""
    writers = {agent.name for agent in AGENTS if {"Edit", "Write"} & set(agent.tools)}
    offenders = [
        f"{sorted(command.scoped_agents)}: {command.command}"
        for command in bash_guard_policies()
        if command.scoped_agents & writers and command.env.get("LITERAL_ONLY") != "1"
    ]
    assert not offenders, (
        "these bash-guard.sh policies fence an agent that can Edit or Write but do not "
        f"set LITERAL_ONLY='1' (ADR-0018 decision 3): {offenders}"
    )


def test_bash_policy_enabling_a_literal_only_rule_is_literal() -> None:
    """ADR-0018 decision 3: every uv and make rule, and git add/commit/merge,
    run only in literal mode."""
    offenders = [
        command.command
        for command in bash_guard_policies()
        if (
            words(command, "ALLOW_CMDS") & LITERAL_ONLY_COMMANDS
            or words(command, "ALLOW_GIT_SUBCMDS") & LITERAL_ONLY_GIT_SUBCMDS
        )
        and command.env.get("LITERAL_ONLY") != "1"
    ]
    assert not offenders, (
        "these bash-guard.sh policies allow uv, make, or git add/commit/merge without "
        f"LITERAL_ONLY='1' (ADR-0018 decision 3): {offenders}"
    )


def test_no_bash_policy_allows_an_interpreter_or_a_writer() -> None:
    """ADR-0018 decision 4: listing any of these voids the guard."""
    offenders = {
        command.command: sorted(words(command, "ALLOW_CMDS") & FORBIDDEN_BASH_COMMANDS)
        for command in bash_guard_policies()
        if words(command, "ALLOW_CMDS") & FORBIDDEN_BASH_COMMANDS
    }
    assert not offenders, f"bash-guard.sh policies list forbidden commands: {offenders}"


@pytest.mark.parametrize("variable", sorted(ADR_0018_CODER_BASH_POLICY))
def test_coder_bash_policy_has_the_adr_0018_values(variable: str) -> None:
    command = coder_policy("Bash", BASH_GUARD)
    expected = frozenset(ADR_0018_CODER_BASH_POLICY[variable].split())
    assert words(command, variable) == expected, (
        f"the coder's Bash policy sets {variable}={command.env.get(variable)!r}; ADR-0018 "
        f"decision 14 says {ADR_0018_CODER_BASH_POLICY[variable]!r}"
    )


def test_coder_write_deny_globs_equal_its_edit_write_deny_globs() -> None:
    """ADR-0018 decision 7: a write launched from Bash respects the same fence
    as a Write, so the two copies of the list must be equal."""
    bash = words(coder_policy("Bash", BASH_GUARD), "WRITE_DENY_GLOBS")
    edit_write = words(coder_policy("Write", PATH_GUARD), "DENY_GLOBS")
    assert bash, "the coder's Bash policy sets no WRITE_DENY_GLOBS"
    assert bash == edit_write, (
        "the coder's Bash WRITE_DENY_GLOBS and Edit|Write DENY_GLOBS differ.\n"
        f"only in WRITE_DENY_GLOBS: {sorted(bash - edit_write)}\n"
        f"only in DENY_GLOBS: {sorted(edit_write - bash)}"
    )


def test_coder_edit_write_deny_globs_cover_decision_12() -> None:
    command = coder_policy("Write", PATH_GUARD)
    assert {"Edit", "Write"} <= command.matched_tools, command.matcher
    missing = ADR_0018_DECISION_12_GLOBS - words(command, "DENY_GLOBS")
    assert not missing, (
        f"the coder's Edit|Write DENY_GLOBS lacks ADR-0018 decision 12's {sorted(missing)}"
    )
    assert ".claude/*" in words(command, "DENY_GLOBS")


# --- ADR-0018's fifth amendment: PATH_ROOT and decision 22's lists ---------
#
# Brief T4, item 6. Every list is compared as a set of words.

# Decision 20: the root each agent's path-guard.sh policies name.
EXPECTED_PATH_ROOTS = {"coder": "cwd", "architect": "project", "test-author": "project"}

# Decision 22's agent-configuration list, which every Edit/Write policy carries.
AGENT_CONFIGURATION_GLOBS = frozenset(
    [
        ".claude",
        ".claude/*",
        "*/.claude",
        "*/.claude/*",
        "CLAUDE.md",
        "*/CLAUDE.md",
        "CLAUDE.local.md",
        "*/CLAUDE.local.md",
        ".mcp.json",
        "*/.mcp.json",
    ]
)

# Decision 22: the test-author's Edit/Write allowlist, exactly.
TEST_AUTHOR_WRITE_ALLOW_GLOBS = frozenset(
    [
        "tests/*",
        "packages/*/tests/*",
        "services/*/tests/*",
        "tools/*/tests/*",
        "packages/hammertime-testkit/*",
    ]
)

# Decision 22: git's internals and the ignored executed state of decision 12 (b),
# which the test-author's Edit/Write deny list adds to the agent-configuration list.
TEST_AUTHOR_WRITE_GIT_AND_IGNORED_GLOBS = frozenset(
    [
        ".git",
        ".git/*",
        "*/.git",
        "*/.git/*",
        ".venv/*",
        "*/.venv/*",
        "__pycache__/*",
        "*/__pycache__/*",
    ]
)

# Decision 22: the test-author's read exemptions, exactly.
TEST_AUTHOR_READ_EXEMPT_GLOBS = frozenset(
    [
        "tests",
        "tests/*",
        "packages/*/tests",
        "packages/*/tests/*",
        "services/*/tests",
        "services/*/tests/*",
        "tools/*/tests",
        "tools/*/tests/*",
        "packages/hammertime-testkit",
        "packages/hammertime-testkit/*",
    ]
)

# Decision 22 (brief T4): what the test-author's read DENY_GLOBS must contain.
TEST_AUTHOR_READ_DENY_GLOBS = frozenset(
    [
        "packages",
        "packages/*",
        "services",
        "services/*",
        "tools",
        "tools/*",
        ".claude",
        ".claude/*",
        ".mypy_cache",
        ".mypy_cache/*",
        "build",
        "build/*",
        "dist",
        "dist/*",
        "htmlcov",
        "htmlcov/*",
    ]
)

# Decision 14: the architect's Edit/Write allowlist, unchanged by decision 22.
ARCHITECT_WRITE_ALLOW_GLOBS = frozenset(["docs/*", "schemas/*", "README.md"])

EDIT_WRITE = frozenset({"Edit", "Write"})
READ_GREP_GLOB = frozenset({"Read", "Grep", "Glob"})


def path_guard_policies() -> list[HookCommand]:
    return [command for command in HOOK_COMMANDS if Path(command.script).name == PATH_GUARD]


def path_guard_policy(agent_name: str, tools: frozenset[str]) -> HookCommand:
    matching = [
        command
        for command in path_guard_policies()
        if agent_name in command.scoped_agents and tools <= command.matched_tools
    ]
    assert len(matching) == 1, (
        f"expected exactly one PreToolUse policy in {SETTINGS_PATH} scoped to "
        f"'{agent_name}', matching {sorted(tools)} and running {PATH_GUARD} (ADR-0018 "
        f"decision 14); found {len(matching)}"
    )
    return matching[0]


def test_path_guard_policies_are_found() -> None:
    """The path-guard invariants below must not pass vacuously."""
    assert path_guard_policies(), f"no PreToolUse policy in {SETTINGS_PATH} runs {PATH_GUARD}"


def test_every_path_guard_policy_sets_its_path_root() -> None:
    """ADR-0018 decisions 14 and 20: every path-guard.sh policy names its root,
    `cwd` for the coder, whose root is its worktree, and `project` for the
    architect and the test-author. An unset PATH_ROOT keeps the two old bases,
    and a policy scoped to agents with different roots cannot name one."""
    offenders = []
    for command in path_guard_policies():
        roots = {
            EXPECTED_PATH_ROOTS[agent]
            for agent in command.scoped_agents
            if agent in EXPECTED_PATH_ROOTS
        }
        value = command.env.get("PATH_ROOT")
        valid = value in {"cwd", "project"} and (not roots or roots == {value})
        if not valid:
            offenders.append(f"PATH_ROOT={value!r}, expected {sorted(roots)}: {command.command}")
    assert not offenders, (
        "these path-guard.sh policies do not set the PATH_ROOT ADR-0018 decision 20 "
        f"gives them: {offenders}"
    )


def test_no_path_guard_allow_or_exempt_glob_begins_with_a_star() -> None:
    """ADR-0018 decisions 20 and 22: an allow or exempt glob that begins with `*`
    matches paths it was never written for, in any directory (gap G1)."""
    offenders = []
    for command in path_guard_policies():
        for variable in ("ALLOW_GLOBS", "EXEMPT_GLOBS"):
            starred = sorted(glob for glob in words(command, variable) if glob.startswith("*"))
            if starred:
                offenders.append(f"{variable} {starred}: {command.command}")
    assert not offenders, (
        f"these path-guard.sh policies allow or exempt a glob beginning with '*': {offenders}"
    )


def test_every_edit_write_path_guard_policy_denies_agent_configuration() -> None:
    """ADR-0018 decision 22: every Edit/Write policy's DENY_GLOBS carries the
    agent-configuration list, so no agent writes a `.claude/`, a `CLAUDE.md` or
    a `.mcp.json` at any depth."""
    policies = [command for command in path_guard_policies() if EDIT_WRITE & command.matched_tools]
    assert policies, f"no Edit/Write policy in {SETTINGS_PATH} runs {PATH_GUARD}"
    offenders = {
        command.command: sorted(AGENT_CONFIGURATION_GLOBS - words(command, "DENY_GLOBS"))
        for command in policies
        if AGENT_CONFIGURATION_GLOBS - words(command, "DENY_GLOBS")
    }
    assert not offenders, (
        f"these Edit/Write policies' DENY_GLOBS lack decision 22's globs: {offenders}"
    )


def test_test_author_write_allow_globs_are_decision_22s() -> None:
    command = path_guard_policy("test-author", EDIT_WRITE)
    allow = words(command, "ALLOW_GLOBS")
    assert allow == TEST_AUTHOR_WRITE_ALLOW_GLOBS, (
        "the test-author's Edit|Write ALLOW_GLOBS differ from ADR-0018 decision 22's.\n"
        f"missing: {sorted(TEST_AUTHOR_WRITE_ALLOW_GLOBS - allow)}\n"
        f"extra: {sorted(allow - TEST_AUTHOR_WRITE_ALLOW_GLOBS)}"
    )


def test_test_author_write_deny_globs_cover_decision_22() -> None:
    command = path_guard_policy("test-author", EDIT_WRITE)
    required = AGENT_CONFIGURATION_GLOBS | TEST_AUTHOR_WRITE_GIT_AND_IGNORED_GLOBS
    missing = required - words(command, "DENY_GLOBS")
    assert not missing, (
        f"the test-author's Edit|Write DENY_GLOBS lack ADR-0018 decision 22's {sorted(missing)}"
    )


def test_test_author_read_exempt_globs_are_decision_22s() -> None:
    command = path_guard_policy("test-author", READ_GREP_GLOB)
    exempt = words(command, "EXEMPT_GLOBS")
    assert exempt == TEST_AUTHOR_READ_EXEMPT_GLOBS, (
        "the test-author's Read|Grep|Glob EXEMPT_GLOBS differ from ADR-0018 decision 22's.\n"
        f"missing: {sorted(TEST_AUTHOR_READ_EXEMPT_GLOBS - exempt)}\n"
        f"extra: {sorted(exempt - TEST_AUTHOR_READ_EXEMPT_GLOBS)}"
    )


def test_test_author_read_deny_globs_cover_decision_22() -> None:
    command = path_guard_policy("test-author", READ_GREP_GLOB)
    missing = TEST_AUTHOR_READ_DENY_GLOBS - words(command, "DENY_GLOBS")
    assert not missing, (
        f"the test-author's Read|Grep|Glob DENY_GLOBS lack decision 22's {sorted(missing)}"
    )


def test_architect_write_allow_globs_are_unchanged() -> None:
    command = path_guard_policy("architect", EDIT_WRITE)
    allow = words(command, "ALLOW_GLOBS")
    assert allow == ARCHITECT_WRITE_ALLOW_GLOBS, (
        f"the architect's Edit|Write ALLOW_GLOBS are {sorted(allow)}; ADR-0018 decisions 14 "
        f"and 22 keep them {sorted(ARCHITECT_WRITE_ALLOW_GLOBS)}"
    )
