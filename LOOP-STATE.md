# Loop 2 - Harness Failure-Mode-Burndown

Branch: `feat/harness-failure-burndown`
Base: `main` at `6c2f64491`
Started: `2026-06-26T20:28:00Z`
Operator overrides:
- 2026-06-26T20:37Z: use targeted `scripts/run_tests.sh <file>` gates only; no full-suite runs for this loop.
- 2026-06-26T20:37Z: Discord pings are not necessary.

## Addressed Clusters

### Round 1 - Gap 3: Decompose root finalizer does not auto-merge

Status: fixed, targeted gates green, pending commit hash.

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

## Skipped Clusters

None yet.

## Next Mining

Round 2 must mine fresh impact from read-only `~/.hermes/kanban.db`, excluding the addressed Round 1 cluster.
