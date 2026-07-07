"""Kanban adapter for Design Board cards."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from hermes_cli import design_board_store as store
from hermes_cli import kanban_db

TERMINAL = {"done", "archived"}
_CHROMIUM_SHOT = os.path.expanduser("~/bin/chromium-shot")
_AFTER_MARKER_PREFIX = "after-screenshot task:"
_RECEIPT_MARKER_PREFIX = "task-receipt task:"

_get_task = kanban_db.get_task


def _open_ro():
    # connect_closing() resolves the active board's kanban.db itself and is the
    # documented convention for long-lived readers (kanban_db.py:2766) — avoids
    # FD exhaustion in the dashboard process.
    return kanban_db.connect_closing()


def task_facets(task_ids: list[str]) -> list[dict]:
    if not task_ids:
        return []
    out: list[dict] = []
    with _open_ro() as conn:
        for tid in task_ids:
            task = _get_task(conn, tid)
            if task is None:
                continue
            out.append({
                "id": task.id, "status": task.status,
                "assignee": task.assignee, "terminal": task.status in TERMINAL,
            })
    return out


_BATCH_CHUNK = 900


def batch_task_facets(task_ids: list[str]) -> dict[str, dict]:
    """Return a mapping task_id -> facet for all found tasks.

    Uses one query per chunk to stay below SQLite host-parameter limits.
    """
    if not task_ids:
        return {}
    unique_ids = list(dict.fromkeys(task_ids))
    out: dict[str, dict] = {}
    with _open_ro() as conn:
        for i in range(0, len(unique_ids), _BATCH_CHUNK):
            chunk = unique_ids[i:i + _BATCH_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            query = f"SELECT id, status, assignee FROM tasks WHERE id IN ({placeholders})"
            for row in conn.execute(query, chunk).fetchall():
                status = row["status"]
                out[row["id"]] = {
                    "id": row["id"], "status": status,
                    "assignee": row["assignee"], "terminal": status in TERMINAL,
                }
    return out


def register_lifecycle_hooks() -> None:
    """Register Design Board Kanban lifecycle observers."""
    from hermes_cli.plugins import get_plugin_manager

    hooks = get_plugin_manager()._hooks
    callbacks = hooks.setdefault("kanban_task_completed", [])
    if handle_task_completed not in callbacks:
        callbacks.append(handle_task_completed)


def handle_task_completed(task_id: str, **kwargs: object) -> None:
    """Attach automatic Design Board updates when a linked task is done."""
    run_id = kwargs.get("run_id")
    attach_completion_receipts_for_task(
        task_id,
        status="done",
        run_id=run_id if isinstance(run_id, int) else None,
    )
    attach_after_screenshots_for_task(task_id, status="done")


def attach_completion_receipts_for_task(
    task_id: str,
    *,
    status: str,
    run_id: int | None = None,
) -> list[str]:
    """Write one idempotent completion receipt comment per linked card."""
    if status not in TERMINAL:
        return []

    created: list[str] = []
    note = _completion_receipt_note(task_id, run_id=run_id)
    for card in _cards_linked_to_task(task_id):
        card_id = card.get("id")
        if not isinstance(card_id, str) or _has_receipt_entry(card, task_id):
            continue
        created.append(store.add_entry(
            card_id,
            author="system",
            kind="comment",
            note=note,
        ))
    return created


def attach_after_screenshots_for_task(task_id: str, *, status: str) -> list[str]:
    """Attach fresh after-screenshots or error comments for cards linked to task_id.

    Returns the Design Board entry ids created. The function is best-effort per
    card so one unavailable view/chromium binary does not block Kanban completion.
    """
    if status not in TERMINAL:
        return []

    created: list[str] = []
    for card in _cards_linked_to_task(task_id):
        card_id = card.get("id")
        if not isinstance(card_id, str) or _has_after_entry(card, task_id):
            continue
        try:
            png = _render_dashboard_view(card)
            asset_name = store.write_asset(card_id, f"after-{task_id}.png", png)
            created.append(store.add_entry(
                card_id,
                author="system",
                kind="screenshot",
                note=f"{_AFTER_MARKER_PREFIX}{task_id}",
                asset_name=asset_name,
            ))
        except Exception as exc:
            created.append(store.add_entry(
                card_id,
                author="system",
                kind="comment",
                note=f"{_AFTER_MARKER_PREFIX}{task_id} failed: {exc}",
            ))
    return created


def _cards_linked_to_task(task_id: str) -> list[dict]:
    return [
        card for card in store.list_cards()
        if task_id in (card.get("linked_tasks") or [])
    ]


def _has_after_entry(card: dict, task_id: str) -> bool:
    marker = f"{_AFTER_MARKER_PREFIX}{task_id}"
    for entry in card.get("entries") or []:
        if isinstance(entry, dict) and str(entry.get("note") or "").startswith(marker):
            return True
    return False


def _has_receipt_entry(card: dict, task_id: str) -> bool:
    marker = f"{_RECEIPT_MARKER_PREFIX}{task_id}"
    for entry in card.get("entries") or []:
        if isinstance(entry, dict) and str(entry.get("note") or "").startswith(marker):
            return True
    return False


def _completion_receipt_note(task_id: str, *, run_id: int | None = None) -> str:
    completed_at, commit = _completion_receipt_metadata(task_id, run_id=run_id)
    completed = _format_completed_at(completed_at) if completed_at else "unknown"
    note = f"{_RECEIPT_MARKER_PREFIX}{task_id} completed_at:{completed}"
    if commit:
        note += f" commit:{commit}"
    return note


def _completion_receipt_metadata(task_id: str, *, run_id: int | None = None) -> tuple[int | None, str | None]:
    completed_at: int | None = None
    commit: str | None = None
    try:
        with _open_ro() as conn:
            task = _get_task(conn, task_id)
            completed_at = task.completed_at if task is not None else None
            if run_id is not None:
                row = conn.execute(
                    "SELECT metadata, ended_at FROM task_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if row is not None:
                    completed_at = completed_at or row["ended_at"]
                    commit = _commit_from_payload(row["metadata"])
            if commit is None:
                row = conn.execute(
                    """
                    SELECT payload FROM task_events
                    WHERE task_id = ? AND kind IN ('completed', 'done')
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                if row is not None:
                    commit = _commit_from_payload(row["payload"])
            # Fallback for review-gated code tasks: the commit is usually stored
            # on an earlier coder run or in the submitted_for_review worker_gate.
            if commit is None:
                commit = _find_historical_commit(conn, task_id)
    except Exception:
        return completed_at, commit
    return completed_at, commit


def _find_historical_commit(conn, task_id: str) -> str | None:
    """Search earlier task_runs and submitted_for_review events for a commit."""
    # 1. Any earlier task_run metadata with a commit, newest first.
    for row in conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL ORDER BY id DESC",
        (task_id,),
    ).fetchall():
        commit = _commit_from_payload(row["metadata"])
        if commit:
            return commit
    # 2. submitted_for_review payload (may carry worker_gate.commit).
    row = conn.execute(
        """
        SELECT payload FROM task_events
        WHERE task_id = ? AND kind = 'submitted_for_review'
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is not None:
        commit = _commit_from_payload(row["payload"])
        if commit:
            return commit
    return None


def _commit_from_payload(raw: object) -> str | None:
    """Extract commit hash from a JSON object/string payload.


    Handles top-level ``commit``/``commit_hash`` keys and nested ``metadata``
    objects. Also understands review-gated ``submitted_for_review`` payloads
    where the hash lives under ``worker_gate.commit``.
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("commit", "commit_hash"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = data.get("metadata")
    if isinstance(nested, dict):
        for key in ("commit", "commit_hash"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    worker_gate = data.get("worker_gate")
    if isinstance(worker_gate, dict):
        value = worker_gate.get("commit")
        if isinstance(value, str) and value.strip():
            return value.strip()
    # submitted_for_review event payload may wrap everything under metadata.
    if isinstance(nested, dict):
        worker_gate = nested.get("worker_gate")
        if isinstance(worker_gate, dict):
            value = worker_gate.get("commit")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _format_completed_at(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _render_dashboard_view(card: dict) -> bytes:
    url = _dashboard_url_for_card(card)
    exe = Path(_CHROMIUM_SHOT)
    if not exe.is_file():
        raise RuntimeError("chromium-shot not found")

    with tempfile.TemporaryDirectory(prefix="design-board-after-") as tmpdir:
        output = Path(tmpdir) / "after.png"
        result = subprocess.run(
            [
                str(exe),
                f"--screenshot={output}",
                "--window-size=1440,1200",
                "--virtual-time-budget=12000",
                url,
            ],
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-500:]
            raise RuntimeError(f"chromium-shot failed: {tail or result.returncode}")
        if not output.is_file():
            raise RuntimeError("chromium-shot produced no screenshot")
        return output.read_bytes()


def _dashboard_url_for_card(card: dict) -> str:
    target = card.get("target") or {}
    if not isinstance(target, dict):
        raise ValueError("card target is missing")
    raw_view = str(target.get("view") or "").strip()
    if not raw_view:
        raise ValueError("card target.view is missing")
    parsed = urlparse(raw_view)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return raw_view
    base = os.environ.get("HERMES_DESIGN_BOARD_DASHBOARD_BASE_URL", "http://127.0.0.1:9119").rstrip("/")
    path = raw_view if raw_view.startswith("/") else f"/{raw_view}"
    return f"{base}{path}"
