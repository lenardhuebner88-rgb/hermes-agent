"""Signals explicit negative review metadata that lacks terminal authority.

This fork-owned module keeps the predicate, event payload, and diagnostic
projection out of the upstream-owned kanban database and diagnostics bodies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND = (
    "negative_review_verdict_without_terminal_authority"
)
NEGATIVE_VERDICT_WITHOUT_AUTHORITY_DIAGNOSTIC_KIND = (
    "negative_review_verdict_without_terminal_authority"
)
REVIEW_AUTHORITY_REASON_NOT_REVIEW_CLAIM = "run_not_claimed_from_review"

_EXPLICIT_REVIEW_VERDICT_KEYS = ("review_verdict", "reviewer_verdict")
_REQUEST_CHANGES = "REQUEST_CHANGES"
_COMPLETED_OUTCOME = "completed"


def _explicit_review_verdict(metadata: object) -> tuple[bool, object]:
    if not isinstance(metadata, Mapping):
        return False, None
    for key in _EXPLICIT_REVIEW_VERDICT_KEYS:
        if key in metadata:
            return True, metadata[key]
    return False, None


def _blocking_findings_count(metadata: Mapping[str, object]) -> int:
    findings = metadata.get("blocking_findings")
    if isinstance(findings, Sequence) and not isinstance(
        findings, (str, bytes, bytearray)
    ):
        return len(findings)
    return 0


def record_negative_verdict_without_authority(
    conn,
    *,
    task_id: str,
    run_id: int,
    outcome: str,
    metadata: object,
    originated_from_review: bool,
    normalize_verdict: Callable[[object], str | None],
    append_event: Callable[..., object],
) -> bool:
    """Append one finding event for a completed, non-review negative verdict."""

    has_explicit_verdict, raw_verdict = _explicit_review_verdict(metadata)
    if (
        outcome != _COMPLETED_OUTCOME
        or originated_from_review
        or not has_explicit_verdict
        or normalize_verdict(raw_verdict) != _REQUEST_CHANGES
    ):
        return False

    assert isinstance(metadata, Mapping)
    append_event(
        conn,
        task_id,
        NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND,
        {
            "normalized_verdict": _REQUEST_CHANGES,
            "raw_verdict": raw_verdict,
            "authority_reason": REVIEW_AUTHORITY_REASON_NOT_REVIEW_CLAIM,
            "blocking_findings_count": _blocking_findings_count(metadata),
        },
        run_id=run_id,
    )
    return True


def derive_negative_verdict_without_authority_diagnostics(
    task: object,
    events: Sequence[object],
    *,
    diagnostic_factory: Callable[..., object],
    field: Callable[[object, str, Any], Any],
    parse_payload: Callable[[object], Mapping[str, Any]],
) -> list[object]:
    """Project the newest triggering event into one warning diagnostic.

    A newer event of this kind and verdict supersedes the prior trigger. An
    archived card clears the projection entirely.
    """

    if field(task, "status", None) == "archived":
        return []

    candidates: list[object] = []
    for event in events:
        if field(event, "kind", None) != NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND:
            continue
        payload = parse_payload(event)
        if payload.get("normalized_verdict") == _REQUEST_CHANGES:
            candidates.append(event)
    if not candidates:
        return []

    trigger = max(
        candidates,
        key=lambda event: (
            int(field(event, "created_at", 0) or 0),
            int(field(event, "id", 0) or 0),
        ),
    )
    payload = parse_payload(trigger)
    task_id = str(field(task, "id", ""))
    verdict = str(payload.get("normalized_verdict"))
    finding_count = int(payload.get("blocking_findings_count") or 0)
    created_at = int(field(trigger, "created_at", 0) or 0)
    event_id = field(trigger, "id", None)
    run_id = field(trigger, "run_id", None)

    return [
        diagnostic_factory(
            kind=NEGATIVE_VERDICT_WITHOUT_AUTHORITY_DIAGNOSTIC_KIND,
            severity="warning",
            title="Negative review verdict had no terminal authority",
            detail=(
                f"Task {task_id} supplied {verdict} with {finding_count} "
                "blocking findings outside a review-stage claim."
            ),
            first_seen_at=created_at,
            last_seen_at=created_at,
            count=1,
            run_id=int(run_id) if run_id is not None else None,
            data={
                "task_id": task_id,
                "event_id": event_id,
                "verdict": verdict,
                "blocking_findings_count": finding_count,
            },
        )
    ]
