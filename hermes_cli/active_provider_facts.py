"""Read-time classification and provider recovery for Kanban task runs.

The Kanban database remains the immutable source of recorded run state.  This
module projects one fact per ``task_runs`` row and augments missing providers
from read-only evidence; it never writes recovered values back to the board.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

NEVER_RAN_STATUSES = frozenset({"reclaimed", "scheduled", "spawn_failed"})

CLASS_NEVER_RAN = "nie_gelaufen"
CLASS_UNKNOWN = "unbekannt"
CLASS_RECONSTRUCTED = "rekonstruiert"
CLASS_STAMPED = "gestempelt"
CLASSIFICATIONS = (
    CLASS_NEVER_RAN,
    CLASS_UNKNOWN,
    CLASS_RECONSTRUCTED,
    CLASS_STAMPED,
)

SOURCE_METADATA_MODEL = "metadata.model"
SOURCE_PROFILE_STATE = "profile_state_db"
SOURCE_CLI_RAW = "cli_raw"


@dataclass(frozen=True, slots=True)
class ActiveProviderFact:
    """The read-time provider classification for one board run."""

    run_id: int | str
    classification: str
    provider: str | None
    model: str | None
    source: str | None
    profile: str | None
    origin: str | None


@dataclass(frozen=True, slots=True)
class MetadataModelAudit:
    """Counts that distinguish the top-level model key from nested keys."""

    top_level_runs: int
    any_level_runs: int
    nested_runs: int
    nested_only_runs: int


@dataclass(frozen=True, slots=True)
class _BoardRun:
    run_id: int | str
    status: str
    active_provider: str | None
    profile: str | None
    metadata: dict[str, Any]
    started_at: float | None
    ended_at: float | None
    last_heartbeat_at: float | None


@dataclass(frozen=True, slots=True)
class _CliFact:
    fact_id: str
    model: str
    captured_at: float
    origin: str


class _StateSessionReader:
    def __init__(self, profiles_root: Path, *, immutable: bool) -> None:
        self._profiles_root = profiles_root.resolve()
        self._immutable = immutable
        self._sessions: dict[
            Path, dict[str, tuple[str | None, str | None]]
        ] = {}

    def profile_path(self, profile: str | None) -> Path | None:
        if not profile or "/" in profile or profile in {".", ".."}:
            return None
        return self._allowed_path(self._profiles_root / profile / "state.db")

    def embedded_path(self, raw_path: str) -> Path | None:
        return self._allowed_path(Path(raw_path).expanduser())

    def session(
        self, path: Path, session_id: str
    ) -> tuple[str | None, str | None] | None:
        sessions = self._sessions.get(path)
        if sessions is None:
            sessions = self._load(path)
            self._sessions[path] = sessions
        return sessions.get(session_id)

    def _allowed_path(self, path: Path) -> Path | None:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self._profiles_root)
        except ValueError:
            return None
        if len(relative.parts) != 2 or relative.parts[1] != "state.db":
            return None
        return resolved

    def _load(self, path: Path) -> dict[str, tuple[str | None, str | None]]:
        if not path.is_file():
            return {}
        try:
            with _read_only_connection(path, immutable=self._immutable) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='sessions'"
                ).fetchone()
                if table is None:
                    return {}
                return {
                    str(row["id"]): (
                        _text(row["billing_provider"]),
                        _text(row["model"]),
                    )
                    for row in connection.execute(
                        "SELECT id, billing_provider, model FROM sessions"
                    )
                }
        except sqlite3.Error:
            return {}


def resolve_active_provider_facts(
    kanban_path: str | Path,
    *,
    usage_facts_path: str | Path | None = None,
    profiles_root: str | Path | None = None,
    immutable_kanban: bool = False,
    immutable_state: bool = False,
    immutable_usage_facts: bool = False,
) -> list[ActiveProviderFact]:
    """Return exactly one classified provider fact for every board run.

    All SQLite inputs are opened with ``mode=ro``.  Provider reconstruction is
    deliberately conservative: ambiguous configuration, state, or time-window
    matches remain ``unbekannt``.
    """

    board_path = Path(kanban_path)
    with _read_only_connection(
        board_path, immutable=immutable_kanban
    ) as connection:
        runs = _load_board_runs(connection)
        routes = _load_lane_routes(connection)

    recovered: dict[
        int | str, tuple[str, str | None, str, str]
    ] = {}

    for run in runs:
        if run.status in NEVER_RAN_STATUSES or run.active_provider:
            continue
        model = _text(run.metadata.get("model"))
        if not model:
            continue
        provider = _metadata_provider(run.metadata)
        if not provider:
            provider = _configured_provider(routes, run.profile, model)
        if provider:
            recovered[run.run_id] = (
                provider,
                model,
                SOURCE_METADATA_MODEL,
                "hermes_agent",
            )

    if profiles_root is not None:
        state_reader = _StateSessionReader(
            Path(profiles_root), immutable=immutable_state
        )
        for run in runs:
            if (
                run.status in NEVER_RAN_STATUSES
                or run.active_provider
                or run.run_id in recovered
            ):
                continue
            candidate = _state_provider(run, state_reader, routes)
            if candidate is not None:
                provider, model = candidate
                recovered[run.run_id] = (
                    provider,
                    model,
                    SOURCE_PROFILE_STATE,
                    "hermes_agent",
                )

    if usage_facts_path is not None:
        unresolved = [
            run
            for run in runs
            if run.status not in NEVER_RAN_STATUSES
            and not run.active_provider
            and run.run_id not in recovered
        ]
        cli_recovered = _cli_providers(
            unresolved,
            Path(usage_facts_path),
            routes,
            immutable=immutable_usage_facts,
        )
        recovered.update(cli_recovered)

    result: list[ActiveProviderFact] = []
    for run in runs:
        if run.status in NEVER_RAN_STATUSES:
            fact = ActiveProviderFact(
                run_id=run.run_id,
                classification=CLASS_NEVER_RAN,
                provider=None,
                model=None,
                source=None,
                profile=run.profile,
                origin=None,
            )
        elif run.active_provider:
            fact = ActiveProviderFact(
                run_id=run.run_id,
                classification=CLASS_STAMPED,
                provider=run.active_provider,
                model=_text(run.metadata.get("model")),
                source=None,
                profile=run.profile,
                origin=None,
            )
        elif run.run_id in recovered:
            provider, model, source, origin = recovered[run.run_id]
            fact = ActiveProviderFact(
                run_id=run.run_id,
                classification=CLASS_RECONSTRUCTED,
                provider=provider,
                model=model,
                source=source,
                profile=run.profile,
                origin=origin,
            )
        else:
            fact = ActiveProviderFact(
                run_id=run.run_id,
                classification=CLASS_UNKNOWN,
                provider=None,
                model=None,
                source=None,
                profile=run.profile,
                origin=None,
            )
        result.append(fact)

    _assert_total_partition(runs, result)
    return result


def materialize_reconstructed_provider_facts(
    kanban_path: str | Path,
    *,
    usage_facts_path: str | Path,
    profiles_root: str | Path | None = None,
    immutable_kanban: bool = False,
    immutable_state: bool = False,
) -> list[ActiveProviderFact]:
    """Project all runs and persist only recovered facts outside the board.

    The usage-facts database is the sole write target.  Existing measured
    provider/model values win over derived recovery on conflict.
    """

    resolved_usage_path = Path(usage_facts_path).expanduser().resolve()
    facts = resolve_active_provider_facts(
        kanban_path,
        usage_facts_path=resolved_usage_path,
        profiles_root=profiles_root,
        immutable_kanban=immutable_kanban,
        immutable_state=immutable_state,
    )
    reconstructed = [
        fact
        for fact in facts
        if fact.classification == CLASS_RECONSTRUCTED
        and fact.provider
        and fact.source
        and fact.origin
    ]
    captured_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    with sqlite3.connect(resolved_usage_path) as connection:
        connection.executemany(
            """
            INSERT INTO run_usage_facts (
                run_id, origin, provider, model, model_source, profile,
                captured_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'derived')
            ON CONFLICT(run_id) DO UPDATE SET
                origin=CASE
                    WHEN run_usage_facts.provider IS NULL
                    THEN excluded.origin
                    ELSE run_usage_facts.origin
                END,
                provider=COALESCE(
                    run_usage_facts.provider, excluded.provider
                ),
                model=COALESCE(run_usage_facts.model, excluded.model),
                model_source=COALESCE(
                    run_usage_facts.model_source, excluded.model_source
                ),
                profile=COALESCE(
                    run_usage_facts.profile, excluded.profile
                ),
                captured_at=COALESCE(
                    run_usage_facts.captured_at, excluded.captured_at
                ),
                source=CASE
                    WHEN run_usage_facts.source = 'unknown'
                    THEN 'derived'
                    ELSE run_usage_facts.source
                END
            """,
            [
                (
                    str(fact.run_id),
                    fact.origin,
                    fact.provider,
                    fact.model,
                    fact.source,
                    fact.profile,
                    captured_at,
                )
                for fact in reconstructed
            ],
        )
    return facts


def classification_counts(
    facts: Iterable[ActiveProviderFact],
) -> dict[str, int]:
    """Count every classification, including zero-count classes."""

    counts = Counter(fact.classification for fact in facts)
    return {classification: counts[classification] for classification in CLASSIFICATIONS}


def reconstruction_source_counts(
    facts: Iterable[ActiveProviderFact],
) -> dict[str, int]:
    """Count successful read-time reconstructions by evidence source."""

    counts = Counter(
        fact.source
        for fact in facts
        if fact.classification == CLASS_RECONSTRUCTED and fact.source
    )
    return dict(sorted(counts.items()))


def audit_metadata_models(
    kanban_path: str | Path, *, immutable: bool = False
) -> MetadataModelAudit:
    """Audit non-empty ``model`` keys on NULL-provider board runs."""

    with _read_only_connection(
        Path(kanban_path), immutable=immutable
    ) as connection:
        rows = connection.execute(
            "SELECT metadata FROM task_runs WHERE active_provider IS NULL"
        )
        top_level_runs = 0
        any_level_runs = 0
        nested_runs = 0
        nested_only_runs = 0
        for row in rows:
            metadata = _metadata_dict(row["metadata"])
            top_level = bool(_text(metadata.get("model")))
            nested = _has_nested_model(metadata)
            top_level_runs += int(top_level)
            nested_runs += int(nested)
            any_level_runs += int(top_level or nested)
            nested_only_runs += int(nested and not top_level)
    return MetadataModelAudit(
        top_level_runs=top_level_runs,
        any_level_runs=any_level_runs,
        nested_runs=nested_runs,
        nested_only_runs=nested_only_runs,
    )


def _read_only_connection(
    path: Path, *, immutable: bool = False
) -> sqlite3.Connection:
    absolute = path.expanduser().resolve()
    uri = f"file:{quote(str(absolute), safe='/')}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _load_board_runs(connection: sqlite3.Connection) -> list[_BoardRun]:
    return [
        _BoardRun(
            run_id=row["id"],
            status=str(row["status"] or ""),
            active_provider=_text(row["active_provider"]),
            profile=_text(row["profile"]),
            metadata=_metadata_dict(row["metadata"]),
            started_at=_timestamp(row["started_at"]),
            ended_at=_timestamp(row["ended_at"]),
            last_heartbeat_at=_timestamp(row["last_heartbeat_at"]),
        )
        for row in connection.execute(
            "SELECT id, status, active_provider, profile, metadata, "
            "started_at, ended_at, last_heartbeat_at "
            "FROM task_runs ORDER BY id"
        )
    ]


def _load_lane_routes(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], frozenset[str]]:
    routes: dict[tuple[str, str], set[str]] = defaultdict(set)
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lanes'"
    ).fetchone()
    if table is None:
        return {}
    for row in connection.execute("SELECT profiles FROM lanes"):
        profiles = _metadata_dict(row["profiles"])
        for profile, raw_config in profiles.items():
            if not isinstance(raw_config, dict):
                continue
            provider = _text(raw_config.get("provider"))
            model = _text(raw_config.get("model"))
            if not provider or not model:
                continue
            for variant in _model_variants(model):
                routes[(str(profile), variant)].add(provider)
    return {key: frozenset(value) for key, value in routes.items()}


def _configured_provider(
    routes: dict[tuple[str, str], frozenset[str]],
    profile: str | None,
    model: str | None,
) -> str | None:
    if not profile or not model:
        return None
    providers: set[str] = set()
    for variant in _model_variants(model):
        providers.update(routes.get((profile, variant), ()))
    if len(providers) == 1:
        return next(iter(providers))
    return None


def _metadata_provider(metadata: dict[str, Any]) -> str | None:
    return _text(metadata.get("provider")) or _text(
        metadata.get("cost_equivalent_provider")
    )


def _state_provider(
    run: _BoardRun,
    reader: _StateSessionReader,
    routes: dict[tuple[str, str], frozenset[str]],
) -> tuple[str, str | None] | None:
    references: list[tuple[Path, str]] = []
    worker_session_id = _text(run.metadata.get("worker_session_id"))
    profile_path = reader.profile_path(run.profile)
    if worker_session_id and profile_path is not None:
        references.append((profile_path, worker_session_id))

    raw_cost_session_ids = run.metadata.get("cost_session_ids")
    if isinstance(raw_cost_session_ids, list):
        for raw_reference in raw_cost_session_ids:
            reference = _text(raw_reference)
            if not reference or "::" not in reference:
                continue
            raw_path, session_id = reference.rsplit("::", 1)
            embedded_path = reader.embedded_path(raw_path)
            if embedded_path is not None and session_id:
                references.append((embedded_path, session_id))

    candidates: set[tuple[str, str | None]] = set()
    for path, session_id in references:
        session = reader.session(path, session_id)
        if session is None:
            continue
        provider, model = session
        if not provider:
            provider = _configured_provider(routes, run.profile, model)
        if provider:
            candidates.add((provider, model))

    providers = {provider for provider, _model in candidates}
    if len(providers) != 1:
        return None
    provider = next(iter(providers))
    models = {model for candidate_provider, model in candidates if candidate_provider == provider}
    return provider, next(iter(models)) if len(models) == 1 else None


def _cli_providers(
    runs: list[_BoardRun],
    usage_facts_path: Path,
    routes: dict[tuple[str, str], frozenset[str]],
    *,
    immutable: bool,
) -> dict[int | str, tuple[str, str | None, str, str]]:
    facts = _load_cli_facts(usage_facts_path, immutable=immutable)
    if not facts or not runs:
        return {}

    by_run: dict[
        int | str, list[tuple[str, str, str, str]]
    ] = defaultdict(list)
    by_fact: dict[str, list[int | str]] = defaultdict(list)
    for run in runs:
        start = run.started_at
        end = run.ended_at or run.last_heartbeat_at or start
        if start is None or end is None:
            continue
        lower, upper = sorted((start, end))
        for fact in facts:
            if not lower <= fact.captured_at <= upper:
                continue
            provider = _configured_provider(routes, run.profile, fact.model)
            if not provider:
                continue
            by_run[run.run_id].append(
                (fact.fact_id, provider, fact.model, fact.origin)
            )
            by_fact[fact.fact_id].append(run.run_id)

    recovered: dict[
        int | str, tuple[str, str | None, str, str]
    ] = {}
    for run_id, candidates in by_run.items():
        if len(candidates) != 1:
            continue
        fact_id, provider, model, origin = candidates[0]
        if len(by_fact[fact_id]) != 1:
            continue
        recovered[run_id] = (
            provider,
            model,
            SOURCE_CLI_RAW,
            origin,
        )
    return recovered


def _load_cli_facts(
    usage_facts_path: Path, *, immutable: bool
) -> list[_CliFact]:
    if not usage_facts_path.is_file():
        return []
    try:
        with _read_only_connection(
            usage_facts_path, immutable=immutable
        ) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='run_usage_facts'"
            ).fetchone()
            if table is None:
                return []
            result: list[_CliFact] = []
            for row in connection.execute(
                "SELECT run_id, model, captured_at, origin "
                "FROM run_usage_facts "
                "WHERE origin GLOB '*_cli' "
                "AND model IS NOT NULL AND trim(model) <> '' "
                "AND captured_at IS NOT NULL"
            ):
                captured_at = _timestamp(row["captured_at"])
                model = _text(row["model"])
                if captured_at is not None and model:
                    result.append(
                        _CliFact(
                            fact_id=str(row["run_id"]),
                            model=model,
                            captured_at=captured_at,
                            origin=str(row["origin"]),
                        )
                    )
            return result
    except sqlite3.Error:
        return []


def _metadata_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _has_nested_model(metadata: dict[str, Any]) -> bool:
    def visit(value: Any, *, depth: int) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if depth > 0 and key == "model" and _text(child):
                    return True
                if visit(child, depth=depth + 1):
                    return True
        elif isinstance(value, list):
            return any(visit(child, depth=depth + 1) for child in value)
        return False

    return visit(metadata, depth=0)


def _model_variants(model: str) -> frozenset[str]:
    normalized = model.strip().lower()
    return frozenset({normalized, normalized.rsplit("/", 1)[-1]})


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _assert_total_partition(
    runs: list[_BoardRun], facts: list[ActiveProviderFact]
) -> None:
    if len(runs) != len(facts):
        raise AssertionError("provider classification did not preserve every run")
    run_ids = [run.run_id for run in runs]
    fact_ids = [fact.run_id for fact in facts]
    if len(set(fact_ids)) != len(fact_ids) or set(run_ids) != set(fact_ids):
        raise AssertionError("provider classification is not a one-to-one run partition")
    if any(fact.classification not in CLASSIFICATIONS for fact in facts):
        raise AssertionError("provider classification contains an invalid class")
