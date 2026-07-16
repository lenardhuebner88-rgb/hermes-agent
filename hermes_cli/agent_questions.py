"""Question-events store, scrape parser, and poll-ingest for the Frage-Assistent.

P0a: detect standing agent questions (select prompts / y-n) from tmux pane
tails, persist open events in a per-profile SQLite store, and expose them via
the dashboard API. Answering (send-keys) is P0b and out of scope here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import agent_terminals as _agent_terminals
from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Import question-window regexes from agent_terminals (do not duplicate; do not
# edit agent_terminals). Private names accessed as module attributes.
_QUESTION_YN_RE = _agent_terminals._QUESTION_YN_RE
_QUESTION_ALLOW_RE = _agent_terminals._QUESTION_ALLOW_RE
_QUESTION_DO_YOU_WANT_RE = _agent_terminals._QUESTION_DO_YOU_WANT_RE
_QUESTION_PRESS_ENTER_RE = _agent_terminals._QUESTION_PRESS_ENTER_RE
_QUESTION_NUMBERED_RE = _agent_terminals._QUESTION_NUMBERED_RE

_OPTION_LINE_RE = re.compile(r"^\s*(?:[❯›]\s*)?(\d+)\.\s+(.*)$")
_RECOMMENDED_MARKER_RE = re.compile(r"^\s*[❯›]\s*\d+\.")

_KIND_HINTS = ("claude", "codex", "kimi", "grok")

# ---------------------------------------------------------------------------
# Paths / schema
# ---------------------------------------------------------------------------


def question_events_db_path() -> Path:
    """Per-profile question events DB (``$HERMES_HOME/question_events.db``)."""
    return get_hermes_home() / "question_events.db"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS question_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    updated_ts    TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'scrape',
    session       TEXT NOT NULL,
    window        TEXT NOT NULL,
    pane_id       TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    kind          TEXT,
    cwd           TEXT,
    question_text TEXT NOT NULL,
    options_json  TEXT NOT NULL DEFAULT '[]',
    class         TEXT NOT NULL DEFAULT 'unknown',
    status        TEXT NOT NULL DEFAULT 'open',
    answered_by   TEXT,
    answer        TEXT,
    latency_s     REAL,
    override      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_question_events_status
    ON question_events(status);

CREATE INDEX IF NOT EXISTS idx_question_events_pane_status
    ON question_events(pane_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_question_events_open_pane_fp
    ON question_events(pane_id, fingerprint) WHERE status = 'open';
"""

_INITIALIZED_PATHS: set[str] = set()


def _iso_now(now: Optional[float] = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (and initialize if needed) the per-profile question_events DB."""
    path = db_path if db_path is not None else question_events_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="question_events.db")
        conn.execute("PRAGMA foreign_keys=ON")
        if resolved not in _INITIALIZED_PATHS:
            conn.executescript(SCHEMA_SQL)
            _INITIALIZED_PATHS.add(resolved)
    except Exception:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def connect_closing(db_path: Optional[Path] = None):
    """Open a question_events DB connection and guarantee it is closed on exit."""
    conn = connect(db_path=db_path)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Parser + fingerprint
# ---------------------------------------------------------------------------


def _recent_window_lines(tail: str) -> list[str]:
    """Last 8 raw lines — same window size as classify_agent_pane."""
    lines = (tail or "").splitlines()
    return lines[-8:]


def _normalized_region(lines: list[str]) -> str:
    """Rstrip each line and drop trailing empty lines."""
    stripped = [line.rstrip() for line in lines]
    while stripped and not stripped[-1].strip():
        stripped.pop()
    return "\n".join(stripped)


def _is_question_pattern(lines: list[str], last_non_empty: str) -> bool:
    return bool(_agent_terminals._is_question_window(lines, last_non_empty))


def _parse_numbered_options(lines: list[str]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for line in lines:
        m = _OPTION_LINE_RE.match(line)
        if not m:
            continue
        nr = int(m.group(1))
        label = m.group(2).strip()
        recommended = bool(_RECOMMENDED_MARKER_RE.match(line))
        options.append({"nr": nr, "label": label, "recommended": recommended})
    return options


def _bottom_option_block(lines: list[str]) -> tuple[int, int]:
    """Return ``(start, end)`` of the bottom-most contiguous option-line block.

    Empty lines break a block. The block's END must lie inside the last 8
    lines (the same window question detection uses) — otherwise a stale,
    scrolled-up select prompt above a fresh bottom question (e.g. y/n) would
    be parsed and fingerprinted instead of the question actually standing.
    The block's START may reach further up so long option lists survive.
    Returns ``(-1, -1)`` when none is found.
    """
    i = len(lines) - 1
    while i >= 0:
        while i >= 0 and not _OPTION_LINE_RE.match(lines[i]):
            i -= 1
        if i < 0:
            break
        end = i + 1
        while i >= 0 and _OPTION_LINE_RE.match(lines[i]):
            i -= 1
        start = i + 1
        # First hit walking bottom-up is the bottom-most contiguous block.
        if end - 1 < max(0, len(lines) - 8):
            return -1, -1
        return start, end
    return -1, -1


def _semantic_select_region(question_text: str, options: list[dict[str, Any]]) -> str:
    """Marker-insensitive region for select prompts (cursor moves do not churn)."""
    parts = [question_text] if question_text else []
    for opt in options:
        parts.append(f"{opt['nr']}. {opt['label']}")
    return "\n".join(parts)


def parse_question(tail: str) -> dict[str, Any] | None:
    """Parse a standing question from an ANSI-stripped capture tail.

    Returns ``None`` when no question pattern is present. On a pattern match,
    returns ``question_text``, ``options`` (list), and ``region`` (normalized
    text used for fingerprinting).

    Entry condition uses the last 8 lines (same as classify_agent_pane). Option
    blocks are recovered from the full tail so long select lists are not
    truncated; select fingerprints are marker-insensitive.
    """
    lines = (tail or "").splitlines()
    non_empty = [line for line in lines if line.strip()]
    last_non_empty = non_empty[-1] if non_empty else ""
    recent = lines[-8:]
    if not _is_question_pattern(recent, last_non_empty):
        return None

    opt_start, opt_end = _bottom_option_block(lines)
    options: list[dict[str, Any]] = []
    question_text = ""

    if opt_start >= 0:
        option_lines = lines[opt_start:opt_end]
        options = _parse_numbered_options(option_lines)
        for line in reversed(lines[:opt_start]):
            if not line.strip():
                continue
            if _OPTION_LINE_RE.match(line):
                continue
            question_text = line.strip()
            break

    if options:
        if not question_text:
            question_text = last_non_empty.strip() if last_non_empty else ""
        region = _semantic_select_region(question_text, options)
        return {
            "question_text": question_text,
            "options": options,
            "region": region,
        }

    # y/n and bare `?` prompts: region = normalized last-8 lines (unchanged).
    region = _normalized_region(recent)
    region_lines = region.splitlines() if region else []
    window = "\n".join(region_lines)
    if _QUESTION_YN_RE.search(window):
        options = [
            {"nr": "y", "label": "yes"},
            {"nr": "n", "label": "no"},
        ]
    if last_non_empty.rstrip().endswith("?"):
        question_text = last_non_empty.strip()
    elif non_empty:
        question_text = non_empty[-1].strip()

    if not question_text:
        question_text = last_non_empty.strip() if last_non_empty else ""

    return {
        "question_text": question_text,
        "options": options,
        "region": region,
    }


def compute_fingerprint(pane_id: str, region: str) -> str:
    """sha256 hex of ``pane_id + newline + normalized region``."""
    normalized = _normalized_region((region or "").splitlines())
    payload = f"{pane_id}\n{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _kind_from_command(command: str | None) -> str:
    cmd = (command or "").lower()
    for hint in _KIND_HINTS:
        if hint in cmd:
            return hint
    base = Path(command or "").name.lower()
    for hint in _KIND_HINTS:
        if hint in base:
            return hint
    return "unknown"


def _activity_age_s(window: dict[str, Any], now: float) -> float | None:
    activity = window.get("activity")
    if activity is None:
        return None
    try:
        return float(now) - float(activity)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def list_question_events(
    *,
    status: str = "open",
    limit: int = 50,
    db_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Return events newest-first; ``options_json`` decoded to ``options``."""
    limit = max(1, min(int(limit), 500))
    with connect_closing(db_path=db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM question_events WHERE status = ? "
                "ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM question_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_event(row) for row in rows]


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    raw = d.pop("options_json", "[]")
    try:
        options = json.loads(raw) if raw is not None else []
        if not isinstance(options, list):
            options = []
    except (TypeError, ValueError, json.JSONDecodeError):
        options = []
    d["options"] = options
    return d


def find_open_event(
    pane_id: str,
    fingerprint: str,
    *,
    db_path: Optional[Path] = None,
) -> dict[str, Any] | None:
    with connect_closing(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT * FROM question_events "
            "WHERE pane_id = ? AND fingerprint = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1",
            (pane_id, fingerprint),
        ).fetchone()
    return _row_to_event(row) if row is not None else None


def list_open_pane_ids(*, db_path: Optional[Path] = None) -> set[str]:
    """Return the set of pane_ids that currently have an open event."""
    with connect_closing(db_path=db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT pane_id FROM question_events WHERE status = 'open'"
        ).fetchall()
    return {str(row["pane_id"]) for row in rows}


def insert_question_event(
    *,
    session: str,
    window: str,
    pane_id: str,
    fingerprint: str,
    question_text: str,
    options: list[dict[str, Any]] | None = None,
    kind: str | None = None,
    cwd: str | None = None,
    source: str = "scrape",
    class_: str = "unknown",
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> int | None:
    """Insert a new open event; returns row id, or None if unique-index ignored."""
    ts = _iso_now(now)
    options_json = json.dumps(options if options is not None else [], ensure_ascii=False)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO question_events ("
                "ts, updated_ts, source, session, window, pane_id, fingerprint, "
                "kind, cwd, question_text, options_json, class, status, override"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0)",
                (
                    ts,
                    ts,
                    source,
                    session,
                    window,
                    pane_id,
                    fingerprint,
                    kind,
                    cwd,
                    question_text,
                    options_json,
                    class_,
                ),
            )
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid)


def supersede_and_insert(
    *,
    session: str,
    window: str,
    pane_id: str,
    fingerprint: str,
    question_text: str,
    options: list[dict[str, Any]] | None = None,
    kind: str | None = None,
    cwd: str | None = None,
    source: str = "scrape",
    class_: str = "unknown",
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> tuple[int, int | None]:
    """Supersede other open events on the pane and insert the new one in ONE
    transaction, so no interleaving writer can observe/leave two open rows
    for the same pane. Returns ``(superseded_count, new_id_or_None)``."""
    ts = _iso_now(now)
    options_json = json.dumps(options if options is not None else [], ensure_ascii=False)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            sup = conn.execute(
                "UPDATE question_events SET status = 'superseded', "
                "updated_ts = ? WHERE pane_id = ? AND status = 'open' "
                "AND fingerprint != ?",
                (ts, pane_id, fingerprint),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO question_events ("
                "ts, updated_ts, source, session, window, pane_id, fingerprint, "
                "kind, cwd, question_text, options_json, class, status, override"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0)",
                (
                    ts,
                    ts,
                    source,
                    session,
                    window,
                    pane_id,
                    fingerprint,
                    kind,
                    cwd,
                    question_text,
                    options_json,
                    class_,
                ),
            )
            new_id = int(cur.lastrowid) if cur.rowcount != 0 else None
            return int(sup.rowcount or 0), new_id


def expire_open_events(
    pane_ids_to_expire: set[str],
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> int:
    """Expire open events for the explicit pane_id set; returns rows touched."""
    if not pane_ids_to_expire:
        return 0
    ts = _iso_now(now)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            open_rows = conn.execute(
                "SELECT id, pane_id FROM question_events WHERE status = 'open'"
            ).fetchall()
            to_expire = [
                int(row["id"])
                for row in open_rows
                if row["pane_id"] in pane_ids_to_expire
            ]
            if not to_expire:
                return 0
            placeholders = ",".join("?" * len(to_expire))
            cur = conn.execute(
                f"UPDATE question_events SET status = 'expired', updated_ts = ? "
                f"WHERE id IN ({placeholders})",
                [ts, *to_expire],
            )
            return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Scrape ingestor
# ---------------------------------------------------------------------------


class QuestionScrapeIngestor:
    """Poll tmux overview for standing questions; write stable open events.

    Double-capture stability: an event is only inserted when the same
    fingerprint was already seen on the previous ``poll_once`` *and*
    ``activity_age > 3`` seconds. Expiry is also two-poll confirmed so a
    single empty/transient overview does not wipe open events.
    """

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        service_factory: Optional[Callable[[], Any]] = None,
        now: Optional[Callable[[], float]] = None,
        activity_age_threshold_s: float = 3.0,
        overview_tail_lines: int = 25,
        session_filter: str = "work",
    ) -> None:
        self.db_path = db_path
        self._service_factory = service_factory
        self._now = now or time.time
        self.activity_age_threshold_s = float(activity_age_threshold_s)
        self.overview_tail_lines = int(overview_tail_lines)
        self.session_filter = session_filter
        self._pending: dict[str, str] = {}
        self._expire_pending: set[str] = set()
        self._empty_snapshots: int = 0

    def _service(self) -> Any:
        if self._service_factory is not None:
            return self._service_factory()
        return _agent_terminals.TmuxAgentSessionService()

    def poll_once(self) -> dict[str, int]:
        """One scrape tick. Returns a counter summary for logs/tests."""
        summary = {
            "windows": 0,
            "frage": 0,
            "pending": 0,
            "created": 0,
            "idempotent": 0,
            "superseded": 0,
            "expired": 0,
            "unstable": 0,
            "skipped_age": 0,
            "parse_none": 0,
            "skipped_expiry_empty_snapshot": 0,
        }
        now = float(self._now())
        overview = self._service().overview(tail_lines=self.overview_tail_lines)
        windows = list(overview.get("windows") or [])
        overview_now = overview.get("now")
        if overview_now is not None:
            try:
                now = float(overview_now)
            except (TypeError, ValueError):
                pass

        summary["windows"] = len(windows)
        frage_panes: set[str] = set()
        next_pending: dict[str, str] = {}

        for win in windows:
            if not isinstance(win, dict):
                continue
            if win.get("session") != self.session_filter:
                continue
            if win.get("state") != "frage":
                continue

            pane_id = str(win.get("pane_id") or "")
            if not pane_id:
                continue

            summary["frage"] += 1
            tail = win.get("tail") or ""
            parsed = parse_question(str(tail))
            if parsed is None:
                summary["parse_none"] += 1
                continue

            fp = compute_fingerprint(pane_id, parsed["region"])
            age = _activity_age_s(win, now)

            if find_open_event(pane_id, fp, db_path=self.db_path) is not None:
                summary["idempotent"] += 1
                next_pending[pane_id] = fp
                frage_panes.add(pane_id)
                continue

            prev_fp = self._pending.get(pane_id)
            stable = prev_fp == fp
            age_ok = age is not None and age > self.activity_age_threshold_s

            if stable and age_ok:
                # Supersede only after the new fingerprint is stable (not on
                # first observation) so a 1-poll flicker cannot kill a valid
                # open; both writes share one transaction so no interleaving
                # writer can leave two open rows for the pane.
                n_super, new_id = supersede_and_insert(
                    session=str(win.get("session") or ""),
                    window=str(win.get("window") or ""),
                    pane_id=pane_id,
                    fingerprint=fp,
                    question_text=parsed["question_text"],
                    options=parsed["options"],
                    kind=_kind_from_command(
                        str(win.get("command") or "") if win.get("command") is not None else None
                    ),
                    cwd=str(win["cwd"]) if win.get("cwd") is not None else None,
                    db_path=self.db_path,
                    now=now,
                )
                summary["superseded"] += n_super
                if new_id is None:
                    summary["idempotent"] += 1
                else:
                    summary["created"] += 1
                next_pending[pane_id] = fp
                frage_panes.add(pane_id)
            else:
                next_pending[pane_id] = fp
                frage_panes.add(pane_id)
                if not stable:
                    summary["unstable"] += 1
                    summary["pending"] += 1
                elif not age_ok:
                    summary["skipped_age"] += 1
                    summary["pending"] += 1
                else:
                    summary["pending"] += 1

        self._pending = next_pending

        # Empty overview (e.g. transient tmux list_windows failure) must not
        # expire everything — skip the expiry passage. But a PERSISTENTLY
        # empty overview (>= 3 consecutive polls) means the windows are truly
        # gone; from then on the expiry passage runs again (still two-poll
        # confirmed via _expire_pending) so events cannot stay open forever.
        if len(windows) == 0:
            self._empty_snapshots += 1
            if self._empty_snapshots < 3:
                summary["skipped_expiry_empty_snapshot"] = 1
                return summary
        else:
            self._empty_snapshots = 0

        open_panes = list_open_pane_ids(db_path=self.db_path)
        candidates = open_panes - frage_panes
        to_expire = candidates & self._expire_pending
        if to_expire:
            summary["expired"] += expire_open_events(
                to_expire, db_path=self.db_path, now=now
            )
        self._expire_pending = candidates - to_expire
        return summary


# ---------------------------------------------------------------------------
# Background poller (web_server startup only)
# ---------------------------------------------------------------------------

_poller_lock = threading.Lock()
_poller_thread: threading.Thread | None = None
_poller_stop = threading.Event()
_default_ingestor: QuestionScrapeIngestor | None = None
_last_warn_mono: float = 0.0


def start_poller(interval_s: float = 5.0, *, db_path: Optional[Path] = None) -> bool:
    """Start the daemon scrape poller. Idempotent; kill-switch via env.

    Returns True if a new thread was started, False if skipped/already running.
    Does nothing when ``HERMES_AGENT_QUESTIONS_POLL=0``.
    """
    global _poller_thread, _default_ingestor

    env = os.environ.get("HERMES_AGENT_QUESTIONS_POLL")
    if env == "0":
        logger.info("agent_questions poller disabled (HERMES_AGENT_QUESTIONS_POLL=0)")
        return False
    # The app lifespan also runs inside `with TestClient(app)` blocks across the
    # test suite. A poller started there would scrape the REAL user tmux and —
    # after fixture teardown restores the env — write into the live
    # $HERMES_HOME store. Under pytest the poller therefore only starts when
    # explicitly forced with HERMES_AGENT_QUESTIONS_POLL=1.
    if env != "1" and "pytest" in sys.modules:
        logger.info("agent_questions poller skipped under pytest (set HERMES_AGENT_QUESTIONS_POLL=1 to force)")
        return False

    with _poller_lock:
        if _poller_thread is not None and _poller_thread.is_alive():
            return False

        _poller_stop.clear()
        # Pin the DB target at start time: the thread must not re-resolve
        # $HERMES_HOME later (env mutations, e.g. test fixtures, would
        # silently redirect writes).
        resolved_db_path = db_path if db_path is not None else question_events_db_path()
        ingestor = QuestionScrapeIngestor(db_path=resolved_db_path)
        _default_ingestor = ingestor
        interval = max(0.5, float(interval_s))

        def _loop() -> None:
            global _last_warn_mono
            while not _poller_stop.is_set():
                try:
                    ingestor.poll_once()
                except Exception:
                    mono = time.monotonic()
                    if mono - _last_warn_mono >= 60.0:
                        logger.warning(
                            "agent_questions poll_once failed",
                            exc_info=True,
                        )
                        _last_warn_mono = mono
                if _poller_stop.wait(interval):
                    break

        thread = threading.Thread(
            target=_loop,
            name="agent-questions-poller",
            daemon=True,
        )
        _poller_thread = thread
        thread.start()
        logger.info("agent_questions poller started (interval_s=%s)", interval)
        return True


def stop_poller() -> None:
    """Stop the daemon poller (tests / shutdown). Safe if never started."""
    global _poller_thread
    _poller_stop.set()
    with _poller_lock:
        thread = _poller_thread
        _poller_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
