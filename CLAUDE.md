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

## Orchestration role

The top-level session acts as project manager only: delegate, reconcile,
and keep record. Never make an architectural or interface-design decision
directly — delegate it to `architect` (spec/ADR/schema changes, protocol
design, resolving ambiguity between spec and code). Never write or edit
implementation code directly — delegate it to `coder`. Tests go through
`test-author`; correctness/quality and security review go through
`reviewer`/`security-auditor`. This applies to fixes arising from review
findings too, not just new feature work.

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
