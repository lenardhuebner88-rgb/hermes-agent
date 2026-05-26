# Kanban Phase 1/2 Observability

Status: implemented
Scope: Hermes Kanban DB/tool surface only

## Phase 1 — Block context is explicit

Workers can now link a longer blocker explanation to the short `kanban_block` reason:

1. Add context with `kanban_comment(...)`.
2. Pass the returned comment id to `kanban_block(reason=..., context_comment_id=<id>)`.
3. The `blocked` event payload stores:
   - `reason`
   - `context_comment_id`
   - `context_snippet`
   - terminal run fields from Phase 2 (`run_id`, `outcome`, `profile`, `summary`, `ended_at`)

This keeps dashboard/notifier block rows short while preserving a direct pointer to the durable comment thread.

## Phase 2 — Terminal events are self-contained

Terminal lifecycle events now include enough payload to render run history from `task_events` without a separate `task_runs` join for basic operator visibility.

Covered event paths:

- `completed`
- `blocked`
- `spawn_failed`
- `timed_out`
- `crashed`
- `gave_up`

Standard terminal payload fields where available:

- `run_id`
- `outcome`
- `profile`
- `status`
- `summary`
- `error`
- `ended_at`

Event-specific fields are preserved, for example `result_len`, `verified_cards`, `failures`, `elapsed`, `limit`, `pid`, and `claimer`.

## Verification

Focused tests:

```bash
python -m pytest \
  tests/hermes_cli/test_kanban_db.py::test_block_event_can_link_context_comment \
  tests/hermes_cli/test_kanban_db.py::test_terminal_events_include_run_outcome_profile_and_handoff \
  tests/hermes_cli/test_kanban_db.py::test_spawn_failure_terminal_event_includes_error_and_outcome \
  tests/tools/test_kanban_tools.py::test_block_accepts_context_comment_id \
  -q
```

Expected: all pass.
