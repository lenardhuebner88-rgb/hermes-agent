# Execution Facts — what the data actually supports

Measured 2026-07-31 08:52 CEST against the live ledger
(`/mnt/data/hermes-observability/execution_facts.db`), after that morning's
crontab identity fix and projection rebuild.

This is the answer to "what can we analyse today, and what would we be
fooling ourselves about". It is deliberately written in terms of *questions
you can ask*, not tables you can join. Numbers are a snapshot; the shape is
what matters and the shape is stable.

Companion documents: the field-level contract is
[execution-facts-contract.md](execution-facts-contract.md), operations are in
[execution-facts-runbook.md](execution-facts-runbook.md), and the proof status
of each invariant is in
[execution-facts-validation-matrix.md](execution-facts-validation-matrix.md).

## Identity: essentially solved

Every execution the system performs is now attributed to a named unit of
work. `run_identity_adoption` is **99.16 %** (17 926 of 18 078), validity
`exact`.

| source | executions | exact identity |
|---|---:|---|
| kanban_timeline | 8 845 | yes |
| crontab_invocation | 6 875 | yes |
| hermes_cron | 2 136 | yes |
| systemd_invocation | 256 | yes |
| usage_facts | 254 | partial |
| loop_ledger | 152 | **no** |
| tmux_reconciliation | 6 | yes |

The single remaining gap is `loop_ledger`: 152 executions, zero exact.
Historical loop ledgers carry no universal run id, so date and round stay
`derived`. That is 0.84 % of all executions and the only place where the
answer to "which run was this?" is still a guess.

**This closes the activation gate.** The Qwen goal plan
(`vault/03-Agents/Codex/plans/2026-07-31-execution-facts-live-gap-closure-qwen-goal.md`)
requires ≥ 99 % adoption; at 99.16 % that is met on the data side. Activation
itself remains a separate operator decision — passing a data gate carries
`activation_effect: none` by design.

### How it got there (and the lesson)

Until this morning the number read 38.32 %, and `crontab_invocation` reported
**zero** exact identity across 17 604 executions. The obvious reading — "cron
processes are inherently unidentifiable" — was wrong.

The collector asked journalctl for three fields (`_BOOT_ID`, `_PID`,
`__REALTIME_TIMESTAMP`) and counted every returned line as an execution. But
`MESSAGE` is present on **100 %** of CRON lines and states `(user) CMD
(command)`. There are only 9 distinct cron commands on this host. Identity
was never missing — it was never read.

Counting lines also inflated the source threefold. Over 7 days of journal:

| line kind | count | share | is it a run? |
|---|---:|---:|---|
| PAM session open/close | 9 791 | 65.2 % | no |
| `(user) CMD (…)` | 4 895 | 32.6 % | **yes** |
| "No MTA installed" notice | 336 | 2.2 % | no |

Lesson worth keeping: when a source reports 0 % identity, check what the
reader discards before concluding the data cannot support identity.

## What you can analyse today, honestly

### Fully supported — ask freely

* **Kanban task lifecycle.** 8 845 executions, coverage **100 %**, `exact`.
* **Outcomes.** 11 113 executions, coverage **100 %**, `exact`, across 16
  statuses: `completed` 6 863 · `reclaimed` 2 102 · `blocked` 804 ·
  `gave_up` 186 · `spawn_failed` 166 · `crashed` 127 · `timed_out` 94.
  ~19 % `reclaimed` is worth a look on its own.
* **Scheduled work.** hermes_cron, systemd and crontab together: 9 075
  executions, adoption **100 %**. You can now ask "how often did *this named
  job* run, and when" — e.g. `crontab:piet-loop_monitor.py-…`,
  `crontab:piet-flock-…` (oma-sync), `crontab:root-command-…` (debian-sa1).
* **Terminal runs.** 6 executions, 100 % identity — correct but tiny; do not
  generalise from it.

### Partially supported — usable with the caveat stated

* **Error classification.** `unknown`, reason
  `incomplete_error_instrumentation`. 1 521 of 11 113 executions carry an
  error class (13.69 %). The classes present are trustworthy; their *absence*
  means "not instrumented", not "no error". Do not compute an error rate yet.
* **Cost and tokens.** 344 of 371 executions have usage rows (92.72 %) but the
  metric stays `unknown`, reason
  `missing_price_or_subscription_allocation`. Token counts are real; euro
  amounts are not derivable for subscription lanes until a versioned monthly
  fee input exists. Fixture:
  `tests/fixtures/execution_facts/subscription-fees.example.json`.

### Not supported — do not build on these yet

* **Landing.** 19 of 11 113 executions (0.17 %) and **zero** carry a commit
  SHA. The signal is derived from status transitions, not evidence. We cannot
  currently connect work to what shipped.
* **Deployment.** `unknown`, reason `deployment_evidence_absent`. Zero
  observations.
* **Loop identity.** 152 executions, 0 exact (see above).
* **Task bindings.** `task_binding_projection` is empty. Nothing joins
  executions to tasks through this path yet.

## Where the data physically lives

| store | path | role |
|---|---|---|
| execution facts ledger | `/mnt/data/hermes-observability/execution_facts.db` | append-only events + derived projections |
| usage facts | `/mnt/data/hermes-observability/usage_facts.db` | tokens/model/duration per run |
| sentinel status | `/mnt/data/hermes-observability/sentinel-status.json` | weekly non-agentic smoke result |
| shadow evidence | `/mnt/data/hermes-observability/execution-facts-shadow/` | per-scan census payloads |

Every path is overridable by environment variable
(`HERMES_EXECUTION_FACTS_DB`, `HERMES_USAGE_FACTS_DB`,
`HERMES_SENTINEL_STATUS_PATH`) — tests must set them, otherwise they read live
host state and change verdict on their own.

## Collection health — how to tell it is actually working

The collector runs every 15 min via `hermes-execution-facts-shadow.timer`.
`systemctl --user is-active` is **not** sufficient evidence: the unit can be
green while a source has silently stopped contributing.

Check the collector payload instead:

* `collector.identity_conflicts` — must be **0**. Non-zero means a source
  restated a fact under an identity it had already used. The retained fact
  wins and the sweep continues, but that source's identity rule is broken.
* `cohorts[].dedupe_reconciled` — false means that source did not reconcile.
* per-source `MAX(ingested_at_ms)` — a source whose timestamp stops advancing
  while others move on has died quietly.

That third check is what would have caught the 2026-07-31 outage in minutes
instead of hours: from 04:28 the unit failed every 15 min and 7 of 11 sources
stopped collecting, while the failure looked like one generic unit error.

## Known-wrong data that was removed

The crontab identity was replaced twice on 2026-07-31 and both superseded
generations were deleted by
`scripts/migrate_execution_facts_crontab_identity.py` (backup:
`execution_facts.db.pre-crontab-identity`), because generations otherwise look
alike to the projection and double-count:

1. `crontab:BOOT:PID` (14 456 rows) — a recycled PID merged unrelated runs,
   and `min()` over a rotating journal window let a retained fact be
   restated. That restatement raised `IdempotencyConflictError` and rolled
   back the whole batch, which is what jammed the collector.
2. `crontab:BOOT:PID:FIRST_MS` (17 612 rows) — stable, but still counted
   session bookkeeping as executions.

Current: `crontab:USER-LABEL-DIGEST:RUN_MS`.

## API surface

| endpoint | status |
|---|---|
| `GET /api/plugins/kanban/stats/observability` | live, consumed by the Statistik tab |
| `GET /api/plugins/kanban/stats/fleet-metrics` | live, **no frontend consumer** |
| `GET /api/plugins/kanban/stats/execution-facts` | live, **no frontend consumer** |

All three are read-only and carry `activation_effect: none`.

## What to fix next, in order of what it unlocks

1. **Landing evidence (SHA).** 0.17 % coverage with zero SHA evidence is now
   the single largest blind spot: we cannot connect work to what shipped.
2. **Subscription fee input.** Unblocks euro-denominated cost for the
   subscription lanes, which is where most of the spend is.
3. **Error instrumentation.** Turns a 13.69 % partial signal into a real
   error rate.
4. **Loop run ids.** The last source without exact identity, 152 executions.
5. **Task bindings.** Empty projection; needed to join executions to tasks.
