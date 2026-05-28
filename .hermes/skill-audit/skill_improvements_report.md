# Skill Improvements Report

- Generated: 2026-05-28T23:33:59+02:00
- Scope: maximum five existing skills plus new `/autoresearch` skill and local audit tooling.
- Non-actions: no secrets touched, no provider/model routing changed, no runtime config changed, no services restarted, no push/merge.

## New Skill

- Path: `/home/piet/.hermes/skills/dev/autoresearch/SKILL.md`
- Purpose: controlled Git/backup-based improvement loop for existing Hermes skills.
- Includes: default `skills` mode, optional `tests`, `code`, `docs`, `research_qa` modes, MiniMax-M2.7-highspeed runner preference, baseline/branch/backup gates, one-hypothesis loop, append-only result log, read-only dashboard contract, examples.

## Existing Skill Changes

### 1. `/home/piet/.hermes/skills/devops/hermes-dashboard-exposure/SKILL.md`

- Problem vorher: Dashboard visualization guidance was tied to exposure work and lacked a clear read-only boundary for audit dashboards.
- Konkrete Änderung: Added a read-only Autoresearch/audit dashboard boundary and mutation ban.
- Nutzen: Prevents a local visualization request from accidentally becoming a public or mutable dashboard change.
- Risiko: Low; additive only, no existing exposure rule removed.
- Size impact: 6602 -> 7420 bytes (12.4% growth).

### 2. `/home/piet/.hermes/skills/devops/hermes-dashboard-exposure/SKILL.md`

- Problem vorher: No explicit output contract for exposure/audit-dashboard closeout.
- Konkrete Änderung: Added concise closeout fields and non-action reporting.
- Nutzen: Makes operator review easier and safer.
- Risiko: Low; reporting-only addition.
- Size impact: 7420 -> 7796 bytes (5.1% growth).

### 3. `/home/piet/.hermes/skills/github/github-pr-workflow/SKILL.md`

- Problem vorher: Branch discipline and stop rules were distributed across long recipes, with no compact hard gate near the top.
- Konkrete Änderung: Added hard stop rules and a PR/commit output contract.
- Nutzen: Reduces accidental main edits, broad staging, token exposure, and unauthorized push/merge actions.
- Risiko: Low; additive safety guidance only.
- Size impact: 40129 -> 41095 bytes (2.4% growth).

### 4. `/home/piet/.hermes/skills/software-development/hermes-agent-skill-authoring/SKILL.md`

- Problem vorher: Skill authoring guidance did not describe the new baseline/hypothesis/eval loop.
- Konkrete Änderung: Added Autoresearch-compatible editing workflow.
- Nutzen: Aligns skill authoring with measured improvement rather than broad rewrites.
- Risiko: Low; additive and scoped.
- Size impact: 15616 -> 16274 bytes (4.2% growth).

### 5. `/home/piet/.hermes/skills/software-development/hermes-agent-skill-authoring/SKILL.md`

- Problem vorher: Skill authoring closeout lacked a concise reusable report contract.
- Konkrete Änderung: Added required closeout fields for skill edits.
- Nutzen: Makes reviews and future audit dashboards easier to populate.
- Risiko: Low; reporting-only addition.
- Size impact: 16274 -> 16602 bytes (2.0% growth).

### 6. `/home/piet/.hermes/skills/devops/hermes-learning-skill-curator/SKILL.md`

- Problem vorher: Learning curation lacked a bridge from general skill patches to Autoresearch campaigns, MiniMax route preference, and read-only dashboard use.
- Konkrete Änderung: Added Autoresearch-compatible curation rules.
- Nutzen: Keeps durable learning promotion aligned with measured skill improvement and safe visualization.
- Risiko: Low; additive only.
- Size impact: 7446 -> 8230 bytes (10.5% growth).

### 7. `/home/piet/.hermes/skills/devops/hermes-modelrouting-codex/SKILL.md`

- Problem vorher: Model-routing skill did not distinguish MiniMax as an Autoresearch runner preference from global model routing changes.
- Konkrete Änderung: Added a MiniMax M2.7 runner boundary with proof and forbidden actions.
- Nutzen: Lets future Autoresearch loops use the high-token route safely without accidental config/secret work.
- Risiko: Low; additive boundary language, no config change.
- Size impact: 15788 -> 16641 bytes (5.4% growth).

## Verification

- python3 scripts/eval_local_skills.py: PASS, 0 structural errors, 104 warnings remain as future improvement candidates.
- python3 -m py_compile scripts/eval_local_skills.py scripts/render_autoresearch_dashboard.py: PASS.

## Test Results

- python scripts/eval_local_skills.py: not runnable because python is not installed as a command on this host.
- python3 scripts/eval_local_skills.py: PASS, 0 structural errors, 104 warnings remain as future improvement candidates.
- python3 -m py_compile scripts/eval_local_skills.py scripts/render_autoresearch_dashboard.py: PASS.
- python3 scripts/render_autoresearch_dashboard.py: PASS, wrote .hermes/skill-audit/dashboard.html.
- pytest -q with .venv and /home/piet/.hermes/scripts/hermes-pytest-wrapper.sh: collection failed because acp is missing (ModuleNotFoundError: No module named acp) across tests/acp/* and tests/acp_adapter/*. No code fix attempted.

## Residual Risk

- ~/.hermes/skills is outside the Hermes-Agent repo and not Git-tracked; changes there are protected by the timestamped backup, not by this repo commit.
- diff -qr against the backup also showed unrelated live skill changes outside this task, including github/github-repo-management and .usage.json; these were not edited or reverted here.
