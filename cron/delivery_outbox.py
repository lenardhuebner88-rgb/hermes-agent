"""Persistent delivery outbox for cron (audit loop 6, A-H2/A-M8).

Before this module, a delivery that failed on every path (live adapter +
standalone fallback) was recorded only in the job's ``last_delivery_error``
field — and the error summary itself went over the same possibly-broken
channel, so report losses were silent and unrecoverable.

The outbox is a durable JSONL store (``cron/outbox.jsonl`` under the active
cron store): when ``cron.scheduler._deliver_result`` exhausts every delivery
path for a target, the payload is enqueued here. Due entries are re-sent over
the standalone path with exponential backoff (5min → 10 → 20 → 40 → 80min
cap) by TWO equivalent triggers (audit loop 13 / F2): the start of every
built-in tick AND the provider-independent ``run_one_job`` body that an
external provider's ``fire_due`` (Chronos) also runs — both guarded by the
non-blocking ``replay_lock()`` so they never double-send. After
``MAX_ATTEMPTS`` failed replays an entry becomes ``dead`` (dead-letter — it
stays in the file and is surfaced by ``hermes cron status``, never silently
dropped).

Design notes:

- One JSON object per line holding the LATEST state of an entry. Enqueue is
  append-only for new entries; status transitions (queued → delivered/dead,
  retry bookkeeping) atomically rewrite the file (tmp + os.replace), which
  doubles as compaction. This keeps the store readable with ``jq``/``tail``
  and avoids a folding log for a workload of a handful of entries.
- Dedupe (audit loop 13 / F1 — per-execution idempotency): entries are keyed
  per RUN, not per job. A repeated delivery failure for the same
  (execution_id, job_id, target) while the entry is still ``queued``
  refreshes that entry's payload/error instead of appending a duplicate — and
  deliberately does NOT postpone an already scheduled ``next_retry_at``.
  Failures from DIFFERENT executions of the same job/target NEVER collapse:
  each run's report gets its own entry, so two failed runs before the replay
  can no longer overwrite each other. Callers that cannot supply an
  execution_id keep the loop-6 legacy key (job_id, target).
- Error classes (audit loop 13 / F3): every entry carries ``error_class`` —
  ``send``   : the payload was attempted on every delivery path and failed;
               queued and retried with backoff (transient blips recover).
  ``config`` : operator/configuration error (unknown platform, platform not
               configured/enabled, gateway config unloadable); dead ON
               ARRIVAL — no replay can fix a typo'd deliver target, so it is
               parked as a visible dead letter instead of being retried
               forever.
  ``relay``  : relay-fronted destination; the relay connector owns the
               platform credential, so a standalone replay could never
               authenticate — dead on arrival, visible.
  Dead-letter entries are APPEND-ONLY per execution (audit loop 17 / F2):
  the dedupe key includes the execution_id, so two failed runs of the same
  job/target keep BOTH payloads — the loop-13 refresh dedupe used to let a
  newer run's config/relay failure overwrite an older run's payload. As the
  anti-pile-up guard for a permanently misconfigured job, at most
  ``MAX_DEAD_PER_TARGET`` (3) config/relay entries per (job_id, target,
  class) are kept: a new entry past the cap replaces the OLDEST one
  (documented cap, not silent growth). Legacy callers without an
  execution_id keep the loop-13 refresh collapse (their key cannot
  distinguish runs).
- Send lease + delivery receipt (audit loop 17 / F3): a replay persists
  status ``sending`` (with ``lease_ts``, ``lease_pid`` and the
  ``send_attempt`` number) BEFORE the send goes out, and records
  ``delivered`` with a ``receipt`` (the platform-side message_id when the
  send path returns one, else a payload hash + timestamp) immediately after.
  At replay start, a ``sending`` entry whose lease is older than
  ``HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS`` (default 600s) is orphaned — its
  holder died mid-send — and is retried; a fresh ``sending`` entry belongs
  to a live process and is skipped. HONEST REMAINING GAP: a crash between
  the successful send and the receipt write still redelivers once after the
  lease expires — that is at-least-once with a visible receipt as proof,
  not a silent loss; exactly-once would require platform-side idempotency
  keys the send APIs do not offer.
- Locking (audit loop 13 / F1): every read-modify-write (enqueue, replay
  bookkeeping) runs under the in-process RLock PLUS a bounded cross-process
  advisory flock on ``<outbox>.lock`` — same pattern as cron/jobs.py's
  ``.jobs.lock`` (#60703: bounded acquisition, degrade to in-process-only on
  timeout rather than freezing the scheduler). Parallel multiplex profiles
  and a standalone ``hermes cron`` invocation can no longer torn-write or
  lose each other's status transitions. ``replay_lock()`` additionally
  serializes whole replay passes (non-blocking: a second concurrent replay
  skips) so the tick hook and the provider-independent run_one_job hook never
  double-send.
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

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# fcntl is Unix-only; on Windows fall back to msvcrt. Either may be absent,
# in which case _store_lock() degrades to in-process locking only (mirrors
# cron/jobs.py).
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - Unix
    msvcrt = None

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

# Error classes (audit loop 13 / F3) — see the module docstring. ``send`` is
# the retriable default; ``config`` and ``relay`` are dead on arrival.
ERROR_CLASS_SEND = "send"
ERROR_CLASS_CONFIG = "config"
ERROR_CLASS_RELAY = "relay"
_ERROR_CLASSES_NO_RETRY = frozenset({ERROR_CLASS_CONFIG, ERROR_CLASS_RELAY})

# Anti-pile-up cap for dead-on-arrival classes (audit loop 17 / F2): at most
# this many config/relay entries per (job_id, target, class) are kept; a new
# entry past the cap replaces the OLDEST. Dead-letter payloads are append-only
# per execution below this cap — visibility without unbounded growth.
MAX_DEAD_PER_TARGET = 3

# Send-lease TTL (audit loop 17 / F3): a ``sending`` entry whose lease is
# older than this is orphaned (its holder crashed mid-send) and retried; a
# fresher lease belongs to a live process and is skipped. Env-overridable,
# read per call so tests and operators can tune without a restart.
_DEFAULT_LEASE_TTL_SECONDS = 600.0

# Upper bound on waiting for the cross-process <outbox>.lock flock — same
# reasoning as cron/jobs.py #60703: a wedged lock holder must not freeze the
# scheduler, so on timeout we degrade to in-process locking only.
_STORE_LOCK_TIMEOUT_SECONDS = 30.0

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
# In-process gate for whole replay passes (separate from _lock so a slow
# replay send never blocks an enqueue in a sibling thread).
_replay_gate = threading.Lock()


@contextlib.contextmanager
def _store_lock(path: Path):
    """Serialize one read-modify-write critical section cross-process.

    Advisory flock on ``<outbox>.lock`` next to the store file, mirroring
    cron/jobs.py's ``.jobs.lock`` (#60703): bounded LOCK_NB acquisition with
    a deadline; on timeout or when no locking primitive exists, degrade to
    in-process-only (the caller already holds the module RLock) rather than
    freezing the scheduler. Critical sections here are field updates on a
    handful of entries, so contention resolves in milliseconds.
    """
    lock_path = path.parent / (path.name + ".lock")
    lock_fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "a+", encoding="utf-8")
        if fcntl is not None:
            deadline = time.monotonic() + _STORE_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, IOError):
                    if time.monotonic() >= deadline:
                        logger.error(
                            "Timed out after %.0fs waiting for the cron "
                            "outbox lock (%s) — proceeding with in-process "
                            "locking only so the scheduler stays alive.",
                            _STORE_LOCK_TIMEOUT_SECONDS, lock_path,
                        )
                        try:
                            lock_fd.close()
                        except OSError:
                            pass
                        lock_fd = None
                        break
                    time.sleep(0.1)
        elif msvcrt is not None:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
    except (OSError, IOError) as e:
        logger.warning(
            "cron outbox cross-process lock unavailable (%s); "
            "proceeding with in-process lock only", e,
        )
    try:
        yield
    finally:
        if lock_fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
            finally:
                lock_fd.close()


@contextlib.contextmanager
def replay_lock():
    """Serialize a whole replay pass; a concurrent replay skips (yields False).

    Two replay triggers exist (audit loop 13 / F2): the built-in tick hook and
    the provider-independent ``run_one_job`` hook (Chronos fire_due). Both may
    fire concurrently in different threads or processes; sending is NOT
    covered by the per-transition store lock, so without this gate two
    replays could read the same due entries and deliver twice. Non-blocking
    in-process gate + non-blocking flock on ``<outbox>.replay.lock``: the
    loser yields False and the caller returns without sending. The per-entry
    ``next_retry_at`` gate makes a skipped retry cheap to rediscover on the
    next trigger.
    """
    if not _replay_gate.acquire(blocking=False):
        yield False
        return
    lock_fd = None
    acquired = True
    try:
        path = _current_outbox_file()
        lock_path = path.parent / (path.name + ".replay.lock")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = open(lock_path, "a+", encoding="utf-8")
            if fcntl is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, IOError):
                    acquired = False
            elif msvcrt is not None:
                try:
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                except (OSError, IOError):
                    acquired = False
        except (OSError, IOError) as e:
            # Lock file unusable — proceed with the in-process gate only.
            logger.debug("cron outbox replay lock unavailable (%s)", e)
        yield acquired
    finally:
        if lock_fd is not None:
            try:
                if acquired and fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                elif acquired and msvcrt is not None:
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
            finally:
                lock_fd.close()
        _replay_gate.release()


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


def _lease_ttl_seconds() -> float:
    """Effective send-lease TTL; env-tunable, invalid values fall back."""
    try:
        return float(os.environ.get(
            "HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", _DEFAULT_LEASE_TTL_SECONDS
        ))
    except (TypeError, ValueError):
        return _DEFAULT_LEASE_TTL_SECONDS


def _lease_is_stale(entry: Dict[str, Any], now: datetime) -> bool:
    """True when a ``sending`` entry's lease holder must be presumed dead."""
    lease = _parse_ts(entry.get("lease_ts"))
    if lease is None:
        return True  # sending without a lease timestamp cannot be fresh
    return (now - lease).total_seconds() > _lease_ttl_seconds()


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
    *,
    execution_id: Optional[str] = None,
    error_class: str = ERROR_CLASS_SEND,
) -> Dict[str, Any]:
    """Persist an undeliverable payload for later replay.

    Idempotent per RUN (audit loop 13 / F1): while an entry for the same
    (execution_id, job_id, target) is still ``queued``, a repeated failure
    refreshes that entry's payload/error instead of appending a duplicate,
    and the pending ``next_retry_at`` is kept (a fresh failure must not
    postpone an already scheduled retry). Failures from DIFFERENT executions
    never collapse — each run's report gets its own entry. Without an
    ``execution_id`` the loop-6 legacy key (job_id, target) applies.

    ``error_class`` (audit loop 13 / F3): ``send`` entries are queued and
    retried with backoff; ``config``/``relay`` entries are dead ON ARRIVAL
    (no replay can fix an operator typo or authenticate a relay destination).
    Audit loop 17 / F2: dead-letter entries are append-only per execution —
    the key includes the execution_id, so different runs keep BOTH payloads.
    A permanently misconfigured job is still bounded: past
    ``MAX_DEAD_PER_TARGET`` entries per (job_id, target, class) the OLDEST
    entry is replaced. Legacy callers without an execution_id keep the
    loop-13 refresh collapse and are marked with a warning on send-class
    enqueues (audit loop 17 / F1 — productive run paths are expected to
    carry an execution_id since run_one_job now wires it).
    """
    if error_class not in (ERROR_CLASS_SEND, ERROR_CLASS_CONFIG, ERROR_CLASS_RELAY):
        error_class = ERROR_CLASS_SEND
    if execution_id is None and error_class == ERROR_CLASS_SEND:
        logger.warning(
            "cron outbox: send-class enqueue for job %s WITHOUT execution_id "
            "(legacy loop-6 caller) — per-run idempotency disabled; productive "
            "run paths should carry job['execution_id']",
            job_id,
        )
    with _lock:
        path = _current_outbox_file()
        with _store_lock(path):
            entries = _read_entries(path)
            key = _dedupe_key(job_id, target)
            now = _hermes_now().isoformat()
            no_retry = error_class in _ERROR_CLASSES_NO_RETRY
            normalized_execution_id = str(execution_id) if execution_id else None
            for entry in entries:
                if _dedupe_key(entry.get("job_id"), entry.get("target")) != key:
                    continue
                if no_retry:
                    # Dead-on-arrival classes (audit loop 17 / F2): only a
                    # repeat of the SAME execution refreshes; a different
                    # execution appends so its payload is never overwritten.
                    # Legacy callers (no execution_id on either side) keep the
                    # loop-13 collapse — their key cannot distinguish runs.
                    if entry.get("error_class") != error_class:
                        continue
                    if entry.get("execution_id") != normalized_execution_id:
                        continue
                else:
                    if entry.get("status") != "queued":
                        continue
                    # Per-execution idempotency: only a refresh of the SAME
                    # run may collapse; different runs keep separate entries.
                    if execution_id is not None or entry.get("execution_id"):
                        if entry.get("execution_id") != execution_id:
                            continue
                entry["payload"] = payload
                entry["last_error"] = error
                entry["last_attempt_at"] = now
                _write_entries(path, entries)
                return entry
            entry = {
                "id": uuid.uuid4().hex[:16],
                "job_id": str(job_id),
                "execution_id": normalized_execution_id,
                "error_class": error_class,
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
                # Dead on arrival for no-retry classes: visible via
                # outbox_counts()["dead"], never returned by due_entries().
                "status": "dead" if no_retry else "queued",
                "attempts": 0,
                "first_failed_at": now,
                "last_attempt_at": now,
                # Due immediately: the first replay happens on the next tick
                # (transient blips recover in ~one tick); later retries back off.
                "next_retry_at": None if no_retry else now,
                "last_error": error,
            }
            entries.append(entry)
            if no_retry:
                # Anti-pile-up cap (audit loop 17 / F2): keep at most
                # MAX_DEAD_PER_TARGET dead letters per (job, target, class);
                # list order is append order, so the head is the oldest.
                siblings = [
                    e for e in entries
                    if e.get("error_class") == error_class
                    and _dedupe_key(e.get("job_id"), e.get("target")) == key
                ]
                overflow = len(siblings) - MAX_DEAD_PER_TARGET
                if overflow > 0:
                    drop_ids = {e["id"] for e in siblings[:overflow]}
                    entries = [e for e in entries if e.get("id") not in drop_ids]
            _write_entries(path, entries)
            return entry


def due_entries(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Return entries whose send is due (oldest first).

    Queued entries past their ``next_retry_at`` gate — plus, audit loop 17 /
    F3, ``sending`` entries whose lease has gone stale (holder crashed
    mid-send): those are orphaned and due for a retry. A ``sending`` entry
    with a FRESH lease belongs to a live process and is not returned.
    """
    with _lock:
        entries = _read_entries(_current_outbox_file())
    now = now or _hermes_now()
    due = []
    for entry in entries:
        if (entry.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        if entry.get("status") == "sending":
            if _lease_is_stale(entry, now):
                due.append(entry)
            continue
        if entry.get("status") != "queued":
            continue
        next_retry = _parse_ts(entry.get("next_retry_at"))
        if next_retry is None or next_retry <= now:
            due.append(entry)
    return due


def begin_replay(entry_id: str) -> Optional[Dict[str, Any]]:
    """Take the send lease on a due entry: persist ``sending`` BEFORE the send.

    Audit loop 17 / F3 — crash-visible sends. Transitions a ``queued`` entry
    (or a ``sending`` entry with a STALE lease — its holder died between the
    send and the status record) to ``sending``, stamping ``lease_ts``,
    ``lease_pid`` and the ``send_attempt`` number (attempts + 1). Returns a
    copy of the leased entry, or None when the entry is terminal/unknown or
    its FRESH lease belongs to a live process (skip — never double-send).
    """
    with _lock:
        path = _current_outbox_file()
        with _store_lock(path):
            entries = _read_entries(path)
            now = _hermes_now()
            for entry in entries:
                if entry.get("id") != entry_id:
                    continue
                status = entry.get("status")
                if status == "sending":
                    if not _lease_is_stale(entry, now):
                        return None  # fresh lease — a live process owns the send
                    # Orphaned lease: holder crashed mid-send. Retake below;
                    # if the original send DID land, the retry is the
                    # documented at-least-once redelivery.
                elif status != "queued":
                    return None  # delivered/dead — terminal
                entry["status"] = "sending"
                entry["lease_ts"] = now.isoformat()
                entry["lease_pid"] = os.getpid()
                entry["send_attempt"] = (entry.get("attempts") or 0) + 1
                _write_entries(path, entries)
                return dict(entry)
    return None


def record_replay_result(
    entry_id: str,
    *,
    success: bool,
    error: Optional[str] = None,
    receipt: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Record the outcome of one replay attempt.

    Success → ``delivered``; the delivery ``receipt`` (audit loop 17 / F3 —
    platform-side message_id when the send path returns one, else a payload
    hash + timestamp) is stored on the entry as visible proof. Failure
    increments ``attempts`` and either schedules the next retry (exponential
    backoff) or, past ``MAX_ATTEMPTS``, marks the entry ``dead`` (dead-letter;
    never retried again, never auto-pruned). Accepts entries in ``sending``
    (the normal post-``begin_replay`` flow) and ``queued`` (loop-6/13 legacy
    direct callers); the send-lease fields are cleared on the transition.
    Returns the updated entry, or None if the id is unknown.
    """
    with _lock:
        path = _current_outbox_file()
        with _store_lock(path):
            entries = _read_entries(path)
            now = _hermes_now()
            for entry in entries:
                if entry.get("id") != entry_id:
                    continue
                if entry.get("status") not in ("queued", "sending"):
                    return entry  # terminal or already transitioned — no rewrite
                entry["last_attempt_at"] = now.isoformat()
                for lease_field in ("lease_ts", "lease_pid", "send_attempt"):
                    entry.pop(lease_field, None)
                if success:
                    entry["status"] = "delivered"
                    entry["last_error"] = None
                    if receipt:
                        entry["receipt"] = receipt
                else:
                    entry["status"] = "queued"
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
