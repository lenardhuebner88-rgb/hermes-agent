# Upstream backlog ledger — `hermes_cli/kanban_db.py`

**What this tracks:** the upstream work the 2026-07-24 sync silently discarded,
and the state of adopting it back.

Status of this file: living ledger. Update the status column when a commit is
adopted; do not delete rows.

---

## 1. What happened

The 2026-07-24 upstream sync (`59191ccec`, 1,962 upstream commits) resolved
`hermes_cli/kanban_db.py` as **ours**. Every other file merged normally.

That single resolution dropped **12 upstream commits / 673 insertions / 20
deletions** on that file:

```bash
git log  --oneline 3bfa6001f..origin/main -- hermes_cli/kanban_db.py   # 12
git diff --stat     3bfa6001f..origin/main -- hermes_cli/kanban_db.py  # +673 -20
```

`3bfa6001f` is the merge base that was in force before the sync — it is still
the correct ruler, because `origin/main` is now an ancestor of `main` and any
`origin/main..HEAD` measurement reads as trivially up-to-date.

**The damage is asymmetric, and that is the whole point.** The merge took
upstream's *tests* while rejecting upstream's *implementation*. The fork
therefore shipped test files asserting behaviour it did not have. Measured on
`5e3b9d282`, per-file pytest:

| file | red before adoption |
|---|---:|
| `tests/hermes_cli/test_kanban_db_repair.py` | 22 |
| `tests/plugins/test_kanban_model_override.py` | 14 |
| `tests/hermes_cli/test_kanban_worktree_isolation.py` | 2 |
| `tests/tools/test_delegate_kanban_isolation.py` | 2 |
| `tests/tools/test_kanban_tools.py` | 2 |
| **total** | **42** |

Those 42 reds are the acceptance criterion for this workstream — a ground-truth
test, not a line count.

## 2. The ledger

| # | commit | what it brings | status |
|---|---|---|---|
| 1 | `899513145` | auto-repair index-only corruption via REINDEX | **adopted** `1315b7417` |
| 2 | `8fb3cc1b1` | cap corrupt-backup retention at 10 per board | **adopted** `1315b7417` |
| 3 | `49828a3fd` | periodic WAL checkpoint (TRUNCATE) on the dispatch tick | **adopted** `1315b7417` |
| 4 | `60cfa1113` | `hermes kanban repair` CLI verb | **adopted** `1315b7417` |
| 5 | `c1b0f6f3c` | per-task model + provider override | **adopted** `64564574f` |
| 6 | `65d42e35d` | stop decompose siblings sharing one worktree | **adopted** (this slice) |
| 7 | `b9b5481d6` | preserve cross-profile project child routing | **adopted** (this slice) |
| 8 | `6833eabb5` | isolate worker-created child workspaces | **adopted** (this slice) |
| 9 | `a7dcf9787` | harden delegated-child mutation boundary | **adopted** (this slice) |
| 10 | `c89481db5` | explicit UTF-8 on all `subprocess(text=True)` | **adopted, widened** — see below |
| 11 | `3fccd698f` | attachment toolset + CLI | **not needed** — fork has an equivalent; `tests/plugins/test_kanban_attachments.py` is green |
| 12 | `f3cbe4560` | unify attachment size cap on `KANBAN_ATTACHMENT_MAX_BYTES` | **not needed** — the fork already routes every surface through that constant; `_MAX_ATTACHMENT_BYTES` in `plugin_api.py` is only an alias assigned from it |

**The backlog is cleared: 10 adopted, 2 already satisfied.**

On #10: upstream fixed 8 call sites in this file. An AST sweep for
`subprocess.*(text=True, …)` without `encoding=` found **19** — the fork had
added 11 more of the same bug since. All 19 are fixed, so the sweep now returns
zero. No test covers this in `kanban_db.py`
(`tests/tools/test_subprocess_utf8_encoding.py` scans other modules), so the
evidence is the AST sweep itself, re-runnable from
[UPSTREAM-STRATEGY.md](UPSTREAM-STRATEGY.md) §4.

## 3. Documented divergences — reds that must NOT be "fixed"

Nine reds survived adoption; **four remain** after 2026-07-25 (§3d was resolved
without touching upstream's tests — see there). Every one was traced to a cause;
none is a defect in the adopted code. Do not chase them without reading this
section.

Current count on this host, measured: **7 red** across the affected set — the
four below, plus the three in §3c that are not fork divergences at all.

### 3a. Fork skips the connect-time integrity guard (2 reds)

- `test_connect_auto_repairs_index_only_corruption`
- `test_guard_fails_closed_when_reindex_does_not_clean`

The fork's `connect()` takes `_try_fast_connect` for a DB carrying the schema
stamp and never reaches `_guard_existing_db_is_healthy`. Verified directly: a
spy on the guard counts **0** calls on a stamped reconnect.

That is deliberate — the full path ran once per process under an exclusive
flock, serializing every dashboard request behind a 120 s busy timeout.

**Consequence worth knowing:** the fork has no connect-time detection of *silent*
index corruption on an already-stamped DB. Corruption bad enough that SQLite
refuses to open the file still hits the guard. `hermes kanban repair` (adopted
above) is the explicit remedy and the reason adopting it mattered.

### 3b. Fork confines `HERMES_KANBAN_DB` to the active home (1 red)

- `test_dispatch_tick_runs_wal_checkpoint_at_interval`

The test points `HERMES_KANBAN_DB` at a `tmp_path`; the fork refuses an override
outside `kanban_home()` ("ignoring inherited kanban path override … outside
active home"). Upstream has no such guard — `rg -c 'ignoring inherited kanban
path override'` on `origin/main:hermes_cli/kanban_db.py` returns 0.

The checkpoint itself fires correctly; only the *key* under which it is recorded
differs, so the test's `_LAST_WAL_CHECKPOINT[key]` lookup raises `KeyError`.

### 3c. NOT a fork divergence — upstream tests missing a SQLite version skip (2 reds)

- `test_wal_checkpoint_truncates_wal_file` (asserts a `-wal` file exists)
- `test_kanban_db_init.py::test_fast_path_applies_connection_pragmas`
  (asserts `PRAGMA journal_mode == "wal"`)

Both fail because SQLite's WAL-reset corruption bug (fixed in 3.51.3, backports
3.50.7 / 3.44.6) makes Hermes fall back to `journal_mode=DELETE`. The system
python links **3.45.1**, the project venv **3.50.4** — both vulnerable.

That gate is **upstream's own code** (`hermes_state.py`, same `>= (3, 51, 3)`
check in both trees), so these are red against upstream on any host with a
vulnerable SQLite. They need a version skip marker — a candidate to report
upstream, not something to fix here.

**Proven, not assumed** for the second one: it was reproduced in a detached
`git worktree` at unmodified `HEAD`, failing identically. It appears in the
affected-test set only because that file imports `kanban_db`.

The practical consequence is worth stating plainly: **this host cannot run
Hermes on WAL at all.** Fixing that is a toolchain upgrade
(python-build-standalone's embedded SQLite; `hermes update` alone may not move
it — see `hermes doctor`), not a code change.

### 3d. Fork validates assignee spawnability — RESOLVED 2026-07-25 (was 5 reds)

- `test_patch_sets_model_override`, `test_patch_clears_model_override`,
  `test_patch_provider_without_model_is_400`,
  `test_create_task_with_override_via_api`, `test_bulk_model_override`

All five died inside the shared `_create()` helper, which builds tasks with
`assignee="worker"`. The fork rejects assignees with no on-disk Hermes profile
("Assignee 'worker' is not spawnable"). They never reached the override code.

**These are now green, and legitimately so.** `tests/plugins/conftest.py`
provisions a real `profiles/worker` directory inside the test's own temporary
`HERMES_HOME`, so the fork's guard executes and *passes* on a precondition that
is actually satisfied. Two cheaper routes were rejected: editing upstream's test
file (adds the merge burden this workstream removes) and monkeypatching
`profile_exists` to `True` (deletes the guard from the run — a green test that
skipped a real code path is worse than a red one).

Control probe, not assumption: provisioning the directory under a *wrong* name
puts exactly these five back to red, which is what proves the guard still runs.

`tests/plugins/test_kanban_model_override_fork.py` is kept. It covers the same
five flows under the fork's real `default` profile, so the two files now assert
the contract against a provisioned profile and against a real one.

## 4. Method notes for the next session

**`git apply -3` does not work on this file.** Tried on the smallest commit
(`c89481db5`, 8 one-line changes): produced **7 conflict regions** spanning
hundreds of lines, because the fork diverged around every hunk. Reverted.

**What does work, in this order:**

1. Symbols upstream has and the fork does not (19 of them, 411 lines) copy in
   verbatim — no conflict is possible. That is ~60 % of the backlog.
   `scripts/refactor/` has the machinery; a plain AST extract is enough.
2. Symbols both trees have get **woven by hand**, never overwritten. Every such
   symbol carries fork logic that upstream does not know about. Examples from
   this workstream:
   - `_guard_existing_db_is_healthy` — the fork's retry/backoff loop was kept
     and upstream's message-list probe folded into it.
   - `set_model_override` — did not duplicate the fork's lane-aware
     `set_task_model_override`; both now share one write core.
   - `_default_spawn` — upstream's `--provider` branch also closed a fork bug
     (a task-level model override suppressed the lane provider entirely).
3. Verify per commit with the upstream test file it shipped with. Red→green is
   the evidence; put the before/after counts in the commit message.

**Watch for half-adopted merges.** `tools/kanban_tools.py` had taken upstream's
`provider` argument *and its validation* while the fork-diverged `create_task(...)`
call site lost the `provider_override=` line. The tool accepted a provider,
validated it, and silently discarded it. Whenever a symbol appears in an
argument list but nowhere else, check the call site.

**Measurement, not guessing.** `scripts/…`-free one-liner for the current gap:

```bash
git diff --stat 3bfa6001f origin/main -- hermes_cli/kanban_db.py
```

and for the symbol-level view:

```bash
python3 scripts/refactor/upstream_divergence.py hermes_cli/kanban_db.py
```
