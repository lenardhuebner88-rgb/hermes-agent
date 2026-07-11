---
name: hermes-multiproject-kanban
description: Use when dispatching, monitoring, or closing Hermes kanban worker chains in a NON-default repo/board (health-track, a new project repo) — creating a board/project for a fresh repo, routing a PlanSpec to a board, releasing a held freigabe chain, closing a root stuck in "scheduled — held before release", marking a PlanSpec shipped, or wiring worker/integration gates for a new repo.
---

# Hermes Multi-Project Kanban

Dispatch worker chains into any repo via boards. Deep background: `/home/piet/vault/00-Canon/planspec-taskgraph.md` (binding schema) and `docs/kanban/ingest-worker-system-runbook.md`. Live worked example: PlanSpec `vault/03-Agents/Claude-Code/plans/2026-07-11-ht-kanban-testrun-gewicht-error-feedback.md` (shipped, chain t_11cd1b24 on board `health-track`).

`H=/home/piet/.hermes/hermes-agent/venv/bin/hermes` (canonical venv).

## Quick reference (exact, verified commands)

| Step | Command |
|---|---|
| New repo → board | `$H kanban boards create <slug> --name "<Name>" --default-workdir <abs-repo>` |
| Project record | `$H project create "<Name>" <abs-repo> --slug <slug> --board <slug>` (optional once the board exists — board routing alone suffices; the record feeds `kanban create --project`) |
| Route PlanSpec | frontmatter `board: <slug>` (validator BLOCKs unknown slugs) — or `plan ingest/validate --board <slug>` (CLI wins) |
| Validate (dry) | `$H plan validate <spec.md> --board <slug>` — spec must live under `vault/03-Agents/<Agent>/plans/` |
| Ingest | `$H plan ingest <spec.md> --board <slug> --author <me> --json` → keys `root_task_id`, `child_ids` (NOT `id`) |
| Release chain | `$H kanban --board <slug> release-freigabe <root_id> --author <me>` (operator-GO; dispatcher picks up in <1 min, all boards polled per tick, no restart) |
| Monitor | `$H kanban --board <slug> show\|list\|tail\|runs\|log <id>` — `tail`/`runs`/`log` target the CHILD id (the root never runs); `--board` goes BEFORE the kanban verb; never `boards switch` (global state, other sessions) |
| Close held root | `$H kanban --board <slug> complete-freigabe <root_id> --note "<evidence>"` — the ONLY way out of "scheduled — Planspec ingest: held before release"; `--note` mandatory |
| Mark spec shipped | `$H plan shipped <spec.md> --kanban-root-task-id <root_id> --kanban-state done --release-evidence "<merge sha + gates>"` — WITHOUT these flags it BLOCKs (board-blind lookup) |

Scripts: pin the board with `HERMES_KANBAN_BOARD=<slug>` env instead of the flag (what `dogfood.sh` needs).

## How routing works (one paragraph)

Board `default_workdir` = the repo. Ingest onto that board writes code-children as `dir` tasks on the repo; the worker runs in an isolated worktree `<repo>/.worktrees/kanban/<root_id>` with the target repo's own AGENTS.md as context, and the integrator merges back to the repo's local `main` (never pushes). Foreign-board work is invisible in /control unless you use the Fleet board switcher.

## Gates for a new repo (config, hot-read — no restart)

In `~/.hermes/config.yaml` under `kanban:`; keys are ABSOLUTE repo paths:
- `worker_gate.repos.<repo>: [cmds]` — pre-submit, runs in the worker's worktree. Without an entry the worker gate is a **silent no-op** — put the gate in the AC too.
- `integration_gate.repos.<repo>: [cmds]` — post-merge, runs in a clean validation worktree (fresh: include `npm ci`). Without an entry a Next.js repo coincidentally gets the FO heuristic (build only, no lint/tests).
Commands are `shlex.split`, no shell operators (`&&` breaks).

## Traps

| Trap | Reality |
|---|---|
| Parsing `--json` output blind | stdout may start with a pkg_resources DeprecationWarning — take the LAST line / filter before `json.loads` |
| Ingest JSON key | `root_task_id` + `child_ids`; there is no `id` key |
| Root "done" never happens by itself | freigabe:operator roots stay held after children merge — `complete-freigabe` is required |
| `plan shipped` says "terminal state required" | It cannot find foreign-board chains — pass `--kanban-root-task-id` + `--kanban-state` explicitly |
| Retargeting a mis-routed task | Impossible via CLI (`respec` copies workspace unchanged) — archive and re-ingest to the right board |
| Frontend slice AC | `kanban.visual_gate` is ON for hermes-agent `web/src/control/` diffs; other repos: demand screenshots/MANUAL-VISUAL in the AC |
| gradle in worker ACs | non-login shells have no JAVA_HOME — `scripts/worker-gate-android.sh` probes `~/Android/jdk`; reuse it, don't call gradlew raw |
