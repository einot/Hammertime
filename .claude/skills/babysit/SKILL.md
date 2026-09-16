# SKILL: babysit (Claude Code safety + supervision)

## Purpose
This skill defines operational guardrails for Claude Code while it works in this repo, and what to do when it is uncertain or blocked.

## Non-negotiable rules
- Never push to `master`/`main` directly. Work on a feature branch and open a Pull Request.
- Do not bypass branch protections. Do not force-push to shared branches.
- Do not create or expose secrets:
  - Never print tokens/keys, `.env` contents, kubeconfigs, SSH private keys, or vault outputs.
  - Redact secrets from logs before sharing.
- Do not run destructive or high-risk commands without explicit human approval:
  - `rm -rf`, disk/partition commands, package manager global installs
  - infra/prod ops: `terraform apply`, `kubectl delete/apply` to prod, DB writes/migrations on prod
- Do not fetch/copy proprietary code from outside sources. Respect licensing.
- Keep changes minimal and reviewable. Prefer small, incremental commits.

## Before making changes
1. Restate the objective and constraints.
2. Discover relevant code by searching and reading before editing.
3. Propose a plan:
   - files to change
   - approach options + tradeoffs (at least 2 if non-trivial)
   - how you will validate (tests/commands)
4. Wait for approval if the change is large, touches security/auth, or changes dependencies.

## Safe default workflow (PR-only)
- Create branch: `git checkout -b claude/<short-topic>`
- Make changes in small commits.
- Run validation (see below).
- Open a PR with a clear summary, testing evidence, and risk notes.

## Validation (must do before declaring "done")
Run the repo's standard checks (see `Makefile`):
- Unit tests: `uv run pytest -q` (equivalently `make test`)
- Lint: `uv run ruff check .` (equivalently `make lint`)
- Format: `uv run ruff format --check .` (`make fmt` without `--check` rewrites files — use the check form to validate, not to fix silently)
- Typecheck: `uv run mypy packages services` (equivalently `make typecheck`; deliberately excludes `tools/` — see CLAUDE.md/known issues for why)

If CI exists, ensure your PR will run it and note any known CI caveats. This repo's `.github/workflows/ci.yml` `integration` job is a known pre-existing exception (see issue #26 and CLAUDE.md's "Pre-1.0 exception") — do not treat that specific job's failure as blocking unless you have introduced a *new* failure in it.

## When blocked / uncertain (STOP protocol)
If you are uncertain, stuck, or see unexpected results, stop and provide:
- What you tried (commands + file paths)
- What happened (exact error output)
- What you think is happening (hypothesis)
- Two next-step options (safe-first), and what info you need from a human

Do not "thrash" by making many speculative edits.

## Escalate immediately if
- You suspect credential leakage, supply-chain compromise, or malicious code.
- You are asked to weaken security controls (authn/z, TLS, permissions) without explicit approval.
- A change would impact production data, billing, or customer security posture.

## PR description template
Include:
- Summary (what + why)
- Scope (files / modules touched)
- Testing (commands run + results)
- Risk/rollout notes (anything reviewers should watch)
- Screenshots/outputs where relevant
