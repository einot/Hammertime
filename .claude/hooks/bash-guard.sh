#!/usr/bin/env bash
# Generic PreToolUse Bash guard shared by Hammertime's project subagents
# (.claude/agents/*.md). This is the Bash counterpart of path-guard.sh:
# each agent's policy is set as env vars inline on the hook's own
# command line in `.claude/settings.json` (see WIRING below -- not in
# the agent file), so the same script is parametrized per-agent instead
# of duplicating the same policy in every agent definition.
#
# The guard is DEFAULT-DENY: a command runs only if every command name in
# it was named by the agent's own allowlist.
#
# Env vars (space-separated lists):
#
#   ALLOW_CMDS         - permitted command names, matched against argv[0]
#                        of every segment of the command. Anything else is
#                        DENIED. Unset or empty means no Bash guard is in
#                        force for this agent (the same "unguarded agent"
#                        shape path-guard.sh uses) and the command passes.
#   ALLOW_GIT_SUBCMDS  - permitted `git` subcommands. Only consulted when
#                        `git` is in ALLOW_CMDS; empty with `git` allowed
#                        denies every git subcommand.
#   ALLOW_NODE_SCRIPTS - glob list of script paths `node` may execute
#                        (`[[ str == pattern ]]` semantics, so a bare `*`
#                        in a pattern matches across `/` too). Checked
#                        against the path as written on the command line
#                        and against the path made relative to
#                        `${CLAUDE_PROJECT_DIR:-$cwd}`. Only consulted
#                        when `node` is in ALLOW_CMDS.
#   SCOPE_AGENT_TYPES  - agent types this policy applies to, matched
#                        against the payload's `agent_type`. Unset or
#                        empty polices every Bash call that reaches this
#                        hook (the historical behaviour). Set polices
#                        only the listed agents and passes every other
#                        caller through untouched. See WIRING below --
#                        this exists because the hook has to be
#                        installed session-wide.
#
# ADR-0018 (docs/adr/0018-coder-bash-policy-literal-commands-and-a-tripwire.md)
# adds five more. Every one is off unless a policy sets it, and with all
# five unset every rule and message below behaves as it did before them.
#
#   LITERAL_ONLY         - `1` turns on literal mode (see LITERAL MODE
#                          below): one lexical check, run before every
#                          other check, that refuses anything bash would
#                          rewrite. Empty or unset is off. Any other value
#                          is a configuration error that refuses every
#                          command. The uv and make rules, git
#                          add/commit/merge and ruff's write mode run only
#                          in literal mode; a policy that reaches one of
#                          them without it gets a configuration error.
#   DENY_ADVICE          - unset, empty or `needs-validation` keeps every
#                          message exactly as it was. `stop-and-report`
#                          appends ADR-0018 decision 11's paragraph
#                          ("This refusal is final for this task. ...")
#                          after one space to EVERY denial, configuration
#                          errors and shared rules included, and rewords
#                          the two auditor-specific messages (a command not
#                          on ALLOW_CMDS, a git subcommand not on
#                          ALLOW_GIT_SUBCMDS) without their
#                          needs-validation sentences. Any other non-empty
#                          value behaves as `stop-and-report`.
#   ALLOW_UV_RUN_TARGETS - targets `uv run` may launch. Every entry must
#                          also be on this script's own UV_RUN_KNOWN_TARGETS
#                          (`pytest ruff`), the targets it has rules for;
#                          any other entry is a configuration error.
#   ALLOW_MAKE_TARGETS   - the make targets allowed, one per command.
#   WRITE_DENY_GLOBS     - glob list (`[[ str == pattern ]]` semantics, as
#                          in path-guard.sh) of paths `ruff format` may not
#                          rewrite in write mode. It is meant to equal the
#                          agent's Edit/Write DENY_GLOBS, so that a write
#                          launched from Bash respects the same fence as a
#                          Write. Empty or unset refuses write mode
#                          altogether.
#
# Reads the PreToolUse JSON payload on stdin (see
# https://code.claude.com/docs/en/hooks) and inspects tool_input.command.
# Any tool other than Bash passes through.
#
# WIRING -- read this before believing the guard is doing anything.
#
# This hook must be wired in `.claude/settings.json` or
# `.claude/settings.local.json`. It must NOT be wired in an agent file's
# `hooks:` frontmatter. A guard declared there did not fire in this
# environment -- observed three times -- so an agent configured that way
# ran completely unfenced with no error anywhere. The docs list `hooks`
# as a supported frontmatter field but require workspace trust for a
# project-level agent's frontmatter hooks, and this project has no trust
# record; that is the best-supported explanation and has not been
# confirmed directly. The guard had been reviewed nine times without
# once being invoked. The smoke test that caught it was the wired
# security-auditor successfully running `sed -n 1,2p CHANGES`, with `sed`
# absent from its ALLOW_CMDS.
#
#   * agent-file `hooks:` frontmatter -- does NOT fire. Three probes: the
#     real agent, a throwaway file-based probe with an absolute hook
#     path, and the same probe with ${CLAUDE_PROJECT_DIR}. All three ran
#     the command that should have been denied.
#   * inline `--agents` JSON hooks -- DO fire, including `VAR='x' /path`
#     env assignments and ${CLAUDE_PROJECT_DIR}.
#   * settings.json / settings.local.json hooks -- DO fire for subagent
#     Bash calls, and ${CLAUDE_PROJECT_DIR} expands there.
#
# Settings-level hooks are session-wide, so they see every Bash call in
# the session and not just the agent you meant to fence. That is what
# SCOPE_AGENT_TYPES is for: the payload's `agent_type` names the caller
# ("security-auditor" for that subagent, absent for a top-level call), so
# a policy can be aimed at the agent it was written for.
#
# One entry carries one policy. The env vars come from the hook's own
# command line, so two agents needing different allowlists need two
# settings entries, each with its own SCOPE_AGENT_TYPES and its own
# ALLOW_CMDS; there is no way to express two policies in one entry. Two
# entries whose SCOPE_AGENT_TYPES overlap would both run, and the
# stricter one's denial wins, since any deny is final.
#
# Two properties of this arrangement. Both are things a reader needs to
# know, and neither is a safety claim:
#
#   SCOPING IS FAIL-OPEN BY DESIGN. An absent or unrecognised agent_type
#   means "not my business", not "deny". It is routing, not a check. This
#   guard exists to fence one agent against one policy; every other
#   caller is governed by its own configuration, or by Claude Code's own
#   sandbox. A version that denied unrecognised callers would break the
#   session it was installed in and would still not be a security
#   boundary for them, because it knows nothing about what they may do.
#   So do not read an exit 0 here as approval -- for an out-of-scope
#   caller it means only that this policy did not apply.
#
#   THE GUARD IS INERT UNLESS CONFIGURED, and that is the failure mode
#   that cost nine rounds. An empty ALLOW_CMDS exits 0. An unmatched
#   agent_type now exits 0. A hook wired in agent frontmatter did not
#   run at all in this environment (see WIRING). From outside, all three
#   are indistinguishable from a working guard: the command simply
#   succeeds. Reading the settings file is not enough either, because it
#   does not tell you the hook was reached. The only way to know it is
#   live is to run a command that MUST be denied and look at the
#   refusal: it has to say "Hammertime bash guard:". If it instead says
#   "Claude Code may only write to files in the allowed working
#   directories", that is the platform sandbox and this guard is not
#   running. Do that check after any change to the wiring, and treat a
#   silent success on a command that should be refused as evidence the
#   fence is missing rather than as a pass.
#
#   A POLICY MUST NEVER BE WIRED BEFORE THE SCRIPT THAT IMPLEMENTS IT
#   (ADR-0018 decision 13). A policy can depend on features of this
#   script -- the coder's depends on literal mode and the uv, make and
#   git add/commit/merge rules. Hook scripts are read from the main
#   checkout, at ${CLAUDE_PROJECT_DIR}/.claude/hooks/bash-guard.sh, so
#   the script that implements a policy must be the file there before
#   the policy goes into settings.json. A script that predates those
#   rules has no uv or make rule at all: wired to it, the coder's
#   policy would admit `uv run python -c ...` while looking closed. The
#   known-command check (see Caveat) makes a FUTURE policy fail closed
#   when it is wired early, by refusing any command this script has no
#   model for, but it cannot protect a transition to a script that does
#   not have that check yet.
#
# What it enforces beyond the name allowlist: the whole command is
# rejected if it contains redirection, backgrounding, process
# substitution, a backtick, any `$` (shell expansion of any kind), a
# brace (bash would expand it into extra words after this hook returns)
# or a newline, because each of those either writes a file, hides a
# second command from inspection, or changes the command after it has
# been inspected. What is left is split on `|`, `;`, `&&` and
# `||`, and every segment is validated on its own, so a pipeline runs only
# if each stage is independently allowed. Per-command rules then strip the
# file-writing and code-executing options that a few otherwise read-only
# tools carry: `sort -o`/`--compress-program`, `find -exec`/`-delete`,
# `git --output`/`-O`/`--help`, `rg --pre`, `file -C`. Three commands are
# handled by allowlist instead, because for them a denylist is
# structurally unsound (see POSITIONAL RULES below): `sed` may use only
# valueless options and a print-only script, `git` may use only a short
# list of valueless global options before its subcommand, and `node` may
# use no options at all before its script path -- which must additionally
# be a literal path, with no glob character and no leading `~`, because
# it is the only path this guard vets by value and bash would otherwise
# expand it into a different path after approval.
#
# LITERAL MODE (LITERAL_ONLY='1', ADR-0018 decision 3). One lexical check
# runs before every other check and refuses a command that contains,
# anywhere: a newline, `$`, a backtick, `\`, `'`, `"`, `{`, `}`, `[`, `]`,
# `(`, `)`, `*`, `?`, `<`, `>`, `#`; an `&` that is not part of `&&`;
# any C0 control character other than tab and newline (0x01-0x08, 0x0B,
# 0x0C, 0x0D, 0x0E-0x1F) and DEL (0x7F); or a word that begins with `~`
# or contains `=~` or `:~`. For that last
# test words are split on blanks and on `|`, `;` and `&`, because bash
# starts a new word after a separator whether or not a blank follows.
# What is left can only be words of ordinary characters separated by
# blanks and by `|`, `;`, `&&` and `||`. On such a string bash removes no
# quotes, performs no brace, tilde, parameter, command, arithmetic or
# process expansion, has no expansion result to word-split, globs nothing
# (extended globs need `(`), and recognises no comment, redirection or
# grouping. So the words split out below are exactly the argv each tool
# receives, and each segment is exactly one simple command; a reserved
# word at the start of a segment is simply not on ALLOW_CMDS. `HEAD~1`,
# `--tb=short`, `a:b` and `x@y` remain available. The denial says `must
# be literal` and names the supported forms: the Grep and Glob tools,
# `git commit -F .commit-msg`, `-k WORD`, and that stderr is captured.
#
# The control-character refusal (second amendment) is invariant hygiene
# rather than a closed exec path. Such bytes mostly fail closed already:
# an exact-match rule rejects a token carrying one (`pytest\r` is not
# `pytest`), and neither present-flag rule fires on a flag carrying one
# (`--check\r` is not `--check`, so ruff format stays in write mode and
# is refused). Refusing them outright makes "words of ordinary
# characters" literally true, and keeps the transcript that the tripwire
# relies on faithful: a carriage return can make a logged command line
# render as something other than what ran. Tab stays allowed because it
# is one of bash's blanks and this guard splits on it exactly as bash
# does. The bytes are enumerated, not matched with a locale-dependent
# range. The refusal is the ordinary literal-mode denial, naming "a
# control character other than tab". It applies only in literal mode:
# the auditor's deny-only policy has no relaxation for one to subvert.
#
# NUL GATE, IN EVERY MODE (ADR-0018 decision 3, second amendment). Before
# the empty-command exit and before literal mode, independent of
# LITERAL_ONLY, a command whose decoded tool_input.command contains a NUL
# (U+0000) is refused. The reason is the extraction every policy shares:
# command_str comes from a command substitution, which silently drops NUL
# bytes, so the string every other rule vets may not be the command the
# harness runs -- `uv run --locked ruff format<NUL> --check .` would be
# vetted as read-only. The gate therefore does NOT trust command_str. It
# runs `jq -e '(.tool_input.command // "") | explode | any(. == 0)'` over
# the raw payload and reads jq's EXIT STATUS, because any captured string
# would pass through the same NUL-stripping. Status 1 (a clean false) is
# the only pass; 0 denies as a NUL; any other status -- a jq error, such
# as `explode` on a command that is a number, array or object -- denies
# as "could not be checked". The status is captured explicitly, because
# `set -e` does not act inside an `if` condition and would otherwise let
# every jq error through as "no NUL". An absent or null command becomes
# the empty string, passes, and exits 0 at the empty-command check as
# before. This is the one refusal the amendment adds to the auditor.
#
# Literal mode is what the ADR-0018 rules stand on, and they run only in
# it:
#
#   * uv (decision 5): only `uv run`, nothing between `uv` and `run`;
#     between `run` and the target only `--locked` and `--offline`,
#     matched exactly (both valueless, so the first word without a dash
#     IS the target); the target on ALLOW_UV_RUN_TARGETS and on
#     UV_RUN_KNOWN_TARGETS; the shadow check (below); the target's own
#     rule; and LAST, `--locked` required among uv's options. Last,
#     because that denial corrects a form instead of refusing a
#     capability, so it is only ever given to a command that is allowed
#     as soon as `--locked` is added, and it names that command.
#   * Shadowing (decisions 5 and 8): every uv and make command is refused
#     while the payload's cwd holds an entry of any type named pytest,
#     ruff, mypy, GNUmakefile or makefile, and when the payload carries
#     no cwd. uv runs a same-named directory with __main__.py, or a
#     zipapp, in place of the tool, and make reads GNUmakefile and
#     makefile before Makefile. This is defence in depth behind the
#     Edit/Write fence, which refuses to create those names: a check
#     against the live filesystem has a window when a Write and a Bash
#     call are issued together.
#   * pytest (decision 6) and ruff (decision 7): exact-match option
#     allowlists, and EVERY word checked -- none skipped as an option's
#     value -- so the guard never models which options take values. A
#     pytest operand must be a relative directory or a test_*.py /
#     *_test.py file (with or without ::node ids), because pytest imports
#     any .py and doctests any .txt/.rst named on its command line.
#     `ruff format` without `--check`/`--diff` is write mode: explicit
#     .py/.pyi files only, none matching WRITE_DENY_GLOBS.
#   * make (decision 8): exactly `make TARGET`, TARGET on
#     ALLOW_MAKE_TARGETS, then the shadow check.
#   * git add, commit and merge (decision 9): an option allowlist each,
#     over every word after the subcommand; `merge` requires --ff-only.
#     Every existing git rule still applies first.
#
# BASH EXPANSIONS -- the layer underneath every option rule in this file.
# Everything below reasons about how a TOOL parses its arguments. That is
# only half the problem: this guard inspects a STRING, and bash rewrites
# that string before any tool starts. Seven rounds of review missed a
# live write-and-exec bypass for exactly this reason. Every round
# compared the guard's model of a tool's parser against the real parser,
# and none asked what bash does to the command between this hook
# returning "allow" and the binary starting. The answer was brace
# expansion, and through the shipping allowlist it deleted a file, wrote
# a 212KB file and executed a script. So when adding a rule here, ask the
# question in BOTH directions: does the tool read this token the way I
# think it does, and will this token still be this token when the tool
# runs?
#
# Bash's word expansions, in the order bash applies them, with where each
# one stands here. Every "REJECTED" names the check that does it.
#
#   1. Brace expansion -- `{a,b}`, `{1..9}`
#      REJECTED whole-command, by the brace check. It has to be: it
#      invents new words after this hook has already answered.
#   2. Tilde expansion -- `~`, `~user`, `~+`, `~-`
#      NOT rejected in general; bounded by what it can produce, except in
#      the one path-vetting rule, where it is rejected outright. Every
#      form expands to an absolute path (verified: `~` -> /root,
#      `~+` -> $PWD, `~-` -> $OLDPWD, `~root` -> /root), so it cannot
#      introduce a leading `-` and cannot turn an operand into an option.
#      The bound fails only if HOME itself begins with `-`, and the agent
#      cannot set HOME -- a leading VAR=value is denied by the argv[0]
#      `=` rule. Only a LEADING `~` expands at all: bash leaves
#      `--foo=~/x` and `x~/y` alone (verified).
#
#      It does reach outside the repository. To be precise about what
#      that means here, because an earlier version of this row said "this
#      guard has no path scoping at all" and that is not quite true: this
#      guard scopes exactly ONE path, node's script, via
#      ALLOW_NODE_SCRIPTS and relative_to_project. Nothing else is path
#      scoped -- `cat /etc/passwd` is allowed, and restricting that is
#      path-guard.sh's job, which does not see Bash. For the one path
#      that IS scoped, a tilde would be vetted as written and run
#      expanded, so check_node rejects a leading `~` there; see the
#      comment on that rejection for why it is worth doing even though
#      the shipping config denies `node ~/x.js` anyway.
#   3. Parameter expansion -- `$VAR`, `${VAR}`, `${VAR:-x}`
#      REJECTED whole-command, by the `$` check.
#   4. Command substitution -- `$(cmd)`, backticks
#      REJECTED whole-command, by the `$` check and by `` ` `` in the
#      forbidden loop.
#   5. Arithmetic expansion -- `$((1+1))`
#      REJECTED whole-command, by the `$` check.
#   6. Process substitution -- `<(cmd)`, `>(cmd)`
#      REJECTED whole-command, by `>(` and `<(` in the forbidden loop,
#      and independently by the bare `>` and `<` entries.
#   7. Word splitting -- splitting expansion results on IFS
#      NOT rejected, and it cannot act. It splits only the results of 3,
#      4 and 5, every one of which is rejected; it never touches literal
#      text. With those rejected, nothing can hand it a word to split.
#   8. Pathname expansion / globbing -- `*`, `?`, `[a-z]`
#      NOT rejected in general, and it cannot be: the guard is quote-blind
#      and `find services -name "*.py"` is core usage, so rejecting `*`
#      would reject the tools' own quoted patterns too. Three separate
#      things therefore have to hold, and they are discharged in three
#      different ways:
#
#      (a) It must not inject an OPTION. A glob expands only to names
#          that already exist, so it can produce a word starting with `-`
#          only if such a file already exists in the globbed directory.
#          Verified both ways: with a file named `-i` deliberately
#          created, `*` does expand to `-i`; no such file existed in the
#          worktree or /tmp when this was checked. This is the one bound
#          resting on the FILESYSTEM rather than on this guard, and the
#          reason it holds is that the agent has no write primitive with
#          which to create such a file -- which is what the rest of this
#          file is for. Treat the filesystem check as evidence from one
#          point in time; the argument, not the check, is the bound.
#
#          FOR A LITERAL POLICY THIS BOUND IS REPLACED, NOT RELIED ON.
#          Its argument fails for an agent that CAN write: a coder could
#          create a file named `--pre=sh` and run `rg x *`, and bash would
#          hand rg `--pre=sh`. Under LITERAL_ONLY='1' no `*`, `?`, `[` or
#          `]` reaches bash at all, so no glob expands and the filesystem
#          plays no part. Any policy fencing an agent with Edit or Write
#          must be literal for this reason alone.
#      (b) It must not inject a SECOND COMMAND. Expansion results are not
#          re-scanned as shell syntax: files named `a;b` and `c|d` are
#          passed as single arguments, not as separators (verified).
#      (c) It must not change a path that a rule vetted BY VALUE. This
#          one is not bounded by argument -- it is REJECTED, in
#          check_node, which is the only rule that matches a path value
#          (the only caller of matches_any). A glob character in node's
#          script path is denied there, because a glob on
#          ALLOW_NODE_SCRIPTS would otherwise let a glob token be
#          approved and expanded afterwards (verified, and denied now).
#          Every other value comparison in the file is exact-string
#          `in_list`, where a glob character simply fails to match and so
#          fails closed by itself.
#   9. Quote removal
#      MODELLED rather than rejected: normalize_token deletes `'`, `"`
#      and `\` so the rules compare what the tool will actually receive.
#      See TOKEN NORMALIZATION next, and the two text divergences under
#      POSITIONAL RULES where the model and bash still differ. Under
#      LITERAL_ONLY='1' it is REJECTED instead: no quote or backslash
#      survives the literal check, so there is nothing to remove, and a
#      leading tilde (row 2) is rejected too.
#
# Not expansions, but the same class of after-the-fact rewriting, for
# completeness: alias and history expansion are both off in the
# non-interactive shell the Bash tool uses (verified: expand_aliases
# off, histexpand off), and an alias needs an `=` the argv[0] rule denies
# in any case. Redirection is rejected by the forbidden loop. `;`, `|`,
# `&&` and `||` are handled by segment splitting, a bare `&` is
# rejected, and a newline is rejected.
#
# TOKEN NORMALIZATION (load-bearing, not cosmetic): every `'`, `"` and `\`
# character is deleted from a token before ANY comparison this script
# makes -- the argv[0] `=` and `/` checks, the ALLOW_CMDS lookup, every
# per-command option rule, the git subcommand lookup and the node script
# path match all run on the normalized token, not on the text as typed.
# Bash performs quote removal before a command ever sees its arguments, so
# `sed -"i"`, `sed "-i"`, `sed -i""`, `find -exe"c"` and `sort -"o"` are
# simply `sed -i`, `find -exec` and `sort -o` by the time they run.
# Matching the text as typed let any partially quoted spelling of a denied
# option walk straight through the option rules and write files, which is
# the one thing this guard exists to prevent. Deleting quote and backslash
# characters can only shorten a token, so where it diverges from bash's
# own quote removal it errs towards a name bash could not have executed
# at all, never towards letting a denied binary through. The divergence
# needs the backslash itself to be quoted: `'c\at'` stays `c\at` for bash
# and is not a command, while it normalizes to `cat` here. (Unquoted
# `c\at` is no divergence at all -- bash's own quote removal makes that
# `cat` too, exactly as this function does.) That reasoning is about
# command NAMES and option spellings. For sed's script, where the token
# is compared against a shape rather than a list, the same deletion
# produces a text divergence that is documented as divergence 2 under
# POSITIONAL RULES below.
#
# OPTION SPELLING: a rule that names one spelling of an option catches
# almost nothing, because the tools accept several. Three spellings are
# handled explicitly, each verified against the binaries installed here:
#
#   * Short-option clusters are walked letter by letter, because getopt
#     reads them that way: `sort -no FILE` still writes FILE even though
#     the token does not start with `-o`, and attached values
#     (`sort -o/tmp/x`) count too. sort and file use short_cluster_has,
#     which stops at the first letter whose argument swallows the rest of
#     the token. sed is stricter and uses neither: every letter of a
#     cluster must be on its valueless allowlist, so `sed -ni` is denied
#     for containing a letter that is not on the list, not for being
#     recognized as in-place editing.
#   * Long options are matched as PREFIXES wherever a rule names one,
#     because GNU getopt_long and git's parse-options accept any
#     unambiguous abbreviation: `sort --o out` writes a file and
#     `sed --in FILE` rewrites one in place (both verified). sort, git,
#     node, rg and file use that prefix matcher -- see long_opt_matches.
#     sed does NOT, and the example above is deliberately kept because
#     the sed behaviour is what makes the point: sed reaches the same
#     result by the stricter route of an exact-match allowlist
#     (sed_option_allowed), so `--in` is refused there for not being on
#     the list, rather than for abbreviating `--in-place`.
#   * `--` ends option parsing, which moves where a tool's OPERANDS
#     start. Only the three rules that care about operand position
#     (sed's script, git's subcommand, node's script path) treat it
#     specially; see each check_* for what it does and why. The option
#     rules otherwise keep scanning past `--`, which can only over-deny
#     an operand that looks like a denied option, and that fails closed.
#     There is one exception, and it follows from the positional rule
#     rather than weakening it: check_sed claims the first token after a
#     `--` as the script before its option rule sees it, so
#     `sed -n -- -i 1p FILE` never reaches sed_option_allowed at all. It
#     is still denied, because the print-only shape admits only `N[,M]p`
#     and `/re/p` and therefore rejects every `-`-prefixed token anyway
#     -- and denied with the right diagnosis, since `-i` is exactly what
#     sed takes as its script there. Scanning every token also covers
#     GNU's argument permutation, where an option placed after an operand
#     still applies (`sed 's/a/b/' FILE -i` edits in place; verified).
#
# No rule waives a check because some flag is PRESENT. That pattern was
# tried once (accepting any sed script when `--sandbox` appeared) and it
# broke exactly as this kind of rule always does: `sed -n --
# '1e printf PWNED' --sandbox FILE` put `--sandbox` after `--`, where sed
# reads it as a filename and never enables sandbox mode, while the guard
# read it as an option and waived the script check. A rule of the form
# "allow because a good flag is present" puts the boundary inside the
# guarded binary's parser; only "deny because a bad token is present"
# keeps it here.
#
# Outside literal mode that rule is absolute. Inside it there are exactly
# two present-flag rules, both from ADR-0018: `ruff format` is read-only
# when `--check` or `--diff` is present (decision 7), and `git merge` is
# allowed only when `--ff-only` is present (decision 9). They are sound
# there, and only there, because all three of these hold, and the sed
# failure above broke the second:
#
#   * literal mode makes the word the guard sees the word the tool gets;
#   * `--` is refused by both rules, so the flag cannot be demoted to an
#     operand;
#   * every option either rule allows is valueless, so nothing can
#     consume the flag as its value.
#
# Drop any one of those and the rule must go with it.
#
# POSITIONAL RULES: three rules do still depend on WHICH token is an
# operand rather than an option -- sed's script, git's subcommand and
# node's script path. Such a rule is sound only when every option that
# could consume the next token is already denied; otherwise the tool eats
# what the guard called an operand as an option's VALUE, and the guard is
# left reasoning about the wrong token. An earlier version of this file
# got that wrong twice, in both cases allowing arbitrary command
# execution (both verified, not theoretical):
#
#   git --namespace log -c diff.external=SCRIPT diff HEAD~1
#       git consumes `log` as the value of --namespace, so the guard
#       booked `log` as the subcommand, found it allowed, and switched
#       off its pre-subcommand denials while git went on to parse `-c`.
#   sed -l 1,40p '1e printf PWNED' FILE
#       GNU sed accepts `1,40p` as the -l value (strtoul stops at the
#       comma without complaining), so the guard checked the print-only
#       shape against `1,40p` while sed ran `1e printf PWNED`.
#
# Enumerating the value-taking options that could do this failed twice,
# because the enumeration only has to miss one. All three rules are
# therefore INVERTED now: a token that this guard treats as an OPTION
# before the operand is permitted only if it is on a short allowlist of
# known-safe VALUELESS options, and is denied otherwise. No permitted
# option consumes the next token, so nothing can shift which token lands
# in the operand slot -- by construction rather than by enumeration. The
# allowlists are exact-match for long options and per-letter for short
# clusters, so an abbreviation of anything at all is denied by default
# instead of being resolved by guesswork about how the tool's parser
# would disambiguate it. node needs no allowlist because it already
# denies every option-shaped token before the script path, which is the
# same property by a stricter route.
#
# The ADR-0018 rules need no position argument of this kind. uv's target
# is the first word without a dash because every uv option allowed
# before it is valueless; pytest, ruff and git add/commit/merge check
# every word, whether or not the tool will read it as some option's
# value; and make takes exactly one word. Their option allowlists are
# exact-match, so no abbreviation is resolved by guesswork there either.
#
# "Treats as an option" is deliberately not the same as "starts with a
# dash", because the tools do not agree that it is either. A bare `-` is
# an OPERAND to sed -- sed reads it as its script, or as the stdin file
# once it has a script -- so check_sed classifies it as one too, which is
# why `sed - 1,40p FILE` is denied (the script is `-`, which is not a
# print-only script, and sed likewise errors on it) while
# `sed -n 1,40p -` is allowed. git and node have no such case: a bare `-`
# is not meaningful to either in that position and both deny it.
#
# The goal behind all of this is that the guard's model of where the
# operand is should match the tool's, whichever way that cuts, because a
# disagreement means the guard vetted a different token from the one the
# tool will act on. For sed's script slot that was checked exhaustively,
# token class by token class, against GNU sed 4.9: an allowed valueless
# option, a safe short cluster, the first `--`, a second `--`, a bare `-`
# before or after the script, an empty token, a denied option before or
# after the script, `--` after the script, and a second candidate script.
# In every one of those the guard's script token is the token sed uses.
# Two of them were found by getting it wrong first: `-e`/`-f` would make
# sed's first operand a FILE rather than the script (`sed -n -e 1p 2p f`
# reports "can't read 2p"), which is why the option allowlist has to
# exclude them, and only the FIRST `--` ends option parsing, so a second
# one is the script and not a separator. Those are all POSITION rows --
# which token. The two rows where the token is the same but its exact
# characters are not follow next.
#
# TWO text divergences remain in sed's script slot. Read that count as a
# statement about what is left, not as a claim that nothing else was ever
# wrong: brace expansion was a third divergence of an entirely different
# kind, between the guard's string and the string bash hands the tool, it
# did NOT fail closed -- it was a live write-and-exec bypass -- and it is
# now rejected outright rather than reasoned about (see BASH EXPANSIONS).
# What follows is scoped to the two remaining cases, both of them text
# rather than position: the guard and sed pick the same token as the
# script but not always the same characters in it. Both were run; neither
# can be turned into a write or an exec, and the bound is narrow enough
# to state exactly.
#
#   1. Whitespace joining. Segment splitting is whitespace-based and
#      quote-blind, so where bash joins a word -- `sed -n 1p\ w/tmp/x f`
#      passes sed the single script `1p w/tmp/x` -- the guard sees only
#      `1p` and shape-checks that. Here its script is a prefix of sed's.
#   2. Token normalization. normalize_token deletes every `'`, `"` and
#      `\`, which is load-bearing for the option rules (see TOKEN
#      NORMALIZATION above), but bash keeps those characters when they
#      are themselves quoted. So `sed -n '"1p"' FILE` is allowed: the
#      guard shape-checks `1p` while sed's script is literally `"1p"` and
#      it dies with "unknown command: `\"'". Here the guard's script is
#      NOT a prefix of sed's -- it is sed's with those characters
#      deleted, wherever they sat.
#
# Why neither reaches a write or an exec. The print-only shape admits
# only `N[,M]p` and `/re/p`, so the only letters the guard can have
# approved are `p` and whatever sits inside the regex of `/re/p`. Neither
# divergence can introduce a letter: joining appends text that sed then
# refuses, and normalization only ever DELETES `'`, `"` and `\`, so every
# letter and every `/` in sed's script is also in the guard's, in the
# same order. That is the crux, because `w`, `W`, `r`, `R`, `e` and the
# `s` command are all letters -- none of them can appear in sed's script
# unless it already got past the shape check. A letter inside `/re/p`
# could only become a command if the address delimiters were re-cut,
# which needs a `/` (never inserted -- the positions are identical) or a
# `\` escaping a delimiter, and that yields "unterminated address regex".
# And sed's only command separators, `;` and a newline, never reach
# check_sed at all: `;` splits the command into segments that must each
# pass on their own, and a newline anywhere rejects the whole command.
# Everything else appended or re-inserted is the same print, a parse
# error ("extra characters after command", "unexpected `}'",
# "unterminated address regex") or a `#` comment. All of those were run:
# nothing was written. So both divergences are real, bounded and fail
# closed. This is not a claim that the two models always agree on the
# script's text, and `sed -n '"1p"' FILE` being allowed is a recorded
# consequence of divergence 2, not a shape to rely on.
#
# FOR A LITERAL POLICY NEITHER DIVERGENCE CAN OCCUR, and that replaces
# the bound above rather than leaning on it. Divergence 1 needs a word
# joined across a blank, which takes a quote or a backslash; divergence 2
# needs a quote or backslash that bash keeps. LITERAL_ONLY='1' refuses
# all three characters, so normalize_token is a no-op and the token every
# rule reasons about is, character for character, the token the tool
# gets. That is not merely tidier: the ADR-0018 rules RELAX or VET on
# what they see ("read-only because --check is present", "this pytest
# operand is a test file"), and for such a rule a quote-blind divergence
# would fail OPEN -- in `ruff format x\ --check .` the guard would see
# `--check` while ruff got the one operand `x --check` and wrote files.
# That is why those rules exist only in literal mode.
#
# Caveat (documented, not a bug): this guard inspects the command STRING.
# It is default-deny and it blocks the obvious and the moderately clever,
# but a command-string guard is not a kernel sandbox and must not be
# described as one. Its guarantee is also only as strong as the binaries
# on the allowlist: every allowed command must itself be incapable of
# writing files or executing arbitrary code, or be fenced by a rule here.
#
# KNOWN COMMANDS (ADR-0018 decision 4). The script carries the list of
# commands it knows how to vet: KNOWN_RULE_CMDS (`find git node rg sed
# sort file uv make`, each with a rule) and KNOWN_READONLY_CMDS (`ls cat
# head tail wc stat grep jq diff cmp pwd`, read-only by construction). A
# command on a policy's ALLOW_CMDS that is on neither is refused with a
# configuration error, whatever its arguments. So adding `awk`,
# `python3`, `perl`, `xargs`, `env`, `tee` or any shell to ALLOW_CMDS no
# longer voids the guarantee silently -- it gets that command refused.
# What voids the guarantee now is adding a name to one of those two lists
# (or a target to UV_RUN_KNOWN_TARGETS) without the rules that make it
# safe: one such entry can do everything the rest of this script exists
# to prevent.
#
# Two known limits are not fixable at the token level: `git` executes
# programs named by its on-disk configuration (core.pager, core.editor,
# diff.external, textconv filters) even with `-c` blocked on the command
# line, so it is only as safe as the config in the repository it runs in;
# and the guard vouches for WHICH script `node` runs, never for what that
# script does. Keep the Bash tool off any agent whose boundary must be a
# hard one.
#
# FOR AN AGENT THAT WRITES CODE THIS IS A TRIPWIRE, NOT A BOUNDARY
# (ADR-0018 decision 1). No Bash policy can stop such an agent from
# running arbitrary code, and a policy that lets it run the gates does
# not try. pytest imports the test modules, the conftest files and the
# implementation they import; mypy loads the plugins its configuration
# names; make runs whatever the Makefile recipe says; uv installs what
# pyproject.toml and uv.lock describe. The agent writes that
# implementation and may edit that configuration, so it can put code in
# one of those files and run a gate, and nothing in `uv run --locked
# pytest -q` shows what that will execute. What such a policy does
# instead: it refuses every route that needs no file edit first
# (interpreters, heredocs, installs, network clients, options that write
# files or load code, launchers pointed at anything but the named tools,
# commands this script has no model for); together with the Edit/Write
# fence it makes the remaining routes leave evidence in `git diff` or
# `git status`; and in stop-and-report mode every refusal says that
# reaching the same effect another way is circumvention. It does not see
# what a gate executes, it does not preserve evidence (an edit, a gate run
# and a revert leave nothing in the files), it does not confine Edit or
# Write to the worktree, and it does not restrict reading. Only an
# OS-enforced sandbox would be a boundary.

set -f -e -u -o pipefail

input="$(cat)"
tool_name="$(printf '%s' "$input" | jq -r '.tool_name // empty')"
command_str="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty')"
agent_type="$(printf '%s' "$input" | jq -r '.agent_type // empty')"

# DENY_ADVICE (ADR-0018 decision 11). Unset, empty or `needs-validation`
# keeps every message exactly as it was. Any other value -- including a
# misspelling -- means `stop-and-report`, failing towards the stricter
# advice: every denial then ends with FINAL_PARAGRAPH after one space.
stop_and_report=0
case "${DENY_ADVICE:-}" in
  "" | needs-validation) ;;
  *) stop_and_report=1 ;;
esac

FINAL_PARAGRAPH="This refusal is final for this task. Do not retry the same effect another way: not with a different spelling, quoting or option order, not with another program or interpreter, and not by writing a script, test, config file or Makefile target and then running a command that picks it up. Each of those is circumventing this guard, whatever the intent, and must be reported as such. If this message names a supported form and that form does what you need, use exactly that form. Otherwise stop the part of your work that needs this, finish anything that does not, and put in your report: the command you ran, what you needed it for, and this refusal word for word. Whoever dispatched you decides what happens next."

# Every refusal in this file goes through here, which is what makes the
# paragraph reach every denial in stop-and-report mode, configuration
# errors and the shared rules included.
deny() {
  local reason="$1"
  if (( stop_and_report )); then
    reason="${reason} ${FINAL_PARAGRAPH}"
  fi
  jq -n --arg reason "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 2
}

[[ "$tool_name" == "Bash" ]] || exit 0

# Scope routing -- deliberately NOT a check. This hook has to be wired
# session-wide (see WIRING in the header), so it sees Bash calls from
# callers this policy was never written for. When SCOPE_AGENT_TYPES is
# set, only the listed agents are policed and everyone else passes
# through untouched: an absent agent_type (a top-level Bash call carries
# none) or one that is not on the list means "not my business", not
# "deny". Unset behaves as it always has and polices every Bash call
# that reaches this hook, which is what the test suite exercises.
#
# This test deliberately sits first, before the allowlist and the
# command are even looked at, so that an out-of-scope caller costs
# nothing and cannot be affected by this policy's configuration.
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

# LITERAL_ONLY (ADR-0018 decision 3). Empty or unset is off, `1` is on,
# and anything else is a configuration error that refuses every command:
# a policy that meant to be literal and misspelt the value must not run
# its literal-only rules without the lexical check they depend on. This
# sits before the "no allowlist" exit below so that it fails closed even
# for a policy that is otherwise incomplete.
literal_mode=0
case "${LITERAL_ONLY:-}" in
  "") ;;
  1) literal_mode=1 ;;
  *)
    deny "Hammertime bash guard: configuration error: LITERAL_ONLY is '${LITERAL_ONLY}', and the only valid values are empty and 1. Every command is refused until the policy in .claude/settings.json is corrected; this is not something the calling agent can fix."
    ;;
esac

# No command allowlist means this agent is not guarded on Bash at all.
[[ -n "${ALLOW_CMDS:-}" ]] || exit 0

# --- NUL gate, in every mode (ADR-0018 decision 3, second amendment) ----
# command_str above came through a command substitution, and bash's
# command substitution silently drops NUL bytes. So a command such as
# `uv run --locked ruff format<NUL> --check .` would be vetted here as the
# read-only `... --check .`, while a harness that hands the shell only the
# bytes before the NUL would run `ruff format` in write mode. This gate
# therefore does not trust command_str: it asks jq, over the raw payload,
# whether the DECODED command contains codepoint 0, and reads the answer
# from jq's exit status rather than from any captured string. `explode`
# turns the string into integer codepoints, so the test does not depend
# on how jq stores a NUL inside a string.
#
# Only status 1 (`jq -e` on a clean `false`) passes. Status 0 means a NUL
# was found. Any other status means the check did not complete -- for
# example the runtime error `explode` raises on a command that is a
# number, array or object -- and that fails closed too. The status is
# captured with `|| nul_status=$?` so that neither `set -e` nor an `if`
# condition can turn a jq error into "no NUL". `// ""` makes an absent,
# null or false command the empty string, which passes here and then
# exits 0 at the empty-command check below, as it always did.
nul_status=0
printf '%s' "$input" | jq -e '(.tool_input.command // "") | explode | any(. == 0)' >/dev/null 2>&1 || nul_status=$?
case "$nul_status" in
  1) ;;
  0)
    deny "Hammertime bash guard: the command contains a NUL byte (U+0000), which cannot be carried through this guard intact — the byte is dropped when the command is read, so the guard cannot vet the command that would actually run. The command is refused."
    ;;
  *)
    deny "Hammertime bash guard: the command could not be checked for a NUL byte (the check ended with status ${nul_status} instead of a result), so the guard cannot confirm that the command it would vet is the command that would run. The command is refused."
    ;;
esac

[[ -n "$command_str" ]] || exit 0

# --- Literal mode (ADR-0018 decision 3) ----------------------------------
# One lexical check, before every other check. What it leaves can only be
# words of ordinary characters separated by blanks and by `|`, `;`, `&&`
# and `||`. On such a string bash removes no quotes (there are none) and
# performs no brace, tilde, parameter, command, arithmetic or process
# expansion; there is no expansion result to word-split and nothing to
# glob (extended globs need `(`); it recognises no comment, redirection or
# grouping. So the words this guard splits out below are exactly the argv
# each tool receives, and each segment is exactly one simple command.
#
# The tilde rule is applied to words split on blanks AND on the three
# separator characters, because bash starts a new word after `;`, `|` and
# `&&` whether or not a blank follows: `ls;~` hands `ls` nothing odd but
# runs `~`, a word the plain whitespace split would have missed.
LITERAL_FORBIDDEN_CHARS=('$' '`' '\' "'" '"' '{' '}' '[' ']' '(' ')' '*' '?' '<' '>' '#')
LITERAL_FORBIDDEN_NAMES=(
  "a dollar sign (\$)"
  "a backtick (\`)"
  "a backslash (\\)"
  "a single quote (')"
  "a double quote (\")"
  "an opening brace ({)"
  "a closing brace (})"
  "an opening bracket ([)"
  "a closing bracket (])"
  "an opening parenthesis (()"
  "a closing parenthesis ())"
  "an asterisk (*)"
  "a question mark (?)"
  "a less-than sign (<)"
  "a greater-than sign (>)"
  "a hash sign (#)"
)

# Every C0 control character except tab (a blank, split on exactly as
# bash splits on it) and newline (refused on its own above), plus DEL.
# NUL cannot appear here: the NUL gate has already refused it in every
# mode, and a bash string cannot hold one. The bytes are enumerated one
# by one rather than written as a bracket range, whose meaning depends on
# the locale's collation.
LITERAL_CONTROL_CHARS=(
  $'\x01' $'\x02' $'\x03' $'\x04' $'\x05' $'\x06' $'\x07' $'\x08'
  $'\x0b' $'\x0c' $'\x0d' $'\x0e' $'\x0f'
  $'\x10' $'\x11' $'\x12' $'\x13' $'\x14' $'\x15' $'\x16' $'\x17'
  $'\x18' $'\x19' $'\x1a' $'\x1b' $'\x1c' $'\x1d' $'\x1e' $'\x1f'
  $'\x7f'
)

deny_literal() {
  deny "Hammertime bash guard: the command contains $1. Commands for this agent must be literal: plain words separated by blanks and by |, ;, && or ||, with no quoting, escaping, expansion, glob, redirection, comment, grouping, backgrounding or second line, so that the words this guard checks are exactly the words each program receives. Supported forms: search with the Grep and Glob tools rather than a quoted pattern; write a commit message, trailers included, to .commit-msg with the Write tool and commit with git commit -F .commit-msg; select tests with -k WORD, a single word; and leave out 2>&1, because the Bash tool already captures stderr."
}

check_literal() {
  local cmd="$1" idx word
  local -a words=()
  if [[ "$cmd" == *$'\n'* ]]; then
    deny_literal "a newline"
  fi
  for (( idx = 0; idx < ${#LITERAL_FORBIDDEN_CHARS[@]}; idx++ )); do
    if [[ "$cmd" == *"${LITERAL_FORBIDDEN_CHARS[idx]}"* ]]; then
      deny_literal "${LITERAL_FORBIDDEN_NAMES[idx]}"
    fi
  done
  for (( idx = 0; idx < ${#LITERAL_CONTROL_CHARS[@]}; idx++ )); do
    if [[ "$cmd" == *"${LITERAL_CONTROL_CHARS[idx]}"* ]]; then
      deny_literal "a control character other than tab (for example a carriage return)"
    fi
  done
  if [[ "${cmd//&&/}" == *'&'* ]]; then
    deny_literal "an '&' that is not part of '&&'"
  fi
  local split="${cmd//|/ }"
  split="${split//;/ }"
  split="${split//&/ }"
  read -r -a words <<< "$split"
  for word in ${words[@]+"${words[@]}"}; do
    if [[ "$word" == '~'* || "$word" == *'=~'* || "$word" == *':~'* ]]; then
      deny_literal "the word '${word}', which bash would tilde-expand"
    fi
  done
  return 0
}

if (( literal_mode )); then
  check_literal "$command_str"
fi

matches_any() {
  local value="$1"; shift
  local pattern
  for pattern in "$@"; do
    [[ -z "$pattern" ]] && continue
    if [[ "$value" == $pattern ]]; then
      return 0
    fi
  done
  return 1
}

in_list() {
  local value="$1"; shift
  local item
  for item in "$@"; do
    [[ "$value" == "$item" ]] && return 0
  done
  return 1
}

# Reproduce bash's quote removal for the simple quoting bash actually
# performs here: delete every quote and backslash character anywhere in
# the token, not just a matching surrounding pair. See TOKEN
# NORMALIZATION in the header -- partial quoting (`-"i"`, `-exe"c"`,
# `-\i`) otherwise defeats every option rule below.
normalize_token() {
  local token="$1"
  token="${token//\'/}"
  token="${token//\"/}"
  token="${token//\\/}"
  printf '%s' "$token"
}

# Walk a short-option cluster (`-ni`) the way getopt reads it: letter by
# letter, stopping at the first option whose argument swallows the rest of
# the token. Returns 0 if one of the dangerous letters is reached first.
#   $1 token, $2 dangerous option letters, $3 letters that take an argument
short_cluster_has() {
  local token="$1" bad="$2" stop="$3" idx ch
  [[ "$token" == -[!-]* ]] || return 1
  for (( idx = 1; idx < ${#token}; idx++ )); do
    ch="${token:idx:1}"
    [[ "$bad" == *"$ch"* ]] && return 0
    [[ "$stop" == *"$ch"* ]] && return 1
  done
  return 1
}

# GNU getopt_long and git's parse-options accept any unambiguous
# abbreviation of a long option, so a rule naming only the full spelling
# matches nothing an attacker would type: `sed --in 's/a/b/' FILE`
# rewrites the file and `sort --o out FILE` writes it (both verified
# here). TOKEN matches CANONICAL when TOKEN, minus any `=value` suffix,
# is a prefix of CANONICAL at least three characters long (`--` plus one
# character). Over-denying an abbreviation that is actually ambiguous is
# fine -- the tool would have rejected it too, and this fails closed.
#
# The containment direction matters and is easy to get backwards:
# CANONICAL must start with TOKEN, never the reverse. `--pre-glob` is not
# an abbreviation of `--pre`, so rg's harmless `--pre-glob` stays allowed
# while `--pre` and `--pre=CMD` are denied. Getting this inverted would
# deny half of every tool's flag set.
long_opt_matches() {
  local token="${1%%=*}" canonical="$2"
  [[ "$token" == --* ]] || return 1
  (( ${#token} >= 3 )) || return 1
  [[ "$canonical" == "$token"* ]]
}

# True when TOKEN abbreviates any of the canonical long options given.
matches_long() {
  local token="$1"; shift
  local canonical
  for canonical in "$@"; do
    if long_opt_matches "$token" "$canonical"; then
      return 0
    fi
  done
  return 1
}

# Normalize a path to the project/worktree root when it sits underneath
# it, the same way path-guard.sh does, so that an absolute path and the
# path as written both get a fair chance against the globs.
project_dir="${CLAUDE_PROJECT_DIR:-$cwd}"
relative_to_project() {
  local path="$1" base
  for base in "$cwd" "$project_dir"; do
    [[ -z "$base" ]] && continue
    base="${base%/}"
    if [[ "$path" == "$base"/* ]]; then
      path="${path#"$base"/}"
      break
    fi
  done
  path="${path#./}"
  printf '%s' "$path"
}

# --- Whole-command rejections -------------------------------------------
# Each of these writes a file or defeats per-segment inspection, so the
# command is rejected outright rather than sanitized. These run on the raw
# command string, deliberately: a `>` inside quotes is rejected too.

if [[ "$command_str" == *$'\n'* ]]; then
  deny "Hammertime bash guard: the command contains a newline, and multi-line shell input can hide a second command from this guard. Send one single-line command per Bash call."
fi

# `&&` is a segment separator (split below); any other `&` backgrounds a
# command or duplicates a file descriptor.
if [[ "${command_str//&&/}" == *"&"* ]]; then
  deny "Hammertime bash guard: the command contains '&', which backgrounds a command or duplicates a file descriptor and so puts it beyond this guard's inspection. Re-run it as a plain foreground pipeline of read-only commands."
fi

# Any `$` at all: command substitution, parameter expansion, arithmetic
# expansion and $'..' quoting all start with it, and this guard can only
# reason about text it can see. Supersedes separate `$(` / `${` checks.
if [[ "$command_str" == *'$'* ]]; then
  deny "Hammertime bash guard: the command contains '\$'. Shell expansion is not available to this agent, because the guard can only inspect what is written in front of it, not what an expansion would turn into. Write the literal path or value you mean instead of \$VAR, \${VAR} or \$(command)."
fi

# Brace expansion, and the reason it has to be a whole-command rejection
# rather than an option rule. bash rewrites `{a,b}` into SEPARATE WORDS
# after this hook has already returned its verdict, so a single token
# that no option rule fired on becomes two tokens no rule ever saw. That
# was a live write-and-exec bypass, verified against the shipping
# allowlist: `find t -name '*.py' {,-delete}` was approved and deleted
# the file, `git log -1 {-p,--output=/tmp/x}` was approved and wrote a
# 212KB file, and `rg hello {--pre,./pre.sh} f.txt` was approved and
# executed the script. No per-command rule can catch this, because the
# dangerous word does not exist yet when the rules run.
#
# Both braces are refused wherever they appear, quoted or not. The guard
# cannot distinguish them: quotes are deleted before any comparison (see
# TOKEN NORMALIZATION), so `'a{2,3}'`, which bash leaves alone, and
# `a{2,3}`, which bash expands, are the same string by the time any rule
# could look. Fail-closed is the only available answer, and the cost is
# real rather than theoretical -- but it is smaller than it first looks,
# and the deny reason has to say so, because that text is read by an
# agent deciding whether to abandon a finding. What is genuinely lost and
# what is not, all of it run rather than reasoned about:
#
#   * Searching for a literal brace still WORKS, via a hex escape, which
#     puts no brace in the command string at all: `rg -n '\x7b\x7d'
#     services` matches `interface{}`. A pattern written with the braces
#     themselves (`'interface\{\}'`) is refused, and so is the
#     character-class dodge `[{]`, since that contains one too -- but the
#     escape is a complete substitute for the search case.
#   * grep needs `-P` for that. grep's default syntax does not implement
#     `\x`: BRE and ERE read `\x7b` as the three letters `x7b`, so
#     `grep '\x7b'` silently matches the WRONG thing instead of failing
#     (verified -- it matched a file containing the text "x7b"). Only
#     `grep -P '\x7b'` matches a brace. A workaround that fails loudly is
#     fine; one that quietly matches something else is a trap, so this
#     distinction belongs in the deny reason.
#   * Regex QUANTIFIERS are genuinely lost. There is no escape route,
#     because a hex escape is a literal: `'a\x7b2,3\x7d'` matches the
#     text `a{2,3}` rather than repeating an `a` (verified). Write the
#     repetition out longhand with `?` -- `aaa?` for `a{2,3}`, `aaaa?a?`
#     for `a{3,5}`. NOT an alternation: `rg 'aa|aaa'` is refused too,
#     because a `|` is a segment separator to this quote-blind guard
#     (verified -- an earlier draft of the deny reason recommended
#     exactly that and would have sent the agent to a second dead end).
#   * jq is barely affected, and an earlier version of this passage was
#     wrong to say object construction "has no brace-free form" -- that
#     generalised from two routes that do not work to all routes, which
#     is the same mistake in the opposite direction. Reading is
#     unaffected (`.name`, `.a.b`, `to_entries`), and objects can be
#     built without a brace by the functions that return one:
#     `jq -c 'with_entries(select(.key=="a"))'` and `jq -c 'del(.b)'`
#     both emit objects and are both allowed here (verified). What does
#     not work: a `{` escape yields a brace inside a string value
#     rather than jq syntax, `from_entries` will not take plain pairs,
#     and the `to_entries | map(...) | from_entries` pipeline is refused
#     by this guard over the `|` rather than over any brace (all
#     verified). Only jq's literal `{...}` object syntax is unavailable.
#
# If even that is not good enough for some future agent, the answer is
# not to relax this check but to give that agent a tool whose arguments
# do not pass through a shell.
if [[ "$command_str" == *'{'* || "$command_str" == *'}'* ]]; then
  deny "Hammertime bash guard: the command contains a brace. bash expands a brace list such as {-p,--output=/tmp/x} into separate words AFTER this guard has inspected the command, so a brace can carry an option past every rule here -- it was a real bypass, not a hypothetical one. Braces are refused whether or not they are quoted, because quotes are stripped before any comparison and the guard therefore cannot tell an expanding brace from a literal one. You can still do almost everything, with three workarounds, all verified: to SEARCH for a literal brace use a hex escape, which puts no brace in the command -- rg -n '\\x7b\\x7d' services does match interface{} -- and with grep you must add -P, because grep's default syntax reads \\x7b as the three letters x7b and would silently match the wrong thing rather than failing. For a regex QUANTIFIER there is no escape route, because a hex escape is a literal: 'a\\x7b2,3\\x7d' matches the text a{2,3} rather than repeating an a. Write the repetition out longhand with '?' instead -- 'aaa?' for a{2,3}, 'aaaa?a?' for a{3,5}. Do not reach for an alternation: a '|' in the pattern is a segment separator to this guard, which is quote-blind, so 'aa|aaa' is refused as well. For jq, read with path expressions such as 'jq .name' or 'jq .a.b', enumerate fields with 'jq to_entries', and build a reduced object without any brace using the functions that return one: 'jq -c with_entries(select(.key==\"a\"))' and 'jq -c del(.b)' both emit objects and are both allowed here (verified). The 'to_entries | map(...) | from_entries' pipeline works in jq but this guard refuses it over the '|', not the braces. If some finding genuinely needs a construct none of that covers, report it as needs-validation with the exact command a human should run."
fi

for forbidden in '<<<' '<<' '>>' '>(' '<(' '>' '<' '`'; do
  if [[ "$command_str" == *"$forbidden"* ]]; then
    deny "Hammertime bash guard: the command contains '${forbidden}', which redirects output into a file or substitutes text this guard cannot inspect. Re-run it as a plain pipeline of read-only commands with no redirection or substitution, and put any output you need into your report rather than into a file."
  fi
done

# --- Split into segments ------------------------------------------------
# Order matters: `&&` and `||` must be consumed before the single `|`.

segments="${command_str//&&/$'\n'}"
segments="${segments//||/$'\n'}"
segments="${segments//|/$'\n'}"
segments="${segments//;/$'\n'}"

# --- Per-command option rules -------------------------------------------
# Every $@ these see has already been through normalize_token.

# `sed` is the one allowed command whose ARGUMENT is itself a program, and
# GNU sed's script language can write files (`w FILE`, `s///w FILE`) and
# execute shell commands (`e`, `s///e`) with no option this guard could
# key off: `sed -n w/tmp/x FILE` writes /tmp/x and
# `sed -n '1e printf PWNED' FILE` runs printf. Both were verified against
# GNU sed 4.9. There is no sound way to validate an arbitrary sed script
# from token text, so sed is allowed in ONE shape: a print-only script,
# `N,Mp` / `Np` (`sed -n 1,40p FILE`) or `/re/p` (`sed -n /warn/p FILE`),
# with -i, -e and -f -- and their clusters and abbreviations -- denied.
#
# An earlier version had a second arm that accepted any script when
# `--sandbox` was present, on the grounds that sed itself then refuses
# e/r/w. That arm was REMOVED deliberately, and should not be
# reintroduced: its safety depended on sed parsing `--sandbox` as an
# option, and `sed -n -- '1e printf PWNED' --sandbox FILE` defeats it,
# because after `--` sed reads `--sandbox` as a filename and never enters
# sandbox mode while the guard believed it had. The boundary belongs in
# this guard, not inside the binary it is guarding. If richer sed is ever
# needed, drop `sed` from that agent's ALLOW_CMDS instead.
#
# sed's options are handled by an ALLOWLIST rather than a list of
# dangerous spellings, which is what makes the script-position rule sound
# (see POSITIONAL RULES in the header). Only valueless options are
# permitted, so no permitted option can swallow the next token and leave
# the guard checking the wrong one. That is not theoretical: GNU sed
# takes `1,40p` as the value of `-l` without complaint, so
# `sed -l 1,40p '1e printf PWNED' FILE` used to pass the print-only check
# on `1,40p` while sed executed `1e printf PWNED` (verified against GNU
# sed 4.9). The dangerous options -i, -e, -f and -l are denied by not
# being on the list, as is every abbreviation and every cluster
# containing one of their letters -- no enumeration of them is needed or
# wanted.
SED_SAFE_LONG_OPTS="--quiet --silent --regexp-extended --separate --unbuffered --null-data --zero-terminated --posix --debug --sandbox --follow-symlinks"
SED_SAFE_SHORT_LETTERS="nErsuz"

# Approval here must always come from a rule that actually ran. An
# earlier version fell out of the short-cluster loop with `return 0`,
# which approved the bare token `-` vacuously -- one character, so the
# loop had nothing to iterate. `-` is not an option to sed at all (see
# check_sed), and nothing else can reach the end of this function.
sed_option_allowed() {
  local token="$1" idx ch
  case "$token" in
    --*)
      # Exact match only. An abbreviation is denied by default rather
      # than resolved by guessing how sed's getopt would disambiguate it.
      in_list "$token" $SED_SAFE_LONG_OPTS
      return
      ;;
    -?*)
      # A short-option cluster. The `?` is load-bearing: it guarantees
      # at least one letter for the loop to check, so the `return 0`
      # below is only reachable after every letter was approved.
      for (( idx = 1; idx < ${#token}; idx++ )); do
        ch="${token:idx:1}"
        [[ "$SED_SAFE_SHORT_LETTERS" == *"$ch"* ]] || return 1
      done
      return 0
      ;;
  esac
  return 1
}

# `--` handling: after a `--`, sed reads the NEXT token as the script even
# if it starts with a dash (verified: `sed -n -- -n FILE` fails with
# "unknown command: `-'"). Only the FIRST `--` does that, for sed and
# here alike, so the script is the single token immediately after it,
# whatever that token is -- including another `--` (verified:
# `sed -n -- -- 1p FILE` makes sed's script `--` and it aborts). A `--`
# once the script is already in hand is getopt's, not an operand, and is
# ignored.
#
# Without any `--`, the script is the first token that is not an option:
# either a token that does not begin with `-`, or a bare `-`, which sed
# reads as a script rather than an option. That is sound because every
# option that could have taken a value -- including -e and -f, which
# would make sed's first operand a file instead of the script -- has
# already been denied by the allowlist above, so nothing can shift which
# token lands in the script slot. See the POSITIONAL RULES note in the
# header for the token-by-token check of that claim against GNU sed, and
# for the two text divergences that remain: the guard and sed always pick
# the same token as the script, but its characters can differ when bash
# joins a word across whitespace, or when normalize_token deletes quotes
# and backslashes that bash itself kept. Both fail closed, for the
# reasons set out there.
check_sed() {
  local token script="" seen_script=0 end_of_opts=0
  for token in "$@"; do
    # This test comes FIRST, before the `--` test below, and the order is
    # the whole of it: once a `--` has ended option parsing, the very next
    # token is the script whatever it is, INCLUDING a second `--`. Testing
    # for `--` first instead consumed every `--`, which made the script
    # the token after the LAST one -- so `sed -n -- -- 1,40p FILE` was
    # allowed on the strength of `1,40p` while sed took `--` as its script
    # and died with "unknown command: `-'". Only the first `--` ends
    # option parsing, for sed and here alike.
    if (( end_of_opts )) && (( ! seen_script )); then
      script="$token"
      seen_script=1
      continue
    fi
    if [[ "$token" == "--" ]]; then
      if (( ! seen_script )); then
        end_of_opts=1
      fi
      continue
    fi
    # Option rules apply at every position, not just before the script:
    # GNU permutes, so `sed 1,40p FILE -i` still edits in place
    # (verified). Past a `--` this can only over-deny an operand that
    # looks like an option, which fails closed.
    #
    # `-?*` and not `-*`: a BARE `-` is not an option to sed, it is an
    # operand. sed reads it as its script (`sed - FILE` dies with
    # "unknown command: `-'") or as the stdin file once a script is
    # already in hand. Skipping it as though it were an option would
    # leave the guard checking the NEXT token as the script while sed
    # checked `-`, i.e. the two disagreeing about which token is the
    # script -- harmless in practice, since sed errors out, but it is
    # exactly the kind of disagreement this guard must not have. Falling
    # through to the operand logic below keeps the two models aligned:
    # `sed - 1,40p FILE` is denied by the print-only shape (the script is
    # `-`), while `sed -n 1,40p -` stays allowed (the script is `1,40p`
    # and `-` is just stdin).
    if [[ "$token" == -?* ]]; then
      if ! sed_option_allowed "$token"; then
        deny "Hammertime bash guard: 'sed ${token}' is not one of the sed options this agent may use. Only options that take no value are allowed (-n/--quiet/--silent, -E/-r/--regexp-extended, -s/--separate, -u/--unbuffered, -z/--null-data, --posix, --debug, --sandbox, --follow-symlinks), because an option that swallows the next token would move what this guard treats as sed's script. That rules out -i (edits files in place), -e and -f (supply a script this guard cannot vet, and a sed script can write files with 'w FILE' and run shell commands with 'e'), and -l. Pass a print-only script as sed's first argument: 'sed -n 1,40p FILE' or 'sed -n /regex/p FILE'."
      fi
      continue
    fi
    if (( ! seen_script )); then
      script="$token"
      seen_script=1
    fi
  done
  if (( ! seen_script )); then
    deny "Hammertime bash guard: this sed command carries no script to run. Give sed a print-only script as its first argument ('sed -n 1,40p FILE' or 'sed -n /regex/p FILE')."
  fi
  if [[ ! "$script" =~ ^[0-9]+(,[0-9]+)?p$ ]] && [[ ! "$script" =~ ^/[^/]*/p$ ]]; then
    deny "Hammertime bash guard: '${script}' is not a print-only sed script. sed's script language can write files ('w FILE', 's///w FILE') and run shell commands ('e', 's///e'), which this guard cannot tell apart from a harmless script, so only a line-range or regex print is accepted: 'sed -n 1,40p FILE' or 'sed -n /regex/p FILE'. Use head, tail, rg or cat to read anything else."
  fi
}

check_sort() {
  local token
  for token in "$@"; do
    case "$token" in
      -o | -o*)
        deny "Hammertime bash guard: 'sort ${token}' writes its output to a file. Drop it and let sort print to stdout."
        ;;
    esac
    if matches_long "$token" --output; then
      deny "Hammertime bash guard: 'sort ${token}' abbreviates --output, which writes sort's output to a file (GNU sort accepts any unambiguous abbreviation, so '--o' is '--output'). Drop it and let sort print to stdout."
    fi
    if matches_long "$token" --compress-program; then
      deny "Hammertime bash guard: 'sort ${token}' abbreviates --compress-program, which runs a program of your choosing to compress sort's temporary files. Drop it."
    fi
    # `sort -no FILE`: bundled, but still writes FILE.
    if short_cluster_has "$token" "o" "kStT"; then
      deny "Hammertime bash guard: 'sort ${token}' bundles the output flag -o into a short-option cluster, which still writes sort's output to a file. Drop it and let sort print to stdout."
    fi
  done
}

# find is the one tool here that does NOT use getopt: its predicates are
# whole words matched exactly, there is no clustering, no `=value` form
# and no abbreviation (`-dele` is an error, not `-delete`), so exact
# matching is the right shape for these rules.
check_find() {
  local token
  for token in "$@"; do
    case "$token" in
      -delete | -exec | -execdir | -ok | -okdir | -fprint | -fprint0 | -fprintf | -fls)
        deny "Hammertime bash guard: 'find ${token}' deletes files or runs another command for each match. Use find only to select paths (-name, -type, -path) and pipe the result into an allowed read-only command."
        ;;
    esac
  done
}

# `--` handling: git rejects `git -- log` outright ("unknown option: --",
# verified), so `--` before a subcommand does not make the next token a
# subcommand; the command simply names none and is denied. After the
# subcommand, `--` is the ordinary pathspec separator and is left alone,
# which keeps `git log -- FILE` working.
#
# Git's GLOBAL options -- the ones before the subcommand -- are handled by
# an ALLOWLIST of valueless flags, and every other dashed token in that
# position is denied. This is the fail-closed inversion of a rule that
# broke twice while it enumerated dangerous globals instead. The reason
# enumeration cannot work here is positional, not a matter of finding the
# right list: a global that takes a VALUE swallows the next token, so
# `git --namespace log -c diff.external=SCRIPT diff HEAD~1` had git
# consume `log` as the value of --namespace while the guard booked `log`
# as the subcommand, declared itself past the pre-subcommand checks, and
# let `-c` through to execute SCRIPT. Verified; it really did run.
#
# With the allowlist, no option that takes a value can precede the
# subcommand at all, so the first non-dash token really is the
# subcommand and the position check underneath it is sound by
# construction. Post-subcommand behaviour is unchanged: `-c` there is the
# harmless combined-diff flag, `-p` is --patch, and `git log -h` prints
# plain usage, so all three stay allowed.
GIT_SAFE_GLOBAL_OPTS="--no-pager --bare --literal-pathspecs --icase-pathspecs --no-replace-objects --no-optional-locks -h"

check_git() {
  local token subcmd="" saw_ddash=0 pos=0 subcmd_pos=0
  for token in "$@"; do
    pos=$(( pos + 1 ))
    # --help launches a manual viewer, or with help.format=web the
    # program named by web.browser, wherever it appears in the command --
    # `git log --help` does it just as `git --help log` does.
    if matches_long "$token" --help; then
      deny "Hammertime bash guard: 'git ${token}' abbreviates --help, which makes git launch a manual viewer (and, with help.format=web, the browser named by web.browser). Use 'git <subcommand> -h' for plain usage text on stdout, or read the documentation outside this agent."
    fi
    case "$token" in
      -o | -o*)
        deny "Hammertime bash guard: 'git ${token}' writes its output to a file. Drop it and read git's output from stdout."
        ;;
      -O*)
        deny "Hammertime bash guard: 'git ${token}' hands git's results to a program of your choosing, which executes that program. Drop it and read git's output from stdout."
        ;;
    esac
    if matches_long "$token" --output; then
      deny "Hammertime bash guard: 'git ${token}' abbreviates --output, which writes git's output to a file. Drop it and read git's output from stdout."
    fi
    if matches_long "$token" --open-files-in-pager; then
      deny "Hammertime bash guard: 'git ${token}' abbreviates --open-files-in-pager, which hands git's results to a program of your choosing and executes it. Drop it and read git's output from stdout."
    fi
    if matches_long "$token" --upload-pack --receive-pack; then
      deny "Hammertime bash guard: 'git ${token}' names a program for git to execute for a transfer. This agent does not run git transfers; drop it."
    fi
    if [[ -z "$subcmd" ]] && (( ! saw_ddash )); then
      if [[ "$token" == "--" ]]; then
        saw_ddash=1
        continue
      fi
      if [[ "$token" == -* ]]; then
        if ! in_list "$token" $GIT_SAFE_GLOBAL_OPTS; then
          deny "Hammertime bash guard: 'git ${token}' is not one of the git global options this agent may use before a subcommand. Only these are, and only in exactly this spelling: ${GIT_SAFE_GLOBAL_OPTS}. Anything else is refused because a global option that takes a value (--namespace, --work-tree, --git-dir, -c, -C, --exec-path and friends) swallows the next token, which would move where the subcommand is and let configuration that names an executable program -- core.pager, core.editor, diff.external -- through unchecked. Put your flags after the subcommand instead, where 'git log -p' and 'git show -c' are fine."
        fi
        continue
      fi
      subcmd="$token"
      subcmd_pos=$pos
    fi
  done
  if [[ -z "$subcmd" ]]; then
    deny "Hammertime bash guard: this git command names no subcommand. Name an allowed read-only subcommand explicitly (ALLOW_GIT_SUBCMDS: ${ALLOW_GIT_SUBCMDS:-none})."
  fi
  if ! in_list "$subcmd" ${ALLOW_GIT_SUBCMDS:-}; then
    if (( stop_and_report )); then
      # ADR-0018 decisions 9 and 11: the auditor's wording, reworded
      # without its read-only framing, naming the one sanctioned
      # alternative there is (for `branch`).
      deny "Hammertime bash guard: 'git ${subcmd}' is not an allowed git subcommand for this agent (ALLOW_GIT_SUBCMDS: ${ALLOW_GIT_SUBCMDS:-none}). To learn the current branch, use git rev-parse --abbrev-ref HEAD or git status; no other git subcommand has a supported substitute."
    fi
    deny "Hammertime bash guard: 'git ${subcmd}' is not an allowed git subcommand for this agent, because it can mutate the repository, the index or the working tree. Use a read-only subcommand instead (ALLOW_GIT_SUBCMDS: ${ALLOW_GIT_SUBCMDS:-none})."
  fi
  # The three writing subcommands (ADR-0018 decision 9). Each is an
  # allowlist over EVERY word after the subcommand, none skipped as an
  # option's value, and each is sound only in literal mode, where the word
  # checked here is the word git receives.
  case "$subcmd" in
    add | commit | merge)
      require_literal "git ${subcmd}"
      shift "$subcmd_pos"
      "check_git_${subcmd}" "$@"
      ;;
  esac
  return 0
}

GIT_ADD_OPTS="-A --all -u --update -N --intent-to-add -v --verbose -n --dry-run"

check_git_add() {
  local token
  for token in "$@"; do
    [[ "$token" == -* ]] || continue
    in_list "$token" $GIT_ADD_OPTS && continue
    [[ "$token" =~ ^-[AuNvn]+$ ]] && continue
    deny "Hammertime bash guard: 'git add ${token}' is not one of the git add options this agent may use. Only these are, matched exactly: ${GIT_ADD_OPTS}, or a cluster of the letters AuNvn. Anything else -- -f/--force, -p, -i, -e, --chmod, --pathspec-from-file, -- and the rest -- is refused. Stage files by naming their paths: git add PATH."
  done
  return 0
}

GIT_COMMIT_OPTS="-a --all -q --quiet -m -F"

# A short cluster is read letter by letter as git reads it: zero or more
# of `a` and `q`, then optionally one `m` or `F`, whose value is the rest
# of the word or, if nothing is left, the next word. That next word is
# still checked by this loop like any other (values are never skipped),
# so a message word beginning with `-` is refused -- a documented
# over-denial, and the reason `-m -n` cannot smuggle in --no-verify.
git_commit_option_allowed() {
  local token="$1" idx=1 ch
  in_list "$token" $GIT_COMMIT_OPTS && return 0
  [[ "$token" == --message=* || "$token" == --file=* ]] && return 0
  [[ "$token" == --* ]] && return 1
  (( ${#token} >= 2 )) || return 1
  while (( idx < ${#token} )); do
    ch="${token:idx:1}"
    case "$ch" in
      a | q) idx=$(( idx + 1 )) ;;
      m | F) return 0 ;;
      *) return 1 ;;
    esac
  done
  return 0
}

check_git_commit() {
  local token
  for token in "$@"; do
    [[ "$token" == -* ]] || continue
    git_commit_option_allowed "$token" && continue
    deny "Hammertime bash guard: 'git commit ${token}' is not one of the git commit options this agent may use. Only these are: -a/--all, -q/--quiet, -m, -F, --message=, --file=, and short clusters of a and q ending in m or F. Everything else -- -n/--no-verify, --amend, -e, -S/--gpg-sign, -C, -c, -p, --trailer, --author, -- and the rest -- is refused, and a message word beginning with '-' is refused too. Write the message, trailers included, to .commit-msg with the Write tool and commit with git commit -F .commit-msg."
  done
  return 0
}

check_git_merge() {
  local token ff_only=0
  for token in "$@"; do
    [[ "$token" == -* ]] || continue
    case "$token" in
      --ff-only) ff_only=1 ;;
      -q | --quiet) ;;
      *)
        deny "Hammertime bash guard: 'git merge ${token}' is not one of the git merge options this agent may use. Only --ff-only, -q and --quiet are, and --ff-only is required: git merge --ff-only REF."
        ;;
    esac
  done
  if (( ! ff_only )); then
    deny "Hammertime bash guard: git merge is allowed for this agent only as a fast-forward: git merge --ff-only REF."
  fi
  return 0
}

# `--` handling: node treats `--` as the end of its own options and runs
# the next token as the script (verified: `node -- script.js arg` runs
# script.js), so the script path is the first token after `--`, whatever
# it looks like. It still has to be on ALLOW_NODE_SCRIPTS.
#
# Positional soundness (the question that caught sed and git): can a
# value-taking node option move the token this function treats as the
# script? No, and by a stricter route than the allowlists those two
# needed. The loop below denies EVERY option-shaped token that appears
# before the script path -- not a list of dangerous ones, anything
# starting with `-` -- so a value-taking option such as `--import X`,
# `--require X` or `--loader X` is refused at the option itself and never
# reaches the point where its value could be mistaken for the script.
# The only other way to reach the script slot is through `--`, and node
# takes the next token as the script there too, which is exactly what
# this code does. Nothing to fix for THAT question; stated explicitly
# because the reasoning is what makes it safe, not the absence of a bug
# report. A different question about this same slot -- whether bash
# rewrites the path after it has been vetted -- did need a fix, and the
# two rejections further down are it.
check_node() {
  local token script="" rel end_of_opts=0
  for token in "$@"; do
    case "$token" in
      -e | -p | -i | -r | --experimental-*)
        deny "Hammertime bash guard: 'node ${token}' evaluates code given on the command line or loads an extra module before the script runs, either of which executes arbitrary code. node may only run one of its approved scripts, with none of -e/--eval, -p/--print, -i/--interactive, -r/--require or --experimental-*."
        ;;
    esac
    if matches_long "$token" --eval --print --interactive --require; then
      deny "Hammertime bash guard: 'node ${token}' evaluates code given on the command line or loads an extra module before the script runs, either of which executes arbitrary code. node may only run one of its approved scripts, with none of -e/--eval, -p/--print, -i/--interactive, -r/--require or --experimental-*."
    fi
    if [[ -z "$script" ]]; then
      if (( end_of_opts )); then
        script="$token"
      elif [[ "$token" == "--" ]]; then
        end_of_opts=1
      elif [[ "$token" == -* ]]; then
        # Anything option-shaped before the script path is an option to
        # node itself. node's option surface is far wider than the list
        # above -- --import, --loader, --inspect, --redirect-warnings,
        # --cpu-prof-dir and friends all execute code or write files --
        # so the approved invocation carries no node options at all.
        deny "Hammertime bash guard: 'node ${token}' passes an option to node itself, and node's options can evaluate code, preload a module, open a debug port or write files. Invoke node as 'node <approved-script> [script arguments]' with no node options (ALLOW_NODE_SCRIPTS: ${ALLOW_NODE_SCRIPTS:-none})."
      else
        script="$token"
      fi
    fi
  done
  if [[ -z "$script" ]]; then
    deny "Hammertime bash guard: this node call names no script to run, and an interactive or stdin-fed node session can execute arbitrary code. Invoke node with one of its approved scripts (ALLOW_NODE_SCRIPTS: ${ALLOW_NODE_SCRIPTS:-none})."
  fi
  # This is the one rule in the file that vets a PATH by value (the only
  # call site of matches_any), which makes it the one rule where an
  # expansion that rewrites a path matters. Globbing and tilde expansion
  # both happen after this hook returns, so without these two rejections
  # an approved token could be a different path by the time node runs it.
  # Globbing was demonstrably reachable: with a glob on
  # ALLOW_NODE_SCRIPTS, `node .claude/skills/security-audit/*.cjs` was
  # approved -- the glob token matches the glob pattern -- and node then
  # ran whatever it expanded to (verified; denied outright now). With the
  # shipping literal patterns it was already denied, so this closes a
  # latent config rather than a live hole.
  #
  # A leading `~` is rejected for the same reason and on the same
  # evidence, though nothing required it: `node ~/x.js` is approved only
  # if the literal string `~/x.js` is itself on ALLOW_NODE_SCRIPTS
  # (verified -- it is denied when the EXPANDED path is on the list,
  # because the written token is what gets matched). Rejecting it makes
  # this rule's guarantee unconditional -- the string vetted here is the
  # string node receives -- instead of resting on nobody putting a glob
  # or a `~` path in that list. It costs nothing, because the approved
  # scripts are repo-relative literal paths. Only a LEADING `~` expands:
  # bash leaves `--foo=~/x` and `x~/y` alone (verified).
  #
  # Scope note: this applies to the script path only. Arguments after it
  # are the script's own and are not vetted by value, so a glob stays
  # usable there. Everywhere else in this file a value is matched with
  # exact-string `in_list` (ALLOW_CMDS, ALLOW_GIT_SUBCMDS and the two
  # option allowlists), where a glob character cannot match and so fails
  # closed by itself.
  case "$script" in
    *'*'* | *'?'* | *'['*)
      deny "Hammertime bash guard: node's script path '${script}' contains a glob character. bash expands globs after this guard has approved the command, so the path checked here would not be the path node runs -- the guard cannot vet a path that is about to become a different path. Name the script exactly (ALLOW_NODE_SCRIPTS: ${ALLOW_NODE_SCRIPTS:-none})."
      ;;
    '~'*)
      deny "Hammertime bash guard: node's script path '${script}' starts with a tilde, which bash expands after this guard has approved the command, so the path checked here would not be the path node runs. Name the script exactly, as a path relative to the project or a full literal path (ALLOW_NODE_SCRIPTS: ${ALLOW_NODE_SCRIPTS:-none})."
      ;;
  esac
  rel="$(relative_to_project "$script")"
  if ! matches_any "$script" ${ALLOW_NODE_SCRIPTS:-} && ! matches_any "$rel" ${ALLOW_NODE_SCRIPTS:-}; then
    deny "Hammertime bash guard: node may not execute '${script}'. Running the repository's own code would execute target-controlled code, and that needs an OS-enforced sandbox this environment does not provide, so report what you found as needs-validation with the exact command a human should run instead (ALLOW_NODE_SCRIPTS: ${ALLOW_NODE_SCRIPTS:-none})."
  fi
}

check_rg() {
  local token
  for token in "$@"; do
    # `--pre-glob` is NOT an abbreviation of `--pre` -- the canonical has
    # to start with the token, not the other way round -- so it stays
    # allowed. See long_opt_matches.
    if matches_long "$token" --pre --hostname-bin; then
      deny "Hammertime bash guard: 'rg ${token}' runs a program of your choosing, which executes that program. Search the files directly instead."
    fi
  done
}

check_file() {
  local token
  for token in "$@"; do
    case "$token" in
      -C)
        deny "Hammertime bash guard: 'file ${token}' compiles a magic file and writes the result to disk. Drop it; use file only to identify existing files."
        ;;
    esac
    if matches_long "$token" --compile; then
      deny "Hammertime bash guard: 'file ${token}' abbreviates --compile, which writes a compiled magic file to disk. Drop it; use file only to identify existing files."
    fi
    # `file -bC -m FILE` compiles through a bundle (verified).
    if short_cluster_has "$token" "C" "eFfmP"; then
      deny "Hammertime bash guard: 'file ${token}' bundles -C into a short-option cluster, which still compiles a magic file and writes it to disk. Drop it."
    fi
  done
}

# --- ADR-0018 rules: uv, make, pytest, ruff ------------------------------
# All of these are literal-mode rules (decision 3). They reason about the
# exact words a tool receives, and they include present-flag and operand
# rules that are sound only when those words are the words bash hands
# over. A policy that reaches one without LITERAL_ONLY='1' gets a
# configuration error instead of a verdict.

# The rules below that match letters and digits spell the classes out
# rather than using ranges, which some locales stretch beyond ASCII.
ASCII_LETTERS="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

require_literal() {
  if (( ! literal_mode )); then
    deny "Hammertime bash guard: configuration error: this policy allows ${1}, whose rules in this script are sound only in literal mode, but LITERAL_ONLY is not '1'. Every use of ${1} is refused until the policy in .claude/settings.json sets LITERAL_ONLY='1'; this is not something the calling agent can fix."
  fi
  return 0
}

# uv classifies its target before looking it up on PATH: a bare target
# naming a directory with __main__.py in the working directory runs as a
# Python package, and a zipapp file of that name runs as that. make reads
# GNUmakefile, then makefile, before Makefile. So an entry of any type
# with one of these names in the payload's cwd would make an unchanged
# gate command run something else (ADR-0018 decision 5 step 4, decision
# 8). `mypy` is here because `make typecheck` runs it through uv. This is
# defence in depth: the fence is the Edit/Write guard's refusal to create
# these names (decision 12), since a check against the live filesystem
# has a window when a Write and a Bash call are issued together.
SHADOW_NAMES="pytest ruff mypy GNUmakefile makefile"

check_shadowing() {
  local base name
  if [[ -z "$cwd" ]]; then
    deny "Hammertime bash guard: this Bash call carries no working directory, so the guard cannot check it for files that would shadow ${1}. It is refused."
  fi
  base="${cwd%/}"
  for name in $SHADOW_NAMES; do
    if [[ -e "${base}/${name}" || -L "${base}/${name}" ]]; then
      deny "Hammertime bash guard: the working directory contains an entry named '${name}'. With it present, ${1} could run that entry instead of the tracked tool or Makefile, so every uv and make command is refused while it exists. Report it; do not remove or rename it to get past this check."
    fi
  done
  return 0
}

# uv (ADR-0018 decision 5). Only `uv run`, with nothing between `uv` and
# `run`; between `run` and the target only the valueless `--locked` and
# `--offline`, matched exactly, so the first word not beginning with `-`
# is the target by construction; the target must be on both
# ALLOW_UV_RUN_TARGETS and UV_RUN_KNOWN_TARGETS; then the shadow check;
# then the target's own rule over the words after it; and LAST the
# requirement that `--locked` was among uv's options. Last, because that
# denial corrects a form rather than refusing a capability, so it must
# only ever reach a command that is allowed once `--locked` is added.
# `--locked` written after the target is the tool's word, not uv's, and
# the tool's rule refuses it.
UV_RUN_KNOWN_TARGETS="pytest ruff"

check_uv() {
  local -a words=("$@") opts=() rest=()
  local n=$# idx=1 has_locked=0 target="" known

  require_literal "uv"

  for known in ${ALLOW_UV_RUN_TARGETS:-}; do
    if ! in_list "$known" $UV_RUN_KNOWN_TARGETS; then
      deny "Hammertime bash guard: configuration error: ALLOW_UV_RUN_TARGETS lists '${known}', but this script has rules only for these uv run targets: ${UV_RUN_KNOWN_TARGETS}. Every uv command is refused until the policy in .claude/settings.json is corrected; this is not something the calling agent can fix."
    fi
  done

  if (( n == 0 )) || [[ "${words[0]}" != "run" ]]; then
    deny "Hammertime bash guard: 'uv ${words[0]:-}' is refused. The only uv subcommand this agent may use is run, written directly after uv with no uv option before it, so installing, syncing, locking and uv's other subcommands are not available: uv run --locked TARGET, with TARGET from ALLOW_UV_RUN_TARGETS (${ALLOW_UV_RUN_TARGETS:-none})."
  fi

  while (( idx < n )); do
    if [[ "${words[idx]}" == -* ]]; then
      case "${words[idx]}" in
        --locked) has_locked=1 ;;
        --offline) ;;
        *)
          deny "Hammertime bash guard: 'uv run ${words[idx]}' is refused. Between run and the target, uv may be given only --locked and --offline, matched exactly; --locked is required. Options that choose another interpreter, script, project, index, cache or environment, or that skip the lockfile check (--frozen, --no-sync), are not available: uv run --locked TARGET."
          ;;
      esac
      opts+=("${words[idx]}")
      idx=$(( idx + 1 ))
      continue
    fi
    target="${words[idx]}"
    break
  done

  if [[ -z "$target" ]]; then
    deny "Hammertime bash guard: this uv run command names no target. Name one from ALLOW_UV_RUN_TARGETS (${ALLOW_UV_RUN_TARGETS:-none}): uv run --locked pytest -q, for example."
  fi

  if ! in_list "$target" ${ALLOW_UV_RUN_TARGETS:-}; then
    local hint=""
    case "$target" in
      mypy) hint=" Type-checking runs through make typecheck." ;;
      python*) hint=" Tests run through uv run --locked pytest, and type-checking through make typecheck." ;;
    esac
    deny "Hammertime bash guard: 'uv run ${target}' is refused: uv run may only launch a target on ALLOW_UV_RUN_TARGETS (${ALLOW_UV_RUN_TARGETS:-none}). An interpreter, a script file, a URL, stdin and the project's own entry points are not available.${hint}"
  fi

  check_shadowing "uv run ${target}"

  rest=(${words[@]+"${words[@]:idx+1}"})
  case "$target" in
    pytest) check_pytest ${rest[@]+"${rest[@]}"} ;;
    ruff) check_ruff ${rest[@]+"${rest[@]}"} ;;
  esac

  if (( ! has_locked )); then
    local corrected="uv run --locked"
    (( ${#opts[@]} )) && corrected+=" ${opts[*]}"
    corrected+=" ${target}"
    (( ${#rest[@]} )) && corrected+=" ${rest[*]}"
    deny "Hammertime bash guard: uv run must carry --locked, before the target, so that uv refuses to change uv.lock instead of re-locking and installing. Run exactly this instead: ${corrected}"
  fi
  return 0
}

# make (ADR-0018 decision 8). Exactly `make TARGET`, with TARGET on
# ALLOW_MAKE_TARGETS: no options, no second target, no assignment. make
# still runs whatever the tracked Makefile's recipe says; that route needs
# a tracked edit, which is what decision 1 counts on.
check_make() {
  require_literal "make"
  if (( $# != 1 )) || [[ "$1" == -* || "$1" == *=* ]] || ! in_list "$1" ${ALLOW_MAKE_TARGETS:-}; then
    deny "Hammertime bash guard: 'make $*' is refused. make may be run only as make TARGET, with exactly one TARGET from ALLOW_MAKE_TARGETS (${ALLOW_MAKE_TARGETS:-none}), and with no option, second target or VAR=value assignment."
  fi
  check_shadowing "make ${1}"
  return 0
}

# pytest (ADR-0018 decision 6), for the words after `uv run ... pytest`.
# Every word is checked and none is skipped as some option's value: a
# word beginning with `-` must be an allowed option wherever it stands,
# and every other word must pass the operand rule even when pytest will
# read it as a value (`-k WORD`, `--tb short`). So the guard never has to
# model which options take values; a value vetted as an operand is either
# harmless or refused, which fails closed.
PYTEST_LONG_OPTS="--quiet --verbose --exitfirst --showlocals --last-failed --lf --failed-first --ff --new-first --nf --stepwise --sw --collect-only --co --no-header --no-summary --setup-show --strict-markers --runxfail --full-trace --benchmark-only --benchmark-skip --benchmark-disable --hypothesis-show-statistics --tb --maxfail --durations --hypothesis-seed"
PYTEST_TB_STYLES="auto long short line native no"

is_digits() {
  [[ -n "$1" && "$1" != *[!0123456789]* ]]
}

pytest_option_allowed() {
  local token="$1"
  in_list "$token" $PYTEST_LONG_OPTS && return 0
  case "$token" in
    --tb=*)
      in_list "${token#--tb=}" $PYTEST_TB_STYLES
      return
      ;;
    --maxfail=* | --durations=* | --hypothesis-seed=*)
      is_digits "${token#*=}"
      return
      ;;
    -k | -m) return 0 ;;
    -r?*)
      [[ "${token#-r}" != *[!${ASCII_LETTERS}]* ]]
      return
      ;;
    --*) return 1 ;;
    -?*)
      [[ "${token#-}" != *[!qvxsl]* ]]
      return
      ;;
  esac
  return 1
}

pytest_operand_allowed() {
  local word="$1" pre path base
  [[ "$word" == @* || "$word" == /* || "$word" == '~'* ]] && return 1
  pre="${word%%::*}"
  [[ "/${pre}/" == */../* ]] && return 1
  path="${pre#./}"
  while [[ "$path" == */ ]]; do
    path="${path%/}"
  done
  [[ -z "$path" || "$path" == "." ]] && return 0
  base="${path##*/}"
  [[ "$base" != *.* ]] && return 0
  [[ "$base" == test_*.py || "$base" == *_test.py ]] && return 0
  return 1
}

check_pytest() {
  local token
  for token in "$@"; do
    if [[ "$token" == -* ]]; then
      if ! pytest_option_allowed "$token"; then
        deny "Hammertime bash guard: 'pytest ${token}' is not one of the pytest options this agent may use. Allowed, matched exactly: ${PYTEST_LONG_OPTS}; --tb=STYLE; --maxfail=N, --durations=N, --hypothesis-seed=N; -k WORD; -m WORD; -r followed by letters; and clusters of the letters qvxsl. Options that load code, write or delete files, send output elsewhere or go interactive are refused, as are -- and an attached -kWORD."
      fi
      continue
    fi
    if ! pytest_operand_allowed "$token"; then
      deny "Hammertime bash guard: '${token}' is not something pytest may be given here. Every word that is not an option, option values included, must be a relative directory or a test file (test_*.py or *_test.py, optionally with ::node ids), with no leading /, ~ or @ and no .. component. pytest imports any .py file named on its command line and runs a .txt or .rst named there as a doctest, whatever its name. Select tests with -k WORD, a single word, or by naming the test file."
    fi
  done
  return 0
}

# ruff (ADR-0018 decision 7), for the words after `uv run ... ruff`.
RUFF_CHECK_OPTS="-q --quiet --no-fix --diff --statistics --show-fixes"
RUFF_FORMAT_OPTS="--check --diff -q --quiet"

ruff_read_operand_allowed() {
  local word="$1"
  [[ "$word" == @* || "$word" == /* || "$word" == '~'* ]] && return 1
  [[ "/${word}/" == */../* ]] && return 1
  return 0
}

deny_ruff_operand() {
  deny "Hammertime bash guard: '${1}' is not a path ruff may be given here. ruff's operands must be relative paths with no leading /, ~ or @ and no .. component."
}

deny_ruff_write() {
  deny "Hammertime bash guard: ${1} Without --check or --diff, ruff format rewrites files, so it takes only the Python files you changed, named one by one: uv run --locked ruff format path/to/module.py [more .py or .pyi files]. '.' and directories are refused because they would also rewrite test files, which are test-author's to change: report a misformatted test file rather than formatting it."
}

check_ruff() {
  local sub token read_only=0 glob rel
  local -a operands=()
  if (( $# == 0 )) || [[ "$1" != "check" && "$1" != "format" ]]; then
    deny "Hammertime bash guard: 'ruff ${1:-}' is refused. ruff may be run only as ruff check or ruff format, with the subcommand first and no global option before it: uv run --locked ruff check . and uv run --locked ruff format --check ."
  fi
  sub="$1"
  shift
  for token in "$@"; do
    if [[ "$token" == -* ]]; then
      if [[ "$sub" == "check" ]]; then
        in_list "$token" $RUFF_CHECK_OPTS && continue
        if [[ "$token" == "--fix" ]]; then
          deny "Hammertime bash guard: 'ruff check --fix' rewrites files and is not available to this agent. Fix lint findings with the Edit tool."
        fi
        deny "Hammertime bash guard: 'ruff check ${token}' is not one of the ruff check options this agent may use. Only these are, matched exactly: ${RUFF_CHECK_OPTS}."
      fi
      if in_list "$token" $RUFF_FORMAT_OPTS; then
        [[ "$token" == "--check" || "$token" == "--diff" ]] && read_only=1
        continue
      fi
      deny "Hammertime bash guard: 'ruff format ${token}' is not one of the ruff format options this agent may use. Only these are, matched exactly: ${RUFF_FORMAT_OPTS}."
    fi
    ruff_read_operand_allowed "$token" || deny_ruff_operand "$token"
    operands+=("$token")
  done

  [[ "$sub" == "check" ]] && return 0
  (( read_only )) && return 0

  # Write mode. `--check`/`--diff` is a present-flag rule, and it is sound
  # here only because literal mode makes the word checked the word ruff
  # gets, `--` is refused so `--check` cannot be demoted to an operand,
  # and every allowed option is valueless so none can consume it.
  if [[ -z "${WRITE_DENY_GLOBS:-}" ]]; then
    deny_ruff_write "ruff format in write mode is not available under this policy, because it sets no WRITE_DENY_GLOBS to fence what it may rewrite."
  fi
  if (( ${#operands[@]} == 0 )); then
    deny_ruff_write "ruff format was given no file to format."
  fi
  for token in "${operands[@]}"; do
    if [[ "$token" != *.py && "$token" != *.pyi ]]; then
      deny_ruff_write "'${token}' is not a .py or .pyi file."
    fi
    rel="${token#./}"
    for glob in $WRITE_DENY_GLOBS; do
      if [[ "$rel" == $glob ]]; then
        deny "Hammertime bash guard: '${token}' matches '${glob}' in WRITE_DENY_GLOBS, the same fence the Edit and Write tools apply, so ruff format may not rewrite it. If it needs formatting, report it."
      fi
    done
  done
  return 0
}

# The commands this script knows how to vet (ADR-0018 decision 4). The
# first list carries a rule each; the second is read-only by construction
# and needs none. A command on a policy's ALLOW_CMDS that is on neither is
# a configuration error. Add a name here only together with the rules
# that make it safe -- a name on this list with no rule behind it is
# admitted with any arguments.
KNOWN_RULE_CMDS="find git node rg sed sort file uv make"
KNOWN_READONLY_CMDS="ls cat head tail wc stat grep jq diff cmp pwd"

# --- Validate every segment ---------------------------------------------

saw_segment=0
while IFS= read -r segment; do
  read -r -a raw_tokens <<< "$segment"
  (( ${#raw_tokens[@]} )) || continue
  saw_segment=1

  tokens=()
  for raw in "${raw_tokens[@]}"; do
    tokens+=("$(normalize_token "$raw")")
  done

  cmd0="${tokens[0]}"
  args=("${tokens[@]:1}")

  if [[ "$cmd0" == *=* ]]; then
    deny "Hammertime bash guard: '${cmd0}' sets an environment variable inline in front of the command, which can change what an allowed binary loads and runs (for example NODE_OPTIONS=--require=...). Re-run the command with no leading VAR=value assignment."
  fi

  if [[ "$cmd0" == */* ]]; then
    deny "Hammertime bash guard: '${cmd0}' names a binary by path, which sidesteps the command-name allowlist. Call an allowed command by its bare name instead (ALLOW_CMDS: ${ALLOW_CMDS})."
  fi

  if ! in_list "$cmd0" $ALLOW_CMDS; then
    if (( stop_and_report )); then
      # ADR-0018 decisions 4 and 11: the auditor's wording without its
      # needs-validation sentences, plus the two required hints.
      not_allowed="Hammertime bash guard: '${cmd0}' is not an allowed command for this agent (ALLOW_CMDS: ${ALLOW_CMDS})."
      case "$cmd0" in
        cd) not_allowed+=" There is no cd: run every command from the worktree root and name paths relative to it." ;;
        python*) not_allowed+=" Tests run through uv run --locked pytest, and type-checking through make typecheck." ;;
      esac
      deny "$not_allowed"
    fi
    deny "Hammertime bash guard: '${cmd0}' is not an allowed command for this agent. This agent gets read-only inspection tools only (ALLOW_CMDS: ${ALLOW_CMDS}); running the repository's own code, its test suite, a package manager or a network client would execute target-controlled code, and that needs an OS-enforced sandbox this environment does not provide. Report the finding as needs-validation, naming the exact command a human should run to confirm it, instead of running it here."
  fi

  # ADR-0018 decision 4: a command the policy allows but this script has
  # no model for is refused, whatever its arguments, instead of being
  # admitted unvetted. This is what makes a policy that lists python3,
  # awk or a shell fail closed, and a future policy wired before the
  # script learns its rules.
  if ! in_list "$cmd0" $KNOWN_RULE_CMDS $KNOWN_READONLY_CMDS; then
    deny "Hammertime bash guard: configuration error: '${cmd0}' is on this policy's ALLOW_CMDS, but this script has no rules for it and cannot vet what it would run, so it is refused whatever its arguments. Commands this script knows: ${KNOWN_RULE_CMDS} ${KNOWN_READONLY_CMDS}. This is not something the calling agent can fix."
  fi

  case "$cmd0" in
    sed) check_sed ${args[@]+"${args[@]}"} ;;
    sort) check_sort ${args[@]+"${args[@]}"} ;;
    find) check_find ${args[@]+"${args[@]}"} ;;
    git) check_git ${args[@]+"${args[@]}"} ;;
    node) check_node ${args[@]+"${args[@]}"} ;;
    rg) check_rg ${args[@]+"${args[@]}"} ;;
    file) check_file ${args[@]+"${args[@]}"} ;;
    uv) check_uv ${args[@]+"${args[@]}"} ;;
    make) check_make ${args[@]+"${args[@]}"} ;;
  esac
done <<< "$segments"

if (( ! saw_segment )); then
  deny "Hammertime bash guard: no command could be parsed out of this Bash call. Send one single-line command built from the allowed read-only tools (ALLOW_CMDS: ${ALLOW_CMDS})."
fi

exit 0
