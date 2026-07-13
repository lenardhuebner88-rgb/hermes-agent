---
name: design-board
description: Use for Hermes Design Board cards, mockups, screenshots, pins, comments, promotion to Kanban, operator visual feedback, or broken dashboard mockup rendering. Routes Codex to the canonical Hermes-maintained Design Board workflow instead of duplicating it in this repository.
---

# Hermes Design Board Router

1. Read `/home/piet/.hermes/skills/design-board/SKILL.md` completely before any Design Board action.
2. Resolve every referenced file relative to `/home/piet/.hermes/skills/design-board/` and read the references required for the concrete task.
3. Stop if the canonical file is missing or contradicts the live code; do not reconstruct a stale workflow from memory.
4. Run commands from `/home/piet/.hermes/hermes-agent` unless the canonical workflow explicitly selects another checkout.
5. Keep operator-facing evidence and card lifecycle changes within the canonical Design Board contract.

This router owns discovery only. The Hermes-maintained source owns all operational details.
