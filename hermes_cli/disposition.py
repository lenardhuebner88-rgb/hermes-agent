"""Disposition schema, parser, and validator for Kanban task-completion metadata.

When a worker completes a task it may record open items (risks, follow-ups,
still-open threads) under a well-known key in the completion metadata:

    metadata["disposition"]["items"]   (list of DispositionItem dicts)

This module provides:

* :class:`DispositionItem` — typed, validated representation of a single item.
* :class:`DispositionResult` — parsed result carrying the items list.
* :func:`parse_disposition` — TOLERANT parser; never raises on alt/old metadata.
* :func:`validate_disposition` — STRICT validator; returns (ok, missing) pairs.

Intentionally standalone: zero imports from other hermes_cli modules, no
wiring into the completion path (that is a later slice).

Schema detail
-------------
Each item in ``metadata["disposition"]["items"]`` must carry:

  typ          : {"risk", "follow_up", "still_open"}
  disposition  : {"done", "delegate", "defer", "drop"}
  next_action  : str — concrete next step (REQUIRED when disposition ∈ {"delegate","defer"})
  severity     : {"real-risk", "scope-note", "none"} — only meaningful for typ="risk";
                 defaults to "none" for other types
  evidence     : str — provenance (file:line / commit / tool_call_id / task_id)
                 Recommended, not hard-required by parse; missing is noted in validate.

LLM-refusal / truncation guard
-------------------------------
If the disposition block carries ``__llm_refusal__: true`` or ``__truncated__: true``,
:func:`validate_disposition` treats the block as semantically invalid — even when
items[] happens to be an empty list — because the LLM signalled it could not or did
not actually complete the disposition assessment.  :func:`parse_disposition` is
tolerant and will still return a (possibly empty) result without raising.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Enum constants (plain frozensets — no stdlib Enum dependency overhead)
# ---------------------------------------------------------------------------

VALID_TYP: frozenset[str] = frozenset({"risk", "follow_up", "still_open"})
VALID_DISPOSITION: frozenset[str] = frozenset({"done", "delegate", "defer", "drop"})
VALID_SEVERITY: frozenset[str] = frozenset({"real-risk", "scope-note", "none"})

#: Valid lifecycle status values for a ``disposition_items`` ledger row.
#: Terminal status values (accepted, task_created, dismissed, superseded) trigger
#: ``decided_at`` to be recorded; ``open`` is non-terminal.
VALID_LEDGER_STATUS: frozenset[str] = frozenset(
    {"open", "accepted", "task_created", "dismissed", "superseded"}
)

#: Status values that record a ``decided_at`` timestamp (all except ``open``).
_TERMINAL_LEDGER_STATUS: frozenset[str] = VALID_LEDGER_STATUS - {"open"}

#: Dispositions that require an explicit next_action (otherwise it is optional).
_NEXT_ACTION_REQUIRED: frozenset[str] = frozenset({"delegate", "defer"})

#: Metadata key under which the disposition block lives.
DISPOSITION_KEY = "disposition"

#: Marker keys that indicate the LLM could not complete the assessment.
_REFUSAL_KEYS: frozenset[str] = frozenset({"__llm_refusal__", "__truncated__"})

#: Mandatory fields a *versioned* completion bundle must carry. These are the
#: fields a reviewer needs to land/judge a worker's deliverable: a schema marker,
#: the gate result, acceptance-criteria coverage, and the residual-risk line.
#: ``gates`` must be a dict carrying an int ``exit_code``; the rest are non-empty.
#: Ordering matters for :func:`render_completion_metadata` — the scalar fields
#: come first (cheap to keep) so they survive truncation even when ``gates`` is
#: large.
MANDATORY_BUNDLE_KEYS: tuple[str, ...] = (
    "schema_version",
    "residual_risk",
    "AC",
    "gates",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DispositionItem:
    """A single captured disposition item from a task-completion run."""

    typ: str
    disposition: str
    next_action: str
    severity: str
    evidence: str


@dataclass
class DispositionResult:
    """Parsed output of :func:`parse_disposition`.

    ``items`` is an empty list when the metadata carries no disposition block
    (backward-compat for old done-tasks) or when all items were invalid and
    were skipped by the tolerant parser.
    """

    items: list[DispositionItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_parse_item(raw: Any, index: int) -> DispositionItem | None:  # noqa: ANN401
    """Attempt to parse one raw dict into a :class:`DispositionItem`.

    Returns ``None`` when the item is fatally malformed (wrong typ/disposition
    enum values or not a dict at all) so the tolerant parser can skip it.
    ``next_action`` and ``evidence`` are coerced to empty strings when absent —
    the *validator* enforces their presence where required; the parser is tolerant.
    """
    if not isinstance(raw, dict):
        return None

    typ = raw.get("typ")
    if typ not in VALID_TYP:
        return None

    disp = raw.get("disposition")
    if disp not in VALID_DISPOSITION:
        return None

    severity = raw.get("severity", "none")
    if severity not in VALID_SEVERITY:
        # Gracefully fall back rather than skip the whole item.
        severity = "none"

    next_action = str(raw.get("next_action", ""))
    evidence = str(raw.get("evidence", ""))

    return DispositionItem(
        typ=typ,
        disposition=disp,
        next_action=next_action,
        severity=severity,
        evidence=evidence,
    )


def _extract_block(metadata: Any) -> dict | None:  # noqa: ANN401
    """Extract the raw disposition block from metadata, or ``None`` if absent/malformed."""
    if not isinstance(metadata, dict):
        return None
    block = metadata.get(DISPOSITION_KEY)
    if not isinstance(block, dict):
        return None
    return block


def _has_refusal_marker(block: dict) -> bool:
    """Return True if any LLM-refusal or truncation marker is set in the block."""
    return any(block.get(key) for key in _REFUSAL_KEYS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_disposition(metadata: Any) -> DispositionResult:  # noqa: ANN401
    """Tolerantly parse ``metadata["disposition"]["items"]`` into a :class:`DispositionResult`.

    Contract:
    - Never raises, regardless of what ``metadata`` contains.
    - Returns an empty :class:`DispositionResult` (``items=[]``) when:
      * ``metadata`` is ``None`` or not a ``dict``,
      * the ``"disposition"`` key is absent,
      * the disposition value is not a ``dict``,
      * ``items`` is not a ``list``.
    - Invalid individual items are skipped; valid ones are kept.
    - Unknown extra keys (in the block or in items) are silently ignored.
    - LLM-refusal / truncation markers do NOT cause an exception; they may
      result in an empty or partial item list.
    """
    block = _extract_block(metadata)
    if block is None:
        return DispositionResult()

    raw_items = block.get("items")
    if not isinstance(raw_items, list):
        return DispositionResult()

    parsed: list[DispositionItem] = []
    for idx, raw in enumerate(raw_items):
        item = _try_parse_item(raw, idx)
        if item is not None:
            parsed.append(item)

    return DispositionResult(items=parsed)


def validate_disposition(metadata: Any) -> tuple[bool, list[str]]:  # noqa: ANN401
    """Strictly validate ``metadata["disposition"]`` and return ``(ok, missing)``.

    Rules (in order):

    1. ``metadata`` must be a ``dict`` and must contain ``"disposition"`` — if
       not, returns ``(False, ["disposition"])`` immediately.
    2. The disposition block must be a ``dict``; otherwise ``(False, ["disposition"])``
       (indistinguishable from absence — both mean "no valid block").
    3. LLM-refusal / truncation markers in the block → ``(False, [<marker description>])``.
       An empty ``items`` list with no such markers IS valid (§4 below).
    4. ``items`` must be a ``list`` (possibly empty). An empty list means "no
       follow-ups or risks captured" which is a legitimate outcome → ``ok=True``.
    5. Per item (0-indexed):
       - ``typ`` must be present and a valid enum value.
       - ``disposition`` must be present and a valid enum value.
       - ``next_action`` must be a non-empty string when
         ``disposition ∈ {"delegate", "defer"}``.
       - Severity errors are NOT hard failures (parse already defaults to "none").
       - ``evidence`` absent is noted as a warning-style entry in ``missing``
         but does NOT set ``ok=False`` on its own (recommended, not required).

    ``missing`` entries follow the convention ``"[<idx>].field"`` for per-item
    failures so callers can pinpoint which items need correction.
    """
    missing: list[str] = []

    # --- Step 1 & 2: block presence ------------------------------------------
    if not isinstance(metadata, dict) or DISPOSITION_KEY not in metadata:
        return False, [DISPOSITION_KEY]

    block = metadata[DISPOSITION_KEY]
    if not isinstance(block, dict):
        return False, [DISPOSITION_KEY]

    # --- Step 3: LLM-refusal / truncation markers ----------------------------
    for marker_key in _REFUSAL_KEYS:
        if block.get(marker_key):
            label = "llm_refusal" if "refusal" in marker_key else "truncated"
            return False, [f"disposition:{label}_marker_set"]

    # --- Step 4: items must be a list ----------------------------------------
    raw_items = block.get("items")
    if not isinstance(raw_items, list):
        return False, ["disposition.items: must be a list"]

    # Empty list is explicitly valid — nothing to check further.
    if not raw_items:
        return True, []

    # --- Step 5: per-item validation -----------------------------------------
    ok = True
    for idx, raw in enumerate(raw_items):
        prefix = f"[{idx}]"

        if not isinstance(raw, dict):
            missing.append(f"{prefix}: item must be a dict")
            ok = False
            continue

        # typ
        typ = raw.get("typ")
        if not typ or typ not in VALID_TYP:
            missing.append(f"{prefix}.typ: must be one of {sorted(VALID_TYP)!r}, got {typ!r}")
            ok = False

        # disposition
        disp = raw.get("disposition")
        if not disp or disp not in VALID_DISPOSITION:
            missing.append(
                f"{prefix}.disposition: must be one of {sorted(VALID_DISPOSITION)!r}, got {disp!r}"
            )
            ok = False

        # next_action (required when disposition ∈ delegate/defer)
        if disp in _NEXT_ACTION_REQUIRED:
            next_action = raw.get("next_action", "")
            if not next_action or not str(next_action).strip():
                missing.append(
                    f"{prefix}.next_action: required when disposition={disp!r}"
                )
                ok = False

        # evidence — recommended; emit a soft note but don't fail
        if not raw.get("evidence"):
            missing.append(f"{prefix}.evidence: recommended (provenance not recorded)")
            # Does NOT set ok = False

    return ok, missing


# ---------------------------------------------------------------------------
# Completion-bundle validation + render guarantee (PlanSpec C landing)
#
# A worker's completion ``metadata`` may carry a *versioned bundle*: the small
# set of fields a reviewer needs to land/judge the deliverable. When a worker
# opts in by emitting ``schema_version`` the bundle MUST be complete, so the
# kanban_complete entry points can reject an incomplete submission IN-FLIGHT —
# surfacing the missing fields back to the worker for a retry instead of letting
# the half-filled completion land and a downstream gate auto-block the task.
# ---------------------------------------------------------------------------


def _is_intish(value: Any) -> bool:  # noqa: ANN401
    """True for an int (NOT bool) or a base-10 integer string like ``"0"``."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        try:
            int(value.strip())
            return True
        except ValueError:
            return False
    return False


def _nonempty(value: Any) -> bool:  # noqa: ANN401
    """True when ``value`` carries content (non-blank str / non-empty container).

    ``residual_risk="none"`` is a legitimate non-empty value (it asserts "no
    residual risk"), so this checks for *presence of content*, not truthiness —
    an explicit ``0`` or ``False`` also counts as content.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def validate_completion_bundle(metadata: Any) -> list[str]:  # noqa: ANN401
    """Return the list of missing/invalid mandatory completion-bundle fields.

    OPT-IN: a completion is only treated as a *versioned bundle* when its
    metadata is a ``dict`` carrying a ``schema_version`` key. Legacy/unversioned
    completions (no ``schema_version``) are exempt and always return ``[]`` so
    existing worker flows stay byte-identical — the guardrail activates only for
    workers that declare they are emitting the structured bundle.

    Mandatory fields (only checked when opted in):
      * ``schema_version``  — integer ``>= 1`` (the bundle marker itself)
      * ``gates.exit_code`` — ``gates`` is a dict carrying an int ``exit_code``
      * ``AC``              — non-empty (acceptance-criteria coverage)
      * ``residual_risk``   — non-empty (one-line residual-risk statement)

    Returns ``[]`` when the bundle is complete OR not opted in. The returned
    labels are human-readable so the kanban_complete tool can echo them straight
    back to the worker (``"add these and call kanban_complete again"``).
    """
    if not isinstance(metadata, dict):
        return []
    if "schema_version" not in metadata:
        return []  # not a versioned bundle — exempt (legacy / back-compat)

    missing: list[str] = []

    version = metadata.get("schema_version")
    if not _is_intish(version) or int(str(version).strip()) < 1:
        missing.append("schema_version (int >= 1)")

    gates = metadata.get("gates")
    if not isinstance(gates, dict):
        missing.append("gates.exit_code (object with an int exit_code)")
    elif "exit_code" not in gates or not _is_intish(gates.get("exit_code")):
        missing.append("gates.exit_code (int)")

    if not _nonempty(metadata.get("AC")):
        missing.append("AC (non-empty acceptance-criteria coverage)")

    if not _nonempty(metadata.get("residual_risk")):
        missing.append("residual_risk (non-empty one-line statement)")

    return missing


def _cap_text(s: str, limit: int) -> str:
    """Truncate ``s`` to ``limit`` chars with the same visible ellipsis the
    worker-context renderer (:func:`hermes_cli.kanban_db._cap`) uses, so output
    framing stays identical whichever path renders the field."""
    if len(s) <= limit:
        return s
    return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"


def render_completion_metadata(metadata: Any, limit: int) -> str:  # noqa: ANN401
    """Serialize completion ``metadata`` to a context string capped at ``limit``
    chars, GUARANTEEING the mandatory bundle fields survive truncation.

    Behaviour:

    * Non-dict metadata → ``json.dumps`` capped like any plain field.
    * Dict that fits within ``limit`` → byte-identical to the prior
      ``json.dumps(..., sort_keys=True)`` output (no change for the common case).
    * Dict that exceeds ``limit`` → the mandatory bundle fields
      (:data:`MANDATORY_BUNDLE_KEYS`) are re-ordered to the FRONT so the cut
      never drops them; only the trailing non-essential fields are truncated.
      If the mandatory fields ALONE exceed ``limit`` they are still rendered in
      full (the guarantee wins over the cap — a required field is never dropped).

    This closes the 4 KB-truncation hole: a large ``disposition`` / ``decisions``
    / ``changed_files`` array can no longer push ``schema_version`` /
    ``residual_risk`` / ``AC`` / ``gates.exit_code`` past the per-field cap, which
    previously fell off the tail because ``sort_keys`` buries them after the
    lowercase d-keys.
    """
    if not isinstance(metadata, dict):
        return _cap_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True), limit)

    full = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    if len(full) <= limit:
        return full

    mandatory = {k: metadata[k] for k in MANDATORY_BUNDLE_KEYS if k in metadata}
    if not mandatory:
        # Nothing to protect — preserve the prior truncation behaviour exactly.
        return _cap_text(full, limit)

    rest = {k: metadata[k] for k in sorted(metadata) if k not in mandatory}
    ordered = json.dumps({**mandatory, **rest}, ensure_ascii=False)
    mand_only = json.dumps(mandatory, ensure_ascii=False)
    if len(mand_only) >= limit:
        # Mandatory fields alone blow the cap — keep them whole (never drop a
        # required field) and shed the non-essential rest entirely.
        return mand_only
    return _cap_text(ordered, limit)
