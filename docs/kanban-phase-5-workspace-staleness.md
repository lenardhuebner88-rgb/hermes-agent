# Kanban Phase 5: Workspace Staleness Diagnostics

Status: implemented in local working tree; gateway live pickup still requires separate restart approval.

Canonical plan:

- `/home/piet/vault/03-Agents/Hermes/plans/2026-05-08-hermes-kanban-phase-5-workspace-staleness-plan.md`

Green receipt:

- `/home/piet/vault/03-Agents/Hermes/receipts/2026-05-08-hermes-kanban-phase-5-workspace-staleness-green.md`

## Scope

Phase 5A adds read-only workspace diagnostics to `hermes_cli/kanban_diagnostics.py`. It does not mutate task state, recreate workspaces, run garbage collection, kill workers, or change dispatcher policy.

## Diagnostics

### `workspace_missing`

Fires when:

- task status is `running`;
- `workspace_path` is set;
- `workspace_path` is absolute;
- path does not exist.

Severity: `error`.

Suggested action: inspect worker log via `hermes kanban log <task_id>`, then explicitly reclaim/reassign if needed.

It intentionally does not fire for:

- non-running tasks;
- missing `workspace_path`;
- relative workspace paths;
- existing workspace directories.

### `stale_workspace`

Fires when:

- task status is `archived`;
- `workspace_kind == "scratch"`;
- most recent `archived` event is older than `workspace_stale_after_hours`;
- workspace path exists as a directory;
- workspace path resolves under the configured Kanban workspaces root.

Default threshold: `workspace_stale_after_hours = 24`.

Severity: `warning`.

Suggested action: `hermes kanban gc`.

It intentionally does not fire for:

- `dir` workspaces;
- `worktree` workspaces;
- missing/already-removed scratch directories;
- recent archives below threshold;
- scratch paths outside `workspaces_root()`;
- non-archived tasks.

## Changed files

- `hermes_cli/kanban_diagnostics.py`
  - adds `Path` import;
  - adds `_path_is_under(...)`;
  - adds `_configured_workspace_root(...)`;
  - adds `_rule_workspace_missing(...)`;
  - adds `_rule_stale_workspace(...)`;
  - adds both rules to `_RULES`;
  - adds `workspace_missing` and `stale_workspace` to `DIAGNOSTIC_KINDS`;
  - adds default `workspace_stale_after_hours: 24`.
- `tests/hermes_cli/test_kanban_diagnostics.py`
  - adds positive and false-positive tests for both workspace diagnostics.

## Test evidence

RED:

```text
2 failed in 2.87s
FAILED test_running_task_with_missing_workspace_emits_diagnostic
FAILED test_archived_scratch_task_with_old_workspace_emits_stale_workspace
```

GREEN focused:

```text
12 passed in 4.71s
```

Diagnostics full file:

```text
37 passed in 4.79s
```

Regression cluster:

```text
196 passed, 4 warnings in 12.70s
```

Warnings are unchanged dependency deprecations from `lark_oapi/ws/client.py` and `websockets/legacy`.

## Live pickup

Not done in this slice. Gateway restart/live smoke remains a separate approval gate.
