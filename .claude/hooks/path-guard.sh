#!/usr/bin/env bash
# Generic PreToolUse path guard shared by Hammertime's project subagents
# (.claude/agents/*.md). A subagent wires this in via its own `hooks:`
# frontmatter, setting env vars inline on the command line to parametrize
# the same script per-agent instead of duplicating the logic five times.
#
# Env vars (space-separated glob lists; `[[ str == pattern ]]` semantics,
# so a bare `*` in a pattern matches across `/` too — "docs/*" matches
# "docs/spec/hammertime_spec_1.md"):
#
#   EXEMPT_GLOBS  - path matching any of these is always ALLOWED,
#                   checked before DENY_GLOBS/ALLOW_GLOBS.
#   DENY_GLOBS    - path matching any of these (and not exempt) is DENIED.
#   ALLOW_GLOBS   - if set, a path that is not exempt/already-denied must
#                   match at least one of these or it is DENIED
#                   (allowlist mode). Leave unset for denylist-only mode.
#
# Reads the PreToolUse JSON payload on stdin (see
# https://code.claude.com/docs/en/hooks) and checks tool_input.file_path,
# falling back to tool_input.path (Grep/Glob).
#
# Caveat (documented, not a bug): this only intercepts the tool calls named
# in the subagent's own `matcher` (Edit|Write or Read|Grep|Glob). It does
# NOT inspect Bash commands, so an agent that also has the Bash tool could
# still read or write a guarded path via a shell command. Keep Bash off
# any agent whose guard must be a hard boundary, or treat the guard as a
# strong default rather than a sandbox for agents that keep Bash.

set -f -e -u -o pipefail

input="$(cat)"
file_path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.path // empty')"
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty')"

# Unrecognized tool_input shape -> don't block on it, just pass through.
if [[ -z "$file_path" ]]; then
  exit 0
fi

# Normalize to a path relative to the project/worktree root when possible.
project_dir="${CLAUDE_PROJECT_DIR:-$cwd}"
rel="$file_path"
if [[ -n "$cwd" && "$file_path" == "$cwd"/* ]]; then
  rel="${file_path#"$cwd"/}"
elif [[ -n "$project_dir" && "$file_path" == "$project_dir"/* ]]; then
  rel="${file_path#"$project_dir"/}"
fi

deny() {
  local reason="$1"
  jq -n --arg reason "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 2
}

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
