#!/usr/bin/env python3
"""Autoresearch proposal reconciler.

Routes passive Autoresearch proposals into the unified self-improvement flywheel:
skill documentation fixes are applied through the existing proposal gate, code/test
findings become deduped Kanban tasks, and risky or non-actionable findings become
operator decisions via the existing decision-queue escalation event.
"""
from __future__ import annotations

import argparse
import hashlib
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

# Severity-derived iteration budget floor for code/test findings routed to Kanban.
# These mirror the PlanSpec budget floors defined in plan_compiler.py
# (_TURN_FLOOR_BY_REVIEW_TIER / _TURN_FLOOR_CONTRACT_DEPTH) and are a FLOOR:
# only explicit "low" severity is omitted → .get() returns None → the profile
# default of 90 turns is used. (Unknown/empty severity coalesces to "medium" in
# _severity(), so it lands on the medium floor (150), not the default.)
_SEVERITY_TO_MAX_ITERATIONS: dict[str, int] = {
    "critical": 220,
    "high": 180,
    "medium": 150,
}

_SEVERITY_TO_REVIEW_TIER: dict[str, str] = {
    "critical": "critical",
    "high": "review",
    "medium": "review",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _severity(proposal: dict[str, Any]) -> str:
    raw = str(proposal.get("severity") or "").strip().lower()
    return raw if raw in SEVERITY_ORDINAL else "medium"


def _priority(proposal: dict[str, Any]) -> int:
    return SEVERITY_ORDINAL.get(_severity(proposal), 2)


def _review_tier(proposal: dict[str, Any]) -> str | None:
    return _SEVERITY_TO_REVIEW_TIER.get(_severity(proposal))


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


def _proposal_hash(proposal: dict[str, Any]) -> str:
    payload = {
        key: proposal.get(key)
        for key in (
            "id", "schema", "proposal_type", "finding_id", "target", "target_path",
            "severity", "category", "evidence", "fix_hint", "rationale_plain",
        )
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _proposal_contract_missing(proposal: dict[str, Any]) -> list[str]:
    target = str(proposal.get("target") or proposal.get("target_path") or "").strip()
    required = {
        "target": target,
        "evidence": str(proposal.get("evidence") or "").strip(),
        "fix_hint": str(proposal.get("fix_hint") or "").strip(),
    }
    return [name for name, value in required.items() if not value]


def _proposal_task_contract(proposal: dict[str, Any]) -> dict[str, Any]:
    target = str(proposal.get("target") or proposal.get("target_path") or "").strip()
    evidence = str(proposal.get("evidence") or "").strip()
    fix_hint = str(proposal.get("fix_hint") or "").strip()
    problem = str(
        proposal.get("problem")
        or proposal.get("rationale_plain")
        or "Resolve the grounded Autoresearch finding."
    ).strip()
    pid = str(proposal.get("id") or "").strip()
    proposal_hash = _proposal_hash(proposal)
    validation = f"scripts/run-affected.sh {target}"
    non_goals = [
        "No unrelated refactor or cleanup.",
        "No push, deploy, runtime restart, secret change, or schema migration.",
    ]
    acceptance = "\n".join([
        f"- AC-AR1: The grounded finding at {target} is resolved without unrelated changes.",
        "- AC-AR2: A focused regression test or equivalent executable proof covers the failure mode.",
        f"- AC-AR3: `{validation}` passes, or an exact blocking reason is recorded.",
        "- AC-AR4: The completion report names changed paths, validation evidence, and residual risk.",
    ])
    body = "\n".join([
        "Autoresearch routed this grounded finding for code/test implementation.",
        "",
        "## Provenance",
        f"- Proposal ID: {pid}",
        f"- Proposal hash (sha256): {proposal_hash}",
        f"- Finding ID: {_finding_id(proposal)}",
        f"- Schema: {proposal.get('schema') or 'unknown'}",
        f"- Type: {proposal.get('proposal_type') or proposal.get('mode') or 'unknown'}",
        f"- Severity / review tier: {_severity(proposal)} / {_review_tier(proposal) or 'default'}",
        f"- Signal: {_signal_key(proposal)}",
        "",
        "## Grounded scope",
        f"- Exact target: {target}",
        f"- Evidence: {evidence}",
        f"- Problem: {problem}",
        f"- Fix intent: {fix_hint}",
        "",
        "## Acceptance criteria",
        acceptance,
        "",
        "## Validation",
        f"- {validation}",
        "",
        "## Non-goals",
        *(f"- {item}" for item in non_goals),
    ])
    scope_contract = {
        "version": 1,
        "source": "autoresearch",
        "proposal_id": pid,
        "proposal_hash_sha256": proposal_hash,
        "proposal_schema": proposal.get("schema"),
        "proposal_type": proposal.get("proposal_type") or proposal.get("mode"),
        "finding_id": _finding_id(proposal),
        "severity": _severity(proposal),
        "review_tier": _review_tier(proposal),
        "allowed_paths": [target],
        "allowed_tools": ["file", "terminal", "kanban"],
        "evidence": evidence,
        "fix_intent": fix_hint,
        "validation": [validation],
        "non_goals": non_goals,
    }
    return {
        "body": body,
        "acceptance_criteria": acceptance,
        "scope_contract": scope_contract,
    }


def _route_to_kanban(conn, proposal: dict[str, Any]) -> tuple[str, bool]:
    idem = "autoresearch:" + _finding_id(proposal)
    existing = _task_for_idempotency(conn, idem)
    max_iter = _SEVERITY_TO_MAX_ITERATIONS.get(_severity(proposal))
    review_tier = _review_tier(proposal)
    contract = _proposal_task_contract(proposal)
    task_id = kb.create_task(
        conn,
        title=str(proposal.get("title") or f"Autoresearch finding {_finding_id(proposal)}"),
        body=contract["body"],
        acceptance_criteria=contract["acceptance_criteria"],
        assignee="coder",
        created_by=CREATED_BY,
        idempotency_key=idem,
        priority=_priority(proposal),
        initial_status="running",
        kind="code",
        scope_contract=contract["scope_contract"],
        max_iterations=max_iter,
        review_tier=review_tier,
    )
    if existing is not None and review_tier:
        row = conn.execute(
            "SELECT review_tier FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row and not row["review_tier"]:
            kb.set_task_review_tier(conn, task_id, review_tier)
    if existing is not None:
        # Repair old idempotent Autoresearch cards that predate the grounded
        # handoff contract. Never replace an active worker's body underneath it.
        with kb.write_txn(conn):
            row = conn.execute(
                "SELECT status, created_by, body, acceptance_criteria, scope_contract "
                "FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if row and row["created_by"] == CREATED_BY:
                updates: list[str] = []
                values: list[Any] = []
                safe_to_backfill = row["status"] not in {"running", "review"}
                if safe_to_backfill and not row["acceptance_criteria"]:
                    updates.append("acceptance_criteria = ?")
                    values.append(kb._parse_acceptance_criteria(contract["acceptance_criteria"]))
                if safe_to_backfill and not row["scope_contract"]:
                    updates.append("scope_contract = ?")
                    values.append(json.dumps(contract["scope_contract"], ensure_ascii=False))
                if safe_to_backfill and "Proposal hash (sha256)" not in str(row["body"] or ""):
                    updates.append("body = ?")
                    values.append(contract["body"])
                if updates:
                    values.append(task_id)
                    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
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
    # Coalesce by signal, NOT by finding: many findings sharing a signal (e.g. 41
    # silent-except hits) collapse into ONE operator decision. The operator vetoes
    # the *signal* (→ reflect suppresses it), so one decision-queue row per signal
    # matches the suppression granularity and keeps a backlog drain from flooding
    # the queue. Findings without a real signal fall back to a finding-unique key.
    signal = _signal_key(proposal)
    idem = "autoresearch-escalation:" + signal
    existing = _task_for_idempotency(conn, idem)
    review_tier = _review_tier(proposal)
    task_id = kb.create_task(
        conn,
        title=str(proposal.get("title") or f"Autoresearch escalation: {signal}"),
        body=f"{reason}\n\nProposal: {proposal.get('id')}\nSignal: {signal}",
        assignee=None,
        created_by=CREATED_BY,
        idempotency_key=idem,
        priority=_priority(proposal),
        initial_status="blocked",
        kind="ops",
        review_tier=review_tier,
    )
    if existing is not None and review_tier:
        row = conn.execute(
            "SELECT review_tier FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row and not row["review_tier"]:
            kb.set_task_review_tier(conn, task_id, review_tier)
    # Only the first finding of a signal emits the escalation event; later ones
    # reuse the same blocked task (idempotency) without re-raising a duplicate.
    if existing is None:
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
    return digest


def _last_reconcile_path() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_RECONCILE_SUMMARY_PATH", "").strip()
    if override:
        return Path(override)
    return get_hermes_home() / "state" / "strategist" / "autoresearch-last-reconcile.json"


def load_last_reconcile() -> dict[str, Any] | None:
    """The outcome of the most recent real reconcile run — what the tab shows as
    'what the loop did'. ``None`` if it has never run."""
    try:
        data = json.loads(_last_reconcile_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _write_last_reconcile(summary: dict[str, Any], digest: dict[str, Any]) -> None:
    record = {
        "generated_at": digest.get("generated_at") or _utc_now(),
        "summary": summary,
        "themes": digest.get("themes", []),
    }
    path = _last_reconcile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def reconcile_proposals(
    *,
    conn=None,
    max_new_tasks: int | None = None,
    min_task_severity: str = DEFAULT_MIN_TASK_SEVERITY,
    once: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Route every currently proposed proposal into one flywheel lane.

    With ``dry_run=True`` nothing is mutated — no apply, no task/escalation
    creation, no proposal-status writes, no digest written. The summary instead
    reports how the current backlog WOULD route, so the operator can preview the
    drain before triggering it for real.
    """
    own_conn = conn is None
    if conn is None:
        conn = kb.connect()
    max_new = DEFAULT_MAX_NEW_TASKS if max_new_tasks is None else max(0, int(max_new_tasks))
    dry_seen: set[str] = set()
    summary = {
        "ok": True,
        "once": bool(once),
        "dry_run": bool(dry_run),
        "seen": 0,
        "applied": 0,
        "held_judge_required": 0,
        "held_invalid_contract": 0,
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
                if dry_run:
                    # Pure classification — mirror the routing decisions below
                    # without any side effect. The skill-doc lane is counted as
                    # held intent; only the dashboard judge may authorize apply.
                    if signal_key in suppressed_signals:
                        summary["suppressed"] += 1
                    elif _skill_doc_has_diff(proposal) and not _is_code_or_test(proposal):
                        summary["held_judge_required"] += 1
                    elif _is_code_or_test(proposal) and _meets_min_severity(proposal, min_task_severity):
                        if _proposal_contract_missing(proposal):
                            summary["held_invalid_contract"] += 1
                        else:
                            fkey = "autoresearch:" + _finding_id(proposal)
                            if fkey in dry_seen:
                                summary["routed_to_kanban"] += 1
                            elif summary["new_tasks"] >= max_new:
                                summary["pooled"] += 1
                            else:
                                dry_seen.add(fkey)
                                summary["routed_to_kanban"] += 1
                                summary["new_tasks"] += 1
                    else:
                        summary["escalated"] += 1
                    continue

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
                    proposal.update({
                        "status": "proposed",
                        "last_outcome": "held_judge_required",
                        "reconciled_at": _utc_now(),
                        "result": "held: independent operator judge and batch-confirm required",
                    })
                    proposals.save_proposal(proposal)
                    summary["held_judge_required"] += 1
                    processed.append({"route": "held_judge_required", "proposal": proposal})
                    continue

                if _is_code_or_test(proposal) and _meets_min_severity(proposal, min_task_severity):
                    missing = _proposal_contract_missing(proposal)
                    if missing:
                        proposal.update({
                            "status": "proposed",
                            "last_outcome": "held_invalid_contract",
                            "reconciled_at": _utc_now(),
                            "result": "held: missing grounded task contract fields: " + ", ".join(missing),
                        })
                        proposals.save_proposal(proposal)
                        summary["held_invalid_contract"] += 1
                        processed.append({"route": "held_invalid_contract", "proposal": proposal})
                        continue
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
        if not dry_run:
            digest = _write_digest(processed)
            _write_last_reconcile(summary, digest)
        return summary
    finally:
        if own_conn:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route Autoresearch proposals into the self-improvement flywheel.")
    parser.add_argument("--once", action="store_true", help="Run one explicit backlog-drain/reconcile pass.")
    parser.add_argument("--max-new-tasks", type=int, default=DEFAULT_MAX_NEW_TASKS)
    parser.add_argument("--min-task-severity", default=DEFAULT_MIN_TASK_SEVERITY)
    parser.add_argument("--dry-run", action="store_true", help="Classify the backlog without any side effect (preview the drain).")
    args = parser.parse_args(argv)
    summary = reconcile_proposals(
        once=args.once,
        max_new_tasks=args.max_new_tasks,
        min_task_severity=args.min_task_severity,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
