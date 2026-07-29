# Langfuse dashboard provisioning

Status: **one fixture-backed native Worker Control Center**. The provisioning
contract in `scripts/langfuse_dashboards.py` is pinned to Langfuse `3.224.0`,
upstream revision `d044f366816282235898a0673d5700e05ccbee8c`.

## Authentication and primary path

The only evidenced provider is NextAuth `credentials`. A live run requires an
approved, already human-authenticated browser-session adapter for a user with
`dashboards:CUD` on project `hermes-agent`; app read-back needs
`dashboards:read`. Do not place cookies, tokens, passwords, DSNs, or session
exports in CLI arguments, repository files, task comments, receipts, or logs.

`TrpcClient` sends POST batch mutations, in this fixed order, and fails closed
on an HTTP error, malformed batch, tRPC error, or unexpected response:

1. `dashboard.createDashboard`
2. `dashboardWidgets.create` (once per configured widget)
3. `dashboard.updateDashboardDefinition`
4. `dashboard.getDashboard` as the required **app-level read-back**

There is no automatic fallback after a tRPC failure. The injectable transport is
intentional: the CLI does not create a session transport and cannot mutate a
live dashboard by accident.

## Golden fixture and Greenfield configuration

`tests/scripts/fixtures/langfuse-dashboard-golden.json` is a versioned,
sanitary export of the normal widget manually created in the Langfuse UI. It
contains the UI-observed `definition`, `view`, `dimensions`, `metrics`,
`filters`, `chart_type`, and `chart_config` structures; every ID is
consistently `<REDACTED_ID>`.

`tests/scripts/fixtures/langfuse-dashboard-contract-3.224.0.json` is the
checked-in extraction of the supported views, chart types, chart-config fields
and filter operators. Its provenance names the exact upstream files and
revision. The validator loads both fixtures on every configuration load; a
hand-edited constant or unsupported config field therefore fails before any
transport is created.

There is exactly one version-matched configuration:

- `config/langfuse-dashboards/control-center.json`

It defines `Hermes Worker Control Center` with 15 native widgets on one
non-overlapping 12-column grid. `CONFIGURATION_PATHS` intentionally rejects a
second dashboard. The mobile order follows the JSON order and starts with the
four decision KPIs, throughput and ranked outcomes.

Every widget carries an explicit `denominator`, so sparse coverage cannot be
silently presented as a population-wide rate. The first row contains absolute
completed, blocked and approved counts plus p95 run duration. A categorical
approval percentage is not invented because the pinned Langfuse metric
contract cannot calculate that ratio in one widget.

The analysis rows deliberately separate:

- task outcome counts from trace throughput;
- model-call volume from known model cost;
- observation latency/TTFT from complete task-run duration;
- review verdict counts from the thin review-iteration sample;
- cost values with known prices from missing-price observations.

Rankable categories use `HORIZONTAL_BAR`; thin
`review_iterations_to_approval` data uses a `NUMBER` plus `HISTOGRAM` instead
of a smoothed daily line. Histogram widgets are accepted only with
`histogram(value)` and `1..100` pinned bins. `PIE` remains supported by the
source contract but is not used in the control center.

Exact correlation and price/score coverage remain read-only audit gates until
an exact native score exists. They must not be approximated inside Langfuse
with session names, trace names, missing-value zeros, or ratios across
different views.

The primary path is invoked only by code that supplies both a real project ID
and the approved session adapter, for example:

```python
plans = load_dashboard_configs(project_id=project_id)
client = TrpcClient("http://127.0.0.1:13000", opener=approved_session_opener)
read_backs = [provision_dashboard(client, plan) for plan in plans]
```

`approved_session_opener` is deliberately not a CLI option or a persisted
setting. Treat IDs returned by this flow as transient runtime values only.

## Direct SQL fallback, guards, and rollback

Direct SQL remains an explicit, dependency-injected fallback, not a production
transport. `run_direct_sql_fallback` needs both `--allow-direct-sql` and an
authorised `DirectSqlAdapter` plus a pinned `SqlGuardContract`; no DSN, driver,
credential, or adapter implementation is shipped. A tRPC failure never calls
the SQL function automatically: it requires a separate, explicit caller run.

An authorised future adapter may execute a write only after it fails closed in
this exact order:

1. `GET /api/public/health` reports an allow-listed Langfuse version.
2. `_prisma_migrations` matches the complete expected set and newest migration.
3. `information_schema` fingerprints `dashboards` and `dashboard_widgets` and
   rejects each new NOT NULL column without a default.
4. `pg_enum` labels match the expected dashboard enum sets.
5. Exactly one `projects` row for `hermes-agent` exists and widget
   `min_version` matches the fixture contract.
6. `pg_dump` captures both dashboard tables as a rollback point.

The adapter must then use one transaction, `ON CONFLICT (id) DO UPDATE`, and
rollback on any unexpected row count or schema drift. It must never update
`projects.home_dashboard_id`. Restore only from the pre-write `pg_dump` after
examining the guard failure; do not retry through a different path.

## Receipt schema

A live caller must persist a machine-readable, secret-free receipt with:

```json
{
  "receipt_version": 2,
  "path": "trpc",
  "understood_definition": [{"dashboard": "Hermes Worker Control Center", "widget_placements": 15}],
  "visible_export_evidence": [
    {"widget": "Laufresultate", "source": "exported_score"},
    {"widget": "Modellaufrufe", "source": "observations"}
  ],
  "model_mix": {
    "source": "OBSERVATIONS",
    "dimension": "providedModelName",
    "denominator": "native kanban-worker or backfilled kanban-worker-usage observations with non-empty providedModelName"
  },
  "changes": 16
}
```

The implemented receipt has `receipt_version`, `path`,
`understood_definition`, `visible_export_evidence`, `model_mix`, and `changes`.
It is built only after app read-back (`dashboard.getDashboard` for tRPC or the
adapter's app-read-back method for SQL). It contains no dashboard, widget,
project, user, session, or secret IDs. `changes: 0` is valid for a second,
idempotent SQL upsert when every write result reports an unchanged row; a
changed denominator must be shown identically in its widget and receipt.

## Safe validation and recovery

```bash
.venv/bin/python scripts/langfuse_dashboards.py --dry-run
```

Dry-run validates the Golden Fixture, source contract and the one
control-center configuration, creates no transport, does not contact Langfuse
or PostgreSQL, and prints
`{"changes": 0, "dashboard_count": 1, "status": "control_center_ready", "widget_count": 15}`. The
CLI flags are `--dry-run` and `--allow-direct-sql`; the latter is only useful
to an explicit programmatic caller that also injects a fully guarded adapter
and contract.

If the fixture, configuration, or app read-back is invalid, stop immediately:
no subsequent mutation and no SQL fallback is permitted. Preserve the existing
dashboard state, re-export the UI-derived fixture if the release changed, and
rerun the fixture/config tests before any authorised retry.
