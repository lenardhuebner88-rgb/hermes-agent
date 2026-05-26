"""Read-only Kanban run report contract helpers.

This module intentionally stays above the lifecycle gates: it only reads
Task/Run/Event materialized objects through kanban_db's public helpers and
does not import completion validators.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from hermes_cli import kanban_db as kb


REPORT_CONTRACT_VERSION = 1


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    try:
        if hasattr(obj, "keys") and name in obj.keys():
            return obj[name]
    except Exception:
        pass
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return bool(value)


def _metadata(run: Any) -> dict[str, Any]:
    meta = _field(run, "metadata")
    return dict(meta) if isinstance(meta, Mapping) else {}


def _first_present(metadata: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = metadata.get(key)
        if _non_empty(value):
            return value
    return None


def _verification_evidence(metadata: Mapping[str, Any]) -> Any:
    return _first_present(
        metadata,
        (
            "verification_evidence",
            "evidence_audited",
            "verified_evidence",
            "acceptance_checks",
            "tests_run",
        ),
    )


def _receipt_reference(metadata: Mapping[str, Any]) -> Any:
    return _first_present(
        metadata,
        (
            "receipt_reference",
            "receipt_path",
            "receipt_uri",
            "receipt",
            "receipt_id",
        ),
    )


def _scope_attestation(metadata: Mapping[str, Any]) -> dict[str, Any]:
    scope_attested = _truthy(metadata.get("scope_attestation"))
    version = _as_int(metadata.get("scope_contract_version"))
    forbidden = _as_int(metadata.get("forbidden_actions_taken"))
    inconsistencies: list[str] = []

    if scope_attested:
        if not _truthy(metadata.get("scope_contract_read")):
            inconsistencies.append("scope_contract_read must be true")
        if version is None or version < 2:
            inconsistencies.append("scope_contract_version must be >= 2")
        if forbidden is None:
            inconsistencies.append("forbidden_actions_taken must be 0")
        elif forbidden != 0:
            inconsistencies.append("forbidden_actions_taken must be 0")
    elif any(
        key in metadata
        for key in ("scope_contract_read", "scope_contract_version", "forbidden_actions_taken")
    ):
        inconsistencies.append("scope_attestation must be true when scope contract fields are present")

    return {
        "present": scope_attested,
        "scope_contract_read": _truthy(metadata.get("scope_contract_read")),
        "scope_contract_version": version,
        "forbidden_actions_taken": forbidden,
        "consistent": not inconsistencies,
        "inconsistencies": inconsistencies,
    }


def report_quality(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact quality summary for a normalized report."""
    contract = report.get("contract") if isinstance(report.get("contract"), Mapping) else {}
    scope = report.get("scope_attestation") if isinstance(report.get("scope_attestation"), Mapping) else {}
    missing: list[str] = []

    if contract.get("version") != REPORT_CONTRACT_VERSION:
        missing.append("report_contract_version")
    if not _non_empty(report.get("verification_evidence")):
        missing.append("verification_evidence")
    if not _non_empty(report.get("receipt_reference")):
        missing.append("receipt_reference")
    if not scope.get("present"):
        missing.append("scope_attestation")

    inconsistencies = list(scope.get("inconsistencies") or [])
    return {
        "ok": not missing and not inconsistencies,
        "missing": missing,
        "inconsistencies": inconsistencies,
    }


def normalize_run_report(task: kb.Task, run: kb.Run, events: Optional[list[kb.Event]] = None) -> dict[str, Any]:
    """Normalize one task run into the report contract view."""
    metadata = _metadata(run)
    version = _as_int(metadata.get("report_contract_version"))
    report: dict[str, Any] = {
        "task": {
            "id": _field(task, "id"),
            "title": _field(task, "title"),
            "status": _field(task, "status"),
            "assignee": _field(task, "assignee"),
        },
        "run": {
            "id": _field(run, "id"),
            "status": _field(run, "status"),
            "outcome": _field(run, "outcome"),
            "profile": _field(run, "profile"),
            "started_at": _field(run, "started_at"),
            "ended_at": _field(run, "ended_at"),
            "summary": _field(run, "summary"),
            "error": _field(run, "error"),
        },
        "contract": {
            "expected_version": REPORT_CONTRACT_VERSION,
            "version": version,
            "ok": version == REPORT_CONTRACT_VERSION,
        },
        "metadata": metadata,
        "verification_evidence": _verification_evidence(metadata),
        "receipt_reference": _receipt_reference(metadata),
        "scope_attestation": _scope_attestation(metadata),
        "events": [
            {
                "id": _field(event, "id"),
                "kind": _field(event, "kind"),
                "created_at": _field(event, "created_at"),
                "run_id": _field(event, "run_id"),
            }
            for event in (events or [])
            if _field(event, "run_id") == _field(run, "id")
        ],
    }
    report["quality"] = report_quality(report)
    return report


def reports_for_task(conn: Any, task_id: str, *, include_incomplete: bool = False) -> list[dict[str, Any]]:
    """Return normalized reports for a task, oldest first."""
    task = kb.get_task(conn, task_id)
    if task is None:
        return []
    runs = kb.list_runs(conn, task_id)
    events = kb.list_events(conn, task_id)
    reports: list[dict[str, Any]] = []
    for run in runs:
        status = _field(run, "status")
        if not include_incomplete and status not in ("done", "completed"):
            continue
        reports.append(normalize_run_report(task, run, events))
    return reports


def latest_report_for_task(conn: Any, task_id: str) -> Optional[dict[str, Any]]:
    """Return the latest completed report for a task, or None."""
    reports = reports_for_task(conn, task_id)
    return reports[-1] if reports else None
