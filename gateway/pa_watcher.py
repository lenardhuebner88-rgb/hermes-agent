"""Event-driven Personal-Assistant watcher owned by the gateway.

The watcher deliberately has no timer/service of its own. ``GatewayRunner``
starts one mixin task, and every blocking SQLite, filesystem, tmux, and engine
operation is submitted through the gateway-owned executor by the async wrapper.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable

from hermes_cli.pa_chat import PAStore, SOL_MODEL, run_engine

logger = logging.getLogger("gateway.run")

WATCHER_INTERVAL_SECONDS = 60
WATCHER_MIN_INTERVAL_SECONDS = 10
WATCHER_MAX_INTERVAL_SECONDS = 3_600
ENGINE_DAILY_CALL_CAP = 60
ENGINE_BATCH_MAX_EVENTS = 20
SOURCE_EVENT_LIMIT = 500
RECEIPT_SCAN_MAX_FILES = 5_000
DELIVERY_MAX_EVENTS = 200
DELIVERY_RATE_LIMIT_SECONDS = 10 * 60
QUIET_START_HOUR = 23
QUIET_END_HOUR = 7
QUIET_END_MINUTE = 30
JUDGEMENT_LEASE_SECONDS = 10 * 60

KANBAN_TERMINAL_KINDS = frozenset(
    {"completed", "blocked", "gave_up", "crashed", "timed_out"}
)
GATE_EVENT_KINDS = frozenset(
    {
        "worker_gate_blocked",
        "release_gate_parked",
        "review_unavailable",
        "review_wait_attention",
        "rebase_conflict_returned",
    }
)
GATE_BLOCK_KINDS = frozenset({"review_revision", "integration"})
_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
_SOURCE_ORDER = {
    "receipt": 0,
    "session_exit": 1,
    "kanban_status": 2,
    "red_gate": 3,
}


@dataclass(frozen=True)
class WatchEvent:
    event_id: str
    source: str
    kind: str
    severity: str
    title: str
    ref: str | None
    occurred_at: int
    detail: str
    fingerprint: str
    expected: bool = False

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _fingerprint(*parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _event_id(fingerprint: str) -> str:
    return f"evt_{fingerprint[:16]}"


def _bounded_text(value: object, limit: int = 1_000) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _json_payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=2.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM pa_watcher_state WHERE key=?", (key,)
    ).fetchone()
    return None if row is None else str(row["value"])


def _state_int(conn: sqlite3.Connection, key: str) -> int | None:
    value = _state_get(conn, key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _state_set(
    conn: sqlite3.Connection,
    key: str,
    value: object,
    *,
    now: int,
) -> None:
    conn.execute(
        "INSERT INTO pa_watcher_state(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), int(now)),
    )


def _cursor_key(source: str, board_path: Path) -> str:
    digest = _fingerprint(board_path.expanduser().resolve())[:16]
    return f"cursor:{source}:{digest}"


def _source_rows(
    db_path: Path,
    *,
    cursor: int | None,
) -> tuple[int, list[sqlite3.Row]]:
    """Read one bounded raw event window and return its safe next cursor."""
    conn = _open_sqlite_readonly(db_path)
    try:
        max_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM task_events"
        ).fetchone()
        current_max = int(max_row["max_id"] if max_row is not None else 0)
        if cursor is None:
            return current_max, []
        rows = conn.execute(
            "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, "
            "t.title, t.status, t.block_kind, t.freigabe, t.live_test_depth "
            "FROM task_events AS e "
            "LEFT JOIN tasks AS t ON t.id=e.task_id "
            "WHERE e.id > ? ORDER BY e.id ASC LIMIT ?",
            (int(cursor), SOURCE_EVENT_LIMIT),
        ).fetchall()
    finally:
        conn.close()
    next_cursor = int(rows[-1]["id"]) if rows else int(cursor)
    return next_cursor, rows


def collect_kanban_status_events(
    db_path: Path,
    *,
    board: str,
    cursor: int | None,
) -> tuple[int, list[WatchEvent]]:
    """Collect the five terminal/status kinds with an independent cursor."""
    next_cursor, rows = _source_rows(db_path, cursor=cursor)
    events: list[WatchEvent] = []
    for row in rows:
        kind = str(row["kind"] or "")
        if kind not in KANBAN_TERMINAL_KINDS:
            continue
        task_id = str(row["task_id"] or "<unknown>")
        title = _bounded_text(row["title"] or task_id, 180)
        payload = _json_payload(row["payload"])
        detail = _bounded_text(
            payload.get("reason") or payload.get("summary") or payload, 1_000
        )
        severity = {
            "completed": "info",
            "blocked": "warning",
            "gave_up": "critical",
            "crashed": "critical",
            "timed_out": "critical",
        }[kind]
        fingerprint = _fingerprint("kanban", db_path.resolve(), row["id"])
        events.append(
            WatchEvent(
                event_id=_event_id(fingerprint),
                source="kanban_status",
                kind=kind,
                severity=severity,
                title=f"Task {task_id}: {title} — {kind}",
                ref=task_id,
                occurred_at=int(row["created_at"] or 0),
                detail=f"Board {board}; {detail}".rstrip("; "),
                fingerprint=fingerprint,
            )
        )
    return next_cursor, events


def _gate_match(row: sqlite3.Row, payload: dict[str, Any]) -> str | None:
    kind = str(row["kind"] or "")
    if kind in GATE_EVENT_KINDS:
        return kind
    block_kind = str(row["block_kind"] or payload.get("kind") or "")
    if kind == "blocked" and (
        block_kind in GATE_BLOCK_KINDS
        or isinstance(payload.get("review_revision"), dict)
        or payload.get("gate_output")
        or payload.get("park_class")
    ):
        return f"blocked:{block_kind or 'gate'}"
    held = str(row["status"] or "") == "scheduled" and (
        str(row["freigabe"] or "").strip().lower() == "operator"
        or str(row["live_test_depth"] or "").strip().lower() == "ui-real"
    )
    if held and kind in {"created", "decomposed", "status"}:
        return "operator_release_required"
    return None


def collect_gate_events(
    db_path: Path,
    *,
    board: str,
    cursor: int | None,
) -> tuple[int, list[WatchEvent]]:
    """Collect newly red or operator-release-relevant gate transitions."""
    next_cursor, rows = _source_rows(db_path, cursor=cursor)
    events: list[WatchEvent] = []
    for row in rows:
        payload = _json_payload(row["payload"])
        match = _gate_match(row, payload)
        if match is None:
            continue
        task_id = str(row["task_id"] or "<unknown>")
        task_title = _bounded_text(row["title"] or task_id, 180)
        reason = _bounded_text(
            payload.get("reason")
            or payload.get("summary")
            or payload.get("output_tail")
            or payload,
            1_000,
        )
        critical = match in {
            "worker_gate_blocked",
            "release_gate_parked",
            "rebase_conflict_returned",
        }
        fingerprint = _fingerprint("kanban", db_path.resolve(), row["id"])
        events.append(
            WatchEvent(
                event_id=_event_id(fingerprint),
                source="red_gate",
                kind=match,
                severity="critical" if critical else "warning",
                title=f"Gate bei Task {task_id}: {task_title} — {match}",
                ref=task_id,
                occurred_at=int(row["created_at"] or 0),
                detail=f"Board {board}; {reason}".rstrip("; "),
                fingerprint=fingerprint,
            )
        )
    return next_cursor, events


def _clean_agent(agent: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "source",
        "kind",
        "label",
        "task",
        "task_id",
        "session_id",
        "project",
        "since",
        "tmux_session",
        "tmux_window",
    )
    return {key: agent.get(key) for key in keys}


def _agent_key(agent: dict[str, Any]) -> str:
    source = str(agent.get("source") or "unknown")
    session_id = str(agent.get("session_id") or "").strip()
    if session_id:
        return f"{source}:session:{session_id}"
    tmux_session = str(agent.get("tmux_session") or "").strip()
    tmux_window = str(agent.get("tmux_window") or "").strip()
    if tmux_session:
        return f"{source}:tmux:{tmux_session}:{tmux_window}"
    task_id = str(agent.get("task_id") or "").strip()
    if task_id:
        return f"{source}:task:{task_id}"
    return f"{source}:label:{str(agent.get('label') or '').strip()}"


def diff_agent_sessions(
    previous_json: str | None,
    current_agents: list[dict[str, Any]],
    *,
    terminal_task_ids: set[str],
    now: int,
) -> tuple[str, list[WatchEvent]]:
    current = {_agent_key(agent): _clean_agent(agent) for agent in current_agents}
    snapshot = json.dumps(current, ensure_ascii=False, sort_keys=True)
    if previous_json is None:
        return snapshot, []
    try:
        prior_value = json.loads(previous_json)
        previous = prior_value if isinstance(prior_value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        previous = {}
    events: list[WatchEvent] = []
    for key in sorted(set(previous) - set(current)):
        raw = previous.get(key)
        agent = raw if isinstance(raw, dict) else {}
        source = str(agent.get("source") or "unknown")
        task_id = str(agent.get("task_id") or "").strip()
        if not task_id and source == "kanban":
            task_id = str(agent.get("label") or "").strip()
        expected = source == "coordination" or (
            bool(task_id) and task_id in terminal_task_ids
        )
        label = _bounded_text(agent.get("label") or key, 200)
        started = agent.get("since")
        fingerprint = _fingerprint("session_exit", key, started)
        events.append(
            WatchEvent(
                event_id=_event_id(fingerprint),
                source="session_exit",
                kind="session_exit",
                severity="info" if expected else "warning",
                title=f"Agenten-Session beendet: {label}",
                ref=task_id or label,
                occurred_at=now,
                detail=(
                    f"source={source}; project={agent.get('project') or '-'}; "
                    f"task={task_id or '-'}"
                ),
                fingerprint=fingerprint,
                expected=expected,
            )
        )
    return snapshot, events


def scan_new_receipts(
    root: Path,
    *,
    cursor_mtime_ns: int | None,
) -> tuple[int, list[WatchEvent]]:
    """Stat at most 5k canonical receipt files and tail by mtime cursor."""
    if not root.is_dir():
        raise FileNotFoundError(root)
    candidates = sorted(root.glob("*/receipts/*.md"))[:RECEIPT_SCAN_MAX_FILES]
    latest = int(cursor_mtime_ns or 0)
    observed: list[tuple[Path, os.stat_result]] = []
    for path in candidates:
        if path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        latest = max(latest, int(stat.st_mtime_ns))
        observed.append((path, stat))
    if cursor_mtime_ns is None:
        return latest, []
    events: list[WatchEvent] = []
    for path, stat in observed:
        if int(stat.st_mtime_ns) <= int(cursor_mtime_ns):
            continue
        resolved = str(path.resolve())
        fingerprint = _fingerprint(
            "receipt", resolved, stat.st_mtime_ns, stat.st_size
        )
        events.append(
            WatchEvent(
                event_id=_event_id(fingerprint),
                source="receipt",
                kind="new_receipt",
                severity="info",
                title=f"Neues Receipt: {path.name}",
                ref=resolved,
                occurred_at=int(stat.st_mtime),
                detail=f"Receipt-Pfad: {resolved}",
                fingerprint=fingerprint,
            )
        )
    events.sort(key=lambda event: (event.occurred_at, event.ref or ""))
    return latest, events


def prefilter_event(event: WatchEvent) -> tuple[bool, str]:
    """Drop only deterministic noise; uncertain candidates reach the engine."""
    if event.kind == "heartbeat":
        return False, "heartbeat"
    if event.kind == "session_exit" and event.expected:
        return False, "expected_exit"
    return True, "rule_candidate"


def _merge_events(events: list[WatchEvent]) -> list[WatchEvent]:
    """Deduplicate one tick, preferring the strongest representation."""
    merged: dict[str, WatchEvent] = {}
    for event in events:
        previous = merged.get(event.fingerprint)
        event_rank = (
            _SEVERITY_ORDER[event.severity],
            _SOURCE_ORDER.get(event.source, 0),
        )
        previous_rank = (
            _SEVERITY_ORDER[previous.severity],
            _SOURCE_ORDER.get(previous.source, 0),
        ) if previous is not None else (-1, -1)
        if previous is None or event_rank > previous_rank:
            merged[event.fingerprint] = event
    return sorted(
        merged.values(), key=lambda event: (event.occurred_at, event.event_id)
    )


def _ingest_events(
    store: PAStore,
    *,
    events: list[WatchEvent],
    state_updates: dict[str, str | int],
    now: int,
) -> int:
    inserted = 0
    with store.connect() as conn:
        for event in _merge_events(events):
            keep, reason = prefilter_event(event)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO pa_watcher_events("
                "fingerprint, event_id, source, kind, severity, title, ref, "
                "payload_json, status, reason, first_seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.fingerprint,
                    event.event_id,
                    event.source,
                    event.kind,
                    event.severity,
                    event.title,
                    event.ref,
                    json.dumps(event.payload(), ensure_ascii=False, sort_keys=True),
                    "candidate" if keep else "ignored",
                    reason,
                    now,
                ),
            )
            inserted += int(cursor.rowcount == 1)
        for key, value in state_updates.items():
            _state_set(conn, key, value, now=now)
    return inserted


def _local_day(now: int, zone: tzinfo | None = None) -> str:
    if zone is None:
        return datetime.fromtimestamp(now).astimezone().date().isoformat()
    return datetime.fromtimestamp(now, tz=zone).date().isoformat()


def _claim_candidates(
    store: PAStore,
    *,
    now: int,
    zone: tzinfo | None,
) -> tuple[str | None, list[dict[str, Any]], bool]:
    token = uuid.uuid4().hex
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE pa_watcher_events SET status='candidate', claim_token=NULL, "
            "claim_expires=NULL WHERE status='judging' "
            "AND COALESCE(claim_expires, 0) <= ?",
            (now,),
        )
        rows = conn.execute(
            "SELECT fingerprint, event_id, source, kind, severity, title, ref, "
            "payload_json FROM pa_watcher_events WHERE status='candidate' "
            "ORDER BY first_seen_at, event_id LIMIT ?",
            (ENGINE_BATCH_MAX_EVENTS,),
        ).fetchall()
        if not rows:
            conn.commit()
            return None, [], False
        placeholders = ",".join("?" for _ in rows)
        fingerprints = [str(row["fingerprint"]) for row in rows]
        conn.execute(
            "UPDATE pa_watcher_events SET status='judging', claim_token=?, "
            f"claim_expires=? WHERE fingerprint IN ({placeholders}) "
            "AND status='candidate'",
            (token, now + JUDGEMENT_LEASE_SECONDS, *fingerprints),
        )
        day = _local_day(now, zone)
        stored_day = _state_get(conn, "engine_day")
        calls = _state_int(conn, "engine_calls") or 0
        if stored_day != day:
            calls = 0
            _state_set(conn, "engine_day", day, now=now)
            _state_set(conn, "engine_calls", calls, now=now)
        capped = calls >= ENGINE_DAILY_CALL_CAP
        if not capped:
            _state_set(conn, "engine_calls", calls + 1, now=now)
        conn.commit()
    return token, [dict(row) for row in rows], capped


def _significance_prompt(rows: list[dict[str, Any]]) -> str:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_payload(row.get("payload_json"))
        candidates.append(
            {
                "id": row["event_id"],
                "source": row["source"],
                "kind": row["kind"],
                "severity": row["severity"],
                "title": _bounded_text(row["title"], 240),
                "ref": _bounded_text(row["ref"], 300),
                "detail": _bounded_text(payload.get("detail"), 400),
            }
        )
    body = json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
    return (
        "Du bist der schnelle Signifikanz-Filter des Jarvis-Wächters. "
        "Urteile mit niedrigem Aufwand und ohne Tools: Welche Ereignisse muss "
        "der Operator jetzt oder im nächsten gebündelten Hinweis sehen? "
        "Behalte echte Abschlüsse mit Nutzwert, Blocker, rote/angehaltene Gates, "
        "unerwartete Session-Abbrüche und relevante Receipts; verwerfe Routine. "
        "Antworte NUR als JSON {\"significant\":[\"id\",...],"
        "\"reason\":\"kurz\"}. Kandidaten:\n"
        + body
    )


def _parse_significance_reply(
    reply: str,
    *,
    known_ids: set[str],
) -> tuple[set[str], str]:
    text = str(reply or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("significance engine returned invalid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("significant"), list):
        raise ValueError("significance engine returned an invalid object")
    selected = {
        item
        for item in value["significant"]
        if isinstance(item, str) and item in known_ids
    }
    reason = _bounded_text(value.get("reason") or "engine judgement", 500)
    return selected, reason


def _finish_judgement(
    store: PAStore,
    *,
    token: str,
    selected_ids: set[str] | None,
    reason: str,
    now: int,
) -> None:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT fingerprint, event_id FROM pa_watcher_events "
            "WHERE status='judging' AND claim_token=?",
            (token,),
        ).fetchall()
        for row in rows:
            pending = selected_ids is None or str(row["event_id"]) in selected_ids
            conn.execute(
                "UPDATE pa_watcher_events SET status=?, reason=?, judged_at=?, "
                "claim_token=NULL, claim_expires=NULL WHERE fingerprint=? "
                "AND status='judging' AND claim_token=?",
                (
                    "pending" if pending else "ignored",
                    reason,
                    now,
                    row["fingerprint"],
                    token,
                ),
            )


def judge_candidates(
    store: PAStore,
    *,
    now: int,
    engine_runner: Callable[..., str] = run_engine,
    zone: tzinfo | None = None,
) -> dict[str, Any]:
    token, rows, capped = _claim_candidates(store, now=now, zone=zone)
    if token is None:
        return {"judged": 0, "engine_called": False, "fallback": False}
    if capped:
        _finish_judgement(
            store,
            token=token,
            selected_ids=None,
            reason="daily_cap_prefilter_fallback",
            now=now,
        )
        return {"judged": len(rows), "engine_called": False, "fallback": True}
    try:
        reply = engine_runner(
            "sol",
            _significance_prompt(rows),
            model=SOL_MODEL,
            image_paths=[],
        )
        selected, reason = _parse_significance_reply(
            reply, known_ids={str(row["event_id"]) for row in rows}
        )
    except Exception as exc:
        detail = _bounded_text(exc, 500) or exc.__class__.__name__
        _finish_judgement(
            store,
            token=token,
            selected_ids=None,
            reason=f"engine_error_prefilter_fallback: {detail}",
            now=now,
        )
        with store.connect() as conn:
            _state_set(conn, "last_engine_error", detail, now=now)
        logger.warning("PA watcher significance engine failed: %s", detail)
        return {"judged": len(rows), "engine_called": True, "fallback": True}
    _finish_judgement(
        store,
        token=token,
        selected_ids=selected,
        reason=reason,
        now=now,
    )
    return {"judged": len(rows), "engine_called": True, "fallback": False}


def is_quiet_time(now: int, *, zone: tzinfo | None = None) -> bool:
    local = (
        datetime.fromtimestamp(now).astimezone()
        if zone is None
        else datetime.fromtimestamp(now, tz=zone)
    )
    minute = local.hour * 60 + local.minute
    quiet_start = QUIET_START_HOUR * 60
    quiet_end = QUIET_END_HOUR * 60 + QUIET_END_MINUTE
    return minute >= quiet_start or minute <= quiet_end


def _bundle_summary(rows: list[sqlite3.Row]) -> str:
    lines = [f"Jarvis-Wächter: {len(rows)} signifikante Ereignisse gebündelt."]
    for row in rows[:12]:
        source = str(row["source"])
        ref = str(row["ref"] or "-")
        if source in {"kanban_status", "red_gate"}:
            evidence = f"Task-ID {ref}"
        elif source == "receipt":
            evidence = f"Receipt-Pfad {ref}"
        else:
            evidence = f"Session {ref}"
        lines.append(f"- {row['title']} (Beleg: {evidence})")
    if len(rows) > 12:
        lines.append(f"- … plus {len(rows) - 12} weitere Ereignisse")
    return _bounded_text("\n".join(lines), 4_000)


def deliver_pending_bundle(
    store: PAStore,
    *,
    now: int,
    zone: tzinfo | None = None,
) -> dict[str, Any]:
    """Atomically append one thread bubble, one feed row, and dedup marks."""
    if is_quiet_time(now, zone=zone):
        return {"delivered": 0, "reason": "quiet_hours"}
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        last_delivery = _state_int(conn, "last_delivery_at")
        if last_delivery is not None and now - last_delivery < DELIVERY_RATE_LIMIT_SECONDS:
            conn.commit()
            return {"delivered": 0, "reason": "rate_limited"}
        rows = conn.execute(
            "SELECT fingerprint, source, kind, severity, title, ref, reason "
            "FROM pa_watcher_events WHERE status='pending' "
            "ORDER BY first_seen_at, event_id LIMIT ?",
            (DELIVERY_MAX_EVENTS,),
        ).fetchall()
        if not rows:
            conn.commit()
            return {"delivered": 0, "reason": "empty"}
        fingerprints = [str(row["fingerprint"]) for row in rows]
        bundle_key = _fingerprint("bundle", *sorted(fingerprints))
        turn_id = f"pa_watcher_{bundle_key[:24]}"
        summary = _bundle_summary(rows)
        severity = max(
            (str(row["severity"]) for row in rows),
            key=lambda value: _SEVERITY_ORDER.get(value, 0),
        )
        lead = max(
            rows,
            key=lambda row: _SEVERITY_ORDER.get(str(row["severity"]), 0),
        )
        title = (
            str(lead["title"])
            if len(rows) == 1
            else f"{len(rows)} signifikante Jarvis-Ereignisse"
        )
        engine = "pa-watcher"
        model = "significance-v1"
        conn.execute(
            "INSERT INTO pa_conversations(id, created_at, updated_at) "
            "VALUES ('default', ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "updated_at=excluded.updated_at",
            (now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO pa_turns("
            "id, conversation_id, status, reply, error, engine, model, "
            "project_scope, attachments_json, ts, updated_ts"
            ") VALUES (?, 'default', 'done', ?, NULL, ?, ?, NULL, '[]', ?, ?)",
            (turn_id, summary, engine, model, now, now),
        )
        exists = conn.execute(
            "SELECT 1 FROM pa_messages WHERE turn_id=? AND role='assistant' "
            "AND engine=? LIMIT 1",
            (turn_id, engine),
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO pa_messages("
                "conversation_id, turn_id, role, content, engine, model, "
                "attachments_json, ts"
                ") VALUES ('default', ?, 'assistant', ?, ?, ?, '[]', ?)",
                (turn_id, summary, engine, model, now),
            )
        conn.execute(
            "INSERT INTO pa_feed(ts, kind, severity, title, ref, delivered_push) "
            "VALUES (?, 'watcher_bundle', ?, ?, ?, 0)",
            (now, severity, _bounded_text(title, 240), lead["ref"]),
        )
        placeholders = ",".join("?" for _ in fingerprints)
        conn.execute(
            "UPDATE pa_watcher_events SET status='delivered', delivered_at=? "
            f"WHERE fingerprint IN ({placeholders}) AND status='pending'",
            (now, *fingerprints),
        )
        _state_set(conn, "last_delivery_at", now, now=now)
        conn.commit()
    return {"delivered": len(rows), "reason": "delivered", "turn_id": turn_id}


def _default_agents_payload() -> dict[str, Any]:
    from hermes_cli.projects_overview import build_agents_payload, load_projects_registry

    return build_agents_payload(load_projects_registry())


def _default_receipts_root() -> Path:
    configured = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    vault = Path(configured).expanduser() if configured else Path("/home/piet/vault")
    return vault / "03-Agents"


def _discover_board_paths() -> list[tuple[str, Path]]:
    from hermes_cli import kanban_db

    try:
        boards = kanban_db.list_boards(include_archived=False)
    except Exception:
        boards = [{"slug": kanban_db.DEFAULT_BOARD}]
    discovered: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for metadata in boards:
        board = str(metadata.get("slug") or kanban_db.DEFAULT_BOARD)
        raw_path = metadata.get("db_path")
        path = (
            Path(str(raw_path)).expanduser()
            if raw_path
            else kanban_db.kanban_db_path(board)
        )
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        discovered.append((board, path))
    return discovered


def collect_sources(
    store: PAStore,
    *,
    now: int,
    agents_builder: Callable[[], dict[str, Any]] = _default_agents_payload,
    receipts_root: Path | None = None,
    board_paths: list[tuple[str, Path]] | None = None,
) -> tuple[list[WatchEvent], dict[str, str | int], list[str]]:
    """Read every independent source; failures never advance its cursor."""
    with store.connect() as conn:
        prior_session_snapshot = _state_get(conn, "cursor:sessions")
        receipt_cursor = _state_int(conn, "cursor:receipts_mtime_ns")
        cursor_values: dict[str, int | None] = {}
        resolved_boards = board_paths if board_paths is not None else _discover_board_paths()
        for _board, db_path in resolved_boards:
            for source in ("kanban", "gates"):
                key = _cursor_key(source, db_path)
                cursor_values[key] = _state_int(conn, key)

    events: list[WatchEvent] = []
    updates: dict[str, str | int] = {}
    errors: list[str] = []
    for board, db_path in resolved_boards:
        status_key = _cursor_key("kanban", db_path)
        try:
            cursor, found = collect_kanban_status_events(
                db_path,
                board=board,
                cursor=cursor_values[status_key],
            )
            updates[status_key] = cursor
            events.extend(found)
        except Exception as exc:
            errors.append(f"kanban_status:{board}: {_bounded_text(exc, 300)}")
        gate_key = _cursor_key("gates", db_path)
        try:
            cursor, found = collect_gate_events(
                db_path,
                board=board,
                cursor=cursor_values[gate_key],
            )
            updates[gate_key] = cursor
            events.extend(found)
        except Exception as exc:
            errors.append(f"red_gate:{board}: {_bounded_text(exc, 300)}")

    terminal_task_ids = {
        str(event.ref)
        for event in events
        if event.source == "kanban_status" and event.ref
    }
    try:
        payload = agents_builder()
        raw_agents = payload.get("agents", []) if isinstance(payload, dict) else []
        agents = [agent for agent in raw_agents if isinstance(agent, dict)]
        snapshot, found = diff_agent_sessions(
            prior_session_snapshot,
            agents,
            terminal_task_ids=terminal_task_ids,
            now=now,
        )
        updates["cursor:sessions"] = snapshot
        events.extend(found)
        if isinstance(payload, dict):
            for error in payload.get("errors", []):
                errors.append(f"sessions: {_bounded_text(error, 300)}")
    except Exception as exc:
        errors.append(f"sessions: {_bounded_text(exc, 300)}")

    try:
        cursor, found = scan_new_receipts(
            receipts_root or _default_receipts_root(),
            cursor_mtime_ns=receipt_cursor,
        )
        updates["cursor:receipts_mtime_ns"] = cursor
        events.extend(found)
    except Exception as exc:
        errors.append(f"receipts: {_bounded_text(exc, 300)}")
    return events, updates, errors


def run_watcher_tick(
    *,
    now: int | None = None,
    interval_seconds: int = WATCHER_INTERVAL_SECONDS,
    store: PAStore | None = None,
    engine_runner: Callable[..., str] = run_engine,
    zone: tzinfo | None = None,
    agents_builder: Callable[[], dict[str, Any]] = _default_agents_payload,
    receipts_root: Path | None = None,
    board_paths: list[tuple[str, Path]] | None = None,
) -> dict[str, Any]:
    """Execute one complete synchronous tick; intended for an owned executor."""
    observed_at = int(time.time()) if now is None else int(now)
    pa_store = store or PAStore()
    pa_store.ensure_schema()
    result: dict[str, Any] = {"collected": 0, "ingested": 0, "errors": []}
    try:
        events, updates, errors = collect_sources(
            pa_store,
            now=observed_at,
            agents_builder=agents_builder,
            receipts_root=receipts_root,
            board_paths=board_paths,
        )
        result["collected"] = len(events)
        result["errors"] = errors
        result["ingested"] = _ingest_events(
            pa_store,
            events=events,
            state_updates=updates,
            now=observed_at,
        )
        result["judgement"] = judge_candidates(
            pa_store,
            now=observed_at,
            engine_runner=engine_runner,
            zone=zone,
        )
        result["delivery"] = deliver_pending_bundle(
            pa_store,
            now=observed_at,
            zone=zone,
        )
    finally:
        with pa_store.connect() as conn:
            _state_set(conn, "last_tick_at", observed_at, now=observed_at)
            _state_set(conn, "interval_seconds", interval_seconds, now=observed_at)
            _state_set(conn, "enabled", 1, now=observed_at)
            _state_set(
                conn,
                "last_source_errors",
                json.dumps(result.get("errors", []), ensure_ascii=False),
                now=observed_at,
            )
    return result


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def load_watcher_config() -> dict[str, Any]:
    from hermes_cli.config import load_config

    config = load_config()
    raw = config.get("pa_watcher", {}) if isinstance(config, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    try:
        interval = int(raw.get("interval_seconds", WATCHER_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        interval = WATCHER_INTERVAL_SECONDS
    interval = max(
        WATCHER_MIN_INTERVAL_SECONDS,
        min(interval, WATCHER_MAX_INTERVAL_SECONDS),
    )
    return {
        "enabled": _coerce_bool(raw.get("enabled", True), default=True),
        "interval_seconds": interval,
    }


def record_disabled_state(*, now: int, interval_seconds: int) -> None:
    store = PAStore()
    store.ensure_schema()
    with store.connect() as conn:
        _state_set(conn, "enabled", 0, now=now)
        _state_set(conn, "interval_seconds", interval_seconds, now=now)


class GatewayPAWatcherMixin:
    """Gateway-owned PA watcher loop; no independent scheduler is created."""

    async def _pa_watcher(self) -> None:
        try:
            config = await self._kanban_off_loop(load_watcher_config)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "PA watcher config load failed; continuing with safe defaults"
            )
            config = {
                "enabled": True,
                "interval_seconds": WATCHER_INTERVAL_SECONDS,
            }
        interval = int(config["interval_seconds"])
        if not config["enabled"]:
            await self._kanban_off_loop(
                record_disabled_state,
                now=int(time.time()),
                interval_seconds=interval,
            )
            logger.info("PA watcher disabled via pa_watcher.enabled=false")
            return
        logger.info("PA watcher enabled — interval=%ss", interval)
        store = PAStore()
        while self._running:
            try:
                outcome = await self._kanban_off_loop(
                    run_watcher_tick,
                    now=int(time.time()),
                    interval_seconds=interval,
                    store=store,
                )
                if outcome.get("errors"):
                    logger.warning(
                        "PA watcher tick completed with source errors: %s",
                        outcome["errors"],
                    )
                else:
                    logger.debug("PA watcher tick: %s", outcome)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PA watcher tick failed")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
