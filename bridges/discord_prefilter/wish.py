"""Deterministic ``idee:`` path — Piet's wishes go straight to Kanban triage.

A message starting with ``idee:`` is a demand-funnel proposal, not a task to
classify: no ``claude -p`` spawn, no forward to #hermes-oc. The bridge creates
a triage card via the Hermes CLI (``python -m hermes_cli.main kanban create``)
in a subprocess — same interpreter the bridge already runs on, no extra auth.

Dedupe lives in the Kanban layer (``--idempotency-key wish:<normalized>``):
the same wish twice returns the existing card instead of a duplicate.

Pure-logic + thin subprocess wrapper, kept free of discord.py so it can be
unit-tested in isolation (see tests/discord_prefilter/test_wish.py).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from bridges.discord_prefilter.config import PrefilterConfig

logger = logging.getLogger("discord_prefilter.wish")

CREATED_BY = "discord-idee"

_PREFIX = re.compile(r"^\s*idee\s*:\s*", re.IGNORECASE)

# Keep titles scannable on the board; the full wish always lands in the body.
_TITLE_LIMIT = 80
# Idempotency keys stay short and stable: lowercase, collapsed whitespace.
_KEY_LIMIT = 120

_CREATE_TIMEOUT_S = 30

BODY_TEMPLATE = (
    "Vorschlag aus Discord (Quelle: Vorfilter-Kanal, Autor: {author}).\n\n"
    "Originalnachricht:\n{wish}\n\n"
    "Standing-Anweisung: Das ist ein Funnel-Vorschlag — bei Annahme zuerst "
    "ausarbeiten/Spec draften, NICHT ungefragt bauen."
)


def extract_wish(content: str) -> Optional[str]:
    """Return the wish text if ``content`` starts with ``idee:``, else None.

    An empty wish (bare ``idee:``) returns None — nothing to file.
    """
    match = _PREFIX.match(content or "")
    if not match:
        return None
    wish = content[match.end():].strip()
    return wish or None


def normalize_wish(wish: str) -> str:
    """Stable dedupe key part: lowercase, collapsed whitespace, length-capped."""
    return re.sub(r"\s+", " ", wish).strip().lower()[:_KEY_LIMIT]


def wish_title(wish: str) -> str:
    first_line = wish.strip().splitlines()[0].strip()
    if len(first_line) > _TITLE_LIMIT:
        first_line = first_line[: _TITLE_LIMIT - 1].rstrip() + "…"
    return first_line


def build_create_argv(wish: str, author: str, config: "PrefilterConfig") -> List[str]:
    """argv for ``hermes kanban create`` — reuses the bridge's hermes_argv."""
    return list(config.hermes_argv) + [
        "kanban",
        "create",
        wish_title(wish),
        "--triage",
        "--created-by",
        CREATED_BY,
        "--idempotency-key",
        f"wish:{normalize_wish(wish)}",
        "--body",
        BODY_TEMPLATE.format(author=author or "?", wish=wish.strip()),
        "--json",
    ]


def run_wish_create(wish: str, author: str, config: "PrefilterConfig") -> Tuple[bool, str]:
    """Create the triage card. Returns (ok, task_id-or-error). Never raises."""
    cmd = build_create_argv(wish, author, config)
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv built above
            cmd,
            capture_output=True,
            text=True,
            timeout=_CREATE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.error("hermes interpreter not found (%s)", cmd[0])
        return False, f"Interpreter nicht gefunden: {cmd[0]}"
    except subprocess.TimeoutExpired:
        logger.warning("kanban create timed out after %ss", _CREATE_TIMEOUT_S)
        return False, f"Timeout nach {_CREATE_TIMEOUT_S}s"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        logger.warning("kanban create exited %s: %s", proc.returncode, err)
        return False, err or f"exit {proc.returncode}"

    task_id = _parse_task_id(proc.stdout or "")
    if not task_id:
        logger.warning("kanban create returned unparseable output: %r",
                       (proc.stdout or "")[:300])
        return False, "Antwort der Kanban-CLI nicht lesbar"
    return True, task_id


def _parse_task_id(stdout: str) -> Optional[str]:
    try:
        obj = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict):
        task_id = str(obj.get("id") or "").strip()
        return task_id or None
    return None
