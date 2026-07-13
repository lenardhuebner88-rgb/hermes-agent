---
name: hermes-ui-preview
description: Use for previewing, visually verifying, or reviewing Hermes dashboard/control UI changes across authenticated live, isolated preview, mobile, tablet, or desktop surfaces. Routes to the canonical Hermes UI preview workflow and its task-specific evidence references.
---

# Hermes UI Preview Router

1. Read `/home/piet/.hermes/skills/hermes-ui-preview/SKILL.md` completely before starting a Hermes UI preview or visual verdict.
2. Resolve its references relative to `/home/piet/.hermes/skills/hermes-ui-preview/` and load only the references matching the affected surface or failure mode.
3. Distinguish deployed/live verification from branch/worktree preview. Never use the deployed dashboard as proof for an unmerged change.
4. Preserve authentication and secret-handling rules; use the canonical runners instead of exposing credentials.
5. Report the exact viewport, command, artifact path, and any verification gap.

This router stays small so Curator-owned workflow knowledge remains canonical and updateable.
