# Provenance

This directory is a **verbatim vendored copy** of a third-party skill. Every
other file here is upstream content — do not hand-edit them.

- Upstream skill: `security-audit`, distributed to this project's users as the
  namespaced skill `anthropic-skills:security-audit`.
- Licence: MIT, Copyright (c) 2025-2026 Cloudflare, Inc. See `LICENSE`, which
  is part of the copy and must stay with it.
- Vendored on: 2026-09-19.
- Vendored because `.claude/agents/security-auditor.md` preloads this skill via
  its `skills:` frontmatter. Referencing the namespaced copy only worked for
  people who happened to have that skill synced to their account; everyone else
  got a "Skill not found" warning and a `security-auditor` with no methodology
  in context. Keeping the copy in-repo makes the agent behave the same for
  every checkout and in CI.

## Local additions

`PROVENANCE.md` (this file) is the only file that is not upstream.

## Re-syncing

The copy is byte-identical to upstream as vendored, so a refresh is a diff plus
a copy — no local changes to replay:

    diff -r <upstream-security-audit-dir> .claude/skills/security-audit \
      --exclude PROVENANCE.md
    cp -a <upstream-security-audit-dir>/. .claude/skills/security-audit/

After refreshing, re-read `.claude/agents/security-auditor.md`: its "Preloaded
skill" section names specific companion files and pins the agent to the skill's
guidance mode, and an upstream rename or a reworked mode model would make that
section wrong.

## Note on the validators

`validate-findings.cjs`, `validate-coverage-ledger.cjs`, their `.test.cjs`
files and `report-schema.json` belong to the skill's full-audit workflow. The
`security-auditor` agent cannot run them — it has no Bash and no Write — and
nothing in this repo's CI runs them either. They are kept only so the copy
stays verbatim and diffable against upstream.
