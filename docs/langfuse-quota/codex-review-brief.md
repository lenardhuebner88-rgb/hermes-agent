# Codex read-only review brief — Langfuse/Quota 8/10 plan (1:1 code grounding)

## Mode
- **READ-ONLY.** Do NOT edit any file. Do NOT run gates/builds/tests. Do NOT `git add/commit/push`,
  no deploy, no service restart. Do NOT touch the unrelated uncommitted Bibliothek WIP in this worktree
  (`hermes_cli/library_*.py`, `web/.../BibliothekView*`, `*Shelf*`, `docs/adr`, `docs/contexts`,
  `CONTEXT-MAP.md`) — review only, leave it exactly as-is.
- Working directory = this isolated worktree:
  `/home/piet/.hermes/worktrees/codex-bibliothek-p6b-unknowns-qwen`
- The line numbers below are HINTS from a prior read-only audit; **re-locate each symbol in the current
  source** and judge against what is actually there. Trust the file, not the hint.

## Target artifact
`docs/langfuse-quota/PLAN.md`.
Focus on §3 (element → real field map), §4 (backend MUST-ADD M1..M5 add-sites), §5 (endpoints),
§1 invariants, and §13 (this review's checklist; in v2 §12 is the mockup section). This is the **v2 re-check**: also confirm the v2 revision-note items (Oa/Ob/Oc applied + UNBACKED-LIVE empty; INV-4 derived denominator; M2b read contract; claim-time attribution = LIVE; M1 at run_turn 414/490; M3 nightly scripts; M4 merge at 8407; M5 producers 1810; exact DESIGN.md 135-138 marker). Read `web/src/control/DESIGN.md` for the design
invariants (ratchet: no raw hex in `.tsx`; bronze = interactive/live only; data-palette identity;
empty-state doctrine; Jarvis `MOCK-DATEN` preview idiom; puls-leiste contract).

## Files to open (re-locate symbols; ranges are hints)
- `agent/codex_runtime.py` (~140-200) — does it fire ANY `invoke_hook(...)` / reference langfuse/plugins?
- `plugins/observability/langfuse/__init__.py` (~140-230 init/env; ~540-640 metadata+usage_details+cost_details; ~880-1080 span/trace names + register) — what metadata keys exist today.
- `agent/usage_pricing.py` (~25-100 CanonicalUsage; ~630-700 resolve_billing_route; ~820-990 normalize + estimate_usage_cost + CostResult status enum).
- `agent/account_usage.py` (~25-65 snapshot/window dataclasses; ~540-660 codex; ~900-1050 anthropic; ~1460-1490 dispatcher).
- `hermes_cli/kanban_db.py` (~2030-2135 tasks+task_runs schema; ~3410-3435 token migrations; ~3620-3635 run columns; ~6350-6370 _run_metadata_subscription; ~7000-7080 repair/equiv; ~7250-7380 price lookup + _equiv_from_tokens; ~30200-30260 _profile_subscription; ~30330-30480 subscription_token_burn; ~30600-30620 cached_tokens; ~34330-34460 chain_cost_breakdown).
- `hermes_cli/autoresearch_runs.py` (~75-115 append_run signature + record schema).
- `hermes_cli/autoresearch_proposals.py` (~1860-1965 budget_stop set + result["budget_stop"] + append_run call).
- `hermes_cli/autoresearch_lane_contracts.py` (~285-300 classify_lane_outcome / quota_skipped).
- `hermes_cli/strategist.py` (~1020-1055 roots_by_class vs by_class fallback).
- `hermes_cli/vision_metrics.py` (~35-50 SCHEMA_VERSION + payload shape).
- `hermes_cli/web_server.py` (~4580-4610 /api/account-usage + /api/start/host-usage).
- `hermes_cli/operations_routes.py` (~695-745 runs/costs, costs-series, subscription-burn).
- `hermes_cli/planspec_flow_routes.py` (~1620-1640 chain-costs).
- `web/src/control/hooks/costsUsage.ts` (~1-90 hooks + polling).
- `web/src/control/lib/schemas/costsUsage.ts` (~1-95 zod schemas, subscription enum).
- `web/src/control/components/leitstand/` (README + component list — confirm KpiTile/SectionHeader/ListRow/StatusChip exist to extend).

## Checks (return blockers only; CONFIRM the rest tersely)
1. **LIVE/PARTIAL rows in PLAN §3 exist:** for each, the cited field/symbol is present at (re-located)
   file:line with the cited type/shape. Flag any row whose field is actually absent or differently shaped
   (these would render a lie → BLOCK).
2. **GAP→Mx rows truly unbacked today:** confirm `append_run` lacks an outcome param; confirm
   `task_runs` lacks cache_read/write/reasoning columns + lacks stamped subscription/billing_mode at write
   time; confirm langfuse metadata lacks task_run_id/billing_mode/subscription; confirm codex_runtime fires
   no hooks. If any "GAP" is in fact already implemented, say so (PLAN over-claims a gap → correct it).
3. **MUST-ADD add-sites (§4) correct + additive:** each M1..M5 insertion point is the right place; the DB
   change in M4 is genuinely additive/expand-contract (no destructive ALTER / no DROP); M3's record-schema
   change is additive. Flag any site that conflicts with an existing migration or writer.
4. **Endpoint reuse + no writes (§5):** the cited existing routes exist; the proposed new endpoints are
   read-only (no POST/PUT/DELETE in this surface); the zod + hook reuse pattern matches `costsUsage.ts`.
5. **Invariants vs DESIGN.md (INV-1..INV-6):** especially INV-1 (no write CTA in these read views), the
   ratchet (no raw hex in `.tsx`), bronze-doctrine, empty-state doctrine for the un-instrumented-lane
   Trace state, and the Jarvis preview-marker idiom for PHASE2/MOCK. Flag any plan element that would
   violate these.
6. **The single most important trust check:** list every PLAN §3 row tagged `LIVE` that is NOT actually
   backed by a real field/endpoint today. This list MUST be empty; if not, those rows must be downgraded
   to PARTIAL/GAP/PHASE2 in the plan (BLOCK until fixed).

## Output contract (bounded; no raw file dumps; short quotes ok)
```
VERDICT: PASS | BLOCK
LIVE/PARTIAL map: <n> confirmed, <list of WRONG with corrected file:line>
GAP rows: <confirmed-absent | list of actually-implemented>
MUST-ADD sites: <confirmed | WRONG with correction>
Endpoint/invariants: <ok | blockers>
UNBACKED-LIVE (must be empty): <list>
BLOCKERS: <file:line — why; or "none">
```
End with a 3-line headline: is the plan genuinely 1:1 grounded, and the 1-2 corrections that matter most.
