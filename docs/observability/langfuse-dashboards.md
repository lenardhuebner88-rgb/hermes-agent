# Langfuse dashboard provisioning

Status: **fixture-backed primary-path implementation**. The provisioning contract
in `scripts/langfuse_dashboards.py` is pinned to Langfuse `3.224.0`, upstream
revision `d044f366816282235898a0673d5700e05ccbee8c`.

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

## Golden Fixture and derived configuration

`tests/scripts/fixtures/langfuse-dashboard-golden.json` is a versioned,
sanitary export of the normal widget manually created in the Langfuse UI. It
contains the UI-observed `definition`, `dimensions`, `metrics`, and
`chart_config` structures; every ID is consistently `<REDACTED_ID>`. The
metadata pins the Langfuse release and source revision.

The three version-matched configuration files are:

- `config/langfuse-dashboards/north-star.json`
- `config/langfuse-dashboards/reviewer-diagnose.json`
- `config/langfuse-dashboards/effizienz.json`

Each widget carries an explicit `denominator`, so sparse coverage cannot be
silently presented as a population-wide rate. The model-mix widget uses the
Langfuse API’s normalized `observations` view (the underlying enum is
`OBSERVATIONS`) and `providedModelName`; its denominator is
`OBSERVATIONS with non-empty providedModelName`, not the under-populated tasks
page model field.

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
  "receipt_version": 1,
  "path": "trpc",
  "understood_definition": [{"dashboard": "Hermes North Star", "widget_placements": 3}],
  "visible_export_evidence": [
    {"widget": "Euro equivalent per done task", "source": "exported_score"},
    {"widget": "Model mix", "source": "observations"}
  ],
  "model_mix": {
    "source": "OBSERVATIONS",
    "dimension": "providedModelName",
    "denominator": "OBSERVATIONS with non-empty providedModelName"
  },
  "changes": 13
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

Dry-run validates the Golden Fixture and all three configurations, creates no
transport, does not contact Langfuse or PostgreSQL, and prints
`{"changes": 0, "dashboard_count": 3, "status": "fixture_ready"}`. The
CLI flags are `--dry-run` and `--allow-direct-sql`; the latter is only useful
to an explicit programmatic caller that also injects a fully guarded adapter
and contract.

If the fixture, configuration, or app read-back is invalid, stop immediately:
no subsequent mutation and no SQL fallback is permitted. Preserve the existing
dashboard state, re-export the UI-derived fixture if the release changed, and
rerun the fixture/config tests before any authorised retry.
