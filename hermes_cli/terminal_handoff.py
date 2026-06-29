"""Terminal-selection → PlanSpec/Kanban handoff helpers (ATH-S5).

The ``/control`` Agent-Terminals view lets an operator hand selected terminal
text (or the last N captured lines) off into a PlanSpec draft or a Kanban triage
task. This module owns the small piece the dashboard cannot do in the browser:
**materialising** an operator-authored draft into a ``.md`` file under the
PlanSpec plans root, so the EXISTING validate / ingest pipeline
(:func:`hermes_cli.planspecs.validate_planspec` and
:func:`hermes_cli.planspecs.ingest_planspec`) can operate on it by path — exactly
like a hand-authored PlanSpec.

Design contract (mirrors the ATH-S5 acceptance criteria):

* **No DB write logic here.** Persistence into Kanban is delegated entirely to
  the existing PlanSpec ingest path / the existing task-creation endpoint.
* **Nothing dispatches.** Writing a draft, validating it, and ingesting it are
  distinct, separately-triggered steps; live dispatch stays an operator action
  on the board. This module never spawns or claims work.
* Drafts live in a dedicated subdir so they are easy to find / sweep and never
  collide with hand-authored PlanSpecs.
"""
from __future__ import annotations

import re
from pathlib import Path

from hermes_cli import planspecs

# Handoff drafts land under the Hermes agent's plans dir in a dedicated subdir.
# Keeping them segregated means a stray, never-finished draft is obvious and can
# be swept without touching curated PlanSpecs.
HANDOFF_SUBDIR = Path("Hermes") / "plans" / "terminal-handoff"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 60


def slugify(text: str, *, fallback: str = "draft") -> str:
    """Lowercase ``[a-z0-9-]`` slug for a draft filename; never empty."""
    slug = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    slug = slug[:_SLUG_MAX].strip("-")
    return slug or fallback


def _plans_root(plans_root: Path | str | None) -> Path:
    # Read the module attribute at call time (not as a def-time default) so tests
    # can redirect the whole pipeline at a tmp dir via monkeypatch.
    if plans_root is not None:
        return Path(plans_root)
    return planspecs.DEFAULT_PLANS_ROOT


def handoff_draft_path(slug: str, *, plans_root: Path | str | None = None) -> Path:
    """Absolute path a draft with ``slug`` would be written to (no I/O)."""
    return _plans_root(plans_root) / HANDOFF_SUBDIR / f"{slugify(slug)}.md"


def write_handoff_draft(
    content: str, *, slug: str, plans_root: Path | str | None = None
) -> Path:
    """Persist ``content`` as a handoff draft ``.md`` under the plans root.

    Returns the written path. Overwrites an existing draft with the same slug
    (idempotent per slug) so re-validating an edited draft does not litter the
    plans dir with one file per keystroke. Creates the handoff subdir on demand.
    """
    path = handoff_draft_path(slug, plans_root=plans_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
