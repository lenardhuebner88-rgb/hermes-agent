# Follow-ups from the two 2026-07-26 Qwen runs

Both runs landed on `main` (`cf7d6e4c9`, `4cddaee9a`). This file is the
short list of what was deliberately *not* done, in the order it should be
picked up. Every number below was measured on 2026-07-26; re-measure before
acting on it.

## 1. `kanban_landed`: close the coverage hole (small, do first)

`sweep()` only considers done tasks that carry `branch_name` **or** an
integration witness event:

```bash
# 370 of 612 done tasks (60.5%) — the rest are never looked at
```

The failure mode is not the 39.5% as such (many genuinely have no evidence
left), it is the *label*. A task whose branch exists by convention but whose
`branch_name` column is NULL is reported `na_commitless` — which reads as
"nothing to land" where "unknown" is the truthful answer. Live example at
measurement time: `t_87e7ee2e` (branch `kanban/t_87e7ee2e` exists, column is
NULL, classified `na_commitless`; the same code classifies the *branch*
`landed_drifted`).

Two changes, both in `hermes_cli/kanban_landed.py`:

- when `branch_name` is empty, try the `kanban/<task_id>` convention before
  giving up, and use the ref if it resolves;
- when there is no evidence at all, return `unknown`, not `na_commitless` —
  fail closed, the way `_classify_merge_commit` already does for an
  unverifiable content check.

Verify with `scripts/kanban_landed_check.py --task t_87e7ee2e --repo
/home/piet/.hermes/hermes-agent` (read-only): it should stop saying
`na_commitless`.

This should land **before** the nightly watcher is armed. A watcher running
at 60% coverage looks like it covers everything.

## 2. Arming the landed-state watcher (operator decision, not a task)

`scripts/kanban_landed_check.py --print-job-draft` prints the cron draft.
Nothing is armed. Without a sweep the materialization table stays empty and
the drifts nobody currently sees stay unseen — but arming it is Piet's call,
and item 1 should come first.

## 3. Upstream round 2 — `hermes_cli/session_recovery.py`

New upstream module (state.db salvage) we do not have; its test file was
touched 10× upstream, so it is where upstream learned the most this cycle.
The run rated the `main.py` wiring cheap (308 byte-identical symbols in that
file). Not started.

```bash
git log --oneline HEAD..origin/main -- hermes_cli/session_recovery.py
python3 scripts/refactor/upstream_divergence.py hermes_cli/main.py
```

## 4. Upstream round 2 — kanban `connect()` tracking

The remaining piece of the `sqlite_safe_read` adoption. Upstream restructured
`connect()`; our version is a fork-diverged ~89-line symbol, and the
monkeypatch targets move with it (visible in upstream's own test hunk). This
is the one that touches the 470-monkeypatch namespace, so it needs a
characterization test committed green *first*.

## What is explicitly NOT follow-up work

- **Splitting `kanban_db.py`.** Two attempts, two reverts, conflict metric
  moved by zero, one attempt broke 64 tests. See
  `docs/refactor/UPSTREAM-STRATEGY.md` §3.
- **The WAL policy.** Decided and adopted on 2026-07-26 (`8e133993c`);
  `apply_wal_with_fallback` is byte-identical with upstream. Nothing pending.
