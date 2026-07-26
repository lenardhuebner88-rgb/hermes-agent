# Upstream state-hardening gap — what NousResearch shipped that we do not have

Date: 2026-07-26 · Branch: `qwen/upstream-state-20260726` (worktree
`/home/piet/.hermes/worktrees/qwen-upstream-state-20260726`)
Range measured: `HEAD..origin/main` after `git fetch origin main`
(`ok fetched (2 new refs)`), fork HEAD `f4183cf56`.

Every number below was measured in this session. The command is listed with
each claim. Line numbers were resolved at measurement time via `rg -n` /
`ast.parse`, never copied from other documents.

---

## 1. Gap inventory

### 1.1 Range size

| Measurement | Command | Result |
|---|---|---|
| Commits upstream has that we lack (raw) | `git rev-list --count HEAD..origin/main` | `197` |
| Merge base | `git merge-base HEAD origin/main` | `760112adb6458417da8614d2269e5325f0739ed5` |
| Commits after patch-dedup | `git rev-list --cherry-pick --right-only --count HEAD...origin/main` | `193` |
| Title filter (state/sqlite/db/lock/kanban/worker/dispatch) | `git log --oneline HEAD..origin/main \| rg -ci "<keywords>"` | `20` |
| Commits touching `hermes_cli/` or `hermes_state.py` | `git log --format='%h\|%ci\|%s' HEAD..origin/main -- hermes_cli/ hermes_state.py` | `37` |
| Commits touching `tests/` | `git log --format='%h' HEAD..origin/main -- tests/ \| wc -l` | `76` |

The raw 197 overcounts: **4 upstream commits are patch-identical to commits
already in our tree** (parallel landings, different SHA). Proof for the
compression-lock fix:

```
$ git patch-id < <(git show 11c487e40) | cut -d' ' -f1
47d4f9d7b4c045aff0b6241299cd387b871fb87e
$ git patch-id < <(git show 4c5d2a22f) | cut -d' ' -f1
47d4f9d7b4c045aff0b6241299cd387b871fb87e
```

The 4 patch-equivalent upstream commits (enumerated by set difference of
`git rev-list --right-only HEAD...origin/main` vs `--cherry-pick --right-only`):
`11c487e40`, `2c6931674`, `32fd9d65c`, `9de7dfe1c` — all compression-related.

### 1.2 Relevant commits (state / SQLite / locking)

**Cluster 1 — lock-safe DB inspection (`sqlite_safe_read`).** Three commits,
2026-07-25, by teknium1:

| SHA | Title | Evidence |
|---|---|---|
| `fbd5e5772` | fix(state): stop cancelling our own POSIX locks on live SQLite databases | `git show --stat fbd5e5772`: `hermes_cli/sqlite_safe_read.py \| 177 +++…` (new), `hermes_cli/kanban_db.py \| 96`, `hermes_state.py \| 75`, `hermes_cli/backup.py \| 40`, tests +351 |
| `95fb47785` | fix(state): close the tracking leak and finish the audit of raw DB reads | `git show --stat 95fb47785`: `sqlite_safe_read.py \| 48+`, `kanban_db.py \| 24`, `hermes_state.py \| 65`, tests +92 |
| `fe431651c` | fix(state): make the byte-probe guard atomic, path-correct, and fail-closed | cumulative `git diff --stat fbd5e5772~1 fe431651c -- hermes_state.py hermes_cli/backup.py hermes_cli/kanban_db.py`: `+178 −64` |

End state upstream: `hermes_cli/sqlite_safe_read.py` **409 lines**
(`git show origin/main:hermes_cli/sqlite_safe_read.py | wc -l`),
test `tests/test_sqlite_lock_safe_inspection.py` **459 lines**.

**Do we have it?** No, and no behavioural equivalent exists either:

```
$ ls hermes_cli/sqlite_safe_read.py
ls: cannot access 'hermes_cli/sqlite_safe_read.py': No such file or directory
$ rg -l "POSIX advisory|howtocorrupt|cancel.*locks|live.connection registry" hermes_cli/ hermes_state.py gateway/
EXIT=1   (no matches)
$ rg -c "sqlite3" hermes_state.py     # control probe: search machinery works
139
```

But we DO have all three vulnerable probe sites the commits fix:

- `hermes_cli/kanban_db.py:4439` `_check_file_length_invariant` — raw
  `open(path, "rb"); f.seek(28); f.read(4)` on the live board, called after
  commits from `kanban_db.py:4553` (`rg -n "_check_file_length_invariant" hermes_cli/kanban_db.py`).
- `hermes_state.py:1660` `is_zeroed_state_db` — raw `open(path,"rb"); fh.read(...)`
  fallback, run on every `SessionDB.__init__` before connect (measured: the
  zeroed check precedes `_connect_and_init()` in `__init__`).
- `hermes_cli/backup.py:300-301` and `384-385` — raw `open(path,"rb"); read(16)`
  header probes.

**Test-without-impl direction:** we have neither the module nor the test
(`ls tests/test_sqlite_lock_safe_inspection.py` → missing; control probe: the
four must-exist files `kanban_db.py`, `hermes_state.py`, `backup.py`,
`tests/conftest.py` all reported present in the same check).

**Cluster 2 — offline state.db recovery (`session_recovery`).**

| SHA | Title | Stat evidence |
|---|---|---|
| `a9b8128bc` | fix(sessions): add offline state database recovery | `hermes_cli/session_recovery.py \| 817+` (new), `hermes_cli/main.py \| 137` |
| `ec2a0f8c1` | fix(sessions): point failed in-place repair at offline recovery | `main.py \| 13+` |
| `508764d38` | fix(sessions): add opt-in partial database recovery | `session_recovery.py \| 408+` |
| `a1c4d9995` | fix(sessions): refuse to snapshot a live database during recovery | `session_recovery.py \| 21+` |
| `9657f6e34` | fix(sessions): close the snapshot check/use race and guard damaged state_meta | `session_recovery.py \| 79`, `sqlite_safe_read.py \| 35+` |
| `c8aa0c7a3` | fix(sessions): report damaged state_meta as loss, not absence | `session_recovery.py \| 30` |
| `d2e733e63` | fix(sessions): reconstruct missing sessions instead of deleting salvaged messages | `session_recovery.py \| 96` |
| `914059fad` | fix(sessions): retain all reconstructed sessions | `session_recovery.py \| 26` |
| `36926af26` | test(sessions): prove the connector reached the lock before asserting blocked | test-only |

End state upstream: `session_recovery.py` **1387 lines**,
`tests/hermes_cli/test_session_recovery.py` (~1100 lines, grew across 10
commits). We have **none of it** (`ls hermes_cli/session_recovery.py` →
missing). Note: this recovers `state.db` (sessions), not `kanban.db`; our
existing snapshot/quarantine machinery (`quarantine_zeroed_state_db`,
`state-snapshots/`) restores previous state but cannot salvage a damaged DB.
`9657f6e34` depends on Cluster 1 (`sqlite_safe_read.py | 35+`).

**Cluster 3 — compression-lock reclaim.** Already ours; see §2 basket C.

**Cluster 4 — WAL test gate.**

| SHA | Title | Stat evidence |
|---|---|---|
| `07e97d2f5` | fix(tests): gate WAL-dependent tests on the linked SQLite's real capability | `tests/conftest.py \| 65+`, `pyproject.toml \| 1+`, `tests/hermes_cli/test_kanban_db.py \| 11`, new `tests/test_conftest_wal_gate.py \| 77` |

We have no capability gate in `tests/conftest.py`
(`rg -n "wal|WAL" tests/conftest.py` → only psutil walker lines). Collision
surface: `git diff --stat HEAD origin/main -- tests/conftest.py` →
`97 insertions(+), 83 deletions(-)` — real fork divergence, adoption is
hand-weaving, not verbatim.

### 1.3 Everything else in range (completeness)

The remaining backend commits in `HEAD..origin/main -- hermes_cli/ hermes_state.py`
are not state/locking work: tools disclosure (`0986ac393`, `e869accc1`,
`e9fe060eb`, `e7172ab1b`), MCP/tool schema rename `7b793f7d2` (the word
"dispatch" there is tool dispatch, not kanban dispatch), relay Phase 1
`ebab890ae`, curator `72de75c0a`/`b9fedab47`/`243a01d5d`, model-picker
`92549c9a6`/`9aefa4c61`/`f7001f968`, sessions UX `16042b0c4`/`c3d199c24`,
dashboard liveness `6179da549`, managed-uv SQLite repair `866cdce20`/`1161cc0b5`
(about uv-managed *Python's* libsqlite, not our DBs), update/WoA probes
`c03a977a9`/`fe3dd9106`, setup `339d96868`/`setup_hidden_env.py`, skills
`c537ae5f4`, aux streaming `5121a2a20`. None touch the kanban DB path.

---

## 2. Baskets

### A — stability now

**A1 (built this run): Cluster 1, `sqlite_safe_read` + call-site weaving.**
Concrete failure paths in *our* system, measured:

1. **state.db, every SessionDB construction, every journal mode.**
   `is_zeroed_state_db` (`hermes_state.py:1660`) raw-opens and reads the file
   before connecting. `SessionDB.__init__` runs this check before
   `_connect_and_init()`. A process that already holds a live connection to
   the same file (the gateway keeps SessionDB connections; `rg -c "SessionDB\(" gateway/run.py` → `2`
   direct constructions, plus indirect churn) cancels *that* connection's
   POSIX locks when the probe's fd closes — sqlite.org/howtocorrupt.html §2.2.
   `hermes sessions optimize` runs `VACUUM` on state.db
   (`rg -n "VACUUM" hermes_state.py` → `hermes_state.py:1101 conn.execute("VACUUM")`),
   so optimize + concurrent SessionDB construction in the same process + any
   other process writing = "database disk image is malformed".
2. **kanban.db, post-commit invariant.** `_check_file_length_invariant`
   (`hermes_cli/kanban_db.py:4439`) raw-reads header bytes at offset 28 after
   commits (`kanban_db.py:4553`). It skips WAL-mode connections — and on this
   host the runtime interpreter currently opens kanban.db in WAL
   (measured: `PRAGMA journal_mode` on a fresh board via
   `venv/bin/python` → `wal`, `sqlite_version: 3.53.1`), so this path is
   *currently shielded in production here*. It is **unshielded** whenever the
   board runs in DELETE mode: the NFS/SMB/FUSE and `LOCKING_PROTOCOL`
   fallbacks, any interpreter linking SQLite < 3.51.3 (measured: the test
   interpreter `.venv/bin/python` links `sqlite 3.50.4`), and any other host.
3. **backup.py header probes** (`hermes_cli/backup.py:300-301`, `384-385`),
   same class, run in CLI processes that may hold other connections.

Upstream's A/B evidence (commit message `fbd5e5772`, quoted): *"raw open/close
during VACUUM: 8 vacuums, 319 vacuum errors, 2/2 corrupt — no raw read
(control): 229 vacuums, 0 vacuum errors, 0/2 corrupt — after this change: 0/4
corrupt, 0 vacuum errors."*

**A2 — downgraded, see B4.** The WAL gate would stabilise the CI signal, but
the red-noise premise no longer reproduces on this host (measured below).

### B — update capability

Metric per the standing rule: fork lines *inside upstream-owned symbols*
(`python3 scripts/refactor/upstream_divergence.py <file>`).

| Item | Evidence | Effect |
|---|---|---|
| **B1:** adopt `sqlite_safe_read.py` verbatim | part of A1 build | new file byte-identical to upstream → future merges of it are free |
| **B2:** converge probe symbols (`is_zeroed_state_db`, `_backup_db_file` guard, backup probes, `_check_file_length_invariant`) to upstream bodies | `upstream_divergence.py hermes_state.py`: 203 fork lines inside upstream symbols (194 in `SessionDB`); `backup.py`: 140 (`run_backup` 72); `kanban_db.py`: **6152** (`_dispatch_once_locked` 997) | replacing raw-probe bodies with upstream's bodies *reduces* the collision metric for exactly those symbols; kanban_db.py stays hot — touch only the one symbol |
| **B3:** adopt `tests/test_sqlite_lock_safe_inspection.py` verbatim | part of A1 build | pins the contract with upstream's own words; no fork-owned paraphrase to drift |
| **B4 (was A2):** WAL capability gate `07e97d2f5` | measured 2026-07-26 with `.venv/bin/python` (sqlite 3.50.4): `test_fast_path_applies_connection_pragmas` EXIT=0 `1 passed`; `test_connect_falls_back_to_delete_on_locking_protocol` EXIT=0 `1 passed`; `test_wal_checkpoint_truncates_wal_file` EXIT=0 `1 passed`; `test_write_txn_wal_mode_ignores_transient_main_file_size_lag` EXIT=4 `no tests ran` (name `rg -l "def <name>" tests/` → not present) | the 4 permanently-red tests recorded in project memory (2026-07-25) are 3× green and 1× gone; the gate is still worth adopting (robustness on hosts with old libsqlite, e.g. the system interpreter), but it is CI hygiene, not an acute fix — next round |
| **B5:** `session_recovery` (Cluster 2) | `upstream_divergence.py hermes_cli/main.py`: 308 byte-identical symbols, 0 fork-only, 321-line backlog | salvage path for a damaged state.db instead of restore-from-snapshot; main.py weaving is cheap; ~1.4k-line module verbatim; depends on Cluster 1 (adopted this run) → round 2 |

### C — deliberately not

| Item | Why | Evidence |
|---|---|---|
| **C1:** compression cluster (`11c487e40`, `2c6931674`, `32fd9d65c`, `9de7dfe1c`) | already in our tree as parallel landings | identical patch-ids (§1.1); our `4c5d2a22f` ≡ upstream `11c487e40`; `refresh_compression_lock` and `_compression_lock_holder_process_is_dead` byte-identical to origin/main (243 of our hermes_state.py symbols are byte-identical overall) |
| **C2:** re-adopt `tests/hermes_cli/test_kanban_core_functionality.py` | deliberately split by the fork | present at merge-base (`git show 760112adb:…` → 4788 lines); removed by our own `c05ffda1d` "test(kanban): split test_kanban_core_functionality into domain files" (2026-07-16); no in-range upstream commit touches it |
| **C3:** the 9 documented intentional red tests | standing divergence list | per AGENTS.md / prior cleanup (42 → 9) |
| **C4:** kanban `connect()` tracking weave (upstream `_sqlite_connect` uses `connect_tracked`) | fork's `connect()` is an 89-line diverged symbol (`ast.parse` → lines 3434–3522) while upstream restructured into `_sqlite_connect`; the switch forces a monkeypatch target migration (`hermes_cli.kanban_db.sqlite3.connect` → `hermes_cli.sqlite_safe_read.sqlite3.connect`, visible in upstream's own test edit, hunk `@@ -3276` of `95fb47785`'s test diff) across our split test files; benefit is intra-process only (registry is per-process) and the invariant fix (A1) already removes fd-opens on the hot path | deferred to round 2 with the patch-target migration done properly |
| **C5:** everything in §1.3 | not state/locking work | listed there |

---

## 3. Adoption plan and costs

### Round 1 (this run): Cluster 1

| Step | Method | Collision surface | Test acceptance | Risk |
|---|---|---|---|---|
| 1. `hermes_cli/sqlite_safe_read.py` | **verbatim** (`git show origin/main:hermes_cli/sqlite_safe_read.py`) | none (new file) | module self-tests in step 4 | none |
| 2. `hermes_state.py` connect tracking | adopt upstream's `_connect_tracked_db` helper verbatim; route the two `SessionDB` connect sites (measured: `hermes_state.py:1877` read-only, `:1919` writer) through it; add the `_backup_db_file` live-connection guard (upstream `hermes_state.py:804`) | `SessionDB` carries 194 fork lines inside the upstream symbol — but the edits touch only the connect lines themselves | `test_session_db_read_only_is_tracked` in the verbatim test file | a regression here breaks every SessionDB open; mitigated by targeted `tests/test_hermes_state.py` run |
| 3. Probe weaving | `is_zeroed_state_db` → `read_header_bytes_preopen` (upstream body, keeping our backup-import preference only if it survives the lock rule — it does not: `backup.is_zeroed_sqlite_file` raw-opens, so it must go); `backup.py:300-301` → `read_header_bytes_preopen`; `backup.py:384-385` → `read_header_bytes_preopen(..., force=True)` for offline snapshots, mirroring upstream | `is_zeroed_state_db` 7 fork lines inside; `verify_sqlite_integrity` 11; `is_zeroed_sqlite_file` 6 | `test_write_lock_survives_zeroed_state_db_probe` (imports `hermes_state.is_zeroed_state_db`, asserts lock survival) | `read_header_bytes_preopen` **returns None while a connection is live** — the zeroed check must treat None as "not zeroed" (upstream semantics); a wrong interpretation silently disables zeroed-DB detection |
| 4. Verbatim test | `tests/test_sqlite_lock_safe_inspection.py` copied unchanged | our `tests/conftest.py` must not break it (imports: module + `hermes_state.SessionDB` + stdlib/pytest only — verified by reading the file) | **14/14 green unmodified** is the acceptance gate | none if steps 1–3 hold |
| 5. `kanban_db.py` invariant | replace `_check_file_length_invariant` body with upstream's delegate to `file_length_matches_header` (PRAGMA + `stat()`, never opens the file) | one symbol in the most-diverged file; symbol name and call site unchanged, so the 3 monkeypatch sites in tests (`test_kanban_write_txn_busy_retry.py:54`, `test_kanban_db_runtime.py:527`, `:551`) keep working | `tests/hermes_cli/test_kanban_db_runtime.py` green after a fork-side `_FakeConn` adaptation (it must answer `PRAGMA page_count`; upstream made the analogous change in their monolith test, hunk `@@ -4728`) | message text changes ("torn-extend detected: …"); our runtime test's `match="torn-extend|page count mismatch"` still matches |

Hard rule honoured: no *new fork code* in upstream-owned files — every woven
body is upstream's own (convergence), and the new module is upstream-owned.

**Not built this round** (documented, not dropped): Cluster 2 (B5) and the WAL
gate (B4) — plan skeletons in §2; kanban connect tracking (C4).

---

## 4. What was built

*(filled in as commits land — see commit messages for test evidence)*

---

## 5. What I could not measure

- **Production overlap frequency.** How often `hermes sessions optimize`'s
  VACUUM actually overlaps a SessionDB construction in the running gateway —
  would require instrumenting live services, which is out of scope (no
  service touched). The corruption window's *existence* is measured; its
  *firing rate* is not.
- **System interpreter SQLite version.** Memory claims system Python links
  SQLite 3.45.1; I did not re-measure it (irrelevant to the build; relevant
  only to B4's "other hosts" argument).
- **Full-suite health after weaving.** Per the standing test-scope rule only
  targeted files were run (one file per pytest process). A collection sweep
  before any merge of this branch is still owed.
- **`tests/conftest.py` WAL-gate weaving cost** beyond the diffstat
  (97+/83−): not attempted, estimate only.

## 6. Refuted — briefing assumptions that failed measurement

1. **"183 upstream commits."** Measured **197** raw (`git rev-list --count
   HEAD..origin/main`), **193** after patch-dedup. The range moved: the fetch
   added refs and the live checkout's HEAD (`a284156a9`) already sits ahead of
   this worktree's fork point (`f4183cf56`).
2. **Candidate table: `tests/hermes_cli/test_kanban_core_functionality.py`
   "(neu)".** Not new in range and not lost: present at the merge-base (4788
   lines), removed by our own deliberate split `c05ffda1d` (2026-07-16); no
   in-range commit touches it. It is a C item, not a gap.
3. **Candidate table: `11c487e40` "exakt unser Muster … das wir nicht haben".**
   We have it: our `4c5d2a22f` has the identical patch-id
   `47d4f9d7b4c045aff0b6241299cd387b871fb87e`. All four compression commits in
   range are patch-equivalent to fork commits. `rev-list` without
   `--cherry-pick` overcounts parallel landings — the briefing's gap list
   inherited that overcount.
4. **Briefing §4: "`venv` (ohne Punkt) hat pytest, `.venv` nicht."** Inverted.
   Measured: `venv/bin/` has **no** pytest (sqlite 3.53.1); `.venv/bin/` has
   **pytest 9.0.2** (sqlite 3.50.4). The working test command uses
   `~/.hermes/hermes-agent/.venv/bin/python`.
5. **Memory: "4 kanban tests permanently red (host SQLite too old)."** No
   longer reproducible: 3 of the 4 names pass under `.venv` (sqlite 3.50.4)
   today, the 4th name no longer exists (§2 B4 evidence). The red-noise
   premise for the WAL gate is stale for this host; the runtime `venv` links
   sqlite 3.53.1 and opens kanban.db in WAL (measured `PRAGMA journal_mode` →
   `wal`), which also shields the kanban post-commit probe path in production
   *here* — the A1 rating rests on the state.db probe path and on
   DELETE-mode fallbacks, not on this host's kanban path.
6. **Großfilter "19".** 20 after the fetch (+1 new commit); one transient
   pipe-run reported `1` once and was not reproducible (file-based and
   repeated pipe runs agree on 20). Direction correct, count stale.

## 7. Operator decision needed

1. **Round 2 scope:** adopt Cluster 2 (`session_recovery`, ~1.4k lines +
   `main.py` CLI wiring) immediately after this lands, or wait for the next
   upstream sync round? Recommendation: adopt — it depends only on Cluster 1
   (done here) and `main.py` is cheap (308 identical symbols, 0 fork-only).
2. **Test interpreter SQLite:** `.venv` links 3.50.4 (wal-reset-vulnerable).
   Bumping it to ≥ 3.51.3 removes the DELETE fallback from the test matrix
   and retires the WAL-gate question for this host. No service was touched
   this run; this is an operator task.
