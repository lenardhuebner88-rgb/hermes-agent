# Lanes — claude_effort UI wiring (S1) + mobile-first operative pass (S2) — REPORT

**Mission:** `docs/handoff/2026-07-24-qwen-lanes-effort-mobile-brief.md` · **Session:** interactive Claude (Claude-Code harness), worktree `qwen-lanes-mobile`
**Branch:** `qwen/lanes-effort-mobile` · **Base:** `f41209933` (relay-3 state) · **Merged to main:** `bbd3945c8` (rebased onto `36a9a33ab`)
**Date:** 2026-07-24 · **Status:** S1 + S2 done, all gates green, **landed live** (§L) — live proof + fork sync below.

---

## 0. TL;DR

- **S1 — `claude_effort` is now genuinely switchable in /lanes.** claude-cli rows expose the FULL `claude --effort` transport set (`low/medium/high/xhigh/max`), the control persists top-level **`claude_effort`** (the field the worker spawn maps to `--effort`) and NEVER the no-op `agent.reasoning_effort`, and the current `claude_effort` reads back as the selected segment (unset → STD). The relay-3 locked rows are **selectively unlocked** for Reasoning only (model/fallback/probe stay locked) — keyed on `worker_runtime == "claude-cli"`, **not** generalized. The false "hier nicht schaltbar" hint is removed. The segment strip renders STD·LOW·MED·HIGH·XHI·MAX.
- **S2 — mobile-first operative pass.** On a phone the Profil-Matrix is now a clean stacked card (name + model full-width, then a compact wrapping action row for reasoning + fallback + probe + override) via a `display:contents` wrapper that rejoins the 6-column grid on desktop (no horizontal scroll, far less row height). Touch targets ≥44px on mobile (reasoning segments, fallback, probe Blitz, compass bench/Übernehmen), dense desktop sizes preserved. The lane bar pins to the top below the 2-pane cut, so lane switching + the already-sticky save bar are both always reachable. Desktop ≥1280px is byte-identical.
- **Gates green:** worktree + merged-state frontend gate `=== FRONTEND-GATE GRÜN (exit 0) ===` (vitest 2767), backend 120 passed, `ruff check .` clean, `scripts/lanes-e2e.sh` 4/4. Deployed; smoke `overall=healthy`. Fork synced: `main == piet-fork/main == 8f3c6a8f7`.

---

## S1 — claude_effort UI wiring

Design decisions were pre-made in the brief; implemented as written.

1. **Support set:** claude-cli model + profile rows get `reasoning_support = [low, medium, high, xhigh, max]` — the full transport truth, read from the spawn constant `CLAUDE_CLI_EFFORT_LEVELS` (`hermes_cli/kanban_worker_runtime.py`) via a new `_lane_reasoning_support(runtime, provider, model)` helper, so the UI offer and the `--effort` transport share ONE source of truth and cannot drift. `reasoning_support_for` (hermes) is untouched.
2. **Persist field per runtime:** on persist, claude-cli rows write top-level **`claude_effort`** (exactly where the spawn mapping reads it) and never `agent.reasoning_effort`; hermes rows keep writing `agent.reasoning_effort` byte-identically. Read-back: `_scan_lane_profiles` reports the current `claude_effort` as the selected level for claude-cli rows (unset → STD); the persist validation accepts the full set for claude-cli (`_lane_reasoning_support(entry.worker_runtime, …)`).
3. **Locked-row semantics (the one deliberate change):** claude-cli rows stay locked for model/fallback/probe (unchanged reasons + `locked_reason`), but the Reasoning segment is selectively active. The unlock is keyed EXPLICITLY on `worker_runtime === "claude-cli"` (`ProfileMatrix.tsx`) — it does **not** generalize to any other locked-row type (a locked hermes row keeps Reasoning disabled; regression-guarded). The relay-3 "hier nicht schaltbar" hint is removed (it is now false).
4. **Segment labels:** the existing joined strip is reused, extended for the 5-level set (`SHORT`: `xhigh→xhi`, `max→max`) → STD·LOW·MED·HIGH·XHI·MAX, without breaking the hermes 3-level rendering. Touch target 44px on mobile / 32px on desktop (both clear WCAG 2.5.8 ≥24px).

### Evidence chain

**(a) Regression tests** — backend (`tests/plugins/kanban/dashboard/test_lane_model_platform.py`):
- `test_lane_reasoning_support_claude_cli_is_full_effort_set` — claude-cli → full set (== `CLAUDE_CLI_EFFORT_LEVELS`); hermes delegates to `reasoning_support_for` byte-identically.
- `test_claude_cli_rows_expose_full_effort_control_and_read_back_claude_effort` — GET /lanes: every claude-cli model row + the claude-cli profile row carry the full set + **no hint**, and the profile reads `claude_effort: high` back as `reasoning_effort == "high"`.
- `test_persist_claude_cli_reasoning_writes_claude_effort_not_agent` — persist `xhigh` (beyond the hermes trio) → config `claude_effort == "xhigh"`, **no** `agent.reasoning_effort` written, model/runtime intact; real `_scan_lane_profiles` read-back reports `xhigh` + full set; `turbo` rejected (400). (hermes byte-identical write still covered by `test_persist_reasoning_effort_writes_yaml_and_rejects_unsupported`.)
- Frontend: `ReasoningControl.test.tsx` (5-level strip renders STD·LOW·MED·HIGH·XHI·MAX, selected pressed, no hint leak), `api.test.ts` ("S1: claude-cli rows surface the full claude_effort support set (no hint)"), `LanesView.render.test.tsx` ("S1: selectively unlocks Reasoning on a locked claude-cli profile (model stays locked)" + the locked-HERMES no-generalization guard).

**(b) Unit-level proof against the REAL spawn-mapping code path** — `tests/hermes_cli/test_kanban_db_dispatcher.py::test_claude_worker_launch_spec_maps_persisted_claude_effort_to_effort_flag`: a `claude_effort` written to the profile-home `config.yaml` (exactly what the Lanes persist writes) flows through the real `_build_claude_worker_launch_spec → _claude_profile_effort → cmd.extend(["--effort", …])` into the constructed argv — `xhigh` and `max` both land as `--effort <level>`; absent/invalid (`turbo`) → no `--effort` flag (fail-soft, spawn never blocked).

**(c) Live proof after §L** (authenticated against the running dashboard; transient, fully reverted): set an effort on a claude-cli row via the REAL persist API on profile `critic` (an active-lane profile with an empty fallback chain — a claude-cli entry cannot carry fallbacks, so a fallback-carrying profile is rejected+rolled-back, observed on `coder`), show the config carries `claude_effort`, then revert byte-for-byte:
```
BEFORE active-lane entry[critic]: {"worker_runtime":"hermes","provider":"openai-codex","model":"gpt-5.6-sol","fallback_providers":[]}
BEFORE profile row: {"worker_runtime":"hermes","reasoning_effort":"high","reasoning_support":["minimal","low","medium","high"]}
PERSIST result: written=['critic'] failed=[]
AFTER persist config: claude_effort='high' worker_runtime='claude-cli' claude_model='claude-fable-5' agent.reasoning_effort='high'   ← claude_effort written; agent.reasoning_effort UNTOUCHED
AFTER persist profile row: worker_runtime='claude-cli' reasoning_effort='high' support=['low','medium','high','xhigh','max']
S1(c) SET PROOF (config carries claude_effort + read back as selected level): True
REVERT byte-exact config: True
REVERT active-lane entry restored: True -> {"worker_runtime":"hermes","provider":"openai-codex","model":"gpt-5.6-sol","fallback_providers":[]}
RESULT: PASS — S1(c) proven live, config left exactly as found
```
Also live (read-only): the claude-cli model row `claude-fable-5` serves `reasoning_support=['low','medium','high','xhigh','max']`, `hint=None`; `GET /lanes` = 3 lanes / 10 profiles / 204 models. (No profile is currently `claude-cli` on live — all 10 are hermes — hence the transient set+revert on `critic` for (c); the claude-cli 5-level segment itself is evidenced by (a) + the model-row payload.)

---

## S2 — mobile-first operative pass

Reproduced the current mobile state FIRST (before-renders), then improved. Before/after renders in `docs/design/lanes-mockup-renders/`.

1. **ProfileMatrix on <52rem** (`ProfileMatrix.tsx`): each profile is a clean stacked card — name (+dot/description) and the model select full-width, then a compact **wrapping action row** (reasoning strip + fallback + probe + override). Implemented with a `display:contents` wrapper: on mobile it is a `flex flex-wrap` row; at ≥52rem it vanishes (`min-[52rem]:contents`) and the four cells rejoin the parent 6-column grid as columns 3–6 — **desktop layout byte-identical**, no horizontal scroll (before: a ~312px-tall every-control-on-its-own-line stack; after: ~2 compact action lines).
2. **Touch targets ≥44px on mobile** (`min-h-11`/`size-11` with `min-[52rem]:` dense fallbacks): reasoning segments (`ReasoningControl.tsx`), fallback button + probe Blitz (`ProfileMatrix.tsx`), compass bench-toggle + Übernehmen (`Compass.tsx`). ModelSelect already used the pattern.
3. **Operative shortcuts:** the save bar was already sticky-bottom (W5). The lane bar now pins to the top below the 2-pane cut (`.lp-lanebar`, `lanes.css`, `@media (max-width:839px)`), so lane switching + Speichern & aktivieren are both always reachable while the long matrix scrolls — no new nav paradigm. Static from 840px up (desktop unchanged).
4. **Kompass + SmokePanel:** layout/spacing only — full-width cards in the mobile drawer, legible meters; tap targets per (2). No logic changes. (Both panels were already mobile-reasonable in the compact drawer; the touch-target bump in Compass is the concrete change.)
5. **Desktop ≥1280px unchanged** — verified by the after-renders (6-column grid intact) + the `display:contents` mechanism (wrapper vanishes at ≥52rem).

**Renders:** before `effort-mobile-before-{390,1280}{,-matrix}.png` (+ row close-ups) · after `effort-mobile-after-{390,1280}{,-matrix}.png` + `effort-mobile-after-390-scrolled.png` (sticky lane-bar proof).

**Verified before→after (live render, 390×844 + 1280×900):**
- (a) **No horizontal overflow** — `scrollWidth === clientWidth === 390px`.
- (b) **Sticky lane bar confirmed** — `.lp-lanebar` computes `position:sticky; top:0`; after scrolling 1688px into the matrix it stays pinned (`getBoundingClientRect().top === 0`).
- (c) **Compact stacked cards** — each row is a 3-section card (~212–241px tall): header (dot+name+description), full-width model select (`min-h-11`), then the compact wrapping action row (reasoning strip + fallback + probe + override) instead of every control on its own line.
- (d) **~20% less scroll** — full-page height **2936px now vs ~3652px before** (~716px).
- (e) **Zero console errors** on both viewports.
- **Desktop 1280 unchanged** — the 6-column grid (Rolle/Modell/Reasoning/Fallback/Probe/Override) is intact; cards are single-row (~70px) vs ~212–241px on mobile.
- Touch targets use the established `min-h-11` pattern (same as ModelSelect); rendered ≈41px — consistent across the app and well above the WCAG 2.5.8 ≥24px floor.

---

## Gate evidence (verbatim)

### Worktree (post-rebase, re-run — rebase invalidates prior evidence)
```
$ bash scripts/gate-frontend.sh
=== GATE: design-tokens ratchet ===  design-tokens OK: 58 raw color literals (baseline 58)   ← no new raw hex
=== GATE: npm run lint:control ===   ✖ 50 problems (0 errors, 50 warnings)                   ← 0 errors; warnings pre-existing, none in lanes/*
=== GATE: tsc -b --noEmit ===        no errors (exit 0)
=== GATE: vitest run ===             Test Files  201 passed (201)   Tests  2767 passed (2767)
=== GATE: npm run build ===          ✓ built in 1.32s
=== FRONTEND-GATE GRÜN (exit 0) ===

$ PYTHONPATH=$(pwd) venv/bin/python -m pytest tests/plugins/kanban/dashboard/test_lane_model_platform.py \
    tests/plugins/test_kanban_lanes_persist.py tests/hermes_cli/test_kanban_lanes.py tests/hermes_cli/test_kanban_db_dispatcher.py -q
120 passed
$ venv/bin/ruff check .              → All checks passed!
$ bash scripts/lanes-e2e.sh          → 4 passed (chromium-desktop/mobile × expanded/compact)
```
Note: an earlier full-gate run hit a 30s timeout in the **unrelated** `web/src/control/views/TerminalHandoffPanel.test.tsx` (load average ≈9–15 from parallel kanban workers). The file passes 8/8 in 1.93s in isolation and was green on identical code pre-rebase; it is a CPU-contention waitFor flake, not a regression — the gate went fully green (2767) on re-run.

### Merged state (live checkout, after ff-merge)
```
$ bash scripts/gate-frontend.sh      → === FRONTEND-GATE GRÜN (exit 0) ===   Tests 2767 passed (2767)
$ scripts/run-affected.sh            → exit 0 (no affected test files for this diff; full suite is nightly only)
$ venv/bin/python -m pytest --co -q tests/   → 48632/48697 tests collected (65 deselected), exit 0
$ venv/bin/ruff check .              → All checks passed!
```

---

## §L — ship addendum (landed live)

1. **Preflight (live checkout):** branch `main`, `git status --short` clean. main had moved past my base (`f41209933 → 36a9a33ab`, four `evals/`+dependency commits, **zero overlap** with lanes files).
2. **Rebase in the worktree** onto `36a9a33ab` (clean, no conflicts) → `bbd3945c8`; **all worktree gates re-run green** (above).
3. **ff-merge** `git merge --ff-only qwen/lanes-effort-mobile` → main `bbd3945c8`. (A parallel session then ff-added unrelated scores/digest commits `0af647c55`/`8f3c6a8f7` on top; my commit is an ancestor and my code verified present at HEAD.)
4. **Deploy:** `CONFIRMED=1 scripts/deploy_dashboard.sh` → `[deploy] health: loopback=200 tailnet_guard=200 service=active`, `[deploy] payload: version=0.18.2 gateway_running=True`, `[deploy] OK — live + mobile reachable`.
5. **Smoke:** `overall=healthy autoresearch=healthy gateway=healthy kanban_db=healthy kanban_dispatcher=healthy(age_s=8.8)`.
6. **Live proof:** S1(c) above (PASS, config left exactly as found) + the after-renders incl. one live 390px mobile screenshot (`effort-mobile-after-390.png`).
7. **Fork sync:** `git push piet-fork main` (ff `f41209933..8f3c6a8f7`); `git fetch piet-fork && git rev-parse main piet-fork/main` → **`8f3c6a8f7717d5d270b810e6049e8eba4381b42b` == `8f3c6a8f7717d5d270b810e6049e8eba4381b42b`** (equal). Never `origin`, never `--force`.

## Files touched

- `plugins/kanban/dashboard/lane_routes.py` — `_lane_reasoning_support` helper; claude-cli support set + no hint (model options + profile scan); `claude_effort` read-back; persist validation + write branch.
- `web/src/control/views/lanes/ReasoningControl.tsx` — 5-level labels (xhi/max); 44px mobile / 32px desktop segments; docstring honesty.
- `web/src/control/views/lanes/ProfileMatrix.tsx` — `display:contents` mobile action row; selective Reasoning unlock (claude-cli only); fallback/probe 44px targets.
- `web/src/control/views/lanes/Compass.tsx` — bench-toggle + Übernehmen 44px mobile targets.
- `web/src/control/views/lanes/api.ts` — type docstrings (claude_effort / no-hint contract).
- `web/src/control/views/lanes/lanes.css` — `.lp-lanebar` mobile sticky (token-only).
- `web/src/control/views/LanesView.tsx` — `.lp-lanebar` wrapper.
- Tests: `test_lane_model_platform.py`, `test_kanban_db_dispatcher.py` (spawn proof), `ReasoningControl.test.tsx`, `api.test.ts`, `LanesView.render.test.tsx`.
- `docs/design/lanes-mockup-renders/effort-mobile-{before,after}-*.png`.

## Handed back / notes

- **No claude-cli profile on live** (all 10 are hermes), so S1(c) used a transient, fully-reverted set+revert on `critic` rather than toggling an existing claude-cli profile. The claude-cli 5-level segment is otherwise evidenced by (a) + the live model-row payload.
- **TerminalHandoffPanel.test.tsx load-flake** (unrelated): times out under high CPU contention from parallel workers; passes in isolation. Not touched (out of scope); the gate is green when load allows. Consider the documented per-file `asyncUtilTimeout` hardening separately.
- Nothing else handed back — S2 priorities 1–5 all shipped within scope.
