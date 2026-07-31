# Execution Facts — what the data actually supports

Measured 2026-07-31 08:35 CEST against the live ledger
(`/mnt/data/hermes-observability/execution_facts.db`), after the crontab
identity fix and the projection rebuild of the same morning.

This is the answer to "what can we analyse today, and what would we be
fooling ourselves about". It is deliberately written in terms of *questions
you can ask*, not tables you can join. Numbers are a snapshot; the shape is
what matters and the shape is stable.

Companion documents: the field-level contract is
[execution-facts-contract.md](execution-facts-contract.md), operations are in
[execution-facts-runbook.md](execution-facts-runbook.md), and the proof status
of each invariant is in
[execution-facts-validation-matrix.md](execution-facts-validation-matrix.md).

## The one thing to understand first

`run_identity_adoption` currently reads **38.32 %** (11 035 of 28 799). That
number is not a quality score and chasing it upward is a mistake.

61 % of its denominator is `crontab_invocation` — 17 612 executions harvested
from the systemd journal, every single one of them `derived` and **zero** of
them `exact`:

| source | executions | exact | derived |
|---|---:|---:|---:|
| crontab_invocation | 17 612 | 0 | 17 612 |
| kanban_timeline | 8 844 | 9 786 ev. | 17 450 ev. |
| hermes_cron | 2 121 | 6 363 ev. | 0 |
| systemd_invocation | 236 | 359 ev. | 0 |
| loop_ledger | 152 | 0 | 432 ev. |
| usage_facts | 250 | 146 ev. | 225 ev. |
| tmux_reconciliation | 6 | 144 ev. | 0 |

A crontab line in the journal gives us a boot id, a PID and a timestamp. It
does not give us — and cannot be made to give us, without changing how the
host's crontab is written — a stable identifier that ties that process to a
piece of our work. These are mostly *other people's* cron jobs on the same
host.

So the denominator mixes two different populations:

* **Our work** — kanban tasks, hermes cron, loops, terminals. Identity here is
  a solvable engineering problem.
* **Ambient host activity** — foreign crontab processes. Identity here is not
  solvable and not interesting.

**Consequence for the goal:** the Qwen goal plan
(`vault/03-Agents/Codex/plans/2026-07-31-execution-facts-live-gap-closure-qwen-goal.md`)
sets an activation gate of "≥ 99 % adoption". Against this denominator that
gate is not merely unmet, it is **mathematically unreachable** — 61 % of the
denominator can never become exact. The gate needs to be restated against
*eligible-for-attribution* executions before anyone tries to satisfy it.

## What you can analyse today, honestly

### Fully supported — ask freely

* **Kanban task lifecycle.** 8 844 executions, lifecycle coverage **100 %**,
  validity `exact`. Every task's phase transitions are there.
* **Outcomes.** 11 087 executions, coverage **100 %**, validity `exact`, split
  across 16 statuses. The distribution is itself informative:
  `completed` 6 863 · `reclaimed` 2 102 · `blocked` 804 · `gave_up` 186 ·
  `spawn_failed` 166 · `crashed` 127 · `timed_out` 94.
  That is ~19 % `reclaimed` — worth a look on its own.
* **Hermes cron executions.** 2 121 executions, all `exact`.
* **Terminal runs.** 6 executions, 100 % identity — correct but tiny; do not
  generalise from it.

### Partially supported — usable with the caveat stated

* **Error classification.** `computed_status: unknown`,
  reason `incomplete_error_instrumentation`. 1 519 of 11 087 executions carry
  an error class (13.70 %), 8 377 were measured. The classes present are
  trustworthy; their *absence* means "not instrumented", not "no error".
  Do not compute an error rate from this yet.
* **Cost and tokens.** 340 of 367 executions have usage rows (92.64 %) but the
  metric is `unknown`, reason `missing_price_or_subscription_allocation`.
  Token counts are real. Euro amounts are not derivable for subscription
  lanes until a versioned monthly fee input exists. See the fixture at
  `tests/fixtures/execution_facts/subscription-fees.example.json`.

### Not supported — do not build on these yet

* **Landing.** 19 of 11 087 executions (0.17 %), and `sha_evidence: 0` — not a
  single landing carries a commit SHA. The signal is derived from status
  transitions, not from evidence.
* **Deployment.** `computed_status: unknown`, reason
  `deployment_evidence_absent`. Zero observations.
* **Loop identity.** 152 executions, **0 exact**. Historical loop ledgers have
  no universal run id, so date/round stays `derived`.
* **Task bindings.** `task_binding_projection` is empty (0 rows). Nothing
  joins executions to tasks through this path yet.

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

* `collector.identity_conflicts` — must be **0**. Non-zero means some source
  restated a fact under an identity it had already used. The retained fact
  wins and the sweep continues, but that source's identity rule is broken.
* `cohorts[].dedupe_reconciled` — false for a source means that source's facts
  did not reconcile this pass.
* per-source `MAX(ingested_at_ms)` — a source whose timestamp stops advancing
  while others move on has died quietly.

That third check is what would have caught the 2026-07-31 outage in minutes
instead of hours: from 04:28 the unit failed every 15 min, and 7 of 11 sources
stopped collecting while the failure looked like a single generic unit error.

## Known-wrong data that was removed

The crontab identity was `crontab:BOOT:PID` until 2026-07-31. It merged
executions that reused a PID (6 859 of 13 698 buckets on this host) and
derived its observed time from `min()` over a journal window that rotates
underneath the collector. Two consequences, both now fixed:

1. It jammed the collector — a rotated-out record restated a retained fact,
   which raised `IdempotencyConflictError` and rolled back the whole batch.
2. It undercounted crontab executions by ~22 % (13 698 reported vs 17 604
   real).

The superseded generation (14 456 rows) was deleted by
`scripts/migrate_execution_facts_crontab_identity.py` and the projection
rebuilt; leaving it in place would have double-counted every crontab
execution (32 060 projected rows vs 17 604 real). Backup:
`execution_facts.db.pre-crontab-identity`.

## API surface

| endpoint | status |
|---|---|
| `GET /api/plugins/kanban/stats/observability` | live, consumed by the Statistik tab |
| `GET /api/plugins/kanban/stats/fleet-metrics` | live, **no frontend consumer** |
| `GET /api/plugins/kanban/stats/execution-facts` | live, **no frontend consumer** |

All three are read-only and carry `activation_effect: none` — passing a data
gate authorises nothing by itself.

## What to fix next, in order of what it unlocks

1. **Restate the adoption gate** against attributable executions instead of
   all executions. Until then every adoption number in this system reads as a
   failing grade for a reason that has nothing to do with our work.
2. **Landing evidence (SHA).** 0.17 % coverage with zero SHA evidence is the
   single largest blind spot: we cannot currently connect work to what shipped.
3. **Subscription fee input.** Unblocks euro-denominated cost for the
   subscription lanes, which is where most of the spend is.
4. **Error instrumentation.** Turns a 13.70 % partial signal into a real
   error rate.
5. **Loop run ids.** Small population, but it is the last source with zero
   exact identity that we actually control.
