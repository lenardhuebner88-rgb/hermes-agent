"""Personal-assistant backend for the Projekte tab.

The PA store is the sole conversation history.  Every model turn is composed
from that store and executed as an independent ``hermes chat -Q`` process;
Hermes resume/session identifiers are deliberately never used by this adapter.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hermes_cli.pa_live_share import (
    LIVE_FRAME_MAX_BYTES,
    LiveShareNoFrame,
    LiveShareNotFound,
    LiveShareRegistry,
)
from hermes_cli.sqlite_util import add_column_if_missing
from hermes_constants import get_hermes_home

DEFAULT_ENGINE = "sol"
ENGINE_NAME = DEFAULT_ENGINE
SOL_MODEL = "gpt-5.6-sol"
CLAUDE_OPUS_MODEL = "opus-4.8"
CLAUDE_FABLE_MODEL = "fable-5"
CLAUDE_OPUS_CLI_MODEL = "claude-opus-4-8"
CLAUDE_FABLE_CLI_MODEL = "claude-fable-5"
KIMI_MODEL = "k3"
KIMI_CLI_MODEL = "kimi-code/k3"
# ``context_engine`` is a valid, statically empty built-in toolset.  Keeping an
# explicit -t value is load-bearing: omitting/emptying -t makes the CLI fall
# back to configured defaults.  If a local context engine is active it may add
# its own retrieval schemas, but web_search and every write tool remain absent.
READ_ONLY_TOOLSETS = "context_engine"
TURN_TIMEOUT_SECONDS = 180
DB_BUSY_TIMEOUT_MS = 5_000
CONTEXT_PACK_MAX_CHARS = 14_000
HISTORY_MAX_MESSAGES = 16
HISTORY_MAX_CHARS = 6_000
UPLOAD_MAX_BYTES = 15 * 1024 * 1024
UPLOAD_TTL_DAYS = 30
UPLOAD_SOFT_MAX_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class EngineSpec:
    models: tuple[str, ...]
    default_model: str
    supports_images: bool


ENGINE_REGISTRY: dict[str, EngineSpec] = {
    "sol": EngineSpec(
        models=(SOL_MODEL,), default_model=SOL_MODEL, supports_images=True
    ),
    "claude": EngineSpec(
        models=(CLAUDE_OPUS_MODEL, CLAUDE_FABLE_MODEL),
        default_model=CLAUDE_OPUS_MODEL,
        supports_images=False,
    ),
    "kimi": EngineSpec(
        models=(KIMI_MODEL,), default_model=KIMI_MODEL, supports_images=False
    ),
}

_CLAUDE_CLI_MODELS = {
    CLAUDE_OPUS_MODEL: CLAUDE_OPUS_CLI_MODEL,
    CLAUDE_FABLE_MODEL: CLAUDE_FABLE_CLI_MODEL,
}
_LEGACY_MODEL_ALIASES = {"sol": {"sol": SOL_MODEL}}

PA_SYSTEM_PROMPT = """Du bist der persönliche Assistent im Projekte-Tab von Hermes.
Du beantwortest Fragen ausschließlich aus dem mitgelieferten Live-Kontext und der
PA-Historie. Du darfst selbst NICHTS mutieren oder bestätigen. Wenn eine Aktion
wirklich nötig ist, darfst du genau einen Vorschlag ausschließlich als
```pa_action {"category":"...","payload":{...},"reason":"..."}``` ausgeben;
führe ihn niemals selbst aus. Gib nie mehr als einen solchen Block pro Antwort aus.
PlanSpec-Entwürfe entstehen immer zuerst über POST /api/pa/planspec/draft; schreibe
niemals PlanSpec-YAML in den Chat. Schlage `planspec.ingest` nur mit einer dort
erhaltenen `draft_id` vor.
Bei Erinnerungsbitten schlage `reminders.create` mit `due_at`, `title` und optional
`body` vor; löse die Uhrzeit selbst als ISO-8601 mit Zeitzone auf.
Antworte kurz auf Deutsch, kennzeichne Unsicherheit und nenne die verwendeten Belege
(z. B. Board, offene Fragen, laufende Ketten oder Receipts)."""

_DEFAULT_CONVERSATION_ID = "default"
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UPLOAD_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
_UPLOAD_MIME_BY_SUFFIX = {suffix: content_type for content_type, suffix in _UPLOAD_SUFFIXES.items()}
_PA_ACTION_BLOCK_RE = re.compile(r"```pa_action\b(.*?)```", re.IGNORECASE | re.DOTALL)
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pa_conversations (
    id          TEXT PRIMARY KEY,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_turns (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES pa_conversations(id),
    status          TEXT NOT NULL CHECK(status IN ('pending','running','done','error')),
    reply           TEXT,
    error           TEXT,
    engine          TEXT NOT NULL,
    model           TEXT NOT NULL,
    project_scope   TEXT,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    ts              INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pa_turns_conversation_ts
    ON pa_turns(conversation_id, ts DESC);

CREATE TABLE IF NOT EXISTS pa_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES pa_conversations(id),
    turn_id         TEXT NOT NULL REFERENCES pa_turns(id),
    role            TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content         TEXT NOT NULL,
    engine          TEXT NOT NULL,
    model           TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    ts              INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pa_messages_conversation_id
    ON pa_messages(conversation_id, id DESC);

CREATE TABLE IF NOT EXISTS pa_feed (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    title           TEXT NOT NULL,
    ref             TEXT,
    delivered_push  INTEGER NOT NULL DEFAULT 0 CHECK(delivered_push IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_pa_feed_ts ON pa_feed(ts, id);

CREATE TABLE IF NOT EXISTS pa_watcher_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_watcher_events (
    fingerprint  TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL,
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    title        TEXT NOT NULL,
    ref          TEXT,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL CHECK(
        status IN ('candidate','judging','pending','ignored','delivered')
    ),
    reason       TEXT,
    first_seen_at INTEGER NOT NULL,
    judged_at    INTEGER,
    delivered_at INTEGER,
    claim_token  TEXT,
    claim_expires INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pa_watcher_events_status
    ON pa_watcher_events(status, first_seen_at);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    due_at_utc TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','fired','cancelled')),
    created_at TEXT NOT NULL,
    fired_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders(status, due_at_utc);
"""

_log = logging.getLogger(__name__)
_PA_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="pa-turn"
)


class PAEngineError(RuntimeError):
    """A user-visible one-shot engine failure."""


class AssetNotFoundError(ValueError):
    """A syntactically valid PA asset id that no longer exists."""


class AttachmentIn(BaseModel):
    asset_id: str


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=32_000)
    project_scope: str | None = Field(default=None, max_length=128)
    engine: str | None = None
    model: str | None = None
    # The current Hermes CLI exposes one --image value. Keep the v1 wire shape
    # list-based for the UI contract, but reject ambiguous multi-image turns.
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=1)


class PAStore:
    """Small standalone SQLite store for PA conversations and turns."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_hermes_home() / "pa" / "pa.db"
        self._schema_ready = False

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            from hermes_state import apply_wal_with_fallback

            apply_wal_with_fallback(conn, db_label="pa/pa.db")
            conn.executescript(_SCHEMA_SQL)
            # Live Sprint-1 databases predate message attachment persistence.
            add_column_if_missing(
                conn,
                "pa_messages",
                "attachments_json",
                "attachments_json TEXT NOT NULL DEFAULT '[]'",
            )
        self._schema_ready = True

    def _ensure_schema(self) -> None:
        if not self._schema_ready:
            self.ensure_schema()

    def create_turn(
        self,
        *,
        text: str,
        engine: str,
        model: str,
        project_scope: str | None,
        attachments: list[str],
        now: int | None = None,
    ) -> str:
        self._ensure_schema()
        ts = int(time.time()) if now is None else int(now)
        turn_id = "turn_" + secrets.token_hex(12)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO pa_conversations(id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
                (_DEFAULT_CONVERSATION_ID, ts, ts),
            )
            conn.execute(
                "INSERT INTO pa_turns(id, conversation_id, status, engine, model, "
                "project_scope, attachments_json, ts, updated_ts) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    _DEFAULT_CONVERSATION_ID,
                    engine,
                    model,
                    project_scope,
                    json.dumps(attachments),
                    ts,
                    ts,
                ),
            )
            conn.execute(
                "INSERT INTO pa_messages(conversation_id, turn_id, role, content, "
                "engine, model, attachments_json, ts) "
                "VALUES (?, ?, 'user', ?, ?, ?, ?, ?)",
                (
                    _DEFAULT_CONVERSATION_ID,
                    turn_id,
                    text,
                    engine,
                    model,
                    json.dumps(attachments),
                    ts,
                ),
            )
        return turn_id

    def set_running(self, turn_id: str, *, now: int | None = None) -> bool:
        self._ensure_schema()
        ts = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE pa_turns SET status='running', updated_ts=? "
                "WHERE id=? AND status='pending'",
                (ts, turn_id),
            )
        return cursor.rowcount == 1

    def finish_turn(self, turn_id: str, reply: str, *, now: int | None = None) -> None:
        self._finish(turn_id, status="done", reply=reply, error=None, now=now)

    def fail_turn(self, turn_id: str, error: str, *, now: int | None = None) -> None:
        self._finish(turn_id, status="error", reply=error, error=error, now=now)

    def _finish(
        self,
        turn_id: str,
        *,
        status: str,
        reply: str,
        error: str | None,
        now: int | None,
    ) -> None:
        self._ensure_schema()
        ts = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT conversation_id, engine, model FROM pa_turns WHERE id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE pa_turns SET status=?, reply=?, error=?, updated_ts=? WHERE id=?",
                (status, reply, error, ts, turn_id),
            )
            conn.execute(
                "INSERT INTO pa_messages(conversation_id, turn_id, role, content, "
                "engine, model, attachments_json, ts) "
                "VALUES (?, ?, 'assistant', ?, ?, ?, '[]', ?)",
                (
                    row["conversation_id"],
                    turn_id,
                    reply,
                    row["engine"],
                    row["model"],
                    ts,
                ),
            )

    def append_executor_message(
        self,
        event_id: int,
        content: str,
        *,
        now: int | None = None,
    ) -> str:
        """Append one idempotent action-evidence bubble to the default thread."""
        self._ensure_schema()
        ts = int(time.time()) if now is None else int(now)
        turn_id = f"pa_action_{int(event_id)}"
        engine = "pa-executor"
        model = "gated-actions-v1"
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO pa_conversations(id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
                (_DEFAULT_CONVERSATION_ID, ts, ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO pa_turns("
                "id, conversation_id, status, reply, error, engine, model, "
                "project_scope, attachments_json, ts, updated_ts"
                ") VALUES (?, ?, 'done', ?, NULL, ?, ?, NULL, '[]', ?, ?)",
                (turn_id, _DEFAULT_CONVERSATION_ID, content, engine, model, ts, ts),
            )
            exists = conn.execute(
                "SELECT 1 FROM pa_messages WHERE turn_id=? AND role='assistant' "
                "AND engine=? LIMIT 1",
                (turn_id, engine),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO pa_messages(conversation_id, turn_id, role, content, "
                    "engine, model, attachments_json, ts) "
                    "VALUES (?, ?, 'assistant', ?, ?, ?, '[]', ?)",
                    (_DEFAULT_CONVERSATION_ID, turn_id, content, engine, model, ts),
                )
        return turn_id

    def reap_interrupted_turns(
        self,
        *,
        now: int | None = None,
        error: str = "Server-Neustart",
    ) -> int:
        """Terminalize every turn that cannot survive a process restart."""
        self._ensure_schema()
        ts = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, conversation_id, engine, model FROM pa_turns "
                "WHERE status IN ('pending','running') ORDER BY ts, rowid"
            ).fetchall()
            for row in rows:
                cursor = conn.execute(
                    "UPDATE pa_turns SET status='error', reply=?, error=?, updated_ts=? "
                    "WHERE id=? AND status IN ('pending','running')",
                    (error, error, ts, row["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM pa_messages WHERE turn_id=? AND role='assistant' LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO pa_messages("
                        "conversation_id, turn_id, role, content, engine, model, "
                        "attachments_json, ts"
                        ") VALUES (?, ?, 'assistant', ?, ?, ?, '[]', ?)",
                        (
                            row["conversation_id"],
                            row["id"],
                            error,
                            row["engine"],
                            row["model"],
                            ts,
                        ),
                    )
        return len(rows)

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, status, reply, engine, model, ts, error "
                "FROM pa_turns WHERE id=?",
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "turn_id": row["id"],
            "status": row["status"],
            "reply": row["reply"],
            "engine": row["engine"],
            "model": row["model"],
            "ts": int(row["ts"]),
            "error": row["error"],
        }

    def recent_messages(
        self, *, exclude_turn_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        where = "conversation_id=?"
        params: list[Any] = [_DEFAULT_CONVERSATION_ID]
        if exclude_turn_id is not None:
            where += " AND turn_id != ?"
            params.append(exclude_turn_id)
        params.append(HISTORY_MAX_MESSAGES)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT role, content, engine, model, ts FROM pa_messages "
                f"WHERE {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_turns(self, limit: int = 30) -> list[dict[str, Any]]:
        self._ensure_schema()
        limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, status, reply, engine, model, ts, error "
                "FROM pa_turns ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "turn_id": row["id"],
                "status": row["status"],
                "reply": row["reply"],
                "engine": row["engine"],
                "model": row["model"],
                "ts": int(row["ts"]),
                "error": row["error"],
            }
            for row in rows
        ]

    def message_page(
        self,
        *,
        limit: int = 30,
        before_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one chronological bubble page with turn-derived state."""
        self._ensure_schema()
        limit = max(1, min(int(limit), 100))
        if before_id is not None and int(before_id) < 1:
            raise ValueError("before_id muss positiv sein")
        where = "m.conversation_id=?"
        params: list[Any] = [_DEFAULT_CONVERSATION_ID]
        if before_id is not None:
            where += " AND m.id < ?"
            params.append(int(before_id))
        params.append(limit + 1)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT m.id, m.turn_id, m.role, m.content, m.engine, m.model, "
                "m.attachments_json, m.ts, t.status, t.error "
                "FROM pa_messages AS m JOIN pa_turns AS t ON t.id=m.turn_id "
                f"WHERE {where} ORDER BY m.id DESC LIMIT ?",
                params,
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        messages: list[dict[str, Any]] = []
        for row in reversed(selected):
            try:
                asset_ids = json.loads(row["attachments_json"] or "[]")
                if not isinstance(asset_ids, list):
                    asset_ids = []
            except (TypeError, ValueError, json.JSONDecodeError):
                asset_ids = []
            messages.append(
                {
                    "id": int(row["id"]),
                    "turn_id": row["turn_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "engine": row["engine"],
                    "model": row["model"],
                    "attachments": [
                        {"asset_id": str(asset_id)}
                        for asset_id in asset_ids
                        if isinstance(asset_id, str)
                    ],
                    "ts": int(row["ts"]),
                    "status": row["status"],
                    "error": row["error"],
                }
            )
        return {
            "messages": messages,
            "next_before_id": (
                int(selected[-1]["id"]) if has_more and selected else None
            ),
        }

    def feed_page(
        self,
        *,
        since_id: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return an ascending, bounded PA-feed page for polling clients."""
        self._ensure_schema()
        since = int(since_id)
        if since < 0:
            raise ValueError("since_id darf nicht negativ sein")
        bounded_limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, kind, severity, title, ref, delivered_push "
                "FROM pa_feed WHERE id > ? ORDER BY id ASC LIMIT ?",
                (since, bounded_limit + 1),
            ).fetchall()
        has_more = len(rows) > bounded_limit
        selected = rows[:bounded_limit]
        items = [
            {
                "id": int(row["id"]),
                "ts": int(row["ts"]),
                "kind": row["kind"],
                "severity": row["severity"],
                "title": row["title"],
                "ref": row["ref"],
                "delivered_push": int(row["delivered_push"]),
            }
            for row in selected
        ]
        return {
            "items": items,
            "next_since_id": items[-1]["id"] if items else since,
            "has_more": has_more,
        }


def uploads_dir() -> Path:
    return get_hermes_home() / "pa" / "uploads"


def prune_uploads(
    *,
    now: float | None = None,
    ttl_days: int = UPLOAD_TTL_DAYS,
    max_total_bytes: int = UPLOAD_SOFT_MAX_BYTES,
) -> dict[str, int]:
    """Prune expired assets, then oldest assets above the soft size cap."""
    root = uploads_dir()
    if not root.is_dir():
        return {"removed": 0, "removed_bytes": 0, "remaining_bytes": 0}
    now_s = time.time() if now is None else float(now)
    cutoff = now_s - max(1, int(ttl_days)) * 86_400
    cap = max(0, int(max_total_bytes))
    removed = 0
    removed_bytes = 0
    survivors: list[tuple[Path, int, float]] = []
    for candidate in root.iterdir():
        if not _ASSET_ID_RE.fullmatch(candidate.name):
            continue
        try:
            stat = candidate.stat()
        except (FileNotFoundError, OSError):
            continue
        if not candidate.is_file():
            continue
        size = int(stat.st_size)
        if stat.st_mtime < cutoff:
            try:
                candidate.unlink()
                removed += 1
                removed_bytes += size
            except (FileNotFoundError, OSError):
                pass
            continue
        survivors.append((candidate, size, float(stat.st_mtime)))

    total = sum(size for _path, size, _mtime in survivors)
    # Soft cap: retain the newest asset even if a future upload limit exceeds
    # the configured cap; otherwise remove oldest-first until under budget.
    for candidate, size, _mtime in sorted(survivors, key=lambda item: (item[2], item[0].name))[:-1]:
        if total <= cap:
            break
        try:
            candidate.unlink()
            removed += 1
            removed_bytes += size
            total -= size
        except (FileNotFoundError, OSError):
            pass
    return {"removed": removed, "removed_bytes": removed_bytes, "remaining_bytes": total}


def resolve_asset(asset_id: str) -> Path:
    if not _ASSET_ID_RE.fullmatch(asset_id or ""):
        raise ValueError("Ungültige asset_id")
    root = uploads_dir().resolve()
    candidate = (root / asset_id).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Ungültige asset_id") from exc
    if not candidate.is_file():
        raise AssetNotFoundError("Unbekannte asset_id")
    return candidate


def asset_content_type(path: Path) -> str:
    return _UPLOAD_MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


def _bounded_json(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= CONTEXT_PACK_MAX_CHARS:
        return text
    envelope = {
        "truncated": True,
        "context_excerpt": text[: CONTEXT_PACK_MAX_CHARS - 64],
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))[
        :CONTEXT_PACK_MAX_CHARS
    ]


def build_context_pack(project_scope: str | None = None) -> str:
    """Build a bounded live context from the Projekte-tab read models."""
    from hermes_cli import agent_questions
    from hermes_cli.projects_overview import (
        build_agents_payload,
        build_project_detail,
        build_projects_payload,
        build_receipts_payload,
        load_projects_registry,
    )

    registry = load_projects_registry()
    overview = build_projects_payload(registry)
    agents = build_agents_payload(registry)
    receipts = build_receipts_payload(registry)
    try:
        questions = agent_questions.list_question_events(status="open", limit=20)
    except Exception as exc:
        questions = [{"error": str(exc)}]

    payload: dict[str, Any] = {
        "generated_at": int(time.time()),
        "project_scope": project_scope,
        "projects_summary": overview,
        "open_questions": questions,
        "running_chains": agents,
        "recent_receipts": receipts,
    }
    if project_scope:
        entry = next(
            (
                candidate
                for candidate in registry.projects
                if candidate.slug == project_scope
            ),
            None,
        )
        if entry is None:
            payload["project_detail"] = {"error": "unknown project scope"}
        else:
            payload["project_detail"] = build_project_detail(
                entry,
                registry,
                agents_payload=agents,
                receipts_payload=receipts,
            )
    return _bounded_json(payload)


def _bounded_history(history: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    used = 0
    for item in reversed(history):
        role = "Nutzer" if item.get("role") == "user" else "Assistent"
        line = f"{role}: {str(item.get('content') or '').strip()}"
        if used + len(line) + 1 > HISTORY_MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(reversed(lines)) or "(keine frühere PA-Historie)"


def compose_prompt(
    *, text: str, context_pack: str, history: list[dict[str, Any]]
) -> str:
    return (
        f"{PA_SYSTEM_PROMPT}\n\n"
        f"LIVE-KONTEXTPACK (JSON):\n{context_pack}\n\n"
        f"LETZTE PA-HISTORIE:\n{_bounded_history(history)}\n\n"
        f"AKTUELLE FRAGE:\n{text.strip()}"
    )


def parse_pa_action_proposal(
    reply: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Strip typed action blocks and return at most one validated proposal."""
    matches = list(_PA_ACTION_BLOCK_RE.finditer(reply or ""))
    if not matches:
        return reply, None, None
    visible = _PA_ACTION_BLOCK_RE.sub("", reply or "")
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    if len(matches) != 1:
        return (
            visible,
            None,
            "Mehrere Aktionsvorschläge wurden verworfen; erlaubt ist höchstens einer.",
        )

    try:
        decoded = json.loads(matches[0].group(1).strip())
        if not isinstance(decoded, dict):
            raise ValueError("proposal must be an object")
        allowed = {"category", "payload", "reason"}
        if set(decoded) - allowed:
            raise ValueError("unknown proposal fields")
        if "category" not in decoded or "payload" not in decoded:
            raise ValueError("category/payload missing")
        from hermes_cli.agent_questions import build_pa_action_envelope

        envelope = build_pa_action_envelope(
            decoded["category"],
            decoded["payload"],
            reason=decoded.get("reason") or "PA-Vorschlag aus dem aktuellen Turn",
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return (
            visible,
            None,
            "Der Aktionsvorschlag wurde wegen ungültiger oder unbekannter Daten verworfen.",
        )
    return visible, envelope, None


def _reply_with_notice(reply: str, notice: str) -> str:
    prefix = reply.strip()
    rendered = f"Hinweis: {notice}"
    return f"{prefix}\n\n{rendered}" if prefix else rendered


def _hermes_bin() -> str:
    """Resolve the hermes CLI binary.

    The dashboard systemd unit does not inherit an interactive PATH, so a
    bare ``hermes`` lookup can fail there (live E2E finding 2026-07-19).
    Prefer PATH, then the sibling binary of the running interpreter (the
    dashboard runs from the repo venv, which ships ``hermes``).
    """
    path = shutil.which("hermes")
    if path:
        return path
    sibling = Path(sys.executable).with_name("hermes")
    if sibling.is_file():
        return str(sibling)
    return "hermes"


def _claude_bin() -> str:
    path = shutil.which("claude")
    if path:
        return path
    candidate = Path.home() / ".local" / "bin" / "claude"
    return str(candidate) if candidate.is_file() else "claude"


def _kimi_bin() -> str:
    path = shutil.which("kimi")
    if path:
        return path
    for candidate in (
        Path.home() / "bin" / "kimi",
        Path.home() / ".kimi-code" / "bin" / "kimi",
    ):
        if candidate.is_file():
            return str(candidate)
    return "kimi"


def build_sol_argv(
    prompt: str, *, model: str, image_paths: list[Path]
) -> list[str]:
    argv = [
        _hermes_bin(),
        "chat",
        "-Q",
        "-q",
        prompt,
        "-m",
        model,
        "-t",
        READ_ONLY_TOOLSETS,
    ]
    for path in image_paths:
        argv.extend(["--image", str(path)])
    return argv


def build_claude_argv(
    prompt: str, *, model: str, image_paths: list[Path]
) -> list[str]:
    if image_paths:
        raise PAEngineError(
            "Engine 'claude' unterstützt keine Bilder im One-Shot-Modus"
        )
    cli_model = _CLAUDE_CLI_MODELS.get(model)
    if cli_model is None:
        raise PAEngineError("PA-Modell passt nicht zur Engine")
    return [
        _claude_bin(),
        "-p",
        prompt,
        "--model",
        cli_model,
        "--permission-mode",
        "plan",
        "--tools",
        "",
        "--no-session-persistence",
        "--output-format",
        "text",
    ]


def build_kimi_argv(
    prompt: str, *, model: str, image_paths: list[Path]
) -> list[str]:
    if image_paths:
        raise PAEngineError(
            "Engine 'kimi' unterstützt keine Bilder im One-Shot-Modus"
        )
    if model != KIMI_MODEL:
        raise PAEngineError("PA-Modell passt nicht zur Engine")
    # Kimi 0.27.0 rejects prompt mode combined with --plan, --auto, or --yolo;
    # its prompt-mode request also sets toolSelect=false. Do not enable approval.
    return [
        _kimi_bin(),
        "-p",
        prompt,
        "-m",
        KIMI_CLI_MODEL,
        "--output-format",
        "text",
    ]


_ENGINE_ARGV_BUILDERS = {
    "sol": build_sol_argv,
    "claude": build_claude_argv,
    "kimi": build_kimi_argv,
}


def run_engine(
    engine: str, prompt: str, *, model: str, image_paths: list[Path]
) -> str:
    """Run one stateless text turn through the selected engine adapter."""
    spec = ENGINE_REGISTRY.get(engine)
    if spec is None:
        raise PAEngineError("Unbekannte PA-Engine")
    if model not in spec.models:
        raise PAEngineError("PA-Modell passt nicht zur Engine")
    if image_paths and not spec.supports_images:
        raise PAEngineError(
            f"Engine '{engine}' unterstützt keine Bilder im One-Shot-Modus"
        )
    argv = _ENGINE_ARGV_BUILDERS[engine](
        prompt, model=model, image_paths=image_paths
    )
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TURN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PAEngineError("Engine-Zeitlimit erreicht") from exc
    except OSError as exc:
        raise PAEngineError(f"Engine nicht verfügbar: {exc}") from exc
    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or "Engine-Aufruf fehlgeschlagen"
        ).strip()
        raise PAEngineError(f"Engine-Fehler: {detail[:800]}")
    reply = result.stdout.strip()
    if not reply:
        raise PAEngineError("Engine lieferte keine Antwort")
    return reply


def run_sol_engine(prompt: str, *, model: str, image_paths: list[Path]) -> str:
    """Backward-compatible Sprint-1 sol adapter entry point."""
    return run_engine(
        "sol", prompt, model=model, image_paths=image_paths
    )


async def _run_sync(fn: Any, /, *args: Any, executor: Any = None, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, functools.partial(fn, *args, **kwargs))


async def _process_turn(
    store: PAStore,
    *,
    turn_id: str,
    text: str,
    project_scope: str | None,
    engine: str,
    model: str,
    image_paths: list[Path],
) -> None:
    try:
        if not await _run_sync(store.set_running, turn_id):
            return
        context_pack, history = await asyncio.gather(
            _run_sync(build_context_pack, project_scope),
            _run_sync(store.recent_messages, exclude_turn_id=turn_id),
        )
        prompt = compose_prompt(text=text, context_pack=context_pack, history=history)
        reply = await asyncio.wait_for(
            _run_sync(
                run_engine,
                engine,
                prompt,
                model=model,
                image_paths=image_paths,
                executor=_PA_EXECUTOR,
            ),
            timeout=TURN_TIMEOUT_SECONDS + 5,
        )
        reply, proposal, notice = parse_pa_action_proposal(reply)
        if proposal is not None:
            try:
                from hermes_cli.pa_actions import enqueue_pa_action

                event_id = await _run_sync(
                    enqueue_pa_action,
                    proposal["category"],
                    proposal["payload"],
                    reason=proposal.get("reason"),
                )
                if not reply:
                    reply = f"Aktion zur Bestätigung eingereiht (#{event_id})."
            except Exception as exc:
                _log.warning("PA action proposal enqueue failed: %s", exc)
                notice = "Der Aktionsvorschlag konnte nicht eingereiht werden."
        if notice:
            reply = _reply_with_notice(reply, notice)
        await _run_sync(store.finish_turn, turn_id, reply)
    except asyncio.TimeoutError:
        await _run_sync(store.fail_turn, turn_id, "Engine-Zeitlimit erreicht")
    except Exception as exc:
        _log.warning("PA turn %s failed: %s", turn_id, exc)
        message = str(exc).strip() or exc.__class__.__name__
        await _run_sync(store.fail_turn, turn_id, message[:1000])


# ---------------------------------------------------------------------------
# S2.4 Entscheidungs-Inbox ("wartet auf dich")
# ---------------------------------------------------------------------------

INBOX_QUESTION_LIMIT = 50
INBOX_MAX_ITEMS = 100


def _iso_to_epoch(value: object) -> int:
    """Best-effort epoch seconds for question-event ISO timestamps."""
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, OSError):
        return 0


def _kanban_block_radius(conn: Any, task_id: str) -> int:
    """1 + live (non-terminal) descendants in the ``task_links`` chain."""
    row = conn.execute(
        """
        WITH RECURSIVE descendants(id) AS (
            SELECT child_id FROM task_links WHERE parent_id = ?
            UNION
            SELECT tl.child_id FROM task_links tl
            JOIN descendants d ON tl.parent_id = d.id
        )
        SELECT COUNT(*) FROM descendants d
        JOIN tasks t ON t.id = d.id
        WHERE t.status NOT IN ('done', 'archived')
        """,
        (task_id,),
    ).fetchone()
    return 1 + int(row[0] if row else 0)


def build_inbox() -> dict[str, Any]:
    """Aggregate the operator queue: open questions, pa_action cards, held
    chains (blocked/needs_input) and freigabe gates (scheduled + freigabe).

    Each source is isolated — a failing store degrades to an ``errors`` entry
    instead of hiding the other sources (same contract as the context pack).
    Sorted by block radius, then newest first.
    """
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    try:
        from hermes_cli import agent_questions

        events = agent_questions.list_question_events(
            status="open", limit=INBOX_QUESTION_LIMIT
        )
        for event in events:
            kind = event.get("kind")
            item: dict[str, Any] = {
                "type": "pa_action" if kind == "pa_action" else "question",
                "id": f"q{event['id']}",
                "question_id": event["id"],
                "title": event.get("question_text") or "",
                "kind": kind,
                "options": event.get("options") or [],
                "block_radius": 1,
                "ts": _iso_to_epoch(event.get("ts")),
            }
            if kind == "pa_action":
                payload = event.get("action_payload") or {}
                item["category"] = payload.get("category")
                item["action_payload"] = payload
            items.append(item)
    except Exception as exc:
        errors.append({"source": "questions", "error": str(exc)[:500]})

    try:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing() as conn:
            rows = conn.execute(
                "SELECT id, title, status, freigabe, block_kind, created_at "
                "FROM tasks WHERE "
                "(status = 'blocked' AND block_kind = 'needs_input') "
                "OR (freigabe IS NOT NULL AND status = 'scheduled')"
            ).fetchall()
            for row in rows:
                is_gate = row["freigabe"] is not None and row["status"] == "scheduled"
                items.append(
                    {
                        "type": "freigabe_gate" if is_gate else "held_task",
                        "id": row["id"],
                        "card_id": row["id"],
                        "title": row["title"],
                        "status": row["status"],
                        "freigabe": row["freigabe"],
                        "block_radius": _kanban_block_radius(conn, row["id"]),
                        "ts": int(row["created_at"] or 0),
                    }
                )
    except Exception as exc:
        errors.append({"source": "kanban", "error": str(exc)[:500]})

    items.sort(
        key=lambda item: (-int(item.get("block_radius") or 0), -int(item.get("ts") or 0))
    )
    return {
        "generated_at": int(time.time()),
        "items": items[:INBOX_MAX_ITEMS],
        "errors": errors,
    }


def register_pa_routes(app: FastAPI) -> None:
    """Register authenticated PA endpoints before the SPA catch-all."""
    store = PAStore()
    # Ephemeral live-screen-share sessions (S-live). Process-local by design:
    # screen frames must not survive a restart and are never persisted by
    # default — only the frame the user actually asks about is materialised
    # into a normal upload asset via /attach.
    live_share = LiveShareRegistry()
    store.ensure_schema()
    reaped = store.reap_interrupted_turns()
    if reaped:
        _log.warning("Reaped %d interrupted PA turn(s) after server restart", reaped)
    try:
        prune_uploads()
    except OSError:
        _log.warning("PA upload startup prune failed", exc_info=True)
    tasks: set[asyncio.Task[Any]] = set()

    from hermes_cli.pa_planspec import register_pa_planspec_routes
    from hermes_cli.pa_health import register_pa_health_routes

    register_pa_planspec_routes(app)
    register_pa_health_routes(app)

    @app.post("/api/pa/message")
    async def pa_message(payload: MessageIn) -> dict[str, str]:
        engine = (payload.engine or DEFAULT_ENGINE).strip() or DEFAULT_ENGINE
        spec = ENGINE_REGISTRY.get(engine)
        if spec is None:
            raise HTTPException(status_code=400, detail="Unbekannte PA-Engine")
        model = (payload.model or spec.default_model).strip() or spec.default_model
        model = _LEGACY_MODEL_ALIASES.get(engine, {}).get(model, model)
        if model not in spec.models:
            raise HTTPException(
                status_code=400, detail="PA-Modell passt nicht zur Engine"
            )
        if payload.attachments and not spec.supports_images:
            raise HTTPException(
                status_code=400,
                detail=f"Engine '{engine}' unterstützt keine Bilder im One-Shot-Modus",
            )
        try:
            image_paths = [
                await _run_sync(resolve_asset, attachment.asset_id)
                for attachment in payload.attachments
            ]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        turn_id = await _run_sync(
            store.create_turn,
            text=payload.text,
            engine=engine,
            model=model,
            project_scope=payload.project_scope,
            attachments=[attachment.asset_id for attachment in payload.attachments],
        )
        task = asyncio.create_task(
            _process_turn(
                store,
                turn_id=turn_id,
                text=payload.text,
                project_scope=payload.project_scope,
                engine=engine,
                model=model,
                image_paths=image_paths,
            )
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"turn_id": turn_id}

    @app.get("/api/pa/turns/{turn_id}")
    async def pa_turn(turn_id: str) -> dict[str, Any]:
        turn = await _run_sync(store.get_turn, turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="Unbekannter PA-Turn")
        return turn

    @app.get("/api/pa/history")
    async def pa_history(limit: int = 30) -> dict[str, Any]:
        return {"turns": await _run_sync(store.recent_turns, limit)}

    @app.get("/api/pa/inbox")
    async def pa_inbox() -> dict[str, Any]:
        """S2.4 'wartet auf dich' queue: questions + pa_action cards + held
        chains + freigabe gates, sorted by block radius."""
        return await _run_sync(build_inbox)

    @app.post("/api/pa/push/test")
    async def pa_push_test() -> dict[str, Any]:
        """S3.2 proof helper: fire one Jarvis test push at all subscriptions."""
        from hermes_cli.pa_push import send_pa_push

        return await _run_sync(
            send_pa_push,
            title="Jarvis Test-Push",
            body="Wenn du das im Hintergrund siehst, ist S3.2 bewiesen.",
            tag="hermes-pa-test",
        )

    @app.get("/api/pa/graph")
    async def pa_graph() -> dict[str, Any]:
        """S2.7 bounded Estate graph; all blocking source reads stay off-loop."""
        from hermes_cli.pa_graph import build_graph

        return await _run_sync(build_graph)

    @app.get("/api/pa/engines")
    async def pa_engines() -> dict[str, Any]:
        """S2.2 switcher roster: engines with models, defaults, capabilities."""
        return {
            "default_engine": DEFAULT_ENGINE,
            "engines": [
                {
                    "engine": engine,
                    "models": list(spec.models),
                    "default_model": spec.default_model,
                    "supports_images": spec.supports_images,
                }
                for engine, spec in ENGINE_REGISTRY.items()
            ],
        }

    @app.get("/api/pa/messages")
    async def pa_messages(
        limit: int = 30,
        before_id: int | None = None,
    ) -> dict[str, Any]:
        """Chronological user/assistant bubbles for the chat UI.

        /api/pa/history intentionally serves turns without the user text;
        the bubble view needs both roles (review finding 2026-07-19).
        """
        try:
            return await _run_sync(
                store.message_page,
                limit=limit,
                before_id=before_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/pa/feed")
    async def pa_feed(
        since_id: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Browser wire v1: bounded ascending polling by durable feed id."""
        try:
            return await _run_sync(
                store.feed_page,
                since_id=since_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/pa/asset/{asset_id}")
    async def pa_asset(asset_id: str) -> FileResponse:
        if not _ASSET_ID_RE.fullmatch(asset_id or ""):
            raise HTTPException(status_code=400, detail="Ungültige asset_id")
        try:
            path = await _run_sync(resolve_asset, asset_id)
        except AssetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(path, media_type=asset_content_type(path))

    @app.post("/api/pa/upload")
    async def pa_upload(file: UploadFile = File(...)) -> dict[str, str]:
        content_type = (file.content_type or "").lower()
        suffix = _UPLOAD_SUFFIXES.get(content_type)
        if suffix is None:
            raise HTTPException(status_code=400, detail="Upload muss ein Bild sein")
        data = await file.read(UPLOAD_MAX_BYTES + 1)
        if not data:
            raise HTTPException(status_code=400, detail="Leerer Upload")
        if len(data) > UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Bild ist zu groß")
        root = uploads_dir()
        await _run_sync(root.mkdir, parents=True, exist_ok=True)
        asset_id = f"asset_{secrets.token_hex(12)}{suffix}"
        target = root / asset_id
        await _run_sync(target.write_bytes, data)
        try:
            await _run_sync(prune_uploads)
        except OSError:
            _log.warning("PA upload prune failed after %s", asset_id, exc_info=True)
        return {"asset_id": asset_id}

    # ── Live-Screen-Share (S-live) ──────────────────────────────────────
    # A real, continuous getDisplayMedia session — deliberately NOT the image
    # picker. The browser streams the latest frame to /frame (latest wins, one
    # frame per session, no asset pile); /attach materialises the current frame
    # into a single normal upload asset for the existing image-turn pipeline.

    @app.post("/api/pa/live-share/start")
    async def pa_live_share_start() -> dict[str, str]:
        return {"session_id": live_share.start()}

    @app.post("/api/pa/live-share/{session_id}/frame")
    async def pa_live_share_frame(
        session_id: str, file: UploadFile = File(...)
    ) -> dict[str, bool]:
        content_type = (file.content_type or "").lower()
        suffix = _UPLOAD_SUFFIXES.get(content_type)
        if suffix is None:
            raise HTTPException(status_code=400, detail="Frame muss ein Bild sein")
        data = await file.read(LIVE_FRAME_MAX_BYTES + 1)
        if not data:
            raise HTTPException(status_code=400, detail="Leerer Frame")
        if len(data) > LIVE_FRAME_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Frame ist zu groß")
        try:
            live_share.put_frame(session_id, data, suffix)
        except LiveShareNotFound as exc:
            raise HTTPException(
                status_code=404, detail="Unbekannte Live-Share-Session"
            ) from exc
        return {"ok": True}

    @app.post("/api/pa/live-share/{session_id}/attach")
    async def pa_live_share_attach(session_id: str) -> dict[str, str]:
        try:
            data, suffix = live_share.latest_frame(session_id)
        except LiveShareNotFound as exc:
            raise HTTPException(
                status_code=404, detail="Unbekannte Live-Share-Session"
            ) from exc
        except LiveShareNoFrame as exc:
            raise HTTPException(
                status_code=409, detail="Noch kein Bildschirm-Frame empfangen"
            ) from exc
        root = uploads_dir()
        await _run_sync(root.mkdir, parents=True, exist_ok=True)
        asset_id = f"asset_{secrets.token_hex(12)}{suffix}"
        await _run_sync((root / asset_id).write_bytes, data)
        try:
            await _run_sync(prune_uploads)
        except OSError:
            _log.warning("PA live-share prune failed after %s", asset_id, exc_info=True)
        return {"asset_id": asset_id}

    @app.post("/api/pa/live-share/{session_id}/stop")
    async def pa_live_share_stop(session_id: str) -> dict[str, bool]:
        # Idempotent: stopping an already-gone session is not an error (unmount
        # and an explicit stop click can race).
        return {"ok": live_share.stop(session_id)}
