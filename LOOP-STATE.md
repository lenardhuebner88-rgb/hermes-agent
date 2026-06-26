# Loop 2 - Harness Failure-Mode-Burndown

Branch: `feat/harness-failure-burndown`
Base: `main` at `6c2f64491`
Started: `2026-06-26T20:28:00Z`
Operator overrides:
- 2026-06-26T20:37Z: use targeted `scripts/run_tests.sh <file>` gates only; no full-suite runs for this loop.
- 2026-06-26T20:37Z: Discord pings are not necessary.

## Addressed Clusters

### Round 1 - Gap 3: Decompose root finalizer does not auto-merge

Status: fixed, targeted gates green.
Commit: `e5bf77c1f`

Cluster:
- Failure mode: decomposed repo root stays as an open finalizer after all children complete, so worker-isolation integration does not merge the root chain automatically.
- Deterministic reproduction: fake git repo, `kanban_home` fixture, no LLM call.
- Test: `tests/hermes_cli/test_kanban_decompose.py::test_decompose_children_auto_integrate_root_finalizer`

Red proof before fix:
- Command: `scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose.py`
- Result: failed, 58 passed / 1 failed.
- Failure: first child was provisioned on `kanban/<child>` instead of the decomposed root branch `kanban/<root>`.

Fix:
- `hermes_cli/kanban_worktrees.py`
- Detect decomposed roots via `task_events.kind='decomposed'`.
- Map direct decompose children back to the root for worker-isolation branch/worktree selection.
- Include decompose children in chain membership even though decompose links point child -> root.
- Auto-complete the decomposed root after the last child triggers a green integration.

Green proof after fix:
- `scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose.py` -> 59 passed.
- `scripts/run_tests.sh tests/hermes_cli/test_kanban_worktrees.py` -> 117 passed.

Notes:
- A full-suite run was started under the original brief and interrupted after operator override; it is not counted as a gate.
- Discord notification intentionally skipped by operator override.

### Round 2 - Gap 7: pid_not_alive death recovery

Status: fixed, targeted gate green, pending commit hash.

Mining:
- Source: read-only `sqlite3.connect("file:/home/piet/.hermes/kanban.db?mode=ro", uri=True)`.
- Selected cluster: `crashed:pid_not_alive`.
- Impact: 60 failure runs across 41 tasks; severity weight 5; score 300.
- Excluded: Round 1 decompose-root finalizer cluster; synthetic stress artifacts `t_8ec520d3`, `t_bbb65f0e`, `t_5fe2f45f`.
- Skipped as out-of-scope/historical: OpenClaw HMAC-secret clusters (`spawn_failed`/`gave_up`) because OpenClaw is decommissioned and the failure depends on live secret/runtime configuration.

Test:
- `tests/hermes_cli/test_kanban_death_recovery.py`
- Fake `kanban_home`; mocked `_pid_alive`; no LLM run.

Red proof before fix:
- Command: `scripts/run_tests.sh tests/hermes_cli/test_kanban_death_recovery.py`
- Result: failed, 0 passed / 1 failed.
- Failure: unknown dead PIDs were returned as `crashed` instead of bounded transient recovery.

Fix:
- `hermes_cli/kanban_db.py`
- For a host-local running task with an active run whose PID is gone but no recent reap status exists (`_classify_worker_exit(pid) == "unknown"`), close the run as `transient_retry`, increment `transient_retry_count`, requeue to `ready`, and avoid the crashed/systemic-breaker path.
- Preserve existing hard-failure behavior for nonzero exits, signals, clean protocol violations, and synthetic rows without an active run.

Green proof after fix:
- `scripts/run_tests.sh tests/hermes_cli/test_kanban_death_recovery.py` -> 2 passed.
- `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py` -> 584 passed / 2 unrelated pricing-golden failures in `_equiv_from_tokens`; not counted as Round 2 regression evidence.

Notes:
- Discord notification intentionally skipped by operator override.

## Skipped Clusters

None yet.

## Next Mining

Round 3 must mine fresh impact from read-only `~/.hermes/kanban.db`, excluding addressed Round 1/2 clusters.
