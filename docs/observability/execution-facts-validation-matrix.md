# Execution Facts validation matrix

> Which store is authoritative per metric family (consumption / lifecycle /
> execution identity) is binding via
> `vault/00-Canon/decisions/2026-07-31-metrik-ssot-register.md`.

Status reflects the current worktree plus the read-only real-source Shadow run
from 2026-07-31. It is evidence for operating the Shadow collector, not for
promoting telemetry into product behavior.

| Requirement | Evidence | Status |
|---|---|---|
| Append-only ledger and idempotency | `test_execution_facts_ledger.py` | Proven in temporary DB |
| Rebuild-only projections | two rebuilds, identical digest in projection and real E2E tests | Proven |
| 90-day raw/permanent monthly retention | additive two-pass retention test | Proven |
| Content-free V1 | closed attribute allowlist plus Cron/Loop/tmux and E2E canaries | Proven |
| Unknown is not zero | contract, missing DB, missing usage/price tests | Proven |
| Post-commit fail-open boundary | source-DB outcome identical for collector off/down/slow/full | Proven in isolated regression |
| Hot-path p95 below 1 ms | 10,000 `try_emit` calls against a full slow collector | Proven: current standalone p95 0.003350 ms; final gate command recorded in receipt |
| Kanban lifecycle | exact runtime milestones, terminal result, failure and retry | Proven in isolated DB |
| Hermes Cron | success, error redaction and explicit retry link | Proven in isolated DB |
| Loops | real successful process plus structured usage/result adapter | Proven |
| tmux | dedicated socket/server, all-pane parser, lower-bound identity, death, close | Proven; six live panes observed through neutral fields in four timestamped scans (24 real observations), three missing durable IDs stamped once with operator approval |
| systemd/Crontab | real successful local processes plus content-free observations | Proven in isolation and against bounded live properties/journal metadata |
| Universal eligible denominator | six explicit source censuses; missing denominator remains `null`/`unknown`, exact empty windows do not become successful zero coverage | Proven with 20-source-record E2E and empty-source negative case |
| Read-only Shadow collector | fixed Kanban → Cron → Loops → tmux → systemd → Crontab order; source hashes stable; neutral command allowlists | Proven with 20 records per source in isolated E2E |
| Collector dedupe/reconciliation | immediate replay inserts zero duplicate source events; two projection rebuilds match | Proven with isolated E2E |
| Collector writer p95 | per-event non-blocking `try_emit` p95 strictly below 1 ms | Proven with isolated E2E and live-source p95 of 2–10 µs; the 20-attempt writer probe is operational and never inflates source event counts |
| Terminal/task identity | durable store, one-current-locator invariant, duplicate-marker handoff, respawn generation and multiple bindings | Proven |
| Agent/sub-agent/tool/test chain | explicit parents, terminal/task links and result spans | Proven in real E2E |
| Test timeout truth | selected/executed/timeout-lost counters cannot form false green; incomplete counter populations stay unknown with an observed lower bound | Proven |
| Retry denominator | only per-run runtime-proven `retry_instrumented=true` executions are eligible | Proven; table existence alone cannot mark historical runs exact |
| Per-source freshness | stale source is not masked by a fresh source | Proven |
| Cost population independent of UI limit | Fleet readmodel full-population tests | Proven |
| K3 run 8802 control | 42,668 input; 118,784 cache; 3,202 output; 0.2116692 USD | Proven against current pricing snapshot |
| Subscription allocation | proportional formula, exact fee sum, population and versions | Proven in adapter and E2E |
| Read-only CLI/API | CLI mtime invariant, `mode=ro`, API route contract test | Proven |
| Shadow gates | positive/negative proof for per-source sample, freshness, identity/metric coverage, capture p95, validity, drop/lag, dedupe/reconciliation and unchanged behavior | Proven; intentionally no activation |

Current focused command covers the foundation, all source adapters, Usage
Facts, the API route, and the Sentinel:

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_execution_facts_*.py \
  tests/hermes_cli/test_fleet_metrics_readmodel.py \
  tests/hermes_cli/test_telemetry_contracts.py \
  tests/hermes_cli/test_claude_code_harvester.py \
  tests/hermes_cli/test_usage_facts_readmodel.py \
  tests/plugins/test_kanban_observability_route.py \
  tests/scripts/test_observability_sentinel_smoke.py \
  -q -p no:cacheprovider
```

Result: 197 passed, 0 failed.

Final integration evidence:

```text
scripts/run-affected.sh
  45 test files · 644 passed · 0 failed/errors

scripts/collect_check.sh -q tests/
  56925/56988 collected · 63 deselected · 0 collection errors

ruff check (all changed Python files)
  All checks passed
```

The worktree was fast-forwarded without cleaning to current `main`
`2b7ded5deadde8be018669e693cd934c6f870348`; all handed-off dirty files were
preserved.

## Real-source Shadow census

Evidence directory:
`/tmp/hermes-execution-facts-final8.apqTcC/evidence/`.
The four UTC-prefixed receipts are
`20260731T012525-eb4fe35f.json`,
`20260731T012717-1b5994aa.json`,
`20260731T012747-5ee4aa9c.json`, and
`20260731T012816-64280a15.json`.

| Source | Eligible | Metric observed | Exact source IDs | Events in Shadow ledger | Writer p95 |
|---|---:|---:|---:|---:|---:|
| Kanban timeline | 8,834 | 8,834 | 8,834 | 27,168 | 10 µs |
| Hermes Cron | 1,867 | 1,867 | 1,867 | 5,601 | 4 µs |
| Loop ledger | 152 | 152 | 0 | 428 | 2 µs |
| tmux reconciliation | 6 | 6 | 6 | 24 across four real scans | 4 µs |
| systemd invocation | 63 | 63 | 63 | 128 cumulative source events | 4 µs |
| Crontab invocation | 14,414 | 14,414 | 0 | 14,414 | 3 µs |
| Usage Facts (orthogonal) | 109,269 | 200 rows in latest bounded sample | 109,269 | 406 | 3 µs |

Every source snapshot remained read-only, immediate replay deduped, two rebuilds
matched on each scan, collector drops were zero, and `activation_effect`
remained `none`. The universal six-source identity denominator is 25,336 with
10,770 exact source identities (42.51%). Outcome coverage is
10,748/10,748. Cost coverage is 313/323, but remains `unknown` because pricing
and subscription-fee allocation are incomplete. Error instrumentation measures
8,040 of 10,748 ended executions; the visible 1,494 errors therefore remain an
`unknown` rate rather than treating unmeasured runs as error-free. Data Trust is
48,229/48,229 exact on this cohort while still excluding `unknown` and
`not_applicable` facts from its numerator. No missing amount or metric is
rendered as zero.
The latest database validates with 48,229 events, zero invalid events, a current
projection, and digest
`742f8acb1b7c0126ddcfcbee3ef2ff1373f5ac98285229650072786fa6090b22`.
Replaying the systemd cohort reports zero duplicate invocation/phase groups
across 128 source events and 80 distinct retained `InvocationID` values;
`PRAGMA integrity_check` is `ok`.

## Independent review

The original foundation packet already carried the handoff's Grok/Kimi
reviews. Claude Opus 5 was requested for this integration but returned no
verdict because its shared quota was exhausted. Two independent GPT-5.6 agents
therefore review the exact final patch: one for execution-fact semantics and
one for read-only/live safety. Their final verdicts and patch ID are recorded
in the integration receipt.

## Usage-ledger recovery during integration

A diagnostic mistake dropped the live `run_usage_facts` aggregate table while
leaving both raw tables intact. The damaged database was preserved at
`/mnt/data/hermes-observability/usage_facts.db.incident-20260731T0120Z.after-drop.bak`.
Recovery rebuilt off-live from the latest pre-incident aggregate snapshot,
current `run_llm_calls`, `run_traces`, and a fresh Claude harvest, then replaced
only the verified live DB atomically. Post-recovery evidence:

```text
run_usage_facts  109,238 at restoration; subsequent writer append observed
run_llm_calls    117,372
run_traces       138,333
PRAGMA integrity_check = ok
usage/readmodel/harvester targeted tests = 70 passed
```

The current Shadow census read the repaired DB successfully. Recovery
artifacts under `/tmp/usage-facts-recovery.AH75lH/` and the incident backup are
retained; no destructive cleanup is part of this integration.

## Real E2E chain

`test_real_isolated_tmux_to_landed_result_end_to_end` creates a unique tmux
server and temporary execution database, then proves:

```text
terminal_run_id
  → agent span
    → sub-agent span
      → real successful tool process
    → real successful test process and gate
  → task_run_id binding
  → exact K3 tokens and four cost dimensions
  → reviewed
  → landed_sha
```

The same test adds failed and retried Kanban/Cron executions, successful Loop,
systemd and Crontab observations, collector health, content-canary absence,
and identical replay digest. Its cleanup targets only the unique test tmux
socket.

## Remaining measurement gaps

- No production post-commit callpoint is enabled. New-run emit parity has not
  accumulated a live sample.
- Existing synchronous `first_request`/`first_token` Runtime Facts remain the
  comparison bridge. This diff does not remove or alter them.
- Six live tmux panes now carry durable run IDs. Three existing IDs were
  preserved and three previously empty pane-local options were stamped once
  with operator approval; prior values were empty and rollback is an option
  unset. The collector itself never stamps or reads pane content.
- Live Hermes Cron has no retry-link columns today. Cron retry lineage remains
  unknown there; the adapter accepts an explicit future extension and never
  infers retries from timing.
- Historical Loop ledgers lack a universal Loop run ID; their date/round IDs
  remain `derived`.
- The content-free systemd/Crontab adapters have real sample depth. Installing
  or enabling the periodic user timer remains a separately verified live step.
- Agent, sub-agent, tool, test, and operator adapters exist, but no production
  callpoint is enabled.
- Landing and deployment remain unknown without explicit milestone/SHA
  evidence. `done` is not promoted.
- Monthly subscription fees require explicit operator-owned versioned input.
  Missing fees or price/FX sources remain unknown.
- Every source has at least 20 real events and parity evidence. tmux reaches 24
  only through four timestamped six-pane scans; the separate 20-attempt writer
  probe does not count toward that source sample. Promotion stays
  blocked where exact source identity is absent (historical Loops and Crontab)
  or where a historical backfill exceeds the steady-state lag budget. Passing
  any later data gate still has `activation_effect: none`.
- The API is backend-only. No UI change belongs to this goal.
