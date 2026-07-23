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
