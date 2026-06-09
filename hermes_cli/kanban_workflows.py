"""Workflow-template loader for native multi-step kanban routing (D7 L2 / K8).

A *workflow template* declares an ordered list of steps, each naming the
assignee (Hermes profile / role) that should run that step — e.g. a native
``coder -> reviewer -> critic`` chain. A task opted into a template (its
``tasks.workflow_template_id`` column is set) is routed step by step:

  * the dispatcher resolves the assignee from the task's *current* step
    (``tasks.current_step_key``) instead of the static ``assignee`` column;
  * on completion the kernel advances ``current_step_key`` to the next step
    and parks the task back in ``ready`` (re-assigned) instead of moving it
    straight to ``done`` — until the final step, which completes normally.

Templates live as YAML at ``<root>/kanban-workflows/<id>.yaml`` (``<root>``
being the Hermes umbrella root that anchors the kanban board; an explicit
``HERMES_KANBAN_WORKFLOWS_DIR`` env var overrides the directory for tests
and unusual deployments)::

    # ~/.hermes/kanban-workflows/code-review-critic.yaml
    steps:
      - key: code
        assignee: coder
      - key: review
        assignee: reviewer
      - key: critique
        assignee: critic

Everything here is **fail-soft by contract**: a missing file, unreadable
YAML, a non-mapping document, or a malformed/duplicate-keyed ``steps`` list
all resolve to ``None`` (= "no template") and NEVER raise. A broken template
therefore degrades a workflow task to today's single-role behaviour rather
than crashing the dispatcher tick or the completion path. Callers in
``kanban_db`` treat a ``None`` return as "this task has no usable workflow"
and fall through to the unchanged legacy routing.

Results are cached per (directory, template_id) so a hot dispatch loop does
not re-read+parse the same YAML every tick. ``clear_workflow_cache()`` drops
the cache (used by tests that rewrite template files between assertions).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_log = logging.getLogger("hermes.kanban.workflows")

# Cache keyed by (resolved_dir_str, template_id) -> Optional[WorkflowTemplate].
# A ``None`` value is cached too (negative cache) so a missing/broken template
# isn't re-read every dispatch tick. Cleared by ``clear_workflow_cache()``.
_CACHE: dict[tuple[str, str], Optional["WorkflowTemplate"]] = {}


@dataclass(frozen=True)
class WorkflowStep:
    """A single ordered step in a workflow template."""

    key: str
    assignee: str


@dataclass(frozen=True)
class WorkflowTemplate:
    """An ordered, validated workflow template.

    Invariants (guaranteed by :func:`load_workflow_template`, the only
    constructor callers should use): ``steps`` is non-empty and every step
    has a non-empty ``key`` and ``assignee``; step keys are unique.
    """

    id: str
    steps: tuple[WorkflowStep, ...]

    def step(self, step_key: str) -> Optional[WorkflowStep]:
        """Return the step with ``step_key`` (or ``None`` if absent)."""
        for s in self.steps:
            if s.key == step_key:
                return s
        return None

    def assignee_for(self, step_key: str) -> Optional[str]:
        """Return the assignee for ``step_key`` (or ``None`` if unknown)."""
        s = self.step(step_key)
        return s.assignee if s is not None else None

    def first_step_key(self) -> Optional[str]:
        """Return the first step's key (or ``None`` for an empty template)."""
        return self.steps[0].key if self.steps else None

    def next_step_key(self, step_key: str) -> Optional[str]:
        """Return the key of the step *after* ``step_key``.

        ``None`` when ``step_key`` is the final step OR is not part of this
        template (an unknown current step is treated as "no next step" so a
        stale/renamed key completes the task rather than stranding it).
        """
        for i, s in enumerate(self.steps):
            if s.key == step_key:
                nxt = i + 1
                return self.steps[nxt].key if nxt < len(self.steps) else None
        return None


def workflows_dir() -> Path:
    """Return the directory holding ``<id>.yaml`` workflow templates.

    Resolution: ``HERMES_KANBAN_WORKFLOWS_DIR`` when set and non-empty, else
    ``<kanban root>/kanban-workflows``. The kanban root is resolved through
    :func:`hermes_cli.kanban_db.kanban_home` so workflows live alongside the
    shared board, not under a per-profile ``HERMES_HOME``.
    """
    override = os.environ.get("HERMES_KANBAN_WORKFLOWS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    # Local import to avoid a module-load cycle (kanban_db imports this
    # module lazily inside its dispatch/complete hot paths).
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / "kanban-workflows"


def clear_workflow_cache() -> None:
    """Drop the in-process template cache. Primarily for tests."""
    _CACHE.clear()


def _parse_template(template_id: str, raw: object) -> Optional[WorkflowTemplate]:
    """Validate a parsed YAML document into a :class:`WorkflowTemplate`.

    Returns ``None`` (never raises) on any structural problem: not a mapping,
    missing/empty ``steps``, a step that isn't a mapping, a missing/blank
    ``key`` or ``assignee``, or a duplicate step key.
    """
    if not isinstance(raw, dict):
        return None
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, (list, tuple)) or not steps_raw:
        return None
    steps: list[WorkflowStep] = []
    seen: set[str] = set()
    for entry in steps_raw:
        if not isinstance(entry, dict):
            return None
        key = entry.get("key")
        assignee = entry.get("assignee")
        if not isinstance(key, str) or not key.strip():
            return None
        if not isinstance(assignee, str) or not assignee.strip():
            return None
        key = key.strip()
        assignee = assignee.strip()
        if key in seen:
            # Duplicate step keys make next_step_key ambiguous — reject the
            # whole template rather than route unpredictably.
            return None
        seen.add(key)
        steps.append(WorkflowStep(key=key, assignee=assignee))
    return WorkflowTemplate(id=template_id, steps=tuple(steps))


def load_workflow_template(template_id: Optional[str]) -> Optional[WorkflowTemplate]:
    """Load and validate the template named ``template_id``.

    Returns the :class:`WorkflowTemplate`, or ``None`` when the id is empty,
    the file is missing/unreadable, the YAML is malformed, or the template
    fails validation. Never raises — a broken template must degrade a task to
    legacy single-role routing, not crash the dispatcher.
    """
    if not template_id or not str(template_id).strip():
        return None
    template_id = str(template_id).strip()
    try:
        directory = workflows_dir()
    except Exception:
        _log.debug("kanban workflows: could not resolve workflows dir", exc_info=True)
        return None
    cache_key = (str(directory), template_id)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    tmpl: Optional[WorkflowTemplate] = None
    path = directory / f"{template_id}.yaml"
    try:
        if path.is_file():
            import yaml  # local import: optional dep, keep module import cheap

            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            tmpl = _parse_template(template_id, raw)
            if tmpl is None:
                _log.debug(
                    "kanban workflows: template %r at %s is malformed; ignoring",
                    template_id, path,
                )
        else:
            _log.debug(
                "kanban workflows: no template file for %r at %s", template_id, path
            )
    except Exception:
        # Unreadable file, YAML error, encoding problem — all degrade to
        # "no template" so the task keeps its legacy routing.
        _log.debug(
            "kanban workflows: failed to load template %r from %s",
            template_id, path, exc_info=True,
        )
        tmpl = None

    _CACHE[cache_key] = tmpl
    return tmpl
