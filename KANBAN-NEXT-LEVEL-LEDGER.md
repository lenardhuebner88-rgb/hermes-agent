# Kanban Next-Level Evidence Ledger

Goal started: 2026-07-12T20:34:25Z
Branch: `codex/kanban-next-level-20260713`
Starting commit: `42bbf39bde1b8eaba5a02f5dd81af9c94b66ab14` (`piet-fork/main`)
Prior audit: `/home/piet/.hermes/worktrees/codex-kanban-adversarial-audit-20260712/KANBAN-AUDIT-LEDGER.md`

This ledger is updated after every iteration. `FIXED` requires the original live repro against the deployed runtime, not only a passing test.

## Iteration 0 — Baseline and release-integrity gates

### N-00  Candidate baseline and deployment identity
- Source:      prior audit
- Class:       RELEASE
- Severity:    S1
- Invariant:   The isolated candidate starts from current `piet-fork/main`, the deployed commit is identified, and all required baseline gates are green before feature work.
- Repro:       Fetch `piet-fork/main`; compare branch ancestry, live checkout HEAD, service process, and served runtime; run the Loop 0 gates.
- Before:      Original worktree started at `d0c79390c`; overnight `piet-fork/main` advanced through terminal integration `626f09f5b` and Dictate changes to `42bbf39bd`. A later Hermes claim still names one dirty terminal test in the original worktree, so that worktree is frozen rather than overwritten.
- Test:        Pending current gate run.
- Change:      NONE
- After:       Resume worktree starts exactly at current `piet-fork/main` `42bbf39bd`; N-01/N-02 were replayed as isolated commits and `npm ci` completed. Full current baseline recapture remains pending.
- Gates:       Resume `npm ci` exit 0; prior collection 43,036 selected / 43,095 collected, exit 0; prior Ruff exit 0; prior targeted Python 90 passed / 2 failed, exit 1; prior frontend 1,805 passed / 1 timed out, exit 1.
- Commit:      NONE
- Status:      OPEN

### N-01  Timeout diagnostics preserve useful endpoint identity without secrets
- Source:      prior final release gate
- Class:       RELEASE
- Severity:    S1
- Invariant:   Delegate timeout diagnostics contain the endpoint scheme/host while redacting credential-bearing paths, queries, and userinfo.
- Repro:       `scripts/run_tests.sh tests/tools/test_delegate_subagent_timeout_diagnostic.py`
- Before:      Current fail-first rerun: 6 passed / 1 failed because the stale test expected the full `https://example.test/v1`; commit `b316b3454` intentionally redacts every non-root path.
- Test:        `scripts/run_tests.sh tests/tools/test_delegate_subagent_timeout_diagnostic.py` failed before the change at the full-URL assertion.
- Change:      Align the regression with the security contract and add a negative assertion that the full path is absent.
- After:       Diagnostic contains `https://example.test/<path redacted>` and does not contain `https://example.test/v1`.
- Gates:       Targeted file 7 passed / 0 failed, exit 0; Ruff on the changed test exit 0.
- Commit:      `1c21a84dc` (replayed from `4b4235836`)
- Status:      FIXED

### N-02  Valid Codex auth satisfies vision requirements
- Source:      prior final release gate
- Class:       RELEASE
- Severity:    S1
- Invariant:   A valid Codex-auth fixture is accepted by vision requirement checks.
- Repro:       `scripts/run_tests.sh tests/tools/test_vision_tools.py`
- Before:      Current fail-first rerun: 84 passed / 1 failed. The fixture combined valid OAuth tokens with `gpt-4o`, which is not in the current Codex OAuth catalog; commit `1cc12db2f` intentionally skips non-Codex main models before attempting auth.
- Test:        `scripts/run_tests.sh tests/tools/test_vision_tools.py` failed before the change at `check_vision_requirements() is True`.
- Change:      Use catalog-valid, vision-capable `gpt-5.4` so the test isolates the promised Codex-auth requirement instead of contradicting the model-routing guard.
- After:       The fixture uses catalog-valid, vision-capable `gpt-5.4`; Codex OAuth client resolution succeeds without weakening the non-Codex-model guard.
- Gates:       Targeted file 85 passed / 0 failed, exit 0; Ruff on the changed test exit 0.
- Commit:      `c846656a9` (replayed from `8acd1eb69`)
- Status:      FIXED

### N-03  Frontend release gate uses deterministic local tooling
- Source:      baseline gate repro
- Class:       RELEASE
- Severity:    S1
- Invariant:   The atomic frontend gate runs the installed worktree `tsc` and `vitest` binaries and bounds Vitest fan-out, independent of ambient `npx` discovery and host CPU count.
- Repro:       `scripts/run_tests.sh tests/scripts/test_gate_frontend.py`; then `scripts/gate-frontend.sh --skip-build`.
- Before:      Fail-first current-main rerun: 6 passed / 1 failed, exit 1; the fake `npx` exited 97 at `npx tsc -b --noEmit`. Prior full-suite attempts also hit EPIPE/SIGTERM and host cgroup PID pressure when Vitest used ambient fan-out.
- Test:        `test_full_gate_uses_local_bins_and_bounded_vitest_workers` replaces `npx` with a fail stub and asserts local `tsc` plus `vitest run --maxWorkers=3` invocations.
- Change:      Resolve required binaries from root/web `node_modules/.bin`; default `GATE_FRONTEND_MAX_WORKERS=4`; execute both binaries directly.
- After:       Targeted gate-script suite 7 passed / 0 failed, exit 0. Full frontend candidate gate completed with 125 files and 1,826 tests passing.
- Gates:       `scripts/run_tests.sh tests/scripts/test_gate_frontend.py` exit 0; `scripts/gate-frontend.sh --skip-build` exit 0 (`125` files, `1,826` tests).
- Commit:      NONE
- Status:      FIXED

## Iteration 1 — Information completeness and archive truth

### N-10  Complete operator-relevant card information
- Source:      prior F-01 / `t_bda1424a`
- Class:       DATA
- Severity:    S1
- Invariant:   Full assignee, priority, comment count, dependency counts, progress, and material timestamps are discoverable for every card without misleading zeroes or pointer-only access.
- Repro:       Repeat the exhaustive DB/API/DOM card matrix at desktop and mobile widths.
- Before:      Prior 249 × 15 matrix: 217 partial assignees, 417 omitted nonzero values, and 871 omitted non-null timestamps.
- Test:        Pending fail-first UI coverage.
- Change:      NONE
- After:       Pending.
- Gates:       Pending.
- Commit:      NONE
- Status:      OPEN

### N-11  Every truncated value has accessible full-value recovery
- Source:      prior F-02 / `t_0163e3a4`
- Class:       ACCESSIBILITY
- Severity:    S3
- Invariant:   Every ellipsized value in all Fleet surfaces has keyboard-accessible full-value recovery at desktop and mobile widths.
- Repro:       Exercise 400-character, RTL, combining-mark, and emoji titles in all six tabs and drawers.
- Before:      Prior Board title `t_33e2669f` had `scrollWidth=450`, `clientWidth=448`, and no full-value attribute.
- Test:        Pending fail-first UI coverage.
- Change:      NONE
- After:       Pending.
- Gates:       Pending.
- Commit:      NONE
- Status:      OPEN

### N-12  Archive filtering is complete and explicit
- Source:      prior F-06 / `t_6ce33433`
- Class:       DATA
- Severity:    S1
- Invariant:   Archive views expose total count, loaded count, deterministic paging/search, and truthful empty/partial states without bloating active polling.
- Repro:       Compare read-only DB archive totals with authenticated archive API metadata and rendered DOM.
- Before:      Prior DB had 4,213 archived tasks while Fleet offered `Archiv` and rendered `Keine Treffer` from an active-only response.
- Test:        Pending fail-first API and UI coverage.
- Change:      NONE
- After:       Pending.
- Gates:       Pending.
- Commit:      NONE
- Status:      OPEN

## Later iterations

Stale-source, timestamp, state-machine/action, live-event/race, scale/query, hostile-state, red-team, and final release items will be added with concrete evidence as each iteration begins.
