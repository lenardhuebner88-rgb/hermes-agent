"""Fork-owned "is it landed?" truth surface for kanban tasks.

The board marks a task ``done`` when the worker finishes; whether the work
is on the merge target *today* is not a property of the task.  Integration
witnesses exist as append-only events (``integration_merged``,
``INTEGRATOR_VERIFIED``) but are written by only two of the done-path
families and are never invalidated — see
``docs/kanban/2026-07-26-landed-state-design.md`` for the full census and
the falsification of the "no event exists" assumption.

This module derives the integration state from the durable evidence — the
witness events, ``tasks.branch_name`` and git itself — using the
revert-safe criteria the fork already proved in
``kanban_worktrees._recover_missing_branch_integration`` (merge
reachability + content drift + explicit-revert grep) plus ``git cherry``
patch equivalence for manually re-applied work.  It materializes results
into the fork-owned ``task_landed_state`` table so one plain-SQL query
answers "done AND on main today" without JSON parsing.

Design contract (load-bearing):

* **Read side only.**  Nothing here writes ``tasks`` or ``task_events``;
  no done-path calls this module.  It cannot freeze a chain because no
  transition consults it — rows merely go stale if nobody sweeps.
* **Git is the truth, events are hints.**  When a park record and the
  content check disagree, the content check wins and the disagreement is
  flagged in ``detail`` for a human.
* **``unknown`` is not ``not_landed``.**  Most done tasks took a git-blind
  path with no witness; declaring all of them "not landed" would make the
  signal noise.  ``unknown`` means "no evidence either way".

The consumer is ``scripts/kanban_landed_check.py`` (per-task query, stale
sweep, materialization).  The schema follows the fork precedent set by
``outcome_verification.ensure_schema``: the module owns its table and
ensures it lazily, so no upstream-owned file is edited.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# states
# ---------------------------------------------------------------------------

LANDED_ACTIVE = "landed_active"
LANDED_DRIFTED = "landed_drifted"
NOT_LANDED = "not_landed"
NA_COMMITLESS = "na_commitless"
UNKNOWN = "unknown"

STATES = frozenset(
    {LANDED_ACTIVE, LANDED_DRIFTED, NOT_LANDED, NA_COMMITLESS, UNKNOWN}
)

#: Event kinds that record a positive integration (merge happened).
POSITIVE_WITNESS_KINDS = (
    "integration_merged",
    "INTEGRATOR_VERIFIED",
    "integration_retry_succeeded",
)
#: Event kind recording that a merge was reverted by the post-merge gate.
PARK_WITNESS_KIND = "integration_parked"

_WITNESS_KINDS = POSITIVE_WITNESS_KINDS + (PARK_WITNESS_KIND,)

_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_COMMIT_KEYS = ("merge_commit", "candidate_commit", "commit")
_MAX_DRIFT_FILES = 20
_MAX_DIFF_FILES = 200
_MAX_UNLANDED_COMMITS = 20
_MERGE_SCAN_LIMIT = 400


# ---------------------------------------------------------------------------
# schema (fork-owned table; additive, idempotent)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_landed_state (
    task_id      TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    merge_commit TEXT,
    target       TEXT,
    branch       TEXT,
    detail       TEXT,
    verified_at  INTEGER NOT NULL
)
"""
_STATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_task_landed_state_state "
    "ON task_landed_state(state)"
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the materialization table if missing. Idempotent by SQL.

    The existence check keeps this a pure read on a connection where the
    table already exists — so read-only consumers (``?mode=ro`` against
    the live board) may call it without tripping the write guard."""
    if _table_exists(conn, "task_landed_state"):
        return
    conn.execute(_SCHEMA)
    conn.execute(_STATE_INDEX)


# ---------------------------------------------------------------------------
# git plumbing (self-contained; read-only commands only)
# ---------------------------------------------------------------------------

def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *[str(a) for a in args]],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def _git(repo: Path, *args: str) -> Optional[str]:
    proc = _run_git(repo, *args)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_lines(repo: Path, *args: str) -> Optional[List[str]]:
    out = _git(repo, *args)
    if out is None:
        return None
    return [line for line in out.splitlines() if line.strip()]


def _branch_resolves(repo: Path, branch: str) -> bool:
    return (
        _git(repo, "rev-parse", "--verify", "-q", f"{branch}^{{commit}}")
        is not None
    )


def _is_ancestor(repo: Path, commit: str, target: str) -> bool:
    return _run_git(repo, "merge-base", "--is-ancestor", commit, target).returncode == 0


def _ahead_count(repo: Path, target: str, branch: str) -> Optional[int]:
    out = _git(repo, "rev-list", "--count", f"{target}..{branch}")
    return int(out) if out is not None and out.isdigit() else None


def _merge_reaching_branch(
    repo: Path, branch: str, target: str, limit: int = _MERGE_SCAN_LIMIT
) -> Optional[str]:
    """Most recent first-parent merge of *target* whose second-parent
    lineage contains *branch* (i.e. the merge that landed the branch)."""
    merges = _git_lines(
        repo, "rev-list", "--first-parent", "--merges", "-n", str(limit), target
    )
    if not merges:
        return None
    for merge in merges:
        second = _git(repo, "rev-parse", "-q", "--verify", f"{merge}^2")
        if not second:
            continue
        if _run_git(repo, "merge-base", "--is-ancestor", branch, second).returncode == 0:
            return merge
    return None


def _changed_files(repo: Path, left: str, right: str) -> List[str]:
    return _git_lines(repo, "diff", "--name-only", left, right) or []


def _diff_is_clean(
    repo: Path, left: str, right: str, files: List[str]
) -> Optional[bool]:
    """True when *files* are identical in *left* and *right* trees.

    ``None`` means the check itself failed (fail closed: callers treat an
    unverifiable content check as drift, matching the fork's post-merge
    gate posture — see ``_run_gate_in_validation_worktree``)."""
    proc = _run_git(
        repo, "diff", "--quiet", left, right, "--", *files[:_MAX_DIFF_FILES]
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _explicit_revert_commits(
    repo: Path, merge_commit: str, target: str
) -> List[str]:
    """Commits on *target* whose message names *merge_commit* — the
    signature of ``git revert`` (same heuristic as
    ``kanban_worktrees._revert_commits_for_merge``)."""
    lines = _git_lines(repo, "log", "--format=%H", f"--grep={merge_commit}", target)
    return [line for line in (lines or []) if line != merge_commit]


def _cherry_signs(
    repo: Path, target: str, branch: str
) -> Optional[List[Tuple[str, str]]]:
    """``git cherry`` patch-equivalence: ``+`` = commit has no equivalent
    on target, ``-`` = an equivalent patch is already on target."""
    lines = _git_lines(repo, "cherry", target, branch)
    if lines is None:
        return None
    return [
        (line[0], line[1:].strip())
        for line in lines
        if len(line) > 2 and line[0] in "+-"
    ]


# ---------------------------------------------------------------------------
# witness extraction
# ---------------------------------------------------------------------------

def _payload(raw: Any) -> Dict[str, Any]:
    try:
        loaded = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _witness_info(
    conn: sqlite3.Connection, task_id: str
) -> Dict[str, Any]:
    """Collect integration-witness facts from ``task_events``.

    Returns ``positive_commit``/``positive_target`` from the latest
    positive witness, plus park bookkeeping: ``any_parked``,
    ``park_after_positive`` (a park recorded after the latest positive
    witness) and ``park_commit``/``park_reason`` from the latest park."""
    placeholders = ",".join("?" for _ in _WITNESS_KINDS)
    rows = conn.execute(
        f"SELECT id, kind, payload FROM task_events WHERE task_id = ? "
        f"AND kind IN ({placeholders}) ORDER BY id",
        (task_id, *_WITNESS_KINDS),
    ).fetchall()

    info: Dict[str, Any] = {
        "positive_commit": None,
        "positive_target": None,
        "positive_kind": None,
        "any_parked": False,
        "park_after_positive": False,
        "park_commit": None,
        "park_reason": None,
    }
    positive_id = -1
    latest_park_id = -1
    for row in rows:
        row_id = int(row[0])
        kind = str(row[1])
        payload = _payload(row[2])
        if kind in POSITIVE_WITNESS_KINDS:
            commit = ""
            for key in _COMMIT_KEYS:
                commit = str(payload.get(key) or "").strip()
                if commit:
                    break
            if commit:
                info["positive_commit"] = commit
                info["positive_target"] = str(payload.get("target") or "").strip() or None
                info["positive_kind"] = kind
                positive_id = row_id
        elif kind == PARK_WITNESS_KIND:
            info["any_parked"] = True
            if row_id > latest_park_id:
                latest_park_id = row_id
                for key in _COMMIT_KEYS:
                    commit = str(payload.get(key) or "").strip()
                    if commit:
                        info["park_commit"] = commit
                        break
                info["park_reason"] = str(payload.get("reason") or "").strip() or None
    info["park_after_positive"] = bool(
        info["any_parked"] and positive_id >= 0 and latest_park_id > positive_id
    )
    return info


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

@dataclass
class LandedState:
    """Derived integration state for one task. ``detail`` is JSON-able."""

    task_id: str
    state: str
    merge_commit: Optional[str] = None
    target: Optional[str] = None
    branch: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def row(self, verified_at: int) -> Tuple[Any, ...]:
        return (
            self.task_id,
            self.state,
            self.merge_commit,
            self.target,
            self.branch,
            json.dumps(self.detail, sort_keys=True),
            int(verified_at),
        )


def _classify_merge_commit(
    repo: Path, merge_commit: str, target: str
) -> Tuple[str, Dict[str, Any]]:
    """Revert-safe verdict for a recorded merge commit on *target*."""
    if not _SHA_RE.fullmatch(merge_commit):
        return UNKNOWN, {
            "evidence": "witness_commit_not_40hex",
            "commit": merge_commit[:64],
        }
    if not _is_ancestor(repo, merge_commit, target):
        return LANDED_DRIFTED, {
            "evidence": "merge_commit_not_ancestor",
            "target": target,
        }
    changed = _changed_files(repo, f"{merge_commit}^1", merge_commit)
    if not changed:
        return LANDED_ACTIVE, {"evidence": "content_active", "checked_files": 0}
    clean = _diff_is_clean(repo, merge_commit, target, changed)
    if clean is None:
        # Fail closed: an unverifiable content check reports drift with an
        # honest marker rather than claiming the work is active.
        return LANDED_DRIFTED, {
            "evidence": "content_check_error",
            "checked_files": len(changed),
        }
    if clean:
        return LANDED_ACTIVE, {
            "evidence": "content_active",
            "checked_files": len(changed),
        }
    drifted = _git_lines(
        repo,
        "diff",
        "--name-only",
        merge_commit,
        target,
        "--",
        *changed[:_MAX_DIFF_FILES],
    ) or []
    reverts = _explicit_revert_commits(repo, merge_commit, target)
    return LANDED_DRIFTED, {
        "evidence": "content_drifted",
        "explicit_reverted": bool(reverts),
        "revert_commits": reverts[:5],
        "drifted_files": drifted[:_MAX_DRIFT_FILES],
        "checked_files": len(changed),
    }


def _classify_resolved_branch(
    repo: Path, branch: str, target: str
) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Revert-safe verdict from a branch ref that still exists.

    Returns ``(state, merge_commit_if_any, detail)``."""
    ahead = _ahead_count(repo, target, branch)
    if ahead is None:
        return UNKNOWN, None, {"evidence": "ahead_count_failed", "branch": branch}
    if ahead > 0:
        signs = _cherry_signs(repo, target, branch)
        if signs is None:
            return UNKNOWN, None, {"evidence": "cherry_failed", "ahead": ahead}
        unlanded = [sha for sign, sha in signs if sign == "+"]
        if unlanded:
            return NOT_LANDED, None, {
                "evidence": "commits_not_patch_equivalent",
                "ahead": ahead,
                "unlanded_commits": unlanded[:_MAX_UNLANDED_COMMITS],
            }
        return LANDED_ACTIVE, None, {
            "evidence": "patch_equivalent",
            "ahead": ahead,
            "commits": len(signs),
        }
    merge = _merge_reaching_branch(repo, branch, target)
    if merge:
        state, detail = _classify_merge_commit(repo, merge, target)
        detail.setdefault("via", "merge_reaching_branch")
        return state, merge, detail
    return LANDED_ACTIVE, None, {"evidence": "ancestor_no_merge_ff_or_rebase"}


def classify_task(
    conn: sqlite3.Connection,
    repo: Path | str,
    task_id: str,
    *,
    target: str = "main",
) -> LandedState:
    """Derive the integration state of *task_id* against *repo*.

    Pure read: touches ``tasks``/``task_events`` read-only and runs
    read-only git commands.  Does not ensure the materialization table
    (callers that persist use :func:`materialize`/:func:`sweep`)."""
    repo = Path(repo)
    row = conn.execute(
        "SELECT status, branch_name FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(task_id)
    branch = str(row[1] or "").strip() or None
    info = _witness_info(conn, task_id)
    effective_target = info["positive_target"] or target

    def _park_strengthen(state: str, detail: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        if state == LANDED_DRIFTED and info["any_parked"] and not detail.get(
            "explicit_reverted"
        ):
            detail["explicit_reverted"] = True
            detail["revert_evidence"] = "integration_parked_event"
        if info["park_reason"]:
            detail.setdefault("last_integration_park", info["park_reason"])
        return state, detail

    # Ladder 1: a positive integration witness exists.
    commit = info["positive_commit"]
    if commit:
        state, detail = _classify_merge_commit(repo, commit, effective_target)
        state, detail = _park_strengthen(state, detail)
        return LandedState(
            task_id=task_id,
            state=state,
            merge_commit=commit,
            target=effective_target,
            branch=branch,
            detail=detail,
        )

    # Ladder 1b: a park record is the only witness — it documents a merge
    # that the gate reverted.  Check its commit against git.
    park_commit = info["park_commit"]
    if park_commit and _SHA_RE.fullmatch(park_commit):
        state, detail = _classify_merge_commit(repo, park_commit, effective_target)
        if state == LANDED_DRIFTED:
            detail.setdefault("explicit_reverted", True)
            detail.setdefault("revert_evidence", "integration_parked_event")
        else:
            # Git says active despite the park record — git wins, flag it.
            detail["park_record_disagrees_with_git"] = True
        if info["park_reason"]:
            detail.setdefault("last_integration_park", info["park_reason"])
        return LandedState(
            task_id=task_id,
            state=state,
            merge_commit=park_commit,
            target=effective_target,
            branch=branch,
            detail=detail,
        )

    # Ladder 2: no witness, but the branch ref still resolves.
    if branch and _branch_resolves(repo, branch):
        state, merge, detail = _classify_resolved_branch(repo, branch, effective_target)
        state, detail = _park_strengthen(state, detail)
        return LandedState(
            task_id=task_id,
            state=state,
            merge_commit=merge,
            target=effective_target,
            branch=branch,
            detail=detail,
        )

    # Ladder 3/4: nothing to verify with.
    if branch:
        return LandedState(
            task_id=task_id,
            state=UNKNOWN,
            target=effective_target,
            branch=branch,
            detail={"reason": "branch_ref_gone_no_witness"},
        )
    return LandedState(
        task_id=task_id,
        state=NA_COMMITLESS,
        target=effective_target,
        detail={"reason": "no_branch_no_witness"},
    )


# ---------------------------------------------------------------------------
# materialization + sweep (the only writers — to the fork-owned table)
# ---------------------------------------------------------------------------

def _record(
    conn: sqlite3.Connection, state: LandedState, verified_at: int
) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO task_landed_state "
        "(task_id, state, merge_commit, target, branch, detail, verified_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        state.row(verified_at),
    )
    conn.commit()


def materialize(
    conn: sqlite3.Connection,
    repo: Path | str,
    task_ids: List[str],
    *,
    target: str = "main",
    now: Optional[int] = None,
) -> List[LandedState]:
    """Classify each task and persist the result. Returns the states."""
    verified_at = int(now if now is not None else time.time())
    results: List[LandedState] = []
    for task_id in task_ids:
        state = classify_task(conn, repo, task_id, target=target)
        _record(conn, state, verified_at)
        results.append(state)
    return results


def sweep(
    conn: sqlite3.Connection,
    repo: Path | str,
    *,
    target: str = "main",
    statuses: Tuple[str, ...] = ("done",),
    limit: Optional[int] = None,
    now: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Re-verify done tasks that carry a branch or an integration witness
    and persist fresh states.  Returns one row per task whose state is new
    or changed: ``{"task_id", "old", "new"}``.

    This is the watcher core.  The cron job around it is a draft — see the
    design doc; activation is an operator decision."""
    verified_at = int(now if now is not None else time.time())
    placeholders = ",".join("?" for _ in statuses)
    witness_ph = ",".join("?" for _ in _WITNESS_KINDS)
    sql = (
        f"SELECT DISTINCT t.id FROM tasks t WHERE t.status IN ({placeholders}) "
        f"AND ((t.branch_name IS NOT NULL AND t.branch_name != '') "
        f"OR t.id IN (SELECT task_id FROM task_events "
        f"WHERE kind IN ({witness_ph}))) ORDER BY t.completed_at DESC"
    )
    params: Tuple[Any, ...] = (*statuses, *_WITNESS_KINDS)
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, int(limit))
    candidates = [row[0] for row in conn.execute(sql, params).fetchall()]

    ensure_schema(conn)
    existing = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT task_id, state FROM task_landed_state"
        ).fetchall()
    }
    transitions: List[Dict[str, Any]] = []
    for task_id in candidates:
        state = classify_task(conn, repo, task_id, target=target)
        _record(conn, state, verified_at)
        old = existing.get(task_id)
        if old != state.state:
            transitions.append(
                {"task_id": task_id, "old": old, "new": state.state}
            )
    return transitions


def list_disagreements(
    conn: sqlite3.Connection,
    *,
    states: Tuple[str, ...] = (LANDED_DRIFTED, NOT_LANDED),
) -> List[Dict[str, Any]]:
    """Plain-SQL consumer query: materialized states that disagree with a
    naive reading of ``status='done'``.  No JSON parsing required on the
    hot path (``detail`` is returned raw for optional rendering).
    Returns [] when the table was never materialized."""
    if not _table_exists(conn, "task_landed_state"):
        return []
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"SELECT s.task_id, t.title, t.status, s.state, s.merge_commit, "
        f"s.target, s.branch, s.detail, s.verified_at "
        f"FROM task_landed_state s JOIN tasks t ON t.id = s.task_id "
        f"WHERE s.state IN ({placeholders}) ORDER BY s.verified_at DESC",
        states,
    ).fetchall()
    return [
        {
            "task_id": row[0],
            "title": row[1],
            "task_status": row[2],
            "state": row[3],
            "merge_commit": row[4],
            "target": row[5],
            "branch": row[6],
            "detail": row[7],
            "verified_at": row[8],
        }
        for row in rows
    ]
