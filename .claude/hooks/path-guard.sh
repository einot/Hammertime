#!/usr/bin/env bash
# Generic PreToolUse path guard shared by Hammertime's project subagents
# (.claude/agents/*.md). One hook entry carries one agent's policy, set as
# env vars inline on the hook's own command line, so the same script is
# parametrized per-agent instead of duplicating the logic five times. See
# WIRING below for where that entry goes -- it is not the agent file.
#
# Env vars. The three glob lists are space-separated and use
# `[[ str == pattern ]]` semantics, so a bare `*` in a pattern matches
# across `/` too — "docs/*" matches "docs/spec/hammertime_spec_1.md":
#
#   EXEMPT_GLOBS      - path matching any of these is always ALLOWED,
#                       checked before DENY_GLOBS/ALLOW_GLOBS.
#   DENY_GLOBS        - path matching any of these (and not exempt) is
#                       DENIED.
#   ALLOW_GLOBS       - if set, a path that is not exempt/already-denied
#                       must match at least one of these or it is DENIED
#                       (allowlist mode). Leave unset for denylist-only
#                       mode.
#   SCOPE_AGENT_TYPES - agent types this policy applies to, matched
#                       against the payload's `agent_type`. A list of
#                       exact names, not globs. Unset or empty polices
#                       every call that reaches this hook (the historical
#                       behaviour). Set polices only the listed agents and
#                       passes every other caller through untouched. See
#                       WIRING below -- this exists because the hook has
#                       to be installed session-wide.
#   PATH_ROOT         - the root every guarded path must lie inside, and
#                       that the glob lists are written relative to (see
#                       ROOT below). `project` is CLAUDE_PROJECT_DIR, `cwd`
#                       is the payload's cwd, and unset or empty keeps the
#                       two old bases, cwd then CLAUDE_PROJECT_DIR. Any
#                       other value is a configuration error that refuses
#                       every in-scope call.
#
# Reads the PreToolUse JSON payload on stdin (see
# https://code.claude.com/docs/en/hooks) and checks the path in the tool's
# own field: tool_input.file_path for a Read, Edit or Write, and
# tool_input.path for a Grep or Glob (see FIELDS below). A payload the
# guard cannot read is refused first of all, for every caller (see A GUARD
# THAT FAILS DENIES below). For an in-scope call under a guarded policy,
# another tool, or a call that also names a path in the other kind's field,
# is refused first (see FIELDS); then a path containing a NUL byte, or a
# path field that is not a string (see NUL GATE below), a path that ends
# with a newline (see TRAILING NEWLINE below), and a path that begins or
# ends with whitespace or a control character (see EDGE CHARACTERS
# below); then a Glob pattern or
# Grep glob outside a narrow grammar (see SEARCH PATTERNS below), and a
# search value that begins with `-` or a pattern component that could
# match `..` (see SEARCH VALUES below). A path that is not in plain form is
# then refused, and after it a path that does not lie inside the policy's
# root, and last a search path with a character outside a narrow grammar,
# before any glob list is consulted (see PLAIN FORM, ROOT and SEARCH
# VALUES below).
#
# A GUARD THAT FAILS DENIES (ADR-0018 decision 19, fifth amendment; parts 1
# and 3 amended, and parts 4 and 5 added, by the seventh). This script
# ends with status 0 (allow, with no output) or 2 (deny) and nothing else.
# Before this, any command that failed under `set -e` -- an extraction line
# whose jq could not read the payload, or the jq inside `deny` -- ended the
# script with some other status, which the harness treats as a
# non-blocking hook error, so the call went through. Five parts, built in
# for every policy, with no knob:
#
#   FAIL-CLOSED EXIT. An EXIT trap, installed as the first command after
#   `set -f -e -u -o pipefail`, turns any exit status other than 0, and
#   other than the 2 of `deny`'s own `exit 2`, into the backstop denial
#   ("... the guard stopped with status N before reaching a verdict ...")
#   and exit 2. `deny` sets a flag immediately before its `exit 2`, after
#   its jq or its fallback, so a command that fails with status 2 anywhere
#   else gets the backstop, not a refusal with no reason. The trap writes
#   the JSON deny with printf and a fixed template, never with jq, which
#   may be what failed.
#
#   DENY'S FALLBACK. `deny` still writes the JSON deny with `jq -n` and
#   exits 2. If that jq fails, it writes the same reason to stderr instead,
#   and still exits 2, which blocks whether or not JSON is printed.
#
#   PAYLOAD SHAPE. The payload is read with `head -c 8388609`, so at most
#   its first 8 MiB and one byte (decision 25), through `tr`, which keeps
#   each NUL byte as U+0002, a control character no JSON text holds raw;
#   the rest of stdin is then read and discarded. So the command
#   substitution drops nothing unseen, and a payload that holds a raw NUL
#   is not judged with the byte removed. Between reading stdin and the
#   first extraction line, one `jq -e -s` call over the payload asks
#   whether it is malformed. A payload that holds a raw U+0002, whatever
#   put it there, is malformed without that call, with status 0. It is
#   well formed when it is exactly one JSON value, that value is an object,
#   its tool_input is an object, its tool_name is a string, and its cwd and
#   agent_type are each a string, null or absent. A payload longer than
#   the bound is cut there, so it is not one JSON value unless the cut
#   falls after one whole value and nothing but whitespace. Only status 1
#   (a clean false: well formed) passes; status 0 (malformed) and any other
#   status (not JSON, or jq failed) get the shape denial ("... could not be
#   read as a single tool call ..."), which names the status and goes
#   through `deny`. This check runs BEFORE the SCOPE_AGENT_TYPES routing,
#   for every caller the hook sees, the top-level session included, and
#   under a policy that constrains nothing: the routing reads agent_type
#   through an extraction line, so a script that cannot read the payload
#   cannot tell whether the caller is in scope. `deny` is therefore defined
#   above the extraction lines. A field inside tool_input is not this
#   check's business: a path that is not a string still meets the NUL
#   gate's could-not-be-checked denial. Once the shape passes, no
#   extraction line can fail on the payload, only through the environment.
#
#   EXTRACTION CHECK. Each extraction line reads its field through a
#   command substitution, which bash may expand to nothing when it cannot
#   make its pipe, and which drops NUL bytes and trailing newlines.
#   Directly after the four extraction lines, and so also before the
#   routing, for every caller, one `jq -e` call over the payload, told only
#   which extracted fields are empty, asks whether each is empty exactly
#   when the payload's field, as `$(...)` renders it, is empty; and whether
#   cwd, when it is a string, ends with a newline or holds a NUL, which
#   `$(...)` would read as another directory. Only status 1 passes; any
#   other status ends the script with status 3, which the trap turns into
#   the backstop denial.
#
#   ONE TOP-LEVEL COMMAND. Every command after the trap is inside one brace
#   group, which ends every path through it with `exit`, and the script's
#   last line, after the group, is `exit 3`. If bash abandoned a command
#   part-way rather than exiting, only `exit 3` would be left to run, and
#   the trap turns it into the backstop denial; so no path reaches exit 0
#   with a check before it skipped.
#
#   The cost: a missing or broken jq now refuses every call this hook sees,
#   the top-level session's included, until jq is restored from outside
#   the session. A guard process that never finishes (killed by a signal),
#   or a hook command that cannot start, is outside what the script can do.
#   The bound on the payload keeps this script's work a fixed number of
#   passes over at most 8 MiB, well inside the hook's time limit.
#
# FIELDS (ADR-0018 decision 24, seventh amendment).
#
# The path is read from its tool's own field, through one selector that the
# extraction line, the extraction check, the NUL gate and the
# trailing-newline test share: tool_input.file_path for a Read, Edit or
# Write, tool_input.path for a Grep or Glob, and nothing for any other
# tool. Before, the extraction took tool_input.file_path falling back to
# tool_input.path whatever the tool, so a Grep or Glob that carried a
# file_path beside its path was vetted on the file_path while the tool
# searched the path. Under a guarded policy, for an in-scope call, the
# tool must be one of those five, and the other kind's field
# (tool_input.path for a Read, Edit or Write, tool_input.file_path for a
# Grep or Glob) must be absent, null or false; the empty string counts as
# a field. Anything else is refused with the fields denial ("... cannot
# tell which path the tool would act on ..."). The check runs after the
# routing, the PATH_ROOT check and `guarded`, and before the NUL gate, so
# before every other check that reads the path. It reads jq's exit status;
# only status 1 passes. There is no knob to turn it off.
#
# NUL GATE (ADR-0018 decision 17, third amendment).
#
# The path is read into file_path through a command substitution, and
# bash's command substitution silently drops NUL bytes. So `uv.lock`, a
# NUL, then `.py` would be vetted as `uv.lock.py`, and a path, a NUL, then
# `/tests/x` as a path inside a tests directory, while a harness that acted
# on the bytes before the NUL would use a different path from the one
# vetted. The gate therefore does not trust file_path: it asks jq, over the
# raw payload, whether the DECODED path -- the very value the extraction
# selects, the tool's own field by decision 24's selector (see FIELDS) --
# contains codepoint 0, and reads the answer from jq's exit status rather
# than from any captured string.
#
# It runs for every in-scope call under a guarded policy (DENY_GLOBS or
# ALLOW_GLOBS set), whatever the tool: it does not look at tool_name. It
# sits after the SCOPE_AGENT_TYPES routing, so an out-of-scope caller
# still passes through untouched, and it does not run under a policy that
# constrains no paths, which still denies nothing. It sits before the
# empty-path check and every glob list, EXEMPT_GLOBS included.
#
# Status rule: only status 1 (`jq -e` on a clean `false`: a string with no
# NUL, the empty string included) passes. Status 0 (a NUL was found) is
# refused with the NUL denial. Any other status means the check did not
# complete -- for example the error `explode` raises on a path field that
# is `true`, a number, an array or an object -- and is refused with the
# could-not-be-checked denial, which names the status. An absent, null or
# false path becomes the empty string through `// ""`, passes, and meets
# the empty-path check as before. There is no knob to turn the gate off.
#
# TRAILING NEWLINE (ADR-0018 decision 17, seventh amendment).
#
# `$(...)` also drops trailing newlines, so a path that ends with one would
# be vetted without it: the project root followed by a newline would be
# vetted as the root, and a path of newlines only as no path, while the
# tool would act on a name that ends in a newline. Under a guarded policy,
# directly after the NUL gate, and so before the empty-path and
# project-root checks, jq asks over the raw payload whether the path, the
# tool's own field, ends with a newline (U+000A). Only status 1 passes;
# status 0, and any other status, get the trailing-newline denial ("the
# path ends with a newline, or could not be checked for one ..."). A path
# with both a NUL and a trailing newline keeps the NUL denial. A newline
# inside a path is carried intact and judged as written. Decision 26's
# check (EDGE CHARACTERS) sits directly after this test. There is no knob.
#
# EDGE CHARACTERS (ADR-0018 decision 26, eighth amendment).
#
# The path is judged exactly as written, so a path that begins with a
# space does not begin with `/`, is judged as a relative path and matches
# no glob anchored at the root, and a path that ends with a space matches
# no exact-name glob such as `*/CLAUDE.md`. A tool that trimmed its path
# after the hook approved it would act on a path no list judged. So under
# a guarded policy, for an in-scope call, whatever the tool, a path (the
# tool's own field) whose first or last character is an edge character is
# refused with the edge denial ("the path begins or ends with whitespace
# or a control character, or could not be checked for one ..."). An edge
# character is U+0000-U+0020 (the C0 controls and the space), U+007F-U+00A0
# (DEL, the C1 controls and the no-break space), U+1680, U+180E,
# U+2000-U+200A, U+2028, U+2029, U+202F, U+205F, U+3000 or U+FEFF: every
# character a common trimming routine removes, and every control
# character. A character inside a path is judged as written. The check is
# one jq call over the raw payload, read from its exit status; only status
# 1 passes. An absent, null or false path becomes "" and passes, to meet
# the empty-path check as before. It sits directly after the
# trailing-newline test, so before the search checks, the empty-path
# check, the relativisation, the project-root check, the plain-form rule,
# the root rule and every glob list; a path with a NUL keeps the NUL
# denial, and one that ends with a newline the trailing-newline denial.
# There is no knob.
#
# PLAIN FORM (ADR-0018 decision 18, fourth amendment).
#
# The globs are matched against the path exactly as written. The script
# resolves no `.` or `..` and expands no `~`: the relativisation below only
# strips a root prefix. So `tests/../packages/x.py` would match `tests/*`
# while the kernel opened `packages/x.py`. The rule therefore refuses,
# rather than normalises, any path that is not in plain form, which is any
# path that:
#
#   1. has a `/`-separated component that is exactly `..`;
#   2. has a component that is exactly `.`;
#   3. contains `//` anywhere;
#   4. begins with `~`, or has a component that begins with `~` (seventh
#      amendment: a `~` after any `/`, such as `<repo>/~x`).
#
# Only a component that is exactly `.` or `..` counts: `.git`, `..foo`,
# `x..y` and `...` are ordinary names. A leading `/` and a single trailing
# `/` are plain. Relative paths are judged by the same four tests, so the
# rule needs no base and reads neither cwd nor CLAUDE_PROJECT_DIR.
#
# It runs for every in-scope call under a guarded policy, whatever the
# tool: it does not look at tool_name. It sits after the NUL gate, the
# trailing-newline test, the edge-character check, the search checks
# before the empty-path check,
# the empty-path check, the relativisation and the project-root check, and
# before every glob list, EXEMPT_GLOBS included. The root's own spellings
# (`<root>`, `<root>/`, `<root>/.`, `<root>/./`, `.` and `./`) are handled
# by the project-root check as before and never reach the rule. There is
# no knob to turn it off.
#
# Symlinks are not handled: a path in plain form can still name, through
# a symlink among its components, a file the globs never see. The guard
# matches strings and does not touch the filesystem (ADR-0018 decision 18,
# Question 5).
#
# SEARCH PATTERNS (ADR-0018 decision 21, fifth amendment).
#
# The path of a Grep or a Glob is vetted, but the pattern it applies under
# that path could name files outside it: a Glob of the exempt `tests` with
# the pattern `../../packages/**/*.py`. The value judged is
# tool_input.pattern when tool_name is Glob, and tool_input.glob when it is
# Grep; any other tool has none, and Grep's `pattern`, a regular
# expression, is not read. It passes when it is absent, null or false, or
# a string that:
#
#   1. contains only ASCII letters, digits and the characters _ - . / * ?;
#   2. does not begin with `/`;
#   3. does not contain `..` anywhere.
#
# Anything else -- braces, ranges, negation, a leading `~`, a NUL, a
# newline, or a value that is not a string -- is refused with the pattern
# denial ("... a Glob pattern or a Grep glob may contain only ..."). Such a
# pattern can only name paths under the searched path, in any engine that
# separates on `/` and goes up only by `..`, and it has no expansion to
# build a `..` from. The check runs entirely in jq over the raw payload and
# is read from jq's exit status; only status 1 passes.
#
# It runs for every in-scope call under a guarded policy, after the NUL
# gate, the trailing-newline test and the edge-character check, and before
# the empty-path check and every glob list, EXEMPT_GLOBS included. There
# is no knob to turn it off.
# One exception to "only paths under the searched path": an engine that
# lists a directory's `.` and `..` entries matches a component such as
# `.?` against `..`; SEARCH VALUES refuses such a component.
#
# SEARCH VALUES (ADR-0018 decision 23, seventh amendment).
#
# How the Grep and Glob tools hand their values on is not known, so for an
# in-scope Grep or Glob under a guarded policy the guard refuses what a
# tool could misread, whatever the tool would do:
#
#   1. no string anywhere in tool_input may begin with `-` (the path,
#      Grep's pattern, glob or type, Glob's pattern, or any other field),
#      which a tool that passed it on as it stands could read as an option;
#   2. no `/`-separated component of Glob's pattern or Grep's glob may
#      begin with `.` followed by `*` or `?`, which an engine that lists
#      `.` and `..` could match against `..`;
#   3. the path, when it is a string, may hold only ASCII letters, digits
#      and `_ - . /`, so that no glob syntax or other special character in
#      it can make the tool search a place no list judged. It is tested as
#      given, the root's part included.
#
# Rules 1 and 2 are one jq check, directly after SEARCH PATTERNS' check and
# before the empty-path check, with the denial "no value in a Grep or a
# Glob may begin with '-' ...". Rule 3 is a second check, after the root
# rule and directly before EXEMPT_GLOBS, so that a path out of plain form,
# a path outside the root and the root's own spellings keep their earlier
# handling; its denial is "the path of a Grep or a Glob may contain only
# ...". Each reads jq's exit status over the raw payload; only status 1
# passes. A Read, Edit or Write, a caller the policy does not name and a
# policy that constrains no paths are untouched. There is no knob.
#
# ROOT (ADR-0018 decision 20, fifth amendment).
#
# Every glob list is written relative to a root, and a path outside it
# would be judged by patterns never written for it: `*/tests/*` matched
# `/tmp/tests/x`, and a list of repository prefixes did not match a Grep of
# the directory above the project. So under a guarded policy a path must
# lie inside the policy's root, which PATH_ROOT names:
#
#   PATH_ROOT   the root                 inside the root
#   project     CLAUDE_PROJECT_DIR       an absolute path equal to the
#                                        root or beginning with the root
#                                        and `/`; a relative path only
#                                        when the payload's cwd equals the
#                                        root
#   cwd         the payload's cwd        an absolute path equal to the
#                                        root or beginning with the root
#                                        and `/`; any relative path
#   unset/empty cwd, then                an absolute path inside either
#               CLAUDE_PROJECT_DIR       usable base; a relative path
#               (which falls back to     only when cwd is usable (eighth
#               cwd), as before          amendment)
#   other       none                     a configuration error: every
#                                        in-scope call is refused
#
# One trailing `/` is removed from a root, and from cwd before it is
# compared with a root. The last column assumes a usable root. A root is
# usable when it begins with `/`, is in plain form by decision 18's four
# tests (the fourth, since the seventh amendment, refusing a `~` at the
# start of any component, so a root with a later component that begins
# with `~` is not usable either), and is not `/`, the one such root that
# is empty once its one
# trailing `/` is removed. Any other root is treated as an empty root,
# whatever the reason: its variable unset or empty, `/`, `//`, `/.`,
# `/x/..`, a relative path, or anything else out of plain form. An empty
# root contains no path, absolute or relative: every guarded path is
# outside it. So under `project` a CLAUDE_PROJECT_DIR, and under `cwd` a
# cwd, that is not usable puts every guarded path outside the root. With
# PATH_ROOT unset, an absolute path is compared only with a base that is
# usable, CLAUDE_PROJECT_DIR among them; a relative path is inside only
# when cwd is usable (eighth amendment: an absent, null or empty cwd no
# longer defers to CLAUDE_PROJECT_DIR for a relative path, which is then
# outside every root and meets the root rule, whatever the harness would
# resolve it against); and with no usable base the root is empty. cwd is
# empty when the payload's cwd is absent, null or
# the empty string, and CLAUDE_PROJECT_DIR when it is unset or empty. The
# usability test is applied to the value as given, before its trailing
# `/` is removed. The relativisation strips
# the root (or, unset, the two
# bases in turn) and does nothing else; the path relative to the root is
# what the glob lists see, and a relative path is kept as written.
#
# The value of PATH_ROOT is checked right after the SCOPE_AGENT_TYPES
# routing and before `guarded` is worked out, so an invalid value refuses
# every in-scope call, guarded or not, while an out-of-scope caller passes
# through untouched. The relativisation keeps its place after the
# empty-path check. The root rule itself runs only under a guarded policy,
# after the plain-form rule (so a path out of plain form keeps that denial)
# and before every glob list, EXEMPT_GLOBS included; it refuses with the
# root denial ("... is not inside this policy's root directory ..."). The
# project root's own spellings are handled by the project-root check,
# before the rule, as before. Only a usable root has spellings of its own
# there. The relativisation strips only a usable root or base, so the
# check, which reads `rel`, fires for an absolute path only when it spells
# a usable one: `<root>`, `<root>/`, `<root>/.` or `<root>/./`. Under a
# root that is not usable, an absolute path keeps its form, and one in
# plain form reaches the root rule: with CLAUDE_PROJECT_DIR and cwd both
# `/` under PATH_ROOT='project', a Grep of `/` gets the root denial, not
# the project-root denial. The relative spellings `.` and `./` are never
# relativised, since a usable root or base is absolute, and keep the
# check's handling whatever the root: a Read, Grep or Glob of either gets
# the project-root denial, and an Edit or Write exits 0. No knob turns the
# rule off: PATH_ROOT
# chooses the root, not whether there is one. A path inside the root that
# leads outside it through a symlink is not handled (Question 5).
#
# WIRING -- read this before believing the guard is doing anything.
#
# This hook must be wired in `.claude/settings.json` (or
# `.claude/settings.local.json`). It must NOT be wired in an agent file's
# `hooks:` frontmatter. `hooks:` is a documented frontmatter field, but
# a guard declared there did not fire in this environment: tested
# three times, including with an absolute script path -- no error, no
# warning, nothing to notice, so the agent ran completely unfenced
# (issue #102). The best-supported explanation is the documented
# requirement that a project-level agent's frontmatter hooks run only
# once the workspace trust dialog has been accepted for the folder
# containing the agent file; this session has no trust record for the
# project. That has not been confirmed directly. Either way the
# consequence is the same: whether a frontmatter guard fires depends on
# environment state that is invisible from the repository, so it can
# look enforced on one machine and silently do nothing on another.
# `.claude/settings.json` hooks fired in every test -- see also WIRING
# in bash-guard.sh.
#
# WHERE THE CONFIGURATION IS READ FROM. Hook configuration is read from
# the main project checkout, not from a subagent's worktree. A
# worktree-isolated agent is fenced by whatever the main checkout's
# `.claude/settings.json` contains at dispatch time; the copy in its
# worktree is inert. So to test a policy change, the change must be in
# the main checkout, and a probe dispatched right after editing tests
# the edited policy -- not whatever the worktree has checked out.
# Established by experiment: with the policy removed from the main
# checkout only, while the worktree copy still carried it, the write
# was not denied (issue #102).
#
# Settings-level hooks are session-wide: they fire for every agent and for
# the top-level session, not only the agent a policy was written for. That
# is what SCOPE_AGENT_TYPES is for. One entry still carries one policy,
# because the env vars come from the hook's own command line, so two
# agents needing different globs need two entries. Two entries whose
# SCOPE_AGENT_TYPES overlap both run, and the stricter one's denial wins,
# since any deny is final.
#
# The scoping is FAIL-OPEN by design: an absent or unlisted agent_type
# means "not my business", not "deny" -- a top-level call carries no
# agent_type at all. It is routing, not a check, so an exit 0 for an
# out-of-scope caller is not approval, only a statement that this policy
# did not apply. See "SCOPING IS FAIL-OPEN BY DESIGN" in bash-guard.sh for
# why a stricter rule would break the session it was installed in without
# being a boundary for anyone.
#
# Unscoped content tools are DENIED for guarded agents. Grep and Glob take
# an optional `path`; without one they search the whole project, and
# Grep's `output_mode: content` then returns matching lines from files the
# guard is supposed to hide. Passing such a call through (as this script
# did before) made the guard advisory rather than enforced. A guarded
# agent must therefore name an in-scope path it wants to search. The same
# applies to a `path` that resolves to the project root itself.
#
# Caveat (documented, not a bug): this only intercepts the tool calls named
# in the subagent's own `matcher` (Edit|Write or Read|Grep|Glob). It does
# NOT inspect Bash commands, so an agent that also has the Bash tool could
# still read or write a guarded path via a shell command. Keep Bash off
# any agent whose guard must be a hard boundary, or treat the guard as a
# strong default rather than a sandbox for agents that keep Bash.

set -f -e -u -o pipefail

# Fail-closed exit (ADR-0018 decision 19, part 1, as the seventh amendment
# leaves it; see A GUARD THAT FAILS DENIES in the header). Installed first,
# so that it covers every line below: any status but 0, and but the 2 of
# `deny`'s own `exit 2` (which sets `denying` just before it), becomes the
# backstop denial and exit 2. A status 2 that `deny` did not make gets the
# backstop too. Written with printf and a fixed template, never with jq,
# which may be what failed; the text has no `"` and no `\`, and the status
# is an integer, so nothing needs escaping. The handler runs under `set -e`
# too, so its printf is guarded with `|| :` and cannot end it early, and
# its last command is `exit 2`. `${denying:-0}` keeps it safe under
# `set -u` wherever it fires.
on_exit() {
  local status="$1"
  if (( status != 0 )) && ! (( status == 2 && ${denying:-0} )); then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "Hammertime path guard: the guard stopped with status ${status} before reaching a verdict, so it cannot vouch for this tool call. The tool call is refused." || :
    exit 2
  fi
}
trap 'on_exit "$?"' EXIT

# One top-level command (ADR-0018 decision 19, part 5). Every command after
# the trap is inside this brace group, which ends every path through it
# with `exit`. The script's last line, after the group, is `exit 3`: only a
# command that bash abandoned part-way can reach it, and the trap turns it
# into the backstop denial. The group's body is not re-indented, so that
# the lines inside it read as they did before.
{

# The payload (ADR-0018 decision 19, part 3, and decision 25, rule 1). At
# most its first 8388609 bytes, 8 MiB and one byte, are read; each NUL byte
# is kept as U+0002, which no JSON text holds raw, so that the command
# substitution drops nothing unseen. The rest of stdin is then read and
# discarded, so that the harness is never left writing into a closed pipe.
# A longer payload is cut there, and the shape check below refuses it
# unless the cut falls after one whole JSON value and nothing but
# whitespace.
input="$(head -c 8388609 | tr '\000' '\002')"
cat >/dev/null

# If jq cannot write the JSON deny, the same reason goes to stderr instead,
# and the exit is still 2, which blocks whether or not JSON is printed
# (ADR-0018 decision 19, part 2). Defined here, above the extraction lines,
# because the payload-shape check below uses it. `denying` is set
# immediately before `exit 2`, after the jq or its fallback, so that a
# failure of either still reaches the trap with its own status (part 1).
deny() {
  local reason="$1"
  jq -n --arg reason "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }' || printf '%s\n' "$reason" >&2
  denying=1
  exit 2
}

# Payload shape (ADR-0018 decision 19, part 3; see A GUARD THAT FAILS
# DENIES in the header). The one check before the routing, for every
# caller: a payload that is not one JSON object whose tool_input is an
# object, whose tool_name is a string, and whose cwd and agent_type are
# strings, null or absent is refused, so that no extraction line below can
# fail on the payload's shape. A payload that holds a raw U+0002, which is
# where a raw NUL byte went when it was read, is malformed without the jq
# call: shape_status stays 0. Only status 1 (a clean false: well formed)
# passes; the status is captured with `|| shape_status=$?` so that neither
# `set -e` nor an `if` condition can turn a jq error into a pass.
shape_status=0
if [[ "$input" != *$'\002'* ]]; then
  printf '%s' "$input" | jq -e -s 'length != 1 or (.[0] | (type != "object") or ((.tool_input | type) != "object") or ((.tool_name | type) != "string") or ([.cwd, .agent_type] | any(. != null and type != "string")))' >/dev/null 2>&1 || shape_status=$?
fi
if [[ "$shape_status" != 1 ]]; then
  deny "Hammertime path guard: the hook payload could not be read as a single tool call (the check ended with status ${shape_status}). A payload must be one JSON object whose tool_input is an object, whose tool_name is a string, and whose cwd and agent_type are strings, null or absent; without that, the guard cannot tell what the call would act on or who is making it. The tool call is refused."
fi

# The path is read from its tool's own field (ADR-0018 decision 24; see
# FIELDS in the header): tool_input.file_path for a Read, Edit or Write,
# tool_input.path for a Grep or Glob, and nothing for any other tool. The
# same selector is used by the extraction check below, the NUL gate and the
# trailing-newline test.
tool_name="$(printf '%s' "$input" | jq -r '.tool_name // empty')"
file_path="$(printf '%s' "$input" | jq -r '(if .tool_name == "Read" or .tool_name == "Edit" or .tool_name == "Write" then .tool_input.file_path elif .tool_name == "Grep" or .tool_name == "Glob" then .tool_input.path else null end) // empty')"
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty')"
agent_type="$(printf '%s' "$input" | jq -r '.agent_type // empty')"

# Extraction check (ADR-0018 decision 19, part 4; see A GUARD THAT FAILS
# DENIES in the header). Directly after the four extraction lines and
# before the routing, for every caller. One jq call over the raw payload
# asks whether each extracted field is empty exactly when the payload's
# field, as `$(...)` renders it, is empty: absent, null or false render
# empty; a string renders empty when, its NUL bytes removed, nothing but
# newlines is left; any other value renders non-empty. jq is told only
# which extracted fields are empty, never their values. The same call asks
# whether cwd, when it is a string, ends with a newline or holds a NUL,
# which `$(...)` would read as another directory. Only status 1 (a clean
# false) passes; any other status ends the script with status 3, which the
# trap turns into the backstop denial.
extract_status=0
printf '%s' "$input" | jq -e --arg tool_name "${tool_name:+1}" --arg cwd "${cwd:+1}" --arg agent_type "${agent_type:+1}" --arg path "${file_path:+1}" 'def rendered: if . == null or . == false then false elif type == "string" then (explode | map(select(. != 0)) | implode | test("\\A\n*\\z") | not) else true end; ([(.tool_name | rendered), (.cwd | rendered), (.agent_type | rendered), ((if .tool_name == "Read" or .tool_name == "Edit" or .tool_name == "Write" then .tool_input.file_path elif .tool_name == "Grep" or .tool_name == "Glob" then .tool_input.path else null end) | rendered)] != [$tool_name == "1", $cwd == "1", $agent_type == "1", $path == "1"]) or ((.cwd | type) == "string" and ((.cwd | endswith("\n")) or (.cwd | explode | map(select(. == 0)) != [])))' >/dev/null 2>&1 || extract_status=$?
if [[ "$extract_status" != 1 ]]; then
  exit 3
fi

# Scope routing -- deliberately NOT a check. This hook has to be wired
# session-wide (see WIRING in the header), so it sees tool calls from
# callers this policy was never written for. When SCOPE_AGENT_TYPES is
# set, only the listed agents are policed and everyone else passes
# through untouched: an absent agent_type (a top-level call carries none)
# or one that is not on the list means "not my business", not "deny".
# Unset behaves as it always has and polices every call that reaches this
# hook, which is what the test suite exercises.
#
# This test deliberately sits first, after only the payload-shape and
# extraction checks above, and before `guarded` is even worked out, so
# that an out-of-scope
# caller costs nothing and cannot be affected by this policy's
# configuration.
if [[ -n "${SCOPE_AGENT_TYPES:-}" ]]; then
  in_scope=0
  if [[ -n "$agent_type" ]]; then
    for scoped_agent in $SCOPE_AGENT_TYPES; do
      if [[ "$agent_type" == "$scoped_agent" ]]; then
        in_scope=1
        break
      fi
    done
  fi
  if (( ! in_scope )); then
    exit 0
  fi
fi

# PATH_ROOT (ADR-0018 decision 20; see ROOT in the header). Empty or unset,
# `project` and `cwd` are the only valid values; anything else is a
# configuration error that refuses every in-scope call, guarded or not, as
# LITERAL_ONLY does in bash-guard.sh. After the routing, so that a caller
# this policy does not name still passes through untouched.
path_root="${PATH_ROOT:-}"
case "$path_root" in
  "" | project | cwd) ;;
  *)
    deny "Hammertime path guard: configuration error: PATH_ROOT is '${path_root}', and the only valid values are empty, project and cwd. Every tool call is refused until the policy in .claude/settings.json is corrected; this is not something the calling agent can fix."
    ;;
esac

# A guard is in force for this agent if it constrains paths at all.
guarded=0
if [[ -n "${DENY_GLOBS:-}" || -n "${ALLOW_GLOBS:-}" ]]; then
  guarded=1
fi

# Tools whose RESULTS can disclose the contents or existence of files
# anywhere under the search root, not just at one named path.
content_tool=0
case "$tool_name" in
  Read | Grep | Glob) content_tool=1 ;;
esac

# The tool and its fields (ADR-0018 decision 24; see FIELDS in the header).
# Only under a guarded policy, after the routing, the PATH_ROOT check and
# `guarded`, and before the NUL gate, so before every other check that
# reads the path. The tool must be a Read, Edit or Write, or a Grep or
# Glob, and the other kind's path field must be absent, null or false.
# Only status 1 passes; the status is captured as in the NUL gate.
if (( guarded )); then
  fields_status=0
  printf '%s' "$input" | jq -e 'if .tool_name == "Read" or .tool_name == "Edit" or .tool_name == "Write" then (.tool_input.path // false) != false elif .tool_name == "Grep" or .tool_name == "Glob" then (.tool_input.file_path // false) != false else true end' >/dev/null 2>&1 || fields_status=$?
  if [[ "$fields_status" != 1 ]]; then
    deny "Hammertime path guard: this guard reads the path of a Read, Edit or Write from file_path and the path of a Grep or Glob from path, and this call either comes from another tool or also names a path in the other field, so the guard cannot tell which path the tool would act on. The tool call is refused."
  fi
fi

# NUL gate (ADR-0018 decision 17; see NUL GATE in the header). Only under a
# guarded policy, and before the empty-path check: a path that is only a
# NUL comes out of the extraction above empty, and EXEMPT_GLOBS below exits
# 0 on a match, so a later gate would never see some NUL-bearing paths.
# It tests the tool's own field, through decision 24's selector.
# The status is captured with `|| nul_status=$?` so that neither `set -e`
# nor an `if` condition can turn a jq error into "no NUL".
if (( guarded )); then
  nul_status=0
  printf '%s' "$input" | jq -e '((if .tool_name == "Read" or .tool_name == "Edit" or .tool_name == "Write" then .tool_input.file_path elif .tool_name == "Grep" or .tool_name == "Glob" then .tool_input.path else null end) // "") | explode | any(. == 0)' >/dev/null 2>&1 || nul_status=$?
  case "$nul_status" in
    1) ;;
    0)
      deny "Hammertime path guard: the path contains a NUL byte (U+0000), which cannot be carried through this guard intact — the byte is dropped when the path is read, so the guard cannot vet the path the tool would actually use. The tool call is refused."
      ;;
    *)
      deny "Hammertime path guard: the path could not be checked for a NUL byte (the check ended with status ${nul_status} instead of a result), so the guard cannot confirm that the path it would vet is the path the tool would use. The tool call is refused."
      ;;
  esac
fi

# Trailing newline (ADR-0018 decision 17, seventh amendment; see NUL GATE
# in the header). Only under a guarded policy, directly after the NUL
# gate, so before the empty-path and project-root checks, which let an
# Edit or Write through with exit 0. `$(...)` drops trailing newlines, so a
# path that ends with one would be vetted as another path. Only status 1
# passes; status 0, and any other status, get the denial.
if (( guarded )); then
  newline_status=0
  printf '%s' "$input" | jq -e '((if .tool_name == "Read" or .tool_name == "Edit" or .tool_name == "Write" then .tool_input.file_path elif .tool_name == "Grep" or .tool_name == "Glob" then .tool_input.path else null end) // "") | endswith("\n")' >/dev/null 2>&1 || newline_status=$?
  if [[ "$newline_status" != 1 ]]; then
    deny "Hammertime path guard: the path ends with a newline, or could not be checked for one, and trailing newlines are dropped when the path is read, so the guard cannot vet the path the tool would actually use. Give the path without the newline. The tool call is refused."
  fi
fi

# Edge characters (ADR-0018 decision 26; see EDGE CHARACTERS in the
# header). Only under a guarded policy, directly after the
# trailing-newline test, so before decision 21's check, decision 23's
# first check, the empty-path check, the relativisation, the project-root
# check, the plain-form rule, the root rule and every glob list. A tool
# that trims its path would act on another path than the one vetted, so a
# path, the tool's own field, whose first or last character is whitespace
# or a control character is refused. Only status 1 passes; status 0, and
# any other status, get the denial. The status is captured as in the NUL
# gate.
if (( guarded )); then
  edge_status=0
  printf '%s' "$input" | jq -e '((if .tool_name == "Read" or .tool_name == "Edit" or .tool_name == "Write" then .tool_input.file_path elif .tool_name == "Grep" or .tool_name == "Glob" then .tool_input.path else null end) // "") | explode | if length == 0 then false else [.[0], .[-1]] | map(. <= 32 or (. >= 127 and . <= 160) or . == 5760 or . == 6158 or (. >= 8192 and . <= 8202) or . == 8232 or . == 8233 or . == 8239 or . == 8287 or . == 12288 or . == 65279) | any end' >/dev/null 2>&1 || edge_status=$?
  if [[ "$edge_status" != 1 ]]; then
    deny "Hammertime path guard: the path begins or ends with whitespace or a control character, or could not be checked for one, and a tool that trims its path would act on a path other than the one this guard vetted. Give the path without them. The tool call is refused."
  fi
fi

# Search patterns (ADR-0018 decision 21; see SEARCH PATTERNS in the
# header). Only under a guarded policy, after the NUL gate, the
# trailing-newline test and decision 26's edge-character check, so that a
# path with a NUL keeps the NUL denial, and before the empty-path
# check and EXEMPT_GLOBS, which exits 0 on a match. The value is Glob's
# tool_input.pattern or Grep's tool_input.glob. It is judged entirely in jq,
# over the raw payload, and read from jq's exit status, so that no command
# substitution touches it and a NUL in it is seen. `\A` and `\z` anchor at
# the very start and end of the string, where `^` and `$` may also match at
# a newline. Only status 1 (a clean false: admitted) passes; the status is
# captured as in the NUL gate.
if (( guarded )); then
  pattern_status=0
  printf '%s' "$input" | jq -e '(if .tool_name == "Glob" then .tool_input.pattern elif .tool_name == "Grep" then .tool_input.glob else null end) | if . == null or . == false then false elif type != "string" then true else (test("\\A[A-Za-z0-9_./*?-]*\\z") | not) or startswith("/") or contains("..") end' >/dev/null 2>&1 || pattern_status=$?
  if [[ "$pattern_status" != 1 ]]; then
    deny "Hammertime path guard: a Glob pattern or a Grep glob may contain only letters, digits and the characters _ - . / * ?, and may not begin with '/' or contain '..', because this guard cannot tell where any other pattern would take the search. Give a pattern in that form, relative to the path being searched. The tool call is refused."
  fi
fi

# Search values, rules 1 and 2 (ADR-0018 decision 23; see SEARCH VALUES in
# the header). Only under a guarded policy, directly after decision 21's
# check, so before the empty-path check and EXEMPT_GLOBS; a value decision
# 21 refuses keeps its denial. For a Grep or a Glob: no string anywhere in
# tool_input may begin with `-`, and no `/`-separated component of Glob's
# pattern or Grep's glob may begin with `.` followed by `*` or `?`. Only
# status 1 passes; the status is captured as in the NUL gate.
if (( guarded )); then
  values_status=0
  printf '%s' "$input" | jq -e 'if .tool_name == "Grep" or .tool_name == "Glob" then ([.tool_input | .. | strings | startswith("-")] | any) or ((if .tool_name == "Glob" then .tool_input.pattern else .tool_input.glob end) as $p | if ($p | type) == "string" then ($p | test("(\\A|/)\\.[*?]")) else false end) else false end' >/dev/null 2>&1 || values_status=$?
  if [[ "$values_status" != 1 ]]; then
    deny "Hammertime path guard: no value in a Grep or a Glob may begin with '-', and no part of a Glob pattern or a Grep glob between slashes may begin with '.' followed by '*' or '?', because the tool could read such a value as an option, or match such a part against the '..' entry and search above the path being searched. Give the value in another form; a Grep pattern that has to match a leading '-' can begin with '[-]' instead. The tool call is refused."
  fi
fi

# No path at all. For a guarded agent this is an unscoped Grep/Glob over
# the whole project, which can return guarded content; deny it and say how
# to proceed. Any other tool shape passes through as before.
if [[ -z "$file_path" ]]; then
  if (( guarded && content_tool )); then
    deny "Hammertime path guard: an unscoped ${tool_name} would search the whole project and can return files this agent may not read. Re-run it with an explicit in-scope 'path' (for example 'docs', 'schemas', or a specific tests directory)."
  fi
  exit 0
fi

# Normalize to a path relative to the policy's root (ADR-0018 decision 20;
# see ROOT in the header), and record in `inside` whether the path lies
# inside it. Under PATH_ROOT='project' the root is CLAUDE_PROJECT_DIR,
# under PATH_ROOT='cwd' the payload's cwd, and unset or empty keeps the two
# old bases, cwd then CLAUDE_PROJECT_DIR (which falls back to cwd), tried
# in turn. A root or base is used only when it is usable (usable_root:
# it begins with `/`, is in plain form by decision 18's four tests, the
# fourth reaching every component's leading `~` since the seventh
# amendment, and is not `/`); any other one, unset or empty included, is
# treated as an empty
# root, skipped, and contains no path. Its one trailing `/` is removed only
# after that test. Note the exact-match arm:
# without it, a path equal to the root itself fell through with `rel`
# still absolute and matched no glob at all. This only strips a prefix; it
# resolves no `.`, `..` or `//` and expands no `~`. A path out of plain
# form is refused by the rule below (see PLAIN FORM in the header).
#
# A relative path is kept as written and judged relative to the root:
# under `cwd` inside only when cwd is usable; under `project` only when
# CLAUDE_PROJECT_DIR is usable and the payload's cwd equals it, one
# trailing `/` removed from each (the harness resolves a relative path
# against cwd); and when PATH_ROOT is unset only when cwd is usable (eighth
# amendment: an absent, null or empty cwd no longer defers to
# CLAUDE_PROJECT_DIR for a relative path, which is then outside every root
# and meets the root rule). An absolute path is still compared with each
# usable base, CLAUDE_PROJECT_DIR among them. With no usable base the root
# is empty and contains no path. Under `project` and `cwd` only
# an absolute path is compared with the root; unset keeps the old
# comparison for every path, against usable bases only. Since a usable
# root is absolute, `.` and `./` are never relativised and keep the
# project-root check's handling whatever the root.
usable_root() {
  local root="$1"
  [[ "$root" == /* && "$root" != / && "$root" != *//* && "/$root/" != */../* && "/$root/" != */./* && "$root" != *"/~"* ]]
}
project_dir="${CLAUDE_PROJECT_DIR:-$cwd}"
case "$path_root" in
  project) root_bases=("${CLAUDE_PROJECT_DIR:-}") ;;
  cwd) root_bases=("$cwd") ;;
  *) root_bases=("$cwd" "$project_dir") ;;
esac
rel="$file_path"
inside=0
if [[ -z "$path_root" || "$file_path" == /* ]]; then
  for base in "${root_bases[@]}"; do
    usable_root "$base" || continue
    base="${base%/}"
    if [[ "$file_path" == "$base" ]]; then
      rel="."
      inside=1
      break
    elif [[ "$file_path" == "$base"/* ]]; then
      rel="${file_path#"$base"/}"
      inside=1
      break
    fi
  done
fi
if (( ! inside )) && [[ "$file_path" != /* ]]; then
  case "$path_root" in
    project)
      if usable_root "${CLAUDE_PROJECT_DIR:-}" && [[ "${cwd%/}" == "${CLAUDE_PROJECT_DIR%/}" ]]; then
        inside=1
      fi
      ;;
    cwd)
      if usable_root "$cwd"; then
        inside=1
      fi
      ;;
    *)
      if usable_root "$cwd"; then
        inside=1
      fi
      ;;
  esac
fi

# The path resolved to the project root: same exposure as no path at all.
if [[ "$rel" == "." || "$rel" == "./" || -z "$rel" ]]; then
  if (( guarded && content_tool )); then
    deny "Hammertime path guard: a project-root ${tool_name} would search every file and can return files this agent may not read. Re-run it with an explicit in-scope 'path'."
  fi
  exit 0
fi

# Plain form (ADR-0018 decision 18; see PLAIN FORM in the header). Only
# under a guarded policy, after the project-root check and before
# EXEMPT_GLOBS, which exits 0 on a match. Wrapping the path in slashes makes
# a `.` or `..` component at the start or the end look like one in the
# middle. The `~` is quoted so that it is not subject to tilde expansion.
# The fourth test reaches every component (seventh amendment): a `~` that
# begins the path, or that follows any `/`.
if (( guarded )); then
  if [[ "/$file_path/" == */../* || "/$file_path/" == */./* || "$file_path" == *//* || "$file_path" == "~"* || "$file_path" == *"/~"* ]]; then
    deny "Hammertime path guard: the path contains a '.' or '..' component, a '//' or a leading '~'. This guard matches a path exactly as written and resolves none of these, so it cannot vet the file or directory the tool would actually use. Give the path without any of them. The tool call is refused."
  fi
fi

# Root rule (ADR-0018 decision 20; see ROOT in the header). Only under a
# guarded policy, after the plain-form rule, so that a path out of plain
# form keeps that denial, and before EXEMPT_GLOBS, which exits 0 on a
# match. Every glob list is written relative to the root, so a path outside
# it is refused whatever the lists would say.
if (( guarded )); then
  if (( ! inside )); then
    deny "Hammertime path guard: the path is not inside this policy's root directory: the project directory under PATH_ROOT='project', the working directory under PATH_ROOT='cwd', or either of them when PATH_ROOT is unset. Under PATH_ROOT='project' a relative path counts as inside only when the working directory is the project directory. This guard's glob lists judge only paths inside the root, so it cannot vet this one. Give an absolute path inside the root. The tool call is refused."
  fi
fi

# Search values, rule 3 (ADR-0018 decision 23; see SEARCH VALUES in the
# header). Only under a guarded policy, after the root rule and directly
# before EXEMPT_GLOBS, so that a path out of plain form keeps decision 18's
# denial, a path outside the root decision 20's, and the root's own
# spellings the project-root check's handling. A Grep's or a Glob's path,
# when it is a string, may hold only ASCII letters, digits and `_ - . /`;
# it is tested as given, the root's part included. Only status 1 passes;
# the status is captured as in the NUL gate.
if (( guarded )); then
  search_path_status=0
  printf '%s' "$input" | jq -e 'if (.tool_name == "Grep" or .tool_name == "Glob") and ((.tool_input.path | type) == "string") then (.tool_input.path | test("\\A[A-Za-z0-9_./-]*\\z") | not) else false end' >/dev/null 2>&1 || search_path_status=$?
  if [[ "$search_path_status" != 1 ]]; then
    deny "Hammertime path guard: the path of a Grep or a Glob may contain only letters, digits and the characters _ - . /, because the tool could read any other character as part of a pattern or an option and search somewhere other than the path this guard vetted. Give the path in that form. The tool call is refused."
  fi
fi

matches_any() {
  local path="$1"; shift
  local pattern
  for pattern in "$@"; do
    [[ -z "$pattern" ]] && continue
    if [[ "$path" == $pattern ]]; then
      return 0
    fi
  done
  return 1
}

if [[ -n "${EXEMPT_GLOBS:-}" ]] && matches_any "$rel" $EXEMPT_GLOBS; then
  exit 0
fi

if [[ -n "${DENY_GLOBS:-}" ]] && matches_any "$rel" $DENY_GLOBS; then
  deny "Hammertime path guard: '$rel' is out of scope for this agent (matched DENY_GLOBS)."
fi

if [[ -n "${ALLOW_GLOBS:-}" ]] && ! matches_any "$rel" $ALLOW_GLOBS; then
  deny "Hammertime path guard: '$rel' is out of scope for this agent (did not match ALLOW_GLOBS)."
fi

exit 0
}
exit 3
