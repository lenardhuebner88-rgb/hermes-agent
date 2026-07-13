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
- Commit:      `6fc1bded2`
- Status:      CONFIRMED

## Iteration 2 — Stale-but-plausible data elimination

### N-20  Every retained Fleet source discloses its own freshness failure
- Source:      Loop 2 source/fault matrix
- Class:       DATA
- Severity:    S1
- Invariant:   A failed refresh may retain last-good data only when the consuming surface names the failed source and its age/error; malformed and semantically empty responses must not become plausible fresh defaults.
- Repro:       Load the authenticated candidate, inject 500 across every visible Fleet source, then inject malformed JSON, `{}`, a network reset, a real 30-second hang, and auth expiry; recover each source without reloading. Independently parse `{}` through every Fleet response schema.
- Before:      Fleet exposed one board-level stale badge while retained workers, PlanSpecs, costs, metrics, chain data, live events, release/risk inputs, health, pressure, lanes, and account data could remain visible without a source-local warning. Fail-first schema matrix: 16/17 listed response schemas accepted `{}` and synthesized current-looking empty/default data.
- Test:        Table-driven pollingStore fault/recovery matrix covers 500, malformed JSON, `{}`, 30-second timeout, auth expiry, and WS/network drop. Consumer test binds every source to the subtab that renders it. Schema test asserts all 17 Fleet response contracts reject `{}`.
- Change:      Add one small source-local freshness boundary and bind it to Heute, Worker, Ketten, Plan, Risiko, and PlanSpec cockpit; carry freshness through live-events, chain-graph, chain-cost, and PlanSpec-detail hooks; require one backend-owned identity field in every Fleet response schema instead of accepting an absent top-level payload.
- After:       Authenticated production-build browser proof retained non-empty content while naming every injected source on Heute (4), Worker (5), Ketten (4), Plan (4), and Risiko (8). All five surfaces cleared naturally after recovery, including the selected-chain 30-second cadence. PlanSpec drawer kept the exact last-good goal beside the contract error and cleared it after recovery. Malformed JSON, `{}`, network reset, and a real 30-second hang all disclosed `Systemzustand` with age and recovered; auth expiry redirected to `/login?next=/control/fleet`. Zero unexpected console or HTTP errors. Evidence: `audit/iteration-2-stale/summary.json` plus five desktop screenshots.
- Gates:       Focused Vitest 100 passed, exit 0; `tsc -b --noEmit` exit 0; `scripts/gate-frontend.sh --skip-build` exit 0 (126 files / 1,864 tests); candidate browser harness exit 0.
- Commit:      `8a3348af7`
- Status:      CONFIRMED

## Iteration 3 — Timestamp integrity and live age

### N-30  Kanban time never invents freshness, chronology, or duration
- Source:      timestamp adversarial matrix and five-minute background proof
- Class:       DATA
- Severity:    S1
- Invariant:   Every numeric Kanban timestamp is treated as guarded epoch seconds; invalid units/ranges and impossible chronology are explicit, future values are truthful, negative durations never look positive, Berlin clocks survive both DST boundaries, and retained worker age advances before refocus recovery.
- Repro:       Parse absent, zero, future +1 day, old -400 days, millisecond-shaped, negative, NaN/infinite/non-number, and reversed chronology through the Fleet schemas/formatters; render Board, Worker, detail activity, results and release-event callers; run `audit/verify_timestamps_candidate.py`; freeze the real production-build page for 300 seconds with `audit/verify_background_age_candidate.py`, complete the synthetic worker while frozen, fail the first refocus request, then recover.
- Before:      `fmtAge` clamped future/invalid values to plausible `0s`; negative durations were clamped positive; local-time rendering depended on the host timezone; Worker geometry accepted invalid starts; Activity rendered contaminated event time as `—`; Fleet schemas replaced malformed `checked_at` with local current time or collapsed malformed task/run times to null; Fleet `now` advanced only when unrelated data polls rerendered it. Fail-first tests included 11 derive failures, Board chronology/future failures, NaN Worker geometry, a schema contamination failure, refocus clock `expected 0 to be 300`, Activity missing `Zeit ungültig`, and task/run contamination reduced to null.
- Test:        Focused final matrix: 7 files / 312 tests passed. The new client-clock regression fails without the visibility handler (`expected 0 to be 300`); the claim-expiry regression fails without epoch validation (`expected healthy to be stuck`); detail/schema regressions fail before preservation (`Zeit ungültig` absent and `Number.isNaN(...)` false).
- Change:      Add one `inspectEpochSeconds` contract plus guarded elapsed/relative/clock/duration helpers; preserve contaminated numeric timestamps as NaN while keeping true absence null; remove local-`Date.now()` freshness invention from Fleet response schemas; validate chronology and Worker geometry; use explicit Europe/Berlin formatting; bind Fleet to the shared 10-second client clock and refresh it synchronously on visibility; render invalid Activity/runtime values explicitly.
- After:       Exact production build at 1440×900 and 390×844 shows invalid Board fields and Worker duration explicitly, marks a future due date and chronology violation, emits no NaN geometry, has zero body overflow and zero console errors (`audit/iteration-3-timestamps/summary.json`). Two independent real 300-second freezes passed; the final recorded run waited 301.29 seconds, first refocus frame showed `5 min` and retained-data staleness, then successful recovery removed the completed worker with zero unexpected console errors (`background-summary.json` plus before/after screenshots). Europe/Berlin unit cases cover both 2026 DST transitions.
- Gates:       Focused 312 passed, exit 0; `tsc -b --noEmit` exit 0; `scripts/gate-frontend.sh` exit 0 on the final source (127 files / 1,884 tests plus production build). One preceding loaded full-suite attempt exposed an unrelated Knowledge deep-link timing flake; its isolated rerun was 19/19 and the complete gate rerun was green without a code change.
- Commit:      `d7163f191`
- Status:      CONFIRMED

## Iteration 4 — State-machine and action honesty

### N-40  Review cards expose no deterministically invalid manual transition
- Source:      new live repro against backend transition contract
- Class:       DEFECT
- Severity:    S1
- Invariant:   Fleet never offers a status action whose exact current-state request is guaranteed to fail; review completion and rejection remain owned by the verifier worker's machine-readable verdict path.
- Repro:       On `audit-scratch`, create → claim → `complete_task(review_gate=True)` a short-lived code task; while its authenticated API payload still says `review`, PATCH the same task to `done` and `blocked`; render that captured payload in the exact production candidate drawer and count offered actions; archive the fixture immediately.
- Before:      `stageActions("review")` advertised `Ausliefern` (`PATCH done`) and `Nacharbeit` (`PATCH blocked`), and the NodeDetail drawer deliberately opted into both. Backend regression `test_patch_status_done_rejected_from_review_without_review_done_affordance` and `block_task`'s legal source set prove both targets invalid. Fail-first frontend: 2 failures / 57 tests, with both actions returned instead of `[]` and both buttons present.
- Test:        `vitest ...fleet.test.ts ...TaskActions.test.tsx` failed 2/57 before the change and passed with NodeDetail coverage as 67/67 after it; `tsc -b --noEmit` exit 0.
- Change:      Remove Ship/Rework from the pure stage action model and its drawer escape hatch; add an explicit Verifier-owned guard for `review`; remove review cards from the operator-actionable queue while preserving the valid archive/cancel management action; align hook comments and public props with the backend contract.
- After:       Authenticated candidate proof used short-lived `audit-scratch` task `t_575b0c9a`: captured API status `review`; PATCH `done` returned 409 `not valid from current state`; PATCH `blocked` returned the same 409 class; DOM rendered zero Ship and zero Rework buttons, retained exactly one valid `Abbrechen` action, and had zero unexpected console errors. Fixture was archived immediately. Evidence: `audit/iteration-4-state/review-action-summary.json` and screenshot.
- Gates:       Focused 67 passed, exit 0; `tsc -b --noEmit` exit 0; production `npm run build` exit 0; candidate harness exit 0.
- Commit:      `4211dd6dd`
- Status:      CONFIRMED

### N-41  Unsatisfied dependencies disable Starten before a guaranteed 409
- Source:      new live repro against a blocked parent/child pair
- Class:       DEFECT
- Severity:    S1
- Invariant:   A `todo` or `scheduled` child with any current parent state other than `done` never offers Starten; the drawer names each blocker before submission and fails closed when a link target is missing.
- Repro:       Create a blocked parent and dependent child through `kanban_db.create_task` on `audit-scratch`; fetch authenticated detail/board payloads; attempt `PATCH ready`; render the captured child in the exact production candidate drawer; archive both fixtures.
- Before:      Board/detail exposed only parent IDs/counts. Fleet always rendered Starten for `todo`/`scheduled`, so the known blocked child submitted a request guaranteed to return the backend's 409 `Cannot move to 'ready'`. Fail-first: frontend 4 failures / 84 tests (parent states stripped, Starten still present in direct component and drawer); backend detail expectation lacked `parent_states`.
- Test:        Frontend schema/action/drawer/flow-guard group 89/89, exit 0; backend plugin file 268/268, exit 0.
- Change:      Extend the on-demand detail link contract additively with current `{id,title,status}` states for parents and children; preserve empty defaults for older payloads; derive a fail-closed blocker explanation in NodeDetail; withhold only the invalid stage advance while retaining valid management actions.
- After:       Exact production candidate on port 9122 captured child `t_df095ea9` as `todo` and parent `t_6da86a6e` as `blocked`; authenticated `PATCH ready` returned 409 naming the exact parent; DOM rendered zero Starten buttons, one valid Abbrechen button, and `Starten nicht verfügbar — Vorgänger AUDIT BLOCKING PARENT (blocked) ist nicht fertig.` Zero unexpected console errors; both fixtures archived. Evidence: `audit/iteration-4-state/dependency-action-summary.json` and screenshot.
- Gates:       Focused frontend 89 passed, exit 0; `tsc -b --noEmit` exit 0; backend plugin 268 passed, exit 0; Ruff exit 0; production build exit 0; candidate harness exit 0.
- Commit:      `3927d107b`
- Status:      CONFIRMED

### N-42  Operator questions are verdict-aware API truth, never punctuation guesses
- Source:      Loop-4 real operator-question versus verifier-prose repro
- Class:       DEFECT
- Severity:    S1
- Invariant:   Fleet offers answer/unblock semantics only when the dispatcher's verdict/retry-aware state says human input is required; a question mark in first-pass verifier feedback never creates a false operator action.
- Repro:       On `audit-scratch`, drive one task through coder handoff → review claim → `REQUEST_CHANGES` with a question-mark summary, and park a second task through the sanctioned atomic operator-hold path; compare authenticated board/detail API flags with Risiko DOM; archive both fixtures.
- Before:      Fleet independently mirrored a reduced prose regex and treated every `?` as an operator question. The dispatcher simultaneously classifies first-pass `REQUEST_CHANGES` as retryable, so `Verifier asks: why is this assertion missing?` produced a false `Operator-Frage beantworten` action. Fail-first: 3 frontend assertions and 2 backend API assertions failed.
- Test:        Frontend fleet/schema/Risiko regressions failed 3/152 before the change; backend plugin failed 2/269. Final focused frontend set passed 306/306 and backend plugin passed 269/269.
- Change:      Add one batched DB classifier that reuses the dispatcher's exact verdict, retry-count, task-body-hash, explicit block-kind, and auto-retry-history contract; expose additive `operator_question` booleans on board and detail APIs; make schemas fail closed for older payloads; remove every client-side prose inference from Risiko, drawer, and pending-item derivation.
- After:       Exact production candidate on port 9122 captured the verifier task as `operator_question=false` in both APIs and the real hold as `true`; Risiko rendered zero verifier cards, one operator card, and exactly one answer form. Both fixtures were archived and the browser recorded zero console errors. Evidence: `audit/iteration-4-state/operator-question-summary.json` and screenshot.
- Gates:       Focused frontend 306 passed, exit 0; backend plugin 269 passed, exit 0; Ruff exit 0; `tsc -b --noEmit` exit 0; production build exit 0; candidate harness exit 0. React quality review found no new effect/state coupling, prop mirroring, accessibility regression, or unstable render identity.
- Commit:      `f7c538207`
- Status:      CONFIRMED

### N-43  Run status is lossless and the drawer selects the newest attempt
- Source:      Loop-4 DB status census and candidate DB/API/DOM comparison
- Class:       DATA
- Severity:    S1
- Invariant:   Every non-empty persisted `task_runs.status`/`outcome` survives frontend parsing unchanged, and a surface labelled as the latest run selects the actual newest row from chronological attempt history.
- Repro:       Read the default DB through a read-only SQLite URI; enumerate all distinct run statuses/outcomes; select the newest done task whose latest run lies outside the UI's old seven-value enum; compare detail API, chain-graph API, and production-candidate drawer.
- Before:      The live DB had 16 distinct run statuses and 12 outcomes, but Worker/chain/result schemas accepted only seven statuses and silently rewrote real values such as `completed`, `review`, `reclaimed`, `spawn_failed`, and `transient_retry` to `running` or `done`. Additionally, detail history is intentionally oldest-first while NodeDetail selected `runs[0]`, so task `t_26c016df` displayed its older `blocked` attempt instead of latest run 6754 `completed`. Fail-first: 13/98 focused assertions failed; first browser harness run showed DB/graph `completed` versus DOM `blocked`.
- Test:        Table-driven schema matrix covers all 16 status values currently present in the live DB plus a real non-legacy outcome; drawer regression requires an explicit human label with raw persisted state.
- Change:      Replace closed run status/outcome enums with lossless non-empty string schemas and open TypeScript vocabulary; add explicit German+raw run-state labels; expose latest run status in the drawer; select the last row from the documented oldest-first detail history.
- After:       Read-only census recorded 16 status values and 12 outcomes. Candidate task `t_26c016df` matched as `completed` in DB, detail API's final history row, and chain graph; DOM rendered `Abgeschlossen (completed)` with zero console errors. Evidence: `audit/iteration-4-state/run-state-truth-summary.json` and screenshot.
- Gates:       Focused frontend 98 passed, exit 0; `tsc -b --noEmit` exit 0; production build exit 0; candidate DB/API/DOM harness exit 0. React quality review found no new effect/state coupling or unstable identity; the only render addition is a guarded scalar KV row.
- Commit:      `9b5137de8`
- Status:      CONFIRMED

### N-44  Operator answer and unblock are one atomic transition
- Source:      Loop-4 answer semantics and real two-tab mutation race
- Class:       RACE
- Severity:    S1
- Invariant:   Answering an operator question either persists the operator comment and releases the current hold together, or writes neither; stale/verifier/archived state never receives a partial answer.
- Repro:       Create two sanctioned operator holds on `audit-scratch`; answer the first through the production DOM; archive the second from another authenticated browser tab after the answer form is already open, then submit the stale form; verify DB/API/DOM and comments.
- Before:      `useAnswerQuestion` issued POST comment → PATCH ready → POST dispatch as three requests. A second-tab state change between the first two left an operator comment on a task that was never successfully answered/unblocked. Fail-first: frontend still made three calls; all three new backend endpoint tests returned 404.
- Test:        Backend covers successful answer, verifier `REQUEST_CHANGES` prose rejection with zero comments, and archived second-tab loss with zero comments. Frontend locks one atomic POST followed only by the best-effort dispatch tick.
- Change:      Add a single `BEGIN IMMEDIATE` DB transition that rechecks verdict-aware eligibility, clears a defensive stale run pointer, dependency-gates the resulting status, writes the operator comment, and emits comment/unblock events atomically; expose POST `/tasks/{id}/answer`; reduce the hook to this transition plus a subsequent dispatch tick.
- After:       Exact candidate on port 9123 returned 200/`ready` and persisted exactly one operator comment for `t_58e2ac22`. A second tab archived `t_8754c1c7`; its stale submit returned 409 visibly, left status `archived`, and wrote zero comments. One expected 409 console line, zero unexpected console errors. Both fixtures ended archived. Evidence: `audit/iteration-4-state/atomic-answer-summary.json` and screenshot.
- Gates:       Frontend AnswerQuestion 3 passed, exit 0; backend plugin 272 passed, exit 0; Ruff exit 0; `tsc -b --noEmit` exit 0; production build exit 0; candidate two-tab harness exit 0.
- Commit:      `cc88a5a3e`
- Status:      CONFIRMED

### N-45  A vanished open task is retained only with source-local stale disclosure
- Source:      Loop-4 vanished-task and second-tab mutation matrix
- Class:       DATA
- Severity:    S1
- Invariant:   When an open task disappears, the drawer may retain last-good detail only while visibly naming the failed Task-Detail source and retained-data age; it never remains plausible current.
- Repro:       Open a completed `audit-scratch` task in the exact production drawer, hard-delete it through the sanctioned authenticated endpoint from a second tab, wait for the drawer's own 8-second poll, and compare API 404 with retained DOM.
- Before:      `useTaskBodyOnDemand` correctly retained last-good data and surfaced error/stale metadata, but `NodeDetailContent` never consumed that metadata. A deleted task therefore remained as a plausible live detail card. Fail-first NodeDetail regression could not find `Task-Detail` or the 404 disclosure.
- Test:        Component regression retains the exact prior task title while requiring a source-local warning whose title carries the 404. Candidate harness performs a real DELETE and waits for the independent drawer poll.
- Change:      Bind the existing small `FleetSourceFreshness` boundary to the Task-Detail polling source inside the drawer; no new fetch, timer, or duplicated state.
- After:       Candidate task `t_e8837403` was deleted from a second tab (200), its next detail request returned 404, and the still-open drawer rendered the retained body only alongside `Task-Detail · Daten von vor 8s`. One expected 404 console line, zero unexpected errors. Evidence: `audit/iteration-4-state/vanished-task-summary.json` and screenshot.
- Gates:       NodeDetail 13 passed, exit 0; `tsc -b --noEmit` exit 0; production build exit 0; candidate vanished-task harness exit 0. React quality review confirms reuse of existing polling metadata and presentation boundary without new effects or mirrored state.
- Commit:      `dd9a4234f`
- Status:      CONFIRMED

### N-46  Running tasks never offer an impossible profile reassignment
- Source:      Loop-4 offered-action matrix against backend transition guards
- Class:       DEFECT
- Severity:    S2
- Invariant:   The drawer never offers a profile change for a task with an active run when its request contract deliberately sends `reclaim_first=false`; no visible button may deterministically return 409 in the current state.
- Repro:       On `audit-scratch`, create and claim a real `running` task, attach the audit process as its worker PID, open the production candidate drawer, compare the offered controls with authenticated POST `/tasks/{id}/reassign` using the exact UI payload, then archive the fixture.
- Before:      DB and detail DOM both showed `running`; the drawer exposed one `Zielprofil` select and one `Profil ändern` button. The exact UI payload returned HTTP 409: `still running (pass reclaim_first=true ...)`. Evidence: `audit/iteration-4-state/running-reassign-before-summary.json` and screenshot.
- Test:        `npm run test -- --run src/control/views/fleet/NodeDetailDrawer.test.tsx` failed 1/14 before the change because the running drawer still rendered the select; the same file passed 14/14 after the change.
- Change:      Treat `running` as non-reassignable in the existing drawer control boundary. The UI does not silently reclaim a live worker; explicit worker recovery remains on the worker control surface.
- After:       On the rebuilt production candidate, the same DB/API state still returned the truthful backend 409 for a forced direct request, while the drawer rendered zero profile selects and zero reassign buttons. Lanes API exposed 10 profiles, proving the absence was state-driven rather than missing catalog data. Zero unexpected console errors. Evidence: `audit/iteration-4-state/running-reassign-after-summary.json` and screenshot.
- Gates:       Targeted NodeDetail 14 passed, exit 0; complete frontend gate 127 files / 1,903 tests, TypeScript, lint, and production build exit 0; candidate DB/API/DOM harness before and after exit 0.
- Commit:      `7e7ae92e8`
- Status:      CONFIRMED

## Later iterations

Operator-directed stopping point on 2026-07-13: the branch is a clean local checkpoint after N-46. Loop 4's complete nine-status/action matrix and Loops 5-8 (live-event races, scale/query measurements, hostile states, and three fresh red-team passes) remain intentionally open. The atomic final release gate was not run, so this branch was not pushed or deployed and the three default-board tracking cards remain open.
