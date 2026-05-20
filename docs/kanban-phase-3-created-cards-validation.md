# Kanban Phase 3 — Created Cards Validation

Status: implemented
Scope: Hermes Kanban DB/tool surface only

## Problem

`kanban_complete(created_cards=[...])` rejects phantom or foreign task ids, but before Phase 3 workers had no side-effect-free way to check the manifest they planned to submit. A typo or stale id could only be discovered by attempting completion and triggering a terminal handoff failure path.

## Implementation

Phase 3 adds a dry-run validation path:

- DB helper: `validate_created_cards(conn, completing_task_id, claimed_ids)`
- Model-native tool: `kanban_validate_created_cards(created_cards=[...])`
- Shared completion gate: `complete_task(..., created_cards=...)` now uses the same validation helper before completing.

The helper returns:

```json
{
  "ok": false,
  "task_id": "t_parent",
  "claimed_cards": ["t_child", "t_deadbeef"],
  "verified_cards": ["t_child"],
  "phantom_cards": ["t_deadbeef"]
}
```

## Verification Semantics

A claimed card is verified when it exists and at least one trust condition holds:

1. `tasks.created_by` matches the completing task's assignee profile.
2. `tasks.created_by` matches the completing task id.
3. The claimed card is linked as a child of the completing task.

Everything else is reported as `phantom_cards`, including non-existent ids and existing cards created by another worker/profile without a child link.

## Side Effects

`kanban_validate_created_cards` is read-only for task lifecycle:

- does not complete the task;
- does not block the task;
- does not emit `completion_blocked_hallucination`;
- does not create an audit event.

The actual completion path still emits `completion_blocked_hallucination` and raises `HallucinatedCardsError` when `phantom_cards` is non-empty.

## Worker Pattern

1. Capture ids returned from successful `kanban_create` calls.
2. If the manifest is non-trivial or uncertain, call:

```python
kanban_validate_created_cards(created_cards=[...])
```

3. If `ok=true`, pass the same list to:

```python
kanban_complete(summary="...", metadata={...}, created_cards=[...])
```

4. If `ok=false`, correct the list or mention the rejected ids in the summary without claiming them as created cards.

## Tests

Focused tests:

```bash
python -m pytest \
  tests/hermes_cli/test_kanban_db.py::test_validate_created_cards_dry_run_reports_verified_and_phantom_without_mutation \
  tests/hermes_cli/test_kanban_db.py::test_complete_task_reuses_created_cards_validation_gate \
  tests/tools/test_kanban_tools.py::test_validate_created_cards_reports_phantoms_without_completing \
  tests/tools/test_kanban_tools.py::test_kanban_tools_visible_with_env_var \
  -q
```

Expected: all pass.
