# Langfuse observability plugin

Langfuse is an optional, explicitly enabled trace sidecar. It is not a source
of truth for Hermes task lifecycle, quota, usage, billing, or outcomes.

## Activation and failure behaviour

The plugin is inert unless it is explicitly enabled through the Hermes plugin
manager **and** both `HERMES_LANGFUSE_PUBLIC_KEY` and
`HERMES_LANGFUSE_SECRET_KEY` are configured. No client is constructed when
credentials are absent; therefore the default state has no Langfuse network
egress. Client construction, hook emission, and export failures are caught and
never change the Hermes run result.

Set `HERMES_LANGFUSE_ENABLED=false` for a process-level emergency off-switch:
it prevents client initialization and egress even when the plugin is registered
and credentials are present. An absent value preserves the plugin-manager
activation contract.

`HERMES_LANGFUSE_TIMEOUT_SECONDS` bounds SDK requests. It defaults to `5`, and
is clamped to `[0.1, 30]` seconds. Invalid values safely fall back to five
seconds.

## Data contract

Before this change, prompt/message content, assistant output, tool arguments,
and tool results were recursively serialized with key-based redaction. That
cannot safely classify arbitrary raw text.

Now this plugin is metadata-only and fail-closed:

- raw prompts, assistant output/reasoning, tool arguments, tool output, image
  payloads, and unclassified structures are emitted only as
  `{omitted: true, reason: raw_content_not_exported, type, length}`;
- allowed structural metadata remains limited to message roles, tool names and
  call IDs, trace/session scope, provider/model, API mode, and token/cost
  summaries already supplied by the hook;
- no secret, credential, token, or sensitive raw content is exported. Hermes
  remains authoritative for `task_id`, `task_run_id`, chain/lane/outcome and
  provider-window usage. This adapter neither creates joins nor migrations.

Rollback: disable `observability/langfuse` (or remove its two credentials), then
revert this commit. No service restart is required for the default-disabled
path.

## Isolated self-host smoke

The smoke uses the official Langfuse `v3.224.0` release, pinned to source commit
`d044f366816282235898a0673d5700e05ccbee8c`, without forking or vendoring its
Compose file. Download the exact pinned source to a temporary directory, then
apply `self-host-smoke.compose.yaml`:

```bash
work=$(mktemp -d)
curl --fail --location \
  https://raw.githubusercontent.com/langfuse/langfuse/d044f366816282235898a0673d5700e05ccbee8c/docker-compose.yml \
  -o "$work/langfuse-compose.yaml"
docker compose -f "$work/langfuse-compose.yaml" \
  -f plugins/observability/langfuse/self-host-smoke.compose.yaml config
```

For the reproducible, non-starting smoke/dry-run use the repo-local wrapper:

```bash
scripts/langfuse_self_host_smoke.sh
```

It fetches only the pinned upstream Compose source into a temporary directory,
validates the loopback overlay, and runs the focused synthetic LLM/tool privacy
and plugin-failure tests. Its terminal output includes
`OPERATOR_LIVE_SMOKE_PENDING`; that is deliberate. A live smoke must be run by
an operator with a disposable local project, loopback-only binding, synthetic
credentials and synthetic prompt/tool data. Do not use production credentials
or prompts, and do not publish the bound port.

The overlay pins both Langfuse images and overrides the web port to
`127.0.0.1:13000`; no public ingress is permitted. For a live operator smoke,
use a disposable local Docker project and synthetic credentials/data only,
create a temporary Langfuse project in the loopback UI, then enable the plugin
for one synthetic LLM call and one synthetic tool call. Confirm the stored
trace contains the correlation values supplied by the hook, contains no
sentinel secret in serialized payloads, and that disabling/rejecting the plugin
leaves the Hermes result unchanged.

`OPERATOR_LIVE_SMOKE_PENDING`: this code task does not start Docker or a
Langfuse service. The deterministic `docker compose ... config` command above
is the local configuration gate; live startup is a chain-end operator action.

## Operator live-smoke acceptance (fail closed)

Contract tests prepare this sequence but do not run it. An operator must approve
the real Kanban run and all live mutations. Do not start containers, expose an
ingress, or print keys, cookies, headers, prompts, or transcripts while collecting
the receipt.

1. Record the pre-change read-only audit and create a transaction-consistent
   backup before any schema initialization or correlation backfill:

   ```bash
   .venv/bin/python scripts/langfuse_worker_audit.py --days 90 > audit-before.json
   .venv/bin/python - "$HERMES_USAGE_FACTS_DB" "$HERMES_USAGE_FACTS_DB.pre-smoke.bak" <<'PY'
   import sqlite3, sys
   source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
   destination = sqlite3.connect(sys.argv[2])
   source.backup(destination)
   destination.close()
   source.close()
   PY
   ```

2. Initialize only missing schema objects, preview the exact-session backfill,
   and inspect both JSON results. Initialization is idempotent; on an already
   current DB it performs no rewrite. Apply only after operator approval:

   ```bash
   .venv/bin/python -c 'from hermes_cli.usage_facts_db import initialize_usage_facts_db; import sys; print(initialize_usage_facts_db(sys.argv[1]))' "$HERMES_USAGE_FACTS_DB"
   .venv/bin/python -m hermes_cli.claude_code_harvester --db "$HERMES_USAGE_FACTS_DB" --backfill-correlations > backfill-preview.json
   .venv/bin/python -m hermes_cli.claude_code_harvester --db "$HERMES_USAGE_FACTS_DB" --backfill-correlations --apply > backfill-applied.json
   .venv/bin/python scripts/langfuse_worker_audit.py --days 90 > audit-after.json
   ```

3. After an approved real Kanban worker run has completed, confirm that its
   preserved `task_runs.metadata.worker_runtime` is exactly `hermes`. Claim-time
   spawn identity is retained when terminal completion metadata is stored. The
   contract is not applicable to `claude-cli` workers: those runs do not pass
   through the Hermes conversation loop and therefore emit neither the plugin
   trace nor its LLM lifecycle observations. Then run the acceptance gate with
   the numeric `task_run_id` of the eligible Hermes-runtime run:

   ```bash
   .venv/bin/python scripts/langfuse_worker_audit.py --live-smoke-run-id "$TASK_RUN_ID" > live-smoke.json
   ```

4. After the separate operational approval and after the configured Langfuse
   listener has been repaired, confirm that the already-running dashboard is
   listening on its canonical loopback port `9119`, then run the control-surface
   smoke. It deliberately accepts loopback dashboard URLs only. Authentication
   reuses the password-login and in-memory cookie flow from
   `scripts/smoke_health_status_auth.py`; it does not require an operator to
   extract or paste the ephemeral dashboard token. Supply the password only
   through the environment; never put it in an argument or redirect an HTTP
   trace containing cookies or headers:

   ```bash
   HERMES_DASHBOARD_PASSWORD="$DASHBOARD_PASSWORD" \
     .venv/bin/python scripts/langfuse_worker_audit.py \
       --control-surface-smoke-url http://127.0.0.1:9119 \
       --dashboard-auth-provider basic \
       --dashboard-username "$DASHBOARD_USERNAME" \
       --warm-calls 10 \
       --no-prompt \
       --days 7 > control-surface-smoke.json
   ```

   This performs a configured-host check, an authenticated Langfuse Public API
   page, a complete pagination scan, an intentionally one-page-limited scan,
   one dashboard warm-up, and ten measured authenticated dashboard reads. The
   CLI enforces the PlanSpec minimum of five reads. A
   complete scan can report `fresh` only after an explicit total-page boundary
   or a short final page. The limited scan always reports `partial`, count
   lower bounds, and unknown total-window coverage. The complete scan is bounded
   by a 30-second deadline and 100,000-row ceiling; reaching either fails closed
   as `partial`. The receipt records wall and client-process-CPU median, maximum,
   and mean, every measured Usage cache age, and the Usage fact count. HTTP
   cannot observe server CPU, so the receipt marks that budget
   `not_observable_over_http`; the LRM-4 in-process test remains its proof and
   only the 300 ms wall budget participates in this smoke's HTTP budget result.
   Overall pass additionally requires fresh Usage with a known fact count. A refused Langfuse
   connection remains red while the dashboard receipt may still show Usage as
   available. The command does not start or restart Langfuse or the dashboard.

The command exits `0` only when the authenticated public trace API is readable,
an explicit trace metadata field and a usage fact carry that exact run ID, the
usage fact agrees on task ID, all required lifecycle observations exist, and the
worker terminated successfully. It exits `3` for a red contract. Once a model
request was observed, `first_token` is required; a run that failed before its
first model request reports the absent observations but does not invent or
require a first token. The receipt includes `worker_runtime` and marks an
ineligible runtime's lifecycle assessment as `not_applicable`. Endpoint errors
separate missing credentials, HTTP responses (including invalid JSON), and
network failures without copying exception text or response bodies; trace IDs
are truncated in the JSON receipt. `schema` reports the versioned
worker-runtime-facts and exact-usage-correlation contracts rather than SQLite's
internal DDL counter.

Archive `audit-before.json`, `backfill-preview.json`, `backfill-applied.json`,
`audit-after.json`, `live-smoke.json`, and `control-surface-smoke.json` together
with the exact commands and their exit codes. A green contract test is only
evidence that this acceptance logic works; it is never a substitute for a green
operator live-smoke receipt.
