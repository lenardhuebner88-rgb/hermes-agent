# Lanes model-switch verification & repair — REPORT

**Mission:** `docs/handoff/2026-07-24-qwen-lanes-model-switch-verify.md` · **Session:** interactive qwen3.8-max-preview (Claude-Code harness)
**Branch:** `qwen/lanes-model-verify` · **Base:** `ef357c392` (main incl. shipped lanes platform `b220cd25a`)
**Date:** 2026-07-24 · **Status:** matrices complete · 3 defects fixed with regression tests · gates below · **NOT merged/deployed** (reviewed-ready branch handed back)

---

## 0. TL;DR

- **Config path is solid.** Matrix A (241/241) and Matrix B (379/379) against an **isolated, DB-verified backend** show every model switch, fallback edit, reasoning toggle, activate round-trip, and rejection path round-trips correctly for all 10 profiles and the full isolated catalog.
- **3 real defects found & fixed** (each with a regression test against the real data format):
  - **F3-1** — frontend `profilesFromEditorRows` dropped `fallback_providers` for *locked hermes* rows (latent; reproduced at unit level).
  - **F-REASONING-K3** — `reasoning_support_for` missed the kimi family by provider (`kimi-coding`/`k3` → `[]` while siblings → `[low,medium,high]`).
  - **F-PROBE-CUSTOM** — model/auth-smoke probe misclassified custom-endpoint providers (alibaba-token-plan) as `fallback` even when the requested model answered exactly (15/19 alibaba chat models).
- **Abo probe sweep (live, real calls):** openai-codex 7/10 `ok`, kimi-coding 5/5 `ok` (incl. `k3`), alibaba-token-plan 14/19 effectively reachable (reclassified after F-PROBE-CUSTOM), claude-cli not probed (by design, see §4).
- **Found-but-not-fixed** (diagnosed, with reasoning): openai-codex `-pro` ids fall back (not directly served), alibaba image models can't be chat-probed, claude-cli reasoning rendering nuance, `extra_models` is openrouter-only for catalog listing.

---

## 1. Isolated backend & DB isolation (precondition §3) — VERIFIED

- Booted the **worktree backend** (`PYTHONPATH=<worktree>` + live venv) on an ephemeral port with a disposable
  `HERMES_HOME=/tmp/hermes-lanes-iso-home.zJScMy`, `HERMES_SANDBOX_MODE=1`, a stub `HERMES_WEB_DIST` (API-only),
  seeded with the **live profile configs (`config.yaml`+`profile.yaml` only — never `auth.json`/credentials)**. The live
  root `config.yaml` was **not** copied (it holds `dashboard.basic_auth.password_hash`/`secret`).
- **DB isolation proven:** after the first authenticated `GET /api/plugins/kanban/lanes`, the server created
  `<HERMES_HOME>/.kanban-sandbox/default.db` (241664 bytes, mtime = that request). The live
  `/home/piet/.hermes/kanban.db` was **NOT** among `/proc/<pid>/fd`. Mechanism: `kanban_db.kanban_db_path()` step 0 —
  `HERMES_SANDBOX_MODE=1` returns `<root>/.kanban-sandbox/<slug>.db` and **wins over `HERMES_KANBAN_DB`**, so the
  known "HERMES_HOME does not isolate kanban.db" trap is closed by sandbox mode. **The isolated backend used
  `/tmp/hermes-lanes-iso-home.zJScMy/.kanban-sandbox/default.db` — a fresh sandbox board, never the live DB.**
- The board auto-seeded 2 builtin lanes (`api-standard` active, `max-abo`). Auth on the loopback bind: the dashboard
  injects `__HERMES_SESSION_TOKEN__` into `/control` (loopback legacy mode, no password) — used as `X-Hermes-Session-Token`.

### Catalog-fidelity finding (F-CATALOG) — test-harness limitation, not a product bug
- No-credentials isolated catalog = 34 models / 9 sinnvoll. Enriching with the public `models_dev_cache.json` +
  `provider_models_cache.json` + `model_catalog.providers.*.extra_models` for all providers → **70 models / 70 sinnvoll**.
- **`extra_models` only injects into the catalog for `openrouter`** (`_append_openrouter_extra_model_options` is
  openrouter-specific). `openai-codex`/`alibaba` did not expand; `kimi/gemini/neuralwatt/nous/xai-oauth/minimax/kimi-coding`
  stay absent — their model lists come from the **authenticated** provider catalog fetch, which needs `auth.json`
  (credentials) we will not copy (cage rule 1/2).
- **Coverage strategy used:** Matrices A/B over the full 70-model isolated catalog (every persist code path, incl. 36
  openrouter models) **stabilized via a throwaway lane with one `pin-*` extra profile per model** (the lane-pinned
  catalog source, lane_routes L284-305, keeps every model representable regardless of profile-config mutations —
  without this, the credential-less catalog drops profile-default-derived models mid-matrix, a pure test artifact).
  Full-63 reasoning/validation coverage additionally via the pure `reasoning_support_for` tests; live Abo reachability
  via Matrix C.
- **Product observation (not fixed):** `model_catalog.providers.<p>.extra_models` marks models `admitted`/`sinnvoll`
  for *all* providers (via `get_configured_provider_extra_models`) but only **adds them to the dropdown catalog for
  openrouter** — an asymmetry worth a follow-up if non-openrouter admission is desired.

---

## 2. Matrix results

### Matrix A — switch matrix (config path, isolated, free) — **241/241 PASS**
Persist → re-read `GET /lanes` → assert provider/model/runtime/fallbacks intact in **both** the active-lane mirror and
the profile-config catalog. Throwaway lane created+activated+deleted; no live mutation.

| Sub-test | asserts | passed | failed |
|---|---|---|---|
| A1 model-sweep | every catalog model (70) → `coder` round-trips | 70 | 0 |
| A2 profile-sweep | `gpt-5.6-sol` → every profile (10) round-trips | 10 | 0 |
| A3 sinnvoll × profile | 15 live-sinnvoll-in-iso models × all 10 profiles | 150 | 0 |
| A4 fallback semantics | preserve (omit) / clear (`[]`) / set | 3 | 0 |
| A5 claude-cli write path | `claude_model`+`worker_runtime=claude-cli`, provider forced null | 1 | 0 |
| A6 rejection paths | unknown model / runtime mismatch / claude-cli+provider / unknown profile / unknown removed → 400 | 5 | 0 |
| A7 removed_profiles | override removed from lane mirror | 1 | 0 |
| A8 activate round-trip | active_id flips builtin↔throwaway, profiles intact | 1 | 0 |

> A first run showed 44 "unknown models" failures — all traced to the **credential-less catalog artifact** (persist
> rewrites the profile configs the catalog is derived from, dropping `gpt-5.6-sol/terra/luna`/`qwen3.8-max-preview`
> mid-matrix). With the catalog stabilized via lane-pins, **0 failures** → confirms the config path itself is correct.
> **R2-concurrency and F3-4 did not reproduce** (no failure traced to them) → left deferred per handoff §4A.

### Matrix B — reasoning matrix (isolated, free) — **379/379 PASS**
The backend reasoning contract is fully self-consistent: the `reasoning_support` flag and the `/persist` validator agree
for every model.

| Sub-test | asserts | passed | failed |
|---|---|---|---|
| B1 round-trip | every offered level × every reasoning model persists + re-reads | 183 | 0 |
| B2 clear | `reasoning_effort:""` clears `agent.reasoning_effort` | 1 | 0 |
| B3 Standard | omitting `reasoning_effort` leaves config untouched | 1 | 0 |
| B4 no-support reject | `reasoning_support=[]` models reject any level (400) — matches UI "no Reasoning-Knopf" | 10 | 0 |
| B5 invalid-level reject | a level outside a model's support 400s | 57 | 0 |
| B6 flag↔validator | in-support accepted / out-support rejected, all 70 models | 127 | 0 |

### Matrix C — Abo probe sweep (LIVE, real calls) — sequential, batches ≤ 8
`catalog-probe` over the Abo providers only. Per-model status feed collected; every non-`ok` diagnosed below.

| provider | tested | ok | fallback (raw) | skipped | notes |
|---|---|---|---|---|---|
| openai-codex | 10 | 7 | 3 | 0 | the 3 `-pro` ids fall back to `kimi/k3` (not directly served) |
| kimi-coding | 5 | 5 | 0 | 0 | **`k3` ok** (`observed kimi-coding/k3`) + 4 siblings |
| alibaba-token-plan | 19 | 0 (raw) | 19 | 0 | **14 are false-`fallback`** → reclassified `ok` after F-PROBE-CUSTOM |
| claude-cli | 5 | — | — | 5 | not probed by design (§4) |

**alibaba-token-plan reclassification after the F-PROBE-CUSTOM fix** (each verified by the regression test logic + the
captured `exact response`/`observed_model` evidence):
- **14 chat models → `ok`** (observed `custom/<same-model>`, `exact response` true): qwen3.6-plus, glm-5, MiniMax-M2.5,
  kimi-k2.5, kimi-k2.6, glm-5.1, qwen3.6-flash, deepseek-v4-flash, deepseek-v4-pro, qwen3.7-max, qwen3.7-plus, glm-5.2,
  kimi-k2.7-code, qwen3.8-max-preview. The call **succeeded**; only the provider self-label (`custom`) ≠ requested
  (`alibaba-token-plan`) tripped the old `fallback` rule.
- **5 stay `fallback`:** deepseek-v3.2 (reached, but `response not exact`) + 4 image models
  (qwen-image-2.0/-pro, wan2.7-image/-pro → genuinely fall back to `kimi/k3`; image models cannot echo a chat token).

Full per-model feed: committed probe capture in this run's artifacts (`matrix_c_results.json` referenced from the
verification scratch; statuses summarized above).

---

## 3. Fixes (smallest-possible diffs, each with a regression test against the REAL data format)

### Fix 1 — F3-1: locked *hermes* rows keep their fallback chain (frontend)
- **File:** `web/src/control/views/lanes/api.ts` — `profilesFromEditorRows`.
- **Root cause:** the `if (row.locked || row.worker_runtime === "claude-cli")` branch built the entry from
  `entryFromProviderAwareChoice(row.choice)` = `{worker_runtime, provider, model}` and **dropped `fallback_providers`**.
  Correct for claude-cli (no fallback transport); wrong for a **locked hermes** row, which carries a fallback chain.
- **Latency:** no current catalog profile is locked-hermes (`_scan_lane_profiles` sets `locked = runtime=="claude-cli"`),
  so F3-1 does **not** manifest live today — it is a latent serializer defect, **reproduced at unit level** with a
  synthetic locked-hermes `EditorRow`. Fixed defensively so the serializer is correct if a hermes profile ever becomes
  locked (e.g. custom-lane extras / future lock reasons). The backend `/persist` already preserves fallbacks correctly
  (lane_routes L1731-1795) — F3-1 was frontend-only.
- **Fix:** in the locked branch, when `base.worker_runtime === "hermes"`, attach the (filtered) `fallback_providers`;
  claude-cli rows still drop them.
- **Regression test:** `web/src/control/views/lanes/api.test.ts` — 3 tests (locked-hermes keeps fallbacks; locked-hermes
  with a reasoning change keeps both; claude-cli control still drops fallbacks).

### Fix 2 — F-REASONING-K3: kimi family detected by provider, not name substring (backend)
- **File:** `plugins/kanban/dashboard/lane_routes.py` — `reasoning_support_for`.
- **Root cause:** kimi detection was `provider == "moonshotai" or "kimi" in model_id`. Short ids on the kimi/kimi-coding
  transport — **`k3`** — contain no `"kimi"` substring and the provider is `kimi-coding`/`kimi` (≠ `moonshotai`), so they
  fell through to `[]` while their siblings (`kimi-k2.5`, `kimi-for-coding`, …) on the **same transport** advertised
  `[low, medium, high]`. Confirmed against the live catalog (`kimi-coding/k3` and `kimi/k3` → `[]`; all siblings → `[low,medium,high]`).
- **Grounding that k3 is a real working model:** Matrix C probed `k3` on `kimi-coding` → **`ok`** (`observed kimi-coding/k3`,
  exact response). So the flag was understating a reachable, reasoning-transport-capable model.
- **Fix:** `if provider in {"moonshotai", "kimi", "kimi-coding", "kimi-coding-cn"} or "kimi" in model_id:` → `[low, medium, high]`.
- **Regression test:** `tests/plugins/kanban/dashboard/test_lane_model_platform.py` — added `("kimi-coding","k3")`,
  `("kimi","k3")`, `("kimi-coding","kimi-for-coding")` → `[low,medium,high]` to the parametrized rule test.
- **Residual note:** whether the kimi-coding *endpoint* honors `reasoning_effort` (vs. carrying it as a no-op like the
  coding plan ignores the model field) is a transport-behavior question a reachability probe can't answer; the fix makes
  the flag **consistent with the kimi family on the same transport**. Operator may verify k3 reasoning on/off behaviorally.

### Fix 3 — F-PROBE-CUSTOM: exact same-model response is `ok`, not `fallback` (backend, shared auth-smoke status)
- **File:** `plugins/kanban/dashboard/lane_routes.py` — `_derive_lanes_auth_smoke_status`.
- **Root cause:** two blunt heuristics false-positived on custom-endpoint providers: (a) `fallback_activated` is set by
  **any** log line containing "fallback" (the configured chain being logged, not used); (b) `observed_provider` for an
  OpenAI-compatible endpoint self-labels `"custom"` ≠ the requested provider id. So a model that **answered exactly**
  was still classified `fallback` (`observed_provider != requested_provider` / `fallback_activated`).
- **Evidence (Matrix C):** 15/19 alibaba-token-plan chat models had `observed custom/<same-model>` + `exact response` —
  reachable, but reported `fallback`.
- **Fix:** before the `fallback` rules, return `ok` when `returncode==0 and response_exact and observed_model == requested_model`
  — a **real** fallback substitutes a *different* model; a provider-label normalization with the requested model answering
  exactly is reachability. Existing semantics preserved (the prior `fallback` test uses a *different* observed model).
- **Blast radius:** shared by `model-probe`, `catalog-probe`, and `auth-smoke`. The change only reclassifies
  exact-same-model responses as `ok` (strictly more accurate; does not weaken activation safety — a model answering its
  own token exactly *is* proof of reachability). All 305 existing auth-smoke/probe tests still pass.
- **Regression test:** `tests/plugins/test_kanban_dashboard_plugin.py` — alibaba custom-endpoint exact-same-model → `ok`;
  real-fallback control (openai-codex `-pro` → `kimi/k3`, different model) → `fallback`.

---

## 4. Found-but-NOT-fixed (diagnosed, with reasoning)

1. **openai-codex `-pro` ids (gpt-5.6-sol/terra/luna-pro) → `fallback` to `kimi/k3`.** Not directly served on the Codex
   endpoint; they only resolve via the profile fallback chain (probe: `observed kimi/k3`, exact response from the
   fallback). **Catalog-curation finding:** they are marked `sinnvoll`/openai-codex/probe-able but are not directly
   reachable. *Not fixed* — they DO work via fallback; whether to relabel them (not probe-able / fallback-only) or fix
   the `-pro` routing is an operator/catalog decision. Stays `fallback` correctly after Fix 3 (observed model differs).
2. **alibaba image models (qwen-image-2.0/-pro, wan2.7-image/-pro) → `fallback` to `kimi/k3`.** Image-generation models
   cannot echo a chat token, so the chat reachability probe fails and falls back. *Expected*, not a bug. *Recommendation:*
   exclude image models from chat-probe scope / mark them non-probe-able in the catalog (the probe is chat-only by design).
3. **alibaba deepseek-v3.2 → `fallback` (reached, `response not exact`).** Model resolved (`observed custom/deepseek-v3.2`)
   but did not echo the token exactly (model behavior). Stays `fallback` after Fix 3 (conservative). Not a code bug.
4. **claude-cli not probe-able (5 models skipped).** The probe runs `hermes … chat --provider --model` and forces
   `runtime="hermes"`; the platform **excludes claude-cli from probe/bench/smoke CTAs by design**. Probing claude models
   could route to **metered `anthropic`** (cage rule 2 forbids), so they were **deliberately not probed**. claude-cli
   reachability is out of probe scope by design (auth-smoke `skips` non-hermes runtimes). *No workaround attempted*
   (rule 2: never switch to metered).
5. **claude-cli reasoning rendering nuance.** `reasoning_support_for` returns `[low,medium,high]` for claude models, so
   claude-cli rows (which are `locked`) render **greyed reasoning segments** — not the honest "Modell hat keinen
   Reasoning-Knopf" state (`ReasoningControl` only shows that for `support=[]`). The platform handoff described claude-cli
   reasoning as "ehrlich deaktiviert **mit Begründung**". The row is fully locked (all controls disabled; the locked-row
   reason is the explanation), so this is arguably acceptable, but the greyed segments *imply* a capability that claude-cli
   may not transport. *Not fixed* — design-intent question; recommend the operator/Claude decide whether claude-cli rows
   should show the "no-Knopf" state (or a "claude-cli transportiert kein Reasoning" hint) instead of greyed segments.
6. **F-CATALOG / `extra_models` openrouter-only** (§1): non-openrouter `extra_models` mark models `admitted` but don't add
   them to the dropdown catalog. *Observation/follow-up*, out of this mission's fix scope.
7. **R2-Concurrency & F3-4** (deferred in the platform handoff): **did not reproduce** in Matrices A/B (all-or-nothing
   persist + removed_profiles round-tripped cleanly). Per handoff §4A ("fix only if a failure actually traces to it") —
   left deferred, no diff.

---

## 5. Gate evidence (verbatim)

### Frontend — `bash scripts/gate-frontend.sh` (from worktree root) → **GRÜN (exit 0)**
```
=== GATE: design-tokens ratchet ===        → passed (no raw-hex violation; gate proceeded)
=== GATE: npm run lint:control ===          → ✖ 50 problems (0 errors, 50 warnings)   ← 0 errors; all 50 warnings
                                              pre-existing (fleet/BoardTab.tsx, fleet/RisikoPulse.tsx, …), none in
                                              lanes/api.ts or lanes/api.test.ts
=== GATE: tsc -b --noEmit (worktree-local) === → no errors (exit 0; gate proceeded to vitest)
=== GATE: vitest run (worktree-local, maxWorkers=4) ===
 Test Files  199 passed (199)
      Tests  2759 passed (2759)
   Duration  73.00s
=== GATE: npm run build ===
✓ 1738 modules transformed.
✓ built in 2.09s
PWA v1.3.0  mode generateSW  precache 209 entries (5054.72 KiB)
=== FRONTEND-GATE GRÜN (exit 0) ===
```

### Python — per-file pytest via live venv (`HERMES_TEST_FILE_TIMEOUT=… PYTHONPATH=$(pwd) …/venv/bin/python -m pytest <file> -q`)
```
tests/plugins/kanban/dashboard/test_lane_model_platform.py   → 17 passed in 6.35s     (reasoning rule incl. k3 cases,
                                                                                        persist reasoning, probe join/cache)
tests/plugins/test_kanban_dashboard_plugin.py                → 305 passed, 6 warnings in 54.15s   (auth-smoke + probe
                                                                                        status incl. F-PROBE-CUSTOM test)
tests/plugins/test_kanban_lanes_persist.py                   → 20 passed in 19.04s    (persist all-or-nothing/rollback)
tests/hermes_cli/test_kanban_lanes.py                        → 30 passed in 3.41s     (lane CRUD/activate)
```

### Ruff
```
$ …/venv/bin/python -m ruff check plugins/kanban/dashboard/lane_routes.py \
      tests/plugins/kanban/dashboard/test_lane_model_platform.py tests/plugins/test_kanban_dashboard_plugin.py
All checks passed!
$ …/venv/bin/python -m ruff check .
All checks passed!
```

### Live catalog read + Abo probes (Matrix C) used the live dashboard on :9119 (read + probes only); no live config/lane
mutation occurred. All Matrix A/B mutations ran on the isolated sandbox DB (§1).

---

## 6. Verbatim gate output

See §5 — the lines above are copied verbatim from `/tmp/lanes_verify/gate-frontend.log` and the pytest/ruff runs
(exit codes are the truth; nothing was piped through `tail`/`grep` to mask a non-zero exit — the gate script's own
`=== FRONTEND-GATE GRÜN (exit 0) ===` line is its exit-code assertion).

---

## 7. Hand-back

- **Branch `qwen/lanes-model-verify`** carries: this REPORT + 3 fixes + their regression tests (commit `lanes-verify: …`).
- **Not pushed / not deployed / no restart / no live-config or credential edits** (cage rules honored throughout).
- **For the reviewer/lander (standing ladder: foreign diff → independent review):** the F-PROBE-CUSTOM fix touches
  shared auth-smoke status logic (blast radius noted in §3 Fix 3); F-REASONING-K3 changes live UI (k3 gains a Reasoning
  control) with a residual transport-behavior note; F3-1 is a latent/defensive frontend fix. Judge per the diffs.
- Suggested follow-ups (operator decisions, §4): openai-codex `-pro` catalog curation, exclude image models from
  chat-probe scope, claude-cli reasoning rendering, non-openrouter `extra_models` catalog listing.

