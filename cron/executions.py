"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue.
Interrupted attempts become ``unknown`` only after their exact owner process is
proved gone. Terminal states are immutable.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# Like cron/jobs.py, the ledger is per-profile by design (#4707): each profile
# owns its own executions.db under its own HERMES_HOME, and multiplex_profiles
# scopes every tick/recovery to one profile at a time. EXECUTIONS_FILE remains
# the import-time default-profile fallback and a compatibility surface for
# existing callers/tests that deliberately re-point it; the ACTIVE store is
# resolved per call by _current_executions_file() — never read EXECUTIONS_FILE
# directly for I/O.
_IMPORT_HOME = get_hermes_home().resolve()
EXECUTIONS_FILE = _IMPORT_HOME / "cron" / "executions.db"

# Retention (see _prune_unlocked): keep the newest N terminal executions PER
# JOB so high-frequency jobs only evict their own history instead of pushing
# rare report jobs out of the ledger (the old global FIFO cap did exactly
# that). On top of the count window there is a TIME-based guarantee (loop 15
# / F2): a row beyond the per-job count is still kept while it falls inside
# the keep-days horizon (default 30 days) — and failed/unknown rows inside
# that horizon survive regardless of count, so error history outlives the
# success flood. A row is only deleted when it is beyond the count window
# AND older than the horizon. Since loop 18 the global cap is horizon-aware
# too: enforcing it may only delete rows already outside the keep-days
# horizon (oldest first, until the ledger is back under the cap), so the cap
# can no longer silently eat the time guarantee at high run volume. When the
# overflow consists entirely of in-horizon rows, nothing is deleted for the
# cap and a loud warning makes the overflow visible instead. Since loop 24
# (M3) that warning is logger-only (no print to stderr from library code)
# and time-deduplicated to at most one per hour per process — the overflow
# condition itself is still evaluated on every prune. All limits are
# DELETE-based and self-healing — changing any value takes effect on the
# next prune, no migration needed.
KEEP_TERMINAL_EXECUTIONS_PER_JOB = 50
KEEP_TERMINAL_EXECUTIONS_DAYS = 30
MAX_TERMINAL_EXECUTIONS = 20000
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex

# Loop 24 / M3: the horizon-cap overflow alarm used to fire on EVERY prune
# (i.e. every finish_execution) and additionally print() to stderr from
# library code. It is now logger-only and deduplicated: at most one warning
# per _CAP_OVERFLOW_WARNING_MIN_INTERVAL_SECONDS per process; the condition
# itself is still evaluated every prune, so nothing is hidden — only the
# repeat noise is. Tests may reset _last_cap_overflow_warning to None.
_last_cap_overflow_warning: Optional[float] = None
_CAP_OVERFLOW_WARNING_MIN_INTERVAL_SECONDS = 3600.0

# Import-time snapshot of the compatibility constant, so deliberate re-pointing
# (monkeypatched EXECUTIONS_FILE — the documented escape hatch existing
# tests/embedders use) is distinguishable from the constant merely being stale.
_IMPORT_EXECUTIONS_FILE = EXECUTIONS_FILE


def _current_executions_file() -> Path:
    """Return the ledger path pinned to this execution context's profile.

    Precedence mirrors cron/jobs.py::_current_cron_store(), most explicit
    first:

    1. an active cron-store override — the ContextVar behind
       cron.jobs.use_cron_store(), which multiplex_profiles sets per profile
       around tick/recovery/heartbeat;
    2. a deliberately re-pointed EXECUTIONS_FILE module constant — if it no
       longer matches its import-time value, someone chose the documented
       process-wide compatibility surface; honor it;
    3. the ACTIVE profile home, resolved fresh via get_hermes_home()
       (context-local override, then the HERMES_HOME env var) — so a caller
       that re-points HERMES_HOME after this module was imported writes ITS
       OWN ledger, not whatever executions.db the import happened to freeze;
    4. the import-time constant (home unchanged since import — the common
       path, returned unchanged).
    """
    # Lazy import: cron/jobs.py imports this module lazily too, so a
    # module-level import here would be circular.
    from cron.jobs import _cron_store_override

    store = _cron_store_override.get()
    if store is not None:
        return store.cron_dir / "executions.db"
    if EXECUTIONS_FILE != _IMPORT_EXECUTIONS_FILE:
        return EXECUTIONS_FILE
    home = get_hermes_home().resolve()
    if home == _IMPORT_HOME:
        return EXECUTIONS_FILE
    return home / "cron" / "executions.db"


@contextmanager
def use_executions_store(home: Union[str, Path]) -> Iterator[None]:
    """Route the executions ledger to ``home`` without mutating globals.

    Thin alias over cron.jobs.use_cron_store() so a single context manager
    scopes the jobs store, output dir, and executions ledger together.
    """
    from cron.jobs import use_cron_store

    with use_cron_store(home):
        yield


def _int_env_override(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r (expected integer); using %d",
            name, raw, fallback,
        )
        return fallback


def _keep_terminal_per_job() -> int:
    return max(
        0,
        _int_env_override(
            "HERMES_CRON_EXECUTIONS_KEEP_PER_JOB",
            KEEP_TERMINAL_EXECUTIONS_PER_JOB,
        ),
    )


def _max_terminal_executions() -> int:
    return max(
        0,
        _int_env_override(
            "HERMES_CRON_EXECUTIONS_MAX_TERMINAL", MAX_TERMINAL_EXECUTIONS
        ),
    )


def _keep_terminal_days() -> int:
    """Time-based retention horizon in days (loop 15 / F2).

    0 disables the guarantee (prune reverts to pure count-based retention).
    """
    return max(
        0,
        _int_env_override(
            "HERMES_CRON_EXECUTIONS_KEEP_DAYS", KEEP_TERMINAL_EXECUTIONS_DAYS
        ),
    )


def _connect() -> sqlite3.Connection:
    executions_file = _current_executions_file()
    executions_file.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(executions_file, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(conn, db_label="cron/executions.db")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    # Loop 24 / M4: expression indexes on the julianday() ordering the
    # read/prune paths actually use (list_executions, latest_executions,
    # _prune_unlocked's window ORDER BY). Without them every julianday-ordered
    # query computes the expression per row and sorts from a full scan —
    # the plain claimed_at indexes above cannot serve an ORDER BY over an
    # expression. IF NOT EXISTS ⇒ no migration; existing stores pick the
    # indexes up on their next connect.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_jd "
        "ON executions(job_id, julianday(claimed_at) DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_jd "
        "ON executions(julianday(claimed_at) DESC, id DESC)"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back
    the transaction; it does not close the connection. Relying on that alone
    leaks a connection (and its WAL/SHM file descriptors) on every call,
    since closing then depends on the garbage collector. Schema init runs
    inside the ``try`` too, so a PRAGMA/DDL failure after a successful
    ``connect()`` still closes the connection instead of leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    """Trim terminal history: per-job retention first, then the global cap.

    Per-job retention keeps the newest N terminal executions of EVERY job, so
    high-frequency jobs evict only their own history and can no longer push
    rare report jobs out of the ledger. Since loop 15 (F2) a row beyond that
    count window is deleted only when ALL of these hold:
      (a) it sits beyond the per-job keep-N window (rn > N), AND
      (b) it is older than the keep-days horizon (default 30 days), AND
      (c) it is not a failed/unknown row inside that horizon (error history
          survives the success flood for the same horizon).
    With a single shared horizon (c) is implied by (b); it stays explicit so
    a future, longer error horizon only has to change one parameter. Setting
    HERMES_CRON_EXECUTIONS_KEEP_DAYS=0 reverts to pure count-based pruning.
    In-flight (claimed/running) rows are never pruned.

    All time comparisons go through SQLite's julianday() (loop 18 / F1): the
    stored claimed_at is a tz-aware ISO string whose offset varies with the
    configured/local timezone, so a raw string comparison misorders mixed
    offsets; julianday() normalizes every ISO-8601 variant (offsets, 'Z',
    space separator) onto one UTC scale. Unparseable values yield NULL, which
    fails safe toward KEEPING the row (both ``<`` comparisons come out NULL).

    Since loop 18 the global cap is horizon-aware: enforcing it may only
    delete terminal rows that are ALREADY older than the keep-days horizon,
    so the time-based guarantee holds even at high run volume (the loop-15
    cap deleted regardless of age and ate the guarantee exactly where it was
    needed). When the ledger exceeds the cap, the OLDEST out-of-horizon rows
    are deleted until the ledger is back under the cap or no deletable rows
    remain; if it still overflows — i.e. the excess consists entirely of
    in-horizon rows — nothing further is deleted and a loud warning makes
    the overflow visible so the operator can raise
    HERMES_CRON_EXECUTIONS_MAX_TERMINAL or shorten the horizon. Since loop
    24 (M3) the warning is logger-only (no print to stderr from library
    code) and time-deduplicated to at most one per hour per process — the
    overflow condition itself is still evaluated on EVERY prune, only the
    repeat noise is deduplicated.
    With KEEP_DAYS=0 the horizon is "now" and the cap degenerates to the old
    pure count-based global bound.
    """
    cutoff = (_hermes_now() - timedelta(days=_keep_terminal_days())).isoformat()
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM (
               SELECT id, claimed_at, status,
                      ROW_NUMBER() OVER (
                        PARTITION BY job_id
                        ORDER BY julianday(claimed_at) DESC, id DESC
                      ) AS rn
               FROM executions
               WHERE status IN ('completed','failed','unknown')
             ) WHERE rn > ?
               AND julianday(claimed_at) < julianday(?)
               AND NOT (status IN ('failed','unknown')
                        AND julianday(claimed_at) >= julianday(?))
           )""",
        (_keep_terminal_per_job(), cutoff, cutoff),
    )
    cap = _max_terminal_executions()
    overflow = conn.execute(
        """SELECT COUNT(*) FROM executions
           WHERE status IN ('completed','failed','unknown')"""
    ).fetchone()[0] - cap
    if overflow > 0:
        conn.execute(
            """DELETE FROM executions WHERE id IN (
                 SELECT id FROM executions
                 WHERE status IN ('completed','failed','unknown')
                   AND julianday(claimed_at) < julianday(?)
                 ORDER BY julianday(claimed_at) ASC, id ASC LIMIT ?
               )""",
            (cutoff, overflow),
        )
        remaining = conn.execute(
            """SELECT COUNT(*) FROM executions
               WHERE status IN ('completed','failed','unknown')"""
        ).fetchone()[0]
        if remaining > cap:
            # Loop 24 / M3: logger-only (no print to stderr from library
            # code) and time-deduplicated per process — finish_execution
            # prunes on every run, so an undrained overflow used to warn
            # (and print) on every single job finish.
            global _last_cap_overflow_warning
            now_mono = time.monotonic()
            if (
                _last_cap_overflow_warning is None
                or now_mono - _last_cap_overflow_warning
                >= _CAP_OVERFLOW_WARNING_MIN_INTERVAL_SECONDS
            ):
                _last_cap_overflow_warning = now_mono
                logger.warning(
                    "cron executions ledger holds %d terminal rows, "
                    "exceeding the global cap of %d; the excess is entirely "
                    "inside the keep-days horizon, so no rows were deleted for "
                    "the cap (the time-based retention guarantee wins). Raise "
                    "HERMES_CRON_EXECUTIONS_MAX_TERMINAL or lower "
                    "HERMES_CRON_EXECUTIONS_KEEP_DAYS to bound the ledger "
                    "again. (This warning repeats at most once per hour.)",
                    remaining, cap,
                )


def create_execution(job_id: str, *, source: str) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now),
        )
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
    return _record(row)  # type: ignore[return-value]


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='running', started_at=?
               WHERE id=? AND status='claimed'""",
            (now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status=?, finished_at=?, error=?
               WHERE id=? AND status IN ('claimed','running')""",
            (status, now, detail, execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    changed = 0
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, process_id, pid, process_started_at FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            cur = conn.execute(
                """UPDATE executions SET status='unknown', finished_at=?, error=?
                   WHERE id=? AND status IN ('claimed','running')""",
                (now,
                 "Scheduler restarted after this execution's owner exited before a durable "
                 "terminal state; whether side effects ran is unknown.",
                 row["id"]),
            )
            changed += cur.rowcount
        if changed:
            _prune_unlocked(conn)
    return changed


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50,
    before_claimed_at: Optional[str] = None,
    before_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination.

    Ordering AND the cursor comparison both go through SQLite julianday()
    (loop 20 / F4 — same reasoning as the loop-18 prune): the stored
    claimed_at is a tz-aware ISO string whose offset varies with the
    configured/local timezone, so a raw string sort/cursor misorders mixed
    offsets; julianday() normalizes every ISO-8601 variant onto one UTC
    scale. The cursor contract stays "strictly older than the last row of
    the previous page" — pass that row's claimed_at verbatim.

    Composite cursor (audit loop 23b / F1): the ORDER BY already has an
    ``id DESC`` tiebreak (``id`` is a TEXT uuid — the tiebreak order is
    lexicographic, not insertion order, but still a total order), yet a
    ``before_claimed_at``-only cursor drops rows whose
    julianday(claimed_at) is IDENTICAL to the cursor row's — page 2 skipped
    those ties. Pass the last row's ``id`` verbatim as ``before_id``
    alongside its claimed_at and the comparison becomes
    ``(jd(claimed_at), id) < (jd(cursor_claimed_at), cursor_id)`` — ties
    continue onto the next page in id order, with no gap and no duplicate.
    ``before_id`` without ``before_claimed_at`` is ignored (there is no
    time component to compare against), and callers that keep passing only
    ``before_claimed_at`` get the old strictly-older behavior unchanged.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        if before_id is not None:
            clauses.append(
                "(julianday(claimed_at) < julianday(?)"
                " OR (julianday(claimed_at) = julianday(?) AND id < ?))"
            )
            params.extend([str(before_claimed_at), str(before_claimed_at),
                           str(before_id)])
        else:
            clauses.append("julianday(claimed_at) < julianday(?)")
            params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY julianday(claimed_at) DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query.

    "Latest" is decided on the julianday() scale (loop 20 / F4) — identical
    ordering to list_executions(), so the per-job top row here always
    matches the head of the paginated list even with mixed-offset
    claimed_at values.
    """
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY julianday(e2.claimed_at) DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
