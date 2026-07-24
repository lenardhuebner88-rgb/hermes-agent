# Branch triage before giant-module modularization

- **Date:** 2026-07-24
- **Base:** `main` @ `1ef243502`
- **Why:** eleven branches carry `hermes_cli/kanban_db.py` deltas. After the split that file no longer exists at that path, so their diffs can no longer auto-merge. Each must be landed or dropped first.
- **Status:** awaiting operator decision (Step 2)

## Finding

**All three branches the design spec flagged for a land-or-drop review are superseded by `main`.** None carries unlanded work worth keeping. The recommendation is therefore to archive-and-delete all eleven rather than the eight the spec assumed.

The evidence is symbol-level, not patch-id-level — `git cherry` alone was misleading on `kanban/t_610a9f84` (it reported 8 of 11 commits as unlanded, which turned out to be a rebase artifact).

## The three "decide" branches

### `kanban/t_c254b029` — 5 commits, 2026-07-22, `kanban_db.py` 626+/284−

TMAX terminal work: review-revision fork argv, `terminal_run_id`, free/isolated write, native context actions, phase-aware worker briefs, W2-S5 archive-fence reconcile, plus a worktree-pruning script.

**Superseded — complete.** `git cherry -v main kanban/t_c254b029` marks all 5 commits `-` (equivalent already in `main`). Symbol-level check: **0 top-level symbols exist on the branch but not on `main`**. Corroborating spot checks on `main`: `scripts/prune-stale-worktrees.sh` present, `terminal_run_id` appears 82× in `hermes_cli/agent_terminals.py`, phase-aware briefs documented in `docs/kanban/LIFECYCLE.md`.

**Recommendation: drop (archive + delete).** Nothing is lost.

### `codex/board-model-truth-20260713` — 1 commit, 2026-07-14, `kanban_db.py` 713+/47−

Board and runtime model truth: model-route badges, board identity, fleet-hub plumbing.

**Superseded — complete.** `git cherry` marks the single commit `-`. `web/src/control/components/fleet/ModelRouteBadge.tsx` exists on `main`.

Symbol-level check reports 28 symbols present on the branch but absent from `main`. All 28 are OpenClaw / Mission-Control / Family-Organizer-backlog machinery (`_dispatch_to_openclaw`, `poll_openclaw_results`, `_build_atlas_envelope`, `_build_forge_envelope`, `_build_lens_envelope`, `_build_pixel_envelope`, `_maybe_close_family_organizer_backlog_item`, the `_FO_BACKLOG_*` / `_MC_*` constants, and so on). These are **not unlanded work** — `main` removed them deliberately in `fd95ada4b codex: retire OpenClaw kanban writers`. OpenClaw was decommissioned on 2026-06-01 and must not be reactivated.

**Recommendation: drop (archive + delete).** Landing it would resurrect retired OpenClaw writers.

### `kanban/t_610a9f84` — 11 commits, 2026-07-14, `kanban_db.py` 187+/34−

Five `loop(builder-reviewer)` reliability fixes from 2026-07-14 (deterministic spawn failure, budget-exhaustion classification, respawn-guard auth wedge, repair-deliverable result fidelity, heiler structural block kind), plus autoresearch flood-guard and `needs_operator` gating, plus gateway shutdown forensics.

**Superseded — verified against content, not patch-id.** `git cherry` initially suggested 8 of 11 commits were unlanded. That is wrong. Every deliverable is already on `main`:

| artifact | on `main` | on branch |
|---|---:|---:|
| `gateway/shutdown_forensics.py` | 739 lines | 739 lines (identical) |
| `tests/hermes_cli/test_kanban_respawn_guard_blocker_auth_ttl.py` | 230 | 230 (identical) |
| `tests/hermes_cli/test_kanban_repair_deliverable_result_fidelity.py` | 135 | 135 (identical) |
| `tests/hermes_cli/test_kanban_heiler_capacity_budget.py` | 47 | 47 (identical) |
| `tests/hermes_cli/test_kanban_deterministic_spawn_failure.py` | **222** | 113 (`main` is ahead) |
| `tests/hermes_cli/test_kanban_heiler_block_kind_structural.py` | 232 | 249 |
| `tests/test_autoresearch_reconcile.py` | **963** | 858 (`main` is ahead) |
| `tests/gateway/test_gateway_exit_diagnostic_production_path.py` | present | present |
| `tests/run_agent/test_fallback_config_to_activation.py` | present | present |

Every function the branch's `kanban_db.py` diff adds already exists on `main`: `_validated_comment_id_watermark`, `_silent_block_escalation_matches_block_episode`, `_deterministic_spawn_failure_marker`, `_blocked_comment_id_watermark`. The symbol-level diff shows the **same 28 retired OpenClaw symbols** as the branch above, and nothing else.

The branch also **already conflicts with `main` today**, independently of the split — `git merge-tree --write-tree main kanban/t_610a9f84` reports content conflicts in `hermes_cli/kanban_db.py`, `hermes_cli/autoresearch_reconcile.py` and `tests/hermes_cli/test_kanban_db.py`, plus add/add conflicts on two test files that `main` now owns in a further-developed form. Landing it would mean resolving conflicts in favour of `main` almost everywhere — i.e. producing nothing.

**Recommendation: drop (archive + delete).**

## The eight "archive + delete" branches

Unchanged from the spec, all verified present at `1ef243502`:

| branch | commits ahead | last commit | `kanban_db.py` delta |
|---|---:|---|---|
| `backup/grok-kanban-block-kind-20260715-pre-rebase` | 4 | 2026-07-15 | 627+/113− |
| `kanban/t_80809063` | 1 | 2026-07-12 | 40+/11− |
| `kanban/t_d2d25240` | 1 | 2026-07-18 | 30+/0− |
| `kanban/t_49c1e99b` | 1 | 2026-07-17 | 19+/1− |
| `worktree-bridge-cse_01HZiECqoEjuEdJuA5DWYFys` | 1 | 2026-07-17 | 19+/2− |
| `kanban/t_57aaa085` | 10 | **2026-07-24** | 10+/1− |
| `salvage/dirty-main-20260712T014834` | 2 | 2026-07-12 | 9+/7− |
| `kanban/t_69536fff` | 1 | 2026-07-12 | 7+/16− |

Two notes for the operator:

- **`kanban/t_57aaa085` is dated today, not stale.** The spec buckets it as "trivial or ≥1 week stale". Its `kanban_db.py` delta is genuinely trivial (10+/1−), so the bucketing is defensible, but it has 10 commits and was touched today, so it may hold live non-`kanban_db` work. **Do not delete without an explicit call.**
- **`backup/grok-kanban-block-kind-20260715-pre-rebase` carries the second-largest delta in the whole set** (627+/113−) yet sits in the archive bucket. That is consistent with its name — a pre-rebase backup, i.e. superseded by the rebased branch — but worth one confirming glance.

## Decision

Operator decision taken 2026-07-24 in a grill session, after two of the flagged branches were
re-verified independently. **The "drop all eleven" recommendation was correct for ten of them and
wrong for one.**

| branch | operator decision | date |
|---|---|---|
| `kanban/t_c254b029` | drop (archive + delete) | 2026-07-24 |
| `codex/board-model-truth-20260713` | drop (archive + delete) | 2026-07-24 |
| `kanban/t_610a9f84` | drop (archive + delete) | 2026-07-24 |
| `kanban/t_57aaa085` | **land first**, then archive + delete | 2026-07-24 |
| the remaining 7 | drop (archive + delete) | 2026-07-24 |

### Correction to the finding

- **`backup/grok-kanban-block-kind-20260715-pre-rebase` — superseded, confirmed.** `git cherry`
  marks `38f94f9a6` ("stamp block_kind on system parks") as unlanded, but that is another rebase
  artifact: `hermes_cli/kanban_diagnostics.py` carries the same 5 `block_kind`/`system park`
  markers on both sides, and `tests/hermes_cli/test_kanban_block_kinds.py` is 696 lines on `main`
  against the branch's +104. Nothing is lost.
- **`kanban/t_57aaa085` — NOT superseded.** `tests/hermes_cli/test_scores_digest.py` is **592
  lines on the branch against 288 on `main`**, plus +188 lines in `hermes_cli/kanban.py` and the
  `HERMES_HOME` venv fix in `scripts/cron/scores-weekly-digest.sh`. The triage's own caveat was
  right; its bucketing into archive-and-delete was not. It was landed rather than dropped.

## Execution record

**Step 2–5 executed 2026-07-24.**

`kanban/t_57aaa085` was landed first. `main` was 33 commits ahead of the branch's rebase base and
had evolved the same two files, so this was a real conflict resolution, not a fast-forward:

- `scripts/cron/scores-weekly-digest.sh` → resolved in favour of `main`. Its venv precedence
  (canonical `venv` before stale sibling `.venv`) is the deliberate later decision and carries a
  written rationale; both venvs were verified to exist and resolve `hermes_cli` to the live repo,
  so preferring `venv` costs nothing.
- `tests/hermes_cli/test_scores_digest.py` → union. `main`'s stricter
  `test_copied_script_resolves_hermes_home_venv` (asserts `venv` beats a stale `.venv` sibling)
  plus every test the branch added: high-cardinality budgeting, sidecar write failure, large
  `--weeks` budget, verdict-filtered approval aggregates.

Gates before landing, all exit 0: `pytest tests/hermes_cli/test_scores_digest.py` → 13 passed ·
`ruff check` → clean · `pytest --co -q tests/` → 48,666 collected · `scripts/run-affected.sh` →
50 files, 1,572 tests, 0 failed. No `web/` change, so frontend gates did not apply.

`main` fast-forwarded to `8f29783e0`. The worktree `.worktrees/kanban/t_57aaa085` was removed
(its work is in `main`).

All eleven branches were then tagged and deleted — **11 tagged, 11 deleted, 0 failures**; each
delete was gated on its tag resolving to the same SHA as the branch tip. Branch count 105 → 94.

| archived tag | tip |
|---|---|
| `archive/pre-modularization/kanban/t_c254b029` | `e978d4bfd` |
| `archive/pre-modularization/codex/board-model-truth-20260713` | `7b9713ed5` |
| `archive/pre-modularization/kanban/t_610a9f84` | `0c3ff00e6` |
| `archive/pre-modularization/backup/grok-kanban-block-kind-20260715-pre-rebase` | `46760f82e` |
| `archive/pre-modularization/kanban/t_80809063` | `8a46eb264` |
| `archive/pre-modularization/kanban/t_d2d25240` | `2655a7969` |
| `archive/pre-modularization/kanban/t_49c1e99b` | `506f094c9` |
| `archive/pre-modularization/worktree-bridge-cse_01HZiECqoEjuEdJuA5DWYFys` | `65fbcc2fb` |
| `archive/pre-modularization/kanban/t_57aaa085` | `9a1b79798` |
| `archive/pre-modularization/salvage/dirty-main-20260712T014834` | `24d785ea1` |
| `archive/pre-modularization/kanban/t_69536fff` | `d1393da21` |

Restore any of them with `git branch <name> archive/pre-modularization/<name>`.

**Task 0 is closed. The split is no longer gated on branch triage.**
