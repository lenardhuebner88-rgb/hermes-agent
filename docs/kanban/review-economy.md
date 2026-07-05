# Review economy — verification flags (`kanban.review_gate`)

Shipped 2026-07-05 (Plan→Board→Release pipeline, Subsystem B). All flags live
in the ROOT `~/.hermes/config.yaml` under `kanban.review_gate` and are
hot-read per decision (`_review_gate_config()` — no restart needed for config
changes; *code* changes still need a gateway restart).

| Key | Code default | Live (2026-07-05) | Effect |
|---|---|---|---|
| `standard_uses_llm_verifier` | `true` | `false` | **B1.** When `false`, a completed `standard`-tier code task whose enforced deterministic worker gate (`kanban.worker_gate`) ran GREEN goes straight to `done` — no LLM verifier spawn. Audit event: `review_skipped_deterministic` (carries the worker-gate stamp). Safety floor: no green gate evidence (gate disabled / no workspace / no repo commands) → the verifier still fires; a RED gate still raises `WorkerGateError` (task stays in-flight). |
| `judge_at_chain_tip` | `false` | `true` | **B2.** When `true`, `review`-tier slices of a PlanSpec chain (`planspec_source` set) defer their LLM review to the chain **tip**: the last open code-bearing slice parks in `review` (the integrated diff gets the ONE judgment), earlier slices go `done` on a GREEN worker gate. Audit event: `review_deferred_to_tip`. Tip detection counts code roles only — a trailing scribe/docs sibling never swallows the judgment. |
| `critical_reviews_each_slice` | `true` | `true` | **B2 guard.** `critical` slices keep per-slice review even with tip judgment on. Flip only with operator sign-off. |

**B3 — risk-triggered ingest governance** (no flag; behavior of
`ingest_planspec`): unsigned plans whose slices all classify ≤ `review`
(via `classify_review_tier`) ingest with the deterministic rubric ADVISORY
(logged warnings) and the subjective LLM judge SKIPPED. The blocking rubric +
judge fire only for plans containing a `critical`-classified slice; the
classifier fails CLOSED (error → treated as critical). The signature
(`approved_by` + `freigabe: complete`) survives as the operator override for
critical plans and as the below-floor downgrade ack
(`review_tier_downgrade_ack`, unchanged).

Tests: `tests/hermes_cli/test_review_economy.py`,
`tests/hermes_cli/test_planspec_rubric.py` (B3 section).
