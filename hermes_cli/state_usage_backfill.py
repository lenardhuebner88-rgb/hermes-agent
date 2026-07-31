"""Exact July usage-fact reconstruction from Hermes state databases.

Source databases are always opened read-only. Applying writes only to the
explicit usage-facts target and only for Kanban runs that have no existing
exact run identity in that target.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from hermes_cli import usage_facts_db as usage_facts_db_mod
from hermes_cli.usage_facts_db import initialize_usage_facts_db, usage_facts_db_path

ORIGIN = "hermes_state_backfill"
CORRELATION_SOURCE = "session_model_usage_run"
RUN_ID_PREFIX = "hermes_state_backfill"
JULY_START = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
AUGUST_START = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
_STATE_COLUMNS = (
    "session_id",
    "model",
    "billing_provider",
    "billing_base_url",
    "billing_mode",
    "task",
    "api_call_count",
    *_TOKEN_COLUMNS,
    "estimated_cost_usd",
    "actual_cost_usd",
    "cost_status",
    "cost_source",
    "first_seen",
    "last_seen",
)
_CHAIN_KEYS = (
    "plan_spec_chain_root",
    "chain_root",
    "chain_id",
    "workflow_id",
)


@dataclass(frozen=True, slots=True)
class _TaskRun:
    run_id: str
    task_id: str
    profile: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _StateSource:
    path: Path
    kind: str


@dataclass(frozen=True, slots=True)
class _DatabaseResult:
    path: str
    kind: str
    status: str
    error: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class _ReconstructedFact:
    task_run: _TaskRun
    fields: dict[str, Any]
    source_kind: str

    @property
    def run_id(self) -> str:
        return f"{RUN_ID_PREFIX}:{self.task_run.run_id}"


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _metadata(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _read_only_connection(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"file:{quote(str(path.resolve(strict=False)), safe='/')}?{query}"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def discover_state_databases(
    hermes_home: Path,
    *,
    active_paths: Sequence[Path | str] | None = None,
    archive_paths: Sequence[Path | str] | None = None,
) -> tuple[_StateSource, ...]:
    """Return active databases first, then the explicitly bounded archives."""
    if active_paths is None:
        active = [hermes_home / "state.db"]
        active.extend(sorted((hermes_home / "profiles").glob("*/state.db")))
    else:
        active = [Path(path) for path in active_paths]
    if archive_paths is None:
        archive = sorted((hermes_home / "_profile-archive").glob("*/state.db"))
        archive.extend(
            path
            for path in sorted(hermes_home.glob("state.db.pre-repair-*"))
            if not path.name.endswith(("-shm", "-wal"))
        )
    else:
        archive = [Path(path) for path in archive_paths]

    sources: list[_StateSource] = []
    seen: set[str] = set()
    for kind, paths in (("active", active), ("archive", archive)):
        for path in paths:
            resolved = path.expanduser().resolve(strict=False)
            key = str(resolved)
            if key in seen or not resolved.is_file():
                continue
            seen.add(key)
            sources.append(_StateSource(resolved, kind))
    return tuple(sources)


def _chunks(values: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _direct_session_ids(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    values = {
        value
        for key in ("worker_session_id", "claude_session_id")
        if (value := _text(metadata.get(key))) is not None
    }
    return tuple(sorted(values))


def _cost_session_references(
    metadata: Mapping[str, Any],
) -> tuple[tuple[str | None, str], ...]:
    raw: object = metadata.get("cost_session_ids")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    references: set[tuple[str | None, str]] = set()
    for item in raw:
        value = _text(item)
        if value is None:
            continue
        if "::" in value:
            raw_path, raw_session_id = value.rsplit("::", 1)
            session_id = _text(raw_session_id)
            path = _text(raw_path)
            if session_id is not None and path is not None:
                references.add((str(Path(path).resolve(strict=False)), session_id))
        else:
            references.add((None, value))
    return tuple(sorted(references, key=lambda item: (item[0] or "", item[1])))


def _load_candidates(
    kanban_path: Path,
    usage_path: Path,
) -> tuple[list[_TaskRun], dict[str, int]]:
    with _read_only_connection(usage_path) as connection:
        existing_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT task_run_id FROM run_usage_facts "
                "WHERE task_run_id IS NOT NULL"
            )
        }
        existing_ids.update(
            str(row[0])
            for row in connection.execute(
                "SELECT run_id FROM run_usage_facts "
                "WHERE run_id GLOB '[0-9]*' AND run_id NOT GLOB '*[^0-9]*'"
            )
        )

    task_totals: dict[str, int] = {}
    candidates: list[_TaskRun] = []
    with _read_only_connection(kanban_path) as connection:
        for row in connection.execute(
            "SELECT task_id, "
            "SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) "
            "FROM task_runs GROUP BY task_id"
        ):
            task_id = _text(row[0])
            if task_id is not None:
                task_totals[task_id] = int(row[1] or 0)
        rows = connection.execute(
            "SELECT id, task_id, profile, metadata FROM task_runs "
            "WHERE started_at>=? AND started_at<? "
            "AND COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) > 0 "
            "ORDER BY id",
            (JULY_START, AUGUST_START),
        )
        for row in rows:
            run_id = str(row["id"])
            task_id = _text(row["task_id"])
            if run_id in existing_ids or task_id is None:
                continue
            candidates.append(
                _TaskRun(
                    run_id=run_id,
                    task_id=task_id,
                    profile=_text(row["profile"]),
                    metadata=_metadata(row["metadata"]),
                )
            )
    return candidates, task_totals


def _load_direct_session_owners(kanban_path: Path) -> dict[str, set[str]]:
    """Map exact metadata session IDs to every run that claims them."""
    owners: dict[str, set[str]] = defaultdict(set)
    with _read_only_connection(kanban_path) as connection:
        for row in connection.execute("SELECT id, metadata FROM task_runs"):
            run_id = str(row["id"])
            for session_id in _direct_session_ids(_metadata(row["metadata"])):
                owners[session_id].add(run_id)
    return owners


def _load_state_rows(
    sources: Sequence[_StateSource],
    session_ids: Sequence[str],
) -> tuple[
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[str, dict[str, list[dict[str, Any]]]],
    list[_DatabaseResult],
]:
    active: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    archive: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    results: list[_DatabaseResult] = []
    for source in sources:
        try:
            with _read_only_connection(
                source.path, immutable=source.kind == "archive"
            ) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='session_model_usage'"
                ).fetchone()
                if table is None:
                    results.append(
                        _DatabaseResult(
                            str(source.path), source.kind, "missing_table"
                        )
                    )
                    continue
                found = active if source.kind == "active" else archive
                for chunk in _chunks(session_ids):
                    placeholders = ", ".join("?" for _ in chunk)
                    rows = connection.execute(
                        f"SELECT {', '.join(_STATE_COLUMNS)} "
                        "FROM session_model_usage "
                        f"WHERE session_id IN ({placeholders}) "
                        "ORDER BY session_id, model, billing_provider, "
                        "billing_base_url, billing_mode, task",
                        tuple(chunk),
                    )
                    for row in rows:
                        session_id = str(row["session_id"])
                        found[session_id].setdefault(str(source.path), []).append(
                            dict(row)
                        )
                results.append(_DatabaseResult(str(source.path), source.kind, "ok"))
        except sqlite3.Error as exc:
            results.append(
                _DatabaseResult(
                    str(source.path),
                    source.kind,
                    "error",
                    type(exc).__name__,
                )
            )
    return active, archive, results


def _select_rows(
    session_id: str,
    *,
    active: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    archive: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    required_path: str | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    for kind, index in (("active", active), ("archive", archive)):
        by_path = index.get(session_id, {})
        if required_path is not None:
            rows = by_path.get(required_path)
            if rows:
                return list(rows), kind
            continue
        if len(by_path) == 1:
            return list(next(iter(by_path.values()))), kind
        if len(by_path) > 1:
            return None, "ambiguous"
    return None, None


def _exact_text(rows: Sequence[Mapping[str, Any]], key: str) -> str | None:
    values = [_text(row.get(key)) for row in rows]
    if not values or any(value is None for value in values):
        return None
    unique = set(values)
    return next(iter(unique)) if len(unique) == 1 else None


def _captured_at(rows: Sequence[Mapping[str, Any]]) -> str | None:
    timestamps = [
        float(value)
        for row in rows
        if (value := row.get("last_seen")) is not None
    ]
    if not timestamps:
        return None
    return datetime.fromtimestamp(max(timestamps), tz=timezone.utc).isoformat()


def _aggregate_fact(
    task_run: _TaskRun,
    rows: Sequence[Mapping[str, Any]],
    session_ids: set[str],
) -> dict[str, Any]:
    tokens = {
        column: sum(int(row.get(column) or 0) for row in rows)
        for column in _TOKEN_COLUMNS
    }
    chain_id = next(
        (
            normalized
            for key in _CHAIN_KEYS
            if (normalized := _text(task_run.metadata.get(key))) is not None
        ),
        None,
    )
    return {
        "origin": ORIGIN,
        "task_run_id": task_run.run_id,
        "task_id": task_run.task_id,
        "chain_id": chain_id,
        "board": "default",
        "session_id": next(iter(session_ids)) if len(session_ids) == 1 else None,
        "correlation_source": CORRELATION_SOURCE,
        "provider": _exact_text(rows, "billing_provider"),
        "model": _exact_text(rows, "model"),
        "model_source": "session_model_usage",
        "lane": next(iter(session_ids)) if len(session_ids) == 1 else None,
        "profile": task_run.profile,
        "call_kind": "state_db_backfill",
        "billing_mode": _exact_text(rows, "billing_mode"),
        "cost_status": _exact_text(rows, "cost_status"),
        "cost_source": _exact_text(rows, "cost_source"),
        **tokens,
        "total_tokens": (
            tokens["input_tokens"]
            + tokens["output_tokens"]
            + tokens["cache_read_tokens"]
            + tokens["cache_write_tokens"]
        ),
        "llm_call_count": sum(int(row.get("api_call_count") or 0) for row in rows),
        "captured_at": _captured_at(rows),
        "source": "measured",
    }


def _existing_usage_by_task(usage_path: Path) -> dict[str, int]:
    totals: dict[str, int] = {}
    with _read_only_connection(usage_path) as connection:
        for row in connection.execute(
            "SELECT task_id, "
            "SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) "
            "FROM run_usage_facts WHERE task_id IS NOT NULL GROUP BY task_id"
        ):
            totals[str(row[0])] = int(row[1] or 0)
    return totals


def backfill_state_usage(
    *,
    db_path: Path | str | None = None,
    kanban_path: Path | str,
    hermes_home: Path | str,
    active_state_paths: Sequence[Path | str] | None = None,
    archive_state_paths: Sequence[Path | str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply the exact July state-usage reconstruction."""
    resolved_db = usage_facts_db_path(db_path).expanduser().resolve(strict=False)
    resolved_kanban = Path(kanban_path).expanduser().resolve(strict=False)
    resolved_home = Path(hermes_home).expanduser().resolve(strict=False)
    if apply:
        initialize_usage_facts_db(resolved_db)

    candidates, task_run_totals = _load_candidates(resolved_kanban, resolved_db)
    direct_ids_by_run = {
        run.run_id: _direct_session_ids(run.metadata) for run in candidates
    }
    cost_refs_by_run = {
        run.run_id: _cost_session_references(run.metadata) for run in candidates
    }
    all_session_ids = sorted(
        {
            session_id
            for run in candidates
            for session_id in (
                *direct_ids_by_run[run.run_id],
                *(reference[1] for reference in cost_refs_by_run[run.run_id]),
            )
        }
    )
    sources = discover_state_databases(
        resolved_home,
        active_paths=active_state_paths,
        archive_paths=archive_state_paths,
    )
    active, archive, database_results = _load_state_rows(sources, all_session_ids)

    primary_owners = _load_direct_session_owners(resolved_kanban)

    reconstructed: list[_ReconstructedFact] = []
    ambiguous_runs = 0
    for task_run in candidates:
        direct_ids = direct_ids_by_run[task_run.run_id]
        if not direct_ids or any(len(primary_owners[sid]) != 1 for sid in direct_ids):
            if direct_ids:
                ambiguous_runs += 1
            continue

        selected: dict[tuple[str, str], list[dict[str, Any]]] = {}
        source_kinds: set[str] = set()
        failed = False
        for session_id in direct_ids:
            rows, kind = _select_rows(
                session_id,
                active=active,
                archive=archive,
            )
            if rows is None or kind in {None, "ambiguous"}:
                failed = True
                if kind == "ambiguous":
                    ambiguous_runs += 1
                break
            source_kinds.add(kind)
            selected[(session_id, kind)] = rows
        if failed:
            continue

        # Qualified cost-session receipts supplement a directly identified run
        # but never seed one: older receipts were sometimes produced by cwd or
        # time-window recovery and are not an independent exact run identity.
        for required_path, session_id in cost_refs_by_run[task_run.run_id]:
            if any(selected_id == session_id for selected_id, _kind in selected):
                continue
            owners = primary_owners.get(session_id, set())
            if owners and owners != {task_run.run_id}:
                failed = True
                ambiguous_runs += 1
                break
            rows, kind = _select_rows(
                session_id,
                active=active,
                archive=archive,
                required_path=required_path,
            )
            if rows is None or kind in {None, "ambiguous"}:
                failed = True
                if kind == "ambiguous":
                    ambiguous_runs += 1
                break
            source_kinds.add(kind)
            selected[(session_id, kind)] = rows
        if failed:
            continue

        all_rows = [row for rows in selected.values() for row in rows]
        fields = _aggregate_fact(
            task_run,
            all_rows,
            {session_id for session_id, _kind in selected},
        )
        reconstructed.append(
            _ReconstructedFact(
                task_run=task_run,
                fields=fields,
                source_kind=(
                    "archive" if "archive" in source_kinds else "active"
                ),
            )
        )

    existing_usage = _existing_usage_by_task(resolved_db)
    proposed_usage: dict[str, int] = defaultdict(int)
    for fact in reconstructed:
        proposed_usage[fact.task_run.task_id] += int(
            fact.fields["input_tokens"] + fact.fields["output_tokens"]
        )
    plausibility_violations = sorted(
        task_id
        for task_id, proposed in proposed_usage.items()
        if existing_usage.get(task_id, 0) + proposed
        > task_run_totals.get(task_id, 0)
    )
    preexisting_violations = sorted(
        task_id
        for task_id, existing in existing_usage.items()
        if existing > task_run_totals.get(task_id, 0)
    )
    rejected_tasks = set(plausibility_violations)
    eligible = [
        fact
        for fact in reconstructed
        if fact.task_run.task_id not in rejected_tasks
    ]

    written = 0
    if apply:
        with usage_facts_db_mod._connection(resolved_db) as connection:
            for fact in eligible:
                existing = connection.execute(
                    "SELECT 1 FROM run_usage_facts "
                    "WHERE task_run_id=? OR run_id=? LIMIT 1",
                    (fact.task_run.run_id, fact.run_id),
                ).fetchone()
                if existing is not None:
                    continue
                usage_facts_db_mod._upsert_run_facts(
                    connection,
                    fact.run_id,
                    fact.fields,
                )
                written += 1

    runs_with_any_session_reference = sum(
        bool(direct_ids_by_run[run.run_id] or cost_refs_by_run[run.run_id])
        for run in candidates
    )
    archive_reconstructed = sum(
        fact.source_kind == "archive" for fact in reconstructed
    )
    token_totals = {
        column: sum(int(fact.fields[column]) for fact in reconstructed)
        for column in (*_TOKEN_COLUMNS, "total_tokens")
    }
    eligible_token_totals = {
        column: sum(int(fact.fields[column]) for fact in eligible)
        for column in (*_TOKEN_COLUMNS, "total_tokens")
    }
    return {
        "applied": apply,
        "month": "2026-07",
        "candidate_runs": len(candidates),
        "runs_with_session_reference": runs_with_any_session_reference,
        "runs_without_session_reference": (
            len(candidates) - runs_with_any_session_reference
        ),
        "reconstructable_runs": len(reconstructed),
        "eligible_runs": len(eligible),
        "plausibility_rejected_runs": len(reconstructed) - len(eligible),
        "active_reconstructed_runs": len(reconstructed) - archive_reconstructed,
        "archive_reconstructed_runs": archive_reconstructed,
        "unreconstructable_with_session_reference": (
            runs_with_any_session_reference - len(reconstructed)
        ),
        "ambiguous_runs": ambiguous_runs,
        "rows_written": written,
        "token_totals": token_totals,
        "eligible_token_totals": eligible_token_totals,
        "preexisting_plausibility_violation_count": len(
            preexisting_violations
        ),
        "plausibility_violation_count": len(plausibility_violations),
        "plausibility_violation_task_ids": plausibility_violations[:20],
        "state_databases": [result.as_dict() for result in database_results],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    default_home = Path.home() / ".hermes"
    parser = argparse.ArgumentParser(
        description=(
            "Preview/apply the exact July session_model_usage backfill. "
            "Source databases are always read-only."
        )
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--kanban-db", type=Path, default=default_home / "kanban.db")
    parser.add_argument("--hermes-home", type=Path, default=default_home)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = backfill_state_usage(
        db_path=args.db,
        kanban_path=args.kanban_db,
        hermes_home=args.hermes_home,
        apply=args.apply,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
