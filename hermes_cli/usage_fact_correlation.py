"""Exact, read-only worker correlation for harvested usage facts.

The adapter deliberately refuses fuzzy joins. A foreign CLI session is linked
to a Kanban task only when task-run metadata names that exact session. Ambiguous
sessions remain uncorrelated instead of being charged to the wrong worker.
"""

from __future__ import annotations

import json
import os
import re
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
_CLAUDE_WORKTREE_RUN_RE = re.compile(
    r"(?<![0-9A-Za-z])kanban(?:-|/)review(?:-|/)"
    r"t(?:-|_)(?P<task_hex>[0-9a-fA-F]{8})(?:-|_)"
    r"r(?P<task_run_id>[0-9]+)(?![0-9])"
)


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
    """A correlation proven by one exact session or worktree identifier."""

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


@dataclass(frozen=True, slots=True)
class CorrelationScan:
    """Authoritative positives plus explicitly proven ambiguous sessions.

    A session absent from ``correlations`` and ``ambiguous_session_ids`` is
    merely unresolved. That distinction matters when a board database is
    missing, locked, or still on an older schema.
    """

    correlations: dict[str, WorkerCorrelation]
    ambiguous_session_ids: frozenset[str]
    databases_scanned: int
    databases_failed: int
    database_results: tuple[DatabaseScanResult, ...] = ()


@dataclass(frozen=True, slots=True)
class DatabaseScanResult:
    """Session-content-free outcome for one board database read."""

    path: str
    board: str | None
    status: str
    error: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "board": self.board,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class _WorktreeRunAnchor:
    """One run identity encoded in a Claude project/transcript path."""

    session_id: str
    task_id: str
    task_run_id: str


def _transcript_session_id(path: Path) -> str | None:
    """Return the correlation identity carried by a transcript path.

    Main transcripts use their filename stem. Subagent transcripts are charged
    through the exact parent-session directory, matching the harvester's
    existing ``lane`` contract.
    """
    parts = path.parts
    if path.parent.name == "subagents" and len(parts) >= 3:
        return _text(parts[-3])
    return _text(path.stem)


def _worktree_run_anchors(
    transcript_paths: Iterable[Path | str],
    *,
    wanted_session_ids: set[str],
) -> tuple[dict[str, _WorktreeRunAnchor], frozenset[str]]:
    """Extract only unique ``session -> (task, run)`` path anchors."""
    candidates: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for raw_path in transcript_paths:
        path = Path(raw_path)
        session_id = _transcript_session_id(path)
        if session_id is None or session_id not in wanted_session_ids:
            continue
        matches = {
            (
                f"t_{match.group('task_hex').lower()}",
                match.group("task_run_id"),
            )
            for match in _CLAUDE_WORKTREE_RUN_RE.finditer(str(path))
        }
        candidates[session_id].update(matches)

    anchors: dict[str, _WorktreeRunAnchor] = {}
    ambiguous: set[str] = set()
    for session_id, matches in candidates.items():
        if len(matches) != 1:
            if matches:
                ambiguous.add(session_id)
            continue
        task_id, task_run_id = next(iter(matches))
        anchors[session_id] = _WorktreeRunAnchor(
            session_id=session_id,
            task_id=task_id,
            task_run_id=task_run_id,
        )
    return anchors, frozenset(ambiguous)


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


def scan_claude_session_correlations(
    session_ids: Iterable[str],
    *,
    kanban_paths: Sequence[Path | str] | None = None,
    transcript_paths: Iterable[Path | str] = (),
) -> CorrelationScan:
    """Resolve only unambiguous Claude session/path → worker relationships.

    Multiple continuations may legitimately reuse a session for one task. In
    that case the task link remains exact but ``task_run_id`` stays NULL.
    When metadata has no session link, an encoded review-worktree path may
    provide an exact run anchor. The path anchor is accepted only after the
    board row proves that the run exists and belongs to the encoded task.
    Sessions spanning tasks, runs, or boards are excluded completely.
    """
    wanted = sorted({_text(value) for value in session_ids} - {None})
    if not wanted:
        return CorrelationScan({}, frozenset(), 0, 0, ())

    wanted_set = set(wanted)
    path_anchors, path_anchor_ambiguities = _worktree_run_anchors(
        transcript_paths,
        wanted_session_ids=wanted_set,
    )
    anchors_by_run: dict[str, list[_WorktreeRunAnchor]] = defaultdict(list)
    for anchor in path_anchors.values():
        anchors_by_run[anchor.task_run_id].append(anchor)

    matches: dict[str, list[dict[str, str | None]]] = defaultdict(list)
    path_matches: dict[str, list[dict[str, str | None]]] = defaultdict(list)
    database_paths = discover_kanban_databases(kanban_paths)
    databases_scanned = 0
    databases_failed = 0
    database_results: list[DatabaseScanResult] = []
    for path in database_paths:
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
                for chunk in _chunks(sorted(anchors_by_run)):
                    placeholders = ", ".join("?" for _ in chunk)
                    rows = connection.execute(
                        "SELECT id, task_id, profile, metadata FROM task_runs "
                        f"WHERE id IN ({placeholders})",
                        tuple(chunk),
                    )
                    for row in rows:
                        task_run_id = _text(row["id"])
                        task_id = _text(row["task_id"])
                        if task_run_id is None or task_id is None:
                            continue
                        metadata = _metadata(row["metadata"])
                        chain_id = next(
                            (
                                normalized
                                for key in _CHAIN_KEYS
                                if (
                                    normalized := _text(metadata.get(key))
                                )
                                is not None
                            ),
                            None,
                        )
                        for anchor in anchors_by_run.get(task_run_id, ()):
                            if anchor.task_id != task_id:
                                continue
                            path_matches[anchor.session_id].append({
                                "task_run_id": task_run_id,
                                "task_id": task_id,
                                "chain_id": chain_id,
                                "board": board,
                                "board_key": board_key,
                                "profile": _text(row["profile"]),
                            })
            databases_scanned += 1
            database_results.append(
                DatabaseScanResult(str(path), board, "ok")
            )
        except sqlite3.Error as exc:
            # Correlation is enrichment. An unavailable/old board must not
            # break the usage harvester or turn absence into guessed data.
            databases_failed += 1
            database_results.append(
                DatabaseScanResult(
                    str(path), board, "error", type(exc).__name__
                )
            )
            continue

    def resolve_rows(
        session_id: str,
        rows: Sequence[Mapping[str, str | None]],
        *,
        source_prefix: str,
    ) -> WorkerCorrelation | None:
        task_ids = {row["task_id"] for row in rows if row["task_id"]}
        board_keys = {row["board_key"] for row in rows if row["board_key"]}
        if len(task_ids) != 1 or len(board_keys) != 1:
            return None
        task_run_ids = {row["task_run_id"] for row in rows if row["task_run_id"]}
        chain_ids = {row["chain_id"] for row in rows if row["chain_id"]}
        profiles = {row["profile"] for row in rows if row["profile"]}
        boards = {row["board"] for row in rows if row["board"]}
        exact_run = next(iter(task_run_ids)) if len(task_run_ids) == 1 else None
        return WorkerCorrelation(
            session_id=session_id,
            task_id=next(iter(task_ids)),
            board=next(iter(boards)) if len(boards) == 1 else None,
            task_run_id=exact_run,
            chain_id=next(iter(chain_ids)) if len(chain_ids) == 1 else None,
            profile=next(iter(profiles)) if len(profiles) == 1 else None,
            source=(
                f"{source_prefix}_run"
                if exact_run is not None
                else f"{source_prefix}_task"
            ),
        )

    resolved: dict[str, WorkerCorrelation] = {}
    ambiguous_session_ids: set[str] = set()
    for session_id, rows in matches.items():
        correlation = resolve_rows(
            session_id,
            rows,
            source_prefix="claude_session_id",
        )
        if correlation is None:
            ambiguous_session_ids.add(session_id)
            continue
        resolved[session_id] = correlation

    for session_id in wanted:
        # Metadata is the stronger, already-established exact anchor. The path
        # fallback is intentionally considered only when metadata is silent.
        if session_id in resolved or session_id in ambiguous_session_ids:
            continue
        if session_id in path_anchor_ambiguities:
            ambiguous_session_ids.add(session_id)
            continue
        rows = path_matches.get(session_id, ())
        if not rows:
            continue
        correlation = resolve_rows(
            session_id,
            rows,
            source_prefix="claude_worktree_path",
        )
        if correlation is None or correlation.task_run_id is None:
            ambiguous_session_ids.add(session_id)
            continue
        resolved[session_id] = correlation
    return CorrelationScan(
        correlations=resolved,
        ambiguous_session_ids=frozenset(ambiguous_session_ids),
        databases_scanned=databases_scanned,
        databases_failed=databases_failed,
        database_results=tuple(database_results),
    )


def load_claude_session_correlations(
    session_ids: Iterable[str],
    *,
    kanban_paths: Sequence[Path | str] | None = None,
) -> dict[str, WorkerCorrelation]:
    """Backward-compatible resolved-only view of a correlation scan."""
    return scan_claude_session_correlations(
        session_ids,
        kanban_paths=kanban_paths,
    ).correlations
