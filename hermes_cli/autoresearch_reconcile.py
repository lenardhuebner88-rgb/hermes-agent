#!/usr/bin/env python3
"""Autoresearch proposal reconciler.

Routes passive Autoresearch proposals into the unified self-improvement flywheel:
skill documentation fixes are applied through the existing proposal gate, code/test
findings become deduped Kanban tasks, and risky or non-actionable findings become
operator decisions via the existing decision-queue escalation event.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli import autoresearch_proposals as proposals
from hermes_cli import kanban_db as kb
from hermes_cli import outcome_verification as outcomes
from hermes_cli.config import get_hermes_home

SEVERITY_ORDINAL = {"low": 1, "medium": 2, "high": 3, "critical": 4}
DEFAULT_MIN_TASK_SEVERITY = "high"
DEFAULT_MAX_NEW_TASKS = 5
CREATED_BY = "autoresearch"
AUTORESEARCH_VETO_PREFIX = "autoresearch:"
REPO_ROOT = Path(__file__).resolve().parents[1]

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


def _reconcile_lock_path() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_RECONCILE_LOCK_PATH", "").strip()
    if override:
        return Path(override)
    return get_hermes_home() / "state" / "strategist" / "autoresearch-reconcile.lock"


def _try_acquire_reconcile_lock():
    """Return the held cross-process lock, or ``None`` when a run is active.

    The per-run task counter is only a real flood guard when exactly one
    reconciler owns the proposal snapshot.  Fail fast instead of queueing a
    second invocation: a queued caller would immediately drain the first run's
    freshly pooled remainder and turn one concurrent wave into two budgets.
    """
    path = _reconcile_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _release_reconcile_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


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
        mode in {"code", "test"}
        or ptype in {"deep_audit", "test_foundry", "code", "test"}
        or "test" in category
        or target.startswith("tests/")
    )


def _is_verified_test_foundry(proposal: dict[str, Any]) -> bool:
    return str(proposal.get("proposal_type") or "").strip().lower() == "mutation_test"


def _delivery_key(proposal: dict[str, Any]) -> str:
    """Kanban idempotency identity; mutation tests are bundled per target."""
    if _is_verified_test_foundry(proposal):
        target = str(proposal.get("target") or proposal.get("target_path") or "unknown").strip().lower()
        return "test-foundry:" + target.replace("/", "-").replace("_", "-")
    return _finding_id(proposal)


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
    affected_tests = [str(item) for item in (proposal.get("affected_tests") or []) if str(item).strip()]
    validation = "scripts/run_tests.sh " + " ".join(affected_tests) if affected_tests else f"scripts/run-affected.sh {target}"
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
        f"- Expected benefit: {proposal.get('expected_benefit') or problem}",
        f"- Risk: {proposal.get('risk_summary') or 'Narrow target-scoped change; regression risk is controlled by the stated gate.'}",
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
        "bundle_key": _delivery_key(proposal),
        "validation": [validation],
        "non_goals": non_goals,
    }
    return {
        "body": body,
        "acceptance_criteria": acceptance,
        "scope_contract": scope_contract,
    }


def _route_to_kanban(
    conn,
    proposal: dict[str, Any],
    *,
    allow_create: bool = True,
) -> tuple[str | None, bool]:
    idem = "autoresearch:" + _delivery_key(proposal)
    existing = _task_for_idempotency(conn, idem)
    max_iter = _SEVERITY_TO_MAX_ITERATIONS.get(_severity(proposal))
    review_tier = _review_tier(proposal)
    contract = _proposal_task_contract(proposal)
    if existing is not None:
        task_id = existing
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
            # An archive can race the optimistic lookup. Revalidate while the
            # same BEGIN IMMEDIATE lock is held before returning a reference.
            task_id = _task_for_idempotency(conn, idem)
        if task_id is not None:
            row = conn.execute(
                "SELECT status, created_by FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            # Recovery seam: a task born blocked by this reconciler may have
            # survived a crash between create, contract registration and
            # release.  It is safe to finish only while still undispatchable.
            if row and row["created_by"] == CREATED_BY and row["status"] == "blocked":
                _register_outcome_before_release(conn, proposal, task_id)
            _append_proposal_reference(conn, task_id, proposal)
            return task_id, False

    if not allow_create:
        return None, False

    _prepare_outcome_baseline(proposal)
    task_id, created = _create_or_reuse_task(
        conn,
        title=str(proposal.get("title") or f"Autoresearch finding {_finding_id(proposal)}"),
        body=contract["body"],
        acceptance_criteria=contract["acceptance_criteria"],
        assignee="coder",
        created_by=CREATED_BY,
        idempotency_key=idem,
        priority=_priority(proposal),
        # The worker cannot observe this task until the immutable probe
        # contract and baseline are durably linked to task_events.
        initial_status="blocked",
        kind="code",
        # Autoresearch code delivery must enter the same dispatcher-managed
        # worktree/integrator path as every other repository task.  A scratch
        # task can never produce the two independent integration witnesses the
        # outcome verifier requires.
        workspace_kind="worktree",
        workspace_path=str(REPO_ROOT),
        scope_contract=contract["scope_contract"],
        max_iterations=max_iter,
        review_tier=review_tier,
    )
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row and row["status"] == "blocked":
        _register_outcome_before_release(conn, proposal, task_id)
    elif not created:
        _append_proposal_reference(conn, task_id, proposal)
    return task_id, created


def _prepare_outcome_baseline(proposal: dict[str, Any]) -> None:
    """Persist a bounded probe baseline before a task can be dispatched."""
    pid = str(proposal.get("id") or "")
    with proposals.proposal_lease(pid):
        latest = proposals.load_proposal(pid)
        authoritative = {**proposal, **(latest or {})}
        existing = authoritative.get("probe_contract")
        baseline = authoritative.get("outcome_baseline")
        if isinstance(existing, dict) or isinstance(baseline, dict):
            if not isinstance(existing, dict) or not isinstance(baseline, dict):
                raise outcomes.ContractError("partial persisted outcome baseline is invalid")
            validated = outcomes.validate_probe_contract(existing)
            outcomes.validate_baseline(validated, baseline)
            authoritative.update(
                {
                    "evidence_grade": "legacy_observational",
                    "outcome_class": validated["outcome_class"],
                    "outcome_target_sha": baseline["target_sha"],
                    "outcome_authority": authoritative.get("outcome_authority")
                    or "proposal_contract",
                }
            )
            proposal.clear()
            proposal.update(authoritative)
            proposals.save_proposal(proposal)
            return
        probe_contract = outcomes.build_probe_contract(authoritative, repo_root=REPO_ROOT)
        captured = outcomes.capture_probe(probe_contract, repo_root=REPO_ROOT)
        outcomes.validate_baseline(probe_contract, captured)
        captured = outcomes.seal_evidence(
            {
                **captured,
                "research_cost_usd": max(
                    0.0, float(authoritative.get("cost_usd") or 0.0)
                ),
            }
        )
        recorded_at = _utc_now()
        fingerprint = outcomes.release_fingerprint(
            proposal_id=pid,
            contract=probe_contract,
            baseline=captured,
            target_sha256=str(authoritative.get("target_sha256") or "") or None,
        )
        authoritative.update(
            {
                "outcome_schema_version": outcomes.OUTCOME_SCHEMA_VERSION,
                "outcome_applicability": "applicable",
                "measurement_status": "pending",
                "outcome_verdict": None,
                "evidence_grade": "legacy_observational",
                "calibration_eligible": False,
                "probe_contract": probe_contract,
                "outcome_class": probe_contract["outcome_class"],
                "outcome_baseline": captured,
                "outcome_target_sha": captured["target_sha"],
                "outcome_baseline_recorded_at": recorded_at,
                "outcome_release_fingerprint": fingerprint,
                "outcome_authority": "proposal_contract",
            }
        )
        proposal.clear()
        proposal.update(authoritative)
        # This is intentionally before create_task(). A crash here leaves only
        # a recoverable proposal baseline and no dispatchable work.
        proposals.save_proposal(proposal)


def _register_outcome_before_release(conn, proposal: dict[str, Any], task_id: str) -> None:
    _prepare_outcome_baseline(proposal)
    probe_contract = proposal["probe_contract"]
    baseline = proposal["outcome_baseline"]
    fingerprint = str(proposal.get("outcome_release_fingerprint") or "")
    outcomes.register_contract(
        conn,
        proposal_id=str(proposal.get("id") or ""),
        task_id=task_id,
        contract=probe_contract,
        baseline=baseline,
        release_fingerprint=fingerprint,
    )
    proposal["kanban_task_id"] = task_id
    proposal["linked_task_id"] = task_id
    proposal["outcome_authority"] = "task_events"
    proposal["lifecycle_source"] = "task_events"
    proposals.save_proposal(proposal)
    if not kb.unblock_task(conn, task_id):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] not in {"ready", "todo"}:
            raise RuntimeError(f"outcome-contracted task {task_id} could not be released")


def _create_or_reuse_task(conn, **kwargs: Any) -> tuple[str, bool]:
    """Return Kanban's selected task and whether this connection inserted it."""
    changes_before = conn.total_changes
    task_id = kb.create_task(conn, **kwargs)
    return task_id, conn.total_changes > changes_before


def _append_proposal_reference(conn, task_id: str, proposal: dict[str, Any]) -> None:
    """Enumerate every coalesced finding exactly once on its shared task."""
    pid = str(proposal.get("id") or "").strip()
    marker = f"[autoresearch-proposal:{pid}]"
    if not pid:
        return
    exists = conn.execute(
        "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ? LIMIT 1",
        (task_id, f"%{marker}%"),
    ).fetchone()
    if exists:
        return
    body = "\n".join([
        marker,
        f"Target: {proposal.get('target') or proposal.get('target_path') or 'unknown'}",
        f"Evidence: {proposal.get('evidence') or 'not recorded'}",
        f"Recommended fix: {proposal.get('fix_hint') or 'revalidate and resolve the grounded finding'}",
    ])
    kb.add_comment(conn, task_id, CREATED_BY, body)


def _escalation_payload(conn, task_id: str, proposal: dict[str, Any], reason: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    payload = kb._operator_escalation_payload(
        conn=conn,
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
    else:
        _append_proposal_reference(conn, task_id, proposal)
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
    max_new = DEFAULT_MAX_NEW_TASKS if max_new_tasks is None else max(0, int(max_new_tasks))
    dry_seen: set[str] = set()
    summary = {
        "ok": True,
        "busy": False,
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
        "stale": 0,
        "rejected_detection_only": 0,
        "suppressed": 0,
        "errors": 0,
    }
    reconcile_lock = None
    if not dry_run:
        reconcile_lock = _try_acquire_reconcile_lock()
        if reconcile_lock is None:
            summary["busy"] = True
            summary["detail"] = "another autoresearch reconcile run already owns the flood budget"
            return summary
    suppressed_signals = _suppressed_autoresearch_signals()
    processed: list[dict[str, Any]] = []
    try:
        if conn is None:
            conn = kb.connect()
        for proposal in proposals._enriched_items(proposals.list_proposals(), conn=conn):
            if proposal.get("status") not in {"proposed", "pooled"}:
                continue
            summary["seen"] += 1
            pid = str(proposal.get("id"))
            try:
                signal_key = _signal_key(proposal)
                if proposal.get("finding_state") == "stale" or proposal.get("duplicate_of"):
                    if dry_run:
                        summary["stale"] += 1
                    else:
                        proposal.update({
                            "status": "skipped",
                            "last_outcome": "stale_target_changed" if proposal.get("target_stale") else "rejected_duplicate",
                            "reconciled_at": _utc_now(),
                            "result": proposal.get("disposition_reason") or "stale or duplicate finding",
                            "disposition_source": "autoresearch_reconciler",
                        })
                        proposals.save_proposal(proposal)
                        summary["stale"] += 1
                        processed.append({"route": "stale", "proposal": proposal})
                    continue
                if dry_run:
                    # Pure classification — mirror the routing decisions below
                    # without any side effect. The skill-doc lane is counted as
                    # held intent; only the dashboard judge may authorize apply.
                    if signal_key in suppressed_signals:
                        summary["suppressed"] += 1
                    elif _skill_doc_has_diff(proposal) and not _is_code_or_test(proposal):
                        summary["held_judge_required"] += 1
                    elif _is_code_or_test(proposal) and (
                        _meets_min_severity(proposal, min_task_severity) or _is_verified_test_foundry(proposal)
                    ):
                        if _proposal_contract_missing(proposal):
                            summary["held_invalid_contract"] += 1
                        else:
                            fkey = "autoresearch:" + _delivery_key(proposal)
                            if fkey in dry_seen:
                                summary["routed_to_kanban"] += 1
                            elif summary["new_tasks"] >= max_new:
                                summary["pooled"] += 1
                            else:
                                dry_seen.add(fkey)
                                summary["routed_to_kanban"] += 1
                                summary["new_tasks"] += 1
                    else:
                        summary["rejected_detection_only"] += 1
                    continue

                if signal_key in suppressed_signals:
                    proposal.update({
                        "status": "skipped",
                        "last_outcome": "rejected_strategist_veto",
                        "reconciled_at": _utc_now(),
                        "result": f"suppressed by strategist veto: {signal_key}",
                        "disposition_reason": f"suppressed by strategist veto: {signal_key}",
                        "disposition_source": "strategist_veto",
                        "finding_state": "rejected",
                        "decision_state": "dismissed",
                        "delivery_state": "none",
                        "operator_action_required": False,
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

                if _is_code_or_test(proposal) and (
                    _meets_min_severity(proposal, min_task_severity) or _is_verified_test_foundry(proposal)
                ):
                    missing = _proposal_contract_missing(proposal)
                    if missing:
                        proposal.update({
                            "status": "skipped",
                            "last_outcome": "held_invalid_contract",
                            "reconciled_at": _utc_now(),
                            "result": "held: missing grounded task contract fields: " + ", ".join(missing),
                            "disposition_reason": "missing grounded task contract fields: " + ", ".join(missing),
                            "disposition_source": "autoresearch_reconciler",
                            "finding_state": "rejected",
                            "decision_state": "dismissed",
                            "delivery_state": "none",
                            "operator_action_required": False,
                        })
                        proposals.save_proposal(proposal)
                        summary["held_invalid_contract"] += 1
                        processed.append({"route": "held_invalid_contract", "proposal": proposal})
                        continue
                    task_id, created = _route_to_kanban(
                        conn,
                        proposal,
                        allow_create=summary["new_tasks"] < max_new,
                    )
                    if task_id is None:
                        proposal.update({
                            "status": "pooled",
                            "reconciled_at": _utc_now(),
                            "result": f"pooled by flood guard: max {max_new} new tasks per run",
                        })
                        proposals.save_proposal(proposal)
                        summary["pooled"] += 1
                        processed.append({"route": "pooled", "proposal": proposal})
                        continue
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

                proposal.update({
                    "status": "skipped",
                    "last_outcome": "rejected_below_intake_threshold",
                    "reconciled_at": _utc_now(),
                    "result": "detection-only: below intake threshold or missing an actionable verified diff",
                    "disposition_reason": "detection-only signal; not a real operator decision",
                    "disposition_source": "autoresearch_reconciler",
                    "finding_state": "rejected",
                    "decision_state": "dismissed",
                    "delivery_state": "none",
                    "operator_action_required": False,
                })
                proposals.save_proposal(proposal)
                summary["rejected_detection_only"] += 1
                processed.append({"route": "rejected_detection_only", "proposal": proposal})
            except Exception as exc:
                summary["errors"] += 1
                proposal["reconcile_error"] = f"{type(exc).__name__}: {exc}"
                proposals.save_proposal(proposal)
        if not dry_run:
            digest = _write_digest(processed)
            _write_last_reconcile(summary, digest)
        return summary
    finally:
        if own_conn and conn is not None:
            conn.close()
        _release_reconcile_lock(reconcile_lock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route Autoresearch proposals into the self-improvement flywheel.")
    parser.add_argument("--once", action="store_true", help="Run one explicit backlog-drain/reconcile pass.")
    parser.add_argument("--max-new-tasks", type=int, default=DEFAULT_MAX_NEW_TASKS)
    parser.add_argument("--min-task-severity", default=DEFAULT_MIN_TASK_SEVERITY)
    parser.add_argument("--dry-run", action="store_true", help="Classify the backlog without any side effect (preview the drain).")
    parser.add_argument(
        "--backfill-lifecycle", action="store_true",
        help="Backfill finding/decision/delivery state, task truth and target hashes instead of routing.",
    )
    parser.add_argument(
        "--cleanup-legacy", action="store_true",
        help="With --backfill-lifecycle, conservatively dispose stale legacy mutation/audit snapshots.",
    )
    args = parser.parse_args(argv)
    if args.cleanup_legacy and not args.backfill_lifecycle:
        parser.error("--cleanup-legacy requires --backfill-lifecycle")
    if args.backfill_lifecycle:
        with kb.connect() as conn:
            summary = proposals.backfill_lifecycle(
                dry_run=args.dry_run,
                conn=conn,
                cleanup_legacy=args.cleanup_legacy,
            )
    else:
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
