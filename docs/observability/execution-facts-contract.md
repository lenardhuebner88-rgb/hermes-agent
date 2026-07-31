# Execution Facts V1

> Which store is authoritative per metric family (consumption / lifecycle /
> execution identity) is binding via
> `vault/00-Canon/decisions/2026-07-31-metrik-ssot-register.md`. This contract
> only defines the schema for the execution-identity family.

`execution-facts.v1` is the content-free, source-independent measurement
contract for Hermes executions. Its default database is
`/mnt/data/hermes-observability/execution_facts.db`, alongside the existing
observability stores. `HERMES_EXECUTION_FACTS_DB` may select an isolated
shadow database.

The raw truth is the append-only `execution_events` table. Every projection is
deleted and rebuilt from those events; projection tables are never a second
write truth. Raw events are retained for 90 days by default. Per-month,
per-source, per-event-type, per-validity aggregates remain permanently. A
content-free dedupe registry also remains permanently, so retrying an event
after raw retention still dedupes and reusing its source key for different
facts still fails closed.

## Identity

Every event carries:

- `execution_id`: universal identity used across source adapters.
- `source_execution_id`: the source's own run identity.
- `idempotency_key`: source-scoped retry identity.
- optional `span_id` and explicit `parent_span_id`.
- optional `terminal_run_id`, generation, `task_run_id`, task, chain, Cron,
  Loop, board, and profile identities.

Usage facts with an exact `task_run_id` adopt the corresponding Kanban
`execution_id`; they do not create a competing run. A terminal is a container.
Its task bindings are append-only spans, so one terminal can bind multiple
task runs without overwriting the previous binding.

Some source rows are mutable snapshots before they become final. Their
idempotency key includes a content-derived revision token: retrying an
unchanged snapshot dedupes, while a later measured revision appends instead
of conflicting with or overwriting the earlier raw event.

Pane death ends one terminal generation. Only an explicit close or archive
ends the terminal run. A durable tmux marker proves a respawn; location, PID,
time overlap, or window name never does.

## Validity

| Value | Meaning |
|---|---|
| `exact` | Direct source observation. |
| `lower_bound` | The fact starts no earlier or is no smaller than observed. |
| `derived` | Reproducible transformation from named source facts. |
| `unknown` | The source cannot support the assertion. This is never zero. |
| `not_applicable` | The milestone or metric cannot occur for this source. |

Projection precedence is `exact`, `lower_bound`, `derived`, `unknown`,
`not_applicable`. For lifecycle milestones, the earliest observation wins
within the same validity; for scalar fields and attributes, the latest
observation at the best validity wins. Field/attribute validity is projected
alongside the value, so a later derived fallback cannot overwrite an exact
fact. A composite span reports its weakest constituent validity.

## Lifecycle and outcomes

The common lifecycle is:

```text
scheduled → queued → claimed → spawn_started → process_started
→ first_request → first_token → running → ended
```

Review, integration, landing, and deployment are separate extensions.
`status`, `end_reason`, `error_class`, `retry_class`, `exit_code`, and evidence
reference remain separate. `done` is not landing evidence. Landing needs a
`landed` milestone and normally `landed_sha`; deployment needs its own
milestone and optionally `deployed_sha`.

The event type is a structural contract, not a label over optional fields:

- every lifecycle event requires an explicit `lifecycle_phase`;
- every span start/end requires both `span_id` and `span_kind`;
- task-binding start/end requires a task-binding span plus
  `terminal_run_id` and `task_run_id`;
- terminal observations and generation ends require both
  `terminal_run_id` and a positive generation;
- terminal close requires `terminal_run_id`.

An exit code is a real integer; booleans and string coercions are rejected.
Malformed partial events fail at the contract boundary instead of projecting
plausible zeroes or anonymous lifecycle facts.

Retry rates use only executions that carry `retry_instrumented=true`.
That flag is proven per run by a runtime timeline row or an exact retry-link
row. Merely finding the additive retry-link table does not retroactively mark
historical runs as instrumented. Mutable legacy and terminal facts include the
flag and all emitted fields in a content revision, so later exact corrections
append instead of colliding with an older immutable fact.
Uninstrumented historical runs are not mixed into that denominator.

## Spans

Agent, sub-agent, tool, test, task-binding, and operator spans use explicit
parentage. Sub-agent spans require a parent. Tool and test spans require a
parent and never infer causality from overlap.

Tool events contain the canonical and raw adapter name, agent/model,
workload/phase, start/end/duration, result, normalized error, timeout, and
retry metadata. Test events additionally retain selected, executed, passed,
failed, skipped, and timeout-lost counts, gate type, flake class, exit code,
and an optional command hash. A timeout with zero executed and 307 lost tests
therefore cannot render as a green zero-test run.

## Content boundary

V1 has no representation for prompts, commands, arguments, outputs, pane
contents, traces, or test logs. Attributes pass through a closed allowlist.
References use bounded colon-separated opaque identifiers; the manual
`record-shadow-proof` command accepts only a known source plus a SHA-256 digest.
URL queries, credentials, and arbitrary free text are rejected. The adapters
explicitly discard Cron error messages, Loop plans and
reasons, tmux content-shaped metadata, and Kanban summaries/errors.

## Source coverage

| Source | Direct facts | Historical limitation |
|---|---|---|
| Kanban | Runtime milestones, terminal result, error/exit, retry link | Legacy start/end fallback is `derived`; no landing is inferred from `done`. |
| Hermes Cron | Claim, process start, end/result | Retry is exact only when the optional retry-link columns exist. |
| Loops | Structured phase, usage, verdict | Without an explicit Loop run ID, the date/round identity is `derived`. |
| tmux | Every observed pane, state-neutral freshness observations, generation death, explicit close/archive | First observation of an existing pane is a lower bound; the adapter never reads pane content. |
| systemd/Crontab | Direct realtime systemd properties and bounded journal schedule/process/end observations | No command or journal text is accepted. |
| Usage Facts | Tokens, model, route, costs, exact task linkage | Missing tokens/prices/fees remain nullable. |
| Tool/test/activity | Explicit spans and parents | No production callpoint is enabled by this diff. |

## Universal eligible denominator

`execution-facts-source-census.v1` records one bounded observation for each
execution source. Its universal external-execution population is the sum of
the exhaustive eligible cohorts for Kanban, Hermes Cron, Loops, tmux,
systemd, and Crontab. Each source census carries:

- the explicit eligibility rule and observation window;
- `eligible`, `observed`, `identity_observed`, and `metric_observed`;
- source-store and read-error counts;
- writer p50/p95, dedupe/reconciliation, and behavior-equivalence evidence;
- the static read-only/snapshot mechanism that proves source neutrality.

A source that is absent or cannot supply a denominator records
`eligible=null` with `validity=unknown`; it never contributes a numeric zero.
Any source read or parse error also makes the full eligible denominator unknown;
successfully parsed rows remain visible only as a partial observation.
An exhaustive empty source in a successfully read bounded window may record
`eligible=0`, but its coverage percentage remains unavailable because the
denominator is empty. Crontab identity derived from journal boot/PID metadata
does not count as exact identity adoption.

The latest census per source drives the P0 identity and coverage denominators.
Census, validation, and collector events are operational evidence, not work
executions, and are excluded from outcome, freshness, and activity totals.
Usage Facts remains an orthogonal row population: `eligible` counts all source
rows, `metric_observed` counts adapted Usage events, and universal
`execution_id` deduplication is reported separately as `observed`. Multiple
Usage rows linked to one Kanban execution therefore cannot erase real metric
coverage.

## Cost contract

The read model keeps four dimensions separate:

1. measured metered cost;
2. marginal cost of one additional subscription run;
3. allocated monthly subscription fee;
4. public API-list-price equivalent.

For an eligible subscription run `r` in month `m`:

```text
allocated(r) =
  monthly_fee(m) × api_equivalent(r)
  / Σ api_equivalent(eligible subscription runs in m)
```

The event records the formula, month, population size, fee amount and version,
pricing version, currency, and allocation. The final run in each population
receives the decimal remainder, so allocations sum exactly to the configured
fee. Fees are explicit input and never loaded implicitly. USD fee and USD
pricing need no FX conversion; mixed currencies stay `unknown` without a
versioned FX source.
Pricing and allocation are reproducible transformations and therefore carry
`derived` validity even when their underlying token counts are exact. A sampled
Usage-Facts cohort may never allocate a monthly fee; fee allocation requires the
complete eligible month.

Control sample Kimi K3 run 8802:

```text
42,668 × 3.00 USD/M       = 0.1280040
118,784 × 0.30 USD/M     = 0.0356352
3,202 × 15.00 USD/M      = 0.0480300
API equivalent           = 0.2116692 USD
subscription marginal    = 0 USD
```

The allocated subscription share remains absent until an explicit monthly fee
and fee version are supplied.

## Read contracts

- CLI:
  `scripts/execution_facts.py schema|show|validate|collect-shadow`
- API: `GET /api/plugins/kanban/stats/execution-facts`
- schema: `execution-facts-read.v1`
- projection: `execution-facts-projection.v1`
- source activation gates: `execution-facts-shadow-gates.v1`

Every rate includes numerator, eligible denominator, validity, computed
status, and an unknown reason. Costs remain decimal strings. Read-only
commands open SQLite with `mode=ro`.
`observed` greater than `eligible` is a denominator conflict and renders
`unknown`; it is never clamped or displayed above 100%. Outcome coverage uses
ended executions as both population and observation domain. Cost coverage
uses all executions carrying any Usage-Facts dimension as its eligible
population, not only those with a known token total.

Shadow validation is a separate exact event from
`execution_facts_validation`; it names its target source without refreshing
that source's data freshness. A source cannot report `shadow_pass` without a
fresh proof of identity and metric coverage, capture p95 below 1 ms,
dedupe/reconciliation parity, zero source read errors, and a named read-only
behavior proof.

The source census is similarly separate under `execution_facts_census`.
Recording a denominator can neither refresh the target source nor manufacture
work. The collector reads source SQLite databases in one
`mode=ro`/`query_only` transactional snapshot, structured Loop JSONL, neutral
six-field tmux pane metadata, bounded systemd properties,
and bounded journal identity/timestamp fields for Crontab.
Systemd timestamps come directly from `ExecMainStartTimestamp` and
`ExecMainExitTimestamp` requested with `--timestamp=us+utc`; they are parsed
strictly and retain millisecond precision. Invocation identity plus lifecycle
phase forms the stable retry key, so observing the same invocation again
dedupes while a genuinely new `InvocationID` appends.
On the current Debian host that journal cohort is selected with
`SYSLOG_IDENTIFIER=CRON`; `_COMM=CRON` is not used because the live process
comm value is lowercase and would silently return an empty cohort.
