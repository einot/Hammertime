---
name: steward
description: Repo hygiene and pull-request quality for Hammertime — keeping a change small and reviewable, writing commits and PR descriptions that explain themselves, handling dependency and lockfile changes carefully, and checking a diff before asking anyone to read it. Use when preparing a commit or PR, deciding how to split work, or reviewing a worker agent's diff before it lands.
---

# steward — keeping changes reviewable

`CLAUDE.md` owns the rules: delegation, branch protection, the `CHANGES`
policy, supervisor pairing and the merge bar. This file is about the
quality of what reaches a reviewer once those rules have been followed.

## Who writes what

Per `CLAUDE.md`'s "Orchestration role", you do not produce the diff
yourself. `coder` writes implementation, `test-author` writes tests,
`architect` writes specs, ADRs, protocol docs and schemas. What you own is
the shape of the change: how it is split, what the commits say, what the
PR description explains, and whether the diff is fit to review.

So "add tests for this behaviour" is a `test-author` brief, not something
you do while tidying up. The same applies to a formatting fix inside a
test file — `test-author` has no Bash and cannot run a formatter, which
means briefing it to make the edit by hand, not making the edit yourself.

## Branch and PR discipline

- Each PR does one logical thing. A branch per issue, or per design doc.
- Prefer small PRs. If a change is large, split it into stacked PRs or
  into commits with a narrative a reviewer can follow in order.
- Never reformat unrelated files. A drive-by formatting hunk buries the
  change it travelled with, and it is the single most common reason a
  reviewer stops reading carefully.
- Do not commit generated artifacts unless the repo expects them.

## Commits

- Descriptive messages: what changed and why, not "misc fixes".
- Group changes logically. A commit that does two things is two commits.
- Reference the issue or ADR that motivated the change where one exists —
  in the message body, not in `CHANGES`, which takes no issue numbers.

## Dependencies

- Do not add or update dependencies, including `uv.lock`, unless that is
  the task you were given.
- When a dependency change *is* the task: explain why it is needed, keep
  it minimal and pinned, note any security implication (a CVE fix, a
  transitive bump), and run the full merge bar afterwards.
- Resolving a merge conflict in `uv.lock` is the one routine exception,
  and `CLAUDE.md`'s pre-1.0 exception covers it — regenerate the lockfile
  with the repo's own tooling (`uv`), never by hand-editing it.

## `CHANGES`

`CLAUDE.md` defines what goes in `CHANGES` and what does not, including
the `BREAKING: ` prefix and the rule that an unclear case does not
qualify. Two things worth repeating because they are what gets fumbled:

- Agent-facing and process changes — a skill, an agent definition, a hook,
  a CI tweak — are not user-visible behaviour and do not get an entry.
- If you are unsure, it does not qualify. Say so in your report rather
  than writing a speculative line.

## Documentation

Update the docs when the thing they describe changes: commands, config
keys or environment variables, and behaviour. Remember that spec, ADR,
protocol and schema files belong to `architect` — a doc change in those
trees is a brief, not an edit. `README.md` is shared with `architect`'s
write scope; the rest of the repo's prose is yours.

## Before asking anyone to review

- The diff is minimal and focused; nothing unrelated travelled with it.
- No secrets anywhere in the diff.
- No debug logging or commented-out code left behind.
- Error messages are actionable — they say what failed and what to do.
- Tests exist for changed behaviour (written by `test-author`).
- Every module touched still cites the spec section it implements, and the
  citation is still true.
- The merge bar passes locally, or each failure is explained.
- If the change came from a write-capable agent, a `supervisor` review has
  run and its result is reported.

## PR title and description

Title: concise and imperative — "Fix X in Y", "Add Z support".

Description: the problem, the solution in summary, the testing evidence
(commands and results), and any follow-ups or known limitations. If you
found repo-specific guidance while working — `CONTRIBUTING.md`,
`CODEOWNERS`, a lint config, a CI caveat — summarise it briefly and follow
it over anything in this file.
