---
title: Hermes Orchestrator Stress-Test — Findings & Next-Level Roadmap
date: 2026-06-16
source: autonomous stress-test campaign 2026-06-15 (Phase 0 + Wave 0-3, 10 dashboard slices + spawn-resilience)
commits: af448ee9d..bf655b972 (pushed piet-fork/main)
status: findings (open)
---

# Hermes Orchestrator — Stress-Test Findings

Run fully autonomous via the kanban CLI on the live board (`/home/piet/.hermes/kanban.db`):
2 audits, 4 standalone fix slices, 1 five-child decomposed package, 1 dispatcher-hardening
slice — all driven through *triage → auto_decompose → coder lane → verifier review_gate →
auto-integrator → main → deploy*.

## What works (proven, keep)
- The full autonomous loop runs end-to-end with **zero real spawn failures in Wave 1-3**.
- **Decomposer is excellent:** clean DAGs, scope-contracts on every code child, verbatim
  constraint preservation (`MANDATORY:`/absolute paths), correct lane routing, correct
  dependency ordering, sensible fanout-vs-specify calls.
- **Report routing correct:** completions → reporting channel `1495737862522405088`, never #hermes-oc.
- **Provisioning self-heals:** scratch code tasks get a carved worktree via `scratch_code_redirect`.
- **Review-gate works:** coder → verifier → APPROVED, consistently.
- Ship-Loop-v1 coder-contract injects **only** on code-roles (research tasks correctly skipped).

---

## Findings (priority-ordered)

### F1 — Integrator frontend gate is partial (HIGH)
**Status 2026-06-16 follow-up.** Closed for automatic quick-gate coverage: web diffs
now run `lint:control`, `tsc -b --noEmit`, and the control Vitest suite before merge.
`npm run build` remains the separate post-merge release/deploy gate because it mutates
generated dashboard assets.

**Original symptom.** The post-merge integration gate for the dashboard (non-FO repo) ran
`ruff + affected-pytest + tsc --noEmit (web/ only)`. It did **NOT** run `eslint`
(`lint:control`), **NOT** `vitest`, **NOT** `npm run build`. A lint violation, a failing or
uncovered vitest test, or a build-only break could therefore auto-merge to `main`.
**Evidence.** `hermes_cli/kanban_worktrees.py:699-752` (`default_quick_gate`); the live merge
gate strings for `t_5182a2ae`/`t_ed9d3591`/`t_40ca037b` were all
`"ruff ok; pytest skipped (no affected test modules); tsc ok"`. My manual per-wave gate
(`lint:control + tsc + vitest 701/701 + build`) was the first to actually run eslint/vitest/build.
**Root cause.** `default_quick_gate` is a deliberately "quick" gate; tsc was added for web/ but
eslint/vitest/build were omitted for cost. It runs in `repo_root` (the live checkout) where
`web/node_modules` IS present — so the missing gates *can* run there.
**Fix.** Extend `default_quick_gate`: when the diff touches `web/`, also run
`npm run lint:control` and `npx vitest run src/control` (and optionally `npm run build`) from
`repo_root/web`, fail-closed like the existing tsc branch. Mirror `scripts/deploy_dashboard.sh`.
Mitigation today: tsc already catches the worst class (type errors).

### F2 — Carved worktrees lack runtime deps (MEDIUM)
**Symptom.** A carved worktree has no `web/node_modules` and no `.venv`, so a **worker** cannot
self-run the frontend gate or pytest in isolation. (Integration gating is fine — it runs in the
live repo. This is about *worker self-verification*.)
**Evidence.** Every slice needed an explicit "do NOT run npm/tsc — no node_modules" instruction;
the Wave-3 worker worked around the missing `.venv` by invoking the live
`/home/piet/.hermes/hermes-agent/.venv/bin/python` with the worktree as cwd.
**Fix.** For code workers, either symlink/`cp -al` the root `node_modules`+`.venv` into the
worktree at provision time, or codify the "live-binary + worktree-cwd" pattern in the
coder-contract so workers can self-gate. Otherwise workers fly blind until integration.

### F3 — Decompose-tree children don't integrate individually (MEDIUM)
**Symptom.** Wave-2 decompose-children all completed + APPROVED but **none emitted
`integration_merged`** (unlike Wave-1 standalone slices, which each auto-merged). Integration is
deferred to **root finalization**; the work sat on per-child branches (with one same-file child,
C5, stacked onto C3's branch `kanban/t_2789a548`). I preempted the root finalizer (blocked the
root, octopus-merged the 4 branches manually) to avoid a race.
**Evidence.** Wave-1 `t_5182a2ae`… had `integration_merged`; Wave-2 children none; root
`t_39775b73` sat `ready`/`todo`. `integrate_chain` at `kanban_worktrees.py:806`.
**Fix.** Document + harden the chain integration so the root finalizer reliably merges all
children (and surface the "children done, tree unmerged" state on the dashboard). Whether the
finalizer *would* have succeeded is untested — but see F4 (it is fragile to leftover files).

### F4 — Integrator parks on uncommitted scratch files, can't reconcile external merge (MED-HIGH)
**Symptom.** The Wave-3 standalone task **parked/blocked** because its worktree held an
uncommitted `.deliverable.md` scratch file ("chain worktree has uncommitted changes"). After I
merged its branch manually + deleted it, `complete` kept re-parking
("uncommitted changes but no commits to merge") → the card stuck `blocked` and had to be archived
despite the work being merged + live.
**Evidence.** `integration_parked` events on `t_0ccbb5a3`; `kanban_worktrees.py:888`.
**Fix.** Before the dirty-worktree check, ignore/clean known worker-scratch artifacts (e.g.
`.deliverable.md`, or a designated scratch path); add an operator "force-complete
(already-integrated)" path so a manually-merged branch can close cleanly.

### F5 — Iteration-budget under-provisioning + worker protocol gap (MEDIUM)
**Symptom.** Wave-0 StaleBadge audit (research, `--max-iterations 60`) produced the full
deliverable (37-row table, posted as a comment) but **blocked at 60/60** before calling
`kanban_complete`. A consolidate child once "exited rc=0 without calling kanban_complete".
**Evidence.** `t_f5465e4f` gave_up "Iteration budget exhausted (60/60)"; `t_5fd01b56`
protocol_violation.
**Fix.** Raise the default iteration budget for audit/research-class tasks; make the worker
call `complete` earlier / guarantee it on budget-exhaustion; surface "deliverable posted but not
completed" as a distinct, recoverable state (don't treat it as a hard failure).

### F6 — Same-repo code tasks fully serialize (LOW, by-design)
**Symptom.** `per_profile=1` + `repo_serialized` ⇒ all hermes-repo code tasks run one-at-a-time
(coder and coder-claude did NOT run concurrently); Wave-2's 5 children took ~30 min serial.
**Evidence.** `repo_serialized` events; `max_in_progress_per_profile=1` in gateway.log.
**Fix.** Intentional (avoids git/worktree contention). For scale, consider limited concurrency
across disjoint-file tasks now that F-spawn-resilience reduces the contention risk. Note for
throughput planning, not a bug.

### F7 — Code tasks need explicit workspace pinning (LOW, documentation)
**Symptom.** A direct CLI `create` of a code task with bare `--workspace worktree` was **refused**
(E1 guard): code tasks would fall back to the live Hermes repo, which is guard-protected. Needed
explicit `dir:/home/piet/.hermes/hermes-agent` (mirrors the FO pin). Decompose-children self-heal
via `scratch_code_redirect`.
**Evidence.** `kanban_db.py:2789-2811` (E1 guard); FO pin at `kanban_db.py:2760-2770`.
**Fix.** Working as intended — document that hermes-agent code tasks need explicit
`dir:<repo>` workspace; the decompose path already handles it.

---

## Recommended next slice
**F1** is the highest-value fix and the cleanest single change: teach `default_quick_gate` to run
`lint:control` + `vitest` (+ optional `build`) on web/ diffs. **F4** is a natural companion (same
file, integrator robustness). Both are dispatcher/integrator changes (Bestands-Eingriff) and
should be done TDD with reviewer + targeted gate + a gateway restart, exactly like the Wave-3
spawn-resilience landing.
