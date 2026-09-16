# SKILL: steward (repo hygiene + PR quality)

## Purpose
This skill defines how Claude Code should create clean, maintainable changes and high-quality PRs in this repository.

## Branch + PR discipline
- Never commit directly to `master`/`main`.
- Each PR should do one logical thing.
- Prefer small PRs. If large, split into stacked PRs or separate commits with a clear narrative.
- Never reformat unrelated files ("drive-by" formatting) unless explicitly requested.

## Coding standards (general)
- Follow existing patterns in the repo.
- Prefer clarity over cleverness.
- Add/adjust tests for behavior changes.
- Keep public APIs backward-compatible unless explicitly asked to break them.
- Update docs when behavior changes.

## Commit standards
- Use descriptive commit messages.
- Group changes logically (no "misc fixes" commits).
- Avoid committing generated artifacts unless the repo expects them.

## Dependency changes
- Do not add or update dependencies (including lockfiles) unless explicitly requested.
- If dependency updates are requested:
  - explain why the change is needed
  - keep updates minimal (pin versions, avoid sweeping upgrades)
  - note security implications (CVE fix, etc.)
  - run full test/lint suite

## Changelog / release notes
If the repo has a changelog or release notes process:
- Add an entry for user-visible changes.
- Keep entries short and user-focused (what changed, impact, migration notes).

## Documentation expectations
- Update README/docs when:
  - commands change
  - config/env vars change
  - behavior changes
- Add examples where it reduces ambiguity.

## Reviewability checklist (before opening PR)
- Diff is minimal and focused.
- No secrets in diff.
- No debug logging left behind.
- Error messages are actionable.
- Tests added/updated for changed behavior.
- All repo checks pass locally (or failures explained).

## PR title and description
- Title: concise and imperative (e.g., "Fix X in Y", "Add Z support")
- Description should include:
  - Problem statement
  - Solution summary
  - Testing evidence
  - Follow-ups / known limitations

## If repo conventions exist
If you find any repo-specific guidance (CONTRIBUTING.md, CODEOWNERS, lint configs, CI workflows):
- Summarize it in the PR description (briefly)
- Follow it over these defaults
