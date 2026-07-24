# Test patch sites against `hermes_cli.kanban_db`

Plan Task 6. Re-derived on `1254ce618`, not taken from the plan's earlier list.

## Result: **no site needs re-pointing**

The concern Task 6 exists for is real in general —
`monkeypatch.setattr("hermes_cli.kanban_db.connect", fake)` rebinds the attribute on
`kanban_db` only, while a `kanban_ext` submodule holding
`from hermes_cli.kanban_db import connect` keeps its own binding and never sees the patch.
Most such tests would fail loudly; `task_age` could pass **silently** against the real
implementation, which is the unacceptable outcome.

Measured against `docs/refactor/boundary-map.kanban_ext.yaml`, that hazard does not
materialise here: **every symbol any test patches is upstream-owned, so all of them stay
in `kanban_db.py`.** Task 7 Step 6 is therefore a no-op — no test edits are required by
the extraction.

| patched symbol | ownership | destination |
|---|---|---|
| `connect` | UPSTREAM | stays |
| `init_db` | UPSTREAM | stays |
| `_record_task_failure` | UPSTREAM | stays |
| `_record_worker_exit` | UPSTREAM | stays |
| `task_age` | UPSTREAM | stays |
| `kanban_db_path` | UPSTREAM | stays |
| `dispatch_once` | UPSTREAM | stays |
| `_pid_alive` | UPSTREAM | stays |
| `_check_file_length_invariant` | UPSTREAM | stays |

**Control probe.** An all-stays table is exactly what a broken lookup also produces, so the
classifier was proven live: fed symbols that *do* move it reports
`_stamp_strategist_lever_outcome_shipped`, `TERMINAL_TASK_STATUSES`,
`KANBAN_ARTIFACT_TREE_MAX_ENTRIES` → `kanban_ext.task_core`, and would flag **730** symbols
as moving. The zero above is a real zero.

## Sites, by shape

### String-patched — `patch("hermes_cli.kanban_db.<symbol>")` (33 sites)

Symbols that are kanban_db's own (all stay):

| file:line | symbol |
|---|---|
| `tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py:34,47` | `connect` |
| `tests/plugins/test_kanban_dashboard_plugin.py:4815,4847` | `task_age` |
| `tests/run_agent/test_run_agent.py:5812,5862` | `_record_task_failure` |
| `tests/run_agent/test_run_agent.py:5814` | `connect` |
| `tests/agent/test_turn_finalizer_iteration_limit_exit.py:275` | `connect` |
| `tests/agent/test_turn_finalizer_iteration_limit_exit.py:283` | `_record_task_failure` |
| `tests/hermes_cli/test_voice_live_tools.py:724,753,779,818,852,867` | `connect` |
| `tests/hermes_cli/test_kanban_db_runtime.py:591,661,698,718` | `_record_worker_exit` |
| `tests/hermes_cli/test_kanban_notify.py:282` | `init_db` |

### Module-attribute patches — `patch("hermes_cli.kanban_db.<module>.<attr>")`

| file:line | target |
|---|---|
| `tests/hermes_cli/test_kanban_db_spawn_workdir.py:167` | `sqlite3.connect` |
| `tests/hermes_cli/test_disposition_ledger.py:150` | `time.time` |
| `tests/hermes_cli/test_kanban_db_runtime.py:470,493` | `os.path.getsize` |
| `tests/hermes_cli/test_kanban_db_runtime.py:590,601,611,629,643,660,717` | `os.waitpid` |
| `tests/hermes_cli/test_kanban_db_runtime.py:600,719` | `os.name` |

**Unaffected by construction.** These patch attributes on the `os` / `time` / `sqlite3`
*module objects*, not on `kanban_db`. Emission rule 1 copies the origin header verbatim
into every submodule, so each submodule binds the **same** module object. The patch is
visible in `kanban_ext` without any change.

### Attribute-patched — `setattr(kanban_db, "<symbol>", …)` (5 sites)

| file:line | symbol |
|---|---|
| `tests/test_planspec_disposition.py:11,72` | `connect` |
| `tests/hermes_cli/test_operator_inventory.py:199` | `kanban_db_path` |
| `tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py:92` | `dispatch_once` |
| `tests/hermes_cli/test_kanban_workflow_routing.py:60` | `_pid_alive` |

### `patch.object` — one site the plan's two greps miss

`tests/hermes_cli/test_kanban_db_runtime.py:518` —
`unittest.mock.patch.object(kanban_db_module, "_check_file_length_invariant", …)`.
Upstream-owned, stays. **The plan's Step 1 greps do not find this shape**
(`patch.object(<alias>, …)` with a module alias); a third pattern is needed:

```bash
rg -n 'patch\.object\(\s*kanban_db' tests/ hermes_cli/ gateway/ scripts/
```

## Standing hazard for Task 8 and beyond

The zero above holds **only for this boundary map**. Any future change that moves a symbol
a test patches re-opens the hazard, and `task_age`-shaped failures are silent. Re-run the
three greps plus the classification whenever the map changes.
