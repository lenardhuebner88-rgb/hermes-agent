# Hermes Autoresearch Run Context

- Timestamp: 2026-05-28T23:22:09+02:00
- Host: huebners
- Repo: /home/piet/.hermes/hermes-agent
- Skills root: /home/piet/.hermes/skills
- Audit folder: /home/piet/.hermes/hermes-agent/.hermes/skill-audit
- Branch: codex/hermes-autoresearch-m27-highspeed-dashboard
- HEAD: e70b34a24
- Skills Git tracking: not-git-tracked
- Backup path: /home/piet/.hermes/backups/skills-before-autoresearch-20260528-231849

## Initial Commands

```bash
cd /home/piet/.hermes/hermes-agent
pwd
git status --short
git branch --show-current
git log --oneline -5
mkdir -p .hermes/skill-audit
find ~/.hermes/skills -maxdepth 4 -type f \( -name "SKILL.md" -o -name "*.md" \) 2>/dev/null | sort
find /home/piet/.hermes/hermes-agent -maxdepth 5 -type f \( -name "SKILL.md" -o -path "*/skills/*" \) 2>/dev/null | sort
```

## Baseline Git Status

```text
?? .firecrawl/
```

## Recent Commits

```text
408925653 Merge dashboard-tailnet-hostguard + kanban fixes into main
2feece25c Merge H1b (FU-4): create-with-parent inherits parent notify-sub
8ac0d7b12 fix(dashboard): trust operator-declared public_url host in DNS-rebinding guard
c849ab1d6 feat(kanban): create-with-parent inherits the parent's notify-sub (H1b / FU-4)
29dcd9072 test(kanban): xfail repro for WI-6 — model_override clobbered before chat
```

## Discovery Scope

- Inventory records actual `SKILL.md` units under `~/.hermes/skills` and Hermes repo skill folders.
- Vendored/generated dependency folders are excluded from scoring: `.git`, `.venv`, `node_modules`, `.next`, `dist`, `build`, `__pycache__`.
- Existing untracked `.firecrawl/` was present before this workflow and was not touched.
- No secrets, credentials, databases, caches, logs, service units, cron jobs, or runtime configs are in scope.


## Plan Spec Integration

Operator added local plan spec `docs/superpowers/specs/2026-05-28-hermes-autoresearch-m27-highspeed-dashboard-design.md` during the run. Adopted only the safe parts: bounded campaign contract, MiniMax-M2.7-highspeed runner preference when already configured, append-only ledger, safety gates, and a read-only local dashboard. Deferred provider/model routing, credentials, core runner, deployment, service restart, and mutation controls.

## Latest Audit Refresh

- Timestamp: 2026-05-28T23:35:16+02:00
- Inventoried SKILL.md files: 308
- High/medium/low: 101/47/160
