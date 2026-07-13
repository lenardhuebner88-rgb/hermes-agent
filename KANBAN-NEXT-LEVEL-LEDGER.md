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
- Test:        Loop-0 gate chain on the resume candidate.
- Change:      NONE
- After:       Resume worktree starts exactly at current `piet-fork/main` `42bbf39bd`; N-01/N-02 were replayed as isolated commits. Current default DB/API census is captured under `audit/`: 4,469 DB tasks, 256 active API cards, 4,213 archived; all 20 authenticated endpoints returned HTTP 200.
- Gates:       `npm ci` exit 0; `scripts/gate-frontend.sh --skip-build` exit 0 (125 files / 1,826 tests); targeted Python 92 passed, exit 0; `scripts/run-affected.sh` 99 passed, exit 0; Ruff exit 0; collection 43,048 selected / 43,107 collected, exit 0.
- Commit:      NONE
- Status:      FIXED

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
- Commit:      `9cb97d401`
- Status:      FIXED

## Iteration 1 — Information completeness and archive truth

### N-10  Complete operator-relevant card information
- Source:      prior F-01 / `t_bda1424a`
- Class:       DATA
- Severity:    S1
- Invariant:   Full assignee, priority, comment count, dependency counts, progress, and material timestamps are discoverable for every card without misleading zeroes or pointer-only access.
- Repro:       Repeat the exhaustive DB/API/DOM card matrix at desktop and mobile widths.
- Before:      Prior 249 × 15 matrix: 217 partial assignees, 417 omitted nonzero values, and 871 omitted non-null timestamps.
- Test:        Fail-first `BoardTab.test.tsx` + `schemas.test.ts`: 2 failed / 50 passed because the card exposed only an initial and the schema stripped `due_at`; after the change 52/52 pass.
- Change:      Preserve `due_at` and `last_heartbeat_at` in the Board schema; add a native named `<details>/<summary>` disclosure per card; expose full assignee plus nonzero priority/comments/dependency/progress metadata without inventing zero values.
- After:       Candidate Vite browser against the authenticated live API: 256 API cards = 256 DOM cards = 256 named disclosures; 1,833 material cells checked at both 1440×900 and 390×844, zero failures, zero console/network errors, body width 1440/1440 and 390/390. Evidence: `audit/iteration-1-f01/`.
- Gates:       Focused Vitest 52 passed, exit 0; `tsc -b --noEmit` exit 0; targeted control lint exit 0; `scripts/gate-frontend.sh --skip-build` exit 0 (126 files / 1,829 tests).
- Commit:      `087f69f2d`
- Status:      CONFIRMED

### N-11  Every truncated value has accessible full-value recovery
- Source:      prior F-02 / `t_0163e3a4`
- Class:       ACCESSIBILITY
- Severity:    S3
- Invariant:   Every ellipsized value in all Fleet surfaces has an accessible full-value recovery path at desktop and mobile widths.
- Repro:       Exercise 400-character, RTL, combining-mark, and emoji titles in all six tabs and drawers.
- Before:      Prior Board title `t_33e2669f` had `scrollWidth=450`, `clientWidth=448`, and no full-value attribute.
- Test:        Initial focused fail-first run: 7 failed / 52 passed across Board, Heute, Ketten, and Risiko because clipped values lacked complete `title` recovery. The first browser candidate then found 11 further live overflows in Board metadata and Risiko triage; targeted regressions failed 2/34 before those paths were fixed.
- Change:      Add complete-value recovery to every known clipped Fleet title, note, model, lane, result, path, activity, and drawer field; preserve the full combined Board metadata line; make string-valued panel metadata recoverable. The audit harness compares hostile fixtures with the title persisted by the authenticated API, so backend whitespace normalization is not misreported as DOM loss.
- After:       Fresh authenticated candidate contexts at 1440×900 and 390×844 exercised all six tabs. Desktop inspected 121 actually clipped elements; mobile inspected 352. Every clip had recovery; 400-character, RTL, combining-mark, and emoji titles matched API-persisted text and DOM/title exactly. Both viewports had zero body overflow, console errors, or HTTP errors. Evidence: `audit/iteration-1-f02/`.
- Gates:       Focused Vitest 60 passed, exit 0; `scripts/gate-frontend.sh` exit 0 (126 files / 1,837 tests plus production build).
- Commit:      `6197d170d`
- Status:      CONFIRMED

### N-12  Archive filtering is complete and explicit
- Source:      prior F-06 / `t_6ce33433`
- Class:       DATA
- Severity:    S1
- Invariant:   Archive views expose total count, loaded count, deterministic paging/search, and truthful empty/partial states without bloating active polling.
- Repro:       Compare read-only DB archive totals with authenticated archive API metadata and rendered DOM.
- Before:      Prior DB had 4,213 archived tasks while Fleet offered `Archiv` and rendered `Keine Treffer` from an active-only response.
- Test:        Backend fail-first returned HTTP 404 for `/board/archive`; frontend/schema fail-first produced 2 failures / 56 passes because archive paging had no schema and selecting `Archiv` only filtered the active payload into `Keine Treffer`.
- Change:      Add a dedicated on-demand `/board/archive` endpoint with literal search, assignee filter, total/filtered/loaded counts, archive timestamps, bounded page size, invalid-cursor rejection, and deterministic `(archived_at, task_id)` keyset cursors. BoardTab fetches it only when `Archiv` is selected, aborts superseded requests, exposes truthful loading/error/empty/count states, and appends unique pages through `Mehr laden`; active `GET /board` polling is unchanged.
- After:       Read-only live DB count was 4,213 archived. The authenticated candidate API returned exactly 4,213 unique ids over 22 cursor pages; first 200-card page was 335,914 bytes / 111.7 ms and the full walk 6,255,372 bytes / 2,536.3 ms. The separate active poll remained 256 cards / 521,056 bytes with zero archived ids and no archive metadata. Candidate DOM rendered 50 then 100 cards, exact task-id search rendered 1/1 while retaining `4,213 insgesamt`, at 1440×900 and 390×844 with no body overflow, console error, or HTTP error. Evidence: `audit/iteration-1-f06/`.
- Gates:       Backend plugin file 268 passed, exit 0; focused frontend/schema 58 passed, exit 0; Ruff exit 0; `tsc -b --noEmit` exit 0; `scripts/gate-frontend.sh` exit 0 (126 files / 1,839 tests plus production build).
- Commit:      this F-06 commit
- Status:      CONFIRMED

## Later iterations

Stale-source, timestamp, state-machine/action, live-event/race, scale/query, hostile-state, red-team, and final release items will be added with concrete evidence as each iteration begins.
