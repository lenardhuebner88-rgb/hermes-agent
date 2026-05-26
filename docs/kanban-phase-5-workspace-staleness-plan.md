# Kanban Phase 5: Workspace Staleness Plan

Status: planned only; no implementation yet.

Canonical plan:

- `/home/piet/vault/03-Agents/Hermes/plans/2026-05-08-hermes-kanban-phase-5-workspace-staleness-plan.md`

## Scope

Phase 5A should add read-only workspace diagnostics only:

- `workspace_missing`: a `running` task has a persisted absolute `workspace_path`, but the path no longer exists.
- `stale_workspace`: an `archived` `scratch` task still has an owned workspace directory under `workspaces_root()` after a retention threshold.

## Non-goals

- No automatic workspace deletion.
- No automatic `kanban gc`.
- No worker kill/reclaim policy change.
- No changes to dispatcher spawn or `resolve_workspace()` in the first slice.
- No classifying external `dir` or `worktree` paths as Kanban-owned garbage.
- No gateway restart before Green plus separate approval.

## Evidence anchors

Existing code paths inspected:

- `hermes_cli/kanban_db.py::workspaces_root(...)`
- `hermes_cli/kanban_db.py::resolve_workspace(...)`
- `hermes_cli/kanban_db.py::dispatch_once(...)`
- `hermes_cli/kanban_db.py::_default_spawn(...)`
- `hermes_cli/kanban.py::_cmd_gc(...)`
- `hermes_cli/kanban_diagnostics.py`
- `tests/hermes_cli/test_kanban_diagnostics.py`

## Planned files

- Modify: `hermes_cli/kanban_diagnostics.py`
- Modify: `tests/hermes_cli/test_kanban_diagnostics.py`
- Create after Green: `docs/kanban-phase-5-workspace-staleness.md`
- Receipt after Green: `/home/piet/vault/03-Agents/Hermes/receipts/2026-05-08-hermes-kanban-phase-5-workspace-staleness-green.md`

## Required gates

1. Precheck dirty tree and path-specific diffs.
2. RED tests for `workspace_missing` and `stale_workspace`.
3. GREEN minimal diagnostics implementation.
4. False-positive boundary tests.
5. Regression cluster:
   - `tests/hermes_cli/test_kanban_diagnostics.py`
   - `tests/hermes_cli/test_kanban_db.py`
   - `tests/hermes_cli/test_kanban_cli.py`
   - `tests/tools/test_kanban_tools.py`
6. Separate Piet approval for gateway restart/live pickup.
