---
name: hermes-fork-sync
description: 'State/Git des LIVE Hermes-Fork-Checkouts ~/.hermes/hermes-agent (parallel editiert!) - Upstream-Sync (NousResearch-Fixes holen), Merge-Konflikte (api.ts, package-lock), Branch-Wald aufraeumen, dirty git status klaeren, Fork pushen, haengender pytest-Lauf. Vor JEDEM Git-State-Eingriff in diesem Checkout konsultieren. NICHT fuer Feature-Bau (hermes-dashboard-dev) oder andere Repos.'
---

# Hermes fork — sync & update-ready

The operator (Piet) runs a **single-person** Hermes agent project and is **not a git/coding expert**.
Keep `~/.hermes/hermes-agent` clean and synced **safely and reversibly**; explain each step in plain
language; never do anything that can't be undone. Simplest move that fixes the real risk — no overbuild.

> **Safety + recovery SoT = the vault runbook:** `~/vault/03-Agents/Hermes/playbooks/hermes-fork-update-runbook.md`
> (schema-validated, born from the 2026-06-08 reset-to-origin rewind incident). Division of labour: **this
> skill owns the mechanics (the command flow); the runbook owns the safety rationale, the cardinal
> never-`reset --hard origin`-rule, and the recovery procedure.** Read the runbook before any
> history-changing step.

## The two remotes (get this right or you push to the wrong place)

```
origin     git@github.com:NousResearch/hermes-agent.git          ← UPSTREAM. Pull/merge FROM. NEVER push.
piet-fork  git@github.com:lenardhuebner88-rgb/hermes-agent.git    ← Piet's fork. Push HERE only.
```

`main` is Piet's working line and tracks `piet-fork/main`. **"Update-ready"** = `main` is clean,
contains all of Piet's work, is **0 commits behind `origin/main`**, and is pushed to `piet-fork/main`.

## Three reflexes before you touch anything

1. **LIVE checkout.** Other sessions (remote `ccd-cli`, Hermes workers) edit files here concurrently.
   `git status --short` first; any modified file you did **not** change belongs to another session —
   leave it (never commit/revert/stash it). `git push` only sends commits, so foreign *uncommitted*
   work stays safely local. See `[[feedback_hermes_live_checkout_concurrent_edits]]`.
2. **Safety net before any history-changing step:** `TS=$(date -u +%Y%m%dT%H%M%SZ); git branch
   "backup/before-<task>-main-$TS" main` (+ the work-branch). Costs nothing; `git reset --hard <backup>` undoes.
3. **Confirm the outward/destructive steps** (force-deletes, push) with the user in plain language first.

## The workflow (run top to bottom; skip steps that don't apply)

```bash
# A. Bring Piet's work onto main (if it's a side branch = main + extra commits)
git merge-base --is-ancestor main <work-branch> && git checkout main && git merge --ff-only <work-branch>

# B. Sync upstream (the big one) — re-fetch+re-merge once more at the end; "update-ready" = 0 behind
git fetch origin --prune
git rev-list --left-right --count origin/main...main    # left=behind, right=ahead
git merge --no-edit origin/main

# B2. MANDATORY merge audit — compares the actual merge result against the clean
# automerge of both parents. Every listed file was a manual resolution decision;
# each one needs a one-line justification in the merge receipt BEFORE pushing.
scripts/merge-audit.sh HEAD        # (exists since 2026-07-03; --strict for hook use)

# C. Verify (targeted; the full suite has a known trap — see below)
( cd web && ../node_modules/.bin/tsc -b --noEmit )              # root-hoisted binary; npx is a stub trap in worktrees
venv/bin/python -m pytest --co -q tests/ 2>&1 | tail -3       # collection sweep: want "0 errors" (catches dropped imports)
venv/bin/python -m pytest -q tests/<touched_or_new>...        # run the files the merge actually touched

# D. Push (outward — confirm first). NOT a fast-forward → STOP and report, never --force
git fetch piet-fork --prune
git merge-base --is-ancestor piet-fork/main main && git push piet-fork main:main
```

**Branch triage — verify by CONTENT, not `git cherry`** (the fork has been through resets, so commits
re-applied under different SHAs → `git cherry`'s patch-id is unreliable here). `git branch -d <b>` only
deletes branches fully merged to `main` and refuses otherwise (self-protecting) — run it on the obvious
ones. For a branch it refuses, find its signature (an env var / function / test name its tip
introduced) and `git grep -n "MARKER" main` — if present in `main`, it's stale → `git branch -D`. Keep
recent `backup/*` and any branch checked out in a worktree (`git worktree list`).

**Recurring conflict shapes:**
- `web/src/lib/api.ts` — both sides add the *same* feature under different names → unify on **our**
  naming, delete the duplicate (verify with `tsc`).
- A test file where both sides appended **different** sections at one spot → keep **both**, just remove
  the `<<<<<<< / ======= / >>>>>>>` markers.
- `web/package-lock.json` → don't hand-merge: `git checkout --theirs web/package-lock.json && npm install --package-lock-only --prefix web`.
- `modify/delete` on an upstream-removed component → if `git grep` finds no remaining refs, accept the deletion (`git rm`).
- **Silent auto-merge damage:** a clean auto-merge can still *drop an import* (seen: `import pytest` vanished) → the collection sweep in step C catches it.
- **Silent side-taking during conflict resolution (the worst failure class):** the v0.18 merge
  `413638a28` (2026-07-02) dropped work in BOTH directions — upstream features (`project_id`,
  `block_kind`, goal-judge gate in `kanban_tools.py`) AND fork hardening (`cron.py` lifecycle
  regex) — while code+tests landed on the same wrong side, so gates stayed green and the damage
  only surfaced via red *nights* and forensics days later. Same pattern as the v0.18-era
  "60 Dateien pauschal" incident. Detection is step B2 (`scripts/merge-audit.sh`): the deviation
  list vs. the clean automerge IS the checklist of manual decisions to justify one by one.

**Traps:**
- **venv:** the test venv is the main `venv/` (`venv/bin/python`; `.venv/` is consumer-free/deprecated
  since 2026-07-02 — do not reinstall into it). `venv` must have `acp` + friends (verified 2026-07-02:
  full collection sweep 0 errors) — if imports fail, run `uv pip install -e ".[all,dev]"` in `venv`.
  Also ensure no stray `/tmp/.git`.
- **Full-suite hang:** `pytest -q tests/` (large suite, ~1.4k test files) **hangs** on a pre-existing
  `delegate`/`tui` flake. If you must run it all: `--timeout=120 --timeout-method=thread` so a hang
  becomes a failure. For routine sync, tsc + collection sweep + touched/new files is enough.

## Landing a worktree branch while other sessions are active

Bridge-worktree work (`.claude/worktrees/bridge-*`) lands on `main` in the LIVE checkout —
often while 2–3 other sessions are mid-flight. The window between their commit waves can be
seconds wide, so **prepare first, land fast**:

1. **Prepare in YOUR worktree** (independent of the live tree's state): merge `main` into your
   branch, resolve, re-run the gates there (worktree gate commands: hermes-dashboard-dev
   §Build & verify, case B — `npm ci` in the worktree, then root-`.bin` binaries). Repeat whenever main moves — landing must be a
   ref-only operation when the window opens.
2. **Read the window:** `git -C ~/.hermes/hermes-agent status --short` + the coordination board
   (`coordination-open-sessions.py`). Foreign *uncommitted* files → do NOT land; sessions commit
   in waves, wait for the wave (or the operator sets the order). Never stash/reset foreign files.
3. **Land:** anchor tag first (`git tag land/<slug>-$(date +%Y%m%d-%H%M) main`), then try
   `git merge --ff-only <branch>`. **ff fails because main just moved again? A normal merge
   commit is the house norm for session landings** — `git merge --no-ff --no-edit <branch>`
   (zero conflicts expected if step 1 was fresh). No rebase of published work.
4. **Verify the push, don't trust it:** `git push piet-fork main` answering "Everything
   up-to-date" can mean ANOTHER session already pushed your merge — confirm with
   `git fetch piet-fork && git log -1 piet-fork/main`.
5. **Editable-install restart trap (the sharp edge):** services import `.py` straight from the
   live tree. NEVER restart gateway/dashboard or run `deploy_dashboard.sh` while foreign
   uncommitted `.py` edits sit in the live checkout — you would ship half-done foreign code
   (near-miss 2026-07-03: an in-flight auth middleware). Your merge can be safely on main
   while the RESTART/DEPLOY waits for its own clean window.

## Plain-language reporting
Piet is not a coder. Summarize in a small table (what changed · risk · reversible?), name the backup
branches as the safety net, quote the exact commands for anything destructive. Ask before push and
before any force-delete. Record the new state (new `main` SHA, behind/ahead vs upstream, what merged) —
format + the recovery procedure live in the vault runbook.
