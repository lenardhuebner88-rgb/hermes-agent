# Vision-Flywheel Phase 2 — Strategist Harness (I1) — Call Contract

The **Strategist** is the proposing/reflecting head of the self-improving
PlanSpec pipeline. This document is the **call contract** the operator's
`strategist-cron` relies on. The cron wiring (systemd unit + crontab) is
**operator work and lives outside this repo**; everything below is the
repo-side, callable logic (`hermes_cli/strategist.py`, exposed as
`hermes vision strategist`).

## Where the pieces live

| Piece | Location | Owner |
|---|---|---|
| Repo logic (propose/reflect/harvest/harvest-watch/digest) | `hermes_cli/strategist.py` | this repo (I1) |
| CLI surface | `hermes vision strategist --mode propose\|reflect\|harvest\|harvest-watch\|digest` | this repo |
| Annotation contract (emit/parse) | `hermes_cli/strategist_surface.py` (`format_annotation` / `parse_annotation`) | G1 |
| Held-proposal surface (read side) | `strategist_surface.held_operator_proposals` + `/api/strategist/proposals` | G1 |
| Escalation ledger (input) | `kanban_db.read_escalation_ledger` | Phase 1 (Heiler) |
| Vision metrics (input) | `~/.hermes/state/vision-metrics.json` | H1 |
| Budget gate (input) | `agent.account_usage.fetch_account_usage` | existing |
| systemd unit + cron schedule | `~/.config/systemd/user/…` (operator) | **operator, not this repo** |

## How the cron invokes it

```sh
# PROPOSE — twice daily (operator schedule). Opus is the strategist brain; the
# repo logic is the deterministic, self-gated, budget-disciplined scaffolding.
claude -p --model claude-opus-4-8 "$(cat strategist-propose-prompt.md)"

# REFLECT — once daily, after the operator has had time to triage.
claude -p --model claude-opus-4-8 "$(cat strategist-reflect-prompt.md)"

# HARVEST-WATCH — cheap autonomous backlog watchdog (operator timer/cron).
# The repo CLI reads Hermes config and only runs a special harvest when the
# disposition backlog crosses disposition_special_run_threshold/rearm.
hermes vision strategist --mode harvest-watch
```

The Opus prompt instructs the strategist to:

1. **Read context cheaply** and judge where ROI lives across the broad Vision
   corridor (autonomy metrics · Heiler root-causes · dashboard/DX/new
   capabilities). **Heavy reads (code, receipts) are delegated to Sonnet
   subagents — Opus only judges.**
2. Either run the deterministic baseline directly:
   ```sh
   hermes vision strategist --mode propose --json
   ```
   or supply its own richer, judged lever drafts and feed them through the same
   self-gate + cap + provenance + annotation rails:
   ```sh
   hermes vision strategist --mode propose --drafts-file levers.json --json
   ```
3. Once daily run reflection:
   ```sh
   hermes vision strategist --mode reflect --json
   ```

Either path applies the same rails. The deterministic baseline guarantees the
harness works headless even if the LLM adds nothing; `--drafts-file` is the
seam where Opus's judgement enters.

## `--mode harvest-watch`

Cheap autonomous backlog watchdog for disposition-harvest cadence. It reads the
normal Hermes config keys (`disposition_special_run_threshold` and
`disposition_special_run_rearm`), counts open disposition ledger rows, and
delegates to the regular `harvest` path only when the backlog crosses the
configured threshold/cooldown. It performs no deploy, restart, push, or direct
task creation.

Repo-side manual/dashboard trigger path:

```sh
hermes vision strategist --mode harvest-watch
```

The repo ships optional user-systemd templates for the preferred operational
path:

```sh
plugins/kanban/systemd/strategist-harvest-watch.service
plugins/kanban/systemd/strategist-harvest-watch.timer
```

The timer is conservative (`OnBootSec=15min`, `OnUnitActiveSec=30min`) and the
service uses `/usr/bin/flock -n /tmp/hermes-strategist-harvest-watch.lock` so a
still-running watch is skipped rather than overlapped. These units are not
auto-armed by the repo; copying them to `~/.config/systemd/user/`, running
`systemctl --user daemon-reload`, and enabling/starting the timer remains an
explicit operator/runtime action.

## `--mode propose`

### Inputs
- `~/.hermes/state/vision-metrics.json` (H1). Absent/malformed → degrades to
  "no snapshot" (metric levers simply don't fire). Override path with
  `HERMES_VISION_METRICS_PATH`.
- Heiler escalation ledger (`read_escalation_ledger`) — `by_class` counts drive
  the root-cause levers.
- Weekly subscription usage (`fetch_account_usage`, provider `--budget-provider`,
  default `anthropic`).
- `~/.hermes/state/strategist/vetoed_levers.json` (reflect-fed) — lever keys the
  operator vetoed are **suppressed**.
- Optional `--drafts-file <json>`: `{"levers": [ … ]}` or a bare `[ … ]` of
  lever dicts (`key,title,lane,target_metric,roi,counter_metric,counter_risk,
  gain_weight,cost,signal_strength`).

### Pipeline
1. **Budget skip** — weekly usage > `--budget-threshold` (default `80`) → run
   ends with `skipped:true`, nothing drafted. Indeterminate usage (no snapshot /
   no weekly window / fetch error) does **not** skip (a transient usage hiccup
   must not silence the strategist) — it proceeds and records why.
2. **Derive levers** from context (suppressed keys removed).
3. **Self-gate** each draft (cheap, deterministic): must carry a paired
   counter-metric, have a positive ROI score (`signal*gain − cost`), and a
   guardrail risk within `COUNTER_BUDGET` (0.5). A blunt lever that can't bound
   its counter-metric is refused.
4. **Cap** survivors to `--cap` (default 5, intended band 3–5), ranked by ROI.
5. **Ingest** survivors with `author=strategist-cron` and frontmatter
   `freigabe: operator` + a `strategist_meta` block. They pass the Phase-1
   hardener (deterministic rubric + Sonnet judge) and land **held** (root parked
   in `scheduled`) on the G1 surface. A judge-refused baseline draft is recorded
   in `ingest_errors` and skipped — it never kills the run.

### Output (`--json`)
```json
{
  "mode": "propose",
  "skipped": false,
  "reason": "weekly usage 42.0% within budget",
  "used_percent": 42.0,
  "idle": false,
  "candidates": 7,
  "survivors": 6,
  "capped": 5,
  "cap": 5,
  "gated_out": [{"key": "AUTON-UPLIFT", "title": "…", "reason": "Counter-Metrik-Risiko 0.60 …"}],
  "ingest_errors": [],
  "ingested": [
    {"key": "HEILER-TRANSIENT", "title": "…", "root_task_id": "t_…",
     "subtask_count": 2, "target_metric": "…", "roi": "…", "counter_metric": "…",
     "already_ingested": false}
  ]
}
```
- `skipped:true` → budget skip (idle by budget).
- `idle:true` with `ingested:[]` → nothing was ROI-positive after self-gate
  (a correct, expected outcome — not an error).

## `--mode reflect`

### Inputs
- The board: tasks `created_by = strategist-cron` and their `task_events` since
  local midnight (`freigabe_released` = approved, `freigabe_vetoed` = vetoed,
  approved root reaching `done` = shipped).

### Effect
- Appends a record to `~/.hermes/state/strategist/reflections.jsonl`.
- Merges vetoed lever keys into `~/.hermes/state/strategist/vetoed_levers.json`
  (the suppression set the next propose run reads). **Vetoed feeds the
  reflection** — the operator's veto teaches the strategist what not to re-raise.

### Output (`--json`)
```json
{"mode": "reflect", "approved": [ … ], "vetoed": [ … ], "shipped": [ … ],
 "note": {"approved": 1, "vetoed": 1, "shipped": 0,
          "approved_levers": ["HEILER-TRANSIENT"], "vetoed_levers": ["AUTON-UPLIFT"]},
 "suppressed_levers": ["AUTON-UPLIFT"], "notes_path": "…/reflections.jsonl"}
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Ran to completion — including budget-skip and idle (both are valid no-op outcomes). |
| `2` | A drafts-file / spec file could not be read (`FileNotFoundError`). |

The strategist **never** pushes, deploys, restarts, or edits outside the board +
its own state directory. It only ingests held proposals (operator-gated) and
writes its own learning notes.

## Shared outcome vocabulary

Strategist outcome records now project the same independent applicability,
measurement, verdict and evidence dimensions used by Autoresearch. The existing
`~/.hermes/state/strategist/lever-outcomes.json` list remains the compatibility
read model; historical verdicts and timestamps are preserved and explicitly
graded `legacy_observational`.

New measurable Strategist baselines carry an immutable
`vision_metric_snapshot.v1` contract for a reviewed metric/direction pair.
Stale snapshots and records without an observed baseline cannot produce a new
directional verdict. A task completed through the explicit "done elsewhere"
event is terminal `not_applicable`, not shipped and not training evidence.

Autoresearch may be visible through the common operator outcome surface, but
its rows are always `calibration_eligible=false`; they are excluded from
`compute_lever_calibration`. Delivery and measurement remain separate:
`integrated` or `shipped` alone never means `improved`.

The detailed contract, source hierarchy, shadow rollout and rollback procedure
is in [`autoresearch-outcome-verification.md`](autoresearch-outcome-verification.md).
