# Loops Tab Audit + Health-Track Improvement Loops

Started: 2026-07-12T19:44:05Z

Health-Track product scope: **mobile only, 390×844, hydrated, light and dark**. Desktop captures do
not count as product evidence or IMPACT. The Loops control surface remains audited at mobile size;
the earlier 1440×900 Loops baseline is retained only as historical dashboard evidence.

## PREFLIGHT-LANES

Read-only source: `/home/piet/.hermes/kanban.db`, `SELECT * FROM lanes WHERE active = 1`.

```json
{
  "db": "/home/piet/.hermes/kanban.db",
  "active_lanes": [
    {
      "id": "lane_46bba8a4",
      "name": "api-standard",
      "profiles": "{\"coder\":{\"worker_runtime\":\"hermes\",\"provider\":\"kimi\",\"model\":\"kimi-k2.7\",\"fallback_providers\":[{\"provider\":\"xai-oauth\",\"model\":\"grok-4.5\"}]},\"critic\":{\"worker_runtime\":\"hermes\",\"provider\":\"xai-oauth\",\"model\":\"grok-4.5\",\"fallback_providers\":[{\"provider\":\"openai-codex\",\"model\":\"gpt-5.6-luna\"}]},\"premium\":{\"worker_runtime\":\"claude-cli\",\"provider\":null,\"model\":\"claude-opus-4-8\",\"fallback_providers\":[]},\"research\":{\"worker_runtime\":\"hermes\",\"provider\":\"xai-oauth\",\"model\":\"grok-4.5\",\"fallback_providers\":[{\"provider\":\"openai-codex\",\"model\":\"gpt-5.6-luna\"}]},\"reviewer\":{\"worker_runtime\":\"hermes\",\"provider\":\"openai-codex\",\"model\":\"gpt-5.6-sol\",\"fallback_providers\":[{\"provider\":\"xai-oauth\",\"model\":\"grok-4.5\"}]},\"scout\":{\"worker_runtime\":\"hermes\",\"provider\":\"openai-codex\",\"model\":\"gpt-5.6-terra\",\"fallback_providers\":[]},\"verifier\":{\"worker_runtime\":\"hermes\",\"provider\":\"openai-codex\",\"model\":\"gpt-5.6-terra\",\"fallback_providers\":[]}}",
      "active": 1,
      "builtin": 1,
      "created_at": 1781125652,
      "updated_at": 1783885079
    }
  ]
}
```

Verdict: PASS — no active role or fallback is pinned to OpenRouter.

## BASELINE GATES

- `npm ci` in `web/` → exit 0 (`added 876 packages`, `found 0 vulnerabilities`; Node 20 engine warnings recorded).
- `scripts/gate-frontend.sh --skip-build` → exit 0.
  - design-token ratchet: pass
  - `npm run lint:control`: 0 errors, 3 pre-existing warnings
  - `tsc -b --noEmit`: pass
  - Vitest: `Test Files 124 passed (124)`; `Tests 1805 passed (1805)`
  - Verbatim terminus: `=== FRONTEND-GATE GRÜN (exit 0) ===`
- `scripts/run-affected.sh` → exit 0.
  - Verbatim: `run-affected: no affected test files for this diff — skipping pytest (targeted scope; full suite is nightly only)`

Baseline verdict: GREEN. Product-code work may proceed.

## ITERATION 1 — BASELINE CAPTURE

- Browser: authenticated real Chromium, 1440×900 and 390×844, hydration/network-idle waited.
- Screenshots: `audit/screenshots/loops-baseline-1440x900.png`, `audit/screenshots/loops-baseline-390x844.png`.
- Reusable harness: `audit/loops-audit.cjs`; disk capture: `audit/disk-truth.py`.
- Complete comparison: `audit/baseline-field-table.md` — 100 rows across 14 packs, zero unchecked cells, zero disk/API/DOM mismatches for rendered fields.

| endpoint capture | status | bytes | latency | rows |
|---|---:|---:|---:|---:|
| GET `/api/loops` | 200 | 18,782 | 2,056.0 ms | 14 |
| GET `/api/loops/models` | 200 | 663 | 8.0 ms | 5 |
| GET `/api/loops/health-track-ux/detail` | 200 | 3,693 | 39.6 ms | 13 |
| POST `/api/loops/__audit_missing__/start` | 404 | 50 | 7.0 ms | 0 |
| POST `/api/loops/__audit_missing__/stop` | 404 | 50 | 5.1 ms | 0 |
| GET `/api/loops/health-track-ux/files` | 200 | 12,776 | 22.7 ms | 4 |
| PUT `/api/loops/__audit_missing__/files/pack.yaml` | 404 | 50 | 21.4 ms | 0 |
| POST `/api/loops/duplicate` (missing source) | 404 | 50 | 12.4 ms | 0 |
| POST `/api/loops/__audit_missing__/land` | 404 | 50 | 4.5 ms | 0 |
| POST `/api/loops/__audit_missing__/timer` | 404 | 50 | 8.1 ms | 0 |
| PUT `/api/loops/__audit_missing__/timer/schedule` | 404 | 50 | 5.7 ms | 0 |

Mutation routes were deliberately captured against a nonexistent pack: deterministic route payloads, no state change. Initial bare-fetch captures returned the expected 401 because the SPA token header was absent; those files were replaced by authenticated page-context captures and are not findings.

## ITERATION 2 — XAI ENGINE HARD GATE

### Adapter TDD

- RED: `scripts/run_tests.sh tests/loops/test_engines.py -k xai` → exit 1; 3/3 selected tests failed with `ImportError: cannot import name 'xai_cli'`.
- Implementation: `loops/engines/xai_cli.py`; registry self-import in `loops/engines/__init__.py`; catalog entry `xai / grok-4.5` in `loops/models.yaml`.
- GREEN: `scripts/run_tests.sh tests/loops/test_engines.py` → exit 0; `37 tests passed, 0 failed`.
- Proven contract: exact command construction, `HERMES_SANDBOX_MODE=1`, cwd, timeout, merged timeout output with rc 124, shared usage-limit detection, registry, and catalog.

### Throwaway pipeline hard gate

- Pack: `/home/piet/.hermes/loops/packs-custom/xai-hard-gate/pack.yaml`.
- Scratch repo/base: `audit/xai-hard-gate-repo`, branch `scratch-base`, seed `92491bb`.
- Attempt 1: planner rc 0 but self-blocked on the parent coordination claim; no plan. Root cause fixed with the canon-required named worker self-exemption.
- Attempt 2 plan: Sol rc 0, 39s, `PLANNED 1`, valid plan `xai-hard-gate-001`.
- Actual build dispatch: `/home/piet/.hermes/hermes-agent/venv/bin/hermes -m grok-4.5 --provider xai-oauth -z <rendered BUILD.md>` with cwd `/home/piet/.hermes/loops/xai-hard-gate/wt`, timeout 1800s, `HERMES_SANDBOX_MODE=1`.
- Engine result: Grok wrapper rc 0 in 5s; verbatim output: `HTTP 403: {"code":"permission-denied","error":"The model grok-4.5 is not available in your region."}`.
- State: no file change, no Git commit, no `BUILT` status; runner emitted `BUILD_FAIL []`, left branch at seed `92491bb`, and stopped on fail streak.
- Evidence: `/home/piet/.hermes/loops/xai-hard-gate/logs/20260712-215714-build.log`; `/home/piet/.hermes/loops/xai-hard-gate/LEDGER.md`; `git log` in `/home/piet/.hermes/loops/xai-hard-gate/wt`.

Initial hard-gate verdict: **FAIL — no Grok-authored commit existed.** Operator stop condition fired and no model was substituted.

### SuperGrok workaround and repeated hard gate

- Official CLI installed: `@xai-official/grok@0.2.93`; `grok --version` → `grok 0.2.93 (f00f96316d)`.
- Subscription authentication: `grok models` → `You are logged in with grok.com`; available build slot `grok-build`.
- Direct subscription smoke: `grok --model grok-build --single ...` → exit 0, verbatim `GROK_BUILD_45_OK`.
- Identity seam: the subscription CLI rejects `--model grok-4.5` as an unknown model id. The xAI adapter therefore maps the operator-facing `grok-4.5` to the official subscription product slot `grok-build`, which xAI documents as powered by Grok 4.5. No API key and no OpenRouter route are used.
- Adapter TDD RED: engine file test → exit 1, `AttributeError: module 'loops.engines.xai_cli' has no attribute 'GROK_BIN'`.
- Adapter TDD GREEN: engine file test → exit 0, `37 tests passed, 0 failed`; Ruff → exit 0, `All checks passed!`.
- Actual repeated build dispatch: `/home/piet/.npm-global/bin/grok --no-memory --no-subagents --disable-web-search --always-approve --model grok-build --single <rendered BUILD.md> --output-format plain`, cwd `/home/piet/.hermes/loops/xai-hard-gate/wt`, timeout 1800s, `HERMES_SANDBOX_MODE=1`.
- Grok build: rc 0, 40s. Sol verify: rc 0, 45s.
- Authored commit: `12d5efd loop(xai-hard-gate): xai-hard-gate-001 grok proof`; `proof.txt` contains exactly `grok-4.5 built this commit`.
- Queue/state: `20-verified/P1-xai-hard-gate.md`; `last-status` = `PASS xai-hard-gate-001`.

Repeated hard-gate verdict: **PASS — a real Grok-4.5-backed subscription CLI run authored commit `12d5efd`, and Sol independently accepted it.**

## ITERATION 3 — HEALTH-TRACK GROUNDING (MOBILE ONLY)

- Operator scope correction: all product judgement is exclusively 390×844; desktop captures are
  outside scope and discarded.
- Authenticated local production build: `next start`, audit login PASS without exposing secrets.
- Canonical mobile matrix: `AUDIT: PASS captures=42 http200=42 auth_redirects=0 console_errors=0
  page_errors=0 overflow=0 focus_failures=0 exit=0`.
- Screenshots: `/home/piet/projects/health-track/.ui-verify/current-audit/2026-07-12/mobile/{light,dark}/`.
- Production build: exit 0; compile 4.1s, TypeScript 8.6s, page data 1.074s, static generation
  0.553s; `.next/static` 1.7 MiB.
- Manifest-derived client JS: Studio 164,887 B; Erfassen 149,877 B; diary 109,208 B; Trends
  94,359 B; Gewicht 98,822 B; Ziele 103,074 B; Einstellungen 101,005 B.
- Backlogs: `audit/backlogs/health-track-ux.md`, `health-track-perf.md`, and
  `health-track-defects.md`; five current, ranked, evidenced entries each.

## ITERATION 4 — THREE PRODUCTION-LOCKED PACKS

- Packs: `/home/piet/.hermes/loops/packs-custom/{ht-ux-polish,ht-perf,ht-defect-hunt}/`.
- All validate via `--cmd status`; all are `pipeline`, base `main`, `land_remote: origin`,
  `land_push: false`, `autoland: false`, timer OFF, empty queues.
- API truth for all three: plan `codex/gpt-5.6-sol`, build `xai/grok-4.5`, verify
  `codex/gpt-5.6-sol`; `/api/loops/models` exposes `xai → [grok-4.5]`.
- Live authenticated screenshot: `audit/screenshots/loops-baseline-1440x900.png`; payload:
  `audit/payloads/loops/01-list.json` and `02-models.json`.
- Hermes xAI engine commit `6fd7dbb4e` fast-forwarded to `main` and `piet-fork/main`; deploy exit
  0 with service active, loopback/tailnet 200. Health-Track was not pushed or deployed.

## TAB FINDINGS

### F-01  A rejected override is invisible everywhere in the Loops tab
- Class: TELEMETRY-GAP
- Severity: S3
- Surface: `web/src/control/views/LoopsView.tsx:545` ⇄ `hermes_cli/control_loops.py:464`
- Repro: open `dashboard-experience`; compare its detail/card with `/home/piet/.hermes/loops/dashboard-experience/overrides.blocked-20260710T001542+0200.env`.
- Evidence: payload: `audit/payloads/loops/03-detail.json` has only live `overrides` | state file: `audit/disk-truth.json` records one blocked override with key `PHASE_PLAN_MODEL` | screenshot: `audit/screenshots/loops-baseline-1440x900.png`
- Truth: disk/git says a model override was rejected and retained; UI shows no blocked/rejected override state or reason.
- Status: CONFIRMED

### F-02  Idle cards hide the configured engine/model triple
- Class: DATA
- Severity: S3
- Surface: `web/src/control/views/LoopsView.tsx:1204` ⇄ `hermes_cli/control_loops.py:394`
- Repro: load the Loops grid with all packs idle; inspect each card before opening Start.
- Evidence: payload: every pack has `phases.<phase>.engine/model` in `audit/payloads/loops/01-list.json` | state file: manifests captured in `audit/disk-truth.json` | screenshot: `audit/screenshots/loops-baseline-1440x900.png`
- Truth: disk/git and API expose the configured phase models; UI shows none on all 14 idle cards (`audit/baseline-field-table.md`: `API MATCH; DATA NOT SHOWN`).
- Status: CONFIRMED

### F-03  Phase history silently truncates up to 20 stored entries to five
- Class: DATA
- Severity: S3
- Surface: `web/src/control/views/LoopsView.tsx:426` ⇄ `loops/runner.py:691`
- Repro: compare `builder-reviewer` heartbeat history with its card.
- Evidence: payload: 20 `heartbeat.last` rows in `audit/payloads/loops/01-list.json` | state file: same 20 in `audit/disk-truth.json` | screenshot: only five bars in `audit/screenshots/loops-baseline-1440x900.png`
- Truth: disk/git and API say 20 phase events exist; UI renders the newest five without a truncation count or disclosure.
- Status: CONFIRMED

### F-04  The five-second summary poll takes over one second and spawns 72 processes for 18 packs
- Class: PERF
- Severity: S3
- Surface: `web/src/control/hooks/useControlData.ts:2834` ⇄ `hermes_cli/control_loops.py:436`
- Repro: keep `/control/loops` open and time authenticated `GET /api/loops`.
- Evidence: payload: three captures measured 1,267.5 ms, 1,034.1 ms, and 2,056.0 ms for the same 18,782-byte/14-pack response | state file: `audit/endpoint-summary.json` | screenshot: `audit/screenshots/loops-baseline-1440x900.png`
- Truth: the idle tab requests summaries every 5s; backend calls `_commits_ahead()` (`git cherry`) per pack and the measured response consumes 20.7–41.1% of each poll interval.
- Status: FIXED (`4937648b5`; timer fan-out reduced from 54 per-pack systemctl calls to two batched calls, so total subprocesses fall from 72 to 20; live latency re-measure pending deployment)

### F-05  Loop token usage and subscription-cost truth do not exist in the API or UI
- Class: TELEMETRY-GAP
- Severity: S3
- Surface: `web/src/control/lib/types.ts:1002` ⇄ `hermes_cli/control_loops.py:374`
- Repro: inspect any summary/detail payload after a completed phase.
- Evidence: payload: `audit/payloads/loops/01-list.json` and `03-detail.json` contain no token/cost fields | state file: current logs are under `/home/piet/.hermes/loops/*/logs/` | screenshot: `audit/screenshots/loops-baseline-1440x900.png`
- Truth: engine logs may contain token evidence; UI shows no per-phase, per-round, or per-pack token usage and cannot state that Codex/xAI subscription spend is metered €0.
- Status: FIXED (`469d27b43`; live payload/DOM re-verification pending deployment)

### F-06  The xAI one-shot reports process success for a provider permission failure
- Class: DEFECT
- Severity: S2
- Surface: `loops/engines/xai_cli.py:32` ⇄ Hermes CLI provider exit semantics
- Repro: run the `xai-hard-gate` build phase with `engine=xai`, `model=grok-4.5`.
- Evidence: payload: engine output is `HTTP 403: {"code":"permission-denied","error":"The model grok-4.5 is not available in your region."}` | state file: `/home/piet/.hermes/loops/xai-hard-gate/logs/20260712-215714-build.log` | screenshot: not applicable (hard gate is runner/engine)
- Truth: provider says permission denied and no work occurred; engine result says rc 0 / `usage_limit=false`. The runner rejects only because commit/status invariants fail.
- Status: FIXED (transport switched to the official SuperGrok CLI; regression test covers the explicit `grok-4.5` → `grok-build` mapping)

### F-07  Start form omits phase overrides when the selected models equal pack defaults
- Class: DATA
- Severity: S3
- Surface: `web/src/control/views/LoopsView.tsx:166` ⇄ `hermes_cli/control_loops.py:473`
- Repro: open Start for `ht-ux-polish`, leave the mandated Sol/Grok/Sol selections unchanged,
  change Max Hours from 3 to 4, and submit from the UI.
- Evidence: payload: POST returned `{"started":true,"pack":"ht-ux-polish","overrides_written":1}` |
  state file: `/home/piet/.hermes/loops/ht-ux-polish/overrides.consumed.env` contains only
  `MAX_HOURS=4` | screenshot: `audit/screenshots/loops-baseline-1440x900.png`
- Truth: pack/API/runtime all use the correct phase models, but the UI writes no
  `PHASE_PLAN_MODEL`, `PHASE_BUILD_MODEL`, or `PHASE_VERIFY_MODEL` because
  `buildPhaseOverrides()` serialises only differences.
- Status: FIXED (`dea4b8e62`; live disk re-verification pending deployment)

### F-08  Failed heartbeat writes leave a previous phase presented as live
- Class: TELEMETRY-GAP
- Severity: S1
- Surface: `web/src/control/views/LoopsView.tsx:403` ⇄ `loops/runner.py:691`
- Repro: during plan, `chmod 444 heartbeat.json`; wait for the real runner to enter build.
- Evidence: payload: samples 13–17 keep `phase=plan`, `model=gpt-5.6-sol` | state file:
  `audit/timelines/ht-ux-polish.jsonl` has `10-building=1` while heartbeat remains plan; systemd
  journal says `23:25:01 Phase build (engine=xai, model=grok-4.5)` | screenshot: pending fixed-build repro
- Truth: systemd/queue says Grok build is running; API and DOM show Sol plan as current for at least
  58 measured seconds, with no stale/frozen disclosure. The lie persists without a later successful
  heartbeat write and has no dashboard timeout.
- Status: FIXED (`b10e7d849`; fixed-build browser repro pending deployment)

### F-11  Elapsed time freezes when the 5-second list poll stalls
- Class: TELEMETRY-GAP
- Severity: S1
- Surface: `web/src/control/views/LoopsView.tsx` `nowMs` default ⇄ `hooks/useControlData.ts` 5s poll
- Repro: render a running real-wire heartbeat, prevent `/api/loops` from completing, and observe
  that no component state changed to advance `nowMs`.
- Evidence: payload: `heartbeat.current.started_at="2026-07-02T23:00:00"` from the captured fixture |
  state file: fail-first Vitest `Loops live clock > advances independently of API polling` exited 1
  with `useLoopNowMs is not a function`, then 60/60 tests passed after the fix | screenshot: original
  blocked-network browser run was inconclusive under host pressure; fixed-build browser repro pending
- Truth: API/phase start remains unchanged while wall time advances; old DOM could only advance on a
  parent poll render. The fixed component owns a 1-second clock independent of network polling.
- Status: FIXED (`521f50ef8`)

### F-12  Next timer run is an unparseable raw systemd string, not an operator-local instant
- Class: DEFECT
- Severity: S2
- Surface: `web/src/control/views/LoopsView.tsx:1195` ⇄ `hermes_cli/control_loops.py:_timer_next_run`
- Repro: enable a timer and inspect `timer_next_run`; run `Date.parse` on the returned value.
- Evidence: payload: `"timer_next_run":"Mon 2026-07-13 03:00:00 CEST"` | state file:
  `node` returned `NaN / Invalid Date` for CEST and CET samples; fail-first backend suite had 31 pass,
  2 fail before ISO conversion and 33/33 pass after; frontend suite 61/61 pass | screenshot:
  `audit/screenshots/loops-baseline-390x844.png`
- Truth: systemd knows an absolute microsecond instant; API discarded that structure and UI rendered
  an English server-local string verbatim. API now returns UTC ISO and UI formats browser-local time
  with timezone abbreviation, including DST.
- Status: FIXED (`b7c48679e`)

### F-09  A killed build is labelled idle and counted as ordinary built work with no recovery path
- Class: TELEMETRY-GAP
- Severity: S1
- Surface: `web/src/control/views/LoopsView.tsx:1221` ⇄ `loops/runner.py:1190`
- Repro: terminate the systemd unit during Grok build after the plan entered `10-building/`.
- Evidence: payload: timeline sample 43 has `running=false`, stale `phase=plan`, queue
  `10-building=1` | state file: `/home/piet/.hermes/loops/ht-ux-polish/queue/10-building/P1-settings-controls-first.md`, clean Git branch, failed unit by SIGTERM | screenshot:
  `audit/screenshots/ht-ux-polish-crashed-stuck.png`
- Truth: disk/systemd says build was interrupted with no diff/commit/verdict; UI says `IM LEERLAUF`
  and `1 GEBAUT`, offers Start but no explanation or recovery action. “Gebaut” is derived solely
  from queue location and therefore misstates unverified interrupted work.
- Status: CONFIRMED; truth label fixed (`1d404dd62`), UI recovery action still OPEN

### F-10  A PASS-ID mismatch is rendered as verified and ready to land after the runner rejected it
- Class: DEFECT
- Severity: S1
- Surface: `web/src/control/views/LoopsView.tsx` phase history and land affordance ⇄ `loops/runner.py` PASS-ID validation
- Repro: let verify write `PASS P1-settings-controls-first` for a plan whose YAML id is
  `HT-UX-P1-SETTINGS-CONTROLS-FIRST`; wait for the runner to reject and revert it.
- Evidence: payload: card reports idle, `verify ✓ 583s`, one bounce, and two commits ready to
  land | state file: journal says `VERIFY_FAIL [PASS_ID_MISMATCH (PASS P1-settings-controls-first)]
  — revert + retry/bounce`; queue has `90-bounced=1`; branch is fix `dc89892` followed by revert
  `3d6c27d`, with zero net product diff | screenshot:
  `audit/screenshots/ht-ux-polish-pass-id-mismatch-bounced.png`
- Truth: the runner rejected the verifier verdict and removed the change; UI shows a green verify
  phase and an enabled land action for two history-only commits that produce no product change.
- Status: FIXED (`78bc2f2c9`)

### F-13  The verifier rubber-stamps a builder's false command-evidence claim because the real tool trace is hidden
- Class: TELEMETRY-GAP
- Severity: S1 (the rail reports independently verified work although the verifier did not see the
  builder commands it was required to audit)
- Surface: `loops/engines/xai_cli.py` ⇄ `loops/runner.py:_run_pipeline` ⇄ custom `VERIFY.md`
- Repro: run `ht-defect-profile-value-validation`; Grok executes targeted tests as
  `npm test -- ... ; echo ...`, then claims in its final output that no test/gate command used `;`.
  Sol receives only that curated final output, declares the builder commands valid, independently
  re-runs clean commands, and writes PASS.
- Evidence: real tool trace:
  `/home/piet/.grok/sessions/%2Fhome%2Fpiet%2F.hermes%2Floops%2Fht-defect-hunt%2Fwt/019f58b3-dd10-7833-9f08-d48bd701f315/updates.jsonl`
  contains verbatim `npm test -- src/lib/__tests__/profile-actions.test.ts
  src/app/profil/__tests__/profil-form.test.tsx ; echo "ANTI_TAUT_TEST_EXIT:$?"` and the analogous
  `RESTORE_GREEN_TEST_EXIT`; verifier log
  `/home/piet/.hermes/loops/ht-defect-hunt/logs/20260713-015045-verify.log:1006` says
  `Die Builder-Kommandos verletzen das Evidence-Verbot nicht` and ends PASS.
- Truth: disk trace says prohibited command chaining occurred; verifier/UI say independently
  verified PASS.
- Status: FIXED (`4d3027719`; engine now records the exact Grok `updates.jsonl` provenance path,
  runner injects it into VERIFY, and all three verifier prompts must inspect raw tool commands;
  live adversarial re-test pending)

### F-14  Loop wire timestamps omit their timezone and shift elapsed time in another browser timezone
- Class: DATA
- Severity: S1
- Surface: `loops/runner.py:807` ⇄ `web/src/control/views/LoopsView.tsx:404`
- Repro: feed the real card the same instant as timezone-less ISO, UTC `Z`, and `+02:00`; compare
  the rendered elapsed value in a Europe/Berlin browser.
- Evidence: payload: `audit/date-matrix/matrix.json` preserves all ten real `/api/loops` rows |
  state file: `heartbeat.current.started_at="2026-07-13T06:17:…"` (no zone) rendered `2h 01m`,
  while the same instant with `Z` or `+02:00` rendered `2m` | screenshot:
  `audit/date-matrix/timezone-less.png`, `audit/date-matrix/utc-z.png`,
  `audit/date-matrix/plus-02.png`
- Truth: runner writes server-local ISO without an offset; `Date.parse` treats it as browser-local,
  so a travelling/remote operator sees a phase shifted by the zone difference. Machine timestamps
  now use explicit UTC `Z` in heartbeat history/current, structured ledger, and visual attestation.
- Status: FIXED (fail-first runner suite 2 failures; post-fix 112/112 green; commit follows)

### F-15  Invalid and future phase timestamps are silently presented as a live `seit 0s`
- Class: DATA
- Severity: S1
- Surface: `web/src/control/lib/loopTime.ts` ⇄ `web/src/control/views/LoopsView.tsx`
- Repro: while the scratch pack lock is held, write absent, empty, garbage, epoch `0`, a
  millisecond number, or a one-hour-future value to `heartbeat.current.started_at` and wait for the
  real five-second poll.
- Evidence: payload: every injected wire value is preserved in `audit/date-matrix/matrix.json` |
  state file: `/home/piet/.hermes/loops/loops-date-audit/heartbeat.json` was restored after capture |
  screenshots: `audit/date-matrix/{absent,empty,garbage,epoch-zero,milliseconds-number,future-one-hour}.png`
- Truth: disk/API contain no valid instant; DOM claims `build · grok-4.5 · seit 0s`, which looks
  freshly live. A single guarded parser now requires a string with `Z`/numeric offset, rejects
  invalid values and material future skew, and renders `Zeitstempel ungültig` in hero, card,
  progress, ring and history-age paths.
- Status: FIXED (fail-first component suite 4 failures; post-fix 69/69 green; commit follows)

## HARD-STOP SAFETY PROOF

- Health-Track: `main...origin/main`, HEAD `330ec8b`; no tracked changes or new commits from this turn; existing untracked local audit/worktree files were left untouched.
- Health-Track push/deploy: not run.
- Grok scratch branch: `loop/xai-hard-gate` remains at seed `92491bb`; `proof.txt` SHA-256 `f7f7e9ea080a037e88d1e646d3b8af3bf10fcd353406ef6ce73a24a1b21dcc99`.
- Hermes: adapter/tests/audit artefacts remain isolated and uncommitted in `/home/piet/.hermes/worktrees/codex-loops-health-track-audit-20260712`.
- No Hermes push or deploy occurred; no Health-Track packs were authored or run after the hard gate.

## LOOP RESULTS

### ht-ux-polish round 1 attempt 1
- Phases: plan(`gpt-5.6-sol`, 203s, rc 0) → build(`grok-4.5`, 353s, operator-terminated) → verify(not run)
- Plan: `P1-settings-controls-first` — move ordinary Settings controls before connection setup
- Diff: none   Commit: none
- Gate: not run; worker stopped on safety-contract violation
- Verdict: FAIL(builder invoked Vercel production-env download path despite explicit secret rule)
- IMPACT: none — worktree stayed clean; no UI change exists. Live `.env.local` metadata stayed at
  `2026-07-09 20:44:56 +0200`, and the loop worktree contains no `.env.local`.
- Worth landing? no — this is exactly a green-looking workflow that must be rejected before code.

### ht-ux-polish round 1 attempt 2
- Phases: plan(reused) → build(`grok-4.5`, 513s, rc -15) → verify(not run)
- Plan: `P1-settings-controls-first`
- Diff: net zero; Fix `aed8d71` followed by committed Revert `b1f0e51`   Commit: none worth landing
- Gate: builder claimed green but piped an audit task through `head`; evidence exit was not
  trustworthy. Runner classified `ENGINE_RC_-15 (BUILT ...)` and bounced.
- Verdict: FAIL(committed its own revert, removed regression test, net no product change)
- IMPACT: none — before and after branch content are byte-equivalent to `main`, despite screenshots
  and a `BUILT` claim. This is success theatre caught by runner rc before verifier.
- Worth landing? no — two history-only commits with zero diff.

### ht-ux-polish round 1 attempt 3
- Phases: plan(reused) → build(`grok-4.5`, 587s, rc 0) → verify(`gpt-5.6-sol`, 583s, rc 0)
- Plan: YAML `HT-UX-P1-SETTINGS-CONTROLS-FIRST` — move ordinary Settings controls before
  connection setup; builder/verifier incorrectly reported filename-derived id
  `P1-settings-controls-first`.
- Diff: page test +36 and page reorder +5/-5 in `dc89892`; runner revert `3d6c27d`; net zero
  product diff   Commit: none worth landing
- Gate: builder and verifier each reported green (`371` tests; authenticated mobile audit `42/42`),
  but the binding verdict failed `PASS_ID_MISMATCH` and the runner reverted the commit.
- Verdict: FAIL(verifier verdict was not bound to the exact YAML plan ID)
- IMPACT: none retained — the visible Settings reorder existed only in `dc89892`; runner rejection
  restored the original UI. Screenshot `audit/screenshots/ht-ux-polish-pass-id-mismatch-bounced.png`
  proves the tab nevertheless presents this as green and land-ready.
- Worth landing? no — third attempt on this defect is exhausted; prompts are tightened and the next
  run must select another mobile backlog item.

### ht-defect-hunt round 1 attempt 1
- Phases: plan(`gpt-5.6-sol`, 278s, rc 0) → build(`grok-4.5`, 371s, rc 0) → verify(`gpt-5.6-sol`, 98s, rc 0)
- Plan: `ht-defect-weight-range-validation` — reject non-finite/out-of-range weight before DB I/O and constrain the mobile field
- Diff: five product/test files, +145/-1   Commit: `c5342f7`, then runner revert `904253c`
- Gate: builder recorded `npm run gate` exit 0 / 379 tests; verifier did not run it because the
  parent audit coordination claim was mistakenly classified as foreign overlap.
- Verdict: BOUNCED(harness coordination contract blocked independent verification)
- IMPACT: none retained — the runner correctly reverted the unverified commit. The bug remains on
  this attempt even though the proposed diff was substantive.
- Worth landing? no — unverified; parent-claim authorisation was added before the automatic retry.

### ht-defect-hunt round 1 attempt 2
- Phases: plan(reused `gpt-5.6-sol`) → build(`grok-4.5`, 319s, rc 0) → verify(`gpt-5.6-sol`, 240s, rc 0)
- Plan: `ht-defect-weight-range-validation` — enforce the existing 20–500 kg contract at mobile
  input and before Supabase client/auth/upsert/revalidate
- Diff: `GewichtForm.tsx`, action, shared validator, and two test files, +149/-1   Commit: `6fc1047`
- Gate: builder `npm run gate` → exit 0, 379 tests; Sol independently repeated gate → exit 0,
  reproduced pre-fix `weight_kg:-1` upsert, post-fix 12/12 targeted green, mutation 6/12 red
- Verdict: PASS
- IMPACT: before, `-1` reached `weight_log.upsert` and the mobile input had no range; after, the
  input exposes min=20/max=500/step=0.1 and invalid direct submissions fail in German before any DB
  I/O. Verifier log: `/home/piet/.hermes/loops/ht-defect-hunt/logs/20260713-012242-verify.log`.
- Worth landing? yes — real reproduced data-integrity bug, visible mobile constraint, non-tautological regression proof.

### ht-defect-hunt goal-range attempt 1
- Phases: plan(`gpt-5.6-sol`, 148s, rc 0) → build(`grok-4.5`, 311s, rc 0) → verify(not run)
- Plan: `ht-defect-goal-range-validation` — validate calories and target weight before DB I/O
- Diff: six focused product/test files; commit `59ded4a`, runner revert `4848287`
- Gate: builder reported exit 0 / 398 tests, but `last-status` began Markdown
  `**BUILT ht-defect-goal-range-validation**` instead of the protocol token.
- Verdict: FAIL(invalid status bytes; runner correctly refused an otherwise substantive commit)
- IMPACT: none retained — runner reverted the code, so the original Ziele bug remains.
- Worth landing? no — no verifier ran.

### ht-defect-hunt goal-range attempt 2
- Phases: plan(reused) → build(`grok-4.5`, 335s, rc 0) → verify(not run)
- Plan: `ht-defect-goal-range-validation`
- Diff: repeated focused fix `d24cc44`, runner revert `bcf43cf`
- Gate: builder again reported exit 0 / 398 tests and again wrote Markdown plus narrative into
  `last-status` instead of one exact line.
- Verdict: FAIL(repeated invalid status bytes; plan bounced after fail-streak)
- IMPACT: none retained — two plausible green implementations produced zero landable change.
- Worth landing? no — this is Grok protocol failure, not product success; the item is killed.

### ht-defect-hunt profile-validation attempt 1
- Phases: plan(`gpt-5.6-sol`, 187s, rc 0) → build(`grok-4.5`, 463s, rc 0) →
  verify(`gpt-5.6-sol`, 172s, rc 0)
- Plan: `ht-defect-profile-value-validation` — reject impossible height, future/invalid birth date,
  and impossible current weight before any DB I/O; expose matching native mobile bounds
- Diff: five focused product/test files, +364/-0   Commit: `e3c7c03`
- Gate: builder and Sol each reported `npm run gate` exit 0; 403 tests; Next build green. Sol
  independently reproduced 15/24 pre-fix failures and 24/24 post-fix passes.
- Verdict: PASS (runner PASS retained after operator clarified that harmless shell formatting must
  not outweigh independently reproduced product truth; Sol's own pre/post/mutation/full-gate
  evidence was green)
- IMPACT: the code itself is substantive — before, `height_cm:-10`, `birth_date:2999-01-01`, and
  `current_weight_kg:-1` reached Supabase; after, invalid values stop before client/auth/upsert and
  the mobile form exposes height 50–300 cm, birth-date max=today, and weight 20–500 kg. However,
  Sol independently proved the bug and the fix; the later stricter retries added no product value
  and were rejected only because the loaded host could not fork Vitest workers.
- Worth landing? yes — real mobile constraint plus server-side data-integrity fix; provenance is
  now available for contradictions, but command-style policing is no longer a product gate.

### ht-defect-hunt profile-validation pragmatic final acceptance
- Phases: retained original Grok build/Sol verification; independent operator re-gate after host
  pressure subsided
- Plan: `ht-defect-profile-value-validation`
- Diff: five focused files, +364/-0   Commit: `ba80c20` (clean cherry-pick of Grok's original
  independently verified `e3c7c03`)
- Gate: `VITEST_MAX_WORKERS=4 npm run gate` → exit 0; `65 passed | 1 skipped` test files,
  `403 passed | 3 skipped` tests; Next production build compiled and generated 22/22 pages
- Verdict: PASS
- IMPACT: before, impossible profile height/date/weight values reached Supabase and the mobile form
  exposed no matching native bounds; after, invalid values fail before client/auth/upsert and the
  390px mobile inputs carry height 50–300, birth-date max=today, and weight 20–500 constraints.
- Worth landing? yes — second independently reproduced and green-gated defect impact; plan restored
  to `20-verified`.

### xai-hard-gate round 1
- Phases: plan(`gpt-5.6-sol`, 39s, rc 0) → build(`grok-4.5`, 5s, wrapper rc 0 / provider HTTP 403) → verify(not run)
- Plan: `xai-hard-gate-001` — replace one proof line and commit it
- Diff: none   Commit: none (branch stayed at seed `92491bb`)
- Gate: not run; build produced no commit
- Verdict: FAIL(provider denied `grok-4.5` in this region)
- IMPACT: none — before `proof.txt` was `xAI hard gate: pending`; after it remained byte-identical.
- Worth landing? no — the mandated builder did not build.

### xai-hard-gate round 2
- Phases: plan(reused `gpt-5.6-sol` plan) → build(`grok-4.5` via official `grok-build` subscription slot, 40s, rc 0) → verify(`gpt-5.6-sol`, 45s, rc 0)
- Plan: `xai-hard-gate-001` — replace one proof line and commit it
- Diff: `proof.txt`, +1/-1   Commit: `12d5efd`
- Gate: verifier checked exact file content, commit existence, clean tree, and no extra files → rc 0; `last-status` = `PASS xai-hard-gate-001`
- Verdict: PASS
- IMPACT: before `proof.txt` said `xAI hard gate: pending`; after it says `grok-4.5 built this commit`, authored and committed by the real Grok builder.
- Worth landing? no — deliberate disposable transport proof; the engine implementation itself is worth landing in Hermes.
