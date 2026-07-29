"""Exact, read-only worker correlation for harvested usage facts.

The adapter deliberately refuses fuzzy joins. A foreign CLI session is linked
to a Kanban task only when task-run metadata names that exact session. Ambiguous
sessions remain uncorrelated instead of being charged to the wrong worker.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import quote

_CLAUDE_SESSION_KEY = "claude_session_id"
_CHAIN_KEYS = (
    "plan_spec_chain_root",
    "chain_root",
    "chain_id",
    "workflow_id",
)
_SQLITE_PARAMETER_BATCH = 500


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _metadata(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


@dataclass(frozen=True, slots=True)
class WorkerCorrelation:
    """A correlation proven by one exact session identifier."""

    session_id: str
    task_id: str
    board: str | None = None
    task_run_id: str | None = None
    chain_id: str | None = None
    profile: str | None = None
    source: str = "claude_session_id_run"

    def as_run_fields(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "session_id": self.session_id,
                "task_run_id": self.task_run_id,
                "task_id": self.task_id,
                "chain_id": self.chain_id,
                "board": self.board,
                "profile": self.profile,
                "correlation_source": self.source,
            }.items()
            if value is not None
        }


def discover_kanban_databases(
    paths: Sequence[Path | str] | None = None,
) -> tuple[Path, ...]:
    """Return explicit DBs or all profile-visible board DBs, deterministically."""
    if paths is not None:
        candidates = [Path(path).expanduser() for path in paths]
    else:
        from hermes_cli.kanban_db import boards_root, kanban_home

        home = kanban_home()
        candidates = []
        override = _text(os.environ.get("HERMES_KANBAN_DB"))
        if override is not None:
            candidates.append(Path(override).expanduser())
        candidates.append(home / "kanban.db")
        candidates.extend(sorted(boards_root().glob("*/kanban.db")))

    unique: dict[str, Path] = {}
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            unique.setdefault(str(resolved), resolved)
    return tuple(unique.values())


def _board_for_path(path: Path) -> str | None:
    from hermes_cli.kanban_db import board_slug_for_db_path

    return board_slug_for_db_path(path)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _chunks(values: Sequence[str]) -> Iterable[Sequence[str]]:
    for offset in range(0, len(values), _SQLITE_PARAMETER_BATCH):
        yield values[offset : offset + _SQLITE_PARAMETER_BATCH]


def load_claude_session_correlations(
    session_ids: Iterable[str],
    *,
    kanban_paths: Sequence[Path | str] | None = None,
) -> dict[str, WorkerCorrelation]:
    """Resolve only unambiguous Claude session → worker relationships.

    Multiple continuations may legitimately reuse a session for one task. In
    that case the task link remains exact but ``task_run_id`` stays NULL.
    Sessions spanning tasks or boards are excluded completely.
    """
    wanted = sorted({_text(value) for value in session_ids} - {None})
    if not wanted:
        return {}

    matches: dict[str, list[dict[str, str | None]]] = defaultdict(list)
    for path in discover_kanban_databases(kanban_paths):
        board = _board_for_path(path)
        board_key = str(path.resolve(strict=False))
        try:
            with closing(_read_only_connection(path)) as connection:
                for chunk in _chunks(wanted):
                    placeholders = ", ".join("?" for _ in chunk)
                    rows = connection.execute(
                        "SELECT id, task_id, profile, metadata FROM task_runs "
                        "WHERE json_valid(metadata) "
                        f"AND json_extract(metadata, '$.{_CLAUDE_SESSION_KEY}') "
                        f"IN ({placeholders})",
                        tuple(chunk),
                    )
                    for row in rows:
                        metadata = _metadata(row["metadata"])
                        session_id = _text(metadata.get(_CLAUDE_SESSION_KEY))
                        task_id = _text(row["task_id"])
                        if session_id is None or task_id is None:
                            continue
                        chain_id = next(
                            (
                                normalized
                                for key in _CHAIN_KEYS
                                if (normalized := _text(metadata.get(key))) is not None
                            ),
                            None,
                        )
                        matches[session_id].append({
                            "task_run_id": _text(row["id"]),
                            "task_id": task_id,
                            "chain_id": chain_id,
                            "board": board,
                            "board_key": board_key,
                            "profile": _text(row["profile"]),
                        })
        except sqlite3.Error:
            # Correlation is enrichment. An unavailable/old board must not
            # break the usage harvester or turn absence into guessed data.
            continue

    resolved: dict[str, WorkerCorrelation] = {}
    for session_id, rows in matches.items():
        task_ids = {row["task_id"] for row in rows if row["task_id"]}
        board_keys = {row["board_key"] for row in rows if row["board_key"]}
        if len(task_ids) != 1 or len(board_keys) != 1:
            continue
        task_run_ids = {row["task_run_id"] for row in rows if row["task_run_id"]}
        chain_ids = {row["chain_id"] for row in rows if row["chain_id"]}
        profiles = {row["profile"] for row in rows if row["profile"]}
        boards = {row["board"] for row in rows if row["board"]}
        exact_run = next(iter(task_run_ids)) if len(task_run_ids) == 1 else None
        resolved[session_id] = WorkerCorrelation(
            session_id=session_id,
            task_id=next(iter(task_ids)),
            board=next(iter(boards)) if len(boards) == 1 else None,
            task_run_id=exact_run,
            chain_id=next(iter(chain_ids)) if len(chain_ids) == 1 else None,
            profile=next(iter(profiles)) if len(profiles) == 1 else None,
            source=(
                "claude_session_id_run"
                if exact_run is not None
                else "claude_session_id_task"
            ),
        )
    return resolved
