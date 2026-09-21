## Standing order (pre-1.0)

Until the 1.0.0 release ships: **where there is one clearly best next
step, the top-level session takes it without consulting the user first.**
Only genuine forks — two or more defensible options with no clear winner —
are put to the user before acting.

This is a rule about *asking*, not about *telling*. Anything this file
requires the session to report, it still reports, in the same turn it
acts; reporting is not consulting, because it does not block. Nor does it
loosen any other rule here: delegation, branch protection, the full test
bar and supervisor pairing all apply exactly as written. It only removes
the wait for a reply when the recommendation is unambiguous.

Recorded at the user's direction, and effective until 1.0.0.

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

Every dispatch to `coder`, `test-author`, `architect`, or
`security-auditor` (the subagents that can write or execute) must be
paired with a `supervisor` review before acting on its output (merging,
pushing, or handing off to another agent). Give `supervisor` exactly two
things: the literal instructions given to the worker agent, and the
worker's own report of what it changed.
`supervisor` is read-only (Read/Grep/Glob only, no Bash, no Edit/Write, no
spawning other agents) — it inspects the actual current file contents
against the task's stated scope and flags anything out of the ordinary:
files touched outside the given scope, actions beyond what was asked,
a report that doesn't match what actually changed, or anything resembling
a backdoor, credential/secret exfiltration, or a disabled safety/security
check.

**Hard stop:** if `supervisor` reports any finding, report it to the user
verbatim before doing anything else. That duty is unconditional — it
survives every other rule in this file, and no finding is ever summarised,
paraphrased, or held back.

What follows the report depends on whether there is a clear next step.
Under the pre-1.0 standing order at the top of this file, where the
session has one clearly best recommendation it may act on it in the same
turn as the verbatim report, without waiting for a reply. Where it does
not — two defensible ways forward, or no confident reading of what the
finding means — the original rule stands in full: STOP ALL PROCESSING, do
not merge, push, dispatch further agents, or continue reconciling, and let
the user decide.

A finding that names a backdoor, credential/secret exfiltration, or a
disabled safety/security check always stops, whatever else looks obvious,
because for those the clear recommendation *is* to stop and hand it to the
user — the session does not get to decide it understood such a finding
well enough to work past it. That follows from the standing order rather
than being carved out of it.

`reviewer` is read-only and structurally incapable of taking an
unauthorized action — no Edit, no Write, no Bash — so routine supervisor
coverage excludes it. Extend coverage to every dispatch if asked.

`security-auditor` was exempt on the same grounds until it gained a Bash
tool, fenced to read-only inspection commands by
`.claude/hooks/bash-guard.sh`. A fence is not the same guarantee as not
having the tool: the exemption rested on there being nothing to fence,
and that is no longer true. It is therefore paired like the agents that
can write. This is deliberately the cautious reading — the fence is
default-deny and carefully written, but it is a shell script, and the
cost of pairing is one extra read-only review per security audit.

Subagents do not dispatch other subagents. `architect` has no `Agent`
tool: it settles the interface, writes ready-to-dispatch briefs, and hands
them back (see `.claude/agents/architect.md`). The top-level session
issues every dispatch and pairs every one with `supervisor` itself. This
keeps a supervisor finding one hop from the user instead of relayed
through an agent, and keeps the decision to trust a worker's report with
the session that can run the tests and read the git state.

## Orchestration role

The top-level session acts as project manager only: delegate, reconcile,
and keep record. Never make an architectural or interface-design decision
directly — delegate it to `architect` (spec/ADR/schema changes, protocol
design, resolving ambiguity between spec and code). Never write or edit
implementation code directly — delegate it to `coder`. Tests go through
`test-author`; correctness/quality and security review go through
`reviewer`/`security-auditor`. This applies to fixes arising from review
findings too, not just new feature work. `architect` returns a plan and
per-worker briefs rather than dispatching anyone; issuing those dispatches
is yours.

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

**Agent configuration.** The top-level session may alter agent
configuration — `.claude/agents/*.md`, including which model backs an
agent — when the user directly instructs it to. It may not alter it on its
own initiative: not to work around a limitation it has run into, and not
because the change would make the job in front of it easier or faster. If
an agent's configuration looks like it is blocking legitimate work, say so
and let the user decide; changing it unasked defeats the point of having
the constraint.

**Agent guards.** The subagent path and Bash guards live in
`.claude/settings.json`, scoped per agent with `SCOPE_AGENT_TYPES`, and
nowhere else. Never declare them in an agent file's `hooks:` frontmatter:
a guard declared there did not fire in this environment (three probes,
#102), most likely because project-level frontmatter hooks require the
workspace trust dialog to have been accepted, which a headless session
never does. Hook configuration is read from the *main checkout*, not from
a worktree-isolated agent's checkout -- so a `coder` dispatch is fenced by
whatever the main checkout's `settings.json` says at that moment, and the
copy in its worktree is inert. Verify any guard change the only way that
counts: put it in the main checkout, dispatch a real agent, and have it
attempt an operation the policy must refuse. `tests/config/` pins the
wiring, but a passing test is not a fired hook.

**Pre-1.0 exception:** until the first release (1.0) ships, the top-level
session may finish and merge PRs itself — resolving merge conflicts
(including regenerating lockfiles with the repo's own tooling, never by
hand), pushing the resolution, and merging — without delegating that work.
This does not extend to designing the change being merged, only to landing
it. Before merging, the full suite must pass (`uv run pytest -q`, `ruff
check`, `ruff format --check`, `mypy`). There is no longer a standing
exception to that bar — the `integration` CI gap that used to be one is now
handled as recorded under "Disabled CI coverage" below. Run those four
commands so a failing one is actually visible: piping each to `tail` hides
its exit status and will report a red gate as green.

## Branch protection

`master` is protected: every significant change lands on a dedicated
feature branch and reaches `master` only through a pull request, never a
direct commit or push to `master` itself. A "significant change" is
anything that touches code, schemas, config, or design docs (ADRs, spec
sections) — a one-line typo fix in passing is not, but when in doubt, use a
branch. This holds even under the pre-1.0 exception above: that exception
covers finishing/merging a PR, not committing straight to `master`.
Branch-per-issue (or per-design-doc) is the established convention here —
keep it that way even as issues get split, reconciled, or stacked on each
other.

## Disabled CI coverage

Anything switched off in CI is recorded here together with the condition
for switching it back on. Nothing gets disabled without an entry, and no
entry is deleted until the thing is genuinely running again.

**`integration` job — disabled, pending #52.** (Originally tracked by
#26, closed 2026-09-17; #52 now carries the remaining work. Note that
`.github/workflows/ci.yml` and several ADRs still cite #26 — in the ADRs
that is a correct historical record and is left alone.)
`.github/workflows/ci.yml` carries `if: false` on that job. It could
never pass: `tests/integration` and `tests/e2e` contain no collectable
tests (pytest exits 5), and three of the four services — aggregator,
trie, detector — have no `def` or `class` outside their own test
directories, so they crash-loop under `docker compose`. **No test is
skipped or quarantined by this**: there are none in those directories to
skip. A disabled job shows as skipped rather than green, so nothing
claims to pass that does not.

Re-enable when all three of these hold:

1. aggregator, trie and detector are implemented and actually start under
   `docker compose`.
2. `test-author` has written real tests into `tests/integration` and
   `tests/e2e`.
3. The job's `docker compose up` step gains `--wait`. Without it the job
   passes even when every container dies on startup, so restoring the job
   unchanged would buy a green check that proves nothing.

A guard step in the `check` job fails the build the moment either
directory starts collecting tests, printing exactly what to turn back on.
That tripwire, not this paragraph, is what makes the disable impossible to
forget — so if you re-enable the job, delete the guard in the same change.
