# GROK_WATCHER_EXECUTOR_REPORT

**Branch:** `refactor/kanban-watcher-owned-executor-20260716`  
**Worktree:** `~/.grok/worktrees/piet-hermes-agent/watcher-executor/`  
**Date:** 2026-07-16  
**Scope:** `gateway/kanban_watchers.py` + 2 named test files (plus one extra seam patch in the same gate file for the closeout test that also mocked `asyncio.to_thread`).

---

## Verify-before-editing (all PASS — did not abort)

### 1. `self._get_executor()` reachable from the mixin

- `GatewayRunner` MRO: `GatewayRunner → GatewayAuthorizationMixin → GatewayKanbanWatchersMixin → GatewaySlashCommandsMixin → object`.
- `_get_executor` lives on `GatewayRunner` (`gateway/run.py:15458`); mixin methods resolve it via MRO.
- Lazy init tolerates `object.__new__(GatewayRunner)`: lock/executor created on first getattr; verified live:
  - `executor type: ThreadPoolExecutor max_workers: 10 prefix: hermes-gateway`
  - after `_shutdown_executor()`: `RuntimeError: Gateway is shutting down; executor unavailable`

### 2. `_executor_closing` / RuntimeError tolerance at every migrated site

`_get_executor` raises `RuntimeError` once `_executor_closing` is set. Migrated sites:

| Path | Guard |
|------|--------|
| Notifications tick body (all notifier `to_thread` sites incl. advance/rewind/checkpoint/unsub/tree/collect) | Outer `try` @ `while self._running` → `except Exception` logs warning |
| `save_alert_state` (finally after alert tick) | Still inside outer tick `try` → `except Exception` |
| Stall-flush | Own `try/except Exception` |
| Auto-receipt | Own `try/except Exception` |
| `_kanban_alert_rules_tick` → `_evaluate` | Called from tick `try`; outer catches |
| Dispatcher zombie reaper | Own `try/except Exception` |
| Dispatcher main tick body (boards/caps/tick/ready) | Outer dispatcher `try` → `except Exception` |
| Cost backfill | Nested `try/except Exception` |
| Heartbeat kwargs write | Nested `try/except Exception` |

**No unguarded site found.** No extra wrap required.

### 3. Concurrency sanity vs `max_workers=10`

- Each watcher loop **awaits sequentially** (one off-loop call at a time per loop).
- Concurrent watcher tasks at most: notifications + dispatcher (+ alerts folded into notifications) ≈ **2** in-flight kanban pool jobs, plus agent work sharing the same pool.
- Well under `max_workers=10`; no starvation risk from this migration.

---

## Per-site migration table (21 rows)

Args lists are byte-identical to pre-migration `asyncio.to_thread(...)` call sites (including kwargs on auto-receipt + heartbeat).

| # | Line (post) | Callee | Args parity |
|---|-------------|--------|-------------|
| 1 | 1112 | `save_alert_state` | yes — `(alert_state_path, alert_state)` |
| 2 | 1120 | `self._kanban_flush_stalled_trees` | yes — `(kanban_cfg, _kb)` |
| 3 | 1244 | `_collect` | yes — `()` |
| 4 | 1256 | `self._kanban_advance` | yes — `(sub, d["cursor"], board_slug, d.get("claim_token"))` |
| 5 | 1280 | `self._kanban_rewind` | yes — `(sub, d["cursor"], d.get("old_cursor", 0), board_slug, d.get("claim_token"))` |
| 6 | 1314 | `self._kanban_tree_completion` | yes — `(task, _root_run, ev, board_slug, kanban_cfg)` |
| 7 | 1473 | `_write_auto_receipt` | yes — `(task, board_slug=…, summary=…, status_override="gave_up")` kwargs |
| 8 | 1491 | `self._kanban_checkpoint` | yes — `(sub, delivered_cursor, board_slug, d.get("claim_token"))` |
| 9 | 1514 | `self._kanban_unsub` | yes — `(sub, board_slug)` |
| 10 | 1522 | `self._kanban_rewind` | yes — `(sub, d["cursor"], delivered_cursor, board_slug, d.get("claim_token"))` |
| 11 | 1535 | `self._kanban_advance` | yes — `(sub, d["cursor"], board_slug, d.get("claim_token"))` |
| 12 | 1623 | `self._kanban_unsub` | yes — `(sub, board_slug)` |
| 13 | 2035 | `_evaluate` | yes — `()` |
| 14 | 2795 | `_kb.reap_worker_zombies` | yes — `()` |
| 15 | 2815 | `_fetch_boards` | yes — `()` |
| 16 | 2822 | `_auto_decompose_tick` | yes — `(tick_boards, _ad_per_tick)` |
| 17 | 2833 | `_read_dispatch_caps` | yes — `(_load_config)` |
| 18 | 2889 | `_tick_once` | yes — `(tick_boards)` |
| 19 | 2913 | `_ready_nonempty` | yes — `(tick_boards)` |
| 20 | 2995 | `_backfill_recent_costs` | yes — `()` |
| 21 | 3005 | `_kb.write_kanban_dispatcher_heartbeat` | yes — kwargs `tick_health="ok", boards=tick_boards` |

Helper added on mixin:

```python
async def _kanban_off_loop(self, fn, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(fn, *args, **kwargs)
    return await loop.run_in_executor(self._get_executor(), ctx.run, call)
```

Docstrings/comments that described these call sites now name `_kanban_off_loop` (sole remaining `asyncio.to_thread` string is the intentional comparison in the helper docstring).

---

## Test seam updates

| Site | Change |
|------|--------|
| `_drive_dispatcher_tick` (~978) | Patch `type(runner)._kanban_off_loop` with inline-executing async fake (keeps selfpipe hermeticity) |
| Corrupt-board counter test (~1182) | Count via `_kanban_off_loop`; comment updated |
| Corrupt-board retry test (~1285) | Patch seam; stop-after-N `_tick_once` preserved |
| Notifier dispatch gate (:249) | `patch.object(GatewayRunner, "_kanban_off_loop", …)` same inline semantics |
| Closeout per-board test (same file) | Also moved off bare `asyncio.to_thread` so the gate file stays meaningful |

**New test:** `test_dispatcher_tick_uses_gateway_owned_executor_not_default`

```
tests/hermes_cli/test_kanban_core_skills_gateway.py::test_dispatcher_tick_uses_gateway_owned_executor_not_default PASSED
============================== 1 passed in 0.71s ===============================
EXIT_NEW=0
```

Asserts: `dispatch_once` runs on `hermes-gateway*`, `loop._default_executor is None` after the tick, `_shutdown_executor()` returns.

---

## Gates (exit codes verbatim; no pipes on producers)

### Gate 1 — targeted tests

```
scripts/run_tests.sh tests/hermes_cli/test_kanban_core_skills_gateway.py \
  tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py \
  tests/gateway/test_session_env.py
```

```
=== Summary: 3 files, 69 tests passed, 0 failed (100% complete) in 10.3s (8 workers) ===
EXIT_GATE1=0
```

### Gate 2 — `scripts/run-affected.sh main`

```
=== Summary: 481 files, 9568 tests passed, 2 failed (100% complete) in 158.0s (8 workers) ===
=== 2 files with test failures (2 tests failed) ===
  tests/gateway/test_api_server.py  (1 test failed)
  tests/gateway/test_readiness.py  (1 test failed)
run-affected: first affected-test run failed with exit 1; rerunning once to require reproduced red before hold
… same 2 failures reproduced on re-run …
EXIT_GATE2=1
```

**Attribution:** Both failures assert readiness/health `"status" == "ok"` but get `"degraded"`. They are **not in the migration diff**. Reproduced on pre-change code (stashed our three files, re-ran the two tests — same RED). Pre-existing env/host readiness degradation; out of scope for this task. All kanban watcher / session_env / seam tests green.

### Gate 3 — ruff

```
/home/piet/.hermes/hermes-agent/venv/bin/ruff check gateway/kanban_watchers.py \
  tests/hermes_cli/test_kanban_core_skills_gateway.py \
  tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py
All checks passed!
EXIT_GATE3=0
```

### Gate 4 — import

```
PYTHONPATH=$PWD /home/piet/.hermes/hermes-agent/venv/bin/python -c "from gateway import kanban_watchers"
EXIT_GATE4=0
```

---

## Commit

Single commit message:

```
refactor(gateway): kanban watchers use the gateway-owned executor, not the loop default
```

Body: 3.11 `asyncio.run` teardown joins the default executor unbounded (stuck SQLite busy-timeout parks shutdown); the gateway already owns a bounded `hermes-gateway` pool shut down with `wait=False`; watcher call sites and their test seams now use that pool via `_kanban_off_loop`.
