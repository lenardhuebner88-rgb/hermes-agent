#!/usr/bin/env python3
"""Autoresearch proposal reconciler.

Routes passive Autoresearch proposals into the unified self-improvement flywheel:
skill documentation fixes are applied through the existing proposal gate, code/test
findings become deduped Kanban tasks, and risky or non-actionable findings become
operator decisions via the existing decision-queue escalation event.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli import autoresearch_proposals as proposals
from hermes_cli import kanban_db as kb
from hermes_cli.config import get_hermes_home

SEVERITY_ORDINAL = {"low": 1, "medium": 2, "high": 3, "critical": 4}
DEFAULT_MIN_TASK_SEVERITY = "high"
DEFAULT_MAX_NEW_TASKS = 5
CREATED_BY = "autoresearch"
AUTORESEARCH_VETO_PREFIX = "autoresearch:"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _severity(proposal: dict[str, Any]) -> str:
    raw = str(proposal.get("severity") or "").strip().lower()
    return raw if raw in SEVERITY_ORDINAL else "medium"


def _priority(proposal: dict[str, Any]) -> int:
    return SEVERITY_ORDINAL.get(_severity(proposal), 2)


def _meets_min_severity(proposal: dict[str, Any], minimum: str) -> bool:
    return SEVERITY_ORDINAL.get(_severity(proposal), 0) >= SEVERITY_ORDINAL.get(minimum, 3)


def _finding_id(proposal: dict[str, Any]) -> str:
    return str(proposal.get("finding_id") or proposal.get("id") or "finding").strip()


def _signal_key(proposal: dict[str, Any]) -> str:
    for key in ("signal_key", "theme", "category", "section", "target"):
        value = str(proposal.get(key) or "").strip()
        if value:
            return value.replace("_", "-").lower()
    return _finding_id(proposal).replace("_", "-").lower()


def _subsystem(proposal: dict[str, Any]) -> str:
    value = str(proposal.get("subsystem") or "").strip()
    if value:
        return value
    target = str(proposal.get("target") or proposal.get("target_path") or "").strip()
    if target and "/" in target:
        return target.split("/", 1)[0]
    return target or "unknown"


def _is_code_or_test(proposal: dict[str, Any]) -> bool:
    mode = str(proposal.get("mode") or "").strip().lower()
    ptype = str(proposal.get("proposal_type") or "").strip().lower()
    target = str(proposal.get("target") or proposal.get("target_path") or "").lower()
    category = str(proposal.get("category") or "").lower()
    return (
        mode == "code"
        or ptype in {"deep_audit", "test_foundry", "code", "test"}
        or "test" in category
        or target.startswith("tests/")
    )


def _skill_doc_has_diff(proposal: dict[str, Any]) -> bool:
    return (
        str(proposal.get("mode") or "") == "skill"
        and bool(str(proposal.get("diff_before_after") or "").strip())
        and bool(str(proposal.get("new_text") or "").strip())
    )


def _task_for_idempotency(conn, idem: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
        (idem,),
    ).fetchone()
    return row["id"] if row else None


def _proposal_body(proposal: dict[str, Any]) -> str:
    parts = [
        "Autoresearch routed this finding for code/test implementation.",
        f"Finding ID: {_finding_id(proposal)}",
        f"Severity: {_severity(proposal)}",
        f"Signal: {_signal_key(proposal)}",
    ]
    target = proposal.get("target_path") or proposal.get("target")
    if target:
        parts.append(f"Target: {target}")
    rationale = proposal.get("rationale_plain") or proposal.get("problem") or proposal.get("fix_hint")
    if rationale:
        parts.append("\nRationale:\n" + str(rationale))
    return "\n".join(parts)


def _route_to_kanban(conn, proposal: dict[str, Any]) -> tuple[str, bool]:
    idem = "autoresearch:" + _finding_id(proposal)
    existing = _task_for_idempotency(conn, idem)
    task_id = kb.create_task(
        conn,
        title=str(proposal.get("title") or f"Autoresearch finding {_finding_id(proposal)}"),
        body=_proposal_body(proposal),
        assignee="coder",
        created_by=CREATED_BY,
        idempotency_key=idem,
        priority=_priority(proposal),
        initial_status="running",
        kind="code",
    )
    return task_id, existing is None


def _escalation_payload(conn, task_id: str, proposal: dict[str, Any], reason: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    payload = kb._operator_escalation_payload(
        task_id=task_id,
        row=row,
        failures=1,
        effective_limit=1,
        limit_source="autoresearch_reconciler",
        error=reason,
        outcome="operator_review_required",
        event_payload_extra={
            "proposal_id": proposal.get("id"),
            "finding_id": _finding_id(proposal),
            "subsystem": _subsystem(proposal),
            "theme": _signal_key(proposal),
        },
    )
    payload["source"] = "autoresearch"
    payload["signal_key"] = _signal_key(proposal)
    return payload


def _escalate(conn, proposal: dict[str, Any], reason: str) -> str:
    idem = "autoresearch-escalation:" + _finding_id(proposal)
    task_id = kb.create_task(
        conn,
        title=str(proposal.get("title") or f"Autoresearch escalation {_finding_id(proposal)}"),
        body=f"{reason}\n\nProposal: {proposal.get('id')}\nSignal: {_signal_key(proposal)}",
        assignee=None,
        created_by=CREATED_BY,
        idempotency_key=idem,
        priority=_priority(proposal),
        initial_status="blocked",
        kind="ops",
    )
    payload = _escalation_payload(conn, task_id, proposal, reason)
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, kb.OPERATOR_ESCALATION_EVENT, payload)
    return task_id


def _digest_path() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_DIGEST_PATH", "").strip()
    if override:
        return Path(override)
    return get_hermes_home() / "state" / "strategist" / "autoresearch-digest.json"


def _suppression_path() -> Path:
    override = os.environ.get("HERMES_STRATEGIST_VETOED_PATH", "").strip()
    if override:
        return Path(override)
    return get_hermes_home() / "state" / "strategist" / "vetoed_levers.json"


def _suppressed_autoresearch_signals() -> set[str]:
    path = _suppression_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    signals: set[str] = set()
    for item in data:
        raw = str(item or "").strip().lower()
        if raw.startswith(AUTORESEARCH_VETO_PREFIX):
            signal = raw[len(AUTORESEARCH_VETO_PREFIX):].strip()
            if signal:
                signals.add(signal)
    return signals


def _write_digest(processed: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in processed:
        proposal = item["proposal"]
        key = (_subsystem(proposal), _signal_key(proposal))
        entry = grouped.setdefault(
            key,
            {
                "subsystem": key[0],
                "theme": key[1],
                "count": 0,
                "severity_max": _severity(proposal),
                "example_finding_ids": [],
                "atomic_tasks_filed": 0,
                "escalated": 0,
            },
        )
        entry["count"] += 1
        if SEVERITY_ORDINAL[_severity(proposal)] > SEVERITY_ORDINAL[entry["severity_max"]]:
            entry["severity_max"] = _severity(proposal)
        fid = _finding_id(proposal)
        if fid not in entry["example_finding_ids"] and len(entry["example_finding_ids"]) < 5:
            entry["example_finding_ids"].append(fid)
        if item["route"] == "kanban":
            entry["atomic_tasks_filed"] += 1
        if item["route"] == "escalated":
            entry["escalated"] += 1
    digest = {"generated_at": _utc_now(), "themes": sorted(grouped.values(), key=lambda x: (x["subsystem"], x["theme"]))}
    path = _digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(digest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def reconcile_proposals(
    *,
    conn=None,
    max_new_tasks: int | None = None,
    min_task_severity: str = DEFAULT_MIN_TASK_SEVERITY,
    once: bool = False,
) -> dict[str, Any]:
    """Route every currently proposed proposal into one flywheel lane."""
    own_conn = conn is None
    if conn is None:
        conn = kb.connect()
    max_new = DEFAULT_MAX_NEW_TASKS if max_new_tasks is None else max(0, int(max_new_tasks))
    summary = {
        "ok": True,
        "once": bool(once),
        "seen": 0,
        "applied": 0,
        "routed_to_kanban": 0,
        "new_tasks": 0,
        "pooled": 0,
        "escalated": 0,
        "suppressed": 0,
        "errors": 0,
    }
    suppressed_signals = _suppressed_autoresearch_signals()
    processed: list[dict[str, Any]] = []
    try:
        for proposal in proposals.list_proposals():
            if proposal.get("status") != "proposed":
                continue
            summary["seen"] += 1
            pid = str(proposal.get("id"))
            try:
                signal_key = _signal_key(proposal)
                if signal_key in suppressed_signals:
                    proposal.update({
                        "status": "skipped",
                        "reconciled_at": _utc_now(),
                        "result": f"suppressed by strategist veto: {signal_key}",
                    })
                    proposals.save_proposal(proposal)
                    summary["suppressed"] += 1
                    continue

                if _skill_doc_has_diff(proposal) and not _is_code_or_test(proposal):
                    result = proposals.apply_proposal(pid, confirm=True, judged=True)
                    if result.get("ok") and result.get("status") == "applied":
                        summary["applied"] += 1
                        processed.append({"route": "applied", "proposal": proposal})
                        continue
                    task_id = _escalate(conn, proposal, str(result.get("detail") or "skill apply gate failed"))
                    proposal = proposals.load_proposal(pid) or proposal
                    proposal.update({
                        "status": "escalated",
                        "escalation_task_id": task_id,
                        "reconciled_at": _utc_now(),
                        "result": str(result.get("detail") or "skill apply gate failed"),
                    })
                    proposals.save_proposal(proposal)
                    summary["escalated"] += 1
                    processed.append({"route": "escalated", "proposal": proposal})
                    continue

                if _is_code_or_test(proposal) and _meets_min_severity(proposal, min_task_severity):
                    if summary["new_tasks"] >= max_new and _task_for_idempotency(conn, "autoresearch:" + _finding_id(proposal)) is None:
                        proposal.update({
                            "status": "pooled",
                            "reconciled_at": _utc_now(),
                            "result": f"pooled by flood guard: max {max_new} new tasks per run",
                        })
                        proposals.save_proposal(proposal)
                        summary["pooled"] += 1
                        processed.append({"route": "pooled", "proposal": proposal})
                        continue
                    task_id, created = _route_to_kanban(conn, proposal)
                    proposal.update({
                        "status": "routed_to_kanban",
                        "kanban_task_id": task_id,
                        "reconciled_at": _utc_now(),
                        "result": f"routed to Kanban task {task_id}",
                    })
                    proposals.save_proposal(proposal)
                    summary["routed_to_kanban"] += 1
                    if created:
                        summary["new_tasks"] += 1
                    processed.append({"route": "kanban", "proposal": proposal})
                    continue

                reason = "autoresearch finding needs operator review: risky, low-severity, or missing actionable diff"
                task_id = _escalate(conn, proposal, reason)
                proposal.update({
                    "status": "escalated",
                    "escalation_task_id": task_id,
                    "reconciled_at": _utc_now(),
                    "result": reason,
                })
                proposals.save_proposal(proposal)
                summary["escalated"] += 1
                processed.append({"route": "escalated", "proposal": proposal})
            except Exception as exc:
                summary["errors"] += 1
                proposal["reconcile_error"] = f"{type(exc).__name__}: {exc}"
                proposals.save_proposal(proposal)
        _write_digest(processed)
        return summary
    finally:
        if own_conn:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route Autoresearch proposals into the self-improvement flywheel.")
    parser.add_argument("--once", action="store_true", help="Run one explicit backlog-drain/reconcile pass.")
    parser.add_argument("--max-new-tasks", type=int, default=DEFAULT_MAX_NEW_TASKS)
    parser.add_argument("--min-task-severity", default=DEFAULT_MIN_TASK_SEVERITY)
    args = parser.parse_args(argv)
    summary = reconcile_proposals(
        once=args.once,
        max_new_tasks=args.max_new_tasks,
        min_task_severity=args.min_task_severity,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
