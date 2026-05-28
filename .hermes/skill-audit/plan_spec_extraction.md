# Plan Spec Extraction: Hermes Autoresearch M2.7 Dashboard

Source reviewed by operator request: `docs/superpowers/specs/2026-05-28-hermes-autoresearch-m27-highspeed-dashboard-design.md` from the local planning workspace.

## Adopted For This Implementation

- Treat Autoresearch as a bounded campaign, not normal chat mode.
- Prefer `MiniMax-M2.7-highspeed` for token-heavy runner loops when already configured and credential-safe.
- Require fixed target artifact, allowed paths, baseline eval, one bounded mutation, safety gate, decision, and append-only ledger.
- Keep the first dashboard read-only. It reads audit/ledger artifacts and exposes no mutation controls.
- Show active run, model route, inventory counts, keep/discard/block counts, score trend, last hypothesis, changed files, safety status, evidence path, and stop reason.
- Fail closed when secrets, runtime config, provider/model routing, cron, systemd, or path allowlist violations appear.

## Deferred Or Rejected In This Pass

- No global Hermes default model change.
- No provider routing or model routing mutation.
- No MiniMax credential handling, login, or secret file edits.
- No production dashboard route, deployment, service restart, or Tailscale/Funnel exposure.
- No core Autoresearch runner implementation beyond local skill-audit support scripts.
- No automatic publication, push, merge, or campaign-result deployment.

## Practical Shape Implemented Here

- New local skill: `~/.hermes/skills/dev/autoresearch/SKILL.md`.
- Result ledger: `.hermes/skill-audit/autoresearch_results.tsv`.
- Local evaluator: `scripts/eval_local_skills.py`.
- Static read-only dashboard generator: `scripts/render_autoresearch_dashboard.py`.
- Dashboard artifact: `.hermes/skill-audit/dashboard.html`.
