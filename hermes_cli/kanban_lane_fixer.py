"""Bounded handoff for lane-scope parks.

A lane-scope gate must keep its attribution verdict intact, but the corrective
work belongs to the lane that owns the violated paths.  This module mirrors the
conflict-park fixer policy without adding fork logic to upstream-owned
``kanban_db.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from hermes_cli.kanban_chain_status import (
    blocked_event_kind,
    is_integration_park,
    is_settled_fixer_card,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_fixer_scope as kfs
from hermes_cli import kanban_worktrees as kwt

_log = logging.getLogger(__name__)

LANE_FIXER_IDEM_PREFIX = "lane-scope-fixer:"
LANE_FIXER_DISPATCHED_EVENT = "lane_scope_fixer_dispatched"
LANE_FIXER_FOR_EVENT = "lane_scope_fixer_for"
LANE_FIXER_PARENT_RESUMED_EVENT = "lane_scope_fixer_parent_resumed"
LANE_SCOPE_ALLOWLISTED_EVENT = "lane_scope_allowlisted"
LANE_SCOPE_ALLOWLIST_WAIVER_EVENT = "lane_scope_allowlist_waiver"


def _lane_fingerprint(violating_paths: Iterable[str], expected_lane: str) -> str:
    canonical = {
        "expected_lane": str(expected_lane),
        "violating_paths": sorted({str(path) for path in violating_paths if str(path)}),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _event_payloads(
    conn: sqlite3.Connection, parent_id: str, fingerprint: str
) -> list[tuple[int, dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT created_at, payload FROM task_events
        WHERE task_id = ? AND kind = ?
        ORDER BY id ASC
        """,
        (parent_id, LANE_FIXER_DISPATCHED_EVENT),
    ).fetchall()
    matched: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("fingerprint") == fingerprint:
            matched.append((int(row["created_at"] or 0), payload))
    return matched


def _is_open_task(conn: sqlite3.Connection, task_id: object) -> bool:
    if not task_id:
        return False
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
    return (
        row is not None
        and row["status"] not in {"done", "archived", "failed", "cancelled"}
        and not is_settled_fixer_card(conn, str(task_id))
    )


def is_lane_scope_fixer_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether *task_id* is a lane fixer, based on its idempotency key."""
    row = conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return bool(row and str(row["idempotency_key"] or "").startswith(LANE_FIXER_IDEM_PREFIX))


def _payload_dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _lane_fingerprint_from_scope_payload(payload: dict[str, Any]) -> str:
    paths = payload.get("violating_paths") or []
    expected_lane = str(payload.get("expected_lane") or "").strip()
    if not paths or not expected_lane:
        return ""
    return _lane_fingerprint(paths, expected_lane)


def current_lane_scope_park_fingerprint(
    conn: sqlite3.Connection,
    parent_id: str,
    current_reason: str,
) -> str:
    """Fingerprint of the park that currently holds *parent_id*, or "".

    Mirrors ``kanban_db._resume_parent_for_completed_conflict_fixer`` binding
    resume to the *current* park context (Opus B4): prefer the latest
    structured ``worker_gate_blocked`` lane-scope payload; fall back to
    refusing when the reason is not a lane-scope integration park.
    """
    reason = str(current_reason or "")
    blocked = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1",
        (parent_id,),
    ).fetchone()
    if not is_integration_park(
        reason=reason,
        block_kind=blocked_event_kind(blocked["payload"] if blocked else None),
    ):
        return ""
    if "lane-scope" not in reason.lower():
        return ""
    rows = conn.execute(
        """
        SELECT payload FROM task_events
        WHERE task_id = ? AND kind = 'worker_gate_blocked'
        ORDER BY id DESC
        """,
        (parent_id,),
    ).fetchall()
    for row in rows:
        payload = _payload_dict(row["payload"])
        if payload.get("gate") != "lane_scope":
            continue
        fingerprint = _lane_fingerprint_from_scope_payload(payload)
        if fingerprint:
            return fingerprint
    return ""


def _resume_tip_for_fingerprint(
    conn: sqlite3.Connection, parent_id: str, fingerprint: str
) -> str:
    rows = conn.execute(
        """
        SELECT payload FROM task_events
        WHERE task_id = ? AND kind = ?
        ORDER BY id DESC
        """,
        (parent_id, LANE_FIXER_PARENT_RESUMED_EVENT),
    ).fetchall()
    for row in rows:
        payload = _payload_dict(row["payload"])
        if str(payload.get("fingerprint") or "") != fingerprint:
            continue
        tip = str(payload.get("resume_branch_tip") or "").strip()
        if tip:
            return tip
    return ""


def _paths_changed_since(
    repo_root: Any,
    tip: str,
    branch: str,
    paths: set[str],
) -> set[str]:
    """Return the subset of *paths* touched by commits after *tip* on *branch*."""
    if not tip or not branch or not paths:
        return set()
    try:
        # ``git diff A..B`` with *diff* is an endpoint comparison (same as
        # ``git diff A B``), not a commit-range walk. Net tree delta between
        # tip and branch tip — paths changed-and-reverted to identical content
        # do not appear.
        changed = {
            line.strip()
            for line in kwt._git(
                repo_root, "diff", "--name-only", f"{tip}..{branch}",
            ).splitlines()
            if line.strip()
        }
    except Exception:
        # Fail closed: unknown git state must not keep a permanent allowlist.
        return set(paths)
    return {path for path in paths if path in changed}


def _latest_lane_scope_park(
    conn: sqlite3.Connection,
    parent_id: str,
) -> tuple[str, str]:
    """Fingerprint and park-time branch tip of the newest lane-scope park."""
    rows = conn.execute(
        """
        SELECT payload FROM task_events
        WHERE task_id = ? AND kind = 'worker_gate_blocked'
        ORDER BY id DESC
        """,
        (parent_id,),
    ).fetchall()
    for row in rows:
        payload = _payload_dict(row["payload"])
        if payload.get("gate") != "lane_scope":
            continue
        fingerprint = _lane_fingerprint_from_scope_payload(payload)
        if fingerprint:
            return (
                fingerprint,
                str(payload.get("park_branch_tip") or "").strip(),
            )
    return "", ""


def _parent_worktree(conn: sqlite3.Connection, parent_id: str) -> Optional[Path]:
    """Provisioned chain worktree of *parent_id*, or ``None``.

    Attribution must run in the checkout that has the chain branch at
    ``HEAD``: ``kanban_fixer_scope._attributed_task_paths`` anchors its
    stamp-plausibility check on ``HEAD``, and the repo root handed to the
    lane gate is the main checkout, whose ``HEAD`` is the merge target.
    """
    row = conn.execute(
        "SELECT workspace_path FROM tasks WHERE id = ?",
        (parent_id,),
    ).fetchone()
    if row is None or not row["workspace_path"]:
        return None
    provisioned = kwt.split_provisioned_path(row["workspace_path"])
    if provisioned is None:
        return None
    return Path(str(provisioned[2]))


def _fixer_committed_paths(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    candidate_ref: str,
    paths: set[str],
) -> set[str]:
    """Subset of *paths* the fixer *child_id* is itself attributable for.

    Merge-safe by construction: attribution runs through the fork-owned
    ``kanban_fixer_scope._attributed_task_paths`` (pre-run..candidate SHA
    range first, per-run commit receipts second) rather than a
    ``git log --grep=kanban(<child>):`` subject convention. Nothing enforces
    that convention on a worker's commit message, and a receipt pointing at
    a MERGE commit reads as Unknown (``diff-tree`` prints no path for it), so
    both subject-shaped routes go blind exactly when a fixer lands its
    corrective work by merging its own branch — and the parent would then
    re-park forever on paths the fixer had in fact adopted.
    """
    if not child_id or not paths or not candidate_ref:
        return set()
    worktree = _parent_worktree(conn, parent_id)
    if worktree is None:
        return set()
    try:
        attributed = kfs._attributed_task_paths(
            conn,
            child_id,
            worktree,
            kwt,
            completion_metadata=None,
            candidate_ref=candidate_ref,
        )
    except Exception:
        return set()
    if not attributed:
        # ``None`` is the helper's Unknown answer, ``[]`` means the fixer
        # contributed nothing. Neither earns an allowlist.
        return set()
    landed = set(attributed)
    return {path for path in paths if path in landed}


def _operator_waived_paths(
    conn: sqlite3.Connection,
    parent_id: str,
    fingerprint: str,
    child_id: str,
    branch_tip: str,
    paths: set[str],
) -> set[str]:
    """Return paths covered by an explicit, attributable operator waiver."""
    rows = conn.execute(
        """
        SELECT payload FROM task_events
        WHERE task_id = ? AND kind = ?
        ORDER BY id DESC
        """,
        (parent_id, LANE_SCOPE_ALLOWLIST_WAIVER_EVENT),
    ).fetchall()
    waived: set[str] = set()
    for row in rows:
        payload = _payload_dict(row["payload"])
        if str(payload.get("fingerprint") or "") != fingerprint:
            continue
        if str(payload.get("child_id") or "") != child_id:
            continue
        if not str(payload.get("operator_id") or "").strip():
            continue
        if str(payload.get("branch_tip") or "").strip() != branch_tip:
            continue
        waived.update(
            str(path).strip()
            for path in payload.get("paths") or []
            if str(path).strip() in paths
        )
    return waived


def _record_allowlisted_paths(
    conn: sqlite3.Connection,
    parent_id: str,
    fingerprint: str,
    evidence: dict[str, dict[str, set[str]]],
) -> None:
    """Audit every earned allowlist on the board and in the log."""
    payloads: list[dict[str, Any]] = []
    for child_id, sources in evidence.items():
        for source, paths in sources.items():
            if not paths:
                continue
            payloads.append(
                {
                    "child_id": child_id,
                    "fingerprint": fingerprint,
                    "paths": sorted(paths),
                    "source": source,
                }
            )
    if not payloads:
        return
    with kb.write_txn(conn):
        for payload in payloads:
            kb._append_event(
                conn, parent_id, LANE_SCOPE_ALLOWLISTED_EVENT, payload,
            )
    for payload in payloads:
        _log.warning(
            "lane-scope allowlist parent=%s child=%s fingerprint=%s paths=%s",
            parent_id,
            payload["child_id"],
            fingerprint,
            ",".join(payload["paths"]),
        )


def allowlisted_paths_for_parent(
    conn: sqlite3.Connection,
    parent_id: str,
    *,
    violating_paths: Optional[Iterable[str]] = None,
    expected_lane: Optional[str] = None,
    repo_root: Any = None,
    branch: Optional[str] = None,
) -> set[str]:
    """Paths a completed lane-scope fixer *earned* for the current violation.

    Bound to the fingerprint of the violation being checked now (Opus B4/B5),
    not to every historical done fixer child. Permanent path allowlisting is
    refused: after a successful fixer resume, new parent commits on those
    paths re-arm the gate (retouch after ``resume_branch_tip``).

    A done fixer card is a *claim*, not evidence. Immunity has to be earned
    per child, per path, from one of exactly two sources:

    * ``fixer_commit`` -- the path is attributable to that fixer child
      (merge-safe, see :func:`_fixer_committed_paths`), and the completion
      hook has recorded the handoff tip, so later parent work stays
      distinguishable from the fixer's own;
    * ``operator_waiver`` -- an explicit, signed, tip-bound
      ``lane_scope_allowlist_waiver`` event names the path.

    Without either, a fixer that closed *without touching anything* would
    hand its parent a free pass over the exact paths that parked it. Every
    granted path is audited via :func:`_record_allowlisted_paths`.
    """
    want_fp = ""
    if violating_paths is not None and expected_lane:
        want_fp = _lane_fingerprint(violating_paths, expected_lane)
        if not want_fp:
            return set()

    rows = conn.execute(
        """
        SELECT payload FROM task_events
        WHERE task_id = ? AND kind = ?
        ORDER BY id ASC
        """,
        (parent_id, LANE_FIXER_DISPATCHED_EVENT),
    ).fetchall()
    candidates: dict[str, set[str]] = {}
    for row in rows:
        payload = _payload_dict(row["payload"])
        child_id = str(payload.get("child_id") or "").strip()
        if not child_id:
            continue
        fingerprint = str(payload.get("fingerprint") or "").strip()
        if want_fp and fingerprint != want_fp:
            continue
        child = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child_id,)
        ).fetchone()
        if child is None or child["status"] != "done":
            continue
        child_paths = candidates.setdefault(child_id, set())
        for path in payload.get("violating_paths") or []:
            text = str(path).strip()
            if text:
                child_paths.add(text)
    allow = set().union(*candidates.values()) if candidates else set()
    if not allow:
        return set()

    # Without a current-violation fingerprint (legacy callers/tests), keep the
    # done-fixer path set — but production always passes violating_paths.
    if not want_fp:
        return set(allow)

    resume_tip = _resume_tip_for_fingerprint(conn, parent_id, want_fp)
    # Only the fingerprint of the newest park gates the lookup; its
    # ``park_branch_tip`` is no longer the attribution basis — each fixer
    # child is bounded by its own pre-run stamp instead of a shared range.
    latest_fp, _park_tip = _latest_lane_scope_park(conn, parent_id)
    if not resume_tip and latest_fp != want_fp:
        # Done fixer for this fp but no matching current park and no resume:
        # refuse permanent allowlist (Opus B5).
        return set()

    # Production always supplies git context. Legacy direct callers keep the
    # exact fingerprint binding, but cannot earn new audit-backed immunity.
    if repo_root is None or not branch:
        return allow
    try:
        endpoint_tip = resume_tip or str(
            kwt._git(repo_root, "rev-parse", branch)
        ).strip()
    except Exception:
        # Fail closed: an unreadable branch is not evidence of a fix.
        return set()

    evidence: dict[str, dict[str, set[str]]] = {}
    for child_id, child_paths in candidates.items():
        committed = _fixer_committed_paths(
            conn, parent_id, child_id, endpoint_tip, child_paths,
        )
        waived = _operator_waived_paths(
            conn, parent_id, want_fp, child_id, endpoint_tip, child_paths,
        )
        # A fixer commit is authoritative only after the completion hook has
        # recorded the handoff tip. Without that boundary, later parent work
        # cannot be distinguished from the fixer's own changes.
        if committed and resume_tip:
            evidence.setdefault(child_id, {})["fixer_commit"] = committed
        if waived:
            evidence.setdefault(child_id, {})["operator_waiver"] = waived

    earned = {
        path
        for sources in evidence.values()
        for source_paths in sources.values()
        for path in source_paths
    }
    if resume_tip and earned:
        # New parent commits after resume on allowlisted paths re-arm the
        # gate (Opus B5) — the fixer only covered the original park.
        committed_paths = {
            path
            for sources in evidence.values()
            for path in sources.get("fixer_commit", set())
        }
        retouched = _paths_changed_since(
            repo_root, resume_tip, branch, committed_paths,
        )
        if retouched:
            for sources in evidence.values():
                sources.get("fixer_commit", set()).difference_update(retouched)
            earned = {
                path
                for sources in evidence.values()
                for source_paths in sources.values()
                for path in source_paths
            }
    if not earned:
        return set()

    _record_allowlisted_paths(conn, parent_id, want_fp, evidence)
    return earned


def _branch_tip_for_parent(conn: sqlite3.Connection, parent_id: str) -> str:
    row = conn.execute(
        "SELECT workspace_path FROM tasks WHERE id = ?",
        (parent_id,),
    ).fetchone()
    if row is None or not row["workspace_path"]:
        return ""
    provisioned = kwt.split_provisioned_path(row["workspace_path"])
    if provisioned is None:
        return ""
    repo_root, root_id, _wt = provisioned
    try:
        return str(
            kwt._git(repo_root, "rev-parse", kwt.chain_branch(root_id))
        ).strip()
    except Exception:
        return ""


def resume_parent_for_completed_lane_fixer(
    conn: sqlite3.Connection,
    child_id: str,
) -> bool:
    """CAS-resume the lane-scope park this successful fixer still owns.

    Shape mirrors ``kanban_db._resume_parent_for_completed_conflict_fixer`` but
    lives here (fork-owned) and matches on ``lane_scope_fixer_for`` events.
    The fingerprint is verified against the park that holds the parent *now*
    (Opus B4), not merely against any historical dispatch of this child.
    """
    marker = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = ? ORDER BY id DESC LIMIT 1",
        (child_id, LANE_FIXER_FOR_EVENT),
    ).fetchone()
    if marker is None:
        return False
    payload = _payload_dict(marker["payload"])
    parent_id = str(payload.get("parent_id") or "").strip()
    expected_fingerprint = str(payload.get("fingerprint") or "").strip()
    if not parent_id or not expected_fingerprint:
        return False

    # Resolve tip before the write txn so git never holds the board lock
    # (Opus R2-6: only the git probe stays outside; fingerprint checks move in).
    resume_tip = _branch_tip_for_parent(conn, parent_id)

    with kb.write_txn(conn):
        parent = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (parent_id,),
        ).fetchone()
        blocked = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
        current_reason = ""
        if blocked is not None:
            current_reason = str(
                _payload_dict(blocked["payload"]).get("reason") or ""
            )
        if (
            parent is None
            or parent["status"] != "blocked"
            or not is_integration_park(
                reason=current_reason,
                block_kind=blocked_event_kind(
                    blocked["payload"] if blocked else None,
                ),
            )
            or "lane-scope" not in current_reason.lower()
        ):
            return False

        # Bind to the CURRENT park fingerprint (same contract as conflict fixer).
        current_fp = current_lane_scope_park_fingerprint(
            conn, parent_id, current_reason,
        )
        if not current_fp or current_fp != expected_fingerprint:
            return False

        # Dispatch event must still name this child for the same fingerprint.
        dispatched = conn.execute(
            """
            SELECT payload FROM task_events
            WHERE task_id = ? AND kind = ?
            ORDER BY id DESC
            """,
            (parent_id, LANE_FIXER_DISPATCHED_EVENT),
        ).fetchall()
        fingerprint_ok = False
        for row in dispatched:
            d_payload = _payload_dict(row["payload"])
            if (
                str(d_payload.get("child_id") or "") == child_id
                and str(d_payload.get("fingerprint") or "")
                == expected_fingerprint
            ):
                fingerprint_ok = True
                break
        if not fingerprint_ok:
            return False

        undone_parent = conn.execute(
            "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
            (parent_id,),
        ).fetchone()
        new_status = "todo" if undone_parent else "ready"
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, block_kind = NULL, "
            "block_recurrences = 0 WHERE id = ? AND status = 'blocked'",
            (new_status, parent_id),
        )
        if cur.rowcount != 1:
            return False
        kb._append_event(
            conn,
            parent_id,
            LANE_FIXER_PARENT_RESUMED_EVENT,
            {
                "child_id": child_id,
                "fingerprint": expected_fingerprint,
                "status": new_status,
                "resume_branch_tip": resume_tip or None,
            },
        )
        kb._append_event(
            conn,
            parent_id,
            "unblocked",
            {"status": new_status, "source": "lane_scope_fixer_completion"},
        )
        return True


def handle_task_completed(task_id: str, **kwargs: object) -> None:
    """Lifecycle observer: release a parked parent when its lane fixer finishes."""
    board = kwargs.get("board")
    board_slug = str(board) if board else None
    try:
        with kb.connect_closing(board=board_slug) as conn:
            resume_parent_for_completed_lane_fixer(conn, task_id)
    except Exception:
        _log.exception(
            "lane-scope fixer parent resume failed for %s", task_id,
        )


def register_lifecycle_hooks() -> None:
    """Register the lane-scope fixer resume observer (idempotent)."""
    from hermes_cli.plugins import register_hook_once

    register_hook_once("kanban_task_completed", handle_task_completed)


def ensure_lifecycle_hooks_registered() -> None:
    """Best-effort self-registration without editing ``kanban_db``/lifecycle.py.

    ``register_hook_once`` is already idempotent and re-registers after a
    plugin-manager force-reset, so callers may invoke this freely.
    """
    try:
        register_lifecycle_hooks()
    except Exception:
        _log.debug("lane-scope fixer lifecycle registration failed", exc_info=True)


def _fixer_body(
    *,
    parent_id: str,
    branch: str,
    violating_paths: list[str],
    expected_lane: str,
    attempt: int,
) -> str:
    paths = "\n".join(f"- `{path}`" for path in violating_paths)
    return (
        f"## Lane-scope fixer for `{parent_id}`\n\n"
        f"The parent task was parked because its attributed diff includes paths owned by "
        f"lane `{expected_lane}`. Work on the existing chain branch `{branch}`.\n\n"
        "The work is **already committed** on this branch. Take it over and complete it "
        "in the correct lane; do not rebuild it and do not revert it.\n\n"
        f"Violating paths (verbatim):\n{paths}\n\n"
        f"Attempt {attempt}/{kb.CONFLICT_FIXER_MAX_ATTEMPTS}."
    )


def _escalate_exhausted(
    conn: sqlite3.Connection,
    parent: sqlite3.Row,
    *,
    fingerprint: str,
    attempts: int,
    now: int,
) -> None:
    escalated = kb._park_stall_once(
        conn,
        parent,
        stall_class=kb.INTEGRATION_PARKED_STALL_CLASS,
        reason="lane-scope fixer budget exhausted",
        evidence={
            "route": "lane_scope_fixer",
            "fingerprint": fingerprint,
            "attempts": attempts,
            "limit": kb.CONFLICT_FIXER_MAX_ATTEMPTS,
        },
        now=now,
    )
    if not escalated and not kb._has_operator_escalation(conn, parent["id"]):
        # The lane gate calls us before its caller performs the final blocked
        # transition.  Preserve the conflict-fixer escalation semantics even
        # in that short running/ready window.
        kb._emit_operator_escalation(
            conn,
            str(parent["id"]),
            None,
            "lane_scope_fixer_budget_exhausted",
            "lane-scope fixer budget exhausted",
        )


def maybe_route_lane_scope_fixer(
    conn: sqlite3.Connection,
    parent: sqlite3.Row,
    *,
    violating_paths: Iterable[str],
    expected_lane: str,
    now: Optional[int] = None,
) -> Optional[str]:
    """Create one budgeted fixer for a lane-scope park, or return ``None``.

    Routing is fail-soft: callers intentionally ignore this return value so a
    failure here can never prevent the original lane-scope park.
    """
    ensure_lifecycle_hooks_registered()
    parent_id = str(parent["id"])
    if is_lane_scope_fixer_task(conn, parent_id):
        return None

    paths = sorted({str(path) for path in violating_paths if str(path)})
    if not paths or not expected_lane:
        return None
    now = int(time.time()) if now is None else int(now)
    fingerprint = _lane_fingerprint(paths, expected_lane)
    dispatched = _event_payloads(conn, parent_id, fingerprint)
    if any(_is_open_task(conn, payload.get("child_id")) for _, payload in dispatched):
        return None

    attempts = len(dispatched)
    if attempts >= kb.CONFLICT_FIXER_MAX_ATTEMPTS:
        _escalate_exhausted(
            conn, parent, fingerprint=fingerprint, attempts=attempts, now=now
        )
        return None
    if dispatched and now - dispatched[-1][0] < kb.CONFLICT_FIXER_BACKOFF_SECONDS:
        return None

    provisioned = kwt.split_provisioned_path(parent["workspace_path"])
    if provisioned is None:
        return None
    _, root_id, _ = provisioned
    branch = kwt.chain_branch(root_id)
    attempt = attempts + 1
    idempotency_key = f"{LANE_FIXER_IDEM_PREFIX}{parent_id}:{fingerprint}:{attempt}"
    try:
        child_id = kb.create_task(
            conn,
            title=f"Lane-scope fixer for {parent_id}",
            body=_fixer_body(
                parent_id=parent_id,
                branch=branch,
                violating_paths=paths,
                expected_lane=expected_lane,
                attempt=attempt,
            ),
            assignee=expected_lane,
            created_by="lane-scope-fixer",
            workspace_kind="dir",
            workspace_path=str(parent["workspace_path"]),
            tenant=parent["tenant"],
            priority=int(parent["priority"] or 0),
            idempotency_key=idempotency_key,
            max_runtime_seconds=kb.CONFLICT_FIXER_MAX_RUNTIME_SECONDS,
            max_retries=1,
            kind="code",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                parent_id,
                LANE_FIXER_DISPATCHED_EVENT,
                {
                    "parent_id": parent_id,
                    "child_id": child_id,
                    "fingerprint": fingerprint,
                    "attempt": attempt,
                    "limit": kb.CONFLICT_FIXER_MAX_ATTEMPTS,
                    "expected_lane": expected_lane,
                    "violating_paths": paths,
                },
            )
            kb._append_event(
                conn,
                child_id,
                LANE_FIXER_FOR_EVENT,
                {
                    "parent_id": parent_id,
                    "fingerprint": fingerprint,
                    "attempt": attempt,
                    "expected_lane": expected_lane,
                    "violating_paths": paths,
                },
            )
    except Exception:
        _log.warning("lane-scope fixer creation failed for %s", parent_id, exc_info=True)
        return None
    return child_id
