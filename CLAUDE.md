## CHANGES

Record user-visible changes in `CHANGES` at the repo root, newest entry first.

Record: new features, changed behaviour, changed defaults, changed wire
formats or event schemas, changed config keys, removed functionality.

Do not record: refactors, internal renames, test-only changes, formatting,
docstring edits, or dependency bumps with no observable effect.

One line per change, present tense, no issue numbers:

    BREAKING: hot-ip events carry `family` field; v1 consumers must be updated
    Add GET /prefix/{cidr} returning hot_count, capacity and hot_ratio
    Reject observations whose window_start is older than allowed_lateness

Prefix `BREAKING: ` when a running deployment needs action to keep working:
a changed event-log schema, a renamed or removed config key, a changed
agent protocol, or a snapshot format that older builds can't load.

If you are unsure whether a change qualifies, it does not. Say so in your
report rather than writing a speculative entry.

## ADR conventions

When `architect` writes or amends an ADR, it must document every assumption
it made that was not explicitly specified by the issue, spec, or a prior
ADR/decision it builds on — not just the decision itself. This includes
things like: values chosen without an explicit requirement (timeouts, key
sizes, table sizes, default rates), scope boundaries assumed rather than
stated, and behavior in edge cases the source material didn't address. State
each such assumption plainly (e.g. under a "Assumptions" heading or inline
next to the decision it informs) so a reviewer or a later reader can tell
which parts of the ADR are derived from a real requirement and which are the
architect's own judgment call, and can push back on the judgment calls
specifically instead of having to re-derive them from the diff.

## Supervisor agent

Every dispatch to `coder`, `test-author`, or `architect` (the only
subagents with write access) must be paired with a `supervisor` review
before acting on its output (merging, pushing, or handing off to another
agent). Give `supervisor` exactly two things: the literal instructions
given to the worker agent, and the worker's own report of what it changed.
`supervisor` is read-only (Read/Grep/Glob only, no Bash, no Edit/Write, no
spawning other agents) — it inspects the actual current file contents
against the task's stated scope and flags anything out of the ordinary:
files touched outside the given scope, actions beyond what was asked,
a report that doesn't match what actually changed, or anything resembling
a backdoor, credential/secret exfiltration, or a disabled safety/security
check.

**Hard stop:** if `supervisor` reports any finding, STOP ALL PROCESSING
immediately — do not merge, push, dispatch further agents, or continue
reconciling — and report the finding to the user verbatim before doing
anything else. This overrides every other standing instruction in this
file, including the pre-1.0 exception below. Only the user decides how to
proceed from a supervisor finding.

`reviewer`/`security-auditor` are themselves read-only and structurally
incapable of taking an unauthorized action (no write access at all), so
routine supervisor coverage is scoped to the three agents that can write;
extend it to every dispatch if asked.

`architect` can itself dispatch `coder`/`test-author`; the same pairing
requirement applies to those dispatches too, so `architect` is also
allowed to spawn `supervisor` (see `.claude/agents/architect.md`). A
finding `supervisor` reports to `architect` reaches the top-level session
as a handed-back blocker like any other — the hard stop above still
applies once it does.

## Orchestration role

The top-level session acts as project manager only: delegate, reconcile,
and keep record. Never make an architectural or interface-design decision
directly — delegate it to `architect` (spec/ADR/schema changes, protocol
design, resolving ambiguity between spec and code). Never write or edit
implementation code directly — delegate it to `coder`. Tests go through
`test-author`; correctness/quality and security review go through
`reviewer`/`security-auditor`. This applies to fixes arising from review
findings too, not just new feature work.

**No exception for "mechanical" edits.** Every test file change goes
through `test-author`, full stop — including a one-line formatting fix, a
lint-only rename, or any other change that looks too small or too
obviously safe to bother delegating. `test-author` has no Bash and so
cannot run a formatter itself; that means making the edit by hand with
Edit until the content matches, not an excuse to make the edit directly
instead. The same holds for `coder`'s and `architect`'s domains: "it's
tiny" is never a reason to touch code, tests, or specs/schemas/ADRs
directly. The top-level session's own tools stay limited to reconciling
already-delegated work (applying a worker's own diff/commit, resolving a
merge conflict per the pre-1.0 exception below) and to editing this file,
other agent definitions, and non-code governance docs it owns directly.

**Pre-1.0 exception:** until the first release (1.0) ships, the top-level
session may finish and merge PRs itself — resolving merge conflicts
(including regenerating lockfiles with the repo's own tooling, never by
hand), pushing the resolution, and merging — without delegating that work.
This does not extend to designing the change being merged, only to landing
it. Before merging, the full suite must pass (`uv run pytest -q`, `ruff
check`, `ruff format --check`, `mypy`); the known pre-existing `integration`
CI gap (empty `tests/integration`/`tests/e2e`, tracked in #26) is the one
standing exception until it's fixed — once it starts passing, hold every
merge to that bar too.
