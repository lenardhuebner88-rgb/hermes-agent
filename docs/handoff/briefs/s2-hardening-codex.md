# BRIEF S2-hardening (Codex gpt-5.6-sol, effort medium, service_tier fast) — read carefully, implement EXACTLY

Work ONLY in `/home/piet/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform` (branch `claude/lanes-model-platform`). `git status --short` first; leave foreign changes alone. NO push/deploy/restart/origin/force. NO backend changes. NO shared files (i18n/de.ts, lib/types.ts, lib/schemas.ts, ControlShell, ControlPage, hooks/useControlData.ts, web/src/lib/api.ts). Token discipline: NO raw hex / `rgb()` in .tsx/.ts (ratchet gate). Use exec-direct with THIS file as the brief (do not pipe the brief through a shell pipe).

## Context
A GPT-5.6 cross-family review of the greenfield /lanes frontend traced the real save/probe/compass contracts and found behavioral regressions the green gates + E2E missed (none exercised these edge states). All findings below were independently re-verified against the source by the orchestrator; implement the prescribed fixes (line anchors are current).

## Verified findings → exact fixes

### F1 (P1) Untouched reasoning rows must NOT enter the payload — `web/src/control/views/lanes/api.ts`
`editorRows` seeds `row.reasoning` from the current `agent.reasoning_effort` (≈L740-743); `profilesFromEditorRows` then treats any concrete reasoning as an override (≈L861-870), so a plain save re-sends every reasoning-configured profile and pins its default model into the lane blob.
- Add `touched?: boolean` to `interface EditorRow` (≈L688).
- In `editorRows`, set `touched: false` on every returned row (both the `catalog.map(...)` object ≈L744 and the `extras` push ≈L771).
- In `LanesView.tsx` `updateRow` (≈L103-105) set the flag on every edit: `setRows(prev => prev.map(row => row.profile === profile ? { ...row, ...patch, touched: true } : row))` (keep the existing setDirty/setSaveError).
- In `profilesFromEditorRows`, at the top of the `for` loop add `if (!row.touched) continue;` so untouched rows emit nothing.

### F2 (P1) Reasoning "Standard" must send a clear (`""`), not be omitted — `api.ts`
Backend clears on `reasoning_effort: ""`; the control emits `null` for Standard and the helper dropped both. With the `touched` flag, send a clear on an explicit change:
- Replace the `reasoningEffort` computation (≈L861-864) with change-tracking:
  `const reasoningChanged = (row.reasoning ?? null) !== (row.defaultReasoning ?? null);`
  `const reasoningEffort = reasoningChanged ? (row.reasoning == null || row.reasoning === "" || row.reasoning === "Standard" ? "" : row.reasoning) : undefined;`
- In `hasStructuredOverride` (≈L865-870) use `reasoningChanged` in place of `reasoningEffort !== undefined`.
- In both the locked/claude-cli branch (≈L878) and the hermes branch (≈L886), include reasoning when `reasoningChanged` (value = `reasoningEffort`, which may be `""`).
- In `persistPayloadFromEditorRows` (≈L918) change the guard from `entry.reasoning_effort != null && entry.reasoning_effort !== ""` to `entry.reasoning_effort != null` so a `""` clear is forwarded.

### F3 (P1) Preserve the config fallback chain — `api.ts` `editorRows`
`fallbackProviders` is seeded from the LANE entry only (≈L755), so a reasoning-only save ships `fallback_providers: []` and the backend clears the profile's production fallback chain (lane_routes persist writes fallback_providers when not None). Seed the effective chain instead:
- ≈L755 → `fallbackProviders: runtime === "claude-cli" ? [] : cloneFallbacks(entry?.fallback_providers ?? p.fallback_providers),`
(No separate fallback-dirty flag needed: an unedited row now carries the config chain and re-sends it; a fallback edit is captured by `touched`.)

### F4 (P1) New-lane save must not overwrite the active lane — `web/src/control/views/LanesView.tsx` outer `handleSave` (≈L349-364)
Backend persist always writes the ACTIVE lane's blob; the code persists first then activates the new (empty) lane. Activate the target BEFORE persisting when it is not active:
- Move `if (!target.active) await activateLane(target.id);` to run BEFORE `const payload = ...; if (...) persistLaneModels(payload);` inside the `run(async () => {...})` body. (The staged `rows` come from the closure and are written to the now-active target.)

### F5 (P1) Exclude claude-cli from probes — `LanesView.tsx` + `ProfileMatrix.tsx`
The Hermes probe path can't run hardcoded claude-cli models (→ `config_error`), and they lead the catalog, wasting the 8-slot batch.
- `handleCatalogProbe` (≈L141): `const targets = filterSinnvoll(models).filter((m) => m.runtime === "hermes").map(...)`
- `handleProbeRow` already no-ops on missing modelId; also guard claude-cli: after computing `modelId`, `if (row.worker_runtime === "claude-cli") return;`
- `ProfileMatrix.tsx` `ProbeCell`: disable the Blitz button for claude-cli: add `const cliOnly = row.worker_runtime === "claude-cli";` and `disabled={busy || probing || cliOnly}` with `title={cliOnly ? "nicht probe-bar (claude-cli)" : t.probeMessen}`.

### F6 (P1) Compass must gate on failed probes — `web/src/control/views/lanes/fit.ts`
`scoreModelForRole` ignores the resolved probe status, so a curated model with a cached/fresh `auth_error|timeout|config_error|error` still scores (and a small failure latency can inflate the speed signal). Import `UNREACHABLE_PROBE_STATUSES` from `./api` and, right after `const probe = resolveProbe(model, probes);`, add:
`if (probe && UNREACHABLE_PROBE_STATUSES.has(probe.status)) return { score: 0, reasons: ["nicht erreichbar"] };`
(Keep the existing `knownUnreachable` claude-cli-exempt gate; this is an additional gate on probe evidence.)

### F7 (P2) Locked profiles must not be editable — `ProfileMatrix.tsx`
`row.locked` is never wired in; selecting a Hermes model on a locked/claude-cli row rewrites its runtime (api.ts ≈L872). Pass the lock into every control:
- `ModelSelect` `disabled={busy || row.locked}` (≈L244)
- `ReasoningControl` `disabled={busy || row.locked}` (≈L251)
- Fallback button (≈L261-266): add `disabled={busy || row.locked}` and `disabled:opacity-40` to its className.
- `ProbeCell`: also disable when `row.locked` (fold into the `disabled`/`cliOnly` logic above).

### F8 (P2) Save must stay retryable on failure — `LanesView.tsx` inner `handleSave` (≈L109-115)
The outer `handleSave` swallows the throw via `run`, so the inner `try` always resolves and `setDirty(false)` (≈L113) runs even on failure. DELETE that `setDirty(false)` line; on success the rows-reset effect (≈L118-123, fires after the outer `run` reload) already clears `dirty`, and on failure (no reload) `dirty` now stays true → Save remains enabled. Leave the inner `catch` (it sets `saveError` if `onSave` ever throws; harmless). The user-visible error continues to render via the outer `setError` banner.

### F9 (P2) Responsive matrix headers overlap at 600px — `ProfileMatrix.tsx`
The `tab:` (600px) 6-col grid gives Fallback 3.25rem → FALLBACK/PROBE headers overlap (confirmed in the 1440 render too at narrow widths).
- `COLS` (≈L23-24): change the `tab:grid-cols-[...]` variant to `min-[52rem]:grid-cols-[...]` and widen the Fallback track `minmax(0,3.25rem)` → `minmax(0,4.25rem)`.
- Header row (≈L211): `tab:grid` → `min-[52rem]:grid`.
- Row `<li>` (≈L225): `tab:items-center` → `min-[52rem]:items-center`.
(600–831px then keeps the readable stacked-card layout; the table appears ≥832px.)

## Tests (colocated vitest; real behaviour, no tautology)
- `fit.test.ts`: add a case — authenticated+sinnvoll true but `probe.status === "auth_error"` (and one `"timeout"`) → `scoreModelForRole(...).score === 0`.
- `lanes.helpers.test.ts`: add cases — (a) an UNTOUCHED row (`touched:false`) that has `reasoning:"medium"` seeded is OMITTED by `profilesFromEditorRows`; (b) a touched row whose reasoning changed to `null` yields `reasoning_effort === ""`; (c) `editorRows` seeds `row.fallbackProviders` from the catalog `fallback_providers` when the lane has no entry for that profile (config-chain preservation).
- `LanesView.render.test.tsx` (or a new colocated render test): a `locked` profile renders its `ModelSelect`/reasoning disabled (query the disabled control by its aria-label `Modell für <profile>`).

## Gates (verbatim; exit code is truth — do NOT pipe through tail)
```
cd /home/piet/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform
bash scripts/gate-frontend.sh                      # lint:control -> tsc -b --force -> vitest FULL -> build -> ratchet
bash scripts/lanes-e2e.sh                          # isolated Playwright E2E vs seeded worktree backend (4 tests)
```
Both must exit 0. If the ratchet trips on a literal you introduced, replace with a token utility (do not bump the baseline).

## Commit (one) + report
Commit message: `lanes: harden save/probe/compass per review (touched-tracking, fallback-preserve, activate-before-persist, locked-disable, claude-cli probe exclusion, compass probe-gate, retryable save, responsive headers)`.
Report = diff summary (files+LOC) + the two gate/e2e exit lines verbatim + the list of tests added + a one-line note per finding (F1..F9) confirming how it was addressed. Do not change behaviour beyond F1..F9.
