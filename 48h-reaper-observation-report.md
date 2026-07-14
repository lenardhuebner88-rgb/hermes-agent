# 48h Observation — Retention/Browser Reaper Dry-Run vs Baseline

**Task:** t_33e2669f · **Evaluated:** 2026-07-14 08:19–08:35 CEST (time gate 2026-07-14T06:05+02 — **met**)
**Mode:** observation only. **No delete/apply activation performed. No code changed.**

## 1. Timer / error status — both units healthy

| Unit | Runs in 48h | Failures | Cadence |
|---|---|---|---|
| `agent-retention-reap.service` | 2 (07-13 04:46, 07-14 04:47) | 0 — all `Finished`, no `Failed`, exit 0 | daily |
| `agent-browser-reap.service` | 191 | 0 — no error/traceback in journal | ~15 min |

Timer activated 2026-07-12T06:05:44 (`Started agent-retention-reap.timer`). Zero unit failures in the whole window.
(`systemctl` is blocked for spawned workers by the FS-cage; status derived from `journalctl --user` + unit files.)

## 2. Baseline comparison

| Metric | Baseline (07-12) | Now (07-14 08:2x) | Delta |
|---|---|---|---|
| Root-FS | 92% → 84% after 07-12 cleanup (158G used) | **78%** (167G used, 49G avail, 226G size) | Use% ↓, but **absolute usage +9 G** |
| retention dry-run | actions=443, browser_status=ok | actions=449, browser_status=ok | +6 |
| browser dry-run | scanned=215, candidates=0 | scanned≈345–361, **candidates=0** | scan pop. ↑, still 0 |
| playwright-mcp-output | 150 MiB | 153 MiB / 1847 files | **0 files written in 48h** (fully stale) |

### The disk improvement is NOT reclamation
Kernel, 2026-07-13 23:05:19: `EXT4-fs (dm-0): resizing filesystem from 52428800 to 60293120 blocks` — the root FS was **extended by ~30 GiB**. The reapers deleted nothing (dry-run). Absolute usage still **grew 158G → 167G (~5 G/day)**. At that rate the new 49 G headroom is ~9–11 days. **Pressure was deferred, not solved.**

## 3. What an `--apply` would actually reclaim (from the 07-14 run, 449 actions)

| Category | Count | Bytes |
|---|---|---|
| `output` (playwright-mcp-output) | 433 | 49.9 MiB |
| `kanban-backup` (`~/.hermes/kanban.db.bak*`) | 16 | 100.9 MiB |
| `browser` | 0 | 0 |
| `worktree-dependency-cache` | **0 — fail-closed** | 0 |
| **TOTAL** | **449** | **150.7 MiB** |

150.7 MiB = **0.07 %** of the filesystem, and 0.5 % of the 30 GiB that had to be added. A blanket apply is not worth its risk.

## 4. Finding A (real risk): worktree-cache eviction has been INERT since it landed

- Landed 2026-07-12 20:26 (`19deafbd2 feat(retention): fail-closed worktree dependency-cache eviction`).
- **Every run since is fail-closed. `category=worktree-dependency-cache` action count over all journal history: 0.**
- Cause: `active_worktree_paths()` (`scripts/retention_reap.py:311-333`) scans `/proc/<pid>/cwd` to learn which worktrees are in use. PID 44298 = `gpg-agent --supervised` — same UID (1000), so it passes the `st_uid` check, but it is **non-dumpable**, so its `/proc` entry is root-owned and `readlink` raises `EPERM`. The `except OSError` branch only allowlists `comm ∈ {systemd, (sd-pam)}`; `gpg-agent` is not in it → `return set(), "fail-closed: ..."` aborts the **entire** scan, zeroing all worktree candidates.
- gpg-agent is a permanent per-user daemon → **this fail-closed is permanent and self-perpetuating.**
- Impact: the only category with real mass is disabled. `~/.hermes/hermes-agent/.worktrees` = 7.5 G, `~/.hermes/worktrees` = 8.9 G ≈ **16 G** of dependency caches that the feature was built to reclaim and never touched once.
- The fail-closed itself is **correct and must not be weakened**: if a cwd is unreadable, the reaper genuinely cannot prove the worktree is idle. The right fix is to extend the known-daemon `comm` allowlist (gpg-agent, ssh-agent, keyring) exactly as `systemd`/`(sd-pam)` already are, staying fail-closed for unknown PIDs. That is a code change and needs its own reviewed task.

## 5. Finding B (coverage gap): the 15 G backups directory is not in scope
`plan_backup_actions` globs only `~/.hermes/kanban.db.bak*` (`retention_reap.py:246`). **`~/.hermes/backups/` — 15.3 G, 129 entries, oldest 2026-05-09 — is entirely outside the reaper's coverage.** Together with the worktrees (~16 G) this is where the disk actually is; the reaper currently plans to delete 150 MiB of screenshots instead.

## 6. Finding C: browser reaper is stable but its kill path is UNPROVEN
- **200 runs ever, 0 `WOULD-KILL` lines ever.** Zero errors, zero candidates — the reaper has never once had a real orphan to act on. Flipping `--apply` today would be a no-op *and* would arm a `SIGTERM`→3 s→`SIGKILL` path (`browser_reap.py:196-209`) that has never been exercised against a live target.
- The 805 MB `playwright-mcp --headless` process (PID 2718838, alive 20.8 h, under a still-running `claude --print` session) is **out of scope by design**, not a matcher bug: `is_orphaned()` (`browser_reap.py:117-130`) requires PPID≤1 / dead parent / subreaper parent. Its parent is alive → skipped deliberately ("live parent — an in-use MCP, leave it"). If that 805 MB footprint is unwanted, that is a **session-lifecycle** question, not a reaper one.

## 7. Recommendation

**Primary — do NOT flip a global `--apply` on either unit. Keep both in dry-run, and fix the fail-closed instead.** Rationale: applying today buys 150 MiB (0.07 % of disk) while the one category that would buy ~16 G is silently inert, and the browser kill path has never been exercised. Applying now would create the *appearance* of an armed retention system while the actual disk driver keeps growing 5 G/day.

Order of work (each its own reviewed task; none of it done here):
1. **Fix Finding A** — extend the non-dumpable-daemon `comm` allowlist in `active_worktree_paths()`; keep fail-closed for unknown PIDs. Verify a dry-run then emits `worktree_status=ok` with a non-zero `category=worktree-dependency-cache` plan.
2. **Then** a *bounded* apply of `worktree-dependency-cache` only — note `retention_reap.py` has **no `--category`/`--max-bytes`/`--limit` flag** (args at :812-824), so a bounded apply needs either such a flag or root-args pointed to scope it. That is a prerequisite, not a detail.
3. **Close Finding B** — bring `~/.hermes/backups/` (15.3 G) under an explicit retention policy.
4. **Browser reaper** — validate `--apply` against a *synthetic* orphaned playwright-mcp before arming; otherwise it stays an unproven kill path. Left in dry-run it costs nothing.

**Alternatives considered.** (a) *Flip retention `--apply` now*: safe (categories are stale — 0 writes to playwright-mcp-output in 48h) but nearly pointless at 150 MiB, and it would mask the inert category. (b) *Continue pure dry-run observation for another window*: the units are already proven stable over 191+2 runs with zero errors — more observation of a reaper that plans 0 actions in its main category yields no new information. Both are dominated by the primary recommendation.

**Capacity note for the operator:** root FS is 78 % *after* a 30 GiB extension, growing ~5 G/day → ~9–11 days of headroom. This is the actual open risk, and the reaper as currently wired does not address it.
