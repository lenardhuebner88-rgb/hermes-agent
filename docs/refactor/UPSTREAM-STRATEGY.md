# Staying merge-capable with upstream — start here

**Read this first if you are picking up work on `hermes_cli/kanban_db.py`,
upstream syncs, or the "giant module" refactor.** It is the orientation
document; everything else in `docs/refactor/` is detail hanging off it.

Last verified: 2026-07-25 against `main`.

---

## 1. The goal, in one paragraph

Hermes is a fork. Upstream (`origin` = NousResearch) keeps shipping real
improvements, and we want them. The obstacle is `hermes_cli/kanban_db.py`:
upstream wrote ~9.8k lines of it, the fork added ~29k more **into the same
file**, so every upstream update to it collides. The goal is **to be able to
pull upstream in cheaply, forever** — not modularization for its own sake. Any
proposal that does not reduce future merge cost or bring upstream work in is
off-target, however tidy it looks.

## 2. Where things stand

Two independent axes. Both are real work; they do not block each other.

### Axis A — catch up on the backlog the last sync dropped

The 2026-07-24 sync resolved `kanban_db.py` as *ours* and silently discarded
**12 upstream commits / 673 lines** — while still taking upstream's tests. The
fork shipped test files asserting behaviour it did not have: **42 red tests**.

**Axis A is done.** 10 of the 12 commits adopted; the other 2 turned out to be
already satisfied by fork equivalents. Brought in: DB repair + REINDEX auto-heal
+ WAL checkpoint + `hermes kanban repair`; per-task model/provider override;
decompose-sibling worktree isolation; cross-profile child routing; worker-child
workspace isolation; delegated-child mutation boundary; UTF-8 on every
`subprocess(text=True)` call.

Result: **42 red → 9 red**, and every one of the 9 is a documented divergence
that must *not* be "fixed" — see
[upstream-backlog.kanban_db.md](upstream-backlog.kanban_db.md) §3.

Two things fell out of the adoption that were not on anyone's list: a silent
defect the sync introduced (`tools/kanban_tools.py` accepted a `provider`
argument, validated it, and discarded it), and 11 unencoded `subprocess` calls
the fork had added itself beyond upstream's 8.

### Axis B — stop the next sync from needing `ours`

Why the last sync had to take `ours`: fork code is interleaved *inside*
upstream-owned function bodies, so git cannot separate the two. The metric is
**fork lines inside upstream symbols**, measured with the AST (§4).

The plan is to lift that fork logic into fork-owned hook functions, leaving a
one-line call in the upstream body. Not started. Detail and the per-symbol
target list: [2026-07-24-giant-module-modularization-plan.md](2026-07-24-giant-module-modularization-plan.md)
Task 8.

**Baseline, measured after the Axis A adoption** (AST, per §4). The seven hook
targets, fork lines inside each:

| symbol | before adoption | now |
|---|---:|---:|
| `create_task` | 276 | 299 |
| `_default_spawn` | 179 | 185 |
| `Task` (incl. `from_row`) | 145 | 147 |
| `_guard_existing_db_is_healthy` | 45 | 49 |
| `_cleanup_worker_tmux` | 5 | 7 |
| `list_comments` | 3 | 3 |
| `_backup_corrupt_db` | 0 | — now **identical** to upstream |
| **sum** | **653** | **690** |

**Read that correctly: Axis A moved this metric slightly the wrong way, and that
is expected, not a regression.** Weaving upstream logic into a fork-diverged
body produces text the differ attributes to the fork side. What Axis A bought is
visible in the other direction — upstream lines not applied fell from 1,843 to
1,759 top-level, `_backup_corrupt_db` went from diverged to byte-identical, and
42 red tests became 9. The alternative (skip adoption to protect the number)
would have left the fork both feature-poor *and* divergent.

Whole-file figures for reference: 6,094 fork lines inside upstream symbols
across 125 diverged top-level symbols; 737 fork-only symbols; 132 symbols
byte-identical with upstream. Note that much of the remaining "upstream not
applied" is deliberate fork replacement, not backlog — `build_worker_context`,
for instance, is 246 upstream lines against 11 fork lines because the fork
replaced it wholesale.

### The plan's hook target list is the wrong seven

**Read this before starting Axis B.** The plan picked its seven targets from
the **14 conflict hunks of one specific merge**. That measures what happened to
collide once, not the structural exposure. Ranked by fork lines inside an
upstream symbol — the metric that actually predicts future merge cost —
`scripts/refactor/upstream_divergence.py` gives a different top ten:

| fork lines inside | symbol |
|---:|---|
| 997 | `_dispatch_once_locked` |
| 448 | `complete_task` |
| 355 | `_migrate_add_optional_columns` |
| 299 | `create_task` |
| 295 | `block_task` |
| 277 | `_record_task_failure` |
| 268 | `detect_crashed_workers` |
| 252 | `SCHEMA_SQL` |
| 235 | `decompose_triage_task` |
| 186 | `archive_task` |

`_dispatch_once_locked` alone carries more fork code than the plan's entire
seven-symbol list. Four of the plan's seven (`_cleanup_worker_tmux` 7,
`list_comments` 3, `_backup_corrupt_db` 0, and `Task` at 147) are nearly noise
by comparison — and `_backup_corrupt_db` is now byte-identical with upstream,
so it is not a hook target at all.

This does not invalidate starting small. `_guard_existing_db_is_healthy` (49)
is still the right *pilot* precisely because it is small and the mechanics are
unproven. But the plan's ordering should not be followed past the pilot: after
it, go by this table, not by the old list.

**Axis A comes first, and this is not a preference — it is measured.** All seven
hook target symbols (`Task`, `create_task`, `_default_spawn`,
`_guard_existing_db_is_healthy`, `_cleanup_worker_tmux`, `list_comments`,
`_backup_corrupt_db`) are touched by the dropped commits. Hooking first would
draw the fork/upstream boundary against a stale upstream shape and then collide
with the adoption.

## 3. Two dead ends — do not re-run these

**Extracting fork code into a sibling package does not help merge capability.**
Measured end to end: conflict hunks **14 → 14**. It shrinks the file
(1,589,570 → 604,371 B) and helps CodeGraph, which is worth something, but it
moves the merge metric by zero. Only Axis B does.

**And the extraction does not currently work anyway.** Two attempts, both
reverted, nothing landed:

- *10 submodules* — the package would not import. Densely mutual submodule
  graph; 38 import-time forward references plus a `review_gate → task_lifecycle
  → workspace → review_gate` cycle.
- *One flat module* — imported fine, `API IDENTICAL`, but **64 tests failed**
  (reproduced on rerun). Cause: the test suite monkeypatches
  `hermes_cli.kanban_db` at **470 sites across 104 symbols**, almost always
  through an alias (`kb` 352×, `_kb` 78×). Extracted code binds those names at
  import time, so `monkeypatch.setattr(kb, "X", fake)` never reaches it.
  Carving the patched symbols out was built and measured — **does not fix it**;
  the 470 sites depend on `kanban_db` being one namespace.

Full analysis: [patch-targets.kanban_db.md](patch-targets.kanban_db.md).

## 4. How to measure — copy-paste, do not guess

**The old merge base is the ruler.** `origin/main` is now an ancestor of `main`,
so `git merge-tree HEAD origin/main` reports **0 conflict hunks**. That is an
artefact of "already up to date", not success. Anyone reading that 0 as a pass
is reporting green without measuring.

```bash
# Upstream work on the file we have not taken (the Axis A gap)
git diff --stat 3bfa6001f origin/main -- hermes_cli/kanban_db.py
git log  --oneline 3bfa6001f..origin/main -- hermes_cli/kanban_db.py
```

**Symbol-level divergence (the Axis B metric).** Use the committed script — it
compares file contents directly, so unlike `merge-tree` it stays valid after a
sync:

```bash
python3 scripts/refactor/upstream_divergence.py hermes_cli/kanban_db.py
python3 scripts/refactor/upstream_divergence.py <path> <ref> --json   # full record
```

It prints the two headline numbers (upstream lines not applied; fork lines
inside upstream symbols) plus the worst offenders. Indentation-based extraction
is useless here — `create_task` has a multi-line signature whose closing
`) -> str:` sits back at indent 0, so a strict scan measures 36 lines instead of
491. The script uses `ast.parse` with `lineno`/`end_lineno`; anything else you
write must too.

Snapshot after the Axis A adoption: 4 upstream symbols still absent (97 lines,
all the attachment toolset the fork replaced), 125 top-level symbols diverged,
132 byte-identical, 737 fork-only.

**Ground truth beats line counts.** Prefer "these N upstream tests go red→green"
as the acceptance criterion. Line counts drift; a test file that upstream
shipped with the feature does not.

## 5. Working rules that were paid for

- **`git apply -3` does not work on this file.** On the smallest commit (8
  one-line changes) it produced 7 conflict regions spanning hundreds of lines.
- **Copy verbatim only what the fork does not have.** 19 symbols / 411 lines
  qualify — zero conflict risk. Everything else gets woven by hand; every shared
  symbol carries fork logic upstream knows nothing about.
- **Never edit an upstream test file to make it pass.** That adds exactly the
  merge burden this workstream exists to remove. If the fork's contract
  legitimately differs, add a fork-owned test beside it — see
  `tests/plugins/test_kanban_model_override_fork.py` for the pattern.
- **Check for half-adopted merges.** `tools/kanban_tools.py` had taken
  upstream's `provider` argument *and its validation* while losing the
  `provider_override=` line at the fork-diverged call site: the tool accepted a
  provider, validated it, and threw it away silently.
- **Control-probe the collection step, not just the decision step.** The 470
  monkeypatch sites were missed by a grep-based sweep that reported 9. A control
  probe was run and still missed it — it proved the *classifier* worked, never
  that the *input list* was complete.
- **`rg`, never `grep -r`** (worktrees are git-excluded), and never `head -N | rg`
  on this file.

## 6. Map of the other documents

| file | what it is |
|---|---|
| [upstream-backlog.kanban_db.md](upstream-backlog.kanban_db.md) | the 12-commit ledger, the 9 documented divergences, adoption method |
| [2026-07-24-giant-module-modularization-plan.md](2026-07-24-giant-module-modularization-plan.md) | the binding plan; Task 8 (hooks) is Axis B |
| [ownership.kanban_db.md](ownership.kanban_db.md) | per-symbol ownership: fork-only / upstream-identical / diverged |
| [patch-targets.kanban_db.md](patch-targets.kanban_db.md) | the 470 monkeypatch sites and why extraction breaks them |
| [boundary-map.kanban_ext.yaml](boundary-map.kanban_ext.yaml) | the extraction boundary map (Axis-B-adjacent, currently unused) |
| `scripts/refactor/upstream_divergence.py` | measures both metrics; run it before and after any Axis A/B work |
| `scripts/refactor/split_module.py` | the AST mover used for pure-move extraction |

## 7. The next concrete step

Axis A is closed. What remains, in order:

1. **Start Axis B** with `_guard_existing_db_is_healthy` (49 fork lines) as the
   pilot — small, and the mechanics are unproven. Characterization test
   committed **first** and green against the unmodified function, then lift the
   fork logic into a fork-owned hook. One symbol per commit.
   Hook design constraint: hooks are **pure functions taking what they need as
   parameters** (conn, row, config) and must not import `kanban_db` at module
   level — that is what made the extraction attempts deadlock (§3).
2. **After the pilot, follow the measured table in §2, not the plan's seven.**
   `_dispatch_once_locked` (997), `complete_task` (448) and
   `_migrate_add_optional_columns` (355) dominate; the plan's list was derived
   from one merge's conflict hunks and mis-ranks the work.
3. **Report `test_wal_checkpoint_truncates_wal_file` upstream** — it is missing
   a SQLite-version skip and is red against upstream's own tree on any host with
   SQLite < 3.51.3.
4. **Consider re-examining the fast-connect guard skip** (§3a in the backlog
   ledger). The fork has no connect-time detection of silent index corruption
   on a stamped DB. That was an acceptable trade when there was no remedy;
   `hermes kanban repair` now exists. This is a product decision for the
   operator, not a silent change to make.

**Standing rule for everyone, not just this workstream:** new fork code does not
go into an upstream-owned file. Put it in a fork-owned module and call it from
one line. Every line that ignores this is a line the next sync has to fight.
