"""Persistent delivery outbox for cron (audit loop 6, A-H2/A-M8).

Before this module, a delivery that failed on every path (live adapter +
standalone fallback) was recorded only in the job's ``last_delivery_error``
field — and the error summary itself went over the same possibly-broken
channel, so report losses were silent and unrecoverable.

The outbox is a durable JSONL store (``cron/outbox.jsonl`` under the active
cron store): when ``cron.scheduler._deliver_result`` exhausts every delivery
path for a target, the payload is enqueued here. At the start of every tick,
due entries are re-sent over the standalone path with exponential backoff
(5min → 10 → 20 → 40 → 80min cap). After ``MAX_ATTEMPTS`` failed replays an
entry becomes ``dead`` (dead-letter — it stays in the file and is surfaced by
``hermes cron status``, never silently dropped).

Design notes:

- One JSON object per line holding the LATEST state of an entry. Enqueue is
  append-only for new entries; status transitions (queued → delivered/dead,
  retry bookkeeping) atomically rewrite the file (tmp + os.replace), which
  doubles as compaction. This keeps the store readable with ``jq``/``tail``
  and avoids a folding log for a workload of a handful of entries.
- Dedupe: a repeated delivery failure for the same (job_id, target) while an
  entry is still ``queued`` refreshes that entry's payload/error instead of
  appending a duplicate — and deliberately does NOT postpone an already
  scheduled ``next_retry_at``.
- Store scoping mirrors cron/executions.py: the ContextVar behind
  cron.jobs.use_cron_store() (per-profile multiplex) wins, then a re-pointed
  OUTBOX_FILE constant (test/embedder escape hatch), then the ACTIVE
  HERMES_HOME resolved fresh per call, then the import-time constant.
- Fail-closed contract: callers wrap every entry point so an outbox I/O
  error never breaks a tick; the payload still lands in
  ``last_delivery_error`` exactly as before (that remains the net of last
  resort).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from utils import atomic_replace

logger = logging.getLogger(__name__)

# Retry policy: up to MAX_ATTEMPTS replays with exponential backoff starting
# at 5 minutes, doubling, capped at 80 minutes. A failed replay beyond the
# cap marks the entry dead (dead-letter, stays visible).
MAX_ATTEMPTS = 5
INITIAL_BACKOFF_SECONDS = 300
MAX_BACKOFF_SECONDS = 4800

# Delivered entries are kept for post-hoc inspection, then compacted away on
# the next write. Queued and dead entries are never auto-pruned — dead
# letters are exactly what an operator must see.
DELIVERED_RETENTION_SECONDS = 7 * 86400

# Like cron/jobs.py and cron/executions.py, the outbox is per-profile by
# design (#4707): each profile owns its own outbox.jsonl under its own
# HERMES_HOME, and multiplex_profiles scopes every tick to one profile at a
# time. OUTBOX_FILE remains the import-time default-profile fallback and a
# compatibility surface for tests/embedders that deliberately re-point it;
# the ACTIVE file is resolved per call by _current_outbox_file() — never
# read OUTBOX_FILE directly for I/O.
_IMPORT_HOME = get_hermes_home().resolve()
OUTBOX_FILE = _IMPORT_HOME / "cron" / "outbox.jsonl"
_IMPORT_OUTBOX_FILE = OUTBOX_FILE

_lock = threading.RLock()


def _current_outbox_file() -> Path:
    """Return the outbox path pinned to this execution context's profile.

    Precedence mirrors cron/executions.py::_current_executions_file(), most
    explicit first:

    1. an active cron-store override — the ContextVar behind
       cron.jobs.use_cron_store(), which multiplex_profiles sets per profile
       around tick/recovery/heartbeat;
    2. a deliberately re-pointed OUTBOX_FILE module constant (documented
       escape hatch for tests/embedders);
    3. the ACTIVE profile home, resolved fresh via get_hermes_home() — so a
       caller that re-points HERMES_HOME after import writes ITS OWN outbox,
       not the one the import happened to freeze;
    4. the import-time constant (home unchanged since import).
    """
    # Lazy import: cron/jobs.py must not import this module at module level.
    from cron.jobs import _cron_store_override

    store = _cron_store_override.get()
    if store is not None:
        return store.cron_dir / "outbox.jsonl"
    if OUTBOX_FILE != _IMPORT_OUTBOX_FILE:
        return OUTBOX_FILE
    home = get_hermes_home().resolve()
    if home == _IMPORT_HOME:
        return OUTBOX_FILE
    return home / "cron" / "outbox.jsonl"


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _ensure_aware(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


def _read_entries(path: Path) -> List[Dict[str, Any]]:
    """Load all entries, skipping malformed lines (tolerates a torn append)."""
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("cron outbox: skipping malformed line in %s", path)
                    continue
                if isinstance(entry, dict) and entry.get("id"):
                    entries.append(entry)
    except OSError:
        raise
    return entries


def _write_entries(path: Path, entries: List[Dict[str, Any]]) -> None:
    """Atomically rewrite the outbox (compaction), pruning aged delivered rows."""
    now = _hermes_now()
    kept: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("status") == "delivered":
            last = _parse_ts(entry.get("last_attempt_at"))
            if last is not None and (now - last).total_seconds() > DELIVERED_RETENTION_SECONDS:
                continue
        kept.append(entry)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for entry in kept:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        real = atomic_replace(tmp_name, path)
        try:
            os.chmod(real, 0o600)
        except (OSError, NotImplementedError):
            pass  # Windows or other platforms where chmod is not supported
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _dedupe_key(job_id: Any, target: Any) -> str:
    target = target if isinstance(target, dict) else {}
    return "|".join([
        str(job_id or ""),
        str(target.get("platform") or ""),
        str(target.get("chat_id") or ""),
        str(target.get("thread_id") or ""),
    ])


def _backoff_seconds(attempts: int) -> int:
    """Backoff for the retry scheduled AFTER ``attempts`` failed replays."""
    return min(
        INITIAL_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)),
        MAX_BACKOFF_SECONDS,
    )


def enqueue(
    job_id: str,
    target: Dict[str, Any],
    payload: str,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist an undeliverable payload for later replay.

    Idempotent: while an entry for the same (job_id, target) is still
    ``queued``, a repeated failure refreshes that entry's payload/error
    instead of appending a duplicate, and the pending ``next_retry_at`` is
    kept (a fresh failure must not postpone an already scheduled retry).
    """
    with _lock:
        path = _current_outbox_file()
        entries = _read_entries(path)
        key = _dedupe_key(job_id, target)
        now = _hermes_now().isoformat()
        for entry in entries:
            if (
                entry.get("status") == "queued"
                and _dedupe_key(entry.get("job_id"), entry.get("target")) == key
            ):
                entry["payload"] = payload
                entry["last_error"] = error
                entry["last_attempt_at"] = now
                _write_entries(path, entries)
                return entry
        entry = {
            "id": uuid.uuid4().hex[:16],
            "job_id": str(job_id),
            "target": {
                "platform": str(target.get("platform") or ""),
                "chat_id": str(target.get("chat_id") or ""),
                "thread_id": (
                    str(target["thread_id"])
                    if target.get("thread_id") is not None
                    else None
                ),
            },
            "payload": payload,
            "status": "queued",
            "attempts": 0,
            "first_failed_at": now,
            "last_attempt_at": now,
            # Due immediately: the first replay happens on the next tick
            # (transient blips recover in ~one tick); later retries back off.
            "next_retry_at": now,
            "last_error": error,
        }
        entries.append(entry)
        _write_entries(path, entries)
        return entry


def due_entries(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Return queued entries whose retry time has come (oldest first)."""
    with _lock:
        entries = _read_entries(_current_outbox_file())
    now = now or _hermes_now()
    due = []
    for entry in entries:
        if entry.get("status") != "queued":
            continue
        if (entry.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        next_retry = _parse_ts(entry.get("next_retry_at"))
        if next_retry is None or next_retry <= now:
            due.append(entry)
    return due


def record_replay_result(
    entry_id: str,
    *,
    success: bool,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Record the outcome of one replay attempt.

    Success → ``delivered``. Failure increments ``attempts`` and either
    schedules the next retry (exponential backoff) or, past ``MAX_ATTEMPTS``,
    marks the entry ``dead`` (dead-letter; never retried again, never
    auto-pruned). Returns the updated entry, or None if the id is unknown.
    """
    with _lock:
        path = _current_outbox_file()
        entries = _read_entries(path)
        now = _hermes_now()
        for entry in entries:
            if entry.get("id") != entry_id:
                continue
            if entry.get("status") != "queued":
                return entry  # terminal or already transitioned — no rewrite
            entry["last_attempt_at"] = now.isoformat()
            if success:
                entry["status"] = "delivered"
                entry["last_error"] = None
            else:
                entry["attempts"] = (entry.get("attempts") or 0) + 1
                entry["last_error"] = error
                if entry["attempts"] >= MAX_ATTEMPTS:
                    entry["status"] = "dead"
                else:
                    entry["next_retry_at"] = (
                        now + timedelta(seconds=_backoff_seconds(entry["attempts"]))
                    ).isoformat()
            _write_entries(path, entries)
            return entry
    return None


def list_entries(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all entries, optionally filtered by status."""
    with _lock:
        entries = _read_entries(_current_outbox_file())
    if status is None:
        return entries
    return [e for e in entries if e.get("status") == status]


def outbox_counts() -> Dict[str, int]:
    """Return per-status counts for ``hermes cron status`` visibility."""
    counts: Dict[str, int] = {"queued": 0, "delivered": 0, "dead": 0}
    for entry in list_entries():
        status = str(entry.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts
