---
name: loop-branch-repair
description: Repair a parked loop branch in its existing Hermes loop worktree so the next deterministic landing run can land it safely.
---

# Loop branch repair

Use this skill only for a recovery card whose title names `loop/<pack>`. The
card's scratch workspace is not the target repository. All Git work happens in
the existing pack worktree at `~/.hermes/loops/<pack>/wt`.

## Safety gate

1. Derive `<pack>` exactly from the card's `loop/<pack>` branch. Reject empty,
   absolute, or path-traversal values.
2. Stop without changing Git state if `~/.hermes/loops/<pack>/STOP` exists.
3. Respect `~/.hermes/loops/<pack>/.lock`. Do not proceed while
   `hermes-loop@<pack>.service` is active or while a non-blocking `flock` cannot
   acquire the pack lock. Keep the lock for the entire repair, not merely the
   initial status check.
4. In `~/.hermes/loops/<pack>/wt`, verify that the checked-out branch is exactly
   `loop/<pack>` and `git status --porcelain` is empty. If either check fails,
   report the state and stop. Never stash, discard, or overwrite foreign work.

## Repair

1. Record the current branch SHA and local `main` SHA. Do not fetch or push.
2. Run `git rebase main` in the loop worktree. Resolve only the recovery
   card's conflicts, stage the resolved files, and continue the rebase. If the
   intended resolution is ambiguous, abort the rebase and report the blocker.
3. Verify the worktree is clean and still on `loop/<pack>`.
4. From the repository root, run `scripts/run-affected.sh main`. This proves the
   complete rebased branch diff. A skipped, unmapped, or red affected gate is
   not success.

## Completion contract

Finish only when the branch is clean, ahead of local `main`, conflict-free, and
the affected gate is green. Report the old and new branch SHAs plus the exact
gate result. Do not merge or reset the branch to `main`: the next Landing-Loop
run must discover and land it itself.

Never force-push, delete the branch, edit the base checkout on `main`, bypass a
STOP/lock, or claim success from a conflict-free rebase without the gate.
