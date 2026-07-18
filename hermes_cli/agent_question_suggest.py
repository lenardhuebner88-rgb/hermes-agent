"""Asynchronous AI answer suggestions for stored agent questions."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from agent.auxiliary_client import call_llm
from hermes_cli.config import get_hermes_home, load_config

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-5.6-terra"
_CONTEXT_CHAR_CAP = 14_000
_LLM_TIMEOUT_SECONDS = 7.0


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _bounded_context(
    *,
    question_text: str,
    options: list[dict[str, Any]],
    task: dict[str, Any] | None,
    task_events: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    cwd: str | None,
    kind: str | None,
) -> str:
    """Render required context in priority order under an approximate 3.5k-token cap."""
    task_data = task or {}
    sections = [
        ("QUESTION", _clip(question_text, 3_000)),
        (
            "OPTIONS",
            _clip(json.dumps(options, ensure_ascii=False, separators=(",", ":")), 2_200),
        ),
        (
            "OWNING TASK",
            _clip(
                json.dumps(
                    {
                        "title": task_data.get("title"),
                        "body": task_data.get("body"),
                        "acceptance_criteria": task_data.get("acceptance_criteria"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                3_200,
            ),
        ),
        (
            "LAST TASK EVENTS",
            _clip(json.dumps(task_events[:5], ensure_ascii=False, separators=(",", ":")), 2_200),
        ),
        (
            "PROJECT RECEIPTS",
            _clip(json.dumps(receipts[:3], ensure_ascii=False, separators=(",", ":")), 1_500),
        ),
        (
            "RUNTIME",
            _clip(
                json.dumps({"cwd": cwd, "kind": kind}, ensure_ascii=False, separators=(",", ":")),
                500,
            ),
        ),
    ]
    rendered = "\n\n".join(f"## {heading}\n{body}" for heading, body in sections)
    return rendered[:_CONTEXT_CHAR_CAP]


def _task_events(kanban_db_path: Path, task_id: str | None) -> list[dict[str, Any]]:
    if not task_id or not kanban_db_path.exists():
        return []
    uri = f"file:{kanban_db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT kind, payload, created_at FROM task_events "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 5",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _build_context(event: dict[str, Any], *, kanban_db_path: Path | None = None) -> str:
    """Collect bounded task/project context without writing board state."""
    from hermes_cli import projects_overview

    resolved_kanban = kanban_db_path or (get_hermes_home() / "kanban.db")
    registry = projects_overview.load_projects_registry()
    agents, _errors = projects_overview._tmux_agents(
        tmux_panes_text=None,
        tmux_sessions_text=None,
        registry=registry,
        kanban_db_path=resolved_kanban,
    )
    owner = next(
        (
            agent
            for agent in agents
            if agent.get("tmux_session") == event.get("session")
            and event.get("window")
            in {agent.get("tmux_window"), agent.get("tmux_window_name")}
        ),
        None,
    )
    task = None
    task_id = None
    project = None
    if owner is not None:
        task_id = owner.get("task_id")
        project = owner.get("project")
        task = {
            "title": owner.get("task"),
            "body": owner.get("task_body"),
            "acceptance_criteria": owner.get("task_acceptance_criteria"),
        }

    receipt_payload = projects_overview.build_receipts_payload(
        registry, project=str(project) if project is not None else None, limit=3
    )
    receipts = receipt_payload.get("receipts", []) if project is not None else []
    options = event.get("options")
    return _bounded_context(
        question_text=str(event.get("question_text") or ""),
        options=options if isinstance(options, list) else [],
        task=task,
        task_events=_task_events(resolved_kanban, str(task_id) if task_id else None),
        receipts=receipts,
        cwd=str(event.get("cwd")) if event.get("cwd") is not None else None,
        kind=str(event.get("kind")) if event.get("kind") is not None else None,
    )


def _configured_model() -> str:
    config = load_config() or {}
    questions = config.get("agent_questions") or {}
    suggest = questions.get("suggest") or {}
    return str(suggest.get("model") or _DEFAULT_MODEL)


def _parse_suggestion(content: str, valid_nrs: set[int]) -> tuple[list[dict[str, Any]], str]:
    raw = str(content or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    data = json.loads(raw)
    ranked = data.get("ranked") if isinstance(data, dict) else None
    confidence = data.get("confidence") if isinstance(data, dict) else None
    if not isinstance(ranked, list) or not ranked or confidence not in {"high", "low"}:
        raise ValueError("invalid suggestion response shape")

    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in ranked:
        if not isinstance(item, dict):
            raise ValueError("invalid ranked item")
        nr = item.get("nr")
        rationale = item.get("rationale")
        if isinstance(nr, bool) or not isinstance(nr, int) or nr not in valid_nrs or nr in seen:
            raise ValueError("ranked option does not reference a stored option")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("ranked rationale must be non-empty")
        seen.add(nr)
        normalized.append({"nr": nr, "rationale": rationale.strip()})
    return normalized, str(confidence)


def precompute_question_suggestion(
    event_id: int,
    *,
    db_path: Path | None = None,
    kanban_db_path: Path | None = None,
) -> bool:
    """Compute and persist one suggestion; every failure degrades to no suggestion."""
    from hermes_cli import agent_questions
    from hermes_cli.sqlite_util import write_txn

    started = time.monotonic()
    try:
        event = agent_questions._load_event(int(event_id), db_path=db_path)
        if event is None or event.get("status") != "open":
            return False
        options = event.get("options")
        if not isinstance(options, list) or not options:
            return False
        valid_nrs = {
            int(option["nr"])
            for option in options
            if isinstance(option, dict)
            and isinstance(option.get("nr"), int)
            and not isinstance(option.get("nr"), bool)
        }
        if not valid_nrs:
            return False

        model = _configured_model()
        context = _build_context(event, kanban_db_path=kanban_db_path)
        response = call_llm(
            task="agent_question_suggest",
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Rank the stored answer options for the agent question. "
                        "Return only JSON as {\"ranked\":[{\"nr\":int,"
                        "\"rationale\":str}],\"confidence\":\"high\"|\"low\"}. "
                        "Use only option numbers present in the context."
                    ),
                },
                {"role": "user", "content": context},
            ],
            temperature=0,
            max_tokens=700,
            timeout=_LLM_TIMEOUT_SECONDS,
            extra_body={"response_format": {"type": "json_object"}},
        )
        content = response.choices[0].message.content
        ranked, confidence = _parse_suggestion(content, valid_nrs)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        suggested_by = str(getattr(response, "model", None) or model)
        target = db_path if db_path is not None else agent_questions.question_events_db_path()
        with agent_questions.connect_closing(db_path=target) as conn:
            with write_txn(conn):
                cur = conn.execute(
                    "UPDATE question_events SET suggestions_json = ?, suggested_by = ?, "
                    "suggested_ts = ?, suggest_latency_ms = ?, suggest_confidence = ? "
                    "WHERE id = ? AND status = 'open' AND suggestions_json IS NULL",
                    (
                        json.dumps(ranked, ensure_ascii=False, separators=(",", ":")),
                        suggested_by,
                        agent_questions._iso_now(),
                        elapsed_ms,
                        confidence,
                        int(event_id),
                    ),
                )
        return int(cur.rowcount or 0) == 1
    except Exception:
        logger.warning("question suggestion failed event_id=%s", event_id, exc_info=True)
        return False


def schedule_question_suggestion(
    event_id: int,
    *,
    db_path: Path | None = None,
    kanban_db_path: Path | None = None,
) -> threading.Thread:
    """Start one daemon precompute thread and return immediately."""
    from hermes_cli import agent_questions

    resolved_db = db_path if db_path is not None else agent_questions.question_events_db_path()
    thread = threading.Thread(
        target=precompute_question_suggestion,
        kwargs={
            "event_id": int(event_id),
            "db_path": resolved_db,
            "kanban_db_path": kanban_db_path,
        },
        name=f"agent-question-suggest-{int(event_id)}",
        daemon=True,
    )
    thread.start()
    return thread
