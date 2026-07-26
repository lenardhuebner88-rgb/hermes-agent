# Landed state: making "is on main" a queryable task property

Date: 2026-07-26
Branch: `qwen/lifecycle-landed-20260726`
Scope: fork-owned module + consumer script + tests. No upstream-owned file is
edited; no done-path is touched; nothing is activated (watcher is a draft).

This document deliberately uses **symbol names, not line anchors**. Line
anchors into `hermes_cli/kanban_db.py` rot silently (see the "This file's
anchors rot silently" trap in `docs/kanban/LIFECYCLE.md`); only
`scripts/check_kanban_lifecycle_anchors.py`-checked anchors in
`LIFECYCLE.md` itself are mechanically verified.

## 1. Current state: every path to `done`, and which ones know about git

`VALID_STATUSES` (re-measured in this worktree via `ast.parse`, verbatim):

```
VALID_STATUSES = {"triage","todo","scheduled","ready","running","blocked","review","done","archived"}
```

`done` is the last content state; `archived` is storage, not integration.
The `tasks` schema (`CREATE TABLE IF NOT EXISTS tasks` in `kanban_db.py`)
carries `branch_name`, `workspace_path`, `result`, `completed_at`,
`current_run_id`, `model_override` and ~50 more columns — **no column says
whether or where the branch landed** (full schema read verbatim; control:
the known column `branch_name` is present in the printout, so the readout is
complete and trustworthy).

All writes that set `status='done'` (found via `rg -n "status = 'done'"`,
each mapped to its enclosing function with `ast.parse`):

| # | Path (function, file) | Git knowledge | Integration witness written |
|---|---|---|---|
| 1 | `complete_task` (`kanban_db`) — the worker self-completion funnel, also reached from the manual CLI (`hermes_cli/kanban.py`), the dashboard (`plugin_api.py`, two call sites), `kanban_swarm.py`, `kanban_close_sprint.py`, and the auto-retry result-comment path in `kanban_db` | none | none |
| 2 | `_finish_release_gate_green` (`kanban_worktrees`) → calls `complete_task` | yes (it is the post-release path) | the merge itself happened earlier in `integrate_chain`; the witness lands on the *integrating* task, see below |
| 3 | `_auto_complete_decompose_root` (`kanban_worktrees`) | yes | **yes** — `integration_merged` + `INTEGRATOR_VERIFIED`, but only when `action == "merged"` and `merge_commit` matches `[0-9a-fA-F]{40}` |
| 4 | `_direct_complete_decompose_root` (`kanban_worktrees`) | knows there is *nothing* to merge | no (`integration_action: "commitless"` in run metadata) |
| 5 | `_finalize_integration_retry` (`kanban_db`) — Heiler lane `blocked -> done` after a re-integration round | yes | a *different* kind: `INTEGRATION_RETRY_SUCCEEDED_EVENT` with `merge_commit` in the JSON payload — not `integration_merged` |
| 6 | `_recover_scout_coder_handoff` (`kanban_db`) — crash recovery for scout→coder handoffs | none | none (`scout_handoff_recovered` event) |
| 7 | `repair_deliverable_posted_not_completed` (`kanban_db`) — repair path | none | none |
| 8 | `design_board_kanban.py` completion | none (different board semantics) | none |

The integration witness writers (production):

- `_record_integration_events_and_receipts` (`kanban_worktrees`) — the
  normal chain-integration recorder invoked from `integrate_chain`. **Note
  the classification trap:** it computes the kind through a dict lookup
  (`{"merged": "integration_merged", "clean": "integration_clean", ...}`),
  so a literal-string grep for `"integration_merged"` misses this writer.
  A first grep sweep reported exactly one writer; the dict-lookup writer was
  found only after reading the integration path end-to-end.
- `_auto_complete_decompose_root` (`kanban_worktrees`) — mirrors both
  witnesses onto the decompose root (path 3 above).

Witness readers (production): `kanban_closeout.py` receipt builder
(`integration_commits`), `outcome_verification.py`
(`_INTEGRATION_EVENT_KINDS = ("integration_merged", "INTEGRATOR_VERIFIED")`),
`_recover_missing_branch_integration` (`kanban_worktrees`), the
integration-pending detector in `kanban_db`, and the dashboard live-events
ticker (`evidence_readmodels._LIVE_EVENT_KINDS`).

The revert-safe git logic already exists — but only on recovery paths:
`_revert_commits_for_merge`, `_first_parent_merges_reaching_branch`, and the
content-drift check (`git diff --quiet <merge> <target> -- <changed files>`)
inside `_recover_missing_branch_integration` and
`_integrate_empty_or_already_merged`.

## 2. Falsification result

The briefing's thesis, literally: *"there is no state, no field, no event
and no query that separates 'worker finished' from 'work is on main'."*

**Refuted as literally stated.** A queryable integration truth exists:
`SELECT task_id FROM task_events WHERE kind IN ('integration_merged',
'INTEGRATOR_VERIFIED')`, with five production readers listed above and
`merge_commit` inside the JSON payload.

**Confirmed in substance.** The witness is:

1. **Not a property of the task.** No column; `tasks.status = 'done'`
   carries zero integration information. The repo's own lifecycle doc says
   so: `LIFECYCLE.md` — *"Do not read 'task X is done' as 'X's code is on
   main'"* — and its Traps section documents the park-cannot-demote-done
   hole with four measured task ids: *"Their work was reverted by the gate
   and never landed, yet they read as successfully done. When auditing,
   trust `task_runs.outcome`, not `tasks.status`."*
2. **Written by only 2 of 8 done-path families** (paths 3 and the
   `integrate_chain` recorder). The biggest funnel, `complete_task`, writes
   no witness (control probe: an AST scan of `complete_task` finds eight
   appended event kinds — `"completed"`, `"review_released"`, etc. — the
   probe demonstrably detects kinds, and `integration_merged` is not among
   them).
3. **Never invalidated.** Append-only; nothing writes an unmerge/revert
   event (search for `integration_reverted|integration_invalidated|
   landed_invalidated` → 0 hits; control `_revert_commits_for_merge` →
   4 hits, so the search discriminates).
4. **Not queryable without JSON parsing** for the commit itself (every
   reader does `json.loads(payload)`).
5. **No single query answers "done AND still on main".** Dashboard search
   for a landed-state query → 0 hits (control: the `_LIVE_EVENT_KINDS`
   ticker list was found, so the search works).

Live-board census (read-only, `sqlite3` opened with `?mode=ro`, board path
printed and verified first; `HERMES_KANBAN_DB`/`HERMES_HOME` unset per
`env | grep`):

```
tasks total=5041 done=606 archived=4425
done WITH integration_merged witness: 125
done WITHOUT witness: 481
done with branch_name set: 340
integration_parked: events=111 run-outcomes=55
```

So 481 of 606 done tasks have no witness at all (most legitimately —
scratch/chat tasks never touch git), ≥215 done tasks carry a branch without
a witness, and the park-event/run-outcome gap documented as 106/50 in
`LIFECYCLE.md` has grown to **111/55** — the drift keeps accumulating.
The four documented revert cases (`t_81a35a60`, `t_461aee5e`, `t_77ffe9cc`,
`t_daed5e85`) **still read `done` today**; their branches are deleted, so
even a branch-based audit cannot reach them — only a stamped/materialized
state would survive branch removal.

**Verdict:** the operational core of the thesis holds. The board can say
"done" and "a merge happened once" but cannot say "the work is on main
*today*", and for most done-paths it cannot say anything at all.

## 3. Design

### Variant A — stamp columns on `tasks` at every done-path (rejected)

Add `tasks.landed_at` / `tasks.landed_commit` columns, written by the
done-paths themselves.

Rejected:

- Requires edits in ~8 places across upstream-owned `kanban_db.py` (paths
  1, 5, 6, 7 funnel through it) — every edit is a future upstream-sync
  conflict, violating the standing fork rule.
- A stamp is only as true as its writer. Six of eight paths are git-blind
  and would stamp nothing or a lie; the stamp must then be re-verified
  against git anyway — at which point the stamp is a cache, and a cache
  that six writers never fill is worse than derivation (it reads as
  authoritative while being absent).
- Risk of new chain freezes (construction rule 3): any gate that consults
  the stamp can strand tasks when a writer is skipped.

### Variant B — derive from durable witnesses, materialize in a fork-owned
table, expose one query (chosen)

Truth lives in git; the board holds durable witnesses (events +
`branch_name`). A fork-owned module computes the integration state from
both, **re-using the revert-safe criteria already proven in the fork**
(merge-reachability + content-drift + explicit-revert grep + patch
equivalence), materializes it into a fork-owned table, and exposes it via a
consumer script. No done-path is touched; nothing gates on the state; the
materialization is a read side, not a transition.

States (column `state`, plain TEXT, queryable without JSON):

| state | meaning |
|---|---|
| `landed_active` | merge witness or merge-reachability, and the merged content is still active on the target today |
| `landed_drifted` | a merge happened, but the content differs from the merge commit on the target. `detail` JSON carries `explicit_reverted` (a commit on the target greps the merge sha — a real revert) vs. later evolution (same files changed afterwards — the work landed and was built on; not an alarm) |
| `not_landed` | branch exists with commits ahead of the target and none of them patch-equivalent (`git cherry` all `+`) |
| `na_commitless` | done without a branch / commitless chain — nothing to land |
| `unknown` | done *with* a branch but no witness and the branch ref is gone — cannot be judged. Deliberately **not** folded into `not_landed`: 481/606 done tasks lack witnesses, and declaring all of them "not landed" would make the signal noise and train readers to ignore it (the score-materialization dead-module fate). `unknown` means "git-blind done-path, no evidence either way". |

Classifier ladder (first hit wins):

1. Witness `integration_merged` with 40-hex `merge_commit` →
   `merge-base --is-ancestor <mc> <target>`; if not ancestor →
   `landed_drifted` (evidence `merge_commit_not_ancestor`). If ancestor →
   content check `git diff --quiet <mc> <target> -- <changed files>` →
   `landed_active`, or on drift `landed_drifted` with `explicit_reverted`
   from `git log --grep=<mc> <target>`.
2. No witness but `branch_name` resolves → `rev-list --count target..branch`:
   - ahead > 0 → `git cherry target branch`: any `+` → `not_landed`
     (catches manual re-apply: all `-` → `landed_active`,
     evidence `patch_equivalent`);
   - ahead == 0 → first-parent merge reaching the branch + content check
     → `landed_active` / `landed_drifted`; no merge found →
     `git cherry` empty → `landed_active` (rebased/ff-in).
3. Branch ref gone, no witness → `unknown`.
4. No branch at all → `na_commitless`.

This answers the briefing's revert-survival requirement: the criterion is
**content activeness** (`git diff --quiet` on the changed file set), not
the ancestor relation — a revert keeps the branch an ancestor and still
flips the state, which is exactly what the naive `rev-list --count
main..kanban/*` watcher would miss.

Construction-rule compliance:

1. *Fork-owned:* new module `hermes_cli/kanban_landed.py` (same pattern as
   the fork-owned `outcome_verification.py` with its own `ensure_schema`
   and own tables `outcome_contracts`/`outcome_attempts`). **Zero edits to
   `kanban_db.py`** — the table is ensured lazily by the module's public
   functions; the consumer is a standalone script.
2. *Real consumer:* `scripts/kanban_landed_check.py` — per-task query,
   `--stale` disagreement sweep, `--materialize`, `--json`. It refuses
   board writes against the live board without `--write`. A dashboard
   endpoint sketch is in §5 (UI out of scope per the briefing).
3. *Cannot freeze chains:* pure observation surface. No transition reads
   it; if nobody runs the sweep, rows merely go stale.

### Migration & backward compatibility

`CREATE TABLE IF NOT EXISTS task_landed_state(task_id TEXT PRIMARY KEY,
state TEXT NOT NULL, merge_commit TEXT, target TEXT, branch TEXT,
detail TEXT, verified_at INTEGER NOT NULL)` — additive, idempotent by SQL,
runs twice with no error (tested). No `ALTER` on `tasks`, no triggers, no
data backfill required: an empty table is correct — absence of a row means
"not yet verified", which is exactly `unknown` semantics. A board that
never ran the module keeps working unchanged; every function ensures the
schema on entry.

## 4. Reality check (read-only, live repo + live board)

**Measurement discipline:** the live checkout is edited by parallel
sessions, so these numbers are snapshots. Two measurement points are
recorded; the second shows the world moved between them — and the query
tracked it.

T1 (~20:20), `main = a284156a9` — all `kanban/*` branches, measured from
the live checkout (read-only git commands only):

```
kanban/t_374ec048  ahead=0  merge=00c698347  CONTENT-ACTIVE   -> landed_active
kanban/t_6b5bac71  ahead=0  merge=00c698347  CONTENT-ACTIVE   -> landed_active
kanban/t_76aa2e17  ahead=0  merge=00c698347  CONTENT-ACTIVE   -> landed_active
kanban/t_76ac50f8  ahead=0  merge=00c698347  CONTENT-ACTIVE   -> landed_active
kanban/t_9beb1180  ahead=0  merge=00c698347  CONTENT-ACTIVE   -> landed_active
kanban/t_7b55c5c1  ahead=0  merge=00c698347  CONTENT-DRIFTED  -> landed_drifted
kanban/t_87e7ee2e  ahead=0  merge=8c2f6799b  CONTENT-DRIFTED  -> landed_drifted
kanban/t_89589323  ahead=0  (integration_parked event)        -> landed_drifted
kanban/t_b7a9208a  ahead=1  no merge, git cherry '+'          -> not_landed
```

T2 (20:46), `main = 6ac0ed481` — the same query ~25 minutes later:
`kanban/t_7b55c5c1` is gone (branch removed by a parallel session) and
`kanban/t_b7a9208a` now points at the same commit as `main` — its work was
landed while this document was being written, flipping the classifier from
`not_landed` to `landed_active` (`ancestor_no_merge_ff_or_rebase`). Two
branches remain `landed_drifted` (`t_87e7ee2e`, `t_89589323`), both with
`explicit_reverted: false` (evolution, not revert). The point of the
exercise, demonstrated live: the answer is a function of *now*, and the
board's `done` never moved in any of these cases.

The uncomfortable cases, deliberately reported:

1. **A naive ancestor watcher would have lied 3 times out of 8.**
   `kanban/t_7b55c5c1`, `t_87e7ee2e`, `t_89589323` are all ancestors of
   `main` (`rev-list --count main..<b>` == 0) yet their merged content no
   longer matches `main`. Only the content check exposes that. This is the
   exact failure mode the briefing predicted ("branch is an ancestor is not
   proof the work exists on main").
2. **The drift is evolution, not revert — and the state must say so.**
   `git log --grep=<merge-sha> main` finds **no** revert commits for either
   drifted merge; the drift comes from later commits editing the same files
   (e.g. the LA-S6 revision reworking the langfuse dashboard JSONs first
   merged by `t_7b55c5c1`). Reporting these as `reverted` would be a false
   alarm; `landed_drifted` with `explicit_reverted: false` is the truthful
   answer. (This re-classified my own initial reading of the data.)
3. **`t_b7a9208a` is done, its branch carries commit `b94610922`, and that
   commit is neither merged nor patch-equivalent to anything on `main`
   (`git cherry main kanban/t_b7a9208a` → `+ b94610922…`).** The work
   reached `main` through a *different* commit ("pfadgenau nachziehen"), so
   per-evidence this task is `not_landed` even though equivalent content
   may exist under another patch. At measurement time a review task
   (`t_e3edd33b`, running) is literally auditing this case. Reported, not
   touched.
4. **`t_89589323`: done with an `integration_parked` witness**
   (`mc=ed52521fdeac`) — the park-cannot-demote-done trap from
   `LIFECYCLE.md`, still live on the board.
5. **The four 2026-07-25 documented revert cases still read `done`**, and
   their branches are deleted — branch-based auditing is impossible for
   them; only materialized state would have survived.

## 5. Consumer sketch: dashboard (description only, not built)

Endpoint (future): `GET /api/kanban/landed?stale=1` →
`[{task_id, title, state, merge_commit, target, detail, verified_at}, …]`
backed by `SELECT … FROM task_landed_state WHERE state IN
('landed_drifted','not_landed')` joined to `tasks` — one indexed read, no
JSON parsing on the hot path. A tile "done but not landed" listing
`landed_drifted` (with revert evidence highlighted) and `not_landed`. UI is
a separate order per the briefing's anti-scope.

Watcher job draft (**not activated** — activation is an operator
decision): a nightly cron entry running
`scripts/kanban_landed_check.py --sweep --materialize --write --json`,
posting only *new* disagreements. Sketch:

```json
{
  "name": "kanban-landed-watch",
  "schedule": "17 3 * * *",
  "command": "scripts/kanban_landed_check.py --sweep --materialize --write --json"
}
```

## 6. What I could not measure

- **Whether `landed_drifted`/evolution cases always correspond to intended
  later work.** Distinguishing "built upon" from "silently broken" needs
  commit-message/intent analysis; the module reports the file set and the
  revert-evidence flag and leaves judgment to the reader.
- **Patch-equivalence limits:** `git cherry` compares patch ids; a manual
  re-apply that changes whitespace/context (as with `t_b7a9208a`) reads as
  `not_landed` even when the *intent* landed. No fully general fix exists;
  the state names the evidence, not the intent.
- **Archived tasks** (4425): not swept. The hole that costs work is
  `done`; archived auditing is a future lever.
- **Full drift-file census across all 481 witness-less done tasks**: most
  predate current branch hygiene and their branches are gone; the census
  would mostly yield `unknown`. Sampled the live `kanban/*` set instead.

## 7. Refuted — briefing assumptions that did not survive measurement

1. **"There is no event / no query" (Section 1 thesis)** — refuted as
   stated; the witness machinery exists and has five readers. The real gap
   is narrower and sharper than briefed: no *task property*, no
   *invalidation*, no coverage of git-blind done-paths, no single "done AND
   on main today" query.
2. **Candidate (b)'s implied assumption that `rev-list --count
   main..kanban/*` is a usable watcher criterion** — refuted empirically:
   3 of 8 live branches are ancestors with drifted content. The
   content-activeness check (already proven in
   `_recover_missing_branch_integration`) is the revert-surviving
   criterion, and the design adopts it.
3. **"Drifted ⇒ reverted" (my own working assumption while measuring)** —
   refuted: zero explicit revert commits behind the three drifted merges;
   the drift is later evolution. The state split
   (`explicit_reverted: true/false`) exists because the data demanded it.
4. **Assumption that the biggest risk is missing merge records** —
   partially refuted: the park-cannot-demote-done trap (111 park events vs
   55 run outcomes) shows the board can record a failure *and still read
   done*; invalidation matters as much as recording.
5. **The briefing's venv note ("`venv` (no dot) has pytest")** — stale at
   execution time: a runtime rotation (`venv.stale.runtime-1785002417…`)
   left `venv/bin` without pytest; `.venv/bin/python -m pytest` (9.0.2) is
   what works now, inverting the briefing's claim. Measured, not assumed —
   `venv/bin/python -m pytest --version` → `No module named pytest`.
