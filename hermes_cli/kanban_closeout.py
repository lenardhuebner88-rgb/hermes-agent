"""Durable closeout outbox for terminal Kanban tasks.

The task status and the delivery status are deliberately separate.  Callers
append ``closeout_pending`` in the same transaction as their terminal task
transition, then call :func:`process_closeout` after commit or let
:func:`closeout_sweep` recover it later.

Release is at-most-once by design.  A committed ``closeout_release_started``
without a terminal acknowledgement is ambiguous: the external side effect may
already have happened, so recovery records ``closeout_release_ambiguous`` and
never invokes the release runner again.  Receipt writes are safe to retry
because they atomically replace one deterministic file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


CLOSEOUT_PENDING = "closeout_pending"
CLOSEOUT_CLAIMED = "closeout_claimed"
CLOSEOUT_CLAIM_FINISHED = "closeout_claim_finished"
CLOSEOUT_RELEASE_STARTED = "closeout_release_started"
CLOSEOUT_RELEASE_WAITING = "closeout_release_waiting"
CLOSEOUT_RELEASE_COMPLETE = "closeout_release_complete"
CLOSEOUT_RELEASE_NOT_REQUIRED = "closeout_release_not_required"
CLOSEOUT_RELEASE_HELD = "closeout_release_held"
CLOSEOUT_RELEASE_AMBIGUOUS = "closeout_release_ambiguous"
CLOSEOUT_RECEIPT_WRITTEN = "closeout_receipt_written"
CLOSEOUT_RECEIPT_FAILED = "closeout_receipt_failed"
CLOSEOUT_DELIVERED = "closeout_delivered"

_RELEASE_SUCCESS_KINDS = {
    CLOSEOUT_RELEASE_COMPLETE,
    CLOSEOUT_RELEASE_NOT_REQUIRED,
}
_RELEASE_TERMINAL_KINDS = _RELEASE_SUCCESS_KINDS | {
    CLOSEOUT_RELEASE_HELD,
    CLOSEOUT_RELEASE_AMBIGUOUS,
}
_COMPLETE_OUTCOMES = {
    "complete",
    "completed",
    "deployed",
    "released",
    "success",
    "succeeded",
}
_NOT_REQUIRED_OUTCOMES = {
    "no_op",
    "noop",
    "not_applicable",
    "not_required",
    "skipped",
}

AUTO_RECEIPT_DEFAULT_DIR = Path(
    "/home/piet/vault/03-Agents/Hermes/receipts/auto"
)
DEFAULT_CLOSEOUT_LEASE_SECONDS = 1800
CLOSEOUT_SPAWN_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class ReceiptArtifact:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class CloseoutClaim:
    task_id: str
    pending_event_id: int
    token: str
    lease_until: int


@dataclass(frozen=True)
class CloseoutReceipt:
    task_id: str
    title: str
    assignee: str
    task_status: str
    board: str
    summary: str
    result: str
    release_state: str
    release_payload: dict[str, Any]


@dataclass(frozen=True)
class CloseoutResult:
    task_id: str
    state: str
    release_state: Optional[str] = None
    receipt_path: Optional[str] = None
    delivered: bool = False
    error: Optional[str] = None


ReleaseRunner = Callable[[Any, str], Any]
ReceiptWriter = Callable[[CloseoutReceipt], ReceiptArtifact]


def _kb():
    # Lazy import keeps this module usable from kanban_db.complete_task without
    # introducing an import cycle at module import time.
    from hermes_cli import kanban_db

    return kanban_db


def _closeout_unit_name(task_id: str, board: Optional[str]) -> str:
    raw = f"{board or 'current'}-{task_id}"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)
    if len(safe) > 180:
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:167]}-{suffix}"
    return f"hermes-kanban-closeout-{safe}"


def spawn_closeout_unit(
    task_id: str,
    board: Optional[str] = None,
    *,
    runner: Optional[Callable[..., Any]] = None,
    hermes_bin: Optional[str] = None,
) -> dict[str, Any]:
    """Launch one restart-safe closeout process in a stable transient unit.

    This launcher deliberately does not claim the outbox row. The detached
    ``--inline`` process claims only after systemd accepted the unit, so launch
    failures remain immediately retryable.
    """

    repo_root = Path(__file__).resolve().parent.parent
    bin_path = (
        hermes_bin
        or os.environ.get("HERMES_BIN")
        or shutil.which("hermes")
        or "hermes"
    )
    systemd_run = os.environ.get("HERMES_SYSTEMD_RUN_BIN") or "systemd-run"
    unit = _closeout_unit_name(task_id, board)
    path_prefix = [
        os.path.expanduser("~/.local/bin"),
        str(repo_root / "venv" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    existing_path = os.environ.get("PATH", "")
    merged_path = ":".join(path_prefix + ([existing_path] if existing_path else []))
    setenv = [f"--setenv=PATH={merged_path}"]
    for key in (
        "HERMES_HOME",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_DB",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        value = os.environ.get(key)
        if value:
            setenv.append(f"--setenv={key}={value}")

    cli_args = [bin_path, "kanban"]
    if board:
        cli_args += ["--board", str(board)]
    cli_args += ["closeout", str(task_id), "--inline", "--json"]
    argv = [
        systemd_run,
        "--user",
        "--collect",
        f"--unit={unit}",
        f"--description=Hermes Kanban closeout {task_id}",
        *setenv,
        *cli_args,
    ]
    run = runner or subprocess.run
    try:
        proc = run(
            argv,
            capture_output=True,
            text=True,
            timeout=CLOSEOUT_SPAWN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "unit": unit, "detail": f"launch failed: {exc}"}
    detail = (
        (getattr(proc, "stdout", "") or "")
        + (getattr(proc, "stderr", "") or "")
    )[-1000:]
    if int(getattr(proc, "returncode", 1)) != 0:
        return {
            "ok": False,
            "unit": unit,
            "detail": detail or "systemd-run failed",
        }
    return {"ok": True, "unit": unit, "detail": detail or "started"}


def spawn_pending_closeouts(
    conn: Any,
    board: Optional[str],
    *,
    limit: int = 10,
    runner: Optional[Callable[..., Any]] = None,
    hermes_bin: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Spawn bounded detached workers without claiming any outbox rows."""

    outcomes: list[dict[str, Any]] = []
    for task_id, pending_event_id in pending_closeouts(conn, limit=limit):
        outcome = spawn_closeout_unit(
            task_id,
            board=board,
            runner=runner,
            hermes_bin=hermes_bin,
        )
        outcomes.append(
            {
                "task_id": task_id,
                "pending_event_id": pending_event_id,
                **outcome,
            }
        )
    return outcomes


def _json_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def _latest_event(conn: Any, task_id: str, kinds: set[str]) -> Optional[Any]:
    placeholders = ",".join("?" for _ in kinds)
    return conn.execute(
        f"SELECT id, kind, payload, created_at FROM task_events "
        f"WHERE task_id = ? AND kind IN ({placeholders}) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, *sorted(kinds)),
    ).fetchone()


def _has_event(conn: Any, task_id: str, kind: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
            (task_id, kind),
        ).fetchone()
        is not None
    )


def _append_once_in_txn(
    conn: Any,
    task_id: str,
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    run_id: Optional[int] = None,
) -> int:
    row = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    return _kb()._append_event(conn, task_id, kind, payload, run_id=run_id)


def enqueue_closeout_pending_in_txn(
    conn: Any,
    task_id: str,
    *,
    run_id: Optional[int] = None,
    summary: Optional[str] = None,
    board: Optional[str] = None,
    release_context: Optional[dict[str, Any]] = None,
) -> int:
    """Append one durable outbox row inside the caller's open transaction.

    The operation is idempotent per task.  This function intentionally does not
    open its own transaction, so ``complete_task`` can make the task transition,
    ``completed`` event, and closeout intent atomic.
    """

    task = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if task is None:
        raise ValueError(f"unknown task {task_id}")
    if task["status"] != "done":
        raise ValueError(f"closeout requires task {task_id} to be done")
    payload = {
        "version": 1,
        "summary": (summary or "").strip()[:400] or None,
        "board": board,
        "release_context": _json_safe(release_context or {}),
    }
    return _append_once_in_txn(
        conn, task_id, CLOSEOUT_PENDING, payload, run_id=run_id
    )


def enqueue_closeout_pending(
    conn: Any,
    task_id: str,
    *,
    run_id: Optional[int] = None,
    summary: Optional[str] = None,
    board: Optional[str] = None,
    release_context: Optional[dict[str, Any]] = None,
) -> int:
    """Transactional wrapper for callers that are not already in a write txn."""

    with _kb().write_txn(conn):
        return enqueue_closeout_pending_in_txn(
            conn,
            task_id,
            run_id=run_id,
            summary=summary,
            board=board,
            release_context=release_context,
        )


def _pending_is_actionable(conn: Any, task_id: str) -> bool:
    task = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if task is None or task["status"] != "done":
        return False
    if not _has_event(conn, task_id, CLOSEOUT_PENDING):
        return False
    if _has_event(conn, task_id, CLOSEOUT_DELIVERED):
        return False
    receipt_done = _has_event(conn, task_id, CLOSEOUT_RECEIPT_WRITTEN)
    release = _latest_event(conn, task_id, _RELEASE_TERMINAL_KINDS)
    if not receipt_done or release is None:
        return True
    context = _pending_release_context(conn, task_id)
    child_id = str(context.get("release_gate_child_id") or "").strip()
    if child_id and _has_event(conn, child_id, "release_gate_activated"):
        return release["kind"] != CLOSEOUT_RELEASE_COMPLETE
    return release["kind"] in _RELEASE_SUCCESS_KINDS


def pending_closeouts(conn: Any, *, limit: int = 10) -> list[tuple[str, int]]:
    """Return bounded actionable ``(task_id, pending_event_id)`` items."""

    limit = max(0, min(int(limit), 100))
    if limit == 0:
        return []
    rows = conn.execute(
        """
        SELECT p.task_id, MIN(p.id) AS pending_event_id
          FROM task_events p
          JOIN tasks t ON t.id = p.task_id AND t.status = 'done'
         WHERE p.kind = ?
           AND NOT EXISTS (
                 SELECT 1 FROM task_events d
                  WHERE d.task_id = p.task_id AND d.kind = ?
               )
           AND (
                 NOT EXISTS (
                     SELECT 1 FROM task_events r
                      WHERE r.task_id = p.task_id AND r.kind = ?
                 )
                 OR NOT EXISTS (
                     SELECT 1 FROM task_events x
                      WHERE x.task_id = p.task_id
                        AND x.kind IN (?, ?, ?, ?)
                 )
                 OR EXISTS (
                     SELECT 1 FROM task_events s
                      WHERE s.task_id = p.task_id AND s.kind IN (?, ?)
                 )
                 OR EXISTS (
                     SELECT 1 FROM task_events g
                      WHERE g.task_id = json_extract(
                            p.payload, '$.release_context.release_gate_child_id'
                        )
                        AND g.kind = 'release_gate_activated'
                 )
               )
         GROUP BY p.task_id
         ORDER BY pending_event_id ASC
         LIMIT ?
        """,
        (
            CLOSEOUT_PENDING,
            CLOSEOUT_DELIVERED,
            CLOSEOUT_RECEIPT_WRITTEN,
            CLOSEOUT_RELEASE_COMPLETE,
            CLOSEOUT_RELEASE_NOT_REQUIRED,
            CLOSEOUT_RELEASE_HELD,
            CLOSEOUT_RELEASE_AMBIGUOUS,
            CLOSEOUT_RELEASE_COMPLETE,
            CLOSEOUT_RELEASE_NOT_REQUIRED,
            limit,
        ),
    ).fetchall()
    return [(str(row["task_id"]), int(row["pending_event_id"])) for row in rows]


def claim_closeout(
    conn: Any,
    task_id: str,
    *,
    pending_event_id: Optional[int] = None,
    lease_seconds: int = DEFAULT_CLOSEOUT_LEASE_SECONDS,
    now: Optional[int] = None,
    token: Optional[str] = None,
) -> Optional[CloseoutClaim]:
    """Claim one closeout under ``BEGIN IMMEDIATE`` with a recoverable lease."""

    now = int(time.time() if now is None else now)
    lease_seconds = max(1, int(lease_seconds))
    kb = _kb()
    with kb.write_txn(conn):
        if not _pending_is_actionable(conn, task_id):
            return None
        pending = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id ASC LIMIT 1",
            (task_id, CLOSEOUT_PENDING),
        ).fetchone()
        if pending is None:
            return None
        actual_pending_id = int(pending["id"])
        if pending_event_id is not None and int(pending_event_id) != actual_pending_id:
            return None

        latest_claim = _latest_event(conn, task_id, {CLOSEOUT_CLAIMED})
        if latest_claim is not None:
            claim_payload = _json_payload(latest_claim["payload"])
            active_token = str(claim_payload.get("token") or "")
            finished_rows = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
                "AND id > ? ORDER BY id",
                (task_id, CLOSEOUT_CLAIM_FINISHED, int(latest_claim["id"])),
            ).fetchall()
            claim_finished = any(
                str(_json_payload(row["payload"]).get("token") or "")
                == active_token
                for row in finished_rows
            )
            try:
                lease_until = int(claim_payload.get("lease_until") or 0)
            except (TypeError, ValueError):
                lease_until = 0
            if not claim_finished and lease_until > now:
                return None

        claim_token = token or uuid.uuid4().hex
        lease_until = now + lease_seconds
        kb._append_event(
            conn,
            task_id,
            CLOSEOUT_CLAIMED,
            {
                "pending_event_id": actual_pending_id,
                "token": claim_token,
                "claimed_at": now,
                "lease_until": lease_until,
            },
        )
        return CloseoutClaim(
            task_id=task_id,
            pending_event_id=actual_pending_id,
            token=claim_token,
            lease_until=lease_until,
        )


def _finish_claim(conn: Any, claim: CloseoutClaim, state: str) -> bool:
    with _kb().write_txn(conn):
        latest_claim = _latest_event(conn, claim.task_id, {CLOSEOUT_CLAIMED})
        if latest_claim is None:
            return False
        latest_payload = _json_payload(latest_claim["payload"])
        if str(latest_payload.get("token") or "") != claim.token:
            return False
        _kb()._append_event(
            conn,
            claim.task_id,
            CLOSEOUT_CLAIM_FINISHED,
            {"token": claim.token, "state": state},
        )
        return True


def _default_release_runner(conn: Any, task_id: str) -> Any:
    from hermes_cli import auto_release

    return auto_release.maybe_auto_release(conn, task_id)


def _classify_release_result(result: Any) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"result": _json_safe(result)}
    if result is None:
        payload["outcome"] = "not_required"
        return CLOSEOUT_RELEASE_NOT_REQUIRED, payload
    if isinstance(result, bool):
        payload["outcome"] = "complete" if result else "held"
        return (
            CLOSEOUT_RELEASE_COMPLETE if result else CLOSEOUT_RELEASE_HELD,
            payload,
        )
    if isinstance(result, dict):
        outcome = str(result.get("outcome") or "").strip().lower()
        payload = _json_safe(result)
        if outcome in _NOT_REQUIRED_OUTCOMES:
            return CLOSEOUT_RELEASE_NOT_REQUIRED, payload
        if outcome in _COMPLETE_OUTCOMES:
            return CLOSEOUT_RELEASE_COMPLETE, payload
        return CLOSEOUT_RELEASE_HELD, payload
    return CLOSEOUT_RELEASE_HELD, payload


def _release_state(conn: Any, task_id: str) -> tuple[Optional[str], dict[str, Any]]:
    row = _latest_event(conn, task_id, _RELEASE_TERMINAL_KINDS)
    if row is None:
        return None, {}
    return str(row["kind"]), _json_payload(row["payload"])


def _pending_release_context(conn: Any, task_id: str) -> dict[str, Any]:
    pending = _latest_event(conn, task_id, {CLOSEOUT_PENDING})
    payload = _json_payload(pending["payload"] if pending is not None else None)
    context = payload.get("release_context")
    return context if isinstance(context, dict) else {}


def _release_gate_context_state(
    conn: Any,
    task_id: str,
    *,
    allow_start: bool = False,
) -> Optional[str]:
    """Reconcile a release-gate child without ever using generic fallback."""

    context = _pending_release_context(conn, task_id)
    child_id = str(context.get("release_gate_child_id") or "").strip()
    if not child_id:
        if context.get("release_gate_required"):
            with _kb().write_txn(conn):
                _append_once_in_txn(
                    conn,
                    task_id,
                    CLOSEOUT_RELEASE_HELD,
                    {
                        "outcome": "held_required_release_gate_missing",
                        "reason": "web release requires a release-gate child",
                    },
                )
            return CLOSEOUT_RELEASE_HELD
        return None
    child = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (child_id,)
    ).fetchone()
    event_rows = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (child_id,)
    ).fetchall()
    event_kinds = {str(row["kind"]) for row in event_rows}

    if "release_gate_activated" in event_kinds:
        with _kb().write_txn(conn):
            _append_once_in_txn(
                conn,
                task_id,
                CLOSEOUT_RELEASE_COMPLETE,
                {
                    "outcome": "deployed_via_release_gate",
                    "release_gate_child_id": child_id,
                },
            )
        return CLOSEOUT_RELEASE_COMPLETE

    if "release_gate_auto_execute_failed" in event_kinds:
        _record_ambiguous(
            conn,
            task_id,
            reason="release-gate launch acknowledgement failed; no fallback allowed",
        )
        return CLOSEOUT_RELEASE_AMBIGUOUS

    failure_events = {
        "release_gate_activation_failed",
        "operator_escalation",
        "gave_up",
        "crashed",
        "timed_out",
    }
    terminal_failure = child is None or (
        child["status"] in {"archived", "failed", "cancelled"}
    )
    if terminal_failure or event_kinds.intersection(failure_events):
        with _kb().write_txn(conn):
            _append_once_in_txn(
                conn,
                task_id,
                CLOSEOUT_RELEASE_HELD,
                {
                    "outcome": "held_release_gate_failed",
                    "release_gate_child_id": child_id,
                    "child_status": child["status"] if child is not None else None,
                    "failure_events": sorted(event_kinds.intersection(failure_events)),
                },
            )
        return CLOSEOUT_RELEASE_HELD

    if "release_gate_auto_execute_started" in event_kinds:
        with _kb().write_txn(conn):
            _append_once_in_txn(
                conn,
                task_id,
                CLOSEOUT_RELEASE_WAITING,
                {
                    "release_gate_child_id": child_id,
                    "child_status": child["status"] if child is not None else None,
                },
            )
        return CLOSEOUT_RELEASE_WAITING

    if "release_gate_auto_execute_held" in event_kinds:
        with _kb().write_txn(conn):
            _append_once_in_txn(
                conn,
                task_id,
                CLOSEOUT_RELEASE_HELD,
                {
                    "outcome": "held_release_gate_not_auto_executed",
                    "release_gate_child_id": child_id,
                    "child_status": child["status"],
                    "auto_execute_held": True,
                },
            )
        return CLOSEOUT_RELEASE_HELD

    if not allow_start:
        return "release_gate_needs_start"

    from hermes_cli import kanban_worktrees

    start_state = kanban_worktrees.start_parked_release_gate(conn, child_id)
    if start_state == "started":
        with _kb().write_txn(conn):
            _append_once_in_txn(
                conn,
                task_id,
                CLOSEOUT_RELEASE_WAITING,
                {"release_gate_child_id": child_id, "child_status": child["status"]},
            )
        return CLOSEOUT_RELEASE_WAITING
    if start_state == "ambiguous":
        _record_ambiguous(
            conn,
            task_id,
            reason="release-gate launch result ambiguous; no fallback allowed",
        )
        return CLOSEOUT_RELEASE_AMBIGUOUS
    with _kb().write_txn(conn):
        _append_once_in_txn(
            conn,
            task_id,
            CLOSEOUT_RELEASE_HELD,
            {
                "outcome": "held_release_gate_not_auto_executed",
                "release_gate_child_id": child_id,
                "child_status": child["status"] if child is not None else None,
                "auto_execute_held": "release_gate_auto_execute_held" in event_kinds,
                "manually_parked": "release_gate_parked" in event_kinds,
            },
        )
    return CLOSEOUT_RELEASE_HELD


def _receipt_context(conn: Any, task_id: str) -> CloseoutReceipt:
    kb = _kb()
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task {task_id}")
    pending = _latest_event(conn, task_id, {CLOSEOUT_PENDING})
    pending_payload = _json_payload(pending["payload"] if pending is not None else None)
    release_kind, release_payload = _release_state(conn, task_id)
    return CloseoutReceipt(
        task_id=task_id,
        title=str(task.title or task_id),
        assignee=str(task.assignee or "unknown"),
        task_status=str(task.status or "done"),
        board=str(pending_payload.get("board") or ""),
        summary=str(kb.latest_summary(conn, task_id) or pending_payload.get("summary") or ""),
        result=str(task.result or ""),
        release_state=str(release_kind or "pending"),
        release_payload=release_payload,
    )


def _yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render_receipt(receipt: CloseoutReceipt) -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    lines = [
        "---",
        "kind: auto-receipt",
        f"task_id: {_yaml_string(receipt.task_id)}",
        f"title: {_yaml_string(receipt.title)}",
        f"assignee: {_yaml_string(receipt.assignee)}",
        f"status: {_yaml_string(receipt.task_status)}",
        f"board: {_yaml_string(receipt.board)}",
        f"release_state: {_yaml_string(receipt.release_state)}",
        f"completed_at: {_yaml_string(timestamp)}",
        "---",
        "",
        f"# {receipt.title}",
        "",
        "## Step-Ledger",
        "",
        f"- Task status: `{receipt.task_status}`.",
        f"- Assignee: `{receipt.assignee}`.",
        f"- Release state: `{receipt.release_state}`.",
    ]
    outcome = receipt.release_payload.get("outcome")
    if outcome:
        lines.append(f"- Release outcome: `{outcome}`.")
    if receipt.summary:
        lines.extend(["", "## Summary", "", receipt.summary])
    if receipt.result:
        lines.extend(["", "## Result", "", receipt.result])
    lines.append("")
    return "\n".join(lines)


def write_receipt_atomic(
    task_id: str,
    content: str,
    *,
    receipt_dir: Optional[Path | str] = None,
) -> ReceiptArtifact:
    """Durably replace ``<task_id>.md`` and return its content hash.

    All errors propagate.  The caller decides retry policy; this function never
    converts an unwritable vault into a false success acknowledgement.
    """

    configured = os.environ.get("HERMES_AUTO_RECEIPT_DIR")
    base = Path(receipt_dir or configured or AUTO_RECEIPT_DEFAULT_DIR)
    base.mkdir(parents=True, exist_ok=True)
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id)) + ".md"
    target = base / filename
    data = content.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    fd = -1
    tmp_path: Optional[Path] = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=base)
        tmp_path = Path(raw_path)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
        dir_fd = os.open(base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        raise
    return ReceiptArtifact(path=str(target), sha256=digest, size=len(data))


def _default_receipt_writer(receipt: CloseoutReceipt) -> ReceiptArtifact:
    return write_receipt_atomic(receipt.task_id, render_receipt(receipt))


def _record_ambiguous(
    conn: Any,
    task_id: str,
    *,
    reason: str,
    error: Optional[BaseException] = None,
) -> None:
    payload: dict[str, Any] = {"reason": reason}
    if error is not None:
        payload.update({"error": str(error), "error_type": type(error).__name__})
    with _kb().write_txn(conn):
        _append_once_in_txn(conn, task_id, CLOSEOUT_RELEASE_AMBIGUOUS, payload)


def _acquire_release_start(conn: Any, claim: CloseoutClaim) -> str:
    """CAS the non-idempotent release side effect to the active claim token."""

    with _kb().write_txn(conn):
        latest_claim = _latest_event(conn, claim.task_id, {CLOSEOUT_CLAIMED})
        if latest_claim is None:
            return "stale_claim"
        claim_payload = _json_payload(latest_claim["payload"])
        if str(claim_payload.get("token") or "") != claim.token:
            return "stale_claim"
        existing = _latest_event(conn, claim.task_id, {CLOSEOUT_RELEASE_STARTED})
        if existing is not None:
            return "already_started"
        _kb()._append_event(
            conn,
            claim.task_id,
            CLOSEOUT_RELEASE_STARTED,
            {"claim_token": claim.token, "pending_event_id": claim.pending_event_id},
        )
        return "acquired"


def _drive_release(
    conn: Any,
    claim: CloseoutClaim,
    release_runner: ReleaseRunner,
) -> str:
    """Drive one completion's release decision through the two mutually-exclusive
    hook paths in a FIXED order:

      1. release-gate path (``_release_gate_context_state`` →
         ``start_parked_release_gate`` → detached ``execute_release_gate``);
      2. ``maybe_auto_release`` fallback (``release_runner``).

    Both ultimately deploy through ``deploy_dashboard.sh`` — a real backend
    restart — so at most ONE may run per completion; firing both would restart
    the backend twice. The gate path is evaluated FIRST and OWNS the deploy
    whenever a release-gate child exists (it returns a non-None state for every
    gate outcome: needs-start→started/waiting, held, failed, ambiguous). Control
    only falls through to ``maybe_auto_release`` when this completion carries no
    release-gate child at all. This ordering is the mutex that makes
    ``release.autonomous`` safe to flip ON without a double-backend-restart.
    """
    gate_state = _release_gate_context_state(conn, claim.task_id)
    if gate_state == "release_gate_needs_start":
        ownership = _acquire_release_start(conn, claim)
        if ownership == "stale_claim":
            return CLOSEOUT_RELEASE_WAITING
        if ownership == "already_started":
            _record_ambiguous(
                conn,
                claim.task_id,
                reason="release-gate start owner expired before launch acknowledgement",
            )
            return CLOSEOUT_RELEASE_AMBIGUOUS
        gate_state = _release_gate_context_state(
            conn, claim.task_id, allow_start=True,
        )
    if gate_state is not None:
        return gate_state

    release_kind, _payload = _release_state(conn, claim.task_id)
    if release_kind is not None:
        return release_kind

    release_context = _pending_release_context(conn, claim.task_id)
    if release_context.get("auto_release_candidate") is False:
        with _kb().write_txn(conn):
            _append_once_in_txn(
                conn,
                claim.task_id,
                CLOSEOUT_RELEASE_NOT_REQUIRED,
                {"outcome": "not_required", "reason": "not_chain_tip_completion"},
            )
        return CLOSEOUT_RELEASE_NOT_REQUIRED

    # Mutex backstop: control only reaches ``maybe_auto_release`` when the gate
    # path yielded no state, which ``_release_gate_context_state`` guarantees
    # ONLY for a completion with no release-gate child. If a gate child is
    # nonetheless recorded here, the gate path already owns (or will own) the
    # backend restart — so refuse the ``maybe_auto_release`` fallback (ambiguous,
    # no second deploy) instead of letting a future change to the gate reconciler
    # silently open a double-backend-restart when release.autonomous is ON.
    if str(release_context.get("release_gate_child_id") or "").strip() or (
        release_context.get("release_gate_required")
    ):
        _record_ambiguous(
            conn,
            claim.task_id,
            reason=(
                "release-gate child present but gate path returned no state; "
                "refusing maybe_auto_release fallback to avoid a double deploy"
            ),
        )
        return CLOSEOUT_RELEASE_AMBIGUOUS

    ownership = _acquire_release_start(conn, claim)
    if ownership == "stale_claim":
        return CLOSEOUT_RELEASE_WAITING
    if ownership == "already_started":
        _record_ambiguous(
            conn,
            claim.task_id,
            reason="release started previously without terminal acknowledgement",
        )
        return CLOSEOUT_RELEASE_AMBIGUOUS

    try:
        result = release_runner(conn, claim.task_id)
    except Exception as exc:
        _record_ambiguous(
            conn,
            claim.task_id,
            reason="release runner raised after durable start marker",
            error=exc,
        )
        return CLOSEOUT_RELEASE_AMBIGUOUS

    kind, payload = _classify_release_result(result)
    with _kb().write_txn(conn):
        _append_once_in_txn(conn, claim.task_id, kind, payload)
        # Preserve the existing alert/metrics contract while the closeout
        # events add durable delivery state. ``maybe_auto_release`` historically
        # emitted this event for every concrete outcome (including holds).
        if isinstance(result, dict):
            _append_once_in_txn(
                conn,
                claim.task_id,
                "auto_release",
                _json_safe(result),
            )
    return kind


def _drive_receipt(
    conn: Any,
    claim: CloseoutClaim,
    receipt_writer: ReceiptWriter,
) -> Optional[ReceiptArtifact]:
    release = _latest_event(conn, claim.task_id, _RELEASE_TERMINAL_KINDS)
    release_event_id = int(release["id"]) if release is not None else None
    existing = _latest_event(conn, claim.task_id, {CLOSEOUT_RECEIPT_WRITTEN})
    if existing is not None:
        payload = _json_payload(existing["payload"])
        if payload.get("release_event_id") == release_event_id:
            return ReceiptArtifact(
                path=str(payload.get("path") or ""),
                sha256=str(payload.get("sha256") or ""),
                size=int(payload.get("size") or 0),
            )

    receipt = _receipt_context(conn, claim.task_id)
    try:
        artifact = receipt_writer(receipt)
    except Exception as exc:
        with _kb().write_txn(conn):
            _kb()._append_event(
                conn,
                claim.task_id,
                CLOSEOUT_RECEIPT_FAILED,
                {"error": str(exc), "error_type": type(exc).__name__},
            )
        raise

    with _kb().write_txn(conn):
        _kb()._append_event(
            conn,
            claim.task_id,
            CLOSEOUT_RECEIPT_WRITTEN,
            {
                "path": artifact.path,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "release_event_id": release_event_id,
            },
        )
    return artifact


def _finalize_if_delivered(conn: Any, claim: CloseoutClaim) -> bool:
    release_kind, release_payload = _release_state(conn, claim.task_id)
    if release_kind not in _RELEASE_SUCCESS_KINDS:
        return False
    receipt = _latest_event(conn, claim.task_id, {CLOSEOUT_RECEIPT_WRITTEN})
    if receipt is None:
        return False
    receipt_payload = _json_payload(receipt["payload"])
    with _kb().write_txn(conn):
        _append_once_in_txn(
            conn,
            claim.task_id,
            CLOSEOUT_DELIVERED,
            {
                "release_state": release_kind,
                "release_outcome": release_payload.get("outcome"),
                "receipt_path": receipt_payload.get("path"),
                "receipt_sha256": receipt_payload.get("sha256"),
            },
        )
    return True


def process_closeout_claim(
    conn: Any,
    claim: CloseoutClaim,
    *,
    release_runner: Optional[ReleaseRunner] = None,
    receipt_writer: Optional[ReceiptWriter] = None,
) -> CloseoutResult:
    """Process one claimed item. Receipt errors propagate after audit logging."""

    release_runner = release_runner or _default_release_runner
    receipt_writer = receipt_writer or _default_receipt_writer
    finish_state = "failed"
    try:
        release_kind = _drive_release(conn, claim, release_runner)
        if release_kind == CLOSEOUT_RELEASE_WAITING:
            finish_state = "pending"
            return CloseoutResult(
                task_id=claim.task_id,
                state="pending",
                release_state=release_kind,
                delivered=False,
            )
        artifact = _drive_receipt(conn, claim, receipt_writer)
        delivered = _finalize_if_delivered(conn, claim)
        if delivered:
            state = "delivered"
        elif release_kind == CLOSEOUT_RELEASE_HELD:
            state = "held"
        elif release_kind == CLOSEOUT_RELEASE_AMBIGUOUS:
            state = "ambiguous"
        else:
            state = "pending"
        finish_state = state
        return CloseoutResult(
            task_id=claim.task_id,
            state=state,
            release_state=release_kind,
            receipt_path=artifact.path if artifact else None,
            delivered=delivered,
        )
    finally:
        _finish_claim(conn, claim, finish_state)


def process_closeout(
    conn: Any,
    task_id: str,
    *,
    lease_seconds: int = DEFAULT_CLOSEOUT_LEASE_SECONDS,
    now: Optional[int] = None,
    release_runner: Optional[ReleaseRunner] = None,
    receipt_writer: Optional[ReceiptWriter] = None,
) -> CloseoutResult:
    """Claim and process one task inline after its completion transaction."""

    claim = claim_closeout(
        conn,
        task_id,
        lease_seconds=lease_seconds,
        now=now,
    )
    if claim is None:
        delivered = _has_event(conn, task_id, CLOSEOUT_DELIVERED)
        return CloseoutResult(
            task_id=task_id,
            state="delivered" if delivered else "not_claimed",
            delivered=delivered,
        )
    return process_closeout_claim(
        conn,
        claim,
        release_runner=release_runner,
        receipt_writer=receipt_writer,
    )


def closeout_sweep(
    conn: Any,
    *,
    limit: int = 10,
    lease_seconds: int = DEFAULT_CLOSEOUT_LEASE_SECONDS,
    now: Optional[int] = None,
    release_runner: Optional[ReleaseRunner] = None,
    receipt_writer: Optional[ReceiptWriter] = None,
) -> list[CloseoutResult]:
    """Process at most ``limit`` actionable outbox items, fail-soft per item."""

    results: list[CloseoutResult] = []
    for task_id, pending_event_id in pending_closeouts(conn, limit=limit):
        claim = claim_closeout(
            conn,
            task_id,
            pending_event_id=pending_event_id,
            lease_seconds=lease_seconds,
            now=now,
        )
        if claim is None:
            continue
        try:
            result = process_closeout_claim(
                conn,
                claim,
                release_runner=release_runner,
                receipt_writer=receipt_writer,
            )
        except Exception as exc:
            results.append(
                CloseoutResult(
                    task_id=task_id,
                    state="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            results.append(result)
    return results
