---
title: Langfuse / Quota Leitstand — 8/10 Design + One-to-One Implementation Plan (v2)
date: 2026-07-22
status: v2 — folded Codex read-only BLOCK (re-located source); re-review PENDING · NOT built · confined to worktree codex-bibliothek-p6b-unknowns-qwen
base: auditor code-map + Codex read-only grounding review (2026-07-22, re-located line refs) + vault mockups 01..04 + web/src/control/DESIGN.md
next-gate: re-dispatch Codex read-only → must return PASS before render/build (§13)
binding-design: web/src/control/DESIGN.md ("Bronze auf Graphit") + theme.css · Bausteine components/leitstand/
read-model: aligns docs/adr/0001-bibliothek-provenance-read-model.md (present, never mutate upstream)
---

# Langfuse / Quota Leitstand — 8/10 Design + One-to-One Plan (v2)

> v2 folds the Codex read-only BLOCK. Line references below are the **re-located** ones from that
> review (trust the file; the v1 auditor hints were partly stale). Every visual element is bound 1:1 to
> a real field or explicitly tagged with a COVERAGE state, so nothing aspirational reads as live.
> Confined to this worktree; the Bibliothek WIP is untouched and independent.

## Revision note (v1 → v2, from Codex BLOCK)
- **Three-way outcome model introduced** (§3 legend Oa/Ob/Oc); v1 conflated them → the UNBACKED-LIVE row.
- **Good news folded:** per-task `metadata.subscription`+`billing_mode` stamped at *claim time*
  (`kanban_db.py:6662-6673`, `:9539-9558`) → lane/billing attribution = LIVE; `task_runs.outcome`
  exists (`:2111-2114`) → per-task outcome chip = LIVE; M3 narrows to the autoresearch skip-log.
- **Anteil denominator defined honestly** (no per-window token-total field exists): share is over
  *traced day-tokens*, decoupled from the account-usage window gauge.
- **M1–M5 sites repaired** (M1 wraps `run_turn`; M3 covers all writers + nightly scripts; M4 merges
  claim metadata + full pipeline plumbing; M5 at metric producers).
- **Langfuse READ contract added** (service-gated, fail-soft) — the Trace view had no read path at all.
- **Exact DESIGN.md MOCK marker** (`:135-138`) mandated via a shared component; `pickBottleneck(providers)`;
  `subscription_token_burn` = outcome (not verdict) for the LIVE scorecard substitute.

## 0. The 6/10 → 8/10 delta (why)

| # | 6/10 tell | code proof (re-located) | 8/10 fix |
|---|---|---|---|
| C1 | Codex lane scored but untraced → black hole vs tank | `codex_runtime.py:325-492` builds/accumulates usage, no lifecycle hooks | Coverage rail; Codex = honest "Traces fehlen · S1 offen" (M1) |
| C2 | Skip panel + cleanliness unbacked | `autoresearch_runs.py:82-106` no outcome/skip; cleanliness not at producers | Skip via Ob (M3); cleanliness via producers (M5) |
| C3 | Anteil no honest denominator; cache tokens missing | no per-window token-total field (`account_usage.py:32-41`); `task_runs:3628-3631` no cache cols | Anteil = share of traced day-tokens (PARTIAL, labelled); window gauge separate (M4) |
| C4 | No provenance; Trace hero mixes sources, no join/read path | langfuse metadata `__init__.py:603-617` lacks join/billing; NO read contract | ProvenanceChip; M2a write + M2b read adapter |
| C5 | Pick-2 (Inspect) shown as live | unbuilt (HANDOFF order 1→2) | exact DESIGN.md MOCK marker (`:135-138`) |
| C6 | Write CTAs in a read view | ADR-0001 read-model; lane routing gated | CTAs out; link to lane-routing surface |
| C7 | Dead "$0.00" prominence; empty canvas | puls-leiste keeps 4 globals | route-local `pickBottleneck(providers)`; canvas = coverage rail |

Note (good news, not a gap): lane/billing attribution is **already** stamped at claim time
(`kanban_db.py:6662-6673`, `:9539-9558`) and `task_runs.outcome` exists (`:2111-2114`) — so the per-task
lane label + outcome chip are LIVE today; the gaps are cache-token completeness (M4), the autoresearch
skip-log (M3), and the untraced Codex lane (M1).

## 1. Guiding invariants (the 8/10 spine)

- **INV-1 Read-model only.** Views present + drill; never mutate. Lane-set / eval-rerun live on the
  lane-routing + eval surfaces (gated), linked-to, not embedded. (ADR-0001.)
- **INV-2 Provenance on every number.** Each value carries
  `{ source, denominator?, freshness(signal_at|fetched_at), coverage, cleanliness }`.
- **INV-3 Coverage is first-class.** Per lane three channels — Tank (`account_usage`), Traces (langfuse
  read adapter), Eval (inspect) — each with an honest state. Tank-green + traces-absent = "instrumentierung
  offen", never a fake span.
- **INV-3b Outcome model — never conflate.** (Oa) operational task-run outcome = `task_runs.outcome`
  [LIVE]; (Ob) autoresearch run-history outcome/skip = `append_run` record [GAP→M3]; (Oc) eval verdict =
  Inspect [PHASE2]. Each row binds exactly one.
- **INV-4 Honest attribution ratio.** The account-usage window gauge (`used_percent`) and the per-task
  Anteil are **separate metrics**. Anteil = `task_tokens ÷ Σ traced_tokens_in_selected_scope` (a derived
  ratio over `task_runs`), always labelled "Anteil am getraceten Verbrauch" — NEVER a share of the
  account-usage window (that window has no token-total field). PARTIAL until M1 (untraced Codex) + M4 (cache).
- **INV-5 Build-phase honesty.** `coverage ∈ { LIVE, PARTIAL, PHASE2, GAP }`. PHASE2/MOCK use the EXACT
  DESIGN.md preview marker (§2), shared component with the Jarvis graph-mock.
- **INV-6 Binding language.** Tokens only (ratchet: no raw hex in `.tsx`); bronze = interactive/live only;
  lane = `--color-data-N` dot + name; mono = data only; status = LED + label; money in `$`; global
  puls-leiste 4 instruments preserved + a route-local sub-instrument row permitted.

## 2. Coverage vocabulary + shared components

`ProvenanceChip` / `CoverageRail` / `MockPreviewMarker` — new atoms in `components/leitstand/` (extend):

| coverage | rendered as | meaning |
|---|---|---|
| `LIVE` | plain value + optional `signal_at` age | measured from a real endpoint |
| `PARTIAL` | value + quiet `· ohne Cache-Tokens` / `· getraceter Anteil` / `· abgeleitet` (ink-3, no status color) | computed on incomplete instrumentation |
| `GAP→S<n>` | dashed outline + `belegt ab S<n>` (ink-3), links to slice | endpoint/field absent |
| `PHASE2` | **MockPreviewMarker** (see below) | needs Pick-2 / Inspect |

**MockPreviewMarker (binding):** reuse the EXACT convention of `DESIGN.md:135-138` (the Jarvis
graph-mock preview label): desktop `… · VORSCHAU — MOCK-DATEN · …`, mobile `· …: Vorschau (Mock)`, as ONE
shared component used by both Jarvis and the Quota/Trace/Scorecard/Reflect PHASE2 surfaces — do NOT invent
a new marker string. No ok-green on a PHASE2/MOCK value.

`CoverageRail`: per lane three LEDs (Tank / Traces / Eval) + labels; Tank=ok & Traces=absent = the visible
Codex hole. State derivation pure + unit-tested in `lib/derive.ts` against REAL payloads.

## 3. Data contract — element → real field (1:1; Codex re-checks this)

Outcome legend: **(Oa)** `task_runs.outcome` (LIVE) · **(Ob)** autoresearch `append_run` outcome/skip (GAP→M3) ·
**(Oc)** Inspect eval verdict (PHASE2). `src` = re-located file:line. `cov` = state today.

### View 1 — Quota (`/control/quota`)
| element | source | cov |
|---|---|---|
| KPI window gauge `%` | `AccountUsageWindow.used_percent @ agent/account_usage.py:35` | LIVE |
| KPI window label/reset/age | `window_key @ :41`, `label @ :34`, `reset_at @ :36`, `signal_at @ :51` | LIVE |
| route-local bottleneck sub-instrument | `pickBottleneck(providers[, config]) @ web/src/control/lib/accountUsage.ts:80-105` | LIVE |
| per-task row · task id/name | `task_runs.task_id @ kanban_db.py:2099` + title | LIVE |
| per-task row · lane/billing | `metadata.subscription @ :6662-6673` + `metadata.billing_mode @ :9539-9558` (claim-time) | LIVE |
| per-task row · tokens in/out | `input_tokens/output_tokens @ :3628-3631` | LIVE |
| per-task row · **Anteil %** | DERIVED `row_traced_tokens ÷ Σ traced_tokens(scope)` over `task_runs` (INV-4) | PARTIAL |
| per-task row · outcome chip | `task_runs.outcome @ :2111-2114` (**Oa**) | LIVE |
| Skip panel rows | (**Ob**) NEW read endpoint over autoresearch run-history outcome/skip | GAP→S2 |
| Coverage rail | tank=`account_usage`; traces=langfuse read adapter presence/lane; eval=PHASE2 | mixed |

### View 2 — Trace (`/control/quota/:task`)
| element | source | cov |
|---|---|---|
| waterfall spans + hero usage | Langfuse READ adapter (M2b) reading generations `__init__.py:895/:1063` | GAP→S1r |
| generation ↔ run join | `task_run_id` in langfuse metadata (M2a) | GAP→S1r |
| fallback hero (no trace) | `task_runs` in/out + outcome (Oa) | PARTIAL |
| un-instrumented-lane state | honest empty (DESIGN.md empty-doctrine): "Keine Traces — Lane nicht instrumentiert (S1)" | LIVE-empty |
| eval verdict / self-report↔eval | (**Oc**) | PHASE2 (MockPreviewMarker) |

### View 3 — Scorecard (`/control/lanes` eval tab)
| element | source | cov |
|---|---|---|
| LIVE substitute row | **outcome** distribution + token share per lane from `subscription_token_burn @ kanban_db.py:30363-30380` (outcome, NOT verdict) | LIVE, labelled "vorläufig · Outcome-basiert" |
| eval scores / overall / trend | (**Oc**) nightly Inspect | PHASE2 (MockPreviewMarker) |
| "Lane setzen / Eval neu fahren" CTAs | — | removed (INV-1) |

### View 4 — Reflect (`/control/stratege`)
| element | source | cov |
|---|---|---|
| "Runs mit Outcome" KPI (relabelled; was "Tasks beurteilt") | count `task_runs.outcome IS NOT NULL` (**Oa**) = closed attempts, NOT judged | LIVE (relabelled) |
| "Tasks evaluiert" KPI | (**Oc**) | PHASE2 (MockPreviewMarker) |
| Self-Report ↔ Eval divergent | (**Oa** vs **Oc**) | PHASE2 |
| Budget-Skips geloggt | (**Ob**) same endpoint as View 1 (single source of truth) | GAP→S2 |
| "Metriken sauber" gauge | per-metric producer cleanliness+source_kind (M5) | GAP→S4 |

UNBACKED-LIVE after v2: **empty** (the scorecard substitute is outcome-based = backed; the judged-task KPI
is PHASE2 or relabelled; Anteil is explicitly PARTIAL).

## 4. Backend MUST-ADD (re-located sites; closes gaps so views become truthful)

All additive / expand-contract; DB changes take a backup per the migration gate. Each: test vs REAL data +
fail-soft where instrumentation is involved.

- **M1 — Codex lane visibility.** `agent/codex_runtime.py`: fire `invoke_hook("pre_api_request")` **before**
  `agent._codex_session.run_turn` at `:414`, and `invoke_hook("post_api_request")` **post-response** near
  `:490-492` (NOT both after the accumulation). Propagate `task_id`/`chain_id`/`subscription`/`billing_mode`
  into the hook kwargs. Wrap each in `try/except: pass` (fail-soft, mirrors `account_usage.py:907`).
  Done-when: a codex run yields ≥1 langfuse generation; langfuse-down ⇒ run completes (test). Closes C1.
- **M2a — langfuse write metadata + propagation.** `plugins/observability/langfuse/__init__.py:603-617`: add
  `billing_mode` (from `resolve_billing_route @ usage_pricing.py:641`, openai-codex→`subscription_included @ :655`),
  `subscription`, `chain_id`/`epic_id`, **`task_run_id`**; propagate from the task context into the span. Closes C4 write-side + the Trace join key.
- **M2b — Langfuse READ contract (NEW).** A **service-gated** read adapter + route, e.g.
  `GET /api/quota/trace/<task_id>` that fetches a task's generations by `task_run_id`/`task_id`; gated on
  langfuse configured+reachable; **fail-soft** → returns `coverage=absent` so View 2 renders the honest empty
  state (no fake spans). zod schema + `useQuotaTrace()` hook. Closes C4 read-side (the gap v1 missed entirely).
- **M3 — autoresearch skip/outcome (Ob).** Add `outcome`(+`skip_reason`) to `append_run() @ autoresearch_runs.py:82`
  and pass from **every** writer: `autoresearch_proposals.py:1956` (`result["outcome"]`/`result["budget_stop"] @ :1932`,
  classify via `autoresearch_lane_contracts.py:291`) **and** the nightly quota short-circuits
  `scripts/autoresearch_nightly.py:172-175` + `scripts/autoresearch_v2_nightly.py:76-90,539-568`, and the other current callers `test_foundry`, `deep_audit`, `run_autoresearch_request`. **Lane-mapping trap (Codex re-review):** nightly `test-foundry` must map to the canonical history lane `test` (or expand `_VALID_LANES`), otherwise it silently falls back to `skill` and the skip is misattributed. Schema additive
  (expand-contract). Done-when: a budget-skip is queryable with reason; View 1/4 skip panels go LIVE. Closes C2-skip.
- **M4 — per-task cache tokens + end-run metadata MERGE.** `kanban_db.py` `task_runs` near `:3628-3631`: add
  `cache_read_tokens`/`cache_write_tokens`/`reasoning_tokens` (additive migration + backup). At end-run, **merge**
  with the existing claim-time metadata at `:8407-8438` (do NOT clobber `subscription`/`billing_mode` stamped at
  `:6662/:9539`). Plumb the new columns through schema, the `Run` dataclass, backfill, end-run, serializers, and the
  aggregates (`chain_cost_breakdown`, `subscription_token_burn`). Done-when: Anteil includes cache tokens and the
  lane/billing attribution survives end-run; PARTIAL→LIVE for the token side. Closes C3 token-completeness.
- **M5 — metric cleanliness at PRODUCERS.** Annotate each metric **producer** in
  `hermes_cli/vision_metrics.py:1810-1847` with `cleanliness ∈ {clean, derived, unknown}` + `source_kind`
  (clean = eval/`roots_by_class`-backed; derived = `by_class` fallback). The strategist `roots_by_class` fallback at
  `strategist.py:1024-1032` is a CONSUMER and cannot establish per-metric cleanliness. Bump SCHEMA_VERSION.
  Done-when: View-4 gauge segments carry a real producer flag. Closes C2-clean.

## 5. Read-only endpoints (reuse-first, no writes; INV-1)

Reuse `GET /api/account-usage @ web_server.py:4587-4596`,
`GET /api/plugins/kanban/runs/{costs,costs-series,subscription-burn} @ plugins/kanban/dashboard/operations_routes.py:700-746`,
`GET /api/plugins/kanban/tasks/{id}/chain-costs @ plugins/kanban/dashboard/planspec_flow_routes.py:1629-1658`.
Add (all read-only, no POST/PUT/DELETE):
- `GET /api/quota/attribution?scope=<day|…>&since=…` → per-task rows
  `{ task_id, lane, billing_mode, in, out, cache?, anteil, anteil_denominator_scope, outcome(Oa), provenance, coverage }`,
  Anteil computed as the INV-4 derived ratio. zod `parseOrThrow`, `useQuotaAttribution()` polling hook in
  `hooks/costsUsage.ts`.
- `GET /api/quota/skips?since=…` over (**Ob**) once M3 lands (GAP→S2 placeholder until then).
- `GET /api/quota/trace/<task_id>` = the M2b service-gated read adapter (fail-soft → coverage=absent).

## 6. Slice sequencing (small, gated; hermes-dashboard-dev 7-step + DB rules)

| slice | content | done-when |
|---|---|---|
| S1 | M1 (codex hooks at :414/:490-492 + context propagation, fail-soft) | codex run traced; fail-soft test green |
| S1r | M2a (write metadata+join key) + M2b (service-gated read adapter/route, fail-soft) | trace readable by task; absent ⇒ coverage=absent |
| S2 | M3 (Ob across ALL writers + both nightly scripts) | skip queryable with reason |
| S3 | M4 (cache cols + end-run MERGE preserving claim metadata + full pipeline plumbing) | Anteil incl. cache; attribution survives end-run |
| S4 | M5 (producer cleanliness+source_kind at vision_metrics:1810-1847) | gauge flags real |
| S5 | endpoint attribution+skips + zod + hook + **View 1** (coverage rail, provenance; Oa chip LIVE; lane LIVE; Anteil PARTIAL labelled; skip GAP→S2) | gate-frontend + visual-verify green |
| S6 | **View 2** via S1r + honest empty/un-instrumented state | empty-state test for Codex lane |
| S7 | **View 3** (LIVE outcome-based substitute + PHASE2 scorecard w/ MockPreviewMarker) + **View 4** (relabelled KPIs, PHASE2 divergence, M5 gauge) | no eval value reads as live; UNBACKED-LIVE empty |

Pick-2 (Inspect) is a **separate track**, NOT in this plan's done-when; S7 wires markers + LIVE substitutes only.

## 7. Test strategy
- Vitest kolokalisiert vs REAL payloads: INV-4 Anteil math + denominator label, coverage-state derivation
  `{all-live, partial, codex-hole, phase2}`, Oa/Ob/Oc routing, divergence detection.
- zod `parseOrThrow` → backend shape-drift fails loud.
- `scripts/gate-frontend.sh` (lint:control → tsc → vitest → build); exit = truth; no `| tail`.
- `scripts/visual-verify.sh` (disposable HERMES_HOME, 390/820/desktop; overflow+console-error = red; never live
  `:9119`) — assert coverage rail shows the Codex hole + MockPreviewMarker renders the exact DESIGN string.
- Python fail-soft: langfuse down ⇒ agent + codex runtime run; M2b absent ⇒ View 2 empty state (not error).

## 8. Done-when (whole plan)
UNBACKED-LIVE empty; Oa/Ob/Oc never conflated; Codex-lane hole a visible honest state; Anteil denominator
explicit + labelled + decoupled from the window gauge; M1 wraps run_turn; M4 preserves claim metadata;
M5 at producers; PHASE2 uses the exact DESIGN.md MockPreviewMarker via a shared component; no write CTA in
read views; `gate-frontend.sh` + `visual-verify.sh` green at 390/820/desktop; no raw hex in `.tsx`.

## 9. Out of scope / non-goals
Pick-2 Inspect live · mutating lanes/eval from these views · touching the Bibliothek WIP · push/deploy
(operator-gated, piet-fork ff-only) · editing the live checkout.

## 10. Open questions for operator
1. M4 DB migration (additive + backup) inside this chain, or its own gated deploy?
2. Confirm the M2b read-adapter auth/service-gating model (langfuse keys in `~/.hermes/.env`, loopback).
3. Confirm rendering the 4 improved 8/10 mockups (delegated UI builder, §12) before S5.

## 11. §12 Producing the visible 8/10 mockups
After §13 PASS: render the four improved mockups (coverage rail, ProvenanceChip, INV-4 labelled Anteil, honest
Codex-hole + MockPreviewMarker, no write CTAs, route-local bottleneck sub-instrument) via a foreign UI builder
from THIS spec + DESIGN.md; orchestrator reviews visually. Output to `docs/langfuse-quota/mockups/` (worktree),
never overwriting the vault 6/10 set.

## 13. Codex re-review (next gate — must PASS)
Re-run the read-only grounding check (`docs/langfuse-quota/codex-review-brief.md`) against THIS v2. Confirm:
- the three-way outcome model (Oa/Ob/Oc) is applied to every row; UNBACKED-LIVE is empty;
- re-located sites in §4 (M1 :414/:490-492; M3 all writers + nightly scripts; M4 merge at :8407-8438; M5 producers
  at vision_metrics:1810-1847) are correct + additive;
- M2b read contract + INV-4 denominator are present and honest;
- §5 routes reuse the cited paths and add NO write verb; INV-1..INV-6 hold (esp. MockPreviewMarker = DESIGN.md:135-138,
  ratchet, empty-state, bronze-doctrine).
Output PASS or BLOCK + `file:line` blockers. Read-only; no edits/gates/deploy.
