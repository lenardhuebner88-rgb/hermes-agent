# Hermes Agent — Architecture Orientation (for coding agents)

> **Purpose.** One map that ties together the **backend**, the **/control dashboard**, the
> **Kanban dispatch lifecycle**, and the concrete **functions/endpoints** — so an agent can
> understand how the pieces connect *without* a broad codebase search. Read this first,
> then jump to the cited `file:line` anchors.
>
> **What is hand-maintained vs generated.** The prose sections (Backend, Dashboard,
> Kanban lifecycle, Cross-references) are written by hand and kept short + pointer-based.
> The four **GENERATED** sections (dashboard tabs, HTTP endpoints, DB schema, config keys)
> are derived from the live code by `scripts/gen_architecture_doc.py` and must not be
> edited by hand — run the script to refresh them. A drift check (`--check`) runs in the
> local dashboard gate.
>
> **Deeper docs.** `AGENTS.md` (full dev architecture, contribution rubric), `web/README.md`
> (frontend stack), and the cross-agent Vault canon at `/home/piet/vault/00-Canon/`
> (`infra-topology`, `agent-roster`, `conventions-gates`, `projects-map`).

---

## 1. Backend architecture

Hermes is a single Python package (`hermes_cli/`) plus a few satellite packages
(`gateway/`, `agent/`, `plugins/`). Everything that touches Kanban goes through **one**
shared data layer — `hermes_cli/kanban_db.py` (SQLite, WAL) — so the CLI, gateway,
web API, and dispatcher cannot drift from each other.

- **HTTP server** — `hermes_cli/web_server.py`. The FastAPI `app` is defined at
  `web_server.py:174` (`app = FastAPI(...)`). It serves the `/control` SPA and the `/api/*`
  surface on **port 9119**. `_lifespan` startup/shutdown only runs under uvicorn (so
  importing the module is side-effect-free — that is what lets the doc generator introspect
  routes). Auth is a per-process session token (loopback or `window.__HERMES_SESSION_TOKEN__`).
- **Dashboard plugin routes** — `plugins/kanban/dashboard/plugin_api.py` defines an
  `APIRouter` mounted under `/api/plugins/kanban/` at import time by
  `_mount_plugin_api_routes()` in `web_server.py`. These are the endpoints the dashboard polls
  (board, tasks, runs, workers, decision-queue, events WebSocket, …).
- **Gateway** — `gateway/run.py` (`GatewayRunner`) is the long-lived multi-platform chat
  process. It hosts the background **Kanban dispatcher** and **notifier** watchers
  (see §4) and the `/kanban` slash commands (`gateway/slash_commands.py`).
- **Agent loop** — `agent/conversation_loop.py` runs the LLM think→tool→observe loop;
  `agent/tool_executor.py` executes tool calls; `agent/anthropic_adapter.py` is the Claude
  API integration. A spawned Kanban worker is just an agent loop launched with
  `--kanban-task <id>`.

## 2. Dashboard architecture (`/control` SPA)

React 19 + TypeScript + Tailwind 4, built with Vite. Source under `web/src/control/`;
`npm run build` (or `.bin/`-pinned tooling, see the gate note in §6) emits to
`hermes_cli/web_dist/`, which `web_server.py` serves.

- **Shell & tabs** — `web/src/control/components/ControlShell.tsx` declares the `ControlTab`
  union and the `tabs` array (id / label / mobileLabel / route / icon). Inbox (`/control`,
  "Postfach") is the landing decision-spine. The full live list is in the GENERATED §5.
- **Routing** — `web/src/control/ControlPage.tsx` maps URL → active tab (`activeFromPath()`)
  and renders the per-tab `<View>` components from `web/src/control/views/`. Keyboard nav:
  `Cmd/Ctrl+K` command palette, `g <letter>` quick-jump.
- **Data fetching** — hooks in `web/src/control/hooks/useControlData.ts`
  (`useBoard`, `useDecisionInbox`, `useHermesWorkers`, `useProposals`, …) sit on top of
  `web/src/control/hooks/pollingStore.ts` — a framework-agnostic polling store that dedups
  N subscribers → 1 timer/request per key, backs off on 5xx, and serves stale-while-error.
  Low-level fetch + session-token injection: `web/src/lib/api.ts`.
- **To add a tab:** new `views/XView.tsx` → import + `<Route>` in `ControlPage.tsx` → add
  to the `ControlTab` union and `tabs` array in `ControlShell.tsx` → label in `i18n/de.ts`
  (`de.tabs.*`) → optional data hook in `useControlData.ts`. Then rebuild.

## 3. Decision Inbox

`InboxView` (`web/src/control/views/InboxView.tsx`) is the landing surface and the single
count source via `useDecisionInbox()`. It aggregates pending items from several surfaces
(Kanban decision-queue, autoresearch proposals, Family Organizer backlog, orchestrator)
rather than each view rebuilding its own inbox.

## 4. Kanban dispatch lifecycle

The dispatcher is a background loop in the gateway, not a separate daemon (config
`kanban.dispatch_in_gateway: true`). Trace, in order:

1. **Watcher** — `gateway/kanban_watchers.py` (`_kanban_dispatcher_watcher`) ticks every
   `dispatch_interval_seconds` and calls `dispatch_once(conn)` via `asyncio.to_thread` so the
   SQLite WAL lock never blocks the event loop.
2. **dispatch_once** — `hermes_cli/kanban_db.py` (`dispatch_once`): releases stale claims,
   detects crashed/stale workers, enforces max-runtime, then `recompute_ready()`
   (`kanban_db.py`, promotes `todo→ready` once dependencies are `done`), then for each ready
   task with an assignee performs a CAS **claim** (atomic write to `claim_lock`) and calls
   the spawn function — respecting `max_spawn` / `max_in_progress[_per_profile]` and the
   `failure_limit` circuit breaker (`consecutive_failures` column).
3. **Spawn** — `_default_spawn` launches a subprocess `hermes chat --kanban-task <id>
   -p <assignee> --skills kanban-worker`, injecting `HERMES_KANBAN_TASK_ID` /
   `HERMES_KANBAN_DB` / `HERMES_KANBAN_BOARD` and recording `worker_pid` for crash detection.
4. **Worker exit** — the worker calls `/kanban complete <result>` (→ `status=done`,
   `task_runs.outcome=completed`) or `/kanban block <reason>` (→ `status=blocked`); a
   timeout/crash is reconciled on the next tick.
5. **Notify** — `_kanban_notifier_watcher` tails the append-only `task_events` table and
   pushes `completed`/`blocked`/`crashed` events to subscribed chat platforms.

The `Task` dataclass and status lifecycle live in `hermes_cli/kanban_db.py` (`class Task`).
Status flow: `triage → todo → ready → running → {done | blocked | review}`.

---

## 5. Dashboard tabs (GENERATED)

<!-- BEGIN GENERATED:dashboard-tabs -->
_10 tabs. Source of truth: `web/src/control/components/ControlShell.tsx`._

| id | Label (de) | Route | Icon |
|---|---|---|---|
| `inbox` | Postfach | `/control` | `Inbox` |
| `overview` | Übersicht | `/control/overview` | `LayoutDashboard` |
| `pulse` | Puls | `/control/pulse` | `Activity` |
| `workstreams` | Ströme | `/control/workstreams` | `GitBranch` |
| `hermes` | Hermes | `/control/hermes` | `Bot` |
| `flow` | Flow | `/control/flow` | `Columns3` |
| `autoresearch` | Autoresearch | `/control/autoresearch` | `FlaskConical` |
| `backlog` | Family Organizer | `/control/backlog` | `KanbanSquare` |
| `orchestrator` | Orchestrator | `/control/orchestrator` | `Workflow` |
| `crons` | Crons | `/control/crons` | `Clock` |

<!-- generated by scripts/gen_architecture_doc.py — do not edit by hand -->
<!-- END GENERATED:dashboard-tabs -->

## 6. HTTP API endpoints (GENERATED)

> The dashboard polls the `/api/plugins/kanban/*` group; other groups back sessions,
> messaging, config, autoresearch, crons, etc. Grouped by path prefix.

<!-- BEGIN GENERATED:api-endpoints -->
_250 HTTP endpoints across 41 groups._

#### `/api/actions`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/actions/{name}/status` | `get_action_status` |

#### `/api/analytics`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/analytics/models` | `get_models_analytics` |
| GET | `/api/analytics/usage` | `get_usage_analytics` |

#### `/api/audio`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/audio/elevenlabs/voices` | `get_elevenlabs_voices` |
| POST | `/api/audio/speak` | `speak_text` |
| POST | `/api/audio/transcribe` | `transcribe_audio_upload` |

#### `/api/auth`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/auth/me` | `auth_me` |
| GET | `/api/auth/providers` | `auth_providers` |
| POST | `/api/auth/ws-ticket` | `auth_ws_ticket` |

#### `/api/autoresearch`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/autoresearch` | `autoresearch_view` |
| GET | `/api/autoresearch/` | `autoresearch_view` |
| POST | `/api/autoresearch/apply` | `autoresearch_apply_proposal` |
| GET | `/api/autoresearch/audit` | `autoresearch_audit` |
| POST | `/api/autoresearch/confirm-batch` | `autoresearch_confirm_batch` |
| GET | `/api/autoresearch/deep-audit/findings` | `autoresearch_deep_audit_findings` |
| GET | `/api/autoresearch/deep-audit/status` | `autoresearch_deep_audit_status` |
| GET | `/api/autoresearch/deep-audit/subsystems` | `autoresearch_deep_audit_subsystems` |
| POST | `/api/autoresearch/deep-audit/trigger` | `autoresearch_deep_audit_trigger` |
| POST | `/api/autoresearch/generate` | `autoresearch_generate` |
| POST | `/api/autoresearch/generate-code-weaknesses` | `autoresearch_generate_code_weaknesses` |
| GET | `/api/autoresearch/proposals` | `autoresearch_proposals` |
| POST | `/api/autoresearch/prune` | `autoresearch_prune` |
| GET | `/api/autoresearch/runs` | `autoresearch_runs` |
| GET | `/api/autoresearch/selftest` | `autoresearch_selftest` |
| POST | `/api/autoresearch/skip` | `autoresearch_skip_proposal` |
| GET | `/api/autoresearch/status` | `autoresearch_status` |
| POST | `/api/autoresearch/stop` | `autoresearch_stop` |
| GET | `/api/autoresearch/test-foundry/status` | `autoresearch_test_foundry_status` |
| GET | `/api/autoresearch/test-foundry/targets` | `autoresearch_test_foundry_targets` |
| POST | `/api/autoresearch/test-foundry/trigger` | `autoresearch_test_foundry_trigger` |
| POST | `/api/autoresearch/trigger` | `autoresearch_trigger` |
| GET | `/api/autoresearch/worklist` | `autoresearch_worklist` |

#### `/api/config`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/config` | `get_config` |
| PUT | `/api/config` | `update_config` |
| GET | `/api/config/defaults` | `get_defaults` |
| GET | `/api/config/raw` | `get_config_raw` |
| PUT | `/api/config/raw` | `update_config_raw` |
| GET | `/api/config/schema` | `get_schema` |

#### `/api/credentials`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/credentials/pool` | `list_credential_pool` |
| POST | `/api/credentials/pool` | `add_credential_pool_entry` |
| DELETE | `/api/credentials/pool/{provider}/{index}` | `remove_credential_pool_entry` |

#### `/api/cron`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/cron/delivery-targets` | `get_cron_delivery_targets` |
| GET | `/api/cron/jobs` | `list_cron_jobs` |
| POST | `/api/cron/jobs` | `create_cron_job` |
| DELETE | `/api/cron/jobs/{job_id}` | `delete_cron_job` |
| GET | `/api/cron/jobs/{job_id}` | `get_cron_job` |
| PUT | `/api/cron/jobs/{job_id}` | `update_cron_job` |
| POST | `/api/cron/jobs/{job_id}/pause` | `pause_cron_job` |
| POST | `/api/cron/jobs/{job_id}/resume` | `resume_cron_job` |
| GET | `/api/cron/jobs/{job_id}/runs` | `list_cron_job_runs` |
| POST | `/api/cron/jobs/{job_id}/trigger` | `trigger_cron_job` |
| GET | `/api/cron/observability` | `cron_observability` |
| GET | `/api/cron/observability/output/{job_id}` | `cron_observability_output` |

#### `/api/curator`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/curator` | `get_curator_status` |
| PUT | `/api/curator/paused` | `set_curator_paused` |
| POST | `/api/curator/run` | `run_curator` |

#### `/api/dashboard`

| Methods | Path | Handler |
|---|---|---|
| POST | `/api/dashboard/agent-plugins/install` | `post_agent_plugin_install` |
| DELETE | `/api/dashboard/agent-plugins/{name:path}` | `delete_agent_plugin` |
| POST | `/api/dashboard/agent-plugins/{name:path}/disable` | `post_agent_plugin_disable` |
| POST | `/api/dashboard/agent-plugins/{name:path}/enable` | `post_agent_plugin_enable` |
| POST | `/api/dashboard/agent-plugins/{name:path}/update` | `post_agent_plugin_update` |
| GET | `/api/dashboard/font` | `get_dashboard_font` |
| PUT | `/api/dashboard/font` | `set_dashboard_font` |
| PUT | `/api/dashboard/plugin-providers` | `put_plugin_providers` |
| GET | `/api/dashboard/plugins` | `get_dashboard_plugins` |
| GET | `/api/dashboard/plugins/hub` | `get_plugins_hub` |
| GET | `/api/dashboard/plugins/rescan` | `rescan_dashboard_plugins` |
| POST | `/api/dashboard/plugins/{name:path}/visibility` | `post_plugin_visibility` |
| PUT | `/api/dashboard/theme` | `set_dashboard_theme` |
| GET | `/api/dashboard/themes` | `get_dashboard_themes` |

#### `/api/env`

| Methods | Path | Handler |
|---|---|---|
| DELETE | `/api/env` | `remove_env_var` |
| GET | `/api/env` | `get_env_vars` |
| PUT | `/api/env` | `set_env_var` |
| POST | `/api/env/reveal` | `reveal_env_var` |

#### `/api/family-organizer`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/family-organizer/backlog` | `family_organizer_backlog` |
| GET | `/api/family-organizer/backlog/{item_id:path}` | `family_organizer_backlog_detail` |

#### `/api/gateway`

| Methods | Path | Handler |
|---|---|---|
| POST | `/api/gateway/restart` | `restart_gateway` |
| POST | `/api/gateway/start` | `start_gateway` |
| POST | `/api/gateway/stop` | `stop_gateway` |

#### `/api/health-status`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/health-status` | `health_status` |

#### `/api/hermes`

| Methods | Path | Handler |
|---|---|---|
| POST | `/api/hermes/update` | `update_hermes` |
| GET | `/api/hermes/update/check` | `check_hermes_update` |

#### `/api/logs`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/logs` | `get_logs` |

#### `/api/mcp`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/mcp/catalog` | `list_mcp_catalog` |
| POST | `/api/mcp/catalog/install` | `install_mcp_catalog_entry` |
| GET | `/api/mcp/servers` | `list_mcp_servers` |
| POST | `/api/mcp/servers` | `add_mcp_server` |
| DELETE | `/api/mcp/servers/{name}` | `remove_mcp_server` |
| PUT | `/api/mcp/servers/{name}/enabled` | `set_mcp_server_enabled` |
| POST | `/api/mcp/servers/{name}/test` | `test_mcp_server` |

#### `/api/media`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/media` | `get_media` |

#### `/api/memory`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/memory` | `get_memory_status` |
| PUT | `/api/memory/provider` | `set_memory_provider` |
| POST | `/api/memory/reset` | `reset_memory` |

#### `/api/messaging`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/messaging/platforms` | `get_messaging_platforms` |
| PUT | `/api/messaging/platforms/{platform_id}` | `update_messaging_platform` |
| POST | `/api/messaging/platforms/{platform_id}/test` | `test_messaging_platform` |
| POST | `/api/messaging/telegram/onboarding/start` | `start_telegram_onboarding` |
| DELETE | `/api/messaging/telegram/onboarding/{pairing_id}` | `cancel_telegram_onboarding` |
| GET | `/api/messaging/telegram/onboarding/{pairing_id}` | `get_telegram_onboarding_status` |
| POST | `/api/messaging/telegram/onboarding/{pairing_id}/apply` | `apply_telegram_onboarding` |

#### `/api/metrics-lite`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/metrics-lite` | `metrics_lite` |

#### `/api/model`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/model/auxiliary` | `get_auxiliary_models` |
| GET | `/api/model/info` | `get_model_info` |
| GET | `/api/model/options` | `get_model_options` |
| GET | `/api/model/recommended-default` | `get_recommended_default_model` |
| POST | `/api/model/set` | `set_model_assignment` |

#### `/api/ops`

| Methods | Path | Handler |
|---|---|---|
| POST | `/api/ops/backup` | `run_backup` |
| GET | `/api/ops/checkpoints` | `list_checkpoints` |
| POST | `/api/ops/checkpoints/prune` | `prune_checkpoints` |
| POST | `/api/ops/config-migrate` | `run_config_migrate` |
| POST | `/api/ops/debug-share` | `run_debug_share_endpoint` |
| POST | `/api/ops/doctor` | `run_doctor` |
| POST | `/api/ops/dump` | `run_dump` |
| DELETE | `/api/ops/hooks` | `delete_hook` |
| GET | `/api/ops/hooks` | `list_hooks` |
| POST | `/api/ops/hooks` | `create_hook` |
| POST | `/api/ops/import` | `run_import` |
| POST | `/api/ops/prompt-size` | `run_prompt_size` |
| POST | `/api/ops/security-audit` | `run_security_audit` |

#### `/api/orchestration`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/orchestration/backlog` | `orchestration_backlog` |
| GET | `/api/orchestration/backlog/{id:path}` | `orchestration_backlog_detail` |

#### `/api/pairing`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/pairing` | `list_pairing` |
| POST | `/api/pairing/approve` | `approve_pairing` |
| POST | `/api/pairing/clear-pending` | `clear_pending_pairing` |
| POST | `/api/pairing/revoke` | `revoke_pairing` |

#### `/api/plugins/hermes-achievements`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/plugins/hermes-achievements/achievements` | `achievements` |
| GET | `/api/plugins/hermes-achievements/recent-unlocks` | `recent_unlocks` |
| POST | `/api/plugins/hermes-achievements/rescan` | `rescan` |
| POST | `/api/plugins/hermes-achievements/reset-state` | `reset_state` |
| GET | `/api/plugins/hermes-achievements/scan-status` | `scan_status` |
| GET | `/api/plugins/hermes-achievements/sessions/{session_id}/badges` | `session_badges` |

#### `/api/plugins/kanban`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/plugins/kanban/assignees` | `get_assignees` |
| DELETE | `/api/plugins/kanban/attachments/{attachment_id}` | `remove_attachment` |
| GET | `/api/plugins/kanban/attachments/{attachment_id}` | `download_attachment` |
| GET | `/api/plugins/kanban/board` | `get_board` |
| GET | `/api/plugins/kanban/boards` | `list_boards` |
| POST | `/api/plugins/kanban/boards` | `create_board_endpoint` |
| DELETE | `/api/plugins/kanban/boards/{slug}` | `delete_board` |
| PATCH | `/api/plugins/kanban/boards/{slug}` | `rename_board` |
| POST | `/api/plugins/kanban/boards/{slug}/switch` | `switch_board` |
| GET | `/api/plugins/kanban/config` | `get_config` |
| GET | `/api/plugins/kanban/decision-queue` | `get_decision_queue` |
| GET | `/api/plugins/kanban/diagnostics` | `list_diagnostics` |
| POST | `/api/plugins/kanban/dispatch` | `dispatch` |
| GET | `/api/plugins/kanban/epics` | `list_epics_endpoint` |
| GET | `/api/plugins/kanban/epics/{epic_id}` | `get_epic_endpoint` |
| GET | `/api/plugins/kanban/home-channels` | `get_home_channels` |
| DELETE | `/api/plugins/kanban/links` | `delete_link` |
| POST | `/api/plugins/kanban/links` | `add_link` |
| GET | `/api/plugins/kanban/orchestration` | `get_orchestration_settings` |
| PUT | `/api/plugins/kanban/orchestration` | `set_orchestration_settings` |
| GET | `/api/plugins/kanban/profiles` | `list_profile_roster` |
| PATCH | `/api/plugins/kanban/profiles/{profile_name}` | `update_profile_description` |
| POST | `/api/plugins/kanban/profiles/{profile_name}/describe-auto` | `auto_describe_profile` |
| GET | `/api/plugins/kanban/runs/blocked-completions` | `list_blocked_completions` |
| GET | `/api/plugins/kanban/runs/recent-results` | `list_recent_results` |
| GET | `/api/plugins/kanban/runs/summary` | `get_runs_summary` |
| GET | `/api/plugins/kanban/runs/today-digest` | `list_today_digest` |
| GET | `/api/plugins/kanban/runs/{run_id}` | `get_run_endpoint` |
| GET | `/api/plugins/kanban/runs/{run_id}/inspect` | `inspect_run_endpoint` |
| POST | `/api/plugins/kanban/runs/{run_id}/terminate` | `terminate_run_endpoint` |
| GET | `/api/plugins/kanban/stats` | `get_stats` |
| POST | `/api/plugins/kanban/tasks` | `create_task` |
| POST | `/api/plugins/kanban/tasks/bulk` | `bulk_update` |
| POST | `/api/plugins/kanban/tasks/flow-capture` | `flow_capture` |
| GET | `/api/plugins/kanban/tasks/review-verdicts` | `list_review_verdicts` |
| DELETE | `/api/plugins/kanban/tasks/{task_id}` | `delete_task` |
| GET | `/api/plugins/kanban/tasks/{task_id}` | `get_task` |
| PATCH | `/api/plugins/kanban/tasks/{task_id}` | `update_task` |
| GET | `/api/plugins/kanban/tasks/{task_id}/attachments` | `list_task_attachments` |
| POST | `/api/plugins/kanban/tasks/{task_id}/attachments` | `upload_task_attachment` |
| POST | `/api/plugins/kanban/tasks/{task_id}/comments` | `add_comment` |
| POST | `/api/plugins/kanban/tasks/{task_id}/decompose` | `decompose_task_endpoint` |
| GET | `/api/plugins/kanban/tasks/{task_id}/deliverables` | `list_task_deliverables` |
| GET | `/api/plugins/kanban/tasks/{task_id}/deliverables/{relative_path:path}` | `download_task_deliverable` |
| GET | `/api/plugins/kanban/tasks/{task_id}/flow-plan` | `get_flow_plan` |
| POST | `/api/plugins/kanban/tasks/{task_id}/flow-release` | `flow_release` |
| DELETE | `/api/plugins/kanban/tasks/{task_id}/home-subscribe/{platform}` | `unsubscribe_home` |
| POST | `/api/plugins/kanban/tasks/{task_id}/home-subscribe/{platform}` | `subscribe_home` |
| GET | `/api/plugins/kanban/tasks/{task_id}/log` | `get_task_log` |
| POST | `/api/plugins/kanban/tasks/{task_id}/reassign` | `reassign_task_endpoint` |
| POST | `/api/plugins/kanban/tasks/{task_id}/reclaim` | `reclaim_task_endpoint` |
| POST | `/api/plugins/kanban/tasks/{task_id}/specify` | `specify_task_endpoint` |
| GET | `/api/plugins/kanban/workers/active` | `list_active_workers` |
| POST | `/api/plugins/kanban/workers/{run_id}/action` | `worker_action_endpoint` |

#### `/api/portal`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/portal` | `get_portal_status` |

#### `/api/profiles`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/profiles` | `list_profiles_endpoint` |
| POST | `/api/profiles` | `create_profile_endpoint` |
| GET | `/api/profiles/active` | `get_active_profile_endpoint` |
| POST | `/api/profiles/active` | `set_active_profile_endpoint` |
| GET | `/api/profiles/sessions` | `get_profiles_sessions` |
| DELETE | `/api/profiles/{name}` | `delete_profile_endpoint` |
| PATCH | `/api/profiles/{name}` | `rename_profile_endpoint` |
| POST | `/api/profiles/{name}/describe-auto` | `describe_profile_auto_endpoint` |
| PUT | `/api/profiles/{name}/description` | `update_profile_description_endpoint` |
| PUT | `/api/profiles/{name}/model` | `update_profile_model_endpoint` |
| POST | `/api/profiles/{name}/open-terminal` | `open_profile_terminal_endpoint` |
| GET | `/api/profiles/{name}/setup-command` | `get_profile_setup_command` |
| GET | `/api/profiles/{name}/soul` | `get_profile_soul` |
| PUT | `/api/profiles/{name}/soul` | `update_profile_soul` |

#### `/api/providers`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/providers/oauth` | `list_oauth_providers` |
| DELETE | `/api/providers/oauth/sessions/{session_id}` | `cancel_oauth_session` |
| DELETE | `/api/providers/oauth/{provider_id}` | `disconnect_oauth_provider` |
| GET | `/api/providers/oauth/{provider_id}/poll/{session_id}` | `poll_oauth_session` |
| POST | `/api/providers/oauth/{provider_id}/start` | `start_oauth_login` |
| POST | `/api/providers/oauth/{provider_id}/submit` | `submit_oauth_code` |
| POST | `/api/providers/validate` | `validate_provider_credential` |

#### `/api/sessions`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/sessions` | `get_sessions` |
| POST | `/api/sessions/bulk-delete` | `bulk_delete_sessions_endpoint` |
| DELETE | `/api/sessions/empty` | `delete_empty_sessions_endpoint` |
| GET | `/api/sessions/empty/count` | `count_empty_sessions_endpoint` |
| POST | `/api/sessions/prune` | `prune_sessions_endpoint` |
| GET | `/api/sessions/search` | `search_sessions` |
| GET | `/api/sessions/stats` | `get_session_stats` |
| DELETE | `/api/sessions/{session_id}` | `delete_session_endpoint` |
| GET | `/api/sessions/{session_id}` | `get_session_detail` |
| PATCH | `/api/sessions/{session_id}` | `rename_session_endpoint` |
| GET | `/api/sessions/{session_id}/export` | `export_session_endpoint` |
| GET | `/api/sessions/{session_id}/latest-descendant` | `get_session_latest_descendant` |
| GET | `/api/sessions/{session_id}/messages` | `get_session_messages` |

#### `/api/skills`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/skills` | `get_skills` |
| POST | `/api/skills/hub/install` | `install_skill_hub` |
| GET | `/api/skills/hub/preview` | `preview_skill_hub` |
| GET | `/api/skills/hub/scan` | `scan_skill_hub` |
| GET | `/api/skills/hub/search` | `search_skills_hub` |
| GET | `/api/skills/hub/sources` | `list_skills_hub_sources` |
| POST | `/api/skills/hub/uninstall` | `uninstall_skill_hub` |
| POST | `/api/skills/hub/update` | `update_skills_hub` |
| PUT | `/api/skills/toggle` | `toggle_skill` |

#### `/api/status`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/status` | `get_status` |

#### `/api/system`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/system/stats` | `get_system_stats` |

#### `/api/tools`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/tools/toolsets` | `get_toolsets` |
| PUT | `/api/tools/toolsets/{name}` | `toggle_toolset` |
| GET | `/api/tools/toolsets/{name}/config` | `get_toolset_config` |
| PUT | `/api/tools/toolsets/{name}/env` | `save_toolset_env` |
| POST | `/api/tools/toolsets/{name}/post-setup` | `run_toolset_post_setup` |
| PUT | `/api/tools/toolsets/{name}/provider` | `select_toolset_provider` |

#### `/api/vault`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/vault/provenance` | `vault_provenance` |

#### `/api/webhooks`

| Methods | Path | Handler |
|---|---|---|
| GET | `/api/webhooks` | `list_webhooks` |
| POST | `/api/webhooks` | `create_webhook` |
| DELETE | `/api/webhooks/{name}` | `delete_webhook` |
| PUT | `/api/webhooks/{name}/enabled` | `set_webhook_enabled` |

#### `/auth`

| Methods | Path | Handler |
|---|---|---|
| GET | `/auth/callback` | `auth_callback` |
| GET | `/auth/login` | `auth_login` |
| POST | `/auth/logout` | `auth_logout` |
| POST | `/auth/password-login` | `auth_password_login` |

#### `/dashboard-plugins`

| Methods | Path | Handler |
|---|---|---|
| GET | `/dashboard-plugins/{plugin_name}/{file_path:path}` | `serve_plugin_asset` |

#### `/login`

| Methods | Path | Handler |
|---|---|---|
| GET | `/login` | `login_page` |

#### `/{full_path:path}`

| Methods | Path | Handler |
|---|---|---|
| GET | `/{full_path:path}` | `no_frontend` |

<!-- generated by scripts/gen_architecture_doc.py — do not edit by hand -->
<!-- END GENERATED:api-endpoints -->

## 7. Kanban DB schema (GENERATED)

> SQLite (WAL) at `~/.hermes/kanban.db` (or a per-board path). All access goes through
> `hermes_cli/kanban_db.py`.

<!-- BEGIN GENERATED:db-schema -->
_8 tables. Source of truth: `hermes_cli/kanban_db.py` (`SCHEMA_SQL`)._

| Table | Columns |
|---|---|
| `tasks` | `id`, `title`, `body`, `assignee`, `status`, `priority`, `created_by`, `created_at`, `started_at`, `completed_at`, `workspace_kind`, `workspace_path`, `branch_name`, `claim_lock`, `claim_expires`, `tenant`, `result`, `idempotency_key`, `consecutive_failures`, `decompose_failed`, `worker_pid`, `last_failure_error`, `max_runtime_seconds`, `last_heartbeat_at`, `current_run_id`, `workflow_template_id`, `current_step_key`, `skills`, `model_override`, `max_retries`, `max_iterations`, `continuation_count`, `max_continuations`, `last_continuation_reason`, `goal_mode`, `goal_max_turns`, `session_id`, `due_at`, `epic_id` |
| `task_links` | `parent_id`, `child_id` |
| `task_comments` | `id`, `task_id`, `author`, `body`, `created_at` |
| `task_events` | `id`, `task_id`, `run_id`, `kind`, `payload`, `created_at` |
| `task_runs` | `id`, `task_id`, `profile`, `step_key`, `status`, `claim_lock`, `claim_expires`, `worker_pid`, `max_runtime_seconds`, `last_heartbeat_at`, `started_at`, `ended_at`, `outcome`, `summary`, `metadata`, `error` |
| `task_attachments` | `id`, `task_id`, `filename`, `stored_path`, `content_type`, `size`, `uploaded_by`, `created_at` |
| `kanban_notify_subs` | `task_id`, `platform`, `chat_id`, `thread_id`, `user_id`, `notifier_profile`, `created_at`, `last_event_id` |
| `epics` | `id`, `title`, `body`, `status`, `created_at`, `closed_at` |

<details><summary>Raw <code>SCHEMA_SQL</code></summary>

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    -- Lifetime count of failed auto_decompose attempts for this task.
    -- Incremented whenever ``decompose_task`` returns ok=False (or
    -- crashes) for the task; reset to 0 on a successful decompose. Unlike
    -- ``consecutive_failures`` this is a decompose-specific signal and does
    -- NOT feed the spawn circuit breaker.
    decompose_failed     INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Appended to the dispatcher's built-in `--skills kanban-worker`.
    -- NULL or empty array = no extras.
    skills               TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- Per-task override for the worker's tool-calling iteration budget
    -- (i.e. `--max-iterations` / `HERMES_MAX_ITERATIONS`). When set,
    -- the dispatcher injects ``HERMES_MAX_ITERATIONS=N`` into the
    -- worker env so the LLM agent loop allows up to N tool-calling
    -- rounds before the iteration-budget guard fires.  NULL (the
    -- common case) falls through to the profile's ``agent.max_turns``
    -- config.  Added 2026-05-27 for hardening sprint TASK 8
    -- (audit-class tasks reproducibly hit the 30-turn profile default).
    max_iterations       INTEGER,
    -- Auto-continuation audit/policy for workers that explicitly report
    -- iteration_budget_exhausted. NULL max_continuations uses the code-level
    -- default; 0 disables auto-continuation for this task.
    continuation_count   INTEGER NOT NULL DEFAULT 0,
    max_continuations    INTEGER,
    last_continuation_reason TEXT,
    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an
    -- auxiliary judge re-evaluates the worker's response against the
    -- card title/body after each turn and feeds a continuation prompt
    -- back into the SAME session until the judge agrees the work is done
    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot
    -- worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var. Indexed so per-session list queries stay cheap on
    -- larger boards.
    session_id           TEXT,
    -- Earliest promotion time (unix epoch seconds). NULL = eligible
    -- immediately (legacy behaviour). recompute_ready holds a task in
    -- ``todo``/``blocked`` until the wall clock reaches a future due_at.
    due_at               INTEGER,
    -- N-E3: durable epic this task belongs to (FK-style pointer into the
    -- ``epics`` table, no hard constraint). NULL = not part of an epic =
    -- exactly the pre-E3 behaviour. Decompose propagates the triage root's
    -- epic_id onto every child so a whole tree rolls up under one epic.
    epic_id              TEXT
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | iteration_budget_exhausted |
    --          (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- N-E3: durable epic — a goal that spans MULTIPLE task trees. Unlike a
-- triage root (one tree) or ``--goal`` (a per-run loop flag) or ``tenant``
-- (a free filter string), an epic is a first-class object tasks point at via
-- ``tasks.epic_id``. Additive: a board with no epics behaves exactly as before.
CREATE TABLE IF NOT EXISTS epics (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    closed_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
```

</details>

<!-- generated by scripts/gen_architecture_doc.py — do not edit by hand -->
<!-- END GENERATED:db-schema -->

## 8. Config keys (GENERATED)

> Defaults from `hermes_cli/config.py`. User overrides live in `~/.hermes/config.yaml`.

<!-- BEGIN GENERATED:config-keys -->
_430 leaf keys. Source of truth: `hermes_cli/config.py` (`DEFAULT_CONFIG`)._

| Key | Default | Type |
|---|---|---|
| `model` | `''` | str |
| `providers` | `{}` | dict |
| `fallback_providers` | `[]` | list |
| `credential_pool_strategies` | `{}` | dict |
| `toolsets` | `['hermes-cli']` | list |
| `max_concurrent_sessions` | `None` | NoneType |
| `agent.max_turns` | `90` | int |
| `agent.gateway_timeout` | `1800` | int |
| `agent.restart_drain_timeout` | `180` | int |
| `agent.api_max_retries` | `3` | int |
| `agent.service_tier` | `''` | str |
| `agent.tool_use_enforcement` | `'auto'` | str |
| `agent.task_completion_guidance` | `True` | bool |
| `agent.environment_probe` | `True` | bool |
| `agent.environment_hint` | `''` | str |
| `agent.gateway_timeout_warning` | `900` | int |
| `agent.clarify_timeout` | `600` | int |
| `agent.gateway_notify_interval` | `180` | int |
| `agent.gateway_auto_continue_freshness` | `3600` | int |
| `agent.image_input_mode` | `'auto'` | str |
| `agent.disabled_toolsets` | `[]` | list |
| `terminal.backend` | `'local'` | str |
| `terminal.modal_mode` | `'auto'` | str |
| `terminal.cwd` | `'.'` | str |
| `terminal.timeout` | `180` | int |
| `terminal.env_passthrough` | `[]` | list |
| `terminal.shell_init_files` | `[]` | list |
| `terminal.auto_source_bashrc` | `True` | bool |
| `terminal.docker_image` | `'nikolaik/python-nodejs:python3.11-nodejs20'` | str |
| `terminal.docker_forward_env` | `[]` | list |
| `terminal.docker_env` | `{}` | dict |
| `terminal.singularity_image` | `'docker://nikolaik/python-nodejs:python3.11-nodejs20'` | str |
| `terminal.modal_image` | `'nikolaik/python-nodejs:python3.11-nodejs20'` | str |
| `terminal.daytona_image` | `'nikolaik/python-nodejs:python3.11-nodejs20'` | str |
| `terminal.container_cpu` | `1` | int |
| `terminal.container_memory` | `5120` | int |
| `terminal.container_disk` | `51200` | int |
| `terminal.container_persistent` | `True` | bool |
| `terminal.docker_volumes` | `[]` | list |
| `terminal.docker_mount_cwd_to_workspace` | `False` | bool |
| `terminal.docker_extra_args` | `[]` | list |
| `terminal.docker_run_as_host_user` | `False` | bool |
| `terminal.persistent_shell` | `True` | bool |
| `web.backend` | `''` | str |
| `web.search_backend` | `''` | str |
| `web.extract_backend` | `''` | str |
| `browser.inactivity_timeout` | `120` | int |
| `browser.command_timeout` | `30` | int |
| `browser.record_sessions` | `False` | bool |
| `browser.allow_private_urls` | `False` | bool |
| `browser.engine` | `'auto'` | str |
| `browser.auto_local_for_private_urls` | `True` | bool |
| `browser.cdp_url` | `''` | str |
| `browser.dialog_policy` | `'must_respond'` | str |
| `browser.dialog_timeout_s` | `300` | int |
| `browser.camofox.managed_persistence` | `False` | bool |
| `browser.camofox.user_id` | `''` | str |
| `browser.camofox.session_key` | `''` | str |
| `browser.camofox.adopt_existing_tab` | `False` | bool |
| `browser.camofox.rewrite_loopback_urls` | `False` | bool |
| `browser.camofox.loopback_host_alias` | `'host.docker.internal'` | str |
| `checkpoints.enabled` | `False` | bool |
| `checkpoints.max_snapshots` | `20` | int |
| `checkpoints.max_total_size_mb` | `500` | int |
| `checkpoints.max_file_size_mb` | `10` | int |
| `checkpoints.auto_prune` | `True` | bool |
| `checkpoints.retention_days` | `7` | int |
| `checkpoints.delete_orphans` | `True` | bool |
| `checkpoints.min_interval_hours` | `24` | int |
| `file_read_max_chars` | `100000` | int |
| `tool_output.max_bytes` | `50000` | int |
| `tool_output.max_lines` | `2000` | int |
| `tool_output.max_line_length` | `2000` | int |
| `tool_loop_guardrails.warnings_enabled` | `True` | bool |
| `tool_loop_guardrails.hard_stop_enabled` | `False` | bool |
| `tool_loop_guardrails.warn_after.exact_failure` | `2` | int |
| `tool_loop_guardrails.warn_after.same_tool_failure` | `3` | int |
| `tool_loop_guardrails.warn_after.idempotent_no_progress` | `2` | int |
| `tool_loop_guardrails.hard_stop_after.exact_failure` | `5` | int |
| `tool_loop_guardrails.hard_stop_after.same_tool_failure` | `8` | int |
| `tool_loop_guardrails.hard_stop_after.idempotent_no_progress` | `5` | int |
| `compression.enabled` | `True` | bool |
| `compression.threshold` | `0.5` | float |
| `compression.target_ratio` | `0.2` | float |
| `compression.protect_last_n` | `20` | int |
| `compression.hygiene_hard_message_limit` | `400` | int |
| `compression.protect_first_n` | `3` | int |
| `compression.abort_on_summary_failure` | `False` | bool |
| `compression.codex_gpt55_autoraise` | `True` | bool |
| `prompt_caching.cache_ttl` | `'5m'` | str |
| `openrouter.response_cache` | `True` | bool |
| `openrouter.response_cache_ttl` | `300` | int |
| `openrouter.min_coding_score` | `0.65` | float |
| `bedrock.region` | `''` | str |
| `bedrock.discovery.enabled` | `True` | bool |
| `bedrock.discovery.provider_filter` | `[]` | list |
| `bedrock.discovery.refresh_interval` | `3600` | int |
| `bedrock.guardrail.guardrail_identifier` | `''` | str |
| `bedrock.guardrail.guardrail_version` | `''` | str |
| `bedrock.guardrail.stream_processing_mode` | `'async'` | str |
| `bedrock.guardrail.trace` | `'disabled'` | str |
| `auxiliary.vision.provider` | `'auto'` | str |
| `auxiliary.vision.model` | `''` | str |
| `auxiliary.vision.base_url` | `''` | str |
| `auxiliary.vision.api_key` | `''` | str |
| `auxiliary.vision.timeout` | `120` | int |
| `auxiliary.vision.extra_body` | `{}` | dict |
| `auxiliary.vision.download_timeout` | `30` | int |
| `auxiliary.web_extract.provider` | `'auto'` | str |
| `auxiliary.web_extract.model` | `''` | str |
| `auxiliary.web_extract.base_url` | `''` | str |
| `auxiliary.web_extract.api_key` | `''` | str |
| `auxiliary.web_extract.timeout` | `360` | int |
| `auxiliary.web_extract.extra_body` | `{}` | dict |
| `auxiliary.compression.provider` | `'auto'` | str |
| `auxiliary.compression.model` | `''` | str |
| `auxiliary.compression.base_url` | `''` | str |
| `auxiliary.compression.api_key` | `''` | str |
| `auxiliary.compression.timeout` | `120` | int |
| `auxiliary.compression.extra_body` | `{}` | dict |
| `auxiliary.skills_hub.provider` | `'auto'` | str |
| `auxiliary.skills_hub.model` | `''` | str |
| `auxiliary.skills_hub.base_url` | `''` | str |
| `auxiliary.skills_hub.api_key` | `''` | str |
| `auxiliary.skills_hub.timeout` | `30` | int |
| `auxiliary.skills_hub.extra_body` | `{}` | dict |
| `auxiliary.approval.provider` | `'auto'` | str |
| `auxiliary.approval.model` | `''` | str |
| `auxiliary.approval.base_url` | `''` | str |
| `auxiliary.approval.api_key` | `''` | str |
| `auxiliary.approval.timeout` | `30` | int |
| `auxiliary.approval.extra_body` | `{}` | dict |
| `auxiliary.mcp.provider` | `'auto'` | str |
| `auxiliary.mcp.model` | `''` | str |
| `auxiliary.mcp.base_url` | `''` | str |
| `auxiliary.mcp.api_key` | `''` | str |
| `auxiliary.mcp.timeout` | `30` | int |
| `auxiliary.mcp.extra_body` | `{}` | dict |
| `auxiliary.title_generation.provider` | `'auto'` | str |
| `auxiliary.title_generation.model` | `''` | str |
| `auxiliary.title_generation.base_url` | `''` | str |
| `auxiliary.title_generation.api_key` | `''` | str |
| `auxiliary.title_generation.timeout` | `30` | int |
| `auxiliary.title_generation.extra_body` | `{}` | dict |
| `auxiliary.triage_specifier.provider` | `'auto'` | str |
| `auxiliary.triage_specifier.model` | `''` | str |
| `auxiliary.triage_specifier.base_url` | `''` | str |
| `auxiliary.triage_specifier.api_key` | `''` | str |
| `auxiliary.triage_specifier.timeout` | `120` | int |
| `auxiliary.triage_specifier.extra_body` | `{}` | dict |
| `auxiliary.kanban_decomposer.provider` | `'auto'` | str |
| `auxiliary.kanban_decomposer.model` | `''` | str |
| `auxiliary.kanban_decomposer.base_url` | `''` | str |
| `auxiliary.kanban_decomposer.api_key` | `''` | str |
| `auxiliary.kanban_decomposer.timeout` | `180` | int |
| `auxiliary.kanban_decomposer.extra_body` | `{}` | dict |
| `auxiliary.profile_describer.provider` | `'auto'` | str |
| `auxiliary.profile_describer.model` | `''` | str |
| `auxiliary.profile_describer.base_url` | `''` | str |
| `auxiliary.profile_describer.api_key` | `''` | str |
| `auxiliary.profile_describer.timeout` | `60` | int |
| `auxiliary.profile_describer.extra_body` | `{}` | dict |
| `auxiliary.curator.provider` | `'auto'` | str |
| `auxiliary.curator.model` | `''` | str |
| `auxiliary.curator.base_url` | `''` | str |
| `auxiliary.curator.api_key` | `''` | str |
| `auxiliary.curator.timeout` | `600` | int |
| `auxiliary.curator.extra_body` | `{}` | dict |
| `auxiliary.code_audit.provider` | `'minimax'` | str |
| `auxiliary.code_audit.model` | `'MiniMax-M2.7'` | str |
| `auxiliary.code_audit.base_url` | `''` | str |
| `auxiliary.code_audit.api_key` | `''` | str |
| `auxiliary.code_audit.timeout` | `120` | int |
| `auxiliary.code_audit.extra_body` | `{}` | dict |
| `auxiliary.test_hardening.provider` | `'minimax'` | str |
| `auxiliary.test_hardening.model` | `'MiniMax-M2.7'` | str |
| `auxiliary.test_hardening.base_url` | `''` | str |
| `auxiliary.test_hardening.api_key` | `''` | str |
| `auxiliary.test_hardening.timeout` | `120` | int |
| `auxiliary.test_hardening.extra_body` | `{}` | dict |
| `display.compact` | `False` | bool |
| `display.personality` | `''` | str |
| `display.resume_display` | `'full'` | str |
| `display.resume_exchanges` | `10` | int |
| `display.resume_max_user_chars` | `300` | int |
| `display.resume_max_assistant_chars` | `200` | int |
| `display.resume_max_assistant_lines` | `3` | int |
| `display.resume_skip_tool_only` | `True` | bool |
| `display.busy_input_mode` | `'interrupt'` | str |
| `display.interface` | `'cli'` | str |
| `display.tui_auto_resume_recent` | `False` | bool |
| `display.tui_agents_nudge` | `True` | bool |
| `display.bell_on_complete` | `False` | bool |
| `display.show_reasoning` | `False` | bool |
| `display.streaming` | `False` | bool |
| `display.timestamps` | `False` | bool |
| `display.final_response_markdown` | `'strip'` | str |
| `display.persistent_output` | `True` | bool |
| `display.persistent_output_max_lines` | `200` | int |
| `display.inline_diffs` | `True` | bool |
| `display.file_mutation_verifier` | `True` | bool |
| `display.turn_completion_explainer` | `True` | bool |
| `display.show_cost` | `False` | bool |
| `display.skin` | `'default'` | str |
| `display.language` | `'en'` | str |
| `display.tui_status_indicator` | `'kaomoji'` | str |
| `display.user_message_preview.first_lines` | `2` | int |
| `display.user_message_preview.last_lines` | `2` | int |
| `display.interim_assistant_messages` | `True` | bool |
| `display.tool_progress_command` | `False` | bool |
| `display.tool_progress_overrides` | `{}` | dict |
| `display.tool_preview_length` | `0` | int |
| `display.ephemeral_system_ttl` | `0` | int |
| `display.platforms.telegram.streaming` | `True` | bool |
| `display.platforms.discord.streaming` | `False` | bool |
| `display.runtime_footer.enabled` | `False` | bool |
| `display.runtime_footer.fields` | `['model', 'context_pct', 'cwd']` | list |
| `display.copy_shortcut` | `'auto'` | str |
| `dashboard.theme` | `'default'` | str |
| `dashboard.show_token_analytics` | `False` | bool |
| `dashboard.oauth.client_id` | `''` | str |
| `dashboard.oauth.portal_url` | `''` | str |
| `dashboard.basic_auth.username` | `''` | str |
| `dashboard.basic_auth.password_hash` | `''` | str |
| `dashboard.basic_auth.password` | `''` | str |
| `dashboard.basic_auth.secret` | `''` | str |
| `dashboard.basic_auth.session_ttl_seconds` | `0` | int |
| `dashboard.public_url` | `''` | str |
| `privacy.redact_pii` | `False` | bool |
| `tts.provider` | `'edge'` | str |
| `tts.edge.voice` | `'en-US-AriaNeural'` | str |
| `tts.elevenlabs.voice_id` | `'pNInz6obpgDQGcFmaJgB'` | str |
| `tts.elevenlabs.model_id` | `'eleven_multilingual_v2'` | str |
| `tts.openai.model` | `'gpt-4o-mini-tts'` | str |
| `tts.openai.voice` | `'alloy'` | str |
| `tts.xai.voice_id` | `'eve'` | str |
| `tts.xai.language` | `'en'` | str |
| `tts.xai.sample_rate` | `24000` | int |
| `tts.xai.bit_rate` | `128000` | int |
| `tts.mistral.model` | `'voxtral-mini-tts-2603'` | str |
| `tts.mistral.voice_id` | `'c69964a6-ab8b-4f8a-9465-ec0925096ec8'` | str |
| `tts.neutts.ref_audio` | `''` | str |
| `tts.neutts.ref_text` | `''` | str |
| `tts.neutts.model` | `'neuphonic/neutts-air-q4-gguf'` | str |
| `tts.neutts.device` | `'cpu'` | str |
| `tts.piper.voice` | `'en_US-lessac-medium'` | str |
| `stt.enabled` | `True` | bool |
| `stt.provider` | `'local'` | str |
| `stt.local.model` | `'base'` | str |
| `stt.local.language` | `''` | str |
| `stt.openai.model` | `'whisper-1'` | str |
| `stt.mistral.model` | `'voxtral-mini-latest'` | str |
| `stt.elevenlabs.model_id` | `'scribe_v2'` | str |
| `stt.elevenlabs.language_code` | `''` | str |
| `stt.elevenlabs.tag_audio_events` | `False` | bool |
| `stt.elevenlabs.diarize` | `False` | bool |
| `voice.record_key` | `'ctrl+b'` | str |
| `voice.max_recording_seconds` | `120` | int |
| `voice.auto_tts` | `False` | bool |
| `voice.beep_enabled` | `True` | bool |
| `voice.silence_threshold` | `200` | int |
| `voice.silence_duration` | `3.0` | float |
| `human_delay.mode` | `'off'` | str |
| `human_delay.min_ms` | `800` | int |
| `human_delay.max_ms` | `2500` | int |
| `context.engine` | `'compressor'` | str |
| `memory.memory_enabled` | `True` | bool |
| `memory.user_profile_enabled` | `True` | bool |
| `memory.write_approval` | `False` | bool |
| `memory.memory_char_limit` | `2200` | int |
| `memory.user_char_limit` | `1375` | int |
| `memory.provider` | `''` | str |
| `delegation.model` | `''` | str |
| `delegation.provider` | `''` | str |
| `delegation.base_url` | `''` | str |
| `delegation.api_key` | `''` | str |
| `delegation.api_mode` | `''` | str |
| `delegation.inherit_mcp_toolsets` | `True` | bool |
| `delegation.max_iterations` | `50` | int |
| `delegation.child_timeout_seconds` | `600` | int |
| `delegation.reasoning_effort` | `''` | str |
| `delegation.max_concurrent_children` | `3` | int |
| `delegation.max_spawn_depth` | `1` | int |
| `delegation.orchestrator_enabled` | `True` | bool |
| `delegation.subagent_auto_approve` | `False` | bool |
| `prefill_messages_file` | `''` | str |
| `goals.max_turns` | `20` | int |
| `skills.external_dirs` | `[]` | list |
| `skills.template_vars` | `True` | bool |
| `skills.inline_shell` | `False` | bool |
| `skills.inline_shell_timeout` | `10` | int |
| `skills.guard_agent_created` | `False` | bool |
| `skills.write_approval` | `False` | bool |
| `curator.enabled` | `True` | bool |
| `curator.interval_hours` | `168` | int |
| `curator.min_idle_hours` | `2` | int |
| `curator.stale_after_days` | `30` | int |
| `curator.archive_after_days` | `90` | int |
| `curator.prune_builtins` | `True` | bool |
| `curator.backup.enabled` | `True` | bool |
| `curator.backup.keep` | `5` | int |
| `honcho` | `{}` | dict |
| `timezone` | `''` | str |
| `slack.require_mention` | `True` | bool |
| `slack.free_response_channels` | `''` | str |
| `slack.allowed_channels` | `''` | str |
| `slack.channel_prompts` | `{}` | dict |
| `discord.require_mention` | `True` | bool |
| `discord.free_response_channels` | `''` | str |
| `discord.allowed_channels` | `''` | str |
| `discord.auto_thread` | `True` | bool |
| `discord.thread_require_mention` | `False` | bool |
| `discord.history_backfill` | `True` | bool |
| `discord.history_backfill_limit` | `50` | int |
| `discord.reactions` | `True` | bool |
| `discord.channel_prompts` | `{}` | dict |
| `discord.dm_role_auth_guild` | `''` | str |
| `discord.server_actions` | `''` | str |
| `discord.allow_any_attachment` | `False` | bool |
| `discord.max_attachment_bytes` | `33554432` | int |
| `discord.voice_fx.enabled` | `False` | bool |
| `discord.voice_fx.ambient_enabled` | `True` | bool |
| `discord.voice_fx.ambient_path` | `''` | str |
| `discord.voice_fx.ambient_gain` | `0.18` | float |
| `discord.voice_fx.duck_gain` | `0.06` | float |
| `discord.voice_fx.speech_gain` | `1.0` | float |
| `discord.voice_fx.ack_enabled` | `True` | bool |
| `discord.voice_fx.ack_phrases` | `['Let me look into that.', 'One moment.', 'Checking on th...` | list |
| `whatsapp` | `{}` | dict |
| `telegram.reactions` | `False` | bool |
| `telegram.channel_prompts` | `{}` | dict |
| `telegram.allowed_chats` | `''` | str |
| `mattermost.require_mention` | `True` | bool |
| `mattermost.free_response_channels` | `''` | str |
| `mattermost.allowed_channels` | `''` | str |
| `mattermost.channel_prompts` | `{}` | dict |
| `matrix.require_mention` | `True` | bool |
| `matrix.free_response_rooms` | `''` | str |
| `matrix.allowed_rooms` | `''` | str |
| `approvals.mode` | `'manual'` | str |
| `approvals.timeout` | `60` | int |
| `approvals.cron_mode` | `'deny'` | str |
| `approvals.mcp_reload_confirm` | `True` | bool |
| `approvals.destructive_slash_confirm` | `True` | bool |
| `command_allowlist` | `[]` | list |
| `quick_commands` | `{}` | dict |
| `hooks` | `{}` | dict |
| `hooks_auto_accept` | `False` | bool |
| `personalities` | `{}` | dict |
| `security.allow_private_urls` | `False` | bool |
| `security.redact_secrets` | `True` | bool |
| `security.tirith_enabled` | `True` | bool |
| `security.tirith_path` | `'tirith'` | str |
| `security.tirith_timeout` | `5` | int |
| `security.tirith_fail_open` | `True` | bool |
| `security.website_blocklist.enabled` | `False` | bool |
| `security.website_blocklist.domains` | `[]` | list |
| `security.website_blocklist.shared_files` | `[]` | list |
| `security.acked_advisories` | `[]` | list |
| `security.allow_lazy_installs` | `True` | bool |
| `cron.wrap_response` | `True` | bool |
| `cron.max_parallel_jobs` | `None` | NoneType |
| `kanban.dispatch_in_gateway` | `True` | bool |
| `kanban.reporting_channel_id` | `''` | str |
| `kanban.reporting_thread_id` | `''` | str |
| `kanban.orchestrator_channel_id` | `''` | str |
| `kanban.dispatch_interval_seconds` | `60` | int |
| `kanban.failure_limit` | `2` | int |
| `kanban.worker_log_rotate_bytes` | `2097152` | int |
| `kanban.worker_log_backup_count` | `1` | int |
| `kanban.orchestrator_profile` | `''` | str |
| `kanban.default_assignee` | `''` | str |
| `kanban.max_in_progress_per_profile` | `None` | NoneType |
| `kanban.daily_token_cap_per_profile` | `None` | NoneType |
| `kanban.daily_cost_cap_usd` | `None` | NoneType |
| `kanban.auto_decompose` | `True` | bool |
| `kanban.auto_decompose_per_tick` | `3` | int |
| `kanban.dispatch_stale_timeout_seconds` | `14400` | int |
| `code_execution.mode` | `'project'` | str |
| `tools.tool_search.enabled` | `'auto'` | str |
| `tools.tool_search.threshold_pct` | `10` | int |
| `tools.tool_search.search_default_limit` | `5` | int |
| `tools.tool_search.max_search_limit` | `20` | int |
| `logging.level` | `'INFO'` | str |
| `logging.max_size_mb` | `5` | int |
| `logging.backup_count` | `3` | int |
| `model_catalog.enabled` | `True` | bool |
| `model_catalog.url` | `'https://hermes-agent.nousresearch.com/docs/api/model-cat...` | str |
| `model_catalog.ttl_hours` | `1` | int |
| `model_catalog.providers` | `{}` | dict |
| `network.force_ipv4` | `False` | bool |
| `gateway.strict` | `False` | bool |
| `gateway.media_delivery_allow_dirs` | `[]` | list |
| `gateway.trust_recent_files` | `True` | bool |
| `gateway.trust_recent_files_seconds` | `600` | int |
| `streaming.enabled` | `False` | bool |
| `streaming.transport` | `'auto'` | str |
| `streaming.edit_interval` | `0.8` | float |
| `streaming.buffer_threshold` | `24` | int |
| `streaming.cursor` | `' ▉'` | str |
| `streaming.fresh_final_after_seconds` | `60.0` | float |
| `sessions.auto_prune` | `False` | bool |
| `sessions.retention_days` | `90` | int |
| `sessions.vacuum_after_prune` | `True` | bool |
| `sessions.min_interval_hours` | `24` | int |
| `sessions.write_json_snapshots` | `False` | bool |
| `onboarding.seen` | `{}` | dict |
| `onboarding.profile_build` | `'ask'` | str |
| `updates.pre_update_backup` | `False` | bool |
| `updates.backup_keep` | `5` | int |
| `updates.non_interactive_local_changes` | `'stash'` | str |
| `lsp.enabled` | `True` | bool |
| `lsp.wait_mode` | `'document'` | str |
| `lsp.wait_timeout` | `5.0` | float |
| `lsp.install_strategy` | `'auto'` | str |
| `lsp.servers` | `{}` | dict |
| `x_search.model` | `'grok-4.20-reasoning'` | str |
| `x_search.timeout_seconds` | `180` | int |
| `x_search.retries` | `2` | int |
| `secrets.bitwarden.enabled` | `False` | bool |
| `secrets.bitwarden.access_token_env` | `'BWS_ACCESS_TOKEN'` | str |
| `secrets.bitwarden.project_id` | `''` | str |
| `secrets.bitwarden.cache_ttl_seconds` | `300` | int |
| `secrets.bitwarden.override_existing` | `True` | bool |
| `secrets.bitwarden.auto_install` | `True` | bool |
| `secrets.bitwarden.server_url` | `''` | str |
| `paste_collapse_threshold` | `5` | int |
| `paste_collapse_threshold_fallback` | `5` | int |
| `paste_collapse_char_threshold` | `2000` | int |
| `_config_version` | `29` | int |

<!-- generated by scripts/gen_architecture_doc.py — do not edit by hand -->
<!-- END GENERATED:config-keys -->

---

## 9. Cross-references

- **Full dev architecture & contribution rules:** `AGENTS.md` (`## Project Structure`,
  agent loop, adding tools/skills/plugins/toolsets, delegation, curator, cron).
- **Frontend stack & conventions:** `web/README.md`.
- **Cross-agent canon (infra, ports, roster, gates):** `/home/piet/vault/00-Canon/`
  — `infra-topology.md`, `agent-roster.md`, `conventions-gates.md`, `projects-map.md`.
- **Refresh the generated sections (§5–§8):** `python3 scripts/gen_architecture_doc.py`
  (drift check: `--check`).

### Gate note

The dashboard gate (lint + tsc + vitest + build) runs from the live `web/` via `.bin/`-pinned
binaries — worktrees lack `node_modules`. The doc drift check
(`python3 scripts/gen_architecture_doc.py --check`) is wired into that local gate; it is
*not* enforced by GitHub Actions (no CI workflow runs the web build).
