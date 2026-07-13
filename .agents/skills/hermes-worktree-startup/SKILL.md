---
name: hermes-worktree-startup
description: Start isolated Hermes implementation, audit, or experiment work in a dedicated worktree when the user requests isolation, the live checkout is dirty, or a multi-step change must not leak into live gates. Do not use for read-only inspection or when an existing named worktree is already the required workspace.
---

# Hermes Worktree Startup

1. Read the repository `AGENTS.md`, `/home/piet/vault/00-Canon/vision.md`, and invoke the global `codex-vault-coordination` skill.
2. Inspect `git status --short --branch` and `git worktree list --porcelain` before choosing a path or branch.
3. Treat a broad claim overlap as a coordination signal. Stop only for concrete same-file work, an inseparable subsystem, or an ambiguous base branch; disjoint files under similar paths may proceed with the separation recorded. Never build from the detached live checkout left by an auto-release rollback.
4. Use the canonical host worktree root `/home/piet/.hermes/worktrees/` with a unique `codex-<slug>` directory and a `codex/<slug>` branch unless the active task names a different managed worktree.
5. Before `git worktree add`, create the coordination claim for the planned worktree path, branch, and intended file scope. Worktree creation is itself a concrete write.
6. Create and verify the worktree:

   ```bash
   git worktree add -b codex/<slug> /home/piet/.hermes/worktrees/codex-<slug> <base>
   git -C /home/piet/.hermes/worktrees/codex-<slug> status --short --branch
   git -C /home/piet/.hermes/worktrees/codex-<slug> rev-parse --show-toplevel
   ```

7. Re-run the overlap checker when intended paths expand or a new parallel session appears in the same file area, refining the claim when scope changes. Do not repeat it mechanically inside an unchanged exact claim, and do not block on merely similar paths when the concrete files are disjoint. Perform all later work and gates in that checkout.
8. Do not copy foreign dirty files, reuse another agent's worktree, push, or remove a worktree unless the active request authorizes it.
