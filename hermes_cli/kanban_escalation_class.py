"""Fork-owned write-time classification for operator escalations.

The deterministic Heiler classifier already supplies the canonical vocabulary.
This module freezes that result into each newly-written escalation event while
leaving historical append-only rows untouched.
"""

from __future__ import annotations

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
