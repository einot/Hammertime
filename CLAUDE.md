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
