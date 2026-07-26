# Langfuse dashboard provisioning

Status: **safety scaffold only**. The tRPC contract in
`scripts/langfuse_dashboards.py` is pinned to Langfuse 3.224.0 upstream revision
`d044f366816282235898a0673d5700e05ccbee8c`. It deliberately does not create a
live dashboard until the required Golden Fixture is exported from a manually
created UI dashboard.

## Authentication and primary path

The only evidenced provider is NextAuth `credentials`. Dashboard mutations need
a human-authenticated browser session with `dashboards:CUD` on the
`hermes-agent` project; app read-back needs `dashboards:read`. Do not put a
cookie, token, password, DSN, or session export in a CLI argument, file, task
comment, receipt, or log.

The injectable `TrpcClient` uses POST batch requests to these version-pinned
procedures:

- `dashboard.createDashboard`
- `dashboardWidgets.create`
- `dashboard.updateDashboardDefinition`
- `dashboard.getDashboard` for app-level read-back

It rejects HTTP, malformed batch, tRPC error, and unexpected result envelopes
before a later mutation is attempted. The test-only transport proves the exact
request route, POST method, and batch body without network traffic.

## Golden Fixture and configuration

Before adding any files under `config/langfuse-dashboards/`, an authorised human
must create one dashboard and widget in the Langfuse UI, then provide a
sanitised structural export. It must retain the exported JSONB shapes
`definition`, `dimensions`, `metrics`, and `chart_config`, redact IDs
consistently, and record Langfuse version/revision plus export date. Never
hand-author these shapes from route names.

Only after that export may the three derived configuration files be added:
`north-star.json`, `reviewer-diagnose.json`, and `effizienz.json`. The
model-mix widget must use `OBSERVATIONS` and `providedModelName`; its displayed
receipt must name its denominator.

## Direct SQL fallback, guards, and rollback

Direct SQL is not implemented or configured in this scaffold. It is physically
unreachable unless a caller explicitly invokes `run_direct_sql_fallback` with
`--allow-direct-sql`; the command-line runner supplies no adapter. This is
intentional: no sanctioned DSN provider is available and PostgreSQL must not
become an implicit primary path.

When the authorised implementation is added, its adapter must fail closed
before every write unless all of these guards pass:

1. `GET /api/public/health` reports an allow-listed Langfuse version.
2. `_prisma_migrations` matches the full expected set and newest migration.
3. `information_schema` fingerprints both target tables and rejects every new
   NOT NULL column lacking a default.
4. `pg_enum` labels match the expected dashboard enum sets.
5. Exactly one `projects` row for `hermes-agent` exists and `min_version`
   matches the export.
6. `pg_dump` has captured both dashboard tables for rollback.

The eventual fallback must use one transaction, `ON CONFLICT (id) DO UPDATE`,
rollback on unexpected row counts, and never change `projects.home_dashboard_id`.
Its receipt must state primary/fallback path, app read-back, observed data
sources, changes, and `changes: 0` for an idempotent second run.

## Safe dry-run and recovery

```bash
.venv/bin/python scripts/langfuse_dashboards.py --dry-run
```

Dry-run creates no transport, does not contact Langfuse or PostgreSQL, and
prints `{"changes": 0, "status": "requires_golden_fixture"}` until a
versioned export and approved session adapter are available. The exact CLI
flags presently supported are `--dry-run` and `--allow-direct-sql`; the latter
is reserved and does not enable a database connection.

If a fixture or app response becomes invalid, stop without a follow-up write,
retain the existing dashboard state, and re-export/review the fixture from the
UI. Recovery from an eventual SQL fallback is the captured `pg_dump`, after
first determining why its guard failed.
