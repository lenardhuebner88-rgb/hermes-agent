# Execution Facts — what the data actually supports

Measured 2026-07-31 09:36 CEST (after the first full usage scan) against the live ledger
(`/mnt/data/hermes-observability/execution_facts.db`).

This is the answer to "what can we analyse today, and what would we be
fooling ourselves about". It is written in terms of *questions you can ask*,
not tables you can join, and it is the intended starting point for building
any view on this data. Numbers are a snapshot; the shape is stable.

Companion documents: the field-level contract is
[execution-facts-contract.md](execution-facts-contract.md), operations are in
[execution-facts-runbook.md](execution-facts-runbook.md), and the proof status
of each invariant is in
[execution-facts-validation-matrix.md](execution-facts-validation-matrix.md).

## Traffic light

| question | status | number |
|---|---|---|
| Which execution was this? | **green** | 99.16 % exact identity |
| What happened to it? | **green** | outcomes 100 %, 11 171 executions |
| When did it run, how long? | **green for kanban/cron**, derived for old runs | see A below |
| Did it ship? | **green since today** | 2 491 landed, 2 472 with commit SHA |
| Was it deployed? | **red** | no evidence at all |
| What did it cost? | **amber** | 95 247 of 103 412 executions priced; money still needs a fee input — see B |
| Which task did this terminal work on? | **red** | wiring exists, no data yet — see C |
| Why did it fail? | **amber** | 13.85 % instrumented |

## Which source is authoritative

Binding decision: `vault/00-Canon/decisions/2026-07-31-metrik-ssot-register.md`,
extending `vault/00-Canon/decisions/2026-07-27-kosten-ssot-im-lesepfad.md`.
Three metric families, three sources — reading a metric out of any other
store gives a *wrong* number, not just a stale one:

| family | metrics | SSOT | explicitly NOT |
|---|---|---|---|
| **Consumption** | tokens (all 6 kinds), model, provider, `billing_mode`, `serving_tier`, tool calls, cost | `/mnt/data/hermes-observability/usage_facts.db` (`run_usage_facts`, `run_llm_calls`, `run_traces`) | `task_runs.input_tokens`/`output_tokens`/`cost_usd` |
| **Lifecycle** | queued/claimed/started/first_request/first_token/ended, duration, outcome, exit code, retry origin | `~/.hermes/kanban.db` (`task_runs`, `worker_run_timeline_events`, `worker_run_terminal_facts`, `worker_run_retry_links`) | `usage_facts` (there, `duration_ms` is populated for 3.5 % of rows and `first_token_ms` for 1.7 % — practically empty) |
| **Execution identity** | which run this was, across Kanban/Cron/systemd/crontab/Loops/tmux; landing evidence | `/mnt/data/hermes-observability/execution_facts.db` (append-only derivation — this file) | a second measurement in any other source |

`task_runs.input_tokens`/`output_tokens`/`cost_usd` is **deprecated for
consumption**, not merely secondary: that table has no
`cache_read_tokens`/`cache_write_tokens` column and structurally never can
have one, since the standing rule behind the 2026-07-27 decision forbids
retrofitting new columns into `hermes_cli/kanban_db.py`. Measured against one
real chain (LL-2, 2026-07-31): the `task_runs` view reports **$31.61 / 7.19 M
tokens**, while `usage_facts` for the same chain reports **$20.88 / 3.82 M
input + 19.26 M cache tokens**. `task_runs` is *higher* on a *smaller* token
base only because it prices cache tokens at the full input rate, never having
recorded them as cache — any consumer still reading consumption out of
`task_runs` is measurably wrong, not just behind.

**Cutoff:** the consumption SSOT counts as complete only from **2026-07-27
16:00** (fact-layer go-live) — 5.3 % coverage of Kanban runs before that
timestamp, 94.6 % after. May/June stay honestly unmeasured; July is being
backfilled. An evaluation spanning the cutoff must name the boundary
explicitly rather than average across it.

## Identity: solved

`run_identity_adoption` is **99.16 %** (17 973 of 18 125), validity `exact`.

| source | executions | exact | derived |
|---|---:|---:|---|
| kanban_timeline | 8 846 | 9 804 ev. | 17 450 ev. |
| crontab_invocation | 6 891 | all | 0 |
| git_landing | 2 472 | all | 0 |
| hermes_cron | 2 161 | all | 0 |
| systemd_invocation | 293 | all | 0 |
| usage_facts | 261 | 146 ev. | 315 ev. |
| loop_ledger | 152 | **0** | 432 ev. |
| tmux_reconciliation | 6 | all | 0 |

The only source without exact identity is `loop_ledger` — 152 executions,
0.84 % of the total. Historical loop ledgers carry no universal run id, so
date and round stay `derived`.

**The activation gate is met on the data side** (the goal plan asks for
≥ 99 %). Activation remains a separate operator decision: every payload
carries `activation_effect: none` by design.

## A) Timing — exact only since 2026-07-29

`kanban_timeline` shows 9 804 exact against 17 450 derived events, and that
ratio is not a defect to fix. Per-phase instrumentation
(`worker_run_timeline_events`) only exists since 2026-07-29: 131 of 8 846
task runs have it. For the 8 714 runs before that, phase timestamps were
never recorded, so the reconciler falls back to `started_at`/`ended_at` from
`task_runs` and honestly marks them `derived`.

Consequences:

* `first_llm_request` / `first_token` do not exist for ~98.5 % of runs. There
  is no legacy column to recover them from — this is not backfillable.
* Even for new runs, coverage is ~87 % (131 of 151): the worker needs
  `HERMES_KANBAN_RUN_ID` and `HERMES_KANBAN_DB` in its environment, and
  without them the recorder silently does nothing.

So: trust exact timing for runs since 2026-07-29, treat older ones as
coarse start/end only, and do not compute latency trends across that
boundary.

## B) Cost — tokens are real, money is not yet

Token counts are trustworthy. Euro/dollar amounts are not, and the metric
says so: `usage_cost_coverage` is `unknown`,
reason `missing_price_or_subscription_allocation`. Coverage is now 95 247 of
103 412 executions (92.10 %) — the denominator before the first full scan was
378.

Three distinct cost concepts — do not mix them up when building a view:

* **`api_equivalent_cost`** — what the request *would* have cost at list
  price. Populated and meaningful. For the Claude Code lane alone this is
  **$11 659.20** across 104 253 rows. Read it as *the value the subscription
  returns*, not as spend.
* **`marginal_cost`** — what one more request actually costs. **0** for
  anything with `billing_mode = subscription_included`. Until today this
  wrongly carried the list price, so the system reported $11 659.20 of spend
  that does not exist. The pricing route infers metered-vs-subscription from
  the provider label, and Claude Code reports `anthropic`, which reads as
  metered API usage; the recorded `billing_mode` is now the authority.
* **`allocated_subscription_cost`** — the share of the monthly fee
  attributable to a run. **Never populated**, because no fee input was ever
  supplied.

### What is still missing for real money numbers

1. **The monthly fees themselves.** Format is
   `{"YYYY-MM:<provider>": "<amount>"}` (see
   `tests/fixtures/execution_facts/subscription-fees.example.json`), passed
   via `--subscription-fees <file> --fee-version <tag>`. The subscription
   lanes needing an amount, for 2026-07: `anthropic` (104 253 rows), `qwen`
   (1 345), `openai-codex` (1 055), `openai` (624), `xai` (583),
   `kimi-code` (110), `kimi-coding` (80), `alibaba-token-plan` (59),
   `xai-oauth` (15). **This is operator knowledge; nothing in the system can
   derive it.**
2. **An FX rate**, if loop costs are to be aggregated with usage costs. Loop
   ledgers record EUR, usage facts USD, and mixing currencies is refused
   rather than guessed. Supply `--fx-rates '{"EUR": "1.08"}' --fx-version
   <tag>`. Note the 238 EUR events already in the append-only ledger keep
   their currency; only newly reconciled events convert.
3. **Price coverage.** Two numbers circulate here and both are correct — they
   count different things, so state which one you mean:
   * **8 165 executions** without a cost (of 103 453 eligible) — the
     readmodel's unit, after usage rows collapse onto their execution.
   * **7 083 raw rows** without a resolvable price (of 109 404 in
     `usage_facts.db`) — the storage unit, breakdown below.

   The row-level view of those 7 083 — note what this does and
   does not cost us, now that subscription rows are free at the margin:
   * **6 805 are subscription rows.** Their marginal cost is 0 regardless, so
     nothing is mis-stated; only `api_equivalent_cost` (the "what would the
     API have charged" figure) is missing. Largest groups:
     `qwen3.8-max-preview` / `qwen3.7-max` recorded under provider
     `anthropic` — a router label mismatch, most likely MoA advisor calls —
     and `xai` rows with `model = NULL`.
   * **278 are not marked as subscription**, and these are the real gap. The
     cause is a missing field rather than a missing price: 277 are
     `hermes_agent`/`openai-codex`/`gpt-5.6-sol` with `billing_mode = NULL`
     even though `openai-codex` is a subscription lane, and most of the rest
     are `hermes_aux` rows with `provider = NULL`. Fixing this belongs in
     usage capture, not in pricing.

### Why the cost denominator looks unlike the execution count

Two independent reasons, both by design rather than by defect:

* Usage rows without a `task_run_id` — 103 445 of 109 363, mostly
  interactive sessions — each become their own execution, while rows with a
  task run collapse onto that run's execution.
* The collector reads a sample of usage rows by default. That cap used to be
  liftable only as a side effect of configuring fees; `--usage-sample-limit
  0` now reads all rows independently. **A full scan takes ~6 minutes**, so
  it runs as its own daily unit
  (`hermes-execution-facts-fullscan.timer`, 04:40) rather than in the
  15-minute collector. The ledger holds 254 262 events after the first full
  pass.

## C) Terminal ↔ task — wired today, still without data

`task_binding_projection` was empty because nothing ever bound: the collector
asked tmux for six fields, none of them the task, and `bind_task_run()` had
no production caller at all. Both halves now exist and are connected.

It is still empty in practice, and the reason is worth knowing: the six live
panes carry `@hermes_kind` (claude, codex, kimi, qwen, grok) but **no**
`@hermes_task_id` — they are interactive agent terminals, not task workers.
Bindings will appear when a task-bound terminal runs. Do not build a view
that assumes this table is populated.

## D) Landing and deployment

**Landing is evidence-backed since today.** 2 491 executions landed (22.30 %
of 11 171), 2 472 of them carrying a commit SHA. The evidence is the git
history itself: the landing convention names the task in the commit subject
(`kanban(t_ab12cd34): …`), and reachability from `main` proves the work
shipped. The landing event reuses the *kanban* execution identity, so
evidence attaches to the execution that produced the work.

Landing overall still reports `derived` because the pre-existing loop-ledger
landings have no SHA; the git-backed ones are `exact`.

**Deployment has no evidence at all** — `unknown`,
reason `deployment_evidence_absent`. Reachability from `main` proves
*shipped*, not *deployed*. Closing this needs evidence from the deploy path
(`scripts/deploy_dashboard.sh`, or the `release/pre-deploy/*` tags) tied back
to a SHA. This is the largest remaining blind spot.

## E) Errors

`error_rate` is `unknown`, reason `incomplete_error_instrumentation`: 1 547
of 11 171 executions carry an error class (13.85 %). The classes that are
present are trustworthy; their *absence* means "not instrumented", not "no
error". Do not compute an error rate from this yet.

Outcomes, by contrast, are complete (11 171, 100 %, `exact`) across 16
statuses: `completed` 6 863 · `reclaimed` 2 102 · `blocked` 804 ·
`gave_up` 186 · `spawn_failed` 166 · `crashed` 127 · `timed_out` 94. The
~19 % `reclaimed` share is worth investigating on its own.

## Where the data lives

| store | path | role |
|---|---|---|
| execution facts ledger | `/mnt/data/hermes-observability/execution_facts.db` | append-only events + projections |
| usage facts | `/mnt/data/hermes-observability/usage_facts.db` | tokens/model/duration per run |
| sentinel status | `/mnt/data/hermes-observability/sentinel-status.json` | weekly non-agentic smoke |
| shadow evidence | `/mnt/data/hermes-observability/execution-facts-shadow/` | per-scan census payloads |

All overridable by environment variable (`HERMES_EXECUTION_FACTS_DB`,
`HERMES_USAGE_FACTS_DB`, `HERMES_SENTINEL_STATUS_PATH`) — tests must set
them, otherwise they read live host state and change verdict on their own.

## Collection health — how to tell it is really working

The collector runs every 15 min via `hermes-execution-facts-shadow.timer`.
`systemctl --user is-active` is **not** sufficient evidence: the unit can be
green while a source has silently stopped contributing.

* `collector.identity_conflicts` — must be **0**. Non-zero means a source
  restated a fact under an identity it had already used; the retained fact
  wins and the sweep continues, but that source's identity rule is broken.
* `cohorts[].dedupe_reconciled` — false means that source did not reconcile.
* per-source `MAX(ingested_at_ms)` — a source whose timestamp stops
  advancing while others move on has died quietly.

The third check is what would have caught the 2026-07-31 outage in minutes
instead of hours: from 04:28 the unit failed every 15 min and 7 of 11 sources
stopped collecting, while the failure looked like one generic unit error.

## Known-wrong data that was removed

Two superseded crontab identity generations were deleted by
`scripts/migrate_execution_facts_crontab_identity.py` (backup:
`execution_facts.db.pre-crontab-identity`), because generations look alike to
the projection and double-count:

1. `crontab:BOOT:PID` (14 456 rows) — a recycled PID merged unrelated runs,
   and `min()` over a rotating journal window let a retained fact be
   restated. That restatement raised `IdempotencyConflictError` and rolled
   back the whole batch — the outage above.
2. `crontab:BOOT:PID:FIRST_MS` (17 612 rows) — stable, but counted every CRON
   journal line as an execution. On this host 65.2 % of those lines are PAM
   session bookkeeping and 2.2 % are daemon notices; only 32.6 % are runs.

Current: `crontab:USER-LABEL-DIGEST:RUN_MS`, taken from the command the
journal states — e.g. `crontab:piet-loop_monitor.py-…`,
`crontab:piet-flock-…` (oma-sync), `crontab:root-command-…` (debian-sa1).

Lesson worth keeping: when a source reports 0 % identity, check what the
reader discards before concluding the data cannot support identity.

## Next, in order of what it unlocks

1. **Subscription fees** (operator input) — turns tokens into money for the
   lanes where nearly all the spend is.
2. **Deployment evidence** — the last completely dark stage of the pipeline.
3. **Error instrumentation** — turns a 13.85 % partial signal into a rate.
4. **Price coverage** for the 7 083 unpriced rows.
5. **Loop run ids** — the last source without exact identity.
6. **Task-bound terminals** — makes the terminal↔task join produce data.
