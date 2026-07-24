# Lanes polish — operator decisions + mobile save-bar — REPORT

**Mission:** `docs/handoff/2026-07-24-qwen-lanes-polish-brief.md` · **Session:** interactive Claude (Claude-Code harness), worktree `qwen-lanes-polish`
**Branch:** `qwen/lanes-polish` · **Base:** `19c3b535e` (main incl. shipped verify relay `94a3f794e`)
**Date:** 2026-07-24 · **Status:** 5 work items done with regression tests + gates green; live landing in §8 addendum.

---

## 0. TL;DR

- **W1** codex `-pro` ids (`gpt-5.6-sol/terra/luna-pro`) no longer offered as selectable/sinnvoll/probe-able; persisted overrides still render. New `_lane_offer_exclusion` + a `sinnvoll` guard; `offer_excluded` flag added to the payload.
- **W2** Alibaba image/video models (`qwen-image-2.0*`, `wan2.7-image*`) dropped from the offer AND the chat-probe scope (model-probe returns `skipped` without burning a call). Capability-first detection (models.dev output modalities) with a documented id-pattern fallback.
- **W3** claude-cli reasoning investigated → **it IS configurable** (`claude --effort <low|medium|high|xhigh|max>`; Hermes already maps the profile field `claude_effort` → `--effort` at worker spawn, `kanban_db.py:30560`). Live proof included. The lanes control, however, persists `agent.reasoning_effort` (a **no-op** for claude-cli), so the greyed segments were misleading → replaced with the **honest no-Knopf state + an accurate hint** pointing at `claude_effort`. Full UI wiring of `claude_effort` handed back as a documented follow-up (design-ambiguous per the brief's escape hatch).
- **W4** `extra_models` now land in the dropdown catalog for **every** provider (real-data: `alibaba-token-plan`), the way openrouter already did; openrouter behavior byte-identical.
- **W5** mobile save/discard bar fixed: at 390px the two actions were stacked full-width (143px-tall bar); now they share a row side-by-side (hint on its own line), desktop unchanged, plus an iOS safe-area bottom inset. Before/after screenshots in `docs/design/lanes-mockup-renders/`.
- **Gates green:** frontend `=== FRONTEND-GATE GRÜN (exit 0) ===` (lint 0 errors / tsc clean / vitest 2765 / build ok, new Tailwind classes confirmed compiled); backend pytest **22 + 356 passed**; `ruff check .` clean.

---

## W1 — remove codex `-pro` from the offer

- **Diff:** `plugins/kanban/dashboard/lane_routes.py`
  - `_lane_offer_exclusion(provider, model)` → reason for `provider=="openai-codex" and model.endswith("-pro")` (the Codex endpoint does not serve them; a probe falls back to kimi/k3 — verify REPORT §4.1).
  - `_annotate_lane_model_relevance`: `sinnvoll = (exclusion is None) and (<old relevance rules>)`; sets `row["offer_excluded"]`. So a `-pro` model that would otherwise be relevant (`used_in_profiles` is True because openai-codex is a profile default) is forced out of the offer.
- **Persisted override keeps rendering:** the model is NOT removed from the catalog — only its `sinnvoll` flips. The frontend's `ModelSelect` `selectedPinned` (api.ts / ModelSelect.tsx) keeps the current selection in the open dropdown even when not sinnvoll, and the matrix row resolves its label from the `models` array (the `-pro` row is still present via the inventory/lane-pinned/profile-default sources).
- **Regression tests** (`tests/plugins/kanban/dashboard/test_lane_model_platform.py`):
  - `test_lane_offer_exclusion_and_image_detection` — unit: all three `-pro` ids excluded; non-pro primary not; a `-pro` id on another provider (`openrouter nex-agi/nex-n2-pro:free`) NOT excluded.
  - `test_offer_excludes_codex_pro_but_keeps_them_renderable` — GET /lanes: `-pro` has `used_in_profiles=True` yet `sinnvoll=False`, `offer_excluded=True`, and is still in the `models` array.

## W2 — remove image models from probe scope and offer

- **Diff:** `lane_routes.py`
  - `_lane_image_model(provider, model)` — capability-first: `get_model_info(...).output_modalities` → image-only output = generator; text output present (incl. multimodal text+image) = NOT excluded; unknown to models.dev → documented id-pattern fallback `qwen-image-*` and `wan*-image*` (Alibaba "Wan" line). Patterns are deliberate and commented, not blind substring guessing.
  - `_lane_offer_exclusion` also returns a reason for image models → `sinnvoll=False` (drops them from the `filterSinnvoll` batch-probe scope and the ModelSelect offer).
  - `_run_lanes_model_probe` returns `status="skipped"` (`reason="image/video model — chat-probe not applicable"`) for image models **before** invoking `_run_single_lanes_auth_smoke`, so an explicit catalog-probe/model-probe on an image model does not burn an Abo call. The auth-smoke endpoint is unaffected — it calls `_run_single_lanes_auth_smoke` directly (caller-grep verified).
- **Regression tests:**
  - `test_lane_offer_exclusion_and_image_detection` — id-pattern + capability paths (image-only True; text & text+image False).
  - `test_offer_excludes_image_models_and_probe_skips_them` — GET /lanes: image models `sinnvoll=False`/`offer_excluded=True` but still in `models`; model-probe on an image model → `skipped` and the auth-smoke runner is **not** called (asserted via a spy); a chat model on the same provider still probes `ok`.

## W3 — claude-cli reasoning: investigated, made honest

### Investigation (operator's question: "kann man für claude Reasoning einstellen?")

**Answer: YES — it is already half-wired.** Evidence (verbatim):

- `claude --help` documents the flag:
  ```
  --effort <level>    Effort level for the current session (low, medium, high, xhigh, max)
  ```
- Hermes already maps the profile config field `claude_effort` → `--effort` at worker spawn (`hermes_cli/kanban_db.py:30556-30562`):
  ```
  # Per-profile reasoning-effort override (optional). Invalid values are
  # already filtered out by _claude_profile_effort (logs a warning and
  # returns None) — the spawn is never blocked by a bad claude_effort.
  worker_effort = _claude_profile_effort(env.get("HERMES_HOME"))
  if worker_effort:
      cmd.extend(["--effort", worker_effort])
  ```
  `CLAUDE_CLI_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")` (`kanban_worker_runtime.py:15`).
- **Live proof (Max Abo, cheap single call):** `claude -p --effort low --model haiku "Reply with exactly the two characters: OK"` → **exit 0**, stdout `OK` (the connector-precendence warning is benign — unrelated to auth). Proves the flag is accepted on this Abo and the transport carries it.

### Why the lanes control was dishonest (and the fix)

The lanes Reasoning segment persists `agent.reasoning_effort` (`lane_routes.py:1829-1834`), which the `claude -p` transport **ignores** — it reads `claude_effort`, a different field. So the old greyed `[low,med,high]` segments (locked claude-cli rows) implied a hermes-style control that, were it active, would write a no-op. The honest state: no hermes-style control here, with a pointer to where reasoning actually lives.

- **Diff (honest state, path 3 of the brief):**
  - `lane_routes.py`: claude-cli **model** rows (`_append_lane_model_option`) and claude-cli **profile** rows (`_scan_lane_profiles`) now get `reasoning_support=[]` + `reasoning_hint=_LANE_CLAUDE_CLI_REASONING_HINT` ("claude -p: Reasoning via Profil-Config „claude_effort" (--effort) — hier nicht schaltbar"). `reasoning_support_for` itself is **unchanged** (still `[low,medium,high]` for `(None,"claude-*")`), so hermes rows are unaffected; the `[]` is applied only at the claude-cli build sites. Because support is `[]`, the frontend can no longer stage a `reasoning_effort` for a claude-cli row, so persist never writes the no-op field for them.
  - Frontend plumbing: `reasoning_hint` added to `LaneModelOption`/`LaneCatalogProfile`; `EditorRow.reasoningHint` (api.ts `editorRows`); `ReasoningControl` gains an optional `hint` prop shown in the empty-support state; `ProfileMatrix` passes `row.reasoningHint`.
- **Regression tests:** backend `test_claude_cli_rows_show_honest_no_reasoning_control` (every claude-cli model row + the claude-cli profile row carry `reasoning_support=[]` + a `claude_effort` hint); frontend `ReasoningControl.test.tsx` (generic text vs explicit hint vs active-segments-don't-leak-hint) + `api.test.ts` "W3: claude-cli rows carry the honest no-Knopf hint".

### Handed back (follow-up, NOT forced — brief's escape hatch)

Full end-to-end UI wiring of `claude_effort` (make the claude-cli Reasoning segment *active*, write `claude_effort` instead of `agent.reasoning_effort` on persist, read it in `_scan_lane_profiles`, decide the exposed level set `[low,med,high]` vs the CLI's full `[low..max]`, and selectively unlock reasoning on an otherwise-locked row) is **design-ambiguous** — it changes locked-row semantics and the persist field per runtime. Shipped the honest state instead; the transport is proven configurable above, so an operator can set `claude_effort` in the profile config today. Recommend a dedicated follow-up slice for the UI wiring.

## W4 — extra_models for all providers (openrouter-optimal)

- **Diff:**
  - `hermes_cli/model_catalog.py`: `get_all_configured_provider_extra_models() -> dict[provider, [model_ids]]` — enumerates every provider with `extra_models`, reusing `get_configured_provider_extra_models` per provider (identical parsing); empty providers omitted.
  - `lane_routes.py`: `_append_other_provider_extra_model_options(out, seen)` iterates that map for every provider **except openrouter** (handled byte-identical by the unchanged `_append_openrouter_extra_model_options`, called immediately before). Each extra model is added with `runtime="hermes"`, `group=_lane_provider_label(provider)`, `provider=<p>`, `source="config"`. Models already in the catalog (e.g. from the authenticated inventory fetch) are deduped by `seen` (key = model/provider/runtime) → purely additive. Fail-soft (try/except logged) so a config read error can't break the catalog build.
- **OpenRouter byte-identical:** the openrouter function and its call position are untouched; the new function explicitly `continue`s on openrouter.
- **Regression test** `test_extra_models_for_all_providers_land_in_catalog`: with an empty inventory (so extra_models is the *only* source), `alibaba-token-plan` extra models appear in the catalog with `source="config"`/`runtime="hermes"`, and the openrouter extra model still appears with `group="OpenRouter"`; the direct accessor returns the expected per-provider map.

## W5 — mobile save/discard bar

- **Diagnosis (reproduced FIRST, real render at 390×844 on the live dashboard):** the save bar was `flex flex-col gap-2` below the 600px `tab` breakpoint → the hint, `Verwerfen`, and `Speichern + aktivieren` **stacked full-width** (two 326px-wide stacked buttons; bar ≈143px tall ≈17% of the viewport) — the "komisch dargestellt". Desktop (≥600px) was a clean single row (hint left, buttons right). No safe-area inset. Before screenshots: `docs/design/lanes-mockup-renders/w5-save-bar-before-390{,-cropped}.png`, `…-before-1280{,-cropped}.png`.
- **Fix** (`ProfileMatrix.tsx` save bar): `flex flex-wrap items-center gap-2 … tab:flex-nowrap tab:justify-end`; the hint/error get `basis-full tab:basis-auto tab:mr-auto`; both buttons `flex-1 … tab:flex-none`; container `pb-[calc(0.75rem+env(safe-area-inset-bottom))]` (env()=0 off-iOS → identical to the old `py-3`).
  - **Mobile (<600px):** `basis-full` forces the hint onto its own line; the two `flex-1` buttons then share the next row side-by-side (conventional secondary|primary), roughly halving the bar height.
  - **Desktop (≥600px):** `tab:flex-nowrap` + `tab:basis-auto tab:mr-auto` + `tab:flex-none` reproduce the old single-row layout exactly (hint pushed left, buttons at intrinsic width on the right) — no regression.
- **Compiled-CSS proof:** the worktree build's `hermes_cli/web_dist` CSS contains `padding-bottom:calc(.75rem + env(safe-area-inset-bottom))`, `flex-wrap:wrap`, and `flex-basis:100%` — the arbitrary safe-area value compiled (Tailwind v4 silently drops unparseable arbitrary values; it did not here).
- **After screenshots:** captured post-deploy at 390px + 1280px → `docs/design/lanes-mockup-renders/w5-save-bar-after-*.png` (see §8). No component test can assert paint (brief: proven by the pd-N bug), so the before/after renders are the acceptance evidence; the layout change is also guarded by tsc/build of the new classes.

---

## Gate evidence (worktree, verbatim)

### Frontend — `bash scripts/gate-frontend.sh` → **GRÜN (exit 0)**
```
=== GATE: npm run lint:control ===  ✖ 50 problems (0 errors, 50 warnings)   ← 0 errors; warnings pre-existing (fleet/BoardTab.tsx, fleet/RisikoPulse.tsx, …), none in lanes/*
=== GATE: tsc -b --noEmit (worktree-local) ===  no errors (exit 0)
=== GATE: vitest run ===   Test Files  201 passed (201)    Tests  2765 passed (2765)
=== GATE: npm run build ===   ✓ built in 1.25s
=== FRONTEND-GATE GRÜN (exit 0) ===
```

### Backend — per-file pytest (live venv, `PYTHONPATH=$(pwd)`)
```
tests/plugins/kanban/dashboard/test_lane_model_platform.py        → 22 passed  (17 prior + 5 new W1–W4)
tests/plugins/test_kanban_dashboard_plugin.py + test_kanban_lanes_persist.py + tests/hermes_cli/test_kanban_lanes.py
                                                                  → 356 passed, 6 warnings
```

### Ruff
```
$ ruff check plugins/kanban/dashboard/lane_routes.py hermes_cli/model_catalog.py   → All checks passed!
$ ruff check .                                                                      → All checks passed!
```

---

## 7. Files touched

- `plugins/kanban/dashboard/lane_routes.py` (W1/W2/W3/W4 backend)
- `hermes_cli/model_catalog.py` (W4 accessor)
- `tests/plugins/kanban/dashboard/test_lane_model_platform.py` (W1–W4 regression tests)
- `web/src/control/views/lanes/api.ts` (W3 types + EditorRow hint)
- `web/src/control/views/lanes/ReasoningControl.tsx` (W3 hint prop)
- `web/src/control/views/lanes/ProfileMatrix.tsx` (W3 hint pass-through + W5 save bar)
- `web/src/control/views/lanes/api.test.ts` (W3 plumbing test)
- `web/src/control/views/lanes/ReasoningControl.test.tsx` (new; W3 render test)
- `docs/design/lanes-mockup-renders/w5-save-bar-*.png` (W5 before/after)

Live landing, merged-state gates, deploy, smoke, live payload proof, W5 after-shots, and rev-parse hashes are recorded in **§8 (ship addendum)** after the §L rollout.
