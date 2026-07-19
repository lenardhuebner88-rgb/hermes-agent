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
from pydantic import BaseModel, Field

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
READ_ONLY_TOOLSETS = "search"
TURN_TIMEOUT_SECONDS = 180
DB_BUSY_TIMEOUT_MS = 5_000
CONTEXT_PACK_MAX_CHARS = 14_000
HISTORY_MAX_MESSAGES = 16
HISTORY_MAX_CHARS = 6_000
UPLOAD_MAX_BYTES = 15 * 1024 * 1024


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
PA-Historie. Du darfst NICHTS mutieren: keine Writes, keine Aktionen, keine
Bestätigungen und keine Ausführung von Vorschlägen. Vorschläge gibst du nur als Text.
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
    ts              INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pa_messages_conversation_id
    ON pa_messages(conversation_id, id DESC);
"""

_log = logging.getLogger(__name__)
_PA_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="pa-turn"
)


class PAEngineError(RuntimeError):
    """A user-visible one-shot engine failure."""


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
                "engine, model, ts) VALUES (?, ?, 'user', ?, ?, ?, ?)",
                (_DEFAULT_CONVERSATION_ID, turn_id, text, engine, model, ts),
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
                "engine, model, ts) VALUES (?, ?, 'assistant', ?, ?, ?, ?)",
                (
                    row["conversation_id"],
                    turn_id,
                    reply,
                    row["engine"],
                    row["model"],
                    ts,
                ),
            )

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


def uploads_dir() -> Path:
    return get_hermes_home() / "pa" / "uploads"


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
        raise ValueError("Unbekannte asset_id")
    return candidate


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
        await _run_sync(store.finish_turn, turn_id, reply)
    except asyncio.TimeoutError:
        await _run_sync(store.fail_turn, turn_id, "Engine-Zeitlimit erreicht")
    except Exception as exc:
        _log.warning("PA turn %s failed: %s", turn_id, exc)
        message = str(exc).strip() or exc.__class__.__name__
        await _run_sync(store.fail_turn, turn_id, message[:1000])


def register_pa_routes(app: FastAPI) -> None:
    """Register authenticated PA endpoints before the SPA catch-all."""
    store = PAStore()
    tasks: set[asyncio.Task[Any]] = set()

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

    @app.get("/api/pa/messages")
    async def pa_messages() -> dict[str, Any]:
        """Chronological user/assistant bubbles for the chat UI.

        /api/pa/history intentionally serves turns without the user text;
        the bubble view needs both roles (review finding 2026-07-19).
        """
        return {"messages": await _run_sync(store.recent_messages)}

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
        return {"asset_id": asset_id}
