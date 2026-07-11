---
name: vision-flywheel
description: Use when operating, observing, tuning, or debugging the Hermes self-improving PlanSpec pipeline — the "Stratege"/strategist that proposes ROI improvements. Triggers: "stratege/flywheel", manually run propose/reflect, see/approve/veto held proposals, turn the autonomous cron on/off (kill-switch), "why did the strategist propose X", strategist metrics. NOT for designing Phase 2 auto-deploy (that's the locked PlanSpec).
---

# Vision-Flywheel (Strategist) — Operate

## Overview
Self-improving PlanSpec pipeline, three control-plane roles: **Härter** (ingest gate, `planspecs.py`), **Heiler** (escalation ledger + retry, `kanban_db.py`), **Stratege** (proposes/reflects, `hermes_cli/strategist.py`). The strategist runs **propose-and-wait**: it ingests ROI levers as PlanSpecs with `freigabe: operator` → they land **held** (root in `scheduled`); the operator approves/vetoes; only then they build. It NEVER deploys/pushes — only ingests held + writes its own state.

Design/history (don't duplicate here): vault `2026-06-18-vision-flywheel-phase2-design.md`, `2026-06-19-vision-flywheel-runway-{design,runbook}.md`; memory `project_vision_flywheel_design`, `reference_kanban_holds_and_links`.

## Run commands with `hermes` (login shell)
`hermes …` is on PATH in a login shell (`/home/piet/.local/bin/hermes` → `venv/bin/hermes`). In a non-login shell it's NOT on PATH — use `cd ~/.hermes/hermes-agent && venv/bin/python -m hermes_cli.main …` (the `venv` editable install reflects live code). `.venv/` is consumer-free/deprecated since 2026-07-02 — always use `venv/`.

## Quick reference
| Do | Command / location |
|---|---|
| Preview levers (no write, no LLM, **safe**) | `hermes vision strategist --mode propose --dry-run --json` |
| Real propose (Opus, **ingests held**) | `~/.hermes/scripts/strategist-cron.sh propose` (wrapper: snapshot → `claude -p` Opus + prompt) |
| Real reflect | `~/.hermes/scripts/strategist-cron.sh reflect` (or `hermes vision strategist --mode reflect --json`) |
| Metrics snapshot / gate ledger | `hermes vision metrics-snapshot --json` · `hermes vision record-gate-result pass\|fail` |
| See held proposals (CLI) | `hermes kanban list --status scheduled` (filter `created_by=strategist-cron`) · `hermes kanban show <id>` |
| See held proposals (API) | `GET /api/plugins/kanban/strategist/proposals` (token-gated; bare loopback curl = 401) |
| Dashboard | `/control/stratege` tab (under the **"Mehr"** dropdown, not the primary rail) |
| **Approve** | `hermes kanban release-freigabe <root> [--author <name>]` · dashboard · `POST …/strategist/proposals/<id>/approve` |
| **Veto** | dashboard · `POST …/strategist/proposals/<id>/veto` (**no CLI** — `kanban archive` does NOT veto) |
| Cron status (is it armed?) | `systemctl --user list-timers \| grep strateg` (absent = not armed) |
| **Arm autonomy** | `systemctl --user enable --now strategist-harvest.timer strategist-propose.timer strategist-reflect.timer stratege-gutachter.timer` |
| **Kill-switch (stop)** | `systemctl --user disable --now strategist-harvest.timer strategist-propose.timer strategist-reflect.timer stratege-gutachter.timer` |
| State files | `~/.hermes/state/vision-metrics.json` · `~/.hermes/state/strategist/{specs/,levers.json,lever-outcomes.json,reflections.jsonl,run-history.jsonl,vetoed_levers.json}` (`levers.json` = Opus's drafts, read by `--drafts-file`; `lever-outcomes.json` = baseline→ship→3d-measure ledger, measured by reflect) |

Live schedule — **four units, all ARMED/enabled (live-verified 2026-07-06;** the old "disabled by default" state ended with the operator's arming act): harvest **05:30** (Sonnet — reaps follow-ups from done-receipts into held proposals; state `harvest_candidates.json`, `disposition_digest.json`) · propose **06:00** · **stratege-gutachter 06:30** (`~/agents/stratege-gutachter/iterate.sh` — judges held proposals GO/SHARPEN/VETO, posts task comment + Discord + logs to its `memory/calibration.jsonl`; **shadow mode: advisory only, gates nothing**) · reflect **20:00**. Check with `systemctl --user list-timers | grep strateg`. Prompts: `~/.hermes/vision-flywheel/strategist-{propose,reflect}-prompt.md`. `--author` defaults to the active profile name, not a fixed user.

## Gotchas (the non-obvious)
- **Veto has NO CLI** (asymmetry: approve = `release-freigabe` CLI+UI; veto = dashboard/API only). `kanban archive` does NOT write `freigabe_vetoed` → reflect won't count it and the lever won't be suppressed. Veto via dashboard/API for correct learning.
- **Dispatcher/sweep code is loaded at gateway process start.** A fix to `kanban_db.py`/the sweep/dispatcher goes live only after `systemctl --user restart hermes-gateway.service`. `deploy_dashboard.sh` restarts ONLY the dashboard (API/frontend reads), NOT the dispatcher.
- **`freigabe` is required** in every binding PlanSpec; `operator` = held, other values build normally. Validate before ingest: `hermes plan validate <spec>`.
- **Härter blocks residue markers** (`TODO`/`<…>`/`...`) unless in backtick/code spans. Write spec markers in backticks, or `hermes plan ingest <spec> --force` (operator escape hatch).
- **Budget self-skip:** propose ends `skipped:true` when weekly subscription usage (the more-spent of opus-week / overall window) > 80% — a valid no-op, not an error. `idle:true` with no ingest = nothing was ROI-positive after self-gate (also valid).
- **Real propose is prompt-mediated:** the wrapper hands the prompt to `claude -p` Opus, which itself calls `hermes vision strategist --mode propose --drafts-file ~/.hermes/state/strategist/levers.json`. If a run logged but ingested nothing, check `levers.json` + the run log under `~/.hermes/logs/strategist/` — the ingest step isn't a guaranteed shell call.
- **Self-heal can defeat holds:** see [[reference_kanban_holds_and_links]] — any rescue/auto-curate sweep must exempt operator holds, and `task_links` is inverted (parent_id=child, child_id=root).

## Phase 2
Auto-deploy (`freigabe:auto`) is designed + locked (`…-phase2-design.md`), unlocked once dogfood is green — needs the operator's manual flip, not in this skill.
