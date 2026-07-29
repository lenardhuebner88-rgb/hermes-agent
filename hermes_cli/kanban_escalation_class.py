"""Fork-owned write-time classification for operator escalations.

The deterministic Heiler classifier already supplies the canonical vocabulary.
This module freezes that result into each newly-written escalation event while
leaving historical append-only rows untouched.
"""

from __future__ import annotations

import json
import sqlite3
from enum import Enum
from typing import Callable, Optional


class EscalationClass(str, Enum):
    TRANSIENT = "transient"
    FLAKY = "flaky"
    REAL_BUG = "real-bug"
    BAD_SPEC = "bad-spec"
    CONFLICT = "conflict"
    UNCLASSIFIED = "unclassified"
    CAPACITY = "capacity"
    PROTOCOL_NONCOMPLIANCE = "protocol-noncompliance"
    OPERATOR_INTENT = "operator-intent"
    OPERATOR_GATED = "operator-gated"


ESCALATION_CLASSES = frozenset(item.value for item in EscalationClass)
OPERATOR_ESCALATION_EVENT = "operator_escalation"
_OPERATOR_HOLD_CLASSES = frozenset(
    {
        EscalationClass.OPERATOR_INTENT.value,
        EscalationClass.OPERATOR_GATED.value,
    }
)
_BASE_PREP_PREFIX = "worker base preparation:"
_BASE_PREP_REBASE_MARKER = "could not rebase onto"
_ESCALATION_RESOLUTION_EVENTS = ("unblocked", "promoted_manual")


def persisted_escalation_class(payload: object) -> Optional[str]:
    """Return a valid frozen class, ignoring missing/unknown legacy values."""
    if not isinstance(payload, dict):
        return None
    value = str(payload.get("escalation_class") or "").strip().lower()
    return value if value in ESCALATION_CLASSES else None


def persisted_escalation_classification(
    payload: object,
    *,
    classifier: Optional[Callable[[dict], tuple[str, dict]]] = None,
) -> Optional[tuple[str, dict]]:
    """Return the classifier-shaped result for a frozen event class."""
    value = persisted_escalation_class(payload)
    if value is None:
        return None
    if classifier is not None and isinstance(payload, dict):
        unstamped = dict(payload)
        unstamped.pop("escalation_class", None)
        try:
            _derived_class, evidence = classifier(unstamped)
        except Exception:
            evidence = {
                "matched": value,
                "signal_source": "persisted_escalation_class",
            }
        return value, evidence
    return value, {
        "matched": value,
        "signal_source": "persisted_escalation_class",
    }


def materialize_event_payload(
    kind: str,
    payload: object,
    *,
    classifier: Callable[[dict], tuple[str, dict]],
) -> object:
    """Stamp a valid class on a new operator-escalation payload."""
    if kind != OPERATOR_ESCALATION_EVENT or not isinstance(payload, dict):
        return payload
    stamped = dict(payload)
    persisted = persisted_escalation_class(stamped)
    if persisted is None:
        try:
            classified, _evidence = classifier(stamped)
        except Exception:
            classified = EscalationClass.UNCLASSIFIED.value
        stamped["escalation_class"] = (
            classified
            if classified in ESCALATION_CLASSES
            else EscalationClass.UNCLASSIFIED.value
        )
    return stamped


def base_prep_retry_allowed(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    reason: str,
    conflict_fingerprint: str,
) -> bool:
    """Allow a failed base-prep fixer to spend its remaining bounded budget.

    A system escalation reports the park but is not an operator hold. The
    persisted class is the authoritative distinction: explicit operator
    classes block, valid system classes do not, and unstamped legacy payloads
    fail closed. The matching failure-event requirement keeps the initial
    in-flight fixer on the existing notification path; only a settled failed
    attempt can unlock the next sweep-routed attempt.
    """
    if not (
        str(reason or "").startswith(_BASE_PREP_PREFIX)
        and _BASE_PREP_REBASE_MARKER in str(reason or "")
        and conflict_fingerprint
    ):
        return False

    failed_rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'conflict_fixer_failed' "
        "ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    matching_failure = False
    for failed_row in failed_rows:
        try:
            payload = json.loads(failed_row["payload"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and str(payload.get("conflict_fingerprint") or "")
            == conflict_fingerprint
        ):
            matching_failure = True
            break
    if not matching_failure:
        return False

    row = conn.execute(
        "SELECT kind, payload FROM task_events "
        "WHERE task_id = ? AND ("
        "  (kind = ? AND COALESCE(json_extract(payload, "
        "   '$.evidence.trigger_outcome'), '') != 'nonspawnable_assignee')"
        "  OR kind IN ('unblocked', 'promoted_manual')) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, OPERATOR_ESCALATION_EVENT),
    ).fetchone()
    if row is None or row["kind"] in _ESCALATION_RESOLUTION_EVENTS:
        return True
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    escalation_class = persisted_escalation_class(payload)
    return (
        escalation_class is not None
        and escalation_class not in _OPERATOR_HOLD_CLASSES
    )
