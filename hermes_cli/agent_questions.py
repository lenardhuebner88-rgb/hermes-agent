"""Question-events store, scrape parser, poll-ingest, and answer path.

P0a: detect standing agent questions (select prompts / y-n) from tmux pane
tails, persist open events in a per-profile SQLite store, and expose them via
the dashboard API.

P0b: claim → fingerprint-recheck → pane-addressed send-keys → verify.
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
from hermes_cli.sqlite_util import add_column_if_missing, write_txn
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

# kind -> how an option answer is delivered (select digit vs y/n).
# Central table so live CLI dialect differences have one audit point.
ANSWER_DIALECTS: dict[str, dict[str, dict[str, bool]]] = {
    "claude": {"select": {"enter": False}, "yn": {"enter": False}},
    "codex": {"select": {"enter": True}, "yn": {"enter": True}},
    "default": {"select": {"enter": False}, "yn": {"enter": True}},
}

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
    answer_verified INTEGER,
    override      INTEGER NOT NULL DEFAULT 0,
    action_context TEXT,
    action_payload TEXT,
    action_result TEXT,
    hook_key      TEXT
);

CREATE INDEX IF NOT EXISTS idx_question_events_status
    ON question_events(status);

CREATE INDEX IF NOT EXISTS idx_question_events_pane_status
    ON question_events(pane_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_question_events_open_pane_fp
    ON question_events(pane_id, fingerprint) WHERE status = 'open';

-- Small shared key/value store (visibility heartbeat, push debounce queue).
-- Lives in the same DB so web process and poller process share state without
-- a second DB file (I3).
CREATE TABLE IF NOT EXISTS question_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_ts  TEXT NOT NULL
);
"""

# Live DBs may predate I2 additive columns — migrate on connect.
_SCHEMA_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("action_context", "action_context TEXT"),
    ("action_payload", "action_payload TEXT"),
    ("action_result", "action_result TEXT"),
    ("hook_key", "hook_key TEXT"),
    ("suggestions_json", "suggestions_json TEXT"),
    ("suggested_by", "suggested_by TEXT"),
    ("suggested_ts", "suggested_ts TEXT"),
    ("suggest_latency_ms", "suggest_latency_ms REAL"),
    ("suggest_confidence", "suggest_confidence TEXT"),
    ("answer_source", "answer_source TEXT"),
)

_PANE_ID_RE = re.compile(r"^%\d+$")
_CAPTURE_GONE_MARKERS: tuple[str, ...] = (
    "can't find",
    "no such window",
    "no such pane",
    "no such session",
    "no server running",
)

_INITIALIZED_PATHS: set[str] = set()


def _iso_now(now: Optional[float] = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ``pa_action`` rows deliberately keep the legacy NOT NULL pane columns.  The
# enqueue path uses the stable ``pa`` sentinel for session/window/pane_id, so
# the existing partial unique index remains the idempotency primitive without a
# table rebuild on live question databases.
PA_ACTION_SENTINEL = "pa"
PA_ACTION_VERSION = 1
_PA_ACTION_SCHEMAS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "tmux.send_keys": (("session", "window", "keys"), ()),
    "tmux.interrupt": (("session", "window"), ()),
    "kanban.unblock": (("card_id",), ("reason",)),
    "kanban.nudge": (("card_id",), ("reason",)),
    "kanban.hold": (("card_id",), ("reason",)),
    "kanban.resume": (("card_id",), ("reason",)),
    "kanban.kill": (("card_id",), ("reason",)),
    "kanban.release": (("card_id",), ("reason",)),
    "planspec.ingest": (("draft_id",), ("reason",)),
    "loops.start_pack": (("pack",), ("model", "max_rounds", "reason")),
    "loops.status": ((), ("pack",)),
}


def normalize_pa_action_payload(category: str, payload: Any) -> dict[str, Any]:
    """Validate and normalize one v1 action payload.

    Every category is a closed schema: unknown/missing fields and wrong value
    types are rejected before an event can be enqueued.  ``keys`` preserves its
    exact text (including surrounding whitespace); identifiers/reasons are
    stripped because whitespace is not part of their identity.  The sole v1
    numeric field, ``loops.start_pack.max_rounds``, is a positive integer.
    """
    category_s = str(category or "").strip()
    schema = _PA_ACTION_SCHEMAS.get(category_s)
    if schema is None:
        raise ValueError(f"Unbekannte pa_action-Kategorie: {category_s or '<leer>'}")
    if not isinstance(payload, dict):
        raise ValueError(f"Payload für {category_s} muss ein JSON-Objekt sein")

    required, optional = schema
    allowed = set(required) | set(optional)
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ValueError(
            f"Payload für {category_s} enthält unbekannte Felder: {', '.join(unknown)}"
        )
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(
            f"Payload für {category_s} fehlt: {', '.join(missing)}"
        )

    normalized: dict[str, Any] = {}
    for key in (*required, *optional):
        if key not in payload:
            continue
        value = payload[key]
        if category_s == "loops.start_pack" and key == "max_rounds":
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= 50
            ):
                raise ValueError(
                    "Payload-Feld max_rounds für loops.start_pack muss eine "
                    "Ganzzahl zwischen 1 und 50 sein"
                )
            normalized[key] = value
            continue
        if not isinstance(value, str):
            raise ValueError(f"Payload-Feld {key} für {category_s} muss Text sein")
        if not value.strip():
            raise ValueError(f"Payload-Feld {key} für {category_s} darf nicht leer sein")
        normalized[key] = value if key == "keys" else value.strip()
    return normalized


def build_pa_action_envelope(
    category: str,
    payload: Any,
    *,
    reason: str | None,
) -> dict[str, Any]:
    """Return the canonical JSON envelope stored in ``action_payload``."""
    category_s = str(category or "").strip()
    normalized_payload = normalize_pa_action_payload(category_s, payload)
    if reason is not None and not isinstance(reason, str):
        raise ValueError("pa_action reason muss Text sein")
    reason_s = reason.strip() if isinstance(reason, str) else ""
    return {
        "version": PA_ACTION_VERSION,
        "category": category_s,
        "payload": normalized_payload,
        "reason": reason_s or None,
    }


def normalize_pa_action_envelope(value: Any) -> dict[str, Any]:
    """Validate a stored/enqueue-time action envelope and canonicalize it."""
    if not isinstance(value, dict):
        raise ValueError("pa_action action_payload muss ein JSON-Objekt sein")
    allowed = {"version", "category", "payload", "reason"}
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValueError(
            "pa_action action_payload enthält unbekannte Felder: " + ", ".join(unknown)
        )
    if value.get("version") != PA_ACTION_VERSION:
        raise ValueError(f"pa_action version muss {PA_ACTION_VERSION} sein")
    if "category" not in value or "payload" not in value:
        raise ValueError("pa_action action_payload braucht category und payload")
    return build_pa_action_envelope(
        value["category"],
        value["payload"],
        reason=value.get("reason"),
    )


def pa_action_fingerprint(category: str, payload: Any) -> str:
    """Stable identity for an executable category+payload (reason excluded)."""
    category_s = str(category or "").strip()
    normalized = normalize_pa_action_payload(category_s, payload)
    canonical = json.dumps(
        {
            "version": PA_ACTION_VERSION,
            "category": category_s,
            "payload": normalized,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "pa:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _action_payload_json(kind: str | None, action_payload: Any) -> str | None:
    if kind == "pa_action":
        envelope = normalize_pa_action_envelope(action_payload)
        return json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if action_payload is not None:
        raise ValueError("action_payload ist nur für kind='pa_action' erlaubt")
    return None


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent additive columns for live P0 DBs (no data loss).

    Uses ``add_column_if_missing`` so two PROCESSES migrating concurrently
    (dashboard poller + a CLI connect) cannot fail each other on the
    "duplicate column name" race of a raw ALTER.
    """
    for column, ddl in _SCHEMA_COLUMN_MIGRATIONS:
        add_column_if_missing(conn, "question_events", column, ddl)


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
        # Always migrate: live DBs existed before I2 columns; CREATE IF NOT EXISTS
        # does not add columns to an already-created table.
        _ensure_schema_migrations(conn)
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


def compute_hook_fingerprint(
    question_text: str, options: list[dict[str, Any]] | None
) -> str:
    """Hook-source fingerprint — own namespace, not shared with scrape.

    Format: ``hook:`` + sha256(question_text + canonical option list).
    Merge across sources is pane-scoped, never fingerprint-equality.
    """
    canon_opts: list[dict[str, Any]] = []
    for opt in options or []:
        if not isinstance(opt, dict):
            continue
        canon_opts.append(
            {
                "nr": opt.get("nr"),
                "label": str(opt.get("label") or ""),
                "recommended": bool(opt.get("recommended")),
            }
        )
    canon = json.dumps(canon_opts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(f"{question_text or ''}{canon}".encode("utf-8")).hexdigest()
    return f"hook:{digest}"


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
    raw_suggestions = d.pop("suggestions_json", None)
    if raw_suggestions is None:
        suggestions = None
    else:
        try:
            parsed = json.loads(raw_suggestions)
            suggestions = parsed if isinstance(parsed, list) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            suggestions = None
    d["suggestions"] = suggestions
    raw_action_payload = d.pop("action_payload", None)
    if raw_action_payload is None:
        action_payload = None
    else:
        try:
            parsed_payload = json.loads(raw_action_payload)
            action_payload = parsed_payload if isinstance(parsed_payload, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            action_payload = None
    d["action_payload"] = action_payload
    raw_action_result = d.pop("action_result", None)
    if raw_action_result is None:
        action_result = None
    else:
        try:
            parsed_result = json.loads(raw_action_result)
            action_result = parsed_result if isinstance(parsed_result, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            action_result = None
    d["action_result"] = action_result
    return d


def get_meta(key: str, *, db_path: Optional[Path] = None) -> str | None:
    """Read a value from the shared ``question_meta`` table."""
    with connect_closing(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT value FROM question_meta WHERE key = ?",
            (str(key),),
        ).fetchone()
    if row is None:
        return None
    return str(row["value"]) if row["value"] is not None else None


def set_meta(
    key: str,
    value: str,
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> None:
    """Upsert a value into the shared ``question_meta`` table."""
    ts = _iso_now(now)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            conn.execute(
                "INSERT INTO question_meta (key, value, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_ts = excluded.updated_ts",
                (str(key), str(value), ts),
            )


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


def list_open_events(*, db_path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Return all open events (any source), newest-first."""
    with connect_closing(db_path=db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM question_events WHERE status = 'open' ORDER BY id DESC"
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def find_open_hook_event(
    pane_id: str,
    *,
    db_path: Optional[Path] = None,
) -> dict[str, Any] | None:
    """Return an open ``source='hook'`` event for ``pane_id``, if any."""
    with connect_closing(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT * FROM question_events "
            "WHERE pane_id = ? AND status = 'open' AND source = 'hook' "
            "ORDER BY id DESC LIMIT 1",
            (pane_id,),
        ).fetchone()
    return _row_to_event(row) if row is not None else None


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
    action_context: str | None = None,
    action_payload: dict[str, Any] | None = None,
    hook_key: str | None = None,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> int | None:
    """Insert a new open event; returns row id, or None if unique-index ignored."""
    ts = _iso_now(now)
    options_json = json.dumps(options if options is not None else [], ensure_ascii=False)
    action_payload_json = _action_payload_json(kind, action_payload)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO question_events ("
                "ts, updated_ts, source, session, window, pane_id, fingerprint, "
                "kind, cwd, question_text, options_json, class, status, override, "
                "action_context, action_payload, hook_key"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?, ?)",
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
                    action_context,
                    action_payload_json,
                    hook_key,
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
    action_context: str | None = None,
    action_payload: dict[str, Any] | None = None,
    hook_key: str | None = None,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> tuple[int, int | None]:
    """Supersede other open events on the pane and insert the new one in ONE
    transaction, so no interleaving writer can observe/leave two open rows
    for the same pane. Returns ``(superseded_count, new_id_or_None)``.

    Hook ingest does NOT use this helper — ``ingest_hook_event`` has its own
    transaction (supersedes ALL open rows on the pane, dedups by hook_key).
    """
    ts = _iso_now(now)
    options_json = json.dumps(options if options is not None else [], ensure_ascii=False)
    action_payload_json = _action_payload_json(kind, action_payload)
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
                "kind, cwd, question_text, options_json, class, status, override, "
                "action_context, action_payload, hook_key"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?, ?)",
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
                    action_context,
                    action_payload_json,
                    hook_key,
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


def recently_answered(
    pane_id: str,
    fingerprint: str,
    *,
    within_s: float = 60.0,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """True when pane+fingerprint was answered with ``updated_ts`` within ``within_s``.

    Blocks re-insert of the same standing prompt right after claim (send can take
    seconds; the next poller tick must not open a duplicate event).
    """
    now_ts = time.time() if now is None else float(now)
    with connect_closing(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT updated_ts FROM question_events "
            "WHERE pane_id = ? AND fingerprint = ? AND status = 'answered' "
            "ORDER BY id DESC LIMIT 1",
            (pane_id, fingerprint),
        ).fetchone()
    if row is None:
        return False
    try:
        updated = _parse_iso_ts(str(row["updated_ts"] or ""))
    except (TypeError, ValueError):
        return False
    return (now_ts - updated) < float(within_s)


# ---------------------------------------------------------------------------
# Hook-source ingest / resolve (I2)
# ---------------------------------------------------------------------------


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _hook_question_needle(question_text: str) -> str:
    """First ~80 chars of whitespace-normalized, ANSI-stripped question text."""
    cleaned = _normalize_ws(_agent_terminals.strip_ansi(question_text or ""))
    return cleaned[:80]


def hook_question_still_present(
    question_text: str,
    raw_capture: str,
    *,
    options: list[dict[str, Any]] | None = None,
) -> bool:
    """True when the hook question_text substring is still in the pane capture.

    Hook expiry must NOT use ``parse_question`` — real Claude Code prompts
    fail the scrape options-block heuristic.

    With ``options`` given (answer-path recheck), every option label must also
    be present: a re-asked question with the SAME first-80-chars but DIFFERENT
    options must fail the recheck (else a digit maps to the wrong option in
    the ~2s window before the re-ask's fire-and-forget ingest lands). Expiry
    stays on the question needle alone — lenient, softened by two-poll strike.
    Wrong direction here is safe: refusal (superseded) instead of wrong send.
    """
    needle = _hook_question_needle(question_text)
    if not needle:
        return False
    hay = _normalize_ws(_agent_terminals.strip_ansi(raw_capture or ""))
    if needle not in hay:
        return False
    for opt in options or []:
        label = _normalize_ws(str(opt.get("label") or ""))[:40]
        if label and label not in hay:
            return False
    return True


def _is_gone_capture_error(exc: BaseException) -> bool:
    """True when capture failed because the pane/session is gone (strike).

    ``subprocess.CalledProcessError`` puts the tmux message in ``stderr``, not
    in ``str(exc)`` (which is only the command line) — check both.
    """
    parts = [str(exc)]
    stderr = getattr(exc, "stderr", None)
    if stderr:
        parts.append(str(stderr))
    msg = " ".join(parts).lower()
    return any(marker in msg for marker in _CAPTURE_GONE_MARKERS)


def _validate_hook_payload(event: dict[str, Any]) -> str | None:
    """Return a reason string when invalid, else None."""
    if not isinstance(event, dict):
        return "invalid-payload"
    pane_id = str(event.get("pane_id") or "")
    if not _PANE_ID_RE.fullmatch(pane_id):
        return "invalid-payload"
    question_text = event.get("question_text")
    if not isinstance(question_text, str) or not question_text.strip():
        return "invalid-payload"
    options = event.get("options")
    if options is None:
        options = []
    if not isinstance(options, list):
        return "invalid-payload"
    return None


def ingest_hook_event(
    event: dict[str, Any],
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Ingest a pre-converted hook-source question into the store.

    Input is store-near (hook script already converted CC payload)::

        {pane_id, session, window, kind, cwd, question_text, options,
         action_context, hook_key}

    Atomic merge supersedes any open event on the same pane. Open events with
    the same ``hook_key`` are idempotent no-ops.
    """
    if _validate_hook_payload(event) is not None:
        return {"ok": False, "reason": "invalid-payload"}

    pane_id = str(event["pane_id"])
    question_text = str(event["question_text"])
    options_raw = event.get("options")
    options: list[dict[str, Any]] = list(options_raw) if isinstance(options_raw, list) else []
    # Normalize option dicts for storage (tolerates partial shapes).
    norm_opts: list[dict[str, Any]] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        norm_opts.append(
            {
                "nr": opt.get("nr"),
                "label": str(opt.get("label") or ""),
                "recommended": bool(opt.get("recommended")),
            }
        )
    hook_key = event.get("hook_key")
    hook_key_s = str(hook_key) if hook_key is not None and str(hook_key) != "" else None
    action_context = event.get("action_context")
    action_context_s = (
        str(action_context) if action_context is not None and str(action_context) != "" else None
    )
    session = str(event.get("session") or "")
    window = str(event.get("window") or "")
    kind = event.get("kind")
    kind_s = str(kind) if kind is not None else None
    cwd = event.get("cwd")
    cwd_s = str(cwd) if cwd is not None else None
    fp = compute_hook_fingerprint(question_text, norm_opts)
    ts = _iso_now(now)
    options_json = json.dumps(norm_opts, ensure_ascii=False)

    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            if hook_key_s is not None:
                # Dedup regardless of status: tool_use_ids are globally unique,
                # so ANY prior row with this hook_key means this PreToolUse was
                # already ingested — a re-POST must not resurrect an event that
                # the dashboard or resolve path already closed.
                existing = conn.execute(
                    "SELECT id FROM question_events "
                    "WHERE hook_key = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (hook_key_s,),
                ).fetchone()
                if existing is not None:
                    return {"ok": True, "deduped": True, "id": int(existing["id"])}

            conn.execute(
                "UPDATE question_events SET status = 'superseded', "
                "updated_ts = ? WHERE pane_id = ? AND status = 'open'",
                (ts, pane_id),
            )
            cur = conn.execute(
                "INSERT INTO question_events ("
                "ts, updated_ts, source, session, window, pane_id, fingerprint, "
                "kind, cwd, question_text, options_json, class, status, override, "
                "action_context, hook_key"
                ") VALUES (?, ?, 'hook', ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', 'open', 0, ?, ?)",
                (
                    ts,
                    ts,
                    session,
                    window,
                    pane_id,
                    fp,
                    kind_s,
                    cwd_s,
                    question_text,
                    options_json,
                    action_context_s,
                    hook_key_s,
                ),
            )
            new_id = int(cur.lastrowid)
    # Push outside the write txn (sender may touch kanban DB).
    try:
        from hermes_cli.agent_question_push import maybe_push_question

        maybe_push_question(new_id, db_path=db_path, now=now)
    except Exception:
        logger.warning(
            "ingest_hook_event push hook failed event_id=%s", new_id, exc_info=True
        )
    try:
        from hermes_cli.agent_question_suggest import schedule_question_suggestion

        schedule_question_suggestion(new_id, db_path=db_path)
    except Exception:
        logger.warning(
            "ingest_hook_event suggestion hook failed event_id=%s", new_id, exc_info=True
        )
    return {"ok": True, "id": new_id}


def resolve_hook_event(
    hook_key: str,
    answer: str | None = None,
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Mark an open hook-sourced event answered (terminal-side resolve).

    No open event (already answered via dashboard / expired) → no-op success
    with ``resolved: False``. Atomic single UPDATE WHERE status='open'.
    """
    key = str(hook_key or "").strip()
    if not key:
        return {"ok": True, "resolved": False}

    now_ts = time.time() if now is None else float(now)
    ts = _iso_now(now_ts)
    answer_s = None if answer is None else str(answer)

    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            row = conn.execute(
                "SELECT id, ts FROM question_events "
                "WHERE status = 'open' AND hook_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if row is None:
                return {"ok": True, "resolved": False}
            event_id = int(row["id"])
            try:
                latency_s = max(0.0, now_ts - _parse_iso_ts(str(row["ts"] or "")))
            except (TypeError, ValueError):
                latency_s = 0.0
            # Resolve-signal (PostToolUse) is authoritative verification for
            # hook-source events — CC often still echoes the question text, so
            # a text-disappear check would false-negative (I3 Mini #3).
            # But only with a real answer: PostToolUse can fire with an empty
            # answers payload (Esc-Abbruch, versionsabhängig) — closing is
            # correct then, a verified-stamp is not (Kimi review m5).
            verified_flag = 1 if answer_s else 0
            cur = conn.execute(
                "UPDATE question_events SET status = 'answered', "
                "answered_by = 'terminal', answer = ?, latency_s = ?, "
                "answer_verified = ?, answer_source = 'terminal', updated_ts = ? "
                "WHERE id = ? AND status = 'open'",
                (answer_s, float(latency_s), verified_flag, ts, event_id),
            )
            if int(cur.rowcount or 0) != 1:
                return {"ok": True, "resolved": False}
            return {
                "ok": True,
                "resolved": True,
                "id": event_id,
                "latency_s": float(latency_s),
                "verified": bool(verified_flag),
            }


# ---------------------------------------------------------------------------
# Answer path (P0b): claim → recheck → send → verify
# ---------------------------------------------------------------------------

# Ingest and recheck must share the same capture window so fingerprints match.
_CAPTURE_TAIL_LINES = 25


def _load_event(event_id: int, *, db_path: Optional[Path] = None) -> dict[str, Any] | None:
    with connect_closing(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT * FROM question_events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
    return _row_to_event(row) if row is not None else None


def _parse_iso_ts(ts: str) -> float:
    """Parse store ISO-UTC (…Z) into epoch seconds."""
    text = (ts or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def _option_nrs(options: list[Any]) -> list[str]:
    nrs: list[str] = []
    for opt in options or []:
        if isinstance(opt, dict) and "nr" in opt:
            nrs.append(str(opt["nr"]))
    return nrs


def _answer_enter_flag(kind: str | None, answer: str) -> bool:
    dialect = ANSWER_DIALECTS.get(kind or "") or ANSWER_DIALECTS["default"]
    opt_type = "yn" if answer in ("y", "n") else "select"
    return bool(dialect.get(opt_type, {}).get("enter", False))


def _normalize_capture_tail(raw: str) -> str:
    """ANSI-strip + drop trailing blank rows (tmux pads short panes to height).

    Shared by ingest and recheck — no 600-char cut; full capture content.
    """
    cleaned = _agent_terminals.strip_ansi(raw or "")
    lines = cleaned.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _window_as_dict(win: Any) -> dict[str, Any] | None:
    """Normalize TmuxWindow / to_dict() result / plain dict for ingest."""
    if isinstance(win, dict):
        return win
    to_dict = getattr(win, "to_dict", None)
    if callable(to_dict):
        d = to_dict()
        if isinstance(d, dict):
            return d
    return None


def _recheck_fingerprint(service: Any, pane_id: str) -> str | None:
    """Capture pane and return current question fingerprint, or None if gone.

    Uses the same ``start=-{_CAPTURE_TAIL_LINES}`` window and
    ``_normalize_capture_tail`` as the scrape ingestor so fingerprints match.
    """
    raw = service.capture_pane(pane_id, start=-_CAPTURE_TAIL_LINES)
    tail = _normalize_capture_tail(raw)
    parsed = parse_question(tail)
    if parsed is None:
        return None
    return compute_fingerprint(pane_id, parsed["region"])


def _recheck_event_standing(
    service: Any,
    event: dict[str, Any],
) -> str | None:
    """Recheck that the claimed event is still standing; return expected fp or None.

    Scrape events: parse + fingerprint match (P0 path).
    Hook events: substring presence of ``question_text`` AND every option
    label (parse fails on real Claude Code prompts — must not use
    parse_question; the option-label requirement blocks the same-question/
    different-options re-ask window).
    """
    pane_id = str(event.get("pane_id") or "")
    expected_fp = str(event.get("fingerprint") or "")
    if event.get("source") == "hook":
        raw = service.capture_pane(pane_id, start=-_CAPTURE_TAIL_LINES)
        options = event.get("options")
        if hook_question_still_present(
            str(event.get("question_text") or ""),
            raw,
            options=options if isinstance(options, list) else None,
        ):
            return expected_fp
        return None
    return _recheck_fingerprint(service, pane_id)


def _claim_event(
    event_id: int,
    *,
    answer: str,
    answered_by: str,
    answer_source: str | None = None,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Atomic open→answered claim. True when this caller owns the send."""
    ts = _iso_now(now)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE question_events SET status = 'answered', answered_by = ?, "
                "answer = ?, answer_source = ?, updated_ts = ? "
                "WHERE id = ? AND status = 'open'",
                (answered_by, answer, answer_source, ts, int(event_id)),
            )
            return int(cur.rowcount or 0) == 1


def _set_event_status(
    event_id: int,
    status: str,
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
    clear_answer: bool = False,
) -> None:
    ts = _iso_now(now)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            if clear_answer:
                conn.execute(
                    "UPDATE question_events SET status = ?, answered_by = NULL, "
                    "answer = NULL, answer_source = NULL, updated_ts = ? WHERE id = ?",
                    (status, ts, int(event_id)),
                )
            else:
                conn.execute(
                    "UPDATE question_events SET status = ?, updated_ts = ? WHERE id = ?",
                    (status, ts, int(event_id)),
                )


def _set_verify_fields(
    event_id: int,
    *,
    answer_verified: bool,
    latency_s: float,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> None:
    ts = _iso_now(now)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            conn.execute(
                "UPDATE question_events SET answer_verified = ?, latency_s = ?, "
                "updated_ts = ? WHERE id = ?",
                (1 if answer_verified else 0, float(latency_s), ts, int(event_id)),
            )


def set_pa_action_result(
    event_id: int,
    result: dict[str, Any],
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> None:
    """Persist bounded structured executor evidence on a ``pa_action`` row."""
    if not isinstance(result, dict):
        raise ValueError("pa_action result muss ein JSON-Objekt sein")
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # Pane tails are bounded by the executor; retain a final defensive limit so
    # a custom handler cannot grow question_events.db without bound.
    if len(encoded) > 32_000:
        encoded = json.dumps(
            {
                "version": PA_ACTION_VERSION,
                "status": "evidence-truncated",
                "excerpt": encoded[:31_000],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    ts = _iso_now(now)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE question_events SET action_result = ?, updated_ts = ? "
                "WHERE id = ? AND kind = 'pa_action'",
                (encoded, ts, int(event_id)),
            )
            if int(cur.rowcount or 0) != 1:
                raise ValueError("pa_action event nicht gefunden")


def answer_question(
    event_id: int,
    answer: str,
    *,
    answered_by: str = "operator",
    via_suggestion: int | None = None,
    db_path: Optional[Path] = None,
    service: Any = None,
    verify_delay_s: float = 1.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Claim an open event, recheck fingerprint, send answer, verify.

    Order is load-bearing (plan Ein-Schuss-Idempotenz + Anti-TOCTOU):
    validate → claim → recheck → send → verify. Double-click safe.
    """
    answer_str = str(answer)
    event = _load_event(event_id, db_path=db_path)
    if event is None:
        return {"ok": False, "reason": "not-found"}

    options = event.get("options") or []
    if not options:
        return {"ok": False, "reason": "free-text-not-supported"}

    valid_nrs = _option_nrs(options)
    if answer_str not in valid_nrs:
        return {"ok": False, "reason": "invalid-option"}

    suggestions_raw = event.get("suggestions")
    suggestions = suggestions_raw if isinstance(suggestions_raw, list) else []
    suggested_nrs = {
        item.get("nr") for item in suggestions if isinstance(item, dict) and "nr" in item
    }
    if via_suggestion is not None and via_suggestion not in suggested_nrs:
        return {"ok": False, "reason": "invalid-suggestion"}

    if event.get("kind") == "pa_action":
        # Gated actions never touch a pane.  Keep this import lazy so the
        # existing scraper/answer process does not acquire kanban/PA-store
        # dependencies unless it is actually executing an operator-confirmed
        # action.
        from hermes_cli.pa_actions import answer_pa_action

        return answer_pa_action(
            event_id,
            answer_str,
            event=event,
            answered_by=answered_by,
            db_path=db_path,
        )

    if not suggestions:
        answer_source = "operator_free"
    else:
        top_nr = suggestions[0].get("nr") if isinstance(suggestions[0], dict) else None
        answer_source = (
            "suggested_accepted" if answer_str == str(top_nr) else "suggested_edited"
        )

    if not _claim_event(
        event_id,
        answer=answer_str,
        answered_by=answered_by,
        answer_source=answer_source,
        db_path=db_path,
    ):
        return {"ok": False, "reason": "not-open"}

    pane_id = str(event.get("pane_id") or "")
    expected_fp = str(event.get("fingerprint") or "")
    svc = service if service is not None else _agent_terminals.TmuxAgentSessionService()

    # Step 4: standing recheck (nothing sent yet → claim can roll back).
    # Hook events use question_text presence; scrape uses fingerprint.
    try:
        current_fp = _recheck_event_standing(svc, event)
    except Exception as exc:
        logger.warning(
            "answer_question recheck failed event_id=%s pane_id=%s: %s",
            event_id,
            pane_id,
            exc,
        )
        _set_event_status(
            event_id, "open", db_path=db_path, clear_answer=True
        )
        return {"ok": False, "reason": "recheck-failed"}

    if current_fp is None or current_fp != expected_fp:
        _set_event_status(event_id, "superseded", db_path=db_path)
        return {"ok": False, "reason": "superseded"}

    # Step 5: send via dialect table
    enter = _answer_enter_flag(
        str(event.get("kind") or "") if event.get("kind") is not None else None,
        answer_str,
    )
    try:
        svc.send_keys_to_pane(pane_id, answer_str, enter=enter)
    except Exception as exc:
        logger.warning(
            "answer_question send failed event_id=%s pane_id=%s: %s",
            event_id,
            pane_id,
            exc,
        )
        # Claim already answered; do not re-open (send may have partially landed).
        return {"ok": False, "reason": "send-failed"}

    # Step 6: verify question region gone / changed
    answer_mono = time.time()
    try:
        event_ts = _parse_iso_ts(str(event.get("ts") or ""))
        latency_s = max(0.0, answer_mono - event_ts)
    except (TypeError, ValueError):
        latency_s = 0.0

    sleep(float(verify_delay_s))
    verified = False
    try:
        if str(event.get("source") or "") == "hook":
            # Hook-source: successful send is treated as verified. The
            # resolve-signal path also stamps verified=True. Text-disappear
            # is a false-negative under CC echo (question still on screen).
            verified = True
        else:
            # Scrape: fingerprint gone/changed. One capture only — options are
            # part of the semantic select region, so options-list disappearance
            # already flips the fingerprint (no second capture_pane).
            fp2 = _recheck_event_standing(svc, event)
            verified = fp2 is None or fp2 != expected_fp
        _set_verify_fields(
            event_id,
            answer_verified=verified,
            latency_s=latency_s,
            db_path=db_path,
        )
    except Exception as exc:
        logger.warning(
            "answer_question verify failed event_id=%s pane_id=%s: %s",
            event_id,
            pane_id,
            exc,
        )
        # Send already happened — stay answered; report recheck failure.
        try:
            _set_verify_fields(
                event_id,
                answer_verified=False,
                latency_s=latency_s,
                db_path=db_path,
            )
        except Exception:
            pass
        return {
            "ok": True,
            "reason": "recheck-failed",
            "verified": False,
            "latency_s": latency_s,
        }

    return {"ok": True, "verified": verified, "latency_s": latency_s}


# ---------------------------------------------------------------------------
# Scrape ingestor
# ---------------------------------------------------------------------------


class QuestionScrapeIngestor:
    """Poll tmux panes for standing questions; write stable open events.

    Uses ``list_windows`` + per-pane ``capture_pane`` (not dashboard
    ``overview()``, which caps tails at 600 chars). Double-capture stability:
    an event is only inserted when the same fingerprint was already seen on
    the previous ``poll_once`` *and* ``activity_age > 3`` seconds. Expiry is
    also two-poll confirmed so a single empty/transient list does not wipe
    open events. After claim, a 60s answered-cooldown blocks re-insert of the
    same pane+fingerprint while send may still leave the prompt visible.
    """

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        service_factory: Optional[Callable[[], Any]] = None,
        now: Optional[Callable[[], float]] = None,
        activity_age_threshold_s: float = 3.0,
        overview_tail_lines: int = _CAPTURE_TAIL_LINES,
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
            "cooldown": 0,
            "capture_errors": 0,
            "skipped_hook_authoritative": 0,
            "cross_session_checked": 0,
        }
        now = float(self._now())
        svc = self._service()
        # Empty-snapshot guard uses the full list_windows length (all sessions)
        # before session filtering — same reference size as former overview.
        try:
            raw_windows = list(svc.list_windows())
        except Exception:
            logger.warning("agent_questions list_windows failed", exc_info=True)
            raw_windows = []

        summary["windows"] = len(raw_windows)
        # standing_panes: panes confirmed still showing their open question
        # (scrape-parse match OR hook question_text substring present).
        standing_panes: set[str] = set()
        scanned_panes: set[str] = set()
        next_pending: dict[str, str] = {}
        tail_start = -abs(self.overview_tail_lines)

        for win_obj in raw_windows:
            win = _window_as_dict(win_obj)
            if win is None:
                continue
            if win.get("session") != self.session_filter:
                continue

            pane_id = str(win.get("pane_id") or "")
            if not pane_id:
                continue
            scanned_panes.add(pane_id)

            try:
                raw = svc.capture_pane(pane_id, start=tail_start)
            except Exception:
                summary["capture_errors"] += 1
                logger.debug(
                    "agent_questions capture_pane failed pane_id=%s",
                    pane_id,
                    exc_info=True,
                )
                continue

            # Hook is authoritative: skip scrape insert while an open hook
            # event exists for this pane. Still confirm standing via
            # question_text substring (parse_question fails on real CC prompts).
            open_hook = find_open_hook_event(pane_id, db_path=self.db_path)
            if open_hook is not None:
                summary["skipped_hook_authoritative"] += 1
                if hook_question_still_present(
                    str(open_hook.get("question_text") or ""), raw
                ):
                    standing_panes.add(pane_id)
                continue

            tail = _normalize_capture_tail(raw)
            age = _activity_age_s(win, now)
            dead = bool(win.get("dead", False))
            state = _agent_terminals.classify_agent_pane(tail, age, dead)
            if state != "frage":
                continue

            summary["frage"] += 1
            parsed = parse_question(tail)
            if parsed is None:
                summary["parse_none"] += 1
                continue

            fp = compute_fingerprint(pane_id, parsed["region"])

            if find_open_event(pane_id, fp, db_path=self.db_path) is not None:
                summary["idempotent"] += 1
                next_pending[pane_id] = fp
                standing_panes.add(pane_id)
                continue

            prev_fp = self._pending.get(pane_id)
            stable = prev_fp == fp
            age_ok = age is not None and age > self.activity_age_threshold_s

            if stable and age_ok:
                # Claim→poller race: after answer the prompt may still stand
                # for a few seconds; do not open a duplicate event.
                # 60s cooldown applies ONLY to scrape inserts (hook ignores it).
                if recently_answered(
                    pane_id, fp, within_s=60.0, db_path=self.db_path, now=now
                ):
                    summary["cooldown"] += 1
                    next_pending[pane_id] = fp
                    standing_panes.add(pane_id)
                    continue
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
                    try:
                        from hermes_cli.agent_question_push import maybe_push_question

                        maybe_push_question(
                            int(new_id), db_path=self.db_path, now=now
                        )
                    except Exception:
                        logger.warning(
                            "scrape insert push hook failed event_id=%s",
                            new_id,
                            exc_info=True,
                        )
                    try:
                        from hermes_cli.agent_question_suggest import (
                            schedule_question_suggestion,
                        )

                        schedule_question_suggestion(int(new_id), db_path=self.db_path)
                    except Exception:
                        logger.warning(
                            "scrape insert suggestion hook failed event_id=%s",
                            new_id,
                            exc_info=True,
                        )
                next_pending[pane_id] = fp
                standing_panes.add(pane_id)
            else:
                next_pending[pane_id] = fp
                standing_panes.add(pane_id)
                if not stable:
                    summary["unstable"] += 1
                    summary["pending"] += 1
                elif not age_ok:
                    summary["skipped_age"] += 1
                    summary["pending"] += 1
                else:
                    summary["pending"] += 1

        self._pending = next_pending

        # Empty list_windows (e.g. transient tmux failure) must not expire
        # everything — skip the expiry passage. But a PERSISTENTLY empty
        # list (>= 3 consecutive polls) means the windows are truly gone;
        # from then on the expiry passage runs again (still two-poll
        # confirmed via _expire_pending) so events cannot stay open forever.
        if len(raw_windows) == 0:
            self._empty_snapshots += 1
            if self._empty_snapshots < 3:
                summary["skipped_expiry_empty_snapshot"] = 1
                return summary
        else:
            self._empty_snapshots = 0

        # Cross-session / unscanned panes: open events whose pane was not in
        # the work-session scan — capture pane-addressed and confirm standing.
        open_events = list_open_events(db_path=self.db_path)
        for ev in open_events:
            pane_id = str(ev.get("pane_id") or "")
            if not pane_id or pane_id in standing_panes:
                continue
            if pane_id in scanned_panes:
                # Already evaluated during the work scan (hook substring or
                # scrape standing). Missing from standing → expiry candidate.
                continue
            summary["cross_session_checked"] += 1
            try:
                raw = svc.capture_pane(pane_id, start=tail_start)
            except Exception as exc:
                if not _is_gone_capture_error(exc):
                    # Transient tmux error ≠ strike (keep standing this poll).
                    standing_panes.add(pane_id)
                # Gone → leave out of standing → expiry strike.
                continue
            if ev.get("source") == "hook":
                if hook_question_still_present(
                    str(ev.get("question_text") or ""), raw
                ):
                    standing_panes.add(pane_id)
            else:
                # Scrape events outside the work scan: parse + fingerprint.
                tail = _normalize_capture_tail(raw)
                parsed = parse_question(tail)
                if parsed is not None:
                    fp = compute_fingerprint(pane_id, parsed["region"])
                    if fp == str(ev.get("fingerprint") or ""):
                        standing_panes.add(pane_id)

        open_panes = {str(ev.get("pane_id") or "") for ev in open_events}
        open_panes.discard("")
        candidates = open_panes - standing_panes
        to_expire = candidates & self._expire_pending
        if to_expire:
            summary["expired"] += expire_open_events(
                to_expire, db_path=self.db_path, now=now
            )
        self._expire_pending = candidates - to_expire
        return summary


# ---------------------------------------------------------------------------
# Housekeeping / GC (I3 Mini #4)
# ---------------------------------------------------------------------------

# Closed events older than this (by updated_ts) are deleted.
PRUNE_MAX_AGE_DAYS = 14
# Prune at most once per hour from the poller loop.
PRUNE_INTERVAL_S = 3600.0


def prune_old_events(
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
    max_age_days: float = PRUNE_MAX_AGE_DAYS,
) -> int:
    """DELETE expired/superseded/answered events with ``updated_ts`` older than max_age_days.

    Returns the number of deleted rows.
    """
    now_ts = time.time() if now is None else float(now)
    cutoff = _iso_now(now_ts - float(max_age_days) * 86400.0)
    with connect_closing(db_path=db_path) as conn:
        with write_txn(conn):
            cur = conn.execute(
                "DELETE FROM question_events WHERE status IN "
                "('expired', 'superseded', 'answered') AND updated_ts < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)


def prune_bak_files(
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
    max_age_days: float = PRUNE_MAX_AGE_DAYS,
) -> int:
    """Delete ``question_events.db.bak-*`` files older than max_age_days.

    Only this exact name pattern next to the DB — nothing else.
    Returns the number of files removed.
    """
    path = db_path if db_path is not None else question_events_db_path()
    parent = path.parent
    now_ts = time.time() if now is None else float(now)
    max_age_s = float(max_age_days) * 86400.0
    removed = 0
    try:
        candidates = sorted(parent.glob("question_events.db.bak-*"))
    except Exception:
        return 0
    for bak in candidates:
        # Strict prefix: basename must start with the bak pattern stem.
        name = bak.name
        if not name.startswith("question_events.db.bak-"):
            continue
        if not bak.is_file():
            continue
        try:
            age_s = now_ts - bak.stat().st_mtime
        except OSError:
            continue
        if age_s <= max_age_s:
            continue
        try:
            bak.unlink()
            removed += 1
        except OSError:
            logger.warning("failed to remove bak file %s", bak, exc_info=True)
    return removed


def run_housekeeping(
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict[str, int]:
    """Prune old events + bak files. Safe to call from the poller."""
    deleted = prune_old_events(db_path=db_path, now=now)
    bak_removed = prune_bak_files(db_path=db_path, now=now)
    return {"deleted": deleted, "bak_removed": bak_removed}


# ---------------------------------------------------------------------------
# Background poller (web_server startup only)
# ---------------------------------------------------------------------------

_poller_lock = threading.Lock()
_poller_thread: threading.Thread | None = None
_poller_stop = threading.Event()
_default_ingestor: QuestionScrapeIngestor | None = None
_last_warn_mono: float = 0.0
_last_prune_mono: float = 0.0


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
            global _last_warn_mono, _last_prune_mono
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
                # Housekeeping at most once per hour (no separate cron).
                try:
                    mono = time.monotonic()
                    if mono - _last_prune_mono >= PRUNE_INTERVAL_S:
                        run_housekeeping(db_path=resolved_db_path)
                        _last_prune_mono = mono
                except Exception:
                    logger.warning(
                        "agent_questions housekeeping failed",
                        exc_info=True,
                    )
                if _poller_stop.wait(interval):
                    break

        thread = threading.Thread(
            target=_loop,
            name="agent-questions-poller",
            daemon=True,
        )
        _poller_thread = thread
        thread.start()
        # Re-arm a bundled push stranded by a restart inside the debounce
        # window (pending ids are persistent, the timer is not — Kimi m2).
        try:
            from hermes_cli.agent_question_push import drain_pending_on_start

            drain_pending_on_start(db_path=db_path)
        except Exception:
            logger.warning("agent_questions push drain-on-start failed", exc_info=True)
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
