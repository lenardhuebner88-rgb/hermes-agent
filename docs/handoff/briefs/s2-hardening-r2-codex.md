# BRIEF S2 hardening round 2 (Codex gpt-5.6-sol, effort medium, service_tier fast) — implement EXACTLY

Work ONLY in `/home/piet/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform` (branch `claude/lanes-model-platform`). `git status --short` first. NO push/deploy/restart/origin/force. Token discipline: NO raw hex / rgb() in .tsx/.ts. Use exec-direct with THIS file as the brief (brief from file, NOT a shell pipe). Wrap the Codex run in `timeout 1800`.

## Why
A GPT-5.6 closed-loop re-review of commit a64d1b752 returned VERDICT block: round-1 fixed the original 9 but the save-path still has silent-corruption / guard-bypass defects (verified by the orchestrator). F2/F6/F9 confirmed correct. R8 verified NOT-a-bug (the rows-reset effect resets on every reload regardless of the updated_at key). Fix R1,R3,R4,R5,R6,R7,R9 + a cheap R2-failure reload below; do NOT change anything else.

## Background facts (confirmed; do not re-derive, rely on them)
- `PUT /lanes/{id}` (`update_lane_endpoint`, lane_routes.py:1521) calls ONLY `kanban_db.update_lane(...)` → it REPLACES the lane's profile blob and writes NO config files.
- `POST /lanes/persist` (`persist_lane_models_endpoint`, :1603) writes per-profile CONFIG via `atomic_roundtrip_yaml_update` (loop :1765-1788) and MERGES the lane blob at :1790-1794 (`merged=dict(active_profiles); merged.update(lane_profiles); update_lane(...)`). Merge cannot delete a key.
- `profilesFromEditorRows` (api.ts) skips rows where `!row.touched` (round-1 F1). `editorRows` seeds `touched:false`; matrix `updateRow` sets `touched:true` on edit.
- `cloneFallbacks` preserves `base_url`; `LaneFallbackProvider` (api.ts) has optional `base_url`; backend `LanePersistFallbackEntry` (lane_routes.py:1582) currently has NO `base_url`.

## Fixes

### R1 (P1) LaneQuickSwitch must send the FULL lane map — `web/src/control/views/fleet/LaneQuickSwitch.tsx`
`handleSave` builds `nextProfiles = profilesFromEditorRows(freshRows.map(row => row.profile===updatedRow.profile ? updatedRow : row))`. Because only `updatedRow` is touched, F1 drops every other row, and since `updateLane` REPLACES the blob, all other overrides + fallback chains are deleted. Fix: mark EVERY row touched in that map so the replace receives the complete keep-set:
`freshRows.map((row) => (row.profile === updatedRow.profile ? updatedRow : { ...row, touched: true }))`
(An untouched override row then has model!=null → included with its current values; an untouched no-override row stays excluded; `updateLane` writes no config, so this is safe.) Do NOT alter the matrix path.

### R4 (P1) Clearing a lane override must remove it (backend + frontend)
Backend `lane_routes.py`:
- Add to `LanePersistBody` (after `profiles`): `removed_profiles: list[str] = Field(default_factory=list)`.
- In the handler, compute `removed = list(payload.removed_profiles)`. Do NOT 400 on a name that appears in BOTH `payload.profiles` and `removed` (overlap is intentional — see frontend). Optionally 400 if a removed name is in neither `known_profiles` nor the current `active_profiles` (typo guard); popping a missing key is otherwise harmless.
- Change the lane-blob guard from `if lane_profiles and active_id is not None and active_lane is not None:` to `if (lane_profiles or removed) and active_id is not None and active_lane is not None:` and AFTER `merged_profiles.update(lane_profiles)` do `for name in removed: merged_profiles.pop(name, None)`, THEN `update_lane(...)`. The per-profile CONFIG loop (:1765) is unchanged (it iterates `payload.profiles`; a removed-only row is not there, so its config is untouched unless it is also in `payload.profiles`).
- `config_snapshots`/rollback already cover every profile in `payload.profiles`; removed-only rows touch no config, so no extra snapshot is needed.
Frontend `api.ts` + `LanesView.tsx`:
- `EditorRow` += `initialChoice?: string`. In `editorRows`, seed `initialChoice: choiceFromEntry(entry)` for BOTH the catalog-mapped rows and the extras.
- `applyChoice` empty-choice branch: set `fallbackProviders: row.defaultFallbackProviders` (the config chain), NOT `[]` — clearing the model must not wipe the effective fallbacks.
- In the matrix save path (`LanesView` outer `handleSave`), compute `removed_profiles = rows.filter(r => r.touched && r.choice === "" && (r.initialChoice ?? "") !== "").map(r => r.profile)` and pass it through `persistLaneModels` (add `removed_profiles?: string[]` to its payload + the api function signature/body).
- Semantics this yields (verify with tests): a cleared row WITHOUT a reasoning change is omitted from `payload.profiles` (F1) but present in `removed_profiles` → lane entry deleted, config untouched. A cleared row WITH a reasoning change (`reasoningChanged`) IS in `payload.profiles` (F1 includes it via reasoningChanged) carrying `reasoning_effort:""` + `model:defaultModel` + `fallback_providers:<config chain>` AND in `removed_profiles` → backend clears config reasoning, writes no-op model.default/fallbacks, then removes the lane entry. Net: lane override gone + config reasoning reverted. Correct.

### R3 (P2) Preserve fallback base_url (backend + frontend)
- Backend `LanePersistFallbackEntry` (lane_routes.py:1582): add `base_url: Optional[str] = None`. (Then `row.model_dump()` at :1781 includes it, so the config keeps the endpoint.)
- Frontend `persistPayloadFromEditorRows` (api.ts): when mapping fallbacks, include base_url if present: `.map(f => ({ provider: f.provider, model: f.model, ...(f.base_url ? { base_url: f.base_url } : {}) }))`.

### R5 (P2) No-op Fallback "Apply" must not mark touched — `ProfileMatrix.tsx` `FallbackDrawer`
On commit, compare the normalized draft to the row's current fallbacks and call `onCommit` ONLY if they differ; otherwise just `onClose`. Compare `JSON.stringify(draft.filter(fb=>fb.provider&&fb.model).map(fb=>({provider:fb.provider,model:fb.model,base_url:fb.base_url??null})))` against the same projection of `row.fallbackProviders`. A no-op Apply then performs no `updateRow`, so the row stays untouched.

### R6 (P2) Compass adopt must respect the lock — `LanesView.tsx` `handleAdopt`
Add `if (row.locked) return;` as the first line after `const row = rows.find(...)` (before `updateRow`). This closes the F7 bypass (Premium adopting a Hermes model into a locked claude-cli profile).

### R7 (P2) Bench must exclude claude-cli — `LanesView.tsx` `handleBench` + `Compass.tsx`
- `Compass.tsx`: disable the per-row bench-select button (the Check toggle) when `fit.model.runtime !== "hermes"` (add to its `disabled` and a `title` like "nicht bench-bar (claude-cli)").
- `handleBench`: as defense-in-depth, filter `selected` to `m.runtime === "hermes"` before probing; if fewer than 2 remain, set a short message / no-op without starting a run.

### R9 (P2) SmokePanel CTA + KPI must use the probe-able set — `SmokePanel.tsx`
Compute `const probeable = filterSinnvoll(models).filter(m => m.runtime === "hermes");` and use `probeable` (not `filterSinnvoll(models)`) for the KPI "erreichbar" denominator, the CTA label count, and the CTA `disabled` (`probeable.length === 0`). The reachable numerator stays probes with status ok/fallback. This keeps the button honest (it already filters hermes in the handler).

### R2-failure (P2) Reveal a partial activate-then-persist failure — `LanesView.tsx` `run`
In `run`'s `catch`, after `setError(...)`, also `await reload();` so a failed op (including activate-succeeded + persist-rejected) refreshes the UI instead of leaving stale rows. (Generic and safe: onActivate/onCreate failures also just refresh.)

### R8 — NO CHANGE (verified not-a-bug)
The `key={lane.id}:${lane.updated_at ?? 0}` on `LanesPlatform` is redundant with the rows-reset effect (deps `[lane, data.profiles, models]`; `reload` produces a freshly parsed `data`, so the effect fires on every successful reload regardless of `updated_at`'s 1s granularity). Leave as-is.

## Tests (colocated; real behaviour, no tautology)
- `lanes.helpers.test.ts` / `api.test.ts`: (a) a touched row whose model was cleared (`choice:""`) but `initialChoice` was non-empty appears in the computed `removed_profiles` and is NOT in `profilesFromEditorRows` output when reasoning is unchanged; (b) the same row WITH a reasoning change IS in `profilesFromEditorRows` with `reasoning_effort:""` AND in `removed_profiles`; (c) `applyChoice(row,"",models)` yields `fallbackProviders` equal to the config chain (`defaultFallbackProviders`), not `[]`; (d) `persistPayloadFromEditorRows` retains `base_url` on fallbacks.
- backend `tests/plugins/kanban/...`: a persist test where the active lane has two profile overrides, the payload supplies one updated profile + `removed_profiles=[the other]` → after, the lane blob keeps the updated one and drops the removed one (read back via `kanban_db.get_active_lane`/`list_lanes`); and an overlap case (same profile in `profiles` with `reasoning_effort:""` and in `removed_profiles`) → config reasoning cleared AND lane entry removed.
- A focused `LaneQuickSwitch` test if feasible (changing one profile's model must NOT delete a sibling profile's override from the lane blob); if the component is too heavy to render in vitest, add a pure helper test or note the gap explicitly.

## Gates (verbatim; exit code is truth; do NOT pipe through tail) — run yourself after editing
```
cd /home/piet/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform
bash scripts/gate-frontend.sh
bash scripts/lanes-e2e.sh
```
Both must exit 0. If red, feed the exact error back for ONE fix attempt, then re-run.

## Commit (one) + report
Message: `lanes: harden save-path round 2 (full-map quick-switch, removed_profiles clear, base_url, lock-adopt, bench/smoke claude-cli, no-op fallback, failure reload)`.
Report: `git diff --stat`, the two gate/e2e exit lines verbatim, the commit SHA, the tests added, and a one-line confirmation per item R1,R3,R4,R5,R6,R7,R9,R2-failure (and an explicit "R8 no-change, verified" line). Do not change behaviour beyond these items. If a gate stays red after the single retry, report the concrete error and STOP.
