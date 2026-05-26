BEGIN;
ALTER TABLE task_runs ADD COLUMN worker_exit_kind TEXT;
ALTER TABLE task_runs ADD COLUMN worker_exit_code INTEGER;
ALTER TABLE task_runs ADD COLUMN worker_protocol_state TEXT;
ALTER TABLE task_runs ADD COLUMN worker_failure_fingerprint TEXT;
COMMIT;
