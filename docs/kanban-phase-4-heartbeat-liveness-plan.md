# Kanban Phase 4A: Heartbeat Liveness Diagnostic

Status: implemented in local working tree, not live in gateway until restart.

Canonical plan:

- `/home/piet/vault/03-Agents/Hermes/plans/2026-05-08-hermes-kanban-phase-4-heartbeat-liveness-plan.md`

Receipt:

- `/home/piet/vault/03-Agents/Hermes/receipts/2026-05-08-hermes-kanban-phase-4a-heartbeat-liveness-green.md`

## Scope

Phase 4A makes stale worker heartbeats visible as a read-only diagnostic. It does not auto-reclaim, kill workers, restart services, or mutate task state while calculating liveness.

## Semantics

Diagnostic kind: `stale_heartbeat`

It fires only when:

- task status is `running`;
- `last_heartbeat_at` exists;
- `now - last_heartbeat_at >= heartbeat_stale_after_seconds`.

Default threshold: `300` seconds.

It intentionally does **not** fire for:

- recent heartbeat;
- non-running tasks;
- running tasks with no heartbeat yet.

Reason: `kanban_heartbeat` is optional and intended for long operations. A worker that has never heartbeated should not be treated as stale in Phase 4A.

## Changed files

- `hermes_cli/kanban_diagnostics.py`
  - adds `_rule_stale_heartbeat(...)`
  - adds `stale_heartbeat` to `_RULES`
  - adds `stale_heartbeat` to `DIAGNOSTIC_KINDS`
  - adds default `heartbeat_stale_after_seconds: 300`
- `tests/hermes_cli/test_kanban_diagnostics.py`
  - adds positive stale-heartbeat test
  - adds false-positive boundary tests

## Test evidence

RED:

```text
1 failed, 3 passed
FAILED test_running_task_with_stale_heartbeat_emits_diagnostic
assert 0 == 1
```

GREEN focused:

```text
4 passed in 2.27s
25 passed in 4.26s
2 passed in 2.44s
```

Regression cluster:

```text
184 passed, 4 warnings in 9.73s
```

Warnings are existing dependency deprecations from `lark_oapi/ws/client.py` and `websockets/legacy`.

## Live pickup

Not done yet. Gateway restart requires separate Piet approval.
