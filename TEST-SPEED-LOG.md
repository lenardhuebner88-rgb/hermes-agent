# TEST-SPEED-LOG

Branch: `codex/test-suite-speed`
Worktree: `/home/piet/.hermes/hermes-agent/.claude/worktrees/codex-testspeed`
Runner: `scripts/run_tests.sh`

## Round 0 Setup

- 2026-06-26T21:32Z: Coordination checked; no overlap with planned worktree.
- Live checkout status was clean before worktree creation.
- Created isolated worktree from `main`.
- Linked `.venv` to `/home/piet/.hermes/hermes-agent/.venv`.
- Smoke test passed: `scripts/run_tests.sh tests/test_utils_truthy_values.py` -> 4 passed in 0.7s.
- MemSearch unavailable: local Milvus connection failed, so prior-session memory was not used as evidence.

## Baseline

- Full suite baseline: `scripts/run_tests.sh`
  - Runner summary: 1715 files, 36191 tests passed, 6 failed, 100% complete in 951.0s with 8 workers.
  - `/usr/bin/time -p`: real 1063.93s, user 5838.17s, sys 575.26s.
  - Total discovered tests: 36276.
  - Baseline status: RED before any test-speed edits. Operator instructed to treat these as baseline failures if not mine.
- Baseline failure set, pre-existing before speed edits:
  - `tests/hermes_cli/test_kanban_core_functionality.py`: 4 failed, 189 passed.
    - `test_multiple_attempts_preserved_as_runs`
    - `test_stale_run_cannot_complete_new_attempt`
    - `test_stale_run_cannot_block_or_heartbeat_new_attempt`
    - `test_detect_crashed_workers_increments_counter`
  - `tests/hermes_cli/test_web_server_fs.py`: 1 failed, 12 passed.
    - `test_fs_git_root_returns_null_outside_repo`
  - `tests/tools/test_kanban_tools.py`: 1 failed, 101 passed.
    - `test_worker_complete_rejects_stale_run_id`
- Baseline comparison rule for this loop: later full-suite runs must preserve the same known failure set and must not add failures, skips, xfails, or reduce pass counts except by turning baseline failures green.
- Total pass count baseline: 36191 passed, 6 failed.
- Full suite checkpoint after Round 4:
  - Runner summary: 1715 files, 36191 tests passed, 6 failed, 100% complete in 908.7s with 8 workers.
  - `/usr/bin/time -p`: real 1016.45s, user 5605.45s, sys 563.11s.
  - Total discovered tests: 36276.
  - Failure set matched the baseline exactly:
    - `tests/hermes_cli/test_kanban_core_functionality.py`: same 4 failed, 189 passed.
    - `tests/hermes_cli/test_web_server_fs.py`: same 1 failed, 12 passed.
    - `tests/tools/test_kanban_tools.py`: same 1 failed, 101 passed.
  - Delta vs full-suite baseline: 951.0s -> 908.7s runner wall, saving 42.3s (4.4%); real 1063.93s -> 1016.45s, saving 47.48s (4.5%).
  - Coverage proxy: same 36276 discovered tests and same 36191 pass count; no new failures.
- Full suite checkpoint after Round 8:
  - Runner summary: 1715 files, 36191 tests passed, 6 failed, 100% complete in 895.4s with 8 workers.
  - `/usr/bin/time -p`: real 1002.82s, user 5575.29s, sys 582.87s.
  - Total discovered tests: 36276.
  - Failure set matched the baseline exactly:
    - `tests/hermes_cli/test_kanban_core_functionality.py`: same 4 failed, 189 passed.
    - `tests/hermes_cli/test_web_server_fs.py`: same 1 failed, 12 passed.
    - `tests/tools/test_kanban_tools.py`: same 1 failed, 101 passed.
  - Delta vs full-suite baseline: 951.0s -> 895.4s runner wall, saving 55.6s (5.8%); real 1063.93s -> 1002.82s, saving 61.11s (5.7%).
  - Delta vs Round 4 checkpoint: 908.7s -> 895.4s runner wall, saving 13.3s (1.5%).
  - Coverage proxy: same 36276 discovered tests and same 36191 pass count; no new failures.
- Full suite checkpoint after Round 12:
  - Runner summary: 1715 files, 36191 tests passed, 6 failed, 100% complete in 856.4s with 8 workers.
  - `/usr/bin/time -p`: real 963.22s, user 5231.94s, sys 542.28s.
  - Total discovered tests: 36276.
  - Failure set matched the baseline exactly:
    - `tests/hermes_cli/test_kanban_core_functionality.py`: same 4 failed, 189 passed.
    - `tests/hermes_cli/test_web_server_fs.py`: same 1 failed, 12 passed.
    - `tests/tools/test_kanban_tools.py`: same 1 failed, 101 passed.
  - Delta vs full-suite baseline: 951.0s -> 856.4s runner wall, saving 94.6s (9.9%); real 1063.93s -> 963.22s, saving 100.71s (9.5%).
  - Delta vs Round 8 checkpoint: 895.4s -> 856.4s runner wall, saving 39.0s (4.4%).
  - Coverage proxy: same 36276 discovered tests and same 36191 pass count; no new failures.
- Final full suite checkpoint after Round 16:
  - Runner summary: 1715 files, 36191 tests passed, 6 failed, 100% complete in 843.1s with 8 workers.
  - `/usr/bin/time -p`: real 947.54s, user 5244.85s, sys 545.23s.
  - Total discovered tests: 36276.
  - Failure set matched the baseline exactly:
    - `tests/hermes_cli/test_kanban_core_functionality.py`: same 4 failed, 189 passed.
    - `tests/hermes_cli/test_web_server_fs.py`: same 1 failed, 12 passed.
    - `tests/tools/test_kanban_tools.py`: same 1 failed, 101 passed.
  - Delta vs full-suite baseline: 951.0s -> 843.1s runner wall, saving 107.9s (11.3%); real 1063.93s -> 947.54s, saving 116.39s (10.9%).
  - Delta vs Round 12 checkpoint: 856.4s -> 843.1s runner wall, saving 13.3s (1.6%); real 963.22s -> 947.54s, saving 15.68s (1.6%).
  - Coverage proxy: same 36276 discovered tests and same 36191 pass count; no new failures.

## Target Backlog

Known start targets:

- [x] `tests/plugins/test_kanban_dashboard_plugin.py` (~148.793s): module-scope router/app build; reduce `kanban_home` profile dirs to profiles tests actually use.
- [x] `tests/plugins/memory/test_openviking_provider.py` (~93.840s): monkeypatch `_SESSION_DRAIN_TIMEOUT` to ~0.05 in only the two hung-thread tests.
- [x] `tests/hermes_cli/test_kanban_db.py` (~47.725s): tried replacing real `time.sleep(1.05)` with distinct `ended_at` SQL; reverted because the file measured slower.

Slowest files from `test_durations.json` above ~30s:

- [x] `tests/run_agent/test_run_agent.py` (122.313s): audited; no safe bounded win found without risky shared-agent fixture reuse.
- [x] `tests/tui_gateway/test_protocol.py` (66.326s): stubbed heavy plugin/skill discovery modules in this unit-test fixture.
- [x] `tests/hermes_cli/test_web_server.py` (47.025s): audited; no safe bounded win found.
- [x] `tests/tools/test_browser_supervisor.py` (44.616s): replaced fixed page-load sleep with readiness polling.
- [x] `tests/tui_gateway/test_goal_command.py` (43.674s): tried command-discovery stubs; reverted because goal persistence tests failed.
- [x] `tests/hermes_cli/test_kanban_core_functionality.py` (42.504s): baseline-red with known pre-existing failures; skipped as unsafe for speed proof.
- [x] `tests/tui_gateway/test_undo_command.py` (39.881s): stubbed heavy plugin/skill discovery modules in this unit-test fixture.
- [x] `tests/hermes_cli/test_doctor.py` (37.362s): current baseline re-measured at 12.1s, below sweep floor; no action.
- [x] `tests/agent/test_codex_ttfb_watchdog.py` (36.618s): audited; no safe bounded win without weakening real-time watchdog semantics.
- [x] `tests/hermes_cli/test_kanban_boards.py` (36.511s): current baseline re-measured at 27.5s, below sweep floor; CLI subprocess coverage kept.
- [x] `tests/run_agent/test_run_agent_codex_responses.py` (31.638s): current baseline re-measured at 28.9s, below sweep floor; no safe concentrated hotspot.
- [x] `tests/agent/test_memory_async_sync.py` (30.732s): reduced fake wedged-provider delay while preserving bounded-shutdown assertion.
- [x] `tests/hermes_cli/test_kanban_worktrees.py` (30.321s from Round 12 full run): current baseline re-measured at 6.9s, below sweep floor.

## Rounds

### Round 1 — `tests/plugins/test_kanban_dashboard_plugin.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py`
- Baseline result: 250 passed, 0 failed, 1 skipped/251 discovered; runner 101.5s; real 103.11s.
- Change:
  - Reused the dynamically loaded Kanban dashboard plugin module and FastAPI app at module scope instead of rebuilding/importlib-executing them for every test.
  - Preserved prior isolation by clearing `_lane_profile_cache` and `_board_cache` before each test and restoring `sys.modules["hermes_dashboard_plugin_kanban_test"]` to the app's router module so existing monkeypatch-based tests still patch the live endpoint globals.
  - Reduced the function-scoped profile directory seed from 17 profiles to the 14 profiles that dashboard endpoint tests require as on-disk spawnable/profile targets.
- Rejected intermediate attempt: module-scoped app without pinning the router module in `sys.modules` produced 6 failures; fixed before accepting the round.
- Green proof:
  - Run 1: 250 passed, 0 failed, 1 skipped/251 discovered; runner 40.5s; real 42.05s.
  - Run 2: 250 passed, 0 failed, 1 skipped/251 discovered; runner 40.8s; real 42.30s.
- Delta: 101.5s -> 40.8s runner wall, saving 60.7s (59.8%) for this file.
- Coverage proxy: no tests removed/skipped/xfailed; discovered count stayed 251; pass/fail/skip shape stayed 250 passed, 0 failed, 1 skipped; assertions unchanged.

### Round 2 — `tests/plugins/memory/test_openviking_provider.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/plugins/memory/test_openviking_provider.py`
- Baseline result: 103 passed, 0 failed; runner 100.6s; real 101.30s.
- Change:
  - In the two synchronous `_HungThread` `on_session_end` tests only, monkeypatched `plugins.memory.openviking._SESSION_DRAIN_TIMEOUT` from the production default 10.0s to 0.05s.
  - Did not change production code or assertions.
- Green proof:
  - Run 1: 103 passed, 0 failed; runner 80.6s; real 81.28s.
  - Run 2: 103 passed, 0 failed; runner 81.2s; real 82.03s.
- Delta: 100.6s -> 81.2s runner wall, saving 19.4s (19.3%) for this file.
- Coverage proxy: no tests removed/skipped/xfailed; discovered count stayed 103; pass/fail shape stayed 103 passed, 0 failed; assertions unchanged.

### Round 3 — `tests/hermes_cli/test_kanban_db.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py`
- Baseline result: 587 passed, 0 failed; runner 47.4s; real 49.37s.
- Tried change:
  - Replaced the `time.sleep(1.05)` in `test_latest_summary_picks_newest_when_multiple_runs` with a direct SQL `ended_at = int(time.time()) + 1` update for the second run.
- Verification:
  - Run 1: 587 passed, 0 failed; runner 50.3s; real 52.21s.
- Outcome: reverted because the file stayed green with the same test count but measured slower than baseline. No code change kept.

### Round 4 — `tests/run_agent/test_run_agent.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/run_agent/test_run_agent.py`
- Baseline result: 385 passed, 0 failed; runner 113.1s; real 119.59s.
- Audit:
  - `rg` found existing retry sleeps already patched to no-op/fake time in relevant tests.
  - The only direct unpatched sleep found in the file is a 0.1s concurrency ordering delay, too small/noisy to be a meaningful target.
  - The dominant cost appears distributed across many `AIAgent` construction and conversation-loop tests. The shared `agent` fixture is heavily mutated by tests, so widening it to module scope would risk cross-test state leakage and order dependence.
- Outcome: no safe bounded win attempted; no code change made.

### Round 5 — `tests/tui_gateway/test_protocol.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/tui_gateway/test_protocol.py`
- Baseline result: 63 passed, 0 failed; runner 61.2s; real 61.65s.
- Diagnostic:
  - Direct `pytest --durations=20` showed 13 command/slash-dispatch tests each spending ~3.1-3.7s in repeated skill/plugin command discovery/import paths.
- Change:
  - In the file's `server` fixture only, replaced `agent.skill_commands` and `hermes_cli.plugins` with minimal fake modules matching the unit-test contract: no plugin/skill command by default, tests can still patch the same functions for positive cases, and async plugin handler results are resolved like the real helper.
  - Did not change production code or assertions.
- Green proof:
  - Run 1: 63 passed, 0 failed; runner 3.0s; real 3.42s.
  - Run 2: 63 passed, 0 failed; runner 3.0s; real 3.45s.
- Delta: 61.2s -> 3.0s runner wall, saving 58.2s (95.1%) for this file.
- Coverage proxy: no tests removed/skipped/xfailed; discovered count stayed 63; pass/fail shape stayed 63 passed, 0 failed; assertions unchanged.

### Round 6 — `tests/hermes_cli/test_web_server.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/hermes_cli/test_web_server.py`
- Baseline result: 317 passed, 0 failed; runner 33.9s; real 34.96s.
- Audit:
  - Direct `pytest --durations=25` showed the slowest call was the first `/api/status` path at 4.24s, dominated by unavoidable web-server import/startup cost for this file.
  - Remaining cost is spread across many FastAPI `TestClient` endpoint tests and per-test `_isolate_hermes_home` state.
  - The few explicit timing waits are either subprocess/PTY synchronization or negative async assertions; shrinking them would weaken the tests or increase flake risk for a very small gain.
- Outcome: no safe bounded win attempted; no code change made.

### Round 7 — `tests/tools/test_browser_supervisor.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/tools/test_browser_supervisor.py`
- Baseline result: 21 passed, 0 failed, 1 skipped/22 discovered; runner 37.2s; real 37.70s.
- Change:
  - Replaced `_fire_on_page()`'s fixed `asyncio.sleep(1.5)` after data-URL navigation with polling for `document.readyState == "complete"` up to the same 1.5s cap.
  - This preserves the helper's wait-for-loaded-page behavior without paying the full sleep on every helper call.
- Green proof:
  - Run 1: 21 passed, 0 failed, 1 skipped/22 discovered; runner 20.7s; real 21.17s.
  - Run 2: 21 passed, 0 failed, 1 skipped/22 discovered; runner 20.4s; real 20.91s.
- Delta: 37.2s -> 20.4s runner wall, saving 16.8s (45.2%) for this file.
- Coverage proxy: no tests removed/skipped/xfailed; discovered count stayed 22; pass/fail/skip shape stayed 21 passed, 0 failed, 1 skipped; assertions unchanged.

### Round 8 — `tests/tui_gateway/test_goal_command.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/tui_gateway/test_goal_command.py`
- Baseline result: 11 passed, 0 failed; runner 35.2s; real 35.56s.
- Tried change:
  - Added lightweight `agent.skill_commands` / `hermes_cli.plugins` fakes in the `server` fixture, mirroring the safe win in `test_protocol.py`, to avoid repeated command-discovery imports.
  - Narrowed to only `agent.skill_commands` after the broader fake failed.
- Verification:
  - Broad fake run: 6 passed, 5 failed; runner 1.4s; goal persistence assertions failed.
  - Skill-only fake run: 6 passed, 5 failed; runner 35.1s; same goal persistence failures.
- Outcome: reverted because the file became red. No code change kept.

### Round 9 — `tests/hermes_cli/test_kanban_core_functionality.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/hermes_cli/test_kanban_core_functionality.py`
- Baseline result: 189 passed, 4 failed/193 discovered; runner 31.0s; real 32.07s.
- Baseline failures matched the pre-existing accepted full-suite failure set:
  - `test_multiple_attempts_preserved_as_runs`
  - `test_stale_run_cannot_complete_new_attempt`
  - `test_stale_run_cannot_block_or_heartbeat_new_attempt`
  - `test_detect_crashed_workers_increments_counter`
- Outcome: no speed change attempted. This file cannot currently satisfy the per-file green proof required by the speed loop, and changing tests around known production-behavior failures would weaken the verification boundary.

### Round 10 — `tests/tui_gateway/test_undo_command.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/tui_gateway/test_undo_command.py`
- Baseline result: 10 passed, 0 failed; runner 36.2s; real 36.59s.
- Diagnostic:
  - Direct `.venv/bin/python -m pytest --durations=20` showed each `/undo` dispatch test spending about 3.1-3.5s in the call phase.
  - `command.dispatch` tries plugin and skill command discovery before routing built-in pending-input commands like `/undo`.
- Change:
  - In the file's `server` fixture only, replaced `agent.skill_commands` and `hermes_cli.plugins` with minimal fake modules matching the unit-test contract: no plugin/skill command by default and awaitable plugin results are still resolved like the real helper.
  - Stopped reloading `tui_gateway.server` between tests and instead cleared the module's per-test mutable state (`_sessions`, pending maps, answers, and DB cache), matching the established fixture pattern in neighboring TUI gateway tests.
  - Did not change production code or assertions.
- Green proof:
  - Run 1: 10 passed, 0 failed; runner 1.6s; real 1.98s.
  - Run 2: 10 passed, 0 failed; runner 1.5s; real 1.91s.
- Delta: 36.2s -> 1.5s runner wall, saving 34.7s (95.9%) for this file.
- Coverage proxy: no tests removed/skipped/xfailed; discovered count stayed 10; pass/fail shape stayed 10 passed, 0 failed; assertions unchanged.

### Round 11 — `tests/hermes_cli/test_doctor.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/hermes_cli/test_doctor.py`
- Baseline result: 64 passed, 0 failed; runner 12.1s; real 13.08s.
- Outcome: no speed change attempted. The file now measures below the ~30s sweep floor, so pursuing fixture changes here would be outside the high-value target set for this loop.

### Round 12 — `tests/agent/test_codex_ttfb_watchdog.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/agent/test_codex_ttfb_watchdog.py`
- Baseline result: 10 passed, 0 failed; runner 34.9s; real 35.33s.
- Audit:
  - Direct `.venv/bin/python -m pytest --durations=20` showed the slow calls are the watchdog proofs themselves: one 8.08s TTFB-kill case, five ~3.3s kill/idle cases, and four ~2.1-2.2s slow-but-not-killed cases.
  - The test file intentionally uses `HERMES_CODEX_TTFB_TIMEOUT_SECONDS=1`, `HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS=1`, and real 2s sleeps to prove timeout boundaries and no-kill behavior around those thresholds.
  - `agent.chat_completion_helpers.interruptible_api_call` uses literal 0.3s poll joins and 2.0s post-kill join grace; no test-side production constant exists to reduce only the join grace.
- Outcome: no speed change attempted. Lowering the thresholds/sleeps or changing the fake hanging stream to exit differently would weaken the watchdog regression being tested.

### Round 13 — `tests/hermes_cli/test_kanban_boards.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/hermes_cli/test_kanban_boards.py`
- Baseline result: 56 passed, 0 failed; runner 27.5s; real 28.16s.
- Audit:
  - `rg` found no direct real sleeps.
  - Direct `.venv/bin/python -m pytest --durations=20` showed the main cost is `TestCLI::test_per_board_task_isolation_via_cli` at 10.66s, followed by `TestWorkerSpawnEnv::test_default_spawn_sets_env_vars` at 3.41s.
  - The slowest test intentionally verifies the CLI surface with multiple subprocess invocations. Replacing it with direct `kanban_db` calls would reduce coverage of the command dispatcher and board flag integration.
- Outcome: no speed change attempted. The file now measures below the ~30s sweep floor, and the remaining cost is purposeful CLI subprocess coverage.

### Round 14 — `tests/run_agent/test_run_agent_codex_responses.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/run_agent/test_run_agent_codex_responses.py`
- Baseline result: 77 passed, 0 failed; runner 28.9s; real 34.47s.
- Audit:
  - `rg` found the existing retry/backoff sleeps are already patched out by the autouse `_no_codex_backoff` fixture.
  - Direct `.venv/bin/python -m pytest --durations=20` showed cost distributed across many agent-construction and conversation-loop tests; the slowest single test was 1.74s and most top entries were subsecond to ~1s.
  - Reusing agent instances across tests would be unsafe because these tests mutate agent runtime state, transport/client fields, replay settings, conversation history, and monkeypatched methods.
- Outcome: no speed change attempted. The file now measures below the ~30s sweep floor and has no concentrated safe hotspot.

### Round 15 — `tests/agent/test_memory_async_sync.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/agent/test_memory_async_sync.py`
- Baseline result: 6 passed, 0 failed; runner 30.7s; real 31.12s.
- Diagnostic:
  - Direct `.venv/bin/python -m pytest --durations=20` showed the tests themselves finish in ~5.4s, with `test_shutdown_all_is_bounded_with_wedged_provider` spending 5.00s in the call phase.
  - The extra subprocess lifetime came from the fake provider's 30s sleep continuing in the executor thread after `shutdown_all()` correctly returned at the 5s drain timeout.
- Change:
  - Reduced the fake wedged provider delay in `test_shutdown_all_is_bounded_with_wedged_provider` from 30.0s to 10.0s.
  - This still exceeds the production 5s drain timeout and the existing `<8s` assertion still catches an unbounded shutdown, while avoiding 20s of post-test subprocess tail.
- Green proof:
  - Run 1: 6 passed, 0 failed; runner 10.7s; real 11.12s.
  - Run 2: 6 passed, 0 failed; runner 10.7s; real 11.07s.
- Delta: 30.7s -> 10.7s runner wall, saving 20.0s (65.1%) for this file.
- Coverage proxy: no tests removed/skipped/xfailed; discovered count stayed 6; pass/fail shape stayed 6 passed, 0 failed; assertions unchanged.

### Round 16 — `tests/hermes_cli/test_kanban_worktrees.py`

- Baseline command: `/usr/bin/time -p scripts/run_tests.sh tests/hermes_cli/test_kanban_worktrees.py`
- Baseline result: 117 passed, 0 failed; runner 6.9s; real 7.63s.
- Context: this file appeared at 30.32s in the Round 12 full-suite top timings, so it was added to the sweep after the original backlog.
- Outcome: no speed change attempted. The current per-file baseline is well below the ~30s sweep floor.

## No Safe Win

- `tests/hermes_cli/test_kanban_db.py`: SQL timestamp replacement for the one real sleep was behavior-preserving but did not produce a measured speed win in the full file run (47.4s baseline vs 50.3s after), so it was reverted.
- `tests/run_agent/test_run_agent.py`: no clear slow sleep/heavy setup removal found; fixture-scope widening looked unsafe because tests mutate shared `AIAgent` state.
- `tests/hermes_cli/test_web_server.py`: no large repeated setup/sleep target found; reducing waits would weaken PTY/negative async timing assertions, and broadening FastAPI client/HERMES_HOME fixture scope looked unsafe.
- `tests/tui_gateway/test_goal_command.py`: command-discovery stubs made goal persistence tests fail, indicating the import path has required side effects for this test's setup; reverted.
- `tests/hermes_cli/test_kanban_core_functionality.py`: baseline-red with the accepted 4 pre-existing failures, so it cannot provide the all-green per-file speed proof.
- `tests/hermes_cli/test_doctor.py`: current measured baseline is 12.1s, below the ~30s sweep floor.
- `tests/agent/test_codex_ttfb_watchdog.py`: runtime is dominated by real-time watchdog boundary assertions; no safe test-side timeout knob exists.
- `tests/hermes_cli/test_kanban_boards.py`: current measured baseline is 27.5s, below the ~30s sweep floor; the slowest path is CLI subprocess coverage.
- `tests/run_agent/test_run_agent_codex_responses.py`: current measured baseline is 28.9s, below the ~30s sweep floor; cost is distributed across mutable agent tests.
- `tests/hermes_cli/test_kanban_worktrees.py`: current measured baseline is 6.9s, below the ~30s sweep floor.
