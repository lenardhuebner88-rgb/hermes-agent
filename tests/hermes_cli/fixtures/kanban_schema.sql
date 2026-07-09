-- Verbatim dump (columns only, comments stripped by sqlite) of the LIVE
-- ~/.hermes/kanban.db schema for `tasks`/`task_runs`, captured 2026-07-09 via:
--   python3 -c "import sqlite3,os; c=sqlite3.connect('file:'+os.path.expanduser('~/.hermes/kanban.db')+'?mode=ro', uri=True); \
--     print('\n'.join(r[0] for r in c.execute(\"SELECT sql FROM sqlite_master WHERE type='table' AND name IN ('tasks','task_runs')\")))"
-- Used by test_library_results.py to build a schema-accurate tmp DB and
-- INSERT real rows harvested from the live board (S1.1).
CREATE TABLE tasks (
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
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    current_run_id       INTEGER,
    workflow_template_id TEXT,
    current_step_key     TEXT,
    skills               TEXT,
    max_retries          INTEGER
, branch_name TEXT, model_override TEXT, session_id TEXT, max_iterations INTEGER, continuation_count INTEGER NOT NULL DEFAULT 0, max_continuations INTEGER, last_continuation_reason TEXT, goal_mode INTEGER NOT NULL DEFAULT 0, goal_max_turns INTEGER, decompose_failed INTEGER NOT NULL DEFAULT 0, due_at INTEGER, acceptance_criteria TEXT, epic_id TEXT, kind TEXT, auto_retry_count INTEGER NOT NULL DEFAULT 0, planspec_subtask_id TEXT, planspec_source TEXT, freigabe TEXT, live_test_depth TEXT, integration_retry_count INTEGER NOT NULL DEFAULT 0, review_tier TEXT, transient_retry_count INTEGER NOT NULL DEFAULT 0, budget_extension_count INTEGER NOT NULL DEFAULT 0, budget_progress_marker INTEGER, scope_contract TEXT, block_kind TEXT, block_recurrences INTEGER NOT NULL DEFAULT 0, project_id TEXT, ui_impact TEXT);
CREATE TABLE task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
, worker_exit_kind TEXT, worker_exit_code INTEGER, worker_protocol_state TEXT, worker_failure_fingerprint TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, verdict TEXT, cost_status TEXT CHECK (cost_status IN ('actual','estimated')), pre_run_commit_sha TEXT);
